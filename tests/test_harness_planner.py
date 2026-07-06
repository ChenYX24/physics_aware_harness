from __future__ import annotations

import unittest

from harness.planning.capability_planner import CapabilityPlanner


class HarnessPlannerTests(unittest.TestCase):
    def test_prompt_maps_to_core_capabilities(self) -> None:
        planner = CapabilityPlanner()
        self.assertEqual(planner.plan("pool cue ball hits target balls")["primary_capability_id"], "rigid_body_contact_causality")
        self.assertEqual(planner.plan("falling blocks under gravity")["primary_capability_id"], "rigid_body_gravity_collision")
        self.assertEqual(planner.plan("domino chain reaction")["primary_capability_id"], "sequential_contact_propagation")
        self.assertEqual(planner.plan("a ball rolls down an inclined plane ramp with friction")["primary_capability_id"], "ramp_sliding_friction")
        self.assertEqual(planner.plan("a projectile is thrown upward at a launch angle")["primary_capability_id"], "projectile_gravity_motion")
        self.assertEqual(planner.plan("a ball drops and rebounds with high restitution")["primary_capability_id"], "bounce_restitution_ball")
        self.assertEqual(planner.plan("a ball rolls on a floor and stops due to rolling friction")["primary_capability_id"], "rolling_friction_ball")


if __name__ == "__main__":
    unittest.main()
