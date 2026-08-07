from __future__ import annotations

import hashlib
import json
import math
import os
import re
import traceback
from pathlib import Path
from typing import Any

import unreal


REQUEST_PATH = Path(os.environ["SIM_HARNESS_UE_IMPORT_REQUEST"]).expanduser().resolve()
RESULT_PATH = Path(os.environ["SIM_HARNESS_UE_IMPORT_RESULT"]).expanduser().resolve()
CONTENT_ROOT = Path(os.environ["SIM_HARNESS_UE_IMPORT_PROJECT_CONTENT"]).expanduser().resolve()


def main() -> None:
    request = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
    try:
        source = _source_file(request)
        asset_name = _safe_asset_name(str(request.get("desired_name") or request["asset_id"]))
        destination_path = "/Game/Generated/Provider"
        object_path = f"{destination_path}/{asset_name}.{asset_name}"
        task = unreal.AssetImportTask()
        task.filename = str(source)
        task.destination_path = destination_path
        task.destination_name = asset_name
        task.automated = True
        task.replace_existing = True
        task.replace_existing_settings = True
        task.save = True
        options = unreal.FbxImportUI()
        remote_asset = str(request.get("source_kind") or "") in {"external_site", "model_generation"}
        options.import_as_skeletal = False
        options.import_mesh = True
        options.import_materials = remote_asset
        options.import_textures = remote_asset
        options.static_mesh_import_data.combine_meshes = True
        options.static_mesh_import_data.generate_lightmap_u_vs = True
        options.static_mesh_import_data.auto_generate_collision = True
        # The outer launcher materializes a temporary centimeter-normalized OBJ.
        # UE 5.7's OBJ/Interchange path ignores FbxImportUI's meter scale.
        options.static_mesh_import_data.import_uniform_scale = 1.0
        task.options = options
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

        asset = unreal.load_asset(object_path)
        if not asset or asset.get_class().get_name() != "StaticMesh":
            raise RuntimeError(f"imported object is not a StaticMesh: {object_path}")
        if int(asset.get_num_sections(0)) <= 0:
            raise RuntimeError(f"imported StaticMesh has no LOD0 render sections: {object_path}")
        actual_size_cm = _validate_dimensions(
            asset,
            request.get("expected_size_m"),
            source_kind=str(request.get("source_kind") or ""),
        )
        mesh_editor = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
        if mesh_editor is None:
            raise RuntimeError("StaticMeshEditorSubsystem is unavailable for collision validation")
        collision_count = int(mesh_editor.get_convex_collision_count(asset))
        if collision_count <= 0:
            generated_collision = mesh_editor.set_convex_decomposition_collisions(asset, 1, 8, 100000)
            collision_count = int(mesh_editor.get_convex_collision_count(asset))
            if generated_collision is False or collision_count <= 0:
                raise RuntimeError(f"could not generate simple collision for imported StaticMesh: {object_path}")
        body_setup = (
            asset.get_body_setup()
            if hasattr(asset, "get_body_setup")
            else asset.get_editor_property("body_setup")
        )
        if body_setup is None:
            raise RuntimeError(f"imported StaticMesh has no collision body setup: {object_path}")
        unreal.EditorAssetLibrary.save_loaded_asset(asset, only_if_is_dirty=False)

        package_file = CONTENT_ROOT / "Generated" / "Provider" / f"{asset_name}.uasset"
        if not package_file.is_file():
            raise RuntimeError(f"saved Unreal package is missing: {package_file}")
        payload = package_file.read_bytes()
        dependencies = _imported_dependencies(task, primary_object_path=object_path)
        result = {
            "schema_version": "harness_backend_asset_import_result_v1",
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "asset_id": request["asset_id"],
            "status": "fulfilled",
            "object_path": object_path,
            "class_name": "StaticMesh",
            "materialized": True,
            "runtime_ready": True,
            "files": [
                {
                    "role": "primary",
                    "local_path": str(package_file),
                    "format": "uasset",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "byte_size": len(payload),
                    "materialized": True,
                }
            ],
            "dependencies": dependencies,
            "import_validation": {
                "loaded_class": asset.get_class().get_name(),
                "lod0_section_count": int(asset.get_num_sections(0)),
                "collision_body_setup_present": True,
                "convex_collision_count": collision_count,
                "actual_size_cm": actual_size_cm,
                "expected_size_m": request.get("expected_size_m"),
                "obj_meter_to_ue_centimeter_scale": 100.0,
                "normalized_source_unit": "centimeter",
                "source_format": source.suffix.lstrip(".").casefold(),
                "materials_imported": remote_asset,
                "textures_imported": remote_asset,
            },
        }
    except Exception as exc:
        result = {
            "schema_version": "harness_backend_asset_import_result_v1",
            "request_id": request.get("request_id"),
            "request_digest": request.get("request_digest"),
            "asset_id": request.get("asset_id"),
            "status": "failed",
            "failure": {
                "code": "backend_asset_import_failed",
                "message": str(exc),
                "retriable": False,
            },
            "traceback": traceback.format_exc(limit=20),
        }
    _write_result(result)


