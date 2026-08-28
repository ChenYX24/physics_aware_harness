from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Mapping

from harness.agent.job_schema import stable_digest, validate_attempt_id, validate_job_id


REVISION_PROPOSAL_SCHEMA_VERSION = "harness_case_spec_revision_proposal_v1"
EVIDENCE_BUNDLE_SCHEMA_VERSION = "harness_evidence_bundle_v2"
REVIEWER_RECEIPT_SCHEMA_VERSION = "harness_semantic_reviewer_receipt_v2"
REVIEWER_INVOCATION_SCHEMA_VERSION = "harness_semantic_reviewer_invocation_v1"
SEMANTIC_REVIEW_SCHEMA_VERSION = "harness_semantic_review_v1"

REPAIR_LAYERS = {
    "none",
    "observation",
    "camera",
    "case_spec_source",
    "evidence",
    "user_decision",
}
SEMANTIC_STATUSES = {"pass", "fail", "uncertain"}


@dataclass(frozen=True)
class RevisionProposal:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RevisionProposal:
        data = dict(raw)
        _exact(
            data,
            {
                "schema_version",
                "job_id",
                "proposal_id",
                "base_attempt_id",
                "base_case_spec_digest",
                "intent_contract_digest",
                "trigger_stage",
                "trigger_failure_code",
                "repair_layer",
                "changes",
                "revised_case_spec_digest",
                "reason",
                "evidence_refs",
                "created_at",
            },
            "revision proposal",
        )
        if data["schema_version"] != REVISION_PROPOSAL_SCHEMA_VERSION:
            raise ValueError("unsupported revision proposal schema")
        validate_job_id(data["job_id"])
        validate_attempt_id(data["base_attempt_id"])
        if not re.fullmatch(r"proposal_[0-9a-f]{16}", str(data["proposal_id"])):
            raise ValueError("proposal_id must match proposal_<16 hex>")
        for field in ("base_case_spec_digest", "intent_contract_digest", "revised_case_spec_digest"):
            _sha256(data[field], field)
        for field in ("trigger_stage", "trigger_failure_code", "reason"):
            _nonempty(data[field], field)
        if data["repair_layer"] not in REPAIR_LAYERS - {"none", "user_decision"}:
            raise ValueError("revision proposal repair_layer is not materializable")
        changes = data["changes"]
        if not isinstance(changes, list) or not changes:
            raise ValueError("revision proposal requires at least one canonical change")
        for index, change in enumerate(changes):
            value = _mapping(change, f"changes[{index}]")
            _exact(value, {"path", "operation", "before", "after"}, f"changes[{index}]")
            if not str(value["path"]).startswith("$."):
                raise ValueError("revision change paths must be canonical JSON paths")
            if value["operation"] not in {"add", "remove", "replace"}:
                raise ValueError("revision change operation is invalid")
        _string_list(data["evidence_refs"], "evidence_refs")
        _timestamp(data["created_at"], "created_at")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


