from __future__ import annotations

from pathlib import Path
from typing import Any


DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[1] / "config" / "harness_capability_profile.json"

PIPELINE_STAGE_CAPABILITIES: tuple[dict[str, str], ...] = (
    {"stage": "planning", "capability_id": "prompt_case_capability_planning"},
    {"stage": "asset_resolution", "capability_id": "asset_intent_resolution"},
    {"stage": "scene_compilation", "capability_id": "scene_spec_compilation"},
    {"stage": "runtime_execution", "capability_id": "runtime_backend_execution"},
    {"stage": "verification", "capability_id": "physics_verifier_truth_gate"},
)
PARTICLE_TERMS = ("fluid", "liquid", "water", "sph", "流体", "液体", "水")
DEFORMABLE_TERMS = ("cloth", "soft body", "deformable", "fabric", "布", "软体", "可变形")


class CapabilityPlanner:
    """Compatibility facade that identifies only a solver/state domain.

    Natural-language process classification was intentionally removed. The
    CaseSpec generator declares objects and primitives; deterministic planning
    chooses a backend from those declarations.
    """

    def __init__(self, profile_path: str | Path | None = None) -> None:
        self.profile_path = Path(profile_path) if profile_path else None

    def plan(self, prompt: str) -> dict[str, Any]:
        normalized = " ".join(str(prompt).casefold().split())
        if any(term in normalized for term in PARTICLE_TERMS):
            primary = "fluid_particle_dynamics"
            domain = "particle"
            preferred_runtime = "GenesisSPH"
            solver_capabilities = ["particle_dynamics", "particle_cache", "surface_mesh_cache"]
        elif any(term in normalized for term in DEFORMABLE_TERMS):
            primary = "deformable_body_dynamics"
            domain = "deformable"
            preferred_runtime = "TaichiCloth"
            solver_capabilities = ["soft_body", "mesh_cache"]
        else:
            primary = "rigid_body_dynamics"
            domain = "rigid_body"
            preferred_runtime = "UE"
            solver_capabilities = ["rigid_body", "trajectory"]
        pipeline = [dict(item) for item in PIPELINE_STAGE_CAPABILITIES]
        all_ids = [primary, *[item["capability_id"] for item in pipeline]]
        return {
            "schema_version": "capability_plan_v2",
            "prompt": prompt,
            "scene_domain": domain,
            "case_family": domain,
            "primary_capability_id": primary,
            "matched_capabilities": [{"capability_id": primary, "reason": "state_representation_domain"}],
            "supporting_capabilities": [item["capability_id"] for item in pipeline],
            "required_solver_capabilities": solver_capabilities,
            "capability_layers": {
                "primary_physics": [primary],
                "pipeline_stages": pipeline,
                "all_capability_ids": all_ids,
            },
            "execution_strategy": {
                "preferred_runtime": preferred_runtime,
                "fallback_runtime": "contract_only",
                "dry_run_supported": True,
                "requires_trajectory": True,
                "requires_contact_events": False,
            },
        }

    def match(self, prompt: str) -> list[dict[str, Any]]:
        return list(self.plan(prompt)["matched_capabilities"])
