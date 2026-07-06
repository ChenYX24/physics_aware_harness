from __future__ import annotations

import math
from typing import Any


MIN_INITIAL_SPEED = 0.03
MIN_SPEED_DROP = 0.05
REVERSE_EPS = 0.03


def verify_rolling(case_spec: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    if not trajectory:
        return "F1_missing_trajectory", failure("trajectory", 0, 0, "frame_count", 0), evidence

    body_ids = [
        str(obj.get("id"))
        for obj in case_spec.get("objects", [])
        if str(obj.get("role") or "") in {"rolling_body", "friction_subject", "rolling_subject"}
    ]
    if not body_ids:
        return "F7_runtime_artifact_incomplete", failure("case_spec", 0, 0, "rolling_body_count", 0), evidence

    contacts = [contact for frame in trajectory for contact in frame.get("contacts") or [] if isinstance(contact, dict)]
    if not contacts:
        return "F2_missing_contact_events", failure("support", 0, 0, "contact_count", 0), evidence

    expected = dict(case_spec.get("expected_physics") or {})
    min_distance = float(expected.get("expected_min_roll_distance_m") or 0.0)
    max_distance = float(expected.get("expected_max_roll_distance_m") or 1000.0)
    expected_final_speed_max = float(expected.get("expected_final_speed_max_m_s") or 1000.0)

    for object_id in body_ids:
        series = [(frame, (frame.get("objects") or {}).get(object_id) or {}) for frame in trajectory if object_id in (frame.get("objects") or {})]
        if len(series) < 3:
            return "F1_missing_trajectory", failure(object_id, 0, 0, "series_length", len(series)), evidence
        positions = [vec3(state.get("position_m")) for _, state in series]
        velocities = [vec3(state.get("velocity_m_s")) for _, state in series]
        initial_speed = horizontal_speed(velocities[0])
        final_speed = horizontal_speed(velocities[-1])
        displacement = positions[-1][0] - positions[0][0]
        if initial_speed < MIN_INITIAL_SPEED:
            return "F3_invalid_initial_physics_state", failure(object_id, int(series[0][0].get("frame", 0)), float(series[0][0].get("time_s", 0)), "initial_horizontal_speed_m_s", round(initial_speed, 6)), evidence
        if displacement < -REVERSE_EPS:
            return "F4_causality_violation", failure(object_id, int(series[-1][0].get("frame", 0)), float(series[-1][0].get("time_s", 0)), "reverse_displacement_m", round(displacement, 6)), evidence
        if displacement < min_distance:
            return "F4_causality_violation", failure(object_id, int(series[-1][0].get("frame", 0)), float(series[-1][0].get("time_s", 0)), "roll_distance_too_short_m", round(displacement, 6)), evidence
        if displacement > max_distance:
            return "F4_causality_violation", failure(object_id, int(series[-1][0].get("frame", 0)), float(series[-1][0].get("time_s", 0)), "roll_distance_too_long_m", round(displacement, 6)), evidence
        if final_speed > expected_final_speed_max:
            return "F4_causality_violation", failure(object_id, int(series[-1][0].get("frame", 0)), float(series[-1][0].get("time_s", 0)), "final_speed_too_high_m_s", round(final_speed, 6)), evidence
        if initial_speed - final_speed < MIN_SPEED_DROP:
            return "F4_causality_violation", failure(object_id, int(series[-1][0].get("frame", 0)), float(series[-1][0].get("time_s", 0)), "speed_drop_m_s", round(initial_speed - final_speed, 6)), evidence
        evidence.append(
            {
                "object_id": object_id,
                "initial_speed_m_s": round(initial_speed, 6),
                "final_speed_m_s": round(final_speed, 6),
                "speed_drop_m_s": round(initial_speed - final_speed, 6),
                "roll_distance_m": round(displacement, 6),
                "expected_distance_range_m": [round(min_distance, 6), round(max_distance, 6)],
                "friction_dynamic": expected.get("friction_dynamic"),
            }
        )

    return None, None, evidence


def horizontal_speed(velocity: list[float]) -> float:
    return math.sqrt(velocity[0] * velocity[0] + velocity[1] * velocity[1])


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    return [float(padded[0]), float(padded[1]), float(padded[2])]


def failure(object_id: str, frame: int, time: float, metric: str, value: Any) -> dict[str, Any]:
    return {"object_id": object_id, "frame": frame, "time": time, "metric": metric, "value": value}
