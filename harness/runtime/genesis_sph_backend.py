from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from harness.core.artifact_schema import read_json, runtime_summary, write_json
from harness.core.runtime_case import RuntimeCase
from harness.core.physics_contract import infer_scene_domain
from harness.core.workspace import workspace_root
from harness.runtime.rigid_sph_scene import compile_rigid_sph_scene
from harness.runtime.rigid_sph_configuration import compile_rigid_sph_solver_configuration
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
        compilation: Any | None = None,
    ) -> Path:
        if infer_scene_domain(case.data) != "particle":
            raise ValueError("genesis_sph requires a particle-domain scene contract")
        run_dir = Path(output_root) / f"{case.case_id}_{self.name}"
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(run_dir / "case_spec.json", case.data)
        solver_configuration = (
            compilation.artifacts.get("solver_configuration")
            if compilation is not None and isinstance(getattr(compilation, "artifacts", None), dict)
            else None
        )
        if not isinstance(solver_configuration, dict):
            solver_configuration = compile_rigid_sph_solver_configuration(case.data)
        write_json(run_dir / "solver_configuration.json", solver_configuration)
        parameters = genesis_parameters(case.data, solver_configuration)
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
        command = genesis_command(executable, run_dir, parameters)
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
    handoff_contract: Mapping[str, Any],
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
    tolerances = handoff_contract.get("numeric_tolerances") if isinstance(handoff_contract.get("numeric_tolerances"), Mapping) else {}
    if "spatial_measurement_absolute" not in tolerances:
        raise RuntimeError("particle surface handoff contract is missing spatial_measurement_absolute tolerance")
    replay = prepare_ue_surface_replay(
        particle_cache,
        replay_input,
        ue_asset_root=f"/Game/HarnessGenerated/Fluid/Cache_{cache_digest[:16]}",
        spatial_measurement_tolerance=float(tolerances["spatial_measurement_absolute"]),
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
    fluid_render_report_path = run_dir / "fluid_ue_render_report.json"
    fluid_render_report = read_json(fluid_render_report_path) if fluid_render_report_path.is_file() else {}
    verification_failed = result.returncode == 2 and fluid_render_report.get("status") == "failed_verification"
    report = {
        "schema_version": "harness_staged_render_report_v1",
        "solver_backend": "genesis_sph",
        "render_backend": "ue",
        "status": (
            "failed_verification"
            if verification_failed
            else ("completed" if result.returncode == 0 else "failed")
        ),
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "surface_replay": str(replay_manifest.relative_to(run_dir)),
        "frame_count": int((replay.get("timebase") or {}).get("frame_count") or 0),
    }
    write_json(run_dir / "staged_render_report.json", report)
    if result.returncode != 0 and not verification_failed:
        raise RuntimeError(
            f"Genesis-to-UE replay failed with exit code {result.returncode}; see {run_dir / 'staged_render_report.json'}"
        )
    return report


def genesis_python() -> Path:
    configured = os.environ.get("SIM_GENESIS_PYTHON")
    if configured:
        return Path(configured).expanduser()
    return workspace_root() / "envs" / "genesis" / "bin" / "python"


def genesis_parameters(
    case_spec: dict[str, Any],
    solver_configuration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return compile_rigid_sph_scene(case_spec, solver_configuration)


def genesis_command(executable: Path, run_dir: Path, parameters: Mapping[str, Any]) -> list[str]:
    if parameters.get("execution_contract") != "rigid_sph_scene":
        raise ValueError("genesis_sph requires the compiled rigid_sph_scene execution contract")
    return [
        str(executable),
        str(ROOT / "scripts" / "harness_genesis_rigid_sph.py"),
        "--case",
        str(run_dir / "case_spec.json"),
        "--output-dir",
        str(run_dir),
        "--solver-configuration",
        str(run_dir / "solver_configuration.json"),
        "--skip-publish",
    ]


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
