from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.workspace import workspace_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the M2.3 UE-only world-model dataset harness.")
    parser.add_argument("--mode", choices=["rgb", "data", "both"], default="both")
    parser.add_argument("--batch", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--case", required=True, help="Declarative CaseSpec JSON to replicate for the batch.")
    parser.add_argument("--out-root", default="runs/world_model_experiments")
    parser.add_argument("--parallel", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    source_name = Path(args.case).stem
    case_dir = workspace_path(None, default_relative=Path("tmp") / "generated_cases" / f"{source_name}_m2_3_seed{args.seed}_{stamp}")
    run_root = workspace_path(args.out_root, default_relative="runs/world_model_experiments")
    generate_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "harness_generate_cases.py"),
        "--case",
        args.case,
        "--count",
        str(args.batch),
        "--seed",
        str(args.seed),
        "--out",
        str(case_dir),
    ]
    batch_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "harness_run_case_batch.py"),
        str(case_dir),
        "--backend",
        "ue",
        "--output-root",
        str(run_root),
        "--timestamp",
        stamp,
        "--mode",
        args.mode,
        "--parallel",
        str(max(1, args.parallel)),
    ]
    generated = subprocess.run(generate_cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if generated.returncode != 0:
        print(json.dumps(failure("case_generation_failed", generate_cmd, generated), indent=2, ensure_ascii=False))
        return generated.returncode
    batch = subprocess.run(batch_cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    payload = {
        "schema_version": "m2_3_experiment_result.v1",
        "artifact_schema_version": "2.3",
        "source_case": args.case,
        "mode": args.mode,
        "seed": args.seed,
        "batch": args.batch,
        "ue_only": True,
        "case_dir": str(case_dir),
        "commands": {
            "generate": generate_cmd,
            "run_batch": batch_cmd,
        },
        "case_generation": json.loads(generated.stdout) if generated.stdout.strip().startswith("{") else {"stdout": generated.stdout},
        "batch": json.loads(batch.stdout) if batch.stdout.strip().startswith("{") else {"stdout": batch.stdout},
        "stderr": {
            "generate": generated.stderr,
            "run_batch": batch.stderr,
        },
    }
    report_path = run_root / f"experiment_{source_name}_{args.mode}_seed{args.seed}_{stamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({**payload, "report_path": str(report_path)}, indent=2, ensure_ascii=False))
    return batch.returncode


def failure(kind: str, command: list[str], result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return {
        "schema_version": "m2_3_experiment_result.v1",
        "status": "failed",
        "failure": kind,
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


if __name__ == "__main__":
    raise SystemExit(main())
