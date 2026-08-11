from __future__ import annotations

import copy
import unittest
from pathlib import Path

from harness.planning.backend_planner import plan_backend
from harness.planning.verification_compiler import compile_verification_plan
from harness.runtime.fallback_backend import trajectory_for_case
from harness.verification.physics_verifier import PhysicsVerifier
from scripts.harness_local_ue_runner import native_case_type


def rigid_scene(label: str) -> dict:
    return {
        "schema_version": "harness_case_spec_v1",
        "case_id": "same_scene",
        "capability_id": label,
        "scene": {"duration_s": 1.0},
        "objects": [
            {
                "id": "body",
                "body_type": "dynamic",
                "initial_position_m": [0.0, 0.0, 1.0],
                "initial_velocity_m_s": [0.0, 0.0, 0.0],
            },
            {"id": "support", "body_type": "static", "role": "support"},
        ],
        "verification_assertions": [{"type": "trajectory_integrity"}],
    }


class NoProcessDispatchTests(unittest.TestCase):
    def test_legacy_process_label_does_not_change_backend_plan(self) -> None:
        plans = [plan_backend(rigid_scene(label), requested_backend="fallback") for label in ("falling", "domino", "projectile")]
        self.assertEqual({item["scene_domain"] for item in plans}, {"rigid_body"})
        self.assertEqual({item["selected_backend"] for item in plans}, {"fallback"})
        self.assertEqual({item["capability_id"] for item in plans}, {"rigid_body_dynamics"})

    def test_legacy_process_label_does_not_select_a_verifier(self) -> None:
        plans = [compile_verification_plan(rigid_scene(label)) for label in ("falling", "domino", "projectile")]
        self.assertEqual({item["verifiers"][0]["id"] for item in plans}, {"trajectory_assertion_verifier"})
        self.assertEqual({tuple(str(value) for value in item["assertions"]) for item in plans}, {("{'type': 'trajectory_integrity'}",)})

    def test_fallback_and_verification_ignore_process_label(self) -> None:
        left = rigid_scene("falling")
        right = copy.deepcopy(left)
        right["capability_id"] = "domino"
        left_trajectory = trajectory_for_case(left)
        right_trajectory = trajectory_for_case(right)
        self.assertEqual(left_trajectory, right_trajectory)
        self.assertEqual(PhysicsVerifier().verify(left, left_trajectory), PhysicsVerifier().verify(right, right_trajectory))

    def test_ue_native_mode_is_always_object_graph(self) -> None:
        self.assertEqual({native_case_type(rigid_scene(label)) for label in ("falling", "domino", "projectile")}, {"llm_object_graph"})

    def test_native_entry_rejects_old_modes_before_simulation(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "scripts" / "native_ue_scene.py").read_text(encoding="utf-8")
        main = source[source.index("def main():") :]
        self.assertLess(main.index('runtime_scene.get("case_type") != "llm_object_graph"'), main.index("simulate_runtime_scene(runtime_scene)"))
        self.assertNotIn("build_scene_spec(DURATION, FPS)", main)


if __name__ == "__main__":
    unittest.main()
