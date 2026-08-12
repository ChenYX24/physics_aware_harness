from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.artifact_schema import read_json, write_json
from harness.runtime.execution_profile import execution_profile, verified_run_status, write_execution_reports
from harness.runtime.render_pass_contract import enforce_ue_render_passes, normalize_passes


class ExecutionProfileTests(unittest.TestCase):
    def test_profiles_make_capture_cost_and_delivery_eligibility_explicit(self) -> None:
        smoke = execution_profile("smoke")
        candidate = execution_profile("candidate")
        publish = execution_profile("publish")

        self.assertEqual(smoke.views, ("event_closeup",))
        self.assertEqual(smoke.render_passes, ("rgb",))
        self.assertEqual(smoke.render_fps, 24)
        self.assertEqual((smoke.width, smoke.height), (1280, 720))
        self.assertEqual((candidate.width, candidate.height), (1920, 1080))
        self.assertEqual((publish.width, publish.height), (3840, 2160))
        self.assertEqual(smoke.physics_hz, 120)
        self.assertEqual(candidate.physics_hz, smoke.physics_hz)
        self.assertEqual(publish.physics_hz, smoke.physics_hz)
        self.assertFalse(smoke.complete_sensor_contract)
        self.assertTrue(candidate.complete_sensor_contract)
        self.assertEqual(len(candidate.views), 5)
        self.assertLess(candidate.width, publish.width)
        self.assertEqual(normalize_passes(smoke.render_passes), ["rgb"])
        self.assertEqual(enforce_ue_render_passes(smoke.render_passes), ["rgb", "depth", "segmentation"])

    def test_efficiency_report_uses_native_timing_and_promotes_smoke_only_after_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_json(
                run_dir / "inputs" / "render_config.json",
                {
                    "width": 320,
                    "height": 180,
                    "views": ["event_closeup"],
                    "passes": ["rgb"],
                    "timebase": {"canonical_frame_count": 37},
                    "execution_strategy": "single_process_shared_solver_multimodal",
                },
            )
            write_json(
                run_dir / "logs" / "native_combined" / "summary.json",
                {"timing": {"setup_seconds": 1.0, "capture_seconds": 10.0, "encode_seconds": 1.0, "total_seconds": 12.0}},
            )

            report = write_execution_reports(run_dir, execution_profile("smoke"), wall_seconds=13.0, status="completed")

            self.assertEqual(report["camera_modality_frames"], 37)
            self.assertEqual(report["solver_pass_count"], 1)
            self.assertEqual(report["throughput"]["camera_modality_frames_per_capture_second"], 3.7)
            self.assertEqual(report["promotion"]["next_profile"], "candidate")
            self.assertEqual(read_json(run_dir / "execution_profile.json")["artifact_eligibility"], "diagnostic_only")

    def test_promotion_status_requires_physics_and_render_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_json(run_dir / "harness_verifier.json", {"status": "fail"})
            write_json(run_dir / "render_sync_report.json", {"status": "pass"})
            self.assertEqual(verified_run_status(run_dir), "fail")
            write_json(run_dir / "harness_verifier.json", {"status": "pass"})
            self.assertEqual(verified_run_status(run_dir), "pass")
            (run_dir / "render_sync_report.json").unlink()
            self.assertEqual(verified_run_status(run_dir), "fail")

    def test_hybrid_genesis_ue_replay_does_not_claim_another_solver_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_json(
                run_dir / "inputs" / "render_config.json",
                {
                    "views": ["front_static"],
                    "passes": ["rgb", "depth", "segmentation"],
                    "timebase": {"canonical_frame_count": 19},
                    "execution_strategy": "genesis_once_surface_reconstruction_then_ue_mesh_replay",
                },
            )
            report = write_execution_reports(run_dir, execution_profile("candidate"), wall_seconds=10.0, status="pass")
            self.assertEqual(report["solver_pass_count"], 0)


if __name__ == "__main__":
    unittest.main()
