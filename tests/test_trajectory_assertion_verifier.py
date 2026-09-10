from __future__ import annotations

import unittest

from harness.verification.trajectory_assertion_verifier import (
    verify_constraint_state_trace,
    verify_trajectory_assertions,
)


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

    def test_declared_linear_velocity_reads_canonical_trajectory_velocity(self) -> None:
        samples = [
            {"frame": 0, "time_s": 0.0, "objects": {"a": {"velocity_m_s": [0.0, 0.0, 0.0]}}, "contacts": []},
            {"frame": 1, "time_s": 0.1, "objects": {"a": {"velocity_m_s": [1.5, 0.0, 0.0]}}, "contacts": []},
        ]
        failure, _, evidence = verify_trajectory_assertions(
            {
                "verification_assertions": [
                    {
                        "type": "state_value",
                        "object_id": "a",
                        "field": "linear_velocity_m_s.x",
                        "reduction": "max",
                        "operator": ">=",
                        "value": 1.0,
                    }
                ]
            },
            samples,
        )

        self.assertIsNone(failure)
        self.assertEqual(evidence[0]["results"][0]["measured"], 1.5)

    def test_state_value_max_abs_reduction_handles_signed_rotation(self) -> None:
        samples = [
            {"frame": 0, "time_s": 0.0, "objects": {"a": {"rotation_deg": [0.0, 0.0, 0.0]}}, "contacts": []},
            {"frame": 1, "time_s": 0.1, "objects": {"a": {"rotation_deg": [0.0, -42.0, 0.0]}}, "contacts": []},
        ]
        failure, _, evidence = verify_trajectory_assertions(
            {
                "verification_assertions": [
                    {
                        "type": "state_value",
                        "object_id": "a",
                        "field": "rotation_deg.y",
                        "reduction": "max_abs",
                        "operator": ">=",
                        "value": 15.0,
                    }
                ]
            },
            samples,
        )

        self.assertIsNone(failure)
        self.assertEqual(evidence[0]["results"][0]["measured"], 42.0)

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

    def test_breakable_constraint_residuals_stop_at_truthful_break_frame(self) -> None:
        case_spec = constrained_runtime_case()
        case_spec["constraints"][0]["break_thresholds"] = {"linear_force_n": 25.0}
        constrained_trajectory = [
            constraint_frame(0, 0.0, [0.0, 0.0, -1.0]),
            constraint_frame(1, 0.1, [0.08, 0.0, -1.0]),
        ]
        for frame, broken in zip(constrained_trajectory, (False, True), strict=True):
            frame["constraints"] = [{
                "constraint_id": "anchor_joint",
                "translation_m": [0.0, 0.0, 0.0],
                "position_target_m": [0.0, 0.0, 0.0],
                "deformation_m": [0.0, 0.0, 0.0],
                "relative_velocity_m_s": [0.0, 0.0, 0.0],
                "linear_force_n": [0.0, 0.0, 0.0],
                "angular_torque_n_m": [0.0, 0.0, 0.0],
                "stiffness_n_m": [0.0, 0.0, 0.0],
                "elastic_potential_j": 0.0,
                "broken": broken,
                "source": "adp_cpp_runtime_driver",
            }]

        failure, _, evidence = verify_trajectory_assertions(case_spec, constrained_trajectory)

        self.assertIsNone(failure)
        self.assertEqual(evidence[0]["results"][0]["broken_frame"], 1)
        self.assertTrue(evidence[1]["results"][0]["broken_observed"])

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

    def test_path_length_spring_recomputes_two_segment_geometry_and_force_law(self) -> None:
        case_spec = {
            "objects": [
                {"id": "a", "body_type": "dynamic", "initial_position_m": [1.0, 0.0, 0.0], "initial_rotation_deg": [0.0, 0.0, 0.0]},
                {"id": "b", "body_type": "dynamic", "initial_position_m": [0.0, 0.0, -1.0], "initial_rotation_deg": [0.0, 0.0, 0.0]},
            ],
            "constraints": [{
                "id": "rope",
                "body_a": "a",
                "body_b": "b",
                "frame_a": {"position_m": [0.0, 0.0, 0.0], "primary_axis": [1.0, 0.0, 0.0], "secondary_axis": [0.0, 1.0, 0.0]},
                "frame_b": {"position_m": [0.0, 0.0, 0.0], "primary_axis": [1.0, 0.0, 0.0], "secondary_axis": [0.0, 1.0, 0.0]},
                "linear_motion": {"x": "free", "y": "free", "z": "free"},
                "angular_motion": {"x": "free", "y": "free", "z": "free"},
                "unilateral_path_length_spring": {
                    "fixed_waypoint_m": [0.0, 0.0, 0.0],
                    "rest_length_m": 1.8,
                    "stiffness_n_m": 10.0,
                    "damping_n_s_m": 2.0,
                },
            }],
            "verification_assertions": [{"type": "trajectory_integrity"}],
        }
        spring = {
            "fixed_waypoint_m": [0.0, 0.0, 0.0],
            "point_a_m": [1.0, 0.0, 0.0],
            "point_b_m": [0.0, 0.0, -1.0],
            "rest_length_m": 1.8,
            "stiffness_n_m": 10.0,
            "damping_n_s_m": 2.0,
            "segment_a_length_m": 1.0,
            "segment_b_length_m": 1.0,
            "path_length_m": 2.0,
            "extension_m": 0.2,
            "path_speed_m_s": 0.5,
            "tension_n": 3.0,
            "direction_a_to_waypoint": [-1.0, 0.0, 0.0],
            "direction_b_to_waypoint": [0.0, 0.0, 1.0],
            "force_on_body_a_n": [-3.0, 0.0, 0.0],
            "force_on_body_b_n": [0.0, 0.0, 3.0],
            "cumulative_evaluation_count": 1,
            "cumulative_active_evaluation_count": 1,
        }
        sample = {
            "frame": 0,
            "time_s": 0.0,
            "objects": {
                "a": {"position_m": [1.0, 0.0, 0.0], "rotation_deg": [0.0, 0.0, 0.0]},
                "b": {"position_m": [0.0, 0.0, -1.0], "rotation_deg": [0.0, 0.0, 0.0]},
            },
            "contacts": [],
            "constraints": [{
                "constraint_id": "rope",
                "translation_m": [0.0, 0.0, 0.0],
                "position_target_m": [0.0, 0.0, 0.0],
                "deformation_m": [0.0, 0.0, 0.0],
                "relative_velocity_m_s": [0.0, 0.0, 0.0],
                "linear_force_n": [0.0, 0.0, 0.0],
                "angular_torque_n_m": [0.0, 0.0, 0.0],
                "stiffness_n_m": [0.0, 0.0, 0.0],
                "unilateral_path_length_spring": spring,
                "elastic_potential_j": 0.2,
                "broken": False,
                "source": "adp_cpp_runtime_driver",
            }],
        }

        failure, _, evidence = verify_trajectory_assertions(case_spec, [sample])

        self.assertIsNone(failure)
        self.assertEqual(evidence[1]["results"][0]["max_path_spring_geometry_residual_m"], 0.0)

        spring["segment_a_length_m"] = 0.8
        failure, counterexample, _ = verify_trajectory_assertions(case_spec, [sample])
        self.assertEqual(failure, "F_RUNTIME_CONSTRAINT_STATE_INVALID")
        self.assertEqual(counterexample["metric"], "constraint_unilateral_path_length_spring_force_law")

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

    def test_radial_force_requires_auditable_live_force_law(self) -> None:
        case_spec = {
            "forces": [{
                "id": "point_field",
                "type": "radial_force",
                "source": "b",
                "object": "a",
                "strength_n_m2": 0.08,
                "softening_distance_m": 0.1,
                "max_distance_m": 2.0,
                "start_time_s": 0.0,
                "end_time_s": 0.2,
            }],
            "verification_assertions": [{"type": "trajectory_integrity"}],
        }
        samples = trajectory()
        for frame in samples:
            source = frame["objects"]["b"]["position_m"]
            target = frame["objects"]["a"]["position_m"]
            delta = [source[axis] - target[axis] for axis in range(3)]
            distance = sum(value * value for value in delta) ** 0.5
            magnitude = 0.08 / (distance * distance + 0.1 * 0.1)
            frame["forces"] = [{
                "force_id": "point_field",
                "force_type": "radial_force",
                "source_object": "b",
                "object": "a",
                "source_position_m": source,
                "target_position_m": target,
                "distance_m": distance,
                "vector_n": [value / distance * magnitude for value in delta],
                "applied": True,
                "source": "adp_cpp_runtime_driver",
            }]

        failure, _, evidence = verify_trajectory_assertions(case_spec, samples)

        self.assertIsNone(failure)
        self.assertEqual(evidence[0]["type"], "radial_force_trace")
        self.assertEqual(evidence[0]["results"][0]["applied_sample_count"], 3)
        samples[1]["forces"][0]["vector_n"] = [0.0, 0.0, 0.0]
        failure, counterexample, _ = verify_trajectory_assertions(case_spec, samples)
        self.assertEqual(failure, "F_RUNTIME_RADIAL_FORCE_TRACE_INVALID")
        self.assertEqual(counterexample["metric"], "radial_force_trace")

    def test_aerodynamic_wind_requires_auditable_native_trace(self) -> None:
        case_spec = {
            "objects": [
                {"id": "a", "role": "rigid_body", "body_type": "dynamic"},
                {
                    "id": "wind",
                    "role": "wind_field",
                    "solver": {
                        "type": "aerodynamic_wind",
                        "velocity_m_s": [5.0, 0.0, 0.0],
                        "targets": [{"object": "a", "drag_coefficient": 0.47}],
                    },
                },
            ],
            "verification_assertions": [{"type": "trajectory_integrity"}],
        }
        samples = trajectory()
        for frame in samples:
            frame["forces"] = [{
                "force_id": "wind",
                "force_type": "aerodynamic_wind",
                "object": "a",
                "wind_velocity_m_s": [5.0, 0.0, 0.0],
                "relative_velocity_m_s": [4.5, 0.0, 0.0],
                "vector_n": [1.2, 0.0, 0.0],
                "projected_area_m2": 0.12,
                "applied": True,
                "source": "adp_cpp_runtime_driver",
            }]

        failure, _, evidence = verify_trajectory_assertions(case_spec, samples)

        self.assertIsNone(failure)
        self.assertEqual(evidence[0]["type"], "aerodynamic_wind_trace")
        samples[0]["forces"] = []
        samples[1]["forces"] = []
        samples[2]["forces"] = []
        failure, counterexample, _ = verify_trajectory_assertions(case_spec, samples)
        self.assertEqual(failure, "F_RUNTIME_AERODYNAMIC_WIND_TRACE_INVALID")
        self.assertEqual(counterexample["metric"], "aerodynamic_wind_trace_missing")

    def test_aerodynamic_wind_defers_cloth_trace_to_deformable_verifier(self) -> None:
        case_spec = {
            "objects": [
                {"id": "flag", "role": "deformable_cloth", "body_type": "dynamic"},
                {
                    "id": "wind",
                    "role": "wind_field",
                    "solver": {
                        "type": "aerodynamic_wind",
                        "targets": [{"object": "flag", "drag_coefficient": 1.15}],
                    },
                },
            ],
            "verification_assertions": [{"type": "trajectory_integrity"}],
        }

        failure, _, evidence = verify_trajectory_assertions(case_spec, trajectory())

        self.assertIsNone(failure)
        self.assertNotIn("aerodynamic_wind_trace", {item["type"] for item in evidence})

    def test_constraint_parameter_readback_accepts_float32_roundoff(self) -> None:
        assert_constraint_parameter_readback_accepts_float32_roundoff()


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


