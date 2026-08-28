from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.verification.depth_geometry_verifier import verify_depth_geometry


class DepthGeometryVerifierTests(unittest.TestCase):
    def test_matching_table_plane_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_billiards_run(Path(tmp))
            with patch(
                "harness.verification.depth_geometry_verifier.decode_exr_planes",
                side_effect=self.synthetic_planes,
            ):
                report = verify_depth_geometry(run_dir)

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["views"]["top_down"]["frames"][0]["support_pixel_count"], 1600)
            self.assertEqual(report["views"]["top_down"]["frames"][0]["mae_cm"], 0.0)
            self.assertTrue((run_dir / "depth_geometry_report.json").is_file())

    def test_wrong_depth_fails_the_hard_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_billiards_run(Path(tmp))

            def wrong_depth(path: Path, width: int, height: int, pixel_format: str, **_: object):
                planes = self.synthetic_planes(path, width, height, pixel_format)
                return [[0.2] * (width * height)] * 4 if "depth" in path.name else planes

            with patch(
                "harness.verification.depth_geometry_verifier.decode_exr_planes",
                side_effect=wrong_depth,
            ):
                report = verify_depth_geometry(run_dir, write=False)

            self.assertEqual(report["status"], "fail")
            self.assertIn("F_DEPTH_GEOMETRY_MAE", report["failure_codes"])
            self.assertIn("F_DEPTH_GEOMETRY_P95", report["failure_codes"])
            self.assertIn("F_DEPTH_GEOMETRY_SCALE", report["failure_codes"])

    def test_wrong_metadata_fails_before_pixel_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_billiards_run(Path(tmp))
            meta_path = run_dir / "views" / "top_down" / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["depth_type"] = "ray_distance"
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            with patch(
                "harness.verification.depth_geometry_verifier.decode_exr_planes",
                side_effect=self.synthetic_planes,
            ):
                report = verify_depth_geometry(run_dir, write=False)

            self.assertEqual(report["status"], "fail")
            self.assertIn("F_DEPTH_GEOMETRY_METADATA", report["failure_codes"])

    def test_run_without_explicit_reference_is_not_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self.write_json(run_dir / "case_spec.json", {"case_id": "falling_cube", "objects": [{"id": "cube"}]})

            report = verify_depth_geometry(run_dir)

            self.assertEqual(report["status"], "not_applicable")
            self.assertFalse(report["applicable"])

    @staticmethod
    def synthetic_planes(path: Path, width: int, height: int, pixel_format: str, **_: object) -> list[list[float]]:
        size = width * height
        if "depth" in path.name:
            return [[0.009] * size] * 4
        # ffmpeg gbrpf32le is planar G, B, R. The table RGB is (0.6, 0.5, 0.1).
        return [[0.5] * size, [0.1] * size, [0.6] * size]

    def make_billiards_run(self, run_dir: Path) -> Path:
        self.write_json(
            run_dir / "case_spec.json",
            {
                "case_id": "sixteen_ball_reference_break",
                "depth_geometry_reference": {"object_id": "table"},
                "objects": [{"id": "cue_ball"}, {"id": "target_ball_01"}, {"id": "table"}],
            },
        )
        self.write_json(
            run_dir / "logs" / "native_data" / "summary.json",
            {"runtime_actor_bounds": {"table": {"origin": [0.0, 0.0, 5.0], "extent": [100.0, 100.0, 5.0]}}},
        )
        frames = [
            {
                "frame": 0,
                "time": 0.0,
                "location_cm": [0.0, 0.0, 100.0],
                "target_cm": [0.0, 0.0, 10.0],
                "fov": 52.0,
                "camera_mode": "fixed",
            }
        ]
        self.write_json(
            run_dir / "camera_trajectory.json",
            {"schema_version": "camera_trajectories_v1", "views": [{"view_id": "top_down", "frames": frames}]},
        )
        view_dir = run_dir / "views" / "top_down"
        (view_dir / "depth_frames").mkdir(parents=True)
        (view_dir / "segmentation_frames").mkdir(parents=True)
        (view_dir / "depth_frames" / "depth_0000.exr").touch()
        (view_dir / "segmentation_frames" / "segmentation_0000.exr").touch()
        self.write_json(
            view_dir / "meta.json",
            {
                "camera_id": "top_down",
                "camera_intrinsics": {"width": 40, "height": 40, "cx": 20.0, "cy": 20.0, "fx": 40.0, "fy": 40.0},
                "depth_type": "view_z",
                "depth_encoding": "linear_view_z_times_0.0001",
                "depth_stored_value_to_centimeter": 10000.0,
                "depth_unit": "centimeter",
                "instance_mapping": [{"instance_id": 17, "object_id": "table", "rgb": [0.6, 0.5, 0.1]}],
            },
        )
        return run_dir

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
