from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


JOB_MANIFEST_SCHEMA_VERSION = "harness_agent_job_manifest_v1"
LEGACY_INTENT_CONTRACT_SCHEMA_VERSION = "harness_intent_contract_v1"
PROJECTED_INTENT_CONTRACT_SCHEMA_VERSION = "harness_intent_contract_v2"
INTENT_CONTRACT_SCHEMA_VERSION = "harness_intent_contract_v3"
ATTEMPT_MANIFEST_SCHEMA_VERSION = "harness_agent_attempt_manifest_v1"
CHECKPOINT_SCHEMA_VERSION = "harness_agent_checkpoint_v1"
SMOKE_GATE_SCHEMA_VERSION = "harness_smoke_gate_v1"
JOB_STATES = {
    "created",
    "running",
    "blocked",
    "needs_user_decision",
    "paused_interrupted",
    "awaiting_semantic_review",
    "failed",
    "cancelled",
    "completed",
}
TERMINAL_JOB_STATES = {"failed", "cancelled", "completed"}
PUBLICATION_TIERS = {"diagnostic_only", "local_preview", "reference"}

DEFAULT_BUDGET = {
    "soft_deadline_seconds": 27 * 60,
    "hard_deadline_seconds": 30 * 60,
    "max_case_spec_revisions": 5,
    "max_stage_retries": 1,
    "max_total_retries": 3,
    "max_ue_launches": 6,
    "smoke_budget_seconds": 5 * 60,
    "candidate_reserve_seconds": 12 * 60,
    "post_candidate_reserve_seconds": 3 * 60,
    "max_paid_submissions": 0,
    # Reviewer limits are per CaseSpec attempt. usage.reviewer_invocations is
    # the whole-Job audit count of turn submissions; it is not a Job-level cap.
    # The primary limit is additionally hard-capped to one by the Controller.
    "max_reviewer_invocations": 1,
    "max_reviewer_technical_retries": 1,
    "min_free_disk_bytes": 1024 * 1024 * 1024,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def stable_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_job_id(value: Any) -> str:
    job_id = str(value or "").strip()
    if not re.fullmatch(r"job_[a-z0-9][a-z0-9_-]{7,95}", job_id):
        raise ValueError("job_id must match job_[a-z0-9][a-z0-9_-]{7,95}")
    return job_id


def validate_attempt_id(value: Any) -> str:
    attempt_id = str(value or "").strip()
    if not re.fullmatch(r"attempt_\d{3}", attempt_id):
        raise ValueError("attempt_id must match attempt_NNN")
    return attempt_id


def normalized_budget(overrides: Mapping[str, Any] | None = None) -> dict[str, int]:
    budget = dict(DEFAULT_BUDGET)
    for key, value in dict(overrides or {}).items():
        if key not in DEFAULT_BUDGET:
            raise ValueError(f"unsupported budget field: {key}")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"budget.{key} must be a non-negative integer")
        budget[key] = value
    if budget["soft_deadline_seconds"] > budget["hard_deadline_seconds"]:
        raise ValueError("soft deadline cannot exceed hard deadline")
    if budget["max_case_spec_revisions"] < 1:
        raise ValueError("max_case_spec_revisions must be at least 1")
    return budget


def empty_usage() -> dict[str, Any]:
    return {
        "active_elapsed_seconds": 0.0,
        "case_spec_revisions": 0,
        "total_retries": 0,
        "stage_retries": {},
        "ue_launches": 0,
        "paid_submissions": 0,
        "generation_invocations": 0,
        "reviewer_invocations": 0,
    }


