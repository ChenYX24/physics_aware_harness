from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UEOrientedBoxContactTests(unittest.TestCase):
    def test_runtime_driver_uses_box_sat_instead_of_actor_aabb(self) -> None:
        source = (
            ROOT
            / "ue_template"
            / "Plugins"
            / "ADPPhysicsRuntime"
            / "Source"
            / "ADPPhysicsRuntime"
            / "Private"
            / "ADPPhysicsRuntimeDriver.cpp"
        ).read_text(encoding="utf-8")
        header = (
            ROOT
            / "ue_template"
            / "Plugins"
            / "ADPPhysicsRuntime"
            / "Source"
            / "ADPPhysicsRuntime"
            / "Public"
            / "ADPPhysicsRuntimeDriver.h"
        ).read_text(encoding="utf-8")

        self.assertIn("OrientedBoxSignedMargin", source)
        self.assertIn("adp_cpp_runtime_oriented_box_sat", source)
        self.assertIn("SignedMarginCm > ContactToleranceCm", source)
        self.assertIn("bool bCollisionEnabled", header)
        self.assertGreaterEqual(source.count("BodyConfigs.Last().bCollisionEnabled = bCollisionEnabled"), 2)
        self.assertIn("!A.bCollisionEnabled || !B.bCollisionEnabled", source)
        self.assertNotIn("GetActorBounds(false, OriginA", source)

    def test_native_scene_registers_explicit_collider_kind(self) -> None:
        source = (ROOT / "scripts" / "native_ue_scene.py").read_text(encoding="utf-8")

        self.assertIn("register_static_body_with_collider", source)
        self.assertIn("register_body_meters_with_collider", source)
        self.assertGreaterEqual(source.count('properties.get("collider")'), 2)


if __name__ == "__main__":
    unittest.main()
