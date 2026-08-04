from __future__ import annotations

import copy
from typing import Any, Mapping

from harness.core.capability import CapabilityStore, canonical_capability_id
from harness.core.case_spec_v2 import CaseSpecV2


VERIFIER_BY_CAPABILITY = {
    "rigid_body_contact_causality": "contact_causality_verifier",
    "sequential_contact_propagation": "domino_verifier",
    "rigid_body_gravity_collision": "falling_verifier",
    "ramp_sliding_friction": "ramp_verifier",
    "projectile_gravity_motion": "projectile_verifier",
    "bounce_restitution_ball": "bounce_verifier",
    "rolling_friction_ball": "rolling_verifier",
    "sliding_crate_friction": "sliding_verifier",
    "force_field_wind_drift": "wind_verifier",
    "magnetic_force_field": "magnetic_verifier",
    "mass_ratio_momentum_transfer": "mass_ratio_verifier",
    "angular_damping_spin_decay": "spin_verifier",
    "agent_rigidbody_action_coupling": "agent_action_verifier",
    "constraint_distance_pendulum_motion": "constraint_verifier",
    "constraint_momentum_transfer": "impulse_chain_verifier",
    "elastic_energy_launch": "elastic_launch_verifier",
    "elastic_constraint_rebound": "elastic_constraint_verifier",
    "brittle_impact_fracture": "brittle_fracture_verifier",
    "fluid_particle_dynamics": "particle_cache_verifier",
    "soft_body_deformation": "deformable_mesh_cache_verifier",
}
DEFAULT_ASSERTION_BY_CAPABILITY = {
    "rigid_body_contact_causality": "contact_causality",
    "sequential_contact_propagation": "ordered_contact_propagation",
    "rigid_body_gravity_collision": "gravity_then_support_contact",
    "ramp_sliding_friction": "frictional_ramp_motion",
    "projectile_gravity_motion": "ballistic_gravity_impact",
    "bounce_restitution_ball": "restitution_envelope",
    "rolling_friction_ball": "rolling_deceleration",
    "sliding_crate_friction": "sliding_deceleration",
    "force_field_wind_drift": "displacement_along_force",
    "magnetic_force_field": "magnetic_response",
    "mass_ratio_momentum_transfer": "momentum_transfer",
    "angular_damping_spin_decay": "angular_speed_decay",
    "agent_rigidbody_action_coupling": "action_effect_occurs",
    "constraint_distance_pendulum_motion": "constraint_preserved",
    "constraint_momentum_transfer": "constraint_impulse_transfer",
    "elastic_energy_launch": "elastic_launch_occurs",
    "elastic_constraint_rebound": "elastic_rebound_occurs",
    "brittle_impact_fracture": "fracture_after_contact",
    "fluid_particle_dynamics": "particle_cache_complete",
    "soft_body_deformation": "mesh_cache_complete",
}
ASSERTION_EVIDENCE = {
    "contact_occurs": ["trajectory", "contact_events"],
    "contact_causality": ["trajectory", "contact_events"],
    "ordered_contact_propagation": ["trajectory", "contact_events"],
    "fracture_after_contact": ["trajectory", "contact_events", "fracture_events", "fragment_manifest"],
    "particle_cache_complete": ["particle_cache", "surface_mesh_cache"],
    "mesh_cache_complete": ["mesh_cache", "trajectory"],
    "action_effect_occurs": ["trajectory", "action_trace", "contact_events"],
}


def compile_verification_plan(
    runtime_case_spec: Mapping[str, Any],
    *,
    source_case_spec: CaseSpecV2 | None = None,
) -> dict[str, Any]:
    capability_id = canonical_capability_id(str(runtime_case_spec.get("capability_id") or ""))
    capability = CapabilityStore().get(capability_id)
    requirements = (
        source_case_spec.data.get("verification_requirements")
        if source_case_spec and isinstance(source_case_spec.data.get("verification_requirements"), dict)
        else {}
    )
    assertions = [
        copy.deepcopy(item)
        for item in requirements.get("assertions") or []
        if isinstance(item, dict) and item.get("type")
    ]
    if not assertions:
        assertions = _legacy_or_default_assertions(runtime_case_spec, capability_id)
    evidence_signals = list(capability.required_signals)
    for assertion in assertions:
        evidence_signals.extend(ASSERTION_EVIDENCE.get(str(assertion.get("type")), ["trajectory"]))
    evidence_signals = list(dict.fromkeys(str(value) for value in evidence_signals if value))
    camera_roles = _camera_evidence_roles(assertions)
    modalities = ["rgb"] if camera_roles else []
    verifier_id = VERIFIER_BY_CAPABILITY.get(capability_id)
    unsupported = verifier_id is None
    return {
        "schema_version": "harness_verification_plan_v1",
        "case_id": runtime_case_spec.get("case_id"),
        "capability_id": capability_id,
        "assertion_vocabulary": "existing_physics_verifier_v1",
        "assertions": assertions,
        "verifiers": []
        if unsupported
        else [
            {
                "id": verifier_id,
                "implementation": f"harness.verification.{verifier_id}",
                "version": "v1",
                "capability_id": capability_id,
                "thresholds": copy.deepcopy(requirements.get("thresholds") or {}),
                "time_window": copy.deepcopy(requirements.get("time_window") or {}),
            }
        ],
        "evidence_requirements": {
            "signals": evidence_signals,
            "modalities": modalities,
            "camera_roles": camera_roles,
            "synchronization": "shared_sim_time",
        },
        "status": "unsupported" if unsupported else "ready",
        "failure_code": "unsupported_verifier_capability" if unsupported else None,
    }


def _legacy_or_default_assertions(
    case_spec: Mapping[str, Any],
    capability_id: str,
) -> list[dict[str, Any]]:
    rules = [str(value) for value in case_spec.get("verification_rules") or [] if value]
    if rules:
        return [{"type": value, "source": "legacy_verification_rule"} for value in rules]
    assertion_type = DEFAULT_ASSERTION_BY_CAPABILITY.get(capability_id)
    return [{"type": assertion_type, "source": "capability_default"}] if assertion_type else []


def _camera_evidence_roles(assertions: list[dict[str, Any]]) -> list[str]:
    assertion_types = {str(item.get("type") or "") for item in assertions}
    roles: list[str] = []
    if assertion_types.intersection({"contact_occurs", "contact_causality", "ordered_contact_propagation"}):
        roles.append("event_closeup")
    if "fracture_after_contact" in assertion_types:
        roles.extend(["event_closeup", "side_static"])
    return list(dict.fromkeys(roles))
