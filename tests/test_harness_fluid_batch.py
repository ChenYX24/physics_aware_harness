from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FluidBatchVerifierTests(unittest.TestCase):
    def test_fluid_local_preview_profile_remains_terminal_rgb(self) -> None:
        from scripts.harness_render_fluid_ue import fluid_execution_profile

        profile = fluid_execution_profile("local_preview", ["front_static", "event_closeup"])

        self.assertEqual(profile.name, "local_preview")
        self.assertEqual(profile.render_passes, ("rgb",))

    def test_rigid_replay_camera_bounds_come_from_declared_workspace(self) -> None:
        from scripts.harness_render_fluid_ue import replay_scene_bounds

        bounds = replay_scene_bounds(
            {"workspace_bounds_m": {"min_m": [-0.8, -0.4, -0.1], "max_m": [0.6, 0.4, 0.9]}},
            render_z_offset_m=-0.05,
        )

        for actual, expected in zip(bounds["center"], [-0.1, 0.0, 0.35], strict=True):
            self.assertAlmostEqual(actual, expected)
        for actual, expected in zip(bounds["extent"], [0.7, 0.4, 0.5], strict=True):
            self.assertAlmostEqual(actual, expected)
        with self.assertRaisesRegex(ValueError, "workspace_bounds_m"):
            replay_scene_bounds({}, render_z_offset_m=0.0)

    def test_fluid_capability_preserves_unified_solver_to_ue_contract(self) -> None:
        capability = json.loads((ROOT / "capabilities" / "fluid_particle_dynamics.json").read_text(encoding="utf-8"))

        self.assertIn("declared_measurements", capability["required_signals"])
        self.assertIn("surface_import_fingerprint", capability["required_signals"])
        self.assertIn("rigid_body_asset_scale_xyz", capability["required_signals"])
        self.assertTrue(any("rigid collider" in rule for rule in capability["physical_assumptions"]))
        self.assertTrue(any("generic time-series reductions" in rule for rule in capability["verifier_rules"]))
        self.assertIn("solver_ue_rotation_mapping_mismatch", capability["failure_taxonomy"])
        self.assertIn(
            "cases/fluid/container_to_container_transfer/v002_wine_glass_to_teacup.json",
            capability["smoke_cases"],
        )

    def test_genesis_native_renderer_reuses_contiguous_surface_cache(self) -> None:
        from scripts.harness_render_fluid_genesis import surface_frame_paths

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(2):
                (root / f"frame_{index:04d}.obj").write_text("v 0 0 0\nf 1 1 1\n", encoding="utf-8")
            cache = {
                "frames": [
                    {"frame": index, "surface": {"path": f"frame_{index:04d}.obj"}}
                    for index in range(2)
                ]
            }

            self.assertEqual(
                surface_frame_paths(cache, root),
                [(root / "frame_0000.obj").resolve(), (root / "frame_0001.obj").resolve()],
            )

    def test_ue_surface_import_cache_is_bound_to_mesh_content(self) -> None:
        from scripts.harness_render_fluid_ue import surface_import_fingerprint, ue_asset_package_file

        replay = {
            "state_truth_sha256": "a" * 64,
            "frames": [{"ue_asset_path": "/Game/Harness/Fluid/SM_0000", "sha256": "b" * 64}],
        }
        first = surface_import_fingerprint(replay)
        replay["frames"][0]["sha256"] = "c" * 64

        self.assertNotEqual(first, surface_import_fingerprint(replay))
        self.assertEqual(
            ue_asset_package_file(Path("/tmp/Project/Test.uproject"), "/Game/Harness/Fluid/SM_0000"),
            Path("/tmp/Project/Content/Harness/Fluid/SM_0000.uasset"),
        )
        with self.assertRaisesRegex(ValueError, "under /Game"):
            ue_asset_package_file(Path("/tmp/Project/Test.uproject"), "/Engine/BasicShapes/Cube")

        importer = (ROOT / "scripts" / "import_ue_fluid_surface_sequence.py").read_text(encoding="utf-8")
        self.assertIn("SystemLibrary.quit_editor()", importer)

        renderer = (ROOT / "scripts" / "harness_render_fluid_ue.py").read_text(encoding="utf-8")
        self.assertIn("run_ue_import_until_report", renderer)
        self.assertIn("process.terminate()", renderer)

    def test_ue_replay_uses_a_uv_independent_opaque_surface_material(self) -> None:
        from scripts.harness_render_fluid_ue import fluid_visual_parameters

        renderer = (ROOT / "scripts" / "harness_render_fluid_ue.py").read_text(encoding="utf-8")
        native = (ROOT / "scripts" / "native_ue_scene.py").read_text(encoding="utf-8")

        self.assertEqual(fluid_visual_parameters({"case_id": "water", "objects": []})["color_rgb"], [0.03, 0.30, 0.78])
        self.assertIn('"generated_material_name": fluid_visual["generated_material_name"]', renderer)
        self.assertIn('"fixed_material_color": True', renderer)
        self.assertIn('"two_sided_material": True', renderer)
        self.assertIn("quantize_native_instance_segmentation", renderer)
        self.assertIn("MaterialExpressionConstant3Vector if fixed_color", native)
        self.assertIn('existing.get_editor_property("two_sided")', native)

    def test_ue_replay_keeps_solver_rigid_states_and_deep_cutaway_basin(self) -> None:
        from scripts.harness_render_fluid_ue import basin_runtime_objects

        objects = basin_runtime_objects(
            {
                "environment": {
                    "center_xy_m": [0.0, 0.0],
                    "floor_z_m": 0.0,
                    "wall_half_extent_m": 0.38,
                    "initial_liquid_surface_z_m": 0.26,
                }
            },
            "/Engine/Floor",
            "/Engine/Wall",
        )
        by_id = {item["id"]: item for item in objects}
        self.assertGreater(by_id["basin_wall_north"]["scale"][2], 0.4)
        self.assertEqual(by_id["basin_wall_south"]["scale"][2], 0.08)
        self.assertAlmostEqual(
            by_id["basin_wall_north"]["initial_position_m"][1]
            - by_id["basin_wall_north"]["scale"][1] / 2,
            0.38,
        )
        self.assertAlmostEqual(
            by_id["basin_wall_west"]["initial_position_m"][0]
            + by_id["basin_wall_west"]["scale"][0] / 2,
            -0.38,
        )

        renderer = (ROOT / "scripts" / "harness_render_fluid_ue.py").read_text(encoding="utf-8")
        self.assertIn('"trajectory_source": "genesis_sph"', renderer)
        self.assertIn('"source": "genesis_rigid_sph_frame"', renderer)
        self.assertIn('"front_static" if value == "overview_static"', renderer)
        self.assertIn("requested UE fluid views did not compile exactly", renderer)

        real_basin = basin_runtime_objects(
            {
                "environment": {
                    "center_xy_m": [0.0, 0.0],
                    "initial_liquid_surface_z_m": 0.26,
                }
            },
            "/Engine/Floor",
            "/Engine/Wall",
            asset_path="/Game/Maps/MarketEnvironment/Mesh/SM_Wash.SM_Wash",
        )
        self.assertEqual(len(real_basin), 1)
        self.assertEqual(real_basin[0]["ue5_path"], "/Game/Maps/MarketEnvironment/Mesh/SM_Wash.SM_Wash")
        self.assertAlmostEqual(real_basin[0]["initial_position_m"][2] + 1.10 * 1.25, 0.26)

        shaped_basin = basin_runtime_objects(
            {"environment": {"initial_liquid_surface_z_m": 0.24}},
            "/Engine/Floor",
            "/Engine/Wall",
            asset_path="/Game/Maps/UrbanDowntown/Meshes/Planter_A.Planter_A",
            asset_scale=[1.35, 1.35, 0.35],
            pivot_to_rim_m=0.886,
            render_z_offset_m=-0.05,
        )
        self.assertEqual(shaped_basin[0]["scale"], [1.35, 1.35, 0.35])
        self.assertAlmostEqual(shaped_basin[0]["initial_position_m"][2], -0.1201)

    def test_particle_surface_run_uses_fluid_quality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            run = batch / "fluid_case_genesis_sph"
            run.mkdir()
            (run / "surface.obj").write_text("v 0 0 0\nf 1 1 1\n", encoding="utf-8")
            (run / "video.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
            (run / "particle_cache.json").write_text(json.dumps(particle_cache()), encoding="utf-8")
            (run / "genesis_sph_backend_report.json").write_text(json.dumps({"case_id": "fluid_case", "capability_id": "fluid_particle_dynamics"}), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "harness_verify_batch.py"), str(batch)],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["pass_count"], 1)
        self.assertEqual(summary["artifact_completeness"]["trajectory_empty"], 0)
        self.assertEqual(summary["cases"][0]["artifact_kind"], "particle_surface_cache")

    def test_rigid_sph_replays_declared_real_ue_bodies(self) -> None:
        from scripts.harness_render_fluid_ue import (
            rigid_body_asset_resolution_entries,
            rigid_body_runtime_objects,
            rigid_body_trajectory_objects,
            runtime_pose_registration_report,
        )

        bodies = [
            {
                "id": "moving_body",
                "mobility": "kinematic",
                "asset": {"ue_path": "/Game/Props/Dining/SM_Glass04.SM_Glass04", "sha256": "a" * 64},
                "transform": {"position_m": [-0.25, 0.0, 0.35], "ue_rotation_pyr_deg": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0]},
                "motion": {"type": "pivot_rotation"},
                "collision": {"type": "axisymmetric_profile", "asset_geometry_match": True},
            },
            {
                "id": "static_body",
                "mobility": "static",
                "asset": {"ue_path": "/Game/Props/Dining/SM_Glass01.SM_Glass01", "sha256": "b" * 64},
                "transform": {"position_m": [0.02, 0.0, -0.025], "ue_rotation_pyr_deg": [0.0, 0.0, 0.0], "scale": [2.0, 1.0, 0.5]},
                "motion": None,
                "collision": {"type": "plane", "asset_geometry_match": True},
            },
            {
                "id": "dynamic_body",
                "mobility": "dynamic",
                "asset": {"ue_path": "/Game/Generated/Irregular.Irregular", "sha256": "c" * 64},
                "transform": {"position_m": [0.0, 0.0, 0.5], "ue_rotation_pyr_deg": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0]},
                "motion": None,
                "collision": {"type": "asset", "asset_geometry_match": True},
            },
        ]
        cache = {"environment": {"rigid_bodies": bodies}}

        dynamic, static = rigid_body_runtime_objects(cache, render_z_offset_m=0.025)
        resolution = rigid_body_asset_resolution_entries(bodies)

        self.assertEqual(dynamic[0]["ue5_path"], bodies[0]["asset"]["ue_path"])
        self.assertEqual(dynamic[1]["ue5_path"], bodies[2]["asset"]["ue_path"])
        self.assertEqual(static[0]["ue5_path"], bodies[1]["asset"]["ue_path"])
        self.assertEqual(dynamic[0]["params"]["base_rotation_degrees"], [0.0, 0.0, 0.0])
        self.assertEqual(dynamic[0]["params"]["pose_anchor"], "bounds_center")
        self.assertEqual(static[0]["params"]["pose_anchor"], "bounds_center")
        self.assertEqual(static[0]["scale"], [2.0, 1.0, 0.5])
        self.assertEqual(static[0]["initial_position_m"], [0.02, 0.0, 0.0])
        self.assertTrue(dynamic[0]["params"]["asset_geometry_match"])
        self.assertEqual(resolution[0]["selected_asset"]["collision_representation"], "axisymmetric_profile")
        self.assertFalse(resolution[0]["selected_asset"]["proxy"])

        trajectory = rigid_body_trajectory_objects(
            {
                "rigid_objects": {
                    "moving_body": {
                        "position_m": [-0.25, 0.0, 0.35],
                        "ue_rotation_pyr_deg": [0.0, 0.0, 0.0],
                        "linear_velocity_m_s": [0.0, 0.0, 0.0],
                    },
                    "dynamic_body": {
                        "position_m": [0.0, 0.0, 0.42],
                        "ue_rotation_pyr_deg": [4.0, 5.0, 6.0],
                        "linear_velocity_m_s": [0.0, 0.0, -1.2],
                    },
                }
            },
            bodies,
            render_z_offset_m=0.025,
        )
        self.assertEqual(trajectory["dynamic_body"]["position"], [0.0, 0.0, 0.445])
        self.assertEqual(trajectory["dynamic_body"]["rotation_degrees"], [4.0, 5.0, 6.0])
        self.assertEqual(trajectory["dynamic_body"]["velocity"], [0.0, 0.0, -1.2])

        del bodies[0]["transform"]["ue_rotation_pyr_deg"]
        with self.assertRaisesRegex(ValueError, "missing an explicit UE transform"):
            rigid_body_runtime_objects(cache, render_z_offset_m=0.0)

        registration = runtime_pose_registration_report(
            {
                "runtime_pose_registrations": {
                    "moving_body": {"anchor": "bounds_center", "max_residual_cm": 0.001},
                    "static_body": {"anchor": "bounds_center", "max_residual_cm": 0.0},
                }
            },
            ["moving_body", "static_body"],
        )
        self.assertEqual(registration["status"], "pass")

        missing_registration = runtime_pose_registration_report({}, ["moving_body"])
        self.assertEqual(missing_registration["status"], "fail")
        self.assertEqual(missing_registration["failures"][0]["code"], "pose_registration_missing")


def particle_cache() -> dict:
    surface = {"path": "surface.obj", "vertex_count": 3, "triangle_count": 1, "topology_consistent": True}
    return {
        "schema_version": "harness_particle_cache_v1",
        "solver": {"gravity_m_s2": [0, 0, -9.81]},
        "particles": {"count": 2, "stable_ids": [0, 1]},
        "frames": [
            {"frame": 0, "time_s": 0.0, "positions_m": [[0, 0, 1], [0.1, 0, 1]], "velocities_m_s": [[0, 0, 0], [0, 0, 0]], "surface": surface},
            {"frame": 1, "time_s": 0.1, "positions_m": [[0, 0, 0.9], [0.1, 0, 0.9]], "velocities_m_s": [[0, 0, -1], [0, 0, -1]], "surface": surface},
        ],
    }


if __name__ == "__main__":
    unittest.main()
