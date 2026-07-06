from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PHYSICS_CRITICAL_ROLES = {
    "active_striker",
    "passive_target",
    "falling_body",
    "stack_block",
    "domino",
    "support",
    "floor",
    "ground",
    "rigid_body",
    "rolling_subject",
    "sliding_subject",
    "ramp_subject",
    "ramp",
    "slope_surface",
    "inclined_plane",
    "constrained_body",
    "constraint_anchor",
    "projectile",
    "thrown_body",
    "launched_body",
    "bouncing_body",
    "restitution_subject",
    "bounce_subject",
    "rolling_body",
    "friction_subject",
}


@dataclass(frozen=True)
class AssetIntent:
    object_id: str
    role: str
    query: str
    category: str
    physics_critical: bool
    required_properties: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "role": self.role,
            "query": self.query,
            "category": self.category,
            "physics_critical": self.physics_critical,
            "required_properties": self.required_properties,
        }


def intent_from_object(obj: dict[str, Any]) -> AssetIntent:
    role = str(obj.get("role") or "").strip() or "visual_object"
    query = str(obj.get("asset_query") or obj.get("shape") or obj.get("id") or role)
    category = classify_asset_role(role)
    physics_critical = category == "physics_critical"
    required = ["collider", "mass", "rigid_body", "collision_profile"] if physics_critical else ["visual_proxy"]
    return AssetIntent(
        object_id=str(obj.get("id") or query),
        role=role,
        query=query,
        category=category,
        physics_critical=physics_critical,
        required_properties=required,
    )


def classify_asset_role(role: str) -> str:
    normalized = role.casefold().replace("-", "_").replace(" ", "_")
    if any(term in normalized for term in PHYSICS_CRITICAL_ROLES):
        return "physics_critical"
    if any(term in normalized for term in ("texture", "material", "decal", "vfx", "visual")):
        return "visual_only"
    if any(term in normalized for term in ("skeleton", "skeletal", "animation", "ik")):
        return "skeletal_animation"
    return "visual_only"
