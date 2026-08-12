from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from harness.core.case_spec_v2 import CaseSpecV2ValidationError, case_spec_v2_from_dict
from harness.planning.case_generation import (
    CaseGenerationError,
    LLMJSONResponse,
    OpenAICompatibleJSONClient,
    _apply_request_identity,
    build_case_request,
    generate_case_spec_v2,
)
from tests.case_spec_v2_fixture import case_spec_v2_fixture


class FakeJSONClient:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, Any],
        images: list[dict[str, Any]] | None = None,
        purpose: str,
    ) -> LLMJSONResponse:
        self.calls.append(
            {
                "purpose": purpose,
                "images": images or [],
                "payload": dict(user_payload),
                "system_prompt": system_prompt,
            }
        )
        payload = self.payloads.pop(0)
        return LLMJSONResponse(
            payload=payload,
            receipt={"schema_version": "harness_llm_call_receipt_v1", "purpose": purpose, "model": "fake-json-model"},
        )


class FailingJSONClient:
    def complete_json(self, **_: Any) -> LLMJSONResponse:
        raise CaseGenerationError("llm_network_error", "temporary reset", retryable=True)


class PartialFailureJSONClient:
    def __init__(self) -> None:
        self.invocation_count = 0

    def complete_json(self, **_: Any) -> LLMJSONResponse:
        self.invocation_count += 1
        if self.invocation_count == 1:
            return LLMJSONResponse(
                payload=expansion_fixture(),
                receipt={
                    "schema_version": "harness_llm_call_receipt_v1",
                    "purpose": "expansion",
                    "model": "fake-json-model",
                    "request_sha256": "a" * 64,
                },
            )
        raise CaseGenerationError(
            "llm_network_error",
            "second call failed",
            retryable=True,
            request_identity="b" * 64,
        )


def expansion_fixture() -> dict[str, Any]:
    return {
        "request_summary": "one ball contacts another",
        "capability_analysis": {},
        "scene_analysis": {},
        "object_analysis": [],
        "event_and_relation_analysis": [],
        "asset_analysis": [],
        "expected_behavior_analysis": {},
        "observation_analysis": {},
        "backend_constraints": {},
        "ambiguities": [],
        "assumptions": [],
    }


def source_constraint_expansion() -> dict[str, Any]:
    expansion = expansion_fixture()
    expansion["object_analysis"] = [
        {"suggested_id": "cue_ball", "role": "striker"},
        {"suggested_id": "target_ball", "role": "target"},
    ]
    expansion["asset_source_constraints"] = [
        {
            "scope": {"object_ids": ["cue_ball"]},
            "allowed_routes": ["external_site", "model_generation"],
            "allowed_providers": ["poly_haven", "meshy"],
            "requirement": "required",
            "fallback_order": ["meshy"],
            "allow_proxy": False,
        },
        {
            "scope": {"object_ids": ["target_ball"]},
            "allowed_routes": ["model_generation"],
            "allowed_providers": ["meshy", "future_mesh_provider"],
            "requirement": "preferred",
            "fallback_order": [],
            "allow_proxy": True,
        },
    ]
    return expansion


