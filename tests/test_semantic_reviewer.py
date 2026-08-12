from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from harness.agent.semantic_reviewer import CodexAppServerReviewer
from harness.agent.review_schema import ReviewerInvocationReceipt, SemanticReview


class SemanticReviewerAdapterTests(unittest.TestCase):
    def test_semantic_review_rejects_unknown_fields_and_evidence_references(self) -> None:
        payload = {
            "schema_version": "harness_semantic_review_v1",
            "job_id": "job_review_schema",
            "attempt_id": "attempt_001",
            "evidence_bundle_digest": "a" * 64,
            "reviewer_receipt_digest": "b" * 64,
            "overall_status": "pass",
            "requirements": [
                {
                    "requirement_id": "original_user_request",
                    "status": "pass",
                    "rationale": "supported",
                    "evidence_refs": [
                        {
                            "artifact_id": "missing_artifact",
                            "time_s": None,
                            "view_id": None,
                            "trajectory_range": "0.0-0.0s",
                            "contact_event_id": None,
                        }
                    ],
                }
            ],
            "repair_layer": "none",
            "summary": "supported",
            "suggested_adjustments": [],
            "created_at": "2026-08-13T00:00:00Z",
        }
        with self.assertRaisesRegex(ValueError, "unknown Evidence Bundle artifact"):
            SemanticReview.from_dict(
                payload,
                expected_requirement_ids={"original_user_request"},
                evidence_artifact_ids={"evidence_summary"},
            )
        payload["requirements"][0]["evidence_refs"][0]["artifact_id"] = "evidence_summary"
        payload["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "fields mismatch"):
            SemanticReview.from_dict(payload)

    def test_app_server_default_model_new_thread_and_restricted_readonly_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            bundle.mkdir()
            executable = root / "fake-codex"
            executable.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import sys

                    if "--version" in sys.argv:
                        print("codex-cli fixture")
                        raise SystemExit(0)

                    review = {
                        "overall_status": "pass",
                        "requirements": [{
                            "requirement_id": "original_user_request",
                            "status": "pass",
                            "rationale": "evidence supports the request",
                            "evidence_refs": [{
                                "artifact_id": "evidence_summary",
                                "time_s": None,
                                "view_id": None,
                                "trajectory_range": "0.0-0.0s",
                                "contact_event_id": None
                            }]
                        }],
                        "repair_layer": "none",
                        "summary": "semantic match",
                        "suggested_adjustments": []
                    }
                    for line in sys.stdin:
                        message = json.loads(line)
                        method = message.get("method")
                        if method == "initialize":
                            print(json.dumps({"id": message["id"], "result": {"userAgent": "fixture"}}), flush=True)
                        elif method == "thread/start":
                            if message["params"].get("model") is not None:
                                print(json.dumps({"id": message["id"], "error": {"code": 1, "message": "model must be omitted"}}), flush=True)
                                continue
                            if "untrusted evidence" not in message["params"].get("developerInstructions", ""):
                                print(json.dumps({"id": message["id"], "error": {"code": 3, "message": "prompt injection boundary missing"}}), flush=True)
                                continue
                            print(json.dumps({
                                "id": message["id"],
                                "result": {
                                    "thread": {"id": "thr_isolated"},
                                    "model": "app-server-default",
                                    "modelProvider": "fixture",
                                    "instructionSources": [],
                                    "sandbox": {"type": "readOnly", "networkAccess": False}
                                }
                            }), flush=True)
                        elif method == "turn/start":
                            policy = message["params"].get("sandboxPolicy") or {}
                            if policy.get("type") != "readOnly" or policy.get("networkAccess") is not False or (policy.get("access") or {}).get("type") != "restricted":
                                print(json.dumps({"id": message["id"], "error": {"code": 2, "message": "isolation missing"}}), flush=True)
                                continue
                            print(json.dumps({"id": message["id"], "result": {"turn": {"id": "turn_isolated", "status": "inProgress", "items": []}}}), flush=True)
                            print(json.dumps({"method": "item/completed", "params": {"threadId": "thr_isolated", "turnId": "turn_isolated", "item": {"type": "agentMessage", "id": "item_1", "text": json.dumps(review)}}}), flush=True)
                            print(json.dumps({"method": "turn/completed", "params": {"threadId": "thr_isolated", "turn": {"id": "turn_isolated", "status": "completed", "items": []}}}), flush=True)
                    """
                ),
                encoding="utf-8",
            )
            executable.chmod(0o755)
            reviewer = CodexAppServerReviewer(
                executable=executable,
                timeout_seconds=10,
                schema_probe=lambda _executable: True,
            )

            result = reviewer.review(
                job_id="job_reviewer_adapter",
                attempt_id="attempt_001",
                bundle_dir=bundle,
                bundle_manifest={"artifacts": []},
                invocation_count=1,
                include_original_images=False,
            )

            receipt = ReviewerInvocationReceipt.from_dict(result["receipt"]).to_dict()
            self.assertEqual(result["review"]["overall_status"], "pass")
            self.assertEqual(receipt["thread_id"], "thr_isolated")
            self.assertEqual(receipt["turn_id"], "turn_isolated")
            self.assertEqual(receipt["model"], "app-server-default")
            self.assertEqual(receipt["sandbox_effective"]["access"]["readableRoots"], [str(bundle.resolve())])
            self.assertFalse(receipt["network_access"])


if __name__ == "__main__":
    unittest.main()
