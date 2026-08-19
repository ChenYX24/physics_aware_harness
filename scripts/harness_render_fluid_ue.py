from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.artifact_manager import ArtifactManager
from harness.core.artifact_schema import read_json, write_json
from harness.core.runtime_case import load_runtime_case
from harness.runtime.fluid_surface_adapter import particle_centers_m
from harness.runtime.observation_planner import camera_ids_from_observation_plan, render_passes_from_observation_plan
from harness.runtime.render_pass_contract import write_render_contract_artifacts
from harness.runtime.execution_profile import (
    execution_profile,
    execution_profile_names,
    write_execution_reports,
)
from harness.verification.render_sync_checker import check_render_sync
from harness.verification.physics_verifier import PhysicsVerifier
from harness.verification.rgb_observability import verify_expected_color_observability
from harness.verification.run_quality import evaluate_run
from scripts.harness_local_ue_runner import (
    DEFAULT_UE_EXECUTABLE,
    camera_runtime_from_plan,
    quantize_native_instance_segmentation,
    run_ue_until_artifacts,
    standardize_native_output,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a Genesis surface sequence through native UE RGB/depth/segmentation capture.")
    parser.add_argument("replay_manifest")
    parser.add_argument("--particle-cache", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--ue-project", required=True)
    parser.add_argument("--ue-executable", default=str(DEFAULT_UE_EXECUTABLE))
    parser.add_argument("--map", default="/Game/Maps/MarketEnvironment/Maps/Day.Day")
    parser.add_argument("--basin-asset")
    parser.add_argument("--basin-geometry", choices=("rectangular", "round"))
    parser.add_argument("--basin-scale", type=float)
    parser.add_argument("--basin-scale-xyz", type=float, nargs=3)
    parser.add_argument("--basin-pivot-to-rim-m", type=float)
    parser.add_argument("--scene-z-offset-m", type=float, default=-0.05)
    parser.add_argument("--profile", choices=execution_profile_names(), default="candidate")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    args = parser.parse_args()

    started = time.perf_counter()
    profile = execution_profile(args.profile)
    width = int(args.width or profile.width)
    height = int(args.height or profile.height)
    render_mode = profile.render_mode
    run_dir = Path(args.run_dir).expanduser().resolve()
    native_output = run_dir / "logs" / "native_combined"
    run_dir.mkdir(parents=True, exist_ok=True)
    observation_plan = read_json(run_dir / "observation_plan.json")
    camera_plan_payload = read_json(run_dir / "camera_plan.json")
    requested_views = camera_ids_from_observation_plan(observation_plan)
    render_passes = render_passes_from_observation_plan(observation_plan)
    if not requested_views:
        raise SystemExit("compiled Observation Plan contains no cameras")
    ue_project = Path(args.ue_project).expanduser().resolve()
    ue_executable = Path(args.ue_executable).expanduser().resolve()
    replay = read_json(Path(args.replay_manifest))
    particle_cache = read_json(Path(args.particle_cache))
    environment = particle_cache.get("environment") if isinstance(particle_cache.get("environment"), dict) else {}
    basin_scale_xyz = args.basin_scale_xyz or ([args.basin_scale] * 3 if args.basin_scale is not None else None)
    rigid_scene_mode = environment.get("type") == "rigid_sph_scene"
    solver_basin_geometry = (
        "declared_rigid_bodies"
        if rigid_scene_mode
        else "round" if environment.get("type") == "cylindrical_basin" else "rectangular"
    )
    if rigid_scene_mode:
        declared_rigid_bodies = [item for item in environment.get("rigid_bodies") or [] if isinstance(item, dict)]
        asset_geometry_match = bool(declared_rigid_bodies) and all(
            isinstance(item, dict)
            and ((item.get("collision") or {}).get("asset_geometry_match") is True)
            and str(((item.get("asset") or {}).get("ue_path") or "")).startswith("/Game/")
            and len((item.get("transform") or {}).get("position_m") or []) == 3
            and len((item.get("transform") or {}).get("ue_rotation_pyr_deg") or []) == 3
            and len((item.get("transform") or {}).get("scale") or []) == 3
            and all(float(value) > 0.0 for value in ((item.get("transform") or {}).get("scale") or []))
            for item in declared_rigid_bodies
        )
    else:
        declared_rigid_bodies = []
        if not all((args.basin_asset, args.basin_geometry, basin_scale_xyz, args.basin_pivot_to_rim_m)):
            raise SystemExit("legacy basin replay requires --basin-asset/geometry/scale-or-scale-xyz/pivot-to-rim-m")
        asset_geometry_match = args.basin_geometry == solver_basin_geometry
    case = load_runtime_case(ROOT / args.case if not Path(args.case).is_absolute() else args.case)
    case_spec = dict(case.data)
    fluid_visual = fluid_visual_parameters(case_spec)
    fps = int((replay.get("timebase") or {}).get("fps") or 0)
    frame_count = int((replay.get("timebase") or {}).get("frame_count") or 0)
    if fps <= 0 or frame_count <= 0 or len(replay.get("frames") or []) != frame_count:
        raise SystemExit("invalid fluid surface replay timebase")
    ensure_ue_surface_assets(
        replay,
        replay_manifest=Path(args.replay_manifest).expanduser().resolve(),
        ue_project=ue_project,
        ue_executable=ue_executable,
        report_path=run_dir / "logs" / "fluid_surface_import.json",
    )
    duration_s = (frame_count - 1) / fps
    scene_bounds = (
        replay_scene_bounds(environment, render_z_offset_m=args.scene_z_offset_m)
        if rigid_scene_mode
        else {
            "center": [-0.24, 0.0, 0.35 + args.scene_z_offset_m],
            "extent": [0.55, 0.55, 0.7],
        }
    )
    case_spec["scene"] = {
        **(case_spec.get("scene") or {}),
        "duration_s": duration_s,
        "map_preference": args.map,
        "scene_bounds": scene_bounds,
    }
    case_spec["timebase"] = {
        "physics_hz": fps,
        "render_fps": fps,
        "sample_phase": "genesis_surface_frame_boundary",
        "endpoint_policy": "inclusive",
    }
    asset_paths = [
        f"{frame['ue_asset_path']}.{str(frame['ue_asset_path']).rsplit('/', 1)[-1]}"
        for frame in replay["frames"]
    ]
    basin_floor_material = "/Engine/BasicShapes/BasicShapeMaterial.BasicShapeMaterial"
    basin_wall_material = basin_floor_material
    particle_centers = [offset_z(value, args.scene_z_offset_m) for value in particle_centers_m(particle_cache)]
    if len(particle_centers) != frame_count:
        raise SystemExit("particle cache and surface replay frame counts differ")
    rigid_specs = (
        (particle_cache.get("environment") or {}).get("rigid_objects")
        if isinstance((particle_cache.get("environment") or {}).get("rigid_objects"), list)
        else []
    )
    rigid_runtime_objects = []
    for item in rigid_specs:
        if item.get("expected_response") == "float":
            visual_asset = "/Game/Maps/UrbanDowntown/Meshes/SoccerBall.SoccerBall"
            authored_diameter_m = 0.7134
        else:
            visual_asset = "/Game/Props/Decorative/SM_8Ball.SM_8Ball"
            authored_diameter_m = 0.1742
        visual_scale = 2.0 * float(item["radius_m"]) / authored_diameter_m
        rigid_runtime_objects.append(
            runtime_object(
                str(item["id"]),
                visual_asset,
                offset_z(item["position_m"], args.scene_z_offset_m),
                [visual_scale] * 3,
                {
                    "preserve_authored_scale": True,
                    "preserve_material": False,
                    "generate_solid_material": True,
                    "generated_material_name": f"M_Harness_FluidRigid_{str(item['id'])}_V2_DeepTank",
                    "fixed_material_color": True,
                    "color_rgb": list(item["visual_color_rgba"][:3]),
                    "roughness": 0.42 if item.get("expected_response") == "float" else 0.20,
                    "metallic": 0.0 if item.get("expected_response") == "float" else 0.82,
                    "emissive": 0.05,
                    "segmentation_identity": str(item["id"]),
                },
            )
        )
    declared_dynamic_runtime, declared_static_runtime = (
        rigid_body_runtime_objects(particle_cache, render_z_offset_m=args.scene_z_offset_m)
        if rigid_scene_mode
        else ([], [])
    )
    precomputed_trajectory = [
        {
            "frame": index,
            "time": round(index / fps, 8),
            "source": "genesis_sph_surface_replay",
            "objects": {
                "fluid_surface": {
                    "position": [0.0, 0.0, args.scene_z_offset_m],
                    "rotation": [0.0, 0.0, 0.0],
                    "velocity": [0.0, 0.0, 0.0],
                    # OBJ vertices are already world-space, so the actor only
                    # receives the shared map-ground offset. Dynamic cameras consume this separate
                    # particle-truth center and must not translate the mesh.
                    "camera_position_m": particle_centers[index],
                    "source": "particle_cache_surface_frame",
                },
                **{
                    str(item["id"]): {
                        "position": offset_z(
                            particle_cache["frames"][index]["rigid_objects"][str(item["id"])]["position_m"],
                            args.scene_z_offset_m,
                        ),
                        "rotation_degrees": [0.0, 0.0, 0.0],
                        "velocity": list(particle_cache["frames"][index]["rigid_objects"][str(item["id"])]["velocity_m_s"]),
                        "source": "genesis_rigid_sph_frame",
                    }
                    for item in rigid_specs
                },
                **rigid_body_trajectory_objects(
                    particle_cache["frames"][index],
                    declared_rigid_bodies,
                    render_z_offset_m=args.scene_z_offset_m,
                ),
            },
            "contacts": [],
        }
        for index in range(frame_count)
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
            "sample_phase": "genesis_surface_frame_boundary",
        },
        "physics": case_spec.get("physical_parameters") or {},
        "physics_controls": {
            "simulate_physics": False,
            "simulation_driver": "genesis_sph_surface_mesh_replay",
            "runtime_driver_backend": "precomputed_trajectory",
            "trajectory_source": "genesis_sph",
            "cpp_runtime_driver_enabled": False,
        },
        "render": {"width": width, "height": height, "fps": fps, "pass_mode": render_mode, "deterministic": True},
        "camera": camera_runtime_from_plan(camera_plan_payload),
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
        "dynamic_objects": [
            runtime_object(
                "fluid_surface",
                asset_paths[0],
                [0.0, 0.0, args.scene_z_offset_m],
                [1.0, 1.0, 1.0],
                {
                    "surface_mesh_sequence": asset_paths,
                    "preserve_authored_scale": True,
                    "disallow_nanite": True,
                    "preserve_material": False,
                    "generate_solid_material": True,
                    "generated_material_name": fluid_visual["generated_material_name"],
                    "fixed_material_color": True,
                    "two_sided_material": True,
                    "color_rgb": fluid_visual["color_rgb"],
                    "roughness": fluid_visual["roughness"],
                    "metallic": 0.0,
                    "emissive": fluid_visual["emissive"],
                    "segmentation_identity": "fluid_surface",
                },
            ),
            *declared_dynamic_runtime,
            *rigid_runtime_objects,
        ],
        "static_objects": (
            declared_static_runtime
            if rigid_scene_mode
            else basin_runtime_objects(
                particle_cache,
                basin_floor_material,
                basin_wall_material,
                asset_path=args.basin_asset,
                asset_scale=basin_scale_xyz,
                pivot_to_rim_m=float(args.basin_pivot_to_rim_m),
                render_z_offset_m=args.scene_z_offset_m,
            )
        ),
        "validation_targets": [],
        "precomputed_trajectory": precomputed_trajectory,
        "asset_policy": "genesis_surface_replay_imported_static_mesh_sequence",
    }
    studio_scene = {
        "schema_version": "harness_fluid_ue_scene_v1",
        "case_id": case_spec["case_id"],
        "background": {"ue5_path": args.map},
        "surface_replay_manifest": str(Path(args.replay_manifest).resolve()),
        "particle_cache": str(Path(args.particle_cache).resolve()),
        "render_assets": {
            "rigid_bodies": [str((item.get("asset") or {}).get("ue_path")) for item in declared_rigid_bodies],
            "basin": None if rigid_scene_mode else args.basin_asset,
            "rigid_objects": [item["ue5_path"] for item in rigid_runtime_objects],
        },
    }
    logs = run_dir / "logs"
    logs.mkdir(exist_ok=True)
    runtime_path = logs / "studio_runtime_scene_combined.json"
    studio_path = run_dir / "studio_scene_spec.json"
    write_json(runtime_path, runtime_scene)
    write_json(studio_path, studio_scene)
    write_json(run_dir / "case_spec.json", case_spec)
    particle_cache_path = Path(args.particle_cache).expanduser().resolve()
    copy_file_if_different(particle_cache_path, run_dir / "particle_cache.json")
    source_surface_frames = particle_cache_path.parent / "surface_frames"
    if not source_surface_frames.is_dir():
        raise SystemExit(f"particle cache surface_frames directory is missing: {source_surface_frames}")
    if source_surface_frames.resolve() != (run_dir / "surface_frames").resolve():
        shutil.copytree(source_surface_frames, run_dir / "surface_frames", dirs_exist_ok=True)
    copy_file_if_different(Path(args.replay_manifest), run_dir / "fluid_surface_replay.json")
    rigid_body_resolution_entries = (
        rigid_body_asset_resolution_entries(declared_rigid_bodies)
        if rigid_scene_mode
        else [
            {
                "object_id": "basin",
                "selected_asset": {
                    "asset_id": str(args.basin_asset).rsplit(".", 1)[0],
                    "ue5_path": args.basin_asset,
                    "proxy": False,
                    "provenance": "mounted AgenticDataPlatform UE asset catalog",
                    "solver_geometry": solver_basin_geometry,
                    "asset_geometry": args.basin_geometry,
                    "scale_xyz": basin_scale_xyz,
                    "asset_geometry_match": asset_geometry_match,
                },
            }
        ]
    )
    replay_asset_resolution = {
        "schema_version": "asset_resolution_v1",
        "assets": [
            {
                "object_id": "fluid_surface",
                "selected_asset": {
                    "asset_id": "genesis_splashsurf_static_mesh_sequence",
                    "ue5_path": asset_paths[0],
                    "sequence_frame_count": len(asset_paths),
                    "proxy": True,
                    "provenance": "particle_cache.json + fluid_surface_replay.json",
                },
            },
            *rigid_body_resolution_entries,
            *[
                {
                    "object_id": item["id"],
                    "selected_asset": {
                        "asset_id": item["ue5_path"].rsplit(".", 1)[0],
                        "ue5_path": item["ue5_path"],
                        "proxy": False,
                        "provenance": "mounted AgenticDataPlatform UE asset catalog",
                    },
                }
                for item in rigid_runtime_objects
            ],
        ],
        "quality_gate": {
            "reference_assets_ready": False,
            "local_preview_count": (len(declared_rigid_bodies) if rigid_scene_mode else 1) + len(rigid_runtime_objects),
            "geometry_match": asset_geometry_match,
            "reason": "real UE scene assets with a derived fluid surface; particle cache remains state truth",
        },
    }
    asset_resolution_path = run_dir / "asset_resolution.json"
    write_json(
        run_dir / "ue_replay_asset_resolution.json" if asset_resolution_path.is_file() else asset_resolution_path,
        replay_asset_resolution,
    )

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
    env = os.environ.copy()
    env.update(
        {
            "OUTPUT_DIR": str(native_output),
            "SCENE_SPEC": str(studio_path),
            "SCENE_RUNTIME_JSON": str(runtime_path),
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
            # Pay the shader/material cold-start cost in the disposable smoke
            # profile. Candidate/publish runs reuse the warmed UE assets and
            # should not add four idle seconds per run.
            "RENDER_SURFACE_REPLAY_MATERIAL_WARMUP_SECONDS": (
                "4.0" if profile.name == "smoke" else "0.5"
            ),
        }
    )
    result = run_ue_until_artifacts(command, env=env, native_output=native_output, timeout=1200)
    if result.get("status") != "completed":
        write_json(run_dir / "fluid_ue_render_report.json", {"status": "failed", "process": result})
        print(json.dumps({"status": "failed", "run_dir": str(run_dir)}, indent=2))
        return 2
    if "segmentation" in render_passes:
        quantization = quantize_native_instance_segmentation(
            native_output,
            width=width,
            height=height,
            fps=fps,
            required=True,
            required_object_ids=set(),
        )
        if quantization.get("status") != "pass":
            write_json(
                run_dir / "fluid_ue_render_report.json",
                {
                    "status": "failed",
                    "failure_code": "F_SEGMENTATION_QUANTIZATION_FAILED",
                    "segmentation_quantization": quantization,
                },
            )
            print(json.dumps({"status": "failed", "run_dir": str(run_dir)}, indent=2))
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
        "execution_strategy": "genesis_once_surface_reconstruction_then_ue_mesh_replay",
    }
    report = standardize_native_output(
        run_dir,
        native_output,
        camera_plan_payload,
        started,
        render_mode=render_mode,
        rgb_native_output=native_output,
        render_config=render_config,
        case_spec=case_spec,
        scene_spec=studio_scene,
    )
    native_summary = read_json(native_output / "summary.json")
    pose_registration = runtime_pose_registration_report(
        native_summary,
        [str(body.get("id") or "") for body in declared_rigid_bodies],
    ) if rigid_scene_mode else {
        "schema_version": "harness_runtime_pose_registration_report_v1",
        "status": "not_required",
        "required_object_ids": [],
        "objects": {},
        "failures": [],
    }
    write_json(run_dir / "runtime_pose_registration_report.json", pose_registration)
    write_render_contract_artifacts(
        run_dir,
        backend="ue",
        case_id=case_spec["case_id"],
        camera_plan=camera_plan_payload,
        render_passes=render_passes,
        allow_placeholders=False,
        source="genesis_surface_ue_replay",
    )
    render_sync = check_render_sync(
        run_dir,
        require_depth="depth" in render_passes,
        require_segmentation="segmentation" in render_passes,
        write=True,
    )
    verifier = PhysicsVerifier().verify_run_dir(run_dir, write=True)
    rgb_observability = verify_expected_color_observability(
        run_dir,
        expected_rgb=fluid_visual["color_rgb"],
        view_ids=requested_views,
        write=True,
    )
    verified = (
        report.get("status") == "completed"
        and verifier.get("status") == "pass"
        and render_sync.get("status") == "pass"
        and rgb_observability.get("status") == "pass"
        and asset_geometry_match
        and pose_registration.get("status") in {"pass", "not_required"}
    )
    write_json(
        run_dir / "run_readiness.json",
        {
            "schema_version": "harness_run_readiness_v1",
            "backend": "ue",
            "solver_backend": "genesis_sph",
            "case_id": case_spec["case_id"],
            "reference_ready": False,
            "local_preview_ready": verified,
            "publication_tier": "local_preview" if verified else "rejected",
            "physics_ready": verifier.get("status") == "pass",
            "visual_ready": report.get("rgb_real_ue") is True and rgb_observability.get("status") == "pass",
            "sensor_ready": render_sync.get("status") == "pass",
            "ue_render_real": render_sync.get("ue_render_real") is True,
            "depth_source": render_sync.get("depth_source"),
            "multi_view_sync_ok": render_sync.get("multi_view_sync_ok") is True,
            "state_truth": "particle_cache.json",
            "render_representation": "fluid_surface_replay.json",
            "asset_geometry_match": asset_geometry_match,
            "runtime_pose_registration_ready": pose_registration.get("status") in {"pass", "not_required"},
        },
    )
    inputs_dir = run_dir / "inputs"
    inputs_dir.mkdir(exist_ok=True)
    write_json(inputs_dir / "render_config.json", render_config)
    write_json(inputs_dir / "case_spec.json", case_spec)
    quality = evaluate_run(run_dir, write=True) if profile.complete_sensor_contract else {"status": "not_required"}
    if profile.complete_sensor_contract and quality.get("status") != "pass":
        verified = False
    readiness = read_json(run_dir / "run_readiness.json")
    readiness.update(
        {
            "local_preview_ready": verified,
            "publication_tier": "local_preview" if verified else "rejected",
            "visual_ready": report.get("rgb_real_ue") is True and rgb_observability.get("status") == "pass",
            "sensor_ready": render_sync.get("status") == "pass",
            "quality_gate_passed": quality.get("status") in {"pass", "not_required"},
        }
    )
    write_json(run_dir / "run_readiness.json", readiness)
    report.update(
        {
            "schema_version": "harness_fluid_ue_render_report_v1",
            "status": "completed" if verified else "failed_verification",
            "physics_verifier_status": verifier.get("status"),
            "render_sync_status": render_sync.get("status"),
            "rgb_observability_status": rgb_observability.get("status"),
            "quality_gate_status": quality.get("status"),
            "runtime_pose_registration_status": pose_registration.get("status"),
            "state_truth": "particle_cache.json",
            "surface_replay": "fluid_surface_replay.json",
            "solver_execution_count": 0,
            "render_adapter": "precomputed_genesis_surface_mesh_sequence",
            "ue_project": str(ue_project),
            "opened_map": args.map.split(".", 1)[0],
            "real_3d_assets": [
                *([str((item.get("asset") or {}).get("ue_path")) for item in declared_rigid_bodies] if rigid_scene_mode else [args.basin_asset]),
                *[item["ue5_path"] for item in rigid_runtime_objects],
            ],
            "scene_z_offset_m": args.scene_z_offset_m,
            "asset_geometry_match": asset_geometry_match,
            "solver_basin_geometry": solver_basin_geometry,
        }
    )
    write_json(run_dir / "fluid_ue_render_report.json", report)
    write_execution_reports(
        run_dir,
        profile,
        wall_seconds=time.perf_counter() - started,
        status="pass" if verified else "fail",
    )
    print(json.dumps({"status": report["status"], "run_dir": str(run_dir), "view_count": len(requested_views), "frame_count": frame_count}, indent=2))
    return 0 if report["status"] == "completed" else 2


