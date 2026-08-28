from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from harness.core.artifact_schema import write_json


STAGE_RESULT_SCHEMA_VERSION = "harness_stage_result_v1"
STAGE_RESULT_STATUSES = {"completed", "failed", "blocked", "interrupted"}
FAILURE_CLASSES = {
    "agent_submission_invalid",
    "artifact_incomplete",
    "awaiting_agent_action",
    "blocked_configuration",
    "blocked_user_action",
    "capability_missing",
    "case_spec_invalid",
    "execution_failed",
    "harness_bug",
    "interrupted",
    "provider_failed",
    "quality_gate_failed",
    "render_sync_failed",
    "semantic_review_failed",
    "transient",
    "verification_failed",
}
ALLOWED_NEXT_ACTIONS = {
    "cancel",
    "continue",
    "fix_configuration",
    "inspect_artifacts",
    "open_development_issue",
    "request_user_action",
    "resume_checkpoint",
    "retry_stage",
    "revise_case_spec",
    "run_semantic_review",
    "submit_native_generation",
}
_COST_FIELDS = {"amount", "currency", "estimated", "provider"}
_REQUIRED_FIELDS = {
    "allowed_next_actions",
    "artifact_refs",
    "attempt_id",
    "checkpoint_ref",
    "cost",
    "elapsed_seconds",
    "failure_class",
    "failure_code",
    "failure_codes",
    "invocation_count",
    "job_id",
    "message",
    "request_identities",
    "required_user_action",
    "retryable",
    "schema_version",
    "stage",
    "status",
}

_CAPABILITY_CODES = {
    "backend_execution_unsupported",
    "multi_backend_handoff_contract_unavailable",
    "no_legal_backend",
    "stage_backend_unregistered",
    "stage_handoff_missing",
    "ue_handoff_unsupported",
    "unsupported_backend",
    "unsupported_provider_route",
    "unsupported_scene_backend",
    "unsupported_solver_capabilities",
}
_USER_ACTION_CODES = {
    "budget_exhausted",
    "case_spec_revision_budget_exhausted",
    "candidate_budget_reserve_insufficient",
    "degraded_preview_only",
    "external_provider_authorization_missing",
    "intent_ambiguity_requires_decision",
    "catalog_not_writable",
    "llm_credentials_missing",
    "meshy_upload_authorization_missing",
    "paid_provider_authorization_missing",
    "paid_provider_budget_missing",
    "paid_provider_budget_exhausted",
    "planning_image_upload_authorization_missing",
    "publication_tier_not_satisfied",
    "provider_credentials_missing",
    "provider_input_manifest_missing",
    "provider_input_missing",
    "provider_resume_checkpoint_missing",
    "provider_submission_state_unknown",
    "request_input_missing",
    "reviewer_invocation_budget_exhausted",
    "soft_deadline_reached",
    "semantic_reviewer_image_upload_authorization_missing",
    "ue_launch_budget_exhausted",
}
_CONFIGURATION_CODES = {
    "backend_importer_unavailable",
    "catalog_missing",
    "disk_budget_insufficient",
    "f1_uproject_invalid",
    "f1_uproject_missing",
    "f2_ue_executable_missing",
    "f3_ue_map_invalid",
    "f3_ue_map_missing",
    "f3_ue_map_package_missing",
    "f3_ue_map_unresolved",
    "f4_ue_actor_class_missing",
    "f5_asset_registry_missing",
    "f6_contact_export_disabled",
    "f7_ue_runner_cmd_missing",
    "llm_model_missing",
    "generation_cache_context_mismatch",
    "legacy_generation_cache_image_mode_unknown",
    "planning_image_input_unsupported",
    "planning_image_probe_already_consumed",
    "planning_image_probe_reservation_mismatch",
    "ue_executable_missing",
    "ue_project_missing",
    "request_digest_mismatch",
    "request_input_identity_mismatch",
    "reviewer_app_server_unavailable",
    "reviewer_permission_profile_forbidden",
    "reviewer_permission_profile_unsupported",
    "reviewer_isolation_unproven",
    "reviewer_unrelated_instruction_source",
    "native_generation_case_spec_contract_schema_unsupported",
    "native_generation_ack_identity_mismatch",
    "native_generation_ack_invalid",
    "native_generation_context_identity_invalid",
    "native_generation_context_identity_mismatch",
    "native_generation_context_identity_schema_unsupported",
    "native_generation_context_invalid",
    "native_generation_context_schema_unsupported",
}
_CASE_SPEC_CONTRACT_CODES = {
    "invalid_generation_spec",
    "unsupported_generation_recipe",
}
_NATIVE_SUBMISSION_INVALID_CODES = {
    "native_generation_agent_report_invalid",
    "native_generation_case_spec_invalid",
    "native_generation_image_use_declaration_invalid",
    "native_generation_intent_draft_invalid",
    "native_generation_parameter_constraint_invalid",
    "native_generation_submission_context_mismatch",
    "native_generation_submission_schema_invalid",
}
_HARNESS_RENDER_DEFECT_CODES = {
    "f_ue_lighting_report_missing",
    "f_ue_preview_shadow_indicator_active",
    "f_ue_runtime_light_mobility_invalid",
}
_HARNESS_RUNTIME_DEFECT_CODES = {
    "f_runtime_constraint_enforcement_failed",
    "f_runtime_constraint_state_invalid",
}
_TRANSIENT_CODES = {
    "backend_importer_timeout",
    "f7_ue_runner_exception",
    "f7_ue_runner_timeout",
    "llm_http_retriable",
    "llm_network_error",
    "provider_download_failed",
    "provider_http_error",
    "provider_network_error",
    "provider_task_timeout",
}
_ARTIFACT_CODES = {
    "asset_resolve_completion_unknown",
    "f7_runtime_artifact_incomplete",
    "f9_ue_output_missing",
    "stage_handoff_incomplete",
    "stage_handoff_schema_mismatch",
    "verifier_input_invalid",
    "evidence_artifact_identity_mismatch",
    "evidence_candidate_path_invalid",
    "evidence_technical_gate_missing",
    "evidence_technical_gate_incomplete",
    "evidence_technical_gate_failed",
    "evidence_trajectory_missing",
    "evidence_trajectory_empty",
    "evidence_trajectory_time_invalid",
    "evidence_canonical_view_missing",
    "reviewer_output_invalid_json",
    "reviewer_output_schema_invalid",
}
_SECRET_PATTERNS = (
    re.compile(r"(?i)([\"']?authorization[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)[^\"'\s,;}]+"),
    re.compile(r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|token|secret|password)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}]+"),
    re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|secret)=)[^&\s]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


