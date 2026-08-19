from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.agent.job_controller import AgentJobController, ControllerHooks
from harness.agent.job_store import JobStoreError
from harness.agent.evidence_bundle import current_evidence_snapshots, semantic_review_requirements
from harness.agent.job_schema import DEFAULT_BUDGET, IntentContract, JobManifest, stable_digest, utc_now
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
from harness.core.harness_config import load_harness_config
from harness.core.stage_result import build_stage_result, failure_stage_result, write_stage_result
from harness.planning.case_generation import CaseGenerationError, build_case_request
from harness.planning.runtime_compiler import RuntimeCompilation
from tests.case_spec_v2_fixture import case_spec_v2_fixture as base_case_spec_v2_fixture


def case_spec_v2_fixture() -> dict:
    value = base_case_spec_v2_fixture()
    value["provenance"]["intent_parameter_analysis"] = [
        {
            "path": "$.scene.duration_s",
            "requirement_level": "inferred",
            "reason": "fixture duration is an inferred execution window",
            "constraint": {"kind": "numeric", "min": 1.0, "max": 3.0},
        },
        {
            "path": "$.observation_requirements.cameras",
            "requirement_level": "inferred",
            "reason": "fixture camera coverage is planner-selected",
            "constraint": {"kind": "list", "min_items": 1, "max_items": 5},
        },
        {
            "path": "$.observation_requirements.modalities",
            "requirement_level": "inferred",
            "reason": "fixture modalities are planner-selected",
            "constraint": {"kind": "list", "min_items": 1, "max_items": 3},
        },
    ]
    return value


def failed_reviewer_receipt(kwargs: dict, code: str, *, status: str = "failed") -> dict:
    bundle_dir = Path(kwargs["bundle_dir"])
    profile = reviewer_permission_profile(
        job_id=kwargs["job_id"],
        attempt_id=kwargs["attempt_id"],
        invocation_count=kwargs["invocation_count"],
        bundle_dir=bundle_dir,
    )
    now = utc_now()
    return {
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
        "status": status,
        "error_code": code,
        "started_at": now,
        "completed_at": now,
    }