def surface_import_fingerprint(replay: dict) -> str:
    payload = {
        "state_truth_sha256": str(replay.get("state_truth_sha256") or ""),
        "frames": [
            [str(frame.get("ue_asset_path") or ""), str(frame.get("sha256") or "")]
            for frame in replay.get("frames") or []
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def ue_asset_package_file(ue_project: Path, ue_asset_path: str) -> Path:
    package = str(ue_asset_path).split(".", 1)[0]
    if not package.startswith("/Game/"):
        raise ValueError(f"fluid surface asset must be under /Game: {ue_asset_path}")
    return ue_project.parent / "Content" / f"{package.removeprefix('/Game/')}.uasset"


def ensure_ue_surface_assets(
    replay: dict,
    *,
    replay_manifest: Path,
    ue_project: Path,
    ue_executable: Path,
    report_path: Path,
) -> dict:
    asset_root = str((replay.get("ue") or {}).get("asset_root") or "").rstrip("/")
    if not asset_root.startswith("/Game/"):
        raise SystemExit(f"invalid UE fluid asset root: {asset_root}")
    fingerprint = surface_import_fingerprint(replay)
    cache_key = hashlib.sha256(asset_root.encode("utf-8")).hexdigest()[:16]
    cache_path = ue_project.parent / "Saved" / "HarnessFluidImports" / f"{cache_key}.json"
    asset_files = [ue_asset_package_file(ue_project, str(frame.get("ue_asset_path") or "")) for frame in replay.get("frames") or []]
    cached = read_json(cache_path) if cache_path.is_file() else {}
    if asset_files and all(path.is_file() for path in asset_files) and cached.get("surface_import_fingerprint") == fingerprint:
        report = {
            **cached,
            "status": "reused",
            "report_role": "content-addressed UE fluid surface import cache",
        }
        write_json(report_path, report)
        return report

    report_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "SIM_STUDIO_FLUID_REPLAY_MANIFEST": str(replay_manifest),
            "SIM_STUDIO_FLUID_IMPORT_REPORT": str(report_path),
        }
    )
    command = [
        str(ue_executable),
        f"-project={ue_project}",
        "-RenderOffScreen",
        "-unattended",
        "-nosplash",
        "-NoScreenMessages",
        "-stdout",
        "-FullStdOutLogOutput",
        f"-ExecutePythonScript={ROOT / 'scripts' / 'import_ue_fluid_surface_sequence.py'}",
    ]
    report_path.unlink(missing_ok=True)
    run_ue_import_until_report(command, env=env, report_path=report_path, asset_files=asset_files)
    report = read_json(report_path)
    if report.get("status") != "completed" or not all(path.is_file() for path in asset_files):
        raise SystemExit("UE fluid surface import did not materialize every declared mesh")
    report.update(
        {
            "surface_import_fingerprint": fingerprint,
            "state_truth_sha256": str(replay.get("state_truth_sha256") or ""),
            "report_role": "content-addressed UE fluid surface import cache",
        }
    )
    write_json(report_path, report)
    write_json(cache_path, report)
    return report


