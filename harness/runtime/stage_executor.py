from __future__ import annotations

import inspect
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from harness.core.artifact_schema import read_json, write_json
from harness.core.runtime_case import RuntimeCase
from harness.core.stage_result import stage_result_from_execution_report, write_stage_result
from harness.core.workspace import workspace_root
from harness.planning.runtime_compiler import RuntimeCompilation
from harness.runtime.deformable_surface_adapter import prepare_ue_deformable_replay
from harness.runtime.fallback_backend import FallbackBackend
from harness.runtime.genesis_fem_backend import GenesisFEMBackend
from harness.runtime.genesis_sph_backend import DEFAULT_UE_MAP, GenesisSPHBackend, run_ue_surface_replay
from harness.runtime.taichi_cloth_backend import TaichiClothBackend
from harness.runtime.ue_backend import UEBackend


ROOT = Path(__file__).resolve().parents[2]
BackendFactory = Callable[[], Any]
RenderAdapter = Callable[..., dict[str, Any]]

BACKEND_FACTORIES: dict[str, BackendFactory] = {
    "fallback": FallbackBackend,
    "genesis_fem": GenesisFEMBackend,
    "genesis_sph": GenesisSPHBackend,
    "taichi_cloth": TaichiClothBackend,
    "ue": UEBackend,
}


class StageExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def execute_runtime_plan(
    case: RuntimeCase,
    output_root: str | Path,
    *,
    compilation: RuntimeCompilation,
    requested_views: list[str] | None,
    render_passes: list[str] | None,
    camera_strategy: str,
    profile: str,
    width: int,
    height: int,
    complete_sensor_contract: bool,
    backend_factories: Mapping[str, BackendFactory] | None = None,
    render_adapters: Mapping[str, RenderAdapter] | None = None,
    physics_handoff_root: str | Path | None = None,
) -> Path:
    """Execute the compiled stage DAG in order using artifact contracts.

    The executor never dispatches on a named physical process. Solver stages
    invoke registered backends; render stages consume the handoff contract
    emitted by Runtime Compiler.
    """
    completed: list[dict[str, Any]] = []
    run_dir: Path | None = None
    started = time.perf_counter()
    try:
        plan = compilation.artifacts["runtime_plan"]
        stages = [dict(stage) for stage in plan.get("stages") or [] if isinstance(stage, Mapping)]
        if not stages:
            raise StageExecutionError("runtime_plan_empty", "runtime_plan contains no executable stages")
        factories = dict(backend_factories or BACKEND_FACTORIES)
        adapters = dict(render_adapters or {"ue": render_ue_handoff})
        particle_handoff = _particle_surface_handoff(stages)
        handoff_root = Path(physics_handoff_root) if physics_handoff_root is not None and particle_handoff else None
        reuse_handoff = bool(handoff_root is not None and (handoff_root / "manifest.json").is_file())
        for stage in stages:
            kind = str(stage.get("kind") or "")
            backend_name = str(stage.get("backend") or "")
            if kind in {"solve", "solve_render"}:
                if kind == "solve" and reuse_handoff:
                    run_dir = Path(output_root) / f"{case.case_id}_{compilation.selected_backend}"
                    run_dir.mkdir(parents=True, exist_ok=True)
                    _materialize_particle_handoff(handoff_root, run_dir, case, compilation, particle_handoff)
                    completed.append({"id": stage.get("id"), "kind": kind, "backend": backend_name, "mode": "handoff_reuse"})
                    continue
                factory = factories.get(backend_name)
                if factory is None:
                    raise StageExecutionError(
                        "stage_backend_unregistered",
                        f"runtime stage backend is not registered: {backend_name}",
                    )
                run_dir = _run_backend(
                    factory(),
                    case,
                    output_root,
                    requested_views=requested_views,
                    render_passes=render_passes,
                    camera_strategy=camera_strategy,
                    compilation=compilation,
                    complete_sensor_contract=complete_sensor_contract,
                )
                if kind == "solve" and handoff_root is not None:
                    _validate_handoff_artifacts(run_dir, particle_handoff)
                    _commit_particle_handoff(handoff_root, run_dir, case, compilation, particle_handoff)
            elif kind == "render":
                if run_dir is None:
                    raise StageExecutionError("render_before_solve", "render stage has no completed solver stage")
                adapter = adapters.get(backend_name)
                handoff = stage.get("handoff_contract")
                if adapter is None:
                    raise StageExecutionError(
                        "render_backend_unregistered",
                        f"runtime render backend is not registered: {backend_name}",
                    )
                if not isinstance(handoff, Mapping):
                    raise StageExecutionError(
                        "stage_handoff_missing",
                        "multi-backend render stage has no compatible handoff contract",
                    )
                _validate_handoff_artifacts(run_dir, handoff)
                adapter(
                    run_dir,
                    handoff_contract=dict(handoff),
                    profile=profile,
                    width=width,
                    height=height,
                )
            else:
                raise StageExecutionError("stage_kind_unsupported", f"unsupported runtime stage kind: {kind}")
            completed.append({"id": stage.get("id"), "kind": kind, "backend": backend_name})
    except BaseException as exc:
        selected_backend = str(getattr(compilation, "selected_backend", "unknown"))
        destination = run_dir or Path(output_root) / f"{case.case_id}_{selected_backend}"
        destination.mkdir(parents=True, exist_ok=True)
        interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
        report = {
            "schema_version": "harness_stage_execution_report_v1",
            "status": "interrupted" if interrupted else "failed",
            "completed_stages": completed,
            "failure_code": "interrupted" if interrupted else getattr(exc, "code", "stage_execution_exception"),
            "failure_message": str(exc) or type(exc).__name__,
        }
        write_json(destination / "stage_execution_report.json", report)
        write_stage_result(
            destination,
            stage_result_from_execution_report(report, elapsed_seconds=time.perf_counter() - started),
        )
        raise
    assert run_dir is not None
    report = {
        "schema_version": "harness_stage_execution_report_v1",
        "status": "completed",
        "completed_stages": completed,
        "failure_code": None,
        "failure_message": None,
    }
    write_json(run_dir / "stage_execution_report.json", report)
    write_stage_result(
        run_dir,
        stage_result_from_execution_report(report, elapsed_seconds=time.perf_counter() - started),
    )
    return run_dir


