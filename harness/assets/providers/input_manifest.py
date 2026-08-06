from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping


PROVIDER_INPUT_MANIFEST_SCHEMA = "harness_provider_input_manifest_v1"


class ProviderInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_provider_input_manifest(
    inputs: list[Mapping[str, Any]],
    *,
    workspace: str | Path,
    meshy_upload_authorized: bool = False,
) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in inputs:
        input_id = str(raw.get("input_id") or "").strip()
        if not input_id or input_id in seen:
            raise ProviderInputError("provider_input_id_invalid", "provider input IDs must be non-empty and unique")
        seen.add(input_id)
        if str(raw.get("kind") or "") != "image":
            raise ProviderInputError("provider_input_kind_unsupported", f"unsupported Provider input kind: {input_id}")
        path = Path(str(raw.get("local_path") or "")).expanduser().resolve()
        if not path.is_file():
            raise ProviderInputError("provider_input_missing", f"Provider input is not materialized: {input_id}")
        if meshy_upload_authorized:
            _require_within_workspace(path, root, input_id=input_id)
        sha256 = str(raw.get("sha256") or "").casefold()
        actual_sha256 = _sha256_file(path)
        if sha256 != actual_sha256:
            raise ProviderInputError("provider_input_hash_mismatch", f"Provider input hash mismatch: {input_id}")
        byte_size = int(raw.get("byte_size") or -1)
        if byte_size != path.stat().st_size:
            raise ProviderInputError("provider_input_size_mismatch", f"Provider input size changed: {input_id}")
        records.append(
            {
                "input_id": input_id,
                "kind": "image",
                "local_path": str(path),
                "mime_type": str(raw.get("mime_type") or ""),
                "sha256": actual_sha256,
                "byte_size": byte_size,
                "authorizations": {
                    "planning_llm_upload": raw.get("external_upload_authorized") is True,
                    "meshy_upload": bool(meshy_upload_authorized),
                },
            }
        )
    return {
        "schema_version": PROVIDER_INPUT_MANIFEST_SCHEMA,
        "workspace_root": str(root),
        "inputs": records,
    }


def bind_provider_reference_inputs(
    references: list[Mapping[str, Any]],
    manifest: Mapping[str, Any] | None,
    *,
    provider: str,
) -> list[dict[str, Any]]:
    if manifest is None:
        raise ProviderInputError(
            "provider_input_manifest_missing",
            f"{provider} references require a Provider input manifest",
        )
    if manifest.get("schema_version") != PROVIDER_INPUT_MANIFEST_SCHEMA:
        raise ProviderInputError("provider_input_manifest_invalid", "unsupported Provider input manifest schema")
    workspace = Path(str(manifest.get("workspace_root") or "")).expanduser().resolve()
    raw_inputs = manifest.get("inputs")
    if not isinstance(raw_inputs, list) or any(not isinstance(row, Mapping) for row in raw_inputs):
        raise ProviderInputError("provider_input_manifest_invalid", "Provider input manifest inputs must be objects")
    by_id = {str(row.get("input_id") or ""): row for row in raw_inputs}
    bound: list[dict[str, Any]] = []
    for reference in references:
        input_id = str(reference.get("input_id") or "").strip()
        row = by_id.get(input_id)
        if row is None:
            raise ProviderInputError("provider_input_unresolved", f"Provider input ID cannot be resolved: {input_id}")
        path = Path(str(row.get("local_path") or "")).expanduser().resolve()
        _require_within_workspace(path, workspace, input_id=input_id)
        if path.suffix.casefold() not in {".jpg", ".jpeg", ".png"} or not path.is_file():
            raise ProviderInputError("provider_input_invalid", f"Provider input is not a workspace JPG/PNG: {input_id}")
        actual_sha256 = _sha256_file(path)
        if actual_sha256 != str(row.get("sha256") or "").casefold():
            raise ProviderInputError("provider_input_hash_mismatch", f"Provider input hash mismatch: {input_id}")
        authorizations = row.get("authorizations") if isinstance(row.get("authorizations"), Mapping) else {}
        authorization_key = f"{provider}_upload"
        bound.append(
            {
                "input_id": input_id,
                "usage": list(reference.get("usage") or []),
                "allow_similarity_search": bool(reference.get("allow_similarity_search", True)),
                "local_path": str(path),
                "mime_type": str(row.get("mime_type") or ""),
                "sha256": actual_sha256,
                "byte_size": path.stat().st_size,
                "upload_authorized": authorizations.get(authorization_key) is True,
            }
        )
    return bound


def _require_within_workspace(path: Path, workspace: Path, *, input_id: str) -> None:
    try:
        path.relative_to(workspace)
    except (OSError, ValueError) as exc:
        raise ProviderInputError(
            "provider_input_outside_workspace",
            f"Provider input must be stored in the external workspace: {input_id}",
        ) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
