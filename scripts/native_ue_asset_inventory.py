from __future__ import annotations

import json
import os
import traceback
from pathlib import Path
from typing import Any

import unreal


REQUEST_PATH = Path(os.environ["SIM_HARNESS_UE_ASSET_SCAN_REQUEST"]).expanduser().resolve()
RESULT_PATH = Path(os.environ["SIM_HARNESS_UE_ASSET_SCAN_RESULT"]).expanduser().resolve()
SCHEMA = "harness_ue_asset_inventory_scan_v1"


def main() -> None:
    request = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
    try:
        project_content = Path(request["project_content_root"]).expanduser().resolve()
        package_roots = [str(value) for value in request.get("package_roots") or []]
        if not package_roots or any(not value.startswith("/Game/") for value in package_roots):
            raise RuntimeError("package_roots must contain explicit /Game paths")
        asset_registry = unreal.AssetRegistryHelpers.get_asset_registry()
        mesh_editor = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for package_root in package_roots:
            for data in asset_registry.get_assets_by_path(unreal.Name(package_root), recursive=True):
                class_name = asset_class_name(data)
                if class_name != "StaticMesh":
                    continue
                object_path = asset_object_path(data)
                if object_path in seen:
                    continue
                seen.add(object_path)
                rows.append(scan_static_mesh(data, project_content=project_content, mesh_editor=mesh_editor))
        rows.sort(key=lambda row: row["object_path"])
        result = {
            "schema_version": SCHEMA,
            "status": "pass",
            "ue_version": str(unreal.SystemLibrary.get_engine_version()),
            "package_roots": package_roots,
            "project_content_root": str(project_content),
            "asset_count": len(rows),
            "collision_ready_count": sum(1 for row in rows if int(row.get("simple_collision_count") or 0) > 0),
            "assets": rows,
        }
    except Exception as exc:
        result = {
            "schema_version": SCHEMA,
            "status": "fail",
            "failure_code": "ue_asset_inventory_scan_failed",
            "failure_message": str(exc),
            "traceback": traceback.format_exc(limit=30),
            "assets": [],
        }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def scan_static_mesh(data: Any, *, project_content: Path, mesh_editor: Any) -> dict[str, Any]:
    asset = data.get_asset()
    object_path = asset_object_path(data)
    package_name = str(data.package_name)
    package_file = project_content / f"{package_name.removeprefix('/Game/')}.uasset"
    if not asset or asset.get_class().get_name() != "StaticMesh":
        raise RuntimeError(f"Asset Registry row did not load as StaticMesh: {object_path}")
    bounds = asset.get_bounding_box()
    size_m = [
        float(bounds.max.x - bounds.min.x) / 100.0,
        float(bounds.max.y - bounds.min.y) / 100.0,
        float(bounds.max.z - bounds.min.z) / 100.0,
    ]
    material_paths = []
    for index in range(max(0, int(asset.get_num_sections(0)))):
        material = asset.get_material(index)
        if material:
            material_paths.append(str(material.get_path_name()))
    simple_collision_count = int(mesh_editor.get_convex_collision_count(asset)) if mesh_editor is not None else 0
    return {
        "name": str(data.asset_name),
        "object_path": object_path,
        "package_name": package_name,
        "package_file": str(package_file),
        "class_name": "StaticMesh",
        "bbox_size_m": size_m,
        "lod0_section_count": int(asset.get_num_sections(0)),
        "simple_collision_count": simple_collision_count,
        "material_paths": sorted(set(material_paths)),
        "dependency_scan_status": "ue_package_load_passed",
    }


def asset_class_name(data: Any) -> str:
    try:
        return str(data.asset_class_path.asset_name)
    except Exception:
        return str(data.asset_class)


def asset_object_path(data: Any) -> str:
    return f"{data.package_name}.{data.asset_name}"


main()
