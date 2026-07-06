from __future__ import annotations

import math
from typing import Any


def verify_elastic_constraint(case_spec: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    if not trajectory:
        return "F1_missing_trajectory", failure("trajectory", 0, 0, "frame_count", 0), evidence

    expected = dict(case_spec.get("expected_physics") or {})
    anchor_id = str(expected.get("anchor_object_id") or first_role(case_spec, {"elastic_constraint_anchor", "constraint_anchor"}) or "anchor")
    body_id = str(expected.get("constrained_object_id") or first_role(case_spec, {"elastic_constrained_body", "constrained_body"}) or "payload")
    rest_length = positive_float(expected.get("rest_length_m"))
    max_extension = positive_float(expected.get("max_extension_m"))
    stiffness = positive_float(expected.get("constraint_stiffness_n_m"))
    if rest_length is None:
        return "F3_invalid_initial_physics_state", failure("elastic_constraint", 0, 0, "rest_length_m", expected.get("rest_length_m")), evidence
    if max_extension is None:
        return "F3_invalid_initial_physics_state", failure("elastic_constraint", 0, 0, "max_extension_m", expected.get("max_extension_m")), evidence
    if stiffness is None:
        return "F3_invalid_initial_physics_state", failure("elastic_constraint", 0, 0, "constraint_stiffness_n_m", expected.get("constraint_stiffness_n_m")), evidence

    samples = []
    trace_events = []
    for frame in trajectory:
        objects = frame_objects(frame)
        anchor = objects.get(anchor_id)
        body = objects.get(body_id)
        if not anchor or not body:
            return "F7_runtime_artifact_incomplete", failure(f"{anchor_id}:{body_id}", frame_id(frame), frame_time(frame), "elastic_constraint_pair_present", False), evidence
        anchor_pos = position(anchor)
        body_pos = position(body)
        measured_distance = dist(anchor_pos, body_pos)
        extension = max(0.0, measured_distance - rest_length)
        event = matching_constraint_event(frame, anchor_id, body_id)
        if event is not None:
            trace_events.append(event)
            measured_distance = float(event.get("measured_distance_m") or measured_distance)
            extension = float(event.get("extension_m") or max(0.0, measured_distance - rest_length))
        samples.append(
            {
                "frame": frame,
                "anchor_pos": anchor_pos,
                "body_pos": body_pos,
                "body_velocity": vec3(body.get("velocity_m_s")),
                "distance": measured_distance,
                "extension": extension,
            }
        )

    if not trace_events:
        return "F7_runtime_artifact_incomplete", failure("elastic_constraint", 0, 0, "constraint_trace_present", False), evidence

    for sample in samples:
        if sample["extension"] > max_extension:
            detail = failure(body_id, frame_id(sample["frame"]), frame_time(sample["frame"]), "elastic_extension_m", round(sample["extension"], 6))
            detail["max_extension_m"] = round(max_extension, 6)
            detail["measured_distance_m"] = round(sample["distance"], 6)
            return "F4_causality_violation", detail, evidence

    min_extension = float(expected.get("expected_min_extension_m") or max_extension * 0.35)
    max_sample = max(samples, key=lambda item: float(item["extension"]))
    if float(max_sample["extension"]) < min_extension:
        detail = failure(body_id, frame_id(max_sample["frame"]), frame_time(max_sample["frame"]), "max_extension_reached_m", round(float(max_sample["extension"]), 6))
        detail["expected_min_extension_m"] = round(min_extension, 6)
        return "F4_causality_violation", detail, evidence

    max_index = samples.index(max_sample)
    rebound_samples = samples[max_index + 1 :]
    if not rebound_samples:
        return "F4_causality_violation", failure(body_id, frame_id(max_sample["frame"]), frame_time(max_sample["frame"]), "post_stretch_rebound_frames", 0), evidence

    min_rebound_speed = float(expected.get("expected_min_rebound_speed_m_s") or 0.1)
    rebound_speeds = [velocity_toward_anchor(sample["anchor_pos"], sample["body_pos"], sample["body_velocity"]) for sample in rebound_samples]
    best_rebound = max(rebound_speeds)
    if best_rebound < min_rebound_speed:
        detail = failure(body_id, frame_id(rebound_samples[-1]["frame"]), frame_time(rebound_samples[-1]["frame"]), "rebound_velocity_toward_anchor_m_s", round(best_rebound, 6))
        detail["expected_min_rebound_speed_m_s"] = round(min_rebound_speed, 6)
        return "F4_causality_violation", detail, evidence

    max_extension_observed = max(float(sample["extension"]) for sample in samples)
    evidence.append(
        {
            "anchor_object_id": anchor_id,
            "constrained_object_id": body_id,
            "rest_length_m": round(rest_length, 6),
            "max_extension_m": round(max_extension_observed, 6),
            "max_allowed_extension_m": round(max_extension, 6),
            "best_rebound_velocity_toward_anchor_m_s": round(best_rebound, 6),
            "constraint_event_count": len(trace_events),
        }
    )
    return None, None, evidence


def matching_constraint_event(frame: dict[str, Any], anchor_id: str, body_id: str) -> dict[str, Any] | None:
    for event in frame.get("constraints") or frame.get("constraint_trace") or []:
        if not isinstance(event, dict):
            continue
        if str(event.get("constraint_type") or "") not in {"elastic_tether", "elastic_constraint", "bungee", "spring_rope"}:
            continue
        event_anchor = str(event.get("anchor_id") or anchor_id)
        event_body = str(event.get("body_id") or event.get("constrained_object_id") or body_id)
        if event_anchor == anchor_id and event_body == body_id:
            return event
    return None


def velocity_toward_anchor(anchor: list[float], body: list[float], velocity: list[float]) -> float:
    direction = [anchor[index] - body[index] for index in range(3)]
    length = math.sqrt(sum(item * item for item in direction))
    if length <= 1e-9:
        return 0.0
    unit = [item / length for item in direction]
    return sum(velocity[index] * unit[index] for index in range(3))


def first_role(case_spec: dict[str, Any], roles: set[str]) -> str | None:
    for obj in case_spec.get("objects") or []:
        if isinstance(obj, dict) and str(obj.get("role") or "") in roles:
            return str(obj.get("id"))
    return None


def positive_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0.0 else None


def frame_objects(frame: dict[str, Any]) -> dict[str, dict[str, Any]]:
    objects = frame.get("objects")
    return objects if isinstance(objects, dict) else {}


def position(state: dict[str, Any]) -> list[float]:
    return vec3(state.get("position_m") or state.get("position") or [0, 0, 0])


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    return [float(padded[0]), float(padded[1]), float(padded[2])]


def dist(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((a[index] - b[index]) ** 2 for index in range(3)))


def frame_id(frame: dict[str, Any]) -> int:
    return int(frame.get("frame") or 0)


def frame_time(frame: dict[str, Any]) -> float:
    return float(frame.get("time_s") or frame.get("time") or 0.0)


def failure(object_id: str, frame: int, time: float, metric: str, value: Any) -> dict[str, Any]:
    return {"object_id": object_id, "frame": frame, "time": time, "metric": metric, "value": value}
