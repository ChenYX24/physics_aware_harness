from __future__ import annotations

import math
from typing import Any


def verify_constraint_motion(case_spec: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    if not trajectory:
        return "F1_missing_trajectory", failure("trajectory", 0, 0, "frame_count", 0), evidence

    expected = dict(case_spec.get("expected_physics") or {})
    constraint_length = expected.get("constraint_length_m")
    if not is_positive_number(constraint_length):
        return "F3_invalid_initial_physics_state", failure("constraint", 0, 0, "constraint_length_m", constraint_length), evidence
    length = float(constraint_length)
    tolerance = float(expected.get("constraint_tolerance_m") or max(0.03, length * 0.05))
    anchor_id = str(expected.get("anchor_object_id") or first_role(case_spec, {"constraint_anchor"}))
    body_id = str(expected.get("constrained_object_id") or first_role(case_spec, {"constrained_body"}))
    if not anchor_id or not body_id or anchor_id == "None" or body_id == "None":
        return "F7_runtime_artifact_incomplete", failure("case_spec", 0, 0, "constraint_object_ids", [anchor_id, body_id]), evidence

    series = []
    for frame in trajectory:
        objects = frame_objects(frame)
        if anchor_id not in objects or body_id not in objects:
            return "F7_runtime_artifact_incomplete", failure(f"{anchor_id}:{body_id}", frame_id(frame), frame_time(frame), "constraint_pair_present", False), evidence
        anchor = vec3(objects[anchor_id].get("position_m"))
        body = vec3(objects[body_id].get("position_m"))
        distance = dist(anchor, body)
        error = abs(distance - length)
        if error > tolerance:
            detail = failure(body_id, frame_id(frame), frame_time(frame), "constraint_length_error_m", round(error, 6))
            detail["measured_distance_m"] = round(distance, 6)
            detail["expected_length_m"] = round(length, 6)
            detail["tolerance_m"] = round(tolerance, 6)
            return "F4_causality_violation", detail, evidence
        series.append((frame, anchor, body, distance))

    max_step = float(expected.get("expected_max_step_displacement_m") or length * 0.9)
    for index in range(1, len(series)):
        step = dist(series[index - 1][2], series[index][2])
        if step > max_step:
            detail = failure(body_id, frame_id(series[index][0]), frame_time(series[index][0]), "teleport_step_displacement_m", round(step, 6))
            detail["max_step_displacement_m"] = round(max_step, 6)
            return "F4_causality_violation", detail, evidence

    if bool(expected.get("require_center_crossing", False)):
        x_values = [body[0] - anchor[0] for _, anchor, body, _ in series]
        if min(x_values) >= -0.02 or max(x_values) <= 0.02:
            return "F4_causality_violation", failure(body_id, frame_id(series[-1][0]), frame_time(series[-1][0]), "center_crossing_x_span_m", [round(min(x_values), 6), round(max(x_values), 6)]), evidence

    evidence.append(
        {
            "anchor_id": anchor_id,
            "constrained_object_id": body_id,
            "constraint_length_m": round(length, 6),
            "max_length_error_m": round(max(abs(item[3] - length) for item in series), 6),
            "max_step_displacement_m": round(max([0.0] + [dist(series[idx - 1][2], series[idx][2]) for idx in range(1, len(series))]), 6),
            "frame_count": len(series),
        }
    )
    return None, None, evidence


def first_role(case_spec: dict[str, Any], roles: set[str]) -> str | None:
    for obj in case_spec.get("objects") or []:
        if isinstance(obj, dict) and str(obj.get("role") or "") in roles:
            return str(obj.get("id"))
    return None


def is_positive_number(value: Any) -> bool:
    try:
        return float(value) > 0.0
    except (TypeError, ValueError):
        return False


def frame_objects(frame: dict[str, Any]) -> dict[str, dict[str, Any]]:
    objects = frame.get("objects")
    return objects if isinstance(objects, dict) else {}


def frame_id(frame: dict[str, Any]) -> int:
    return int(frame.get("frame") or 0)


def frame_time(frame: dict[str, Any]) -> float:
    return float(frame.get("time_s") or frame.get("time") or 0.0)


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    return [float(padded[0]), float(padded[1]), float(padded[2])]


def dist(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((a[idx] - b[idx]) ** 2 for idx in range(3)))


def failure(object_id: str, frame: int, time: float, metric: str, value: Any) -> dict[str, Any]:
    return {"object_id": object_id, "frame": frame, "time": time, "metric": metric, "value": value}
