#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.assets.asset_resolver import resolve_asset_intents
from harness.core.artifact_schema import write_json
from harness.core.case_spec import load_case_spec
from harness.planning.static_scene_builder import build_static_scene_layout
from harness.verification.static_scene_verifier import verify_static_scene_layout


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and verify a static scene layout from a harness case spec.")
    parser.add_argument("case_spec", help="Path to cases/.../*.json")
    parser.add_argument("--output-dir", default=None, help="Directory for scene_layout.json, asset_resolution.json, and static_scene_report.json")
    parser.add_argument("--top-k", type=int, default=5, help="Asset candidate count per object")
    parser.add_argument("--views", default="front_static,side_static,top_down,tracking_subject,event_closeup")
    parser.add_argument("--camera-strategy", default="bounds_auto_v1")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    case_path = Path(args.case_spec)
    case = load_case_spec(case_path)
    requested_views = [item.strip() for item in args.views.split(",") if item.strip()]
    output_dir = Path(args.output_dir) if args.output_dir else ROOT / "runs" / "static_scene" / case.case_id
    output_dir.mkdir(parents=True, exist_ok=True)
    case_dict = case.data
    asset_resolution = resolve_asset_intents(case_dict, top_k=args.top_k)
    scene_layout = build_static_scene_layout(
        case_dict,
        asset_resolution=asset_resolution,
        requested_views=requested_views,
        camera_strategy=args.camera_strategy,
    )
    report = verify_static_scene_layout(case_dict, scene_layout)
    write_json(output_dir / "asset_resolution.json", asset_resolution)
    write_json(output_dir / "scene_layout.json", scene_layout)
    write_json(output_dir / "static_scene_report.json", report)
    summary = {
        "schema_version": "harness_static_scene_cli_result_v1",
        "case_id": case.case_id,
        "status": report["status"],
        "failure_type": report.get("failure_type"),
        "output_dir": str(output_dir),
        "object_count": report.get("checks", {}).get("object_count", 0),
        "physics_critical_count": report.get("checks", {}).get("physics_critical_count", 0),
        "camera_count": report.get("checks", {}).get("camera_count", 0),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