def run_ue_import_until_report(
    command: list[str],
    *,
    env: dict[str, str],
    report_path: Path,
    asset_files: list[Path],
    timeout_s: float = 600.0,
) -> None:
    stdout_path = report_path.with_name(f"{report_path.stem}_stdout.log")
    stderr_path = report_path.with_name(f"{report_path.stem}_stderr.log")
    deadline = time.monotonic() + timeout_s
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, env=env, text=True, stdout=stdout, stderr=stderr)
        while time.monotonic() < deadline:
            report_complete = False
            if report_path.is_file():
                try:
                    report_complete = read_json(report_path).get("status") == "completed"
                except Exception:
                    report_complete = False
            if report_complete and all(path.is_file() for path in asset_files):
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=15)
                return
            return_code = process.poll()
            if return_code is not None:
                raise SystemExit(f"UE fluid surface import exited before a complete report ({return_code}); see {stderr_path}")
            time.sleep(0.25)
        process.terminate()
        raise SystemExit(f"UE fluid surface import timed out after {timeout_s:.0f}s; see {stderr_path}")


def offset_z(position: list[float], amount: float) -> list[float]:
    return [float(position[0]), float(position[1]), float(position[2]) + amount]


def copy_file_if_different(source: Path, destination: Path) -> None:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if source == destination:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def replay_scene_bounds(environment: dict, *, render_z_offset_m: float) -> dict:
    bounds = environment.get("workspace_bounds_m") if isinstance(environment.get("workspace_bounds_m"), dict) else {}
    lower = bounds.get("min_m") if isinstance(bounds.get("min_m"), list) else []
    upper = bounds.get("max_m") if isinstance(bounds.get("max_m"), list) else []
    if len(lower) == 3 and len(upper) == 3:
        lower_values = [float(value) for value in lower]
        upper_values = [float(value) for value in upper]
        if all(math.isfinite(value) for value in lower_values + upper_values) and all(
            high > low for low, high in zip(lower_values, upper_values, strict=True)
        ):
            center = [(low + high) / 2.0 for low, high in zip(lower_values, upper_values, strict=True)]
            center[2] += render_z_offset_m
            return {
                "center": center,
                "extent": [(high - low) / 2.0 for low, high in zip(lower_values, upper_values, strict=True)],
            }
    raise ValueError("rigid surface replay requires finite workspace_bounds_m with positive extents")


