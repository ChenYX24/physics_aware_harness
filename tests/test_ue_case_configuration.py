from __future__ import annotations

import os
import ast
import json
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path

from harness.core.timebase import build_timebase, sample_solver_trajectory
from harness.runtime.ue_backend import build_ue_preflight_report, collision_geometry_reference_status, compile_minimal_scene_spec, requested_map_package
from scripts.harness_local_ue_runner import duration_for_case, quantize_rgb24_to_palette, timebase_for_case


ROOT = Path(__file__).resolve().parents[1]


def native_function_source(name: str) -> str:
    path = ROOT / "scripts" / "native_ue_scene.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
    return ast.get_source_segment(source, node) or ""


class UECaseConfigurationTests(unittest.TestCase):
    def test_static_mesh_material_override_covers_every_asset_slot(self) -> None:
        source = native_function_source("spawn_static_mesh")

        self.assertIn("get_num_materials", source)
        self.assertIn("for material_index in range(material_count)", source)
        self.assertIn("set_material(material_index, material)", source)

    def test_native_exr_capture_retries_a_missing_image_write(self) -> None:
        path = ROOT / "scripts" / "native_ue_scene.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "capture_render_target_to_file"
        )
        calls = []

        def export_to_disk(_target, output, _options):
            calls.append(output)
            if len(calls) == 2:
                Path(output).write_bytes(b"\x76\x2f\x31\x01")

        unreal = types.SimpleNamespace(
            DesiredImageFormat=types.SimpleNamespace(EXR="exr"),
            ImageWriteOptions=lambda **kwargs: kwargs,
            ImageWriteBlueprintLibrary=types.SimpleNamespace(export_to_disk=export_to_disk),
            RenderingLibrary=types.SimpleNamespace(export_render_target=lambda *_: None),
        )
        namespace = {
            "Path": Path,
            "unreal": unreal,
            "flush_editor_rendering": lambda *_: None,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "depth.exr"
            captured = namespace["capture_render_target_to_file"](
                {"capture_comp": types.SimpleNamespace(capture_scene=lambda: None), "render_target": object()},
                target,
                force_exr=True,
            )

            self.assertTrue(captured)
            self.assertEqual(len(calls), 2)

    def test_case_map_is_used_without_environment_override(self) -> None:
        case = {"case_id": "map_case", "scene": {"map_preference": "/Game/Maps/Chosen.Chosen"}}
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(requested_map_package(case), "/Game/Maps/Chosen.Chosen")
            self.assertEqual(compile_minimal_scene_spec(case)["map"]["requested_package"], "")
            resolved = compile_minimal_scene_spec(
                case,
                asset_resolution={
                    "scene_map": {
                        "requested_reference": "/Game/Maps/Chosen.Chosen",
                        "selection_reason": "first_reference_approved_candidate",
                        "selected_asset": {
                            "asset_id": "chosen_map",
                            "ue_path": "/Game/Maps/Chosen.Chosen",
                        },
                    }
                },
            )
            self.assertEqual(resolved["map"]["requested_package"], "/Game/Maps/Chosen.Chosen")
            self.assertEqual(resolved["map"]["selected_asset_id"], "chosen_map")

    def test_explicit_environment_map_overrides_case(self) -> None:
        case = {"scene": {"map_preference": "/Game/Maps/Chosen.Chosen"}}
        with patch.dict(os.environ, {"SIM_STUDIO_UE_MAP": "/Game/Maps/Override.Override"}, clear=True):
            self.assertEqual(requested_map_package(case), "/Game/Maps/Override.Override")

    def test_preflight_cannot_restore_an_unresolved_case_or_environment_map(self) -> None:
        case = {"case_id": "map_case", "scene": {"map_preference": "/Game/Maps/CaseOnly.CaseOnly"}}
        with patch.dict(os.environ, {"SIM_STUDIO_UE_MAP": "/Game/Maps/EnvironmentOnly.EnvironmentOnly"}, clear=True):
            report = build_ue_preflight_report("map_case", case, resolved_map_package="")

        self.assertEqual(report["raw_env"]["SIM_STUDIO_UE_MAP"], "")
        self.assertFalse(report["env_presence"]["SIM_STUDIO_UE_MAP"])

    def test_one_120hz_solver_trace_samples_to_24_and_60fps(self) -> None:
        plan_24 = build_timebase(duration_s=5.0, physics_hz=120, render_fps=24)
        plan_60 = build_timebase(duration_s=5.0, physics_hz=120, render_fps=60)
        raw = [
            {"frame": frame, "time": frame / 120, "objects": {"ball": {"position": [frame / 120, 0, 0]}}, "contacts": []}
            for frame in range(601)
        ]

        sampled_24, _ = sample_solver_trajectory(raw, plan_24)
        sampled_60, _ = sample_solver_trajectory(raw, plan_60)

        self.assertEqual((plan_24["solver_frame_count"], plan_24["canonical_frame_count"], plan_24["substeps_per_render"]), (601, 121, 5))
        self.assertEqual((plan_60["solver_frame_count"], plan_60["canonical_frame_count"], plan_60["substeps_per_render"]), (601, 301, 2))
        self.assertEqual(sampled_24[2]["source_solver_frame"], 10)
        self.assertEqual(sampled_24[2]["objects"], sampled_60[5]["objects"])

    def test_non_integer_timebase_ratio_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "integer multiple"):
            build_timebase(duration_s=1.0, physics_hz=100, render_fps=24)

    def test_render_boundary_timebase_distinguishes_simulated_steps_from_saved_states(self) -> None:
        case = {
            "expected_physics": {"duration_s": 5.0},
            "timebase": {"physics_hz": 120, "render_fps": 24},
        }

        with patch.dict(os.environ, {}, clear=True):
            timebase = timebase_for_case(case)

        self.assertEqual(timebase["physics_step_count"], 600)
        self.assertEqual(timebase["full_solver_frame_count"], 601)
        self.assertEqual(timebase["solver_frame_count"], 121)
        self.assertEqual(timebase["raw_capture_frame_count"], 121)
        self.assertEqual(timebase["canonical_frame_count"], 121)
        self.assertEqual(timebase["event_window_duration_s"], 5.0)
        self.assertEqual(timebase["post_event_tail_s"], 0.0)

    def test_probe_duration_environment_override_wins(self) -> None:
        with patch.dict(os.environ, {"SIM_STUDIO_UE_DURATION": "1.25"}, clear=True):
            self.assertEqual(duration_for_case({"expected_physics": {"duration_s": 5.0}}), 1.25)

    def test_case_duration_prefers_expected_then_scene_and_clamps(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(duration_for_case({"expected_physics": {"duration_s": 5.0}, "scene": {"duration_s": 8.0}}), 5.0)
            self.assertEqual(duration_for_case({"scene": {"duration_s": 7.0}}), 7.0)
            self.assertEqual(duration_for_case({"scene": {"duration_s": 0.25}}), 1.0)
            self.assertEqual(duration_for_case({"scene": {"duration_s": 20.0}}), 12.0)
            self.assertEqual(duration_for_case({}), 4.0)

    def test_post_event_tail_can_be_calibrated_or_disabled_per_case(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                duration_for_case({"expected_physics": {"duration_s": 3.0, "post_event_tail_s": 1.5}}),
                4.5,
            )
            self.assertEqual(
                duration_for_case({"expected_physics": {"duration_s": 3.0, "post_event_tail_s": 0.0}}),
                3.0,
            )
        with patch.dict(os.environ, {"SIM_STUDIO_UE_POST_EVENT_TAIL_S": "2.0"}, clear=True):
            self.assertEqual(duration_for_case({"expected_physics": {"duration_s": 3.0}}), 5.0)

    def test_instance_mask_quantization_closes_pixels_to_declared_palette(self) -> None:
        palette = [(0, 0, 0), (255, 0, 0), (0, 255, 0)]
        source = bytes((3, 4, 2, 240, 12, 4, 7, 230, 9, 128, 128, 0))

        quantized = quantize_rgb24_to_palette(source, palette)

        self.assertEqual(quantized, bytes((0, 0, 0, 255, 0, 0, 0, 255, 0, 255, 0, 0)))

    def test_native_depth_uses_linear_scene_depth_post_process(self) -> None:
        material_source = native_function_source("create_generated_depth_post_process_material")
        capture_source = native_function_source("export_depth_and_segmentation_frame")

        self.assertIn("MaterialExpressionSceneDepth", material_source)
        self.assertIn("MaterialExpressionMultiply", material_source)
        self.assertIn('set_editor_property("const_b", 0.0001)', material_source)
        self.assertIn("MD_POST_PROCESS", material_source)
        self.assertIn("disable_pre_exposure_scale", material_source)
        self.assertIn("SCS_FINAL_COLOR_HDR", capture_source)
        self.assertIn("add_or_update_blendable", capture_source)
        self.assertIn('"depth_post_process_warmed"', capture_source)
        self.assertGreaterEqual(capture_source.count("capture_scene()"), 1)
        self.assertIn("remove_blendable", capture_source)
        self.assertNotIn("SCS_DeviceDepth", capture_source)

    def test_native_data_pass_recaptures_frame_zero_without_advancing_physics(self) -> None:
        recapture_source = native_function_source("recapture_first_data_frame")

        self.assertIn("set_capture_view", recapture_source)
        self.assertIn("export_depth_and_segmentation_frame", recapture_source)
        self.assertNotIn("advance_", recapture_source)

    def test_domino_native_validation_accepts_formal_domino_ids_and_any_rotation_axis(self) -> None:
        path = ROOT / "scripts" / "native_ue_scene.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "validate_runtime_scene"
        )
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        runtime_scene = {
            "case_type": "bottle_domino_chain",
            "dynamic_objects": [
                {"id": f"domino_{index}", "ue5_path": "/Game/Test/Domino"}
                for index in range(3)
            ],
            "static_objects": [],
        }
        trajectory = [
            {
                "frame": frame,
                "time": float(frame),
                "objects": {
                    "domino_0": {"position": [0, 0, 0], "rotation_degrees": [0, -60, 0]},
                    "domino_1": {"position": [0, 0, 0], "rotation_degrees": [0, -60 if frame >= 1 else 0, 0]},
                    "domino_2": {"position": [0, 0, 0], "rotation_degrees": [0, -60 if frame >= 2 else 0, 0]},
                },
            }
            for frame in range(3)
        ]

        result = namespace["validate_runtime_scene"](runtime_scene, trajectory)

        self.assertTrue(result["checks"]["domino_order"]["passed"])
        self.assertEqual(result["checks"]["domino_order"]["tip_start_times"], [0.0, 1.0, 2.0])

        trajectory[1]["objects"]["domino_2"]["rotation_degrees"] = [0, -60, 0]
        simultaneous = namespace["validate_runtime_scene"](runtime_scene, trajectory)
        self.assertFalse(simultaneous["checks"]["domino_order"]["passed"])

    def test_formal_domino_case_preserves_the_requested_map(self) -> None:
        path = ROOT / "scripts" / "native_ue_scene.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "CONTROLLED_BOTTLE_STAGE" for target in node.targets)
        )
        namespace = {"os": os}
        with patch.dict(os.environ, {}, clear=True):
            exec(compile(ast.Module(body=[assignment], type_ignores=[]), str(path), "exec"), namespace)
        self.assertFalse(namespace["CONTROLLED_BOTTLE_STAGE"])
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "remove_map_actors_for_controlled_case"
        )
        namespace["runtime_lighting_controls"] = lambda _scene: {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)

        removed = namespace["remove_map_actors_for_controlled_case"](
            object(),
            {"opened": True, "name": "MarketEnvironment_Day"},
            "bottle_domino_chain",
            {"case_type": "bottle_domino_chain"},
        )

        self.assertEqual(removed, [])

        namespace["CONTROLLED_BOTTLE_STAGE"] = True
        removed = namespace["remove_map_actors_for_controlled_case"](
            object(),
            {"opened": True, "name": "MarketEnvironment_Day"},
            "bottle_domino_chain",
            {
                "case_type": "bottle_domino_chain",
                "physics_controls": {"input_mode": "initial_state_only", "state_solver": "ue_chaos"},
            },
        )

        self.assertEqual(removed, [])

    def test_reference_readiness_rejects_unverified_selected_asset_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            placement = {
                "actor_bindings": [
                    {
                        "object_id": "box",
                        "physics_critical": True,
                        "physics": {
                            "collision_enabled": True,
                            "collision_geometry_verification": "declared_unverified",
                        },
                    }
                ]
            }
            (run_dir / "runtime_actor_placement.json").write_text(json.dumps(placement), encoding="utf-8")

            status = collision_geometry_reference_status(run_dir)

            self.assertFalse(status["ready"])
            self.assertEqual(status["unverified_object_ids"], ["box"])

            placement["actor_bindings"][0]["physics"]["collision_geometry_verification"] = "runtime_controlled"
            (run_dir / "runtime_actor_placement.json").write_text(json.dumps(placement), encoding="utf-8")
            self.assertTrue(collision_geometry_reference_status(run_dir)["ready"])


if __name__ == "__main__":
    unittest.main()
