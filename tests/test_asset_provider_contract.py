from __future__ import annotations

import unittest

from harness.assets.providers.contracts import (
    BACKEND_IMPORT_RESULT_SCHEMA,
    PROVIDER_REQUEST_SCHEMA,
    PROVIDER_RESULT_SCHEMA,
    BackendImportResult,
    ProviderReceipt,
    ProviderRequest,
    ProviderResult,
)


class AssetProviderContractTests(unittest.TestCase):
    def request(self) -> dict[str, object]:
        return {
            "schema_version": PROVIDER_REQUEST_SCHEMA,
            "request_id": "request.1",
            "request_digest": "a" * 64,
            "case_id": "case",
            "object_id": "box",
            "slot": "primary",
            "route": "procedural_generation",
            "requirement": "required",
            "origin": "user_explicit",
            "provider_hint": "box_mesh_v1",
            "source_uri_hint": None,
            "reference_inputs": [],
            "search_intent": {"raw_query": "box"},
            "target_backend": "unreal",
            "required_license_tier": "local_preview",
            "generation_spec": {
                "recipe_id": "box_mesh_v1",
                "recipe_version": "v1",
                "shape": "box",
                "size_m": [1.0, 2.0, 3.0],
            },
        }

    def test_unknown_schema_versions_are_rejected(self) -> None:
        request = self.request()
        request["schema_version"] = "future_provider_request"
        with self.assertRaisesRegex(ValueError, "unsupported schema_version"):
            ProviderRequest.from_dict(request)

    def test_route_neutral_request_allows_provider_specific_generation_fields_to_be_absent(self) -> None:
        request = self.request()
        request["route"] = "external_site"
        request["generation_spec"] = {}
        parsed = ProviderRequest.from_dict(request)
        self.assertEqual(parsed.data["generation_spec"], {})

    def test_invalid_result_state_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid Provider result status"):
            ProviderResult.from_dict(
                {
                    "schema_version": PROVIDER_RESULT_SCHEMA,
                    "request_id": "request.1",
                    "request_digest": "a" * 64,
                    "object_id": "box",
                    "slot": "primary",
                    "status": "maybe",
                    "catalog_asset_ids": [],
                    "receipt_ids": [],
                }
            )

    def test_fulfilled_result_without_registered_ids_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "require Catalog asset IDs"):
            ProviderResult.from_dict(
                {
                    "schema_version": PROVIDER_RESULT_SCHEMA,
                    "request_id": "request.1",
                    "request_digest": "a" * 64,
                    "object_id": "box",
                    "slot": "primary",
                    "status": "fulfilled",
                    "catalog_asset_ids": [],
                    "receipt_ids": ["receipt.1"],
                }
            )

    def test_fulfilled_import_requires_materialized_runtime_ready_files(self) -> None:
        with self.assertRaisesRegex(ValueError, "materialized and runtime-ready"):
            BackendImportResult.from_dict(
                {
                    "schema_version": BACKEND_IMPORT_RESULT_SCHEMA,
                    "request_id": "import.1",
                    "request_digest": "b" * 64,
                    "asset_id": "asset.1",
                    "status": "fulfilled",
                    "object_path": "/Game/Generated/Asset.Asset",
                    "class_name": "StaticMesh",
                    "materialized": False,
                    "runtime_ready": True,
                    "files": [],
                    "dependencies": [],
                }
            )

    def test_receipt_lifecycle_cannot_skip_or_reorder_states(self) -> None:
        with self.assertRaisesRegex(ValueError, "out of order"):
            ProviderReceipt.from_dict(
                {
                    "schema_version": "harness_asset_provider_receipt_v1",
                    "receipt_id": "receipt.1",
                    "status": "failed",
                    "provider_id": "provider",
                    "provider_version": "1",
                    "request_id": "request.1",
                    "request_digest": "a" * 64,
                    "recipe_id": "box_mesh_v1",
                    "recipe_version": "v1",
                    "recipe_parameters": {},
                    "generator_source_version": "v1",
                    "importer_request_digest": "b" * 64,
                    "importer_result_digest": "c" * 64,
                    "input_identities": [],
                    "output_files": [],
                    "source_kind": "procedural_generation",
                    "source_uri": "provider://fixture",
                    "license": "All Rights Reserved",
                    "redistribution": {},
                    "lifecycle_transitions": ["requested", "materialized"],
                    "backend_binding": {},
                }
            )


if __name__ == "__main__":
    unittest.main()
