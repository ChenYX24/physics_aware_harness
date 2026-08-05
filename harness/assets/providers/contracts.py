from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping


PROVIDER_REQUEST_SCHEMA = "harness_asset_provider_request_v1"
PROVIDER_RESULT_SCHEMA = "harness_asset_provider_result_v1"
PROVIDER_RECEIPT_SCHEMA = "harness_asset_provider_receipt_v1"
PROVIDER_BATCH_SCHEMA = "harness_asset_provider_batch_v1"
BACKEND_IMPORT_REQUEST_SCHEMA = "harness_backend_asset_import_request_v1"
BACKEND_IMPORT_RESULT_SCHEMA = "harness_backend_asset_import_result_v1"
PROVIDER_STATUSES = {"fulfilled", "blocked", "failed"}
SUCCESSFUL_LIFECYCLE = [
    "requested",
    "generated",
    "hashed_and_license_recorded",
    "normalized",
    "materialized",
    "imported",
    "registered",
    "qualified",
    "runtime_bound",
]


def stable_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_schema(data: Mapping[str, Any], expected: str) -> None:
    actual = data.get("schema_version")
    if actual != expected:
        raise ValueError(f"unsupported schema_version {actual!r}; expected {expected}")


def _required_text(data: Mapping[str, Any], field: str) -> str:
    value = str(data.get(field) or "").strip()
    if not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _required_sha256(data: Mapping[str, Any], field: str) -> str:
    value = _required_text(data, field).casefold()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field} must be a SHA-256 digest")
    return value


def _mapping(data: Mapping[str, Any], field: str) -> dict[str, Any]:
    value = data.get(field)
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return dict(value)


def _dict_list(data: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    value = data.get(field)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{field} must be a list of objects")
    return [dict(item) for item in value]


@dataclass(frozen=True)
class ProviderRequest:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ProviderRequest:
        data = dict(raw)
        _require_schema(data, PROVIDER_REQUEST_SCHEMA)
        for field in (
            "request_id",
            "request_digest",
            "case_id",
            "object_id",
            "slot",
            "route",
            "requirement",
            "origin",
            "target_backend",
            "required_license_tier",
        ):
            _required_text(data, field)
        _required_sha256(data, "request_digest")
        if data["route"] not in {
            "default",
            "local_catalog",
            "external_site",
            "procedural_generation",
            "model_generation",
        }:
            raise ValueError("route is invalid")
        if data["requirement"] not in {"preferred", "required"}:
            raise ValueError("requirement is invalid")
        _mapping(data, "search_intent")
        generation = _mapping(data, "generation_spec")
        for field in ("recipe_id", "recipe_version", "shape"):
            _required_text(generation, field)
        size = generation.get("size_m")
        if not isinstance(size, list) or len(size) != 3 or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0.0
            for value in size
        ):
            raise ValueError("generation_spec.size_m must contain three positive finite numbers")
        _dict_list(data, "reference_inputs")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


@dataclass(frozen=True)
class ProviderResult:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ProviderResult:
        data = dict(raw)
        _require_schema(data, PROVIDER_RESULT_SCHEMA)
        for field in ("request_id", "request_digest", "object_id", "slot", "status"):
            _required_text(data, field)
        _required_sha256(data, "request_digest")
        status = str(data["status"])
        if status not in PROVIDER_STATUSES:
            raise ValueError(f"invalid Provider result status: {status}")
        asset_ids = data.get("catalog_asset_ids")
        receipt_ids = data.get("receipt_ids")
        if not isinstance(asset_ids, list) or any(not str(value).strip() for value in asset_ids):
            raise ValueError("catalog_asset_ids must be a list of non-empty strings")
        if not isinstance(receipt_ids, list) or any(not str(value).strip() for value in receipt_ids):
            raise ValueError("receipt_ids must be a list of non-empty strings")
        if status == "fulfilled" and (not asset_ids or not receipt_ids):
            raise ValueError("fulfilled Provider results require Catalog asset IDs and receipt IDs")
        if status != "fulfilled":
            failure = _mapping(data, "failure")
            _required_text(failure, "code")
            _required_text(failure, "message")
            if not isinstance(failure.get("retriable"), bool):
                raise ValueError("failure.retriable must be boolean")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


