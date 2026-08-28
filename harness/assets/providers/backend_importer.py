from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from harness.assets.providers.contracts import (
    BACKEND_IMPORT_RESULT_SCHEMA,
    BackendImportRequest,
    BackendImportResult,
)
from harness.core.artifact_schema import write_json


# The production launcher owns the Unreal process timeout (300 seconds) and
# needs enough time afterward to persist its structured result.  Keep this
# outer command timeout longer so it does not terminate the launcher first.
DEFAULT_TIMEOUT_S = 330.0
IMPORTER_COMMAND_ENV = "SIM_HARNESS_UE_ASSET_IMPORTER_CMD"
IMPORTER_CONTRACT_VERSION = "ue_static_mesh_import_v4"


class BackendImporterAdapter:
    def import_asset(self, request: BackendImportRequest, *, work_dir: Path, workspace: Path) -> BackendImportResult:
        raise NotImplementedError

    def import_assets(
        self,
        requests: Sequence[tuple[BackendImportRequest, Path]],
        *,
        workspace: Path,
    ) -> list[BackendImportResult]:
        return [self.import_asset(request, work_dir=work_dir, workspace=workspace) for request, work_dir in requests]


class BackendImportValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class UECommandImporterAdapter(BackendImporterAdapter):
    def __init__(self, command: Sequence[str] | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        configured = list(command) if command is not None else shlex.split(os.environ.get(IMPORTER_COMMAND_ENV, ""))
        self.command = tuple(str(value) for value in configured if str(value))
        self.timeout_s = float(timeout_s)

    def import_asset(self, request: BackendImportRequest, *, work_dir: Path, workspace: Path) -> BackendImportResult:
        work_dir.mkdir(parents=True, exist_ok=True)
        request_path = work_dir / "backend_import_request.json"
        result_path = work_dir / "backend_import_result.json"
        write_json(request_path, request.to_dict())
        if str(request.data.get("target_backend") or "").casefold() not in {"ue", "unreal"}:
            result = _failure_result(
                request.data,
                status="blocked",
                code="backend_importer_unsupported_backend",
                message=f"UE importer cannot bind target backend {request.data.get('target_backend')}",
            )
            write_json(result_path, result)
            return BackendImportResult.from_dict(result)
        source_error = _validate_file_records(request.data["source_files"], workspace=workspace)
        if source_error:
            result = _failure_result(request.data, status="failed", code=source_error, message="source file validation failed")
            write_json(result_path, result)
            return BackendImportResult.from_dict(result)
        if not self.command:
            result = _failure_result(
                request.data,
                status="blocked",
                code="backend_importer_unavailable",
                message="no UE backend asset importer command is configured",
            )
            write_json(result_path, result)
            return BackendImportResult.from_dict(result)
        argv = [*self.command, "--request", str(request_path), "--result", str(result_path)]
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            result = _failure_result(
                request.data,
                status="failed",
                code="backend_importer_timeout",
                message=f"backend importer exceeded {self.timeout_s:g}s",
                retriable=True,
                stdout=_safe_output(exc.stdout),
                stderr=_safe_output(exc.stderr),
            )
            write_json(result_path, result)
            return BackendImportResult.from_dict(result)
        stdout = _safe_output(completed.stdout)
        stderr = _safe_output(completed.stderr)
        if completed.returncode != 0 or not result_path.is_file():
            result = _failure_result(
                request.data,
                status="failed",
                code="backend_importer_execution_failed",
                message=f"backend importer exited with code {completed.returncode}",
                retriable=True,
                stdout=stdout,
                stderr=stderr,
                returncode=completed.returncode,
            )
            write_json(result_path, result)
            return BackendImportResult.from_dict(result)
        try:
            raw = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise ValueError("result root must be an object")
            result = dict(raw)
            result["stdout"] = stdout
            result["stderr"] = stderr
            result["returncode"] = completed.returncode
            parsed = BackendImportResult.from_dict(result)
            _validate_result_identity(request.data, parsed.data)
            source_error = _validate_file_records(request.data["source_files"], workspace=workspace)
            if source_error:
                raise BackendImportValidationError(source_error, "source file changed during backend import")
            _validate_fulfilled_result(parsed.data, workspace=workspace)
        except BackendImportValidationError as exc:
            result = _failure_result(
                request.data,
                status="failed",
                code=exc.code,
                message=exc.message,
                stdout=stdout,
                stderr=stderr,
                returncode=completed.returncode,
            )
            write_json(result_path, result)
            return BackendImportResult.from_dict(result)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            result = _failure_result(
                request.data,
                status="failed",
                code="backend_importer_result_invalid",
                message=str(exc),
                stdout=stdout,
                stderr=stderr,
                returncode=completed.returncode,
            )
            write_json(result_path, result)
            return BackendImportResult.from_dict(result)
        write_json(result_path, parsed.to_dict())
        return parsed

    def import_assets(
        self,
        requests: Sequence[tuple[BackendImportRequest, Path]],
        *,
        workspace: Path,
    ) -> list[BackendImportResult]:
        if len(requests) <= 1:
            return super().import_assets(requests, workspace=workspace)
        prepared: list[tuple[BackendImportRequest, Path, Path]] = []
        direct: dict[str, BackendImportResult] = {}
        for request, work_dir in requests:
            work_dir.mkdir(parents=True, exist_ok=True)
            request_path = work_dir / "backend_import_request.json"
            result_path = work_dir / "backend_import_result.json"
            write_json(request_path, request.to_dict())
            error = _validate_file_records(request.data["source_files"], workspace=workspace)
            if error:
                direct[request.data["request_digest"]] = BackendImportResult.from_dict(
                    _failure_result(
                        request.data,
                        status="failed",
                        code=error,
                        message="source file validation failed",
                    )
                )
            else:
                prepared.append((request, request_path, result_path))
        if not prepared:
            return [direct[request.data["request_digest"]] for request, _ in requests]
        if not self.command:
            for request, _, _ in prepared:
                direct[request.data["request_digest"]] = BackendImportResult.from_dict(
                    _failure_result(
                        request.data,
                        status="blocked",
                        code="backend_importer_unavailable",
                        message="no UE backend asset importer command is configured",
                    )
                )
            return [direct[request.data["request_digest"]] for request, _ in requests]
        batch_digest = hashlib.sha256(
            "\n".join(request.data["request_digest"] for request, _, _ in prepared).encode("utf-8")
        ).hexdigest()
        batch_dir = workspace / "providers" / "_import_batches" / batch_digest
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch_request_path = batch_dir / "backend_import_batch_request.json"
        batch_result_path = batch_dir / "backend_import_batch_result.json"
        batch_result_path.unlink(missing_ok=True)
        for _, _, result_path in prepared:
            result_path.unlink(missing_ok=True)
        write_json(
            batch_request_path,
            {
                "schema_version": "harness_backend_asset_import_batch_v1",
                "items": [
                    {"request_path": str(request_path), "result_path": str(result_path)}
                    for _, request_path, result_path in prepared
                ],
            },
        )
        argv = [
            *self.command,
            "--batch-request",
            str(batch_request_path),
            "--batch-result",
            str(batch_result_path),
        ]
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                shell=False,
            )
            stdout = _safe_output(completed.stdout)
            stderr = _safe_output(completed.stderr)
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = _safe_output(exc.stdout)
            stderr = _safe_output(exc.stderr)
            returncode = None
        for request, _, result_path in prepared:
            if returncode is None:
                parsed = BackendImportResult.from_dict(
                    _failure_result(
                        request.data,
                        status="failed",
                        code="backend_importer_timeout",
                        message=f"backend importer exceeded {self.timeout_s:g}s",
                        retriable=True,
                        stdout=stdout,
                        stderr=stderr,
                        returncode=returncode,
                    )
                )
            elif returncode != 0:
                parsed = BackendImportResult.from_dict(
                    _failure_result(
                        request.data,
                        status="failed",
                        code="backend_importer_execution_failed",
                        message=f"backend importer exited with code {returncode}",
                        retriable=True,
                        stdout=stdout,
                        stderr=stderr,
                        returncode=returncode,
                    )
                )
            else:
                try:
                    raw = json.loads(result_path.read_text(encoding="utf-8"))
                    if not isinstance(raw, Mapping):
                        raise ValueError("result root must be an object")
                    result = dict(raw)
                    result.update(
                        {
                            "stdout": stdout,
                            "stderr": stderr,
                            "returncode": returncode,
                            "batch_size": len(prepared),
                            "cache_hit": False,
                            "importer_invoked": True,
                        }
                    )
                    parsed = BackendImportResult.from_dict(result)
                    validate_import_result(request, parsed, workspace=workspace)
                except (OSError, json.JSONDecodeError, BackendImportValidationError, ValueError) as exc:
                    parsed = BackendImportResult.from_dict(
                        _failure_result(
                            request.data,
                            status="failed",
                            code="backend_importer_result_invalid",
                            message=str(exc),
                            stdout=stdout,
                            stderr=stderr,
                            returncode=returncode,
                        )
                    )
            write_json(result_path, parsed.to_dict())
            direct[request.data["request_digest"]] = parsed
        return [direct[request.data["request_digest"]] for request, _ in requests]


