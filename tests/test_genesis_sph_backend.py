from __future__ import annotations

import copy
import json
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
    write_genesis_artifacts,
)
from harness.runtime.genesis_headless import import_headless_genesis
from harness.runtime.stage_executor import execute_runtime_plan
from scripts.harness_genesis_fluid import apply_negative_mode, confine_surface_vertices, initial_velocity_rows, percentile, surface_component_metrics


ROOT = Path(__file__).resolve().parents[1]


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

    def test_reconstructed_surface_is_projected_to_container_interior(self) -> None:
        import numpy as np

        vertices = np.asarray([[-0.4, 0.0, -0.02], [0.0, 0.5, 0.1], [0.4, -0.5, 0.2]])
        confined = confine_surface_vertices(vertices, [0.0, 0.0], 0.0, 0.3, np)

        np.testing.assert_allclose(confined, [[-0.3, 0.0, 0.0], [0.0, 0.3, 0.1], [0.3, -0.3, 0.2]])

    def test_initial_velocity_fields_are_initialized_once_from_particle_positions(self) -> None:
        import numpy as np

        positions = np.asarray([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        swirl = initial_velocity_rows(
            positions,
            {"type": "swirl_z", "center_m": [0.0, 0.0, 0.0], "angular_speed_rad_s": 2.0, "maximum_speed_m_s": 10.0},
            np,
        )
        uniform = initial_velocity_rows(positions, {"type": "uniform", "velocity_m_s": [0.5, 0.0, -0.2]}, np)

        np.testing.assert_allclose(swirl, [[0.0, 2.0, 0.0], [-2.0, 0.0, 0.0]])
        np.testing.assert_allclose(uniform, [[0.5, 0.0, -0.2], [0.5, 0.0, -0.2]])

    def test_cylinder_pulse_compiles_geometry_and_initial_flow(self) -> None:
        case = copy.deepcopy(load_case_spec(ROOT / "cases/fluid/fluid_drop_in_basin.json").data)
        fluid = case["objects"][0]
        fluid["initial_condition"] = {
            "type": "bounded_volume",
            "shape": "cylinder",
            "radius_m": 0.08,
            "height_m": 0.3,
            "euler_deg": [0.0, 90.0, 0.0],
            "velocity_field": {"type": "uniform", "velocity_m_s": [0.8, 0.0, -0.2]},
        }
        parameters = genesis_parameters(case)
        command = genesis_command(Path("/isolated/python"), Path("/runs/pour"), case["backend_options"], parameters)

        self.assertEqual(parameters["liquid_shape"], "cylinder")
        self.assertEqual(parameters["initial_velocity_field"]["velocity_m_s"], [0.8, 0.0, -0.2])
        self.assertEqual(command[command.index("--liquid-height") + 1], "0.3")
        self.assertEqual(command[command.index("--liquid-euler") + 1 : command.index("--liquid-euler") + 4], ["0.0", "90.0", "0.0"])

    def test_splash_baseline_uses_robust_settled_surface_not_single_droplet(self) -> None:
        self.assertEqual(percentile([0.1] * 19 + [1.0], 0.95), 0.1)

    def test_drop_height_matrix_is_ofat_and_executable(self) -> None:
        paths = sorted((ROOT / "cases" / "fluid" / "fluid_drop_height_matrix").glob("*.json"))
        cases = [load_case_spec(path).data for path in paths]
        self.assertEqual([case["experiment"]["value_m"] for case in cases], [0.55, 0.65, 0.75])
        self.assertEqual([case["objects"][0]["initial_position_m"][2] for case in cases], [0.55, 0.65, 0.75])
        normalized = []
        for case in cases:
            clone = copy.deepcopy(case)
            clone["case_id"] = "fluid_drop_height"
            clone["prompt"] = "normalized"
            clone["experiment"]["condition_id"] = "normalized"
            clone["experiment"]["value_m"] = 0.65
            clone["experiment"]["comparison_role"] = "normalized"
            clone["expected_physics"]["causal_expectation"] = "normalized"
            clone["objects"][0]["initial_position_m"][2] = 0.65
            clone["notes"] = "normalized"
            normalized.append(clone)
        self.assertEqual(normalized[0], normalized[1])
        self.assertEqual(normalized[1], normalized[2])

    def test_case_parameters_compile_to_isolated_backend_command(self) -> None:
        case = copy.deepcopy(load_case_spec(ROOT / "cases/fluid/fluid_drop_in_basin.json").data)
        case["expected_physics"]["gravity_m_s2"] = [0.5, 0.0, -3.0]
        case["objects"][0].update({"initial_position_m": [0.1, -0.2, 0.8], "size_m": [0.2, 0.25, 0.3]})
        case["objects"][1].update({"initial_position_m": [1.0, 2.0, 0.1], "floor_z_m": 0.15, "wall_half_extent_m": 0.7})
        parameters = genesis_parameters(case)
        command = genesis_command(Path("/isolated/python"), Path("/runs/fluid"), case["backend_options"], parameters)

        self.assertEqual(command[0], "/isolated/python")
        self.assertIn("--skip-publish", command)
        self.assertEqual(command[command.index("--fps") + 1], "24")
        self.assertEqual(command[command.index("--particle-size") + 1], "0.025")
        self.assertEqual(command[command.index("--gravity") + 1 : command.index("--gravity") + 4], ["0.5", "0.0", "-3.0"])
        self.assertEqual(command[command.index("--liquid-position") + 1 : command.index("--liquid-position") + 4], ["0.1", "-0.2", "0.8"])
        self.assertEqual(command[command.index("--liquid-size") + 1 : command.index("--liquid-size") + 4], ["0.2", "0.25", "0.3"])
        self.assertEqual(command[command.index("--basin-center") + 1 : command.index("--basin-center") + 3], ["1.0", "2.0"])
        self.assertEqual(command[command.index("--basin-floor-z") + 1], "0.15")
        self.assertEqual(command[command.index("--basin-half-extent") + 1], "0.7")

    def test_prefilled_buoyancy_case_compiles_rigid_density_inputs(self) -> None:
        case = load_case_spec(ROOT / "cases/fluid/drop_in_liquid/rubber_and_lead_balls.json")
        parameters = genesis_parameters(case.data)
        command = genesis_command(Path("/isolated/python"), Path("/runs/buoyancy"), case.data["backend_options"], parameters)

        self.assertEqual(parameters["liquid_initial_condition_type"], "container_fill")
        self.assertEqual([item["density_kg_m3"] for item in parameters["rigid_spheres"]], [250.0, 11340.0])
        self.assertEqual([item["expected_response"] for item in parameters["rigid_spheres"]], ["float", "sink"])
        self.assertIn("--rigid-spheres-json", command)
        self.assertEqual(command[command.index("--pre-roll") + 1], "1.25")

    def test_completed_run_writes_unified_artifacts_but_not_reference_readiness(self) -> None:
        case = load_case_spec(ROOT / "cases/fluid/fluid_drop_in_basin.json")
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "python"
            executable.touch()

            def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                run_dir = Path(command[command.index("--output-dir") + 1])
                environment = kwargs["env"]
                self.assertEqual(environment["MPLBACKEND"], "Agg")
                self.assertEqual(environment["MPLCONFIGDIR"], str((run_dir / ".matplotlib").resolve()))
                write_valid_cache(run_dir)
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
        case = load_case_spec(ROOT / "cases/fluid/fluid_drop_in_basin.json")
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
                write_valid_cache(run_dir, falling=False)
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
        case = load_case_spec(ROOT / "cases/fluid/fluid_drop_in_basin.json")
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
            run_dir = Path(tmp) / "runs" / "fluid_drop_in_basin_genesis_sph"
            execute_result = read_json(run_dir / "stage_results" / "execute.json")

        self.assertEqual(execute_result["failure_code"], "genesis_sph_process_failed")
        self.assertEqual(execute_result["failure_class"], "execution_failed")

    def test_completed_cache_can_enter_existing_ue_replay_without_publishing_solver_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            write_valid_cache(run_dir)
            write_json(run_dir / "case_spec.json", load_case_spec(ROOT / "cases/fluid/fluid_drop_in_basin.json").data)
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

    def test_registered_negative_case_is_rejected_by_unified_verifier(self) -> None:
        capability = read_json(ROOT / "capabilities/fluid_particle_dynamics.json")
        relative = "cases/fluid/negative_no_gravity_response.json"
        self.assertIn(relative, capability["regression_cases"])
        case = load_case_spec(ROOT / relative)
        parameters = genesis_parameters(case.data)
        command = genesis_command(Path("/isolated/python"), Path("/runs/negative"), case.data["backend_options"], parameters)
        self.assertEqual(command[command.index("--negative-mode") + 1], "no_gravity_response")
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "negative_no_gravity_response_genesis_sph"
            run_dir.mkdir()
            write_json(run_dir / "case_spec.json", case.data)
            write_valid_cache(run_dir, falling=False)
            report = write_genesis_artifacts(case, run_dir)

            readiness = read_json(run_dir / "run_readiness.json")

        self.assertEqual(case.data["verifier_expectation"]["failure_type"], "gravity_direction_not_observed")
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "gravity_direction_not_observed")
        self.assertFalse(readiness["physics_ready"])
        self.assertEqual(readiness["publication_tier"], "rejected")

    def test_negative_mode_rewrites_runtime_cache_to_stable_verifier_failure(self) -> None:
        cache = {
            "frames": [
                {"positions_m": [[0.0, 0.0, 1.0]], "velocities_m_s": [[0.0, 0.0, 0.0]]},
                {"positions_m": [[0.0, 0.0, 0.5]], "velocities_m_s": [[0.0, 0.0, -2.0]]},
            ]
        }

        result = apply_negative_mode(cache, "no_gravity_response")

        self.assertEqual(result["frames"][1]["positions_m"], [[0.0, 0.0, 1.0]])
        self.assertEqual(result["frames"][1]["velocities_m_s"], [[0.0, 0.0, 0.0]])
        self.assertEqual(result["negative_fixture"]["expected_failure"], "gravity_direction_not_observed")

    def test_missing_environment_still_leaves_unified_diagnostics(self) -> None:
        case = load_case_spec(ROOT / "cases/fluid/fluid_drop_in_basin.json")
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "runs"
            with patch("harness.runtime.genesis_sph_backend.genesis_python", return_value=Path(tmp) / "missing-python"):
                with self.assertRaisesRegex(RuntimeError, "Genesis environment missing"):
                    GenesisSPHBackend().run_case(case, output_root)
            run_dir = output_root / "fluid_drop_in_basin_genesis_sph"
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


def write_valid_cache(run_dir: Path, *, falling: bool = True) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    surface = run_dir / "surface.obj"
    surface.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    final_z = 0.8 if falling else 1.0
    frame_surface = {"path": "surface.obj", "vertex_count": 3, "triangle_count": 1, "topology_consistent": True}
    cache = {
        "schema_version": "harness_particle_cache_v1",
        "solver": {"gravity_m_s2": [0.0, 0.0, -9.81]},
        "timebase": {"fps": 10, "output_dt_s": 0.1},
        "particles": {"count": 2, "stable_ids": [0, 1]},
        "environment": {
            "type": "five_plane_basin",
            "center_xy_m": [0.0, 0.0],
            "floor_z_m": 0.0,
            "wall_half_extent_m": 1.5,
            "penetration_tolerance_m": 0.01,
        },
        "frames": [
            {"frame": 0, "time_s": 0.0, "positions_m": [[0.0, 0.0, 1.0], [0.1, 0.0, 1.0]], "velocities_m_s": [[0, 0, 0], [0, 0, 0]], "surface": frame_surface},
            {"frame": 1, "time_s": 0.1, "positions_m": [[0.0, 0.0, final_z], [0.1, 0.0, final_z]], "velocities_m_s": [[0, 0, -1], [0, 0, -1]], "surface": frame_surface},
        ],
    }
    write_json(run_dir / "particle_cache.json", cache)
    (run_dir / "video.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")


if __name__ == "__main__":
    unittest.main()
