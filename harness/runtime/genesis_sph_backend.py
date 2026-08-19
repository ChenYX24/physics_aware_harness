from __future__ import annotations

import math
import os
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from harness.core.artifact_schema import read_json, runtime_summary, write_json
from harness.core.runtime_case import RuntimeCase
from harness.core.physics_contract import infer_scene_domain
from harness.core.workspace import workspace_root
from harness.runtime.rigid_sph_scene import compile_rigid_sph_scene
from harness.runtime.fluid_surface_adapter import file_sha256, prepare_ue_surface_replay
from harness.verification.physics_verifier import PhysicsVerifier


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UE_MAP = "/Engine/Maps/Templates/Template_Default.Template_Default"


class GenesisSPHExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GenesisSPHBackend:
    name = "genesis_sph"

    def run_case(
        self,
        case: RuntimeCase,
        output_root: str | Path,
        *,
        requested_views: list[str] | None = None,
        render_passes: list[str] | None = None,
        camera_strategy: str = "bounds_auto_v1",
    ) -> Path:
        if infer_scene_domain(case.data) != "particle":
            raise ValueError("genesis_sph requires a particle-domain scene contract")
        run_dir = Path(output_root) / f"{case.case_id}_{self.name}"
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(run_dir / "case_spec.json", case.data)
        executable = genesis_python()
        if not executable.is_file():
            report = {
                "schema_version": "harness_genesis_sph_backend_report_v1",
                "backend": self.name,
                "case_id": case.case_id,
                "capability_id": case.capability_id,
                "status": "failed_unavailable",
                "process_isolation": str(executable),
                "returncode": None,
                "stderr": "Genesis environment missing",
            }
            write_json(run_dir / "genesis_sph_backend_report.json", report)
            write_genesis_artifacts(case, run_dir)
            raise RuntimeError(
                "Genesis environment missing. Set SIM_GENESIS_PYTHON or create "
                f"{workspace_root() / 'envs' / 'genesis'} with genesis-world and pysplashsurf."
            )
        parameters = genesis_parameters(case.data)
        command = genesis_command(executable, run_dir, case.data.get("backend_options"), parameters)
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=genesis_child_environment(run_dir),
            text=True,
            capture_output=True,
            check=False,
        )
        report = {
            "schema_version": "harness_genesis_sph_backend_report_v1",
            "backend": self.name,
            "case_id": case.case_id,
            "capability_id": case.capability_id,
            "status": "completed" if result.returncode == 0 else "failed",
            "process_isolation": str(executable),
            "command": command,
            "parameters": parameters,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "failure_code": "genesis_sph_process_failed" if result.returncode != 0 else None,
        }
        write_json(run_dir / "genesis_sph_backend_report.json", report)
        if result.returncode != 0:
            raise GenesisSPHExecutionError(
                "genesis_sph_process_failed",
                f"Genesis SPH backend failed with exit code {result.returncode}; "
                f"see {run_dir / 'genesis_sph_backend_report.json'}",
            )
        verifier = write_genesis_artifacts(case, run_dir)
        report["verification_status"] = verifier["status"]
        write_json(run_dir / "genesis_sph_backend_report.json", report)
        return run_dir


def genesis_child_environment(run_dir: str | Path) -> dict[str, str]:
    """Keep Genesis imports headless without changing the controller process."""
    config_dir = Path(run_dir) / ".matplotlib"
    config_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["MPLBACKEND"] = "Agg"
    environment["MPLCONFIGDIR"] = str(config_dir.resolve())
    return environment


