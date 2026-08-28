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
from harness.core.case_spec import load_case_spec
from harness.runtime.deformable_surface_adapter import prepare_ue_deformable_replay
from harness.verification.deformable_mesh_cache_verifier import (
    verify_deformable_mesh_cache_file,
    verify_deformable_solid_impact_file,
)


def simulate(case_data: dict[str, Any]) -> dict[str, Any]:
    import taichi as ti

    cloth = object_with_role(case_data, "deformable_cloth")
    sphere = optional_object_with_role(case_data, "rigid_collider")
    floor = optional_object_with_role(case_data, "support_surface")
    wind = optional_object_with_role(case_data, "wind_field")
    options = case_data.get("backend_options") or {}
    material = cloth.get("material") or {}

    n = int(cloth.get("grid_resolution") or 25)
    if n < 4:
        raise ValueError("cloth grid_resolution must be at least 4")
    size = [float(value) for value in cloth.get("size_m") or [1.5, 1.5]]
    if len(size) != 2 or min(size) <= 0.0:
        raise ValueError("cloth size_m must contain two positive values")
    center = np.asarray(cloth.get("initial_center_m") or [0.0, 0.0, 1.35], dtype=np.float32)
    orientation = str(cloth.get("initial_orientation") or "horizontal_xy")
    pinned_boundary = str(cloth.get("pinned_boundary") or "none")
    sphere_center = np.asarray((sphere or {}).get("position_m") or [0.0, 0.0, 0.0], dtype=np.float32)
    sphere_radius = float((sphere or {}).get("radius_m") or 0.0)
    floor_z = float((floor or {}).get("z_m") or 0.0)
    total_mass = float(cloth.get("total_mass_kg") or 0.45)
    point_mass = total_mass / (n * n)
    fps = int(options.get("fps") or 24)
    duration_s = float(options.get("duration_s") or 2.5)
    substeps_per_frame = int(options.get("substeps_per_frame") or 80)
    if fps <= 0 or duration_s <= 0.0 or substeps_per_frame <= 0:
        raise ValueError("fps, duration_s, and substeps_per_frame must be positive")
    dt = 1.0 / (fps * substeps_per_frame)
    collision_thickness = float(options.get("constraint_collision_thickness_m") or 0.012)
    structural_stiffness = float(material.get("structural_stiffness_n_m") or 35.0)
    shear_stiffness = float(material.get("shear_stiffness_n_m") or 22.0)
    bending_stiffness = float(material.get("bending_stiffness_n_m") or 4.0)
    spring_damping = float(material.get("spring_damping_kg_s") or 0.003)
    air_damping = float(material.get("air_damping_1_s") or 0.8)
    sphere_friction = float((sphere or {}).get("friction") or 0.0)
    floor_friction = float((floor or {}).get("friction") or 0.0)
    sphere_restitution = float((sphere or {}).get("restitution") or 0.0)
    floor_restitution = float((floor or {}).get("restitution") or 0.0)
    wind_velocity = np.asarray((wind or {}).get("velocity_m_s") or [0.0, 0.0, 0.0], dtype=np.float32)
    air_density = float((wind or {}).get("air_density_kg_m3") or 1.225)
    drag_coefficient = float((wind or {}).get("drag_coefficient") or 1.0)
    gust_amplitude = float((wind or {}).get("gust_amplitude") or 0.0)
    gust_frequency = float((wind or {}).get("gust_frequency_hz") or 0.0)

    ti.init(arch=ti.cpu, default_fp=ti.f32, random_seed=int(options.get("seed") or 0), offline_cache=False)
    vertex_count = n * n
    positions = ti.Vector.field(3, dtype=ti.f32, shape=vertex_count)
    velocities = ti.Vector.field(3, dtype=ti.f32, shape=vertex_count)
    forces = ti.Vector.field(3, dtype=ti.f32, shape=vertex_count)
    pinned = ti.field(dtype=ti.i32, shape=vertex_count)
    simulation_time = ti.field(dtype=ti.f32, shape=())

    initial = grid_positions(n, size, center, orientation=orientation)
    pinned_mask = grid_pinned_mask(n, pinned_boundary)
    faces_np = grid_faces(n)
    faces_ti = ti.Vector.field(3, dtype=ti.i32, shape=len(faces_np))
    positions.from_numpy(initial)
    velocities.fill(0.0)
    pinned.from_numpy(pinned_mask)
    faces_ti.from_numpy(faces_np)

    spacing_x = size[0] / (n - 1)
    spacing_y = size[1] / (n - 1)
    drag = math.exp(-air_damping * dt)
    gravity = ti.Vector([0.0, 0.0, -9.81])
    sphere_center_ti = ti.Vector([float(value) for value in sphere_center])
    wind_velocity_ti = ti.Vector([float(value) for value in wind_velocity])
    has_sphere = sphere is not None
    has_floor = floor is not None
    has_wind = wind is not None and float(np.linalg.norm(wind_velocity)) > 0.0
    spring_neighbors = (
        (-1, 0, spacing_x, structural_stiffness),
        (1, 0, spacing_x, structural_stiffness),
        (0, -1, spacing_y, structural_stiffness),
        (0, 1, spacing_y, structural_stiffness),
        (-1, -1, math.hypot(spacing_x, spacing_y), shear_stiffness),
        (-1, 1, math.hypot(spacing_x, spacing_y), shear_stiffness),
        (1, -1, math.hypot(spacing_x, spacing_y), shear_stiffness),
        (1, 1, math.hypot(spacing_x, spacing_y), shear_stiffness),
        (-2, 0, 2.0 * spacing_x, bending_stiffness),
        (2, 0, 2.0 * spacing_x, bending_stiffness),
        (0, -2, 2.0 * spacing_y, bending_stiffness),
        (0, 2, 2.0 * spacing_y, bending_stiffness),
    )

    @ti.kernel
    def substep():
        for vertex in positions:
            forces[vertex] = gravity * point_mass
        for vertex in positions:
            row = vertex // n
            column = vertex - row * n
            force = forces[vertex]
            for dx, dy, rest_length, stiffness in ti.static(spring_neighbors):
                neighbour_row = row + dx
                neighbour_column = column + dy
                if 0 <= neighbour_row < n and 0 <= neighbour_column < n:
                    neighbour = neighbour_row * n + neighbour_column
                    delta = positions[neighbour] - positions[vertex]
                    distance = delta.norm()
                    if distance > 1e-7:
                        direction = delta / distance
                        relative_speed = (velocities[neighbour] - velocities[vertex]).dot(direction)
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

            if ti.static(has_sphere):
                sphere_delta = positions[vertex] - sphere_center_ti
                sphere_distance = sphere_delta.norm()
                collision_radius = sphere_radius + collision_thickness
                if sphere_distance < collision_radius:
                    normal = sphere_delta / ti.max(sphere_distance, 1e-7)
                    positions[vertex] = sphere_center_ti + collision_radius * normal
                    normal_speed = velocities[vertex].dot(normal)
                    if normal_speed < 0.0:
                        velocities[vertex] -= (1.0 + sphere_restitution) * normal_speed * normal
                    tangent = velocities[vertex] - velocities[vertex].dot(normal) * normal
                    velocities[vertex] -= ti.min(1.0, sphere_friction) * tangent

            if ti.static(has_floor):
                floor_height = floor_z + collision_thickness
                if positions[vertex].z < floor_height:
                    positions[vertex].z = floor_height
                    if velocities[vertex].z < 0.0:
                        velocities[vertex].z *= -floor_restitution
                    velocities[vertex].x *= 1.0 - ti.min(1.0, floor_friction)
                    velocities[vertex].y *= 1.0 - ti.min(1.0, floor_friction)

    frame_count = max(1, int(round(duration_s * fps)))
    position_frames = np.empty((frame_count + 1, vertex_count, 3), dtype=np.float32)
    velocity_frames = np.empty_like(position_frames)
    for frame in range(frame_count + 1):
        position_frames[frame] = positions.to_numpy()
        velocity_frames[frame] = velocities.to_numpy()
        if frame < frame_count:
            for substep_index in range(substeps_per_frame):
                simulation_time[None] = (frame + substep_index / substeps_per_frame) / fps
                substep()

    return {
        "positions_m": position_frames,
        "velocities_m_s": velocity_frames,
        "faces": faces_np,
        "structural_edges": grid_structural_edges(n),
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
            "initial_orientation": orientation,
            "pinned_boundary": pinned_boundary,
            "sphere_center_m": sphere_center.tolist() if sphere is not None else None,
            "sphere_radius_m": sphere_radius if sphere is not None else None,
            "floor_z_m": floor_z if floor is not None else None,
            "wind_velocity_m_s": wind_velocity.tolist() if wind is not None else None,
            "air_density_kg_m3": air_density if wind is not None else None,
            "drag_coefficient": drag_coefficient if wind is not None else None,
            "gust_amplitude": gust_amplitude if wind is not None else None,
            "gust_frequency_hz": gust_frequency if wind is not None else None,
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
            "pinned_boundary": parameters["pinned_boundary"],
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
    sphere = optional_object_with_role(case_data, "rigid_collider")
    floor = optional_object_with_role(case_data, "support_surface")
    wind = optional_object_with_role(case_data, "wind_field")
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
            sphere_center_m=[float(value) for value in sphere["position_m"]] if sphere else None,
            sphere_radius_m=float(sphere["radius_m"]) if sphere else None,
            floor_z_m=float(floor.get("z_m") or 0.0) if floor else None,
            collision_thickness_m=float(parameters["collision_thickness_m"]),
            pinned_indices=result["pinned_indices"].tolist(),
            wind_axis=[float(value) for value in wind["velocity_m_s"]] if wind else None,
        )
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
        "verification": "harness_verifier.json",
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
) -> np.ndarray:
    x_values = np.linspace(-size[0] / 2.0, size[0] / 2.0, n, dtype=np.float32)
    y_values = np.linspace(-size[1] / 2.0, size[1] / 2.0, n, dtype=np.float32)
    result = np.empty((n * n, 3), dtype=np.float32)
    for row, x in enumerate(x_values):
        for column, y in enumerate(y_values):
            if orientation == "horizontal_xy":
                offset = [x, y, 0.015 * x - 0.008 * y]
            elif orientation == "vertical_xz":
                offset = [x, 0.008 * x - 0.004 * y, y]
            else:
                raise ValueError(f"unsupported cloth initial_orientation: {orientation}")
            result[row * n + column] = center + np.asarray(offset, dtype=np.float32)
    return result


