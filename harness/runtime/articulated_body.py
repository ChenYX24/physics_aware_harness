from __future__ import annotations

import math
from typing import Any, Mapping


ARTICULATED_BODY_MODEL_ID = "harness_ue_mannequin_v1"
ARTICULATED_BODY_ASSET_PATH = "/Game/Characters/Mannequins/Meshes/SKM_Manny_Simple.SKM_Manny_Simple"
ARTICULATED_BODY_PHYSICS_ASSET_PATH = "/Game/Characters/Mannequins/Rigs/PA_Mannequin.PA_Mannequin"
ARTICULATED_BODY_CONTROL_RIG_PATH = "/Game/Characters/Mannequins/Rigs/CR_Mannequin_Body.CR_Mannequin_Body"
ARTICULATED_BODY_AUTHORED_SIZE_M = (0.58, 0.36, 1.92)
ARTICULATED_BODY_MODES = frozenset({"kinematic", "ragdoll"})
ARTICULATED_POSE_SOURCE_TYPES = frozenset({"pose_keyframes", "animation_sequence"})
ARTICULATED_ROOT_TRANSFORM_SOURCE_TYPES = frozenset({"root_keyframes", "animation_root_motion", "character_movement"})
ARTICULATED_IK_GOALS = frozenset({"hand_l", "hand_r", "foot_l", "foot_r"})
ARTICULATED_POSE_OVERLAY_TYPE = "bone_local_rotation_offsets"
ARTICULATED_ANIMATION_ASSETS = {
    "harness_ue4_mannequin_idle_v1": {
        "asset_path": "/Game/Characters/Mannequins/Anims/Unarmed/MM_Idle.MM_Idle",
        "in_place": True,
        "has_root_motion": False,
    },
    "harness_ue4_mannequin_walk_v1": {
        "asset_path": "/Game/Characters/Mannequins/Anims/Unarmed/Walk/MF_Unarmed_Walk_Fwd.MF_Unarmed_Walk_Fwd",
        "in_place": True,
        "has_root_motion": False,
    },
    "harness_ue4_mannequin_run_v1": {
        "asset_path": "/Game/Characters/Mannequins/Anims/Unarmed/Jog/MF_Unarmed_Jog_Fwd.MF_Unarmed_Jog_Fwd",
        "in_place": True,
        "has_root_motion": False,
    },
    "harness_ue4_mannequin_jump_v1": {
        "asset_path": "/Game/Characters/Mannequins/Anims/Unarmed/Jump/MM_Jump.MM_Jump",
        "in_place": True,
        "has_root_motion": False,
    },
}
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
ARTICULATED_POSE_OVERLAY_CONTROLS = {
    "pelvis": "hips_ctrl",
    "spine_01": "spine_01_ctrl",
    "spine_02": "spine_02_ctrl",
    "spine_03": "spine_03_ctrl",
    "neck_01": "neck_01_ctrl",
    "head": "head_ctrl",
    "clavicle_l": "clavicle_l_ctrl",
    "upperarm_l": "upperarm_l_fk_ctrl",
    "lowerarm_l": "lowerarm_l_fk_ctrl",
    "hand_l": "hand_l_fk_ctrl",
    "clavicle_r": "clavicle_r_ctrl",
    "upperarm_r": "upperarm_r_fk_ctrl",
    "lowerarm_r": "lowerarm_r_fk_ctrl",
    "hand_r": "hand_r_fk_ctrl",
    "thigh_l": "thigh_l_fk_ctrl",
    "calf_l": "calf_l_fk_ctrl",
    "foot_l": "foot_l_fk_ctrl",
    "thigh_r": "thigh_r_fk_ctrl",
    "calf_r": "calf_r_fk_ctrl",
    "foot_r": "foot_r_fk_ctrl",
}
ARTICULATED_POSE_OVERLAY_BONES = frozenset(ARTICULATED_POSE_OVERLAY_CONTROLS)
ARTICULATED_IK_CONTROLS = {
    "hand_l": {"target": "hand_l_ik_ctrl", "pole": "arm_l_pv_ik_ctrl", "switch": "arm_l_fk_ik_switch"},
    "hand_r": {"target": "hand_r_ik_ctrl", "pole": "arm_r_pv_ik_ctrl", "switch": "arm_r_fk_ik_switch"},
    "foot_l": {"target": "foot_l_ik_ctrl", "pole": "leg_l_pv_ik_ctrl", "switch": "leg_l_fk_ik_switch"},
    "foot_r": {"target": "foot_r_ik_ctrl", "pole": "leg_r_pv_ik_ctrl", "switch": "leg_r_fk_ik_switch"},
}
ARTICULATED_HEAD_LOOK_CONTROLS = {
    "target": "head_ik_ctrl",
    "switch": "neck_fk_ik_switch",
}


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
            "pose_source",
            "pose_overlay",
            "root_transform_source",
            "ik_targets",
            "head_look_target",
            "support_object_id",
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

    pose_source = _compile_pose_source(solver.get("pose_source"), duration)
    pose_overlay = _compile_pose_overlay(solver.get("pose_overlay"), duration)
    root_transform_source = _compile_root_transform_source(solver.get("root_transform_source"), duration)
    _validate_motion_source_pair(pose_source, root_transform_source)
    ik_targets = _compile_ik_targets(solver.get("ik_targets"), duration)
    head_look_target = _compile_head_look_target(solver.get("head_look_target"), duration)
    if (pose_overlay or ik_targets or head_look_target) and pose_source.get("type") != "animation_sequence":
        raise ArticulatedBodyContractError("pose overlay, IK and head look require pose_source.type=animation_sequence")
    support_object_id = solver.get("support_object_id")
    if support_object_id is not None:
        support_object_id = str(support_object_id)
        if support_object_id not in known_object_ids or support_object_id == object_id:
            raise ArticulatedBodyContractError("support_object_id must reference another declared object")
    ragdoll_start = solver.get("ragdoll_start_time_s")
    if mode == "kinematic" and ragdoll_start is not None:
        raise ArticulatedBodyContractError("ragdoll_start_time_s is only valid in ragdoll mode")
    if mode == "ragdoll":
        ragdoll_start = _time(ragdoll_start, "ragdoll_start_time_s", duration)
    attachments = _compile_attachments(
        solver.get("attachments"),
        duration,
        known_object_ids=known_object_ids,
        object_id=object_id,
        ragdoll_start_time_s=ragdoll_start if mode == "ragdoll" else None,
    )
    return {
        "schema_version": "harness_articulated_body_contract_v3",
        "type": "articulated_body",
        "model": ARTICULATED_BODY_MODEL_ID,
        "asset_path": ARTICULATED_BODY_ASSET_PATH,
        "physics_asset_path": ARTICULATED_BODY_PHYSICS_ASSET_PATH,
        "control_rig_path": ARTICULATED_BODY_CONTROL_RIG_PATH,
        "authored_size_m": list(ARTICULATED_BODY_AUTHORED_SIZE_M),
        "mode": mode,
        "rotation_space": "component_offset",
        "pose_source": pose_source,
        "pose_overlay": pose_overlay,
        "root_transform_source": root_transform_source,
        "ik_targets": ik_targets,
        "head_look_target": head_look_target,
        "support_object_id": support_object_id,
        "attachments": attachments,
        **({"ragdoll_start_time_s": ragdoll_start} if mode == "ragdoll" else {}),
    }


