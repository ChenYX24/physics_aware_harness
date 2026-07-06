from __future__ import annotations

from pathlib import Path
from typing import Any
import math

from harness.core.case_spec import CaseSpec
from harness.runtime.artifact_collector import write_runtime_artifacts


class FallbackBackend:
    name = "fallback"

    def run_case(
        self,
        case: CaseSpec,
        output_root: str | Path,
        *,
        requested_views: list[str] | None = None,
        render_passes: list[str] | None = None,
        camera_strategy: str = "bounds_auto_v1",
    ) -> Path:
        run_id = f"{case.case_id}_fallback"
        run_dir = Path(output_root) / run_id
        trajectory = trajectory_for_case(case.data)
        return write_runtime_artifacts(
            run_dir,
            case_spec=case.data,
            trajectory=trajectory,
            backend=self.name,
            requested_views=requested_views,
            render_passes=render_passes,
            camera_strategy=camera_strategy,
        )


def trajectory_for_case(case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    capability_id = str(case_spec["capability_id"])
    case_id = str(case_spec["case_id"])
    if is_contact_causality_capability(capability_id):
        return billiards_trajectory(case_id, case_spec)
    if capability_id == "sequential_contact_propagation":
        return domino_trajectory(case_id, case_spec)
    if capability_id == "rigid_body_gravity_collision":
        return falling_trajectory(case_id, case_spec)
    if capability_id == "ramp_sliding_friction":
        return ramp_trajectory(case_id, case_spec)
    if capability_id == "projectile_gravity_motion":
        return projectile_trajectory(case_id, case_spec)
    if capability_id == "bounce_restitution_ball":
        return bounce_trajectory(case_id, case_spec)
    if capability_id == "rolling_friction_ball":
        return rolling_trajectory(case_id, case_spec)
    if capability_id == "sliding_crate_friction":
        return sliding_trajectory(case_id, case_spec)
    if capability_id == "force_field_wind_drift":
        return wind_trajectory(case_id, case_spec)
    if capability_id == "mass_ratio_momentum_transfer":
        return mass_ratio_trajectory(case_id, case_spec)
    if capability_id == "angular_damping_spin_decay":
        return spin_trajectory(case_id, case_spec)
    return []


def is_contact_causality_capability(capability_id: str) -> bool:
    return capability_id in {"rigid_body_contact_causality", "billiard_causality_compiler"}


def billiards_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "negative" in case_id:
        negative_mode = "precontact_motion"
    objects = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    active_ids = [str(item) for item in case_spec.get("active_objects", [])]
    passive_ids = [str(item) for item in case_spec.get("passive_objects", [])]
    cue_id = active_ids[0] if active_ids else "cue_ball"
    cue_obj = objects.get(cue_id, {})
    cue_velocity = vec3(cue_obj.get("initial_velocity_m_s") or [1.0, 0.0, 0.0])
    speed = max(abs(cue_velocity[0]), 0.6)
    initial_states = {oid: state_from_spec(obj) for oid, obj in objects.items() if str(obj.get("role")) != "support"}
    if not initial_states:
        initial_states = {"cue_ball": state([-1.0, 0, 0.09], [1.0, 0, 0]), "target_ball_1": state([0, 0, 0.09], [0, 0, 0])}
    if negative_mode == "precontact_motion" and passive_ids:
        initial_states[passive_ids[0]]["velocity_m_s"] = [0.25, 0.0, 0.0]

    frames = [frame(0, 0.0, clone_states(initial_states))]
    pre = clone_states(initial_states)
    cue_pos = vec3(pre.get(cue_id, {}).get("position_m"))
    first_target_pos = vec3(pre.get(passive_ids[0], {}).get("position_m")) if passive_ids else [0.0, 0.0, 0.09]
    pre[cue_id] = state(midpoint(cue_pos, first_target_pos, 0.65), [round(speed * 0.8, 4), 0.0, 0.0])
    if negative_mode == "precontact_motion" and passive_ids:
        moving = pre[passive_ids[0]]
        moving["position_m"] = [round(vec3(moving.get("position_m"))[0] + 0.04, 4), vec3(moving.get("position_m"))[1], vec3(moving.get("position_m"))[2]]
    frames.append(frame(1, 0.2, pre))

    previous = cue_id
    current_states = clone_states(pre)
    for idx, passive_id in enumerate(passive_ids, start=1):
        frame_id = idx + 1
        time_s = round(frame_id * 0.2, 4)
        current_states = clone_states(current_states)
        target_pos = vec3(current_states.get(passive_id, {}).get("position_m"))
        previous_pos = vec3(current_states.get(previous, {}).get("position_m"))
        current_states[previous] = state(midpoint(previous_pos, target_pos, 0.8), [round(speed * max(0.05, 0.35 - idx * 0.04), 4), 0.0, 0.0])
        current_states[passive_id] = state([round(target_pos[0] + 0.08, 4), target_pos[1], target_pos[2]], [round(speed * max(0.08, 0.5 - idx * 0.05), 4), 0.0, 0.0])
        contacts = [] if negative_mode == "missing_contact" and idx == 1 else [contact(previous, passive_id, frame_id, time_s)]
        frames.append(frame(frame_id, time_s, current_states, contacts=contacts))
        previous = passive_id
    return frames


def domino_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "negative" in case_id:
        negative_mode = "simultaneous_motion"
    domino_ids = [str(obj.get("id")) for obj in case_spec.get("objects", []) if str(obj.get("role") or "") == "domino"]
    if not domino_ids:
        domino_ids = [f"domino_{idx}" for idx in range(5)]
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    objects = {oid: state(vec3((object_specs.get(oid) or {}).get("initial_position_m") or [idx * 0.18, 0, 0.2]), [0, 0, 0], rotation=[0, 0, 0]) for idx, oid in enumerate(domino_ids)}
    frames = [frame(0, 0.0, objects)]
    frame1 = {key: dict(value) for key, value in objects.items()}
    frame1[domino_ids[0]] = state(vec3(frame1[domino_ids[0]].get("position_m")), [0, 0, 0], rotation=[0, 20, 0])
    if negative_mode == "simultaneous_motion" and len(domino_ids) >= 4:
        for oid in domino_ids[2:4]:
            frame1[oid] = state(vec3(frame1[oid].get("position_m")), [0, 0, 0], rotation=[0, 25, 0])
    frames.append(frame(1, 0.2, frame1))
    for idx in range(1, len(domino_ids)):
        states = {key: dict(value) for key, value in frames[-1]["objects"].items()}
        oid = domino_ids[idx]
        states[oid] = state(vec3(states[oid].get("position_m")), [0, 0, 0], rotation=[0, 25 + idx * 8, 0])
        contacts = [] if negative_mode == "missing_contact" and idx == 1 else [contact(domino_ids[idx - 1], oid, idx + 1, 0.2 * (idx + 1))]
        frames.append(frame(idx + 1, 0.2 * (idx + 1), states, contacts=contacts))
    return frames


def falling_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and ("negative" in case_id or "floating" in case_id):
        negative_mode = "floating_block"
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    falling_ids = [oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"falling_body", "stack_block"}]
    if not falling_ids:
        falling_ids = ["falling_block"]
        object_specs["falling_block"] = {"initial_position_m": [0, 0, 1.2]}
    floor_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"support", "floor", "ground"}), "floor")
    floor_state = state(vec3((object_specs.get(floor_id) or {}).get("initial_position_m") or [0, 0, 0]), [0, 0, 0])
    initial = {floor_id: floor_state}
    for oid in falling_ids:
        initial[oid] = state(vec3((object_specs.get(oid) or {}).get("initial_position_m") or [0, 0, 1.2]), [0, 0, 0])
    if negative_mode == "floating_block" or "floating" in case_id:
        return [frame(0, 0.0, clone_states(initial)), frame(1, 0.2, clone_states(initial))]
    mid = {floor_id: floor_state}
    final = {floor_id: floor_state}
    contacts = []
    for idx, oid in enumerate(falling_ids):
        p0 = vec3(initial[oid].get("position_m"))
        mid[oid] = state([p0[0], p0[1], round(max(0.25, p0[2] * 0.5), 4)], [0, 0, -2.0])
        final[oid] = state([p0[0], p0[1], 0.1 + idx * 0.08], [0, 0, 0])
        if negative_mode != "missing_contact":
            contacts.append(contact(oid, floor_id, 2, 0.4))
    return [frame(0, 0.0, initial), frame(1, 0.2, mid), frame(2, 0.4, final, contacts=contacts)]


