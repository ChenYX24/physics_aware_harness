from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.case_spec import load_case_spec
from harness.runtime.fallback_backend import FallbackBackend
from harness.verification.physics_verifier import PhysicsVerifier


ROOT = Path(__file__).resolve().parents[1]


class HarnessRampVerifierTests(unittest.TestCase):
    def test_ramp_positive_and_negative(self) -> None:
        self.assertEqual(run_case("cases/ramp/ramp_roll_low_friction.json")["status"], "pass")
        self.assertEqual(run_case("cases/ramp/ramp_slide_high_friction_short_travel.json")["status"], "pass")
        uphill = run_case("cases/ramp/negative_uphill_without_force.json")
        self.assertEqual(uphill["status"], "fail")
        self.assertEqual(uphill["failure_type"], "F4_causality_violation")
        friction = run_case("cases/ramp/negative_no_friction_sensitivity.json")
        self.assertEqual(friction["status"], "fail")
        self.assertEqual(friction["failure_type"], "F4_causality_violation")


def run_case(rel_path: str) -> dict:
    case = load_case_spec(ROOT / rel_path)
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = FallbackBackend().run_case(case, tmp)
        return PhysicsVerifier().verify_run_dir(run_dir)


if __name__ == "__main__":
    unittest.main()
