from __future__ import annotations

import math
from typing import Any

from harness.assets.asset_intent import intent_from_object


SCENE_LAYOUT_SCHEMA_VERSION = "harness_scene_layout_v1"
SUPPORT_ROLES = {"support", "floor", "ground", "table"}


def build_object_node(
    obj: dict[str, Any],
    asset_row: dict[str, Any] | None = None,
    *,
    objects_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    intent = intent_from_object(obj)
    selected_asset = asset_row.get("selected_asset") if asset_row else None
    required_asset_unresolved = _required_asset_unresolved(asset_row)
    solver = obj.get("solver") if isinstance(obj.get("solver"), dict) else {}
    position, rotation, declared_scale = declared_object_transform(obj, objects_by_id or {})
    geometry_obj = dict(obj)
    geometry_obj["scale"] = declared_scale
    initial_volume = solver.get("initial_volume") if isinstance(solver.get("initial_volume"), dict) else {}
    if initial_volume:
        geometry_obj.update(
            {
                key: initial_volume[key]
                for key in ("shape", "size_m", "radius_m", "height_m")
                if initial_volume.get(key) is not None
            }
        )
    declared_asset = obj.get("asset") if isinstance(obj.get("asset"), dict) else {}
    declared_asset_size = first_positive_size(declared_asset.get("bbox_m"), declared_asset.get("bbox_size_m"))
    geometry_asset = selected_asset
    articulated_size = solver.get("authored_size_m") if solver.get("type") == "articulated_body" else None
    if isinstance(articulated_size, list) and len(articulated_size) == 3:
        geometry_asset = {
            **(selected_asset or {}),
            "authored_size_m": [float(value) for value in articulated_size],
            "preserve_authored_scale": True,
        }
    if declared_asset_size is not None:
        world_size = [declared_asset_size[index] * declared_scale[index] for index in range(3)]
        geometry_asset = {**(selected_asset or {}), "authored_size_m": world_size, "bbox_size_m": world_size}
    instance_geometry = resolve_instance_geometry(geometry_obj, geometry_asset)
    extents = instance_geometry["extents_m"]
    radius = round(math.sqrt(sum(value * value for value in extents)), 6)
    state_kind = declared_state_kind(obj)
    body_type = obj.get("body_type") or solver.get("mobility") or ("particle" if state_kind == "particle" else None)
    collision = solver.get("collision") if isinstance(solver.get("collision"), dict) else {}
    explicit_solver_collision = str(collision.get("type") or "") in {"plane", "axisymmetric_profile"}
    collision_required = obj.get("collision_required")
    if collision_required is None:
        collision_required = False if state_kind == "particle" else True if collision else None
    declared_collision_geometry = (
        obj.get("collision_geometry")
        if isinstance(obj.get("collision_geometry"), dict)
        else None
    )
    visual_representation = (
        obj.get("visual_representation")
        if isinstance(obj.get("visual_representation"), dict)
        else {}
    )
    visual_source = str(visual_representation.get("source") or "asset")
    if declared_collision_geometry is not None and collision_required is None:
        collision_required = True
    mass = obj.get("mass_kg")
    if mass is None and isinstance(selected_asset, dict):
        mass = selected_asset.get("mass_kg")
    material = obj.get("material")
    if material is None and isinstance(selected_asset, dict):
        material = selected_asset.get("material")
    collider = obj.get("collider")
    if collider is None:
        collider = collision.get("type")
    if collider is None and isinstance(selected_asset, dict):
        collider = selected_asset.get("collider")
    collision_profile = obj.get("collision_profile")
    if collision_profile is None and isinstance(selected_asset, dict):
        collision_profile = selected_asset.get("collision_profile")
    if state_kind != "particle" and (declared_collision_geometry is not None or explicit_solver_collision):
        defaults = analytic_physics_defaults(obj, intent.role)
        mass = mass if mass is not None else defaults["mass_kg"]
        collider = collider if collider is not None else defaults["collider"]
        collision_profile = collision_profile if collision_profile is not None else defaults["collision_profile"]
        material = material if material is not None else defaults["material"]
    bounds = (
        {
            "bottom_z": round(position[2], 6),
            "top_z": round(position[2] + extents[2] * 2.0, 6),
        }
        if str(solver.get("type") or "") == "articulated_body"
        else bounds_for_position(
            position,
            extents,
            intent.role,
            centered=bool(
                isinstance(selected_asset, dict)
                and selected_asset.get("source_kind") == "procedural_generation"
                and selected_asset.get("preserve_authored_scale") is True
                and selected_asset.get("authored_size_m")
            ),
        )
    )
    shape = obj.get("shape")
    if shape is None:
        shape = initial_volume.get("shape") or collision.get("type")
    if shape is None and isinstance(selected_asset, dict):
        shape = selected_asset.get("shape") or selected_asset.get("collider")
    if shape is None:
        shape = "box"
    collision_geometry = resolved_collision_geometry(
        declared_collision_geometry,
        object_position_m=position,
        object_rotation_deg=rotation,
        collision_enabled=collision_required is not False and state_kind != "particle",
    )
    if collision_geometry is not None:
        collider = collision_geometry["shape"]
    selected_collision = (
        selected_asset.get("collision")
        if isinstance(selected_asset, dict) and isinstance(selected_asset.get("collision"), dict)
        else {}
    )
    asset_body_setup_verified = bool(
        collision_required is not False
        and visual_source == "asset"
        and isinstance(selected_asset, dict)
        and selected_asset.get("ue_path")
        and selected_collision.get("present") is not False
        and selected_asset.get("collider")
        and selected_asset.get("collision_profile")
    )
    collision_binding_source = (
        "analytic"
        if collision_geometry is not None
        else "explicit_solver_analytic"
        if explicit_solver_collision
        else "asset_body_setup"
        if asset_body_setup_verified
        else "none"
        if collision_required is False or state_kind == "particle"
        else "unverified_asset_body_setup"
        if visual_source == "asset" and isinstance(selected_asset, dict) and selected_asset.get("ue_path")
        else "unbound"
    )
    visual_center_offset = [
        0.0,
        0.0,
        (float(bounds["bottom_z"]) + float(bounds["top_z"])) / 2.0 - position[2],
    ]
    solver_contract = obj.get("solver") if isinstance(obj.get("solver"), dict) else None
    return {
        "object_id": intent.object_id,
        "role": intent.role,
        "shape": str(shape),
        "category": intent.category,
        "physics_critical": intent.physics_critical,
        "physics_graph_member": intent.physics_critical,
        "transform": {
            "position_m": round_vec(position),
            "rotation_deg": round_vec(rotation),
            "scale": instance_geometry["instance_scale"],
        },
        "bounds": {
            "extents_m": round_vec(extents),
            "bounding_radius_m": radius,
            "bottom_z": bounds["bottom_z"],
            "top_z": bounds["top_z"],
            "local_center_offset_m": round_vec(visual_center_offset),
        },
        "visual_representation": {
            "source": visual_source,
            "visible": visual_representation.get("visible") is not False,
        },
        "solver_declared": isinstance(obj.get("solver"), dict),
        **(
            {"articulated_body_contract": solver_contract}
            if str((solver_contract or {}).get("type") or "") == "articulated_body"
            else {}
        ),
        "physics": {
            "state_kind": state_kind,
            "body_type": body_type,
            "collision_required": collision_required,
            "mass_kg": mass,
            "collider": collider,
            "collision_geometry": collision_geometry,
            "collision_binding_source": collision_binding_source,
            "collision_profile": collision_profile,
            "material": material,
            "linear_damping": obj.get("linear_damping"),
            "angular_damping": obj.get("angular_damping"),
            "enable_gravity": obj.get("enable_gravity"),
            "use_ccd": obj.get("use_ccd"),
            "initial_angular_velocity_rad_s": obj.get("initial_angular_velocity_rad_s"),
            "kinematic": bool(obj.get("kinematic", str(body_type).casefold() in {"static", "kinematic"} or is_support_role(intent.role))),
            "proxy": (
                bool(selected_asset.get("proxy"))
                if isinstance(selected_asset, dict)
                else bool(asset_row and asset_row.get("fallback_reason") and not required_asset_unresolved)
            ),
        },
        "asset_binding": {
            "selected_asset_id": asset_id(selected_asset),
            "selected_asset_ue_path": selected_asset.get("ue_path") if isinstance(selected_asset, dict) else None,
            "asset_kind": (
                selected_asset.get("asset_kind")
                or selected_asset.get("type")
                or selected_asset.get("class_name")
            ) if isinstance(selected_asset, dict) else None,
            "source_kind": (
                selected_asset.get("source_kind")
                if isinstance(selected_asset, dict)
                else None
                if required_asset_unresolved
                else "analytic_proxy"
            ),
            "source_uri": selected_asset.get("source_uri") if isinstance(selected_asset, dict) else None,
            "license": selected_asset.get("license") if isinstance(selected_asset, dict) else None,
            "sha256": selected_asset.get("sha256") if isinstance(selected_asset, dict) else None,
            "preserve_authored_scale": bool(
                selected_asset.get("preserve_authored_scale")
                and instance_geometry["scale_policy"] == "preserve_authored"
            ) if isinstance(selected_asset, dict) else False,
            "catalog_preserve_authored_scale": bool(selected_asset.get("preserve_authored_scale")) if isinstance(selected_asset, dict) else False,
            "authored_size_m": selected_asset.get("authored_size_m") if isinstance(selected_asset, dict) else None,
            "scale_policy": instance_geometry["scale_policy"],
            "scale_applied": instance_geometry["scale_applied"],
            "instance_scale": instance_geometry["instance_scale"],
            "uniform_scale_factor": instance_geometry["uniform_scale_factor"],
            "target_size_m": instance_geometry["target_size_m"],
            "effective_size_m": instance_geometry["effective_size_m"],
            "geometry_source": "declared_object_asset" if declared_asset_size is not None else "asset_resolution_or_object_geometry",
            "quality_gate": selected_asset.get("quality_gate") if isinstance(selected_asset, dict) else None,
            "fallback_reason": asset_row.get("fallback_reason") if asset_row else "asset resolution missing",
            "required_asset_unresolved": required_asset_unresolved,
            "runtime_binding_requirements": asset_row.get("runtime_binding_requirements", []) if asset_row else [],
            "collision": selected_collision or None,
            "collision_body_setup_verified": asset_body_setup_verified,
            "geometry_registration": declared_asset.get("geometry_registration"),
        },
    }


def resolved_collision_geometry(
    declared: dict[str, Any] | None,
    *,
    object_position_m: list[float],
    object_rotation_deg: list[float],
    collision_enabled: bool,
) -> dict[str, Any] | None:
    if not collision_enabled or declared is None:
        return None
    collision_shape = str(declared.get("shape") or "box")
    size_m = vec3(declared.get("size_m"))
    local_offset = vec3(declared.get("local_center_offset_m"))
    world_offset = rotate_local_vector_ue(local_offset, object_rotation_deg)
    world_center = [object_position_m[index] + world_offset[index] for index in range(3)]
    return {
        "shape": collision_shape,
        "size_m": round_vec(size_m),
        "local_center_offset_m": round_vec(local_offset),
        "world_center_m": round_vec(world_center),
        "source": "declared",
    }


def rotate_local_vector_ue(vector: list[float], rotation_deg: list[float]) -> list[float]:
    pitch, yaw, roll = [math.radians(float(value)) for value in [*rotation_deg, 0.0, 0.0, 0.0][:3]]
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    axes = [
        [cy * cp, sy * cp, sp],
        [cy * sp * sr - sy * cr, sy * sp * sr + cy * cr, -cp * sr],
        [-(cy * sp * cr + sy * sr), cy * sr - sy * sp * cr, cp * cr],
    ]
    return [sum(axes[axis][component] * vector[axis] for axis in range(3)) for component in range(3)]


def declared_state_kind(obj: dict[str, Any]) -> str:
    solver = obj.get("solver") if isinstance(obj.get("solver"), dict) else {}
    material_model = str(solver.get("material_model") or "").casefold()
    role = str(obj.get("role") or "").casefold()
    if material_model.startswith("sph_") or role in {"fluid", "fluid_volume"}:
        return "particle"
    return "rigid"


def declared_object_transform(
    obj: dict[str, Any],
    objects_by_id: dict[str, dict[str, Any]],
) -> tuple[list[float], list[float], list[float]]:
    solver = obj.get("solver") if isinstance(obj.get("solver"), dict) else {}
    transform = solver.get("transform") if isinstance(solver.get("transform"), dict) else {}
    position = vec3(
        transform.get("position_m")
        or obj.get("initial_position_m")
        or obj.get("position_m")
        or obj.get("position")
        or [0.0, 0.0, 0.0]
    )
    rotation = vec3(
        transform.get("euler_xyz_deg")
        or obj.get("rotation_deg")
        or obj.get("initial_rotation_deg")
        or [0.0, 0.0, 0.0]
    )
    scale = vec3(transform.get("scale") or obj.get("scale") or [1.0, 1.0, 1.0])
    initial = solver.get("initial_volume") if isinstance(solver.get("initial_volume"), dict) else {}
    frame = initial.get("frame") if isinstance(initial.get("frame"), dict) else {}
    if frame.get("type") == "world":
        position = vec3(initial.get("position_m"))
        rotation = vec3(initial.get("euler_xyz_deg"))
    elif frame.get("type") == "body_local":
        parent = objects_by_id.get(str(frame.get("body_id") or ""))
        if isinstance(parent, dict):
            parent_position, parent_rotation, _parent_scale = declared_object_transform(parent, {})
            local_position = vec3(initial.get("position_m"))
            rotated = matrix_vector(rotation_matrix_xyz(parent_rotation), local_position)
            position = [parent_position[index] + rotated[index] for index in range(3)]
            rotation = [parent_rotation[index] + vec3(initial.get("euler_xyz_deg"))[index] for index in range(3)]
    return position, rotation, scale


def rotation_matrix_xyz(euler_deg: list[float]) -> list[list[float]]:
    x, y, z = [math.radians(float(value)) for value in euler_deg]
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    return [
        [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
        [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
        [-sy, cy * sx, cy * cx],
    ]


def matrix_vector(matrix: list[list[float]], vector: list[float]) -> list[float]:
    return [sum(matrix[row][axis] * vector[axis] for axis in range(3)) for row in range(3)]


def _required_asset_unresolved(asset_row: dict[str, Any] | None) -> bool:
    if not isinstance(asset_row, dict) or asset_row.get("selected_asset") is not None:
        return False
    acquisition = asset_row.get("acquisition") if isinstance(asset_row.get("acquisition"), dict) else {}
    requested = acquisition.get("requested") if isinstance(acquisition.get("requested"), dict) else {}
    return bool(
        requested.get("requirement") == "required"
        and requested.get("route") in {"external_site", "procedural_generation", "model_generation"}
    )


def resolve_instance_geometry(
    obj: dict[str, Any],
    selected_asset: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve an auditable per-scene scale without mutating the Catalog asset."""
    authored_size = first_positive_size(
        (selected_asset or {}).get("authored_size_m"),
        (selected_asset or {}).get("bbox_size_m"),
    )
    target_size = first_positive_size(obj.get("size_m"))
    scale_policy = str(obj.get("asset_scale_policy") or "preserve_authored")
    uniform_scale_factor = 1.0
    effective_size = authored_size or target_size
    if (
        scale_policy == "fit_uniform_to_approx_size"
        and authored_size is not None
        and target_size is not None
    ):
        authored_diagonal = math.sqrt(sum(value * value for value in authored_size))
        target_diagonal = math.sqrt(sum(value * value for value in target_size))
        if authored_diagonal > 1e-9:
            uniform_scale_factor = target_diagonal / authored_diagonal
            effective_size = [value * uniform_scale_factor for value in authored_size]
    if effective_size is None:
        extents = estimate_shape_extents(obj, selected_asset)
        effective_size = [value * 2.0 for value in extents]
    else:
        extents = [max(value / 2.0, 0.001) for value in effective_size]
    instance_scale = (
        [uniform_scale_factor, uniform_scale_factor, uniform_scale_factor]
        if scale_policy == "fit_uniform_to_approx_size" and authored_size is not None and target_size is not None
        else vec3(obj.get("scale") or [1.0, 1.0, 1.0])
    )
    return {
        "scale_policy": scale_policy,
        "scale_applied": bool(
            scale_policy == "fit_uniform_to_approx_size"
            and authored_size is not None
            and target_size is not None
        ),
        "uniform_scale_factor": round(uniform_scale_factor, 9),
        "instance_scale": round_vec(instance_scale),
        "target_size_m": round_vec(target_size) if target_size is not None else None,
        "effective_size_m": round_vec(effective_size),
        "extents_m": extents,
    }


def first_positive_size(*values: Any) -> list[float] | None:
    for value in values:
        if not isinstance(value, (list, tuple)) or len(value) < 3:
            continue
        size = vec3(value)
        if all(component > 0.0 for component in size):
            return size
    return None


def analytic_physics_defaults(obj: dict[str, Any], role: str) -> dict[str, Any]:
    shape = str(obj.get("shape") or "").casefold()
    body_type = str(obj.get("body_type") or "").casefold()
    if is_support_role(role) or body_type in {"static", "kinematic"}:
        return {
            "mass_kg": 100.0,
            "collider": "box",
            "collision_profile": "BlockAll",
            "material": {"static_friction": 0.06, "dynamic_friction": 0.04, "restitution": 0.15},
        }
    if "sphere" in shape or "ball" in shape:
        return {
            "mass_kg": 0.17,
            "collider": "sphere",
            "collision_profile": "PhysicsActor",
            "material": {"static_friction": 0.05, "dynamic_friction": 0.035, "restitution": 0.86},
        }
    return {
        "mass_kg": 1.0,
        "collider": "box",
        "collision_profile": "PhysicsActor",
        "material": {"static_friction": 0.5, "dynamic_friction": 0.35, "restitution": 0.2},
    }


def bounds_for_position(
    position: list[float],
    extents: list[float],
    role: str,
    *,
    centered: bool = False,
) -> dict[str, float]:
    if is_support_role(role) and not centered:
        return {
            "bottom_z": round(position[2] - extents[2] * 2.0, 6),
            "top_z": round(position[2], 6),
        }
    return {
        "bottom_z": round(position[2] - extents[2], 6),
        "top_z": round(position[2] + extents[2], 6),
    }


def estimate_shape_extents(obj: dict[str, Any], selected_asset: dict[str, Any] | None = None) -> list[float]:
    shape = str(obj.get("shape") or (selected_asset or {}).get("collider") or "").casefold()
    for size in (
        (selected_asset or {}).get("authored_size_m"),
        (selected_asset or {}).get("bbox_size_m"),
        obj.get("size_m"),
    ):
        if isinstance(size, list) and len(size) >= 3:
            dimensions = vec3(size)
            if all(value > 0.0 for value in dimensions):
                return [max(value / 2.0, 0.001) for value in dimensions]
    if "capsule" in shape or "pin" in shape or "cylinder" in shape:
        radius = safe_float(obj.get("radius_m") or (selected_asset or {}).get("radius_m"), 0.06)
        height = safe_float(obj.get("height_m") or obj.get("pin_height_m"), 0.36)
        return [radius, radius, max(height / 2.0, radius)]
    if "radius_m" in obj:
        radius = safe_float(obj.get("radius_m"), 0.09)
        return [radius, radius, radius]
    if selected_asset and "radius_m" in selected_asset:
        radius = safe_float(selected_asset.get("radius_m"), 0.09)
        return [radius, radius, radius]
    if "thin_box" in shape or "panel" in shape or "glass" in shape:
        return [0.04, 0.5, 0.5]
    if "sphere" in shape or "ball" in shape:
        return [0.09, 0.09, 0.09]
    if "ramp" in shape or "inclined" in shape or "plane" in shape:
        return [0.8, 0.5, 0.05]
    if "floor" in shape or str(obj.get("role") or "").casefold() in {"support", "floor", "ground", "table"}:
        return [1.5, 1.0, 0.05]
    if "constraint" in shape or "fixed_point" in shape or "anchor" in shape:
        return [0.001, 0.001, 0.001]
    return [0.25, 0.25, 0.25]


def is_support_role(role: str) -> bool:
    normalized = str(role).casefold().replace("-", "_").replace(" ", "_")
    return normalized in SUPPORT_ROLES or any(token in normalized for token in ("support", "floor", "ground", "table"))


def allows_above_support(role: str) -> bool:
    return not is_support_role(role)


def asset_id(selected_asset: Any) -> str | None:
    if not isinstance(selected_asset, dict):
        return None
    value = selected_asset.get("asset_id") or selected_asset.get("id") or selected_asset.get("name")
    return str(value) if value is not None else None


def object_position(node: dict[str, Any]) -> list[float]:
    return vec3((node.get("transform") or {}).get("position_m"))


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        value = [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    return [safe_float(padded[0], 0.0), safe_float(padded[1], 0.0), safe_float(padded[2], 0.0)]


def round_vec(value: list[float] | tuple[float, float, float]) -> list[float]:
    return [round(float(value[0]), 6), round(float(value[1]), 6), round(float(value[2]), 6)]


def safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
