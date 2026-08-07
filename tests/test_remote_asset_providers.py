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
from harness.assets.providers.input_manifest import ProviderInputError, build_provider_input_manifest
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
    def __init__(self, *, json_responses: list[Any], downloads: Mapping[str, bytes]) -> None:
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
        response = self.json_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

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


class DependencyStubImporter(StubImporter):
    def import_asset(self, request, *, work_dir: Path, workspace: Path) -> BackendImportResult:
        result = super().import_asset(request, work_dir=work_dir, workspace=workspace).to_dict()
        result["import_validation"] = {
            "actual_size_cm": [17.0, 17.0, 17.0],
            "expected_size_m": [0.18, 0.18, 0.18],
        }
        dependency_file = work_dir / "MI_Remote.uasset"
        dependency_payload = b"FAKE_REMOTE_UE_MATERIAL\n"
        dependency_file.write_bytes(dependency_payload)
        result["dependencies"] = [
            {
                "dependency_id": "/Game/Generated/Provider/MI_Remote.MI_Remote",
                "package": "/Game/Generated/Provider/MI_Remote",
                "local_path": str(dependency_file),
                "format": "uasset",
                "sha256": hashlib.sha256(dependency_payload).hexdigest(),
                "byte_size": len(dependency_payload),
                "materialized": True,
            }
        ]
        return BackendImportResult.from_dict(result)


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

    def test_provider_manifest_keeps_planning_and_meshy_authorizations_separate(self) -> None:
        image = self.workspace / "input.png"
        image.write_bytes(b"png")
        raw = self._provider_input(image, planning_upload=True)
        denied = build_provider_input_manifest([raw], workspace=self.workspace, meshy_upload_authorized=False)
        self.assertTrue(denied["inputs"][0]["authorizations"]["planning_llm_upload"])
        self.assertFalse(denied["inputs"][0]["authorizations"]["meshy_upload"])
        allowed = build_provider_input_manifest([raw], workspace=self.workspace, meshy_upload_authorized=True)
        self.assertTrue(allowed["inputs"][0]["authorizations"]["meshy_upload"])

    def test_meshy_manifest_rejects_authorized_input_outside_workspace(self) -> None:
        image = self.root / "outside.png"
        image.write_bytes(b"png")
        with self.assertRaises(ProviderInputError) as context:
            build_provider_input_manifest(
                [self._provider_input(image)],
                workspace=self.workspace,
                meshy_upload_authorized=True,
            )
        self.assertEqual(context.exception.code, "provider_input_outside_workspace")

    def test_meshy_rejects_unverified_remote_reference_urls(self) -> None:
        request = self._meshy_request(
            [{"input_id": "front", "uri": "https://example.com/image.png", "sha256": "0" * 64, "upload_authorized": True}]
        )
        adapter = MeshyModelGenerationAdapter(transport=FakeTransport(json_responses=[], downloads={}), api_key="test")
        with self.assertRaises(RemoteProviderError) as context:
            adapter.acquire(request, destination=self.workspace / "remote-url", workspace=self.workspace)
        self.assertEqual(context.exception.code, "remote_reference_url_unsupported")

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

    def test_meshy_resumes_checkpoint_without_duplicate_post(self) -> None:
        image = self.workspace / "resume.png"
        image.write_bytes(b"image")
        request = self._meshy_request(
            [
                {
                    "input_id": "front",
                    "local_path": str(image),
                    "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                    "upload_authorized": True,
                }
            ]
        )
        destination = self.workspace / "resume-provider"
        first_transport = FakeTransport(
            json_responses=[
                {"result": "paid-task-123"},
                RemoteProviderError("provider_network_error", "connection lost", retriable=True),
            ],
            downloads={},
        )
        with self.assertRaises(RemoteProviderError) as context:
            MeshyModelGenerationAdapter(transport=first_transport, api_key="test").acquire(
                request,
                destination=destination,
                workspace=self.workspace,
            )
        self.assertEqual(context.exception.details["task_id"], "paid-task-123")
        checkpoint = json.loads((destination / "task_checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["task_id"], "paid-task-123")

        resumed_transport = FakeTransport(
            json_responses=[
                {
                    "status": "SUCCEEDED",
                    "model_urls": {"glb": "https://d/model.glb", "obj": "https://d/model.obj"},
                }
            ],
            downloads={"https://d/model.glb": b"glb", "https://d/model.obj": b"v 0 0 0\n"},
        )
        acquisition = MeshyModelGenerationAdapter(transport=resumed_transport, api_key="test").acquire(
            request,
            destination=destination,
            workspace=self.workspace,
        )
        self.assertEqual(acquisition.source_asset_id, "paid-task-123")
        self.assertEqual([row["method"] for row in resumed_transport.requests], ["GET"])

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

    def test_poly_haven_ranks_candidates_and_breaks_score_ties_by_asset_id(self) -> None:
        fbx = b"fbx"
        files = {
            "fbx": {
                "1k": {
                    "fbx": {
                        "url": "https://d/model.fbx",
                        "md5": hashlib.md5(fbx, usedforsecurity=False).hexdigest(),
                    }
                }
            }
        }
        ambiguous = {
            "red_ball": {"type": 2, "name": "Red Ball", "tags": [], "dimensions": [180, 180, 180]},
            "blue_ball": {"type": 2, "name": "Blue Ball", "tags": [], "dimensions": [180, 180, 180]},
        }
        adapter = PolyHavenExternalSiteAdapter(
            transport=FakeTransport(
                json_responses=[ambiguous, files],
                downloads={"https://d/model.fbx": fbx},
            )
        )
        acquisition = adapter.acquire(
            {"provider_hint": "polyhaven", "search_intent": {"raw_query": "ball"}},
            destination=self.workspace / "ambiguous",
            workspace=self.workspace,
        )
        self.assertEqual(acquisition.source_asset_id, "blue_ball")
        discovery = acquisition.metadata["discovery"]
        self.assertEqual(discovery["selection_reason"], "stable_asset_id_tiebreak")
        self.assertEqual(discovery["tie_count"], 2)
        self.assertEqual(
            [row["asset_id"] for row in discovery["ranked_candidates"]],
            ["blue_ball", "red_ball"],
        )
        self.assertTrue((self.workspace / "ambiguous" / "discovery.json").is_file())

        ranked_assets = {
            "generic_crate": {"type": 2, "name": "Crate", "tags": ["container"]},
            "wooden_crate_02": {"type": 2, "name": "Wooden Crate", "tags": ["wood", "crate"]},
        }
        ranked = PolyHavenExternalSiteAdapter(
            transport=FakeTransport(
                json_responses=[ranked_assets, files],
                downloads={"https://d/model.fbx": fbx},
            )
        ).acquire(
            {"provider_hint": "polyhaven", "search_intent": {"raw_query": "realistic wooden crate"}},
            destination=self.workspace / "ranked",
            workspace=self.workspace,
        )
        self.assertEqual(ranked.source_asset_id, "wooden_crate_02")
        self.assertEqual(ranked.metadata["discovery"]["selection_reason"], "highest_relevance_score")
        scores = ranked.metadata["discovery"]["ranked_candidates"]
        self.assertGreater(scores[0]["score"], scores[1]["score"])

        drum_assets = {
            "metal_stool_01": {
                "type": 2,
                "name": "Metal Stool 01",
                "tags": ["metal", "industrial"],
                "category": "Furniture/Seating/Stools",
                "dimensions": [352, 355, 883],
            },
            "Barrel_01": {
                "type": 2,
                "name": "Barrel_01",
                "tags": ["metal", "industrial", "oil", "drums"],
                "category": "Containers & Storage/Barrels & Drums/Metal Drums",
                "dimensions": [563, 563, 880],
            },
        }
        drum = PolyHavenExternalSiteAdapter(
            transport=FakeTransport(
                json_responses=[drum_assets, files],
                downloads={"https://d/model.fbx": fbx},
            )
        ).acquire(
            {
                "provider_hint": "polyhaven",
                "search_intent": {
                    "raw_query": "Industrial metal drum, cylindrical, dimensions approx 0.56 m diameter, 0.88 m height.",
                    "must": {"approx_size_m": [0.56, 0.56, 0.88]},
                    "taxonomy": {"category": "container", "object_type": "drum"},
                },
            },
            destination=self.workspace / "drum",
            workspace=self.workspace,
        )
        self.assertEqual(drum.source_asset_id, "Barrel_01")
        self.assertIn("barrel", drum.metadata["discovery"]["query_tokens"])

    def test_poly_haven_rejects_hash_mismatch(self) -> None:
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
            provider_input_manifest=build_provider_input_manifest(
                [self._provider_input(image)],
                workspace=self.workspace,
                meshy_upload_authorized=True,
            ),
        )
        self.assertEqual(model_compilation.report["asset_resolve_invocation_count"], 1)
        model_result = model_compilation.artifacts["asset_provider_batch"]["results"][0]
        self.assertEqual(model_result["status"], "fulfilled")
        self.assertEqual(self.registry.get_asset_by_id(model_result["catalog_asset_ids"][0])["lifecycle_status"], "runtime_bound")
        provider_reference = model_compilation.artifacts["asset_provider_batch"]["requests"][0]["reference_inputs"][0]
        self.assertEqual(provider_reference["sha256"], image_sha)
        self.assertTrue(provider_reference["upload_authorized"])

        poly_transport = self._poly_transport()
        poly = PolyHavenExternalSiteAdapter(transport=poly_transport)
        external_compilation = compile_runtime_case(
            self._case(
                route="external_site",
                provider_hint="polyhaven",
                source_uri="polyhaven:dirty_football",
                asset_must={"license_tier": "local_preview", "geometry_type": "sphere"},
            ),
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=AssetProviderOrchestrator(
                workspace=self.workspace,
                importer=DependencyStubImporter(),
                remote_providers={"external_site": poly},
            ),
        )
        self.assertEqual(external_compilation.report["asset_resolve_invocation_count"], 1)
        external_result = external_compilation.artifacts["asset_provider_batch"]["results"][0]
        self.assertEqual(external_result["status"], "fulfilled")
        selected = self.registry.get_asset_by_id(external_result["catalog_asset_ids"][0])
        self.assertEqual(selected["license_tier"], "reference")
        self.assertEqual(selected["source_uri"], "https://polyhaven.com/a/dirty_football")
        self.assertEqual(selected["shape"], "sphere")
        self.assertEqual(selected["collider"], "box")
        self.assertEqual(selected["authored_size_m"], [0.17, 0.17, 0.17])
        self.assertEqual(selected["provider_reported_size_m"], [0.18, 0.18, 0.18])

    def test_external_asset_and_procedural_ground_qualify_in_same_case(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["asset"] = {
            "description": "dirty football",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "external_site",
                "requirement": "required",
                "origin": "user_explicit",
                "provider_hint": "polyhaven",
                "source_uri_hint": "polyhaven:dirty_football",
                "reference_inputs": [],
                "fallback_order": [],
            },
        }
        data["objects"][2]["asset"] = {
            "description": "procedural collision floor",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "procedural_generation",
                "requirement": "required",
                "origin": "user_explicit",
                "provider_hint": "box_mesh_v1",
                "reference_inputs": [],
                "fallback_order": [],
            },
        }
        compilation = compile_runtime_case(
            case_spec_v2_from_dict(data),
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=AssetProviderOrchestrator(
                workspace=self.workspace,
                importer=DependencyStubImporter(),
                remote_providers={
                    "external_site": PolyHavenExternalSiteAdapter(transport=self._poly_transport())
                },
            ),
        )
        self.assertEqual(compilation.report["asset_resolve_invocation_count"], 1)
        results = compilation.artifacts["asset_provider_batch"]["results"]
        self.assertEqual([row["status"] for row in results], ["fulfilled", "fulfilled"])
        selected = {
            row["intent"]["object_id"]: row["selected_asset"]["asset_id"]
            for row in compilation.artifacts["asset_resolution"]["assets"]
        }
        self.assertTrue(selected["cue_ball"].startswith("external.polyhaven.dirty_football."))
        self.assertTrue(selected["floor"].startswith("generated.local.box_mesh_v1."))

    def test_normal_model_route_requires_manifest_and_independent_meshy_authorization(self) -> None:
        image = self.workspace / "authorization.png"
        image.write_bytes(b"image")
        case = self._case(
            route="model_generation",
            provider_hint="meshy",
            references=[{"input_id": "front", "usage": ["generation_condition"]}],
        )
        transport = FakeTransport(json_responses=[], downloads={})
        orchestrator = AssetProviderOrchestrator(
            workspace=self.workspace,
            importer=StubImporter(),
            remote_providers={
                "model_generation": MeshyModelGenerationAdapter(transport=transport, api_key="test")
            },
        )
        missing = compile_runtime_case(
            case,
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=orchestrator,
        )
        self.assertEqual(
            missing.artifacts["asset_provider_batch"]["results"][0]["failure"]["code"],
            "provider_input_manifest_missing",
        )
        denied = compile_runtime_case(
            case,
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=orchestrator,
            provider_input_manifest=build_provider_input_manifest(
                [self._provider_input(image, planning_upload=True)],
                workspace=self.workspace,
                meshy_upload_authorized=False,
            ),
        )
        self.assertEqual(
            denied.artifacts["asset_provider_batch"]["results"][0]["failure"]["code"],
            "upload_not_authorized",
        )
        self.assertEqual(transport.requests, [])

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

    def test_meshy_paid_task_failure_has_receipt_with_checkpoint(self) -> None:
        image = self.workspace / "failed.png"
        image.write_bytes(b"image")
        meshy = MeshyModelGenerationAdapter(
            transport=FakeTransport(
                json_responses=[{"result": "paid-task"}, {"status": "FAILED", "task_error": "moderation"}],
                downloads={},
            ),
            api_key="test",
            poll_interval_s=0,
        )
        compilation = compile_runtime_case(
            self._case(
                route="model_generation",
                provider_hint="meshy",
                references=[{"input_id": "front", "usage": ["generation_condition"]}],
            ),
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=AssetProviderOrchestrator(
                workspace=self.workspace,
                importer=StubImporter(),
                remote_providers={"model_generation": meshy},
            ),
            provider_input_manifest=build_provider_input_manifest(
                [self._provider_input(image)],
                workspace=self.workspace,
                meshy_upload_authorized=True,
            ),
        )
        result = compilation.artifacts["asset_provider_batch"]["results"][0]
        self.assertEqual(result["failure"]["code"], "provider_task_failed")
        self.assertEqual(len(result["receipt_ids"]), 1)
        receipt = compilation.provider_receipts[0]
        self.assertEqual(receipt["provider_execution"]["task_id"], "paid-task")
        self.assertEqual(receipt["output_files"][0]["role"], "provider_task_checkpoint")

    def _case(
        self,
        *,
        route: str,
        provider_hint: str,
        references: list[dict[str, Any]] | None = None,
        source_uri: str | None = None,
        asset_must: dict[str, Any] | None = None,
    ):
        data = case_spec_v2_fixture()
        data["objects"][0]["asset"] = {
            "description": "dirty football",
            "resource_kind": "mesh_3d",
            "must": dict(asset_must or {}),
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
    def _provider_input(path: Path, *, planning_upload: bool = False) -> dict[str, Any]:
        payload = path.read_bytes()
        return {
            "input_id": "front",
            "kind": "image",
            "local_path": str(path),
            "mime_type": "image/png",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
            "external_upload_authorized": planning_upload,
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