def _validate_result_identity(request: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    for field in ("request_id", "request_digest", "asset_id"):
        if result.get(field) != request.get(field):
            raise BackendImportValidationError(
                "backend_importer_identity_mismatch",
                f"backend importer result {field} does not match request",
            )


def validate_import_result(
    request: BackendImportRequest,
    result: BackendImportResult,
    *,
    workspace: Path,
) -> None:
    _validate_result_identity(request.data, result.data)
    source_error = _validate_file_records(request.data["source_files"], workspace=workspace)
    if source_error:
        raise BackendImportValidationError(source_error, "source file validation failed")
    _validate_fulfilled_result(result.data, workspace=workspace)


def _validate_fulfilled_result(result: Mapping[str, Any], *, workspace: Path) -> None:
    if result.get("status") != "fulfilled":
        return
    if not re.fullmatch(r"/Game/[^\s]+\.[^/\s.]+", str(result.get("object_path") or "")):
        raise BackendImportValidationError(
            "invalid_backend_object_path",
            "backend importer returned an invalid /Game object path",
        )
    if not str(result.get("class_name") or "").strip():
        raise BackendImportValidationError("backend_class_name_missing", "backend importer class_name is missing")
    error = _validate_file_records(result.get("files") or [], workspace=workspace)
    if error:
        raise BackendImportValidationError(error, "imported file validation failed")
    error = _validate_file_records(result.get("dependencies") or [], workspace=workspace, dependencies=True)
    if error:
        raise BackendImportValidationError(error, "imported dependency validation failed")
    portable_collision = result.get("portable_collision_artifact")
    if portable_collision is not None:
        if not isinstance(portable_collision, Mapping):
            raise BackendImportValidationError(
                "portable_collision_artifact_invalid",
                "portable collision artifact must be an object",
            )
        if portable_collision.get("schema_version") != "harness_portable_collision_mesh_v1":
            raise BackendImportValidationError(
                "portable_collision_artifact_invalid",
                "portable collision artifact schema is unsupported",
            )
        error = _validate_file_records([portable_collision], workspace=workspace)
        if error:
            raise BackendImportValidationError(error, "portable collision artifact validation failed")
        if portable_collision.get("format") != "obj" or portable_collision.get("coordinate_system") != "asset_local_z_up_m":
            raise BackendImportValidationError(
                "portable_collision_artifact_invalid",
                "portable collision artifact must be an asset-local Z-up OBJ in meters",
            )
        transform = portable_collision.get("artifact_to_asset_transform")
        matrix = transform.get("matrix4x4") if isinstance(transform, Mapping) else None
        if not (
            isinstance(matrix, list)
            and len(matrix) == 4
            and all(isinstance(row, list) and len(row) == 4 for row in matrix)
            and all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                for row in matrix
                for value in row
            )
        ):
            raise BackendImportValidationError(
                "portable_collision_transform_invalid",
                "portable collision artifact requires a finite artifact-to-asset 4x4 transform",
            )


