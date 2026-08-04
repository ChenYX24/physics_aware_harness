#!/usr/bin/env python3
"""Build or inspect the Asset Catalog OpenCLIP/sqlite-vec index."""

from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.assets.embedding_index import OpenCLIPEmbeddingProvider
from harness.assets.hybrid_ranking import DEFAULT_RETRIEVAL_CONFIG, load_retrieval_config
from harness.assets.sqlite_catalog import SQLiteCatalog, default_catalog_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-path", default=str(default_catalog_path()))
    parser.add_argument("--config", default=str(DEFAULT_RETRIEVAL_CONFIG))
    parser.add_argument("--model-cache", default=None, help="Override the external OpenCLIP cache directory.")
    parser.add_argument("--checkpoint-path", default=None, help="Use an explicit local OpenCLIP checkpoint.")
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--device", default=None, help="OpenCLIP device; CPU is the deterministic default.")
    parser.add_argument("--download", action="store_true", help="Allow OpenCLIP/Hugging Face downloads into model-cache.")
    parser.add_argument("--force", action="store_true", help="Re-encode unchanged assets instead of reusing vectors.")
    parser.add_argument("--skip-images", action="store_true")
    parser.add_argument("--status", action="store_true", help="Print index state without loading OpenCLIP.")
    parser.add_argument("--dry-run", action="store_true", help="Print resolved settings and asset count only.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = copy.deepcopy(load_retrieval_config(args.config))
    embedding = config["embedding"]
    if args.model_cache:
        embedding["model_cache"] = args.model_cache
    if args.checkpoint_path:
        embedding["checkpoint_path"] = args.checkpoint_path
    if args.device:
        embedding["device"] = args.device
    catalog = SQLiteCatalog(args.catalog_path, retrieval_config_path=args.config)
    if args.status:
        print(json.dumps(catalog.vector_index_status(), indent=2, ensure_ascii=False, sort_keys=True))
        return
    workspace = _workspace_root(Path(args.catalog_path))
    provider = OpenCLIPEmbeddingProvider.from_config(
        config,
        workspace=workspace,
        allow_download=args.download,
    )
    batch_size = args.batch_size or int(embedding.get("batch_size") or 32)
    if args.dry_run:
        with sqlite3.connect(args.catalog_path) as connection:
            asset_count = int(connection.execute("SELECT count(*) FROM assets").fetchone()[0])
        print(
            json.dumps(
                {
                    "catalog_path": str(Path(args.catalog_path)),
                    "asset_count": asset_count,
                    "model": provider.spec.to_dict(),
                    "model_cache": str(provider.cache_dir),
                    "download_allowed": provider.allow_download,
                    "include_images": not args.skip_images,
                    "batch_size": batch_size,
                },
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    result = catalog.rebuild_vector_index(
        provider,
        include_images=not args.skip_images,
        batch_size=batch_size,
        force=args.force,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))


def _workspace_root(catalog_path: Path) -> Path:
    if catalog_path.parent.name == "assets" and catalog_path.parent.parent.name == "catalog":
        return catalog_path.parents[2]
    return catalog_path.parent


if __name__ == "__main__":
    main()
