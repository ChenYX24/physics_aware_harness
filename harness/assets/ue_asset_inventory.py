from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any, Mapping

from harness.assets.asset_registry import AssetRegistry
from harness.core.artifact_schema import read_json, write_json


UE_ASSET_SCAN_SCHEMA = "harness_ue_asset_inventory_scan_v1"
UE_ASSET_REGISTRATION_SCHEMA = "harness_ue_asset_inventory_registration_v1"


class UEAssetInventoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def register_ue_asset_inventory(
    scan_path: str | Path,
    *,
    registry: AssetRegistry,
    receipt_path: str | Path,
    source_uri_root: str,
    source_name: str,
    license_name: str = "research_use_user_attested_nonredistributable",
    license_tier: str = "local_preview",
) -> dict[str, Any]:
    scan = read_json(Path(scan_path).expanduser().resolve())
    if scan.get("schema_version") != UE_ASSET_SCAN_SCHEMA or scan.get("status") != "pass":
        raise UEAssetInventoryError("ue_asset_inventory_scan_invalid", "UE asset scan did not pass")
    if not registry.writable:
        raise UEAssetInventoryError("catalog_not_writable", f"UE asset inventory requires writable Catalog: {registry.path}")
    if license_tier not in {"local_preview", "reference"}:
        raise UEAssetInventoryError("ue_asset_inventory_license_tier_invalid", "license tier must be local_preview or reference")
    rows = [row for row in scan.get("assets") or [] if isinstance(row, Mapping)]
    if not rows:
        raise UEAssetInventoryError("ue_asset_inventory_empty", "UE asset scan contains no assets")
    registered: list[dict[str, Any]] = []
    for row in rows:
        asset = catalog_asset_from_scan(
            row,
            source_uri_root=source_uri_root,
            source_name=source_name,
            license_name=license_name,
            license_tier=license_tier,
        )
        result = registry.register_asset(asset)
        if result.get("status") != "registered":
            raise UEAssetInventoryError(
                str(result.get("code") or "catalog_registration_failed"),
                str(result.get("message") or f"could not register {asset['asset_id']}"),
            )
        registered.append(
            {
                "asset_id": asset["asset_id"],
                "name": asset["name"],
                "ue_path": asset["ue_path"],
                "collision_ready": bool(asset.get("collider")),
                "bbox_size_m": asset.get("bbox_size_m"),
            }
        )
    receipt = {
        "schema_version": UE_ASSET_REGISTRATION_SCHEMA,
        "status": "registered",
        "source_name": source_name,
        "source_uri_root": source_uri_root,
        "catalog": str(registry.path),
        "scan_receipt": str(Path(scan_path).expanduser().resolve()),
        "asset_count": len(registered),
        "collision_ready_count": sum(1 for row in registered if row["collision_ready"]),
        "visual_only_count": sum(1 for row in registered if not row["collision_ready"]),
        "assets": registered,
    }
    write_json(Path(receipt_path).expanduser().resolve(), receipt)
    return receipt


