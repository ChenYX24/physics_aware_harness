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

    def test_bounce_role_is_physics_critical(self) -> None:
        subject = intent_from_object({"id": "bounce_ball", "role": "bouncing_body", "shape": "sphere"})
        self.assertTrue(subject.physics_critical)
        self.assertIn("rigid_body", subject.required_properties)

    def test_rolling_role_is_physics_critical(self) -> None:
        subject = intent_from_object({"id": "rolling_ball", "role": "rolling_body", "shape": "sphere"})
        self.assertTrue(subject.physics_critical)
        self.assertIn("collider", subject.required_properties)

    def test_sliding_role_is_physics_critical(self) -> None:
        subject = intent_from_object({"id": "sliding_crate", "role": "sliding_body", "shape": "box"})
        self.assertTrue(subject.physics_critical)
        self.assertIn("rigid_body", subject.required_properties)

    def test_wind_role_is_physics_critical(self) -> None:
        subject = intent_from_object({"id": "balloon", "role": "wind_drift_body", "shape": "sphere"})
        self.assertTrue(subject.physics_critical)
        self.assertIn("collision_profile", subject.required_properties)

    def test_spinning_body_role_is_physics_critical(self) -> None:
        subject = intent_from_object({"id": "spinner", "role": "spinning_body", "shape": "sphere"})
        self.assertTrue(subject.physics_critical)
        self.assertIn("rigid_body", subject.required_properties)

    def test_agent_action_roles_are_physics_critical(self) -> None:
        agent = intent_from_object({"id": "agent", "role": "active_agent", "shape": "capsule"})
        target = intent_from_object({"id": "box", "role": "action_coupled_body", "shape": "box"})
        self.assertTrue(agent.physics_critical)
        self.assertTrue(target.physics_critical)
        self.assertIn("collision_profile", target.required_properties)

    def test_constraint_roles_are_physics_critical(self) -> None:
        anchor = intent_from_object({"id": "anchor", "role": "constraint_anchor", "shape": "fixed_point"})
        bob = intent_from_object({"id": "bob", "role": "constrained_body", "shape": "sphere"})
        self.assertTrue(anchor.physics_critical)
        self.assertTrue(bob.physics_critical)
        self.assertIn("collider", bob.required_properties)

    def test_impulse_chain_roles_are_physics_critical(self) -> None:
        driver = intent_from_object({"id": "driver", "role": "active_chain_driver", "shape": "sphere"})
        receiver = intent_from_object({"id": "receiver", "role": "constrained_chain_body", "shape": "sphere"})
        self.assertTrue(driver.physics_critical)
        self.assertTrue(receiver.physics_critical)

    def test_elastic_launch_roles_are_physics_critical(self) -> None:
        launcher = intent_from_object({"id": "spring", "role": "elastic_launcher", "shape": "spring_proxy"})
        payload = intent_from_object({"id": "payload", "role": "launched_body", "shape": "sphere"})
        self.assertTrue(launcher.physics_critical)
        self.assertTrue(payload.physics_critical)
        self.assertIn("collision_profile", launcher.required_properties)

    def test_elastic_constraint_roles_are_physics_critical(self) -> None:
        anchor = intent_from_object({"id": "anchor", "role": "elastic_constraint_anchor", "shape": "fixed_point"})
        payload = intent_from_object({"id": "payload", "role": "elastic_constrained_body", "shape": "sphere"})
        tether = intent_from_object({"id": "tether", "role": "elastic_tether_constraint", "shape": "constraint"})
        self.assertTrue(anchor.physics_critical)
        self.assertTrue(payload.physics_critical)
        self.assertTrue(tether.physics_critical)

    def test_example_registry_resolves_core_static_scene_assets(self) -> None:
        case_spec = {
            "case_id": "asset_smoke",
            "objects": [
                {"id": "cue_ball", "role": "active_striker", "shape": "sphere"},
                {"id": "ramp", "role": "ramp", "shape": "inclined_plane"},
                {"id": "projectile", "role": "projectile", "shape": "sphere"},
                {"id": "bounce_ball", "role": "bouncing_body", "shape": "sphere"},
                {"id": "rolling_ball", "role": "rolling_body", "shape": "sphere"},
                {"id": "sliding_crate", "role": "sliding_body", "shape": "box"},
                {"id": "wind_body", "role": "wind_drift_body", "shape": "sphere"},
                {"id": "spinner", "role": "spinning_body", "shape": "sphere"},
                {"id": "agent", "role": "active_agent", "shape": "capsule"},
                {"id": "payload", "role": "action_coupled_body", "shape": "box"},
                {"id": "anchor", "role": "constraint_anchor", "shape": "fixed_point"},
                {"id": "bob", "role": "constrained_body", "shape": "sphere"},
                {"id": "chain_driver", "role": "active_chain_driver", "shape": "sphere"},
                {"id": "chain_receiver", "role": "constrained_chain_body", "shape": "sphere"},
                {"id": "spring", "role": "elastic_launcher", "shape": "spring_proxy"},
                {"id": "spring_payload", "role": "launched_body", "shape": "sphere"},
                {"id": "elastic_anchor", "role": "elastic_constraint_anchor", "shape": "fixed_point"},
                {"id": "elastic_payload", "role": "elastic_constrained_body", "shape": "sphere"},
                {"id": "elastic_tether", "role": "elastic_tether_constraint", "shape": "constraint"},
            ],
        }
        result = resolve_asset_intents(case_spec, top_k=2)
        self.assertEqual(result["case_id"], "asset_smoke")
        self.assertEqual(result["capability_id"], "asset_intent_resolution")
        self.assertEqual(result["invocation_contract"]["next_capability_id"], "asset_runtime_binding_invocation")
        self.assertEqual(result["physics_critical_count"], 19)
        self.assertEqual(len(result["assets"]), 19)
        self.assertTrue(all(row["selected_asset"] for row in result["assets"]))
        self.assertTrue(all(row["runtime_binding_requirements"] for row in result["assets"]))


if __name__ == "__main__":
    unittest.main()
