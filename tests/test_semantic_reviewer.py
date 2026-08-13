from __future__ import annotations

import copy
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.agent.semantic_reviewer import CodexAppServerReviewer, SemanticReviewerError
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

    def test_semantic_review_rejects_nonexistent_or_out_of_range_locators(self) -> None:
        manifest = {
            "schema_version": "harness_evidence_bundle_v2",
            "job_id": "job_locator_validation",
            "attempt_id": "attempt_001",
            "case_spec_digest": "a" * 64,
            "intent_contract_digest": "b" * 64,
            "candidate_run": {"path": "runs/candidate/run", "fingerprint": "c" * 64},
            "technical_gates": {
                name: {"status": "pass", "path": f"runs/candidate/run/{name}.json", "sha256": character * 64}
                for name, character in (("verifier", "d"), ("render_sync", "e"), ("quality_gate", "f"))
            },
            "event_selection": {
                "strategy": "event_anchored",
                "reason": "fixture contact",
                "points": [
                    {"label": "before", "time_s": 0.0, "frame_index": 0, "event_refs": []},
                    {"label": "during", "time_s": 0.5, "frame_index": 1, "event_refs": ["contact_000"]},
                    {"label": "after", "time_s": 1.0, "frame_index": 2, "event_refs": []},
                ],
            },
            "trajectory_summary": {
                "source_path": "runs/candidate/run/trajectory.json",
                "source_sha256": "1" * 64,
                "frame_count": 3,
                "start_time_s": 0.0,
                "end_time_s": 1.0,
                "objects": ["ball"],
                "sampling": {
                    "strategy": "uniform_event_state_v1",
                    "max_sample_frames": 24,
                    "selected_frame_count": 3,
                    "omitted_frame_count": 0,
                    "state_transition_count": 0,
                    "state_transitions_included": 0,
                },
                "sampled_frames": [
                    {
                        "frame_index": index,
                        "time_s": time_s,
                        "reasons": ["fixture"],
                        "objects": [{
                            "object_id": "ball",
                            "transform": {
                                "position": {"field": "position_m", "values": [time_s, 0.0, 0.0]},
                                "rotation": None,
                                "scale": None,
                            },
                            "linear_velocity": None,
                            "angular_velocity": None,
                            "state": [],
                        }],
                    }
                    for index, time_s in enumerate((0.0, 0.5, 1.0))
                ],
                "readable_ranges": [{
                    "range_id": "trajectory_full",
                    "start_frame_index": 0,
                    "end_frame_index": 2,
                    "start_time_s": 0.0,
                    "end_time_s": 1.0,
                    "sample_frame_indices": [0, 1, 2],
                    "event_refs": ["contact_000"],
                }],
                "state_transitions": [],
            },
            "contact_timeline": [
                {"event_id": "contact_000", "frame_index": 1, "time_s": 0.5, "objects": ["ball", "floor"], "kind": "impact"}
            ],
            "artifacts": [
                {
                    "artifact_id": "evidence_summary",
                    "kind": "structured_summary",
                    "path": "evidence_summary.json",
                    "sha256": "2" * 64,
                    "mime_type": "application/json",
                    "time_s": None,
                    "view_id": None,
                    "source_ref": None,
                },
                {
                    "artifact_id": "keyframe_during_front",
                    "kind": "keyframe",
                    "path": "keyframes/during_front.png",
                    "sha256": "3" * 64,
                    "mime_type": "image/png",
                    "time_s": 0.5,
                    "view_id": "front",
                    "source_ref": "runs/candidate/run/views/front/rgb.mp4",
                },
                {
                    "artifact_id": "user_request_snapshot",
                    "kind": "input_snapshot",
                    "path": "inputs/user_request.json",
                    "sha256": "4" * 64,
                    "mime_type": "application/json",
                    "time_s": None,
                    "view_id": None,
                    "source_ref": None,
                },
            ],
            "created_at": "2026-08-13T00:00:00Z",
        }
        payload = {
            "schema_version": "harness_semantic_review_v1",
            "job_id": "job_locator_validation",
            "attempt_id": "attempt_001",
            "evidence_bundle_digest": "5" * 64,
            "reviewer_receipt_digest": "6" * 64,
            "overall_status": "pass",
            "requirements": [
                {
                    "requirement_id": "original_user_request",
                    "status": "pass",
                    "rationale": "supported",
                    "evidence_refs": [
                        {
                            "artifact_id": "evidence_summary",
                            "time_s": None,
                            "view_id": None,
                            "trajectory_range": "0.0-1.0s",
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
        artifact_ids = {row["artifact_id"] for row in manifest["artifacts"]}
        SemanticReview.from_dict(
            payload,
            expected_requirement_ids={"original_user_request"},
            evidence_artifact_ids=artifact_ids,
            evidence_manifest=manifest,
        )
        cases = []
        for field, value in (
            ("time_s", -999.0),
            ("view_id", "missing_view"),
            ("trajectory_range", "not-a-range"),
            ("trajectory_range", "0.1-0.9s"),
            ("contact_event_id", "contact_missing"),
        ):
            changed = copy.deepcopy(payload)
            changed["requirements"][0]["evidence_refs"][0][field] = value
            cases.append((field, changed))
        input_only = copy.deepcopy(payload)
        input_only["requirements"][0]["evidence_refs"][0]["artifact_id"] = "user_request_snapshot"
        cases.append(("input_only", input_only))
        for label, changed in cases:
            with self.subTest(locator=label), self.assertRaises(ValueError):
                SemanticReview.from_dict(
                    changed,
                    expected_requirement_ids={"original_user_request"},
                    evidence_artifact_ids=artifact_ids,
                    evidence_manifest=manifest,
                )

    def test_app_server_default_model_new_thread_and_named_permission_profile(self) -> None:
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
                    import os
                    import sys

                    if "--version" in sys.argv:
                        print("codex-cli fixture")
                        raise SystemExit(0)
                    if os.environ.get("HOST_SECRET_FOR_REVIEWER_TEST"):
                        raise SystemExit("host environment leaked into app-server")
                    profile_prefix = "permissions."
                    profile_suffix = ".filesystem="
                    profile_args = [value for value in sys.argv if value.startswith(profile_prefix) and profile_suffix in value]
                    if len(profile_args) != 1:
                        raise SystemExit("permission profile was not configured before app-server startup")
                    profile_id = profile_args[0].split(profile_suffix, 1)[0][len(profile_prefix):]
                    network_args = [value for value in sys.argv if value.startswith(f"permissions.{profile_id}.network=")]
                    if len(network_args) != 1:
                        raise SystemExit("permission profile network policy was not configured before app-server startup")
                    profile_listed = False

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
                            if not (message["params"].get("capabilities") or {}).get("experimentalApi"):
                                print(json.dumps({"id": message["id"], "error": {"code": 4, "message": "experimental api missing"}}), flush=True)
                                continue
                            print(json.dumps({"id": message["id"], "result": {"userAgent": "fixture"}}), flush=True)
                        elif method == "config/read":
                            print(json.dumps({
                                "id": message["id"],
                                "result": {
                                    "config": {
                                        "mcp_servers": {
                                            "host_secret_server": {
                                                "command": "/usr/bin/host-secret-mcp",
                                                "args": ["--secret-host-argument"],
                                                "env": None,
                                                "tool_timeout_sec": None
                                            },
                                            "serena": {
                                                "url": "http://127.0.0.1:9121/mcp",
                                                "startup_timeout_sec": 30.0,
                                                "tool_timeout_sec": None,
                                                "http_headers": {"Authorization": "must-not-copy"}
                                            }
                                        }
                                    },
                                    "origins": {}
                                }
                            }), flush=True)
                        elif method == "permissionProfile/list":
                            profile_listed = True
                            print(json.dumps({
                                "id": message["id"],
                                "result": {"data": [{"id": profile_id, "allowed": True}], "nextCursor": None}
                            }), flush=True)
                        elif method == "thread/start":
                            if not profile_listed:
                                print(json.dumps({"id": message["id"], "error": {"code": 13, "message": "profile was not listed"}}), flush=True)
                                continue
                            if message["params"].get("model") is not None:
                                print(json.dumps({"id": message["id"], "error": {"code": 1, "message": "model must be omitted"}}), flush=True)
                                continue
                            if "untrusted evidence" not in message["params"].get("developerInstructions", ""):
                                print(json.dumps({"id": message["id"], "error": {"code": 3, "message": "prompt injection boundary missing"}}), flush=True)
                                continue
                            params = message["params"]
                            profile_id = params.get("permissions")
                            config = params.get("config") or {}
                            filesystem = config.get(f"permissions.{profile_id}.filesystem")
                            network = config.get(f"permissions.{profile_id}.network")
                            if not profile_id or filesystem != {params["cwd"]: "read"} or network != {"enabled": False}:
                                print(json.dumps({"id": message["id"], "error": {"code": 5, "message": "permission profile missing"}}), flush=True)
                                continue
                            if params.get("ephemeral") is not True or params.get("environments") != [] or "sandbox" in params:
                                print(json.dumps({"id": message["id"], "error": {"code": 6, "message": "thread isolation fields invalid"}}), flush=True)
                                continue
                            if config.get("shell_environment_policy.inherit") != "none" or config.get("allow_login_shell") is not False:
                                print(json.dumps({"id": message["id"], "error": {"code": 7, "message": "shell environment policy missing"}}), flush=True)
                                continue
                            if config.get("project_doc_max_bytes") != 0:
                                print(json.dumps({"id": message["id"], "error": {"code": 8, "message": "instruction discovery is enabled"}}), flush=True)
                                continue
                            mcp_servers = config.get("mcp_servers") or {}
                            if mcp_servers.get("host_secret_server") != {"command": "/usr/bin/host-secret-mcp", "enabled": False}:
                                print(json.dumps({"id": message["id"], "error": {"code": 9, "message": "host MCP was inherited"}}), flush=True)
                                continue
                            if mcp_servers.get("serena") != {"url": "http://127.0.0.1:9121/mcp", "enabled": False}:
                                print(json.dumps({"id": message["id"], "error": {"code": 12, "message": "HTTP MCP was not minimized"}}), flush=True)
                                continue
                            if "codex_apps" in mcp_servers:
                                print(json.dumps({"id": message["id"], "error": {"code": 11, "message": "synthetic MCP lacks transport"}}), flush=True)
                                continue
                            if config.get("features.apps") is not False or config.get("skills.include_instructions") is not False:
                                print(json.dumps({"id": message["id"], "error": {"code": 10, "message": "host extensions were inherited"}}), flush=True)
                                continue
                            print(json.dumps({
                                "id": message["id"],
                                "result": {
                                    "thread": {"id": "thr_isolated"},
                                    "model": "app-server-default",
                                    "modelProvider": "fixture",
                                    "instructionSources": [],
                                    "activePermissionProfile": {"id": profile_id}
                                }
                            }), flush=True)
                        elif method == "turn/start":
                            if "sandboxPolicy" in message["params"] or "permissions" in message["params"]:
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

            with patch.dict("os.environ", {"HOST_SECRET_FOR_REVIEWER_TEST": "must-not-leak"}):
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
            self.assertEqual(receipt["runtime_workspace_roots"], [str(bundle.resolve())])
            self.assertEqual(receipt["active_permission_profile_id"], receipt["requested_permission_profile"]["id"])
            self.assertTrue(receipt["ephemeral"])
            self.assertEqual(receipt["shell_environment_policy"]["inherit"], "none")
            self.assertFalse(receipt["network_access"])

    def test_managed_requirements_denial_is_blocked_before_thread_start(self) -> None:
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
                    prefix = "permissions."
                    suffix = ".filesystem="
                    profile_arg = next(value for value in sys.argv if value.startswith(prefix) and suffix in value)
                    profile_id = profile_arg.split(suffix, 1)[0][len(prefix):]
                    for line in sys.stdin:
                        message = json.loads(line)
                        method = message.get("method")
                        if method == "initialize":
                            print(json.dumps({"id": message["id"], "result": {"userAgent": "fixture"}}), flush=True)
                        elif method == "config/read":
                            print(json.dumps({"id": message["id"], "result": {"config": {"mcp_servers": {}}, "origins": {}}}), flush=True)
                        elif method == "permissionProfile/list":
                            print(json.dumps({
                                "id": message["id"],
                                "result": {"data": [{"id": profile_id, "allowed": False}], "nextCursor": None}
                            }), flush=True)
                        elif method == "thread/start":
                            raise SystemExit("thread/start must not run for a forbidden profile")
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

            with self.assertRaises(SemanticReviewerError) as caught:
                reviewer.review(
                    job_id="job_reviewer_profile_denied",
                    attempt_id="attempt_001",
                    bundle_dir=bundle,
                    bundle_manifest={"artifacts": []},
                    invocation_count=1,
                    include_original_images=False,
                )

            self.assertEqual(caught.exception.code, "reviewer_permission_profile_forbidden")
            self.assertFalse(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
