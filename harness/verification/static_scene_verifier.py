from __future__ import annotations

from typing import Any

from harness.core.scene_layout import SCENE_LAYOUT_SCHEMA_VERSION


def verify_static_scene_layout(case_spec: dict[str, Any], scene_layout: dict[str, Any] | None) -> dict[str, Any]:
    case_id = str(case_spec.get("case_id") or "")
    if not isinstance(scene_layout, dict) or scene_layout.get("schema_version") != SCENE_LAYOUT_SCHEMA_VERSION:
        return static_report(
            case_id,
            "fail",
            "F1_scene_parsing_failure",
            {"object_id": case_id, "frame": 0, "time": 0.0, "metric": "scene_layout_schema", "value": scene_layout.get("schema_version") if isinstance(scene_layout, dict) else None},
            [],
            ["Generate a schema-valid scene_layout.json before runtime."],
            {},
        )
    nodes = scene_layout.get("object_nodes") if isinstance(scene_layout.get("object_nodes"), list) else []
    checks = {
        "object_count": len(nodes),
        "physics_critical_count": sum(1 for node in nodes if isinstance(node, dict) and node.get("physics_critical")),
        "support_relation_count": len(scene_layout.get("support_relations") or []),
        "overlap_pair_count": len(scene_layout.get("overlap_pairs") or []),
        "camera_count": len(((scene_layout.get("camera_plan") or {}).get("views") or [])),
    }
    duplicate = first_duplicate([str(node.get("object_id")) for node in nodes if isinstance(node, dict)])
    if duplicate:
        return fail_report(case_id, "F1_scene_parsing_failure", "duplicate_object_id", duplicate, checks)
    missing_asset = first_missing_physics_asset(nodes)
    if missing_asset:
        return fail_report(case_id, "F2_asset_missing", "missing_physics_asset_binding", missing_asset, checks)
    overlap_pairs = scene_layout.get("overlap_pairs") or []
    if overlap_pairs:
        return fail_report(case_id, "F3_invalid_initial_physics_state", "initial_overlap_pair", overlap_pairs[0], checks)
    bad_support = first_bad_support(scene_layout.get("support_relations") or [])
    if bad_support:
        return fail_report(case_id, "F3_invalid_initial_physics_state", "invalid_support_relation", bad_support, checks)
    if checks["camera_count"] == 0:
        return fail_report(case_id, "F7_runtime_artifact_incomplete", "missing_camera_plan", None, checks)
    return static_report(
        case_id,
        "pass",
        None,
        None,
        [{"type": "static_scene_checks", "checks": checks}],
        [],
        checks,
    )


def first_missing_physics_asset(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    for node in nodes:
        if not isinstance(node, dict) or not node.get("physics_critical"):
            continue
        binding = node.get("asset_binding") or {}
        physics = node.get("physics") or {}
        selected = binding.get("selected_asset_id")
        fallback = binding.get("fallback_reason")
        if not selected and not fallback:
            return {"object_id": node.get("object_id"), "reason": "no selected asset or fallback reason"}
        for key in ("collider", "mass_kg", "collision_profile"):
            value = physics.get(key)
            if value is None:
                return {"object_id": node.get("object_id"), "missing_property": key}
        if physics.get("material") is None and not str(node.get("role", "")).endswith("anchor"):
            return {"object_id": node.get("object_id"), "missing_property": "material"}
    return None


def first_bad_support(relations: list[dict[str, Any]]) -> dict[str, Any] | None:
    bad_statuses = {"missing_support", "penetrating_support", "unsupported_gap"}
    for relation in relations:
        if isinstance(relation, dict) and relation.get("status") in bad_statuses:
            return relation
    return None


def first_duplicate(values: list[str]) -> str | None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


def fail_report(case_id: str, failure_type: str, metric: str, value: Any, checks: dict[str, Any]) -> dict[str, Any]:
    object_id = value.get("object_id") if isinstance(value, dict) else case_id
    return static_report(
        case_id,
        "fail",
        failure_type,
        {"object_id": object_id or case_id, "frame": 0, "time": 0.0, "metric": metric, "value": value},
        [{"type": "static_scene_checks", "checks": checks}],
        repair_suggestions(failure_type),
        checks,
    )


def static_report(
    case_id: str,
    status: str,
    failure_type: str | None,
    first_failure: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
    repair_suggestions: list[str],
    checks: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "harness_static_scene_report_v1",
        "case_id": case_id,
        "capability_id": "static_scene_placement",
        "status": status,
        "failure_type": failure_type,
        "first_failure": first_failure,
        "evidence": evidence,
        "repair_suggestions": repair_suggestions,
        "checks": checks,
        "artifact_completeness": {
            "scene_layout": True,
            "camera_plan": checks.get("camera_count", 0) > 0 if checks else False,
            "object_nodes": checks.get("object_count", 0) > 0 if checks else False,
        },
    }


def repair_suggestions(failure_type: str) -> list[str]:
    if failure_type == "F2_asset_missing":
        return ["Resolve physics-critical assets or mark analytic proxies with collider, mass, material, and collision profile."]
    if failure_type == "F3_invalid_initial_physics_state":
        return ["Adjust initial transforms so objects do not overlap or penetrate supports before runtime."]
    if failure_type == "F7_runtime_artifact_incomplete":
        return ["Regenerate camera_plan.json from scene bounds before runtime."]
    return ["Regenerate a schema-valid static scene layout."]
