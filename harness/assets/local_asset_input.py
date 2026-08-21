from __future__ import annotations

import copy
import hashlib
import math
import mimetypes
import re
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping
from urllib.parse import quote

from harness.assets.asset_registry import AssetRegistry
from harness.assets.asset_resolver import asset_quality_gate
from harness.assets.providers.backend_importer import BackendImporterAdapter, validate_import_result
from harness.assets.providers.contracts import (
    BACKEND_IMPORT_REQUEST_SCHEMA,
    BackendImportRequest,
    stable_digest,
)
from harness.core.artifact_schema import write_json


LOCAL_ASSET_REGISTRATION_SCHEMA = "harness_local_asset_registration_v1"
SUPPORTED_ASSET_SUFFIXES = {".fbx", ".obj"}
SUPPORTED_ARCHIVE_SUFFIXES = {".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2"}
MAX_ARCHIVE_ENTRIES = 4096
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024


class LocalAssetRegistrationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def register_local_asset_input(
    source_path: str | Path,
    *,
    workspace: str | Path,
    registration_root: str | Path,
    registry: AssetRegistry,
    importer: BackendImporterAdapter,
    before_import: Callable[[BackendImportRequest], None] | None = None,
    license_name: str = "All Rights Reserved",
    license_tier: str = "local_preview",
) -> dict[str, Any]:
    source = Path(source_path).expanduser().resolve()
    workspace_root = Path(workspace).expanduser().resolve()
    root = Path(registration_root).expanduser().resolve()
    if not source.is_file():
        raise LocalAssetRegistrationError("local_asset_input_missing", f"local asset input is not a file: {source}")
    if license_tier not in {"local_preview", "reference"}:
        raise LocalAssetRegistrationError("local_asset_license_tier_invalid", "license tier must be local_preview or reference")
    source_sha256 = sha256_file(source)
    source_uri = f"local-input://sha256/{source_sha256}/{quote(source.name)}"
    stem = slug(source.name.split(".", 1)[0])
    asset_id = f"local_input.{stem}.{source_sha256[:16]}"
    existing = registry.get_asset_by_id(asset_id)
    if _existing_registration_ready(existing, source_uri=source_uri, source_sha256=source_sha256):
        existing_path = Path(str(existing.get("local_path") or "")).expanduser().resolve()
        return registration_result(
            status="registered",
            asset=existing,
            source_path=existing_path,
            source_uri=source_uri,
            source_sha256=source_sha256,
            source_byte_size=existing_path.stat().st_size,
            importer_invoked=False,
            registration_root=root,
        )
    if not registry.writable:
        raise LocalAssetRegistrationError("catalog_not_writable", f"local asset registration requires writable Catalog: {registry.path}")
    try:
        root.relative_to(workspace_root)
    except ValueError as exc:
        raise LocalAssetRegistrationError(
            "local_asset_registration_outside_workspace",
            "local asset registration output must be inside the Harness workspace",
        ) from exc
    root.mkdir(parents=True, exist_ok=True)
    materialized_input = _materialize_source(source, root / "input")
    primary = _select_primary_import_file(materialized_input, root / "extracted")
    primary_sha256 = sha256_file(primary)
    request = _backend_import_request(
        asset_id=asset_id,
        source=primary,
        source_sha256=primary_sha256,
    )
    if before_import is not None:
        before_import(request)
    import_result = importer.import_asset(request, work_dir=root / "import", workspace=workspace_root)
    if import_result.data.get("status") != "fulfilled":
        failure = import_result.data.get("failure") if isinstance(import_result.data.get("failure"), Mapping) else {}
        raise LocalAssetRegistrationError(
            str(failure.get("code") or "backend_import_failed"),
            str(failure.get("message") or "backend importer did not fulfill the local asset input"),
        )
    try:
        validate_import_result(request, import_result, workspace=workspace_root)
    except ValueError as exc:
        raise LocalAssetRegistrationError("backend_import_result_invalid", str(exc)) from exc

    asset = _catalog_asset(
        asset_id=asset_id,
        source=source,
        materialized_input=materialized_input,
        primary=primary,
        source_uri=source_uri,
        source_sha256=source_sha256,
        primary_sha256=primary_sha256,
        import_result=import_result.data,
        license_name=license_name,
        license_tier=license_tier,
    )
    registration = registry.register_asset(asset)
    if registration.get("status") != "registered":
        raise LocalAssetRegistrationError(
            str(registration.get("code") or "catalog_registration_failed"),
            str(registration.get("message") or "local asset Catalog registration failed"),
        )
    registered = registry.get_asset_by_id(asset_id)
    if registered is None:
        raise LocalAssetRegistrationError("catalog_registration_failed", "registered local asset cannot be read back")
    quality = asset_quality_gate(
        registered,
        physics_critical=True,
        allow_local_preview=license_tier == "local_preview",
    )
    if not str(quality.get("status") or "").startswith("pass"):
        raise LocalAssetRegistrationError(
            "asset_qualification_failed",
            f"local asset failed runtime qualification: {quality.get('failure_codes')}",
        )
    runtime_bound = copy.deepcopy(registered)
    runtime_bound["lifecycle_status"] = "runtime_bound"
    runtime_bound["qualification"] = copy.deepcopy(quality)
    final_registration = registry.register_asset(runtime_bound)
    final_asset = registry.get_asset_by_id(asset_id)
    if (
        final_registration.get("status") != "registered"
        or final_asset is None
        or final_asset.get("lifecycle_status") != "runtime_bound"
    ):
        raise LocalAssetRegistrationError(
            "runtime_binding_registration_failed",
            "qualified local asset could not be persisted as runtime_bound",
        )
    result = registration_result(
        status="registered",
        asset=final_asset,
        source_path=materialized_input,
        source_uri=source_uri,
        source_sha256=source_sha256,
        source_byte_size=materialized_input.stat().st_size,
        importer_invoked=True,
        registration_root=root,
    )
    write_json(root / "local_asset_registration.json", result)
    return result


