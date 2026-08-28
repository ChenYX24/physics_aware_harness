from __future__ import annotations

from typing import Any, Mapping


SCENE_DOMAIN_BACKENDS = {
    "rigid_body": {"fallback", "ue"},
    "particle": {"fallback", "genesis_sph"},
    "deformable": {"fallback", "genesis_fem", "taichi_cloth"},
}
SCENE_DOMAIN_DEFAULT_BACKEND = {
    "rigid_body": "ue",
    "particle": "genesis_sph",
    "deformable": "taichi_cloth",
}
SCENE_DOMAIN_CAPABILITY = {
    "rigid_body": "rigid_body_dynamics",
    "particle": "fluid_particle_dynamics",
    "deformable": "deformable_body_dynamics",
}


def infer_scene_domain(case_spec: Mapping[str, Any]) -> str:
    """Infer only the state/coupling domain, never a named physical process."""
    solver_scene = case_spec.get("solver_scene") if isinstance(case_spec.get("solver_scene"), Mapping) else {}
    if solver_scene.get("type") == "rigid_sph":
        return "particle"
    objects = [item for item in case_spec.get("objects") or [] if isinstance(item, Mapping)]
    roles = {str(item.get("role") or "").casefold() for item in objects}
    if any(role in {"fluid", "fluid_volume"} or "fluid" in role for role in roles):
        return "particle"
    for item in objects:
        role = str(item.get("role") or "").casefold()
        physics = item.get("physics") if isinstance(item.get("physics"), Mapping) else {}
        material_model = str(physics.get("material_model") or "").casefold()
        if any(token in role for token in ("soft_body", "deformable", "cloth")) or material_model in {
            "fem",
            "cloth",
            "deformable",
        }:
            return "deformable"
    return "rigid_body"


def execution_capability_id(case_spec: Mapping[str, Any]) -> str:
    return SCENE_DOMAIN_CAPABILITY[infer_scene_domain(case_spec)]


def allowed_backends_for_scene(case_spec: Mapping[str, Any]) -> set[str]:
    return set(SCENE_DOMAIN_BACKENDS[infer_scene_domain(case_spec)])


def default_backend_for_scene(case_spec: Mapping[str, Any]) -> str:
    return SCENE_DOMAIN_DEFAULT_BACKEND[infer_scene_domain(case_spec)]
