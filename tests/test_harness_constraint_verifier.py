from __future__ import annotations

import unittest
from pathlib import Path

from harness.core.case_spec import load_case_spec
from harness.runtime.fallback_backend import trajectory_for_case
from harness.verification.physics_verifier import PhysicsVerifier


ROOT = Path(__file__).resolve().parents[1]


class HarnessConstraintVerifierTests(unittest.TestCase):
    def verify_case(self, rel_path: str) -> dict:
        case = load_case_spec(ROOT / rel_path)
        trajectory = trajectory_for_case(case.data)
        return PhysicsVerifier().verify(case.data, trajectory)

    def test_positive_pendulum_constraint_cases_pass(self) -> None:
        for rel_path in (
            "cases/constraint/pendulum_length_preserved.json",
            "cases/constraint/pendulum_swing_crosses_center.json",
        ):
            report = self.verify_case(rel_path)
            self.assertEqual(report["status"], "pass", rel_path)
            self.assertIsNone(report["failure_type"])
            self.assertTrue(report["evidence"])

    def test_constraint_length_drift_is_rejected(self) -> None:
        report = self.verify_case("cases/constraint/negative_constraint_length_drift.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "constraint_length_error_m")

    def test_teleporting_body_is_rejected(self) -> None:
        report = self.verify_case("cases/constraint/negative_teleporting_body.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "teleport_step_displacement_m")

    def test_missing_constraint_label_is_rejected(self) -> None:
        report = self.verify_case("cases/constraint/negative_missing_constraint_label.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F3_invalid_initial_physics_state")
        self.assertEqual(report["first_failure"]["metric"], "constraint_length_m")


if __name__ == "__main__":
    unittest.main()
