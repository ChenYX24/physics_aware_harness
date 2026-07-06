from __future__ import annotations

import unittest

from harness.verification.physics_verifier import PhysicsVerifier


class HarnessBrittleFractureVerifierTests(unittest.TestCase):
    def test_brittle_impact_fracture_trace_passes(self) -> None:
        report = PhysicsVerifier().verify(case_spec(), passing_trace())
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["evidence"][0]["fractured_object_id"], "glass_panel")
        self.assertGreaterEqual(report["evidence"][0]["fragment_count"], 6)

    def test_missing_fracture_event_fails(self) -> None:
        trace = passing_trace()
        for frame in trace:
            frame["fracture_events"] = []
            frame["fragments"] = []
        report = PhysicsVerifier().verify(case_spec(), trace)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F7_runtime_artifact_incomplete")
        self.assertEqual(report["first_failure"]["metric"], "fracture_event_present")

    def test_fracture_before_contact_fails(self) -> None:
        trace = passing_trace()
        trace[0]["fracture_events"] = [fracture_event(frame_id=0, time_s=0.0, fragment_count=6, impact_energy_j=4.8)]
        trace[1]["fracture_events"] = []
        report = PhysicsVerifier().verify(case_spec(), trace)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "fracture_frame_before_contact")

    def test_fracture_below_threshold_fails(self) -> None:
        trace = passing_trace()
        trace[1]["contacts"][0]["impact_energy_j"] = 0.7
        trace[1]["fracture_events"][0]["impact_energy_j"] = 0.7
        report = PhysicsVerifier().verify(case_spec(), trace)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "impact_energy_j")

    def test_too_few_fragments_fails(self) -> None:
        trace = passing_trace()
        trace[1]["fracture_events"][0]["fragment_count"] = 2
        trace[1]["fragments"] = trace[1]["fragments"][:2]
        report = PhysicsVerifier().verify(case_spec(), trace)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "fragment_count")


def case_spec() -> dict:
    return {
        "schema_version": "harness_case_spec_v1",
        "case_id": "brittle_fracture_unit",
        "capability_id": "brittle_impact_fracture",
        "prompt": "A striker hits a brittle glass panel and it fractures only after impact energy exceeds threshold.",
        "expected_physics": {
            "impactor_object_id": "striker",
            "brittle_object_id": "glass_panel",
            "fracture_threshold_j": 2.4,
            "expected_min_fragment_count": 6,
            "expected_contact_pair": ["striker", "glass_panel"],
        },
        "objects": [
            {"id": "striker", "role": "active_impactor", "shape": "sphere", "mass_kg": 1.2, "initial_position_m": [-0.6, 0.0, 0.4], "initial_velocity_m_s": [2.8, 0.0, 0.0]},
            {"id": "glass_panel", "role": "brittle_fracture_body", "shape": "thin_box", "mass_kg": 0.7, "fracture_threshold_j": 2.4, "initial_position_m": [0.0, 0.0, 0.4], "initial_velocity_m_s": [0.0, 0.0, 0.0]},
        ],
        "active_objects": ["striker"],
        "passive_objects": ["glass_panel"],
        "required_assets": ["impactor rigid body", "brittle fracture body", "fracture fragments"],
        "required_signals": ["trajectory", "contact_events", "fracture_events", "fragment_manifest", "energy_labels"],
        "verifier_expectation": {"status": "pass"},
        "should_pass": True,
        "notes": "Unit fixture for brittle impact fracture verifier.",
    }


def passing_trace() -> list[dict]:
    return [
        {
            "frame": 0,
            "time_s": 0.0,
            "objects": {
                "striker": {"position_m": [-0.6, 0.0, 0.4], "velocity_m_s": [2.8, 0.0, 0.0]},
                "glass_panel": {"position_m": [0.0, 0.0, 0.4], "velocity_m_s": [0.0, 0.0, 0.0]},
            },
            "contacts": [],
            "fracture_events": [],
            "fragments": [],
        },
        {
            "frame": 1,
            "time_s": 0.2,
            "objects": {
                "striker": {"position_m": [-0.05, 0.0, 0.4], "velocity_m_s": [0.4, 0.0, 0.0]},
                "glass_panel": {"position_m": [0.0, 0.0, 0.4], "velocity_m_s": [0.0, 0.0, 0.0]},
            },
            "contacts": [{"objects": ["striker", "glass_panel"], "frame": 1, "time_s": 0.2, "impact_energy_j": 4.8, "normal_impulse_n_s": 2.2}],
            "fracture_events": [fracture_event(frame_id=1, time_s=0.2, fragment_count=6, impact_energy_j=4.8)],
            "fragments": [{"fragment_id": f"glass_panel_frag_{idx}", "source_object_id": "glass_panel"} for idx in range(6)],
        },
        {
            "frame": 2,
            "time_s": 0.4,
            "objects": {
                "striker": {"position_m": [0.05, 0.0, 0.4], "velocity_m_s": [0.1, 0.0, 0.0]},
                "glass_panel": {"position_m": [0.0, 0.0, 0.4], "velocity_m_s": [0.0, 0.0, 0.0], "fractured": True},
            },
            "contacts": [],
            "fracture_events": [],
            "fragments": [{"fragment_id": f"glass_panel_frag_{idx}", "source_object_id": "glass_panel"} for idx in range(6)],
        },
    ]


def fracture_event(*, frame_id: int, time_s: float, fragment_count: int, impact_energy_j: float) -> dict:
    return {
        "event_type": "fracture",
        "object_id": "glass_panel",
        "caused_by_object_id": "striker",
        "frame": frame_id,
        "time_s": time_s,
        "impact_energy_j": impact_energy_j,
        "fracture_threshold_j": 2.4,
        "fragment_count": fragment_count,
    }


if __name__ == "__main__":
    unittest.main()