def registration_result(
    *,
    status: str,
    asset: Mapping[str, Any],
    source_path: Path,
    source_uri: str,
    source_sha256: str,
    source_byte_size: int,
    importer_invoked: bool,
    registration_root: Path,
) -> dict[str, Any]:
    return {
        "schema_version": LOCAL_ASSET_REGISTRATION_SCHEMA,
        "status": status,
        "asset_id": str(asset.get("asset_id") or ""),
        "source_uri": source_uri,
        "source_sha256": source_sha256,
        "catalog_sha256": str(asset.get("sha256") or ""),
        "local_path": str(source_path),
        "byte_size": int(source_byte_size),
        "ue_path": str(asset.get("ue_path") or ""),
        "class_name": str(asset.get("class_name") or ""),
        "license": str(asset.get("license") or ""),
        "license_tier": str(asset.get("license_tier") or ""),
        "qualification": copy.deepcopy(asset.get("qualification") or {}),
        "importer_invoked": bool(importer_invoked),
        "registration_root": str(registration_root),
        "case_spec_reference": {
            "route": "local_catalog",
            "requirement": "required",
            "origin": "user_explicit",
            "source_uri_hint": source_uri,
            "reference_inputs": [],
            "fallback_order": [],
        },
    }


def provider_manifest_input(result: Mapping[str, Any]) -> dict[str, Any]:
    if result.get("schema_version") != LOCAL_ASSET_REGISTRATION_SCHEMA or result.get("status") != "registered":
        raise ValueError("local asset registration result is not fulfilled")
    source_sha256 = str(result.get("source_sha256") or "")
    return {
        "input_id": f"local_asset_{source_sha256[:24]}",
        "kind": "asset_3d",
        "local_path": str(result["local_path"]),
        "mime_type": mimetypes.guess_type(str(result["local_path"]))[0] or "application/octet-stream",
        "sha256": source_sha256,
        "byte_size": int(result["byte_size"]),
        "authorizations": {"planning_llm_upload": False, "meshy_upload": False},
        "asset_id": str(result["asset_id"]),
        "source_uri": str(result["source_uri"]),
        "catalog_sha256": str(result["catalog_sha256"]),
    }


def _existing_registration_ready(
    asset: Mapping[str, Any] | None,
    *,
    source_uri: str,
    source_sha256: str,
) -> bool:
    if not isinstance(asset, Mapping):
        return False
    qualification = asset.get("qualification") if isinstance(asset.get("qualification"), Mapping) else {}
    provenance = asset.get("provenance") if isinstance(asset.get("provenance"), Mapping) else {}
    local_path = Path(str(asset.get("local_path") or "")).expanduser()
    return bool(
        asset.get("source_uri") == source_uri
        and provenance.get("source_input_sha256") == source_sha256
        and asset.get("lifecycle_status") == "runtime_bound"
        and str(qualification.get("status") or "").startswith("pass")
        and isinstance(asset.get("geometry_analysis"), Mapping)
        and local_path.is_file()
        and sha256_file(local_path) == source_sha256
    )


