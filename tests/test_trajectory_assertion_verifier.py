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
        failure, counterexample, _ = self.verify([
            {"id": "x_limit", "type": "state_value", "object_id": "c", "field": "position_m.x", "reduction": "final", "operator": "<", "value": 1.0}
        ])
        self.assertEqual(failure, "declared_assertion_failed")
        self.assertEqual(counterexample["metric"], "x_limit")


if __name__ == "__main__":
    unittest.main()
