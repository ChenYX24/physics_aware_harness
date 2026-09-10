from __future__ import annotations

import ast
import math
import time
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "native_ue_scene.py"


def load_functions(*names: str, extra: dict | None = None) -> dict:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {
        "ARTICULATED_ANIMATION_SOURCE_TAG": "HarnessArticulatedAnimationSource",
        "math": math,
        "time": time,
    }
    namespace.update(extra or {})
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class Vector:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x, self.y, self.z = x, y, z


class Component:
    def __init__(self, name: str) -> None:
        self.name = name
        self.visible = None
        self.hidden = None

    def component_has_tag(self, _tag) -> bool:
        return False

    def get_name(self) -> str:
        return self.name

    def set_visibility(self, value: bool, propagate: bool) -> None:
        self.visible = (value, propagate)

    def set_hidden_in_game(self, value: bool, propagate: bool) -> None:
        self.hidden = (value, propagate)


class Movement:
    def __init__(self) -> None:
        self.velocity = Vector(4.0, 5.0, 6.0)
        self.stopped = False
        self.cleared = False
        self.mode = None

    def stop_movement_immediately(self) -> None:
        self.stopped = True

    def clear_accumulated_forces(self) -> None:
        self.cleared = True

    def set_movement_mode(self, mode) -> None:
        self.mode = mode


class Character:
    def __init__(self) -> None:
        self.mesh = Component("CharacterMesh0")
        self.helper = Component("OrientationSphere")
        self.capsule = Component("CapsuleComponent")
        self.movement = Movement()
        self.stopped_jumping = False

    def get_component_by_class(self, cls):
        return self.movement if cls == "movement" else None

    def get_components_by_class(self, cls):
        if cls == "skeletal":
            return [self.mesh]
        if cls == "primitive":
            return [self.capsule, self.helper, self.mesh]
        return []

    def stop_jumping(self) -> None:
        self.stopped_jumping = True


def unreal_namespace():
    return types.SimpleNamespace(
        ActorComponent="actor_component",
        CharacterMovementComponent="movement",
        SkeletalMeshComponent="skeletal",
        PrimitiveComponent="primitive",
        MovementMode=types.SimpleNamespace(MOVE_WALKING="walking"),
        Name=lambda value: value,
        Vector=Vector,
    )


