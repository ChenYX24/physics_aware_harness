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
    if capability_id == "billiard_causality_compiler":
        return billiards_trajectory(case_id, case_spec)
    if capability_id == "sequential_contact_propagation":
        return domino_trajectory(case_id, case_spec)
    if capability_id == "rigid_body_gravity_collision":
        return falling_trajectory(case_id, case_spec)
    if capability_id == "ramp_sliding_friction":
        return ramp_trajectory(case_id, case_spec)
    return []


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


def state(position: list[float], velocity: list[float], *, rotation: list[float] | None = None) -> dict[str, Any]:
    return {"position_m": position, "velocity_m_s": velocity, "rotation_deg": rotation or [0, 0, 0]}


def state_from_spec(obj: dict[str, Any]) -> dict[str, Any]:
    return state(vec3(obj.get("initial_position_m") or [0, 0, 0]), vec3(obj.get("initial_velocity_m_s") or [0, 0, 0]))


def clone_states(states: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {key: {inner_key: list(value) if isinstance(value, list) else value for inner_key, value in state_value.items()} for key, state_value in states.items()}


def midpoint(a: list[float], b: list[float], fraction: float) -> list[float]:
    return [round(a[idx] + (b[idx] - a[idx]) * fraction, 4) for idx in range(3)]


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    return [float(padded[0]), float(padded[1]), float(padded[2])]


def frame(frame_id: int, time_s: float, objects: dict[str, Any], *, contacts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"frame": frame_id, "time_s": time_s, "objects": objects, "contacts": contacts or []}


def contact(a: str, b: str, frame_id: int, time_s: float) -> dict[str, Any]:
    return {"objects": [a, b], "frame": frame_id, "time_s": time_s, "method": "fallback_deterministic_contact"}
