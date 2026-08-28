from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.artifact_schema import write_json
from harness.core.case_spec import validate_case_spec
from harness.core.case_spec_v2 import validate_case_spec_v2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a batch from one declarative case contract. Named physical-process suites are intentionally unsupported."
    )
    parser.add_argument("--case", required=True, help="Source CaseSpec JSON. Physics must be declared by objects, primitives, and assertions.")
    parser.add_argument("--count", "--num-cases", dest="count", type=int, required=True)
    parser.add_argument("--seed", type=int, default=0, help="Recorded for reproducibility; no process-specific perturbation is inferred.")
    parser.add_argument("--out", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.count <= 0:
        raise ValueError("case count must be positive")
    source_path = Path(args.case)
    if not source_path.is_absolute():
        source_path = ROOT / source_path
    source = json.loads(source_path.read_text(encoding="utf-8"))
    validate(source)
    output_dir = Path(args.out)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    cases = []
    source_case_id = str((source.get("identity") or {}).get("case_id") or source.get("case_id") or "case")
    for index in range(args.count):
        case = copy.deepcopy(source)
        case_id = f"{source_case_id}__seed{args.seed}_{index:03d}"
        generation = {
            "mode": "declarative_case_replication",
            "source_case": str(source_path),
            "seed": args.seed,
            "index": index,
        }
        if case.get("schema_version") == "harness_case_spec_v2":
            case.setdefault("identity", {})["case_id"] = case_id
            case.setdefault("provenance", {})["replication"] = generation
        else:
            case["case_id"] = case_id
            case["generation"] = generation
        validate(case)
        path = output_dir / f"{case_id}.json"
        write_json(path, case)
        cases.append({"case_id": case_id, "path": path.name})
    manifest = {
        "schema_version": "harness_generated_case_manifest_v2",
        "generation_mode": "declarative_case_replication",
        "source_case": str(source_path),
        "seed": args.seed,
        "num_cases": args.count,
        "cases": cases,
    }
    write_json(output_dir / "manifest.json", manifest)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


def validate(case: dict) -> None:
    if case.get("schema_version") == "harness_case_spec_v2":
        validate_case_spec_v2(case)
    else:
        validate_case_spec(case)


if __name__ == "__main__":
    raise SystemExit(main())
