from __future__ import annotations

import math
from typing import Any, Mapping


ARTICULATED_BODY_MODEL_ID = "harness_ue_mannequin_v1"
ARTICULATED_BODY_ASSET_PATH = "/Game/Mannequin/Character/Mesh/SK_Mannequin.SK_Mannequin"
ARTICULATED_BODY_PHYSICS_ASSET_PATH = "/Game/Mannequin/Character/Mesh/SK_Mannequin_PhysicsAsset.SK_Mannequin_PhysicsAsset"
ARTICULATED_BODY_AUTHORED_SIZE_M = (0.58, 0.36, 1.92)
ARTICULATED_BODY_MODES = frozenset({"kinematic", "ragdoll"})
ARTICULATED_BODY_BONES = frozenset({
    "root",
    "pelvis",
    "spine_01",
    "spine_02",
    "spine_03",
    "neck_01",
    "head",
    "clavicle_l",
    "upperarm_l",
    "lowerarm_l",
    "hand_l",
    "clavicle_r",
    "upperarm_r",
    "lowerarm_r",
    "hand_r",
    "thigh_l",
    "calf_l",
    "foot_l",
    "thigh_r",
    "calf_r",
    "foot_r",
})


class ArticulatedBodyContractError(ValueError):
    pass


def is_articulated_body_solver(value: Any) -> bool:
    return isinstance(value, Mapping) and str(value.get("type") or "") == "articulated_body"


def compile_articulated_body_contract(
    solver: Mapping[str, Any],
    *,
    duration_s: float,
    known_object_ids: set[str],
    object_id: str,
) -> dict[str, Any]:
    unknown = sorted(
        str(key)
        for key in solver
        if str(key) not in {
            "type",
            "model",
            "mode",
            "ragdoll_start_time_s",
            "root_keyframes",
            "joint_keyframes",
            "attachments",
        }
    )
    if unknown:
        raise ArticulatedBodyContractError(f"unsupported articulated_body fields: {', '.join(unknown)}")
    if solver.get("model") != ARTICULATED_BODY_MODEL_ID:
        raise ArticulatedBodyContractError(f"model must be {ARTICULATED_BODY_MODEL_ID}")
    mode = str(solver.get("mode") or "")
    if mode not in ARTICULATED_BODY_MODES:
        raise ArticulatedBodyContractError(f"mode must be one of {sorted(ARTICULATED_BODY_MODES)}")
    duration = _finite_number(duration_s, "scene duration")
    if duration <= 0.0:
        raise ArticulatedBodyContractError("scene duration must be positive")

    root_keyframes = _compile_root_keyframes(solver.get("root_keyframes"), duration)
    joint_keyframes = _compile_joint_keyframes(solver.get("joint_keyframes"), duration)
    ragdoll_start = solver.get("ragdoll_start_time_s")
    if mode == "kinematic" and ragdoll_start is not None:
        raise ArticulatedBodyContractError("ragdoll_start_time_s is only valid in ragdoll mode")
    if mode == "ragdoll":
        ragdoll_start = _time(ragdoll_start, "ragdoll_start_time_s", duration)
        if any(
            abs(float(component)) > 1e-9
            for keyframe in joint_keyframes
            for rotation in keyframe["rotations_deg"].values()
            for component in rotation
        ):
            raise ArticulatedBodyContractError("ragdoll mode uses the fixed reference pose; joint offsets require kinematic mode")
    attachments = _compile_attachments(
        solver.get("attachments"),
        duration,
        known_object_ids=known_object_ids,
        object_id=object_id,
        ragdoll_start_time_s=ragdoll_start if mode == "ragdoll" else None,
    )
    return {
        "schema_version": "harness_articulated_body_contract_v1",
        "type": "articulated_body",
        "model": ARTICULATED_BODY_MODEL_ID,
        "asset_path": ARTICULATED_BODY_ASSET_PATH,
        "physics_asset_path": ARTICULATED_BODY_PHYSICS_ASSET_PATH,
        "authored_size_m": list(ARTICULATED_BODY_AUTHORED_SIZE_M),
        "mode": mode,
        "rotation_space": "component_offset",
        "root_keyframes": root_keyframes,
        "joint_keyframes": joint_keyframes,
        "attachments": attachments,
        **({"ragdoll_start_time_s": ragdoll_start} if mode == "ragdoll" else {}),
    }


