from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.artifact_schema import write_json
from harness.runtime.deformable_surface_adapter import (
    collider_position_m,
    collider_world_from_local_columns,
    deformable_analytic_colliders,
    load_deformable_case_data,
    object_material_number,
    object_position_m,
    prepare_ue_deformable_replay,
    support_height_m,
    wind_field_parameters,
)
from harness.verification.deformable_mesh_cache_verifier import (
    verify_deformable_mesh_cache_file,
    verify_deformable_solid_impact_file,
)


def simulate(case_data: dict[str, Any]) -> dict[str, Any]:
    import taichi as ti

    cloth = object_with_role(case_data, "deformable_cloth")
    colliders = deformable_analytic_colliders(case_data)
    floor = optional_object_with_role(case_data, "support_surface")
    wind = optional_object_with_role(case_data, "wind_field")
    wind_parameters = wind_field_parameters(wind, str(cloth.get("id") or ""))
    options = case_data.get("backend_options") or {}
    cloth_solver = cloth.get("solver") or {}
    runtime_contract = cloth_solver.get("type") == "mass_spring_cloth"
    material = cloth.get("material") or {}

    n = int(cloth_solver.get("grid_resolution") if runtime_contract else cloth.get("grid_resolution") or 25)
    if n < 4:
        raise ValueError("cloth grid_resolution must be at least 4")
    orientation = str(
        cloth_solver.get("initial_orientation")
        if runtime_contract
        else cloth.get("initial_orientation") or "horizontal_xy"
    )
    raw_size = [float(value) for value in cloth.get("size_m") or [1.5, 1.5]]
    if runtime_contract and len(raw_size) == 3:
        size = raw_size[:2] if orientation == "horizontal_xy" else [raw_size[0], raw_size[2]]
    else:
        size = raw_size
    if len(size) != 2 or min(size) <= 0.0:
        raise ValueError("cloth size_m must contain two positive values")
    center = np.asarray(
        object_position_m(cloth, [0.0, 0.0, 1.35], legacy_field="initial_center_m"),
        dtype=np.float32,
    )
    pinned_boundaries = cloth_solver.get("pinned_boundaries") if runtime_contract else None
    if pinned_boundaries is None:
        legacy_boundary = str(cloth_solver.get("pinned_boundary") if runtime_contract else cloth.get("pinned_boundary") or "none")
        pinned_boundaries = [] if legacy_boundary == "none" else [legacy_boundary]
    pinned_boundaries = [str(value) for value in pinned_boundaries]
    width_scale_by_v = [float(value) for value in cloth_solver.get("width_scale_by_v") or [1.0, 1.0]]
    u_cuts = [dict(value) for value in cloth_solver.get("u_cuts") or []]
    floor_z = support_height_m(floor or {})
    total_mass = float(cloth.get("mass_kg") if runtime_contract else cloth.get("total_mass_kg") or 0.45)
    point_mass = total_mass / (n * n)
    timebase = case_data.get("timebase") or {}
    scene = case_data.get("scene") or {}
    fps = int(options.get("fps") or timebase.get("render_fps") or timebase.get("observation_fps") or 24)
    duration_s = float(options.get("duration_s") or scene.get("duration_s") or 2.5)
    substeps_per_frame = int(
        cloth_solver.get("substeps_per_frame") if runtime_contract else options.get("substeps_per_frame") or 80
    )
    if fps <= 0 or duration_s <= 0.0 or substeps_per_frame <= 0:
        raise ValueError("fps, duration_s, and substeps_per_frame must be positive")
    dt = 1.0 / (fps * substeps_per_frame)
    collision_thickness = float(
        cloth_solver.get("collision_thickness_m")
        if runtime_contract
        else options.get("constraint_collision_thickness_m") or 0.012
    )
    structural_stiffness = float(material.get("structural_stiffness_n_m", 35.0))
    shear_stiffness = float(material.get("shear_stiffness_n_m", 22.0))
    bending_stiffness = float(material.get("bending_stiffness_n_m", 4.0))
    spring_damping = float(material.get("spring_damping_kg_s", 0.003))
    air_damping = float(material.get("air_damping_1_s", 0.8))
    floor_friction = object_material_number(floor, "dynamic_friction", legacy_field="friction")
    floor_restitution = object_material_number(floor, "restitution")
    wind_velocity = np.asarray((wind_parameters or {}).get("velocity_m_s") or [0.0, 0.0, 0.0], dtype=np.float32)
    air_density = float((wind_parameters or {}).get("air_density_kg_m3") or 1.225)
    drag_coefficient = float((wind_parameters or {}).get("drag_coefficient") or 1.0)
    gust_amplitude = float((wind_parameters or {}).get("gust_amplitude") or 0.0)
    gust_frequency = float((wind_parameters or {}).get("gust_frequency_hz") or 0.0)

    ti.init(
        arch=ti.cpu,
        default_fp=ti.f32,
        random_seed=int(options.get("seed") or case_data.get("seed") or timebase.get("deterministic_seed") or 0),
        offline_cache=False,
    )
    vertex_count = n * n
    positions = ti.Vector.field(3, dtype=ti.f32, shape=vertex_count)
    velocities = ti.Vector.field(3, dtype=ti.f32, shape=vertex_count)
    forces = ti.Vector.field(3, dtype=ti.f32, shape=vertex_count)
    pinned = ti.field(dtype=ti.i32, shape=vertex_count)
    simulation_time = ti.field(dtype=ti.f32, shape=())

    initial = grid_positions(
        n,
        size,
        center,
        orientation=orientation,
        width_scale_by_v=width_scale_by_v,
        u_cuts=u_cuts,
    )
    pinned_mask = grid_pinned_mask(n, pinned_boundaries)
    faces_np = grid_faces(n, u_cuts=u_cuts)
    faces_ti = ti.Vector.field(3, dtype=ti.i32, shape=len(faces_np))
    positions.from_numpy(initial)
    velocities.fill(0.0)
    pinned.from_numpy(pinned_mask)
    faces_ti.from_numpy(faces_np)

    drag = math.exp(-air_damping * dt)
    gravity = ti.Vector([0.0, 0.0, -9.81])
    wind_velocity_ti = ti.Vector([float(value) for value in wind_velocity])
    has_floor = floor is not None
    has_wind = wind is not None and float(np.linalg.norm(wind_velocity)) > 0.0
    spring_neighbors = (
        (-1, 0, structural_stiffness), (1, 0, structural_stiffness),
        (0, -1, structural_stiffness), (0, 1, structural_stiffness),
        (-1, -1, shear_stiffness), (-1, 1, shear_stiffness),
        (1, -1, shear_stiffness), (1, 1, shear_stiffness),
        (-2, 0, bending_stiffness), (2, 0, bending_stiffness),
        (0, -2, bending_stiffness), (0, 2, bending_stiffness),
    )
    spring_rest_values, spring_active_values = grid_spring_data(initial, n, spring_neighbors, u_cuts)
    spring_rest_lengths = ti.field(dtype=ti.f32, shape=(vertex_count, len(spring_neighbors)))
    spring_active = ti.field(dtype=ti.i32, shape=(vertex_count, len(spring_neighbors)))
    spring_rest_lengths.from_numpy(spring_rest_values)
    spring_active.from_numpy(spring_active_values)

    frame_count = max(1, int(round(duration_s * fps)))
    solver_step_count = frame_count * substeps_per_frame
    collider_count = len(colliders)
    collider_storage_count = max(1, collider_count)
    collider_type_values = np.zeros(collider_storage_count, dtype=np.int32)
    collider_half_size_values = np.zeros((collider_storage_count, 3), dtype=np.float32)
    collider_world_from_local_values = np.zeros(
        (solver_step_count + 1, collider_storage_count, 3, 3),
        dtype=np.float32,
    )
    collider_friction_values = np.zeros(collider_storage_count, dtype=np.float32)
    collider_restitution_values = np.zeros(collider_storage_count, dtype=np.float32)
    collider_center_values = np.zeros(
        (solver_step_count + 1, collider_storage_count, 3),
        dtype=np.float32,
    )
    shape_codes = {"sphere": 0, "box": 1, "cylinder": 2}
    for collider_index, collider in enumerate(colliders):
        collider_type_values[collider_index] = shape_codes[collider["shape"]]
        collider_half_size_values[collider_index] = np.asarray(collider["size_m"], dtype=np.float32) / 2.0
        collider_friction_values[collider_index] = float(collider["dynamic_friction"])
        collider_restitution_values[collider_index] = float(collider["restitution"])
        for step_index in range(solver_step_count + 1):
            step_time_s = step_index * dt
            collider_center_values[step_index, collider_index] = collider_position_m(
                collider,
                step_time_s,
            )
            collider_world_from_local_values[step_index, collider_index] = np.asarray(
                collider_world_from_local_columns(collider, step_time_s),
                dtype=np.float32,
            ).T
    collider_types = ti.field(dtype=ti.i32, shape=collider_storage_count)
    collider_half_sizes = ti.Vector.field(3, dtype=ti.f32, shape=collider_storage_count)
    collider_world_from_local = ti.Matrix.field(
        3,
        3,
        dtype=ti.f32,
        shape=(solver_step_count + 1, collider_storage_count),
    )
    collider_frictions = ti.field(dtype=ti.f32, shape=collider_storage_count)
    collider_restitutions = ti.field(dtype=ti.f32, shape=collider_storage_count)
    current_solver_step = ti.field(dtype=ti.i32, shape=())
    surface_position_corrections = ti.Vector.field(3, dtype=ti.f32, shape=vertex_count)
    surface_velocity_corrections = ti.Vector.field(3, dtype=ti.f32, shape=vertex_count)
    surface_correction_weights = ti.field(dtype=ti.f32, shape=vertex_count)
    collider_centers = ti.Vector.field(
        3,
        dtype=ti.f32,
        shape=(solver_step_count + 1, collider_storage_count),
    )
    collider_types.from_numpy(collider_type_values)
    collider_half_sizes.from_numpy(collider_half_size_values)
    collider_world_from_local.from_numpy(collider_world_from_local_values)
    collider_frictions.from_numpy(collider_friction_values)
    collider_restitutions.from_numpy(collider_restitution_values)
    collider_centers.from_numpy(collider_center_values)

    @ti.func
    def project_against_collider(point, point_velocity, collider_index):
        center = collider_centers[current_solver_step[None] + 1, collider_index]
        previous_center = collider_centers[current_solver_step[None], collider_index]
        collider_velocity = (center - previous_center) / dt
        world_from_local = collider_world_from_local[current_solver_step[None] + 1, collider_index]
        local = world_from_local.transpose() @ (point - center)
        half_size = collider_half_sizes[collider_index]
        projected = local
        normal_local = ti.Vector([0.0, 0.0, 0.0])
        colliding = 0
        shape_code = collider_types[collider_index]

        if shape_code == 0:
            radius = half_size.x + collision_thickness
            distance = local.norm()
            if distance < radius:
                normal_local = local / ti.max(distance, 1e-7)
                if distance <= 1e-7:
                    normal_local = ti.Vector([1.0, 0.0, 0.0])
                projected = radius * normal_local
                colliding = 1
        elif shape_code == 1:
            expanded = half_size + ti.Vector([collision_thickness] * 3)
            penetration = expanded - ti.abs(local)
            if penetration.x > 0.0 and penetration.y > 0.0 and penetration.z > 0.0:
                axis = 0
                if penetration.y < penetration.x:
                    axis = 1
                if penetration.z < penetration[axis]:
                    axis = 2
                sign = 1.0 if local[axis] >= 0.0 else -1.0
                normal_local[axis] = sign
                projected[axis] = sign * expanded[axis]
                colliding = 1
        else:
            radius = half_size.x + collision_thickness
            half_height = half_size.z + collision_thickness
            radial_distance = ti.sqrt(local.x * local.x + local.y * local.y)
            if radial_distance < radius and ti.abs(local.z) < half_height:
                side_penetration = radius - radial_distance
                cap_penetration = half_height - ti.abs(local.z)
                if side_penetration < cap_penetration:
                    radial_normal = ti.Vector([1.0, 0.0])
                    if radial_distance > 1e-7:
                        radial_normal = ti.Vector([local.x, local.y]) / radial_distance
                    normal_local = ti.Vector([radial_normal.x, radial_normal.y, 0.0])
                    projected.x = radius * radial_normal.x
                    projected.y = radius * radial_normal.y
                else:
                    sign = 1.0 if local.z >= 0.0 else -1.0
                    normal_local = ti.Vector([0.0, 0.0, sign])
                    projected.z = sign * half_height
                colliding = 1

        projected_world = point
        corrected_velocity = point_velocity
        if colliding != 0:
            normal = world_from_local @ normal_local
            projected_world = center + world_from_local @ projected
            relative_velocity = point_velocity - collider_velocity
            normal_speed = relative_velocity.dot(normal)
            if normal_speed < 0.0:
                relative_velocity -= (
                    1.0 + collider_restitutions[collider_index]
                ) * normal_speed * normal
            tangent = relative_velocity - relative_velocity.dot(normal) * normal
            relative_velocity -= ti.min(1.0, collider_frictions[collider_index]) * tangent
            corrected_velocity = relative_velocity + collider_velocity
        return projected_world, corrected_velocity, colliding

    @ti.kernel
    def substep():
        for vertex in positions:
            forces[vertex] = gravity * point_mass
        for vertex in positions:
            row = vertex // n
            column = vertex - row * n
            force = forces[vertex]
            for neighbor_index in ti.static(range(len(spring_neighbors))):
                dx, dy, stiffness = ti.static(spring_neighbors[neighbor_index])
                neighbour_row = row + dx
                neighbour_column = column + dy
                if 0 <= neighbour_row < n and 0 <= neighbour_column < n and spring_active[vertex, neighbor_index] != 0:
                    neighbour = neighbour_row * n + neighbour_column
                    delta = positions[neighbour] - positions[vertex]
                    distance = delta.norm()
                    if distance > 1e-7:
                        direction = delta / distance
                        relative_speed = (velocities[neighbour] - velocities[vertex]).dot(direction)
                        rest_length = spring_rest_lengths[vertex, neighbor_index]
                        force += (stiffness * (distance - rest_length) + spring_damping * relative_speed) * direction
            forces[vertex] = force
        if ti.static(has_wind):
            gust = 1.0 + gust_amplitude * ti.sin(2.0 * math.pi * gust_frequency * simulation_time[None])
            for triangle in faces_ti:
                indices = faces_ti[triangle]
                edge_a = positions[indices[1]] - positions[indices[0]]
                edge_b = positions[indices[2]] - positions[indices[0]]
                area_vector = edge_a.cross(edge_b)
                twice_area = area_vector.norm()
                if twice_area > 1e-8:
                    normal = area_vector / twice_area
                    relative_wind = gust * wind_velocity_ti - (
                        velocities[indices[0]] + velocities[indices[1]] + velocities[indices[2]]
                    ) / 3.0
                    normal_speed = relative_wind.dot(normal)
                    pressure = 0.5 * air_density * drag_coefficient * normal_speed * ti.abs(normal_speed)
                    aerodynamic_force = pressure * 0.5 * twice_area * normal / 3.0
                    for corner in ti.static(range(3)):
                        for component in ti.static(range(3)):
                            ti.atomic_add(forces[indices[corner]][component], aerodynamic_force[component])
        for vertex in positions:
            if pinned[vertex] != 0:
                velocities[vertex] = ti.Vector([0.0, 0.0, 0.0])
                continue
            velocities[vertex] += dt * forces[vertex] / point_mass
            velocities[vertex] *= drag
            positions[vertex] += dt * velocities[vertex]

            for collider_index in ti.static(range(collider_count)):
                projected, corrected_velocity, colliding = project_against_collider(
                    positions[vertex], velocities[vertex], collider_index
                )
                if colliding != 0:
                    positions[vertex] = projected
                    velocities[vertex] = corrected_velocity

            if ti.static(has_floor):
                floor_height = floor_z + collision_thickness
                if positions[vertex].z < floor_height:
                    positions[vertex].z = floor_height
                    if velocities[vertex].z < 0.0:
                        velocities[vertex].z *= -floor_restitution
                    velocities[vertex].x *= 1.0 - ti.min(1.0, floor_friction)
                    velocities[vertex].y *= 1.0 - ti.min(1.0, floor_friction)

    @ti.kernel
    def resolve_surface_contacts():
        for vertex in positions:
            surface_position_corrections[vertex] = ti.Vector([0.0, 0.0, 0.0])
            surface_velocity_corrections[vertex] = ti.Vector([0.0, 0.0, 0.0])
            surface_correction_weights[vertex] = 0.0
        for triangle in faces_ti:
            indices = faces_ti[triangle]
            for collider_index in ti.static(range(collider_count)):
                for sample_index in ti.static(range(4)):
                    weights = ti.Vector([1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0])
                    if sample_index == 0:
                        weights = ti.Vector([0.5, 0.5, 0.0])
                    elif sample_index == 1:
                        weights = ti.Vector([0.0, 0.5, 0.5])
                    elif sample_index == 2:
                        weights = ti.Vector([0.5, 0.0, 0.5])
                    sample_position = ti.Vector([0.0, 0.0, 0.0])
                    sample_velocity = ti.Vector([0.0, 0.0, 0.0])
                    for corner in ti.static(range(3)):
                        sample_position += weights[corner] * positions[indices[corner]]
                        sample_velocity += weights[corner] * velocities[indices[corner]]
                    projected, corrected_velocity, colliding = project_against_collider(
                        sample_position, sample_velocity, collider_index
                    )
                    if colliding != 0:
                        position_delta = projected - sample_position
                        velocity_delta = corrected_velocity - sample_velocity
                        for corner in ti.static(range(3)):
                            vertex = indices[corner]
                            weight = weights[corner]
                            if weight > 0.0 and pinned[vertex] == 0:
                                for component in ti.static(range(3)):
                                    ti.atomic_add(
                                        surface_position_corrections[vertex][component],
                                        weight * position_delta[component],
                                    )
                                    ti.atomic_add(
                                        surface_velocity_corrections[vertex][component],
                                        weight * velocity_delta[component],
                                    )
                                ti.atomic_add(surface_correction_weights[vertex], weight)
        for vertex in positions:
            weight = surface_correction_weights[vertex]
            if pinned[vertex] == 0 and weight > 0.0:
                positions[vertex] += surface_position_corrections[vertex] / weight
                velocities[vertex] += surface_velocity_corrections[vertex] / weight

    position_frames = np.empty((frame_count + 1, vertex_count, 3), dtype=np.float32)
    velocity_frames = np.empty_like(position_frames)
    for frame in range(frame_count + 1):
        position_frames[frame] = positions.to_numpy()
        velocity_frames[frame] = velocities.to_numpy()
        if frame < frame_count:
            for substep_index in range(substeps_per_frame):
                simulation_time[None] = (frame + substep_index / substeps_per_frame) / fps
                solver_step_index = frame * substeps_per_frame + substep_index
                current_solver_step[None] = solver_step_index
                substep()
                if collider_count:
                    resolve_surface_contacts()

    return {
        "positions_m": position_frames,
        "velocities_m_s": velocity_frames,
        "faces": faces_np,
        "structural_edges": grid_structural_edges(n, u_cuts=u_cuts),
        "pinned_indices": np.flatnonzero(pinned_mask).astype(np.int32),
        "times_s": np.arange(frame_count + 1, dtype=np.float64) / fps,
        "parameters": {
            "backend": "taichi_mass_spring_cpu",
            "taichi_version": str(ti.__version__),
            "grid_resolution": n,
            "vertex_count": vertex_count,
            "fps": fps,
            "duration_s": duration_s,
            "substeps_per_frame": substeps_per_frame,
            "solver_dt_s": dt,
            "point_mass_kg": point_mass,
            "collision_thickness_m": collision_thickness,
            "surface_contact_samples": ["triangle_edge_midpoints", "triangle_centroid"],
            "initial_orientation": orientation,
            "pinned_boundaries": pinned_boundaries,
            "width_scale_by_v": width_scale_by_v,
            "u_cuts": u_cuts,
            "analytic_colliders": colliders,
            "floor_z_m": floor_z if floor is not None else None,
            "wind_velocity_m_s": wind_velocity.tolist() if wind_parameters is not None else None,
            "air_density_kg_m3": air_density if wind_parameters is not None else None,
            "drag_coefficient": drag_coefficient if wind_parameters is not None else None,
            "gust_amplitude": gust_amplitude if wind_parameters is not None else None,
            "gust_frequency_hz": gust_frequency if wind_parameters is not None else None,
        },
    }


