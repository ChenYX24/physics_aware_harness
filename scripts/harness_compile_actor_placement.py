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
from harness.runtime.actor_placement import compile_runtime_actor_placement
from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement
from harness.verification.static_scene_verifier import verify_static_scene_layout


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile static scene layout into deterministic runtime actor bindings.")
    parser.add_argument("case_spec", help="Path to cases/.../*.json")
    parser.add_argument("--output-dir", default=None, help="Directory for actor placement artifacts")
    parser.add_argument("--top-k", type=int, default=5, help="Asset candidate count per object")
    parser.add_argument("--views", default="front_static,side_static,top_down,tracking_subject,event_closeup")
    parser.add_argument("--camera-strategy", default="bounds_auto_v1")
    parser.add_argument("--target-backend", default="UE")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    case = load_case_spec(Path(args.case_spec))
    requested_views = [item.strip() for item in args.views.split(",") if item.strip()]
    output_dir = Path(args.output_dir) if args.output_dir else ROOT / "runs" / "actor_placement" / case.case_id
    output_dir.mkdir(parents=True, exist_ok=True)

    asset_resolution = resolve_asset_intents(case.data, top_k=args.top_k)
    scene_layout = build_static_scene_layout(
        case.data,
        asset_resolution=asset_resolution,
        requested_views=requested_views,
        camera_strategy=args.camera_strategy,
    )
    static_report = verify_static_scene_layout(case.data, scene_layout)
    placement = compile_runtime_actor_placement(
        case.data,
        scene_layout,
        asset_resolution=asset_resolution,
        target_backend=args.target_backend,
    )
    placement_report = verify_runtime_actor_placement(case.data, placement)

    write_json(output_dir / "asset_resolution.json", asset_resolution)
    write_json(output_dir / "scene_layout.json", scene_layout)
    write_json(output_dir / "static_scene_report.json", static_report)
    write_json(output_dir / "runtime_actor_placement.json", placement)
    write_json(output_dir / "runtime_actor_placement_report.json", placement_report)

    status = "pass" if static_report["status"] == "pass" and placement_report["status"] == "pass" else "fail"
    summary = {
        "schema_version": "harness_actor_placement_cli_result_v1",
        "case_id": case.case_id,
        "status": status,
        "static_scene_status": static_report["status"],
        "runtime_actor_placement_status": placement_report["status"],
        "failure_type": placement_report.get("failure_type") or static_report.get("failure_type"),
        "output_dir": str(output_dir),
        "actor_count": placement_report.get("checks", {}).get("actor_count", 0),
        "physics_critical_count": placement_report.get("checks", {}).get("physics_critical_count", 0),
        "simulated_actor_count": placement_report.get("checks", {}).get("simulated_actor_count", 0),
        "camera_count": placement_report.get("checks", {}).get("camera_count", 0),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
