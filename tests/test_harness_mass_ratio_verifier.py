from __future__ import annotations

import unittest
from pathlib import Path

from harness.core.case_spec import load_case_spec
from harness.runtime.fallback_backend import trajectory_for_case
from harness.verification.physics_verifier import PhysicsVerifier


ROOT = Path(__file__).resolve().parents[1]


class HarnessMassRatioVerifierTests(unittest.TestCase):
    def verify_case(self, rel_path: str) -> dict:
        case = load_case_spec(ROOT / rel_path)
        trajectory = trajectory_for_case(case.data)
        return PhysicsVerifier().verify(case.data, trajectory)

    def test_positive_mass_ratio_cases_pass(self) -> None:
        for rel_path in (
            "cases/mass_ratio/heavy_striker_light_target.json",
            "cases/mass_ratio/light_striker_heavy_target.json",
        ):
            report = self.verify_case(rel_path)
            self.assertEqual(report["status"], "pass", rel_path)
            self.assertIsNone(report["failure_type"])
            self.assertTrue(report["evidence"])

    def test_missing_mass_label_is_rejected(self) -> None:
        report = self.verify_case("cases/mass_ratio/negative_missing_mass_label.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F3_invalid_initial_physics_state")
        self.assertEqual(report["first_failure"]["metric"], "mass_kg")

    def test_wrong_velocity_order_is_rejected(self) -> None:
        report = self.verify_case("cases/mass_ratio/negative_wrong_velocity_order.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "target_speed_too_slow_m_s")

    def test_momentum_gain_is_rejected(self) -> None:
        report = self.verify_case("cases/mass_ratio/negative_momentum_gain.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "energy_ratio")


if __name__ == "__main__":
    unittest.main()
