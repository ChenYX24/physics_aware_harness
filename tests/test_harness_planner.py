from __future__ import annotations

import unittest

from harness.planning.capability_planner import CapabilityPlanner


class HarnessPlannerTests(unittest.TestCase):
    def test_prompt_maps_to_core_capabilities(self) -> None:
        planner = CapabilityPlanner()
        self.assertEqual(planner.plan("pool cue ball hits target balls")["primary_capability_id"], "billiard_causality_compiler")
        self.assertEqual(planner.plan("falling blocks under gravity")["primary_capability_id"], "rigid_body_gravity_collision")
        self.assertEqual(planner.plan("domino chain reaction")["primary_capability_id"], "sequential_contact_propagation")
        self.assertEqual(planner.plan("a ball rolls down an inclined plane ramp with friction")["primary_capability_id"], "ramp_sliding_friction")


if __name__ == "__main__":
    unittest.main()
