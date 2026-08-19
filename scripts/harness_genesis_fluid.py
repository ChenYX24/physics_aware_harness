from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.verification.particle_cache_verifier import verify_particle_cache
from harness.core.artifact_manager import ArtifactManager
from harness.core.workspace import workspace_path
from harness.runtime.genesis_headless import import_headless_genesis


def simulate_fluid(
    *,
    fps: int,
    duration_s: float,
    particle_size: float,
    pre_roll_s: float,
    gravity: list[float],
    liquid_position: list[float],
    liquid_initial_condition: str,
    liquid_shape: str,
    liquid_size: list[float],
    liquid_radius: float,
    liquid_height: float,
    liquid_euler: list[float],
    initial_velocity_field: dict[str, Any],
    surface_smoothing_length: float,
    surface_cube_size: float,
    surface_iso_threshold: float,
    basin_center: list[float],
    basin_floor_z: float,
    basin_half_extent: float,
    rigid_spheres: list[dict[str, Any]],
    minimum_splash_rise: float,
    minimum_float_sink_separation: float,
    minimum_initial_flow_speed: float,
    minimum_horizontal_displacement: float,
    minimum_jet_rise: float,
    minimum_final_surface_component_fraction: float,
    maximum_final_surface_area_to_volume_ratio: float,
    maximum_final_surface_volume_relative_error: float,
) -> dict[str, Any]:
    gs = import_headless_genesis()
    import numpy as np
    import pysplashsurf

    steps_per_frame = 40
    solver_dt = 1.0 / (fps * steps_per_frame)
    center_x, center_y = basin_center
    margin = max(0.15, particle_size * 4.0)
    lower_bound = (center_x - basin_half_extent - margin, center_y - basin_half_extent - margin, basin_floor_z - margin)
    vertical_extent = liquid_radius if liquid_shape == "sphere" else (liquid_height / 2.0 if liquid_shape == "cylinder" else liquid_size[2] / 2.0)
    rigid_top = max((float(item["position_m"][2]) + float(item["radius_m"]) for item in rigid_spheres), default=0.0)
    upper_bound = (
        center_x + basin_half_extent + margin,
        center_y + basin_half_extent + margin,
        max(basin_floor_z + 1.0, liquid_position[2] + vertical_extent + margin, rigid_top + margin),
    )
    gs.init(backend=gs.cpu, logging_level="warning")
    scene = gs.Scene(
        sim_options=gs.options.SimOptions(dt=solver_dt, gravity=tuple(gravity)),
        sph_options=gs.options.SPHOptions(
            dt=solver_dt,
            particle_size=particle_size,
            pressure_solver="WCSPH",
            lower_bound=lower_bound,
            upper_bound=upper_bound,
        ),
        profiling_options=gs.options.ProfilingOptions(show_FPS=False),
        show_viewer=False,
    )
    basin_material = gs.materials.Rigid(needs_coup=True, coup_friction=0.1, coup_softness=0.01)
    for position, normal in (
        ((center_x, center_y, basin_floor_z), (0.0, 0.0, 1.0)),
        ((center_x - basin_half_extent, center_y, basin_floor_z), (1.0, 0.0, 0.0)),
        ((center_x + basin_half_extent, center_y, basin_floor_z), (-1.0, 0.0, 0.0)),
        ((center_x, center_y - basin_half_extent, basin_floor_z), (0.0, 1.0, 0.0)),
        ((center_x, center_y + basin_half_extent, basin_floor_z), (0.0, -1.0, 0.0)),
    ):
        scene.add_entity(morph=gs.morphs.Plane(pos=position, normal=normal), material=basin_material)
    if liquid_initial_condition not in {"bounded_volume", "container_fill"}:
        raise ValueError(f"unsupported liquid initial condition: {liquid_initial_condition}")
    if liquid_shape == "sphere":
        liquid_morph = gs.morphs.Sphere(radius=liquid_radius, pos=tuple(liquid_position))
    elif liquid_shape == "cylinder":
        liquid_morph = gs.morphs.Cylinder(
            radius=liquid_radius,
            height=liquid_height,
            pos=tuple(liquid_position),
            euler=tuple(liquid_euler),
        )
    else:
        liquid_morph = gs.morphs.Box(size=tuple(liquid_size), pos=tuple(liquid_position))
    liquid = scene.add_entity(
        morph=liquid_morph,
        material=gs.materials.SPH.Liquid(sampler="regular"),
    )
    rigid_entities = {
        item["id"]: scene.add_entity(
            morph=gs.morphs.Sphere(radius=float(item["radius_m"]), pos=tuple(item["position_m"])),
            material=gs.materials.Rigid(
                rho=float(item["density_kg_m3"]), needs_coup=True, coup_softness=0.01
            ),
            name=item["id"],
        )
        for item in rigid_spheres
    }
    scene.build()
    for _ in range(max(0, int(round(pre_roll_s / solver_dt)))):
        for item in rigid_spheres:
            rigid_entities[item["id"]].set_pos(tuple(item["position_m"]), zero_velocity=True)
        scene.step()
    for item in rigid_spheres:
        rigid_entities[item["id"]].set_pos(tuple(item["position_m"]), zero_velocity=True)
    initial_positions = np.asarray(tensor_rows(liquid.get_particles_pos()), dtype=np.float32)
    liquid.set_particles_vel(initial_velocity_rows(initial_positions, initial_velocity_field, np))
    frame_count = max(1, int(round(duration_s * fps)))
    frames = []
    for frame_index in range(frame_count + 1):
        positions = tensor_rows(liquid.get_particles_pos())
        velocities = tensor_rows(liquid.get_particles_vel())
        rigid_states = {
            object_id: {
                "position_m": tensor_vector(entity.get_pos()),
                "velocity_m_s": tensor_vector(entity.get_vel()),
            }
            for object_id, entity in rigid_entities.items()
        }
        reconstruction_positions = np.asarray(positions, dtype=np.float32)
        particle_radius = particle_size / 2.0
        reconstruction_mask = np.ones(len(reconstruction_positions), dtype=bool)
        for item in rigid_spheres:
            center = np.asarray(rigid_states[item["id"]]["position_m"], dtype=np.float32)
            reconstruction_mask &= (
                np.linalg.norm(reconstruction_positions - center, axis=1)
                >= float(item["radius_m"]) + particle_radius
            )
        reconstruction_positions = reconstruction_positions[reconstruction_mask]
        if len(reconstruction_positions) < 4:
            raise RuntimeError("surface reconstruction has fewer than four particles after collider filtering")
        reconstruction = pysplashsurf.reconstruct_surface(
            reconstruction_positions,
            particle_radius=particle_radius,
            smoothing_length=surface_smoothing_length,
            cube_size=surface_cube_size,
            iso_surface_threshold=surface_iso_threshold,
            aabb_min=np.asarray(
                [
                    center_x - basin_half_extent,
                    center_y - basin_half_extent,
                    basin_floor_z,
                ],
                dtype=np.float32,
            ),
            aabb_max=np.asarray(
                [
                    center_x + basin_half_extent,
                    center_y + basin_half_extent,
                    upper_bound[2],
                ],
                dtype=np.float32,
            ),
        )
        mesh = reconstruction.mesh
        topology_issue = pysplashsurf.check_mesh_consistency(mesh, reconstruction.grid, check_closed=True, check_manifold=True)
        surface_vertices = confine_surface_vertices(
            mesh.vertices,
            basin_center,
            basin_floor_z,
            basin_half_extent,
            np,
        )
        if len(surface_vertices) == 0 or len(mesh.triangles) == 0:
            retained_bounds = {
                "min_m": [float(value) for value in reconstruction_positions.min(axis=0)],
                "max_m": [float(value) for value in reconstruction_positions.max(axis=0)],
            }
            raise RuntimeError(
                f"surface reconstruction is empty at frame {frame_index}; "
                f"retained_particles={len(reconstruction_positions)}, bounds={retained_bounds}"
            )
        surface_bounds = {
            "min_m": [float(value) for value in surface_vertices.min(axis=0)],
            "max_m": [float(value) for value in surface_vertices.max(axis=0)],
        }
        component_metrics = surface_component_metrics(mesh.triangles, len(surface_vertices))
        shape_metrics = surface_shape_metrics(surface_vertices, mesh.triangles, np)
        rigid_intersection_vertex_count = 0
        for item in rigid_spheres:
            center = np.asarray(rigid_states[item["id"]]["position_m"], dtype=np.float32)
            rigid_intersection_vertex_count += int(
                np.count_nonzero(
                    np.linalg.norm(surface_vertices - center, axis=1) < float(item["radius_m"])
                )
            )
        frames.append(
            {
                "frame": frame_index,
                "time_s": round(frame_index / fps, 8),
                "positions_m": positions,
                "velocities_m_s": velocities,
                "rigid_objects": rigid_states,
                "surface_arrays": {
                    "vertices": surface_vertices,
                    "triangles": mesh.triangles,
                    "topology_consistent": topology_issue is None,
                    "topology_issue": topology_issue,
                    "bounds_m": surface_bounds,
                    "rigid_intersection_vertex_count": rigid_intersection_vertex_count,
                    **component_metrics,
                    **shape_metrics,
                },
            }
        )
        if frame_index < frame_count:
            for _ in range(steps_per_frame):
                scene.step()
    particle_count = len(frames[0]["positions_m"])
    return {
        "schema_version": "harness_particle_cache_v1",
        "backend": "genesis_sph",
        "solver": {
            "genesis_version": str(gs.__version__),
            "backend": "cpu",
            "pressure_solver": "WCSPH",
            "solver_dt_s": solver_dt,
            "gravity_m_s2": gravity,
        },
        "timebase": {
            "fps": fps,
            "output_dt_s": round(1.0 / fps, 10),
            "steps_per_output": steps_per_frame,
            "sampling_phase": "state after previous solver steps; frame 0 is initial state",
            "pre_roll_s": pre_roll_s,
        },
        "particles": {
            "count": particle_count,
            "stable_ids": list(range(particle_count)),
            "radius_m": particle_size / 2.0,
            "rest_density_kg_m3": 1000.0,
        },
        "environment": {
            "type": "five_plane_basin",
            "center_xy_m": basin_center,
            "floor_z_m": basin_floor_z,
            "wall_half_extent_m": basin_half_extent,
            "penetration_tolerance_m": particle_size,
            "collision_backend": "genesis_rigid_sph_legacy_coupler",
            "initial_condition": {
                "type": liquid_initial_condition,
                "shape": liquid_shape,
                **(
                    {"radius_m": liquid_radius}
                    if liquid_shape == "sphere"
                    else (
                        {"radius_m": liquid_radius, "height_m": liquid_height, "euler_deg": liquid_euler}
                        if liquid_shape == "cylinder"
                        else {"size_m": liquid_size}
                    )
                ),
                "velocity_field": initial_velocity_field,
            },
            "initial_liquid_position_m": liquid_position,
            # A splash is the highest later particle above the settled bulk
            # surface.  Using the single highest pre-roll particle as the
            # baseline makes one residual droplet erase a real splash.
            "initial_liquid_surface_z_m": percentile(
                [float(row[2]) for row in frames[0]["positions_m"]], 0.95
            ),
            "initial_liquid_surface_statistic": "particle_z_p95_after_preroll",
            "maximum_initial_surface_outlier_m": 0.08,
            "minimum_splash_rise_m": minimum_splash_rise,
            "minimum_float_sink_separation_m": minimum_float_sink_separation,
            "minimum_initial_flow_speed_m_s": minimum_initial_flow_speed,
            "minimum_horizontal_displacement_m": minimum_horizontal_displacement,
            "minimum_jet_rise_m": minimum_jet_rise,
            "minimum_final_surface_component_fraction": minimum_final_surface_component_fraction,
            "maximum_final_surface_area_to_volume_ratio_1_m": maximum_final_surface_area_to_volume_ratio,
            "maximum_final_surface_volume_relative_error": maximum_final_surface_volume_relative_error,
            "initial_liquid_volume_m3": initial_liquid_volume(liquid_shape, liquid_size, liquid_radius, liquid_height),
            "rigid_objects": rigid_spheres,
        },
        "coupling": {
            "processor": "pysplashsurf",
            "processor_version": "0.14.1.0",
            "smoothing_length_in_particle_radii": surface_smoothing_length,
            "cube_size_in_particle_radii": surface_cube_size,
            "iso_surface_threshold": surface_iso_threshold,
            "basin_reconstruction_inset_m": 0.0,
            "surface_boundary_projection": "clip reconstructed vertices to the interior five-plane basin",
            "rigid_reconstruction_clearance_m": particle_size / 2.0,
            "representation": "per-frame OBJ surface mesh",
            "ue_next_step": "import/cache surface meshes, then replay by frame id",
        },
        "frames": frames,
    }


