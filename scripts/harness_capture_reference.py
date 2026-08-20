from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.artifact_schema import write_json
from harness.core.external_reference import capture_external_reference
from harness.core.workspace import workspace_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture a cited HTTPS source with a hash-stamped lineage record.")
    parser.add_argument("url")
    parser.add_argument("--name", required=True, help="Portable output filename under workspace/references.")
    parser.add_argument("--usage-note", required=True)
    parser.add_argument("--license-note", default="unverified")
    parser.add_argument("--max-bytes", type=int, default=20 * 1024 * 1024)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.name) or args.name in {".", ".."}:
        raise SystemExit("--name must be a portable filename")
    output = workspace_root() / "references" / args.name
    record = capture_external_reference(
        args.url,
        output,
        usage_note=args.usage_note,
        license_note=args.license_note,
        max_bytes=args.max_bytes,
    )
    manifest = output.with_suffix(output.suffix + ".reference.json")
    write_json(manifest, record)
    print(json.dumps({"artifact": str(output), "manifest": str(manifest), "sha256": record["sha256"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