@dataclass(frozen=True)
class StageResult:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> StageResult:
        data = dict(raw)
        if data.get("schema_version") == STAGE_RESULT_SCHEMA_VERSION and "failure_codes" not in data:
            data["failure_codes"] = [] if data.get("status") == "completed" else [data.get("failure_code")]
        missing = sorted(_REQUIRED_FIELDS - set(data))
        if missing:
            raise ValueError(f"stage result is missing required fields: {missing}")
        extra = sorted(set(data) - _REQUIRED_FIELDS)
        if extra:
            raise ValueError(f"stage result contains unsupported fields: {extra}")
        if data.get("schema_version") != STAGE_RESULT_SCHEMA_VERSION:
            raise ValueError(f"stage result schema_version must be {STAGE_RESULT_SCHEMA_VERSION}")
        for field in ("job_id", "attempt_id"):
            if data.get(field) is not None and (not isinstance(data[field], str) or not data[field].strip()):
                raise ValueError(f"{field} must be null or a non-empty string")
        if not isinstance(data.get("stage"), str):
            raise ValueError("stage must be a string")
        stage = data["stage"].strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", stage):
            raise ValueError("stage must use lowercase letters, digits, and underscores")
        if not isinstance(data.get("status"), str):
            raise ValueError("status must be a string")
        status = data["status"]
        if status not in STAGE_RESULT_STATUSES:
            raise ValueError(f"invalid stage result status: {status}")
        elapsed = data.get("elapsed_seconds")
        if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or float(elapsed) < 0.0:
            raise ValueError("elapsed_seconds must be a non-negative number")
        invocation_count = data.get("invocation_count")
        if not isinstance(invocation_count, int) or isinstance(invocation_count, bool) or invocation_count < 0:
            raise ValueError("invocation_count must be a non-negative integer")
        cost = data.get("cost")
        if not isinstance(cost, Mapping) or set(cost) - _COST_FIELDS:
            raise ValueError("cost must be an object")
        if "amount" in cost and (
            not isinstance(cost["amount"], (int, float))
            or isinstance(cost["amount"], bool)
            or float(cost["amount"]) < 0.0
        ):
            raise ValueError("cost.amount must be a non-negative number")
        for field in ("currency", "provider"):
            if field in cost and (not isinstance(cost[field], str) or not cost[field].strip()):
                raise ValueError(f"cost.{field} must be a non-empty string")
        if "estimated" in cost and not isinstance(cost["estimated"], bool):
            raise ValueError("cost.estimated must be boolean")
        request_identities = data.get("request_identities")
        if not isinstance(request_identities, list) or any(
            not isinstance(value, str) or not _is_sha256(value) for value in request_identities
        ):
            raise ValueError("request_identities must be a list of SHA-256 digests")
        checkpoint_ref = data.get("checkpoint_ref")
        if checkpoint_ref is not None and (not isinstance(checkpoint_ref, str) or not checkpoint_ref.strip()):
            raise ValueError("checkpoint_ref must be null or a non-empty string")
        artifact_refs = data.get("artifact_refs")
        if not isinstance(artifact_refs, list) or any(not _valid_artifact_ref(value) for value in artifact_refs):
            raise ValueError("artifact_refs must be a list of named path objects")
        actions = data.get("allowed_next_actions")
        if not isinstance(actions, list) or not actions or any(action not in ALLOWED_NEXT_ACTIONS for action in actions):
            raise ValueError("allowed_next_actions contains an unsupported action")
        required_user_action = data.get("required_user_action")
        if required_user_action is not None and not _valid_user_action(required_user_action):
            raise ValueError("required_user_action must be null or contain non-empty code and message")
        if status == "completed":
            if any(data.get(field) is not None for field in ("failure_class", "failure_code", "message")):
                raise ValueError("completed stage results cannot contain failure fields")
            if data.get("retryable") is not False:
                raise ValueError("completed stage results must set retryable=false")
            if required_user_action is not None:
                raise ValueError("completed stage results cannot require user action")
            if data.get("failure_codes") != []:
                raise ValueError("completed stage results must have an empty failure_codes list")
        else:
            if not isinstance(data.get("failure_class"), str):
                raise ValueError("failure_class must be a string")
            failure_class = data["failure_class"]
            if failure_class not in FAILURE_CLASSES:
                raise ValueError(f"invalid failure_class: {failure_class}")
            if not isinstance(data.get("failure_code"), str) or not data["failure_code"].strip():
                raise ValueError("non-completed stage results require failure_code")
            failure_codes = data.get("failure_codes")
            if (
                not isinstance(failure_codes, list)
                or not failure_codes
                or any(not isinstance(value, str) or not value.strip() for value in failure_codes)
                or len(failure_codes) != len(set(failure_codes))
                or data["failure_code"] not in failure_codes
            ):
                raise ValueError("failure_codes must be unique non-empty strings containing failure_code")
            if not isinstance(data.get("message"), str) or not data["message"].strip():
                raise ValueError("non-completed stage results require message")
            if not isinstance(data.get("retryable"), bool):
                raise ValueError("retryable must be boolean")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


