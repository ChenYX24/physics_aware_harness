#!/usr/bin/env python3
"""Import portable release-audit metadata and optionally link it to a local UE Content tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.assets.sqlite_catalog import default_catalog_path, infer_license_tier, initialize_catalog


SOURCE_NAME = "release_audit_metadata"


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Expected JSON object at {path}:{line_number}")
            rows.append(value)
    return rows


def normalize_content_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    windows_absolute = bool(path.parts and path.parts[0].endswith(":"))
    if not raw or str(path) == "." or path.is_absolute() or windows_absolute or ".." in path.parts:
        raise ValueError(f"Content path must be a safe relative path: {value!r}")
    return path.as_posix()


def local_content_path(content_root: Path | None, relative_path: str) -> Path | None:
    if content_root is None:
        return None
    root = content_root.resolve()
    candidate = root.joinpath(*PurePosixPath(relative_path).parts).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise ValueError(f"Content path escapes root: {relative_path}")
    return candidate


def ue_package_from_file(relative_path: str) -> str:
    return "/Game/" + str(PurePosixPath(relative_path).with_suffix(""))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_lfs_pointer(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(80).startswith(b"version https://git-lfs.github.com/spec/v1")


def build_file_index(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        relative_path = normalize_content_path(row.get("path"))
        result[relative_path] = dict(row)
    return result


def link_file(
    relative_path: str,
    *,
    role: str,
    content_root: Path | None,
    audit_files: dict[str, dict[str, Any]],
    verify_hashes: bool,
) -> dict[str, Any]:
    audit = audit_files.get(relative_path) or {}
    expected_hash = str(audit.get("sha256") or "").casefold() or None
    expected_size = audit.get("size_bytes")
    local_path = local_content_path(content_root, relative_path)
    materialized = bool(local_path and local_path.is_file())
    actual_hash: str | None = None
    hash_verified: bool | None = None
    status = "unlinked" if content_root is None else "missing"
    if materialized and local_path:
        if is_lfs_pointer(local_path):
            materialized = False
            status = "lfs_pointer"
        else:
            actual_size = local_path.stat().st_size
            if expected_size is not None and int(expected_size) != actual_size:
                materialized = False
                status = "size_mismatch"
            elif verify_hashes or not expected_hash:
                actual_hash = sha256_file(local_path)
                hash_verified = expected_hash is None or actual_hash == expected_hash
                materialized = bool(hash_verified)
                status = "verified" if materialized else "hash_mismatch"
            else:
                status = "linked_unverified"
    return {
        "role": role,
        "local_path": str(local_path) if local_path else relative_path,
        "content_relative_path": relative_path,
        "format": PurePosixPath(relative_path).suffix.casefold().lstrip("."),
        "sha256": actual_hash or expected_hash,
        "byte_size": int(expected_size) if expected_size is not None else local_path.stat().st_size if local_path and local_path.is_file() else None,
        "materialized": materialized,
        "hash_verified": hash_verified,
        "link_status": status,
    }


def convert_asset(
    row: dict[str, Any],
    *,
    content_root: Path | None,
    audit_files: dict[str, dict[str, Any]],
    verify_hashes: bool,
) -> dict[str, Any]:
    content_file = normalize_content_path(row.get("content_file"))
    primary = link_file(
        content_file,
        role="primary",
        content_root=content_root,
        audit_files=audit_files,
        verify_hashes=verify_hashes,
    )
    dependency_paths = [normalize_content_path(value) for value in row.get("dependency_files") or []]
    dependency_files = [
        link_file(
            relative_path,
            role="dependency",
            content_root=content_root,
            audit_files=audit_files,
            verify_hashes=verify_hashes,
        )
        for relative_path in dependency_paths
    ]
    dependency_records = [
        {
            "dependency_id": ue_package_from_file(file_row["content_relative_path"]),
            "package": ue_package_from_file(file_row["content_relative_path"]),
            "kind": "asset",
            "local_path": file_row["local_path"],
            "content_relative_path": file_row["content_relative_path"],
            "materialized": file_row["materialized"],
            "sha256": file_row.get("sha256"),
        }
        for file_row in dependency_files
    ]
    dependencies_ready = all(file_row["materialized"] for file_row in dependency_files)
    runtime_ready = bool(primary["materialized"] and dependencies_ready)
    quality_status = "local_preview" if runtime_ready else "discovered"
    license_name = str(row.get("license") or "")
    ue_path = str(row.get("ue_path") or "")
    class_name = str(row.get("class_name") or "")
    local_path = primary["local_path"] if primary["materialized"] else None
    aliases = sorted(
        {
            str(row.get("name") or "").strip(),
            PurePosixPath(content_file).stem,
        }
        - {""}
    )
    tags = sorted(
        {
            str(row.get("category_l1") or "").strip(),
            str(row.get("category_l2") or "").strip(),
            class_name,
        }
        - {""}
    )
    return {
        "asset_id": str(row.get("asset_id") or ""),
        "name": str(row.get("name") or PurePosixPath(content_file).stem),
        "description": f"{class_name or 'UE'} asset from portable release audit metadata",
        "category_l1": str(row.get("category_l1") or "asset"),
        "category_l2": str(row.get("category_l2") or "generic"),
        "type": class_name,
        "aliases": aliases,
        "tags": tags,
        "source_kind": str(row.get("source_kind") or "local_ue_project"),
        "source_uri": str(row.get("source_uri") or f"audit://{row.get('asset_id')}"),
        "license": license_name,
        "license_tier": infer_license_tier(license_name, quality_status),
        "quality_status": quality_status,
        "lifecycle_status": "runtime_bound" if runtime_ready else "materialized" if primary["materialized"] else "discovered",
        "materialized": bool(primary["materialized"]),
        "sha256": primary.get("sha256"),
        "byte_size": primary.get("byte_size"),
        "ue_path": ue_path,
        "content_relative_path": content_file,
        "paths": {"ue5": ue_path, "local_file": local_path},
        "files": [primary, *dependency_files],
        "ue": {
            "object_path": ue_path,
            "package_name": ue_path.split(".", 1)[0] if ue_path else ue_package_from_file(content_file),
            "class_name": class_name,
            "dependencies": [record["package"] for record in dependency_records],
        },
        "bundle": {
            "bundle_id": f"ue_bundle:{row.get('asset_id')}",
            "owner_asset": ue_path.split(".", 1)[0] if ue_path else ue_package_from_file(content_file),
            "dependencies": dependency_records,
        },
        "backend_bindings": {
            "unreal": {
                "object_path": ue_path,
                "class_name": class_name,
                "materialized": bool(primary["materialized"]),
                "runtime_ready": runtime_ready,
            }
        },
        "acquisition": {
            "mode": "preimported" if primary["materialized"] else "portable_audit_link",
            "status": "runtime_bound" if runtime_ready else "awaiting_local_content",
        },
        "release_audit": {
            "publication_eligible": bool(row.get("publication_eligible")),
            "publication_blockers": list(row.get("publication_blockers") or []),
            "redistribution": dict(row.get("redistribution") or {}),
            "content_relative_path": content_file,
            "dependency_relative_paths": dependency_paths,
        },
    }


def build_registry(
    audit_dir: str | Path,
    *,
    content_root: str | Path | None = None,
    verify_hashes: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    audit_path = Path(audit_dir)
    assets = read_jsonl(audit_path / "assets.jsonl")
    if limit is not None:
        assets = assets[:limit]
    file_index = build_file_index(read_jsonl(audit_path / "files.jsonl"))
    root = Path(content_root) if content_root else None
    converted = [
        convert_asset(row, content_root=root, audit_files=file_index, verify_hashes=verify_hashes)
        for row in assets
    ]
    return {
        "schema_version": "asset_registry.release_audit.v1",
        "source": SOURCE_NAME,
        "audit_source": str(audit_path.resolve()),
        "content_root": str(root.resolve()) if root else None,
        "asset_count": len(converted),
        "assets": converted,
    }


def import_report(registry: dict[str, Any], catalog_stats: dict[str, int]) -> dict[str, Any]:
    assets = registry["assets"]
    files = [file_row for asset in assets for file_row in asset.get("files") or []]
    return {
        "schema_version": "asset_release_audit_import_report.v1",
        "audit_source": registry.get("audit_source"),
        "content_root": registry.get("content_root"),
        "asset_count": len(assets),
        "primary_materialized_count": sum(1 for asset in assets if asset.get("materialized")),
        "runtime_ready_count": sum(
            1
            for asset in assets
            if ((asset.get("backend_bindings") or {}).get("unreal") or {}).get("runtime_ready")
        ),
        "discovered_count": sum(1 for asset in assets if asset.get("lifecycle_status") == "discovered"),
        "file_record_count": len(files),
        "linked_file_count": sum(1 for row in files if row.get("materialized")),
        "missing_file_count": sum(1 for row in files if row.get("link_status") == "missing"),
        "hash_mismatch_count": sum(1 for row in files if row.get("link_status") == "hash_mismatch"),
        "publication_eligible_count": sum(
            1 for asset in assets if (asset.get("release_audit") or {}).get("publication_eligible")
        ),
        "catalog": catalog_stats,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", required=True, help="Directory containing assets.jsonl and files.jsonl")
    parser.add_argument("--content-root", help="This machine's UE Content root; omit for metadata-only discovery import")
    parser.add_argument("--catalog-path", default=str(default_catalog_path()))
    parser.add_argument("--report-path", help="Import report path; defaults beside the SQLite Catalog")
    parser.add_argument("--skip-hash-verification", action="store_true")
    parser.add_argument("--limit", type=int, help="Optional deterministic prefix limit for smoke tests")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    registry = build_registry(
        args.audit_dir,
        content_root=args.content_root,
        verify_hashes=not args.skip_hash_verification,
        limit=args.limit,
    )
    catalog = initialize_catalog(args.catalog_path)
    stats = catalog.import_registry(registry)
    report = import_report(registry, stats)
    report_path = Path(args.report_path) if args.report_path else Path(args.catalog_path).with_name("release_audit_import_report.json")
    write_json(report_path, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