def write_run(output_dir: Path, case_data: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "case_spec.json", case_data)
    cache_path = output_dir / "deformable_cache.npz"
    np.savez_compressed(
        cache_path,
        positions_m=result["positions_m"],
        velocities_m_s=result["velocities_m_s"],
        faces=result["faces"],
        structural_edges=result["structural_edges"],
        **({"tetrahedra": result["tetrahedra"]} if "tetrahedra" in result else {}),
        pinned_indices=result["pinned_indices"],
        times_s=result["times_s"],
    )
    surface_dir = output_dir / "surface"
    surface_dir.mkdir(parents=True, exist_ok=True)
    frames: list[dict[str, Any]] = []
    for frame, positions in enumerate(result["positions_m"]):
        path = surface_dir / f"frame_{frame:04d}.obj"
        write_obj(path, positions, result["faces"])
        frames.append({
            "frame": frame,
            "time_s": round(float(result["times_s"][frame]), 8),
            "surface": path.relative_to(output_dir).as_posix(),
            "vertex_count": int(len(positions)),
            "triangle_count": int(len(result["faces"])),
            "bounds_m": {
                "min": [round(float(value), 8) for value in positions.min(axis=0)],
                "max": [round(float(value), 8) for value in positions.max(axis=0)],
            },
            "sha256": file_sha256(path),
        })
    parameters = result["parameters"]
    manifest = {
        "schema_version": "harness_deformable_mesh_cache_v1",
        "case_id": case_data["case_id"],
        "capability_id": case_data["capability_id"],
        "backend": parameters["backend"],
        "canonical_state": "deformable_cache.npz",
        "canonical_state_sha256": file_sha256(cache_path),
        "coordinate_system": "right_handed_z_up_metres",
        "topology": {
            "fixed": True,
            "vertex_count": parameters["vertex_count"],
            "triangle_count": int(len(result["faces"])),
            "structural_edge_count": int(len(result["structural_edges"])),
            "tetrahedron_count": int(len(result.get("tetrahedra", []))),
        },
        "constraints": {
            "pinned_boundaries": parameters["pinned_boundaries"],
            "pinned_vertex_indices": result["pinned_indices"].tolist(),
        },
        "timebase": {
            "fps": parameters["fps"],
            "frame_count": int(len(result["positions_m"])),
            "solver_dt_s": parameters["solver_dt_s"],
            "substeps_per_output": parameters["substeps_per_frame"],
            "sampling_phase": "frame 0 is initial state; later frames follow solver-owned substeps",
        },
        "solver": parameters,
        "surface_role": "derived_render_representation",
        "frames": frames,
    }
    write_json(output_dir / "deformable_cache.json", manifest)

    solid = optional_object_with_role(case_data, "deformable_solid")
    colliders = deformable_analytic_colliders(case_data)
    floor = optional_object_with_role(case_data, "support_surface")
    wind = optional_object_with_role(case_data, "wind_field")
    wind_parameters = wind_field_parameters(wind, str((optional_object_with_role(case_data, "deformable_cloth") or {}).get("id") or ""))
    if solid:
        verification = verify_deformable_solid_impact_file(
            cache_path,
            expected=case_data.get("expected_physics") or {},
            floor_z_m=float((floor or {}).get("z_m") or 0.0),
        )
    else:
        verification = verify_deformable_mesh_cache_file(
            cache_path,
            expected=case_data.get("expected_physics") or {},
            colliders=colliders,
            floor_z_m=support_height_m(floor) if floor else None,
            collision_thickness_m=float(parameters["collision_thickness_m"]),
            pinned_indices=result["pinned_indices"].tolist(),
            wind_axis=[float(value) for value in wind_parameters["velocity_m_s"]] if wind_parameters else None,
        )
    write_json(output_dir / "deformable_solver_verifier.json", verification)
    write_json(output_dir / "harness_verifier.json", verification)
    replay_path = None
    if verification["status"] == "pass":
        replay_root = output_dir / "ue_replay"
        prepare_ue_deformable_replay(
            output_dir / "deformable_cache.json",
            replay_root,
            ue_asset_root=f"/Game/HarnessGenerated/Deformable/{file_sha256(cache_path)[:16]}",
        )
        replay_path = "ue_replay/deformable_surface_replay.json"
    report_stem = "genesis_fem" if str(parameters["backend"]).startswith("genesis_fem") else "taichi_cloth"
    write_json(output_dir / f"{report_stem}_report.json", {
        "schema_version": f"harness_{report_stem}_report_v1",
        "status": "completed" if verification["status"] == "pass" else "failed_verification",
        "case_id": case_data["case_id"],
        "backend": parameters["backend"],
        "canonical_state": "deformable_cache.npz",
        "manifest": "deformable_cache.json",
        "verification": "deformable_solver_verifier.json",
        "verification_status": verification["status"],
        "ue_replay": replay_path,
    })
    return verification


