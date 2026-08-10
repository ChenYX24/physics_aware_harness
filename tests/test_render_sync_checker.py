from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.artifact_schema import read_json, write_json
from harness.runtime.camera_planner import SceneBounds, camera_plan_to_dict, plan_cameras_for_scene
from harness.verification.render_sync_checker import check_render_sync

EXR = b"\x76\x2f\x31\x01"
MP4 = b"\x00\x00\x00\x18ftypisom"


class RenderSyncCheckerTests(unittest.TestCase):
    def test_valid_multiview_ue_outputs_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_valid_views(run_dir)
            report = check_render_sync(run_dir)
            self.assertEqual(report["status"], "pass")
            self.assertTrue(report["ue_render_real"])
            self.assertEqual(report["depth_source"], "ue")
            self.assertTrue(report["multi_view_sync_ok"])
            self.assertTrue((run_dir / "render_sync_report.json").exists())

    def test_depth_missing_is_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_valid_views(run_dir)
            (run_dir / "views" / "overview" / "depth.exr").unlink()
            report = check_render_sync(run_dir)
            self.assertEqual(report["status"], "fail")
            self.assertIn("F_DEPTH_MISSING", report["failure_codes"])
            self.assertEqual(report["render_observability_fail"], 1)

    def test_rgb_only_native_ue_capture_is_real_without_depth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_valid_views(run_dir)
            for camera_id in ("overview", "side"):
                view_dir = run_dir / "views" / camera_id
                (view_dir / "depth.exr").unlink()
                (view_dir / "segmentation.exr").unlink()
                meta_path = view_dir / "meta.json"
                meta = read_json(meta_path)
                meta.update({"native_ue_rgb": True, "frame_count_depth": 0, "frame_count_segmentation": 0, "depth_source": "missing"})
                write_json(meta_path, meta)

            report = check_render_sync(run_dir, require_depth=False, require_segmentation=False)

            self.assertEqual(report["status"], "pass")
            self.assertTrue(report["ue_render_real"])
            self.assertEqual(report["depth_source"], "missing")

    def test_timestamp_mismatch_is_sync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_valid_views(run_dir)
            meta_path = run_dir / "views" / "overview" / "meta.json"
            meta = read_json(meta_path)
            meta["timestamps_depth"] = [0.0, 0.1, 0.3]
            write_json(meta_path, meta)
            report = check_render_sync(run_dir)
            self.assertEqual(report["status"], "fail")
            self.assertIn("F_RENDER_SYNC_FAIL", report["failure_codes"])

    def test_missing_view_is_view_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_valid_views(run_dir)
            for path in (run_dir / "views" / "side").iterdir():
                path.unlink()
            (run_dir / "views" / "side").rmdir()
            report = check_render_sync(run_dir)
            self.assertEqual(report["status"], "fail")
            self.assertIn("F_VIEW_MISMATCH", report["failure_codes"])

    def test_native_view_id_must_exactly_match_planned_camera_id(self) -> None:
        for source_native_view_id in (None, "side"):
            with self.subTest(source_native_view_id=source_native_view_id), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp)
                write_valid_views(run_dir)
                meta_path = run_dir / "views" / "overview" / "meta.json"
                meta = read_json(meta_path)
                if source_native_view_id is None:
                    meta.pop("source_native_view_id")
                else:
                    meta["source_native_view_id"] = source_native_view_id
                write_json(meta_path, meta)

                report = check_render_sync(run_dir)

                self.assertEqual(report["status"], "fail")
                self.assertIn("F_VIEW_MISMATCH", report["failure_codes"])

    def test_native_camera_echo_must_match_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_valid_views(run_dir)
            trajectory = read_json(run_dir / "camera_trajectory.json")
            trajectory["views"][0]["frames"][0]["fov"] = 35.0
            write_json(run_dir / "camera_trajectory.json", trajectory)

            report = check_render_sync(run_dir)

            self.assertEqual(report["status"], "fail")
            self.assertIn("F_CAMERA_STATE_MISMATCH", report["failure_codes"])

    def test_magic_and_legacy_segmentation_extension_are_hard_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_valid_views(run_dir)
            view_dir = run_dir / "views" / "overview"
            (view_dir / "rgb.mp4").write_bytes(b"not mp4")
            (view_dir / "depth.exr").write_bytes(b"not exr")
            (view_dir / "segmentation.exr").rename(view_dir / "segmentation.png")

            report = check_render_sync(run_dir)

            self.assertTrue({"F_RGB_MAGIC_INVALID", "F_DEPTH_MAGIC_INVALID", "F_SEGMENTATION_EXTENSION_MISMATCH"}.issubset(report["failure_codes"]))
            self.assertTrue(report["views"]["overview"]["segmentation_extension_mismatch"])

    def test_segmentation_sequence_count_must_match_rgb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_valid_views(run_dir)
            meta_path = run_dir / "views" / "overview" / "meta.json"
            meta = read_json(meta_path)
            meta["frame_count_segmentation"] = 2
            write_json(meta_path, meta)

            report = check_render_sync(run_dir)

            self.assertEqual(report["status"], "fail")
            self.assertTrue(any(item["message"] == "rgb/segmentation frame count mismatch" for item in report["failures"]))

    def test_fracture_sensor_state_mismatch_is_final_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_valid_views(run_dir)
            write_json(
                run_dir / "fracture_sensor_state_report.json",
                {
                    "schema_version": "harness_fracture_sensor_state_report_v1",
                    "status": "fail",
                    "failure_codes": ["F_FRACTURE_SENSOR_STATE_MISMATCH"],
                    "rgb_event_keys": [["glass_panel", 26]],
                    "data_event_keys": [],
                },
            )

            report = check_render_sync(run_dir)

            self.assertEqual(report["status"], "fail")
            self.assertFalse(report["fracture_sensor_state_ready"])
            self.assertIn("F_FRACTURE_SENSOR_STATE_MISMATCH", report["failure_codes"])

    def test_matching_fracture_sensor_state_preserves_render_sync_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_valid_views(run_dir)
            write_json(
                run_dir / "fracture_sensor_state_report.json",
                {
                    "schema_version": "harness_fracture_sensor_state_report_v2",
                    "status": "pass",
                    "comparison_required": True,
                    "failure_codes": [],
                    "rgb_event_keys": [["glass_panel", 26]],
                    "data_event_keys": [["glass_panel", 26]],
                    "rgb_fragment_state_hashes": {"glass_panel@26": "same"},
                    "data_fragment_state_hashes": {"glass_panel@26": "same"},
                },
            )

            report = check_render_sync(run_dir)

            self.assertEqual(report["status"], "pass")
            self.assertTrue(report["fracture_sensor_state_ready"])

    def test_declared_fracture_sync_pass_without_fragment_hashes_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_valid_views(run_dir)
            write_json(
                run_dir / "fracture_sensor_state_report.json",
                {
                    "schema_version": "harness_fracture_sensor_state_report_v2",
                    "status": "pass",
                    "comparison_required": True,
                    "failure_codes": [],
                    "rgb_event_keys": [["glass_panel", 26]],
                    "data_event_keys": [["glass_panel", 26]],
                },
            )

            report = check_render_sync(run_dir)

            self.assertEqual(report["status"], "fail")
            self.assertIn("F_FRACTURE_FRAGMENT_STATE_MISSING", report["failure_codes"])