def sample_articulated_body_contract(contract: Mapping[str, Any], time_s: float) -> dict[str, Any]:
    pose_source = contract.get("pose_source") if isinstance(contract.get("pose_source"), Mapping) else {}
    root_source = contract.get("root_transform_source") if isinstance(contract.get("root_transform_source"), Mapping) else {}
    root_keyframes = root_source.get("keyframes") if root_source.get("type") in {"root_keyframes", "character_movement"} else []
    animation_segment = _active_animation_segment(pose_source.get("segments") or [], time_s)
    pose_overlay = contract.get("pose_overlay") if isinstance(contract.get("pose_overlay"), Mapping) else {}
    overlay_keyframes = pose_overlay.get("keyframes") or []
    head_look_target = contract.get("head_look_target") if isinstance(contract.get("head_look_target"), Mapping) else None
    return {
        "root_position_offset_m": _sample_vec_keyframes(
            root_keyframes or [], time_s, "position_offset_m"
        ),
        "root_rotation_offset_deg": _sample_vec_keyframes(
            root_keyframes or [], time_s, "rotation_offset_deg"
        ),
        "joint_rotations_deg": _sample_joint_keyframes(pose_source.get("keyframes") or [], time_s),
        "pose_overlay": (
            {
                "rotation_space": "bone_local_offset",
                "blend_mode": "weighted_additive",
                "rotations_deg": _sample_joint_keyframes(overlay_keyframes, time_s),
                "weight": round(_sample_scalar_keyframes(overlay_keyframes, time_s, "weight"), 6),
            }
            if overlay_keyframes
            else None
        ),
        "animation_segment": dict(animation_segment) if animation_segment else None,
        "ik_targets": {
            str(target["goal"]): _sample_ik_target(target, time_s)
            for target in contract.get("ik_targets") or []
            if isinstance(target, Mapping)
        },
        "head_look_target": _sample_head_look_target(head_look_target, time_s) if head_look_target else None,
    }


