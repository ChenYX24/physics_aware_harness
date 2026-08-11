from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.case_spec import load_case_spec
from harness.core.case_library import build_run_control_execution, write_run_control_page
from harness.core.workspace import workspace_path
from harness.runtime.fallback_backend import FallbackBackend
from harness.verification.physics_verifier import PhysicsVerifier


DEFAULT_CASES = [
    "cases/billiards/low_speed_single_contact.json",
    "cases/domino/five_domino_chain.json",
    "cases/falling/falling_block_on_floor.json",
    "cases/projectile/upward_throw_arc.json",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run generic scene-contract smoke cases without process routing.")
    parser.add_argument("--backend", choices=["fallback"], default="fallback")
    parser.add_argument("--output-root", default="runs/harness_smoke")
    parser.add_argument("--timestamp", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timestamp = args.timestamp or time.strftime("%Y%m%dT%H%M%S")
    output_root = workspace_path(args.output_root, default_relative="runs/harness_smoke") / timestamp
    backend = FallbackBackend()
    verifier = PhysicsVerifier()
    cases = []
    expected_ok = 0
    for rel in DEFAULT_CASES:
        case = load_case_spec(ROOT / rel)
        run_dir = output_root / f"{case.case_id}_fallback"
        execution, command = build_run_control_execution(
            run_dir,
            output_root,
            backend="fallback",
            views=["front_static", "side_static", "top_down", "tracking_subject", "event_closeup"],
            render_passes=["rgb"],
            mode="rgb",
            width=1920,
            height=1080,
            camera_strategy="bounds_auto_v1",
        )
        write_run_control_page(
            run_dir,
            case.data,
            execution=execution,
            reproduce_command=command,
            status="prepared",
        )
        try:
            run_dir = backend.run_case(case, output_root)
        except Exception:
            write_run_control_page(
                run_dir,
                case.data,
                execution=execution,
                reproduce_command=command,
                status="failed",
            )
            raise
        report = verifier.verify_run_dir(run_dir, write=True)
        write_run_control_page(
            run_dir,
            case.data,
            execution=execution,
            reproduce_command=command,
            status="completed",
        )
        expectation_met = (report["status"] == "pass") == case.should_pass
        expected_ok += int(expectation_met)
        cases.append(
            {
                "case_id": case.case_id,
                "capability_id": case.capability_id,
                "should_pass": case.should_pass,
                "status": report["status"],
                "failure_type": report["failure_type"],
                "expectation_met": expectation_met,
                "run_dir": str(run_dir),
            }
        )
    summary = {
        "schema_version": "harness_smoke_summary_v1",
        "backend": args.backend,
        "output_root": str(output_root),
        "case_count": len(cases),
        "expectation_met_count": expected_ok,
        "cases": cases,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if expected_ok == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
