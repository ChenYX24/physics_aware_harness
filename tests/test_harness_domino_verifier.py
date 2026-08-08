from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.case_spec import load_case_spec
from harness.runtime.fallback_backend import FallbackBackend
from harness.verification.physics_verifier import PhysicsVerifier


ROOT = Path(__file__).resolve().parents[1]


class HarnessDominoVerifierTests(unittest.TestCase):
    def test_domino_positive_and_negative(self) -> None:
        self.assertEqual(run_case("cases/domino/five_domino_chain.json")["status"], "pass")
        negative = run_case("cases/domino/negative_simultaneous_motion.json")
        self.assertEqual(negative["status"], "fail")
        self.assertEqual(negative["failure_type"], "F4_causality_violation")

    def test_non_domino_ordered_chain_uses_declared_edges_and_native_contacts(self) -> None:
        case = {
            "case_id": "ordered_containers",
            "capability_id": "sequential_contact_propagation",
            "objects": [
                {"id": "driver", "role": "rolling_projectile"},
                {"id": "target_1", "role": "first_target"},
                {"id": "target_2", "role": "second_target"},
            ],
            "expected_physics": {
                "collision_graph": [["driver", "target_1"], ["target_1", "target_2"]],
            },
        }
        trajectory = [
            {
                "frame": 0,
                "time_s": 0.0,
                "objects": {
                    "driver": {"position_m": [0.0, 0.0, 0.0], "velocity_m_s": [0.0, 0.0, 0.0]},
                    "target_1": {"position_m": [1.0, 0.0, 0.0], "velocity_m_s": [0.0, 0.0, 0.0]},
                    "target_2": {"position_m": [2.0, 0.0, 0.0], "velocity_m_s": [0.0, 0.0, 0.0]},
                },
                "contacts": [
                    {"objects": ["target_1", "target_2"], "native_collision": False, "method": "bounds_inference"},
                ],
            },
            {
                "frame": 1,
                "time_s": 0.1,
                "objects": {
                    "driver": {"position_m": [0.2, 0.0, 0.0], "velocity_m_s": [1.0, 0.0, 0.0]},
                    "target_1": {"position_m": [1.0, 0.0, 0.0], "velocity_m_s": [0.2, 0.0, 0.0]},
                    "target_2": {"position_m": [2.0, 0.0, 0.0], "velocity_m_s": [0.0, 0.0, 0.0]},
                },
                "contacts": [{"objects": ["driver", "target_1"], "native_collision": True}],
            },
            {
                "frame": 2,
                "time_s": 0.2,
                "objects": {
                    "driver": {"position_m": [0.3, 0.0, 0.0], "velocity_m_s": [0.5, 0.0, 0.0]},
                    "target_1": {"position_m": [1.1, 0.0, 0.0], "velocity_m_s": [0.4, 0.0, 0.0]},
                    "target_2": {"position_m": [2.0, 0.0, 0.0], "velocity_m_s": [0.2, 0.0, 0.0]},
                },
                "contacts": [{"objects": ["target_1", "target_2"], "native_collision": True}],
            },
        ]

        report = PhysicsVerifier().verify(case, trajectory)

        self.assertEqual(report["status"], "pass", report)


def run_case(rel_path: str) -> dict:
    case = load_case_spec(ROOT / rel_path)
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = FallbackBackend().run_case(case, tmp)
        return PhysicsVerifier().verify_run_dir(run_dir)


if __name__ == "__main__":
    unittest.main()
