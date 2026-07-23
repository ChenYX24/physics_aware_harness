from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UE_PROJECT = ROOT / "ue_template" / "SimulatorStudioTemplate.uproject"
DEFAULT_UE_EXECUTABLE = Path("/Users/Shared/Epic Games/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd")
DEFAULT_UE_SCRIPT = ROOT / "scripts" / "native_ue_physics_phenomena_scene.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.artifact_manager import ArtifactManager, link_or_copy
from harness.core.capability import canonical_capability_id
from harness.core.timebase import build_timebase
from harness.runtime.mujoco_rigid import simulate_rigid_case
from harness.verification.render_sync_checker import depth_pixel_statistics, has_mp4_magic, has_openexr_magic


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harness-compatible local UnrealEditor-Cmd runner.")
    parser.add_argument("--case-spec", required=True)
    parser.add_argument("--scene-spec")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--camera-plan", required=True)
    parser.add_argument("--actor-placement", default="")
    parser.add_argument("--render-pass-manifest-out")
    parser.add_argument("--views", default="front_static,side_static,top_down,tracking_subject,event_closeup")
    parser.add_argument("--passes", default="rgb,depth,segmentation")
    parser.add_argument("--map", default="")
    parser.add_argument("--actor-class", default="")
    parser.add_argument("--asset-registry", default="")
    parser.add_argument("--ue-project", default="")
    parser.add_argument("--ue-executable", default="")
    parser.add_argument("--mode", choices=["rgb", "data", "both"], default=os.environ.get("SIM_STUDIO_UE_RENDER_MODE", "both"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    case_spec = read_json(Path(args.case_spec))
    camera_plan = read_json(Path(args.camera_plan))
    actor_placement = read_optional_json(Path(args.actor_placement)) if args.actor_placement else {}
    if args.actor_placement and not actor_placement.get("actor_bindings"):
        write_json(run_dir / "local_ue_runner_report.json", fail_report("F_RUNTIME_ACTOR_PLACEMENT_MISSING", f"Actor placement is missing or invalid: {args.actor_placement}"))
        return 2
    duration_s = duration_for_case(case_spec)
    try:
        timebase = timebase_for_case(case_spec)
        fps = int(timebase["render_fps"])
        simulation_trajectory = prepare_rigid_simulation(case_spec, actor_placement, fps=fps, duration_s=duration_s)
    except (RuntimeError, ValueError) as exc:
        write_json(run_dir / "local_ue_runner_report.json", fail_report("F_SIMULATOR_UNAVAILABLE", str(exc)))
        return 2
    if simulation_trajectory:
        write_json(run_dir / "simulation_trajectory.json", simulation_trajectory)
    studio_scene_spec = build_studio_scene_spec(case_spec, args, actor_placement=actor_placement)
    runtime_scene = build_runtime_scene(case_spec, camera_plan, args, pass_mode="data" if args.mode == "both" else args.mode, actor_placement=actor_placement, simulation_trajectory=simulation_trajectory)
    studio_scene_path = run_dir / "studio_scene_spec.json"
    runtime_scene_path = run_dir / "studio_runtime_scene.json"
    write_json(studio_scene_path, studio_scene_spec)
    write_json(runtime_scene_path, runtime_scene)
    render_config = build_render_config(args, case_spec, camera_plan)
    ArtifactManager(run_dir).write_inputs(
        case_spec=case_spec,
        scene_spec=studio_scene_spec,
        camera_plan=camera_plan,
        render_config=render_config,
    )

    ue_project = Path(args.ue_project or os.environ.get("SIM_STUDIO_UE_PROJECT") or DEFAULT_UE_PROJECT)
    ue_executable = Path(args.ue_executable or os.environ.get("SIM_STUDIO_UE_EXECUTABLE") or DEFAULT_UE_EXECUTABLE)
    ue_script = Path(os.environ.get("SIM_STUDIO_UE_SCRIPT") or DEFAULT_UE_SCRIPT)
    if not ue_project.is_file():
        write_json(run_dir / "local_ue_runner_report.json", fail_report("F1_UPROJECT_MISSING", f"UE project not found: {ue_project}"))
        return 2
    if not ue_executable.is_file():
        write_json(run_dir / "local_ue_runner_report.json", fail_report("F2_UE_EXECUTABLE_MISSING", f"UE executable not found: {ue_executable}"))
        return 2
    if not ue_script.is_file():
        write_json(run_dir / "local_ue_runner_report.json", fail_report("F3_UE_SCRIPT_MISSING", f"UE Python scene script not found: {ue_script}"))
        return 2

    command = [
        str(ue_executable),
        f"-project={ue_project}",
        "-RenderOffScreen",
        "-unattended",
        "-nosplash",
        "-NoScreenMessages",
        "-stdout",
        "-FullStdOutLogOutput",
        f"-ExecutePythonScript={ue_script}",
    ]
    started = time.perf_counter()
    pass_results: dict[str, dict[str, Any]] = {}
    rgb_native_output: Path | None = None
    data_native_output: Path | None = None
    render_trajectory = simulation_trajectory
    live_fracture_ids = fracture_object_ids(case_spec)
    live_fracture = bool(live_fracture_ids)
    single_pass_fracture = (
        args.mode == "both"
        and simulation_trajectory is None
        and live_fracture
        and os.environ.get("SIM_STUDIO_UE_SINGLE_PASS_FRACTURE", "1") != "0"
    )
    for pass_mode in pass_sequence(
        args.mode,
        live_solver=simulation_trajectory is None,
        live_fracture=live_fracture,
        single_pass_fracture=single_pass_fracture,
    ):
        native_output = native_output_dir(run_dir, pass_mode, args.mode)
        data_capable_pass = pass_mode in {"data", "combined"}
        pass_trajectory = None if data_capable_pass and live_fracture_ids else (render_trajectory if data_capable_pass else simulation_trajectory)
        pass_runtime_scene = build_runtime_scene(case_spec, camera_plan, args, pass_mode=pass_mode, actor_placement=actor_placement, simulation_trajectory=pass_trajectory)
        sampling_map = read_optional_json(run_dir / "sampling_map.json")
        if sampling_map.get("solver_cache_sha256") and not (pass_mode == "data" and live_fracture_ids):
            pass_runtime_scene["simulation"]["solver_cache_sha256"] = sampling_map["solver_cache_sha256"]
        pass_runtime_scene_path = run_dir / "logs" / f"studio_runtime_scene_{pass_mode}.json"
        write_json(pass_runtime_scene_path, pass_runtime_scene)
        proc_result = run_native_pass(
            command,
            args=args,
            case_spec=case_spec,
            runtime_scene=pass_runtime_scene,
            studio_scene_path=studio_scene_path,
            runtime_scene_path=pass_runtime_scene_path,
            native_output=native_output,
            pass_mode=pass_mode,
        )
        pass_results[pass_mode] = proc_result
        append_runner_log(run_dir, pass_mode, proc_result)
        if proc_result["status"] == "timeout":
            write_json(run_dir / "local_ue_runner_report.json", fail_report("F6_RUNTIME_OR_RENDER_FAILURE", f"UE {pass_mode} pass timed out after {proc_result['timeout']}s", pass_results=pass_results))
            return 2
        if proc_result["status"] == "failed":
            classified = classify_native_pass_failure(native_output, pass_mode)
            write_json(
                run_dir / "local_ue_runner_report.json",
                fail_report(
                    classified["failure_code"],
                    classified["failure_message"] or f"UE {pass_mode} pass exited with {proc_result['returncode']}",
                    command=command,
                    pass_results=pass_results,
                    pass_mode=pass_mode,
                    stderr_tail=str(proc_result["stderr"])[-4000:],
                ),
            )
            return int(proc_result["returncode"] or 2)
        if pass_mode in {"rgb", "combined"}:
            rgb_native_output = native_output
            if render_trajectory is None:
                render_trajectory = read_json(native_output / "trajectory.json")
                solver_source = native_output / "solver_trajectory.json"
                if solver_source.is_file():
                    shutil.copyfile(solver_source, run_dir / "solver_trajectory.json")
                else:
                    write_json(run_dir / "solver_trajectory.json", render_trajectory)
                sampling_source = native_output / "sampling_map.json"
                if sampling_source.is_file():
                    shutil.copyfile(sampling_source, run_dir / "sampling_map.json")
                    if live_fracture_ids:
                        shutil.copyfile(sampling_source, run_dir / "rgb_sampling_map.json")
        if pass_mode in {"data", "combined"}:
            quantization = quantize_native_instance_segmentation(
                native_output,
                width=int(render_config.get("width") or 0),
                height=int(render_config.get("height") or 0),
                fps=int(render_config.get("fps") or 0),
                required="segmentation" in {item.strip() for item in args.passes.split(",")},
                required_object_ids=live_fracture_ids,
            )
            proc_result["segmentation_quantization"] = quantization
            if quantization.get("status") == "fail":
                write_json(
                    run_dir / "local_ue_runner_report.json",
                    fail_report(
                        "F_SEGMENTATION_QUANTIZATION_FAILED",
                        str(quantization.get("error") or "Instance segmentation palette quantization failed."),
                        pass_results=pass_results,
                    ),
                )
                return 2
            if live_fracture_ids:
                sampling_source = native_output / "sampling_map.json"
                if sampling_source.is_file():
                    shutil.copyfile(sampling_source, run_dir / "sampling_map.json")
            data_native_output = native_output

    primary_native_output = data_native_output or rgb_native_output
    if primary_native_output is None:
        write_json(run_dir / "local_ue_runner_report.json", fail_report("F6_RUNTIME_OR_RENDER_FAILURE", "No UE pass was executed."))
        return 2
    report = standardize_native_output(
        run_dir,
        primary_native_output,
        camera_plan,
        started,
        render_mode=args.mode,
        rgb_native_output=rgb_native_output,
        render_config=render_config,
        case_spec=case_spec,
        scene_spec=studio_scene_spec,
    )
    report["execution_strategy"] = "single_process_shared_solver_multimodal" if single_pass_fracture else "separate_render_passes"
    report["pass_results"] = {
        key: {"status": value.get("status"), "returncode": value.get("returncode"), "native_output": str(native_output_dir(run_dir, key, args.mode))}
        for key, value in pass_results.items()
    }
    write_json(run_dir / "local_ue_runner_report.json", report)
    return 0 if report["status"] == "completed" else 2


def prepare_rigid_simulation(
    case_spec: dict[str, Any],
    actor_placement: dict[str, Any],
    *,
    fps: int,
    duration_s: float,
) -> list[dict[str, Any]] | None:
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    required_source = str(expected.get("required_trajectory_source") or "").strip().casefold()
    default_mode = "mujoco_replay" if required_source.startswith("mujoco") else "chaos_live"
    mode = os.environ.get("SIM_STUDIO_UE_RIGID_MODE", default_mode).strip().casefold()
    if required_source.startswith("mujoco") and mode != "mujoco_replay":
        raise RuntimeError(
            f"Case requires trajectory source {required_source!r}; SIM_STUDIO_UE_RIGID_MODE must be 'mujoco_replay'."
        )
    if mode == "chaos_live":
        return None
    if mode == "mujoco_replay":
        return simulate_rigid_case(case_spec, actor_placement, fps=fps, duration_s=duration_s)
    raise RuntimeError(
        f"Unsupported SIM_STUDIO_UE_RIGID_MODE={mode!r}; use 'chaos_live' or the explicit debug mode 'mujoco_replay'."
    )


def fracture_object_ids(case_spec: dict[str, Any]) -> set[str]:
    return {
        str(obj.get("id") or obj.get("object_id"))
        for obj in case_spec.get("objects") or []
        if isinstance(obj, dict) and isinstance(obj.get("fracture_response"), dict) and (obj.get("id") or obj.get("object_id"))
    }


def build_fracture_sensor_state_report(
    case_spec: dict[str, Any],
    render_mode: str,
    rgb_fractures: list[dict[str, Any]],
    data_fractures: list[dict[str, Any]],
) -> dict[str, Any]:
    fracture_ids = fracture_object_ids(case_spec)
    expected_fracture = (case_spec.get("expected_physics") or {}).get("expected_fracture", True)
    positive_fracture_expected = bool(fracture_ids) and expected_fracture is not False and not bool(case_spec.get("negative_or_boundary"))
    comparison_required = render_mode == "both" and positive_fracture_expected
    rgb_event_keys = fracture_event_keys(rgb_fractures, fracture_ids)
    data_event_keys = fracture_event_keys(data_fractures, fracture_ids)
    rgb_state_hashes = fracture_state_hashes(rgb_fractures, fracture_ids)
    data_state_hashes = fracture_state_hashes(data_fractures, fracture_ids)
    failure_codes: list[str] = []
    if comparison_required:
        if not rgb_event_keys or not data_event_keys:
            failure_codes.append("F_FRACTURE_SENSOR_EVENT_MISSING")
        elif rgb_event_keys != data_event_keys:
            failure_codes.append("F_FRACTURE_SENSOR_STATE_MISMATCH")
        if not rgb_state_hashes or not data_state_hashes:
            failure_codes.append("F_FRACTURE_FRAGMENT_STATE_MISSING")
        elif rgb_state_hashes != data_state_hashes:
            failure_codes.append("F_FRACTURE_SENSOR_STATE_MISMATCH")
    elif render_mode == "both" and rgb_event_keys != data_event_keys:
        failure_codes.append("F_FRACTURE_SENSOR_STATE_MISMATCH")
    if expected_fracture is False and (rgb_event_keys or data_event_keys):
        failure_codes.append("F_FRACTURE_UNEXPECTED")
    failure_codes = sorted(set(failure_codes))
    return {
        "schema_version": "harness_fracture_sensor_state_report_v2",
        "status": "pass" if not failure_codes else "fail",
        "comparison_required": comparison_required,
        "comparison_mode": "shared_solver_cocapture" if rgb_fractures is data_fractures else "independent_pass_comparison",
        "failure_codes": failure_codes,
        "rgb_fracture_events": len(rgb_fractures),
        "data_fracture_events": len(data_fractures),
        "rgb_event_keys": sorted([list(key) for key in rgb_event_keys]),
        "data_event_keys": sorted([list(key) for key in data_event_keys]),
        "rgb_fragment_state_hashes": rgb_state_hashes,
        "data_fragment_state_hashes": data_state_hashes,
    }


def fracture_event_keys(events: list[dict[str, Any]], fracture_ids: set[str]) -> set[tuple[str, int]]:
    return {
        (str(event.get("object_id") or ""), int(event.get("frame") or 0))
        for event in events
        if isinstance(event, dict) and (not fracture_ids or str(event.get("object_id") or "") in fracture_ids)
    }


def fracture_state_hashes(events: list[dict[str, Any]], fracture_ids: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        object_id = str(event.get("object_id") or "")
        if fracture_ids and object_id not in fracture_ids:
            continue
        state_hash = event.get("fragment_state_sha256")
        if state_hash:
            result[f"{object_id}@{int(event.get('frame') or 0)}"] = str(state_hash)
    return result


def build_studio_scene_spec(case_spec: dict[str, Any], args: argparse.Namespace, *, actor_placement: dict[str, Any] | None = None) -> dict[str, Any]:
    timebase = timebase_for_case(case_spec)
    return {
        "draft_id": str(case_spec.get("case_id") or "harness_case"),
        "prompt": case_spec.get("prompt", ""),
        "expanded_prompt": case_spec.get("prompt", ""),
        "asset_policy": "harness_local_ue_analytic_proxy",
        "physics_settings": {
            "duration_s": duration_for_case(case_spec),
            "fps": timebase["render_fps"],
            "timebase": timebase,
            "simulate_physics": True,
            "contact_event_recording": True,
        },
        "physics_controls": default_physics_controls(case_spec),
        "semantic_plan": {"case_type": native_case_type(case_spec), "source": "harness_case_spec"},
        "runtime_actor_placement": summarize_actor_placement(actor_placement),
        "camera_plan": {},
        "map_lighting_controls": default_lighting_controls("data", case_spec),
        "assets": [],
        "background": {"ue5_path": args.map or "/Game/Maps/MarketEnvironment/Maps/Day.Day"},
    }


def build_runtime_scene(case_spec: dict[str, Any], camera_plan: dict[str, Any], args: argparse.Namespace, *, pass_mode: str, actor_placement: dict[str, Any] | None = None, simulation_trajectory: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    timebase = timebase_for_case(case_spec)
    dynamic_objects, static_objects = runtime_objects_for_case(case_spec, actor_placement=actor_placement)
    physics_controls = default_physics_controls(case_spec)
    if simulation_trajectory:
        trajectory_source = str((simulation_trajectory[0] or {}).get("source") or "precomputed_trajectory")
        replay_driver = "ue_chaos_output_replay" if trajectory_source in {"adp_cpp_runtime_driver", "ue_chaos_transform_capture"} else trajectory_source
        physics_controls.update(
            {
                "simulate_physics": False,
                "simulation_driver": replay_driver,
                "trajectory_source": trajectory_source,
                "runtime_driver_backend": "precomputed_trajectory",
                "cpp_runtime_driver_enabled": False,
            }
        )
    lighting_controls = default_lighting_controls("rgb" if pass_mode == "combined" else pass_mode, case_spec)
    live_geometry_collection_data = pass_mode in {"data", "combined"} and bool(fracture_object_ids(case_spec))
    if physics_controls.get("simulate_physics") and (pass_mode == "rgb" or live_geometry_collection_data):
        lighting_controls["capture_backend"] = "highres_viewport"
    return {
        "schema_version": "studio_runtime_v1",
        "draft_id": str(case_spec.get("case_id") or "harness_case"),
        "case_type": native_case_type(case_spec),
        "background_map": {"ue5_path": args.map or "/Game/Maps/MarketEnvironment/Maps/Day.Day"},
        "prompt": case_spec.get("prompt", ""),
        "expanded_prompt": case_spec.get("prompt", ""),
        "simulation": {
            "duration_s": duration_for_case(case_spec),
            "fps": timebase["render_fps"],
            "dt": timebase["render_dt_s"],
            **timebase,
        },
        "physics": case_spec.get("physical_parameters") or {},
        "physics_controls": physics_controls,
        "render": {
            "width": int(os.environ.get("SIM_STUDIO_UE_WIDTH", "1920")),
            "height": int(os.environ.get("SIM_STUDIO_UE_HEIGHT", "1080")),
            "fps": int(os.environ.get("SIM_STUDIO_UE_FPS", "24")),
            "quality_preset": os.environ.get("SIM_STUDIO_UE_RENDER_QUALITY", "medium"),
            "pass_mode": pass_mode,
            "deterministic": True,
        },
        "camera": camera_runtime_from_plan(camera_plan),
        "requested_views": [str(view.get("camera_id")) for view in camera_plan.get("views", []) if isinstance(view, dict) and view.get("camera_id")],
        "map_lighting_controls": lighting_controls,
        "dynamic_objects": dynamic_objects,
        "static_objects": static_objects,
        "validation_targets": [],
        "precomputed_trajectory": simulation_trajectory or [],
        "asset_policy": "harness_local_ue_analytic_proxy",
    }


def runtime_objects_for_case(case_spec: dict[str, Any], actor_placement: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if actor_placement and actor_placement.get("actor_bindings"):
        return runtime_objects_from_actor_placement(actor_placement, case_spec)
    capability = canonical_capability_id(str(case_spec.get("capability_id") or ""))
    if capability == "rigid_body_contact_causality":
        return billiards_objects(case_spec)
    if capability == "sequential_contact_propagation":
        return domino_objects(case_spec)
    if capability == "rigid_body_gravity_collision":
        return falling_objects(case_spec)
    return generic_objects(case_spec)


def runtime_objects_from_actor_placement(actor_placement: dict[str, Any], case_spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_case_object = {
        str(obj.get("id") or obj.get("object_id")): obj
        for obj in case_spec.get("objects", [])
        if isinstance(obj, dict) and (obj.get("id") or obj.get("object_id"))
    }
    dynamic: list[dict[str, Any]] = []
    static: list[dict[str, Any]] = []
    case_parameters = case_spec.get("physical_parameters") if isinstance(case_spec.get("physical_parameters"), dict) else {}
    for index, binding in enumerate(actor_placement.get("actor_bindings") or []):
        if not isinstance(binding, dict):
            continue
        object_id = str(binding.get("object_id") or f"actor_{index:02d}")
        case_object = by_case_object.get(object_id, {})
        physics = binding.get("physics") if isinstance(binding.get("physics"), dict) else {}
        material = physics.get("material") if isinstance(physics.get("material"), dict) else {}
        collider = str(physics.get("collider") or "").casefold()
        is_dynamic_sphere = bool(physics.get("simulate_physics")) and "sphere" in collider
        linear_damping = case_object.get("linear_damping")
        if linear_damping is None:
            linear_damping = physics.get("linear_damping")
        if linear_damping is None and is_dynamic_sphere:
            linear_damping = case_parameters.get("ball_linear_damping")
        angular_damping = case_object.get("angular_damping")
        if angular_damping is None:
            angular_damping = physics.get("angular_damping")
        if angular_damping is None and is_dynamic_sphere:
            angular_damping = case_parameters.get("ball_angular_damping")
        transform = binding.get("transform") if isinstance(binding.get("transform"), dict) else {}
        bounds = binding.get("bounds") if isinstance(binding.get("bounds"), dict) else {}
        asset = binding.get("asset") if isinstance(binding.get("asset"), dict) else {}
        params = {
            "runtime_actor_id": binding.get("runtime_actor_id"),
            "binding_source": asset.get("binding_source"),
            "asset_runtime_usage": asset.get("runtime_usage"),
            "role": binding.get("role"),
            "material": physics.get("material"),
            "collision_profile": physics.get("collision_profile"),
            "collider": physics.get("collider"),
            "desired_extent_cm": desired_extent_cm_for_binding(binding),
            "fit_dynamic_plan": case_object.get("fit_dynamic_plan"),
            "fracture_response": case_object.get("fracture_response"),
            "visual_material_path": case_object.get("visual_material_path"),
            "intact_visual_material_path": case_object.get("intact_visual_material_path"),
            "intact_visual_scale": case_object.get("intact_visual_scale"),
        }
        role = str(binding.get("role") or "").casefold()
        runtime_scale = scale_for_binding(binding)
        if (
            canonical_capability_id(str(case_spec.get("capability_id") or "")) == "constraint_momentum_transfer"
            and role in {"active_chain_driver", "constrained_chain_body"}
        ):
            # A Newton-cradle body is suspended by its declared constraint. It
            # must never be auto-snapped to the nearest horizontal support.
            params["fit_dynamic_plan"] = False
        if role == "elastic_constraint_anchor":
            # UE BasicShapes/Cube is one metre wide. Binding bounds store half extents,
            # so use twice the metre value and bypass generic asset normalization.
            runtime_scale = [max(0.02, float(value) * 2.0) for value in runtime_scale]
            params["preserve_authored_scale"] = True
            params["fit_dynamic_plan"] = False
        if any(token in role for token in ("support", "floor", "ground", "table", "surface")):
            params["support_top_m"] = bounds.get("top_z")
        for key in (
            "release_time_s",
            "release_position_m",
            "release_velocity_m_s",
            "release_angular_velocity_deg_s",
            "hold_position_m",
            "hold_rotation_degrees",
            "generate_solid_material",
            "generated_material_name",
            "color_rgb",
            "roughness",
            "metallic",
            "emissive",
            "fixed_material_color",
        ):
            if case_object.get(key) is not None:
                params[key] = case_object[key]
        visual_path = str(asset.get("ue_path") or "")
        intact_visual_path = str(case_object.get("intact_visual_ue_path") or "")
        if intact_visual_path:
            params["visual_ue5_path"] = intact_visual_path
            params["visual_asset_kind"] = str(case_object.get("intact_visual_asset_kind") or "static_mesh")
        if physics.get("simulate_physics") and "sphere" in collider and is_runtime_mesh_path(visual_path):
            params["visual_ue5_path"] = visual_path
            params["visual_collision_profile"] = "NoCollision"
            params["visual_simulate_physics"] = False
        runtime_physics = {
            "mass_kg": physics.get("mass_kg"),
            "initial_velocity_m_s": list(case_object.get("initial_velocity_m_s") or case_object.get("initial_velocity") or [0.0, 0.0, 0.0]),
            "initial_angular_velocity_rad_s": list(case_object.get("initial_angular_velocity_rad_s") or physics.get("initial_angular_velocity_rad_s") or [0.0, 0.0, 0.0]),
            "linear_damping": linear_damping,
            "angular_damping": angular_damping,
            "enable_gravity": case_object.get("enable_gravity", physics.get("enable_gravity")),
            "use_ccd": case_object.get("use_ccd", physics.get("use_ccd")),
            "collision_profile": physics.get("collision_profile"),
            "collider": physics.get("collider"),
            "collision_geometry_source": physics.get("collision_geometry_source"),
            "collision_geometry_verification": physics.get("collision_geometry_verification"),
            "material": physics.get("material"),
            "static_friction": material.get("static_friction"),
            "dynamic_friction": material.get("dynamic_friction"),
            "restitution": material.get("restitution"),
            "simulate_physics": bool(physics.get("simulate_physics")),
            "kinematic": bool(physics.get("kinematic")),
        }
        runtime_path = ue_path_for_binding(binding)
        runtime_kind = "static_mesh" if runtime_path.startswith("/Engine/BasicShapes/") else runtime_asset_kind(asset.get("asset_kind"))
        actor = runtime_object(
            object_id,
            runtime_path,
            "llm_rigid_body" if physics.get("simulate_physics") else "llm_static_body",
            list(transform.get("position_m") or case_object.get("initial_position_m") or case_object.get("position_m") or [0.0, 0.0, 0.0]),
            runtime_scale,
            runtime_physics,
            params,
            asset_kind=runtime_kind,
        )
        actor["rotation_degrees"] = list(transform.get("rotation_deg") or [0.0, 0.0, 0.0])
        if physics.get("simulate_physics"):
            dynamic.append(actor)
        else:
            static.append(actor)
    if canonical_capability_id(str(case_spec.get("capability_id") or "")) == "elastic_constraint_rebound":
        expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
        anchor_id = str(expected.get("anchor_object_id") or "anchor")
        body_id = str(expected.get("constrained_object_id") or "payload")
        anchor_position = list((by_case_object.get(anchor_id) or {}).get("initial_position_m") or [0.0, 0.0, 2.0])
        body_position = list((by_case_object.get(body_id) or {}).get("initial_position_m") or [0.0, 0.0, 1.0])
        midpoint = [(float(anchor_position[index]) + float(body_position[index])) / 2.0 for index in range(3)]
        distance = sum((float(anchor_position[index]) - float(body_position[index])) ** 2 for index in range(3)) ** 0.5
        static.append(
            runtime_object(
                "elastic_tether_visual",
                "/Engine/BasicShapes/Cube.Cube",
                "elastic_tether_visual",
                midpoint,
                [0.025, 0.025, max(distance, 0.05)],
                {"simulate_physics": "force_off", "collision_profile": "NoCollision"},
                {
                    "anchor_id": anchor_id,
                    "body_id": body_id,
                    "desired_extent_cm": max(distance * 100.0, 5.0),
                    "color_rgb": [0.92, 0.58, 0.08],
                    "material": "elastic tether",
                },
            )
        )
    elif canonical_capability_id(str(case_spec.get("capability_id") or "")) == "constraint_momentum_transfer":
        expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
        for index, body_id in enumerate(expected.get("chain_objects") or []):
            body_id = str(body_id)
            anchor_id = str((by_case_object.get(body_id) or {}).get("constraint_anchor_id") or f"anchor_{index}")
            anchor_position = list((by_case_object.get(anchor_id) or {}).get("initial_position_m") or [index * 0.161, 0.0, 1.25])
            body_position = list((by_case_object.get(body_id) or {}).get("initial_position_m") or [index * 0.161, 0.0, 0.65])
            midpoint = [(float(anchor_position[axis]) + float(body_position[axis])) / 2.0 for axis in range(3)]
            distance = sum((float(anchor_position[axis]) - float(body_position[axis])) ** 2 for axis in range(3)) ** 0.5
            static.append(
                runtime_object(
                    f"elastic_tether_visual_{body_id}",
                    "/Engine/BasicShapes/Cube.Cube",
                    "elastic_tether_visual",
                    midpoint,
                    [0.012, 0.012, max(distance, 0.05)],
                    {"simulate_physics": "force_off", "collision_profile": "NoCollision"},
                    {
                        "anchor_id": anchor_id,
                        "body_id": body_id,
                        "desired_extent_cm": max(distance * 100.0, 5.0),
                        "color_rgb": [0.12, 0.12, 0.14],
                        "material": "Newton cradle tether",
                    },
                )
            )
    return dynamic, static


def ue_path_for_binding(binding: dict[str, Any]) -> str:
    asset = binding.get("asset") if isinstance(binding.get("asset"), dict) else {}
    ue_path = str(asset.get("ue_path") or "")
    physics = binding.get("physics") if isinstance(binding.get("physics"), dict) else {}
    collider = str(physics.get("collider") or "").casefold()
    collision_geometry_source = str(physics.get("collision_geometry_source") or "").casefold()
    role = str(binding.get("role") or "").casefold()
    if collision_geometry_source == "analytic_sphere":
        return "/Engine/BasicShapes/Sphere.Sphere"
    if collision_geometry_source == "analytic_box":
        return "/Engine/BasicShapes/Cube.Cube"
    if role == "elastic_constrained_body":
        return "/Engine/BasicShapes/Sphere.Sphere"
    if role == "elastic_constraint_anchor":
        return "/Engine/BasicShapes/Cube.Cube"
    if is_runtime_mesh_path(ue_path):
        return ue_path
    if "sphere" in collider or "ball" in role:
        return "/Engine/BasicShapes/Sphere.Sphere"
    if "cylinder" in collider or "pin" in role:
        return "/Engine/BasicShapes/Cylinder.Cylinder"
    if "capsule" in collider or "domino" in role:
        return "/Engine/BasicShapes/Cube.Cube"
    return "/Engine/BasicShapes/Cube.Cube"


def is_runtime_mesh_path(ue_path: str) -> bool:
    if not ue_path:
        return False
    if ue_path.startswith("/Script/"):
        return False
    if "." not in ue_path:
        return False
    return True


def scale_for_binding(binding: dict[str, Any]) -> list[float]:
    asset = binding.get("asset") if isinstance(binding.get("asset"), dict) else {}
    if asset.get("preserve_authored_scale"):
        return [1.0, 1.0, 1.0]
    bounds = binding.get("bounds") if isinstance(binding.get("bounds"), dict) else {}
    extents = bounds.get("extents_m")
    if isinstance(extents, list) and len(extents) >= 3:
        return [float(extents[0]), float(extents[1]), float(extents[2])]
    return [0.25, 0.25, 0.25]


def desired_extent_cm_for_binding(binding: dict[str, Any]) -> float | None:
    bounds = binding.get("bounds") if isinstance(binding.get("bounds"), dict) else {}
    extents = bounds.get("extents_m")
    if isinstance(extents, list) and len(extents) >= 3:
        return max(float(value) for value in extents[:3]) * 100.0
    return None


def summarize_actor_placement(actor_placement: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(actor_placement, dict) or not actor_placement.get("actor_bindings"):
        return {"available": False, "actor_count": 0}
    summary = actor_placement.get("placement_summary") if isinstance(actor_placement.get("placement_summary"), dict) else {}
    return {
        "available": True,
        "schema_version": actor_placement.get("schema_version"),
        "actor_count": summary.get("actor_count", len(actor_placement.get("actor_bindings") or [])),
        "physics_critical_count": summary.get("physics_critical_count"),
        "simulated_actor_count": summary.get("simulated_actor_count"),
        "camera_count": summary.get("camera_count"),
    }


def billiards_objects(case_spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dynamic = []
    objects = case_spec.get("objects") or []
    if not objects:
        objects = [
            {"object_id": "cue_ball", "role": "active", "initial_position": [-1.0, 0.0, 0.42], "initial_velocity": [1.2, 0.0, 0.0]},
            {"object_id": "target_ball_01", "role": "passive", "initial_position": [0.0, 0.0, 0.42], "initial_velocity": [0.0, 0.0, 0.0]},
        ]
    for index, obj in enumerate(objects):
        oid = str(obj.get("object_id") or obj.get("id") or f"ball_{index:02d}")
        pos = list(obj.get("initial_position_m") or obj.get("initial_position") or obj.get("position") or [index * 0.22, 0.0, 0.42])
        vel = list(obj.get("initial_velocity_m_s") or obj.get("initial_velocity") or [0.0, 0.0, 0.0])
        dynamic.append(
            runtime_object(
                oid,
                "/Engine/BasicShapes/Sphere.Sphere",
                "llm_rigid_body",
                pos,
                [0.09, 0.09, 0.09],
                {
                    "mass_kg": 0.17,
                    "radius_m": 0.09,
                    "initial_velocity_m_s": vel,
                    "linear_damping": 0.08,
                    "angular_damping": 0.12,
                    "restitution": 0.86,
                    "simulate_physics": True,
                },
                {"material": "white" if "cue" in oid else "colored billiard ball"},
            )
        )
    static = [
        runtime_object(
            "billiards_tabletop",
            "/Engine/BasicShapes/Cube.Cube",
            "llm_static_body",
            [0.0, 0.0, 0.32],
            [2.8, 1.45, 0.08],
            {"mass_kg": 100.0, "friction": 0.055, "restitution": 0.15, "simulate_physics": "force_off"},
            {"material": "green felt", "color_rgb": [0.03, 0.32, 0.18]},
        )
    ]
    return dynamic, static


def domino_objects(case_spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dynamic = []
    objects = case_spec.get("objects") or []
    if not objects:
        objects = [{"object_id": f"domino_{idx:02d}", "initial_position": [idx * 0.22, 0.0, 0.45]} for idx in range(5)]
    for index, obj in enumerate(objects):
        oid = str(obj.get("object_id") or obj.get("id") or f"domino_{index:02d}")
        vel = [0.75, 0.0, 0.0] if index == 0 else [0.0, 0.0, 0.0]
        dynamic.append(
            runtime_object(
                oid,
                "/Engine/BasicShapes/Cube.Cube",
                "llm_rigid_body",
                list(obj.get("initial_position_m") or obj.get("initial_position") or [index * 0.22, 0.0, 0.45]),
                [0.045, 0.16, 0.42],
                {
                    "mass_kg": 0.08,
                    "initial_velocity_m_s": list(obj.get("initial_velocity_m_s") or obj.get("initial_velocity") or vel),
                    "linear_damping": 0.03,
                    "angular_damping": 0.02,
                    "restitution": 0.35,
                    "simulate_physics": True,
                },
                {"material": "domino"},
            )
        )
    static = [
        runtime_object("domino_floor", "/Engine/BasicShapes/Cube.Cube", "llm_static_body", [0.45, 0.0, 0.03], [2.2, 0.8, 0.05], {"simulate_physics": "force_off"}, {"material": "matte floor"})
    ]
    return dynamic, static


def falling_objects(case_spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dynamic = []
    objects = case_spec.get("objects") or []
    static_roles = {"static", "support", "floor", "ground", "landing_surface"}
    dynamic_specs = [obj for obj in objects if str(obj.get("role") or "").lower() not in static_roles]
    if not dynamic_specs:
        dynamic_specs = [{"object_id": "falling_block", "initial_position": [0.0, 0.0, 1.25], "initial_velocity": [0.0, 0.0, 0.0]}]
    for index, obj in enumerate(dynamic_specs):
        dynamic.append(
            runtime_object(
                str(obj.get("object_id") or obj.get("id") or f"falling_block_{index}"),
                "/Engine/BasicShapes/Cube.Cube",
                "falling_collision",
                list(obj.get("initial_position_m") or obj.get("initial_position") or [0.0, 0.0, 1.25 + index * 0.25]),
                [0.28, 0.28, 0.28],
                {
                    "mass_kg": 1.0,
                    "initial_velocity_m_s": list(obj.get("initial_velocity_m_s") or obj.get("initial_velocity") or [0.0, 0.0, 0.0]),
                    "desired_extent_cm": 28.0,
                    "linear_damping": 0.05,
                    "angular_damping": 0.08,
                    "restitution": 0.25,
                    "simulate_physics": True,
                },
                {"material": "falling cube"},
            )
        )
    static = [
        runtime_object("landing_pad", "/Engine/BasicShapes/Cube.Cube", "landing_surface", [0.0, 0.0, 0.03], [1.6, 1.2, 0.05], {"simulate_physics": "force_off"}, {"material": "matte floor"})
    ]
    return dynamic, static


def generic_objects(case_spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return falling_objects(case_spec)


def runtime_object(
    object_id: str,
    ue5_path: str,
    behavior: str,
    position: list[float],
    scale: list[float],
    physics: dict[str, Any],
    params: dict[str, Any],
    *,
    asset_kind: str = "static_mesh",
) -> dict[str, Any]:
    return {
        "id": object_id,
        "asset_key": object_id,
        "asset_name": object_id,
        "ue5_path": ue5_path,
        "category_l1": "harness",
        "category_l2": "physics",
        "class_name": {
            "blueprint": "Blueprint",
            "geometry_collection": "GeometryCollection",
            "skeletal_mesh": "SkeletalMesh",
        }.get(asset_kind, "StaticMesh"),
        "asset_kind": asset_kind,
        "render_usage": f"runtime_{asset_kind}",
        "runtime_spawnable": True,
        "behavior": behavior,
        "initial_position_m": position,
        "scale": scale,
        "physics_properties": physics,
        "params": params,
    }


def runtime_asset_kind(value: Any) -> str:
    normalized = str(value or "static_mesh").strip().casefold().replace(" ", "_")
    return {
        "blueprint": "blueprint",
        "skeletalmesh": "skeletal_mesh",
        "skeletal_mesh": "skeletal_mesh",
        "geometrycollection": "geometry_collection",
        "geometry_collection": "geometry_collection",
        "staticmesh": "static_mesh",
        "static_mesh": "static_mesh",
    }.get(normalized, "static_mesh")


def native_case_type(case_spec: dict[str, Any]) -> str:
    capability = canonical_capability_id(str(case_spec.get("capability_id") or ""))
    if capability == "rigid_body_contact_causality":
        return "llm_object_graph"
    if capability == "sequential_contact_propagation":
        return "bottle_domino_chain"
    if capability == "rigid_body_gravity_collision":
        return "falling_crate_collision"
    return "llm_object_graph"


def duration_for_case(case_spec: dict[str, Any]) -> float:
    if os.environ.get("SIM_STUDIO_UE_DURATION"):
        return max(1.0, min(12.0, float(os.environ["SIM_STUDIO_UE_DURATION"])))
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    event_window_s = None
    for key in ("duration_s", "duration"):
        if expected.get(key) is not None:
            event_window_s = float(expected[key])
            break
    scene = case_spec.get("scene") if isinstance(case_spec.get("scene"), dict) else {}
    if event_window_s is None and scene.get("duration_s") is not None:
        event_window_s = float(scene["duration_s"])
    if event_window_s is None:
        event_window_s = 4.0
    event_window_s = max(1.0, min(12.0, event_window_s))
    return max(1.0, min(12.0, event_window_s + post_event_tail_for_case(case_spec)))


def post_event_tail_for_case(case_spec: dict[str, Any]) -> float:
    """Time reserved for the autonomous simulation to settle after its event window."""
    if os.environ.get("SIM_STUDIO_UE_DURATION"):
        return 0.0
    if os.environ.get("SIM_STUDIO_UE_POST_EVENT_TAIL_S") is not None:
        return max(0.0, min(3.0, float(os.environ["SIM_STUDIO_UE_POST_EVENT_TAIL_S"])))
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    scene = case_spec.get("scene") if isinstance(case_spec.get("scene"), dict) else {}
    value = expected.get("post_event_tail_s", scene.get("post_event_tail_s", 1.0))
    return max(0.0, min(3.0, float(value)))


def default_physics_controls(case_spec: dict[str, Any]) -> dict[str, Any]:
    parameters = case_spec.get("physical_parameters") if isinstance(case_spec.get("physical_parameters"), dict) else {}
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    contract = expected.get("simulation_contract") if isinstance(expected.get("simulation_contract"), dict) else {}
    return {
        "schema_version": "physics_controls_v1",
        "gravity_enabled": parameters.get("gravity_enabled", True) is not False,
        "gravity_m_s2": parameters.get("gravity_m_s2") or [0.0, 0.0, -9.81],
        "collision_enabled": True,
        "collision_focus": True,
        "rigid_body_setup_enabled": True,
        "simulate_physics": True,
        "simulation_driver": "adp_cpp_runtime_driver",
        "runtime_driver_backend": "cpp_runtime_driver",
        "cpp_runtime_driver_enabled": True,
        "dynamic_collision_profile": "PhysicsActor",
        "static_collision_profile": "BlockAll",
        "apply_mass": True,
        "apply_damping": True,
        "apply_physical_material": True,
        "apply_initial_impulse": True,
        "initial_impulse_start_frame": 0,
        "record_contact_events": True,
        "deterministic_replay_fallback": False,
        "harness_capability_id": case_spec.get("capability_id"),
        "input_mode": contract.get("input_mode"),
        "state_solver": contract.get("state_solver"),
        "trajectory_role": contract.get("trajectory_role"),
    }


def default_lighting_controls(pass_mode: str = "data", case_spec: dict[str, Any] | None = None) -> dict[str, Any]:
    scene = case_spec.get("scene") if isinstance(case_spec, dict) and isinstance(case_spec.get("scene"), dict) else {}
    requested_preset = os.environ.get("SIM_STUDIO_UE_LIGHTING_PRESET") or scene.get("lighting_preset") or ("data_neutral" if pass_mode == "data" else "map_lights_balanced_fill")
    requested_preset = str(requested_preset)
    controls = {
        "preset": requested_preset,
        "visual_realism_profile": os.environ.get("SIM_STUDIO_UE_VISUAL_REALISM_PROFILE", "data_neutral" if pass_mode == "data" else "editor_parity"),
        "use_existing_map_lights": True,
        "spawn_directional_sun": True,
        "spawn_fill_light": True,
        "spawn_sky_light": True,
        "spawn_map_boost_lights": False,
        "spawn_sky_atmosphere": True,
        "use_post_process": True,
        "fixed_auto_exposure": True,
        "stage_helpers": True,
        "map_backdrop_helpers": False,
        "capture_backend": os.environ.get("SIM_STUDIO_UE_CAPTURE_BACKEND", "scene_capture"),
        "capture_source": "SCS_FINAL_COLOR_LDR",
        "video_filter": "",
    }
    if pass_mode == "rgb":
        rgb_backend = os.environ.get("SIM_STUDIO_UE_RGB_CAPTURE_BACKEND", "highres_viewport")
        controls.update(
            {
                "preset": "harness_rgb_editor_viewport",
                "spawn_directional_sun": rgb_backend != "highres_viewport",
                "spawn_fill_light": rgb_backend != "highres_viewport",
                "spawn_sky_light": rgb_backend != "highres_viewport",
                "fixed_auto_exposure": rgb_backend != "highres_viewport",
                "capture_backend": rgb_backend,
                "video_filter": "",
            }
        )
    elif pass_mode == "data":
        data_profiles = {
            "data_neutral": (18.0, 6.0, 3.2, 0.75),
            "fixed_exposure_data_neutral": (18.0, 6.0, 3.2, 0.75),
            "map_lights_balanced_fill": (48.0, 16.0, 4.0, 1.05),
            "cinematic_subject_key_fill": (72.0, 12.0, 3.6, 0.90),
        }
        sun, fill, sky, exposure = data_profiles.get(requested_preset, data_profiles["data_neutral"])
        controls.update(
            {
                "preset": requested_preset,
                "capture_backend": "scene_capture",
                "fixed_auto_exposure": True,
                "sun_intensity": float(os.environ.get("SIM_STUDIO_UE_SUN_INTENSITY", sun)),
                "fill_intensity": float(os.environ.get("SIM_STUDIO_UE_FILL_INTENSITY", fill)),
                "sky_intensity": float(os.environ.get("SIM_STUDIO_UE_SKY_INTENSITY", sky)),
                "exposure_bias": float(os.environ.get("SIM_STUDIO_UE_EXPOSURE_BIAS", exposure)),
            }
        )
    return controls


def build_render_config(args: argparse.Namespace, case_spec: dict[str, Any], camera_plan: dict[str, Any]) -> dict[str, Any]:
    timebase = timebase_for_case(case_spec)
    fps = int(timebase["render_fps"])
    return {
        "schema_version": "render_config.v2.3",
        "mode": args.mode,
        "backend": "ue",
        "ue_renderer_only": True,
        "seed": int(os.environ.get("SIM_STUDIO_SEED", case_spec.get("seed") or "0")),
        "width": int(os.environ.get("SIM_STUDIO_UE_WIDTH", "1920")),
        "height": int(os.environ.get("SIM_STUDIO_UE_HEIGHT", "1080")),
        "fps": fps,
        "timebase": timebase,
        "duration_s": duration_for_case(case_spec),
        "views": [str(view.get("camera_id")) for view in camera_plan.get("views", []) if isinstance(view, dict) and view.get("camera_id")],
        "passes": [item.strip() for item in args.passes.split(",") if item.strip()],
        "execution_strategy": (
            "single_process_shared_solver_multimodal"
            if args.mode == "both"
            and bool(fracture_object_ids(case_spec))
            and os.environ.get("SIM_STUDIO_UE_SINGLE_PASS_FRACTURE", "1") != "0"
            else "separate_render_passes"
        ),
        "rgb_pass": {
            "capture_backend": os.environ.get("SIM_STUDIO_UE_RGB_CAPTURE_BACKEND", "highres_viewport"),
            "deterministic": True,
            "physics_enabled": True,
        },
        "data_pass": {
            "capture_backend": "scene_capture",
            "deterministic": True,
            "physics_enabled": True,
            "depth_source": "ue",
            "mask_source": "ue_instance_material_mask",
        },
    }


def timebase_for_case(case_spec: dict[str, Any]) -> dict[str, Any]:
    requested = case_spec.get("timebase") if isinstance(case_spec.get("timebase"), dict) else {}
    render_fps = int(os.environ.get("SIM_STUDIO_UE_FPS", requested.get("render_fps") or 24))
    physics_hz = int(os.environ.get("SIM_STUDIO_UE_PHYSICS_HZ", requested.get("physics_hz") or render_fps))
    duration_s = duration_for_case(case_spec)
    timebase = build_timebase(duration_s=duration_s, physics_hz=physics_hz, render_fps=render_fps)
    full_solver_frame_count = int(timebase["solver_frame_count"])
    timebase["physics_step_count"] = full_solver_frame_count - 1
    timebase["full_solver_frame_count"] = full_solver_frame_count
    timebase["solver_frame_count"] = int(timebase["canonical_frame_count"])
    timebase["solver_capture_mode"] = "render_boundary"
    timebase["raw_capture_frame_count"] = timebase["canonical_frame_count"]
    tail_s = post_event_tail_for_case(case_spec)
    timebase["post_event_tail_s"] = tail_s
    timebase["event_window_duration_s"] = round(max(0.0, duration_s - tail_s), 8)
    return timebase


def pass_sequence(
    mode: str,
    *,
    live_solver: bool = False,
    live_fracture: bool = False,
    single_pass_fracture: bool = False,
) -> list[str]:
    if mode == "both" and live_solver and live_fracture and single_pass_fracture:
        return ["combined"]
    return ["rgb", "data"] if mode == "both" or (mode == "data" and live_solver) else [mode]


def native_output_dir(run_dir: Path, pass_mode: str, requested_mode: str) -> Path:
    if pass_mode == "combined":
        return run_dir / "logs" / "native_combined"
    if requested_mode == "both":
        return run_dir / "logs" / f"native_{pass_mode}"
    if pass_mode == "rgb":
        return run_dir / "logs" / "native_rgb"
    if pass_mode == "data":
        return run_dir / "logs" / "native_data"
    return run_dir / "ue_native_output"


def run_native_pass(
    command: list[str],
    *,
    args: argparse.Namespace,
    case_spec: dict[str, Any],
    runtime_scene: dict[str, Any],
    studio_scene_path: Path,
    runtime_scene_path: Path,
    native_output: Path,
    pass_mode: str,
) -> dict[str, Any]:
    env = os.environ.copy()
    precomputed = (runtime_scene.get("physics_controls") or {}).get("runtime_driver_backend") == "precomputed_trajectory"
    live_rgb_solver = pass_mode in {"rgb", "combined"} and not precomputed
    env.update(
        {
            "OUTPUT_DIR": str(native_output),
            "SCENE_SPEC": str(studio_scene_path),
            "SCENE_RUNTIME_JSON": str(runtime_scene_path),
            "ASSET_DATABASE_ONLY_ASSETS": "0",
            "GITLAB_ONLY_ASSETS": "0",
            "SCENE_MAP": args.map or "/Game/Maps/MarketEnvironment/Maps/Day",
            "SCENE_DESCRIPTION": str(case_spec.get("prompt") or case_spec.get("case_id") or "Harness UE render"),
            "ASSET_ROOT": str((ROOT / "assets").resolve()),
            "WIDTH": os.environ.get("SIM_STUDIO_UE_WIDTH", "1920"),
            "HEIGHT": os.environ.get("SIM_STUDIO_UE_HEIGHT", "1080"),
            "FPS": os.environ.get("SIM_STUDIO_UE_FPS", "24"),
            "DURATION": str(runtime_scene["simulation"]["duration_s"]),
            "MULTI_VIEW": "1",
            "CANONICAL_MULTI_VIEW": "1",
            "RENDER_DATA_PASSES": "1" if pass_mode in {"data", "combined"} else "0",
            "AUDIO_PASS_ENABLED": "0",
            "KEEP_RENDER_FRAMES": os.environ.get("SIM_STUDIO_KEEP_RENDER_FRAMES", "0"),
            "CHAOS_RIGID_BODY_SETUP": "1",
            "CHAOS_SIMULATION_ENABLED": "0" if precomputed else "1",
            "RENDER_QUALITY_PRESET": os.environ.get("SIM_STUDIO_UE_RENDER_QUALITY", "medium"),
            "VISUAL_REALISM_PROFILE": os.environ.get("SIM_STUDIO_UE_VISUAL_REALISM_PROFILE", "editor_parity"),
            "RENDER_VIEWPORT_SETTLE_SECONDS": os.environ.get("SIM_STUDIO_UE_VIEWPORT_SETTLE_SECONDS", "0.5"),
            "RENDER_WARMUP_FRAMES": os.environ.get("SIM_STUDIO_UE_WARMUP_FRAMES", "2"),
            "RENDER_FIRST_FRAME_STABILITY_SAMPLES": os.environ.get("SIM_STUDIO_UE_FIRST_FRAME_STABILITY_SAMPLES", "1"),
            "RENDER_SCREENSHOT_STABLE_TICKS": os.environ.get("SIM_STUDIO_UE_SCREENSHOT_STABLE_TICKS", "1"),
            "VIDEO_CRF": os.environ.get("SIM_STUDIO_UE_VIDEO_CRF", "18"),
            "VIDEO_PRESET": os.environ.get("SIM_STUDIO_UE_VIDEO_PRESET", "slow"),
            "SIM_STUDIO_UE_CAPTURE_BACKEND": "highres_viewport" if live_rgb_solver else (os.environ.get("SIM_STUDIO_UE_RGB_CAPTURE_BACKEND", "highres_viewport") if pass_mode == "rgb" else "scene_capture"),
            "WORLD_MODEL_RENDER_PASS_MODE": "both" if pass_mode == "combined" else pass_mode,
            "SIM_STUDIO_SEED": str(os.environ.get("SIM_STUDIO_SEED", case_spec.get("seed") or "0")),
        }
    )
    return run_ue_until_artifacts(
        command,
        env=env,
        native_output=native_output,
        timeout=int(os.environ.get("SIM_STUDIO_UE_TIMEOUT_SECONDS", "1800")),
    )


def append_runner_log(run_dir: Path, pass_mode: str, proc_result: dict[str, Any]) -> None:
    write_json(run_dir / "logs" / f"runner_stdout_{pass_mode}.json", {"pass_mode": pass_mode, "stdout": proc_result.get("stdout", "")})
    write_json(run_dir / "logs" / f"runner_stderr_{pass_mode}.json", {"pass_mode": pass_mode, "stderr": proc_result.get("stderr", "")})
    if pass_mode in {"data", "combined"}:
        write_json(run_dir / "runner_stdout.json", {"stdout": proc_result.get("stdout", "")})
        write_json(run_dir / "runner_stderr.json", {"stderr": proc_result.get("stderr", "")})


def classify_native_pass_failure(native_output: Path, pass_mode: str) -> dict[str, str]:
    summary_path = native_output / "summary.json"
    summary = read_optional_json(summary_path)
    errors = summary.get("errors") if isinstance(summary.get("errors"), list) else []
    error_text = "\n".join(str(item.get("error") or item) for item in errors if isinstance(item, dict) or item)
    process_text = "\n".join(
        [
            error_text,
            read_text_tail(native_output / "ue_process_stdout.log", limit=20000),
            read_text_tail(native_output / "ue_process_stderr.log", limit=20000),
        ]
    )
    if "ADPPhysicsRuntime" in process_text and ("Unable to find module" in process_text or "无法找到模块" in process_text or "插件" in process_text):
        return {
            "failure_code": "F_UE_PLUGIN_MODULE_MISSING",
            "failure_message": "UE project could not load the ADPPhysicsRuntime plugin module. Build the UE template/plugin for the configured engine version before rendering.",
        }
    if "failed to load runtime material-library asset" in process_text:
        return {
            "failure_code": "F_ASSET_RUNTIME_BINDING_INVALID",
            "failure_message": "UE runner tried to load a non-mesh class or invalid asset path as a renderable runtime asset. Keep Blueprint/class paths separate from StaticMesh asset paths.",
        }
    if pass_mode in {"rgb", "combined"} and "timeout waiting for" in error_text and "frames/frame_" in error_text:
        return {
            "failure_code": "F_RGB_HIGHRES_VIEWPORT_UNAVAILABLE",
            "failure_message": "RGB highres viewport capture did not produce screenshot frames in this UE launch mode.",
        }
    if pass_mode in {"rgb", "combined"} and not (native_output / "preview.mp4").exists():
        return {
            "failure_code": "F_RGB_PASS_MISSING",
            "failure_message": "RGB pass completed without a preview/video artifact.",
        }
    if pass_mode in {"data", "combined"}:
        manifest = read_optional_json(native_output / "render_pass_manifest.json")
        passes = manifest.get("passes") if isinstance(manifest.get("passes"), dict) else {}
        if (passes.get("depth") or {}).get("status") != "available":
            return {
                "failure_code": "F_DEPTH_MISSING",
                "failure_message": "DATA pass did not export UE depth.",
            }
        if (passes.get("segmentation") or {}).get("status") != "available":
            return {
                "failure_code": "F_VIEW_MISMATCH",
                "failure_message": "DATA pass did not export instance segmentation.",
            }
    return {
        "failure_code": "F6_RUNTIME_OR_RENDER_FAILURE",
        "failure_message": "",
    }


def camera_runtime_from_plan(camera_plan: dict[str, Any]) -> dict[str, Any]:
    views = camera_plan.get("views") if isinstance(camera_plan.get("views"), list) else []
    if not views:
        return {"mode": "fixed"}
    first = views[0]
    position = first.get("location") or first.get("position_m") or [-4.0, -5.0, 2.4]
    target = first.get("target") or first.get("target_m") or [0.0, 0.0, 0.5]
    return {
        "mode": "fixed",
        "coordinate_frame": "world",
        "movement": "static",
        "camera_id": first.get("camera_id"),
        "fov": first.get("fov"),
        "near_clip": first.get("near_clip"),
        "far_clip": first.get("far_clip"),
        "views": [
            {
                "camera_id": view.get("camera_id"),
                "role": view.get("role"),
                "location": view.get("location"),
                "target": view.get("target"),
                "fov": view.get("fov"),
                "near_clip": view.get("near_clip"),
                "far_clip": view.get("far_clip"),
            }
            for view in views
            if isinstance(view, dict) and view.get("camera_id")
        ],
        "preview_waypoints": [
            {"time_s": 0, "position_m": position, "target_offset_m": target, "fov": first.get("fov")},
            {"time_s": duration_from_camera_plan(camera_plan), "position_m": position, "target_offset_m": target, "fov": first.get("fov")},
        ],
    }


def duration_from_camera_plan(camera_plan: dict[str, Any]) -> float:
    return 4.0


def standardize_native_output(
    run_dir: Path,
    native_output: Path,
    camera_plan: dict[str, Any],
    started: float,
    *,
    render_mode: str = "data",
    rgb_native_output: Path | None = None,
    render_config: dict[str, Any] | None = None,
    case_spec: dict[str, Any] | None = None,
    scene_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary_path = native_output / "summary.json"
    trajectory_path = native_output / "trajectory.json"
    data_camera_tracks_path = native_output / "camera_trajectories.json"
    data_render_manifest_path = native_output / "render_pass_manifest.json"
    rgb_source = rgb_native_output or native_output
    rgb_summary_path = rgb_source / "summary.json"
    rgb_camera_tracks_path = rgb_source / "camera_trajectories.json"
    rgb_render_manifest_path = rgb_source / "render_pass_manifest.json"
    required_paths = (
        summary_path,
        trajectory_path,
        data_camera_tracks_path,
        data_render_manifest_path,
        rgb_summary_path,
        rgb_camera_tracks_path,
        rgb_render_manifest_path,
    )
    missing = [str(path) for path in dict.fromkeys(required_paths) if not path.exists() or path.stat().st_size == 0]
    if missing:
        return fail_report("F6_RUNTIME_OR_RENDER_FAILURE", f"Native UE output missing: {missing}")
    shutil.copyfile(trajectory_path, run_dir / "trajectory.json")
    fracture_events_path = rgb_source / "fracture_events.json"
    if fracture_events_path.is_file():
        shutil.copyfile(fracture_events_path, run_dir / "fracture_events.json")
    fragment_trajectory_path = rgb_source / "fragment_trajectory.json"
    if fragment_trajectory_path.is_file():
        shutil.copyfile(fragment_trajectory_path, run_dir / "fragment_trajectory.json")
    contacts = extract_contacts(read_json(trajectory_path))
    write_json(run_dir / "contact_events.json", contacts)
    shutil.copyfile(rgb_camera_tracks_path, run_dir / "camera_trajectory.json")

    camera_ids = [str(view.get("camera_id")) for view in camera_plan.get("views", []) if view.get("camera_id")]
    if not camera_ids:
        camera_ids = ["overview"]
    native_summary = read_json(summary_path)
    rgb_summary = read_json(rgb_summary_path)
    rgb_fracture_path = rgb_source / "fracture_events.json"
    data_fracture_path = native_output / "fracture_events.json"
    rgb_fractures = read_json(rgb_fracture_path) if rgb_fracture_path.is_file() else []
    data_fractures = rgb_fractures if rgb_fracture_path.resolve() == data_fracture_path.resolve() else (read_json(data_fracture_path) if data_fracture_path.is_file() else [])
    rgb_fractures = rgb_fractures if isinstance(rgb_fractures, list) else []
    data_fractures = data_fractures if isinstance(data_fractures, list) else []
    fracture_sensor_report = build_fracture_sensor_state_report(case_spec or {}, render_mode, rgb_fractures, data_fractures)
    fracture_sensor_state_ready = fracture_sensor_report["status"] == "pass"
    write_json(run_dir / "fracture_sensor_state_report.json", fracture_sensor_report)
    native_manifest = read_json(data_render_manifest_path)
    rgb_manifest = read_json(rgb_render_manifest_path)
    native_camera_trajectory = read_json(rgb_camera_tracks_path)
    native_passes = native_manifest.get("passes") if isinstance(native_manifest.get("passes"), dict) else {}
    rgb_passes = rgb_manifest.get("passes") if isinstance(rgb_manifest.get("passes"), dict) else {}
    write_json(
        run_dir / "render_manifest.json",
        {
            **native_manifest,
            "passes": {**native_passes, "rgb": rgb_passes.get("rgb") or {"status": "missing", "views": []}},
        },
    )
    selected_map = native_summary.get("selected_map") or {}
    requested_map_asset = ((scene_spec or {}).get("background") or {}).get("ue5_path")
    requested_map = canonical_game_package(requested_map_asset)
    opened_map = canonical_game_package(selected_map.get("opened_package") or selected_map.get("path"))
    actor_count = int(native_summary.get("loaded_map_actor_count") or 0)
    map_failures = []
    if not selected_map.get("opened"):
        map_failures.append("F_MAP_NOT_OPENED")
    if requested_map and opened_map != requested_map:
        map_failures.append("F_MAP_PACKAGE_MISMATCH")
    if actor_count < 1:
        map_failures.append("F_MAP_EMPTY")
    write_json(
        run_dir / "map_report.json",
        {
            "schema_version": "harness_map_report_v1",
            "status": "fail" if map_failures else "pass",
            "failure_codes": map_failures,
            "requested_package": requested_map,
            "requested_asset_path": requested_map_asset,
            "opened_package": opened_map,
            "opened": bool(selected_map.get("opened")),
            "dependency_policy": "runtime_load_success",
            "loaded_actor_count": actor_count,
            "visible_actor_count": int((native_summary.get("visible_map_actors") or {}).get("actors") or 0),
            "visible_component_count": int((native_summary.get("visible_map_actors") or {}).get("components") or 0),
            "scene_origin_cm": native_summary.get("scene_origin") or [],
            "render_bounds_cm": native_summary.get("camera_pose") or {},
            "available_lights": ((native_summary.get("lighting") or {}).get("existing_map_lights") or {}),
            "camera_ids": [str(view.get("camera_id")) for view in camera_plan.get("views", []) if view.get("camera_id")],
        },
    )
    native_rgb_views = [
        view
        for view in (((rgb_manifest.get("passes") or {}).get("rgb") or {}).get("views") or [])
        if isinstance(view, dict) and view.get("path")
    ]
    primary_rgb_view = native_view_for_camera(camera_ids[0], 0, native_rgb_views)
    primary_rgb_path = Path(str(primary_rgb_view["path"])) if primary_rgb_view.get("path") else None
    root_video = run_dir / "video.mp4"
    if primary_rgb_path and has_mp4_magic(primary_rgb_path):
        link_or_copy(primary_rgb_path, root_video)
    elif root_video.exists():
        root_video.unlink()
    native_depth_views = views_by_id(((native_manifest.get("passes") or {}).get("depth") or {}).get("views") or [])
    native_segmentation_views = views_by_id(((native_manifest.get("passes") or {}).get("segmentation") or {}).get("views") or [])
    frame_count = int(rgb_summary.get("frames") or rgb_manifest.get("frame_count") or 0)
    fps = int(rgb_summary.get("fps") or rgb_manifest.get("fps") or 12)
    width = int(rgb_summary.get("width") or (render_config or {}).get("width") or 0)
    height = int(rgb_summary.get("height") or (render_config or {}).get("height") or 0)
    timestamps = [round(idx / max(1, fps), 6) for idx in range(frame_count)]
    sampling_map = read_optional_json(run_dir / "sampling_map.json")
    timebase = (render_config or {}).get("timebase") or native_summary.get("timebase") or {}
    solver_cache_sha256 = sampling_map.get("solver_cache_sha256")
    planned_views = {str(view.get("camera_id")): view for view in camera_plan.get("views", []) if isinstance(view, dict) and view.get("camera_id")}
    actual_camera_views = camera_views_by_id(native_camera_trajectory)
    sensor_views: list[dict[str, Any]] = []
    rgb_available_by_camera: dict[str, bool] = {}
    for index, camera_id in enumerate(camera_ids):
        view_dir = run_dir / "views" / camera_id
        view_dir.mkdir(parents=True, exist_ok=True)
        native_view = native_view_for_camera(camera_id, index, native_rgb_views)
        native_rgb_path = Path(str(native_view["path"])) if native_view.get("path") else None
        rgb_target = view_dir / "rgb.mp4"
        rgb_available = bool(native_rgb_path and has_mp4_magic(native_rgb_path))
        rgb_available_by_camera[camera_id] = rgb_available
        if rgb_available and native_rgb_path is not None:
            link_or_copy(native_rgb_path, rgb_target)
        elif rgb_target.exists():
            rgb_target.unlink()
        rgb_frame_count = frame_count if rgb_available else 0
        rgb_timestamps = timestamps if rgb_available else []
        depth_copy = copy_native_pass_view(native_depth_views.get(camera_id), view_dir, "depth.exr", "depth_frames")
        segmentation_copy = copy_native_pass_view(native_segmentation_views.get(camera_id), view_dir, "segmentation.exr", "segmentation_frames")
        depth_available = bool(depth_copy.get("available"))
        segmentation_available = bool(segmentation_copy.get("available"))
        depth_frame_count = int(depth_copy.get("frame_count") or 0)
        segmentation_frame_count = int(segmentation_copy.get("frame_count") or 0)
        depth_statistics = depth_pixel_statistics(view_dir / "depth.exr") if depth_available else None
        depth_preview = encode_sensor_preview(view_dir / "depth_frames", view_dir / "depth_preview.mp4", fps=fps, modality="depth")
        segmentation_preview = encode_sensor_preview(view_dir / "segmentation_frames", view_dir / "segmentation_preview.mp4", fps=fps, modality="segmentation")
        segmentation_type = "instance" if segmentation_available else "missing"
        actual_camera = actual_camera_views.get(camera_id) or {}
        camera_frames = actual_camera.get("frames") if isinstance(actual_camera.get("frames"), list) else []
        first_camera_frame = camera_frames[0] if camera_frames else {}
        intrinsics = camera_intrinsics(width, height, first_camera_frame.get("fov"))
        sensor_views.append(
            {
                "camera_id": camera_id,
                "planned": planned_views.get(camera_id),
                "frame_count_rgb": rgb_frame_count,
                "frame_count_depth": depth_frame_count,
                "frame_count_segmentation": segmentation_frame_count,
                "runtime_echo_frame_count": len(camera_frames),
                "runtime_echo_first": first_camera_frame or None,
                "intrinsics": intrinsics,
            }
        )
        write_json(
            view_dir / "meta.json",
            {
                "camera_id": camera_id,
                "source_native_view_id": native_view.get("view_id"),
                "frame_count_rgb": rgb_frame_count,
                "frame_count_depth": depth_frame_count,
                "frame_count_segmentation": segmentation_frame_count,
                "timestamps_rgb": rgb_timestamps,
                "timestamps_depth": timestamps[:depth_frame_count],
                "timestamps_segmentation": timestamps[:segmentation_frame_count],
                "fps": fps,
                "timebase": timebase,
                "solver_cache_sha256": solver_cache_sha256,
                "depth_source": "ue" if depth_available else "missing",
                "depth_unit": depth_copy.get("unit"),
                "depth_type": depth_copy.get("depth_type"),
                "depth_encoding": depth_copy.get("depth_encoding"),
                "depth_stored_value_to_centimeter": depth_copy.get("stored_value_to_centimeter"),
                "depth_unit_validation": depth_copy.get("unit_validation"),
                "depth_variance": float((depth_statistics or {}).get("variance") or 0.0),
                "depth_pixel_statistics": depth_statistics,
                "depth_frames": depth_copy.get("frames", []),
                "depth_preview": "depth_preview.mp4" if depth_preview else None,
                "depth_format_mismatch_count": int(depth_copy.get("format_mismatch_count") or 0),
                "segmentation_type": segmentation_type,
                "instance_level": segmentation_available,
                "segmentation_frames": segmentation_copy.get("frames", []),
                "segmentation_preview": "segmentation_preview.mp4" if segmentation_preview else None,
                "segmentation_format_mismatch_count": int(segmentation_copy.get("format_mismatch_count") or 0),
                "instance_count": int(segmentation_copy.get("instance_count") or 0),
                "instance_mapping": segmentation_copy.get("instance_mapping") or [],
                "segmentation_palette_quantized": bool(segmentation_copy.get("palette_quantized")),
                "segmentation_palette_closure": bool(segmentation_copy.get("palette_closure")),
                "segmentation_palette_rgb8": segmentation_copy.get("palette_rgb8") or [],
                "segmentation_raw_frames": segmentation_copy.get("raw_frames") or [],
                "preview_contract": "physinone_style_derived_mp4_v1",
                "render_time_sec": round(time.perf_counter() - started, 4),
                "native_ue_rgb": rgb_available,
                "native_output": str(native_output),
                "rgb_pass_native_output": str(rgb_source),
                "native_rgb_path": str(native_rgb_path) if native_rgb_path else None,
                "camera_state_source": "ue_runtime_echo" if camera_frames else "missing",
                "camera_trajectory_path": "../../camera_trajectory.json",
                "camera_runtime_frame_count": len(camera_frames),
                "camera_runtime_first": first_camera_frame or None,
                "camera_intrinsics": intrinsics,
            },
        )
    lighting = native_summary.get("lighting") if isinstance(native_summary.get("lighting"), dict) else {}
    write_json(
        run_dir / "sensor_state.json",
        {
            "schema_version": "harness_sensor_state_v1",
            "frame_count": frame_count,
            "fps": fps,
            "simulation_timebase": timebase,
            "solver_cache_sha256": solver_cache_sha256,
            "timebase": native_camera_trajectory.get("timebase") or "frame_index / fps",
            "alignment": "solver snapshot -> camera pose -> rgb/depth/segmentation capture in one UE frame loop",
            "camera_state_source": "ue_runtime_echo",
            "views": sensor_views,
            "depth": {
                "source": "ue_scene_capture",
                "capture_source": first_capture_source(native_depth_views),
                "unit": first_view_value(native_depth_views, "unit"),
                "depth_type": first_view_value(native_depth_views, "depth_type"),
                "depth_encoding": first_view_value(native_depth_views, "depth_encoding"),
                "stored_value_to_centimeter": first_view_value(native_depth_views, "stored_value_to_centimeter"),
                "unit_validation": first_view_value(native_depth_views, "unit_validation"),
            },
            "segmentation": {"source": "ue_instance_material_mask", "instance_level": bool(native_segmentation_views)},
            "lighting": {
                "state_source": "ue_runtime_echo",
                "profile": lighting.get("profile"),
                "sun_intensity": lighting.get("sun_intensity"),
                "fill_intensity": lighting.get("fill_intensity"),
                "sky_intensity": lighting.get("sky_intensity"),
                "exposure_bias": lighting.get("exposure_bias"),
                "fixed_auto_exposure": lighting.get("fixed_auto_exposure"),
                "existing_map_lights": lighting.get("existing_map_lights"),
            },
        },
    )
    if case_spec is not None and scene_spec is not None and render_config is not None:
        ArtifactManager(run_dir).finalize(
            run_id=run_dir.name,
            case_id=str(case_spec.get("case_id") or run_dir.name),
            mode=render_mode,
            seed=int(render_config.get("seed") or 0),
            camera_plan=camera_plan,
            render_config=render_config,
            rgb_video_source=run_dir / "video.mp4",
        )
    if not fracture_sensor_state_ready:
        render_sync_path = run_dir / "render_sync_report.json"
        render_sync = read_optional_json(render_sync_path)
        failure_codes = list(render_sync.get("failure_codes") or [])
        for code in fracture_sensor_report.get("failure_codes") or ["F_FRACTURE_SENSOR_STATE_MISMATCH"]:
            if code not in failure_codes:
                failure_codes.append(code)
        write_json(
            render_sync_path,
            {
                **render_sync,
                "status": "fail",
                "failure_codes": failure_codes,
                "render_pass_valid": False,
                "fracture_sensor_state_ready": False,
            },
        )
    missing_rgb_camera_ids = [camera_id for camera_id in camera_ids if not rgb_available_by_camera.get(camera_id)]
    return {
        "schema_version": "harness_local_ue_runner_report.v1",
        "status": "completed",
        "native_ue_invoked": True,
        "native_output": str(native_output),
        "render_mode": render_mode,
        "rgb_real_ue": not missing_rgb_camera_ids,
        "rgb_native_view_count": len(native_rgb_views),
        "missing_rgb_camera_ids": missing_rgb_camera_ids,
        "depth_real_ue": bool(native_depth_views),
        "segmentation_real_ue": bool(native_segmentation_views),
        "fracture_sensor_state_ready": fracture_sensor_state_ready,
        "fracture_sensor_state_report": "fracture_sensor_state_report.json",
        "world_model_manifest": "manifest.json",
        "message": "Native UE dual-pass artifacts completed; RGB and data availability are reflected in manifest.json and render_sync_report.",
    }


def canonical_game_package(value: Any) -> str | None:
    text = str(value or "").strip().split(":", 1)[0]
    if not text:
        return None
    dot = text.find(".", text.rfind("/"))
    return text[:dot] if dot >= 0 else text.rstrip("/")


def views_by_id(views: list[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for view in views:
        if isinstance(view, dict) and view.get("view_id"):
            result[str(view["view_id"])] = view
    return result


def quantize_rgb24_to_palette(data: bytes, palette: list[tuple[int, int, int]]) -> bytes:
    if len(data) % 3:
        raise ValueError("RGB24 payload length must be divisible by three")
    if not palette:
        raise ValueError("instance palette must not be empty")
    cache: dict[int, tuple[int, int, int]] = {}
    output = bytearray(len(data))
    for offset in range(0, len(data), 3):
        red, green, blue = data[offset], data[offset + 1], data[offset + 2]
        key = (red << 16) | (green << 8) | blue
        nearest = cache.get(key)
        if nearest is None:
            nearest = min(
                palette,
                key=lambda color: (color[0] - red) ** 2 + (color[1] - green) ** 2 + (color[2] - blue) ** 2,
            )
            cache[key] = nearest
        output[offset : offset + 3] = bytes(nearest)
    return bytes(output)


def quantize_native_instance_segmentation(
    native_output: Path,
    *,
    width: int,
    height: int,
    fps: int,
    required: bool,
    required_object_ids: set[str] | None = None,
) -> dict[str, Any]:
    manifest_path = native_output / "render_pass_manifest.json"
    manifest = read_optional_json(manifest_path)
    segmentation = ((manifest.get("passes") or {}).get("segmentation") or {}) if isinstance(manifest, dict) else {}
    views = segmentation.get("views") if isinstance(segmentation.get("views"), list) else []
    if not views:
        return {
            "status": "fail" if required else "not_required",
            "error": "segmentation manifest has no views" if required else None,
            "views": [],
        }
    if width <= 0 or height <= 0 or fps <= 0:
        return {"status": "fail", "error": "invalid render dimensions for segmentation quantization", "views": []}

    results = []
    for view in views:
        view_id = str(view.get("view_id") or "view")
        frame_paths = [Path(str(path)) for path in view.get("frames") or [] if path]
        if len(set(frame_paths)) != len(frame_paths):
            return {
                "status": "fail",
                "error": f"segmentation manifest contains duplicate frame paths for {view_id}",
                "views": results,
            }
        mapping = [item for item in view.get("instance_mapping") or [] if isinstance(item, dict)]
        mapped_object_ids = {str(item.get("object_id") or "") for item in mapping}
        missing_mapping = sorted((required_object_ids or set()) - mapped_object_ids)
        if missing_mapping:
            return {
                "status": "fail",
                "error": f"required segmentation instances are not mapped for {view_id}: {missing_mapping}",
                "views": results,
            }
        palette = [(0, 0, 0)]
        required_colors = set()
        for item in mapping:
            rgb = item.get("rgb")
            if not isinstance(rgb, list) or len(rgb) < 3:
                continue
            color = tuple(max(0, min(255, int(round(float(value) * 255.0)))) for value in rgb[:3])
            palette.append(color)
            if str(item.get("object_id") or "") in (required_object_ids or set()):
                required_colors.add(color)
        palette = list(dict.fromkeys(palette))
        if not frame_paths or len(palette) <= 1 or any(not path.is_file() for path in frame_paths):
            return {
                "status": "fail",
                "error": f"segmentation frames or palette are incomplete for {view_id}",
                "views": results,
            }

        raw_dir = native_output / "segmentation_raw" / view_id
        raw_dir.mkdir(parents=True, exist_ok=True)
        output_dir = frame_paths[0].parent
        output_dir.mkdir(parents=True, exist_ok=True)
        raw_frames = []
        for index, source in enumerate(frame_paths):
            raw_target = raw_dir / f"frame_{index:04d}.exr"
            shutil.move(str(source), raw_target)
            raw_frames.append(raw_target)

        decoder = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-start_number",
                "0",
                "-i",
                str(raw_dir / "frame_%04d.exr"),
                "-frames:v",
                str(len(raw_frames)),
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        encoder = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-video_size",
                f"{width}x{height}",
                "-framerate",
                str(fps),
                "-i",
                "pipe:0",
                "-frames:v",
                str(len(raw_frames)),
                "-c:v",
                "exr",
                "-start_number",
                "0",
                str(output_dir / "frame_%04d.exr"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        frame_size = width * height * 3
        sample_indices = {0, len(raw_frames) // 2}
        sample_colors: dict[int, set[tuple[int, int, int]]] = {}
        observed_colors: set[tuple[int, int, int]] = set()
        try:
            for frame_index, _ in enumerate(raw_frames):
                chunks = bytearray()
                while len(chunks) < frame_size:
                    chunk = decoder.stdout.read(frame_size - len(chunks)) if decoder.stdout else b""
                    if not chunk:
                        break
                    chunks.extend(chunk)
                if len(chunks) != frame_size:
                    raise RuntimeError(f"ffmpeg decoded a short segmentation frame for {view_id}")
                if not encoder.stdin:
                    raise RuntimeError("ffmpeg segmentation encoder stdin is unavailable")
                quantized = quantize_rgb24_to_palette(bytes(chunks), palette)
                observed_colors.update(zip(quantized[0::3], quantized[1::3], quantized[2::3]))
                if frame_index in sample_indices:
                    sample_colors[frame_index] = set(zip(quantized[0::3], quantized[1::3], quantized[2::3]))
                encoder.stdin.write(quantized)
            if encoder.stdin:
                encoder.stdin.close()
            decoder_code = decoder.wait(timeout=120)
            encoder_code = encoder.wait(timeout=120)
            if decoder_code or encoder_code:
                decoder_error = (decoder.stderr.read() if decoder.stderr else b"").decode("utf-8", errors="replace")
                encoder_error = (encoder.stderr.read() if encoder.stderr else b"").decode("utf-8", errors="replace")
                raise RuntimeError(f"ffmpeg segmentation quantization failed: {decoder_error[-500:]} {encoder_error[-500:]}")
            if any(not colors or colors == {(0, 0, 0)} for colors in sample_colors.values()):
                raise RuntimeError(f"segmentation quantization produced an empty instance mask for {view_id}")
            missing_colors = sorted(required_colors - observed_colors)
            if missing_colors:
                raise RuntimeError(f"required segmentation instances have no visible pixels for {view_id}: {missing_colors}")
        except Exception as exc:
            decoder.kill()
            encoder.kill()
            return {"status": "fail", "error": str(exc), "views": results}

        output_frames = [output_dir / f"frame_{index:04d}.exr" for index in range(len(raw_frames))]
        if any(not has_openexr_magic(path) for path in output_frames):
            return {"status": "fail", "error": f"quantized OpenEXR sequence is incomplete for {view_id}", "views": results}
        view["frames"] = [str(path) for path in output_frames]
        view["path"] = str(output_frames[0])
        view["raw_frames"] = [str(path) for path in raw_frames]
        view["palette_rgb8"] = [list(color) for color in palette]
        view["palette_quantized"] = True
        view["palette_closure"] = True
        results.append(
            {
                "view_id": view_id,
                "frame_count": len(output_frames),
                "palette_size": len(palette),
                "raw_dir": str(raw_dir),
            }
        )

    write_json(manifest_path, manifest)
    return {"status": "pass", "views": results}


def camera_views_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return views_by_id(payload.get("views") if isinstance(payload.get("views"), list) else [])


def camera_intrinsics(width: int, height: int, fov_degrees: Any) -> dict[str, Any] | None:
    try:
        fov = float(fov_degrees)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0 or not 0.0 < fov < 180.0:
        return None
    focal = width / (2.0 * math.tan(math.radians(fov) / 2.0))
    return {
        "model": "pinhole",
        "width": width,
        "height": height,
        "horizontal_fov_deg": round(fov, 6),
        "fx": round(focal, 6),
        "fy": round(focal, 6),
        "cx": round(width / 2.0, 6),
        "cy": round(height / 2.0, 6),
    }


def first_capture_source(views: dict[str, dict[str, Any]]) -> Any:
    return first_view_value(views, "capture_source")


def first_view_value(views: dict[str, dict[str, Any]], key: str) -> Any:
    return next(iter(views.values()), {}).get(key)


def native_view_for_camera(camera_id: str, index: int, native_rgb_views: list[dict[str, Any]]) -> dict[str, Any]:
    for view in native_rgb_views:
        if str(view.get("view_id")) == camera_id:
            return view
    return {}


def copy_native_pass_view(native_view: dict[str, Any] | None, view_dir: Path, anchor_name: str, frames_dir_name: str) -> dict[str, Any]:
    if not native_view:
        return {"available": False, "frame_count": 0, "frames": [], "format_mismatch_count": 0}
    frames = [Path(str(path)) for path in (native_view.get("frames") or []) if path]
    if not frames and native_view.get("path"):
        frames = [Path(str(native_view["path"]))]
    copied_frames: list[str] = []
    valid_frames: list[Path] = []
    format_mismatch_count = 0
    frame_dir = view_dir / frames_dir_name
    for source in frames:
        if not source.exists() or source.stat().st_size == 0:
            continue
        if anchor_name.endswith(".exr") and not has_openexr_magic(source):
            format_mismatch_count += 1
            continue
        frame_dir.mkdir(parents=True, exist_ok=True)
        target = frame_dir / source.with_suffix(Path(anchor_name).suffix).name
        link_or_copy(source, target)
        valid_frames.append(source)
        copied_frames.append(str(target.relative_to(view_dir.parent.parent)))
    anchor_source = valid_frames[0] if valid_frames else Path("")
    anchor_target = view_dir / anchor_name
    if valid_frames:
        link_or_copy(anchor_source, anchor_target)
        if anchor_name == "segmentation.exr" and (view_dir / "segmentation.png").exists():
            (view_dir / "segmentation.png").unlink()
    elif anchor_target.exists():
        anchor_target.unlink()
    available = has_openexr_magic(anchor_target) if anchor_name.endswith(".exr") else anchor_target.exists() and anchor_target.stat().st_size > 0
    return {
        "available": available,
        "frame_count": len(copied_frames) if copied_frames else (1 if available else 0),
        "frames": copied_frames,
        "format_mismatch_count": format_mismatch_count,
        "depth_variance": native_view.get("depth_variance"),
        "unit": native_view.get("unit"),
        "depth_type": native_view.get("depth_type"),
        "depth_encoding": native_view.get("depth_encoding"),
        "stored_value_to_centimeter": native_view.get("stored_value_to_centimeter"),
        "unit_validation": native_view.get("unit_validation"),
        "instance_count": native_view.get("instance_count"),
        "instance_mapping": native_view.get("instance_mapping"),
        "palette_quantized": native_view.get("palette_quantized"),
        "palette_closure": native_view.get("palette_closure"),
        "palette_rgb8": native_view.get("palette_rgb8"),
        "raw_frames": native_view.get("raw_frames"),
    }


def encode_sensor_preview(frame_dir: Path, output: Path, *, fps: int, modality: str) -> bool:
    frames = sorted(frame_dir.glob("frame_*.exr"))
    if not frames:
        return False
    # Preview only: clip the 100 m sentinel/far background at 8 m so centimeter-scale
    # subjects remain visible. Canonical metric values stay untouched in EXR.
    video_filter = (
        "colorlevels=rimax=0.08:gimax=0.08:bimax=0.08,"
        "normalize=smoothing=24:independence=0,pseudocolor=preset=viridis,format=yuv420p"
        if modality == "depth"
        else "format=yuv420p"
    )
    output.unlink(missing_ok=True)
    completed = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            str(max(1, fps)),
            "-start_number",
            "0",
            "-i",
            str(frame_dir / "frame_%04d.exr"),
            "-frames:v",
            str(len(frames)),
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ],
        capture_output=True,
        check=False,
        timeout=180,
    )
    return completed.returncode == 0 and has_mp4_magic(output)


def run_ue_until_artifacts(command: list[str], *, env: dict[str, str], native_output: Path, timeout: int) -> dict[str, Any]:
    if native_output.exists():
        shutil.rmtree(native_output)
    native_output.mkdir(parents=True, exist_ok=True)
    stdout_path = native_output / "ue_process_stdout.log"
    stderr_path = native_output / "ue_process_stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(command, cwd=ROOT, env=env, text=True, stdout=stdout_handle, stderr=stderr_handle)
    started = time.perf_counter()
    stable_signature = None
    stable_count = 0
    ready = False
    while True:
        returncode = proc.poll()
        signature = native_artifact_signature(native_output)
        if signature is not None and signature == stable_signature:
            stable_count += 1
        elif signature is not None:
            stable_signature = signature
            stable_count = 1
        if signature is not None and stable_count >= 2:
            ready = True
            break
        if returncode is not None:
            stdout_handle.close()
            stderr_handle.close()
            stdout = read_text_tail(stdout_path, limit=200000)
            stderr = read_text_tail(stderr_path, limit=200000)
            if native_output_ready(native_output):
                return {"status": "completed", "returncode": returncode, "stdout": stdout or "", "stderr": stderr or ""}
            return {"status": "failed", "returncode": returncode, "stdout": stdout or "", "stderr": stderr or ""}
        if time.perf_counter() - started > timeout:
            terminate_process(proc)
            stdout_handle.close()
            stderr_handle.close()
            stdout = read_text_tail(stdout_path, limit=200000)
            stderr = read_text_tail(stderr_path, limit=200000)
            return {"status": "timeout", "returncode": proc.returncode, "stdout": stdout or "", "stderr": stderr or "", "timeout": timeout}
        time.sleep(5.0)
    if ready:
        terminate_process(proc)
        stdout_handle.close()
        stderr_handle.close()
        stdout = read_text_tail(stdout_path, limit=200000)
        stderr = read_text_tail(stderr_path, limit=200000)
        return {"status": "completed", "returncode": proc.returncode, "stdout": stdout or "", "stderr": stderr or ""}
    stdout_handle.close()
    stderr_handle.close()
    stdout = read_text_tail(stdout_path, limit=200000)
    stderr = read_text_tail(stderr_path, limit=200000)
    return {"status": "completed", "returncode": proc.returncode, "stdout": stdout or "", "stderr": stderr or ""}


def native_output_ready(native_output: Path) -> bool:
    required = [
        native_output / "summary.json",
        native_output / "trajectory.json",
        native_output / "camera_trajectories.json",
        native_output / "render_pass_manifest.json",
        native_primary_video(native_output),
    ]
    return all(path.exists() and path.stat().st_size > 0 for path in required)


def native_artifact_signature(native_output: Path) -> tuple[int, ...] | None:
    if not native_output_ready(native_output):
        return None
    paths = [
        native_output / "summary.json",
        native_output / "trajectory.json",
        native_output / "camera_trajectories.json",
        native_output / "render_pass_manifest.json",
        native_primary_video(native_output),
    ]
    try:
        return tuple(path.stat().st_size for path in paths)
    except OSError:
        return None


def native_primary_video(native_output: Path, *, camera_plan: dict[str, Any] | None = None) -> Path:
    default = native_output / "preview.mp4"
    manifest = read_optional_json(native_output / "render_pass_manifest.json")
    views = [view for view in (((manifest.get("passes") or {}).get("rgb") or {}).get("views") or []) if isinstance(view, dict)]
    if camera_plan is None:
        if default.is_file() and default.stat().st_size > 0:
            return default
        for view in views:
            candidate = Path(str(view.get("path") or ""))
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return default
    camera_id = next((str(view.get("camera_id")) for view in (camera_plan or {}).get("views", []) if view.get("camera_id")), "")
    candidate = Path(str(native_view_for_camera(camera_id, 0, views).get("path") or ""))
    return candidate if candidate.is_file() and candidate.stat().st_size > 0 else native_output / f"preview_{camera_id}.mp4"


def terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


def read_text_tail(path: Path, *, limit: int) -> str:
    if not path.exists():
        return ""
    data = path.read_text(encoding="utf-8", errors="replace")
    return data[-limit:]


def extract_contacts(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    for frame in trajectory:
        frame_id = int(frame.get("frame") or 0)
        time_s = float(frame.get("time") or frame.get("time_s") or 0.0)
        for contact in frame.get("contacts") or []:
            if isinstance(contact, dict):
                row = dict(contact)
                row.setdefault("frame", frame_id)
                row.setdefault("time_s", time_s)
                contacts.append(row)
    return contacts


def fail_report(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": "harness_local_ue_runner_report.v1",
        "status": "failed",
        "failure_code": code,
        "failure_message": message,
        "native_ue_invoked": code.startswith("F6"),
        **extra,
    }


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = read_json(path)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
