from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from harness.assets.asset_intent import AssetIntent


ALLOWED_MUST_FIELDS = {
    "approx_size_m",
    "asset_type",
    "backend",
    "category",
    "class_name",
    "collision",
    "geometry_type",
    "license_tier",
    "materialized",
    "physics_role",
    "real_3d_geometry",
    "runtime_ready",
    "source_kind",
}
ALLOWED_MUST_NOT_FIELDS = {
    "asset_type",
    "backend",
    "category",
    "class_name",
    "geometry_type",
    "license_tier",
    "source_kind",
}
PHYSICS_ROLE_ALIASES = {
    "dynamic": "dynamic_rigid_body",
    "static": "static_rigid_body",
    "kinematic": "kinematic_rigid_body",
}


def acceptable_license_tiers(required: Any) -> set[str]:
    """Return tiers that satisfy a minimum publication/use clearance."""
    raw_values = required if isinstance(required, list) else [required]
    accepted: set[str] = set()
    for raw_value in raw_values:
        value = str(raw_value).strip().casefold()
        if not value:
            continue
        accepted.add(value)
        if value == "local_preview":
            accepted.add("reference")
    return accepted


def license_tier_satisfies(actual: Any, required: Any) -> bool:
    if required is None:
        return True
    return str(actual or "").strip().casefold() in acceptable_license_tiers(required)


@dataclass(frozen=True)
class SearchPreference:
    field: str
    value: Any
    weight: float = 1.0
    confidence: float = 1.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SearchPreference:
        name = str(data.get("field") or "").strip()
        if not name:
            raise ValueError("SearchIntent should entries require a non-empty field")
        weight = float(data.get("weight", 1.0))
        confidence = float(data.get("confidence", 1.0))
        if weight < 0.0:
            raise ValueError("SearchIntent should weight must be non-negative")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("SearchIntent should confidence must be between 0 and 1")
        return cls(field=name, value=data.get("value"), weight=weight, confidence=confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "weight": self.weight,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class SearchIntent:
    raw_query: str
    taxonomy: dict[str, str] = field(default_factory=dict)
    must: dict[str, Any] = field(default_factory=dict)
    should: tuple[SearchPreference, ...] = ()
    must_not: dict[str, Any] = field(default_factory=dict)
    semantic_text: str = ""
    reference_image: str | None = None
    relaxation_policy: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SearchIntent:
        raw_query = str(data.get("raw_query") or data.get("query") or data.get("semantic_text") or "").strip()
        if not raw_query:
            raise ValueError("SearchIntent requires raw_query, query, or semantic_text")
        taxonomy = _string_mapping(data.get("taxonomy"), "taxonomy")
        must = _mapping(data.get("must"), "must")
        must_not = _mapping(data.get("must_not"), "must_not")
        _reject_unknown_fields(must, ALLOWED_MUST_FIELDS, "must")
        _reject_unknown_fields(must_not, ALLOWED_MUST_NOT_FIELDS, "must_not")
        if "physics_role" in must:
            must = {**must, "physics_role": canonical_physics_role(must["physics_role"])}
        if "approx_size_m" in must:
            _validate_size_vector(must["approx_size_m"], "must.approx_size_m")
        raw_should = data.get("should") or []
        if not isinstance(raw_should, list):
            raise ValueError("SearchIntent should must be a list")
        if any(not isinstance(item, Mapping) for item in raw_should):
            raise ValueError("SearchIntent should entries must be objects")
        should = tuple(SearchPreference.from_dict(item) for item in raw_should)
        relaxation = data.get("relaxation_policy") or {}
        if not isinstance(relaxation, Mapping):
            raise ValueError("SearchIntent relaxation_policy must be an object")
        return cls(
            raw_query=raw_query,
            taxonomy=taxonomy,
            must=dict(must),
            should=should,
            must_not=dict(must_not),
            semantic_text=str(data.get("semantic_text") or raw_query).strip(),
            reference_image=str(data["reference_image"]) if data.get("reference_image") else None,
            relaxation_policy={str(key): bool(value) for key, value in relaxation.items()},
        )
    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_query": self.raw_query,
            "taxonomy": dict(self.taxonomy),
            "must": dict(self.must),
            "should": [preference.to_dict() for preference in self.should],
            "must_not": dict(self.must_not),
            "semantic_text": self.semantic_text or self.raw_query,
            "reference_image": self.reference_image,
            "relaxation_policy": dict(self.relaxation_policy),
        }


def canonical_physics_role(value: Any) -> Any:
    if isinstance(value, list):
        return [canonical_physics_role(item) for item in value]
    normalized = str(value).strip().casefold()
    return PHYSICS_ROLE_ALIASES.get(normalized, value)


def search_intent_from_asset_intent(intent: AssetIntent, *, backend: str = "unreal") -> SearchIntent:
    must: dict[str, Any] = {"backend": backend, "real_3d_geometry": True}
    if intent.physics_critical:
        must["collision"] = True
    return SearchIntent(
        raw_query=intent.query,
        taxonomy={"category": intent.category},
        must=must,
        should=(SearchPreference(field="physics_role", value=intent.role),),
        semantic_text=intent.query,
        relaxation_policy={"allow_parent_category": True, "allow_format_conversion": False},
    )