@dataclass(frozen=True)
class EvidenceBundleManifest:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> EvidenceBundleManifest:
        data = dict(raw)
        _exact(
            data,
            {
                "schema_version",
                "job_id",
                "attempt_id",
                "case_spec_digest",
                "intent_contract_digest",
                "candidate_run",
                "technical_gates",
                "event_selection",
                "trajectory_summary",
                "contact_timeline",
                "artifacts",
                "created_at",
            },
            "evidence bundle manifest",
        )
        if data["schema_version"] != EVIDENCE_BUNDLE_SCHEMA_VERSION:
            raise ValueError("unsupported Evidence Bundle schema")
        validate_job_id(data["job_id"])
        validate_attempt_id(data["attempt_id"])
        _sha256(data["case_spec_digest"], "case_spec_digest")
        _sha256(data["intent_contract_digest"], "intent_contract_digest")
        candidate = _mapping(data["candidate_run"], "candidate_run")
        _exact(candidate, {"path", "fingerprint"}, "candidate_run")
        _safe_relative(candidate["path"], "candidate_run.path")
        _sha256(candidate["fingerprint"], "candidate_run.fingerprint")
        gates = _mapping(data["technical_gates"], "technical_gates")
        _exact(gates, {"verifier", "render_sync", "quality_gate"}, "technical_gates")
        for name, gate in gates.items():
            value = _mapping(gate, f"technical_gates.{name}")
            _exact(value, {"status", "path", "sha256"}, f"technical_gates.{name}")
            if value["status"] != "pass":
                raise ValueError("formal Evidence Bundle requires every technical gate to pass")
            _safe_relative(value["path"], f"technical_gates.{name}.path")
            _sha256(value["sha256"], f"technical_gates.{name}.sha256")
        selection = _mapping(data["event_selection"], "event_selection")
        _exact(selection, {"strategy", "reason", "points"}, "event_selection")
        if selection["strategy"] not in {
            "event_anchored",
            "event_sequence_transition",
            "start_mid_end",
            "uniform_interval",
        }:
            raise ValueError("event_selection.strategy is invalid")
        _nonempty(selection["reason"], "event_selection.reason")
        points = selection["points"]
        labels = [row.get("label") for row in points if isinstance(row, Mapping)] if isinstance(points, list) else []
        if not labels or len(labels) > 6 or len(labels) != len(points) or len(set(labels)) != len(labels):
            raise ValueError("event_selection.points must contain one to six uniquely labeled points")
        for index, point in enumerate(points):
            value = _mapping(point, f"event_selection.points[{index}]")
            _exact(value, {"label", "time_s", "frame_index", "event_refs"}, f"event_selection.points[{index}]")
            _number(value["time_s"], f"event_selection.points[{index}].time_s")
            if not isinstance(value["frame_index"], int) or isinstance(value["frame_index"], bool) or value["frame_index"] < 0:
                raise ValueError("event selection frame_index must be non-negative")
            _string_list(value["event_refs"], "event_refs")
        trajectory = _mapping(data["trajectory_summary"], "trajectory_summary")
        _exact(
            trajectory,
            {
                "source_path",
                "source_sha256",
                "frame_count",
                "start_time_s",
                "end_time_s",
                "objects",
                "sampling",
                "sampled_frames",
                "readable_ranges",
                "state_transitions",
            },
            "trajectory_summary",
        )
        _safe_relative(trajectory["source_path"], "trajectory_summary.source_path")
        _sha256(trajectory["source_sha256"], "trajectory_summary.source_sha256")
        if not isinstance(trajectory["frame_count"], int) or trajectory["frame_count"] < 1:
            raise ValueError("trajectory_summary.frame_count must be positive")
        _number(trajectory["start_time_s"], "trajectory_summary.start_time_s")
        _number(trajectory["end_time_s"], "trajectory_summary.end_time_s")
        if trajectory["start_time_s"] > trajectory["end_time_s"]:
            raise ValueError("trajectory_summary time range is invalid")
        _string_list(trajectory["objects"], "trajectory_summary.objects")
        if len(trajectory["objects"]) != len(set(trajectory["objects"])):
            raise ValueError("trajectory_summary.objects must be unique")
        sampling = _mapping(trajectory["sampling"], "trajectory_summary.sampling")
        _exact(
            sampling,
            {
                "strategy",
                "max_sample_frames",
                "selected_frame_count",
                "omitted_frame_count",
                "state_transition_count",
                "state_transitions_included",
            },
            "trajectory_summary.sampling",
        )
        if sampling["strategy"] != "uniform_event_state_v1":
            raise ValueError("trajectory sampling strategy is invalid")
        for field in (
            "max_sample_frames",
            "selected_frame_count",
            "omitted_frame_count",
            "state_transition_count",
            "state_transitions_included",
        ):
            if not isinstance(sampling[field], int) or isinstance(sampling[field], bool) or sampling[field] < 0:
                raise ValueError(f"trajectory_summary.sampling.{field} must be a non-negative integer")
        samples = trajectory["sampled_frames"]
        if not isinstance(samples, list) or not samples:
            raise ValueError("trajectory_summary.sampled_frames must be a non-empty list")
        sample_by_index: dict[int, Mapping[str, Any]] = {}
        prior_frame_index = -1
        for index, sample in enumerate(samples):
            value = _mapping(sample, f"trajectory_summary.sampled_frames[{index}]")
            _exact(value, {"frame_index", "time_s", "reasons", "objects"}, f"trajectory_summary.sampled_frames[{index}]")
            frame_index = value["frame_index"]
            if (
                not isinstance(frame_index, int)
                or isinstance(frame_index, bool)
                or not 0 <= frame_index < trajectory["frame_count"]
                or frame_index <= prior_frame_index
            ):
                raise ValueError("trajectory sampled frame indices must be unique, ordered, and in range")
            prior_frame_index = frame_index
            _number(value["time_s"], "trajectory sample time_s")
            if not trajectory["start_time_s"] <= value["time_s"] <= trajectory["end_time_s"]:
                raise ValueError("trajectory sampled frame time_s is outside the trajectory")
            _string_list(value["reasons"], "trajectory sample reasons")
            if not isinstance(value["objects"], list):
                raise ValueError("trajectory sample objects must be a list")
            object_ids: set[str] = set()
            for object_index, object_state in enumerate(value["objects"]):
                state = _mapping(object_state, f"trajectory sample objects[{object_index}]")
                _exact(
                    state,
                    {"object_id", "transform", "linear_velocity", "angular_velocity", "state"},
                    f"trajectory sample objects[{object_index}]",
                )
                _nonempty(state["object_id"], "trajectory sample object_id")
                if state["object_id"] in object_ids or state["object_id"] not in trajectory["objects"]:
                    raise ValueError("trajectory sample object IDs must be unique and declared")
                object_ids.add(state["object_id"])
                transform = _mapping(state["transform"], "trajectory sample transform")
                _exact(transform, {"position", "rotation", "scale"}, "trajectory sample transform")
                for field in ("position", "rotation", "scale"):
                    _state_vector(transform[field], f"trajectory sample transform.{field}")
                _state_vector(state["linear_velocity"], "trajectory sample linear_velocity")
                _state_vector(state["angular_velocity"], "trajectory sample angular_velocity")
                _state_fields(state["state"], "trajectory sample state")
            sample_by_index[frame_index] = value
        if sampling["selected_frame_count"] != len(samples):
            raise ValueError("trajectory selected_frame_count does not match sampled_frames")
        if sampling["omitted_frame_count"] != trajectory["frame_count"] - len(samples):
            raise ValueError("trajectory omitted_frame_count does not match frame_count")
        ranges = trajectory["readable_ranges"]
        if not isinstance(ranges, list) or not ranges:
            raise ValueError("trajectory_summary.readable_ranges must be a non-empty list")
        range_ids: set[str] = set()
        for index, readable in enumerate(ranges):
            value = _mapping(readable, f"trajectory_summary.readable_ranges[{index}]")
            _exact(
                value,
                {
                    "range_id",
                    "start_frame_index",
                    "end_frame_index",
                    "start_time_s",
                    "end_time_s",
                    "sample_frame_indices",
                    "event_refs",
                },
                f"trajectory_summary.readable_ranges[{index}]",
            )
            _nonempty(value["range_id"], "trajectory readable range_id")
            if value["range_id"] in range_ids:
                raise ValueError("trajectory readable range IDs must be unique")
            range_ids.add(value["range_id"])
            start_index, end_index = value["start_frame_index"], value["end_frame_index"]
            if (
                not isinstance(start_index, int)
                or isinstance(start_index, bool)
                or not isinstance(end_index, int)
                or isinstance(end_index, bool)
                or start_index > end_index
                or start_index not in sample_by_index
                or end_index not in sample_by_index
            ):
                raise ValueError("trajectory readable range endpoints must be sampled frames")
            _number(value["start_time_s"], "trajectory readable range start_time_s")
            _number(value["end_time_s"], "trajectory readable range end_time_s")
            if (
                not math.isclose(float(value["start_time_s"]), float(sample_by_index[start_index]["time_s"]), abs_tol=1e-9)
                or not math.isclose(float(value["end_time_s"]), float(sample_by_index[end_index]["time_s"]), abs_tol=1e-9)
            ):
                raise ValueError("trajectory readable range times must match sampled endpoints")
            indices = value["sample_frame_indices"]
            if (
                not isinstance(indices, list)
                or not indices
                or indices != sorted(set(indices))
                or indices[0] != start_index
                or indices[-1] != end_index
                or any(frame_index not in sample_by_index for frame_index in indices)
            ):
                raise ValueError("trajectory readable range must list its ordered sampled frames")
            _string_list(value["event_refs"], "trajectory readable range event_refs")
        transitions = trajectory["state_transitions"]
        if not isinstance(transitions, list):
            raise ValueError("trajectory_summary.state_transitions must be a list")
        for index, transition in enumerate(transitions):
            value = _mapping(transition, f"trajectory_summary.state_transitions[{index}]")
            _exact(value, {"object_id", "frame_index", "time_s", "before", "after"}, f"trajectory_summary.state_transitions[{index}]")
            if value["object_id"] not in trajectory["objects"]:
                raise ValueError("trajectory state transition object is not declared")
            if not isinstance(value["frame_index"], int) or not 0 <= value["frame_index"] < trajectory["frame_count"]:
                raise ValueError("trajectory state transition frame is outside the trajectory")
            _number(value["time_s"], "trajectory state transition time_s")
            _state_fields(value["before"], "trajectory state transition before")
            _state_fields(value["after"], "trajectory state transition after")
        if sampling["state_transitions_included"] != len(transitions):
            raise ValueError("trajectory state_transitions_included does not match state_transitions")
        if sampling["state_transition_count"] < len(transitions):
            raise ValueError("trajectory state_transition_count cannot be smaller than included transitions")
        if not isinstance(data["contact_timeline"], list) or any(not isinstance(row, Mapping) for row in data["contact_timeline"]):
            raise ValueError("contact_timeline must be a list of objects")
        contact_ids: set[str] = set()
        for index, contact in enumerate(data["contact_timeline"]):
            value = _mapping(contact, f"contact_timeline[{index}]")
            _exact(value, {"event_id", "frame_index", "time_s", "objects", "kind"}, f"contact_timeline[{index}]")
            _nonempty(value["event_id"], "contact.event_id")
            if value["event_id"] in contact_ids:
                raise ValueError("contact event IDs must be unique")
            contact_ids.add(value["event_id"])
            if not isinstance(value["frame_index"], int) or isinstance(value["frame_index"], bool) or not 0 <= value["frame_index"] < trajectory["frame_count"]:
                raise ValueError("contact frame_index is outside the trajectory")
            _number(value["time_s"], "contact.time_s")
            if not trajectory["start_time_s"] <= value["time_s"] <= trajectory["end_time_s"]:
                raise ValueError("contact time_s is outside the trajectory")
            if not isinstance(value["objects"], list) or any(not isinstance(item, str) or not item for item in value["objects"]):
                raise ValueError("contact.objects must be a string list")
            _nonempty(value["kind"], "contact.kind")
        for readable in ranges:
            if any(event_id not in contact_ids for event_id in readable["event_refs"]):
                raise ValueError("trajectory readable range references an unknown contact event")
        for point in points:
            if point["frame_index"] >= trajectory["frame_count"]:
                raise ValueError("event selection frame_index is outside the trajectory")
            if not trajectory["start_time_s"] <= point["time_s"] <= trajectory["end_time_s"]:
                raise ValueError("event selection time_s is outside the trajectory")
            if any(event_id not in contact_ids for event_id in point["event_refs"]):
                raise ValueError("event selection references an unknown contact event")
        artifacts = data["artifacts"]
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError("Evidence Bundle requires artifacts")
        identities: set[str] = set()
        for index, artifact in enumerate(artifacts):
            value = _mapping(artifact, f"artifacts[{index}]")
            _exact(value, {"artifact_id", "kind", "path", "sha256", "mime_type", "time_s", "view_id", "source_ref"}, f"artifacts[{index}]")
            identity = str(value["artifact_id"])
            if not re.fullmatch(r"[a-z][a-z0-9_]{2,95}", identity) or identity in identities:
                raise ValueError("Evidence Bundle artifact IDs must be unique stable identifiers")
            identities.add(identity)
            _nonempty(value["kind"], "artifact.kind")
            _safe_relative(value["path"], "artifact.path")
            _sha256(value["sha256"], "artifact.sha256")
            _nonempty(value["mime_type"], "artifact.mime_type")
            if value["time_s"] is not None:
                _number(value["time_s"], "artifact.time_s")
                if not trajectory["start_time_s"] <= value["time_s"] <= trajectory["end_time_s"]:
                    raise ValueError("artifact time_s is outside the trajectory")
            if value["view_id"] is not None:
                _nonempty(value["view_id"], "artifact.view_id")
            if value["source_ref"] is not None:
                _safe_relative(value["source_ref"], "artifact.source_ref")
        _timestamp(data["created_at"], "created_at")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


