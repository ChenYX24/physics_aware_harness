from __future__ import annotations

import math
from typing import Any


ROTATION_THRESHOLD_DEG = 12.0
DISPLACEMENT_THRESHOLD_M = 0.01
SPEED_THRESHOLD_M_S = 0.05


def verify_ordered_contact_propagation(
    case_spec: dict[str, Any],
    trajectory: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    if not trajectory:
        return "F1_missing_trajectory", failure("trajectory", 0, 0, "frame_count", 0), evidence
    edges = ordered_contact_edges(case_spec)
    chain = chain_from_edges(edges)
    if len(chain) < 3:
        return "F7_runtime_artifact_incomplete", failure("case_spec", 0, 0, "ordered_chain_count", len(chain)), evidence
    if len(edges) != len(chain) - 1:
        return "F7_runtime_artifact_incomplete", failure("case_spec", 0, 0, "ordered_chain_contiguous", False), evidence

    object_specs = {
        str(obj.get("id")): obj
        for obj in case_spec.get("objects", [])
        if isinstance(obj, dict) and obj.get("id")
    }
    first_objects = frame_objects(trajectory[0])
    missing = next((object_id for object_id in chain if object_id not in first_objects), None)
    if missing is not None:
        return "F7_runtime_artifact_incomplete", failure(missing, 0, 0, "initial_state_present", False), evidence

    activation = {
        object_id: first_activation_frame(
            trajectory,
            object_id,
            domino=str((object_specs.get(object_id) or {}).get("role") or "").casefold() == "domino",
        )
        for object_id in chain
    }
    contacts = contact_pairs_by_frame(trajectory)
    previous_contact_frame = -1
    for source_id, target_id in edges:
        pair = tuple(sorted((source_id, target_id)))
        contact_frame = contacts.get(pair)
        if contact_frame is None:
            return "F2_missing_contact_events", failure(
                target_id,
                activation.get(target_id),
                0,
                "missing_contact_edge",
                list(pair),
                first_broken_edge=[source_id, target_id],
            ), evidence
        if contact_frame < previous_contact_frame:
            return "F4_causality_violation", failure(
                target_id,
                contact_frame,
                frame_time_by_id(trajectory, contact_frame),
                "contact_chain_order",
                {"previous_frame": previous_contact_frame, "current_frame": contact_frame},
                first_broken_edge=[source_id, target_id],
            ), evidence
        activation_frame = activation.get(target_id)
        if activation_frame is None:
            return "F4_causality_violation", failure(
                target_id,
                -1,
                0,
                "activation_frame",
                None,
                first_broken_edge=[source_id, target_id],
            ), evidence
        if activation_frame < contact_frame:
            return "F4_causality_violation", failure(
                target_id,
                activation_frame,
                frame_time_by_id(trajectory, activation_frame),
                "activation_before_contact",
                contact_frame,
                first_broken_edge=[source_id, target_id],
            ), evidence
        evidence.append(
            {
                "edge": [source_id, target_id],
                "contact_frame": contact_frame,
                "activation_frame": activation_frame,
            }
        )
        previous_contact_frame = contact_frame

    if all(str((object_specs.get(object_id) or {}).get("role") or "").casefold() == "domino" for object_id in chain):
        passive_activations = [activation[object_id] for object_id in chain[1:] if activation[object_id] is not None]
        if len(passive_activations) != len(set(passive_activations)):
            return "F4_causality_violation", failure(
                "passive_dominoes",
                min(passive_activations),
                0,
                "simultaneous_activation_frames",
                passive_activations,
            ), evidence
    return None, None, evidence


def ordered_contact_edges(case_spec: dict[str, Any]) -> list[tuple[str, str]]:
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    for key in ("collision_graph", "contact_order"):
        raw = expected.get(key)
        if isinstance(raw, list) and raw:
            edges = [
                (str(edge[0]), str(edge[1]))
                for edge in raw
                if isinstance(edge, list) and len(edge) >= 2 and edge[0] and edge[1]
            ]
            if edges:
                return longest_contiguous_edge_segment(edges)
    ordered = expected.get("ordered_chain")
    if isinstance(ordered, list) and len(ordered) >= 2:
        ids = [str(value) for value in ordered]
        return list(zip(ids, ids[1:]))
    domino_ids = [
        str(obj.get("id"))
        for obj in case_spec.get("objects", [])
        if isinstance(obj, dict) and str(obj.get("role") or "").casefold() == "domino"
    ]
    return list(zip(domino_ids, domino_ids[1:]))


def longest_contiguous_edge_segment(edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Select the declared sequential path from legacy mixed contact graphs."""
    segments: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for edge in edges:
        if current and edge[0] != current[-1][1]:
            segments.append(current)
            current = []
        current.append(edge)
    if current:
        segments.append(current)
    return max(segments, key=len, default=[])


def chain_from_edges(edges: list[tuple[str, str]]) -> list[str]:
    if not edges:
        return []
    chain = [edges[0][0], edges[0][1]]
    for source_id, target_id in edges[1:]:
        if source_id != chain[-1]:
            return chain
        chain.append(target_id)
    return chain


def first_activation_frame(
    trajectory: list[dict[str, Any]],
    object_id: str,
    *,
    domino: bool,
) -> int | None:
    initial = frame_objects(trajectory[0]).get(object_id) or {}
    initial_position = position(initial)
    initial_rotation = rotation(initial)
    for index, frame in enumerate(trajectory):
        state = frame_objects(frame).get(object_id) or {}
        if domino:
            if max(abs(rotation(state)[axis] - initial_rotation[axis]) for axis in range(3)) >= ROTATION_THRESHOLD_DEG:
                return frame_id(frame, index)
            continue
        if distance(position(state), initial_position) >= DISPLACEMENT_THRESHOLD_M:
            return frame_id(frame, index)
        if norm(state.get("velocity_m_s")) >= SPEED_THRESHOLD_M_S:
            return frame_id(frame, index)
    return None


def contact_pairs_by_frame(trajectory: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    result: dict[tuple[str, str], int] = {}
    for index, frame in enumerate(trajectory):
        current_frame = frame_id(frame, index)
        for contact in frame.get("contacts") or []:
            if not isinstance(contact, dict) or not meaningful_runtime_contact(contact):
                continue
            objects = [str(item) for item in contact.get("objects") or []]
            if len(objects) >= 2:
                result.setdefault(tuple(sorted(objects[:2])), current_frame)
    return result


def meaningful_runtime_contact(contact: dict[str, Any]) -> bool:
    if "native_collision" in contact:
        return contact.get("native_collision") is True
    method = str(contact.get("method") or contact.get("raw_method") or "").casefold()
    if "bounds" in method and abs(float(contact.get("normal_impulse_n_s") or 0.0)) <= 1e-9:
        return False
    return True


def frame_objects(frame: dict[str, Any]) -> dict[str, dict[str, Any]]:
    objects = frame.get("objects")
    return objects if isinstance(objects, dict) else {}


def frame_id(frame: dict[str, Any], default: int = 0) -> int:
    return int(frame.get("frame", default))


def frame_time_by_id(trajectory: list[dict[str, Any]], wanted_frame: int) -> float:
    for index, frame in enumerate(trajectory):
        if frame_id(frame, index) == wanted_frame:
            return float(frame.get("time_s") or frame.get("time") or 0.0)
    return 0.0


def position(state: dict[str, Any]) -> list[float]:
    return vec3(state.get("position_m") or state.get("position"))


def rotation(state: dict[str, Any]) -> list[float]:
    return vec3(state.get("rotation_deg") or state.get("rotation_degrees"))


def norm(value: Any) -> float:
    return math.sqrt(sum(component * component for component in vec3(value)))


def distance(left: list[float], right: list[float]) -> float:
    return math.sqrt(sum((left[index] - right[index]) ** 2 for index in range(3)))


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        value = [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    return [float(padded[0]), float(padded[1]), float(padded[2])]


def failure(
    object_id: str,
    frame: int | None,
    time: float,
    metric: str,
    value: Any,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "object_id": object_id,
        "frame": frame,
        "time": time,
        "metric": metric,
        "value": value,
        **extra,
    }
