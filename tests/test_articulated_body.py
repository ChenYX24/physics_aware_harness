from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.case_spec_v2 import CaseSpecV2ValidationError, case_spec_v2_from_dict, compile_case_spec_v2_runtime
from harness.planning.static_scene_builder import build_static_scene_layout
from harness.runtime.actor_placement import compile_runtime_actor_placement
from harness.runtime.articulated_body import (
    ARTICULATED_BODY_ASSET_PATH,
    ArticulatedBodyContractError,
    compile_articulated_body_contract,
    sample_articulated_body_contract,
)
from harness.runtime.ue_backend import materialize_articulated_body_assets
from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement
from scripts.harness_local_ue_runner import default_physics_controls, runtime_objects_from_actor_placement
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
            "root_keyframes": [
                {"time_s": 0.0, "position_offset_m": [0.0, 0.0, 0.0], "rotation_offset_deg": [0.0, 0.0, 0.0]},
                {"time_s": 2.0, "position_offset_m": [0.8, 0.0, 0.0], "rotation_offset_deg": [0.0, 90.0, 0.0]},
            ],
            "joint_keyframes": [
                {"time_s": 0.0, "rotations_deg": {"upperarm_r": [0.0, 0.0, 0.0]}},
                {"time_s": 2.0, "rotations_deg": {"upperarm_r": [0.0, 0.0, 60.0]}},
            ],
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


class ArticulatedBodyTests(unittest.TestCase):
    def test_fixed_mannequin_materializes_at_its_authored_package_root(self) -> None:
        runtime = compile_case_spec_v2_runtime(case_spec_v2_from_dict(articulated_case()))
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            executable = root / "UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd"
            project = root / "Project/SimulatorWorkspace.uproject"
            source = root / "UE_5.7/Templates/TemplateResources/High/Mannequin/Content/Character"
            executable.parent.mkdir(parents=True)
            executable.touch()
            project.parent.mkdir(parents=True)
            project.touch()
            for relative in (
                "Mesh/SK_Mannequin.uasset",
                "Mesh/UE4_Mannequin_Skeleton.uasset",
                "Mesh/SK_Mannequin_PhysicsAsset.uasset",
                "Materials/M_Male_Body.uasset",
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
                "/Game/Mannequin/Character/Mesh/SK_Mannequin.SK_Mannequin",
            )
            self.assertTrue((project.parent / "Content/Mannequin/Character/Materials/M_Male_Body.uasset").is_file())
            self.assertFalse((project.parent / "Content/Character/Mesh/SK_Mannequin.uasset").exists())

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
        human_runtime = next(item for item in dynamic if item["id"] == "cue_ball")
        attached_runtime = next(item for item in dynamic if item["id"] == "target_ball")
        self.assertEqual(report["status"], "pass")
        self.assertEqual(human_binding["render_binding"]["kind"], "articulated_body")
        self.assertEqual(human_binding["physics"]["simulate_physics"], False)
        self.assertEqual(human_runtime["behavior"], "articulated_kinematic")
        self.assertEqual(human_runtime["asset_kind"], "articulated_kinematic")
        self.assertEqual(human_runtime["params"]["pose_anchor"], "actor_origin")
        self.assertEqual(human_runtime["params"]["base_rotation_degrees"], [0.0, 15.0, 0.0])
        self.assertEqual(human_runtime["rotation_degrees"], [0.0, 0.0, 0.0])
        self.assertFalse(attached_runtime["physics_properties"]["simulate_physics"])
        self.assertFalse(any(item["id"] == "target_ball" for item in static))
        self.assertTrue(any(item["id"] == "floor" for item in static))

    def test_unknown_bone_and_implicit_mode_transition_fail(self) -> None:
        bad_bone = articulated_case()
        bad_bone["objects"][0]["solver"]["joint_keyframes"][0]["rotations_deg"] = {"invented_hand": [0.0, 0.0, 0.0]}
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
        body["solver"]["joint_keyframes"] = [
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
