#!/usr/bin/env python3
"""Import the AgenticDataPlatform AssetIndex into Simulator Studio manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.assets.sqlite_catalog import default_catalog_path, initialize_catalog

SOURCE_NAME = "agenticdataplatform_asset_index"
DEFAULT_WORKSPACE = Path.home() / "SimulatorWorkspace" / "physics_aware_harness"
DEFAULT_QUERIES = [
    "water plane",
    "ball sphere",
    "MarketEnvironment Day",
    "traffic cone",
    "wood crate",
    "gas station",
    "gear metal",
    "bottle domino",
]
QUERY_ALIASES = {
    "football": ("football", "soccer", "ball", "sphere", "8ball", "8-ball"),
    "soccer": ("soccer", "football", "ball", "sphere", "8ball", "8-ball"),
    "water": ("water", "liquid", "lake", "ocean", "pond", "pool"),
    "map": ("map", "level", "scene", "world"),
    "scene": ("scene", "map", "level", "world"),
    "gas": ("gas", "fuel", "station", "pump"),
    "bottle": ("bottle", "can", "drink", "domino"),
}


def resolve_index_path(source: str | Path) -> Path:
    path = Path(source)
    if path.is_dir():
        return path / "AssetIndex" / "ASSETS_INDEX.json"
    return path


def infer_repo_root(source: str | Path, repo_root: str | Path | None = None) -> Path | None:
    if repo_root:
        return Path(repo_root)
    path = Path(source)
    if path.is_dir() and (path / "AssetIndex" / "ASSETS_INDEX.json").exists():
        return path
    for parent in path.parents:
        if (parent / "AssetIndex" / "ASSETS_INDEX.json").exists() and (parent / "Content").exists():
            return parent
    return None


def asset_key(asset_id: str) -> str:
    return asset_id.strip("/").replace("/", "_").replace(".", "_").lower()


def object_path(asset_id: str) -> str:
    name = asset_id.rsplit("/", 1)[-1]
    return asset_id if "." in name else f"{asset_id}.{name}"


def package_file_path(repo_root: Path | None, package_name: str, class_name: str | None) -> Path | None:
    if not repo_root or not package_name.startswith("/Game/"):
        return None
    ext = ".umap" if class_name == "World" else ".uasset"
    return repo_root / "Content" / f"{package_name.removeprefix('/Game/')}{ext}"


def is_materialized(path: Path | None) -> bool:
    if not path or not path.is_file():
        return False
    with path.open("rb") as stream:
        return not stream.read(80).startswith(b"version https://git-lfs.github.com/spec/v1")


def sha256_file(path: Path | None) -> str | None:
    if not is_materialized(path):
        return None
    assert path is not None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def category_pair(item: dict[str, Any]) -> tuple[str, str]:
    category = str(item.get("category") or "asset").lower()
    subcategory = str(item.get("subcategory") or "generic").lower()
    name = str(item.get("asset_name") or item.get("asset_id") or "").lower()
    tags = " ".join(item.get("tags") or []).lower()
    haystack = f"{category} {subcategory} {name} {tags}"
    if item.get("ue_class") == "World":
        return "map", subcategory
    if "water" in haystack and any(word in haystack for word in ("plane", "material", "water")):
        return "environment", "water"
    if any(word in haystack for word in ("8-ball", "8ball", "sphere", "ball", "pool-ball")):
        return "prop", "ball"
    if any(word in haystack for word in ("chair", "table", "desk", "sofa")):
        if "chair" in haystack:
            return "furniture", "chair"
        if "table" in haystack:
            return "furniture", "table"
        return "furniture", "generic"
    if any(word in haystack for word in ("vehicle", "car", "truck", "bike", "motorcycle")):
        return "vehicle", "generic"
    if any(word in haystack for word in ("character", "mannequin", "citizen", "boy", "adventurer")):
        return "character", "humanoid"
    if category == "props":
        return "prop", subcategory
    if category == "maps":
        return "environment", subcategory
    return category, subcategory


def material_guess(item: dict[str, Any]) -> str:
    haystack = " ".join(
        str(value).lower()
        for value in (
            item.get("asset_name", ""),
            item.get("asset_id", ""),
            item.get("semantic_name", ""),
            item.get("full_description", ""),
            " ".join(item.get("tags") or []),
        )
    )
    if any(term in haystack for term in ("billiard", "8-ball", "8ball", "pool-ball")):
        return "resin"
    if "felt" in haystack:
        return "felt"
    for material in ("water", "metal", "steel", "wood", "glass", "rubber", "plastic", "stone", "concrete", "fabric"):
        if material in haystack:
            return "metal" if material == "steel" else material
    return "plastic"


MATERIAL_DEFAULTS = {
    "resin": {"static_friction": 0.08, "dynamic_friction": 0.06, "restitution": 0.82},
    "felt": {"static_friction": 0.18, "dynamic_friction": 0.12, "restitution": 0.05},
    "rubber": {"static_friction": 0.8, "dynamic_friction": 0.65, "restitution": 0.55},
    "metal": {"static_friction": 0.45, "dynamic_friction": 0.32, "restitution": 0.22},
    "wood": {"static_friction": 0.55, "dynamic_friction": 0.4, "restitution": 0.18},
    "glass": {"static_friction": 0.35, "dynamic_friction": 0.25, "restitution": 0.12},
    "stone": {"static_friction": 0.7, "dynamic_friction": 0.55, "restitution": 0.08},
    "concrete": {"static_friction": 0.75, "dynamic_friction": 0.6, "restitution": 0.05},
    "plastic": {"static_friction": 0.45, "dynamic_friction": 0.32, "restitution": 0.18},
    "fabric": {"static_friction": 0.7, "dynamic_friction": 0.55, "restitution": 0.03},
    "water": {"static_friction": 0.0, "dynamic_friction": 0.0, "restitution": 0.0},
}


def material_properties(category: str) -> dict[str, float]:
    return dict(MATERIAL_DEFAULTS.get(category, MATERIAL_DEFAULTS["plastic"]))


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_") or "unnamed"


def dependency_kind(path: str, raw: dict[str, dict[str, Any]]) -> str:
    item = raw.get(path) or {}
    class_name = str(item.get("ue_class") or "").casefold()
    text = path.casefold()
    if "material" in class_name or "/material" in text:
        return "material"
    if "blueprint" in class_name:
        return "blueprint_logic"
    if "skeleton" in class_name or "anim" in class_name:
        return "animation"
    return "asset"


def thumbnail_path(index_path: Path, value: Any, asset_id: str = "") -> str | None:
    candidates: list[Path] = []
    if value:
        path = Path(str(value))
        candidates.append(path if path.is_absolute() else index_path.parent.parent / path)
    if asset_id:
        filename = asset_id.lstrip("/").replace("/", "__") + ".png"
        candidates.append(index_path.parent / "thumbnails" / filename)
    for path in candidates:
        if path.is_file():
            return str(path.resolve())
    return None


def bbox_size_m(item: dict[str, Any]) -> list[float] | None:
    bbox = (item.get("geometry") or {}).get("bbox_cm")
    if not bbox:
        return None
    return [round(float(value) / 100.0, 5) for value in bbox]


def dependency_file_paths(
    repo_root: Path | None,
    dependencies: list[str],
    raw: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    paths: list[str] = []
    raw = raw or {}
    for dependency in dependencies:
        dep_file = package_file_path(repo_root, dependency, (raw.get(dependency) or {}).get("ue_class"))
        if dep_file:
            paths.append(str(dep_file))
    return paths


def dependency_record(
    dependency: str,
    raw: dict[str, dict[str, Any]],
    materialize_repo_root: Path | None,
) -> dict[str, Any]:
    class_name = (raw.get(dependency) or {}).get("ue_class")
    local_path = package_file_path(materialize_repo_root, dependency, class_name)
    materialized = is_materialized(local_path)
    return {
        "package": dependency,
        "class_name": class_name,
        "kind": dependency_kind(dependency, raw),
        "local_path": str(local_path) if local_path else None,
        "materialized": materialized,
        "sha256": sha256_file(local_path) if materialized else None,
        "byte_size": local_path.stat().st_size if materialized and local_path else None,
    }


def convert_asset(
    asset_id: str,
    item: dict[str, Any],
    materialize_repo_root: Path | None,
    metadata_repo_root: Path | None = None,
    *,
    raw: dict[str, dict[str, Any]] | None = None,
    index_path: Path | None = None,
) -> dict[str, Any]:
    category_l1, category_l2 = category_pair(item)
    class_name = item.get("ue_class")
    dependencies = item.get("dependencies") or []
    raw = raw or {}
    local_file = package_file_path(materialize_repo_root, asset_id, class_name)
    metadata_file = package_file_path(metadata_repo_root or materialize_repo_root, asset_id, class_name)
    dependency_materialized_count = sum(
        1
        for dependency in dependencies
        if is_materialized(
            package_file_path(materialize_repo_root, dependency, (raw.get(dependency) or {}).get("ue_class"))
        )
    )
    material_category = material_guess(item)
    materialized = is_materialized(local_file)
    runtime_ready = materialized and dependency_materialized_count == len(dependencies)
    interaction = item.get("interaction") if isinstance(item.get("interaction"), dict) else {}
    dependency_records = [dependency_record(dependency, raw, materialize_repo_root) for dependency in dependencies]
    aliases = sorted(
        {
            str(value).strip()
            for value in [item.get("asset_name"), item.get("semantic_name"), *(item.get("tags") or [])]
            if str(value or "").strip()
        }
    )
    usage_groups = sorted(
        {
            category_l1,
            f"{category_l1}/{category_l2}",
            *(str(value) for value in interaction.get("active") or []),
            *(str(value) for value in interaction.get("passive") or []),
        }
    )
    collision_profile = ((item.get("physics") or {}).get("collision_profile") if isinstance(item.get("physics"), dict) else None) or "BlockAll"
    collider = "mesh" if class_name in {"StaticMesh", "SkeletalMesh"} else "actor"
    mass_kg = item.get("estimated_mass_kg")
    preview_path = thumbnail_path(index_path, item.get("thumbnail"), asset_id) if index_path else None
    content_hash = sha256_file(local_file)
    return {
        "asset_id": asset_key(asset_id),
        "name": item.get("asset_name") or asset_id.rsplit("/", 1)[-1],
        "semantic_name": item.get("semantic_name") or "",
        "description": item.get("full_description") or item.get("semantic_name") or "",
        "category_l1": category_l1,
        "category_l2": category_l2,
        "tags": item.get("tags") or [],
        "aliases": aliases,
        "name_group": slug(str(item.get("semantic_name") or item.get("asset_name") or asset_id.rsplit("/", 1)[-1])),
        "usage_groups": usage_groups,
        "bbox_size_m": bbox_size_m(item),
        "source_kind": "local_ue_project" if materialized else "catalog_candidate",
        "source_uri": f"ue://{asset_id.lstrip('/')}",
        "license": "UNVERIFIED_LOCAL_ENTITLEMENT",
        "license_tier": "local_preview" if materialized else "blocked",
        "quality_status": "local_preview" if materialized else "discovered",
        "lifecycle_status": "runtime_bound" if runtime_ready else "materialized" if materialized else "discovered",
        "sha256": content_hash,
        "byte_size": local_file.stat().st_size if materialized and local_file else None,
        "ue_path": object_path(asset_id),
        "collider": collider,
        "mass_kg": mass_kg,
        "material": material_properties(material_category),
        "collision_profile": collision_profile,
        "physics": {
            "material_category": material_category,
            "material_properties": material_properties(material_category),
            "estimated_mass_kg": mass_kg,
            "collider": collider,
            "collision_profile": collision_profile,
        },
        "paths": {
            "ue5": object_path(asset_id),
            "thumbnail": preview_path,
            "local_file": str(local_file) if local_file else None,
        },
        "files": [
            {
                "role": "primary",
                "local_path": str(local_file),
                "format": local_file.suffix.casefold().lstrip("."),
                "sha256": content_hash,
                "byte_size": local_file.stat().st_size if materialized else None,
                "materialized": materialized,
            }
        ] if local_file else [],
        "ue": {
            "object_path": object_path(asset_id),
            "package_name": asset_id,
            "package_path": item.get("package_path"),
            "class_name": class_name,
            "dependencies": dependencies,
            "material_paths": [dep for dep in dependencies if "/Material" in dep or "/Materials" in dep],
        },
        "backend_bindings": {
            "unreal": {
                "object_path": object_path(asset_id),
                "class_name": class_name,
                "materialized": materialized,
                "runtime_ready": runtime_ready,
            }
        },
        "bundle": {
            "bundle_id": f"ue_bundle:{asset_id}",
            "owner_asset": asset_id,
            "dependencies": dependency_records,
            "blueprint_logic_dependencies": [row["package"] for row in dependency_records if row["kind"] == "blueprint_logic"],
            "callable_functions": [],
            "function_introspection_status": "pending_ue_reflection" if class_name == "Blueprint" else "not_applicable",
        },
        "acquisition": {
            "mode": "preimported" if materialized else "harness_find_at_runtime",
            "status": "materialized" if materialized else "catalogued_not_materialized",
            "generator": None,
        },
        "adp": {
            "asset_id": asset_id,
            "semantic_name": item.get("semantic_name"),
            "interaction": item.get("interaction"),
            "estimated_mass_kg": item.get("estimated_mass_kg"),
            "repo_file": str(metadata_file) if metadata_file else None,
            "dependency_files": dependency_file_paths(metadata_repo_root or materialize_repo_root, dependencies, raw),
            "dependency_materialized_count": dependency_materialized_count,
        },
        "source": SOURCE_NAME,
        "materialized": materialized,
    }


def build_registry(
    source: str | Path,
    repo_root: str | Path | None = None,
    metadata_repo_root: str | Path | None = None,
    metadata_source_path: str | Path | None = None,
) -> dict[str, Any]:
    index_path = resolve_index_path(source)
    raw = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"ADP index must be a JSON object: {index_path}")
    repo = infer_repo_root(source, repo_root)
    metadata_repo = Path(metadata_repo_root) if metadata_repo_root else repo
    assets = [
        convert_asset(
            asset_id,
            item,
            repo,
            metadata_repo,
            raw=raw,
            index_path=index_path,
        )
        for asset_id, item in raw.items()
    ]
    class_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    materialized_counts = {"materialized": 0, "missing": 0}
    for asset in assets:
        class_name = asset["ue"].get("class_name") or "Unknown"
        class_counts[class_name] = class_counts.get(class_name, 0) + 1
        category = asset["category_l1"]
        category_counts[category] = category_counts.get(category, 0) + 1
        materialized_counts["materialized" if asset.get("materialized") else "missing"] += 1
    return {
        "schema_version": "asset_registry.v3",
        "source": SOURCE_NAME,
        "source_path": str(metadata_source_path or index_path),
        "repo_root": str(metadata_repo) if metadata_repo else None,
        "asset_count": len(assets),
        "assets": assets,
        "class_counts": dict(sorted(class_counts.items())),
        "category_counts": dict(sorted(category_counts.items())),
        "materialized_counts": materialized_counts,
        "acquisition_modes": {
            "preimported": "Harness indexes already-materialized UE packages.",
            "harness_generate": "Harness records generator inputs and imports generated output before use.",
            "harness_find_at_runtime": "Harness may discover candidates, but must materialize and license-check before reference use.",
        },
    }


def expanded_terms(query: str) -> list[str]:
    terms: list[str] = []
    for raw in query.lower().replace("_", " ").replace("-", " ").split():
        for alias in QUERY_ALIASES.get(raw, (raw,)):
            if alias not in terms:
                terms.append(alias)
    return terms


def searchable_text(asset: dict[str, Any]) -> str:
    ue = asset.get("ue") or {}
    adp = asset.get("adp") or {}
    return " ".join(
        str(part)
        for part in (
            asset.get("asset_id", ""),
            asset.get("name", ""),
            asset.get("description", ""),
            asset.get("category_l1", ""),
            asset.get("category_l2", ""),
            " ".join(asset.get("tags") or []),
            ue.get("object_path", ""),
            ue.get("package_path", ""),
            ue.get("class_name", ""),
            adp.get("semantic_name", ""),
        )
    ).lower()


def search_assets(registry: dict[str, Any], query: str, top_k: int = 8, materialized_only: bool = False) -> list[dict[str, Any]]:
    terms = expanded_terms(query)
    results = []
    for asset in registry.get("assets", []):
        if materialized_only and not asset.get("materialized"):
            continue
        text = searchable_text(asset)
        score = sum(1 for term in terms if term in text)
        if query.lower() in text:
            score += 4
        if asset.get("materialized"):
            score += 1
        if score:
            results.append({**asset, "score": score})
    results.sort(key=lambda item: (-int(item["score"]), not bool(item.get("materialized")), str(item.get("name") or "")))
    return results[:top_k]


def compact(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "score": asset.get("score"),
        "asset_id": asset.get("asset_id"),
        "name": asset.get("name"),
        "category_l1": asset.get("category_l1"),
        "category_l2": asset.get("category_l2"),
        "class_name": asset.get("ue", {}).get("class_name"),
        "ue5_path": asset.get("paths", {}).get("ue5"),
        "materialized": asset.get("materialized"),
        "repo_file": asset.get("adp", {}).get("repo_file"),
        "dependency_count": len(asset.get("ue", {}).get("dependencies") or []),
    }


def build_search_report(registry: dict[str, Any], queries: list[str], top_k: int) -> dict[str, Any]:
    return {
        "source": registry.get("source"),
        "source_path": registry.get("source_path"),
        "repo_root": registry.get("repo_root"),
        "asset_count": registry.get("asset_count"),
        "class_counts": registry.get("class_counts"),
        "category_counts": registry.get("category_counts"),
        "materialized_counts": registry.get("materialized_counts"),
        "queries": {query: [compact(asset) for asset in search_assets(registry, query, top_k=top_k)] for query in queries},
    }


def build_scenario_manifest(registry: dict[str, Any]) -> dict[str, Any]:
    maps = []
    for asset in registry.get("assets", []):
        if asset.get("ue", {}).get("class_name") != "World" and asset.get("category_l1") != "map":
            continue
        deps = asset.get("ue", {}).get("dependencies") or []
        materialized_deps = int((asset.get("adp") or {}).get("dependency_materialized_count") or 0)
        maps.append(
            {
                **compact(asset),
                "description": asset.get("description") or asset.get("semantic_name") or asset.get("name"),
                "tags": list(asset.get("tags") or []),
                "thumbnail": (asset.get("paths") or {}).get("thumbnail"),
                "quality_status": asset.get("quality_status"),
                "license": asset.get("license"),
                "dependency_count": len(deps),
                "materialized_dependency_count": materialized_deps,
                "missing_dependency_count": max(len(deps) - materialized_deps, 0),
                "dependencies": deps,
                "dependency_bundle": asset.get("bundle") or {},
                "preview_presets": map_preview_presets(asset),
            }
        )
    maps.sort(key=lambda item: (not item.get("materialized"), item.get("name") or ""))
    return {
        "schema_version": "map_catalog.v1",
        "source": registry.get("source"),
        "source_path": registry.get("source_path"),
        "repo_root": registry.get("repo_root"),
        "map_count": len(maps),
        "maps": maps,
    }


def map_preview_presets(asset: dict[str, Any]) -> list[dict[str, Any]]:
    map_id = str((asset.get("ue") or {}).get("package_name") or asset.get("asset_id") or "map")
    return [
        {
            "preset_id": f"{slug(map_id)}__balanced_static",
            "description": "Readable map-light preview from three validated static viewpoints.",
            "camera_ids": ["front_static", "side_static", "top_down"],
            "lighting_preset": "map_lights_balanced_fill",
            "render_passes": ["rgb"],
            "preview_status": "pending_capture",
            "runtime_status": "supported_static",
        },
        {
            "preset_id": f"{slug(map_id)}__data_neutral",
            "description": "Fixed-exposure sensor preview for RGB, depth, and instance segmentation.",
            "camera_ids": ["front_static", "side_static", "top_down"],
            "lighting_preset": "fixed_exposure_data_neutral",
            "render_passes": ["rgb", "depth", "segmentation"],
            "preview_status": "pending_capture",
            "runtime_status": "supported_static",
        },
        {
            "preset_id": f"{slug(map_id)}__tracking_candidate",
            "description": "Moving subject-tracking preview; selectable only after runtime trajectory validation.",
            "camera_ids": ["tracking_subject"],
            "lighting_preset": "cinematic_subject_key_fill",
            "render_passes": ["rgb"],
            "preview_status": "not_generated",
            "runtime_status": "planned_unverified",
        },
    ]


def build_group_index(registry: dict[str, Any]) -> dict[str, Any]:
    name_groups: dict[str, list[str]] = {}
    usage_groups: dict[str, list[str]] = {}
    bundles: list[dict[str, Any]] = []
    for asset in registry.get("assets", []):
        asset_id = str(asset.get("asset_id") or "")
        if not asset_id:
            continue
        name_groups.setdefault(str(asset.get("name_group") or "unnamed"), []).append(asset_id)
        for group in asset.get("usage_groups") or []:
            usage_groups.setdefault(str(group), []).append(asset_id)
        bundle = asset.get("bundle")
        if isinstance(bundle, dict) and bundle.get("dependencies"):
            bundles.append(bundle)
    return {
        "schema_version": "asset_group_index.v1",
        "name_groups": {key: sorted(values) for key, values in sorted(name_groups.items())},
        "usage_groups": {key: sorted(values) for key, values in sorted(usage_groups.items())},
        "dependency_bundles": bundles,
    }


def manifest_entry(key: str, asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": key,
        "asset_id": asset.get("asset_id"),
        "name": asset.get("name"),
        "category_l1": asset.get("category_l1"),
        "category_l2": asset.get("category_l2"),
        "ue5_path": asset.get("paths", {}).get("ue5"),
        "material_path": None,
        "material": (asset.get("physics") or {}).get("material_properties"),
        "tags": list(asset.get("tags") or []),
        "bbox_size_m": asset.get("bbox_size_m"),
        "physics": dict(asset.get("physics") or {}),
        "source": "asset_database_materialized",
        "materialized": asset.get("materialized"),
        "class_name": asset.get("ue", {}).get("class_name"),
        "repo_file": asset.get("adp", {}).get("repo_file"),
    }


def first_materialized(registry: dict[str, Any], queries: tuple[str, ...], class_name: str | None = "StaticMesh") -> dict[str, Any] | None:
    for query in queries:
        for asset in search_assets(registry, query, top_k=20, materialized_only=True):
            if class_name and str(asset.get("ue", {}).get("class_name") or "") != class_name:
                continue
            return asset
    return None


def build_default_manifest(registry: dict[str, Any]) -> dict[str, Any]:
    selection = {
        "water_plane": first_materialized(registry, ("SM_WaterPlane", "water plane")),
        "visual_ball": first_materialized(registry, ("SM_8Ball", "ball sphere", "soccer ball")),
        "sphere": first_materialized(registry, ("SM_8Ball", "ball sphere", "soccer ball")),
        "cube": first_materialized(registry, ("SM_ToothedGear_01", "SM_BigStone_01", "stone")),
        "floor": first_materialized(registry, ("SM_WoodenDisc_01", "SM_WoodenBridge_01", "floor")),
        "wall": first_materialized(registry, ("SM_WoodenFence_2m_01", "SM_WoodenPole_01")),
        "chair": first_materialized(registry, ("SM_FlowerPot", "SM_WoodenPole_03", "SM_WoodenDisc_01")),
        "table": first_materialized(registry, ("SM_Table_01", "SM_WoodenDisc_01", "table")),
        "rock": first_materialized(registry, ("SM_Stone_01", "SM_BigStone_01", "stone")),
        "bush": first_materialized(registry, ("SM_Plant_Grass_01", "SM_CoconutTree_01", "plant")),
        "traffic_cone": first_materialized(registry, ("SM_Cone", "traffic cone")),
        "market_bottle": first_materialized(registry, ("SM_Bottle_01a", "bottle")),
        "market_box": first_materialized(registry, ("SM_Apple_Box", "wood crate", "box")),
    }
    assets = {key: manifest_entry(key, asset) for key, asset in selection.items() if asset}
    scene = first_materialized(registry, ("MarketEnvironment Day", "Day"), class_name="World")
    return {
        "resolver": "asset_database_materialized_only",
        "source": "agenticdataplatform_modelscope",
        "source_path": registry.get("source_path"),
        "repo_root": registry.get("repo_root"),
        "policy": "asset_database_only_no_engine_or_startercontent_fallback",
        "registry_asset_count": registry.get("asset_count"),
        "materialized_counts": registry.get("materialized_counts"),
        "assets": assets,
        "materials": {},
        "scene": manifest_entry("scene", scene) if scene else None,
        "missing_required_keys": sorted(key for key, asset in selection.items() if not asset),
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="ADP repo root or AssetIndex/ASSETS_INDEX.json")
    parser.add_argument("--repo-root", default=None, help="Optional ADP repo root for materialized checks")
    parser.add_argument("--metadata-repo-root", default=None, help="Repo root path to write into generated metadata")
    parser.add_argument("--metadata-source-path", default=None, help="Asset index path to write into generated metadata")
    parser.add_argument(
        "--output-dir",
        default=str(Path(os.environ.get("SIM_HARNESS_WORKSPACE", DEFAULT_WORKSPACE)) / "catalog" / "adp"),
        help="Local catalog output directory outside Git.",
    )
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument(
        "--catalog-path",
        default=str(default_catalog_path()),
        help="SQLite Asset Catalog path; defaults to $SIM_HARNESS_WORKSPACE/catalog/assets/catalog.sqlite.",
    )
    parser.add_argument("--skip-sqlite", action="store_true", help="Write legacy JSON outputs without updating SQLite.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    registry = build_registry(args.source, args.repo_root, args.metadata_repo_root, args.metadata_source_path)
    manifest = build_default_manifest(registry)
    search_report = build_search_report(registry, DEFAULT_QUERIES, args.top_k)
    scenario_manifest = build_scenario_manifest(registry)
    group_index = build_group_index(registry)
    write_json(output_dir / "asset_registry.local.json", registry)
    write_json(output_dir / "full_asset_registry.json", registry)
    write_json(output_dir / "asset_database_manifest.json", manifest)
    write_json(output_dir / "gitlab_only_manifest.json", manifest)
    write_json(output_dir / "search_report.json", search_report)
    write_json(output_dir / "scenario_manifest.json", scenario_manifest)
    write_json(output_dir / "map_catalog.json", scenario_manifest)
    write_json(output_dir / "asset_group_index.json", group_index)
    catalog_stats = None
    if not args.skip_sqlite:
        catalog_stats = initialize_catalog(args.catalog_path).import_registry(registry)
    print(
        json.dumps(
            {
                "asset_count": registry["asset_count"],
                "class_counts": registry["class_counts"],
                "materialized_counts": registry["materialized_counts"],
                "map_count": scenario_manifest["map_count"],
                "missing_required_keys": manifest["missing_required_keys"],
                "sqlite_catalog": catalog_stats,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
