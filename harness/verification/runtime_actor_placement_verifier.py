from __future__ import annotations

from typing import Any

from harness.runtime.actor_placement import RUNTIME_ACTOR_PLACEMENT_SCHEMA_VERSION


def verify_runtime_actor_placement(case_spec: dict[str, Any], placement: dict[str, Any] | None) -> dict[str, Any]:
    case_id = str(case_spec.get("case_id") or "")
    if not isinstance(placement, dict) or placement.get("schema_version") != RUNTIME_ACTOR_PLACEMENT_SCHEMA_VERSION:
        return fail_report(case_id, "F7_runtime_artifact_incomplete", "runtime_actor_placement_schema", "schema_version", placement.get("schema_version") if isinstance(placement, dict) else None)
    bindings = [binding for binding in placement.get("actor_bindings") or [] if isinstance(binding, dict)]
    if not bindings:
        return fail_report(case_id, "F7_runtime_artifact_incomplete", "runtime_actor_placement", "actor_count", 0)
    runtime_ids = [str(binding.get("runtime_actor_id") or "") for binding in bindings]
    duplicate = first_duplicate(runtime_ids)
    if duplicate:
        return fail_report(case_id, "F7_runtime_artifact_incomplete", duplicate, "duplicate_runtime_actor_id", duplicate)
    by_object = {str(binding.get("object_id")): binding for binding in bindings if binding.get("object_id")}
    missing_physics_object = first_missing_physics_object(case_spec, by_object)
    if missing_physics_object:
        return fail_report(case_id, "F7_runtime_artifact_incomplete", missing_physics_object, "missing_runtime_actor_binding", missing_physics_object, checks=checks(placement, bindings))
    bad_binding = first_bad_physics_binding(bindings)
    if bad_binding:
        return fail_report(case_id, bad_binding["failure_type"], bad_binding["object_id"], bad_binding["metric"], bad_binding["value"], checks=checks(placement, bindings))
    missing_edge = first_missing_collision_edge_actor(placement, by_object)
    if missing_edge:
        return fail_report(case_id, "F7_runtime_artifact_incomplete", ":".join(missing_edge), "missing_collision_edge_actor", missing_edge, checks=checks(placement, bindings))
    if not placement.get("camera_bindings"):
        return fail_report(case_id, "F7_runtime_artifact_incomplete", case_id, "camera_bindings", 0, checks=checks(placement, bindings))
    return {
        "schema_version": "harness_runtime_actor_placement_report_v1",
        "case_id": case_id,
        "capability_id": "runtime_actor_placement_compilation",
        "status": "pass",
        "failure_type": None,
        "first_failure": None,
        "checks": checks(placement, bindings),
        "repair_suggestions": [],
    }


def first_missing_physics_object(case_spec: dict[str, Any], by_object: dict[str, dict[str, Any]]) -> str | None:
    for obj in case_spec.get("objects") or []:
        if not isinstance(obj, dict):
            continue
        object_id = str(obj.get("id") or "")
        if not object_id:
            continue
        if object_id not in by_object and is_physics_critical_role(str(obj.get("role") or "")):
            return object_id
    return None


def first_bad_physics_binding(bindings: list[dict[str, Any]]) -> dict[str, Any] | None:
    for binding in bindings:
        if not binding.get("physics_critical"):
            continue
        object_id = str(binding.get("object_id") or "")
        asset = binding.get("asset") if isinstance(binding.get("asset"), dict) else {}
        physics = binding.get("physics") if isinstance(binding.get("physics"), dict) else {}
        if not asset.get("ue_path") and not asset.get("proxy"):
            return {"failure_type": "F2_asset_missing", "object_id": object_id, "metric": "missing_asset_or_proxy_binding", "value": None}
        if physics.get("collision_enabled") and not physics.get("collider"):
            return {"failure_type": "F3_invalid_initial_physics_state", "object_id": object_id, "metric": "missing_collider", "value": None}
        if physics.get("collision_enabled") and not physics.get("collision_profile"):
            return {"failure_type": "F3_invalid_initial_physics_state", "object_id": object_id, "metric": "missing_collision_profile", "value": None}
        if physics.get("simulate_physics") and physics.get("mass_kg") is None:
            return {"failure_type": "F3_invalid_initial_physics_state", "object_id": object_id, "metric": "missing_mass_kg", "value": None}
    return None


def first_missing_collision_edge_actor(placement: dict[str, Any], by_object: dict[str, dict[str, Any]]) -> list[str] | None:
    for edge in ((placement.get("physics_graph") or {}).get("collision_edges") or []):
        if not isinstance(edge, list) or len(edge) < 2:
            continue
        pair = [str(item) for item in edge[:2]]
        if any(object_id not in by_object for object_id in pair):
            return pair
    return None


def checks(placement: dict[str, Any], bindings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "actor_count": len(bindings),
        "physics_critical_count": sum(1 for binding in bindings if binding.get("physics_critical")),
        "simulated_actor_count": sum(1 for binding in bindings if (binding.get("physics") or {}).get("simulate_physics")),
        "proxy_actor_count": sum(1 for binding in bindings if (binding.get("asset") or {}).get("proxy")),
        "camera_count": len(placement.get("camera_bindings") or []),
        "collision_edge_count": len((placement.get("physics_graph") or {}).get("collision_edges") or []),
    }


def fail_report(
    case_id: str,
    failure_type: str,
    object_id: str,
    metric: str,
    value: Any,
    *,
    checks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "harness_runtime_actor_placement_report_v1",
        "case_id": case_id,
        "capability_id": "runtime_actor_placement_compilation",
        "status": "fail",
        "failure_type": failure_type,
        "first_failure": {
            "object_id": object_id,
            "frame": 0,
            "time": 0.0,
            "metric": metric,
            "value": value,
        },
        "checks": checks or {},
        "repair_suggestions": repair_suggestions(failure_type),
    }


def repair_suggestions(failure_type: str) -> list[str]:
    if failure_type == "F2_asset_missing":
        return ["Resolve a selected UE asset or mark an analytic proxy before runtime actor placement."]
    if failure_type == "F3_invalid_initial_physics_state":
        return ["Add collider, mass, material, and collision profile metadata for the physics-critical actor."]
    return ["Regenerate static scene placement and actor placement from a valid case spec."]


def first_duplicate(values: list[str]) -> str | None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


def is_physics_critical_role(role: str) -> bool:
    normalized = str(role).casefold().replace("-", "_").replace(" ", "_")
    return not any(term in normalized for term in ("texture", "material", "decal", "vfx", "visual"))