def ramp_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "uphill" in case_id:
        negative_mode = "uphill_without_force"
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    subject_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"rolling_subject", "sliding_subject", "ramp_subject"}), "ramp_subject")
    ramp_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"ramp", "slope_surface"}), "ramp")
    subject_spec = object_specs.get(subject_id) or {"initial_position_m": [0.0, 0.0, 0.8]}
    ramp_state = state(vec3((object_specs.get(ramp_id) or {}).get("initial_position_m") or [0.6, 0.0, 0.35]), [0, 0, 0], rotation=vec3((object_specs.get(ramp_id) or {}).get("initial_rotation_deg") or [0, 0, 0]))
    p0 = vec3(subject_spec.get("initial_position_m") or [0.0, 0.0, 0.8])
    expected = dict(case_spec.get("expected_physics") or {})
    travel = float(expected.get("fallback_downhill_displacement_m") or expected.get("expected_min_downhill_displacement_m") or 0.35)
    if negative_mode == "uphill_without_force":
        travel = -abs(travel)
    elif negative_mode == "no_friction_sensitivity":
        travel = max(0.01, float(expected.get("expected_min_downhill_displacement_m", 0.2)) * 0.25)
    slope_angle_rad = math.radians(float(expected.get("slope_angle_deg") or 18.0))
    z_drop = abs(travel) * math.tan(slope_angle_rad) if travel >= 0 else -abs(travel) * math.tan(slope_angle_rad)
    initial = {
        ramp_id: ramp_state,
        subject_id: state(p0, vec3(subject_spec.get("initial_velocity_m_s") or [0, 0, 0])),
    }
    mid = {
        ramp_id: ramp_state,
        subject_id: state([round(p0[0] + travel * 0.45, 4), p0[1], round(p0[2] - z_drop * 0.45, 4)], [round(travel * 1.2, 4), 0, round(-z_drop * 1.2, 4)]),
    }
    final = {
        ramp_id: ramp_state,
        subject_id: state([round(p0[0] + travel, 4), p0[1], round(p0[2] - z_drop, 4)], [round(travel * 0.5, 4), 0, round(-z_drop * 0.5, 4)]),
    }
    contacts = [] if negative_mode == "missing_contact" else [contact(subject_id, ramp_id, 1, 0.2), contact(subject_id, ramp_id, 2, 0.4)]
    return [frame(0, 0.0, initial), frame(1, 0.2, mid, contacts=contacts[:1]), frame(2, 0.4, final, contacts=contacts[1:])]


