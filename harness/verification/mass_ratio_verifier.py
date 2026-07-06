from __future__ import annotations

import math
from typing import Any


SPEED_EPS = 0.05


def verify_mass_ratio(case_spec: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    if not trajectory:
        return "F1_missing_trajectory", failure("trajectory", 0, 0, "frame_count", 0), evidence

    active_ids = [str(item) for item in case_spec.get("active_objects", [])]
    passive_ids = [str(item) for item in case_spec.get("passive_objects", [])]
    if not active_ids or not passive_ids:
        return "F7_runtime_artifact_incomplete", failure("case_spec", 0, 0, "active_passive_sets", 0), evidence
    active_id = active_ids[0]
    passive_id = passive_ids[0]
    objects = {str(obj.get("id")): obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)}
    active_obj = objects.get(active_id) or {}
    passive_obj = objects.get(passive_id) or {}
    m_active = positive_mass(active_obj)
    m_passive = positive_mass(passive_obj)
    if m_active is None:
        return "F3_invalid_initial_physics_state", failure(active_id, 0, 0, "mass_kg", active_obj.get("mass_kg")), evidence
    if m_passive is None:
        return "F3_invalid_initial_physics_state", failure(passive_id, 0, 0, "mass_kg", passive_obj.get("mass_kg")), evidence

    contacts = all_contacts(trajectory)
    if not contacts:
        return "F2_missing_contact_events", failure("contact_events", 0, 0, "contact_count", 0), evidence
    if not contact_pair_exists(contacts, active_id, passive_id):
        return "F2_missing_contact_events", failure(f"{active_id}:{passive_id}", 0, 0, "missing_expected_collision_edge", [active_id, passive_id]), evidence

    first = trajectory[0]
    first_objects = frame_objects(first)
    active_initial_state = first_objects.get(active_id) or {}
    passive_initial_state = first_objects.get(passive_id) or {}
    active_initial_velocity = vec3(active_initial_state.get("velocity_m_s"))
    passive_initial_speed = norm(passive_initial_state.get("velocity_m_s"))
    if passive_initial_speed > SPEED_EPS:
        return "F5_passive_precontact_motion", failure(passive_id, frame_id(first), frame_time(first), "velocity_m_s", round(passive_initial_speed, 6)), evidence

    expected = dict(case_spec.get("expected_physics") or {})
    axis_sign = -1.0 if str(expected.get("collision_axis") or "+x").startswith("-") else 1.0
    initial_axis_speed = axis_sign * active_initial_velocity[0]
    if initial_axis_speed <= SPEED_EPS:
        return "F3_invalid_initial_physics_state", failure(active_id, frame_id(first), frame_time(first), "initial_axis_speed_m_s", round(initial_axis_speed, 6)), evidence

    contact_index = first_contact_index(trajectory, active_id, passive_id)
    post_frame = trajectory[-1] if contact_index is None else trajectory[contact_index]
    post_objects = frame_objects(post_frame)
    active_post_velocity = vec3((post_objects.get(active_id) or {}).get("velocity_m_s"))
    passive_post_velocity = vec3((post_objects.get(passive_id) or {}).get("velocity_m_s"))
    active_post_axis_speed = axis_sign * active_post_velocity[0]
    passive_post_axis_speed = axis_sign * passive_post_velocity[0]
    if passive_post_axis_speed <= SPEED_EPS:
        return "F4_causality_violation", failure(passive_id, frame_id(post_frame), frame_time(post_frame), "target_post_collision_axis_speed_m_s", round(passive_post_axis_speed, 6)), evidence

    target_min = float(expected.get("expected_target_speed_min_m_s") or 0.0)
    target_max = float(expected.get("expected_target_speed_max_m_s") or 1000.0)
    striker_abs_max = float(expected.get("expected_striker_speed_abs_max_m_s") or 1000.0)
    if passive_post_axis_speed < target_min:
        return "F4_causality_violation", failure(passive_id, frame_id(post_frame), frame_time(post_frame), "target_speed_too_slow_m_s", round(passive_post_axis_speed, 6)), evidence
    if passive_post_axis_speed > target_max:
        return "F4_causality_violation", failure(passive_id, frame_id(post_frame), frame_time(post_frame), "target_speed_too_fast_m_s", round(passive_post_axis_speed, 6)), evidence
    if abs(active_post_axis_speed) > striker_abs_max:
        return "F4_causality_violation", failure(active_id, frame_id(post_frame), frame_time(post_frame), "striker_post_speed_abs_m_s", round(abs(active_post_axis_speed), 6)), evidence

    order = str(expected.get("expected_velocity_order") or "")
    if order == "target_faster_than_striker" and passive_post_axis_speed <= abs(active_post_axis_speed) + SPEED_EPS:
        return "F4_causality_violation", failure(passive_id, frame_id(post_frame), frame_time(post_frame), "target_speed_too_slow_m_s", round(passive_post_axis_speed, 6)), evidence
    if order == "target_slower_than_initial" and passive_post_axis_speed >= initial_axis_speed - SPEED_EPS:
        return "F4_causality_violation", failure(passive_id, frame_id(post_frame), frame_time(post_frame), "target_speed_too_fast_m_s", round(passive_post_axis_speed, 6)), evidence

    initial_energy = kinetic_energy(m_active, active_initial_velocity) + kinetic_energy(m_passive, vec3(passive_initial_state.get("velocity_m_s")))
    post_energy = kinetic_energy(m_active, active_post_velocity) + kinetic_energy(m_passive, passive_post_velocity)
    energy_ratio = post_energy / initial_energy if initial_energy > 1e-9 else 0.0
    max_energy_ratio = float(expected.get("expected_energy_ratio_max") or 1.05)
    if energy_ratio > max_energy_ratio:
        return "F4_causality_violation", failure("collision_pair", frame_id(post_frame), frame_time(post_frame), "energy_ratio", round(energy_ratio, 6)), evidence

    evidence.append(
        {
            "active_object_id": active_id,
            "passive_object_id": passive_id,
            "active_mass_kg": round(m_active, 6),
            "passive_mass_kg": round(m_passive, 6),
            "initial_axis_speed_m_s": round(initial_axis_speed, 6),
            "active_post_axis_speed_m_s": round(active_post_axis_speed, 6),
            "passive_post_axis_speed_m_s": round(passive_post_axis_speed, 6),
            "energy_ratio": round(energy_ratio, 6),
            "expected_velocity_order": order,
        }
    )
    return None, None, evidence


