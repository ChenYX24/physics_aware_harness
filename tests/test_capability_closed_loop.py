from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.capability_closed_loop import run_closed_loop_demo
from tools.capability_planner import CapabilityPlanner
from tools.capability_verifier import CapabilityVerifier
from tools.failure_taxonomy import VALID_FAILURE_TYPES


ROOT = Path(__file__).resolve().parents[1]


class CapabilityClosedLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = CapabilityPlanner(ROOT / "config" / "harness_capability_profile.json")
        self.verifier = CapabilityVerifier()

    def test_capability_profile_can_be_read_by_planner(self) -> None:
        self.assertTrue(self.planner.profile.has("rigid_body_contact_causality"))
        self.assertTrue(self.planner.profile.has("billiard_causality_compiler"))
        self.assertTrue(self.planner.profile.has("rigid_body_gravity_collision"))
        self.assertTrue(self.planner.profile.has("sequential_contact_propagation"))
        self.assertTrue(self.planner.profile.has("bounce_restitution_ball"))
        self.assertTrue(self.planner.profile.has("rolling_friction_ball"))

    def test_billiards_prompt_maps_to_generic_contact_causality(self) -> None:
        plan = self.planner.plan("A pool table with a cue ball hitting passive target balls.")
        self.assertEqual(plan["primary_capability_id"], "rigid_body_contact_causality")

    def test_falling_blocks_prompt_maps_to_gravity_collision(self) -> None:
        plan = self.planner.plan("Falling blocks under gravity collide with the ground.")
        self.assertEqual(plan["primary_capability_id"], "rigid_body_gravity_collision")

    def test_domino_prompt_maps_to_sequential_contact(self) -> None:
        plan = self.planner.plan("A domino chain reaction tips each block through sequential contact.")
        self.assertEqual(plan["primary_capability_id"], "sequential_contact_propagation")

    def test_verifier_rejects_passive_target_pre_contact_velocity(self) -> None:
        plan = self.planner.plan("billiards cue ball hits passive target")
        execution = minimal_billiard_execution(pre_contact_target_velocity=[0.2, 0.0, 0.0])
        report = self.verifier.verify(plan, execution)
        self.assertFalse(report["capability_ready"])
        self.assertEqual(report["primary_failure_type"], "F4_causality_violation")

    def test_verifier_accepts_contact_driven_billiard_trace(self) -> None:
        plan = self.planner.plan("billiards cue ball hits passive target")
        execution = minimal_billiard_execution(pre_contact_target_velocity=[0.0, 0.0, 0.0])
        report = self.verifier.verify(plan, execution)
        self.assertTrue(report["capability_ready"])
        self.assertFalse(report["reference_video_ready"])
        self.assertEqual(report["artifact_tier"], "simulated_trace_not_video")

    def test_failure_taxonomy_outputs_are_legal(self) -> None:
        plan = self.planner.plan("falling blocks under gravity")
        execution = {
            "schema_version": "capability_execution_trace_v1",
            "case_id": "bad_falling",
            "environment": {"gravity_m_s2": 0.0},
            "objects": [{"id": "falling_block_1", "role": "falling_body", "initial_state": {"linear_velocity_m_s": [0, 0, 0]}, "physics": {"gravity_enabled": False, "collision_enabled": True}}],
            "trajectory": [{"frame": 0, "time_s": 0.0, "objects": {"falling_block_1": {"position_m": [0, 0, 1], "velocity_m_s": [0, 0, 0]}}}],
            "render_evidence": {"source_type": "SIM_PROXY", "trajectory_available": True, "runtime_status": "completed"},
        }
        report = self.verifier.verify(plan, execution)
        self.assertFalse(report["capability_ready"])
        for failure in report["failure_modes"]:
            self.assertIn(failure["failure_type"], VALID_FAILURE_TYPES)

    def test_closed_loop_demo_writes_structured_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copy_minimal_profile(root)
            result = run_closed_loop_demo(root, timestamp="test_run")
            run_dir = Path(result["run_dir"])
            self.assertTrue((run_dir / "summary.json").exists())
            self.assertTrue((run_dir / "case_results.json").exists())
            self.assertTrue((run_dir / "diagnosis.md").exists())
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["case_count"], 3)
            self.assertEqual(summary["capability_ready_count"], 3)


def minimal_billiard_execution(*, pre_contact_target_velocity: list[float]) -> dict:
    return {
        "schema_version": "capability_execution_trace_v1",
        "case_id": "billiard_unit",
        "environment": {"gravity_m_s2": 9.81},
        "objects": [
            {"id": "cue_ball", "role": "active_striker", "initial_state": {"linear_velocity_m_s": [1.0, 0.0, 0.0]}, "physics": {"collision_enabled": True, "gravity_enabled": True}},
            {"id": "target_ball", "role": "passive_target", "initial_state": {"linear_velocity_m_s": [0.0, 0.0, 0.0]}, "physics": {"collision_enabled": True, "gravity_enabled": True}},
        ],
        "trajectory": [
            {
                "frame": 0,
                "time_s": 0.0,
                "objects": {
                    "cue_ball": {"position_m": [-1, 0, 0], "velocity_m_s": [1.0, 0, 0]},
                    "target_ball": {"position_m": [0, 0, 0], "velocity_m_s": pre_contact_target_velocity},
                },
                "contacts": [],
            },
            {
                "frame": 1,
                "time_s": 0.2,
                "objects": {
                    "cue_ball": {"position_m": [-0.2, 0, 0], "velocity_m_s": [0.5, 0, 0]},
                    "target_ball": {"position_m": [0.05, 0, 0], "velocity_m_s": [0.4, 0, 0]},
                },
                "contacts": [{"objects": ["cue_ball", "target_ball"], "time_s": 0.2}],
            },
            {
                "frame": 2,
                "time_s": 0.4,
                "objects": {
                    "cue_ball": {"position_m": [-0.1, 0, 0], "velocity_m_s": [0.2, 0, 0]},
                    "target_ball": {"position_m": [0.14, 0, 0], "velocity_m_s": [0.3, 0, 0]},
                },
                "contacts": [],
            },
        ],
        "render_evidence": {"source_type": "SIM_PROXY", "runtime_status": "completed", "trajectory_available": True, "contact_events_available": True, "video_available": False},
    }


def copy_minimal_profile(root: Path) -> None:
    config = root / "config"
    config.mkdir(parents=True)
    source = ROOT / "config" / "harness_capability_profile.json"
    (config / "harness_capability_profile.json").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
