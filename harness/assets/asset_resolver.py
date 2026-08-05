from __future__ import annotations

import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from harness.assets.asset_intent_compiler import CompiledAssetIntent, local_catalog_allowed, provider_route_required
from harness.assets.asset_intent import intent_from_object
from harness.assets.asset_registry import AssetRegistry
from harness.assets.search_intent import SearchIntent, analytic_search_intent_from_asset_intent, search_intent_from_asset_intent
from harness.assets.sqlite_catalog import effective_license_tier, reference_license_authorized


def resolve_asset_intents(
    case_spec: dict[str, Any],
    *,
    top_k: int = 5,
    registry: AssetRegistry | None = None,
    compiled_intents: list[CompiledAssetIntent] | None = None,
    target_backend: str = "unreal",
) -> dict[str, Any]:
    registry = registry or AssetRegistry()
    allow_local_preview = os.environ.get("SIM_HARNESS_ALLOW_LOCAL_PREVIEW_ASSETS", "").casefold() in {"1", "true", "yes"}
    objects = [obj for obj in case_spec.get("objects", []) if isinstance(obj, dict)]
    compiled_by_id = {item.object_id: item for item in compiled_intents or []}
    intents = [
        compiled_by_id[str(obj.get("id") or "")].legacy_intent
        if str(obj.get("id") or "") in compiled_by_id
        else intent_from_object(obj)
        for obj in objects
    ]
    rows = []
    for obj, intent in zip(objects, intents):
        compiled = compiled_by_id.get(str(obj.get("id") or ""))
        acquisition = compiled.acquisition if compiled else None
        provider_pending = bool(acquisition and provider_route_required(acquisition))
        explicit_proxy = bool(obj.get("force_analytic_proxy") or obj.get("asset_policy") == "analytic_proxy")
        search_intent = (
            compiled.search_intent
            if compiled
            else analytic_search_intent_from_asset_intent(intent, obj, backend=target_backend)
            if explicit_proxy
            else search_intent_from_asset_intent(intent, backend=target_backend)
        )
        can_search_catalog = (
            explicit_proxy
            or acquisition is None
            or local_catalog_allowed(
                acquisition,
                allow_local=compiled.allow_local if compiled else True,
            )
        )
        ranked = registry.search_intent(search_intent, top_k=max(top_k * 4, top_k)) if can_search_catalog else []
        if explicit_proxy:
            ranked = [resolved_analytic_recipe(candidate, obj, intent.to_dict()) for candidate in ranked]
        evaluated = [
            {
                **candidate,
                "quality_gate": asset_quality_gate(
                    candidate,
                    physics_critical=intent.physics_critical,
                    allow_local_preview=allow_local_preview,
                ),
            }
            for candidate in ranked
        ]
        selected = next((candidate for candidate in evaluated if str(candidate["quality_gate"]["status"]).startswith("pass")), None)
        rejected = [candidate for candidate in evaluated if candidate["quality_gate"]["status"] == "fail"][:top_k]
        row = {
            "intent": intent.to_dict(),
            "candidates": evaluated[:top_k],
            "rejected_candidates": rejected,
            "selected_asset": selected,
            "selection_reason": (
                "explicit_analytic_recipe_policy"
                if explicit_proxy and selected
                else "first_reference_approved_candidate"
                if selected and selected["quality_gate"]["status"] == "pass"
                else "first_explicit_local_preview_candidate"
                if selected
                else "provider_route_not_implemented"
                if provider_pending
                else "no_quality_approved_candidate"
            ),
            "runtime_binding_requirements": intent.required_properties,
            "fallback_reason": (
                None
                if selected
                else f"requested acquisition route requires next-stage Provider: {acquisition['route']}"
                if provider_pending and acquisition
                else "no quality-approved analytic recipe candidate"
                if explicit_proxy
                else "no quality-approved registry candidate; use analytic/proxy asset"
            ),
            "fallback_mode": (
                "provider_required"
                if provider_pending and not selected
                else "harness_generate_analytic"
                if explicit_proxy and not selected
                else "automatic_proxy"
                if not selected
                else None
            ),
        }
        if acquisition is not None:
            row["acquisition"] = {
                "requested": dict(acquisition),
                "status": (
                    "resolved_local_fallback"
                    if selected and provider_pending
                    else "resolved_local_catalog"
                    if selected
                    else "provider_required"
                    if provider_pending
                    else "local_catalog_unresolved"
                ),
                "actual_route": "local_catalog" if selected else None,
                "route_honored": bool(
                    selected and str(acquisition.get("route")) in {"default", "local_catalog"}
                ),
                "provider_execution": "deferred_to_provider_phase" if provider_pending else "not_required",
            }
            row["intent"] = {
                **row["intent"],
                "compiled_search_intent": search_intent.to_dict(),
                "slot": compiled.slot if compiled else "primary",
            }
        rows.append(row)
    scene_map = resolve_scene_map(
        case_spec,
        registry=registry,
        top_k=top_k,
        allow_local_preview=allow_local_preview,
    )
    resolution_rows = [*rows, *([scene_map] if scene_map else [])]
    selected = [row["selected_asset"] for row in resolution_rows if row.get("selected_asset")]
    result = {
        "schema_version": "harness_asset_resolution_v1",
        "capability_id": "asset_intent_resolution",
        "stage_id": "asset_resolution",
        "case_id": case_spec.get("case_id"),
        "top_k": top_k,
        "physics_critical_count": sum(1 for intent in intents if intent.physics_critical),
        "visual_only_count": sum(1 for intent in intents if not intent.physics_critical),
        "quality_gate": {
            "approved_count": sum(1 for asset in selected if asset["quality_gate"]["status"] == "pass"),
            "local_preview_count": sum(1 for asset in selected if asset["quality_gate"]["status"] == "pass_local_preview"),
            "fallback_count": sum(1 for row in resolution_rows if not row["selected_asset"]),
            "rejected_candidate_count": sum(len(row["rejected_candidates"]) for row in resolution_rows),
            "reference_assets_ready": bool(resolution_rows)
            and all(asset["quality_gate"]["status"] == "pass" for asset in selected)
            and len(selected) == len(resolution_rows),
            "local_preview_enabled": allow_local_preview,
        },
        "invocation_contract": {
            "next_capability_id": "asset_runtime_binding_invocation",
            "requires_selected_asset_or_fallback": True,
            "physics_critical_required_properties": ["collider", "mass", "rigid_body", "collision_profile"],
        },
        "assets": rows,
    }
    if compiled_intents is not None:
        result["asset_intent_compiler"] = {
            "schema_version": "harness_compiled_asset_intents_v1",
            "target_backend": target_backend,
            "intent_count": len(compiled_intents),
            "provider_required_count": sum(
                1
                for row in rows
                if (row.get("acquisition") or {}).get("status") == "provider_required"
            ),
        }
    if scene_map:
        result["scene_map"] = scene_map
    return result