def positive_mass(obj: dict[str, Any]) -> float | None:
    value = obj.get("mass_kg")
    if value is None:
        return None
    mass = float(value)
    return mass if mass > 0 else None


def all_contacts(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [contact for frame in trajectory for contact in frame.get("contacts") or [] if isinstance(contact, dict)]


def contact_pair_exists(contacts: list[dict[str, Any]], a: str, b: str) -> bool:
    expected = {a, b}
    return any(expected.issubset({str(item) for item in contact.get("objects") or []}) for contact in contacts)


def first_contact_index(trajectory: list[dict[str, Any]], a: str, b: str) -> int | None:
    expected = {a, b}
    for index, frame in enumerate(trajectory):
        for contact in frame.get("contacts") or []:
            if expected.issubset({str(item) for item in contact.get("objects") or []}):
                return index
    return None


def frame_objects(frame: dict[str, Any]) -> dict[str, dict[str, Any]]:
    objects = frame.get("objects")
    return objects if isinstance(objects, dict) else {}


def kinetic_energy(mass: float, velocity: list[float]) -> float:
    speed_sq = sum(item * item for item in velocity)
    return 0.5 * mass * speed_sq


def norm(value: Any) -> float:
    return math.sqrt(sum(item * item for item in vec3(value)))


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    return [float(padded[0]), float(padded[1]), float(padded[2])]


def frame_id(frame: dict[str, Any]) -> int:
    return int(frame.get("frame") or 0)


def frame_time(frame: dict[str, Any]) -> float:
    return float(frame.get("time_s") or frame.get("time") or 0.0)


def failure(object_id: str, frame: int, time: float, metric: str, value: Any) -> dict[str, Any]:
    return {"object_id": object_id, "frame": frame, "time": time, "metric": metric, "value": value}