@dataclass(frozen=True)
class JobManifest:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> JobManifest:
        data = dict(raw)
        required = {
            "schema_version",
            "job_id",
            "state",
            "current_stage",
            "current_attempt_id",
            "active_compilation_id",
            "request_digest",
            "intent_contract_digest",
            "target",
            "authorizations",
            "budget",
            "usage",
            "blocker",
            "allowed_next_actions",
            "created_at",
            "updated_at",
        }
        _exact_fields(data, required, "job manifest")
        if data["schema_version"] != JOB_MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported job manifest schema: {data['schema_version']!r}")
        validate_job_id(data["job_id"])
        if data["state"] not in JOB_STATES:
            raise ValueError(f"invalid job state: {data['state']!r}")
        if not isinstance(data["current_stage"], str) or not data["current_stage"].strip():
            raise ValueError("current_stage must be a non-empty string")
        if data["current_attempt_id"] is not None:
            validate_attempt_id(data["current_attempt_id"])
        if data["active_compilation_id"] is not None and not _nonempty(data["active_compilation_id"]):
            raise ValueError("active_compilation_id must be null or a non-empty string")
        _sha256(data["request_digest"], "request_digest")
        if data["intent_contract_digest"] is not None:
            _sha256(data["intent_contract_digest"], "intent_contract_digest")
        target = _mapping(data["target"], "target")
        target.setdefault("ignore_provenance_and_release_gates", False)
        data["target"] = target
        if target.get("execution_profile") not in {"candidate", "local_preview"}:
            raise ValueError("target.execution_profile must be candidate or local_preview")
        if target.get("publication_tier") not in PUBLICATION_TIERS:
            raise ValueError("target.publication_tier is invalid")
        if not isinstance(target.get("ignore_provenance_and_release_gates"), bool):
            raise ValueError("target.ignore_provenance_and_release_gates must be boolean")
        authorizations = _mapping(data["authorizations"], "authorizations")
        # Deterministic in-memory migration for M2/M3 manifests. Semantic
        # Reviewer image upload is purpose-specific and therefore defaults to
        # denied for every historical job.
        authorizations.setdefault("semantic_reviewer_image_upload", False)
        data["authorizations"] = authorizations
        for field in (
            "planning_llm_upload",
            "meshy_upload",
            "external_provider",
            "paid_provider_submission",
            "semantic_reviewer_image_upload",
        ):
            if not isinstance(authorizations.get(field), bool):
                raise ValueError(f"authorizations.{field} must be boolean")
        normalized = normalized_budget(_mapping(data["budget"], "budget"))
        if normalized != data["budget"]:
            raise ValueError("budget must contain every supported field")
        _validate_usage(data["usage"])
        if data["blocker"] is not None:
            blocker = _mapping(data["blocker"], "blocker")
            if set(blocker) != {"code", "message", "stage"} or not all(_nonempty(blocker[key]) for key in blocker):
                raise ValueError("blocker must contain non-empty code, message, and stage")
        if not isinstance(data["allowed_next_actions"], list) or any(not _nonempty(v) for v in data["allowed_next_actions"]):
            raise ValueError("allowed_next_actions must be a string list")
        if (
            data["usage"]["case_spec_revisions"] >= data["budget"]["max_case_spec_revisions"]
            and not (
                isinstance(data.get("blocker"), Mapping)
                and data["blocker"].get("code") == "case_spec_revision_budget_exhausted"
            )
        ):
            data["allowed_next_actions"] = [
                action
                for action in data["allowed_next_actions"]
                if action != "resume_with_revision"
            ]
        if (
            isinstance(data.get("blocker"), Mapping)
            and data["blocker"].get("code") == "case_spec_revision_budget_exhausted"
        ):
            data["allowed_next_actions"] = list(dict.fromkeys([
                "resume",
                "resume_with_revision",
                *data["allowed_next_actions"],
            ]))
        if (
            isinstance(data.get("blocker"), Mapping)
            and data["blocker"].get("code") == "ue_launch_budget_exhausted"
            and "resume" not in data["allowed_next_actions"]
        ):
            data["allowed_next_actions"] = ["resume", *data["allowed_next_actions"]]
        _timestamp(data["created_at"], "created_at")
        _timestamp(data["updated_at"], "updated_at")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