class SuccessfulHarness:
    def __init__(self, *, selected_backend: str = "fallback", fail_verifier: bool = False, semantic_status: str = "pass") -> None:
        self.selected_backend = selected_backend
        self.fail_verifier = fail_verifier
        self.compile_calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.execute_calls: list[str] = []
        self.provider_manifests: list[dict | None] = []
        self.generation_calls = 0
        self.generation_requests: list[dict] = []
        self.semantic_status = semantic_status
        self.semantic_calls = 0

    def generate(self, request: dict, *, artifact_dir: Path, job_id: str, attempt_id: str):
        self.generation_calls += 1
        self.generation_requests.append(copy.deepcopy(request))
        case = case_spec_v2_from_dict(case_spec_v2_fixture())
        expansion = {
            "schema_version": "harness_expansion_v1",
            "ambiguities": [],
            "assumptions": [],
            "parameter_analysis": [
                {
                    "path": "$.scene.duration_s",
                    "requirement_level": "inferred",
                    "reason": "fixture duration is an inferred execution window",
                    "constraint": {"kind": "numeric", "min": 1.0, "max": 3.0},
                },
                {
                    "path": "$.observation_requirements.cameras",
                    "requirement_level": "inferred",
                    "reason": "fixture camera coverage is planner-selected",
                    "constraint": {"kind": "list", "min_items": 1, "max_items": 5},
                },
                {
                    "path": "$.observation_requirements.modalities",
                    "requirement_level": "inferred",
                    "reason": "fixture modalities are planner-selected",
                    "constraint": {"kind": "list", "min_items": 1, "max_items": 3},
                },
            ],
        }
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
            "source_reports": {"run_readiness": {"publication_tier": "reference"}},
        }
        write_json(run_dir / "quality_report.json", report)
        write_stage_result(run_dir, build_stage_result(stage="quality_gate", status="completed"))
        return report

    @staticmethod
    def evidence(*, job_id: str, attempt: dict, attempt_dir: Path, candidate_run_dir: Path, **kwargs):
        bundle_dir = attempt_dir / "evidence_bundle"
        summary = bundle_dir / "evidence_summary.json"
        write_json(
            summary,
            {
                "schema_version": "harness_evidence_summary_v2",
                "summary": "fixture",
                "semantic_requirements": semantic_review_requirements(
                    kwargs["intent_contract"],
                    kwargs.get("intent_amendments") or [],
                ),
                "result_provenance": kwargs["result_provenance"],
            },
        )

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
                "sampling": {
                    "strategy": "uniform_event_state_v1",
                    "max_sample_frames": 24,
                    "selected_frame_count": 1,
                    "omitted_frame_count": 0,
                    "state_transition_count": 0,
                    "state_transitions_included": 0,
                },
                "sampled_frames": [{"frame_index": 0, "time_s": 0.0, "reasons": ["fixture"], "objects": []}],
                "readable_ranges": [{
                    "range_id": "trajectory_full",
                    "start_frame_index": 0,
                    "end_frame_index": 0,
                    "start_time_s": 0.0,
                    "end_time_s": 0.0,
                    "sample_frame_indices": [0],
                    "event_refs": [],
                }],
                "state_transitions": [],
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
        lifecycle_callback = kwargs.get("lifecycle_callback")
        if lifecycle_callback is not None:
            lifecycle_callback("started")
        repair_layer = "none" if self.semantic_status == "pass" else "camera" if self.semantic_status == "fail" else "evidence"
        semantic_requirements = read_json(bundle_dir / "evidence_summary.json")["semantic_requirements"]
        raw = {
            "overall_status": self.semantic_status,
            "requirements": [
                {
                    "requirement_id": str(requirement["id"]),
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
                for requirement in semantic_requirements
            ],
            "repair_layer": repair_layer,
            "summary": "fixture semantic verdict",
            "suggested_adjustments": (
                [{"path": "$.observation_requirements.cameras", "desired_outcome": "improve view", "evidence_refs": ["evidence_summary"]}]
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

    def _image_request(
        self,
        *,
        text: str | None,
        allow_upload: bool = False,
        required: bool = False,
    ) -> dict:
        image = self.workspace / f"planning-{len(list(self.workspace.glob('planning-*.png')))}.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nplanning-fixture")
        return build_case_request(
            case_id="planning_image_case",
            text=text,
            image_paths=[image],
            allow_image_upload=allow_upload,
            planning_images_required=required,
            requested_backend="fallback",
        )

    def test_image_only_without_planning_authorization_blocks_at_l0_without_generation(self) -> None:
        controller, fake = self.controller()
        request = self._image_request(text=None)
        controller.create(
            request,
            job_id="job_planning_image_l0",
            publication_tier="local_preview",
        )

        blocked = controller.advance_until_blocked("job_planning_image_l0")

        self.assertEqual(request["planning_image_requirement"]["mode"], "required")
        self.assertEqual(blocked["job"]["current_stage"], "intake_readiness")
        self.assertEqual(blocked["job"]["blocker"]["code"], "planning_image_upload_authorization_missing")
        self.assertEqual(blocked["job"]["usage"]["generation_invocations"], 0)
        self.assertEqual(fake.generation_calls, 0)

    def test_image_only_with_planning_authorization_uploads_pixels_to_expansion_input(self) -> None:
        controller, fake = self.controller()
        request = self._image_request(text=None, allow_upload=True)
        controller.create(
            request,
            job_id="job_planning_image_authorized",
            publication_tier="local_preview",
            authorizations={"planning_llm_upload": True},
            generation_mode="legacy",
        )

        inspection = controller.advance_until_blocked("job_planning_image_authorized")

        self.assertEqual(inspection["job"]["state"], "awaiting_semantic_review")
        sent = fake.generation_requests[0]["inputs"][0]
        self.assertTrue(sent["external_upload_authorized"])

    def test_configured_unsupported_required_image_blocks_with_zero_generation_calls(self) -> None:
        fake = SuccessfulHarness()
        config = replace(
            load_harness_config(cli_overrides={"paths.workspace": str(self.workspace)}),
            planning_image_capability="unsupported",
        )
        controller = AgentJobController(config=config, hooks=fake.hooks())
        request = self._image_request(text=None, allow_upload=True)
        controller.create(
            request,
            job_id="job_planning_image_unsupported",
            publication_tier="local_preview",
            authorizations={"planning_llm_upload": True},
            generation_mode="legacy",
        )

        blocked = controller.advance_until_blocked("job_planning_image_unsupported")

        self.assertEqual(blocked["job"]["state"], "blocked")
        self.assertEqual(blocked["job"]["blocker"]["code"], "planning_image_input_unsupported")
        stage_result = read_json(
            Path(blocked["paths"]["job_root"]) / "stage_results" / "generation.json"
        )
        self.assertEqual(stage_result["failure_class"], "blocked_configuration")
        self.assertEqual(blocked["job"]["usage"]["generation_invocations"], 0)
        self.assertEqual(fake.generation_calls, 0)

    def test_optional_planning_image_without_authorization_uses_metadata_only_generation(self) -> None:
        controller, fake = self.controller()
        request = self._image_request(text="drop the pictured object", allow_upload=False)
        controller.create(
            request,
            job_id="job_planning_image_optional",
            publication_tier="local_preview",
            generation_mode="legacy",
        )

        inspection = controller.advance_until_blocked("job_planning_image_optional")

        self.assertEqual(inspection["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(request["planning_image_requirement"]["mode"], "optional")
        self.assertFalse(fake.generation_requests[0]["inputs"][0]["external_upload_authorized"])
        intent = read_json(Path(inspection["paths"]["intent_contract"]))
        self.assertEqual(intent["planning_image_requirement"], request["planning_image_requirement"])
        self.assertFalse(intent["authorizations"]["planning_llm_upload"])
        missing_requirement = copy.deepcopy(intent)
        missing_requirement.pop("planning_image_requirement")
        with self.assertRaisesRegex(ValueError, "fields mismatch"):
            IntentContract.from_dict(missing_requirement)

    def test_required_text_image_blocks_before_generation_then_amendment_uploads_it(self) -> None:
        controller, fake = self.controller()
        request = self._image_request(
            text="use the pictured geometry as the simulated object",
            required=True,
        )
        controller.create(
            request,
            job_id="job_planning_image_resume",
            publication_tier="local_preview",
            authorizations={
                "planning_llm_upload": False,
                "meshy_upload": True,
                "semantic_reviewer_image_upload": True,
            },
            generation_mode="legacy",
        )
        blocked = controller.advance_until_blocked("job_planning_image_resume")
        self.assertEqual(blocked["job"]["current_stage"], "generation")
        self.assertEqual(blocked["job"]["blocker"]["code"], "planning_image_upload_authorization_missing")
        self.assertEqual(fake.generation_calls, 0)
        request_root = Path(blocked["paths"]["job_root"]) / "request"
        metadata_cache = request_root / "generation"
        write_json(metadata_cache / "request.json", request)

        resumed = controller.resume(
            "job_planning_image_resume",
            authorizations={"planning_llm_upload": True},
        )

        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(fake.generation_calls, 1)
        self.assertTrue(fake.generation_requests[0]["inputs"][0]["external_upload_authorized"])
        root = Path(resumed["paths"]["job_root"])
        self.assertTrue((root / "request" / "generation_metadata_only_001" / "request.json").is_file())
        amendment = read_json(root / "request" / "authorization_amendment_001.json")
        self.assertEqual(
            amendment["authorization_changes"]["planning_llm_upload"],
            {"before": False, "after": True},
        )

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
        self.assertEqual(fake.execute_calls, ["smoke", "local_preview"])
        self.assertEqual(inspection["job"]["target"]["execution_profile"], "local_preview")
        self.assertEqual(len(inspection["attempts"]), 1)
        attempt_root = Path(inspection["paths"]["job_root"]) / "attempts" / "attempt_001"
        gate = read_json(attempt_root / "smoke_gate.json")
        self.assertEqual(gate["mode"], "executed")
        self.assertEqual(gate["status"], "pass")
        self.assertNotEqual(fake.compile_calls[0], fake.compile_calls[-1])
        self.assertEqual(
            fake.compile_calls[-1],
            (("front_static", "event_closeup"), ("rgb",)),
        )
        candidate = read_json(attempt_root / "candidate_run.json")
        self.assertTrue((Path(candidate["run_dir"]) / "quality_report.json").is_file())
        leaf = inspection["current_leaf_stage_result"]
        self.assertEqual(leaf["result"]["stage"], "evidence_bundle")
        self.assertEqual(leaf["result"]["status"], "completed")
        self.assertEqual(Path(leaf["path"]), attempt_root / "stage_results" / "evidence_bundle.json")
        events = [read_json(path) for path in sorted((Path(inspection["paths"]["job_root"]) / "events").glob("*.json"))]
        self.assertNotIn("job_terminal", [row["event"] for row in events])

    def test_inspect_returns_the_current_nested_candidate_leaf_stage_result(self) -> None:
        fake = SuccessfulHarness()
        verify_calls = 0

        def fail_candidate(run_dir: Path):
            nonlocal verify_calls
            verify_calls += 1
            fake.fail_verifier = verify_calls == 2
            return fake.verify(run_dir)

        hooks = fake.hooks()
        hooks.verify = fail_candidate
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_candidate_leaf_result",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )

        blocked = controller.advance_until_blocked("job_candidate_leaf_result")

        self.assertEqual(blocked["job"]["state"], "needs_user_decision")
        self.assertEqual(blocked["job"]["blocker"]["stage"], "verifier")
        leaf = blocked["current_leaf_stage_result"]
        self.assertEqual(leaf["result"]["failure_code"], "declared_assertion_failed")
        self.assertEqual(leaf["result"]["attempt_id"], "attempt_001")
        self.assertIn("/runs/candidate/", leaf["path"])

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
        self.assertEqual(completed["current_leaf_stage_result"]["result"]["stage"], "semantic_review")
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

    def test_semantic_pass_publication_failure_is_the_authoritative_leaf(self) -> None:
        controller, fake = self.controller()
        controller.create(
            self.request,
            job_id="job_m4_publication_leaf",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        boundary = controller.advance_until_blocked("job_m4_publication_leaf")
        manifest = boundary["job"]
        manifest["target"]["publication_tier"] = "reference"
        controller.store.write_manifest(manifest)

        blocked = controller.run_semantic_review("job_m4_publication_leaf")

        self.assertEqual(blocked["job"]["state"], "needs_user_decision")
        self.assertEqual(blocked["job"]["blocker"]["code"], "publication_tier_not_satisfied")
        self.assertEqual(fake.semantic_calls, 1)
        leaf = blocked["current_leaf_stage_result"]
        self.assertEqual(leaf["result"]["status"], "blocked")
        self.assertEqual(leaf["result"]["failure_code"], "publication_tier_not_satisfied")
        attempt = Path(blocked["paths"]["job_root"]) / "attempts" / "attempt_001"
        self.assertEqual(Path(leaf["path"]), attempt / "stage_results" / "semantic_review.json")
        events = [read_json(path) for path in sorted((Path(blocked["paths"]["job_root"]) / "events").glob("*.json"))]
        semantic_events = [row for row in events if row.get("stage") == "semantic_review"]
        self.assertEqual(semantic_events[-1]["event"], "stage_blocked")
        self.assertEqual(semantic_events[-1]["result"]["failure_code"], "publication_tier_not_satisfied")
        self.assertNotIn("job_terminal", [row["event"] for row in events])

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

    def test_semantic_revision_cannot_change_an_allowed_but_unsuggested_path(self) -> None:
        controller, _ = self.controller(SuccessfulHarness(semantic_status="fail"))
        controller.create(
            self.request,
            job_id="job_m4_suggestion_scope",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_suggestion_scope")
        controller.run_semantic_review("job_m4_suggestion_scope")
        revised = case_spec_v2_fixture()
        revised["observation_requirements"]["modalities"].append("depth")

        with self.assertRaisesRegex(ValueError, "not suggested by the current Semantic Review"):
            controller.apply_revision_proposal(
                "job_m4_suggestion_scope",
                revised,
                reason="modalities are Intent-authorized but were not suggested",
            )

    def test_semantic_revision_rejects_suggested_path_outside_intent_range(self) -> None:
        controller, _ = self.controller(SuccessfulHarness(semantic_status="fail"))
        controller.create(
            self.request,
            job_id="job_m4_suggestion_range",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_suggestion_range")
        controller.run_semantic_review("job_m4_suggestion_range")
        revised = case_spec_v2_fixture()
        revised["observation_requirements"]["cameras"] = [
            {"role": "front_static", "target_objects": ["cue_ball", "target_ball"]}
            for _ in range(6)
        ]

        with self.assertRaisesRegex(ValueError, "exceeds Intent Contract list range"):
            controller.apply_revision_proposal(
                "job_m4_suggestion_range",
                revised,
                reason="suggested path still must respect its Intent range",
            )

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

    def test_evidence_provenance_rebuild_reuses_candidate_and_preserves_usage(self) -> None:
        controller, fake = self.controller(SuccessfulHarness(semantic_status="uncertain"))
        controller.create(
            self.request,
            job_id="job_m4_evidence_rebuild",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_evidence_rebuild")
        uncertain = controller.run_semantic_review("job_m4_evidence_rebuild")
        usage_before = copy.deepcopy(uncertain["job"]["usage"])

        rebuilt = controller.rebuild_evidence_after_provenance(
            "job_m4_evidence_rebuild",
            reason="project authoritative result provenance into the Evidence Bundle",
        )

        self.assertEqual(rebuilt["job"]["state"], "awaiting_semantic_review")
        self.assertGreaterEqual(rebuilt["job"]["usage"]["active_elapsed_seconds"], usage_before["active_elapsed_seconds"])
        self.assertEqual(
            {key: value for key, value in rebuilt["job"]["usage"].items() if key != "active_elapsed_seconds"},
            {key: value for key, value in usage_before.items() if key != "active_elapsed_seconds"},
        )
        self.assertEqual(fake.execute_calls, ["smoke", "local_preview"])
        self.assertEqual(fake.semantic_calls, 1)
        attempt = Path(rebuilt["paths"]["job_root"]) / "attempts" / "attempt_001"
        self.assertTrue((attempt / "evidence_bundle_superseded_001" / "manifest.json").is_file())
        self.assertTrue((attempt / "semantic_review_superseded_001" / "semantic_review.json").is_file())
        self.assertFalse((attempt / "semantic_review.json").exists())
        summary = read_json(attempt / "evidence_bundle" / "evidence_summary.json")
        self.assertEqual(summary["result_provenance"]["provider_usage"]["paid_submissions"], 0)

        reviewed = controller.run_semantic_review("job_m4_evidence_rebuild")
        self.assertEqual(reviewed["job"]["usage"]["reviewer_invocations"], 2)
        self.assertEqual(fake.semantic_calls, 2)

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

    def test_missing_evidence_artifact_emits_failed_job_terminal(self) -> None:
        controller, fake = self.controller()
        controller.create(
            self.request,
            job_id="job_missing_evidence_artifact_terminal",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        boundary = controller.advance_until_blocked("job_missing_evidence_artifact_terminal")
        root = Path(boundary["paths"]["job_root"])
        attempt_dir = root / "attempts" / "attempt_001"
        (attempt_dir / "evidence_bundle" / "evidence_summary.json").unlink()

        failed = controller.run_semantic_review("job_missing_evidence_artifact_terminal")

        self.assertEqual(failed["job"]["state"], "failed")
        self.assertEqual(failed["job"]["blocker"]["code"], "evidence_bundle_validation_failed")
        self.assertEqual(fake.semantic_calls, 0)
        leaf = failed["current_leaf_stage_result"]["result"]
        self.assertEqual(leaf["status"], "failed")
        self.assertEqual(leaf["failure_code"], "evidence_bundle_validation_failed")
        events = [read_json(path) for path in sorted((root / "events").glob("*.json"))]
        self.assertEqual([row["event"] for row in events[-2:]], ["stage_blocked", "job_terminal"])
        self.assertEqual(events[-1]["state"], "failed")
        self.assertEqual(events[-1]["stage"], "semantic_review")

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
            kwargs["lifecycle_callback"]("started")
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

    def test_reviewer_crash_after_launch_before_receipt_uses_only_technical_retry(self) -> None:
        class SimulatedProcessCrash(BaseException):
            pass

        fake = SuccessfulHarness()
        hooks = fake.hooks()

        def crash_after_launch(**kwargs):
            kwargs["lifecycle_callback"]("started")
            raise SimulatedProcessCrash("process died before receipt persistence")

        hooks.semantic_review = crash_after_launch
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_m4_reviewer_crash_window",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        boundary = controller.advance_until_blocked("job_m4_reviewer_crash_window")
        attempt = Path(boundary["paths"]["job_root"]) / "attempts" / "attempt_001"

        with self.assertRaises(SimulatedProcessCrash):
            controller.run_semantic_review("job_m4_reviewer_crash_window")

        first = read_json(attempt / "reviewer_reservation_001.json")
        self.assertEqual(first["state"], "started")
        self.assertFalse((attempt / "reviewer_invocation_001.json").exists())
        recovered_hooks = fake.hooks()
        recovered = AgentJobController(self.workspace, hooks=recovered_hooks)
        inspection = recovered.run_semantic_review("job_m4_reviewer_crash_window")

        self.assertEqual(inspection["job"]["state"], "completed")
        self.assertEqual(inspection["job"]["usage"]["reviewer_invocations"], 2)
        self.assertEqual(inspection["job"]["usage"]["stage_retries"]["semantic_review"], 1)
        self.assertEqual(read_json(attempt / "reviewer_reservation_001.json")["outcome"], "completion_unknown")
        self.assertEqual(read_json(attempt / "reviewer_reservation_002.json")["role"], "technical_retry")
        self.assertFalse((attempt / "reviewer_invocation_001.json").exists())
        self.assertTrue((attempt / "reviewer_invocation_002.json").is_file())

    def test_reviewer_interruption_pauses_and_resumes_without_technical_retry(self) -> None:
        fake = SuccessfulHarness()
        hooks = fake.hooks()

        def interrupted(**kwargs):
            kwargs["lifecycle_callback"]("started")
            raise SemanticReviewerError(
                "reviewer_interrupted",
                "user interrupted review",
                retryable=False,
                receipt=failed_reviewer_receipt(kwargs, "reviewer_interrupted", status="interrupted"),
            )

        hooks.semantic_review = interrupted
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_m4_reviewer_interrupted",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_reviewer_interrupted")
        paused = controller.run_semantic_review("job_m4_reviewer_interrupted")

        self.assertEqual(paused["job"]["state"], "paused_interrupted")
        self.assertEqual(paused["job"]["usage"]["total_retries"], 0)
        attempt = Path(paused["paths"]["job_root"]) / "attempts" / "attempt_001"
        self.assertEqual(read_json(attempt / "stage_results" / "semantic_review.json")["status"], "interrupted")
        controller.hooks.semantic_review = fake.semantic_review
        boundary = controller.resume("job_m4_reviewer_interrupted")
        self.assertEqual(boundary["job"]["state"], "awaiting_semantic_review")
        completed = controller.run_semantic_review("job_m4_reviewer_interrupted")
        self.assertEqual(completed["job"]["state"], "completed")
        self.assertEqual(completed["job"]["usage"]["reviewer_invocations"], 2)
        self.assertEqual(completed["job"]["usage"]["total_retries"], 0)
        self.assertEqual(read_json(attempt / "reviewer_reservation_002.json")["role"], "resume")

    def test_repeated_started_interruptions_cannot_exceed_reviewer_launch_limit(self) -> None:
        fake = SuccessfulHarness()
        hooks = fake.hooks()
        calls = 0

        def interrupted(**kwargs):
            nonlocal calls
            calls += 1
            kwargs["lifecycle_callback"]("started")
            raise SemanticReviewerError(
                "reviewer_interrupted",
                "user interrupted review",
                retryable=False,
                receipt=failed_reviewer_receipt(kwargs, "reviewer_interrupted", status="interrupted"),
            )

        hooks.semantic_review = interrupted
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_m4_reviewer_interrupt_cap",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_reviewer_interrupt_cap")
        self.assertEqual(controller.run_semantic_review("job_m4_reviewer_interrupt_cap")["job"]["state"], "paused_interrupted")
        controller.resume("job_m4_reviewer_interrupt_cap")
        second = controller.run_semantic_review("job_m4_reviewer_interrupt_cap")
        self.assertEqual(second["job"]["state"], "paused_interrupted")
        self.assertEqual(second["job"]["usage"]["reviewer_invocations"], 2)
        controller.resume("job_m4_reviewer_interrupt_cap")
        exhausted = controller.run_semantic_review("job_m4_reviewer_interrupt_cap")
        self.assertEqual(calls, 2)
        self.assertEqual(exhausted["job"]["blocker"]["code"], "reviewer_invocation_budget_exhausted")

    def test_permission_profile_block_is_stable_and_resumes_same_attempt(self) -> None:
        fake = SuccessfulHarness()
        hooks = fake.hooks()

        def unsupported(**kwargs):
            raise SemanticReviewerError(
                "reviewer_permission_profile_unsupported",
                "upgrade Codex permission profile support",
                retryable=False,
                receipt=failed_reviewer_receipt(kwargs, "reviewer_permission_profile_unsupported"),
            )

        hooks.semantic_review = unsupported
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_m4_permission_profile",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_permission_profile")
        blocked = controller.run_semantic_review("job_m4_permission_profile")
        self.assertEqual(blocked["job"]["state"], "blocked")
        self.assertEqual(blocked["job"]["blocker"]["code"], "reviewer_permission_profile_unsupported")
        self.assertEqual(blocked["job"]["usage"]["reviewer_invocations"], 0)
        self.assertEqual(blocked["job"]["usage"]["total_retries"], 0)
        attempt = Path(blocked["paths"]["job_root"]) / "attempts" / "attempt_001"
        controller.hooks.semantic_review = fake.semantic_review
        controller.resume("job_m4_permission_profile")
        completed = controller.run_semantic_review("job_m4_permission_profile")
        self.assertEqual(completed["job"]["state"], "completed")
        self.assertEqual(completed["job"]["usage"]["reviewer_invocations"], 1)
        self.assertFalse(read_json(attempt / "reviewer_reservation_001.json")["usage_counted"])
        self.assertEqual(read_json(attempt / "reviewer_reservation_002.json")["role"], "primary")
        self.assertEqual([row["attempt_id"] for row in completed["attempts"]], ["attempt_001"])

    def test_permission_profile_forbidden_does_not_consume_primary_invocation(self) -> None:
        fake = SuccessfulHarness()
        hooks = fake.hooks()

        def forbidden(**kwargs):
            raise SemanticReviewerError(
                "reviewer_permission_profile_forbidden",
                "managed requirements deny the Reviewer profile",
                retryable=False,
                receipt=failed_reviewer_receipt(kwargs, "reviewer_permission_profile_forbidden"),
            )

        hooks.semantic_review = forbidden
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_m4_permission_profile_forbidden",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_permission_profile_forbidden")

        blocked = controller.run_semantic_review("job_m4_permission_profile_forbidden")

        self.assertEqual(blocked["job"]["state"], "blocked")
        self.assertEqual(blocked["job"]["blocker"]["code"], "reviewer_permission_profile_forbidden")
        self.assertEqual(blocked["job"]["usage"]["reviewer_invocations"], 0)
        attempt = Path(blocked["paths"]["job_root"]) / "attempts" / "attempt_001"
        self.assertFalse(read_json(attempt / "reviewer_reservation_001.json")["usage_counted"])

        recovered = AgentJobController(self.workspace, hooks=fake.hooks())
        recovered.resume("job_m4_permission_profile_forbidden")
        completed = recovered.run_semantic_review("job_m4_permission_profile_forbidden")

        self.assertEqual(completed["job"]["state"], "completed")
        self.assertEqual(completed["job"]["usage"]["reviewer_invocations"], 1)
        self.assertEqual(read_json(attempt / "reviewer_reservation_002.json")["role"], "primary")

    def test_reviewer_usage_rebuild_repairs_manifest_counted_reservation_not_launched(self) -> None:
        fake = SuccessfulHarness()
        hooks = fake.hooks()

        def unsupported(**kwargs):
            raise SemanticReviewerError(
                "reviewer_permission_profile_unsupported",
                "upgrade Codex permission profile support",
                retryable=False,
                receipt=failed_reviewer_receipt(kwargs, "reviewer_permission_profile_unsupported"),
            )

        hooks.semantic_review = unsupported
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_m4_reviewer_usage_rebuild",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_reviewer_usage_rebuild")
        blocked = controller.run_semantic_review("job_m4_reviewer_usage_rebuild")
        attempt = Path(blocked["paths"]["job_root"]) / "attempts" / "attempt_001"
        self.assertFalse(read_json(attempt / "reviewer_reservation_001.json")["usage_counted"])

        stale = controller.store.load_manifest("job_m4_reviewer_usage_rebuild")
        stale["usage"]["reviewer_invocations"] = 1
        controller.store.write_manifest(stale)
        controller.hooks.semantic_review = fake.semantic_review
        controller.resume("job_m4_reviewer_usage_rebuild")
        completed = controller.run_semantic_review("job_m4_reviewer_usage_rebuild")

        self.assertEqual(completed["job"]["state"], "completed")
        self.assertEqual(completed["job"]["usage"]["reviewer_invocations"], 1)
        self.assertFalse(read_json(attempt / "reviewer_reservation_001.json")["usage_counted"])
        self.assertTrue(read_json(attempt / "reviewer_reservation_002.json")["usage_counted"])

    def test_semantic_review_checks_deadline_counts_runtime_and_obeys_total_retry_budget(self) -> None:
        fake = SuccessfulHarness()
        controller, _ = self.controller(fake)
        controller.create(
            self.request,
            job_id="job_m4_review_budget",
            budget={"max_total_retries": 0},
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        boundary = controller.advance_until_blocked("job_m4_review_budget")
        before = boundary["job"]["usage"]["active_elapsed_seconds"]
        successful = controller.hooks.semantic_review
        calls = 0

        def technical_failure(**kwargs):
            nonlocal calls
            calls += 1
            kwargs["lifecycle_callback"]("started")
            raise SemanticReviewerError(
                "reviewer_app_server_failure",
                "temporary failure",
                retryable=True,
                receipt=failed_reviewer_receipt(kwargs, "reviewer_app_server_failure"),
            )

        clock = iter((100.0, 107.5))
        controller.hooks.monotonic = lambda: next(clock)
        controller.hooks.semantic_review = technical_failure
        failed = controller.run_semantic_review("job_m4_review_budget")
        self.assertEqual(calls, 1)
        self.assertAlmostEqual(failed["job"]["usage"]["active_elapsed_seconds"] - before, 7.5, places=5)
        self.assertNotIn("semantic_review", failed["job"]["usage"]["stage_retries"])

        controller2, fake2 = self.controller(SuccessfulHarness())
        controller2.create(
            self.request,
            job_id="job_m4_review_deadline",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        deadline = controller2.advance_until_blocked("job_m4_review_deadline")
        manifest = deadline["job"]
        manifest["usage"]["active_elapsed_seconds"] = manifest["budget"]["soft_deadline_seconds"]
        controller2.store.write_manifest(manifest)
        blocked = controller2.run_semantic_review("job_m4_review_deadline")
        self.assertEqual(blocked["job"]["state"], "needs_user_decision")
        self.assertEqual(blocked["job"]["blocker"]["code"], "soft_deadline_reached")
        self.assertEqual(fake2.semantic_calls, 0)
        self.assertEqual(blocked["current_leaf_stage_result"]["result"]["failure_code"], "soft_deadline_reached")
        deadline_events = [
            read_json(path)
            for path in sorted((Path(blocked["paths"]["job_root"]) / "events").glob("*.json"))
        ]
        self.assertEqual(deadline_events[-1]["event"], "stage_blocked")
        self.assertEqual(deadline_events[-1]["stage"], "budget")

    def test_semantic_review_return_crossing_hard_deadline_blocks_without_resampling(self) -> None:
        controller, fake = self.controller()
        controller.create(
            self.request,
            job_id="job_m4_review_post_call_deadline",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        boundary = controller.advance_until_blocked("job_m4_review_post_call_deadline")
        manifest = boundary["job"]
        manifest["usage"]["active_elapsed_seconds"] = 1619.0
        controller.store.write_manifest(manifest)
        clock = iter((100.0, 282.0))
        controller.hooks.monotonic = lambda: next(clock)

        blocked = controller.run_semantic_review("job_m4_review_post_call_deadline")

        self.assertEqual(blocked["job"]["state"], "needs_user_decision")
        self.assertEqual(blocked["job"]["blocker"]["code"], "budget_exhausted")
        self.assertEqual(blocked["job"]["usage"]["active_elapsed_seconds"], 1801.0)
        self.assertEqual(fake.semantic_calls, 1)
        attempt = Path(blocked["paths"]["job_root"]) / "attempts" / "attempt_001"
        self.assertTrue((attempt / "semantic_review.json").is_file())
        self.assertEqual(blocked["current_leaf_stage_result"]["result"]["stage"], "budget")
        events = [read_json(path) for path in sorted((Path(blocked["paths"]["job_root"]) / "events").glob("*.json"))]
        self.assertNotIn("job_terminal", [row["event"] for row in events])

        resumed = controller.resume(
            "job_m4_review_post_call_deadline",
            budget_extension_seconds=300,
        )
        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(resumed["current_leaf_stage_result"]["result"]["stage"], "semantic_review")
        completed = controller.run_semantic_review("job_m4_review_post_call_deadline")
        self.assertEqual(completed["job"]["state"], "completed")
        self.assertEqual(fake.semantic_calls, 1)

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
        leaf = inspection["current_leaf_stage_result"]
        self.assertEqual(leaf["result"]["failure_code"], "catalog_missing")
        self.assertEqual(Path(leaf["path"]), Path(inspection["paths"]["job_root"]) / "stage_results" / "intake_readiness.json")
        events = [read_json(path) for path in sorted((Path(inspection["paths"]["job_root"]) / "events").glob("*.json"))]
        self.assertEqual([row["event"] for row in events], ["job_created", "stage_started", "stage_blocked"])

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
        controller.create(self.request, job_id="job_ambiguous_intent", publication_tier="local_preview", generation_mode="legacy")

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
        controller.create(self.request, job_id="job_interrupt_resume", publication_tier="local_preview", generation_mode="legacy")

        paused = controller.advance_until_blocked("job_interrupt_resume")
        self.assertEqual(paused["job"]["state"], "paused_interrupted")
        self.assertEqual(paused["job"]["current_stage"], "generation")

        resumed = controller.resume("job_interrupt_resume")
        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(resumed["job"]["current_attempt_id"], "attempt_001")

    def test_unclosed_running_stage_is_explicitly_recovered_without_manual_manifest_edits(self) -> None:
        controller, _ = self.controller()
        controller.create(
            self.request,
            job_id="job_stale_running_recovery",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        inspection = controller.advance_until_blocked("job_stale_running_recovery")
        manifest = inspection["job"]
        controller._update_manifest(
            manifest,
            state="running",
            current_stage="compile",
            blocker=None,
            allowed_next_actions=["cancel"],
        )
        manifest = controller.store.load_manifest("job_stale_running_recovery")
        controller._stage_event(manifest, "stage_started", "compile")

        stale = controller.inspect("job_stale_running_recovery")
        self.assertTrue(stale["interrupted_recovery"]["available"])
        self.assertEqual(stale["interrupted_recovery"]["reason"], "unclosed_stage_started_event")

        recovered = controller.recover_interrupted("job_stale_running_recovery")

        self.assertEqual(recovered["job"]["state"], "paused_interrupted")
        self.assertEqual(recovered["job"]["current_stage"], "compile")
        self.assertEqual(recovered["job"]["allowed_next_actions"], ["resume", "cancel"])
        self.assertEqual(recovered["current_leaf_stage_result"]["result"]["failure_class"], "interrupted")
        self.assertFalse(recovered["interrupted_recovery"]["available"])
        with self.assertRaisesRegex(JobStoreError, "not in running state"):
            controller.recover_interrupted("job_stale_running_recovery")

    def test_active_in_flight_marker_is_recoverable_only_after_controller_lock_releases(self) -> None:
        controller, _ = self.controller()
        controller.create(self.request, job_id="job_stale_lock", publication_tier="local_preview")
        manifest = controller.store.load_manifest("job_stale_lock")
        manifest = controller._update_manifest(
            manifest,
            state="running",
            blocker=None,
            allowed_next_actions=["cancel"],
        )
        controller._start_in_flight(manifest, "intake_readiness")
        self.assertEqual(
            controller.inspect("job_stale_lock")["interrupted_recovery"]["reason"],
            "active_in_flight_marker",
        )
        with controller.store.lock("job_stale_lock"):
            with self.assertRaisesRegex(JobStoreError, "already being advanced"):
                controller.recover_interrupted("job_stale_lock")

        recovered = controller.recover_interrupted("job_stale_lock")
        marker = read_json(Path(recovered["paths"]["job_root"]) / "checkpoints" / "in_flight.json")
        self.assertEqual(marker["status"], "closed")
        self.assertEqual(marker["outcome"], "recovered_interrupted")

    def test_completed_stage_transition_recovers_at_next_stage_without_overwriting_completion(self) -> None:
        controller, _ = self.controller()
        controller.create(self.request, job_id="job_transition_crash", publication_tier="local_preview")
        manifest = controller.store.load_manifest("job_transition_crash")
        manifest = controller._update_manifest(
            manifest,
            state="running",
            blocker=None,
            allowed_next_actions=["cancel"],
        )
        controller._start_in_flight(manifest, "intake_readiness")
        completed = build_stage_result(
            stage="intake_readiness",
            status="completed",
            job_id="job_transition_crash",
        )
        controller._write_controller_stage_result(manifest, completed)
        controller._checkpoint(manifest, "intake_readiness", "completed", manifest["request_digest"])
        controller._update_manifest(manifest, current_stage="generation")

        stale = controller.inspect("job_transition_crash")
        self.assertTrue(stale["interrupted_recovery"]["available"])
        self.assertEqual(stale["interrupted_recovery"]["reason"], "completed_stage_transition")
        recovered = controller.recover_interrupted("job_transition_crash")

        self.assertEqual(recovered["job"]["state"], "paused_interrupted")
        self.assertEqual(recovered["job"]["current_stage"], "generation")
        prior = read_json(Path(recovered["paths"]["job_root"]) / "stage_results" / "intake_readiness.json")
        self.assertEqual(prior["status"], "completed")
        self.assertEqual(recovered["current_leaf_stage_result"]["result"]["stage"], "generation")

    def test_interrupted_recovery_accounts_elapsed_once_and_reapplies_hard_deadline(self) -> None:
        fake = SuccessfulHarness()
        clock = [100.0]
        hooks = fake.hooks()
        hooks.monotonic = lambda: clock[0]
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_recovery_elapsed",
            publication_tier="local_preview",
            budget={"soft_deadline_seconds": 10, "hard_deadline_seconds": 10},
        )
        manifest = controller.store.load_manifest("job_recovery_elapsed")
        manifest = controller._update_manifest(
            manifest,
            state="running",
            blocker=None,
            allowed_next_actions=["cancel"],
        )
        controller._start_in_flight(manifest, "intake_readiness", started_monotonic=clock[0])
        clock[0] = 115.0

        first = controller._reconcile_in_flight_elapsed(manifest)
        second = controller._reconcile_in_flight_elapsed(first)
        self.assertEqual(first["usage"]["active_elapsed_seconds"], 15.0)
        self.assertEqual(second["usage"]["active_elapsed_seconds"], 15.0)

        recovered = controller.recover_interrupted("job_recovery_elapsed")
        marker = read_json(Path(recovered["paths"]["job_root"]) / "checkpoints" / "in_flight.json")
        self.assertEqual(recovered["job"]["state"], "needs_user_decision")
        self.assertEqual(recovered["job"]["blocker"]["code"], "budget_exhausted")
        self.assertEqual(recovered["job"]["usage"]["active_elapsed_seconds"], 15.0)
        self.assertEqual(marker["elapsed_accounted_seconds"], 15.0)

    def test_semantic_reviewer_host_interruption_leaves_recoverable_in_flight_marker(self) -> None:
        controller, _ = self.controller()
        controller.create(
            self.request,
            job_id="job_reviewer_host_interrupt",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        ready = controller.advance_until_blocked("job_reviewer_host_interrupt")
        self.assertEqual(ready["job"]["state"], "awaiting_semantic_review")

        def interrupted_review(**kwargs):
            raise KeyboardInterrupt()

        controller.hooks.semantic_review = interrupted_review
        with self.assertRaises(KeyboardInterrupt):
            controller.run_semantic_review("job_reviewer_host_interrupt")

        stale = controller.inspect("job_reviewer_host_interrupt")
        marker = read_json(Path(stale["paths"]["job_root"]) / "checkpoints" / "in_flight.json")
        self.assertEqual(stale["job"]["state"], "running")
        self.assertTrue(stale["interrupted_recovery"]["available"])
        self.assertEqual(stale["interrupted_recovery"]["stage"], "semantic_review")
        self.assertEqual(marker["status"], "active")

        recovered = controller.recover_interrupted("job_reviewer_host_interrupt")
        self.assertEqual(recovered["job"]["state"], "paused_interrupted")
        self.assertEqual(recovered["job"]["current_stage"], "semantic_review")

    def test_generation_resume_after_intent_commit_reuses_immutable_contract(self) -> None:
        controller, fake = self.controller()
        controller.create(self.request, job_id="job_intent_commit_crash", publication_tier="local_preview", generation_mode="legacy")
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

    def test_generation_resume_reads_real_legacy_v1_contract_fail_closed(self) -> None:
        controller, _ = self.controller()
        controller.create(self.request, job_id="job_legacy_v1_intent", publication_tier="local_preview", generation_mode="legacy")
        original_create_attempt = controller.store.create_attempt
        calls = 0

        def interrupt_after_intent(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt()
            return original_create_attempt(*args, **kwargs)

        controller.store.create_attempt = interrupt_after_intent
        paused = controller.advance_until_blocked("job_legacy_v1_intent")
        intent_path = Path(paused["paths"]["intent_contract"])
        legacy = read_json(intent_path)
        legacy["schema_version"] = "harness_intent_contract_v1"
        legacy.pop("planning_image_requirement")
        legacy["allowed_adjustments"] = {
            "paths": ["$.scene.duration_s", "$.observation_requirements", "$.scene.camera"],
            "ranges": {},
        }
        write_json(intent_path, legacy)

        resumed = controller.resume("job_legacy_v1_intent")

        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(IntentContract.from_dict(read_json(intent_path)).to_dict(), legacy)
        self.assertEqual(resumed["job"]["intent_contract_digest"], stable_digest(legacy))
        effective_allowed = controller._effective_allowed_adjustments(legacy)
        self.assertEqual(effective_allowed, {"paths": [], "ranges": {}})
        with self.assertRaisesRegex(ValueError, "outside Intent Contract allowed_adjustments"):
            controller._validate_revision_changes(
                [{"path": "$.scene.duration_s", "operation": "replace", "before": 2.0, "after": 2.2}],
                effective_allowed,
                repair_layer="case_spec_source",
            )

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
        controller.create(self.request, job_id="job_transient_retry", publication_tier="local_preview", generation_mode="legacy")

        inspection = controller.advance_until_blocked("job_transient_retry")

        self.assertEqual(inspection["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(inspection["job"]["usage"]["total_retries"], 1)
        self.assertEqual(inspection["job"]["usage"]["stage_retries"], {"generation": 1})
        self.assertEqual([row["attempt_id"] for row in inspection["attempts"]], ["attempt_001"])

    def test_terminal_transient_failure_has_audited_same_attempt_retry(self) -> None:
        fake = SuccessfulHarness()
        original_execute = fake.execute
        calls = 0

        def transient_execute(case, output_root, *, compilation, profile, **kwargs):
            nonlocal calls
            calls += 1
            if calls <= 2:
                run_dir = Path(output_root) / f"{case.case_id}_{compilation.selected_backend}"
                run_dir.mkdir(parents=True, exist_ok=True)
                write_stage_result(
                    run_dir,
                    failure_stage_result(
                        stage="execute",
                        failure_code="backend_importer_timeout",
                        message="transient external launch timeout",
                        retryable=True,
                    ),
                )
                raise RuntimeError("transient external launch timeout")
            return original_execute(case, output_root, compilation=compilation, profile=profile, **kwargs)

        hooks = fake.hooks()
        hooks.execute = transient_execute
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_failed_stage_retry",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )

        failed = controller.advance_until_blocked("job_failed_stage_retry")
        transaction = Path(failed["paths"]["job_root"]) / "attempts" / "attempt_001" / "compilation" / "compilation_transaction.json"
        transaction_digest = stable_digest(read_json(transaction))
        usage_before = copy.deepcopy(failed["job"]["usage"])
        self.assertEqual(failed["job"]["state"], "failed")
        self.assertTrue(failed["failed_stage_retry"]["available"])
        self.assertIn("retry_failed_stage", failed["job"]["allowed_next_actions"])

        reopened = controller.retry_failed_stage(
            "job_failed_stage_retry",
            reason="external execution permission was corrected",
        )

        self.assertEqual(reopened["job"]["state"], "paused_interrupted")
        self.assertEqual(reopened["job"]["current_attempt_id"], "attempt_001")
        self.assertEqual(reopened["job"]["usage"], usage_before)
        self.assertEqual(stable_digest(read_json(transaction)), transaction_digest)
        receipt = Path(reopened["paths"]["job_root"]) / "amendments" / "failed_stage_retry_001.json"
        self.assertEqual(read_json(receipt)["failure_code"], "backend_importer_timeout")

        resumed = controller.resume("job_failed_stage_retry")
        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(resumed["job"]["current_attempt_id"], "attempt_001")
        self.assertEqual(calls, 4)
        self.assertEqual(resumed["job"]["usage"]["total_retries"], 1)

    def test_map_blocker_recompile_archives_transaction_and_preserves_provider_and_usage(self) -> None:
        controller, fake = self.controller()
        controller.create(
            self.request,
            job_id="job_map_config_recompile",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        completed = controller.advance_until_blocked("job_map_config_recompile")
        root = Path(completed["paths"]["job_root"])
        attempt_dir = root / "attempts" / "attempt_001"
        attempt = controller.store.load_attempt("job_map_config_recompile", "attempt_001")
        transaction_path = attempt_dir / "compilation" / "compilation_transaction.json"
        transaction = read_json(transaction_path)
        transaction["input_identity"] = {
            "case_spec_digest": attempt["case_spec_digest"],
            "requested_backend": "fallback",
        }
        transaction["catalog_snapshot"] = {
            "path": str(controller.config.catalog),
            "sha256": None,
        }
        write_json(transaction_path, transaction)
        blocker = failure_stage_result(
            stage="preflight",
            failure_code="F3_UE_MAP_MISSING",
            message="compiled scene has no requested Map",
            source_status="blocked",
            job_id="job_map_config_recompile",
            attempt_id="attempt_001",
        )
        write_stage_result(attempt_dir, blocker)
        manifest = controller.store.load_manifest("job_map_config_recompile")
        controller._update_manifest(
            manifest,
            state="blocked",
            current_stage="smoke",
            blocker={"code": "F3_UE_MAP_MISSING", "message": blocker["message"], "stage": "preflight"},
            allowed_next_actions=["resume", "cancel"],
        )
        before = controller.inspect("job_map_config_recompile")
        usage_before = copy.deepcopy(before["job"]["usage"])
        provider_before = read_json(attempt_dir / "compilation" / "asset_provider_batch.json")

        reopened = controller.recompile_after_config(
            "job_map_config_recompile",
            reason="configured the Catalog-qualified Harness Map",
        )

        self.assertTrue(before["configuration_recompile"]["available"])
        self.assertEqual(reopened["job"]["state"], "paused_interrupted")
        self.assertEqual(reopened["job"]["current_stage"], "compile")
        self.assertEqual(reopened["job"]["current_attempt_id"], "attempt_001")
        self.assertEqual(reopened["job"]["usage"], usage_before)
        self.assertFalse((attempt_dir / "compilation" / "compilation_transaction.json").exists())
        self.assertEqual(read_json(attempt_dir / "compilation" / "asset_provider_batch.json"), provider_before)
        self.assertTrue((attempt_dir / "compilation_superseded_001" / "compilation_transaction.json").is_file())
        receipt = read_json(root / "receipts" / "configuration_recompile_001.json")
        self.assertNotEqual(receipt["old_compile_config_digest"], receipt["new_compile_config_digest"])
        self.assertEqual(receipt["provider_checkpoint"]["request_identities"], [])
        self.assertEqual(fake.compile_calls.count((("event_closeup",), ("rgb",))), 1)

        resumed = controller.resume("job_map_config_recompile")
        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(resumed["job"]["current_attempt_id"], "attempt_001")
        self.assertEqual(fake.compile_calls.count((("event_closeup",), ("rgb",))), 2)
        self.assertGreaterEqual(
            resumed["job"]["usage"]["active_elapsed_seconds"],
            usage_before["active_elapsed_seconds"],
        )
        self.assertEqual(
            {key: value for key, value in resumed["job"]["usage"].items() if key != "active_elapsed_seconds"},
            {key: value for key, value in usage_before.items() if key != "active_elapsed_seconds"},
        )

    def test_configuration_recompile_rejects_non_map_and_non_configuration_failures(self) -> None:
        controller, _ = self.controller()
        controller.create(
            self.request,
            job_id="job_recompile_rejected",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        completed = controller.advance_until_blocked("job_recompile_rejected")
        manifest = controller.store.load_manifest("job_recompile_rejected")
        attempt_dir = Path(completed["paths"]["job_root"]) / "attempts" / "attempt_001"
        for stage, code in (("preflight", "F4_UE_ACTOR_CLASS_MISSING"), ("verifier", "declared_assertion_failed")):
            result = failure_stage_result(
                stage=stage,
                failure_code=code,
                message="not a compile-affecting Map correction",
                source_status="blocked" if stage == "preflight" else None,
                job_id="job_recompile_rejected",
                attempt_id="attempt_001",
            )
            write_stage_result(attempt_dir, result)
            controller._update_manifest(
                manifest,
                state="blocked",
                current_stage="smoke",
                blocker={"code": code, "message": result["message"], "stage": stage},
                allowed_next_actions=["resume", "cancel"],
            )
            inspection = controller.inspect("job_recompile_rejected")
            self.assertFalse(inspection["configuration_recompile"]["available"])
            with self.assertRaises(JobStoreError):
                controller.recompile_after_config("job_recompile_rejected", reason="irrelevant change")
            manifest = controller.store.load_manifest("job_recompile_rejected")

    def test_reviewer_contract_fix_chains_two_digests_without_reexecuting_candidate(self) -> None:
        fake = SuccessfulHarness()
        hooks = fake.hooks()
        successful_review = hooks.semantic_review
        digest = {"value": "a" * 64}
        calls = 0

        def schema_invalid_then_valid(**kwargs):
            nonlocal calls
            calls += 1
            result = successful_review(**kwargs)
            if calls <= 3:
                for requirement in result["review"]["requirements"]:
                    requirement["evidence_refs"][0].update(
                        {
                            "time_s": None,
                            "view_id": None,
                            "trajectory_range": None,
                            "contact_event_id": None,
                        }
                    )
                result["receipt"]["output_digest"] = stable_digest(result["review"])
            result["receipt"]["input_digest"] = digest["value"]
            return result

        hooks.semantic_review = schema_invalid_then_valid
        controller = AgentJobController(self.workspace, hooks=hooks)
        with (
            patch(
                "harness.agent.job_controller.semantic_reviewer_input_digest",
                side_effect=lambda **_kwargs: digest["value"],
            ),
            patch(
                "tests.test_agent_job_controller.semantic_reviewer_input_digest",
                side_effect=lambda **_kwargs: digest["value"],
            ),
        ):
            controller.create(
                self.request,
                job_id="job_reviewer_contract_retry",
                budget={"max_total_retries": 2},
                publication_tier="local_preview",
                seed_case_spec=case_spec_v2_fixture(),
            )
            ready = controller.advance_until_blocked("job_reviewer_contract_retry")
            failed = controller.run_semantic_review("job_reviewer_contract_retry")

            self.assertEqual(failed["job"]["state"], "failed")
            self.assertEqual(failed["job"]["blocker"]["code"], "reviewer_output_schema_invalid")
            self.assertFalse(failed["reviewer_contract_retry"]["available"])
            usage_before = copy.deepcopy(failed["job"]["usage"])
            budget_before = copy.deepcopy(failed["job"]["budget"])
            execute_before = list(fake.execute_calls)
            bundle_before = stable_digest(
                read_json(Path(ready["paths"]["job_root"]) / "attempts" / "attempt_001" / "evidence_bundle" / "manifest.json")
            )

            digest["value"] = "b" * 64
            eligible = controller.inspect("job_reviewer_contract_retry")
            self.assertTrue(eligible["reviewer_contract_retry"]["available"])
            reopened = controller.retry_review_after_contract_fix(
                "job_reviewer_contract_retry",
                reason="Reviewer locator contract is now explicit in the prompt and output schema",
            )

            self.assertEqual(reopened["job"]["state"], "awaiting_semantic_review")
            self.assertEqual(reopened["job"]["usage"], usage_before)
            self.assertEqual(
                reopened["job"]["budget"]["max_reviewer_technical_retries"],
                budget_before["max_reviewer_technical_retries"] + 1,
            )
            receipt_path = Path(reopened["paths"]["job_root"]) / "amendments" / "reviewer_contract_retry_001.json"
            receipt = read_json(receipt_path)
            self.assertEqual(receipt["schema_version"], "harness_agent_reviewer_contract_retry_v2")
            self.assertEqual(receipt["old_input_digest"], "a" * 64)
            self.assertEqual(receipt["new_input_digest"], "b" * 64)
            self.assertEqual(receipt["usage_preserved"], usage_before)

            # The real Job created receipt 001 before v2 added explicit total-retry
            # budget fields. Preserve and validate that immutable v1 migration shape.
            receipt["schema_version"] = "harness_agent_reviewer_contract_retry_v1"
            receipt.pop("total_retries_before")
            receipt.pop("total_retries_after")
            write_json(receipt_path, receipt)

            failed_again = controller.run_semantic_review("job_reviewer_contract_retry")

            self.assertEqual(failed_again["job"]["state"], "failed")
            self.assertEqual(calls, 3)
            self.assertEqual(failed_again["job"]["usage"]["total_retries"], 2)
            digest["value"] = "c" * 64
            eligible_again = controller.inspect("job_reviewer_contract_retry")
            self.assertTrue(eligible_again["reviewer_contract_retry"]["available"])
            reopened_again = controller.retry_review_after_contract_fix(
                "job_reviewer_contract_retry",
                reason="Canonical adjustment paths are now explicit in the Reviewer contract",
            )

            self.assertEqual(reopened_again["job"]["state"], "awaiting_semantic_review")
            self.assertEqual(
                reopened_again["job"]["budget"]["max_reviewer_technical_retries"],
                budget_before["max_reviewer_technical_retries"] + 2,
            )
            self.assertEqual(reopened_again["job"]["budget"]["max_total_retries"], 3)
            receipt_002 = read_json(
                Path(reopened_again["paths"]["job_root"])
                / "amendments"
                / "reviewer_contract_retry_002.json"
            )
            self.assertEqual(receipt_002["schema_version"], "harness_agent_reviewer_contract_retry_v2")
            self.assertEqual(receipt_002["old_input_digest"], "b" * 64)
            self.assertEqual(receipt_002["new_input_digest"], "c" * 64)
            self.assertEqual(receipt_002["prior_invocation_count"], 3)
            self.assertEqual(receipt_002["total_retries_before"], 2)
            self.assertEqual(receipt_002["total_retries_after"], 3)

            completed = controller.run_semantic_review("job_reviewer_contract_retry")

            self.assertEqual(completed["job"]["state"], "completed")
            self.assertEqual(calls, 4)
            self.assertEqual(fake.execute_calls, execute_before)
            self.assertEqual(completed["job"]["usage"]["reviewer_invocations"], 4)
            self.assertEqual(
                stable_digest(
                    read_json(
                        Path(completed["paths"]["job_root"])
                        / "attempts"
                        / "attempt_001"
                        / "evidence_bundle"
                        / "manifest.json"
                    )
                ),
                bundle_before,
            )
            with self.assertRaises(JobStoreError):
                controller.retry_review_after_contract_fix(
                    "job_reviewer_contract_retry",
                    reason="a second contract retry is forbidden",
                )

    def test_failed_importer_usage_migrates_before_same_job_retry(self) -> None:
        controller, _ = self.controller()
        controller.create(
            self.request,
            job_id="job_legacy_importer_usage",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        ready = controller.advance_until_blocked("job_legacy_importer_usage")
        manifest = controller.store.load_manifest("job_legacy_importer_usage")
        attempt_dir = Path(ready["paths"]["job_root"]) / "attempts" / "attempt_001"
        (Path(ready["paths"]["job_root"]) / "receipts" / "ue_launch_usage.json").unlink(missing_ok=True)
        write_json(
            attempt_dir / "compilation" / "asset_provider_batch.json",
            {
                "schema_version": "harness_asset_provider_batch_v1",
                "case_id": "fixture",
                "requests": [],
                "results": [],
                "receipt_ids": [],
                "import_summary": {"importer_invocation_count": 1},
            },
        )
        provider_result = failure_stage_result(
            stage="provider",
            failure_code="backend_importer_timeout",
            message="Unreal asset import exceeded its timeout",
            retryable=True,
            job_id="job_legacy_importer_usage",
            attempt_id="attempt_001",
            invocation_count=2,
        )
        write_stage_result(attempt_dir / "compilation", provider_result)
        controller._update_manifest(
            manifest,
            state="failed",
            current_stage="compile",
            blocker={"code": "backend_importer_timeout", "message": "timeout", "stage": "provider"},
            allowed_next_actions=["inspect_artifacts"],
        )

        reopened = controller.retry_failed_stage(
            "job_legacy_importer_usage",
            reason="UE will be relaunched outside the workspace sandbox",
        )

        self.assertEqual(reopened["job"]["usage"]["ue_launches"], 2)
        ledger = read_json(Path(reopened["paths"]["job_root"]) / "receipts" / "ue_launch_usage.json")
        self.assertEqual(ledger["baseline_launches"], 2)
        self.assertEqual(ledger["legacy_importer_launches_reconciled"], 2)

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
        self.assertEqual(fake.execute_calls, ["smoke", "local_preview"])

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
        leaf = inspection["current_leaf_stage_result"]
        self.assertEqual(leaf["result"]["status"], "blocked")
        self.assertEqual(leaf["result"]["failure_code"], "budget_exhausted")
        self.assertEqual(Path(leaf["path"]), Path(inspection["paths"]["job_root"]) / "stage_results" / "budget.json")
        events = [read_json(path) for path in sorted((Path(inspection["paths"]["job_root"]) / "events").glob("*.json"))]
        self.assertEqual([row["event"] for row in events], ["job_created", "stage_blocked"])
        self.assertEqual(events[-1]["stage"], "budget")
        self.assertEqual(events[-1]["result"]["failure_code"], "budget_exhausted")

    def test_unlaunched_reviewer_setup_failure_recovers_without_retry_or_usage(self) -> None:
        fake = SuccessfulHarness()
        hooks = fake.hooks()

        def setup_failure(**kwargs):
            raise SemanticReviewerError(
                "reviewer_app_server_failure",
                "state runtime is unavailable",
                retryable=True,
                receipt=failed_reviewer_receipt(kwargs, "reviewer_app_server_failure"),
            )

        hooks.semantic_review = setup_failure
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_m4_unlaunched_review",
            budget={"max_total_retries": 0},
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
        )
        controller.advance_until_blocked("job_m4_unlaunched_review")
        failed = controller.run_semantic_review("job_m4_unlaunched_review")
        self.assertEqual(failed["job"]["usage"]["reviewer_invocations"], 0)
        self.assertEqual(failed["job"]["usage"]["total_retries"], 0)

        recovered = controller.recover_unlaunched_review(
            "job_m4_unlaunched_review",
            reason="make the Reviewer state directory writable",
        )
        self.assertEqual(recovered["job"]["state"], "awaiting_semantic_review")
        controller.hooks.semantic_review = fake.semantic_review
        completed = controller.run_semantic_review("job_m4_unlaunched_review")

        self.assertEqual(completed["job"]["state"], "completed")
        self.assertEqual(completed["job"]["usage"]["reviewer_invocations"], 1)
        self.assertEqual(completed["job"]["usage"]["total_retries"], 0)

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

        self.assertEqual(inspection["job"]["state"], "needs_user_decision")
        self.assertEqual(inspection["job"]["blocker"]["code"], "ue_launch_budget_exhausted")
        self.assertEqual(inspection["job"]["allowed_next_actions"], ["inspect_artifacts", "cancel"])
        self.assertEqual(fake.execute_calls, [])

    def test_revision_budget_exhaustion_preserves_trigger_and_blocks_stale_revision(self) -> None:
        controller, _ = self.controller(SuccessfulHarness(fail_verifier=True))
        controller.create(
            self.request,
            job_id="job_revision_budget",
            publication_tier="local_preview",
            seed_case_spec=case_spec_v2_fixture(),
            budget={"max_case_spec_revisions": 1},
        )

        inspection = controller.advance_until_blocked("job_revision_budget")

        self.assertEqual(inspection["job"]["state"], "needs_user_decision")
        self.assertEqual(inspection["job"]["current_stage"], "budget")
        self.assertEqual(inspection["job"]["blocker"]["code"], "case_spec_revision_budget_exhausted")
        self.assertEqual(inspection["job"]["allowed_next_actions"], ["inspect_artifacts", "cancel"])
        self.assertFalse(inspection["case_spec_revision_policy"]["available"])
        root = Path(inspection["paths"]["job_root"])
        budget_path = root / "attempts" / "attempt_001" / "stage_results" / "budget.json"
        budget_result = read_json(budget_path)
        self.assertEqual(budget_result["failure_code"], "case_spec_revision_budget_exhausted")
        trigger_path = Path(budget_result["artifact_refs"][0]["path"])
        self.assertTrue(trigger_path.is_file())
        self.assertEqual(trigger_path.name, "verifier.json")

        stale = read_json(root / "job_manifest.json")
        stale["state"] = "needs_user_decision"
        stale["current_stage"] = "verifier"
        stale["blocker"] = {
            "code": "declared_assertion_failed",
            "message": "historical manifest still projects a revision",
            "stage": "verifier",
        }
        stale["allowed_next_actions"] = ["resume_with_revision", "cancel"]
        write_json(root / "job_manifest.json", stale)
        stale_inspection = controller.inspect("job_revision_budget")
        self.assertEqual(stale_inspection["job"]["allowed_next_actions"], ["cancel"])
        revised = case_spec_v2_fixture()
        revised["scene"]["duration_s"] = 2.5

        rejected = controller.resume(
            "job_revision_budget",
            revised_case_spec=revised,
            revision_reason="historical stale action",
        )

        self.assertEqual(rejected["job"]["blocker"]["code"], "case_spec_revision_budget_exhausted")
        self.assertEqual(rejected["job"]["allowed_next_actions"], ["inspect_artifacts", "cancel"])
        self.assertFalse((root / "attempts" / "attempt_002").exists())
        rewritten_budget_result = read_json(budget_path)
        self.assertEqual(Path(rewritten_budget_result["artifact_refs"][0]["path"]).name, "verifier.json")

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

        rejected = controller.resume(
            "job_revision_budget",
            revised_case_spec=over_limit,
            revision_reason="sixth revision",
        )

        self.assertEqual(rejected["job"]["blocker"]["code"], "case_spec_revision_budget_exhausted")
        self.assertEqual(rejected["job"]["allowed_next_actions"], ["inspect_artifacts", "cancel"])
        self.assertEqual(len(rejected["attempts"]), 5)

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
        self.assertEqual(fake.execute_calls, ["smoke", "local_preview"])
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
        self.assertEqual(fake.execute_calls, ["smoke", "local_preview"])

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

    def test_revision_preserves_frozen_asset_policy_and_does_not_implicitly_open_mass(self) -> None:
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
        unlisted["objects"][0]["physics"]["body_type"] = "kinematic"
        with self.assertRaisesRegex(ValueError, "allowed_adjustments"):
            controller.resume("job_frozen_policy", revised_case_spec=unlisted, revision_reason="change topology")

        revised = case_spec_v2_fixture()
        revised["objects"][0]["physics"]["mass_kg"] = 0.2
        with self.assertRaisesRegex(ValueError, "allowed_adjustments"):
            controller.resume("job_frozen_policy", revised_case_spec=revised, revision_reason="change mass")

    def test_object_leaf_adjustment_uses_stable_object_id_path(self) -> None:
        before = case_spec_v2_fixture()
        after = copy.deepcopy(before)
        after["objects"][0]["geometry"]["shape_hint"] = "box"

        changes = AgentJobController._json_diff(before, after)

        self.assertEqual(
            changes,
            [
                {
                    "path": "$.objects.cue_ball.geometry.shape_hint",
                    "operation": "replace",
                    "before": "sphere",
                    "after": "box",
                }
            ],
        )
        AgentJobController._validate_revision_changes(
            changes,
            {
                "paths": ["$.objects.cue_ball.geometry.shape_hint"],
                "ranges": {
                    "$.objects.cue_ball.geometry.shape_hint": {
                        "kind": "enum",
                        "values": ["box"],
                    }
                },
            },
            repair_layer="case_spec_source",
        )

    def test_case_spec_revision_policy_opens_bounded_layout_fields_by_semantics(self) -> None:
        case_spec = case_spec_v2_fixture()
        cue = case_spec["objects"][0]
        cue["initial_state"]["angular_velocity_deg_s"] = [0.0, 0.0, 1500.0]
        cue["physics"].update(
            {
                "linear_damping": 0.02,
                "angular_damping": 0.24,
                "enable_gravity": True,
                "use_ccd": False,
            }
        )
        cue["physics"]["material"]["static_friction"] = 0.04

        policy = AgentJobController._case_spec_revision_policy(case_spec)

        self.assertIn("$.objects.cue_ball.initial_state.position_m", policy["paths"])
        self.assertIn("$.objects.cue_ball.initial_state.rotation_deg", policy["paths"])
        self.assertNotIn("$.objects.cue_ball.initial_state.linear_velocity_m_s", policy["paths"])
        self.assertNotIn("$.objects.cue_ball.initial_state.angular_velocity_deg_s", policy["paths"])
        self.assertNotIn("$.objects.cue_ball.physics.mass_kg", policy["paths"])
        self.assertNotIn("$.objects.cue_ball.physics.linear_damping", policy["paths"])
        self.assertNotIn("$.objects.cue_ball.physics.angular_damping", policy["paths"])
        self.assertNotIn("$.objects.cue_ball.physics.enable_gravity", policy["paths"])
        self.assertNotIn("$.objects.cue_ball.physics.use_ccd", policy["paths"])
        self.assertNotIn("$.objects.cue_ball.physics.material.static_friction", policy["paths"])
        self.assertNotIn("$.objects.cue_ball.physics.material.dynamic_friction", policy["paths"])
        self.assertNotIn("$.objects.cue_ball.physics.material.restitution", policy["paths"])
        self.assertIn("$.objects.floor.geometry.approx_size_m", policy["paths"])
        self.assertNotIn("$.objects.cue_ball.geometry.approx_size_m", policy["paths"])
        self.assertNotIn("$.objects.cue_ball.physics.body_type", policy["paths"])
        self.assertNotIn("$.objects.cue_ball.physics.collision_required", policy["paths"])
        AgentJobController._validate_revision_changes(
            [
                {
                    "path": "$.objects.cue_ball.initial_state.rotation_deg",
                    "operation": "replace",
                    "before": [0.0, 0.0, 0.0],
                    "after": [0.0, 45.0, 0.0],
                }
            ],
            policy,
            repair_layer="case_spec_source",
        )
        with self.assertRaisesRegex(ValueError, "numeric vector range"):
            AgentJobController._validate_revision_changes(
                [
                    {
                        "path": "$.objects.cue_ball.initial_state.rotation_deg",
                        "operation": "replace",
                        "before": [0.0, 0.0, 0.0],
                        "after": [0.0, 200.0, 0.0],
                    }
                ],
                policy,
                repair_layer="case_spec_source",
            )

        with self.assertRaisesRegex(ValueError, "allowed_adjustments"):
            AgentJobController._validate_revision_changes(
                [
                    {
                        "path": "$.objects.cue_ball.physics.angular_damping",
                        "operation": "replace",
                        "before": 0.24,
                        "after": 0.1,
                    }
                ],
                policy,
                repair_layer="case_spec_source",
            )

    def test_case_spec_revision_policy_excludes_hard_parameter_paths(self) -> None:
        path = "$.objects.cue_ball.initial_state.position_m"

        policy = AgentJobController._case_spec_revision_policy(
            case_spec_v2_fixture(),
            excluded_paths={path},
        )

        self.assertNotIn(path, policy["paths"])

    def test_soft_intent_explicitly_opens_one_physics_parameter(self) -> None:
        case_spec = case_spec_v2_fixture()
        path = "$.objects.cue_ball.physics.mass_kg"
        declared = AgentJobController._project_allowed_adjustments(
            {
                "parameter_analysis": [
                    {
                        "path": path,
                        "requirement_level": "soft",
                        "constraint": {"kind": "numeric", "min": 0.1, "max": 0.3},
                    }
                ]
            },
            case_spec,
        )
        policy = AgentJobController._overlay_allowed_adjustments(
            AgentJobController._case_spec_revision_policy(case_spec),
            declared,
        )

        self.assertIn(path, policy["paths"])
        AgentJobController._validate_revision_changes(
            [
                {
                    "path": path,
                    "operation": "replace",
                    "before": 0.17,
                    "after": 0.2,
                }
            ],
            policy,
            repair_layer="case_spec_source",
        )

    def test_historical_provider_shape_failure_recovers_by_exact_allowed_adjustments(self) -> None:
        controller, _ = self.controller()
        invalid = case_spec_v2_fixture()
        subject = invalid["objects"][0]
        subject["geometry"]["shape_hint"] = "upright rectangular box with longest edge vertical"
        subject["geometry"]["approx_size_m"] = [0.03, 0.12, 0.3]
        subject["asset"] = {
            "description": "a local procedural domino",
            "resource_kind": "mesh_3d",
            "must": {
                "geometry_type": "box",
                "source_kind": "procedural_generation",
            },
            "acquisition": {
                "route": "procedural_generation",
                "requirement": "required",
                "origin": "user_explicit",
                "provider_hint": "box_mesh_v1",
                "reference_inputs": [],
                "fallback_order": [],
            },
        }
        controller.create(
            self.request,
            job_id="job_provider_contract_repair",
            publication_tier="local_preview",
            seed_case_spec=invalid,
        )
        blocked = controller.advance_until_blocked("job_provider_contract_repair")
        self.assertEqual(blocked["job"]["blocker"]["code"], "invalid_generation_spec")
        attempt_dir = Path(blocked["paths"]["job_root"]) / "attempts" / "attempt_001"
        batch = {
            "schema_version": "harness_asset_provider_batch_v1",
            "case_id": "v2_ball_contact",
            "requests": [
                {
                    "object_id": "cue_ball",
                    "request_digest": "a" * 64,
                    "generation_spec": {
                        "recipe_id": "box_mesh_v1",
                        "recipe_version": "v1",
                        "shape": "upright rectangular box with longest edge vertical",
                        "size_m": [0.03, 0.12, 0.3],
                    },
                }
            ],
            "results": [
                {
                    "object_id": "cue_ball",
                    "status": "failed",
                    "failure": {
                        "code": "unsupported_generation_recipe",
                        "message": "shape is not canonical",
                        "retriable": False,
                    },
                }
            ],
        }
        write_json(attempt_dir / "compilation" / "asset_provider_batch.json", batch)
        provider_failure = failure_stage_result(
            stage="provider",
            failure_code="unsupported_generation_recipe",
            message="shape is not canonical",
            job_id="job_provider_contract_repair",
            attempt_id="attempt_001",
        )
        write_stage_result(attempt_dir, provider_failure)
        manifest = controller.store.load_manifest("job_provider_contract_repair")
        controller._update_manifest(
            manifest,
            state="failed",
            current_stage="compile",
            blocker={
                "code": "unsupported_generation_recipe",
                "message": "shape is not canonical",
                "stage": "provider",
            },
            allowed_next_actions=["inspect_artifacts"],
        )

        inspection = controller.inspect("job_provider_contract_repair")
        repair = inspection["case_spec_contract_repair"]
        self.assertTrue(repair["available"])
        self.assertEqual(
            repair["allowed_adjustments"]["paths"],
            ["$.objects.cue_ball.geometry.shape_hint"],
        )
        revised = copy.deepcopy(invalid)
        revised["objects"][0]["geometry"]["shape_hint"] = "box"

        resumed = controller.resume(
            "job_provider_contract_repair",
            revised_case_spec=revised,
            revision_reason="canonicalize the built-in primitive shape",
        )

        self.assertEqual(resumed["job"]["state"], "awaiting_semantic_review")
        self.assertEqual(resumed["job"]["usage"]["case_spec_revisions"], 2)
        proposal = read_json(attempt_dir / "revision_proposal_001.json")
        self.assertEqual(
            [row["path"] for row in proposal["changes"]],
            ["$.objects.cue_ball.geometry.shape_hint"],
        )

    def test_intent_projection_opens_only_constrained_non_hard_leaf_paths(self) -> None:
        fake = SuccessfulHarness(fail_verifier=True)
        controller, _ = self.controller(fake)
        seed = case_spec_v2_fixture()
        seed["provenance"]["intent_parameter_analysis"] = [
            {
                "path": "$.scene.duration_s",
                "requirement_level": "hard",
                "reason": "the user explicitly fixed duration",
                "constraint": None,
            },
            {
                "path": "$.observation_requirements",
                "requirement_level": "inferred",
                "reason": "invalid subtree authorization must fail closed",
                "constraint": {"kind": "list", "min_items": 1, "max_items": 5},
            },
        ]
        controller.create(
            self.request,
            job_id="job_intent_leaf_scope",
            publication_tier="local_preview",
            seed_case_spec=seed,
        )
        blocked = controller.advance_until_blocked("job_intent_leaf_scope")
        intent = read_json(blocked["paths"]["intent_contract"])
        self.assertEqual(intent["schema_version"], "harness_intent_contract_v2")
        self.assertNotIn("$.scene.duration_s", intent["allowed_adjustments"]["paths"])
        self.assertIn(
            "$.objects.cue_ball.initial_state.position_m",
            intent["allowed_adjustments"]["paths"],
        )
        self.assertIn(
            "$.objects.cue_ball.initial_state.rotation_deg",
            intent["allowed_adjustments"]["paths"],
        )
        revised = copy.deepcopy(seed)
        revised["scene"]["duration_s"] = 2.2
        with self.assertRaisesRegex(ValueError, "allowed_adjustments"):
            controller.resume(
                "job_intent_leaf_scope",
                revised_case_spec=revised,
                revision_reason="hard duration cannot be auto-adjusted",
            )

    def test_numeric_adjustment_without_room_in_explicit_range_fails_closed(self) -> None:
        fake = SuccessfulHarness(fail_verifier=True)
        controller, _ = self.controller(fake)
        seed = case_spec_v2_fixture()
        seed["provenance"]["intent_parameter_analysis"] = [{
            "path": "$.scene.duration_s",
            "requirement_level": "inferred",
            "reason": "bounded inferred duration",
            "constraint": {"kind": "numeric", "min": 1.5, "max": 2.5},
        }]
        controller.create(
            self.request,
            job_id="job_intent_numeric_range",
            publication_tier="local_preview",
            seed_case_spec=seed,
        )
        controller.advance_until_blocked("job_intent_numeric_range")
        revised = copy.deepcopy(seed)
        revised["scene"]["duration_s"] = 2.6
        with self.assertRaisesRegex(ValueError, "allowed range"):
            controller.resume(
                "job_intent_numeric_range",
                revised_case_spec=revised,
                revision_reason="out-of-range duration",
            )

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
        controller.create(self.request, job_id="job_ambiguity_identity", publication_tier="local_preview", generation_mode="legacy")
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
        completed = controller.run_semantic_review("job_ambiguity_identity")
        self.assertEqual(completed["job"]["state"], "completed")
        attempt = Path(completed["paths"]["job_root"]) / "attempts" / "attempt_001"
        review = read_json(attempt / "semantic_review.json")
        self.assertEqual(
            {row["requirement_id"] for row in review["requirements"]},
            {"original_user_request", f"ambiguity_decision_{stable_digest(ambiguity_id)[:16]}"},
        )

    def test_semantic_pass_cannot_omit_ambiguity_decision_requirement(self) -> None:
        fake = SuccessfulHarness()

        def ambiguous(request: dict, *, artifact_dir: Path, job_id: str, attempt_id: str):
            generated = fake.generate(request, artifact_dir=artifact_dir, job_id=job_id, attempt_id=attempt_id)
            generated.expansion["ambiguities"] = [{"question": "which object should move?"}]
            write_json(artifact_dir / "expansion.json", generated.expansion)
            return generated

        hooks = fake.hooks()
        hooks.generate = ambiguous
        successful_review = hooks.semantic_review

        def incomplete_review(**kwargs):
            result = successful_review(**kwargs)
            result["review"]["requirements"] = [
                row
                for row in result["review"]["requirements"]
                if row["requirement_id"] == "original_user_request"
            ]
            result["receipt"]["output_digest"] = stable_digest(result["review"])
            return result

        hooks.semantic_review = incomplete_review
        controller = AgentJobController(self.workspace, hooks=hooks)
        controller.create(
            self.request,
            job_id="job_ambiguity_review_coverage",
            budget={"max_reviewer_technical_retries": 0},
            publication_tier="local_preview",
            generation_mode="legacy",
        )
        blocked = controller.advance_until_blocked("job_ambiguity_review_coverage")
        ambiguity_id = read_json(blocked["paths"]["intent_contract"])["ambiguities"][0]["ambiguity_id"]
        controller.resume(
            "job_ambiguity_review_coverage",
            intent_amendment={"ambiguity_resolutions": [{"ambiguity_id": ambiguity_id, "decision": "cue_ball"}]},
        )

        failed = controller.run_semantic_review("job_ambiguity_review_coverage")

        self.assertEqual(failed["job"]["state"], "failed")
        self.assertEqual(failed["job"]["blocker"]["code"], "reviewer_output_schema_invalid")
        attempt = Path(failed["paths"]["job_root"]) / "attempts" / "attempt_001"
        self.assertFalse((attempt / "semantic_review.json").exists())
        rejected = read_json(attempt / "reviewer_output_rejected_001.json")
        self.assertEqual(rejected["schema_version"], "harness_rejected_reviewer_output_v1")
        self.assertEqual(rejected["failure_code"], "reviewer_output_schema_invalid")
        self.assertEqual(rejected["invocation_count"], 1)
        self.assertEqual(
            failed["current_leaf_stage_result"]["result"]["artifact_refs"][0]["path"],
            str(attempt / "reviewer_output_rejected_001.json"),
        )

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
        self.assertIn("--allow-planning-image-upload", completed.stdout)
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
        self.assertIn("recover-interrupted", completed.stdout)
        self.assertIn("retry-failed", completed.stdout)
        self.assertIn("recompile-after-config", completed.stdout)

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