def _validate_file_records(
    records: Any,
    *,
    workspace: Path,
    dependencies: bool = False,
) -> str | None:
    if not isinstance(records, list):
        return "file_records_invalid"
    for record in records:
        if not isinstance(record, Mapping):
            return "file_record_invalid"
        if dependencies and not str(record.get("dependency_id") or record.get("package") or "").strip():
            return "dependency_identity_missing"
        if record.get("materialized") is not True:
            return "dependency_incomplete" if dependencies else "file_not_materialized"
        path_value = record.get("local_path")
        if not path_value:
            return "dependency_file_missing" if dependencies else "file_path_missing"
        path = Path(str(path_value))
        try:
            path.resolve().relative_to(workspace.resolve())
        except (OSError, ValueError):
            return "file_outside_workspace"
        if not path.is_file():
            return "dependency_file_missing" if dependencies else "file_missing"
        if _is_lfs_pointer(path):
            return "dependency_lfs_pointer" if dependencies else "file_lfs_pointer"
        expected = str(record.get("sha256") or "").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            return "dependency_hash_missing" if dependencies else "file_hash_missing"
        if _sha256_file(path) != expected:
            return "dependency_hash_mismatch" if dependencies else "file_hash_mismatch"
        if record.get("byte_size") is not None and int(record["byte_size"]) != path.stat().st_size:
            return "file_size_mismatch"
    return None


def _failure_result(
    request: Mapping[str, Any],
    *,
    status: str,
    code: str,
    message: str,
    retriable: bool = False,
    stdout: str = "",
    stderr: str = "",
    returncode: int | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": BACKEND_IMPORT_RESULT_SCHEMA,
        "request_id": request["request_id"],
        "request_digest": request["request_digest"],
        "asset_id": request["asset_id"],
        "status": status,
        "failure": {"code": code, "message": message, "retriable": retriable},
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_lfs_pointer(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(80).startswith(b"version https://git-lfs.github.com/spec/v1")
    except OSError:
        return False


def _safe_output(value: Any, *, limit: int = 65536) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value or "")
    for name in (
        "SIM_HARNESS_LLM_API_KEY",
        "OPENAI_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|token|secret)\s*[=:]\s*)\S+", r"\1[REDACTED]", text)
    return text[:limit]