@dataclass(frozen=True)
class ReviewerInvocationReceipt:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ReviewerInvocationReceipt:
        data = dict(raw)
        _exact(
            data,
            {
                "schema_version",
                "job_id",
                "attempt_id",
                "invocation_count",
                "transport",
                "executable",
                "codex_version",
                "thread_id",
                "turn_id",
                "model",
                "model_provider",
                "requested_new_thread",
                "requested_permission_profile",
                "requested_permission_profile_digest",
                "active_permission_profile_id",
                "runtime_workspace_roots",
                "ephemeral",
                "shell_environment_policy",
                "instruction_sources",
                "network_access",
                "input_digest",
                "output_digest",
                "status",
                "error_code",
                "started_at",
                "completed_at",
            },
            "reviewer invocation receipt",
        )
        if data["schema_version"] != REVIEWER_RECEIPT_SCHEMA_VERSION:
            raise ValueError("unsupported Reviewer receipt schema")
        validate_job_id(data["job_id"])
        validate_attempt_id(data["attempt_id"])
        if not isinstance(data["invocation_count"], int) or isinstance(data["invocation_count"], bool) or data["invocation_count"] < 1:
            raise ValueError("reviewer invocation_count must be positive")
        if data["transport"] != "stdio_jsonl":
            raise ValueError("Reviewer transport must be stdio_jsonl")
        for field in ("executable", "codex_version"):
            _nonempty(data[field], field)
        for field in ("thread_id", "turn_id", "model", "model_provider", "output_digest", "error_code"):
            if data[field] is not None:
                _nonempty(data[field], field)
        if data["requested_new_thread"] is not True:
            raise ValueError("Reviewer receipt must prove a new thread was requested")
        requested = _mapping(data["requested_permission_profile"], "requested_permission_profile")
        _exact(requested, {"id", "filesystem", "network"}, "requested_permission_profile")
        if not re.fullmatch(r"harness_reviewer_[0-9a-f]{16}", str(requested["id"])):
            raise ValueError("Reviewer permission profile id is invalid")
        filesystem = _mapping(requested["filesystem"], "requested_permission_profile.filesystem")
        roots = data["runtime_workspace_roots"]
        if not isinstance(roots, list) or len(roots) != 1:
            raise ValueError("Reviewer must have exactly one runtime workspace root")
        _nonempty(roots[0], "runtime_workspace_roots[0]")
        if not PurePosixPath(roots[0]).is_absolute():
            raise ValueError("Reviewer runtime workspace root must be absolute")
        current_filesystem = {":root": "deny", ":minimal": "read", roots[0]: "read"}
        if filesystem != current_filesystem:
            raise ValueError(
                "Reviewer permission profile must deny the filesystem by default and grant only minimal runtime and Bundle reads"
            )
        network = _mapping(requested["network"], "requested_permission_profile.network")
        if network != {"enabled": False}:
            raise ValueError("Reviewer permission profile must disable network access")
        _sha256(data["requested_permission_profile_digest"], "requested_permission_profile_digest")
        if data["requested_permission_profile_digest"] != stable_digest(requested):
            raise ValueError("Reviewer permission profile digest is invalid")
        if data["ephemeral"] is not True:
            raise ValueError("Reviewer thread must be ephemeral")
        shell_policy = _mapping(data["shell_environment_policy"], "shell_environment_policy")
        _exact(shell_policy, {"inherit", "set", "use_profile"}, "shell_environment_policy")
        if shell_policy["inherit"] != "none" or shell_policy["use_profile"] is not False:
            raise ValueError("Reviewer shell must not inherit the host environment or shell profile")
        shell_set = _mapping(shell_policy["set"], "shell_environment_policy.set")
        if set(shell_set) != {"PATH"} or not isinstance(shell_set["PATH"], str) or not shell_set["PATH"]:
            raise ValueError("Reviewer shell environment may only receive a controlled PATH")
        _string_list(data["instruction_sources"], "instruction_sources")
        if data["network_access"] is not False:
            raise ValueError("Reviewer network access must be disabled")
        _sha256(data["input_digest"], "input_digest")
        if data["output_digest"] is not None:
            _sha256(data["output_digest"], "output_digest")
        if data["status"] not in {"completed", "failed", "interrupted"}:
            raise ValueError("Reviewer receipt status is invalid")
        if data["active_permission_profile_id"] is not None:
            _nonempty(data["active_permission_profile_id"], "active_permission_profile_id")
        if data["status"] == "completed" and (
            not data["thread_id"]
            or not data["turn_id"]
            or not data["model"]
            or not data["output_digest"]
            or data["error_code"] is not None
            or data["active_permission_profile_id"] != requested["id"]
        ):
            raise ValueError("completed Reviewer receipt is incomplete")
        if data["status"] != "completed" and data["error_code"] is None:
            raise ValueError("failed Reviewer receipt requires error_code")
        _timestamp(data["started_at"], "started_at")
        _timestamp(data["completed_at"], "completed_at")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


