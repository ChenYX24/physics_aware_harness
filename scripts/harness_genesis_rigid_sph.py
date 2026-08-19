from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.runtime_case import load_runtime_case
from harness.core.workspace import workspace_path
from harness.runtime.rigid_sph_scene import (
    compile_rigid_sph_scene,
    matrix_vector,
    profile_collision_parts,
    point_inside_profile,
    quaternion_from_matrix,
    rotation_matrix_xyz,
    subtract,
    ue_rotation_pyr_from_solver_xyz,
)
from scripts.harness_genesis_fluid import (
    surface_component_metrics,
    surface_shape_metrics,
    tensor_rows,
    tensor_vector,
    write_fluid_cache,
)


def simulate_rigid_sph_scene(case_spec: dict[str, Any]) -> dict[str, Any]:
    wake_macos_display()
    import genesis as gs
    import numpy as np
    import pysplashsurf

    compiled = compile_rigid_sph_scene(case_spec)
    options = case_spec.get("backend_options") if isinstance(case_spec.get("backend_options"), dict) else {}
    physical = case_spec.get("physical_parameters") if isinstance(case_spec.get("physical_parameters"), dict) else {}
    simulation_settings = rigid_sph_simulation_settings(case_spec)
    fps = simulation_settings["fps"]
    duration_s = simulation_settings["duration_s"]
    particle_size = float(options.get("particle_size_m") or 0.006)
    steps_per_frame = simulation_settings["steps_per_frame"]
    initialization = compiled.get("initialization") if isinstance(compiled.get("initialization"), dict) else {}
    pre_roll_s = (
        float(initialization.get("pre_roll_s") or 0.0)
        if initialization.get("declared") is True
        else float(options.get("pre_roll_s") or 0.0)
    )
    solver_dt = 1.0 / (fps * steps_per_frame)
    gravity = physical.get("gravity_m_s2") or [0.0, 0.0, -9.81]
    reconstruction_options = options.get("surface_reconstruction") if isinstance(options.get("surface_reconstruction"), dict) else {}
    smoothing = float(reconstruction_options.get("smoothing_length_in_particle_radii") or 2.0)
    cube_size = float(reconstruction_options.get("cube_size_in_particle_radii") or 0.75)
    iso_threshold = float(reconstruction_options.get("iso_surface_threshold") or 0.65)
    lower = compiled["workspace_bounds_m"]["min_m"]
    upper = compiled["workspace_bounds_m"]["max_m"]

    gs.init(backend=gs.cpu, logging_level="warning")
    scene = gs.Scene(
        sim_options=gs.options.SimOptions(dt=solver_dt, gravity=tuple(gravity)),
        sph_options=gs.options.SPHOptions(
            dt=solver_dt,
            particle_size=particle_size,
            pressure_solver="WCSPH",
            lower_bound=tuple(lower),
            upper_bound=tuple(upper),
        ),
        profiling_options=gs.options.ProfilingOptions(show_FPS=False),
        show_viewer=False,
    )
    rigid_material = gs.materials.Rigid(
        needs_coup=True,
        coup_friction=0.08,
        coup_softness=0.002,
        gravity_compensation=1.0,
    )
    plane_bodies = [body for body in compiled["rigid_bodies"] if body["collision"]["type"] == "plane"]
    if not plane_bodies:
        raise RuntimeError("rigid_sph scene requires at least one plane collision")
    for body in plane_bodies:
        collision = body["collision"]
        scene.add_entity(
            morph=gs.morphs.Plane(pos=tuple(collision["position_m"]), normal=tuple(collision["normal"])),
            material=rigid_material,
        )
    horizontal_planes = [
        body
        for body in plane_bodies
        if abs(float(body["collision"]["normal"][0])) <= 1e-6
        and abs(float(body["collision"]["normal"][1])) <= 1e-6
        and float(body["collision"]["normal"][2]) > 0.0
    ]
    if not horizontal_planes:
        raise RuntimeError("surface reconstruction requires a declared upward horizontal plane")
    floor_z = min(float(body["collision"]["position_m"][2]) for body in horizontal_planes)
    kinematic_entities: dict[str, Any] = {}
    dynamic_entities: dict[str, Any] = {}
    for body in compiled["rigid_bodies"]:
        if body["collision"]["type"] == "axisymmetric_profile":
            if body["mobility"] == "kinematic":
                kinematic_entities[body["id"]] = add_kinematic_axisymmetric_collision(scene, gs, rigid_material, body)
            else:
                add_static_axisymmetric_collision(scene, gs, rigid_material, body)
        elif body["collision"]["type"] == "asset":
            entity = add_asset_collision(scene, gs, body)
            if body["mobility"] == "kinematic":
                kinematic_entities[body["id"]] = entity
            elif body["mobility"] == "dynamic":
                dynamic_entities[body["id"]] = entity
    fluid_spec = compiled["fluid"]
    liquid = scene.add_entity(
        morph=gs.morphs.Cylinder(
            radius=float(fluid_spec["radius_m"]),
            height=float(fluid_spec["height_m"]),
            pos=tuple(fluid_spec["world_position_m"]),
            quat=tuple(fluid_spec["world_quaternion_wxyz"]),
        ),
        material=gs.materials.SPH.Liquid(sampler="regular"),
    )
    scene.build()
    for body in compiled["rigid_bodies"]:
        if body["id"] in dynamic_entities:
            dynamic_entities[body["id"]].set_mass(float(body["mass_kg"]))
            set_dynamic_body_initial_state(dynamic_entities[body["id"]], body, hold=pre_roll_s > 0.0)
    for _ in range(max(0, int(round(pre_roll_s / solver_dt)))):
        for body in compiled["rigid_bodies"]:
            if body["id"] in kinematic_entities:
                set_rigid_body_pose(kinematic_entities[body["id"]], body, 0.0)
            elif body["id"] in dynamic_entities:
                set_dynamic_body_initial_state(dynamic_entities[body["id"]], body, hold=True)
        scene.step()
    for body in compiled["rigid_bodies"]:
        if body["id"] in kinematic_entities:
            set_rigid_body_pose(kinematic_entities[body["id"]], body, 0.0)
        elif body["id"] in dynamic_entities:
            set_dynamic_body_initial_state(dynamic_entities[body["id"]], body, hold=False)

    frame_count = simulation_settings["frame_count"]
    frames: list[dict[str, Any]] = []
    for frame_index in range(simulation_settings["output_frame_count"]):
        positions = tensor_rows(liquid.get_particles_pos())
        velocities = tensor_rows(liquid.get_particles_vel())
        bodies_at_frame: dict[str, dict[str, Any]] = {}
        rigid_states: dict[str, dict[str, Any]] = {}
        for body in compiled["rigid_bodies"]:
            if body["mobility"] == "kinematic":
                position, solver_rotation, ue_rotation = rigid_body_pose_at_time(body, frame_index / fps)
                bodies_at_frame[body["id"]] = rigid_body_at_pose(body, position, solver_rotation, ue_rotation)
                rigid_states[body["id"]] = {
                    "position_m": position,
                    "solver_rotation_xyz_deg": solver_rotation,
                    "ue_rotation_pyr_deg": ue_rotation,
                    "linear_velocity_m_s": [0.0, 0.0, 0.0],
                    "angular_velocity_rad_s": [0.0, 0.0, 0.0],
                    "kinematic": True,
                    "mobility": "kinematic",
                }
            elif body["mobility"] == "dynamic":
                entity = dynamic_entities[body["id"]]
                position = tensor_vector(entity.get_pos())
                solver_rotation = tensor_vector(gs.utils.geom.quat_to_xyz(entity.get_quat(), rpy=True, degrees=True))
                ue_rotation = ue_rotation_pyr_from_solver_xyz(solver_rotation)
                bodies_at_frame[body["id"]] = rigid_body_at_pose(body, position, solver_rotation, ue_rotation)
                rigid_states[body["id"]] = {
                    "position_m": position,
                    "solver_rotation_xyz_deg": solver_rotation,
                    "ue_rotation_pyr_deg": ue_rotation,
                    "linear_velocity_m_s": tensor_vector(entity.get_vel()),
                    "angular_velocity_rad_s": tensor_vector(entity.get_ang()),
                    "kinematic": False,
                    "mobility": "dynamic",
                }
            else:
                bodies_at_frame[body["id"]] = body
                rigid_states[body["id"]] = {
                    "position_m": list(body["transform"]["position_m"]),
                    "solver_rotation_xyz_deg": list(body["transform"]["euler_xyz_deg"]),
                    "ue_rotation_pyr_deg": list(body["transform"]["ue_rotation_pyr_deg"]),
                    "linear_velocity_m_s": [0.0, 0.0, 0.0],
                    "angular_velocity_rad_s": [0.0, 0.0, 0.0],
                    "kinematic": True,
                    "mobility": "static",
                }
        measurements = evaluate_measurements(
            positions,
            bodies_at_frame,
            rigid_states,
            compiled["measurements"],
        )
        reconstruction_positions = np.asarray(positions, dtype=np.float32)
        reconstruction = pysplashsurf.reconstruct_surface(
            reconstruction_positions,
            particle_radius=particle_size / 2.0,
            smoothing_length=smoothing,
            cube_size=cube_size,
            iso_surface_threshold=iso_threshold,
            aabb_min=np.asarray([lower[0], lower[1], floor_z], dtype=np.float32),
            aabb_max=np.asarray(upper, dtype=np.float32),
        )
        mesh = reconstruction.mesh
        topology_issue = pysplashsurf.check_mesh_consistency(
            mesh,
            reconstruction.grid,
            check_closed=True,
            check_manifold=True,
        )
        surface_vertices = np.asarray(mesh.vertices).copy()
        surface_vertices[:, 2] = np.maximum(surface_vertices[:, 2], floor_z)
        if not len(surface_vertices) or not len(mesh.triangles):
            raise RuntimeError(f"surface reconstruction is empty at frame {frame_index}")
        frames.append(
            {
                "frame": frame_index,
                "time_s": round(frame_index / fps, 8),
                "positions_m": positions,
                "velocities_m_s": velocities,
                "rigid_objects": rigid_states,
                "measurements": measurements,
                "surface_arrays": {
                    "vertices": surface_vertices,
                    "triangles": mesh.triangles,
                    "topology_consistent": topology_issue is None,
                    "topology_issue": topology_issue,
                    "bounds_m": {
                        "min_m": [float(value) for value in surface_vertices.min(axis=0)],
                        "max_m": [float(value) for value in surface_vertices.max(axis=0)],
                    },
                    "rigid_intersection_vertex_count": 0,
                    **surface_component_metrics(mesh.triangles, len(surface_vertices)),
                    **surface_shape_metrics(surface_vertices, mesh.triangles, np),
                },
            }
        )
        if frame_index < frame_count:
            for substep in range(steps_per_frame):
                current_time = (frame_index + substep / steps_per_frame) / fps
                next_time = (frame_index + (substep + 1) / steps_per_frame) / fps
                for body in compiled["rigid_bodies"]:
                    if body["id"] in kinematic_entities:
                        set_rigid_body_pose(kinematic_entities[body["id"]], body, current_time, next_time_s=next_time)
                scene.step()

    particle_count = len(frames[0]["positions_m"])
    initial_volume = math.pi * float(fluid_spec["radius_m"]) ** 2 * float(fluid_spec["height_m"])
    return {
        "schema_version": "harness_particle_cache_v1",
        "backend": "genesis_sph",
        "solver": {
            "genesis_version": str(gs.__version__),
            "backend": "cpu",
            "pressure_solver": "WCSPH",
            "solver_dt_s": solver_dt,
            "gravity_m_s2": list(gravity),
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
            "type": "rigid_sph_scene",
            "floor_z_m": floor_z,
            "workspace_bounds_m": compiled["workspace_bounds_m"],
            "penetration_tolerance_m": particle_size,
            "collision_backend": "genesis_rigid_sph_coupler",
            "collision_representation": "declared_rigid_body_colliders",
            "rigid_bodies": [without_parts(body) for body in compiled["rigid_bodies"]],
            "measurements": compiled["measurements"],
            "assertions": compiled["assertions"],
            "initial_condition": {
                "type": "bounded_volume",
                "shape": "cylinder",
                "frame": fluid_spec["frame"],
                "velocity_field": {"type": "still"},
            },
            "initial_liquid_position_m": fluid_spec["world_position_m"],
            "initial_liquid_volume_m3": initial_volume,
            "surface_container_intersection_metric": "not_applied_for_boundary_contacting_fluid",
            "minimum_splash_rise_m": 0.0,
            "minimum_float_sink_separation_m": 0.0,
            "minimum_initial_flow_speed_m_s": 0.0,
            "minimum_horizontal_displacement_m": 0.0,
            "minimum_jet_rise_m": 0.0,
            "minimum_final_surface_component_fraction": 0.0,
            "maximum_final_surface_area_to_volume_ratio_1_m": 0.0,
            "maximum_final_surface_volume_relative_error": 0.0,
            "rigid_objects": [],
        },
        "coupling": {
            "processor": "pysplashsurf",
            "processor_version": "0.14.1.0",
            "smoothing_length_in_particle_radii": smoothing,
            "cube_size_in_particle_radii": cube_size,
            "iso_surface_threshold": iso_threshold,
            "surface_boundary_projection": "lowest_horizontal_plane_only",
            "representation": "per-frame OBJ surface mesh",
            "ue_next_step": "replay surface and declared rigid-body transforms",
        },
        "frames": frames,
    }


