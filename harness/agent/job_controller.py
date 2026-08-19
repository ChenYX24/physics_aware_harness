from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import secrets
import shlex
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
    TERMINAL_JOB_STATES,
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
    NATIVE_GENERATION_CONTEXT_IDENTITY_SCHEMA_VERSION,
    NativeGenerationValidationError,
    build_native_generation_ack,
    build_native_generation_context,
    build_native_generation_context_identity,
    generation_policy,
    validate_generation_policy,
    validate_native_generation_ack,
    validate_native_generation_context,
    validate_native_generation_context_identity,
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
from harness.assets.providers.local_procedural_mesh import RECIPE_BY_SHAPE
from harness.assets.providers.remote import MeshyModelGenerationAdapter, PolyHavenExternalSiteAdapter
from harness.core.artifact_schema import read_json, write_json
from harness.core.harness_config import EffectiveHarnessConfig, load_harness_config
from harness.core.case_spec_v2 import (
    CaseSpecV2,
    CaseSpecV2ValidationError,
    asset_requests,
    case_spec_v2_from_dict,
    compile_case_spec_v2_runtime,
    validate_agent_case_spec_contract,
)
from harness.core.stage_result import (
    StageResult,
    artifact_ref,
    build_stage_result,
    classify_failure,
    failure_stage_result,
    stage_result_from_provider_batch,
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

_NEXT_CONTROLLER_STAGE = {
    "intake_readiness": "generation",
    "generation": "task_readiness",
    "task_readiness": "compile",
    "compile": "smoke",
    "smoke": "candidate",
    "candidate": "quality_gate",
    "quality_gate": "evidence_bundle",
    "evidence_bundle": "semantic_review",
}

_CASE_SPEC_CONTRACT_FAILURE_CODES = frozenset(
    {"unsupported_generation_recipe", "invalid_generation_spec"}
)
_CANONICAL_OBJECT_PATH_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

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
        target_profile = (
            "local_preview"
            if publication_tier in {"diagnostic_only", "local_preview"}
            else "candidate"
        )
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
                "target": {"execution_profile": target_profile, "publication_tier": publication_tier},
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
        context_identity_path = root / "request" / "native_generation_context_identity.json"
        context_digest = None
        if context_identity_path.is_file():
            identity = read_json(context_identity_path)
            if isinstance(identity, Mapping):
                context_digest = identity.get("context_digest")
        elif context_path.is_file():
            context_digest = stable_digest(read_json(context_path))
        attempts = []
        for path in sorted((root / "attempts").glob("attempt_*/attempt_manifest.json")):
            attempts.append(AttemptManifest.from_dict(read_json(path)).to_dict())
        current_leaf_stage_result = self._current_leaf_stage_result(manifest)
        case_spec_contract_repair = self._case_spec_contract_repair_eligibility(
            manifest,
            current_leaf_stage_result=current_leaf_stage_result,
        )
        effective_revision_policy = {
            "available": False,
            "allowed_adjustments": {"paths": [], "ranges": {}},
            "message": "job has no current CaseSpec revision policy",
        }
        current_attempt_id = manifest.get("current_attempt_id")
        intent_path = root / "request" / "intent_contract.json"
        if current_attempt_id is not None and intent_path.is_file():
            case_spec_path = self.store.attempt_dir(job_id, str(current_attempt_id)) / "case_spec.json"
            if case_spec_path.is_file():
                case_spec = read_json(case_spec_path)
                intent = IntentContract.from_dict(read_json(intent_path)).to_dict()
                allowed = self._effective_allowed_adjustments(intent)
                if intent.get("schema_version") == INTENT_CONTRACT_SCHEMA_VERSION:
                    allowed = self._overlay_allowed_adjustments(
                        self._case_spec_revision_policy(
                            case_spec,
                            excluded_paths=self._native_hard_parameter_paths(job_id),
                        ),
                        allowed,
                    )
                remaining_revisions = max(
                    0,
                    int(manifest["budget"]["max_case_spec_revisions"])
                    - int(manifest["usage"]["case_spec_revisions"]),
                )
                effective_revision_policy = {
                    "available": bool(allowed["paths"]) and remaining_revisions > 0,
                    "allowed_adjustments": allowed,
                    "message": (
                        "revisions may change only these bounded source CaseSpec leaves"
                        if remaining_revisions > 0
                        else "CaseSpec revision budget is exhausted"
                    ),
                }
                if remaining_revisions == 0 and case_spec_contract_repair.get("available") is True:
                    case_spec_contract_repair = {
                        **case_spec_contract_repair,
                        "available": False,
                        "message": "CaseSpec revision budget is exhausted",
                    }
        return {
            "schema_version": "harness_agent_job_inspection_v1",
            "effective_config_digest": self.config.digest,
            "generation_mode": self._generation_policy(job_id)["mode"],
            "native_generation_context_digest": context_digest,
            "job": manifest,
            "attempts": attempts,
            "current_leaf_stage_result": current_leaf_stage_result,
            "failed_stage_retry": self._failed_stage_retry_eligibility(
                manifest,
                current_leaf_stage_result=current_leaf_stage_result,
            ),
            "case_spec_contract_repair": case_spec_contract_repair,
            "case_spec_revision_policy": effective_revision_policy,
            "configuration_recompile": self._configuration_recompile_eligibility(
                manifest,
                current_leaf_stage_result=current_leaf_stage_result,
            ),
            "reviewer_contract_retry": self._reviewer_contract_retry_eligibility(
                manifest,
                current_leaf_stage_result=current_leaf_stage_result,
            ),
            "interrupted_recovery": self._interrupted_recovery(manifest),
            "paths": {
                "job_root": str(root),
                "job_manifest": str(root / "job_manifest.json"),
                "intent_contract": str(root / "request" / "intent_contract.json") if (root / "request" / "intent_contract.json").is_file() else None,
                "native_generation_context": str(root / "request" / "native_generation_context.json") if (root / "request" / "native_generation_context.json").is_file() else None,
                "native_generation_context_identity": str(context_identity_path) if context_identity_path.is_file() else None,
                "native_generation_ack": str(root / "request" / "native_generation_ack.json") if (root / "request" / "native_generation_ack.json").is_file() else None,
            },
        }

    def recover_interrupted(self, job_id: str) -> dict[str, Any]:
        """Return a lock-orphaned running stage to the normal resumable boundary."""
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            recovery = self._interrupted_recovery(manifest)
            if recovery["available"] is not True:
                raise JobStoreError(str(recovery["message"]))
            stage = str(recovery["stage"])
            marker_stage = str(recovery.get("marker_stage") or stage)
            manifest = self._reconcile_in_flight_elapsed(manifest)
            if marker_stage == "compile" or stage == "compile":
                manifest = self._reconcile_provider_usage(manifest)
                manifest = self._reconcile_ue_launch_usage(manifest)
            checkpoint_path = self.store.job_dir(job_id) / "checkpoints" / f"{stage}.json"
            self._checkpoint(
                manifest,
                stage,
                "interrupted",
                self._stage_input_digest(manifest, stage),
            )
            result = failure_stage_result(
                stage=stage,
                failure_code="interrupted",
                message=f"{stage} was recovered after its Controller process ended without closing the stage",
                source_status="interrupted",
                job_id=job_id,
                attempt_id=manifest.get("current_attempt_id"),
                checkpoint_ref=str(checkpoint_path),
            )
            self._write_controller_stage_result(manifest, result)
            manifest = self._apply_stage_result(manifest, result)
            self._finish_in_flight(job_id, marker_stage, "recovered_interrupted")
            self._stage_event(manifest, "stage_blocked", stage, result=result)
            deadline_result = self._hard_deadline_gate(manifest)
            if deadline_result is not None:
                self._write_controller_stage_result(manifest, deadline_result)
                manifest = self._apply_stage_result(manifest, deadline_result)
                self._stage_event(manifest, "stage_blocked", "budget", result=deadline_result)
        return self.inspect(job_id)

    def retry_failed_stage(self, job_id: str, *, reason: str) -> dict[str, Any]:
        """Audit and reopen one terminal transient failure at its existing checkpoint."""
        explanation = str(reason or "").strip()
        if not explanation:
            raise ValueError("failed-stage retry requires a non-empty correction reason")
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            leaf = self._current_leaf_stage_result(manifest)
            eligibility = self._failed_stage_retry_eligibility(
                manifest,
                current_leaf_stage_result=leaf,
            )
            if eligibility["available"] is not True:
                raise JobStoreError(str(eligibility["message"]))
            self._ensure_ue_launch_ledger(manifest)
            manifest = self._reconcile_ue_launch_usage(manifest)
            result = dict(leaf["result"])
            amendments_dir = self.store.job_dir(job_id) / "amendments"
            sequence = len(list(amendments_dir.glob("failed_stage_retry_*.json"))) + 1
            transaction_path = (
                self.store.attempt_dir(job_id, str(manifest["current_attempt_id"]))
                / "compilation"
                / "compilation_transaction.json"
            )
            provider_usage_path = self.store.job_dir(job_id) / "receipts" / "provider_usage.json"
            receipt_path = amendments_dir / f"failed_stage_retry_{sequence:03d}.json"
            write_json(
                receipt_path,
                {
                    "schema_version": "harness_agent_failed_stage_retry_v1",
                    "job_id": job_id,
                    "attempt_id": manifest.get("current_attempt_id"),
                    "controller_stage": manifest["current_stage"],
                    "failed_stage": result["stage"],
                    "failure_code": result["failure_code"],
                    "failure_result_digest": stable_digest(result),
                    "correction_reason": explanation,
                    "compilation_transaction": str(transaction_path) if transaction_path.is_file() else None,
                    "compilation_transaction_digest": (
                        stable_digest(read_json(transaction_path)) if transaction_path.is_file() else None
                    ),
                    "provider_usage": str(provider_usage_path) if provider_usage_path.is_file() else None,
                    "provider_usage_digest": (
                        stable_digest(read_json(provider_usage_path)) if provider_usage_path.is_file() else None
                    ),
                    "usage_before_retry": copy.deepcopy(manifest["usage"]),
                    "created_at": utc_now(),
                },
            )
            manifest = self._update_manifest(
                manifest,
                state="paused_interrupted",
                blocker={
                    "code": "failed_stage_retry_authorized",
                    "message": "a corrected external condition has one audited retry from the existing checkpoint",
                    "stage": str(manifest["current_stage"]),
                },
                allowed_next_actions=["resume", "cancel"],
            )
            self._emit(
                job_id,
                "failed_stage_retry_authorized",
                stage=manifest["current_stage"],
                artifact_refs=[str(receipt_path)],
            )
        return self.inspect(job_id)

    def recompile_after_config(self, job_id: str, *, reason: str) -> dict[str, Any]:
        """Archive one Map-invalid compilation and reopen the same attempt at compile."""
        explanation = str(reason or "").strip()
        if not explanation:
            raise ValueError("configuration recompile requires a non-empty correction reason")
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            leaf = self._current_leaf_stage_result(manifest)
            eligibility = self._configuration_recompile_eligibility(
                manifest,
                current_leaf_stage_result=leaf,
            )
            if eligibility["available"] is not True:
                raise JobStoreError(str(eligibility["message"]))
            attempt_id = str(manifest["current_attempt_id"])
            attempt_dir = self.store.attempt_dir(job_id, attempt_id)
            compilation_dir = attempt_dir / "compilation"
            transaction = read_json(compilation_dir / "compilation_transaction.json")
            provider = self._validated_reusable_provider_checkpoint(
                compilation_dir,
                job_id=job_id,
                attempt_id=attempt_id,
            )
            receipts_dir = self.store.job_dir(job_id) / "receipts"
            sequence = len(list(receipts_dir.glob("configuration_recompile_*.json"))) + 1
            archive = attempt_dir / f"compilation_superseded_{sequence:03d}"
            if archive.exists():
                raise JobStoreError(f"configuration recompile archive already exists: {archive.name}")
            blocker_result = dict(leaf["result"])
            receipt_path = receipts_dir / f"configuration_recompile_{sequence:03d}.json"
            write_json(
                receipt_path,
                {
                    "schema_version": "harness_agent_configuration_recompile_receipt_v1",
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "blocker": {
                        "stage": blocker_result["stage"],
                        "failure_code": blocker_result["failure_code"],
                        "stage_result_digest": stable_digest(blocker_result),
                    },
                    "correction_reason": explanation,
                    "old_compile_config": eligibility["old_compile_config"],
                    "old_compile_config_digest": eligibility["old_compile_config_digest"],
                    "new_compile_config": eligibility["new_compile_config"],
                    "new_compile_config_digest": eligibility["new_compile_config_digest"],
                    "old_transaction_id": transaction["transaction_id"],
                    "old_transaction_digest": stable_digest(transaction),
                    "provider_checkpoint": provider,
                    "usage_preserved": copy.deepcopy(manifest["usage"]),
                    "archive": str(archive),
                    "created_at": utc_now(),
                },
            )
            compilation_dir.replace(archive)
            compilation_dir.mkdir()
            shutil.copy2(archive / "asset_provider_batch.json", compilation_dir / "asset_provider_batch.json")
            source_receipts = archive / "provider_receipts"
            if source_receipts.is_dir():
                shutil.copytree(source_receipts, compilation_dir / "provider_receipts")
            attempt = self.store.load_attempt(job_id, attempt_id)
            attempt.update(
                {
                    "status": "generated",
                    "compilation_id": None,
                    "execution_fingerprint": None,
                    "smoke_gate": None,
                    "updated_at": utc_now(),
                }
            )
            self.store.write_attempt(attempt)
            for key in [key for key in self._compilations if key[0] == job_id and key[1] == attempt_id]:
                self._compilations.pop(key, None)
            manifest = self._update_manifest(
                manifest,
                state="paused_interrupted",
                current_stage="compile",
                active_compilation_id=None,
                blocker={
                    "code": "configuration_recompile_authorized",
                    "message": "compile-affecting Map configuration changed; resume the audited replacement transaction",
                    "stage": "compile",
                },
                allowed_next_actions=["resume", "cancel"],
            )
            self._emit(
                job_id,
                "configuration_recompile_authorized",
                stage="compile",
                attempt_id=attempt_id,
                artifact_refs=[str(receipt_path), str(archive)],
            )
        return self.inspect(job_id)

    def retry_review_after_contract_fix(self, job_id: str, *, reason: str) -> dict[str, Any]:
        """Authorize one new Reviewer turn after a schema contract correction."""
        explanation = str(reason or "").strip()
        if not explanation:
            raise ValueError("Reviewer contract retry requires a non-empty correction reason")
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            leaf = self._current_leaf_stage_result(manifest)
            eligibility = self._reviewer_contract_retry_eligibility(
                manifest,
                current_leaf_stage_result=leaf,
            )
            if eligibility["available"] is not True:
                raise JobStoreError(str(eligibility["message"]))
            attempt_id = str(manifest["current_attempt_id"])
            amendments_dir = self.store.job_dir(job_id) / "amendments"
            sequence = len(list(amendments_dir.glob("reviewer_contract_retry_*.json"))) + 1
            receipt_path = amendments_dir / f"reviewer_contract_retry_{sequence:03d}.json"
            budget_before = copy.deepcopy(manifest["budget"])
            budget_after = dict(budget_before)
            budget_after["max_reviewer_technical_retries"] += 1
            if int(manifest["usage"]["total_retries"]) >= budget_after["max_total_retries"]:
                budget_after["max_total_retries"] += 1
            budget_after = normalized_budget(budget_after)
            write_json(
                receipt_path,
                {
                    "schema_version": "harness_agent_reviewer_contract_retry_v2",
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "failure_code": "reviewer_output_schema_invalid",
                    "failure_result_digest": eligibility["failure_result_digest"],
                    "bundle_digest": eligibility["bundle_digest"],
                    "prior_invocation_count": eligibility["prior_invocation_count"],
                    "old_input_digest": eligibility["old_input_digest"],
                    "new_input_digest": eligibility["new_input_digest"],
                    "reviewer_technical_retries_before": budget_before["max_reviewer_technical_retries"],
                    "reviewer_technical_retries_after": budget_after["max_reviewer_technical_retries"],
                    "total_retries_before": budget_before["max_total_retries"],
                    "total_retries_after": budget_after["max_total_retries"],
                    "usage_preserved": copy.deepcopy(manifest["usage"]),
                    "correction_reason": explanation,
                    "created_at": utc_now(),
                },
            )
            manifest = self._update_manifest(
                manifest,
                budget=budget_after,
                state="awaiting_semantic_review",
                blocker=None,
                allowed_next_actions=["run_semantic_review", "cancel"],
            )
            self._emit(
                job_id,
                "reviewer_contract_retry_authorized",
                stage="semantic_review",
                attempt_id=attempt_id,
                artifact_refs=[str(receipt_path)],
            )
        return self.inspect(job_id)

    def rebuild_evidence_after_provenance(self, job_id: str, *, reason: str) -> dict[str, Any]:
        """Rebuild an evidence-deficient uncertain review without rerunning the Candidate."""
        explanation = str(reason or "").strip()
        if not explanation:
            raise ValueError("evidence provenance rebuild requires a non-empty correction reason")
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            if (
                manifest["state"] != "needs_user_decision"
                or manifest["current_stage"] != "semantic_review"
                or (manifest.get("blocker") or {}).get("code") != "semantic_review_uncertain"
            ):
                raise JobStoreError("evidence provenance rebuild requires an uncertain Semantic Review")
            attempt_id = str(manifest["current_attempt_id"])
            attempt_dir = self.store.attempt_dir(job_id, attempt_id)
            bundle = self._validated_current_bundle_from_files(
                manifest,
                expected_manifest_digest=None,
                require_result_provenance=False,
            )
            review = SemanticReview.from_dict(
                read_json(attempt_dir / "semantic_review.json"),
                expected_requirement_ids={
                    str(row["id"])
                    for row in read_json(attempt_dir / "evidence_bundle" / "evidence_summary.json")["semantic_requirements"]
                },
                evidence_artifact_ids={str(row["artifact_id"]) for row in bundle["artifacts"]},
                evidence_manifest=bundle,
            ).to_dict()
            if (
                review["job_id"] != job_id
                or review["attempt_id"] != attempt_id
                or review["evidence_bundle_digest"] != stable_digest(bundle)
                or review["overall_status"] != "uncertain"
                or review["repair_layer"] != "evidence"
            ):
                raise JobStoreError("Semantic Review does not identify an evidence-layer uncertainty")
            receipts_dir = self.store.job_dir(job_id) / "receipts"
            sequence = len(list(receipts_dir.glob("evidence_provenance_rebuild_*.json"))) + 1
            bundle_archive = attempt_dir / f"evidence_bundle_superseded_{sequence:03d}"
            review_archive = attempt_dir / f"semantic_review_superseded_{sequence:03d}"
            if bundle_archive.exists() or review_archive.exists():
                raise JobStoreError("evidence provenance rebuild archive already exists")
            receipt_path = receipts_dir / f"evidence_provenance_rebuild_{sequence:03d}.json"
            write_json(
                receipt_path,
                {
                    "schema_version": "harness_agent_evidence_provenance_rebuild_v1",
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "correction_reason": explanation,
                    "old_bundle_digest": stable_digest(bundle),
                    "old_review_digest": stable_digest(review),
                    "usage_preserved": copy.deepcopy(manifest["usage"]),
                    "bundle_archive": str(bundle_archive),
                    "review_archive": str(review_archive),
                    "created_at": utc_now(),
                },
            )
            (attempt_dir / "evidence_bundle").replace(bundle_archive)
            review_archive.mkdir()
            for path in [
                attempt_dir / "semantic_review.json",
                attempt_dir / "stage_results" / "semantic_review.json",
                attempt_dir / "stage_results" / "evidence_bundle.json",
                *sorted(attempt_dir.glob("reviewer_reservation_*.json")),
                *sorted(attempt_dir.glob("reviewer_invocation_*.json")),
                *sorted((self.store.job_dir(job_id) / "amendments").glob("reviewer_contract_retry_*.json")),
            ]:
                if path.is_file():
                    path.replace(review_archive / path.name)
            attempt = self.store.load_attempt(job_id, attempt_id)
            attempt.update({"status": "quality_gate_passed", "updated_at": utc_now()})
            self.store.write_attempt(attempt)
            self._update_manifest(
                manifest,
                state="paused_interrupted",
                current_stage="evidence_bundle",
                blocker={
                    "code": "evidence_provenance_rebuild_authorized",
                    "message": "result provenance was added; rebuild the Evidence Bundle from the existing Candidate",
                    "stage": "evidence_bundle",
                },
                allowed_next_actions=["resume", "cancel"],
            )
            self._emit(
                job_id,
                "evidence_provenance_rebuild_authorized",
                stage="evidence_bundle",
                attempt_id=attempt_id,
                artifact_refs=[str(receipt_path), str(bundle_archive), str(review_archive)],
            )
        return self.advance_until_blocked(job_id)

    def recover_unlaunched_review(self, job_id: str, *, reason: str) -> dict[str, Any]:
        """Reopen a Reviewer setup failure that occurred before any model work."""
        explanation = str(reason or "").strip()
        if not explanation:
            raise ValueError("unlaunched Reviewer recovery requires a non-empty correction reason")
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            if (
                manifest["state"] != "failed"
                or manifest["current_stage"] != "semantic_review"
                or (manifest.get("blocker") or {}).get("code") != "reviewer_app_server_failure"
            ):
                raise JobStoreError("unlaunched Reviewer recovery requires an app-server setup failure")
            attempt_id = str(manifest["current_attempt_id"])
            attempt_dir = self.store.attempt_dir(job_id, attempt_id)
            reservation_paths = sorted(attempt_dir.glob("reviewer_reservation_*.json"))
            receipt_paths = sorted(attempt_dir.glob("reviewer_invocation_*.json"))
            if len(reservation_paths) != 1 or len(receipt_paths) != 1:
                raise JobStoreError("unlaunched Reviewer recovery requires one current setup attempt")
            reservation = ReviewerInvocationReservation.from_dict(read_json(reservation_paths[0])).to_dict()
            receipt = ReviewerInvocationReceipt.from_dict(read_json(receipt_paths[0])).to_dict()
            if (
                reservation["job_id"] != job_id
                or reservation["attempt_id"] != attempt_id
                or reservation["usage_counted"] is not False
                or reservation["outcome"] != "technical_failed"
                or reservation["error_code"] != "reviewer_app_server_failure"
                or receipt["job_id"] != job_id
                or receipt["attempt_id"] != attempt_id
                or receipt["status"] != "failed"
                or receipt["error_code"] != "reviewer_app_server_failure"
                or receipt["thread_id"] is not None
                or receipt["turn_id"] is not None
                or receipt["output_digest"] is not None
            ):
                raise JobStoreError("Reviewer receipt does not prove a zero-invocation setup failure")
            bundle = self._validated_current_bundle(manifest)
            sequence = len(list(attempt_dir.glob("reviewer_setup_superseded_*"))) + 1
            archive = attempt_dir / f"reviewer_setup_superseded_{sequence:03d}"
            if archive.exists():
                raise JobStoreError("Reviewer setup archive already exists")
            receipts_dir = self.store.job_dir(job_id) / "receipts"
            recovery_path = receipts_dir / f"unlaunched_reviewer_recovery_{sequence:03d}.json"
            write_json(
                recovery_path,
                {
                    "schema_version": "harness_agent_unlaunched_reviewer_recovery_v1",
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "failure_code": "reviewer_app_server_failure",
                    "bundle_digest": stable_digest(bundle),
                    "reservation_digest": stable_digest(reservation),
                    "receipt_digest": stable_digest(receipt),
                    "usage_preserved": copy.deepcopy(manifest["usage"]),
                    "correction_reason": explanation,
                    "archive": str(archive),
                    "created_at": utc_now(),
                },
            )
            archive.mkdir()
            for path in [
                *reservation_paths,
                *receipt_paths,
                attempt_dir / "stage_results" / "semantic_review.json",
            ]:
                if path.is_file():
                    path.replace(archive / path.name)
            attempt = self.store.load_attempt(job_id, attempt_id)
            attempt.update({"status": "awaiting_semantic_review", "updated_at": utc_now()})
            self.store.write_attempt(attempt)
            self._update_manifest(
                manifest,
                state="awaiting_semantic_review",
                blocker=None,
                allowed_next_actions=["run_semantic_review", "cancel"],
            )
            self._emit(
                job_id,
                "unlaunched_reviewer_recovered",
                stage="semantic_review",
                attempt_id=attempt_id,
                artifact_refs=[str(recovery_path), str(archive)],
            )
        return self.inspect(job_id)

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
            request = read_json(root / "user_request.json")
            try:
                context = self._load_frozen_native_context(manifest, request=request, create_identity_if_missing=True)
                value = validate_native_generation_submission(submission, context=context)
            except NativeGenerationValidationError as exc:
                return self._record_native_generation_rejection(
                    manifest,
                    failure_code=exc.code,
                    message=str(exc),
                    context_path=context_path,
                )
            try:
                case_spec = self._native_case_spec(request, value["case_spec"])
            except (KeyError, TypeError, ValueError) as exc:
                return self._record_native_generation_rejection(
                    manifest,
                    failure_code="native_generation_case_spec_invalid",
                    message=str(exc) or type(exc).__name__,
                    context_path=context_path,
                )
            try:
                self._project_native_intent_contract(manifest, request, value["intent_draft"], case_spec.data)
            except (KeyError, TypeError, ValueError) as exc:
                return self._record_native_generation_rejection(
                    manifest,
                    failure_code="native_generation_parameter_constraint_invalid",
                    message=str(exc) or type(exc).__name__,
                    context_path=context_path,
                )
            submission_path = root / "native_generation_submission.json"
            ack_path = root / "native_generation_ack.json"
            if submission_path.is_file():
                existing = read_json(submission_path)
                if existing != value:
                    result = build_stage_result(
                        stage="generation",
                        status="failed",
                        failure_class="agent_submission_invalid",
                        failure_code="native_generation_submission_immutable_conflict",
                        failure_codes=["native_generation_submission_immutable_conflict"],
                        message="an immutable accepted native generation submission already differs",
                        retryable=False,
                        job_id=job_id,
                        invocation_count=0,
                        artifact_refs=[
                            artifact_ref("native_generation_context", str(context_path), str(context["schema_version"])),
                            artifact_ref(
                                "native_generation_submission",
                                str(submission_path),
                                str(existing.get("schema_version") or ""),
                            ),
                        ],
                        allowed_next_actions=["continue", "cancel"],
                    )
                    self._write_controller_stage_result(manifest, result)
                    return self._inspection_with_submission_result(job_id, result)
            else:
                write_json(submission_path, value)
            if ack_path.is_file():
                try:
                    validate_native_generation_ack(read_json(ack_path), context=context, submission=value)
                except NativeGenerationValidationError as exc:
                    return self._record_native_generation_rejection(
                        manifest,
                        failure_code=exc.code,
                        message=str(exc),
                        context_path=context_path,
                    )
            else:
                write_json(ack_path, build_native_generation_ack(context=context, submission=value))
            self._update_manifest(
                manifest,
                state="running",
                blocker=None,
                allowed_next_actions=["advance", "cancel"],
            )
        return self.inspect(job_id)

    def reject_native_generation_submission_input(self, job_id: str, message: str) -> dict[str, Any]:
        """Record an unreadable CLI submission without writing it as an immutable submission."""
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            if manifest["current_stage"] != "generation":
                raise JobStoreError("native generation submission requires the generation stage")
            return self._record_native_generation_rejection(
                manifest,
                failure_code="native_generation_submission_schema_invalid",
                message=message,
                context_path=self.store.job_dir(job_id) / "request" / "native_generation_context.json",
            )

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
                    self._write_controller_stage_result(manifest, budget_result)
                    manifest = self._apply_stage_result(manifest, budget_result)
                    self._stage_event(manifest, "stage_blocked", "budget", result=budget_result)
                    break
                stage = str(manifest["current_stage"])
                started = self.hooks.monotonic()
                stage_result_snapshot = self._stage_result_snapshot(manifest)
                self._stage_event(manifest, "stage_started", stage)
                self._start_in_flight(manifest, stage, started_monotonic=started)
                in_flight_outcome = "returned"
                try:
                    with self._effective_environment(manifest):
                        manifest = self._advance_one(manifest)
                except (KeyboardInterrupt, SystemExit) as exc:
                    in_flight_outcome = "interrupted"
                    if stage == "compile":
                        manifest = self._reconcile_provider_usage(manifest)
                        manifest = self._reconcile_ue_launch_usage(manifest)
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
                    self._stage_event(manifest, "stage_blocked", str(result["stage"]), result=result)
                    break
                except BaseException as exc:
                    in_flight_outcome = "failed"
                    if stage == "compile":
                        manifest = self._reconcile_provider_usage(manifest)
                        manifest = self._reconcile_ue_launch_usage(manifest)
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
                    self._stage_event(manifest, "stage_blocked", str(result["stage"]), result=result)
                    if manifest["state"] == "running":
                        continue
                    break
                finally:
                    self._finish_in_flight(job_id, stage, in_flight_outcome)
                elapsed = max(0.0, self.hooks.monotonic() - started)
                manifest = self._add_active_elapsed(manifest, elapsed)
                structured_failure = self._changed_noncompleted_stage_result(
                    manifest,
                    stage_result_snapshot=stage_result_snapshot,
                )
                if structured_failure is not None:
                    self._stage_event(
                        manifest,
                        "stage_blocked",
                        str(structured_failure["stage"]),
                        result=structured_failure,
                    )
                elif manifest["state"] in {"running", "awaiting_semantic_review"}:
                    self._stage_event(manifest, "stage_completed", stage)
                else:
                    leaf = self._current_leaf_stage_result(manifest)
                    result = leaf["result"] if leaf is not None else None
                    blocked_stage = str(result["stage"]) if result is not None else stage
                    self._stage_event(manifest, "stage_blocked", blocked_stage, result=result)
            self._emit_job_terminal_if_terminal(manifest)
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
            leaf = self._current_leaf_stage_result(manifest)
            contract_repair = self._case_spec_contract_repair_eligibility(
                manifest,
                current_leaf_stage_result=leaf,
            )
            if manifest["state"] not in {"blocked", "needs_user_decision", "paused_interrupted", "failed"}:
                raise JobStoreError(f"job cannot be resumed from state {manifest['state']}")
            if (
                revised_case_spec is not None
                and manifest["usage"]["case_spec_revisions"] >= manifest["budget"]["max_case_spec_revisions"]
            ):
                manifest, budget_result = self._block_case_spec_revision_budget(
                    manifest,
                    trigger_result=(leaf or {}).get("result"),
                )
                self._stage_event(manifest, "stage_blocked", "budget", result=budget_result)
                return self.inspect(job_id)
            contract_revision = revised_case_spec is not None and contract_repair["available"] is True
            if (
                manifest["state"] == "failed"
                and (manifest.get("blocker") or {}).get("code") != "budget_exhausted"
                and not contract_revision
            ):
                raise JobStoreError("only budget-exhausted failed jobs may be resumed")
            requested_action = "resume_with_revision" if revised_case_spec is not None else "resume"
            if requested_action not in manifest["allowed_next_actions"] and not contract_revision:
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
                    additional_allowed_adjustments=(
                        contract_repair["allowed_adjustments"] if contract_revision else None
                    ),
                    evidence_refs=(
                        [str(contract_repair["provider_batch"])] if contract_revision else None
                    ),
                )
            if manifest["current_stage"] == "semantic_review" and revised_case_spec is None:
                attempt_dir = self.store.attempt_dir(job_id, manifest["current_attempt_id"])
                review_path = attempt_dir / "semantic_review.json"
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
                self._stage_event(manifest, "stage_blocked", "budget", result=budget_result)
                return self.inspect(job_id)
            self._stage_event(manifest, "stage_started", "semantic_review")
            attempt_id = str(manifest["current_attempt_id"])
            attempt_dir = self.store.attempt_dir(job_id, attempt_id)
            attempt = self.store.load_attempt(job_id, attempt_id)
            bundle_path = attempt_dir / "evidence_bundle" / "manifest.json"
            if not bundle_path.is_file():
                raise JobStoreError("formal semantic review requires a completed Evidence Bundle")
            review_path = attempt_dir / "semantic_review.json"
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
                self._emit_job_terminal_if_terminal(manifest)
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
                self._emit_job_terminal_if_terminal(manifest)
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
                self._emit_job_terminal_if_terminal(manifest)
                return self.inspect(job_id)

            if review_path.is_file():
                intent = IntentContract.from_dict(
                    read_json(self.store.job_dir(job_id) / "request" / "intent_contract.json")
                ).to_dict()
                review = self._load_validated_semantic_review(manifest)
                manifest = self._apply_semantic_outcome(manifest, attempt, intent, review)
                self._emit_semantic_outcome_event(manifest)
                self._emit_job_terminal_if_terminal(manifest)
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
                raw_review: dict[str, Any] | None = None
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
                reviewer_finished = reviewer_started
                reviewer_elapsed_recorded = False
                post_review_hard_gate: dict[str, Any] | None = None
                close_in_flight = True
                in_flight_outcome = "returned"
                self._start_in_flight(manifest, "semantic_review", started_monotonic=reviewer_started)
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
                    reviewer_finished = self.hooks.monotonic()
                    elapsed = max(0.0, reviewer_finished - reviewer_started)
                    manifest = self._add_active_elapsed(manifest, elapsed)
                    reviewer_elapsed_recorded = True
                    post_review_hard_gate = self._hard_deadline_gate(manifest)
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
                    if post_review_hard_gate is not None:
                        self._write_controller_stage_result(manifest, post_review_hard_gate)
                        manifest = self._apply_stage_result(manifest, post_review_hard_gate)
                        self._stage_event(manifest, "stage_completed", "semantic_review")
                        self._stage_event(manifest, "stage_blocked", "budget", result=post_review_hard_gate)
                        self._emit_job_terminal_if_terminal(manifest)
                        return self.inspect(job_id)
                    manifest = self._apply_semantic_outcome(manifest, attempt, intent, review)
                    self._emit_semantic_outcome_event(manifest)
                    self._emit_job_terminal_if_terminal(manifest)
                    return self.inspect(job_id)
                except SemanticReviewerError as exc:
                    in_flight_outcome = "interrupted" if exc.code == "reviewer_interrupted" else "failed"
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
                    in_flight_outcome = "failed"
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
                    in_flight_outcome = "failed"
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
                    in_flight_outcome = "failed"
                    rejected_refs: list[dict[str, Any]] = []
                    if raw_review is not None:
                        rejected_path = attempt_dir / f"reviewer_output_rejected_{invocation_count:03d}.json"
                        write_json(
                            rejected_path,
                            {
                                "schema_version": "harness_rejected_reviewer_output_v1",
                                "job_id": job_id,
                                "attempt_id": attempt_id,
                                "invocation_count": invocation_count,
                                "input_digest": expected_input_digest,
                                "output_digest": stable_digest(raw_review),
                                "failure_code": "reviewer_output_schema_invalid",
                                "message": str(exc),
                                "review": raw_review,
                                "created_at": utc_now(),
                            },
                        )
                        rejected_refs.append(
                            {
                                "name": "rejected_reviewer_output",
                                "path": str(rejected_path),
                                "schema_version": "harness_rejected_reviewer_output_v1",
                            }
                        )
                    result = failure_stage_result(
                        stage="semantic_review",
                        failure_code="reviewer_output_schema_invalid",
                        message=str(exc),
                        retryable=True,
                        job_id=job_id,
                        attempt_id=attempt_id,
                        invocation_count=invocation_count,
                        artifact_refs=rejected_refs,
                    )
                except BaseException:
                    close_in_flight = False
                    raise
                finally:
                    if not reviewer_elapsed_recorded:
                        reviewer_finished = self.hooks.monotonic()
                        elapsed = max(0.0, reviewer_finished - reviewer_started)
                        manifest = self._add_active_elapsed(manifest, elapsed)
                    if close_in_flight:
                        self._finish_in_flight(
                            job_id,
                            "semantic_review",
                            in_flight_outcome,
                            finished_monotonic=reviewer_finished,
                        )
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
                post_review_hard_gate = post_review_hard_gate or self._hard_deadline_gate(manifest)
                if post_review_hard_gate is not None:
                    self._write_controller_stage_result(manifest, post_review_hard_gate)
                    manifest = self._apply_stage_result(manifest, post_review_hard_gate)
                    self._stage_event(manifest, "stage_blocked", "budget", result=post_review_hard_gate)
                    self._emit_job_terminal_if_terminal(manifest)
                    return self.inspect(job_id)
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
                    self._stage_event(manifest, "stage_blocked", "budget", result=retry_budget_result)
                    return self.inspect(job_id)
                self._checkpoint(manifest, "semantic_review", "failed", stable_digest(bundle), [str(bundle_path)])
                exhausted = dict(result)
                exhausted["retryable"] = False
                exhausted["allowed_next_actions"] = ["inspect_artifacts"]
                exhausted = StageResult.from_dict(exhausted).to_dict()
                write_stage_result(attempt_dir, exhausted)
                manifest = self._apply_stage_result(manifest, exhausted)
                self._stage_event(manifest, "stage_blocked", "semantic_review", result=exhausted)
                self._emit_job_terminal_if_terminal(manifest)
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
            self._emit_job_terminal_if_terminal(manifest)
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
                self._write_controller_stage_result(manifest, result)
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
            return self._advance_run(
                manifest,
                profile_name=str(manifest["target"]["execution_profile"]),
                controller_stage="candidate",
            )
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
            try:
                context = self._load_frozen_native_context(manifest, request=request, create_identity_if_missing=True)
            except NativeGenerationValidationError as exc:
                result = failure_stage_result(
                    stage="generation",
                    failure_code=exc.code,
                    message=str(exc),
                    source_status="blocked",
                    job_id=job_id,
                    invocation_count=0,
                    artifact_refs=[artifact_ref("native_generation_context", str(context_path))],
                )
                self._write_controller_stage_result(manifest, result)
                return self._apply_stage_result(manifest, result)
        else:
            context = build_native_generation_context(
                job_id=job_id,
                request_digest=manifest["request_digest"],
                request=request,
                target=manifest["target"],
                authorizations=manifest["authorizations"],
            )
            write_json(context_path, context)
            write_json(
                request_root / "native_generation_context_identity.json",
                build_native_generation_context_identity(context),
            )
        submission_path = request_root / "native_generation_submission.json"
        ack_path = request_root / "native_generation_ack.json"
        context_identity_path = request_root / "native_generation_context_identity.json"
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
                    ),
                    artifact_ref(
                        "native_generation_context_identity",
                        str(context_identity_path),
                        NATIVE_GENERATION_CONTEXT_IDENTITY_SCHEMA_VERSION,
                    ),
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
                artifact_ref(
                    "native_generation_context_identity",
                    str(context_identity_path),
                    NATIVE_GENERATION_CONTEXT_IDENTITY_SCHEMA_VERSION,
                ),
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
        validate_agent_case_spec_contract(initial.data)
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
        for index, row in enumerate(draft["parameter_analysis"]):
            path = str(row["path"])
            try:
                self._case_spec_path_value(case_spec, path)
            except KeyError as exc:
                raise ValueError(
                    f"intent_draft.parameter_analysis[{index}].path={path!r} does not exist "
                    "in the submitted CaseSpec"
                ) from exc
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
        missing_adjustments = sorted(
            set(expected_adjustable) - set(contract["allowed_adjustments"]["paths"])
        )
        if missing_adjustments:
            raise ValueError(
                "soft/inferred parameter_analysis paths must name bounded adjustable CaseSpec leaves; "
                f"invalid paths: {missing_adjustments}"
            )
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

    def _native_hard_parameter_paths(self, job_id: str) -> set[str]:
        path = self.store.job_dir(job_id) / "request" / "native_generation_submission.json"
        if not path.is_file():
            return set()
        try:
            submission = read_json(path)
        except (OSError, TypeError, ValueError):
            return set()
        draft = submission.get("intent_draft") if isinstance(submission, Mapping) else None
        rows = draft.get("parameter_analysis") if isinstance(draft, Mapping) else None
        return {
            str(row.get("path") or "")
            for row in rows or []
            if isinstance(row, Mapping) and row.get("requirement_level") == "hard"
        }

    def _load_frozen_native_context(
        self,
        manifest: Mapping[str, Any],
        *,
        request: Mapping[str, Any],
        create_identity_if_missing: bool,
    ) -> dict[str, Any]:
        request_root = self.store.job_dir(manifest["job_id"]) / "request"
        context_path = request_root / "native_generation_context.json"
        try:
            context = validate_native_generation_context(read_json(context_path))
        except NativeGenerationValidationError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise NativeGenerationValidationError(
                "native_generation_context_invalid",
                str(exc) or type(exc).__name__,
            ) from exc
        self._validate_native_context_binding(manifest, context, request)
        identity_path = request_root / "native_generation_context_identity.json"
        if identity_path.is_file():
            try:
                validate_native_generation_context_identity(read_json(identity_path), context=context)
            except NativeGenerationValidationError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise NativeGenerationValidationError(
                    "native_generation_context_identity_invalid",
                    str(exc) or type(exc).__name__,
                ) from exc
        elif create_identity_if_missing:
            # M5 contexts predate the independent identity sidecar. Freeze the
            # already stored, binding-valid context in place; never regenerate it.
            write_json(identity_path, build_native_generation_context_identity(context))
        else:
            raise NativeGenerationValidationError(
                "native_generation_context_identity_invalid",
                "native generation context identity is missing",
            )
        return context

    def _record_native_generation_rejection(
        self,
        manifest: Mapping[str, Any],
        *,
        failure_code: str,
        message: str,
        context_path: Path,
    ) -> dict[str, Any]:
        refs = [artifact_ref("native_generation_context", str(context_path))] if context_path.is_file() else []
        identity_path = context_path.with_name("native_generation_context_identity.json")
        if identity_path.is_file():
            refs.append(
                artifact_ref(
                    "native_generation_context_identity",
                    str(identity_path),
                    NATIVE_GENERATION_CONTEXT_IDENTITY_SCHEMA_VERSION,
                )
            )
        result = failure_stage_result(
            stage="generation",
            failure_code=failure_code,
            message=message,
            source_status="blocked",
            job_id=manifest["job_id"],
            invocation_count=0,
            artifact_refs=refs,
        )
        self._write_controller_stage_result(manifest, result)
        updated = self._apply_stage_result(dict(manifest), result)
        self._stage_event(updated, "stage_blocked", "generation", result=result)
        return self._inspection_with_submission_result(str(manifest["job_id"]), result)

    def _inspection_with_submission_result(
        self,
        job_id: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        inspection = self.inspect(job_id)
        inspection["submission_stage_result"] = StageResult.from_dict(result).to_dict()
        return inspection

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
            raise NativeGenerationValidationError(
                "native_generation_context_identity_mismatch",
                "native generation context identity mismatch",
            )

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
        try:
            validate_agent_case_spec_contract(case_spec.data)
        except CaseSpecV2ValidationError as exc:
            result = failure_stage_result(
                stage="compile",
                failure_code="invalid_generation_spec",
                message=str(exc) or type(exc).__name__,
                job_id=manifest["job_id"],
                attempt_id=manifest["current_attempt_id"],
            )
            self._write_controller_stage_result(manifest, result)
            return self._apply_stage_result(manifest, result)
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
            compile_config=self.config.ue_compile_identity(case_spec.data),
        )
        compilation.write(attempt_dir / "compilation")
        manifest = self._reconcile_provider_usage(manifest)
        manifest = self._reconcile_ue_launch_usage(manifest)
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

    def _advance_run(
        self,
        manifest: dict[str, Any],
        *,
        profile_name: str,
        controller_stage: str | None = None,
    ) -> dict[str, Any]:
        stage_name = controller_stage or profile_name
        attempt_id = manifest["current_attempt_id"]
        attempt_dir = self.store.attempt_dir(manifest["job_id"], attempt_id)
        case_spec = self._load_current_case_spec(manifest)
        compilation = self._compilation_for_attempt(manifest, case_spec, profile_name=profile_name)
        profile = execution_profile(profile_name)
        profile_fingerprint = stable_digest(
            {"execution": self._execution_fingerprint(case_spec.data, compilation), "profile": profile.__dict__}
        )
        run_slot = attempt_dir / "runs" / stage_name
        gate_path = attempt_dir / "smoke_gate.json"
        if stage_name == "smoke" and gate_path.is_file():
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
            manifest = self._reconcile_ue_launch_usage(manifest)
            usage = copy.deepcopy(manifest["usage"])
            if usage["ue_launches"] >= manifest["budget"]["max_ue_launches"]:
                result = failure_stage_result(
                    stage=stage_name,
                    failure_code="ue_launch_budget_exhausted",
                    message="UE launch budget is exhausted",
                    job_id=manifest["job_id"],
                    attempt_id=attempt_id,
                )
                self._write_controller_stage_result(manifest, result)
                return self._apply_stage_result(manifest, result)
            self._record_controller_ue_launch(manifest, kind="runtime", stage=stage_name)
            manifest = self._reconcile_ue_launch_usage(manifest)
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
        if stage_name == "smoke":
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
        checkpoint_stage = stage_name
        self._checkpoint(manifest, checkpoint_stage, "completed", profile_fingerprint, [str(run_dir)])
        if stage_name == "smoke":
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
        result_provenance = self._evidence_result_provenance(
            manifest,
            attempt_dir=attempt_dir,
            run_dir=Path(str(candidate["run_dir"])),
        )
        result = dict(
            self.hooks.evidence(
                job_id=job_id,
                attempt=attempt,
                attempt_dir=attempt_dir,
                candidate_run_dir=Path(str(candidate["run_dir"])),
                request=request,
                intent_contract=intent,
                result_provenance=result_provenance,
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
            expected_result_provenance=result_provenance,
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

    def _evidence_result_provenance(
        self,
        manifest: Mapping[str, Any],
        *,
        attempt_dir: Path,
        run_dir: Path,
    ) -> dict[str, Any]:
        asset_resolution_path = attempt_dir / "compilation" / "asset_resolution.json"
        provider_batch_path = attempt_dir / "compilation" / "asset_provider_batch.json"
        quality_path = run_dir / "quality_report.json"
        asset_resolution = read_json(asset_resolution_path)
        provider_batch = read_json(provider_batch_path)
        quality = read_json(quality_path)
        readiness = ((quality.get("source_reports") or {}).get("run_readiness") or {})
        achieved_tier = readiness.get("publication_tier")
        if achieved_tier not in {"diagnostic_only", "local_preview", "reference"}:
            raise JobStoreError("Quality Report lacks a valid achieved publication tier")

        assets = []
        for resolution in asset_resolution.get("assets") or []:
            selected = resolution.get("selected_asset") if isinstance(resolution, Mapping) else None
            intent = resolution.get("intent") if isinstance(resolution, Mapping) else None
            provenance = selected.get("provenance") if isinstance(selected, Mapping) else None
            if not isinstance(selected, Mapping) or not isinstance(intent, Mapping) or not isinstance(provenance, Mapping):
                raise JobStoreError("Asset Resolve lacks selected-asset provenance")
            assets.append(
                {
                    "object_id": intent.get("object_id"),
                    "slot": intent.get("slot"),
                    "asset_id": selected.get("asset_id"),
                    "source_kind": selected.get("source_kind"),
                    "license_tier": selected.get("license_tier"),
                    "provider_id": provenance.get("provider_id"),
                    "recipe_id": provenance.get("recipe_id"),
                    "receipt_id": provenance.get("receipt_id"),
                }
            )
        routes = sorted(
            {
                str(row.get("route"))
                for row in provider_batch.get("requests") or []
                if isinstance(row, Mapping) and row.get("route")
            }
        )
        receipt_ids = sorted(str(value) for value in provider_batch.get("receipt_ids") or [])
        intent = read_json(self.store.job_dir(str(manifest["job_id"])) / "request" / "intent_contract.json")
        requested_tier = (intent.get("execution") or {}).get("publication_tier")
        if requested_tier not in {"diagnostic_only", "local_preview", "reference"}:
            raise JobStoreError("Intent Contract lacks a valid requested publication tier")
        return {
            "schema_version": "harness_evidence_result_provenance_v1",
            "publication": {
                "requested_tier": requested_tier,
                "achieved_tier": achieved_tier,
                "quality_report": {
                    "path": quality_path.relative_to(attempt_dir).as_posix(),
                    "sha256": self._sha256_file(quality_path),
                },
            },
            "assets": {
                "items": assets,
                "asset_resolution": {
                    "path": asset_resolution_path.relative_to(attempt_dir).as_posix(),
                    "sha256": self._sha256_file(asset_resolution_path),
                },
            },
            "provider_usage": {
                "routes": routes,
                "external_provider_used": bool(set(routes).intersection({"external_site", "model_generation"})),
                "paid_submissions": int(manifest["usage"]["paid_submissions"]),
                "request_count": len(provider_batch.get("requests") or []),
                "receipt_count": len(receipt_ids),
                "provider_batch": {
                    "path": provider_batch_path.relative_to(attempt_dir).as_posix(),
                    "sha256": self._sha256_file(provider_batch_path),
                },
            },
        }

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
            compile_config=self.config.ue_compile_identity(case_spec.data),
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
        target_execution_profile = execution_profile(str(manifest["target"]["execution_profile"]))
        execution = {
            "backend_constraints": copy.deepcopy(case_spec.get("backend_constraints") or {}),
            "target_profile": manifest["target"]["execution_profile"],
            "publication_tier": manifest["target"]["publication_tier"],
            "duration_s": (case_spec.get("scene") or {}).get("duration_s"),
            "resolution": [target_execution_profile.width, target_execution_profile.height],
        }
        declared_adjustments = self._project_allowed_adjustments(expansion, case_spec)
        hard_parameter_paths = {
            str(row.get("path") or "")
            for row in expansion.get("parameter_analysis") or []
            if isinstance(row, Mapping) and row.get("requirement_level") == "hard"
        }
        allowed_adjustments = self._overlay_allowed_adjustments(
            self._case_spec_revision_policy(case_spec, excluded_paths=hard_parameter_paths),
            declared_adjustments,
        )
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
            elif kind == "numeric_vector":
                minimum, maximum = constraint.get("min"), constraint.get("max")
                if (
                    set(constraint) != {"kind", "min", "max"}
                    or not cls._finite_numeric_vector(current, length=len(minimum) if isinstance(minimum, list) else 0)
                    or not isinstance(minimum, list)
                    or not isinstance(maximum, list)
                    or not minimum
                    or len(current) != len(minimum)
                    or len(current) != len(maximum)
                    or any(
                        isinstance(bound, bool)
                        or not isinstance(bound, (int, float))
                        or not math.isfinite(float(bound))
                        for bound in minimum + maximum
                    )
                    or any(
                        low > value or value > high
                        for value, low, high in zip(current, minimum, maximum)
                    )
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
            raise ValueError("CaseSpec path is not an exact dot path")
        value: Any = case_spec
        for component in path[2:].split("."):
            if isinstance(value, Mapping) and component in value:
                value = value[component]
                continue
            if isinstance(value, list):
                matches = [
                    row
                    for row in value
                    if isinstance(row, Mapping) and str(row.get("id") or "") == component
                ]
                if len(matches) == 1:
                    value = matches[0]
                    continue
            raise KeyError(path)
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
        additional_allowed_adjustments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if manifest["usage"]["case_spec_revisions"] >= manifest["budget"]["max_case_spec_revisions"]:
            raise JobStoreError("CaseSpec revision budget is exhausted")
        request = read_json(self.store.job_dir(manifest["job_id"]) / "request" / "user_request.json")
        case_spec = case_spec_v2_from_dict(
            raw_case_spec,
            available_input_ids=[str(row.get("input_id")) for row in request.get("inputs") or []],
        )
        validate_agent_case_spec_contract(case_spec.data)
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
        intent_allowed = self._effective_allowed_adjustments(intent)
        if intent.get("schema_version") == INTENT_CONTRACT_SCHEMA_VERSION:
            intent_allowed = self._overlay_allowed_adjustments(
                self._case_spec_revision_policy(
                    parent_spec,
                    excluded_paths=self._native_hard_parameter_paths(manifest["job_id"]),
                ),
                intent_allowed,
            )
        allowed = self._merge_allowed_adjustments(
            intent_allowed,
            additional_allowed_adjustments,
        )
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
        hard_gate = self._hard_deadline_gate(manifest)
        if hard_gate is not None:
            return hard_gate
        elapsed = float(manifest["usage"]["active_elapsed_seconds"])
        soft = int(manifest["budget"]["soft_deadline_seconds"])
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
            hard = int(manifest["budget"]["hard_deadline_seconds"])
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

    @staticmethod
    def _hard_deadline_gate(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
        if float(manifest["usage"]["active_elapsed_seconds"]) < int(manifest["budget"]["hard_deadline_seconds"]):
            return None
        return failure_stage_result(
            stage="budget",
            failure_code="budget_exhausted",
            message="active runtime hard deadline is exhausted; approve an extension to continue",
            source_status="blocked",
            job_id=manifest["job_id"],
            attempt_id=manifest.get("current_attempt_id"),
        )

    def _case_spec_revision_budget_result(
        self,
        manifest: Mapping[str, Any],
        *,
        trigger_result: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        refs: list[dict[str, Any]] = []
        if isinstance(trigger_result, Mapping):
            if trigger_result.get("failure_code") == "case_spec_revision_budget_exhausted":
                refs = [
                    dict(ref)
                    for ref in trigger_result.get("artifact_refs") or []
                    if isinstance(ref, Mapping) and ref.get("role") == "trigger_stage_result"
                ]
            trigger_stage = str(trigger_result.get("stage") or manifest.get("current_stage") or "compile")
            if not refs:
                expected = StageResult.from_dict(trigger_result).to_dict()
                matching_paths: list[Path] = []
                for candidate in sorted(self._stage_artifact_root(manifest).rglob(f"stage_results/{trigger_stage}.json")):
                    try:
                        if StageResult.from_dict(read_json(candidate)).to_dict() == expected:
                            matching_paths.append(candidate)
                    except (OSError, TypeError, ValueError):
                        continue
                if matching_paths:
                    trigger_path = matching_paths[0]
                    refs.append(
                        artifact_ref(
                            "trigger_stage_result",
                            str(trigger_path),
                            str(trigger_result["schema_version"]) if trigger_result.get("schema_version") else None,
                        )
                    )
        return build_stage_result(
            stage="budget",
            status="blocked",
            failure_class="blocked_user_action",
            failure_code="case_spec_revision_budget_exhausted",
            failure_codes=["case_spec_revision_budget_exhausted"],
            message="CaseSpec revision budget is exhausted; the current Job cannot create another revision",
            retryable=False,
            job_id=str(manifest["job_id"]),
            attempt_id=manifest.get("current_attempt_id"),
            artifact_refs=refs,
            allowed_next_actions=["inspect_artifacts", "cancel"],
            required_user_action={
                "code": "case_spec_revision_budget_exhausted",
                "message": "create a new Job if another CaseSpec revision is required",
            },
        )

    def _block_case_spec_revision_budget(
        self,
        manifest: dict[str, Any],
        *,
        trigger_result: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        result = self._case_spec_revision_budget_result(manifest, trigger_result=trigger_result)
        self._write_controller_stage_result(manifest, result)
        updated = self._update_manifest(
            manifest,
            state="needs_user_decision",
            current_stage="budget",
            blocker={"code": result["failure_code"], "message": result["message"], "stage": "budget"},
            allowed_next_actions=["inspect_artifacts", "cancel"],
        )
        return updated, result

    def _apply_stage_result(self, manifest: dict[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
        value = StageResult.from_dict(result).to_dict()
        if value["status"] == "completed":
            return manifest
        if "submit_native_generation" in value["allowed_next_actions"]:
            return self._update_manifest(
                manifest,
                state="blocked",
                blocker={"code": value["failure_code"], "message": value["message"], "stage": value["stage"]},
                allowed_next_actions=value["allowed_next_actions"],
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
                "case_spec_revision_budget_exhausted",
                "candidate_budget_reserve_insufficient",
                "publication_tier_not_satisfied",
                "degraded_preview_only",
                "soft_deadline_reached",
                "ue_launch_budget_exhausted",
            } else "blocked"
            actions = (
                ["recompile_after_config", "cancel"]
                if value["failure_class"] == "blocked_configuration"
                and value["failure_code"] in {
                    "F3_UE_MAP_MISSING",
                    "F3_UE_MAP_INVALID",
                    "F3_UE_MAP_PACKAGE_MISSING",
                    "F3_UE_MAP_UNRESOLVED",
                }
                else ["resume", "cancel"]
            )
            if value["failure_code"] in {
                "case_spec_revision_budget_exhausted",
                "ue_launch_budget_exhausted",
            }:
                actions = ["inspect_artifacts", "cancel"]
            return self._update_manifest(
                manifest,
                state=state,
                blocker={"code": value["failure_code"], "message": value["message"], "stage": value["stage"]},
                allowed_next_actions=actions,
            )
        if value["failure_class"] in {"case_spec_invalid", "verification_failed", "render_sync_failed", "quality_gate_failed"}:
            if manifest["usage"]["case_spec_revisions"] >= manifest["budget"]["max_case_spec_revisions"]:
                blocked, _ = self._block_case_spec_revision_budget(manifest, trigger_result=value)
                return blocked
            return self._update_manifest(
                manifest,
                state="needs_user_decision",
                blocker={"code": value["failure_code"], "message": value["message"], "stage": value["stage"]},
                allowed_next_actions=["resume_with_revision", "cancel"],
            )
        actions = ["inspect_artifacts"]
        if (
            manifest.get("current_attempt_id") is not None
            and value["failure_class"] == "transient"
            and value["retryable"]
            and value["stage"] != "semantic_review"
        ):
            actions = ["retry_failed_stage", "inspect_artifacts", "cancel"]
        return self._update_manifest(
            manifest,
            state="failed",
            blocker={"code": value["failure_code"], "message": value["message"], "stage": value["stage"]},
            allowed_next_actions=actions,
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
        hint = str(getattr(exc, "_harness_stage", "") or "")
        selected = self._changed_noncompleted_stage_result(
            manifest,
            stage_result_snapshot=stage_result_snapshot,
            hint=hint,
        )
        if selected is not None:
            return selected
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

    def _changed_noncompleted_stage_result(
        self,
        manifest: Mapping[str, Any],
        *,
        stage_result_snapshot: Mapping[str, str] | None = None,
        hint: str = "",
    ) -> dict[str, Any] | None:
        candidates: list[tuple[Path, dict[str, Any]]] = []
        before = dict(stage_result_snapshot or {})
        for path in sorted(self._stage_artifact_root(manifest).rglob("stage_results/*.json")):
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
                "budget": 10,
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
        return None

    def _stage_result_snapshot(self, manifest: Mapping[str, Any]) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for path in self._stage_artifact_root(manifest).rglob("stage_results/*.json"):
            try:
                snapshot[str(path)] = self._sha256_file(path)
            except OSError:
                continue
        return snapshot

    def _current_leaf_stage_result(self, manifest: Mapping[str, Any]) -> dict[str, Any] | None:
        blocker = manifest.get("blocker") if isinstance(manifest.get("blocker"), Mapping) else None
        if blocker is not None:
            target_stage = str(blocker["stage"])
        elif manifest["state"] == "awaiting_semantic_review":
            attempt_id = manifest.get("current_attempt_id")
            semantic_result_path = (
                self.store.attempt_dir(str(manifest["job_id"]), str(attempt_id))
                / "stage_results"
                / "semantic_review.json"
                if attempt_id is not None
                else None
            )
            target_stage = (
                "semantic_review"
                if semantic_result_path is not None and semantic_result_path.is_file()
                else "evidence_bundle"
            )
        elif manifest["state"] == "completed":
            target_stage = "semantic_review"
        else:
            return None

        job_id = str(manifest["job_id"])
        attempt_id = manifest.get("current_attempt_id")
        job_root = self.store.job_dir(job_id)
        artifact_root = self._stage_artifact_root(manifest)
        candidates = [artifact_root / "stage_results" / f"{target_stage}.json"]
        if attempt_id is not None:
            attempt_dir = self.store.attempt_dir(job_id, str(attempt_id))
            candidates.append(attempt_dir / "compilation" / "stage_results" / f"{target_stage}.json")
            for reference_name in ("smoke_gate.json", "candidate_run.json"):
                reference_path = attempt_dir / reference_name
                if not reference_path.is_file():
                    continue
                reference = read_json(reference_path)
                run_path = reference.get("run_dir")
                if isinstance(run_path, str) and run_path:
                    candidates.append(Path(run_path) / "stage_results" / f"{target_stage}.json")
            profile = str(manifest["current_stage"])
            if profile in {"smoke", "candidate", "quality_gate", "semantic_review"}:
                run_profile = "candidate" if profile in {"quality_gate", "semantic_review"} else profile
                candidates.extend(
                    sorted((attempt_dir / "runs" / run_profile).glob(f"*/stage_results/{target_stage}.json"))
                )
        else:
            candidates.append(job_root / "stage_results" / f"{target_stage}.json")

        seen: set[Path] = set()
        matches: list[tuple[Path, dict[str, Any]]] = []
        for path in candidates:
            normalized = path.resolve(strict=False)
            if normalized in seen or not path.is_file():
                continue
            seen.add(normalized)
            try:
                normalized.relative_to(job_root.resolve())
                result = StageResult.from_dict(read_json(path)).to_dict()
            except (OSError, ValueError, TypeError):
                continue
            if result["stage"] != target_stage or result["job_id"] != job_id:
                continue
            if attempt_id is not None and result["attempt_id"] != attempt_id:
                continue
            if attempt_id is None and result["attempt_id"] is not None:
                continue
            if blocker is not None and target_stage != "semantic_review":
                if result["status"] == "completed" or result["failure_code"] != blocker["code"]:
                    continue
            matches.append((path, result))

        if not matches:
            return None
        path, result = matches[0]
        return {"path": str(path), "result": result}

    @staticmethod
    def _failed_stage_retry_eligibility(
        manifest: Mapping[str, Any],
        *,
        current_leaf_stage_result: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        unavailable = {
            "available": False,
            "stage": None,
            "failure_code": None,
            "message": "job has no terminal retryable transient failure",
        }
        if (
            manifest.get("state") != "failed"
            or manifest.get("current_attempt_id") is None
            or not isinstance(current_leaf_stage_result, Mapping)
        ):
            return unavailable
        result = current_leaf_stage_result.get("result")
        if not isinstance(result, Mapping):
            return unavailable
        if (
            result.get("status") != "failed"
            or result.get("failure_class") != "transient"
            or result.get("retryable") is not True
            or result.get("stage") == "semantic_review"
        ):
            return unavailable
        return {
            "available": True,
            "stage": str(result["stage"]),
            "failure_code": str(result["failure_code"]),
            "message": "correct the external cause, record an explicit reason, then reopen the same checkpoint",
        }

    def _case_spec_contract_repair_eligibility(
        self,
        manifest: Mapping[str, Any],
        *,
        current_leaf_stage_result: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        unavailable = {
            "available": False,
            "stage": None,
            "failure_code": None,
            "allowed_adjustments": {"paths": [], "ranges": {}},
            "provider_batch": None,
            "message": "job has no deterministic Provider contract error with an exact CaseSpec repair",
        }
        if (
            manifest.get("state") not in {"failed", "needs_user_decision"}
            or manifest.get("current_attempt_id") is None
            or not isinstance(current_leaf_stage_result, Mapping)
        ):
            return unavailable
        result = current_leaf_stage_result.get("result")
        if (
            not isinstance(result, Mapping)
            or result.get("failure_code") not in _CASE_SPEC_CONTRACT_FAILURE_CODES
            or result.get("stage") not in {"provider", "compile"}
        ):
            return unavailable
        attempt_dir = self.store.attempt_dir(
            str(manifest["job_id"]),
            str(manifest["current_attempt_id"]),
        )
        batch_path = attempt_dir / "compilation" / "asset_provider_batch.json"
        case_spec_path = attempt_dir / "case_spec.json"
        if not batch_path.is_file() or not case_spec_path.is_file():
            return unavailable
        try:
            batch = read_json(batch_path)
            case_spec = read_json(case_spec_path)
        except (OSError, TypeError, ValueError):
            return unavailable
        failed_ids = {
            str(row.get("object_id") or "")
            for row in batch.get("results") or []
            if isinstance(row, Mapping)
            and isinstance(row.get("failure"), Mapping)
            and row["failure"].get("code") in _CASE_SPEC_CONTRACT_FAILURE_CODES
        }
        requests = {
            str(row.get("object_id") or ""): row
            for row in batch.get("requests") or []
            if isinstance(row, Mapping) and row.get("object_id")
        }
        objects = {
            str(row.get("id") or ""): row
            for row in case_spec.get("objects") or []
            if isinstance(row, Mapping) and row.get("id")
        }
        if not failed_ids:
            return unavailable
        recipe_shapes = {recipe: shape for shape, recipe in RECIPE_BY_SHAPE.items()}
        constraints: dict[str, Any] = {}
        for object_id in sorted(failed_ids):
            request = requests.get(object_id)
            source_object = objects.get(object_id)
            if (
                request is None
                or source_object is None
                or not _CANONICAL_OBJECT_PATH_SEGMENT.fullmatch(object_id)
            ):
                return unavailable
            generation_spec = (
                request.get("generation_spec")
                if isinstance(request.get("generation_spec"), Mapping)
                else {}
            )
            expected_shape = recipe_shapes.get(str(generation_spec.get("recipe_id") or ""))
            geometry = (
                source_object.get("geometry")
                if isinstance(source_object.get("geometry"), Mapping)
                else {}
            )
            if expected_shape is None or geometry.get("shape_hint") == expected_shape:
                return unavailable
            path = f"$.objects.{object_id}.geometry.shape_hint"
            constraints[path] = {"kind": "enum", "values": [expected_shape]}
        paths = sorted(constraints)
        return {
            "available": True,
            "stage": str(result["stage"]),
            "failure_code": str(result["failure_code"]),
            "allowed_adjustments": {
                "paths": paths,
                "ranges": {path: constraints[path] for path in paths},
            },
            "provider_batch": str(batch_path),
            "message": "submit a revised CaseSpec changing only the listed primitive shape_hint leaves",
        }

    def _configuration_recompile_eligibility(
        self,
        manifest: Mapping[str, Any],
        *,
        current_leaf_stage_result: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        unavailable = {
            "available": False,
            "stage": None,
            "failure_code": None,
            "old_compile_config": None,
            "old_compile_config_digest": None,
            "new_compile_config": None,
            "new_compile_config_digest": None,
            "message": "job has no Map configuration blocker eligible for recompilation",
        }
        if (
            manifest.get("state") != "blocked"
            or manifest.get("current_attempt_id") is None
            or not isinstance(current_leaf_stage_result, Mapping)
        ):
            return unavailable
        result = current_leaf_stage_result.get("result")
        map_codes = {
            "F3_UE_MAP_MISSING",
            "F3_UE_MAP_INVALID",
            "F3_UE_MAP_PACKAGE_MISSING",
            "F3_UE_MAP_UNRESOLVED",
        }
        if (
            not isinstance(result, Mapping)
            or result.get("status") != "blocked"
            or result.get("failure_class") != "blocked_configuration"
            or result.get("stage") not in {"compile", "preflight"}
            or result.get("failure_code") not in map_codes
        ):
            return unavailable
        attempt_id = str(manifest["current_attempt_id"])
        attempt_dir = self.store.attempt_dir(str(manifest["job_id"]), attempt_id)
        compilation_dir = attempt_dir / "compilation"
        transaction_path = compilation_dir / "compilation_transaction.json"
        if not transaction_path.is_file():
            return {**unavailable, "message": "eligible Map blocker has no completed compilation transaction"}
        try:
            transaction = read_json(transaction_path)
            attempt = self.store.load_attempt(str(manifest["job_id"]), attempt_id)
            if (
                not isinstance(transaction, Mapping)
                or transaction.get("state") != "completed"
                or int(transaction.get("asset_resolve_invocation_count") or 0) != 1
                or (transaction.get("input_identity") or {}).get("case_spec_digest") != attempt["case_spec_digest"]
            ):
                raise ValueError("transaction is not a completed single-Resolve compilation for this CaseSpec")
            self._validated_reusable_provider_checkpoint(
                compilation_dir,
                job_id=str(manifest["job_id"]),
                attempt_id=attempt_id,
            )
            case_spec = read_json(attempt_dir / "case_spec.json")
            old_config = self._transaction_compile_config(transaction, compilation_dir)
            new_config = self.config.ue_compile_identity(case_spec)
        except (OSError, TypeError, ValueError, JobStoreError) as exc:
            return {**unavailable, "message": f"configuration recompile checkpoint is invalid: {exc}"}
        old_digest = stable_digest(old_config)
        new_digest = stable_digest(new_config)
        failure_code = str(result["failure_code"])
        map_correction_missing = (
            failure_code == "F3_UE_MAP_MISSING"
            and (
                not new_config["map_package"]
                or old_config["map_package"] == new_config["map_package"]
            )
        )
        if old_digest == new_digest or map_correction_missing:
            return {
                **unavailable,
                "stage": str(result["stage"]),
                "failure_code": str(result["failure_code"]),
                "old_compile_config": old_config,
                "old_compile_config_digest": old_digest,
                "new_compile_config": new_config,
                "new_compile_config_digest": new_digest,
                "message": "compile-affecting Map/UE project/Catalog configuration has not corrected the Map blocker",
            }
        return {
            "available": True,
            "stage": str(result["stage"]),
            "failure_code": str(result["failure_code"]),
            "old_compile_config": old_config,
            "old_compile_config_digest": old_digest,
            "new_compile_config": new_config,
            "new_compile_config_digest": new_digest,
            "message": "archive the old compilation and rebuild from compile with the same Provider checkpoint",
        }

    def _reviewer_contract_retry_eligibility(
        self,
        manifest: Mapping[str, Any],
        *,
        current_leaf_stage_result: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        unavailable = {
            "available": False,
            "failure_result_digest": None,
            "bundle_digest": None,
            "prior_invocation_count": None,
            "old_input_digest": None,
            "new_input_digest": None,
            "message": "job has no Reviewer schema failure eligible for a contract-fix retry",
        }
        if (
            manifest.get("state") != "failed"
            or manifest.get("current_stage") != "semantic_review"
            or manifest.get("current_attempt_id") is None
            or not isinstance(current_leaf_stage_result, Mapping)
        ):
            return unavailable
        result = current_leaf_stage_result.get("result")
        if (
            not isinstance(result, Mapping)
            or result.get("stage") != "semantic_review"
            or result.get("status") != "failed"
            or result.get("failure_code") != "reviewer_output_schema_invalid"
        ):
            return unavailable
        job_id = str(manifest["job_id"])
        attempt_id = str(manifest["current_attempt_id"])
        amendment_paths = sorted(
            (self.store.job_dir(job_id) / "amendments").glob("reviewer_contract_retry_*.json")
        )
        if len(amendment_paths) >= 2:
            return {**unavailable, "message": "this Job already used its two explicitly authorized Reviewer contract-fix retries"}
        try:
            bundle = self._validated_current_bundle(manifest)
            if not self._technical_completion_intact(manifest, bundle):
                raise ValueError("Candidate technical gates are no longer intact")
            bundle_digest = stable_digest(bundle)
            request = read_json(self.store.job_dir(job_id) / "request" / "user_request.json")
            has_images = any(
                isinstance(row, Mapping) and row.get("kind") == "image"
                for row in request.get("inputs") or []
            )
            attempt_dir = self.store.attempt_dir(job_id, attempt_id)
            reservation_paths = sorted(attempt_dir.glob("reviewer_reservation_*.json"))
            if not reservation_paths:
                raise ValueError("Reviewer reservations are missing")
            first = ReviewerInvocationReservation.from_dict(read_json(reservation_paths[0])).to_dict()
            old_input_digest = str(first["input_digest"])
            current_input_digest = semantic_reviewer_input_digest(
                bundle_dir=attempt_dir / "evidence_bundle",
                bundle_manifest=bundle,
                include_original_images=has_images,
            )
            reservations = self._reviewer_reservations(
                attempt_dir,
                job_id=job_id,
                attempt_id=attempt_id,
                bundle_digest=bundle_digest,
                input_digest=current_input_digest if amendment_paths else old_input_digest,
            )
            if (
                len(reservations) != int(result.get("invocation_count") or 0)
                or not all(bool(row["usage_counted"]) for row in reservations)
                or reservations[-1]["outcome"] != "technical_failed"
                or reservations[-1]["error_code"] != "reviewer_output_schema_invalid"
            ):
                raise ValueError("Reviewer failure history is not a completed schema-invalid retry sequence")
            old_input_digest = str(reservations[-1]["input_digest"])
            new_input_digest = current_input_digest
            if old_input_digest == new_input_digest:
                raise ValueError("Reviewer prompt/output contract digest has not changed")
        except (EvidenceBundleError, JobStoreError, OSError, TypeError, ValueError) as exc:
            return {**unavailable, "message": f"Reviewer contract-fix retry is unavailable: {exc}"}
        return {
            "available": True,
            "failure_result_digest": stable_digest(result),
            "bundle_digest": bundle_digest,
            "prior_invocation_count": len(reservations),
            "old_input_digest": old_input_digest,
            "new_input_digest": new_input_digest,
            "message": "authorize one new Reviewer turn against the corrected prompt/output contract",
        }

    @staticmethod
    def _transaction_compile_config(
        transaction: Mapping[str, Any],
        compilation_dir: Path,
    ) -> dict[str, str]:
        recorded = transaction.get("compile_config")
        if isinstance(recorded, Mapping) and set(recorded) == {
            "schema_version",
            "map_package",
            "ue_project",
            "catalog",
        }:
            return {key: str(recorded[key]) for key in recorded}
        resolution_path = compilation_dir / "asset_resolution.json"
        resolution = read_json(resolution_path) if resolution_path.is_file() else {}
        scene_map = resolution.get("scene_map") if isinstance(resolution, Mapping) else None
        map_package = (
            str(scene_map.get("requested_reference") or "")
            if isinstance(scene_map, Mapping)
            else ""
        )
        snapshot = transaction.get("catalog_snapshot")
        catalog = str(snapshot.get("path") or "") if isinstance(snapshot, Mapping) else ""
        return {
            "schema_version": "harness_ue_compile_config_v1",
            "map_package": map_package,
            "ue_project": "",
            "catalog": catalog,
        }

    @staticmethod
    def _validated_reusable_provider_checkpoint(
        compilation_dir: Path,
        *,
        job_id: str,
        attempt_id: str,
    ) -> dict[str, Any]:
        batch_path = compilation_dir / "asset_provider_batch.json"
        if not batch_path.is_file():
            raise JobStoreError("completed compilation has no Provider batch checkpoint")
        batch = read_json(batch_path)
        if not isinstance(batch, Mapping):
            raise JobStoreError("Provider batch checkpoint must be an object")
        result = stage_result_from_provider_batch(batch, job_id=job_id, attempt_id=attempt_id)
        if result["status"] != "completed":
            raise JobStoreError("Provider batch checkpoint is not completed")
        request_identities = list(result["request_identities"])
        if any(not identity for identity in request_identities) or len(request_identities) != len(set(request_identities)):
            raise JobStoreError("Provider batch request identities are missing or duplicated")
        receipt_ids = [str(value) for value in batch.get("receipt_ids") or []]
        if len(receipt_ids) != len(set(receipt_ids)) or any(not value for value in receipt_ids):
            raise JobStoreError("Provider receipt identities are missing or duplicated")
        for receipt_id in receipt_ids:
            path = compilation_dir / "provider_receipts" / f"{receipt_id}.json"
            receipt = read_json(path) if path.is_file() else None
            if not isinstance(receipt, Mapping) or receipt.get("receipt_id") != receipt_id:
                raise JobStoreError(f"Provider receipt checkpoint is missing or mismatched: {receipt_id}")
        return {
            "batch_digest": stable_digest(batch),
            "request_identities": request_identities,
            "receipt_ids": receipt_ids,
        }

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

    def _interrupted_recovery(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        if manifest["state"] != "running":
            return {
                "available": False,
                "stage": None,
                "marker_stage": None,
                "reason": "job_not_running",
                "message": f"job is not in running state: {manifest['state']}",
            }
        marker_path = self.store.job_dir(manifest["job_id"]) / "checkpoints" / "in_flight.json"
        marker = read_json(marker_path) if marker_path.is_file() else None
        if (
            isinstance(marker, Mapping)
            and marker.get("schema_version") == "harness_agent_in_flight_v1"
            and marker.get("job_id") == manifest["job_id"]
            and marker.get("status") == "active"
            and marker.get("stage") == manifest["current_stage"]
        ):
            return {
                "available": True,
                "stage": str(marker["stage"]),
                "marker_stage": str(marker["stage"]),
                "reason": "active_in_flight_marker",
                "message": "running stage has an unclosed durable in-flight marker",
            }
        if (
            isinstance(marker, Mapping)
            and marker.get("schema_version") == "harness_agent_in_flight_v1"
            and marker.get("job_id") == manifest["job_id"]
            and marker.get("status") in {"active", "closed"}
            and marker.get("stage") != manifest["current_stage"]
            and self._completed_stage_transition(
                manifest,
                previous_stage=str(marker.get("stage") or ""),
                current_stage=str(manifest["current_stage"]),
            )
        ):
            return {
                "available": True,
                "stage": str(manifest["current_stage"]),
                "marker_stage": str(marker["stage"]),
                "reason": "completed_stage_transition",
                "message": "the prior stage completed durably before its in-flight lifecycle closed",
            }
        events = sorted((self.store.job_dir(manifest["job_id"]) / "events").glob("*.json"))
        latest = read_json(events[-1]) if events else None
        if (
            isinstance(latest, Mapping)
            and latest.get("event") == "stage_started"
            and latest.get("stage") == manifest["current_stage"]
        ):
            return {
                "available": True,
                "stage": str(latest["stage"]),
                "marker_stage": str(latest["stage"]),
                "reason": "unclosed_stage_started_event",
                "message": "running stage has no matching completion or blocked event",
            }
        return {
            "available": False,
            "stage": None,
            "marker_stage": None,
            "reason": "no_unclosed_stage_evidence",
            "message": "running job has no durable evidence of an interrupted stage",
        }

    def _completed_stage_transition(
        self,
        manifest: Mapping[str, Any],
        *,
        previous_stage: str,
        current_stage: str,
    ) -> bool:
        if _NEXT_CONTROLLER_STAGE.get(previous_stage) != current_stage:
            return False
        checkpoint_path = self.store.job_dir(manifest["job_id"]) / "checkpoints" / f"{previous_stage}.json"
        if not checkpoint_path.is_file():
            return False
        try:
            checkpoint = read_json(checkpoint_path)
        except (OSError, ValueError, TypeError):
            return False
        if (
            not isinstance(checkpoint, Mapping)
            or checkpoint.get("job_id") != manifest["job_id"]
            or checkpoint.get("stage") != previous_stage
            or checkpoint.get("status") not in {"completed", "reused"}
        ):
            return False
        if previous_stage in {"smoke", "candidate"}:
            refs = checkpoint.get("artifact_refs") or []
            run_paths = [Path(str(row.get("path") or "")) for row in refs if isinstance(row, Mapping)]
            return any(self._run_technical_pass(path) for path in run_paths)
        job_root = self.store.job_dir(manifest["job_id"])
        for path in job_root.rglob(f"stage_results/{previous_stage}.json"):
            try:
                result = StageResult.from_dict(read_json(path)).to_dict()
            except (OSError, ValueError, TypeError):
                continue
            if result["job_id"] != manifest["job_id"] or result["status"] != "completed":
                continue
            if result["attempt_id"] is not None and result["attempt_id"] != manifest.get("current_attempt_id"):
                continue
            return True
        return False

    def _start_in_flight(
        self,
        manifest: Mapping[str, Any],
        stage: str,
        *,
        started_monotonic: float | None = None,
    ) -> None:
        write_json(
            self.store.job_dir(manifest["job_id"]) / "checkpoints" / "in_flight.json",
            {
                "schema_version": "harness_agent_in_flight_v1",
                "job_id": manifest["job_id"],
                "attempt_id": manifest.get("current_attempt_id"),
                "stage": stage,
                "status": "active",
                "controller_pid": os.getpid(),
                "started_at": utc_now(),
                "started_at_epoch": time.time(),
                "started_monotonic": float(
                    self.hooks.monotonic() if started_monotonic is None else started_monotonic
                ),
                "active_elapsed_baseline": float(manifest["usage"]["active_elapsed_seconds"]),
                "elapsed_accounted_seconds": 0.0,
                "finished_at": None,
                "outcome": None,
            },
        )

    def _reconcile_in_flight_elapsed(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        path = self.store.job_dir(manifest["job_id"]) / "checkpoints" / "in_flight.json"
        if not path.is_file():
            return dict(manifest)
        marker = read_json(path)
        if (
            not isinstance(marker, Mapping)
            or marker.get("schema_version") != "harness_agent_in_flight_v1"
            or marker.get("job_id") != manifest["job_id"]
        ):
            return dict(manifest)
        elapsed = max(0.0, float(marker.get("elapsed_accounted_seconds") or 0.0))
        monotonic_start = marker.get("started_monotonic")
        monotonic_now = (
            marker.get("finished_monotonic")
            if marker.get("status") == "closed"
            else self.hooks.monotonic()
        )
        if (
            isinstance(monotonic_start, (int, float))
            and not isinstance(monotonic_start, bool)
            and isinstance(monotonic_now, (int, float))
            and not isinstance(monotonic_now, bool)
            and monotonic_now >= monotonic_start
        ):
            elapsed = max(elapsed, float(monotonic_now) - float(monotonic_start))
        else:
            epoch_start = marker.get("started_at_epoch")
            if isinstance(epoch_start, (int, float)) and not isinstance(epoch_start, bool):
                elapsed = max(elapsed, time.time() - float(epoch_start))
        updated_marker = dict(marker)
        updated_marker["elapsed_accounted_seconds"] = round(elapsed, 6)
        updated_marker["elapsed_accounted_at"] = utc_now()
        write_json(path, updated_marker)
        baseline = max(0.0, float(marker.get("active_elapsed_baseline") or 0.0))
        target = round(baseline + elapsed, 6)
        current = self.store.load_manifest(manifest["job_id"])
        if float(current["usage"]["active_elapsed_seconds"]) >= target:
            return current
        usage = copy.deepcopy(current["usage"])
        usage["active_elapsed_seconds"] = target
        return self._update_manifest(current, usage=usage)

    def _finish_in_flight(
        self,
        job_id: str,
        stage: str,
        outcome: str,
        *,
        finished_monotonic: float | None = None,
    ) -> None:
        path = self.store.job_dir(job_id) / "checkpoints" / "in_flight.json"
        if not path.is_file():
            return
        marker = read_json(path)
        if not isinstance(marker, Mapping) or marker.get("job_id") != job_id or marker.get("stage") != stage:
            return
        closed = dict(marker)
        finished = float(self.hooks.monotonic() if finished_monotonic is None else finished_monotonic)
        started = marker.get("started_monotonic")
        accounted = max(0.0, float(marker.get("elapsed_accounted_seconds") or 0.0))
        if isinstance(started, (int, float)) and not isinstance(started, bool) and finished >= started:
            accounted = max(accounted, finished - float(started))
        closed.update(
            {
                "status": "closed",
                "finished_at": utc_now(),
                "finished_monotonic": finished,
                "elapsed_accounted_seconds": round(accounted, 6),
                "elapsed_accounted_at": utc_now(),
                "outcome": outcome,
            }
        )
        write_json(path, closed)

    def _emit_semantic_outcome_event(self, manifest: Mapping[str, Any]) -> None:
        leaf = self._current_leaf_stage_result(manifest)
        result = leaf["result"] if leaf is not None else None
        if result is not None and result["stage"] == "semantic_review" and result["status"] != "completed":
            self._stage_event(manifest, "stage_blocked", "semantic_review", result=result)
        else:
            self._stage_event(manifest, "stage_completed", "semantic_review")

    def _emit_job_terminal_if_terminal(self, manifest: Mapping[str, Any]) -> None:
        if manifest["state"] in TERMINAL_JOB_STATES:
            self._emit(
                str(manifest["job_id"]),
                "job_terminal",
                stage=manifest["current_stage"],
                state=manifest["state"],
            )

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
        ue_launch_ledger_path = self._ensure_ue_launch_ledger(manifest)
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
            ue_launch_ledger_path=ue_launch_ledger_path,
            usage_job_id=str(manifest["job_id"]),
            usage_attempt_id=str(manifest["current_attempt_id"]),
            max_ue_launches=int(manifest["budget"]["max_ue_launches"]),
        )

    def _ensure_ue_launch_ledger(self, manifest: Mapping[str, Any]) -> Path:
        path = self.store.job_dir(manifest["job_id"]) / "receipts" / "ue_launch_usage.json"
        if not path.is_file():
            baseline = int(manifest["usage"]["ue_launches"])
            migration = self._legacy_importer_launch_count(manifest) if baseline == 0 else 0
            baseline = max(baseline, migration)
            write_json(
                path,
                {
                    "schema_version": "harness_agent_ue_launch_ledger_v1",
                    "job_id": manifest["job_id"],
                    "baseline_launches": baseline,
                    "launches": [],
                    "legacy_importer_launches_reconciled": migration,
                },
            )
        return path

    def _legacy_importer_launch_count(self, manifest: Mapping[str, Any]) -> int:
        attempt_id = manifest.get("current_attempt_id")
        if not attempt_id:
            return 0
        compilation_dir = self.store.attempt_dir(manifest["job_id"], str(attempt_id)) / "compilation"
        provider_result_path = compilation_dir / "stage_results" / "provider.json"
        batch_path = compilation_dir / "asset_provider_batch.json"
        if not provider_result_path.is_file() or not batch_path.is_file():
            return 0
        try:
            result = StageResult.from_dict(read_json(provider_result_path)).to_dict()
            batch = read_json(batch_path)
            per_invocation = int((batch.get("import_summary") or {}).get("importer_invocation_count") or 0)
        except (OSError, TypeError, ValueError):
            return 0
        if (
            result["stage"] != "provider"
            or result["failure_code"] not in {"backend_importer_timeout", "backend_importer_execution_failed"}
            or per_invocation < 1
        ):
            return 0
        return int(result["invocation_count"]) * per_invocation

    def _record_controller_ue_launch(self, manifest: Mapping[str, Any], *, kind: str, stage: str) -> None:
        path = self._ensure_ue_launch_ledger(manifest)
        ledger = read_json(path)
        launches = [dict(row) for row in ledger.get("launches") or [] if isinstance(row, Mapping)]
        launches.append(
            {
                "sequence": len(launches) + 1,
                "kind": kind,
                "attempt_id": manifest.get("current_attempt_id"),
                "stage": stage,
                "recorded_at_epoch": time.time(),
            }
        )
        write_json(path, {**ledger, "launches": launches})

    def _reconcile_ue_launch_usage(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        path = self.store.job_dir(manifest["job_id"]) / "receipts" / "ue_launch_usage.json"
        if not path.is_file():
            return dict(manifest)
        ledger = read_json(path)
        if (
            not isinstance(ledger, Mapping)
            or ledger.get("schema_version") != "harness_agent_ue_launch_ledger_v1"
            or ledger.get("job_id") != manifest["job_id"]
            or not isinstance(ledger.get("baseline_launches"), int)
            or isinstance(ledger.get("baseline_launches"), bool)
            or int(ledger.get("baseline_launches")) < 0
            or not isinstance(ledger.get("launches"), list)
            or any(not isinstance(row, Mapping) for row in ledger.get("launches") or [])
        ):
            raise JobStoreError("UE launch usage ledger is invalid")
        current = self.store.load_manifest(manifest["job_id"])
        target = int(ledger["baseline_launches"]) + len(ledger["launches"])
        if int(current["usage"]["ue_launches"]) == target:
            return current
        if int(current["usage"]["ue_launches"]) > target:
            raise JobStoreError("UE launch usage ledger would decrease recorded usage")
        usage = copy.deepcopy(current["usage"])
        usage["ue_launches"] = target
        return self._update_manifest(current, usage=usage)

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
                "ue_execution_config": self.config.ue_execution_identity(case_spec),
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
    def _effective_environment(self, manifest: Mapping[str, Any] | None = None):
        case_spec = None
        if manifest is not None and manifest.get("current_attempt_id"):
            path = self.store.attempt_dir(
                str(manifest["job_id"]), str(manifest["current_attempt_id"])
            ) / "case_spec.json"
            if path.is_file():
                case_spec = read_json(path)
        values = {
            "SIM_HARNESS_WORKSPACE": str(self.config.workspace),
            "SIM_HARNESS_ASSET_CATALOG": str(self.config.catalog),
            "SIM_STUDIO_UE_PROJECT": str(self.config.ue_project),
            "SIM_STUDIO_UE_MAP": self.config.ue_map_package_for_case(case_spec),
            "SIM_STUDIO_UE_ACTOR_CLASS": self.config.ue_actor_class,
            "SIM_STUDIO_UE_CONTACT_EXPORT": "1" if self.config.ue_contact_export else "0",
        }
        if self.config.ue_asset_registry is not None:
            values["SIM_STUDIO_ASSET_REGISTRY"] = str(self.config.ue_asset_registry)
        if self.config.ue_runner_command:
            values["SIM_STUDIO_UE_RUNNER_CMD"] = shlex.join(self.config.ue_runner_command)
        if self.config.ue_executable is not None:
            values["SIM_STUDIO_UE_EXECUTABLE"] = str(self.config.ue_executable)
        if self.config.codex_executable is not None:
            values["SIM_HARNESS_CODEX_EXECUTABLE"] = str(self.config.codex_executable)
        if self.config.ue_asset_importer_command:
            values["SIM_HARNESS_UE_ASSET_IMPORTER_CMD"] = shlex.join(self.config.ue_asset_importer_command)
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
        if path == "$.objects" and isinstance(before, list) and isinstance(after, list):
            before_by_id = cls._canonical_object_list(before)
            after_by_id = cls._canonical_object_list(after)
            if before_by_id is not None and after_by_id is not None and set(before_by_id) == set(after_by_id):
                changes = []
                for object_id in sorted(before_by_id):
                    changes.extend(
                        cls._json_diff(
                            before_by_id[object_id],
                            after_by_id[object_id],
                            f"{path}.{object_id}",
                        )
                    )
                return changes
        return [{"path": path, "operation": "replace", "before": before, "after": after}]

    @staticmethod
    def _canonical_object_list(value: list[Any]) -> dict[str, Mapping[str, Any]] | None:
        rows: dict[str, Mapping[str, Any]] = {}
        for row in value:
            if not isinstance(row, Mapping):
                return None
            object_id = str(row.get("id") or "")
            if not _CANONICAL_OBJECT_PATH_SEGMENT.fullmatch(object_id) or object_id in rows:
                return None
            rows[object_id] = row
        return rows

    @classmethod
    def _case_spec_revision_policy(
        cls,
        case_spec: Mapping[str, Any],
        *,
        excluded_paths: set[str] | None = None,
    ) -> dict[str, Any]:
        """Open bounded layout and tunable physics leaves without changing topology."""
        excluded = excluded_paths or set()
        raw_bounds = (case_spec.get("scene") or {}).get("bounds_hint_m")
        scene_bounds = (
            [float(value) for value in raw_bounds]
            if isinstance(raw_bounds, list)
            and len(raw_bounds) == 3
            and all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                and float(value) > 0.0
                for value in raw_bounds
            )
            else [20.0, 20.0, 20.0]
        )
        constraints: dict[str, Any] = {}

        def add_constraint(path: str, value: Any, constraint: Mapping[str, Any]) -> None:
            if path not in excluded and value is not None:
                constraints[path] = dict(constraint)

        for obj in case_spec.get("objects") or []:
            if not isinstance(obj, Mapping):
                continue
            object_id = str(obj.get("id") or "")
            if not _CANONICAL_OBJECT_PATH_SEGMENT.fullmatch(object_id):
                continue
            initial = obj.get("initial_state") if isinstance(obj.get("initial_state"), Mapping) else {}
            position = initial.get("position_m")
            position_path = f"$.objects.{object_id}.initial_state.position_m"
            if position_path not in excluded and cls._finite_numeric_vector(position, length=3):
                limits = [
                    max(scene_bounds[index], abs(float(position[index])))
                    for index in range(3)
                ]
                constraints[position_path] = {
                    "kind": "numeric_vector",
                    "min": [-value for value in limits],
                    "max": limits,
                }
            rotation = initial.get("rotation_deg")
            rotation_path = f"$.objects.{object_id}.initial_state.rotation_deg"
            if rotation_path not in excluded and cls._finite_numeric_vector(rotation, length=3):
                constraints[rotation_path] = {
                    "kind": "numeric_vector",
                    "min": [-180.0, -180.0, -180.0],
                    "max": [180.0, 180.0, 180.0],
                }
            physics = obj.get("physics") if isinstance(obj.get("physics"), Mapping) else {}
            body_type = str(physics.get("body_type") or "").casefold()
            if body_type in {"static", "kinematic"}:
                geometry = obj.get("geometry") if isinstance(obj.get("geometry"), Mapping) else {}
                size = geometry.get("approx_size_m")
                size_path = f"$.objects.{object_id}.geometry.approx_size_m"
                if size_path not in excluded and cls._finite_numeric_vector(size, length=3):
                    constraints[size_path] = {
                        "kind": "numeric_vector",
                        "min": [0.001, 0.001, 0.001],
                        "max": [
                            max(scene_bounds[index] * 2.0, float(size[index]))
                            for index in range(3)
                        ],
                    }

            if body_type == "dynamic":
                for field, limit in (
                    ("linear_velocity_m_s", 1000.0),
                    ("angular_velocity_rad_s", 10000.0),
                    ("angular_velocity_deg_s", math.degrees(10000.0)),
                ):
                    value = initial.get(field)
                    if not cls._finite_numeric_vector(value, length=3):
                        continue
                    bound = max(limit, *(abs(float(component)) for component in value))
                    add_constraint(
                        f"$.objects.{object_id}.initial_state.{field}",
                        value,
                        {
                            "kind": "numeric_vector",
                            "min": [-bound, -bound, -bound],
                            "max": [bound, bound, bound],
                        },
                    )

                for field, minimum, maximum in (
                    ("mass_kg", 1e-9, 1e6),
                    ("linear_damping", 0.0, 1000.0),
                    ("angular_damping", 0.0, 1000.0),
                ):
                    value = physics.get(field)
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                        add_constraint(
                            f"$.objects.{object_id}.physics.{field}",
                            value,
                            {"kind": "numeric", "min": minimum, "max": max(maximum, float(value))},
                        )
                for field in ("enable_gravity", "use_ccd"):
                    value = physics.get(field)
                    if isinstance(value, bool):
                        add_constraint(
                            f"$.objects.{object_id}.physics.{field}",
                            value,
                            {"kind": "enum", "values": [False, True]},
                        )

            material = physics.get("material") if isinstance(physics.get("material"), Mapping) else {}
            for field, maximum in (
                ("static_friction", 100.0),
                ("dynamic_friction", 100.0),
                ("restitution", 1.0),
            ):
                value = material.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                    add_constraint(
                        f"$.objects.{object_id}.physics.material.{field}",
                        value,
                        {"kind": "numeric", "min": 0.0, "max": max(maximum, float(value))},
                    )
        paths = sorted(constraints)
        return {
            "paths": paths,
            "ranges": {path: constraints[path] for path in paths},
        }

    @staticmethod
    def _finite_numeric_vector(value: Any, *, length: int) -> bool:
        return bool(
            isinstance(value, list)
            and len(value) == length
            and all(
                isinstance(component, (int, float))
                and not isinstance(component, bool)
                and math.isfinite(float(component))
                for component in value
            )
        )

    @staticmethod
    def _overlay_allowed_adjustments(
        base: Mapping[str, Any],
        override: Mapping[str, Any],
    ) -> dict[str, Any]:
        ranges = copy.deepcopy(dict(base.get("ranges") or {}))
        override_ranges = override.get("ranges") if isinstance(override.get("ranges"), Mapping) else {}
        for path in override.get("paths") or []:
            ranges[str(path)] = copy.deepcopy(override_ranges.get(str(path)))
        paths = sorted(ranges)
        return {"paths": paths, "ranges": {path: ranges[path] for path in paths}}

    @staticmethod
    def _merge_allowed_adjustments(
        base: Mapping[str, Any],
        additional: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        paths = [str(path) for path in base.get("paths") or []]
        ranges = copy.deepcopy(dict(base.get("ranges") or {}))
        if additional is not None:
            extra_ranges = additional.get("ranges") if isinstance(additional.get("ranges"), Mapping) else {}
            for path in additional.get("paths") or []:
                canonical = str(path)
                constraint = extra_ranges.get(canonical)
                if canonical in ranges and ranges[canonical] != constraint:
                    raise JobStoreError(f"conflicting allowed adjustment contract for {canonical}")
                if canonical not in paths:
                    paths.append(canonical)
                ranges[canonical] = copy.deepcopy(constraint)
        return {"paths": sorted(paths), "ranges": {path: ranges[path] for path in sorted(paths)}}

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
            elif kind == "numeric_vector":
                minimum = constraint.get("min")
                maximum = constraint.get("max")
                if (
                    not isinstance(value, list)
                    or not isinstance(minimum, list)
                    or not isinstance(maximum, list)
                    or len(value) != len(minimum)
                    or len(value) != len(maximum)
                    or any(
                        isinstance(component, bool)
                        or not isinstance(component, (int, float))
                        or component < low
                        or component > high
                        for component, low, high in zip(value, minimum, maximum)
                    )
                ):
                    raise JobStoreError(f"revision value at {path} exceeds Intent Contract numeric vector range")
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
        require_result_provenance: bool = True,
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
        validate_current_evidence_bundle(
            manifest_path=attempt_dir / "evidence_bundle" / "manifest.json",
            job_id=job_id,
            attempt=attempt,
            attempt_dir=attempt_dir,
            expected_intent_contract_digest=str(manifest["intent_contract_digest"]),
            expected_snapshots=snapshots,
            expected_manifest_digest=expected_manifest_digest,
        )
        result_provenance = None
        if require_result_provenance:
            candidate = read_json(attempt_dir / "candidate_run.json")
            result_provenance = self._evidence_result_provenance(
                manifest,
                attempt_dir=attempt_dir,
                run_dir=Path(str(candidate["run_dir"])),
            )
        return validate_current_evidence_bundle(
            manifest_path=attempt_dir / "evidence_bundle" / "manifest.json",
            job_id=job_id,
            attempt=attempt,
            attempt_dir=attempt_dir,
            expected_intent_contract_digest=str(manifest["intent_contract_digest"]),
            expected_snapshots=snapshots,
            expected_result_provenance=result_provenance,
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
        transitions = self._reviewer_contract_retry_transitions(
            attempt_dir,
            job_id=job_id,
            attempt_id=attempt_id,
            bundle_digest=bundle_digest,
            input_digest=input_digest,
        )
        reservations: list[dict[str, Any]] = []
        for expected_count, path in enumerate(sorted(attempt_dir.glob("reviewer_reservation_*.json")), start=1):
            reservation = ReviewerInvocationReservation.from_dict(read_json(path)).to_dict()
            expected_input_digest = transitions[0]["old_input_digest"] if transitions else input_digest
            for transition in transitions:
                if expected_count > transition["prior_invocation_count"]:
                    expected_input_digest = transition["new_input_digest"]
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
                or reservation["input_digest"] != expected_input_digest
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
                    or receipt["input_digest"] != reservation["input_digest"]
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

    def _reviewer_contract_retry_transitions(
        self,
        attempt_dir: Path,
        *,
        job_id: str,
        attempt_id: str,
        bundle_digest: str,
        input_digest: str,
    ) -> list[dict[str, Any]]:
        paths = sorted(
            (self.store.job_dir(job_id) / "amendments").glob("reviewer_contract_retry_*.json")
        )
        if not paths:
            return []
        if len(paths) > 2:
            raise JobStoreError("Reviewer contract-fix retry receipt sequence is invalid")
        common_fields = {
            "schema_version",
            "job_id",
            "attempt_id",
            "failure_code",
            "failure_result_digest",
            "bundle_digest",
            "prior_invocation_count",
            "old_input_digest",
            "new_input_digest",
            "reviewer_technical_retries_before",
            "reviewer_technical_retries_after",
            "usage_preserved",
            "correction_reason",
            "created_at",
        }
        transitions: list[dict[str, Any]] = []
        for sequence, path in enumerate(paths, start=1):
            if path.name != f"reviewer_contract_retry_{sequence:03d}.json":
                raise JobStoreError("Reviewer contract-fix retry receipt sequence is invalid")
            data = read_json(path)
            schema_version = data.get("schema_version") if isinstance(data, Mapping) else None
            expected = (
                common_fields
                if schema_version == "harness_agent_reviewer_contract_retry_v1"
                else common_fields | {"total_retries_before", "total_retries_after"}
            )
            if (
                not isinstance(data, Mapping)
                or schema_version not in {
                    "harness_agent_reviewer_contract_retry_v1",
                    "harness_agent_reviewer_contract_retry_v2",
                }
                or set(data) != expected
            ):
                raise JobStoreError("Reviewer contract-fix retry receipt has invalid fields")
            prior_count = data["prior_invocation_count"]
            before = data["reviewer_technical_retries_before"]
            after = data["reviewer_technical_retries_after"]
            digests_valid = all(
                isinstance(data[field], str) and re.fullmatch(r"[0-9a-f]{64}", data[field])
                for field in ("failure_result_digest", "bundle_digest", "old_input_digest", "new_input_digest")
            )
            totals_valid = True
            if schema_version == "harness_agent_reviewer_contract_retry_v2":
                total_before = data["total_retries_before"]
                total_after = data["total_retries_after"]
                totals_valid = (
                    isinstance(total_before, int)
                    and not isinstance(total_before, bool)
                    and isinstance(total_after, int)
                    and not isinstance(total_after, bool)
                    and total_after in {total_before, total_before + 1}
                )
            previous = transitions[-1] if transitions else None
            if (
                data["job_id"] != job_id
                or data["attempt_id"] != attempt_id
                or data["failure_code"] != "reviewer_output_schema_invalid"
                or data["bundle_digest"] != bundle_digest
                or data["old_input_digest"] == data["new_input_digest"]
                or not digests_valid
                or not isinstance(prior_count, int)
                or isinstance(prior_count, bool)
                or prior_count < 1
                or not isinstance(before, int)
                or isinstance(before, bool)
                or not isinstance(after, int)
                or isinstance(after, bool)
                or after != before + 1
                or not totals_valid
                or (
                    previous is not None
                    and (
                        data["old_input_digest"] != previous["new_input_digest"]
                        or prior_count <= previous["prior_invocation_count"]
                        or before != previous["reviewer_technical_retries_after"]
                    )
                )
            ):
                raise JobStoreError("Reviewer contract-fix retry receipt identity is invalid")
            transitions.append(dict(data))
        reservation_count = len(list(attempt_dir.glob("reviewer_reservation_*.json")))
        if any(row["prior_invocation_count"] > reservation_count for row in transitions):
            raise JobStoreError("Reviewer contract-fix retry receipt references a missing prior invocation")
        if reservation_count == transitions[-1]["prior_invocation_count"] and input_digest != transitions[-1]["new_input_digest"]:
            raise JobStoreError("Reviewer contract-fix retry input digest does not match the authorized transition")
        return transitions

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
            review_roots = [attempt_dir, *sorted(attempt_dir.glob("semantic_review_superseded_*"))]
            for review_root in review_roots:
                for expected_count, path in enumerate(
                    sorted(review_root.glob("reviewer_reservation_*.json")),
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