def requested_map_reference(case_spec: dict[str, Any] | None = None) -> str:
    explicit = os.environ.get("SIM_STUDIO_UE_MAP", "").strip()
    if explicit:
        return explicit
    scene = case_spec.get("scene") if isinstance(case_spec, dict) and isinstance(case_spec.get("scene"), dict) else {}
    return str(scene.get("map_preference") or scene.get("map_package") or "").strip()


def resolve_scene_map(
    case_spec: dict[str, Any],
    *,
    registry: AssetRegistry,
    top_k: int,
    allow_local_preview: bool,
) -> dict[str, Any] | None:
    requested = requested_map_reference(case_spec)
    if not requested:
        return None
    query = requested.rsplit(".", 1)[-1].rsplit("/", 1)[-1] if requested.startswith("/Game/") else requested
    intent = SearchIntent(
        raw_query=query,
        taxonomy={"category": "map"},
        must={"backend": "unreal", "class_name": "World", "real_3d_geometry": True},
        semantic_text=query,
        relaxation_policy={"allow_parent_category": False, "allow_format_conversion": False},
    )
    invalid_package_reference = "/" in requested and not requested.startswith("/Game/")
    ranked = [] if invalid_package_reference else registry.search_intent(intent, top_k=max(top_k * 4, top_k))
    if requested.startswith("/Game/"):
        requested_normalized = canonical_ue_map_object_path(requested).casefold()
        ranked = [
            candidate
            for candidate in ranked
            if canonical_ue_map_object_path(candidate_ue_object_path(candidate)).casefold() == requested_normalized
        ]
    evaluated = [
        {
            **candidate,
            "quality_gate": asset_quality_gate(
                candidate,
                physics_critical=False,
                allow_local_preview=allow_local_preview,
            ),
        }
        for candidate in ranked
    ]
    selected = next((candidate for candidate in evaluated if str(candidate["quality_gate"]["status"]).startswith("pass")), None)
    rejected = [candidate for candidate in evaluated if candidate["quality_gate"]["status"] == "fail"][:top_k]
    return {
        "requested_reference": requested,
        "intent": intent.to_dict(),
        "candidates": evaluated[:top_k],
        "rejected_candidates": rejected,
        "selected_asset": selected,
        "selection_reason": (
            "first_reference_approved_candidate"
            if selected and selected["quality_gate"]["status"] == "pass"
            else "first_explicit_local_preview_candidate"
            if selected
            else "no_quality_approved_candidate"
        ),
        "runtime_binding_requirements": ["World", "materialized", "runtime_ready"],
        "fallback_reason": None if selected else "requested map has no quality-approved catalog candidate",
        "fallback_mode": None,
    }


