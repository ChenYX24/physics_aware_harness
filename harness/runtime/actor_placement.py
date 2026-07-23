from __future__ import annotations

import re
from typing import Any

from harness.core.scene_layout import is_support_role


RUNTIME_ACTOR_PLACEMENT_SCHEMA_VERSION = "harness_runtime_actor_placement_v1"
FIELD_OR_CONTROLLER_ROLES = {
    "force_field",
    "magnetic_source",
    "magnet_source",
    "constraint_anchor",
    "elastic_constraint_anchor",
    "bungee_anchor",
    "active_agent",
    "agent_controller",
    "pushing_agent",
    "throwing_agent",
}


def compile_runtime_actor_placement(
    case_spec: dict[str, Any],
    scene_layout: dict[str, Any],
    *,
    asset_resolution: dict[str, Any] | None = None,
    target_backend: str = "UE",
) -> dict[str, Any]:
    object_nodes = [node for node in scene_layout.get("object_nodes", []) if isinstance(node, dict)]
    actor_bindings = [actor_binding_from_node(node, target_backend=target_backend) for node in object_nodes]
    camera_bindings = camera_bindings_from_layout(scene_layout)
    placement_warnings = placement_warnings_for(actor_bindings, scene_layout)
    return {
        "schema_version": RUNTIME_ACTOR_PLACEMENT_SCHEMA_VERSION,
        "capability_id": "runtime_actor_placement_compilation",
        "case_id": case_spec.get("case_id") or scene_layout.get("case_id"),
        "source_capability_id": case_spec.get("capability_id") or scene_layout.get("source_capability_id"),
        "stage_id": "runtime_actor_placement",
        "target_backend": target_backend,
        "coordinate_system": scene_layout.get("coordinate_system", "z_up"),
        "actor_bindings": actor_bindings,
        "camera_bindings": camera_bindings,
        "physics_graph": scene_layout.get("physics_graph") or {"nodes": [], "collision_edges": []},
        "required_runtime_exports": [
            "trajectory.json",
            "contact_events.json",
            "camera_trajectory.json",
            "render_manifest.json",
            "verifier_report.json",
        ],
        "source_artifacts": {
            "scene_layout_schema_version": scene_layout.get("schema_version"),
            "asset_resolution_schema_version": asset_resolution.get("schema_version") if isinstance(asset_resolution, dict) else None,
            "static_scene_case_id": scene_layout.get("case_id"),
        },
        "placement_summary": {
            "actor_count": len(actor_bindings),
            "physics_critical_count": sum(1 for binding in actor_bindings if binding.get("physics_critical")),
            "simulated_actor_count": sum(1 for binding in actor_bindings if (binding.get("physics") or {}).get("simulate_physics")),
            "camera_count": len(camera_bindings),
            "proxy_actor_count": sum(1 for binding in actor_bindings if (binding.get("asset") or {}).get("proxy")),
        },
        "placement_warnings": placement_warnings,
    }