def grid_positions(
    n: int,
    size: list[float],
    center: np.ndarray,
    *,
    orientation: str = "horizontal_xy",
    width_scale_by_v: list[float] | None = None,
    u_cuts: list[dict[str, float]] | None = None,
) -> np.ndarray:
    u_values = np.linspace(0.0, 1.0, n, dtype=np.float32)
    v_values = np.linspace(0.0, 1.0, n, dtype=np.float32)
    width_scales = width_scale_by_v or [1.0, 1.0]
    cuts = u_cuts or []
    result = np.empty((n * n, 3), dtype=np.float32)
    for row, u in enumerate(u_values):
        for column, v in enumerate(v_values):
            width_scale = (1.0 - float(v)) * width_scales[0] + float(v) * width_scales[1]
            x = (float(u) - 0.5) * size[0] * width_scale
            for cut in cuts:
                x += (-0.5 if float(u) < float(cut["fraction"]) else 0.5) * float(cut.get("gap_m") or 0.0)
            y = (float(v) - 0.5) * size[1]
            if orientation == "horizontal_xy":
                offset = [x, y, 0.015 * x - 0.008 * y]
            elif orientation == "vertical_xz":
                offset = [x, 0.008 * x - 0.004 * y, y]
            else:
                raise ValueError(f"unsupported cloth initial_orientation: {orientation}")
            result[row * n + column] = center + np.asarray(offset, dtype=np.float32)
    return result


