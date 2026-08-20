from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.core.case_spec import load_case_spec, validate_case_spec
from harness.runtime.genesis_sph_backend import genesis_command, genesis_parameters
from harness.runtime.rigid_sph_scene import (
    RigidSphCapabilityMissing,
    add,
    compile_rigid_sph_scene,
    matrix_vector,
    point_inside_profile,
    rotation_matrix_xyz,
    ue_rotation_pyr_from_solver_xyz,
)
from harness.runtime.rigid_sph_configuration import compile_rigid_sph_solver_configuration
from scripts.harness_genesis_rigid_sph import (
    evaluate_measurements,
    main as rigid_sph_main,
    rigid_body_pose_at_time,
    rigid_sph_simulation_settings,
    set_dynamic_body_initial_state,
    set_rigid_body_pose,
)


ROOT = Path(__file__).resolve().parents[1]
TRANSFER_CASE = ROOT / "cases/fluid/container_to_container_transfer/v002_wine_glass_to_teacup.json"
COFFEE_CASE = ROOT / "cases/fluid/container_to_surface_spill/v001_coffee_mug_table_spill.json"


class RigidSPHSceneTests(unittest.TestCase):
    def test_scene_duration_controls_full_physical_frame_window(self) -> None:
        settings = rigid_sph_simulation_settings(
            {
                "scene": {"duration_s": 4.0},
                "backend_options": {"duration_s": 2.0, "fps": 24, "steps_per_frame": 10},
            }
        )

        self.assertEqual(settings["duration_s"], 4.0)
        self.assertEqual(settings["frame_count"], 96)
        self.assertEqual(settings["output_frame_count"], 97)
        with self.assertRaisesRegex(ValueError, "scene.duration_s"):
            rigid_sph_simulation_settings({"backend_options": {"duration_s": 2.0}})

    def test_runner_exit_code_does_not_encode_physics_assertion_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "run"
            case = SimpleNamespace(data={})
            report = {"status": "fail", "checks": {"declared_assertions_passed": False}}
            with patch.object(sys, "argv", ["harness_genesis_rigid_sph.py", "--case", "case.json", "--output-dir", str(output_dir)]), patch(
                "scripts.harness_genesis_rigid_sph.load_runtime_case", return_value=case
            ), patch(
                "scripts.harness_genesis_rigid_sph.workspace_path", return_value=output_dir
            ), patch(
                "scripts.harness_genesis_rigid_sph.compile_rigid_sph_solver_configuration",
                return_value=compile_rigid_sph_solver_configuration({"scene": {"duration_s": 1.0}}),
            ), patch(
                "scripts.harness_genesis_rigid_sph.compile_rigid_sph_scene", return_value={"schema_version": "test"}
            ), patch(
                "scripts.harness_genesis_rigid_sph.simulate_rigid_sph_scene", return_value={}
            ), patch(
                "scripts.harness_genesis_rigid_sph.write_fluid_cache", return_value=report
            ), patch(
                "builtins.print"
            ):
                returncode = rigid_sph_main()

        self.assertEqual(returncode, 0)

    def test_dynamic_body_uses_qualified_asset_collision_and_structured_initial_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "irregular.obj"
            source.write_text("v 0 0 0\nv 0.1 0 0\nv 0 0.2 0\nf 1 2 3\n", encoding="utf-8")
            case = dynamic_asset_case(source)

            compiled = compile_rigid_sph_scene(case)
            body = next(item for item in compiled["rigid_bodies"] if item["id"] == "irregular_body")

            self.assertEqual(body["mobility"], "dynamic")
            self.assertEqual(body["mass_kg"], 0.42)
            self.assertEqual(body["initial_linear_velocity_m_s"], [0.0, 0.0, -0.3])
            self.assertEqual(body["collision"]["type"], "asset")
            self.assertEqual(
                body["collision"]["backend_conversion"],
                "catalog_portable_collision_mesh_v1_to_genesis_mesh",
            )
            self.assertTrue(body["collision"]["asset_geometry_match"])

            case["objects"][1]["asset"]["collision"]["present"] = False
            with self.assertRaisesRegex(RigidSphCapabilityMissing, "not qualified"):
                compile_rigid_sph_scene(case)

            case = dynamic_asset_case(source)
            case["objects"][1]["asset"]["collision"]["portable_mesh"]["sha256"] = "0" * 64
            with self.assertRaisesRegex(RigidSphCapabilityMissing, "identity"):
                compile_rigid_sph_scene(case)

    def test_dynamic_body_initial_state_is_held_for_preroll_then_released(self) -> None:
        calls: list[tuple[str, tuple[float, ...]]] = []

        class Entity:
            def set_pos(self, value, **_kwargs) -> None:
                calls.append(("position", tuple(value)))

            def set_quat(self, value, **_kwargs) -> None:
                calls.append(("rotation", tuple(value)))

            def set_dofs_velocity(self, value, **_kwargs) -> None:
                calls.append(("velocity", tuple(value)))

        body = {
            "transform": {"position_m": [0.1, 0.2, 0.3], "euler_xyz_deg": [0.0, 0.0, 0.0]},
            "initial_linear_velocity_m_s": [1.0, 2.0, 3.0],
            "initial_angular_velocity_rad_s": [0.1, 0.2, 0.3],
        }
        entity = Entity()

        set_dynamic_body_initial_state(entity, body, hold=True)
        self.assertEqual(calls[-1][1], (0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
        set_dynamic_body_initial_state(entity, body, hold=False)
        self.assertEqual(calls[-1][1], (1.0, 2.0, 3.0, 0.1, 0.2, 0.3))

    def test_rigid_body_state_measurement_projects_structured_solver_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "irregular.obj"
            source.write_text("v 0 0 0\nv 0.1 0 0\nv 0 0.2 0\nf 1 2 3\n", encoding="utf-8")
            case = dynamic_asset_case(source)
            case["solver_scene"]["measurements"].append(
                {
                    "id": "body_speed",
                    "type": "rigid_body_state",
                    "body_id": "irregular_body",
                    "field": "linear_velocity_m_s",
                    "component": "magnitude",
                }
            )
            compiled = compile_rigid_sph_scene(case)

        definition = next(row for row in compiled["measurements"] if row["id"] == "body_speed")
        measured = evaluate_measurements(
            [[0.0, 0.0, 0.0]],
            {"irregular_body": compiled["rigid_bodies"][1]},
            {
                "irregular_body": {
                    "position_m": [0.0, 0.0, 0.5],
                    "linear_velocity_m_s": [3.0, 4.0, 0.0],
                    "angular_velocity_rad_s": [0.0, 1.0, 0.0],
                }
            },
            [definition],
        )
        self.assertEqual(measured["body_speed"], 5.0)

    def test_different_scenarios_compile_to_one_execution_contract(self) -> None:
        transfer = compile_rigid_sph_scene(load_case_spec(TRANSFER_CASE).data)
        coffee = compile_rigid_sph_scene(load_case_spec(COFFEE_CASE).data)

        self.assertEqual(transfer["execution_contract"], "rigid_sph_scene")
        self.assertEqual(coffee["execution_contract"], "rigid_sph_scene")
        self.assertEqual(len(transfer["rigid_bodies"]), 3)
        self.assertEqual(len(coffee["rigid_bodies"]), 2)
        self.assertNotIn("solver_mode", transfer)
        self.assertNotIn("solver_mode", coffee)

    def test_declared_settled_initialization_is_compiled(self) -> None:
        case = copy.deepcopy(load_case_spec(COFFEE_CASE).data)
        case["solver_scene"]["initialization"] = {
            "state": "settled",
            "pre_roll_s": 0.25,
            "capture_after_pre_roll": True,
        }

        compiled = compile_rigid_sph_scene(case)

        self.assertEqual(compiled["initialization"]["state"], "settled")
        self.assertEqual(compiled["initialization"]["pre_roll_s"], 0.25)
        self.assertTrue(compiled["initialization"]["capture_after_pre_roll"])

    def test_runtime_layers_do_not_dispatch_on_scenario_names(self) -> None:
        paths = [
            ROOT / "harness/runtime/genesis_sph_backend.py",
            ROOT / "harness/verification/particle_cache_verifier.py",
            ROOT / "scripts/harness_genesis_rigid_sph.py",
            ROOT / "scripts/harness_render_fluid_ue.py",
        ]
        source = "\n".join(path.read_text(encoding="utf-8") for path in paths)

        self.assertNotIn("container_transfer", source)
        self.assertNotIn("container_surface_spill", source)
        self.assertNotIn("transfer_mode", source)
        backend_source = paths[0].read_text(encoding="utf-8")
        verifier_source = paths[1].read_text(encoding="utf-8")
        self.assertNotIn("harness_genesis_fluid.py", backend_source)
        self.assertNotIn("rigid_spheres", backend_source)
        self.assertNotIn("minimum_float_sink_separation", verifier_source)
        self.assertNotIn("verify_rigid_fluid_response", verifier_source)

    def test_backend_selects_contract_executor_without_large_cli_payload(self) -> None:
        case = load_case_spec(TRANSFER_CASE)
        parameters = genesis_parameters(case.data)
        command = genesis_command(Path("/isolated/python"), Path("/runs/scene"), parameters)

        self.assertIn("harness_genesis_rigid_sph.py", command[1])
        self.assertEqual(command[command.index("--case") + 1], "/runs/scene/case_spec.json")
        self.assertEqual(command[-1], "--skip-publish")

    def test_render_and_collision_bind_to_same_real_assets(self) -> None:
        compiled = compile_rigid_sph_scene(load_case_spec(TRANSFER_CASE).data)
        bodies = {body["id"]: body for body in compiled["rigid_bodies"]}

        self.assertEqual(bodies["wine_glass"]["asset"]["ue_path"], "/Game/Props/Dining/SM_Glass04.SM_Glass04")
        self.assertEqual(bodies["teacup"]["asset"]["ue_path"], "/Game/Props/Dining/SM_TeaCup.SM_TeaCup")
        self.assertFalse(bodies["wine_glass"]["asset"]["proxy"])
        self.assertTrue(bodies["wine_glass"]["collision"]["asset_geometry_match"])
        self.assertEqual(len(bodies["wine_glass"]["collision"]["parts"]), 73)
        self.assertEqual(len(bodies["teacup"]["collision"]["parts"]), 49)
        self.assertTrue(point_inside_profile(compiled["fluid"]["world_position_m"], bodies["wine_glass"]))
        self.assertFalse(point_inside_profile(compiled["fluid"]["world_position_m"], bodies["teacup"]))

    def test_invalid_asset_or_collision_is_rejected_before_solver(self) -> None:
        case = copy.deepcopy(load_case_spec(TRANSFER_CASE).data)
        body = next(item for item in case["objects"] if item["id"] == "wine_glass")
        body["asset"]["proxy"] = True
        body["solver"]["collision"]["type"] = "convex_hull"

        with self.assertRaisesRegex(ValueError, "non-proxy"):
            validate_case_spec(case)

    def test_plane_collision_must_match_visible_asset(self) -> None:
        case = copy.deepcopy(load_case_spec(COFFEE_CASE).data)
        tabletop = next(item for item in case["objects"] if item["id"] == "tabletop")
        tabletop["solver"]["collision"]["asset_geometry_match"] = False

        with self.assertRaisesRegex(ValueError, "explicitly fitted"):
            validate_case_spec(case)

    def test_unified_solver_declarations_compile_to_valid_static_layout(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.verification.static_scene_verifier import verify_static_scene_layout

        case = load_case_spec(COFFEE_CASE).data
        layout = build_static_scene_layout(case)
        nodes = {node["object_id"]: node for node in layout["object_nodes"]}

        self.assertEqual(layout["overlap_pairs"], [])
        self.assertEqual(
            layout["containment_relations"],
            [
                {
                    "object_id": "coffee",
                    "container_id": "coffee_mug",
                    "relation": "initially_contained_by",
                    "source": "solver.initial_volume.frame",
                }
            ],
        )
        self.assertEqual(nodes["coffee"]["physics"]["state_kind"], "particle")
        self.assertEqual(nodes["coffee"]["transform"]["position_m"], [-0.22, 0.0, 0.055])
        self.assertEqual(nodes["coffee_mug"]["transform"]["position_m"], [-0.22, 0.0, 0.05])
        self.assertEqual(nodes["tabletop"]["bounds"]["extents_m"], [0.4, 0.4, 0.025])
        self.assertEqual(verify_static_scene_layout(case, layout)["status"], "pass")

    def test_kinematic_body_reports_boundary_velocity_to_solver(self) -> None:
        compiled = compile_rigid_sph_scene(load_case_spec(TRANSFER_CASE).data)
        body = next(item for item in compiled["rigid_bodies"] if item["mobility"] == "kinematic")

        class Entity:
            velocity = None

            def set_pos(self, *_args, **_kwargs) -> None:
                pass

            def set_quat(self, *_args, **_kwargs) -> None:
                pass

            def set_dofs_velocity(self, velocity, **_kwargs) -> None:
                self.velocity = velocity

        entity = Entity()
        set_rigid_body_pose(entity, body, 0.3, next_time_s=0.31)

        self.assertIsNotNone(entity.velocity)
        self.assertGreater(max(abs(value) for value in entity.velocity[:3]), 0.0)
        self.assertGreater(max(abs(value) for value in entity.velocity[3:]), 0.0)

    def test_pivot_rotation_keeps_declared_pivot_fixed(self) -> None:
        compiled = compile_rigid_sph_scene(load_case_spec(TRANSFER_CASE).data)
        body = next(item for item in compiled["rigid_bodies"] if item["mobility"] == "kinematic")

        position, rotation, _ue_rotation = rigid_body_pose_at_time(body, 1.0)
        pivot_world = add(position, matrix_vector(rotation_matrix_xyz(rotation), body["motion"]["pivot_local_m"]))

        for actual, expected in zip(pivot_world, body["motion"]["pivot_world_m"], strict=True):
            self.assertAlmostEqual(actual, expected)

    def test_solver_rotation_is_mapped_to_equivalent_ue_rotator(self) -> None:
        self.assertEqual(ue_rotation_pyr_from_solver_xyz([10.0, 20.0, 30.0]), [-20.0, -30.0, 10.0])

        case = copy.deepcopy(load_case_spec(COFFEE_CASE).data)
        mug = next(item for item in case["objects"] if item["id"] == "coffee_mug")
        mug["solver"]["motion"]["ue_end_rotation_pyr_deg"] = [110.0, 0.0, 0.0]

        with self.assertRaisesRegex(ValueError, "UE rotation must equal"):
            validate_case_spec(case)

    def test_profile_fit_evidence_and_initial_fluid_clearance_are_required(self) -> None:
        missing_fit = copy.deepcopy(load_case_spec(COFFEE_CASE).data)
        mug = next(item for item in missing_fit["objects"] if item["id"] == "coffee_mug")
        mug["solver"]["collision"]["fit_method"] = ""
        with self.assertRaisesRegex(ValueError, "non-empty fit_method"):
            validate_case_spec(missing_fit)

        no_clearance = copy.deepcopy(load_case_spec(COFFEE_CASE).data)
        fluid = next(item for item in no_clearance["objects"] if item["role"] == "fluid")
        fluid["solver"]["initial_volume"]["radius_m"] = 0.04
        with self.assertRaisesRegex(ValueError, "clear the container wall"):
            validate_case_spec(no_clearance)


def dynamic_asset_case(source: Path) -> dict:
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    asset = {
        "ue_path": "/Game/Generated/Asset.Asset",
        "sha256": "a" * 64,
        "proxy": False,
        "bbox_m": [0.1, 0.2, 0.1],
    }
    return {
        "solver_scene": {
            "type": "rigid_sph",
            "measurements": [{"id": "vertical_span", "type": "axis_span", "axes": ["z"]}],
            "assertions": [
                {
                    "id": "span",
                    "measurement_id": "vertical_span",
                    "reduction": "max",
                    "operator": ">=",
                    "value": 0.01,
                }
            ],
        },
        "workspace_bounds_m": {"min_m": [-1.0, -1.0, -0.1], "max_m": [1.0, 1.0, 1.0]},
        "objects": [
            {
                "id": "floor",
                "role": "rigid_body",
                "asset": dict(asset),
                "solver": {
                    "mobility": "static",
                    "transform": {
                        "position_m": [0.0, 0.0, -0.025],
                        "euler_xyz_deg": [0.0, 0.0, 0.0],
                        "ue_rotation_pyr_deg": [0.0, 0.0, 0.0],
                    },
                    "collision": {
                        "type": "plane",
                        "position_m": [0.0, 0.0, 0.0],
                        "normal": [0.0, 0.0, 1.0],
                        "asset_geometry_match": True,
                    },
                },
            },
            {
                "id": "irregular_body",
                "role": "rigid_body",
                "asset": {
                    **asset,
                    "collision": {
                        "present": True,
                        "kind": "simple_convex",
                        "portable_mesh": {
                            "schema_version": "harness_portable_collision_mesh_v1",
                            "role": "qualified_collision_mesh",
                            "local_path": str(source),
                            "sha256": source_sha256,
                            "byte_size": source.stat().st_size,
                            "materialized": True,
                            "format": "obj",
                            "coordinate_system": "asset_local_z_up_m",
                            "artifact_to_asset_transform": {
                                "matrix4x4": [
                                    [1.0, 0.0, 0.0, 0.0],
                                    [0.0, 1.0, 0.0, 0.0],
                                    [0.0, 0.0, 1.0, 0.0],
                                    [0.0, 0.0, 0.0, 1.0],
                                ]
                            },
                        },
                    },
                },
                "mass_kg": 0.42,
                "initial_velocity_m_s": [0.0, 0.0, -0.3],
                "initial_angular_velocity_rad_s": [0.0, 1.0, 0.0],
                "solver": {
                    "mobility": "dynamic",
                    "transform": {
                        "position_m": [0.0, 0.0, 0.5],
                        "euler_xyz_deg": [0.0, 0.0, 0.0],
                        "ue_rotation_pyr_deg": [0.0, 0.0, 0.0],
                    },
                    "collision": {"type": "asset"},
                },
            },
            {
                "id": "water",
                "role": "fluid",
                "solver": {
                    "material_model": "sph_liquid",
                    "initial_volume": {
                        "shape": "cylinder",
                        "frame": {"type": "world"},
                        "position_m": [0.0, 0.0, 0.15],
                        "radius_m": 0.25,
                        "height_m": 0.25,
                    },
                },
            },
        ],
    }


if __name__ == "__main__":
    unittest.main()
