from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
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
QUEUE_SCHEMA_VERSION = "harness_parameter_batch_queue_v1"
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
    action.add_argument(
        "--prepare",
        action="store_true",
        help="Materialize CaseSpecs and a persistent queue without rendering.",
    )
    action.add_argument("--dry-run", action="store_true", help="Validate and print commands only (default).")
    parser.add_argument("--workspace", help="Absolute SIM_HARNESS_WORKSPACE override.")
    parser.add_argument("--backend", choices=["ue", "fallback"], default="ue")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="With --execute, append a new attempt for render/validation failures.",
    )
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
        resolution = render.get("resolution")
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
        if (
            not isinstance(resolution, dict)
            or not isinstance(resolution.get("width"), int)
            or not isinstance(resolution.get("height"), int)
            or not 320 <= resolution["width"] <= 7680
            or not 180 <= resolution["height"] <= 4320
        ):
            raise ValueError(
                f"entry {entry_id} resolution must define integer width/height within 320x180 and 7680x4320"
            )
    return payload


def render_mode(passes: list[str]) -> str:
    if passes == ["rgb"]:
        return "rgb"
    return "both" if "rgb" in passes else "data"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command_for(
    entry: dict[str, Any],
    *,
    case_path: Path,
    output_root: Path,
    backend: str,
) -> list[str]:
    render = entry["render"]
    return [
        sys.executable,
        str(ROOT / "scripts" / "harness_run_case.py"),
        str(case_path),
        "--backend",
        backend,
        "--output-root",
        str(output_root),
        "--views",
        ",".join(render["views"]),
        "--render-passes",
        ",".join(render["passes"]),
        "--mode",
        render_mode(render["passes"]),
        "--width",
        str(render["resolution"]["width"]),
        "--height",
        str(render["resolution"]["height"]),
    ]


def queue_roots(
    batch: dict[str, Any],
    workspace: Path | None,
) -> tuple[Path, Path]:
    route_root = case_output_root(batch["case_route"], workspace)
    input_root = (
        route_root
        / "inputs"
        / "parameter_batches"
        / batch["batch_id"]
    )
    run_root = route_root / "parameter_batches" / batch["batch_id"]
    return input_root, run_root


def attempt_paths(
    entry: dict[str, Any],
    *,
    input_root: Path,
    run_root: Path,
    attempt_number: int,
) -> tuple[Path, Path]:
    attempt_id = f"attempt_{attempt_number:02d}"
    case_path = (
        input_root
        / "entries"
        / entry["id"]
        / attempt_id
        / f"{safe_filename(entry['case_spec']['case_id'])}.json"
    )
    output_root = run_root / entry["id"] / attempt_id
    return case_path, output_root


def new_attempt(
    entry: dict[str, Any],
    *,
    input_root: Path,
    run_root: Path,
    backend: str,
    attempt_number: int,
) -> dict[str, Any]:
    case_path, output_root = attempt_paths(
        entry,
        input_root=input_root,
        run_root=run_root,
        attempt_number=attempt_number,
    )
    write_json(case_path, entry["case_spec"])
    command = command_for(
        entry,
        case_path=case_path,
        output_root=output_root,
        backend=backend,
    )
    timestamp = now_utc()
    return {
        "attempt": attempt_number,
        "created_at": timestamp,
        "file": {
            "status": "generated",
            "path": str(case_path),
            "sha256": file_sha256(case_path),
            "updated_at": timestamp,
        },
        "render": {
            "status": "pending",
            "command": shlex.join(command),
            "output_root": str(output_root),
            "returncode": None,
            "run_dir": None,
            "updated_at": timestamp,
        },
        "validation": {
            "status": "blocked",
            "command": None,
            "returncode": None,
            "updated_at": timestamp,
        },
    }


def build_queue(
    batch: dict[str, Any],
    *,
    input_root: Path,
    run_root: Path,
    backend: str,
) -> dict[str, Any]:
    timestamp = now_utc()
    return {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "batch_id": batch["batch_id"],
        "batch_sha256": json_sha256(batch),
        "case_route": batch["case_route"],
        "backend": backend,
        "created_at": timestamp,
        "updated_at": timestamp,
        "entries": [
            {
                "id": entry["id"],
                "case_id": entry["case_spec"]["case_id"],
                "status": "pending_render",
                "regeneration_count": 0,
                "attempts": [
                    new_attempt(
                        entry,
                        input_root=input_root,
                        run_root=run_root,
                        backend=backend,
                        attempt_number=1,
                    )
                ],
            }
            for entry in batch["entries"]
        ],
    }