def _compile_pose_source(value: Any, duration_s: float) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ArticulatedBodyContractError("pose_source must be an object")
    source_type = str(value.get("type") or "")
    if source_type not in ARTICULATED_POSE_SOURCE_TYPES:
        raise ArticulatedBodyContractError(f"pose_source.type must be one of {sorted(ARTICULATED_POSE_SOURCE_TYPES)}")
    if source_type == "pose_keyframes":
        _exact_fields(value, {"type", "keyframes"}, "pose_source")
        return {"type": source_type, "keyframes": _compile_joint_keyframes(value.get("keyframes"), duration_s)}
    _exact_fields(value, {"type", "segments"}, "pose_source")
    return {"type": source_type, "segments": _compile_animation_segments(value.get("segments"), duration_s)}


def _compile_pose_overlay(value: Any, duration_s: float) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ArticulatedBodyContractError("pose_overlay must be an object")
    _exact_fields(value, {"type", "keyframes"}, "pose_overlay")
    if value.get("type") != ARTICULATED_POSE_OVERLAY_TYPE:
        raise ArticulatedBodyContractError(f"pose_overlay.type must be {ARTICULATED_POSE_OVERLAY_TYPE}")
    keyframes = value.get("keyframes")
    if not isinstance(keyframes, list) or not keyframes:
        raise ArticulatedBodyContractError("pose_overlay.keyframes must be a non-empty list")
    compiled = []
    for index, keyframe in enumerate(keyframes):
        if not isinstance(keyframe, Mapping):
            raise ArticulatedBodyContractError(f"pose_overlay.keyframes[{index}] must be an object")
        _exact_fields(keyframe, {"time_s", "rotations_deg", "weight"}, f"pose_overlay.keyframes[{index}]")
        rotations = keyframe.get("rotations_deg")
        if not isinstance(rotations, Mapping) or not rotations:
            raise ArticulatedBodyContractError(f"pose_overlay.keyframes[{index}].rotations_deg must be a non-empty object")
        unsupported = sorted(str(bone) for bone in rotations if str(bone) not in ARTICULATED_POSE_OVERLAY_BONES)
        if unsupported:
            raise ArticulatedBodyContractError(f"unsupported pose overlay bones: {', '.join(unsupported)}")
        weight = _unit_interval(keyframe.get("weight"), f"pose_overlay.keyframes[{index}].weight")
        compiled.append({
            "time_s": _time(keyframe.get("time_s"), f"pose_overlay.keyframes[{index}].time_s", duration_s),
            "rotations_deg": {
                str(bone): _rotation(rotation, f"pose_overlay.keyframes[{index}].rotations_deg.{bone}")
                for bone, rotation in sorted(rotations.items())
            },
            "weight": round(weight, 6),
        })
    _strictly_increasing(compiled, "pose_overlay.keyframes")
    if compiled[0]["time_s"] != 0.0:
        raise ArticulatedBodyContractError("pose_overlay.keyframes must begin at time 0")
    return {
        "type": ARTICULATED_POSE_OVERLAY_TYPE,
        "rotation_space": "bone_local_offset",
        "blend_mode": "weighted_additive",
        "keyframes": compiled,
    }