@dataclass(frozen=True)
class ProviderReceipt:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ProviderReceipt:
        data = dict(raw)
        _require_schema(data, PROVIDER_RECEIPT_SCHEMA)
        for field in (
            "receipt_id",
            "provider_id",
            "provider_version",
            "request_id",
            "request_digest",
            "recipe_id",
            "recipe_version",
            "generator_source_version",
            "source_kind",
            "source_uri",
            "license",
            "importer_request_digest",
            "importer_result_digest",
        ):
            _required_text(data, field)
        for field in ("request_digest", "importer_request_digest", "importer_result_digest"):
            _required_sha256(data, field)
        _mapping(data, "recipe_parameters")
        inputs = _dict_list(data, "input_identities")
        for identity in inputs:
            _required_text(identity, "input_id")
            _required_sha256(identity, "sha256")
        outputs = _dict_list(data, "output_files")
        for output in outputs:
            for field in ("path", "role", "format", "sha256"):
                _required_text(output, field)
            _required_sha256(output, "sha256")
            if str(output["path"]).startswith("/"):
                raise ValueError("receipt output paths must be workspace-relative")
            if not isinstance(output.get("byte_size"), int) or output["byte_size"] < 0:
                raise ValueError("receipt output byte_size must be non-negative")
        transitions = data.get("lifecycle_transitions")
        if not isinstance(transitions, list) or not transitions:
            raise ValueError("lifecycle_transitions must be a non-empty list")
        if transitions != SUCCESSFUL_LIFECYCLE[: len(transitions)]:
            raise ValueError("lifecycle transitions are out of order")
        if data.get("status") == "fulfilled" and transitions != SUCCESSFUL_LIFECYCLE:
            raise ValueError("fulfilled receipt must contain the complete lifecycle")
        if data.get("status") not in PROVIDER_STATUSES:
            raise ValueError("receipt status is invalid")
        _mapping(data, "redistribution")
        binding = _mapping(data, "backend_binding")
        if data.get("status") == "fulfilled" and (
            not binding or binding.get("materialized") is not True or binding.get("runtime_ready") is not True
        ):
            raise ValueError("fulfilled receipt requires a materialized runtime-ready backend binding")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


@dataclass(frozen=True)
class ProviderBatch:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ProviderBatch:
        data = dict(raw)
        _require_schema(data, PROVIDER_BATCH_SCHEMA)
        _required_text(data, "case_id")
        requests = _dict_list(data, "requests")
        results = _dict_list(data, "results")
        for request in requests:
            ProviderRequest.from_dict(request)
        for result in results:
            ProviderResult.from_dict(result)
        receipt_ids = data.get("receipt_ids")
        if not isinstance(receipt_ids, list) or any(not str(value).strip() for value in receipt_ids):
            raise ValueError("receipt_ids must be a list of non-empty strings")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


@dataclass(frozen=True)
class BackendImportRequest:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> BackendImportRequest:
        data = dict(raw)
        _require_schema(data, BACKEND_IMPORT_REQUEST_SCHEMA)
        for field in ("request_id", "request_digest", "asset_id", "target_backend", "class_name"):
            _required_text(data, field)
        _required_sha256(data, "request_digest")
        _dict_list(data, "source_files")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


@dataclass(frozen=True)
class BackendImportResult:
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> BackendImportResult:
        data = dict(raw)
        _require_schema(data, BACKEND_IMPORT_RESULT_SCHEMA)
        for field in ("request_id", "request_digest", "asset_id", "status"):
            _required_text(data, field)
        _required_sha256(data, "request_digest")
        if data["status"] not in {"fulfilled", "blocked", "failed"}:
            raise ValueError("invalid backend importer status")
        if data["status"] == "fulfilled":
            for field in ("object_path", "class_name"):
                _required_text(data, field)
            if data.get("materialized") is not True or data.get("runtime_ready") is not True:
                raise ValueError("fulfilled importer result must be materialized and runtime-ready")
            if not _dict_list(data, "files"):
                raise ValueError("fulfilled importer result requires at least one file")
            _dict_list(data, "dependencies")
        else:
            failure = _mapping(data, "failure")
            _required_text(failure, "code")
            _required_text(failure, "message")
            if not isinstance(failure.get("retriable"), bool):
                raise ValueError("failure.retriable must be boolean")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


def provider_failure(
    request: Mapping[str, Any],
    *,
    status: str,
    code: str,
    message: str,
    retriable: bool = False,
    receipt_ids: list[str] | None = None,
) -> dict[str, Any]:
    return ProviderResult.from_dict(
        {
            "schema_version": PROVIDER_RESULT_SCHEMA,
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "object_id": request["object_id"],
            "slot": request["slot"],
            "status": status,
            "catalog_asset_ids": [],
            "receipt_ids": list(receipt_ids or []),
            "failure": {"code": code, "message": message, "retriable": retriable},
        }
    ).to_dict()