def load_or_create_queue(
    batch: dict[str, Any],
    *,
    input_root: Path,
    run_root: Path,
    backend: str,
) -> tuple[dict[str, Any], Path]:
    queue_path = input_root / "batch_queue.json"
    if queue_path.is_file():
        queue = read_json(queue_path)
        if (
            not isinstance(queue, dict)
            or queue.get("schema_version") != QUEUE_SCHEMA_VERSION
            or queue.get("batch_sha256") != json_sha256(batch)
            or queue.get("backend") != backend
        ):
            raise ValueError(
                f"existing queue does not match this batch/backend: {queue_path}"
            )
        by_id = {entry["id"]: entry for entry in batch["entries"]}
        for row in queue["entries"]:
            attempt = row["attempts"][-1]
            case_path = Path(attempt["file"]["path"])
            if not case_path.is_file():
                write_json(case_path, by_id[row["id"]]["case_spec"])
                attempt["file"].update(
                    {
                        "status": "generated",
                        "sha256": file_sha256(case_path),
                        "updated_at": now_utc(),
                    }
                )
    else:
        queue = build_queue(
            batch,
            input_root=input_root,
            run_root=run_root,
            backend=backend,
        )
    write_json(input_root / "batch_manifest.json", batch)
    write_json(queue_path, queue)
    return queue, queue_path


def latest_attempt(row: dict[str, Any]) -> dict[str, Any]:
    return row["attempts"][-1]


