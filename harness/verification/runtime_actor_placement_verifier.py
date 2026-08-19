from __future__ import annotations

import math
from typing import Any

from harness.runtime.actor_placement import (
    RUNTIME_ACTOR_PLACEMENT_SCHEMA_VERSION,
    actorless_world_anchor_object_ids,
    constraint_frame_in_world,
    world_constraint_endpoint_object_ids,
)


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
    constraint_error = first_bad_constraint_binding(case_spec, placement, by_object)
    if constraint_error:
        return fail_report(
            case_id,
            "F7_runtime_artifact_incomplete",
            constraint_error["constraint_id"],
            constraint_error["metric"],
            constraint_error["value"],
            checks=checks(placement, bindings),
        )
    missing_physics_object = first_missing_physics_object(case_spec, by_object)
    if missing_physics_object:
        return fail_report(case_id, "F7_runtime_artifact_incomplete", missing_physics_object, "missing_runtime_actor_binding", missing_physics_object, checks=checks(placement, bindings))
    invalid_specialized_asset = first_invalid_specialized_asset(case_spec, by_object)
    if invalid_specialized_asset:
        return fail_report(
            case_id,
            "F2_asset_missing",
            invalid_specialized_asset["object_id"],
            invalid_specialized_asset["metric"],
            invalid_specialized_asset["value"],
            checks=checks(placement, bindings),
        )
    contract_mismatch = first_structured_physics_contract_mismatch(case_spec, by_object)
    if contract_mismatch:
        return fail_report(
            case_id,
            "F3_invalid_initial_physics_state",
            contract_mismatch["object_id"],
            contract_mismatch["metric"],
            contract_mismatch["value"],
            checks=checks(placement, bindings),
        )
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
    actorless_anchor_ids = actorless_world_anchor_object_ids(case_spec)
    for obj in case_spec.get("objects") or []:
        if not isinstance(obj, dict):
            continue
        object_id = str(obj.get("id") or "")
        if not object_id:
            continue
        if object_id not in by_object and object_id not in actorless_anchor_ids and is_physics_contract_object(obj):
            return object_id
    return None