@dataclass(frozen=True)
class ReviewerInvocationReservation:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ReviewerInvocationReservation:
        data = dict(raw)
        _exact(
            data,
            {
                "schema_version",
                "job_id",
                "attempt_id",
                "invocation_count",
                "invocation_id",
                "role",
                "state",
                "outcome",
                "bundle_digest",
                "input_digest",
                "usage_counted",
                "retryable",
                "receipt_path",
                "error_code",
                "created_at",
                "updated_at",
            },
            "reviewer invocation reservation",
        )
        if data["schema_version"] != REVIEWER_INVOCATION_SCHEMA_VERSION:
            raise ValueError("unsupported Reviewer invocation reservation schema")
        validate_job_id(data["job_id"])
        validate_attempt_id(data["attempt_id"])
        if not isinstance(data["invocation_count"], int) or isinstance(data["invocation_count"], bool) or data["invocation_count"] < 1:
            raise ValueError("Reviewer invocation_count must be positive")
        if not re.fullmatch(r"reviewer_[0-9a-f]{16}", str(data["invocation_id"])):
            raise ValueError("Reviewer invocation_id is invalid")
        if data["role"] not in {"primary", "resume", "technical_retry"}:
            raise ValueError("Reviewer invocation role is invalid")
        if data["state"] not in {
            "reserved",
            "launching",
            "started",
            "output_received",
            "completion_unknown",
            "receipt_recorded",
        }:
            raise ValueError("Reviewer invocation state is invalid")
        if data["outcome"] not in {
            "pending",
            "completed",
            "technical_failed",
            "interrupted",
            "blocked_configuration",
            "completion_unknown",
        }:
            raise ValueError("Reviewer invocation outcome is invalid")
        _sha256(data["bundle_digest"], "bundle_digest")
        _sha256(data["input_digest"], "input_digest")
        if not isinstance(data["usage_counted"], bool):
            raise ValueError("Reviewer invocation usage_counted must be boolean")
        if data["retryable"] is not None and not isinstance(data["retryable"], bool):
            raise ValueError("Reviewer invocation retryable must be null or boolean")
        if data["receipt_path"] is not None:
            _safe_relative(data["receipt_path"], "receipt_path")
        if data["error_code"] is not None:
            _nonempty(data["error_code"], "error_code")
        _timestamp(data["created_at"], "created_at")
        _timestamp(data["updated_at"], "updated_at")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


