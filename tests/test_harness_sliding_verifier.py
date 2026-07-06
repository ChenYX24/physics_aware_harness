from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.case_spec import load_case_spec
from harness.runtime.fallback_backend import FallbackBackend
from harness.verification.physics_verifier import PhysicsVerifier


ROOT = Path(__file__).resolve().parents[1]


class HarnessSlidingVerifierTests(unittest.TestCase):
    def test_sliding_positive_and_negative(self) -> None:
        self.assertEqual(run_case("cases/sliding/medium_friction_slide.json")["status"], "pass")
        self.assertEqual(run_case("cases/sliding/static_threshold_hold.json")["status"], "pass")
        no_deceleration = run_case("cases/sliding/negative_no_deceleration.json")
        self.assertEqual(no_deceleration["status"], "fail")
        self.assertEqual(no_deceleration["failure_type"], "F4_causality_violation")
        threshold = run_case("cases/sliding/negative_static_threshold_violation.json")
        self.assertEqual(threshold["status"], "fail")
        self.assertEqual(threshold["failure_type"], "F4_causality_violation")
        missing_contact = run_case("cases/sliding/negative_missing_contact.json")
        self.assertEqual(missing_contact["status"], "fail")
        self.assertEqual(missing_contact["failure_type"], "F2_missing_contact_events")


def run_case(rel_path: str) -> dict:
    case = load_case_spec(ROOT / rel_path)
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = FallbackBackend().run_case(case, tmp)
        return PhysicsVerifier().verify_run_dir(run_dir)


if __name__ == "__main__":
    unittest.main()