def first_bad_constraint_binding(
    case_spec: dict[str, Any],
    placement: dict[str, Any],
    by_object: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    expected = {
        str(item.get("id") or ""): item
        for item in case_spec.get("constraints") or []
        if isinstance(item, dict) and item.get("id")
    }
    actual = {
        str(item.get("constraint_id") or ""): item
        for item in placement.get("constraint_bindings") or []
        if isinstance(item, dict) and item.get("constraint_id")
    }
    if set(expected) != set(actual):
        return {
            "constraint_id": sorted(set(expected) ^ set(actual))[0],
            "metric": "missing_runtime_constraint_binding",
            "value": {"expected": sorted(expected), "actual": sorted(actual)},
        }
    objects_by_id = {
        str(obj.get("id") or ""): obj
        for obj in case_spec.get("objects") or []
        if isinstance(obj, dict) and obj.get("id")
    }
    world_anchor_ids = world_constraint_endpoint_object_ids(case_spec)
    actorless_anchor_ids = actorless_world_anchor_object_ids(case_spec)
    for constraint_id, declaration in expected.items():
        binding = actual[constraint_id]
        for side in ("a", "b"):
            object_id = str(declaration.get(f"body_{side}") or "")
            body_binding = binding.get(f"body_{side}") if isinstance(binding.get(f"body_{side}"), dict) else {}
            frame = binding.get(f"frame_{side}")
            if object_id in world_anchor_ids:
                expected_frame = constraint_frame_in_world(objects_by_id[object_id], declaration[f"frame_{side}"])
                valid = bool(
                    (object_id in by_object) == (object_id not in actorless_anchor_ids)
                    and body_binding.get("endpoint_type") == "world_anchor"
                    and body_binding.get("object_id") == object_id
                    and body_binding.get("frame_space") == "world"
                    and not body_binding.get("runtime_actor_id")
                    and frame == expected_frame
                )
            else:
                valid = bool(
                    object_id in by_object
                    and body_binding.get("endpoint_type") == "rigid_body"
                    and body_binding.get("object_id") == object_id
                    and body_binding.get("runtime_actor_id") == by_object[object_id].get("runtime_actor_id")
                    and body_binding.get("frame_space") == "body_local"
                    and frame == declaration.get(f"frame_{side}")
                )
            if not valid:
                return {
                    "constraint_id": constraint_id,
                    "metric": "invalid_runtime_constraint_body_binding",
                    "value": {"side": side, "object_id": object_id, "binding": body_binding},
                }
    return None


def first_structured_physics_contract_mismatch(
    case_spec: dict[str, Any],
    by_object: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    for obj in case_spec.get("objects") or []:
        if not isinstance(obj, dict):
            continue
        object_id = str(obj.get("id") or "")
        binding = by_object.get(object_id)
        if not binding:
            continue
        physics = binding.get("physics") if isinstance(binding.get("physics"), dict) else {}
        body_type = str(obj.get("body_type") or "").casefold()
        state_kind = str(physics.get("state_kind") or "rigid").casefold()
        if state_kind != "particle" and body_type == "dynamic" and physics.get("simulate_physics") is not True:
            return {
                "object_id": object_id,
                "metric": "dynamic_object_not_simulated",
                "value": physics.get("simulate_physics"),
            }
        if body_type in {"static", "kinematic"} and physics.get("simulate_physics") is True:
            return {
                "object_id": object_id,
                "metric": "non_dynamic_object_simulated",
                "value": physics.get("simulate_physics"),
            }
        if obj.get("collision_required") is True and physics.get("collision_enabled") is not True:
            return {
                "object_id": object_id,
                "metric": "required_collision_not_enabled",
                "value": physics.get("collision_enabled"),
            }
    return None


def is_physics_contract_object(obj: dict[str, Any]) -> bool:
    return (
        str(obj.get("body_type") or "").casefold() == "dynamic"
        or obj.get("collision_required") is True
        or is_physics_critical_role(str(obj.get("role") or ""))
    )


def first_invalid_specialized_asset(
    case_spec: dict[str, Any],
    by_object: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    for obj in case_spec.get("objects") or []:
        if not isinstance(obj, dict):
            continue
        response = obj.get("fracture_response")
        if not isinstance(response, dict) or response.get("mode") != "contact_external_strain":
            continue
        object_id = str(obj.get("id") or "")
        binding = by_object.get(object_id) or {}
        asset = binding.get("asset") if isinstance(binding.get("asset"), dict) else {}
        kind = str(asset.get("asset_kind") or "").casefold().replace(" ", "_")
        if kind not in {"geometrycollection", "geometry_collection"} or not asset.get("ue_path"):
            return {
                "object_id": object_id,
                "metric": "fracture_asset_must_be_geometry_collection",
                "value": {
                    "asset_kind": asset.get("asset_kind"),
                    "ue_path": asset.get("ue_path"),
                    "proxy": bool(asset.get("proxy")),
                },
            }
    return None


def first_bad_physics_binding(bindings: list[dict[str, Any]]) -> dict[str, Any] | None:
    for binding in bindings:
        if not binding.get("physics_critical"):
            continue
        object_id = str(binding.get("object_id") or "")
        asset = binding.get("asset") if isinstance(binding.get("asset"), dict) else {}
        physics = binding.get("physics") if isinstance(binding.get("physics"), dict) else {}
        visual = binding.get("visual_representation") if isinstance(binding.get("visual_representation"), dict) else {}
        visual_source = str(visual.get("source") or "asset")
        render_binding = binding.get("render_binding") if isinstance(binding.get("render_binding"), dict) else {}
        if visual_source == "solver_generated":
            cache_contract = render_binding.get("cache_contract") if isinstance(render_binding.get("cache_contract"), dict) else {}
            if (
                render_binding.get("kind") != "solver_generated"
                or render_binding.get("solver_declared") is not True
                or not str(cache_contract.get("contract_id") or "")
                or not str(cache_contract.get("schema_version") or "")
                or not str(cache_contract.get("producer_backend") or "")
                or not str(cache_contract.get("consumer_backend") or "")
                or not isinstance(cache_contract.get("required_artifacts"), list)
                or not cache_contract.get("required_artifacts")
                or not str(cache_contract.get("adapter_contract") or "")
            ):
                return {
                    "failure_type": "F7_runtime_artifact_incomplete",
                    "object_id": object_id,
                    "metric": "solver_generated_render_cache_binding",
                    "value": render_binding,
                }
        elif visual_source == "asset":
            if not asset.get("ue_path") and not asset.get("proxy"):
                return {"failure_type": "F2_asset_missing", "object_id": object_id, "metric": "missing_asset_or_proxy_binding", "value": None}
            quality_gate = asset.get("quality_gate")
            if asset.get("ue_path") and (
                not isinstance(quality_gate, dict)
                or quality_gate.get("status") not in {"pass", "pass_local_preview"}
            ):
                return {"failure_type": "F2_asset_missing", "object_id": object_id, "metric": "asset_quality_gate", "value": quality_gate}
        if visual_source == "asset" and asset.get("ue_path") and asset.get("scale_policy") == "fit_uniform_to_approx_size":
            instance_scale = asset.get("instance_scale")
            valid_scale = bool(
                asset.get("scale_applied") is True
                and isinstance(instance_scale, list)
                and len(instance_scale) == 3
                and all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    and float(value) > 0.0
                    for value in instance_scale
                )
                and max(float(value) for value in instance_scale)
                - min(float(value) for value in instance_scale)
                <= 1e-6
            )
            if not valid_scale:
                return {
                    "failure_type": "F3_invalid_initial_physics_state",
                    "object_id": object_id,
                    "metric": "invalid_uniform_asset_instance_scale",
                    "value": {
                        "scale_applied": asset.get("scale_applied"),
                        "instance_scale": instance_scale,
                    },
                }
        if physics.get("collision_enabled") and not physics.get("collider"):
            return {"failure_type": "F3_invalid_initial_physics_state", "object_id": object_id, "metric": "missing_collider", "value": None}
        if physics.get("collision_enabled") and not physics.get("collision_profile"):
            return {"failure_type": "F3_invalid_initial_physics_state", "object_id": object_id, "metric": "missing_collision_profile", "value": None}
        if physics.get("collision_enabled") and physics.get("collision_geometry_verification") not in {
            "runtime_controlled",
            "body_setup_verified",
            "asset_body_setup_reflected",
        }:
            return {
                "failure_type": "F2_asset_missing",
                "object_id": object_id,
                "metric": "collision_binding_unverified",
                "value": physics.get("collision_geometry_verification"),
            }
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
        "solver_generated_actor_count": sum(
            1
            for binding in bindings
            if ((binding.get("render_binding") or {}).get("kind") == "solver_generated")
        ),
        "local_preview_asset_count": sum(
            1
            for binding in bindings
            if (((binding.get("asset") or {}).get("quality_gate") or {}).get("status") == "pass_local_preview")
        ),
        "camera_count": len(placement.get("camera_bindings") or []),
        "constraint_count": len(placement.get("constraint_bindings") or []),
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
        return [
            "Resolve a selected UE asset before runtime actor placement; contact_external_strain fracture requires a real Geometry Collection and cannot use an analytic proxy."
        ]
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
