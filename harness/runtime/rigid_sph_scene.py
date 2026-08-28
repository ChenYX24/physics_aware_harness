from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from harness.runtime.rigid_sph_configuration import compile_rigid_sph_solver_configuration, rigid_sph_parameters


PORTABLE_COLLISION_MESH_SCHEMA_VERSION = "harness_portable_collision_mesh_v1"


class RigidSphCapabilityMissing(ValueError):
    """The declared rigid/SPH scene cannot be represented without changing its physics truth."""


def compile_rigid_sph_scene(
    case_spec: dict[str, Any],
    solver_configuration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile declarations into one backend-neutral rigid/SPH execution contract."""
    scene = case_spec.get("solver_scene") if isinstance(case_spec.get("solver_scene"), dict) else {}
    if scene.get("type") != "rigid_sph":
        raise ValueError("solver_scene.type must be rigid_sph")
    objects = [item for item in case_spec.get("objects") or [] if isinstance(item, dict)]
    rigid_bodies = [compile_rigid_body(item) for item in objects if item.get("role") == "rigid_body"]
    if not rigid_bodies:
        raise ValueError("rigid_sph scene requires at least one rigid_body")
    body_by_id = {item["id"]: item for item in rigid_bodies}
    if len(body_by_id) != len(rigid_bodies):
        raise ValueError("rigid_body ids must be unique")
    fluids = [item for item in objects if item.get("role") in {"fluid", "fluid_volume"}]
    if len(fluids) != 1:
        raise ValueError("rigid_sph scene currently requires exactly one fluid")
    if solver_configuration is None:
        validation_case = dict(case_spec)
        validation_scene = dict(case_spec.get("scene") or {})
        validation_scene.setdefault("duration_s", 1.0)
        validation_case["scene"] = validation_scene
        solver_configuration = compile_rigid_sph_solver_configuration(validation_case)
    parameters = rigid_sph_parameters(solver_configuration)
    particle_size_m = positive(parameters.get("particle_size_m"), "solver_configuration.parameters.particle_size_m")
    fluid = compile_fluid(fluids[0], body_by_id, particle_radius_m=particle_size_m / 2.0)
    workspace = case_spec.get("workspace_bounds_m")
    if not isinstance(workspace, dict):
        raise ValueError("rigid_sph scene requires workspace_bounds_m")
    minimum = vec3(workspace.get("min_m"), "workspace_bounds_m.min_m")
    maximum = vec3(workspace.get("max_m"), "workspace_bounds_m.max_m")
    if any(minimum[index] >= maximum[index] for index in range(3)):
        raise ValueError("workspace bounds must have min < max on every axis")
    measurements = compile_measurements(scene.get("measurements"), body_by_id)
    assertions = compile_assertions(scene.get("assertions"), {item["id"] for item in measurements})
    initialization = compile_initialization(scene.get("initialization"))
    return {
        "schema_version": "harness_rigid_sph_scene_v1",
        "execution_contract": "rigid_sph_scene",
        "rigid_bodies": rigid_bodies,
        "fluid": fluid,
        "measurements": measurements,
        "assertions": assertions,
        "initialization": initialization,
        "workspace_bounds_m": {"min_m": minimum, "max_m": maximum},
        "solver_configuration": solver_configuration,
    }


def compile_initialization(value: Any) -> dict[str, Any]:
    if value is None:
        return {"state": "as_authored", "pre_roll_s": 0.0, "capture_after_pre_roll": False, "declared": False}
    if not isinstance(value, dict):
        raise ValueError("solver_scene.initialization must be an object")
    state = str(value.get("state") or "")
    if state not in {"as_authored", "settled"}:
        raise ValueError("solver_scene.initialization.state must be as_authored or settled")
    pre_roll_s = finite(value.get("pre_roll_s"), "solver initialization pre_roll_s")
    capture_after_pre_roll = value.get("capture_after_pre_roll")
    if pre_roll_s < 0.0 or not isinstance(capture_after_pre_roll, bool):
        raise ValueError("solver initialization requires non-negative pre_roll_s and boolean capture_after_pre_roll")
    if state == "settled" and (pre_roll_s <= 0.0 or capture_after_pre_roll is not True):
        raise ValueError("settled initialization requires positive pre_roll_s and capture_after_pre_roll=true")
    return {
        "state": state,
        "pre_roll_s": pre_roll_s,
        "capture_after_pre_roll": capture_after_pre_roll,
        "declared": True,
    }


def evaluate_measurements(
    positions: list[list[float]],
    bodies: dict[str, dict[str, Any]],
    rigid_states: dict[str, dict[str, Any]],
    definitions: list[dict[str, Any]],
) -> dict[str, float]:
    total = max(1, len(positions))
    result: dict[str, float] = {}
    for definition in definitions:
        kind = definition["type"]
        if kind == "body_interior_fraction":
            value = sum(point_inside_profile(row, bodies[definition["body_id"]]) for row in positions) / total
        elif kind == "outside_body_interiors_fraction":
            selected = [bodies[body_id] for body_id in definition["body_ids"]]
            value = sum(not any(point_inside_profile(row, body) for body in selected) for row in positions) / total
        elif kind == "plane_proximity_fraction":
            collision = bodies[definition["body_id"]]["collision"]
            normal_length = math.sqrt(sum(float(component) ** 2 for component in collision["normal"]))
            normal = [float(component) / normal_length for component in collision["normal"]]
            origin = collision["position_m"]
            distance = float(definition["distance_m"])
            value = sum(
                abs(sum((float(row[axis]) - float(origin[axis])) * normal[axis] for axis in range(3))) <= distance
                for row in positions
            ) / total
        elif kind == "axis_span":
            axis_indices = {"x": 0, "y": 1, "z": 2}
            value = max(
                max(float(row[axis_indices[axis]]) for row in positions)
                - min(float(row[axis_indices[axis]]) for row in positions)
                for axis in definition["axes"]
            ) if positions else 0.0
        else:
            state = rigid_states[definition["body_id"]]
            vector = [float(component) for component in state[definition["field"]]]
            component = definition["component"]
            value = math.sqrt(sum(item * item for item in vector)) if component == "magnitude" else vector[{"x": 0, "y": 1, "z": 2}[component]]
        result[definition["id"]] = value
    return result


def compile_rigid_body(body: dict[str, Any]) -> dict[str, Any]:
    solver = body.get("solver") if isinstance(body.get("solver"), dict) else {}
    asset = body.get("asset") if isinstance(body.get("asset"), dict) else {}
    collision = solver.get("collision") if isinstance(solver.get("collision"), dict) else {}
    body_id = str(body.get("id") or "")
    if not body_id:
        raise ValueError("rigid_body requires a non-empty id")
    ue_path = str(asset.get("ue_path") or "")
    asset_hash = str(asset.get("sha256") or "")
    if not ue_path.startswith("/Game/") or "." not in ue_path:
        raise ValueError("rigid_body asset must declare a full /Game UE object path")
    if len(asset_hash) != 64 or any(character not in "0123456789abcdef" for character in asset_hash.lower()):
        raise ValueError("rigid_body asset sha256 must be a 64-character hex digest")
    if asset.get("proxy") is not False:
        raise ValueError("rigid_sph scene requires non-proxy UE assets")
    mobility = str(solver.get("mobility") or "")
    if mobility not in {"static", "kinematic", "dynamic"}:
        raise ValueError("rigid_body solver.mobility must be static, kinematic, or dynamic")
    transform_raw = solver.get("transform") if isinstance(solver.get("transform"), dict) else {}
    solver_rotation = vec3(transform_raw.get("euler_xyz_deg"), "rigid_body transform.euler_xyz_deg")
    declared_ue_rotation = vec3(
        transform_raw.get("ue_rotation_pyr_deg"),
        "rigid_body transform.ue_rotation_pyr_deg",
    )
    ue_rotation = ue_rotation_pyr_from_solver_xyz(solver_rotation)
    require_matching_ue_rotation(declared_ue_rotation, ue_rotation, "rigid_body transform")
    transform = {
        "position_m": vec3(transform_raw.get("position_m"), "rigid_body transform.position_m"),
        "euler_xyz_deg": solver_rotation,
        "ue_rotation_pyr_deg": ue_rotation,
        "scale": vec3(transform_raw.get("scale", [1.0, 1.0, 1.0]), "rigid_body transform.scale"),
    }
    if any(value <= 0.0 for value in transform["scale"]):
        raise ValueError("rigid_body transform.scale values must be positive")
    compiled_collision = compile_collision(collision, transform, asset)
    if mobility == "dynamic" and compiled_collision["type"] != "asset":
        raise RigidSphCapabilityMissing("dynamic rigid_sph bodies require a qualified asset collision mesh")
    compiled_motion = compile_motion(solver.get("motion"), transform, mobility)
    material = dict(body.get("material") or {})
    density_kg_m3 = material.get("density_kg_m3")
    if density_kg_m3 is not None:
        density_kg_m3 = positive(density_kg_m3, "rigid_body material.density_kg_m3")
    mass_kg = body.get("mass_kg")
    if mobility == "dynamic":
        mass_kg = positive(mass_kg, "dynamic rigid_body mass_kg")
    elif mass_kg is not None:
        mass_kg = positive(mass_kg, "rigid_body mass_kg")
    return {
        "id": body_id,
        "role": "rigid_body",
        "mobility": mobility,
        "asset": {
            "ue_path": ue_path,
            "material_path": str(asset.get("material_path") or ""),
            "sha256": asset_hash.lower(),
            "proxy": False,
            "catalog_source": str(asset.get("catalog_source") or ""),
            "bbox_m": vec3(asset.get("bbox_m"), "rigid_body asset bbox_m"),
            "geometry_registration": dict(asset.get("geometry_registration") or {}),
            "support_registration": dict(asset.get("support_registration") or {}),
        },
        "transform": transform,
        "mass_kg": mass_kg,
        "material": {
            **material,
            **({"density_kg_m3": density_kg_m3} if density_kg_m3 is not None else {}),
        },
        "initial_linear_velocity_m_s": vec3(
            body.get("initial_velocity_m_s", [0.0, 0.0, 0.0]),
            "rigid_body initial_velocity_m_s",
        ),
        "initial_angular_velocity_rad_s": vec3(
            body.get("initial_angular_velocity_rad_s", [0.0, 0.0, 0.0]),
            "rigid_body initial_angular_velocity_rad_s",
        ),
        "motion": compiled_motion,
        "collision": compiled_collision,
    }


def compile_collision(
    collision: dict[str, Any],
    transform: dict[str, Any],
    asset: dict[str, Any],
) -> dict[str, Any]:
    collision_type = str(collision.get("type") or "")
    if collision_type == "asset":
        qualification = asset.get("collision") if isinstance(asset.get("collision"), dict) else {}
        collision_kind = str(qualification.get("kind") or "").strip()
        if qualification.get("present") is not True or not collision_kind:
            raise RigidSphCapabilityMissing("asset collision is not qualified by the Catalog")
        portable = qualification.get("portable_mesh") if isinstance(qualification.get("portable_mesh"), dict) else {}
        artifact_path = Path(str(portable.get("local_path") or "")).expanduser()
        artifact_sha256 = str(portable.get("sha256") or "").lower()
        if portable.get("schema_version") != PORTABLE_COLLISION_MESH_SCHEMA_VERSION:
            raise RigidSphCapabilityMissing("Catalog qualification has no supported portable collision mesh artifact")
        if portable.get("format") != "obj" or portable.get("coordinate_system") != "asset_local_z_up_m":
            raise RigidSphCapabilityMissing("Genesis cannot consume the qualified portable collision mesh representation")
        if portable.get("materialized") is not True or not artifact_path.is_file():
            raise RigidSphCapabilityMissing(f"qualified portable collision mesh is unavailable: {artifact_path}")
        if (
            not is_sha256(artifact_sha256)
            or sha256_file(artifact_path) != artifact_sha256
            or not isinstance(portable.get("byte_size"), int)
            or isinstance(portable.get("byte_size"), bool)
            or portable.get("byte_size") != artifact_path.stat().st_size
        ):
            raise RigidSphCapabilityMissing("qualified portable collision mesh identity does not match its Catalog record")
        transform = portable.get("artifact_to_asset_transform")
        matrix = transform.get("matrix4x4") if isinstance(transform, dict) else None
        if not identity_matrix4x4(matrix):
            raise RigidSphCapabilityMissing(
                "Genesis cannot consume a portable collision artifact whose transform was not baked to asset-local space"
            )
        return {
            "type": "asset",
            "asset_geometry_match": True,
            "catalog_collision_kind": collision_kind,
            "portable_mesh_path": str(artifact_path.resolve()),
            "portable_mesh_sha256": artifact_sha256,
            "portable_mesh_schema_version": PORTABLE_COLLISION_MESH_SCHEMA_VERSION,
            "coordinate_system": "asset_local_z_up_m",
            "artifact_to_asset_transform": {"matrix4x4": matrix},
            "backend_conversion": "catalog_portable_collision_mesh_v1_to_genesis_mesh",
            "convexify": True,
            "geometry_registration": dict(collision.get("geometry_registration") or {}),
        }
    if collision_type == "plane":
        normal = vec3(collision.get("normal"), "plane normal")
        if math.sqrt(sum(value * value for value in normal)) <= 1e-12:
            raise ValueError("plane normal must be non-zero")
        if collision.get("asset_geometry_match") is not True:
            raise ValueError("plane collision must be explicitly fitted to the render asset")
        return {
            "type": "plane",
            "position_m": vec3(collision.get("position_m"), "plane position_m"),
            "normal": normal,
            "asset_geometry_match": True,
            "geometry_registration": dict(collision.get("geometry_registration") or {}),
        }
    if collision_type != "axisymmetric_profile":
        raise ValueError(f"unsupported rigid_sph collision type: {collision_type}")
    if collision.get("asset_geometry_match") is not True:
        raise ValueError("axisymmetric_profile must be explicitly fitted to the render asset")
    fit_method = str(collision.get("fit_method") or "").strip()
    if not fit_method:
        raise ValueError("axisymmetric_profile requires a non-empty fit_method identifying its geometry evidence")
    panel_count = int(collision.get("panel_count") or 0)
    if panel_count < 12:
        raise ValueError("axisymmetric_profile requires at least 12 wall panels")
    profile = compile_inner_profile(collision.get("inner_profile"))
    bottom_z = profile[0]["z_m"]
    bottom_radius = profile[0]["radius_m"]
    rim_z = profile[-1]["z_m"]
    rim_radius = profile[-1]["radius_m"]
    thickness = positive(collision.get("wall_thickness_m"), "wall_thickness_m")
    if rim_z <= bottom_z:
        raise ValueError("axisymmetric profile end must be above its start")
    rotation = rotation_matrix_xyz(transform["euler_xyz_deg"])
    parts = profile_collision_parts(
        transform["position_m"],
        rotation,
        profile,
        thickness,
        panel_count,
    )
    return {
        "type": "axisymmetric_profile",
        "asset_geometry_match": True,
        "fit_method": fit_method,
        "inner_bottom_radius_m": bottom_radius,
        "inner_rim_radius_m": rim_radius,
        "inner_bottom_z_m": bottom_z,
        "inner_rim_z_m": rim_z,
        "inner_profile": profile,
        "wall_thickness_m": thickness,
        "panel_count": panel_count,
        "parts": parts,
        "geometry_registration": dict(collision.get("geometry_registration") or {}),
    }


def is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def identity_matrix4x4(value: Any) -> bool:
    expected = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    return bool(
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(row, list) and len(row) == 4 for row in value)
        and all(
            isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and math.isfinite(float(actual))
            and abs(float(actual) - target) <= 1e-12
            for row, expected_row in zip(value, expected, strict=True)
            for actual, target in zip(row, expected_row, strict=True)
        )
    )


def compile_motion(value: Any, transform: dict[str, Any], mobility: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if mobility != "kinematic" or not isinstance(value, dict) or value.get("type") != "pivot_rotation":
        raise ValueError("rigid_body motion must be pivot_rotation on a kinematic body")
    solver_end_rotation = vec3(value.get("solver_end_rotation_xyz_deg"), "motion solver end rotation")
    declared_ue_end_rotation = vec3(value.get("ue_end_rotation_pyr_deg"), "motion UE end rotation")
    ue_end_rotation = ue_rotation_pyr_from_solver_xyz(solver_end_rotation)
    require_matching_ue_rotation(declared_ue_end_rotation, ue_end_rotation, "motion end rotation")
    motion = {
        "type": "pivot_rotation",
        "start_time_s": finite(value.get("start_time_s"), "motion start_time_s"),
        "duration_s": positive(value.get("duration_s"), "motion duration_s"),
        "pivot_local_m": vec3(value.get("pivot_local_m"), "motion pivot_local_m"),
        "solver_start_rotation_xyz_deg": list(transform["euler_xyz_deg"]),
        "solver_end_rotation_xyz_deg": solver_end_rotation,
        "ue_start_rotation_pyr_deg": list(transform["ue_rotation_pyr_deg"]),
        "ue_end_rotation_pyr_deg": ue_end_rotation,
    }
    motion["pivot_world_m"] = add(
        transform["position_m"],
        matrix_vector(rotation_matrix_xyz(transform["euler_xyz_deg"]), motion["pivot_local_m"]),
    )
    return motion


def compile_fluid(
    fluid: dict[str, Any],
    bodies: dict[str, dict[str, Any]],
    *,
    particle_radius_m: float,
) -> dict[str, Any]:
    solver = fluid.get("solver") if isinstance(fluid.get("solver"), dict) else {}
    initial = solver.get("initial_volume") if isinstance(solver.get("initial_volume"), dict) else {}
    if solver.get("material_model") != "sph_liquid" or initial.get("shape") != "cylinder":
        raise ValueError("rigid_sph currently supports sph_liquid with a cylindrical initial_volume")
    frame = initial.get("frame") if isinstance(initial.get("frame"), dict) else {}
    frame_type = str(frame.get("type") or "")
    local_position = vec3(initial.get("position_m"), "fluid initial position")
    local_rotation = rotation_matrix_xyz(
        vec3(initial.get("euler_xyz_deg", [0.0, 0.0, 0.0]), "fluid initial euler")
    )
    rotation = local_rotation
    radius_m = positive(initial.get("radius_m"), "fluid radius_m")
    height_m = positive(initial.get("height_m"), "fluid height_m")
    if frame_type == "body_local":
        body_id = str(frame.get("body_id") or "")
        if body_id not in bodies:
            raise ValueError(f"fluid initial frame references unknown rigid_body: {body_id}")
        body = bodies[body_id]
        body_rotation = rotation_matrix_xyz(body["transform"]["euler_xyz_deg"])
        world_position = add(body["transform"]["position_m"], matrix_vector(body_rotation, local_position))
        rotation = matrix_multiply(body_rotation, rotation)
        if body["collision"]["type"] == "axisymmetric_profile":
            require_initial_cylinder_clearance(
                local_position,
                local_rotation,
                radius_m,
                height_m,
                body["collision"]["inner_profile"],
                particle_radius_m,
            )
    elif frame_type == "world":
        body_id = None
        world_position = local_position
    else:
        raise ValueError("fluid initial_volume.frame.type must be body_local or world")
    return {
        "id": str(fluid.get("id") or "fluid"),
        "material_model": "sph_liquid",
        "shape": "cylinder",
        "radius_m": radius_m,
        "height_m": height_m,
        "frame": {"type": frame_type, "body_id": body_id},
        "world_position_m": world_position,
        "world_quaternion_wxyz": quaternion_from_matrix(rotation),
        "initial_velocity_m_s": [0.0, 0.0, 0.0],
    }


def ue_rotation_pyr_from_solver_xyz(euler_xyz_deg: list[float]) -> list[float]:
    """Map Genesis RH XYZ Euler components to UE Rotator pitch/yaw/roll."""
    x_deg, y_deg, z_deg = [float(value) for value in euler_xyz_deg]
    return [-y_deg, -z_deg, x_deg]


def require_matching_ue_rotation(declared: list[float], expected: list[float], label: str) -> None:
    if any(abs(angle_delta_deg(actual, target)) > 1e-6 for actual, target in zip(declared, expected, strict=True)):
        raise ValueError(f"{label} UE rotation must equal [-solver_y, -solver_z, solver_x]")


def angle_delta_deg(left: float, right: float) -> float:
    return (float(left) - float(right) + 180.0) % 360.0 - 180.0


def require_initial_cylinder_clearance(
    center_local_m: list[float],
    rotation: list[list[float]],
    radius_m: float,
    height_m: float,
    profile: list[dict[str, float]],
    clearance_m: float,
) -> None:
    axis = matrix_vector(rotation, [0.0, 0.0, 1.0])
    radial_axis = math.hypot(axis[0], axis[1])
    half_height = height_m / 2.0
    radial_extent = math.hypot(center_local_m[0], center_local_m[1]) + radius_m + half_height * radial_axis
    vertical_extent = half_height * abs(axis[2]) + radius_m * radial_axis
    minimum_z = center_local_m[2] - vertical_extent
    maximum_z = center_local_m[2] + vertical_extent
    bottom_z = float(profile[0]["z_m"])
    rim_z = float(profile[-1]["z_m"])
    if minimum_z < bottom_z + clearance_m or maximum_z > rim_z - clearance_m:
        raise ValueError("body-local fluid cylinder must clear the container bottom and rim by at least one particle radius")
    candidate_z = [minimum_z, maximum_z]
    candidate_z.extend(float(point["z_m"]) for point in profile if minimum_z < float(point["z_m"]) < maximum_z)
    minimum_radius = min(profile_radius_at(profile, z_m) for z_m in candidate_z)
    if radial_extent > minimum_radius - clearance_m:
        raise ValueError("body-local fluid cylinder must clear the container wall by at least one particle radius")


def profile_radius_at(profile: list[dict[str, float]], z_m: float) -> float:
    for lower, upper in zip(profile, profile[1:]):
        lower_z = float(lower["z_m"])
        upper_z = float(upper["z_m"])
        if lower_z <= z_m <= upper_z:
            fraction = (z_m - lower_z) / (upper_z - lower_z)
            return float(lower["radius_m"]) + fraction * (float(upper["radius_m"]) - float(lower["radius_m"]))
    raise ValueError("fluid cylinder vertical bounds lie outside the container profile")


def compile_measurements(value: Any, bodies: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("rigid_sph scene requires measurements")
    result = []
    for item in value:
        if not isinstance(item, dict) or not str(item.get("id") or ""):
            raise ValueError("each measurement requires an id")
        kind = str(item.get("type") or "")
        compiled = {"id": str(item["id"]), "type": kind}
        if kind == "body_interior_fraction":
            body_id = str(item.get("body_id") or "")
            if body_id not in bodies or bodies[body_id]["collision"]["type"] != "axisymmetric_profile":
                raise ValueError("body_interior_fraction requires an axisymmetric_profile body_id")
            compiled["body_id"] = body_id
        elif kind == "outside_body_interiors_fraction":
            body_ids = [str(body_id) for body_id in item.get("body_ids") or []]
            if not body_ids or any(body_id not in bodies for body_id in body_ids):
                raise ValueError("outside_body_interiors_fraction requires known body_ids")
            compiled["body_ids"] = body_ids
        elif kind == "plane_proximity_fraction":
            body_id = str(item.get("body_id") or "")
            if body_id not in bodies or bodies[body_id]["collision"]["type"] != "plane":
                raise ValueError("plane_proximity_fraction requires a plane body_id")
            compiled.update({"body_id": body_id, "distance_m": positive(item.get("distance_m"), "plane measurement distance_m")})
        elif kind == "axis_span":
            axes = [str(axis) for axis in item.get("axes") or []]
            if not axes or any(axis not in {"x", "y", "z"} for axis in axes):
                raise ValueError("axis_span requires axes drawn from x/y/z")
            compiled["axes"] = axes
        elif kind == "rigid_body_state":
            body_id = str(item.get("body_id") or "")
            field = str(item.get("field") or "")
            component = str(item.get("component") or "")
            if body_id not in bodies:
                raise ValueError("rigid_body_state requires a known body_id")
            if field not in {"position_m", "linear_velocity_m_s", "angular_velocity_rad_s"}:
                raise ValueError(
                    "rigid_body_state field must be position_m, linear_velocity_m_s, or angular_velocity_rad_s"
                )
            if component not in {"x", "y", "z", "magnitude"}:
                raise ValueError("rigid_body_state component must be x, y, z, or magnitude")
            compiled.update({"body_id": body_id, "field": field, "component": component})
        else:
            raise ValueError(f"unsupported rigid_sph measurement type: {kind}")
        result.append(compiled)
    if len({item["id"] for item in result}) != len(result):
        raise ValueError("measurement ids must be unique")
    return result


def compile_assertions(value: Any, measurement_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("rigid_sph scene requires assertions")
    result = []
    for item in value:
        if not isinstance(item, dict) or str(item.get("measurement_id") or "") not in measurement_ids:
            raise ValueError("assertion measurement_id must reference a declared measurement")
        reduction = str(item.get("reduction") or "")
        if reduction not in {"initial", "final", "max", "min", "initial_minus_final", "max_frame_decrease", "threshold_crossing_duration"}:
            raise ValueError(f"unsupported assertion reduction: {reduction}")
        operator = str(item.get("operator") or "")
        if operator not in {">=", "<="}:
            raise ValueError("assertion operator must be >= or <=")
        compiled = {
            "id": str(item.get("id") or f"assertion_{len(result)}"),
            "measurement_id": str(item["measurement_id"]),
            "reduction": reduction,
            "operator": operator,
            "value": finite(item.get("value"), "assertion value"),
        }
        if reduction == "threshold_crossing_duration":
            compiled["start_delta"] = positive(item.get("start_delta", 0.01), "assertion start_delta")
            compiled["end_value"] = finite(item.get("end_value", 0.01), "assertion end_value")
        result.append(compiled)
    if len({item["id"] for item in result}) != len(result):
        raise ValueError("assertion ids must be unique")
    return result


def compile_inner_profile(value: Any) -> list[dict[str, float]]:
    if not isinstance(value, list) or len(value) < 2:
        raise ValueError("axisymmetric collision requires at least two inner_profile points")
    profile: list[dict[str, float]] = []
    for point in value:
        if not isinstance(point, dict):
            raise ValueError("axisymmetric inner_profile points must be objects")
        z_m = finite(point.get("z_m"), "inner_profile z_m")
        radius_m = positive(point.get("radius_m"), "inner_profile radius_m")
        if profile and z_m <= profile[-1]["z_m"]:
            raise ValueError("axisymmetric inner_profile z_m values must be strictly increasing")
        profile.append({"z_m": z_m, "radius_m": radius_m})
    return profile


def profile_collision_parts(
    base_position: list[float],
    rotation: list[list[float]],
    profile: list[dict[str, float]],
    thickness: float,
    panel_count: int,
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for lower, upper in zip(profile, profile[1:]):
        segment = frustum_collision_parts(
            base_position,
            rotation,
            lower["radius_m"],
            upper["radius_m"],
            lower["z_m"],
            upper["z_m"],
            thickness,
            panel_count,
        )
        parts.extend(segment[:panel_count])
    bottom = profile[0]
    bottom_position = add(
        base_position,
        matrix_vector(rotation, [0.0, 0.0, bottom["z_m"] - thickness / 2.0]),
    )
    parts.append(
        {
            "kind": "cylinder",
            "position_m": bottom_position,
            "quaternion_wxyz": quaternion_from_matrix(rotation),
            "radius_m": bottom["radius_m"] + thickness * 1.5,
            "height_m": thickness,
        }
    )
    return parts


def frustum_collision_parts(
    base_position: list[float],
    rotation: list[list[float]],
    bottom_radius: float,
    rim_radius: float,
    bottom_z: float,
    rim_z: float,
    thickness: float,
    panel_count: int,
) -> list[dict[str, Any]]:
    height = rim_z - bottom_z
    radial_delta = rim_radius - bottom_radius
    slant = math.hypot(height, radial_delta)
    middle_radius = (bottom_radius + rim_radius) / 2.0
    parts: list[dict[str, Any]] = []
    for index in range(panel_count):
        angle = 2.0 * math.pi * index / panel_count
        tangent = [-math.sin(angle), math.cos(angle), 0.0]
        local_z = [radial_delta * math.cos(angle) / slant, radial_delta * math.sin(angle) / slant, height / slant]
        local_y = normalize(cross(local_z, tangent))
        local_rotation = columns(tangent, local_y, local_z)
        world_rotation = matrix_multiply(rotation, local_rotation)
        local_center = [middle_radius * math.cos(angle), middle_radius * math.sin(angle), (bottom_z + rim_z) / 2.0]
        world_center = add(base_position, matrix_vector(rotation, local_center))
        parts.append(
            {
                "kind": "box",
                "position_m": world_center,
                "quaternion_wxyz": quaternion_from_matrix(world_rotation),
                "size_m": [2.0 * math.pi * max(bottom_radius, rim_radius) / panel_count * 1.25, thickness, slant * 1.03],
            }
        )
    bottom_position = add(base_position, matrix_vector(rotation, [0.0, 0.0, bottom_z - thickness / 2.0]))
    parts.append(
        {
            "kind": "cylinder",
            "position_m": bottom_position,
            "quaternion_wxyz": quaternion_from_matrix(rotation),
            "radius_m": bottom_radius + thickness * 1.5,
            "height_m": thickness,
        }
    )
    return parts


def point_inside_profile(point: list[float], container: dict[str, Any], *, radial_margin_m: float = 0.0) -> bool:
    transform = container["transform"]
    rotation = rotation_matrix_xyz(transform["euler_xyz_deg"])
    local = matrix_vector(transpose(rotation), subtract(point, transform["position_m"]))
    collision = container["collision"]
    profile = collision["inner_profile"]
    bottom_z = float(profile[0]["z_m"])
    rim_z = float(profile[-1]["z_m"])
    if local[2] < bottom_z or local[2] > rim_z:
        return False
    lower, upper = next(
        (lower, upper)
        for lower, upper in zip(profile, profile[1:])
        if float(lower["z_m"]) <= local[2] <= float(upper["z_m"])
    )
    fraction = (local[2] - float(lower["z_m"])) / (float(upper["z_m"]) - float(lower["z_m"]))
    radius = float(lower["radius_m"]) + fraction * (float(upper["radius_m"]) - float(lower["radius_m"]))
    return math.hypot(local[0], local[1]) <= max(0.0, radius - radial_margin_m)




def rotation_matrix_xyz(euler_deg: list[float]) -> list[list[float]]:
    x, y, z = [math.radians(float(value)) for value in euler_deg]
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    return [
        [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
        [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
        [-sy, cy * sx, cy * cx],
    ]


def quaternion_from_matrix(matrix: list[list[float]]) -> list[float]:
    trace = matrix[0][0] + matrix[1][1] + matrix[2][2]
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        values = [0.25 * scale, (matrix[2][1] - matrix[1][2]) / scale, (matrix[0][2] - matrix[2][0]) / scale, (matrix[1][0] - matrix[0][1]) / scale]
    elif matrix[0][0] > matrix[1][1] and matrix[0][0] > matrix[2][2]:
        scale = math.sqrt(1.0 + matrix[0][0] - matrix[1][1] - matrix[2][2]) * 2.0
        values = [(matrix[2][1] - matrix[1][2]) / scale, 0.25 * scale, (matrix[0][1] + matrix[1][0]) / scale, (matrix[0][2] + matrix[2][0]) / scale]
    elif matrix[1][1] > matrix[2][2]:
        scale = math.sqrt(1.0 + matrix[1][1] - matrix[0][0] - matrix[2][2]) * 2.0
        values = [(matrix[0][2] - matrix[2][0]) / scale, (matrix[0][1] + matrix[1][0]) / scale, 0.25 * scale, (matrix[1][2] + matrix[2][1]) / scale]
    else:
        scale = math.sqrt(1.0 + matrix[2][2] - matrix[0][0] - matrix[1][1]) * 2.0
        values = [(matrix[1][0] - matrix[0][1]) / scale, (matrix[0][2] + matrix[2][0]) / scale, (matrix[1][2] + matrix[2][1]) / scale, 0.25 * scale]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


def matrix_multiply(left: list[list[float]], right: list[list[float]]) -> list[list[float]]:
    return [[sum(left[row][axis] * right[axis][column] for axis in range(3)) for column in range(3)] for row in range(3)]


def matrix_vector(matrix: list[list[float]], vector: list[float]) -> list[float]:
    return [sum(matrix[row][axis] * vector[axis] for axis in range(3)) for row in range(3)]


def transpose(matrix: list[list[float]]) -> list[list[float]]:
    return [[matrix[column][row] for column in range(3)] for row in range(3)]


def columns(first: list[float], second: list[float], third: list[float]) -> list[list[float]]:
    return [[first[row], second[row], third[row]] for row in range(3)]


def cross(left: list[float], right: list[float]) -> list[float]:
    return [left[1] * right[2] - left[2] * right[1], left[2] * right[0] - left[0] * right[2], left[0] * right[1] - left[1] * right[0]]


def normalize(vector: list[float]) -> list[float]:
    length = math.sqrt(sum(value * value for value in vector))
    return [value / length for value in vector]


def add(left: list[float], right: list[float]) -> list[float]:
    return [float(left[index]) + float(right[index]) for index in range(3)]


def subtract(left: list[float], right: list[float]) -> list[float]:
    return [float(left[index]) - float(right[index]) for index in range(3)]


def vec3(value: Any, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{name} must be a finite 3-vector")
    return [finite(item, name) for item in value]


def vec2(value: Any, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} must be a finite 2-vector")
    return [finite(item, name) for item in value]


def positive(value: Any, name: str) -> float:
    number = finite(value, name)
    if number <= 0.0:
        raise ValueError(f"{name} must be positive")
    return number


def finite(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number