def candidate_ue_object_path(candidate: dict[str, Any]) -> str:
    ue = candidate.get("ue") if isinstance(candidate.get("ue"), dict) else {}
    return str(candidate.get("ue_path") or ue.get("object_path") or "")


def canonical_ue_map_object_path(value: str) -> str:
    text = str(value or "").strip()
    if not text.startswith("/Game/") or "." in text.rsplit("/", 1)[-1]:
        return text
    return f"{text}.{text.rsplit('/', 1)[-1]}"


def asset_quality_gate(
    asset: dict[str, Any],
    *,
    physics_critical: bool,
    allow_local_preview: bool = False,
) -> dict[str, Any]:
    execution_failures: list[str] = []
    reference_failures: list[str] = []
    source_kind = str(asset.get("source_kind") or "").strip()
    source_uri = str(asset.get("source_uri") or "").strip()
    license_name = str(asset.get("license") or "").strip()
    quality_status = str(asset.get("quality_status") or "").strip()
    sha256 = str(asset.get("sha256") or "").strip().casefold()
    redistribution = asset.get("redistribution") or (asset.get("release_audit") or {}).get("redistribution")
    declared_license_tier = str(asset.get("license_tier") or "")
    license_tier = effective_license_tier(
        license_name,
        quality_status,
        declared_tier=declared_license_tier,
        source_kind=source_kind,
        redistribution=redistribution,
    )
    local_path = asset_local_path(asset)
    dependency_status = asset_dependency_status(asset)
    source_requires_file = source_kind not in {"engine_builtin", "analytic_proxy"}

    if not asset.get("ue_path"):
        execution_failures.append("missing_ue_path")
    elif not valid_ue_object_path(str(asset["ue_path"])):
        execution_failures.append("invalid_ue_object_path")
    if not source_kind:
        execution_failures.append("missing_source_kind")
    if not source_uri:
        execution_failures.append("missing_source_uri")
    if source_requires_file and not asset.get("materialized"):
        execution_failures.append("not_materialized")
    if source_requires_file and asset.get("materialized"):
        if local_path is None or not local_path.is_file():
            execution_failures.append("missing_local_file")
        elif is_git_lfs_pointer(local_path):
            execution_failures.append("local_file_is_lfs_pointer")
    if dependency_status["declared_count"] and not dependency_status["complete"]:
        execution_failures.append("dependency_closure_incomplete")
    if not license_name or any(term in license_name.casefold() for term in ("unknown", "unverified", "pending")):
        reference_failures.append("missing_or_unverified_license")
    if quality_status not in {"approved", "approved_proxy"}:
        reference_failures.append("quality_not_approved")
    if source_requires_file and not is_sha256(sha256):
        reference_failures.append("missing_or_invalid_sha256")
    elif source_requires_file and local_path and local_path.is_file() and not is_git_lfs_pointer(local_path):
        if sha256_file(local_path) != sha256:
            execution_failures.append("sha256_mismatch")
    if license_tier == "blocked":
        reference_failures.append("license_tier_blocked")
    elif license_tier == "local_preview":
        reference_failures.append("license_tier_local_preview")
    elif license_tier != "reference":
        reference_failures.append("license_tier_invalid")
    if declared_license_tier == "reference" and not reference_license_authorized(
        license_name,
        source_kind=source_kind,
        redistribution=redistribution,
    ):
        reference_failures.append("reference_license_evidence_missing")
    for size_field in ("bbox_size_m", "authored_size_m"):
        if asset.get(size_field) is not None and not valid_dimensions(asset[size_field]):
            execution_failures.append(f"invalid_{size_field}")
    binding = unreal_binding(asset)
    if binding is not None and binding.get("runtime_ready") is False:
        execution_failures.append("ue_binding_not_runtime_ready")
    if physics_critical:
        for field in ("collider", "mass_kg", "material", "collision_profile"):
            if asset.get(field) is None:
                execution_failures.append(f"missing_physics_{field}")
        if not collision_ready(asset):
            execution_failures.append("collision_not_ready")
    execution_failures = dedupe(execution_failures)
    reference_failures = dedupe(reference_failures)
    local_preview = (
        allow_local_preview
        and license_tier == "local_preview"
        and quality_status in {"approved", "approved_proxy", "local_preview"}
        and not execution_failures
    )
    failures = [*execution_failures, *reference_failures]
    return {
        "status": "fail" if execution_failures or (reference_failures and not local_preview) else "pass_local_preview" if local_preview else "pass",
        "failure_codes": failures,
        "execution_failure_codes": execution_failures,
        "reference_blockers": reference_failures,
        "reference_approved": not failures,
        "content_identity": sha256 or source_uri or None,
        "hash_required": source_requires_file,
        "license_tier": license_tier,
        "materialized": bool(asset.get("materialized")) or not source_requires_file,
        "local_file": str(local_path) if local_path else None,
        "dependency_status": dependency_status,
        "ue_binding_ready": bool(asset.get("ue_path")) and (binding is None or binding.get("runtime_ready") is not False),
    }


