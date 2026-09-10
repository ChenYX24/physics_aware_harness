#!/usr/bin/env python3
"""Prepare deterministic backend-import requests for a local Quixel collection manifest."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.assets.providers.contracts import BACKEND_IMPORT_REQUEST_SCHEMA, BackendImportRequest, stable_digest
from harness.core.artifact_schema import write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--destination-path", default="/Game/Imported/WarehouseLow")
    return parser.parse_args()


def prepare_batch(source_manifest: Path, output_root: Path, *, destination_path: str) -> dict[str, Any]:
    source = json.loads(source_manifest.read_text(encoding="utf-8"))
    if source.get("schema_version") != "harness_quixel_collection_manifest_v1":
        raise ValueError("unsupported Quixel collection manifest")
    if not re.fullmatch(r"/Game(?:/[A-Za-z0-9_]+)+", destination_path.rstrip("/")):
        raise ValueError("destination_path must be a package path under /Game")
    assets = source.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ValueError("Quixel collection manifest contains no assets")
    requests_dir = output_root / "requests"
    results_dir = output_root / "results"
    requests_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    identities: set[str] = set()
    for row in assets:
        if not isinstance(row, dict):
            raise ValueError("Quixel asset row must be an object")
        source_id = str(row.get("asset_id") or "").strip()
        source_file = row.get("local_fbx") if isinstance(row.get("local_fbx"), dict) else {}
        local_path = Path(str(source_file.get("local_path") or "")).expanduser().resolve()
        source_sha256 = str(source_file.get("sha256") or "").casefold()
        if not source_id or source_id in identities:
            raise ValueError(f"missing or duplicate Quixel asset_id: {source_id!r}")
        if not local_path.is_file() or len(source_sha256) != 64:
            raise ValueError(f"Quixel FBX is not materialized and hashed: {source_id}")
        identities.add(source_id)
        semantic_name = str(row.get("semantic_name") or row.get("name") or source_id).strip()
        desired_name = _safe_name(f"WarehouseLow_{semantic_name}_{source_id}")
        payload = {
            "schema_version": BACKEND_IMPORT_REQUEST_SCHEMA,
            "asset_id": f"quixel.warehouse_low.{source_id}",
            "target_backend": "unreal",
            "class_name": "StaticMesh",
            "source_files": [
                {
                    "role": "user_import_source",
                    "local_path": str(local_path),
                    "format": "fbx",
                    "sha256": source_sha256,
                    "byte_size": int(source_file.get("byte_size") or local_path.stat().st_size),
                    "materialized": True,
                }
            ],
            "desired_name": desired_name,
            "destination_path": destination_path.rstrip("/"),
            "source_kind": "external_site",
            "provider_id": "user_attested_quixel_collection_v1",
            "provider_version": "1",
            "importer_contract_version": "ue_static_mesh_import_v4",
        }
        authored_size = row.get("authored_size_m")
        if isinstance(authored_size, list) and len(authored_size) == 3:
            payload["expected_size_m"] = [float(value) for value in authored_size]
        digest_identity = json.loads(json.dumps(payload))
        for file_row in digest_identity["source_files"]:
            file_row.pop("local_path", None)
        digest = stable_digest(digest_identity)
        request = BackendImportRequest.from_dict(
            {**payload, "request_id": f"backend-import.{digest[:24]}", "request_digest": digest}
        ).to_dict()
        request_path = requests_dir / f"{source_id}.json"
        result_path = results_dir / f"{source_id}.json"
        write_json(request_path, request)
        items.append(
            {
                "source_asset_id": source_id,
                "request_digest": digest,
                "request_path": str(request_path.resolve()),
                "result_path": str(result_path.resolve()),
            }
        )
    batch = {
        "schema_version": "harness_backend_asset_import_batch_request_v1",
        "source_manifest": str(source_manifest.resolve()),
        "destination_path": destination_path.rstrip("/"),
        "item_count": len(items),
        "items": items,
    }
    write_json(output_root / "batch_request.json", batch)
    return batch


def _safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    if not name:
        raise ValueError("asset has no safe UE name")
    return name[:120]


def main() -> int:
    args = parse_args()
    batch = prepare_batch(
        Path(args.source_manifest).expanduser().resolve(),
        Path(args.output_root).expanduser().resolve(),
        destination_path=args.destination_path,
    )
    print(json.dumps(batch, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
