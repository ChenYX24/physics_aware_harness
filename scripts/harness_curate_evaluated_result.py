#!/usr/bin/env python3
"""Materialize one compact, human-accepted ICLR result from a durable Job."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


METRICS = (
    "camera_trajectory.json",
    "contact_events.json",
    "harness_verifier.json",
    "quality_report.json",
    "render_sync_report.json",
    "sensor_artifact_retention.json",
    "trajectory.json",
)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_completed_stage(path: Path, stage: str) -> None:
    result = read_json(path)
    if result.get("stage") != stage or result.get("status") != "completed":
        raise RuntimeError(f"{stage} is not completed: {path}")


def copy_file(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    shutil.copy2(source, target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--attempt-id", default="attempt_001")
    parser.add_argument("--ordinal", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--decision", required=True)
    args = parser.parse_args()

    job_root = args.workspace / "jobs" / args.job_id
    attempt_root = job_root / "attempts" / args.attempt_id
    job_manifest = read_json(job_root / "job_manifest.json")
    if job_manifest.get("state") != "awaiting_semantic_review":
        raise RuntimeError("Job must be paused after Evidence Bundle")
    usage = job_manifest.get("usage", {})
    if usage.get("reviewer_invocations") != 0:
        raise RuntimeError("semantic Reviewer must remain uninvoked")
    require_completed_stage(attempt_root / "stage_results" / "evidence_bundle.json", "evidence_bundle")

    candidate_roots = list((attempt_root / "runs" / "candidate").glob(f"{args.case_id}_*"))
    if len(candidate_roots) != 1:
        raise RuntimeError(f"expected one Candidate run, found {len(candidate_roots)}")
    run_root = candidate_roots[0]
    quality_report = read_json(run_root / "quality_report.json")
    if quality_report.get("status") != "pass" or not quality_report.get("hard_gate_passed"):
        raise RuntimeError("Candidate Quality Gate did not pass")

    camera_plan = read_json(run_root / "camera_plan.json")
    expected_views = sorted(view["camera_id"] for view in camera_plan["views"])
    view_roots = sorted(path for path in (run_root / "views").iterdir() if path.is_dir())
    actual_views = [path.name for path in view_roots]
    if actual_views != expected_views:
        raise RuntimeError(
            f"Candidate views differ from camera plan: expected {expected_views}, found {actual_views}"
        )

    target = args.output_dir
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise RuntimeError(f"output directory is not empty: {target}")
    (target / "metrics").mkdir()
    (target / "receipts").mkdir()

    copy_file(attempt_root / "case_spec.json", target / "case_spec.json")
    copy_file(job_root / "request" / "user_request.json", target / "request.json")
    for name in METRICS:
        copy_file(run_root / name, target / "metrics" / name)
    copy_file(job_root / "job_manifest.json", target / "receipts" / "controller_job_manifest.json")
    copy_file(
        attempt_root / "evidence_bundle" / "manifest.json",
        target / "receipts" / "evidence_bundle_manifest.json",
    )

    videos = []
    view_contract = None
    for source_view in view_roots:
        view_id = source_view.name
        target_view = target / "views" / view_id
        target_view.mkdir(parents=True)
        copy_file(source_view / "meta.json", target_view / "meta.json")
        meta = read_json(source_view / "meta.json")
        contract = {
            "width": meta["camera_intrinsics"]["width"],
            "height": meta["camera_intrinsics"]["height"],
            "fps": meta["fps"],
            "frame_count": meta["frame_count_rgb"],
            "duration_s": round(meta["frame_count_rgb"] / meta["fps"], 6),
        }
        if view_contract is None:
            view_contract = contract
        elif view_contract != contract:
            raise RuntimeError("Candidate view contracts differ")
        for source_name, target_name in (
            ("rgb.mp4", "rgb.mp4"),
            ("depth_preview.mp4", "depth.mp4"),
            ("segmentation_preview.mp4", "segmentation.mp4"),
        ):
            source = source_view / source_name
            destination = target_view / target_name
            os.link(source, destination)
            videos.append({"path": destination.relative_to(target).as_posix(), "sha256": sha256(destination)})

    assert view_contract is not None
    if view_contract["duration_s"] < 5.0:
        raise RuntimeError("formal Candidate duration is below 5 seconds")
    accepted_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    accepted_case_sha = sha256(target / "case_spec.json")
    frozen_case = args.experiment_root / "cases" / args.case_id / "case_spec.json"
    frozen_sha = sha256(frozen_case) if frozen_case.is_file() else None
    acceptance = {
        "schema_version": "harness_iclr_formal_human_acceptance_v1",
        "experiment_id": args.experiment_root.name,
        "ordinal": args.ordinal,
        "case_id": args.case_id,
        "agent": {
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "reasoning_mode": "standard",
        },
        "job_id": args.job_id,
        "attempt_id": args.attempt_id,
        "accepted_at": accepted_at,
        "human_video_status": "pass",
        "decision": args.decision,
        "technical_gates": {
            "smoke": "pass",
            "candidate": "pass",
            "quality_gate": "pass",
            "evidence_bundle": "complete",
            "semantic_reviewer": "paused_by_user",
        },
        "controller_state_preserved": "awaiting_semantic_review",
        "controller_reviewer_invocations": 0,
        "condition_record": {
            "original_freeze_case_spec_sha256": frozen_sha,
            "accepted_case_spec_sha256": accepted_case_sha,
            "matches_original_freeze": frozen_sha == accepted_case_sha,
            "amendment": "The accepted formal condition includes all revisions explicitly approved by the user.",
        },
        "storage_policy": {
            "video_materialization": "same_volume_hard_links",
            "per_frame_png_retained": 0,
            "per_frame_exr_retained": 0,
        },
    }
    write_json(target / "receipts" / "human_acceptance.json", acceptance)

    core_paths = (
        "case_spec.json",
        "request.json",
        "metrics/quality_report.json",
        "metrics/harness_verifier.json",
        "metrics/render_sync_report.json",
        "receipts/evidence_bundle_manifest.json",
    )
    result = {
        "schema_version": "harness_iclr_evaluated_result_v1",
        "experiment_id": args.experiment_root.name,
        "ordinal": args.ordinal,
        "case_id": args.case_id,
        "job_id": args.job_id,
        "attempt_id": args.attempt_id,
        "accepted_at": accepted_at,
        "status": "human_accepted",
        "video_contract": {**view_contract, "modalities": ["rgb", "depth", "segmentation"]},
        "videos": videos,
        "core_artifacts": [{"path": path, "sha256": sha256(target / path)} for path in core_paths],
    }
    write_json(target / "result_manifest.json", result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
