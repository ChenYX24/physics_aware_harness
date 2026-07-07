from __future__ import annotations

import argparse
import json
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

from harness.core.artifact_manager import ArtifactManager
from harness.core.capability import canonical_capability_id


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
    studio_scene_spec = build_studio_scene_spec(case_spec, args, actor_placement=actor_placement)
    runtime_scene = build_runtime_scene(case_spec, camera_plan, args, pass_mode="data" if args.mode == "both" else args.mode, actor_placement=actor_placement)
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
    for pass_mode in pass_sequence(args.mode):
        native_output = native_output_dir(run_dir, pass_mode, args.mode)
        pass_runtime_scene = build_runtime_scene(case_spec, camera_plan, args, pass_mode=pass_mode, actor_placement=actor_placement)
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
        if pass_mode == "rgb":
            rgb_native_output = native_output
        if pass_mode == "data":
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
    report["pass_results"] = {
        key: {"status": value.get("status"), "returncode": value.get("returncode"), "native_output": str(native_output_dir(run_dir, key, args.mode))}
        for key, value in pass_results.items()
    }
    write_json(run_dir / "local_ue_runner_report.json", report)
    return 0 if report["status"] == "completed" else 2


def build_studio_scene_spec(case_spec: dict[str, Any], args: argparse.Namespace, *, actor_placement: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "draft_id": str(case_spec.get("case_id") or "harness_case"),
        "prompt": case_spec.get("prompt", ""),
        "expanded_prompt": case_spec.get("prompt", ""),
        "asset_policy": "harness_local_ue_analytic_proxy",
        "physics_settings": {
            "duration_s": duration_for_case(case_spec),
            "fps": int(os.environ.get("SIM_STUDIO_UE_FPS", "24")),
            "simulate_physics": True,
            "contact_event_recording": True,
        },
        "physics_controls": default_physics_controls(case_spec),
        "semantic_plan": {"case_type": native_case_type(case_spec), "source": "harness_case_spec"},
        "runtime_actor_placement": summarize_actor_placement(actor_placement),
        "camera_plan": {},
        "map_lighting_controls": default_lighting_controls("data"),
        "assets": [],
        "background": {"ue5_path": args.map or "/Game/Maps/MarketEnvironment/Maps/Day.Day"},
    }


