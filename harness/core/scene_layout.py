from __future__ import annotations

import math
from typing import Any

from harness.assets.asset_intent import intent_from_object


SCENE_LAYOUT_SCHEMA_VERSION = "harness_scene_layout_v1"
SUPPORT_ROLES = {"support", "floor", "ground", "table", "ramp", "slope_surface", "inclined_plane"}
DYNAMIC_ABOVE_SUPPORT_ROLES = {
    "active_chain_driver",
    "constrained_chain_body",
    "constraint_anchor",
    "falling_body",
    "stack_block",
    "projectile",
    "thrown_body",
    "launched_body",
    "elastic_payload",
    "bungee_payload",
    "elastic_constraint_anchor",
    "elastic_constrained_body",
    "wind_drift_body",
    "wind_subject",
    "balloon",
    "light_body",
    "magnetized_body",
    "magnetic_body",
    "magnetic_subject",
    "magnetic_source",
    "magnet_source",
}


def build_object_node(obj: dict[str, Any], asset_row: dict[str, Any] | None = None) -> dict[str, Any]:
    intent = intent_from_object(obj)
    selected_asset = asset_row.get("selected_asset") if asset_row else None
    position = vec3(obj.get("initial_position_m") or obj.get("position_m") or obj.get("position") or [0.0, 0.0, 0.0])
    rotation = vec3(obj.get("rotation_deg") or obj.get("initial_rotation_deg") or [0.0, 0.0, 0.0])
    extents = estimate_shape_extents(obj, selected_asset)
    radius = round(math.sqrt(sum(value * value for value in extents)), 6)
    mass = obj.get("mass_kg")
    if mass is None and isinstance(selected_asset, dict):
        mass = selected_asset.get("mass_kg")
    material = obj.get("material")
    if material is None and isinstance(selected_asset, dict):
        material = selected_asset.get("material")
    collider = obj.get("collider")
    if collider is None and isinstance(selected_asset, dict):
        collider = selected_asset.get("collider")
    collision_profile = obj.get("collision_profile")
    if collision_profile is None and isinstance(selected_asset, dict):
        collision_profile = selected_asset.get("collision_profile")
    if selected_asset is None and asset_row and asset_row.get("fallback_reason"):
        defaults = analytic_physics_defaults(obj, intent.role)
        mass = mass if mass is not None else defaults["mass_kg"]
        collider = collider if collider is not None else defaults["collider"]
        collision_profile = collision_profile if collision_profile is not None else defaults["collision_profile"]
        material = material if material is not None else defaults["material"]
    bounds = bounds_for_position(
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
    shape = obj.get("shape")
    if shape is None and isinstance(selected_asset, dict):
        shape = selected_asset.get("shape") or selected_asset.get("collider")
    if shape is None:
        shape = "box"
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
            "scale": obj.get("scale") or [1.0, 1.0, 1.0],
        },
        "bounds": {
            "extents_m": round_vec(extents),
            "bounding_radius_m": radius,
            "bottom_z": bounds["bottom_z"],
            "top_z": bounds["top_z"],
        },
        "physics": {
            "body_type": obj.get("body_type"),
            "collision_required": obj.get("collision_required"),
            "mass_kg": mass,
            "collider": collider,
            "collision_profile": collision_profile,
            "material": material,
            "linear_damping": obj.get("linear_damping"),
            "angular_damping": obj.get("angular_damping"),
            "enable_gravity": obj.get("enable_gravity"),
            "use_ccd": obj.get("use_ccd"),
            "initial_angular_velocity_rad_s": obj.get("initial_angular_velocity_rad_s"),
            "kinematic": bool(obj.get("kinematic", is_support_role(intent.role))),
            "proxy": bool(selected_asset.get("proxy")) if isinstance(selected_asset, dict) else asset_row is not None and asset_row.get("fallback_reason") is not None,
        },
        "asset_binding": {
            "selected_asset_id": asset_id(selected_asset),
            "selected_asset_ue_path": selected_asset.get("ue_path") if isinstance(selected_asset, dict) else None,
            "asset_kind": (
                selected_asset.get("asset_kind")
                or selected_asset.get("type")
                or selected_asset.get("class_name")
            ) if isinstance(selected_asset, dict) else None,
            "source_kind": selected_asset.get("source_kind") if isinstance(selected_asset, dict) else "analytic_proxy",
            "source_uri": selected_asset.get("source_uri") if isinstance(selected_asset, dict) else None,
            "license": selected_asset.get("license") if isinstance(selected_asset, dict) else None,
            "sha256": selected_asset.get("sha256") if isinstance(selected_asset, dict) else None,
            "preserve_authored_scale": bool(selected_asset.get("preserve_authored_scale")) if isinstance(selected_asset, dict) else False,
            "authored_size_m": selected_asset.get("authored_size_m") if isinstance(selected_asset, dict) else None,
            "quality_gate": selected_asset.get("quality_gate") if isinstance(selected_asset, dict) else None,
            "fallback_reason": asset_row.get("fallback_reason") if asset_row else "asset resolution missing",
            "runtime_binding_requirements": asset_row.get("runtime_binding_requirements", []) if asset_row else [],
        },
    }


def analytic_physics_defaults(obj: dict[str, Any], role: str) -> dict[str, Any]:
    shape = str(obj.get("shape") or "").casefold()
    if is_support_role(role):
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
    return normalized in SUPPORT_ROLES or any(token in normalized for token in ("support", "floor", "ground", "table", "ramp", "slope"))


def allows_above_support(role: str) -> bool:
    normalized = str(role).casefold().replace("-", "_").replace(" ", "_")
    return normalized in DYNAMIC_ABOVE_SUPPORT_ROLES


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