def queue_table(queue: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for entry in queue["entries"]:
        attempt = latest_attempt(entry)
        rows.append(
            {
                "id": entry["id"],
                "case_id": entry["case_id"],
                "attempt": attempt["attempt"],
                "status": entry["status"],
                "file": attempt["file"]["status"],
                "render": attempt["render"]["status"],
                "validation": attempt["validation"]["status"],
                "regeneration_count": entry["regeneration_count"],
                "run_dir": attempt["render"].get("run_dir"),
            }
        )
    return rows


def parse_run_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def error_tail(completed: subprocess.CompletedProcess[str]) -> str | None:
    text = (completed.stderr or completed.stdout or "").strip()
    return text[-1200:] or None


def run_queue(
    batch: dict[str, Any],
    queue: dict[str, Any],
    queue_path: Path,
    *,
    input_root: Path,
    run_root: Path,
    backend: str,
    env: dict[str, str],
    retry_failed: bool,
    continue_on_error: bool,
) -> None:
    batch_entries = {entry["id"]: entry for entry in batch["entries"]}
    failed_statuses = {"render_failed", "validation_failed"}
    if retry_failed and not any(
        row["status"] in failed_statuses for row in queue["entries"]
    ):
        raise ValueError("queue has no failed entries to regenerate")

    # ponytail: one queue writer; add an OS lock when parallel workers are introduced.
    for row in queue["entries"]:
        if retry_failed:
            if row["status"] not in failed_statuses:
                continue
            row["regeneration_count"] += 1
            row["attempts"].append(
                new_attempt(
                    batch_entries[row["id"]],
                    input_root=input_root,
                    run_root=run_root,
                    backend=backend,
                    attempt_number=len(row["attempts"]) + 1,
                )
            )
            row["status"] = "pending_render"

        attempt = latest_attempt(row)
        if row["status"] != "pending_render":
            continue

        command = shlex.split(attempt["render"]["command"])
        row["status"] = "rendering"
        attempt["render"].update({"status": "running", "updated_at": now_utc()})
        queue["updated_at"] = now_utc()
        write_json(queue_path, queue)
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        result = parse_run_result(completed)
        run_dir = result.get("run_dir")
        render_ok = (
            completed.returncode == 0
            and isinstance(run_dir, str)
            and Path(run_dir).is_dir()
        )
        attempt["render"].update(
            {
                "status": "completed" if render_ok else "failed",
                "returncode": completed.returncode,
                "run_dir": run_dir,
                "error": None if render_ok else error_tail(completed),
                "updated_at": now_utc(),
            }
        )
        if not render_ok:
            row["status"] = "render_failed"
            attempt["validation"].update(
                {"status": "blocked", "updated_at": now_utc()}
            )
            queue["updated_at"] = now_utc()
            write_json(queue_path, queue)
            if not continue_on_error:
                break
            continue

        validation_command = [
            sys.executable,
            str(ROOT / "scripts" / "harness_verify_run.py"),
            str(run_dir),
        ]
        row["status"] = "validating"
        attempt["validation"].update(
            {
                "status": "running",
                "command": shlex.join(validation_command),
                "updated_at": now_utc(),
            }
        )
        write_json(queue_path, queue)
        verified = subprocess.run(
            validation_command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        validation_ok = verified.returncode == 0
        attempt["validation"].update(
            {
                "status": "passed" if validation_ok else "failed",
                "returncode": verified.returncode,
                "error": None if validation_ok else error_tail(verified),
                "updated_at": now_utc(),
            }
        )
        row["status"] = "validated" if validation_ok else "validation_failed"
        queue["updated_at"] = now_utc()
        write_json(queue_path, queue)
        if not validation_ok and not continue_on_error:
            break


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.retry_failed and not args.execute:
        parser.error("--retry-failed requires --execute")
    batch_path = Path(args.batch).expanduser().resolve(strict=True)
    try:
        batch = load_batch(batch_path)
        workspace = (
            Path(args.workspace).expanduser().resolve(strict=False)
            if args.workspace
            else None
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if not args.execute and not args.prepare:
        preview_root = (
            Path("<workspace>")
            / "runs"
            / "case_routes"
            / Path(batch["case_route"])
        )
        preview_input_root = (
            preview_root
            / "inputs"
            / "parameter_batches"
            / batch["batch_id"]
        )
        preview_run_root = preview_root / "parameter_batches" / batch["batch_id"]
        commands = []
        table = []
        for entry in batch["entries"]:
            case_path, output_root = attempt_paths(
                entry,
                input_root=preview_input_root,
                run_root=preview_run_root,
                attempt_number=1,
            )
            command = command_for(
                entry,
                case_path=case_path,
                output_root=output_root,
                backend=args.backend,
            )
            commands.append(command)
            table.append(
                {
                    "id": entry["id"],
                    "case_id": entry["case_spec"]["case_id"],
                    "attempt": 1,
                    "file": "would_generate",
                    "render": "pending",
                    "validation": "blocked",
                    "regeneration_count": 0,
                }
            )
        print(
            json.dumps(
                {
                    "schema_version": "harness_parameter_batch_preview_v1",
                    "batch": str(batch_path),
                    "entry_count": len(commands),
                    "commands": [shlex.join(command) for command in commands],
                    "generation_table": table,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    try:
        input_root, run_root = queue_roots(batch, workspace)
        queue, queue_path = load_or_create_queue(
            batch,
            input_root=input_root,
            run_root=run_root,
            backend=args.backend,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.prepare:
        print(
            json.dumps(
                {
                    "schema_version": "harness_parameter_batch_prepare_v1",
                    "batch": str(batch_path),
                    "queue": str(queue_path),
                    "entry_count": len(queue["entries"]),
                    "generation_table": queue_table(queue),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    env = os.environ.copy()
    if workspace is not None:
        env[WORKSPACE_ENV] = str(workspace)
    try:
        run_queue(
            batch,
            queue,
            queue_path,
            input_root=input_root,
            run_root=run_root,
            backend=args.backend,
            env=env,
            retry_failed=args.retry_failed,
            continue_on_error=args.continue_on_error,
        )
    except ValueError as exc:
        parser.error(str(exc))
    table = queue_table(queue)
    failed_count = sum(
        row["status"] in {"render_failed", "validation_failed"} for row in table
    )
    completed_count = sum(row["status"] == "validated" for row in table)
    summary = {
        "schema_version": "harness_parameter_batch_run_v1",
        "batch": str(batch_path),
        "input_root": str(input_root),
        "queue": str(queue_path),
        "requested_count": len(batch["entries"]),
        "completed_count": completed_count,
        "failed_count": failed_count,
        "results": table,
    }
    write_json(input_root / "batch_run.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return (
        0
        if completed_count == len(batch["entries"]) and not failed_count
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
