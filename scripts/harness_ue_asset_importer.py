from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
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
    try:
        ue_request_path, temporary_paths = _prepare_ue_request(request_path, request)
    except (OSError, ValueError) as exc:
        _write_json(
            result_path,
            _failure_result(
                request,
                code="backend_asset_import_failed",
                message=f"could not normalize source asset for Unreal: {exc}",
            ),
        )
        return 0
    environment = os.environ.copy()
    environment.update(
        {
            "SIM_HARNESS_UE_IMPORT_REQUEST": str(ue_request_path),
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
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stdout_file, tempfile.TemporaryFile(
            mode="w+", encoding="utf-8", errors="replace"
        ) as stderr_file:
            try:
                process = subprocess.Popen(
                    command,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    shell=False,
                    env=environment,
                )
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
            outcome = _wait_for_result(process, result_path=result_path, timeout_s=float(args.timeout))
            if outcome == "result":
                _stop_process(process)
            elif outcome == "timeout":
                _stop_process(process)
                _write_json(
                    result_path,
                    _failure_result(
                        request,
                        code="backend_importer_timeout",
                        message=f"Unreal asset import exceeded {float(args.timeout):g}s",
                        retriable=True,
                    ),
                )
            else:
                returncode = process.returncode
                if not _complete_json_file(result_path):
                    _write_json(
                        result_path,
                        _failure_result(
                            request,
                            code="backend_importer_execution_failed",
                            message=f"Unreal Editor exited with code {returncode} without an importer result",
                            retriable=True,
                        ),
                    )
            stdout_file.seek(0)
            stderr_file.seek(0)
            _emit_output(stdout_file.read(), stderr_file.read())
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)
    return 0


def _prepare_ue_request(request_path: Path, request: dict[str, Any]) -> tuple[Path, tuple[Path, ...]]:
    sources = request.get("source_files") or []
    if len(sources) != 1:
        raise ValueError("Unreal static-mesh import requires exactly one source file")
    source = Path(str(sources[0].get("local_path") or "")).expanduser().resolve()
    if source.suffix.casefold() != ".obj" or not source.is_file():
        raise ValueError(f"Unreal static-mesh import requires a materialized OBJ: {source}")
    normalized_obj = request_path.with_name(f"{request_path.stem}.ue_centimeters.obj")
    _write_scaled_obj(source, normalized_obj, scale=100.0)
    ue_request = json.loads(json.dumps(request))
    payload = normalized_obj.read_bytes()
    ue_request["source_files"][0].update(
        {
            "local_path": str(normalized_obj),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
            "materialized": True,
        }
    )
    ue_request_path = request_path.with_name(f"{request_path.stem}.ue_import.json")
    _write_json(ue_request_path, ue_request)
    return ue_request_path, (normalized_obj, ue_request_path)


def _write_scaled_obj(source: Path, destination: Path, *, scale: float) -> None:
    output: list[str] = []
    vertex_count = 0
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.startswith("v "):
            fields = line.split()
            if len(fields) != 4:
                raise ValueError(f"unsupported OBJ vertex record: {line}")
            coordinates = [float(value) * scale for value in fields[1:]]
            if not all(value == value and abs(value) != float("inf") for value in coordinates):
                raise ValueError("OBJ vertex coordinates must be finite")
            line = "v " + " ".join(_format_float(value) for value in coordinates)
            vertex_count += 1
        output.append(line)
    if vertex_count == 0:
        raise ValueError("OBJ contains no vertices")
    output.insert(1, "# normalized from meters to Unreal centimeters")
    destination.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")


def _wait_for_result(process: subprocess.Popen[str], *, result_path: Path, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    while True:
        if _complete_json_file(result_path):
            return "result"
        if process.poll() is not None:
            return "exit"
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(0.1)


def _complete_json_file(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("status") in {"fulfilled", "blocked", "failed"}


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def _format_float(value: float) -> str:
    text = format(float(value), ".17g")
    return "0" if text in {"-0", "-0.0"} else text


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
