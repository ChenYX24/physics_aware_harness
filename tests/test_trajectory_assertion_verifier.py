from __future__ import annotations

import unittest

from harness.verification.trajectory_assertion_verifier import verify_trajectory_assertions


def trajectory() -> list[dict]:
    return [
        {"frame": 0, "time_s": 0.0, "objects": {"a": {"position_m": [0.0, 0.0, 1.0]}, "b": {"position_m": [1.0, 0.0, 0.0]}, "c": {"position_m": [2.0, 0.0, 0.0]}}, "contacts": []},
        {"frame": 1, "time_s": 0.1, "objects": {"a": {"position_m": [0.4, 0.0, 0.5]}, "b": {"position_m": [1.1, 0.0, 0.0]}, "c": {"position_m": [2.0, 0.0, 0.0]}}, "contacts": [{"objects": ["a", "b"]}]},
        {"frame": 2, "time_s": 0.2, "objects": {"a": {"position_m": [0.6, 0.0, 0.2]}, "b": {"position_m": [1.4, 0.0, 0.0]}, "c": {"position_m": [2.2, 0.0, 0.0]}}, "contacts": [{"objects": ["b", "c"]}]},
    ]


class TrajectoryAssertionVerifierTests(unittest.TestCase):
    def verify(self, assertions: list[dict]) -> tuple[str | None, dict | None, list[dict]]:
        return verify_trajectory_assertions({"verification_assertions": assertions}, trajectory())

    def test_event_sequence_is_generic(self) -> None:
        failure, _, _ = self.verify([{"type": "event_sequence", "pairs": [["a", "b"], ["b", "c"]]}])
        self.assertIsNone(failure)

    def test_event_count_and_state_delta_are_composable(self) -> None:
        failure, _, evidence = self.verify([
            {"type": "event_count", "event": "contact", "operator": ">=", "value": 2},
            {"type": "state_delta", "object_id": "a", "field": "position_m.z", "operator": "<=", "value": -0.7},
        ])
        self.assertIsNone(failure)
        self.assertEqual(len(evidence[0]["results"]), 2)

    def test_assertion_failure_is_reported_without_process_label(self) -> None:
        failure, counterexample, evidence = self.verify([
            {"id": "x_limit", "type": "state_value", "object_id": "c", "field": "position_m.x", "reduction": "final", "operator": "<", "value": 1.0},
            {"id": "missing_contact", "type": "event_exists", "objects": ["a", "c"]},
            {"id": "present_contact", "type": "event_exists", "objects": ["a", "b"]},
        ])
        self.assertEqual(failure, "declared_assertion_failed")
        self.assertEqual(counterexample["metric"], "x_limit")
        self.assertEqual(
            [(row["id"], row["passed"]) for row in evidence[0]["results"]],
            [("x_limit", False), ("missing_contact", False), ("present_contact", True)],
        )

    def test_event_sequence_reports_every_pair(self) -> None:
        failure, _, evidence = self.verify([
            {"id": "sequence", "type": "event_sequence", "pairs": [["a", "b"], ["a", "c"], ["b", "c"]]},
        ])

        self.assertEqual(failure, "declared_assertion_failed")
        result = evidence[0]["results"][0]
        self.assertEqual(
            [(row["objects"], row["observed"]) for row in result["pair_results"]],
            [(["a", "b"], True), (["a", "c"], False), (["b", "c"], True)],
        )

    def test_rigid_constraint_residual_passes_before_declared_assertions(self) -> None:
        case_spec = constrained_runtime_case()
        constrained_trajectory = [
            constraint_frame(0, 0.0, [0.0, 0.0, -1.0]),
            constraint_frame(1, 0.1, [0.005, 0.0, -1.0]),
        ]

        failure, _, evidence = verify_trajectory_assertions(case_spec, constrained_trajectory)

        self.assertIsNone(failure)
        self.assertEqual(
            [item["type"] for item in evidence],
            ["rigid_constraint_residuals", "trajectory_assertions"],
        )

    def test_rigid_constraint_drift_is_a_runtime_enforcement_failure(self) -> None:
        case_spec = constrained_runtime_case()
        constrained_trajectory = [
            constraint_frame(0, 0.0, [0.0, 0.0, -1.0]),
            constraint_frame(1, 0.01, [0.004, 0.0, -1.0]),
            constraint_frame(2, 0.02, [0.009, 0.0, -1.0]),
            constraint_frame(3, 0.03, [0.018, 0.0, -1.0]),
        ]

        failure, counterexample, evidence = verify_trajectory_assertions(case_spec, constrained_trajectory)

        self.assertEqual(failure, "F_RUNTIME_CONSTRAINT_ENFORCEMENT_FAILED")
        self.assertEqual(counterexample["metric"], "constraint_linear_residual_m")
        self.assertEqual(counterexample["value"]["constraint_id"], "anchor_joint")
        self.assertEqual(counterexample["frame"], 3)
        self.assertFalse(evidence[0]["results"][0]["passed"])

    def test_driven_constraint_requires_native_state_trace(self) -> None:
        case_spec = driven_runtime_case()
        driven_trajectory = [driven_constraint_frame(0, 0.0, 0.2)]

        failure, _, evidence = verify_trajectory_assertions(case_spec, driven_trajectory)

        self.assertIsNone(failure)
        self.assertEqual(
            [item["type"] for item in evidence],
            ["rigid_constraint_residuals", "constraint_state_trace", "trajectory_assertions"],
        )
        self.assertEqual(evidence[1]["results"][0]["sample_count"], 1)

        driven_trajectory[0]["constraints"] = []
        failure, counterexample, _ = verify_trajectory_assertions(case_spec, driven_trajectory)
        self.assertEqual(failure, "F_RUNTIME_CONSTRAINT_STATE_INVALID")
        self.assertEqual(counterexample["metric"], "constraint_trace_missing")

    def test_unilateral_distance_spring_requires_auditable_force_law_and_parameter_echo(self) -> None:
        case_spec = constrained_runtime_case()
        constraint = case_spec["constraints"][0]
        constraint["linear_motion"] = {"x": "free", "y": "free", "z": "free"}
        constraint.pop("linear_limit_m", None)
        constraint["unilateral_distance_spring"] = {
            "rest_length_m": 1.0,
            "stiffness_n_m": 45.0,
            "damping_n_s_m": 3.0,
        }
        samples = [
            constraint_frame(0, 0.0, [0.0, 0.0, -1.0]),
            constraint_frame(1, 0.1, [0.7, 0.0, -1.0]),
        ]
        spring_samples = [
            {
                "rest_length_m": 1.0,
                "stiffness_n_m": 45.0,
                "damping_n_s_m": 3.0,
                "distance_m": 0.5,
                "extension_m": 0.0,
                "separation_speed_m_s": 0.0,
                "tension_n": 0.0,
                "direction_a_to_b": [1.0, 0.0, 0.0],
                "force_on_body_b_n": [0.0, 0.0, 0.0],
                "cumulative_evaluation_count": 0,
                "cumulative_active_evaluation_count": 0,
            },
            {
                "rest_length_m": 1.0,
                "stiffness_n_m": 45.0,
                "damping_n_s_m": 3.0,
                "distance_m": 1.2,
                "extension_m": 0.2,
                "separation_speed_m_s": 1.0,
                "tension_n": 12.0,
                "direction_a_to_b": [1.0, 0.0, 0.0],
                "force_on_body_b_n": [-12.0, 0.0, 0.0],
                "cumulative_evaluation_count": 1,
                "cumulative_active_evaluation_count": 1,
            },
        ]
        for sample, spring_sample in zip(samples, spring_samples, strict=True):
            sample["constraints"] = [{
                "constraint_id": "anchor_joint",
                "translation_m": [sample["objects"]["rod"]["position_m"][0], 0.0, 0.0],
                "position_target_m": [0.0, 0.0, 0.0],
                "deformation_m": [sample["objects"]["rod"]["position_m"][0], 0.0, 0.0],
                "relative_velocity_m_s": [0.0, 0.0, 0.0],
                "linear_force_n": [0.0, 0.0, 0.0],
                "angular_torque_n_m": [0.0, 0.0, 0.0],
                "stiffness_n_m": [0.0, 0.0, 0.0],
                "unilateral_distance_spring": spring_sample,
                "elastic_potential_j": 0.5 * 45.0 * spring_sample["extension_m"] ** 2,
                "broken": False,
                "source": "adp_cpp_runtime_driver",
            }]

        failure, _, evidence = verify_trajectory_assertions(case_spec, samples)

        self.assertIsNone(failure)
        self.assertEqual(evidence[1]["results"][0]["last_distance_spring_evaluation_count"], 1)

        samples[1]["constraints"][0]["unilateral_distance_spring"]["stiffness_n_m"] = 40.0
        failure, counterexample, _ = verify_trajectory_assertions(case_spec, samples)
        self.assertEqual(failure, "F_RUNTIME_CONSTRAINT_STATE_INVALID")
        self.assertEqual(counterexample["metric"], "constraint_unilateral_distance_spring_mismatch")

    def test_continuous_force_requires_matching_native_trace(self) -> None:
        case_spec = {
            "forces": [
                {
                    "id": "wind",
                    "type": "continuous_force",
                    "object": "a",
                    "vector_n": [1.5, 0.0, 0.0],
                    "start_time_s": 0.1,
                    "end_time_s": 0.2,
                }
            ],
            "verification_assertions": [{"type": "trajectory_integrity"}],
        }
        samples = trajectory()
        for frame in samples[1:]:
            frame["forces"] = [
                {
                    "force_id": "wind",
                    "object": "a",
                    "vector_n": [1.5, 0.0, 0.0],
                    "source": "adp_cpp_runtime_driver",
                }
            ]

        failure, _, evidence = verify_trajectory_assertions(case_spec, samples)

        self.assertIsNone(failure)
        self.assertEqual(evidence[0]["type"], "continuous_force_trace")
        samples[1]["forces"][0]["vector_n"] = [0.0, 0.0, 0.0]
        failure, counterexample, _ = verify_trajectory_assertions(case_spec, samples)
        self.assertEqual(failure, "F_RUNTIME_CONTINUOUS_FORCE_TRACE_INVALID")
        self.assertEqual(counterexample["metric"], "continuous_force_trace")


