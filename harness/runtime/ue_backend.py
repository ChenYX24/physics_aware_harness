from __future__ import annotations

import os
import signal
import shlex
import subprocess
from pathlib import Path
from typing import Any

from harness.core.case_spec import CaseSpec
from harness.core.artifact_schema import read_json, write_json
from harness.core.artifact_manager import ArtifactManager
from harness.assets.asset_resolver import resolve_asset_intents
from harness.planning.static_scene_builder import build_static_scene_layout
from harness.runtime.actor_placement import compile_runtime_actor_placement
from harness.runtime.camera_planner import camera_plan_from_case_spec
from harness.runtime.render_pass_contract import enforce_ue_render_passes, normalize_passes, verify_render_observability, write_render_contract_artifacts
from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement
from harness.verification.static_scene_verifier import verify_static_scene_layout
from harness.verification.render_sync_checker import ARTIFACT_SCHEMA_VERSION, check_render_sync


DEFAULT_UE_VIEWS = ["front_static", "side_static", "top_down", "tracking_subject", "event_closeup"]


class UEBackendUnavailable(RuntimeError):
    def __init__(self, message: str, run_dir: Path, failure_type: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.run_dir = run_dir
        self.failure_type = failure_type
        self.report = report


class UEBackend:
    name = "ue"

    def run_case(
        self,
        case: CaseSpec,
        output_root: str | Path,
        *,
        requested_views: list[str] | None = None,
        render_passes: list[str] | None = None,
        camera_strategy: str = "bounds_auto_v1",
        complete_sensor_contract: bool = True,
    ) -> Path:
        run_id = f"{case.case_id}_ue"
        run_dir = Path(output_root) / run_id
        output_dir = run_dir / "ue_output"
        output_dir.mkdir(parents=True, exist_ok=True)
        scene_spec = compile_minimal_scene_spec(case.data)
        write_json(run_dir / "case_spec.json", case.data)
        write_json(run_dir / "scene_spec.json", scene_spec)
        ue_requested_views = requested_views or DEFAULT_UE_VIEWS
        ue_render_passes = (
            enforce_ue_render_passes(render_passes)
            if complete_sensor_contract
            else normalize_passes(render_passes)
        )
        camera_plan = camera_plan_from_case_spec(case.data, requested_views=ue_requested_views, camera_strategy=camera_strategy)
        write_json(run_dir / "camera_plan.json", camera_plan_to_json(camera_plan))
        actor_contract = prepare_runtime_actor_contract(
            case,
            run_dir,
            requested_views=ue_requested_views,
            camera_strategy=camera_strategy,
        )
        if actor_contract["status"] != "pass":
            report = build_backend_report(
                case,
                run_id,
                empty_preflight(case.case_id),
                phase="actor_placement",
                real_ue_invoked=False,
                failure_code=str(actor_contract["failure_type"]),
                failure_message=str(actor_contract["failure_message"]),
                failure_category="preflight_failure",
            )
            write_json(run_dir / "ue_preflight_report.json", empty_preflight(case.case_id))
            write_failed_ue_artifacts(run_dir, output_dir, case, run_id, report, camera_plan=camera_plan, render_passes=ue_render_passes, requested_view_count=len(ue_requested_views))
            raise UEBackendUnavailable(report["failure_message"], run_dir, str(report["failure_code"]), report)
        preflight = build_ue_preflight_report(case.case_id, case.data)
        write_json(run_dir / "ue_preflight_report.json", preflight)
        if preflight["failure_code"]:
            report = build_backend_report(case, run_id, preflight, phase="preflight", real_ue_invoked=False)
            write_failed_ue_artifacts(run_dir, output_dir, case, run_id, report, camera_plan=camera_plan, render_passes=ue_render_passes, requested_view_count=len(ue_requested_views))
            raise UEBackendUnavailable(report["failure_message"], run_dir, str(report["failure_code"]), report)

        report = invoke_real_ue_runner(case, run_dir, output_dir, scene_spec, preflight, requested_views=ue_requested_views, render_passes=ue_render_passes)
        write_json(run_dir / "ue_backend_report.json", report)
        if report.get("status") != "completed":
            write_failed_ue_artifacts(run_dir, output_dir, case, run_id, report, camera_plan=camera_plan, render_passes=ue_render_passes, requested_view_count=len(ue_requested_views))
            raise UEBackendUnavailable(str(report["failure_message"]), run_dir, str(report["failure_code"]), report)

        standardize_runner_outputs(run_dir, output_dir, case, run_id, report, camera_plan=camera_plan, render_passes=ue_render_passes, requested_view_count=len(ue_requested_views))
        return run_dir


def write_failed_ue_artifacts(
    run_dir: Path,
    output_dir: Path,
    case: CaseSpec,
    run_id: str,
    report: dict[str, Any],
    camera_plan: Any,
    render_passes: list[str],
    requested_view_count: int,
) -> None:
    failure_type = str(report["failure_code"])
    reason = str(report["failure_message"])
    preserve_runtime = preserve_completed_runtime(run_dir, report)
    if not preserve_runtime:
        write_json(output_dir / "trajectory.json", [])
        write_json(output_dir / "contact_events.json", [])
        write_json(run_dir / "trajectory.json", [])
        write_json(run_dir / "contact_events.json", [])
        write_json(
            run_dir / "camera_trajectory.json",
            {
                "schema_version": "harness_camera_trajectory_v1",
                "available": False,
                "backend": "ue",
                "reason": reason,
                "frames": [],
            },
        )
    write_json(
        output_dir / "summary.json",
        {
            "schema_version": "harness_runtime_artifact_v1",
            "run_id": run_id,
            "case_id": case.case_id,
            "capability_id": case.capability_id,
            "backend": "ue",
            "status": "failed",
            "failure_type": failure_type,
            "failure_category": report.get("failure_category"),
            "reason": reason,
            "ue_backend_report": "ue_backend_report.json",
        },
    )
    write_json(
        output_dir / "run_readiness.json",
        {
            "schema_version": "harness_run_readiness_v1",
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "reference_ready": False,
            "physics_ready": False,
            "visual_ready": False,
            "diagnostic_runtime_preserved": preserve_runtime,
            "backend": "ue",
            "case_id": case.case_id,
            "failure_type": failure_type,
            "failure_category": report.get("failure_category"),
            "reason": reason,
            "ue_render_real": False,
            "depth_source": "missing",
            "multi_view_sync_ok": False,
            "render_pass_valid": False,
            "render_observability_fail": 1,
        },
    )
    write_json(
        run_dir / "run_readiness.json",
        {
            "schema_version": "harness_run_readiness_v1",
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "reference_ready": False,
            "physics_ready": False,
            "visual_ready": False,
            "diagnostic_runtime_preserved": preserve_runtime,
            "backend": "ue",
            "case_id": case.case_id,
            "failure_type": failure_type,
            "failure_category": report.get("failure_category"),
            "reason": reason,
            "ue_render_real": False,
            "depth_source": "missing",
            "multi_view_sync_ok": False,
            "render_pass_valid": False,
            "render_observability_fail": 1,
        },
    )
    write_json(
        output_dir / "render_manifest.json",
        {
            "schema_version": "harness_render_manifest_v1",
            "backend": "ue",
            "render_available": False,
            "reason": reason,
            "video_missing_expected": report.get("failure_category") == "preflight_failure",
            "passes": [],
        },
    )
    if not preserve_runtime:
        write_json(
            run_dir / "render_manifest.json",
            {
                "schema_version": "harness_render_manifest_v1",
                "backend": "ue",
                "render_available": False,
                "reason": reason,
                "video_missing_expected": report.get("failure_category") == "preflight_failure",
                "passes": [],
            },
        )
    write_json(
        output_dir / "render_pass_manifest.json",
        {
            "schema_version": "render_pass_manifest_v1",
            "passes": {},
            "sync": {},
            "status": "missing",
            "reason": reason,
        },
    )
    write_json(
        run_dir / "render_pass_manifest.json",
        write_render_contract_artifacts(run_dir, backend="ue", case_id=case.case_id, camera_plan=camera_plan, render_passes=render_passes, allow_placeholders=False, source="ue_preflight_failure"),
    )
    render_sync = check_render_sync(run_dir, require_depth="depth" in set(render_passes), require_segmentation="segmentation" in set(render_passes), write=True)
    observability = verify_render_observability(run_dir, require_multiview=requested_view_count > 1, require_depth="depth" in set(render_passes), min_view_count=requested_view_count)
    write_json(
        run_dir / "harness_artifact.json",
        {
            "schema_version": "harness_runtime_artifact_package_v1",
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "run_id": run_id,
            "case_id": case.case_id,
            "capability_id": case.capability_id,
            "backend": "ue",
            "status": "failed",
            "failure_category": report.get("failure_category"),
            "ue_backend_report": "ue_backend_report.json",
            "camera_plan_ready": observability["camera_plan_ready"],
            "multi_view_ready": observability["multi_view_ready"],
            "depth_ready": observability["depth_ready"],
            "render_pass_ready": observability["render_pass_ready"],
            "sync_ready": observability["sync_ready"],
            "ue_render_real": False,
            "depth_source": "missing",
            "multi_view_sync_ok": False,
            "render_pass_valid": False,
            "render_observability_fail": 1,
            "view_count": observability["view_count"],
            "camera_ids": observability["camera_ids"],
            "paths": {
                "case_spec": "case_spec.json",
                "scene_spec": "scene_spec.json",
                "asset_resolution": "asset_resolution.json",
                "scene_layout": "scene_layout.json",
                "static_scene_report": "static_scene_report.json",
                "runtime_actor_placement": "runtime_actor_placement.json",
                "runtime_actor_placement_report": "runtime_actor_placement_report.json",
                "trajectory": "trajectory.json",
                "contact_events": "contact_events.json",
                "camera_trajectory": "camera_trajectory.json",
                "camera_plan": "camera_plan.json",
                "summary": "ue_output/summary.json",
                "run_readiness": "ue_output/run_readiness.json",
                "render_manifest": "render_manifest.json",
                "render_pass_manifest": "render_pass_manifest.json",
                "render_sync_report": "render_sync_report.json",
            },
        },
    )
    verifier = {
        "schema_version": "harness_verifier_report_v1",
        "case_id": case.case_id,
        "capability_id": case.capability_id,
        "status": "fail",
        "failure_type": failure_type,
        "failure_category": report.get("failure_category"),
        "first_failure": {"object_id": "ue_backend", "frame": 0, "time": 0.0, "metric": report.get("phase", "ue_failure"), "value": reason},
        "evidence": [],
        "repair_suggestions": [
            str(report.get("next_required_action") or "Fix UE backend configuration and rerun."),
            "Ensure UE exports trajectory.json, contact_events.json, camera_trajectory.json, render_manifest.json, and video.mp4 before enabling reference-ready evaluation.",
        ],
        "artifact_completeness": {
            "scene_spec": True,
            "trajectory": False,
            "contact_events": False,
            "render_manifest": True,
            "render_sync_report": True,
            "render_sync_status": render_sync["status"],
            **observability,
        },
    }
    write_json(run_dir / "harness_verifier.json", verifier)
    write_json(run_dir / "verifier_report.json", verifier)
    write_json(
        run_dir / "artifact_manifest.json",
        {
            "schema_version": "harness_artifact_manifest_v1",
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "run_id": run_id,
            "case_id": case.case_id,
            "backend": "ue",
            "status": "failed",
            "failure_category": report.get("failure_category"),
            "artifacts": {
                "case_spec": "case_spec.json",
                "scene_spec": "scene_spec.json",
                "asset_resolution": "asset_resolution.json",
                "scene_layout": "scene_layout.json",
                "static_scene_report": "static_scene_report.json",
                "runtime_actor_placement": "runtime_actor_placement.json",
                "runtime_actor_placement_report": "runtime_actor_placement_report.json",
                "harness_artifact": "harness_artifact.json",
                "harness_verifier": "harness_verifier.json",
                "trajectory": "trajectory.json",
                "contact_events": "contact_events.json",
                "camera_trajectory": "camera_trajectory.json",
                "camera_plan": "camera_plan.json",
                "render_manifest": "render_manifest.json",
                "render_pass_manifest": "render_pass_manifest.json",
                "render_sync_report": "render_sync_report.json",
                "ue_preflight_report": "ue_preflight_report.json",
                "ue_backend_report": "ue_backend_report.json",
            },
        },
    )
    write_json(run_dir / "ue_backend_report.json", report)


def preserve_completed_runtime(run_dir: Path, report: dict[str, Any]) -> bool:
    return bool(
        report.get("whether_real_ue_invoked")
        and (run_dir / "video.mp4").is_file()
        and any((run_dir / "views").glob("*/rgb.mp4"))
    )


def empty_preflight(case_id: str) -> dict[str, Any]:
    return {
        "schema_version": "harness_ue_preflight_report_v1",
        "backend_mode": "ue",
        "requested_case_id": case_id,
        "env_presence": {},
        "resolved_paths": {},
        "path_exists": {},
        "path_properties": {},
        "raw_env": {},
        "failure_code": None,
        "failure_message": None,
        "next_required_action": "Fix runtime actor placement before UE preflight.",
        "whether_real_ue_invoked": False,
    }


def prepare_runtime_actor_contract(
    case: CaseSpec,
    run_dir: Path,
    *,
    requested_views: list[str],
    camera_strategy: str,
) -> dict[str, Any]:
    asset_resolution = resolve_asset_intents(case.data)
    scene_layout = build_static_scene_layout(
        case.data,
        asset_resolution=asset_resolution,
        requested_views=requested_views,
        camera_strategy=camera_strategy,
    )
    static_report = verify_static_scene_layout(case.data, scene_layout)
    runtime_actor_placement = compile_runtime_actor_placement(
        case.data,
        scene_layout,
        asset_resolution=asset_resolution,
        target_backend="UE",
    )
    runtime_actor_placement_report = verify_runtime_actor_placement(case.data, runtime_actor_placement)
    write_json(run_dir / "asset_resolution.json", asset_resolution)
    write_json(run_dir / "scene_layout.json", scene_layout)
    write_json(run_dir / "static_scene_report.json", static_report)
    write_json(run_dir / "runtime_actor_placement.json", runtime_actor_placement)
    write_json(run_dir / "runtime_actor_placement_report.json", runtime_actor_placement_report)
    if static_report.get("status") != "pass":
        return {
            "status": "fail",
            "failure_type": static_report.get("failure_type") or "F3_invalid_initial_physics_state",
            "failure_message": f"Static scene placement failed: {static_report.get('failure_type')}",
        }
    if runtime_actor_placement_report.get("status") != "pass":
        return {
            "status": "fail",
            "failure_type": runtime_actor_placement_report.get("failure_type") or "F7_runtime_artifact_incomplete",
            "failure_message": f"Runtime actor placement failed: {runtime_actor_placement_report.get('failure_type')}",
        }
    return {"status": "pass", "failure_type": None, "failure_message": None}


def build_ue_preflight_report(case_id: str, case_spec: dict[str, Any] | None = None) -> dict[str, Any]:
    explicit_project = os.environ.get("SIM_STUDIO_UE_PROJECT", "").strip()
    workspace_project = initialized_workspace_project() if not explicit_project else ""
    env = {
        "SIM_STUDIO_UE_PROJECT": explicit_project or workspace_project,
        "SIM_STUDIO_UE_EXECUTABLE": os.environ.get("SIM_STUDIO_UE_EXECUTABLE", ""),
        "SIM_STUDIO_UE_MAP": requested_map_package(case_spec),
        "SIM_STUDIO_UE_ACTOR_CLASS": os.environ.get("SIM_STUDIO_UE_ACTOR_CLASS", ""),
        "SIM_STUDIO_ASSET_REGISTRY": os.environ.get("SIM_STUDIO_ASSET_REGISTRY", ""),
        "SIM_STUDIO_UE_CONTACT_EXPORT": os.environ.get("SIM_STUDIO_UE_CONTACT_EXPORT", ""),
        "SIM_STUDIO_UE_RUNNER_CMD": os.environ.get("SIM_STUDIO_UE_RUNNER_CMD", ""),
    }
    resolved_paths = {
        "SIM_STUDIO_UE_PROJECT": resolve_path(env["SIM_STUDIO_UE_PROJECT"]),
        "SIM_STUDIO_UE_EXECUTABLE": resolve_path(env["SIM_STUDIO_UE_EXECUTABLE"]),
        "SIM_STUDIO_ASSET_REGISTRY": resolve_path(env["SIM_STUDIO_ASSET_REGISTRY"]),
    }
    env_presence = {key: bool(value) for key, value in env.items()}
    env_presence["SIM_STUDIO_UE_PROJECT"] = bool(explicit_project)
    config_sources = {
        "SIM_STUDIO_UE_PROJECT": (
            "environment" if explicit_project else "workspace_default" if workspace_project else "missing"
        )
    }
    path_exists = {key: bool(path and Path(path).exists()) for key, path in resolved_paths.items()}
    project_path = Path(resolved_paths["SIM_STUDIO_UE_PROJECT"]) if resolved_paths["SIM_STUDIO_UE_PROJECT"] else None
    project_is_file = bool(project_path and project_path.is_file())
    project_is_uproject = bool(project_path and project_path.suffix.lower() == ".uproject")
    map_file = resolve_game_map_file(project_path, env["SIM_STUDIO_UE_MAP"]) if project_is_uproject else None
    resolved_paths["SIM_STUDIO_UE_MAP_PACKAGE"] = str(map_file) if map_file else ""
    path_exists["SIM_STUDIO_UE_MAP_PACKAGE"] = bool(map_file and map_file.is_file())
    path_properties = {
        "SIM_STUDIO_UE_PROJECT": {
            "exists": path_exists["SIM_STUDIO_UE_PROJECT"],
            "is_file": project_is_file,
            "suffix": project_path.suffix if project_path else "",
            "is_uproject": project_is_uproject,
        },
        "SIM_STUDIO_UE_MAP": {
            "package": env["SIM_STUDIO_UE_MAP"],
            "resolved_umap": str(map_file) if map_file else "",
            "exists": path_exists["SIM_STUDIO_UE_MAP_PACKAGE"],
        },
    }
    failure_code = None
    failure_message = None
    next_action = "Run the UE bridge; all required preflight inputs are present."
    if not path_exists["SIM_STUDIO_UE_PROJECT"]:
        failure_code = "F1_UPROJECT_MISSING"
        failure_message = "SIM_STUDIO_UE_PROJECT is missing or does not exist."
        next_action = "Set SIM_STUDIO_UE_PROJECT to the absolute path of a valid .uproject file."
    elif not project_is_file or not project_is_uproject:
        failure_code = "F1_UPROJECT_INVALID"
        failure_message = "SIM_STUDIO_UE_PROJECT must point to an existing .uproject file."
        next_action = "Set SIM_STUDIO_UE_PROJECT to a real .uproject file, not a directory or unrelated file."
    elif not path_exists["SIM_STUDIO_UE_EXECUTABLE"]:
        failure_code = "F2_UE_EXECUTABLE_MISSING"
        failure_message = "SIM_STUDIO_UE_EXECUTABLE is missing or does not exist."
        next_action = "Set SIM_STUDIO_UE_EXECUTABLE to UnrealEditor-Cmd or an equivalent UE command executable."
    elif not env["SIM_STUDIO_UE_MAP"]:
        failure_code = "F3_UE_MAP_MISSING"
        failure_message = "SIM_STUDIO_UE_MAP is missing."
        next_action = "Set SIM_STUDIO_UE_MAP to the map package path used for harness cases."
    elif map_file is None:
        failure_code = "F3_UE_MAP_INVALID"
        failure_message = "SIM_STUDIO_UE_MAP must be a /Game/... package path."
        next_action = "Use the materialized UE map package path, for example /Game/Maps/MyMap.MyMap."
    elif not path_exists["SIM_STUDIO_UE_MAP_PACKAGE"]:
        failure_code = "F3_UE_MAP_PACKAGE_MISSING"
        failure_message = f"SIM_STUDIO_UE_MAP does not exist in the current project's Content directory: {map_file}"
        next_action = "Materialize the requested .umap under this .uproject's Content directory or select a map that is already present."
    elif not env["SIM_STUDIO_UE_ACTOR_CLASS"]:
        failure_code = "F4_UE_ACTOR_CLASS_MISSING"
        failure_message = "SIM_STUDIO_UE_ACTOR_CLASS is missing."
        next_action = "Set SIM_STUDIO_UE_ACTOR_CLASS to the Blueprint/C++ actor class that consumes harness scene specs."
    elif not path_exists["SIM_STUDIO_ASSET_REGISTRY"]:
        failure_code = "F5_ASSET_REGISTRY_MISSING"
        failure_message = "SIM_STUDIO_ASSET_REGISTRY is missing or does not exist."
        next_action = "Set SIM_STUDIO_ASSET_REGISTRY to a JSON asset registry with collider/mass/material/collision metadata."
    elif env["SIM_STUDIO_UE_CONTACT_EXPORT"] != "1":
        failure_code = "F6_CONTACT_EXPORT_DISABLED"
        failure_message = "SIM_STUDIO_UE_CONTACT_EXPORT=1 is required before UE verification."
        next_action = "Enable or implement UE contact/hit export, then set SIM_STUDIO_UE_CONTACT_EXPORT=1."
    elif not env["SIM_STUDIO_UE_RUNNER_CMD"]:
        failure_code = "F7_UE_RUNNER_CMD_MISSING"
        failure_message = "SIM_STUDIO_UE_RUNNER_CMD is required; no default legacy runner is invoked."
        next_action = "Set SIM_STUDIO_UE_RUNNER_CMD to a harness-compatible runner command that writes the required UE artifacts."
    return {
        "schema_version": "harness_ue_preflight_report_v1",
        "backend_mode": "ue",
        "requested_case_id": case_id,
        "env_presence": env_presence,
        "config_sources": config_sources,
        "resolved_paths": resolved_paths,
        "path_exists": path_exists,
        "path_properties": path_properties,
        "raw_env": env,
        "failure_code": failure_code,
        "failure_message": failure_message,
        "next_required_action": next_action,
        "whether_real_ue_invoked": False,
    }


def initialized_workspace_project() -> str:
    """Use the materialized workspace project only when its root was explicitly selected."""
    raw_workspace = os.environ.get("SIM_HARNESS_WORKSPACE", "").strip()
    if not raw_workspace:
        return ""
    workspace = Path(raw_workspace).expanduser()
    if not workspace.is_absolute():
        return ""
    project = workspace.resolve() / "ue" / "SimulatorWorkspace.uproject"
    return str(project) if project.is_file() else ""


def build_backend_report(
    case: CaseSpec,
    run_id: str,
    preflight: dict[str, Any],
    *,
    phase: str,
    real_ue_invoked: bool,
    status: str = "failed",
    failure_code: str | None = None,
    failure_message: str | None = None,
    runner_command: list[str] | None = None,
    failure_category: str | None = None,
) -> dict[str, Any]:
    code = failure_code if failure_code is not None else preflight.get("failure_code")
    message = failure_message if failure_message is not None else preflight.get("failure_message")
    category = failure_category if failure_category is not None else ("preflight_failure" if phase == "preflight" else ("runtime_failure" if status != "completed" else None))
    return {
        "schema_version": "harness_ue_backend_report_v1",
        "backend_mode": "ue",
        "requested_case_id": case.case_id,
        "run_id": run_id,
        "status": status,
        "phase": phase,
        "failure_category": category,
        "failure_code": code,
        "failure_message": message,
        "next_required_action": preflight.get("next_required_action") if phase == "preflight" else "Inspect UE runner stderr/stdout and missing output artifacts.",
        "whether_real_ue_invoked": real_ue_invoked,
        "env_presence": preflight["env_presence"],
        "resolved_paths": preflight["resolved_paths"],
        "runner_command": runner_command or [],
        "expected_outputs": [
            "trajectory.json",
            "contact_events.json",
            "camera_trajectory.json",
            "render_manifest.json",
            "map_report.json",
            "views/<camera_id>/rgb.mp4",
            "views/<camera_id>/depth.exr",
            "views/<camera_id>/segmentation.exr",
            "views/<camera_id>/meta.json",
        ],
    }


def invoke_real_ue_runner(
    case: CaseSpec,
    run_dir: Path,
    output_dir: Path,
    scene_spec: dict[str, Any],
    preflight: dict[str, Any],
    requested_views: list[str],
    render_passes: list[str],
) -> dict[str, Any]:
    run_id = run_dir.name
    runner_command = build_runner_command(run_dir, preflight, requested_views=requested_views, render_passes=render_passes)
    timeout = int(os.environ.get("SIM_STUDIO_UE_TIMEOUT_SECONDS", "3600"))
    try:
        completed = run_runner_process_group(
            runner_command,
            cwd=Path(__file__).resolve().parents[2],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - requires a real long-running UE child.
        write_json(output_dir / "runner_stdout.json", {"stdout": exc.stdout or ""})
        write_json(output_dir / "runner_stderr.json", {"stderr": exc.stderr or ""})
        return build_backend_report(
            case,
            run_id,
            preflight,
            phase="runtime",
            real_ue_invoked=True,
            failure_code="F7_UE_RUNNER_TIMEOUT",
            failure_message=f"UE runner exceeded {timeout} seconds; its process group was terminated.",
            runner_command=runner_command,
        )
    except Exception as exc:  # pragma: no cover - requires real UE bridge config.
        return build_backend_report(
            case,
            run_id,
            preflight,
            phase="runtime",
            real_ue_invoked=True,
            failure_code="F7_UE_RUNNER_EXCEPTION",
            failure_message=str(exc),
            runner_command=runner_command,
        )
    write_json(output_dir / "runner_stdout.json", {"stdout": completed.stdout})
    write_json(output_dir / "runner_stderr.json", {"stderr": completed.stderr})
    if completed.returncode != 0:
        runner_report = read_runner_failure_report(run_dir)
        return build_backend_report(
            case,
            run_id,
            preflight,
            phase="runtime",
            real_ue_invoked=True,
            failure_code=str(runner_report.get("failure_code") or "F8_UE_RUNNER_FAILED"),
            failure_message=str(runner_report.get("failure_message") or f"UE runner exited with code {completed.returncode}."),
            runner_command=runner_command,
            failure_category=str(runner_report.get("failure_category") or "runtime_failure"),
        )
    missing = [name for name in ("trajectory.json", "contact_events.json", "camera_trajectory.json", "render_manifest.json", "map_report.json") if not (run_dir / name).exists() and not (output_dir / name).exists()]
    if missing:
        return build_backend_report(
            case,
            run_id,
            preflight,
            phase="runtime",
            real_ue_invoked=True,
            failure_code="F9_UE_OUTPUT_MISSING",
            failure_message=f"UE runner completed but did not produce required outputs: {missing}.",
            runner_command=runner_command,
            failure_category="artifact_missing",
        )
    render_sync = check_render_sync(run_dir, require_depth="depth" in set(render_passes), require_segmentation="segmentation" in set(render_passes), write=True)
    if render_sync["status"] != "pass":
        first_code = str((render_sync.get("failure_codes") or ["F_RENDER_SYNC_FAIL"])[0])
        return build_backend_report(
            case,
            run_id,
            preflight,
            phase="runtime",
            real_ue_invoked=True,
            failure_code=first_code,
            failure_message=f"UE runner completed but render sync validation failed: {render_sync.get('failure_codes', [])}.",
            runner_command=runner_command,
            failure_category="artifact_missing" if first_code in {"F_DEPTH_MISSING", "F_VIEW_MISMATCH"} else "render_sync_failure",
        )
    return build_backend_report(case, run_id, preflight, phase="runtime", real_ue_invoked=True, status="completed", runner_command=runner_command)


def run_runner_process_group(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run the bridge and ensure a timeout also terminates its UnrealEditor child."""
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows is not a supported native-UE host yet.
            proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover
                proc.kill()
            stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def build_runner_command(run_dir: Path, preflight: dict[str, Any], *, requested_views: list[str], render_passes: list[str]) -> list[str]:
    raw = os.environ.get("SIM_STUDIO_UE_RUNNER_CMD")
    if not raw:
        raise RuntimeError("SIM_STUDIO_UE_RUNNER_CMD is required for real UE invocation.")
    values = {
        "case_spec": str(run_dir / "case_spec.json"),
        "scene_spec": str(run_dir / "scene_spec.json"),
        "run_dir": str(run_dir),
        "map": preflight["raw_env"]["SIM_STUDIO_UE_MAP"],
        "actor_class": preflight["raw_env"]["SIM_STUDIO_UE_ACTOR_CLASS"],
        "asset_registry": str(preflight["resolved_paths"]["SIM_STUDIO_ASSET_REGISTRY"]),
        "ue_project": str(preflight["resolved_paths"]["SIM_STUDIO_UE_PROJECT"]),
        "ue_executable": str(preflight["resolved_paths"]["SIM_STUDIO_UE_EXECUTABLE"]),
        "actor_placement": str(run_dir / "runtime_actor_placement.json"),
    }
    command = [part.format(**values) for part in shlex.split(raw)]
    append_option(command, "--case-spec", values["case_spec"])
    append_option(command, "--run-dir", values["run_dir"])
    append_option(command, "--output-dir", values["run_dir"])
    append_option(command, "--camera-plan", str(run_dir / "camera_plan.json"))
    append_option(command, "--render-pass-manifest-out", str(run_dir / "render_pass_manifest.json"))
    append_option(command, "--views", ",".join(requested_views))
    append_option(command, "--passes", ",".join(render_passes))
    append_option(command, "--mode", os.environ.get("SIM_STUDIO_UE_RENDER_MODE", "both"))
    append_option(command, "--map", values["map"])
    append_option(command, "--actor-class", values["actor_class"])
    append_option(command, "--asset-registry", values["asset_registry"])
    append_option(command, "--actor-placement", values["actor_placement"])
    append_option(command, "--ue-project", values["ue_project"])
    append_option(command, "--ue-executable", values["ue_executable"])
    return command


def read_runner_failure_report(run_dir: Path) -> dict[str, Any]:
    for path in (run_dir / "local_ue_runner_report.json", run_dir / "ue_output" / "local_ue_runner_report.json"):
        if not path.exists():
            continue
        try:
            import json

            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    return {}


def append_option(command: list[str], option: str, value: str) -> None:
    if not any(part == option or part.startswith(f"{option}=") for part in command):
        command.extend([option, value])


def evaluate_ue_physics_readiness(run_dir: str | Path) -> tuple[bool, dict[str, Any]]:
    run_dir = Path(run_dir)
    trajectory_path = run_dir / "trajectory.json"
    contact_path = run_dir / "contact_events.json"
    if not trajectory_path.is_file() or not contact_path.is_file():
        return False, {
            "required": False,
            "status": "fail",
            "violations": ["trajectory_or_contact_artifact_missing"],
        }
    trajectory_payload = read_json(trajectory_path)
    trajectory = trajectory_payload.get("frames") if isinstance(trajectory_payload, dict) else trajectory_payload
    if not isinstance(trajectory, list) or not trajectory:
        return False, {
            "required": False,
            "status": "fail",
            "violations": ["trajectory_empty_or_invalid"],
        }
    from harness.verification.run_quality import validate_solver_execution

    failures: list[dict[str, Any]] = []
    provenance = validate_solver_execution(run_dir, trajectory, failures, backend="ue")
    provenance_ready = provenance.get("status") in {"pass", "not_required"}
    return provenance_ready and not failures, provenance


def standardize_runner_outputs(run_dir: Path, output_dir: Path, case: CaseSpec, run_id: str, report: dict[str, Any], *, camera_plan: Any, render_passes: list[str], requested_view_count: int) -> None:
    for name in ("trajectory.json", "contact_events.json", "fracture_events.json", "camera_trajectory.json", "render_manifest.json"):
        source = run_dir / name
        if not source.exists():
            source = output_dir / name
        if source.exists() and source.parent != run_dir:
            target = run_dir / name
            target.write_bytes(source.read_bytes())
        root_copy = run_dir / name
        output_copy = output_dir / name
        if root_copy.exists() and (not output_copy.exists() or output_copy.stat().st_size <= 3):
            output_copy.write_bytes(root_copy.read_bytes())
    video_source = run_dir / "video.mp4"
    if not video_source.exists() and camera_plan.views:
        video_source = run_dir / "views" / camera_plan.views[0].camera_id / "rgb.mp4"
    if not video_source.exists():
        video_source = output_dir / "video.mp4"
    if video_source.exists() and video_source.parent != run_dir:
        (run_dir / "video.mp4").write_bytes(video_source.read_bytes())
    manifest = write_render_contract_artifacts(run_dir, backend="ue", case_id=case.case_id, camera_plan=camera_plan, render_passes=render_passes, allow_placeholders=False, source="ue_runner")
    write_json(output_dir / "render_pass_manifest.json", manifest)
    render_sync = check_render_sync(run_dir, require_depth="depth" in set(render_passes), require_segmentation="segmentation" in set(render_passes), write=True)
    observability = verify_render_observability(run_dir, require_multiview=requested_view_count > 1, require_depth="depth" in set(render_passes), min_view_count=requested_view_count)
    camera_plan_dict = camera_plan_to_json(camera_plan)
    existing_render_config = read_json(run_dir / "inputs" / "render_config.json") if (run_dir / "inputs" / "render_config.json").is_file() else {}
    render_config = {
        **existing_render_config,
        "schema_version": "render_config.v2.3",
        "mode": os.environ.get("SIM_STUDIO_UE_RENDER_MODE", "both"),
        "backend": "ue",
        "ue_renderer_only": True,
        "seed": int(os.environ.get("SIM_STUDIO_SEED", case.data.get("seed") or "0")),
        "fps": infer_fps_from_views(run_dir),
        "views": [str(view.get("camera_id")) for view in camera_plan_dict.get("views", []) if isinstance(view, dict) and view.get("camera_id")],
        "passes": render_passes,
    }
    artifact_manager = ArtifactManager(run_dir)
    artifact_manager.write_inputs(
        case_spec=case.data,
        scene_spec=compile_minimal_scene_spec(case.data),
        camera_plan=camera_plan_dict,
        render_config=render_config,
    )
    world_manifest = artifact_manager.finalize(
        run_id=run_id,
        case_id=case.case_id,
        mode=str(render_config["mode"]),
        seed=int(render_config["seed"]),
        camera_plan=camera_plan_dict,
        render_config=render_config,
        rgb_video_source=run_dir / "video.mp4",
    )
    write_json(run_dir / "ue_backend_report.json", report)
    write_json(
        output_dir / "summary.json",
        {
            "schema_version": "harness_runtime_artifact_v1",
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "run_id": run_id,
            "case_id": case.case_id,
            "capability_id": case.capability_id,
            "backend": "ue",
            "status": "completed",
            "ue_backend_report": "ue_backend_report.json",
        },
    )
    from harness.verification.physics_verifier import PhysicsVerifier

    verifier_report = PhysicsVerifier().verify_run_dir(run_dir, write=True)
    map_report = read_json(run_dir / "map_report.json")
    map_ready = map_report.get("status") == "pass"
    asset_resolution = read_json(run_dir / "asset_resolution.json")
    asset_quality = asset_resolution.get("quality_gate") if isinstance(asset_resolution.get("quality_gate"), dict) else {}
    asset_catalog_reference_ready = bool(asset_quality.get("reference_assets_ready"))
    collision_reference = collision_geometry_reference_status(run_dir)
    assets_reference_ready = asset_catalog_reference_ready and collision_reference["ready"]
    physics_ready, physics_provenance = evaluate_ue_physics_readiness(run_dir)
    execution_ready = map_ready and physics_ready and verifier_report["status"] == "pass" and all(observability[key] for key in ("camera_plan_ready", "multi_view_ready", "render_pass_ready", "sync_ready")) and observability["depth_ready"] and bool(render_sync.get("camera_state_ready")) and (run_dir / "sensor_state.json").exists()
    local_preview_ready = execution_ready and not assets_reference_ready and (
        int(asset_quality.get("local_preview_count") or 0) > 0 or asset_catalog_reference_ready
    )
    run_readiness = {
        "schema_version": "harness_run_readiness_v1",
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "reference_ready": execution_ready and assets_reference_ready,
        "local_preview_ready": local_preview_ready,
        "publication_tier": "reference" if execution_ready and assets_reference_ready else "local_preview" if local_preview_ready else "rejected",
        "assets_reference_ready": assets_reference_ready,
        "asset_catalog_reference_ready": asset_catalog_reference_ready,
        "collision_geometry_reference_ready": collision_reference["ready"],
        "unverified_collision_object_ids": collision_reference["unverified_object_ids"],
        "local_preview_asset_count": int(asset_quality.get("local_preview_count") or 0),
        "asset_fallback_count": int(asset_quality.get("fallback_count") or 0),
        "map_ready": map_ready,
        "physics_ready": physics_ready,
        "execution_ready": execution_ready,
        "physics_provenance": physics_provenance,
        "visual_ready": (run_dir / "video.mp4").exists(),
        "backend": "ue",
        "case_id": case.case_id,
        "verifier_status": verifier_report["status"],
        "ue_render_real": bool(render_sync["ue_render_real"]),
        "depth_source": render_sync["depth_source"],
        "multi_view_sync_ok": bool(render_sync["multi_view_sync_ok"]),
        "render_pass_valid": bool(render_sync["render_pass_valid"]),
        "render_observability_fail": int(render_sync["render_observability_fail"]),
        "camera_state_ready": bool(render_sync.get("camera_state_ready")),
        "sensor_state_ready": (run_dir / "sensor_state.json").exists(),
        **observability,
    }
    write_json(run_dir / "run_readiness.json", run_readiness)
    write_json(output_dir / "run_readiness.json", run_readiness)
    write_json(
        run_dir / "harness_artifact.json",
        {
            "schema_version": "harness_runtime_artifact_package_v1",
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "run_id": run_id,
            "case_id": case.case_id,
            "capability_id": case.capability_id,
            "backend": "ue",
            "status": "completed",
            "camera_plan_ready": observability["camera_plan_ready"],
            "multi_view_ready": observability["multi_view_ready"],
            "depth_ready": observability["depth_ready"],
            "render_pass_ready": observability["render_pass_ready"],
            "sync_ready": observability["sync_ready"],
            "ue_render_real": bool(render_sync["ue_render_real"]),
            "depth_source": render_sync["depth_source"],
            "multi_view_sync_ok": bool(render_sync["multi_view_sync_ok"]),
            "render_pass_valid": bool(render_sync["render_pass_valid"]),
            "render_observability_fail": int(render_sync["render_observability_fail"]),
            "view_count": observability["view_count"],
            "camera_ids": observability["camera_ids"],
            "paths": {
                "case_spec": "case_spec.json",
                "scene_spec": "scene_spec.json",
                "map_report": "map_report.json",
                "asset_resolution": "asset_resolution.json",
                "scene_layout": "scene_layout.json",
                "static_scene_report": "static_scene_report.json",
                "runtime_actor_placement": "runtime_actor_placement.json",
                "runtime_actor_placement_report": "runtime_actor_placement_report.json",
                "trajectory": "trajectory.json",
                "contact_events": "contact_events.json",
                "fracture_events": "fracture_events.json",
                "camera_trajectory": "camera_trajectory.json",
                "sensor_state": "sensor_state.json",
                "summary": "ue_output/summary.json",
                "run_readiness": "run_readiness.json",
                "render_manifest": "render_manifest.json",
                "render_pass_manifest": "render_pass_manifest.json",
                "render_sync_report": "render_sync_report.json",
                "video": "video.mp4",
                "ue_preflight_report": "ue_preflight_report.json",
                "ue_backend_report": "ue_backend_report.json",
                "verifier_report": "verifier_report.json",
            },
        },
    )
    write_json(
        run_dir / "artifact_manifest.json",
        {
            "schema_version": "harness_artifact_manifest_v1",
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "run_id": run_id,
            "case_id": case.case_id,
            "backend": "ue",
            "status": "completed",
            "views": world_manifest.get("views", {}),
            "intermediates": world_manifest.get("intermediates", {}),
            "artifacts": {
                "case_spec": "case_spec.json",
                "scene_spec": "scene_spec.json",
                "map_report": "map_report.json",
                "asset_resolution": "asset_resolution.json",
                "scene_layout": "scene_layout.json",
                "static_scene_report": "static_scene_report.json",
                "runtime_actor_placement": "runtime_actor_placement.json",
                "runtime_actor_placement_report": "runtime_actor_placement_report.json",
                "trajectory": "trajectory.json",
                "contact_events": "contact_events.json",
                "fracture_events": "fracture_events.json",
                "camera_trajectory": "camera_trajectory.json",
                "sensor_state": "sensor_state.json",
                "summary": "ue_output/summary.json",
                "run_readiness": "run_readiness.json",
                "render_manifest": "render_manifest.json",
                "render_pass_manifest": "render_pass_manifest.json",
                "render_sync_report": "render_sync_report.json",
                "video": "video.mp4",
                "harness_artifact": "harness_artifact.json",
                "harness_verifier": "harness_verifier.json",
                "verifier_report": "verifier_report.json",
                "ue_preflight_report": "ue_preflight_report.json",
                "ue_backend_report": "ue_backend_report.json",
            },
        },
    )


def collision_geometry_reference_status(run_dir: str | Path) -> dict[str, Any]:
    placement_path = Path(run_dir) / "runtime_actor_placement.json"
    if not placement_path.is_file():
        return {"ready": False, "unverified_object_ids": ["runtime_actor_placement_missing"]}
    placement = read_json(placement_path)
    bindings = placement.get("actor_bindings") if isinstance(placement.get("actor_bindings"), list) else []
    accepted = {"runtime_controlled", "body_setup_verified", "asset_body_setup_reflected", "not_applicable"}
    unverified = []
    for binding in bindings:
        if not isinstance(binding, dict) or not binding.get("physics_critical"):
            continue
        physics = binding.get("physics") if isinstance(binding.get("physics"), dict) else {}
        if not physics.get("collision_enabled"):
            continue
        if str(physics.get("collision_geometry_verification") or "") not in accepted:
            unverified.append(str(binding.get("object_id") or "unnamed"))
    return {"ready": not unverified, "unverified_object_ids": unverified}


def resolve_path(value: str) -> str:
    return str(Path(value).expanduser().resolve()) if value else ""


def resolve_game_map_file(project_path: Path | None, package_path: str) -> Path | None:
    """Map a /Game package name to the .umap owned by this .uproject."""
    value = package_path.strip()
    if not project_path or not value.startswith("/Game/"):
        return None
    relative = value[len("/Game/") :].split(".", 1)[0]
    parts = relative.split("/")
    if not relative or any(part in {"", ".", ".."} for part in parts):
        return None
    return project_path.parent / "Content" / Path(*parts).with_suffix(".umap")


def infer_fps_from_views(run_dir: Path) -> int:
    for meta_path in sorted((run_dir / "views").glob("*/meta.json")):
        try:
            import json

            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            return int(meta.get("fps") or 60)
        except (TypeError, ValueError):
            continue
    return 60


def camera_plan_to_json(camera_plan: Any) -> dict[str, Any]:
    from harness.runtime.camera_planner import camera_plan_to_dict

    return camera_plan_to_dict(camera_plan)


def compile_minimal_scene_spec(case_spec: dict[str, Any]) -> dict[str, Any]:
    map_package = requested_map_package(case_spec)
    return {
        "schema_version": "harness_scene_spec_v1",
        "case_id": case_spec.get("case_id"),
        "capability_id": case_spec.get("capability_id"),
        "objects": case_spec.get("objects", []),
        "active_objects": case_spec.get("active_objects", []),
        "passive_objects": case_spec.get("passive_objects", []),
        "expected_physics": case_spec.get("expected_physics", {}),
        "required_assets": case_spec.get("required_assets", []),
        "required_signals": case_spec.get("required_signals", []),
        "camera_policy": (case_spec.get("expected_physics") or {}).get("camera", {"mode": "unspecified"}),
        "map": {
            "requested_package": map_package,
            "require_opened": True,
            "minimum_actor_count": 1,
            "dependency_policy": "runtime_load_success",
        },
        "runtime_contract": {
            "must_export": ["trajectory.json", "contact_events.json", "run_readiness.json", "render_manifest.json", "map_report.json"],
            "must_not_fallback_silently": True,
        },
    }


def requested_map_package(case_spec: dict[str, Any] | None = None) -> str:
    explicit = os.environ.get("SIM_STUDIO_UE_MAP", "").strip()
    if explicit:
        return explicit
    scene = case_spec.get("scene") if isinstance(case_spec, dict) and isinstance(case_spec.get("scene"), dict) else {}
    return str(scene.get("map_preference") or scene.get("map_package") or "").strip()
