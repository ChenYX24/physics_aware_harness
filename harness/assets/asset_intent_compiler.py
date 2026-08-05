from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from harness.assets.asset_intent import AssetIntent, intent_from_object
from harness.assets.search_intent import (
    SearchIntent,
    SearchPreference,
    analytic_search_intent_from_asset_intent,
    search_intent_from_asset_intent,
)
from harness.core.case_spec_v2 import CaseSpecV2, asset_requests


RESOURCE_KIND_TO_ASSET_TYPE = {
    "mesh_3d": "StaticMesh",
    "skeletal_mesh": "SkeletalMesh",
    "geometry_collection": "GeometryCollection",
    "blueprint_actor": "Blueprint",
    "material": "Material",
    "texture_2d": "Texture2D",
    "map": "World",
}

SOURCE_KIND_ALIASES = {
    "procedural": "procedural_generation",
    "local_procedural": "procedural_generation",
    "generated_procedural": "procedural_generation",
}


@dataclass(frozen=True)
class CompiledAssetIntent:
    object_id: str
    legacy_intent: AssetIntent
    search_intent: SearchIntent
    acquisition: dict[str, Any]
    slot: str = "primary"
    allow_local: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "slot": self.slot,
            "legacy_intent": self.legacy_intent.to_dict(),
            "search_intent": self.search_intent.to_dict(),
            "acquisition": dict(self.acquisition),
            "allow_local": self.allow_local,
        }


def compile_v2_asset_intents(
    case_spec: CaseSpecV2,
    legacy_case_spec: Mapping[str, Any],
    *,
    target_backend: str,
) -> list[CompiledAssetIntent]:
    legacy_by_id = {
        str(obj.get("id")): obj
        for obj in legacy_case_spec.get("objects") or []
        if isinstance(obj, dict) and obj.get("id")
    }
    policy = case_spec.data.get("asset_policy") if isinstance(case_spec.data.get("asset_policy"), dict) else {}
    compiled: list[CompiledAssetIntent] = []
    for obj in case_spec.objects:
        object_id = str(obj.get("id") or "")
        legacy_object = legacy_by_id.get(object_id, {"id": object_id, "role": obj.get("role")})
        legacy_intent = intent_from_object(legacy_object)
        requests = asset_requests(obj.get("asset"))
        request = requests[0] if requests else {}
        acquisition = normalized_acquisition(request.get("acquisition"))
        search_intent = (
            analytic_search_intent_from_asset_intent(
                legacy_intent,
                legacy_object,
                backend=target_backend,
            )
            if not request and legacy_object.get("force_analytic_proxy")
            else _compile_search_intent(
                request,
                obj,
                legacy_intent,
                target_backend=target_backend,
                required_license_tier=str(policy.get("required_license_tier") or "local_preview"),
            )
        )
        compiled.append(
            CompiledAssetIntent(
                object_id=object_id,
                legacy_intent=legacy_intent,
                search_intent=search_intent,
                acquisition=acquisition,
                allow_local=bool(policy.get("allow_local", True)),
            )
        )
    return compiled


def normalized_acquisition(value: Any) -> dict[str, Any]:
    acquisition = dict(value) if isinstance(value, Mapping) else {}
    return {
        "route": str(acquisition.get("route") or "default"),
        "requirement": str(acquisition.get("requirement") or "preferred"),
        "origin": str(acquisition.get("origin") or "system_default"),
        "provider_hint": acquisition.get("provider_hint"),
        "source_uri_hint": acquisition.get("source_uri_hint"),
        "reference_inputs": [dict(item) for item in acquisition.get("reference_inputs") or [] if isinstance(item, Mapping)],
        "fallback_order": [str(item) for item in acquisition.get("fallback_order") or []],
    }


def local_catalog_allowed(acquisition: Mapping[str, Any], *, allow_local: bool = True) -> bool:
    if not allow_local:
        return False
    route = str(acquisition.get("route") or "default")
    if route in {"default", "local_catalog"}:
        return True
    return "local_catalog" in {str(value) for value in acquisition.get("fallback_order") or []}


def provider_route_required(acquisition: Mapping[str, Any]) -> bool:
    return str(acquisition.get("route") or "default") in {
        "external_site",
        "procedural_generation",
        "model_generation",
    }


def _compile_search_intent(
    request: Mapping[str, Any],
    obj: Mapping[str, Any],
    legacy_intent: AssetIntent,
    *,
    target_backend: str,
    required_license_tier: str,
) -> SearchIntent:
    if not request:
        return search_intent_from_asset_intent(legacy_intent, backend=target_backend)
    description = str(request.get("description") or legacy_intent.query).strip()
    raw_must = request.get("must") if isinstance(request.get("must"), Mapping) else {}
    must = dict(raw_must)
    if must.get("source_kind") is not None:
        must["source_kind"] = _canonical_source_kind(must["source_kind"])
    must.setdefault("backend", target_backend)
    resource_kind = str(request.get("resource_kind") or "").strip()
    if resource_kind in RESOURCE_KIND_TO_ASSET_TYPE:
        must.setdefault("asset_type", RESOURCE_KIND_TO_ASSET_TYPE[resource_kind])
    physics = obj.get("physics") if isinstance(obj.get("physics"), Mapping) else {}
    if physics.get("collision_required"):
        must.setdefault("collision", True)
        must.setdefault("real_3d_geometry", True)
    geometry = obj.get("geometry") if isinstance(obj.get("geometry"), Mapping) else {}
    if isinstance(geometry.get("approx_size_m"), list):
        must.setdefault("approx_size_m", list(geometry["approx_size_m"]))
    if required_license_tier == "reference":
        must.setdefault("license_tier", "reference")
    preferences = request.get("preferences") if isinstance(request.get("preferences"), Mapping) else {}
    should = [SearchPreference(field="physics_role", value=legacy_intent.role)]
    for field, raw_value in preferences.items():
        if isinstance(raw_value, Mapping) and "value" in raw_value:
            should.append(
                SearchPreference(
                    field=str(field),
                    value=raw_value.get("value"),
                    weight=float(raw_value.get("weight", 1.0)),
                    confidence=float(raw_value.get("confidence", 1.0)),
                )
            )
        else:
            should.append(SearchPreference(field=str(field), value=raw_value))
    taxonomy = request.get("taxonomy") if isinstance(request.get("taxonomy"), Mapping) else {}
    return SearchIntent.from_dict(
        {
            "raw_query": description,
            "taxonomy": dict(taxonomy) or {"category": legacy_intent.category},
            "must": must,
            "should": [item.to_dict() for item in should],
            "must_not": request.get("must_not") or {},
            "semantic_text": str(request.get("semantic_text") or description),
            "reference_image": _similarity_reference(request),
            "relaxation_policy": request.get("relaxation_policy")
            or {"allow_parent_category": True, "allow_format_conversion": False},
        }
    )


def _similarity_reference(request: Mapping[str, Any]) -> str | None:
    acquisition = request.get("acquisition") if isinstance(request.get("acquisition"), Mapping) else {}
    for reference in acquisition.get("reference_inputs") or []:
        if not isinstance(reference, Mapping):
            continue
        usage = {str(value) for value in reference.get("usage") or []}
        if "similarity_search" in usage and reference.get("allow_similarity_search", True):
            return str(reference.get("input_id") or "") or None
    return None


def _canonical_source_kind(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonical_source_kind(item) for item in value]
    normalized = str(value).strip().casefold()
    return SOURCE_KIND_ALIASES.get(normalized, value)
