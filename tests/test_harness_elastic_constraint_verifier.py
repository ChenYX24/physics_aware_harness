from __future__ import annotations

import unittest

from harness.verification.physics_verifier import PhysicsVerifier


class HarnessElasticConstraintVerifierTests(unittest.TestCase):
    def test_elastic_constraint_rebound_trace_passes(self) -> None:
        report = PhysicsVerifier().verify(case_spec(), passing_trace())
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["evidence"][0]["constrained_object_id"], "payload")
        self.assertGreater(report["evidence"][0]["max_extension_m"], 0.0)

    def test_missing_constraint_trace_fails(self) -> None:
        trace = passing_trace()
        for frame in trace:
            frame["constraints"] = []
        report = PhysicsVerifier().verify(case_spec(), trace)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F7_runtime_artifact_incomplete")
        self.assertEqual(report["first_failure"]["metric"], "constraint_trace_present")

    def test_overstretch_fails(self) -> None:
        trace = passing_trace()
        trace[2]["objects"]["payload"]["position_m"] = [0.0, 0.0, 0.15]
        trace[2]["constraints"][0]["measured_distance_m"] = 1.85
        trace[2]["constraints"][0]["extension_m"] = 0.65
        report = PhysicsVerifier().verify(case_spec(), trace)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "elastic_extension_m")

    def test_no_rebound_after_max_stretch_fails(self) -> None:
        trace = passing_trace()
        trace[3]["objects"]["payload"]["velocity_m_s"] = [0.0, 0.0, -0.05]
        report = PhysicsVerifier().verify(case_spec(), trace)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "rebound_velocity_toward_anchor_m_s")


def case_spec() -> dict:
    return {
        "schema_version": "harness_case_spec_v1",
        "case_id": "elastic_rope_unit",
        "capability_id": "elastic_constraint_rebound",
        "prompt": "A bungee payload falls, stretches an elastic rope, then rebounds upward.",
        "expected_physics": {
            "anchor_object_id": "anchor",
            "constrained_object_id": "payload",
            "rest_length_m": 1.2,
            "max_extension_m": 0.42,
            "expected_min_extension_m": 0.25,
            "expected_min_rebound_speed_m_s": 0.2,
            "constraint_stiffness_n_m": 45.0,
            "damping_ratio": 0.22,
        },
        "objects": [
            {"id": "anchor", "role": "elastic_constraint_anchor", "shape": "fixed_point", "initial_position_m": [0.0, 0.0, 2.0], "initial_velocity_m_s": [0.0, 0.0, 0.0], "kinematic": True},
            {"id": "payload", "role": "elastic_constrained_body", "shape": "sphere", "mass_kg": 0.8, "initial_position_m": [0.0, 0.0, 1.0], "initial_velocity_m_s": [0.0, 0.0, 0.0]},
        ],
        "active_objects": [],
        "passive_objects": ["payload"],
        "required_assets": ["elastic constraint anchor", "elastic constrained rigid body", "elastic tether constraint"],
        "required_signals": ["trajectory", "constraint_trace", "elastic_energy_labels"],
        "verifier_expectation": {"status": "pass"},
        "should_pass": True,
        "notes": "Unit fixture for elastic constraint rebound verifier.",
    }


def passing_trace() -> list[dict]:
    frames = [
        frame(0, 0.0, 2.0, 1.0, 0.0),
        frame(1, 0.2, 2.0, 0.62, -1.4),
        frame(2, 0.4, 2.0, 0.42, -0.2),
        frame(3, 0.6, 2.0, 0.72, 0.9),
    ]
    return frames


def frame(frame_id: int, time_s: float, anchor_z: float, payload_z: float, payload_vz: float) -> dict:
    distance = abs(anchor_z - payload_z)
    extension = max(0.0, distance - 1.2)
    return {
        "frame": frame_id,
        "time_s": time_s,
        "objects": {
            "anchor": {"position_m": [0.0, 0.0, anchor_z], "velocity_m_s": [0.0, 0.0, 0.0]},
            "payload": {"position_m": [0.0, 0.0, payload_z], "velocity_m_s": [0.0, 0.0, payload_vz]},
        },
        "contacts": [],
        "constraints": [
            {
                "constraint_id": "elastic_rope",
                "constraint_type": "elastic_tether",
                "anchor_id": "anchor",
                "body_id": "payload",
                "rest_length_m": 1.2,
                "measured_distance_m": round(distance, 6),
                "extension_m": round(extension, 6),
                "frame": frame_id,
                "time_s": time_s,
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