def projectile_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "no_gravity" in case_id:
        negative_mode = "no_gravity_float"
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    projectile_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"projectile", "thrown_body", "launched_body"}), "projectile")
    ground_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"support", "ground", "floor"}), "ground")
    projectile_spec = object_specs.get(projectile_id) or {"initial_position_m": [0.0, 0.0, 0.2], "initial_velocity_m_s": [2.0, 0.0, 3.0]}
    ground_state = state(vec3((object_specs.get(ground_id) or {}).get("initial_position_m") or [0.0, 0.0, 0.0]), [0, 0, 0])
    p0 = vec3(projectile_spec.get("initial_position_m") or [0.0, 0.0, 0.2])
    v0 = vec3(projectile_spec.get("initial_velocity_m_s") or [2.0, 0.0, 3.0])
    if negative_mode == "no_gravity_float":
        frames = []
        for frame_id, time_s in enumerate([0.0, 0.2, 0.4]):
            position = [round(p0[0] + v0[0] * time_s, 4), p0[1], round(p0[2] + max(v0[2], 0.8) * time_s, 4)]
            frames.append(frame(frame_id, time_s, {ground_id: ground_state, projectile_id: state(position, v0)}))
        return frames
    gravity = float((case_spec.get("expected_physics") or {}).get("gravity_m_s2") or 9.81)
    sample_times = [0.0, 0.25, 0.5, 0.75]
    frames = []
    for frame_id, time_s in enumerate(sample_times):
        z = p0[2] + v0[2] * time_s - 0.5 * gravity * time_s * time_s
        if frame_id == len(sample_times) - 1:
            z = max(0.1, min(z, 0.12))
        vz = v0[2] - gravity * time_s
        position = [round(p0[0] + v0[0] * time_s, 4), p0[1], round(z, 4)]
        contacts = [] if negative_mode == "missing_landing_contact" or frame_id < len(sample_times) - 1 else [contact(projectile_id, ground_id, frame_id, time_s)]
        frames.append(frame(frame_id, time_s, {ground_id: ground_state, projectile_id: state(position, [round(v0[0], 4), 0.0, round(vz, 4)])}, contacts=contacts))
    return frames


