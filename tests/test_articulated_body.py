from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.case_spec_v2 import CaseSpecV2ValidationError, case_spec_v2_from_dict, compile_case_spec_v2_runtime
from harness.core.artifact_schema import write_json
from harness.planning.static_scene_builder import build_static_scene_layout
from harness.runtime.actor_placement import compile_runtime_actor_placement
from harness.runtime.articulated_body import (
    ARTICULATED_BODY_ASSET_PATH,
    ARTICULATED_BODY_CONTROL_RIG_PATH,
    ARTICULATED_HEAD_LOOK_CONTROLS,
    ARTICULATED_IK_CONTROLS,
    ArticulatedBodyContractError,
    compile_articulated_body_contract,
    sample_articulated_body_contract,
)
from harness.runtime.ue_backend import materialize_articulated_body_assets
from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement
from harness.verification.physics_verifier import PhysicsVerifier
from harness.verification.trajectory_assertion_verifier import evaluate_assertion
from scripts.harness_local_ue_runner import (
    canonicalize_native_trajectory,
    default_physics_controls,
    runtime_objects_from_actor_placement,
)
from tests.case_spec_v2_fixture import case_spec_v2_fixture


def articulated_case() -> dict:
    data = case_spec_v2_fixture()
    data["identity"]["case_id"] = "articulated_turn_and_hold"
    data["capabilities"] = {
        "primary": "articulated_body_motion",
        "required": ["articulated_body_motion"],
    }
    data["backend_constraints"] = {
        "required_solver_capabilities": ["articulated_body", "trajectory"],
        "allowed_solvers": ["ue"],
        "render_backend": "ue",
        "allow_multi_backend": False,
    }
    data["objects"][0] = {
        "id": "cue_ball",
        "role": "articulated_body",
        "geometry": {"shape_hint": "humanoid", "approx_size_m": [0.58, 0.36, 1.92]},
        "physics": {"body_type": "kinematic", "collision_required": False},
        "initial_state": {
            "position_m": [-0.8, 0.0, 0.0],
            "rotation_deg": [0.0, 15.0, 0.0],
            "linear_velocity_m_s": [0.0, 0.0, 0.0],
        },
        "behavior": {},
        "visual_representation": {"source": "solver_generated", "visible": True},
        "solver": {
            "type": "articulated_body",
            "model": "harness_ue_mannequin_v1",
            "mode": "kinematic",
            "pose_source": {
                "type": "pose_keyframes",
                "keyframes": [
                    {"time_s": 0.0, "rotations_deg": {"upperarm_r": [0.0, 0.0, 0.0]}},
                    {"time_s": 2.0, "rotations_deg": {"upperarm_r": [0.0, 0.0, 60.0]}},
                ],
            },
            "root_transform_source": {
                "type": "root_keyframes",
                "keyframes": [
                    {"time_s": 0.0, "position_offset_m": [0.0, 0.0, 0.0], "rotation_offset_deg": [0.0, 0.0, 0.0]},
                    {"time_s": 2.0, "position_offset_m": [0.8, 0.0, 0.0], "rotation_offset_deg": [0.0, 90.0, 0.0]},
                ],
            },
            "ik_targets": [],
            "attachments": [
                {
                    "object_id": "target_ball",
                    "bone": "hand_r",
                    "start_time_s": 0.0,
                    "end_time_s": 2.0,
                    "local_position_m": [0.0, 0.0, 0.0],
                    "local_rotation_deg": [0.0, 0.0, 0.0],
                }
            ],
        },
    }
    data["objects"][1]["physics"]["body_type"] = "kinematic"
    data["objects"][1]["initial_state"]["position_m"] = [-0.8, 0.0, 1.2]
    data["objects"][1]["physics"]["collision_geometry"] = {
        "shape": "sphere",
        "size_m": [0.18, 0.18, 0.18],
        "local_center_offset_m": [0.0, 0.0, 0.0],
    }
    data["objects"][2]["physics"]["collision_geometry"] = {
        "shape": "box",
        "size_m": [3.0, 2.0, 0.1],
        "local_center_offset_m": [0.0, 0.0, 0.0],
    }
    data["relations"] = []
    data["events"] = []
    data["expected_behavior"] = {}
    data["observation_requirements"]["signals"] = ["trajectory", "articulated_pose"]
    data["verification_requirements"]["assertions"] = []
    return data


