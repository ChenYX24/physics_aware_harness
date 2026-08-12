from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness.agent.job_controller import AgentJobController, ControllerHooks
from harness.agent.job_schema import DEFAULT_BUDGET, JobManifest
from harness.assets.sqlite_catalog import initialize_catalog
from harness.core.artifact_schema import read_json, write_json
from harness.core.case_spec_v2 import case_spec_v2_from_dict, compile_case_spec_v2_runtime
from harness.core.stage_result import build_stage_result, failure_stage_result, write_stage_result
from harness.planning.case_generation import CaseGenerationError, build_case_request
from harness.planning.runtime_compiler import RuntimeCompilation
from tests.case_spec_v2_fixture import case_spec_v2_fixture


class SuccessfulHarness:
    def __init__(self, *, selected_backend: str = "fallback", fail_verifier: bool = False) -> None:
        self.selected_backend = selected_backend
        self.fail_verifier = fail_verifier
        self.compile_calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.execute_calls: list[str] = []
        self.generation_calls = 0

    def generate(self, request: dict, *, artifact_dir: Path, job_id: str, attempt_id: str):
        self.generation_calls += 1
        case = case_spec_v2_from_dict(case_spec_v2_fixture())
        expansion = {"schema_version": "harness_expansion_v1", "ambiguities": [], "assumptions": []}
        result = build_stage_result(
            stage="generation",
            status="completed",
            job_id=job_id,
            attempt_id=attempt_id,
            invocation_count=2,
        )
        write_json(artifact_dir / "request.json", request)
        write_json(artifact_dir / "expansion.json", expansion)
        write_json(artifact_dir / "case_spec_v2.json", case.data)
        write_stage_result(artifact_dir, result)
        return SimpleNamespace(case_spec=case, expansion=expansion, stage_result=result)

    def compile(self, case_spec, **kwargs):
        views = tuple(kwargs.get("requested_views") or ())
        passes = tuple(kwargs.get("render_passes") or ())
        self.compile_calls.append((views, passes))
        runtime_case = compile_case_spec_v2_runtime(case_spec)
        backend = self.selected_backend
        artifacts = {
            "asset_resolution": {"schema_version": "asset_resolution.v1", "assets": []},
            "scene_layout": {"schema_version": "scene_layout.v1", "views": list(views)},
            "verification_plan": {"schema_version": "verification_plan.v1"},
            "observation_plan": {
                "schema_version": "observation_plan.v1",
                "views": [{"camera_id": value} for value in views],
                "render_passes": list(passes),
            },
            "camera_plan": {"schema_version": "camera_plan.v1", "views": [{"camera_id": value} for value in views]},
            "runtime_actor_placement": {"schema_version": "runtime_actor_placement.v1", "actors": []},
            "runtime_plan": {
                "schema_version": "runtime_plan.v1",
                "stages": [{"id": "solve_render", "kind": "solve_render", "backend": backend}],
            },
            "asset_provider_batch": {
                "schema_version": "harness_asset_provider_batch_v1",
                "case_id": runtime_case.case_id,
                "requests": [],
                "results": [],
                "receipt_ids": [],
            },
        }
        report = {
            "schema_version": "harness_runtime_compilation_report_v1",
            "status": "pass",
            "errors": [],
            "asset_resolve_invocation_count": 1,
        }
        stage_result = build_stage_result(
            stage="compile",
            status="completed",
            job_id=kwargs.get("job_id"),
            attempt_id=kwargs.get("attempt_id"),
        )
        transaction = Path(kwargs["transaction_dir"])
        write_json(
            transaction / "compilation_transaction.json",
            {
                "schema_version": "harness_runtime_compilation_transaction_v1",
                "transaction_id": "compilation_" + "a" * 24,
                "input_identity": {"case_spec_digest": "a" * 64, "requested_backend": None},
                "latest_projection": {"requested_views": list(views), "render_passes": list(passes), "camera_strategy": "bounds_auto_v1"},
                "state": "completed",
                "asset_resolve_invocation_count": 1,
                "catalog_snapshot": None,
                "updated_at_epoch": 0.0,
            },
        )
        write_stage_result(kwargs["stage_result_dir"], stage_result)
        return RuntimeCompilation(
            source_case_spec=copy.deepcopy(case_spec.data),
            runtime_case=runtime_case,
            backend_selection={
                "selected_backend": backend,
                "render_backend": backend,
                "target_asset_backend": backend,
            },
            compiled_asset_intents=(),
            artifacts=artifacts,
            report=report,
            stage_result=stage_result,
        )

    def execute(self, case, output_root, *, compilation, profile, **kwargs):
        self.execute_calls.append(profile)
        run_dir = Path(output_root) / f"{case.case_id}_{compilation.selected_backend}"
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(run_dir / "case_spec.json", case.data)
        write_json(run_dir / "camera_plan.json", compilation.artifacts["camera_plan"])
        write_json(run_dir / "trajectory.json", [{"frame": 0, "time": 0.0, "objects": {}}])
        write_json(run_dir / "stage_execution_report.json", {"status": "completed"})
        write_stage_result(run_dir, build_stage_result(stage="execute", status="completed"))
        return run_dir

    def verify(self, run_dir: Path):
        passed = not self.fail_verifier
        report = {
            "schema_version": "harness_verifier_report_v1",
            "status": "pass" if passed else "fail",
            "failure_type": None if passed else "declared_assertion_failed",
        }
        write_json(run_dir / "harness_verifier.json", report)
        result = (
            build_stage_result(stage="verifier", status="completed")
            if passed
            else failure_stage_result(
                stage="verifier",
                failure_code="declared_assertion_failed",
                message="assertion failed",
            )
        )
        write_stage_result(run_dir, result)
        return report

    @staticmethod
    def render_sync(run_dir: Path, **kwargs):
        report = {"schema_version": "render_sync_report.v2.3", "status": "pass", "failure_codes": []}
        write_json(run_dir / "render_sync_report.json", report)
        write_stage_result(run_dir, build_stage_result(stage="render_sync", status="completed"))
        return report

    @staticmethod
    def quality(run_dir: Path):
        report = {
            "schema_version": "harness_run_quality_v1",
            "status": "pass",
            "hard_gate_passed": True,
            "hard_gate": {"failures": []},
        }
        write_json(run_dir / "quality_report.json", report)
        write_stage_result(run_dir, build_stage_result(stage="quality_gate", status="completed"))
        return report

    def hooks(self) -> ControllerHooks:
        return ControllerHooks(
            generate=self.generate,
            compile=self.compile,
            execute=self.execute,
            verify=self.verify,
            render_sync=self.render_sync,
            quality=self.quality,
        )


class AgentJobControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        initialize_catalog(self.workspace / "catalog" / "assets" / "catalog.sqlite")
        self.request = build_case_request(case_id="agent_case", text="drop a ball", requested_backend="fallback")

    def controller(self, harness: SuccessfulHarness | None = None) -> tuple[AgentJobController, SuccessfulHarness]:
        fake = harness or SuccessfulHarness()
        return AgentJobController(self.workspace, hooks=fake.hooks()), fake

    def test_full_m2_technical_chain_stops_before_semantic_review(self) -> None:
        controller, fake = self.controller()
        controller.create(
            self.request,
            job_id="job_technical_chain",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )

        inspection = controller.advance_until_blocked("job_technical_chain")

        self.assertEqual(inspection["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(inspection["job"]["current_stage"], "semantic_review")
        self.assertNotEqual(inspection["job"]["state"], "completed")
        self.assertEqual(fake.execute_calls, ["smoke", "candidate"])
        self.assertEqual(len(inspection["attempts"]), 1)
        attempt_root = Path(inspection["paths"]["job_root"]) / "attempts" / "attempt_001"
        gate = read_json(attempt_root / "smoke_gate.json")
        self.assertEqual(gate["mode"], "executed")
        self.assertEqual(gate["status"], "pass")
        self.assertNotEqual(fake.compile_calls[0], fake.compile_calls[-1])
        candidate = read_json(attempt_root / "candidate_run.json")
        self.assertTrue((Path(candidate["run_dir"]) / "quality_report.json").is_file())

    def test_l0_missing_catalog_blocks_before_generation(self) -> None:
        missing_workspace = Path(self.temporary.name) / "missing_catalog_workspace"
        missing_workspace.mkdir()
        fake = SuccessfulHarness()
        controller = AgentJobController(missing_workspace, hooks=fake.hooks())
        controller.create(self.request, job_id="job_missing_catalog", publication_tier="local_preview")

        inspection = controller.advance_until_blocked("job_missing_catalog")

        self.assertEqual(inspection["job"]["state"], "blocked")
        self.assertEqual(inspection["job"]["blocker"]["code"], "catalog_missing")
        self.assertEqual(fake.generation_calls, 0)
        self.assertEqual(fake.compile_calls, [])
        self.assertEqual(fake.execute_calls, [])

    def test_intent_ambiguity_blocks_before_task_readiness_and_compile(self) -> None:
        fake = SuccessfulHarness()

        def ambiguous(request: dict, *, artifact_dir: Path, job_id: str, attempt_id: str):
            generated = fake.generate(request, artifact_dir=artifact_dir, job_id=job_id, attempt_id=attempt_id)
            generated.expansion["ambiguities"] = [{"question": "which object should move?"}]
            write_json(artifact_dir / "expansion.json", generated.expansion)
            return generated

        hooks = fake.hooks()
        hooks.generate = ambiguous
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(self.request, job_id="job_ambiguous_intent", publication_tier="local_preview")

        inspection = controller.advance_until_blocked("job_ambiguous_intent")

        self.assertEqual(inspection["job"]["state"], "blocked")
        self.assertEqual(inspection["job"]["blocker"]["code"], "intent_ambiguity_requires_decision")
        self.assertEqual(fake.compile_calls, [])
        self.assertEqual(fake.execute_calls, [])

        with self.assertRaisesRegex(ValueError, "intent_amendment"):
            controller.resume("job_ambiguous_intent")
        resumed = controller.resume(
            "job_ambiguous_intent",
            intent_amendment={
                "ambiguity_resolutions": [{"question": "which object should move?", "decision": "cue_ball"}],
                "reason": "user selected cue_ball",
            },
        )
        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")
        self.assertTrue(
            (Path(resumed["paths"]["job_root"]) / "request" / "intent_amendment_001.json").is_file()
        )

    def test_external_provider_authorization_blocks_with_zero_downstream_calls(self) -> None:
        external = case_spec_v2_fixture()
        external["objects"][0]["asset"] = {
            "description": "external impact sphere",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "external_site",
                "requirement": "required",
                "origin": "user_explicit",
                "provider_hint": "poly_haven",
                "reference_inputs": [],
                "fallback_order": [],
            },
        }
        controller, fake = self.controller()
        controller.create(
            self.request,
            job_id="job_external_auth",
            publication_tier="local_preview",
            seed_case_spec=external,
        )

        inspection = controller.advance_until_blocked("job_external_auth")

        self.assertEqual(inspection["job"]["state"], "blocked")
        self.assertEqual(inspection["job"]["blocker"]["code"], "external_provider_authorization_missing")
        self.assertEqual(fake.compile_calls, [])
        self.assertEqual(fake.execute_calls, [])

    def test_keyboard_interrupt_pauses_and_resume_uses_same_job(self) -> None:
        fake = SuccessfulHarness()
        original = fake.generate
        calls = 0

        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt()
            return original(*args, **kwargs)

        hooks = fake.hooks()
        hooks.generate = interrupted
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(self.request, job_id="job_interrupt_resume", publication_tier="local_preview")

        paused = controller.advance_until_blocked("job_interrupt_resume")
        self.assertEqual(paused["job"]["state"], "paused_interrupted")
        self.assertEqual(paused["job"]["current_stage"], "generation")

        resumed = controller.resume("job_interrupt_resume")
        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(resumed["job"]["current_attempt_id"], "attempt_001")

    def test_transient_stage_retries_once_without_new_attempt(self) -> None:
        fake = SuccessfulHarness()
        original = fake.generate
        calls = 0

        def transient(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise CaseGenerationError("llm_network_error", "reset", retryable=True)
            return original(*args, **kwargs)

        hooks = fake.hooks()
        hooks.generate = transient
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(self.request, job_id="job_transient_retry", publication_tier="local_preview")

        inspection = controller.advance_until_blocked("job_transient_retry")

        self.assertEqual(inspection["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(inspection["job"]["usage"]["total_retries"], 1)
        self.assertEqual(inspection["job"]["usage"]["stage_retries"], {"generation": 1})
        self.assertEqual([row["attempt_id"] for row in inspection["attempts"]], ["attempt_001"])

    def test_resume_after_verifier_interrupt_reuses_completed_execution(self) -> None:
        fake = SuccessfulHarness()
        original_verify = fake.verify
        calls = 0

        def interrupted_verify(run_dir: Path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt()
            return original_verify(run_dir)

        hooks = fake.hooks()
        hooks.verify = interrupted_verify
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_verifier_resume",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )

        paused = controller.advance_until_blocked("job_verifier_resume")
        self.assertEqual(paused["job"]["state"], "paused_interrupted")
        self.assertEqual(fake.execute_calls, ["smoke"])

        restarted = AgentJobController(self.workspace, hooks=hooks)
        resumed = restarted.resume("job_verifier_resume")
        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(fake.execute_calls, ["smoke", "candidate"])

    def test_default_budget_and_hard_deadline_gate(self) -> None:
        self.assertEqual(DEFAULT_BUDGET["max_case_spec_revisions"], 5)
        self.assertEqual(DEFAULT_BUDGET["max_ue_launches"], 6)
        self.assertEqual(DEFAULT_BUDGET["hard_deadline_seconds"], 1800)
        controller, _ = self.controller()
        controller.create(
            self.request,
            job_id="job_budget_gate",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
            budget={"soft_deadline_seconds": 0, "hard_deadline_seconds": 0},
        )

        inspection = controller.advance_until_blocked("job_budget_gate")

        self.assertEqual(inspection["job"]["state"], "needs_user_decision")
        self.assertEqual(inspection["job"]["blocker"]["code"], "budget_exhausted")

    def test_ue_launch_budget_stops_before_backend_invocation(self) -> None:
        controller, fake = self.controller(SuccessfulHarness(selected_backend="ue"))
        controller.create(
            self.request,
            job_id="job_ue_budget",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
            budget={"max_ue_launches": 0},
        )

        inspection = controller.advance_until_blocked("job_ue_budget")

        self.assertEqual(inspection["job"]["state"], "blocked")
        self.assertEqual(inspection["job"]["blocker"]["code"], "ue_launch_budget_exhausted")
        self.assertEqual(fake.execute_calls, [])

    def test_paid_provider_checkpoint_is_counted_once_by_request_identity(self) -> None:
        controller, _ = self.controller(SuccessfulHarness(fail_verifier=True))
        controller.create(
            self.request,
            job_id="job_paid_identity",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
            budget={"max_paid_submissions": 1},
            authorizations={"external_provider": True, "paid_provider_submission": True},
        )
        inspection = controller.advance_until_blocked("job_paid_identity")
        digest = "d" * 64
        root = Path(inspection["paths"]["job_root"])
        write_json(
            root / "attempts" / "attempt_001" / "compilation" / "asset_provider_batch.json",
            {
                "schema_version": "harness_asset_provider_batch_v1",
                "case_id": "agent_case",
                "requests": [{"route": "model_generation", "request_digest": digest}],
                "results": [],
                "receipt_ids": [],
            },
        )
        write_json(
            self.workspace / "providers" / "meshy_model_generation_v1" / digest / "task_checkpoint.json",
            {"task_id": "paid-task-123"},
        )

        manifest = controller._reconcile_provider_usage(inspection["job"])
        manifest = controller._reconcile_provider_usage(manifest)

        self.assertEqual(manifest["usage"]["paid_submissions"], 1)
        usage = read_json(root / "receipts" / "provider_usage.json")
        self.assertEqual(usage["requests"][digest]["task_id"], "paid-task-123")

    def test_five_revision_budget_is_a_hard_limit(self) -> None:
        controller, _ = self.controller(SuccessfulHarness(fail_verifier=True))
        controller.create(
            self.request,
            job_id="job_revision_budget",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_revision_budget")
        for revision in range(2, 6):
            revised = case_spec_v2_fixture()
            revised["scene"]["duration_s"] += revision / 10.0
            inspection = controller.resume(
                "job_revision_budget",
                revised_case_spec=revised,
                revision_reason=f"revision {revision}",
            )
            self.assertEqual(inspection["job"]["state"], "needs_user_decision")
        over_limit = case_spec_v2_fixture()
        over_limit["scene"]["duration_s"] += 0.9

        with self.assertRaisesRegex(ValueError, "revision budget is exhausted"):
            controller.resume(
                "job_revision_budget",
                revised_case_spec=over_limit,
                revision_reason="sixth revision",
            )

    def test_case_spec_revision_creates_new_immutable_attempt(self) -> None:
        controller, fake = self.controller(SuccessfulHarness(fail_verifier=True))
        controller.create(
            self.request,
            job_id="job_revision_history",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        blocked = controller.advance_until_blocked("job_revision_history")
        self.assertEqual(blocked["job"]["state"], "needs_user_decision")
        first_path = Path(blocked["paths"]["job_root"]) / "attempts" / "attempt_001" / "case_spec.json"
        first_bytes = first_path.read_bytes()
        revised = case_spec_v2_fixture()
        revised["scene"]["duration_s"] += 0.5
        fake.fail_verifier = False

        resumed = controller.resume(
            "job_revision_history",
            revised_case_spec=revised,
            revision_reason="adjust unlocked duration",
        )

        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")
        self.assertEqual([row["attempt_id"] for row in resumed["attempts"]], ["attempt_001", "attempt_002"])
        self.assertEqual(first_path.read_bytes(), first_bytes)
        second_root = Path(resumed["paths"]["job_root"]) / "attempts" / "attempt_002"
        self.assertTrue(read_json(second_root / "case_spec_diff.json")["changes"])

    def test_observation_only_revision_records_targeted_smoke(self) -> None:
        fake = SuccessfulHarness(fail_verifier=True)
        controller, _ = self.controller(fake)
        controller.create(
            self.request,
            job_id="job_targeted_smoke",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_targeted_smoke")
        revised = case_spec_v2_fixture()
        revised["observation_requirements"]["cameras"].append(
            {"role": "side_static", "target_objects": ["cue_ball", "target_ball"]}
        )
        fake.fail_verifier = False

        inspection = controller.resume(
            "job_targeted_smoke",
            revised_case_spec=revised,
            revision_reason="add a side evidence camera",
        )

        gate = read_json(
            Path(inspection["paths"]["job_root"]) / "attempts" / "attempt_002" / "smoke_gate.json"
        )
        self.assertEqual(gate["mode"], "targeted")

    def test_matching_completed_smoke_gate_is_reused_by_fingerprint(self) -> None:
        controller, fake = self.controller()
        controller.create(
            self.request,
            job_id="job_reused_smoke",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        inspection = controller.advance_until_blocked("job_reused_smoke")
        self.assertEqual(fake.execute_calls, ["smoke", "candidate"])
        manifest = inspection["job"]
        manifest.update(
            {
                "state": "paused_interrupted",
                "current_stage": "smoke",
                "blocker": {"code": "interrupted", "message": "simulated crash after gate commit", "stage": "smoke"},
                "allowed_next_actions": ["resume", "cancel"],
            }
        )
        controller.store.write_manifest(manifest)

        resumed = controller.resume("job_reused_smoke")

        gate = read_json(
            Path(resumed["paths"]["job_root"]) / "attempts" / "attempt_001" / "smoke_gate.json"
        )
        self.assertEqual(gate["mode"], "reused")
        self.assertEqual(fake.execute_calls, ["smoke", "candidate"])

    def test_revision_cannot_remove_frozen_assertion(self) -> None:
        controller, _ = self.controller(SuccessfulHarness(fail_verifier=True))
        controller.create(
            self.request,
            job_id="job_frozen_assertion",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_frozen_assertion")
        weakened = case_spec_v2_fixture()
        weakened["verification_requirements"]["assertions"] = []

        with self.assertRaisesRegex(ValueError, "frozen verification assertions"):
            controller.resume("job_frozen_assertion", revised_case_spec=weakened, revision_reason="weaken")

    def test_cancel_is_distinct_from_interruption(self) -> None:
        controller, _ = self.controller()
        controller.create(self.request, job_id="job_cancelled_state", publication_tier="local_preview")

        inspection = controller.cancel("job_cancelled_state")

        self.assertEqual(inspection["job"]["state"], "cancelled")
        self.assertEqual(inspection["job"]["current_stage"], "cancelled")


class JobManifestSchemaTests(unittest.TestCase):
    def test_unknown_manifest_fields_fail_closed(self) -> None:
        now = "2026-08-12T00:00:00Z"
        data = {
            "schema_version": "harness_agent_job_manifest_v1",
            "job_id": "job_schema_test",
            "state": "created",
            "current_stage": "intake_readiness",
            "current_attempt_id": None,
            "active_compilation_id": None,
            "request_digest": "a" * 64,
            "intent_contract_digest": None,
            "target": {"execution_profile": "candidate", "publication_tier": "reference"},
            "authorizations": {
                "planning_llm_upload": False,
                "meshy_upload": False,
                "external_provider": False,
                "paid_provider_submission": False,
            },
            "budget": dict(DEFAULT_BUDGET),
            "usage": {
                "active_elapsed_seconds": 0.0,
                "case_spec_revisions": 0,
                "total_retries": 0,
                "stage_retries": {},
                "ue_launches": 0,
                "paid_submissions": 0,
                "generation_invocations": 0,
                "reviewer_invocations": 0,
            },
            "blocker": None,
            "allowed_next_actions": ["advance"],
            "created_at": now,
            "updated_at": now,
            "unexpected": True,
        }
        with self.assertRaisesRegex(ValueError, "fields mismatch"):
            JobManifest.from_dict(data)


class AgentJobCliTests(unittest.TestCase):
    def test_create_and_inspect_emit_structured_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            initialize_catalog(workspace / "catalog" / "assets" / "catalog.sqlite")
            request_path = root / "request.json"
            seed_path = root / "case_spec.json"
            write_json(request_path, build_case_request(case_id="cli_case", text="drop a ball", requested_backend="fallback"))
            write_json(seed_path, case_spec_v2_fixture())
            script = Path(__file__).resolve().parents[1] / "scripts" / "harness_agent_job.py"
            created = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--workspace",
                    str(workspace),
                    "create",
                    "--request",
                    str(request_path),
                    "--seed-case-spec",
                    str(seed_path),
                    "--publication-tier",
                    "local_preview",
                    "--job-id",
                    "job_cli_contract",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            payload = json.loads(created.stdout)
            self.assertEqual(payload["job"]["job_id"], "job_cli_contract")

            inspected = subprocess.run(
                [sys.executable, str(script), "--workspace", str(workspace), "inspect", "job_cli_contract"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            self.assertEqual(json.loads(inspected.stdout)["job"]["state"], "created")


if __name__ == "__main__":
    unittest.main()
