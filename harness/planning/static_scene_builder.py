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
    placement_adjustments = align_v2_explicit_supports(case_spec, nodes)
    expected_physics = case_spec.get("expected_physics") or {}
    collision_edges = normalize_edges(expected_physics.get("collision_graph") or expected_physics.get("contact_order") or [])
    if not collision_edges:
        collision_edges = infer_collision_edges(nodes)
    overlap_adjustments = separate_v2_dynamic_overlaps(case_spec, nodes, collision_edges)
    placement_adjustments.extend(overlap_adjustments)
    if overlap_adjustments:
        placement_adjustments.extend(align_v2_explicit_supports(case_spec, nodes))
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
        "placement_adjustments": placement_adjustments,
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


def align_v2_explicit_supports(case_spec: dict[str, Any], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Snap explicitly supported V2 bodies to resolved support geometry."""
    projection = case_spec.get("v2_projection") if isinstance(case_spec.get("v2_projection"), dict) else {}
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    support_map = expected.get("support") if isinstance(expected.get("support"), dict) else {}
    if projection.get("source_schema_version") != "harness_case_spec_v2" or not support_map:
        return []
    by_id = {str(node.get("object_id")): node for node in nodes}
    adjustments: list[dict[str, Any]] = []
    clearance_m = 0.003
    for object_id, support_id in support_map.items():
        node = by_id.get(str(object_id))
        support = by_id.get(str(support_id))
        if node is None or support is None:
            continue
        physics = node.get("physics") if isinstance(node.get("physics"), dict) else {}
        if str(physics.get("body_type") or "").casefold() != "dynamic":
            # Static/kinematic supports may intentionally touch multiple other
            # supports (for example both ends of an inclined ramp). Their
            # authored CaseSpec transform is authoritative; a one-support snap
            # would flatten or translate the complete structure.
            continue
        gap = inclined_surface_gap(node, support)
        normal_z = 1.0
        if gap is None:
            gap = round(
                float((node.get("bounds") or {}).get("bottom_z", 0.0))
                - float((support.get("bounds") or {}).get("top_z", 0.0)),
                6,
            )
        else:
            pitch = math.radians(float(((support.get("transform") or {}).get("rotation_deg") or [0.0])[0]))
            normal_z = math.cos(pitch)
        if abs(normal_z) < 1e-6:
            continue
        delta_z = (clearance_m - gap) / normal_z
        if abs(delta_z) <= 1e-6:
            continue
        transform = node.setdefault("transform", {})
        position = object_position(node)
        position[2] = round(position[2] + delta_z, 6)
        transform["position_m"] = position
        bounds = node.setdefault("bounds", {})
        for key in ("bottom_z", "top_z"):
            if bounds.get(key) is not None:
                bounds[key] = round(float(bounds[key]) + delta_z, 6)
        adjustments.append(
            {
                "object_id": str(object_id),
                "support_id": str(support_id),
                "type": "explicit_support_surface_snap",
                "delta_z_m": round(delta_z, 6),
                "clearance_m": clearance_m,
            }
        )
    return adjustments


def separate_v2_dynamic_overlaps(
    case_spec: dict[str, Any],
    nodes: list[dict[str, Any]],
    collision_edges: list[list[str]],
    *,
    clearance_m: float = 0.005,
) -> list[dict[str, Any]]:
    """Apply the smallest inferable horizontal separation to V2 dynamic chains.

    The LLM-provided order and direction remain authoritative.  Exact co-location,
    deep disagreement, static geometry, and unrelated bodies remain hard failures.
    """
    projection = case_spec.get("v2_projection") if isinstance(case_spec.get("v2_projection"), dict) else {}
    if projection.get("source_schema_version") != "harness_case_spec_v2":
        return []
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    support_map = expected.get("support") if isinstance(expected.get("support"), dict) else {}
    edge_pairs = {
        frozenset((str(edge[0]), str(edge[1])))
        for edge in collision_edges
        if isinstance(edge, list) and len(edge) >= 2
    }
    adjustments: list[dict[str, Any]] = []
    maximum_passes = max(1, len(nodes) * 2)
    for _ in range(maximum_passes):
        changed = False
        for left_index, left in enumerate(nodes):
            if not is_dynamic_collidable(left):
                continue
            for right in nodes[left_index + 1 :]:
                if not is_dynamic_collidable(right):
                    continue
                left_id = str(left.get("object_id") or "")
                right_id = str(right.get("object_id") or "")
                same_support = bool(
                    support_map.get(left_id)
                    and support_map.get(left_id) == support_map.get(right_id)
                )
                if not same_support and frozenset((left_id, right_id)) not in edge_pairs:
                    continue
                left_position = object_position(left)
                right_position = object_position(right)
                left_extents = object_extents(left)
                right_extents = object_extents(right)
                axis_distances = [abs(left_position[axis] - right_position[axis]) for axis in range(3)]
                overlap_thresholds = [
                    max((left_extents[axis] + right_extents[axis]) * 0.92, 0.001)
                    for axis in range(3)
                ]
                if not all(axis_distances[axis] < overlap_thresholds[axis] for axis in range(3)):
                    continue
                horizontal_axes = sorted(
                    (0, 1),
                    key=lambda axis: axis_distances[axis] / max(left_extents[axis] + right_extents[axis], 0.001),
                    reverse=True,
                )
                axis = next((value for value in horizontal_axes if axis_distances[value] > 1e-6), None)
                if axis is None:
                    continue
                required_distance = left_extents[axis] + right_extents[axis] + clearance_m
                shift = required_distance - axis_distances[axis]
                maximum_safe_shift = max(0.25, required_distance * 0.75)
                if shift <= 1e-6 or shift > maximum_safe_shift:
                    continue
                direction = 1.0 if right_position[axis] > left_position[axis] else -1.0
                original_position = list(right_position)
                right_position[axis] = round(right_position[axis] + direction * shift, 6)
                right.setdefault("transform", {})["position_m"] = right_position
                adjustments.append(
                    {
                        "object_id": right_id,
                        "relative_to_object_id": left_id,
                        "type": "dynamic_overlap_bounds_separation",
                        "axis": "xyz"[axis],
                        "delta_m": round(direction * shift, 6),
                        "clearance_m": clearance_m,
                        "original_position_m": [round(value, 6) for value in original_position],
                        "position_m": list(right_position),
                        "bounds_source": "resolved_effective_asset_bounds",
                    }
                )
                changed = True
        if not changed:
            break
    return adjustments


def is_dynamic_collidable(node: dict[str, Any]) -> bool:
    physics = node.get("physics") if isinstance(node.get("physics"), dict) else {}
    return bool(
        str(physics.get("body_type") or "").casefold() == "dynamic"
        and physics.get("collision_required") is not False
        and node.get("physics_critical")
    )


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
    footprint_margins = support_footprint_margins(node, support_node)
    gap = inclined_surface_gap(node, support_node)
    if gap is None:
        bottom = float((node.get("bounds") or {}).get("bottom_z", 0.0))
        support_top = float((support_node.get("bounds") or {}).get("top_z", 0.0))
        gap = round(bottom - support_top, 6)
    if any(margin < -0.01 for margin in footprint_margins):
        status = "outside_support_footprint"
    elif gap < -0.01:
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
        "horizontal_margin_m": footprint_margins,
    }


def support_footprint_margins(node: dict[str, Any], support_node: dict[str, Any]) -> list[float]:
    node_position = object_position(node)
    support_position = object_position(support_node)
    node_extents = object_extents(node)
    support_extents = object_extents(support_node)
    return [
        round(support_extents[axis] - abs(node_position[axis] - support_position[axis]) - node_extents[axis], 6)
        for axis in (0, 1)
    ]


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
    # Match the UE runtime convention used by the registered ramp cases:
    # positive pitch makes local +X the downhill direction.
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