@dataclass(frozen=True)
class SemanticReview:
    data: dict[str, Any]

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
        *,
        expected_requirement_ids: set[str] | None = None,
        evidence_artifact_ids: set[str] | None = None,
        evidence_manifest: Mapping[str, Any] | None = None,
    ) -> SemanticReview:
        data = dict(raw)
        _exact(
            data,
            {
                "schema_version",
                "job_id",
                "attempt_id",
                "evidence_bundle_digest",
                "reviewer_receipt_digest",
                "overall_status",
                "requirements",
                "repair_layer",
                "summary",
                "suggested_adjustments",
                "created_at",
            },
            "semantic review",
        )
        if data["schema_version"] != SEMANTIC_REVIEW_SCHEMA_VERSION:
            raise ValueError("unsupported Semantic Review schema")
        validate_job_id(data["job_id"])
        validate_attempt_id(data["attempt_id"])
        _sha256(data["evidence_bundle_digest"], "evidence_bundle_digest")
        _sha256(data["reviewer_receipt_digest"], "reviewer_receipt_digest")
        if data["overall_status"] not in SEMANTIC_STATUSES:
            raise ValueError("semantic overall_status is invalid")
        rows = data["requirements"]
        if not isinstance(rows, list) or not rows:
            raise ValueError("semantic review requires requirement verdicts")
        seen: set[str] = set()
        statuses: list[str] = []
        for index, row in enumerate(rows):
            value = _mapping(row, f"requirements[{index}]")
            _exact(value, {"requirement_id", "status", "rationale", "evidence_refs"}, f"requirements[{index}]")
            identity = str(value["requirement_id"])
            _nonempty(identity, "requirement_id")
            if identity in seen:
                raise ValueError("semantic requirement IDs must be unique")
            seen.add(identity)
            if value["status"] not in SEMANTIC_STATUSES:
                raise ValueError("semantic requirement status is invalid")
            statuses.append(value["status"])
            _nonempty(value["rationale"], "requirement.rationale")
            refs = value["evidence_refs"]
            if not isinstance(refs, list) or not refs:
                raise ValueError("requirement.evidence_refs must be a non-empty list")
            for ref_index, ref in enumerate(refs):
                item = _mapping(ref, f"evidence_refs[{ref_index}]")
                _exact(item, {"artifact_id", "time_s", "view_id", "trajectory_range", "contact_event_id"}, f"evidence_refs[{ref_index}]")
                _nonempty(item["artifact_id"], "evidence_ref.artifact_id")
                if evidence_artifact_ids is not None and item["artifact_id"] not in evidence_artifact_ids:
                    raise ValueError("semantic review references an unknown Evidence Bundle artifact")
                if item["time_s"] is not None:
                    _number(item["time_s"], "evidence_ref.time_s")
                for field in ("view_id", "trajectory_range", "contact_event_id"):
                    if item[field] is not None:
                        _nonempty(item[field], f"evidence_ref.{field}")
                if all(item[field] is None for field in ("time_s", "view_id", "trajectory_range", "contact_event_id")):
                    raise ValueError("each semantic evidence reference requires a concrete locator")
        if expected_requirement_ids is not None and seen != expected_requirement_ids:
            raise ValueError("semantic review must cover every hard requirement exactly once")
        derived = "uncertain" if "uncertain" in statuses else "fail" if "fail" in statuses else "pass"
        if data["overall_status"] != derived:
            raise ValueError("semantic overall_status does not match requirement verdicts")
        if data["repair_layer"] not in REPAIR_LAYERS:
            raise ValueError("semantic repair_layer is invalid")
        if derived == "pass" and data["repair_layer"] != "none":
            raise ValueError("semantic pass must use repair_layer=none")
        if derived != "pass" and data["repair_layer"] == "none":
            raise ValueError("semantic non-pass requires a repair layer")
        _nonempty(data["summary"], "summary")
        suggestions = data["suggested_adjustments"]
        if not isinstance(suggestions, list):
            raise ValueError("suggested_adjustments must be a list")
        suggested_paths: set[str] = set()
        for index, suggestion in enumerate(suggestions):
            value = _mapping(suggestion, f"suggested_adjustments[{index}]")
            _exact(value, {"path", "desired_outcome", "evidence_refs"}, f"suggested_adjustments[{index}]")
            path = str(value["path"])
            if not re.fullmatch(r"\$\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", path):
                raise ValueError("suggested adjustment path must be a canonical source CaseSpec object path")
            if path in suggested_paths:
                raise ValueError("suggested adjustment paths must be unique")
            suggested_paths.add(path)
            _nonempty(value["desired_outcome"], "suggested_adjustment.desired_outcome")
            _string_list(value["evidence_refs"], "suggested_adjustment.evidence_refs")
            if evidence_artifact_ids is not None and any(
                artifact_id not in evidence_artifact_ids for artifact_id in value["evidence_refs"]
            ):
                raise ValueError("suggested adjustment references an unknown Evidence Bundle artifact")
        _timestamp(data["created_at"], "created_at")
        if evidence_manifest is not None:
            _validate_evidence_locators(data, EvidenceBundleManifest.from_dict(evidence_manifest).to_dict())
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


