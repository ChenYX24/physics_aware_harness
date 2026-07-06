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
        self.assertEqual(planner.plan("a magnetic field attracts a steel ball toward a source")["primary_capability_id"], "magnetic_force_field")
        self.assertEqual(planner.plan("a heavy striker transfers momentum to a light target in a mass ratio collision")["primary_capability_id"], "mass_ratio_momentum_transfer")
        self.assertEqual(planner.plan("a crate impact transfers momentum to a lighter target")["primary_capability_id"], "mass_ratio_momentum_transfer")
        self.assertEqual(planner.plan("a spinning body slows down because of angular damping")["primary_capability_id"], "angular_damping_spin_decay")
        self.assertEqual(planner.plan("a robot pushes a box and the box moves only after the action trace")["primary_capability_id"], "agent_rigidbody_action_coupling")
        self.assertEqual(planner.plan("a pendulum keeps a fixed length distance constraint while swinging")["primary_capability_id"], "constraint_distance_pendulum_motion")
        self.assertEqual(planner.plan("a Newton's cradle transfers impulse through a suspended ball chain")["primary_capability_id"], "constraint_momentum_transfer")
        self.assertEqual(planner.plan("a compressed spring launches a payload through elastic energy release")["primary_capability_id"], "elastic_energy_launch")
        self.assertEqual(planner.plan("a bungee payload stretches an elastic rope then rebounds")["primary_capability_id"], "elastic_constraint_rebound")
        self.assertEqual(planner.plan("a brittle glass panel shatters after an impact exceeds fracture threshold")["primary_capability_id"], "brittle_impact_fracture")

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

    def test_legacy_billiard_alias_is_not_agent_facing(self) -> None:
        plan = CapabilityPlanner().plan("台球白球撞击目标球")
        self.assertEqual(plan["primary_capability_id"], "rigid_body_contact_causality")
        self.assertNotIn("billiard_causality_compiler", plan["supporting_capabilities"])


if __name__ == "__main__":
    unittest.main()
