from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness.core.case_spec import load_case_spec
from harness.runtime.fallback_backend import FallbackBackend
from harness.verification.physics_verifier import PhysicsVerifier


ROOT = Path(__file__).resolve().parents[1]


class HarnessBowlingCaseFamilyTests(unittest.TestCase):
    def run_case(self, rel_path: str) -> dict:
        case = load_case_spec(ROOT / rel_path)
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = FallbackBackend().run_case(case, tmp)
            return PhysicsVerifier().verify_run_dir(run_dir)

    def test_bowling_pin_chain_contact_passes_under_contact_causality(self) -> None:
        case = load_case_spec(ROOT / "cases/bowling/bowling_pin_chain_contact.json")
        self.assertEqual(case.capability_id, "rigid_body_contact_causality")
        report = self.run_case("cases/bowling/bowling_pin_chain_contact.json")
        self.assertEqual(report["status"], "pass")
        self.assertGreaterEqual(len(report["evidence"]), 4)

    def test_bowling_pin_precontact_motion_is_rejected(self) -> None:
        report = self.run_case("cases/bowling/negative_pin_precontact_motion.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F5_passive_precontact_motion")
        self.assertIn("pin_", report["first_failure"]["object_id"])

    def test_generated_bowling_suite_runs_with_expected_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "bowling_cases"
            run_root = Path(tmp) / "runs"
            generated = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_generate_cases.py"),
                    "--suite",
                    "bowling",
                    "--count",
                    "6",
                    "--seed",
                    "71",
                    "--out",
                    str(case_dir),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["capability_id"], "rigid_body_contact_causality")
            self.assertEqual(manifest["num_cases"], 6)
            batch = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_run_case_batch.py"),
                    str(case_dir),
                    "--backend",
                    "fallback",
                    "--output-root",
                    str(run_root),
                    "--timestamp",
                    "unit",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(batch.returncode, 0, batch.stderr)
            summary = json.loads(batch.stdout)
            self.assertEqual(summary["case_count"], 6)
            self.assertEqual(summary["unexpected_count"], 0)
            self.assertEqual(summary["positive_pass_count"] + summary["negative_caught_count"], 6)


if __name__ == "__main__":
    unittest.main()