def _source_file(request: dict[str, Any]) -> Path:
    sources = request.get("source_files") or []
    if len(sources) != 1:
        raise RuntimeError("UE static-mesh importer requires exactly one source file")
    source = Path(str(sources[0].get("local_path") or "")).expanduser().resolve()
    if source.suffix.casefold() not in {".obj", ".fbx"} or not source.is_file():
        raise RuntimeError(f"UE static-mesh importer requires a materialized OBJ or FBX: {source}")
    return source


def _imported_dependencies(task: Any, *, primary_object_path: str) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    for raw_path in task.imported_object_paths or []:
        object_path = str(raw_path)
        if object_path == primary_object_path:
            continue
        imported = unreal.load_asset(object_path)
        if imported is None:
            raise RuntimeError(f"imported dependency cannot be loaded: {object_path}")
        unreal.EditorAssetLibrary.save_loaded_asset(imported, only_if_is_dirty=False)
        package_path = object_path.split(".", 1)[0]
        if not package_path.startswith("/Game/"):
            raise RuntimeError(f"imported dependency is outside /Game: {object_path}")
        package_file = CONTENT_ROOT / f"{package_path.removeprefix('/Game/')}.uasset"
        if not package_file.is_file():
            raise RuntimeError(f"imported dependency package is missing: {package_file}")
        payload = package_file.read_bytes()
        dependencies.append(
            {
                "dependency_id": object_path,
                "package": package_path,
                "local_path": str(package_file),
                "format": "uasset",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "byte_size": len(payload),
                "materialized": True,
            }
        )
    return dependencies


def _safe_asset_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", value).strip("_")
    if not name:
        raise RuntimeError("backend import request has no usable asset name")
    return name[:120]


def _dimensions_match_source(
    actual_size_cm: list[float],
    expected_size_cm: list[float],
    *,
    source_kind: str,
) -> bool:
    if len(actual_size_cm) != 3 or len(expected_size_cm) != 3:
        return False
    if any(not math.isfinite(value) or value <= 0.0 for value in [*actual_size_cm, *expected_size_cm]):
        return False
    # Poly Haven's API dimensions can include source-node transforms or small
    # non-rendering parts that are absent from the combined FBX StaticMesh.
    # Validate that import retained the same physical scale without distorting
    # the authored FBX to reproduce advisory metadata exactly.  Sorting also
    # tolerates source/up-axis conventions.  Procedural and fitted sources keep
    # the strict per-axis contract.
    external_fbx = str(source_kind).casefold() == "external_site"
    actual_values = sorted(actual_size_cm) if external_fbx else actual_size_cm
    expected_values = sorted(expected_size_cm) if external_fbx else expected_size_cm
    relative_tolerance = 0.20 if external_fbx else 0.01
    absolute_tolerance_cm = 1.0 if external_fbx else 0.1
    return all(
        abs(actual - expected) <= max(absolute_tolerance_cm, abs(expected) * relative_tolerance)
        for actual, expected in zip(actual_values, expected_values)
    )


def _validate_dimensions(asset: Any, expected_size_m: Any, *, source_kind: str = "") -> list[float]:
    bounds = asset.get_bounding_box()
    actual_size_cm = [
        float(bounds.max.x - bounds.min.x),
        float(bounds.max.y - bounds.min.y),
        float(bounds.max.z - bounds.min.z),
    ]
    if expected_size_m is None:
        if any(value <= 0 for value in actual_size_cm):
            raise RuntimeError(f"imported StaticMesh has degenerate bounds: {actual_size_cm}")
        return actual_size_cm
    if not isinstance(expected_size_m, list) or len(expected_size_m) != 3:
        raise RuntimeError("backend import request expected_size_m must contain three values when provided")
    expected_size_cm = [float(value) * 100.0 for value in expected_size_m]
    if not _dimensions_match_source(actual_size_cm, expected_size_cm, source_kind=source_kind):
        raise RuntimeError(
            f"imported StaticMesh bounds mismatch: actual_cm={actual_size_cm}, expected_cm={expected_size_cm}"
        )
    return actual_size_cm


def _write_result(result: dict[str, Any]) -> None:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = RESULT_PATH.with_suffix(RESULT_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(RESULT_PATH)
    print(json.dumps({"status": result["status"], "asset_id": result.get("asset_id")}))


try:
    main()
finally:
    try:
        unreal.EditorPythonScripting.set_keep_python_script_alive(False)
    except Exception:
        pass
    # The outer launcher owns process shutdown after it observes the atomic
    # result file. UE 5.7 can crash in LevelEditor teardown when a headless
    # ExecutePythonScript calls quit_editor() before the level editor is ready.