def grid_pinned_mask(n: int, boundaries: list[str]) -> np.ndarray:
    mask = np.zeros(n * n, dtype=np.int32)
    for boundary in boundaries:
        if boundary == "negative_u":
            mask[:n] = 1
        elif boundary == "positive_u":
            mask[(n - 1) * n:] = 1
        elif boundary == "negative_v":
            mask[::n] = 1
        elif boundary == "positive_v":
            mask[n - 1::n] = 1
        else:
            raise ValueError(f"unsupported cloth pinned boundary: {boundary}")
    return mask


def _crosses_u_cut(row_a: int, row_b: int, n: int, u_cuts: list[dict[str, float]]) -> bool:
    low, high = sorted((row_a / (n - 1), row_b / (n - 1)))
    return any(low < float(cut["fraction"]) <= high for cut in u_cuts)


def grid_faces(n: int, *, u_cuts: list[dict[str, float]] | None = None) -> np.ndarray:
    faces: list[tuple[int, int, int]] = []
    cuts = u_cuts or []
    for row in range(n - 1):
        for column in range(n - 1):
            if _crosses_u_cut(row, row + 1, n, cuts):
                continue
            a = row * n + column
            b = a + n
            c = b + 1
            d = a + 1
            faces.extend(((a, b, c), (a, c, d)))
    return np.asarray(faces, dtype=np.int32)