def tensor_rows(value: Any) -> list[list[float]]:
    array = value.detach().cpu().numpy().reshape(-1, 3)
    return [[round(float(component), 8) for component in row] for row in array]


def initial_velocity_rows(positions: Any, field: dict[str, Any], np_module: Any) -> Any:
    positions = np_module.asarray(positions, dtype=np_module.float32)
    field_type = str(field.get("type") or "still")
    if field_type == "still":
        return np_module.zeros_like(positions)
    if field_type == "uniform":
        velocity = np_module.asarray(field["velocity_m_s"], dtype=np_module.float32)
        return np_module.broadcast_to(velocity, positions.shape).copy()
    if field_type == "swirl_z":
        center = np_module.asarray(field.get("center_m") or [0.0, 0.0, 0.0], dtype=np_module.float32)
        omega = float(field["angular_speed_rad_s"])
        result = np_module.zeros_like(positions)
        result[:, 0] = -omega * (positions[:, 1] - center[1])
        result[:, 1] = omega * (positions[:, 0] - center[0])
        maximum = float(field.get("maximum_speed_m_s") or 1.0)
        speed = np_module.linalg.norm(result[:, :2], axis=1)
        scale = np_module.minimum(1.0, maximum / np_module.maximum(speed, 1e-8))
        result[:, :2] *= scale[:, None]
        return result
    raise ValueError(f"unsupported initial velocity field: {field_type}")