def bounce_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "no_rebound" in case_id:
        negative_mode = "no_rebound"
    if not negative_mode and "energy_gain" in case_id:
        negative_mode = "energy_gain"
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    body_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"bouncing_body", "restitution_subject", "bounce_subject"}), "bounce_ball")
    support_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"support", "ground", "floor"}), "floor")
    body_spec = object_specs.get(body_id) or {"initial_position_m": [0.0, 0.0, 1.2], "radius_m": 0.12}
    support_state = state(vec3((object_specs.get(support_id) or {}).get("initial_position_m") or [0.0, 0.0, 0.0]), [0, 0, 0])
    p0 = vec3(body_spec.get("initial_position_m") or [0.0, 0.0, 1.2])
    radius = float(body_spec.get("radius_m") or 0.1)
    expected = dict(case_spec.get("expected_physics") or {})
    drop_height = float(expected.get("drop_height_m") or max(0.2, p0[2] - radius))
    restitution = float(expected.get("restitution") or body_spec.get("restitution") or 0.5)
    rebound_ratio = restitution * restitution
    if negative_mode == "no_rebound":
        rebound_ratio = 0.01
    elif negative_mode == "energy_gain":
        rebound_ratio = max(1.05, float(expected.get("expected_max_rebound_ratio") or 0.5) + 0.25)
    contact_z = radius
    rebound_z = contact_z + drop_height * rebound_ratio
    initial = {support_id: support_state, body_id: state(p0, [0, 0, 0])}
    falling = {support_id: support_state, body_id: state([p0[0], p0[1], round(contact_z + drop_height * 0.35, 4)], [0, 0, -3.0])}
    contact_state = {support_id: support_state, body_id: state([p0[0], p0[1], round(contact_z, 4)], [0, 0, round(3.0 * restitution, 4)])}
    rebound = {support_id: support_state, body_id: state([p0[0], p0[1], round(rebound_z, 4)], [0, 0, 0])}
    contacts = [] if negative_mode == "missing_contact" else [contact(body_id, support_id, 2, 0.4)]
    return [
        frame(0, 0.0, initial),
        frame(1, 0.2, falling),
        frame(2, 0.4, contact_state, contacts=contacts),
        frame(3, 0.6, rebound),
    ]


