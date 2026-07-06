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
        self.assertIn("prompt_case_capability_planning", ids)
        self.assertIn("explicit_physics_control_surface", ids)
        self.assertIn("physics_verifier_truth_gate", ids)
        self.assertIn("canonical_signal_capture", ids)
        self.assertIn("dataset_artifact_packaging", ids)
        self.assertIn("asset_intent_resolution", ids)
        self.assertIn("pipeline_stage_orchestration", ids)
        self.assertIn("physics_property_constraint_validation", ids)
        self.assertIn("asset_runtime_binding_invocation", ids)
        self.assertIn("bounce_restitution_ball", ids)
        self.assertIn("rolling_friction_ball", ids)
        self.assertIn("sliding_crate_friction", ids)
        self.assertIn("force_field_wind_drift", ids)
        self.assertIn("mass_ratio_momentum_transfer", ids)
        self.assertIn("angular_damping_spin_decay", ids)
        alias = next(item for item in capabilities if item.id == "billiard_causality_compiler")
        self.assertEqual(alias.capability_type, "compatibility_alias")
        self.assertEqual(alias.deprecated_by, "rigid_body_contact_causality")
        contact = next(item for item in capabilities if item.id == "rigid_body_contact_causality")
        self.assertEqual(contact.capability_type, "physics_constraint")


if __name__ == "__main__":
    unittest.main()
