from __future__ import annotations

from typing import Any

from harness.core.capability import canonical_capability_id


def policy(preferred_backend: str, status: str, coupling_contract: str, *, validation_backend: str | None = None) -> dict[str, Any]:
    return {
        "preferred_backend": preferred_backend,
        "status": status,
        "coupling_contract": coupling_contract,
        "validation_backend": validation_backend,
        "renderer": "unreal_engine",
        "fallback_is_reference_truth": False,
    }


BACKEND_POLICIES: dict[str, dict[str, Any]] = {
    "rigid_body_dynamics": policy("ue", "validated", "rigid_transforms_contacts_constraints"),
    "fluid_particle_dynamics": policy("genesis_sph", "prototype_validated", "particles_to_surface_cache", validation_backend="sphinxsys"),
    "deformable_body_dynamics": policy("taichi_cloth", "prototype_validated", "vertices_to_fixed_topology_mesh_cache"),
}


def backend_plan(capability_id: str) -> dict[str, Any]:
    canonical = canonical_capability_id(capability_id)
    selected = BACKEND_POLICIES.get(canonical)
    if selected is None:
        return {
            "capability_id": canonical,
            "preferred_backend": None,
            "status": "unsupported",
            "coupling_contract": None,
            "validation_backend": None,
            "renderer": "unreal_engine",
            "fallback_is_reference_truth": False,
        }
    return {"capability_id": canonical, **selected}
