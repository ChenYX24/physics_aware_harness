from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.artifact_schema import write_json
from harness.core.artifact_manager import ArtifactManager
from harness.core.case_spec import load_case_spec
from harness.core.case_library import build_run_control_execution, write_run_control_page
from harness.core.workspace import case_output_root, workspace_path
from harness.runtime.fallback_backend import FallbackBackend
from harness.runtime.execution_profile import EXECUTION_PROFILES, execution_profile, write_execution_reports
from harness.runtime.genesis_sph_backend import GenesisSPHBackend
from harness.runtime.ue_backend import UEBackend, UEBackendUnavailable
from harness.verification.physics_verifier import PhysicsVerifier


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run every harness case spec in a directory and verify each run.")
    parser.add_argument("case_dir", help="Directory containing generated or golden case JSON files.")
    parser.add_argument("--backend", choices=["fallback", "genesis_sph", "ue"], default="fallback")
    outputs = parser.add_mutually_exclusive_group()
    outputs.add_argument("--output-root", help="Absolute path, or a path relative to the local harness workspace.")
    outputs.add_argument("--case-route", help="Canonical physics/scenario/vNNN_description route under workspace/cases.")
    parser.add_argument("--video-root", help="Batch previews default to review/probes; publish only a validated selection to review/inbox.")
    parser.add_argument("--timestamp", default="")
    parser.add_argument("--views", default="front_static")
    parser.add_argument("--render-passes", default="rgb")
    parser.add_argument("--camera-strategy", default="bounds_auto_v1")
    parser.add_argument("--parallel", type=int, default=1, help="Parallel UE case execution. Output ordering remains deterministic.")
    parser.add_argument("--mode", choices=["rgb", "data", "both"], default="rgb", help="UE render pass mode; fallback ignores this.")
    parser.add_argument(
        "--profile",
        choices=sorted(EXECUTION_PROFILES),
        help="Named UE capture cost/quality contract; overrides views, passes, mode, resolution, and FPS.",
    )
    return parser.parse_args()


