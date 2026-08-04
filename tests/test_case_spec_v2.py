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
