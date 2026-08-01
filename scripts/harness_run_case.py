from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.case_spec import CaseSpec, load_case_spec, validate_case_spec
from harness.core.artifact_manager import ArtifactManager
from harness.core.case_library import build_run_control_execution, write_run_control_page
from harness.core.workspace import WORKSPACE_ENV, case_output_root, workspace_path, workspace_root
from harness.planning.prompt_to_case import prompt_to_case
from harness.runtime.fallback_backend import FallbackBackend
from harness.runtime.genesis_sph_backend import GenesisSPHBackend
from harness.runtime.genesis_fem_backend import GenesisFEMBackend
from harness.runtime.taichi_cloth_backend import TaichiClothBackend
from harness.runtime.execution_profile import EXECUTION_PROFILES, execution_profile, verified_run_status, write_execution_reports
from harness.runtime.ue_backend import UEBackend, UEBackendUnavailable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one harness case spec with a selected backend.")
    parser.add_argument("case_spec", nargs="?", help="Path to cases/.../*.json")
    parser.add_argument("--case", dest="case_spec_flag", help="Path to cases/.../*.json")
    parser.add_argument("--prompt", help="Compile a natural-language prompt into a conservative CaseSpec and run it.")
    parser.add_argument("--case-id", default="generated_case", help="Case id used with --prompt.")
    parser.add_argument("--backend", choices=["fallback", "genesis_fem", "genesis_sph", "taichi_cloth", "ue"], default="fallback")
    outputs = parser.add_mutually_exclusive_group()
    outputs.add_argument("--output-root", "--out", help="Absolute path, or a path relative to the local harness workspace.")
    outputs.add_argument("--case-route", help="Canonical physics/scenario/vNNN_description route under workspace/cases.")
    parser.add_argument(
        "--video-root",
        default="review/probes",
        help="Unvalidated one-off previews; defaults to review/probes. Use harness_iterate_case.py to publish a hard-gate winner to review/inbox.",
    )
    parser.add_argument(
        "--views",
        default="front_static",
        help="Comma-separated camera ids. The default is a one-view smoke run; pass the complete case camera set for delivery renders.",
    )
    parser.add_argument("--render-passes", default="rgb")
    parser.add_argument("--width", type=int, help="Custom UE capture width; requires --height.")
    parser.add_argument("--height", type=int, help="Custom UE capture height; requires --width.")
    parser.add_argument("--camera-strategy", default="bounds_auto_v1")
    parser.add_argument("--mode", choices=["rgb", "data", "both"], default="rgb", help="UE render pass mode; fallback ignores this.")
    parser.add_argument(
        "--profile",
        choices=sorted(EXECUTION_PROFILES),
        help="Named cost/quality contract. It overrides --views, --render-passes, and --mode.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.environ.setdefault(WORKSPACE_ENV, str(workspace_root()))
    case_path = args.case_spec_flag or args.case_spec
    if bool(case_path) == bool(args.prompt):
        raise SystemExit("provide exactly one of --case/case_spec or --prompt")
    output_root = case_output_root(args.case_route) if args.case_route else workspace_path(args.output_root, default_relative="runs/harness_cases")
    profile = execution_profile(args.profile) if args.profile else None
    if bool(args.width) != bool(args.height):
        raise SystemExit("--width and --height must be provided together")
    if profile and (args.width or args.height):
        raise SystemExit("--profile already defines resolution; omit --width/--height")
    if args.width and (
        args.width < 320
        or args.height < 180
        or args.width > 7680
        or args.height > 4320
    ):
        raise SystemExit("custom resolution must be within 320x180 and 7680x4320")
    requested_views = list(profile.views) if profile else parse_csv(args.views)
    render_passes = list(profile.render_passes) if profile else parse_csv(args.render_passes)
    render_mode = profile.render_mode if profile else args.mode
    if args.prompt:
        generated = prompt_to_case(args.prompt, case_id=args.case_id)
        validate_case_spec(generated)
        case = CaseSpec(generated)
    else:
        case = load_case_spec(case_path)
    if args.backend == "ue":
        os.environ["SIM_STUDIO_UE_RENDER_MODE"] = render_mode
        if profile:
            os.environ.update(profile.environment())
        elif args.width and args.height:
            os.environ["SIM_STUDIO_UE_WIDTH"] = str(args.width)
            os.environ["SIM_STUDIO_UE_HEIGHT"] = str(args.height)
    backend = {
        "fallback": FallbackBackend,
        "genesis_fem": GenesisFEMBackend,
        "genesis_sph": GenesisSPHBackend,
        "taichi_cloth": TaichiClothBackend,
        "ue": UEBackend,
    }[args.backend]()
    run_dir = Path(output_root) / f"{case.case_id}_{args.backend}"
    width = profile.width if profile else int(args.width or os.environ.get("SIM_STUDIO_UE_WIDTH", 1920))
    height = profile.height if profile else int(args.height or os.environ.get("SIM_STUDIO_UE_HEIGHT", 1080))
    execution, reproduce_command = build_run_control_execution(
        run_dir,
        output_root,
        backend=args.backend,
        views=requested_views,
        render_passes=render_passes,
        mode=render_mode,
        width=width,
        height=height,
        camera_strategy=args.camera_strategy,
        profile=profile.name if profile else "custom",
        case_route=args.case_route,
        lighting_preset=os.environ.get("SIM_STUDIO_UE_LIGHTING_PRESET"),
    )
    write_run_control_page(
        run_dir,
        case.data,
        execution=execution,
        reproduce_command=reproduce_command,
        status="prepared",
    )
    started = time.perf_counter()
    try:
        run_kwargs = {
            "requested_views": requested_views,
            "render_passes": render_passes,
            "camera_strategy": args.camera_strategy,
        }
        if args.backend == "ue":
            run_kwargs["complete_sensor_contract"] = profile.complete_sensor_contract if profile else False
        run_dir = backend.run_case(case, output_root, **run_kwargs)
    except UEBackendUnavailable as exc:
        write_run_control_page(
            exc.run_dir,
            case.data,
            execution=execution,
            reproduce_command=reproduce_command,
            status="failed",
        )
        if profile:
            write_execution_reports(
                exc.run_dir,
                profile,
                wall_seconds=time.perf_counter() - started,
                status="fail",
            )
        videos = ArtifactManager(exc.run_dir).publish_videos(workspace_path(args.video_root, default_relative="review/probes"), case_id=case.case_id, backend=args.backend)
        print(
            json.dumps(
                {
                    "schema_version": "harness_run_case_result_v1",
                    "run_dir": str(exc.run_dir),
                    "case_id": case.case_id,
                    "backend": args.backend,
                    "profile": profile.name if profile else "custom",
                    "status": "failed_unavailable",
                    "failure_type": exc.failure_type,
                    "failure_category": exc.report.get("failure_category"),
                    "real_ue_invoked": bool(exc.report.get("whether_real_ue_invoked")),
                    "reason": str(exc),
                    "run_control": str(exc.run_dir / "run_control.html"),
                    "videos": [str(path) for path in videos],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 2
    except Exception:
        write_run_control_page(
            run_dir,
            case.data,
            execution=execution,
            reproduce_command=reproduce_command,
            status="failed",
        )
        raise
    if profile:
        verification_status = verified_run_status(run_dir)
        write_execution_reports(
            run_dir,
            profile,
            wall_seconds=time.perf_counter() - started,
            status=verification_status,
        )
    else:
        verification_status = None
    write_run_control_page(
        run_dir,
        case.data,
        execution=execution,
        reproduce_command=reproduce_command,
        status="completed",
    )
    videos = ArtifactManager(run_dir).publish_videos(workspace_path(args.video_root, default_relative="review/probes"), case_id=case.case_id, backend=args.backend)
    print(json.dumps({"schema_version": "harness_run_case_result_v1", "run_dir": str(run_dir), "case_id": case.case_id, "backend": args.backend, "status": "completed", "verification_status": verification_status, "profile": profile.name if profile else "custom", "run_control": str(run_dir / "run_control.html"), "videos": [str(path) for path in videos]}, indent=2, ensure_ascii=False))
    return 0


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