def rolling_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "no_deceleration" in case_id:
        negative_mode = "no_deceleration"
    if not negative_mode and "excessive_friction" in case_id:
        negative_mode = "excessive_friction_stop"
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    body_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"rolling_body", "friction_subject", "rolling_subject"}), "rolling_ball")
    support_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"support", "ground", "floor"}), "floor")
    body_spec = object_specs.get(body_id) or {"initial_position_m": [0.0, 0.0, 0.12], "initial_velocity_m_s": [1.2, 0.0, 0.0]}
    support_state = state(vec3((object_specs.get(support_id) or {}).get("initial_position_m") or [0.0, 0.0, 0.0]), [0, 0, 0])
    p0 = vec3(body_spec.get("initial_position_m") or [0.0, 0.0, 0.12])
    v0 = vec3(body_spec.get("initial_velocity_m_s") or [1.2, 0.0, 0.0])
    expected = dict(case_spec.get("expected_physics") or {})
    travel = float(expected.get("fallback_roll_distance_m") or midpoint_value(float(expected.get("expected_min_roll_distance_m") or 0.15), float(expected.get("expected_max_roll_distance_m") or 0.9)))
    final_speed = min(abs(v0[0]) * 0.25, float(expected.get("expected_final_speed_max_m_s") or abs(v0[0]) * 0.4))
    if negative_mode == "no_deceleration":
        travel = float(expected.get("expected_max_roll_distance_m") or travel) + 0.35
        final_speed = max(abs(v0[0]) * 0.92, float(expected.get("expected_final_speed_max_m_s") or 0.0) + 0.3)
    elif negative_mode == "excessive_friction_stop":
        travel = max(0.0, float(expected.get("expected_min_roll_distance_m") or travel) * 0.2)
        final_speed = 0.0
    initial = {support_id: support_state, body_id: state(p0, v0)}
    mid = {support_id: support_state, body_id: state([round(p0[0] + travel * 0.65, 4), p0[1], p0[2]], [round(max(final_speed, abs(v0[0]) * 0.45), 4), 0.0, 0.0])}
    final = {support_id: support_state, body_id: state([round(p0[0] + travel, 4), p0[1], p0[2]], [round(final_speed, 4), 0.0, 0.0])}
    contacts = [] if negative_mode == "missing_contact" else [contact(body_id, support_id, 0, 0.0), contact(body_id, support_id, 1, 0.3), contact(body_id, support_id, 2, 0.6)]
    return [
        frame(0, 0.0, initial, contacts=contacts[:1]),
        frame(1, 0.3, mid, contacts=contacts[1:2]),
        frame(2, 0.6, final, contacts=contacts[2:]),
    ]


def sliding_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "no_deceleration" in case_id:
        negative_mode = "no_deceleration"
    if not negative_mode and "static_threshold_violation" in case_id:
        negative_mode = "static_threshold_violation"
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    body_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"sliding_body", "sliding_crate", "friction_subject"}), "sliding_crate")
    support_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"support", "ground", "floor"}), "floor")
    body_spec = object_specs.get(body_id) or {"initial_position_m": [0.0, 0.0, 0.25], "initial_velocity_m_s": [1.2, 0.0, 0.0]}
    support_state = state(vec3((object_specs.get(support_id) or {}).get("initial_position_m") or [0.0, 0.0, 0.0]), [0, 0, 0])
    p0 = vec3(body_spec.get("initial_position_m") or [0.0, 0.0, 0.25])
    v0 = vec3(body_spec.get("initial_velocity_m_s") or [1.2, 0.0, 0.0])
    expected = dict(case_spec.get("expected_physics") or {})
    mode = str(expected.get("mode") or "sliding_stop")
    if mode == "static_threshold":
        travel = 0.0
        final_speed = 0.0
        if negative_mode == "static_threshold_violation":
            travel = max(0.08, float(expected.get("max_static_displacement_m") or 0.02) * 8.0)
            final_speed = max(0.12, float(expected.get("expected_final_speed_max_m_s") or 0.02) * 8.0)
        initial = {support_id: support_state, body_id: state(p0, [0.0, 0.0, 0.0])}
        final = {support_id: support_state, body_id: state([round(p0[0] + travel, 4), p0[1], p0[2]], [round(final_speed, 4), 0.0, 0.0])}
        contacts = [] if negative_mode == "missing_contact" else [contact(body_id, support_id, 0, 0.0), contact(body_id, support_id, 1, 0.5)]
        return [frame(0, 0.0, initial, contacts=contacts[:1]), frame(1, 0.5, final, contacts=contacts[1:])]

    travel = float(expected.get("fallback_slide_distance_m") or midpoint_value(float(expected.get("expected_min_slide_distance_m") or 0.1), float(expected.get("expected_max_slide_distance_m") or 0.7)))
    final_speed = min(abs(v0[0]) * 0.12, float(expected.get("expected_final_speed_max_m_s") or abs(v0[0]) * 0.2))
    if negative_mode == "no_deceleration":
        travel = float(expected.get("expected_max_slide_distance_m") or travel) + 0.35
        final_speed = max(abs(v0[0]) * 0.9, float(expected.get("expected_final_speed_max_m_s") or 0.0) + 0.25)
    elif negative_mode == "excessive_friction_stop":
        travel = max(0.0, float(expected.get("expected_min_slide_distance_m") or travel) * 0.2)
        final_speed = 0.0
    initial = {support_id: support_state, body_id: state(p0, v0)}
    mid = {support_id: support_state, body_id: state([round(p0[0] + travel * 0.65, 4), p0[1], p0[2]], [round(max(final_speed, abs(v0[0]) * 0.35), 4), 0.0, 0.0])}
    final = {support_id: support_state, body_id: state([round(p0[0] + travel, 4), p0[1], p0[2]], [round(final_speed, 4), 0.0, 0.0])}
    contacts = [] if negative_mode == "missing_contact" else [contact(body_id, support_id, 0, 0.0), contact(body_id, support_id, 1, 0.3), contact(body_id, support_id, 2, 0.6)]
    return [
        frame(0, 0.0, initial, contacts=contacts[:1]),
        frame(1, 0.3, mid, contacts=contacts[1:2]),
        frame(2, 0.6, final, contacts=contacts[2:]),
    ]