def _compile_root_transform_source(value: Any, duration_s: float) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ArticulatedBodyContractError("root_transform_source must be an object")
    source_type = str(value.get("type") or "")
    if source_type not in ARTICULATED_ROOT_TRANSFORM_SOURCE_TYPES:
        raise ArticulatedBodyContractError(
            f"root_transform_source.type must be one of {sorted(ARTICULATED_ROOT_TRANSFORM_SOURCE_TYPES)}"
        )
    if source_type == "root_keyframes":
        _exact_fields(value, {"type", "keyframes"}, "root_transform_source")
        return {"type": source_type, "keyframes": _compile_root_keyframes(value.get("keyframes"), duration_s)}
    if source_type == "animation_root_motion":
        _exact_fields(value, {"type"}, "root_transform_source")
        return {"type": source_type}
    _exact_fields(
        value,
        {"type", "keyframes", "max_speed_m_s", "max_acceleration_m_s2"},
        "root_transform_source",
    )
    max_speed = _positive_number(value.get("max_speed_m_s"), "root_transform_source.max_speed_m_s")
    max_acceleration = _positive_number(
        value.get("max_acceleration_m_s2"), "root_transform_source.max_acceleration_m_s2"
    )
    return {
        "type": source_type,
        "keyframes": _compile_root_keyframes(value.get("keyframes"), duration_s),
        "max_speed_m_s": round(max_speed, 6),
        "max_acceleration_m_s2": round(max_acceleration, 6),
    }


