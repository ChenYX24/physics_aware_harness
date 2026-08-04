from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from harness.core.case_spec_v2 import CaseSpecV2ValidationError
from harness.planning.case_generation import (
    LLMJSONResponse,
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
        self.calls.append({"purpose": purpose, "images": images or [], "payload": dict(user_payload)})
        payload = self.payloads.pop(0)
        return LLMJSONResponse(
            payload=payload,
            receipt={"schema_version": "harness_llm_call_receipt_v1", "purpose": purpose, "model": "fake-json-model"},
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


class CaseGenerationV2Tests(unittest.TestCase):
    def test_exactly_two_normal_calls_generate_v2(self) -> None:
        request = build_case_request(case_id="generated_v2", text="Make one ball hit another.")
        client = FakeJSONClient([expansion_fixture(), case_spec_v2_fixture()])
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_case_spec_v2(request, client=client, artifact_dir=temporary)
            self.assertTrue((Path(temporary) / "request.json").is_file())
            self.assertTrue((Path(temporary) / "expansion.json").is_file())
            self.assertTrue((Path(temporary) / "case_spec_v2.json").is_file())

        self.assertEqual([call["purpose"] for call in client.calls], ["expansion", "case_spec_generation"])
        contract = client.calls[1]["payload"]["case_spec_contract"]
        self.assertIn("rigid_body_contact_causality", contract["enums"]["primary_capability"])
        self.assertEqual(result.case_spec.case_id, "generated_v2")
        self.assertEqual(result.repair_count, 0)

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
        errors = client.calls[-1]["payload"]["validation_errors"]["issues"]
        self.assertIn("/timebase", {item["path"] for item in errors})

    def test_image_upload_requires_explicit_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "reference.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            with self.assertRaisesRegex(ValueError, "allow-image-upload"):
                build_case_request(case_id="image_case", image_paths=[image])
            request = build_case_request(
                case_id="image_case",
                image_paths=[image],
                allow_image_upload=True,
            )
        self.assertEqual(request["inputs"][0]["input_id"], "request_image_0")
        self.assertTrue(request["inputs"][0]["external_upload_authorized"])

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

        with self.assertRaisesRegex(CaseSpecV2ValidationError, "physics_hz must be an integer multiple"):
            generate_case_spec_v2(request, client=client)

        self.assertEqual(len(client.calls), 3)


if __name__ == "__main__":
    unittest.main()
