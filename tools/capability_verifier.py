from __future__ import annotations

from typing import Any

from harness.verification.trajectory_assertion_verifier import verify_trajectory_assertions
from tools.failure_taxonomy import failure_record, first_failure_type


class CapabilityVerifier:
    """Compatibility report facade over the generic trajectory assertion verifier.

    The verifier deliberately has no knowledge of named physical processes. A
    case must declare the events and state relations it expects to observe.
    """

    def verify(self, capability_plan: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
        schema_failures = self._schema_failures(capability_plan, execution)
        trajectory = execution.get("trajectory") if isinstance(execution.get("trajectory"), list) else []
        assertion_spec = {
            "verification_assertions": execution.get("verification_assertions")
            or capability_plan.get("verification_assertions")
            or [{"id": "trajectory_integrity", "type": "trajectory_integrity"}]
        }
        failure_type, counterexample, evidence = verify_trajectory_assertions(assertion_spec, trajectory)
        assertion_failures = []
        if failure_type:
            assertion_failures.append(
                failure_record(
                    "F4_causality_violation" if failure_type == "declared_assertion_failed" else "F5_weak_visual_evidence",
                    failure_type,
                    evidence=counterexample,
                )
            )
        render_failures = self._render_failures(execution)
        failures = [*schema_failures, *assertion_failures, *render_failures]
        layers = {
            "schema_validity": {"passed": not schema_failures, "failures": schema_failures},
            "declared_assertions": {"passed": not assertion_failures, "failures": assertion_failures, "evidence": evidence},
            "render_evidence_validity": {"passed": not render_failures, "failures": render_failures},
        }
        ready = not failures
        render_evidence = execution.get("render_evidence") if isinstance(execution.get("render_evidence"), dict) else {}
        reference_ready = bool(render_evidence.get("video_available") and ready)
        return {
            "schema_version": "capability_verifier_report_v2",
            "case_id": execution.get("case_id"),
            "capability_ids": capability_plan.get("matched_capabilities", []),
            "scene_domain": capability_plan.get("scene_domain"),
            "assertion_vocabulary": "generic_state_event_assertions_v1",
            "capability_ready": ready,
            "reference_video_ready": reference_ready,
            "artifact_tier": "reference_video" if reference_ready else "simulated_trace_not_video",
            "layers": layers,
            "failure_modes": failures,
            "primary_failure_type": first_failure_type(failures),
            "diagnosis": self._diagnosis(failures),
        }

    @staticmethod
    def _schema_failures(capability_plan: dict[str, Any], execution: dict[str, Any]) -> list[dict[str, Any]]:
        failures = []
        if capability_plan.get("schema_version") not in {"capability_plan_v1", "capability_plan_v2"}:
            failures.append(failure_record("F1_scene_parsing_failure", "capability plan schema_version is invalid"))
        if execution.get("schema_version") != "capability_execution_trace_v1":
            failures.append(failure_record("F1_scene_parsing_failure", "execution trace schema_version is invalid"))
        if not isinstance(execution.get("objects"), list) or not execution.get("objects"):
            failures.append(failure_record("F1_scene_parsing_failure", "execution trace has no objects"))
        return failures

    @staticmethod
    def _render_failures(execution: dict[str, Any]) -> list[dict[str, Any]]:
        evidence = execution.get("render_evidence") if isinstance(execution.get("render_evidence"), dict) else {}
        failures = []
        if evidence.get("runtime_status") == "failed":
            failures.append(failure_record("F6_runtime_or_render_failure", "runtime backend reported failure"))
        if not evidence.get("trajectory_available"):
            failures.append(failure_record("F5_weak_visual_evidence", "trajectory evidence is missing"))
        if evidence.get("source_type") == "VISUAL_ONLY":
            failures.append(failure_record("F5_weak_visual_evidence", "visual-only animation cannot verify declared physics assertions"))
        return failures

    @staticmethod
    def _diagnosis(failures: list[dict[str, Any]]) -> dict[str, Any]:
        if not failures:
            return {"root_cause": "none", "repair_suggestion": "declared generic assertions passed"}
        first = failures[0]
        return {
            "root_cause": first.get("reason"),
            "repair_suggestion": "inspect the declared assertion and its trajectory/event evidence",
        }
