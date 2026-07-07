from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def ue_env_without_config() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "SIM_STUDIO_UE_PROJECT",
        "SIM_STUDIO_UE_EXECUTABLE",
        "SIM_STUDIO_UE_MAP",
        "SIM_STUDIO_UE_ACTOR_CLASS",
        "SIM_STUDIO_ASSET_REGISTRY",
        "SIM_STUDIO_UE_CONTACT_EXPORT",
        "SIM_STUDIO_UE_RUNNER_CMD",
    ):
        env.pop(key, None)
    return env


def ue_env_with_fake_config(tmp: str, *, project: Path | None = None, runner_cmd: str | None = None) -> dict[str, str]:
    root = Path(tmp)
    fake_project = project or (root / "HarnessSmoke.uproject")
    if project is None:
        fake_project.write_text("{}", encoding="utf-8")
    fake_executable = root / "UnrealEditor-Cmd"
    fake_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_asset_registry = root / "asset_registry.json"
    fake_asset_registry.write_text("{}", encoding="utf-8")
    env = ue_env_without_config()
    env.update(
        {
            "SIM_STUDIO_UE_PROJECT": str(fake_project),
            "SIM_STUDIO_UE_EXECUTABLE": str(fake_executable),
            "SIM_STUDIO_UE_MAP": "/Game/Maps/HarnessSmoke",
            "SIM_STUDIO_UE_ACTOR_CLASS": "/Game/Blueprints/BP_HarnessSceneRunner.BP_HarnessSceneRunner_C",
            "SIM_STUDIO_ASSET_REGISTRY": str(fake_asset_registry),
            "SIM_STUDIO_UE_CONTACT_EXPORT": "1",
        }
    )
    if runner_cmd is not None:
        env["SIM_STUDIO_UE_RUNNER_CMD"] = runner_cmd
    return env


def write_fake_ue_runner(path: Path) -> None:
    path.write_text(
        """
from __future__ import annotations

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--case-spec")
parser.add_argument("--run-dir", required=True)
parser.add_argument("--camera-plan")
parser.add_argument("--actor-placement")
args, _ = parser.parse_known_args()
run_dir = Path(args.run_dir)
run_dir.mkdir(parents=True, exist_ok=True)
actor_placement_path = Path(args.actor_placement or "")
if not actor_placement_path.exists():
    (run_dir / "local_ue_runner_report.json").write_text(json.dumps({
        "schema_version": "harness_local_ue_runner_report.v1",
        "status": "failed",
        "failure_code": "F_RUNTIME_ACTOR_PLACEMENT_MISSING",
        "failure_message": "actor placement path was not passed or does not exist"
    }), encoding="utf-8")
    raise SystemExit(2)
actor_placement = json.loads(actor_placement_path.read_text(encoding="utf-8"))
(run_dir / "runner_received_args.json").write_text(json.dumps({
    "actor_placement": str(actor_placement_path),
    "actor_count": len(actor_placement.get("actor_bindings", []))
}), encoding="utf-8")
trajectory = [
    {"frame": 0, "time_s": 0.0, "objects": {"floor": {"position_m": [0, 0, 0], "velocity_m_s": [0, 0, 0]}, "falling_block": {"position_m": [0, 0, 1.2], "velocity_m_s": [0, 0, 0]}}, "contacts": []},
    {"frame": 1, "time_s": 0.2, "objects": {"floor": {"position_m": [0, 0, 0], "velocity_m_s": [0, 0, 0]}, "falling_block": {"position_m": [0, 0, 0.6], "velocity_m_s": [0, 0, -2.0]}}, "contacts": []},
    {"frame": 2, "time_s": 0.4, "objects": {"floor": {"position_m": [0, 0, 0], "velocity_m_s": [0, 0, 0]}, "falling_block": {"position_m": [0, 0, 0.1], "velocity_m_s": [0, 0, 0]}}, "contacts": [{"objects": ["falling_block", "floor"], "frame": 2, "time_s": 0.4, "method": "fake_ue_contact"}]},
]
(run_dir / "trajectory.json").write_text(json.dumps(trajectory), encoding="utf-8")
(run_dir / "contact_events.json").write_text(json.dumps(trajectory[-1]["contacts"]), encoding="utf-8")
(run_dir / "camera_trajectory.json").write_text(json.dumps({"schema_version": "harness_camera_trajectory_v1", "available": True, "frames": [{"frame": 0, "time_s": 0.0, "camera": "main"}]}), encoding="utf-8")
camera_plan = json.loads(Path(args.camera_plan).read_text(encoding="utf-8"))
render_views = []
for view in camera_plan["views"]:
    camera_id = view["camera_id"]
    view_dir = run_dir / "views" / camera_id
    view_dir.mkdir(parents=True, exist_ok=True)
    (view_dir / "rgb.mp4").write_bytes(b"fake view rgb")
    (view_dir / "depth.exr").write_bytes(b"fake ue depth")
    (view_dir / "segmentation.png").write_bytes(b"fake instance segmentation")
    (view_dir / "meta.json").write_text(json.dumps({
        "camera_id": camera_id,
        "frame_count_rgb": 3,
        "frame_count_depth": 3,
        "timestamps_rgb": [0.0, 0.1, 0.2],
        "timestamps_depth": [0.0, 0.1, 0.2],
        "fps": 10,
        "depth_source": "ue",
        "depth_variance": 1.0,
        "segmentation_type": "instance",
        "instance_level": True,
        "render_time_sec": 0.05
    }), encoding="utf-8")
    render_views.append({"camera_id": camera_id, "rgb": f"views/{camera_id}/rgb.mp4", "depth": f"views/{camera_id}/depth.exr", "segmentation": f"views/{camera_id}/segmentation.png"})
(run_dir / "render_manifest.json").write_text(json.dumps({"schema_version": "harness_render_manifest_v1", "backend": "ue", "render_available": True, "views": render_views, "passes": [{"name": "rgb"}, {"name": "depth"}, {"name": "segmentation"}]}), encoding="utf-8")
(run_dir / "video.mp4").write_bytes((run_dir / "views" / camera_plan["views"][0]["camera_id"] / "rgb.mp4").read_bytes())
""".lstrip(),
        encoding="utf-8",
    )


