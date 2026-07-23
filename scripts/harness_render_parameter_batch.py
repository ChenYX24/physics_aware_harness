from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.artifact_manager import safe_filename
from harness.core.artifact_schema import read_json, write_json
from harness.core.case_spec import validate_case_spec
from harness.core.workspace import WORKSPACE_ENV, case_output_root


SCHEMA_VERSION = "harness_parameter_batch_v1"
PASSES = {"rgb", "depth", "segmentation"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate an HTML-exported parameter batch and render each embedded "
            "CaseSpec with its selected cameras and modalities."
        )
    )
    parser.add_argument("batch", help="harness_parameter_batch_v1 JSON")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--execute", action="store_true", help="Run UE/fallback renders sequentially.")
    action.add_argument("--dry-run", action="store_true", help="Validate and print commands only (default).")
    parser.add_argument("--workspace", help="Absolute SIM_HARNESS_WORKSPACE override.")
    parser.add_argument("--backend", choices=["ue", "fallback"], default="ue")
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Attempt later variants when one render command fails.",
    )
    return parser


def load_batch(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"batch schema_version must be {SCHEMA_VERSION}")
    batch_id = payload.get("batch_id")
    if not isinstance(batch_id, str) or safe_filename(batch_id) != batch_id:
        raise ValueError("batch_id must be a safe filename")
    route = payload.get("case_route")
    route_parts = Path(route).parts if isinstance(route, str) else ()
    if (
        len(route_parts) != 3
        or any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", part) for part in route_parts)
        or not re.fullmatch(r"v\d+_[a-z0-9][a-z0-9_-]*", route_parts[2])
    ):
        raise ValueError("case_route must be physics/scenario/vNNN_description")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("entries must be a non-empty list")
    seen_ids: set[str] = set()
    seen_cases: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("every batch entry must be an object")
        entry_id = entry.get("id")
        if (
            not isinstance(entry_id, str)
            or safe_filename(entry_id) != entry_id
            or entry_id in seen_ids
        ):
            raise ValueError(f"entry id is invalid or duplicate: {entry_id!r}")
        seen_ids.add(entry_id)
        case_spec = entry.get("case_spec")
        if not isinstance(case_spec, dict):
            raise ValueError(f"entry {entry_id} case_spec must be an object")
        validate_case_spec(case_spec)
        case_id = str(case_spec["case_id"])
        if case_id in seen_cases:
            raise ValueError(f"embedded case_id is duplicate: {case_id}")
        seen_cases.add(case_id)
        render = entry.get("render")
        if not isinstance(render, dict):
            raise ValueError(f"entry {entry_id} render must be an object")
        views = render.get("views")
        passes = render.get("passes")
        if (
            not isinstance(views, list)
            or not views
            or len(set(views)) != len(views)
            or any(not isinstance(view, str) or safe_filename(view) != view for view in views)
        ):
            raise ValueError(f"entry {entry_id} views must be unique safe camera ids")
        if (
            not isinstance(passes, list)
            or not passes
            or len(set(passes)) != len(passes)
            or any(item not in PASSES for item in passes)
        ):
            raise ValueError(f"entry {entry_id} passes must be unique rgb/depth/segmentation values")
    return payload


def render_mode(passes: list[str]) -> str:
    if passes == ["rgb"]:
        return "rgb"
    return "both" if "rgb" in passes else "data"


def command_for(
    entry: dict[str, Any],
    *,
    case_path: Path,
    route: str,
    backend: str,
) -> list[str]:
    render = entry["render"]
    return [
        sys.executable,
        str(ROOT / "scripts" / "harness_run_case.py"),
        str(case_path),
        "--backend",
        backend,
        "--case-route",
        route,
        "--views",
        ",".join(render["views"]),
        "--render-passes",
        ",".join(render["passes"]),
        "--mode",
        render_mode(render["passes"]),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    batch_path = Path(args.batch).expanduser().resolve(strict=True)
    try:
        batch = load_batch(batch_path)
        workspace = (
            Path(args.workspace).expanduser().resolve(strict=False)
            if args.workspace
            else None
        )
        if args.execute:
            input_root = (
                case_output_root(batch["case_route"], workspace)
                / "inputs"
                / "parameter_batches"
                / batch["batch_id"]
            )
        else:
            input_root = (
                Path("<workspace>")
                / "cases"
                / Path(batch["case_route"])
                / "inputs"
                / "parameter_batches"
                / batch["batch_id"]
            )
        commands = []
        case_paths = []
        for entry in batch["entries"]:
            case_path = input_root / f"{safe_filename(entry['case_spec']['case_id'])}.json"
            case_paths.append(case_path)
            commands.append(
                command_for(
                    entry,
                    case_path=case_path,
                    route=batch["case_route"],
                    backend=args.backend,
                )
            )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if not args.execute:
        print(
            json.dumps(
                {
                    "schema_version": "harness_parameter_batch_preview_v1",
                    "batch": str(batch_path),
                    "entry_count": len(commands),
                    "commands": [shlex.join(command) for command in commands],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    env = os.environ.copy()
    if workspace is not None:
        env[WORKSPACE_ENV] = str(workspace)
    results = []
    write_json(input_root / "batch_manifest.json", batch)
    for entry, case_path, command in zip(batch["entries"], case_paths, commands):
        write_json(case_path, entry["case_spec"])
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            stdout=sys.stderr,
            stderr=sys.stderr,
            check=False,
        )
        results.append(
            {
                "id": entry["id"],
                "case_spec": str(case_path),
                "returncode": completed.returncode,
            }
        )
        if completed.returncode != 0 and not args.continue_on_error:
            break
    summary = {
        "schema_version": "harness_parameter_batch_run_v1",
        "batch": str(batch_path),
        "input_root": str(input_root),
        "requested_count": len(commands),
        "completed_count": len(results),
        "failed_count": sum(row["returncode"] != 0 for row in results),
        "results": results,
    }
    write_json(input_root / "batch_run.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if len(results) == len(commands) and not summary["failed_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
