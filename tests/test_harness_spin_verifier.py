from __future__ import annotations

import unittest
from pathlib import Path

from harness.core.case_spec import load_case_spec
from harness.runtime.fallback_backend import trajectory_for_case
from harness.verification.physics_verifier import PhysicsVerifier


ROOT = Path(__file__).resolve().parents[1]


class HarnessSpinVerifierTests(unittest.TestCase):
    def verify_case(self, rel_path: str) -> dict:
        case = load_case_spec(ROOT / rel_path)
        trajectory = trajectory_for_case(case.data)
        return PhysicsVerifier().verify(case.data, trajectory)

    def test_positive_spin_decay_cases_pass(self) -> None:
        for rel_path in (
            "cases/spin/high_damping_spin_decay.json",
            "cases/spin/low_damping_spin_decay.json",
        ):
            report = self.verify_case(rel_path)
            self.assertEqual(report["status"], "pass", rel_path)
            self.assertIsNone(report["failure_type"])
            self.assertTrue(report["evidence"])

    def test_no_spin_decay_is_rejected(self) -> None:
        report = self.verify_case("cases/spin/negative_no_spin_decay.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "final_angular_speed_deg_s")

    def test_spin_gain_is_rejected(self) -> None:
        report = self.verify_case("cases/spin/negative_spin_gain.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "angular_speed_increase_deg_s")

    def test_missing_angular_velocity_label_is_rejected(self) -> None:
        report = self.verify_case("cases/spin/negative_missing_angular_velocity_label.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F3_invalid_initial_physics_state")
        self.assertEqual(report["first_failure"]["metric"], "initial_angular_speed_deg_s")


if __name__ == "__main__":
    unittest.main()
