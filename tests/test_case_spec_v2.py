from __future__ import annotations

import unittest

from harness.core.case_spec_v2 import (
    CaseSpecV2ValidationError,
    case_spec_v2_from_dict,
    project_case_spec_v2_to_v1,
)
from tests.case_spec_v2_fixture import case_spec_v2_fixture


class CaseSpecV2Tests(unittest.TestCase):
    def test_defaults_validation_and_v1_projection(self) -> None:
        data = case_spec_v2_fixture()
        del data["timebase"]["deterministic_seed"]
        case = case_spec_v2_from_dict(data)
        projection = project_case_spec_v2_to_v1(case)

        self.assertEqual(case.data["timebase"]["deterministic_seed"], 42)
        self.assertEqual(projection.data["schema_version"], "harness_case_spec_v1")
        self.assertEqual(projection.case_id, "v2_ball_contact")
        self.assertEqual(projection.data["active_objects"], ["cue_ball"])
        self.assertIn("target_ball", projection.data["passive_objects"])
        self.assertTrue(projection.data["objects"][0]["force_analytic_proxy"])
        self.assertEqual(projection.data["timebase"]["render_fps"], 24)
        self.assertEqual(projection.data["seed"], 42)
        self.assertEqual(projection.data["v2_projection"]["source_schema_version"], "harness_case_spec_v2")
        self.assertEqual(projection.data["objects"][0]["body_type"], "dynamic")
        self.assertTrue(projection.data["objects"][0]["collision_required"])
        self.assertEqual(projection.data["objects"][2]["body_type"], "static")

    def test_text_can_require_model_generation_without_reference_image(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["asset"] = {
            "description": "a ball generated from the textual design",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "model_generation",
                "requirement": "required",
                "origin": "user_explicit",
                "reference_inputs": [],
                "fallback_order": [],
            },
        }
        case = case_spec_v2_from_dict(data)
        acquisition = case.data["objects"][0]["asset"]["acquisition"]
        self.assertEqual(acquisition["route"], "model_generation")
        self.assertEqual(acquisition["reference_inputs"], [])

    def test_reference_image_can_be_generation_only_and_not_similarity_search(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["asset"] = {
            "description": "reconstruct the object shown in the reference",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "model_generation",
                "requirement": "required",
                "origin": "user_explicit",
                "reference_inputs": [
                    {
                        "input_id": "request_image_0",
                        "usage": ["generation_condition", "geometry_reference"],
                        "allow_similarity_search": False,
                    }
                ],
                "fallback_order": [],
            },
        }
        case = case_spec_v2_from_dict(data, available_input_ids=["request_image_0"])
        reference = case.data["objects"][0]["asset"]["acquisition"]["reference_inputs"][0]
        self.assertFalse(reference["allow_similarity_search"])

    def test_llm_inferred_route_cannot_become_a_hard_requirement(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["asset"] = {
            "description": "a custom ball",
            "acquisition": {
                "route": "model_generation",
                "requirement": "required",
                "origin": "llm_inferred",
            },
        }
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("inferred_hard_requirement", {issue.code for issue in context.exception.issues})

    def test_system_default_route_cannot_become_a_hard_requirement(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["asset"] = {
            "description": "a custom ball",
            "acquisition": {
                "route": "model_generation",
                "requirement": "required",
            },
        }
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("inferred_hard_requirement", {issue.code for issue in context.exception.issues})

    def test_cross_field_validator_reports_unknown_object_reference(self) -> None:
        data = case_spec_v2_fixture()
        data["relations"][0]["target"] = "missing_ball"
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        issue = next(issue for issue in context.exception.issues if issue.code == "unknown_object_reference")
        self.assertEqual(issue.path, "/relations/0")

    def test_cross_field_validator_checks_declared_kinetic_energy(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["behavior"]["initial_kinetic_energy_j"] = 99.0
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("kinetic_energy_mismatch", {issue.code for issue in context.exception.issues})

    def test_box_ramp_must_use_pitch_or_roll_not_yaw_only(self) -> None:
        data = case_spec_v2_fixture()
        ramp = data["objects"][2]
        ramp["role"] = "static inclined ramp"
        ramp["geometry"]["shape_hint"] = "box"
        ramp["initial_state"]["rotation_deg"] = [0.0, -12.0, 0.0]
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("ramp_has_no_incline_rotation", {issue.code for issue in context.exception.issues})

        ramp["initial_state"]["rotation_deg"] = [-12.0, 0.0, 0.0]
        case_spec_v2_from_dict(data)

    def test_support_relations_project_to_explicit_runtime_support_map(self) -> None:
        data = case_spec_v2_fixture()
        data["relations"].append({"type": "supported_by", "source": "cue_ball", "target": "floor"})
        data["relations"].append({"type": "supports", "source": "floor", "target": "target_ball"})
        projection = project_case_spec_v2_to_v1(case_spec_v2_from_dict(data))
        self.assertEqual(
            projection.data["expected_physics"]["support"],
            {"cue_ball": "floor", "target_ball": "floor"},
        )

    def test_explicit_support_must_contain_subject_horizontal_bounds(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["initial_state"]["position_m"][0] = 3.0
        data["relations"].append({"type": "supported_by", "source": "cue_ball", "target": "floor"})
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("support_footprint_too_small", {issue.code for issue in context.exception.issues})

    def test_fast_dynamic_body_defaults_to_ccd_but_preserves_explicit_false(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["initial_state"]["linear_velocity_m_s"] = [4.0, 0.0, 0.0]
        projected = project_case_spec_v2_to_v1(case_spec_v2_from_dict(data)).data
        self.assertTrue(projected["objects"][0]["use_ccd"])

        data["objects"][0]["physics"]["use_ccd"] = False
        projected = project_case_spec_v2_to_v1(case_spec_v2_from_dict(data)).data
        self.assertFalse(projected["objects"][0]["use_ccd"])

    def test_invalid_energy_inputs_remain_structured_validation_errors(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["initial_state"]["linear_velocity_m_s"] = ["fast", 0.0, 0.0]
        data["objects"][0]["behavior"]["initial_kinetic_energy_j"] = 1.0
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("energy_inputs_missing", {issue.code for issue in context.exception.issues})

    def test_observation_camera_references_known_objects(self) -> None:
        data = case_spec_v2_fixture()
        data["observation_requirements"]["cameras"][0]["target_objects"] = ["missing_ball"]
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("unknown_object_reference", {issue.code for issue in context.exception.issues})

    def test_unsupported_capability_backend_combination_is_rejected_before_compilation(self) -> None:
        data = case_spec_v2_fixture()
        data["capabilities"] = {
            "primary": "fluid_particle_dynamics",
            "required": ["fluid_particle_dynamics"],
        }
        data["backend_constraints"]["allowed_solvers"] = ["taichi_cloth"]
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("unsupported_capability_backend", {issue.code for issue in context.exception.issues})

    def test_rigid_primary_cannot_be_routed_to_a_specialized_solver_by_omission(self) -> None:
        data = case_spec_v2_fixture()
        data["backend_constraints"]["required_solver_capabilities"] = []
        data["backend_constraints"]["allowed_solvers"] = ["genesis_sph"]
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("unsupported_capability_backend", {issue.code for issue in context.exception.issues})

    def test_required_solver_capability_must_use_registered_vocabulary(self) -> None:
        data = case_spec_v2_fixture()
        data["backend_constraints"]["required_solver_capabilities"].append("quantum_entanglement")
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        issue = next(
            issue for issue in context.exception.issues if issue.code == "unsupported_solver_capability"
        )
        self.assertEqual(issue.path, "/backend_constraints/required_solver_capabilities/2")

    def test_every_required_capability_must_be_registered(self) -> None:
        data = case_spec_v2_fixture()
        data["capabilities"]["required"].append("nonexistent_required_capability")
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        issue = next(issue for issue in context.exception.issues if issue.code == "unsupported_capability")
        self.assertEqual(issue.path, "/capabilities/required/1")

    def test_additional_registered_required_capability_fails_closed(self) -> None:
        data = case_spec_v2_fixture()
        data["capabilities"]["required"].append("physics_property_constraint_validation")
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        issue = next(
            issue
            for issue in context.exception.issues
            if issue.code == "additional_required_capability_unsupported"
        )
        self.assertEqual(issue.path, "/capabilities/required/1")

    def test_primary_compatibility_alias_may_use_canonical_required_id(self) -> None:
        data = case_spec_v2_fixture()
        data["capabilities"] = {
            "primary": "billiard_causality_compiler",
            "required": ["rigid_body_contact_causality"],
        }
        case = case_spec_v2_from_dict(data)
        self.assertEqual(case.capability_id, "rigid_body_contact_causality")

    def test_asset_policy_applies_to_every_fallback_route(self) -> None:
        data = case_spec_v2_fixture()
        data["asset_policy"]["allow_local"] = False
        data["objects"][0]["asset"] = {
            "description": "a generated ball with a local fallback",
            "acquisition": {
                "route": "model_generation",
                "requirement": "preferred",
                "origin": "user_explicit",
                "fallback_order": ["local_catalog"],
            },
        }
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        issue = next(issue for issue in context.exception.issues if issue.code == "route_disallowed")
        self.assertEqual(issue.path, "/objects/0/asset/acquisition/fallback_order/0")

    def test_asset_intent_is_required_when_analytic_proxy_is_disabled(self) -> None:
        data = case_spec_v2_fixture()
        data["asset_policy"]["allow_analytic_proxy"] = False
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("asset_required", {issue.code for issue in context.exception.issues})

    def test_runtime_vocabulary_is_validated_before_compilation(self) -> None:
        data = case_spec_v2_fixture()
        data["observation_requirements"]["cameras"][0]["role"] = "cinematic_magic"
        data["verification_requirements"]["assertions"][0]["type"] = "looks_physically_good"
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        codes = {issue.code for issue in context.exception.issues}
        self.assertIn("unsupported_camera_role", codes)
        self.assertIn("unsupported_verification_assertion", codes)


if __name__ == "__main__":
    unittest.main()
