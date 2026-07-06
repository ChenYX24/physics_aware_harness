from __future__ import annotations

import unittest

from harness.verification.physics_verifier import PhysicsVerifier


class HarnessImpulseChainVerifierTests(unittest.TestCase):
    def test_newton_cradle_smoke_trace_passes_generic_impulse_chain(self) -> None:
        report = PhysicsVerifier().verify(case_spec(), positive_trace())
        self.assertEqual(report["status"], "pass")
        self.assertIsNone(report["failure_type"])
        self.assertEqual(report["evidence"][0]["receiver_object_id"], "ball_4")

    def test_passive_chain_member_cannot_have_hidden_initial_velocity(self) -> None:
        trace = positive_trace()
        trace[0]["objects"]["ball_2"]["velocity_m_s"] = [0.18, 0.0, 0.0]
        report = PhysicsVerifier().verify(case_spec(), trace)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F5_passive_precontact_motion")
        self.assertEqual(report["first_failure"]["object_id"], "ball_2")

    def test_terminal_receiver_must_respond_after_chain_contact(self) -> None:
        trace = positive_trace()
        for frame in trace:
            frame["objects"]["ball_4"]["velocity_m_s"] = [0.0, 0.0, 0.0]
        report = PhysicsVerifier().verify(case_spec(), trace)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "receiver_post_chain_speed_m_s")

    def test_contact_chain_order_is_enforced(self) -> None:
        trace = positive_trace()
        trace[1]["contacts"] = [{"objects": ["ball_2", "ball_3"], "frame": 1, "time_s": 0.2}]
        trace[3]["contacts"] = [{"objects": ["ball_0", "ball_1"], "frame": 3, "time_s": 0.6}]
        report = PhysicsVerifier().verify(case_spec(), trace)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F4_causality_violation")
        self.assertEqual(report["first_failure"]["metric"], "contact_chain_order")


def case_spec() -> dict:
    chain = [f"ball_{idx}" for idx in range(5)]
    return {
        "schema_version": "harness_case_spec_v1",
        "case_id": "newton_cradle_impulse_transfer",
        "capability_id": "constraint_momentum_transfer",
        "prompt": "A constrained impulse chain transfers motion from the first suspended ball to the final receiver.",
        "expected_physics": {
            "coordinate_system": "z_up",
            "chain_objects": chain,
            "active_object_id": "ball_0",
            "receiver_object_id": "ball_4",
            "expected_contact_chain": [[chain[idx], chain[idx + 1]] for idx in range(len(chain) - 1)],
            "expected_min_receiver_speed_m_s": 0.35,
            "expected_max_intermediate_displacement_m": 0.05,
            "expected_energy_ratio_max": 1.1,
        },
        "objects": [
            {
                "id": object_id,
                "role": "constrained_chain_body" if idx else "active_chain_driver",
                "shape": "sphere",
                "radius_m": 0.08,
                "mass_kg": 0.18,
                "initial_position_m": [round(idx * 0.18, 4), 0.0, 0.9],
                "initial_velocity_m_s": [0.9, 0.0, 0.0] if idx == 0 else [0.0, 0.0, 0.0],
            }
            for idx, object_id in enumerate(chain)
        ],
        "active_objects": ["ball_0"],
        "passive_objects": chain[1:],
        "required_assets": ["constrained rigid bodies", "colliders", "constraint anchors"],
        "required_signals": ["trajectory", "contact_events", "constraint_trace", "mass_labels"],
        "verifier_expectation": {"status": "pass"},
        "should_pass": True,
        "notes": "Newton's cradle is a smoke family for generic constrained impulse-chain transfer.",
    }


def positive_trace() -> list[dict]:
    base_positions = {f"ball_{idx}": [round(idx * 0.18, 4), 0.0, 0.9] for idx in range(5)}

    def objects(frame_id: int) -> dict:
        result = {}
        for idx in range(5):
            object_id = f"ball_{idx}"
            position = list(base_positions[object_id])
            velocity = [0.0, 0.0, 0.0]
            if object_id == "ball_0" and frame_id <= 1:
                velocity = [0.9 if frame_id == 0 else 0.12, 0.0, 0.0]
                position[0] += 0.08 * frame_id
            if object_id in {"ball_1", "ball_2", "ball_3"} and frame_id >= idx + 1:
                position[0] += 0.012
                velocity = [0.08, 0.0, 0.0]
            if object_id == "ball_4" and frame_id >= 4:
                position[0] += 0.12
                velocity = [0.62, 0.0, 0.0]
            result[object_id] = {"position_m": position, "velocity_m_s": velocity, "rotation_deg": [0, 0, 0]}
        return result

    contacts = {
        1: [["ball_0", "ball_1"]],
        2: [["ball_1", "ball_2"]],
        3: [["ball_2", "ball_3"]],
        4: [["ball_3", "ball_4"]],
    }
    return [
        {
            "frame": frame_id,
            "time_s": round(frame_id * 0.2, 4),
            "objects": objects(frame_id),
            "contacts": [{"objects": pair, "frame": frame_id, "time_s": round(frame_id * 0.2, 4)} for pair in contacts.get(frame_id, [])],
        }
        for frame_id in range(5)
    ]


if __name__ == "__main__":
    unittest.main()