def constrained_runtime_case() -> dict:
    return {
        "objects": [
            {
                "id": "anchor",
                "body_type": "static",
                "initial_position_m": [0.0, 0.0, 0.0],
                "initial_rotation_deg": [0.0, 0.0, 0.0],
            },
            {
                "id": "rod",
                "body_type": "dynamic",
                "initial_position_m": [0.0, 0.0, -1.0],
                "initial_rotation_deg": [0.0, 0.0, 0.0],
            },
        ],
        "constraints": [
            {
                "id": "anchor_joint",
                "body_a": "anchor",
                "body_b": "rod",
                "frame_a": {
                    "position_m": [0.0, 0.0, 0.0],
                    "primary_axis": [1.0, 0.0, 0.0],
                    "secondary_axis": [0.0, 1.0, 0.0],
                },
                "frame_b": {
                    "position_m": [0.0, 0.0, 1.0],
                    "primary_axis": [1.0, 0.0, 0.0],
                    "secondary_axis": [0.0, 1.0, 0.0],
                },
                "linear_motion": {"x": "locked", "y": "locked", "z": "locked"},
                "linear_limit_m": None,
            }
        ],
        "verification_assertions": [{"id": "integrity", "type": "trajectory_integrity"}],
    }


def constraint_frame(frame: int, time_s: float, rod_position: list[float]) -> dict:
    return {
        "frame": frame,
        "time_s": time_s,
        "objects": {
            "rod": {
                "position_m": rod_position,
                "rotation_deg": [0.0, 0.0, 0.0],
            }
        },
    }


