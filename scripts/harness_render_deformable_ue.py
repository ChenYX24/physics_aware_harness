from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.artifact_manager import ArtifactManager, link_or_copy
from harness.core.artifact_schema import read_json, write_json
from harness.runtime.deformable_surface_adapter import (
    load_deformable_case_data,
)
from harness.runtime.kinematic_motion import sample_linear_kinematic_position, sample_linear_kinematic_rotation
from harness.runtime.observation_planner import camera_ids_from_observation_plan, render_mode_for_passes, render_passes_from_observation_plan
from harness.runtime.execution_profile import (
    execution_profile,
    execution_profile_names,
    write_execution_reports,
)
from harness.runtime.render_pass_contract import write_render_contract_artifacts
from harness.verification.render_sync_checker import check_render_sync
from harness.verification.rgb_observability import verify_expected_color_observability
from harness.verification.run_quality import evaluate_run
from scripts.harness_local_ue_runner import (
    DEFAULT_UE_EXECUTABLE,
    camera_runtime_from_plan,
    quantize_native_instance_segmentation,
    run_precompiled_ue_scene,
    runtime_objects_from_actor_placement,
    standardize_native_output,
)
from scripts.harness_render_fluid_ue import ensure_ue_surface_assets


CLOTH_COLOR = [0.72, 0.08, 0.025]
SOLID_COLOR = [0.06, 0.32, 0.78]


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a fixed-topology deformable mesh cache through native UE capture.")
    parser.add_argument("replay_manifest")
    parser.add_argument("--cache-manifest", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--ue-project", required=True)
    parser.add_argument("--ue-executable", default=str(DEFAULT_UE_EXECUTABLE))
    parser.add_argument("--map", default="/Game/Maps/MarketEnvironment/Maps/Day.Day")
    parser.add_argument("--profile", choices=execution_profile_names(), default="smoke")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    args = parser.parse_args()

    started = time.perf_counter()
    profile = execution_profile(args.profile)
    width = int(args.width or profile.width)
    height = int(args.height or profile.height)
    run_dir = Path(args.run_dir).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    observation_plan = read_json(run_dir / "observation_plan.json")
    camera_payload = read_json(run_dir / "camera_plan.json")
    requested_views = camera_ids_from_observation_plan(observation_plan)
    render_passes = render_passes_from_observation_plan(observation_plan)
    render_mode = render_mode_for_passes(render_passes)
    complete_sensor_contract = {"rgb", "depth", "segmentation"}.issubset(render_passes)
    if not requested_views:
        raise SystemExit("compiled Observation Plan contains no cameras")
    logs = run_dir / "logs"
    logs.mkdir(exist_ok=True)
    native_output = logs / "native_combined"
    replay_path = Path(args.replay_manifest).expanduser().resolve()
    cache_manifest_path = Path(args.cache_manifest).expanduser().resolve()
    case_path = Path(args.case).expanduser().resolve()
    ue_project = Path(args.ue_project).expanduser().resolve()
    ue_executable = Path(args.ue_executable).expanduser().resolve()
    replay = read_json(replay_path)
    cache_manifest = read_json(cache_manifest_path)
    case_spec = dict(load_deformable_case_data(case_path))
    from harness.core.physics_contract import infer_scene_domain

    if infer_scene_domain(case_spec) != "deformable":
        raise SystemExit("deformable UE replay requires a deformable-domain scene contract")
    fps = int((replay.get("timebase") or {}).get("fps") or 0)
    frame_count = int((replay.get("timebase") or {}).get("frame_count") or 0)
    if fps <= 0 or frame_count <= 1 or len(replay.get("frames") or []) != frame_count:
        raise SystemExit("invalid deformable surface replay timebase")
    ensure_ue_surface_assets(
        replay,
        replay_manifest=replay_path,
        ue_project=ue_project,
        ue_executable=ue_executable,
        report_path=logs / "deformable_surface_import.json",
    )

    cache_frames = cache_manifest.get("frames") or []
    if len(cache_frames) != frame_count:
        raise SystemExit("deformable cache and replay frame counts differ")
    cache_path = cache_manifest_path.parent / str(cache_manifest["canonical_state"])
    surface_centers: list[list[float]] = []
    bounds_min = [float("inf")] * 3
    bounds_max = [float("-inf")] * 3
    for frame in cache_frames:
        bounds = frame.get("bounds_m") if isinstance(frame, dict) else None
        minimum = bounds.get("min") if isinstance(bounds, dict) else None
        maximum = bounds.get("max") if isinstance(bounds, dict) else None
        if not (
            isinstance(minimum, list)
            and isinstance(maximum, list)
            and len(minimum) == 3
            and len(maximum) == 3
        ):
            raise SystemExit("deformable cache frame is missing bounds_m")
        minimum = [float(value) for value in minimum]
        maximum = [float(value) for value in maximum]
        surface_centers.append([(minimum[axis] + maximum[axis]) / 2.0 for axis in range(3)])
        bounds_min = [min(bounds_min[axis], minimum[axis]) for axis in range(3)]
        bounds_max = [max(bounds_max[axis], maximum[axis]) for axis in range(3)]
    solid = optional_object_with_role(case_spec, "deformable_solid")
    surface_id = "soft_ball_surface" if solid else "cloth_surface"
    surface_color = SOLID_COLOR if solid else CLOTH_COLOR
    solver_backend = str(cache_manifest.get("backend") or "unknown_deformable_solver")
    cloth = required_object_with_role(case_spec, "deformable_cloth")
    bounds_center = [(bounds_min[axis] + bounds_max[axis]) / 2.0 for axis in range(3)]
    bounds_extent = [
        max((bounds_max[axis] - bounds_min[axis]) / 2.0 + margin, minimum)
        for axis, (margin, minimum) in enumerate(((0.6, 1.5), (0.6, 1.5), (0.5, 1.2)))
    ]
    duration_s = (frame_count - 1) / fps
    case_spec["scene"] = {
        **(case_spec.get("scene") or {}),
        "duration_s": duration_s,
        "map_preference": args.map,
        "scene_bounds": {"center": bounds_center, "extent": bounds_extent},
    }
    case_spec["timebase"] = {
        "physics_hz": fps,
        "render_fps": fps,
        "sample_phase": "external_solver_fixed_topology_frame_boundary",
        "endpoint_policy": "inclusive",
    }
    asset_paths = [
        f"{frame['ue_asset_path']}.{str(frame['ue_asset_path']).rsplit('/', 1)[-1]}"
        for frame in replay["frames"]
    ]
    cloth_material_path = str(cloth.get("visual_material_path") or "")
    declared_cloth_color = cloth.get("color_rgb")
    if isinstance(declared_cloth_color, list) and len(declared_cloth_color) == 3:
        surface_color = [float(value) for value in declared_cloth_color]
    cloth_object = runtime_object(
        surface_id,
        asset_paths[0],
        [0.0, 0.0, 0.0],
        [1.0, 1.0, 1.0],
        {
            "surface_mesh_sequence": asset_paths,
            "preserve_authored_scale": True,
            "disallow_nanite": True,
            "preserve_material": False,
            "generate_solid_material": not bool(cloth_material_path),
            "generated_material_name": "M_Harness_DeformableSolid_V1" if solid else "M_Harness_Cloth_Opaque_TwoSided_V1",
            "fixed_material_color": cloth.get("fixed_material_color") is not False,
            "two_sided_material": not bool(solid),
            "color_rgb": surface_color,
            **({"visual_material_path": cloth_material_path} if cloth_material_path else {}),
            "roughness": 0.42 if solid else 0.72,
            "metallic": 0.0,
            "emissive": 0.05,
            "segmentation_identity": surface_id,
        },
    )
    placement_path = run_dir / "runtime_actor_placement.json"
    if not placement_path.is_file():
        raise SystemExit("deformable UE replay requires compiled runtime_actor_placement.json")
    actor_placement = read_json(placement_path)
    placed_dynamic, placed_static = runtime_objects_from_actor_placement(actor_placement, case_spec)
    case_objects = {
        str(obj.get("id") or ""): obj
        for obj in case_spec.get("objects") or []
        if isinstance(obj, dict) and obj.get("id")
    }
    replay_props = []
    for obj in [*placed_dynamic, *placed_static]:
        object_id = str(obj.get("id") or "")
        case_object = case_objects.get(object_id) or {}
        role = str(case_object.get("role") or "")
        if role in {"deformable_cloth", "deformable_solid", "wind_field"}:
            continue
        configure_analytic_replay_prop(obj, case_object)
        properties = obj.setdefault("physics_properties", {})
        properties["simulate_physics"] = "force_off"
        properties["collision_enabled"] = False
        replay_props.append(obj)
    moving_props = [
        obj
        for obj in replay_props
        if isinstance((case_objects.get(str(obj.get("id") or "")) or {}).get("kinematic_motion"), dict)
    ]
    moving_ids = {str(obj.get("id") or "") for obj in moving_props}
    static_objects = [obj for obj in replay_props if str(obj.get("id") or "") not in moving_ids]
    trajectory = [
        {
            "frame": frame,
            "time": round(frame / fps, 8),
            "source": "external_solver_fixed_topology_mesh_cache",
            "objects": {
                surface_id: {
                    "position": [0.0, 0.0, 0.0],
                    "rotation_degrees": [0.0, 0.0, 0.0],
                    "velocity": [0.0, 0.0, 0.0],
                    "camera_position_m": [float(value) for value in surface_centers[frame]],
                    "source": "deformable_cache_vertex_mean",
                },
                **{
                    object_id: kinematic_replay_state(case_objects[object_id], frame / fps)
                    for object_id in sorted(moving_ids)
                },
            },
            "contacts": [],
        }
        for frame in range(frame_count)
    ]
    runtime_scene = {
        "schema_version": "studio_runtime_v1",
        "draft_id": case_spec["case_id"],
        "case_type": "llm_object_graph",
        "background_map": {"ue5_path": args.map},
        "prompt": case_spec.get("prompt", ""),
        "simulation": {
            "duration_s": duration_s,
            "fps": fps,
            "dt": 1.0 / fps,
            "render_fps": fps,
            "physics_hz": fps,
            "canonical_frame_count": frame_count,
            "solver_frame_count": frame_count,
            "endpoint_policy": "inclusive",
            "sample_phase": "external_solver_fixed_topology_frame_boundary",
        },
        "physics": {},
        "physics_controls": {
            "simulate_physics": False,
            "simulation_driver": "external_deformable_mesh_replay",
            "runtime_driver_backend": "precomputed_trajectory",
            "trajectory_source": solver_backend,
            "cpp_runtime_driver_enabled": False,
        },
        "render": {"width": width, "height": height, "fps": fps, "pass_mode": render_mode, "deterministic": True},
        "camera": camera_runtime_from_plan(camera_payload),
        "requested_views": requested_views,
        "map_lighting_controls": {
            "preset": "harness_rgb_editor_viewport",
            "visual_realism_profile": "editor_parity",
            "use_existing_map_lights": True,
            "spawn_directional_sun": True,
            "spawn_fill_light": False,
            "spawn_sky_light": True,
            "spawn_sky_atmosphere": True,
            "use_post_process": True,
            "fixed_auto_exposure": True,
            "stage_helpers": True,
            "map_backdrop_helpers": False,
            "capture_backend": "highres_viewport",
            "capture_source": "SCS_FINAL_COLOR_LDR",
            "video_filter": "",
        },
        "dynamic_objects": [cloth_object, *moving_props],
        "static_objects": static_objects,
        "validation_targets": [],
        "precomputed_trajectory": trajectory,
        "asset_policy": "external_fixed_topology_surface_replay",
    }
    studio_scene = {
        "schema_version": "harness_deformable_ue_scene_v1",
        "case_id": case_spec["case_id"],
        "background": {"ue5_path": args.map},
        "surface_replay_manifest": str(replay_path),
        "deformable_cache": str(cache_path),
    }
    runtime_path = logs / "studio_runtime_scene_combined.json"
    studio_path = run_dir / "studio_scene_spec.json"
    write_json(runtime_path, runtime_scene)
    write_json(studio_path, studio_scene)
    write_json(run_dir / "case_spec.json", case_spec)
    link_or_copy(cache_path, run_dir / "deformable_cache.npz")
    link_or_copy(cache_manifest_path, run_dir / "deformable_cache.json")
    link_or_copy(replay_path, run_dir / "deformable_surface_replay.json")
    source_verifier = cache_manifest_path.parent / "deformable_solver_verifier.json"
    link_or_copy(source_verifier, run_dir / "deformable_solver_verifier.json")
    resolved_assets = [
        {"object_id": surface_id, "selected_asset": {"asset_id": "solver_fixed_topology_mesh_sequence", "ue5_path": asset_paths[0], "sequence_frame_count": len(asset_paths), "proxy": True, "provenance": "deformable_cache.npz + deformable_surface_replay.json"}},
    ]
    for obj in [*static_objects, *moving_props]:
        resolved_assets.append({
            "object_id": obj["id"],
            "selected_asset": {
                "asset_id": obj["ue5_path"].split(".", 1)[0],
                "ue5_path": obj["ue5_path"],
                "proxy": False,
                "provenance": "Unreal Engine built-in asset",
            },
        })
    write_json(run_dir / "asset_resolution.json", {
        "schema_version": "asset_resolution_v1",
        "assets": resolved_assets,
        "quality_gate": {"reference_assets_ready": False, "local_preview_count": len(resolved_assets), "geometry_match": True, "reason": "solver-derived deformable mesh with UE-native support geometry, map, material, lighting, and sensors"},
    })

    command = [
        str(ue_executable),
        f"-project={ue_project}",
        "-RenderOffScreen",
        "-unattended",
        "-nosplash",
        "-NoScreenMessages",
        "-stdout",
        "-FullStdOutLogOutput",
        f"-ExecutePythonScript={ROOT / 'scripts' / 'native_ue_scene.py'}",
    ]
    environment = {
        "SCENE_MAP": args.map.split(".", 1)[0],
        "WIDTH": str(width),
        "HEIGHT": str(height),
        "FPS": str(fps),
        "DURATION": str(duration_s),
        "MULTI_VIEW": "1",
        "CANONICAL_MULTI_VIEW": "1",
        "RENDER_DATA_PASSES": "1" if {"depth", "segmentation"}.intersection(render_passes) else "0",
        "CHAOS_SIMULATION_ENABLED": "0",
        "SIM_STUDIO_UE_CAPTURE_BACKEND": "highres_viewport",
        "WORLD_MODEL_RENDER_PASS_MODE": render_mode,
        "KEEP_RENDER_FRAMES": os.environ.get("SIM_STUDIO_KEEP_RENDER_FRAMES", "0"),
        "VIDEO_CRF": "18",
        "VIDEO_PRESET": "fast",
        "RENDER_SURFACE_REPLAY_MATERIAL_WARMUP_SECONDS": "4.0" if profile.name == "smoke" else "0.5",
    }
    process = run_precompiled_ue_scene(
        command,
        studio_scene_path=studio_path,
        runtime_scene_path=runtime_path,
        native_output=native_output,
        environment=environment,
        timeout=1200,
    )
    if process.get("status") != "completed":
        write_json(run_dir / "deformable_ue_render_report.json", {"status": "failed", "process": process})
        return 2
    if "segmentation" in render_passes:
        quantization = quantize_native_instance_segmentation(native_output, width=width, height=height, fps=fps, required=True)
        if quantization.get("status") != "pass":
            write_json(run_dir / "deformable_ue_render_report.json", {"status": "failed", "failure_code": "F_SEGMENTATION_QUANTIZATION_FAILED", "segmentation_quantization": quantization})
            return 2
    render_config = {
        "schema_version": "render_config.v2.3",
        "mode": render_mode,
        "backend": "ue",
        "width": width,
        "height": height,
        "fps": fps,
        "views": requested_views,
        "passes": render_passes,
        "timebase": runtime_scene["simulation"],
        "execution_strategy": "external_solver_once_fixed_topology_cache_then_ue_mesh_replay",
    }
    report = standardize_native_output(run_dir, native_output, camera_payload, started, render_mode=render_mode, rgb_native_output=native_output, render_config=render_config, case_spec=case_spec, scene_spec=studio_scene)
    write_render_contract_artifacts(run_dir, backend="ue", case_id=case_spec["case_id"], camera_plan=camera_payload, render_passes=render_passes, allow_placeholders=False, source="external_deformable_ue_replay")
    sync = check_render_sync(run_dir, require_depth="depth" in render_passes, require_segmentation="segmentation" in render_passes, write=True)
    observability = verify_expected_color_observability(run_dir, expected_rgb=surface_color, view_ids=requested_views, write=True)
    solver_verifier = read_json(run_dir / "deformable_solver_verifier.json")
    ue_render_real = report.get("rgb_real_ue") is True and (
        sync.get("ue_render_real") is True or not complete_sensor_contract
    )
    verified = report.get("status") == "completed" and report.get("rgb_real_ue") is True and sync.get("status") == "pass" and observability.get("status") == "pass" and solver_verifier.get("status") == "pass"
    inputs = run_dir / "inputs"
    inputs.mkdir(exist_ok=True)
    write_json(inputs / "render_config.json", render_config)
    write_json(inputs / "case_spec.json", case_spec)
    readiness = {
        "schema_version": "harness_run_readiness_v1",
        "backend": "ue",
        "solver_backend": solver_backend,
        "case_id": case_spec["case_id"],
        "reference_ready": False,
        "local_preview_ready": verified,
        "publication_tier": "local_preview" if verified else "rejected",
        "physics_ready": solver_verifier.get("status") == "pass",
        "visual_ready": report.get("rgb_real_ue") is True and observability.get("status") == "pass",
        "sensor_ready": sync.get("status") == "pass",
        "ue_render_real": ue_render_real,
        "state_truth": "deformable_cache.npz",
        "render_representation": "deformable_surface_replay.json",
    }
    write_json(run_dir / "run_readiness.json", readiness)
    quality = evaluate_run(run_dir, write=True) if complete_sensor_contract else {"status": "not_required"}
    overall = ArtifactManager(run_dir).publish_run_overall() if complete_sensor_contract else {}
    if complete_sensor_contract and quality.get("status") != "pass":
        verified = False
    readiness.update({
        "local_preview_ready": verified,
        "publication_tier": "local_preview" if verified else "rejected",
        "quality_gate_passed": quality.get("status") in {"pass", "not_required"},
    })
    write_json(run_dir / "run_readiness.json", readiness)
    report.update({
        "schema_version": "harness_deformable_ue_render_report_v1",
        "status": "completed" if verified else "failed_verification",
        "solver_verifier_status": solver_verifier.get("status"),
        "render_sync_status": sync.get("status"),
        "rgb_observability_status": observability.get("status"),
        "quality_gate_status": quality.get("status"),
        "state_truth": "deformable_cache.npz",
        "surface_replay": "deformable_surface_replay.json",
        "solver_execution_count": 0,
        "render_adapter": "precomputed_external_fixed_topology_mesh_sequence",
        "ue_project": str(ue_project),
        "opened_map": args.map.split(".", 1)[0],
        "overall": {name: str(path.relative_to(run_dir)) for name, path in overall.items()},
    })
    write_json(run_dir / "deformable_ue_render_report.json", report)
    write_execution_reports(run_dir, profile, wall_seconds=time.perf_counter() - started, status="pass" if verified else "fail")
    print(json.dumps({"status": report["status"], "run_dir": str(run_dir), "view_count": len(requested_views), "frame_count": frame_count}, indent=2))
    return 0 if verified else 2


def optional_object_with_role(case_spec: dict[str, Any], role: str) -> dict[str, Any] | None:
    matches = [item for item in case_spec.get("objects") or [] if isinstance(item, dict) and item.get("role") == role]
    if len(matches) > 1:
        raise ValueError(f"case permits at most one object with role {role}")
    return matches[0] if matches else None


def required_object_with_role(case_spec: dict[str, Any], role: str) -> dict[str, Any]:
    matches = [item for item in case_spec.get("objects") or [] if isinstance(item, dict) and item.get("role") == role]
    if len(matches) != 1:
        raise ValueError(f"case requires exactly one object with role {role}")
    return matches[0]


def kinematic_replay_state(obj: dict[str, Any], time_s: float) -> dict[str, Any]:
    motion = obj["kinematic_motion"]
    position = sample_linear_kinematic_position(motion, time_s)
    next_time = min(float(motion["keyframes"][-1]["time_s"]), time_s + 1e-4)
    next_position = sample_linear_kinematic_position(motion, next_time)
    interval = next_time - time_s
    velocity = (
        [(next_position[axis] - position[axis]) / interval for axis in range(3)]
        if interval > 0.0
        else [0.0, 0.0, 0.0]
    )
    return {
        "position": position,
        "rotation_degrees": sample_linear_kinematic_rotation(
            motion,
            time_s,
            [float(value) for value in obj.get("initial_rotation_deg") or [0.0, 0.0, 0.0]],
        ),
        "velocity": velocity,
        "source": "declared_linear_kinematic_transform",
    }


def configure_analytic_replay_prop(runtime_obj: dict[str, Any], case_obj: dict[str, Any]) -> None:
    representation = case_obj.get("visual_representation") if isinstance(case_obj.get("visual_representation"), dict) else {}
    if representation.get("source") == "asset":
        params = runtime_obj.setdefault("params", {})
        if case_obj.get("visual_material_path"):
            params["preserve_material"] = False
            params["visual_material_path"] = str(case_obj["visual_material_path"])
        elif case_obj.get("fixed_material_color") is True and case_obj.get("color_rgb"):
            params.update({
                "preserve_material": False,
                "generate_solid_material": True,
                "generated_material_name": f"M_Harness_DeformableProp_{case_obj.get('id')}",
                "fixed_material_color": True,
                "color_rgb": case_obj["color_rgb"],
                "roughness": 0.38,
                "metallic": 0.45,
            })
        return
    shape = str(case_obj.get("shape") or "").casefold()
    size = case_obj.get("size_m")
    if shape not in {"sphere", "box", "cylinder"} or not isinstance(size, list) or len(size) != 3:
        return
    mesh_path = {
        "sphere": "/Engine/BasicShapes/Sphere.Sphere",
        "box": "/Engine/BasicShapes/Cube.Cube",
        "cylinder": "/Engine/BasicShapes/Cylinder.Cylinder",
    }[shape]
    scale = [float(value) for value in size]
    position = [float(value) for value in case_obj.get("initial_position_m") or [0.0, 0.0, 0.0]]
    if case_obj.get("role") == "support_surface":
        position[2] -= scale[2] / 2.0
    runtime_obj["ue5_path"] = mesh_path
    runtime_obj["scale"] = scale
    runtime_obj["initial_position_m"] = position
    runtime_obj["rotation_degrees"] = [
        float(value) for value in case_obj.get("initial_rotation_deg") or [0.0, 0.0, 0.0]
    ]
    params = runtime_obj.setdefault("params", {})
    params.update({
        "preserve_authored_scale": True,
        "preserve_material": False,
        "generate_solid_material": not bool(case_obj.get("visual_material_path")),
        "generated_material_name": f"M_Harness_DeformableProp_{case_obj.get('id')}",
        "fixed_material_color": case_obj.get("fixed_material_color") is not False,
        "color_rgb": case_obj.get("color_rgb") or [0.16, 0.19, 0.24],
        "roughness": 0.38,
        "metallic": 0.45,
        "expected_render_mesh_path": mesh_path,
    })
    if case_obj.get("visual_material_path"):
        params["visual_material_path"] = str(case_obj["visual_material_path"])


def runtime_object(object_id: str, asset_path: str, position: list[float], scale: list[float], params: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": object_id,
        "asset_key": object_id,
        "asset_name": object_id,
        "ue5_path": asset_path,
        "category_l1": "harness",
        "category_l2": "deformable_surface_replay",
        "class_name": "StaticMesh",
        "asset_kind": "static_mesh",
        "render_usage": "runtime_static_mesh",
        "runtime_spawnable": True,
        "behavior": "static_prop",
        "initial_position_m": position,
        "scale": scale,
        "physics_properties": {"simulate_physics": "force_off", "collision_enabled": False},
        "params": params,
    }


if __name__ == "__main__":
    raise SystemExit(main())
