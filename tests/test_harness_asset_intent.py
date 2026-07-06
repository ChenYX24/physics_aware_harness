from __future__ import annotations

import unittest

from harness.assets.asset_intent import intent_from_object
from harness.assets.asset_resolver import resolve_asset_intents


class HarnessAssetIntentTests(unittest.TestCase):
    def test_physics_critical_and_visual_only_classification(self) -> None:
        rigid = intent_from_object({"id": "ball", "role": "passive_target", "shape": "sphere"})
        visual = intent_from_object({"id": "label", "role": "decal", "asset_query": "logo decal"})
        self.assertTrue(rigid.physics_critical)
        self.assertIn("collider", rigid.required_properties)
        self.assertFalse(visual.physics_critical)
        self.assertEqual(visual.category, "visual_only")

    def test_ramp_roles_are_physics_critical(self) -> None:
        subject = intent_from_object({"id": "ramp_subject", "role": "rolling_subject", "shape": "sphere"})
        ramp = intent_from_object({"id": "ramp", "role": "ramp", "shape": "inclined_plane"})
        self.assertTrue(subject.physics_critical)
        self.assertTrue(ramp.physics_critical)

    def test_example_registry_resolves_core_static_scene_assets(self) -> None:
        case_spec = {
            "case_id": "asset_smoke",
            "objects": [
                {"id": "cue_ball", "role": "active_striker", "shape": "sphere"},
                {"id": "ramp", "role": "ramp", "shape": "inclined_plane"},
            ],
        }
        result = resolve_asset_intents(case_spec, top_k=2)
        self.assertEqual(result["case_id"], "asset_smoke")
        self.assertEqual(len(result["assets"]), 2)
        self.assertTrue(all(row["selected_asset"] for row in result["assets"]))


if __name__ == "__main__":
    unittest.main()
