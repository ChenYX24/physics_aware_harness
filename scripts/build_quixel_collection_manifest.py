#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


SCHEMA = "harness_quixel_collection_manifest_v1"
API_ROOT = "https://megascans.se/v1/assets"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recover Quixel metadata for a local FBX collection.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=8)
    return parser.parse_args()


def build_manifest(root: str | Path, *, workers: int = 8) -> dict[str, Any]:
    source_root = Path(root).expanduser().resolve()
    sources = sorted(source_root.rglob("*.fbx"))
    if not sources:
        raise ValueError(f"Quixel collection contains no FBX files: {source_root}")
    if workers <= 0:
        raise ValueError("workers must be positive")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        metadata_rows = list(executor.map(fetch_asset_metadata, (source.stem for source in sources)))
    assets = [local_asset_record(source_root, source, metadata) for source, metadata in zip(sources, metadata_rows)]
    failures = [row["asset_id"] for row in assets if row["metadata_status"] != "resolved"]
    return {
        "schema_version": SCHEMA,
        "source_root": str(source_root),
        "source_kind": "quixel_megascans_local_fbx_collection",
        "asset_count": len(assets),
        "metadata_resolved_count": len(assets) - len(failures),
        "metadata_failed_count": len(failures),
        "metadata_failed_asset_ids": failures,
        "license": "research_use_user_attested_nonredistributable",
        "entitlement_evidence": "receipts/asset_provenance_user_attestation_20260826.md",
        "assets": assets,
    }


def fetch_asset_metadata(asset_id: str) -> dict[str, Any]:
    endpoint = f"{API_ROOT}/{asset_id}"
    request = Request(endpoint, headers={"User-Agent": "PhysicsAwareHarness/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or not str(payload.get("name") or "").strip():
            raise ValueError("metadata response has no asset name")
        return {"status": "resolved", "endpoint": endpoint, "payload": payload}
    except Exception as exc:
        return {"status": "failed", "endpoint": endpoint, "error": f"{type(exc).__name__}: {exc}"}


def local_asset_record(root: Path, source: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    asset_id = source.stem
    folder = source.parent
    textures = sorted(path for path in folder.iterdir() if path.is_file() and path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".exr"})
    payload = metadata.get("payload") if isinstance(metadata.get("payload"), dict) else {}
    dimensions_m = physical_dimensions(payload)
    preview = preview_uri(payload)
    return {
        "asset_id": asset_id,
        "name": str(payload.get("name") or asset_id),
        "semantic_name": str((payload.get("semanticTags") or {}).get("name") or payload.get("name") or asset_id),
        "tags": sorted({str(value) for value in payload.get("tags") or [] if str(value).strip()}),
        "categories": [str(value) for value in payload.get("categories") or []],
        "pack": dict(payload.get("pack") or {}),
        "mesh_topology": str((payload.get("semanticTags") or {}).get("3d_mesh") or ""),
        "authored_size_m": dimensions_m,
        "local_fbx": file_record(source),
        "local_textures": [file_record(path) for path in textures],
        "metadata_status": str(metadata["status"]),
        "metadata_endpoint": str(metadata["endpoint"]),
        "metadata_error": metadata.get("error"),
        "source_uri": f"https://quixel.com/megascans/home?assetId={asset_id}",
        "preview_uri": preview,
    }


def physical_dimensions(payload: dict[str, Any]) -> list[float] | None:
    values: dict[str, float] = {}
    for row in payload.get("meta") or []:
        if not isinstance(row, dict) or row.get("key") not in {"length", "width", "height"}:
            continue
        match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*m\s*", str(row.get("value") or ""))
        if match:
            values[str(row["key"])] = float(match.group(1))
    if set(values) != {"length", "width", "height"} or any(value <= 0.0 for value in values.values()):
        return None
    return [values["length"], values["width"], values["height"]]


def preview_uri(payload: dict[str, Any]) -> str | None:
    rows = (payload.get("previews") or {}).get("images") or []
    preferred = next(
        (
            row
            for row in rows
            if isinstance(row, dict) and "thumb" in {str(value) for value in row.get("tags") or []} and not str(row.get("uri") or "").startswith("data:")
        ),
        None,
    )
    return str(preferred.get("uri")) if preferred else None


def file_record(path: Path) -> dict[str, Any]:
    return {
        "local_path": str(path.resolve()),
        "sha256": sha256_file(path),
        "byte_size": path.stat().st_size,
        "format": path.suffix.casefold().lstrip("."),
        "materialized": True,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    manifest = build_manifest(args.root, workers=args.workers)
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: manifest[key] for key in ("schema_version", "asset_count", "metadata_resolved_count", "metadata_failed_count")}, indent=2))
    return 0 if manifest["metadata_failed_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
