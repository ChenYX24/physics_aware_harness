from __future__ import annotations

import copy
import hashlib
import re
import shutil
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from harness.assets.asset_registry import AssetRegistry
from harness.core.artifact_schema import read_json, write_json


PREPARED_MAP_REGISTRATION_SCHEMA = "harness_prepared_map_registration_v1"
PREPARED_MAP_QUALIFICATION_SCHEMA = "harness_prepared_map_qualification_v1"


class PreparedMapInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def prepare_map_input(
    source_content_root: str | Path,
    *,
    map_package: str,
    ue_project: str | Path,
    registration_root: str | Path,
    registry: AssetRegistry,
    source_uri: str | None = None,
    license_name: str = "UNVERIFIED_LOCAL_ENTITLEMENT",
    license_tier: str = "local_preview",
) -> dict[str, Any]:
    source_root = Path(source_content_root).expanduser().resolve()
    project = Path(ue_project).expanduser().resolve()
    receipt_root = Path(registration_root).expanduser().resolve()
    package = canonical_map_package(map_package)
    if not source_root.is_dir():
        raise PreparedMapInputError("prepared_map_source_missing", f"Content root is not a directory: {source_root}")
    if not project.is_file() or project.suffix.casefold() != ".uproject":
        raise PreparedMapInputError("prepared_map_project_missing", f"UE project is not a .uproject file: {project}")
    if not package.startswith("/Game/") or len(package.split("/")) < 4:
        raise PreparedMapInputError(
            "prepared_map_package_invalid",
            "prepared Map package must be an explicit /Game/<bundle>/... package path",
        )
    if license_tier not in {"local_preview", "reference"}:
        raise PreparedMapInputError("prepared_map_license_tier_invalid", "license tier must be local_preview or reference")
    if not registry.writable:
        raise PreparedMapInputError("catalog_not_writable", f"prepared Map registration requires writable Catalog: {registry.path}")

    relative_package = Path(package.removeprefix("/Game/"))
    source_map = source_root / relative_package.with_suffix(".umap")
    if not source_map.is_file():
        raise PreparedMapInputError("prepared_map_package_missing", f"Map package file is missing: {source_map}")
    bundle_name = relative_package.parts[0]
    source_bundle = source_root / bundle_name
    if not source_bundle.is_dir():
        raise PreparedMapInputError("prepared_map_bundle_missing", f"Top-level Content bundle is missing: {source_bundle}")

    source_inventory = content_tree_inventory(source_bundle)
    target_bundle = project.parent / "Content" / bundle_name
    _materialize_bundle(source_bundle, target_bundle, expected=source_inventory)
    target_inventory = content_tree_inventory(target_bundle)
    if target_inventory["tree_sha256"] != source_inventory["tree_sha256"]:
        raise PreparedMapInputError("prepared_map_materialization_mismatch", "materialized Content bundle differs from its source")
    target_map = project.parent / "Content" / relative_package.with_suffix(".umap")
    if not target_map.is_file():
        raise PreparedMapInputError("prepared_map_materialization_missing", f"materialized Map is missing: {target_map}")

    map_sha256 = sha256_file(target_map)
    resolved_source_uri = source_uri or (
        f"local-content://sha256/{source_inventory['tree_sha256']}/{quote(bundle_name)}"
    )
    asset_id = f"prepared_map.{slug(package)}.{source_inventory['tree_sha256'][:12]}"
    object_path = map_object_path(package)
    asset = {
        "asset_id": asset_id,
        "name": relative_package.name,
        "semantic_name": relative_package.name,
        "description": f"Prepared Unreal Map from the {bundle_name} Content bundle.",
        "category_l1": "map",
        "category_l2": "prepared_environment",
        "asset_type": "World",
        "type": "World",
        "tags": ["map", "prepared_environment", bundle_name],
        "aliases": [package, object_path, relative_package.name, bundle_name],
        "usage_groups": ["map", "map/prepared_environment"],
        "source_kind": "local_ue_content_bundle",
        "source_uri": resolved_source_uri,
        "license": license_name,
        "license_tier": license_tier,
        "quality_status": "local_preview",
        "lifecycle_status": "materialized_pending_ue_qualification",
        "sha256": map_sha256,
        "byte_size": target_map.stat().st_size,
        "ue_path": object_path,
        "real_3d_geometry": True,
        "materialized": True,
        "paths": {"ue5": object_path, "local_file": str(target_map)},
        "files": [
            {
                "role": "primary",
                "local_path": str(target_map),
                "format": "umap",
                "sha256": map_sha256,
                "byte_size": target_map.stat().st_size,
                "materialized": True,
            }
        ],
        "ue": {
            "object_path": object_path,
            "package_name": package,
            "class_name": "World",
            "dependencies": [],
            "dependency_discovery_status": "pending_ue_asset_registry",
        },
        "backend_bindings": {
            "unreal": {
                "object_path": object_path,
                "class_name": "World",
                "materialized": True,
                "runtime_ready": False,
            }
        },
        "bundle": {
            "bundle_id": f"prepared_map_bundle:{source_inventory['tree_sha256']}",
            "owner_asset": package,
            "content_root": str(target_bundle),
            "file_count": source_inventory["file_count"],
            "byte_size": source_inventory["byte_size"],
            "tree_sha256": source_inventory["tree_sha256"],
            "dependencies": [],
            "dependency_discovery_status": "pending_ue_asset_registry",
        },
        "acquisition": {"mode": "preimported", "status": "materialized", "generator": None},
    }
    registration = registry.register_asset(asset)
    if registration.get("status") != "registered":
        raise PreparedMapInputError(
            str(registration.get("code") or "catalog_registration_failed"),
            str(registration.get("message") or "prepared Map Catalog registration failed"),
        )

    receipt_root.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": PREPARED_MAP_REGISTRATION_SCHEMA,
        "status": "materialized_pending_ue_qualification",
        "asset_id": asset_id,
        "map_package": package,
        "map_object_path": object_path,
        "map_sha256": map_sha256,
        "source_uri": resolved_source_uri,
        "source_content_root": str(source_root),
        "source_bundle_root": str(source_bundle),
        "materialized_bundle_root": str(target_bundle),
        "materialized_map_file": str(target_map),
        "ue_project": str(project),
        "catalog": str(registry.path),
        "license": license_name,
        "license_tier": license_tier,
        "bundle_inventory": source_inventory,
        "qualification": {
            "runtime_ready": False,
            "required_schema": PREPARED_MAP_QUALIFICATION_SCHEMA,
            "required_next_action": "run_real_ue_map_smoke",
        },
    }
    write_json(receipt_root / "prepared_map_registration.json", result)
    return result


