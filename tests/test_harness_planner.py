from __future__ import annotations

import unittest

from harness.planning.capability_planner import CapabilityPlanner


class HarnessPlannerTests(unittest.TestCase):
    def test_named_rigid_processes_share_one_domain(self) -> None:
        planner = CapabilityPlanner()
        prompts = (
            "pool cue ball hits target balls",
            "falling blocks under gravity",
            "domino chain reaction",
            "a projectile rebounds from a ramp",
            "a brittle object collides and fractures",
            "a pendulum constraint swings",
        )
        self.assertEqual({planner.plan(prompt)["primary_capability_id"] for prompt in prompts}, {"rigid_body_dynamics"})
        self.assertEqual({planner.plan(prompt)["scene_domain"] for prompt in prompts}, {"rigid_body"})

    def test_only_state_representation_changes_domain(self) -> None:
        planner = CapabilityPlanner()
        self.assertEqual(planner.plan("water represented with SPH particles")["primary_capability_id"], "fluid_particle_dynamics")
        self.assertEqual(planner.plan("deformable cloth mesh")["primary_capability_id"], "deformable_body_dynamics")

    def test_planner_returns_reusable_pipeline_stages(self) -> None:
        plan = CapabilityPlanner().plan("a bowling ball hits pins")
        stage_ids = {item["capability_id"] for item in plan["capability_layers"]["pipeline_stages"]}
        self.assertIn("prompt_case_capability_planning", stage_ids)
        self.assertIn("asset_intent_resolution", stage_ids)
        self.assertIn("runtime_backend_execution", stage_ids)
        self.assertIn("physics_verifier_truth_gate", stage_ids)
        self.assertNotIn("sequential_contact_propagation", plan["capability_layers"]["all_capability_ids"])


if __name__ == "__main__":
    unittest.main()