def build_stage_result(
    *,
    stage: str,
    status: str,
    job_id: str | None = None,
    attempt_id: str | None = None,
    failure_class: str | None = None,
    failure_code: str | None = None,
    failure_codes: Sequence[str] | None = None,
    message: str | None = None,
    retryable: bool = False,
    checkpoint_ref: str | None = None,
    artifact_refs: Sequence[Mapping[str, Any]] = (),
    cost: Mapping[str, Any] | None = None,
    elapsed_seconds: float = 0.0,
    invocation_count: int = 1,
    request_identities: Sequence[str] = (),
    allowed_next_actions: Sequence[str] | None = None,
    required_user_action: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    safe_message = redact_text(message) if message is not None else None
    safe_user_action = None
    if required_user_action is not None:
        safe_user_action = {
            "code": required_user_action.get("code"),
            "message": (
                redact_text(required_user_action.get("message"))
                if isinstance(required_user_action.get("message"), str)
                else required_user_action.get("message")
            ),
        }
    payload = {
        "schema_version": STAGE_RESULT_SCHEMA_VERSION,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "stage": stage,
        "status": status,
        "failure_class": failure_class,
        "failure_code": failure_code,
        "failure_codes": list(dict.fromkeys(failure_codes or ([failure_code] if failure_code else []))),
        "message": safe_message,
        "retryable": bool(retryable),
        "checkpoint_ref": redact_text(str(checkpoint_ref)) if checkpoint_ref is not None else None,
        "artifact_refs": [_sanitize_artifact_ref(value) for value in artifact_refs],
        "cost": dict(cost or {}),
        "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 6),
        "invocation_count": int(invocation_count),
        "request_identities": sorted({str(value) for value in request_identities if str(value).strip()}),
        "allowed_next_actions": list(allowed_next_actions or (["continue"] if status == "completed" else ["inspect_artifacts"])),
        "required_user_action": safe_user_action,
    }
    return StageResult.from_dict(payload).to_dict()


def failure_stage_result(
    *,
    stage: str,
    failure_code: str,
    message: str,
    retryable: bool | None = None,
    source_status: str | None = None,
    required_action_message: str | None = None,
    failure_codes: Sequence[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    classification = classify_failure(stage, failure_code, retryable=retryable, source_status=source_status)
    required_user_action = None
    if (
        classification["failure_class"] in {"blocked_configuration", "blocked_user_action"}
        or "request_user_action" in classification["allowed_next_actions"]
    ):
        required_user_action = {
            "code": failure_code,
            "message": required_action_message or message,
        }
    return build_stage_result(
        stage=stage,
        status=classification["status"],
        failure_class=classification["failure_class"],
        failure_code=failure_code,
        failure_codes=failure_codes,
        message=message,
        retryable=classification["retryable"],
        allowed_next_actions=classification["allowed_next_actions"],
        required_user_action=required_user_action,
        **kwargs,
    )


def classify_failure(
    stage: str,
    failure_code: str,
    *,
    retryable: bool | None = None,
    source_status: str | None = None,
) -> dict[str, Any]:
    """Map only stable stage/code/status fields; never inspect a human message."""
    code = str(failure_code or "unknown_failure").strip().casefold()
    normalized_stage = str(stage or "").strip().casefold()
    if source_status == "interrupted" or code in {"interrupted", "keyboardinterrupt"}:
        return _classification("interrupted", "interrupted", False, ["resume_checkpoint", "cancel"])
    if code == "native_generation_submission_required":
        return _classification("blocked", "awaiting_agent_action", False, ["submit_native_generation", "cancel"])
    if code in _NATIVE_SUBMISSION_INVALID_CODES:
        return _classification("blocked", "agent_submission_invalid", False, ["submit_native_generation", "cancel"])
    if code == "case_spec_revision_budget_exhausted":
        return _classification("blocked", "blocked_user_action", False, ["inspect_artifacts", "cancel"])
    if code in _CAPABILITY_CODES or "capability" in code or "handoff_contract_unavailable" in code:
        return _classification("blocked", "capability_missing", False, ["open_development_issue", "cancel"])
    if code in _USER_ACTION_CODES or code.startswith("provider_input_") or "authorization" in code or "credits" in code:
        return _classification("blocked", "blocked_user_action", False, ["request_user_action", "cancel"])
    if code in _CONFIGURATION_CODES or normalized_stage in {"readiness", "preflight"}:
        return _classification("blocked", "blocked_configuration", False, ["fix_configuration", "cancel"])
    if code in _CASE_SPEC_CONTRACT_CODES:
        return _classification("failed", "case_spec_invalid", False, ["revise_case_spec", "inspect_artifacts"])
    if retryable is True or (retryable is None and code in _TRANSIENT_CODES):
        actions = ["resume_checkpoint", "retry_stage"] if "provider" in normalized_stage else ["retry_stage"]
        return _classification("failed", "transient", True, actions)
    if code in _HARNESS_RENDER_DEFECT_CODES or code in _HARNESS_RUNTIME_DEFECT_CODES:
        return _classification("failed", "harness_bug", False, ["inspect_artifacts", "open_development_issue"])
    if "solver_provenance" in code or "physics_capture" in code:
        return _classification("failed", "execution_failed", False, ["inspect_artifacts", "open_development_issue"])
    if code.endswith("_exception") or code.endswith("_unhandled_exception"):
        return _classification("failed", "harness_bug", False, ["inspect_artifacts", "open_development_issue"])
    if code in _ARTIFACT_CODES or "artifact_missing" in code or "output_missing" in code:
        return _classification("failed", "artifact_incomplete", False, ["inspect_artifacts", "open_development_issue"])
    if normalized_stage == "generation" or normalized_stage == "compile":
        return _classification("failed", "case_spec_invalid", False, ["revise_case_spec", "inspect_artifacts"])
    if normalized_stage == "provider":
        return _classification(
            "blocked" if source_status == "blocked" else "failed",
            "provider_failed",
            False,
            ["request_user_action", "cancel"] if source_status == "blocked" else ["inspect_artifacts", "open_development_issue"],
        )
    if normalized_stage == "verifier":
        return _classification("failed", "verification_failed", False, ["inspect_artifacts", "revise_case_spec"])
    if normalized_stage == "render_sync":
        return _classification("failed", "render_sync_failed", False, ["inspect_artifacts", "retry_stage"])
    if normalized_stage == "quality_gate":
        return _classification("failed", "quality_gate_failed", False, ["inspect_artifacts", "revise_case_spec"])
    if normalized_stage == "evidence_bundle":
        return _classification("failed", "artifact_incomplete", False, ["inspect_artifacts", "open_development_issue"])
    if normalized_stage == "semantic_review":
        return _classification("failed", "semantic_review_failed", False, ["inspect_artifacts", "revise_case_spec"])
    if normalized_stage in {"execute", "execution"}:
        return _classification("failed", "execution_failed", False, ["inspect_artifacts", "open_development_issue"])
    return _classification("failed", "harness_bug", False, ["inspect_artifacts", "open_development_issue"])


def select_primary_failure(stage: str, failures: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Select a deterministic primary failure while retaining every stable code."""
    normalized: list[dict[str, Any]] = []
    for index, failure in enumerate(failures):
        code = failure.get("code")
        if not isinstance(code, str) or not code.strip():
            continue
        retryable = failure.get("retryable")
        if retryable is None:
            retryable = failure.get("retriable")
        retryable = retryable if isinstance(retryable, bool) else None
        source_status = failure.get("source_status")
        classification = classify_failure(
            stage,
            code,
            retryable=retryable,
            source_status=str(source_status) if source_status is not None else None,
        )
        normalized.append(
            {
                "code": code,
                "message": str(failure.get("message") or code),
                "retryable": retryable,
                "source_status": source_status,
                "classification": classification,
                "index": index,
            }
        )
    if not normalized:
        return {
            "code": f"{stage}_failed",
            "message": f"{stage} failed.",
            "retryable": None,
            "source_status": None,
            "failure_codes": [f"{stage}_failed"],
        }
    primary = min(normalized, key=lambda value: (_failure_priority(value["classification"]), value["index"]))
    return {
        "code": primary["code"],
        "message": primary["message"],
        "retryable": primary["retryable"],
        "source_status": primary["source_status"],
        "failure_codes": list(dict.fromkeys(value["code"] for value in normalized)),
    }


def stage_result_from_compilation_report(
    report: Mapping[str, Any],
    *,
    job_id: str | None = None,
    attempt_id: str | None = None,
    elapsed_seconds: float = 0.0,
) -> dict[str, Any]:
    refs = [artifact_ref("runtime_compilation_report", "runtime_compilation_report.json", report.get("schema_version"))]
    if report.get("status") == "pass":
        return build_stage_result(
            stage="compile",
            status="completed",
            job_id=job_id,
            attempt_id=attempt_id,
            artifact_refs=refs,
            elapsed_seconds=elapsed_seconds,
        )
    failures = [value for value in report.get("errors") or [] if isinstance(value, Mapping)]
    primary = select_primary_failure("compile", failures)
    return failure_stage_result(
        stage="compile",
        failure_code=primary["code"],
        failure_codes=primary["failure_codes"],
        message=primary["message"],
        job_id=job_id,
        attempt_id=attempt_id,
        artifact_refs=refs,
        elapsed_seconds=elapsed_seconds,
    )


def stage_result_from_provider_batch(
    batch: Mapping[str, Any],
    *,
    job_id: str | None = None,
    attempt_id: str | None = None,
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    requests = [value for value in batch.get("requests") or [] if isinstance(value, Mapping)]
    results = [value for value in batch.get("results") or [] if isinstance(value, Mapping)]
    identities = [str(value.get("request_digest") or "") for value in requests]
    refs = [artifact_ref("asset_provider_batch", "asset_provider_batch.json", batch.get("schema_version"))]
    elapsed = float(batch.get("elapsed_seconds") or 0.0) if elapsed_seconds is None else elapsed_seconds
    failed = [value for value in results if value.get("status") != "fulfilled"]
    if not failed:
        return build_stage_result(
            stage="provider",
            status="completed",
            job_id=job_id,
            attempt_id=attempt_id,
            artifact_refs=refs,
            elapsed_seconds=elapsed,
            invocation_count=len(requests),
            request_identities=identities,
        )
    failures = []
    receipt_ids = []
    for result in failed:
        failure = result.get("failure") if isinstance(result.get("failure"), Mapping) else {}
        failures.append(
            {
                "code": failure.get("code") or "provider_execution_failed",
                "message": failure.get("message") or "Provider execution failed.",
                "retryable": failure.get("retriable"),
                "source_status": result.get("status") or "failed",
            }
        )
        receipt_ids.extend(str(value) for value in result.get("receipt_ids") or [] if str(value).strip())
    primary = select_primary_failure("provider", failures)
    return failure_stage_result(
        stage="provider",
        failure_code=primary["code"],
        failure_codes=primary["failure_codes"],
        message=primary["message"],
        retryable=primary["retryable"],
        source_status=primary["source_status"],
        job_id=job_id,
        attempt_id=attempt_id,
        artifact_refs=refs,
        elapsed_seconds=elapsed,
        invocation_count=len(requests),
        request_identities=identities,
        checkpoint_ref=(f"provider_receipts/{receipt_ids[0]}.json" if receipt_ids else None),
    )


def stage_result_from_preflight_report(
    report: Mapping[str, Any],
    *,
    job_id: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    refs = [artifact_ref("ue_preflight_report", "ue_preflight_report.json", report.get("schema_version"))]
    code = report.get("failure_code")
    if not code:
        return build_stage_result(
            stage="preflight",
            status="completed",
            job_id=job_id,
            attempt_id=attempt_id,
            artifact_refs=refs,
        )
    return failure_stage_result(
        stage="preflight",
        failure_code=str(code),
        message=str(report.get("failure_message") or "Backend preflight failed."),
        required_action_message=str(report.get("next_required_action") or report.get("failure_message") or "Fix backend configuration."),
        job_id=job_id,
        attempt_id=attempt_id,
        artifact_refs=refs,
    )


def stage_result_from_execution_report(
    report: Mapping[str, Any],
    *,
    job_id: str | None = None,
    attempt_id: str | None = None,
    elapsed_seconds: float = 0.0,
) -> dict[str, Any]:
    refs = [artifact_ref("stage_execution_report", "stage_execution_report.json", report.get("schema_version"))]
    invocation_count = len(report.get("completed_stages") or []) + (0 if report.get("status") == "completed" else 1)
    if report.get("status") == "completed":
        return build_stage_result(
            stage="execute",
            status="completed",
            job_id=job_id,
            attempt_id=attempt_id,
            artifact_refs=refs,
            elapsed_seconds=elapsed_seconds,
            invocation_count=invocation_count,
        )
    return failure_stage_result(
        stage="execute",
        failure_code=str(report.get("failure_code") or "stage_execution_failed"),
        message=str(report.get("failure_message") or "Runtime stage execution failed."),
        source_status=str(report.get("status") or "failed"),
        job_id=job_id,
        attempt_id=attempt_id,
        artifact_refs=refs,
        elapsed_seconds=elapsed_seconds,
        invocation_count=invocation_count,
    )


def stage_result_from_verifier_report(
    report: Mapping[str, Any],
    *,
    job_id: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    refs = [artifact_ref("harness_verifier", "harness_verifier.json", report.get("schema_version"))]
    if report.get("status") == "pass":
        return build_stage_result(stage="verifier", status="completed", job_id=job_id, attempt_id=attempt_id, artifact_refs=refs)
    first = report.get("first_failure") if isinstance(report.get("first_failure"), Mapping) else {}
    return failure_stage_result(
        stage="verifier",
        failure_code=str(report.get("failure_type") or "physics_verification_failed"),
        message=str(first.get("value") or "Physics verifier failed."),
        job_id=job_id,
        attempt_id=attempt_id,
        artifact_refs=refs,
    )


def stage_result_from_render_sync_report(
    report: Mapping[str, Any],
    *,
    job_id: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    refs = [artifact_ref("render_sync_report", "render_sync_report.json", report.get("schema_version"))]
    if report.get("status") == "pass":
        return build_stage_result(stage="render_sync", status="completed", job_id=job_id, attempt_id=attempt_id, artifact_refs=refs)
    failures = [value for value in report.get("failures") or [] if isinstance(value, Mapping)]
    if not failures:
        failures = [{"code": value, "message": "Render synchronization failed."} for value in report.get("failure_codes") or []]
    primary = select_primary_failure("render_sync", failures)
    return failure_stage_result(
        stage="render_sync",
        failure_code=primary["code"],
        failure_codes=primary["failure_codes"],
        message=primary["message"],
        job_id=job_id,
        attempt_id=attempt_id,
        artifact_refs=refs,
    )


def stage_result_from_quality_report(
    report: Mapping[str, Any],
    *,
    job_id: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    refs = [artifact_ref("quality_report", "quality_report.json", report.get("schema_version"))]
    if report.get("status") == "pass":
        return build_stage_result(stage="quality_gate", status="completed", job_id=job_id, attempt_id=attempt_id, artifact_refs=refs)
    hard_gate = report.get("hard_gate") if isinstance(report.get("hard_gate"), Mapping) else {}
    failures = [value for value in hard_gate.get("failures") or [] if isinstance(value, Mapping)]
    primary = select_primary_failure("quality_gate", failures)
    return failure_stage_result(
        stage="quality_gate",
        failure_code=primary["code"],
        failure_codes=primary["failure_codes"],
        message=primary["message"],
        job_id=job_id,
        attempt_id=attempt_id,
        artifact_refs=refs,
    )


def artifact_ref(name: str, path: str | Path, schema_version: Any = None) -> dict[str, Any]:
    result = {"name": str(name), "path": str(path)}
    if schema_version:
        result["schema_version"] = str(schema_version)
    return result


def write_stage_result(root: str | Path, result: Mapping[str, Any]) -> Path:
    sanitized = _redact_value(result)
    validated = StageResult.from_dict(sanitized).to_dict()
    destination = Path(root) / "stage_results" / f"{validated['stage']}.json"
    write_json(destination, validated)
    return destination


def redact_text(value: str | None) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("\\bsk-"):
            text = pattern.sub("[REDACTED]", text)
        else:
            text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    return text[:4000]


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    return value


def _classification(status: str, failure_class: str, retryable: bool, actions: list[str]) -> dict[str, Any]:
    return {
        "status": status,
        "failure_class": failure_class,
        "retryable": retryable,
        "allowed_next_actions": actions,
    }


def _failure_priority(classification: Mapping[str, Any]) -> int:
    return {
        "blocked_user_action": 0,
        "awaiting_agent_action": 0,
        "agent_submission_invalid": 0,
        "blocked_configuration": 1,
        "capability_missing": 2,
        "interrupted": 3,
        "execution_failed": 10,
        "harness_bug": 11,
        "artifact_incomplete": 20,
        "verification_failed": 30,
        "transient": 40,
        "provider_failed": 50,
        "render_sync_failed": 60,
        "case_spec_invalid": 70,
        "quality_gate_failed": 80,
        "semantic_review_failed": 90,
    }.get(str(classification.get("failure_class") or ""), 100)


def _sanitize_artifact_ref(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "name": redact_text(value.get("name")) if isinstance(value.get("name"), str) else value.get("name"),
        "path": redact_text(value.get("path")) if isinstance(value.get("path"), str) else value.get("path"),
    }
    if value.get("schema_version"):
        result["schema_version"] = str(value["schema_version"])
    if value.get("sha256"):
        result["sha256"] = str(value["sha256"])
    return result


def _valid_artifact_ref(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if set(value) - {"name", "path", "schema_version", "sha256"}:
        return False
    if (
        not isinstance(value.get("name"), str)
        or not value["name"].strip()
        or not isinstance(value.get("path"), str)
        or not value["path"].strip()
    ):
        return False
    digest = value.get("sha256")
    schema_version = value.get("schema_version")
    if schema_version is not None and (not isinstance(schema_version, str) or not schema_version.strip()):
        return False
    if digest is None:
        return True
    return isinstance(digest, str) and _is_sha256(digest)


def _valid_user_action(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"code", "message"}
        and isinstance(value.get("code"), str)
        and bool(value["code"].strip())
        and isinstance(value.get("message"), str)
        and bool(value["message"].strip())
    )


def _is_sha256(value: str) -> bool:
    normalized = value.strip().casefold()
    return len(normalized) == 64 and all(character in "0123456789abcdef" for character in normalized)