def _materialize_source(source: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / source.name
    if target.is_file():
        if sha256_file(target) != sha256_file(source):
            raise LocalAssetRegistrationError("local_asset_materialization_conflict", f"materialized input differs: {target}")
        return target
    shutil.copy2(source, target)
    if sha256_file(target) != sha256_file(source):
        raise LocalAssetRegistrationError("local_asset_hash_mismatch", "local asset changed while being materialized")
    return target


def _select_primary_import_file(materialized: Path, extraction_root: Path) -> Path:
    suffix = _compound_suffix(materialized)
    if materialized.suffix.casefold() in SUPPORTED_ASSET_SUFFIXES:
        return materialized
    if suffix not in SUPPORTED_ARCHIVE_SUFFIXES:
        raise LocalAssetRegistrationError(
            "local_asset_format_unsupported",
            "local asset input must be an FBX, OBJ, ZIP, or TAR archive",
        )
    extraction_root.mkdir(parents=True, exist_ok=True)
    if suffix == ".zip":
        _extract_zip(materialized, extraction_root)
    else:
        _extract_tar(materialized, extraction_root)
    candidates = sorted(
        path
        for path in extraction_root.rglob("*")
        if path.is_file() and path.suffix.casefold() in SUPPORTED_ASSET_SUFFIXES
    )
    if len(candidates) != 1:
        raise LocalAssetRegistrationError(
            "local_asset_archive_primary_ambiguous",
            f"local asset archive must contain exactly one FBX or OBJ; found {len(candidates)}",
        )
    return candidates[0]


def _extract_zip(source: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            _validate_archive_count_and_size(len(entries), sum(int(item.file_size) for item in entries))
            for item in entries:
                relative = _safe_archive_path(item.filename)
                mode = (item.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise LocalAssetRegistrationError("local_asset_archive_unsafe", "archive symlinks are not allowed")
                target = destination / relative
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source_stream, target.open("wb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream)
    except (OSError, zipfile.BadZipFile) as exc:
        raise LocalAssetRegistrationError("local_asset_archive_invalid", str(exc)) from exc


def _extract_tar(source: Path, destination: Path) -> None:
    try:
        with tarfile.open(source, mode="r:*") as archive:
            entries = archive.getmembers()
            _validate_archive_count_and_size(len(entries), sum(int(item.size) for item in entries if item.isfile()))
            for item in entries:
                relative = _safe_archive_path(item.name)
                if not (item.isdir() or item.isfile()):
                    raise LocalAssetRegistrationError(
                        "local_asset_archive_unsafe",
                        "archive links and special files are not allowed",
                    )
                target = destination / relative
                if item.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                source_stream = archive.extractfile(item)
                if source_stream is None:
                    raise LocalAssetRegistrationError("local_asset_archive_invalid", f"cannot read archive member: {item.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source_stream, target.open("wb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream)
    except (OSError, tarfile.TarError) as exc:
        raise LocalAssetRegistrationError("local_asset_archive_invalid", str(exc)) from exc


def _safe_archive_path(value: str) -> Path:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise LocalAssetRegistrationError("local_asset_archive_unsafe", f"unsafe archive member path: {value}")
    return Path(*path.parts)


def _validate_archive_count_and_size(count: int, byte_size: int) -> None:
    if count > MAX_ARCHIVE_ENTRIES or byte_size > MAX_ARCHIVE_BYTES:
        raise LocalAssetRegistrationError("local_asset_archive_too_large", "local asset archive exceeds extraction limits")


def _backend_import_request(*, asset_id: str, source: Path, source_sha256: str) -> BackendImportRequest:
    payload = {
        "schema_version": BACKEND_IMPORT_REQUEST_SCHEMA,
        "asset_id": asset_id,
        "target_backend": "unreal",
        "class_name": "StaticMesh",
        "source_files": [
            {
                "role": "user_import_source",
                "local_path": str(source),
                "format": source.suffix.casefold().lstrip("."),
                "sha256": source_sha256,
                "byte_size": source.stat().st_size,
                "materialized": True,
            }
        ],
        "desired_name": asset_id.replace(".", "_"),
        "source_kind": "local_input",
        "provider_id": "local_asset_input_v1",
        "provider_version": "1",
        "importer_contract_version": "ue_static_mesh_import_v4",
    }
    digest_identity = copy.deepcopy(payload)
    for row in digest_identity["source_files"]:
        row.pop("local_path", None)
    digest = stable_digest(digest_identity)
    return BackendImportRequest.from_dict(
        {**payload, "request_id": f"backend-import.{digest[:24]}", "request_digest": digest}
    )


def _catalog_asset(
    *,
    asset_id: str,
    source: Path,
    materialized_input: Path,
    primary: Path,
    source_uri: str,
    source_sha256: str,
    primary_sha256: str,
    import_result: Mapping[str, Any],
    license_name: str,
    license_tier: str,
) -> dict[str, Any]:
    imported_files = [dict(row) for row in import_result.get("files") or []]
    dependencies = [dict(row) for row in import_result.get("dependencies") or []]
    import_validation = import_result.get("import_validation") if isinstance(import_result.get("import_validation"), Mapping) else {}
    geometry_analysis = (
        copy.deepcopy(dict(import_validation["geometry_analysis"]))
        if isinstance(import_validation.get("geometry_analysis"), Mapping)
        else None
    )
    actual_size_cm = import_validation.get("actual_size_cm")
    size = (
        [float(value) / 100.0 for value in actual_size_cm]
        if isinstance(actual_size_cm, list)
        and len(actual_size_cm) == 3
        and all(isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) > 0 for value in actual_size_cm)
        else None
    )
    if size is None:
        raise LocalAssetRegistrationError(
            "asset_qualification_failed",
            "backend importer did not report finite positive imported bounds",
        )
    volume_m3 = math.prod(size)
    collision: dict[str, Any] = {"present": True, "kind": "simple_convex"}
    portable_collision = import_result.get("portable_collision_artifact")
    if isinstance(portable_collision, Mapping):
        collision["portable_mesh"] = copy.deepcopy(dict(portable_collision))
    source_record = {
        "role": "source_input",
        "local_path": str(materialized_input),
        "format": _compound_suffix(materialized_input).lstrip("."),
        "sha256": source_sha256,
        "byte_size": materialized_input.stat().st_size,
        "materialized": True,
    }
    primary_record = {
        "role": "primary_import_source",
        "local_path": str(primary),
        "format": primary.suffix.casefold().lstrip("."),
        "sha256": primary_sha256,
        "byte_size": primary.stat().st_size,
        "materialized": True,
    }
    return {
        "asset_id": asset_id,
        "name": source.stem,
        "semantic_name": source.stem.replace("_", " ").replace("-", " "),
        "description": f"User-provided local 3D asset: {source.name}",
        "aliases": [source.name, source.stem, asset_id],
        "tags": ["local_input", source.stem],
        "category": "user_asset",
        "category_l1": "user_asset",
        "type": "StaticMesh",
        "asset_kind": "StaticMesh",
        "source_kind": "local_input",
        "source_uri": source_uri,
        "author": "User provided",
        "license": license_name,
        "license_tier": license_tier,
        "redistribution": {},
        "quality_status": "approved",
        "lifecycle_status": "registered",
        "materialized": True,
        "ue_path": str(import_result["object_path"]),
        "class_name": str(import_result["class_name"]),
        "local_path": str(materialized_input),
        "sha256": source_sha256,
        "byte_size": materialized_input.stat().st_size,
        "bbox_size_m": size,
        "authored_size_m": size,
        "geometry_analysis": geometry_analysis,
        "preserve_authored_scale": True,
        "collider": "box",
        "collision_profile": "PhysicsActor",
        "mass_kg": max(volume_m3 * 1000.0, 0.001),
        "material": {"static_friction": 0.5, "dynamic_friction": 0.4, "restitution": 0.1},
        "collision": collision,
        "files": [source_record, primary_record, *imported_files, *([dict(portable_collision)] if isinstance(portable_collision, Mapping) else [])],
        "ue": {
            "object_path": str(import_result["object_path"]),
            "class_name": str(import_result["class_name"]),
            "dependencies": [str(row.get("package") or row.get("dependency_id")) for row in dependencies],
        },
        "bundle": {"dependencies": dependencies},
        "backend_bindings": {
            "unreal": {
                "backend": "unreal",
                "object_path": str(import_result["object_path"]),
                "class_name": str(import_result["class_name"]),
                "materialized": True,
                "runtime_ready": True,
                "files": imported_files,
                "dependencies": dependencies,
            }
        },
        "provenance": {
            "provider_id": "local_asset_input_v1",
            "provider_version": "1",
            "source_input_sha256": source_sha256,
            "primary_import_sha256": primary_sha256,
            "source_filename": source.name,
        },
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", str(value)).strip("_").casefold()
    return normalized or "asset"


def _compound_suffix(path: Path) -> str:
    name = path.name.casefold()
    for suffix in sorted(SUPPORTED_ARCHIVE_SUFFIXES, key=len, reverse=True):
        if name.endswith(suffix):
            return suffix
    return path.suffix.casefold()
