from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from harness.assets.providers.contracts import stable_digest
from harness.core.artifact_schema import write_json


MESHY_API_KEY_ENV = "SIM_HARNESS_MESHY_API_KEY"
MESHY_API_ROOT = "https://api.meshy.ai/openapi/v1"
POLY_HAVEN_API_ROOT = "https://api.polyhaven.com"
POLY_HAVEN_USER_AGENT = "PhysicsAwareHarness/0.1 (asset-provider; https://polyhaven.com)"
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024


class RemoteProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: str = "failed",
        retriable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retriable = retriable
        self.details = dict(details or {})


class RemoteTransport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any] | None = None,
        timeout_s: float = 30.0,
    ) -> dict[str, Any]: ...

    def download(
        self,
        url: str,
        destination: Path,
        *,
        headers: Mapping[str, str],
        expected_md5: str | None = None,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]: ...


class UrllibRemoteTransport:
    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any] | None = None,
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers = {**headers, "Accept": "application/json"}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = _safe_http_error(exc)
            raise RemoteProviderError(
                "provider_http_error",
                f"remote provider returned HTTP {exc.code}: {detail}",
                status="blocked" if exc.code in {401, 402, 403} else "failed",
                retriable=exc.code in {408, 409, 425, 429} or exc.code >= 500,
            ) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise RemoteProviderError("provider_network_error", str(exc), retriable=True) from exc
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteProviderError("provider_response_invalid", "remote provider returned invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise RemoteProviderError("provider_response_invalid", "remote provider JSON root must be an object")
        return dict(value)

    def download(
        self,
        url: str,
        destination: Path,
        *,
        headers: Mapping[str, str],
        expected_md5: str | None = None,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RemoteProviderError("download_url_invalid", "provider download URL must use HTTPS")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f"{destination.name}.part")
        request = urllib.request.Request(url, headers={**headers, "Accept": "*/*"})
        sha256 = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        byte_size = 0
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response, partial.open("wb") as stream:
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > MAX_DOWNLOAD_BYTES:
                    raise RemoteProviderError("download_too_large", f"download exceeds {MAX_DOWNLOAD_BYTES} bytes")
                while chunk := response.read(1024 * 1024):
                    byte_size += len(chunk)
                    if byte_size > MAX_DOWNLOAD_BYTES:
                        raise RemoteProviderError("download_too_large", f"download exceeds {MAX_DOWNLOAD_BYTES} bytes")
                    sha256.update(chunk)
                    md5.update(chunk)
                    stream.write(chunk)
        except RemoteProviderError:
            partial.unlink(missing_ok=True)
            raise
        except urllib.error.HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise RemoteProviderError(
                "provider_download_failed",
                f"provider download returned HTTP {exc.code}",
                retriable=exc.code in {408, 425, 429} or exc.code >= 500,
            ) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            partial.unlink(missing_ok=True)
            raise RemoteProviderError("provider_download_failed", str(exc), retriable=True) from exc
        actual_md5 = md5.hexdigest()
        if expected_md5 and actual_md5.casefold() != expected_md5.casefold():
            partial.unlink(missing_ok=True)
            raise RemoteProviderError("download_hash_mismatch", f"MD5 mismatch for {destination.name}")
        os.replace(partial, destination)
        return {
            "path": destination,
            "sha256": sha256.hexdigest(),
            "md5": actual_md5,
            "byte_size": byte_size,
        }


@dataclass(frozen=True)
class RemoteAcquisition:
    provider_id: str
    provider_version: str
    source_kind: str
    source_uri: str
    source_asset_id: str
    asset_id: str
    name: str
    description: str
    author: str
    license: str
    license_tier: str
    request_parameters: dict[str, Any]
    input_identities: tuple[dict[str, str], ...]
    files: tuple[dict[str, Any], ...]
    import_file: Path
    canonical_file: Path
    expected_size_m: tuple[float, float, float] | None
    metadata: dict[str, Any]


class RemoteProviderAdapter(Protocol):
    provider_id: str
    provider_version: str
    source_kind: str

    def acquire(
        self,
        request: Mapping[str, Any],
        *,
        destination: Path,
        workspace: Path,
    ) -> RemoteAcquisition: ...


class MeshyModelGenerationAdapter:
    provider_id = "meshy_model_generation_v1"
    provider_version = "2026-08-06"
    source_kind = "model_generation"

    def __init__(
        self,
        *,
        transport: RemoteTransport | None = None,
        api_key: str | None = None,
        poll_interval_s: float = 5.0,
        timeout_s: float = 1800.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport or UrllibRemoteTransport()
        self.api_key = api_key
        self.poll_interval_s = float(poll_interval_s)
        self.timeout_s = float(timeout_s)
        self.sleep = sleep

    def acquire(
        self,
        request: Mapping[str, Any],
        *,
        destination: Path,
        workspace: Path,
    ) -> RemoteAcquisition:
        hint = str(request.get("provider_hint") or "").strip().casefold()
        if hint and hint not in {"meshy", "meshy_v1", self.provider_id}:
            raise RemoteProviderError("unsupported_provider_hint", f"model_generation provider is not supported: {hint}", status="blocked")
        cached = _load_cached_acquisition(destination, provider_id=self.provider_id, request=request)
        if cached is not None:
            return cached
        api_key = (os.environ.get(MESHY_API_KEY_ENV, "") if self.api_key is None else self.api_key).strip()
        if not api_key:
            raise RemoteProviderError(
                "provider_credentials_missing",
                f"Meshy API key is not configured in {MESHY_API_KEY_ENV}",
                status="blocked",
            )
        references, identities = _meshy_references(request.get("reference_inputs"), workspace=workspace)
        size = _optional_size(request.get("generation_spec"))
        payload = {
            "image_urls": references,
            "ai_model": "latest",
            "should_texture": True,
            "enable_pbr": True,
            "texture_resolution": "2k",
            "should_remesh": True,
            "topology": "triangle",
            "target_polycount": 30000,
            "image_enhancement": False,
            "remove_lighting": True,
            "target_formats": ["glb", "obj"],
            "auto_size": size is None,
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        destination.mkdir(parents=True, exist_ok=True)
        checkpoint = _load_meshy_checkpoint(destination, request=request, provider_id=self.provider_id)
        if checkpoint is None:
            submitted = self.transport.request_json(
                "POST",
                f"{MESHY_API_ROOT}/multi-image-to-3d",
                headers=headers,
                payload=payload,
                timeout_s=30.0,
            )
            write_json(destination / "submit_response.json", _redact_signed_urls(submitted))
            task_id = str(submitted.get("result") or "").strip()
            if not task_id:
                raise RemoteProviderError("provider_response_invalid", "Meshy create response does not contain a task ID")
            checkpoint = _write_meshy_checkpoint(
                destination,
                request=request,
                provider_id=self.provider_id,
                task_id=task_id,
                task={"status": "SUBMITTED"},
            )
        else:
            task_id = str(checkpoint["task_id"])
        deadline = time.monotonic() + self.timeout_s
        task: dict[str, Any] = {}
        try:
            while True:
                task = self.transport.request_json(
                    "GET",
                    f"{MESHY_API_ROOT}/multi-image-to-3d/{urllib.parse.quote(task_id, safe='')}",
                    headers=headers,
                    timeout_s=30.0,
                )
                write_json(destination / "task_response_latest.json", _redact_signed_urls(task))
                _write_meshy_checkpoint(
                    destination,
                    request=request,
                    provider_id=self.provider_id,
                    task_id=task_id,
                    task=task,
                )
                status = str(task.get("status") or "").upper()
                if status == "SUCCEEDED":
                    break
                if status in {"FAILED", "CANCELED"}:
                    raise RemoteProviderError(
                        "provider_task_failed" if status == "FAILED" else "provider_task_canceled",
                        str(task.get("task_error") or task.get("message") or f"Meshy task {status.casefold()}"),
                    )
                if status not in {"PENDING", "IN_PROGRESS"}:
                    raise RemoteProviderError("provider_response_invalid", f"unknown Meshy task status: {status or '<missing>'}")
                if time.monotonic() >= deadline:
                    raise RemoteProviderError("provider_task_timeout", f"Meshy task exceeded {self.timeout_s:g}s", retriable=True)
                self.sleep(min(self.poll_interval_s, max(0.0, deadline - time.monotonic())))
            model_urls = task.get("model_urls") if isinstance(task.get("model_urls"), Mapping) else {}
            glb_url = str(model_urls.get("glb") or task.get("model_url") or "").strip()
            obj_url = str(model_urls.get("obj") or "").strip()
            if not glb_url or not obj_url:
                raise RemoteProviderError("provider_output_missing", "Meshy task did not return both GLB and OBJ outputs")
            files: list[dict[str, Any]] = []
            for role, file_format, url in (("canonical", "glb", glb_url), ("import_source", "obj", obj_url)):
                downloaded = self.transport.download(url, destination / f"model.{file_format}", headers={})
                files.append(_download_record(downloaded, role=role, file_format=file_format))
            for optional_format in ("mtl",):
                url = str(model_urls.get(optional_format) or "").strip()
                if url:
                    downloaded = self.transport.download(url, destination / f"model.{optional_format}", headers={})
                    files.append(_download_record(downloaded, role="material_dependency", file_format=optional_format))
            downloaded_names = {Path(str(row["path"])).name for row in files}
            for texture_index, texture_set in enumerate(task.get("texture_urls") or []):
                if not isinstance(texture_set, Mapping):
                    raise RemoteProviderError("provider_response_invalid", "Meshy texture output must be an object")
                for texture_role, raw_url in sorted(texture_set.items()):
                    url = str(raw_url or "").strip()
                    if not url:
                        continue
                    name = urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).name)
                    if not name or name in downloaded_names or Path(name).name != name:
                        name = f"texture_{texture_index}_{_safe_id(str(texture_role))}.png"
                    downloaded_names.add(name)
                    downloaded = self.transport.download(url, destination / name, headers={})
                    files.append(
                        _download_record(
                            downloaded,
                            role=f"texture_{texture_role}",
                            file_format=Path(name).suffix.lstrip(".") or "png",
                        )
                    )
        except RemoteProviderError as exc:
            exc.details.update(
                {
                    "task_id": task_id,
                    "task_status": str(task.get("status") or checkpoint.get("task_status") or "UNKNOWN").upper(),
                    "progress": task.get("progress", checkpoint.get("progress")),
                    "consumed_credits": task.get("consumed_credits", checkpoint.get("consumed_credits")),
                }
            )
            raise
        output_digest = stable_digest({"task_id": task_id, "files": [{"format": row["format"], "sha256": row["sha256"]} for row in files]})
        acquisition = RemoteAcquisition(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            source_kind="model_generation",
            source_uri=f"meshy://multi-image-to-3d/{task_id}",
            source_asset_id=task_id,
            asset_id=f"generated.meshy.{output_digest[:24]}",
            name=f"Meshy generated asset {task_id[:12]}",
            description=str((request.get("search_intent") or {}).get("raw_query") or "Meshy multi-image generated mesh"),
            author="Meshy",
            license="All Rights Reserved",
            license_tier="local_preview",
            request_parameters={**payload, "image_urls": [f"input:{row['input_id']}" for row in identities]},
            input_identities=tuple(identities),
            files=tuple(files),
            import_file=destination / "model.obj",
            canonical_file=destination / "model.glb",
            expected_size_m=size,
            metadata={
                "task_id": task_id,
                "status": "SUCCEEDED",
                "consumed_credits": task.get("consumed_credits"),
                "progress": task.get("progress"),
            },
        )
        _write_cached_acquisition(destination, request=request, acquisition=acquisition)
        return acquisition


class PolyHavenExternalSiteAdapter:
    provider_id = "poly_haven_external_site_v1"
    provider_version = "2026-07-18"
    source_kind = "external_site"

    def __init__(self, *, transport: RemoteTransport | None = None, resolution: str = "1k") -> None:
        self.transport = transport or UrllibRemoteTransport()
        self.resolution = resolution

    def acquire(
        self,
        request: Mapping[str, Any],
        *,
        destination: Path,
        workspace: Path,
    ) -> RemoteAcquisition:
        del workspace
        hint = str(request.get("provider_hint") or "").strip().casefold()
        if hint and hint not in {"polyhaven", "poly_haven", "poly_haven_v1", self.provider_id} and not hint.startswith("polyhaven:"):
            raise RemoteProviderError("unsupported_provider_hint", f"external_site provider is not supported: {hint}", status="blocked")
        cached = _load_cached_acquisition(destination, provider_id=self.provider_id, request=request)
        if cached is not None:
            return cached
        headers = {"User-Agent": POLY_HAVEN_USER_AGENT}
        assets = self.transport.request_json("GET", f"{POLY_HAVEN_API_ROOT}/assets", headers=headers)
        asset_id, metadata = _select_poly_haven_asset(request, assets)
        files_response = self.transport.request_json(
            "GET",
            f"{POLY_HAVEN_API_ROOT}/files/{urllib.parse.quote(asset_id, safe='')}",
            headers=headers,
        )
        destination.mkdir(parents=True, exist_ok=True)
        write_json(destination / "asset_metadata.json", {"asset_id": asset_id, **metadata})
        write_json(destination / "files_response.json", files_response)
        selected = _poly_haven_fbx(files_response, resolution=self.resolution)
        source = self.transport.download(
            str(selected["url"]),
            destination / f"{asset_id}.fbx",
            headers=headers,
            expected_md5=str(selected.get("md5") or "") or None,
        )
        files = [_download_record(source, role="import_source", file_format="fbx")]
        includes = selected.get("include") if isinstance(selected.get("include"), Mapping) else {}
        for relative_name, record in sorted(includes.items()):
            if not isinstance(record, Mapping):
                raise RemoteProviderError("provider_response_invalid", "Poly Haven dependency record is invalid")
            relative = _safe_relative_path(str(relative_name))
            downloaded = self.transport.download(
                str(record.get("url") or ""),
                destination / relative,
                headers=headers,
                expected_md5=str(record.get("md5") or "") or None,
            )
            files.append(_download_record(downloaded, role="source_dependency", file_format=relative.suffix.lstrip(".")))
        source_version = str(metadata.get("files_hash") or stable_digest(files_response))
        canonical = destination / f"{asset_id}.fbx"
        acquisition = RemoteAcquisition(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            source_kind="external_site",
            source_uri=f"https://polyhaven.com/a/{asset_id}",
            source_asset_id=asset_id,
            asset_id=f"external.polyhaven.{_safe_id(asset_id)}.{source_version[:12]}",
            name=str(metadata.get("name") or asset_id),
            description=str(metadata.get("description") or "Poly Haven CC0 asset"),
            author=", ".join(sorted(str(value) for value in (metadata.get("authors") or {}).keys())) or "Poly Haven",
            license="CC0-1.0",
            license_tier="reference",
            request_parameters={"asset_id": asset_id, "resolution": self.resolution, "format": "fbx"},
            input_identities=(),
            files=tuple(files),
            import_file=canonical,
            canonical_file=canonical,
            expected_size_m=_poly_haven_size(metadata) or _optional_size(request.get("generation_spec")),
            metadata={
                "asset_id": asset_id,
                "files_hash": metadata.get("files_hash"),
                "category": metadata.get("category"),
                "tags": list(metadata.get("tags") or []),
                "authors": dict(metadata.get("authors") or {}),
                "api_attribution": "Poly Haven",
            },
        )
        _write_cached_acquisition(destination, request=request, acquisition=acquisition)
        return acquisition


def _write_cached_acquisition(
    destination: Path,
    *,
    request: Mapping[str, Any],
    acquisition: RemoteAcquisition,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    files = []
    for row in acquisition.files:
        path = Path(str(row["path"])).resolve()
        relative = path.relative_to(destination.resolve())
        files.append(
            {
                "role": str(row["role"]),
                "path": relative.as_posix(),
                "format": str(row["format"]),
                "sha256": str(row["sha256"]),
                "md5": str(row.get("md5") or ""),
                "byte_size": int(row["byte_size"]),
            }
        )
    payload = {
        "schema_version": "harness_remote_acquisition_v1",
        "request_identity": _request_identity(request),
        "provider_id": acquisition.provider_id,
        "provider_version": acquisition.provider_version,
        "source_kind": acquisition.source_kind,
        "source_uri": acquisition.source_uri,
        "source_asset_id": acquisition.source_asset_id,
        "asset_id": acquisition.asset_id,
        "name": acquisition.name,
        "description": acquisition.description,
        "author": acquisition.author,
        "license": acquisition.license,
        "license_tier": acquisition.license_tier,
        "request_parameters": acquisition.request_parameters,
        "input_identities": list(acquisition.input_identities),
        "files": files,
        "import_file": acquisition.import_file.resolve().relative_to(destination.resolve()).as_posix(),
        "canonical_file": acquisition.canonical_file.resolve().relative_to(destination.resolve()).as_posix(),
        "expected_size_m": list(acquisition.expected_size_m) if acquisition.expected_size_m is not None else None,
        "metadata": acquisition.metadata,
    }
    write_json(destination / "acquisition.json", payload)


def _load_cached_acquisition(
    destination: Path,
    *,
    provider_id: str,
    request: Mapping[str, Any],
) -> RemoteAcquisition | None:
    manifest = destination / "acquisition.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != "harness_remote_acquisition_v1"
        or value.get("provider_id") != provider_id
        or value.get("request_identity") != _request_identity(request)
    ):
        return None
    files: list[dict[str, Any]] = []
    for raw in value.get("files") or []:
        if not isinstance(raw, Mapping):
            return None
        try:
            relative = _safe_relative_path(str(raw.get("path") or ""))
        except RemoteProviderError:
            return None
        path = (destination / relative).resolve()
        try:
            path.relative_to(destination.resolve())
        except ValueError:
            return None
        if not path.is_file() or path.stat().st_size != int(raw.get("byte_size") or -1):
            return None
        if _sha256_file(path) != str(raw.get("sha256") or ""):
            return None
        files.append({**dict(raw), "path": path})
    if not files:
        return None
    try:
        import_file = (destination / _safe_relative_path(str(value["import_file"]))).resolve()
        canonical_file = (destination / _safe_relative_path(str(value["canonical_file"]))).resolve()
        expected = value.get("expected_size_m")
        expected_size = tuple(float(item) for item in expected) if isinstance(expected, list) else None
        if expected_size is not None and len(expected_size) != 3:
            return None
        return RemoteAcquisition(
            provider_id=str(value["provider_id"]),
            provider_version=str(value["provider_version"]),
            source_kind=str(value["source_kind"]),
            source_uri=str(value["source_uri"]),
            source_asset_id=str(value["source_asset_id"]),
            asset_id=str(value["asset_id"]),
            name=str(value["name"]),
            description=str(value["description"]),
            author=str(value["author"]),
            license=str(value["license"]),
            license_tier=str(value["license_tier"]),
            request_parameters=dict(value.get("request_parameters") or {}),
            input_identities=tuple(dict(row) for row in value.get("input_identities") or []),
            files=tuple(files),
            import_file=import_file,
            canonical_file=canonical_file,
            expected_size_m=expected_size,
            metadata=dict(value.get("metadata") or {}),
        )
    except (KeyError, TypeError, ValueError, RemoteProviderError):
        return None


def _request_identity(request: Mapping[str, Any]) -> str:
    digest = str(request.get("request_digest") or "")
    return digest if re.fullmatch(r"[0-9a-f]{64}", digest.casefold()) else stable_digest(request)


def _load_meshy_checkpoint(
    destination: Path,
    *,
    request: Mapping[str, Any],
    provider_id: str,
) -> dict[str, Any] | None:
    path = destination / "task_checkpoint.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteProviderError(
            "provider_task_checkpoint_invalid",
            "Meshy task checkpoint exists but cannot be read; refusing to submit a duplicate paid task",
            status="blocked",
        ) from exc
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != "harness_meshy_task_checkpoint_v1"
        or value.get("provider_id") != provider_id
        or value.get("request_identity") != _request_identity(request)
        or not str(value.get("task_id") or "").strip()
    ):
        raise RemoteProviderError(
            "provider_task_checkpoint_invalid",
            "Meshy task checkpoint does not match this request; refusing to submit a duplicate paid task",
            status="blocked",
        )
    return dict(value)