def semantic_review_output_schema() -> dict[str, Any]:
    nullable_string = {"type": ["string", "null"]}
    nullable_number = {"type": ["number", "null"]}
    evidence_ref = {
        "type": "object",
        "additionalProperties": False,
        "description": (
            "A manifest artifact plus a concrete locator. At least one of time_s, view_id, "
            "trajectory_range, or contact_event_id must be non-null; copy locator values exactly "
            "from the Evidence Bundle manifest. Keyframes require exact time_s and view_id; "
            "multi-view montages require exact time_s."
        ),
        "properties": {
            "artifact_id": {"type": "string"},
            "time_s": nullable_number,
            "view_id": nullable_string,
            "trajectory_range": nullable_string,
            "contact_event_id": nullable_string,
        },
        "required": ["artifact_id", "time_s", "view_id", "trajectory_range", "contact_event_id"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "overall_status": {
                "type": "string",
                "enum": sorted(SEMANTIC_STATUSES),
                "description": (
                    "uncertain if any requirement is uncertain; otherwise fail if any requirement "
                    "fails; otherwise pass"
                ),
            },
            "requirements": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "requirement_id": {"type": "string"},
                        "status": {"type": "string", "enum": sorted(SEMANTIC_STATUSES)},
                        "rationale": {"type": "string"},
                        "evidence_refs": {"type": "array", "minItems": 1, "items": evidence_ref},
                    },
                    "required": ["requirement_id", "status", "rationale", "evidence_refs"],
                },
            },
            "repair_layer": {
                "type": "string",
                "enum": sorted(REPAIR_LAYERS),
                "description": "Use none only for overall_status=pass; non-pass requires a non-none layer.",
            },
            "summary": {"type": "string"},
            "suggested_adjustments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Copy one exact canonical path from inputs/intent_contract.json "
                                "allowed_adjustments.paths. Do not invent paths, use array indices, "
                                "or cite broad parent objects."
                            ),
                        },
                        "desired_outcome": {"type": "string"},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["path", "desired_outcome", "evidence_refs"],
                },
            },
        },
        "required": ["overall_status", "requirements", "repair_layer", "summary", "suggested_adjustments"],
    }