def read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def run_one_case(
    index: int,
    case_path: Path,
    *,
    backend_name: str,
    output_root: Path,
    requested_views: list[str],
    render_passes: list[str],
    camera_strategy: str,
    render_mode: str,
    video_root: Path,
    profile_name: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    case = load_case_spec(case_path)
    profile = execution_profile(profile_name) if profile_name else None
    if profile:
        requested_views = list(profile.views)
        render_passes = list(profile.render_passes)
        render_mode = profile.render_mode
    if backend_name == "ue":
        import os

        os.environ["SIM_STUDIO_UE_RENDER_MODE"] = render_mode
        if profile:
            os.environ.update(profile.environment())
    backend = {"fallback": FallbackBackend, "genesis_sph": GenesisSPHBackend, "ue": UEBackend}[backend_name]()
    verifier = PhysicsVerifier()
    run_dir = output_root / f"{case.case_id}_{backend_name}"
    width = profile.width if profile else 1920
    height = profile.height if profile else 1080
    execution, command = build_run_control_execution(
        run_dir,
        output_root,
        backend=backend_name,
        views=requested_views,
        render_passes=render_passes,
        mode=render_mode,
        width=width,
        height=height,
        camera_strategy=camera_strategy,
        profile=profile.name if profile else "custom",
        source_case_filename="case_spec.json",
    )
    write_run_control_page(
        run_dir,
        case.data,
        execution=execution,
        reproduce_command=command,
        status="prepared",
    )
    try:
        run_kwargs = {
            "requested_views": requested_views,
            "render_passes": render_passes,
            "camera_strategy": camera_strategy,
        }
        if backend_name == "ue":
            run_kwargs["complete_sensor_contract"] = profile.complete_sensor_contract if profile else False
        run_dir = backend.run_case(case, output_root, **run_kwargs)
        report = read_optional_json(run_dir / "fluid_report.json") if backend_name == "genesis_sph" else verifier.verify_run_dir(run_dir, write=True)
        status = report["status"]
        failure_codes = report.get("failure_codes") if isinstance(report.get("failure_codes"), list) else []
        failure_type = report.get("failure_type") or (failure_codes[0] if failure_codes else None)
        backend_report_name = "genesis_sph_backend_report.json" if backend_name == "genesis_sph" else "ue_backend_report.json"
        backend_report = read_optional_json(run_dir / backend_report_name)
        render_sync = read_optional_json(run_dir / "render_sync_report.json")
        failure_category = backend_report.get("failure_category") or ("verifier_failure" if status == "fail" else None)
        real_ue_invoked = bool(backend_report.get("whether_real_ue_invoked", backend_name == "ue" and status == "pass"))
        run_error = None
    except UEBackendUnavailable as exc:
        run_dir = exc.run_dir
        report = read_optional_json(run_dir / "harness_verifier.json") or {"status": "fail", "failure_type": exc.failure_type}
        backend_report = exc.report or read_optional_json(run_dir / "ue_backend_report.json")
        render_sync = read_optional_json(run_dir / "render_sync_report.json")
        status = str(report.get("status") or "fail")
        failure_type = str(report.get("failure_type") or exc.failure_type)
        failure_category = backend_report.get("failure_category") or "preflight_failure"
        real_ue_invoked = bool(backend_report.get("whether_real_ue_invoked"))
        run_error = str(exc)
    except Exception as exc:  # pragma: no cover - guarded by CLI behavior tests.
        run_dir = output_root / f"{case.case_id}_{backend_name}"
        status = "fail"
        failure_type = "F6_RUNTIME_OR_RENDER_FAILURE"
        failure_category = "runtime_failure"
        real_ue_invoked = backend_name == "ue"
        run_error = str(exc)
        render_sync = {}

    has_explicit_assertions = bool(case.data.get("verification_assertions"))
    expected_negative_caught = has_explicit_assertions and (not case.should_pass) and status == "fail" and failure_category == "verifier_failure"
    expectation_met = (
        (case.should_pass and status == "pass") or expected_negative_caught
        if has_explicit_assertions
        else status == "pass"
    )
    published_videos = ArtifactManager(run_dir).publish_videos(video_root, case_id=case.case_id, backend=backend_name)
    video_exists = bool(published_videos) or (run_dir / "video.mp4").exists()
    video_missing_expected = failure_category == "preflight_failure" and not video_exists
    elapsed = round(time.perf_counter() - started, 6)
    if profile:
        write_execution_reports(run_dir, profile, wall_seconds=elapsed, status=status)
    write_run_control_page(
        run_dir,
        case.data,
        execution=execution,
        reproduce_command=command,
        status="failed" if run_error else "completed",
    )
    return {
        "index": index,
        "case_id": case.case_id,
        "case_path": str(case_path),
        "capability_id": case.capability_id,
        "should_pass": case.should_pass,
        "explicit_assertion_contract": has_explicit_assertions,
        "status": status,
        "failure_type": failure_type,
        "failure_category": failure_category,
        "expectation_met": expectation_met,
        "expected_negative_caught": expected_negative_caught,
        "negative_mode": case.data.get("negative_mode"),
        "run_dir": str(run_dir),
        "run_error": run_error,
        "real_ue_invoked": real_ue_invoked,
        "video_exists": video_exists,
        "videos": [str(path) for path in published_videos],
        "video_missing_expected": video_missing_expected,
        "render_time_sec": elapsed,
        "execution_profile": profile.name if profile else "custom",
        "artifact_eligibility": profile.artifact_eligibility if profile else "legacy_custom",
        "render_mode": render_mode if backend_name == "ue" else ("surface_preview" if backend_name == "genesis_sph" else "debug_fallback"),
        "render_sync_status": render_sync.get("status"),
        "render_sync_failure_codes": render_sync.get("failure_codes", []),
        "per_camera_statistics": render_sync.get("per_camera_statistics", {}),
    }


def build_batch_render_report(rows: list[dict[str, Any]], *, backend: str, output_root: Path) -> dict[str, Any]:
    total = len(rows)
    depth_fail_count = sum(1 for row in rows if row.get("failure_type") == "F_DEPTH_MISSING" or "F_DEPTH_MISSING" in row.get("render_sync_failure_codes", []))
    sync_fail_count = sum(1 for row in rows if row.get("failure_type") == "F_RENDER_SYNC_FAIL" or "F_RENDER_SYNC_FAIL" in row.get("render_sync_failure_codes", []))
    camera_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"case_count": 0, "pass_count": 0, "failure_codes": Counter(), "avg_depth_variance": 0.0, "avg_render_time": 0.0})
    depth_variance_acc: dict[str, list[float]] = defaultdict(list)
    render_time_acc: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for camera_id, stats in (row.get("per_camera_statistics") or {}).items():
            camera_stats[camera_id]["case_count"] += 1
            if stats.get("status") == "pass":
                camera_stats[camera_id]["pass_count"] += 1
            for code in stats.get("failure_codes") or []:
                camera_stats[camera_id]["failure_codes"][str(code)] += 1
            depth_variance_acc[camera_id].append(float(stats.get("depth_variance") or 0.0))
            render_time_acc[camera_id].append(float(stats.get("render_time_sec") or 0.0))
    per_camera = {}
    for camera_id, stats in sorted(camera_stats.items()):
        depth_values = [value for value in depth_variance_acc[camera_id] if value > 0]
        render_values = [value for value in render_time_acc[camera_id] if value > 0]
        per_camera[camera_id] = {
            "case_count": stats["case_count"],
            "pass_count": stats["pass_count"],
            "failure_codes": dict(sorted(stats["failure_codes"].items())),
            "avg_depth_variance": round(sum(depth_values) / len(depth_values), 6) if depth_values else 0.0,
            "avg_render_time": round(sum(render_values) / len(render_values), 6) if render_values else 0.0,
        }
    return {
        "schema_version": "batch_render_report.v2.3",
        "artifact_schema_version": "2.3",
        "backend": backend,
        "output_root": str(output_root),
        "case_count": total,
        "success_rate": round(sum(1 for row in rows if row["status"] == "pass") / total, 6) if total else 0.0,
        "depth_fail_rate": round(depth_fail_count / total, 6) if total else 0.0,
        "sync_fail_rate": round(sync_fail_count / total, 6) if total else 0.0,
        "avg_render_time": round(sum(float(row.get("render_time_sec") or 0.0) for row in rows) / total, 6) if total else 0.0,
        "per_camera_statistics": per_camera,
        "fallback_debug_only": backend == "fallback",
    }


