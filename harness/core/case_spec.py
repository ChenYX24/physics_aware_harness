from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.core.prompt_lineage import prompt_stage_text, validate_prompt_lineage


CASE_SPEC_SCHEMA_VERSION = "harness_case_spec_v1"


@dataclass(frozen=True)
class CaseSpec:
    data: dict[str, Any]

    @property
    def case_id(self) -> str:
        return str(self.data["case_id"])

    @property
    def capability_id(self) -> str:
        return str(self.data["capability_id"])

    @property
    def should_pass(self) -> bool:
        return bool(self.data["should_pass"])

    @property
    def objects(self) -> list[dict[str, Any]]:
        return [item for item in self.data.get("objects", []) if isinstance(item, dict)]


def load_case_spec(path: str | Path) -> CaseSpec:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"case spec must be a JSON object: {path}")
    validate_case_spec(data)
    return CaseSpec(data)


def load_case_spec_document(path: str | Path) -> Any:
    """Explicitly dispatch a persisted CaseSpec by schema_version without changing the V1 loader."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"case spec must be a JSON object: {path}")
    if data.get("schema_version") == "harness_case_spec_v2":
        from harness.core.case_spec_v2 import case_spec_v2_from_dict

        return case_spec_v2_from_dict(data)
    validate_case_spec(data)
    return CaseSpec(data)


def validate_case_spec(data: dict[str, Any]) -> None:
    if data.get("schema_version") != CASE_SPEC_SCHEMA_VERSION:
        raise ValueError("case spec schema_version must be harness_case_spec_v1")
    required = [
        "case_id",
        "capability_id",
        "prompt",
        "expected_physics",
        "objects",
        "active_objects",
        "passive_objects",
        "required_assets",
        "required_signals",
        "verifier_expectation",
        "should_pass",
        "notes",
    ]
    for key in required:
        if key not in data:
            raise ValueError(f"case spec missing field: {key}")
    if not isinstance(data["objects"], list) or not data["objects"]:
        raise ValueError("case spec objects must be a non-empty list")
    for key in ("active_objects", "passive_objects", "required_assets", "required_signals"):
        if not isinstance(data[key], list):
            raise ValueError(f"case spec field must be list: {key}")
    if not isinstance(data["should_pass"], bool):
        raise ValueError("case spec should_pass must be boolean")
    lineage = data.get("prompt_lineage")
    if lineage is not None:
        if not isinstance(lineage, dict):
            raise ValueError("case spec prompt_lineage must be an object")
        validate_prompt_lineage(lineage)
        if data["prompt"] != prompt_stage_text(lineage, str(lineage.get("canonical_stage_id") or "")):
            raise ValueError("case spec prompt must equal the canonical prompt lineage stage")
        if data.get("refiner_prompt") != prompt_stage_text(lineage, str(lineage.get("refiner_stage_id") or "")):
            raise ValueError("case spec refiner_prompt must equal the refiner prompt lineage stage")
    if data.get("capability_id") == "sequential_contact_propagation":
        validate_domino_parameter_consistency(data)
    if data.get("capability_id") == "fluid_particle_dynamics":
        validate_fluid_initial_conditions(data)
    if data.get("capability_id") == "agent_rigidbody_action_coupling":
        validate_agent_action_effect(data)
    verification_rules = {str(rule) for rule in data.get("verification_rules") or []}
    if "ballistic_gravity_impact" in verification_rules:
        validate_ballistic_gravity_impact(data)
    if "impact_centered_fracture" in verification_rules:
        validate_impact_centered_fracture(data)
    for obj in data["objects"]:
        if isinstance(obj, dict) and isinstance(obj.get("fracture_response"), dict):
            validate_fracture_energy_response(obj["fracture_response"])


def validate_fluid_initial_conditions(data: dict[str, Any]) -> None:
    """Validate the fluid-source vocabulary without pretending every source is implemented."""
    fluids = [
        obj
        for obj in data.get("objects") or []
        if isinstance(obj, dict) and str(obj.get("role") or "") in {"fluid", "fluid_volume"}
    ]
    if not fluids:
        raise ValueError("fluid_particle_dynamics requires at least one fluid object")
    roles = {str(obj.get("role") or "") for obj in data.get("objects") or [] if isinstance(obj, dict)}
    if {"source_container", "receiver_container"}.intersection(roles):
        validate_container_transfer_contract(data)
    for fluid in fluids:
        initial = fluid.get("initial_condition")
        if initial is None:
            continue  # Legacy box-volume cases remain readable.
        if not isinstance(initial, dict):
            raise ValueError("fluid initial_condition must be an object")
        source_type = str(initial.get("type") or "")
        if source_type not in {"bounded_volume", "container_fill", "emitter"}:
            raise ValueError("fluid initial_condition.type must be bounded_volume, container_fill, or emitter")
        if source_type == "emitter":
            continue
        shape = str(initial.get("shape") or "")
        if source_type == "container_fill" and shape != "box":
            raise ValueError("container_fill currently requires box geometry")
        if shape == "box":
            size = initial.get("size_m", fluid.get("size_m"))
            if not vector3(size) or any(float(value) <= 0.0 for value in size):
                raise ValueError("bounded_volume box requires a positive size_m 3-vector")
        elif shape == "sphere":
            radius = initial.get("radius_m")
            if not isinstance(radius, (int, float)) or not math.isfinite(float(radius)) or float(radius) <= 0.0:
                raise ValueError("bounded_volume sphere requires positive radius_m")
        elif shape == "cylinder":
            radius = initial.get("radius_m")
            height = initial.get("height_m")
            if not isinstance(radius, (int, float)) or not math.isfinite(float(radius)) or float(radius) <= 0.0:
                raise ValueError("bounded_volume cylinder requires positive radius_m")
            if not isinstance(height, (int, float)) or not math.isfinite(float(height)) or float(height) <= 0.0:
                raise ValueError("bounded_volume cylinder requires positive height_m")
            euler = initial.get("euler_deg", [0.0, 0.0, 0.0])
            if not vector3(euler):
                raise ValueError("bounded_volume cylinder euler_deg must be a 3-vector")
        else:
            raise ValueError("fluid initial-condition shape must be box, sphere, or cylinder")
        velocity_field = initial.get("velocity_field")
        if velocity_field is not None:
            validate_fluid_velocity_field(velocity_field)


def validate_container_transfer_contract(data: dict[str, Any]) -> None:
    objects = [obj for obj in data.get("objects") or [] if isinstance(obj, dict)]
    for role in ("source_container", "receiver_container"):
        matches = [obj for obj in objects if str(obj.get("role") or "") == role]
        if len(matches) != 1:
            raise ValueError(f"container transfer requires exactly one {role}")
        container = matches[0]
        asset = container.get("asset") if isinstance(container.get("asset"), dict) else {}
        collision = container.get("collision") if isinstance(container.get("collision"), dict) else {}
        if not str(asset.get("ue_path") or "").startswith("/Game/") or asset.get("proxy") is not False:
            raise ValueError("container transfer requires a non-proxy /Game UE asset")
        digest = str(asset.get("sha256") or "")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
            raise ValueError("container transfer asset requires a sha256 digest")
        if collision.get("type") != "axisymmetric_profile" or collision.get("asset_geometry_match") is not True:
            raise ValueError("container transfer requires an asset-matched axisymmetric_profile collider")
        if int(collision.get("panel_count") or 0) < 12:
            raise ValueError("container transfer collider requires at least 12 wall panels")
        profile = collision.get("inner_profile")
        if not isinstance(profile, list) or len(profile) < 2:
            raise ValueError("container transfer collider requires at least two inner_profile points")
        previous_z = -math.inf
        for point in profile:
            if not isinstance(point, dict):
                raise ValueError("container inner_profile points must be objects")
            z_m = point.get("z_m")
            radius_m = point.get("radius_m")
            if not isinstance(z_m, (int, float)) or not math.isfinite(float(z_m)) or float(z_m) <= previous_z:
                raise ValueError("container inner_profile z_m values must be finite and strictly increasing")
            if not isinstance(radius_m, (int, float)) or not math.isfinite(float(radius_m)) or float(radius_m) <= 0.0:
                raise ValueError("container inner_profile radius_m values must be positive")
            previous_z = float(z_m)
        if not vector3(container.get("solver_rotation_xyz_deg")) or not vector3(container.get("ue_rotation_pyr_deg")):
            raise ValueError("container transfer requires explicit solver XYZ and UE pitch/yaw/roll rotations")
        motion = container.get("kinematic_motion")
        if motion is not None:
            if not isinstance(motion, dict) or motion.get("type") != "tilt":
                raise ValueError("container transfer kinematic_motion must be a tilt object")
            if not vector3(motion.get("solver_end_rotation_xyz_deg")) or not vector3(motion.get("ue_end_rotation_pyr_deg")):
                raise ValueError("container tilt requires solver and UE end rotations")
            if float(motion.get("start_time_s") or -1.0) < 0.0 or float(motion.get("duration_s") or 0.0) <= 0.0:
                raise ValueError("container tilt requires non-negative start_time_s and positive duration_s")
            pivot = motion.get("pivot_local_m")
            if not vector3(pivot):
                raise ValueError("container tilt requires a pivot_local_m 3-vector")
            rim = profile[-1]
            if not math.isclose(float(pivot[2]), float(rim["z_m"]), abs_tol=1e-6):
                raise ValueError("container tilt pivot_local_m must lie at the fitted rim height")
            pivot_radius = math.hypot(float(pivot[0]), float(pivot[1]))
            if not math.isclose(pivot_radius, float(rim["radius_m"]), abs_tol=1e-6):
                raise ValueError("container tilt pivot_local_m must lie on the fitted rim radius")
            landing = motion.get("expected_stream_landing_xy_m")
            if not isinstance(landing, list) or len(landing) != 2 or not all(isinstance(value, (int, float)) for value in landing):
                raise ValueError("container tilt requires expected_stream_landing_xy_m from a solver probe")
            solver_end = motion["solver_end_rotation_xyz_deg"]
            ue_end = motion["ue_end_rotation_pyr_deg"]
            if not (
                math.isclose(float(solver_end[0]), 0.0, abs_tol=1e-6)
                and math.isclose(float(solver_end[2]), 0.0, abs_tol=1e-6)
                and math.isclose(float(ue_end[0]), -float(solver_end[1]), abs_tol=1e-6)
                and math.isclose(float(ue_end[1]), 0.0, abs_tol=1e-6)
                and math.isclose(float(ue_end[2]), 0.0, abs_tol=1e-6)
            ):
                raise ValueError("container solver +Y tilt must map to negative UE pitch")
    fluid = next(obj for obj in objects if str(obj.get("role") or "") in {"fluid", "fluid_volume"})
    initial = fluid.get("initial_condition") if isinstance(fluid.get("initial_condition"), dict) else {}
    if initial.get("frame") != "source_container_local" or not vector3(initial.get("local_position_m")):
        raise ValueError("container transfer fluid requires source_container_local local_position_m")
    bounds = data.get("workspace_bounds_m") if isinstance(data.get("workspace_bounds_m"), dict) else {}
    if not vector3(bounds.get("min_m")) or not vector3(bounds.get("max_m")):
        raise ValueError("container transfer requires workspace_bounds_m min_m/max_m")
    expected = data.get("expected_physics") if isinstance(data.get("expected_physics"), dict) else {}
    evacuation_duration = expected.get("minimum_source_evacuation_duration_s")
    maximum_drop = expected.get("maximum_source_fraction_drop_per_frame")
    if not isinstance(evacuation_duration, (int, float)) or float(evacuation_duration) <= 0.0:
        raise ValueError("container transfer requires positive minimum_source_evacuation_duration_s")
    if not isinstance(maximum_drop, (int, float)) or not 0.0 < float(maximum_drop) <= 1.0:
        raise ValueError("container transfer maximum_source_fraction_drop_per_frame must be in (0, 1]")
    scene = data.get("scene") if isinstance(data.get("scene"), dict) else {}
    support = scene.get("support_surface") if isinstance(scene.get("support_surface"), dict) else {}
    support_asset = support.get("asset") if isinstance(support.get("asset"), dict) else {}
    support_path = str(support_asset.get("ue_path") or "")
    if not support_path.startswith("/Game/") or support_asset.get("proxy") is not False:
        raise ValueError("asset-bound container transfer requires a non-proxy /Game support surface")
    if not vector3(support_asset.get("bbox_m")) or not vector3(support.get("position_m")) or not vector3(support.get("scale")):
        raise ValueError("container transfer support surface requires bbox_m, position_m, and scale")
    floor_to_surface = support.get("solver_floor_to_surface_m")
    if not isinstance(floor_to_surface, (int, float)) or not math.isfinite(float(floor_to_surface)) or float(floor_to_surface) <= 0.0:
        raise ValueError("container transfer support surface requires positive solver_floor_to_surface_m")


def validate_fluid_velocity_field(field: Any) -> None:
    if not isinstance(field, dict):
        raise ValueError("fluid initial_condition.velocity_field must be an object")
    field_type = str(field.get("type") or "")
    if field_type == "uniform":
        if not vector3(field.get("velocity_m_s")):
            raise ValueError("uniform fluid velocity field requires velocity_m_s")
    elif field_type == "swirl_z":
        angular_speed = field.get("angular_speed_rad_s")
        if not isinstance(angular_speed, (int, float)) or not math.isfinite(float(angular_speed)) or float(angular_speed) == 0.0:
            raise ValueError("swirl_z fluid velocity field requires non-zero angular_speed_rad_s")
        if not vector3(field.get("center_m", [0.0, 0.0, 0.0])):
            raise ValueError("swirl_z fluid velocity field center_m must be a 3-vector")
        maximum_speed = field.get("maximum_speed_m_s", math.inf)
        if not isinstance(maximum_speed, (int, float)) or float(maximum_speed) <= 0.0:
            raise ValueError("swirl_z maximum_speed_m_s must be positive")
    else:
        raise ValueError("fluid velocity field type must be uniform or swirl_z")


def validate_fracture_energy_response(response: dict[str, Any]) -> None:
    levels = response.get("energy_response_levels")
    if levels is None:
        return
    if not isinstance(levels, list) or not levels:
        raise ValueError("fracture energy_response_levels must be a non-empty list")
    thresholds: list[float] = []
    states: set[str] = set()
    for level in levels:
        if not isinstance(level, dict):
            raise ValueError("fracture energy response level must be an object")
        state = str(level.get("damage_state") or "")
        if not state or state in states:
            raise ValueError("fracture energy response damage_state must be non-empty and unique")
        states.add(state)
        threshold = float(level.get("minimum_impact_energy_j"))
        if threshold < 0.0:
            raise ValueError("fracture energy response threshold must be non-negative")
        thresholds.append(threshold)
    if thresholds != sorted(thresholds) or len(thresholds) != len(set(thresholds)):
        raise ValueError("fracture energy response thresholds must be strictly increasing")


def fracture_response_for_energy(response: dict[str, Any], impact_energy_j: float) -> dict[str, Any] | None:
    """Return the highest configured fracture response reached by measured incident energy."""
    levels = response.get("energy_response_levels")
    if not isinstance(levels, list):
        return dict(response)
    eligible = [
        level
        for level in levels
        if isinstance(level, dict) and impact_energy_j >= float(level.get("minimum_impact_energy_j") or 0.0)
    ]
    if not eligible:
        return None
    selected = dict(response)
    selected.pop("energy_response_levels", None)
    selected.update(max(eligible, key=lambda level: float(level.get("minimum_impact_energy_j") or 0.0)))
    return selected


def validate_ballistic_gravity_impact(data: dict[str, Any]) -> None:
    """Require a declared gravity arc to reach the planned impact point from initial state only."""
    parameters = data.get("physical_parameters") or {}
    duration = float(parameters.get("ballistic_time_to_impact_s") or 0.0)
    target = parameters.get("target_impact_point_m")
    gravity = parameters.get("gravity_m_s2")
    projectile = next(
        (obj for obj in data.get("objects") or [] if isinstance(obj, dict) and obj.get("role") == "projectile"),
        None,
    )
    if duration <= 0.0 or not vector3(target) or not vector3(gravity) or not projectile:
        raise ValueError("ballistic_gravity_impact requires time, target, gravity, and one projectile")
    if parameters.get("gravity_enabled") is not True or projectile.get("enable_gravity") is not True:
        raise ValueError("ballistic_gravity_impact projectile must enable gravity")
    position = projectile.get("initial_position_m")
    velocity = projectile.get("initial_velocity_m_s")
    if not vector3(position) or not vector3(velocity):
        raise ValueError("ballistic_gravity_impact requires projectile position and velocity")
    predicted = [
        float(position[index]) + float(velocity[index]) * duration + 0.5 * float(gravity[index]) * duration * duration
        for index in range(3)
    ]
    if any(not math.isclose(predicted[index], float(target[index]), abs_tol=1e-6) for index in range(3)):
        raise ValueError(f"ballistic launch does not reach target_impact_point_m: predicted={predicted}")


def validate_impact_centered_fracture(data: dict[str, Any]) -> None:
    brittle = next(
        (
            obj
            for obj in data.get("objects") or []
            if isinstance(obj, dict) and obj.get("role") == "brittle_fracture_body"
        ),
        None,
    )
    response = brittle.get("fracture_response") if brittle else None
    if not isinstance(response, dict):
        raise ValueError("impact_centered_fracture requires a brittle fracture_response")
    if response.get("center_source") != "native_contact_impact_point":
        raise ValueError("impact_centered_fracture must use native_contact_impact_point")
    if response.get("prefracture_pattern") != "radial_voronoi":
        raise ValueError("impact_centered_fracture must declare radial_voronoi topology")


def vector3(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(isinstance(item, (int, float)) for item in value)


def validate_agent_action_effect(data: dict[str, Any]) -> None:
    expected = data.get("expected_physics") if isinstance(data.get("expected_physics"), dict) else {}
    sequence = expected.get("required_action_sequence")
    if sequence is not None and (not isinstance(sequence, list) or not sequence or not all(isinstance(item, str) and item for item in sequence)):
        raise ValueError("required_action_sequence must be a non-empty string list")
    effect = expected.get("object_effect")
    if effect is None:
        return
    if not isinstance(effect, dict):
        raise ValueError("object_effect must be an object")
    for key in ("minimum_lift_height_m", "maximum_final_speed_m_s"):
        value = effect.get(key)
        if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0.0):
            raise ValueError(f"object_effect.{key} must be finite and non-negative")
    goal = effect.get("final_goal_region")
    if goal is not None:
        if not isinstance(goal, dict) or not vector3(goal.get("center_m")) or not vector3(goal.get("half_extent_m")):
            raise ValueError("object_effect.final_goal_region requires center_m and half_extent_m 3-vectors")
        if any(not math.isfinite(float(value)) or float(value) <= 0.0 for value in goal["half_extent_m"]):
            raise ValueError("object_effect.final_goal_region half extents must be finite and positive")
    contact = effect.get("required_final_contact")
    if contact is not None and (not isinstance(contact, list) or len(contact) != 2 or not all(isinstance(item, str) and item for item in contact)):
        raise ValueError("object_effect.required_final_contact must contain two object ids")


def fracture_center_from_contact(contact: dict[str, Any], fallback_cm: list[float]) -> tuple[list[float], str]:
    point = contact.get("impact_point_cm")
    if vector3(point) and all(math.isfinite(float(value)) for value in point):
        return [float(value) for value in point], "native_contact_impact_point"
    return [float(value) for value in fallback_cm], "impactor_actor_location"


def validate_domino_parameter_consistency(data: dict[str, Any]) -> None:
    """Keep the domino sweep labels and the object states they control in lockstep."""
    parameters = data.get("physical_parameters")
    if not isinstance(parameters, dict):
        return
    dominoes = [
        obj for obj in data.get("objects", [])
        if isinstance(obj, dict) and str(obj.get("role") or "").casefold() == "domino"
    ]
    if not dominoes:
        return

    def require_equal(label: str, actual: Any, expected: Any) -> None:
        if actual != expected:
            raise ValueError(f"domino physical_parameters drift from object state: {label}")

    for domino in dominoes:
        if "domino_size_m" in parameters:
            require_equal("domino_size_m", domino.get("size_m"), parameters["domino_size_m"])
        if "domino_mass_kg" in parameters:
            require_equal("domino_mass_kg", domino.get("mass_kg"), parameters["domino_mass_kg"])
        material = domino.get("material") if isinstance(domino.get("material"), dict) else {}
        if "dynamic_friction" in parameters:
            require_equal("dynamic_friction", material.get("dynamic_friction"), parameters["dynamic_friction"])
        if "restitution" in parameters:
            require_equal("restitution", material.get("restitution"), parameters["restitution"])

    if "initial_pitch_deg" in parameters:
        rotation = dominoes[0].get("initial_rotation_deg")
        actual_pitch = rotation[1] if isinstance(rotation, list) and len(rotation) >= 2 else None
        require_equal("initial_pitch_deg", actual_pitch, parameters["initial_pitch_deg"])
    if "domino_spacing_m" in parameters and len(dominoes) > 1:
        expected_spacing = parameters["domino_spacing_m"]
        for previous, current in zip(dominoes, dominoes[1:]):
            previous_position = previous.get("initial_position_m")
            current_position = current.get("initial_position_m")
            actual_spacing = (
                round(float(current_position[0]) - float(previous_position[0]), 9)
                if isinstance(previous_position, list)
                and isinstance(current_position, list)
                and previous_position
                and current_position
                else None
            )
            require_equal("domino_spacing_m", actual_spacing, expected_spacing)
