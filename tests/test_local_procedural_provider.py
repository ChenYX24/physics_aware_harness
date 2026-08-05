from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from harness.assets.asset_registry import AssetRegistry
from harness.assets.asset_resolver import resolve_asset_intents
from harness.assets.providers.backend_importer import UECommandImporterAdapter
from harness.assets.providers.contracts import BACKEND_IMPORT_REQUEST_SCHEMA, BackendImportRequest
from harness.assets.providers.local_procedural_mesh import generate_box_obj
from harness.assets.providers.orchestrator import AssetProviderOrchestrator
from harness.assets.sqlite_catalog import initialize_catalog
from harness.core.case_spec import load_case_spec
from harness.core.case_spec_v2 import case_spec_v2_from_dict, project_case_spec_v2_to_v1
from harness.planning.runtime_compiler import compile_runtime_case
from harness.runtime.ue_backend import UEBackend, UEBackendUnavailable
from tests.case_spec_v2_fixture import case_spec_v2_fixture


ROOT = Path(__file__).resolve().parents[1]
FAKE_IMPORTER = r'''from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--mode", default="success")
parser.add_argument("--request", required=True)
parser.add_argument("--result", required=True)
args = parser.parse_args()
request_path = Path(args.request)
request = json.loads(request_path.read_text(encoding="utf-8"))
work = request_path.parent
source = Path(request["source_files"][0]["local_path"])
if args.mode == "tampered_source":
    source.write_bytes(source.read_bytes() + b"tampered")
asset_file = work / "SM_GeneratedBox.uasset"
payload = b"FAKE_UE_STATIC_MESH_V1\n"
if args.mode == "lfs_pointer":
    payload = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"0" * 64 + b"\nsize 1\n"
asset_file.write_bytes(payload)
digest = hashlib.sha256(payload).hexdigest()
file_hash = "0" * 64 if args.mode == "bad_hash" else digest
dependencies = []
if args.mode == "incomplete_dependency":
    dependencies = [{
        "dependency_id": "/Game/Generated/M_Missing.M_Missing",
        "local_path": str(work / "Missing.uasset"),
        "sha256": "0" * 64,
        "byte_size": 1,
        "materialized": True,
    }]
result = {
    "schema_version": "harness_backend_asset_import_result_v1",
    "request_id": request["request_id"],
    "request_digest": request["request_digest"],
    "asset_id": "wrong.asset" if args.mode == "identity_mismatch" else request["asset_id"],
    "status": "fulfilled",
    "object_path": "invalid/path" if args.mode == "invalid_path" else "/Game/Generated/SM_GeneratedBox.SM_GeneratedBox",
    "class_name": "StaticMesh",
    "materialized": True,
    "runtime_ready": True,
    "files": [{
        "role": "primary",
        "local_path": str(asset_file),
        "format": "uasset",
        "sha256": file_hash,
        "byte_size": len(payload),
        "materialized": True,
    }],
    "dependencies": dependencies,
}
Path(args.result).write_text(json.dumps(result), encoding="utf-8")
print("fake importer completed", args.mode)
'''


class LocalProceduralProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.catalog_path = self.workspace / "catalog" / "assets" / "catalog.sqlite"
        initialize_catalog(self.catalog_path)
        self.registry = AssetRegistry(self.catalog_path)
        self.importer_script = self.root / "fake_importer.py"
        self.importer_script.write_text(FAKE_IMPORTER, encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def importer(self, mode: str = "success") -> UECommandImporterAdapter:
        return UECommandImporterAdapter([sys.executable, str(self.importer_script), "--mode", mode], timeout_s=10)

    def provider_case(
        self,
        *,
        requirement: str = "required",
        shape: str = "box",
        fallback_order: list[str] | None = None,
        route: str = "procedural_generation",
        license_tier: str = "local_preview",
    ):
        data = case_spec_v2_fixture()
        data["asset_policy"]["required_license_tier"] = license_tier
        data["objects"][0]["geometry"] = {"shape_hint": shape, "approx_size_m": [0.4, 0.6, 0.8]}
        data["objects"][0]["initial_state"]["position_m"][2] = 0.4
        data["objects"][0]["asset"] = {
            "description": "deterministic generated box",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": route,
                "requirement": requirement,
                "origin": "user_explicit",
                "provider_hint": "box_mesh_v1",
                "reference_inputs": [],
                "fallback_order": list(fallback_order or []),
            },
        }
        return case_spec_v2_from_dict(data)

    def orchestrator(self, mode: str = "success", *, evidence: dict[str, object] | None = None) -> AssetProviderOrchestrator:
        return AssetProviderOrchestrator(
            workspace=self.workspace,
            importer=self.importer(mode),
            redistribution_evidence=evidence,
        )

    def test_identical_generation_is_byte_and_id_idempotent_outside_repo(self) -> None:
        spec = {"recipe_id": "box_mesh_v1", "recipe_version": "v1", "shape": "box", "size_m": [1, 2, 3]}
        first = generate_box_obj(spec, self.workspace / "one" / "box.obj")
        second = generate_box_obj(spec, self.workspace / "two" / "box.obj")
        self.assertEqual(first["asset_id"], second["asset_id"])
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["path"].read_bytes(), second["path"].read_bytes())
        with self.assertRaises(ValueError):
            first["path"].resolve().relative_to(ROOT.resolve())

    def test_fake_import_register_lookup_qualify_and_resolver_select(self) -> None:
        case = self.provider_case()
        compilation = compile_runtime_case(
            case,
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=self.orchestrator(),
        )
        self.assertEqual(compilation.status, "pass", compilation.errors)
        self.assertEqual(compilation.report["asset_resolve_invocation_count"], 1)
        row = compilation.artifacts["asset_resolution"]["assets"][0]
        selected_id = row["selected_asset"]["asset_id"]
        self.assertTrue(selected_id.startswith("generated.local.box_mesh_v1."))
        self.assertEqual(row["acquisition"]["status"], "resolved_provider")
        self.assertEqual(row["acquisition"]["actual_route"], "procedural_generation")
        self.assertIsNotNone(self.registry.get_asset_by_id(selected_id))
        receipt = compilation.provider_receipts[0]
        self.assertEqual(receipt["lifecycle_transitions"][-1], "runtime_bound")
        for output in receipt["output_files"]:
            path = self.workspace / output["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), output["sha256"])
        run_dir = compilation.write(self.root / "successful_run")
        self.assertTrue((run_dir / "asset_provider_batch.json").is_file())
        self.assertTrue((run_dir / "provider_receipts" / f"{receipt['receipt_id']}.json").is_file())

    def test_required_missing_importer_fails_with_artifacts_and_single_resolve(self) -> None:
        case = self.provider_case()
        compilation = compile_runtime_case(
            case,
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=AssetProviderOrchestrator(
                workspace=self.workspace,
                importer=UECommandImporterAdapter([], timeout_s=1),
            ),
        )
        self.assertEqual(compilation.status, "fail")
        self.assertEqual(compilation.report["asset_resolve_invocation_count"], 1)
        self.assertIn("backend_importer_unavailable", {error["code"] for error in compilation.errors})
        run_dir = compilation.write(self.root / "missing_importer_run")
        batch = json.loads((run_dir / "asset_provider_batch.json").read_text(encoding="utf-8"))
        self.assertEqual(batch["results"][0]["failure"]["code"], "backend_importer_unavailable")
        self.assertTrue(any((run_dir / "provider_receipts").glob("*.json")))
        with patch("harness.runtime.ue_backend.build_ue_preflight_report") as preflight, patch(
            "harness.runtime.ue_backend.invoke_real_ue_runner"
        ) as runner:
            with self.assertRaises(UEBackendUnavailable) as context:
                UEBackend().run_case(
                    compilation.runtime_case,
                    self.root / "ue_runs",
                    compilation=compilation,
                    complete_sensor_contract=False,
                )
        self.assertEqual(context.exception.failure_type, "backend_importer_unavailable")
        preflight.assert_not_called()
        runner.assert_not_called()

    def test_importer_tamper_hash_path_dependency_and_lfs_fail_closed(self) -> None:
        expected_fragments = {
            "tampered_source": "file_hash_mismatch",
            "bad_hash": "file_hash_mismatch",
            "invalid_path": "invalid /Game object path",
            "incomplete_dependency": "dependency_file_missing",
            "lfs_pointer": "file_lfs_pointer",
            "identity_mismatch": "does not match request",
        }
        for mode, fragment in expected_fragments.items():
            with self.subTest(mode=mode):
                request_dir = self.workspace / mode
                generated = generate_box_obj(
                    {"recipe_id": "box_mesh_v1", "recipe_version": "v1", "shape": "box", "size_m": [1, 1, 1]},
                    request_dir / "box.obj",
                )
                payload = {
                    "schema_version": BACKEND_IMPORT_REQUEST_SCHEMA,
                    "request_id": f"import.{mode}",
                    "request_digest": hashlib.sha256(mode.encode()).hexdigest(),
                    "asset_id": f"asset.{mode}",
                    "target_backend": "unreal",
                    "class_name": "StaticMesh",
                    "source_files": [{
                        "role": "generated_source",
                        "local_path": str(generated["path"]),
                        "format": "obj",
                        "sha256": generated["sha256"],
                        "byte_size": generated["byte_size"],
                        "materialized": True,
                    }],
                }
                result = self.importer(mode).import_asset(
                    BackendImportRequest.from_dict(payload),
                    work_dir=request_dir,
                    workspace=self.workspace,
                )
                self.assertEqual(result.data["status"], "failed")
                self.assertIn(fragment, result.data["failure"]["message"] + result.data["failure"]["code"])

    def test_reference_needs_trusted_redistribution_evidence(self) -> None:
        case = self.provider_case(license_tier="reference")
        denied = compile_runtime_case(
            case,
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=self.orchestrator(),
        )
        self.assertEqual(denied.status, "fail")
        self.assertIn("asset_qualification_failed", {error["code"] for error in denied.errors})
        evidence = {
            "allowed": True,
            "rights_holder": "Physics-Aware Harness test fixtures",
            "evidence_uri": "fixture://tests/repository-owned-generated-assets",
            "verified_at": "2026-08-05T00:00:00Z",
        }
        accepted = compile_runtime_case(
            case,
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=self.orchestrator(evidence=evidence),
        )
        self.assertEqual(accepted.status, "pass", accepted.errors)
        self.assertEqual(
            accepted.artifacts["asset_resolution"]["assets"][0]["selected_asset"]["quality_gate"]["license_tier"],
            "reference",
        )

    def test_provider_returned_unregistered_id_cannot_be_selected(self) -> None:
        case = self.provider_case()
        compiled = compile_runtime_case(
            case,
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=AssetProviderOrchestrator(
                workspace=self.workspace,
                importer=UECommandImporterAdapter([], timeout_s=1),
            ),
        ).compiled_asset_intents
        result = resolve_asset_intents(
            project_case_spec_v2_to_v1(case).data,
            registry=self.registry,
            compiled_intents=list(compiled),
            provider_results={
                (compiled[0].object_id, compiled[0].slot): {
                    "status": "fulfilled",
                    "catalog_asset_ids": ["generated.unregistered"],
                    "receipt_ids": ["receipt.unregistered"],
                }
            },
            target_backend="unreal",
        )
        self.assertIsNone(result["assets"][0]["selected_asset"])
        self.assertEqual(result["assets"][0]["acquisition"]["status"], "provider_asset_unresolved")

    def test_preferred_failure_searches_local_only_with_explicit_fallback(self) -> None:
        self._register_local_box()
        with_fallback = self.provider_case(requirement="preferred", shape="sphere", fallback_order=["local_catalog"])
        without_fallback = self.provider_case(requirement="preferred", shape="sphere")
        first = compile_runtime_case(
            with_fallback,
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=self.orchestrator(),
        )
        second = compile_runtime_case(
            without_fallback,
            requested_backend="ue",
            registry=self.registry,
            provider_orchestrator=self.orchestrator(),
        )
        self.assertEqual(first.artifacts["asset_resolution"]["assets"][0]["acquisition"]["status"], "resolved_local_fallback")
        self.assertIsNone(second.artifacts["asset_resolution"]["assets"][0]["selected_asset"])
        self.assertEqual(
            second.artifacts["asset_provider_batch"]["results"][0]["failure"]["code"],
            "unsupported_generation_recipe",
        )

    def test_json_catalog_returns_catalog_not_writable_without_generation(self) -> None:
        json_registry = AssetRegistry(ROOT / "assets" / "asset_registry.example.json")
        compilation = compile_runtime_case(
            self.provider_case(),
            requested_backend="ue",
            registry=json_registry,
            provider_orchestrator=self.orchestrator(),
        )
        result = compilation.artifacts["asset_provider_batch"]["results"][0]
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure"]["code"], "catalog_not_writable")
        self.assertFalse((self.workspace / "providers").exists())

    def test_external_and_model_routes_are_structured_blockers(self) -> None:
        for route in ("external_site", "model_generation"):
            with self.subTest(route=route):
                compilation = compile_runtime_case(
                    self.provider_case(route=route),
                    requested_backend="ue",
                    registry=self.registry,
                    provider_orchestrator=self.orchestrator(),
                )
                result = compilation.artifacts["asset_provider_batch"]["results"][0]
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["failure"]["code"], "unsupported_provider_route")
                self.assertEqual(compilation.report["asset_resolve_invocation_count"], 1)

    def test_v1_does_not_invoke_provider_or_add_provider_artifacts(self) -> None:
        case = load_case_spec(ROOT / "cases" / "billiards" / "low_speed_single_contact.json")
        orchestrator = self.orchestrator()
        with patch.object(orchestrator, "fulfill", wraps=orchestrator.fulfill) as fulfill:
            compilation = compile_runtime_case(
                case,
                requested_backend="fallback",
                registry=AssetRegistry(ROOT / "assets" / "asset_registry.example.json"),
                provider_orchestrator=orchestrator,
            )
        self.assertEqual(fulfill.call_count, 0)
        self.assertNotIn("asset_provider_batch", compilation.artifacts)
        self.assertEqual(compilation.report["asset_resolve_invocation_count"], 1)

    def test_registration_marks_vector_index_stale_without_rebuild(self) -> None:
        with closing(sqlite3.connect(self.catalog_path)) as connection:
            connection.execute(
                "INSERT INTO embedding_models(model_id, provider, model_name, pretrained, dimension, document_version, library_version, model_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("test", "fixture", "fixture", "fixture", 2, "v1", "v1", "{}"),
            )
            connection.execute(
                "INSERT INTO vector_index_state(index_name, table_name, model_id, dimension, distance_metric, row_count, source_digest, sqlite_vec_version, status, rebuilt_at, config_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("test_index", "test_vec", "test", 2, "cosine", 0, "empty", "fixture", "ready", "2026-08-05T00:00:00Z", "{}"),
            )
            connection.commit()
        with patch.dict(os.environ, {"SIM_HARNESS_ALLOW_LOCAL_PREVIEW_ASSETS": "1"}):
            compilation = compile_runtime_case(
                self.provider_case(),
                requested_backend="ue",
                registry=self.registry,
                provider_orchestrator=self.orchestrator(),
            )
        self.assertEqual(compilation.status, "pass", compilation.errors)
        with closing(sqlite3.connect(self.catalog_path)) as connection:
            status = connection.execute("SELECT status FROM vector_index_state WHERE index_name='test_index'").fetchone()[0]
        self.assertEqual(status, "stale")

    def test_repeated_compilations_do_not_duplicate_assets_or_conflict_receipts(self) -> None:
        case = self.provider_case()
        orchestrator = self.orchestrator()
        with patch.dict(os.environ, {"SIM_HARNESS_ALLOW_LOCAL_PREVIEW_ASSETS": "1"}):
            first = compile_runtime_case(case, requested_backend="ue", registry=self.registry, provider_orchestrator=orchestrator)
            second = compile_runtime_case(case, requested_backend="ue", registry=self.registry, provider_orchestrator=orchestrator)
        self.assertEqual(first.provider_receipts, second.provider_receipts)
        with closing(sqlite3.connect(self.catalog_path)) as connection:
            count = connection.execute("SELECT count(*) FROM assets WHERE asset_id LIKE 'generated.local.box_mesh_v1.%'").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(first.report["asset_resolve_invocation_count"], 1)
        self.assertEqual(second.report["asset_resolve_invocation_count"], 1)

    def _register_local_box(self) -> None:
        self.registry.register_asset(
            {
                "asset_id": "local.fixture.box",
                "name": "deterministic generated box",
                "aliases": ["generated box"],
                "tags": ["active_striker", "box"],
                "category": "physics_critical",
                "type": "StaticMesh",
                "source_kind": "engine_builtin",
                "source_uri": "ue://Engine/BasicShapes/Cube.Cube",
                "license": "Unreal Engine EULA",
                "license_tier": "reference",
                "quality_status": "approved_proxy",
                "materialized": True,
                "ue_path": "/Engine/BasicShapes/Cube.Cube",
                "bbox_size_m": [0.4, 0.6, 0.8],
                "collider": "box",
                "collision_profile": "PhysicsActor",
                "mass_kg": 1.0,
                "material": {"dynamic_friction": 0.4},
            }
        )


if __name__ == "__main__":
    unittest.main()
