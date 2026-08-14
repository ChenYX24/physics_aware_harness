from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.agent.job_controller import AgentJobController
from harness.agent.job_schema import stable_digest
from harness.agent.native_generation import NATIVE_GENERATION_SUBMISSION_SCHEMA_VERSION
from harness.assets.sqlite_catalog import initialize_catalog
from harness.core.artifact_schema import read_json, write_json
from harness.core.case_spec_v2 import compile_case_spec_v2_runtime
from harness.planning.backend_planner import plan_backend
from harness.planning.case_generation import build_case_request
from tests.test_agent_job_controller import SuccessfulHarness, case_spec_v2_fixture


class NativeGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        initialize_catalog(self.workspace / "catalog" / "assets" / "catalog.sqlite")
        self.fake = SuccessfulHarness()
        self.controller = AgentJobController(self.workspace, hooks=self.fake.hooks())
        self.request = build_case_request(
            case_id="native_case",
            text="Make one ball hit another.",
            requested_backend="fallback",
        )

    def _create_context(self, job_id: str = "job_native_generation") -> tuple[dict, dict]:
        self.controller.create(self.request, job_id=job_id, publication_tier="local_preview")
        blocked = self.controller.advance_until_blocked(job_id)
        context = read_json(blocked["paths"]["native_generation_context"])
        return blocked, context

    @staticmethod
    def _submission(context: dict, *, model_turn_count: int | None = 2) -> dict:
        return {
            "schema_version": NATIVE_GENERATION_SUBMISSION_SCHEMA_VERSION,
            "job_id": context["job_id"],
            "generation_context_digest": stable_digest(context),
            "intent_draft": {
                "hard_requirements": [
                    {"id": "contact_required", "text": "The two balls must make contact."},
                ],
                "soft_preferences": [],
                "prohibitions": [],
                "ambiguities": [],
                "parameter_analysis": [
                    {
                        "path": "$.scene.duration_s",
                        "requirement_level": "inferred",
                        "reason": "The duration is an inferred capture window.",
                        "constraint": {"kind": "numeric", "min": 1.0, "max": 3.0},
                    }
                ],
            },
            "case_spec": case_spec_v2_fixture(),
            "agent_reported": {
                "thread_id": "thread_from_tui",
                "model": "agent-selected-model",
                "model_provider": "agent-reported-provider",
                "model_turn_count": model_turn_count,
                "image_input_ids_used": [],
            },
        }

    def test_new_job_defaults_to_native_context_and_never_calls_legacy_generator(self) -> None:
        blocked, context = self._create_context()

        self.assertEqual(blocked["job"]["state"], "blocked")
        self.assertEqual(blocked["job"]["blocker"]["code"], "native_generation_submission_required")
        self.assertEqual(blocked["job"]["allowed_next_actions"], ["submit_native_generation", "cancel"])
        self.assertEqual(self.fake.generation_calls, 0)
        self.assertEqual(blocked["job"]["usage"]["generation_invocations"], 0)
        self.assertEqual(context["agent_reporting_contract"]["controller_observed_invocation_count"], 0)
        self.assertEqual(blocked["native_generation_context_digest"], stable_digest(context))
        self.assertEqual(context["submission_contract"]["schema_version"], NATIVE_GENERATION_SUBMISSION_SCHEMA_VERSION)
        pause_result = read_json(Path(blocked["paths"]["job_root"]) / "stage_results" / "generation.json")
        self.assertEqual(pause_result["allowed_next_actions"], ["submit_native_generation", "cancel"])
        self.assertEqual(pause_result["failure_class"], "awaiting_agent_action")
        self.assertIsNone(pause_result["required_user_action"])
        self.assertIn("backend_stage_io", context["case_spec_contract"])
        self.assertIn("valid_structure_example_do_not_copy_values", context["case_spec_contract"])

    def test_cli_exposes_native_default_and_submission_command(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "harness_agent_job.py"
        create_help = subprocess.run(
            [sys.executable, str(script), "create", "--help"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        submit_help = subprocess.run(
            [sys.executable, str(script), "submit-generation", "--help"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("--generation-mode {native,legacy}", create_help)
        self.assertIn("--submission", submit_help)

    def test_cli_invalid_submission_is_structured_jsonl_without_traceback(self) -> None:
        blocked, context = self._create_context("job_native_cli_reject")
        invalid = self.workspace / "invalid_submission.json"
        invalid.write_text("{not-json", encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "harness_agent_job.py"

        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--workspace",
                str(self.workspace),
                "--jsonl",
                "submit-generation",
                context["job_id"],
                "--submission",
                str(invalid),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(events)
        result = events[-1]["result"]
        self.assertEqual(result["failure_code"], "native_generation_submission_schema_invalid")
        self.assertEqual(result["allowed_next_actions"], ["submit_native_generation", "cancel"])
        self.assertNotIn("Traceback", completed.stderr)
        request_root = Path(blocked["paths"]["job_root"]) / "request"
        self.assertFalse((request_root / "native_generation_submission.json").exists())
        self.assertFalse((request_root / "native_generation_ack.json").exists())

    def test_valid_submission_is_acked_then_uses_existing_controller_chain(self) -> None:
        _, context = self._create_context()
        submission = self._submission(context, model_turn_count=4)

        accepted = self.controller.submit_native_generation(context["job_id"], submission)
        inspection = self.controller.advance_until_blocked(context["job_id"])

        self.assertEqual(accepted["job"]["state"], "running")
        self.assertEqual(inspection["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(inspection["job"]["usage"]["generation_invocations"], 0)
        self.assertEqual(self.fake.generation_calls, 0)
        intent = read_json(inspection["paths"]["intent_contract"])
        self.assertEqual(intent["schema_version"], "harness_intent_contract_v3")
        self.assertEqual(intent["source"], "agent_native_submission_v1")
        self.assertEqual(
            {row["id"] for row in intent["hard_requirements"]},
            {"original_user_request", "contact_required"},
        )
        ack = read_json(inspection["paths"]["native_generation_ack"])
        self.assertEqual(ack["controller_observed"]["controller_model_invocations"], 0)
        self.assertEqual(ack["agent_reported"]["model_turn_count"], 4)

    def test_submission_is_idempotent_but_changed_or_cross_job_content_is_rejected(self) -> None:
        _, context = self._create_context("job_native_idempotent")
        submission = self._submission(context)
        self.controller.submit_native_generation(context["job_id"], submission)
        self.controller.submit_native_generation(context["job_id"], submission)

        changed = copy.deepcopy(submission)
        changed["agent_reported"]["model_turn_count"] = 3
        rejected = self.controller.submit_native_generation(context["job_id"], changed)
        self.assertEqual(
            rejected["submission_stage_result"]["failure_code"],
            "native_generation_submission_immutable_conflict",
        )
        self.assertEqual(rejected["job"]["allowed_next_actions"], ["advance", "cancel"])

        wrong_job = copy.deepcopy(submission)
        wrong_job["job_id"] = "job_native_other"
        rejected = self.controller.submit_native_generation(context["job_id"], wrong_job)
        self.assertEqual(
            rejected["submission_stage_result"]["failure_code"],
            "native_generation_submission_context_mismatch",
        )

    def test_tampered_context_cannot_be_rebound_by_recomputing_the_submission_digest(self) -> None:
        blocked, context = self._create_context("job_native_context_tamper")
        context["request"]["text"] = "tampered request"
        write_json(blocked["paths"]["native_generation_context"], context)
        submission = self._submission(context)

        rejected = self.controller.submit_native_generation(context["job_id"], submission)
        self.assertEqual(
            rejected["submission_stage_result"]["failure_code"],
            "native_generation_context_identity_mismatch",
        )
        self.assertEqual(rejected["submission_stage_result"]["failure_class"], "blocked_configuration")

    def test_optional_image_pixels_cannot_be_claimed_by_native_submission(self) -> None:
        image = self.workspace / "optional.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nnative")
        request = build_case_request(
            case_id="native_optional_image",
            text="Use the image as optional context.",
            image_paths=[image],
            allow_image_upload=True,
            requested_backend="fallback",
        )
        self.controller.create(
            request,
            job_id="job_native_optional_image",
            publication_tier="local_preview",
            authorizations={"planning_llm_upload": True},
        )
        blocked = self.controller.advance_until_blocked("job_native_optional_image")
        context = read_json(blocked["paths"]["native_generation_context"])
        submission = self._submission(context)
        submission["agent_reported"]["image_input_ids_used"] = ["request_image_0"]

        rejected = self.controller.submit_native_generation(context["job_id"], submission)
        self.assertEqual(
            rejected["submission_stage_result"]["failure_code"],
            "native_generation_image_use_declaration_invalid",
        )

    def test_required_image_usage_requires_exact_authorized_agent_report(self) -> None:
        image = self.workspace / "required.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nrequired-native")
        request = build_case_request(
            case_id="native_required_image",
            image_paths=[image],
            allow_image_upload=True,
            requested_backend="fallback",
        )
        self.controller.create(
            request,
            job_id="job_native_required_image",
            publication_tier="local_preview",
            authorizations={"planning_llm_upload": True},
        )
        blocked = self.controller.advance_until_blocked("job_native_required_image")
        context = read_json(blocked["paths"]["native_generation_context"])
        missing = self._submission(context)
        rejected = self.controller.submit_native_generation(context["job_id"], missing)
        self.assertEqual(
            rejected["submission_stage_result"]["failure_code"],
            "native_generation_image_use_declaration_invalid",
        )

        accepted = self._submission(context)
        accepted["agent_reported"]["image_input_ids_used"] = ["request_image_0"]
        self.controller.submit_native_generation(context["job_id"], accepted)
        inspection = self.controller.advance_until_blocked(context["job_id"])

        self.assertEqual(inspection["job"]["state"], "awaiting_semantic_review")
        intent = read_json(inspection["paths"]["intent_contract"])
        self.assertEqual(intent["hard_requirements"][0]["id"], "original_user_visual_inputs")

    def test_invalid_submission_boundaries_are_structured_and_do_not_write_immutable_artifacts(self) -> None:
        mutations = {
            "native_generation_submission_schema_invalid": lambda value: value.pop("agent_reported"),
            "native_generation_submission_context_mismatch": lambda value: value.__setitem__(
                "generation_context_digest", "0" * 64
            ),
            "native_generation_intent_draft_invalid": lambda value: value["intent_draft"]["hard_requirements"].append(
                {"id": "contact_required", "text": "duplicate ID"}
            ),
            "native_generation_case_spec_invalid": lambda value: value["case_spec"]["scene"].__setitem__(
                "duration_s", -1.0
            ),
            "native_generation_parameter_constraint_invalid": lambda value: value["intent_draft"][
                "parameter_analysis"
            ][0].__setitem__("path", "$.scene.missing_leaf"),
        }
        for index, (expected_code, mutate) in enumerate(mutations.items(), start=1):
            with self.subTest(expected_code=expected_code):
                job_id = f"job_native_reject_{index:02d}"
                blocked, context = self._create_context(job_id)
                submission = self._submission(context)
                mutate(submission)

                rejected = self.controller.submit_native_generation(job_id, submission)

                result = rejected["submission_stage_result"]
                self.assertEqual(result["schema_version"], "harness_stage_result_v1")
                self.assertEqual(result["failure_code"], expected_code)
                self.assertEqual(result["failure_class"], "agent_submission_invalid")
                self.assertEqual(result["allowed_next_actions"], ["submit_native_generation", "cancel"])
                self.assertIsNone(result["required_user_action"])
                self.assertTrue(result["artifact_refs"])
                self.assertEqual(rejected["job"]["allowed_next_actions"], ["submit_native_generation", "cancel"])
                request_root = Path(blocked["paths"]["job_root"]) / "request"
                self.assertFalse((request_root / "native_generation_submission.json").exists())
                self.assertFalse((request_root / "native_generation_ack.json").exists())
                self.assertEqual(
                    read_json(Path(blocked["paths"]["job_root"]) / "stage_results" / "generation.json")[
                        "failure_code"
                    ],
                    expected_code,
                )

    def test_frozen_context_survives_current_contract_presentation_changes(self) -> None:
        blocked, context = self._create_context("job_native_frozen_contract")
        original_digest = blocked["native_generation_context_digest"]
        # Simulate a waiting M5 Job created before the independent identity
        # sidecar existed. Recovery must freeze this context, not regenerate it.
        Path(blocked["paths"]["native_generation_context_identity"]).unlink()
        changed_current_contract = {
            "schema_version": "harness_case_spec_v2",
            "enums": {"backend": ["description-only-change"]},
            "valid_structure_example_do_not_copy_values": {"changed": True},
            "description": "new current prose",
        }

        with patch(
            "harness.agent.native_generation.case_spec_generation_contract",
            return_value=changed_current_contract,
        ):
            accepted = self.controller.submit_native_generation(context["job_id"], self._submission(context))
            inspection = self.controller.advance_until_blocked(context["job_id"])

        self.assertEqual(accepted["native_generation_context_digest"], original_digest)
        self.assertEqual(inspection["native_generation_context_digest"], original_digest)
        self.assertTrue(Path(inspection["paths"]["native_generation_context_identity"]).is_file())
        self.assertEqual(inspection["job"]["state"], "awaiting_semantic_review")

    def test_unsupported_frozen_context_schema_is_a_structured_migration_blocker(self) -> None:
        blocked, context = self._create_context("job_native_unsupported_context")
        context["schema_version"] = "harness_native_generation_context_v0"
        write_json(blocked["paths"]["native_generation_context"], context)

        inspection = self.controller.advance_until_blocked(context["job_id"])
        result = read_json(Path(inspection["paths"]["job_root"]) / "stage_results" / "generation.json")

        self.assertEqual(inspection["job"]["state"], "blocked")
        self.assertEqual(result["failure_code"], "native_generation_context_schema_unsupported")
        self.assertEqual(result["failure_class"], "blocked_configuration")
        self.assertEqual(inspection["job"]["allowed_next_actions"], ["resume", "cancel"])

    def test_prohibition_is_reviewed_exactly_and_a_violation_cannot_complete(self) -> None:
        fake = SuccessfulHarness()
        hooks = fake.hooks()
        passing_review = hooks.semantic_review

        def prohibition_violation(**kwargs):
            result = passing_review(**kwargs)
            for row in result["review"]["requirements"]:
                if row["requirement_id"] == "no_overlap":
                    row["status"] = "fail"
                    row["rationale"] = "the result visibly violates the frozen prohibition"
            result["review"]["overall_status"] = "fail"
            result["review"]["repair_layer"] = "user_decision"
            result["review"]["summary"] = "a frozen prohibition was violated"
            result["receipt"]["output_digest"] = stable_digest(result["review"])
            return result

        hooks.semantic_review = prohibition_violation
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_native_prohibition",
            publication_tier="local_preview",
        )
        blocked = controller.advance_until_blocked("job_native_prohibition")
        context = read_json(blocked["paths"]["native_generation_context"])
        submission = self._submission(context)
        submission["intent_draft"]["prohibitions"] = [
            {"id": "no_overlap", "text": "The balls must never interpenetrate."}
        ]
        controller.submit_native_generation(context["job_id"], submission)
        awaiting = controller.advance_until_blocked(context["job_id"])
        intent = read_json(awaiting["paths"]["intent_contract"])
        self.assertNotIn("no_overlap", {row["id"] for row in intent["hard_requirements"]})
        self.assertEqual([row["id"] for row in intent["prohibitions"]], ["no_overlap"])
        summary = read_json(
            Path(awaiting["paths"]["job_root"])
            / "attempts"
            / "attempt_001"
            / "evidence_bundle"
            / "evidence_summary.json"
        )
        prohibition = next(row for row in summary["semantic_requirements"] if row["id"] == "no_overlap")
        self.assertEqual(prohibition["source"], "intent_prohibition")
        self.assertEqual(prohibition["polarity"], "prohibition")

        reviewed = controller.run_semantic_review(context["job_id"])
        self.assertNotEqual(reviewed["job"]["state"], "completed")
        self.assertEqual(reviewed["job"]["blocker"]["code"], "semantic_intent_mismatch")
        review = read_json(
            Path(reviewed["paths"]["job_root"])
            / "attempts"
            / "attempt_001"
            / "semantic_review.json"
        )
        self.assertEqual(
            [row["requirement_id"] for row in review["requirements"]].count("no_overlap"),
            1,
        )
        self.assertEqual(
            next(row for row in review["requirements"] if row["requirement_id"] == "no_overlap")["status"],
            "fail",
        )

    def test_prompt_and_object_renaming_do_not_change_a_structurally_equivalent_runtime_route(self) -> None:
        first = case_spec_v2_fixture()
        second = copy.deepcopy(first)
        mapping = {"cue_ball": "moving_sphere", "target_ball": "resting_sphere", "floor": "support_plane"}
        for obj in second["objects"]:
            obj["id"] = mapping[obj["id"]]
        for relation in second["relations"]:
            relation["source"] = mapping[relation["source"]]
            relation["target"] = mapping[relation["target"]]
        for event in second["events"]:
            event["object"] = mapping[event["object"]]
        for camera in second["observation_requirements"]["cameras"]:
            camera["target_objects"] = [mapping[value] for value in camera["target_objects"]]
        for assertion in second["verification_requirements"]["assertions"]:
            assertion["objects"] = [mapping[value] for value in assertion["objects"]]
        request_a = build_case_request(case_id="route_a", text="Make one ball hit another.", requested_backend="fallback")
        request_b = build_case_request(case_id="route_b", text="Show a sphere transferring motion.", requested_backend="fallback")
        case_a = self.controller._native_case_spec(request_a, first)
        case_b = self.controller._native_case_spec(request_b, second)

        route_a = plan_backend(compile_case_spec_v2_runtime(case_a).data, source_case_spec=case_a, requested_backend="fallback")
        route_b = plan_backend(compile_case_spec_v2_runtime(case_b).data, source_case_spec=case_b, requested_backend="fallback")

        for field in ("selected_backend", "render_backend", "multi_backend", "stages", "handoff_contract"):
            self.assertEqual(route_a[field], route_b[field])


if __name__ == "__main__":
    unittest.main()