def rigid_sph_simulation_settings(case_spec: dict[str, Any]) -> dict[str, Any]:
    """Resolve the physical observation window independently of render profile."""
    scene = case_spec.get("scene") if isinstance(case_spec.get("scene"), dict) else {}
    options = case_spec.get("backend_options") if isinstance(case_spec.get("backend_options"), dict) else {}
    fps = int(options.get("fps") or 24)
    steps_per_frame = int(options.get("steps_per_frame") or 145)
    if scene.get("duration_s") is None:
        raise ValueError("rigid_sph requires scene.duration_s")
    duration_s = float(scene["duration_s"])
    if fps <= 0 or steps_per_frame <= 0 or duration_s <= 0.0:
        raise ValueError("rigid_sph fps, steps_per_frame, and scene.duration_s must be positive")
    frame_count = max(1, int(round(duration_s * fps)))
    return {
        "fps": fps,
        "duration_s": duration_s,
        "steps_per_frame": steps_per_frame,
        "frame_count": frame_count,
        "output_frame_count": frame_count + 1,
    }


def add_static_axisymmetric_collision(
    scene: Any,
    gs: Any,
    material: Any,
    body: dict[str, Any],
) -> list[Any]:
    entities = []
    for part in body["collision"]["parts"]:
        if part["kind"] == "box":
            morph = gs.morphs.Box(
                size=tuple(part["size_m"]),
                pos=tuple(part["position_m"]),
                quat=tuple(part["quaternion_wxyz"]),
                fixed=True,
                visualization=False,
            )
        else:
            morph = gs.morphs.Cylinder(
                radius=float(part["radius_m"]),
                height=float(part["height_m"]),
                pos=tuple(part["position_m"]),
                quat=tuple(part["quaternion_wxyz"]),
                fixed=True,
                visualization=False,
            )
        entities.append(scene.add_entity(morph=morph, material=material))
    return entities


