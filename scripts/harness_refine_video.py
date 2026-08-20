from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.artifact_schema import read_json
from harness.core.workspace import workspace_root
from harness.refinement.video_refiner import (
    ARK_MODEL_ENDPOINTS,
    RefinementJob,
    dry_run_plan,
    run_refinement,
    split_multiview_grid,
    splice_from_repair_spec,
    splice_video,
    validate_comparison_jobs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run or inspect an asynchronous video refinement job.")
    parser.add_argument("job", nargs="?", help="Path to a harness_video_refinement_job_v1 JSON file.")
    parser.add_argument("--base-url", help="Override the provider endpoint.")
    parser.add_argument(
        "--validate-comparison",
        nargs="+",
        metavar="JOB",
        help="Validate that a set of model jobs shares one canonical prompt and one UE Refiner prompt.",
    )
    parser.add_argument("--ark-model", choices=tuple(ARK_MODEL_ENDPOINTS), help="Override an Ark job with 2.0-fast or 2.5.")
    parser.add_argument("--manifest", help="Manifest path; defaults next to the output video.")
    parser.add_argument("--dry-run", action="store_true", help="Print a redacted request without network calls.")
    parser.add_argument(
        "--compare-reference",
        action="store_true",
        help="With --dry-run, print reference-video on/off payloads.",
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--max-polls", type=int, default=720)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--splice", action="store_true", help="Run a frame-exact video splice instead of refinement.")
    parser.add_argument("--source-video")
    parser.add_argument("--replacement-video")
    parser.add_argument("--output-video")
    parser.add_argument("--start-frame", type=int)
    parser.add_argument("--end-frame", type=int)
    parser.add_argument("--mode", choices=("replace", "insert"))
    parser.add_argument("--repair-spec", help="Derive a verified replace interval from a VideoRepairSpec.")
    parser.add_argument("--split-multiview", action="store_true", help="Split a row-major video grid into named views.")
    parser.add_argument("--input-grid")
    parser.add_argument("--output-dir")
    parser.add_argument("--view-ids", default="front_static,side_static,top_down,tracking_subject,event_closeup")
    parser.add_argument("--grid-columns", type=int, default=3)
    parser.add_argument("--grid-rows", type=int, default=2)
    return parser.parse_args()


def default_base_url(job: RefinementJob) -> str:
    if job.provider == "h3_sglang":
        return "http://127.0.0.1:30011" if job.use_reference_video else "http://127.0.0.1:30010"
    if job.provider == "ark_seedance":
        return "https://ark.cn-beijing.volces.com/api/v3"
    raise ValueError(f"unsupported refinement provider: {job.provider}")


def main() -> int:
    args = parse_args()
    workspace = workspace_root()
    if args.split_multiview:
        if args.job or args.splice or args.validate_comparison:
            raise SystemExit("--split-multiview cannot be combined with a job, --splice, or --validate-comparison")
        if not args.input_grid or not args.output_dir:
            raise SystemExit("--split-multiview requires --input-grid and --output-dir")
        output_dir = Path(args.output_dir)
        manifest_path = Path(args.manifest) if args.manifest else output_dir / "split_manifest.json"
        result = split_multiview_grid(
            args.input_grid,
            output_dir,
            [item.strip() for item in args.view_ids.split(",") if item.strip()],
            columns=args.grid_columns,
            rows=args.grid_rows,
            manifest_path=manifest_path,
            workspace=workspace,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    if args.validate_comparison:
        if args.job or args.splice:
            raise SystemExit("--validate-comparison cannot be combined with a positional job or --splice")
        jobs = [RefinementJob.from_dict(read_json(path)) for path in args.validate_comparison]
        print(json.dumps(validate_comparison_jobs(jobs), indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    if args.splice:
        required = {
            "--source-video": args.source_video,
            "--replacement-video": args.replacement_video,
            "--output-video": args.output_video,
        }
        if not args.repair_spec:
            required.update({"--start-frame": args.start_frame, "--end-frame": args.end_frame, "--mode": args.mode})
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise SystemExit(f"--splice requires {', '.join(missing)}")
        if args.repair_spec and any(value is not None for value in (args.start_frame, args.end_frame, args.mode)):
            raise SystemExit("--repair-spec derives the interval and cannot be combined with --start-frame, --end-frame, or --mode")
        output = Path(args.output_video)
        manifest_path = Path(args.manifest) if args.manifest else output.with_suffix(
            output.suffix + ".splice_manifest.json"
        )
        if args.repair_spec:
            result = splice_from_repair_spec(
                args.repair_spec,
                args.source_video,
                args.replacement_video,
                output,
                manifest_path=manifest_path,
                workspace=workspace,
            )
        else:
            result = splice_video(
                args.source_video,
                args.replacement_video,
                output,
                start_frame=args.start_frame,
                end_frame=args.end_frame,
                mode=args.mode,
                manifest_path=manifest_path,
                workspace=workspace,
            )
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    if not args.job:
        raise SystemExit("refinement requires a job JSON path, or use --splice")
    if args.compare_reference and not args.dry_run:
        raise SystemExit("--compare-reference requires --dry-run")
    job = RefinementJob.from_dict(read_json(args.job))
    if args.ark_model:
        if job.provider != "ark_seedance":
            raise SystemExit("--ark-model is only valid for Ark jobs")
        job = job.with_model(ARK_MODEL_ENDPOINTS[args.ark_model])
    if args.compare_reference:
        enabled = job.with_reference_video(True)
        disabled = job.with_reference_video(False)
        result = {
            "with_reference": dry_run_plan(enabled, base_url=args.base_url or default_base_url(enabled)),
            "without_reference": dry_run_plan(disabled, base_url=args.base_url or default_base_url(disabled)),
        }
    elif args.dry_run:
        result = dry_run_plan(job, base_url=args.base_url or default_base_url(job))
    else:
        manifest_path = Path(args.manifest) if args.manifest else job.output_video.with_suffix(
            job.output_video.suffix + ".manifest.json"
        )
        result = run_refinement(
            job,
            base_url=args.base_url or default_base_url(job),
            manifest_path=manifest_path,
            poll_interval=args.poll_interval,
            max_polls=args.max_polls,
            timeout=args.timeout,
            workspace=workspace,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if args.dry_run or result.get("status") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