def _exact(data: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(data))
    extra = sorted(set(data) - expected)
    if missing or extra:
        raise ValueError(f"{label} fields mismatch; missing={missing}, extra={extra}")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _nonempty(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value.casefold()):
        raise ValueError(f"{label} must be a SHA-256 digest")


def _timestamp(value: Any, label: str) -> None:
    _nonempty(value, label)
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc


def _safe_relative(value: Any, label: str) -> None:
    _nonempty(value, label)
    path = PurePosixPath(str(value))
    if path.is_absolute() or ".." in path.parts or str(path) in {".", ""}:
        raise ValueError(f"{label} must be a safe relative path")


def _number(value: Any, label: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be numeric")


def _string_list(value: Any, label: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} must be a string list")


def _validate_evidence_locators(review: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    artifacts = {str(row["artifact_id"]): row for row in manifest["artifacts"]}
    trajectory = manifest["trajectory_summary"]
    start_time = float(trajectory["start_time_s"])
    end_time = float(trajectory["end_time_s"])
    contacts = {str(row["event_id"]): row for row in manifest["contact_timeline"]}
    readable_point_times = [
        float(row["time_s"])
        for row in trajectory["sampled_frames"]
    ] + [
        float(row["time_s"])
        for row in trajectory["state_transitions"]
    ] + [float(row["time_s"]) for row in contacts.values()]
    result_kinds = {"structured_summary", "keyframe", "multi_view_montage"}
    for row in review["requirements"]:
        has_result_evidence = False
        for ref in row["evidence_refs"]:
            artifact = artifacts[str(ref["artifact_id"])]
            artifact_kind = artifact["kind"]
            if artifact_kind in result_kinds:
                has_result_evidence = True
            time_s = ref["time_s"]
            if time_s is not None:
                _number(time_s, "evidence_ref.time_s")
                if not start_time <= float(time_s) <= end_time:
                    raise ValueError("semantic evidence time_s is outside the trajectory")
                if artifact["time_s"] is not None and not math.isclose(
                    float(time_s), float(artifact["time_s"]), rel_tol=0.0, abs_tol=1e-6
                ):
                    raise ValueError("semantic evidence time_s does not locate the referenced artifact")
                if artifact_kind == "structured_summary" and not any(
                    math.isclose(float(time_s), readable_time, rel_tol=0.0, abs_tol=1e-6)
                    for readable_time in readable_point_times
                ):
                    raise ValueError("semantic evidence time_s does not identify a readable sampled or event time")
            view_id = ref["view_id"]
            if view_id is not None and artifact["view_id"] != view_id:
                raise ValueError("semantic evidence view_id does not locate the referenced artifact")
            trajectory_range = ref["trajectory_range"]
            if trajectory_range is not None:
                if artifact_kind != "structured_summary":
                    raise ValueError("semantic evidence trajectory_range is incompatible with the referenced artifact")
                bounds = _parse_trajectory_range(trajectory_range)
                if bounds is None:
                    raise ValueError("semantic evidence trajectory_range is invalid")
                range_start, range_end = bounds
                if range_start > range_end or range_start < start_time or range_end > end_time:
                    raise ValueError("semantic evidence trajectory_range is outside the trajectory")
                readable_ranges = trajectory["readable_ranges"]
                if not any(
                    math.isclose(range_start, float(readable["start_time_s"]), rel_tol=0.0, abs_tol=1e-6)
                    and math.isclose(range_end, float(readable["end_time_s"]), rel_tol=0.0, abs_tol=1e-6)
                    for readable in readable_ranges
                ):
                    raise ValueError("semantic evidence trajectory_range does not identify a readable sampled range")
            contact_event_id = ref["contact_event_id"]
            if contact_event_id is not None:
                if contact_event_id not in contacts:
                    raise ValueError("semantic evidence contact_event_id does not exist")
                if artifact_kind not in result_kinds:
                    raise ValueError("semantic evidence contact_event_id is incompatible with the referenced artifact")
                if time_s is not None and not math.isclose(
                    float(time_s), float(contacts[contact_event_id]["time_s"]), rel_tol=0.0, abs_tol=1e-6
                ):
                    raise ValueError("semantic evidence contact_event_id does not match its time_s")
            if artifact_kind not in result_kinds and any(
                ref[field] is not None for field in ("time_s", "view_id", "trajectory_range", "contact_event_id")
            ):
                raise ValueError("semantic evidence locator is incompatible with the referenced artifact")
            if artifact_kind == "keyframe" and (time_s is None or view_id is None):
                raise ValueError("keyframe evidence requires its exact time_s and view_id")
            if artifact_kind == "multi_view_montage" and time_s is None:
                raise ValueError("multi-view montage evidence requires its exact time_s")
        if row["status"] == "pass" and not has_result_evidence:
            raise ValueError("semantic pass requires result evidence, not only request/input snapshots")


def _parse_trajectory_range(value: str) -> tuple[float, float] | None:
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*s?\s*-\s*(\d+(?:\.\d+)?)\s*s?\s*",
        value,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    start, end = float(match.group(1)), float(match.group(2))
    if not math.isfinite(start) or not math.isfinite(end):
        return None
    return start, end


def _state_vector(value: Any, label: str) -> None:
    if value is None:
        return
    data = _mapping(value, label)
    _exact(data, {"field", "values"}, label)
    _nonempty(data["field"], f"{label}.field")
    values = data["values"]
    if not isinstance(values, list) or not 1 <= len(values) <= 4:
        raise ValueError(f"{label}.values must contain one to four numbers")
    for index, item in enumerate(values):
        _number(item, f"{label}.values[{index}]")


def _state_fields(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    prior = ""
    for index, item in enumerate(value):
        data = _mapping(item, f"{label}[{index}]")
        _exact(data, {"field", "value"}, f"{label}[{index}]")
        _nonempty(data["field"], f"{label}[{index}].field")
        if data["field"] <= prior:
            raise ValueError(f"{label} fields must be unique and sorted")
        prior = data["field"]
        scalar = data["value"]
        if isinstance(scalar, float) and not math.isfinite(scalar):
            raise ValueError(f"{label}[{index}].value must be finite")
        if not isinstance(scalar, (str, int, float, bool)) and scalar is not None:
            raise ValueError(f"{label}[{index}].value must be a JSON scalar")
