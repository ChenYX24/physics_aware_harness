from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from harness.core.case_spec import load_case_spec, validate_case_spec
from harness.runtime.fallback_backend import trajectory_for_case
from harness.verification.physics_verifier import PhysicsVerifier


ROOT = Path(__file__).resolve().parents[1]


class HarnessAgentActionVerifierTests(unittest.TestCase):
    def verify_case(self, rel_path: str) -> dict:
        case = load_case_spec(ROOT / rel_path)
        trajectory = trajectory_for_case(case.data)
        return PhysicsVerifier().verify(case.data, trajectory)

    def test_positive_agent_action_cases_pass(self) -> None:
        for rel_path in (
            "cases/agent_action/agent_push_box_contact.json",
            "cases/agent_action/agent_throw_ball_release.json",
            "cases/agent_action/agent_pick_object_lift.json",
            "cases/agent_action/agent_pick_and_place_on_table.json",
            "cases/agent_action/agent_throw_object_into_bin.json",
        ):
            report = self.verify_case(rel_path)
            self.assertEqual(report["status"], "pass", rel_path)
            self.assertIsNone(report["failure_type"])
            self.assertTrue(report["evidence"])

    def test_target_preaction_motion_is_rejected(self) -> None:
        report = self.verify_case("cases/agent_action/negative_target_preaction_motion.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F5_passive_precontact_motion")
        self.assertEqual(report["first_failure"]["metric"], "preaction_velocity_m_s")

    def test_missing_action_trace_is_rejected(self) -> None:
        report = self.verify_case("cases/agent_action/negative_missing_action_trace.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F7_runtime_artifact_incomplete")
        self.assertEqual(report["first_failure"]["metric"], "action_trace_count")

    def test_no_post_action_motion_is_rejected(self) -> None:
        report = self.verify_case("cases/agent_action/negative_no_post_action_motion.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "post_action_response")

    def test_place_outside_goal_is_rejected(self) -> None:
        report = self.verify_case("cases/agent_action/negative_place_outside_goal.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "final_goal_region")

    def test_generated_batch_covers_four_action_families(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/harness_generate_cases.py"),
                    "--suite",
                    "agent_action",
                    "--count",
                    "8",
                    "--seed",
                    "27",
                    "--out",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            cases = [load_case_spec(path) for path in Path(tmp).glob("*.json") if path.name != "manifest.json"]
            self.assertEqual({case.data["expected_physics"]["coupling_type"] for case in cases}, {"push", "throw", "pick", "pick_place"})
            for case in cases:
                report = PhysicsVerifier().verify(case.data, trajectory_for_case(case.data))
                self.assertEqual(report["status"] == "pass", case.should_pass, case.case_id)

    def test_goal_region_requires_positive_three_dimensional_extent(self) -> None:
        case = deepcopy(load_case_spec(ROOT / "cases/agent_action/agent_pick_and_place_on_table.json").data)
        case["expected_physics"]["object_effect"]["final_goal_region"]["half_extent_m"] = [0.2, 0.0, 0.2]
        with self.assertRaisesRegex(ValueError, "half extents"):
            validate_case_spec(case)


if __name__ == "__main__":
    unittest.main()