def grid_pinned_mask(n: int, boundary: str) -> np.ndarray:
    mask = np.zeros(n * n, dtype=np.int32)
    if boundary == "none":
        return mask
    if boundary != "negative_u":
        raise ValueError(f"unsupported cloth pinned_boundary: {boundary}")
    mask[:n] = 1
    return mask


def grid_faces(n: int) -> np.ndarray:
    faces: list[tuple[int, int, int]] = []
    for row in range(n - 1):
        for column in range(n - 1):
            a = row * n + column
            b = a + n
            c = b + 1
            d = a + 1
            faces.extend(((a, b, c), (a, c, d)))
    return np.asarray(faces, dtype=np.int32)


def grid_structural_edges(n: int) -> np.ndarray:
    edges: list[tuple[int, int]] = []
    for row in range(n):
        for column in range(n):
            vertex = row * n + column
            if row + 1 < n:
                edges.append((vertex, vertex + n))
            if column + 1 < n:
                edges.append((vertex, vertex + 1))
    return np.asarray(edges, dtype=np.int32)


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
    case = load_case_spec(args.case)
    from harness.core.physics_contract import infer_scene_domain

    if infer_scene_domain(case.data) != "deformable":
        raise SystemExit("Taichi cloth backend requires a deformable-domain scene contract")
    result = simulate(case.data)
    verification = write_run(Path(args.output_dir).expanduser().resolve(), case.data, result)
    print(json.dumps(verification, indent=2, ensure_ascii=False))
    return 0 if verification["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
