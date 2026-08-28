from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from harness.assets.asset_registry import AssetRegistry
from harness.assets.asset_resolver import asset_quality_gate, resolve_scene_map
from harness.assets.prepared_map_input import (
    PreparedMapInputError,
    prepare_map_input,
    qualify_map_input,
)
from harness.assets.sqlite_catalog import initialize_catalog
from harness.core.artifact_schema import write_json


class PreparedMapInputTests(unittest.TestCase):
    def test_materializes_bundle_and_promotes_only_after_matching_ue_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_content = root / "source" / "Content"
            source_map = source_content / "ExampleEnvironment" / "Maps" / "Room.umap"
            dependency = source_content / "ExampleEnvironment" / "Meshes" / "Chair.uasset"
            source_map.parent.mkdir(parents=True)
            dependency.parent.mkdir(parents=True)
            source_map.write_bytes(b"example world")
            dependency.write_bytes(b"example chair")
            project = root / "workspace" / "ue" / "Harness.uproject"
            project.parent.mkdir(parents=True)
            project.write_text("{}", encoding="utf-8")
            catalog_path = root / "workspace" / "catalog" / "catalog.sqlite"
            initialize_catalog(catalog_path)
            registry = AssetRegistry(catalog_path)
            receipt_root = root / "receipts" / "room"

            registration = prepare_map_input(
                source_content,
                map_package="/Game/ExampleEnvironment/Maps/Room.Room",
                ue_project=project,
                registration_root=receipt_root,
                registry=registry,
            )

            self.assertEqual(registration["status"], "materialized_pending_ue_qualification")
            self.assertEqual(registration["bundle_inventory"]["file_count"], 2)
            self.assertTrue((project.parent / "Content" / "ExampleEnvironment" / "Meshes" / "Chair.uasset").is_file())
            pending = registry.get_asset_by_id(registration["asset_id"])
            self.assertFalse(pending["backend_bindings"]["unreal"]["runtime_ready"])
            self.assertEqual(
                asset_quality_gate(pending, physics_critical=False, allow_local_preview=True)["status"],
                "fail",
            )

            qualification_path = receipt_root / "ue_map_qualification.json"
            write_json(
                qualification_path,
                {
                    "schema_version": "harness_prepared_map_qualification_v1",
                    "status": "pass",
                    "asset_id": registration["asset_id"],
                    "requested_package": "/Game/ExampleEnvironment/Maps/Room",
                    "opened_package": "/Game/ExampleEnvironment/Maps/Room.Room",
                    "map_file": registration["materialized_map_file"],
                    "map_sha256": hashlib.sha256(b"example world").hexdigest(),
                    "loaded_actor_count": 7,
                    "actor_class_counts": {"StaticMeshActor": 7},
                },
            )
            promotion = qualify_map_input(
                receipt_root / "prepared_map_registration.json",
                qualification_path,
                registry=registry,
            )

            self.assertTrue(promotion["runtime_ready"])
            qualified = registry.get_asset_by_id(registration["asset_id"])
            self.assertTrue(qualified["backend_bindings"]["unreal"]["runtime_ready"])
            self.assertEqual(
                asset_quality_gate(qualified, physics_critical=False, allow_local_preview=True)["status"],
                "pass_local_preview",
            )
            resolved = resolve_scene_map(
                {"scene": {"map_package": "/Game/ExampleEnvironment/Maps/Room"}},
                registry=registry,
                top_k=5,
                allow_local_preview=True,
            )
            self.assertEqual(resolved["selected_asset"]["asset_id"], registration["asset_id"])

    def test_refuses_to_overwrite_a_different_existing_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_map = root / "source" / "Content" / "Bundle" / "Maps" / "Room.umap"
            source_map.parent.mkdir(parents=True)
            source_map.write_bytes(b"source")
            project = root / "workspace" / "Harness.uproject"
            project.parent.mkdir(parents=True, exist_ok=True)
            project.write_text("{}", encoding="utf-8")
            target_map = project.parent / "Content" / "Bundle" / "Maps" / "Room.umap"
            target_map.parent.mkdir(parents=True)
            target_map.write_bytes(b"different")
            catalog_path = root / "catalog.sqlite"
            initialize_catalog(catalog_path)

            with self.assertRaises(PreparedMapInputError) as context:
                prepare_map_input(
                    root / "source" / "Content",
                    map_package="/Game/Bundle/Maps/Room",
                    ue_project=project,
                    registration_root=root / "receipt",
                    registry=AssetRegistry(catalog_path),
                )

            self.assertEqual(context.exception.code, "prepared_map_materialization_conflict")
            self.assertEqual(target_map.read_bytes(), b"different")


if __name__ == "__main__":
    unittest.main()
