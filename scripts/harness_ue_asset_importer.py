from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
UE_SCRIPT = ROOT / "scripts" / "native_ue_asset_importer.py"
DEFAULT_TIMEOUT_S = 600.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import one Provider asset through a real Unreal Editor process.")
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--ue-executable")
    parser.add_argument("--ue-project")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    request_path = Path(args.request).expanduser().resolve()
    result_path = Path(args.result).expanduser().resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    workspace_value = os.environ.get("SIM_HARNESS_WORKSPACE", "").strip()
    workspace = Path(workspace_value).expanduser().resolve() if workspace_value else None
    ue_executable = Path(
        args.ue_executable or os.environ.get("SIM_STUDIO_UE_EXECUTABLE", "")
    ).expanduser()
    ue_project = Path(
        args.ue_project
        or os.environ.get("SIM_STUDIO_UE_PROJECT", "")
        or (workspace / "ue" / "SimulatorWorkspace.uproject" if workspace is not None else "")
    ).expanduser()
    failure = _configuration_failure(request, ue_executable=ue_executable, ue_project=ue_project)
    if failure is not None:
        _write_json(result_path, failure)
        return 0

    result_path.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "SIM_HARNESS_UE_IMPORT_REQUEST": str(request_path),
            "SIM_HARNESS_UE_IMPORT_RESULT": str(result_path),
            "SIM_HARNESS_UE_IMPORT_PROJECT_CONTENT": str(ue_project.parent / "Content"),
        }
    )
    command = [
        str(ue_executable),
        f"-project={ue_project}",
        "-RenderOffScreen",
        "-unattended",
        "-nosplash",
        "-NoScreenMessages",
        "-stdout",
        "-FullStdOutLogOutput",
        f"-ExecutePythonScript={UE_SCRIPT}",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=float(args.timeout),
            shell=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        _write_json(
            result_path,
            _failure_result(
                request,
                code="backend_importer_timeout",
                message=f"Unreal asset import exceeded {float(args.timeout):g}s",
                retriable=True,
            ),
        )
        _emit_output(exc.stdout, exc.stderr)
        return 0
    except OSError as exc:
        _write_json(
            result_path,
            _failure_result(
                request,
                code="backend_importer_execution_failed",
                message=f"could not start Unreal Editor: {exc}",
                retriable=True,
            ),
        )
        return 0

    _emit_output(completed.stdout, completed.stderr)
    if not result_path.is_file():
        _write_json(
            result_path,
            _failure_result(
                request,
                code="backend_importer_execution_failed",
                message=f"Unreal Editor exited with code {completed.returncode} without an importer result",
                retriable=True,
            ),
        )
    return 0


def _configuration_failure(request: dict[str, Any], *, ue_executable: Path, ue_project: Path) -> dict[str, Any] | None:
    if not ue_executable.is_file():
        return _failure_result(
            request,
            code="backend_importer_unavailable",
            message=f"Unreal Editor executable is unavailable: {ue_executable}",
        )
    if not ue_project.is_file() or ue_project.suffix.casefold() != ".uproject":
        return _failure_result(
            request,
            code="backend_importer_unavailable",
            message=f"Unreal project is unavailable: {ue_project}",
        )
    if not UE_SCRIPT.is_file():
        return _failure_result(
            request,
            code="backend_importer_unavailable",
            message=f"Unreal importer script is unavailable: {UE_SCRIPT}",
        )
    return None


def _failure_result(
    request: dict[str, Any],
    *,
    code: str,
    message: str,
    retriable: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": "harness_backend_asset_import_result_v1",
        "request_id": request.get("request_id"),
        "request_digest": request.get("request_digest"),
        "asset_id": request.get("asset_id"),
        "status": "blocked" if code == "backend_importer_unavailable" else "failed",
        "failure": {"code": code, "message": message, "retriable": retriable},
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _emit_output(stdout: Any, stderr: Any, *, limit: int = 32768) -> None:
    if stdout:
        print(str(stdout)[-limit:])
    if stderr:
        print(str(stderr)[-limit:], file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
