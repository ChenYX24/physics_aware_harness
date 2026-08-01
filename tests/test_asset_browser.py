from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.artifact_schema import write_json
from scripts.harness_asset_browser import (
    build_asset_view,
    filter_assets,
    load_binding_evidence,
)


class AssetBrowserTests(unittest.TestCase):
    def test_local_asset_keeps_preview_and_binding_claims_separate(self) -> None:
        asset = {
            "asset_id": "local_glass",
            "name": "Local Glass",
            "semantic_name": "glass panel",
            "category_l1": "prop",
            "type": "StaticMesh",
            "source_kind": "local_ue_project",
            "source_uri": "ue://Game/Props/Glass",
            "license": "UNVERIFIED_LOCAL_ENTITLEMENT",
            "quality_status": "local_preview",
            "ue_path": "/Game/Props/Glass.Glass",
            "collider": "box",
            "mass_kg": 5.0,
            "material": {},
            "collision_profile": "PhysicsActor",
            "materialized": True,
            "ue": {"dependencies": ["/Game/Materials/Glass"]},
            "adp": {"dependency_materialized_count": 1},
        }
        view = build_asset_view(asset, {})
        self.assertEqual(view["qualification"], "local_preview")
        self.assertEqual(view["binding_status"], "catalog_ready")
        self.assertFalse(view["reference_gate"]["reference_approved"])
        self.assertTrue(view["dependencies_ready"])

    def test_runtime_report_upgrades_binding_evidence_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "runtime_actor_placement.json"
            write_json(
                report,
                {
                    "case_id": "glass_case",
                    "actor_bindings": [
                        {
                            "object_id": "panel",
                            "runtime_actor_id": "actor_panel",
                            "asset": {
                                "selected_asset_id": "local_glass",
                                "binding_source": "ue_asset",
                                "runtime_usage": "collision_and_visual",
                            },
                            "physics": {
                                "collision_geometry_verification": "runtime_controlled"
                            },
                        }
                    ],
                },
            )
            evidence = load_binding_evidence([report])
        view = build_asset_view(
            {
                "asset_id": "local_glass",
                "type": "StaticMesh",
                "source_kind": "local_ue_project",
                "source_uri": "ue://Game/Props/Glass",
                "license": "UNVERIFIED_LOCAL_ENTITLEMENT",
                "quality_status": "local_preview",
                "ue_path": "/Game/Props/Glass.Glass",
                "collider": "box",
                "mass_kg": 5.0,
                "material": {},
                "collision_profile": "PhysicsActor",
                "materialized": True,
            },
            evidence,
        )
        self.assertEqual(view["binding_status"], "runtime_verified")
        self.assertEqual(view["runtime_evidence"][0]["case_id"], "glass_case")
        self.assertEqual(view["qualification"], "local_preview")

    def test_filters_are_conjunctive(self) -> None:
        rows = [
            {
                "asset_id": "glass",
                "_search": "glass panel prop",
                "category": "prop",
                "qualification": "local_preview",
                "binding_status": "runtime_verified",
                "source_kind": "local_ue_project",
            },
            {
                "asset_id": "chair",
                "_search": "wood chair furniture",
                "category": "furniture",
                "qualification": "local_preview",
                "binding_status": "catalog_ready",
                "source_kind": "local_ue_project",
            },
        ]
        filtered = filter_assets(
            rows,
            query="glass panel",
            category="prop",
            binding="runtime_verified",
        )
        self.assertEqual([row["asset_id"] for row in filtered], ["glass"])


if __name__ == "__main__":
    unittest.main()