def sample_articulated_body_contract(contract: Mapping[str, Any], time_s: float) -> dict[str, Any]:
    return {
        "root_position_offset_m": _sample_vec_keyframes(
            contract.get("root_keyframes") or [], time_s, "position_offset_m"
        ),
        "root_rotation_offset_deg": _sample_vec_keyframes(
            contract.get("root_keyframes") or [], time_s, "rotation_offset_deg"
        ),
        "joint_rotations_deg": _sample_joint_keyframes(contract.get("joint_keyframes") or [], time_s),
    }


def _compile_root_keyframes(value: Any, duration_s: float) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ArticulatedBodyContractError("root_keyframes must be a non-empty list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ArticulatedBodyContractError(f"root_keyframes[{index}] must be an object")
        unknown = sorted(str(key) for key in item if str(key) not in {"time_s", "position_offset_m", "rotation_offset_deg"})
        if unknown:
            raise ArticulatedBodyContractError(f"root_keyframes[{index}] has unsupported fields: {', '.join(unknown)}")
        result.append({
            "time_s": _time(item.get("time_s"), f"root_keyframes[{index}].time_s", duration_s),
            "position_offset_m": _vec3(item.get("position_offset_m"), f"root_keyframes[{index}].position_offset_m"),
            "rotation_offset_deg": _rotation(item.get("rotation_offset_deg"), f"root_keyframes[{index}].rotation_offset_deg"),
        })
    _strictly_increasing(result, "root_keyframes")
    if result[0] != {"time_s": 0.0, "position_offset_m": [0.0, 0.0, 0.0], "rotation_offset_deg": [0.0, 0.0, 0.0]}:
        raise ArticulatedBodyContractError("root_keyframes must begin at time 0 with zero offsets")
    return result


def _compile_joint_keyframes(value: Any, duration_s: float) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ArticulatedBodyContractError("joint_keyframes must be a non-empty list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ArticulatedBodyContractError(f"joint_keyframes[{index}] must be an object")
        unknown = sorted(str(key) for key in item if str(key) not in {"time_s", "rotations_deg"})
        if unknown:
            raise ArticulatedBodyContractError(f"joint_keyframes[{index}] has unsupported fields: {', '.join(unknown)}")
        rotations = item.get("rotations_deg")
        if not isinstance(rotations, Mapping):
            raise ArticulatedBodyContractError(f"joint_keyframes[{index}].rotations_deg must be an object")
        unknown_bones = sorted(str(key) for key in rotations if str(key) not in ARTICULATED_BODY_BONES)
        if unknown_bones:
            raise ArticulatedBodyContractError(f"unknown articulated bones: {', '.join(unknown_bones)}")
        result.append({
            "time_s": _time(item.get("time_s"), f"joint_keyframes[{index}].time_s", duration_s),
            "rotations_deg": {
                str(bone): _rotation(rotation, f"joint_keyframes[{index}].rotations_deg.{bone}")
                for bone, rotation in sorted(rotations.items())
            },
        })
    _strictly_increasing(result, "joint_keyframes")
    if result[0]["time_s"] != 0.0:
        raise ArticulatedBodyContractError("joint_keyframes must begin at time 0")
    return result


def _compile_attachments(
    value: Any,
    duration_s: float,
    *,
    known_object_ids: set[str],
    object_id: str,
    ragdoll_start_time_s: float | None,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ArticulatedBodyContractError("attachments must be a list")
    result: list[dict[str, Any]] = []
    attached_ids: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ArticulatedBodyContractError(f"attachments[{index}] must be an object")
        unknown = sorted(
            str(key)
            for key in item
            if str(key) not in {"object_id", "bone", "start_time_s", "end_time_s", "local_position_m", "local_rotation_deg"}
        )
        if unknown:
            raise ArticulatedBodyContractError(f"attachments[{index}] has unsupported fields: {', '.join(unknown)}")
        target_id = str(item.get("object_id") or "")
        if target_id not in known_object_ids or target_id == object_id:
            raise ArticulatedBodyContractError(f"attachments[{index}].object_id must reference another declared object")
        if target_id in attached_ids:
            raise ArticulatedBodyContractError(f"object {target_id} may be attached only once")
        bone = str(item.get("bone") or "")
        if bone not in ARTICULATED_BODY_BONES:
            raise ArticulatedBodyContractError(f"attachments[{index}].bone is not in the fixed skeleton")
        start = _time(item.get("start_time_s"), f"attachments[{index}].start_time_s", duration_s)
        raw_end = item.get("end_time_s")
        end = _time(raw_end, f"attachments[{index}].end_time_s", duration_s) if raw_end is not None else None
        if end is not None and end <= start:
            raise ArticulatedBodyContractError(f"attachments[{index}].end_time_s must be after start_time_s")
        if ragdoll_start_time_s is not None and (end is None or end > ragdoll_start_time_s):
            raise ArticulatedBodyContractError("attachments must end no later than ragdoll_start_time_s")
        result.append({
            "object_id": target_id,
            "bone": bone,
            "start_time_s": start,
            "end_time_s": end,
            "local_position_m": _vec3(item.get("local_position_m"), f"attachments[{index}].local_position_m"),
            "local_rotation_deg": _rotation(item.get("local_rotation_deg"), f"attachments[{index}].local_rotation_deg"),
        })
        attached_ids.add(target_id)
    return result


def _sample_vec_keyframes(keyframes: list[Any], time_s: float, field: str) -> list[float]:
    left, right, alpha = _bracket(keyframes, time_s)
    return _lerp_vec(left.get(field) or [0.0, 0.0, 0.0], right.get(field) or [0.0, 0.0, 0.0], alpha)


def _sample_joint_keyframes(keyframes: list[Any], time_s: float) -> dict[str, list[float]]:
    left, right, alpha = _bracket(keyframes, time_s)
    left_values = left.get("rotations_deg") or {}
    right_values = right.get("rotations_deg") or {}
    return {
        bone: _lerp_vec(left_values.get(bone) or [0.0, 0.0, 0.0], right_values.get(bone) or [0.0, 0.0, 0.0], alpha)
        for bone in sorted(set(left_values) | set(right_values))
    }


def _bracket(keyframes: list[Any], time_s: float) -> tuple[Mapping[str, Any], Mapping[str, Any], float]:
    if not keyframes:
        empty = {"time_s": 0.0}
        return empty, empty, 0.0
    time_value = float(time_s)
    if time_value <= float(keyframes[0]["time_s"]):
        return keyframes[0], keyframes[0], 0.0
    for index in range(1, len(keyframes)):
        right = keyframes[index]
        if time_value <= float(right["time_s"]):
            left = keyframes[index - 1]
            span = float(right["time_s"]) - float(left["time_s"])
            return left, right, 0.0 if span <= 0.0 else (time_value - float(left["time_s"])) / span
    return keyframes[-1], keyframes[-1], 0.0


def _lerp_vec(left: Any, right: Any, alpha: float) -> list[float]:
    return [round(float(left[index]) + (float(right[index]) - float(left[index])) * alpha, 6) for index in range(3)]


def _strictly_increasing(items: list[dict[str, Any]], field: str) -> None:
    times = [float(item["time_s"]) for item in items]
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ArticulatedBodyContractError(f"{field} times must be strictly increasing")


def _time(value: Any, field: str, duration_s: float) -> float:
    result = _finite_number(value, field)
    if result < 0.0 or result > duration_s:
        raise ArticulatedBodyContractError(f"{field} must be within scene duration")
    return result


def _vec3(value: Any, field: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ArticulatedBodyContractError(f"{field} must contain three numbers")
    return [round(_finite_number(item, field), 6) for item in value]


def _rotation(value: Any, field: str) -> list[float]:
    result = _vec3(value, field)
    if any(abs(component) > 180.0 for component in result):
        raise ArticulatedBodyContractError(f"{field} components must be within [-180, 180] degrees")
    return result


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ArticulatedBodyContractError(f"{field} must be a finite number")
    return float(value)
