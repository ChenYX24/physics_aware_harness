from __future__ import annotations

import json
import sqlite3
import os
import re
from pathlib import Path
from typing import Any

from harness.assets.embedding_index import EmbeddingProvider
from harness.assets.search_intent import SearchIntent, asset_matches_approx_size, taxonomy_relaxation_values
from harness.assets.sqlite_catalog import SQLiteCatalog, default_catalog_path, effective_license_tier


ROOT = Path(__file__).resolve().parents[2]


class AssetRegistry:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        retrieval_config_path: str | Path | None = None,
    ) -> None:
        explicit = (
            path
            or os.environ.get("SIM_HARNESS_ASSET_CATALOG")
        )
        workspace_catalog = default_catalog_path()
        configured = explicit or (workspace_catalog if workspace_catalog.is_file() else ROOT / "assets" / "asset_physics_index.json")
        self.path = Path(configured)
        self._sqlite = (
            SQLiteCatalog(
                self.path,
                embedding_provider=embedding_provider,
                retrieval_config_path=retrieval_config_path,
            )
            if self.path.suffix.casefold() in {".sqlite", ".sqlite3", ".db"} and self.path.is_file()
            else None
        )
        self.assets = self._load_path(ROOT / "assets" / "asset_registry.example.json") if self._sqlite else self._load()

    def search(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        if not str(query).strip():
            return []
        return self.search_intent(SearchIntent(raw_query=query, semantic_text=query), top_k=top_k)

    @property
    def writable(self) -> bool:
        return self._sqlite is not None and self._sqlite.is_writable()

    def register_asset(self, asset: dict[str, Any]) -> dict[str, Any]:
        asset_id = str(asset.get("asset_id") or "").strip()
        if not asset_id:
            raise ValueError("Catalog registration requires asset_id")
        if self._sqlite is None:
            return {
                "status": "blocked",
                "code": "catalog_not_writable",
                "message": f"Provider registration requires a writable SQLite Catalog: {self.path}",
                "asset_id": asset_id,
            }
        try:
            stats = self._sqlite.import_registry([asset])
            registered = self._sqlite.get_asset(asset_id)
        except (OSError, sqlite3.Error) as exc:
            readonly_codes = {
                sqlite3.SQLITE_READONLY,
                sqlite3.SQLITE_PERM,
                sqlite3.SQLITE_CANTOPEN,
            }
            error_code = getattr(exc, "sqlite_errorcode", None)
            is_readonly = (
                isinstance(error_code, int) and error_code & 0xFF in readonly_codes
            ) or any(
                fragment in str(exc).casefold()
                for fragment in ("readonly", "read-only", "permission denied", "attempt to write")
            )
            return {
                "status": "blocked" if is_readonly else "failed",
                "code": "catalog_not_writable" if is_readonly else "catalog_registration_failed",
                "message": f"Catalog registration failed for {self.path}: {exc}",
                "asset_id": asset_id,
            }
        if registered is None:
            return {
                "status": "failed",
                "code": "catalog_registration_failed",
                "message": f"registered asset cannot be read back through AssetRegistry: {asset_id}",
                "asset_id": asset_id,
            }
        return {
            "status": "registered",
            "asset_id": asset_id,
            "changed": bool(stats["changed_count"]),
            "catalog_asset_count": stats["catalog_asset_count"],
        }

    def get_asset_by_id(self, asset_id: str) -> dict[str, Any] | None:
        identity = str(asset_id).strip()
        if not identity:
            return None
        if self._sqlite is not None:
            return self._sqlite.get_asset(identity)
        return next((item for item in self.assets if asset_identity(item) == identity), None)

    def get_assets_by_ids(self, asset_ids: list[str]) -> list[dict[str, Any]]:
        return [asset for asset_id in asset_ids if (asset := self.get_asset_by_id(asset_id)) is not None]

    def search_intent(self, intent: SearchIntent, *, top_k: int = 5) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if self._sqlite:
            results.extend(self._sqlite.search(intent, top_k=top_k))
            if len(results) >= top_k:
                return results
        if not self.assets:
            return results
        for requested_category in taxonomy_relaxation_values(intent):
            fallback = self._search_json(intent, top_k=top_k, requested_category=requested_category)
            if fallback:
                seen = {asset_identity(item) for item in results}
                results.extend(item for item in fallback if asset_identity(item) not in seen)
                return results[:top_k]
        return results[:top_k]

    def search_detailed(self, intent: SearchIntent, *, top_k: int = 5) -> dict[str, Any]:
        if self._sqlite:
            return self._sqlite.search_detailed(intent, top_k=top_k)
        assets = self.search_intent(intent, top_k=top_k)
        return {
            "results": [
                {
                    "asset": asset,
                    "score": {
                        "asset_id": asset_identity(asset),
                        "channels": {"legacy_json": {"rank": index + 1}},
                    },
                }
                for index, asset in enumerate(assets)
            ],
            "retrieval": {"backend": "legacy_json", "eligible_count": len(assets)},
        }

    def _search_json(
        self,
        intent: SearchIntent,
        *,
        top_k: int,
        requested_category: str | None,
    ) -> list[dict[str, Any]]:
        q = intent.raw_query.casefold().strip()
        tokens = [token for token in re.split(r"[^\w]+", q) if token]
        scored = []
        for item in self.assets:
            if not candidate_matches_search_intent(item, intent, requested_category=requested_category):
                continue
            text = searchable_text(item)
            exact_values = {
                str(item.get(key) or "").casefold()
                for key in ("id", "asset_id", "name", "ue_path")
            }
            aliases = {str(value).casefold() for value in item.get("aliases") or []}
            score = sum(1 for token in tokens if token in text)
            if q in exact_values:
                score += 20
            elif q in aliases:
                score += 18
            elif q and q in text:
                score += 4
            if item.get("materialized"):
                score += 1
            score += preference_score(item, intent)
            if score:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("id") or pair[1].get("name") or "")))
        return [item for _, item in scored[:top_k]]

    def _load(self) -> list[dict[str, Any]]:
        path = self.path
        if not path.exists() and self.path.name == "asset_physics_index.json":
            path = ROOT / "assets" / "asset_registry.example.json"
        return self._load_path(path)

    def _load_path(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [normalize_asset(item) for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            defaults = data.get("provenance_defaults") if isinstance(data.get("provenance_defaults"), dict) else {}
            for key in ("assets", "items", "entries"):
                if isinstance(data.get(key), list):
                    return [normalize_asset(item, defaults) for item in data[key] if isinstance(item, dict)]
        return []


def normalize_asset(item: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = {**(defaults or {}), **item}
    paths = normalized.get("paths") if isinstance(normalized.get("paths"), dict) else {}
    ue = normalized.get("ue") if isinstance(normalized.get("ue"), dict) else {}
    physics = normalized.get("physics") if isinstance(normalized.get("physics"), dict) else {}
    normalized.setdefault("asset_id", normalized.get("id") or normalized.get("name"))
    normalized.setdefault("ue_path", paths.get("ue5") or ue.get("object_path"))
    normalized.setdefault("category", normalized.get("category_l1"))
    normalized.setdefault("type", normalized.get("asset_kind") or ue.get("class_name"))
    normalized.setdefault("thumbnail", paths.get("thumbnail"))
    normalized.setdefault("mass_kg", physics.get("estimated_mass_kg"))
    normalized.setdefault("collision_profile", physics.get("collision_profile"))
    normalized.setdefault("collider", physics.get("collider"))
    if not isinstance(normalized.get("material"), dict) and isinstance(physics.get("material_properties"), dict):
        normalized["material"] = physics["material_properties"]
    if not normalized.get("source_uri") and normalized.get("source_kind") == "engine_builtin" and normalized.get("ue_path"):
        normalized["source_uri"] = f"ue://{str(normalized['ue_path']).lstrip('/')}"
    return normalized


def searchable_text(item: dict[str, Any]) -> str:
    values: list[str] = []
    for key in (
        "id",
        "asset_id",
        "name",
        "description",
        "semantic_name",
        "path",
        "ue_path",
        "tags",
        "aliases",
        "usage_groups",
        "category",
        "category_l1",
        "category_l2",
        "type",
        "collider",
        "shape",
    ):
        value = item.get(key)
        if isinstance(value, list):
            values.extend(str(entry) for entry in value)
        elif isinstance(value, dict):
            values.extend(str(entry) for entry in value.values())
        elif value is not None:
            values.append(str(value))
    return " ".join(values).casefold()


def asset_identity(item: dict[str, Any]) -> str:
    return str(item.get("asset_id") or item.get("id") or item.get("ue_path") or item.get("name") or "")


def candidate_matches_search_intent(
    item: dict[str, Any],
    intent: SearchIntent,
    *,
    requested_category: str | None = None,
) -> bool:
    must = intent.must
    ue = item.get("ue") if isinstance(item.get("ue"), dict) else {}
    asset_type = str(item.get("type") or item.get("asset_kind") or ue.get("class_name") or "").casefold()
    category_values = {
        str(item.get(key) or "").casefold()
        for key in ("category", "category_l1", "category_l2")
        if item.get(key)
    }
    source_kind = str(item.get("source_kind") or "").casefold()
    ue_path = item.get("ue_path") or ue.get("object_path")
    backend_values = _backend_values(must.get("backend"))
    if backend_values and "unreal" in backend_values and not ue_path:
        return False
    if "collision" in must and bool(must["collision"]) != bool(item.get("collider") and item.get("collision_profile")):
        return False
    if "materialized" in must and bool(must["materialized"]) != bool(item.get("materialized")):
        return False
    if "runtime_ready" in must and bool(must["runtime_ready"]) != _runtime_ready(item):
        return False
    if bool(must.get("real_3d_geometry")) and asset_type in {"image", "texture", "material", "material_only", "decal"}:
        return False
    if not _matches_value(source_kind, must.get("source_kind")):
        return False
    if not asset_matches_approx_size(item, intent):
        return False
    if not _matches_value(asset_type, must.get("asset_type", must.get("class_name"))):
        return False
    geometry_values = {
        str(value).casefold()
        for value in (item.get("shape"), item.get("collider"), asset_type)
        if value
    }
    geometry_type = must.get("geometry_type")
    if geometry_type is not None and not any(_matches_value(value, geometry_type) for value in geometry_values):
        return False
    inferred_license_tier = effective_license_tier(
        str(item.get("license") or ""),
        item.get("quality_status"),
        declared_tier=item.get("license_tier"),
        source_kind=item.get("source_kind"),
        redistribution=item.get("redistribution") or (item.get("release_audit") or {}).get("redistribution"),
    )
    if not _matches_value(inferred_license_tier, must.get("license_tier")):
        return False
    if requested_category and str(requested_category).casefold() not in {"physics_critical", "visual_only"}:
        if str(requested_category).casefold() not in category_values:
            return False
    if must.get("physics_role") is not None:
        roles = {
            str(value).casefold()
            for value in [*(item.get("tags") or []), *(item.get("usage_groups") or [])]
        }
        if not any(value in roles for value in _expected_values(must["physics_role"])):
            return False
    excluded_type = intent.must_not.get("asset_type", intent.must_not.get("class_name"))
    if excluded_type is not None and _matches_value(asset_type, excluded_type):
        return False
    excluded_geometry = intent.must_not.get("geometry_type")
    if excluded_geometry is not None and any(_matches_value(value, excluded_geometry) for value in geometry_values):
        return False
    if intent.must_not.get("license_tier") is not None and _matches_value(
        inferred_license_tier,
        intent.must_not["license_tier"],
    ):
        return False
    if intent.must_not.get("source_kind") is not None and _matches_value(source_kind, intent.must_not["source_kind"]):
        return False
    excluded_backends = _backend_values(intent.must_not.get("backend"))
    if "unreal" in excluded_backends and ue_path:
        return False
    excluded_categories = _expected_values(intent.must_not.get("category"))
    if excluded_categories.intersection(category_values):
        return False
    return True


def preference_score(item: dict[str, Any], intent: SearchIntent) -> float:
    text = searchable_text(item)
    score = 0.0
    for preference in intent.should:
        value = preference.value
        values = value if isinstance(value, list) else [value]
        if any(str(candidate).casefold() in text for candidate in values):
            score += preference.weight * preference.confidence
    return score


def _runtime_ready(item: dict[str, Any]) -> bool:
    bindings = item.get("backend_bindings")
    if isinstance(bindings, dict):
        unreal = next(
            (
                value
                for backend, value in bindings.items()
                if str(backend).casefold() == "ue"
                or str(backend).casefold().startswith("ue_")
                or str(backend).casefold().startswith("unreal")
            ),
            None,
        )
        if isinstance(unreal, dict) and "runtime_ready" in unreal:
            return bool(unreal["runtime_ready"])
    source_kind = str(item.get("source_kind") or "")
    return bool(item.get("ue_path") and (item.get("materialized") or source_kind in {"engine_builtin", "analytic_proxy"}))


def _matches_value(actual: str, expected: Any) -> bool:
    if expected is None:
        return True
    return actual in _expected_values(expected)


def _expected_values(value: Any) -> set[str]:
    if value is None:
        return set()
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return {str(item).casefold() for item in values}


def _backend_values(value: Any) -> set[str]:
    return {"unreal" if item == "ue" else item for item in _expected_values(value)}
