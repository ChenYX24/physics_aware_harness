from __future__ import annotations

import copy
from typing import Any, Mapping

from harness.core.case_spec_v2 import CaseSpecV2
from harness.core.physics_contract import execution_capability_id, infer_scene_domain


GENERIC_ASSERTION_TYPES = {
    "artifact_complete",
    "event_count",
    "event_exists",
    "event_sequence",
    "state_delta",
    "state_value",
    "trajectory_integrity",
}
ASSERTION_EVIDENCE = {
    "artifact_complete": ["trajectory"],
    "event_count": ["trajectory", "contact_events"],
    "event_exists": ["trajectory", "contact_events"],
    "event_sequence": ["trajectory", "contact_events"],
    "state_delta": ["trajectory"],
    "state_value": ["trajectory"],
    "trajectory_integrity": ["trajectory"],
}


def compile_verification_plan(
    runtime_case_spec: Mapping[str, Any],
    *,
    source_case_spec: CaseSpecV2 | None = None,
) -> dict[str, Any]:
    domain = infer_scene_domain(runtime_case_spec)
    execution_capability = execution_capability_id(runtime_case_spec)
    requirements = (
        source_case_spec.data.get("verification_requirements")
        if source_case_spec and isinstance(source_case_spec.data.get("verification_requirements"), dict)
        else {}
    )
    source_assertions = requirements.get("assertions") if source_case_spec else runtime_case_spec.get("verification_assertions")
    assertions = [copy.deepcopy(item) for item in source_assertions or [] if isinstance(item, dict)]
    if domain == "rigid_body" and not assertions:
        assertions = [{"id": "trajectory_integrity", "type": "trajectory_integrity"}]
    unsupported_assertions = [
        str(item.get("type") or "")
        for item in assertions
        if str(item.get("type") or "") not in GENERIC_ASSERTION_TYPES
    ]
    verifier_id = {
        "rigid_body": "trajectory_assertion_verifier",
        "particle": "particle_cache_verifier",
        "deformable": "deformable_mesh_cache_verifier",
    }[domain]
    evidence_signals: list[str] = []
    for assertion in assertions:
        evidence_signals.extend(ASSERTION_EVIDENCE.get(str(assertion.get("type") or ""), []))
    if domain == "particle":
        evidence_signals.extend(["particle_cache", "surface_mesh_cache"])
    elif domain == "deformable":
        evidence_signals.extend(["mesh_cache", "trajectory"])
    evidence_signals = list(dict.fromkeys(evidence_signals))
    camera_roles = _camera_evidence_roles(assertions)
    return {
        "schema_version": "harness_verification_plan_v1",
        "case_id": runtime_case_spec.get("case_id"),
        "capability_id": execution_capability,
        "source_capability_id": runtime_case_spec.get("capability_id"),
        "scene_domain": domain,
        "assertion_vocabulary": "generic_state_event_assertions_v1",
        "assertions": assertions,
        "verifiers": [
            {
                "id": verifier_id,
                "implementation": f"harness.verification.{verifier_id}",
                "version": "v1",
                "scene_domain": domain,
            }
        ],
        "evidence_requirements": {
            "signals": evidence_signals,
            "modalities": ["rgb"] if camera_roles else [],
            "camera_roles": camera_roles,
            "synchronization": "shared_sim_time",
        },
        "status": "unsupported" if unsupported_assertions else "ready",
        "failure_code": "unsupported_generic_assertion" if unsupported_assertions else None,
        "unsupported_assertions": unsupported_assertions,
    }


def _camera_evidence_roles(assertions: list[dict[str, Any]]) -> list[str]:
    types = {str(item.get("type") or "") for item in assertions}
    return ["event_closeup"] if types.intersection({"event_count", "event_exists", "event_sequence"}) else []
