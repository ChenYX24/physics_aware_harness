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
    camera_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    asset_rows = asset_rows_by_object_id(asset_resolution)
    nodes = [build_object_node(obj, asset_rows.get(str(obj.get("id")))) for obj in case_spec.get("objects", []) if isinstance(obj, dict)]
    compiled_camera_plan = (
        camera_plan
        if camera_plan is not None
        else camera_plan_to_dict(
            camera_plan_from_case_spec(
                case_spec,
                requested_views=requested_views,
                camera_strategy=camera_strategy,
            )
        )
    )
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
        "camera_plan": compiled_camera_plan,
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
    support_nodes = [node for node in nodes if is_support_node(node)]
    relations: list[dict[str, Any]] = []
    for node in nodes:
        if not node.get("physics_critical") or not requires_support_relation(node):
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
        if allows_free_initial_motion(node):
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
    gap = inclined_surface_gap(node, support_node)
    if gap is None:
        bottom = float((node.get("bounds") or {}).get("bottom_z", 0.0))
        support_top = float((support_node.get("bounds") or {}).get("top_z", 0.0))
        gap = round(bottom - support_top, 6)
    if gap < -0.01:
        status = "penetrating_support"
    elif abs(gap) <= 0.01:
        status = "contact_at_rest"
    elif allows_free_initial_motion(node):
        status = "above_support"
    else:
        status = "unsupported_gap"
    return {
        "object_id": node["object_id"],
        "support_id": support_node["object_id"],
        "status": status,
        "vertical_gap_m": gap,
    }


def is_support_node(node: dict[str, Any]) -> bool:
    physics = node.get("physics") if isinstance(node.get("physics"), dict) else {}
    body_type = str(physics.get("body_type") or "").casefold()
    if body_type in {"static", "kinematic"} and physics.get("collision_required") is True:
        return True
    return is_support_role(str(node.get("role")))


def requires_support_relation(node: dict[str, Any]) -> bool:
    physics = node.get("physics") if isinstance(node.get("physics"), dict) else {}
    body_type = str(physics.get("body_type") or "").casefold()
    if body_type:
        return body_type == "dynamic"
    return not is_support_role(str(node.get("role")))


def allows_free_initial_motion(node: dict[str, Any]) -> bool:
    physics = node.get("physics") if isinstance(node.get("physics"), dict) else {}
    body_type = str(physics.get("body_type") or "").casefold()
    if body_type:
        return body_type == "dynamic"
    return allows_above_support(str(node.get("role")))


def inclined_surface_gap(node: dict[str, Any], support_node: dict[str, Any]) -> float | None:
    shape_role = f"{support_node.get('shape', '')} {support_node.get('role', '')}".casefold()
    if not any(token in shape_role for token in ("ramp", "inclined", "slope")):
        return None
    pitch = math.radians(float(((support_node.get("transform") or {}).get("rotation_deg") or [0.0])[0]))
    normal = [math.sin(pitch), 0.0, math.cos(pitch)]
    node_position = object_position(node)
    support_position = object_position(support_node)
    center_delta = [node_position[axis] - support_position[axis] for axis in range(3)]
    support_half_thickness = object_extents(support_node)[2]
    node_extents = object_extents(node)
    subject_radius = max(node_extents) if "sphere" in str(node.get("shape") or "").casefold() else sum(abs(normal[axis]) * node_extents[axis] for axis in range(3))
    return round(sum(center_delta[axis] * normal[axis] for axis in range(3)) - support_half_thickness - subject_radius, 6)


def find_overlap_pairs(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    collidable = [node for node in nodes if node.get("physics_critical") and not is_support_role(str(node.get("role")))]
    for index, left in enumerate(collidable):
        for right in collidable[index + 1 :]:
            left_pos = object_position(left)
            right_pos = object_position(right)
            distance = math.dist(left_pos, right_pos)
            left_extents = object_extents(left)
            right_extents = object_extents(right)
            axis_thresholds = [
                max((left_extents[axis] + right_extents[axis]) * 0.92, 0.001)
                for axis in range(3)
            ]
            axis_distances = [abs(left_pos[axis] - right_pos[axis]) for axis in range(3)]
            if all(axis_distances[axis] < axis_thresholds[axis] for axis in range(3)):
                pairs.append(
                    {
                        "object_ids": [left["object_id"], right["object_id"]],
                        "distance_m": round(distance, 6),
                        "axis_distances_m": [round(value, 6) for value in axis_distances],
                        "axis_thresholds_m": [round(value, 6) for value in axis_thresholds],
                    }
                )
    return pairs


def object_extents(node: dict[str, Any]) -> list[float]:
    extents = (node.get("bounds") or {}).get("extents_m") or [0.0, 0.0, 0.0]
    if not isinstance(extents, list):
        return [0.0, 0.0, 0.0]
    padded = [*extents, 0.0, 0.0, 0.0]
    return [float(padded[0]), float(padded[1]), float(padded[2])]


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
        "proxy_or_fallback_count": sum(
            1
            for row in rows
            if row.get("fallback_reason") or bool((row.get("selected_asset") or {}).get("proxy"))
        ),
    }
