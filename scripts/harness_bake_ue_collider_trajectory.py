#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.artifact_schema import read_json, write_json
from harness.core.case_spec_v2 import case_spec_v2_from_dict
from harness.runtime.trajectory_motion import bake_trajectory_into_case


def main() -> None:
    parser = argparse.ArgumentParser(description="Bake UE object transforms into kinematic collider keyframes.")
    parser.add_argument("--trajectory", required=True)
    parser.add_argument("--base-case", required=True)
    parser.add_argument("--map", action="append", required=True, metavar="SOURCE_ID:TARGET_ID")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    mappings: dict[str, str] = {}
    for raw in args.map:
        source_id, separator, target_id = raw.partition(":")
        if not separator or not source_id or not target_id:
            raise SystemExit("--map must be SOURCE_ID:TARGET_ID")
        mappings[source_id] = target_id
    trajectory = read_json(Path(args.trajectory))
    base_case = read_json(Path(args.base_case))
    if not isinstance(trajectory, list):
        raise SystemExit("trajectory root must be a list")
    baked = bake_trajectory_into_case(base_case, trajectory, mappings, case_id=args.case_id)
    case_spec_v2_from_dict(baked)
    write_json(Path(args.output), baked)
    print(json.dumps({
        "schema_version": "harness_ue_trajectory_bake_result_v1",
        "case_id": args.case_id,
        "output": str(Path(args.output).resolve()),
        "mappings": mappings,
        "frame_count": len(trajectory),
    }, indent=2))


if __name__ == "__main__":
    main()
