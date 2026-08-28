from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.core.artifact_schema import read_json, write_json
from harness.core.case_spec import load_case_spec
from harness.runtime.genesis_sph_backend import (
    GenesisSPHBackend,
    GenesisSPHExecutionError,
    genesis_child_environment,
    genesis_command,
    genesis_parameters,
    genesis_python,
    run_ue_surface_replay,
)
from harness.runtime.genesis_headless import import_headless_genesis
from harness.runtime.stage_executor import execute_runtime_plan
from scripts.harness_genesis_fluid import surface_component_metrics


ROOT = Path(__file__).resolve().parents[1]
TRANSFER_CASE = ROOT / "cases/fluid/container_to_container_transfer/v002_wine_glass_to_teacup.json"


class GenesisSPHBackendTests(unittest.TestCase):
    def test_headless_import_blocks_tk_before_loading_genesis(self) -> None:
        prior_tkinter = sys.modules.get("tkinter", ...)
        fake_genesis = object()
        try:
            with patch("harness.runtime.genesis_headless.importlib.import_module") as import_module:
                import_module.side_effect = lambda name: (
                    self.assertIsNone(sys.modules["tkinter"]) or fake_genesis
                )
                self.assertIs(import_headless_genesis(), fake_genesis)
                import_module.assert_called_once_with("genesis")
        finally:
            if prior_tkinter is ...:
                sys.modules.pop("tkinter", None)
            else:
                sys.modules["tkinter"] = prior_tkinter

    def test_genesis_child_environment_is_headless_and_run_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"MPLBACKEND": "TkAgg", "MPLCONFIGDIR": "/unwritable/matplotlib"},
            clear=False,
        ):
            run_dir = Path(tmp) / "run"
            environment = genesis_child_environment(run_dir)
            self.assertEqual(environment["MPLBACKEND"], "Agg")
            self.assertEqual(environment["MPLCONFIGDIR"], str((run_dir / ".matplotlib").resolve()))
            self.assertTrue((run_dir / ".matplotlib").is_dir())
            self.assertEqual(os.environ["MPLBACKEND"], "TkAgg")
            self.assertEqual(os.environ["MPLCONFIGDIR"], "/unwritable/matplotlib")

    def test_surface_component_metric_detects_fragmentation(self) -> None:
        connected = surface_component_metrics([[0, 1, 2], [2, 1, 3]], 4)
        fragmented = surface_component_metrics([[0, 1, 2], [3, 4, 5]], 6)

        self.assertEqual(connected["connected_component_count"], 1)
        self.assertEqual(connected["largest_component_triangle_fraction"], 1.0)
        self.assertEqual(fragmented["connected_component_count"], 2)
        self.assertEqual(fragmented["largest_component_triangle_fraction"], 0.5)

    def test_legacy_fluid_case_is_rejected_without_rigid_sph_contract(self) -> None:
        case = load_case_spec(ROOT / "cases/fluid/fluid_drop_in_basin.json")

        with self.assertRaisesRegex(ValueError, "solver_scene.type must be rigid_sph"):
            genesis_parameters(case.data)

    def test_completed_run_writes_unified_artifacts_but_not_reference_readiness(self) -> None:
        case = load_case_spec(TRANSFER_CASE)
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "python"
            executable.touch()

            def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                run_dir = Path(command[command.index("--output-dir") + 1])
                environment = kwargs["env"]
                self.assertEqual(environment["MPLBACKEND"], "Agg")
                self.assertEqual(environment["MPLCONFIGDIR"], str((run_dir / ".matplotlib").resolve()))
                write_valid_cache(run_dir, case.data)
                return subprocess.CompletedProcess(command, 0, "genesis ok", "")

            with patch("harness.runtime.genesis_sph_backend.genesis_python", return_value=executable), patch(
                "harness.runtime.genesis_sph_backend.subprocess.run", side_effect=fake_run
            ):
                run_dir = GenesisSPHBackend().run_case(case, Path(tmp) / "runs")

            for relative in (
                "case_spec.json",
                "artifact_manifest.json",
                "harness_artifact.json",
                "harness_verifier.json",
                "run_readiness.json",
                "render_manifest.json",
                "render_pass_manifest.json",
                "genesis_sph_output/summary.json",
                "genesis_sph_output/run_readiness.json",
                "trajectory.json",
                "contact_events.json",
                "genesis_sph_output/trajectory.json",
                "genesis_sph_output/contact_events.json",
            ):
                self.assertTrue((run_dir / relative).is_file(), relative)
            verifier = read_json(run_dir / "harness_verifier.json")
            readiness = read_json(run_dir / "run_readiness.json")
            manifest = read_json(run_dir / "artifact_manifest.json")
            self.assertEqual(verifier["status"], "pass")
            self.assertTrue(readiness["physics_ready"])
            self.assertFalse(readiness["local_preview_ready"])
            self.assertFalse(readiness["visual_ready"])
            self.assertTrue(readiness["solver_preview_ready"])
            self.assertFalse(readiness["reference_ready"])
            self.assertFalse(readiness["ue_render_real"])
            self.assertEqual(manifest["artifacts"]["particle_cache"], "particle_cache.json")
            trajectory = read_json(run_dir / "trajectory.json")
            self.assertEqual(trajectory[0]["objects"]["water"]["particle_count"], 2)
            self.assertEqual(read_json(run_dir / "contact_events.json"), [])

    def test_physics_assertion_failure_keeps_execute_completed_and_verifier_failed(self) -> None:
        case = load_case_spec(TRANSFER_CASE)
        compilation = SimpleNamespace(
            selected_backend="genesis_sph",
            artifacts={
                "runtime_plan": {
                    "stages": [
                        {"id": "solve_render", "kind": "solve_render", "backend": "genesis_sph"}
                    ]
                }
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "python"
            executable.touch()

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                run_dir = Path(command[command.index("--output-dir") + 1])
                write_valid_cache(run_dir, case.data, assertions_pass=False)
                return subprocess.CompletedProcess(command, 0, "assertions failed", "")

            with patch("harness.runtime.genesis_sph_backend.genesis_python", return_value=executable), patch(
                "harness.runtime.genesis_sph_backend.subprocess.run", side_effect=fake_run
            ):
                run_dir = execute_runtime_plan(
                    case,
                    Path(tmp) / "runs",
                    compilation=compilation,
                    requested_views=None,
                    render_passes=None,
                    camera_strategy="bounds_auto_v1",
                    profile="smoke",
                    width=320,
                    height=180,
                    complete_sensor_contract=False,
                )

            execute_result = read_json(run_dir / "stage_results" / "execute.json")
            verifier_result = read_json(run_dir / "stage_results" / "verifier.json")
            backend_report = read_json(run_dir / "genesis_sph_backend_report.json")

        self.assertEqual(execute_result["status"], "completed")
        self.assertEqual(verifier_result["failure_class"], "verification_failed")
        self.assertIn("revise_case_spec", verifier_result["allowed_next_actions"])
        self.assertEqual(backend_report["status"], "completed")
        self.assertEqual(backend_report["verification_status"], "fail")

    def test_nonzero_process_exit_remains_execution_failure(self) -> None:
        case = load_case_spec(TRANSFER_CASE)
        compilation = SimpleNamespace(
            selected_backend="genesis_sph",
            artifacts={
                "runtime_plan": {
                    "stages": [
                        {"id": "solve_render", "kind": "solve_render", "backend": "genesis_sph"}
                    ]
                }
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "python"
            executable.touch()
            completed = subprocess.CompletedProcess([], 7, "", "solver crashed")
            with patch("harness.runtime.genesis_sph_backend.genesis_python", return_value=executable), patch(
                "harness.runtime.genesis_sph_backend.subprocess.run", return_value=completed
            ):
                with self.assertRaises(GenesisSPHExecutionError):
                    execute_runtime_plan(
                        case,
                        Path(tmp) / "runs",
                        compilation=compilation,
                        requested_views=None,
                        render_passes=None,
                        camera_strategy="bounds_auto_v1",
                        profile="smoke",
                        width=320,
                        height=180,
                        complete_sensor_contract=False,
                    )
            run_dir = Path(tmp) / "runs" / f"{case.case_id}_genesis_sph"
            execute_result = read_json(run_dir / "stage_results" / "execute.json")

        self.assertEqual(execute_result["failure_code"], "genesis_sph_process_failed")
        self.assertEqual(execute_result["failure_class"], "execution_failed")

    def test_completed_cache_can_enter_existing_ue_replay_without_publishing_solver_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            case = load_case_spec(TRANSFER_CASE)
            write_valid_cache(run_dir, case.data)
            cache = read_json(run_dir / "particle_cache.json")
            cache["environment"]["measurements"] = []
            cache["environment"]["assertions"] = []
            for frame in cache["frames"]:
                frame["measurements"] = {}
            write_json(run_dir / "particle_cache.json", cache)
            write_json(run_dir / "case_spec.json", case.data)
            write_json(
                run_dir / "observation_plan.json",
                {
                    "cameras": [
                        {"camera_id": "overview", "role": "overview"},
                        {"camera_id": "side_static", "role": "side_static"},
                    ],
                    "modalities": ["rgb"],
                },
            )
            write_json(
                run_dir / "camera_plan.json",
                {
                    "views": [
                        {"camera_id": "overview", "role": "overview"},
                        {"camera_id": "side_static", "role": "side_static"},
                    ]
                },
            )
            write_json(
                run_dir / "render_manifest.json",
                {"render_kind": "solver_surface_preview", "ue_render_real": False},
            )
            project = root / "SimulatorWorkspace.uproject"
            executable = root / "UnrealEditor-Cmd"
            project.write_text("{}", encoding="utf-8")
            executable.touch()
            completed = subprocess.CompletedProcess([], 0, '{"status":"completed"}', "")

            with patch.dict(
                os.environ,
                {
                    "SIM_STUDIO_UE_PROJECT": str(project),
                    "SIM_STUDIO_UE_EXECUTABLE": str(executable),
                    "SIM_STUDIO_UE_MAP": "/Game/Maps/Test.Test",
                },
                clear=False,
            ), patch("harness.runtime.genesis_sph_backend.subprocess.run", return_value=completed) as runner:
                report = run_ue_surface_replay(
                    run_dir,
                    handoff_contract={"numeric_tolerances": {"spatial_measurement_absolute": 1e-6}},
                    profile="smoke",
                    width=1280,
                    height=720,
                )

            command = runner.call_args.args[0]
            self.assertEqual(report["status"], "completed")
            self.assertTrue((run_dir / "solver_preview.mp4").is_file())
            self.assertFalse((run_dir / "video.mp4").exists())
            self.assertIn("harness_render_fluid_ue.py", command[1])
            self.assertNotIn("--views", command)
            self.assertEqual(
                [row["camera_id"] for row in read_json(run_dir / "observation_plan.json")["cameras"]],
                ["overview", "side_static"],
            )
            self.assertTrue((run_dir / "ue_replay_input" / "fluid_surface_replay.json").is_file())

    def test_ue_replay_verification_failure_is_left_to_verifier_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            case = load_case_spec(TRANSFER_CASE)
            write_valid_cache(run_dir, case.data)
            cache = read_json(run_dir / "particle_cache.json")
            cache["environment"]["measurements"] = []
            cache["environment"]["assertions"] = []
            for frame in cache["frames"]:
                frame["measurements"] = {}
            write_json(run_dir / "particle_cache.json", cache)
            write_json(run_dir / "case_spec.json", case.data)
            write_json(
                run_dir / "fluid_ue_render_report.json",
                {"status": "failed_verification", "physics_verifier_status": "fail"},
            )
            project = root / "SimulatorWorkspace.uproject"
            executable = root / "UnrealEditor-Cmd"
            project.write_text("{}", encoding="utf-8")
            executable.touch()
            completed = subprocess.CompletedProcess([], 2, '{"status":"failed_verification"}', "")

            with patch.dict(
                os.environ,
                {
                    "SIM_STUDIO_UE_PROJECT": str(project),
                    "SIM_STUDIO_UE_EXECUTABLE": str(executable),
                },
                clear=False,
            ), patch("harness.runtime.genesis_sph_backend.subprocess.run", return_value=completed):
                report = run_ue_surface_replay(
                    run_dir,
                    handoff_contract={"numeric_tolerances": {"spatial_measurement_absolute": 1e-6}},
                    profile="smoke",
                )

            self.assertEqual(report["status"], "failed_verification")
            self.assertEqual(report["returncode"], 2)

    def test_missing_environment_still_leaves_unified_diagnostics(self) -> None:
        case = load_case_spec(TRANSFER_CASE)
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "runs"
            with patch("harness.runtime.genesis_sph_backend.genesis_python", return_value=Path(tmp) / "missing-python"):
                with self.assertRaisesRegex(RuntimeError, "Genesis environment missing"):
                    GenesisSPHBackend().run_case(case, output_root)
            run_dir = output_root / f"{case.case_id}_genesis_sph"
            report = read_json(run_dir / "genesis_sph_backend_report.json")
            readiness = read_json(run_dir / "run_readiness.json")
            summary = read_json(run_dir / "genesis_sph_output/summary.json")
            manifest_exists = (run_dir / "artifact_manifest.json").is_file()

        self.assertEqual(report["status"], "failed_unavailable")
        self.assertEqual(summary["status"], "failed_unavailable")
        self.assertFalse(readiness["physics_ready"])
        self.assertTrue(manifest_exists)

    def test_default_environment_is_always_outside_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SIM_HARNESS_WORKSPACE": tmp}, clear=False):
            self.assertEqual(genesis_python(), Path(tmp).resolve() / "envs" / "genesis" / "bin" / "python")


def write_valid_cache(run_dir: Path, case_spec: dict, *, assertions_pass: bool = True) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    surface = run_dir / "surface.obj"
    surface.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    contract = genesis_parameters(case_spec)
    frame_surface = {
        "path": "surface.obj",
        "vertex_count": 3,
        "triangle_count": 1,
        "topology_consistent": True,
        "bounds_m": {"min_m": [-0.1, -0.1, 0.1], "max_m": [0.1, 0.1, 0.3]},
        "rigid_intersection_vertex_count": 0,
    }
    glass_values = [1.0, 0.99, 0.83, 0.67, 0.51, 0.35, 0.18, 0.0] if assertions_pass else [1.0] * 8
    cup_values = [0.0, 0.0, 0.1, 0.25, 0.4, 0.6, 0.8, 0.95] if assertions_pass else [0.0] * 8
    frames = []
    for index, (glass, cup) in enumerate(zip(glass_values, cup_values, strict=True)):
        values = {
            "glass_occupancy": glass,
            "cup_occupancy": cup,
            "outside_vessels": max(0.0, 1.0 - glass - cup),
        }
        frames.append(
            {
                "frame": index,
                "time_s": index / 10.0,
                "positions_m": [[0.0, 0.0, 0.2], [0.05, 0.0, 0.2]],
                "velocities_m_s": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
                "measurements": values,
                "surface": frame_surface,
            }
        )
    cache = {
        "schema_version": "harness_particle_cache_v1",
        "solver": {"gravity_m_s2": [0.0, 0.0, -9.81]},
        "timebase": {"fps": 10, "output_dt_s": 0.1},
        "particles": {"count": 2, "stable_ids": [0, 1]},
        "environment": {
            "type": "rigid_sph_scene",
            "workspace_bounds_m": contract["workspace_bounds_m"],
            "penetration_tolerance_m": 0.01,
            "measurements": contract["measurements"],
            "assertions": contract["assertions"],
            "surface_container_intersection_metric": "not_applied_for_boundary_contacting_fluid",
        },
        "frames": frames,
    }
    write_json(run_dir / "particle_cache.json", cache)
    (run_dir / "video.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")


if __name__ == "__main__":
    unittest.main()
