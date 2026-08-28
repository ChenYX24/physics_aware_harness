from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.artifact_schema import write_json
from harness.runtime.fluid_surface_adapter import FluidSurfaceAdapterError, particle_centers_m, prepare_ue_surface_replay


class FluidSurfaceAdapterTests(unittest.TestCase):
    def test_replay_adapter_converts_units_handedness_and_winding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            surface = root / "surface_frames" / "frame_0000.obj"
            surface.parent.mkdir()
            surface.write_text("v 1 2 3\nv 0 0 0\nv 1 0 0\nf 1 2 3\n", encoding="utf-8")
            cache = root / "particle_cache.json"
            write_json(
                cache,
                {
                    "timebase": {"fps": 24},
                    "frames": [
                        {
                            "frame": 0,
                            "time_s": 0.0,
                            "surface": {"path": "surface_frames/frame_0000.obj", "vertex_count": 3, "triangle_count": 1},
                        }
                    ],
                },
            )

            manifest = prepare_ue_surface_replay(
                cache,
                root / "ue_replay",
                ue_asset_root="/Game/Test/Fluid",
                spatial_measurement_tolerance=1e-6,
            )

            converted = (root / "ue_replay" / "obj_frames_cm_lh" / "frame_0000.obj").read_text(encoding="utf-8")
            self.assertIn("v 100.00000000 -200.00000000 300.00000000", converted)
            self.assertIn("f 1 3 2", converted)
            self.assertEqual(manifest["frames"][0]["ue_asset_path"], "/Game/Test/Fluid/SM_Fluid_0000")
            self.assertEqual(manifest["ue"]["segmentation_id_policy"], "one stable instance id for every mesh frame")

    def test_replay_adapter_rejects_non_contiguous_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "particle_cache.json", {"timebase": {"fps": 24}, "frames": [{"frame": 2}]})
            with self.assertRaisesRegex(FluidSurfaceAdapterError, "contiguous"):
                prepare_ue_surface_replay(
                    root / "particle_cache.json",
                    root / "out",
                    ue_asset_root="/Game/Test",
                    spatial_measurement_tolerance=1e-6,
                )

    def test_replay_adapter_rejects_spatial_measurement_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(
                root / "particle_cache.json",
                {
                    "timebase": {"fps": 24},
                    "environment": {"measurements": [{"id": "span", "type": "axis_span", "axes": ["x"]}]},
                    "frames": [
                        {
                            "frame": 0,
                            "positions_m": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
                            "measurements": {"span": 0.5},
                        }
                    ],
                },
            )
            with self.assertRaisesRegex(FluidSurfaceAdapterError, "exceeds handoff tolerance") as raised:
                prepare_ue_surface_replay(
                    root / "particle_cache.json",
                    root / "out",
                    ue_asset_root="/Game/Test",
                    spatial_measurement_tolerance=1e-6,
                )
            self.assertEqual(raised.exception.code, "handoff_spatial_registration_failed")

    def test_particle_centers_drive_camera_without_moving_world_space_surface_mesh(self) -> None:
        centers = particle_centers_m(
            {
                "frames": [
                    {"frame": 0, "positions_m": [[0.0, 0.0, 0.6], [0.2, 0.0, 0.8]]},
                    {"frame": 1, "positions_m": [[0.0, 0.0, 0.4], [0.2, 0.0, 0.6]]},
                ]
            }
        )
        self.assertEqual(centers, [[0.1, 0.0, 0.7], [0.1, 0.0, 0.5]])


if __name__ == "__main__":
    unittest.main()