class GaspCharacterRuntimeTests(unittest.TestCase):
    def test_gasp_head_look_does_not_enter_fixed_control_rig_path(self) -> None:
        apply_layers = load_functions(
            "apply_articulated_control_layers",
            extra={
                "sample_articulated_body_contract": lambda *_args: (_ for _ in ()).throw(
                    AssertionError("fixed Control Rig sampling must not run for GASP")
                )
            },
        )["apply_articulated_control_layers"]
        obj = {
            "id": "character",
            "params": {
                "articulated_body": {
                    "movement_control": {"type": "character_movement"},
                    "head_look_target": {"keyframes": []},
                }
            },
        }

        self.assertIsNone(apply_layers({"character": object()}, obj, 0.0))

    def test_gasp_initialization_uses_finite_character_movement_turn_rate(self) -> None:
        properties = {}
        movement = types.SimpleNamespace(
            set_editor_property=lambda name, value: properties.__setitem__(name, value)
        )
        actor = types.SimpleNamespace(
            get_component_by_class=lambda _class: movement,
            get_actor_location=lambda: Vector(0.0, 0.0, 0.0),
        )
        controller = types.SimpleNamespace(possess=lambda _actor: None)
        initialize = load_functions(
            "runtime_gasp_objects",
            "initialize_gasp_locomotion",
            extra={
                "runtime_rotator": lambda value: value,
                "sanitize_articulated_character_rendering": lambda _actor: {},
                "sanitize_articulated_character_cameras": lambda _actor, _controller: {},
                "reset_gasp_idle_state": lambda _actor: {},
                "unreal": types.SimpleNamespace(
                    CharacterMovementComponent="movement",
                    GameplayStatics=types.SimpleNamespace(get_player_controller=lambda _world, _index: controller),
                ),
            },
        )["initialize_gasp_locomotion"]
        actors = {"character": actor, "physics_game_world": object()}
        runtime_scene = {
            "dynamic_objects": [{
                "id": "character",
                "behavior": "articulated_character",
                "params": {"articulated_body": {"movement_control": {
                    "max_speed_m_s": 2.0,
                    "max_acceleration_m_s2": 4.0,
                    "max_turn_rate_deg_s": 90.0,
                }}},
            }]
        }

        self.assertTrue(initialize(actors, runtime_scene, {}))
        self.assertEqual(properties["rotation_rate"], [0.0, 90.0, 0.0])

    def test_grounding_places_capsule_bottom_on_support_with_component_offset(self) -> None:
        grounded_z = load_functions("grounded_actor_z_cm")["grounded_actor_z_cm"]

        actor_z = grounded_z(0.0, 86.0, 102.15, 100.0)

        self.assertAlmostEqual(actor_z, 83.85)
        self.assertAlmostEqual(actor_z + 2.15 - 86.0, 0.0)

    def test_turn_in_place_intent_tracks_stationary_facing_change(self) -> None:
        calls = []
        rotations = []
        movement_properties = {}
        apply = load_functions(
            "apply_gasp_locomotion_frame",
            extra={
                "sample_articulated_body_contract": lambda _contract, _time: {
                    "movement_control": {
                        "target_velocity_m_s": [0.0, 0.0, 0.0],
                        "facing_yaw_deg": 90.0,
                        "crouched": False,
                    }
                },
                "gasp_visible_root_yaw": lambda _actor: -90.0,
                "set_gasp_aim_intent": lambda actor, enabled: calls.append((actor, enabled)),
                "runtime_rotator": lambda value: value,
                "unreal": unreal_namespace(),
            },
        )["apply_gasp_locomotion_frame"]
        movement = types.SimpleNamespace(
            set_editor_property=lambda name, value: movement_properties.__setitem__(name, value)
        )
        character = types.SimpleNamespace(
            stop_jumping=lambda: None,
            un_crouch=lambda _client: None,
            get_actor_rotation=lambda: types.SimpleNamespace(yaw=0.0),
            get_component_by_class=lambda _class: movement,
        )
        controller = types.SimpleNamespace(set_control_rotation=lambda rotation: rotations.append(rotation))
        actors = {
            "character": character,
            "gasp_locomotion": {
                "actor_id": "character",
                "controller": controller,
                "movement_control": {"max_speed_m_s": 2.0, "events": []},
                "last_frame": None,
                "turn_in_place_active": False,
                "root_facing_offset_deg": -90.0,
                "fired_events": set(),
                "crouched": False,
                "jump_pressed": False,
                "detail": {},
            },
        }

        applied = apply(actors, {"id": "character", "params": {}}, {"frame": 1, "time": 1.0})

        self.assertTrue(applied)
        self.assertEqual(calls, [(character, True)])
        self.assertEqual(rotations, [[0.0, 90.0, 0.0]])
        self.assertTrue(actors["gasp_locomotion"]["turn_in_place_active"])
        self.assertTrue(movement_properties["use_controller_desired_rotation"])

    def test_runtime_fixed_frame_step_uses_render_delta(self) -> None:
        calls = []
        configure = load_functions(
            "configure_runtime_fixed_frame_step",
            extra={
                "runtime_timebase": lambda _scene: {"render_dt_s": 1.0 / 24.0},
                "unreal": types.SimpleNamespace(
                    ADPPhysicsRuntimeLibrary=types.SimpleNamespace(
                        configure_fixed_frame_step=lambda value: calls.append(value) or True
                    )
                ),
            },
        )["configure_runtime_fixed_frame_step"]
        status = {}

        self.assertTrue(configure({}, status))
        self.assertEqual(calls, [1.0 / 24.0])
        self.assertEqual(status["fixed_frame_step"], {
            "enabled": True,
            "fixed_delta_s": 0.041666667,
        })

    def test_target_velocity_scales_native_movement_input(self) -> None:
        movement_properties = {}
        inputs = []
        apply = load_functions(
            "apply_gasp_locomotion_frame",
            extra={
                "sample_articulated_body_contract": lambda _contract, _time: {
                    "movement_control": {
                        "target_velocity_m_s": [0.02, 0.0, 0.0],
                        "facing_yaw_deg": 0.0,
                        "crouched": False,
                    }
                },
                "gasp_visible_root_yaw": lambda _actor: -90.0,
                "set_gasp_aim_intent": lambda _actor, _enabled: None,
                "runtime_rotator": lambda value: value,
                "unreal": unreal_namespace(),
            },
        )["apply_gasp_locomotion_frame"]
        movement = types.SimpleNamespace(
            set_editor_property=lambda name, value: movement_properties.__setitem__(name, value)
        )
        character = types.SimpleNamespace(
            stop_jumping=lambda: None,
            un_crouch=lambda _client: None,
            get_actor_rotation=lambda: types.SimpleNamespace(yaw=0.0),
            get_component_by_class=lambda _class: movement,
            add_movement_input=lambda direction, scale, force: inputs.append((direction, scale, force)),
        )
        actors = {
            "character": character,
            "gasp_locomotion": {
                "actor_id": "character",
                "controller": types.SimpleNamespace(set_control_rotation=lambda _rotation: None),
                "movement_control": {"max_speed_m_s": 2.0, "events": []},
                "last_frame": None,
                "turn_in_place_active": False,
                "root_facing_offset_deg": -90.0,
                "fired_events": set(),
                "crouched": False,
                "jump_pressed": False,
                "detail": {},
            },
        }

        self.assertTrue(apply(actors, {"id": "character", "params": {}}, {"frame": 1, "time": 1.0}))
        self.assertNotIn("max_walk_speed", movement_properties)
        self.assertNotIn("max_walk_speed_crouched", movement_properties)
        self.assertEqual(inputs[0][1:], (0.01, True))

    def test_world_look_target_holds_body_facing_and_drives_controller_aim(self) -> None:
        movement_properties = {}
        controller_rotations = []
        aim_states = []
        apply = load_functions(
            "apply_gasp_locomotion_frame",
            extra={
                "sample_articulated_body_contract": lambda _contract, _time: {
                    "movement_control": {
                        "target_velocity_m_s": [0.0, 0.0, 0.0],
                        "facing_yaw_deg": 0.0,
                        "crouched": False,
                    },
                    "head_look_target": {
                        "position_m": [0.0, -2.0, 1.6],
                        "weight": 1.0,
                    },
                },
                "gasp_world_look_rotation": lambda _actor, _target, _origin: (-90.0, 5.0),
                "gasp_visible_root_yaw": lambda _actor: -90.0,
                "set_gasp_aim_intent": lambda actor, enabled: aim_states.append((actor, enabled)),
                "runtime_rotator": lambda value: value,
                "unreal": unreal_namespace(),
            },
        )["apply_gasp_locomotion_frame"]
        movement = types.SimpleNamespace(
            set_editor_property=lambda name, value: movement_properties.__setitem__(name, value)
        )
        character = types.SimpleNamespace(
            stop_jumping=lambda: None,
            un_crouch=lambda _client: None,
            get_actor_rotation=lambda: types.SimpleNamespace(pitch=0.0, yaw=0.0, roll=0.0),
            get_component_by_class=lambda _class: movement,
        )
        actors = {
            "character": character,
            "scene_origin": Vector(0.0, 0.0, 0.0),
            "gasp_locomotion": {
                "actor_id": "character",
                "controller": types.SimpleNamespace(
                    set_control_rotation=lambda rotation: controller_rotations.append(rotation)
                ),
                "movement_control": {
                    "max_speed_m_s": 2.0,
                    "max_turn_rate_deg_s": 60.0,
                    "events": [],
                },
                "last_frame": None,
                "turn_in_place_active": False,
                "root_facing_offset_deg": -90.0,
                "fired_events": set(),
                "crouched": False,
                "jump_pressed": False,
                "detail": {},
            },
        }

        self.assertTrue(apply(actors, {"id": "character", "params": {}}, {"frame": 24, "time": 1.0}))
        self.assertFalse(movement_properties["use_controller_desired_rotation"])
        self.assertFalse(movement_properties["orient_rotation_to_movement"])
        self.assertEqual(controller_rotations, [[5.0, -90.0, 0.0]])
        self.assertEqual(aim_states, [(character, True)])

    def test_world_look_with_motion_uses_standard_movement_orientation(self) -> None:
        movement_properties = {}
        apply = load_functions(
            "apply_gasp_locomotion_frame",
            extra={
                "sample_articulated_body_contract": lambda _contract, _time: {
                    "movement_control": {
                        "target_velocity_m_s": [0.0, -0.2, 0.0],
                        "facing_yaw_deg": -90.0,
                        "crouched": False,
                    },
                    "head_look_target": {
                        "position_m": [-2.0, 0.0, 1.6],
                        "weight": 1.0,
                    },
                },
                "gasp_world_look_rotation": lambda _actor, _target, _origin: (-180.0, 0.0),
                "gasp_visible_root_yaw": lambda _actor: -90.0,
                "set_gasp_aim_intent": lambda _actor, _enabled: None,
                "runtime_rotator": lambda value: value,
                "unreal": unreal_namespace(),
            },
        )["apply_gasp_locomotion_frame"]
        movement = types.SimpleNamespace(
            set_editor_property=lambda name, value: movement_properties.__setitem__(name, value)
        )
        character = types.SimpleNamespace(
            stop_jumping=lambda: None,
            un_crouch=lambda _client: None,
            get_actor_rotation=lambda: types.SimpleNamespace(pitch=0.0, yaw=0.0, roll=0.0),
            get_component_by_class=lambda _class: movement,
            add_movement_input=lambda *_args: None,
        )
        actors = {
            "character": character,
            "scene_origin": Vector(0.0, 0.0, 0.0),
            "gasp_locomotion": {
                "actor_id": "character",
                "controller": types.SimpleNamespace(set_control_rotation=lambda _rotation: None),
                "movement_control": {"max_speed_m_s": 2.0, "events": []},
                "last_frame": None,
                "turn_in_place_active": False,
                "root_facing_offset_deg": -90.0,
                "fired_events": set(),
                "crouched": False,
                "jump_pressed": False,
                "detail": {},
            },
        }

        self.assertTrue(apply(actors, {"id": "character", "params": {}}, {"frame": 24, "time": 1.0}))
        self.assertFalse(movement_properties["use_controller_desired_rotation"])
        self.assertTrue(movement_properties["orient_rotation_to_movement"])

    def test_turn_in_place_intent_releases_at_target_facing(self) -> None:
        calls = []
        apply = load_functions(
            "apply_gasp_locomotion_frame",
            extra={
                "sample_articulated_body_contract": lambda _contract, _time: {
                    "movement_control": {
                        "target_velocity_m_s": [0.0, 0.0, 0.0],
                        "facing_yaw_deg": -180.0,
                        "crouched": False,
                    }
                },
                "gasp_visible_root_yaw": lambda _actor: 90.0,
                "set_gasp_aim_intent": lambda actor, enabled: calls.append((actor, enabled)),
                "runtime_rotator": lambda value: value,
                "unreal": unreal_namespace(),
            },
        )["apply_gasp_locomotion_frame"]
        movement = types.SimpleNamespace(set_editor_property=lambda _name, _value: None)
        character = types.SimpleNamespace(
            stop_jumping=lambda: None,
            un_crouch=lambda _client: None,
            get_actor_rotation=lambda: types.SimpleNamespace(yaw=180.0),
            get_component_by_class=lambda _class: movement,
        )
        actors = {
            "character": character,
            "gasp_locomotion": {
                "actor_id": "character",
                "controller": types.SimpleNamespace(set_control_rotation=lambda _rotation: None),
                "movement_control": {"max_speed_m_s": 2.0, "events": []},
                "last_frame": None,
                "turn_in_place_active": True,
                "root_facing_offset_deg": -90.0,
                "fired_events": set(),
                "crouched": False,
                "jump_pressed": False,
                "detail": {},
            },
        }

        applied = apply(actors, {"id": "character", "params": {}}, {"frame": 1, "time": 1.0})

        self.assertTrue(applied)
        self.assertEqual(calls, [(character, False)])
        self.assertFalse(actors["gasp_locomotion"]["turn_in_place_active"])

    def test_turn_in_place_stays_active_until_visible_root_reaches_facing(self) -> None:
        calls = []
        apply = load_functions(
            "apply_gasp_locomotion_frame",
            extra={
                "sample_articulated_body_contract": lambda _contract, _time: {
                    "movement_control": {
                        "target_velocity_m_s": [0.0, 0.0, 0.0],
                        "facing_yaw_deg": -180.0,
                        "crouched": False,
                    }
                },
                "gasp_visible_root_yaw": lambda _actor: 135.0,
                "set_gasp_aim_intent": lambda actor, enabled: calls.append((actor, enabled)),
                "runtime_rotator": lambda value: value,
                "unreal": unreal_namespace(),
            },
        )["apply_gasp_locomotion_frame"]
        movement = types.SimpleNamespace(set_editor_property=lambda _name, _value: None)
        character = types.SimpleNamespace(
            stop_jumping=lambda: None,
            un_crouch=lambda _client: None,
            get_actor_rotation=lambda: types.SimpleNamespace(yaw=180.0),
            get_component_by_class=lambda _class: movement,
        )
        actors = {
            "character": character,
            "gasp_locomotion": {
                "actor_id": "character",
                "controller": types.SimpleNamespace(set_control_rotation=lambda _rotation: None),
                "movement_control": {"max_speed_m_s": 2.0, "events": []},
                "last_frame": None,
                "turn_in_place_active": True,
                "root_facing_offset_deg": -90.0,
                "fired_events": set(),
                "crouched": False,
                "jump_pressed": False,
                "detail": {},
            },
        }

        self.assertTrue(apply(actors, {"id": "character", "params": {}}, {"frame": 1, "time": 1.0}))
        self.assertEqual(calls, [(character, True)])
        self.assertTrue(actors["gasp_locomotion"]["turn_in_place_active"])

    def test_idle_warmup_restores_declared_position_before_grounding_settle(self) -> None:
        movement = types.SimpleNamespace(
            velocity=Vector(1.0, 2.0, 3.0),
            mode=None,
            stop_movement_immediately=lambda: None,
            clear_accumulated_forces=lambda: None,
            set_movement_mode=lambda mode: setattr(movement, "mode", mode),
        )

        class GroundedCharacter:
            def __init__(self) -> None:
                self.location = Vector(10.0, 20.0, 95.0)

            def get_component_by_class(self, _class):
                return movement

            def get_actor_location(self):
                return self.location

            def set_actor_location(self, location, _sweep, _teleport):
                self.location = location

            def stop_jumping(self):
                pass

        character = GroundedCharacter()
        functions = load_functions(
            "begin_gasp_grounding_settle",
            extra={
                "unreal": types.SimpleNamespace(
                    CharacterMovementComponent="movement",
                    GameplayStatics=types.SimpleNamespace(get_time_seconds=lambda _world: 0.5),
                    Vector=Vector,
                    MovementMode=types.SimpleNamespace(MOVE_FALLING="falling"),
                ),
                "set_simulation_world_paused": lambda _actors, _status, _paused: None,
                "restore_articulated_frame_zero_roots": lambda _actors, _scene: setattr(
                    character, "location", Vector(10.0, 20.0, 92.6)
                ),
            },
        )
        actors = {
            "character": character,
            "physics_game_world": object(),
            "gasp_locomotion": {"actor_id": "character", "detail": {}},
            "gasp_idle_warmup": {
                "active": True,
                "phase": "animation_warmup",
                "duration_s": 0.5,
                "game_time_start_s": 0.0,
                "start_location_cm": [10.0, 20.0, 92.6],
            },
        }

        detail = functions["begin_gasp_grounding_settle"](actors, {}, {})

        self.assertEqual(detail["phase"], "grounding_settle")
        self.assertEqual(
            (character.location.x, character.location.y, character.location.z),
            (10.0, 20.0, 92.6),
        )
        self.assertEqual(movement.mode, "falling")
        self.assertEqual((movement.velocity.x, movement.velocity.y, movement.velocity.z), (0.0, 0.0, 0.0))

    def test_grounding_settle_requires_stable_real_ground_contact(self) -> None:
        game_time = {"value": 0.5}
        movement = types.SimpleNamespace(
            velocity=Vector(0.0, 0.0, 0.0),
            is_moving_on_ground=lambda: True,
        )

        class SettlingCharacter:
            def __init__(self) -> None:
                self.location = Vector(10.0, 20.0, 95.0)

            def get_component_by_class(self, _class):
                return movement

            def get_actor_location(self):
                return self.location

            def set_actor_location(self, location, _sweep, _teleport):
                self.location = location

        character = SettlingCharacter()
        poll = load_functions(
            "poll_gasp_grounding_settle",
            extra={
                "unreal": types.SimpleNamespace(
                    CharacterMovementComponent="movement",
                    GameplayStatics=types.SimpleNamespace(get_time_seconds=lambda _world: game_time["value"]),
                    Vector=Vector,
                ),
                "set_simulation_world_paused": lambda _actors, _status, _paused: None,
                "reset_gasp_idle_state": lambda _actor: {"movement_mode": "MOVE_WALKING"},
                "gasp_visible_root_yaw": lambda _actor: -90.0,
                "GASP_POST_GROUND_SETTLE_SECONDS": 0.3,
            },
        )["poll_gasp_grounding_settle"]
        actors = {
            "character": character,
            "physics_game_world": object(),
            "gasp_locomotion": {
                "actor_id": "character",
                "detail": {},
                "fired_events": set(),
                "initial_facing_yaw_deg": 0.0,
            },
            "gasp_idle_warmup": {
                "active": True,
                "phase": "grounding_settle",
                "duration_s": 0.5,
                "game_time_start_s": 0.0,
                "settle_game_time_start_s": 0.5,
                "target_xy_cm": [10.0, 20.0],
                "last_z_cm": 95.0,
                "last_sample_game_s": 0.5,
                "grounding_stable_samples": 0,
                "pre_reset_displacement_cm": 118.0,
            },
        }

        results = []
        for value in (0.52, 0.68, 0.84):
            game_time["value"] = value
            results.append(poll(actors, {}, {}))

        self.assertIsNone(results[0])
        self.assertIsNone(results[1])
        self.assertTrue(results[2]["capture_start_grounded"])
        self.assertEqual(results[2]["grounding_stable_samples"], 3)
        self.assertGreaterEqual(results[2]["post_ground_animation_settle_s"], 0.3)
        self.assertEqual(results[2]["settled_capture_z_cm"], 95.0)
        self.assertEqual(actors["articulated_capture_start_z_cm"], {"character": 95.0})

    def test_frame_zero_root_restore_reuses_grounded_capture_height(self) -> None:
        restored_rotations = []

        class CharacterRoot:
            def __init__(self) -> None:
                self.location = None

            def set_actor_location(self, location, _sweep, _teleport):
                self.location = location

            def set_actor_rotation(self, rotation, _teleport):
                restored_rotations.append(rotation)

        restore = load_functions(
            "restore_articulated_frame_zero_roots",
            extra={
                "unreal": types.SimpleNamespace(Vector=Vector),
                "runtime_rotator": lambda value: value,
            },
        )["restore_articulated_frame_zero_roots"]
        character = CharacterRoot()
        actors = {
            "character": character,
            "runtime_initial_transforms": {
                "character": {
                    "position_cm": [10.0, 20.0, 92.6],
                    "rotation_degrees": [0.0, 0.0, 0.0],
                }
            },
            "articulated_capture_start_z_cm": {"character": 95.0},
        }
        runtime_scene = {
            "dynamic_objects": [
                {"id": "character", "behavior": "articulated_character"}
            ]
        }

        restore(actors, runtime_scene)

        self.assertEqual(
            (character.location.x, character.location.y, character.location.z),
            (10.0, 20.0, 95.0),
        )
        self.assertEqual(restored_rotations, [[0.0, 0.0, 0.0]])

    def test_scene_capture_gasp_uses_async_pie_lifecycle(self) -> None:
        requires_async = load_functions(
            "capture_backend_requires_async_pie",
            extra={"runtime_gasp_objects": lambda scene: scene.get("gasp_objects", [])},
        )["capture_backend_requires_async_pie"]

        self.assertTrue(requires_async("highres_viewport", {}))
        self.assertTrue(requires_async("scene_capture", {"gasp_objects": ["character"]}))
        self.assertFalse(requires_async("scene_capture", {"gasp_objects": []}))

    def test_async_scene_capture_exports_rebound_render_target_as_exr(self) -> None:
        calls = []
        submit = load_functions(
            "submit_async_rgb_frame",
            extra={
                "Path": Path,
                "capture_render_target_to_file": lambda actors, path, **kwargs: calls.append(
                    (actors, path, kwargs)
                )
                or True,
            },
        )["submit_async_rgb_frame"]
        actors = {"capture_world": "pie_world", "capture_comp": "pie_capture"}

        captured, task = submit(actors, Path("frame_0001.exr"), "scene_capture", object())

        self.assertTrue(captured)
        self.assertIsNone(task)
        self.assertEqual(calls, [(actors, Path("frame_0001.exr"), {"force_exr": True})])

    def test_character_internal_cameras_are_deactivated_for_harness_capture(self) -> None:
        sanitize = load_functions(
            "sanitize_articulated_character_cameras",
            extra={"unreal": unreal_namespace()},
        )["sanitize_articulated_character_cameras"]

        class CameraOutput:
            def __init__(self, name: str, class_name: str) -> None:
                self.name = name
                self.class_name = class_name
                self.properties = {}
                self.deactivated = False

            def get_class(self):
                return types.SimpleNamespace(get_name=lambda: self.class_name)

            def get_name(self):
                return self.name

            def set_editor_property(self, name, value):
                self.properties[name] = value

            def deactivate(self):
                self.deactivated = True

        camera = CameraOutput("Camera(NotUsedByDefault)", "CameraComponent")
        gameplay_camera = CameraOutput("GameplayCamera", "GameplayCameraComponent")
        cine_camera = CameraOutput("OutputCameraComponent", "CineCameraComponent")
        movement = CameraOutput("CharMoveComp", "CharacterMovementComponent")
        actor = types.SimpleNamespace(
            get_components_by_class=lambda _class: [camera, gameplay_camera, cine_camera, movement]
        )
        controller = types.SimpleNamespace(properties={})
        controller.set_editor_property = lambda name, value: controller.properties.__setitem__(name, value)

        detail = sanitize(actor, controller)

        self.assertEqual(controller.properties["auto_manage_active_camera_target"], False)
        self.assertEqual(
            detail["deactivated_components"],
            ["Camera(NotUsedByDefault)", "GameplayCamera", "OutputCameraComponent"],
        )
        self.assertTrue(camera.deactivated)
        self.assertTrue(gameplay_camera.deactivated)
        self.assertTrue(cine_camera.deactivated)
        self.assertFalse(movement.deactivated)
        self.assertEqual(camera.properties["auto_activate"], False)

    def test_character_uses_skeletal_mesh_and_hides_auxiliary_primitives(self) -> None:
        functions = load_functions(
            "authoritative_character_mesh_component",
            "sanitize_articulated_character_rendering",
            "actor_runtime_component",
            extra={"unreal": unreal_namespace()},
        )
        character = Character()

        selected = functions["actor_runtime_component"](character)
        audit = functions["sanitize_articulated_character_rendering"](character)

        self.assertIs(selected, character.mesh)
        self.assertTrue(audit["sanitized"])
        self.assertEqual(audit["render_component"], "CharacterMesh0")
        self.assertEqual(character.mesh.visible, (True, False))
        self.assertEqual(character.mesh.hidden, (False, False))
        self.assertEqual(character.helper.hidden, (True, False))
        self.assertEqual(character.capsule.hidden, (True, False))

    def test_idle_reset_starts_from_zero_velocity_in_walking_mode(self) -> None:
        reset = load_functions("reset_gasp_idle_state", extra={"unreal": unreal_namespace()})[
            "reset_gasp_idle_state"
        ]
        character = Character()

        detail = reset(character)

        self.assertTrue(character.stopped_jumping)
        self.assertTrue(character.movement.stopped)
        self.assertTrue(character.movement.cleared)
        self.assertEqual(character.movement.mode, "walking")
        self.assertEqual(
            (character.movement.velocity.x, character.movement.velocity.y, character.movement.velocity.z),
            (0.0, 0.0, 0.0),
        )
        self.assertEqual(detail["movement_mode"], "MOVE_WALKING")

    def test_instance_masks_return_authoritative_render_components(self) -> None:
        character = Character()
        functions = load_functions(
            "assign_instance_mask_materials",
            extra={
                "runtime_actor_ids_for_segmentation": lambda _scene: ["person"],
                "actor_runtime_component": lambda _actor: character.mesh,
                "snapshot_actor_materials": lambda _actor: ["authored"],
                "instance_mask_material": lambda _index: "mask",
                "set_actor_material": lambda _actor, _material: None,
                "instance_color_from_index": lambda _index: types.SimpleNamespace(r=0.1, g=0.2, b=0.3),
                "flush_editor_rendering": lambda: None,
            },
        )

        state = functions["assign_instance_mask_materials"]({"person": character}, {})

        self.assertEqual(state["components"], [character.mesh])
        self.assertNotIn("actors", state)
        self.assertEqual(state["mapping"][0]["object_id"], "person")

    def test_segmentation_capture_uses_component_show_only(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("capture_comp.show_only_component(mask_component)", source)
        self.assertNotIn("capture_comp.show_only_actor_components(mask_actor, True)", source)


if __name__ == "__main__":
    unittest.main()