def resolved_analytic_recipe(
    candidate: dict[str, Any],
    obj: dict[str, Any],
    intent: dict[str, Any],
) -> dict[str, Any]:
    recipe_id = str(candidate.get("asset_id") or candidate.get("id") or "analytic_recipe")
    return {
        **candidate,
        "proxy": True,
        "analytic_recipe": {
            "schema_version": "harness_analytic_asset_recipe_v1",
            "recipe_id": recipe_id,
            "provider": "builtin_catalog",
            "parameters": {
                "role": intent.get("role"),
                "shape": obj.get("shape"),
                "collider": obj.get("collider") or candidate.get("collider"),
                "dimensions_m": obj.get("dimensions_m") or obj.get("size_m"),
            },
        },
    }


def is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def asset_local_path(asset: dict[str, Any]) -> Path | None:
    paths = asset.get("paths") if isinstance(asset.get("paths"), dict) else {}
    adp = asset.get("adp") if isinstance(asset.get("adp"), dict) else {}
    candidates = [asset.get("local_path"), paths.get("local_file"), adp.get("repo_file")]
    files = asset.get("files") if isinstance(asset.get("files"), list) else []
    candidates.extend(row.get("local_path") for row in files if isinstance(row, dict) and row.get("role") in {None, "primary"})
    value = next((candidate for candidate in candidates if candidate), None)
    return Path(str(value)) if value else None


