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
    projection = case_spec.get("v2_projection") if isinstance(case_spec.get("v2_projection"), dict) else {}
    if not collision_edges and projection.get("source_schema_version") != "harness_case_spec_v2":
        collision_edges = infer_collision_edges(nodes)
    chain_adjustments = align_v2_ordered_dynamic_chain(case_spec, nodes, collision_edges)
    placement_adjustments.extend(chain_adjustments)
    if chain_adjustments:
        placement_adjustments.extend(align_v2_explicit_supports(case_spec, nodes))
    overlap_adjustments = separate_v2_dynamic_overlaps(case_spec, nodes, collision_edges)
    placement_adjustments.extend(overlap_adjustments)
    if overlap_adjustments:
        placement_adjustments.extend(align_v2_explicit_supports(case_spec, nodes))
    obstacle_adjustments = separate_v2_chain_from_static_obstacles(case_spec, nodes, collision_edges)
    placement_adjustments.extend(obstacle_adjustments)
    if obstacle_adjustments:
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
    effective_support_map = {
        str(relation["object_id"]): str(relation["support_id"])
        for relation in support_relations
        if relation.get("object_id") and relation.get("support_id")
    }
    overlap_pairs = find_overlap_pairs(
        nodes,
        support_map=effective_support_map,
        include_static_obstacles=projection.get("source_schema_version") == "harness_case_spec_v2",
    )
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
    # An explicit support relation describes a body already resting at frame
    # zero.  Adding an air gap here creates an unintended gravity-drop event.
    clearance_m = 0.0
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
        if abs(delta_z) > 1e-6:
            transform = node.setdefault("transform", {})
            position = object_position(node)
            position[2] = round(position[2] + delta_z, 6)
            transform["position_m"] = position
            translate_node_bounds(node, [0.0, 0.0, delta_z])
            adjustments.append(
                {
                    "object_id": str(object_id),
                    "support_id": str(support_id),
                    "type": "explicit_support_surface_snap",
                    "delta_z_m": round(delta_z, 6),
                    "clearance_m": clearance_m,
                }
            )
        footprint_adjustment = fit_dynamic_to_support_footprint(node, support)
        if footprint_adjustment is not None:
            adjustments.append(footprint_adjustment)
    return adjustments


def fit_dynamic_to_support_footprint(
    node: dict[str, Any],
    support_node: dict[str, Any],
    *,
    clearance_m: float = 0.005,
) -> dict[str, Any] | None:
    """Move a supported body inward only when its center is already on the support."""
    axes, support_half_extents = support_footprint_axes(support_node)
    node_position = object_position(node)
    support_position = object_position(support_node)
    center_delta = [node_position[index] - support_position[index] for index in range(3)]
    total_shift = [0.0, 0.0, 0.0]
    margins_before: list[float] = []
    for axis, support_half_extent in zip(axes, support_half_extents):
        coordinate = sum(center_delta[index] * axis[index] for index in range(3))
        subject_radius = projected_object_radius(node, axis)
        margins_before.append(round(support_half_extent - abs(coordinate) - subject_radius, 6))
        usable_half_extent = support_half_extent - subject_radius - clearance_m
        if usable_half_extent < 0.0 or abs(coordinate) > support_half_extent + 1e-6:
            return None
        fitted_coordinate = min(max(coordinate, -usable_half_extent), usable_half_extent)
        correction = fitted_coordinate - coordinate
        for index in range(3):
            total_shift[index] += correction * axis[index]
            center_delta[index] += correction * axis[index]
    if max(abs(value) for value in total_shift) <= 1e-6:
        return None
    original_position = list(node_position)
    fitted_position = [round(node_position[index] + total_shift[index], 6) for index in range(3)]
    node.setdefault("transform", {})["position_m"] = fitted_position
    translate_node_bounds(node, total_shift)
    return {
        "object_id": str(node.get("object_id") or ""),
        "support_id": str(support_node.get("object_id") or ""),
        "type": "explicit_support_footprint_fit",
        "original_position_m": [round(value, 6) for value in original_position],
        "position_m": fitted_position,
        "delta_position_m": [round(value, 6) for value in total_shift],
        "horizontal_margin_before_m": margins_before,
        "clearance_m": clearance_m,
        "bounds_source": "resolved_effective_asset_bounds",
    }


