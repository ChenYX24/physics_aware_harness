from __future__ import annotations

import math
from typing import Any

from harness.core.scene_layout import (
    SCENE_LAYOUT_SCHEMA_VERSION,
    allows_above_support,
    build_object_node,
    is_support_role,
    object_position,
)
from harness.runtime.camera_planner import camera_plan_from_case_spec, camera_plan_to_dict


def build_static_scene_layout(
    case_spec: dict[str, Any],
    *,
    asset_resolution: dict[str, Any] | None = None,
    requested_views: list[str] | None = None,
    camera_strategy: str = "bounds_auto_v1",
) -> dict[str, Any]:
    asset_rows = asset_rows_by_object_id(asset_resolution)
    nodes = [build_object_node(obj, asset_rows.get(str(obj.get("id")))) for obj in case_spec.get("objects", []) if isinstance(obj, dict)]
    camera_plan = camera_plan_to_dict(camera_plan_from_case_spec(case_spec, requested_views=requested_views, camera_strategy=camera_strategy))
    expected_physics = case_spec.get("expected_physics") or {}
    collision_edges = normalize_edges(expected_physics.get("collision_graph") or expected_physics.get("contact_order") or [])
    if not collision_edges:
        collision_edges = infer_collision_edges(nodes)
    support_relations = infer_support_relations(case_spec, nodes)
    overlap_pairs = find_overlap_pairs(nodes)
    return {
        "schema_version": SCENE_LAYOUT_SCHEMA_VERSION,
        "capability_id": "static_scene_placement",
        "case_id": case_spec.get("case_id"),
        "source_capability_id": case_spec.get("capability_id"),
        "coordinate_system": expected_physics.get("coordinate_system", "z_up"),
        "stage_id": "static_scene_layout",
        "object_nodes": nodes,
        "support_relations": support_relations,
        "overlap_pairs": overlap_pairs,
        "physics_graph": {
            "nodes": [node["object_id"] for node in nodes if node.get("physics_graph_member")],
            "collision_edges": collision_edges,
        },
        "camera_plan": camera_plan,
        "asset_resolution_summary": summarize_asset_resolution(asset_resolution),
        "expected_invariants": [
            "unique_object_ids",
            "physics_critical_asset_binding",
            "no_initial_overlap",
            "explicit_support_relation",
            "camera_plan_available",
        ],
    }


def asset_rows_by_object_id(asset_resolution: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not isinstance(asset_resolution, dict):
        return rows
    for row in asset_resolution.get("assets", []):
        if not isinstance(row, dict):
            continue
        intent = row.get("intent") or {}
        object_id = intent.get("object_id")
        if object_id:
            rows[str(object_id)] = row
    return rows


def infer_support_relations(case_spec: dict[str, Any], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected = case_spec.get("expected_physics") or {}
    by_id = {node["object_id"]: node for node in nodes}
    support_nodes = [node for node in nodes if is_support_role(str(node.get("role")))]
    relations: list[dict[str, Any]] = []
    for node in nodes:
        if not node.get("physics_critical") or is_support_role(str(node.get("role"))):
            continue
        support_id = support_id_for_node(node, expected, support_nodes)
        support_node = by_id.get(support_id) if support_id else None
        relations.append(support_relation(node, support_node))
    return relations


def support_id_for_node(node: dict[str, Any], expected: dict[str, Any], support_nodes: list[dict[str, Any]]) -> str | None:
    object_id = str(node.get("object_id"))
    support = expected.get("support")
    if isinstance(support, dict):
        value = support.get(object_id) or support.get("default")
        if value:
            return str(value)
    if isinstance(support, str):
        return support
    contact_surface = expected.get("contact_surface")
    if isinstance(contact_surface, dict):
        value = contact_surface.get(object_id) or contact_surface.get("default")
        if value:
            return str(value)
    if isinstance(contact_surface, str):
        return contact_surface
    return str(support_nodes[0]["object_id"]) if support_nodes else None


def support_relation(node: dict[str, Any], support_node: dict[str, Any] | None) -> dict[str, Any]:
    if support_node is None:
        if allows_above_support(str(node.get("role"))):
            return {
                "object_id": node["object_id"],
                "support_id": None,
                "status": "free_body_allowed",
                "vertical_gap_m": None,
            }
        return {
            "object_id": node["object_id"],
            "support_id": None,
            "status": "missing_support",
            "vertical_gap_m": None,
        }
    bottom = float((node.get("bounds") or {}).get("bottom_z", 0.0))
    support_top = float((support_node.get("bounds") or {}).get("top_z", 0.0))
    gap = round(bottom - support_top, 6)
    if gap < -0.01:
        status = "penetrating_support"
    elif abs(gap) <= 0.01:
        status = "contact_at_rest"
    elif allows_above_support(str(node.get("role"))):
        status = "above_support"
    else:
        status = "unsupported_gap"
    return {
        "object_id": node["object_id"],
        "support_id": support_node["object_id"],
        "status": status,
        "vertical_gap_m": gap,
    }


def find_overlap_pairs(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    collidable = [node for node in nodes if node.get("physics_critical") and not is_support_role(str(node.get("role")))]
    for index, left in enumerate(collidable):
        for right in collidable[index + 1 :]:
            left_pos = object_position(left)
            right_pos = object_position(right)
            distance = math.dist(left_pos, right_pos)
            left_radius = float((left.get("bounds") or {}).get("bounding_radius_m", 0.0))
            right_radius = float((right.get("bounds") or {}).get("bounding_radius_m", 0.0))
            threshold = max((left_radius + right_radius) * 0.5, 0.01)
            if distance < threshold:
                pairs.append(
                    {
                        "object_ids": [left["object_id"], right["object_id"]],
                        "distance_m": round(distance, 6),
                        "threshold_m": round(threshold, 6),
                    }
                )
    return pairs


def normalize_edges(raw_edges: Any) -> list[list[str]]:
    edges: list[list[str]] = []
    if not isinstance(raw_edges, list):
        return edges
    for item in raw_edges:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            edges.append([str(item[0]), str(item[1])])
        elif isinstance(item, dict):
            left = item.get("from") or item.get("source") or item.get("a")
            right = item.get("to") or item.get("target") or item.get("b")
            if left and right:
                edges.append([str(left), str(right)])
    return edges


def infer_collision_edges(nodes: list[dict[str, Any]]) -> list[list[str]]:
    ids = [str(node["object_id"]) for node in nodes if node.get("physics_critical")]
    return [[ids[index], ids[index + 1]] for index in range(len(ids) - 1)]


def summarize_asset_resolution(asset_resolution: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(asset_resolution, dict):
        return {
            "available": False,
            "physics_critical_count": 0,
            "resolved_count": 0,
            "proxy_or_fallback_count": 0,
        }
    rows = [row for row in asset_resolution.get("assets", []) if isinstance(row, dict)]
    return {
        "available": True,
        "physics_critical_count": asset_resolution.get("physics_critical_count", 0),
        "resolved_count": sum(1 for row in rows if row.get("selected_asset")),
        "proxy_or_fallback_count": sum(1 for row in rows if row.get("fallback_reason")),
    }
