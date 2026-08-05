from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from harness.core.capability import canonical_capability_id
from harness.core.case_spec_v2 import BACKEND_SOLVER_CAPABILITIES, CaseSpecV2
from harness.runtime.backend_policy import backend_plan as legacy_backend_policy


EXECUTION_BACKENDS = {"fallback", "genesis_fem", "genesis_sph", "taichi_cloth", "ue"}
CAPABILITY_DEFAULT_BACKEND = {
    "fluid_particle_dynamics": "genesis_sph",
    "soft_body_deformation": "taichi_cloth",
}
CAPABILITY_BACKEND_RESTRICTIONS = {
    "fluid_particle_dynamics": {"fallback", "genesis_sph"},
    "soft_body_deformation": {"fallback", "genesis_fem", "taichi_cloth"},
}
DEFAULT_CAPABILITY_BACKENDS = {"fallback", "ue"}
@dataclass(frozen=True)
class BackendPlanningError(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def plan_backend(
    runtime_case_spec: Mapping[str, Any],
    *,
    source_case_spec: CaseSpecV2 | None = None,
    requested_backend: str | None = None,
) -> dict[str, Any]:
    capability_id = canonical_capability_id(str(runtime_case_spec.get("capability_id") or ""))
    constraints = (
        source_case_spec.data.get("backend_constraints")
        if source_case_spec and isinstance(source_case_spec.data.get("backend_constraints"), dict)
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
    selected = requested or _default_backend(capability_id, allowed, required_solver_capabilities)
    supported = CAPABILITY_BACKEND_RESTRICTIONS.get(capability_id, DEFAULT_CAPABILITY_BACKENDS)
    if selected not in supported:
        raise BackendPlanningError(
            "unsupported_capability_backend",
            f"{capability_id} cannot execute on {selected}; supported={sorted(supported)}",
        )
    if allowed and selected not in allowed:
        raise BackendPlanningError(
            "no_legal_backend",
            f"no registered backend satisfies allowed_solvers={sorted(allowed)} for {capability_id}",
        )
    missing_solver_capabilities = sorted(
        required_solver_capabilities - BACKEND_SOLVER_CAPABILITIES.get(selected, set())
    )
    if missing_solver_capabilities:
        raise BackendPlanningError(
            "unsupported_solver_capabilities",
            f"backend {selected} does not provide required solver capabilities: {missing_solver_capabilities}",
        )
    # A solver remains its own capture backend unless V2 explicitly requests a
    # separate renderer. This preserves the existing standalone Genesis/Taichi
    # execution paths and creates a staged runtime_plan only for an intentional
    # multi-backend case.
    render_backend = normalize_backend(constraints.get("render_backend")) if constraints.get("render_backend") else selected
    multi_backend = render_backend != selected
    if multi_backend and constraints.get("allow_multi_backend") is False:
        raise BackendPlanningError(
            "multi_backend_disallowed",
            f"solver {selected} and render backend {render_backend} require a multi-backend plan",
        )
    stages = _stages(selected, render_backend)
    legacy_policy = legacy_backend_policy(capability_id)
    return {
        "schema_version": "harness_backend_selection_v1",
        "capability_id": capability_id,
        "required_capabilities": sorted(required_solver_capabilities),
        "provided_solver_capabilities": sorted(BACKEND_SOLVER_CAPABILITIES[selected]),
        "required_case_capabilities": [
            canonical_capability_id(str(value))
            for value in ((source_case_spec.data.get("capabilities") or {}).get("required") or [])
        ] if source_case_spec else [capability_id],
        "selected_backend": selected,
        "solver_backend": selected,
        "render_backend": render_backend,
        "multi_backend": multi_backend,
        "selection_policy": "case_constraints_then_capability_registry_v1",
        "selection_reason": (
            "explicit_runtime_override"
            if requested
            else "allowed_solver_constraint"
            if allowed
            else "capability_default"
        ),
        "target_asset_backend": "unreal" if render_backend == "ue" else render_backend,
        "stages": stages,
        "runtime_plan_required": multi_backend,
        "execution_supported": not multi_backend,
        "execution_blocker": (
            "multi_backend_stage_executor_not_implemented"
            if multi_backend
            else None
        ),
        "fallback_is_reference_truth": False,
        "legacy_policy": legacy_policy,
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


def _default_backend(capability_id: str, allowed: set[str], required_capabilities: set[str]) -> str:
    preferred = CAPABILITY_DEFAULT_BACKEND.get(capability_id, "ue")
    supported = CAPABILITY_BACKEND_RESTRICTIONS.get(capability_id, DEFAULT_CAPABILITY_BACKENDS)
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


def _stages(solver_backend: str, render_backend: str) -> list[dict[str, Any]]:
    if solver_backend == render_backend:
        return [
            {
                "id": "solve_render" if solver_backend == "ue" else "solve_capture",
                "backend": solver_backend,
                "inputs": ["asset_resolution", "scene_layout", "runtime_actor_placement"],
                "outputs": ["trajectory", "render_artifacts", "signals"],
            }
        ]
    cache_kind = {
        "genesis_sph": "particle_surface_cache",
        "taichi_cloth": "mesh_cache",
        "genesis_fem": "deformable_mesh_cache",
    }.get(solver_backend, "state_cache")
    return [
        {
            "id": "solve",
            "backend": solver_backend,
            "inputs": ["asset_resolution", "scene_layout"],
            "outputs": [cache_kind, "trajectory", "signals"],
        },
        {
            "id": "render",
            "backend": render_backend,
            "inputs": [cache_kind, "runtime_actor_placement", "observation_plan"],
            "outputs": ["render_artifacts"],
        },
    ]