def add_asset_collision(scene: Any, gs: Any, body: dict[str, Any]) -> Any:
    collision = body["collision"]
    material_spec = body.get("material") if isinstance(body.get("material"), dict) else {}
    material_options: dict[str, Any] = {
        "needs_coup": True,
        "coup_softness": 0.002,
        "gravity_compensation": 0.0 if body["mobility"] == "dynamic" else 1.0,
    }
    if material_spec.get("density_kg_m3") is not None:
        material_options["rho"] = float(material_spec["density_kg_m3"])
    if material_spec.get("dynamic_friction") is not None:
        material_options["friction"] = float(material_spec["dynamic_friction"])
        material_options["coup_friction"] = float(material_spec["dynamic_friction"])
    if material_spec.get("restitution") is not None:
        material_options["coup_restitution"] = float(material_spec["restitution"])
    return scene.add_entity(
        morph=gs.morphs.Mesh(
            file=collision["portable_mesh_path"],
            scale=tuple(body["transform"]["scale"]),
            pos=tuple(body["transform"]["position_m"]),
            euler=tuple(body["transform"]["euler_xyz_deg"]),
            fixed=body["mobility"] == "static",
            visualization=False,
            collision=True,
            convexify=True,
            decompose_object_error_threshold=0.0,
            recompute_inertia=True,
            align=False,
            file_meshes_are_zup=True,
        ),
        material=gs.materials.Rigid(**material_options),
    )