def translate_node_bounds(node: dict[str, Any], delta: list[float]) -> None:
    bounds = node.setdefault("bounds", {})
    if abs(delta[2]) <= 1e-12:
        return
    for key in ("bottom_z", "top_z"):
        if bounds.get(key) is not None:
            bounds[key] = round(float(bounds[key]) + delta[2], 6)


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
                left_extents = conservative_world_extents(left)
                right_extents = conservative_world_extents(right)
                sphere_radii = sphere_pair_radii(left, right)
                if sphere_radii is not None:
                    overlap_distance = sum(sphere_radii) - 1e-4
                    if math.dist(left_position, right_position) >= overlap_distance:
                        continue
                    horizontal_delta = [
                        right_position[axis] - left_position[axis]
                        for axis in range(2)
                    ]
                    horizontal_distance = math.hypot(*horizontal_delta)
                    required_center_distance = sum(sphere_radii) + clearance_m
                    vertical_distance = abs(right_position[2] - left_position[2])
                    if horizontal_distance <= 1e-6 or vertical_distance >= required_center_distance:
                        continue
                    required_horizontal_distance = math.sqrt(
                        required_center_distance * required_center_distance
                        - vertical_distance * vertical_distance
                    )
                    shift = required_horizontal_distance - horizontal_distance
                    maximum_safe_shift = max(0.25, required_center_distance * 0.75)
                    if shift <= 1e-6 or shift > maximum_safe_shift:
                        continue
                    original_position = list(right_position)
                    delta_position = [
                        shift * horizontal_delta[axis] / horizontal_distance
                        for axis in range(2)
                    ]
                    for axis in range(2):
                        right_position[axis] = round(right_position[axis] + delta_position[axis], 6)
                    right.setdefault("transform", {})["position_m"] = right_position
                    adjustments.append(
                        {
                            "object_id": right_id,
                            "relative_to_object_id": left_id,
                            "type": "dynamic_overlap_bounds_separation",
                            "axis": "xy_radial",
                            "delta_position_m": [
                                round(delta_position[0], 6),
                                round(delta_position[1], 6),
                                0.0,
                            ],
                            "clearance_m": clearance_m,
                            "original_position_m": [round(value, 6) for value in original_position],
                            "position_m": list(right_position),
                            "bounds_source": "resolved_effective_asset_bounds",
                            "overlap_test": "sphere_center_distance",
                        }
                    )
                    changed = True
                    continue
                axis_distances = [abs(left_position[axis] - right_position[axis]) for axis in range(3)]
                overlap_thresholds = [
                    max(left_extents[axis] + right_extents[axis] - 1e-4, 0.001)
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


def align_v2_ordered_dynamic_chain(
    case_spec: dict[str, Any],
    nodes: list[dict[str, Any]],
    collision_edges: list[list[str]],
    *,
    clearance_m: float = 0.005,
) -> list[dict[str, Any]]:
    """Align and tighten downstream edges in one simple sequential chain."""
    projection = case_spec.get("v2_projection") if isinstance(case_spec.get("v2_projection"), dict) else {}
    if (
        projection.get("source_schema_version") != "harness_case_spec_v2"
        or str(case_spec.get("capability_id") or "") != "sequential_contact_propagation"
    ):
        return []
    by_id = {str(node.get("object_id") or ""): node for node in nodes}
    edges = [
        (str(edge[0]), str(edge[1]))
        for edge in collision_edges
        if isinstance(edge, list)
        and len(edge) >= 2
        and str(edge[0]) in by_id
        and str(edge[1]) in by_id
        and is_dynamic_collidable(by_id[str(edge[0])])
        and is_dynamic_collidable(by_id[str(edge[1])])
    ]
    if len(edges) < 2:
        return []
    predecessors: dict[str, list[str]] = {}
    successors: dict[str, list[str]] = {}
    for source_id, target_id in edges:
        predecessors.setdefault(target_id, []).append(source_id)
        successors.setdefault(source_id, []).append(target_id)
    if any(len(values) > 1 for values in predecessors.values()) or any(len(values) > 1 for values in successors.values()):
        return []
    roots = sorted({source_id for source_id, _ in edges if source_id not in predecessors})
    if len(roots) != 1:
        return []
    ordered_ids = [roots[0]]
    while ordered_ids[-1] in successors:
        next_id = successors[ordered_ids[-1]][0]
        if next_id in ordered_ids:
            return []
        ordered_ids.append(next_id)
    if len(ordered_ids) != len(edges) + 1 or set(ordered_ids) != {value for edge in edges for value in edge}:
        return []
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    explicit_gaps = {
        (str(row.get("source") or ""), str(row.get("target") or "")): float(row["surface_gap_m"])
        for row in expected.get("collision_surface_gaps_m") or []
        if isinstance(row, dict)
        and row.get("source")
        and row.get("target")
        and isinstance(row.get("surface_gap_m"), (int, float))
        and not isinstance(row.get("surface_gap_m"), bool)
        and math.isfinite(float(row["surface_gap_m"]))
        and float(row["surface_gap_m"]) >= 0.0
    }
    adjustments: list[dict[str, Any]] = []
    root_position = object_position(by_id[ordered_ids[0]])
    first_position = object_position(by_id[ordered_ids[1]])
    first_delta = [first_position[index] - root_position[index] for index in range(2)]
    if max(abs(value) for value in first_delta) <= 1e-6:
        return []
    axis = 0 if abs(first_delta[0]) >= abs(first_delta[1]) else 1
    transverse_axis = 1 - axis
    sign = 1.0 if first_delta[axis] > 0.0 else -1.0
    # Preserve the authored launch gap on the first edge. Every later edge is a
    # passive propagation edge and can be placed at resolved-bounds clearance.
    for source_id, target_id in zip(ordered_ids[1:-1], ordered_ids[2:]):
        source = by_id[source_id]
        target = by_id[target_id]
        source_position = object_position(source)
        target_position = object_position(target)
        source_extent = conservative_world_extents(source)[axis]
        target_extent = conservative_world_extents(target)[axis]
        explicit_gap = explicit_gaps.get((source_id, target_id))
        target_gap = explicit_gap if explicit_gap is not None else clearance_m
        required_distance = source_extent + target_extent + target_gap
        primary_delta = sign * (target_position[axis] - source_position[axis])
        transverse_delta = abs(target_position[transverse_axis] - source_position[transverse_axis])
        surface_gap = primary_delta - source_extent - target_extent
        reversed_edge = primary_delta <= 1e-6
        transverse_edge = primary_delta < transverse_delta
        excessive_gap = surface_gap > clearance_m + 1e-6
        explicit_gap_mismatch = explicit_gap is not None and abs(surface_gap - explicit_gap) > 1e-6
        if not (reversed_edge or transverse_edge or (excessive_gap and explicit_gap is None) or explicit_gap_mismatch):
            continue
        original_position = list(target_position)
        target_position[axis] = round(source_position[axis] + sign * required_distance, 6)
        target_position[transverse_axis] = round(source_position[transverse_axis], 6)
        target.setdefault("transform", {})["position_m"] = target_position
        adjustments.append(
            {
                "object_id": target_id,
                "relative_to_object_id": source_id,
                "type": (
                    "ordered_chain_explicit_gap_alignment"
                    if explicit_gap is not None
                    else "ordered_chain_bounds_tightening"
                    if excessive_gap and not reversed_edge and not transverse_edge
                    else "ordered_chain_direction_alignment"
                ),
                "axis": "xyz"[axis],
                "original_position_m": [round(value, 6) for value in original_position],
                "position_m": list(target_position),
                "surface_gap_before_m": round(surface_gap, 6),
                "clearance_m": target_gap,
                "explicit_surface_gap": explicit_gap is not None,
                "bounds_source": "resolved_effective_asset_bounds",
            }
        )
    return adjustments


def separate_v2_chain_from_static_obstacles(
    case_spec: dict[str, Any],
    nodes: list[dict[str, Any]],
    collision_edges: list[list[str]],
    *,
    clearance_m: float = 0.005,
) -> list[dict[str, Any]]:
    """Move an ordered dynamic chain minimally past a static boundary overlap.

    Only a body already on the outgoing side of the obstacle is adjusted, and
    all of its downstream collision targets move by the same amount.  This
    preserves the authored chain while accounting for resolved asset bounds.
    """
    projection = case_spec.get("v2_projection") if isinstance(case_spec.get("v2_projection"), dict) else {}
    if projection.get("source_schema_version") != "harness_case_spec_v2":
        return []
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    by_id = {str(node.get("object_id") or ""): node for node in nodes}
    directed_edges = [
        (str(edge[0]), str(edge[1]))
        for edge in collision_edges
        if isinstance(edge, list) and len(edge) >= 2 and str(edge[0]) in by_id and str(edge[1]) in by_id
    ]
    predecessors: dict[str, list[str]] = {}
    successors: dict[str, list[str]] = {}
    for source_id, target_id in directed_edges:
        predecessors.setdefault(target_id, []).append(source_id)
        successors.setdefault(source_id, []).append(target_id)
    static_nodes = [node for node in nodes if is_static_collidable(node)]
    support_nodes = [node for node in nodes if is_support_node(node)]
    adjustments: list[dict[str, Any]] = []
    ordered_ids: list[str] = []
    for source_id, target_id in directed_edges:
        for object_id in (source_id, target_id):
            if object_id not in ordered_ids:
                ordered_ids.append(object_id)
    for object_id in ordered_ids:
        node = by_id[object_id]
        if not is_dynamic_collidable(node):
            continue
        direction = chain_horizontal_direction(node, predecessors.get(object_id, []), successors.get(object_id, []), by_id)
        if direction is None:
            continue
        axis, sign = direction
        for obstacle in static_nodes:
            obstacle_id = str(obstacle.get("object_id") or "")
            effective_support_id = support_id_for_node(node, expected, support_nodes)
            if effective_support_id == obstacle_id or not nodes_overlap_conservative(node, obstacle):
                continue
            node_position = object_position(node)
            obstacle_position = object_position(obstacle)
            # A minimal deterministic correction is safe only at the boundary
            # the chain is already leaving, never through an obstacle center.
            if sign * (node_position[axis] - obstacle_position[axis]) < -1e-6:
                continue
            node_extent = conservative_world_extents(node)[axis]
            obstacle_extent = conservative_world_extents(obstacle)[axis]
            boundary = obstacle_position[axis] + sign * (obstacle_extent + node_extent + clearance_m)
            shift = boundary - node_position[axis]
            if sign * shift <= 1e-6:
                continue
            moved_ids = downstream_ids(object_id, successors)
            for moved_id in moved_ids:
                moved = by_id.get(moved_id)
                if moved is None:
                    continue
                position = object_position(moved)
                position[axis] = round(position[axis] + shift, 6)
                moved.setdefault("transform", {})["position_m"] = position
            adjustments.append(
                {
                    "object_id": object_id,
                    "obstacle_id": obstacle_id,
                    "type": "dynamic_chain_static_boundary_clearance",
                    "axis": "xyz"[axis],
                    "delta_m": round(shift, 6),
                    "clearance_m": clearance_m,
                    "moved_object_ids": moved_ids,
                    "bounds_source": "resolved_effective_asset_bounds",
                }
            )
    return adjustments


def chain_horizontal_direction(
    node: dict[str, Any],
    predecessor_ids: list[str],
    successor_ids: list[str],
    by_id: dict[str, dict[str, Any]],
) -> tuple[int, float] | None:
    position = object_position(node)
    candidates: list[list[float]] = []
    for predecessor_id in predecessor_ids:
        predecessor = by_id.get(predecessor_id)
        if predecessor is not None:
            predecessor_position = object_position(predecessor)
            candidates.append([position[index] - predecessor_position[index] for index in range(3)])
    for successor_id in successor_ids:
        successor = by_id.get(successor_id)
        if successor is not None:
            successor_position = object_position(successor)
            candidates.append([successor_position[index] - position[index] for index in range(3)])
    vector = next((value for value in candidates if max(abs(value[0]), abs(value[1])) > 1e-6), None)
    if vector is None:
        return None
    axis = 0 if abs(vector[0]) >= abs(vector[1]) else 1
    return axis, 1.0 if vector[axis] > 0.0 else -1.0


def downstream_ids(object_id: str, successors: dict[str, list[str]]) -> list[str]:
    result: list[str] = []
    pending = [object_id]
    seen: set[str] = set()
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        result.append(current)
        pending.extend(successors.get(current, []))
    return result


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
    center_delta = [node_position[index] - support_position[index] for index in range(3)]
    axes, support_half_extents = support_footprint_axes(support_node)
    margins: list[float] = []
    for axis, support_half_extent in zip(axes, support_half_extents):
        coordinate = sum(center_delta[index] * axis[index] for index in range(3))
        subject_radius = projected_object_radius(node, axis)
        margins.append(round(support_half_extent - abs(coordinate) - subject_radius, 6))
    return margins


def support_footprint_axes(support_node: dict[str, Any]) -> tuple[list[list[float]], list[float]]:
    support_extents = object_extents(support_node)
    shape_role = f"{support_node.get('shape', '')} {support_node.get('role', '')}".casefold()
    if any(token in shape_role for token in ("ramp", "inclined", "slope")):
        pitch = math.radians(float(((support_node.get("transform") or {}).get("rotation_deg") or [0.0])[0]))
        return (
            [[math.cos(pitch), 0.0, -math.sin(pitch)], [0.0, 1.0, 0.0]],
            [support_extents[0], support_extents[1]],
        )
    return ([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], [support_extents[0], support_extents[1]])


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
    subject_radius = projected_object_radius(node, normal)
    return round(sum(center_delta[axis] * normal[axis] for axis in range(3)) - support_half_thickness - subject_radius, 6)


def find_overlap_pairs(
    nodes: list[dict[str, Any]],
    *,
    support_map: dict[str, Any] | None = None,
    include_static_obstacles: bool = False,
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    support_map = support_map if isinstance(support_map, dict) else {}
    collidable = [
        node
        for node in nodes
        if node.get("physics_critical")
        and (include_static_obstacles or not is_support_role(str(node.get("role"))))
    ]
    for index, left in enumerate(collidable):
        for right in collidable[index + 1 :]:
            if is_static_collidable(left) and is_static_collidable(right):
                continue
            left_id = str(left.get("object_id") or "")
            right_id = str(right.get("object_id") or "")
            if support_map.get(left_id) == right_id or support_map.get(right_id) == left_id:
                continue
            left_pos = object_position(left)
            right_pos = object_position(right)
            distance = math.dist(left_pos, right_pos)
            left_extents = conservative_world_extents(left)
            right_extents = conservative_world_extents(right)
            axis_thresholds = [
                max(left_extents[axis] + right_extents[axis] - 1e-4, 0.001)
                for axis in range(3)
            ]
            axis_distances = [abs(left_pos[axis] - right_pos[axis]) for axis in range(3)]
            sphere_radii = sphere_pair_radii(left, right)
            overlaps = (
                distance < sum(sphere_radii) - 1e-4
                if sphere_radii is not None
                else all(axis_distances[axis] < axis_thresholds[axis] for axis in range(3))
            )
            if overlaps:
                pairs.append(
                    {
                        "object_ids": [left["object_id"], right["object_id"]],
                        "distance_m": round(distance, 6),
                        "axis_distances_m": [round(value, 6) for value in axis_distances],
                        "axis_thresholds_m": [round(value, 6) for value in axis_thresholds],
                        "overlap_test": (
                            "sphere_center_distance"
                            if sphere_radii is not None
                            else "axis_aligned_bounds"
                        ),
                    }
                )
    return pairs


def is_static_collidable(node: dict[str, Any]) -> bool:
    physics = node.get("physics") if isinstance(node.get("physics"), dict) else {}
    return bool(
        str(physics.get("body_type") or "").casefold() in {"static", "kinematic"}
        and physics.get("collision_required") is not False
        and node.get("physics_critical")
    )


def nodes_overlap_conservative(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_position = object_position(left)
    right_position = object_position(right)
    left_extents = conservative_world_extents(left)
    right_extents = conservative_world_extents(right)
    return all(
        abs(left_position[axis] - right_position[axis])
        < left_extents[axis] + right_extents[axis] - 1e-4
        for axis in range(3)
    )


def sphere_pair_radii(left: dict[str, Any], right: dict[str, Any]) -> tuple[float, float] | None:
    radii: list[float] = []
    for node in (left, right):
        physics = node.get("physics") if isinstance(node.get("physics"), dict) else {}
        shape = str(node.get("shape") or "").casefold()
        collider = str(physics.get("collider") or "").casefold()
        if "sphere" not in shape and collider != "sphere":
            return None
        radius = max(object_extents(node))
        if radius <= 0.0:
            return None
        radii.append(radius)
    return radii[0], radii[1]


def conservative_world_extents(node: dict[str, Any]) -> list[float]:
    local_extents = object_extents(node)
    world_extents = [
        projected_object_radius(node, [1.0 if axis == index else 0.0 for axis in range(3)])
        for index in range(3)
    ]
    # Native UE contact sidecars historically reported unrotated component
    # bounds.  The maximum keeps preflight conservative until those artifacts
    # and the runtime collider share the same oriented-bounds implementation.
    return [max(local_extents[index], world_extents[index]) for index in range(3)]


def projected_object_radius(node: dict[str, Any], direction: list[float]) -> float:
    extents = object_extents(node)
    axes = object_local_axes(node)
    shape = str(node.get("shape") or "").casefold()
    if "sphere" in shape:
        return max(extents)
    if "cylinder" in shape:
        axial_alignment = abs(sum(direction[index] * axes[2][index] for index in range(3)))
        axial_alignment = min(1.0, max(0.0, axial_alignment))
        radial_alignment = math.sqrt(max(0.0, 1.0 - axial_alignment * axial_alignment))
        return extents[2] * axial_alignment + max(extents[0], extents[1]) * radial_alignment
    return sum(
        abs(sum(direction[index] * axes[local_axis][index] for index in range(3))) * extents[local_axis]
        for local_axis in range(3)
    )


def object_local_axes(node: dict[str, Any]) -> list[list[float]]:
    rotation = ((node.get("transform") or {}).get("rotation_deg") or [0.0, 0.0, 0.0])
    pitch, yaw, roll = [math.radians(float(value)) for value in [*rotation, 0.0, 0.0, 0.0][:3]]
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    # UE Rotator semantics: yaw about Z, pitch about Y, roll about X.
    return [
        [cy * cp, sy * cp, -sp],
        [cy * sp * sr - sy * cr, sy * sp * sr + cy * cr, cp * sr],
        [cy * sp * cr + sy * sr, sy * sp * cr - cy * sr, cp * cr],
    ]


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
