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
    def test_prepare_and_retry_failed_keep_attempt_history(self) -> None:
        case_spec = read_json(ROOT / "cases" / "falling" / "falling_block_on_floor.json")
        case_spec["case_id"] = "falling_block__retry_smoke"
        manifest = {
            "schema_version": "harness_parameter_batch_v1",
            "batch_id": "retry_smoke",
            "case_route": "rigid_motion/falling/v001_gravity_contact",
            "entries": [
                {
                    "id": "baseline",
                    "case_spec": case_spec,
                    "render": {
                        "views": ["front_static"],
                        "passes": ["rgb"],
                        "resolution": {"width": 1280, "height": 720},
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "batch.json"
            workspace = root / "workspace"
            write_json(path, manifest)
            prepared = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_render_parameter_batch.py"),
                    str(path),
                    "--prepare",
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
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            queue_path = Path(json.loads(prepared.stdout)["queue"])
            queue = read_json(queue_path)
            attempt = queue["entries"][0]["attempts"][0]
            self.assertEqual(queue["entries"][0]["status"], "pending_render")
            self.assertEqual(attempt["file"]["status"], "generated")
            self.assertFalse(Path(attempt["render"]["output_root"]).exists())

            queue["entries"][0]["status"] = "render_failed"
            attempt["render"]["status"] = "failed"
            write_json(queue_path, queue)
            retried = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_render_parameter_batch.py"),
                    str(path),
                    "--execute",
                    "--retry-failed",
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
            self.assertEqual(retried.returncode, 0, retried.stderr or retried.stdout)
            queue = read_json(queue_path)
            self.assertEqual(queue["entries"][0]["status"], "validated")
            self.assertEqual(queue["entries"][0]["regeneration_count"], 1)
            self.assertEqual(len(queue["entries"][0]["attempts"]), 2)

    def test_execute_persists_selected_case_and_runs_existing_entrypoint(self) -> None:
        case_spec = read_json(
            ROOT
            / "cases"
            / "falling"
            / "falling_block_on_floor.json"
        )
        case_spec["case_id"] = "falling_block__batch_smoke"
        manifest = {
            "schema_version": "harness_parameter_batch_v1",
            "batch_id": "falling_block_parameter_batch",
            "case_route": "rigid_motion/falling/v001_gravity_contact",
            "entries": [
                {
                    "id": "batch_smoke",
                    "case_spec": case_spec,
                    "render": {
                        "views": ["event_closeup"],
                        "passes": ["rgb"],
                        "resolution": {"width": 1920, "height": 1080},
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
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            result = json.loads(completed.stdout)
            input_root = Path(result["input_root"])
            self.assertTrue((input_root / "batch_manifest.json").is_file())
            queue = read_json(input_root / "batch_queue.json")
            case_path = Path(queue["entries"][0]["attempts"][0]["file"]["path"])
            self.assertTrue(case_path.is_file())
            self.assertTrue((input_root / "batch_run.json").is_file())
            self.assertEqual(result["completed_count"], 1)
            self.assertEqual(result["failed_count"], 0)
            self.assertEqual(result["results"][0]["validation"], "passed")

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
                        "resolution": {"width": 1920, "height": 1080},
                    },
                },
                {
                    "id": "all",
                    "case_spec": all_case,
                    "render": {
                        "views": ["front_static", "side_static", "top_down"],
                        "passes": ["rgb", "depth", "segmentation"],
                        "resolution": {"width": 3840, "height": 2160},
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
        self.assertIn("--width 1920 --height 1080", result["commands"][0])
        self.assertIn("--render-passes rgb,depth,segmentation", result["commands"][1])
        self.assertIn("--mode both", result["commands"][1])
        self.assertIn("--width 3840 --height 2160", result["commands"][1])


if __name__ == "__main__":
    unittest.main()
