from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.core.review_feedback import compile_review_feedback, verified_execution_evidence_from_run_dirs


class ReviewFeedbackTests(unittest.TestCase):
    def test_evidence_rejects_a_different_evaluator_case_spec(self) -> None:
        run_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (run_dir / "inputs").mkdir()
        (run_dir / "case_spec.json").write_text(json.dumps({"case_id": "reviewed"}), encoding="utf-8")
        (run_dir / "inputs" / "case.json").write_text(json.dumps({"case_id": "evaluated"}), encoding="utf-8")
        report = {
            "schema_version": "harness_run_quality_v1",
            "run_dir": str(run_dir.resolve()),
            "status": "pass",
            "hard_gate_passed": True,
            "hard_gate": {"status": "pass", "passed": True, "failure_count": 0, "failures": []},
        }
        (run_dir / "quality_report.json").write_text(json.dumps(report), encoding="utf-8")

        with (
            patch("harness.core.review_feedback.evaluate_run", return_value=report),
            self.assertRaisesRegex(ValueError, "evaluated CaseSpec differs"),
        ):
            verified_execution_evidence_from_run_dirs([run_dir])

    def test_incomplete_failure_is_not_negative_execution_evidence(self) -> None:
        run_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (run_dir / "case_spec.json").write_text(json.dumps({"case_id": "incomplete"}), encoding="utf-8")
        report = {
            "schema_version": "harness_run_quality_v1",
            "run_dir": str(run_dir.resolve()),
            "status": "fail",
            "hard_gate_passed": False,
            "hard_gate": {
                "status": "fail",
                "passed": False,
                "failure_count": 1,
                "failures": [{"code": "F_VIDEO_MISSING", "message": "video missing"}],
            },
        }
        (run_dir / "quality_report.json").write_text(json.dumps(report), encoding="utf-8")

        with (
            patch("harness.core.review_feedback.evaluate_run", return_value=report),
            self.assertRaisesRegex(ValueError, "artifact_manifest.json"),
        ):
            verified_execution_evidence_from_run_dirs([run_dir])

    def test_stored_failure_signature_must_match_recomputed_report(self) -> None:
        run_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (run_dir / "case_spec.json").write_text(json.dumps({"case_id": "drifted"}), encoding="utf-8")
        stored = {
            "schema_version": "harness_run_quality_v1",
            "run_dir": str(run_dir.resolve()),
            "status": "fail",
            "hard_gate_passed": False,
            "hard_gate": {"status": "fail", "passed": False, "failures": [{"code": "F_A"}]},
        }
        recomputed = {**stored, "hard_gate": {"status": "fail", "passed": False, "failures": [{"code": "F_B"}]}}
        (run_dir / "quality_report.json").write_text(json.dumps(stored), encoding="utf-8")

        with (
            patch("harness.core.review_feedback.evaluate_run", return_value=recomputed),
            self.assertRaisesRegex(ValueError, "does not match canonical recomputation"),
        ):
            verified_execution_evidence_from_run_dirs([run_dir])

    def test_hand_written_passing_quality_report_is_not_execution_evidence(self) -> None:
        run_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (run_dir / "case_spec.json").write_text(json.dumps({"case_id": "forged"}), encoding="utf-8")
        (run_dir / "quality_report.json").write_text(
            json.dumps(
                {
                    "schema_version": "harness_run_quality_v1",
                    "run_dir": str(run_dir.resolve()),
                    "status": "pass",
                    "hard_gate_passed": True,
                    "hard_gate": {"status": "pass", "passed": True},
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "does not match canonical recomputation"):
            verified_execution_evidence_from_run_dirs([run_dir])

    def test_every_case_becomes_weekly_or_regression_knowledge(self) -> None:
        payload = {
            "schema_version": "physics_harness_case_curation_decisions_v2",
            "catalog_sha256": "catalog",
            "case_index": {
                "pass_case": {
                    "title": "通过案例",
                    "category": 1,
                    "collection": "workspace_inventory",
                    "execution_status": "pass",
                    "case_route": "rigid_collision/domino/v1",
                    "prompt": "same prompt",
                    "artifacts": {"pass_video": {"label": "front", "path": "front.mp4"}},
                },
                "fail_case": {
                    "title": "失败案例",
                    "category": 1,
                    "collection": "workspace_inventory",
                    "execution_status": "fail",
                    "artifacts": {"fail_video": {"label": "failure", "path": "failure.mp4"}},
                },
                "legacy_case": {
                    "title": "旧案例",
                    "category": 1,
                    "collection": "workspace_inventory",
                    "execution_status": "legacy",
                    "artifacts": {},
                },
            },
            "decisions": {
                "pass_case": {
                    "decision": "keep",
                    "issues": [],
                    "feedback": "",
                    "artifacts": {"pass_video": "keep"},
                },
                "fail_case": {
                    "decision": "keep",
                    "issues": ["hard_gate_failed", "source_label_corrected"],
                    "feedback": "Keep as a negative example.",
                    "artifacts": {"fail_video": "keep"},
                },
                "legacy_case": {
                    "decision": "improve",
                    "issues": ["legacy_unverified"],
                    "feedback": "",
                    "artifacts": {},
                },
            },
        }
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        run_dirs = []
        for case_id, passed in (("pass_case", True), ("fail_case", False)):
            run_dir = root / case_id
            run_dir.mkdir()
            (run_dir / "case_spec.json").write_text(json.dumps({"case_id": case_id}), encoding="utf-8")
            hard_gate = {
                "status": "pass" if passed else "fail",
                "passed": passed,
                "failure_count": 0 if passed else 1,
                "failures": [] if passed else [{"code": "F_TEST", "message": "expected failure"}],
            }
            (run_dir / "quality_report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "harness_run_quality_v1",
                        "run_dir": str(run_dir.resolve()),
                        "status": "pass" if passed else "fail",
                        "hard_gate_passed": passed,
                        "hard_gate": hard_gate,
                    }
                ),
                encoding="utf-8",
            )
            if not passed:
                (run_dir / "failure.mp4").write_bytes(b"representative failure")
                (run_dir / "artifact_manifest.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "harness_artifact_manifest_v1",
                            "case_id": case_id,
                            "artifacts": {"video": "failure.mp4"},
                        }
                    ),
                    encoding="utf-8",
                )
            run_dirs.append(run_dir)
        with patch(
            "harness.core.review_feedback.evaluate_run",
            side_effect=lambda run_dir, write=False: json.loads((Path(run_dir) / "quality_report.json").read_text()),
        ):
            evidence = verified_execution_evidence_from_run_dirs(run_dirs)
        for case_id in ("pass_case", "fail_case"):
            payload["case_index"][case_id]["case_spec_sha256"] = evidence[case_id]["case_spec_sha256"]
        with patch(
            "harness.core.review_feedback.evaluate_run",
            side_effect=lambda run_dir, write=False: json.loads((Path(run_dir) / "quality_report.json").read_text()),
        ):
            compiled = compile_review_feedback(payload, verified_execution_evidence=evidence)

        self.assertEqual(len(compiled["case_lessons"]), 3)
        self.assertEqual([row["case_id"] for row in compiled["weekly_candidates"]], ["pass_case", "fail_case"])
        self.assertEqual(compiled["weekly_candidates"][0]["artifacts"][0]["path"], "front.mp4")
        self.assertEqual(compiled["regression_candidates"]["positive"], ["pass_case"])
        self.assertEqual(compiled["regression_candidates"]["negative"], ["fail_case"])
        self.assertEqual(compiled["regression_candidates"]["quarantined_legacy"], [])
        self.assertEqual(compiled["regression_candidates"]["pending_evidence"], ["legacy_case"])
        self.assertIn("hard_gate_failed", {row["issue_id"] for row in compiled["rules"]})
        self.assertIn("source_label_corrected", {row["issue_id"] for row in compiled["rules"]})

        payload["case_index"]["pass_case"]["case_spec_sha256"] = "0" * 64
        with (
            patch(
                "harness.core.review_feedback.evaluate_run",
                side_effect=lambda run_dir, write=False: json.loads((Path(run_dir) / "quality_report.json").read_text()),
            ),
            self.assertRaisesRegex(ValueError, "does not match the reviewed CaseSpec"),
        ):
            compile_review_feedback(payload, verified_execution_evidence=evidence)

        unverified = compile_review_feedback(payload)
        self.assertEqual(unverified["regression_candidates"]["positive"], [])
        self.assertEqual(unverified["regression_candidates"]["negative"], [])
        self.assertEqual(
            unverified["regression_candidates"]["pending_evidence"],
            ["fail_case", "legacy_case", "pass_case"],
        )


if __name__ == "__main__":
    unittest.main()
