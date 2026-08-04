from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from harness.assets.search_intent import SearchIntent
from harness.assets.sqlite_catalog import initialize_catalog


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "import_asset_release_audit",
    ROOT / "scripts" / "import_asset_release_audit.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AssetReleaseAuditImportTests(unittest.TestCase):
    def test_metadata_links_to_this_machines_content_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit_dir = root / "audit"
            content_root = root / "Content"
            chair_path = content_root / "Props" / "SM_Chair.uasset"
            material_path = content_root / "Props" / "Materials" / "M_Chair.uasset"
            chair_path.parent.mkdir(parents=True)
            material_path.parent.mkdir(parents=True)
            chair_path.write_bytes(b"chair")
            material_path.write_bytes(b"material")
            self.write_audit(audit_dir, chair_path, material_path)

            registry = MODULE.build_registry(audit_dir, content_root=content_root)

            chair = registry["assets"][0]
            missing = registry["assets"][1]
            self.assertTrue(chair["materialized"])
            self.assertTrue(chair["backend_bindings"]["unreal"]["runtime_ready"])
            self.assertEqual(chair["quality_status"], "local_preview")
            self.assertTrue(chair["files"][1]["materialized"])
            self.assertFalse(missing["materialized"])
            self.assertEqual(missing["lifecycle_status"], "discovered")

            catalog = initialize_catalog(root / "catalog.sqlite")
            first = catalog.import_registry(registry)
            second = catalog.import_registry(registry)
            self.assertEqual(first["imported_count"], 2)
            self.assertEqual(second["catalog_asset_count"], 2)
            results = catalog.search(
                SearchIntent.from_dict(
                    {"raw_query": "SM Chair", "must": {"backend": "unreal", "runtime_ready": True}}
                ),
                top_k=5,
            )
            self.assertEqual([row["asset_id"] for row in results], ["chair_asset"])

    def test_metadata_only_import_keeps_portable_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit_dir = root / "audit"
            content_root = root / "Content"
            chair_path = content_root / "Props" / "SM_Chair.uasset"
            material_path = content_root / "Props" / "Materials" / "M_Chair.uasset"
            chair_path.parent.mkdir(parents=True)
            material_path.parent.mkdir(parents=True)
            chair_path.write_bytes(b"chair")
            material_path.write_bytes(b"material")
            self.write_audit(audit_dir, chair_path, material_path)

            registry = MODULE.build_registry(audit_dir)

            chair = registry["assets"][0]
            self.assertFalse(chair["materialized"])
            self.assertEqual(chair["files"][0]["local_path"], "Props/SM_Chair.uasset")
            self.assertEqual(chair["files"][1]["local_path"], "Props/Materials/M_Chair.uasset")
            self.assertIsNone(registry["content_root"])

    def test_rejects_paths_that_escape_the_content_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "safe relative path"):
            MODULE.normalize_content_path("../Outside.uasset")
        with self.assertRaisesRegex(ValueError, "safe relative path"):
            MODULE.normalize_content_path("C:\\External\\Outside.uasset")
        with self.assertRaisesRegex(ValueError, "safe relative path"):
            MODULE.normalize_content_path("")

    def write_audit(self, audit_dir: Path, chair_path: Path, material_path: Path) -> None:
        audit_dir.mkdir(parents=True)
        assets = [
            {
                "asset_id": "chair_asset",
                "name": "SM_Chair",
                "category_l1": "furniture",
                "category_l2": "chair",
                "class_name": "StaticMesh",
                "content_file": "Props/SM_Chair.uasset",
                "dependency_files": ["Props/Materials/M_Chair.uasset"],
                "license": "UNVERIFIED_LOCAL_ENTITLEMENT",
                "source_kind": "local_ue_project",
                "source_uri": "ue://Game/Props/SM_Chair",
                "ue_path": "/Game/Props/SM_Chair.SM_Chair",
                "publication_eligible": False,
                "publication_blockers": ["missing_or_unverified_license"],
            },
            {
                "asset_id": "missing_table",
                "name": "SM_MissingTable",
                "category_l1": "furniture",
                "category_l2": "table",
                "class_name": "StaticMesh",
                "content_file": "Props/SM_MissingTable.uasset",
                "dependency_files": [],
                "license": "UNVERIFIED_LOCAL_ENTITLEMENT",
                "source_kind": "local_ue_project",
                "source_uri": "ue://Game/Props/SM_MissingTable",
                "ue_path": "/Game/Props/SM_MissingTable.SM_MissingTable",
                "publication_eligible": False,
            },
        ]
        files = [
            {
                "path": "Props/SM_Chair.uasset",
                "sha256": hashlib.sha256(chair_path.read_bytes()).hexdigest(),
                "size_bytes": chair_path.stat().st_size,
            },
            {
                "path": "Props/Materials/M_Chair.uasset",
                "sha256": hashlib.sha256(material_path.read_bytes()).hexdigest(),
                "size_bytes": material_path.stat().st_size,
            },
        ]
        (audit_dir / "assets.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in assets),
            encoding="utf-8",
        )
        (audit_dir / "files.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in files),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
