from __future__ import annotations

import copy
import hashlib
import json
import os
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
from harness.agent.job_store import JobStore, JobStoreError
from harness.assets.asset_registry import AssetRegistry
from harness.assets.providers.orchestrator import AssetProviderOrchestrator
from harness.assets.providers.input_manifest import PROVIDER_INPUT_MANIFEST_SCHEMA, build_provider_input_manifest
from harness.assets.providers.remote import MESHY_API_KEY_ENV
from harness.core.artifact_schema import read_json, write_json
from harness.core.case_spec_v2 import (
    CaseSpecV2,
    asset_requests,
    case_spec_v2_from_dict,
    compile_case_spec_v2_runtime,
)
from harness.core.stage_result import (
    StageResult,
    build_stage_result,
    failure_stage_result,
    write_stage_result,
)
from harness.planning.backend_planner import plan_backend
from harness.planning.case_generation import REQUEST_SCHEMA_VERSION, generate_case_spec_v2
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
    ) -> None:
        self.store = JobStore(workspace)
        self.hooks = hooks or ControllerHooks()
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
    ) -> dict[str, Any]:
        request_data = self._validate_request(request)
        identity = validate_job_id(job_id or self._new_job_id())
        auth = {
            "planning_llm_upload": bool(request_data.get("inputs")) and all(
                row.get("external_upload_authorized") is True for row in request_data.get("inputs") or []
            ),
            "meshy_upload": False,
            "external_provider": False,
            "paid_provider_submission": False,
        }
        for key, value in dict(authorizations or {}).items():
            if key not in auth or not isinstance(value, bool):
                raise ValueError(f"unsupported or invalid authorization: {key}")
            auth[key] = value
        if request_data.get("inputs") and auth["planning_llm_upload"] != all(
            row.get("external_upload_authorized") is True for row in request_data["inputs"]
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
        self._emit(identity, "job_created", stage="intake_readiness", artifact_refs=[str(root / "job_manifest.json")])
        return self.inspect(identity)

    def inspect(self, job_id: str) -> dict[str, Any]:
        manifest = self.store.load_manifest(job_id)
        root = self.store.job_dir(job_id)
        attempts = []
        for path in sorted((root / "attempts").glob("attempt_*/attempt_manifest.json")):
            attempts.append(AttemptManifest.from_dict(read_json(path)).to_dict())
        return {
            "schema_version": "harness_agent_job_inspection_v1",
            "job": manifest,
            "attempts": attempts,
            "paths": {
                "job_root": str(root),
                "job_manifest": str(root / "job_manifest.json"),
                "intent_contract": str(root / "request" / "intent_contract.json") if (root / "request" / "intent_contract.json").is_file() else None,
            },
        }

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
        with self.store.lock(job_id):
            manifest = self.store.load_manifest(job_id)
            if manifest["state"] not in {"blocked", "needs_user_decision", "paused_interrupted", "failed"}:
                raise JobStoreError(f"job cannot be resumed from state {manifest['state']}")
            if manifest["state"] == "failed" and (manifest.get("blocker") or {}).get("code") != "budget_exhausted":
                raise JobStoreError("only budget-exhausted failed jobs may be resumed")
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
                manifest = self._create_revision(manifest, revised_case_spec, revision_reason or "user-approved revision")
            manifest = self._update_manifest(manifest, state="running", blocker=None, allowed_next_actions=["cancel"])
        return self.advance_until_blocked(job_id)

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
        raise RuntimeError(f"unsupported controller stage: {stage}")

    def _advance_l0(self, manifest: dict[str, Any]) -> dict[str, Any]:
        job_id = manifest["job_id"]
        request = read_json(self.store.job_dir(job_id) / "request" / "user_request.json")
        failures = []
        if stable_digest(request) != manifest["request_digest"]:
            failures.append(("request_digest_mismatch", "immutable request digest changed"))
        if not str(request.get("text") or "").strip() and not request.get("inputs"):
            failures.append(("request_input_missing", "request requires text, an image, or both"))
        for row in request.get("inputs") or []:
            path = Path(str(row.get("local_path") or ""))
            if not path.is_file() or self._sha256_file(path) != str(row.get("sha256") or ""):
                failures.append(("request_input_identity_mismatch", f"input identity changed: {row.get('input_id')}"))
        catalog_path = self.store.workspace / "catalog" / "assets" / "catalog.sqlite"
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
        generation_dir = root / "request" / "generation"
        seed_path = root / "request" / "seed_case_spec.json"
        if seed_path.is_file():
            case_spec = case_spec_v2_from_dict(
                read_json(seed_path),
                available_input_ids=[str(row.get("input_id")) for row in request.get("inputs") or []],
            )
            expansion = {"schema_version": "harness_expansion_v1", "ambiguities": [], "assumptions": []}
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
            generated = self.hooks.generate(
                request,
                artifact_dir=generation_dir,
                job_id=job_id,
                attempt_id="attempt_001",
            )
            case_spec = generated.case_spec
            expansion = generated.expansion
            stage_result = generated.stage_result or read_json(generation_dir / "stage_results" / "generation.json")
        projected_intent = self._project_intent_contract(manifest, request, expansion, case_spec.data)
        intent_path = root / "request" / "intent_contract.json"
        if intent_path.is_file():
            intent = IntentContract.from_dict(read_json(intent_path)).to_dict()
            existing_identity = self._intent_recovery_identity(intent)
            projected_identity = self._intent_recovery_identity(projected_intent)
            if existing_identity != projected_identity:
                raise JobStoreError("immutable Intent Contract differs from the recovered generation projection")
        else:
            intent = projected_intent
            intent_path = self.store.write_intent_contract(intent)
        intent_digest = stable_digest(intent)
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
        elif "model_generation" in routes and not os.environ.get(MESHY_API_KEY_ENV, "").strip():
            blocker = ("provider_credentials_missing", f"configure {MESHY_API_KEY_ENV}")
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
        attempt.update({"status": "awaiting_semantic_review", "updated_at": utc_now()})
        self.store.write_attempt(attempt)
        self._checkpoint(manifest, "quality_gate", "completed", stable_digest(report), [str(run_dir / "quality_report.json")])
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
        contract = {
            "schema_version": INTENT_CONTRACT_SCHEMA_VERSION,
            "job_id": manifest["job_id"],
            "request_digest": manifest["request_digest"],
            "source": "expansion_case_spec_projection_v1",
            "original_request": {"text": request.get("text") or "", "case_id": request.get("case_id")},
            "input_identities": input_identities,
            "hard_requirements": hard,
            "soft_preferences": assumptions,
            "prohibitions": [],
            "ambiguities": ambiguities,
            "asset_policy": copy.deepcopy(case_spec.get("asset_policy") or {}),
            "execution": execution,
            "authorizations": copy.deepcopy(manifest["authorizations"]),
            "verification": {"assertions": copy.deepcopy(assertions or []), "frozen": True},
            "allowed_adjustments": {
                "paths": ["$.scene.duration_s", "$.observation_requirements", "$.scene.camera"],
                "ranges": {},
            },
            "frozen_digests": {
                "original_request": stable_digest({"text": request.get("text") or "", "inputs": input_identities}),
                "verification_assertions": stable_digest(assertions or []),
                "backend_constraints": stable_digest(case_spec.get("backend_constraints") or {}),
                "asset_policy": stable_digest(case_spec.get("asset_policy") or {}),
            },
            "created_at": utc_now(),
        }
        return IntentContract.from_dict(contract).to_dict()

    def _create_revision(
        self,
        manifest: dict[str, Any],
        raw_case_spec: Mapping[str, Any],
        reason: str,
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
            raise JobStoreError("revision cannot change frozen backend constraints in M2")
        if stable_digest(case_spec.data.get("asset_policy") or {}) != frozen["asset_policy"]:
            raise JobStoreError("revision cannot change the frozen asset policy")
        parent_id = manifest["current_attempt_id"]
        parent_spec = read_json(self.store.attempt_dir(manifest["job_id"], parent_id) / "case_spec.json")
        changes = self._json_diff(parent_spec, case_spec.data)
        allowed = intent["allowed_adjustments"]
        allowed_paths = [str(path) for path in allowed.get("paths") or [] if str(path).startswith("$.")]
        disallowed = [
            str(change.get("path") or "")
            for change in changes
            if not any(
                str(change.get("path") or "") == path
                or str(change.get("path") or "").startswith(f"{path}.")
                for path in allowed_paths
            )
        ]
        if disallowed:
            raise JobStoreError(f"revision changes paths outside Intent Contract allowed_adjustments: {disallowed}")
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
        write_json(root / "revision_reason.json", {"reason": reason, "trigger": "user_or_agent_approved_source_revision"})
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
        base = IntentContract.from_dict(read_json(root / "intent_contract.json")).to_dict()
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
        if elapsed >= soft and manifest["current_stage"] in {"compile", "smoke", "candidate"}:
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
        return AssetRegistry(self.store.workspace / "catalog" / "assets" / "catalog.sqlite")

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

    @staticmethod
    def _intent_recovery_identity(contract: Mapping[str, Any]) -> dict[str, Any]:
        identity = copy.deepcopy(dict(contract))
        identity.pop("created_at", None)
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
                    changes.append({"path": child, "operation": "add", "after": after[key]})
                elif key not in after:
                    changes.append({"path": child, "operation": "remove", "before": before[key]})
                else:
                    changes.extend(cls._json_diff(before[key], after[key], child))
            return changes
        return [{"path": path, "operation": "replace", "before": before, "after": after}]

    @staticmethod
    def _smoke_mode(attempt_dir: Path) -> str:
        del attempt_dir
        # The current executor runs the complete runtime plan. A future
        # observation-only replay may return "targeted" only after it writes a
        # parent-run/cache receipt proving that the solver was not rerun.
        return "executed"
