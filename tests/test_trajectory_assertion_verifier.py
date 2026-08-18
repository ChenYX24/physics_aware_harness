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
            constraint_frame(1, 0.1, [0.08, 0.0, -1.0]),
        ]

        failure, counterexample, evidence = verify_trajectory_assertions(case_spec, constrained_trajectory)

        self.assertEqual(failure, "F_RUNTIME_CONSTRAINT_ENFORCEMENT_FAILED")
        self.assertEqual(counterexample["metric"], "constraint_linear_residual_m")
        self.assertEqual(counterexample["value"]["constraint_id"], "anchor_joint")
        self.assertFalse(evidence[0]["results"][0]["passed"])


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


if __name__ == "__main__":
    unittest.main()
