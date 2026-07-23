from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness.core.artifact_schema import read_json, write_json


ROOT = Path(__file__).resolve().parents[1]


class ParameterBatchTests(unittest.TestCase):
    def test_execute_persists_selected_case_and_runs_existing_entrypoint(self) -> None:
        case_spec = read_json(
            ROOT
            / "cases"
            / "fracture"
            / "glass_energy_response_matrix"
            / "glass_panel_e16_shatter.json"
        )
        case_spec["case_id"] = "glass_panel__batch_smoke"
        manifest = {
            "schema_version": "harness_parameter_batch_v1",
            "batch_id": "glass_panel_parameter_batch",
            "case_route": "brittle_fracture/glass_panel/v001_energy_response",
            "entries": [
                {
                    "id": "batch_smoke",
                    "case_spec": case_spec,
                    "render": {
                        "views": ["event_closeup"],
                        "passes": ["rgb"],
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "batch.json"
            workspace = root / "workspace"
            write_json(path, manifest)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_render_parameter_batch.py"),
                    str(path),
                    "--execute",
                    "--backend",
                    "fallback",
                    "--workspace",
                    str(workspace),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            input_root = Path(result["input_root"])
            self.assertTrue((input_root / "batch_manifest.json").is_file())
            self.assertTrue((input_root / "glass_panel__batch_smoke.json").is_file())
            self.assertTrue((input_root / "batch_run.json").is_file())
            self.assertEqual(result["completed_count"], 1)
            self.assertEqual(result["failed_count"], 0)

    def test_dry_run_validates_embedded_cases_and_builds_per_variant_capture_commands(self) -> None:
        base = read_json(
            ROOT
            / "cases"
            / "fracture"
            / "glass_energy_response_matrix"
            / "glass_panel_e16_shatter.json"
        )
        rgb_case = json.loads(json.dumps(base))
        rgb_case["case_id"] = "glass_panel__rgb"
        all_case = json.loads(json.dumps(base))
        all_case["case_id"] = "glass_panel__all"
        manifest = {
            "schema_version": "harness_parameter_batch_v1",
            "batch_id": "glass_panel_parameter_batch",
            "case_route": "brittle_fracture/glass_panel/v001_energy_response",
            "entries": [
                {
                    "id": "rgb",
                    "case_spec": rgb_case,
                    "render": {
                        "views": ["front_static", "event_closeup"],
                        "passes": ["rgb"],
                    },
                },
                {
                    "id": "all",
                    "case_spec": all_case,
                    "render": {
                        "views": ["front_static", "side_static", "top_down"],
                        "passes": ["rgb", "depth", "segmentation"],
                    },
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch.json"
            write_json(path, manifest)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_render_parameter_batch.py"),
                    str(path),
                    "--dry-run",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["schema_version"], "harness_parameter_batch_preview_v1")
        self.assertEqual(result["entry_count"], 2)
        self.assertIn("--views front_static,event_closeup", result["commands"][0])
        self.assertIn("--render-passes rgb", result["commands"][0])
        self.assertIn("--mode rgb", result["commands"][0])
        self.assertIn("--render-passes rgb,depth,segmentation", result["commands"][1])
        self.assertIn("--mode both", result["commands"][1])


if __name__ == "__main__":
    unittest.main()
