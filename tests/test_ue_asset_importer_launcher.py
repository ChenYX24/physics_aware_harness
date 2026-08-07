from __future__ import annotations

import ast
import json
import math
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness.assets.providers.local_procedural_mesh import generate_procedural_obj
from scripts.harness_ue_asset_importer import _prepare_ue_request, _stop_process, _wait_for_result


class UEAssetImporterLauncherTests(unittest.TestCase):
    def test_external_fbx_bounds_allow_metadata_drift_but_reject_scale_errors(self) -> None:
        source = Path(__file__).resolve().parents[1] / "scripts" / "native_ue_asset_importer.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        function = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_dimensions_match_source"
        )
        namespace = {"math": math}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
        matches = namespace["_dimensions_match_source"]

        actual = [40.00098991394043, 27.053309440612793, 30.472382032600194]
        expected = [40.00099003314972, 29.232875257730484, 34.710586071014404]
        self.assertTrue(matches(actual, expected, source_kind="external_site"))
        self.assertFalse(matches([4000.0, 2700.0, 3000.0], expected, source_kind="external_site"))
        self.assertFalse(matches(actual, expected, source_kind="procedural_generation"))

    def test_prepare_request_normalizes_meter_obj_to_centimeters_without_changing_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generated = generate_procedural_obj(
                {
                    "recipe_id": "box_mesh_v1",
                    "recipe_version": "v1",
                    "shape": "box",
                    "size_m": [10.0, 10.0, 0.1],
                },
                root / "floor.obj",
            )
            request = {
                "request_id": "backend-import.fixture",
                "request_digest": "a" * 64,
                "asset_id": generated["asset_id"],
                "source_files": [
                    {
                        "local_path": str(generated["path"]),
                        "sha256": generated["sha256"],
                        "byte_size": generated["byte_size"],
                        "materialized": True,
                    }
                ],
            }
            request_path = root / "backend_import_request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            ue_request_path, temporary_paths = _prepare_ue_request(request_path, request)
            ue_request = json.loads(ue_request_path.read_text(encoding="utf-8"))
            self.assertEqual(ue_request["request_id"], request["request_id"])
            self.assertEqual(ue_request["request_digest"], request["request_digest"])
            self.assertEqual(request["source_files"][0]["local_path"], str(generated["path"]))
            normalized = Path(ue_request["source_files"][0]["local_path"])
            vertices = [
                [float(value) for value in line.split()[1:]]
                for line in normalized.read_text(encoding="utf-8").splitlines()
                if line.startswith("v ")
            ]
            extents = [max(row[axis] for row in vertices) - min(row[axis] for row in vertices) for axis in range(3)]
            self.assertEqual(extents, [1000.0, 1000.0, 10.0])
            for path in temporary_paths:
                path.unlink(missing_ok=True)

    def test_result_file_completes_launcher_before_hung_editor_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "result.json"
            code = (
                "import json,sys,time; "
                "open(sys.argv[1], 'w').write(json.dumps({'status':'fulfilled'})); "
                "time.sleep(30)"
            )
            process = subprocess.Popen([sys.executable, "-c", code, str(result_path)], text=True)
            started = time.monotonic()
            outcome = _wait_for_result(process, result_path=result_path, timeout_s=5.0)
            _stop_process(process)
            self.assertEqual(outcome, "result")
            self.assertLess(time.monotonic() - started, 5.0)

    def test_prepare_remote_obj_fits_declared_bounds_and_centers_pivot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "remote.obj"
            source.write_text(
                "v 10 20 30\nv 12 20 30\nv 10 24 30\nv 10 20 38\nf 1 2 3\n",
                encoding="utf-8",
            )
            request = {
                "request_id": "backend-import.remote",
                "request_digest": "b" * 64,
                "asset_id": "generated.meshy.fixture",
                "source_kind": "model_generation",
                "expected_size_m": [0.2, 0.4, 0.8],
                "source_files": [{"local_path": str(source), "materialized": True}],
            }
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            ue_request_path, temporary_paths = _prepare_ue_request(request_path, request)
            normalized = Path(json.loads(ue_request_path.read_text(encoding="utf-8"))["source_files"][0]["local_path"])
            vertices = [
                [float(value) for value in line.split()[1:]]
                for line in normalized.read_text(encoding="utf-8").splitlines()
                if line.startswith("v ")
            ]
            extents = [max(row[axis] for row in vertices) - min(row[axis] for row in vertices) for axis in range(3)]
            centers = [(max(row[axis] for row in vertices) + min(row[axis] for row in vertices)) / 2 for axis in range(3)]
            self.assertEqual(extents, [20.0, 40.0, 80.0])
            self.assertEqual(centers, [0.0, 0.0, 0.0])
            for path in temporary_paths:
                path.unlink(missing_ok=True)

    def test_prepare_remote_fbx_preserves_materialized_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "asset.fbx"
            source.write_bytes(b"fbx")
            request = {
                "request_id": "backend-import.external",
                "request_digest": "c" * 64,
                "asset_id": "external.polyhaven.fixture",
                "source_kind": "external_site",
                "source_files": [{"local_path": str(source), "materialized": True}],
            }
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            ue_request_path, temporary_paths = _prepare_ue_request(request_path, request)
            self.assertEqual(ue_request_path, request_path)
            self.assertEqual(temporary_paths, ())


if __name__ == "__main__":
    unittest.main()
