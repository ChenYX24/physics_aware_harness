from __future__ import annotations

import unittest

from harness.core.case_spec_v2 import case_spec_v2_from_dict, compile_case_spec_v2_runtime
from harness.planning.static_scene_builder import build_static_scene_layout
from harness.runtime.actor_placement import compile_runtime_actor_placement
from harness.runtime.kinematic_motion import sample_linear_kinematic_position, sample_linear_kinematic_rotation
from harness.verification.trajectory_assertion_verifier import verify_trajectory_assertions
from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement
from tests.case_spec_v2_fixture import case_spec_v2_fixture


class KinematicMotionTests(unittest.TestCase):
    def test_linear_sampling_holds_before_after_and_interpolates_between_keyframes(self) -> None:
        motion = {
            "interpolation": "linear",
            "keyframes": [
                {"time_s": 0.0, "position_m": [0.0, 0.0, 0.5]},
                {"time_s": 1.0, "position_m": [1.0, 0.0, 0.5]},
                {"time_s": 2.0, "position_m": [1.0, 0.0, 0.5]},
            ],
        }

        self.assertEqual(sample_linear_kinematic_position(motion, -1.0), [0.0, 0.0, 0.5])
        self.assertEqual(sample_linear_kinematic_position(motion, 0.25), [0.25, 0.0, 0.5])
        self.assertEqual(sample_linear_kinematic_position(motion, 1.5), [1.0, 0.0, 0.5])
        self.assertEqual(sample_linear_kinematic_position(motion, 3.0), [1.0, 0.0, 0.5])

    def test_motion_survives_compilation_into_runtime_static_object(self) -> None:
        data = case_spec_v2_fixture()
        data["backend_constraints"]["allowed_solvers"] = ["ue"]
        data["backend_constraints"]["required_solver_capabilities"].append("kinematic_motion")
        platform = data["objects"][2]
        platform["physics"]["body_type"] = "kinematic"
        platform["kinematic_motion"] = {
            "interpolation": "linear",
            "keyframes": [
                {"time_s": 0.0, "position_m": [0.0, 0.0, 0.0], "rotation_deg": [0.0, 0.0, 0.0]},
                {"time_s": 1.0, "position_m": [0.5, 0.0, 0.0], "rotation_deg": [0.0, 45.0, 0.0]},
            ],
        }

        runtime = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data))
        layout = build_static_scene_layout(runtime.data)
        placement = compile_runtime_actor_placement(runtime.data, layout)
        _dynamic, static = runtime_objects_from_actor_placement(placement, runtime.data)

        self.assertEqual(static[0]["physics_properties"]["kinematic_motion"], platform["kinematic_motion"])

    def test_rotation_sampling_uses_shortest_wrapped_linear_path(self) -> None:
        motion = {
            "interpolation": "linear",
            "keyframes": [
                {"time_s": 0.0, "position_m": [0.0, 0.0, 0.0], "rotation_deg": [0.0, 170.0, 0.0]},
                {"time_s": 1.0, "position_m": [0.0, 0.0, 0.0], "rotation_deg": [0.0, -170.0, 0.0]},
            ],
        }

        self.assertEqual(sample_linear_kinematic_rotation(motion, 0.5), [0.0, 180.0, 0.0])

    def test_verifier_checks_declared_motion_against_observed_trace(self) -> None:
        case = {
            "objects": [
                {
                    "id": "platform",
                    "kinematic_motion": {
                        "interpolation": "linear",
                        "keyframes": [
                            {"time_s": 0.0, "position_m": [0.0, 0.0, 0.0]},
                            {"time_s": 1.0, "position_m": [1.0, 0.0, 0.0]},
                        ],
                    },
                }
            ]
        }
        trajectory = [
            {"frame": 0, "time": 0.0, "objects": {"platform": {"position": [0.0, 0.0, 0.0]}}},
            {"frame": 1, "time": 0.5, "objects": {"platform": {"position": [0.5, 0.0, 0.0]}}},
            {"frame": 2, "time": 1.0, "objects": {"platform": {"position": [1.0, 0.0, 0.0]}}},
        ]

        failure_type, failure, evidence = verify_trajectory_assertions(case, trajectory)

        self.assertIsNone(failure_type)
        self.assertIsNone(failure)
        self.assertTrue(evidence[0]["results"][0]["passed"])


if __name__ == "__main__":
    unittest.main()