def add_kinematic_axisymmetric_collision(scene: Any, gs: Any, material: Any, body: dict[str, Any]) -> Any:
    collision = body["collision"]
    parts = profile_collision_parts(
        [0.0, 0.0, 0.0],
        rotation_matrix_xyz([0.0, 0.0, 0.0]),
        collision["inner_profile"],
        float(collision["wall_thickness_m"]),
        int(collision["panel_count"]),
    )
    geoms = []
    for part in parts:
        pos = " ".join(str(value) for value in part["position_m"])
        quat = " ".join(str(value) for value in part["quaternion_wxyz"])
        if part["kind"] == "box":
            size = " ".join(str(float(value) / 2.0) for value in part["size_m"])
            geoms.append(f'<geom type="box" pos="{pos}" quat="{quat}" size="{size}"/>')
        else:
            size = f'{part["radius_m"]} {float(part["height_m"]) / 2.0}'
            geoms.append(f'<geom type="cylinder" pos="{pos}" quat="{quat}" size="{size}"/>')
    body_pos = " ".join(str(value) for value in body["transform"]["position_m"])
    body_quat = " ".join(str(value) for value in quaternion_from_matrix(rotation_matrix_xyz(body["transform"]["euler_xyz_deg"])))
    xml = (
        '<mujoco model="rigid_body"><worldbody>'
        f'<body name="rigid_body" pos="{body_pos}" quat="{body_quat}">'
        '<freejoint/><inertial pos="0 0 0" mass="1" diaginertia="0.01 0.01 0.01"/>'
        + "".join(geoms)
        + "</body></worldbody></mujoco>"
    )
    return scene.add_entity(
        morph=gs.morphs.MJCF(file=xml, visualization=False, requires_jac_and_IK=False),
        material=material,
    )


