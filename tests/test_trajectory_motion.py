from __future__ import annotations

import unittest

from harness.runtime.trajectory_motion import bake_trajectory_into_case, kinematic_motion_from_trajectory


class TrajectoryMotionTests(unittest.TestCase):
    def test_bakes_complete_transform_frames_without_resampling(self) -> None:
        trajectory = [
            {"time": 0.0, "objects": {"source": {"position": [0.0, 0.0, 1.0], "rotation_degrees": [0.0, 0.0, 0.0]}}},
            {"time": 0.5, "objects": {"source": {"position_m": [1.0, 0.0, 0.5], "rotation_deg": [0.0, 45.0, 0.0]}}},
        ]
        base = {
            "identity": {"case_id": "base"},
            "scene": {"duration_s": 1.0},
            "objects": [{
                "id": "target",
                "role": "rigid_collider",
                "physics": {"body_type": "kinematic"},
                "initial_state": {"position_m": [9.0, 9.0, 9.0], "rotation_deg": [0.0, 0.0, 0.0]},
            }],
        }

        baked = bake_trajectory_into_case(base, trajectory, {"source": "target"}, case_id="baked")

        self.assertEqual(baked["identity"]["case_id"], "baked")
        self.assertEqual(baked["scene"]["duration_s"], 0.5)
        self.assertEqual(len(baked["objects"][0]["kinematic_motion"]["keyframes"]), 2)
        self.assertEqual(baked["objects"][0]["initial_state"]["position_m"], [0.0, 0.0, 1.0])
        self.assertEqual(kinematic_motion_from_trajectory(trajectory, "source")["keyframes"][1]["rotation_deg"], [0.0, 45.0, 0.0])


if __name__ == "__main__":
    unittest.main()