class CaseGenerationV2Tests(unittest.TestCase):
    def test_malformed_http_response_retains_external_request_identity(self) -> None:
        request = build_case_request(case_id="malformed_response", text="Make one ball hit another.")
        client = OpenAICompatibleJSONClient(base_url="https://llm.example/v1", model="test-model")
        with tempfile.TemporaryDirectory() as temporary:
            with patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value.read.return_value = b"{not-json"
                with self.assertRaises(json.JSONDecodeError):
                    generate_case_spec_v2(request, client=client, artifact_dir=temporary)

            stage_result = json.loads(
                (Path(temporary) / "stage_results" / "generation.json").read_text(encoding="utf-8")
            )

        self.assertEqual(stage_result["invocation_count"], 1)
        self.assertEqual(len(stage_result["request_identities"]), 1)
        self.assertEqual(len(stage_result["request_identities"][0]), 64)

    def test_partial_generation_failure_retains_all_external_request_identities(self) -> None:
        request = build_case_request(case_id="partial_failure", text="Make one ball hit another.")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CaseGenerationError):
                generate_case_spec_v2(request, client=PartialFailureJSONClient(), artifact_dir=temporary)

            stage_result = json.loads(
                (Path(temporary) / "stage_results" / "generation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stage_result["invocation_count"], 2)
            self.assertEqual(stage_result["request_identities"], ["a" * 64, "b" * 64])

    def test_structured_generation_failure_is_landed_without_changing_exception_behavior(self) -> None:
        request = build_case_request(case_id="failed_generation", text="Make one ball hit another.")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CaseGenerationError):
                generate_case_spec_v2(request, client=FailingJSONClient(), artifact_dir=temporary)

            stage_result = json.loads(
                (Path(temporary) / "stage_results" / "generation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stage_result["failure_class"], "transient")
            self.assertTrue(stage_result["retryable"])

    def test_generation_resume_reuses_completed_expansion_call(self) -> None:
        request = build_case_request(case_id="resumed_generation", text="Make one ball hit another.")
        with tempfile.TemporaryDirectory() as temporary:
            partial = PartialFailureJSONClient()
            with self.assertRaises(CaseGenerationError):
                generate_case_spec_v2(request, client=partial, artifact_dir=temporary)
            self.assertEqual(partial.invocation_count, 2)

            resumed_client = FakeJSONClient([case_spec_v2_fixture()])
            result = generate_case_spec_v2(request, client=resumed_client, artifact_dir=temporary)

            self.assertEqual([call["purpose"] for call in resumed_client.calls], ["case_spec_generation"])
            self.assertEqual(result.stage_result["invocation_count"], 2)
            self.assertIn("a" * 64, result.stage_result["request_identities"])

    def test_exactly_two_normal_calls_generate_v2(self) -> None:
        request = build_case_request(case_id="generated_v2", text="Make one ball hit another.")
        client = FakeJSONClient([expansion_fixture(), case_spec_v2_fixture()])
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_case_spec_v2(request, client=client, artifact_dir=temporary)
            self.assertTrue((Path(temporary) / "request.json").is_file())
            self.assertTrue((Path(temporary) / "expansion.json").is_file())
            self.assertTrue((Path(temporary) / "case_spec_v2.json").is_file())
            self.assertTrue((Path(temporary) / "case_spec_generation_raw.json").is_file())
            self.assertTrue((Path(temporary) / "case_spec_generation_call_receipt.json").is_file())
            self.assertTrue((Path(temporary) / "stage_results" / "generation.json").is_file())

        self.assertEqual([call["purpose"] for call in client.calls], ["expansion", "case_spec_generation"])
        self.assertEqual(result.stage_result["status"], "completed")
        contract = client.calls[1]["payload"]["case_spec_contract"]
        self.assertEqual(
            contract["enums"]["local_procedural_recipe"],
            ["box_mesh_v1", "sphere_mesh_v1", "cylinder_mesh_v1"],
        )
        self.assertEqual(
            set(contract["enums"]["primary_capability"]),
            {"rigid_body_dynamics", "fluid_particle_dynamics", "deformable_body_dynamics"},
        )
        self.assertEqual(
            set(contract["enums"]["resource_kind"]),
            {
                "animation",
                "blueprint_actor",
                "geometry_collection",
                "map",
                "material",
                "mesh_3d",
                "skeletal_mesh",
                "texture_2d",
                "vfx",
            },
        )
        self.assertIn("runtime_ready", contract["enums"]["asset_must_field"])
        self.assertIn("source_kind", contract["enums"]["asset_must_not_field"])
        self.assertIn("rigid_body", contract["enums"]["solver_capability"])
        self.assertNotIn("rigid body dynamics", contract["enums"]["solver_capability"])
        self.assertEqual(
            contract["enums"]["geometry_scale_policy"],
            ["preserve_authored", "fit_uniform_to_approx_size"],
        )
        self.assertIn("preferences", contract["field_shapes"]["asset_request"])
        self.assertIn("color_rgb", contract["field_shapes"]["object"])
        self.assertIn("fixed_material_color", contract["field_shapes"]["object"])
        self.assertIn("allow_similarity_search", contract["field_shapes"]["reference_input"])
        self.assertIn("source", contract["field_shapes"]["binary_relation"])
        self.assertIn("surface_gap_m", contract["field_shapes"]["binary_relation"])
        self.assertIn("thresholds", contract["field_shapes"]["verification_requirements"])
        expansion_contract = client.calls[0]["payload"]["expansion_contract"]
        self.assertEqual(expansion_contract["field_types"]["object_analysis"], "array")
        self.assertEqual(expansion_contract["field_types"]["asset_source_constraints"], "array")
        self.assertIn("allowed_providers", expansion_contract["asset_source_constraint_shape"])
        expansion_prompt = client.calls[0]["system_prompt"]
        self.assertIn("turns a user's natural-language request", expansion_prompt)
        self.assertIn("Unreal Engine (UE)", expansion_prompt)
        self.assertIn("FIELD-BY-FIELD INSTRUCTIONS", expansion_prompt)
        self.assertIn("exactly one Asset Resolve", expansion_prompt)
        self.assertIn("Never split one\n   physical object", expansion_prompt)
        case_prompt = client.calls[1]["system_prompt"]
        self.assertIn("do not leave the color only in role or descriptive text", case_prompt)
        self.assertIn("Default to\n   local_preview", case_prompt)
        self.assertIn("source restriction for one named object applies only to that object's acquisition", case_prompt)
        self.assertIn("CASESPEC V2 GENERATOR", case_prompt)
        self.assertIn("PROVIDER AND RUNTIME BOUNDARY", case_prompt)
        self.assertIn("verification_requirements", case_prompt)
        self.assertIn("must_not contains hard exclusions", case_prompt)
        self.assertIn("passed unchanged to the selected verifier", case_prompt)
        self.assertIn("fit_uniform_to_approx_size", case_prompt)
        self.assertIn("positive pitch makes local +X downhill", case_prompt)
        self.assertIn("A cylinder's authored/analytic axis is local Z", case_prompt)
        self.assertIn("competing classes as must_not.category exclusions", case_prompt)
        self.assertIn("Use supported_by, not plain contact", case_prompt)
        self.assertIn("Never classify the\n   request as a named physical process", case_prompt)
        self.assertIn("Never invent a\n   placeholder mesh asset for solver-generated output", case_prompt)
        self.assertIn("set physics.enable_gravity=false only when the user explicitly requests", case_prompt)
        self.assertIn("must not add the unsupported\n   rigid_body solver capability", case_prompt)
        self.assertIn("write that nonnegative value as surface_gap_m", case_prompt)
        self.assertIn("passes close to each body's center of mass", case_prompt)
        self.assertIn("Do not raise box, cylinder, container", case_prompt)
        structure_example = client.calls[1]["payload"]["case_spec_contract"]["valid_structure_example_do_not_copy_values"]
        self.assertEqual(case_spec_v2_from_dict(structure_example).case_id, "example")
        rigid_sph_example = client.calls[1]["payload"]["case_spec_contract"][
            "valid_rigid_sph_shape_example_do_not_copy_values"
        ]
        container = rigid_sph_example["objects"][0]
        liquid = rigid_sph_example["objects"][2]
        self.assertEqual(rigid_sph_example["solver_scene"]["initialization"]["state"], "settled")
        self.assertEqual(
            rigid_sph_example["backend_constraints"],
            {
                "required_solver_capabilities": [
                    "particle_dynamics",
                    "particle_cache",
                    "surface_mesh_cache",
                ],
                "allowed_solvers": ["genesis_sph"],
                "render_backend": "ue",
                "allow_multi_backend": True,
            },
        )
        self.assertEqual(container["role"], "rigid_body")
        self.assertEqual(container["visual_representation"], {"source": "asset"})
        self.assertEqual(liquid["visual_representation"], {"source": "solver_generated"})
        self.assertNotIn("asset", liquid)
        self.assertIn("asset", container)
        self.assertEqual(container["solver"]["collision"]["type"], "axisymmetric_profile")
        self.assertNotIn(
            "rigid_body",
            client.calls[1]["payload"]["case_spec_contract"]["backend_solver_capability_matrix"]["genesis_sph"],
        )
        self.assertEqual(result.case_spec.case_id, "generated_v2")
        self.assertEqual(result.repair_count, 0)

    def test_multiple_structured_source_constraints_are_audited_in_case_spec(self) -> None:
        generated = case_spec_v2_fixture()
        generated["objects"][0]["asset"] = {
            "description": "external striker",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "external_site",
                "requirement": "required",
                "origin": "user_explicit",
                "provider_hint": "poly_haven",
                "fallback_order": [],
            },
        }
        generated["objects"][1]["asset"] = {
            "description": "generated target",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "model_generation",
                "requirement": "preferred",
                "origin": "llm_inferred",
                "provider_hint": "meshy",
                "fallback_order": [],
            },
        }
        request = build_case_request(case_id="multiple_source_constraints", text="Use the explicitly requested sources.")
        client = FakeJSONClient([source_constraint_expansion(), generated])

        result = generate_case_spec_v2(request, client=client)

        audited = result.case_spec.data["provenance"]["case_generation"]["asset_source_constraints"]
        self.assertEqual(len(audited), 2)
        self.assertEqual(audited[0]["allowed_providers"], ["poly_haven", "meshy"])
        self.assertEqual(audited[0]["fallback_order"], [])

    def test_required_no_proxy_source_mismatch_enters_existing_bounded_repair(self) -> None:
        invalid = case_spec_v2_fixture()
        invalid["objects"][0]["asset"] = {
            "description": "external striker",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "external_site",
                "requirement": "preferred",
                "origin": "llm_inferred",
                "provider_hint": "unapproved_provider",
                "fallback_order": [],
            },
        }
        repaired = deepcopy(invalid)
        repaired["objects"][0]["asset"]["acquisition"].update(
            requirement="required",
            origin="user_explicit",
            provider_hint="poly_haven",
        )
        request = build_case_request(case_id="required_source_repair", text="Use the explicitly requested source.")
        expansion = source_constraint_expansion()
        expansion["asset_source_constraints"] = expansion["asset_source_constraints"][:1]
        client = FakeJSONClient([expansion, invalid, repaired])

        result = generate_case_spec_v2(request, client=client)

        self.assertEqual(result.repair_count, 1)
        errors = client.calls[-1]["payload"]["validation_errors"]["issues"]
        codes = {item["code"] for item in errors}
        self.assertIn("asset_source_provider_mismatch", codes)
        self.assertIn("explicit_asset_source_not_required", codes)
        self.assertIn("explicit_asset_source_origin_lost", codes)
        self.assertEqual(
            client.calls[-1]["payload"]["repair_constraints"]["asset_source_constraints"][0]["scope"]["object_ids"],
            ["cue_ball"],
        )
        self.assertIn("Merge the visual asset request", client.calls[-1]["system_prompt"])

    def test_route_only_source_constraint_does_not_require_a_provider_list(self) -> None:
        generated = case_spec_v2_fixture()
        generated["objects"][0]["asset"] = {
            "description": "a locally generated sphere",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "procedural_generation",
                "requirement": "required",
                "origin": "user_explicit",
                "provider_hint": "sphere_mesh_v1",
                "reference_inputs": [],
                "fallback_order": [],
            },
        }
        expansion = source_constraint_expansion()
        expansion["asset_source_constraints"] = [
            {
                "scope": {"object_ids": ["cue_ball"]},
                "allowed_routes": ["procedural_generation"],
                "allowed_providers": [],
                "requirement": "required",
                "fallback_order": [],
                "allow_proxy": False,
            }
        ]
        client = FakeJSONClient([expansion, generated])

        result = generate_case_spec_v2(
            build_case_request(case_id="route_only_constraint", text="Generate the ball procedurally."),
            client=client,
        )

        audited = result.case_spec.data["provenance"]["case_generation"]["asset_source_constraints"]
        self.assertEqual(audited[0]["allowed_providers"], [])
        self.assertEqual(result.repair_count, 0)

        del expansion["asset_source_constraints"][0]["allowed_providers"]
        omitted = generate_case_spec_v2(
            build_case_request(case_id="route_only_constraint_omitted", text="Generate the ball procedurally."),
            client=FakeJSONClient([expansion, generated]),
        )
        self.assertEqual(omitted.repair_count, 0)

    def test_common_mapping_shaped_analysis_is_canonicalized_without_losing_keys(self) -> None:
        expansion = expansion_fixture()
        expansion["object_analysis"] = {
            "generated_box": {"role": "dynamic rigid body"},
            "floor": {"role": "static support"},
        }
        request = build_case_request(case_id="mapping_expansion", text="Drop a generated box.")
        client = FakeJSONClient([expansion, case_spec_v2_fixture()])

        result = generate_case_spec_v2(request, client=client)

        self.assertEqual(
            [row["analysis_key"] for row in result.expansion["object_analysis"]],
            ["generated_box", "floor"],
        )

    def test_invalid_expansion_is_auditable_before_normalization_failure(self) -> None:
        expansion = expansion_fixture()
        expansion["object_analysis"] = "not an array or object"
        request = build_case_request(case_id="invalid_expansion", text="Drop a box.")
        client = FakeJSONClient([expansion])
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            with self.assertRaisesRegex(ValueError, "object_analysis must be a list"):
                generate_case_spec_v2(request, client=client, artifact_dir=destination)
            raw = json.loads((destination / "expansion_raw.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["object_analysis"], "not an array or object")
            self.assertTrue((destination / "expansion_call_receipt.json").is_file())

    def test_validation_failure_allows_one_bounded_repair(self) -> None:
        invalid = case_spec_v2_fixture()
        invalid["timebase"]["observation_fps"] = 25
        repaired = deepcopy(invalid)
        repaired["timebase"]["observation_fps"] = 24
        request = build_case_request(case_id="repaired_v2", text="Make one ball hit another.")
        client = FakeJSONClient([expansion_fixture(), invalid, repaired])

        result = generate_case_spec_v2(request, client=client)

        self.assertEqual(result.repair_count, 1)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.calls[-1]["purpose"], "case_spec_validation_repair")
        self.assertIn("never\nadd a solver, renderer, or fallback", client.calls[-1]["system_prompt"])
        errors = client.calls[-1]["payload"]["validation_errors"]["issues"]
        self.assertIn("/timebase", {item["path"] for item in errors})

    def test_requested_backend_is_explicit_in_bounded_repair_constraints(self) -> None:
        invalid = case_spec_v2_fixture()
        invalid["timebase"]["observation_fps"] = 25
        repaired = deepcopy(invalid)
        repaired["timebase"]["observation_fps"] = 24
        request = build_case_request(
            case_id="repaired_for_ue",
            text="Drop a rigid body in UE.",
            requested_backend="ue",
        )
        client = FakeJSONClient([expansion_fixture(), invalid, repaired])

        generate_case_spec_v2(request, client=client)

        self.assertEqual(
            client.calls[-1]["payload"]["repair_constraints"]["requested_backend"],
            "ue",
        )

    def test_explicit_requested_backend_is_authoritative_for_generated_case(self) -> None:
        generated = case_spec_v2_fixture()
        generated["backend_constraints"]["allowed_solvers"] = ["fallback"]
        generated["backend_constraints"]["render_backend"] = "fallback"
        request = build_case_request(
            case_id="generated_for_ue",
            text="Render a collision in UE.",
            requested_backend="ue",
        )
        client = FakeJSONClient([expansion_fixture(), generated])

        result = generate_case_spec_v2(request, client=client)

        constraints = result.case_spec.data["backend_constraints"]
        self.assertEqual(constraints["allowed_solvers"], ["ue"])
        self.assertEqual(constraints["render_backend"], "ue")
        self.assertFalse(constraints["allow_multi_backend"])
        self.assertEqual(
            result.case_spec.data["provenance"]["case_generation"]["execution_constraints"],
            {"requested_backend": "ue"},
        )
        self.assertEqual(
            client.calls[0]["payload"]["request"]["execution_constraints"],
            {"requested_backend": "ue"},
        )

    def test_explicit_particle_solver_does_not_invent_a_separate_renderer(self) -> None:
        result = _apply_request_identity(
            {
                "backend_constraints": {},
                "solver_scene": {"type": "rigid_sph"},
            },
            {
                "case_id": "particle_scene",
                "text": "generic coupled particle and rigid-body scene",
                "execution_constraints": {"requested_backend": "genesis_sph"},
            },
        )

        self.assertEqual(result["backend_constraints"]["allowed_solvers"], ["genesis_sph"])
        self.assertEqual(result["backend_constraints"]["render_backend"], "genesis_sph")
        self.assertFalse(result["backend_constraints"]["allow_multi_backend"])

    def test_unregistered_natural_language_solver_capability_enters_bounded_repair(self) -> None:
        invalid = case_spec_v2_fixture()
        invalid["backend_constraints"]["required_solver_capabilities"] = ["rigid body dynamics"]
        repaired = case_spec_v2_fixture()
        request = build_case_request(case_id="solver_vocabulary_repair", text="Drop a rigid body.")
        client = FakeJSONClient([expansion_fixture(), invalid, repaired])

        result = generate_case_spec_v2(request, client=client)

        self.assertEqual(result.repair_count, 1)
        errors = client.calls[-1]["payload"]["validation_errors"]["issues"]
        self.assertIn(
            "/backend_constraints/required_solver_capabilities/0",
            {item["path"] for item in errors},
        )

    def test_procedural_cylinder_world_axis_dimensions_enter_bounded_repair(self) -> None:
        invalid = case_spec_v2_fixture()
        obj = invalid["objects"][0]
        obj["geometry"] = {"shape_hint": "cylinder", "approx_size_m": [0.2, 0.6, 0.2]}
        obj["initial_state"]["rotation_deg"] = [0.0, 0.0, 90.0]
        obj["asset"] = {
            "description": "a local procedural cylinder",
            "resource_kind": "mesh_3d",
            "must": {"geometry_type": "cylinder", "source_kind": "procedural_generation"},
            "acquisition": {
                "route": "procedural_generation",
                "requirement": "required",
                "origin": "user_explicit",
                "provider_hint": "deterministic_local",
                "reference_inputs": [],
                "fallback_order": [],
            },
        }
        repaired = deepcopy(invalid)
        repaired["objects"][0]["geometry"]["approx_size_m"] = [0.2, 0.2, 0.6]
        request = build_case_request(case_id="cylinder_axis_repair", text="Place a cylinder along world Y.")
        client = FakeJSONClient([expansion_fixture(), invalid, repaired])

        result = generate_case_spec_v2(request, client=client)

        self.assertEqual(result.repair_count, 1)
        self.assertEqual(result.case_spec.data["objects"][0]["geometry"]["approx_size_m"], [0.2, 0.2, 0.6])
        errors = client.calls[-1]["payload"]["validation_errors"]["issues"]
        self.assertIn(
            "procedural_cylinder_local_axis_size_mismatch",
            {item["code"] for item in errors},
        )
        self.assertIn("local Z", client.calls[-1]["system_prompt"])

    def test_release_velocity_away_from_impacts_target_enters_bounded_repair(self) -> None:
        invalid = case_spec_v2_fixture()
        invalid["objects"][0]["initial_state"]["linear_velocity_m_s"] = [0.0, 0.0, 0.0]
        invalid["relations"] = [{"type": "impacts", "source": "cue_ball", "target": "target_ball"}]
        invalid["events"] = [{
            "type": "release",
            "object": "cue_ball",
            "time_s": 0.4,
            "linear_velocity_m_s": [-2.0, 0.0, 0.0],
        }]
        repaired = deepcopy(invalid)
        repaired["events"][0]["linear_velocity_m_s"] = [2.0, 0.0, 0.0]
        request = build_case_request(case_id="release_direction_repair", text="Launch the ball into the target.")
        client = FakeJSONClient([expansion_fixture(), invalid, repaired])

        result = generate_case_spec_v2(request, client=client)

        self.assertEqual(result.repair_count, 1)
        self.assertEqual(result.case_spec.data["events"][0]["linear_velocity_m_s"], [2.0, 0.0, 0.0])
        errors = client.calls[-1]["payload"]["validation_errors"]["issues"]
        self.assertIn(
            "release_velocity_points_away_from_impact_target",
            {item["code"] for item in errors},
        )

    def test_image_registration_and_planning_upload_authorization_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "reference.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            local_request = build_case_request(case_id="image_case", image_paths=[image])
            upload_request = build_case_request(
                case_id="image_case",
                image_paths=[image],
                allow_image_upload=True,
            )
        self.assertEqual(local_request["inputs"][0]["input_id"], "request_image_0")
        self.assertFalse(local_request["inputs"][0]["external_upload_authorized"])
        self.assertTrue(upload_request["inputs"][0]["external_upload_authorized"])

    def test_reference_image_metadata_reaches_expansion_without_pixel_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "reference.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            request = build_case_request(case_id="metadata_only_image", image_paths=[image])
            client = FakeJSONClient([expansion_fixture(), case_spec_v2_fixture()])

            generate_case_spec_v2(request, client=client)

        self.assertEqual(client.calls[0]["images"], [])
        model_inputs = client.calls[0]["payload"]["request"]["inputs"]
        self.assertEqual(model_inputs[0]["input_id"], "request_image_0")
        self.assertEqual(model_inputs[0]["kind"], "image")
        self.assertIn("sha256", model_inputs[0])

    def test_reference_image_is_seen_by_expansion_and_bound_by_id_in_case_spec(self) -> None:
        generated = case_spec_v2_fixture()
        generated["objects"][0]["asset"] = {
            "description": "reconstruct the pictured ball",
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
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "reference.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            request = build_case_request(
                case_id="image_generation_v2",
                image_paths=[image],
                allow_image_upload=True,
            )
            client = FakeJSONClient([expansion_fixture(), generated])
            result = generate_case_spec_v2(request, client=client)

        self.assertEqual([item["input_id"] for item in client.calls[0]["images"]], ["request_image_0"])
        self.assertEqual(client.calls[1]["images"], [])
        reference = result.case_spec.data["objects"][0]["asset"]["acquisition"]["reference_inputs"][0]
        self.assertFalse(reference["allow_similarity_search"])

    def test_a_failed_repair_is_not_repaired_again(self) -> None:
        invalid = case_spec_v2_fixture()
        invalid["timebase"]["observation_fps"] = 25
        request = build_case_request(case_id="still_invalid", text="Make one ball hit another.")
        client = FakeJSONClient([expansion_fixture(), invalid, invalid])

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            with self.assertRaisesRegex(CaseSpecV2ValidationError, "physics_hz must be an integer multiple"):
                generate_case_spec_v2(request, client=client, artifact_dir=destination)
            self.assertTrue((destination / "case_spec_validation_errors.json").is_file())
            self.assertTrue((destination / "case_spec_repair_raw.json").is_file())
            self.assertTrue((destination / "case_spec_repair_call_receipt.json").is_file())
            self.assertTrue((destination / "case_spec_repair_validation_errors.json").is_file())

        self.assertEqual(len(client.calls), 3)


if __name__ == "__main__":
    unittest.main()
