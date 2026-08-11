from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.capability_closed_loop import run_closed_loop_demo, simulate_execution_trace
from tools.capability_planner import CapabilityPlanner
from tools.capability_verifier import CapabilityVerifier
from tools.failure_taxonomy import VALID_FAILURE_TYPES


ROOT = Path(__file__).resolve().parents[1]


class CapabilityClosedLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = CapabilityPlanner(ROOT / "config" / "harness_capability_profile.json")
        self.verifier = CapabilityVerifier()

    def test_profile_contains_only_execution_domains(self) -> None:
        profile = json.loads((ROOT / "config" / "harness_capability_profile.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {item["id"] for item in profile["capabilities"]},
            {"rigid_body_dynamics", "fluid_particle_dynamics", "deformable_body_dynamics"},
        )

    def test_named_rigid_prompts_do_not_change_route(self) -> None:
        prompts = (
            "billiards cue ball hits passive target",
            "falling blocks under gravity",
            "domino chain reaction",
            "compressed spring launches a payload",
            "brittle panel fractures after impact",
        )
        self.assertEqual({self.planner.plan(prompt)["primary_capability_id"] for prompt in prompts}, {"rigid_body_dynamics"})

    def test_verifier_evaluates_declared_assertions_only(self) -> None:
        plan = self.planner.plan("rigid bodies")
        execution = simulate_execution_trace(
            "generic",
            plan,
            [{"id": "large_delta", "type": "state_delta", "object_id": "body_a", "field": "position_m.x", "operator": ">", "value": 1.0}],
        )
        report = self.verifier.verify(plan, execution)
        self.assertFalse(report["capability_ready"])
        self.assertEqual(report["primary_failure_type"], "F4_causality_violation")
        self.assertTrue(all(item["failure_type"] in VALID_FAILURE_TYPES for item in report["failure_modes"]))

    def test_closed_loop_demo_writes_structured_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_closed_loop_demo(Path(tmp), timestamp="test_run")
            run_dir = Path(result["run_dir"])
            self.assertTrue((run_dir / "summary.json").exists())
            self.assertTrue((run_dir / "case_results.json").exists())
            self.assertEqual(result["summary"]["capability_ready_count"], 3)


if __name__ == "__main__":
    unittest.main()