def main() -> int:
    args = parse_args()
    if args.profile and args.backend != "ue":
        raise SystemExit("--profile is currently defined for the UE capture backend")
    case_dir = Path(args.case_dir)
    if not case_dir.is_absolute():
        case_dir = ROOT / case_dir
    timestamp = args.timestamp or time.strftime("%Y%m%dT%H%M%S")
    output_root = case_output_root(args.case_route) if args.case_route else workspace_path(args.output_root, default_relative="runs/harness_cases") / f"{case_dir.name}_{args.backend}_{timestamp}"
    video_root = (
        workspace_path(args.video_root, default_relative="review/probes")
        if args.video_root
        else output_root / "review" / "probes"
        if args.output_root and Path(args.output_root).expanduser().is_absolute()
        else workspace_path(None, default_relative="review/probes")
    )
    output_root.mkdir(parents=True, exist_ok=True)

    case_paths = [path for path in sorted(case_dir.glob("*.json")) if path.name != "manifest.json"]
    requested_views = parse_csv(args.views)
    render_passes = parse_csv(args.render_passes)
    selected_profile = execution_profile(args.profile) if args.profile else None
    worker_count = max(1, args.parallel if args.backend == "ue" else 1)
    if worker_count == 1:
        rows = [
            run_one_case(index, case_path, backend_name=args.backend, output_root=output_root, requested_views=requested_views, render_passes=render_passes, camera_strategy=args.camera_strategy, render_mode=args.mode, video_root=video_root, profile_name=args.profile)
            for index, case_path in enumerate(case_paths)
        ]
    else:
        rows = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(run_one_case, index, case_path, backend_name=args.backend, output_root=output_root, requested_views=requested_views, render_passes=render_passes, camera_strategy=args.camera_strategy, render_mode=args.mode, video_root=video_root, profile_name=args.profile)
                for index, case_path in enumerate(case_paths)
            ]
            for future in as_completed(futures):
                rows.append(future.result())
        rows.sort(key=lambda row: int(row["index"]))

    failure_counts = Counter(str(row["failure_type"]) for row in rows if row["failure_type"])
    category_counts = Counter(str(row["failure_category"]) for row in rows if row.get("failure_category"))
    summary = {
        "schema_version": "harness_batch_run_summary_v1",
        "artifact_schema_version": "2.3",
        "backend": args.backend,
        "case_dir": str(case_dir),
        "output_root": str(output_root),
        "video_root": str(video_root),
        "case_count": len(rows),
        "positive_pass_count": sum(1 for row in rows if row["should_pass"] and row["status"] == "pass"),
        "negative_caught_count": sum(1 for row in rows if row["expected_negative_caught"]),
        "expected_negative_caught_count": sum(1 for row in rows if row["expected_negative_caught"]),
        "unexpected_count": sum(1 for row in rows if not row["expectation_met"]),
        "failure_code_distribution": dict(sorted(failure_counts.items())),
        "failure_category_distribution": dict(sorted(category_counts.items())),
        "preflight_failure_count": category_counts.get("preflight_failure", 0),
        "runtime_failure_count": category_counts.get("runtime_failure", 0),
        "artifact_missing_count": category_counts.get("artifact_missing", 0),
        "render_sync_failure_count": category_counts.get("render_sync_failure", 0),
        "verifier_failure_count": category_counts.get("verifier_failure", 0),
        "real_ue_invoked_count": sum(1 for row in rows if row["real_ue_invoked"]),
        "video_count": sum(1 for row in rows if row["video_exists"]),
        "video_missing_expected_count": sum(1 for row in rows if row["video_missing_expected"]),
        "parallel": worker_count,
        "render_mode": (selected_profile.render_mode if selected_profile else args.mode) if args.backend == "ue" else ("surface_preview" if args.backend == "genesis_sph" else "debug_fallback"),
        "execution_profile": args.profile or "custom",
        "cases": rows,
    }
    render_report = build_batch_render_report(rows, backend=args.backend, output_root=output_root)
    write_json(output_root / "batch_summary.json", summary)
    write_json(output_root / "batch_render_report.json", render_report)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["unexpected_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