def write_fake_failing_ue_runner(path: Path) -> None:
    path.write_text(
        """
from __future__ import annotations

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--run-dir", required=True)
args, _ = parser.parse_known_args()
run_dir = Path(args.run_dir)
run_dir.mkdir(parents=True, exist_ok=True)
(run_dir / "local_ue_runner_report.json").write_text(json.dumps({
    "schema_version": "harness_local_ue_runner_report.v1",
    "status": "failed",
    "failure_code": "F_RGB_HIGHRES_VIEWPORT_UNAVAILABLE",
    "failure_message": "RGB highres viewport capture did not produce screenshot frames in this UE launch mode."
}), encoding="utf-8")
raise SystemExit(2)
""".lstrip(),
        encoding="utf-8",
    )


class HarnessCliTests(unittest.TestCase):
    def test_cli_help_commands_do_not_error(self) -> None:
        for script in (
            "harness_list_capabilities.py",
            "harness_run_case.py",
            "harness_verify_run.py",
            "harness_package_dataset.py",
            "harness_smoke.py",
            "harness_generate_cases.py",
            "harness_run_case_batch.py",
            "harness_verify_batch.py",
            "harness_build_static_scene.py",
            "harness_compile_actor_placement.py",
        ):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--help"], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(result.returncode, 0, result.stderr)
        experiment_help = subprocess.run([sys.executable, str(ROOT / "run_experiment.py"), "--help"], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(experiment_help.returncode, 0, experiment_help.stderr)

    def test_list_capabilities_exposes_layered_taxonomy_without_scene_aliases(self) -> None:
        default = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "harness_list_capabilities.py"), "--json"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(default.returncode, 0, default.stderr)
        payload = json.loads(default.stdout)
        default_ids = {item["id"] for item in payload["capabilities"]}
        self.assertIn("rigid_body_contact_causality", default_ids)
        self.assertNotIn("billiard_causality_compiler", default_ids)
        taxonomy = payload["capability_taxonomy"]
        self.assertIn("prompt_case_capability_planning", taxonomy["pipeline_stage_capabilities"])
        self.assertIn("asset_intent_resolution", taxonomy["asset_operation_capabilities"])
        self.assertIn("blueprint_function_invocation", taxonomy["runtime_bridge_capabilities"])
        self.assertIn("physics_parameter_semantics", taxonomy["physical_property_constraint_capabilities"])
        self.assertIn("physics_verifier_truth_gate", taxonomy["verification_capabilities"])
        self.assertIn("rigid_body_contact_causality", taxonomy["physics_behavior_capabilities"])
        self.assertEqual(taxonomy["pipeline_execution_order"][0], "prompt_case_capability_planning")
        self.assertLess(
            taxonomy["pipeline_execution_order"].index("runtime_actor_placement_compilation"),
            taxonomy["pipeline_execution_order"].index("runtime_backend_execution"),
        )
        self.assertEqual(taxonomy["deprecated_aliases"]["billiard_causality_compiler"], "rigid_body_contact_causality")

        with_deprecated = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "harness_list_capabilities.py"), "--json", "--include-deprecated"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(with_deprecated.returncode, 0, with_deprecated.stderr)
        deprecated_ids = {item["id"] for item in json.loads(with_deprecated.stdout)["capabilities"]}
        self.assertNotIn("billiard_causality_compiler", deprecated_ids)

    def test_harness_smoke_outputs_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "harness_smoke.py"), "--backend", "fallback", "--output-root", tmp, "--timestamp", "unit"],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["schema_version"], "harness_smoke_summary_v1")
            self.assertEqual(summary["case_count"], summary["expectation_met_count"])

    def test_generated_cases_batch_outputs_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "generated"
            generated = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_generate_cases.py"),
                    "--suite",
                    "billiards",
                    "--count",
                    "4",
                    "--seed",
                    "7",
                    "--out",
                    str(case_dir),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            manifest = json.loads((case_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "harness_generated_case_manifest_v1")
            self.assertEqual(manifest["num_cases"], 4)
            self.assertEqual(len(manifest["cases"]), 4)
            generated_case_path = next(path for path in case_dir.glob("*.json") if path.name != "manifest.json")
            generated_case = json.loads(generated_case_path.read_text(encoding="utf-8"))
            for key in (
                "task_type",
                "scene",
                "initial_state",
                "physical_parameters",
                "expected_event",
                "negative_or_boundary",
                "asset_requirements",
                "allowed_proxy_policy",
                "verification_rules",
            ):
                self.assertIn(key, generated_case)
            batch = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_run_case_batch.py"),
                    str(case_dir),
                    "--backend",
                    "fallback",
                    "--output-root",
                    str(Path(tmp) / "runs"),
                    "--timestamp",
                    "unit",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(batch.returncode, 0, batch.stderr)
            summary = json.loads(batch.stdout)
            self.assertEqual(summary["schema_version"], "harness_batch_run_summary_v1")
            self.assertEqual(summary["case_count"], 4)
            self.assertEqual(summary["unexpected_count"], 0)
            verify = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_verify_batch.py"),
                    summary["output_root"],
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(verify.returncode, 0, verify.stderr)
            verify_summary = json.loads(verify.stdout)
            self.assertEqual(verify_summary["schema_version"], "harness_batch_verifier_summary_v1")
            self.assertEqual(verify_summary["case_count"], 4)

    def test_ue_backend_fails_with_artifact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = ue_env_without_config()
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_run_case.py"),
                    "cases/billiards/low_speed_single_contact.json",
                    "--backend",
                    "ue",
                    "--output-root",
                    tmp,
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 2)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["backend"], "ue")
            self.assertEqual(summary["failure_type"], "F1_UPROJECT_MISSING")
            self.assertEqual(summary["failure_category"], "preflight_failure")
            self.assertFalse(summary["real_ue_invoked"])
            run_dir = Path(summary["run_dir"])
            self.assertTrue((run_dir / "scene_spec.json").exists())
            self.assertTrue((run_dir / "harness_artifact.json").exists())
            self.assertTrue((run_dir / "harness_verifier.json").exists())
            self.assertTrue((run_dir / "verifier_report.json").exists())
            self.assertTrue((run_dir / "ue_preflight_report.json").exists())
            self.assertTrue((run_dir / "ue_backend_report.json").exists())
            self.assertTrue((run_dir / "trajectory.json").exists())
            self.assertTrue((run_dir / "contact_events.json").exists())
            self.assertTrue((run_dir / "camera_trajectory.json").exists())
            self.assertTrue((run_dir / "render_manifest.json").exists())
            self.assertFalse((run_dir / "fallback_output").exists())
            preflight = json.loads((run_dir / "ue_preflight_report.json").read_text(encoding="utf-8"))
            backend_report = json.loads((run_dir / "ue_backend_report.json").read_text(encoding="utf-8"))
            for key in (
                "backend_mode",
                "requested_case_id",
                "env_presence",
                "resolved_paths",
                "failure_code",
                "failure_message",
                "next_required_action",
                "whether_real_ue_invoked",
            ):
                self.assertIn(key, preflight)
                self.assertIn(key, backend_report)

    def test_ue_batch_reports_preflight_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = ue_env_without_config()
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_run_case_batch.py"),
                    "cases/falling",
                    "--backend",
                    "ue",
                    "--output-root",
                    str(Path(tmp) / "runs"),
                    "--timestamp",
                    "unit",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 1)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["preflight_failure_count"], summary["case_count"])
            self.assertEqual(summary["real_ue_invoked_count"], 0)
            self.assertEqual(summary["video_count"], 0)
            self.assertEqual(summary["video_missing_expected_count"], summary["case_count"])
            self.assertEqual(summary["failure_code_distribution"], {"F1_UPROJECT_MISSING": summary["case_count"]})
            self.assertEqual(summary["expected_negative_caught_count"], 0)

    def test_ue_runner_command_is_required_after_preflight_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = ue_env_with_fake_config(tmp)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_run_case.py"),
                    "cases/falling/falling_block_on_floor.json",
                    "--backend",
                    "ue",
                    "--output-root",
                    tmp,
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 2)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["failure_type"], "F7_UE_RUNNER_CMD_MISSING")
            self.assertEqual(summary["failure_category"], "preflight_failure")
            self.assertFalse(summary["real_ue_invoked"])

    def test_ue_project_must_be_uproject_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            invalid_projects = [tmp_path / "ProjectDirectory", tmp_path / "Project.txt"]
            invalid_projects[0].mkdir()
            invalid_projects[1].write_text("not a uproject", encoding="utf-8")
            for project in invalid_projects:
                with self.subTest(project=project.name):
                    env = ue_env_with_fake_config(tmp, project=project)
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / "scripts" / "harness_run_case.py"),
                            "cases/falling/falling_block_on_floor.json",
                            "--backend",
                            "ue",
                            "--output-root",
                            str(tmp_path / f"run_{project.name}"),
                        ],
                        cwd=ROOT,
                        env=env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    self.assertEqual(result.returncode, 2)
                    summary = json.loads(result.stdout)
                    self.assertEqual(summary["failure_type"], "F1_UPROJECT_INVALID")
                    self.assertFalse(summary["real_ue_invoked"])

    def test_fake_ue_runner_success_writes_full_artifact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_runner = tmp_path / "fake_ue_runner.py"
            write_fake_ue_runner(fake_runner)
            runner_cmd = f"{sys.executable} {fake_runner} --case-spec {{case_spec}} --run-dir {{run_dir}}"
            env = ue_env_with_fake_config(tmp, runner_cmd=runner_cmd)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_run_case.py"),
                    "cases/falling/falling_block_on_floor.json",
                    "--backend",
                    "ue",
                    "--output-root",
                    tmp,
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            run_dir = Path(summary["run_dir"])
            for name in (
                "manifest.json",
                "harness_artifact.json",
                "artifact_manifest.json",
                "run_readiness.json",
                "render_pass_manifest.json",
                "render_sync_report.json",
                "verifier_report.json",
                "ue_backend_report.json",
                "runtime_actor_placement.json",
                "runtime_actor_placement_report.json",
                "video.mp4",
                "inputs/case.json",
                "inputs/scene.json",
                "inputs/camera.json",
                "inputs/render_config.json",
                "passes/rgb/video.mp4",
                "passes/data/depth.exr",
                "passes/data/mask.png",
                "passes/data/instance.json",
                "sync/camera_trajectory.json",
                "sync/physics_trace.json",
                "sync/sync_report.json",
            ):
                self.assertTrue((run_dir / name).exists(), name)
                self.assertGreater((run_dir / name).stat().st_size, 0, name)
            backend_report = json.loads((run_dir / "ue_backend_report.json").read_text(encoding="utf-8"))
            received = json.loads((run_dir / "runner_received_args.json").read_text(encoding="utf-8"))
            readiness = json.loads((run_dir / "run_readiness.json").read_text(encoding="utf-8"))
            self.assertEqual(backend_report["status"], "completed")
            self.assertTrue(backend_report["whether_real_ue_invoked"])
            self.assertIn("--actor-placement", backend_report["runner_command"])
            self.assertGreater(received["actor_count"], 0)
            self.assertTrue(readiness["visual_ready"])
            self.assertTrue(readiness["physics_ready"])
            self.assertTrue(readiness["ue_render_real"])
            self.assertEqual(readiness["depth_source"], "ue")
            self.assertTrue(readiness["multi_view_sync_ok"])
            self.assertTrue(readiness["render_pass_valid"])
            self.assertEqual(readiness["render_observability_fail"], 0)
            self.assertTrue((run_dir / "views" / "front_static" / "rgb.mp4").exists())
            self.assertTrue((run_dir / "views" / "front_static" / "depth.exr").exists())
            self.assertTrue((run_dir / "views" / "front_static" / "segmentation.png").exists())
            self.assertTrue((run_dir / "views" / "front_static" / "meta.json").exists())
            self.assertTrue((run_dir / "ue_output" / "runner_stdout.json").exists())
            self.assertTrue((run_dir / "ue_output" / "runner_stderr.json").exists())

    def test_ue_backend_propagates_runner_failure_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_runner = tmp_path / "fake_failing_ue_runner.py"
            write_fake_failing_ue_runner(fake_runner)
            runner_cmd = f"{sys.executable} {fake_runner} --run-dir {{run_dir}}"
            env = ue_env_with_fake_config(tmp, runner_cmd=runner_cmd)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_run_case.py"),
                    "cases/falling/falling_block_on_floor.json",
                    "--backend",
                    "ue",
                    "--output-root",
                    tmp,
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 2)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["failure_type"], "F_RGB_HIGHRES_VIEWPORT_UNAVAILABLE")
            self.assertEqual(summary["failure_category"], "runtime_failure")
            self.assertTrue(summary["real_ue_invoked"])

    def test_cli_accepts_views_and_render_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_run_case.py"),
                    "--case",
                    "cases/falling/falling_block_on_floor.json",
                    "--backend",
                    "fallback",
                    "--out",
                    tmp,
                    "--views",
                    "overview,front,side,top",
                    "--render-passes",
                    "rgb,depth,segmentation",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            run_dir = Path(summary["run_dir"])
            self.assertTrue((run_dir / "views" / "overview" / "rgb.mp4").exists())
            self.assertTrue((run_dir / "views" / "top" / "depth_placeholder.json").exists())
            readiness = json.loads((run_dir / "run_readiness.json").read_text(encoding="utf-8"))
            self.assertTrue(readiness["camera_plan_ready"])
            self.assertTrue(readiness["multi_view_ready"])
            self.assertFalse(readiness["depth_ready"])
            self.assertFalse(readiness["ue_render_real"])

    def test_batch_runner_passes_multiview_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_run_case_batch.py"),
                    "cases/falling",
                    "--backend",
                    "fallback",
                    "--output-root",
                    str(Path(tmp) / "runs"),
                    "--timestamp",
                    "unit",
                    "--views",
                    "overview,front,side,top",
                    "--render-passes",
                    "rgb,depth,segmentation",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            verify = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_verify_batch.py"),
                    "--runs",
                    summary["output_root"],
                    "--require-multiview",
                    "--require-depth",
                    "--min-view-count",
                    "4",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(verify.returncode, 1, verify.stderr)
            render_report = Path(summary["output_root"]) / "batch_render_report.json"
            self.assertTrue(render_report.exists())


if __name__ == "__main__":
    unittest.main()
