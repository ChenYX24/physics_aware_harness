from __future__ import annotations

import math
from typing import Any


DEFAULT_EPS = 0.05
DISPLACEMENT_EPS = 0.01


def verify_agent_action(case_spec: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    if not trajectory:
        return "F1_missing_trajectory", failure("trajectory", 0, 0, "frame_count", 0), evidence

    expected = dict(case_spec.get("expected_physics") or {})
    action_trace = collect_action_trace(case_spec, trajectory)
    if not action_trace:
        return "F7_runtime_artifact_incomplete", failure("action_trace", 0, 0, "action_trace_count", 0), evidence

    target_id = str(expected.get("target_object_id") or first_target_id(case_spec))
    actor_id = str(expected.get("action_actor_id") or first_actor_id(case_spec))
    if not target_id or target_id == "None":
        return "F7_runtime_artifact_incomplete", failure("case_spec", 0, 0, "target_object_id", None), evidence

    action = first_action_for_target(action_trace, target_id)
    if action is None:
        return "F7_runtime_artifact_incomplete", failure(target_id, 0, 0, "target_action_count", 0), evidence
    required_actions = [str(item) for item in expected.get("required_action_sequence") or []]
    target_actions = [
        str(item.get("action_type") or "")
        for item in sorted(action_trace, key=lambda item: (int(item.get("frame") or 0), float(item.get("time_s") or 0.0)))
        if str(item.get("target_id") or item.get("object_id") or "") == target_id
    ]
    if required_actions and not is_subsequence(required_actions, target_actions):
        return "F7_runtime_artifact_incomplete", failure(target_id, 0, 0, "required_action_sequence", {"expected": required_actions, "actual": target_actions}), evidence
    action_frame = int(action.get("frame") if action.get("frame") is not None else expected.get("action_frame") or 0)
    action_time = float(action.get("time_s") if action.get("time_s") is not None else expected.get("action_time_s") or 0.0)
    pre_eps = float(expected.get("passive_pre_action_velocity_epsilon_m_s") or DEFAULT_EPS)

    first = trajectory[0]
    initial_state = frame_objects(first).get(target_id) or {}
    if norm(initial_state.get("velocity_m_s")) > pre_eps:
        return "F5_passive_precontact_motion", failure(target_id, frame_id(first), frame_time(first), "preaction_velocity_m_s", round(norm(initial_state.get("velocity_m_s")), 6)), evidence

    initial_position = state_position(initial_state)
    for frame in trajectory:
        if frame_id(frame) >= action_frame:
            break
        state = frame_objects(frame).get(target_id) or {}
        speed = norm(state.get("velocity_m_s"))
        displacement = distance(state_position(state), initial_position)
        if speed > pre_eps or displacement > DISPLACEMENT_EPS:
            detail = failure(target_id, frame_id(frame), frame_time(frame), "preaction_velocity_m_s", round(speed, 6))
            detail["displacement_m"] = round(displacement, 6)
            detail["action_frame"] = action_frame
            return "F5_passive_precontact_motion", detail, evidence

    coupling_type = str(expected.get("coupling_type") or action.get("action_type") or "")
    if coupling_type in {"push", "pick", "pick_place"}:
        contacts = all_contacts(trajectory)
        if not contact_pair_exists(contacts, actor_id, target_id):
            return "F2_missing_contact_events", failure(f"{actor_id}:{target_id}", action_frame, action_time, "missing_expected_action_contact", [actor_id, target_id]), evidence
    elif coupling_type in {"throw", "release"}:
        impulse = action.get("impulse_n_s")
        if not isinstance(impulse, list) and not action.get("release"):
            return "F3_invalid_initial_physics_state", failure(target_id, action_frame, action_time, "release_impulse", None), evidence

    post_series = [(frame, frame_objects(frame).get(target_id) or {}) for frame in trajectory if frame_id(frame) >= action_frame and target_id in frame_objects(frame)]
    if not post_series:
        return "F1_missing_trajectory", failure(target_id, action_frame, action_time, "post_action_series_length", 0), evidence
    final_frame, final_state = post_series[-1]
    final_position = state_position(final_state)
    final_speed = norm(final_state.get("velocity_m_s"))
    displacement = distance(final_position, initial_position)
    min_displacement = float(expected.get("expected_min_target_displacement_m") or 0.0)
    min_speed = float(expected.get("expected_min_post_action_speed_m_s") or 0.0)
    if displacement < min_displacement and final_speed < min_speed:
        detail = failure(target_id, frame_id(final_frame), frame_time(final_frame), "post_action_response", {"displacement_m": round(displacement, 6), "speed_m_s": round(final_speed, 6)})
        detail["action_frame"] = action_frame
        return "F4_causality_violation", detail, evidence

    effect = expected.get("object_effect") if isinstance(expected.get("object_effect"), dict) else {}
    minimum_lift = float(effect.get("minimum_lift_height_m") or 0.0)
    lift_height = max((state_position(state)[2] - initial_position[2] for _, state in post_series), default=0.0)
    if lift_height < minimum_lift:
        return "F4_causality_violation", failure(target_id, frame_id(final_frame), frame_time(final_frame), "object_lift_height_m", round(lift_height, 6)), evidence

    goal = effect.get("final_goal_region") if isinstance(effect.get("final_goal_region"), dict) else {}
    if goal and not inside_box(final_position, vec3(goal.get("center_m")), vec3(goal.get("half_extent_m"))):
        return "F4_causality_violation", failure(target_id, frame_id(final_frame), frame_time(final_frame), "final_goal_region", {"position_m": final_position, "goal": goal}), evidence

    maximum_final_speed = effect.get("maximum_final_speed_m_s")
    if maximum_final_speed is not None and final_speed > float(maximum_final_speed):
        return "F4_causality_violation", failure(target_id, frame_id(final_frame), frame_time(final_frame), "final_speed_m_s", round(final_speed, 6)), evidence

    final_contact = effect.get("required_final_contact")
    if isinstance(final_contact, list) and len(final_contact) == 2:
        release_frame = max((int(item.get("frame") or action_frame) for item in action_trace if item.get("release") or item.get("action_type") == "release"), default=action_frame)
        if not contact_pair_exists(all_contacts(trajectory), str(final_contact[0]), str(final_contact[1]), minimum_frame=release_frame):
            return "F2_missing_contact_events", failure(target_id, frame_id(final_frame), frame_time(final_frame), "missing_final_contact", final_contact), evidence

    evidence.append(
        {
            "actor_id": actor_id,
            "target_id": target_id,
            "action_type": action.get("action_type"),
            "action_frame": action_frame,
            "action_time_s": action_time,
            "target_displacement_m": round(displacement, 6),
            "target_final_speed_m_s": round(final_speed, 6),
            "coupling_type": coupling_type,
            "object_lift_height_m": round(lift_height, 6),
            "object_effect": effect,
        }
    )
    return None, None, evidence


def collect_action_trace(case_spec: dict[str, Any], trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    actions = expected.get("action_trace") if isinstance(expected, dict) else None
    if isinstance(actions, list) and actions:
        return [dict(action) for action in actions if isinstance(action, dict)]
    case_actions = case_spec.get("action_trace")
    if isinstance(case_actions, list) and case_actions:
        return [dict(action) for action in case_actions if isinstance(action, dict)]
    collected = []
    for frame in trajectory:
        for action in frame.get("actions") or []:
            if isinstance(action, dict):
                row = dict(action)
                row.setdefault("frame", frame_id(frame))
                row.setdefault("time_s", frame_time(frame))
                collected.append(row)
    return collected


def first_action_for_target(actions: list[dict[str, Any]], target_id: str) -> dict[str, Any] | None:
    for action in sorted(actions, key=lambda item: (int(item.get("frame") or 0), float(item.get("time_s") or 0.0))):
        if str(action.get("target_id") or action.get("object_id") or "") == target_id:
            return action
    return None


def first_target_id(case_spec: dict[str, Any]) -> str | None:
    for obj in case_spec.get("objects") or []:
        if isinstance(obj, dict) and str(obj.get("role") or "") in {"action_coupled_body", "pushed_body", "thrown_body", "rigid_body_payload"}:
            return str(obj.get("id"))
    passive = case_spec.get("passive_objects") or []
    return str(passive[0]) if passive else None


def first_actor_id(case_spec: dict[str, Any]) -> str | None:
    for obj in case_spec.get("objects") or []:
        if isinstance(obj, dict) and str(obj.get("role") or "") in {"active_agent", "agent_controller", "pushing_agent", "throwing_agent"}:
            return str(obj.get("id"))
    active = case_spec.get("active_objects") or []
    return str(active[0]) if active else None


def all_contacts(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [contact for frame in trajectory for contact in frame.get("contacts") or [] if isinstance(contact, dict)]


def contact_pair_exists(contacts: list[dict[str, Any]], a: str, b: str, *, minimum_frame: int = 0) -> bool:
    expected = {a, b}
    return any(
        int(contact.get("frame") or 0) >= minimum_frame
        and expected.issubset({str(item) for item in contact.get("objects") or []})
        for contact in contacts
    )


def is_subsequence(expected: list[str], actual: list[str]) -> bool:
    remaining = iter(actual)
    return all(any(item == required for item in remaining) for required in expected)


def inside_box(position: list[float], center: list[float], half_extent: list[float]) -> bool:
    return all(abs(position[idx] - center[idx]) <= half_extent[idx] for idx in range(3))


def frame_objects(frame: dict[str, Any]) -> dict[str, dict[str, Any]]:
    objects = frame.get("objects")
    return objects if isinstance(objects, dict) else {}


def state_position(state: dict[str, Any]) -> list[float]:
    return vec3(state.get("position_m") or state.get("position"))


def frame_id(frame: dict[str, Any]) -> int:
    return int(frame.get("frame") or 0)


def frame_time(frame: dict[str, Any]) -> float:
    return float(frame.get("time_s") or frame.get("time") or 0.0)


def norm(value: Any) -> float:
    return math.sqrt(sum(item * item for item in vec3(value)))


def distance(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((a[idx] - b[idx]) ** 2 for idx in range(3)))


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    return [float(padded[0]), float(padded[1]), float(padded[2])]


def failure(object_id: str, frame: int, time: float, metric: str, value: Any) -> dict[str, Any]:
    return {"object_id": object_id, "frame": frame, "time": time, "metric": metric, "value": value}
