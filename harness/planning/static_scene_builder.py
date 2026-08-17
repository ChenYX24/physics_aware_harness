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
    objects = [obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)]
    objects_by_id = {str(obj.get("id") or ""): obj for obj in objects if obj.get("id")}
    nodes = [
        build_object_node(
            obj,
            asset_rows.get(str(obj.get("id"))),
            objects_by_id=objects_by_id,
        )
        for obj in objects
    ]
    containment_relations = declared_containment_relations(objects)
    placement_adjustments = align_v2_explicit_supports(case_spec, nodes)
    expected_physics = case_spec.get("expected_physics") or {}
    collision_edges = normalize_edges(expected_physics.get("collision_graph") or expected_physics.get("contact_order") or [])
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
        allowed_overlap_pairs={
            frozenset((str(relation["object_id"]), str(relation["container_id"])))
            for relation in containment_relations
        },
        include_static_obstacles=True,
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
        "containment_relations": containment_relations,
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


def declared_containment_relations(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relations: list[dict[str, Any]] = []
    object_ids = {str(obj.get("id") or "") for obj in objects if obj.get("id")}
    for obj in objects:
        solver = obj.get("solver") if isinstance(obj.get("solver"), dict) else {}
        initial = solver.get("initial_volume") if isinstance(solver.get("initial_volume"), dict) else {}
        frame = initial.get("frame") if isinstance(initial.get("frame"), dict) else {}
        object_id = str(obj.get("id") or "")
        container_id = str(frame.get("body_id") or "")
        if frame.get("type") != "body_local" or not object_id or container_id not in object_ids:
            continue
        relations.append(
            {
                "object_id": object_id,
                "container_id": container_id,
                "relation": "initially_contained_by",
                "source": "solver.initial_volume.frame",
            }
        )
    return relations


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
    """Snap explicitly supported bodies to resolved support geometry."""
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    support_map = expected.get("support") if isinstance(expected.get("support"), dict) else {}
    if not support_map:
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
    return adjustments


def translate_node_bounds(node: dict[str, Any], delta: list[float]) -> None:
    bounds = node.setdefault("bounds", {})
    if abs(delta[2]) <= 1e-12:
        return
    for key in ("bottom_z", "top_z"):
        if bounds.get(key) is not None:
            bounds[key] = round(float(bounds[key]) + delta[2], 6)


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
    rotation = (support_node.get("transform") or {}).get("rotation_deg") or [0.0, 0.0, 0.0]
    pitch = math.radians(float(rotation[0]))
    if abs(pitch) > 1e-9:
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
    rotation = (support_node.get("transform") or {}).get("rotation_deg") or [0.0, 0.0, 0.0]
    pitch = math.radians(float(rotation[0]))
    if abs(pitch) <= 1e-9:
        return None
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
    allowed_overlap_pairs: set[frozenset[str]] | None = None,
    include_static_obstacles: bool = False,
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    support_map = support_map if isinstance(support_map, dict) else {}
    allowed_overlap_pairs = allowed_overlap_pairs or set()
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
            if frozenset((left_id, right_id)) in allowed_overlap_pairs:
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