def driven_runtime_case() -> dict:
    return {
        "objects": [
            {
                "id": "anchor",
                "body_type": "static",
                "initial_position_m": [0.0, 0.0, 0.0],
                "initial_rotation_deg": [0.0, 0.0, 0.0],
            },
            {
                "id": "cart",
                "body_type": "dynamic",
                "initial_position_m": [0.2, 0.0, 0.0],
                "initial_rotation_deg": [0.0, 0.0, 0.0],
            },
        ],
        "constraints": [
            {
                "id": "spring_joint",
                "body_a": "anchor",
                "body_b": "cart",
                "frame_a": {"position_m": [0.0, 0.0, 0.0], "primary_axis": [1.0, 0.0, 0.0], "secondary_axis": [0.0, 1.0, 0.0]},
                "frame_b": {"position_m": [0.0, 0.0, 0.0], "primary_axis": [1.0, 0.0, 0.0], "secondary_axis": [0.0, 1.0, 0.0]},
                "linear_motion": {"x": "free", "y": "locked", "z": "locked"},
                "linear_drive": {
                    "stiffness_n_m": [100.0, 0.0, 0.0],
                },
            }
        ],
        "verification_assertions": [{"id": "integrity", "type": "trajectory_integrity"}],
    }


def driven_constraint_frame(frame: int, time_s: float, cart_x: float) -> dict:
    deformation = cart_x - 0.3
    return {
        "frame": frame,
        "time_s": time_s,
        "objects": {
            "cart": {
                "position_m": [cart_x, 0.0, 0.0],
                "rotation_deg": [0.0, 0.0, 0.0],
            }
        },
        "constraints": [
            {
                "constraint_id": "spring_joint",
                "translation_m": [cart_x, 0.0, 0.0],
                "position_target_m": [0.3, 0.0, 0.0],
                "deformation_m": [deformation, 0.0, 0.0],
                "relative_velocity_m_s": [0.0, 0.0, 0.0],
                "linear_force_n": [-100.0 * deformation, 0.0, 0.0],
                "angular_torque_n_m": [0.0, 0.0, 0.0],
                "stiffness_n_m": [100.0, 0.0, 0.0],
                "elastic_potential_j": 0.5 * 100.0 * deformation * deformation,
                "broken": False,
                "source": "adp_cpp_runtime_driver",
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