def animated_character_case() -> dict:
    data = articulated_case()
    solver = data["objects"][0]["solver"]
    solver["pose_source"] = {
        "type": "animation_sequence",
        "segments": [
            {
                "animation_asset_id": "harness_ue4_mannequin_walk_v1",
                "start_time_s": 0.0,
                "end_time_s": 2.0,
                "play_rate": 1.0,
                "loop": True,
            }
        ],
    }
    solver["root_transform_source"] = {
        "type": "character_movement",
        "keyframes": [
            {"time_s": 0.0, "position_offset_m": [0.0, 0.0, 0.0], "rotation_offset_deg": [0.0, 0.0, 0.0]},
            {"time_s": 2.0, "position_offset_m": [1.2, 0.0, 0.0], "rotation_offset_deg": [0.0, 0.0, 0.0]},
        ],
        "max_speed_m_s": 1.0,
        "max_acceleration_m_s2": 2.0,
    }
    return data


class ArticulatedBodyTests(unittest.TestCase):
    def test_fixed_mannequin_uses_boolean_ik_switch_controls(self) -> None:
        self.assertEqual(ARTICULATED_IK_CONTROLS["hand_l"]["switch"], "arm_l_fk_ik_switch")
        self.assertEqual(ARTICULATED_IK_CONTROLS["hand_r"]["switch"], "arm_r_fk_ik_switch")
        self.assertEqual(ARTICULATED_HEAD_LOOK_CONTROLS["target"], "head_ik_ctrl")
        self.assertEqual(ARTICULATED_HEAD_LOOK_CONTROLS["switch"], "neck_fk_ik_switch")

    def test_standard_verifier_rejects_commanded_articulated_trajectory_without_observation(self) -> None:
        runtime = compile_case_spec_v2_runtime(case_spec_v2_from_dict(animated_character_case()))
        with tempfile.TemporaryDirectory() as raw_root:
            run_dir = Path(raw_root)
            write_json(run_dir / "case_spec.json", runtime.data)
            write_json(run_dir / "trajectory.json", [{
                "frame": 0,
                "time": 0.0,
                "objects": {"cue_ball": {"position": [-0.8, 0.0, 0.96], "source": "scripted_runtime_preview"}},
            }])

            report = PhysicsVerifier().verify_run_dir(run_dir)

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F_ARTICULATED_EXECUTION_FAILED")
        self.assertEqual(report["first_failure"]["metric"], "post_tick_observation_missing")

    def test_animation_and_character_movement_compile_as_orthogonal_sources(self) -> None:
        case = case_spec_v2_from_dict(animated_character_case())
        runtime = compile_case_spec_v2_runtime(case)
        contract = runtime.data["objects"][0]["solver"]
        layout = build_static_scene_layout(runtime.data)
        placement = compile_runtime_actor_placement(runtime.data, layout, target_backend="ue")
        dynamic, _ = runtime_objects_from_actor_placement(placement, runtime.data)
        human = next(item for item in dynamic if item["id"] == "cue_ball")

        self.assertEqual(contract["schema_version"], "harness_articulated_body_contract_v3")
        self.assertEqual(contract["pose_source"]["segments"][0]["animation_asset_path"], "/Game/Characters/Mannequins/Anims/Unarmed/Walk/MF_Unarmed_Walk_Fwd.MF_Unarmed_Walk_Fwd")
        self.assertEqual(contract["root_transform_source"]["type"], "character_movement")
        self.assertEqual(human["behavior"], "articulated_character")
        self.assertEqual(human["asset_kind"], "articulated_character")

    def test_unique_support_is_compiled_without_guessing_between_multiple_targets(self) -> None:
        data = animated_character_case()
        data["relations"] = [{"type": "supported_by", "source": "cue_ball", "target": "floor"}]
        runtime = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data))
        layout = build_static_scene_layout(runtime.data)
        placement = compile_runtime_actor_placement(runtime.data, layout, target_backend="ue")

        dynamic, _ = runtime_objects_from_actor_placement(placement, runtime.data)
        human = next(item for item in dynamic if item["id"] == "cue_ball")

        self.assertEqual(human["params"]["support_object_id"], "floor")
        self.assertEqual(human["params"]["support_binding_source"], "unique_supported_by_relation")

        ambiguous = animated_character_case()
        ambiguous["relations"] = [
            {"type": "supported_by", "source": "cue_ball", "target": "floor"},
            {"type": "supported_by", "source": "cue_ball", "target": "target_ball"},
        ]
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(ambiguous)
        self.assertIn("ambiguous_articulated_support", {issue.code for issue in context.exception.issues})

        ambiguous["objects"][0]["solver"]["support_object_id"] = "floor"
        case_spec_v2_from_dict(ambiguous)

    def test_in_place_animation_cannot_claim_root_motion(self) -> None:
        data = animated_character_case()
        data["objects"][0]["solver"]["root_transform_source"] = {"type": "animation_root_motion"}

        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)

        self.assertIn("invalid_articulated_body_contract", {issue.code for issue in context.exception.issues})

    def test_ik_targets_are_sampled_independently_of_base_pose(self) -> None:
        data = animated_character_case()
        data["objects"][0]["solver"]["ik_targets"] = [
            {
                "goal": "hand_r",
                "tolerance_m": 0.03,
                "keyframes": [
                    {"time_s": 0.0, "position_m": [0.0, 0.0, 1.0], "rotation_deg": [0.0, 0.0, 0.0], "weight": 0.0},
                    {"time_s": 2.0, "position_m": [0.4, 0.0, 1.2], "rotation_deg": [0.0, 45.0, 0.0], "weight": 1.0},
                ],
            }
        ]
        runtime = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data))

        pose = sample_articulated_body_contract(runtime.data["objects"][0]["solver"], 1.0)

        self.assertEqual(pose["ik_targets"]["hand_r"]["position_m"], [0.2, 0.0, 1.1])
        self.assertEqual(pose["ik_targets"]["hand_r"]["rotation_deg"], [0.0, 22.5, 0.0])
        self.assertEqual(pose["ik_targets"]["hand_r"]["weight"], 0.5)

    def test_animation_pose_overlay_is_weighted_local_rotation_without_root_ownership(self) -> None:
        data = animated_character_case()
        data["objects"][0]["solver"]["pose_overlay"] = {
            "type": "bone_local_rotation_offsets",
            "keyframes": [
                {"time_s": 0.0, "rotations_deg": {"head": [0.0, 0.0, 0.0]}, "weight": 0.0},
                {"time_s": 2.0, "rotations_deg": {"head": [0.0, 60.0, 0.0]}, "weight": 1.0},
            ],
        }
        contract = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data)).data["objects"][0]["solver"]

        sampled = sample_articulated_body_contract(contract, 1.0)

        self.assertEqual(sampled["pose_overlay"]["rotation_space"], "bone_local_offset")
        self.assertEqual(sampled["pose_overlay"]["blend_mode"], "weighted_additive")
        self.assertEqual(sampled["pose_overlay"]["rotations_deg"]["head"], [0.0, 30.0, 0.0])
        self.assertEqual(sampled["pose_overlay"]["weight"], 0.5)
        self.assertEqual(contract["root_transform_source"]["type"], "character_movement")

    def test_pose_overlay_rejects_root(self) -> None:
        data = animated_character_case()
        data["objects"][0]["solver"]["pose_overlay"] = {
            "type": "bone_local_rotation_offsets",
            "keyframes": [{"time_s": 0.0, "rotations_deg": {"root": [0.0, 10.0, 0.0]}, "weight": 1.0}],
        }

        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)

        self.assertIn("invalid_articulated_body_contract", {issue.code for issue in context.exception.issues})

    def test_ik_pole_and_head_look_are_sampled_as_chain_layers(self) -> None:
        data = animated_character_case()
        solver = data["objects"][0]["solver"]
        solver["ik_targets"] = [{
            "goal": "hand_l",
            "tolerance_m": 0.03,
            "keyframes": [
                {"time_s": 0.0, "position_m": [0.0, 0.0, 1.0], "rotation_deg": [0.0, 0.0, 0.0], "pole_position_m": [0.0, -0.4, 1.2], "weight": 0.0},
                {"time_s": 2.0, "position_m": [0.4, 0.0, 1.2], "rotation_deg": [0.0, 40.0, 0.0], "pole_position_m": [0.2, -0.4, 1.3], "weight": 1.0},
            ],
        }]
        solver["head_look_target"] = {
            "tolerance_deg": 4.0,
            "keyframes": [
                {"time_s": 0.0, "position_m": [1.0, 0.0, 1.7], "weight": 0.0},
                {"time_s": 2.0, "position_m": [1.0, 1.0, 1.7], "weight": 1.0},
            ],
        }
        contract = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data)).data["objects"][0]["solver"]

        sampled = sample_articulated_body_contract(contract, 1.0)

        self.assertEqual(sampled["ik_targets"]["hand_l"]["pole_position_m"], [0.1, -0.4, 1.25])
        self.assertEqual(sampled["head_look_target"]["position_m"], [1.0, 0.5, 1.7])
        self.assertEqual(sampled["head_look_target"]["weight"], 0.5)

    def test_ik_compiles_to_the_fixed_control_rig(self) -> None:
        data = animated_character_case()
        data["objects"][0]["solver"]["ik_targets"] = [{
            "goal": "hand_r",
            "tolerance_m": 0.03,
            "keyframes": [
                {"time_s": 0.0, "position_m": [0.0, 0.0, 1.0], "rotation_deg": [0.0, 0.0, 0.0], "weight": 1.0},
                {"time_s": 2.0, "position_m": [0.4, 0.0, 1.2], "rotation_deg": [0.0, 45.0, 0.0], "weight": 1.0},
            ],
        }]
        runtime = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data))

        self.assertEqual(runtime.data["objects"][0]["solver"]["control_rig_path"], ARTICULATED_BODY_CONTROL_RIG_PATH)

    def test_fixed_mannequin_materializes_at_its_authored_package_root(self) -> None:
        runtime = compile_case_spec_v2_runtime(case_spec_v2_from_dict(articulated_case()))
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            executable = root / "UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd"
            project = root / "Project/SimulatorWorkspace.uproject"
            source = root / "UE_5.7/Templates/TemplateResources/High/Characters/Content/Mannequins"
            executable.parent.mkdir(parents=True)
            executable.touch()
            project.parent.mkdir(parents=True)
            project.touch()
            for relative in (
                "Meshes/SKM_Manny_Simple.uasset",
                "Meshes/SK_Mannequin.uasset",
                "Rigs/PA_Mannequin.uasset",
                "Rigs/CR_Mannequin_Body.uasset",
            ):
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative.encode("utf-8"))

            materialize_articulated_body_assets(
                runtime,
                {
                    "resolved_paths": {
                        "SIM_STUDIO_UE_EXECUTABLE": str(executable),
                        "SIM_STUDIO_UE_PROJECT": str(project),
                    }
                },
            )

            self.assertEqual(
                ARTICULATED_BODY_ASSET_PATH,
                "/Game/Characters/Mannequins/Meshes/SKM_Manny_Simple.SKM_Manny_Simple",
            )
            self.assertTrue((project.parent / "Content/Characters/Mannequins/Rigs/CR_Mannequin_Body.uasset").is_file())
            self.assertFalse((project.parent / "Content/Mannequin/Character/Mesh/SK_Mannequin.uasset").exists())

    def test_declared_animation_is_materialized_from_fixed_bundle(self) -> None:
        runtime = compile_case_spec_v2_runtime(case_spec_v2_from_dict(animated_character_case()))
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            executable = root / "UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd"
            project = root / "Project/SimulatorWorkspace.uproject"
            character_source = root / "UE_5.7/Templates/TemplateResources/High/Characters/Content/Mannequins"
            animation_source = root / "UE_5.7/Templates/TemplateResources/High/Characters/Content/Mannequins/Anims/Unarmed/Walk"
            executable.parent.mkdir(parents=True)
            executable.touch()
            project.parent.mkdir(parents=True)
            project.touch()
            for relative in (
                "Meshes/SKM_Manny_Simple.uasset",
                "Meshes/SK_Mannequin.uasset",
                "Rigs/PA_Mannequin.uasset",
                "Rigs/CR_Mannequin_Body.uasset",
            ):
                path = character_source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative.encode("utf-8"))
            animation_source.mkdir(parents=True)
            (animation_source / "MF_Unarmed_Walk_Fwd.uasset").write_bytes(b"walk")

            materialize_articulated_body_assets(
                runtime,
                {"resolved_paths": {"SIM_STUDIO_UE_EXECUTABLE": str(executable), "SIM_STUDIO_UE_PROJECT": str(project)}},
            )

            self.assertEqual(
                (project.parent / "Content/Characters/Mannequins/Anims/Unarmed/Walk/MF_Unarmed_Walk_Fwd.uasset").read_bytes(),
                b"walk",
            )

    def test_contract_compiles_and_interpolates_root_and_joint_pose(self) -> None:
        solver = articulated_case()["objects"][0]["solver"]
        contract = compile_articulated_body_contract(
            solver,
            duration_s=2.0,
            known_object_ids={"cue_ball", "target_ball", "floor"},
            object_id="cue_ball",
        )

        pose = sample_articulated_body_contract(contract, 1.0)

        self.assertEqual(pose["root_position_offset_m"], [0.4, 0.0, 0.0])
        self.assertEqual(pose["root_rotation_offset_deg"], [0.0, 45.0, 0.0])
        self.assertEqual(pose["joint_rotations_deg"]["upperarm_r"], [0.0, 0.0, 30.0])

    def test_case_projection_actor_binding_and_local_ue_runtime_are_articulated_only(self) -> None:
        case = case_spec_v2_from_dict(articulated_case())
        runtime = compile_case_spec_v2_runtime(case)
        layout = build_static_scene_layout(runtime.data)
        placement = compile_runtime_actor_placement(runtime.data, layout, target_backend="ue")
        report = verify_runtime_actor_placement(runtime.data, placement)
        dynamic, static = runtime_objects_from_actor_placement(placement, runtime.data)

        human_binding = next(item for item in placement["actor_bindings"] if item["object_id"] == "cue_ball")
        human_node = next(item for item in layout["object_nodes"] if item["object_id"] == "cue_ball")
        human_runtime = next(item for item in dynamic if item["id"] == "cue_ball")
        attached_runtime = next(item for item in dynamic if item["id"] == "target_ball")
        self.assertEqual(report["status"], "pass")
        self.assertEqual(human_binding["render_binding"]["kind"], "articulated_body")
        self.assertEqual(human_binding["asset"]["authored_size_m"], [0.58, 0.36, 1.92])
        self.assertTrue(human_binding["asset"]["preserve_authored_scale"])
        self.assertEqual(human_node["bounds"]["extents_m"], [0.29, 0.18, 0.96])
        self.assertEqual(human_node["bounds"]["top_z"], 1.92)
        self.assertEqual(human_binding["physics"]["simulate_physics"], False)
        self.assertEqual(human_runtime["behavior"], "articulated_kinematic")
        self.assertEqual(human_runtime["asset_kind"], "articulated_kinematic")
        self.assertEqual(human_runtime["scale"], [1.0, 1.0, 1.0])
        self.assertEqual(human_runtime["params"]["pose_anchor"], "actor_origin")
        self.assertEqual(human_runtime["params"]["base_rotation_degrees"], [0.0, 15.0, 0.0])
        self.assertEqual(human_runtime["rotation_degrees"], [0.0, 0.0, 0.0])
        self.assertFalse(attached_runtime["physics_properties"]["simulate_physics"])
        self.assertFalse(any(item["id"] == "target_ball" for item in static))
        self.assertTrue(any(item["id"] == "floor" for item in static))

    def test_native_ue_trajectory_is_canonicalized_before_verification(self) -> None:
        native = [
            {
                "frame": 0,
                "time": 0.0,
                "objects": {
                    "cup": {
                        "position": [0.0, 0.0, 0.89],
                        "rotation_degrees": [0.0, 0.0, 0.0],
                    }
                },
            },
            {
                "frame": 1,
                "time": 1.0,
                "objects": {
                    "cup": {
                        "position": [0.0, 0.0, 1.19],
                        "rotation_degrees": [0.0, 0.0, 0.0],
                    }
                },
            },
        ]

        trajectory = canonicalize_native_trajectory(native)
        result = evaluate_assertion(
            {
                "type": "state_delta",
                "object_id": "cup",
                "field": "position_m.z",
                "operator": ">",
                "value": 0.0,
            },
            trajectory,
        )

        self.assertEqual(trajectory[0]["objects"]["cup"]["position_m"], [0.0, 0.0, 0.89])
        self.assertEqual(trajectory[0]["objects"]["cup"]["rotation_deg"], [0.0, 0.0, 0.0])
        self.assertTrue(result["passed"])
        self.assertAlmostEqual(result["measured"], 0.3)

    def test_unknown_bone_and_implicit_mode_transition_fail(self) -> None:
        bad_bone = articulated_case()
        bad_bone["objects"][0]["solver"]["pose_source"]["keyframes"][0]["rotations_deg"] = {"invented_hand": [0.0, 0.0, 0.0]}
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(bad_bone)
        self.assertIn("invalid_articulated_body_contract", {issue.code for issue in context.exception.issues})

        implicit_transition = articulated_case()["objects"][0]["solver"]
        implicit_transition["ragdoll_start_time_s"] = 1.0
        with self.assertRaises(ArticulatedBodyContractError):
            compile_articulated_body_contract(
                implicit_transition,
                duration_s=2.0,
                known_object_ids={"cue_ball", "target_ball", "floor"},
                object_id="cue_ball",
            )

    def test_ragdoll_is_explicit_chaos_mode_without_cpp_driver(self) -> None:
        data = articulated_case()
        body = data["objects"][0]
        body["physics"] = {"body_type": "dynamic", "collision_required": True}
        body["solver"]["mode"] = "ragdoll"
        body["solver"]["ragdoll_start_time_s"] = 1.0
        body["solver"]["pose_source"]["keyframes"] = [
            {"time_s": 0.0, "rotations_deg": {}},
            {"time_s": 2.0, "rotations_deg": {}},
        ]
        body["solver"]["attachments"][0]["end_time_s"] = 1.0

        case = case_spec_v2_from_dict(data)
        runtime = compile_case_spec_v2_runtime(case)
        controls = default_physics_controls(runtime.data)
        layout = build_static_scene_layout(runtime.data)
        placement = compile_runtime_actor_placement(runtime.data, layout, target_backend="ue")
        dynamic, _ = runtime_objects_from_actor_placement(placement, runtime.data)
        human = next(item for item in dynamic if item["id"] == "cue_ball")

        self.assertEqual(human["behavior"], "articulated_ragdoll")
        self.assertEqual(human["physics_properties"]["simulate_physics"], "force_off_until_release")
        self.assertEqual(controls["runtime_driver_backend"], "ue_world_simulation")
        self.assertFalse(controls["cpp_runtime_driver_enabled"])


if __name__ == "__main__":
    unittest.main()