def confine_surface_vertices(
    vertices: Any,
    basin_center: list[float],
    basin_floor_z: float,
    basin_half_extent: float,
    np_module: Any,
) -> Any:
    confined = np_module.asarray(vertices).copy()
    center_x, center_y = [float(value) for value in basin_center]
    confined[:, 0] = np_module.clip(
        confined[:, 0], center_x - basin_half_extent, center_x + basin_half_extent
    )
    confined[:, 1] = np_module.clip(
        confined[:, 1], center_y - basin_half_extent, center_y + basin_half_extent
    )
    confined[:, 2] = np_module.maximum(confined[:, 2], basin_floor_z)
    return confined


def surface_component_metrics(triangles: Any, vertex_count: int) -> dict[str, Any]:
    parent = list(range(vertex_count))
    sizes = [1] * vertex_count

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if sizes[left_root] < sizes[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        sizes[left_root] += sizes[right_root]

    rows = [[int(value) for value in triangle] for triangle in triangles]
    for first, second, third in rows:
        union(first, second)
        union(first, third)
    triangle_counts: dict[int, int] = {}
    for first, _second, _third in rows:
        root = find(first)
        triangle_counts[root] = triangle_counts.get(root, 0) + 1
    largest = max(triangle_counts.values(), default=0)
    return {
        "connected_component_count": len(triangle_counts),
        "largest_component_triangle_fraction": largest / max(1, len(rows)),
    }


def surface_shape_metrics(vertices: Any, triangles: Any, np_module: Any) -> dict[str, float]:
    triangle_vertices = np_module.asarray(vertices)[np_module.asarray(triangles, dtype=np_module.int64)]
    cross = np_module.cross(
        triangle_vertices[:, 1] - triangle_vertices[:, 0],
        triangle_vertices[:, 2] - triangle_vertices[:, 0],
    )
    area = float(0.5 * np_module.linalg.norm(cross, axis=1).sum())
    volume = float(
        abs(
            np_module.einsum(
                "ij,ij->i",
                triangle_vertices[:, 0],
                np_module.cross(triangle_vertices[:, 1], triangle_vertices[:, 2]),
            ).sum()
            / 6.0
        )
    )
    return {
        "surface_area_m2": area,
        "enclosed_volume_m3": volume,
        "surface_area_to_volume_ratio_1_m": area / max(volume, 1e-12),
    }


def initial_liquid_volume(shape: str, size: list[float], radius: float, height: float) -> float:
    if shape == "sphere":
        return 4.0 * math.pi * radius ** 3 / 3.0
    if shape == "cylinder":
        return math.pi * radius ** 2 * height
    return float(size[0]) * float(size[1]) * float(size[2])


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    index = min(len(ordered) - 1, max(0, math.ceil(float(fraction) * len(ordered)) - 1))
    return round(ordered[index], 8)


def tensor_vector(value: Any) -> list[float]:
    return tensor_rows(value)[0]


def apply_negative_mode(cache: dict[str, Any], mode: str) -> dict[str, Any]:
    """Create declared-invalid regression evidence without changing production cases."""
    if not mode:
        return cache
    if mode != "no_gravity_response":
        raise ValueError(f"unsupported Genesis negative mode: {mode}")
    frames = cache.get("frames") if isinstance(cache.get("frames"), list) else []
    if not frames:
        return cache
    initial_positions = [list(row) for row in frames[0].get("positions_m") or []]
    zero_velocities = [[0.0, 0.0, 0.0] for _ in initial_positions]
    for frame in frames[1:]:
        frame["positions_m"] = [list(row) for row in initial_positions]
        frame["velocities_m_s"] = [list(row) for row in zero_velocities]
    cache["negative_fixture"] = {
        "mode": mode,
        "expected_failure": "gravity_direction_not_observed",
        "scope": "verifier_regression_only",
    }
    return cache


def write_fluid_cache(cache: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    surface_dir = output_dir / "surface_frames"
    surface_dir.mkdir(exist_ok=True)
    preview_dir = output_dir / "preview_frames"
    preview_dir.mkdir(exist_ok=True)
    for frame in cache["frames"]:
        arrays = frame.pop("surface_arrays")
        path = surface_dir / f"frame_{int(frame['frame']):04d}.obj"
        write_obj(path, arrays["vertices"], arrays["triangles"])
        render_surface_frame(
            preview_dir / f"frame_{int(frame['frame']):04d}.png",
            arrays["vertices"],
            arrays["triangles"],
            cache.get("environment") or {},
        )
        frame["surface"] = {
            "path": str(path.relative_to(output_dir)),
            "vertex_count": int(len(arrays["vertices"])),
            "triangle_count": int(len(arrays["triangles"])),
            "topology_consistent": bool(arrays["topology_consistent"]),
            "topology_issue": arrays["topology_issue"],
            "bounds_m": arrays["bounds_m"],
            "rigid_intersection_vertex_count": int(arrays["rigid_intersection_vertex_count"]),
            "connected_component_count": int(arrays["connected_component_count"]),
            "largest_component_triangle_fraction": float(arrays["largest_component_triangle_fraction"]),
            "surface_area_m2": float(arrays["surface_area_m2"]),
            "enclosed_volume_m3": float(arrays["enclosed_volume_m3"]),
            "surface_area_to_volume_ratio_1_m": float(arrays["surface_area_to_volume_ratio_1_m"]),
        }
    cache_path = output_dir / "particle_cache.json"
    cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    report = verify_particle_cache(cache, root=output_dir)
    video = output_dir / "video.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(cache["timebase"]["fps"]), "-i", str(preview_dir / "frame_%04d.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    report["video"] = str(video)
    (output_dir / "fluid_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def write_obj(path: Path, vertices: Any, triangles: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for vertex in vertices:
            handle.write(f"v {float(vertex[0]):.8f} {float(vertex[1]):.8f} {float(vertex[2]):.8f}\n")
        for triangle in triangles:
            handle.write(f"f {int(triangle[0]) + 1} {int(triangle[1]) + 1} {int(triangle[2]) + 1}\n")


def render_surface_frame(path: Path, vertices: Any, triangles: Any, environment: dict[str, Any]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    figure = plt.figure(figsize=(6.4, 4.8), dpi=100)
    axis = figure.add_subplot(111, projection="3d")
    center_x, center_y = environment.get("center_xy_m") or [0.0, 0.0]
    extent = float(environment.get("wall_half_extent_m") or 0.3)
    floor_z = float(environment.get("floor_z_m") or 0.0)
    basin = [[
        (center_x - extent, center_y - extent, floor_z),
        (center_x + extent, center_y - extent, floor_z),
        (center_x + extent, center_y + extent, floor_z),
        (center_x - extent, center_y + extent, floor_z),
    ]]
    axis.add_collection3d(Poly3DCollection(basin, facecolor="#cbd3da", edgecolor="#77838e", linewidth=0.5, alpha=0.45))
    axis.add_collection3d(Poly3DCollection(vertices[triangles], facecolor="#4aa3df", edgecolor="#235f82", linewidth=0.08, alpha=0.9))
    axis.set(
        xlim=(center_x - extent * 1.15, center_x + extent * 1.15),
        ylim=(center_y - extent * 1.15, center_y + extent * 1.15),
        zlim=(floor_z, floor_z + max(0.8, extent * 2.5)),
    )
    axis.set_box_aspect((extent * 2.0, extent * 2.0, max(0.8, extent * 2.5)))
    axis.view_init(elev=22, azim=-58)
    axis.set_axis_off()
    figure.tight_layout(pad=0)
    figure.savefig(path, facecolor="#e8edf2")
    plt.close(figure)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Genesis SPH smoke and export a canonical particle plus reconstructed-surface cache.")
    parser.add_argument("--output-dir", default="runs/fluid/genesis_sph_smoke")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--duration", type=float, default=0.75)
    parser.add_argument("--particle-size", type=float, default=0.025)
    parser.add_argument("--pre-roll", type=float, default=0.0)
    parser.add_argument("--gravity", type=float, nargs=3, default=[0.0, 0.0, -9.81])
    parser.add_argument("--liquid-position", type=float, nargs=3, default=[0.0, 0.0, 0.65])
    parser.add_argument("--liquid-initial-condition", choices=("bounded_volume", "container_fill"), default="bounded_volume")
    parser.add_argument("--liquid-shape", choices=("box", "sphere", "cylinder"), default="box")
    parser.add_argument("--liquid-size", type=float, nargs=3, default=[0.3, 0.3, 0.3])
    parser.add_argument("--liquid-radius", type=float, default=0.15)
    parser.add_argument("--liquid-height", type=float, default=0.3)
    parser.add_argument("--liquid-euler", type=float, nargs=3, default=[0.0, 0.0, 0.0])
    parser.add_argument("--initial-velocity-json", default='{"type":"still"}')
    parser.add_argument("--surface-smoothing-length", type=float, default=2.0)
    parser.add_argument("--surface-cube-size", type=float, default=0.75)
    parser.add_argument("--surface-iso-threshold", type=float, default=0.65)
    parser.add_argument("--basin-center", type=float, nargs=2, default=[0.0, 0.0])
    parser.add_argument("--basin-floor-z", type=float, default=0.0)
    parser.add_argument("--basin-half-extent", type=float, default=0.3)
    parser.add_argument("--rigid-spheres-json", default="[]")
    parser.add_argument("--minimum-splash-rise", type=float, default=0.04)
    parser.add_argument("--minimum-float-sink-separation", type=float, default=0.04)
    parser.add_argument("--minimum-initial-flow-speed", type=float, default=0.0)
    parser.add_argument("--minimum-horizontal-displacement", type=float, default=0.0)
    parser.add_argument("--minimum-jet-rise", type=float, default=0.0)
    parser.add_argument("--minimum-final-surface-component-fraction", type=float, default=0.0)
    parser.add_argument("--maximum-final-surface-area-volume-ratio", type=float, default=0.0)
    parser.add_argument("--maximum-final-surface-volume-relative-error", type=float, default=0.0)
    parser.add_argument("--negative-mode", choices=("no_gravity_response",), default="")
    parser.add_argument("--video-root", default="review/probes")
    parser.add_argument("--skip-publish", action="store_true")
    args = parser.parse_args()
    rigid_spheres = json.loads(args.rigid_spheres_json)
    if not isinstance(rigid_spheres, list):
        raise SystemExit("--rigid-spheres-json must decode to a list")
    initial_velocity_field = json.loads(args.initial_velocity_json)
    if not isinstance(initial_velocity_field, dict):
        raise SystemExit("--initial-velocity-json must decode to an object")
    output_dir = workspace_path(args.output_dir, default_relative="runs/fluid/genesis_sph_smoke")
    video_root = workspace_path(args.video_root, default_relative="review/probes")
    cache = simulate_fluid(
        fps=args.fps,
        duration_s=args.duration,
        particle_size=args.particle_size,
        pre_roll_s=args.pre_roll,
        gravity=args.gravity,
        liquid_position=args.liquid_position,
        liquid_initial_condition=args.liquid_initial_condition,
        liquid_shape=args.liquid_shape,
        liquid_size=args.liquid_size,
        liquid_radius=args.liquid_radius,
        liquid_height=args.liquid_height,
        liquid_euler=args.liquid_euler,
        initial_velocity_field=initial_velocity_field,
        surface_smoothing_length=args.surface_smoothing_length,
        surface_cube_size=args.surface_cube_size,
        surface_iso_threshold=args.surface_iso_threshold,
        basin_center=args.basin_center,
        basin_floor_z=args.basin_floor_z,
        basin_half_extent=args.basin_half_extent,
        rigid_spheres=rigid_spheres,
        minimum_splash_rise=args.minimum_splash_rise,
        minimum_float_sink_separation=args.minimum_float_sink_separation,
        minimum_initial_flow_speed=args.minimum_initial_flow_speed,
        minimum_horizontal_displacement=args.minimum_horizontal_displacement,
        minimum_jet_rise=args.minimum_jet_rise,
        minimum_final_surface_component_fraction=args.minimum_final_surface_component_fraction,
        maximum_final_surface_area_to_volume_ratio=args.maximum_final_surface_area_volume_ratio,
        maximum_final_surface_volume_relative_error=args.maximum_final_surface_volume_relative_error,
    )
    cache = apply_negative_mode(cache, args.negative_mode)
    report = write_fluid_cache(cache, output_dir)
    if not args.skip_publish:
        published = ArtifactManager(output_dir).publish_videos(video_root, case_id="fluid_drop_in_basin", backend="genesis_sph")
        report["published_videos"] = [str(path) for path in published]
        (output_dir / "fluid_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"status": report["status"], "output_dir": str(output_dir), **report["checks"]}, indent=2))
    # The process exit status describes execution/artifact generation only.
    # Physical assertion failures remain in fluid_report.json and are consumed
    # by the verifier stage.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
