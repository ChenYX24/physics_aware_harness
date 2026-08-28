from __future__ import annotations

import json
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.core.timebase import build_timebase, sample_solver_trajectory
from harness.verification.run_quality import EXR_MAGIC, evaluate_run
from harness.verification.run_quality import (
    validate_articulated_execution,
    validate_contacts,
    validate_runtime_binding_snapshot,
)


class RunQualityTests(unittest.TestCase):
    def test_articulated_execution_requires_post_tick_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self.write_json(run_dir / "case_spec.json", {
                "objects": [{
                    "id": "person",
                    "solver": {
                        "type": "articulated_body",
                        "mode": "kinematic",
                        "pose_source": {"type": "animation_sequence"},
                        "root_transform_source": {"type": "character_movement"},
                    },
                }],
            })
            failures: list[dict] = []

            report = validate_articulated_execution(
                run_dir,
                [{"frame": 0, "time": 0.0, "objects": {"person": {"position": [0.0, 0.0, 0.0]}}}],
                failures,
            )

            self.assertEqual(report["status"], "fail")
            self.assertEqual(failures[0]["code"], "F_ARTICULATED_EXECUTION_FAILED")

    def test_articulated_execution_accepts_actual_character_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self.write_json(run_dir / "case_spec.json", {
                "objects": [{
                    "id": "person",
                    "solver": {
                        "type": "articulated_body",
                        "mode": "kinematic",
                        "pose_source": {"type": "animation_sequence"},
                        "root_transform_source": {"type": "character_movement"},
                    },
                }],
            })
            frames = []
            for index, animation_time in enumerate((0.0, 0.04)):
                frames.append({
                    "frame": index,
                    "time": animation_time,
                    "objects": {"person": {"articulated_observed": {
                        "sample_phase": "post_tick",
                        "sample_time_s": animation_time,
                        "frame_zero_evaluated": True,
                        "pose_source_type": "animation_sequence",
                        "root_transform_source_type": "character_movement",
                        "actor_transform": {"position_m": [animation_time, 0.0, 0.0]},
                        "bones": {"root": {"position_m": [animation_time, 0.0, 0.0]}},
                        "movement_mode": "Walking",
                        "velocity_m_s": [1.0, 0.0, 0.0],
                        "animation_asset_path": "/Game/Test.Walk",
                        "animation_time_s": animation_time,
                        "animation_playing": True,
                        "root_motion_delta": {"translation_m": [animation_time, 0.0, 0.0], "rotation_deg": [0.0, 0.0, 0.0]},
                        "grounding": {
                            "status": "applied",
                            "support_gap_m": 0.0 if index == 0 else 0.25,
                        },
                        "ik": {},
                        "errors": [],
                    }}},
                })
            failures: list[dict] = []

            report = validate_articulated_execution(run_dir, frames, failures)

            self.assertEqual(report["status"], "pass")
            self.assertEqual(failures, [])

            frames[0]["objects"]["person"]["articulated_observed"]["grounding"]["support_gap_m"] = 0.03
            self.assertEqual(validate_articulated_execution(run_dir, frames, [])["status"], "fail")

    def test_articulated_control_layers_require_same_post_tick_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self.write_json(run_dir / "case_spec.json", {
                "objects": [{
                    "id": "person",
                    "solver": {
                        "type": "articulated_body",
                        "mode": "kinematic",
                        "pose_source": {"type": "animation_sequence"},
                        "pose_overlay": {"keyframes": [{}]},
                        "ik_targets": [{"goal": "hand_r", "keyframes": [{}]}],
                        "root_transform_source": {"type": "character_movement", "max_speed_m_s": 1.0},
                    },
                }],
            })
            frames = []
            for index, sample_time in enumerate((0.0, 0.04)):
                frames.append({
                    "frame": index,
                    "time": sample_time,
                    "objects": {"person": {"articulated_observed": {
                        "sample_phase": "post_tick",
                        "sample_time_s": sample_time,
                        "frame_zero_evaluated": True,
                        "pose_source_type": "animation_sequence",
                        "root_transform_source_type": "character_movement",
                        "actor_transform": {"position_m": [sample_time, 0.0, 0.0]},
                        "bones": {"root": {"position_m": [sample_time, 0.0, 0.0]}},
                        "movement_mode": "Walking",
                        "velocity_m_s": [1.0, 0.0, 0.0],
                        "animation_asset_path": "/Game/Test.Walk",
                        "animation_time_s": sample_time,
                        "animation_playing": True,
                        "root_motion_delta": {"translation_m": [sample_time, 0.0, 0.0], "rotation_deg": [0.0, 0.0, 0.0]},
                        "control_rig": {
                            "registered": True,
                            "evaluation_time_s": sample_time,
                            "evaluation_phase": "post_forwards_solve",
                            "pre_initialize_executed": True,
                            "backwards_solve_executed": True,
                            "pre_forwards_solve_executed": True,
                            "forwards_solve_executed": True,
                            "post_forwards_solve_executed": True,
                            "control_write_verified": True,
                            "mesh_output_sampled": True,
                            "coordinate_space": "control_rig_component_lifecycle",
                        },
                        "pose_overlay": {"results": [{
                            "bone": "head",
                            "commanded_rotation_deg": [0.0, 10.0, 0.0],
                            "output_rotation_delta_deg": [0.0, 9.8, 0.0],
                        }]},
                        "ik": {
                            "hand_r": {
                                "weight": 0.5,
                                "switch_value": 1.0,
                                "commanded_target_transform": {"position_m": [0.4, 0.0, 1.2]},
                                "target_control": {"observed_position_m": [0.4, 0.0, 1.2]},
                                "pole": {
                                    "target_position_m": [0.2, 0.0, 1.4],
                                    "observed_position_m": [0.2, 0.0, 1.4],
                                },
                                "effector_transform": {"position_m": [0.0, 0.0, 1.0]},
                                "error_m": 0.45,
                                "tolerance_m": 0.1,
                            }
                        },
                        "errors": [],
                    }}},
                })

            failures: list[dict] = []
            report = validate_articulated_execution(run_dir, frames, failures)
            self.assertEqual(report["status"], "pass")

            frames[0]["objects"]["person"]["articulated_observed"]["ik"]["hand_r"]["pole"]["observed_position_m"][2] = 0.4
            report = validate_articulated_execution(run_dir, frames, [])
            self.assertEqual(report["status"], "fail")
            self.assertIn(
                "ik_pole_control_mismatch",
                {finding["metric"] for finding in report["objects"]["person"]["findings"]},
            )
            frames[0]["objects"]["person"]["articulated_observed"]["ik"]["hand_r"]["pole"]["observed_position_m"][2] = 1.4

            frames[1]["objects"]["person"]["articulated_observed"]["bones"]["root"]["position_m"][2] = 0.5
            report = validate_articulated_execution(run_dir, frames, [])
            self.assertEqual(report["status"], "fail")
            self.assertIn(
                {"metric": "frame_zero_initialization_jump_m", "value": 0.501597, "allowed": 0.09, "source": "mesh_root"},
                report["objects"]["person"]["findings"],
            )
            frames[1]["objects"]["person"]["articulated_observed"]["bones"]["root"]["position_m"][2] = 0.0

            frames[0]["objects"]["person"]["articulated_observed"]["control_rig"]["evaluation_time_s"] = 0.1
            report = validate_articulated_execution(run_dir, frames, [])
            self.assertEqual(report["status"], "fail")

            frames[0]["objects"]["person"]["articulated_observed"]["control_rig"]["evaluation_time_s"] = 0.0
            frames[0]["objects"]["person"]["articulated_observed"]["control_rig"]["control_write_verified"] = False
            report = validate_articulated_execution(run_dir, frames, [])
            self.assertEqual(report["status"], "fail")
            self.assertIn(
                "control_rig_command_write_failed",
                {finding["metric"] for finding in report["objects"]["person"]["findings"]},
            )

            frames[0]["objects"]["person"]["articulated_observed"]["control_rig"]["control_write_verified"] = True
            frames[0]["objects"]["person"]["articulated_observed"]["ik"]["hand_r"]["weight"] = 1.0
            frames[0]["objects"]["person"]["articulated_observed"]["ik"]["hand_r"]["switch_value"] = 1.0
            frames[0]["objects"]["person"]["articulated_observed"]["ik"]["hand_r"]["error_m"] = 0.0
            report = validate_articulated_execution(run_dir, frames, [])
            self.assertEqual(report["status"], "fail")

    def test_solver_declared_measurements_are_contact_evidence_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self.write_json(
                run_dir / "case_spec.json",
                {
                    "expected_physics": {
                        "contact_required": True,
                        "collision_graph": [["fluid", "support"]],
                    },
                    "objects": [],
                },
            )
            self.write_json(
                run_dir / "runtime_plan.json",
                {
                    "stages": [
                        {"id": "solve", "kind": "solve", "outputs": ["trajectory", "declared_measurements"]},
                        {"id": "render", "kind": "render", "outputs": ["render_artifacts"]},
                    ]
                },
            )
            self.write_json(run_dir / "contact_events.json", [])

            failures: list[dict] = []
            report = validate_contacts(run_dir, [], failures)

            self.assertEqual(failures, [])
            self.assertEqual(report["evidence_authority"], "declared_measurements")
            self.assertEqual(report["initial_contact_scope"], "solver_declared_measurements")
            self.assertIsNone(report["initial_expected_contact_free"])

    def test_runtime_constraint_binding_is_compared_as_executed_state(self) -> None:
        constraint = {
            "constraint_id": "joint",
            "body_a": {"object_id": "rod", "runtime_actor_id": "actor_rod"},
            "body_b": {"object_id": "anchor", "runtime_actor_id": "actor_anchor"},
            "frame_a": {"position_m": [0.0, 0.0, 0.5], "primary_axis": [0.0, 1.0, 0.0], "secondary_axis": [0.0, 0.0, 1.0]},
            "frame_b": {"position_m": [0.0, 0.0, 0.0], "primary_axis": [0.0, 1.0, 0.0], "secondary_axis": [0.0, 0.0, 1.0]},
            "linear_motion": {"x": "locked", "y": "locked", "z": "locked"},
            "angular_motion": {"x": "free", "y": "locked", "z": "locked"},
            "angular_limits_deg": {},
            "collision_enabled": False,
        }
        actual = {
            **constraint,
            "component_registered": True,
            "body_bindings_verified": True,
            "configuration_applied": True,
            "broken": False,
        }
        report = validate_runtime_binding_snapshot(
            {"constraints": [constraint]},
            {"runtime_binding_snapshot": {"capture_phase": "pre_simulation", "objects": [], "constraints": [actual]}},
        )
        self.assertEqual(report["status"], "pass")
        actual["body_bindings_verified"] = False
        report = validate_runtime_binding_snapshot(
            {"constraints": [constraint]},
            {"runtime_binding_snapshot": {"capture_phase": "pre_simulation", "objects": [], "constraints": [actual]}},
        )
        self.assertEqual(report["status"], "fail")

    def test_articulated_binding_includes_declared_base_rotation(self) -> None:
        report = validate_runtime_binding_snapshot(
            {
                "dynamic_objects": [{
                    "id": "adult",
                    "initial_position_m": [0.0, 0.0, 0.0],
                    "rotation_degrees": [0.0, 0.0, 0.0],
                    "params": {"base_rotation_degrees": [0.0, 180.0, 0.0], "visible": False},
                    "physics_properties": {"collision_enabled": False, "simulate_physics": False},
                }],
            },
            {
                "runtime_binding_snapshot": {
                    "capture_phase": "pre_simulation",
                    "objects": [{
                        "object_id": "adult",
                        "world_transform": {
                            "position_m": [0.0, 0.0, 0.0],
                            "rotation_deg": [0.0, 180.0, 0.0],
                        },
                        "asset": {
                            "render_mesh_path": None,
                            "collision_mesh_path": None,
                            "asset_id": None,
                            "sha256": None,
                            "binding_metadata_registered": True,
                        },
                        "mesh_component": {
                            "registered": True,
                            "visible": False,
                            "participates_in_rendering": False,
                        },
                        "visible": False,
                        "collision_enabled": False,
                        "simulate_physics": False,
                        "collision_geometry": None,
                    }],
                    "constraints": [],
                },
            },
        )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["residuals"]["adult"]["rotation_deg"], 0.0)

    def test_articulated_character_binding_requires_character_components(self) -> None:
        expected = {
            "id": "adult",
            "behavior": "articulated_character",
            "initial_position_m": [0.0, 0.0, 0.96],
            "rotation_degrees": [0.0, 0.0, 0.0],
            "params": {"base_rotation_degrees": [0.0, 0.0, 0.0], "visible": False},
            "physics_properties": {"collision_enabled": False, "simulate_physics": False},
        }
        actual = {
            "object_id": "adult",
            "world_transform": {"position_m": [0.0, 0.0, 0.96], "rotation_deg": [0.0, 0.0, 0.0]},
            "asset": {
                "render_mesh_path": None,
                "collision_mesh_path": None,
                "asset_id": None,
                "sha256": None,
                "binding_metadata_registered": True,
            },
            "mesh_component": {
                "actor_class": "Character",
                "component_class": "SkeletalMeshComponent",
                "registered": True,
                "visible": False,
                "participates_in_rendering": False,
            },
            "articulated_components": {
                "capsule_registered": True,
                "character_movement_registered": True,
                "control_rig_registered": False,
                "control_rig_path": None,
            },
            "visible": False,
            "collision_enabled": False,
            "simulate_physics": False,
            "collision_geometry": None,
        }

        report = validate_runtime_binding_snapshot(
            {"dynamic_objects": [expected]},
            {"runtime_binding_snapshot": {"capture_phase": "pre_simulation", "objects": [actual], "constraints": []}},
        )

        self.assertEqual(report["status"], "pass")
        actual["articulated_components"]["character_movement_registered"] = False
        report = validate_runtime_binding_snapshot(
            {"dynamic_objects": [expected]},
            {"runtime_binding_snapshot": {"capture_phase": "pre_simulation", "objects": [actual], "constraints": []}},
        )
        self.assertEqual(report["status"], "fail")

        actual["articulated_components"]["character_movement_registered"] = True
        expected["params"]["articulated_body"] = {
            "pose_overlay": {"keyframes": [{}]},
            "control_rig_path": "/Game/Characters/Mannequins/Rigs/CR_Mannequin_Body.CR_Mannequin_Body",
        }
        report = validate_runtime_binding_snapshot(
            {"dynamic_objects": [expected]},
            {"runtime_binding_snapshot": {"capture_phase": "pre_simulation", "objects": [actual], "constraints": []}},
        )
        self.assertEqual(report["status"], "fail")
        actual["articulated_components"]["control_rig_registered"] = True
        actual["articulated_components"]["control_rig_path"] = expected["params"]["articulated_body"]["control_rig_path"]
        report = validate_runtime_binding_snapshot(
            {"dynamic_objects": [expected]},
            {"runtime_binding_snapshot": {"capture_phase": "pre_simulation", "objects": [actual], "constraints": []}},
        )
        self.assertEqual(report["status"], "pass")

    def test_valid_run_passes_hard_gate_and_gets_technical_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            with self.mock_ffprobe():
                report = evaluate_run(run_dir)

            self.assertTrue(report["hard_gate_passed"])
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["solver_execution"]["status"], "pass")
            self.assertEqual(report["solver_execution"]["contact_evidence"], "ue_postsolve_bounds_inference")
            self.assertTrue(report["contacts"]["initial_expected_contact_free"])
            self.assertEqual(report["contacts"]["initial_contact_scope"], "expected_collision_graph")
            self.assertTrue(report["ranking"]["eligible"])
            self.assertIsInstance(report["ranking"]["technical_score"], float)
            self.assertTrue((run_dir / "quality_report.json").is_file())
            quality_stage = self.read_json(run_dir / "stage_results" / "quality_gate.json")
            self.assertEqual(quality_stage["status"], "completed")
            readiness = self.read_json(run_dir / "run_readiness.json")
            self.assertTrue(readiness["physics_ready"])
            self.assertEqual(readiness["physics_provenance"]["status"], "pass")

    def test_rgb_observability_failure_is_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            self.write_json(
                run_dir / "rgb_observability_report.json",
                {
                    "status": "fail",
                    "enforcement": "advisory",
                    "failures": [{"code": "F_RGB_EXPECTED_SUBJECT_NOT_OBSERVABLE"}],
                },
            )
            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            self.assertTrue(report["hard_gate_passed"])
            self.assertIn("W_RGB_OBSERVABILITY_LOW", {item["code"] for item in report["warnings"]})

    def test_runtime_binding_mismatch_is_a_direct_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            summary_path = run_dir / "logs" / "native_data" / "summary.json"
            summary = self.read_json(summary_path)
            summary["runtime_binding_snapshot"]["objects"][0]["collision_geometry"]["size_m"] = [0.4, 0.4, 0.4]
            self.write_json(summary_path, summary)

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            failures = {item["code"]: item for item in report["hard_gate"]["failures"]}
            self.assertIn("F_RUNTIME_BINDING_MISMATCH", failures)
            mismatch_fields = {item["field"] for item in failures["F_RUNTIME_BINDING_MISMATCH"]["mismatches"]}
            self.assertIn("collision_geometry.size_m", mismatch_fields)

    def test_local_preview_requires_rgb_but_not_depth_or_segmentation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            self.write_json(
                run_dir / "execution_profile.json",
                {"name": "local_preview"},
            )
            self.write_json(run_dir / "inputs" / "render_config.json", {"passes": ["rgb"]})
            for path in (
                run_dir / "views" / "front_static" / "depth.exr",
                run_dir / "views" / "front_static" / "segmentation.exr",
            ):
                path.unlink()
            sensor = self.read_json(run_dir / "sensor_state.json")
            sensor["depth"] = {"source": "missing"}
            sensor["segmentation"] = {"instance_level": False}
            self.write_json(run_dir / "sensor_state.json", sensor)
            readiness = self.read_json(run_dir / "run_readiness.json")
            readiness.update(
                {
                    "reference_ready": False,
                    "local_preview_ready": True,
                    "publication_tier": "local_preview",
                }
            )
            self.write_json(run_dir / "run_readiness.json", readiness)

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            self.assertTrue(report["hard_gate_passed"], report["hard_gate"]["failures"])
            self.assertEqual(report["quality_contract"]["required_modalities"], ["rgb"])
            self.assertFalse(report["quality_contract"]["complete_sensor_contract"])
            self.assertEqual(report["depth_geometry"]["status"], "not_required")
            media = report["media"]["views"]["front_static"]
            self.assertEqual(media["depth"]["status"], "not_required")
            self.assertEqual(media["segmentation"]["status"], "not_required")

    def test_highres_viewport_requires_complete_rgb_lighting_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            map_report = self.read_json(run_dir / "map_report.json")
            map_report.pop("rgb_capture")
            self.write_json(run_dir / "map_report.json", map_report)

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            self.assertIn(
                "F_UE_LIGHTING_REPORT_MISSING",
                {item["code"] for item in report["hard_gate"]["failures"]},
            )

    def test_highres_viewport_rejects_active_preview_shadow_indicator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            map_report = self.read_json(run_dir / "map_report.json")
            map_report["rgb_capture"]["viewport_hygiene"]["preview_shadow_indicator_disabled"] = False
            map_report["rgb_capture"]["preview_shadow_safe"] = False
            self.write_json(run_dir / "map_report.json", map_report)

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            self.assertIn(
                "F_UE_PREVIEW_SHADOW_INDICATOR_ACTIVE",
                {item["code"] for item in report["hard_gate"]["failures"]},
            )

    def test_highres_viewport_rejects_non_movable_runtime_light(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            map_report = self.read_json(run_dir / "map_report.json")
            audit = map_report["rgb_capture"]["runtime_light_audit"]
            audit["preview_shadow_safe"] = False
            audit["remaining_non_movable"] = ["StaticSun"]
            map_report["rgb_capture"]["preview_shadow_safe"] = False
            self.write_json(run_dir / "map_report.json", map_report)

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            self.assertIn(
                "F_UE_RUNTIME_LIGHT_MOBILITY_INVALID",
                {item["code"] for item in report["hard_gate"]["failures"]},
            )

    def test_combined_native_pass_is_a_first_class_solver_evidence_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            readiness = self.read_json(run_dir / "run_readiness.json")
            readiness.update(
                {
                    "reference_ready": False,
                    "local_preview_ready": False,
                    "publication_tier": "rejected",
                    "physics_ready": False,
                    "map_ready": True,
                    "camera_plan_ready": True,
                    "multi_view_ready": True,
                    "render_pass_ready": True,
                    "sync_ready": True,
                    "depth_ready": True,
                    "camera_state_ready": True,
                    "sensor_state_ready": True,
                    "verifier_status": "pass",
                    "assets_reference_ready": False,
                    "asset_catalog_reference_ready": False,
                    "local_preview_asset_count": 1,
                }
            )
            self.write_json(run_dir / "run_readiness.json", readiness)
            native_data = run_dir / "logs" / "native_data"
            native_combined = run_dir / "logs" / "native_combined"
            native_data.rename(native_combined)
            self.write_json(
                run_dir / "ue_output" / "summary.json",
                {"status": "completed", "physics_capture": {"enabled": False}},
            )

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            self.assertTrue(report["hard_gate_passed"], report["hard_gate"]["failures"])
            self.assertEqual(report["solver_execution"]["status"], "pass")
            self.assertEqual(report["solver_execution"]["native_summary"], "logs/native_combined/summary.json")

    def test_native_component_hit_is_accepted_as_raw_contact_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            native_dir = run_dir / "logs" / "native_data"
            raw_capture = self.read_json(native_dir / "cpp_physics_capture.json")
            raw_event = raw_capture["frames"][1]["contacts"][0]
            raw_event["method"] = "ue_on_component_hit"
            raw_event["native_collision"] = True
            self.write_json(native_dir / "cpp_physics_capture.json", raw_capture)
            contacts = self.read_json(run_dir / "contact_events.json")
            contacts[0]["method"] = "ue_native_component_hit"
            contacts[0]["raw_method"] = "ue_on_component_hit"
            contacts[0]["native_collision"] = True
            self.write_json(run_dir / "contact_events.json", contacts)

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            self.assertTrue(report["hard_gate_passed"], report["hard_gate"]["failures"])
            self.assertEqual(report["solver_execution"]["contact_evidence"], "ue_native_component_hit")

    def test_high_frequency_solver_trace_maps_to_render_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            timebase = build_timebase(duration_s=1.0, physics_hz=2, render_fps=1)
            runtime = self.read_json(run_dir / "studio_runtime_scene.json")
            runtime["simulation"] = {"duration_s": 1.0, "fps": 1, **timebase}
            self.write_json(run_dir / "studio_runtime_scene.json", runtime)

            def state(frame: int, contact: bool = False) -> dict:
                position_cm = frame * 50.0
                return {
                    "frame": frame,
                    "time": frame / 2,
                    "source": "adp_cpp_runtime_driver",
                    "objects": {
                        "ball_a": {
                            "position": [position_cm / 100, 0.0, 0.0],
                            "position_cm": [position_cm, 0.0, 0.0],
                            "rotation_degrees": [0.0, 0.0, 0.0],
                            "velocity_cm_s": [100.0, 0.0, 0.0],
                            "source": "adp_cpp_runtime_driver",
                        }
                    },
                    "contacts": ([{
                        "objects": ["ball_a", "ball_b"],
                        "method": "ue_postsolve_bounds_inference",
                        "raw_method": "adp_cpp_runtime_oriented_box_sat",
                    }] if contact else []),
                }

            solver = [state(0), state(1, contact=True), state(2)]
            canonical, contacts = sample_solver_trajectory(solver, timebase)
            self.write_json(run_dir / "solver_trajectory.json", solver)
            self.write_json(run_dir / "trajectory.json", canonical)
            self.write_json(run_dir / "contact_events.json", contacts)
            solver_sha = hashlib.sha256((run_dir / "solver_trajectory.json").read_bytes()).hexdigest()
            self.write_json(
                run_dir / "sampling_map.json",
                {
                    "timebase": timebase,
                    "solver_cache_sha256": solver_sha,
                    "samples": [
                        {"frame": index, "source_solver_frame": source}
                        for index, source in enumerate(timebase["source_solver_indices"])
                    ],
                },
            )
            native_dir = run_dir / "logs" / "native_data"
            raw = self.read_json(native_dir / "cpp_physics_capture.json")
            raw["frame_count"] = 3
            raw["requested_max_frames"] = 3
            raw["sample_interval_s"] = 0.5
            raw["frames"] = [
                {**frame, "contacts": [
                    {"objects": event["objects"], "method": event["raw_method"]}
                    for event in frame["contacts"]
                ]}
                for frame in solver
            ]
            self.write_json(native_dir / "cpp_physics_capture.json", raw)
            summary = self.read_json(native_dir / "summary.json")
            summary["physics_capture"]["cpp_runtime_driver"].update(
                {"trajectory_frames": 2, "solver_trajectory_frames": 3}
            )
            self.write_json(native_dir / "summary.json", summary)

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            self.assertTrue(report["hard_gate_passed"], report["hard_gate"]["failures"])
            self.assertEqual(report["solver_execution"]["timebase"]["substeps_per_render"], 2)
            self.assertEqual(contacts[0]["source_solver_frame"], 1)

    def test_render_boundary_capture_cannot_claim_uncaptured_substep_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            timebase = build_timebase(duration_s=1.0, physics_hz=2, render_fps=1)
            runtime = self.read_json(run_dir / "studio_runtime_scene.json")
            runtime["simulation"] = {
                "duration_s": 1.0,
                "fps": 1,
                **timebase,
                "physics_step_count": 2,
                "full_solver_frame_count": 3,
                "solver_frame_count": 3,
                "raw_capture_frame_count": 2,
                "solver_capture_mode": "render_boundary",
            }
            self.write_json(run_dir / "studio_runtime_scene.json", runtime)

            trajectory = self.read_json(run_dir / "trajectory.json")
            for frame_index, frame in enumerate(trajectory):
                frame["source_solver_frame"] = frame_index
                frame["source_physics_step"] = frame_index * 2
            self.write_json(run_dir / "trajectory.json", trajectory)
            self.write_json(run_dir / "solver_trajectory.json", trajectory)
            solver_sha = hashlib.sha256((run_dir / "solver_trajectory.json").read_bytes()).hexdigest()
            self.write_json(
                run_dir / "sampling_map.json",
                {
                    "timebase": runtime["simulation"],
                    "solver_cache_sha256": solver_sha,
                    "samples": [
                        {"frame": frame_index, "source_solver_frame": frame_index}
                        for frame_index in range(2)
                    ],
                },
            )
            summary_path = run_dir / "logs" / "native_data" / "summary.json"
            summary = self.read_json(summary_path)
            summary["physics_capture"]["physics_substepping"] = {
                "enabled": True,
                "max_substeps": 2,
                "max_substep_delta_time_s": 0.5,
            }
            summary["physics_capture"]["cpp_runtime_driver"]["solver_trajectory_frames"] = 2
            self.write_json(summary_path, summary)

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            self.assertFalse(report["hard_gate_passed"])
            self.assertIn(
                "declared_solver_frame_count_mismatch",
                report["solver_execution"]["violations"],
            )

    def test_legacy_png_containing_exr_is_a_format_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            segmentation = run_dir / "views" / "front_static" / "segmentation.exr"
            segmentation.unlink()
            segmentation.with_suffix(".png").write_bytes(EXR_MAGIC + b"legacy")

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=True)

            codes = {item["code"] for item in report["hard_gate"]["failures"]}
            self.assertIn("F_SEGMENTATION_EXTENSION_MISMATCH", codes)
            self.assertFalse(report["hard_gate_passed"])
            self.assertFalse(report["ranking"]["eligible"])
            self.assertIsNone(report["ranking"]["technical_score"])

    def test_positive_collision_at_frame_zero_fails_hard_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp), contact_frame=0)
            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            codes = {item["code"] for item in report["hard_gate"]["failures"]}
            self.assertIn("F_CONTACT_AT_INITIAL_FRAME", codes)
            self.assertFalse(report["hard_gate_passed"])

    def test_native_segmentation_count_mismatch_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            self.write_json(
                run_dir / "render_manifest.json",
                {
                    "passes": {
                        "rgb": {"views": [{"view_id": "front_static", "frame_count": 2}]},
                        "depth": {"views": [{"view_id": "front_static", "frame_count": 2}]},
                        "segmentation": {"views": [{"view_id": "front_static", "frame_count": 1}]},
                    }
                },
            )
            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            codes = {item["code"] for item in report["hard_gate"]["failures"]}
            self.assertIn("F_SEGMENTATION_FRAME_COUNT_MISMATCH", codes)

    def test_failed_quality_gate_revokes_preview_without_erasing_physics_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp), contact_frame=0)
            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=True)

            readiness = self.read_json(run_dir / "run_readiness.json")
            self.assertFalse(report["hard_gate_passed"])
            self.assertTrue(readiness["physics_ready"])
            self.assertFalse(readiness["quality_gate_passed"])
            self.assertFalse(readiness["local_preview_ready"])
            self.assertFalse(readiness["reference_ready"])
            self.assertEqual(readiness["publication_tier"], "rejected")

    def test_segmentation_matching_rgb_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            view_dir = run_dir / "views" / "front_static"
            frame = view_dir / "segmentation_frames" / "frame_0000.exr"
            frame.parent.mkdir()
            frame.write_bytes(EXR_MAGIC + b"mask")
            meta = self.read_json(view_dir / "meta.json")
            meta.update(
                {
                    "instance_count": 2,
                    "instance_mapping": [{"instance_id": 1}, {"instance_id": 2}],
                    "segmentation_frames": [str(frame.relative_to(run_dir))],
                }
            )
            self.write_json(view_dir / "meta.json", meta)
            raw_frame = b"".join(
                b"\x14\x28\x3c" if index % 2 else b"\x50\x64\x78"
                for index in range(64 * 64)
            )

            with self.mock_ffprobe(rgb_frame=raw_frame, segmentation_frame=raw_frame):
                report = evaluate_run(run_dir, write=False)

            codes = {item["code"] for item in report["hard_gate"]["failures"]}
            self.assertIn("F_SEGMENTATION_RGB_DUPLICATE", codes)
            self.assertFalse(report["hard_gate_passed"])

    def test_constant_depth_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            view_dir = run_dir / "views" / "front_static"
            frame = view_dir / "depth_frames" / "frame_0000.exr"
            frame.parent.mkdir()
            frame.write_bytes(EXR_MAGIC + b"depth")
            meta = self.read_json(view_dir / "meta.json")
            meta["depth_frames"] = [str(frame.relative_to(run_dir))]
            self.write_json(view_dir / "meta.json", meta)

            with self.mock_ffprobe(), patch("harness.verification.run_quality.depth_pixel_statistics", return_value={"minimum": 1.0, "maximum": 1.0, "variance": 0.0}):
                report = evaluate_run(run_dir, write=False)

            codes = {item["code"] for item in report["hard_gate"]["failures"]}
            self.assertIn("F_DEPTH_CONSTANT", codes)

    def test_segmentation_with_excessive_color_categories_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            view_dir = run_dir / "views" / "front_static"
            frame = view_dir / "segmentation_frames" / "frame_0000.exr"
            frame.parent.mkdir()
            frame.write_bytes(EXR_MAGIC + b"mask")
            meta = self.read_json(view_dir / "meta.json")
            meta.update(
                {
                    "instance_count": 2,
                    "instance_mapping": [{"instance_id": 1}, {"instance_id": 2}],
                    "segmentation_frames": [str(frame.relative_to(run_dir))],
                }
            )
            self.write_json(view_dir / "meta.json", meta)
            segmentation = b"".join(
                bytes((index % 256, (index // 256) % 256, (index * 17) % 256))
                for index in range(64 * 64)
            )
            rgb = b"\0" * len(segmentation)

            with self.mock_ffprobe(rgb_frame=rgb, segmentation_frame=segmentation):
                report = evaluate_run(run_dir, write=False)

            codes = {item["code"] for item in report["hard_gate"]["failures"]}
            self.assertIn("F_SEGMENTATION_COLOR_CARDINALITY", codes)
            self.assertFalse(report["hard_gate_passed"])

    def test_quantized_segmentation_requires_palette_closure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            view_dir = run_dir / "views" / "front_static"
            frame = view_dir / "segmentation_frames" / "frame_0000.exr"
            frame.parent.mkdir()
            frame.write_bytes(EXR_MAGIC + b"mask")
            meta = self.read_json(view_dir / "meta.json")
            meta.update(
                {
                    "instance_count": 1,
                    "instance_mapping": [{"instance_id": 1, "rgb": [1.0, 0.0, 0.0]}],
                    "segmentation_frames": [str(frame.relative_to(run_dir))],
                    "segmentation_palette_quantized": True,
                    "segmentation_palette_closure": True,
                    "segmentation_palette_rgb8": [[0, 0, 0], [255, 0, 0]],
                }
            )
            self.write_json(view_dir / "meta.json", meta)
            segmentation = b"\xff\x00\x00" * (64 * 64 - 1) + b"\x00\x00\xff"
            rgb = b"\0" * len(segmentation)

            with self.mock_ffprobe(rgb_frame=rgb, segmentation_frame=segmentation):
                report = evaluate_run(run_dir, write=False)

            codes = {item["code"] for item in report["hard_gate"]["failures"]}
            self.assertIn("F_SEGMENTATION_PALETTE_CLOSURE", codes)

    def test_quantized_segmentation_allows_a_full_foreground_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            view_dir = run_dir / "views" / "front_static"
            frame = view_dir / "segmentation_frames" / "frame_0000.exr"
            frame.parent.mkdir()
            frame.write_bytes(EXR_MAGIC + b"mask")
            meta = self.read_json(view_dir / "meta.json")
            meta.update(
                {
                    "instance_count": 1,
                    "instance_mapping": [{"instance_id": 1, "rgb": [1.0, 0.0, 0.0]}],
                    "segmentation_frames": [str(frame.relative_to(run_dir))],
                    "segmentation_palette_quantized": True,
                    "segmentation_palette_closure": True,
                    "segmentation_palette_rgb8": [[0, 0, 0], [255, 0, 0]],
                }
            )
            self.write_json(view_dir / "meta.json", meta)
            segmentation = b"\xff\x00\x00" * (64 * 64)
            rgb = b"\0" * len(segmentation)

            with self.mock_ffprobe(rgb_frame=rgb, segmentation_frame=segmentation):
                report = evaluate_run(run_dir, write=False)

            codes = {item["code"] for item in report["hard_gate"]["failures"]}
            self.assertNotIn("F_SEGMENTATION_PALETTE_CLOSURE", codes)

    def test_named_spread_metadata_does_not_enable_hidden_quality_gate(self) -> None:
        for expected_spread in ("full_rack_break", "angled_rack_break"):
            with self.subTest(expected_spread=expected_spread), tempfile.TemporaryDirectory() as tmp:
                run_dir = self.make_run(Path(tmp))
                case = self.read_json(run_dir / "case_spec.json")
                case["expected_physics"]["expected_spread"] = expected_spread
                case["passive_objects"] = ["ball_b", "ball_c"]
                case["objects"].append({"id": "ball_c", "role": "passive_target"})
                self.write_json(run_dir / "case_spec.json", case)
                self.write_json(
                    run_dir / "trajectory.json",
                    [
                        {"frame": 0, "time": 0.0, "objects": {"ball_a": {"position": [0, 0, 0]}, "ball_b": {"position": [0, 0, 0]}, "ball_c": {"position": [0, 0, 0]}}},
                        {"frame": 1, "time": 1.0, "objects": {"ball_a": {"position": [1, 0, 0]}, "ball_b": {"position": [0.02, 0, 0]}, "ball_c": {"position": [0, 0, 0]}}},
                    ],
                )
                with self.mock_ffprobe():
                    report = evaluate_run(run_dir, write=False)

                codes = {item["code"] for item in report["hard_gate"]["failures"]}
                self.assertNotIn("F_FULL_RACK_CONTACT_INCOMPLETE", codes)
                self.assertNotIn("F_FULL_RACK_MOTION_INCOMPLETE", codes)
                self.assertNotIn("complete_passive_propagation", report["contacts"])

    def test_precomputed_replay_cannot_pass_initial_state_live_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            case = self.read_json(run_dir / "case_spec.json")
            case["expected_physics"].pop("simulation_contract")
            self.write_json(run_dir / "case_spec.json", case)
            runtime_scene = self.read_json(run_dir / "studio_runtime_scene.json")
            runtime_scene["precomputed_trajectory"] = self.read_json(run_dir / "trajectory.json")
            runtime_scene["physics_controls"].update(
                {
                    "simulate_physics": False,
                    "simulation_driver": "mujoco_rigid",
                    "runtime_driver_backend": "precomputed_trajectory",
                    "cpp_runtime_driver_enabled": False,
                }
            )
            self.write_json(run_dir / "studio_runtime_scene.json", runtime_scene)

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=True)

            codes = {item["code"] for item in report["hard_gate"]["failures"]}
            self.assertIn("F_RIGID_SOLVER_PROVENANCE", codes)
            self.assertEqual(report["solver_execution"]["status"], "fail")
            self.assertEqual(report["solver_execution"]["contract_source"], "backend_policy")
            self.assertIn("precomputed_trajectory_present", report["solver_execution"]["violations"])
            readiness = self.read_json(run_dir / "run_readiness.json")
            self.assertFalse(readiness["physics_ready"])
            self.assertFalse(readiness["reference_ready"])
            self.assertFalse(readiness["local_preview_ready"])
            self.assertEqual(readiness["publication_tier"], "rejected")

    def test_kinematic_driver_object_does_not_require_live_chaos_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            runtime_scene = self.read_json(run_dir / "studio_runtime_scene.json")
            actor = runtime_scene["dynamic_objects"][0]
            actor["behavior"] = "articulated_kinematic"
            actor["physics_properties"].update(
                {
                    "kinematic": True,
                    "simulate_physics": False,
                    "collision_enabled": False,
                    "collision_geometry": None,
                }
            )
            actor["params"]["expected_collision_mesh_path"] = None
            self.write_json(run_dir / "studio_runtime_scene.json", runtime_scene)
            summary_path = run_dir / "logs" / "native_data" / "summary.json"
            summary = self.read_json(summary_path)
            snapshot = summary["runtime_binding_snapshot"]["objects"][0]
            snapshot["asset"]["collision_mesh_path"] = None
            snapshot.update(
                {
                    "simulate_physics": False,
                    "collision_enabled": False,
                    "collision_geometry": None,
                }
            )
            summary["chaos_runtime"]["actors"][0].update(
                {"simulate_physics": False, "collision_enabled": False}
            )
            self.write_json(summary_path, summary)

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            self.assertEqual(report["solver_execution"]["status"], "pass")
            self.assertNotIn(
                "F_RIGID_SOLVER_PROVENANCE",
                {item["code"] for item in report["hard_gate"]["failures"]},
            )

    def test_provenance_override_records_solver_failure_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            case = self.read_json(run_dir / "case_spec.json")
            case["expected_physics"].pop("simulation_contract")
            self.write_json(run_dir / "case_spec.json", case)
            runtime_scene = self.read_json(run_dir / "studio_runtime_scene.json")
            runtime_scene["precomputed_trajectory"] = self.read_json(run_dir / "trajectory.json")
            runtime_scene["physics_controls"].update(
                {
                    "simulate_physics": False,
                    "simulation_driver": "mujoco_rigid",
                    "runtime_driver_backend": "precomputed_trajectory",
                    "cpp_runtime_driver_enabled": False,
                }
            )
            self.write_json(run_dir / "studio_runtime_scene.json", runtime_scene)
            self.write_json(
                run_dir / "quality_policy.json",
                {
                    "schema_version": "harness_quality_policy_v1",
                    "ignore_provenance_and_release_gates": True,
                },
            )

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=True)

            self.assertTrue(report["hard_gate_passed"])
            self.assertEqual(report["solver_execution"]["status"], "fail")
            self.assertNotIn("F_RIGID_SOLVER_PROVENANCE", {item["code"] for item in report["hard_gate"]["failures"]})
            warning = next(item for item in report["warnings"] if item.get("original_code") == "F_RIGID_SOLVER_PROVENANCE")
            self.assertEqual(warning["code"], "W_PROVENANCE_AND_RELEASE_GATE_IGNORED")
            readiness = self.read_json(run_dir / "run_readiness.json")
            self.assertTrue(readiness["quality_gate_passed"])
            self.assertTrue(readiness["local_preview_ready"])
            self.assertTrue(readiness["provenance_and_release_gate_override"])

    def test_solver_provenance_uses_runtime_selected_by_native_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            replay_path = run_dir / "logs" / "studio_runtime_scene_rgb.json"
            replay = self.read_json(run_dir / "studio_runtime_scene.json")
            replay["precomputed_trajectory"] = self.read_json(run_dir / "trajectory.json")
            replay["physics_controls"]["simulate_physics"] = False
            self.write_json(replay_path, replay)
            summary_path = run_dir / "logs" / "native_data" / "summary.json"
            summary = self.read_json(summary_path)
            summary["studio_runtime_scene"] = {"path": str(replay_path)}
            self.write_json(summary_path, summary)

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            self.assertEqual(report["solver_execution"]["runtime_scene"], "logs/studio_runtime_scene_rgb.json")
            self.assertIn("precomputed_trajectory_present", report["solver_execution"]["violations"])

    def test_injected_canonical_contact_fails_solver_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            capture_path = run_dir / "logs" / "native_data" / "cpp_physics_capture.json"
            capture = self.read_json(capture_path)
            capture["frames"][1]["contacts"] = []
            self.write_json(capture_path, capture)

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            self.assertIn("canonical_contacts_not_derived_from_cpp_capture", report["solver_execution"]["violations"])

    def test_raw_frame_time_mismatch_fails_solver_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            capture_path = run_dir / "logs" / "native_data" / "cpp_physics_capture.json"
            capture = self.read_json(capture_path)
            capture["frames"][1]["time"] = 1.25
            self.write_json(capture_path, capture)

            with self.mock_ffprobe():
                report = evaluate_run(run_dir, write=False)

            self.assertIn("solver_frame_time_mismatch", report["solver_execution"]["violations"])

    def test_ue_physics_readiness_rejects_policy_default_replay(self) -> None:
        from harness.runtime.ue_backend import evaluate_ue_physics_readiness

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self.make_run(Path(tmp))
            case = self.read_json(run_dir / "case_spec.json")
            case["expected_physics"].pop("simulation_contract")
            self.write_json(run_dir / "case_spec.json", case)
            runtime_scene = self.read_json(run_dir / "studio_runtime_scene.json")
            runtime_scene["precomputed_trajectory"] = self.read_json(run_dir / "trajectory.json")
            self.write_json(run_dir / "studio_runtime_scene.json", runtime_scene)

            ready, provenance = evaluate_ue_physics_readiness(run_dir)

            self.assertFalse(ready)
            self.assertEqual(provenance["status"], "fail")
            self.assertEqual(provenance["contract_source"], "backend_policy")

    def make_run(self, root: Path, *, contact_frame: int = 1) -> Path:
        run_dir = root / "run"
        view_dir = run_dir / "views" / "front_static"
        view_dir.mkdir(parents=True)
        self.write_json(
            run_dir / "run_readiness.json",
            {
                "backend": "ue",
                "reference_ready": True,
                "local_preview_ready": True,
                "publication_tier": "reference",
                "physics_ready": True,
                "view_count": 1,
            },
        )
        self.write_json(run_dir / "harness_verifier.json", {"status": "pass", "failure_type": None})
        self.write_json(
            run_dir / "runtime_plan.json",
            {
                "stages": [
                    {
                        "id": "solve_render",
                        "kind": "solve_render",
                        "outputs": ["trajectory", "render_artifacts", "signals", "contact_events"],
                    }
                ]
            },
        )
        self.write_json(
            run_dir / "inputs" / "render_config.json",
            {"passes": ["rgb", "depth", "segmentation"]},
        )
        self.write_json(
            run_dir / "render_sync_report.json",
            {
                "status": "pass",
                "multi_view_sync_ok": True,
                "render_pass_valid": True,
                "views": {"front_static": {"rgb_path": "views/front_static/rgb.mp4"}},
            },
        )
        self.write_json(
            run_dir / "map_report.json",
            {
                "status": "pass",
                "map_opened": True,
                "selected_map": {"path": "/Game/Test.Test"},
                "rgb_capture": {
                    "backend": "highres_viewport",
                    "existing_map_lights": {
                        "inspected": 1,
                        "normalized_to_movable": 1,
                        "failures": [],
                        "preview_shadow_safe": True,
                    },
                    "runtime_light_audit": {
                        "inspected": 1,
                        "enabled": 1,
                        "normalized_to_movable": 0,
                        "remaining_non_movable": [],
                        "failures": [],
                        "preview_shadow_safe": True,
                    },
                    "viewport_hygiene": {
                        "game_view_after": True,
                        "preview_shadow_indicator_disabled": True,
                        "failures": [],
                        "preview_shadow_safe": True,
                    },
                    "preview_shadow_safe": True,
                },
            },
        )
        self.write_json(run_dir / "sensor_state.json", {"frame_count": 2, "views": [{"camera_id": "front_static"}], "depth": {"source": "ue_scene_capture"}, "segmentation": {"instance_level": True}})
        self.write_json(
            run_dir / "asset_resolution.json",
            {"assets": [{"selected_asset": {"asset_id": "ball", "proxy": False}}]},
        )
        self.write_json(
            run_dir / "case_spec.json",
            {
                "capability_id": "rigid_body_contact_causality",
                "should_pass": True,
                "negative_or_boundary": False,
                "expected_physics": {
                    "collision_graph": [["ball_a", "ball_b"]],
                    "simulation_contract": {
                        "input_mode": "initial_state_only",
                        "state_solver": "ue_chaos",
                        "trajectory_role": "solver_output_render_cache",
                    },
                },
                "objects": [
                    {"id": "ball_a", "role": "active_striker"},
                    {"id": "ball_b", "role": "passive_target"},
                ],
            },
        )
        self.write_json(
            run_dir / "trajectory.json",
            [
                {
                    "frame": 0,
                    "time": 0.0,
                    "source": "adp_cpp_runtime_driver",
                    "objects": {
                        "ball_a": {
                            "position": [0.0, 0.0, 0.0],
                            "position_cm": [0.0, 0.0, 0.0],
                            "rotation_degrees": [0.0, 0.0, 0.0],
                            "velocity_cm_s": [100.0, 0.0, 0.0],
                            "source": "adp_cpp_runtime_driver",
                        }
                    },
                },
                {
                    "frame": 1,
                    "time": 1.0,
                    "source": "adp_cpp_runtime_driver",
                    "objects": {
                        "ball_a": {
                            "position": [1.0, 0.0, 0.0],
                            "position_cm": [100.0, 0.0, 0.0],
                            "rotation_degrees": [0.0, 0.0, 0.0],
                            "velocity_cm_s": [100.0, 0.0, 0.0],
                            "source": "adp_cpp_runtime_driver",
                        }
                    },
                },
            ],
        )
        self.write_json(
            run_dir / "studio_runtime_scene.json",
            {
                "physics_controls": {
                    "simulate_physics": True,
                    "simulation_driver": "adp_cpp_runtime_driver",
                    "runtime_driver_backend": "cpp_runtime_driver",
                    "cpp_runtime_driver_enabled": True,
                    "deterministic_replay_fallback": False,
                },
                "dynamic_objects": [
                    {
                        "id": "ball_a",
                        "initial_position_m": [0.0, 0.0, 0.0],
                        "rotation_degrees": [0.0, 0.0, 0.0],
                        "params": {
                            "visible": True,
                            "expected_render_mesh_path": "/Engine/BasicShapes/Sphere.Sphere",
                            "expected_collision_mesh_path": "/Engine/BasicShapes/Sphere.Sphere",
                            "expected_asset_id": None,
                            "expected_asset_sha256": None,
                        },
                        "physics_properties": {
                            "initial_velocity_m_s": [1.0, 0.0, 0.0],
                            "collision_enabled": True,
                            "simulate_physics": True,
                            "collision_geometry": {
                                "shape": "sphere",
                                "size_m": [0.2, 0.2, 0.2],
                                "local_center_offset_m": [0.0, 0.0, 0.0],
                                "world_center_m": [0.0, 0.0, 0.0],
                            },
                        },
                    }
                ],
                "precomputed_trajectory": [],
            },
        )
        native_dir = run_dir / "logs" / "native_data"
        self.write_json(
            native_dir / "summary.json",
            {
                "frames": 2,
                "studio_runtime_scene": {"path": str(run_dir / "studio_runtime_scene.json")},
                "runtime_initial_transforms": {
                    "ball_a": {"position_cm": [0.0, 0.0, 0.0], "rotation_degrees": [0.0, 0.0, 0.0]}
                },
                "runtime_binding_snapshot": {
                    "schema_version": "harness_ue_runtime_binding_snapshot_v1",
                    "capture_phase": "pre_simulation",
                    "objects": [{
                        "object_id": "ball_a",
                        "world_transform": {
                            "position_m": [0.0, 0.0, 0.0],
                            "rotation_deg": [0.0, 0.0, 0.0],
                        },
                        "asset": {
                            "render_mesh_path": "/Engine/BasicShapes/Sphere.Sphere",
                            "collision_mesh_path": "/Engine/BasicShapes/Sphere.Sphere",
                            "asset_id": None,
                            "sha256": None,
                            "binding_metadata_registered": True,
                        },
                        "mesh_component": {
                            "registered": True,
                            "visible": True,
                            "participates_in_rendering": True,
                        },
                        "visible": True,
                        "collision_enabled": True,
                        "simulate_physics": True,
                        "collision_geometry": {
                            "shape": "sphere",
                            "size_m": [0.2, 0.2, 0.2],
                            "local_center_offset_m": [0.0, 0.0, 0.0],
                            "world_center_m": [0.0, 0.0, 0.0],
                        },
                    }],
                },
                "chaos_runtime": {
                    "controls": {
                        "simulate_physics": True,
                        "runtime_driver_backend": "cpp_runtime_driver",
                        "cpp_runtime_driver_enabled": True,
                        "deterministic_replay_fallback": False,
                    },
                    "actors": [
                        {
                            "id": "ball_a",
                            "role": "dynamic",
                            "simulate_physics": True,
                            "collision_enabled": True,
                            "errors": [],
                        }
                    ],
                },
                "physics_capture": {
                    "enabled": True,
                    "game_world_count": 1,
                    "initial_state_reset": True,
                    "actual_frame_count": 2,
                    "cpp_runtime_driver": {
                        "started": True,
                        "capture_complete": True,
                        "registered_dynamic": ["ball_a"],
                        "trajectory_frames": 2,
                    },
                },
            },
        )
        self.write_json(
            native_dir / "cpp_physics_capture.json",
            {
                "driver": "ADPPhysicsRuntimeDriver",
                "frame_count": 2,
                "requested_max_frames": 2,
                "sample_interval_s": 1.0,
                "capture_complete": True,
                "frames": [
                    {
                        "frame": 0,
                        "time": 0.0,
                        "source": "adp_cpp_runtime_driver",
                        "objects": {
                            "ball_a": {
                                "position_cm": [0.0, 0.0, 0.0],
                                "rotation_degrees": [0.0, 0.0, 0.0],
                                "velocity_cm_s": [100.0, 0.0, 0.0],
                                "source": "adp_cpp_runtime_driver",
                            }
                        },
                        "contacts": [] if contact_frame != 0 else [{"objects": ["ball_a", "ball_b"], "method": "adp_cpp_runtime_oriented_box_sat"}],
                    },
                    {
                        "frame": 1,
                        "time": 1.0,
                        "source": "adp_cpp_runtime_driver",
                        "objects": {
                            "ball_a": {
                                "position_cm": [100.0, 0.0, 0.0],
                                "rotation_degrees": [0.0, 0.0, 0.0],
                                "velocity_cm_s": [100.0, 0.0, 0.0],
                                "source": "adp_cpp_runtime_driver",
                            }
                        },
                        "contacts": [] if contact_frame != 1 else [{"objects": ["ball_a", "ball_b"], "method": "adp_cpp_runtime_oriented_box_sat"}],
                    },
                ],
            },
        )
        self.write_json(
            run_dir / "contact_events.json",
            [{
                "frame": contact_frame,
                "objects": ["ball_a", "ball_b"],
                "method": "ue_postsolve_bounds_inference",
                "raw_method": "adp_cpp_runtime_oriented_box_sat",
            }],
        )
        self.write_json(
            run_dir / "camera_trajectory.json",
            {"views": [{"view_id": "front_static", "camera_mode": "fixed", "frames": [{"location_cm": [0, 0, 100]}, {"location_cm": [0, 0, 100]}]}]},
        )
        (view_dir / "rgb.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom")
        (view_dir / "depth.exr").write_bytes(EXR_MAGIC + b"depth")
        (view_dir / "segmentation.exr").write_bytes(EXR_MAGIC + b"mask")
        self.write_json(
            view_dir / "meta.json",
            {
                "frame_count_rgb": 2,
                "frame_count_depth": 2,
                "frame_count_segmentation": 2,
                "timestamps_rgb": [0.0, 0.5],
                "timestamps_depth": [0.0, 0.5],
                "timestamps_segmentation": [0.0, 0.5],
            },
        )
        return run_dir

    def mock_ffprobe(self, *, rgb_frame: bytes | None = None, segmentation_frame: bytes | None = None):
        payload = {
            "streams": [
                {
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "2/1",
                    "nb_frames": "2",
                    "bit_rate": "3000000",
                }
            ],
            "format": {"duration": "1.0", "size": "400000", "bit_rate": "3000000"},
        }
        if rgb_frame is None or segmentation_frame is None:
            return patch(
                "harness.verification.run_quality.subprocess.run",
                return_value=subprocess.CompletedProcess(args=["ffprobe"], returncode=0, stdout=json.dumps(payload), stderr=""),
            )

        def run(command, **_kwargs):
            if command[0] == "ffmpeg":
                source = Path(command[command.index("-i") + 1])
                output = rgb_frame if source.suffix == ".mp4" else segmentation_frame
                return subprocess.CompletedProcess(args=command, returncode=0, stdout=output, stderr=b"")
            return subprocess.CompletedProcess(args=command, returncode=0, stdout=json.dumps(payload), stderr="")

        return patch("harness.verification.run_quality.subprocess.run", side_effect=run)

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
