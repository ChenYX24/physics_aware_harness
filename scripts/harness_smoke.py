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
from harness.runtime.fallback_backend import FallbackBackend
from harness.verification.physics_verifier import PhysicsVerifier


DEFAULT_CASES = [
    "cases/billiards/low_speed_single_contact.json",
    "cases/billiards/multi_ball_chain_contact.json",
    "cases/billiards/negative_precontact_motion.json",
    "cases/bowling/bowling_pin_chain_contact.json",
    "cases/bowling/negative_pin_precontact_motion.json",
    "cases/domino/five_domino_chain.json",
    "cases/domino/negative_simultaneous_motion.json",
    "cases/falling/falling_block_on_floor.json",
    "cases/falling/stacked_blocks_contact.json",
    "cases/falling/negative_floating_block.json",
    "cases/spin/high_damping_spin_decay.json",
    "cases/spin/negative_spin_gain.json",
    "cases/agent_action/agent_push_box_contact.json",
    "cases/agent_action/negative_target_preaction_motion.json",
    "cases/constraint/pendulum_length_preserved.json",
    "cases/constraint/negative_constraint_length_drift.json",
    "cases/impulse_chain/newton_cradle_five_ball_transfer.json",
    "cases/impulse_chain/negative_terminal_no_response.json",
    "cases/elastic_launch/spring_launch_forward_arc.json",
    "cases/elastic_launch/negative_missing_release_event.json",
    "cases/elastic_constraint/bungee_rebound.json",
    "cases/elastic_constraint/negative_overstretch.json",
    "cases/fracture/glass_panel_impact_fracture.json",
    "cases/fracture/negative_below_threshold_fracture.json",
    "cases/magnetic/attract_magnetic_body.json",
    "cases/magnetic/negative_wrong_magnetic_direction.json",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the minimal physics-aware harness smoke suite.")
    parser.add_argument("--backend", choices=["fallback"], default="fallback")
    parser.add_argument("--output-root", default="runs/harness_smoke")
    parser.add_argument("--timestamp", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timestamp = args.timestamp or time.strftime("%Y%m%dT%H%M%S")
    output_root = ROOT / args.output_root / timestamp
    backend = FallbackBackend()
    verifier = PhysicsVerifier()
    cases = []
    expected_ok = 0
    for rel in DEFAULT_CASES:
        case = load_case_spec(ROOT / rel)
        run_dir = backend.run_case(case, output_root)
        report = verifier.verify_run_dir(run_dir, write=True)
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
