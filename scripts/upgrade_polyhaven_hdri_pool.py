#!/usr/bin/env python3
"""Upgrade every HDRI already listed in a fixed Poly Haven pool manifest."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--resolution", default="8k", choices=("4k", "8k", "16k"))
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def hashes(path: Path) -> tuple[str, str]:
    md5 = hashlib.md5()  # noqa: S324 - retained for the existing manifest contract
    sha256 = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            md5.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest()


def download(row: dict[str, Any], resolution: str) -> dict[str, Any]:
    asset_id = str(row["source_asset_id"])
    destination = Path(str(row["local_path"])).parent / f"{asset_id}_{resolution}.hdr"
    remote_url = f"https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/{resolution}/{asset_id}_{resolution}.hdr"
    if not destination.is_file() or destination.stat().st_size == 0:
        temporary = destination.with_suffix(destination.suffix + ".part")
        for attempt in range(3):
            try:
                request = urllib.request.Request(remote_url, headers={"User-Agent": "physics-aware-harness/1"})
                with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as target:
                    while chunk := response.read(4 * 1024 * 1024):
                        target.write(chunk)
                temporary.replace(destination)
                break
            except Exception:
                temporary.unlink(missing_ok=True)
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
    md5, sha256 = hashes(destination)
    updated = dict(row)
    updated.update(
        {
            "resolution": resolution,
            "local_path": str(destination.resolve()),
            "byte_size": destination.stat().st_size,
            "md5": md5,
            "sha256": sha256,
            "remote_url": remote_url,
        }
    )
    print(f"{asset_id}\t{destination.stat().st_size}\t{sha256}", flush=True)
    return updated


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = manifest.get("assets") or []
    if manifest.get("schema_version") != "harness_polyhaven_hdri_pool_v1" or not rows:
        raise ValueError("unsupported or empty HDRI pool manifest")
    workers = max(1, min(int(args.workers), 8))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        updated_rows = list(executor.map(lambda row: download(row, args.resolution), rows))
    updated_manifest = dict(manifest)
    updated_manifest.update(
        {
            "asset_count": len(updated_rows),
            "resolution": args.resolution,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "assets": updated_rows,
        }
    )
    for row in updated_rows:
        write_json(Path(row["local_path"]).parent / "asset.json", row)
    write_json(manifest_path, updated_manifest)
    print(json.dumps({"status": "pass", "asset_count": len(updated_rows), "resolution": args.resolution}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
