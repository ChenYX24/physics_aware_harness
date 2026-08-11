from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
    shape = str(obj.get("shape") or "").strip()
    query = str(obj.get("asset_query") or " ".join(part for part in (role, shape) if part) or obj.get("id") or role)
    category = classify_asset_role(" ".join(part for part in (role, shape, str(obj.get("asset_type") or ""), query) if part))
    body_type = str(obj.get("body_type") or "").casefold()
    collision_required = obj.get("collision_required")
    if body_type == "dynamic" or collision_required is True:
        category = "physics_critical"
    physics_critical = category == "physics_critical"
    physics_required = ["mass", "rigid_body"]
    if collision_required is not False:
        physics_required = ["collider", *physics_required, "collision_profile"]
    required = {
        "physics_critical": physics_required,
        "scene_map": ["map_package", "dependencies", "preview_presets"],
        "blueprint_logic": ["owner_asset", "dependencies", "callable_functions"],
        "skeletal_animation": ["skeleton", "animation_compatibility"],
    }.get(category, ["visual_proxy"])
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
    tokens = {token for token in normalized.split("_") if token}
    if tokens.intersection({"map", "scene", "level", "environment", "world"}):
        return "scene_map"
    if tokens.intersection({"blueprint", "callable", "logic", "interface"}) or "function_library" in normalized:
        return "blueprint_logic"
    if tokens.intersection({"texture", "material", "decal", "vfx", "visual"}):
        return "visual_only"
    if tokens.intersection({"skeleton", "skeletal", "animation", "ik"}):
        return "skeletal_animation"
    return "physics_critical"
