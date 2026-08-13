from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.agent.job_controller import AgentJobController, ControllerHooks
from harness.agent.evidence_bundle import current_evidence_snapshots
from harness.agent.job_schema import DEFAULT_BUDGET, JobManifest, stable_digest, utc_now
from harness.agent.review_schema import EVIDENCE_BUNDLE_SCHEMA_VERSION, REVIEWER_RECEIPT_SCHEMA_VERSION
from harness.agent.semantic_reviewer import (
    SemanticReviewerError,
    reviewer_permission_profile,
    semantic_reviewer_input_digest,
)
from harness.assets.providers.input_manifest import build_provider_input_manifest
from harness.assets.sqlite_catalog import initialize_catalog
from harness.core.artifact_schema import read_json, write_json
from harness.core.case_spec_v2 import case_spec_v2_from_dict, compile_case_spec_v2_runtime
from harness.core.stage_result import build_stage_result, failure_stage_result, write_stage_result
from harness.planning.case_generation import CaseGenerationError, build_case_request
from harness.planning.runtime_compiler import RuntimeCompilation
from tests.case_spec_v2_fixture import case_spec_v2_fixture


class SuccessfulHarness:
    def __init__(self, *, selected_backend: str = "fallback", fail_verifier: bool = False, semantic_status: str = "pass") -> None:
        self.selected_backend = selected_backend
        self.fail_verifier = fail_verifier
        self.compile_calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.execute_calls: list[str] = []
        self.provider_manifests: list[dict | None] = []
        self.generation_calls = 0
        self.semantic_status = semantic_status
        self.semantic_calls = 0

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
        self.provider_manifests.append(copy.deepcopy(kwargs.get("provider_input_manifest")))
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

    @staticmethod
    def evidence(*, job_id: str, attempt: dict, attempt_dir: Path, candidate_run_dir: Path, **kwargs):
        bundle_dir = attempt_dir / "evidence_bundle"
        summary = bundle_dir / "evidence_summary.json"
        write_json(summary, {"schema_version": "harness_evidence_summary_v1", "summary": "fixture"})

        def digest(path: Path) -> str:
            import hashlib
            return hashlib.sha256(path.read_bytes()).hexdigest()

        gates = {}
        report_paths = {
            "verifier": candidate_run_dir / "harness_verifier.json",
            "render_sync": candidate_run_dir / "render_sync_report.json",
            "quality_gate": candidate_run_dir / "quality_report.json",
        }
        for name, path in report_paths.items():
            gates[name] = {"status": "pass", "path": path.relative_to(attempt_dir).as_posix(), "sha256": digest(path)}
        trajectory = candidate_run_dir / "trajectory.json"
        point = {"label": "before", "time_s": 0.0, "frame_index": 0, "event_refs": []}
        artifacts = [
            {
                "artifact_id": "evidence_summary",
                "kind": "structured_summary",
                "path": "evidence_summary.json",
                "sha256": digest(summary),
                "mime_type": "application/json",
                "time_s": None,
                "view_id": None,
                "source_ref": None,
            }
        ]
        snapshots = current_evidence_snapshots(
            attempt_dir=attempt_dir,
            request=kwargs["request"],
            intent_contract=kwargs["intent_contract"],
            intent_amendments=kwargs.get("intent_amendments") or [],
        )
        for name, value in snapshots.items():
            path = bundle_dir / "inputs" / f"{name}.json"
            write_json(path, value)
            artifacts.append(
                {
                    "artifact_id": f"{name}_snapshot",
                    "kind": "input_snapshot",
                    "path": f"inputs/{name}.json",
                    "sha256": digest(path),
                    "mime_type": "application/json",
                    "time_s": None,
                    "view_id": None,
                    "source_ref": None,
                }
            )
        for row in kwargs["request"].get("inputs") or []:
            if row.get("kind") != "image":
                continue
            input_id = "".join(
                character if character.isalnum() else "_"
                for character in str(row.get("input_id") or "input").casefold()
            ).strip("_")[:64] or "item"
            source = Path(str(row["local_path"]))
            destination = bundle_dir / "inputs" / f"{input_id}{source.suffix.lower()}"
            destination.write_bytes(source.read_bytes())
            artifacts.append(
                {
                    "artifact_id": f"original_{input_id}",
                    "kind": "original_input_snapshot",
                    "path": destination.relative_to(bundle_dir).as_posix(),
                    "sha256": digest(destination),
                    "mime_type": str(row.get("mime_type") or "application/octet-stream"),
                    "time_s": None,
                    "view_id": None,
                    "source_ref": None,
                }
            )
        manifest = {
            "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
            "job_id": job_id,
            "attempt_id": attempt["attempt_id"],
            "case_spec_digest": attempt["case_spec_digest"],
            "intent_contract_digest": attempt["intent_contract_digest"],
            "candidate_run": {
                "path": candidate_run_dir.relative_to(attempt_dir).as_posix(),
                "fingerprint": stable_digest(read_json(attempt_dir / "candidate_run.json")),
            },
            "technical_gates": gates,
            "event_selection": {
                "strategy": "start_mid_end",
                "reason": "fixture has no event",
                "points": [point, {**point, "label": "during"}, {**point, "label": "after"}],
            },
            "trajectory_summary": {
                "source_path": trajectory.relative_to(attempt_dir).as_posix(),
                "source_sha256": digest(trajectory),
                "frame_count": 1,
                "start_time_s": 0.0,
                "end_time_s": 0.0,
                "objects": [],
            },
            "contact_timeline": [],
            "artifacts": artifacts,
            "created_at": utc_now(),
        }
        manifest_path = bundle_dir / "manifest.json"
        write_json(manifest_path, manifest)
        return {
            "manifest": manifest,
            "manifest_path": str(manifest_path),
            "stage_result": build_stage_result(stage="evidence_bundle", status="completed"),
        }

    def semantic_review(self, *, job_id: str, attempt_id: str, bundle_dir: Path, bundle_manifest: dict, invocation_count: int, **kwargs):
        self.semantic_calls += 1
        repair_layer = "none" if self.semantic_status == "pass" else "camera" if self.semantic_status == "fail" else "user_decision"
        raw = {
            "overall_status": self.semantic_status,
            "requirements": [
                {
                    "requirement_id": "original_user_request",
                    "status": self.semantic_status,
                    "rationale": "fixture semantic verdict",
                    "evidence_refs": [
                        {
                            "artifact_id": "evidence_summary",
                            "time_s": None,
                            "view_id": None,
                            "trajectory_range": "0.0-0.0s",
                            "contact_event_id": None,
                        }
                    ],
                }
            ],
            "repair_layer": repair_layer,
            "summary": "fixture semantic verdict",
            "suggested_adjustments": (
                [{"path": "$.observation_requirements", "desired_outcome": "improve view", "evidence_refs": ["evidence_summary"]}]
                if self.semantic_status == "fail"
                else []
            ),
        }
        profile = reviewer_permission_profile(
            job_id=job_id,
            attempt_id=attempt_id,
            invocation_count=invocation_count,
            bundle_dir=bundle_dir,
        )
        receipt = {
            "schema_version": REVIEWER_RECEIPT_SCHEMA_VERSION,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "invocation_count": invocation_count,
            "transport": "stdio_jsonl",
            "executable": "/usr/bin/fake-codex",
            "codex_version": "codex-cli fixture",
            "thread_id": f"thr_fixture_{invocation_count}",
            "turn_id": f"turn_fixture_{invocation_count}",
            "model": "app-server-default",
            "model_provider": "fixture",
            "requested_new_thread": True,
            "requested_permission_profile": profile,
            "requested_permission_profile_digest": stable_digest(profile),
            "active_permission_profile_id": profile["id"],
            "runtime_workspace_roots": [str(bundle_dir)],
            "ephemeral": True,
            "shell_environment_policy": {
                "inherit": "none",
                "set": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
                "use_profile": False,
            },
            "instruction_sources": [],
            "network_access": False,
            "input_digest": semantic_reviewer_input_digest(
                bundle_dir=bundle_dir,
                bundle_manifest=bundle_manifest,
                include_original_images=bool(kwargs.get("include_original_images")),
            ),
            "output_digest": stable_digest(raw),
            "status": "completed",
            "error_code": None,
            "started_at": utc_now(),
            "completed_at": utc_now(),
        }
        return {"review": raw, "receipt": receipt}

    def hooks(self) -> ControllerHooks:
        return ControllerHooks(
            generate=self.generate,
            compile=self.compile,
            execute=self.execute,
            verify=self.verify,
            render_sync=self.render_sync,
            quality=self.quality,
            evidence=self.evidence,
            semantic_review=self.semantic_review,
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

    def test_full_technical_chain_stops_before_explicit_semantic_review(self) -> None:
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

    def test_m4_explicit_semantic_review_completes_the_job(self) -> None:
        controller, fake = self.controller()
        controller.create(
            self.request,
            job_id="job_m4_semantic_pass",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        boundary = controller.advance_until_blocked("job_m4_semantic_pass")

        completed = controller.run_semantic_review("job_m4_semantic_pass")

        self.assertEqual(boundary["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(completed["job"]["state"], "completed")
        self.assertEqual(fake.semantic_calls, 1)
        attempt = Path(completed["paths"]["job_root"]) / "attempts" / "attempt_001"
        self.assertTrue((attempt / "semantic_review.json").is_file())
        receipt = read_json(attempt / "reviewer_invocation_001.json")
        self.assertTrue(receipt["requested_new_thread"])
        self.assertEqual(receipt["active_permission_profile_id"], receipt["requested_permission_profile"]["id"])
        self.assertFalse(receipt["network_access"])
        events = [read_json(path) for path in sorted((Path(completed["paths"]["job_root"]) / "events").glob("*.json"))]
        semantic_events = [row["event"] for row in events if row.get("stage") == "semantic_review"]
        self.assertIn("stage_started", semantic_events)
        self.assertIn("stage_completed", semantic_events)
        self.assertEqual(events[-1]["event"], "job_terminal")
        self.assertEqual(events[-1]["state"], "completed")

    def test_semantic_fail_allows_intent_bounded_automatic_revision(self) -> None:
        fake = SuccessfulHarness(semantic_status="fail")
        controller, _ = self.controller(fake)
        controller.create(
            self.request,
            job_id="job_m4_semantic_revision",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_semantic_revision")
        failed = controller.run_semantic_review("job_m4_semantic_revision")
        self.assertEqual(failed["job"]["allowed_next_actions"], ["apply_revision_proposal", "cancel"])
        first_root = Path(failed["paths"]["job_root"]) / "attempts" / "attempt_001"
        first_bytes = (first_root / "case_spec.json").read_bytes()
        revised = case_spec_v2_fixture()
        revised["observation_requirements"]["cameras"].append(
            {"role": "side_static", "target_objects": ["cue_ball", "target_ball"]}
        )
        fake.semantic_status = "pass"

        second = controller.apply_revision_proposal(
            "job_m4_semantic_revision",
            revised,
            reason="add Intent-authorized side evidence",
        )

        self.assertEqual(second["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(second["job"]["current_attempt_id"], "attempt_002")
        self.assertEqual((first_root / "case_spec.json").read_bytes(), first_bytes)
        proposal = read_json(first_root / "revision_proposal_001.json")
        self.assertEqual(proposal["repair_layer"], "camera")
        self.assertTrue(read_json(Path(second["paths"]["job_root"]) / "attempts" / "attempt_002" / "case_spec_diff.json")["changes"])
        self.assertEqual(controller.run_semantic_review("job_m4_semantic_revision")["job"]["state"], "completed")

    def test_camera_repair_cannot_change_even_an_otherwise_allowed_non_camera_path(self) -> None:
        controller, _ = self.controller(SuccessfulHarness(semantic_status="fail"))
        controller.create(
            self.request,
            job_id="job_m4_camera_scope",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_camera_scope")
        controller.run_semantic_review("job_m4_camera_scope")
        invalid = case_spec_v2_fixture()
        invalid["scene"]["duration_s"] += 0.5

        with self.assertRaisesRegex(ValueError, "camera repair"):
            controller.apply_revision_proposal(
                "job_m4_camera_scope",
                invalid,
                reason="do not permit physics duration repair through camera layer",
            )

    def test_tampered_semantic_review_cannot_authorize_a_revision(self) -> None:
        controller, _ = self.controller(SuccessfulHarness(semantic_status="fail"))
        controller.create(
            self.request,
            job_id="job_m4_review_tamper",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_review_tamper")
        inspection = controller.run_semantic_review("job_m4_review_tamper")
        attempt = Path(inspection["paths"]["job_root"]) / "attempts" / "attempt_001"
        review = read_json(attempt / "semantic_review.json")
        review["summary"] = "tampered after review"
        write_json(attempt / "semantic_review.json", review)
        revised = case_spec_v2_fixture()
        revised["observation_requirements"]["cameras"].append(
            {"role": "side_static", "target_objects": ["cue_ball", "target_ball"]}
        )

        with self.assertRaisesRegex(ValueError, "output digest"):
            controller.apply_revision_proposal(
                "job_m4_review_tamper",
                revised,
                reason="must reject tampered review",
            )

    def test_semantic_uncertain_never_completes(self) -> None:
        controller, fake = self.controller(SuccessfulHarness(semantic_status="uncertain"))
        controller.create(
            self.request,
            job_id="job_m4_semantic_uncertain",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_semantic_uncertain")

        inspection = controller.run_semantic_review("job_m4_semantic_uncertain")

        self.assertEqual(inspection["job"]["state"], "needs_user_decision")
        self.assertEqual(inspection["job"]["blocker"]["code"], "semantic_review_uncertain")
        self.assertNotEqual(inspection["job"]["state"], "completed")
        self.assertEqual(fake.semantic_calls, 1)

        with self.assertRaisesRegex(ValueError, "does not permit resume"):
            controller.resume("job_m4_semantic_uncertain")
        with self.assertRaisesRegex(ValueError, "explicit review boundary"):
            controller.run_semantic_review("job_m4_semantic_uncertain")
        self.assertEqual(fake.semantic_calls, 1)

    def test_reviewer_receipt_and_body_are_bound_to_the_current_invocation(self) -> None:
        mutations = (
            ("job", lambda result: result["receipt"].__setitem__("job_id", "job_wrong_identity")),
            ("attempt", lambda result: result["receipt"].__setitem__("attempt_id", "attempt_999")),
            ("invocation", lambda result: result["receipt"].__setitem__("invocation_count", 99)),
            ("input_digest", lambda result: result["receipt"].__setitem__("input_digest", "a" * 64)),
            ("output_digest", lambda result: result["receipt"].__setitem__("output_digest", "b" * 64)),
            ("active_profile", lambda result: result["receipt"].__setitem__("active_permission_profile_id", "harness_reviewer_0000000000000000")),
            ("outside_instruction", lambda result: result["receipt"].__setitem__("instruction_sources", [str(Path(__file__).resolve())])),
            ("review_envelope", lambda result: result["review"].__setitem__("job_id", "job_wrong_identity")),
        )
        for index, (label, mutate) in enumerate(mutations, start=1):
            with self.subTest(field=label):
                fake = SuccessfulHarness()
                hooks = fake.hooks()
                valid_review = hooks.semantic_review

                def adversarial(**kwargs):
                    result = valid_review(**kwargs)
                    mutate(result)
                    return result

                hooks.semantic_review = adversarial
                controller = AgentJobController(self.workspace, hooks=hooks)
                job_id = f"job_review_binding_{index}"
                controller.create(
                    self.request,
                    job_id=job_id,
                    publication_tier="local_preview",
                    seed_case_spec=case_spec_v2_fixture(),
                )
                boundary = controller.advance_until_blocked(job_id)
                failed = controller.run_semantic_review(job_id)
                attempt_dir = Path(boundary["paths"]["job_root"]) / "attempts" / "attempt_001"
                self.assertEqual(failed["job"]["state"], "failed")
                self.assertFalse((attempt_dir / "semantic_review.json").exists())
                self.assertEqual(fake.semantic_calls, 1)

    def test_foreign_bundle_is_rejected_before_reviewer_invocation(self) -> None:
        controller, fake = self.controller()
        controller.create(
            self.request,
            job_id="job_foreign_bundle_controller",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        boundary = controller.advance_until_blocked("job_foreign_bundle_controller")
        attempt_dir = Path(boundary["paths"]["job_root"]) / "attempts" / "attempt_001"
        manifest_path = attempt_dir / "evidence_bundle" / "manifest.json"
        bundle = read_json(manifest_path)
        bundle["attempt_id"] = "attempt_999"
        write_json(manifest_path, bundle)

        failed = controller.run_semantic_review("job_foreign_bundle_controller")

        self.assertEqual(failed["job"]["state"], "failed")
        self.assertEqual(fake.semantic_calls, 0)
        self.assertFalse((attempt_dir / "semantic_review.json").exists())

    def test_review_time_manifest_or_artifact_replacement_leaves_no_formal_review(self) -> None:
        for index, target in enumerate(("manifest", "artifact"), start=1):
            with self.subTest(target=target):
                fake = SuccessfulHarness()
                hooks = fake.hooks()
                valid_review = hooks.semantic_review

                def tampering_review(**kwargs):
                    result = valid_review(**kwargs)
                    bundle_dir = Path(kwargs["bundle_dir"])
                    manifest_path = bundle_dir / "manifest.json"
                    if target == "manifest":
                        manifest = read_json(manifest_path)
                        manifest["created_at"] = "2026-08-13T00:00:00Z"
                        write_json(manifest_path, manifest)
                    else:
                        manifest = read_json(manifest_path)
                        artifact = next(row for row in manifest["artifacts"] if row["artifact_id"] == "evidence_summary")
                        (bundle_dir / artifact["path"]).write_bytes(b"replaced during review")
                    return result

                hooks.semantic_review = tampering_review
                controller = AgentJobController(self.workspace, hooks=hooks)
                job_id = f"job_review_time_tamper_{index}"
                controller.create(
                    self.request,
                    job_id=job_id,
                    publication_tier="local_preview",
                    seed_case_spec=case_spec_v2_fixture(),
                )
                boundary = controller.advance_until_blocked(job_id)
                failed = controller.run_semantic_review(job_id)
                attempt_dir = Path(boundary["paths"]["job_root"]) / "attempts" / "attempt_001"
                self.assertEqual(failed["job"]["state"], "failed")
                self.assertFalse((attempt_dir / "semantic_review.json").exists())
                self.assertEqual(fake.semantic_calls, 1)

    def test_semantic_reviewer_image_authorization_is_independent_and_precedes_turn(self) -> None:
        image = self.workspace / "semantic-review.png"
        image.write_bytes(b"semantic-review-image")
        request = build_case_request(
            case_id="semantic_image_case",
            text="match this image",
            image_paths=[image],
            allow_image_upload=False,
            requested_backend="fallback",
        )
        controller, fake = self.controller()
        controller.create(
            request,
            job_id="job_m4_semantic_image_auth",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
            authorizations={"planning_llm_upload": False, "meshy_upload": True},
        )
        controller.advance_until_blocked("job_m4_semantic_image_auth")

        blocked = controller.run_semantic_review("job_m4_semantic_image_auth")

        self.assertEqual(blocked["job"]["state"], "blocked")
        self.assertEqual(blocked["job"]["blocker"]["code"], "semantic_reviewer_image_upload_authorization_missing")
        self.assertEqual(fake.semantic_calls, 0)
        boundary = controller.resume(
            "job_m4_semantic_image_auth",
            authorizations={"semantic_reviewer_image_upload": True},
        )
        self.assertEqual(boundary["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(controller.run_semantic_review("job_m4_semantic_image_auth")["job"]["state"], "completed")

    def test_semantic_pass_cannot_override_changed_technical_evidence(self) -> None:
        controller, fake = self.controller()
        controller.create(
            self.request,
            job_id="job_m4_stale_gate",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        boundary = controller.advance_until_blocked("job_m4_stale_gate")
        attempt = Path(boundary["paths"]["job_root"]) / "attempts" / "attempt_001"
        candidate = Path(read_json(attempt / "candidate_run.json")["run_dir"])
        write_json(candidate / "quality_report.json", {"schema_version": "harness_run_quality_v1", "status": "fail", "hard_gate_passed": False})

        inspection = controller.run_semantic_review("job_m4_stale_gate")

        self.assertEqual(inspection["job"]["state"], "failed")
        self.assertEqual(inspection["job"]["blocker"]["code"], "semantic_review_technical_gate_stale")
        self.assertEqual(fake.semantic_calls, 0)

    def test_reviewer_technical_failure_retries_once_without_new_attempt(self) -> None:
        controller, fake = self.controller()
        hooks = fake.hooks()
        calls = 0
        successful = hooks.semantic_review

        def flaky(**kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                return successful(**kwargs)
            bundle_dir = Path(kwargs["bundle_dir"])
            profile = reviewer_permission_profile(
                job_id=kwargs["job_id"],
                attempt_id=kwargs["attempt_id"],
                invocation_count=kwargs["invocation_count"],
                bundle_dir=bundle_dir,
            )
            now = utc_now()
            receipt = {
                "schema_version": REVIEWER_RECEIPT_SCHEMA_VERSION,
                "job_id": kwargs["job_id"],
                "attempt_id": kwargs["attempt_id"],
                "invocation_count": kwargs["invocation_count"],
                "transport": "stdio_jsonl",
                "executable": "/usr/bin/fake-codex",
                "codex_version": "fixture",
                "thread_id": None,
                "turn_id": None,
                "model": None,
                "model_provider": None,
                "requested_new_thread": True,
                "requested_permission_profile": profile,
                "requested_permission_profile_digest": stable_digest(profile),
                "active_permission_profile_id": None,
                "runtime_workspace_roots": [str(bundle_dir)],
                "ephemeral": True,
                "shell_environment_policy": {
                    "inherit": "none",
                    "set": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
                    "use_profile": False,
                },
                "instruction_sources": [],
                "network_access": False,
                "input_digest": semantic_reviewer_input_digest(
                    bundle_dir=bundle_dir,
                    bundle_manifest=kwargs["bundle_manifest"],
                    include_original_images=bool(kwargs.get("include_original_images")),
                ),
                "output_digest": None,
                "status": "failed",
                "error_code": "reviewer_app_server_failure",
                "started_at": now,
                "completed_at": now,
            }
            raise SemanticReviewerError("reviewer_app_server_failure", "temporary failure", retryable=True, receipt=receipt)

        hooks.semantic_review = flaky
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_m4_reviewer_retry",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_reviewer_retry")

        inspection = controller.run_semantic_review("job_m4_reviewer_retry")

        self.assertEqual(inspection["job"]["state"], "completed")
        self.assertEqual(inspection["job"]["usage"]["reviewer_invocations"], 2)
        self.assertEqual(inspection["job"]["usage"]["stage_retries"]["semantic_review"], 1)
        self.assertEqual([row["attempt_id"] for row in inspection["attempts"]], ["attempt_001"])

    def test_zero_reviewer_invocation_budget_never_calls_reviewer(self) -> None:
        controller, fake = self.controller()
        controller.create(
            self.request,
            job_id="job_m4_zero_reviewer_budget",
            budget={"max_reviewer_invocations": 0},
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_zero_reviewer_budget")

        inspection = controller.run_semantic_review("job_m4_zero_reviewer_budget")

        self.assertEqual(inspection["job"]["blocker"]["code"], "reviewer_invocation_budget_exhausted")
        self.assertEqual(fake.semantic_calls, 0)

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

    def test_generation_resume_after_intent_commit_reuses_immutable_contract(self) -> None:
        controller, fake = self.controller()
        controller.create(self.request, job_id="job_intent_commit_crash", publication_tier="local_preview")
        original_create_attempt = controller.store.create_attempt
        calls = 0

        def interrupt_after_intent(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt()
            return original_create_attempt(*args, **kwargs)

        controller.store.create_attempt = interrupt_after_intent
        paused = controller.advance_until_blocked("job_intent_commit_crash")
        intent_path = Path(paused["paths"]["intent_contract"])
        intent_bytes = intent_path.read_bytes()
        self.assertEqual(paused["job"]["state"], "paused_interrupted")

        resumed = controller.resume("job_intent_commit_crash")

        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(intent_path.read_bytes(), intent_bytes)
        self.assertEqual(resumed["job"]["blocker"], None)

    def test_compile_exception_consumes_changed_provider_stage_result(self) -> None:
        fake = SuccessfulHarness()

        def provider_failure(case_spec, **kwargs):
            del case_spec
            write_stage_result(
                kwargs["stage_result_dir"],
                failure_stage_result(
                    stage="provider",
                    failure_code="provider_credentials_missing",
                    message="credential is unavailable",
                    source_status="blocked",
                ),
            )
            raise RuntimeError("provider adapter stopped")

        hooks = fake.hooks()
        hooks.compile = provider_failure
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_provider_stage_truth",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )

        inspection = controller.advance_until_blocked("job_provider_stage_truth")

        self.assertEqual(inspection["job"]["state"], "blocked")
        self.assertEqual(inspection["job"]["blocker"]["stage"], "provider")
        self.assertEqual(inspection["job"]["blocker"]["code"], "provider_credentials_missing")
        self.assertEqual(inspection["job"]["usage"]["stage_retries"], {})

    def test_runtime_exception_consumes_leaf_preflight_stage_result(self) -> None:
        fake = SuccessfulHarness()

        def preflight_failure(case, output_root, *, compilation, **kwargs):
            del kwargs
            run_dir = Path(output_root) / f"{case.case_id}_{compilation.selected_backend}"
            write_stage_result(
                run_dir,
                failure_stage_result(
                    stage="preflight",
                    failure_code="ue_project_missing",
                    message="project is unavailable",
                    source_status="blocked",
                ),
            )
            write_stage_result(
                run_dir,
                failure_stage_result(
                    stage="execute",
                    failure_code="stage_execution_exception",
                    message="preflight wrapper failed",
                ),
            )
            raise RuntimeError("execution stopped")

        hooks = fake.hooks()
        hooks.execute = preflight_failure
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_preflight_stage_truth",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )

        inspection = controller.advance_until_blocked("job_preflight_stage_truth")

        self.assertEqual(inspection["job"]["state"], "blocked")
        self.assertEqual(inspection["job"]["blocker"]["stage"], "preflight")
        self.assertEqual(inspection["job"]["blocker"]["code"], "ue_project_missing")

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
                write_stage_result(
                    run_dir,
                    failure_stage_result(
                        stage="verifier",
                        failure_code="interrupted",
                        message="verifier interrupted",
                        source_status="interrupted",
                    ),
                )
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
        self.assertEqual(paused["job"]["blocker"]["stage"], "verifier")
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

    def test_observation_only_revision_records_executed_smoke_without_replay_evidence(self) -> None:
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
        self.assertEqual(gate["mode"], "executed")

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

    def test_incomplete_smoke_evidence_is_not_reused(self) -> None:
        controller, fake = self.controller()
        controller.create(
            self.request,
            job_id="job_incomplete_smoke",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        inspection = controller.advance_until_blocked("job_incomplete_smoke")
        root = Path(inspection["paths"]["job_root"])
        gate_path = root / "attempts" / "attempt_001" / "smoke_gate.json"
        smoke_run = Path(read_json(gate_path)["run_dir"])
        (smoke_run / "render_sync_report.json").unlink()
        manifest = inspection["job"]
        manifest.update(
            {
                "state": "paused_interrupted",
                "current_stage": "smoke",
                "blocker": {"code": "interrupted", "message": "resume smoke", "stage": "smoke"},
                "allowed_next_actions": ["resume", "cancel"],
            }
        )
        controller.store.write_manifest(manifest)

        resumed = controller.resume("job_incomplete_smoke")

        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(read_json(gate_path)["mode"], "executed")
        self.assertTrue((smoke_run / "render_sync_report.json").is_file())

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

    def test_revision_cannot_change_frozen_asset_policy_or_unlisted_path(self) -> None:
        controller, _ = self.controller(SuccessfulHarness(fail_verifier=True))
        controller.create(
            self.request,
            job_id="job_frozen_policy",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_frozen_policy")
        downgraded = case_spec_v2_fixture()
        downgraded["asset_policy"]["required_license_tier"] = "reference"
        with self.assertRaisesRegex(ValueError, "frozen asset policy"):
            controller.resume("job_frozen_policy", revised_case_spec=downgraded, revision_reason="change tier")

        unlisted = case_spec_v2_fixture()
        unlisted["objects"][0]["physics"]["mass_kg"] = 0.2
        with self.assertRaisesRegex(ValueError, "allowed_adjustments"):
            controller.resume("job_frozen_policy", revised_case_spec=unlisted, revision_reason="change mass")

    def test_ambiguity_amendment_requires_exact_identity_and_decision(self) -> None:
        fake = SuccessfulHarness()

        def ambiguous(request: dict, *, artifact_dir: Path, job_id: str, attempt_id: str):
            generated = fake.generate(request, artifact_dir=artifact_dir, job_id=job_id, attempt_id=attempt_id)
            generated.expansion["ambiguities"] = [{"question": "which object should move?"}]
            write_json(artifact_dir / "expansion.json", generated.expansion)
            return generated

        hooks = fake.hooks()
        hooks.generate = ambiguous
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(self.request, job_id="job_ambiguity_identity", publication_tier="local_preview")
        blocked = controller.advance_until_blocked("job_ambiguity_identity")
        intent = read_json(blocked["paths"]["intent_contract"])
        ambiguity_id = intent["ambiguities"][0]["ambiguity_id"]

        with self.assertRaisesRegex(ValueError, "ambiguity_id"):
            controller.resume("job_ambiguity_identity", intent_amendment={"ambiguity_resolutions": [{}]})
        with self.assertRaisesRegex(ValueError, "recorded ambiguities"):
            controller.resume(
                "job_ambiguity_identity",
                intent_amendment={"ambiguity_resolutions": [{"ambiguity_id": "wrong", "decision": "cue_ball"}]},
            )
        resumed = controller.resume(
            "job_ambiguity_identity",
            intent_amendment={"ambiguity_resolutions": [{"ambiguity_id": ambiguity_id, "decision": "cue_ball"}]},
        )
        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")

    def test_authorization_resume_writes_amendment_and_effective_provider_manifest(self) -> None:
        image = self.workspace / "authorization.png"
        image.write_bytes(b"image")
        request = build_case_request(
            case_id="authorization_case",
            text="generate a ball from this image",
            image_paths=[str(image)],
            allow_image_upload=False,
            requested_backend="fallback",
        )
        input_id = request["inputs"][0]["input_id"]
        generated = case_spec_v2_fixture()
        generated["objects"][0]["asset"] = {
            "description": "generated ball",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "model_generation",
                "requirement": "required",
                "origin": "user_explicit",
                "provider_hint": "meshy",
                "reference_inputs": [{"input_id": input_id, "usage": ["generation_condition"]}],
                "fallback_order": [],
            },
        }
        controller, fake = self.controller()
        controller.create(
            request,
            provider_input_manifest=build_provider_input_manifest(
                request["inputs"],
                workspace=self.workspace,
                meshy_upload_authorized=False,
            ),
            job_id="job_authorization_amendment",
            publication_tier="local_preview",
            seed_case_spec=generated,
        )
        blocked = controller.advance_until_blocked("job_authorization_amendment")
        self.assertEqual(blocked["job"]["blocker"]["code"], "external_provider_authorization_missing")

        with patch.dict("os.environ", {"SIM_HARNESS_MESHY_API_KEY": "test-key"}, clear=False):
            resumed = controller.resume(
                "job_authorization_amendment",
                authorizations={
                    "meshy_upload": True,
                    "external_provider": True,
                    "paid_provider_submission": True,
                },
                max_paid_submissions=1,
            )

        root = Path(resumed["paths"]["job_root"])
        amendment = read_json(root / "request" / "authorization_amendment_001.json")
        effective = read_json(root / "request" / "provider_input_manifest_effective_001.json")
        self.assertEqual(amendment["budget_changes"]["max_paid_submissions"]["after"], 1)
        self.assertEqual(effective["schema_version"], "harness_provider_input_manifest_v1")
        self.assertTrue(effective["inputs"][0]["authorizations"]["meshy_upload"])
        self.assertEqual(resumed["job"]["budget"]["max_paid_submissions"], 1)
        self.assertEqual(fake.provider_manifests[-1], effective)

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
    def test_resume_cli_exposes_paid_submission_limit(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "harness_agent_job.py"
        completed = subprocess.run(
            [sys.executable, str(script), "resume", "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--max-paid-submissions", completed.stdout)
        self.assertIn("--allow-semantic-reviewer-image-upload", completed.stdout)

    def test_m4_cli_exposes_explicit_review_and_revision_actions(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "harness_agent_job.py"
        completed = subprocess.run(
            [sys.executable, str(script), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("review", completed.stdout)
        self.assertIn("apply-revision", completed.stdout)

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
