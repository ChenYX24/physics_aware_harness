from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from harness.assets.asset_registry import AssetRegistry
from harness.assets.providers.contracts import BACKEND_IMPORT_RESULT_SCHEMA, BackendImportResult
from harness.assets.providers.orchestrator import AssetProviderOrchestrator
from harness.assets.providers.remote import (
    MeshyModelGenerationAdapter,
    PolyHavenExternalSiteAdapter,
    RemoteProviderError,
    UrllibRemoteTransport,
)
from harness.assets.sqlite_catalog import initialize_catalog
from harness.core.case_spec_v2 import case_spec_v2_from_dict
from harness.planning.runtime_compiler import compile_runtime_case
from tests.case_spec_v2_fixture import case_spec_v2_fixture


class FakeTransport:
    def __init__(self, *, json_responses: list[dict[str, Any]], downloads: Mapping[str, bytes]) -> None:
        self.json_responses = list(json_responses)
        self.downloads = dict(downloads)
        self.requests: list[dict[str, Any]] = []

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any] | None = None,
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers), "payload": dict(payload or {}), "timeout_s": timeout_s}
        )
        if not self.json_responses:
            raise AssertionError(f"unexpected JSON request: {method} {url}")
        return self.json_responses.pop(0)

    def download(
        self,
        url: str,
        destination: Path,
        *,
        headers: Mapping[str, str],
        expected_md5: str | None = None,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        del headers, timeout_s
        payload = self.downloads[url]
        actual_md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
        if expected_md5 and actual_md5 != expected_md5:
            raise RemoteProviderError("download_hash_mismatch", destination.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return {
            "path": destination,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "md5": actual_md5,
            "byte_size": len(payload),
        }


class StubImporter:
    def import_asset(self, request, *, work_dir: Path, workspace: Path) -> BackendImportResult:
        del workspace
        work_dir.mkdir(parents=True, exist_ok=True)
        asset_file = work_dir / "SM_Remote.uasset"
        payload = b"FAKE_REMOTE_UE_STATIC_MESH\n"
        asset_file.write_bytes(payload)
        return BackendImportResult.from_dict(
            {
                "schema_version": BACKEND_IMPORT_RESULT_SCHEMA,
                "request_id": request.data["request_id"],
                "request_digest": request.data["request_digest"],
                "asset_id": request.data["asset_id"],
                "status": "fulfilled",
                "object_path": "/Game/Generated/Provider/SM_Remote.SM_Remote",
                "class_name": "StaticMesh",
                "materialized": True,
                "runtime_ready": True,
                "files": [
                    {
                        "role": "primary",
                        "local_path": str(asset_file),
                        "format": "uasset",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "byte_size": len(payload),
                        "materialized": True,
                    }
                ],
                "dependencies": [],
            }
        )


class FailingImporter:
    def import_asset(self, request, *, work_dir: Path, workspace: Path) -> BackendImportResult:
        del work_dir, workspace
        return BackendImportResult.from_dict(
            {
                "schema_version": BACKEND_IMPORT_RESULT_SCHEMA,
                "request_id": request.data["request_id"],
                "request_digest": request.data["request_digest"],
                "asset_id": request.data["asset_id"],
                "status": "failed",
                "failure": {"code": "backend_asset_import_failed", "message": "fixture failure", "retriable": False},
            }
        )


class RemoteAssetProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.catalog_path = self.workspace / "catalog" / "assets" / "catalog.sqlite"
        initialize_catalog(self.catalog_path)
        self.registry = AssetRegistry(self.catalog_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_meshy_is_blocked_before_network_without_credentials(self) -> None:
        transport = FakeTransport(json_responses=[], downloads={})
        adapter = MeshyModelGenerationAdapter(transport=transport, api_key="")
        with self.assertRaises(RemoteProviderError) as context:
            adapter.acquire(self._meshy_request([]), destination=self.workspace / "provider", workspace=self.workspace)
        self.assertEqual(context.exception.code, "provider_credentials_missing")
        self.assertEqual(context.exception.status, "blocked")
        self.assertEqual(transport.requests, [])

    def test_insufficient_credits_http_response_is_a_non_retriable_blocker(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.meshy.ai/openapi/v1/multi-image-to-3d",
            402,
            "Payment Required",
            {},
            io.BytesIO(b'{"message":"Insufficient credits"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RemoteProviderError) as context:
                UrllibRemoteTransport().request_json(
                    "POST",
                    "https://api.meshy.ai/openapi/v1/multi-image-to-3d",
                    headers={"Authorization": "Bearer redacted"},
                    payload={},
                )
        self.assertEqual(context.exception.code, "provider_http_error")
        self.assertEqual(context.exception.status, "blocked")
        self.assertFalse(context.exception.retriable)
        self.assertIn("Insufficient credits", context.exception.message)

    def test_meshy_requires_explicit_upload_authorization(self) -> None:
        image = self.workspace / "input.png"
        image.write_bytes(b"png")
        request = self._meshy_request(
            [{"input_id": "front", "local_path": str(image), "sha256": hashlib.sha256(b"png").hexdigest()}]
        )
        adapter = MeshyModelGenerationAdapter(transport=FakeTransport(json_responses=[], downloads={}), api_key="test")
        with self.assertRaises(RemoteProviderError) as context:
            adapter.acquire(request, destination=self.workspace / "provider", workspace=self.workspace)
        self.assertEqual(context.exception.code, "upload_not_authorized")

    def test_meshy_submits_polls_and_immediately_materializes_glb_and_obj(self) -> None:
        image = self.workspace / "input.png"
        image.write_bytes(b"png-image")
        sha256 = hashlib.sha256(image.read_bytes()).hexdigest()
        transport = FakeTransport(
            json_responses=[
                {"result": "task-123"},
                {"id": "task-123", "status": "IN_PROGRESS", "progress": 50},
                {
                    "id": "task-123",
                    "status": "SUCCEEDED",
                    "progress": 100,
                    "consumed_credits": 20,
                    "model_urls": {
                        "glb": "https://signed.example/model.glb?token=secret",
                        "obj": "https://signed.example/model.obj?token=secret",
                    },
                },
            ],
            downloads={
                "https://signed.example/model.glb?token=secret": b"glb",
                "https://signed.example/model.obj?token=secret": b"v 0 0 0\n",
            },
        )
        adapter = MeshyModelGenerationAdapter(
            transport=transport,
            api_key="secret-key",
            poll_interval_s=0,
            sleep=lambda _: None,
        )
        request = self._meshy_request(
            [
                {
                    "input_id": "front",
                    "local_path": str(image),
                    "sha256": sha256,
                    "upload_authorized": True,
                }
            ]
        )
        acquisition = adapter.acquire(
            request,
            destination=self.workspace / "provider",
            workspace=self.workspace,
        )
        self.assertEqual(acquisition.source_uri, "meshy://multi-image-to-3d/task-123")
        self.assertTrue(acquisition.canonical_file.is_file())
        self.assertTrue(acquisition.import_file.is_file())
        create = transport.requests[0]
        self.assertEqual(create["payload"]["target_formats"], ["glb", "obj"])
        self.assertFalse(create["payload"]["image_enhancement"])
        self.assertNotIn("secret-key", json.dumps(acquisition.metadata))
        audited = json.loads((self.workspace / "provider" / "task_response_latest.json").read_text(encoding="utf-8"))
        self.assertNotIn("token=secret", json.dumps(audited))
        cache_only = MeshyModelGenerationAdapter(
            transport=FakeTransport(json_responses=[], downloads={}),
            api_key="",
        ).acquire(request, destination=self.workspace / "provider", workspace=self.workspace)
        self.assertEqual(cache_only.asset_id, acquisition.asset_id)
        manifest = (self.workspace / "provider" / "acquisition.json").read_text(encoding="utf-8")
        self.assertNotIn("secret-key", manifest)
        self.assertNotIn("token=secret", manifest)

    def test_meshy_failure_timeout_and_missing_outputs_are_structured(self) -> None:
        image = self.workspace / "input.png"
        image.write_bytes(b"image")
        reference = {
            "input_id": "front",
            "local_path": str(image),
            "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            "upload_authorized": True,
        }
        fixtures = [
            ([{"result": "task"}, {"status": "FAILED", "task_error": "moderation"}], 10, "provider_task_failed"),
            ([{"result": "task"}, {"status": "CANCELED"}], 10, "provider_task_canceled"),
            ([{"result": "task"}, {"status": "PENDING"}], 0, "provider_task_timeout"),
            ([{"result": "task"}, {"status": "SUCCEEDED", "model_urls": {"glb": "https://d/model.glb"}}], 10, "provider_output_missing"),
        ]
        for responses, timeout_s, code in fixtures:
            with self.subTest(code=code):
                adapter = MeshyModelGenerationAdapter(
                    transport=FakeTransport(json_responses=responses, downloads={}),
                    api_key="test",
                    poll_interval_s=0,
                    timeout_s=timeout_s,
                    sleep=lambda _: None,
                )
                with self.assertRaises(RemoteProviderError) as context:
                    adapter.acquire(
                        self._meshy_request([reference]),
                        destination=self.workspace / code,
                        workspace=self.workspace,
                    )
                self.assertEqual(context.exception.code, code)

    def test_poly_haven_pins_identity_checks_md5_and_materializes_dependency_closure(self) -> None:
        fbx = b"fbx"
        texture = b"jpg"
        assets = {
            "dirty_football": {
                "type": 2,
                "name": "Dirty Football",
                "description": "A scanned ball",
                "category": "Leisure/Sports/Balls",
                "tags": ["ball", "football"],
                "authors": {"Artist": "All"},
                "dimensions": [180, 180, 180],
                "files_hash": "abcdef1234567890",
            }
        }
        files = {
            "fbx": {
                "1k": {
                    "fbx": {
                        "url": "https://download.example/ball.fbx",
                        "md5": hashlib.md5(fbx, usedforsecurity=False).hexdigest(),
                        "include": {
                            "textures/ball.jpg": {
                                "url": "https://download.example/ball.jpg",
                                "md5": hashlib.md5(texture, usedforsecurity=False).hexdigest(),
                            }
                        },
                    }
                }
            }
        }
        transport = FakeTransport(
            json_responses=[assets, files],
            downloads={"https://download.example/ball.fbx": fbx, "https://download.example/ball.jpg": texture},
        )
        adapter = PolyHavenExternalSiteAdapter(transport=transport)
        acquisition = adapter.acquire(
            self._poly_request(),
            destination=self.workspace / "poly",
            workspace=self.workspace,
        )
        self.assertEqual(acquisition.source_asset_id, "dirty_football")
        self.assertEqual(acquisition.license, "CC0-1.0")
        self.assertEqual(acquisition.expected_size_m, (0.18, 0.18, 0.18))
        self.assertTrue((self.workspace / "poly" / "textures" / "ball.jpg").is_file())
        self.assertEqual(transport.requests[0]["headers"]["User-Agent"].split("/")[0], "PhysicsAwareHarness")

    def test_poly_haven_rejects_ambiguous_discovery_and_hash_mismatch(self) -> None:
        ambiguous = {
            "red_ball": {"type": 2, "name": "Red Ball", "tags": []},
            "blue_ball": {"type": 2, "name": "Blue Ball", "tags": []},
        }
        adapter = PolyHavenExternalSiteAdapter(transport=FakeTransport(json_responses=[ambiguous], downloads={}))
        with self.assertRaises(RemoteProviderError) as context:
            adapter.acquire(
                {"provider_hint": "polyhaven", "search_intent": {"raw_query": "ball"}},
                destination=self.workspace / "ambiguous",
                workspace=self.workspace,
            )
        self.assertEqual(context.exception.code, "ambiguous_external_asset")

        fbx = b"fbx"
        bad_hash_transport = FakeTransport(
            json_responses=[
                {"ball": {"type": 2, "name": "Ball", "dimensions": [100, 100, 100]}},
                {"fbx": {"1k": {"fbx": {"url": "https://d/ball.fbx", "md5": "0" * 32}}}},
            ],
            downloads={"https://d/ball.fbx": fbx},
        )
        adapter = PolyHavenExternalSiteAdapter(transport=bad_hash_transport)
        with self.assertRaises(RemoteProviderError) as context:
            adapter.acquire(
                {"provider_hint": "polyhaven", "source_uri_hint": "polyhaven:ball", "search_intent": {}},
                destination=self.workspace / "bad-hash",
                workspace=self.workspace,
            )
        self.assertEqual(context.exception.code, "download_hash_mismatch")

    def test_remote_providers_register_qualify_and_preserve_single_resolve(self) -> None:
        image = self.workspace / "front.png"
        image.write_bytes(b"image")
        image_sha = hashlib.sha256(image.read_bytes()).hexdigest()
        meshy_transport = FakeTransport(
            json_responses=[
                {"result": "task"},
                {
                    "status": "SUCCEEDED",
                    "model_urls": {"glb": "https://d/model.glb", "obj": "https://d/model.obj"},
                },
            ],
            downloads={"https://d/model.glb": b"glb", "https://d/model.obj": b"v 0 0 0\n"},
        )
        meshy = MeshyModelGenerationAdapter(transport=meshy_transport, api_key="test", poll_interval_s=0)
        model_compilation = compile_runtime_case(
            self._case(
                route="model_generation",
                provider_hint="meshy",
                references=[
                    {
                        "input_id": "front",
                        "usage": ["generation_condition", "geometry_reference"],
                        "allow_similarity_search": False,
                        "local_path": str(image),
                        "sha256": image_sha,
                        "upload_authorized": True,
                    }
                ],
            ),
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=AssetProviderOrchestrator(
                workspace=self.workspace,
                importer=StubImporter(),
                remote_providers={"model_generation": meshy},
            ),
        )
        self.assertEqual(model_compilation.report["asset_resolve_invocation_count"], 1)
        model_result = model_compilation.artifacts["asset_provider_batch"]["results"][0]
        self.assertEqual(model_result["status"], "fulfilled")
        self.assertEqual(self.registry.get_asset_by_id(model_result["catalog_asset_ids"][0])["lifecycle_status"], "runtime_bound")

        poly_transport = self._poly_transport()
        poly = PolyHavenExternalSiteAdapter(transport=poly_transport)
        external_compilation = compile_runtime_case(
            self._case(route="external_site", provider_hint="polyhaven", source_uri="polyhaven:dirty_football"),
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=AssetProviderOrchestrator(
                workspace=self.workspace,
                importer=StubImporter(),
                remote_providers={"external_site": poly},
            ),
        )
        self.assertEqual(external_compilation.report["asset_resolve_invocation_count"], 1)
        external_result = external_compilation.artifacts["asset_provider_batch"]["results"][0]
        self.assertEqual(external_result["status"], "fulfilled")
        selected = self.registry.get_asset_by_id(external_result["catalog_asset_ids"][0])
        self.assertEqual(selected["license_tier"], "reference")
        self.assertEqual(selected["source_uri"], "https://polyhaven.com/a/dirty_football")

    def test_remote_import_failure_is_receipted_and_never_registered(self) -> None:
        poly = PolyHavenExternalSiteAdapter(transport=self._poly_transport())
        compilation = compile_runtime_case(
            self._case(route="external_site", provider_hint="polyhaven", source_uri="polyhaven:dirty_football"),
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=AssetProviderOrchestrator(
                workspace=self.workspace,
                importer=FailingImporter(),
                remote_providers={"external_site": poly},
            ),
        )
        result = compilation.artifacts["asset_provider_batch"]["results"][0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["code"], "backend_asset_import_failed")
        self.assertEqual(compilation.report["asset_resolve_invocation_count"], 1)
        self.assertEqual(len(result["receipt_ids"]), 1)
        self.assertIsNone(self.registry.get_asset_by_id("external.polyhaven.dirty_football.abcdef123456"))

    def _case(
        self,
        *,
        route: str,
        provider_hint: str,
        references: list[dict[str, Any]] | None = None,
        source_uri: str | None = None,
    ):
        data = case_spec_v2_fixture()
        data["objects"][0]["asset"] = {
            "description": "dirty football",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": route,
                "requirement": "required",
                "origin": "user_explicit",
                "provider_hint": provider_hint,
                "source_uri_hint": source_uri,
                "reference_inputs": list(references or []),
                "fallback_order": [],
            },
        }
        available = [str(row["input_id"]) for row in references or []]
        return case_spec_v2_from_dict(data, available_input_ids=available)

    @staticmethod
    def _meshy_request(references: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "provider_hint": "meshy",
            "reference_inputs": references,
            "search_intent": {"raw_query": "a real ball"},
            "generation_spec": {"size_m": [0.18, 0.18, 0.18]},
        }

    @staticmethod
    def _poly_request() -> dict[str, Any]:
        return {
            "provider_hint": "polyhaven",
            "source_uri_hint": "polyhaven:dirty_football",
            "search_intent": {"raw_query": "dirty football"},
            "generation_spec": {"size_m": [0.18, 0.18, 0.18]},
        }

    @staticmethod
    def _poly_transport() -> FakeTransport:
        fbx = b"fbx"
        return FakeTransport(
            json_responses=[
                {
                    "dirty_football": {
                        "type": 2,
                        "name": "Dirty Football",
                        "description": "A scanned ball",
                        "category": "Leisure/Sports/Balls",
                        "tags": ["ball", "football"],
                        "authors": {"Artist": "All"},
                        "dimensions": [180, 180, 180],
                        "files_hash": "abcdef1234567890",
                    }
                },
                {
                    "fbx": {
                        "1k": {
                            "fbx": {
                                "url": "https://d/ball.fbx",
                                "md5": hashlib.md5(fbx, usedforsecurity=False).hexdigest(),
                            }
                        }
                    }
                },
            ],
            downloads={"https://d/ball.fbx": fbx},
        )


if __name__ == "__main__":
    unittest.main()