def fluid_visual_parameters(case_spec: dict) -> dict:
    fluid = next(
        (
            item
            for item in case_spec.get("objects") or []
            if isinstance(item, dict) and str(item.get("role") or "") in {"fluid", "fluid_volume"}
        ),
        {},
    )
    visual = fluid.get("visual") if isinstance(fluid.get("visual"), dict) else {}
    color = visual.get("color_rgb", [0.03, 0.30, 0.78])
    if not isinstance(color, list) or len(color) != 3:
        raise ValueError("fluid visual color_rgb must contain three values")
    return {
        "generated_material_name": f"M_Harness_FluidSurface_{str(case_spec.get('case_id') or 'Fluid')}_V1",
        "color_rgb": [float(value) for value in color],
        "roughness": float(visual.get("roughness", 0.12)),
        "emissive": float(visual.get("emissive", 0.18)),
    }


def runtime_pose_registration_report(
    native_summary: dict,
    required_object_ids: list[str],
    *,
    tolerance_cm: float = 0.1,
) -> dict:
    registrations = (
        native_summary.get("runtime_pose_registrations")
        if isinstance(native_summary.get("runtime_pose_registrations"), dict)
        else {}
    )
    failures = []
    objects = {}
    for object_id in required_object_ids:
        registration = registrations.get(object_id)
        if not isinstance(registration, dict):
            failures.append({"object_id": object_id, "code": "pose_registration_missing"})
            continue
        residual = float(registration.get("max_residual_cm", registration.get("residual_cm", float("inf"))))
        objects[object_id] = registration
        if registration.get("anchor") != "bounds_center":
            failures.append({"object_id": object_id, "code": "pose_anchor_invalid"})
        if not math.isfinite(residual) or residual > tolerance_cm:
            failures.append(
                {
                    "object_id": object_id,
                    "code": "pose_registration_residual_exceeded",
                    "residual_cm": residual,
                }
            )
    return {
        "schema_version": "harness_runtime_pose_registration_report_v1",
        "status": "fail" if failures else "pass",
        "anchor": "bounds_center",
        "tolerance_cm": tolerance_cm,
        "required_object_ids": required_object_ids,
        "objects": objects,
        "failures": failures,
    }