def build_runtime_scene(case_spec: dict[str, Any], camera_plan: dict[str, Any], args: argparse.Namespace, *, pass_mode: str, actor_placement: dict[str, Any] | None = None) -> dict[str, Any]:
    dynamic_objects, static_objects = runtime_objects_for_case(case_spec, actor_placement=actor_placement)
    return {
        "schema_version": "studio_runtime_v1",
        "draft_id": str(case_spec.get("case_id") or "harness_case"),
        "case_type": native_case_type(case_spec),
        "background_map": {"ue5_path": args.map or "/Game/Maps/MarketEnvironment/Maps/Day.Day"},
        "prompt": case_spec.get("prompt", ""),
        "expanded_prompt": case_spec.get("prompt", ""),
        "simulation": {
            "duration_s": duration_for_case(case_spec),
            "fps": int(os.environ.get("SIM_STUDIO_UE_FPS", "24")),
            "dt": 1.0 / max(1, int(os.environ.get("SIM_STUDIO_UE_FPS", "24"))),
        },
        "physics": case_spec.get("physical_parameters") or {},
        "physics_controls": default_physics_controls(case_spec),
        "render": {
            "width": int(os.environ.get("SIM_STUDIO_UE_WIDTH", "1920")),
            "height": int(os.environ.get("SIM_STUDIO_UE_HEIGHT", "1080")),
            "fps": int(os.environ.get("SIM_STUDIO_UE_FPS", "24")),
            "quality_preset": os.environ.get("SIM_STUDIO_UE_RENDER_QUALITY", "medium"),
            "pass_mode": pass_mode,
            "deterministic": True,
        },
        "camera": camera_runtime_from_plan(camera_plan),
        "map_lighting_controls": default_lighting_controls(pass_mode),
        "dynamic_objects": dynamic_objects,
        "static_objects": static_objects,
        "validation_targets": [],
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
    for index, binding in enumerate(actor_placement.get("actor_bindings") or []):
        if not isinstance(binding, dict):
            continue
        object_id = str(binding.get("object_id") or f"actor_{index:02d}")
        case_object = by_case_object.get(object_id, {})
        physics = binding.get("physics") if isinstance(binding.get("physics"), dict) else {}
        transform = binding.get("transform") if isinstance(binding.get("transform"), dict) else {}
        bounds = binding.get("bounds") if isinstance(binding.get("bounds"), dict) else {}
        params = {
            "runtime_actor_id": binding.get("runtime_actor_id"),
            "binding_source": ((binding.get("asset") or {}).get("binding_source") if isinstance(binding.get("asset"), dict) else None),
            "role": binding.get("role"),
            "material": physics.get("material"),
            "collision_profile": physics.get("collision_profile"),
            "collider": physics.get("collider"),
        }
        runtime_physics = {
            "mass_kg": physics.get("mass_kg"),
            "initial_velocity_m_s": list(case_object.get("initial_velocity_m_s") or case_object.get("initial_velocity") or [0.0, 0.0, 0.0]),
            "collision_profile": physics.get("collision_profile"),
            "collider": physics.get("collider"),
            "material": physics.get("material"),
            "simulate_physics": bool(physics.get("simulate_physics")),
            "kinematic": bool(physics.get("kinematic")),
        }
        actor = runtime_object(
            object_id,
            ue_path_for_binding(binding),
            "llm_rigid_body" if physics.get("simulate_physics") else "llm_static_body",
            list(transform.get("position_m") or case_object.get("initial_position_m") or case_object.get("position_m") or [0.0, 0.0, 0.0]),
            scale_for_binding(binding),
            runtime_physics,
            params,
        )
        if physics.get("simulate_physics"):
            dynamic.append(actor)
        else:
            static.append(actor)
    return dynamic, static


def ue_path_for_binding(binding: dict[str, Any]) -> str:
    asset = binding.get("asset") if isinstance(binding.get("asset"), dict) else {}
    ue_path = str(asset.get("ue_path") or "")
    if is_runtime_mesh_path(ue_path):
        return ue_path
    physics = binding.get("physics") if isinstance(binding.get("physics"), dict) else {}
    collider = str(physics.get("collider") or "").casefold()
    role = str(binding.get("role") or "").casefold()
    if "sphere" in collider or "ball" in role:
        return "/Engine/BasicShapes/Sphere.Sphere"
    if "capsule" in collider or "cylinder" in collider or "pin" in role or "domino" in role:
        return "/Engine/BasicShapes/Cylinder.Cylinder" if "pin" in role else "/Engine/BasicShapes/Cube.Cube"
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
    bounds = binding.get("bounds") if isinstance(binding.get("bounds"), dict) else {}
    extents = bounds.get("extents_m")
    if isinstance(extents, list) and len(extents) >= 3:
        return [float(extents[0]), float(extents[1]), float(extents[2])]
    return [0.25, 0.25, 0.25]


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
) -> dict[str, Any]:
    return {
        "id": object_id,
        "asset_key": object_id,
        "asset_name": object_id,
        "ue5_path": ue5_path,
        "category_l1": "harness",
        "category_l2": "physics",
        "class_name": "StaticMesh",
        "asset_kind": "static_mesh",
        "render_usage": "runtime_static_mesh",
        "runtime_spawnable": True,
        "behavior": behavior,
        "initial_position_m": position,
        "scale": scale,
        "physics_properties": physics,
        "params": params,
    }


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
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    for key in ("duration_s", "duration"):
        if expected.get(key) is not None:
            return max(1.0, min(12.0, float(expected[key])))
    return float(os.environ.get("SIM_STUDIO_UE_DURATION", "4.0"))


def default_physics_controls(case_spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "physics_controls_v1",
        "gravity_enabled": True,
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
    }


