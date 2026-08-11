from __future__ import annotations

import ast
import math
import os
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Vector:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x, self.y, self.z = x, y, z

    def __add__(self, other: "Vector") -> "Vector":
        return Vector(self.x + other.x, self.y + other.y, self.z + other.z)


def load_camera_functions() -> dict:
    path = ROOT / "scripts" / "native_ue_scene.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    wanted = {
        "apply_explicit_runtime_visual_appearance",
        "actor_runtime_component",
        "bool_control",
        "camera_view_for_frame",
        "delayed_release_projectile_objects",
        "fixed_physics_step_s",
        "float_control",
        "int_control",
        "interpolate_values",
        "initialize_gasp_locomotion",
        "projectile_hold_position",
        "released_body_gravity_enabled",
        "runtime_physics_controls",
        "runtime_object_color",
        "runtime_gasp_objects",
        "runtime_subject_delta_cm",
        "runtime_vec3",
        "rebind_runtime_actors_to_simulation_world",
        "set_delayed_release_collision_enabled",
    }
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {
        "CANONICAL_MULTI_VIEW": True,
        "CHAOS_RIGID_BODY_SETUP": True,
        "CHAOS_SIMULATION_ENABLED": False,
        "math": math,
        "os": os,
        "unreal": types.SimpleNamespace(
            Vector=Vector,
            LinearColor=lambda *values: tuple(values),
            CollisionEnabled=types.SimpleNamespace(
                NO_COLLISION="no_collision",
                QUERY_AND_PHYSICS="query_and_physics",
            ),
        ),
        "set_actor_material": lambda actor, material: actor.events.append(("material", material)),
        "set_actor_color": lambda actor, color: actor.events.append(("color", color)),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class DynamicCameraTruthTests(unittest.TestCase):
    def test_explicit_color_is_applied_to_the_visible_runtime_proxy_only_when_declared(self) -> None:
        apply_appearance = load_camera_functions()["apply_explicit_runtime_visual_appearance"]

        colored = types.SimpleNamespace(events=[])
        self.assertTrue(
            apply_appearance(
                {"behavior": "llm_rigid_body", "params": {"color_rgb": [1.0, 0.2, 0.05]}},
                colored,
                {"runtime_air_sphere": "sphere_material"},
            )
        )
        self.assertEqual(colored.events[0], ("material", "sphere_material"))
        self.assertEqual(colored.events[1], ("color", (1.0, 0.2, 0.05, 1.0)))

        authored = types.SimpleNamespace(events=[])
        self.assertFalse(apply_appearance({"params": {"preserve_material": True}}, authored, {}))
        self.assertEqual(authored.events, [])

    def test_delayed_release_defaults_null_gravity_to_enabled(self) -> None:
        released_body_gravity_enabled = load_camera_functions()["released_body_gravity_enabled"]

        self.assertTrue(released_body_gravity_enabled({}))
        self.assertTrue(released_body_gravity_enabled({"enable_gravity": None}))
        self.assertTrue(released_body_gravity_enabled({"enable_gravity": True}))
        self.assertFalse(released_body_gravity_enabled({"enable_gravity": False}))
        self.assertFalse(released_body_gravity_enabled({}, {"gravity_enabled": False}))
        self.assertTrue(released_body_gravity_enabled({"enable_gravity": True}, {"gravity_enabled": False}))

    def test_scheduled_release_disables_collision_while_held_and_restores_it_on_release(self) -> None:
        set_collision = load_camera_functions()["set_delayed_release_collision_enabled"]

        class Component:
            def __init__(self) -> None:
                self.values = []

            def set_collision_enabled(self, value) -> None:
                self.values.append(value)

        component = Component()
        set_collision(component, False)
        set_collision(component, True)

        self.assertEqual(component.values, ["no_collision", "query_and_physics"])

    def test_generic_rigid_body_can_declare_a_delayed_release_initialization(self) -> None:
        functions = load_camera_functions()
        scheduled = {
            "id": "steel_ball",
            "behavior": "llm_rigid_body",
            "initial_position_m": [0.0, -1.4, 0.1],
            "params": {"release_time_s": 1.0},
        }
        scene = {
            "dynamic_objects": [
                scheduled,
                {"id": "free_ball", "behavior": "llm_rigid_body", "params": {}},
            ]
        }

        selected = functions["delayed_release_projectile_objects"](scene)
        held = functions["projectile_hold_position"](scheduled, {"objects": {}})

        self.assertEqual([row["id"] for row in selected], ["steel_ball"])
        self.assertEqual(held, [0.0, -1.4, 0.1])

    def test_solver_output_replay_is_render_cache_not_fallback(self) -> None:
        runtime_physics_controls = load_camera_functions()["runtime_physics_controls"]

        controls = runtime_physics_controls({
            "physics_controls": {
                "simulate_physics": False,
                "simulation_driver": "ue_chaos_output_replay",
                "runtime_driver_backend": "precomputed_trajectory",
                "trajectory_source": "adp_cpp_runtime_driver",
            }
        })

        self.assertFalse(controls["deterministic_replay_fallback"])
        self.assertEqual(controls["replay_kind"], "solver_output_render_cache")

    def test_fixed_step_captures_declared_initial_state_before_advancing(self) -> None:
        fixed_physics_step_s = load_camera_functions()["fixed_physics_step_s"]

        self.assertEqual(fixed_physics_step_s(0, 60), 0.0)
        self.assertAlmostEqual(fixed_physics_step_s(1, 60), 1.0 / 60.0)

    def test_object_bound_camera_moves_with_first_dynamic_subject(self) -> None:
        camera_view_for_frame = load_camera_functions()["camera_view_for_frame"]
        view = {"camera_mode": "object_bound", "location": Vector(0, 0, 10), "target": Vector(5, 0, 0), "fov": 50}
        runtime_scene = {
            "dynamic_objects": [{"id": "cue_ball"}],
            "precomputed_trajectory": [
                {"frame": 0, "objects": {"cue_ball": {"position": [0.0, 0.0, 0.0]}}},
                {"frame": 1, "objects": {"cue_ball": {"position": [1.0, 0.0, 0.0]}}},
            ],
        }

        frame = camera_view_for_frame(view, runtime_scene, 1, 2)

        self.assertEqual((frame["location"].x, frame["target"].x), (100.0, 105.0))

    def test_live_camera_reads_solver_output_instead_of_requiring_replay_input(self) -> None:
        camera_view_for_frame = load_camera_functions()["camera_view_for_frame"]
        view = {"camera_mode": "object_bound", "location": Vector(0, 0, 10), "target": Vector(5, 0, 0), "fov": 50}
        runtime_scene = {"dynamic_objects": [{"id": "cue_ball"}], "precomputed_trajectory": []}
        solver_trajectory = [
            {"frame": 0, "objects": {"cue_ball": {"position": [0.0, 0.0, 0.0]}}},
            {"frame": 1, "objects": {"cue_ball": {"position": [1.0, 0.0, 0.0]}}},
        ]

        frame = camera_view_for_frame(view, runtime_scene, 1, 2, solver_trajectory)

        self.assertEqual((frame["location"].x, frame["target"].x), (100.0, 105.0))

    def test_event_camera_damps_subject_follow_to_keep_collision_region_in_frame(self) -> None:
        camera_view_for_frame = load_camera_functions()["camera_view_for_frame"]
        view = {
            "camera_mode": "trajectory",
            "location": Vector(0, 0, 10),
            "target": Vector(5, 0, 0),
            "fov": 50,
            "subject_follow_location_gain": 0.2,
            "subject_follow_target_gain": 0.1,
        }
        runtime_scene = {
            "dynamic_objects": [{"id": "steel_ball"}],
            "precomputed_trajectory": [
                {"frame": 0, "objects": {"steel_ball": {"position": [0.0, 0.0, 0.0]}}},
                {"frame": 1, "objects": {"steel_ball": {"position": [1.0, 0.0, 0.0]}}},
            ],
        }

        frame = camera_view_for_frame(view, runtime_scene, 1, 2)

        self.assertEqual((frame["location"].x, frame["target"].x), (100.0, 15.0))

    def test_pie_world_rebinds_visual_proxy_with_its_physics_actor(self) -> None:
        functions = load_camera_functions()
        rebind = functions["rebind_runtime_actors_to_simulation_world"]

        class Actor:
            def __init__(self, label: str) -> None:
                self.label = label
                self.capture_component2d = types.SimpleNamespace(
                    get_editor_property=lambda name: "pie_render_target" if name == "texture_target" else None
                ) if label == "native_phenomena_demo_capture_camera" else None

            def get_actor_label(self) -> str:
                return self.label

        world = object()
        pie_physics = Actor("native_phenomena_demo_cue_ball")
        pie_visual = Actor("native_phenomena_visual_cue_ball")
        pie_capture = Actor("native_phenomena_demo_capture_camera")
        world_actors = [pie_physics, pie_visual, pie_capture]
        functions["time"] = types.SimpleNamespace(perf_counter=lambda: 0.0, sleep=lambda _: None)
        functions["unreal"] = types.SimpleNamespace(
            Actor=Actor,
            LevelEditorSubsystem=type("LevelEditorSubsystem", (), {}),
            UnrealEditorSubsystem=type("UnrealEditorSubsystem", (), {}),
            StaticMeshActor=type("StaticMeshActor", (), {}),
            EditorLevelLibrary=types.SimpleNamespace(
                get_game_world=lambda: world,
                get_pie_worlds=lambda _: [world],
            ),
            GameplayStatics=types.SimpleNamespace(
                get_all_actors_of_class=lambda *_: world_actors,
                set_global_time_dilation=lambda *_: None,
            ),
            get_editor_subsystem=lambda subsystem: types.SimpleNamespace(
                is_in_play_in_editor=lambda: True,
                get_game_world=lambda: world,
            ),
        )
        editor_visual = Actor("native_phenomena_visual_cue_ball")
        actors = {
            "cue_ball": Actor("native_phenomena_demo_cue_ball"),
            "visual_actors": {"cue_ball": editor_visual},
            "capture": Actor("native_phenomena_demo_capture_camera"),
            "physics_controls": {"physics_time_dilation": 1.0},
        }
        status = {"enabled": True}
        scene = {"dynamic_objects": [{"id": "cue_ball"}], "static_objects": []}

        rebind(actors, scene, status, timeout_s=0.0)

        self.assertIs(actors["cue_ball"], pie_physics)
        self.assertIs(actors["visual_actors"]["cue_ball"], pie_visual)
        self.assertIs(actors["capture"], pie_capture)
        self.assertIs(actors["capture_comp"], pie_capture.capture_component2d)
        self.assertIs(actors["capture_world"], world)
        self.assertEqual(actors["render_target"], "pie_render_target")
        self.assertTrue(status["rebound_capture_actor"])
        self.assertEqual(status["rebound_visual_actor_ids"], ["cue_ball"])

        world_actors.remove(pie_visual)
        status = {"enabled": True}
        actors["visual_actors"]["cue_ball"] = editor_visual
        rebind(actors, scene, status, timeout_s=0.0)

        self.assertEqual(status["missing_visual_actor_ids"], ["cue_ball"])
        self.assertFalse(status["visual_rebind_complete"])
        self.assertIn("game_world_visual_rebind:missing:cue_ball", status["errors"])


if __name__ == "__main__":
    unittest.main()
