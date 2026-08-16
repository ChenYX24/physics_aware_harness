from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.artifact_schema import read_json
from harness.core.stage_result import (
    STAGE_RESULT_SCHEMA_VERSION,
    StageResult,
    artifact_ref,
    build_stage_result,
    classify_failure,
    failure_stage_result,
    stage_result_from_compilation_report,
    stage_result_from_execution_report,
    stage_result_from_preflight_report,
    stage_result_from_provider_batch,
    stage_result_from_quality_report,
    stage_result_from_render_sync_report,
    stage_result_from_verifier_report,
    write_stage_result,
)
from harness.runtime.ue_backend import write_ue_preflight_result
from harness.verification.physics_verifier import PhysicsVerifier


class StageResultContractTests(unittest.TestCase):
    def test_standalone_result_keeps_nullable_job_identity(self) -> None:
        result = build_stage_result(
            stage="compile",
            status="completed",
            artifact_refs=[artifact_ref("report", "runtime_compilation_report.json")],
        )

        self.assertEqual(result["schema_version"], STAGE_RESULT_SCHEMA_VERSION)
        self.assertIsNone(result["job_id"])
        self.assertIsNone(result["attempt_id"])
        self.assertEqual(result["allowed_next_actions"], ["continue"])
        self.assertEqual(StageResult.from_dict(result).to_dict(), result)

    def test_failure_classification_uses_code_not_message(self) -> None:
        first = failure_stage_result(
            stage="execute",
            failure_code="F7_UE_RUNNER_TIMEOUT",
            message="first wording",
        )
        second = failure_stage_result(
            stage="execute",
            failure_code="F7_UE_RUNNER_TIMEOUT",
            message="completely different wording",
        )

        for field in ("status", "failure_class", "failure_code", "retryable", "allowed_next_actions"):
            self.assertEqual(first[field], second[field])
        self.assertEqual(first["failure_class"], "transient")
        self.assertTrue(first["retryable"])

    def test_secret_like_values_are_redacted_from_messages_and_refs(self) -> None:
        result = failure_stage_result(
            stage="provider",
            failure_code="provider_credentials_missing",
            message="Authorization: Bearer secret-value api_key=sk-abcdefghijk",
            required_action_message="token=private-token",
            artifact_refs=[artifact_ref("audit", "failure.json?token=private")],
        )
        serialized = str(result)

        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("sk-abcdefghijk", serialized)
        self.assertNotIn("private-token", serialized)
        self.assertNotIn("token=private", serialized)
        self.assertIn("[REDACTED]", serialized)

        json_style = failure_stage_result(
            stage="provider",
            failure_code="provider_credentials_missing",
            message='{"Authorization":"Bearer json-secret","api_key":"json-key"}',
        )
        self.assertNotIn("json-secret", str(json_style))
        self.assertNotIn("json-key", str(json_style))

    def test_validator_rejects_numeric_identity_and_artifact_fields(self) -> None:
        result = build_stage_result(stage="compile", status="completed")
        for field, value in (("job_id", 7), ("attempt_id", 1), ("stage", 2)):
            invalid = dict(result)
            invalid[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                StageResult.from_dict(invalid)
        for field in ("name", "path"):
            invalid = dict(result)
            invalid["artifact_refs"] = [{"name": "report", "path": "report.json", field: 3}]
            with self.subTest(artifact_field=field), self.assertRaises(ValueError):
                StageResult.from_dict(invalid)

    def test_retryable_cannot_override_hard_blockers(self) -> None:
        credentials = classify_failure("provider", "provider_credentials_missing", retryable=True)
        capability = classify_failure("compile", "unsupported_solver_capabilities", retryable=True)

        self.assertEqual(credentials["failure_class"], "blocked_user_action")
        self.assertFalse(credentials["retryable"])
        self.assertEqual(capability["failure_class"], "capability_missing")
        self.assertFalse(capability["retryable"])

    def test_permission_profile_unsupported_is_a_stable_configuration_blocker(self) -> None:
        for code in (
            "reviewer_permission_profile_unsupported",
            "reviewer_permission_profile_forbidden",
        ):
            with self.subTest(code=code):
                current = classify_failure("semantic_review", code)
                self.assertEqual(current["failure_class"], "blocked_configuration")
                self.assertEqual(current["status"], "blocked")
                self.assertFalse(current["retryable"])

    def test_invalid_success_failure_mix_is_rejected(self) -> None:
        result = build_stage_result(stage="compile", status="completed")
        result["failure_code"] = "impossible"

        with self.assertRaises(ValueError):
            StageResult.from_dict(result)

    def test_v1_reader_migrates_results_without_failure_codes(self) -> None:
        completed = build_stage_result(stage="compile", status="completed")
        completed.pop("failure_codes")
        failed = failure_stage_result(
            stage="provider",
            failure_code="provider_network_error",
            message="connection reset",
        )
        failed.pop("failure_codes")

        self.assertEqual(StageResult.from_dict(completed).to_dict()["failure_codes"], [])
        self.assertEqual(
            StageResult.from_dict(failed).to_dict()["failure_codes"],
            ["provider_network_error"],
        )

    def test_atomic_writer_uses_stage_results_directory(self) -> None:
        result = build_stage_result(stage="compile", status="completed")
        with tempfile.TemporaryDirectory() as temporary:
            path = write_stage_result(temporary, result)

            self.assertEqual(path, Path(temporary) / "stage_results" / "compile.json")
            self.assertEqual(read_json(path), result)

    def test_writer_redacts_secrets_from_direct_envelopes(self) -> None:
        result = failure_stage_result(
            stage="provider",
            failure_code="provider_execution_failed",
            message="safe placeholder",
        )
        result["message"] = '{"Authorization":"Bearer writer-secret"}'
        with tempfile.TemporaryDirectory() as temporary:
            path = write_stage_result(temporary, result)
            landed = read_json(path)

        self.assertNotIn("writer-secret", str(landed))
        self.assertIn("[REDACTED]", landed["message"])

    def test_ue_preflight_writer_preserves_source_report_and_adds_sidecar(self) -> None:
        report = {
            "schema_version": "harness_ue_preflight_report_v1",
            "failure_code": "F2_UE_EXECUTABLE_MISSING",
            "failure_message": "executable missing",
            "next_required_action": "Configure UE.",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_ue_preflight_result(root, report)

            self.assertEqual(read_json(root / "ue_preflight_report.json"), report)
            stage_result = read_json(root / "stage_results" / "preflight.json")
            self.assertEqual(stage_result["failure_class"], "blocked_configuration")

    def test_corrupt_verifier_input_writes_failure_sidecar_before_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "case_spec.json").write_text("{not-json", encoding="utf-8")

            with self.assertRaises(ValueError):
                PhysicsVerifier().verify_run_dir(root, write=True)

            result = read_json(root / "stage_results" / "verifier.json")
            self.assertEqual(result["failure_code"], "verifier_input_invalid")
            self.assertEqual(result["failure_class"], "artifact_incomplete")

    def test_capability_and_configuration_failures_have_distinct_actions(self) -> None:
        capability = classify_failure("compile", "multi_backend_handoff_contract_unavailable")
        configuration = classify_failure("preflight", "F2_UE_EXECUTABLE_MISSING")

        self.assertEqual(capability["failure_class"], "capability_missing")
        self.assertIn("open_development_issue", capability["allowed_next_actions"])
        self.assertEqual(configuration["failure_class"], "blocked_configuration")
        self.assertIn("fix_configuration", configuration["allowed_next_actions"])


class StageResultAdapterTests(unittest.TestCase):
    def test_compilation_adapter_preserves_stable_failure_code(self) -> None:
        result = stage_result_from_compilation_report(
            {
                "schema_version": "harness_runtime_compilation_report_v1",
                "status": "fail",
                "errors": [{"code": "F3_invalid_initial_physics_state", "message": "overlap"}],
            }
        )

        self.assertEqual(result["failure_code"], "F3_invalid_initial_physics_state")
        self.assertEqual(result["failure_class"], "case_spec_invalid")
        self.assertIn("revise_case_spec", result["allowed_next_actions"])

    def test_provider_adapter_normalizes_retriable_and_request_identity(self) -> None:
        digest = "a" * 64
        result = stage_result_from_provider_batch(
            {
                "schema_version": "harness_asset_provider_batch_v1",
                "requests": [{"request_digest": digest}],
                "results": [
                    {
                        "status": "failed",
                        "failure": {
                            "code": "provider_network_error",
                            "message": "connection reset",
                            "retriable": True,
                        },
                    }
                ],
            }
        )

        self.assertTrue(result["retryable"])
        self.assertEqual(result["failure_class"], "transient")
        self.assertEqual(result["request_identities"], [digest])
        self.assertEqual(result["invocation_count"], 1)
        self.assertIn("resume_checkpoint", result["allowed_next_actions"])

    def test_provider_adapter_retains_all_codes_and_prioritizes_hard_blocker(self) -> None:
        result = stage_result_from_provider_batch(
            {
                "schema_version": "harness_asset_provider_batch_v1",
                "requests": [{"request_digest": "a" * 64}, {"request_digest": "b" * 64}],
                "results": [
                    {
                        "status": "failed",
                        "failure": {"code": "provider_network_error", "message": "reset", "retriable": True},
                    },
                    {
                        "status": "blocked",
                        "failure": {"code": "provider_credentials_missing", "message": "key missing", "retriable": True},
                    },
                ],
            }
        )

        self.assertEqual(result["failure_code"], "provider_credentials_missing")
        self.assertEqual(result["failure_codes"], ["provider_network_error", "provider_credentials_missing"])
        self.assertEqual(result["failure_class"], "blocked_user_action")
        self.assertFalse(result["retryable"])

    def test_preflight_adapter_requires_configuration_action(self) -> None:
        result = stage_result_from_preflight_report(
            {
                "schema_version": "harness_ue_preflight_report_v1",
                "failure_code": "F1_UPROJECT_MISSING",
                "failure_message": "project missing",
                "next_required_action": "Set the project path.",
            }
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["required_user_action"]["message"], "Set the project path.")

    def test_execute_verifier_render_sync_and_quality_adapters(self) -> None:
        execution = stage_result_from_execution_report(
            {
                "schema_version": "harness_stage_execution_report_v1",
                "status": "failed",
                "completed_stages": [],
                "failure_code": "stage_handoff_incomplete",
                "failure_message": "cache missing",
            }
        )
        verifier = stage_result_from_verifier_report(
            {
                "schema_version": "harness_verifier_report_v1",
                "status": "fail",
                "failure_type": "F_ASSERTION_FAILED",
                "first_failure": {"value": "threshold exceeded"},
            }
        )
        render_sync = stage_result_from_render_sync_report(
            {
                "schema_version": "render_sync_report.v2.3",
                "status": "fail",
                "failure_codes": ["F_VIEW_MISMATCH"],
                "failures": [{"message": "view missing"}],
            }
        )
        quality = stage_result_from_quality_report(
            {
                "schema_version": "harness_run_quality_v1",
                "status": "fail",
                "hard_gate": {"failures": [{"code": "F_MEDIA_MISSING", "message": "video missing"}]},
            }
        )

        self.assertEqual(execution["failure_class"], "artifact_incomplete")
        self.assertEqual(verifier["failure_class"], "verification_failed")
        self.assertEqual(render_sync["failure_class"], "render_sync_failed")
        self.assertEqual(quality["failure_class"], "quality_gate_failed")

    def test_quality_adapter_retains_all_codes_and_prioritizes_solver_provenance(self) -> None:
        result = stage_result_from_quality_report(
            {
                "schema_version": "harness_run_quality_v1",
                "status": "fail",
                "hard_gate": {
                    "failures": [
                        {"code": "F_DEPTH_MISSING", "message": "depth missing"},
                        {"code": "F_RIGID_SOLVER_PROVENANCE", "message": "capture not proven"},
                        {"code": "F_SEGMENTATION_MISSING", "message": "segmentation missing"},
                    ]
                },
            }
        )

        self.assertEqual(result["failure_code"], "F_RIGID_SOLVER_PROVENANCE")
        self.assertEqual(
            result["failure_codes"],
            ["F_DEPTH_MISSING", "F_RIGID_SOLVER_PROVENANCE", "F_SEGMENTATION_MISSING"],
        )
        self.assertEqual(result["failure_class"], "execution_failed")
        self.assertIn("open_development_issue", result["allowed_next_actions"])

    def test_preview_shadow_quality_failures_are_classified_as_harness_defects(self) -> None:
        for code in (
            "F_UE_LIGHTING_REPORT_MISSING",
            "F_UE_PREVIEW_SHADOW_INDICATOR_ACTIVE",
            "F_UE_RUNTIME_LIGHT_MOBILITY_INVALID",
        ):
            with self.subTest(code=code):
                result = stage_result_from_quality_report(
                    {
                        "schema_version": "harness_run_quality_v1",
                        "status": "fail",
                        "hard_gate": {"failures": [{"code": code, "message": "render defect"}]},
                    }
                )
                self.assertEqual(result["failure_class"], "harness_bug")
                self.assertNotIn("revise_case_spec", result["allowed_next_actions"])
                self.assertIn("open_development_issue", result["allowed_next_actions"])


if __name__ == "__main__":
    unittest.main()