def set_rigid_body_pose(
    entity: Any,
    body: dict[str, Any],
    time_s: float,
    *,
    next_time_s: float | None = None,
) -> None:
    position, solver_rotation, _ue_rotation = rigid_body_pose_at_time(body, time_s)
    linear_velocity = [0.0, 0.0, 0.0]
    angular_velocity = [0.0, 0.0, 0.0]
    if next_time_s is not None:
        dt = float(next_time_s) - float(time_s)
        if dt <= 0.0:
            raise ValueError("rigid-body pose next_time_s must be greater than time_s")
        next_position, next_rotation, _next_ue_rotation = rigid_body_pose_at_time(body, next_time_s)
        linear_velocity = [(after - before) / dt for before, after in zip(position, next_position, strict=True)]
        angular_velocity = [math.radians(after - before) / dt for before, after in zip(solver_rotation, next_rotation, strict=True)]
    entity.set_pos(
        tuple(position),
        zero_velocity=True,
        relative=False,
        skip_forward=True,
    )
    entity.set_quat(
        tuple(quaternion_from_matrix(rotation_matrix_xyz(solver_rotation))),
        zero_velocity=True,
        relative=False,
        skip_forward=True,
    )
    entity.set_dofs_velocity((*linear_velocity, *angular_velocity), skip_forward=False)


def set_dynamic_body_initial_state(entity: Any, body: dict[str, Any], *, hold: bool) -> None:
    transform = body["transform"]
    entity.set_pos(
        tuple(transform["position_m"]),
        zero_velocity=True,
        relative=False,
        skip_forward=True,
    )
    entity.set_quat(
        tuple(quaternion_from_matrix(rotation_matrix_xyz(transform["euler_xyz_deg"]))),
        zero_velocity=True,
        relative=False,
        skip_forward=True,
    )
    velocity = [0.0] * 6 if hold else [
        *body["initial_linear_velocity_m_s"],
        *body["initial_angular_velocity_rad_s"],
    ]
    entity.set_dofs_velocity(tuple(velocity), skip_forward=False)


def rigid_body_pose_at_time(body: dict[str, Any], time_s: float) -> tuple[list[float], list[float], list[float]]:
    solver_rotation, ue_rotation = rigid_body_rotations_at_time(body, time_s)
    motion = body.get("motion")
    if not isinstance(motion, dict):
        return list(body["transform"]["position_m"]), solver_rotation, ue_rotation
    position = subtract(
        motion["pivot_world_m"],
        matrix_vector(rotation_matrix_xyz(solver_rotation), motion["pivot_local_m"]),
    )
    return position, solver_rotation, ue_rotation


