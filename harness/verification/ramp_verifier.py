from __future__ import annotations

from typing import Any


DOWNHILL_EPS = 0.02
Z_DROP_EPS = 0.01


def verify_ramp(case_spec: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    if not trajectory:
        return "F1_missing_trajectory", failure("trajectory", 0, 0, "frame_count", 0), evidence

    subject_ids = [
        str(obj.get("id"))
        for obj in case_spec.get("objects", [])
        if str(obj.get("role") or "") in {"rolling_subject", "sliding_subject", "ramp_subject"}
    ]
    if not subject_ids:
        return "F7_runtime_artifact_incomplete", failure("case_spec", 0, 0, "ramp_subject_count", 0), evidence

    expected = dict(case_spec.get("expected_physics") or {})
    min_distance = float(expected.get("expected_min_downhill_displacement_m", DOWNHILL_EPS))
    max_distance = float(expected.get("expected_max_downhill_displacement_m", 1000.0))
    contacts = [contact for frame in trajectory for contact in frame.get("contacts") or [] if isinstance(contact, dict)]

    for subject_id in subject_ids:
        series = [(frame, (frame.get("objects") or {}).get(subject_id) or {}) for frame in trajectory if subject_id in (frame.get("objects") or {})]
        if len(series) < 2:
            return "F1_missing_trajectory", failure(subject_id, 0, 0, "series_length", len(series)), evidence
        start = vec3(series[0][1].get("position_m"))
        end = vec3(series[-1][1].get("position_m"))
        downhill_displacement = end[0] - start[0]
        z_drop = start[2] - end[2]
        if downhill_displacement < min_distance:
            return "F4_causality_violation", failure(subject_id, int(series[-1][0].get("frame", 0)), float(series[-1][0].get("time_s", 0)), "downhill_displacement_m", round(downhill_displacement, 6)), evidence
        if downhill_displacement > max_distance:
            return "F4_causality_violation", failure(subject_id, int(series[-1][0].get("frame", 0)), float(series[-1][0].get("time_s", 0)), "friction_bounded_displacement_m", round(downhill_displacement, 6)), evidence
        if z_drop < Z_DROP_EPS:
            return "F4_causality_violation", failure(subject_id, int(series[-1][0].get("frame", 0)), float(series[-1][0].get("time_s", 0)), "z_drop_m", round(z_drop, 6)), evidence
        evidence.append(
            {
                "object_id": subject_id,
                "downhill_displacement_m": round(downhill_displacement, 6),
                "z_drop_m": round(z_drop, 6),
                "friction_dynamic": expected.get("friction_dynamic"),
                "slope_angle_deg": expected.get("slope_angle_deg"),
            }
        )

    if not contacts:
        return "F2_missing_contact_events", failure("ramp", 0, 0, "contact_count", 0), evidence
    return None, None, evidence


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    return [float(padded[0]), float(padded[1]), float(padded[2])]


def failure(object_id: str, frame: int, time: float, metric: str, value: Any) -> dict[str, Any]:
    return {"object_id": object_id, "frame": frame, "time": time, "metric": metric, "value": value}