def analytic_search_intent_from_asset_intent(
    intent: AssetIntent,
    obj: Mapping[str, Any],
    *,
    backend: str = "unreal",
) -> SearchIntent:
    role = str(obj.get("role") or intent.role).strip()
    shape = str(obj.get("shape") or obj.get("collider") or "").strip()
    query = " ".join(value for value in (role, shape) if value) or intent.query
    return SearchIntent(
        raw_query=query,
        taxonomy={"category": intent.category},
        must={
            "backend": backend,
            "real_3d_geometry": True,
            "collision": bool(intent.physics_critical),
            "source_kind": ["analytic_proxy", "engine_builtin"],
        },
        should=(SearchPreference(field="physics_role", value=role),) if role else (),
        semantic_text=query,
        relaxation_policy={"allow_parent_category": True, "allow_format_conversion": False},
    )


def search_intent_from_v2_asset_intent(data: Mapping[str, Any]) -> SearchIntent:
    """Adapt a future V2 asset-intent-shaped mapping without introducing CaseSpec V2."""
    if any(key in data for key in ("raw_query", "taxonomy", "must", "should", "must_not")):
        return SearchIntent.from_dict(data)
    raw_constraints = data.get("constraints")
    if raw_constraints is not None and not isinstance(raw_constraints, Mapping):
        raise ValueError("V2 AssetIntent constraints must be an object")
    constraints = raw_constraints or {}
    supported_constraints = {
        "approx_size_cm",
        "approx_size_m",
        "collision_required",
        "geometry_type",
        "license_tier",
        "materialized",
        "real_3d_geometry",
        "runtime_ready",
    }
    unknown_constraints = sorted(str(key) for key in constraints if str(key) not in supported_constraints)
    if unknown_constraints:
        raise ValueError(f"V2 AssetIntent contains unsupported constraints: {', '.join(unknown_constraints)}")
    must: dict[str, Any] = {}
    backend = data.get("backend")
    if backend:
        must["backend"] = backend
    field_mapping = {
        "real_3d_geometry": "real_3d_geometry",
        "collision_required": "collision",
        "geometry_type": "geometry_type",
        "materialized": "materialized",
        "runtime_ready": "runtime_ready",
        "license_tier": "license_tier",
    }
    for source, target in field_mapping.items():
        if source in constraints:
            must[target] = constraints[source]
    if "approx_size_m" in constraints:
        must["approx_size_m"] = _validate_size_vector(constraints["approx_size_m"], "constraints.approx_size_m")
    elif "approx_size_cm" in constraints:
        size_cm = _validate_size_vector(constraints["approx_size_cm"], "constraints.approx_size_cm")
        must["approx_size_m"] = [float(component) / 100.0 for component in size_cm]
    should: list[dict[str, Any]] = []
    if data.get("physics_role"):
        should.append({"field": "physics_role", "value": data["physics_role"]})
    taxonomy = data.get("taxonomy") if isinstance(data.get("taxonomy"), Mapping) else {}
    return SearchIntent.from_dict(
        {
            "raw_query": data.get("query") or data.get("asset_query") or data.get("intent_id"),
            "taxonomy": taxonomy,
            "must": must,
            "should": should,
            "semantic_text": data.get("semantic_text") or data.get("query") or data.get("asset_query"),
            "relaxation_policy": data.get("relaxation_policy") or {},
        }
    )


def taxonomy_relaxation_values(intent: SearchIntent) -> list[str | None]:
    explicit = intent.must.get("category")
    if explicit is not None:
        return [str(explicit)]
    values: list[str] = []
    for key in ("object_type", "subcategory", "category", "domain"):
        value = str(intent.taxonomy.get(key) or "").strip()
        if value and value.casefold() not in {item.casefold() for item in values}:
            values.append(value)
    if not values:
        return [None]
    if not intent.relaxation_policy.get("allow_parent_category"):
        return [values[0]]
    return values


def asset_matches_approx_size(asset: Mapping[str, Any], intent: SearchIntent) -> bool:
    target = intent.must.get("approx_size_m")
    if target is None:
        return True
    if intent.relaxation_policy.get("allow_uniform_scale_to_approx_size"):
        return True
    actual = asset.get("bbox_size_m") or asset.get("authored_size_m")
    if not isinstance(target, list) or len(target) != 3 or not isinstance(actual, list) or len(actual) != 3:
        return False
    try:
        target_values = sorted(float(component) for component in target)
        actual_values = sorted(float(component) for component in actual)
    except (TypeError, ValueError):
        return False
    return all(
        abs(actual_value - target_value) <= max(target_value * 0.5, 0.05)
        for actual_value, target_value in zip(actual_values, target_values)
    )


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"SearchIntent {field_name} must be an object")
    return value


def _string_mapping(value: Any, field_name: str) -> dict[str, str]:
    mapping = _mapping(value, field_name)
    return {
        str(key): str(item).strip()
        for key, item in mapping.items()
        if str(item or "").strip()
    }


def _reject_unknown_fields(values: Mapping[str, Any], allowed: set[str], field_name: str) -> None:
    unknown = sorted(str(key) for key in values if str(key) not in allowed)
    if unknown:
        raise ValueError(f"SearchIntent {field_name} contains unsupported hard fields: {', '.join(unknown)}")


def _validate_size_vector(value: Any, field_name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{field_name} must contain three positive numbers")
    if any(
        not isinstance(component, (int, float)) or isinstance(component, bool) or float(component) <= 0.0
        for component in value
    ):
        raise ValueError(f"{field_name} must contain three positive numbers")
    return [float(component) for component in value]
