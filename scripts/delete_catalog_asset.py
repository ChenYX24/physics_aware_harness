#!/usr/bin/env python3
"""Delete one exact asset_id from the SQLite Asset Catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.assets.sqlite_catalog import SQLiteCatalog, default_catalog_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset_id")
    parser.add_argument("--catalog-path", default=str(default_catalog_path()))
    args = parser.parse_args()

    result = SQLiteCatalog(args.catalog_path).delete_asset(args.asset_id)
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
