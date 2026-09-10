#!/usr/bin/env python3
"""Download an explicit, reusable Poly Haven HDRI pool."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


API_ROOT = "https://api.polyhaven.com"
USER_AGENT = "PhysicsAwareHarness/0.1 (HDRI acquisition; https://polyhaven.com)"


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object from {url}")
    return value


def download(url: str, destination: Path, *, expected_md5: str) -> None:
    if destination.is_file() and file_md5(destination) == expected_md5:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as handle:
        while chunk := response.read(1024 * 1024):
            handle.write(chunk)
    actual = file_md5(temporary)
    if actual != expected_md5:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"MD5 mismatch for {destination.name}: expected {expected_md5}, got {actual}")
    temporary.replace(destination)


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def select_hdr(files: Mapping[str, Any], resolution: str) -> dict[str, Any]:
    hdri = files.get("hdri")
    level = hdri.get(resolution) if isinstance(hdri, Mapping) else None
    record = level.get("hdr") if isinstance(level, Mapping) else None
    if not isinstance(record, Mapping) or not record.get("url") or not record.get("md5"):
        raise RuntimeError(f"Poly Haven asset has no {resolution} HDR file")
    return dict(record)


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--resolution", default="2k")
    parser.add_argument("--asset", action="append", required=True, dest="asset_ids")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    destination = args.destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    metadata = request_json(f"{API_ROOT}/assets?t=hdris")
    manifest_assets: list[dict[str, Any]] = []
    for asset_id in dict.fromkeys(args.asset_ids):
        asset = metadata.get(asset_id)
        if not isinstance(asset, Mapping):
            raise RuntimeError(f"unknown Poly Haven HDRI: {asset_id}")
        files = request_json(f"{API_ROOT}/files/{asset_id}")
        selected = select_hdr(files, args.resolution)
        asset_dir = destination / asset_id
        hdr_path = asset_dir / f"{asset_id}_{args.resolution}.hdr"
        download(str(selected["url"]), hdr_path, expected_md5=str(selected["md5"]))
        record = {
            "asset_id": f"external.polyhaven.hdri.{asset_id}",
            "source_asset_id": asset_id,
            "name": str(asset.get("name") or asset_id),
            "source_uri": f"https://polyhaven.com/a/{asset_id}",
            "license": "CC0-1.0",
            "authors": sorted(str(author) for author in (asset.get("authors") or {}).keys()),
            "categories": list(asset.get("categories") or []),
            "tags": list(asset.get("tags") or []),
            "resolution": args.resolution,
            "local_path": str(hdr_path),
            "byte_size": hdr_path.stat().st_size,
            "md5": file_md5(hdr_path),
            "sha256": file_sha256(hdr_path),
            "remote_url": str(selected["url"]),
        }
        write_json(asset_dir / "asset.json", record)
        manifest_assets.append(record)
        print(json.dumps({"status": "ready", "asset_id": asset_id, "path": str(hdr_path)}), flush=True)
    manifest = {
        "schema_version": "harness_polyhaven_hdri_pool_v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "resolution": args.resolution,
        "asset_count": len(manifest_assets),
        "assets": manifest_assets,
    }
    write_json(destination / "manifest.json", manifest)
    print(json.dumps({"status": "completed", "manifest": str(destination / "manifest.json"), "asset_count": len(manifest_assets)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
