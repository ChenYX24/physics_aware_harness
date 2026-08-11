from __future__ import annotations

import ast
import copy
import json
import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path

from harness.core.case_spec import fracture_center_from_contact, fracture_response_for_energy


ROOT = Path(__file__).resolve().parents[1]


class Vector:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x, self.y, self.z = x, y, z


class HighresViewportMultiviewTests(unittest.TestCase):
    def test_precomputed_trajectory_uses_named_runtime_rotator_mapping(self) -> None:
        source = ROOT / "scripts" / "native_ue_scene.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"apply_trajectory_frame", "update_elastic_tether_visual"}
        }
        function = functions["apply_trajectory_frame"]
        tether_helper = functions["update_elastic_tether_visual"]

        class Actor:
            def __init__(self) -> None:
                self.rotation = None

            def set_actor_location(self, *_args) -> None:
                pass

            def set_actor_rotation(self, rotation, _sweep) -> None:
                self.rotation = rotation

        mapped = []

        def runtime_rotator(values):
            mapped.append(list(values))
            return ("named_pyr", *values)

        namespace = {
            "ue_vec_from_meters": lambda *_args, **_kwargs: None,
            "runtime_combined_rotation": lambda _obj, values: list(values),
            "runtime_rotator": runtime_rotator,
            "align_runtime_actor_to_pose_anchor": lambda *_args, **_kwargs: None,
        }
        exec(
            compile(ast.Module(body=[tether_helper, function], type_ignores=[]), str(source), "exec"),
            namespace,
        )
        actor = Actor()
        runtime_scene = {"dynamic_objects": [{"id": "source_wine_glass"}], "static_objects": []}
        namespace["apply_trajectory_frame"](
            {"source_wine_glass": actor, "runtime_ground_offsets": {}},
            ["source_wine_glass"],
            {
                "objects": {
                    "source_wine_glass": {
                        "position": [0.0, 0.0, 0.0],
                        "rotation_degrees": [-52.0, 0.0, 0.0],
                    }
                }
            },
            None,
            runtime_scene,
        )

        self.assertEqual(mapped, [[-52.0, 0.0, 0.0]])
        self.assertEqual(actor.rotation, ("named_pyr", -52.0, 0.0, 0.0))

    def test_geometry_collection_strain_is_gated_by_measured_incident_energy(self) -> None:
        source = ROOT / "scripts" / "native_ue_scene.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "apply_geometry_collection_fracture_response"
        )

        class Component:
            def __init__(self, velocity_cm_s: float = 0.0) -> None:
                self.velocity_cm_s = velocity_cm_s
                self.strain_calls = 0
                self.last_strain_args = None
                self.radial_impulse_calls = 0

            def is_root_broken(self):
                return False

            def get_root_index(self):
                return 0

            def get_mass(self):
                return 1.0

            def get_physics_linear_velocity(self):
                return Vector(self.velocity_cm_s, 0.0, 0.0)

            def apply_external_strain(self, *_args):
                self.strain_calls += 1
                self.last_strain_args = _args

            def add_radial_impulse(self, *_args):
                self.radial_impulse_calls += 1

        class Actor:
            def __init__(self, component: Component) -> None:
                self.component = component

            def get_component_by_class(self, _class):
                return self.component

            def get_actor_location(self):
                return Vector(0.0, 0.0, 0.0)

        namespace = {
            "unreal": types.SimpleNamespace(
                GeometryCollectionComponent=object,
                RadialImpulseFalloff=types.SimpleNamespace(RIF_LINEAR="linear"),
                Vector=Vector,
            ),
            "actor_runtime_component": lambda actor: actor.component,
            "fracture_center_from_contact": fracture_center_from_contact,
            "fracture_response_for_energy": fracture_response_for_energy,
            "vector_payload": lambda value: [value.x, value.y, value.z],
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
        runtime_scene = {
            "dynamic_objects": [
                {"id": "striker", "physics_properties": {"mass_kg": 1.0}},
                {
                    "id": "panel",
                    "params": {
                        "fracture_response": {
                            "impactor_id": "striker",
                            "external_strain": 100.0,
                            "minimum_impact_energy_j": 1.0,
                        }
                    },
                },
            ]
        }

        panel_component = Component()
        striker_component = Component(300.0)
        first_frame_contact = {"objects": ["panel", "striker"], "method": "ue_native_component_hit", "native_collision": True}
        first_frame_status = {}
        namespace["apply_geometry_collection_fracture_response"](
            {"panel": Actor(panel_component), "striker": Actor(striker_component)},
            runtime_scene,
            [first_frame_contact],
            0,
            first_frame_status,
        )
        self.assertEqual(panel_component.strain_calls, 0)
        self.assertNotIn("impact_energy_j", first_frame_contact)
        self.assertIn(
            "precontact_impactor_sample_unavailable",
            first_frame_status["geometry_collection_fracture"]["energy_checks"][0]["error"],
        )

        for velocity_cm_s, expected_calls in ((100.0, 0), (300.0, 1)):
            panel_component = Component()
            striker_component = Component(velocity_cm_s)
            actors = {"panel": Actor(panel_component), "striker": Actor(striker_component)}
            status = {}
            namespace["apply_geometry_collection_fracture_response"](
                actors,
                runtime_scene,
                [],
                4,
                status,
            )
            contact = {"objects": ["panel", "striker"], "method": "ue_native_component_hit", "native_collision": True}
            namespace["apply_geometry_collection_fracture_response"](
                actors,
                runtime_scene,
                [contact],
                5,
                status,
            )
            self.assertEqual(panel_component.strain_calls, expected_calls)
            self.assertAlmostEqual(contact["impact_energy_j"], 0.5 if velocity_cm_s == 100.0 else 4.5)
            self.assertEqual(contact["external_strain_applied"], expected_calls == 1)

        panel_component = Component()
        striker_component = Component(300.0)
        actors = {"panel": Actor(panel_component), "striker": Actor(striker_component)}
        status = {}
        namespace["apply_geometry_collection_fracture_response"](actors, runtime_scene, [], 4, status)
        striker_component.velocity_cm_s = 10.0
        contact = {"objects": ["panel", "striker"], "method": "ue_native_component_hit", "native_collision": True}
        namespace["apply_geometry_collection_fracture_response"](actors, runtime_scene, [contact], 5, status)

        self.assertEqual(panel_component.strain_calls, 1)
        self.assertAlmostEqual(contact["impact_energy_j"], 4.5)
        self.assertEqual(contact["energy_sample_frame"], 4)

        runtime_scene["dynamic_objects"][1]["params"]["fracture_response"] = {
            "impactor_id": "striker",
            "energy_response_levels": [
                {"damage_state": "cracked", "minimum_impact_energy_j": 1.0, "external_strain": 10.0},
                {
                    "damage_state": "burst",
                    "minimum_impact_energy_j": 4.0,
                    "external_strain": 100.0,
                    "radial_impulse_strength": 250.0,
                },
            ],
        }
        panel_component = Component()
        striker_component = Component(300.0)
        actors = {"panel": Actor(panel_component), "striker": Actor(striker_component)}
        status = {}
        namespace["apply_geometry_collection_fracture_response"](actors, runtime_scene, [], 4, status)
        contact = {"objects": ["panel", "striker"], "method": "ue_native_component_hit", "native_collision": True}
        namespace["apply_geometry_collection_fracture_response"](actors, runtime_scene, [contact], 5, status)

        self.assertEqual(contact["damage_state"], "burst")
        self.assertEqual(panel_component.strain_calls, 1)
        self.assertEqual(panel_component.radial_impulse_calls, 1)
        self.assertTrue(contact["radial_impulse_applied"])

        runtime_scene["dynamic_objects"][1]["params"]["fracture_response"].update(
            {"center_source": "native_contact_impact_point"}
        )
        panel_component = Component()
        striker_component = Component(300.0)
        actors = {"panel": Actor(panel_component), "striker": Actor(striker_component)}
        status = {}
        namespace["apply_geometry_collection_fracture_response"](actors, runtime_scene, [], 4, status)
        contact = {
            "objects": ["panel", "striker"],
            "method": "ue_native_component_hit",
            "native_collision": True,
            "impact_point_cm": [10.0, 20.0, 30.0],
        }
        namespace["apply_geometry_collection_fracture_response"](actors, runtime_scene, [contact], 5, status)
        self.assertEqual(panel_component.strain_calls, 1)
        strain_location = panel_component.last_strain_args[1]
        self.assertEqual((strain_location.x, strain_location.y, strain_location.z), (10.0, 20.0, 30.0))

        panel_component = Component()
        actors = {"panel": Actor(panel_component), "striker": Actor(Component(300.0))}
        status = {}
        namespace["apply_geometry_collection_fracture_response"](actors, runtime_scene, [], 4, status)
        missing_point_contact = {
            "objects": ["panel", "striker"],
            "method": "ue_native_component_hit",
            "native_collision": True,
        }
        namespace["apply_geometry_collection_fracture_response"](
            actors, runtime_scene, [missing_point_contact], 5, status
        )
        self.assertEqual(panel_component.strain_calls, 0)
        self.assertIn("panel:native_contact_impact_point_unavailable", status["geometry_collection_fracture"]["errors"])

    def test_measured_energy_is_attached_to_authoritative_solver_contact(self) -> None:
        source = ROOT / "scripts" / "native_ue_scene.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "attach_geometry_collection_fracture_events"
        )
        with tempfile.TemporaryDirectory() as tmp:
            namespace = {"FPS": 24, "OUTPUT_DIR": Path(tmp), "json": json}
            exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
            trajectory = [{"frame": 12, "contacts": [{"objects": ["steel_ball", "panel"]}]}]
            status = {
                "geometry_collection_fracture": {
                    "commands": [],
                    "break_events": [],
                    "energy_checks": [{
                        "frame": 12,
                        "target_id": "panel",
                        "impactor_id": "steel_ball",
                        "impactor_mass_kg": 4.0,
                        "impact_speed_m_s": 2.0,
                        "impact_energy_j": 8.0,
                        "minimum_impact_energy_j": 10.0,
                        "energy_model": "ue_component_poststep_incident_translational_energy",
                        "passed": False,
                        "external_strain_applied": False,
                    }],
                }
            }

            namespace["attach_geometry_collection_fracture_events"](trajectory, status)

        contact = trajectory[0]["contacts"][0]
        self.assertEqual(contact["impact_energy_j"], 8.0)
        self.assertFalse(contact["energy_gate_passed"])
        self.assertFalse(contact["external_strain_applied"])

    def test_runtime_physics_enables_ccd_for_fast_impactor(self) -> None:
        source = ROOT / "scripts" / "native_ue_scene.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        function = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "configure_runtime_physics"
        )

        class Component:
            ccd = False

            def set_mobility(self, *_): pass
            def set_collision_enabled(self, *_): pass
            def set_collision_profile_name(self, *_): pass
            def set_simulate_physics(self, *_): pass
            def set_enable_gravity(self, *_): pass
            def set_mass_override_in_kg(self, *_): pass
            def set_use_ccd(self, enabled): self.ccd = enabled

        component = Component()
        namespace = {
            "unreal": types.SimpleNamespace(
                ComponentMobility=types.SimpleNamespace(MOVABLE="movable", STATIC="static"),
                CollisionEnabled=types.SimpleNamespace(NO_COLLISION="none", QUERY_AND_PHYSICS="all"),
            ),
            "bool_control": lambda value, default: default if value is None else bool(value),
            "runtime_physics_controls": lambda *_: {},
            "actor_runtime_component": lambda _actor: component,
            "apply_runtime_physical_material": lambda *_: None,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)

        detail = namespace["configure_runtime_physics"](
            object(),
            {"id": "steel_ball", "physics_properties": {"simulate_physics": True, "use_ccd": True}},
            "dynamic",
            {"simulate_physics": True, "collision_enabled": True, "rigid_body_setup_enabled": True},
        )

        self.assertTrue(component.ccd)
        self.assertTrue(detail["use_ccd"])

    def test_instance_mask_material_enables_geometry_collection_usage(self) -> None:
        source = ROOT / "scripts" / "native_ue_scene.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "ensure_geometry_collection_material_usage"
        )
        calls = []
        material = object()
        library = types.SimpleNamespace(
            has_material_usage=lambda *_: bool(calls),
            set_material_usage=lambda *_: calls.append("set"),
            recompile_material=lambda *_: calls.append("compile"),
        )
        unreal = types.SimpleNamespace(
            MaterialUsage=types.SimpleNamespace(MATUSAGE_GEOMETRY_COLLECTIONS="gc"),
            MaterialEditingLibrary=library,
            EditorAssetLibrary=types.SimpleNamespace(save_loaded_asset=lambda *_: calls.append("save")),
        )
        namespace = {"unreal": unreal}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)

        self.assertTrue(namespace["ensure_geometry_collection_material_usage"](material))
        self.assertEqual(calls, ["set", "compile", "save"])

    def test_each_solver_frame_is_captured_once_for_every_requested_view(self) -> None:
        source = ROOT / "scripts" / "native_ue_scene.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "start_highres_viewport_capture"
        )

        callbacks = []
        summaries = []
        solver_frames = []
        data_captures = []
        exited = []
        manifest_call = {}

        class Camera:
            def __init__(self) -> None:
                self.camera_component = types.SimpleNamespace(set_editor_property=lambda *_: None)

            def set_actor_label(self, *_):
                pass

            def set_actor_location(self, *_):
                pass

            def set_actor_rotation(self, *_):
                pass

        class ScreenshotTask:
            def is_task_done(self) -> bool:
                return True

        def take_screenshot(_width, _height, path, *_args):
            Path(path).write_bytes(b"png")
            return ScreenshotTask()

        unreal = types.SimpleNamespace(
            Vector=Vector,
            CameraActor=type("CameraActor", (), {}),
            UnrealEditorSubsystem=type("UnrealEditorSubsystem", (), {}),
            EditorPythonScripting=types.SimpleNamespace(set_keep_python_script_alive=lambda *_: None),
            EditorLevelLibrary=types.SimpleNamespace(spawn_actor_from_class=lambda *_: Camera()),
            AutomationLibrary=types.SimpleNamespace(take_high_res_screenshot=take_screenshot),
            ComparisonTolerance=types.SimpleNamespace(LOW=0),
            get_editor_subsystem=lambda *_: types.SimpleNamespace(set_level_viewport_camera_info=lambda *_: None),
            register_slate_post_tick_callback=lambda callback: callbacks.append(callback) or callback,
            unregister_slate_post_tick_callback=lambda *_: None,
        )

        views = [
            {
                "id": "top_down",
                "view_id": "top_down",
                "suffix": "_top_down",
                "label": "Top",
                "camera_mode": "fixed",
                "location": Vector(0, -100, 50),
                "target": Vector(0, 0, 0),
                "fov": 50.0,
            },
            {
                "id": "event_closeup",
                "view_id": "event_closeup",
                "suffix": "_event_closeup",
                "label": "Close-up",
                "camera_mode": "fixed",
                "location": Vector(-100, 0, 50),
                "target": Vector(0, 0, 0),
                "fov": 55.0,
            },
        ]

        def write_manifest(_output_dir, encode_results, camera_tracks, *_args):
            manifest_call["view_ids"] = [item["view_id"] for item in encode_results]
            manifest_call["camera_tracks"] = copy.deepcopy(camera_tracks)
            return {"path": "render_pass_manifest.json", "passes": {}}

        reentered_data_capture = []
        reentered_frame_settle = []

        def export_data(_actors, _scene, view_id, frame_index, _dirs):
            data_captures.append((view_id, frame_index))
            if not reentered_data_capture:
                reentered_data_capture.append(True)
                callbacks[0](1 / 24)
            return {"view_id": view_id, "frame": frame_index}

        def settle_viewport(*_args):
            if data_captures and not reentered_frame_settle:
                reentered_frame_settle.append(True)
                callbacks[0](1 / 24)
            return {"ticks": 0}

        namespace = {
            "Path": Path,
            "time": time,
            "json": json,
            "shutil": shutil,
            "unreal": unreal,
            "OUTPUT_DIR": None,
            "WIDTH": 64,
            "HEIGHT": 36,
            "FPS": 24,
            "DURATION": 2 / 24,
            "KEEP_RENDER_FRAMES": True,
            "RENDER_DATA_PASSES": True,
            "RENDER_WARMUP_FRAMES": 0,
            "RENDER_VIEWPORT_SETTLE_SECONDS": 0.0,
            "RENDER_FIRST_FRAME_STABILITY_SAMPLES": 0,
            "RENDER_PER_FRAME_SETTLE_TICKS": 0,
            "RENDER_SCREENSHOT_STABLE_TICKS": 0,
            "RENDER_FIRST_FRAME_STABILITY_SIZE_TOLERANCE": 0,
            "ASSET_MANIFEST_DATA": {},
            "GITLAB_ONLY_ASSETS": [],
            "ASSET_SELECTION_METADATA": {},
            "STUDIO_SCENE_SPEC": {},
            "STUDIO_RUNTIME_SCENE": {},
            "SCENE_SPEC": "scene.json",
            "SCENE_RUNTIME_JSON": "runtime.json",
            "camera_view_specs": lambda *_: views,
            "initialize_data_pass_dirs": lambda *_: {},
            "runtime_timebase": lambda *_: {"fps": 24, "frame_count": 2},
            "physics_capture_enabled": lambda *_: True,
            "start_editor_physics_capture": lambda *_: {"enabled": True},
            "runtime_physics_controls": lambda *_: {},
            "int_control": lambda value, default, *_: default if value is None else int(value),
            "configure_clean_highres_viewport": lambda: [],
            "settle_highres_viewport": settle_viewport,
            "write_summary": lambda summary: summaries.append(copy.deepcopy(summary)),
            "analytic_contact_solver_enabled": lambda *_: True,
            "analytic_solver_source": lambda *_: "analytic_contact_solver",
            "start_cpp_runtime_driver": lambda *_: False,
            "apply_initial_physics_impulses": lambda *_: None,
            "stop_cpp_runtime_driver": lambda *_: ([], []),
            "stop_editor_physics_capture": lambda *_: None,
            "attach_geometry_collection_fracture_events": lambda *_: [],
            "merge_scripted_runner_trajectory": lambda trajectory, *_: trajectory,
            "validate_runtime_scene": lambda *_: {"valid": True},
            "sampled_frame_hashes": lambda path, *_: {"dir": Path(path).name, "unique": 2},
            "encode_video": lambda frames, preview, *_: {"encoded": True, "frames_dir": Path(frames).name, "preview": str(preview)},
            "write_render_pass_manifest": write_manifest,
            "request_editor_exit": lambda: exited.append(True),
            "rebind_runtime_actors_to_simulation_world": lambda _actors, _scene, status, **_kwargs: status.update({"game_world_count": 1, "rebound_actor_ids": ["cue_ball"]}),
            "set_simulation_world_paused": lambda *_: None,
            "reset_runtime_actors_to_initial_state": lambda *_: None,
            "apply_trajectory_frame": lambda _actors, _ids, frame, *_: solver_frames.append(frame["frame"]),
            "apply_runtime_animation_segments": lambda *_: None,
            "apply_delayed_release_projectiles": lambda *_: None,
            "advance_cpp_runtime_driver": lambda *_: 0,
            "advance_physics_capture": lambda *_: None,
            "fixed_physics_step_s": lambda *_: 0.0,
            "record_physics_transform_frame": lambda *_: ({}, []),
            "apply_surface_mesh_sequence_replay": lambda *_: None,
            "sync_runtime_visuals": lambda *_: None,
            "camera_view_for_frame": lambda view, *_: view,
            "set_capture_view": lambda *_: None,
            "export_depth_and_segmentation_frame": export_data,
            "frame_time_s": lambda frame, *_: frame["frame"] / 24,
            "vector_payload": lambda value: [value.x, value.y, value.z],
            "look_at_rotation": lambda *_: None,
            "file_fingerprint": lambda *_: {},
            "print": lambda *_args, **_kwargs: None,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)

        trajectory = [
            {"frame": 0, "time": 0.0, "objects": {"cue_ball": {}}, "contacts": []},
            {"frame": 1, "time": 1 / 24, "objects": {"cue_ball": {}}, "contacts": []},
        ]
        runtime_scene = {"dynamic_objects": [{"id": "cue_ball"}]}
        actors = {"scene_origin": [0, 0, 0], "physics_controls": {}, "lighting": {}}

        with tempfile.TemporaryDirectory() as tmp:
            namespace["OUTPUT_DIR"] = Path(tmp)
            frames_dir = Path(tmp) / "frames"
            namespace["start_highres_viewport_capture"](
                actors,
                runtime_scene,
                trajectory,
                {"valid": True},
                0.0,
                time.perf_counter(),
                frames_dir,
            )
            for _ in range(30):
                if exited:
                    break
                callbacks[0](1 / 24)

            self.assertTrue(exited, summaries[-1].get("errors") if summaries else "capture did not finish")
            summary = summaries[-1]
            self.assertEqual([item["view_id"] for item in summary["multi_view"]], ["top_down", "event_closeup"])
            self.assertEqual(
                [Path(item["preview"]).name for item in summary["multi_view"]],
                ["preview.mp4", "preview_event_closeup.mp4"],
            )
            self.assertEqual(solver_frames, [0, 1])
            self.assertEqual(
                data_captures,
                [("top_down", 0), ("event_closeup", 0), ("top_down", 1), ("event_closeup", 1)],
            )
            self.assertEqual(manifest_call["view_ids"], ["top_down", "event_closeup"])
            self.assertEqual(list(manifest_call["camera_tracks"]), ["top_down", "event_closeup"])
            self.assertEqual([len(track) for track in manifest_call["camera_tracks"].values()], [2, 2])
            camera_payload = json.loads((Path(tmp) / "camera_trajectories.json").read_text(encoding="utf-8"))
            self.assertEqual([view["view_id"] for view in camera_payload["views"]], ["top_down", "event_closeup"])
            self.assertEqual([len(view["frames"]) for view in camera_payload["views"]], [2, 2])
            self.assertTrue((frames_dir / "frame_0001.png").exists())
            self.assertTrue((Path(tmp) / "frames_event_closeup" / "frame_0001.png").exists())


if __name__ == "__main__":
    unittest.main()