def grid_structural_edges(n: int, *, u_cuts: list[dict[str, float]] | None = None) -> np.ndarray:
    edges: list[tuple[int, int]] = []
    cuts = u_cuts or []
    for row in range(n):
        for column in range(n):
            vertex = row * n + column
            if row + 1 < n and not _crosses_u_cut(row, row + 1, n, cuts):
                edges.append((vertex, vertex + n))
            if column + 1 < n:
                edges.append((vertex, vertex + 1))
    return np.asarray(edges, dtype=np.int32)


def grid_spring_data(
    initial: np.ndarray,
    n: int,
    neighbors: tuple[tuple[int, int, float], ...],
    u_cuts: list[dict[str, float]],
) -> tuple[np.ndarray, np.ndarray]:
    rest = np.zeros((n * n, len(neighbors)), dtype=np.float32)
    active = np.zeros((n * n, len(neighbors)), dtype=np.int32)
    for vertex in range(n * n):
        row, column = divmod(vertex, n)
        for neighbor_index, (du, dv, _stiffness) in enumerate(neighbors):
            other_row, other_column = row + du, column + dv
            if not (0 <= other_row < n and 0 <= other_column < n):
                continue
            if _crosses_u_cut(row, other_row, n, u_cuts):
                continue
            other = other_row * n + other_column
            rest[vertex, neighbor_index] = float(np.linalg.norm(initial[other] - initial[vertex]))
            active[vertex, neighbor_index] = 1
    return rest, active


