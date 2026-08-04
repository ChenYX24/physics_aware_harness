from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from harness.assets.asset_registry import AssetRegistry
from harness.assets.search_intent import SearchIntent
from harness.assets.sqlite_catalog import CATALOG_SCHEMA_VERSION, SQLiteCatalog, default_catalog_path, effective_license_tier, infer_license_tier, initialize_catalog


class AssetSQLiteCatalogTests(unittest.TestCase):
    def test_schema_import_and_search_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self.registry_payload(root)
            catalog_path = root / "catalog.sqlite"
            catalog = initialize_catalog(catalog_path)

            first = catalog.import_registry(payload)
            second = catalog.import_registry(payload)

            self.assertEqual(first["imported_count"], 3)
            self.assertEqual(second["catalog_asset_count"], 3)
            with closing(sqlite3.connect(catalog_path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], CATALOG_SCHEMA_VERSION)
                self.assertEqual(connection.execute("SELECT count(*) FROM asset_aliases").fetchone()[0], 10)
                self.assertEqual(connection.execute("SELECT count(*) FROM asset_search_fts").fetchone()[0], 3)

            registry = AssetRegistry(catalog_path)
            self.assertEqual(registry.search("modern_wood_chair", top_k=1)[0]["asset_id"], "modern_wood_chair")
            self.assertEqual(registry.search("办公椅", top_k=1)[0]["asset_id"], "modern_wood_chair")
            self.assertEqual(registry.search("comfortable wooden seating", top_k=1)[0]["asset_id"], "modern_wood_chair")
            self.assertEqual(registry.search("passive_target sphere", top_k=1)[0]["source_kind"], "engine_builtin")

    def test_hard_conditions_remove_semantically_similar_invalid_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = initialize_catalog(root / "catalog.sqlite")
            catalog.import_registry(self.registry_payload(root))
            intent = SearchIntent.from_dict(
                {
                    "raw_query": "office chair",
                    "must": {
                        "backend": "unreal",
                        "real_3d_geometry": True,
                        "collision": True,
                        "approx_size_m": [0.6, 0.6, 1.0],
                    },
                    "must_not": {"asset_type": ["texture", "material_only"]},
                }
            )

            results = catalog.search(intent, top_k=5)

            self.assertEqual([row["asset_id"] for row in results], ["modern_wood_chair"])

    def test_taxonomy_relaxes_from_object_type_to_parent_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = initialize_catalog(root / "catalog.sqlite")
            catalog.import_registry(self.registry_payload(root))
            intent = SearchIntent.from_dict(
                {
                    "raw_query": "office chair",
                    "taxonomy": {
                        "category": "furniture",
                        "subcategory": "chair",
                        "object_type": "ergonomic_office_chair",
                    },
                    "must": {"backend": "unreal", "collision": True, "real_3d_geometry": True},
                    "relaxation_policy": {"allow_parent_category": True},
                }
            )

            results = catalog.search(intent, top_k=1)

            self.assertEqual(results[0]["asset_id"], "modern_wood_chair")

    def test_reimport_updates_rows_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = initialize_catalog(root / "catalog.sqlite")
            payload = self.registry_payload(root)
            catalog.import_registry(payload)
            payload["assets"][0]["name"] = "Renamed Office Chair"

            stats = catalog.import_registry(payload)

            self.assertEqual(stats["catalog_asset_count"], 3)
            result = catalog.search(SearchIntent(raw_query="Renamed Office Chair"), top_k=1)
            self.assertEqual(result[0]["name"], "Renamed Office Chair")

    def test_v1_catalog_migrates_to_embedding_schema_without_sqlite_vec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.sqlite"
            initialize_catalog(path)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("DROP TABLE vector_index_state")
                connection.execute("DROP TABLE asset_embeddings")
                connection.execute("DROP TABLE embedding_models")
                connection.execute("DELETE FROM catalog_migrations WHERE version = 2")
                connection.execute("PRAGMA user_version = 1")
                connection.commit()

            SQLiteCatalog(path)

            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], CATALOG_SCHEMA_VERSION)
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            self.assertIn("asset_embeddings", tables)
            self.assertIn("vector_index_state", tables)

    def test_default_catalog_path_uses_workspace(self) -> None:
        with patch.dict(os.environ, {"SIM_HARNESS_WORKSPACE": "/Volumes/TestWorkspace"}):
            self.assertEqual(
                default_catalog_path(),
                Path("/Volumes/TestWorkspace/catalog/assets/catalog.sqlite"),
            )

    def test_reference_license_tier_requires_explicit_authorization(self) -> None:
        self.assertEqual(infer_license_tier("All Rights Reserved", "approved"), "local_preview")
        self.assertEqual(infer_license_tier("CC0-1.0", "approved"), "reference")
        self.assertEqual(infer_license_tier("Unreal Engine EULA", "approved_proxy"), "local_preview")
        self.assertEqual(
            infer_license_tier("Unreal Engine EULA", "approved_proxy", source_kind="engine_builtin"),
            "reference",
        )
        self.assertEqual(
            effective_license_tier(
                "All Rights Reserved",
                "approved",
                declared_tier="reference",
            ),
            "local_preview",
        )
        self.assertEqual(
            infer_license_tier(
                "Custom License",
                "approved",
                redistribution={
                    "allowed": True,
                    "rights_holder": "Example Studio",
                    "evidence_uri": "https://example.invalid/license-evidence",
                    "verified_at": "2026-08-03T00:00:00Z",
                },
            ),
            "reference",
        )

    def registry_payload(self, root: Path) -> dict[str, object]:
        chair_file = root / "Chair.uasset"
        chair_file.write_bytes(b"chair asset")
        chair_hash = hashlib.sha256(chair_file.read_bytes()).hexdigest()
        base_physics = {
            "collider": "mesh",
            "mass_kg": 8.0,
            "material": {"static_friction": 0.5},
            "collision_profile": "PhysicsActor",
        }
        chair = {
            "asset_id": "modern_wood_chair",
            "name": "Modern Wood Chair",
            "semantic_name": "office chair",
            "description": "Comfortable wooden seating for a modern office",
            "aliases": ["办公椅", "木椅"],
            "tags": ["chair", "wood", "modern"],
            "category_l1": "furniture",
            "category_l2": "chair",
            "type": "StaticMesh",
            "bbox_size_m": [0.6, 0.6, 1.0],
            "source_kind": "harness_generated",
            "source_uri": "harness://tests/chair",
            "license": "CC0-1.0",
            "quality_status": "approved",
            "materialized": True,
            "sha256": chair_hash,
            "paths": {"local_file": str(chair_file), "ue5": "/Game/Props/Chair.Chair"},
            "ue_path": "/Game/Props/Chair.Chair",
            "ue": {"object_path": "/Game/Props/Chair.Chair", "class_name": "StaticMesh", "dependencies": []},
            "backend_bindings": {
                "ue_5_7": {
                    "object_path": "/Game/Props/Chair.Chair",
                    "class_name": "StaticMesh",
                    "materialized": True,
                    "runtime_ready": True,
                }
            },
            **base_physics,
        }
        no_collision = {
            **chair,
            "asset_id": "chair_without_collision",
            "name": "Chair Without Collision",
            "aliases": ["office chair no collision"],
            "source_uri": "harness://tests/chair_without_collision",
            "ue_path": "/Game/Props/ChairNoCollision.ChairNoCollision",
            "collider": None,
            "collision_profile": None,
        }
        texture = {
            **chair,
            "asset_id": "chair_reference_texture",
            "name": "Office Chair Texture",
            "aliases": ["office chair image"],
            "source_uri": "harness://tests/chair_texture",
            "type": "texture",
            "ue_path": "/Game/Textures/T_Chair.T_Chair",
        }
        return {"schema_version": "asset_registry.v3", "assets": [chair, no_collision, texture]}


if __name__ == "__main__":
    unittest.main()