def _compile_animation_segments(value: Any, duration_s: float) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ArticulatedBodyContractError("pose_source.segments must be a non-empty list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ArticulatedBodyContractError(f"pose_source.segments[{index}] must be an object")
        _exact_fields(
            item,
            {"animation_asset_id", "start_time_s", "end_time_s", "play_rate", "loop"},
            f"pose_source.segments[{index}]",
        )
        asset_id = str(item.get("animation_asset_id") or "")
        metadata = ARTICULATED_ANIMATION_ASSETS.get(asset_id)
        if metadata is None:
            raise ArticulatedBodyContractError(f"unknown articulated animation asset: {asset_id}")
        start = _time(item.get("start_time_s"), f"pose_source.segments[{index}].start_time_s", duration_s)
        end = _time(item.get("end_time_s"), f"pose_source.segments[{index}].end_time_s", duration_s)
        if end <= start:
            raise ArticulatedBodyContractError(f"pose_source.segments[{index}].end_time_s must be after start_time_s")
        loop = item.get("loop")
        if not isinstance(loop, bool):
            raise ArticulatedBodyContractError(f"pose_source.segments[{index}].loop must be boolean")
        result.append({
            "animation_asset_id": asset_id,
            "animation_asset_path": metadata["asset_path"],
            "in_place": metadata["in_place"],
            "has_root_motion": metadata["has_root_motion"],
            "start_time_s": start,
            "end_time_s": end,
            "play_rate": round(_positive_number(item.get("play_rate"), f"pose_source.segments[{index}].play_rate"), 6),
            "loop": loop,
        })
    ordered = sorted(result, key=lambda item: float(item["start_time_s"]))
    if ordered != result:
        raise ArticulatedBodyContractError("pose_source.segments must be ordered by start_time_s")
    if any(float(right["start_time_s"]) < float(left["end_time_s"]) for left, right in zip(result, result[1:])):
        raise ArticulatedBodyContractError("pose_source.segments must not overlap")
    return result


def _validate_motion_source_pair(pose_source: Mapping[str, Any], root_source: Mapping[str, Any]) -> None:
    pose_type = str(pose_source.get("type") or "")
    root_type = str(root_source.get("type") or "")
    if pose_type == "pose_keyframes" and root_type != "root_keyframes":
        raise ArticulatedBodyContractError("pose_keyframes requires root_transform_source.type=root_keyframes")
    if root_type == "animation_root_motion":
        if pose_type != "animation_sequence":
            raise ArticulatedBodyContractError("animation_root_motion requires pose_source.type=animation_sequence")
        if any(segment.get("has_root_motion") is not True for segment in pose_source.get("segments") or []):
            raise ArticulatedBodyContractError("animation_root_motion requires qualified root-motion animation assets")
    if root_type == "character_movement":
        if pose_type != "animation_sequence":
            raise ArticulatedBodyContractError("character_movement requires pose_source.type=animation_sequence")
        if any(segment.get("in_place") is not True for segment in pose_source.get("segments") or []):
            raise ArticulatedBodyContractError("character_movement requires in-place animation assets")


def _compile_ik_targets(value: Any, duration_s: float) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ArticulatedBodyContractError("ik_targets must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ArticulatedBodyContractError(f"ik_targets[{index}] must be an object")
        _exact_fields(item, {"goal", "tolerance_m", "keyframes"}, f"ik_targets[{index}]")
        goal = str(item.get("goal") or "")
        if goal not in ARTICULATED_IK_GOALS:
            raise ArticulatedBodyContractError(f"ik_targets[{index}].goal must be one of {sorted(ARTICULATED_IK_GOALS)}")
        if goal in seen:
            raise ArticulatedBodyContractError(f"duplicate IK goal: {goal}")
        keyframes = item.get("keyframes")
        if not isinstance(keyframes, list) or not keyframes:
            raise ArticulatedBodyContractError(f"ik_targets[{index}].keyframes must be a non-empty list")
        compiled_keyframes = []
        pole_declared: bool | None = None
        for keyframe_index, keyframe in enumerate(keyframes):
            if not isinstance(keyframe, Mapping):
                raise ArticulatedBodyContractError(f"ik_targets[{index}].keyframes[{keyframe_index}] must be an object")
            expected = {"time_s", "position_m", "rotation_deg", "weight"}
            actual = frozenset(str(field) for field in keyframe)
            if actual not in {frozenset(expected), frozenset(expected | {"pole_position_m"})}:
                raise ArticulatedBodyContractError(
                    f"ik_targets[{index}].keyframes[{keyframe_index}] must contain exactly target fields and optional pole_position_m"
                )
            has_pole = "pole_position_m" in keyframe
            if pole_declared is None:
                pole_declared = has_pole
            elif pole_declared != has_pole:
                raise ArticulatedBodyContractError("pole_position_m must be present on every keyframe in an IK chain or omitted")
            weight = _unit_interval(keyframe.get("weight"), f"ik_targets[{index}].keyframes[{keyframe_index}].weight")
            compiled_keyframe = {
                "time_s": _time(keyframe.get("time_s"), f"ik_targets[{index}].keyframes[{keyframe_index}].time_s", duration_s),
                "position_m": _vec3(keyframe.get("position_m"), f"ik_targets[{index}].keyframes[{keyframe_index}].position_m"),
                "rotation_deg": _rotation(keyframe.get("rotation_deg"), f"ik_targets[{index}].keyframes[{keyframe_index}].rotation_deg"),
                "weight": round(weight, 6),
            }
            if has_pole:
                compiled_keyframe["pole_position_m"] = _vec3(
                    keyframe.get("pole_position_m"),
                    f"ik_targets[{index}].keyframes[{keyframe_index}].pole_position_m",
                )
            compiled_keyframes.append(compiled_keyframe)
        _strictly_increasing(compiled_keyframes, f"ik_targets[{index}].keyframes")
        result.append({
            "goal": goal,
            "bone": goal,
            "pole_bone": ({"hand_l": "lowerarm_l", "hand_r": "lowerarm_r", "foot_l": "calf_l", "foot_r": "calf_r"})[goal],
            "tolerance_m": round(_positive_number(item.get("tolerance_m"), f"ik_targets[{index}].tolerance_m"), 6),
            "keyframes": compiled_keyframes,
        })
        seen.add(goal)
    return result


def _compile_head_look_target(value: Any, duration_s: float) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ArticulatedBodyContractError("head_look_target must be an object")
    _exact_fields(value, {"tolerance_deg", "keyframes"}, "head_look_target")
    tolerance = _positive_number(value.get("tolerance_deg"), "head_look_target.tolerance_deg")
    if tolerance > 180.0:
        raise ArticulatedBodyContractError("head_look_target.tolerance_deg must not exceed 180")
    keyframes = value.get("keyframes")
    if not isinstance(keyframes, list) or not keyframes:
        raise ArticulatedBodyContractError("head_look_target.keyframes must be a non-empty list")
    compiled = []
    for index, keyframe in enumerate(keyframes):
        if not isinstance(keyframe, Mapping):
            raise ArticulatedBodyContractError(f"head_look_target.keyframes[{index}] must be an object")
        _exact_fields(keyframe, {"time_s", "position_m", "weight"}, f"head_look_target.keyframes[{index}]")
        compiled.append({
            "time_s": _time(keyframe.get("time_s"), f"head_look_target.keyframes[{index}].time_s", duration_s),
            "position_m": _vec3(keyframe.get("position_m"), f"head_look_target.keyframes[{index}].position_m"),
            "weight": round(_unit_interval(keyframe.get("weight"), f"head_look_target.keyframes[{index}].weight"), 6),
        })
    _strictly_increasing(compiled, "head_look_target.keyframes")
    return {"bone": "head", "tolerance_deg": round(tolerance, 6), "keyframes": compiled}


def _active_animation_segment(segments: list[Any], time_s: float) -> Mapping[str, Any] | None:
    return next(
        (
            segment
            for segment in segments
            if isinstance(segment, Mapping)
            and float(segment.get("start_time_s") or 0.0) <= float(time_s) <= float(segment.get("end_time_s") or 0.0)
        ),
        None,
    )


def _sample_ik_target(target: Mapping[str, Any], time_s: float) -> dict[str, Any]:
    keyframes = target.get("keyframes") or []
    sampled = {
        "position_m": _sample_vec_keyframes(keyframes, time_s, "position_m"),
        "rotation_deg": _sample_vec_keyframes(keyframes, time_s, "rotation_deg"),
        "weight": round(_sample_scalar_keyframes(keyframes, time_s, "weight"), 6),
        "tolerance_m": target.get("tolerance_m"),
    }
    if keyframes and "pole_position_m" in keyframes[0]:
        sampled["pole_position_m"] = _sample_vec_keyframes(keyframes, time_s, "pole_position_m")
    return sampled


def _sample_head_look_target(target: Mapping[str, Any], time_s: float) -> dict[str, Any]:
    keyframes = target.get("keyframes") or []
    return {
        "position_m": _sample_vec_keyframes(keyframes, time_s, "position_m"),
        "weight": round(_sample_scalar_keyframes(keyframes, time_s, "weight"), 6),
        "tolerance_deg": target.get("tolerance_deg"),
    }


def _sample_scalar_keyframes(keyframes: list[Any], time_s: float, field: str) -> float:
    left, right, alpha = _bracket(keyframes, time_s)
    return float(left.get(field) or 0.0) + (float(right.get(field) or 0.0) - float(left.get(field) or 0.0)) * alpha


def _exact_fields(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = {str(key) for key in value}
    if actual != expected:
        raise ArticulatedBodyContractError(f"{field} must contain exactly {sorted(expected)}")


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


def _positive_number(value: Any, field: str) -> float:
    result = _finite_number(value, field)
    if result <= 0.0:
        raise ArticulatedBodyContractError(f"{field} must be positive")
    return result


def _unit_interval(value: Any, field: str) -> float:
    result = _finite_number(value, field)
    if result < 0.0 or result > 1.0:
        raise ArticulatedBodyContractError(f"{field} must be within [0, 1]")
    return result