def assert_constraint_parameter_readback_accepts_float32_roundoff() -> None:
    case = driven_runtime_case()
    declaration = case["constraints"][0]
    declaration.pop("linear_drive")
    declaration["linear_motion"] = {"x": "free", "y": "free", "z": "free"}
    declaration["unilateral_distance_spring"] = {
        "rest_length_m": 0.01,
        "stiffness_n_m": 1.4,
        "damping_n_s_m": 0.01,
    }
    trajectory = [driven_constraint_frame(0, 0.0, 0.2)]
    sample = trajectory[0]["constraints"][0]
    sample["unilateral_distance_spring"] = {
        "rest_length_m": 0.01,
        "stiffness_n_m": 1.399999976158142,
        "damping_n_s_m": 0.009999999776482582,
        "distance_m": 0.2,
        "extension_m": 0.19,
        "separation_speed_m_s": 0.0,
        "tension_n": 0.266,
        "direction_a_to_b": [1.0, 0.0, 0.0],
        "force_on_body_b_n": [-0.266, 0.0, 0.0],
        "cumulative_evaluation_count": 1,
        "cumulative_active_evaluation_count": 1,
    }
    sample["deformation_m"] = [0.2, 0.0, 0.0]
    sample["elastic_potential_j"] = 0.5 * 1.4 * 0.19 * 0.19

    failure, evidence = verify_constraint_state_trace(case, trajectory)

    assert failure is None
    assert evidence[0]["results"][0]["passed"] is True

    sample["unilateral_distance_spring"].update({
        "tension_n": 0.0,
        "force_on_body_b_n": [0.0, 0.0, 0.0],
        "cumulative_evaluation_count": 0,
        "cumulative_active_evaluation_count": 0,
    })
    failure, _ = verify_constraint_state_trace(case, trajectory)
    assert failure is None


if __name__ == "__main__":
    unittest.main()