@dataclass(frozen=True)
class IntentContract:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> IntentContract:
        data = dict(raw)
        required = {
            "schema_version",
            "job_id",
            "request_digest",
            "source",
            "original_request",
            "input_identities",
            "hard_requirements",
            "soft_preferences",
            "prohibitions",
            "ambiguities",
            "asset_policy",
            "execution",
            "authorizations",
            "verification",
            "allowed_adjustments",
            "frozen_digests",
            "created_at",
        }
        expected = required if data.get("schema_version") == LEGACY_INTENT_CONTRACT_SCHEMA_VERSION else required | {"planning_image_requirement"}
        if set(data) != expected:
            raise ValueError("intent contract fields mismatch")
        if data["schema_version"] not in {
            LEGACY_INTENT_CONTRACT_SCHEMA_VERSION,
            PROJECTED_INTENT_CONTRACT_SCHEMA_VERSION,
            INTENT_CONTRACT_SCHEMA_VERSION,
        }:
            raise ValueError(f"unsupported intent contract schema: {data['schema_version']!r}")
        validate_job_id(data["job_id"])
        _sha256(data["request_digest"], "request_digest")
        expected_source = {
            LEGACY_INTENT_CONTRACT_SCHEMA_VERSION: "expansion_case_spec_projection_v1",
            PROJECTED_INTENT_CONTRACT_SCHEMA_VERSION: "expansion_case_spec_projection_v1",
            INTENT_CONTRACT_SCHEMA_VERSION: "agent_native_submission_v1",
        }[data["schema_version"]]
        if data["source"] != expected_source:
            raise ValueError("unsupported intent contract source")
        _mapping(data["original_request"], "original_request")
        for field in ("input_identities", "hard_requirements", "soft_preferences", "prohibitions", "ambiguities"):
            if not isinstance(data[field], list) or any(not isinstance(row, Mapping) for row in data[field]):
                raise ValueError(f"{field} must be a list of objects")
        if data["schema_version"] != LEGACY_INTENT_CONTRACT_SCHEMA_VERSION:
            requirement = _mapping(data["planning_image_requirement"], "planning_image_requirement")
            if set(requirement) != {"mode", "input_ids"} or requirement.get("mode") not in {"required", "optional"}:
                raise ValueError("planning_image_requirement is invalid")
            input_ids = requirement.get("input_ids")
            known_image_ids = {
                str(row.get("input_id"))
                for row in data["input_identities"]
                if row.get("kind") == "image"
            }
            if (
                not isinstance(input_ids, list)
                or any(not isinstance(value, str) for value in input_ids)
                or len(input_ids) != len(set(input_ids))
                or set(input_ids) != known_image_ids
                or (requirement["mode"] == "required" and not input_ids)
            ):
                raise ValueError("planning_image_requirement input IDs are invalid")
        for field in ("asset_policy", "execution", "authorizations", "verification", "allowed_adjustments", "frozen_digests"):
            _mapping(data[field], field)
        if data["schema_version"] == LEGACY_INTENT_CONTRACT_SCHEMA_VERSION:
            _validate_legacy_allowed_adjustments(data["allowed_adjustments"])
        else:
            _validate_allowed_adjustments(data["allowed_adjustments"])
        for label, digest in data["frozen_digests"].items():
            _sha256(digest, f"frozen_digests.{label}")
        _timestamp(data["created_at"], "created_at")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


def _validate_allowed_adjustments(value: Mapping[str, Any]) -> None:
    if set(value) != {"paths", "ranges"}:
        raise ValueError("allowed_adjustments must contain only paths and ranges")
    paths = value.get("paths")
    ranges = value.get("ranges")
    if not isinstance(paths, list) or any(
        not isinstance(path, str)
        or not re.fullmatch(r"\$\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", path)
        for path in paths
    ):
        raise ValueError("allowed_adjustments.paths must contain canonical object leaf paths")
    if len(paths) != len(set(paths)):
        raise ValueError("allowed_adjustments.paths must be unique")
    if not isinstance(ranges, Mapping) or set(ranges) != set(paths):
        raise ValueError("allowed_adjustments.ranges must constrain every allowed path exactly")
    for path in paths:
        constraint = ranges[path]
        if not isinstance(constraint, Mapping):
            raise ValueError(f"allowed_adjustments range for {path} must be an object")
        kind = constraint.get("kind")
        if kind == "numeric":
            if set(constraint) != {"kind", "min", "max"}:
                raise ValueError(f"allowed_adjustments numeric range for {path} is invalid")
            minimum, maximum = constraint.get("min"), constraint.get("max")
            if (
                not isinstance(minimum, (int, float))
                or isinstance(minimum, bool)
                or not isinstance(maximum, (int, float))
                or isinstance(maximum, bool)
                or not math.isfinite(float(minimum))
                or not math.isfinite(float(maximum))
                or minimum > maximum
            ):
                raise ValueError(f"allowed_adjustments numeric range for {path} is invalid")
        elif kind == "list":
            if set(constraint) != {"kind", "min_items", "max_items"}:
                raise ValueError(f"allowed_adjustments list range for {path} is invalid")
            minimum, maximum = constraint.get("min_items"), constraint.get("max_items")
            if (
                not isinstance(minimum, int)
                or isinstance(minimum, bool)
                or not isinstance(maximum, int)
                or isinstance(maximum, bool)
                or minimum < 0
                or minimum > maximum
            ):
                raise ValueError(f"allowed_adjustments list range for {path} is invalid")
        elif kind == "numeric_vector":
            minimum, maximum = constraint.get("min"), constraint.get("max")
            if (
                set(constraint) != {"kind", "min", "max"}
                or not isinstance(minimum, list)
                or not isinstance(maximum, list)
                or not minimum
                or len(minimum) != len(maximum)
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    for value in minimum + maximum
                )
                or any(low > high for low, high in zip(minimum, maximum))
            ):
                raise ValueError(f"allowed_adjustments numeric vector range for {path} is invalid")
        elif kind == "enum":
            values = constraint.get("values")
            if (
                set(constraint) != {"kind", "values"}
                or not isinstance(values, list)
                or not values
                or any(isinstance(item, (dict, list)) for item in values)
            ):
                raise ValueError(f"allowed_adjustments enum range for {path} is invalid")
        else:
            raise ValueError(f"allowed_adjustments range kind for {path} is invalid")


