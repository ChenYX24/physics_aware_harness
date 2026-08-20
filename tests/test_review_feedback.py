from __future__ import annotations

import unittest

from harness.core.review_feedback import compile_review_feedback


class ReviewFeedbackTests(unittest.TestCase):
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
        compiled = compile_review_feedback(
            payload,
            verified_execution_statuses={
                "pass_case": "pass",
                "fail_case": "fail",
                "legacy_case": "legacy",
            },
        )

        self.assertEqual(len(compiled["case_lessons"]), 3)
        self.assertEqual([row["case_id"] for row in compiled["weekly_candidates"]], ["pass_case", "fail_case"])
        self.assertEqual(compiled["weekly_candidates"][0]["artifacts"][0]["path"], "front.mp4")
        self.assertEqual(compiled["regression_candidates"]["positive"], ["pass_case"])
        self.assertEqual(compiled["regression_candidates"]["negative"], ["fail_case"])
        self.assertEqual(compiled["regression_candidates"]["quarantined_legacy"], ["legacy_case"])
        self.assertIn("hard_gate_failed", {row["issue_id"] for row in compiled["rules"]})
        self.assertIn("source_label_corrected", {row["issue_id"] for row in compiled["rules"]})

        unverified = compile_review_feedback(payload)
        self.assertEqual(unverified["regression_candidates"]["positive"], [])
        self.assertEqual(unverified["regression_candidates"]["negative"], [])
        self.assertEqual(
            unverified["regression_candidates"]["pending_evidence"],
            ["fail_case", "legacy_case", "pass_case"],
        )


if __name__ == "__main__":
    unittest.main()
