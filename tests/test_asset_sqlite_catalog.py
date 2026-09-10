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

    def test_reference_assets_satisfy_local_preview_minimum_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = initialize_catalog(root / "catalog.sqlite")
            catalog.import_registry(self.registry_payload(root))

            local_preview = SearchIntent.from_dict(
                {
                    "raw_query": "office chair",
                    "must": {"license_tier": "local_preview", "asset_type": "StaticMesh", "collision": True},
                }
            )
            reference = SearchIntent.from_dict(
                {
                    "raw_query": "office chair",
                    "must": {"license_tier": "reference", "asset_type": "StaticMesh", "collision": True},
                }
            )

            self.assertEqual(catalog.search(local_preview, top_k=1)[0]["asset_id"], "modern_wood_chair")
            self.assertEqual(catalog.search(reference, top_k=1)[0]["asset_id"], "modern_wood_chair")

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

            detailed = catalog.search_detailed(intent, top_k=1)
            results = [row["asset"] for row in detailed["results"]]

            self.assertEqual(results[0]["asset_id"], "modern_wood_chair")
            self.assertEqual(
                [row["category"] for row in detailed["retrieval"]["taxonomy_attempts"]],
                ["ergonomic_office_chair", "chair"],
            )

    def test_geometry_type_filters_shape_instead_of_asset_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = initialize_catalog(root / "catalog.sqlite")
            payload = self.registry_payload(root)
            sphere = {
                **payload["assets"][0],
                "asset_id": "test_sphere_mesh",
                "name": "Test Sphere Mesh",
                "semantic_name": "sphere",
                "aliases": ["round ball"],
                "tags": ["sphere", "dynamic_rigid_body"],
                "category_l1": "geometry",
                "category_l2": "primitive",
                "source_uri": "harness://tests/sphere",
                "shape": "sphere",
                "collider": "sphere",
            }
            catalog.import_registry({"assets": [sphere]})
            intent = SearchIntent.from_dict(
                {
                    "raw_query": "test sphere mesh",
                    "must": {"asset_type": "StaticMesh", "geometry_type": "sphere"},
                }
            )

            results = catalog.search(intent, top_k=1)

            self.assertEqual(results[0]["asset_id"], "test_sphere_mesh")

    def test_unknown_semantics_do_not_return_arbitrary_category_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = initialize_catalog(root / "catalog.sqlite")
            catalog.import_registry(self.registry_payload(root))
            intent = SearchIntent.from_dict(
                {
                    "raw_query": "office unicorn",
                    "taxonomy": {"category": "furniture"},
                    "must": {"asset_type": "StaticMesh", "collision": True},
                    "relaxation_policy": {"allow_parent_category": True},
                }
            )

            detailed = catalog.search_detailed(intent, top_k=1)

            self.assertEqual(detailed["results"], [])
            self.assertNotIn("category_fallback", detailed["retrieval"]["channels"])
            self.assertEqual(
                detailed["retrieval"]["match_decision"]["reason"],
                "no_semantic_evidence",
            )

    def test_empty_strict_taxonomy_reports_no_relevant_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = initialize_catalog(root / "catalog.sqlite")
            catalog.import_registry(self.registry_payload(root))
            intent = SearchIntent.from_dict(
                {
                    "raw_query": "marble funnel",
                    "taxonomy": {"category": "prop", "subcategory": "funnel"},
                    "must": {"asset_type": "StaticMesh", "collision": True},
                    "relaxation_policy": {"allow_parent_category": False},
                }
            )

            detailed = catalog.search_detailed(intent, top_k=5)

            self.assertEqual(detailed["results"], [])
            self.assertEqual(detailed["retrieval"]["match_decision"]["status"], "no_relevant_asset")
            self.assertEqual(detailed["retrieval"]["match_decision"]["reason"], "no_eligible_candidates")
            self.assertEqual(detailed["retrieval"]["taxonomy_attempts"][0]["match_status"], "no_relevant_asset")

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

    def test_delete_asset_removes_only_the_exact_catalog_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog_path = root / "catalog.sqlite"
            catalog = initialize_catalog(catalog_path)
            catalog.import_registry(self.registry_payload(root))

            result = catalog.delete_asset("modern_wood_chair")

            self.assertTrue(result["deleted"])
            self.assertEqual(result["catalog_asset_count"], 2)
            self.assertIsNone(catalog.get_asset("modern_wood_chair"))
            self.assertIsNotNone(catalog.get_asset("chair_without_collision"))
            with closing(sqlite3.connect(catalog_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM asset_search_fts WHERE asset_id = ?",
                        ("modern_wood_chair",),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM asset_aliases WHERE asset_id = ?",
                        ("modern_wood_chair",),
                    ).fetchone()[0],
                    0,
                )

            self.assertFalse(catalog.delete_asset("modern_wood_chair")["deleted"])

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