def _validate_legacy_allowed_adjustments(value: Mapping[str, Any]) -> None:
    """Read historical v1 Contracts without restoring their broad permissions.

    M2/M3 v1 artifacts allowed canonical paths without corresponding ranges.
    They remain readable and digest-stable, while revision validation continues
    to reject every path that lacks an explicit valid range.
    """
    if set(value) != {"paths", "ranges"}:
        raise ValueError("allowed_adjustments must contain only paths and ranges")
    paths = value.get("paths")
    ranges = value.get("ranges")
    if not isinstance(paths, list) or any(
        not isinstance(path, str)
        or not re.fullmatch(r"\$\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", path)
        for path in paths
    ):
        raise ValueError("allowed_adjustments.paths must contain canonical object paths")
    if len(paths) != len(set(paths)):
        raise ValueError("allowed_adjustments.paths must be unique")
    if not isinstance(ranges, Mapping) or not set(ranges).issubset(set(paths)):
        raise ValueError("legacy allowed_adjustments.ranges must constrain only allowed paths")
    if ranges:
        constrained_paths = sorted(ranges)
        _validate_allowed_adjustments(
            {
                "paths": constrained_paths,
                "ranges": {path: ranges[path] for path in constrained_paths},
            }
        )


@dataclass(frozen=True)
class AttemptManifest:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AttemptManifest:
        data = dict(raw)
        required = {
            "schema_version",
            "job_id",
            "attempt_id",
            "revision",
            "parent_attempt_id",
            "case_spec_digest",
            "intent_contract_digest",
            "revision_reason",
            "status",
            "compilation_id",
            "execution_fingerprint",
            "smoke_gate",
            "created_at",
            "updated_at",
        }
        _exact_fields(data, required, "attempt manifest")
        if data["schema_version"] != ATTEMPT_MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported attempt manifest schema: {data['schema_version']!r}")
        validate_job_id(data["job_id"])
        validate_attempt_id(data["attempt_id"])
        if not isinstance(data["revision"], int) or isinstance(data["revision"], bool) or data["revision"] < 1:
            raise ValueError("revision must be a positive integer")
        if data["parent_attempt_id"] is not None:
            validate_attempt_id(data["parent_attempt_id"])
        for field in ("case_spec_digest", "intent_contract_digest"):
            _sha256(data[field], field)
        if not _nonempty(data["revision_reason"]) or not _nonempty(data["status"]):
            raise ValueError("revision_reason and status must be non-empty strings")
        for field in ("compilation_id", "execution_fingerprint", "smoke_gate"):
            if data[field] is not None and not _nonempty(data[field]):
                raise ValueError(f"{field} must be null or a non-empty string")
        _timestamp(data["created_at"], "created_at")
        _timestamp(data["updated_at"], "updated_at")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


def checkpoint_payload(
    *,
    job_id: str,
    attempt_id: str | None,
    stage: str,
    status: str,
    input_digest: str,
    artifact_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    validate_job_id(job_id)
    if attempt_id is not None:
        validate_attempt_id(attempt_id)
    _sha256(input_digest, "input_digest")
    if status not in {"started", "completed", "blocked", "failed", "interrupted", "reused"}:
        raise ValueError(f"invalid checkpoint status: {status}")
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "stage": str(stage),
        "status": status,
        "input_digest": input_digest,
        "artifact_refs": list(artifact_refs or []),
        "updated_at": utc_now(),
    }


def _validate_usage(value: Any) -> None:
    usage = _mapping(value, "usage")
    if set(usage) != set(empty_usage()):
        raise ValueError("usage must contain every supported field")
    elapsed = usage["active_elapsed_seconds"]
    if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or elapsed < 0:
        raise ValueError("usage.active_elapsed_seconds must be non-negative")
    for field in set(empty_usage()) - {"active_elapsed_seconds", "stage_retries"}:
        if not isinstance(usage[field], int) or isinstance(usage[field], bool) or usage[field] < 0:
            raise ValueError(f"usage.{field} must be a non-negative integer")
    retries = _mapping(usage["stage_retries"], "usage.stage_retries")
    if any(not _nonempty(key) or not isinstance(count, int) or isinstance(count, bool) or count < 0 for key, count in retries.items()):
        raise ValueError("usage.stage_retries must map stage names to non-negative integers")


def _exact_fields(data: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(data))
    extra = sorted(set(data) - expected)
    if missing or extra:
        raise ValueError(f"{label} fields mismatch; missing={missing}, extra={extra}")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value.casefold()):
        raise ValueError(f"{label} must be a SHA-256 digest")


def _timestamp(value: Any, label: str) -> None:
    if not _nonempty(value):
        raise ValueError(f"{label} must be an ISO timestamp")
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
