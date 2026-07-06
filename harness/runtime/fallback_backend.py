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
    if capability_id == "agent_rigidbody_action_coupling":
        return agent_action_trajectory(case_id, case_spec)
    if capability_id == "constraint_distance_pendulum_motion":
        return constraint_trajectory(case_id, case_spec)
    if capability_id == "constraint_momentum_transfer":
        return impulse_chain_trajectory(case_id, case_spec)
    if capability_id == "elastic_energy_launch":
        return elastic_launch_trajectory(case_id, case_spec)
    if capability_id == "elastic_constraint_rebound":
        return elastic_constraint_trajectory(case_id, case_spec)
    if capability_id == "brittle_impact_fracture":
        return brittle_fracture_trajectory(case_id, case_spec)
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


def agent_action_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "preaction" in case_id:
        negative_mode = "preaction_motion"
    if not negative_mode and "missing_action_trace" in case_id:
        negative_mode = "missing_action_trace"
    if not negative_mode and "no_post_action" in case_id:
        negative_mode = "no_post_action_motion"

    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    expected = dict(case_spec.get("expected_physics") or {})
    actor_id = str(expected.get("action_actor_id") or next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"active_agent", "agent_controller", "pushing_agent", "throwing_agent"}), "agent"))
    target_id = str(expected.get("target_object_id") or next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"action_coupled_body", "pushed_body", "thrown_body", "rigid_body_payload"}), "target"))
    support_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"support", "floor", "ground"}), "floor")
    actor_spec = object_specs.get(actor_id) or {"initial_position_m": [-0.35, 0.0, 0.45], "initial_velocity_m_s": [0.0, 0.0, 0.0]}
    target_spec = object_specs.get(target_id) or {"initial_position_m": [0.0, 0.0, 0.25], "initial_velocity_m_s": [0.0, 0.0, 0.0]}
    support_state = state(vec3((object_specs.get(support_id) or {}).get("initial_position_m") or [0.0, 0.0, 0.0]), [0.0, 0.0, 0.0])
    actor_p0 = vec3(actor_spec.get("initial_position_m") or [-0.35, 0.0, 0.45])
    target_p0 = vec3(target_spec.get("initial_position_m") or [0.0, 0.0, 0.25])
    actor_state = state(actor_p0, vec3(actor_spec.get("initial_velocity_m_s") or [0.0, 0.0, 0.0]))
    target_initial_velocity = [0.0, 0.0, 0.0]
    if negative_mode == "preaction_motion":
        target_initial_velocity = [0.16, 0.0, 0.0]
    coupling_type = str(expected.get("coupling_type") or "push")
    action_frame = int(expected.get("action_frame") or 1)
    action_time = float(expected.get("action_time_s") or 0.2)
    action_trace = [dict(action) for action in expected.get("action_trace") or [] if isinstance(action, dict)]
    if not action_trace and negative_mode != "missing_action_trace":
        action_trace = [
            {
                "frame": action_frame,
                "time_s": action_time,
                "actor_id": actor_id,
                "target_id": target_id,
                "action_type": coupling_type,
                "impulse_n_s": [1.0, 0.0, 0.0],
            }
        ]

    initial = {
        support_id: support_state,
        actor_id: actor_state,
        target_id: state(target_p0, target_initial_velocity),
    }
    pre_target_position = target_p0
    if negative_mode == "preaction_motion":
        pre_target_position = [round(target_p0[0] + 0.06, 4), target_p0[1], target_p0[2]]
    pre = {
        support_id: support_state,
        actor_id: state(midpoint(actor_p0, target_p0, 0.75), [0.2, 0.0, 0.0]),
        target_id: state(pre_target_position, target_initial_velocity),
    }
    action_velocity = [0.0, 0.0, 0.0] if negative_mode == "no_post_action_motion" else [0.85, 0.0, 0.0]
    action_position = pre_target_position if negative_mode == "no_post_action_motion" else [round(target_p0[0] + 0.16, 4), target_p0[1], target_p0[2]]
    if coupling_type in {"throw", "release"}:
        action_velocity = [1.8, 0.0, 1.2] if negative_mode != "no_post_action_motion" else [0.0, 0.0, 0.0]
        action_position = pre_target_position if negative_mode == "no_post_action_motion" else [round(target_p0[0] + 0.22, 4), target_p0[1], round(target_p0[2] + 0.18, 4)]
    action_frame_state = {
        support_id: support_state,
        actor_id: state(midpoint(actor_p0, target_p0, 0.92), [0.0, 0.0, 0.0]),
        target_id: state(action_position, action_velocity),
    }
    final_position = action_position if negative_mode == "no_post_action_motion" else [round(action_position[0] + max(abs(action_velocity[0]) * 0.18, 0.14), 4), action_position[1], round(max(0.18, action_position[2] + action_velocity[2] * 0.08 - 0.04), 4)]
    final = {
        support_id: support_state,
        actor_id: action_frame_state[actor_id],
        target_id: state(final_position, [round(action_velocity[0] * 0.65, 4), 0.0, round(action_velocity[2] * 0.45, 4)]),
    }
    contacts = [] if negative_mode == "missing_contact" else [contact(actor_id, target_id, action_frame, action_time)]
    actions = [] if negative_mode == "missing_action_trace" else action_trace
    return [
        frame(0, 0.0, initial),
        frame(max(0, action_frame - 1), max(0.0, round(action_time * 0.5, 4)), pre),
        frame(action_frame, action_time, action_frame_state, contacts=contacts, actions=actions),
        frame(action_frame + 1, round(action_time + 0.25, 4), final),
    ]


