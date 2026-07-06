from __future__ import annotations

import unittest

from harness.planning.capability_planner import CapabilityPlanner


class HarnessPlannerTests(unittest.TestCase):
    def test_prompt_maps_to_core_capabilities(self) -> None:
        planner = CapabilityPlanner()
        billiards_plan = planner.plan("pool cue ball hits target balls")
        self.assertEqual(billiards_plan["primary_capability_id"], "rigid_body_contact_causality")
        self.assertEqual(planner.plan("falling blocks under gravity")["primary_capability_id"], "rigid_body_gravity_collision")
        self.assertEqual(planner.plan("domino chain reaction")["primary_capability_id"], "sequential_contact_propagation")
        self.assertEqual(planner.plan("a ball rolls down an inclined plane ramp with friction")["primary_capability_id"], "ramp_sliding_friction")
        self.assertEqual(planner.plan("a projectile is thrown upward at a launch angle")["primary_capability_id"], "projectile_gravity_motion")
        self.assertEqual(planner.plan("a ball drops and rebounds with high restitution")["primary_capability_id"], "bounce_restitution_ball")
        self.assertEqual(planner.plan("a ball rolls on a floor and stops due to rolling friction")["primary_capability_id"], "rolling_friction_ball")
        self.assertEqual(planner.plan("a sliding crate slows down due to sliding friction")["primary_capability_id"], "sliding_crate_friction")
        self.assertEqual(planner.plan("a balloon drifts in a steady wind field")["primary_capability_id"], "force_field_wind_drift")
        self.assertEqual(planner.plan("a heavy striker transfers momentum to a light target in a mass ratio collision")["primary_capability_id"], "mass_ratio_momentum_transfer")
        self.assertEqual(planner.plan("a crate impact transfers momentum to a lighter target")["primary_capability_id"], "mass_ratio_momentum_transfer")
        self.assertEqual(planner.plan("a spinning body slows down because of angular damping")["primary_capability_id"], "angular_damping_spin_decay")

    def test_planner_returns_layered_harness_capabilities(self) -> None:
        plan = CapabilityPlanner().plan("a bowling ball hits passive pins through contact")
        self.assertEqual(plan["primary_capability_id"], "rigid_body_contact_causality")
        layers = plan["capability_layers"]
        stage_ids = {item["capability_id"] for item in layers["pipeline_stages"]}
        self.assertIn("prompt_case_capability_planning", stage_ids)
        self.assertIn("asset_intent_resolution", stage_ids)
        self.assertIn("asset_runtime_binding_invocation", stage_ids)
        self.assertIn("physics_verifier_truth_gate", stage_ids)
        constraint_ids = {item["capability_id"] for item in layers["physics_constraints"]}
        self.assertIn("rigid_body_contact_causality", constraint_ids)
        self.assertIn("physics_property_constraint_validation", constraint_ids)
        self.assertNotEqual(plan["primary_capability_id"], "billiard_causality_compiler")


if __name__ == "__main__":
    unittest.main()
