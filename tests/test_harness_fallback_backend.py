from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.artifact_schema import read_json
from harness.core.case_spec import load_case_spec
from harness.runtime.fallback_backend import FallbackBackend
from harness.verification.physics_verifier import PhysicsVerifier


ROOT = Path(__file__).resolve().parents[1]


class HarnessFallbackBackendTests(unittest.TestCase):
    def test_fallback_backend_generates_deterministic_artifacts(self) -> None:
        case = load_case_spec(ROOT / "cases" / "billiards" / "low_speed_single_contact.json")
        with tempfile.TemporaryDirectory() as tmp:
            run_a = FallbackBackend().run_case(case, Path(tmp) / "a")
            run_b = FallbackBackend().run_case(case, Path(tmp) / "b")
            traj_a = read_json(run_a / "fallback_output" / "trajectory.json")
            traj_b = read_json(run_b / "fallback_output" / "trajectory.json")
            self.assertEqual(traj_a, traj_b)
            self.assertTrue((run_a / "artifact_manifest.json").exists())
            self.assertTrue((run_a / "harness_artifact.json").exists())
            self.assertTrue((run_a / "trajectory.json").exists())
            self.assertTrue((run_a / "contact_events.json").exists())
            self.assertTrue((run_a / "camera_trajectory.json").exists())
            self.assertTrue((run_a / "render_manifest.json").exists())
            self.assertTrue((run_a / "run_readiness.json").exists())
            self.assertTrue((run_a / "camera_plan.json").exists())
            self.assertTrue((run_a / "views" / "front_static" / "rgb.mp4").exists())
            self.assertTrue((run_a / "fallback_output" / "contact_events.json").exists())
            contacts = read_json(run_a / "fallback_output" / "contact_events.json")
            self.assertEqual(contacts, [])
            verifier = PhysicsVerifier().verify_run_dir(run_a, write=True)
            self.assertEqual(verifier["status"], "pass")
            self.assertTrue((run_a / "harness_verifier.json").exists())
            self.assertTrue((run_a / "verifier_report.json").exists())

    def test_fallback_backend_can_emit_multiview_depth_contract(self) -> None:
        case = load_case_spec(ROOT / "cases" / "falling" / "falling_block_on_floor.json")
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = FallbackBackend().run_case(
                case,
                Path(tmp),
                requested_views=["overview", "front", "side", "top"],
                render_passes=["rgb", "depth", "segmentation"],
            )
            self.assertTrue((run_dir / "camera_plan.json").exists())
            self.assertTrue((run_dir / "render_pass_manifest.json").exists())
            self.assertTrue((run_dir / "views" / "overview" / "rgb.mp4").exists())
            self.assertTrue((run_dir / "views" / "overview" / "depth_placeholder.json").exists())
            readiness = read_json(run_dir / "run_readiness.json")
            self.assertTrue(readiness["camera_plan_ready"])
            self.assertTrue(readiness["multi_view_ready"])
            self.assertFalse(readiness["depth_ready"])
            self.assertFalse(readiness["ue_render_real"])


if __name__ == "__main__":
    unittest.main()