def qualify_map_input(
    registration_path: str | Path,
    qualification_path: str | Path,
    *,
    registry: AssetRegistry,
) -> dict[str, Any]:
    registration = read_json(Path(registration_path))
    qualification = read_json(Path(qualification_path))
    if registration.get("schema_version") != PREPARED_MAP_REGISTRATION_SCHEMA:
        raise PreparedMapInputError("prepared_map_registration_invalid", "registration receipt schema is invalid")
    if qualification.get("schema_version") != PREPARED_MAP_QUALIFICATION_SCHEMA:
        raise PreparedMapInputError("prepared_map_qualification_invalid", "qualification receipt schema is invalid")
    if qualification.get("status") != "pass":
        raise PreparedMapInputError("prepared_map_qualification_failed", "UE qualification did not pass")
    if canonical_map_package(str(qualification.get("requested_package") or "")) != registration["map_package"]:
        raise PreparedMapInputError("prepared_map_qualification_identity_mismatch", "qualified Map package differs from registration")
    if canonical_map_package(str(qualification.get("opened_package") or "")) != registration["map_package"]:
        raise PreparedMapInputError("prepared_map_qualification_opened_map_mismatch", "UE opened a different Map package")
    if str(qualification.get("map_sha256") or "") != registration["map_sha256"]:
        raise PreparedMapInputError("prepared_map_qualification_hash_mismatch", "qualified Map hash differs from registration")
    if int(qualification.get("loaded_actor_count") or 0) <= 0:
        raise PreparedMapInputError("prepared_map_qualification_empty", "qualified Map contains no loaded actors")
    if not registry.writable:
        raise PreparedMapInputError("catalog_not_writable", f"prepared Map qualification requires writable Catalog: {registry.path}")

    asset = registry.get_asset_by_id(str(registration["asset_id"]))
    if asset is None:
        raise PreparedMapInputError("prepared_map_catalog_asset_missing", "registered prepared Map is absent from Catalog")
    promoted = copy.deepcopy(asset)
    promoted["lifecycle_status"] = "runtime_bound"
    promoted["quality_status"] = "local_preview"
    unreal_binding = copy.deepcopy((promoted.get("backend_bindings") or {}).get("unreal") or {})
    unreal_binding["materialized"] = True
    unreal_binding["runtime_ready"] = True
    promoted.setdefault("backend_bindings", {})["unreal"] = unreal_binding
    promoted["qualification"] = copy.deepcopy(qualification)
    result = registry.register_asset(promoted)
    if result.get("status") != "registered":
        raise PreparedMapInputError(
            str(result.get("code") or "catalog_registration_failed"),
            str(result.get("message") or "qualified prepared Map could not be promoted"),
        )
    return {
        "schema_version": PREPARED_MAP_REGISTRATION_SCHEMA,
        "status": "runtime_bound_local_preview",
        "asset_id": registration["asset_id"],
        "map_package": registration["map_package"],
        "catalog": str(registry.path),
        "qualification_receipt": str(Path(qualification_path).expanduser().resolve()),
        "loaded_actor_count": int(qualification["loaded_actor_count"]),
        "runtime_ready": True,
        "reference_ready": False,
    }


def canonical_map_package(value: str) -> str:
    text = str(value or "").strip().split(":", 1)[0]
    dot = text.find(".", text.rfind("/"))
    return text[:dot] if dot >= 0 else text.rstrip("/")


def map_object_path(package: str) -> str:
    canonical = canonical_map_package(package)
    return f"{canonical}.{canonical.rsplit('/', 1)[-1]}"


def content_tree_inventory(root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    file_count = 0
    byte_size = 0
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        file_hash = sha256_file(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
        file_count += 1
        byte_size += size
    if file_count == 0:
        raise PreparedMapInputError("prepared_map_bundle_empty", f"Content bundle has no files: {root}")
    return {"file_count": file_count, "byte_size": byte_size, "tree_sha256": digest.hexdigest()}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_") or "unnamed"


def _materialize_bundle(source: Path, target: Path, *, expected: Mapping[str, Any]) -> None:
    if target.exists():
        if not target.is_dir():
            raise PreparedMapInputError("prepared_map_materialization_conflict", f"target exists and is not a directory: {target}")
        actual = content_tree_inventory(target)
        if actual["tree_sha256"] != expected["tree_sha256"]:
            raise PreparedMapInputError("prepared_map_materialization_conflict", f"target Content bundle differs: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.harness-staging-{str(expected['tree_sha256'])[:12]}"
    if staging.exists():
        raise PreparedMapInputError("prepared_map_staging_conflict", f"staging directory already exists: {staging}")
    try:
        shutil.copytree(source, staging, copy_function=shutil.copy2)
        staging.rename(target)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
