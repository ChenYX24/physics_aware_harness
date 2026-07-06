from __future__ import annotations

import math
from typing import Any


MAGNETIC_EPS_M = 0.02
FIELD_EPS = 1e-9


def verify_magnetic(case_spec: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    if not trajectory:
        return "F1_missing_trajectory", failure("trajectory", 0, 0, "frame_count", 0), evidence

    expected = dict(case_spec.get("expected_physics") or {})
    mode = str(expected.get("magnetic_mode") or "").casefold()
    if mode not in {"attract", "repel"}:
        return "F3_invalid_initial_physics_state", failure("magnetic_field", 0, 0, "magnetic_mode", mode or None), evidence
    strength = float(expected.get("magnetic_strength") or 0.0)
    if abs(strength) <= FIELD_EPS:
        return "F3_invalid_initial_physics_state", failure("magnetic_field", 0, 0, "magnetic_strength", strength), evidence

    source_id = str(expected.get("source_object_id") or find_role_object(case_spec, {"magnetic_source", "magnet_source"}))
    body_id = str(expected.get("magnetic_subject_id") or find_role_object(case_spec, {"magnetized_body", "magnetic_body", "magnetic_subject"}))
    if not body_id or body_id == "None":
        return "F7_runtime_artifact_incomplete", failure("case_spec", 0, 0, "magnetic_subject_count", 0), evidence
    source_position = source_position_from_case(case_spec, source_id, expected)

    series = [(frame, (frame.get("objects") or {}).get(body_id) or {}) for frame in trajectory if body_id in (frame.get("objects") or {})]
    if len(series) < 2:
        return "F1_missing_trajectory", failure(body_id, 0, 0, "series_length", len(series)), evidence
    start = vec3(series[0][1].get("position_m"))
    end = vec3(series[-1][1].get("position_m"))
    initial_distance = planar_distance(start, source_position)
    final_distance = planar_distance(end, source_position)
    if initial_distance <= MAGNETIC_EPS_M:
        return "F3_invalid_initial_physics_state", failure(body_id, 0, 0, "initial_distance_to_source_m", round(initial_distance, 6)), evidence

    radial_displacement = initial_distance - final_distance if mode == "attract" else final_distance - initial_distance
    min_displacement = float(expected.get("expected_min_radial_displacement_m") or 0.0)
    max_displacement = float(expected.get("expected_max_radial_displacement_m") or 1000.0)
    frame_id = int(series[-1][0].get("frame", 0))
    time_s = float(series[-1][0].get("time_s", 0.0))
    if radial_displacement < -MAGNETIC_EPS_M:
        return "F4_causality_violation", failure(body_id, frame_id, time_s, "magnetic_radial_direction_m", round(radial_displacement, 6)), evidence
    if radial_displacement < min_displacement:
        return "F4_causality_violation", failure(body_id, frame_id, time_s, "magnetic_radial_displacement_too_small_m", round(radial_displacement, 6)), evidence
    if radial_displacement > max_displacement:
        return "F4_causality_violation", failure(body_id, frame_id, time_s, "magnetic_radial_displacement_too_large_m", round(radial_displacement, 6)), evidence

    evidence.append(
        {
            "object_id": body_id,
            "source_object_id": source_id,
            "magnetic_mode": mode,
            "magnetic_strength": round(strength, 6),
            "initial_distance_to_source_m": round(initial_distance, 6),
            "final_distance_to_source_m": round(final_distance, 6),
            "radial_displacement_m": round(radial_displacement, 6),
        }
    )
    return None, None, evidence


def find_role_object(case_spec: dict[str, Any], roles: set[str]) -> str | None:
    for obj in case_spec.get("objects", []):
        if isinstance(obj, dict) and str(obj.get("role") or "") in roles:
            return str(obj.get("id"))
    return None


def source_position_from_case(case_spec: dict[str, Any], source_id: str, expected: dict[str, Any]) -> list[float]:
    if isinstance(expected.get("field_center_m"), (list, tuple)):
        return vec3(expected.get("field_center_m"))
    for obj in case_spec.get("objects", []):
        if isinstance(obj, dict) and str(obj.get("id")) == source_id:
            return vec3(obj.get("initial_position_m") or obj.get("position_m") or [0.0, 0.0, 0.0])
    return [0.0, 0.0, 0.0]


def planar_distance(left: list[float], right: list[float]) -> float:
    return math.sqrt((left[0] - right[0]) ** 2 + (left[1] - right[1]) ** 2)


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    return [float(padded[0]), float(padded[1]), float(padded[2])]


def failure(object_id: str, frame: int, time: float, metric: str, value: Any) -> dict[str, Any]:
    return {"object_id": object_id, "frame": frame, "time": time, "metric": metric, "value": value}