def write_obj(path: Path, positions: np.ndarray, faces: np.ndarray) -> None:
    lines = ["# Derived deformable render surface; canonical state is deformable_cache.npz\n"]
    lines.extend(f"v {float(x):.8f} {float(y):.8f} {float(z):.8f}\n" for x, y, z in positions)
    lines.extend(f"f {int(a) + 1} {int(b) + 1} {int(c) + 1}\n" for a, b, c in faces)
    path.write_text("".join(lines), encoding="utf-8")


def object_with_role(case_data: dict[str, Any], role: str) -> dict[str, Any]:
    matches = [item for item in case_data.get("objects") or [] if isinstance(item, dict) and item.get("role") == role]
    if len(matches) != 1:
        raise ValueError(f"case requires exactly one object with role {role}")
    return matches[0]


def optional_object_with_role(case_data: dict[str, Any], role: str) -> dict[str, Any] | None:
    matches = [item for item in case_data.get("objects") or [] if isinstance(item, dict) and item.get("role") == role]
    if len(matches) > 1:
        raise ValueError(f"case permits at most one object with role {role}")
    return matches[0] if matches else None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Solve a fixed-topology cloth case with isolated Taichi.")
    parser.add_argument("--case", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    case_data = load_deformable_case_data(args.case)
    from harness.core.physics_contract import infer_scene_domain

    if infer_scene_domain(case_data) != "deformable":
        raise SystemExit("Taichi cloth backend requires a deformable-domain scene contract")
    result = simulate(case_data)
    verification = write_run(Path(args.output_dir).expanduser().resolve(), case_data, result)
    print(json.dumps(verification, indent=2, ensure_ascii=False))
    return 0 if verification["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