def actor_binding_from_node(node: dict[str, Any], *, target_backend: str) -> dict[str, Any]:
    object_id = str(node.get("object_id") or "")
    role = str(node.get("role") or "")
    physics = node.get("physics") if isinstance(node.get("physics"), dict) else {}
    asset_binding = node.get("asset_binding") if isinstance(node.get("asset_binding"), dict) else {}
    physics_critical = bool(node.get("physics_critical"))
    is_support = is_support_role(role)
    is_field = normalized_role(role) in FIELD_OR_CONTROLLER_ROLES or str(node.get("shape") or "").casefold() in {"fixed_point", "constraint"}
    kinematic = bool(physics.get("kinematic") or is_support or is_field)
    simulate_physics = bool(physics_critical and not kinematic and not is_field)
    proxy = bool(physics.get("proxy") or asset_binding.get("fallback_reason"))
    ue_path = asset_binding.get("selected_asset_ue_path")
    collider = str(physics.get("collider") or node.get("shape") or "box").casefold()
    collision_enabled = bool(physics_critical and not is_field)
    analytic_primitive = (
        "sphere"
        if "sphere" in collider
        else "box"
        if "box" in collider
        else None
    )
    # A declared primitive collider is part of the CaseSpec physics contract.
    # Never let an arbitrary selected asset's pivot or BodySetup silently replace
    # that geometry: those properties vary by asset and by target UE platform.
    controlled_analytic_collision = bool(
        collision_enabled and (not ue_path or analytic_primitive is not None)
    )
    collision_geometry_source = (
        "none"
        if not collision_enabled
        else f"analytic_{analytic_primitive or collider}"
        if controlled_analytic_collision
        else "selected_asset"
    )
    runtime_usage = (
        "visual_proxy"
        if ue_path and analytic_primitive == "sphere" and simulate_physics
        else "analytic_proxy"
        if controlled_analytic_collision
        else "collision_and_visual"
        if ue_path
        else "analytic_proxy"
    )
    return {
        "object_id": object_id,
        "runtime_actor_id": runtime_actor_id(object_id),
        "role": role,
        "category": node.get("category"),
        "physics_critical": physics_critical,
        "physics_graph_member": bool(node.get("physics_graph_member")),
        "ue_class": ue_class_for(node, is_field=is_field),
        "transform": node.get("transform") or {},
        "bounds": node.get("bounds") or {},
        "asset": {
            "selected_asset_id": asset_binding.get("selected_asset_id"),
            "ue_path": ue_path,
            "asset_kind": asset_binding.get("asset_kind"),
            "proxy": proxy,
            "binding_source": "ue_asset" if ue_path else "analytic_proxy" if proxy else "unbound",
            "runtime_usage": runtime_usage,
            "source_kind": asset_binding.get("source_kind"),
            "source_uri": asset_binding.get("source_uri"),
            "license": asset_binding.get("license"),
            "sha256": asset_binding.get("sha256"),
            "preserve_authored_scale": bool(asset_binding.get("preserve_authored_scale")),
            "authored_size_m": asset_binding.get("authored_size_m"),
            "quality_gate": asset_binding.get("quality_gate"),
            "fallback_reason": asset_binding.get("fallback_reason"),
        },
        "physics": {
            "simulate_physics": simulate_physics,
            "kinematic": kinematic,
            "collision_enabled": collision_enabled,
            "mass_kg": physics.get("mass_kg"),
            "collider": physics.get("collider"),
            "collision_geometry_source": collision_geometry_source,
            "collision_geometry_verification": (
                "not_applicable"
                if not collision_enabled
                else "runtime_controlled"
                if controlled_analytic_collision
                else "declared_unverified"
            ),
            "collision_profile": "NoCollision" if is_field else physics.get("collision_profile"),
            "material": physics.get("material"),
            "linear_damping": physics.get("linear_damping"),
            "angular_damping": physics.get("angular_damping"),
            "enable_gravity": physics.get("enable_gravity"),
            "use_ccd": physics.get("use_ccd"),
            "initial_angular_velocity_rad_s": physics.get("initial_angular_velocity_rad_s"),
        },
        "runtime_binding_requirements": asset_binding.get("runtime_binding_requirements") or [],
        "target_backend": target_backend,
    }


def camera_bindings_from_layout(scene_layout: dict[str, Any]) -> list[dict[str, Any]]:
    views = ((scene_layout.get("camera_plan") or {}).get("views") or []) if isinstance(scene_layout.get("camera_plan"), dict) else []
    bindings: list[dict[str, Any]] = []
    for index, view in enumerate(views):
        if not isinstance(view, dict):
            continue
        camera_id = str(view.get("camera_id") or view.get("id") or f"camera_{index:02d}")
        bindings.append(
            {
                "camera_id": camera_id,
                "runtime_camera_id": runtime_actor_id(camera_id, prefix="camera"),
                "mode": view.get("mode") or view.get("type") or "planned_view",
                "transform": view.get("transform") or view.get("pose") or {},
                "target_object_id": view.get("target_object_id") or view.get("target"),
            }
        )
    return bindings


def placement_warnings_for(actor_bindings: list[dict[str, Any]], scene_layout: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    known = {binding["object_id"] for binding in actor_bindings}
    for edge in (scene_layout.get("physics_graph") or {}).get("collision_edges") or []:
        if not isinstance(edge, list) or len(edge) < 2:
            continue
        missing = [str(item) for item in edge[:2] if str(item) not in known]
        if missing:
            warnings.append({"code": "missing_collision_edge_actor", "object_ids": missing})
    return warnings


def ue_class_for(node: dict[str, Any], *, is_field: bool) -> str:
    if is_field:
        return "/Script/Engine.Actor"
    asset_binding = node.get("asset_binding") if isinstance(node.get("asset_binding"), dict) else {}
    asset_kind = str(asset_binding.get("asset_kind") or "").casefold()
    asset_path = str(asset_binding.get("selected_asset_ue_path") or "")
    if asset_kind in {"geometrycollection", "geometry_collection"}:
        return "/Script/GeometryCollectionEngine.GeometryCollectionActor"
    if asset_kind == "blueprint" and "." in asset_path:
        package, object_name = asset_path.rsplit(".", 1)
        return f"{package}.{object_name}_C"
    shape = str(node.get("shape") or "").casefold()
    if "skeletal" in shape:
        return "/Script/Engine.SkeletalMeshActor"
    return "/Script/Engine.StaticMeshActor"


def runtime_actor_id(object_id: str, *, prefix: str = "actor") -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", str(object_id).strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_").lower()
    if not normalized:
        normalized = "unnamed"
    return f"{prefix}_{normalized}"


def normalized_role(role: str) -> str:
    return str(role).casefold().replace("-", "_").replace(" ", "_")