def write_valid_views(run_dir: Path) -> None:
    plan = plan_cameras_for_scene(SceneBounds(center=(0, 0, 0.5), extent=(2, 2, 1)), requested_views=["overview", "side"])
    write_json(run_dir / "camera_plan.json", camera_plan_to_dict(plan))
    write_json(
        run_dir / "camera_trajectory.json",
        {
            "schema_version": "camera_trajectories_v1",
            "frame_count": 3,
            "fps": 10,
            "views": [
                {
                    "view_id": view.camera_id,
                    "camera_mode": "fixed",
                    "frames": [
                        {"frame": frame, "location_cm": [value * 100 for value in view.location], "target_cm": [value * 100 for value in view.target], "fov": view.fov}
                        for frame in range(3)
                    ],
                }
                for view in plan.views
            ],
        },
    )
    for camera_id in ("overview", "side"):
        view_dir = run_dir / "views" / camera_id
        view_dir.mkdir(parents=True, exist_ok=True)
        (view_dir / "rgb.mp4").write_bytes(MP4)
        (view_dir / "depth.exr").write_bytes(EXR + b"depth")
        (view_dir / "segmentation.exr").write_bytes(EXR + b"seg")
        write_json(
            view_dir / "meta.json",
            {
                "camera_id": camera_id,
                "source_native_view_id": camera_id,
                "frame_count_rgb": 3,
                "frame_count_depth": 3,
                "frame_count_segmentation": 3,
                "timestamps_rgb": [0.0, 0.1, 0.2],
                "timestamps_depth": [0.0, 0.1, 0.2],
                "timestamps_segmentation": [0.0, 0.1, 0.2],
                "depth_source": "ue",
                "depth_variance": 1.25,
                "segmentation_type": "instance",
                "instance_level": True,
                "render_time_sec": 0.05,
            },
        )


if __name__ == "__main__":
    unittest.main()