def _write_meshy_checkpoint(
    destination: Path,
    *,
    request: Mapping[str, Any],
    provider_id: str,
    task_id: str,
    task: Mapping[str, Any],
) -> dict[str, Any]:
    checkpoint = {
        "schema_version": "harness_meshy_task_checkpoint_v1",
        "provider_id": provider_id,
        "request_identity": _request_identity(request),
        "task_id": task_id,
        "task_status": str(task.get("status") or "UNKNOWN").upper(),
        "progress": task.get("progress"),
        "consumed_credits": task.get("consumed_credits"),
    }
    write_json(destination / "task_checkpoint.json", checkpoint)
    return checkpoint


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _meshy_references(value: Any, *, workspace: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        raise RemoteProviderError("reference_inputs_required", "Meshy multi-image generation requires 1 to 4 reference inputs", status="blocked")
    references: list[str] = []
    identities: list[dict[str, str]] = []
    for row in value:
        if not isinstance(row, Mapping):
            raise RemoteProviderError("reference_input_invalid", "Meshy reference input must be an object", status="blocked")
        input_id = str(row.get("input_id") or "").strip()
        expected_sha = str(row.get("sha256") or "").casefold()
        if not input_id or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise RemoteProviderError("input_hash_missing", f"Meshy reference input lacks a verified SHA-256: {input_id}", status="blocked")
        if row.get("upload_authorized") is not True:
            raise RemoteProviderError("upload_not_authorized", f"Meshy upload is not authorized for input: {input_id}", status="blocked")
        local_value = row.get("local_path")
        if not local_value:
            raise RemoteProviderError(
                "remote_reference_url_unsupported",
                f"Meshy MVP accepts only workspace-local image files: {input_id}",
                status="blocked",
            )
        path = Path(str(local_value)).expanduser().resolve()
        try:
            path.relative_to(workspace.resolve())
        except (OSError, ValueError) as exc:
            raise RemoteProviderError("input_outside_workspace", f"Meshy input must be stored in the external workspace: {input_id}", status="blocked") from exc
        if path.suffix.casefold() not in {".jpg", ".jpeg", ".png"} or not path.is_file():
            raise RemoteProviderError("reference_input_invalid", f"Meshy input is not a materialized JPG/PNG: {input_id}", status="blocked")
        payload = path.read_bytes()
        actual_sha = hashlib.sha256(payload).hexdigest()
        if actual_sha != expected_sha:
            raise RemoteProviderError("input_hash_mismatch", f"Meshy input hash mismatch: {input_id}", status="blocked")
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        uri = f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
        references.append(uri)
        identities.append({"input_id": input_id, "sha256": expected_sha})
    return references, identities


def _select_poly_haven_asset(request: Mapping[str, Any], assets: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    models = {str(key): dict(value) for key, value in assets.items() if isinstance(value, Mapping) and value.get("type") == 2}
    explicit = _poly_haven_explicit_id(request)
    if explicit:
        if explicit not in models:
            raise RemoteProviderError("external_asset_not_found", f"Poly Haven model does not exist: {explicit}", status="blocked")
        return explicit, models[explicit]
    search = request.get("search_intent") if isinstance(request.get("search_intent"), Mapping) else {}
    query = str(search.get("raw_query") or search.get("semantic_text") or "").strip()
    tokens = set(_search_tokens(query))
    if not tokens:
        raise RemoteProviderError("external_search_query_missing", "Poly Haven discovery requires a search query", status="blocked")
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for asset_id, metadata in models.items():
        name_tokens = set(_search_tokens(f"{asset_id} {metadata.get('name', '')}"))
        all_tokens = set(_search_tokens(" ".join([str(metadata.get("category") or ""), *[str(tag) for tag in metadata.get("tags") or []]])))
        score = 4.0 * len(tokens & name_tokens) + 1.0 * len(tokens & all_tokens)
        if _safe_id(query) in {_safe_id(asset_id), _safe_id(str(metadata.get("name") or ""))}:
            score += 100.0
        if score > 0:
            ranked.append((score, asset_id, metadata))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    if not ranked:
        raise RemoteProviderError("no_relevant_external_asset", f"Poly Haven has no relevant model for: {query}", status="blocked")
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        raise RemoteProviderError(
            "ambiguous_external_asset",
            f"Poly Haven search is ambiguous: {ranked[0][1]}, {ranked[1][1]}",
            status="blocked",
        )
    return ranked[0][1], ranked[0][2]


def _poly_haven_explicit_id(request: Mapping[str, Any]) -> str | None:
    values = [str(request.get("source_uri_hint") or ""), str(request.get("provider_hint") or "")]
    for value in values:
        text = value.strip()
        if text.casefold().startswith("polyhaven:"):
            return text.split(":", 1)[1].strip().strip("/")
        parsed = urllib.parse.urlparse(text)
        if parsed.netloc.casefold() in {"polyhaven.com", "www.polyhaven.com"} and "/a/" in parsed.path:
            return parsed.path.split("/a/", 1)[1].strip("/").split("/", 1)[0]
    return None


def _poly_haven_fbx(files: Mapping[str, Any], *, resolution: str) -> dict[str, Any]:
    formats = files.get("fbx") if isinstance(files.get("fbx"), Mapping) else {}
    available = [resolution, "1k", "2k", "4k", "8k"]
    for candidate in dict.fromkeys(available):
        level = formats.get(candidate) if isinstance(formats.get(candidate), Mapping) else {}
        record = level.get("fbx") if isinstance(level.get("fbx"), Mapping) else None
        if record and record.get("url"):
            return dict(record)
    raise RemoteProviderError("provider_output_missing", "Poly Haven model has no FBX download", status="blocked")


def _poly_haven_size(metadata: Mapping[str, Any]) -> tuple[float, float, float] | None:
    values = metadata.get("dimensions")
    if not isinstance(values, list) or len(values) != 3:
        return None
    try:
        size = tuple(float(value) / 1000.0 for value in values)
    except (TypeError, ValueError):
        return None
    return size if all(value > 0 for value in size) else None


def _optional_size(generation_spec: Any) -> tuple[float, float, float] | None:
    values = generation_spec.get("size_m") if isinstance(generation_spec, Mapping) else None
    if not isinstance(values, list) or len(values) != 3:
        return None
    try:
        size = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        return None
    return size if all(value > 0 for value in size) else None


def _download_record(downloaded: Mapping[str, Any], *, role: str, file_format: str) -> dict[str, Any]:
    return {
        "role": role,
        "path": Path(str(downloaded["path"])),
        "format": file_format,
        "sha256": str(downloaded["sha256"]),
        "md5": str(downloaded.get("md5") or ""),
        "byte_size": int(downloaded["byte_size"]),
    }


def _safe_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RemoteProviderError("provider_dependency_path_invalid", f"unsafe provider dependency path: {value}")
    return path


def _safe_id(value: str) -> str:
    return "_".join(token for token in re.split(r"[^a-z0-9]+", str(value).casefold()) if token)


def _search_tokens(value: str) -> list[str]:
    stop = {"a", "an", "the", "asset", "mesh", "model", "3d", "for", "of", "with", "generated"}
    return [token for token in re.split(r"[^a-z0-9]+", str(value).casefold()) if token and token not in stop]


def _redact_signed_urls(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _redact_signed_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_signed_urls(item) for item in value]
    if isinstance(value, str) and value.startswith("https://") and urllib.parse.urlparse(value).query:
        parsed = urllib.parse.urlsplit(value)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "<redacted>", ""))
    return value


def _safe_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(4096).decode("utf-8", errors="replace")
    except OSError:
        return ""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:500]
    if isinstance(value, Mapping):
        return str(value.get("message") or value.get("error") or value)[:500]
    return str(value)[:500]
