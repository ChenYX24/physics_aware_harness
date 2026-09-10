from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.core.artifact_schema import write_json
from harness.core.runtime_case import RuntimeCase
from harness.runtime.taichi_cloth_backend import TaichiClothBackend
from scripts.harness_taichi_cloth import grid_faces, grid_pinned_mask, grid_positions


class TaichiClothBackendTests(unittest.TestCase):
    def test_general_grid_topology_builds_trapezoid_and_disconnected_panels(self) -> None:
        import numpy as np

        positions = grid_positions(
            5,
            [2.0, 1.0],
            np.zeros(3, dtype=np.float32),
            orientation="vertical_xz",
            width_scale_by_v=[1.0, 0.5],
            u_cuts=[{"fraction": 0.5, "gap_m": 0.2}],
        )
        pins = grid_pinned_mask(5, ["negative_v", "positive_v"])
        faces = grid_faces(5, u_cuts=[{"fraction": 0.5, "gap_m": 0.2}])

        self.assertEqual(int(pins.sum()), 10)
        self.assertGreater(float(positions[20, 0] - positions[0, 0]), float(positions[24, 0] - positions[4, 0]))
        self.assertEqual(len(faces), 24)

    def test_uses_external_workspace_for_taichi_cache(self) -> None:
        case = RuntimeCase(
            {
                "schema_version": "harness_runtime_case_v2",
                "case_id": "cloth_cache_probe",
                "capability_id": "deformable_body_dynamics",
                "prompt": "cloth cache probe",
                "should_pass": True,
                "objects": [{"id": "cloth", "role": "deformable_cloth"}],
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "taichi-python"
            executable.write_text("", encoding="utf-8")
            captured: dict = {}

            def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
                captured.update(kwargs)
                run_dir = Path(command[command.index("--output-dir") + 1])
                write_json(run_dir / "harness_verifier.json", {"status": "pass"})
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("harness.runtime.taichi_cloth_backend.taichi_python", return_value=executable),
                patch("harness.runtime.taichi_cloth_backend.workspace_root", return_value=root / "workspace"),
                patch("harness.runtime.taichi_cloth_backend.subprocess.run", side_effect=fake_run),
            ):
                TaichiClothBackend().run_case(case, root / "runs")

            self.assertEqual(
                captured["env"]["TI_OFFLINE_CACHE_FILE_PATH"],
                str(root / "workspace/cache/taichi"),
            )


if __name__ == "__main__":
    unittest.main()