def default_lighting_controls(pass_mode: str = "data") -> dict[str, Any]:
    controls = {
        "preset": "harness_local_ue",
        "visual_realism_profile": "editor_parity",
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
        controls.update(
            {
                "preset": "harness_data_scene_capture",
                "capture_backend": "scene_capture",
                "fixed_auto_exposure": True,
            }
        )
    return controls


def build_render_config(args: argparse.Namespace, case_spec: dict[str, Any], camera_plan: dict[str, Any]) -> dict[str, Any]:
    fps = int(os.environ.get("SIM_STUDIO_UE_FPS", "24"))
    return {
        "schema_version": "render_config.v2.3",
        "mode": args.mode,
        "backend": "ue",
        "ue_renderer_only": True,
        "seed": int(os.environ.get("SIM_STUDIO_SEED", case_spec.get("seed") or "0")),
        "width": int(os.environ.get("SIM_STUDIO_UE_WIDTH", "1920")),
        "height": int(os.environ.get("SIM_STUDIO_UE_HEIGHT", "1080")),
        "fps": fps,
        "duration_s": duration_for_case(case_spec),
        "views": [str(view.get("camera_id")) for view in camera_plan.get("views", []) if isinstance(view, dict) and view.get("camera_id")],
        "passes": [item.strip() for item in args.passes.split(",") if item.strip()],
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


def pass_sequence(mode: str) -> list[str]:
    return ["rgb", "data"] if mode == "both" else [mode]


def native_output_dir(run_dir: Path, pass_mode: str, requested_mode: str) -> Path:
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
            "RENDER_DATA_PASSES": "1" if pass_mode == "data" else "0",
            "AUDIO_PASS_ENABLED": "0",
            "KEEP_RENDER_FRAMES": os.environ.get("SIM_STUDIO_KEEP_RENDER_FRAMES", "0"),
            "CHAOS_RIGID_BODY_SETUP": "1",
            "CHAOS_SIMULATION_ENABLED": "1",
            "RENDER_QUALITY_PRESET": os.environ.get("SIM_STUDIO_UE_RENDER_QUALITY", "medium"),
            "VISUAL_REALISM_PROFILE": os.environ.get("SIM_STUDIO_UE_VISUAL_REALISM_PROFILE", "editor_parity"),
            "RENDER_VIEWPORT_SETTLE_SECONDS": os.environ.get("SIM_STUDIO_UE_VIEWPORT_SETTLE_SECONDS", "0.5"),
            "RENDER_WARMUP_FRAMES": os.environ.get("SIM_STUDIO_UE_WARMUP_FRAMES", "2"),
            "RENDER_FIRST_FRAME_STABILITY_SAMPLES": os.environ.get("SIM_STUDIO_UE_FIRST_FRAME_STABILITY_SAMPLES", "1"),
            "RENDER_SCREENSHOT_STABLE_TICKS": os.environ.get("SIM_STUDIO_UE_SCREENSHOT_STABLE_TICKS", "1"),
            "VIDEO_CRF": os.environ.get("SIM_STUDIO_UE_VIDEO_CRF", "18"),
            "VIDEO_PRESET": os.environ.get("SIM_STUDIO_UE_VIDEO_PRESET", "slow"),
            "SIM_STUDIO_UE_CAPTURE_BACKEND": os.environ.get("SIM_STUDIO_UE_RGB_CAPTURE_BACKEND", "highres_viewport") if pass_mode == "rgb" else "scene_capture",
            "WORLD_MODEL_RENDER_PASS_MODE": pass_mode,
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
    if pass_mode == "data":
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
    if pass_mode == "rgb" and "timeout waiting for" in error_text and "frames/frame_" in error_text:
        return {
            "failure_code": "F_RGB_HIGHRES_VIEWPORT_UNAVAILABLE",
            "failure_message": "RGB highres viewport capture did not produce screenshot frames in this UE launch mode.",
        }
    if pass_mode == "rgb" and not (native_output / "preview.mp4").exists():
        return {
            "failure_code": "F_RGB_PASS_MISSING",
            "failure_message": "RGB pass completed without a preview/video artifact.",
        }
    if pass_mode == "data":
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
    position = first.get("position_m") or [-4.0, -5.0, 2.4]
    target = first.get("target_m") or [0.0, 0.0, 0.5]
    return {
        "mode": "fixed",
        "coordinate_frame": "world",
        "movement": "static",
        "preview_waypoints": [
            {"time_s": 0, "position_m": position, "target_offset_m": target},
            {"time_s": duration_from_camera_plan(camera_plan), "position_m": position, "target_offset_m": target},
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
    preview_path = native_output / "preview.mp4"
    camera_tracks_path = native_output / "camera_trajectories.json"
    render_manifest_path = native_output / "render_pass_manifest.json"
    missing = [str(path.name) for path in (summary_path, trajectory_path, preview_path, camera_tracks_path, render_manifest_path) if not path.exists() or path.stat().st_size == 0]
    if missing:
        return fail_report("F6_RUNTIME_OR_RENDER_FAILURE", f"Native UE output missing: {missing}")
    shutil.copyfile(trajectory_path, run_dir / "trajectory.json")
    contacts = extract_contacts(read_json(trajectory_path))
    write_json(run_dir / "contact_events.json", contacts)
    shutil.copyfile(camera_tracks_path, run_dir / "camera_trajectory.json")
    shutil.copyfile(render_manifest_path, run_dir / "render_manifest.json")
    shutil.copyfile(preview_path, run_dir / "video.mp4")
    rgb_preview_path = (rgb_native_output / "preview.mp4") if rgb_native_output else None
    if rgb_preview_path and rgb_preview_path.exists() and rgb_preview_path.stat().st_size > 0:
        shutil.copyfile(rgb_preview_path, run_dir / "video.mp4")

    # Real UE RGB is available. Strict M2.3 depth/segmentation remains absent unless
    # the native UE script exports actual buffers; do not fabricate them here.
    camera_ids = [str(view.get("camera_id")) for view in camera_plan.get("views", []) if view.get("camera_id")]
    if not camera_ids:
        camera_ids = ["overview"]
    native_summary = read_json(summary_path)
    native_manifest = read_json(render_manifest_path)
    native_rgb_views = [
        view
        for view in (((native_manifest.get("passes") or {}).get("rgb") or {}).get("views") or [])
        if isinstance(view, dict) and view.get("path")
    ]
    native_depth_views = views_by_id(((native_manifest.get("passes") or {}).get("depth") or {}).get("views") or [])
    native_segmentation_views = views_by_id(((native_manifest.get("passes") or {}).get("segmentation") or {}).get("views") or [])
    frame_count = int(native_summary.get("frames") or 0)
    fps = int(native_summary.get("fps") or 12)
    timestamps = [round(idx / max(1, fps), 6) for idx in range(frame_count)]
    for index, camera_id in enumerate(camera_ids):
        view_dir = run_dir / "views" / camera_id
        view_dir.mkdir(parents=True, exist_ok=True)
        native_view = native_view_for_camera(camera_id, index, native_rgb_views)
        native_rgb_path = Path(str(native_view.get("path") or preview_path))
        if rgb_native_output and index == 0 and (rgb_native_output / "preview.mp4").exists():
            native_rgb_path = rgb_native_output / "preview.mp4"
        if native_rgb_path.exists() and native_rgb_path.stat().st_size > 0:
            shutil.copyfile(native_rgb_path, view_dir / "rgb.mp4")
        native_view_id = str(native_view.get("view_id") or camera_id)
        depth_copy = copy_native_pass_view(native_depth_views.get(native_view_id) or native_depth_views.get(camera_id), view_dir, "depth.exr", "depth_frames")
        segmentation_copy = copy_native_pass_view(native_segmentation_views.get(native_view_id) or native_segmentation_views.get(camera_id), view_dir, "segmentation.png", "segmentation_frames")
        depth_available = bool(depth_copy.get("available"))
        segmentation_available = bool(segmentation_copy.get("available"))
        depth_frame_count = int(depth_copy.get("frame_count") or 0)
        segmentation_type = "instance" if segmentation_available else "missing"
        write_json(
            view_dir / "meta.json",
            {
                "camera_id": camera_id,
                "source_native_view_id": native_view.get("view_id"),
                "frame_count_rgb": frame_count,
                "frame_count_depth": depth_frame_count,
                "timestamps_rgb": timestamps,
                "timestamps_depth": timestamps[:depth_frame_count],
                "fps": fps,
                "depth_source": "ue" if depth_available else "missing",
                "depth_variance": float(depth_copy.get("depth_variance") or (1.0 if depth_available else 0.0)),
                "depth_frames": depth_copy.get("frames", []),
                "segmentation_type": segmentation_type,
                "instance_level": segmentation_available,
                "segmentation_frames": segmentation_copy.get("frames", []),
                "instance_count": int(segmentation_copy.get("instance_count") or 0),
                "instance_mapping": segmentation_copy.get("instance_mapping") or [],
                "render_time_sec": round(time.perf_counter() - started, 4),
                "native_ue_rgb": True,
                "native_output": str(native_output),
                "rgb_pass_native_output": str(rgb_native_output) if rgb_native_output else None,
                "native_rgb_path": str(native_rgb_path),
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
            rgb_video_source=(rgb_preview_path if rgb_preview_path and rgb_preview_path.exists() else run_dir / "video.mp4"),
        )
    return {
        "schema_version": "harness_local_ue_runner_report.v1",
        "status": "completed",
        "native_ue_invoked": True,
        "native_output": str(native_output),
        "render_mode": render_mode,
        "rgb_real_ue": True,
        "rgb_native_view_count": len(native_rgb_views),
        "depth_real_ue": bool(native_depth_views),
        "segmentation_real_ue": bool(native_segmentation_views),
        "world_model_manifest": "manifest.json",
        "message": "Native UE dual-pass artifacts completed; RGB and data availability are reflected in manifest.json and render_sync_report.",
    }


def views_by_id(views: list[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for view in views:
        if isinstance(view, dict) and view.get("view_id"):
            result[str(view["view_id"])] = view
    return result


def native_view_for_camera(camera_id: str, index: int, native_rgb_views: list[dict[str, Any]]) -> dict[str, Any]:
    for view in native_rgb_views:
        if str(view.get("view_id")) == camera_id:
            return view
    return native_rgb_views[index] if index < len(native_rgb_views) else {}


def copy_native_pass_view(native_view: dict[str, Any] | None, view_dir: Path, anchor_name: str, frames_dir_name: str) -> dict[str, Any]:
    if not native_view:
        return {"available": False, "frame_count": 0, "frames": []}
    frames = [Path(str(path)) for path in (native_view.get("frames") or []) if path]
    if not frames and native_view.get("path"):
        frames = [Path(str(native_view["path"]))]
    copied_frames: list[str] = []
    frame_dir = view_dir / frames_dir_name
    for source in frames:
        if not source.exists() or source.stat().st_size == 0:
            continue
        frame_dir.mkdir(parents=True, exist_ok=True)
        target = frame_dir / source.name
        shutil.copyfile(source, target)
        copied_frames.append(str(target.relative_to(view_dir.parent.parent)))
    anchor_source = frames[0] if frames else Path(str(native_view.get("path") or ""))
    anchor_target = view_dir / anchor_name
    if anchor_source.exists() and anchor_source.stat().st_size > 0:
        shutil.copyfile(anchor_source, anchor_target)
    available = anchor_target.exists() and anchor_target.stat().st_size > 0
    return {
        "available": available,
        "frame_count": len(copied_frames) if copied_frames else (1 if available else 0),
        "frames": copied_frames,
        "depth_variance": native_view.get("depth_variance"),
        "instance_count": native_view.get("instance_count"),
        "instance_mapping": native_view.get("instance_mapping"),
    }


def run_ue_until_artifacts(command: list[str], *, env: dict[str, str], native_output: Path, timeout: int) -> dict[str, Any]:
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
        native_output / "preview.mp4",
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
        native_output / "preview.mp4",
    ]
    try:
        return tuple(path.stat().st_size for path in paths)
    except OSError:
        return None


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
