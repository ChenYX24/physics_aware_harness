from __future__ import annotations

from typing import Any, Mapping


def sample_linear_kinematic_position(motion: Mapping[str, Any], time_s: float) -> list[float]:
    keyframes = motion["keyframes"]
    if time_s <= float(keyframes[0]["time_s"]):
        return [float(value) for value in keyframes[0]["position_m"]]
    if time_s >= float(keyframes[-1]["time_s"]):
        return [float(value) for value in keyframes[-1]["position_m"]]
    for left, right in zip(keyframes, keyframes[1:]):
        left_time = float(left["time_s"])
        right_time = float(right["time_s"])
        if time_s <= right_time:
            alpha = (time_s - left_time) / (right_time - left_time)
            return [
                float(left["position_m"][axis])
                + alpha * (float(right["position_m"][axis]) - float(left["position_m"][axis]))
                for axis in range(3)
            ]
    raise AssertionError("validated kinematic motion has no enclosing interval")


def sample_linear_kinematic_rotation(
    motion: Mapping[str, Any],
    time_s: float,
    default_rotation_deg: list[float] | None = None,
) -> list[float]:
    keyframes = motion["keyframes"]
    if not all("rotation_deg" in keyframe for keyframe in keyframes):
        return [float(value) for value in (default_rotation_deg or [0.0, 0.0, 0.0])]
    if time_s <= float(keyframes[0]["time_s"]):
        return [float(value) for value in keyframes[0]["rotation_deg"]]
    if time_s >= float(keyframes[-1]["time_s"]):
        return [float(value) for value in keyframes[-1]["rotation_deg"]]
    for left, right in zip(keyframes, keyframes[1:]):
        left_time = float(left["time_s"])
        right_time = float(right["time_s"])
        if time_s <= right_time:
            alpha = (time_s - left_time) / (right_time - left_time)
            return [
                float(left["rotation_deg"][axis])
                + alpha * (((float(right["rotation_deg"][axis]) - float(left["rotation_deg"][axis]) + 180.0) % 360.0) - 180.0)
                for axis in range(3)
            ]
    raise AssertionError("validated kinematic motion has no enclosing interval")
