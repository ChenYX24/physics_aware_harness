from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness.agent.job_controller import AgentJobController
from harness.agent.job_schema import stable_digest
from harness.agent.job_store import JobStoreError
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
        with self.assertRaisesRegex(JobStoreError, "already differs"):
            self.controller.submit_native_generation(context["job_id"], changed)

        wrong_job = copy.deepcopy(submission)
        wrong_job["job_id"] = "job_native_other"
        with self.assertRaisesRegex(ValueError, "job identity mismatch"):
            self.controller.submit_native_generation(context["job_id"], wrong_job)

    def test_tampered_context_cannot_be_rebound_by_recomputing_the_submission_digest(self) -> None:
        blocked, context = self._create_context("job_native_context_tamper")
        context["request"]["text"] = "tampered request"
        write_json(blocked["paths"]["native_generation_context"], context)
        submission = self._submission(context)

        with self.assertRaisesRegex(JobStoreError, "context identity mismatch"):
            self.controller.submit_native_generation(context["job_id"], submission)

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

        with self.assertRaisesRegex(ValueError, "metadata-only"):
            self.controller.submit_native_generation(context["job_id"], submission)

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
        with self.assertRaisesRegex(ValueError, "must be reported as used"):
            self.controller.submit_native_generation(context["job_id"], missing)

        accepted = self._submission(context)
        accepted["agent_reported"]["image_input_ids_used"] = ["request_image_0"]
        self.controller.submit_native_generation(context["job_id"], accepted)
        inspection = self.controller.advance_until_blocked(context["job_id"])

        self.assertEqual(inspection["job"]["state"], "awaiting_semantic_review")
        intent = read_json(inspection["paths"]["intent_contract"])
        self.assertEqual(intent["hard_requirements"][0]["id"], "original_user_visual_inputs")

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