def constraint_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "length_drift" in case_id:
        negative_mode = "constraint_length_drift"
    if not negative_mode and "teleport" in case_id:
        negative_mode = "teleporting_body"
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    expected = dict(case_spec.get("expected_physics") or {})
    anchor_id = str(expected.get("anchor_object_id") or next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") == "constraint_anchor"), "anchor"))
    body_id = str(expected.get("constrained_object_id") or next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") == "constrained_body"), "bob"))
    anchor_spec = object_specs.get(anchor_id) or {"initial_position_m": [0.0, 0.0, 1.6]}
    body_spec = object_specs.get(body_id) or {"initial_position_m": [0.6, 0.0, 0.6]}
    anchor_pos = vec3(anchor_spec.get("initial_position_m") or [0.0, 0.0, 1.6])
    length = float(expected.get("constraint_length_m") or 1.0)
    release_angle = math.radians(float(expected.get("release_angle_deg") or 30.0))
    angles = [release_angle, release_angle * 0.28, -release_angle * 0.35, -release_angle * 0.16]
    if negative_mode == "teleporting_body":
        angles = [release_angle, -release_angle, release_angle, -release_angle]
    frames: list[dict[str, Any]] = []
    previous_body = vec3(body_spec.get("initial_position_m") or pendulum_position(anchor_pos, length, angles[0]))
    for frame_id, angle in enumerate(angles):
        distance_length = length
        if negative_mode == "constraint_length_drift" and frame_id >= 2:
            distance_length = length * 1.24
        body_pos = pendulum_position(anchor_pos, distance_length, angle)
        if negative_mode == "teleporting_body" and frame_id == 2:
            body_pos = [round(-previous_body[0] - length * 0.9, 4), previous_body[1], previous_body[2]]
            vector = [body_pos[0] - anchor_pos[0], body_pos[1] - anchor_pos[1], body_pos[2] - anchor_pos[2]]
            scale = length / max(math.sqrt(sum(item * item for item in vector)), 1e-9)
            body_pos = [round(anchor_pos[idx] + vector[idx] * scale, 4) for idx in range(3)]
        velocity = [round(body_pos[idx] - previous_body[idx], 4) for idx in range(3)]
        states = {
            anchor_id: state(anchor_pos, [0.0, 0.0, 0.0]),
            body_id: state(body_pos, velocity),
        }
        constraint_row = {
            "constraint_id": "pendulum_distance",
            "anchor_id": anchor_id,
            "body_id": body_id,
            "constraint_length_m": round(length, 6),
            "measured_distance_m": round(math.sqrt(sum((body_pos[idx] - anchor_pos[idx]) ** 2 for idx in range(3))), 6),
        }
        frames.append(frame(frame_id, round(frame_id * 0.25, 4), states, constraints=[constraint_row]))
        previous_body = body_pos
    return frames


def pendulum_position(anchor_pos: list[float], length: float, angle_rad: float) -> list[float]:
    return [round(anchor_pos[0] + math.sin(angle_rad) * length, 4), anchor_pos[1], round(anchor_pos[2] - math.cos(angle_rad) * length, 4)]


def impulse_chain_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "prechain" in case_id:
        negative_mode = "passive_prechain_motion"
    if not negative_mode and "terminal_no_response" in case_id:
        negative_mode = "terminal_no_response"
    if not negative_mode and "order" in case_id:
        negative_mode = "contact_order_violation"
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    expected = dict(case_spec.get("expected_physics") or {})
    chain = [str(item) for item in expected.get("chain_objects") or []]
    if not chain:
        chain = [str(obj.get("id")) for obj in case_spec.get("objects", []) if str(obj.get("role") or "") in {"active_chain_driver", "constrained_chain_body"}]
    if len(chain) < 3:
        chain = [f"chain_body_{idx}" for idx in range(5)]
    active_id = str(expected.get("active_object_id") or chain[0])
    receiver_id = str(expected.get("receiver_object_id") or chain[-1])
    initial_speed = abs(vec3((object_specs.get(active_id) or {}).get("initial_velocity_m_s") or [0.9, 0, 0])[0]) or 0.9
    spacing = infer_chain_spacing(chain, object_specs)
    base_positions = {
        object_id: vec3((object_specs.get(object_id) or {}).get("initial_position_m") or [idx * spacing, 0.0, 0.9])
        for idx, object_id in enumerate(chain)
    }
    max_frame = len(chain)
    frames: list[dict[str, Any]] = []
    for frame_id in range(max_frame):
        states = {}
        for idx, object_id in enumerate(chain):
            position = list(base_positions[object_id])
            velocity = [0.0, 0.0, 0.0]
            if object_id == active_id and frame_id <= 1:
                velocity = [round(initial_speed if frame_id == 0 else initial_speed * 0.12, 4), 0.0, 0.0]
                position[0] = round(position[0] + spacing * 0.45 * frame_id, 4)
            elif object_id == receiver_id and frame_id >= len(chain) - 1:
                receiver_speed = 0.0 if negative_mode == "terminal_no_response" else round(initial_speed * 0.68, 4)
                velocity = [receiver_speed, 0.0, 0.0]
                position[0] = round(position[0] + (0.0 if negative_mode == "terminal_no_response" else spacing * 0.65), 4)
            elif 0 < idx < len(chain) - 1 and frame_id >= idx + 1:
                velocity = [round(initial_speed * 0.08, 4), 0.0, 0.0]
                position[0] = round(position[0] + spacing * 0.06, 4)
            states[object_id] = state(position, velocity)
        if negative_mode == "passive_prechain_motion" and len(chain) > 2 and frame_id == 0:
            states[chain[2]]["velocity_m_s"] = [round(initial_speed * 0.25, 4), 0.0, 0.0]
        contacts = impulse_chain_contacts(chain, frame_id, negative_mode)
        constraints = impulse_chain_constraints(chain, base_positions, states, spacing, frame_id)
        frames.append(frame(frame_id, round(frame_id * 0.2, 4), states, contacts=contacts, constraints=constraints))
    return frames


def impulse_chain_contacts(chain: list[str], frame_id: int, negative_mode: str) -> list[dict[str, Any]]:
    if frame_id == 0:
        return []
    if frame_id >= len(chain):
        return []
    edge_index = frame_id - 1
    if negative_mode == "contact_order_violation" and frame_id == 1 and len(chain) >= 4:
        edge_index = 2
    elif negative_mode == "contact_order_violation" and frame_id == 3:
        edge_index = 0
    if edge_index < 0 or edge_index >= len(chain) - 1:
        return []
    return [contact(chain[edge_index], chain[edge_index + 1], frame_id, round(frame_id * 0.2, 4))]


def impulse_chain_constraints(chain: list[str], base_positions: dict[str, list[float]], states: dict[str, Any], spacing: float, frame_id: int) -> list[dict[str, Any]]:
    rows = []
    for object_id in chain:
        anchor_id = f"{object_id}_anchor"
        base = base_positions[object_id]
        anchor_position = [base[0], base[1], round(base[2] + spacing * 3.0, 4)]
        body_position = vec3(states[object_id].get("position_m"))
        rows.append(
            {
                "constraint_id": f"{object_id}_suspension",
                "anchor_id": anchor_id,
                "body_id": object_id,
                "constraint_length_m": round(dist(anchor_position, base), 6),
                "measured_distance_m": round(dist(anchor_position, body_position), 6),
                "frame": frame_id,
            }
        )
    return rows


def infer_chain_spacing(chain: list[str], object_specs: dict[str, dict[str, Any]]) -> float:
    if len(chain) >= 2:
        first = vec3((object_specs.get(chain[0]) or {}).get("initial_position_m"))
        second = vec3((object_specs.get(chain[1]) or {}).get("initial_position_m"))
        spacing = abs(second[0] - first[0])
        if spacing > 0.01:
            return spacing
    return 0.18


def elastic_launch_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "missing_release" in case_id:
        negative_mode = "missing_release_event"
    if not negative_mode and "no_launch" in case_id:
        negative_mode = "no_launch_response"
    if not negative_mode and "energy_gain" in case_id:
        negative_mode = "energy_gain"
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    expected = dict(case_spec.get("expected_physics") or {})
    launcher_id = str(expected.get("launcher_object_id") or next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") == "elastic_launcher"), "spring"))
    payload_id = str(expected.get("launched_object_id") or next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") == "launched_body"), "payload"))
    support_id = next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"support", "floor", "ground"}), "floor")
    launcher_spec = object_specs.get(launcher_id) or {"initial_position_m": [0.0, 0.0, 0.1]}
    payload_spec = object_specs.get(payload_id) or {"initial_position_m": [0.0, 0.0, 0.22], "mass_kg": 0.5}
    support_spec = object_specs.get(support_id) or {"initial_position_m": [0.0, 0.0, 0.0]}
    launcher_state = state(vec3(launcher_spec.get("initial_position_m") or [0.0, 0.0, 0.1]), [0.0, 0.0, 0.0])
    support_state = state(vec3(support_spec.get("initial_position_m") or [0.0, 0.0, 0.0]), [0.0, 0.0, 0.0])
    p0 = vec3(payload_spec.get("initial_position_m") or [0.0, 0.0, 0.22])
    mass = float(expected.get("payload_mass_kg") or payload_spec.get("mass_kg") or 0.5)
    spring_constant = float(expected.get("spring_constant_n_m") or launcher_spec.get("spring_constant_n_m") or 120.0)
    compression = float(expected.get("compression_m") or launcher_spec.get("compression_m") or 0.18)
    angle = math.radians(float(expected.get("launch_angle_deg") or 55.0))
    stored_energy = 0.5 * spring_constant * compression * compression
    speed = math.sqrt(max(2.0 * stored_energy / max(mass, 1e-6), 0.0)) * 0.88
    if negative_mode == "energy_gain":
        speed *= 2.2
    if negative_mode == "no_launch_response":
        speed = 0.0
    vx = round(speed * math.cos(angle), 4)
    vz = round(speed * math.sin(angle), 4)
    initial = {
        launcher_id: launcher_state,
        payload_id: state(p0, [0.0, 0.0, 0.0]),
        support_id: support_state,
    }
    release_pos = [round(p0[0] + vx * 0.12, 4), p0[1], round(p0[2] + max(vz * 0.12, 0.0), 4)]
    final_pos = [round(p0[0] + vx * 0.35, 4), p0[1], round(p0[2] + max(vz * 0.28 - 0.08, 0.0), 4)]
    release_event = {
        "event_type": "release",
        "launcher_id": launcher_id,
        "target_id": payload_id,
        "frame": 1,
        "time_s": 0.2,
        "compression_m": round(compression, 6),
        "spring_constant_n_m": round(spring_constant, 6),
        "stored_energy_j": round(stored_energy, 6),
    }
    spring_events = [] if negative_mode == "missing_release_event" else [release_event]
    release = {
        launcher_id: launcher_state,
        payload_id: state(release_pos if negative_mode != "no_launch_response" else p0, [vx, 0.0, vz]),
        support_id: support_state,
    }
    final = {
        launcher_id: launcher_state,
        payload_id: state(final_pos if negative_mode != "no_launch_response" else p0, [round(vx * 0.55, 4), 0.0, round(max(vz * 0.25, 0.0), 4)]),
        support_id: support_state,
    }
    return [
        frame(0, 0.0, initial, contacts=[contact(payload_id, launcher_id, 0, 0.0)]),
        frame(1, 0.2, release, spring_events=spring_events),
        frame(2, 0.4, final),
    ]


def elastic_constraint_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "missing_constraint" in case_id:
        negative_mode = "missing_constraint_trace"
    if not negative_mode and "overstretch" in case_id:
        negative_mode = "overstretch"
    if not negative_mode and "no_rebound" in case_id:
        negative_mode = "no_rebound"
    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    expected = dict(case_spec.get("expected_physics") or {})
    anchor_id = str(expected.get("anchor_object_id") or next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") == "elastic_constraint_anchor"), "anchor"))
    body_id = str(expected.get("constrained_object_id") or next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") == "elastic_constrained_body"), "payload"))
    anchor_spec = object_specs.get(anchor_id) or {"initial_position_m": [0.0, 0.0, 2.0]}
    body_spec = object_specs.get(body_id) or {"initial_position_m": [0.0, 0.0, 1.0]}
    anchor_pos = vec3(anchor_spec.get("initial_position_m") or [0.0, 0.0, 2.0])
    body_pos = vec3(body_spec.get("initial_position_m") or [0.0, 0.0, anchor_pos[2] - 1.0])
    rest_length = float(expected.get("rest_length_m") or max(0.5, abs(anchor_pos[2] - body_pos[2])))
    max_extension = float(expected.get("max_extension_m") or max(0.25, rest_length * 0.35))
    peak_extension = max(float(expected.get("expected_min_extension_m") or max_extension * 0.65), max_extension * 0.82)
    if negative_mode == "overstretch":
        peak_extension = max_extension * 1.35
    initial_distance = abs(anchor_pos[2] - body_pos[2])
    fall_z = anchor_pos[2] - rest_length - peak_extension * 0.6
    peak_z = anchor_pos[2] - rest_length - peak_extension
    rebound_z = anchor_pos[2] - rest_length - peak_extension * (0.42 if negative_mode != "no_rebound" else 0.9)
    rebound_vz = 0.85 if negative_mode != "no_rebound" else -0.05

    def states(payload_z: float, payload_vz: float) -> dict[str, Any]:
        return {
            anchor_id: state(anchor_pos, [0.0, 0.0, 0.0]),
            body_id: state([body_pos[0], body_pos[1], round(payload_z, 4)], [0.0, 0.0, round(payload_vz, 4)]),
        }

    def constraint_for(frame_id: int, time_s: float, payload_z: float) -> list[dict[str, Any]]:
        if negative_mode == "missing_constraint_trace":
            return []
        distance = abs(anchor_pos[2] - payload_z)
        extension = max(0.0, distance - rest_length)
        return [
            {
                "constraint_id": "elastic_tether",
                "constraint_type": "elastic_tether",
                "anchor_id": anchor_id,
                "body_id": body_id,
                "rest_length_m": round(rest_length, 6),
                "measured_distance_m": round(distance, 6),
                "extension_m": round(extension, 6),
                "frame": frame_id,
                "time_s": time_s,
            }
        ]

    return [
        frame(0, 0.0, states(body_pos[2], 0.0), constraints=constraint_for(0, 0.0, body_pos[2])),
        frame(1, 0.2, states(fall_z, -1.25), constraints=constraint_for(1, 0.2, fall_z)),
        frame(2, 0.4, states(peak_z, -0.15), constraints=constraint_for(2, 0.4, peak_z)),
        frame(3, 0.6, states(rebound_z, rebound_vz), constraints=constraint_for(3, 0.6, rebound_z)),
    ]


def brittle_fracture_trajectory(case_id: str, case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    negative_mode = str(case_spec.get("negative_mode") or "")
    if not negative_mode and "missing_fracture" in case_id:
        negative_mode = "missing_fracture_event"
    if not negative_mode and "before_contact" in case_id:
        negative_mode = "fracture_before_contact"
    if not negative_mode and "below_threshold" in case_id:
        negative_mode = "below_threshold_fracture"
    if not negative_mode and "too_few" in case_id:
        negative_mode = "too_few_fragments"

    object_specs = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    expected = dict(case_spec.get("expected_physics") or {})
    impactor_id = str(expected.get("impactor_object_id") or next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"active_impactor", "active_striker"}), "striker"))
    brittle_id = str(expected.get("brittle_object_id") or next((oid for oid, obj in object_specs.items() if str(obj.get("role") or "") in {"brittle_fracture_body", "breakable_body", "destructible_body"}), "brittle_body"))
    impactor_spec = object_specs.get(impactor_id) or {"initial_position_m": [-0.6, 0.0, 0.4], "initial_velocity_m_s": [2.8, 0.0, 0.0], "mass_kg": 1.2}
    brittle_spec = object_specs.get(brittle_id) or {"initial_position_m": [0.0, 0.0, 0.4], "initial_velocity_m_s": [0.0, 0.0, 0.0], "mass_kg": 0.7}
    p_impactor = vec3(impactor_spec.get("initial_position_m") or [-0.6, 0.0, 0.4])
    p_brittle = vec3(brittle_spec.get("initial_position_m") or [0.0, 0.0, 0.4])
    v_impactor = vec3(impactor_spec.get("initial_velocity_m_s") or [2.8, 0.0, 0.0])
    threshold = float(expected.get("fracture_threshold_j") or brittle_spec.get("fracture_threshold_j") or 2.5)
    min_fragments = int(expected.get("expected_min_fragment_count") or 6)
    impact_energy = float(expected.get("impact_energy_j") or max(threshold * 1.6, 3.0))
    if negative_mode == "below_threshold_fracture":
        impact_energy = round(threshold * 0.45, 6)
    fragment_count = min_fragments
    if negative_mode == "too_few_fragments":
        fragment_count = max(1, min_fragments - 3)

    initial = {
        impactor_id: state(p_impactor, v_impactor),
        brittle_id: state(p_brittle, vec3(brittle_spec.get("initial_velocity_m_s") or [0.0, 0.0, 0.0])),
    }
    contact_state = {
        impactor_id: state(midpoint(p_impactor, p_brittle, 0.92), [round(v_impactor[0] * 0.18, 4), round(v_impactor[1] * 0.18, 4), round(v_impactor[2] * 0.18, 4)]),
        brittle_id: state(p_brittle, [0.0, 0.0, 0.0]),
    }
    post_state = {
        impactor_id: state([round(p_brittle[0] + 0.08, 4), p_impactor[1], p_impactor[2]], [0.05, 0.0, 0.0]),
        brittle_id: {**state(p_brittle, [0.0, 0.0, 0.0]), "fractured": negative_mode != "missing_fracture_event"},
    }

    contact_event = contact(impactor_id, brittle_id, 1, 0.2)
    contact_event["impact_energy_j"] = round(impact_energy, 6)
    contact_event["normal_impulse_n_s"] = round(max(0.05, impact_energy / max(abs(v_impactor[0]) or 1.0, 1e-6)), 6)
    fracture_event = {
        "event_type": "fracture",
        "object_id": brittle_id,
        "caused_by_object_id": impactor_id,
        "frame": 0 if negative_mode == "fracture_before_contact" else 1,
        "time_s": 0.0 if negative_mode == "fracture_before_contact" else 0.2,
        "impact_energy_j": round(impact_energy, 6),
        "fracture_threshold_j": round(threshold, 6),
        "fragment_count": fragment_count,
    }
    fragments = [
        {"fragment_id": f"{brittle_id}_frag_{idx}", "source_object_id": brittle_id}
        for idx in range(fragment_count)
    ]
    frame0_fracture = [fracture_event] if negative_mode == "fracture_before_contact" else []
    frame1_fracture = [] if negative_mode in {"missing_fracture_event", "fracture_before_contact"} else [fracture_event]
    frame1_fragments = [] if negative_mode in {"missing_fracture_event", "fracture_before_contact"} else fragments
    frame2_fragments = [] if negative_mode == "missing_fracture_event" else fragments
    return [
        frame(0, 0.0, initial, fracture_events=frame0_fracture, fragments=fragments if frame0_fracture else []),
        frame(1, 0.2, contact_state, contacts=[contact_event], fracture_events=frame1_fracture, fragments=frame1_fragments),
        frame(2, 0.4, post_state, fragments=frame2_fragments),
    ]


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


def dist(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((a[idx] - b[idx]) ** 2 for idx in range(3)))


def frame(
    frame_id: int,
    time_s: float,
    objects: dict[str, Any],
    *,
    contacts: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    constraints: list[dict[str, Any]] | None = None,
    spring_events: list[dict[str, Any]] | None = None,
    fracture_events: list[dict[str, Any]] | None = None,
    fragments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"frame": frame_id, "time_s": time_s, "objects": objects, "contacts": contacts or []}
    if actions is not None:
        result["actions"] = actions
    if constraints is not None:
        result["constraints"] = constraints
    if spring_events is not None:
        result["spring_events"] = spring_events
    if fracture_events is not None:
        result["fracture_events"] = fracture_events
    if fragments is not None:
        result["fragments"] = fragments
    return result


def contact(a: str, b: str, frame_id: int, time_s: float) -> dict[str, Any]:
    return {"objects": [a, b], "frame": frame_id, "time_s": time_s, "method": "fallback_deterministic_contact"}
