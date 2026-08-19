from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from harness.assets.asset_registry import AssetRegistry
from harness.assets.asset_intent_compiler import compile_v2_asset_intents
from harness.assets.asset_resolver import resolve_asset_intents
from harness.assets.local_asset_input import (
    LocalAssetRegistrationError,
    provider_manifest_input,
    register_local_asset_input,
)
from harness.assets.providers.backend_importer import BackendImporterAdapter
from harness.assets.providers.contracts import BACKEND_IMPORT_RESULT_SCHEMA, BackendImportResult
from harness.assets.providers.input_manifest import build_provider_input_manifest, with_registered_asset_input
from harness.assets.sqlite_catalog import initialize_catalog
from harness.core.case_spec_v2 import case_spec_v2_from_dict, compile_case_spec_v2_runtime
from tests.case_spec_v2_fixture import case_spec_v2_fixture


class StubImporter(BackendImporterAdapter):
    def __init__(self) -> None:
        self.invocations = 0

    def import_asset(self, request, *, work_dir: Path, workspace: Path) -> BackendImportResult:
        self.invocations += 1
        work_dir.mkdir(parents=True, exist_ok=True)
        imported = work_dir / "SM_Local.uasset"
        imported.write_bytes(b"imported")
        collision = work_dir / "qualified_collision_mesh.obj"
        collision.write_bytes(b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
        identity = [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
        return BackendImportResult.from_dict(
            {
                "schema_version": BACKEND_IMPORT_RESULT_SCHEMA,
                "request_id": request.data["request_id"],
                "request_digest": request.data["request_digest"],
                "asset_id": request.data["asset_id"],
                "status": "fulfilled",
                "object_path": "/Game/HarnessImported/SM_Local.SM_Local",
                "class_name": "StaticMesh",
                "materialized": True,
                "runtime_ready": True,
                "files": [self._record(imported, role="primary", file_format="uasset")],
                "dependencies": [],
                "import_validation": {"actual_size_cm": [10.0, 8.0, 12.0]},
                "portable_collision_artifact": {
                    "schema_version": "harness_portable_collision_mesh_v1",
                    **self._record(collision, role="qualified_collision_mesh", file_format="obj"),
                    "coordinate_system": "asset_local_z_up_m",
                    "artifact_to_asset_transform": {"matrix4x4": identity},
                },
            }
        )

    @staticmethod
    def _record(path: Path, *, role: str, file_format: str) -> dict:
        payload = path.read_bytes()
        return {
            "role": role,
            "local_path": str(path),
            "format": file_format,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
            "materialized": True,
        }


class LocalAssetInputTests(unittest.TestCase):
    def test_user_fbx_becomes_hashed_catalog_and_manifest_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            catalog_path = workspace / "catalog.sqlite"
            initialize_catalog(catalog_path)
            registry = AssetRegistry(catalog_path)
            source = root / "incoming" / "coffee_mug.fbx"
            source.parent.mkdir()
            source.write_bytes(b"user-authored-coffee-mug")
            importer = StubImporter()

            result = register_local_asset_input(
                source,
                workspace=workspace,
                registration_root=workspace / "jobs" / "job" / "request" / "local_assets" / "mug",
                registry=registry,
                importer=importer,
            )

            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertEqual(result["source_sha256"], source_hash)
            self.assertEqual(result["catalog_sha256"], source_hash)
            self.assertEqual(result["case_spec_reference"]["source_uri_hint"], result["source_uri"])
            self.assertEqual(importer.invocations, 1)
            registered = registry.get_asset_by_id(result["asset_id"])
            self.assertEqual(registered["source_uri"], result["source_uri"])
            self.assertEqual(registered["lifecycle_status"], "runtime_bound")
            self.assertEqual(registered["qualification"]["status"], "pass_local_preview")

            manifest = with_registered_asset_input(
                build_provider_input_manifest([], workspace=workspace),
                provider_manifest_input(result),
            )
            self.assertEqual(manifest["inputs"][0]["kind"], "asset_3d")
            self.assertEqual(manifest["inputs"][0]["asset_id"], result["asset_id"])
            self.assertEqual(manifest["inputs"][0]["sha256"], source_hash)

            case_data = case_spec_v2_fixture()
            case_data["objects"][0]["asset"] = {
                "description": "the user-provided coffee mug",
                "resource_kind": "mesh_3d",
                "acquisition": result["case_spec_reference"],
            }
            case = case_spec_v2_from_dict(case_data)
            runtime = compile_case_spec_v2_runtime(case)
            resolution = resolve_asset_intents(
                runtime.data,
                registry=registry,
                compiled_intents=compile_v2_asset_intents(case, runtime.data, target_backend="unreal"),
                target_backend="unreal",
                allow_local_preview=True,
            )
            self.assertEqual(resolution["assets"][0]["selected_asset"]["asset_id"], result["asset_id"])
            self.assertEqual(resolution["assets"][0]["selection_reason"], "required_source_uri_exact_match")

            cached = register_local_asset_input(
                source,
                workspace=workspace,
                registration_root=workspace / "other",
                registry=registry,
                importer=importer,
            )
            self.assertFalse(cached["importer_invoked"])
            self.assertEqual(importer.invocations, 1)

    def test_archive_traversal_is_rejected_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            catalog_path = workspace / "catalog.sqlite"
            initialize_catalog(catalog_path)
            archive = workspace / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../coffee_mug.fbx", b"fbx")
            importer = StubImporter()

            with self.assertRaises(LocalAssetRegistrationError) as context:
                register_local_asset_input(
                    archive,
                    workspace=workspace,
                    registration_root=workspace / "registration",
                    registry=AssetRegistry(catalog_path),
                    importer=importer,
                )

            self.assertEqual(context.exception.code, "local_asset_archive_unsafe")
            self.assertEqual(importer.invocations, 0)


if __name__ == "__main__":
    unittest.main()
