from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
UE_SCRIPT = ROOT / "scripts" / "native_ue_asset_importer.py"
DEFAULT_TIMEOUT_S = 300.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Provider assets through a real Unreal Editor process.")
    parser.add_argument("--request")
    parser.add_argument("--result")
    parser.add_argument("--batch-request")
    parser.add_argument("--batch-result")
    parser.add_argument("--ue-executable")
    parser.add_argument("--ue-project")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.batch_request or args.batch_result:
        if not args.batch_request or not args.batch_result or args.request or args.result:
            raise SystemExit("batch import requires --batch-request and --batch-result only")
        return _main_batch(args)
    if not args.request or not args.result:
        raise SystemExit("single import requires --request and --result")
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


def _main_batch(args: argparse.Namespace) -> int:
    manifest_path = Path(args.batch_request).expanduser().resolve()
    batch_result_path = Path(args.batch_result).expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest.get("items") or []
    if not isinstance(items, list) or not items:
        raise SystemExit("batch request contains no import items")
    requests: list[dict[str, Any]] = []
    result_paths: list[Path] = []
    request_paths: list[Path] = []
    for item in items:
        request_path = Path(str(item["request_path"])).expanduser().resolve()
        request_paths.append(request_path)
        result_paths.append(Path(str(item["result_path"])).expanduser().resolve())
        requests.append(json.loads(request_path.read_text(encoding="utf-8")))
    workspace_value = os.environ.get("SIM_HARNESS_WORKSPACE", "").strip()
    workspace = Path(workspace_value).expanduser().resolve() if workspace_value else None
    ue_executable = Path(args.ue_executable or os.environ.get("SIM_STUDIO_UE_EXECUTABLE", "")).expanduser()
    ue_project = Path(
        args.ue_project
        or os.environ.get("SIM_STUDIO_UE_PROJECT", "")
        or (workspace / "ue" / "SimulatorWorkspace.uproject" if workspace is not None else "")
    ).expanduser()
    failure = _configuration_failure(requests[0], ue_executable=ue_executable, ue_project=ue_project)
    if failure is not None:
        for request, result_path in zip(requests, result_paths):
            _write_json(
                result_path,
                _failure_result(request, code=failure["failure"]["code"], message=failure["failure"]["message"]),
            )
        return 0
    temporary_paths: list[Path] = []
    prepared_requests: list[dict[str, Any]] = []
    try:
        for request_path, request in zip(request_paths, requests):
            prepared_path, temporary = _prepare_ue_request(request_path, request)
            temporary_paths.extend(temporary)
            prepared_requests.append(json.loads(prepared_path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        for request, result_path in zip(requests, result_paths):
            _write_json(
                result_path,
                _failure_result(
                    request,
                    code="backend_asset_import_failed",
                    message=f"could not normalize source asset for Unreal: {exc}",
                ),
            )
        return 0
    ue_batch_request_path = manifest_path.with_name(f"{manifest_path.stem}.ue_import.json")
    _write_json(ue_batch_request_path, {"requests": prepared_requests})
    temporary_paths.append(ue_batch_request_path)
    batch_result_path.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "SIM_HARNESS_UE_IMPORT_BATCH_REQUEST": str(ue_batch_request_path),
            "SIM_HARNESS_UE_IMPORT_BATCH_RESULT": str(batch_result_path),
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
    results: list[dict[str, Any]] | None = None
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stdout_file, tempfile.TemporaryFile(
            mode="w+", encoding="utf-8", errors="replace"
        ) as stderr_file:
            try:
                process = subprocess.Popen(command, stdout=stdout_file, stderr=stderr_file, text=True, shell=False, env=environment)
            except OSError as exc:
                results = [
                    _failure_result(
                        request,
                        code="backend_importer_execution_failed",
                        message=f"could not start Unreal Editor: {exc}",
                        retriable=True,
                    )
                    for request in requests
                ]
            else:
                outcome = _wait_for_result(process, result_path=batch_result_path, timeout_s=float(args.timeout))
                if outcome == "result":
                    _stop_process(process)
                    payload = json.loads(batch_result_path.read_text(encoding="utf-8"))
                    results = payload.get("results") if isinstance(payload, dict) else None
                elif outcome == "timeout":
                    _stop_process(process)
                    results = [
                        _failure_result(
                            request,
                            code="backend_importer_timeout",
                            message=f"Unreal asset import exceeded {float(args.timeout):g}s",
                            retriable=True,
                        )
                        for request in requests
                    ]
                else:
                    results = [
                        _failure_result(
                            request,
                            code="backend_importer_execution_failed",
                            message=f"Unreal Editor exited with code {process.returncode} without an importer result",
                            retriable=True,
                        )
                        for request in requests
                    ]
            stdout_file.seek(0)
            stderr_file.seek(0)
            _emit_output(stdout_file.read(), stderr_file.read())
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)
    if not isinstance(results, list) or len(results) != len(requests):
        results = [
            _failure_result(
                request,
                code="backend_importer_result_invalid",
                message="Unreal batch importer returned the wrong number of results",
            )
            for request in requests
        ]
    for result, result_path in zip(results, result_paths):
        _write_json(result_path, result)
    _write_json(batch_result_path, {"results": results})
    return 0


def _prepare_ue_request(request_path: Path, request: dict[str, Any]) -> tuple[Path, tuple[Path, ...]]:
    sources = request.get("source_files") or []
    if len(sources) != 1:
        raise ValueError("Unreal static-mesh import requires exactly one source file")
    source = Path(str(sources[0].get("local_path") or "")).expanduser().resolve()
    if source.suffix.casefold() not in {".obj", ".fbx"} or not source.is_file():
        raise ValueError(f"Unreal static-mesh import requires a materialized OBJ or FBX: {source}")
    if source.suffix.casefold() == ".fbx":
        ue_request = json.loads(json.dumps(request))
        ue_request["portable_collision_artifact_path"] = str(
            request_path.with_name("qualified_collision_mesh.obj")
        )
        ue_request_path = request_path.with_name(f"{request_path.stem}.ue_import.json")
        _write_json(ue_request_path, ue_request)
        return ue_request_path, (ue_request_path,)
    normalized_obj = request_path.with_name(f"{request_path.stem}.ue_centimeters.obj")
    if str(request.get("source_kind") or "") in {"external_site", "model_generation"} and request.get("expected_size_m"):
        fitted_size_m = _write_fitted_obj(
            source,
            normalized_obj,
            expected_size_m=request["expected_size_m"],
            source_up_axis=(
                "y"
                if str(request.get("provider_id") or "").casefold() == "meshy_model_generation_v1"
                else "z"
            ),
        )
    else:
        _write_scaled_obj(source, normalized_obj, scale=100.0)
        fitted_size_m = None
    ue_request = json.loads(json.dumps(request))
    ue_request["portable_collision_artifact_path"] = str(
        request_path.with_name("qualified_collision_mesh.obj")
    )
    if fitted_size_m is not None:
        ue_request["expected_size_m"] = fitted_size_m
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


def _write_fitted_obj(
    source: Path,
    destination: Path,
    *,
    expected_size_m: object,
    source_up_axis: str = "z",
) -> list[float]:
    if not isinstance(expected_size_m, list) or len(expected_size_m) != 3:
        raise ValueError("remote OBJ normalization requires three expected_size_m values")
    expected_cm = [float(value) * 100.0 for value in expected_size_m]
    if any(value <= 0 or not math.isfinite(value) for value in expected_cm):
        raise ValueError("remote OBJ normalization expected_size_m must be finite and positive")
    lines = source.read_text(encoding="utf-8").splitlines()
    vertices: list[list[float]] = []
    for line in lines:
        if line.startswith("v "):
            fields = line.split()
            if len(fields) < 4:
                raise ValueError(f"unsupported OBJ vertex record: {line}")
            vertex = [float(value) for value in fields[1:4]]
            if any(not math.isfinite(value) for value in vertex):
                raise ValueError("OBJ vertex coordinates must be finite")
            vertices.append(vertex)
    if not vertices:
        raise ValueError("OBJ contains no vertices")
    oriented_vertices = [_orient_obj_vector(vertex, source_up_axis=source_up_axis) for vertex in vertices]
    minima = [min(vertex[axis] for vertex in oriented_vertices) for axis in range(3)]
    maxima = [max(vertex[axis] for vertex in oriented_vertices) for axis in range(3)]
    extents = [maxima[axis] - minima[axis] for axis in range(3)]
    if any(value <= 0 for value in extents):
        raise ValueError("OBJ bounds must be non-degenerate on every axis")
    centers = [(minima[axis] + maxima[axis]) / 2.0 for axis in range(3)]
    source_diagonal = math.sqrt(sum(value * value for value in extents))
    target_diagonal = math.sqrt(sum(value * value for value in expected_cm))
    scale = target_diagonal / source_diagonal
    fitted_size_cm = [value * scale for value in extents]
    output: list[str] = [f"# uniformly fitted to CaseSpec size; source_up_axis={source_up_axis}"]
    vertex_index = 0
    for line in lines:
        if line.startswith("v "):
            vertex = oriented_vertices[vertex_index]
            line = "v " + " ".join(
                _format_float((vertex[axis] - centers[axis]) * scale) for axis in range(3)
            )
            vertex_index += 1
        elif line.startswith("vn "):
            fields = line.split()
            if len(fields) < 4:
                raise ValueError(f"unsupported OBJ normal record: {line}")
            normal = _orient_obj_vector([float(value) for value in fields[1:4]], source_up_axis=source_up_axis)
            line = "vn " + " ".join(_format_float(value) for value in normal)
        output.append(line)
    destination.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")
    return [value / 100.0 for value in fitted_size_cm]


def _orient_obj_vector(vector: list[float], *, source_up_axis: str) -> list[float]:
    if source_up_axis == "z":
        return list(vector)
    if source_up_axis == "y":
        return [vector[0], -vector[2], vector[1]]
    raise ValueError(f"unsupported OBJ source up axis: {source_up_axis}")


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
    return isinstance(value, dict) and (
        value.get("status") in {"fulfilled", "blocked", "failed"}
        or isinstance(value.get("results"), list)
    )


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
