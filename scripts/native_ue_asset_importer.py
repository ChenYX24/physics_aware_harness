from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import traceback
from pathlib import Path
from typing import Any

import unreal


REQUEST_PATH = Path(os.environ["SIM_HARNESS_UE_IMPORT_REQUEST"]).expanduser().resolve() if os.environ.get("SIM_HARNESS_UE_IMPORT_REQUEST") else None
RESULT_PATH = Path(os.environ["SIM_HARNESS_UE_IMPORT_RESULT"]).expanduser().resolve() if os.environ.get("SIM_HARNESS_UE_IMPORT_RESULT") else None
BATCH_REQUEST_PATH = Path(os.environ["SIM_HARNESS_UE_IMPORT_BATCH_REQUEST"]).expanduser().resolve() if os.environ.get("SIM_HARNESS_UE_IMPORT_BATCH_REQUEST") else None
BATCH_RESULT_PATH = Path(os.environ["SIM_HARNESS_UE_IMPORT_BATCH_RESULT"]).expanduser().resolve() if os.environ.get("SIM_HARNESS_UE_IMPORT_BATCH_RESULT") else None
CONTENT_ROOT = Path(os.environ["SIM_HARNESS_UE_IMPORT_PROJECT_CONTENT"]).expanduser().resolve()


def main() -> None:
    if BATCH_REQUEST_PATH is not None and BATCH_RESULT_PATH is not None:
        payload = json.loads(BATCH_REQUEST_PATH.read_text(encoding="utf-8"))
        requests = payload.get("requests") or []
        _write_result({"results": [_import_one(request) for request in requests]}, BATCH_RESULT_PATH)
        return
    if REQUEST_PATH is None or RESULT_PATH is None:
        raise RuntimeError("Unreal importer request/result environment is incomplete")
    request = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
    _write_result(_import_one(request), RESULT_PATH)


