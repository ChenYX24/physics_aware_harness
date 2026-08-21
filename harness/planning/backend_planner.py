from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from harness.core.capability import canonical_capability_id
from harness.core.physics_contract import (
    allowed_backends_for_scene,
    default_backend_for_scene,
    execution_capability_id,
    infer_scene_domain,
)
from harness.core.case_spec_v2 import BACKEND_SOLVER_CAPABILITIES, CaseSpecV2
from harness.runtime.stage_contracts import stage_handoff_contract


EXECUTION_BACKENDS = {"fallback", "genesis_fem", "genesis_sph", "taichi_cloth", "ue"}
PHYSICS_EVIDENCE_OUTPUTS = {"contact_events", "declared_measurements"}


@dataclass(frozen=True)
class BackendPlanningError(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def plan_backend(
    runtime_case_spec: Mapping[str, Any],
    *,
    source_case_spec: CaseSpecV2,
    requested_backend: str | None = None,
) -> dict[str, Any]:
    source_capability_id = canonical_capability_id(str(runtime_case_spec.get("capability_id") or ""))
    capability_id = execution_capability_id(runtime_case_spec)
    scene_domain = infer_scene_domain(runtime_case_spec)
    constraints = (
        source_case_spec.data.get("backend_constraints")
        if isinstance(source_case_spec.data.get("backend_constraints"), dict)
        else {}
    )
    requested = normalize_backend(requested_backend) if requested_backend else None
    allowed = {
        normalize_backend(value)
        for value in constraints.get("allowed_solvers") or []
        if str(value).strip()
    }
    required_solver_capabilities = {
        str(value).strip()
        for value in constraints.get("required_solver_capabilities") or []
        if str(value).strip()
    }
    if requested == "auto":
        requested = None
    if requested and requested not in EXECUTION_BACKENDS:
        raise BackendPlanningError("unsupported_backend", f"backend is not registered: {requested_backend}")
    if requested and allowed and requested not in allowed:
        raise BackendPlanningError(
            "backend_constraint_conflict",
            f"requested backend {requested} is not in allowed_solvers={sorted(allowed)}",
        )
    selected = requested or _default_backend(runtime_case_spec, allowed, required_solver_capabilities)
    supported = allowed_backends_for_scene(runtime_case_spec)
    if selected not in supported:
        raise BackendPlanningError(
            "unsupported_scene_backend",
            f"scene domain {scene_domain} cannot execute on {selected}; supported={sorted(supported)}",
        )
    if allowed and selected not in allowed:
        raise BackendPlanningError(
            "no_legal_backend",
            f"no registered backend satisfies allowed_solvers={sorted(allowed)} for scene domain {scene_domain}",
        )
    missing_solver_capabilities = sorted(
        required_solver_capabilities - BACKEND_SOLVER_CAPABILITIES.get(selected, set())
    )
    if missing_solver_capabilities:
        raise BackendPlanningError(
            "unsupported_solver_capabilities",
            f"backend {selected} does not provide required solver capabilities: {missing_solver_capabilities}",
        )
    # Solvers remain their own capture backend unless the declarative contract
    # requests a separate renderer. Compatibility is decided by versioned
    # artifact I/O, never by a named process or a backend-pair allowlist.
    render_backend = normalize_backend(constraints.get("render_backend")) if constraints.get("render_backend") else selected
    multi_backend = render_backend != selected
    if multi_backend and constraints.get("allow_multi_backend") is False:
        raise BackendPlanningError(
            "multi_backend_disallowed",
            f"solver {selected} and render backend {render_backend} require a multi-backend plan",
        )
    handoff_contract = stage_handoff_contract(selected, render_backend) if multi_backend else None
    stages = _stages(selected, render_backend, handoff_contract=handoff_contract)
    staged_execution_supported = handoff_contract is not None
    return {
        "schema_version": "harness_backend_selection_v1",
        "capability_id": capability_id,
        "source_capability_id": source_capability_id,
        "scene_domain": scene_domain,
        "required_capabilities": sorted(required_solver_capabilities),
        "provided_solver_capabilities": sorted(BACKEND_SOLVER_CAPABILITIES[selected]),
        "required_case_capabilities": [
            canonical_capability_id(str(value))
            for value in ((source_case_spec.data.get("capabilities") or {}).get("required") or [])
        ],
        "selected_backend": selected,
        "solver_backend": selected,
        "render_backend": render_backend,
        "multi_backend": multi_backend,
        "selection_policy": "case_constraints_then_scene_domain_v1",
        "selection_reason": (
            "explicit_runtime_override"
            if requested
            else "allowed_solver_constraint"
            if allowed
            else "scene_domain_default"
        ),
        "target_asset_backend": "unreal" if render_backend == "ue" else render_backend,
        "handoff_contract": handoff_contract,
        "stages": stages,
        "runtime_plan_required": multi_backend,
        "execution_supported": not multi_backend or staged_execution_supported,
        "execution_blocker": (
            "multi_backend_handoff_contract_unavailable"
            if multi_backend and not staged_execution_supported
            else None
        ),
        "fallback_is_reference_truth": False,
        "legacy_policy": None,
    }


def normalize_backend(value: Any) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        "unreal": "ue",
        "unreal_engine": "ue",
        "ue_chaos": "ue",
        "ue_chaos_initial_state": "ue",
        "ue_chaos_destruction": "ue",
        "genesis": "genesis_sph",
        "taichi": "taichi_cloth",
    }
    return aliases.get(normalized, normalized)


def _default_backend(case_spec: Mapping[str, Any], allowed: set[str], required_capabilities: set[str]) -> str:
    preferred = default_backend_for_scene(case_spec)
    supported = allowed_backends_for_scene(case_spec)
    candidates = EXECUTION_BACKENDS.intersection(allowed or EXECUTION_BACKENDS).intersection(supported)
    capable = {
        backend
        for backend in candidates
        if required_capabilities.issubset(BACKEND_SOLVER_CAPABILITIES.get(backend, set()))
    }
    if preferred in capable:
        return preferred
    if not capable:
        raise BackendPlanningError(
            "no_legal_backend",
            f"no backend satisfies allowed_solvers={sorted(allowed)} and required_solver_capabilities={sorted(required_capabilities)}",
        )
    return sorted(capable)[0]


def _stages(
    solver_backend: str,
    render_backend: str,
    *,
    handoff_contract: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    evidence_outputs = sorted(
        PHYSICS_EVIDENCE_OUTPUTS.intersection(BACKEND_SOLVER_CAPABILITIES.get(solver_backend, set()))
    )
    if solver_backend == render_backend:
        return [
            {
                "id": "solve_render" if solver_backend == "ue" else "solve_capture",
                "kind": "solve_render",
                "backend": solver_backend,
                "inputs": ["asset_resolution", "scene_layout", "runtime_actor_placement"],
                "outputs": ["trajectory", "render_artifacts", "signals", *evidence_outputs],
            }
        ]
    cache_kind = str((handoff_contract or {}).get("contract_id") or "unsupported_state_handoff")
    return [
        {
            "id": "solve",
            "kind": "solve",
            "backend": solver_backend,
            "inputs": ["asset_resolution", "scene_layout"],
            "outputs": [cache_kind, "trajectory", "signals", *evidence_outputs],
            "handoff_contract": dict(handoff_contract) if handoff_contract is not None else None,
        },
        {
            "id": "render",
            "kind": "render",
            "backend": render_backend,
            "inputs": [cache_kind, "runtime_actor_placement", "observation_plan"],
            "outputs": ["render_artifacts"],
            "handoff_contract": dict(handoff_contract) if handoff_contract is not None else None,
        },
    ]
