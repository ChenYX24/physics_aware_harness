from __future__ import annotations

import unittest

from harness.core.capability import CapabilityStore, canonical_capability_id


class HarnessCapabilitySchemaTests(unittest.TestCase):
    def test_active_taxonomy_uses_generic_execution_domains(self) -> None:
        store = CapabilityStore()
        capabilities = store.list()
        by_id = {item.id: item for item in capabilities}
        for capability_id in ("rigid_body_dynamics", "fluid_particle_dynamics", "deformable_body_dynamics"):
            self.assertIn(capability_id, by_id)
            self.assertNotEqual(by_id[capability_id].capability_type, "compatibility_alias")
        self.assertEqual(by_id["rigid_body_gravity_collision"].capability_type, "compatibility_alias")
        self.assertEqual(by_id["sequential_contact_propagation"].deprecated_by, "rigid_body_dynamics")
        taxonomy = store.taxonomy()
        self.assertNotIn("rigid_body_gravity_collision", taxonomy["physics_behavior_capabilities"])
        self.assertIn("runtime_backend_execution", taxonomy["pipeline_stage_capabilities"])

    def test_deprecated_scene_alias_remains_metadata_only(self) -> None:
        self.assertEqual(canonical_capability_id("billiard_causality_compiler"), "rigid_body_contact_causality")


if __name__ == "__main__":
    unittest.main()