def runtime_object(object_id: str, path: str, position: list[float], scale: list[float], params: dict) -> dict:
    return {
        "id": object_id,
        "asset_key": object_id,
        "asset_name": object_id,
        "ue5_path": path,
        "category_l1": "harness",
        "category_l2": "fluid_surface_replay",
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


def rigid_body_runtime_objects(cache: dict, *, render_z_offset_m: float) -> tuple[list[dict], list[dict]]:
    environment = cache.get("environment") if isinstance(cache.get("environment"), dict) else {}
    dynamic_objects: list[dict] = []
    static_objects: list[dict] = []
    for body in environment.get("rigid_bodies") or []:
        if not isinstance(body, dict):
            continue
        asset = body.get("asset") if isinstance(body.get("asset"), dict) else {}
        transform = body.get("transform") if isinstance(body.get("transform"), dict) else {}
        if len(transform.get("position_m") or []) != 3 or len(transform.get("ue_rotation_pyr_deg") or []) != 3:
            raise ValueError(f"declared rigid body is missing an explicit UE transform: {body.get('id')}")
        scale = transform.get("scale") or []
        if len(scale) != 3 or any(float(value) <= 0.0 for value in scale):
            raise ValueError(f"declared rigid body has invalid scale: {body.get('id')}")
        runtime = runtime_object(
            str(body.get("id") or "rigid_body"),
            str(asset.get("ue_path") or ""),
            offset_z(transform.get("position_m") or [0.0, 0.0, 0.0], render_z_offset_m),
            [float(value) for value in scale],
            {
                "base_rotation_degrees": (
                    [0.0, 0.0, 0.0]
                    if body.get("mobility") in {"kinematic", "dynamic"}
                    else list(transform["ue_rotation_pyr_deg"])
                ),
                "preserve_authored_scale": True,
                # Solver rigid-body poses are expressed at the collision/body
                # center. Imported meshes may retain an arbitrary FBX authoring
                # pivot, so the UE replay must register their visible bounds to
                # that backend-neutral pose anchor.
                "pose_anchor": "bounds_center",
                "preserve_material": True,
                "visual_material_path": str(asset.get("material_path") or ""),
                "segmentation_identity": str(body.get("id") or "rigid_body"),
                "asset_geometry_match": ((body.get("collision") or {}).get("asset_geometry_match") is True),
            },
        )
        (dynamic_objects if body.get("mobility") in {"kinematic", "dynamic"} else static_objects).append(runtime)
    return dynamic_objects, static_objects


def rigid_body_trajectory_objects(
    frame: dict,
    bodies: list[dict],
    *,
    render_z_offset_m: float,
) -> dict[str, dict]:
    states = frame.get("rigid_objects") if isinstance(frame.get("rigid_objects"), dict) else {}
    result: dict[str, dict] = {}
    for body in bodies:
        if body.get("mobility") not in {"kinematic", "dynamic"}:
            continue
        body_id = str(body.get("id") or "")
        state = states.get(body_id) if isinstance(states.get(body_id), dict) else {}
        required_fields = ["position_m", "ue_rotation_pyr_deg"]
        if body.get("mobility") == "dynamic":
            required_fields.extend(("linear_velocity_m_s", "angular_velocity_rad_s"))
        if any(len(state.get(field) or []) != 3 for field in required_fields):
            raise ValueError(f"moving rigid body is missing a complete solver state: {body_id}")
        result[body_id] = {
            "position": offset_z(state["position_m"], render_z_offset_m),
            "rotation_degrees": list(state["ue_rotation_pyr_deg"]),
            "velocity": (
                list(state["linear_velocity_m_s"])
                if body.get("mobility") == "dynamic"
                else [0.0, 0.0, 0.0]
            ),
            "angular_velocity_rad_s": (
                list(state["angular_velocity_rad_s"])
                if body.get("mobility") == "dynamic"
                else [0.0, 0.0, 0.0]
            ),
            "source": "genesis_rigid_body_frame",
        }
    return result


def rigid_body_asset_resolution_entries(bodies: list[dict]) -> list[dict]:
    return [
        {
            "object_id": str(body.get("id") or "rigid_body"),
            "selected_asset": {
                "asset_id": str((body.get("asset") or {}).get("ue_path") or "").rsplit(".", 1)[0],
                "ue5_path": str((body.get("asset") or {}).get("ue_path") or ""),
                "sha256": str((body.get("asset") or {}).get("sha256") or ""),
                "proxy": False,
                "provenance": str((body.get("asset") or {}).get("catalog_source") or "mounted AgenticDataPlatform UE asset catalog"),
                "collision_representation": str((body.get("collision") or {}).get("type") or ""),
                "asset_geometry_match": ((body.get("collision") or {}).get("asset_geometry_match") is True),
            },
        }
        for body in bodies
    ]


def basin_runtime_objects(
    cache: dict,
    floor_material: str,
    wall_material: str,
    *,
    asset_path: str | None = None,
    asset_scale: float | list[float] = 1.25,
    pivot_to_rim_m: float = 1.10,
    render_z_offset_m: float = 0.0,
) -> list[dict]:
    environment = cache.get("environment") if isinstance(cache.get("environment"), dict) else {}
    center_x, center_y = [float(value) for value in environment.get("center_xy_m") or [0.0, 0.0]]
    floor_z = float(environment.get("floor_z_m") or 0.0)
    extent = float(environment.get("wall_half_extent_m") or 0.3)
    initial_surface_z = float(environment.get("initial_liquid_surface_z_m") or floor_z + 0.18)
    wall_height = max(0.30, initial_surface_z - floor_z + 0.16)
    cutaway_height = min(0.08, wall_height)
    wall_thickness = 0.03
    span = extent * 2.2

    if asset_path:
        scale = (
            [float(value) for value in asset_scale]
            if isinstance(asset_scale, (list, tuple))
            else [float(asset_scale)] * 3
        )
        if len(scale) != 3 or any(value <= 0.0 for value in scale):
            raise ValueError("basin asset scale must contain three positive values")
        return [
            runtime_object(
                "basin",
                asset_path,
                [center_x, center_y, initial_surface_z - pivot_to_rim_m * scale[2] + render_z_offset_m],
                scale,
                {
                    "preserve_authored_scale": True,
                    "preserve_material": True,
                    "segmentation_identity": "basin",
                },
            )
        ]

    def basin_part(object_id: str, position: list[float], scale: list[float], material: str, color: list[float]) -> dict:
        return runtime_object(
            object_id,
            "/Engine/BasicShapes/Cube.Cube",
            position,
            scale,
            {
                "visual_material_path": material,
                "generate_solid_material": True,
                "generated_material_name": f"M_Harness_{object_id}_DeepTank",
                "fixed_material_color": True,
                "color_rgb": color,
                "preserve_authored_scale": True,
                "segmentation_identity": "basin",
            },
        )

    return [
        basin_part("basin_floor", [center_x, center_y, floor_z - 0.025], [span, span, 0.05], floor_material, [0.28, 0.32, 0.36]),
        basin_part("basin_wall_north", [center_x, center_y + extent + wall_thickness / 2, floor_z + wall_height / 2], [span, wall_thickness, wall_height], wall_material, [0.28, 0.34, 0.40]),
        basin_part("basin_wall_south", [center_x, center_y - extent - wall_thickness / 2, floor_z + cutaway_height / 2], [span, wall_thickness, cutaway_height], wall_material, [0.28, 0.34, 0.40]),
        basin_part("basin_wall_east", [center_x + extent + wall_thickness / 2, center_y, floor_z + wall_height / 2], [wall_thickness, span, wall_height], wall_material, [0.28, 0.34, 0.40]),
        basin_part("basin_wall_west", [center_x - extent - wall_thickness / 2, center_y, floor_z + wall_height / 2], [wall_thickness, span, wall_height], wall_material, [0.28, 0.34, 0.40]),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