def asset_dependency_status(asset: dict[str, Any]) -> dict[str, Any]:
    ue = asset.get("ue") if isinstance(asset.get("ue"), dict) else {}
    bundle = asset.get("bundle") if isinstance(asset.get("bundle"), dict) else {}
    dependencies = [str(value) for value in ue.get("dependencies") or []]
    records = [row for row in bundle.get("dependencies") or [] if isinstance(row, dict)]
    record_dependencies = [
        str(row.get("package") or row.get("dependency_id"))
        for row in records
        if row.get("package") or row.get("dependency_id")
    ]
    dependencies = dedupe([*dependencies, *record_dependencies])
    records_by_dependency = {
        str(row.get("package") or row.get("dependency_id")): row
        for row in records
        if row.get("package") or row.get("dependency_id")
    }
    missing: list[str] = []
    missing_files: list[str] = []
    missing_hashes: list[str] = []
    hash_mismatches: list[str] = []
    lfs_pointers: list[str] = []
    for dependency in dependencies:
        row = records_by_dependency.get(dependency)
        if row is None or row.get("materialized") is False:
            missing.append(dependency)
            continue
        path_value = row.get("local_path")
        if not path_value:
            if dependency.startswith(("/Engine/", "/Script/")) and row.get("materialized") is True:
                continue
            missing.append(dependency)
            missing_files.append(dependency)
            continue
        path = Path(str(path_value))
        if not path.is_file():
            missing.append(dependency)
            missing_files.append(dependency)
            continue
        if is_git_lfs_pointer(path):
            missing.append(dependency)
            lfs_pointers.append(dependency)
            continue
        expected_hash = str(row.get("sha256") or "").casefold()
        if not is_sha256(expected_hash):
            missing.append(dependency)
            missing_hashes.append(dependency)
            continue
        if sha256_file(path) != expected_hash:
            missing.append(dependency)
            hash_mismatches.append(dependency)
    complete = not missing
    status = {
        "declared_count": len(dependencies),
        "materialized_count": len(dependencies) - len(missing) if dependencies else 0,
        "missing_dependencies": missing,
        "complete": complete,
    }
    for key, values in (
        ("missing_files", missing_files),
        ("missing_hashes", missing_hashes),
        ("hash_mismatches", hash_mismatches),
        ("lfs_pointers", lfs_pointers),
    ):
        if values:
            status[key] = values
    return status


def valid_ue_object_path(value: str) -> bool:
    return bool(
        re.fullmatch(r"/(?:Game|Engine)/[^\s]+\.[^/\s.]+", value)
        or re.fullmatch(r"/Script/[^/\s.]+\.[^/\s.]+", value)
    )


def valid_dimensions(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(
        isinstance(component, (int, float)) and not isinstance(component, bool) and float(component) > 0.0
        for component in value
    )


def unreal_binding(asset: dict[str, Any]) -> dict[str, Any] | None:
    bindings = asset.get("backend_bindings")
    if isinstance(bindings, dict):
        return next(
            (
                value
                for backend, value in bindings.items()
                if isinstance(value, dict)
                and (
                    str(backend).casefold() == "ue"
                    or str(backend).casefold().startswith("ue_")
                    or str(backend).casefold().startswith("unreal")
                )
            ),
            None,
        )
    if isinstance(bindings, list):
        return next(
            (
                row
                for row in bindings
                if isinstance(row, dict) and str(row.get("backend") or "").casefold() in {"unreal", "ue"}
            ),
            None,
        )
    return None


def collision_ready(asset: dict[str, Any]) -> bool:
    collision = asset.get("collision") if isinstance(asset.get("collision"), dict) else {}
    if collision.get("present") is False:
        return False
    return bool(asset.get("collider") and asset.get("collision_profile"))


def sha256_file(path: Path) -> str:
    stat = path.stat()
    return _sha256_file_cached(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=256)
def _sha256_file_cached(path: str, size: int, mtime_ns: int) -> str:
    del size, mtime_ns
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_git_lfs_pointer(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(80).startswith(b"version https://git-lfs.github.com/spec/v1")
    except OSError:
        return False


def dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