def wind_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "wrong_direction" in case_id:
        negative_mode = "wrong_direction"
    if not negative_mode and "no_wind_drift" in case_id:
        negative_mode = "no_wind_drift"
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    body_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"wind_drift_body", "wind_subject", "balloon", "light_body"}), "wind_body")
    body_spec = object_specs.get(body_id) or {"initial_position_m": [0.0, 0.0, 1.0], "initial_velocity_m_s": [0.0, 0.0, 0.0]}
    expected = dict(case_spec.get("expected_physics") or {})
    wind = vec3(expected.get("wind_vector_m_s") or expected.get("wind_vector") or [1.0, 0.0, 0.0])
    horizontal = math.sqrt(wind[0] * wind[0] + wind[1] * wind[1])
    unit = [1.0, 0.0] if horizontal <= 1e-9 else [wind[0] / horizontal, wind[1] / horizontal]
    drift = float(expected.get("fallback_wind_drift_m") or midpoint_value(float(expected.get("expected_min_wind_aligned_drift_m") or 0.2), float(expected.get("expected_max_wind_aligned_drift_m") or 0.9)))
    if negative_mode == "wrong_direction":
        drift = -abs(drift)
    elif negative_mode == "no_wind_drift":
        drift = max(0.01, float(expected.get("expected_min_wind_aligned_drift_m") or 0.3) * 0.15)
    p0 = vec3(body_spec.get("initial_position_m") or [0.0, 0.0, 1.0])
    v0 = vec3(body_spec.get("initial_velocity_m_s") or [0.0, 0.0, 0.0])
    z_mid = p0[2] + 0.03
    z_end = p0[2] + 0.05
    initial = {body_id: state(p0, v0)}
    mid = {body_id: state([round(p0[0] + unit[0] * drift * 0.55, 4), round(p0[1] + unit[1] * drift * 0.55, 4), round(z_mid, 4)], [round(unit[0] * abs(drift), 4), round(unit[1] * abs(drift), 4), 0.05])}
    final = {body_id: state([round(p0[0] + unit[0] * drift, 4), round(p0[1] + unit[1] * drift, 4), round(z_end, 4)], [round(unit[0] * abs(drift) * 0.35, 4), round(unit[1] * abs(drift) * 0.35, 4), 0.0])}
    return [frame(0, 0.0, initial), frame(1, 0.4, mid), frame(2, 0.8, final)]