def catalog_asset_from_scan(
    row: Mapping[str, Any],
    *,
    source_uri_root: str,
    source_name: str,
    license_name: str,
    license_tier: str,
) -> dict[str, Any]:
    object_path = str(row.get("object_path") or "")
    package_file = Path(str(row.get("package_file") or "")).expanduser().resolve()
    if not object_path.startswith("/Game/") or not package_file.is_file():
        raise UEAssetInventoryError("ue_asset_inventory_row_invalid", f"scan row is not materialized: {object_path}")
    bbox = row.get("bbox_size_m")
    if not (
        isinstance(bbox, list)
        and len(bbox) == 3
        and all(isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) > 0.0 for value in bbox)
    ):
        raise UEAssetInventoryError("ue_asset_inventory_bounds_invalid", f"scan row has invalid bounds: {object_path}")
    size = [float(value) for value in bbox]
    name = str(row.get("name") or object_path.rsplit("/", 1)[-1].split(".", 1)[0])
    semantic_name = semantic_asset_name(name)
    category_l1, category_l2 = infer_category(semantic_name)
    material_category = infer_material([semantic_name, *(str(value) for value in row.get("material_paths") or [])])
    friction, restitution, density = material_defaults(material_category)
    simple_collision_count = int(row.get("simple_collision_count") or 0)
    collision_ready = simple_collision_count > 0
    file_sha256 = sha256_file(package_file)
    volume = math.prod(size)
    asset_id = f"ue_inventory.{slug(source_name)}.{slug(object_path)}"
    tags = sorted({*tokenize(semantic_name), category_l1, category_l2, source_name})
    return {
        "asset_id": asset_id,
        "name": semantic_name,
        "semantic_name": semantic_name,
        "description": f"UE-scanned StaticMesh from {source_name}: {semantic_name}",
        "aliases": [name, semantic_name, object_path],
        "tags": tags,
        "usage_groups": [category_l1, f"{category_l1}/{category_l2}"],
        "category": category_l1,
        "category_l1": category_l1,
        "category_l2": category_l2,
        "type": "StaticMesh",
        "asset_type": "StaticMesh",
        "asset_kind": "StaticMesh",
        "source_kind": "local_ue_content_bundle",
        "source_uri": f"{source_uri_root.rstrip('/')}/{object_path.removeprefix('/Game/')}",
        "license": license_name,
        "license_tier": license_tier,
        "redistribution": {"original_asset_files_allowed": False, "derived_video_allowed": True},
        "quality_status": "local_preview",
        "lifecycle_status": "runtime_bound",
        "materialized": True,
        "ue_path": object_path,
        "class_name": "StaticMesh",
        "local_path": str(package_file),
        "sha256": file_sha256,
        "byte_size": package_file.stat().st_size,
        "bbox_size_m": size,
        "authored_size_m": size,
        "preserve_authored_scale": True,
        "collider": "mesh" if collision_ready else None,
        "collision_profile": "PhysicsActor" if collision_ready else None,
        "mass_kg": max(volume * density, 0.001),
        "mass_estimate": {"method": "bbox_volume_material_density", "requires_case_override": True},
        "material": {"static_friction": friction + 0.1, "dynamic_friction": friction, "restitution": restitution},
        "collision": {
            "present": collision_ready,
            "kind": "ue_simple_collision" if collision_ready else "none",
            "simple_collision_count": simple_collision_count,
            "qualification_scope": "ue_editor_introspection",
        },
        "paths": {"ue5": object_path, "local_file": str(package_file)},
        "files": [
            {
                "role": "primary",
                "local_path": str(package_file),
                "format": "uasset",
                "sha256": file_sha256,
                "byte_size": package_file.stat().st_size,
                "materialized": True,
            }
        ],
        "ue": {
            "object_path": object_path,
            "package_name": str(row.get("package_name") or object_path.split(".", 1)[0]),
            "class_name": "StaticMesh",
            "dependencies": [],
            "dependency_scan_status": str(row.get("dependency_scan_status") or "ue_package_load_passed"),
            "material_paths": [str(value) for value in row.get("material_paths") or []],
        },
        "backend_bindings": {
            "unreal": {
                "object_path": object_path,
                "class_name": "StaticMesh",
                "materialized": True,
                "runtime_ready": True,
            }
        },
        "provenance": {
            "source_name": source_name,
            "scan_method": "unreal_asset_registry_and_static_mesh_editor_v1",
            "lod0_section_count": int(row.get("lod0_section_count") or 0),
        },
    }


def semantic_asset_name(name: str) -> str:
    value = re.sub(r"^(SM|SK|M|MI)_", "", str(name), flags=re.IGNORECASE)
    return re.sub(r"[_-]+", " ", value).strip()


def infer_category(name: str) -> tuple[str, str]:
    tokens = tokenize(name)
    groups = {
        "furniture": {"chair", "stool", "sofa", "table", "shelf", "shelves", "cupboard", "commode", "sideboard", "cabinet", "rack", "workbench"},
        "container": {"bottle", "cup", "glass", "wineglass", "jar", "jug", "bucket", "box", "crate", "barrel", "drum", "can", "bowl", "vase"},
        "tool": {"ladder", "fork", "knife", "spoon", "pan", "mixer", "cart", "trolley", "handtruck"},
        "lighting": {"lamp", "light", "candle"},
    }
    for category, words in groups.items():
        matches = tokens.intersection(words)
        if matches:
            return ("prop" if category != "furniture" else "furniture", sorted(matches)[0])
    return "prop", "generic"


def infer_material(values: list[str]) -> str:
    text = " ".join(values).casefold()
    for material in ("metal", "steel", "iron", "wood", "glass", "plastic", "rubber", "concrete", "cloth", "cardboard"):
        if material in text:
            return "metal" if material in {"steel", "iron"} else material
    return "generic"


def material_defaults(material: str) -> tuple[float, float, float]:
    return {
        "metal": (0.32, 0.18, 7800.0),
        "wood": (0.4, 0.15, 700.0),
        "glass": (0.25, 0.08, 2500.0),
        "plastic": (0.38, 0.2, 950.0),
        "rubber": (0.65, 0.55, 1100.0),
        "concrete": (0.6, 0.05, 2300.0),
        "cloth": (0.55, 0.03, 300.0),
        "cardboard": (0.45, 0.08, 300.0),
        "generic": (0.4, 0.1, 1000.0),
    }[material]


def tokenize(value: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", value.casefold()) if token}


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_") or "unnamed"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