def _run_backend(backend: Any, case: RuntimeCase, output_root: str | Path, **kwargs: Any) -> Path:
    signature = inspect.signature(backend.run_case)
    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
    accepted = kwargs if accepts_kwargs else {key: value for key, value in kwargs.items() if key in signature.parameters}
    return Path(backend.run_case(case, output_root, **accepted))


def _validate_handoff_artifacts(run_dir: Path, handoff: Mapping[str, Any]) -> None:
    missing = [str(path) for path in handoff.get("required_artifacts") or [] if not (run_dir / str(path)).is_file()]
    if missing:
        raise StageExecutionError(
            "stage_handoff_incomplete",
            f"handoff {handoff.get('contract_id')} is missing required artifacts: {missing}",
        )
    manifest_name = next((str(path) for path in handoff.get("required_artifacts") or [] if str(path).endswith(".json")), "")
    if manifest_name:
        payload = read_json(run_dir / manifest_name)
        expected = str(handoff.get("schema_version") or "")
        if payload.get("schema_version") != expected:
            raise StageExecutionError(
                "stage_handoff_schema_mismatch",
                f"{manifest_name} schema is {payload.get('schema_version')!r}, expected {expected!r}",
            )


def _particle_surface_handoff(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for stage in stages:
        contract = stage.get("handoff_contract")
        if isinstance(contract, Mapping) and contract.get("contract_id") == "particle_surface_cache_v1":
            return dict(contract)
    return None


def _particle_handoff_identity(
    case: RuntimeCase,
    compilation: RuntimeCompilation,
    handoff: Mapping[str, Any],
) -> str:
    payload = {
        "case_spec": case.data,
        "solver_configuration": compilation.artifacts.get("solver_configuration"),
        "handoff_contract": dict(handoff),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _particle_handoff_files(run_dir: Path) -> list[str]:
    cache = read_json(run_dir / "particle_cache.json")
    paths = {"particle_cache.json", "solver_configuration.json"}
    if (run_dir / "rigid_sph_scene.json").is_file():
        paths.add("rigid_sph_scene.json")
    for frame in cache.get("frames") or []:
        surface = frame.get("surface") if isinstance(frame, Mapping) else None
        relative = str((surface or {}).get("path") or "") if isinstance(surface, Mapping) else ""
        if not relative:
            raise StageExecutionError("stage_handoff_incomplete", "particle cache frame has no surface artifact")
        source = (run_dir / relative).resolve()
        if not source.is_relative_to(run_dir.resolve()) or not source.is_file():
            raise StageExecutionError("stage_handoff_incomplete", f"particle surface artifact is invalid: {relative}")
        paths.add(relative)
    return sorted(paths)


def _commit_particle_handoff(
    handoff_root: Path,
    run_dir: Path,
    case: RuntimeCase,
    compilation: RuntimeCompilation,
    handoff: Mapping[str, Any],
) -> None:
    manifest_path = handoff_root / "manifest.json"
    if manifest_path.is_file() or (handoff_root.exists() and any(handoff_root.iterdir())):
        raise StageExecutionError("physics_handoff_already_exists", "attempt physics handoff is immutable")
    files = []
    for relative in _particle_handoff_files(run_dir):
        source = run_dir / relative
        destination = handoff_root / "files" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        files.append({"path": relative, "sha256": _file_sha256(destination), "byte_size": destination.stat().st_size})
    write_json(
        manifest_path,
        {
            "schema_version": "harness_particle_surface_handoff_manifest_v1",
            "contract_id": "particle_surface_cache_v1",
            "physics_identity": _particle_handoff_identity(case, compilation, handoff),
            "files": files,
        },
    )


def _materialize_particle_handoff(
    handoff_root: Path,
    run_dir: Path,
    case: RuntimeCase,
    compilation: RuntimeCompilation,
    handoff: Mapping[str, Any],
) -> None:
    manifest = read_json(handoff_root / "manifest.json")
    if (
        manifest.get("schema_version") != "harness_particle_surface_handoff_manifest_v1"
        or manifest.get("contract_id") != "particle_surface_cache_v1"
        or manifest.get("physics_identity") != _particle_handoff_identity(case, compilation, handoff)
    ):
        raise StageExecutionError("physics_handoff_identity_mismatch", "attempt physics handoff does not match compilation")
    for row in manifest.get("files") or []:
        relative = str(row.get("path") or "")
        source = (handoff_root / "files" / relative).resolve()
        if not source.is_relative_to((handoff_root / "files").resolve()) or not source.is_file():
            raise StageExecutionError("physics_handoff_incomplete", f"physics handoff file is missing: {relative}")
        if source.stat().st_size != int(row.get("byte_size") or -1) or _file_sha256(source) != row.get("sha256"):
            raise StageExecutionError("physics_handoff_mutated", f"physics handoff file changed: {relative}")
        destination = run_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    write_json(run_dir / "physics_handoff.json", manifest)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_ue_handoff(
    run_dir: str | Path,
    *,
    handoff_contract: Mapping[str, Any],
    profile: str,
    width: int,
    height: int,
) -> dict[str, Any]:
    adapters: dict[str, RenderAdapter] = {
        "particle_surface_cache_v1": _render_particle_surface_cache_in_ue,
        "deformable_mesh_cache_v1": _render_deformable_mesh_cache_in_ue,
    }
    contract_id = str(handoff_contract.get("contract_id") or "")
    adapter = adapters.get(contract_id)
    if adapter is None:
        raise StageExecutionError("ue_handoff_unsupported", f"UE cannot consume handoff contract: {contract_id}")
    return adapter(
        Path(run_dir),
        handoff_contract=handoff_contract,
        profile=profile,
        width=width,
        height=height,
    )


def _render_particle_surface_cache_in_ue(
    run_dir: Path,
    *,
    handoff_contract: Mapping[str, Any],
    profile: str,
    width: int,
    height: int,
) -> dict[str, Any]:
    return run_ue_surface_replay(
        run_dir,
        handoff_contract=handoff_contract,
        profile=profile,
        width=width,
        height=height,
    )


def _render_deformable_mesh_cache_in_ue(
    run_dir: Path,
    *,
    handoff_contract: Mapping[str, Any],
    profile: str,
    width: int,
    height: int,
) -> dict[str, Any]:
    cache_manifest = run_dir / "deformable_cache.json"
    case_spec = run_dir / "case_spec.json"
    replay_root = run_dir / "ue_replay_input"
    replay = prepare_ue_deformable_replay(
        cache_manifest,
        replay_root,
        ue_asset_root=f"/Game/HarnessGenerated/Deformable/{_cache_identity(cache_manifest)[:16]}",
    )
    replay_manifest = replay_root / "deformable_surface_replay.json"
    ue_project, ue_executable = _ue_paths()
    command = [
        sys.executable,
        str(ROOT / "scripts" / "harness_render_deformable_ue.py"),
        str(replay_manifest),
        "--cache-manifest",
        str(cache_manifest),
        "--case",
        str(case_spec),
        "--run-dir",
        str(run_dir),
        "--ue-project",
        str(ue_project),
        "--ue-executable",
        str(ue_executable),
        "--map",
        os.environ.get("SIM_STUDIO_UE_MAP") or DEFAULT_UE_MAP,
        "--profile",
        profile,
    ]
    command.extend(("--width", str(width), "--height", str(height)))
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    report = {
        "schema_version": "harness_staged_render_report_v1",
        "handoff_contract": "deformable_mesh_cache_v1",
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
        raise StageExecutionError(
            "staged_render_failed",
            f"UE render adapter failed with exit code {result.returncode}; see {run_dir / 'staged_render_report.json'}",
        )
    return report


def _ue_paths() -> tuple[Path, Path]:
    project_value = os.environ.get("SIM_STUDIO_UE_PROJECT", "").strip()
    project = Path(project_value).expanduser() if project_value else workspace_root() / "ue" / "SimulatorWorkspace.uproject"
    executable_value = os.environ.get("SIM_STUDIO_UE_EXECUTABLE", "").strip()
    executable = Path(executable_value).expanduser() if executable_value else Path()
    if not project.is_file():
        raise StageExecutionError("ue_project_missing", f"UE project does not exist: {project}")
    if not executable_value or not executable.is_file():
        raise StageExecutionError("ue_executable_missing", "SIM_STUDIO_UE_EXECUTABLE must name an existing executable")
    return project, executable


def _cache_identity(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
