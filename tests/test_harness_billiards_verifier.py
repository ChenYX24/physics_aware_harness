from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.artifact_schema import write_json
from harness.core.case_spec import load_case_spec
from harness.runtime.fallback_backend import FallbackBackend, trajectory_for_case
from harness.verification.physics_verifier import PhysicsVerifier


ROOT = Path(__file__).resolve().parents[1]


class HarnessBilliardsVerifierTests(unittest.TestCase):
    def test_low_speed_single_contact_passes(self) -> None:
        report = run_case("cases/billiards/low_speed_single_contact.json")
        self.assertEqual(report["status"], "pass")

    def test_negative_precontact_motion_fails(self) -> None:
        report = run_case("cases/billiards/negative_precontact_motion.json")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F5_passive_precontact_motion")
        self.assertEqual(report["first_failure"]["object_id"], "target_ball_1")

    def test_static_active_body_cannot_pass_from_contacts_alone(self) -> None:
        case = load_case_spec(ROOT / "cases/billiards/low_speed_single_contact.json")
        trajectory = trajectory_for_case(case.data)
        cue_position = trajectory[0]["objects"]["cue_ball"]["position_m"]
        for frame in trajectory:
            frame["objects"]["cue_ball"]["position_m"] = cue_position

        report = PhysicsVerifier().verify(case.data, trajectory)

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["first_failure"]["metric"], "active_displacement_m")

    def test_full_rack_break_requires_contact_and_one_centimeter_motion_for_every_target(self) -> None:
        case = load_case_spec(ROOT / "cases/billiards/sixteen_ball_reference_break.json")
        trajectory = trajectory_for_case(case.data)
        self.assertEqual(PhysicsVerifier().verify(case.data, trajectory)["status"], "pass")

        missing_contact = trajectory_for_case(case.data)
        for frame in missing_contact:
            frame["contacts"] = [contact for contact in frame.get("contacts") or [] if "target_ball_15" not in set(contact.get("objects") or [])]
        report = PhysicsVerifier().verify(case.data, missing_contact)
        self.assertEqual(report["first_failure"]["metric"], "full_rack_passive_contact_missing")

        insufficient_motion = trajectory_for_case(case.data)
        initial = insufficient_motion[0]["objects"]["target_ball_15"]["position_m"]
        for frame in insufficient_motion:
            frame["objects"]["target_ball_15"]["position_m"] = list(initial)
        report = PhysicsVerifier().verify(case.data, insufficient_motion)
        self.assertEqual(report["first_failure"]["metric"], "full_rack_passive_displacement_m")

    def test_angled_rack_break_keeps_the_complete_passive_propagation_gate(self) -> None:
        case = load_case_spec(ROOT / "cases/billiards/sixteen_ball_reference_break.json")
        case.data["expected_physics"]["expected_spread"] = "angled_rack_break"
        trajectory = trajectory_for_case(case.data)
        for frame in trajectory:
            frame["contacts"] = [
                contact
                for contact in frame.get("contacts") or []
                if "target_ball_15" not in set(contact.get("objects") or [])
            ]

        report = PhysicsVerifier().verify(case.data, trajectory)

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["first_failure"]["metric"], "full_rack_passive_contact_missing")

    def test_run_dir_prefers_current_root_trajectory_over_stale_backend_copy(self) -> None:
        case = load_case_spec(ROOT / "cases/billiards/low_speed_single_contact.json")
        current_trajectory = trajectory_for_case(case.data)
        stale_trajectory = trajectory_for_case(case.data)
        for frame in stale_trajectory:
            frame["contacts"] = []

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "ue_output").mkdir()
            write_json(run_dir / "case_spec.json", case.data)
            write_json(run_dir / "trajectory.json", current_trajectory)
            write_json(run_dir / "ue_output" / "trajectory.json", stale_trajectory)
            write_json(run_dir / "ue_backend_report.json", {"status": "completed"})

            report = PhysicsVerifier().verify_run_dir(run_dir)

        self.assertEqual(report["status"], "pass", report)


def run_case(rel_path: str) -> dict:
    case = load_case_spec(ROOT / rel_path)
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = FallbackBackend().run_case(case, tmp)
        return PhysicsVerifier().verify_run_dir(run_dir)


if __name__ == "__main__":
    unittest.main()
