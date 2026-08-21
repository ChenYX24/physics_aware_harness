from __future__ import annotations

import ast
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness.assets.providers.local_procedural_mesh import generate_procedural_obj
from scripts.harness_ue_asset_importer import _prepare_ue_request, _stop_process, _wait_for_result


ROOT = Path(__file__).resolve().parents[1]


class UEAssetImporterLauncherTests(unittest.TestCase):
    def test_native_geometry_analysis_uses_python_exposed_static_mesh_export(self) -> None:
        source = Path(__file__).resolve().parents[1] / "scripts" / "native_ue_asset_importer.py"
        text = source.read_text(encoding="utf-8")

        self.assertNotIn("get_mesh_description", text)
        self.assertIn("unreal.StaticMeshExporterOBJ()", text)
        self.assertIn("unreal.Exporter.run_asset_export_task(task)", text)
        self.assertIn(
            "points.append([float(fields[1]), float(fields[3]), float(fields[2])])",
            text,
        )

    def test_batch_launcher_reports_configuration_failure_per_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            items = []
            for index in range(2):
                request_path = root / f"request_{index}.json"
                result_path = root / f"result_{index}.json"
                request_path.write_text(
                    json.dumps(
                        {
                            "request_id": f"backend-import.batch-{index}",
                            "request_digest": str(index) * 64,
                            "asset_id": f"asset.batch.{index}",
                        }
                    ),
                    encoding="utf-8",
                )
                items.append({"request_path": str(request_path), "result_path": str(result_path)})
            manifest = root / "batch.json"
            aggregate = root / "aggregate.json"
            manifest.write_text(json.dumps({"items": items}), encoding="utf-8")
            environment = os.environ.copy()
            environment.pop("SIM_STUDIO_UE_EXECUTABLE", None)
            environment.pop("SIM_STUDIO_UE_PROJECT", None)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_ue_asset_importer.py"),
                    "--batch-request",
                    str(manifest),
                    "--batch-result",
                    str(aggregate),
                    "--ue-executable",
                    str(root / "missing-editor"),
                    "--ue-project",
                    str(root / "missing.uproject"),
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            for item in items:
                result = json.loads(Path(item["result_path"]).read_text(encoding="utf-8"))
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["failure"]["code"], "backend_importer_unavailable")

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
        self.assertTrue(
            matches(
                [34.550649642944336, 10.713947296142578, 50.31427764892578],
                [37.74920701980591, 41.845703125, 50.3142774105072],
                source_kind="external_site",
            )
        )
        self.assertFalse(matches([1.0, 2.0, 50.31427764892578], [37.75, 41.85, 50.31], source_kind="external_site"))
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

    def test_prepare_meshy_obj_converts_y_up_and_uniformly_fits_declared_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "remote.obj"
            source.write_text(
                "v 10 20 30\nv 12 20 30\nv 10 28 30\nv 10 20 34\nvn 0 1 0\nf 1//1 2//1 3//1\n",
                encoding="utf-8",
            )
            request = {
                "request_id": "backend-import.remote",
                "request_digest": "b" * 64,
                "asset_id": "generated.meshy.fixture",
                "source_kind": "model_generation",
                "provider_id": "meshy_model_generation_v1",
                "expected_size_m": [0.3, 0.4, 0.8],
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
            self.assertAlmostEqual(extents[1] / extents[0], 2.0)
            self.assertAlmostEqual(extents[2] / extents[0], 4.0)
            self.assertAlmostEqual(math.sqrt(sum(value * value for value in extents)), math.sqrt(30.0**2 + 40.0**2 + 80.0**2))
            self.assertEqual(centers, [0.0, 0.0, 0.0])
            ue_request = json.loads(ue_request_path.read_text(encoding="utf-8"))
            for actual, expected in zip(ue_request["expected_size_m"], [value / 100.0 for value in extents]):
                self.assertAlmostEqual(actual, expected)
            normal = next(
                [float(value) for value in line.split()[1:]]
                for line in normalized.read_text(encoding="utf-8").splitlines()
                if line.startswith("vn ")
            )
            self.assertEqual(normal, [0.0, 0.0, 1.0])
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
            self.assertEqual(ue_request_path, root / "request.ue_import.json")
            self.assertEqual(temporary_paths, (ue_request_path,))
            ue_request = json.loads(ue_request_path.read_text(encoding="utf-8"))
            self.assertEqual(ue_request["source_files"], request["source_files"])
            self.assertEqual(
                ue_request["portable_collision_artifact_path"],
                str(root / "qualified_collision_mesh.obj"),
            )

    def test_portable_collision_uses_asset_local_exported_geometry(self) -> None:
        source = Path(__file__).resolve().parents[1] / "scripts" / "native_ue_asset_importer.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        functions = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"_format_float", "_portable_collision_obj"}
        ]
        namespace = {"math": math}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)

        lines, vertex_count, triangle_count = namespace["_portable_collision_obj"](
            "\n".join(
                (
                    "v 100 300 200",
                    "v 0 0 0",
                    "v 100 0 0",
                    "v 0 100 0",
                    "f 1/1/1 2/2/2 3/3/3 4/4/4",
                )
            )
        )

        self.assertEqual(vertex_count, 4)
        self.assertEqual(triangle_count, 2)
        self.assertIn("v 1 2 3", lines)
        self.assertEqual(lines[-2:], ["f 1 2 3", "f 1 3 4"])
        native_source = source.read_text(encoding="utf-8")
        self.assertNotIn('get_editor_property("vertex_data")', native_source)
        self.assertIn('"unreal_static_mesh_lod0_convexification_v1"', native_source)

    def test_fbx_scale_is_uniformly_corrected_once_from_declared_dimensions(self) -> None:
        source = Path(__file__).resolve().parents[1] / "scripts" / "native_ue_asset_importer.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        functions = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"_dimensions_match_source", "_corrected_fbx_import_scale"}
        ]
        namespace = {"math": math, "Path": Path, "Any": object}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
        corrected_scale = namespace["_corrected_fbx_import_scale"]

        corrected = corrected_scale(
            Path("cup.fbx"),
            [1321.569091796875, 999.9079971313477, 1051.3495593070984],
            [0.131555, 0.105135, 0.099992],
            current_scale=37.5,
            source_kind="user_file",
        )

        self.assertIsNotNone(corrected)
        self.assertAlmostEqual(corrected, 0.375, delta=0.02)
        self.assertIsNone(
            corrected_scale(
                Path("cup.fbx"),
                [13.1555, 10.5135, 9.9992],
                [0.131555, 0.105135, 0.099992],
                current_scale=float(corrected),
                source_kind="user_file",
            )
        )
        self.assertIsNone(
            corrected_scale(
                Path("surface.obj"),
                [1300.0, 1000.0, 1000.0],
                [0.13, 0.10, 0.10],
                current_scale=1.0,
                source_kind="model_generation",
            )
        )


if __name__ == "__main__":
    unittest.main()
