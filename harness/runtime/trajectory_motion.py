from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


def kinematic_motion_from_trajectory(
    trajectory: list[Mapping[str, Any]],
    object_id: str,
) -> dict[str, Any]:
    keyframes: list[dict[str, Any]] = []
    previous_time = -1.0
    for frame_index, frame in enumerate(trajectory):
        time_value = frame.get("time_s", frame.get("time"))
        state = (frame.get("objects") or {}).get(object_id)
        if not isinstance(time_value, (int, float)) or not isinstance(state, Mapping):
            raise ValueError(f"trajectory frame {frame_index} is missing time or object {object_id}")
        time_s = float(time_value)
        position = state.get("position_m", state.get("position"))
        rotation = state.get("rotation_deg", state.get("rotation_degrees"))
        if (
            not isinstance(position, list) or len(position) != 3
            or not isinstance(rotation, list) or len(rotation) != 3
        ):
            raise ValueError(f"trajectory frame {frame_index} has no complete transform for {object_id}")
        if time_s <= previous_time or (frame_index == 0 and abs(time_s) > 1e-9):
            raise ValueError("trajectory times must be strictly increasing from zero")
        previous_time = time_s
        keyframes.append({
            "time_s": round(time_s, 8),
            "position_m": [float(value) for value in position],
            "rotation_deg": [float(value) for value in rotation],
        })
    if len(keyframes) < 2:
        raise ValueError("trajectory must contain at least two frames")
    return {"interpolation": "linear", "keyframes": keyframes}


def bake_trajectory_into_case(
    case_spec: Mapping[str, Any],
    trajectory: list[Mapping[str, Any]],
    mappings: Mapping[str, str],
    *,
    case_id: str | None = None,
) -> dict[str, Any]:
    result = deepcopy(dict(case_spec))
    objects = {
        str(obj.get("id") or ""): obj
        for obj in result.get("objects") or []
        if isinstance(obj, dict) and obj.get("id")
    }
    duration_s = 0.0
    for source_id, target_id in mappings.items():
        target = objects.get(target_id)
        if target is None:
            raise ValueError(f"target object is missing: {target_id}")
        physics = target.get("physics") if isinstance(target.get("physics"), dict) else {}
        if target.get("role") != "rigid_collider" or physics.get("body_type") != "kinematic":
            raise ValueError(f"target must be a kinematic rigid_collider: {target_id}")
        motion = kinematic_motion_from_trajectory(trajectory, source_id)
        target["kinematic_motion"] = motion
        target.setdefault("initial_state", {})["position_m"] = list(motion["keyframes"][0]["position_m"])
        target["initial_state"]["rotation_deg"] = list(motion["keyframes"][0]["rotation_deg"])
        duration_s = max(duration_s, float(motion["keyframes"][-1]["time_s"]))
    if case_id:
        result.setdefault("identity", {})["case_id"] = case_id
    if duration_s > 0.0:
        result.setdefault("scene", {})["duration_s"] = duration_s
    return result