def rigid_body_rotations_at_time(body: dict[str, Any], time_s: float) -> tuple[list[float], list[float]]:
    transform = body["transform"]
    motion = body.get("motion")
    solver_start = list(transform["euler_xyz_deg"])
    ue_start = list(transform["ue_rotation_pyr_deg"])
    if not isinstance(motion, dict):
        return solver_start, ue_start
    duration = float(motion["duration_s"])
    fraction = max(0.0, min(1.0, (float(time_s) - float(motion["start_time_s"])) / duration))
    fraction = fraction * fraction * (3.0 - 2.0 * fraction)
    return (
        interpolate_rotation(solver_start, motion["solver_end_rotation_xyz_deg"], fraction),
        interpolate_rotation(ue_start, motion["ue_end_rotation_pyr_deg"], fraction),
    )


def interpolate_rotation(start: list[float], end: list[float], fraction: float) -> list[float]:
    return [float(start[index]) + (float(end[index]) - float(start[index])) * fraction for index in range(3)]


def rigid_body_at_pose(
    body: dict[str, Any],
    position: list[float],
    solver_rotation: list[float],
    ue_rotation: list[float],
) -> dict[str, Any]:
    return {
        **body,
        "transform": {
            **body["transform"],
            "position_m": list(position),
            "euler_xyz_deg": list(solver_rotation),
            "ue_rotation_pyr_deg": list(ue_rotation),
        },
    }


def evaluate_measurements(
    positions: list[list[float]],
    bodies: dict[str, dict[str, Any]],
    rigid_states: dict[str, dict[str, Any]],
    definitions: list[dict[str, Any]],
) -> dict[str, float]:
    total = max(1, len(positions))
    result: dict[str, float] = {}
    for definition in definitions:
        kind = definition["type"]
        if kind == "body_interior_fraction":
            count = sum(point_inside_profile(row, bodies[definition["body_id"]]) for row in positions)
            value = count / total
        elif kind == "outside_body_interiors_fraction":
            selected = [bodies[body_id] for body_id in definition["body_ids"]]
            count = sum(not any(point_inside_profile(row, body) for body in selected) for row in positions)
            value = count / total
        elif kind == "plane_proximity_fraction":
            collision = bodies[definition["body_id"]]["collision"]
            normal_length = math.sqrt(sum(float(value) ** 2 for value in collision["normal"]))
            normal = [float(value) / normal_length for value in collision["normal"]]
            origin = collision["position_m"]
            distance = float(definition["distance_m"])
            count = sum(abs(sum((float(row[axis]) - float(origin[axis])) * normal[axis] for axis in range(3))) <= distance for row in positions)
            value = count / total
        elif kind == "axis_span":
            axis_indices = {"x": 0, "y": 1, "z": 2}
            value = max(
                max(float(row[axis_indices[axis]]) for row in positions) - min(float(row[axis_indices[axis]]) for row in positions)
                for axis in definition["axes"]
            ) if positions else 0.0
        else:
            state = rigid_states[definition["body_id"]]
            vector = [float(component) for component in state[definition["field"]]]
            component = definition["component"]
            value = (
                math.sqrt(sum(item * item for item in vector))
                if component == "magnitude"
                else vector[{"x": 0, "y": 1, "z": 2}[component]]
            )
        result[definition["id"]] = value
    return result


def without_parts(body: dict[str, Any]) -> dict[str, Any]:
    collision = dict(body["collision"])
    collision.pop("parts", None)
    return {**body, "collision": collision}


def wake_macos_display() -> None:
    """Genesis' offscreen rasterizer still needs a Cocoa screen on macOS."""
    caffeinate = shutil.which("caffeinate")
    if sys.platform == "darwin" and caffeinate:
        subprocess.run([caffeinate, "-u", "-t", "2"], check=False, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a declarative Genesis rigid-body/SPH scene and export canonical truth.")
    parser.add_argument("--case", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--skip-publish", action="store_true")
    args = parser.parse_args()
    case = load_runtime_case(args.case)
    output_dir = workspace_path(args.output_dir, default_relative="runs/fluid/rigid_sph")
    compiled = compile_rigid_sph_scene(case.data)
    (output_dir / "rigid_sph_scene.json").parent.mkdir(parents=True, exist_ok=True)
    (output_dir / "rigid_sph_scene.json").write_text(
        json.dumps(compiled, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    report = write_fluid_cache(simulate_rigid_sph_scene(case.data), output_dir)
    print(json.dumps({"status": report["status"], "output_dir": str(output_dir), **report["checks"]}, indent=2))
    # Physical assertions are represented by fluid_report.json and the
    # verifier Stage Result. Reaching this point means execution and artifact
    # generation succeeded, regardless of the assertion verdict.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
