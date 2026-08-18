from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class RuntimeActorPlacementTests(unittest.TestCase):
    def load_case(self, relative_path: str) -> dict:
        return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))

    def test_compiled_actor_placement_is_not_refit_without_explicit_opt_in(self) -> None:
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement

        binding = {
            "object_id": "falling_block",
            "runtime_actor_id": "actor_falling_block",
            "role": "falling_body",
            "asset": {"ue_path": "/Engine/BasicShapes/Cube.Cube", "proxy": True},
            "transform": {"position_m": [0.0, 0.0, 1.2]},
            "bounds": {"extents_m": [0.25, 0.25, 0.25]},
            "physics": {
                "simulate_physics": True,
                "kinematic": False,
                "collider": "box",
                "enable_gravity": True,
            },
        }
        placement = {"actor_bindings": [binding]}

        dynamic, _ = runtime_objects_from_actor_placement(
            placement,
            {"objects": [{"id": "falling_block", "initial_position_m": [0.0, 0.0, 1.2]}]},
        )
        self.assertEqual(dynamic[0]["initial_position_m"], [0.0, 0.0, 1.2])
        self.assertIs(dynamic[0]["params"]["fit_dynamic_plan"], False)

        opted_in, _ = runtime_objects_from_actor_placement(
            placement,
            {
                "objects": [
                    {
                        "id": "falling_block",
                        "initial_position_m": [0.0, 0.0, 1.2],
                        "fit_dynamic_plan": True,
                    }
                ]
            },
        )
        self.assertIs(opted_in[0]["params"]["fit_dynamic_plan"], True)

    def test_native_ue_snapshots_initial_pose_before_enabling_physics(self) -> None:
        import ast

        source = ROOT / "scripts" / "native_ue_scene.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        setup_scene = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "setup_scene"
        )
        snapshot_lines = sorted(
            node.lineno
            for node in ast.walk(setup_scene)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "record_runtime_initial_transform"
        )
        physics_lines = sorted(
            node.lineno
            for node in ast.walk(setup_scene)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "configure_runtime_physics"
        )

        self.assertEqual(len(snapshot_lines), 2)
        self.assertEqual(len(physics_lines), 2)
        self.assertTrue(all(snapshot < physics for snapshot, physics in zip(snapshot_lines, physics_lines, strict=True)))

    def test_fracture_energy_matrix_changes_only_speed_and_expected_outcome(self) -> None:
        matrix_dir = ROOT / "cases" / "fracture" / "steel_ball_board_energy_matrix"
        cases = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(matrix_dir.glob("*.json"))]

        self.assertEqual(len(cases), 3)
        self.assertEqual(
            [case["expected_physics"]["nominal_incident_energy_j"] for case in cases],
            [2.0, 8.0, 18.0],
        )
        self.assertEqual(
            [case["expected_physics"]["expected_fracture"] for case in cases],
            [False, False, True],
        )
        for case, expected_speed in zip(cases, (1.0, 2.0, 3.0), strict=True):
            striker, panel, _floor = case["objects"]
            self.assertEqual(striker["role"], "projectile")
            self.assertEqual(striker["mass_kg"], 4.0)
            self.assertEqual(striker["initial_position_m"], [0.0, -1.4, 0.1])
            self.assertEqual(striker["initial_velocity_m_s"], [0.0, expected_speed, 0.0])
            self.assertTrue(case["physical_parameters"]["gravity_enabled"])
            self.assertEqual(case["physical_parameters"]["gravity_m_s2"], [0.0, 0.0, -9.81])
            self.assertFalse(striker["enable_gravity"])
            self.assertTrue(striker["use_ccd"])
            self.assertEqual(panel["fracture_response"]["minimum_impact_energy_j"], 10.0)
            self.assertEqual(_floor["collision_profile"], "BlockAll")

    def test_glass_energy_response_matrix_uses_one_curve_and_changes_only_speed(self) -> None:
        from harness.core.case_spec import fracture_response_for_energy, validate_case_spec

        matrix_dir = ROOT / "cases" / "fracture" / "glass_energy_response_matrix"
        cases = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(matrix_dir.glob("*.json"))]

        self.assertEqual(len(cases), 3)
        responses = []
        expected_y = (-0.33, -0.58, -0.83)
        for case, speed, state, launch_y in zip(
            cases,
            (1.0, 2.0, 3.0),
            ("cracked", "shattered", "burst"),
            expected_y,
            strict=True,
        ):
            validate_case_spec(case)
            ball, panel, _floor = case["objects"]
            self.assertTrue(ball["enable_gravity"])
            self.assertEqual(ball["initial_position_m"], [0.0, launch_y, 0.3434375])
            self.assertEqual(ball["initial_velocity_m_s"], [0.0, speed, 2.4525])
            self.assertEqual(case["physical_parameters"]["ballistic_time_to_impact_s"], 0.25)
            self.assertEqual(case["physical_parameters"]["target_impact_point_m"], [0.0, -0.08, 0.65])
            self.assertFalse(ball["fit_dynamic_plan"])
            self.assertFalse(panel["fit_dynamic_plan"])
            self.assertEqual(panel["asset_query"], "GC_GlassPanelRadial")
            self.assertEqual(panel["fracture_response"]["center_source"], "native_contact_impact_point")
            self.assertEqual(panel["fracture_response"]["prefracture_pattern"], "radial_voronoi")
            self.assertIn("MI_DestructibleGlass", panel["visual_material_path"])
            self.assertNotIn("GC_SM_board1", " ".join(case["required_assets"]))
            levels = panel["fracture_response"]["energy_response_levels"]
            burst = next(level for level in levels if level["damage_state"] == "burst")
            self.assertLessEqual(burst["radial_impulse_strength"], 250.0)
            selected = fracture_response_for_energy(panel["fracture_response"], case["physical_parameters"]["nominal_incident_energy_j"])
            self.assertEqual(selected["damage_state"], state)
            responses.append(panel["fracture_response"])
        self.assertEqual(responses[0], responses[1])
        self.assertEqual(responses[1], responses[2])

    def test_fracture_center_prefers_native_contact_point(self) -> None:
        from harness.core.case_spec import fracture_center_from_contact

        center, source = fracture_center_from_contact(
            {"impact_point_cm": [10.0, 20.0, 30.0]},
            [1.0, 2.0, 3.0],
        )

        self.assertEqual(center, [10.0, 20.0, 30.0])
        self.assertEqual(source, "native_contact_impact_point")

    def test_glass_position_matrix_changes_only_x_and_uses_matching_gc(self) -> None:
        from harness.core.case_spec import validate_case_spec

        root = ROOT / "cases" / "fracture" / "glass_impact_position_matrix"
        cases = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*.json"))]
        self.assertEqual(len(cases), 2)
        for case, expected_x, asset in zip(
            cases,
            (-0.45, 0.45),
            ("GC_GlassPanelRadialLeft45", "GC_GlassPanelRadialRight45"),
            strict=True,
        ):
            validate_case_spec(case)
            ball, panel, _floor = case["objects"]
            self.assertEqual(case["physical_parameters"]["target_impact_point_m"], [expected_x, -0.08, 0.65])
            self.assertEqual(ball["initial_position_m"], [expected_x, -0.58, 0.3434375])
            self.assertEqual(panel["asset_query"], asset)
            self.assertEqual(panel["fracture_response"]["planned_fracture_center_local_cm"][0], expected_x * 100)

    def test_case_can_disable_gravity_for_a_horizontal_impact_probe(self) -> None:
        from scripts.harness_local_ue_runner import default_physics_controls

        controls = default_physics_controls({"physical_parameters": {"gravity_enabled": False}})

        self.assertFalse(controls["gravity_enabled"])

    def test_unreal_editor_command_can_select_a_shared_server_gpu(self) -> None:
        from scripts.harness_local_ue_runner import unreal_editor_command

        with patch.dict("os.environ", {"SIM_STUDIO_UE_GRAPHICS_ADAPTER": "3"}, clear=True):
            command = unreal_editor_command(
                Path("/opt/UE/Engine/Binaries/Linux/UnrealEditor-Cmd"),
                Path("/workspace/SimulatorWorkspace.uproject"),
                Path("/repo/scripts/native_scene.py"),
            )

        self.assertIn("-graphicsadapter=3", command)
        with patch.dict("os.environ", {"SIM_STUDIO_UE_GRAPHICS_ADAPTER": "-1"}, clear=True):
            with self.assertRaisesRegex(ValueError, "non-negative integer"):
                unreal_editor_command(Path("/ue"), Path("/project"), Path("/script"))

    def test_bowling_case_compiles_runtime_actor_bindings(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement

        case = self.load_case("cases/bowling/bowling_pin_chain_contact.json")
        asset_resolution = resolve_asset_intents(case)
        scene_layout = build_static_scene_layout(case, asset_resolution=asset_resolution)

        placement = compile_runtime_actor_placement(case, scene_layout, asset_resolution=asset_resolution)
        report = verify_runtime_actor_placement(case, placement)

        self.assertEqual(placement["schema_version"], "harness_runtime_actor_placement_v1")
        self.assertEqual(report["status"], "pass")
        actor_ids = {binding["runtime_actor_id"] for binding in placement["actor_bindings"]}
        self.assertEqual(len(actor_ids), len(placement["actor_bindings"]))
        by_object = {binding["object_id"]: binding for binding in placement["actor_bindings"]}
        self.assertIn("bowling_ball", by_object)
        self.assertEqual(by_object["bowling_ball"]["runtime_actor_id"], "actor_bowling_ball")
        self.assertTrue(by_object["bowling_ball"]["physics"]["simulate_physics"])
        self.assertTrue(by_object["lane"]["physics"]["kinematic"])
        self.assertIn(by_object["pin_1"]["asset"]["binding_source"], {"ue_asset", "analytic_proxy"})
        self.assertTrue(by_object["pin_1"]["asset"]["ue_path"] or by_object["pin_1"]["asset"]["proxy"])
        self.assertEqual(by_object["bowling_ball"]["asset"]["quality_gate"]["status"], "pass")
        self.assertTrue(by_object["bowling_ball"]["asset"]["license"])
        self.assertIn(["bowling_ball", "pin_1"], placement["physics_graph"]["collision_edges"])
        self.assertTrue(placement["camera_bindings"])

    def test_magnetic_case_keeps_force_source_bound_but_not_simulated(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement

        case = self.load_case("cases/magnetic/attract_magnetic_body.json")
        asset_resolution = resolve_asset_intents(case)
        scene_layout = build_static_scene_layout(case, asset_resolution=asset_resolution)

        placement = compile_runtime_actor_placement(case, scene_layout, asset_resolution=asset_resolution)
        report = verify_runtime_actor_placement(case, placement)

        self.assertEqual(report["status"], "pass")
        by_object = {binding["object_id"]: binding for binding in placement["actor_bindings"]}
        self.assertFalse(by_object["magnet_source"]["physics"]["simulate_physics"])
        self.assertEqual(by_object["magnet_source"]["physics"]["collision_enabled"], False)
        self.assertTrue(by_object["steel_ball"]["physics"]["simulate_physics"])

    def test_collision_disabled_binding_reaches_runtime_scene(self) -> None:
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement

        binding = {
            "object_id": "visual_container",
            "runtime_actor_id": "actor_visual_container",
            "role": "visible container",
            "asset": {
                "ue_path": "/Game/Props/SM_Container.SM_Container",
                "runtime_usage": "collision_and_visual",
            },
            "bounds": {"extents_m": [0.5, 0.5, 0.5]},
            "transform": {"position_m": [0.0, 0.0, 0.5]},
            "physics": {
                "simulate_physics": False,
                "kinematic": True,
                "collision_enabled": False,
                "collider": "box",
            },
        }

        _, static = runtime_objects_from_actor_placement(
            {"actor_bindings": [binding]},
            {"objects": [{"id": "visual_container"}]},
        )

        self.assertFalse(static[0]["physics_properties"]["collision_enabled"])

    def test_verifier_rejects_physics_object_without_asset_or_proxy(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement

        case = self.load_case("cases/bowling/bowling_pin_chain_contact.json")
        asset_resolution = resolve_asset_intents(case)
        scene_layout = build_static_scene_layout(case, asset_resolution=asset_resolution)
        bad_layout = deepcopy(scene_layout)
        first = bad_layout["object_nodes"][0]
        first["asset_binding"]["selected_asset_ue_path"] = None
        first["asset_binding"]["fallback_reason"] = None
        first["physics"]["proxy"] = False

        placement = compile_runtime_actor_placement(case, bad_layout, asset_resolution=asset_resolution)
        report = verify_runtime_actor_placement(case, placement)

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F2_asset_missing")
        self.assertEqual(report["first_failure"]["metric"], "missing_asset_or_proxy_binding")

    def test_verifier_rejects_fracture_proxy_before_starting_ue(self) -> None:
        import tempfile
        from pathlib import Path

        from harness.assets.asset_registry import AssetRegistry
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement

        case = self.load_case("cases/fracture/glass_energy_response_matrix/glass_panel_e16_shatter.json")
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "empty_registry.json"
            registry_path.write_text('{"assets": []}', encoding="utf-8")
            asset_resolution = resolve_asset_intents(case, registry=AssetRegistry(registry_path))
        scene_layout = build_static_scene_layout(case, asset_resolution=asset_resolution)
        placement = compile_runtime_actor_placement(case, scene_layout, asset_resolution=asset_resolution)

        report = verify_runtime_actor_placement(case, placement)

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F2_asset_missing")
        self.assertEqual(report["first_failure"]["metric"], "fracture_asset_must_be_geometry_collection")

    def test_local_ue_runner_does_not_treat_blueprint_class_as_mesh_path(self) -> None:
        from scripts.harness_local_ue_runner import is_runtime_mesh_path, ue_path_for_binding

        binding = {
            "role": "support",
            "asset": {
                "ue_path": "/Script/Engine.Actor",
                "proxy": True,
                "binding_source": "ue_asset",
            },
            "physics": {
                "collider": "box",
            },
        }

        self.assertFalse(is_runtime_mesh_path("/Script/Engine.Actor"))
        self.assertEqual(ue_path_for_binding(binding), "/Engine/BasicShapes/Cube.Cube")

    def test_analytic_cylinder_and_generated_material_reach_local_ue_runtime(self) -> None:
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement, ue_path_for_binding

        binding = {
            "object_id": "magnet_source",
            "runtime_actor_id": "actor_magnet_source",
            "role": "magnetic_source",
            "asset": {"proxy": True},
            "transform": {"position_m": [0.0, 0.0, 0.08]},
            "bounds": {"extents_m": [0.09, 0.09, 0.08]},
            "physics": {"simulate_physics": False, "kinematic": True, "collider": "cylinder"},
        }
        case_object = {
            "id": "magnet_source",
            "generate_solid_material": True,
            "generated_material_name": "M_Harness_MagneticSource_Red",
            "color_rgb": [0.82, 0.02, 0.01],
            "roughness": 0.28,
            "metallic": 0.65,
            "fixed_material_color": True,
        }

        self.assertEqual(ue_path_for_binding(binding), "/Engine/BasicShapes/Cylinder.Cylinder")
        _, static = runtime_objects_from_actor_placement(
            {"actor_bindings": [binding]},
            {"objects": [case_object]},
        )
        self.assertEqual(static[0]["params"]["color_rgb"], [0.82, 0.02, 0.01])
        self.assertTrue(static[0]["params"]["generate_solid_material"])
        self.assertEqual(static[0]["params"]["metallic"], 0.65)

    def test_constraint_case_does_not_inject_tether_visuals(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from scripts.harness_local_ue_runner import prepare_rigid_simulation, runtime_objects_from_actor_placement

        case = self.load_case("cases/rigid_collision/newton_cradle/v001_release_angle_ofat/release_25deg.json")
        assets = resolve_asset_intents(case)
        layout = build_static_scene_layout(case, asset_resolution=assets)
        placement = compile_runtime_actor_placement(case, layout, asset_resolution=assets)
        dynamic, static = runtime_objects_from_actor_placement(placement, case)

        self.assertEqual(len([item for item in dynamic if item["id"].startswith("ball_")]), 5)
        self.assertTrue(all(item["params"]["fit_dynamic_plan"] is False for item in dynamic if item["id"].startswith("ball_")))
        self.assertEqual(len([item for item in static if item["behavior"] == "elastic_tether_visual"]), 0)
        with patch.dict(os.environ, {}, clear=False), patch(
            "scripts.harness_local_ue_runner.simulate_rigid_case",
            return_value=[{"frame": 0, "source": "mujoco_constraint_impulse"}],
        ) as simulate:
            os.environ.pop("SIM_STUDIO_UE_RIGID_MODE", None)
            trajectory = prepare_rigid_simulation(case, placement, fps=24, duration_s=5.0)
        self.assertEqual(trajectory[0]["source"], "mujoco_constraint_impulse")
        simulate.assert_called_once()

    def test_blueprint_asset_kind_reaches_local_ue_runtime(self) -> None:
        from harness.core.scene_layout import build_object_node
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement

        case_object = {
            "id": "glass_panel",
            "role": "brittle_fracture_body",
            "shape": "thin_box",
            "kinematic": True,
            "fracture_response": {"impactor_id": "striker", "external_strain": 750000.0},
            "initial_position_m": [0.0, 0.0, 0.45],
            "size_m": [0.1, 1.6, 0.9],
        }
        node = build_object_node(
            case_object,
            {
                "selected_asset": {
                    "id": "bp_board_destructible",
                    "type": "Blueprint",
                    "ue_path": "/Game/DD_Vehicles_Advanced/Blueprints/garage/BP_BoardDestructible.BP_BoardDestructible",
                    "collider": "actor",
                },
                "fallback_reason": None,
            },
        )
        placement = compile_runtime_actor_placement(
            {"case_id": "glass", "objects": [case_object]},
            {"case_id": "glass", "object_nodes": [node]},
        )
        _, static = runtime_objects_from_actor_placement(placement, {"objects": [case_object]})

        self.assertEqual(placement["actor_bindings"][0]["asset"]["asset_kind"], "Blueprint")
        self.assertEqual(
            placement["actor_bindings"][0]["ue_class"],
            "/Game/DD_Vehicles_Advanced/Blueprints/garage/BP_BoardDestructible.BP_BoardDestructible_C",
        )
        self.assertEqual(static[0]["asset_kind"], "static_mesh")
        self.assertEqual(static[0]["params"]["visual_asset_kind"], "blueprint")
        self.assertEqual(
            static[0]["params"]["visual_ue5_path"],
            "/Game/DD_Vehicles_Advanced/Blueprints/garage/BP_BoardDestructible.BP_BoardDestructible",
        )
        self.assertEqual(static[0]["params"]["fracture_response"]["impactor_id"], "striker")
        self.assertEqual(static[0]["class_name"], "StaticMesh")
        self.assertEqual(
            static[0]["ue5_path"],
            "/Engine/BasicShapes/Cube.Cube",
        )

    def test_geometry_collection_can_compile_an_intact_visual_shell(self) -> None:
        from harness.core.scene_layout import build_object_node
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement

        case_object = {
            "id": "glass_panel",
            "role": "brittle_fracture_body",
            "shape": "thin_box",
            "asset_query": "GC_GlassPanel",
            "intact_visual_ue_path": "/Engine/BasicShapes/Cube.Cube",
            "intact_visual_material_path": "/Game/Materials/M_Glass.M_Glass",
            "intact_visual_scale": [1.6, 0.12, 1.0],
            "initial_position_m": [0.0, 0.0, 0.5],
            "size_m": [1.6, 0.12, 1.0],
        }
        node = build_object_node(
            case_object,
            {
                "selected_asset": {
                    "id": "GC_GlassPanel",
                    "type": "geometry_collection",
                    "ue_path": "/Game/HarnessGenerated/Glass/GC_GlassPanel.GC_GlassPanel",
                    "collider": "geometry_collection",
                    "preserve_authored_scale": True,
                },
                "fallback_reason": None,
            },
        )
        placement = compile_runtime_actor_placement(
            {"case_id": "glass", "objects": [case_object]},
            {"case_id": "glass", "object_nodes": [node]},
        )
        dynamic, _ = runtime_objects_from_actor_placement(placement, {"objects": [case_object]})

        self.assertEqual(dynamic[0]["params"]["visual_ue5_path"], "/Engine/BasicShapes/Cube.Cube")
        self.assertEqual(dynamic[0]["params"]["visual_asset_kind"], "static_mesh")
        self.assertEqual(dynamic[0]["params"]["intact_visual_scale"], [1.6, 0.12, 1.0])
        self.assertEqual(dynamic[0]["params"]["intact_visual_material_path"], "/Game/Materials/M_Glass.M_Glass")

    def test_authored_scale_is_preserved_only_for_the_selected_runtime_mesh(self) -> None:
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement

        binding = {
            "object_id": "rod",
            "runtime_actor_id": "actor_rod",
            "role": "falling rigid cylinder",
            "asset": {
                "ue_path": "/Game/Generated/Rod.Rod",
                "runtime_usage": "collision_and_visual",
                "preserve_authored_scale": True,
            },
            "bounds": {"extents_m": [0.06, 0.06, 0.6]},
            "transform": {"position_m": [0.0, 0.0, 0.61], "rotation_deg": [0.0, 20.0, 0.0]},
            "physics": {"simulate_physics": True, "collider": "cylinder"},
        }
        dynamic, _ = runtime_objects_from_actor_placement(
            {"actor_bindings": [binding]},
            {"objects": [{"id": "rod"}]},
        )
        self.assertEqual(dynamic[0]["scale"], [1.0, 1.0, 1.0])
        self.assertTrue(dynamic[0]["params"]["preserve_authored_scale"])
        self.assertEqual(dynamic[0]["params"]["pose_anchor"], "bounds_center")
        self.assertEqual(dynamic[0]["params"]["desired_extent_cm"], 60.0)

        visual_binding = deepcopy(binding)
        visual_binding["asset"]["runtime_usage"] = "visual_proxy"
        visual_binding["bounds"]["extents_m"] = [0.15, 0.15, 0.15]
        visual_binding["physics"]["collider"] = "sphere"
        visual, _ = runtime_objects_from_actor_placement(
            {"actor_bindings": [visual_binding]},
            {"objects": [{"id": "rod"}]},
        )
        self.assertEqual(visual[0]["scale"], [0.15, 0.15, 0.15])
        self.assertNotIn("preserve_authored_scale", visual[0]["params"])
        self.assertTrue(visual[0]["params"]["preserve_visual_authored_scale"])
        native_source = (ROOT / "scripts" / "native_ue_scene.py").read_text(encoding="utf-8")
        self.assertIn('get("preserve_visual_authored_scale")', native_source)

    def test_uniform_instance_scale_reaches_runtime_mesh_and_visual_proxy(self) -> None:
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement

        binding = {
            "object_id": "crate",
            "runtime_actor_id": "actor_crate",
            "role": "passive target",
            "asset": {
                "ue_path": "/Game/Provider/Crate.Crate",
                "runtime_usage": "collision_and_visual",
                "scale_policy": "fit_uniform_to_approx_size",
                "scale_applied": True,
                "instance_scale": [0.75, 0.75, 0.75],
                "preserve_authored_scale": False,
            },
            "bounds": {"extents_m": [0.3, 0.2, 0.25]},
            "transform": {"position_m": [0.0, 0.0, 0.25]},
            "physics": {"simulate_physics": True, "collider": "mesh"},
        }

        dynamic, _ = runtime_objects_from_actor_placement(
            {"actor_bindings": [binding]},
            {"objects": [{"id": "crate"}]},
        )
        self.assertEqual(dynamic[0]["scale"], [0.75, 0.75, 0.75])
        self.assertTrue(dynamic[0]["params"]["preserve_authored_scale"])
        self.assertEqual(dynamic[0]["params"]["pose_anchor"], "bounds_center")

        visual_binding = deepcopy(binding)
        visual_binding["asset"]["runtime_usage"] = "visual_proxy"
        visual_binding["physics"]["collider"] = "box"
        visual, _ = runtime_objects_from_actor_placement(
            {"actor_bindings": [visual_binding]},
            {"objects": [{"id": "crate"}]},
        )
        self.assertEqual(visual[0]["params"]["visual_instance_scale"], [0.75, 0.75, 0.75])
        self.assertTrue(visual[0]["params"]["preserve_visual_authored_scale"])

    def test_runtime_verifier_rejects_unapplied_uniform_asset_scale(self) -> None:
        from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement

        placement = {
            "schema_version": "harness_runtime_actor_placement_v1",
            "actor_bindings": [
                {
                    "object_id": "crate",
                    "runtime_actor_id": "actor_crate",
                    "physics_critical": True,
                    "asset": {
                        "ue_path": "/Game/Provider/Crate.Crate",
                        "proxy": False,
                        "quality_gate": {"status": "pass_local_preview"},
                        "scale_policy": "fit_uniform_to_approx_size",
                        "scale_applied": False,
                        "instance_scale": [1.0, 1.0, 1.0],
                    },
                    "physics": {
                        "simulate_physics": True,
                        "collision_enabled": True,
                        "collider": "box",
                        "collision_profile": "PhysicsActor",
                        "mass_kg": 1.0,
                    },
                }
            ],
            "camera_bindings": [{"camera_id": "front"}],
            "physics_graph": {"collision_edges": []},
        }
        case = {
            "case_id": "invalid_scale",
            "objects": [
                {
                    "id": "crate",
                    "body_type": "dynamic",
                    "collision_required": True,
                }
            ],
        }

        report = verify_runtime_actor_placement(case, placement)

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["first_failure"]["metric"], "invalid_uniform_asset_instance_scale")

    def test_dynamic_sphere_uses_controlled_collision_geometry(self) -> None:
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement, ue_path_for_binding

        binding = {
            "object_id": "cue_ball",
            "runtime_actor_id": "actor_cue_ball",
            "role": "active_striker",
            "asset": {
                "ue_path": "/Game/Props/Decorative/SM_8Ball.SM_8Ball",
                "runtime_usage": "visual_proxy",
            },
            "bounds": {"extents_m": [0.09, 0.09, 0.09]},
            "transform": {"position_m": [0, 0, 0.09]},
            "physics": {
                "simulate_physics": True,
                "collider": "sphere",
                "mass_kg": 0.17,
                "collision_geometry_source": "analytic_sphere",
                "collision_geometry_verification": "runtime_controlled",
            },
        }

        self.assertEqual(ue_path_for_binding(binding), "/Engine/BasicShapes/Sphere.Sphere")
        dynamic, _ = runtime_objects_from_actor_placement(
            {"actor_bindings": [binding]},
            {
                "objects": [
                    {
                        "id": "cue_ball",
                        "enable_gravity": False,
                        "use_ccd": True,
                        "initial_velocity_m_s": [2.8, 0, 0],
                        "release_time_s": 1.0,
                        "hold_position_m": [0.0, -1.4, 0.1],
                    }
                ]
            },
        )
        self.assertEqual(dynamic[0]["ue5_path"], "/Engine/BasicShapes/Sphere.Sphere")
        self.assertEqual(dynamic[0]["params"]["visual_ue5_path"], "/Game/Props/Decorative/SM_8Ball.SM_8Ball")
        self.assertEqual(dynamic[0]["params"]["visual_collision_profile"], "NoCollision")
        self.assertFalse(dynamic[0]["params"]["visual_simulate_physics"])
        self.assertEqual(dynamic[0]["physics_properties"]["collision_geometry_source"], "analytic_sphere")
        self.assertEqual(dynamic[0]["physics_properties"]["collision_geometry_verification"], "runtime_controlled")
        self.assertFalse(dynamic[0]["physics_properties"]["enable_gravity"])
        self.assertTrue(dynamic[0]["physics_properties"]["use_ccd"])
        self.assertEqual(dynamic[0]["params"]["release_time_s"], 1.0)
        self.assertEqual(dynamic[0]["params"]["hold_position_m"], [0.0, -1.4, 0.1])

    def test_v2_delayed_release_uses_compiled_support_snapped_position(self) -> None:
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement

        binding = {
            "object_id": "crate",
            "runtime_actor_id": "actor_crate",
            "role": "dynamic crate",
            "asset": {"ue_path": "/Game/Props/SM_Crate.SM_Crate", "runtime_usage": "visual_proxy"},
            "bounds": {"extents_m": [0.3, 0.3, 0.3]},
            "transform": {"position_m": [1.0, 0.0, 0.83]},
            "physics": {"simulate_physics": True, "collider": "box"},
        }
        dynamic, _ = runtime_objects_from_actor_placement(
            {"actor_bindings": [binding]},
            {
                "v2_projection": {"source_schema_version": "harness_case_spec_v2"},
                "expected_physics": {"support": {"crate": "ramp"}},
                "objects": [{
                    "id": "crate",
                    "release_time_s": 0.7,
                    "hold_position_m": [1.0, 0.0, 0.5],
                    "release_position_m": [1.0, 0.0, 0.5],
                }],
            },
        )
        self.assertEqual(dynamic[0]["params"]["hold_position_m"], [1.0, 0.0, 0.83])
        self.assertEqual(dynamic[0]["params"]["release_position_m"], [1.0, 0.0, 0.83])

    def test_static_anchor_preserves_compiled_bounds_without_role_override(self) -> None:
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement, ue_path_for_binding

        binding = {
            "object_id": "anchor",
            "runtime_actor_id": "actor_anchor",
            "role": "elastic_constraint_anchor",
            "asset": {"ue_path": "/Script/Engine.Actor", "asset_kind": "analytic_actor"},
            "bounds": {"extents_m": [0.09, 0.09, 0.09]},
            "transform": {"position_m": [0.0, 0.0, 2.0]},
            "physics": {"simulate_physics": False, "kinematic": True, "collider": "none"},
        }

        self.assertEqual(ue_path_for_binding(binding), "/Engine/BasicShapes/Cube.Cube")
        _, static = runtime_objects_from_actor_placement(
            {"actor_bindings": [binding]},
            {"capability_id": "elastic_constraint_rebound", "objects": [{"id": "anchor"}]},
        )
        anchor = next(obj for obj in static if obj["id"] == "anchor")
        self.assertEqual(anchor["ue5_path"], "/Engine/BasicShapes/Cube.Cube")
        self.assertEqual(anchor["asset_kind"], "static_mesh")
        self.assertEqual(anchor["scale"], [0.09, 0.09, 0.09])
        self.assertEqual(anchor["params"]["desired_extent_cm"], 9.0)

    def test_compiled_dynamic_sphere_marks_selected_mesh_as_visual_only(self) -> None:
        from harness.runtime.actor_placement import compile_runtime_actor_placement

        placement = compile_runtime_actor_placement(
            {"case_id": "billiards"},
            {
                "case_id": "billiards",
                "object_nodes": [
                    {
                        "object_id": "cue_ball",
                        "role": "active_striker",
                        "shape": "sphere",
                        "physics_critical": True,
                        "physics_graph_member": True,
                        "transform": {"position_m": [0.0, 0.0, 0.09]},
                        "bounds": {"extents_m": [0.09, 0.09, 0.09]},
                        "physics": {
                            "collider": "sphere",
                            "collision_profile": "PhysicsActor",
                            "collision_geometry": {
                                "shape": "sphere",
                                "size_m": [0.18, 0.18, 0.18],
                                "local_center_offset_m": [0.0, 0.0, 0.0],
                                "world_center_m": [0.0, 0.0, 0.09],
                                "source": "analytic_sphere",
                            },
                        },
                        "asset_binding": {
                            "selected_asset_id": "game_props_decorative_sm_8ball",
                            "selected_asset_ue_path": "/Game/Props/Decorative/SM_8Ball.SM_8Ball",
                            "source_kind": "local_ue_project",
                        },
                    }
                ],
            },
        )

        binding = placement["actor_bindings"][0]
        self.assertEqual(binding["asset"]["runtime_usage"], "visual_proxy")
        self.assertEqual(binding["physics"]["collision_geometry_source"], "analytic_sphere")
        self.assertEqual(binding["physics"]["collision_geometry_verification"], "runtime_controlled")

    def test_static_selected_asset_and_hidden_collision_proxy_are_orthogonal(self) -> None:
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement

        node = {
            "object_id": "table",
            "role": "support",
            "shape": "box",
            "physics_critical": True,
            "transform": {"position_m": [0.0, 0.0, 0.55], "rotation_deg": [0.0, 0.0, 0.0]},
            "bounds": {
                "extents_m": [1.2, 0.55, 0.48],
                "local_center_offset_m": [0.0, 0.0, -0.48],
            },
            "visual_representation": {"source": "asset", "visible": True},
            "physics": {
                "body_type": "static",
                "collision_required": True,
                "collider": "box",
                "collision_profile": "BlockAll",
                "collision_geometry": {
                    "shape": "box",
                    "size_m": [2.4, 1.1, 0.06],
                    "local_center_offset_m": [0.0, 0.0, -0.03],
                    "world_center_m": [0.0, 0.0, 0.52],
                    "source": "declared",
                },
            },
            "asset_binding": {
                "selected_asset_id": "painted_table",
                "selected_asset_ue_path": "/Game/Provider/SM_Table.SM_Table",
                "sha256": "abc123",
            },
        }
        placement = compile_runtime_actor_placement(
            {"case_id": "table"},
            {"case_id": "table", "object_nodes": [node]},
        )
        binding = placement["actor_bindings"][0]
        dynamic, static = runtime_objects_from_actor_placement(
            placement,
            {"case_id": "table", "objects": [{"id": "table"}]},
        )

        self.assertEqual(dynamic, [])
        self.assertEqual(binding["asset"]["runtime_usage"], "visual_proxy")
        self.assertEqual(binding["transform"]["position_m"], [0.0, 0.0, 0.52])
        self.assertEqual(binding["visual_representation"]["local_bounds_center_from_collision_m"], [0.0, 0.0, -0.44999999999999996])
        self.assertEqual(static[0]["ue5_path"], "/Engine/BasicShapes/Cube.Cube")
        self.assertEqual(static[0]["scale"], [2.4, 1.1, 0.06])
        self.assertEqual(static[0]["params"]["visual_ue5_path"], "/Game/Provider/SM_Table.SM_Table")
        self.assertTrue(static[0]["params"]["visible"])

        hidden_node = deepcopy(node)
        hidden_node["object_id"] = "hidden_proxy"
        hidden_node["visual_representation"]["visible"] = False
        hidden = compile_runtime_actor_placement(
            {"case_id": "hidden"},
            {"case_id": "hidden", "object_nodes": [hidden_node]},
        )
        _, hidden_static = runtime_objects_from_actor_placement(
            hidden,
            {"case_id": "hidden", "objects": [{"id": "hidden_proxy"}]},
        )
        self.assertEqual(hidden["actor_bindings"][0]["asset"]["runtime_usage"], "analytic_proxy")
        self.assertFalse(hidden_static[0]["params"]["visible"])
        self.assertNotIn("visual_ue5_path", hidden_static[0]["params"])
        self.assertTrue(hidden_static[0]["physics_properties"]["collision_enabled"])

    def test_hidden_explicit_collision_needs_no_asset_metadata(self) -> None:
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement

        node = {
            "object_id": "hidden_wall",
            "role": "collision wall",
            "shape": "box",
            "physics_critical": True,
            "transform": {"position_m": [0.0, 0.0, 0.5], "rotation_deg": [0.0, 0.0, 0.0]},
            "visual_representation": {"source": "none", "visible": False},
            "physics": {
                "body_type": "static",
                "collision_required": True,
                "collider": "box",
                "collision_geometry": {
                    "shape": "box",
                    "size_m": [0.1, 1.0, 1.0],
                    "local_center_offset_m": [0.0, 0.0, 0.0],
                    "world_center_m": [0.0, 0.0, 0.5],
                    "source": "declared",
                },
            },
            "asset_binding": {},
        }

        placement = compile_runtime_actor_placement(
            {"case_id": "hidden"},
            {"case_id": "hidden", "object_nodes": [node]},
        )
        binding = placement["actor_bindings"][0]
        _, static = runtime_objects_from_actor_placement(
            placement,
            {"case_id": "hidden", "objects": [{"id": "hidden_wall"}]},
        )

        self.assertTrue(binding["asset"]["proxy"])
        self.assertEqual(binding["asset"]["runtime_usage"], "analytic_proxy")
        self.assertEqual(binding["physics"]["collision_profile"], "BlockAll")
        self.assertEqual(binding["physics"]["collision_geometry_verification"], "runtime_controlled")
        self.assertEqual(static[0]["ue5_path"], "/Engine/BasicShapes/Cube.Cube")
        self.assertFalse(static[0]["params"]["visible"])

    def test_asset_body_setup_remains_collision_and_visual(self) -> None:
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement

        node = {
            "object_id": "complex_asset",
            "role": "dynamic body",
            "shape": "irregular",
            "physics_critical": True,
            "transform": {"position_m": [0.0, 0.0, 1.0], "rotation_deg": [0.0, 0.0, 0.0]},
            "visual_representation": {"source": "asset", "visible": True},
            "physics": {
                "body_type": "dynamic",
                "collision_required": True,
                "collider": "simple_convex",
                "collision_profile": "PhysicsActor",
                "mass_kg": 2.0,
            },
            "asset_binding": {
                "selected_asset_id": "complex_asset",
                "selected_asset_ue_path": "/Game/Generated/SM_Complex.SM_Complex",
                "asset_kind": "StaticMesh",
                "collision": {"present": True, "kind": "simple_convex"},
                "collision_body_setup_verified": True,
                "quality_gate": {"status": "pass"},
                "instance_scale": [1.0, 1.0, 1.0],
            },
        }

        placement = compile_runtime_actor_placement(
            {"case_id": "asset_collision"},
            {"case_id": "asset_collision", "object_nodes": [node]},
        )
        binding = placement["actor_bindings"][0]
        dynamic, _ = runtime_objects_from_actor_placement(
            placement,
            {"case_id": "asset_collision", "objects": [{"id": "complex_asset"}]},
        )

        self.assertIsNone(binding["physics"]["collision_geometry"])
        self.assertEqual(binding["physics"]["collision_geometry_source"], "asset_body_setup")
        self.assertEqual(binding["physics"]["collision_geometry_verification"], "body_setup_verified")
        self.assertEqual(binding["asset"]["runtime_usage"], "collision_and_visual")
        self.assertEqual(dynamic[0]["ue5_path"], "/Game/Generated/SM_Complex.SM_Complex")
        self.assertNotIn("visual_ue5_path", dynamic[0]["params"])

        node["asset_binding"]["collision_body_setup_verified"] = False
        unverified = compile_runtime_actor_placement(
            {"case_id": "asset_collision"},
            {"case_id": "asset_collision", "object_nodes": [node]},
        )
        report = verify_runtime_actor_placement({"case_id": "asset_collision"}, unverified)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F2_asset_missing")
        self.assertEqual(report["first_failure"]["metric"], "collision_binding_unverified")

    def test_declared_box_collider_overrides_unverified_selected_asset_collision(self) -> None:
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement, ue_path_for_binding

        placement = compile_runtime_actor_placement(
            {"case_id": "box"},
            {
                "case_id": "box",
                "object_nodes": [{
                    "object_id": "box",
                    "role": "passive_target",
                    "shape": "box",
                    "physics_critical": True,
                    "physics": {
                        "collider": "box",
                        "collision_profile": "PhysicsActor",
                        "collision_geometry": {
                            "shape": "box",
                            "size_m": [0.5, 0.5, 0.5],
                            "local_center_offset_m": [0.0, 0.0, 0.0],
                            "world_center_m": [0.0, 0.0, 0.0],
                            "source": "analytic_box",
                        },
                    },
                    "asset_binding": {"selected_asset_ue_path": "/Game/Props/SM_Box.SM_Box"},
                }],
            },
        )

        binding = placement["actor_bindings"][0]
        self.assertEqual(binding["asset"]["runtime_usage"], "visual_proxy")
        self.assertEqual(binding["physics"]["collision_geometry_source"], "analytic_box")
        self.assertEqual(binding["physics"]["collision_geometry_verification"], "runtime_controlled")
        self.assertEqual(ue_path_for_binding(binding), "/Engine/BasicShapes/Cube.Cube")
        dynamic, static = runtime_objects_from_actor_placement(
            placement,
            {"case_id": "box", "objects": [{"id": "box"}]},
        )
        self.assertEqual(static, [])
        self.assertEqual(dynamic[0]["ue5_path"], "/Engine/BasicShapes/Cube.Cube")
        self.assertEqual(dynamic[0]["params"]["visual_ue5_path"], "/Game/Props/SM_Box.SM_Box")
        self.assertEqual(dynamic[0]["params"]["visual_collision_profile"], "NoCollision")
        self.assertFalse(dynamic[0]["params"]["visual_simulate_physics"])
        self.assertEqual(dynamic[0]["physics_properties"]["collision_geometry_source"], "analytic_box")

    def test_declared_cylinder_collider_uses_centered_proxy_for_provider_visual(self) -> None:
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement, ue_path_for_binding

        placement = compile_runtime_actor_placement(
            {"case_id": "bottle"},
            {
                "case_id": "bottle",
                "object_nodes": [{
                    "object_id": "bottle",
                    "role": "passive_target",
                    "shape": "cylinder",
                    "physics_critical": True,
                    "physics": {
                        "collider": "cylinder",
                        "collision_profile": "PhysicsActor",
                        "collision_geometry": {
                            "shape": "cylinder",
                            "size_m": [0.5, 0.5, 0.5],
                            "local_center_offset_m": [0.0, 0.0, 0.0],
                            "world_center_m": [0.0, 0.0, 0.0],
                            "source": "analytic_cylinder",
                        },
                    },
                    "asset_binding": {
                        "selected_asset_ue_path": "/Game/Provider/SM_Bottle.SM_Bottle",
                        "preserve_authored_scale": True,
                    },
                }],
            },
        )

        binding = placement["actor_bindings"][0]
        self.assertEqual(binding["asset"]["runtime_usage"], "visual_proxy")
        self.assertEqual(binding["physics"]["collision_geometry_source"], "analytic_cylinder")
        self.assertEqual(binding["physics"]["collision_geometry_verification"], "runtime_controlled")
        self.assertEqual(ue_path_for_binding(binding), "/Engine/BasicShapes/Cylinder.Cylinder")
        dynamic, static = runtime_objects_from_actor_placement(
            placement,
            {"case_id": "bottle", "objects": [{"id": "bottle"}]},
        )
        self.assertEqual(static, [])
        self.assertEqual(dynamic[0]["ue5_path"], "/Engine/BasicShapes/Cylinder.Cylinder")
        self.assertEqual(dynamic[0]["params"]["visual_ue5_path"], "/Game/Provider/SM_Bottle.SM_Bottle")
        self.assertTrue(dynamic[0]["params"]["preserve_visual_authored_scale"])

    def test_native_visual_proxy_tracks_collision_bounds_center(self) -> None:
        native_source = (ROOT / "scripts" / "native_ue_scene.py").read_text(encoding="utf-8")
        self.assertIn("physics_origin, _ = actor_bounds(physics_actor)", native_source)
        self.assertIn("visual_origin, _ = actor_bounds(visual)", native_source)
        self.assertIn("center_delta = physics_origin + world_offset - visual_origin", native_source)
        self.assertIn(
            'obj.get("initial_position_m", [0.0, 0.0, 0.0]),\n                z_offset_cm=0.0,\n                origin=scene_origin,',
            native_source,
        )

    def test_native_ue_preserves_selected_asset_materials_for_llm_objects(self) -> None:
        native_source = (ROOT / "scripts" / "native_ue_scene.py").read_text(encoding="utf-8")
        self.assertIn('params.get("binding_source") == "ue_asset"', native_source)
        self.assertIn('params.get("asset_runtime_usage") in {"collision_and_visual", "visual_proxy"}', native_source)

    def test_local_ue_runner_preserves_compiled_actor_extent(self) -> None:
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement

        placement = {
            "actor_bindings": [{
                "object_id": "ball",
                "runtime_actor_id": "actor_ball",
                "role": "active",
                "asset": {"proxy": True},
                "transform": {"position_m": [0, 0, 0.09]},
                "bounds": {"extents_m": [0.09, 0.09, 0.09]},
                "physics": {
                    "simulate_physics": True,
                    "collider": "sphere",
                    "material": {"static_friction": 0.05, "dynamic_friction": 0.035, "restitution": 0.88},
                },
            }],
        }
        dynamic, static = runtime_objects_from_actor_placement(
            placement,
            {
                "objects": [{"id": "ball", "visual_material_path": "/Game/Materials/MI_Glass.MI_Glass"}],
                "physical_parameters": {
                    "ball_linear_damping": 0.01,
                    "ball_angular_damping": 0.02,
                },
            },
        )

        self.assertFalse(static)
        self.assertEqual(dynamic[0]["params"]["desired_extent_cm"], 9.0)
        self.assertEqual(dynamic[0]["physics_properties"]["dynamic_friction"], 0.035)
        self.assertEqual(dynamic[0]["physics_properties"]["restitution"], 0.88)
        self.assertIsNone(dynamic[0]["physics_properties"]["linear_damping"])
        self.assertIsNone(dynamic[0]["physics_properties"]["angular_damping"])
        self.assertEqual(dynamic[0]["params"]["visual_material_path"], "/Game/Materials/MI_Glass.MI_Glass")

    def test_domino_case_compiles_as_valid_initial_state_only_chaos_scene(self) -> None:
        from copy import deepcopy

        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.core.case_spec import validate_case_spec
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from harness.runtime.camera_planner import camera_plan_from_case_spec
        from scripts.harness_local_ue_runner import default_physics_controls, runtime_objects_from_actor_placement

        case = self.load_case("cases/domino/five_domino_chain.json")
        assets = resolve_asset_intents(case)
        layout = build_static_scene_layout(case, asset_resolution=assets)
        placement = compile_runtime_actor_placement(case, layout, asset_resolution=assets)
        dynamic, static = runtime_objects_from_actor_placement(placement, case)
        physics_controls = default_physics_controls(case)
        camera_plan = camera_plan_from_case_spec(case, requested_views=["event_closeup"])

        self.assertFalse(layout["overlap_pairs"])
        self.assertTrue(all(row["status"] == "contact_at_rest" for row in layout["support_relations"]))
        self.assertEqual(case["expected_physics"]["simulation_contract"]["input_mode"], "initial_state_only")
        self.assertEqual(case["expected_physics"]["simulation_contract"]["state_solver"], "ue_chaos")
        self.assertEqual(physics_controls["input_mode"], "initial_state_only")
        self.assertEqual(physics_controls["state_solver"], "ue_chaos")
        self.assertEqual([obj["id"] for obj in dynamic], [f"domino_{index}" for index in range(5)])
        self.assertEqual([obj["id"] for obj in static], ["domino_floor"])
        self.assertTrue(all(obj["ue5_path"] == "/Engine/BasicShapes/Cube.Cube" for obj in dynamic + static))
        self.assertTrue(
            all(
                obj["physics_properties"]["collision_geometry_source"] == "compiled_default"
                for obj in dynamic + static
            )
        )
        self.assertTrue(
            all(
                obj["physics_properties"]["collision_geometry_verification"] == "runtime_controlled"
                for obj in dynamic + static
            )
        )
        self.assertNotIn("support_top_m", static[0]["params"])
        self.assertEqual(dynamic[0]["rotation_degrees"], [0.0, -20.0, 0.0])
        self.assertTrue(all(obj.get("rotation_degrees") == [0.0, 0.0, 0.0] for obj in dynamic[1:]))
        self.assertTrue(all(obj["physics_properties"]["initial_velocity_m_s"] == [0.0, 0.0, 0.0] for obj in dynamic))
        self.assertTrue(all(obj["physics_properties"]["initial_angular_velocity_rad_s"] == [0.0, 0.0, 0.0] for obj in dynamic))
        self.assertEqual(camera_plan.views[0].target, (0.36, 0.0, 0.6))

        drifted = deepcopy(case)
        drifted["physical_parameters"]["initial_pitch_deg"] = -10.0
        validate_case_spec(drifted)

    def test_sixth_domino_extends_the_existing_initial_state_chain(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.core.case_spec import validate_case_spec
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement

        case = self.load_case("cases/domino/six_domino_chain.json")
        validate_case_spec(case)
        assets = resolve_asset_intents(case)
        layout = build_static_scene_layout(case, asset_resolution=assets)
        placement = compile_runtime_actor_placement(case, layout, asset_resolution=assets)
        dynamic, static = runtime_objects_from_actor_placement(placement, case)

        self.assertEqual([obj["id"] for obj in dynamic], [f"domino_{index}" for index in range(6)])
        self.assertEqual([obj["id"] for obj in static], ["domino_floor"])
        self.assertIn(["domino_4", "domino_5"], placement["physics_graph"]["collision_edges"])
        self.assertEqual(case["passive_objects"][-1], "domino_5")
        self.assertIs(case["objects"][-1]["fit_dynamic_plan"], False)
        self.assertEqual(dynamic[-1]["rotation_degrees"], [0.0, 0.0, 0.0])
        self.assertEqual(dynamic[-1]["physics_properties"]["initial_velocity_m_s"], [0.0, 0.0, 0.0])

    def test_precomputed_trajectory_disables_chaos_process_override(self) -> None:
        from scripts.harness_local_ue_runner import run_native_pass

        runtime_scene = {
            "simulation": {"duration_s": 1.0},
            "physics_controls": {"runtime_driver_backend": "precomputed_trajectory"},
        }
        with tempfile.TemporaryDirectory() as tmp, patch("scripts.harness_local_ue_runner.run_ue_until_artifacts", return_value={"status": "ok"}) as runner:
            run_native_pass(
                ["UnrealEditor-Cmd"],
                args=Namespace(map=""),
                case_spec={"case_id": "case_a"},
                runtime_scene=runtime_scene,
                studio_scene_path=Path(tmp) / "scene.json",
                runtime_scene_path=Path(tmp) / "runtime.json",
                native_output=Path(tmp) / "native",
                pass_mode="data",
            )

        self.assertEqual(runner.call_args.kwargs["env"]["CHAOS_SIMULATION_ENABLED"], "0")

    def test_constraint_case_does_not_add_nonphysical_tether_visual(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from scripts.harness_local_ue_runner import runtime_objects_from_actor_placement

        case = self.load_case("cases/elastic_constraint/bungee_rebound.json")
        assets = resolve_asset_intents(case)
        layout = build_static_scene_layout(case, asset_resolution=assets)
        placement = compile_runtime_actor_placement(case, layout, asset_resolution=assets)

        dynamic, static = runtime_objects_from_actor_placement(placement, case)

        self.assertEqual([obj["id"] for obj in dynamic], ["payload"])
        self.assertEqual(dynamic[0]["ue5_path"], "/Engine/BasicShapes/Sphere.Sphere")
        self.assertEqual(dynamic[0]["asset_kind"], "static_mesh")
        self.assertNotIn("elastic_tether_visual", {obj["id"] for obj in static})

        from scripts.harness_local_ue_runner import ue_path_for_binding

        self.assertEqual(
            ue_path_for_binding(
                {
                    "role": "elastic_constrained_body",
                    "physics": {"simulate_physics": True, "collider": "actor"},
                    "asset": {"ue_path": "/Game/Blueprints/BP_Projectile.BP_Projectile", "asset_kind": "Blueprint"},
                }
            ),
            "/Game/Blueprints/BP_Projectile.BP_Projectile",
        )

    def test_ue_rigid_defaults_to_live_chaos_without_precomputed_trajectory(self) -> None:
        from scripts.harness_local_ue_runner import prepare_rigid_simulation

        with patch.dict("os.environ", {}, clear=True), patch(
            "scripts.harness_local_ue_runner.simulate_rigid_case"
        ) as simulate:
            trajectory = prepare_rigid_simulation({}, {}, fps=60, duration_s=1.0)

        self.assertIsNone(trajectory)
        simulate.assert_not_called()

    def test_mujoco_replay_requires_explicit_debug_mode(self) -> None:
        from scripts.harness_local_ue_runner import prepare_rigid_simulation

        expected = [{"frame": 0, "objects": {}}]
        with patch.dict("os.environ", {"SIM_STUDIO_UE_RIGID_MODE": "mujoco_replay"}, clear=True), patch(
            "scripts.harness_local_ue_runner.simulate_rigid_case", return_value=expected
        ) as simulate:
            trajectory = prepare_rigid_simulation({"case_id": "case_a"}, {}, fps=60, duration_s=1.0)

        self.assertEqual(trajectory, expected)
        simulate.assert_called_once()

    def test_live_data_request_runs_solver_pass_before_data_replay(self) -> None:
        from scripts.harness_local_ue_runner import pass_sequence

        self.assertEqual(pass_sequence("data", live_solver=True), ["rgb", "data"])

    def test_live_fracture_both_mode_uses_one_shared_solver_pass(self) -> None:
        from scripts.harness_local_ue_runner import pass_sequence

        self.assertEqual(
            pass_sequence(
                "both",
                live_solver=True,
                live_fracture=True,
                single_pass_fracture=True,
            ),
            ["combined"],
        )

    def test_only_explicit_fracture_objects_require_live_data_pass(self) -> None:
        from scripts.harness_local_ue_runner import fracture_object_ids

        self.assertEqual(
            fracture_object_ids({"objects": [{"id": "panel", "fracture_response": {"mode": "contact_external_strain"}}]}),
            {"panel"},
        )
        self.assertEqual(fracture_object_ids({"objects": [{"id": "ball"}]}), set())

    def test_positive_fracture_sensor_sync_requires_events_and_fragment_state_hashes(self) -> None:
        from scripts.harness_local_ue_runner import build_fracture_sensor_state_report

        spec = {"objects": [{"id": "panel", "fracture_response": {"mode": "contact_external_strain"}}]}
        missing = build_fracture_sensor_state_report(spec, "both", [], [])
        self.assertEqual(missing["status"], "fail")
        self.assertEqual(
            set(missing["failure_codes"]),
            {"F_FRACTURE_SENSOR_EVENT_MISSING", "F_FRACTURE_FRAGMENT_STATE_MISSING"},
        )

        rgb = [{"object_id": "panel", "frame": 12, "fragment_state_sha256": "same"}]
        data = [{"object_id": "panel", "frame": 12, "fragment_state_sha256": "same"}]
        matching = build_fracture_sensor_state_report(spec, "both", rgb, data)
        self.assertEqual(matching["status"], "pass")
        self.assertTrue(matching["comparison_required"])

    def test_declared_intact_fracture_case_does_not_require_break_events(self) -> None:
        from scripts.harness_local_ue_runner import build_fracture_sensor_state_report

        spec = {
            "expected_physics": {"expected_fracture": False},
            "objects": [{"id": "panel", "fracture_response": {"mode": "contact_external_strain"}}],
        }

        report = build_fracture_sensor_state_report(spec, "both", [], [])

        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["comparison_required"])

    def test_declared_intact_fracture_case_rejects_break_in_either_sensor_pass(self) -> None:
        from scripts.harness_local_ue_runner import build_fracture_sensor_state_report

        spec = {
            "expected_physics": {"expected_fracture": False},
            "objects": [{"id": "panel", "fracture_response": {"mode": "contact_external_strain"}}],
        }
        event = {"object_id": "panel", "frame": 12, "fragment_state_sha256": "same"}

        for rgb, data in (([event], []), ([], [event]), ([event], [event])):
            with self.subTest(rgb=bool(rgb), data=bool(data)):
                report = build_fracture_sensor_state_report(spec, "both", rgb, data)
                self.assertEqual(report["status"], "fail")
                self.assertIn("F_FRACTURE_UNEXPECTED", report["failure_codes"])

    def test_fracture_sensor_sync_rejects_matching_event_with_different_fragment_state(self) -> None:
        from scripts.harness_local_ue_runner import build_fracture_sensor_state_report

        spec = {"objects": [{"id": "panel", "fracture_response": {"mode": "contact_external_strain"}}]}
        rgb = [{"object_id": "panel", "frame": 12, "fragment_state_sha256": "rgb"}]
        data = [{"object_id": "panel", "frame": 12, "fragment_state_sha256": "data"}]
        report = build_fracture_sensor_state_report(spec, "both", rgb, data)
        self.assertEqual(report["status"], "fail")
        self.assertIn("F_FRACTURE_SENSOR_STATE_MISMATCH", report["failure_codes"])

    def test_live_fracture_data_uses_async_pie_capture_backend(self) -> None:
        from scripts.harness_local_ue_runner import build_runtime_scene

        scene = build_runtime_scene(
            {
                "case_id": "fracture_case",
                "objects": [
                    {
                        "id": "panel",
                        "fracture_response": {"mode": "contact_external_strain"},
                    }
                ],
            },
            {"views": []},
            Namespace(map=""),
            pass_mode="data",
            simulation_trajectory=None,
        )

        self.assertTrue(scene["physics_controls"]["simulate_physics"])
        self.assertEqual(scene["map_lighting_controls"]["capture_backend"], "highres_viewport")

    def test_map_report_projects_lighting_from_the_actual_rgb_pass(self) -> None:
        from scripts.harness_local_ue_runner import build_map_report

        native_summary = {
            "selected_map": {"opened": True, "opened_package": "/Game/Test"},
            "loaded_map_actor_count": 2,
            "lighting": {"existing_map_lights": {"source": "data_pass"}},
        }
        rgb_summary = {
            "capture_backend": "highres_viewport",
            "lighting": {
                "capture_backend": "highres_viewport",
                "existing_map_lights": {"source": "rgb_pass"},
                "runtime_light_audit": {"preview_shadow_safe": True},
                "highres_viewport": {
                    "game_view_after": True,
                    "preview_shadow_indicator_disabled": True,
                },
                "preview_shadow_safe": True,
            },
        }

        report = build_map_report(
            native_summary,
            rgb_summary,
            {"background": {"ue5_path": "/Game/Test.Test"}},
            {"views": [{"camera_id": "front_static"}]},
        )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["available_lights"]["source"], "data_pass")
        self.assertEqual(report["rgb_capture"]["existing_map_lights"]["source"], "rgb_pass")
        self.assertEqual(report["rgb_capture"]["backend"], "highres_viewport")
        self.assertTrue(report["rgb_capture"]["preview_shadow_safe"])

    def test_chaos_output_replay_is_not_labeled_as_mujoco(self) -> None:
        from scripts.harness_local_ue_runner import build_runtime_scene

        trajectory = [
            {
                "frame": 0,
                "time": 0.0,
                "source": "adp_cpp_runtime_driver",
                "objects": {"ball": {"source": "adp_cpp_runtime_driver", "position": [0.0, 0.0, 0.0]}},
            }
        ]
        scene = build_runtime_scene(
            {"case_id": "case_a"},
            {"views": []},
            Namespace(map=""),
            pass_mode="data",
            simulation_trajectory=trajectory,
        )

        self.assertEqual(scene["physics_controls"]["simulation_driver"], "ue_chaos_output_replay")
        self.assertEqual(scene["physics_controls"]["trajectory_source"], "adp_cpp_runtime_driver")

    def test_camera_runtime_uses_compiled_plan_pose_and_fov(self) -> None:
        from scripts.harness_local_ue_runner import camera_intrinsics, camera_runtime_from_plan

        runtime = camera_runtime_from_plan({"views": [{"camera_id": "front_static", "location": [2.0, -3.0, 4.0], "target": [0.0, 0.0, 0.5], "fov": 60.0}]})

        self.assertEqual(runtime["camera_id"], "front_static")
        self.assertEqual(runtime["fov"], 60.0)
        self.assertEqual(runtime["preview_waypoints"][0]["position_m"], [2.0, -3.0, 4.0])
        self.assertEqual(runtime["preview_waypoints"][0]["target_offset_m"], [0.0, 0.0, 0.5])
        self.assertEqual(runtime["views"][0]["camera_id"], "front_static")
        self.assertEqual(camera_intrinsics(1920, 1080, 60.0)["cx"], 960.0)

    def test_local_ue_runner_accepts_non_front_primary_video(self) -> None:
        from scripts.harness_local_ue_runner import native_output_ready, standardize_native_output

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            native = root / "native"
            run_dir.mkdir()
            native.mkdir()
            video = native / "preview_top_down.mp4"
            video.write_bytes(b"\x00\x00\x00\x18ftypisom")
            for name, payload in (
                ("summary.json", {"frames": 2, "fps": 24}),
                ("trajectory.json", [{"frame": 0, "objects": {}}]),
                ("camera_trajectories.json", {"views": []}),
                ("render_pass_manifest.json", {"passes": {"rgb": {"views": [{"view_id": "top_down", "path": str(video)}]}}}),
            ):
                (native / name).write_text(json.dumps(payload), encoding="utf-8")

            report = standardize_native_output(
                run_dir,
                native,
                {"views": [{"camera_id": "top_down"}]},
                0.0,
            )

            self.assertEqual(report["status"], "completed")
            self.assertTrue(native_output_ready(native))
            self.assertEqual((run_dir / "video.mp4").read_bytes(), b"\x00\x00\x00\x18ftypisom")

    def test_native_output_ready_accepts_multiview_manifest_without_default_preview(self) -> None:
        from scripts.harness_local_ue_runner import native_output_ready

        with tempfile.TemporaryDirectory() as tmp:
            native = Path(tmp)
            views = []
            for camera_id in ("top_down", "event_closeup"):
                video = native / f"preview_{camera_id}.mp4"
                video.write_bytes(b"\x00\x00\x00\x18ftypisom")
                views.append({"view_id": camera_id, "path": str(video)})
            for name, payload in (
                ("summary.json", {"frames": 2, "fps": 24}),
                ("trajectory.json", [{"frame": 0, "objects": {}}]),
                ("camera_trajectories.json", {"views": []}),
                ("render_pass_manifest.json", {"passes": {"rgb": {"views": views}}}),
            ):
                (native / name).write_text(json.dumps(payload), encoding="utf-8")

            self.assertTrue(native_output_ready(native))

    def test_native_view_lookup_never_falls_back_by_position(self) -> None:
        from scripts.harness_local_ue_runner import native_view_for_camera

        views = [{"view_id": "top_down", "path": "/tmp/top_down.mp4"}]

        self.assertEqual(native_view_for_camera("event_closeup", 0, views), {})

    def test_depth_preview_clips_far_range_before_color_mapping(self) -> None:
        from scripts.harness_local_ue_runner import encode_sensor_preview

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame_dir = root / "depth_frames"
            frame_dir.mkdir()
            (frame_dir / "frame_0000.exr").touch()
            output = root / "depth_preview.mp4"
            captured: dict[str, list[str]] = {}

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
                captured["command"] = command
                output.write_bytes(b"\x00\x00\x00\x18ftypisom")
                return subprocess.CompletedProcess(command, 0, b"", b"")

            with patch("scripts.harness_local_ue_runner.subprocess.run", side_effect=fake_run):
                self.assertTrue(encode_sensor_preview(frame_dir, output, fps=24, modality="depth"))

            video_filter = captured["command"][captured["command"].index("-vf") + 1]
            self.assertIn("colorlevels", video_filter)
            self.assertIn("rimax=0.08", video_filter)
            self.assertIn("pseudocolor=preset=viridis", video_filter)

    def test_standardizer_uses_highres_rgb_truth_and_exact_modality_views(self) -> None:
        from scripts.harness_local_ue_runner import standardize_native_output

        mp4 = b"\x00\x00\x00\x18ftypisom"
        exr = b"\x76\x2f\x31\x01"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            data_native = root / "native_data"
            rgb_native = root / "native_rgb"
            run_dir.mkdir()
            data_native.mkdir()
            rgb_native.mkdir()

            data_preview = data_native / "preview.mp4"
            data_preview.write_bytes(mp4 + b"data")
            highres_top = rgb_native / "preview_top_down.mp4"
            highres_top.write_bytes(mp4 + b"highres-top")
            data_rgb_event = data_native / "event_closeup.mp4"
            data_rgb_event.write_bytes(mp4 + b"data-event")
            data_rgb_top = data_native / "top_down.mp4"
            data_rgb_top.write_bytes(mp4 + b"data-top")
            for modality in ("depth", "segmentation"):
                for camera_id in ("event_closeup", "top_down"):
                    (data_native / f"{modality}_{camera_id}.exr").write_bytes(
                        exr + f"{modality}-{camera_id}".encode()
                    )

            summary = {"frames": 2, "fps": 24, "width": 640, "height": 360}
            trajectory = [{"frame": 0, "objects": {}}]
            data_cameras = {
                "schema_version": "camera_trajectories_v1",
                "views": [
                    {"view_id": camera_id, "frames": [{"frame": 0, "fov": 50.0}]}
                    for camera_id in ("event_closeup", "top_down")
                ],
            }
            rgb_cameras = {
                "schema_version": "camera_trajectories_v1",
                "truth": "highres",
                "views": [{"view_id": "top_down", "frames": [{"frame": 0, "fov": 52.0}]}],
            }
            data_manifest = {
                "passes": {
                    "rgb": {
                        "views": [
                            {"view_id": "event_closeup", "path": str(data_rgb_event)},
                            {"view_id": "top_down", "path": str(data_rgb_top)},
                        ]
                    },
                    "depth": {
                        "views": [
                            {"view_id": camera_id, "frames": [str(data_native / f"depth_{camera_id}.exr")]}
                            for camera_id in ("event_closeup", "top_down")
                        ]
                    },
                    "segmentation": {
                        "views": [
                            {"view_id": camera_id, "frames": [str(data_native / f"segmentation_{camera_id}.exr")]}
                            for camera_id in ("event_closeup", "top_down")
                        ]
                    },
                }
            }
            rgb_manifest = {
                "frame_count": 2,
                "passes": {"rgb": {"views": [{"view_id": "top_down", "path": str(highres_top)}]}},
            }
            for native, cameras, manifest in (
                (data_native, data_cameras, data_manifest),
                (rgb_native, rgb_cameras, rgb_manifest),
            ):
                (native / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
                (native / "trajectory.json").write_text(json.dumps(trajectory), encoding="utf-8")
                (native / "camera_trajectories.json").write_text(json.dumps(cameras), encoding="utf-8")
                (native / "render_pass_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            camera_plan = {
                "views": [
                    {"camera_id": "event_closeup"},
                    {"camera_id": "top_down"},
                ]
            }
            with patch("scripts.harness_local_ue_runner.depth_pixel_statistics", return_value={"variance": 1.0}), patch(
                "scripts.harness_local_ue_runner.encode_sensor_preview", return_value=False
            ):
                report = standardize_native_output(
                    run_dir,
                    data_native,
                    camera_plan,
                    0.0,
                    render_mode="both",
                    rgb_native_output=rgb_native,
                )

            event_meta = json.loads((run_dir / "views" / "event_closeup" / "meta.json").read_text())
            top_meta = json.loads((run_dir / "views" / "top_down" / "meta.json").read_text())
            self.assertEqual(report["status"], "completed")
            self.assertFalse(report["rgb_real_ue"])
            self.assertFalse((run_dir / "views" / "event_closeup" / "rgb.mp4").exists())
            self.assertEqual(event_meta["frame_count_rgb"], 0)
            self.assertIsNone(event_meta["source_native_view_id"])
            self.assertFalse(event_meta["native_ue_rgb"])
            self.assertEqual(top_meta["frame_count_rgb"], 2)
            self.assertEqual(top_meta["source_native_view_id"], "top_down")
            self.assertEqual((run_dir / "views" / "top_down" / "rgb.mp4").read_bytes(), mp4 + b"highres-top")
            self.assertEqual(
                (run_dir / "views" / "event_closeup" / "depth.exr").read_bytes(),
                exr + b"depth-event_closeup",
            )
            self.assertEqual(
                (run_dir / "views" / "event_closeup" / "segmentation.exr").read_bytes(),
                exr + b"segmentation-event_closeup",
            )
            self.assertEqual(json.loads((run_dir / "camera_trajectory.json").read_text()), rgb_cameras)
            self.assertEqual(
                json.loads((run_dir / "render_manifest.json").read_text())["passes"]["rgb"],
                rgb_manifest["passes"]["rgb"],
            )

    def test_native_exr_pass_is_canonical_and_hard_linked(self) -> None:
        from scripts.harness_local_ue_runner import copy_native_pass_view

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "native" / "mask.png"
            source.parent.mkdir()
            source.write_bytes(b"\x76\x2f\x31\x01mask")
            view_dir = root / "views" / "front"
            view_dir.mkdir(parents=True)

            result = copy_native_pass_view(
                {
                    "frames": [str(source)],
                    "depth_type": "view_z",
                    "depth_encoding": "linear_view_z_times_0.0001",
                    "stored_value_to_centimeter": 10000.0,
                },
                view_dir,
                "segmentation.exr",
                "segmentation_frames",
            )

            anchor = view_dir / "segmentation.exr"
            frame = view_dir / "segmentation_frames" / "mask.exr"
            self.assertTrue(result["available"])
            self.assertTrue(source.samefile(anchor))
            self.assertTrue(source.samefile(frame))
            self.assertEqual(result["depth_type"], "view_z")
            self.assertEqual(result["stored_value_to_centimeter"], 10000.0)

    def test_noncanonical_frame_cleanup_preserves_canonical_sensor_sequences(self) -> None:
        from scripts.harness_local_ue_runner import cleanup_noncanonical_render_frames

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            native = run_dir / "logs" / "native_data"
            source = native / "segmentation" / "front_static" / "frame_0000.exr"
            raw = native / "segmentation_raw" / "front_static" / "frame_0000.exr"
            depth = native / "depth_exr" / "front_static" / "frame_0000.exr"
            rgb = native / "frames" / "frame_0000.png"
            for path, payload in ((source, b"mask"), (raw, b"raw"), (depth, b"depth"), (rgb, b"rgb")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            canonical = run_dir / "views" / "front_static" / "segmentation_frames" / "frame_0000.exr"
            canonical.parent.mkdir(parents=True, exist_ok=True)
            canonical.hardlink_to(source)
            meta = run_dir / "views" / "front_static" / "meta.json"
            meta.write_text(json.dumps({"segmentation_raw_frames": [str(raw)]}), encoding="utf-8")

            report = cleanup_noncanonical_render_frames(
                run_dir,
                native_outputs=[native],
                keep_render_frames=False,
            )

            self.assertEqual(report["status"], "pass")
            self.assertTrue(report["canonical_sensor_sequences_retained"])
            self.assertTrue(canonical.is_file())
            self.assertEqual(canonical.read_bytes(), b"mask")
            self.assertFalse((native / "segmentation").exists())
            self.assertFalse((native / "segmentation_raw").exists())
            self.assertFalse((native / "depth_exr").exists())
            self.assertFalse((native / "frames").exists())
            self.assertEqual(json.loads(meta.read_text())["segmentation_raw_frames"], [])

    def test_noncanonical_frame_cleanup_honors_keep_switch(self) -> None:
        from scripts.harness_local_ue_runner import cleanup_noncanonical_render_frames

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            native = run_dir / "logs" / "native_rgb"
            frame = native / "frames" / "frame_0000.png"
            frame.parent.mkdir(parents=True)
            frame.write_bytes(b"rgb")

            report = cleanup_noncanonical_render_frames(
                run_dir,
                native_outputs=[native],
                keep_render_frames=True,
            )

            self.assertEqual(report["status"], "pass")
            self.assertTrue(report["keep_render_frames"])
            self.assertEqual(report["removed"], [])
            self.assertTrue(frame.is_file())

    def test_ue_map_object_path_normalizes_to_package(self) -> None:
        from scripts.harness_local_ue_runner import canonical_game_package

        self.assertEqual(canonical_game_package("/Game/Maps/Day.Day"), "/Game/Maps/Day")
        self.assertEqual(canonical_game_package("/Game/Maps/Day"), "/Game/Maps/Day")

    def test_actor_placement_cli_writes_contract_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_compile_actor_placement.py"),
                    str(ROOT / "cases" / "bowling" / "bowling_pin_chain_contact.json"),
                    "--output-dir",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["actor_count"], 6)
            self.assertTrue((Path(tmp) / "runtime_actor_placement.json").exists())
            self.assertTrue((Path(tmp) / "runtime_actor_placement_report.json").exists())
            self.assertTrue((Path(tmp) / "scene_layout.json").exists())
            self.assertTrue((Path(tmp) / "asset_resolution.json").exists())


if __name__ == "__main__":
    unittest.main()
