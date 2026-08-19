from __future__ import annotations

import math
from typing import Any

from harness.core.scene_layout import (
    SCENE_LAYOUT_SCHEMA_VERSION,
    allows_above_support,
    build_object_node,
    is_support_role,
    object_position,
    rotate_local_vector_ue,
    round_vec,
)
from harness.runtime.camera_planner import camera_plan_from_case_spec, camera_plan_to_dict


SUPPORT_CONTACT_TOLERANCE_M = 0.002


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
    constraint_collision_disabled_pairs = {
        frozenset((str(item.get("body_a") or ""), str(item.get("body_b") or "")))
        for item in case_spec.get("constraints") or []
        if isinstance(item, dict) and item.get("collision_enabled") is False
    }
    overlap_diagnostics: list[dict[str, Any]] = []
    overlap_pairs = find_overlap_pairs(
        nodes,
        collision_disabled_pairs=constraint_collision_disabled_pairs,
        diagnostics=overlap_diagnostics,
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
        "placement_adjustments": [],
        "overlap_pairs": overlap_pairs,
        "overlap_diagnostics": overlap_diagnostics,
        "constraint_collision_disabled_pairs": [
            sorted(pair) for pair in sorted(constraint_collision_disabled_pairs, key=lambda pair: sorted(pair))
        ],
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
    relations: list[dict[str, Any]] = []
    for node in nodes:
        if (
            not node.get("physics_critical")
            or not is_dynamic_body(node)
            or not has_declared_support(node, expected)
        ):
            continue
        support_id = support_id_for_node(node, expected)
        support_node = by_id.get(support_id) if support_id else None
        relations.append(
            support_relation(
                node,
                support_node,
                require_contact=True,
            )
        )
    return relations


def support_id_for_node(node: dict[str, Any], expected: dict[str, Any]) -> str | None:
    object_id = str(node.get("object_id"))
    support = expected.get("support")
    if isinstance(support, dict):
        value = support.get(object_id) or support.get("default")
        if value:
            return str(value)
    if isinstance(support, str):
        return support
    return None


def has_declared_support(node: dict[str, Any], expected: dict[str, Any]) -> bool:
    object_id = str(node.get("object_id"))
    declaration = expected.get("support")
    if isinstance(declaration, str):
        return bool(declaration)
    if isinstance(declaration, dict) and (object_id in declaration or "default" in declaration):
        return True
    return False


def support_relation(
    node: dict[str, Any],
    support_node: dict[str, Any] | None,
    *,
    require_contact: bool = False,
) -> dict[str, Any]:
    if support_node is None:
        if allows_free_initial_motion(node) and not require_contact:
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
    normal = object_local_axes(support_node)[2]
    gap = support_surface_gap(node, support_node, normal)
    if any(margin < -SUPPORT_CONTACT_TOLERANCE_M for margin in footprint_margins):
        status = "outside_support_footprint"
    elif gap < -SUPPORT_CONTACT_TOLERANCE_M:
        status = "penetrating_support"
    elif abs(gap) <= SUPPORT_CONTACT_TOLERANCE_M:
        status = "contact_at_rest"
    elif allows_free_initial_motion(node) and not require_contact:
        status = "above_support"
    else:
        status = "unsupported_gap"
    return {
        "object_id": node["object_id"],
        "support_id": support_node["object_id"],
        "status": status,
        "vertical_gap_m": gap,
        "signed_surface_gap_m": gap,
        "horizontal_margin_m": footprint_margins,
        "support_normal_world": round_vec(normal),
        "suggested_translation_m": round_vec(
            [0.0 if abs(gap * component) < 1e-12 else -gap * component for component in normal]
        ),
        "tolerance_m": SUPPORT_CONTACT_TOLERANCE_M,
    }


def support_footprint_margins(node: dict[str, Any], support_node: dict[str, Any]) -> list[float]:
    node_position = collision_center(node)
    support_position = collision_center(support_node)
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
    return (object_local_axes(support_node)[:2], support_extents[:2])


def is_dynamic_body(node: dict[str, Any]) -> bool:
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


def support_surface_gap(
    node: dict[str, Any],
    support_node: dict[str, Any],
    normal: list[float] | None = None,
) -> float:
    normal = normal or object_local_axes(support_node)[2]
    node_position = collision_center(node)
    support_position = collision_center(support_node)
    center_delta = [node_position[axis] - support_position[axis] for axis in range(3)]
    support_half_thickness = object_extents(support_node)[2]
    subject_radius = projected_object_radius(node, normal)
    return round(sum(center_delta[axis] * normal[axis] for axis in range(3)) - support_half_thickness - subject_radius, 6)


def find_overlap_pairs(
    nodes: list[dict[str, Any]],
    *,
    collision_disabled_pairs: set[frozenset[str]] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    disabled_pairs = collision_disabled_pairs or set()
    collidable = [
        node
        for node in nodes
        if collision_enabled(node)
    ]
    for index, left in enumerate(collidable):
        for right in collidable[index + 1 :]:
            left_id = str(left.get("object_id") or "")
            right_id = str(right.get("object_id") or "")
            if frozenset((left_id, right_id)) in disabled_pairs:
                continue
            left_pos = collision_center(left)
            right_pos = collision_center(right)
            distance = math.dist(left_pos, right_pos)
            detail = collision_pair_penetration(left, right, tolerance_m=0.002)
            overlaps = bool(detail["overlaps"])
            overlap_test = str(detail["overlap_test"])
            if diagnostics is not None:
                diagnostics.append(
                    {
                        "object_ids": [left_id, right_id],
                        **detail,
                    }
                )
            if overlaps:
                pairs.append(
                    {
                        "object_ids": [left["object_id"], right["object_id"]],
                        "distance_m": round(distance, 6),
                        "world_collision_centers_m": [round_vec(left_pos), round_vec(right_pos)],
                        "overlap_test": overlap_test,
                        **detail,
                    }
                )
    return pairs


def collision_enabled(node: dict[str, Any]) -> bool:
    physics = node.get("physics") if isinstance(node.get("physics"), dict) else {}
    return bool(
        node.get("physics_critical")
        and physics.get("state_kind") != "particle"
        and physics.get("collision_required") is not False
        and isinstance(physics.get("collision_geometry"), dict)
    )


def collision_center(node: dict[str, Any]) -> list[float]:
    geometry = (node.get("physics") or {}).get("collision_geometry") or {}
    center = geometry.get("world_center_m")
    if isinstance(center, list) and len(center) >= 3:
        return [float(value) for value in center[:3]]
    position = object_position(node)
    offset = (node.get("bounds") or {}).get("local_center_offset_m") or [0.0, 0.0, 0.0]
    padded_offset = [float(value) for value in [*offset, 0.0, 0.0, 0.0][:3]]
    axes = object_local_axes(node)
    return [
        position[component] + sum(padded_offset[axis] * axes[axis][component] for axis in range(3))
        for component in range(3)
    ]


def collision_shape(node: dict[str, Any]) -> str:
    geometry = (node.get("physics") or {}).get("collision_geometry") or {}
    physics = node.get("physics") or {}
    return str(geometry.get("shape") or physics.get("collider") or node.get("shape") or "box").casefold()


def collision_pair_penetration(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    tolerance_m: float,
) -> dict[str, Any]:
    left_shape = collision_shape(left)
    right_shape = collision_shape(right)
    if left_shape == right_shape == "sphere":
        return sphere_sphere_penetration(left, right, tolerance_m=tolerance_m)
    if {left_shape, right_shape} == {"sphere", "box"}:
        return sphere_box_penetration(left, right, tolerance_m=tolerance_m)
    if {left_shape, right_shape} == {"sphere", "cylinder"}:
        return sphere_cylinder_penetration(left, right, tolerance_m=tolerance_m)
    return convex_axis_penetration(left, right, tolerance_m=tolerance_m)


def sphere_sphere_penetration(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    tolerance_m: float,
) -> dict[str, Any]:
    delta = vector_subtract(collision_center(right), collision_center(left))
    distance = vector_length(delta)
    signed_margin = distance - object_extents(left)[0] - object_extents(right)[0]
    axis = normalize_vector(delta) if distance > 1e-12 else [1.0, 0.0, 0.0]
    return penetration_detail(
        signed_margin,
        axis,
        tolerance_m=tolerance_m,
        overlap_test="sphere_center_distance",
        decisive_axis="center_line",
        tested_axis_count=1,
    )


def sphere_box_penetration(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    tolerance_m: float,
) -> dict[str, Any]:
    sphere = left if collision_shape(left) == "sphere" else right
    box = right if sphere is left else left
    sphere_center = collision_center(sphere)
    box_center = collision_center(box)
    box_axes = object_local_axes(box)
    box_extents = object_extents(box)
    relative = vector_subtract(sphere_center, box_center)
    local = [dot_product(relative, axis) for axis in box_axes]
    closest_local = [max(-box_extents[index], min(box_extents[index], local[index])) for index in range(3)]
    closest_world = [
        box_center[component]
        + sum(closest_local[axis] * box_axes[axis][component] for axis in range(3))
        for component in range(3)
    ]
    separation = vector_subtract(closest_world, sphere_center)
    separation_length = vector_length(separation)
    radius = object_extents(sphere)[0]
    if separation_length > 1e-12:
        signed_margin = separation_length - radius
        axis = normalize_vector(separation)
        decisive_axis = "sphere_to_closest_box_point"
    else:
        clearances = [box_extents[index] - abs(local[index]) for index in range(3)]
        nearest_axis = min(range(3), key=lambda index: clearances[index])
        sign = 1.0 if local[nearest_axis] >= 0.0 else -1.0
        axis = [sign * value for value in box_axes[nearest_axis]]
        signed_margin = -(radius + clearances[nearest_axis])
        decisive_axis = f"box_inner_face_{nearest_axis}"
    if sphere is right:
        axis = [-value for value in axis]
    return penetration_detail(
        signed_margin,
        axis,
        tolerance_m=tolerance_m,
        overlap_test="sphere_oriented_box_closest_point",
        decisive_axis=decisive_axis,
        tested_axis_count=1,
    )


def sphere_cylinder_penetration(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    tolerance_m: float,
) -> dict[str, Any]:
    sphere = left if collision_shape(left) == "sphere" else right
    cylinder = right if sphere is left else left
    sphere_center = collision_center(sphere)
    cylinder_center = collision_center(cylinder)
    cylinder_axis = object_local_axes(cylinder)[2]
    relative = vector_subtract(sphere_center, cylinder_center)
    axial = dot_product(relative, cylinder_axis)
    radial = vector_subtract(relative, [axial * value for value in cylinder_axis])
    radial_length = vector_length(radial)
    radius, half_height = object_extents(cylinder)[0], object_extents(cylinder)[2]
    clamped_axial = max(-half_height, min(half_height, axial))
    clamped_radial = (
        [value * min(1.0, radius / radial_length) for value in radial]
        if radial_length > 1e-12
        else [0.0, 0.0, 0.0]
    )
    closest = [
        cylinder_center[index] + clamped_axial * cylinder_axis[index] + clamped_radial[index]
        for index in range(3)
    ]
    separation = vector_subtract(closest, sphere_center)
    separation_length = vector_length(separation)
    sphere_radius = object_extents(sphere)[0]
    if separation_length > 1e-12:
        signed_margin = separation_length - sphere_radius
        axis = normalize_vector(separation)
        decisive_axis = "sphere_to_closest_cylinder_point"
    else:
        radial_clearance = radius - radial_length
        cap_clearance = half_height - abs(axial)
        if radial_clearance <= cap_clearance:
            axis = normalize_vector(radial) if radial_length > 1e-12 else object_local_axes(cylinder)[0]
            signed_margin = -(sphere_radius + radial_clearance)
            decisive_axis = "cylinder_inner_side"
        else:
            sign = 1.0 if axial >= 0.0 else -1.0
            axis = [sign * value for value in cylinder_axis]
            signed_margin = -(sphere_radius + cap_clearance)
            decisive_axis = "cylinder_inner_cap"
    if sphere is right:
        axis = [-value for value in axis]
    return penetration_detail(
        signed_margin,
        axis,
        tolerance_m=tolerance_m,
        overlap_test="sphere_oriented_cylinder_closest_point",
        decisive_axis=decisive_axis,
        tested_axis_count=1,
    )


def convex_axis_penetration(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    tolerance_m: float,
) -> dict[str, Any]:
    center_delta = vector_subtract(collision_center(right), collision_center(left))
    candidates: list[tuple[str, list[float]]] = []
    left_shape = collision_shape(left)
    right_shape = collision_shape(right)
    left_axes = object_local_axes(left)
    right_axes = object_local_axes(right)
    if left_shape == "box":
        candidates.extend((f"left_axis_{index}", axis) for index, axis in enumerate(left_axes))
    if right_shape == "box":
        candidates.extend((f"right_axis_{index}", axis) for index, axis in enumerate(right_axes))
    if left_shape == "cylinder":
        candidates.append(("left_cylinder_axis", left_axes[2]))
    if right_shape == "cylinder":
        candidates.append(("right_cylinder_axis", right_axes[2]))
    for left_index, left_axis in enumerate(left_axes if left_shape in {"box", "cylinder"} else []):
        for right_index, right_axis in enumerate(right_axes if right_shape in {"box", "cylinder"} else []):
            candidates.append((f"cross_{left_index}_{right_index}", cross_product(left_axis, right_axis)))
    if left_shape == "sphere" or right_shape == "sphere":
        candidates.append(("center_line", center_delta))
    # Cylinder side normals are continuous. Directions from its axis to the
    # other primitive's oriented vertices cover the finite box/cylinder
    # feature pairs without replacing the declared cylinder by a box.
    if left_shape == "cylinder":
        candidates.extend(
            cylinder_radial_candidates(left, right, label="left")
        )
    if right_shape == "cylinder":
        candidates.extend(
            cylinder_radial_candidates(right, left, label="right")
        )
    largest_gap = -math.inf
    decisive_axis = ""
    decisive_vector = [1.0, 0.0, 0.0]
    tested_axes = 0
    for label, raw_axis in candidates:
        axis = normalize_vector(raw_axis)
        if vector_length(axis) <= 1e-12:
            continue
        center_distance = abs(dot_product(center_delta, axis))
        gap = center_distance - shape_projection_radius(left, axis) - shape_projection_radius(right, axis)
        tested_axes += 1
        if gap > largest_gap:
            largest_gap = gap
            decisive_axis = label
            decisive_vector = axis if dot_product(center_delta, axis) >= 0.0 else [-value for value in axis]
    if not math.isfinite(largest_gap):
        largest_gap = math.inf
    return penetration_detail(
        largest_gap,
        decisive_vector,
        tolerance_m=tolerance_m,
        overlap_test=(
            "oriented_box_sat"
            if left_shape == right_shape == "box"
            else "declared_convex_shape_sat"
        ),
        decisive_axis=decisive_axis,
        tested_axis_count=tested_axes,
    )


def cylinder_radial_candidates(
    cylinder: dict[str, Any],
    other: dict[str, Any],
    *,
    label: str,
) -> list[tuple[str, list[float]]]:
    cylinder_center = collision_center(cylinder)
    cylinder_axis = object_local_axes(cylinder)[2]
    points = oriented_vertices(other) if collision_shape(other) == "box" else [collision_center(other)]
    candidates: list[tuple[str, list[float]]] = []
    for index, point in enumerate(points):
        delta = vector_subtract(point, cylinder_center)
        radial = vector_subtract(delta, [dot_product(delta, cylinder_axis) * value for value in cylinder_axis])
        candidates.append((f"{label}_radial_{index}", radial))
    return candidates


def oriented_vertices(node: dict[str, Any]) -> list[list[float]]:
    center = collision_center(node)
    axes = object_local_axes(node)
    extents = object_extents(node)
    return [
        [
            center[component]
            + sum(signs[axis] * extents[axis] * axes[axis][component] for axis in range(3))
            for component in range(3)
        ]
        for signs in (
            (-1.0, -1.0, -1.0), (-1.0, -1.0, 1.0), (-1.0, 1.0, -1.0), (-1.0, 1.0, 1.0),
            (1.0, -1.0, -1.0), (1.0, -1.0, 1.0), (1.0, 1.0, -1.0), (1.0, 1.0, 1.0),
        )
    ]


def shape_projection_radius(node: dict[str, Any], axis: list[float]) -> float:
    extents = object_extents(node)
    shape = collision_shape(node)
    if shape == "sphere":
        return extents[0]
    axes = object_local_axes(node)
    if shape == "cylinder":
        axial = min(1.0, abs(dot_product(axis, axes[2])))
        return extents[2] * axial + extents[0] * math.sqrt(max(0.0, 1.0 - axial * axial))
    return sum(extents[index] * abs(dot_product(axis, axes[index])) for index in range(3))


def penetration_detail(
    signed_margin: float,
    axis: list[float],
    *,
    tolerance_m: float,
    overlap_test: str,
    decisive_axis: str,
    tested_axis_count: int,
) -> dict[str, Any]:
    overlaps = signed_margin < -tolerance_m
    return {
        "overlaps": overlaps,
        "signed_margin_m": round(signed_margin, 6),
        "penetration_depth_m": round(max(0.0, -signed_margin), 6),
        "minimum_translation_axis": round_vec(axis),
        "tolerance_m": tolerance_m,
        "decisive_axis": decisive_axis,
        "tested_axis_count": tested_axis_count,
        "overlap_test": overlap_test,
    }


def dot_product(left: list[float], right: list[float]) -> float:
    return sum(left[index] * right[index] for index in range(3))


def cross_product(left: list[float], right: list[float]) -> list[float]:
    return [
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    ]


def vector_subtract(left: list[float], right: list[float]) -> list[float]:
    return [left[index] - right[index] for index in range(3)]


def vector_length(value: list[float]) -> float:
    return math.sqrt(dot_product(value, value))


def normalize_vector(value: list[float]) -> list[float]:
    length = vector_length(value)
    return [component / length for component in value] if length > 1e-12 else [0.0, 0.0, 0.0]


def projected_object_radius(node: dict[str, Any], direction: list[float]) -> float:
    extents = object_extents(node)
    axes = object_local_axes(node)
    shape = collision_shape(node)
    if shape == "sphere":
        return max(extents)
    if shape == "cylinder":
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
    return [
        rotate_local_vector_ue(axis, rotation)
        for axis in ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0])
    ]


def object_extents(node: dict[str, Any]) -> list[float]:
    geometry = (node.get("physics") or {}).get("collision_geometry") or {}
    size_m = geometry.get("size_m")
    if isinstance(size_m, list) and len(size_m) >= 3:
        return [float(size_m[0]) / 2.0, float(size_m[1]) / 2.0, float(size_m[2]) / 2.0]
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
