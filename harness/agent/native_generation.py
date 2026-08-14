from __future__ import annotations

import copy
import math
import re
import secrets
from typing import Any, Mapping

from harness.agent.job_schema import stable_digest, utc_now, validate_job_id
from harness.planning.case_generation import case_spec_generation_contract


NATIVE_GENERATION_CONTEXT_SCHEMA_VERSION = "harness_native_generation_context_v1"
NATIVE_GENERATION_SUBMISSION_SCHEMA_VERSION = "harness_native_generation_submission_v1"
NATIVE_GENERATION_ACK_SCHEMA_VERSION = "harness_native_generation_ack_v1"
NATIVE_GENERATION_CONTEXT_IDENTITY_SCHEMA_VERSION = "harness_native_generation_context_identity_v1"
GENERATION_POLICY_SCHEMA_VERSION = "harness_generation_policy_v1"
GENERATION_MODES = {"native", "legacy", "seed"}
SUPPORTED_CASE_SPEC_CONTRACT_SCHEMA_VERSIONS = {"harness_case_spec_v2"}


class NativeGenerationValidationError(ValueError):
    """Stable native-generation validation failure for Controller decisions."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def generation_policy(mode: str) -> dict[str, Any]:
    value = str(mode).strip()
    if value not in GENERATION_MODES:
        raise ValueError(f"generation mode must be one of {sorted(GENERATION_MODES)}")
    return {
        "schema_version": GENERATION_POLICY_SCHEMA_VERSION,
        "mode": value,
        "created_at": utc_now(),
    }


def validate_generation_policy(raw: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(raw)
    if set(value) != {"schema_version", "mode", "created_at"}:
        raise ValueError("generation policy fields mismatch")
    if value["schema_version"] != GENERATION_POLICY_SCHEMA_VERSION:
        raise ValueError("unsupported generation policy schema")
    if value["mode"] not in GENERATION_MODES:
        raise ValueError("unsupported generation mode")
    if not isinstance(value["created_at"], str) or not value["created_at"].strip():
        raise ValueError("generation policy created_at must be non-empty")
    return value


def build_native_generation_context(
    *,
    job_id: str,
    request_digest: str,
    request: Mapping[str, Any],
    target: Mapping[str, Any],
    authorizations: Mapping[str, Any],
) -> dict[str, Any]:
    validate_job_id(job_id)
    return {
        "schema_version": NATIVE_GENERATION_CONTEXT_SCHEMA_VERSION,
        "job_id": job_id,
        "request_digest": request_digest,
        "submission_nonce": secrets.token_hex(16),
        "request": copy.deepcopy(dict(request)),
        "target": copy.deepcopy(dict(target)),
        "authorizations": copy.deepcopy(dict(authorizations)),
        "intent_draft_contract": {
            "required_fields": [
                "hard_requirements",
                "soft_preferences",
                "prohibitions",
                "ambiguities",
                "parameter_analysis",
            ],
            "requirement_shape": {"id": "unique non-empty string", "text": "non-empty string"},
            "ambiguity_shape": {"question": "non-empty string"},
            "parameter_analysis_shape": {
                "path": "canonical CaseSpec object leaf path",
                "requirement_level": "hard, soft, or inferred",
                "reason": "non-empty string",
                "constraint": "null for hard; bounded numeric, list, or enum constraint otherwise",
            },
        },
        "submission_contract": {
            "schema_version": NATIVE_GENERATION_SUBMISSION_SCHEMA_VERSION,
            "required_fields": [
                "schema_version",
                "job_id",
                "generation_context_digest",
                "intent_draft",
                "case_spec",
                "agent_reported",
            ],
        },
        "case_spec_contract": case_spec_generation_contract(),
        "agent_reporting_contract": {
            "trust": "agent_reported_not_controller_observed",
            "fields": [
                "thread_id",
                "model",
                "model_provider",
                "model_turn_count",
                "image_input_ids_used",
            ],
            "controller_observed_invocation_count": 0,
        },
        "created_at": utc_now(),
    }


def validate_native_generation_context(raw: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = copy.deepcopy(dict(raw))
    except (TypeError, ValueError) as exc:
        raise NativeGenerationValidationError(
            "native_generation_context_invalid",
            "native generation context must be an object",
        ) from exc
    expected = {
        "schema_version",
        "job_id",
        "request_digest",
        "submission_nonce",
        "request",
        "target",
        "authorizations",
        "intent_draft_contract",
        "submission_contract",
        "case_spec_contract",
        "agent_reporting_contract",
        "created_at",
    }
    if set(value) != expected:
        raise NativeGenerationValidationError("native_generation_context_invalid", "native generation context fields mismatch")
    if value["schema_version"] != NATIVE_GENERATION_CONTEXT_SCHEMA_VERSION:
        raise NativeGenerationValidationError(
            "native_generation_context_schema_unsupported",
            "unsupported native generation context schema",
        )
    try:
        validate_job_id(value["job_id"])
    except ValueError as exc:
        raise NativeGenerationValidationError("native_generation_context_invalid", str(exc)) from exc
    if not re.fullmatch(r"[0-9a-f]{64}", str(value["request_digest"])):
        raise NativeGenerationValidationError("native_generation_context_invalid", "native generation request_digest must be SHA-256")
    if not re.fullmatch(r"[0-9a-f]{32}", str(value["submission_nonce"])):
        raise NativeGenerationValidationError("native_generation_context_invalid", "native generation submission_nonce is invalid")
    for field in ("request", "target", "authorizations", "intent_draft_contract", "submission_contract", "case_spec_contract", "agent_reporting_contract"):
        if not isinstance(value[field], Mapping):
            raise NativeGenerationValidationError("native_generation_context_invalid", f"native generation context {field} must be an object")
    case_spec_contract_version = value["case_spec_contract"].get("schema_version")
    if case_spec_contract_version not in SUPPORTED_CASE_SPEC_CONTRACT_SCHEMA_VERSIONS:
        raise NativeGenerationValidationError(
            "native_generation_case_spec_contract_schema_unsupported",
            "native generation context references an unsupported CaseSpec contract schema",
        )
    if value["submission_contract"] != {
        "schema_version": NATIVE_GENERATION_SUBMISSION_SCHEMA_VERSION,
        "required_fields": [
            "schema_version",
            "job_id",
            "generation_context_digest",
            "intent_draft",
            "case_spec",
            "agent_reported",
        ],
    }:
        raise NativeGenerationValidationError("native_generation_context_invalid", "native generation submission contract is invalid")
    if value["agent_reporting_contract"].get("trust") != "agent_reported_not_controller_observed" or value[
        "agent_reporting_contract"
    ].get("controller_observed_invocation_count") != 0:
        raise NativeGenerationValidationError("native_generation_context_invalid", "native generation agent reporting trust boundary is invalid")
    return value


def build_native_generation_context_identity(context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": NATIVE_GENERATION_CONTEXT_IDENTITY_SCHEMA_VERSION,
        "job_id": context["job_id"],
        "request_digest": context["request_digest"],
        "context_schema_version": context["schema_version"],
        "context_digest": stable_digest(context),
        "submission_nonce": context["submission_nonce"],
        "created_at": context["created_at"],
    }


def validate_native_generation_context_identity(
    raw: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    value = copy.deepcopy(dict(raw))
    expected_fields = {
        "schema_version",
        "job_id",
        "request_digest",
        "context_schema_version",
        "context_digest",
        "submission_nonce",
        "created_at",
    }
    if set(value) != expected_fields:
        raise NativeGenerationValidationError(
            "native_generation_context_identity_invalid",
            "native generation context identity fields mismatch",
        )
    if value["schema_version"] != NATIVE_GENERATION_CONTEXT_IDENTITY_SCHEMA_VERSION:
        raise NativeGenerationValidationError(
            "native_generation_context_identity_schema_unsupported",
            "unsupported native generation context identity schema",
        )
    expected = build_native_generation_context_identity(context)
    if value != expected:
        raise NativeGenerationValidationError(
            "native_generation_context_identity_mismatch",
            "native generation context no longer matches its frozen identity",
        )
    return value


def validate_native_generation_submission(
    raw: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        value = copy.deepcopy(dict(raw))
    except (TypeError, ValueError) as exc:
        raise NativeGenerationValidationError(
            "native_generation_submission_schema_invalid",
            "native generation submission must be an object",
        ) from exc
    expected = {
        "schema_version",
        "job_id",
        "generation_context_digest",
        "intent_draft",
        "case_spec",
        "agent_reported",
    }
    if set(value) != expected:
        raise NativeGenerationValidationError(
            "native_generation_submission_schema_invalid",
            "native generation submission fields mismatch",
        )
    if value["schema_version"] != NATIVE_GENERATION_SUBMISSION_SCHEMA_VERSION:
        raise NativeGenerationValidationError(
            "native_generation_submission_schema_invalid",
            "unsupported native generation submission schema",
        )
    if value["job_id"] != context["job_id"]:
        raise NativeGenerationValidationError(
            "native_generation_submission_context_mismatch",
            "native generation submission job identity mismatch",
        )
    if value["generation_context_digest"] != stable_digest(context):
        raise NativeGenerationValidationError(
            "native_generation_submission_context_mismatch",
            "native generation context digest mismatch",
        )
    try:
        _validate_intent_draft(value["intent_draft"])
    except NativeGenerationValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise NativeGenerationValidationError("native_generation_intent_draft_invalid", str(exc)) from exc
    if not isinstance(value["case_spec"], Mapping):
        raise NativeGenerationValidationError(
            "native_generation_case_spec_invalid",
            "native generation case_spec must be an object",
        )
    try:
        _validate_agent_reported(value["agent_reported"], context=context)
    except NativeGenerationValidationError:
        raise
    except (TypeError, ValueError) as exc:
        raise NativeGenerationValidationError("native_generation_agent_report_invalid", str(exc)) from exc
    return value


def build_native_generation_ack(
    *,
    context: Mapping[str, Any],
    submission: Mapping[str, Any],
) -> dict[str, Any]:
    context_digest = stable_digest(context)
    submission_digest = stable_digest(submission)
    return {
        "schema_version": NATIVE_GENERATION_ACK_SCHEMA_VERSION,
        "job_id": context["job_id"],
        "generation_context_digest": context_digest,
        "submission_digest": submission_digest,
        "ack_identity": stable_digest(
            {
                "job_id": context["job_id"],
                "generation_context_digest": context_digest,
                "submission_digest": submission_digest,
            }
        ),
        "controller_observed": {
            "received_at": utc_now(),
            "schema_validated": True,
            "controller_model_invocations": 0,
        },
        "agent_reported": copy.deepcopy(dict(submission["agent_reported"])),
    }


def validate_native_generation_ack(
    raw: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
    submission: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        value = copy.deepcopy(dict(raw))
    except (TypeError, ValueError) as exc:
        raise NativeGenerationValidationError(
            "native_generation_ack_invalid",
            "native generation ack must be an object",
        ) from exc
    expected = {
        "schema_version",
        "job_id",
        "generation_context_digest",
        "submission_digest",
        "ack_identity",
        "controller_observed",
        "agent_reported",
    }
    if set(value) != expected or value["schema_version"] != NATIVE_GENERATION_ACK_SCHEMA_VERSION:
        raise NativeGenerationValidationError("native_generation_ack_invalid", "native generation ack is invalid")
    expected_value = build_native_generation_ack(context=context, submission=submission)
    expected_value["controller_observed"]["received_at"] = value.get("controller_observed", {}).get("received_at")
    if value != expected_value:
        raise NativeGenerationValidationError(
            "native_generation_ack_identity_mismatch",
            "native generation ack identity mismatch",
        )
    return value


def _validate_intent_draft(raw: Any) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError("native generation intent_draft must be an object")
    expected = {"hard_requirements", "soft_preferences", "prohibitions", "ambiguities", "parameter_analysis"}
    if set(raw) != expected:
        raise ValueError("native generation intent_draft fields mismatch")
    seen: set[str] = set()
    for field in ("hard_requirements", "soft_preferences", "prohibitions"):
        rows = raw[field]
        if not isinstance(rows, list):
            raise ValueError(f"intent_draft.{field} must be a list")
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != {"id", "text"}:
                raise ValueError(f"intent_draft.{field} entries must contain only id and text")
            identity = str(row.get("id") or "").strip()
            text = str(row.get("text") or "").strip()
            if not identity or not text or identity in seen or identity in {"original_user_request", "original_user_visual_inputs"}:
                raise ValueError("native Intent requirement identities must be unique, non-empty, and non-reserved")
            seen.add(identity)
    ambiguities = raw["ambiguities"]
    if not isinstance(ambiguities, list):
        raise ValueError("intent_draft.ambiguities must be a list")
    for row in ambiguities:
        if not isinstance(row, Mapping) or set(row) != {"question"} or not str(row.get("question") or "").strip():
            raise ValueError("native Intent ambiguities must contain only a non-empty question")
    try:
        analysis = raw["parameter_analysis"]
        if not isinstance(analysis, list):
            raise ValueError("intent_draft.parameter_analysis must be a list")
        paths: set[str] = set()
        for row in analysis:
            if not isinstance(row, Mapping) or set(row) != {"path", "requirement_level", "reason", "constraint"}:
                raise ValueError("native parameter analysis fields mismatch")
            path = str(row.get("path") or "")
            if not re.fullmatch(r"\$\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", path) or path in paths:
                raise ValueError("native parameter analysis paths must be unique canonical object paths")
            paths.add(path)
            level = row.get("requirement_level")
            if level not in {"hard", "soft", "inferred"} or not str(row.get("reason") or "").strip():
                raise ValueError("native parameter analysis level or reason is invalid")
            constraint = row.get("constraint")
            if level == "hard":
                if constraint is not None:
                    raise ValueError("hard native parameters cannot authorize an adjustment range")
            else:
                _validate_constraint(constraint)
    except (TypeError, ValueError) as exc:
        raise NativeGenerationValidationError("native_generation_parameter_constraint_invalid", str(exc)) from exc


def _validate_constraint(raw: Any) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError("soft or inferred native parameters require a bounded constraint")
    kind = raw.get("kind")
    if kind == "numeric":
        if set(raw) != {"kind", "min", "max"}:
            raise ValueError("native numeric constraint fields mismatch")
        minimum, maximum = raw["min"], raw["max"]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in (minimum, maximum)) or minimum > maximum:
            raise ValueError("native numeric constraint is invalid")
    elif kind == "list":
        if set(raw) != {"kind", "min_items", "max_items"}:
            raise ValueError("native list constraint fields mismatch")
        minimum, maximum = raw["min_items"], raw["max_items"]
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (minimum, maximum)) or minimum < 0 or minimum > maximum:
            raise ValueError("native list constraint is invalid")
    elif kind == "enum":
        values = raw.get("values")
        if set(raw) != {"kind", "values"} or not isinstance(values, list) or not values or any(isinstance(item, (dict, list)) for item in values):
            raise ValueError("native enum constraint is invalid")
    else:
        raise ValueError("native constraint kind is invalid")


def _validate_agent_reported(raw: Any, *, context: Mapping[str, Any]) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError("agent_reported must be an object")
    expected = {"thread_id", "model", "model_provider", "model_turn_count", "image_input_ids_used"}
    if set(raw) != expected:
        raise ValueError("agent_reported fields mismatch")
    for field in ("thread_id", "model", "model_provider"):
        if raw[field] is not None and (not isinstance(raw[field], str) or not raw[field].strip()):
            raise ValueError(f"agent_reported.{field} must be null or a non-empty string")
    turns = raw["model_turn_count"]
    if turns is not None and (not isinstance(turns, int) or isinstance(turns, bool) or turns < 0):
        raise ValueError("agent_reported.model_turn_count must be null or non-negative")
    used = raw["image_input_ids_used"]
    if not isinstance(used, list) or any(not isinstance(value, str) for value in used) or len(used) != len(set(used)):
        raise ValueError("agent_reported.image_input_ids_used must be a unique string list")
    request = context["request"]
    known = {str(row["input_id"]) for row in request.get("inputs") or [] if row.get("kind") == "image"}
    if not set(used).issubset(known):
        raise NativeGenerationValidationError(
            "native_generation_image_use_declaration_invalid",
            "agent_reported image usage references an unknown input",
        )
    requirement = request["planning_image_requirement"]
    if requirement["mode"] == "required":
        if context["authorizations"].get("planning_llm_upload") is not True:
            raise NativeGenerationValidationError(
                "native_generation_image_use_declaration_invalid",
                "required native planning image use is not authorized",
            )
        if set(used) != set(requirement["input_ids"]):
            raise NativeGenerationValidationError(
                "native_generation_image_use_declaration_invalid",
                "required native planning images must be reported as used",
            )
    elif used:
        raise NativeGenerationValidationError(
            "native_generation_image_use_declaration_invalid",
            "optional planning images must remain metadata-only",
        )
