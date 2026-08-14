from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from harness.agent.job_schema import (
    ATTEMPT_MANIFEST_SCHEMA_VERSION,
    INTENT_CONTRACT_SCHEMA_VERSION,
    PROJECTED_INTENT_CONTRACT_SCHEMA_VERSION,
    JOB_MANIFEST_SCHEMA_VERSION,
    SMOKE_GATE_SCHEMA_VERSION,
    AttemptManifest,
    IntentContract,
    JobManifest,
    checkpoint_payload,
    empty_usage,
    normalized_budget,
    stable_digest,
    utc_now,
    validate_attempt_id,
    validate_job_id,
)
from harness.agent.native_generation import (
    NATIVE_GENERATION_ACK_SCHEMA_VERSION,
    build_native_generation_ack,
    build_native_generation_context,
    generation_policy,
    validate_generation_policy,
    validate_native_generation_ack,
    validate_native_generation_context,
    validate_native_generation_submission,
)
from harness.agent.job_store import JobStore, JobStoreError
from harness.agent.evidence_bundle import (
    EvidenceBundleError,
    build_evidence_bundle,
    current_evidence_snapshots,
    semantic_review_requirements,
    validate_current_evidence_bundle,
)
from harness.agent.review_schema import (
    REVISION_PROPOSAL_SCHEMA_VERSION,
    REVIEWER_INVOCATION_SCHEMA_VERSION,
    SEMANTIC_REVIEW_SCHEMA_VERSION,
    EvidenceBundleManifest,
    ReviewerInvocationReceipt,
    ReviewerInvocationReservation,
    RevisionProposal,
    SemanticReview,
)
from harness.agent.semantic_reviewer import (
    CodexAppServerReviewer,
    SemanticReviewerError,
    reviewer_permission_profile,
    semantic_reviewer_input_digest,
)
from harness.assets.asset_registry import AssetRegistry
from harness.assets.providers.orchestrator import AssetProviderOrchestrator
from harness.assets.providers.input_manifest import PROVIDER_INPUT_MANIFEST_SCHEMA, build_provider_input_manifest
from harness.assets.providers.remote import MeshyModelGenerationAdapter, PolyHavenExternalSiteAdapter
from harness.core.artifact_schema import read_json, write_json
from harness.core.harness_config import EffectiveHarnessConfig, load_harness_config
from harness.core.case_spec_v2 import (
    CaseSpecV2,
    asset_requests,
    case_spec_v2_from_dict,
    compile_case_spec_v2_runtime,
)
from harness.core.stage_result import (
    StageResult,
    artifact_ref,
    build_stage_result,
    classify_failure,
    failure_stage_result,
    write_stage_result,
)
from harness.planning.backend_planner import plan_backend
from harness.planning.case_generation import (
    REQUEST_SCHEMA_VERSION,
    apply_case_request_identity,
    generate_case_spec_v2,
    normalize_planning_image_requirement,
    planning_image_decision,
)
from harness.planning.runtime_compiler import RuntimeCompilation, compile_runtime_case
from harness.runtime.execution_profile import execution_profile, verified_run_status, write_execution_reports
from harness.runtime.stage_executor import execute_runtime_plan
from harness.verification.physics_verifier import PhysicsVerifier
from harness.verification.render_sync_checker import check_render_sync
from harness.verification.run_quality import evaluate_run


EventSink = Callable[[dict[str, Any]], None]

@dataclass
class ControllerHooks:
    generate: Callable[..., Any] = generate_case_spec_v2
    compile: Callable[..., RuntimeCompilation] = compile_runtime_case
    execute: Callable[..., Path] = execute_runtime_plan
    verify: Callable[[Path], Mapping[str, Any]] = lambda run_dir: PhysicsVerifier().verify_run_dir(run_dir, write=True)
    render_sync: Callable[..., Mapping[str, Any]] = check_render_sync
    quality: Callable[[Path], Mapping[str, Any]] = lambda run_dir: evaluate_run(run_dir, write=True)
    evidence: Callable[..., Mapping[str, Any]] = build_evidence_bundle
    semantic_review: Callable[..., Mapping[str, Any]] = lambda **kwargs: CodexAppServerReviewer().review(**kwargs)
    monotonic: Callable[[], float] = time.monotonic