def mass_ratio_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "wrong_velocity_order" in case_id:
        negative_mode = "wrong_velocity_order"
    if not negative_mode and "momentum_gain" in case_id:
        negative_mode = "momentum_gain"
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    active_id = str((case_spec.get("active_objects") or ["striker"])[0])
    passive_id = str((case_spec.get("passive_objects") or ["target"])[0])
    active_spec = object_specs.get(active_id) or {"initial_position_m": [-0.5, 0.0, 0.12], "initial_velocity_m_s": [1.0, 0.0, 0.0], "mass_kg": 1.0}
    passive_spec = object_specs.get(passive_id) or {"initial_position_m": [0.0, 0.0, 0.12], "initial_velocity_m_s": [0.0, 0.0, 0.0], "mass_kg": 1.0}
    p_active = vec3(active_spec.get("initial_position_m") or [-0.5, 0.0, 0.12])
    p_passive = vec3(passive_spec.get("initial_position_m") or [0.0, 0.0, 0.12])
    v_active = vec3(active_spec.get("initial_velocity_m_s") or [1.0, 0.0, 0.0])
    m_active = float(active_spec.get("mass_kg") or 1.0)
    m_passive = float(passive_spec.get("mass_kg") or 1.0)
    restitution = float((case_spec.get("expected_physics") or {}).get("restitution") or active_spec.get("restitution") or 0.6)
    u = v_active[0]
    denominator = max(m_active + m_passive, 1e-9)
    v1_post = ((m_active - restitution * m_passive) / denominator) * u
    v2_post = (((1.0 + restitution) * m_active) / denominator) * u
    if negative_mode == "wrong_velocity_order":
        v1_post = max(0.65 * abs(u), 0.55)
        v2_post = max(0.25 * abs(u), 0.25)
    elif negative_mode == "momentum_gain":
        v1_post = min(float((case_spec.get("expected_physics") or {}).get("expected_striker_speed_abs_max_m_s") or abs(u)), abs(u) * 0.78)
        v2_post = min(float((case_spec.get("expected_physics") or {}).get("expected_target_speed_max_m_s") or abs(u) * 2.0), abs(u) * 1.65)
    initial = {
        active_id: state(p_active, v_active),
        passive_id: state(p_passive, vec3(passive_spec.get("initial_velocity_m_s") or [0.0, 0.0, 0.0])),
    }
    pre = {
        active_id: state(midpoint(p_active, p_passive, 0.7), [round(u * 0.9, 4), 0.0, 0.0]),
        passive_id: state(p_passive, [0.0, 0.0, 0.0]),
    }
    post = {
        active_id: state([round(p_passive[0] - 0.03 + v1_post * 0.08, 4), p_active[1], p_active[2]], [round(v1_post, 4), 0.0, 0.0]),
        passive_id: state([round(p_passive[0] + 0.04 + v2_post * 0.08, 4), p_passive[1], p_passive[2]], [round(v2_post, 4), 0.0, 0.0]),
    }
    final = {
        active_id: state([round(vec3(post[active_id].get("position_m"))[0] + v1_post * 0.18, 4), p_active[1], p_active[2]], [round(v1_post * 0.75, 4), 0.0, 0.0]),
        passive_id: state([round(vec3(post[passive_id].get("position_m"))[0] + v2_post * 0.18, 4), p_passive[1], p_passive[2]], [round(v2_post * 0.85, 4), 0.0, 0.0]),
    }
    return [
        frame(0, 0.0, initial),
        frame(1, 0.2, pre),
        frame(2, 0.4, post, contacts=[contact(active_id, passive_id, 2, 0.4)]),
        frame(3, 0.6, final),
    ]


