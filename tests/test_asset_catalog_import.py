from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness.assets.sqlite_catalog import initialize_catalog


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("import_adp_asset_index", ROOT / "scripts" / "import_adp_asset_index.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AssetCatalogImportTests(unittest.TestCase):
    def test_catalog_groups_dependencies_and_map_preview_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "AssetIndex" / "thumbnails").mkdir(parents=True)
            (source / "Content" / "Maps").mkdir(parents=True)
            (source / "Content" / "Props").mkdir(parents=True)
            (source / "Content" / "Maps" / "Day.umap").write_bytes(b"map")
            (source / "Content" / "Props" / "Ball.uasset").write_bytes(b"asset")
            (source / "AssetIndex" / "thumbnails" / "ball.png").write_bytes(b"png")
            (source / "AssetIndex" / "thumbnails" / "Game__Maps__Day.png").write_bytes(b"png")
            index = {
                "/Game/Maps/Day": {"asset_name": "Day", "ue_class": "World", "category": "Maps", "dependencies": []},
                "/Game/Props/Ball": {
                    "asset_name": "Ball",
                    "semantic_name": "billiard ball",
                    "full_description": "Glossy resin billiard ball",
                    "ue_class": "StaticMesh",
                    "category": "Props",
                    "tags": ["ball", "billiard"],
                    "thumbnail": "AssetIndex/thumbnails/ball.png",
                    "dependencies": ["/Game/Props/Materials/M_Ball"],
                    "estimated_mass_kg": 0.17,
                },
            }
            (source / "AssetIndex" / "ASSETS_INDEX.json").write_text(json.dumps(index), encoding="utf-8")

            registry = MODULE.build_registry(source)
            groups = MODULE.build_group_index(registry)
            maps = MODULE.build_scenario_manifest(registry)

            ball = next(asset for asset in registry["assets"] if asset["name"] == "Ball")
            day = next(asset for asset in registry["assets"] if asset["name"] == "Day")
            self.assertTrue(ball["materialized"])
            self.assertTrue(ball["paths"]["thumbnail"].endswith("ball.png"))
            self.assertTrue(day["paths"]["thumbnail"].endswith("Game__Maps__Day.png"))
            self.assertIn(ball["asset_id"], groups["usage_groups"]["prop/ball"])
            self.assertEqual(maps["schema_version"], "map_catalog.v1")
            self.assertEqual(maps["maps"][0]["preview_presets"][2]["runtime_status"], "planned_unverified")
            self.assertEqual(ball["sha256"], "d59386e0ae435e292fbe0ebcdb954b75ed5fb3922091277cb19f798fc5d50718")
            self.assertFalse(ball["backend_bindings"]["unreal"]["runtime_ready"])

            dependency_path = source / "Content" / "Props" / "Materials" / "M_Ball.uasset"
            dependency_path.parent.mkdir(parents=True)
            dependency_path.write_bytes(b"material")
            registry = MODULE.build_registry(source)
            ball = next(asset for asset in registry["assets"] if asset["name"] == "Ball")
            self.assertTrue(ball["backend_bindings"]["unreal"]["runtime_ready"])
            self.assertEqual(
                ball["bundle"]["dependencies"][0]["sha256"],
                MODULE.sha256_file(dependency_path),
            )

            catalog = initialize_catalog(source / "catalog.sqlite")
            first = catalog.import_registry(registry)
            second = catalog.import_registry(registry)
            self.assertEqual(first["imported_count"], 2)
            self.assertEqual(second["catalog_asset_count"], 2)

            cli_catalog = source / "cli_catalog.sqlite"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "import_adp_asset_index.py"),
                    "--source",
                    str(source),
                    "--repo-root",
                    str(source),
                    "--output-dir",
                    str(source / "catalog_output"),
                    "--catalog-path",
                    str(cli_catalog),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["sqlite_catalog"]["catalog_asset_count"], 2)
            self.assertTrue(cli_catalog.is_file())


if __name__ == "__main__":
    unittest.main()