class AgentJobController:
    """Durable single-job control plane.

    It selects and resumes Harness stages from structured artifacts. It never
    edits compiler outputs or treats free-form exception text as control data.
    """

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        hooks: ControllerHooks | None = None,
        event_sink: EventSink | None = None,
        config: EffectiveHarnessConfig | None = None,
    ) -> None:
        self.config = config or load_harness_config(
            cli_overrides={"paths.workspace": str(workspace)} if workspace is not None else None
        )
        if workspace is not None and Path(workspace).expanduser().resolve(strict=False) != self.config.workspace:
            raise ValueError("Controller workspace must match the effective Harness configuration")
        self.store = JobStore(self.config.workspace)
        if hooks is None:
            reviewer_executable = str(self.config.codex_executable) if self.config.codex_executable is not None else None
            hooks = ControllerHooks(
                semantic_review=lambda **kwargs: CodexAppServerReviewer(executable=reviewer_executable).review(**kwargs)
            )
        self.hooks = hooks
        self.event_sink = event_sink
        self._compilations: dict[tuple[str, str, str], RuntimeCompilation] = {}

    def create(
        self,
        request: Mapping[str, Any],
        *,
        provider_input_manifest: Mapping[str, Any] | None = None,
        job_id: str | None = None,
        budget: Mapping[str, Any] | None = None,
        authorizations: Mapping[str, Any] | None = None,
        publication_tier: str = "reference",
        seed_case_spec: Mapping[str, Any] | None = None,
        generation_mode: str = "native",
    ) -> dict[str, Any]:
        request_data = self._validate_request(request)
        identity = validate_job_id(job_id or self._new_job_id())
        image_inputs = [row for row in request_data.get("inputs") or [] if row.get("kind") == "image"]
        auth = {
            "planning_llm_upload": bool(image_inputs) and all(
                row.get("external_upload_authorized") is True for row in image_inputs
            ),
            "meshy_upload": False,
            "external_provider": False,
            "paid_provider_submission": False,
            "semantic_reviewer_image_upload": False,
        }
        for key, value in dict(authorizations or {}).items():
            if key not in auth or not isinstance(value, bool):
                raise ValueError(f"unsupported or invalid authorization: {key}")
            auth[key] = value
        if image_inputs and auth["planning_llm_upload"] != all(
            row.get("external_upload_authorized") is True for row in image_inputs
        ):
            raise ValueError("planning_llm_upload authorization must match every immutable request input record")
        normalized = normalized_budget(budget)
        if auth["paid_provider_submission"] and normalized["max_paid_submissions"] == 0:
            raise ValueError("paid provider authorization requires max_paid_submissions > 0")
        if publication_tier not in {"diagnostic_only", "local_preview", "reference"}:
            raise ValueError("publication_tier is invalid")
        provider_manifest = (
            self._validate_provider_manifest(provider_input_manifest)
            if provider_input_manifest is not None
            else None
        )
        if provider_manifest is not None:
            meshy_flags = [
                (row.get("authorizations") or {}).get("meshy_upload") is True
                for row in provider_manifest.get("inputs") or []
            ]
            if meshy_flags and auth["meshy_upload"] != all(meshy_flags):
                raise ValueError("meshy_upload authorization must match every Provider input manifest record")
        validated_seed = None
        if seed_case_spec is not None:
            available = [str(row.get("input_id")) for row in request_data.get("inputs") or []]
            validated_seed = case_spec_v2_from_dict(seed_case_spec, available_input_ids=available)
        mode = "seed" if validated_seed is not None else str(generation_mode).strip()
        if validated_seed is not None and generation_mode not in {"native", "seed"}:
            raise ValueError("seed_case_spec cannot be combined with legacy generation mode")
        policy = generation_policy(mode)
        now = utc_now()
        manifest = JobManifest.from_dict(
            {
                "schema_version": JOB_MANIFEST_SCHEMA_VERSION,
                "job_id": identity,
                "state": "created",
                "current_stage": "intake_readiness",
                "current_attempt_id": None,
                "active_compilation_id": None,
                "request_digest": stable_digest(request_data),
                "intent_contract_digest": None,
                "target": {"execution_profile": "candidate", "publication_tier": publication_tier},
                "authorizations": auth,
                "budget": normalized,
                "usage": empty_usage(),
                "blocker": None,
                "allowed_next_actions": ["advance", "cancel"],
                "created_at": now,
                "updated_at": now,
            }
        ).to_dict()
        root = self.store.create(manifest)
        self.store.write_request_artifact(identity, "user_request.json", request_data)
        if provider_manifest is not None:
            self.store.write_request_artifact(identity, "provider_input_manifest.json", provider_manifest)
        if validated_seed is not None:
            self.store.write_request_artifact(identity, "seed_case_spec.json", validated_seed.data)
        self.store.write_request_artifact(identity, "generation_policy.json", policy)
        self._emit(identity, "job_created", stage="intake_readiness", artifact_refs=[str(root / "job_manifest.json")])
        return self.inspect(identity)

    def inspect(self, job_id: str) -> dict[str, Any]:
        manifest = self.store.load_manifest(job_id)
        root = self.store.job_dir(job_id)
        context_path = root / "request" / "native_generation_context.json"
        attempts = []
        for path in sorted((root / "attempts").glob("attempt_*/attempt_manifest.json")):
            attempts.append(AttemptManifest.from_dict(read_json(path)).to_dict())
        return {
            "schema_version": "harness_agent_job_inspection_v1",
            "effective_config_digest": self.config.digest,
            "generation_mode": self._generation_policy(job_id)["mode"],
            "native_generation_context_digest": stable_digest(read_json(context_path)) if context_path.is_file() else None,
            "job": manifest,
            "attempts": attempts,
            "paths": {
                "job_root": str(root),
                "job_manifest": str(root / "job_manifest.json"),
                "intent_contract": str(root / "request" / "intent_contract.json") if (root / "request" / "intent_contract.json").is_file() else None,
                "native_generation_context": str(root / "request" / "native_generation_context.json") if (root / "request" / "native_generation_context.json").is_file() else None,
                "native_generation_ack": str(root / "request" / "native_generation_ack.json") if (root / "request" / "native_generation_ack.json").is_file() else None,
            },
        }

    def submit_native_generation(self, job_id: str, submission: Mapping[str, Any]) -> dict[str, Any]:
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            if manifest["current_stage"] != "generation":
                raise JobStoreError("native generation submission requires the generation stage")
            policy = self._generation_policy(job_id)
            if policy["mode"] != "native":
                raise JobStoreError("job is not configured for native generation")
            root = self.store.job_dir(job_id) / "request"
            context_path = root / "native_generation_context.json"
            if not context_path.is_file():
                raise JobStoreError("advance the job once to create the native generation context")
            context = validate_native_generation_context(read_json(context_path))
            request = read_json(root / "user_request.json")
            self._validate_native_context_binding(manifest, context, request)
            value = validate_native_generation_submission(submission, context=context)
            case_spec = self._native_case_spec(request, value["case_spec"])
            self._project_native_intent_contract(manifest, request, value["intent_draft"], case_spec.data)
            submission_path = root / "native_generation_submission.json"
            ack_path = root / "native_generation_ack.json"
            if submission_path.is_file():
                existing = read_json(submission_path)
                if existing != value:
                    raise JobStoreError("an immutable native generation submission already differs")
            else:
                write_json(submission_path, value)
            if ack_path.is_file():
                validate_native_generation_ack(read_json(ack_path), context=context, submission=value)
            else:
                write_json(ack_path, build_native_generation_ack(context=context, submission=value))
            self._update_manifest(
                manifest,
                state="running",
                blocker=None,
                allowed_next_actions=["advance", "cancel"],
            )
        return self.inspect(job_id)

    def advance_until_blocked(self, job_id: str) -> dict[str, Any]:
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            if manifest["state"] in {"cancelled", "completed", "failed", "awaiting_semantic_review"}:
                return self.inspect(job_id)
            manifest = self._update_manifest(
                manifest,
                state="running",
                blocker=None,
                allowed_next_actions=["cancel"],
            )
            while manifest["state"] == "running":
                budget_result = self._budget_gate(manifest)
                if budget_result is not None:
                    manifest = self._apply_stage_result(manifest, budget_result)
                    break
                stage = str(manifest["current_stage"])
                started = self.hooks.monotonic()
                stage_result_snapshot = self._stage_result_snapshot(manifest)
                self._stage_event(manifest, "stage_started", stage)
                try:
                    with self._effective_environment():
                        manifest = self._advance_one(manifest)
                except (KeyboardInterrupt, SystemExit) as exc:
                    if stage == "compile":
                        manifest = self._reconcile_provider_usage(manifest)
                    elapsed = max(0.0, self.hooks.monotonic() - started)
                    manifest = self._add_active_elapsed(manifest, elapsed)
                    self._checkpoint(
                        manifest,
                        stage,
                        "interrupted",
                        self._stage_input_digest(manifest, stage),
                    )
                    result = self._exception_stage_result(
                        manifest,
                        stage,
                        exc,
                        stage_result_snapshot=stage_result_snapshot,
                    )
                    if result["failure_class"] != "interrupted":
                        result = failure_stage_result(
                            stage=result["stage"],
                            failure_code="interrupted",
                            message=f"{result['stage']} was interrupted at a safe controller boundary",
                            source_status="interrupted",
                            job_id=job_id,
                            attempt_id=manifest.get("current_attempt_id"),
                            checkpoint_ref=str(self.store.job_dir(job_id) / "checkpoints" / f"{stage}.json"),
                        )
                    self._write_controller_stage_result(manifest, result)
                    manifest = self._apply_stage_result(manifest, result)
                    self._stage_event(manifest, "stage_blocked", stage, result=result)
                    break
                except BaseException as exc:
                    if stage == "compile":
                        manifest = self._reconcile_provider_usage(manifest)
                    elapsed = max(0.0, self.hooks.monotonic() - started)
                    manifest = self._add_active_elapsed(manifest, elapsed)
                    result = self._exception_stage_result(
                        manifest,
                        stage,
                        exc,
                        stage_result_snapshot=stage_result_snapshot,
                    )
                    self._write_controller_stage_result(manifest, result)
                    manifest = self._apply_stage_result(manifest, result)
                    self._stage_event(manifest, "stage_blocked", stage, result=result)
                    if manifest["state"] == "running":
                        continue
                    break
                elapsed = max(0.0, self.hooks.monotonic() - started)
                manifest = self._add_active_elapsed(manifest, elapsed)
                self._stage_event(manifest, "stage_completed", stage)
                manifest = self.store.load_manifest(job_id)
            if manifest["state"] in {"failed", "cancelled", "completed", "awaiting_semantic_review"}:
                self._emit(job_id, "job_terminal", stage=manifest["current_stage"], state=manifest["state"])
            return self.inspect(job_id)

    def resume(
        self,
        job_id: str,
        *,
        budget_extension_seconds: int = 0,
        max_paid_submissions: int | None = None,
        authorizations: Mapping[str, Any] | None = None,
        intent_amendment: Mapping[str, Any] | None = None,
        revised_case_spec: Mapping[str, Any] | None = None,
        revision_reason: str | None = None,
    ) -> dict[str, Any]:
        resume_to_review_boundary = False
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            if manifest["state"] not in {"blocked", "needs_user_decision", "paused_interrupted", "failed"}:
                raise JobStoreError(f"job cannot be resumed from state {manifest['state']}")
            if manifest["state"] == "failed" and (manifest.get("blocker") or {}).get("code") != "budget_exhausted":
                raise JobStoreError("only budget-exhausted failed jobs may be resumed")
            requested_action = "resume_with_revision" if revised_case_spec is not None else "resume"
            if requested_action not in manifest["allowed_next_actions"]:
                raise JobStoreError(f"current job state does not permit {requested_action}")
            prior_authorizations = copy.deepcopy(manifest["authorizations"])
            prior_budget = copy.deepcopy(manifest["budget"])
            if budget_extension_seconds:
                if not isinstance(budget_extension_seconds, int) or budget_extension_seconds < 1:
                    raise ValueError("budget_extension_seconds must be a positive integer")
                budget = dict(manifest["budget"])
                budget["hard_deadline_seconds"] += budget_extension_seconds
                budget["soft_deadline_seconds"] += budget_extension_seconds
                manifest["budget"] = normalized_budget(budget)
            if max_paid_submissions is not None:
                if (
                    not isinstance(max_paid_submissions, int)
                    or isinstance(max_paid_submissions, bool)
                    or max_paid_submissions < manifest["budget"]["max_paid_submissions"]
                ):
                    raise ValueError("max_paid_submissions may only increase the current non-negative limit")
                budget = dict(manifest["budget"])
                budget["max_paid_submissions"] = max_paid_submissions
                manifest["budget"] = normalized_budget(budget)
            if authorizations:
                current = dict(manifest["authorizations"])
                for key, value in authorizations.items():
                    if key not in current or not isinstance(value, bool):
                        raise ValueError(f"unsupported or invalid authorization: {key}")
                    current[key] = value
                if current["paid_provider_submission"] and manifest["budget"]["max_paid_submissions"] == 0:
                    raise ValueError("paid provider authorization requires a positive paid submission budget")
                manifest["authorizations"] = current
            if (
                manifest["authorizations"] != prior_authorizations
                or manifest["budget"]["max_paid_submissions"] != prior_budget["max_paid_submissions"]
            ):
                manifest = self._record_authorization_amendment(
                    manifest,
                    prior_authorizations=prior_authorizations,
                    prior_budget=prior_budget,
                )
            if (manifest.get("blocker") or {}).get("code") == "intent_ambiguity_requires_decision" and intent_amendment is None:
                raise JobStoreError("resolving an Intent Contract ambiguity requires an intent_amendment")
            if intent_amendment is not None:
                manifest = self._apply_intent_amendment(manifest, intent_amendment)
            if revised_case_spec is not None:
                manifest = self._create_revision(
                    manifest,
                    revised_case_spec,
                    revision_reason or "user-approved revision",
                    repair_layer="case_spec_source",
                    trigger_stage=str((manifest.get("blocker") or {}).get("stage") or manifest["current_stage"]),
                    trigger_failure_code=str((manifest.get("blocker") or {}).get("code") or "user_approved_revision"),
                )
            if manifest["current_stage"] == "semantic_review" and revised_case_spec is None:
                attempt_dir = self.store.attempt_dir(job_id, manifest["current_attempt_id"])
                review_path = attempt_dir / "semantic_review.json"
                if review_path.exists():
                    raise JobStoreError("a completed semantic judgment cannot be resampled in the same attempt")
                bundle_dir = attempt_dir / "evidence_bundle"
                bundle_manifest_path = bundle_dir / "manifest.json"
                bundle_intent_digest = (
                    str(read_json(bundle_manifest_path).get("intent_contract_digest") or "")
                    if bundle_manifest_path.is_file()
                    else ""
                )
                if bundle_intent_digest != manifest["intent_contract_digest"]:
                    sequence = len(list(attempt_dir.glob("evidence_bundle_superseded_*"))) + 1
                    bundle_dir.replace(attempt_dir / f"evidence_bundle_superseded_{sequence:03d}")
                    manifest = self._update_manifest(
                        manifest,
                        state="running",
                        current_stage="evidence_bundle",
                        blocker=None,
                        allowed_next_actions=["cancel"],
                    )
                else:
                    manifest = self._update_manifest(
                        manifest,
                        state="awaiting_semantic_review",
                        blocker=None,
                        allowed_next_actions=["run_semantic_review", "cancel"],
                    )
                    resume_to_review_boundary = True
            else:
                manifest = self._update_manifest(manifest, state="running", blocker=None, allowed_next_actions=["cancel"])
        if resume_to_review_boundary:
            return self.inspect(job_id)
        return self.advance_until_blocked(job_id)

    def run_semantic_review(self, job_id: str) -> dict[str, Any]:
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            if manifest["state"] not in {"awaiting_semantic_review", "running"} or manifest["current_stage"] != "semantic_review":
                raise JobStoreError("semantic review requires awaiting_semantic_review at the explicit review boundary")
            manifest = self._reconcile_reviewer_usage(manifest)
            budget_result = self._budget_gate(manifest)
            if budget_result is not None:
                self._write_controller_stage_result(manifest, budget_result)
                manifest = self._apply_stage_result(manifest, budget_result)
                self._stage_event(manifest, "stage_blocked", "semantic_review", result=budget_result)
                return self.inspect(job_id)
            self._stage_event(manifest, "stage_started", "semantic_review")
            attempt_id = str(manifest["current_attempt_id"])
            attempt_dir = self.store.attempt_dir(job_id, attempt_id)
            attempt = self.store.load_attempt(job_id, attempt_id)
            bundle_path = attempt_dir / "evidence_bundle" / "manifest.json"
            if not bundle_path.is_file():
                raise JobStoreError("formal semantic review requires a completed Evidence Bundle")
            review_path = attempt_dir / "semantic_review.json"
            if review_path.exists():
                raise JobStoreError("the current attempt already has a formal semantic judgment")
            try:
                bundle = self._validated_current_bundle(manifest)
            except EvidenceBundleError as exc:
                failure_code = (
                    "semantic_review_technical_gate_stale"
                    if exc.code == "evidence_technical_gate_identity_mismatch"
                    else exc.code
                )
                result = failure_stage_result(
                    stage="semantic_review",
                    failure_code=failure_code,
                    message=str(exc),
                    job_id=job_id,
                    attempt_id=attempt_id,
                )
                self._write_controller_stage_result(manifest, result)
                manifest = self._apply_stage_result(manifest, result)
                self._stage_event(manifest, "stage_blocked", "semantic_review", result=result)
                return self.inspect(job_id)
            bundle_digest = stable_digest(bundle)
            if not self._technical_completion_intact(manifest, bundle):
                result = failure_stage_result(
                    stage="semantic_review",
                    failure_code="semantic_review_technical_gate_stale",
                    message="Candidate technical gates or Evidence Bundle identity changed before review",
                    job_id=job_id,
                    attempt_id=attempt_id,
                )
                self._write_controller_stage_result(manifest, result)
                self._checkpoint(manifest, "semantic_review", "failed", stable_digest(bundle), [str(bundle_path)])
                manifest = self._apply_stage_result(manifest, result)
                self._stage_event(manifest, "stage_blocked", "semantic_review", result=result)
                self._emit(job_id, "job_terminal", stage="semantic_review", state=manifest["state"])
                return self.inspect(job_id)
            request = read_json(self.store.job_dir(job_id) / "request" / "user_request.json")
            has_images = any(
                isinstance(row, Mapping) and row.get("kind") == "image"
                for row in request.get("inputs") or []
            )
            if has_images and manifest["authorizations"]["semantic_reviewer_image_upload"] is not True:
                result = failure_stage_result(
                    stage="semantic_review",
                    failure_code="semantic_reviewer_image_upload_authorization_missing",
                    message="Semantic Reviewer image upload requires its own explicit authorization",
                    source_status="blocked",
                    job_id=job_id,
                    attempt_id=attempt_id,
                )
                self._write_controller_stage_result(manifest, result)
                self._checkpoint(manifest, "semantic_review", "blocked", stable_digest(bundle), [str(bundle_path)])
                manifest = self._apply_stage_result(manifest, result)
                self._stage_event(manifest, "stage_blocked", "semantic_review", result=result)
                self._emit(job_id, "job_terminal", stage="semantic_review", state=manifest["state"])
                return self.inspect(job_id)

            expected_input_digest = semantic_reviewer_input_digest(
                bundle_dir=attempt_dir / "evidence_bundle",
                bundle_manifest=bundle,
                include_original_images=has_images,
            )
            reservations = self._reviewer_reservations(
                attempt_dir,
                job_id=job_id,
                attempt_id=attempt_id,
                bundle_digest=bundle_digest,
                input_digest=expected_input_digest,
            )
            manifest = self._reconcile_reviewer_usage(manifest)
            while True:
                reservation = self._next_reviewer_reservation(
                    manifest,
                    attempt_dir=attempt_dir,
                    attempt_id=attempt_id,
                    bundle_digest=bundle_digest,
                    input_digest=expected_input_digest,
                    reservations=reservations,
                )
                if reservation is None:
                    break
                invocation_count = int(reservation["invocation_count"])
                receipt_path = attempt_dir / f"reviewer_invocation_{invocation_count:03d}.json"
                written_review = False
                technical_reservations = sum(row["role"] == "technical_retry" for row in reservations)
                recorded_technical_retries = int(manifest["usage"]["stage_retries"].get("semantic_review", 0))
                if reservation["role"] == "technical_retry" and technical_reservations > recorded_technical_retries:
                    usage = copy.deepcopy(manifest["usage"])
                    usage["stage_retries"]["semantic_review"] = recorded_technical_retries + 1
                    usage["total_retries"] += 1
                    manifest = self._update_manifest(manifest, usage=usage)
                manifest = self._update_manifest(manifest, state="running", allowed_next_actions=["cancel"])

                def lifecycle(state: str) -> None:
                    nonlocal manifest, reservation
                    changes: dict[str, Any] = {"state": state}
                    if state == "started":
                        changes["usage_counted"] = True
                    reservation = self._update_reviewer_reservation(attempt_dir, reservation, **changes)
                    if state == "started":
                        manifest = self._reconcile_reviewer_usage(manifest)

                reviewer_started = self.hooks.monotonic()
                reviewer_elapsed_recorded = False
                try:
                    invocation = dict(
                        self.hooks.semantic_review(
                            job_id=job_id,
                            attempt_id=attempt_id,
                            bundle_dir=attempt_dir / "evidence_bundle",
                            bundle_manifest=bundle,
                            invocation_count=invocation_count,
                            include_original_images=has_images,
                            lifecycle_callback=lifecycle,
                        )
                    )
                    if not reservation["usage_counted"]:
                        reservation = self._update_reviewer_reservation(
                            attempt_dir,
                            reservation,
                            state="output_received",
                            usage_counted=True,
                        )
                        manifest = self._reconcile_reviewer_usage(manifest)
                    elapsed = max(0.0, self.hooks.monotonic() - reviewer_started)
                    manifest = self._add_active_elapsed(manifest, elapsed)
                    reviewer_elapsed_recorded = True
                    raw_review = dict(invocation["review"])
                    if set(raw_review) != {
                        "overall_status",
                        "requirements",
                        "repair_layer",
                        "summary",
                        "suggested_adjustments",
                    }:
                        raise JobStoreError("Reviewer output must contain only the semantic judgment body")
                    receipt = self._validate_reviewer_receipt(
                        invocation["receipt"],
                        job_id=job_id,
                        attempt_id=attempt_id,
                        invocation_count=invocation_count,
                        bundle_dir=attempt_dir / "evidence_bundle",
                        expected_input_digest=expected_input_digest,
                        expected_output_digest=stable_digest(raw_review),
                        require_completed=True,
                    )
                    write_json(receipt_path, receipt)
                    reservation = self._update_reviewer_reservation(
                        attempt_dir,
                        reservation,
                        state="receipt_recorded",
                        outcome="completed",
                        retryable=False,
                        receipt_path=receipt_path.name,
                        error_code=None,
                    )
                    intent = IntentContract.from_dict(
                        read_json(self.store.job_dir(job_id) / "request" / "intent_contract.json")
                    ).to_dict()
                    current_bundle = self._validated_current_bundle(
                        manifest,
                        expected_manifest_digest=bundle_digest,
                    )
                    expected_requirements = self._semantic_review_requirements(job_id, intent)
                    review = SemanticReview.from_dict(
                        {
                            "schema_version": SEMANTIC_REVIEW_SCHEMA_VERSION,
                            "job_id": job_id,
                            "attempt_id": attempt_id,
                            "evidence_bundle_digest": bundle_digest,
                            "reviewer_receipt_digest": stable_digest(receipt),
                            **raw_review,
                            "created_at": utc_now(),
                        },
                        expected_requirement_ids={str(row["id"]) for row in expected_requirements},
                        evidence_artifact_ids={str(row["artifact_id"]) for row in current_bundle["artifacts"]},
                        evidence_manifest=current_bundle,
                    ).to_dict()
                    if not self._technical_completion_intact(manifest, current_bundle):
                        raise EvidenceBundleError(
                            "semantic_review_technical_gate_stale",
                            "Candidate technical gates changed while Semantic Review was running",
                        )
                    write_json(review_path, review)
                    written_review = True
                    review = self._load_validated_semantic_review(manifest)
                    stage_result = build_stage_result(
                        stage="semantic_review",
                        status="completed",
                        job_id=job_id,
                        attempt_id=attempt_id,
                        invocation_count=invocation_count,
                        artifact_refs=[
                            {"name": "semantic_review", "path": str(review_path), "schema_version": SEMANTIC_REVIEW_SCHEMA_VERSION},
                            {"name": "reviewer_receipt", "path": str(receipt_path), "schema_version": receipt["schema_version"]},
                        ],
                    )
                    write_stage_result(attempt_dir, stage_result)
                    self._checkpoint(
                        manifest,
                        "semantic_review",
                        "completed",
                        stable_digest(review),
                        [str(review_path), str(receipt_path)],
                    )
                    manifest = self._apply_semantic_outcome(manifest, attempt, intent, review)
                    self._stage_event(manifest, "stage_completed", "semantic_review")
                    self._emit(job_id, "job_terminal", stage=manifest["current_stage"], state=manifest["state"])
                    return self.inspect(job_id)
                except SemanticReviewerError as exc:
                    try:
                        receipt = self._validate_reviewer_receipt(
                            exc.receipt,
                            job_id=job_id,
                            attempt_id=attempt_id,
                            invocation_count=invocation_count,
                            bundle_dir=attempt_dir / "evidence_bundle",
                            expected_input_digest=expected_input_digest,
                            expected_output_digest=None,
                            require_completed=False,
                        )
                        write_json(receipt_path, receipt)
                        artifact_refs = [
                            {"name": "reviewer_receipt", "path": str(receipt_path), "schema_version": receipt["schema_version"]}
                        ]
                    except (ValueError, JobStoreError) as receipt_error:
                        result = failure_stage_result(
                            stage="semantic_review",
                            failure_code="reviewer_invocation_identity_invalid",
                            message=str(receipt_error),
                            retryable=False,
                            job_id=job_id,
                            attempt_id=attempt_id,
                            invocation_count=invocation_count,
                        )
                    else:
                        result = failure_stage_result(
                            stage="semantic_review",
                            failure_code=exc.code,
                            message=str(exc),
                            retryable=exc.retryable,
                            source_status="interrupted" if exc.code == "reviewer_interrupted" else None,
                            job_id=job_id,
                            attempt_id=attempt_id,
                            invocation_count=invocation_count,
                            artifact_refs=artifact_refs,
                        )
                except EvidenceBundleError as exc:
                    result = failure_stage_result(
                        stage="semantic_review",
                        failure_code=exc.code,
                        message=str(exc),
                        retryable=False,
                        job_id=job_id,
                        attempt_id=attempt_id,
                        invocation_count=invocation_count,
                    )
                except JobStoreError as exc:
                    result = failure_stage_result(
                        stage="semantic_review",
                        failure_code="reviewer_invocation_identity_invalid",
                        message=str(exc),
                        retryable=False,
                        job_id=job_id,
                        attempt_id=attempt_id,
                        invocation_count=invocation_count,
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    result = failure_stage_result(
                        stage="semantic_review",
                        failure_code="reviewer_output_schema_invalid",
                        message=str(exc),
                        retryable=True,
                        job_id=job_id,
                        attempt_id=attempt_id,
                        invocation_count=invocation_count,
                    )
                finally:
                    if not reviewer_elapsed_recorded:
                        elapsed = max(0.0, self.hooks.monotonic() - reviewer_started)
                        manifest = self._add_active_elapsed(manifest, elapsed)
                if written_review:
                    review_path.unlink(missing_ok=True)
                write_stage_result(attempt_dir, result)
                outcome = (
                    "interrupted"
                    if result["failure_class"] == "interrupted"
                    else "blocked_configuration"
                    if result["failure_class"] == "blocked_configuration"
                    else "technical_failed"
                )
                reservation = self._update_reviewer_reservation(
                    attempt_dir,
                    reservation,
                    state="receipt_recorded" if receipt_path.is_file() else reservation["state"],
                    outcome=outcome,
                    retryable=bool(result["retryable"]),
                    receipt_path=receipt_path.name if receipt_path.is_file() else None,
                    error_code=result["failure_code"],
                )
                reservations[-1] = reservation
                if result["failure_class"] == "interrupted":
                    self._checkpoint(manifest, "semantic_review", "interrupted", stable_digest(bundle), [str(bundle_path), str(receipt_path)])
                    manifest = self._apply_stage_result(manifest, result)
                    self._stage_event(manifest, "stage_blocked", "semantic_review", result=result)
                    return self.inspect(job_id)
                retry_budget_result = self._budget_gate(manifest) if result["retryable"] else None
                launched_count = sum(bool(row["usage_counted"]) for row in reservations)
                primary_limit = min(1, int(manifest["budget"]["max_reviewer_invocations"]))
                launch_limit = (
                    primary_limit + int(manifest["budget"]["max_reviewer_technical_retries"])
                    if primary_limit
                    else 0
                )
                can_retry = (
                    result["retryable"]
                    and launched_count < launch_limit
                    and sum(row["role"] == "technical_retry" for row in reservations)
                    < int(manifest["budget"]["max_reviewer_technical_retries"])
                    and manifest["usage"]["total_retries"] < manifest["budget"]["max_total_retries"]
                    and retry_budget_result is None
                )
                if can_retry:
                    usage = copy.deepcopy(manifest["usage"])
                    usage["stage_retries"]["semantic_review"] = int(
                        usage["stage_retries"].get("semantic_review", 0)
                    ) + 1
                    usage["total_retries"] += 1
                    manifest = self._update_manifest(manifest, usage=usage)
                    continue
                if retry_budget_result is not None:
                    self._write_controller_stage_result(manifest, retry_budget_result)
                    manifest = self._apply_stage_result(manifest, retry_budget_result)
                    self._stage_event(manifest, "stage_blocked", "semantic_review", result=retry_budget_result)
                    return self.inspect(job_id)
                self._checkpoint(manifest, "semantic_review", "failed", stable_digest(bundle), [str(bundle_path)])
                exhausted = dict(result)
                exhausted["retryable"] = False
                exhausted["allowed_next_actions"] = ["inspect_artifacts"]
                exhausted = StageResult.from_dict(exhausted).to_dict()
                manifest = self._apply_stage_result(manifest, exhausted)
                self._stage_event(manifest, "stage_blocked", "semantic_review", result=exhausted)
                self._emit(job_id, "job_terminal", stage="semantic_review", state=manifest["state"])
                return self.inspect(job_id)
            result = failure_stage_result(
                stage="semantic_review",
                failure_code="reviewer_invocation_budget_exhausted",
                message="Semantic Reviewer invocation budget is exhausted",
                source_status="blocked",
                job_id=job_id,
                attempt_id=attempt_id,
            )
            self._write_controller_stage_result(manifest, result)
            manifest = self._apply_stage_result(manifest, result)
            self._stage_event(manifest, "stage_blocked", "semantic_review", result=result)
            self._emit(job_id, "job_terminal", stage="semantic_review", state=manifest["state"])
            return self.inspect(job_id)

    def apply_revision_proposal(
        self,
        job_id: str,
        revised_case_spec: Mapping[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            if "apply_revision_proposal" not in manifest["allowed_next_actions"]:
                raise JobStoreError("current job state does not permit an automatic revision proposal")
            attempt_dir = self.store.attempt_dir(job_id, manifest["current_attempt_id"])
            review = self._load_validated_semantic_review(manifest)
            if review["overall_status"] != "fail":
                raise JobStoreError("automatic revision proposals require a semantic fail verdict")
            manifest = self._create_revision(
                manifest,
                revised_case_spec,
                reason,
                repair_layer=review["repair_layer"],
                suggested_paths=[str(row["path"]) for row in review["suggested_adjustments"]],
                trigger_stage="semantic_review",
                trigger_failure_code="semantic_intent_mismatch",
                evidence_refs=sorted(
                    {
                        str(ref["artifact_id"])
                        for row in review["requirements"]
                        for ref in row["evidence_refs"]
                    }
                ),
            )
            self._update_manifest(manifest, state="running", blocker=None, allowed_next_actions=["cancel"])
        return self.advance_until_blocked(job_id)

    def _apply_semantic_outcome(
        self,
        manifest: dict[str, Any],
        attempt: dict[str, Any],
        intent: Mapping[str, Any],
        review: Mapping[str, Any],
    ) -> dict[str, Any]:
        attempt = dict(attempt)
        if review["overall_status"] == "pass":
            if not self._publication_tier_satisfied(manifest):
                result = failure_stage_result(
                    stage="semantic_review",
                    failure_code="publication_tier_not_satisfied",
                    message="Semantic pass cannot override the requested publication tier",
                    source_status="blocked",
                    job_id=manifest["job_id"],
                    attempt_id=attempt["attempt_id"],
                )
                return self._apply_stage_result(manifest, result)
            attempt.update({"status": "completed", "updated_at": utc_now()})
            self.store.write_attempt(attempt)
            return self._update_manifest(
                manifest,
                state="completed",
                current_stage="completed",
                blocker=None,
                allowed_next_actions=[],
            )
        if review["overall_status"] == "uncertain":
            attempt.update({"status": "semantic_uncertain", "updated_at": utc_now()})
            self.store.write_attempt(attempt)
            return self._update_manifest(
                manifest,
                state="needs_user_decision",
                blocker={"code": "semantic_review_uncertain", "message": review["summary"], "stage": "semantic_review"},
                allowed_next_actions=["resume_with_revision", "cancel"],
            )
        attempt.update({"status": "semantic_failed", "updated_at": utc_now()})
        self.store.write_attempt(attempt)
        auto_allowed = self._semantic_repair_allowed(intent, review)
        return self._update_manifest(
            manifest,
            state="blocked" if auto_allowed else "needs_user_decision",
            blocker={"code": "semantic_intent_mismatch", "message": review["summary"], "stage": "semantic_review"},
            allowed_next_actions=["apply_revision_proposal", "cancel"] if auto_allowed else ["resume_with_revision", "cancel"],
        )

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            if manifest["state"] == "completed":
                raise JobStoreError("completed job cannot be cancelled")
            manifest = self._update_manifest(
                manifest,
                state="cancelled",
                current_stage="cancelled",
                blocker=None,
                allowed_next_actions=[],
            )
            provider_usage = self.store.read_optional(job_id, "receipts/provider_usage.json") or {
                "requests": {}
            }
            cancel_receipt = {
                "schema_version": "harness_agent_cancel_receipt_v1",
                "job_id": job_id,
                "remote_tasks": [
                    {
                        **dict(row),
                        "cancel_attempted": False,
                        "reason": "provider_adapter_has_no_cancel_contract",
                    }
                    for row in (provider_usage.get("requests") or {}).values()
                    if isinstance(row, Mapping)
                ],
                "created_at": utc_now(),
            }
            write_json(self.store.job_dir(job_id) / "receipts" / "cancel_receipt.json", cancel_receipt)
            checkpoint = checkpoint_payload(
                job_id=job_id,
                attempt_id=manifest.get("current_attempt_id"),
                stage="cancelled",
                status="completed",
                input_digest=manifest["request_digest"],
            )
            self.store.write_checkpoint(job_id, "cancelled", checkpoint)
            self._emit(job_id, "job_terminal", stage="cancelled", state="cancelled")
        return self.inspect(job_id)

    def _advance_one(self, manifest: dict[str, Any]) -> dict[str, Any]:
        stage = manifest["current_stage"]
        if stage == "intake_readiness":
            return self._advance_l0(manifest)
        if stage == "generation":
            return self._advance_generation(manifest)
        if stage == "task_readiness":
            return self._advance_l1(manifest)
        if stage == "compile":
            return self._advance_compile(manifest)
        if stage == "smoke":
            return self._advance_run(manifest, profile_name="smoke")
        if stage == "candidate":
            return self._advance_run(manifest, profile_name="candidate")
        if stage == "quality_gate":
            return self._advance_quality(manifest)
        if stage == "evidence_bundle":
            return self._advance_evidence(manifest)
        raise RuntimeError(f"unsupported controller stage: {stage}")

    def _advance_l0(self, manifest: dict[str, Any]) -> dict[str, Any]:
        job_id = manifest["job_id"]
        request = read_json(self.store.job_dir(job_id) / "request" / "user_request.json")
        failures = []
        if stable_digest(request) != manifest["request_digest"]:
            failures.append(("request_digest_mismatch", "immutable request digest changed"))
        if not str(request.get("text") or "").strip() and not request.get("inputs"):
            failures.append(("request_input_missing", "request requires text, an image, or both"))
        image_requirement = normalize_planning_image_requirement(request)
        for row in request.get("inputs") or []:
            path = Path(str(row.get("local_path") or ""))
            if not path.is_file() or self._sha256_file(path) != str(row.get("sha256") or ""):
                failures.append(("request_input_identity_mismatch", f"input identity changed: {row.get('input_id')}"))
        if (
            image_requirement["mode"] == "required"
            and not str(request.get("text") or "").strip()
            and manifest["authorizations"]["planning_llm_upload"] is not True
        ):
            failures.append(
                (
                    "planning_image_upload_authorization_missing",
                    "authorize upload of the required image inputs to the planning model",
                )
            )
        catalog_path = self.config.catalog
        if not catalog_path.is_file():
            failures.append(("catalog_missing", f"Asset Catalog is not initialized: {catalog_path}"))
        elif not AssetRegistry(catalog_path).writable:
            failures.append(("catalog_not_writable", f"Asset Catalog is not writable: {catalog_path}"))
        if failures:
            code, message = failures[0]
            result = failure_stage_result(
                stage="intake_readiness",
                failure_code=code,
                message=message,
                source_status="blocked",
                job_id=job_id,
            )
            self._write_controller_stage_result(manifest, result)
            return self._apply_stage_result(manifest, result)
        result = build_stage_result(stage="intake_readiness", status="completed", job_id=job_id)
        self._write_controller_stage_result(manifest, result)
        self._checkpoint(manifest, "intake_readiness", "completed", manifest["request_digest"])
        return self._update_manifest(manifest, current_stage="generation")

    def _advance_generation(self, manifest: dict[str, Any]) -> dict[str, Any]:
        job_id = manifest["job_id"]
        root = self.store.job_dir(job_id)
        request = read_json(root / "request" / "user_request.json")
        policy = self._generation_policy(job_id)
        if policy["mode"] == "native":
            return self._advance_native_generation(manifest, request=request)
        decision = planning_image_decision(
            request,
            upload_authorized=manifest["authorizations"]["planning_llm_upload"] is True,
            image_capability=self.config.planning_image_capability,
        )
        if decision["status"] != "ready":
            result = failure_stage_result(
                stage="generation",
                failure_code=str(decision["failure_code"]),
                message=str(decision["message"]),
                source_status="blocked",
                job_id=job_id,
            )
            self._write_controller_stage_result(manifest, result)
            return self._apply_stage_result(manifest, result)
        generation_dir = root / "request" / "generation"
        seed_path = root / "request" / "seed_case_spec.json"
        if seed_path.is_file():
            case_spec = case_spec_v2_from_dict(
                read_json(seed_path),
                available_input_ids=[str(row.get("input_id")) for row in request.get("inputs") or []],
            )
            seed_provenance = case_spec.data.get("provenance") or {}
            expansion = {
                "schema_version": "harness_expansion_v1",
                "ambiguities": [],
                "assumptions": [],
                "parameter_analysis": copy.deepcopy(seed_provenance.get("intent_parameter_analysis") or []),
            }
            stage_result = build_stage_result(
                stage="generation",
                status="completed",
                job_id=job_id,
                attempt_id="attempt_001",
                invocation_count=0,
            )
            generation_dir.mkdir(parents=True, exist_ok=True)
            write_json(generation_dir / "case_spec_v2.json", case_spec.data)
            write_json(generation_dir / "expansion.json", expansion)
            write_stage_result(generation_dir, stage_result)
        else:
            generation_request = copy.deepcopy(request)
            if manifest["authorizations"]["planning_llm_upload"] is True:
                for row in generation_request.get("inputs") or []:
                    if row.get("kind") == "image":
                        row["external_upload_authorized"] = True
                if generation_dir.is_dir():
                    cached_request_path = generation_dir / "request.json"
                    cached_request = read_json(cached_request_path) if cached_request_path.is_file() else {}
                    cached_uploaded = any(
                        row.get("kind") == "image" and row.get("external_upload_authorized") is True
                        for row in cached_request.get("inputs") or []
                        if isinstance(row, Mapping)
                    )
                    if not cached_uploaded:
                        sequence = len(list((root / "request").glob("generation_metadata_only_*"))) + 1
                        generation_dir.replace(root / "request" / f"generation_metadata_only_{sequence:03d}")
            generation_kwargs = {
                "artifact_dir": generation_dir,
                "job_id": job_id,
                "attempt_id": "attempt_001",
            }
            if self.hooks.generate is generate_case_spec_v2:
                generation_kwargs["effective_config"] = self.config
            generated = self.hooks.generate(generation_request, **generation_kwargs)
            case_spec = generated.case_spec
            expansion = generated.expansion
            stage_result = generated.stage_result or read_json(generation_dir / "stage_results" / "generation.json")
        projected_intent = self._project_intent_contract(manifest, request, expansion, case_spec.data)
        return self._complete_initial_generation(
            manifest,
            request=request,
            case_spec=case_spec,
            stage_result=stage_result,
            projected_intent=projected_intent,
        )

    def _complete_initial_generation(
        self,
        manifest: dict[str, Any],
        *,
        request: Mapping[str, Any],
        case_spec: CaseSpecV2,
        stage_result: Mapping[str, Any],
        projected_intent: Mapping[str, Any],
    ) -> dict[str, Any]:
        job_id = manifest["job_id"]
        root = self.store.job_dir(job_id)
        intent_path = root / "request" / "intent_contract.json"
        if intent_path.is_file():
            intent = IntentContract.from_dict(read_json(intent_path)).to_dict()
            existing_identity = self._intent_recovery_identity(intent)
            projected_identity = self._intent_recovery_identity(projected_intent)
            if intent["schema_version"] == "harness_intent_contract_v1":
                projected_identity.pop("planning_image_requirement", None)
            if existing_identity != projected_identity:
                raise JobStoreError("immutable Intent Contract differs from the recovered generation projection")
        else:
            intent = projected_intent
            intent_path = self.store.write_intent_contract(intent)
        amendment_paths = [
            *root.joinpath("request").glob("intent_amendment_*.json"),
            *root.joinpath("request").glob("authorization_amendment_*.json"),
        ]
        intent_digest = (
            self._effective_intent_digest(job_id, intent)
            if amendment_paths
            else stable_digest(intent)
        )
        attempt_id = "attempt_001"
        now = utc_now()
        attempt = AttemptManifest.from_dict(
            {
                "schema_version": ATTEMPT_MANIFEST_SCHEMA_VERSION,
                "job_id": job_id,
                "attempt_id": attempt_id,
                "revision": 1,
                "parent_attempt_id": None,
                "case_spec_digest": stable_digest(case_spec.data),
                "intent_contract_digest": intent_digest,
                "revision_reason": "initial_generation",
                "status": "generated",
                "compilation_id": None,
                "execution_fingerprint": None,
                "smoke_gate": None,
                "created_at": now,
                "updated_at": now,
            }
        ).to_dict()
        try:
            attempt_dir = self.store.create_attempt(attempt, case_spec.data)
            write_json(attempt_dir / "case_spec_diff.json", {"schema_version": "harness_case_spec_diff_v1", "changes": []})
            write_json(attempt_dir / "revision_reason.json", {"reason": "initial_generation", "trigger": "request"})
        except JobStoreError:
            existing = self.store.load_attempt(job_id, attempt_id)
            if existing["case_spec_digest"] != stable_digest(case_spec.data):
                raise
            attempt_dir = self.store.attempt_dir(job_id, attempt_id)
        write_stage_result(attempt_dir, self._with_identity(stage_result, job_id, attempt_id))
        usage = copy.deepcopy(manifest["usage"])
        usage["case_spec_revisions"] = max(1, int(usage["case_spec_revisions"]))
        usage["generation_invocations"] = max(
            int(usage["generation_invocations"]), int(stage_result.get("invocation_count") or 0)
        )
        manifest = self._update_manifest(
            manifest,
            current_stage="task_readiness",
            current_attempt_id=attempt_id,
            intent_contract_digest=intent_digest,
            usage=usage,
        )
        self._checkpoint(manifest, "generation", "completed", stable_digest(case_spec.data), [str(intent_path)])
        if intent["ambiguities"]:
            result = failure_stage_result(
                stage="intent_contract",
                failure_code="intent_ambiguity_requires_decision",
                message="the generated Intent Contract contains ambiguities that require a user decision",
                source_status="blocked",
                job_id=job_id,
                attempt_id=attempt_id,
            )
            self._write_controller_stage_result(manifest, result)
            return self._apply_stage_result(manifest, result)
        return manifest

    def _advance_native_generation(
        self,
        manifest: dict[str, Any],
        *,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        job_id = manifest["job_id"]
        request_root = self.store.job_dir(job_id) / "request"
        requirement = request["planning_image_requirement"]
        if requirement["mode"] == "required" and manifest["authorizations"]["planning_llm_upload"] is not True:
            result = failure_stage_result(
                stage="generation",
                failure_code="planning_image_upload_authorization_missing",
                message="authorize use of the required image inputs by the native planning Agent",
                source_status="blocked",
                job_id=job_id,
                invocation_count=0,
            )
            self._write_controller_stage_result(manifest, result)
            return self._apply_stage_result(manifest, result)
        context_path = request_root / "native_generation_context.json"
        if context_path.is_file():
            context = validate_native_generation_context(read_json(context_path))
            self._validate_native_context_binding(manifest, context, request)
        else:
            context = build_native_generation_context(
                job_id=job_id,
                request_digest=manifest["request_digest"],
                request=request,
                target=manifest["target"],
                authorizations=manifest["authorizations"],
            )
            write_json(context_path, context)
        submission_path = request_root / "native_generation_submission.json"
        ack_path = request_root / "native_generation_ack.json"
        if not submission_path.is_file() or not ack_path.is_file():
            result = failure_stage_result(
                stage="generation",
                failure_code="native_generation_submission_required",
                message="submit an Agent-native Intent draft and CaseSpec using the immutable generation context",
                source_status="blocked",
                job_id=job_id,
                invocation_count=0,
                artifact_refs=[
                    artifact_ref(
                        "native_generation_context",
                        str(context_path),
                        str(context["schema_version"]),
                    )
                ],
            )
            self._write_controller_stage_result(manifest, result)
            return self._apply_stage_result(manifest, result)
        submission = validate_native_generation_submission(read_json(submission_path), context=context)
        ack = validate_native_generation_ack(read_json(ack_path), context=context, submission=submission)
        case_spec = self._native_case_spec(request, submission["case_spec"])
        projected_intent = self._project_native_intent_contract(
            manifest,
            request,
            submission["intent_draft"],
            case_spec.data,
        )
        stage_result = build_stage_result(
            stage="generation",
            status="completed",
            job_id=job_id,
            attempt_id="attempt_001",
            invocation_count=0,
            request_identities=[str(ack["ack_identity"])],
            artifact_refs=[
                artifact_ref("native_generation_context", str(context_path), str(context["schema_version"])),
                artifact_ref("native_generation_submission", str(submission_path), str(submission["schema_version"])),
                artifact_ref("native_generation_ack", str(ack_path), NATIVE_GENERATION_ACK_SCHEMA_VERSION),
            ],
        )
        return self._complete_initial_generation(
            manifest,
            request=request,
            case_spec=case_spec,
            stage_result=stage_result,
            projected_intent=projected_intent,
        )

    def _native_case_spec(self, request: Mapping[str, Any], raw: Mapping[str, Any]) -> CaseSpecV2:
        available = [str(row.get("input_id")) for row in request.get("inputs") or []]
        initial = case_spec_v2_from_dict(
            apply_case_request_identity(raw, request),
            available_input_ids=available,
        )
        value = copy.deepcopy(initial.data)
        provenance = value.setdefault("provenance", {})
        provenance["case_generation"] = {
            "workflow": "agent_native_submission_v1",
            "controller_model_invocation_count": 0,
        }
        return case_spec_v2_from_dict(value, available_input_ids=available)

    def _project_native_intent_contract(
        self,
        manifest: Mapping[str, Any],
        request: Mapping[str, Any],
        draft: Mapping[str, Any],
        case_spec: Mapping[str, Any],
    ) -> dict[str, Any]:
        for row in draft["parameter_analysis"]:
            self._case_spec_path_value(case_spec, str(row["path"]))
        expansion = {
            "ambiguities": copy.deepcopy(draft["ambiguities"]),
            "assumptions": copy.deepcopy(draft["soft_preferences"]),
            "parameter_analysis": copy.deepcopy(draft["parameter_analysis"]),
        }
        contract = self._project_intent_contract(manifest, request, expansion, case_spec)
        expected_adjustable = sorted(
            str(row["path"])
            for row in draft["parameter_analysis"]
            if row["requirement_level"] in {"soft", "inferred"}
        )
        if contract["allowed_adjustments"]["paths"] != expected_adjustable:
            raise ValueError("native parameter analysis does not match a bounded CaseSpec leaf")
        contract["schema_version"] = INTENT_CONTRACT_SCHEMA_VERSION
        contract["source"] = "agent_native_submission_v1"
        contract["hard_requirements"].extend(
            {"id": str(row["id"]), "text": str(row["text"]), "frozen": True}
            for row in draft["hard_requirements"]
        )
        contract["soft_preferences"] = copy.deepcopy(draft["soft_preferences"])
        contract["prohibitions"] = [
            {"id": str(row["id"]), "text": str(row["text"]), "frozen": True}
            for row in draft["prohibitions"]
        ]
        return IntentContract.from_dict(contract).to_dict()

    def _generation_policy(self, job_id: str) -> dict[str, Any]:
        path = self.store.job_dir(job_id) / "request" / "generation_policy.json"
        if not path.is_file():
            raise JobStoreError("generation policy is missing")
        return validate_generation_policy(read_json(path))

    @staticmethod
    def _validate_native_context_binding(
        manifest: Mapping[str, Any],
        context: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> None:
        if (
            context["job_id"] != manifest["job_id"]
            or context["request_digest"] != manifest["request_digest"]
            or stable_digest(context["request"]) != manifest["request_digest"]
            or context["request"] != request
            or context["target"] != manifest["target"]
            or context["authorizations"] != manifest["authorizations"]
        ):
            raise JobStoreError("native generation context identity mismatch")

    def _advance_l1(self, manifest: dict[str, Any]) -> dict[str, Any]:
        case_spec = self._load_current_case_spec(manifest)
        runtime_case = compile_case_spec_v2_runtime(case_spec)
        requested = str((read_json(self.store.job_dir(manifest["job_id"]) / "request" / "user_request.json").get("execution_constraints") or {}).get("requested_backend") or "") or None
        try:
            selection = plan_backend(runtime_case.data, source_case_spec=case_spec, requested_backend=requested)
        except BaseException as exc:
            result = failure_stage_result(
                stage="task_readiness",
                failure_code=str(getattr(exc, "code", "task_readiness_exception")),
                message=str(exc) or type(exc).__name__,
                job_id=manifest["job_id"],
                attempt_id=manifest["current_attempt_id"],
            )
            self._write_controller_stage_result(manifest, result)
            return self._apply_stage_result(manifest, result)
        routes = self._provider_routes(case_spec)
        meshy_references = self._meshy_image_references(case_spec)
        provider_manifest = self._effective_provider_input_manifest(manifest["job_id"])
        auth = manifest["authorizations"]
        blocker: tuple[str, str] | None = None
        if routes.intersection({"external_site", "model_generation"}) and not auth["external_provider"]:
            blocker = ("external_provider_authorization_missing", "authorize the required external Provider operation")
        elif "model_generation" in routes and not auth["paid_provider_submission"]:
            blocker = ("paid_provider_authorization_missing", "authorize the paid model-generation submission")
        elif "model_generation" in routes and manifest["budget"]["max_paid_submissions"] < 1:
            blocker = ("paid_provider_budget_missing", "increase max_paid_submissions before model generation")
        elif (
            "model_generation" in routes
            and manifest["usage"]["paid_submissions"] >= manifest["budget"]["max_paid_submissions"]
        ):
            blocker = ("paid_provider_budget_exhausted", "paid Provider submission budget is already exhausted")
        elif "model_generation" in routes and self.config.meshy_api_key() is None:
            blocker = ("provider_credentials_missing", f"configure {self.config.meshy_api_key_env}")
        elif meshy_references and provider_manifest is None:
            blocker = ("provider_input_manifest_missing", "materialize a Provider input manifest for Meshy references")
        elif meshy_references and not auth["meshy_upload"]:
            blocker = ("meshy_upload_authorization_missing", "authorize image upload to Meshy separately")
        elif meshy_references:
            manifest_ids = {
                str(row.get("input_id") or "")
                for row in provider_manifest.get("inputs") or []
                if isinstance(row, Mapping)
            }
            missing_ids = sorted(
                str(row.get("input_id") or "")
                for row in meshy_references
                if str(row.get("input_id") or "") not in manifest_ids
            )
            if missing_ids:
                blocker = ("provider_input_missing", f"Provider input IDs are unresolved: {missing_ids}")
        target_tier = manifest["target"]["publication_tier"]
        case_tier = str((case_spec.data.get("asset_policy") or {}).get("required_license_tier") or "local_preview")
        if blocker is None and target_tier == "reference" and case_tier != "reference":
            blocker = ("publication_tier_not_satisfied", "CaseSpec must require reference-tier assets")
        if blocker is None and target_tier == "reference" and selection["selected_backend"] == "fallback":
            blocker = ("degraded_preview_only", "fallback cannot satisfy a reference-tier job")
        if blocker is None and shutil.disk_usage(self.store.workspace).free < manifest["budget"]["min_free_disk_bytes"]:
            blocker = ("disk_budget_insufficient", "free workspace storage is below the configured task minimum")
        if blocker is not None:
            result = failure_stage_result(
                stage="task_readiness",
                failure_code=blocker[0],
                message=blocker[1],
                source_status="blocked",
                job_id=manifest["job_id"],
                attempt_id=manifest["current_attempt_id"],
            )
            self._write_controller_stage_result(manifest, result)
            return self._apply_stage_result(manifest, result)
        result = build_stage_result(
            stage="task_readiness",
            status="completed",
            job_id=manifest["job_id"],
            attempt_id=manifest["current_attempt_id"],
        )
        self._write_controller_stage_result(manifest, result)
        self._checkpoint(manifest, "task_readiness", "completed", stable_digest(selection))
        return self._update_manifest(manifest, current_stage="compile")

    def _advance_compile(self, manifest: dict[str, Any]) -> dict[str, Any]:
        case_spec = self._load_current_case_spec(manifest)
        attempt_dir = self.store.attempt_dir(manifest["job_id"], manifest["current_attempt_id"])
        request = read_json(self.store.job_dir(manifest["job_id"]) / "request" / "user_request.json")
        provider_manifest = self._effective_provider_input_manifest(manifest["job_id"])
        requested = str((request.get("execution_constraints") or {}).get("requested_backend") or "") or None
        compilation = self.hooks.compile(
            case_spec,
            requested_backend=requested,
            requested_views=list(execution_profile("smoke").views),
            render_passes=list(execution_profile("smoke").render_passes),
            registry=self._registry(),
            provider_orchestrator=self._provider_orchestrator(manifest),
            provider_input_manifest=provider_manifest,
            stage_result_dir=attempt_dir,
            job_id=manifest["job_id"],
            attempt_id=manifest["current_attempt_id"],
            transaction_dir=attempt_dir / "compilation",
        )
        compilation.write(attempt_dir / "compilation")
        manifest = self._reconcile_provider_usage(manifest)
        self._compilations[(manifest["job_id"], manifest["current_attempt_id"], "smoke")] = compilation
        if compilation.status != "pass":
            result = self._with_identity(compilation.stage_result or {}, manifest["job_id"], manifest["current_attempt_id"])
            return self._apply_stage_result(manifest, result)
        transaction = read_json(attempt_dir / "compilation" / "compilation_transaction.json")
        fingerprint = self._execution_fingerprint(case_spec.data, compilation)
        attempt = self.store.load_attempt(manifest["job_id"], manifest["current_attempt_id"])
        attempt.update(
            {
                "status": "compiled",
                "compilation_id": str(transaction["transaction_id"]),
                "execution_fingerprint": fingerprint,
                "updated_at": utc_now(),
            }
        )
        self.store.write_attempt(attempt)
        self._checkpoint(manifest, "compile", "completed", fingerprint)
        return self._update_manifest(
            manifest,
            current_stage="smoke",
            active_compilation_id=str(transaction["transaction_id"]),
        )

    def _advance_run(self, manifest: dict[str, Any], *, profile_name: str) -> dict[str, Any]:
        attempt_id = manifest["current_attempt_id"]
        attempt_dir = self.store.attempt_dir(manifest["job_id"], attempt_id)
        case_spec = self._load_current_case_spec(manifest)
        compilation = self._compilation_for_attempt(manifest, case_spec, profile_name=profile_name)
        profile = execution_profile(profile_name)
        profile_fingerprint = stable_digest(
            {"execution": self._execution_fingerprint(case_spec.data, compilation), "profile": profile.__dict__}
        )
        run_slot = attempt_dir / "runs" / profile_name
        gate_path = attempt_dir / "smoke_gate.json"
        if profile_name == "smoke" and gate_path.is_file():
            prior = read_json(gate_path)
            prior_run = Path(str(prior.get("run_dir") or ""))
            if (
                prior.get("execution_fingerprint") == profile_fingerprint
                and prior.get("status") == "pass"
                and self._run_technical_pass(prior_run)
            ):
                prior["mode"] = "reused"
                prior["updated_at"] = utc_now()
                write_json(gate_path, prior)
                self._checkpoint(manifest, "smoke", "reused", profile_fingerprint, [str(gate_path)])
                return self._update_manifest(manifest, current_stage="candidate")
        ue_required = "ue" in {
            str(compilation.backend_selection.get("selected_backend") or ""),
            str(compilation.backend_selection.get("render_backend") or ""),
        }
        expected_run = run_slot / f"{compilation.runtime_case.case_id}_{compilation.selected_backend}"
        execute_result_path = expected_run / "stage_results" / "execute.json"
        execution_reusable = False
        if execute_result_path.is_file():
            prior_execute = StageResult.from_dict(read_json(execute_result_path)).to_dict()
            execution_reusable = prior_execute["status"] == "completed"
        if ue_required and not execution_reusable:
            usage = copy.deepcopy(manifest["usage"])
            if usage["ue_launches"] >= manifest["budget"]["max_ue_launches"]:
                result = failure_stage_result(
                    stage=profile_name,
                    failure_code="ue_launch_budget_exhausted",
                    message="UE launch budget is exhausted",
                    job_id=manifest["job_id"],
                    attempt_id=attempt_id,
                )
                self._write_controller_stage_result(manifest, result)
                return self._apply_stage_result(manifest, result)
            usage["ue_launches"] += 1
            manifest = self._update_manifest(manifest, usage=usage)
        compilation.write(expected_run)
        started = self.hooks.monotonic()
        if execution_reusable:
            run_dir = expected_run
        else:
            try:
                with self._profile_environment(profile.environment() if ue_required else {}):
                    run_dir = Path(
                        self.hooks.execute(
                            compilation.runtime_case,
                            run_slot,
                            compilation=compilation,
                            requested_views=list(profile.views),
                            render_passes=list(profile.render_passes),
                            camera_strategy="bounds_auto_v1",
                            profile=profile.name,
                            width=profile.width,
                            height=profile.height,
                            complete_sensor_contract=profile.complete_sensor_contract,
                        )
                    )
            except BaseException as exc:
                self._attach_exception_stage_result(exc, expected_run, "execute")
                raise
        verifier_path = run_dir / "harness_verifier.json"
        verifier_stage_path = run_dir / "stage_results" / "verifier.json"
        try:
            verifier = (
                read_json(verifier_path)
                if verifier_path.is_file()
                and verifier_stage_path.is_file()
                and read_json(verifier_stage_path).get("status") in {"completed", "failed", "blocked"}
                else dict(self.hooks.verify(run_dir))
            )
        except BaseException as exc:
            self._attach_exception_stage_result(exc, run_dir, "verifier")
            raise
        render_path = run_dir / "render_sync_report.json"
        render_stage_path = run_dir / "stage_results" / "render_sync.json"
        try:
            render_sync = (
                read_json(render_path)
                if render_path.is_file()
                and render_stage_path.is_file()
                and read_json(render_stage_path).get("status") in {"completed", "failed", "blocked"}
                else dict(
                    self.hooks.render_sync(
                        run_dir,
                        require_depth="depth" in profile.render_passes,
                        require_segmentation="segmentation" in profile.render_passes,
                        write=True,
                    )
                )
            )
        except BaseException as exc:
            self._attach_exception_stage_result(exc, run_dir, "render_sync")
            raise
        write_execution_reports(
            run_dir,
            profile,
            wall_seconds=max(0.0, self.hooks.monotonic() - started),
            status="pass" if verifier.get("status") == "pass" and render_sync.get("status") == "pass" else "fail",
        )
        self._adopt_run_stage_results(run_dir, manifest["job_id"], attempt_id)
        passed = verifier.get("status") == "pass" and render_sync.get("status") == "pass"
        if profile_name == "smoke":
            smoke_mode = self._smoke_mode(attempt_dir)
            gate = {
                "schema_version": SMOKE_GATE_SCHEMA_VERSION,
                "job_id": manifest["job_id"],
                "attempt_id": attempt_id,
                "mode": smoke_mode,
                "status": "pass" if passed else "fail",
                "execution_fingerprint": profile_fingerprint,
                "run_dir": str(run_dir),
                "verifier_status": verifier.get("status"),
                "render_sync_status": render_sync.get("status"),
                "updated_at": utc_now(),
            }
            write_json(gate_path, gate)
            attempt = self.store.load_attempt(manifest["job_id"], attempt_id)
            attempt.update({"smoke_gate": str(gate_path), "status": "smoke_passed" if passed else "smoke_failed", "updated_at": utc_now()})
            self.store.write_attempt(attempt)
        if not passed:
            result_path = run_dir / "stage_results" / ("verifier.json" if verifier.get("status") != "pass" else "render_sync.json")
            result = self._with_identity(read_json(result_path), manifest["job_id"], attempt_id)
            return self._apply_stage_result(manifest, result)
        checkpoint_stage = profile_name
        self._checkpoint(manifest, checkpoint_stage, "completed", profile_fingerprint, [str(run_dir)])
        if profile_name == "smoke":
            return self._update_manifest(manifest, current_stage="candidate")
        attempt = self.store.load_attempt(manifest["job_id"], attempt_id)
        attempt.update({"status": "candidate_passed", "updated_at": utc_now()})
        self.store.write_attempt(attempt)
        write_json(attempt_dir / "candidate_run.json", {"run_dir": str(run_dir), "fingerprint": profile_fingerprint})
        return self._update_manifest(manifest, current_stage="quality_gate")

    def _advance_quality(self, manifest: dict[str, Any]) -> dict[str, Any]:
        attempt_dir = self.store.attempt_dir(manifest["job_id"], manifest["current_attempt_id"])
        candidate = read_json(attempt_dir / "candidate_run.json")
        run_dir = Path(str(candidate["run_dir"]))
        try:
            report = dict(self.hooks.quality(run_dir))
        except BaseException as exc:
            self._attach_exception_stage_result(exc, run_dir, "quality_gate")
            raise
        self._adopt_run_stage_results(run_dir, manifest["job_id"], manifest["current_attempt_id"])
        result = read_json(run_dir / "stage_results" / "quality_gate.json")
        if report.get("hard_gate_passed") is not True:
            return self._apply_stage_result(manifest, self._with_identity(result, manifest["job_id"], manifest["current_attempt_id"]))
        attempt = self.store.load_attempt(manifest["job_id"], manifest["current_attempt_id"])
        attempt.update({"status": "quality_gate_passed", "updated_at": utc_now()})
        self.store.write_attempt(attempt)
        self._checkpoint(manifest, "quality_gate", "completed", stable_digest(report), [str(run_dir / "quality_report.json")])
        return self._update_manifest(manifest, current_stage="evidence_bundle")

    def _advance_evidence(self, manifest: dict[str, Any]) -> dict[str, Any]:
        job_id = manifest["job_id"]
        attempt_id = manifest["current_attempt_id"]
        attempt_dir = self.store.attempt_dir(job_id, attempt_id)
        attempt = self.store.load_attempt(job_id, attempt_id)
        candidate = read_json(attempt_dir / "candidate_run.json")
        request = read_json(self.store.job_dir(job_id) / "request" / "user_request.json")
        intent = IntentContract.from_dict(
            read_json(self.store.job_dir(job_id) / "request" / "intent_contract.json")
        ).to_dict()
        request_root = self.store.job_dir(job_id) / "request"
        intent_amendments = [
            read_json(path)
            for path in sorted(
                [*request_root.glob("intent_amendment_*.json"), *request_root.glob("authorization_amendment_*.json")],
                key=lambda value: value.name,
            )
        ]
        result = dict(
            self.hooks.evidence(
                job_id=job_id,
                attempt=attempt,
                attempt_dir=attempt_dir,
                candidate_run_dir=Path(str(candidate["run_dir"])),
                request=request,
                intent_contract=intent,
                intent_amendments=intent_amendments,
            )
        )
        stage_result = self._with_identity(result["stage_result"], job_id, attempt_id)
        write_stage_result(attempt_dir, stage_result)
        manifest_path = Path(str(result["manifest_path"]))
        bundle = EvidenceBundleManifest.from_dict(result["manifest"]).to_dict()
        if manifest_path != attempt_dir / "evidence_bundle" / "manifest.json":
            raise JobStoreError("Evidence Bundle manifest path is not canonical")
        snapshots = current_evidence_snapshots(
            attempt_dir=attempt_dir,
            request=request,
            intent_contract=intent,
            intent_amendments=intent_amendments,
        )
        bundle = validate_current_evidence_bundle(
            manifest_path=manifest_path,
            job_id=job_id,
            attempt=attempt,
            attempt_dir=attempt_dir,
            expected_candidate_run_dir=Path(str(candidate["run_dir"])),
            expected_intent_contract_digest=manifest["intent_contract_digest"],
            expected_snapshots=snapshots,
            expected_manifest_digest=stable_digest(bundle),
        )
        attempt.update({"status": "awaiting_semantic_review", "updated_at": utc_now()})
        self.store.write_attempt(attempt)
        self._checkpoint(
            manifest,
            "evidence_bundle",
            "completed",
            stable_digest(bundle),
            [str(manifest_path)],
        )
        return self._update_manifest(
            manifest,
            state="awaiting_semantic_review",
            current_stage="semantic_review",
            allowed_next_actions=["run_semantic_review", "cancel"],
        )

    def _compilation_for_attempt(
        self,
        manifest: Mapping[str, Any],
        case_spec: CaseSpecV2,
        *,
        profile_name: str,
    ) -> RuntimeCompilation:
        key = (str(manifest["job_id"]), str(manifest["current_attempt_id"]), profile_name)
        cached = self._compilations.get(key)
        if cached is not None:
            return cached
        attempt_dir = self.store.attempt_dir(key[0], key[1])
        request = read_json(self.store.job_dir(key[0]) / "request" / "user_request.json")
        requested = str((request.get("execution_constraints") or {}).get("requested_backend") or "") or None
        compilation = self.hooks.compile(
            case_spec,
            requested_backend=requested,
            requested_views=list(execution_profile(profile_name).views),
            render_passes=list(execution_profile(profile_name).render_passes),
            registry=self._registry(),
            provider_orchestrator=self._provider_orchestrator(manifest),
            provider_input_manifest=self._effective_provider_input_manifest(key[0]),
            stage_result_dir=attempt_dir,
            job_id=key[0],
            attempt_id=key[1],
            transaction_dir=attempt_dir / "compilation",
        )
        self._compilations[key] = compilation
        return compilation

    def _project_intent_contract(
        self,
        manifest: Mapping[str, Any],
        request: Mapping[str, Any],
        expansion: Mapping[str, Any],
        case_spec: Mapping[str, Any],
    ) -> dict[str, Any]:
        verification = copy.deepcopy(case_spec.get("verification_requirements") or {})
        assertions = verification.get("assertions") if isinstance(verification, Mapping) else []
        hard = []
        if str(request.get("text") or "").strip():
            hard.append({"id": "original_user_request", "text": str(request["text"]), "frozen": True})
        input_identities = [
            {key: row.get(key) for key in ("input_id", "kind", "mime_type", "sha256", "byte_size")}
            for row in request.get("inputs") or []
        ]
        if not hard and input_identities:
            hard.append(
                {
                    "id": "original_user_visual_inputs",
                    "text": "The result must semantically match the immutable original visual inputs.",
                    "frozen": True,
                }
            )
        ambiguities = []
        for index, raw in enumerate(expansion.get("ambiguities") or [], start=1):
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            row["ambiguity_id"] = f"ambiguity_{index:03d}_{stable_digest(row)[:12]}"
            ambiguities.append(row)
        assumptions = [dict(row) for row in expansion.get("assumptions") or [] if isinstance(row, Mapping)]
        execution = {
            "backend_constraints": copy.deepcopy(case_spec.get("backend_constraints") or {}),
            "target_profile": manifest["target"]["execution_profile"],
            "publication_tier": manifest["target"]["publication_tier"],
            "duration_s": (case_spec.get("scene") or {}).get("duration_s"),
            "resolution": [1920, 1080],
        }
        allowed_adjustments = self._project_allowed_adjustments(expansion, case_spec)
        contract = {
            "schema_version": PROJECTED_INTENT_CONTRACT_SCHEMA_VERSION,
            "job_id": manifest["job_id"],
            "request_digest": manifest["request_digest"],
            "source": "expansion_case_spec_projection_v1",
            "original_request": {"text": request.get("text") or "", "case_id": request.get("case_id")},
            "input_identities": input_identities,
            "planning_image_requirement": copy.deepcopy(request["planning_image_requirement"]),
            "hard_requirements": hard,
            "soft_preferences": assumptions,
            "prohibitions": [],
            "ambiguities": ambiguities,
            "asset_policy": copy.deepcopy(case_spec.get("asset_policy") or {}),
            "execution": execution,
            "authorizations": copy.deepcopy(manifest["authorizations"]),
            "verification": {"assertions": copy.deepcopy(assertions or []), "frozen": True},
            "allowed_adjustments": allowed_adjustments,
            "frozen_digests": {
                "original_request": stable_digest({"text": request.get("text") or "", "inputs": input_identities}),
                "verification_assertions": stable_digest(assertions or []),
                "backend_constraints": stable_digest(case_spec.get("backend_constraints") or {}),
                "asset_policy": stable_digest(case_spec.get("asset_policy") or {}),
            },
            "created_at": utc_now(),
        }
        return IntentContract.from_dict(contract).to_dict()

    @classmethod
    def _project_allowed_adjustments(
        cls,
        expansion: Mapping[str, Any],
        case_spec: Mapping[str, Any],
    ) -> dict[str, Any]:
        constraints: dict[str, Any] = {}
        for row in expansion.get("parameter_analysis") or []:
            if not isinstance(row, Mapping) or row.get("requirement_level") not in {"soft", "inferred"}:
                continue
            path = str(row.get("path") or "")
            constraint = row.get("constraint")
            try:
                current = cls._case_spec_path_value(case_spec, path)
            except (KeyError, ValueError):
                continue
            if isinstance(current, Mapping) or not isinstance(constraint, Mapping):
                continue
            kind = constraint.get("kind")
            if kind == "numeric":
                minimum, maximum = constraint.get("min"), constraint.get("max")
                if (
                    set(constraint) != {"kind", "min", "max"}
                    or
                    not isinstance(current, (int, float))
                    or isinstance(current, bool)
                    or not isinstance(minimum, (int, float))
                    or isinstance(minimum, bool)
                    or not isinstance(maximum, (int, float))
                    or isinstance(maximum, bool)
                    or minimum > maximum
                    or not minimum <= current <= maximum
                ):
                    continue
            elif kind == "list":
                minimum, maximum = constraint.get("min_items"), constraint.get("max_items")
                if (
                    set(constraint) != {"kind", "min_items", "max_items"}
                    or
                    not isinstance(current, list)
                    or not isinstance(minimum, int)
                    or isinstance(minimum, bool)
                    or not isinstance(maximum, int)
                    or isinstance(maximum, bool)
                    or minimum < 0
                    or minimum > maximum
                    or not minimum <= len(current) <= maximum
                ):
                    continue
            elif kind == "enum":
                values = constraint.get("values")
                if set(constraint) != {"kind", "values"} or not isinstance(values, list) or not values or current not in values:
                    continue
            else:
                continue
            constraints[path] = copy.deepcopy(dict(constraint))
        paths = sorted(constraints)
        return {"paths": paths, "ranges": {path: constraints[path] for path in paths}}

    @staticmethod
    def _case_spec_path_value(case_spec: Mapping[str, Any], path: str) -> Any:
        if not re.fullmatch(r"\$\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", path):
            raise ValueError("CaseSpec path is not a canonical object path")
        value: Any = case_spec
        for component in path[2:].split("."):
            if not isinstance(value, Mapping) or component not in value:
                raise KeyError(path)
            value = value[component]
        return value

    def _create_revision(
        self,
        manifest: dict[str, Any],
        raw_case_spec: Mapping[str, Any],
        reason: str,
        *,
        repair_layer: str,
        trigger_stage: str,
        trigger_failure_code: str,
        evidence_refs: list[str] | None = None,
        suggested_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        if manifest["usage"]["case_spec_revisions"] >= manifest["budget"]["max_case_spec_revisions"]:
            raise JobStoreError("CaseSpec revision budget is exhausted")
        request = read_json(self.store.job_dir(manifest["job_id"]) / "request" / "user_request.json")
        case_spec = case_spec_v2_from_dict(
            raw_case_spec,
            available_input_ids=[str(row.get("input_id")) for row in request.get("inputs") or []],
        )
        intent = IntentContract.from_dict(read_json(self.store.job_dir(manifest["job_id"]) / "request" / "intent_contract.json")).to_dict()
        frozen = intent["frozen_digests"]
        if stable_digest((case_spec.data.get("verification_requirements") or {}).get("assertions") or []) != frozen["verification_assertions"]:
            raise JobStoreError("revision cannot weaken or replace frozen verification assertions")
        if stable_digest(case_spec.data.get("backend_constraints") or {}) != frozen["backend_constraints"]:
            raise JobStoreError("revision cannot change frozen backend constraints")
        if stable_digest(case_spec.data.get("asset_policy") or {}) != frozen["asset_policy"]:
            raise JobStoreError("revision cannot change the frozen asset policy")
        parent_id = manifest["current_attempt_id"]
        parent_spec = read_json(self.store.attempt_dir(manifest["job_id"], parent_id) / "case_spec.json")
        changes = self._json_diff(parent_spec, case_spec.data)
        if not changes:
            raise JobStoreError("revision proposal does not change the source CaseSpec")
        allowed = self._effective_allowed_adjustments(intent)
        self._validate_revision_changes(
            changes,
            allowed,
            repair_layer=repair_layer,
            suggested_paths=suggested_paths,
        )
        proposal_core = {
            "job_id": manifest["job_id"],
            "base_attempt_id": parent_id,
            "base_case_spec_digest": stable_digest(parent_spec),
            "intent_contract_digest": manifest["intent_contract_digest"],
            "trigger_stage": trigger_stage,
            "trigger_failure_code": trigger_failure_code,
            "repair_layer": repair_layer,
            "changes": changes,
            "revised_case_spec_digest": stable_digest(case_spec.data),
            "reason": reason,
            "evidence_refs": list(evidence_refs or []),
        }
        proposal = RevisionProposal.from_dict(
            {
                "schema_version": REVISION_PROPOSAL_SCHEMA_VERSION,
                "proposal_id": f"proposal_{stable_digest(proposal_core)[:16]}",
                **proposal_core,
                "created_at": utc_now(),
            }
        ).to_dict()
        parent_root = self.store.attempt_dir(manifest["job_id"], parent_id)
        sequence = len(list(parent_root.glob("revision_proposal_*.json"))) + 1
        write_json(parent_root / f"revision_proposal_{sequence:03d}.json", proposal)
        revision = int(manifest["usage"]["case_spec_revisions"]) + 1
        attempt_id = validate_attempt_id(f"attempt_{revision:03d}")
        now = utc_now()
        attempt = AttemptManifest.from_dict(
            {
                "schema_version": ATTEMPT_MANIFEST_SCHEMA_VERSION,
                "job_id": manifest["job_id"],
                "attempt_id": attempt_id,
                "revision": revision,
                "parent_attempt_id": parent_id,
                "case_spec_digest": stable_digest(case_spec.data),
                "intent_contract_digest": manifest["intent_contract_digest"],
                "revision_reason": reason,
                "status": "revised",
                "compilation_id": None,
                "execution_fingerprint": None,
                "smoke_gate": None,
                "created_at": now,
                "updated_at": now,
            }
        ).to_dict()
        root = self.store.create_attempt(attempt, case_spec.data)
        write_json(root / "case_spec_diff.json", {"schema_version": "harness_case_spec_diff_v1", "changes": changes})
        write_json(root / "revision_reason.json", {"reason": reason, "trigger": trigger_stage, "failure_code": trigger_failure_code})
        write_json(root / "revision_proposal.json", proposal)
        usage = copy.deepcopy(manifest["usage"])
        usage["case_spec_revisions"] = revision
        return self._update_manifest(
            manifest,
            current_attempt_id=attempt_id,
            active_compilation_id=None,
            current_stage="task_readiness",
            usage=usage,
        )

    def _apply_intent_amendment(
        self,
        manifest: Mapping[str, Any],
        raw: Mapping[str, Any],
    ) -> dict[str, Any]:
        amendment = copy.deepcopy(dict(raw))
        resolutions = amendment.get("ambiguity_resolutions")
        if not isinstance(resolutions, list) or not resolutions or any(not isinstance(row, Mapping) for row in resolutions):
            raise ValueError("intent_amendment.ambiguity_resolutions must be a non-empty list of objects")
        base = IntentContract.from_dict(
            read_json(self.store.job_dir(manifest["job_id"]) / "request" / "intent_contract.json")
        ).to_dict()
        expected = {}
        for index, ambiguity in enumerate(base["ambiguities"], start=1):
            identity = str(ambiguity.get("ambiguity_id") or "").strip()
            if not identity:
                identity = f"ambiguity_{index:03d}_{stable_digest(ambiguity)[:12]}"
            if identity in expected:
                raise ValueError("Intent Contract ambiguity identities must be unique")
            expected[identity] = ambiguity
        canonical_resolutions = []
        seen: set[str] = set()
        for raw_resolution in resolutions:
            resolution = dict(raw_resolution)
            ambiguity_id = str(resolution.get("ambiguity_id") or "").strip()
            if not ambiguity_id:
                question = str(resolution.get("question") or "").strip()
                matches = [
                    identity
                    for identity, ambiguity in expected.items()
                    if question and str(ambiguity.get("question") or "").strip() == question
                ]
                if len(matches) == 1:
                    ambiguity_id = matches[0]
            if not ambiguity_id:
                raise ValueError("every ambiguity resolution requires an ambiguity_id (or exact recorded question)")
            decision = resolution.get("decision")
            if not isinstance(decision, str) or not decision.strip():
                raise ValueError("every ambiguity resolution requires a non-empty decision")
            if ambiguity_id not in expected or ambiguity_id in seen:
                raise ValueError("intent amendment must match the recorded ambiguities exactly once")
            seen.add(ambiguity_id)
            resolution["ambiguity_id"] = ambiguity_id
            resolution["decision"] = decision.strip()
            canonical_resolutions.append(resolution)
        if seen != set(expected):
            raise ValueError("intent amendment must resolve every recorded ambiguity exactly once")
        root = self.store.job_dir(manifest["job_id"]) / "request"
        prior = sorted(root.glob("intent_amendment_*.json"))
        payload = {
            "schema_version": "harness_intent_contract_amendment_v1",
            "job_id": manifest["job_id"],
            "sequence": len(prior) + 1,
            "parent_intent_digest": manifest["intent_contract_digest"],
            "ambiguity_resolutions": canonical_resolutions,
            "reason": str(amendment.get("reason") or "user decision"),
            "created_at": utc_now(),
        }
        path = self.store.write_request_artifact(
            manifest["job_id"],
            f"intent_amendment_{payload['sequence']:03d}.json",
            payload,
        )
        effective_digest = self._effective_intent_digest(manifest["job_id"], base)
        if manifest.get("current_attempt_id"):
            attempt = self.store.load_attempt(manifest["job_id"], manifest["current_attempt_id"])
            attempt.update({"intent_contract_digest": effective_digest, "updated_at": utc_now()})
            self.store.write_attempt(attempt)
        return self._update_manifest(
            manifest,
            intent_contract_digest=effective_digest,
        )

    def _record_authorization_amendment(
        self,
        manifest: Mapping[str, Any],
        *,
        prior_authorizations: Mapping[str, Any],
        prior_budget: Mapping[str, Any],
    ) -> dict[str, Any]:
        root = self.store.job_dir(manifest["job_id"]) / "request"
        prior = sorted(root.glob("authorization_amendment_*.json"))
        sequence = len(prior) + 1
        authorization_changes = {
            key: {"before": prior_authorizations[key], "after": manifest["authorizations"][key]}
            for key in manifest["authorizations"]
            if prior_authorizations[key] != manifest["authorizations"][key]
        }
        budget_changes = {}
        if prior_budget["max_paid_submissions"] != manifest["budget"]["max_paid_submissions"]:
            budget_changes["max_paid_submissions"] = {
                "before": prior_budget["max_paid_submissions"],
                "after": manifest["budget"]["max_paid_submissions"],
            }
        request = read_json(root / "user_request.json")
        effective_provider_manifest = build_provider_input_manifest(
            list(request.get("inputs") or []),
            workspace=self.store.workspace,
            meshy_upload_authorized=manifest["authorizations"]["meshy_upload"],
        )
        provider_path = self.store.write_request_artifact(
            manifest["job_id"],
            f"provider_input_manifest_effective_{sequence:03d}.json",
            effective_provider_manifest,
        )
        payload = {
            "schema_version": "harness_authorization_amendment_v1",
            "job_id": manifest["job_id"],
            "sequence": sequence,
            "parent_intent_digest": manifest["intent_contract_digest"],
            "authorization_changes": authorization_changes,
            "budget_changes": budget_changes,
            "effective_provider_input_manifest": {
                "path": str(provider_path),
                "digest": stable_digest(effective_provider_manifest),
            },
            "created_at": utc_now(),
        }
        self.store.write_request_artifact(
            manifest["job_id"],
            f"authorization_amendment_{sequence:03d}.json",
            payload,
        )
        intent_path = root / "intent_contract.json"
        effective_digest = manifest.get("intent_contract_digest")
        if intent_path.is_file():
            base = IntentContract.from_dict(read_json(intent_path)).to_dict()
            effective_digest = self._effective_intent_digest(manifest["job_id"], base)
        if manifest.get("current_attempt_id"):
            attempt = self.store.load_attempt(manifest["job_id"], manifest["current_attempt_id"])
            attempt.update({"intent_contract_digest": effective_digest, "updated_at": utc_now()})
            self.store.write_attempt(attempt)
        return self._update_manifest(manifest, intent_contract_digest=effective_digest)

    def _effective_intent_digest(self, job_id: str, base: Mapping[str, Any]) -> str:
        root = self.store.job_dir(job_id) / "request"
        amendments = sorted(
            [*root.glob("intent_amendment_*.json"), *root.glob("authorization_amendment_*.json")],
            key=lambda path: path.name,
        )
        return stable_digest(
            {
                "base": stable_digest(base),
                "amendments": [
                    {"name": path.name, "digest": stable_digest(read_json(path))}
                    for path in amendments
                ],
            }
        )

    def _budget_gate(self, manifest: Mapping[str, Any]) -> dict[str, Any] | None:
        elapsed = float(manifest["usage"]["active_elapsed_seconds"])
        hard = int(manifest["budget"]["hard_deadline_seconds"])
        soft = int(manifest["budget"]["soft_deadline_seconds"])
        if elapsed >= hard:
            return failure_stage_result(
                stage="budget",
                failure_code="budget_exhausted",
                message="active runtime hard deadline is exhausted; approve an extension to continue",
                source_status="blocked",
                job_id=manifest["job_id"],
                attempt_id=manifest.get("current_attempt_id"),
            )
        if elapsed >= soft and manifest["current_stage"] in {"compile", "smoke", "candidate", "semantic_review"}:
            return failure_stage_result(
                stage="budget",
                failure_code="soft_deadline_reached",
                message="active runtime soft deadline was reached before another expensive stage",
                source_status="blocked",
                job_id=manifest["job_id"],
                attempt_id=manifest.get("current_attempt_id"),
            )
        if manifest["current_stage"] == "candidate":
            required = int(manifest["budget"]["candidate_reserve_seconds"]) + int(manifest["budget"]["post_candidate_reserve_seconds"])
            if hard - elapsed < required:
                return failure_stage_result(
                    stage="budget",
                    failure_code="candidate_budget_reserve_insufficient",
                    message="insufficient active runtime remains for Candidate and post-Candidate gates",
                    source_status="blocked",
                    job_id=manifest["job_id"],
                    attempt_id=manifest.get("current_attempt_id"),
                )
        return None

    def _apply_stage_result(self, manifest: dict[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
        value = StageResult.from_dict(result).to_dict()
        if value["status"] == "completed":
            return manifest
        if value["failure_code"] == "native_generation_submission_required":
            return self._update_manifest(
                manifest,
                state="blocked",
                blocker={"code": value["failure_code"], "message": value["message"], "stage": value["stage"]},
                allowed_next_actions=["submit_native_generation", "cancel"],
            )
        if value["failure_class"] == "interrupted":
            return self._update_manifest(
                manifest,
                state="paused_interrupted",
                blocker={"code": value["failure_code"], "message": value["message"], "stage": value["stage"]},
                allowed_next_actions=["resume", "cancel"],
            )
        if value["retryable"]:
            usage = copy.deepcopy(manifest["usage"])
            retries = int(usage["stage_retries"].get(value["stage"], 0))
            if retries < manifest["budget"]["max_stage_retries"] and usage["total_retries"] < manifest["budget"]["max_total_retries"]:
                usage["stage_retries"][value["stage"]] = retries + 1
                usage["total_retries"] += 1
                return self._update_manifest(manifest, state="running", usage=usage)
        if value["failure_class"] in {"blocked_user_action", "blocked_configuration"}:
            state = "needs_user_decision" if value["failure_code"] in {
                "budget_exhausted",
                "candidate_budget_reserve_insufficient",
                "publication_tier_not_satisfied",
                "degraded_preview_only",
                "soft_deadline_reached",
            } else "blocked"
            return self._update_manifest(
                manifest,
                state=state,
                blocker={"code": value["failure_code"], "message": value["message"], "stage": value["stage"]},
                allowed_next_actions=["resume", "cancel"],
            )
        if value["failure_class"] in {"case_spec_invalid", "verification_failed", "render_sync_failed", "quality_gate_failed"}:
            return self._update_manifest(
                manifest,
                state="needs_user_decision",
                blocker={"code": value["failure_code"], "message": value["message"], "stage": value["stage"]},
                allowed_next_actions=["resume_with_revision", "cancel"],
            )
        return self._update_manifest(
            manifest,
            state="failed",
            blocker={"code": value["failure_code"], "message": value["message"], "stage": value["stage"]},
            allowed_next_actions=["inspect_artifacts"],
        )

    def _exception_stage_result(
        self,
        manifest: Mapping[str, Any],
        stage: str,
        exc: BaseException,
        *,
        stage_result_snapshot: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        persisted = getattr(exc, "stage_result", None)
        if isinstance(persisted, Mapping) and persisted.get("status") != "completed":
            return self._with_identity(persisted, manifest["job_id"], manifest.get("current_attempt_id"))
        stage_dir = self._stage_artifact_root(manifest)
        hint = str(getattr(exc, "_harness_stage", "") or "")
        candidates: list[tuple[Path, dict[str, Any]]] = []
        before = dict(stage_result_snapshot or {})
        for path in sorted(stage_dir.rglob("stage_results/*.json")):
            try:
                raw = read_json(path)
                parsed = StageResult.from_dict(raw).to_dict()
            except (OSError, ValueError, TypeError):
                continue
            if parsed["status"] == "completed":
                continue
            changed = before.get(str(path)) != self._sha256_file(path)
            if changed or (hint and parsed["stage"] == hint):
                candidates.append((path, parsed))
        if candidates:
            order = {
                "generation": 0,
                "provider": 1,
                "compile": 2,
                "preflight": 3,
                "execute": 4,
                "verifier": 5,
                "render_sync": 6,
                "quality_gate": 7,
                "evidence_bundle": 8,
                "semantic_review": 9,
            }
            _, selected = max(
                candidates,
                key=lambda item: (
                    item[1]["stage"] == hint,
                    order.get(item[1]["stage"], -1),
                    str(item[0]),
                ),
            )
            return self._with_identity(selected, manifest["job_id"], manifest.get("current_attempt_id"))
        return failure_stage_result(
            stage=stage,
            failure_code=str(getattr(exc, "code", f"{stage}_unhandled_exception")),
            message=str(exc) or type(exc).__name__,
            retryable=getattr(exc, "retryable", None),
            source_status="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else None,
            job_id=manifest["job_id"],
            attempt_id=manifest.get("current_attempt_id"),
            checkpoint_ref=getattr(exc, "checkpoint_ref", None),
            request_identities=list(getattr(exc, "request_identities", []) or []),
        )

    def _stage_result_snapshot(self, manifest: Mapping[str, Any]) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for path in self._stage_artifact_root(manifest).rglob("stage_results/*.json"):
            try:
                snapshot[str(path)] = self._sha256_file(path)
            except OSError:
                continue
        return snapshot

    @staticmethod
    def _attach_exception_stage_result(exc: BaseException, root: Path, stage: str) -> None:
        candidates = ("preflight", stage) if stage == "execute" else (stage,)
        for actual_stage in candidates:
            path = root / "stage_results" / f"{actual_stage}.json"
            if not path.is_file():
                continue
            try:
                result = StageResult.from_dict(read_json(path)).to_dict()
            except (OSError, ValueError, TypeError):
                continue
            if result["status"] != "completed":
                setattr(exc, "stage_result", result)
                setattr(exc, "_harness_stage", actual_stage)
                return

    def _write_controller_stage_result(self, manifest: Mapping[str, Any], result: Mapping[str, Any]) -> Path:
        return write_stage_result(self._stage_artifact_root(manifest), result)

    def _stage_artifact_root(self, manifest: Mapping[str, Any]) -> Path:
        attempt_id = manifest.get("current_attempt_id")
        return self.store.attempt_dir(manifest["job_id"], attempt_id) if attempt_id else self.store.job_dir(manifest["job_id"])

    def _checkpoint(
        self,
        manifest: Mapping[str, Any],
        stage: str,
        status: str,
        input_digest: str,
        refs: list[str] | None = None,
    ) -> None:
        payload = checkpoint_payload(
            job_id=manifest["job_id"],
            attempt_id=manifest.get("current_attempt_id"),
            stage=stage,
            status=status,
            input_digest=input_digest,
            artifact_refs=[{"name": Path(path).name, "path": path} for path in refs or []],
        )
        path = self.store.write_checkpoint(manifest["job_id"], stage, payload)
        self._emit(
            manifest["job_id"],
            "checkpoint",
            stage=stage,
            attempt_id=manifest.get("current_attempt_id"),
            status=status,
            checkpoint_ref=str(path),
        )

    def _stage_event(self, manifest: Mapping[str, Any], event: str, stage: str, *, result: Mapping[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"stage": stage, "attempt_id": manifest.get("current_attempt_id")}
        if result is not None:
            payload["result"] = dict(result)
        self._emit(manifest["job_id"], event, **payload)

    def _emit(self, job_id: str, event_type: str, **payload: Any) -> None:
        event = {
            "schema_version": "harness_agent_job_event_v1",
            "event": event_type,
            "job_id": job_id,
            "timestamp": utc_now(),
            **payload,
        }
        self.store.append_event(job_id, event)
        if self.event_sink is not None:
            self.event_sink(event)

    def _update_manifest(self, manifest: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
        updated = copy.deepcopy(dict(manifest))
        updated.update(changes)
        updated["updated_at"] = utc_now()
        validated = JobManifest.from_dict(updated).to_dict()
        self.store.write_manifest(validated)
        return validated

    def _add_active_elapsed(self, manifest: Mapping[str, Any], elapsed: float) -> dict[str, Any]:
        current = self.store.load_manifest(manifest["job_id"])
        usage = copy.deepcopy(current["usage"])
        usage["active_elapsed_seconds"] = round(float(usage["active_elapsed_seconds"]) + max(0.0, elapsed), 6)
        return self._update_manifest(current, usage=usage)

    def _load_current_case_spec(self, manifest: Mapping[str, Any]) -> CaseSpecV2:
        attempt_id = str(manifest.get("current_attempt_id") or "")
        validate_attempt_id(attempt_id)
        request = read_json(self.store.job_dir(manifest["job_id"]) / "request" / "user_request.json")
        return case_spec_v2_from_dict(
            read_json(self.store.attempt_dir(manifest["job_id"], attempt_id) / "case_spec.json"),
            available_input_ids=[str(row.get("input_id")) for row in request.get("inputs") or []],
        )

    def _stage_input_digest(self, manifest: Mapping[str, Any], stage: str) -> str:
        if manifest.get("current_attempt_id"):
            attempt = self.store.load_attempt(manifest["job_id"], manifest["current_attempt_id"])
            return stable_digest({"stage": stage, "case_spec_digest": attempt["case_spec_digest"]})
        return stable_digest({"stage": stage, "request_digest": manifest["request_digest"]})

    def _registry(self) -> AssetRegistry:
        return AssetRegistry(self.config.catalog)

    def _effective_provider_input_manifest(self, job_id: str) -> dict[str, Any] | None:
        request_root = self.store.job_dir(job_id) / "request"
        effective = sorted(request_root.glob("provider_input_manifest_effective_*.json"))
        path = effective[-1] if effective else request_root / "provider_input_manifest.json"
        if not path.is_file():
            return None
        return self._validate_provider_manifest(read_json(path))

    def _provider_orchestrator(self, manifest: Mapping[str, Any]) -> AssetProviderOrchestrator:
        return AssetProviderOrchestrator(
            workspace=self.store.workspace,
            remote_providers={
                "model_generation": MeshyModelGenerationAdapter(api_key=self.config.meshy_api_key()),
                "external_site": PolyHavenExternalSiteAdapter(),
            },
            max_paid_submissions=int(manifest["budget"]["max_paid_submissions"]),
            paid_submission_ledger_path=(
                self.store.job_dir(manifest["job_id"]) / "receipts" / "provider_usage.json"
            ),
        )

    def _reconcile_provider_usage(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        attempt_id = manifest.get("current_attempt_id")
        if not attempt_id:
            return dict(manifest)
        compilation_dir = self.store.attempt_dir(manifest["job_id"], attempt_id) / "compilation"
        batch_path = compilation_dir / "asset_provider_batch.json"
        existing_path = self.store.job_dir(manifest["job_id"]) / "receipts" / "provider_usage.json"
        usage_record = read_json(existing_path) if existing_path.is_file() else {
            "schema_version": "harness_agent_provider_usage_v1",
            "requests": {},
        }
        requests = usage_record.get("requests") if isinstance(usage_record.get("requests"), dict) else {}
        if batch_path.is_file():
            batch = read_json(batch_path)
            for request in batch.get("requests") or []:
                if not isinstance(request, Mapping) or request.get("route") != "model_generation":
                    continue
                digest = str(request.get("request_digest") or "")
                provider_root = self.store.workspace / "providers" / "meshy_model_generation_v1" / digest
                checkpoint_path = provider_root / "task_checkpoint.json"
                submission_path = provider_root / "submission_attempt.json"
                checkpoint = read_json(checkpoint_path) if checkpoint_path.is_file() else {}
                submission = read_json(submission_path) if submission_path.is_file() else {}
                state = str(submission.get("state") or "")
                submitted = bool(checkpoint.get("task_id")) or state in {"attempting", "unknown", "acknowledged"}
                if submitted:
                    requests[digest] = {
                        "request_digest": digest,
                        "task_id": checkpoint.get("task_id"),
                        "submission_state": state or "checkpointed",
                        "checkpoint": str(checkpoint_path) if checkpoint_path.is_file() else None,
                    }
        usage_record["requests"] = requests
        write_json(existing_path, usage_record)
        current = self.store.load_manifest(manifest["job_id"])
        counter = copy.deepcopy(current["usage"])
        counter["paid_submissions"] = len(requests)
        return self._update_manifest(current, usage=counter)

    def _execution_fingerprint(self, case_spec: Mapping[str, Any], compilation: RuntimeCompilation) -> str:
        return stable_digest(
            {
                "case_spec": case_spec,
                "asset_resolution": compilation.artifacts.get("asset_resolution"),
                "scene_layout": compilation.artifacts.get("scene_layout"),
                "runtime_actor_placement": compilation.artifacts.get("runtime_actor_placement"),
                "runtime_plan": compilation.artifacts.get("runtime_plan"),
            }
        )

    @staticmethod
    def _run_technical_pass(run_dir: Path) -> bool:
        if not run_dir.is_dir():
            return False
        required = ("execute", "verifier", "render_sync")
        try:
            stages = {
                stage: StageResult.from_dict(read_json(run_dir / "stage_results" / f"{stage}.json")).to_dict()
                for stage in required
            }
        except (OSError, ValueError, TypeError):
            return False
        return all(value["status"] == "completed" for value in stages.values()) and verified_run_status(run_dir) == "pass"

    @staticmethod
    def _with_identity(result: Mapping[str, Any], job_id: str, attempt_id: str | None) -> dict[str, Any]:
        value = dict(result)
        value["job_id"] = job_id
        value["attempt_id"] = attempt_id
        return StageResult.from_dict(value).to_dict()

    @staticmethod
    def _adopt_run_stage_results(run_dir: Path, job_id: str, attempt_id: str) -> None:
        for path in sorted((run_dir / "stage_results").glob("*.json")):
            value = read_json(path)
            value["job_id"] = job_id
            value["attempt_id"] = attempt_id
            write_stage_result(run_dir, StageResult.from_dict(value).to_dict())

    @staticmethod
    def _provider_routes(case_spec: CaseSpecV2) -> set[str]:
        routes = set()
        for obj in case_spec.objects:
            for request in asset_requests(obj.get("asset")):
                acquisition = request.get("acquisition") if isinstance(request.get("acquisition"), Mapping) else {}
                routes.add(str(acquisition.get("route") or "default"))
        return routes

    @staticmethod
    def _meshy_image_references(case_spec: CaseSpecV2) -> list[dict[str, Any]]:
        rows = []
        for obj in case_spec.objects:
            for request in asset_requests(obj.get("asset")):
                acquisition = request.get("acquisition") if isinstance(request.get("acquisition"), Mapping) else {}
                if acquisition.get("route") == "model_generation":
                    rows.extend([dict(row) for row in acquisition.get("reference_inputs") or [] if isinstance(row, Mapping)])
        return rows

    @staticmethod
    def _validate_request(request: Mapping[str, Any]) -> dict[str, Any]:
        data = copy.deepcopy(dict(request))
        if data.get("schema_version") != REQUEST_SCHEMA_VERSION:
            raise ValueError(f"request schema_version must be {REQUEST_SCHEMA_VERSION}")
        if not isinstance(data.get("inputs"), list) or any(not isinstance(row, Mapping) for row in data["inputs"]):
            raise ValueError("request.inputs must be a list of objects")
        if not str(data.get("text") or "").strip() and not data["inputs"]:
            raise ValueError("request requires text, an image, or both")
        data["planning_image_requirement"] = normalize_planning_image_requirement(data)
        return data

    @staticmethod
    def _validate_provider_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
        data = copy.deepcopy(dict(value))
        if data.get("schema_version") != PROVIDER_INPUT_MANIFEST_SCHEMA:
            raise ValueError("unsupported Provider input manifest schema")
        if not isinstance(data.get("inputs"), list):
            raise ValueError("Provider input manifest inputs must be a list")
        return data

    @staticmethod
    def _new_job_id() -> str:
        return f"job_{int(time.time()):x}_{secrets.token_hex(6)}"

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    @contextmanager
    def _profile_environment(values: Mapping[str, str]):
        prior = {key: os.environ.get(key) for key in values}
        os.environ.update(values)
        try:
            yield
        finally:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    @contextmanager
    def _effective_environment(self):
        values = {
            "SIM_HARNESS_WORKSPACE": str(self.config.workspace),
            "SIM_HARNESS_ASSET_CATALOG": str(self.config.catalog),
            "SIM_STUDIO_UE_PROJECT": str(self.config.ue_project),
        }
        if self.config.ue_executable is not None:
            values["SIM_STUDIO_UE_EXECUTABLE"] = str(self.config.ue_executable)
        if self.config.codex_executable is not None:
            values["SIM_HARNESS_CODEX_EXECUTABLE"] = str(self.config.codex_executable)
        with self._profile_environment(values):
            yield

    @staticmethod
    def _intent_recovery_identity(contract: Mapping[str, Any]) -> dict[str, Any]:
        identity = copy.deepcopy(dict(contract))
        identity.pop("created_at", None)
        identity.pop("schema_version", None)
        # These controller-policy fields were tightened after the first M2
        # release. They do not change the generated CaseSpec or frozen request
        # identity, so an already committed valid Contract remains recoverable.
        identity.pop("allowed_adjustments", None)
        for ambiguity in identity.get("ambiguities") or []:
            if isinstance(ambiguity, dict):
                ambiguity.pop("ambiguity_id", None)
        return identity

    @classmethod
    def _json_diff(cls, before: Any, after: Any, path: str = "$") -> list[dict[str, Any]]:
        if before == after:
            return []
        if isinstance(before, Mapping) and isinstance(after, Mapping):
            changes = []
            for key in sorted(set(before).union(after)):
                child = f"{path}.{key}"
                if key not in before:
                    changes.append({"path": child, "operation": "add", "before": None, "after": after[key]})
                elif key not in after:
                    changes.append({"path": child, "operation": "remove", "before": before[key], "after": None})
                else:
                    changes.extend(cls._json_diff(before[key], after[key], child))
            return changes
        return [{"path": path, "operation": "replace", "before": before, "after": after}]

    @staticmethod
    def _validate_revision_changes(
        changes: list[dict[str, Any]],
        allowed: Mapping[str, Any],
        *,
        repair_layer: str,
        suggested_paths: list[str] | None = None,
    ) -> None:
        allowed_paths = [str(path) for path in allowed.get("paths") or [] if str(path).startswith("$.")]
        disallowed = [
            str(change.get("path") or "")
            for change in changes
            if str(change.get("path") or "") not in allowed_paths
        ]
        if disallowed:
            raise JobStoreError(f"revision changes paths outside Intent Contract allowed_adjustments: {disallowed}")
        ranges = allowed.get("ranges") if isinstance(allowed.get("ranges"), Mapping) else {}
        for change in changes:
            path = str(change["path"])
            constraint = ranges.get(path)
            if not isinstance(constraint, Mapping):
                raise JobStoreError(f"Intent Contract range for {path} is missing or invalid")
            value = change.get("after")
            kind = constraint.get("kind")
            if kind == "numeric":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise JobStoreError(f"revision value at {path} is outside its numeric adjustment contract")
                if value < constraint["min"] or value > constraint["max"]:
                    raise JobStoreError(f"revision value at {path} exceeds Intent Contract allowed range")
            elif kind == "list":
                if not isinstance(value, list) or not constraint["min_items"] <= len(value) <= constraint["max_items"]:
                    raise JobStoreError(f"revision value at {path} exceeds Intent Contract list range")
            elif kind == "enum":
                if value not in constraint["values"]:
                    raise JobStoreError(f"revision value at {path} is outside Intent Contract enum values")
            else:
                raise JobStoreError(f"Intent Contract range for {path} is invalid")
        if repair_layer == "observation" and any(
            not str(change["path"]).startswith("$.observation_requirements") for change in changes
        ):
            raise JobStoreError("observation repair may only change observation_requirements source intent")
        if repair_layer == "camera" and any(
            not (
                str(change["path"]).startswith("$.observation_requirements")
                or str(change["path"]).startswith("$.scene.camera")
            )
            for change in changes
        ):
            raise JobStoreError("camera repair may only change camera/observation source intent")
        if suggested_paths is not None:
            suggested = set(suggested_paths)
            if len(suggested) != len(suggested_paths):
                raise JobStoreError("Reviewer suggested adjustment paths must be unique")
            unsuggested = [
                str(change.get("path") or "")
                for change in changes
                if str(change.get("path") or "") not in suggested
            ]
            if unsuggested:
                raise JobStoreError(
                    f"revision changes paths not suggested by the current Semantic Review: {unsuggested}"
                )

    def _technical_completion_intact(
        self,
        manifest: Mapping[str, Any],
        bundle: Mapping[str, Any],
    ) -> bool:
        try:
            current = self._validated_current_bundle(
                manifest,
                expected_manifest_digest=stable_digest(bundle),
            )
        except (OSError, ValueError, TypeError, KeyError, EvidenceBundleError):
            return False
        return current == dict(bundle)

    def _validated_current_bundle(
        self,
        manifest: Mapping[str, Any],
        *,
        expected_manifest_digest: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self._validated_current_bundle_from_files(
                manifest,
                expected_manifest_digest=expected_manifest_digest,
            )
        except EvidenceBundleError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise EvidenceBundleError(
                "evidence_bundle_validation_failed",
                "Current Job inputs for the Evidence Bundle cannot be validated",
            ) from exc

    def _validated_current_bundle_from_files(
        self,
        manifest: Mapping[str, Any],
        *,
        expected_manifest_digest: str | None,
    ) -> dict[str, Any]:
        job_id = str(manifest["job_id"])
        attempt_id = str(manifest["current_attempt_id"])
        attempt_dir = self.store.attempt_dir(job_id, attempt_id)
        attempt = self.store.load_attempt(job_id, attempt_id)
        request_root = self.store.job_dir(job_id) / "request"
        request = read_json(request_root / "user_request.json")
        intent = IntentContract.from_dict(read_json(request_root / "intent_contract.json")).to_dict()
        amendments = [
            read_json(path)
            for path in sorted(
                [*request_root.glob("intent_amendment_*.json"), *request_root.glob("authorization_amendment_*.json")],
                key=lambda path: path.name,
            )
        ]
        snapshots = current_evidence_snapshots(
            attempt_dir=attempt_dir,
            request=request,
            intent_contract=intent,
            intent_amendments=amendments,
        )
        return validate_current_evidence_bundle(
            manifest_path=attempt_dir / "evidence_bundle" / "manifest.json",
            job_id=job_id,
            attempt=attempt,
            attempt_dir=attempt_dir,
            expected_intent_contract_digest=str(manifest["intent_contract_digest"]),
            expected_snapshots=snapshots,
            expected_manifest_digest=expected_manifest_digest,
        )

    def _reviewer_reservations(
        self,
        attempt_dir: Path,
        *,
        job_id: str,
        attempt_id: str,
        bundle_digest: str,
        input_digest: str,
    ) -> list[dict[str, Any]]:
        reservations: list[dict[str, Any]] = []
        for expected_count, path in enumerate(sorted(attempt_dir.glob("reviewer_reservation_*.json")), start=1):
            reservation = ReviewerInvocationReservation.from_dict(read_json(path)).to_dict()
            identity_core = {
                "job_id": reservation["job_id"],
                "attempt_id": reservation["attempt_id"],
                "invocation_count": reservation["invocation_count"],
                "role": reservation["role"],
                "bundle_digest": reservation["bundle_digest"],
                "input_digest": reservation["input_digest"],
            }
            if (
                reservation["job_id"] != job_id
                or reservation["attempt_id"] != attempt_id
                or reservation["invocation_count"] != expected_count
                or reservation["bundle_digest"] != bundle_digest
                or reservation["input_digest"] != input_digest
                or reservation["invocation_id"] != f"reviewer_{stable_digest(identity_core)[:16]}"
            ):
                raise JobStoreError("Reviewer invocation reservation identity does not match the current attempt")
            receipt_path = attempt_dir / f"reviewer_invocation_{expected_count:03d}.json"
            if reservation["state"] == "receipt_recorded" and not receipt_path.is_file():
                raise JobStoreError("Reviewer reservation records a receipt that is missing")
            if reservation["state"] == "receipt_recorded" and reservation["receipt_path"] != receipt_path.name:
                raise JobStoreError("Reviewer reservation receipt path is inconsistent")
            if receipt_path.is_file():
                receipt = ReviewerInvocationReceipt.from_dict(read_json(receipt_path)).to_dict()
                if (
                    receipt["job_id"] != job_id
                    or receipt["attempt_id"] != attempt_id
                    or receipt["invocation_count"] != expected_count
                    or receipt["input_digest"] != input_digest
                ):
                    raise JobStoreError("Reviewer receipt identity does not match its reservation")
                if reservation["state"] != "receipt_recorded":
                    if receipt["status"] == "interrupted":
                        outcome, retryable, error_code = "interrupted", False, "reviewer_interrupted"
                    elif receipt["status"] == "failed" and classify_failure(
                        "semantic_review", str(receipt.get("error_code") or "reviewer_failed")
                    )["failure_class"] == "blocked_configuration":
                        outcome, retryable, error_code = "blocked_configuration", False, str(receipt["error_code"])
                    else:
                        outcome, retryable, error_code = (
                            "technical_failed",
                            True,
                            str(receipt.get("error_code") or "reviewer_completed_output_uncommitted"),
                        )
                    reservation = self._update_reviewer_reservation(
                        attempt_dir,
                        reservation,
                        state="receipt_recorded",
                        outcome=outcome,
                        retryable=retryable,
                        receipt_path=receipt_path.name,
                        error_code=error_code,
                    )
            elif reservation["state"] in {"launching", "started", "output_received"}:
                reservation = self._update_reviewer_reservation(
                    attempt_dir,
                    reservation,
                    state="completion_unknown",
                    outcome="completion_unknown",
                    retryable=True,
                    error_code="reviewer_completion_unknown",
                )
            reservations.append(reservation)
        receipt_counts = {
            int(path.stem.rsplit("_", 1)[1])
            for path in attempt_dir.glob("reviewer_invocation_*.json")
            if path.stem.rsplit("_", 1)[-1].isdigit()
        }
        if any(count > len(reservations) for count in receipt_counts):
            raise JobStoreError("Reviewer receipt exists without a durable invocation reservation")
        return reservations

    def _next_reviewer_reservation(
        self,
        manifest: Mapping[str, Any],
        *,
        attempt_dir: Path,
        attempt_id: str,
        bundle_digest: str,
        input_digest: str,
        reservations: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        primary_limit = min(1, int(manifest["budget"]["max_reviewer_invocations"]))
        technical_limit = int(manifest["budget"]["max_reviewer_technical_retries"])
        launched = sum(bool(row["usage_counted"]) for row in reservations)
        launch_limit = primary_limit + technical_limit if primary_limit else 0
        role: str | None = None
        if reservations:
            latest = reservations[-1]
            if latest["state"] == "reserved":
                return latest
            if latest["outcome"] in {"interrupted", "blocked_configuration"}:
                if launched < launch_limit:
                    role = "primary" if launched < primary_limit else "resume"
            elif latest["outcome"] in {"technical_failed", "completion_unknown"} and latest["retryable"] is True:
                if (
                    launched < launch_limit
                    and sum(row["role"] == "technical_retry" and row["usage_counted"] for row in reservations)
                    < technical_limit
                    and int(manifest["usage"]["total_retries"]) < int(manifest["budget"]["max_total_retries"])
                    and self._budget_gate(manifest) is None
                ):
                    role = "technical_retry"
            else:
                return None
        elif primary_limit:
            role = "primary"
        if role is None:
            return None
        invocation_count = len(reservations) + 1
        core = {
            "job_id": manifest["job_id"],
            "attempt_id": attempt_id,
            "invocation_count": invocation_count,
            "role": role,
            "bundle_digest": bundle_digest,
            "input_digest": input_digest,
        }
        now = utc_now()
        reservation = ReviewerInvocationReservation.from_dict(
            {
                "schema_version": REVIEWER_INVOCATION_SCHEMA_VERSION,
                **core,
                "invocation_id": f"reviewer_{stable_digest(core)[:16]}",
                "state": "reserved",
                "outcome": "pending",
                "usage_counted": False,
                "retryable": None,
                "receipt_path": None,
                "error_code": None,
                "created_at": now,
                "updated_at": now,
            }
        ).to_dict()
        write_json(attempt_dir / f"reviewer_reservation_{invocation_count:03d}.json", reservation)
        reservations.append(reservation)
        return reservation

    def _reconcile_reviewer_usage(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        """Rebuild the Job audit count from durable actual-launch markers."""
        job_id = str(manifest["job_id"])
        launched = 0
        attempts_root = self.store.job_dir(job_id) / "attempts"
        for attempt_dir in sorted(path for path in attempts_root.glob("attempt_*") if path.is_dir()):
            expected_attempt_id = attempt_dir.name
            for expected_count, path in enumerate(
                sorted(attempt_dir.glob("reviewer_reservation_*.json")),
                start=1,
            ):
                reservation = ReviewerInvocationReservation.from_dict(read_json(path)).to_dict()
                if (
                    reservation["job_id"] != job_id
                    or reservation["attempt_id"] != expected_attempt_id
                    or reservation["invocation_count"] != expected_count
                ):
                    raise JobStoreError("Reviewer reservation sequence cannot rebuild Job usage")
                launched += int(reservation["usage_counted"])
        if int(manifest["usage"]["reviewer_invocations"]) == launched:
            return dict(manifest)
        usage = copy.deepcopy(manifest["usage"])
        usage["reviewer_invocations"] = launched
        return self._update_manifest(manifest, usage=usage)

    @staticmethod
    def _update_reviewer_reservation(
        attempt_dir: Path,
        reservation: Mapping[str, Any],
        **changes: Any,
    ) -> dict[str, Any]:
        updated = {**dict(reservation), **changes, "updated_at": utc_now()}
        value = ReviewerInvocationReservation.from_dict(updated).to_dict()
        write_json(attempt_dir / f"reviewer_reservation_{value['invocation_count']:03d}.json", value)
        return value

    @staticmethod
    def _validate_reviewer_receipt(
        raw: Mapping[str, Any],
        *,
        job_id: str,
        attempt_id: str,
        invocation_count: int,
        bundle_dir: Path,
        expected_input_digest: str,
        expected_output_digest: str | None,
        require_completed: bool,
    ) -> dict[str, Any]:
        try:
            receipt = ReviewerInvocationReceipt.from_dict(raw).to_dict()
        except (ValueError, TypeError, KeyError) as exc:
            raise JobStoreError(f"Reviewer receipt schema is invalid: {exc}") from exc
        expected_profile = reviewer_permission_profile(
            job_id=job_id,
            attempt_id=attempt_id,
            invocation_count=invocation_count,
            bundle_dir=bundle_dir,
        )
        if (
            receipt["job_id"] != job_id
            or receipt["attempt_id"] != attempt_id
            or receipt["invocation_count"] != invocation_count
            or receipt["input_digest"] != expected_input_digest
            or receipt["requested_permission_profile"] != expected_profile
            or receipt["active_permission_profile_id"] not in {None, expected_profile["id"]}
        ):
            raise JobStoreError("Reviewer receipt identity does not match the current invocation")
        if receipt["instruction_sources"]:
            raise JobStoreError("Reviewer receipt must not include inherited instruction sources")
        if require_completed:
            if receipt["status"] != "completed" or receipt["output_digest"] != expected_output_digest:
                raise JobStoreError("Reviewer output digest does not match the current invocation")
        elif receipt["status"] == "completed":
            raise JobStoreError("a technical Reviewer error cannot carry a completed receipt")
        return receipt

    @staticmethod
    def _path_chain_has_symlink(path: Path, root: Path) -> bool:
        raw_path = path if path.is_absolute() else path.absolute()
        raw_root = root if root.is_absolute() else root.absolute()
        try:
            relative = raw_path.relative_to(raw_root)
        except ValueError:
            return True
        current = raw_root.resolve(strict=True)
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return True
        return False

    def _publication_tier_satisfied(self, manifest: Mapping[str, Any]) -> bool:
        if manifest["target"]["publication_tier"] != "reference":
            return True
        attempt_dir = self.store.attempt_dir(manifest["job_id"], manifest["current_attempt_id"])
        candidate = read_json(attempt_dir / "candidate_run.json")
        quality = read_json(Path(str(candidate["run_dir"])) / "quality_report.json")
        readiness = ((quality.get("source_reports") or {}).get("run_readiness") or {})
        return readiness.get("reference_ready") is True

    def _load_validated_semantic_review(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        attempt_dir = self.store.attempt_dir(manifest["job_id"], manifest["current_attempt_id"])
        try:
            bundle = self._validated_current_bundle(manifest)
        except EvidenceBundleError as exc:
            raise JobStoreError(str(exc)) from exc
        intent = IntentContract.from_dict(
            read_json(self.store.job_dir(manifest["job_id"]) / "request" / "intent_contract.json")
        ).to_dict()
        expected_requirements = self._semantic_review_requirements(str(manifest["job_id"]), intent)
        review = SemanticReview.from_dict(
            read_json(attempt_dir / "semantic_review.json"),
            expected_requirement_ids={str(row["id"]) for row in expected_requirements},
            evidence_artifact_ids={str(row["artifact_id"]) for row in bundle["artifacts"]},
            evidence_manifest=bundle,
        ).to_dict()
        if review["job_id"] != manifest["job_id"] or review["attempt_id"] != manifest["current_attempt_id"]:
            raise JobStoreError("Semantic Review identity does not match the current attempt")
        if review["evidence_bundle_digest"] != stable_digest(bundle):
            raise JobStoreError("Semantic Review Evidence Bundle digest no longer matches")
        request = read_json(self.store.job_dir(manifest["job_id"]) / "request" / "user_request.json")
        has_images = any(
            isinstance(row, Mapping) and row.get("kind") == "image"
            for row in request.get("inputs") or []
        )
        expected_input_digest = semantic_reviewer_input_digest(
            bundle_dir=attempt_dir / "evidence_bundle",
            bundle_manifest=bundle,
            include_original_images=has_images,
        )
        raw_output = {
            key: review[key]
            for key in ("overall_status", "requirements", "repair_layer", "summary", "suggested_adjustments")
        }
        receipts = []
        for path in sorted(attempt_dir.glob("reviewer_invocation_*.json")):
            receipt = ReviewerInvocationReceipt.from_dict(read_json(path)).to_dict()
            if stable_digest(receipt) == review["reviewer_receipt_digest"]:
                try:
                    recorded_invocation_count = int(path.stem.rsplit("_", 1)[1])
                except (ValueError, IndexError) as exc:
                    raise JobStoreError("Reviewer receipt filename does not identify its invocation") from exc
                receipts.append(
                    self._validate_reviewer_receipt(
                        receipt,
                        job_id=str(manifest["job_id"]),
                        attempt_id=str(manifest["current_attempt_id"]),
                        invocation_count=recorded_invocation_count,
                        bundle_dir=attempt_dir / "evidence_bundle",
                        expected_input_digest=expected_input_digest,
                        expected_output_digest=stable_digest(raw_output),
                        require_completed=True,
                    )
                )
        if len(receipts) != 1:
            raise JobStoreError("Semantic Review does not identify exactly one valid Reviewer receipt")
        return review

    def _semantic_review_requirements(
        self,
        job_id: str,
        intent: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        request_root = self.store.job_dir(job_id) / "request"
        amendments = [
            read_json(path)
            for path in sorted(request_root.glob("intent_amendment_*.json"), key=lambda value: value.name)
        ]
        return semantic_review_requirements(intent, amendments)

    @staticmethod
    def _semantic_repair_allowed(intent: Mapping[str, Any], review: Mapping[str, Any]) -> bool:
        if review.get("repair_layer") not in {"observation", "camera", "case_spec_source"}:
            return False
        suggestions = review.get("suggested_adjustments") or []
        if not suggestions:
            return False
        allowed_paths = [
            str(value)
            for value in AgentJobController._effective_allowed_adjustments(intent).get("paths") or []
        ]
        return all(
            isinstance(suggestion, Mapping)
            and str(suggestion.get("path") or "") in allowed_paths
            for suggestion in suggestions
        )

    @staticmethod
    def _effective_allowed_adjustments(intent: Mapping[str, Any]) -> dict[str, Any]:
        if intent.get("schema_version") not in {
            PROJECTED_INTENT_CONTRACT_SCHEMA_VERSION,
            INTENT_CONTRACT_SCHEMA_VERSION,
        }:
            return {"paths": [], "ranges": {}}
        allowed = intent.get("allowed_adjustments")
        return dict(allowed) if isinstance(allowed, Mapping) else {"paths": [], "ranges": {}}

    @staticmethod
    def _smoke_mode(attempt_dir: Path) -> str:
        del attempt_dir
        # The current executor runs the complete runtime plan. A future
        # observation-only replay may return "targeted" only after it writes a
        # parent-run/cache receipt proving that the solver was not rerun.
        return "executed"
