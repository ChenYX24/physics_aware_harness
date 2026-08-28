from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness.assets.ue_asset_inventory import catalog_asset_from_scan, infer_category, semantic_asset_name


class UEAssetInventoryTests(unittest.TestCase):
    def test_ue57_asset_data_path_uses_package_and_asset_names(self) -> None:
        source = Path(__file__).resolve().parents[1] / "scripts" / "native_ue_asset_inventory.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "asset_object_path"
        )
        namespace: dict[str, object] = {"Any": object}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)

        path = namespace["asset_object_path"](
            SimpleNamespace(package_name="/Game/Home/Meshes/SM_Cup", asset_name="SM_Cup")
        )

        self.assertEqual(path, "/Game/Home/Meshes/SM_Cup.SM_Cup")

    def test_semantic_name_and_category_are_generic(self) -> None:
        self.assertEqual(semantic_asset_name("SM_Dinner_table"), "Dinner table")
        self.assertEqual(infer_category("Dinner table"), ("furniture", "table"))
        self.assertEqual(infer_category("Wineglass"), ("prop", "wineglass"))

    def test_catalog_row_preserves_real_binding_and_collision_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "SM_Cup.uasset"
            package.write_bytes(b"cup")
            row = catalog_asset_from_scan(
                {
                    "name": "SM_Cup",
                    "object_path": "/Game/Home/Meshes/SM_Cup.SM_Cup",
                    "package_name": "/Game/Home/Meshes/SM_Cup",
                    "package_file": str(package),
                    "bbox_size_m": [0.1, 0.1, 0.15],
                    "lod0_section_count": 1,
                    "simple_collision_count": 2,
                    "material_paths": ["/Game/Home/Materials/M_Glass.M_Glass"],
                },
                source_uri_root="local-content://bundle",
                source_name="home",
                license_name="research_use_user_attested_nonredistributable",
                license_tier="local_preview",
            )
            self.assertEqual(row["name"], "Cup")
            self.assertEqual(row["category_l2"], "cup")
            self.assertEqual(row["collider"], "mesh")
            self.assertTrue(row["backend_bindings"]["unreal"]["runtime_ready"])
            self.assertTrue(row["mass_estimate"]["requires_case_override"])


if __name__ == "__main__":
    unittest.main()
