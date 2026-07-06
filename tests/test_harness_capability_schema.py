from __future__ import annotations

import unittest

from harness.core.capability import CapabilityStore


class HarnessCapabilitySchemaTests(unittest.TestCase):
    def test_all_capabilities_are_schema_valid(self) -> None:
        capabilities = CapabilityStore().list()
        self.assertGreaterEqual(len(capabilities), 6)
        ids = {item.id for item in capabilities}
        self.assertIn("rigid_body_contact_causality", ids)
        self.assertIn("billiard_causality_compiler", ids)
        self.assertIn("asset_intent_resolution", ids)
        self.assertIn("pipeline_stage_orchestration", ids)
        self.assertIn("physics_property_constraint_validation", ids)
        self.assertIn("asset_runtime_binding_invocation", ids)
        self.assertIn("bounce_restitution_ball", ids)
        self.assertIn("rolling_friction_ball", ids)
        self.assertIn("sliding_crate_friction", ids)


if __name__ == "__main__":
    unittest.main()
