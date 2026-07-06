from __future__ import annotations

import math
from typing import Any


SPIN_GAIN_EPS_DEG_S = 5.0


def verify_spin(case_spec: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    if not trajectory:
        return "F1_missing_trajectory", failure("trajectory", 0, 0, "frame_count", 0), evidence

    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    subject_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"spinning_body", "spin_subject", "angular_damping_subject"}), None)
    if subject_id is None:
        return "F7_runtime_artifact_incomplete", failure("case_spec", 0, 0, "spinning_body_count", 0), evidence

    expected = dict(case_spec.get("expected_physics") or {})
    initial_speed = expected.get("initial_angular_speed_deg_s")
    if not is_positive_number(initial_speed):
        return "F3_invalid_initial_physics_state", failure(subject_id, 0, 0, "initial_angular_speed_deg_s", initial_speed), evidence
    damping = expected.get("angular_damping")
    if not is_positive_number(damping):
        return "F3_invalid_initial_physics_state", failure(subject_id, 0, 0, "angular_damping", damping), evidence

    series = object_series(trajectory, subject_id)
    if len(series) < 2:
        return "F7_runtime_artifact_incomplete", failure(subject_id, 0, 0, "spin_frame_count", len(series)), evidence
    speeds = []
    rotations = []
    for frame, state in series:
        if "angular_velocity_deg_s" not in state:
            return "F7_runtime_artifact_incomplete", failure(subject_id, frame_id(frame), frame_time(frame), "angular_velocity_deg_s", None), evidence
        speeds.append((frame, norm(state.get("angular_velocity_deg_s"))))
        rotations.append((frame, rotation_axis_value(state, str(expected.get("spin_axis") or "z"))))

    for index in range(1, len(speeds)):
        previous_speed = speeds[index - 1][1]
        current_frame, current_speed = speeds[index]
        if current_speed > previous_speed + SPIN_GAIN_EPS_DEG_S:
            return "F4_causality_violation", failure(subject_id, frame_id(current_frame), frame_time(current_frame), "angular_speed_increase_deg_s", round(current_speed - previous_speed, 6)), evidence

    final_frame, final_speed = speeds[-1]
    max_final = expected.get("expected_final_angular_speed_max_deg_s")
    if is_positive_number(max_final) and final_speed > float(max_final):
        return "F4_causality_violation", failure(subject_id, frame_id(final_frame), frame_time(final_frame), "final_angular_speed_deg_s", round(final_speed, 6)), evidence

    speed_drop = speeds[0][1] - final_speed
    min_drop = expected.get("expected_min_angular_speed_drop_deg_s")
    if is_positive_number(min_drop) and speed_drop < float(min_drop):
        return "F4_causality_violation", failure(subject_id, frame_id(final_frame), frame_time(final_frame), "angular_speed_drop_deg_s", round(speed_drop, 6)), evidence

    rotation_delta = max(value for _, value in rotations) - min(value for _, value in rotations)
    min_rotation_delta = expected.get("expected_min_rotation_delta_deg")
    if is_positive_number(min_rotation_delta) and rotation_delta < float(min_rotation_delta):
        return "F4_causality_violation", failure(subject_id, frame_id(final_frame), frame_time(final_frame), "rotation_delta_deg", round(rotation_delta, 6)), evidence

    evidence.append(
        {
            "object_id": subject_id,
            "initial_angular_speed_deg_s": round(speeds[0][1], 6),
            "final_angular_speed_deg_s": round(final_speed, 6),
            "angular_speed_drop_deg_s": round(speed_drop, 6),
            "rotation_delta_deg": round(rotation_delta, 6),
            "angular_damping": float(damping),
        }
    )
    return None, None, evidence


def object_series(trajectory: list[dict[str, Any]], object_id: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    series = []
    for frame in trajectory:
        objects = frame.get("objects")
        if not isinstance(objects, dict):
            continue
        state = objects.get(object_id)
        if isinstance(state, dict):
            series.append((frame, state))
    return series


def rotation_axis_value(state: dict[str, Any], axis: str) -> float:
    index = {"x": 0, "y": 1, "z": 2}.get(axis.casefold(), 2)
    return vec3(state.get("rotation_deg") or state.get("rotation_degrees"))[index]


def is_positive_number(value: Any) -> bool:
    try:
        return float(value) > 0.0
    except (TypeError, ValueError):
        return False


def norm(value: Any) -> float:
    return math.sqrt(sum(item * item for item in vec3(value)))


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    return [float(padded[0]), float(padded[1]), float(padded[2])]


def frame_id(frame: dict[str, Any]) -> int:
    return int(frame.get("frame") or 0)


def frame_time(frame: dict[str, Any]) -> float:
    return float(frame.get("time_s") or frame.get("time") or 0.0)


def failure(object_id: str, frame: int, time: float, metric: str, value: Any) -> dict[str, Any]:
    return {"object_id": object_id, "frame": frame, "time": time, "metric": metric, "value": value}