def spin_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "no_spin_decay" in case_id:
        negative_mode = "no_spin_decay"
    if not negative_mode and "spin_gain" in case_id:
        negative_mode = "spin_gain"
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    subject_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"spinning_body", "spin_subject", "angular_damping_subject"}), "spinner")
    subject = object_specs.get(subject_id) or {
        "initial_position_m": [0.0, 0.0, 0.2],
        "initial_velocity_m_s": [0.0, 0.0, 0.0],
        "initial_rotation_deg": [0.0, 0.0, 0.0],
        "initial_angular_velocity_deg_s": [0.0, 0.0, 360.0],
    }
    expected = dict(case_spec.get("expected_physics") or {})
    p0 = vec3(subject.get("initial_position_m") or [0.0, 0.0, 0.2])
    v0 = vec3(subject.get("initial_velocity_m_s") or [0.0, 0.0, 0.0])
    initial_rotation = vec3(subject.get("initial_rotation_deg") or [0.0, 0.0, 0.0])
    initial_angular_velocity = vec3(subject.get("initial_angular_velocity_deg_s") or [0.0, 0.0, expected.get("initial_angular_speed_deg_s") or 360.0])
    w0 = abs(float(expected.get("initial_angular_speed_deg_s") or initial_angular_velocity[2] or 360.0))
    damping = max(float(expected.get("angular_damping") or subject.get("angular_damping") or 0.5), 0.0)
    duration = max(float(expected.get("spin_duration_s") or 1.0), 0.2)
    sample_times = [0.0, round(duration / 3.0, 4), round(2.0 * duration / 3.0, 4), round(duration, 4)]
    if negative_mode == "no_spin_decay":
        speeds = [w0, w0 * 0.98, w0 * 0.95, w0 * 0.93]
    elif negative_mode == "spin_gain":
        speeds = [w0, w0 * 1.1, w0 * 1.2, w0 * 1.3]
    else:
        speeds = [w0 * math.exp(-damping * t) for t in sample_times]
    frames: list[dict[str, Any]] = []
    rotation_z = initial_rotation[2]
    previous_time = sample_times[0]
    previous_speed = speeds[0]
    for frame_id, (time_s, speed) in enumerate(zip(sample_times, speeds)):
        if frame_id > 0:
            dt = time_s - previous_time
            rotation_z += (previous_speed + speed) * 0.5 * dt
        angular_velocity = [0.0, 0.0, round(speed, 4)]
        frames.append(
            frame(
                frame_id,
                time_s,
                {
                    subject_id: state(
                        p0,
                        v0,
                        rotation=[initial_rotation[0], initial_rotation[1], round(rotation_z, 4)],
                        angular_velocity=angular_velocity,
                    )
                },
            )
        )
        previous_time = time_s
        previous_speed = speed
    return frames


def state(position: list[float], velocity: list[float], *, rotation: list[float] | None = None, angular_velocity: list[float] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"position_m": position, "velocity_m_s": velocity, "rotation_deg": rotation or [0, 0, 0]}
    if angular_velocity is not None:
        result["angular_velocity_deg_s"] = angular_velocity
    return result


def state_from_spec(obj: dict[str, Any]) -> dict[str, Any]:
    return state(vec3(obj.get("initial_position_m") or [0, 0, 0]), vec3(obj.get("initial_velocity_m_s") or [0, 0, 0]))


def clone_states(states: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {key: {inner_key: list(value) if isinstance(value, list) else value for inner_key, value in state_value.items()} for key, state_value in states.items()}


def midpoint(a: list[float], b: list[float], fraction: float) -> list[float]:
    return [round(a[idx] + (b[idx] - a[idx]) * fraction, 4) for idx in range(3)]


def midpoint_value(a: float, b: float) -> float:
    return round(a + (b - a) * 0.5, 4)


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    return [float(padded[0]), float(padded[1]), float(padded[2])]


def frame(frame_id: int, time_s: float, objects: dict[str, Any], *, contacts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"frame": frame_id, "time_s": time_s, "objects": objects, "contacts": contacts or []}


def contact(a: str, b: str, frame_id: int, time_s: float) -> dict[str, Any]:
    return {"objects": [a, b], "frame": frame_id, "time_s": time_s, "method": "fallback_deterministic_contact"}