def _import_one(request: dict[str, Any]) -> dict[str, Any]:
    try:
        source = _source_file(request)
        asset_name = _safe_asset_name(str(request.get("desired_name") or request["asset_id"]))
        destination_path = _safe_destination_path(str(request.get("destination_path") or "/Game/Generated/Provider"))
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
        source_kind = str(request.get("source_kind") or "")
        remote_asset = source_kind in {"external_site", "model_generation", "user_file"}
        options.import_as_skeletal = False
        options.import_mesh = True
        options.import_materials = remote_asset
        options.import_textures = remote_asset
        options.static_mesh_import_data.combine_meshes = True
        options.static_mesh_import_data.generate_lightmap_u_vs = True
        options.static_mesh_import_data.auto_generate_collision = True
        # The outer launcher materializes a temporary centimeter-normalized OBJ.
        # UE 5.7's OBJ/Interchange path ignores FbxImportUI's meter scale.
        import_uniform_scale = float(request.get("import_uniform_scale") or 1.0)
        if not math.isfinite(import_uniform_scale) or import_uniform_scale <= 0.0:
            raise RuntimeError("import_uniform_scale must be finite and positive")
        options.static_mesh_import_data.import_uniform_scale = import_uniform_scale
        task.options = options
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

        asset = unreal.load_asset(object_path)
        if not asset or asset.get_class().get_name() != "StaticMesh":
            raise RuntimeError(f"imported object is not a StaticMesh: {object_path}")
        if int(asset.get_num_sections(0)) <= 0:
            raise RuntimeError(f"imported StaticMesh has no LOD0 render sections: {object_path}")
        initial_size_cm = _asset_size_cm(asset)
        effective_import_uniform_scale = import_uniform_scale
        corrected_scale = _corrected_fbx_import_scale(
            source,
            initial_size_cm,
            request.get("expected_size_m"),
            current_scale=import_uniform_scale,
            source_kind=source_kind,
        )
        if corrected_scale is not None:
            effective_import_uniform_scale = corrected_scale
            options.static_mesh_import_data.import_uniform_scale = corrected_scale
            unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
            asset = unreal.load_asset(object_path)
            if not asset or asset.get_class().get_name() != "StaticMesh":
                raise RuntimeError(f"corrected FBX import is not a StaticMesh: {object_path}")
            if int(asset.get_num_sections(0)) <= 0:
                raise RuntimeError(f"corrected FBX import has no LOD0 render sections: {object_path}")
        actual_size_cm = _validate_dimensions(
            asset,
            request.get("expected_size_m"),
            source_kind=str(request.get("source_kind") or ""),
        )
        geometry_analysis = None
        geometry_analysis_error = None
        try:
            geometry_analysis = _analyze_asset_geometry(asset)
        except Exception as exc:
            geometry_analysis_error = str(exc)
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
        portable_collision_artifact = None
        portable_collision_error = None
        try:
            portable_collision_artifact = _export_portable_collision_artifact(asset, request)
        except Exception as exc:
            # UE runtime qualification remains useful to UE-only cases.  A
            # solver that requires the portable collision contract will reject
            # this asset as capability_missing instead of substituting visual
            # geometry or a bounds proxy.
            portable_collision_error = str(exc)
        unreal.EditorAssetLibrary.save_loaded_asset(asset, only_if_is_dirty=False)

        package_file = CONTENT_ROOT / destination_path.removeprefix("/Game/") / f"{asset_name}.uasset"
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
            "portable_collision_artifact": portable_collision_artifact,
            "import_validation": {
                "loaded_class": asset.get_class().get_name(),
                "lod0_section_count": int(asset.get_num_sections(0)),
                "collision_body_setup_present": True,
                "convex_collision_count": collision_count,
                "portable_collision_artifact_status": (
                    "qualified" if portable_collision_artifact is not None else "unavailable"
                ),
                "portable_collision_artifact_error": portable_collision_error,
                "actual_size_cm": actual_size_cm,
                "geometry_analysis": geometry_analysis,
                "geometry_analysis_error": geometry_analysis_error,
                "expected_size_m": request.get("expected_size_m"),
                "obj_meter_to_ue_centimeter_scale": 100.0,
                "normalized_source_unit": "centimeter",
                "source_format": source.suffix.lstrip(".").casefold(),
                "import_uniform_scale": effective_import_uniform_scale,
                "requested_import_uniform_scale": import_uniform_scale,
                "effective_import_uniform_scale": effective_import_uniform_scale,
                "scale_correction_applied": corrected_scale is not None,
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
    return result


def _export_portable_collision_artifact(asset: Any, request: dict[str, Any]) -> dict[str, Any]:
    path_value = request.get("portable_collision_artifact_path")
    if not isinstance(path_value, str) or not path_value.strip():
        raise RuntimeError("portable collision artifact destination is not declared")
    destination = Path(path_value).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="harness_collision_mesh_") as temporary:
        exported_obj = Path(temporary) / "lod0.obj"
        _export_static_mesh_obj(asset, exported_obj)
        lines, vertex_count, triangle_count = _portable_collision_obj(
            exported_obj.read_text(encoding="utf-8", errors="strict")
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    payload = destination.read_bytes()
    return {
        "schema_version": "harness_portable_collision_mesh_v1",
        "role": "qualified_collision_mesh",
        "local_path": str(destination),
        "format": "obj",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "materialized": True,
        "coordinate_system": "asset_local_z_up_m",
        "artifact_to_asset_transform": {
            "matrix4x4": [
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]
        },
        "qualification_source": "unreal_static_mesh_lod0_convexification_v1",
        "convex_part_count": 1,
        "vertex_count": vertex_count,
        "triangle_count": triangle_count,
    }


def _portable_collision_obj(source: str) -> tuple[list[str], int, int]:
    vertices: list[list[float]] = []
    polygons: list[list[int]] = []
    for line in source.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[0] == "v":
            # StaticMeshExporterOBJ writes UE asset-local X, Z, Y in centimeters.
            vertex = [float(fields[1]) / 100.0, float(fields[3]) / 100.0, float(fields[2]) / 100.0]
            if any(not math.isfinite(value) for value in vertex):
                raise RuntimeError("exported StaticMesh contains non-finite vertices")
            vertices.append(vertex)
        elif len(fields) >= 4 and fields[0] == "f":
            polygons.append([int(value.split("/", 1)[0]) for value in fields[1:]])
    if len(vertices) < 4 or not polygons:
        raise RuntimeError("exported StaticMesh LOD0 has incomplete collision geometry")
    if any(index <= 0 or index > len(vertices) for polygon in polygons for index in polygon):
        raise RuntimeError("exported StaticMesh LOD0 has invalid face indices")
    lines = [
        "# harness_portable_collision_mesh_v1",
        "# UE asset-local Z-up coordinates in meters; Genesis convexifies this closed LOD0 surface",
        "o collision_hull_0000",
        *("v " + " ".join(_format_float(value) for value in vertex) for vertex in vertices),
    ]
    triangle_count = 0
    for polygon in polygons:
        for index in range(1, len(polygon) - 1):
            lines.append(f"f {polygon[0]} {polygon[index]} {polygon[index + 1]}")
            triangle_count += 1
    return lines, len(vertices), triangle_count


def _format_float(value: float) -> str:
    text = format(float(value), ".17g")
    return "0" if text in {"-0", "-0.0"} else text


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


def _safe_destination_path(value: str) -> str:
    path = value.rstrip("/")
    if not re.fullmatch(r"/Game(?:/[A-Za-z0-9_]+)+", path):
        raise RuntimeError(f"backend import destination_path must be a package path under /Game: {value!r}")
    return path


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
    # Poly Haven's API dimensions can include rig/open-pose transforms that are
    # absent from the imported static pose. Validate the global physical scale
    # using the longest dimension and at least one other axis. Sorting tolerates
    # source/up-axis conventions. Procedural and fitted sources keep the strict
    # per-axis contract.
    fbx_with_authored_axes = str(source_kind).casefold() in {"external_site", "user_file"}
    actual_values = sorted(actual_size_cm) if fbx_with_authored_axes else actual_size_cm
    expected_values = sorted(expected_size_cm) if fbx_with_authored_axes else expected_size_cm
    relative_tolerance = 0.20 if str(source_kind).casefold() == "external_site" else 0.05 if fbx_with_authored_axes else 0.01
    absolute_tolerance_cm = 1.0 if str(source_kind).casefold() == "external_site" else 0.2 if fbx_with_authored_axes else 0.1
    matches = [
        abs(actual - expected) <= max(absolute_tolerance_cm, abs(expected) * relative_tolerance)
        for actual, expected in zip(actual_values, expected_values)
    ]
    if fbx_with_authored_axes:
        return matches[-1] and sum(matches) >= 2
    return all(matches)


def _asset_size_cm(asset: Any) -> list[float]:
    bounds = asset.get_bounding_box()
    return [
        float(bounds.max.x - bounds.min.x),
        float(bounds.max.y - bounds.min.y),
        float(bounds.max.z - bounds.min.z),
    ]


def _analyze_asset_geometry(asset: Any) -> dict[str, Any]:
    points = _exported_asset_vertices(asset)
    if len(points) < 12 or any(not all(math.isfinite(value) for value in point) for point in points):
        raise RuntimeError("imported StaticMesh has insufficient finite LOD0 vertices")
    return {
        "schema_version": "harness_asset_geometry_analysis_v1",
        "vertex_count": len(points),
        "axisymmetric_z_frame": _axisymmetric_z_frame(points),
    }


def _exported_asset_vertices(asset: Any) -> list[list[float]]:
    with tempfile.TemporaryDirectory(prefix="harness_static_mesh_") as temporary:
        destination = Path(temporary) / "lod0.obj"
        _export_static_mesh_obj(asset, destination)
        points = []
        for line in destination.read_text(encoding="utf-8", errors="strict").splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[0] == "v":
                # StaticMeshExporterOBJ writes UE asset-local X, Z, Y.
                points.append([float(fields[1]), float(fields[3]), float(fields[2])])
        return points


def _export_static_mesh_obj(asset: Any, destination: Path) -> None:
    task = unreal.AssetExportTask()
    task.object = asset
    task.exporter = unreal.StaticMeshExporterOBJ()
    task.filename = str(destination)
    task.automated = True
    task.prompt = False
    task.replace_identical = True
    if not unreal.Exporter.run_asset_export_task(task) or not destination.is_file():
        errors = "; ".join(str(value) for value in (task.errors or []))
        raise RuntimeError(f"could not export imported StaticMesh LOD0 as OBJ: {errors}")


def _axisymmetric_z_frame(points: list[list[float]]) -> dict[str, Any]:
    z_values = [point[2] for point in points]
    span_z = max(z_values) - min(z_values)
    if span_z <= 1e-6:
        return {"status": "unavailable", "method": "robust_horizontal_ring_fit_v1"}
    bucket_width = max(span_z * 1e-4, 0.01)
    buckets: dict[int, list[list[float]]] = {}
    for point in points:
        buckets.setdefault(round(point[2] / bucket_width), []).append(point)
    rings = []
    for ring_points in buckets.values():
        if len(ring_points) < 12:
            continue
        xs = sorted(point[0] for point in ring_points)
        ys = sorted(point[1] for point in ring_points)
        center_x = (_quantile(xs, 0.10) + _quantile(xs, 0.90)) / 2.0
        center_y = (_quantile(ys, 0.10) + _quantile(ys, 0.90)) / 2.0
        span_x = _quantile(xs, 0.90) - _quantile(xs, 0.10)
        span_y = _quantile(ys, 0.90) - _quantile(ys, 0.10)
        if min(span_x, span_y) <= 1e-6 or max(span_x, span_y) / min(span_x, span_y) > 1.10:
            continue
        radii = sorted(math.hypot(point[0] - center_x, point[1] - center_y) for point in ring_points)
        median_radius = _quantile(radii, 0.50)
        radial_mad = _quantile(sorted(abs(radius - median_radius) for radius in radii), 0.50)
        if median_radius <= 1e-6 or radial_mad / median_radius > 0.05:
            continue
        rings.append(
            {
                "center_x": center_x,
                "center_y": center_y,
                "z": sum(point[2] for point in ring_points) / len(ring_points),
                "radius": median_radius,
            }
        )
    if len(rings) < 3:
        return {"status": "unavailable", "method": "robust_horizontal_ring_fit_v1", "ring_count": len(rings)}
    center_x = _quantile(sorted(ring["center_x"] for ring in rings), 0.50)
    center_y = _quantile(sorted(ring["center_y"] for ring in rings), 0.50)
    residuals = sorted(math.hypot(ring["center_x"] - center_x, ring["center_y"] - center_y) for ring in rings)
    radii = sorted(ring["radius"] for ring in rings)
    center_residual = _quantile(residuals, 0.50)
    median_radius = _quantile(radii, 0.50)
    ring_z = [ring["z"] for ring in rings]
    axial_coverage = (max(ring_z) - min(ring_z)) / span_z
    if center_residual > max(0.05, median_radius * 0.02) or axial_coverage < 0.50:
        return {
            "status": "unavailable",
            "method": "robust_horizontal_ring_fit_v1",
            "ring_count": len(rings),
            "center_residual_cm": center_residual,
            "axial_coverage": axial_coverage,
        }
    return {
        "status": "verified",
        "method": "robust_horizontal_ring_fit_v1",
        "frame_origin_cm": [center_x, center_y, (min(ring_z) + max(ring_z)) / 2.0],
        "axis_direction": [0.0, 0.0, 1.0],
        "ring_count": len(rings),
        "center_residual_cm": center_residual,
        "axial_coverage": axial_coverage,
    }


def _quantile(values: list[float], fraction: float) -> float:
    position = max(0.0, min(1.0, float(fraction))) * (len(values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(values[lower])
    weight = position - lower
    return float(values[lower]) * (1.0 - weight) + float(values[upper]) * weight


def _corrected_fbx_import_scale(
    source: Path,
    actual_size_cm: list[float],
    expected_size_m: Any,
    *,
    current_scale: float,
    source_kind: str,
) -> float | None:
    """Return one uniform FBX refit when the declared dimensions disagree."""
    if source.suffix.casefold() != ".fbx" or expected_size_m is None:
        return None
    if not isinstance(expected_size_m, list) or len(expected_size_m) != 3:
        raise RuntimeError("backend import request expected_size_m must contain three values when provided")
    expected_size_cm = [float(value) * 100.0 for value in expected_size_m]
    if _dimensions_match_source(actual_size_cm, expected_size_cm, source_kind=source_kind):
        return None
    if any(not math.isfinite(value) or value <= 0.0 for value in [*actual_size_cm, *expected_size_cm]):
        raise RuntimeError("FBX scale correction requires finite positive bounds")
    actual_diagonal = math.sqrt(sum(value * value for value in actual_size_cm))
    expected_diagonal = math.sqrt(sum(value * value for value in expected_size_cm))
    corrected = float(current_scale) * expected_diagonal / actual_diagonal
    if not math.isfinite(corrected) or corrected <= 0.0:
        raise RuntimeError("FBX scale correction produced an invalid import scale")
    return corrected


def _validate_dimensions(asset: Any, expected_size_m: Any, *, source_kind: str = "") -> list[float]:
    actual_size_cm = _asset_size_cm(asset)
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


def _write_result(result: dict[str, Any], result_path: Path) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = result_path.with_suffix(result_path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(result_path)
    if "results" in result:
        print(json.dumps({"status": "complete", "result_count": len(result["results"])}))
    else:
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
