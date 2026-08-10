from __future__ import annotations

from typing import Any


Z_DROP_EPS = 0.02
FLOOR_PENETRATION_EPS = -0.05


def verify_falling(case_spec: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    if not trajectory:
        return "F1_missing_trajectory", failure("trajectory", 0, 0, "frame_count", 0), evidence
    falling = [
        str(obj.get("id"))
        for obj in case_spec.get("objects", [])
        if str(obj.get("verification_role") or obj.get("role") or "") in {"falling_body", "stack_block"}
    ]
    if not falling:
        return "F7_runtime_artifact_incomplete", failure("case_spec", 0, 0, "falling_body_count", 0), evidence
    contacts = [contact for frame in trajectory for contact in frame.get("contacts") or [] if isinstance(contact, dict)]
    for object_id in falling:
        series = [(frame, (frame.get("objects") or {}).get(object_id) or {}) for frame in trajectory if object_id in (frame.get("objects") or {})]
        if len(series) < 2:
            return "F1_missing_trajectory", failure(object_id, 0, 0, "series_length", len(series)), evidence
        z0 = position_z(series[0][1])
        zmin = min(position_z(state) for _, state in series)
        if zmin > z0 - Z_DROP_EPS:
            return "F4_causality_violation", failure(object_id, int(series[-1][0].get("frame", 0)), float(series[-1][0].get("time_s", 0)), "z_drop_m", round(z0 - zmin, 6)), evidence
        if zmin < FLOOR_PENETRATION_EPS:
            return "F4_causality_violation", failure(object_id, 0, 0, "floor_penetration_m", zmin), evidence
        evidence.append({"object_id": object_id, "z_start": z0, "z_min": zmin})
    if not contacts:
        return "F2_missing_contact_events", failure("support", 0, 0, "contact_count", 0), evidence
    return None, None, evidence


def position_z(state: dict[str, Any]) -> float:
    pos = state.get("position_m") or state.get("position") or [0, 0, 0]
    if not isinstance(pos, (list, tuple)) or len(pos) < 3:
        return 0.0
    return float(pos[2])


def failure(object_id: str, frame: int, time: float, metric: str, value: Any) -> dict[str, Any]:
    return {"object_id": object_id, "frame": frame, "time": time, "metric": metric, "value": value}