def run_ue_surface_replay(
    run_dir: str | Path,
    *,
    profile: str,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """Render one completed Genesis particle/surface cache with resolved UE assets."""
    run_dir = Path(run_dir).resolve()
    particle_cache = run_dir / "particle_cache.json"
    case_spec = run_dir / "case_spec.json"
    if not particle_cache.is_file() or not case_spec.is_file():
        raise RuntimeError("Genesis-to-UE replay requires particle_cache.json and case_spec.json")

    ue_project = os.environ.get("SIM_STUDIO_UE_PROJECT", "").strip()
    if not ue_project:
        candidate = workspace_root() / "ue" / "SimulatorWorkspace.uproject"
        ue_project = str(candidate) if candidate.is_file() else ""
    if not ue_project or not Path(ue_project).is_file():
        raise RuntimeError("Genesis-to-UE replay requires SIM_STUDIO_UE_PROJECT or the initialized workspace UE project")

    configured_executable = os.environ.get("SIM_STUDIO_UE_EXECUTABLE", "").strip()
    if not configured_executable:
        raise RuntimeError("Genesis-to-UE replay requires SIM_STUDIO_UE_EXECUTABLE")
    ue_executable = Path(configured_executable).expanduser()
    if not ue_executable.is_file():
        raise RuntimeError(f"SIM_STUDIO_UE_EXECUTABLE does not exist: {ue_executable}")

    replay_input = run_dir / "ue_replay_input"
    cache_digest = file_sha256(particle_cache)
    replay = prepare_ue_surface_replay(
        particle_cache,
        replay_input,
        ue_asset_root=f"/Game/HarnessGenerated/Fluid/Cache_{cache_digest[:16]}",
    )
    replay_manifest = replay_input / "fluid_surface_replay.json"
    solver_preview = run_dir / "video.mp4"
    render_manifest_path = run_dir / "render_manifest.json"
    render_manifest = read_json(render_manifest_path) if render_manifest_path.is_file() else {}
    if solver_preview.is_file() and render_manifest.get("render_kind") == "solver_surface_preview":
        solver_preview.replace(run_dir / "solver_preview.mp4")

    command = [
        sys.executable,
        str(ROOT / "scripts" / "harness_render_fluid_ue.py"),
        str(replay_manifest),
        "--particle-cache",
        str(particle_cache),
        "--case",
        str(case_spec),
        "--run-dir",
        str(run_dir),
        "--ue-project",
        ue_project,
        "--ue-executable",
        str(ue_executable),
        "--map",
        os.environ.get("SIM_STUDIO_UE_MAP") or DEFAULT_UE_MAP,
        "--profile",
        profile,
    ]
    if width is not None and height is not None:
        command.extend(("--width", str(width), "--height", str(height)))
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    report = {
        "schema_version": "harness_staged_render_report_v1",
        "solver_backend": "genesis_sph",
        "render_backend": "ue",
        "status": "completed" if result.returncode == 0 else "failed",
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "surface_replay": str(replay_manifest.relative_to(run_dir)),
        "frame_count": int((replay.get("timebase") or {}).get("frame_count") or 0),
    }
    write_json(run_dir / "staged_render_report.json", report)
    if result.returncode != 0:
        raise RuntimeError(
            f"Genesis-to-UE replay failed with exit code {result.returncode}; see {run_dir / 'staged_render_report.json'}"
        )
    return report


def genesis_python() -> Path:
    configured = os.environ.get("SIM_GENESIS_PYTHON")
    if configured:
        return Path(configured).expanduser()
    return workspace_root() / "envs" / "genesis" / "bin" / "python"


def genesis_parameters(case_spec: dict[str, Any]) -> dict[str, Any]:
    options = case_spec.get("backend_options") if isinstance(case_spec.get("backend_options"), dict) else {}
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    physical = case_spec.get("physical_parameters") if isinstance(case_spec.get("physical_parameters"), dict) else {}
    objects = [item for item in case_spec.get("objects") or [] if isinstance(item, dict)]
    solver_scene = case_spec.get("solver_scene") if isinstance(case_spec.get("solver_scene"), dict) else {}
    if solver_scene.get("type") == "rigid_sph":
        return compile_rigid_sph_scene(case_spec)
    liquid = next((item for item in objects if str(item.get("role") or "") in {"fluid", "fluid_volume"}), {})
    basin = next((item for item in objects if str(item.get("role") or "") in {"rigid_container", "basin"}), {})
    coordinate_system = str(expected.get("coordinate_system") or "z_up")
    if coordinate_system != "z_up":
        raise ValueError(f"genesis_sph requires z_up coordinates, got {coordinate_system}")
    raw_gravity = physical.get("gravity_m_s2", expected.get("gravity_m_s2", 9.81))
    if isinstance(raw_gravity, (int, float)):
        gravity = [0.0, 0.0, -abs(float(raw_gravity))]
    else:
        gravity = vec(raw_gravity, [0.0, 0.0, -9.81], "gravity_m_s2")
    liquid_position = vec(liquid.get("initial_position_m"), [0.0, 0.0, 0.65], "fluid.initial_position_m")
    initial = liquid.get("initial_condition") if isinstance(liquid.get("initial_condition"), dict) else {}
    initial_type = str(initial.get("type") or "bounded_volume")
    if initial_type not in {"bounded_volume", "container_fill"}:
        raise ValueError(f"genesis_sph does not yet implement fluid initial_condition.type={initial_type}")
    liquid_shape = str(initial.get("shape") or "box")
    if liquid_shape not in {"box", "sphere", "cylinder"}:
        raise ValueError(f"genesis_sph does not support bounded_volume shape={liquid_shape}")
    liquid_size = vec(initial.get("size_m", liquid.get("size_m", options.get("liquid_size_m"))), [0.3, 0.3, 0.3], "fluid.initial_condition.size_m", scalar=True)
    liquid_radius = positive_float(initial.get("radius_m", options.get("liquid_radius_m", 0.15)), "fluid.initial_condition.radius_m")
    liquid_height = positive_float(initial.get("height_m", options.get("liquid_height_m", 0.3)), "fluid.initial_condition.height_m")
    liquid_euler = vec(initial.get("euler_deg"), [0.0, 0.0, 0.0], "fluid.initial_condition.euler_deg")
    initial_velocity_field = fluid_velocity_field_parameters(initial.get("velocity_field"))
    reconstruction = options.get("surface_reconstruction") if isinstance(options.get("surface_reconstruction"), dict) else {}
    basin_position = vec(basin.get("initial_position_m"), [0.0, 0.0, 0.0], "basin.initial_position_m")
    floor_z = finite_float(basin.get("floor_z_m", basin_position[2]), "basin.floor_z_m")
    half_extent = positive_float(basin.get("wall_half_extent_m", 0.3), "basin.wall_half_extent_m")
    rigid_spheres = [rigid_sphere_parameters(item) for item in objects if str(item.get("role") or "") in {"buoyant_body", "dense_body"}]
    return {
        "gravity_m_s2": gravity,
        "liquid_position_m": liquid_position,
        "liquid_initial_condition_type": initial_type,
        "liquid_shape": liquid_shape,
        "liquid_size_m": liquid_size,
        "liquid_radius_m": liquid_radius,
        "liquid_height_m": liquid_height,
        "liquid_euler_deg": liquid_euler,
        "initial_velocity_field": initial_velocity_field,
        "surface_reconstruction": {
            "smoothing_length_in_particle_radii": positive_float(reconstruction.get("smoothing_length_in_particle_radii", 2.0), "surface reconstruction smoothing length"),
            "cube_size_in_particle_radii": positive_float(reconstruction.get("cube_size_in_particle_radii", 0.75), "surface reconstruction cube size"),
            "iso_surface_threshold": positive_float(reconstruction.get("iso_surface_threshold", 0.65), "surface reconstruction iso threshold"),
        },
        "basin_center_xy_m": basin_position[:2],
        "basin_floor_z_m": floor_z,
        "basin_half_extent_m": half_extent,
        "rigid_spheres": rigid_spheres,
        "minimum_splash_rise_m": positive_float(expected.get("minimum_splash_rise_m", 0.04), "expected_physics.minimum_splash_rise_m"),
        "minimum_float_sink_separation_m": positive_float(expected.get("minimum_float_sink_separation_m", 0.04), "expected_physics.minimum_float_sink_separation_m"),
        "minimum_initial_flow_speed_m_s": max(0.0, finite_float(expected.get("minimum_initial_flow_speed_m_s", 0.0), "expected_physics.minimum_initial_flow_speed_m_s")),
        "minimum_horizontal_displacement_m": max(0.0, finite_float(expected.get("minimum_horizontal_displacement_m", 0.0), "expected_physics.minimum_horizontal_displacement_m")),
        "minimum_jet_rise_m": max(0.0, finite_float(expected.get("minimum_jet_rise_m", 0.0), "expected_physics.minimum_jet_rise_m")),
        "minimum_final_surface_component_fraction": max(0.0, min(1.0, finite_float(expected.get("minimum_final_surface_component_fraction", 0.0), "expected_physics.minimum_final_surface_component_fraction"))),
        "maximum_final_surface_area_to_volume_ratio_1_m": max(0.0, finite_float(expected.get("maximum_final_surface_area_to_volume_ratio_1_m", 0.0), "expected_physics.maximum_final_surface_area_to_volume_ratio_1_m")),
        "maximum_final_surface_volume_relative_error": max(0.0, finite_float(expected.get("maximum_final_surface_volume_relative_error", 0.0), "expected_physics.maximum_final_surface_volume_relative_error")),
        "negative_mode": str(case_spec.get("negative_mode") or ""),
    }


def genesis_command(executable: Path, run_dir: Path, options: Any, parameters: dict[str, Any] | None = None) -> list[str]:
    values = options if isinstance(options, dict) else {}
    settings = parameters or {
        "gravity_m_s2": [0.0, 0.0, -9.81],
        "liquid_position_m": [0.0, 0.0, 0.65],
        "liquid_initial_condition_type": "bounded_volume",
        "liquid_shape": "box",
        "liquid_size_m": vec(values.get("liquid_size_m"), [0.3, 0.3, 0.3], "liquid_size_m", scalar=True),
        "liquid_radius_m": positive_float(values.get("liquid_radius_m", 0.15), "liquid_radius_m"),
        "liquid_height_m": positive_float(values.get("liquid_height_m", 0.3), "liquid_height_m"),
        "liquid_euler_deg": [0.0, 0.0, 0.0],
        "initial_velocity_field": {"type": "still"},
        "surface_reconstruction": {
            "smoothing_length_in_particle_radii": 2.0,
            "cube_size_in_particle_radii": 0.75,
            "iso_surface_threshold": 0.65,
        },
        "basin_center_xy_m": [0.0, 0.0],
        "basin_floor_z_m": 0.0,
        "basin_half_extent_m": 0.3,
        "rigid_spheres": [],
        "minimum_splash_rise_m": 0.04,
        "minimum_float_sink_separation_m": 0.04,
        "minimum_initial_flow_speed_m_s": 0.0,
        "minimum_horizontal_displacement_m": 0.0,
        "minimum_jet_rise_m": 0.0,
        "minimum_final_surface_component_fraction": 0.0,
        "maximum_final_surface_area_to_volume_ratio_1_m": 0.0,
        "maximum_final_surface_volume_relative_error": 0.0,
    }
    if settings.get("execution_contract") == "rigid_sph_scene":
        return [
            str(executable),
            str(ROOT / "scripts" / "harness_genesis_rigid_sph.py"),
            "--case",
            str(run_dir / "case_spec.json"),
            "--output-dir",
            str(run_dir),
            "--skip-publish",
        ]
    command = [
        str(executable),
        str(ROOT / "scripts" / "harness_genesis_fluid.py"),
        "--output-dir",
        str(run_dir),
        "--fps",
        str(int(values.get("fps") or 24)),
        "--duration",
        str(float(values.get("duration_s") or 0.75)),
        "--particle-size",
        str(float(values.get("particle_size_m") or 0.025)),
        "--pre-roll",
        str(float(values.get("pre_roll_s") or 0.0)),
        "--gravity",
        *strings(settings["gravity_m_s2"]),
        "--liquid-position",
        *strings(settings["liquid_position_m"]),
        "--liquid-initial-condition",
        str(settings["liquid_initial_condition_type"]),
        "--liquid-shape",
        str(settings["liquid_shape"]),
        "--liquid-size",
        *strings(settings["liquid_size_m"]),
        "--liquid-radius",
        str(settings["liquid_radius_m"]),
        "--liquid-height",
        str(settings["liquid_height_m"]),
        "--liquid-euler",
        *strings(settings["liquid_euler_deg"]),
        "--initial-velocity-json",
        json.dumps(settings["initial_velocity_field"], separators=(",", ":")),
        "--surface-smoothing-length",
        str(settings["surface_reconstruction"]["smoothing_length_in_particle_radii"]),
        "--surface-cube-size",
        str(settings["surface_reconstruction"]["cube_size_in_particle_radii"]),
        "--surface-iso-threshold",
        str(settings["surface_reconstruction"]["iso_surface_threshold"]),
        "--basin-center",
        *strings(settings["basin_center_xy_m"]),
        "--basin-floor-z",
        str(settings["basin_floor_z_m"]),
        "--basin-half-extent",
        str(settings["basin_half_extent_m"]),
        "--rigid-spheres-json",
        json.dumps(settings["rigid_spheres"], separators=(",", ":")),
        "--minimum-splash-rise",
        str(settings["minimum_splash_rise_m"]),
        "--minimum-float-sink-separation",
        str(settings["minimum_float_sink_separation_m"]),
        "--minimum-initial-flow-speed",
        str(settings["minimum_initial_flow_speed_m_s"]),
        "--minimum-horizontal-displacement",
        str(settings["minimum_horizontal_displacement_m"]),
        "--minimum-jet-rise",
        str(settings["minimum_jet_rise_m"]),
        "--minimum-final-surface-component-fraction",
        str(settings["minimum_final_surface_component_fraction"]),
        "--maximum-final-surface-area-volume-ratio",
        str(settings["maximum_final_surface_area_to_volume_ratio_1_m"]),
        "--maximum-final-surface-volume-relative-error",
        str(settings["maximum_final_surface_volume_relative_error"]),
    ]
    if settings.get("negative_mode"):
        command.extend(("--negative-mode", str(settings["negative_mode"])))
    command.append("--skip-publish")
    return command


def rigid_sphere_parameters(item: dict[str, Any]) -> dict[str, Any]:
    if str(item.get("shape") or "") != "sphere":
        raise ValueError(f"Genesis fluid coupling only supports rigid spheres, got {item.get('shape')}")
    density = item.get("effective_bulk_density_kg_m3", item.get("density_kg_m3"))
    color = item.get("visual_color_rgba", [0.5, 0.5, 0.5, 1.0])
    if not isinstance(color, list) or len(color) != 4:
        raise ValueError("rigid sphere visual_color_rgba must have four components")
    return {
        "id": str(item.get("id") or ""),
        "position_m": vec(item.get("initial_position_m"), [0.0, 0.0, 0.6], "rigid sphere initial_position_m"),
        "radius_m": positive_float(item.get("radius_m"), "rigid sphere radius_m"),
        "density_kg_m3": positive_float(density, "rigid sphere density_kg_m3"),
        "expected_response": str(item.get("expected_response") or ""),
        "visual_color_rgba": [finite_float(value, "rigid sphere visual_color_rgba") for value in color],
    }


def fluid_velocity_field_parameters(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "still"}
    if not isinstance(value, dict):
        raise ValueError("fluid initial velocity field must be an object")
    field_type = str(value.get("type") or "")
    if field_type == "uniform":
        return {
            "type": field_type,
            "velocity_m_s": vec(value.get("velocity_m_s"), [0.0, 0.0, 0.0], "fluid velocity_m_s"),
        }
    if field_type == "swirl_z":
        angular_speed = finite_float(value.get("angular_speed_rad_s"), "fluid angular_speed_rad_s")
        if angular_speed == 0.0:
            raise ValueError("fluid angular_speed_rad_s must be non-zero")
        return {
            "type": field_type,
            "center_m": vec(value.get("center_m"), [0.0, 0.0, 0.0], "fluid swirl center_m"),
            "angular_speed_rad_s": angular_speed,
            "maximum_speed_m_s": positive_float(value.get("maximum_speed_m_s", 1.0), "fluid maximum_speed_m_s"),
        }
    raise ValueError(f"unsupported fluid initial velocity field: {field_type}")


def write_genesis_artifacts(case: RuntimeCase, run_dir: Path) -> dict[str, Any]:
    output_dir = run_dir / "genesis_sph_output"
    output_dir.mkdir(exist_ok=True)
    cache_path = run_dir / "particle_cache.json"
    cache = read_json(cache_path) if cache_path.is_file() else {}
    backend_report_path = run_dir / "genesis_sph_backend_report.json"
    backend_report = read_json(backend_report_path) if backend_report_path.is_file() else {}
    backend_status = str(backend_report.get("status") or "completed")
    video_ready = (run_dir / "video.mp4").is_file() and (run_dir / "video.mp4").stat().st_size > 0
    fluid_object_id = next(
        (
            str(item.get("id"))
            for item in case.data.get("objects") or []
            if isinstance(item, dict) and str(item.get("role") or "") in {"fluid", "fluid_volume"} and item.get("id")
        ),
        "fluid_particles",
    )
    trajectory = particle_center_trajectory(cache, object_id=fluid_object_id)
    contact_events: list[dict[str, Any]] = []
    for directory in (run_dir, output_dir):
        write_json(directory / "trajectory.json", trajectory)
        write_json(directory / "contact_events.json", contact_events)
    summary = {
        **runtime_summary(
            run_dir.name,
            case.case_id,
            case.capability_id,
            "genesis_sph",
            status="completed" if backend_status == "completed" else backend_status,
        ),
        "particle_cache": "../particle_cache.json",
        "frame_count": len(cache.get("frames") or []) if isinstance(cache, dict) else 0,
        "particle_count": int(((cache.get("particles") or {}).get("count") or 0)) if isinstance(cache, dict) else 0,
        "solver": cache.get("solver") if isinstance(cache, dict) else {},
        "runtime_boundary": "Genesis owns SPH particle truth; RGB is a solver-surface preview, not UE sensor output.",
        "trajectory_semantics": "center-of-mass projection of canonical particle cache; particle_cache.json remains truth",
        "contact_event_semantics": "particle-container contacts are not exported; contact_events.json is intentionally empty",
    }
    render_manifest = {
        "schema_version": "harness_render_manifest_v1",
        "backend": "genesis_sph",
        "render_available": video_ready,
        "ue_render_real": False,
        "render_kind": "solver_surface_preview",
        "passes": [{"name": "rgb_preview", "path": "video.mp4", "status": "available"}] if video_ready else [],
    }
    pass_manifest = {
        "schema_version": "render_pass_manifest_v1",
        "passes": {
            "rgb": {"status": "preview" if video_ready else "missing", "source_type": "genesis_surface_preview"},
            "depth": {"status": "missing", "source_type": "not_exported"},
            "segmentation": {"status": "missing", "source_type": "not_exported"},
        },
        "sync": {"particle_cache": "particle_cache.json"},
    }
    provisional_readiness = {
        "schema_version": "harness_run_readiness_v1",
        "backend": "genesis_sph",
        "case_id": case.case_id,
        "reference_ready": False,
        "physics_ready": False,
        "visual_ready": False,
        "solver_preview_ready": video_ready,
        "local_preview_ready": False,
        "ue_render_real": False,
        "publication_tier": "rejected",
        "trajectory_ready": bool(trajectory),
        "contact_events_ready": False,
    }
    for directory in (run_dir, output_dir):
        write_json(directory / "render_manifest.json", render_manifest)
        write_json(directory / "render_pass_manifest.json", pass_manifest)
        write_json(directory / "run_readiness.json", provisional_readiness)
    write_json(output_dir / "summary.json", summary)
    verifier = PhysicsVerifier().verify_run_dir(run_dir, write=True)
    physics_ready = verifier["status"] == "pass"
    if not physics_ready and summary["status"] == "completed":
        summary["status"] = "failed_verification"
        write_json(output_dir / "summary.json", summary)
    readiness = {
        **provisional_readiness,
        "physics_ready": physics_ready,
        "local_preview_ready": False,
        "publication_tier": "rejected",
        "verifier_status": verifier["status"],
    }
    for directory in (run_dir, output_dir):
        write_json(directory / "run_readiness.json", readiness)
    artifacts = {
        "case_spec": "case_spec.json",
        "particle_cache": "particle_cache.json",
        "trajectory": "trajectory.json",
        "contact_events": "contact_events.json",
        "surface_frames": "surface_frames/",
        "video": "video.mp4",
        "summary": "genesis_sph_output/summary.json",
        "run_readiness": "run_readiness.json",
        "render_manifest": "render_manifest.json",
        "render_pass_manifest": "render_pass_manifest.json",
        "verifier": "harness_verifier.json",
        "backend_report": "genesis_sph_backend_report.json",
    }
    if (run_dir / "rigid_sph_scene.json").is_file():
        artifacts["rigid_sph_scene"] = "rigid_sph_scene.json"
    write_json(
        run_dir / "harness_artifact.json",
        {
            "schema_version": "harness_runtime_artifact_package_v1",
            "run_id": run_dir.name,
            "case_id": case.case_id,
            "capability_id": case.capability_id,
            "backend": "genesis_sph",
            "runtime_boundary": summary["runtime_boundary"],
            "paths": artifacts,
        },
    )
    write_json(
        run_dir / "artifact_manifest.json",
        {
            "schema_version": "harness_artifact_manifest_v1",
            "run_id": run_dir.name,
            "case_id": case.case_id,
            "backend": "genesis_sph",
            "artifacts": artifacts,
        },
    )
    return verifier


def particle_center_trajectory(cache: dict[str, Any], *, object_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for frame in cache.get("frames") or []:
        if not isinstance(frame, dict):
            continue
        positions = frame.get("positions_m") if isinstance(frame.get("positions_m"), list) else []
        velocities = frame.get("velocities_m_s") if isinstance(frame.get("velocities_m_s"), list) else []
        if not positions:
            continue
        try:
            center = mean_vec3(positions)
            mean_velocity = mean_vec3(velocities) if velocities else [0.0, 0.0, 0.0]
        except (IndexError, TypeError, ValueError):
            continue
        rows.append(
            {
                "frame": int(frame.get("frame") or 0),
                "time_s": float(frame.get("time_s") or 0.0),
                "objects": {
                    object_id: {
                        "position_m": center,
                        "velocity_m_s": mean_velocity,
                        "particle_count": len(positions),
                        "state_source": "particle_cache_center_of_mass_projection",
                    }
                },
                "contacts": [],
            }
        )
    return rows


def mean_vec3(rows: list[Any]) -> list[float]:
    return [sum(float(row[axis]) for row in rows) / len(rows) for axis in range(3)]


def vec(value: Any, default: list[float], name: str, *, scalar: bool = False) -> list[float]:
    if value is None:
        values = default
    elif scalar and isinstance(value, (int, float)):
        values = [value, value, value]
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        values = list(value)
    else:
        raise ValueError(f"{name} must be a finite 3-vector" + (" or scalar" if scalar else ""))
    return [finite_float(item, name) for item in values]


def finite_float(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def positive_float(value: Any, name: str) -> float:
    number = finite_float(value, name)
    if number <= 0.0:
        raise ValueError(f"{name} must be positive")
    return number


def strings(values: list[float]) -> list[str]:
    return [str(float(value)) for value in values]
