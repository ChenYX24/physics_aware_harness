from __future__ import annotations

from copy import deepcopy
import unittest

from harness.core.case_spec_v2 import (
    CaseSpecV2ValidationError,
    case_spec_v2_from_dict,
    compile_case_spec_v2_runtime,
    validate_agent_case_spec_contract,
)
from tests.case_spec_v2_fixture import case_spec_v2_fixture


class CaseSpecV2Tests(unittest.TestCase):
    def test_agent_contract_requires_canonical_local_procedural_shape(self) -> None:
        data = case_spec_v2_fixture()
        subject = data["objects"][0]
        subject["geometry"]["shape_hint"] = "upright rectangular box with longest edge vertical"
        subject["geometry"]["approx_size_m"] = [0.03, 0.12, 0.3]
        subject["asset"] = {
            "description": "a procedural domino",
            "resource_kind": "mesh_3d",
            "must": {
                "geometry_type": "box",
                "source_kind": "procedural_generation",
            },
            "acquisition": {
                "route": "procedural_generation",
                "requirement": "required",
                "origin": "user_explicit",
                "provider_hint": "box_mesh_v1",
                "reference_inputs": [],
                "fallback_order": [],
            },
        }

        historical = case_spec_v2_from_dict(data)
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            validate_agent_case_spec_contract(historical.data)

        self.assertIn(
            "procedural_shape_hint_not_canonical",
            {issue.code for issue in context.exception.issues},
        )

    def test_agent_contract_rejects_recipe_and_geometry_type_conflicts(self) -> None:
        data = case_spec_v2_fixture()
        subject = data["objects"][0]
        subject["geometry"]["shape_hint"] = "sphere"
        subject["asset"] = {
            "description": "a conflicting primitive",
            "resource_kind": "mesh_3d",
            "must": {
                "geometry_type": "cylinder",
                "source_kind": "procedural_generation",
            },
            "acquisition": {
                "route": "procedural_generation",
                "requirement": "required",
                "origin": "user_explicit",
                "provider_hint": "box_mesh_v1",
                "reference_inputs": [],
                "fallback_order": [],
            },
        }

        parsed = case_spec_v2_from_dict(data)
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            validate_agent_case_spec_contract(parsed.data)

        self.assertEqual(
            {issue.code for issue in context.exception.issues},
            {"procedural_geometry_type_mismatch", "procedural_recipe_shape_mismatch"},
        )

    def test_defaults_validation_and_runtime_compilation(self) -> None:
        data = case_spec_v2_fixture()
        del data["timebase"]["deterministic_seed"]
        case = case_spec_v2_from_dict(data)
        runtime = compile_case_spec_v2_runtime(case)

        self.assertEqual(case.data["timebase"]["deterministic_seed"], 42)
        self.assertEqual(runtime.data["schema_version"], "harness_runtime_case_v2")
        self.assertEqual(runtime.case_id, "v2_ball_contact")
        self.assertEqual(runtime.data["active_objects"], ["cue_ball"])
        self.assertIn("target_ball", runtime.data["passive_objects"])
        self.assertTrue(runtime.data["objects"][0]["force_analytic_proxy"])
        self.assertEqual(runtime.data["timebase"]["render_fps"], 24)
        self.assertEqual(runtime.data["seed"], 42)
        self.assertEqual(runtime.data["source_contract"]["source_schema_version"], "harness_case_spec_v2")
        self.assertEqual(runtime.data["objects"][0]["body_type"], "dynamic")
        self.assertTrue(runtime.data["objects"][0]["collision_required"])
        self.assertEqual(runtime.data["objects"][2]["body_type"], "static")

    def test_historical_gravity_label_does_not_rewrite_object_roles(self) -> None:
        data = case_spec_v2_fixture()
        data["capabilities"] = {
            "primary": "rigid_body_gravity_collision",
            "required": ["rigid_body_gravity_collision"],
        }
        data["objects"][0]["role"] = "自由描述的下落物体"
        data["objects"][0]["initial_state"]["linear_velocity_m_s"] = [0.0, 0.0, 0.0]

        projection = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data)).data
        subject = projection["objects"][0]

        self.assertEqual(subject["role"], "自由描述的下落物体")
        self.assertNotIn("verification_role", subject)
        self.assertEqual(projection["objects"][2]["role"], "support")

    def test_uniform_asset_scale_policy_is_validated_and_projected(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["geometry"]["scale_policy"] = "fit_uniform_to_approx_size"

        projection = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data))

        self.assertEqual(
            projection.data["objects"][0]["asset_scale_policy"],
            "fit_uniform_to_approx_size",
        )

        data["objects"][0]["geometry"]["scale_policy"] = "stretch_each_axis"
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("invalid_enum", {issue.code for issue in context.exception.issues})

    def test_generic_solver_declarations_survive_runtime_compilation(self) -> None:
        data = case_spec_v2_fixture()
        data["capabilities"] = {
            "primary": "fluid_particle_dynamics",
            "required": ["fluid_particle_dynamics"],
        }
        data["backend_constraints"] = {
            "required_solver_capabilities": ["particle_dynamics", "fluid_dynamics", "particle_cache"],
            "allowed_solvers": ["genesis_sph"],
            "render_backend": "genesis_sph",
            "allow_multi_backend": True,
        }
        data["workspace_bounds_m"] = {"min_m": [-1.0, -1.0, -0.1], "max_m": [1.0, 1.0, 1.0]}
        data["solver_scene"] = {
            "type": "rigid_sph",
            "initialization": {"state": "settled", "pre_roll_s": 0.25, "capture_after_pre_roll": True},
            "measurements": [{"id": "span", "type": "axis_span", "axes": ["x", "y"]}],
            "assertions": [
                {
                    "id": "span_grows",
                    "measurement_id": "span",
                    "reduction": "final",
                    "operator": ">=",
                    "value": 0.1,
                }
            ],
        }
        data["objects"][0]["role"] = "fluid"
        data["objects"][0]["solver"] = {
            "material_model": "sph_liquid",
            "initial_volume": {
                "shape": "cylinder",
                "frame": {"type": "body_local", "body_id": "target_ball"},
                "position_m": [0.0, 0.0, 0.0],
                "radius_m": 0.025,
                "height_m": 0.06,
            },
        }
        data["objects"][1]["role"] = "rigid_body"
        data["objects"][1]["solver"] = {
            "mobility": "kinematic",
            "transform": {
                "position_m": [0.0, 0.0, 0.09],
                "euler_xyz_deg": [0.0, 0.0, 0.0],
                "ue_rotation_pyr_deg": [0.0, 0.0, 0.0],
            },
            "collision": {
                "type": "axisymmetric_profile",
                "asset_geometry_match": True,
                "fit_method": "fixture_profile_fit_v1",
                "inner_profile": [{"z_m": -0.04, "radius_m": 0.03}, {"z_m": 0.04, "radius_m": 0.04}],
                "wall_thickness_m": 0.005,
                "panel_count": 16,
            },
            "motion": {
                "type": "pivot_rotation",
                "start_time_s": 0.3,
                "duration_s": 1.0,
                "pivot_local_m": [0.04, 0.0, 0.04],
                "solver_end_rotation_xyz_deg": [0.0, 90.0, 0.0],
                "ue_end_rotation_pyr_deg": [-90.0, 0.0, 0.0],
            },
        }
        data["objects"][2]["role"] = "rigid_body"
        data["objects"][2]["solver"] = {
            "mobility": "static",
            "transform": {
                "position_m": [0.0, 0.0, -0.025],
                "euler_xyz_deg": [0.0, 0.0, 0.0],
                "ue_rotation_pyr_deg": [0.0, 0.0, 0.0],
            },
            "collision": {
                "type": "plane",
                "position_m": [0.0, 0.0, 0.0],
                "normal": [0.0, 0.0, 1.0],
                "asset_geometry_match": True,
            },
        }

        projection = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data)).data

        self.assertEqual(projection["solver_scene"], data["solver_scene"])
        self.assertEqual(projection["workspace_bounds_m"], data["workspace_bounds_m"])
        self.assertEqual(projection["objects"][1]["solver"], data["objects"][1]["solver"])

        invalid_rotation = deepcopy(data)
        invalid_rotation["objects"][1]["solver"]["motion"]["ue_end_rotation_pyr_deg"] = [90.0, 0.0, 0.0]
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(invalid_rotation)
        self.assertIn("rigid_sph_rotation_mapping_mismatch", {issue.code for issue in context.exception.issues})

        missing_fit = deepcopy(data)
        missing_fit["objects"][1]["solver"]["collision"].pop("fit_method")
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(missing_fit)
        self.assertIn("missing_collision_fit_evidence", {issue.code for issue in context.exception.issues})

        no_clearance = deepcopy(data)
        no_clearance["objects"][0]["solver"]["initial_volume"]["radius_m"] = 0.03
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(no_clearance)
        self.assertIn("insufficient_initial_fluid_clearance", {issue.code for issue in context.exception.issues})

    def test_invalid_rigid_sph_nested_contract_is_rejected_before_projection(self) -> None:
        data = case_spec_v2_fixture()
        data["capabilities"] = {
            "primary": "fluid_particle_dynamics",
            "required": ["fluid_particle_dynamics"],
        }
        data["backend_constraints"] = {
            "required_solver_capabilities": ["particle_dynamics", "fluid_dynamics", "particle_cache"],
            "allowed_solvers": ["genesis_sph"],
            "render_backend": "genesis_sph",
            "allow_multi_backend": True,
        }
        data["workspace_bounds_m"] = {"min_m": [-1.0, -1.0, -0.1], "max_m": [1.0, 1.0, 1.0]}
        data["solver_scene"] = {
            "type": "rigid_sph",
            "measurements": [{"id": "inside", "type": "volume_query"}],
            "assertions": [
                {"id": "inside", "measurement_id": "inside", "operator": ">", "value": 0.5}
            ],
        }
        data["objects"][0]["role"] = "coffee mug container"
        data["objects"][0]["solver"] = {
            "mobility": "kinematic",
            "transform": {"position_m": [0.0, 0.0, 0.1], "rotation_deg": [0.0, 0.0, 0.0]},
            "collision": {"type": "composite", "primitives": []},
            "motion": {
                "type": "pivot_rotation",
                "start_s": 0.3,
                "duration_s": 1.5,
                "angle_deg": 110.0,
            },
        }
        data["objects"][1]["role"] = "support surface"
        data["objects"][1].pop("solver", None)
        data["objects"][2]["role"] = "fluid"
        data["objects"][2]["solver"] = {
            "material_model": "sph_liquid",
            "initial_volume": {
                "shape": "cylinder",
                "frame": "body_local",
                "body_id": "target_ball",
                "dimensions_m": {"radius": 0.03, "height": 0.06},
                "pose": {"position_m": [0.0, 0.0, 0.0]},
            },
        }

        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)

        codes = {issue.code for issue in context.exception.issues}
        self.assertIn("rigid_sph_role_required", codes)
        self.assertIn("unsupported_rigid_sph_collision", codes)
        self.assertIn("invalid_rigid_sph_frame", codes)
        self.assertIn("unsupported_rigid_sph_measurement", codes)
        self.assertIn("unsupported_rigid_sph_reduction", codes)
        self.assertIn("unsupported_rigid_sph_operator", codes)

    def test_allowed_solver_must_provide_every_required_solver_capability(self) -> None:
        data = case_spec_v2_fixture()
        data["backend_constraints"]["allowed_solvers"] = ["genesis_sph"]
        data["backend_constraints"]["required_solver_capabilities"] = ["particle_dynamics", "contact_events"]

        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)

        self.assertIn("solver_capability_mismatch", {issue.code for issue in context.exception.issues})

    def test_explicit_object_color_is_validated_and_projected(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["color_rgb"] = [1.0, 0.2, 0.05]
        data["objects"][0]["fixed_material_color"] = True

        projection = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data))

        self.assertEqual(projection.data["objects"][0]["color_rgb"], [1.0, 0.2, 0.05])
        self.assertTrue(projection.data["objects"][0]["fixed_material_color"])

        data["objects"][0]["color_rgb"] = [1.01, 0.2, 0.05]
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("invalid_color", {issue.code for issue in context.exception.issues})

        data["objects"][0]["color_rgb"] = [1.0, 0.2, 0.05]
        data["objects"][0]["fixed_material_color"] = "yes"
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("invalid_type", {issue.code for issue in context.exception.issues})

    def test_uniform_asset_scale_policy_requires_a_target_size(self) -> None:
        data = case_spec_v2_fixture()
        geometry = data["objects"][0]["geometry"]
        geometry["scale_policy"] = "fit_uniform_to_approx_size"
        del geometry["approx_size_m"]

        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)

        self.assertIn("scale_target_missing", {issue.code for issue in context.exception.issues})

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

    def test_meshy_texture_prompt_is_optional_and_limited_to_600_characters(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["asset"] = {
            "description": "a generated ball",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "model_generation",
                "requirement": "required",
                "origin": "user_explicit",
                "provider_hint": "meshy",
                "reference_inputs": [],
                "fallback_order": [],
                "texture_prompt": "matte red painted wood",
            },
        }
        acquisition = data["objects"][0]["asset"]["acquisition"]
        case = case_spec_v2_from_dict(data)
        self.assertEqual(
            case.data["objects"][0]["asset"]["acquisition"]["texture_prompt"],
            "matte red painted wood",
        )

        acquisition["texture_prompt"] = "x" * 601
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("texture_prompt_too_long", {issue.code for issue in context.exception.issues})

        acquisition["texture_prompt"] = "red"
        acquisition["route"] = "procedural_generation"
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("texture_prompt_route_mismatch", {issue.code for issue in context.exception.issues})

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

    def test_semantic_role_does_not_add_a_process_specific_rotation_rule(self) -> None:
        data = case_spec_v2_fixture()
        ramp = data["objects"][2]
        ramp["role"] = "static inclined ramp"
        ramp["geometry"]["shape_hint"] = "box"
        ramp["initial_state"]["rotation_deg"] = [0.0, -12.0, 0.0]
        case_spec_v2_from_dict(data)

        ramp["initial_state"]["rotation_deg"] = [-12.0, 0.0, 0.0]
        case_spec_v2_from_dict(data)

    def test_box_that_only_mentions_a_ramp_is_not_an_inclined_surface(self) -> None:
        roles = (
            "static block supporting high end of ramp",
            "first target container closest to slope",
            "third target container farthest from ramp",
            "ramp support block",
        )
        for role in roles:
            with self.subTest(role=role):
                data = case_spec_v2_fixture()
                subject = data["objects"][2]
                subject["id"] = "ordinary_box"
                subject["role"] = role
                subject["geometry"]["shape_hint"] = "box"
                subject["initial_state"]["rotation_deg"] = [0.0, 0.0, 0.0]
                case_spec_v2_from_dict(data)

    def test_support_relations_project_to_explicit_runtime_support_map(self) -> None:
        data = case_spec_v2_fixture()
        data["relations"].append({"type": "supported_by", "source": "cue_ball", "target": "floor"})
        data["relations"].append({"type": "supports", "source": "floor", "target": "target_ball"})
        projection = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data))
        self.assertEqual(
            projection.data["expected_physics"]["support"],
            {"cue_ball": "floor", "target_ball": "floor"},
        )

    def test_nearby_stationary_contact_projects_as_support_not_collision_edge(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["initial_state"]["linear_velocity_m_s"] = [0.0, 0.0, 0.0]
        data["relations"].append({"type": "contact", "source": "cue_ball", "target": "floor"})

        projection = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data)).data

        self.assertEqual(projection["expected_physics"]["support"]["cue_ball"], "floor")
        self.assertEqual(
            projection["expected_physics"]["collision_graph"],
            [["cue_ball", "target_ball"]],
        )

    def test_singular_impact_relation_is_canonicalized_and_projected(self) -> None:
        data = case_spec_v2_fixture()
        data["relations"] = [{"type": "impact", "source": "cue_ball", "target": "target_ball"}]

        parsed = case_spec_v2_from_dict(data)
        projection = compile_case_spec_v2_runtime(parsed).data

        self.assertEqual(parsed.data["relations"][0]["type"], "impacts")
        self.assertEqual(
            projection["expected_physics"]["collision_graph"],
            [["cue_ball", "target_ball"]],
        )

    def test_explicit_collision_surface_gap_is_validated_and_projected(self) -> None:
        data = case_spec_v2_fixture()
        data["relations"][0]["surface_gap_m"] = 0.12

        projection = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data)).data

        self.assertEqual(
            projection["expected_physics"]["collision_surface_gaps_m"],
            [{"source": "cue_ball", "target": "target_ball", "surface_gap_m": 0.12}],
        )

        data["relations"][0]["surface_gap_m"] = -0.1
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("invalid_surface_gap", {issue.code for issue in context.exception.issues})

        data = case_spec_v2_fixture()
        data["relations"].append({
            "type": "supported_by",
            "source": "cue_ball",
            "target": "floor",
            "surface_gap_m": 0.01,
        })
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("surface_gap_requires_collision_relation", {issue.code for issue in context.exception.issues})

    def test_explicit_support_must_contain_subject_horizontal_bounds(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["initial_state"]["position_m"][0] = 3.0
        data["relations"].append({"type": "supported_by", "source": "cue_ball", "target": "floor"})
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("support_footprint_too_small", {issue.code for issue in context.exception.issues})

    def test_static_ramp_can_be_locally_supported_by_smaller_high_end_block(self) -> None:
        data = case_spec_v2_fixture()
        ramp = data["objects"][2]
        ramp["id"] = "ramp"
        ramp["role"] = "static inclined ramp"
        ramp["geometry"]["approx_size_m"] = [5.0, 1.0, 0.1]
        ramp["initial_state"]["position_m"] = [0.0, 0.0, 0.7]
        ramp["initial_state"]["rotation_deg"] = [15.0, 0.0, 0.0]
        support = {
            "id": "support_block",
            "role": "high end support",
            "geometry": {"shape_hint": "box", "approx_size_m": [0.5, 1.0, 1.3]},
            "physics": {"body_type": "static", "collision_required": True},
            "initial_state": {"position_m": [-2.4, 0.0, 0.65]},
            "behavior": {},
        }
        data["objects"].append(support)
        data["relations"].append({"type": "supported_by", "source": "ramp", "target": "support_block"})

        parsed = case_spec_v2_from_dict(data)

        self.assertEqual(parsed.data["objects"][-1]["id"], "support_block")

    def test_fast_dynamic_body_defaults_to_ccd_but_preserves_explicit_false(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["initial_state"]["linear_velocity_m_s"] = [4.0, 0.0, 0.0]
        projected = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data)).data
        self.assertTrue(projected["objects"][0]["use_ccd"])

        data["objects"][0]["physics"]["use_ccd"] = False
        projected = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data)).data
        self.assertFalse(projected["objects"][0]["use_ccd"])

    def test_ccd_must_be_declared_in_physics_not_behavior(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["behavior"]["use_ccd"] = True

        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)

        self.assertIn("misplaced_physics_field", {issue.code for issue in context.exception.issues})

    def test_release_event_velocity_overrides_zero_hold_velocity(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["initial_state"]["linear_velocity_m_s"] = [0.0, 0.0, 0.0]
        data["events"] = [{
            "type": "release",
            "object": "cue_ball",
            "time_s": 0.8,
            "linear_velocity_m_s": [1.2, 0.0, 0.0],
            "angular_velocity_rad_s": [0.0, 1.0, 0.0],
        }]

        projected = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data)).data
        ball = next(obj for obj in projected["objects"] if obj["id"] == "cue_ball")
        self.assertEqual(ball["initial_velocity_m_s"], [0.0, 0.0, 0.0])
        self.assertEqual(ball["release_time_s"], 0.8)
        self.assertEqual(ball["release_velocity_m_s"], [1.2, 0.0, 0.0])
        self.assertEqual(ball["release_angular_velocity_deg_s"], [0.0, 57.29577951308232, 0.0])

    def test_release_event_infers_active_body_without_language_specific_role(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["role"] = "撞击球"
        data["objects"][0]["initial_state"]["linear_velocity_m_s"] = [0.0, 0.0, 0.0]
        data["objects"][1]["role"] = "第一个目标"
        data["objects"][2]["role"] = "桌面"
        data["events"] = [{
            "type": "release",
            "object": "cue_ball",
            "time_s": 0.5,
            "linear_velocity_m_s": [1.2, 0.0, 0.0],
        }]

        projection = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data)).data

        self.assertEqual(projection["active_objects"], ["cue_ball"])
        self.assertEqual(projection["passive_objects"], ["target_ball"])

    def test_release_event_rejects_invalid_velocity(self) -> None:
        data = case_spec_v2_fixture()
        data["events"] = [{
            "type": "release",
            "object": "cue_ball",
            "time_s": 0.8,
            "linear_velocity_m_s": [1.2, 0.0],
        }]
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("invalid_vector", {issue.code for issue in context.exception.issues})

    def test_release_velocity_must_point_toward_single_impacts_target(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["initial_state"]["linear_velocity_m_s"] = [0.0, 0.0, 0.0]
        data["relations"] = [{"type": "impacts", "source": "cue_ball", "target": "target_ball"}]
        data["events"] = [{
            "type": "release",
            "object": "cue_ball",
            "time_s": 0.4,
            "linear_velocity_m_s": [-1.2, 0.0, 0.0],
        }]

        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)

        self.assertIn(
            "release_velocity_points_away_from_impact_target",
            {issue.code for issue in context.exception.issues},
        )
        data["events"][0]["linear_velocity_m_s"] = [1.2, 0.0, 0.0]
        case_spec_v2_from_dict(data)

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
        self.assertIn("unsupported_scene_backend", {issue.code for issue in context.exception.issues})

    def test_rigid_primary_cannot_be_routed_to_a_specialized_solver_by_omission(self) -> None:
        data = case_spec_v2_fixture()
        data["backend_constraints"]["required_solver_capabilities"] = []
        data["backend_constraints"]["allowed_solvers"] = ["genesis_sph"]
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(data)
        self.assertIn("unsupported_scene_backend", {issue.code for issue in context.exception.issues})

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

    def test_non_asset_visual_representations_do_not_require_asset_intents(self) -> None:
        data = case_spec_v2_fixture()
        data["asset_policy"]["allow_analytic_proxy"] = False
        data["objects"][0]["visual_representation"] = {"source": "solver_generated"}
        data["objects"][0]["solver"] = {"output": "renderable_geometry"}
        data["objects"][1]["visual_representation"] = {"source": "none"}
        data["objects"][2]["visual_representation"] = {"source": "none"}

        case = case_spec_v2_from_dict(data)
        projection = compile_case_spec_v2_runtime(case)

        self.assertEqual(projection.data["objects"][0]["visual_representation"]["source"], "solver_generated")
        self.assertNotIn("force_analytic_proxy", projection.data["objects"][0])

        conflict = deepcopy(data)
        conflict["objects"][0]["asset"] = {"description": "incorrect placeholder asset"}
        with self.assertRaises(CaseSpecV2ValidationError) as context:
            case_spec_v2_from_dict(conflict)
        self.assertIn("visual_representation_conflict", {issue.code for issue in context.exception.issues})

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
