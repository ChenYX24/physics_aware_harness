from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import mimetypes
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Protocol

from harness.core.artifact_schema import read_json, write_json
from harness.core.harness_config import EffectiveHarnessConfig, endpoint_identity, load_harness_config
from harness.core.stage_result import artifact_ref, build_stage_result, failure_stage_result, write_stage_result
from harness.core.case_spec_v2 import (
    ACQUISITION_ROUTES,
    ASSET_MUST_FIELDS,
    ASSET_MUST_NOT_FIELDS,
    BACKEND_SOLVER_CAPABILITIES,
    CAMERA_ROLES,
    CASE_SPEC_V2_SCHEMA_VERSION,
    OBSERVATION_MODALITIES,
    REFERENCE_INPUT_USAGES,
    RESOURCE_KINDS,
    VERIFICATION_ASSERTION_TYPES,
    CaseSpecV2,
    CaseSpecV2ValidationError,
    ValidationIssue,
    asset_requests,
    case_spec_v2_from_dict,
    collect_case_spec_v2_issues,
    normalize_case_spec_v2,
    stable_case_spec_digest,
)
from harness.runtime.stage_contracts import BACKEND_STAGE_IO, stage_handoff_contract


REQUEST_SCHEMA_VERSION = "harness_case_request_v1"
EXPANSION_SCHEMA_VERSION = "harness_expansion_v1"
EXPANSION_FIELDS = (
    "request_summary",
    "capability_analysis",
    "scene_analysis",
    "object_analysis",
    "event_and_relation_analysis",
    "asset_analysis",
    "expected_behavior_analysis",
    "observation_analysis",
    "backend_constraints",
    "asset_source_constraints",
    "parameter_analysis",
    "ambiguities",
    "assumptions",
)
EXPANSION_ANALYSIS_LIST_FIELDS = (
    "object_analysis",
    "event_and_relation_analysis",
    "asset_analysis",
    "parameter_analysis",
    "ambiguities",
    "assumptions",
)
EXPANSION_LIST_FIELDS = (*EXPANSION_ANALYSIS_LIST_FIELDS, "asset_source_constraints")
EXPANSION_OBJECT_FIELDS = (
    "capability_analysis",
    "scene_analysis",
    "expected_behavior_analysis",
    "observation_analysis",
    "backend_constraints",
)
REQUESTED_BACKENDS = {"fallback", "genesis_fem", "genesis_sph", "taichi_cloth", "ue"}
GENERATION_CONTEXT_SCHEMA_VERSION = "harness_generation_context_v1"
PLANNING_IMAGE_RESERVATION_SCHEMA_VERSION = "harness_planning_image_reservation_v1"


@dataclass(frozen=True)
class LLMJSONResponse:
    payload: dict[str, Any]
    receipt: dict[str, Any]


class JSONCompletionClient(Protocol):
    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, Any],
        images: list[dict[str, Any]] | None = None,
        purpose: str,
    ) -> LLMJSONResponse:
        ...


class CaseGenerationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        request_identity: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.request_identity = request_identity


class _AuditedJSONClient:
    def __init__(self, client: JSONCompletionClient) -> None:
        self.client = client
        self.invocation_count = 0
        self.request_identities: list[str] = []

    def complete_json(self, **kwargs: Any) -> LLMJSONResponse:
        self.invocation_count += 1
        try:
            response = self.client.complete_json(**kwargs)
        except BaseException as exc:
            identity = getattr(exc, "request_identity", None)
            if isinstance(identity, str) and identity:
                self.request_identities.append(identity)
            raise
        identity = response.receipt.get("request_sha256")
        if isinstance(identity, str) and identity:
            self.request_identities.append(identity)
        return response


@dataclass(frozen=True)
class CaseGenerationResult:
    request: dict[str, Any]
    expansion: dict[str, Any]
    case_spec: CaseSpecV2
    llm_trace: dict[str, Any]
    stage_result: dict[str, Any] | None = None

    @property
    def repair_count(self) -> int:
        return int(self.llm_trace.get("repair_count") or 0)


class OpenAICompatibleJSONClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int = 180,
        effective_config: EffectiveHarnessConfig | None = None,
    ) -> None:
        config = effective_config
        if config is not None:
            self.base_url = str(base_url if base_url is not None else config.planning_base_url).rstrip("/")
            self.api_key = api_key if api_key is not None else config.planning_api_key()
            self.model = str(model if model is not None else config.planning_model).strip()
        else:
            self.base_url = str(
                base_url
                or os.environ.get("SIM_HARNESS_LLM_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or "https://api.openai.com/v1"
            ).rstrip("/")
            self.api_key = api_key or os.environ.get("SIM_HARNESS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
            self.model = str(model or os.environ.get("SIM_HARNESS_LLM_MODEL") or os.environ.get("OPENAI_MODEL") or "").strip()
        self.effective_config_digest = config.digest if config is not None else None
        self.planning_target_digest = config.planning_target_digest if config is not None else None
        self.timeout_seconds = int(timeout_seconds)

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, Any],
        images: list[dict[str, Any]] | None = None,
        purpose: str,
    ) -> LLMJSONResponse:
        if not self.model:
            raise CaseGenerationError(
                "llm_model_missing",
                "Set SIM_HARNESS_LLM_MODEL (or OPENAI_MODEL) for CaseSpec V2 generation.",
            )
        if self.base_url.startswith("https://api.openai.com/") and not self.api_key:
            raise CaseGenerationError(
                "llm_credentials_missing",
                "Set SIM_HARNESS_LLM_API_KEY (or OPENAI_API_KEY) for the configured LLM endpoint.",
            )
        content: str | list[dict[str, Any]] = json.dumps(user_payload, ensure_ascii=False)
        if images:
            content = [{"type": "text", "text": content}]
            for image in images:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": _image_data_url(image)},
                    }
                )
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_identity = hashlib.sha256(encoded).hexdigest()
        endpoint = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(endpoint, data=encoded, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            exc.read()
            retryable = exc.code in {408, 409, 425, 429} or exc.code >= 500
            raise CaseGenerationError(
                "llm_http_retriable" if retryable else "llm_http_error",
                f"LLM {purpose} request failed with HTTP {exc.code}",
                retryable=retryable,
                request_identity=request_identity,
            ) from exc
        except urllib.error.URLError as exc:
            raise CaseGenerationError(
                "llm_network_error",
                f"LLM {purpose} request failed: {exc.reason}",
                retryable=True,
                request_identity=request_identity,
            ) from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise CaseGenerationError("llm_response_invalid", f"LLM {purpose} response must be a JSON object")
            payload = _completion_payload(decoded)
        except BaseException as exc:
            setattr(exc, "request_identity", request_identity)
            raise
        receipt = {
            "schema_version": "harness_llm_call_receipt_v1",
            "purpose": purpose,
            "response_id": decoded.get("id"),
            "model": decoded.get("model") or self.model,
            "usage": decoded.get("usage") or {},
            "request_sha256": request_identity,
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "endpoint_kind": "openai_compatible_chat_completions",
            "endpoint_identity": endpoint_identity(self.base_url),
            "configured_model": self.model,
            "effective_config_digest": self.effective_config_digest,
            "planning_target_digest": self.planning_target_digest,
        }
        return LLMJSONResponse(payload=payload, receipt=receipt)


def build_case_request(
    *,
    case_id: str,
    text: str | None = None,
    image_paths: list[str | Path] | None = None,
    allow_image_upload: bool = False,
    planning_images_required: bool = False,
    requested_backend: str | None = None,
) -> dict[str, Any]:
    normalized_text = " ".join(str(text or "").split())
    paths = [Path(value).expanduser().resolve() for value in image_paths or []]
    if not normalized_text and not paths:
        raise ValueError("CaseSpec V2 generation requires text, at least one image, or both")
    images: list[dict[str, Any]] = []
    for index, path in enumerate(paths):
        if not path.is_file():
            raise FileNotFoundError(f"reference image does not exist: {path}")
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not mime_type.startswith("image/"):
            raise ValueError(f"reference input is not a recognized image: {path}")
        images.append(
            {
                "input_id": f"request_image_{index}",
                "kind": "image",
                "local_path": str(path),
                "mime_type": mime_type,
                "sha256": _sha256_file(path),
                "byte_size": path.stat().st_size,
                "external_upload_authorized": bool(allow_image_upload),
            }
        )
    backend = str(requested_backend or "").strip()
    if backend and backend not in REQUESTED_BACKENDS:
        raise ValueError(f"requested_backend must be one of {sorted(REQUESTED_BACKENDS)}")
    image_ids = [str(row["input_id"]) for row in images]
    if planning_images_required and not image_ids:
        raise ValueError("planning_images_required requires at least one image input")
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "case_id": str(case_id),
        "text": normalized_text,
        "inputs": images,
        "planning_image_requirement": {
            "mode": "required" if image_ids and (not normalized_text or planning_images_required) else "optional",
            "input_ids": image_ids,
        },
        "execution_constraints": {"requested_backend": backend or None},
    }


def generate_case_spec_v2(
    request: Mapping[str, Any],
    *,
    client: JSONCompletionClient | None = None,
    artifact_dir: str | Path | None = None,
    job_id: str | None = None,
    attempt_id: str | None = None,
    effective_config: EffectiveHarnessConfig | None = None,
) -> CaseGenerationResult:
    started = time.perf_counter()
    destination = Path(artifact_dir) if artifact_dir is not None else None
    config = effective_config or load_harness_config()
    audited_client = _AuditedJSONClient(client or OpenAICompatibleJSONClient(effective_config=config))
    try:
        result = _generate_case_spec_v2_impl(
            request,
            client=audited_client,
            artifact_dir=artifact_dir,
            job_id=job_id,
            effective_config=config,
        )
    except BaseException as exc:
        if isinstance(exc, CaseSpecV2ValidationError) and exc.issues:
            failure_code = exc.issues[0].code
        else:
            failure_code = str(getattr(exc, "code", "generation_unhandled_exception"))
        stage_result = failure_stage_result(
            stage="generation",
            failure_code=failure_code,
            message=str(exc) or type(exc).__name__,
            retryable=getattr(exc, "retryable", None),
            source_status="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else None,
            job_id=job_id,
            attempt_id=attempt_id,
            artifact_refs=(
                [artifact_ref("request", "request.json", REQUEST_SCHEMA_VERSION)]
                if destination is not None and (destination / "request.json").is_file()
                else []
            ),
            elapsed_seconds=time.perf_counter() - started,
            invocation_count=audited_client.invocation_count,
            request_identities=audited_client.request_identities,
        )
        if destination is not None:
            write_stage_result(destination, stage_result)
        raise
    calls = [value for value in result.llm_trace.get("calls") or [] if isinstance(value, Mapping)]
    request_identities = list(audited_client.request_identities)
    request_identities.extend(
        str(value.get("request_sha256"))
        for value in calls
        if isinstance(value.get("request_sha256"), str)
    )
    stage_result = build_stage_result(
        stage="generation",
        status="completed",
        job_id=job_id,
        attempt_id=attempt_id,
        artifact_refs=[
            artifact_ref("request", "request.json", REQUEST_SCHEMA_VERSION),
            artifact_ref("expansion", "expansion.json", EXPANSION_SCHEMA_VERSION),
            artifact_ref("case_spec", "case_spec_v2.json", CASE_SPEC_V2_SCHEMA_VERSION),
            artifact_ref("generation_trace", "case_generation_trace.json", "harness_case_generation_trace_v1"),
            artifact_ref("generation_context", "generation_context.json", GENERATION_CONTEXT_SCHEMA_VERSION),
        ],
        elapsed_seconds=time.perf_counter() - started,
        invocation_count=len(calls),
        request_identities=request_identities,
    )
    if destination is not None:
        write_stage_result(destination, stage_result)
    return replace(result, stage_result=stage_result)


def _generate_case_spec_v2_impl(
    request: Mapping[str, Any],
    *,
    client: JSONCompletionClient | None = None,
    artifact_dir: str | Path | None = None,
    job_id: str | None = None,
    effective_config: EffectiveHarnessConfig | None = None,
) -> CaseGenerationResult:
    validated_request = _validate_request(request)
    config = effective_config or load_harness_config()
    client = client or OpenAICompatibleJSONClient(effective_config=config)
    destination = Path(artifact_dir) if artifact_dir is not None else None
    image_decision = planning_image_decision(
        validated_request,
        upload_authorized=all(
            item.get("external_upload_authorized") is True
            for item in validated_request.get("inputs") or []
            if item.get("kind") == "image"
        ),
        image_capability=config.planning_image_capability,
    )
    if image_decision["status"] != "ready":
        raise CaseGenerationError(
            str(image_decision["failure_code"]),
            str(image_decision["message"]),
        )
    generation_context = _generation_context(
        validated_request,
        config=config,
        image_decision=image_decision,
        job_id=job_id,
    )
    if destination is not None:
        destination.mkdir(parents=True, exist_ok=True)
        request_path = destination / "request.json"
        if request_path.is_file() and read_json(request_path) != validated_request:
            raise ValueError("generation checkpoint request differs from the immutable job request")
        write_json(request_path, validated_request)
        _validate_or_write_generation_context(destination, generation_context, validated_request)
        completed_paths = (
            destination / "expansion.json",
            destination / "case_spec_v2.json",
            destination / "case_generation_trace.json",
        )
        if all(path.is_file() for path in completed_paths):
            expansion = read_json(completed_paths[0])
            trace = read_json(completed_paths[2])
            case_spec = case_spec_v2_from_dict(
                read_json(completed_paths[1]),
                available_input_ids=[str(item["input_id"]) for item in validated_request.get("inputs") or []],
            )
            return CaseGenerationResult(
                request=validated_request,
                expansion=expansion,
                case_spec=case_spec,
                llm_trace=trace,
            )
    images = [
        dict(item)
        for item in validated_request.get("inputs") or []
        if item.get("kind") == "image" and image_decision["mode"] == "uploaded"
    ]
    image_usage = {
        "mode": image_decision["mode"],
        "input_ids": [str(item["input_id"]) for item in images]
        if images
        else list(validated_request["planning_image_requirement"]["input_ids"]),
    }
    expansion_cached = bool(
        destination is not None
        and (destination / "expansion_call_receipt.json").is_file()
        and (
            (destination / "expansion.json").is_file()
            or (destination / "expansion_raw.json").is_file()
        )
    )
    if expansion_cached:
        expansion_source = (
            destination / "expansion.json"
            if (destination / "expansion.json").is_file()
            else destination / "expansion_raw.json"
        )
        expansion = _normalize_expansion(read_json(expansion_source))
        expansion_receipt = read_json(destination / "expansion_call_receipt.json")
        if not (destination / "expansion.json").is_file():
            write_json(destination / "expansion.json", expansion)
    else:
        expansion_payload = {
            "request": _request_for_model(validated_request),
            "planning_contract": {
                "executable_primary_capabilities": _executable_primary_capabilities(),
            },
            "expansion_contract": _expansion_contract(),
        }
        reservation = _reserve_unknown_image_call(
            destination,
            generation_context=generation_context,
            required=image_decision["probe_required"] is True,
        )
        try:
            expansion_response = client.complete_json(
                system_prompt=_expansion_system_prompt(),
                user_payload=expansion_payload,
                images=images,
                purpose="expansion",
            )
        except BaseException as exc:
            _finish_unknown_image_reservation(destination, reservation, status="unknown", error_code=str(getattr(exc, "code", "call_failed")))
            raise
        expansion_receipt = _contextual_receipt(
            expansion_response.receipt,
            generation_context=generation_context,
            purpose="expansion",
            input_payload=expansion_payload,
            output_payload=expansion_response.payload,
        )
        if destination is not None:
            write_json(destination / "expansion_raw.json", expansion_response.payload)
            write_json(destination / "expansion_call_receipt.json", expansion_receipt)
        _finish_unknown_image_reservation(destination, reservation, status="completed", output_digest=_stable_json_digest(expansion_response.payload))
        expansion = _normalize_expansion(expansion_response.payload)
        if destination is not None:
            write_json(destination / "expansion.json", expansion)
    generation_cached = bool(
        destination is not None
        and (destination / "case_spec_generation_raw.json").is_file()
        and (destination / "case_spec_generation_call_receipt.json").is_file()
    )
    if generation_cached:
        generation_payload = read_json(destination / "case_spec_generation_raw.json")
        generation_receipt = read_json(destination / "case_spec_generation_call_receipt.json")
    else:
        case_generation_payload = {
            "request": _request_for_model(validated_request),
            "expansion": expansion,
            "case_spec_contract": _case_spec_contract(),
        }
        generation_response = client.complete_json(
            system_prompt=_case_spec_system_prompt(),
            user_payload=case_generation_payload,
            images=None,
            purpose="case_spec_generation",
        )
        generation_payload = generation_response.payload
        generation_receipt = _contextual_receipt(
            generation_response.receipt,
            generation_context=generation_context,
            purpose="case_spec_generation",
            input_payload=case_generation_payload,
            output_payload=generation_payload,
        )
        if destination is not None:
            write_json(destination / "case_spec_generation_raw.json", generation_payload)
            write_json(destination / "case_spec_generation_call_receipt.json", generation_receipt)
    raw_case_spec = _unwrap_case_spec(generation_payload)
    raw_case_spec = _apply_request_identity(raw_case_spec, validated_request)
    receipts = [expansion_receipt, generation_receipt]
    repair_count = 0
    try:
        case_spec = _case_spec_from_generation(
            raw_case_spec,
            expansion=expansion,
            available_input_ids=[str(item["input_id"]) for item in validated_request.get("inputs") or []],
        )
    except CaseSpecV2ValidationError as validation_error:
        if destination is not None:
            write_json(destination / "case_spec_validation_errors.json", validation_error.to_dict())
        repair_cached = bool(
            destination is not None
            and (destination / "case_spec_repair_raw.json").is_file()
            and (destination / "case_spec_repair_call_receipt.json").is_file()
        )
        if repair_cached:
            repair_payload = read_json(destination / "case_spec_repair_raw.json")
            repair_receipt = read_json(destination / "case_spec_repair_call_receipt.json")
        else:
            repair_input = {
                "invalid_case_spec": normalize_case_spec_v2(raw_case_spec),
                "validation_errors": validation_error.to_dict(),
                "repair_constraints": {
                    "maximum_repairs": 1,
                    "preserve_user_intent": True,
                    "do_not_change_valid_fields_unless_required_by_an_error": True,
                    "requested_backend": str(
                        (validated_request.get("execution_constraints") or {}).get("requested_backend") or ""
                    ) or None,
                    "asset_source_constraints": copy.deepcopy(expansion.get("asset_source_constraints") or []),
                },
                "case_spec_contract": _case_spec_contract(),
            }
            repair_response = client.complete_json(
                system_prompt=_repair_system_prompt(),
                user_payload=repair_input,
                images=None,
                purpose="case_spec_validation_repair",
            )
            repair_payload = repair_response.payload
            repair_receipt = _contextual_receipt(
                repair_response.receipt,
                generation_context=generation_context,
                purpose="case_spec_validation_repair",
                input_payload=repair_input,
                output_payload=repair_payload,
            )
            if destination is not None:
                write_json(destination / "case_spec_repair_raw.json", repair_payload)
                write_json(destination / "case_spec_repair_call_receipt.json", repair_receipt)
        repair_count = 1
        receipts.append(repair_receipt)
        repaired = _apply_request_identity(_unwrap_case_spec(repair_payload), validated_request)
        try:
            case_spec = _case_spec_from_generation(
                repaired,
                expansion=expansion,
                available_input_ids=[str(item["input_id"]) for item in validated_request.get("inputs") or []],
            )
        except CaseSpecV2ValidationError as repair_error:
            if destination is not None:
                write_json(destination / "case_spec_repair_validation_errors.json", repair_error.to_dict())
            raise
    provenance = case_spec.data.setdefault("provenance", {})
    provenance["case_generation"] = {
        "workflow": "expansion_then_single_case_spec_with_one_bounded_repair_v1",
        "expansion_digest": stable_case_spec_digest(expansion),
        "llm_calls": receipts,
        "repair_count": repair_count,
        "execution_constraints": copy.deepcopy(validated_request.get("execution_constraints") or {}),
        "asset_source_constraints": copy.deepcopy(expansion.get("asset_source_constraints") or []),
        "planning_image_usage": image_usage,
        "generation_context": generation_context,
    }
    if destination is not None:
        write_json(destination / "case_spec_v2.json", case_spec.data)
        write_json(
            destination / "case_generation_trace.json",
            {
                "schema_version": "harness_case_generation_trace_v1",
                "normal_call_count": 2,
                "repair_count": repair_count,
                "calls": receipts,
                "planning_image_usage": image_usage,
                "generation_context": generation_context,
            },
        )
    return CaseGenerationResult(
        request=validated_request,
        expansion=expansion,
        case_spec=case_spec,
        llm_trace={
            "schema_version": "harness_case_generation_trace_v1",
            "normal_call_count": 2,
            "repair_count": repair_count,
            "calls": receipts,
            "planning_image_usage": image_usage,
            "generation_context": generation_context,
        },
    )


def planning_image_decision(
    request: Mapping[str, Any],
    *,
    upload_authorized: bool,
    image_capability: str,
) -> dict[str, Any]:
    requirement = normalize_planning_image_requirement(request)
    input_ids = list(requirement["input_ids"])
    mode = "none" if not input_ids else requirement["mode"]
    if image_capability not in {"supported", "unsupported", "unknown"}:
        raise ValueError("planning image capability is invalid")
    if mode == "none":
        return {"status": "ready", "mode": "none", "probe_required": False, "requirement": "none"}
    if mode == "optional":
        return {"status": "ready", "mode": "metadata_only", "probe_required": False, "requirement": "optional"}
    if not upload_authorized:
        return {
            "status": "blocked_user_action",
            "failure_code": "planning_image_upload_authorization_missing",
            "message": "authorize upload of the required image inputs to the planning model",
            "mode": None,
            "probe_required": False,
            "requirement": "required",
        }
    if image_capability == "unsupported":
        return {
            "status": "blocked_configuration",
            "failure_code": "planning_image_input_unsupported",
            "message": "the configured planning target does not support required image input",
            "mode": None,
            "probe_required": False,
            "requirement": "required",
        }
    return {
        "status": "ready",
        "mode": "uploaded",
        "probe_required": image_capability == "unknown",
        "requirement": "required",
    }


def _generation_context(
    request: Mapping[str, Any],
    *,
    config: EffectiveHarnessConfig,
    image_decision: Mapping[str, Any],
    job_id: str | None,
) -> dict[str, Any]:
    images = [
        {
            "input_id": item.get("input_id"),
            "sha256": item.get("sha256"),
            "mime_type": item.get("mime_type"),
            "byte_size": item.get("byte_size"),
        }
        for item in request.get("inputs") or []
        if item.get("kind") == "image"
    ]
    requirement = normalize_planning_image_requirement(request)
    authorization = all(
        item.get("external_upload_authorized") is True
        for item in request.get("inputs") or []
        if item.get("kind") == "image"
    )
    return {
        "schema_version": GENERATION_CONTEXT_SCHEMA_VERSION,
        "job_id": job_id,
        "effective_config_digest": config.digest,
        "planning_target_digest": config.planning_target_digest,
        "planning_endpoint_identity": endpoint_identity(config.planning_base_url),
        "planning_model": config.planning_model,
        "planning_image_requirement": "none" if not requirement["input_ids"] else requirement["mode"],
        "planning_image_capability": config.planning_image_capability,
        "planning_image_authorization": authorization,
        "planning_image_usage_mode": image_decision["mode"],
        "input_image_digest": _stable_json_digest(images),
        "input_images": images,
    }


def _validate_or_write_generation_context(
    destination: Path,
    desired: Mapping[str, Any],
    request: Mapping[str, Any],
) -> None:
    path = destination / "generation_context.json"
    if path.is_file():
        existing = read_json(path)
        if existing != desired:
            raise CaseGenerationError(
                "generation_cache_context_mismatch",
                "generation cache identity differs from the current effective configuration or image usage mode",
            )
        return
    cached = any(
        (destination / name).is_file()
        for name in (
            "expansion.json",
            "expansion_call_receipt.json",
            "case_spec_generation_raw.json",
            "case_generation_trace.json",
        )
    )
    if cached:
        recovered_mode = _recover_legacy_image_usage(destination, request)
        if recovered_mode is None:
            raise CaseGenerationError(
                "legacy_generation_cache_image_mode_unknown",
                "legacy generation cache does not uniquely identify whether images were uploaded",
            )
        if recovered_mode != desired.get("planning_image_usage_mode"):
            raise CaseGenerationError(
                "generation_cache_context_mismatch",
                "legacy generation cache image mode differs from the current image decision",
            )
    write_json(path, dict(desired))


def _recover_legacy_image_usage(destination: Path, request: Mapping[str, Any]) -> str | None:
    trace_path = destination / "case_generation_trace.json"
    if trace_path.is_file():
        usage = read_json(trace_path).get("planning_image_usage")
        if isinstance(usage, Mapping) and usage.get("mode") in {"none", "metadata_only", "uploaded"}:
            return str(usage["mode"])
        return None
    cached_request_path = destination / "request.json"
    if not cached_request_path.is_file():
        return None
    cached_request = read_json(cached_request_path)
    if cached_request != request:
        return None
    images = [item for item in request.get("inputs") or [] if item.get("kind") == "image"]
    if not images:
        return "none"
    # Before generation_context_v1 the implementation deterministically sent
    # every image whose immutable request record carried upload authorization.
    return "uploaded" if all(item.get("external_upload_authorized") is True for item in images) else "metadata_only"


def _reserve_unknown_image_call(
    destination: Path | None,
    *,
    generation_context: Mapping[str, Any],
    required: bool,
) -> dict[str, Any] | None:
    if not required:
        return None
    identity = {
        "job_id": generation_context.get("job_id"),
        "planning_target_digest": generation_context["planning_target_digest"],
        "input_image_digest": generation_context["input_image_digest"],
        "generation_context_digest": _stable_json_digest(generation_context),
    }
    if destination is None:
        return {
            "schema_version": PLANNING_IMAGE_RESERVATION_SCHEMA_VERSION,
            **identity,
            "state": "started",
            "attempt_count": 1,
        }
    path = destination / "planning_image_reservation.json"
    if path.exists():
        existing = read_json(path)
        if any(existing.get(key) != value for key, value in identity.items()):
            raise CaseGenerationError(
                "planning_image_probe_reservation_mismatch",
                "existing planning image reservation has a different immutable identity",
            )
        raise CaseGenerationError(
            "planning_image_probe_already_consumed",
            "the unknown-capability image probe was already reserved; refusing a second image call",
        )
    reservation = {
        "schema_version": PLANNING_IMAGE_RESERVATION_SCHEMA_VERSION,
        **identity,
        "state": "reserved",
        "attempt_count": 1,
        "output_digest": None,
        "error_code": None,
    }
    write_json(path, reservation)
    reservation["state"] = "started"
    write_json(path, reservation)
    return reservation


def _finish_unknown_image_reservation(
    destination: Path | None,
    reservation: Mapping[str, Any] | None,
    *,
    status: str,
    output_digest: str | None = None,
    error_code: str | None = None,
) -> None:
    if reservation is None:
        return
    updated = dict(reservation)
    updated.update({"state": status, "output_digest": output_digest, "error_code": error_code})
    if destination is not None:
        write_json(destination / "planning_image_reservation.json", updated)


def _contextual_receipt(
    receipt: Mapping[str, Any],
    *,
    generation_context: Mapping[str, Any],
    purpose: str,
    input_payload: Mapping[str, Any],
    output_payload: Mapping[str, Any],
) -> dict[str, Any]:
    value = copy.deepcopy(dict(receipt))
    value.update(
        {
            "purpose": purpose,
            "effective_config_digest": generation_context["effective_config_digest"],
            "planning_target_digest": generation_context["planning_target_digest"],
            "endpoint_identity": generation_context["planning_endpoint_identity"],
            "configured_model": generation_context["planning_model"],
            "planning_image_usage_mode": generation_context["planning_image_usage_mode"],
            "input_digest": _stable_json_digest(input_payload),
            "output_digest": _stable_json_digest(output_payload),
        }
    )
    return value


def _stable_json_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_request(request: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(request)
    if data.get("schema_version") != REQUEST_SCHEMA_VERSION:
        raise ValueError(f"request schema_version must be {REQUEST_SCHEMA_VERSION}")
    if not str(data.get("case_id") or "").strip():
        raise ValueError("request case_id must be non-empty")
    inputs = data.get("inputs") or []
    if not isinstance(inputs, list) or any(not isinstance(item, dict) for item in inputs):
        raise ValueError("request inputs must be a list of objects")
    input_ids = [str(item.get("input_id") or "") for item in inputs]
    if any(not value for value in input_ids) or len(input_ids) != len(set(input_ids)):
        raise ValueError("request input_id values must be non-empty and unique")
    if not str(data.get("text") or "").strip() and not inputs:
        raise ValueError("request requires text or inputs")
    data["planning_image_requirement"] = normalize_planning_image_requirement(data)
    constraints = data.get("execution_constraints") or {}
    if not isinstance(constraints, Mapping):
        raise ValueError("request execution_constraints must be an object")
    requested_backend = constraints.get("requested_backend")
    if requested_backend is not None and requested_backend not in REQUESTED_BACKENDS:
        raise ValueError(f"request requested_backend must be one of {sorted(REQUESTED_BACKENDS)}")
    data["execution_constraints"] = dict(constraints)
    return data


def normalize_planning_image_requirement(request: Mapping[str, Any]) -> dict[str, Any]:
    inputs = request.get("inputs") or []
    if not isinstance(inputs, list):
        raise ValueError("request inputs must be a list")
    image_ids = [
        str(item.get("input_id") or "")
        for item in inputs
        if isinstance(item, Mapping) and item.get("kind") == "image"
    ]
    raw = request.get("planning_image_requirement")
    if raw is None:
        return {
            "mode": "required" if image_ids and not str(request.get("text") or "").strip() else "optional",
            "input_ids": image_ids,
        }
    if not isinstance(raw, Mapping) or set(raw) != {"mode", "input_ids"}:
        raise ValueError("planning_image_requirement must contain only mode and input_ids")
    mode = raw.get("mode")
    input_ids = raw.get("input_ids")
    if mode not in {"required", "optional"}:
        raise ValueError("planning_image_requirement.mode must be required or optional")
    if (
        not isinstance(input_ids, list)
        or any(not isinstance(value, str) or not value for value in input_ids)
        or len(input_ids) != len(set(input_ids))
        or set(input_ids) != set(image_ids)
    ):
        raise ValueError("planning_image_requirement.input_ids must identify every image input exactly once")
    if mode == "required" and not input_ids:
        raise ValueError("required planning images must identify at least one image input")
    if image_ids and not str(request.get("text") or "").strip() and mode != "required":
        raise ValueError("image-only requests require every image for planning")
    return {"mode": str(mode), "input_ids": list(input_ids)}


def _request_for_model(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": request.get("schema_version"),
        "case_id": request.get("case_id"),
        "text": request.get("text"),
        "inputs": [
            {
                "input_id": item.get("input_id"),
                "kind": item.get("kind"),
                "mime_type": item.get("mime_type"),
                "sha256": item.get("sha256"),
            }
            for item in request.get("inputs") or []
        ],
        "planning_image_requirement": copy.deepcopy(request["planning_image_requirement"]),
        "execution_constraints": dict(request.get("execution_constraints") or {}),
    }


def _normalize_expansion(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("expansion") if isinstance(payload.get("expansion"), Mapping) else payload
    expansion = copy.deepcopy(dict(raw))
    expansion["schema_version"] = EXPANSION_SCHEMA_VERSION
    for field in EXPANSION_FIELDS:
        if field not in expansion:
            expansion[field] = [] if field in EXPANSION_LIST_FIELDS else {}
    if not isinstance(expansion.get("request_summary"), str):
        raise ValueError("expansion.request_summary must be a string")
    for field in EXPANSION_ANALYSIS_LIST_FIELDS:
        value = expansion.get(field)
        if isinstance(value, Mapping):
            expansion[field] = _analysis_mapping_to_list(value)
        elif not isinstance(value, list):
            raise ValueError(f"expansion.{field} must be a list")
    for field in EXPANSION_OBJECT_FIELDS:
        if not isinstance(expansion.get(field), dict):
            raise ValueError(f"expansion.{field} must be an object")
    for constraint in expansion.get("asset_source_constraints") or []:
        if isinstance(constraint, dict) and constraint.get("requirement") == "required":
            # Required sources are exclusive. The raw model response remains
            # persisted for audit; the normalized planning contract enforces
            # the stricter no-fallback meaning used by CaseSpec generation.
            constraint["fallback_order"] = []
    _validate_asset_source_constraints(expansion)
    _validate_parameter_analysis(expansion)
    return expansion


def _validate_parameter_analysis(expansion: Mapping[str, Any]) -> None:
    rows = expansion.get("parameter_analysis")
    if not isinstance(rows, list):
        raise ValueError("expansion.parameter_analysis must be a list")
    seen: set[str] = set()
    for index, row in enumerate(rows):
        path = f"expansion.parameter_analysis[{index}]"
        if not isinstance(row, Mapping) or set(row) != {
            "path",
            "requirement_level",
            "reason",
            "constraint",
        }:
            raise ValueError(f"{path} must contain path, requirement_level, reason, and constraint")
        case_path = row.get("path")
        if not isinstance(case_path, str) or not re.fullmatch(r"\$\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", case_path):
            raise ValueError(
                f"{path}.path={case_path!r} must be an exact CaseSpec leaf dot path; "
                "brackets, numeric indices, wildcards, selectors, and ranges are invalid"
            )
        if case_path in seen:
            raise ValueError(f"{path}.path must be unique")
        seen.add(case_path)
        level = row.get("requirement_level")
        if level not in {"hard", "soft", "inferred"}:
            raise ValueError(f"{path}.requirement_level is invalid")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError(f"{path}.reason must be non-empty")
        constraint = row.get("constraint")
        if level == "hard":
            if constraint is not None:
                raise ValueError(f"{path}.constraint must be null for a hard requirement")
            continue
        if not isinstance(constraint, Mapping):
            raise ValueError(f"{path}.constraint is required for an automatically adjustable parameter")
        kind = constraint.get("kind")
        if kind == "numeric":
            if set(constraint) != {"kind", "min", "max"}:
                raise ValueError(f"{path}.constraint numeric fields are invalid")
            minimum, maximum = constraint.get("min"), constraint.get("max")
            if (
                not isinstance(minimum, (int, float))
                or isinstance(minimum, bool)
                or not isinstance(maximum, (int, float))
                or isinstance(maximum, bool)
                or not math.isfinite(float(minimum))
                or not math.isfinite(float(maximum))
                or minimum > maximum
            ):
                raise ValueError(f"{path}.constraint numeric range is invalid")
        elif kind == "list":
            if set(constraint) != {"kind", "min_items", "max_items"}:
                raise ValueError(f"{path}.constraint list fields are invalid")
            minimum, maximum = constraint.get("min_items"), constraint.get("max_items")
            if (
                not isinstance(minimum, int)
                or isinstance(minimum, bool)
                or not isinstance(maximum, int)
                or isinstance(maximum, bool)
                or minimum < 0
                or minimum > maximum
            ):
                raise ValueError(f"{path}.constraint list range is invalid")
        elif kind == "enum":
            if set(constraint) != {"kind", "values"} or not isinstance(constraint.get("values"), list) or not constraint["values"]:
                raise ValueError(f"{path}.constraint enum values are invalid")
            if any(isinstance(value, (dict, list)) for value in constraint["values"]):
                raise ValueError(f"{path}.constraint enum values must be JSON scalars")
        else:
            raise ValueError(f"{path}.constraint kind is invalid")


def _validate_asset_source_constraints(expansion: Mapping[str, Any]) -> None:
    constraints = expansion.get("asset_source_constraints")
    if not isinstance(constraints, list):
        raise ValueError("expansion.asset_source_constraints must be a list")
    suggested_ids = {
        str(item.get("suggested_id") or "").strip()
        for item in expansion.get("object_analysis") or []
        if isinstance(item, Mapping) and str(item.get("suggested_id") or "").strip()
    }
    for index, constraint in enumerate(constraints):
        path = f"expansion.asset_source_constraints[{index}]"
        if not isinstance(constraint, Mapping):
            raise ValueError(f"{path} must be an object")
        scope = constraint.get("scope")
        if not isinstance(scope, Mapping):
            raise ValueError(f"{path}.scope must be an object")
        object_ids = _nonempty_unique_strings(scope.get("object_ids"), f"{path}.scope.object_ids")
        unknown_ids = [object_id for object_id in object_ids if object_id not in suggested_ids]
        if unknown_ids:
            raise ValueError(f"{path}.scope.object_ids references unknown suggested object IDs: {', '.join(unknown_ids)}")
        allowed_routes = _nonempty_unique_strings(constraint.get("allowed_routes"), f"{path}.allowed_routes")
        invalid_routes = [route for route in allowed_routes if route not in ACQUISITION_ROUTES]
        if invalid_routes:
            raise ValueError(f"{path}.allowed_routes contains invalid routes: {', '.join(invalid_routes)}")
        allowed_providers = _unique_strings(
            constraint.get("allowed_providers", []),
            f"{path}.allowed_providers",
        )
        fallback_order = _unique_strings(constraint.get("fallback_order"), f"{path}.fallback_order")
        canonical_allowed_providers = {_canonical_provider_id(value) for value in allowed_providers}
        unknown_fallbacks = [
            provider
            for provider in fallback_order
            if _canonical_provider_id(provider) not in canonical_allowed_providers
        ]
        if allowed_providers and unknown_fallbacks:
            raise ValueError(f"{path}.fallback_order contains providers outside allowed_providers: {', '.join(unknown_fallbacks)}")
        if constraint.get("requirement") not in {"preferred", "required"}:
            raise ValueError(f"{path}.requirement must be preferred or required")
        if not isinstance(constraint.get("allow_proxy"), bool):
            raise ValueError(f"{path}.allow_proxy must be a boolean")


def _nonempty_unique_strings(value: Any, path: str) -> list[str]:
    values = _unique_strings(value, path)
    if not values:
        raise ValueError(f"{path} must contain at least one value")
    return values


def _unique_strings(value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{path} must be a list of non-empty strings")
    values = [str(item).strip() for item in value]
    if len(values) != len(set(values)):
        raise ValueError(f"{path} values must be unique")
    return values


def _case_spec_from_generation(
    data: Mapping[str, Any],
    *,
    expansion: Mapping[str, Any],
    available_input_ids: list[str],
) -> CaseSpecV2:
    normalized = normalize_case_spec_v2(data)
    issues = collect_case_spec_v2_issues(
        normalized,
        available_input_ids=available_input_ids,
    )
    issues.extend(_asset_source_constraint_issues(expansion, normalized))
    if issues:
        raise CaseSpecV2ValidationError(issues)
    return CaseSpecV2(normalized)


def _asset_source_constraint_issues(
    expansion: Mapping[str, Any],
    case_spec: Mapping[str, Any],
) -> list[ValidationIssue]:
    constraints = expansion.get("asset_source_constraints") or []
    objects = {
        str(obj.get("id") or ""): (index, obj)
        for index, obj in enumerate(case_spec.get("objects") or [])
        if isinstance(obj, Mapping) and obj.get("id")
    }
    issues: list[ValidationIssue] = []
    for constraint_index, constraint in enumerate(constraints):
        if not isinstance(constraint, Mapping):
            continue
        scope = constraint.get("scope") if isinstance(constraint.get("scope"), Mapping) else {}
        allowed_routes = {str(value) for value in constraint.get("allowed_routes") or []}
        allowed_providers = {_canonical_provider_id(value) for value in constraint.get("allowed_providers") or []}
        required = constraint.get("requirement") == "required"
        no_proxy = constraint.get("allow_proxy") is False
        for object_id in scope.get("object_ids") or []:
            entry = objects.get(str(object_id))
            if entry is None:
                issues.append(
                    ValidationIssue(
                        path="/objects",
                        code="asset_source_scope_object_missing",
                        message=(
                            f"asset_source_constraints/{constraint_index} references missing object ID: {object_id}"
                        ),
                    )
                )
                continue
            object_index, obj = entry
            path = f"/objects/{object_index}/asset"
            requests = asset_requests(obj.get("asset"))
            if not requests:
                issues.append(
                    ValidationIssue(
                        path=path,
                        code="asset_source_constraint_missing_request",
                        message=f"object {object_id} requires an asset acquisition matching Expansion constraint {constraint_index}",
                    )
                )
                continue
            acquisition = requests[0].get("acquisition")
            acquisition = acquisition if isinstance(acquisition, Mapping) else {}
            acquisition_path = f"{path}/acquisition"
            route = str(acquisition.get("route") or "default")
            if route not in allowed_routes:
                issues.append(
                    ValidationIssue(
                        path=f"{acquisition_path}/route",
                        code="asset_source_route_mismatch",
                        message=(
                            f"object {object_id} route {route!r} is outside Expansion allowed_routes "
                            f"{sorted(allowed_routes)}"
                        ),
                    )
                )
            provider_hint = str(acquisition.get("provider_hint") or "").strip()
            provider = _canonical_provider_id(provider_hint or _default_provider_for_route(route))
            if allowed_providers and (not provider or provider not in allowed_providers):
                issues.append(
                    ValidationIssue(
                        path=f"{acquisition_path}/provider_hint",
                        code="asset_source_provider_mismatch",
                        message=(
                            f"object {object_id} provider {provider_hint or '<unspecified>'!r} is outside "
                            f"Expansion allowed_providers {sorted(allowed_providers)}"
                        ),
                    )
                )
            expected_route = _route_for_provider(provider)
            if expected_route and route != expected_route:
                issues.append(
                    ValidationIssue(
                        path=f"{acquisition_path}/provider_hint",
                        code="asset_provider_route_mismatch",
                        message=f"object {object_id} provider {provider!r} is not compatible with route {route!r}",
                    )
                )
            if required and acquisition.get("requirement") != "required":
                issues.append(
                    ValidationIssue(
                        path=f"{acquisition_path}/requirement",
                        code="explicit_asset_source_not_required",
                        message=f"object {object_id} must preserve Expansion requirement=required",
                    )
                )
            if required and acquisition.get("origin") != "user_explicit":
                issues.append(
                    ValidationIssue(
                        path=f"{acquisition_path}/origin",
                        code="explicit_asset_source_origin_lost",
                        message=f"object {object_id} must preserve Expansion origin=user_explicit",
                    )
                )
            fallback_routes = [str(value) for value in acquisition.get("fallback_order") or []]
            unauthorized_fallbacks = [value for value in fallback_routes if value not in allowed_routes]
            if unauthorized_fallbacks or (required and no_proxy and fallback_routes):
                issues.append(
                    ValidationIssue(
                        path=f"{acquisition_path}/fallback_order",
                        code="unauthorized_asset_source_fallback",
                        message=(
                            f"object {object_id} has fallback routes not authorized by its required no-proxy "
                            f"Expansion constraint: {fallback_routes}"
                        ),
                    )
                )
    return issues


def _canonical_provider_id(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")
    return {
        "polyhaven": "poly_haven",
        "poly_haven_v1": "poly_haven",
        "poly_haven_external_site_v1": "poly_haven",
        "meshy_v1": "meshy",
        "meshy_model_generation_v1": "meshy",
    }.get(normalized, normalized)


def _default_provider_for_route(route: str) -> str:
    return {
        "external_site": "poly_haven",
        "model_generation": "meshy",
    }.get(str(route), "")


def _route_for_provider(provider: str) -> str:
    return {
        "poly_haven": "external_site",
        "meshy": "model_generation",
        "box_mesh_v1": "procedural_generation",
        "sphere_mesh_v1": "procedural_generation",
        "cylinder_mesh_v1": "procedural_generation",
    }.get(provider, "")


def _analysis_mapping_to_list(value: Mapping[str, Any]) -> list[Any]:
    data = dict(value)
    if not data:
        return []
    if len(data) == 1:
        wrapped = next(iter(data.values()))
        if isinstance(wrapped, list):
            return list(wrapped)
    if all(isinstance(item, Mapping) for item in data.values()):
        return [
            {"analysis_key": str(key), **dict(item)}
            for key, item in data.items()
        ]
    return [data]


def apply_case_request_identity(case_spec: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(case_spec)
    result["schema_version"] = CASE_SPEC_V2_SCHEMA_VERSION
    identity = dict(result.get("identity")) if isinstance(result.get("identity"), Mapping) else {}
    identity["case_id"] = str(request["case_id"])
    identity["source_request"] = str(request.get("text") or identity.get("source_request") or "")
    result["identity"] = identity
    requested_backend = str(
        (request.get("execution_constraints") or {}).get("requested_backend") or ""
    ).strip()
    if requested_backend:
        backend = dict(result.get("backend_constraints")) if isinstance(result.get("backend_constraints"), Mapping) else {}
        backend["allowed_solvers"] = [requested_backend]
        render_backend = str(backend.get("render_backend") or requested_backend)
        if render_backend != requested_backend and stage_handoff_contract(requested_backend, render_backend) is None:
            render_backend = requested_backend
        backend["render_backend"] = render_backend
        backend["allow_multi_backend"] = render_backend != requested_backend
        result["backend_constraints"] = backend
    return result


def _apply_request_identity(case_spec: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    """Compatibility alias for the legacy fixed-LLM generation path."""
    return apply_case_request_identity(case_spec, request)


def _unwrap_case_spec(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("case_spec") if isinstance(payload.get("case_spec"), Mapping) else payload
    return dict(value)


def _completion_payload(response: Mapping[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise RuntimeError("LLM response has no choices[0]")
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise RuntimeError("LLM response has no message")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, Mapping) and item.get("type") in {"text", "output_text"}
        )
    if isinstance(content, Mapping):
        return dict(content)
    if not isinstance(content, str):
        raise RuntimeError("LLM response message.content must be JSON text")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise RuntimeError("LLM JSON content must be an object")
    return payload


def _image_data_url(image: Mapping[str, Any]) -> str:
    if image.get("external_upload_authorized") is not True:
        raise ValueError(f"image upload is not authorized: {image.get('input_id')}")
    path = Path(str(image.get("local_path") or ""))
    if not path.is_file() or _sha256_file(path) != image.get("sha256"):
        raise ValueError(f"image input changed after request capture: {path}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{image.get('mime_type')};base64,{encoded}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _harness_mission_context() -> str:
    return """MISSION AND SYSTEM CONTEXT
You are part of the Physics-Aware Harness. The Harness turns a user's natural-language request and
optional reference images into an executable physics-simulation case. A registered engine such as
Unreal Engine (UE), Genesis, Taichi, or the deterministic fallback then simulates the case, renders
physics video and sensor modalities, and produces machine-checkable evidence for physics verifiers.

You are a planning component, not the simulator or renderer. Describe intent and requirements; never
pretend that an asset was found, generated, licensed, hashed, imported, registered, simulated, or
rendered. Later deterministic stages perform backend planning, Provider acquisition or generation,
Catalog registration and qualification, exactly one Asset Resolve, scene layout, runtime binding,
engine execution, video capture, and verification.

AUTHORITY AND SAFETY
- Preserve explicit user requirements. Do not silently change a required asset route or requested backend.
- Treat request.execution_constraints.requested_backend as authoritative when non-null.
- An image may be used only through its supplied input_id and declared usage. Do not invent image IDs.
- Do not infer permission to upload an image, spend money, call a paid service, scrape a website, or
  redistribute an asset.
- Do not invent licenses, provenance, hashes, dependencies, Catalog IDs, UE object paths, runtime-ready
  claims, rendered files, or verifier results.
- Use SI units in semantic planning: meters, seconds, kilograms, and radians unless a field says otherwise.
"""


def _expansion_system_prompt() -> str:
    return _harness_mission_context() + """

YOUR ROLE: EXPANSION
Analyze the request before CaseSpec generation. Resolve what the user means, identify what must be
represented, and expose uncertainty. Do not produce a CaseSpec, runtime coordinates, exact camera
poses, backend stages, implementation code, UE paths, or files. Return every required field even when
its value is empty.

FIELD-BY-FIELD INSTRUCTIONS
1. request_summary: one concise string stating the requested physical phenomenon, scene, requested
   output, and any explicit local-preview/reference intent.
2. capability_analysis: an object describing the physical invariant to execute and the best primary
   capability from planning_contract.executable_primary_capabilities. Do not invent capability IDs.
3. scene_analysis: an object describing the semantic environment, scale, coordinate assumptions,
   approximate duration, and necessary support objects. Keep it engine-neutral.
4. object_analysis: an array with one object per distinct logical physical object. For each, propose
   a stable machine-friendly suggested_id, semantic role, geometry and dimensions, body behavior,
   material/physics needs, initial-state intent, and whether it requires an asset. Never split one
   physical object into separate visual-mesh and collision-proxy object IDs. Its visual asset and
   simplified collision geometry belong to the same logical object; describe both needs on that item.
5. event_and_relation_analysis: an array of temporal events and relations among proposed objects, such
   as falling, contact, collision order, support, attachment, fracture, or settling. Refer to proposed
   object IDs consistently.
6. asset_analysis: an array with one entry per asset need. Separate the logical object from its asset.
   State whether the need is satisfied by default/local Catalog retrieval, external_site acquisition,
   procedural_generation, or model_generation. Preserve explicit routes; inferred routes are soft.
   Prefer procedural_generation for simple rule-based primitives that can be described exactly as a
   box, sphere, or z-axis cylinder; plates/walls are thin boxes and rods/poles/columns/discs are cylinders.
7. asset_source_constraints: an array of auditable hard source constraints extracted only from explicit
   user requirements. Each entry scopes exact object_analysis suggested_id values, lists every allowed
   acquisition route and, only when the user names Providers, every allowed structured Provider ID; preserves Provider fallback order, records
   requirement as preferred or required, and records allow_proxy. A required constraint always has an
   empty fallback_order; the selected Provider belongs in allowed_providers, not fallback_order. Different object groups may use different
   constraints. An omitted or empty allowed_providers means the user constrained the route but did not
   constrain the Provider. Do not infer a required route, Provider permission, or no-proxy rule from a mere preference.
8. expected_behavior_analysis: an object describing observable preconditions, event ordering, causal
   response, and postconditions without claiming that the run passed.
9. observation_analysis: an object describing useful camera roles, modalities (RGB/depth/segmentation),
   solver signals, and what evidence is needed. Do not emit exact camera transforms.
10. backend_constraints: an object describing required solver capabilities and the explicit requested
   backend, if any. Never contradict request.execution_constraints.requested_backend.
11. parameter_analysis: an array mapping only concrete CaseSpec leaf paths to hard, soft, or inferred
    requirements. Use exact dot paths such as $.scene.duration_s or $.observation_requirements.cameras;
    list entries use their exact id as a path segment, for example
    $.objects.domino_10.initial_state.rotation_deg. Do not use brackets, numeric indices, wildcards,
    selectors, ranges, or an entire object subtree. A hard user requirement
    has constraint null and can never be auto-adjusted. Every soft/inferred numeric leaf requires an
    explicit numeric min/max constraint; list leaves require min_items/max_items; scalar alternatives
    require an enum values constraint. Do not classify an explicit user requirement as soft/inferred.
12. ambiguities: an array of unresolved questions that could materially change the case.
13. assumptions: an array of conservative assumptions used to make the case executable. Assumptions
    must not grant permissions, licenses, or evidence.

TEXT, IMAGE, AND ASSET RULES
- Natural-language names describe semantics; they are not Catalog asset IDs.
- For an image, distinguish similarity_search from generation_condition, geometry_reference,
  style_reference, and texture_source. A generation condition must not silently become similarity search.
- Procedural generation means a later Provider must generate, import, register, qualify, and return a
  Catalog asset ID. It does not mean you may inject geometry directly into the runtime.
- The deterministic local Provider supports boxes, spheres, and z-axis cylinders. Record full bounding-box
  dimensions in meters; a sphere has equal x/y/z diameters and a cylinder has equal x/y diameters with
  z as its length. Do not use this route for irregular or articulated objects.

OUTPUT PROTOCOL
Return exactly one valid JSON object matching expansion_contract. Use double-quoted JSON property names
and strings. Do not use Markdown fences, comments, trailing commas, NaN, Infinity, single quotes, or
prose before or after the JSON. Arrays must remain arrays and objects must remain objects.
"""


def _expansion_contract() -> dict[str, Any]:
    return {
        "schema_version": EXPANSION_SCHEMA_VERSION,
        "required_fields": list(EXPANSION_FIELDS),
        "field_types": {
            "request_summary": "string",
            "capability_analysis": "object",
            "scene_analysis": "object",
            "object_analysis": "array",
            "event_and_relation_analysis": "array",
            "asset_analysis": "array",
            "asset_source_constraints": "array",
            "parameter_analysis": "array",
            "expected_behavior_analysis": "object",
            "observation_analysis": "object",
            "backend_constraints": "object",
            "ambiguities": "array",
            "assumptions": "array",
        },
        "asset_source_constraint_shape": {
            "scope": {"object_ids": ["exact object_analysis suggested_id values"]},
            "allowed_routes": ["one or more acquisition_route values"],
            "allowed_providers": ["zero or more structured Provider IDs; empty means no Provider restriction"],
            "requirement": "preferred or required",
            "fallback_order": ["zero or more allowed Provider IDs in priority order"],
            "allow_proxy": "boolean",
        },
        "parameter_analysis_shape": {
            "path": (
                "exact CaseSpec leaf dot path, for example $.scene.duration_s or "
                "$.objects.domino_10.initial_state.rotation_deg; list entries use exact ids"
            ),
            "requirement_level": "hard, soft, or inferred",
            "reason": "non-empty provenance explanation",
            "constraint": "null for hard; otherwise numeric {kind,min,max}, list {kind,min_items,max_items}, or enum {kind,values}",
        },
        "output": "one JSON object only; no markdown or prose outside JSON",
    }


def _case_spec_system_prompt() -> str:
    return _harness_mission_context() + """

YOUR ROLE: CASESPEC V2 GENERATOR
Compile the request and Expansion into exactly one complete harness_case_spec_v2 JSON object. The result
is a declarative contract consumed by deterministic planners; it is not a narrative and not executable
code. Follow case_spec_contract exactly, including required fields, field shapes, enums, hard rules, and
the structural example. Use the example only for structure; derive all values from the current request.

FIELD-BY-FIELD INSTRUCTIONS
1. identity: set case_id, a concise title, and source_request. The caller will enforce the original
   case_id and source text.
2. capabilities: this must be an object. Choose only the state/coupling domain:
   rigid_body_dynamics, fluid_particle_dynamics, or deformable_body_dynamics. Never classify the
   request as a named physical process such as falling, bouncing, sequential collision, pouring,
   fracture, or pendulum motion. required contains that same primary and no unrelated capability.
3. scene: describe environment_intent, z_up coordinates, positive duration_s, and optional positive
   bounds_hint_m. Do not put UE map packages or runtime paths here.
4. timebase: use positive integer physics_hz and observation_fps with physics_hz exactly divisible by
   observation_fps; deterministic_seed must be an integer.
5. backend_constraints: required_solver_capabilities must use only registered solver_capability enum
   tokens provided by the selected solver in case_spec_contract.backend_solver_capability_matrix, never
   natural-language phrases. If request.execution_constraints has a requested_backend, use exactly that
   backend in allowed_solvers and do not add or substitute another solver or fallback. Renderer capabilities
   never satisfy required_solver_capabilities. A declared rigid_sph scene solved by genesis_sph uses exactly
   particle_dynamics, particle_cache, and surface_mesh_cache as required_solver_capabilities. The rigid_body
   role on a static or kinematic rigid_sph participant is a scene-role requirement and must not add the unsupported
   rigid_body solver capability to genesis_sph. Use the solver itself as render_backend unless
   the requested evidence needs a separate renderer and case_spec_contract.backend_stage_io shows a shared,
   versioned artifact contract. Set allow_multi_backend=true exactly when solver and renderer differ.
6. asset_policy: use booleans for allow_local, allow_external, allow_generation, and
   allow_analytic_proxy. Set allow_generation=true for procedural_generation or model_generation.
   required_license_tier is local_preview or reference and must reflect the user's request. Default to
   local_preview unless the user explicitly requires reference/publishable/distributable output; choosing
   external_site or a source that happens to be CC0 does not by itself make the whole case require the
   reference tier. In asset.must, license_tier is a minimum acceptable clearance: a reference asset also
   satisfies local_preview. A source restriction for one named object applies only to that object's acquisition:
   keep the permitted local/procedural/analytic routes needed for support surfaces and other infrastructure
   unless the user explicitly forbids those routes for the entire scene.
7. objects: create one entry per logical scene object. Each id must be unique, stable, machine-friendly,
   and independent of an eventual asset ID. role is semantic. geometry uses shape_hint, positive
   approx_size_m, and optional scale_policy. For a built-in local procedural recipe, shape_hint is
   exactly box, sphere, or cylinder; dimensions and orientation never belong in shape_hint prose.
   scale_policy is preserve_authored or
   fit_uniform_to_approx_size. Use fit_uniform_to_approx_size for an external/model-generated mesh when
   the requested physical dimensions are part of the scenario; it applies one uniform instance scale,
   preserving mesh proportions and materials while matching the target bounding-box diagonal as closely
   as possible. Use preserve_authored when source-authored real-world scale
   is explicitly required. physics.body_type is exactly dynamic, static, or kinematic. behavior is always an
   object. Use finite three-number arrays for positions, rotations, and velocities. rotation_deg is
   exactly [pitch, yaw, roll], matching UE Rotator semantics. Heading around world vertical Z belongs
   in yaw (the second value); pitch and roll tilt an otherwise upright object. Incline a box ramp along X with pitch
   (the first value), not yaw. In the Harness UE convention, positive pitch makes local +X downhill
   (the -X end is high), while negative pitch makes local -X downhill; place the high-end support and
   released body on the corresponding high end. A cylinder's authored/analytic axis is local Z: leave
   rotation zero for a world-Z axis, use an absolute 90-degree roll for a world-Y axis, and an absolute
   90-degree pitch for a world-X axis. An object declared supported_by and initially at rest must begin in
   resolved surface contact at frame zero, not above the support with a gravity-settling gap. When the user
   supplies or you generate an initial transform, its horizontal position and orientation are authoritative.
   The compiler may resolve vertical surface contact explicitly declared by supported_by, but otherwise validates
   overlaps and support footprints without rearranging objects from expected relations, separating bodies, or
   moving them around obstacles. Encode curved and staged layouts directly with valid positions and rotations.
   explicitly requests an object's color, store normalized RGB in the object's top-level color_rgb and set
   top-level fixed_material_color=true; do not leave the color only in role or descriptive text. Use low restitution (normally <=0.1) unless a bounce is
   requested, and set physics.use_ccd=true for small or fast-moving collision bodies. Dynamic bodies use
   gravity by default; set physics.enable_gravity=false only when the user explicitly requests a gravity-free
   body. Do not put use_ccd inside behavior. Declare physics.collision_geometry only when an explicit
   analytic collision proxy is needed. Its shape is exactly box, sphere, or cylinder; size_m is the full
   local size in meters; local_center_offset_m is an optional object-local offset that rotates with the
   object and is not scaled again with the visual asset. Sphere size components are equal diameters;
   cylinder x=y is the diameter and z is the local-Z height. Once declared, collision_geometry is the
   collision truth. It cannot coexist with collision_required=false. Do not invent mesh or compound shapes.
   When visual_representation.source=asset and collision_geometry is omitted, the qualified imported asset
   BodySetup is the collision source. When source=none and collision_required=true, collision_geometry is
   required. Never request an implicit bounds-derived collision fallback.
8. solver_scene and object.solver: use these only when the selected backend needs explicit generic
   solver primitives beyond rigid-body fields. For a coupled particle/rigid scene, set
   solver_scene.type="rigid_sph" and declare initialization, measurements, and numeric assertions there.
   initialization is {state:settled|as_authored,pre_roll_s,capture_after_pre_roll}. Use settled with a
   positive pre-roll and capture_after_pre_roll=true when the requested frame zero is an already resting
   or equilibrated state; use as_authored only when the initial transient itself must be observed. A liquid
   participant uses role="fluid" and solver.material_model="sph_liquid" plus solver.initial_volume with a geometric shape,
   dimensions, pose, and a frame. Use frame={"type":"body_local","body_id":exact_id} when the initial
   volume is contained by a moving rigid body; use a world frame otherwise. Every coupled rigid participant
   uses role="rigid_body" and declares
   solver.mobility, solver.transform, and solver.collision. Supported generic collision primitives are
   exactly plane and axisymmetric_profile; do not emit composite or primitives arrays. A plane contains
   position_m, normal, and asset_geometry_match=true. An axisymmetric_profile contains an inner_profile
   array of positive-radius {z_m,radius_m} points, wall_thickness_m, panel_count,
   asset_geometry_match=true, and a non-empty fit_method naming the evidence used to align that profile
   to the render asset. Do not claim asset_geometry_match without such evidence. A body-local cylindrical
   fluid volume must clear the profile wall, bottom, and rim by at least 0.003 m; never make its radius
   exactly equal to the container's inner radius. solver.transform uses position_m, euler_xyz_deg,
   ue_rotation_pyr_deg, and
   optional scale. Kinematic pivot_rotation uses start_time_s, duration_s, pivot_local_m,
   solver_end_rotation_xyz_deg, and ue_end_rotation_pyr_deg; do not invent angle/axis/pivot objects.
   Convert every solver XYZ rotation [x,y,z] to UE pitch/yaw/roll as [-y,-z,x], including motion endpoints.
   pivot_local_m is the actual local pivot point: if the request names a rim or profile edge, use that
   edge's radial coordinate and z coordinate rather than the profile center [0,0,z].
   Keep solver transforms consistent with
   object.initial_state. Also provide workspace_bounds_m={"min_m":[x,y,z],"max_m":[x,y,z]} enclosing
   every declared body, initial volume, and expected motion. Do not name a physical phenomenon, select a prepared process, or put rendering
   behavior in these declarations. If the request does not require such coupling, omit both fields.
   Every object also declares visual_representation.source as exactly asset, solver_generated, or none.
   visual_representation.visible is a boolean that defaults to true and controls rendering only; it never
   enables or disables collision. Set it false for an intentionally hidden collision-proxy object.
   Use asset when Asset Resolve must supply a visual resource. Use solver_generated when the selected solver
   produces the renderable geometry or field consumed by a later render stage; omit object.asset in that case.
   Use none only for an intentionally invisible logical/helper object, and omit object.asset. Never invent a
   placeholder mesh asset for solver-generated output.
9. object.asset: description and optional semantic_text are natural-language search/generation intent;
   resource_kind must use its enum. must contains hard filters that every candidate must satisfy;
   must_not contains hard exclusions; preferences contains soft ranking preferences and can never override
   must/must_not. taxonomy contains string hierarchy labels such as domain, category, subcategory, and
   object_type. relaxation_policy contains boolean allow_parent_category and allow_format_conversion;
   enable relaxation only when the user allows a broader match. When the request explicitly contrasts
   multiple semantic object classes, encode the competing classes as must_not.category exclusions for
   each role; shared material or approximate size is not sufficient identity evidence. must_not must never
   exclude the requested category itself or a parent category that contains it. A required Provider
   route must fail rather than accept an excluded or semantically different class. acquisition.route is default,
   local_catalog, external_site, procedural_generation, or model_generation. Use required only when the
   user explicitly demanded that exact route and origin=user_explicit; otherwise use preferred. Required
   routes have no fallback. For exact rule-based primitives, prefer procedural_generation and use only
   the registered provider hints box_mesh_v1, sphere_mesh_v1, or cylinder_mesh_v1; alternatively leave
   provider_hint null so the Provider can infer it from shape_hint. Never invent a recipe ID. Use box for
   boxes/plates/walls, sphere for balls/spheres, and cylinder for rods/poles/columns/discs. When the user
   has not required local procedural generation, use these built-ins as an efficient option for matching
   primitives rather than a mandatory route; non-primitive assets may be better served by an authorized
   Catalog, external_site, or model_generation source. approx_size_m
   is the full x/y/z bounding-box size in meters; sphere dimensions must be equal and cylinder x/y must
   be equal with z as its length. Irregular or articulated objects are not local primitives. If
   must.source_kind is used with procedural_generation, write the exact token procedural_generation,
   not procedural or descriptive prose. asset_type is the backend asset class such as StaticMesh;
   geometry_type is the shape such as box, sphere, or cylinder. If must.physics_role is needed, use
   dynamic_rigid_body, static_rigid_body, or kinematic_rigid_body to match physics.body_type.
   Expansion asset_source_constraints are hard: for every scoped exact object ID, choose only an allowed
   route and allowed Provider, preserve required and user_explicit, and do not add an unauthorized fallback.
   A global allow_analytic_proxy=true for unrelated objects never weakens a scoped required no-proxy route.
10. acquisition.reference_inputs: every entry is an object with input_id copied exactly from
   request.inputs, usage as an array of registered usage enums, and allow_similarity_search as a boolean.
   Do not copy local paths, hashes, image bytes, captions, or invented IDs into this object.
   acquisition.texture_prompt is optional, applies only to model_generation texturing, and is limited to
   600 characters. Fill it only from an explicit material, color, or style intent. When the user wants to
   preserve the source photos' original texture/colors without a new style, omit texture_prompt. Never use
   it as a geometry prompt or synthesize it from description, role, or shape.
11. relations and events: use canonical reference-bearing objects. A binary relation is
    {"type": string, "source": exact_id, "target": exact_id}; a group relation may use
    {"type": string, "objects": [exact_ids]}; an object event is
    {"type": string, "object": exact_id}. Additional semantic parameters may be objects or scalars, but
    must not replace these exact ID references. Express support canonically as
    {"type":"supported_by","source":subject_id,"target":support_id}. A delayed release event uses
    {"type":"release","object":exact_id,"time_s":nonnegative_number}; put the post-release launch velocity
    on that event as "linear_velocity_m_s":[x,y,z] (and optional "angular_velocity_rad_s":[x,y,z]), while the
    object's initial_state velocity remains zero during the hold. Until release time the runtime holds the object
    at its declared initial transform. Never use phrases such as "box with floor" as a reference.
    Use supported_by, not plain contact, for every dynamic object that initially rests on a load-bearing
    surface. Reserve collision/impacts for runtime propagation edges; plain contact is an initial/static
    scene relation and is not an ordered impact. Size every support surface so its horizontal footprint contains every supported object's full initial
    bounds plus at least 0.25 m margin, and ensure scene.bounds_hint_m contains all full object bounds.
    Declare every expected future binary contact as a collision/impacts relation between its exact object IDs.
    When the user requires an order, repeat those exact pairs in an event_sequence assertion; do not invent
    next_in_chain, chain_order, or topple_order fields. Expected interactions must be physically reachable from
    the authored positions, orientations, velocities, gravity, material parameters, and release times. If the
    user explicitly specifies a pair's surface clearance, write that nonnegative value as surface_gap_m on the
    matching collision/impacts relation; never infer surface_gap_m from approximate positions. When a release
    event's object is the source of one direct impacts relation, its linear velocity must point from the source's
    initial position toward that impacts target.
12. expected_behavior: describe causal and observable outcomes without claiming success.
13. observation_requirements: cameras use registered camera roles and exact target object IDs; modalities
    use registered values; signals name evidence required by the capability and assertions. Do not emit
    exact camera coordinates.
14. verification_requirements: each assertion is an object with a registered type and exact object IDs.
    event_sequence uses pairs=[[id_a,id_b],[id_b,id_c],...] with at least two explicit event pairs; a start/end
    objects list does not express a sequence.
    Choose assertions that test the primary physical invariant. thresholds and time_window are global
    verifier configuration objects passed unchanged to the selected verifier. Use {} unless the selected
    capability contract or an explicit user requirement supplies a named numeric tolerance/window; do not
    invent threshold names, measured values, or pass/fail evidence.
15. variant: should_pass is boolean. provenance is an object. notes is a string.

REFERENCE INTEGRITY
First declare every object in objects. Then reuse those exact IDs in relations, events, camera targets,
and verification assertions. These IDs link CaseSpec records; they are not natural-language asset-search
queries. Asset search later uses asset.description, geometry, taxonomy, and hard requirements.

PROVIDER AND RUNTIME BOUNDARY
Provider routes describe acquisition intent only. Never return a Catalog ID, receipt, generated file,
hash, license proof, dependency, UE object path, scene actor, or runtime binding. Later stages generate or
retrieve assets, register and qualify them, and the single Asset Resolve selects the binding.

OUTPUT PROTOCOL
Return exactly one valid JSON object matching case_spec_contract. Use double quotes. Do not use Markdown,
comments, trailing commas, single quotes, NaN, Infinity, ellipses, placeholders, or explanatory prose.
Before returning, check every required top-level field, enum, array/object type, object ID reference,
backend constraint, acquisition-policy relationship, and assertion type.
"""


def _repair_system_prompt() -> str:
    return _harness_mission_context() + """

YOUR ROLE: ONE BOUNDED CASESPEC REPAIR
Repair the supplied CaseSpec using only the structured validation_errors and case_spec_contract. Return
one complete harness_case_spec_v2 object, not a patch. Preserve user intent and every valid field unless
a listed error requires a change. For each error, correct the exact JSON path and then recheck dependent
rules: capabilities.required contains primary; backend constraints honor the explicit requested backend;
asset-policy booleans authorize declared routes; behavior is an object; body_type is an enum; every
relation, event, camera, and assertion reference exactly matches an objects[].id; every assertion has a
registered type. asset_source_constraints in repair_constraints are hard and must be restored at the exact
object asset acquisition paths named by validation_errors. For
solver_capability_mismatch, keep repair_constraints.requested_backend as the only allowed solver and never
add a solver, renderer, or fallback to allowed_solvers. Replace unsupported requirements using the selected
solver's row in case_spec_contract.backend_solver_capability_matrix. In particular, a genesis_sph rigid_sph
scene requires exactly particle_dynamics, particle_cache, and surface_mesh_cache; its rigid_body object roles
describe static or kinematic collision participants and do not require the unsupported rigid_body solver
capability. UE remains render_backend only. For
rigid_sph_role_required or missing rigid_sph solver fields, never turn a visual-only duplicate into a
second rigid body. Merge the visual asset request and simplified solver collision onto one logical object,
keep the asset-source-constrained object ID, remove the redundant visual/collision duplicate, and remap
references to that retained ID. Every non-fluid rigid_sph object has role=rigid_body and its own solver;
the fluid has role=fluid. A required acquisition always has acquisition.fallback_order=[]; Provider IDs such
as meshy belong only in provider_hint and must never be copied into the route fallback_order. For
procedural_cylinder_local_axis_size_mismatch, store equal diameters in size_m x/y and the authored cylinder
length in z, then use rotation_deg to orient local Z in world space. For procedural_sphere_size_mismatch,
use equal x/y/z diameters. For invalid_surface_gap, use a finite nonnegative surface_gap_m only on its named
collision/impacts relation. For release_velocity_points_away_from_impact_target, preserve the release speed and
change only its direction so it points from the named source position toward the named impacts target. Do not
redesign the scene, add a Provider, add UE paths, relax a required route, invent
evidence, or perform free-form regeneration.

Return exactly one valid JSON object. No Markdown, comments, trailing commas, single quotes, placeholders,
or prose outside JSON.
"""


def _executable_primary_capabilities() -> list[str]:
    return ["deformable_body_dynamics", "fluid_particle_dynamics", "rigid_body_dynamics"]


def case_spec_generation_contract() -> dict[str, Any]:
    return {
        "schema_version": CASE_SPEC_V2_SCHEMA_VERSION,
        "required_top_level_fields": [
            "identity",
            "capabilities",
            "scene",
            "timebase",
            "backend_constraints",
            "asset_policy",
            "objects",
            "relations",
            "events",
            "expected_behavior",
            "observation_requirements",
            "verification_requirements",
            "variant",
            "provenance",
        ],
        "optional_top_level_fields": ["solver_scene", "workspace_bounds_m", "notes"],
        "object_shape": {
            "required": ["id", "role", "visual_representation"],
            "semantic_sections": ["visual_representation", "asset", "geometry", "physics", "initial_state", "behavior", "solver"],
            "asset_cardinality": "zero_or_one_direct_request",
        },
        "field_shapes": {
            "capabilities": {"primary": "string from primary_capability enum", "required": ["same primary string only"]},
            "asset_policy": {
                "allow_local": "boolean",
                "allow_external": "boolean",
                "allow_generation": "boolean; true for procedural_generation or model_generation",
                "allow_analytic_proxy": "boolean",
                "required_license_tier": "local_preview or reference",
            },
            "object": {
                "id": "stable identifier string",
                "role": "semantic object role only; execution behavior comes from physics, initial_state, relations, events, and constraints",
                "visual_representation": {
                    "source": "asset, solver_generated, or none",
                    "visible": "optional boolean; defaults true; controls rendering only and never collision",
                },
                "color_rgb": "optional [red, green, blue], each component between 0 and 1",
                "fixed_material_color": "optional boolean; true when an explicit color_rgb must be rendered",
                "geometry": {
                    "shape_hint": "string; exactly box, sphere, or cylinder for built-in local procedural recipes",
                    "approx_size_m": ["positive x", "positive y", "positive z"],
                    "scale_policy": "preserve_authored or fit_uniform_to_approx_size",
                },
                "physics": {
                    "body_type": "dynamic, static, or kinematic",
                    "mass_kg": "positive number",
                    "collision_required": "boolean",
                    "collision_geometry": {
                        "shape": "box, sphere, or cylinder",
                        "size_m": (
                            "three positive full dimensions in object-local meters; sphere x=y=z is diameter; "
                            "cylinder x=y is diameter and z is local-Z height"
                        ),
                        "local_center_offset_m": (
                            "optional finite [x,y,z] in object-local meters; defaults zero, rotates with the object, "
                            "and is not rescaled by the visual asset instance scale"
                        ),
                    },
                    "material": "object",
                },
                "initial_state": {
                    "position_m": ["x", "y", "z"],
                    "rotation_deg": ["pitch", "yaw", "roll"],
                    "linear_velocity_m_s": ["x", "y", "z"],
                },
                "behavior": "object, never a string",
                "solver": "optional object of generic backend solver primitives; never a named physical-process mode",
            },
            "solver_scene": {
                "type": "rigid_sph when explicit rigid/particle coupling is required",
                "initialization": "{state:settled|as_authored,pre_roll_s:nonnegative,capture_after_pre_roll:boolean}",
                "measurements": {
                    "allowed_types": [
                        "body_interior_fraction",
                        "outside_body_interiors_fraction",
                        "plane_proximity_fraction",
                        "axis_span",
                    ],
                    "shape": "non-empty array; each item has id, type, and only the fields required by that type",
                },
                "assertions": {
                    "shape": "non-empty array of {id, measurement_id, reduction, operator, value}",
                    "reductions": [
                        "initial",
                        "final",
                        "max",
                        "min",
                        "initial_minus_final",
                        "max_frame_decrease",
                        "threshold_crossing_duration",
                    ],
                    "operators": [">=", "<="],
                },
            },
            "rigid_sph_rigid_object_solver": {
                "role": "exactly rigid_body",
                "mobility": "static or kinematic",
                "transform": {
                    "position_m": ["x", "y", "z"],
                    "euler_xyz_deg": ["x", "y", "z"],
                    "ue_rotation_pyr_deg": ["pitch", "yaw", "roll"],
                    "scale": "optional positive xyz",
                },
                "collision": "exactly one plane or axisymmetric_profile object; axisymmetric_profile requires non-empty fit_method; composite/primitives are invalid",
                "motion": "optional {type:pivot_rotation,start_time_s,duration_s,pivot_local_m,solver_end_rotation_xyz_deg,ue_end_rotation_pyr_deg}",
            },
            "rigid_sph_fluid_object_solver": {
                "role": "fluid",
                "material_model": "sph_liquid",
                "initial_volume": "{shape:cylinder,frame:{type:world|body_local,body_id?:exact_id},position_m,radius_m,height_m,euler_xyz_deg?}",
            },
            "workspace_bounds_m": {
                "min_m": ["finite x", "finite y", "finite z"],
                "max_m": ["finite x greater than min x", "finite y greater than min y", "finite z greater than min z"],
            },
            "asset_acquisition": {
                "route": "one acquisition_route enum",
                "requirement": "preferred or required",
                "origin": "user_explicit, llm_inferred, or system_default",
                "provider_hint": "string or null",
                "source_uri_hint": "string or null",
                "reference_inputs": [],
                "fallback_order": [],
                "texture_prompt": "optional texture-only guidance string of at most 600 characters",
            },
            "asset_request": {
                "description": "natural-language asset intent string",
                "resource_kind": "one resource_kind enum",
                "must": "object using only asset_must_field keys; hard candidate requirements",
                "must_not": "object using only asset_must_not_field keys; hard candidate exclusions",
                "preferences": {
                    "field_name": "a scalar value, or {value, weight>=0, confidence between 0 and 1}"
                },
                "taxonomy": {
                    "domain": "string",
                    "category": "string",
                    "subcategory": "string",
                    "object_type": "string",
                },
                "semantic_text": "optional semantic ranking string",
                "relaxation_policy": {
                    "allow_parent_category": "boolean",
                    "allow_format_conversion": "boolean",
                },
                "acquisition": "asset_acquisition object",
            },
            "reference_input": {
                "input_id": "exact request.inputs[].input_id",
                "usage": ["one or more reference_usage enum values"],
                "allow_similarity_search": "boolean; false for generation/style/geometry-only conditions",
            },
            "binary_relation": {
                "type": "string",
                "source": "exact object.id",
                "target": "exact object.id",
                "surface_gap_m": "optional finite nonnegative number; only when explicitly requested",
            },
            "group_relation": {"type": "string", "objects": ["exact object.id"]},
            "object_event": {"type": "string", "object": "exact object.id"},
            "verification_assertion": {
                "type": "one verification_assertion enum string",
                "objects": "exact object IDs for a single-object or single-pair assertion",
                "pairs": "event_sequence only: at least two ordered [exact object.id, exact object.id] pairs",
            },
            "camera": {"role": "one camera_role enum", "target_objects": ["exact object.id"]},
            "verification_requirements": {
                "assertions": ["verification_assertion objects"],
                "thresholds": "global capability/verifier-specific object; empty unless a known contract supplies names",
                "time_window": "global capability/verifier-specific object; empty unless explicitly required",
            },
        },
        "enums": {
            "primary_capability": _executable_primary_capabilities(),
            "coordinate_system": ["z_up"],
            "body_type": ["dynamic", "static", "kinematic"],
            "geometry_scale_policy": ["preserve_authored", "fit_uniform_to_approx_size"],
            "backend": ["fallback", "genesis_fem", "genesis_sph", "taichi_cloth", "ue"],
            "solver_capability": sorted(frozenset().union(*BACKEND_SOLVER_CAPABILITIES.values())),
            "acquisition_route": [
                "default",
                "local_catalog",
                "external_site",
                "procedural_generation",
                "model_generation",
            ],
            "acquisition_requirement": ["preferred", "required"],
            "acquisition_origin": ["user_explicit", "llm_inferred", "system_default"],
            "reference_usage": [
                *sorted(REFERENCE_INPUT_USAGES),
            ],
            "resource_kind": sorted(RESOURCE_KINDS),
            "asset_must_field": sorted(ASSET_MUST_FIELDS),
            "asset_must_not_field": sorted(ASSET_MUST_NOT_FIELDS),
            "camera_role": sorted(CAMERA_ROLES),
            "observation_modality": sorted(OBSERVATION_MODALITIES),
            "verification_assertion": sorted(VERIFICATION_ASSERTION_TYPES),
            "local_procedural_recipe": ["box_mesh_v1", "sphere_mesh_v1", "cylinder_mesh_v1"],
        },
        "backend_solver_capability_matrix": {
            backend: sorted(capabilities)
            for backend, capabilities in sorted(BACKEND_SOLVER_CAPABILITIES.items())
        },
        "backend_stage_io": {
            backend: {
                direction: sorted(contracts)
                for direction, contracts in io.items()
            }
            for backend, io in sorted(BACKEND_STAGE_IO.items())
        },
        "hard_rules": [
            "capabilities must be an object and capabilities.required must contain exactly capabilities.primary",
            "every object behavior must be an object and every physics.body_type must use the body_type enum",
            "every relation, event, camera, and assertion object reference must exactly equal one declared object.id; never use a phrase",
            "initial load-bearing support uses supported_by; expected future contacts use collision/impacts relations",
            "event_sequence uses at least two explicit ordered pairs; never use start/end shorthand or next_in_chain, chain_order, or topple_order",
            "an explicitly requested collision surface clearance belongs in relation.surface_gap_m and must survive projection",
            "verification assertions use only generic state/event operators; object references must be exact declared IDs",
            "must and must_not are hard filters; preferences is soft ranking and cannot weaken a hard filter",
            "put numeric comparison values and operators directly on the generic assertion that consumes them",
            "required acquisition is legal only when origin=user_explicit and route is specific",
            "LLM inferred acquisition must use requirement=preferred",
            "reference input_id must come from request.inputs",
            "do not emit UE paths, runtime stages, exact camera poses, or verifier implementations",
            "solver_scene and object.solver may declare generic primitives only and must survive deterministic projection unchanged",
            "asset resolution applies only to objects with visual_representation.source=asset; solver_generated and none must omit object.asset",
            "visual_representation.visible controls rendering only; collision is controlled by physics declarations and the selected collision binding",
            "declared physics.collision_geometry is the sole collision truth and supports only box, sphere, or cylinder; it cannot coexist with collision_required=false",
            "source=none with collision_required=true requires explicit collision_geometry; source=asset without collision_geometry uses the qualified imported asset BodySetup and never a bounds-derived fallback",
            "one logical rigid_sph body owns both its visual asset request and simplified solver collision; never split them into visual/collision object IDs",
            "a genesis_sph rigid_sph scene requires particle_dynamics, particle_cache, and surface_mesh_cache; rigid_body is an object role, not a genesis_sph solver capability",
        ],
        "valid_structure_example_do_not_copy_values": _valid_case_spec_structure_example(),
        "valid_rigid_sph_shape_example_do_not_copy_values": _valid_rigid_sph_shape_example(),
    }


def _case_spec_contract() -> dict[str, Any]:
    """Compatibility alias for the legacy fixed-LLM generation path."""
    return case_spec_generation_contract()


def _valid_case_spec_structure_example() -> dict[str, Any]:
    return {
        "schema_version": CASE_SPEC_V2_SCHEMA_VERSION,
        "identity": {"case_id": "example", "title": "Example", "source_request": "Example request"},
        "capabilities": {
            "primary": "rigid_body_dynamics",
            "required": ["rigid_body_dynamics"],
        },
        "scene": {
            "environment_intent": "minimal floor",
            "coordinate_system": "z_up",
            "duration_s": 2.0,
            "bounds_hint_m": [3.0, 3.0, 3.0],
        },
        "timebase": {"physics_hz": 120, "observation_fps": 24, "deterministic_seed": 1},
        "backend_constraints": {
            "required_solver_capabilities": ["rigid_body", "contact_events"],
            "allowed_solvers": ["ue"],
            "render_backend": "ue",
            "allow_multi_backend": False,
        },
        "asset_policy": {
            "allow_local": True,
            "allow_external": False,
            "allow_generation": True,
            "allow_analytic_proxy": True,
            "required_license_tier": "local_preview",
        },
        "objects": [
            {
                "id": "generated_box",
                "role": "dynamic_box",
                "visual_representation": {"source": "asset", "visible": True},
                "geometry": {"shape_hint": "box", "approx_size_m": [0.4, 0.6, 0.8]},
                "physics": {
                    "body_type": "dynamic",
                    "enable_gravity": True,
                    "mass_kg": 1.0,
                    "collision_required": True,
                    "collision_geometry": {
                        "shape": "box",
                        "size_m": [0.4, 0.6, 0.8],
                        "local_center_offset_m": [0.0, 0.0, 0.0],
                    },
                    "material": {"dynamic_friction": 0.5, "restitution": 0.2},
                },
                "initial_state": {
                    "position_m": [0.0, 0.0, 1.4],
                    "rotation_deg": [0.0, 0.0, 0.0],
                    "linear_velocity_m_s": [0.0, 0.0, 0.0],
                },
                "behavior": {},
                "asset": {
                    "description": "generated box",
                    "resource_kind": "mesh_3d",
                    "acquisition": {
                        "route": "procedural_generation",
                        "requirement": "required",
                        "origin": "user_explicit",
                        "provider_hint": "box_mesh_v1",
                        "source_uri_hint": None,
                        "reference_inputs": [],
                        "fallback_order": [],
                    },
                },
            },
            {
                "id": "floor",
                "role": "support",
                "visual_representation": {"source": "none", "visible": False},
                "geometry": {"shape_hint": "box", "approx_size_m": [3.0, 3.0, 0.1]},
                "physics": {
                    "body_type": "static",
                    "collision_required": True,
                    "collision_geometry": {
                        "shape": "box",
                        "size_m": [3.0, 3.0, 0.1],
                        "local_center_offset_m": [0.0, 0.0, 0.0],
                    },
                },
                "initial_state": {"position_m": [0.0, 0.0, 0.0]},
                "behavior": {},
            },
        ],
        "relations": [{"type": "collision", "source": "generated_box", "target": "floor"}],
        "events": [],
        "expected_behavior": {"contact_required": True},
        "observation_requirements": {
            "cameras": [{"role": "front_static", "target_objects": ["generated_box", "floor"]}],
            "modalities": ["rgb"],
            "signals": ["trajectory", "contact_events"],
        },
        "verification_requirements": {
            "assertions": [
                {"id": "box_contacts_floor", "type": "event_exists", "event": "contact", "objects": ["generated_box", "floor"]}
            ],
            "thresholds": {},
        },
        "variant": {"should_pass": True},
        "provenance": {},
        "notes": "Structure example only.",
    }


def _valid_rigid_sph_shape_example() -> dict[str, Any]:
    """Show the exact coupled shape without duplicating a complete CaseSpec."""
    return {
        "backend_constraints": {
            "required_solver_capabilities": [
                "particle_dynamics",
                "particle_cache",
                "surface_mesh_cache",
            ],
            "allowed_solvers": ["genesis_sph"],
            "render_backend": "ue",
            "allow_multi_backend": True,
        },
        "workspace_bounds_m": {"min_m": [-1.0, -1.0, -0.1], "max_m": [1.0, 1.0, 1.0]},
        "objects": [
            {
                "id": "container",
                "role": "rigid_body",
                "visual_representation": {"source": "asset"},
                "asset": {
                    "description": "visual mesh for this same physical container",
                    "resource_kind": "mesh_3d",
                    "must": {},
                    "must_not": {},
                    "preferences": {},
                    "taxonomy": {},
                    "relaxation_policy": {
                        "allow_parent_category": False,
                        "allow_format_conversion": True,
                    },
                    "acquisition": {
                        "route": "model_generation",
                        "requirement": "required",
                        "origin": "user_explicit",
                        "provider_hint": "named_provider",
                        "source_uri_hint": None,
                        "reference_inputs": [],
                        "fallback_order": [],
                    },
                },
                "solver": {
                    "mobility": "kinematic",
                    "transform": {
                        "position_m": [0.0, 0.0, 0.1],
                        "euler_xyz_deg": [0.0, 0.0, 0.0],
                        "ue_rotation_pyr_deg": [0.0, 0.0, 0.0],
                    },
                    "collision": {
                        "type": "axisymmetric_profile",
                        "asset_geometry_match": True,
                        "fit_method": "declared_asset_dimensions_and_profile_landmarks_v1",
                        "inner_profile": [
                            {"z_m": -0.04, "radius_m": 0.03},
                            {"z_m": 0.04, "radius_m": 0.04},
                        ],
                        "wall_thickness_m": 0.005,
                        "panel_count": 16,
                    },
                    "motion": {
                        "type": "pivot_rotation",
                        "start_time_s": 0.3,
                        "duration_s": 1.0,
                        "pivot_local_m": [0.04, 0.0, 0.04],
                        "solver_end_rotation_xyz_deg": [0.0, 90.0, 0.0],
                        "ue_end_rotation_pyr_deg": [-90.0, 0.0, 0.0],
                    },
                },
            },
            {
                "id": "surface",
                "role": "rigid_body",
                "visual_representation": {"source": "asset"},
                "geometry": {"shape_hint": "box", "approx_size_m": [0.8, 0.8, 0.05]},
                "asset": {
                    "description": "procedural support surface",
                    "resource_kind": "mesh_3d",
                    "must": {"geometry_type": "box", "source_kind": "procedural_generation"},
                    "must_not": {},
                    "preferences": {},
                    "taxonomy": {},
                    "relaxation_policy": {
                        "allow_parent_category": False,
                        "allow_format_conversion": False,
                    },
                    "acquisition": {
                        "route": "procedural_generation",
                        "requirement": "preferred",
                        "origin": "llm_inferred",
                        "provider_hint": "box_mesh_v1",
                        "source_uri_hint": None,
                        "reference_inputs": [],
                        "fallback_order": [],
                    },
                },
                "solver": {
                    "mobility": "static",
                    "transform": {
                        "position_m": [0.0, 0.0, -0.025],
                        "euler_xyz_deg": [0.0, 0.0, 0.0],
                        "ue_rotation_pyr_deg": [0.0, 0.0, 0.0],
                    },
                    "collision": {
                        "type": "plane",
                        "position_m": [0.0, 0.0, 0.0],
                        "normal": [0.0, 0.0, 1.0],
                        "asset_geometry_match": True,
                    },
                },
            },
            {
                "id": "liquid",
                "role": "fluid",
                "visual_representation": {"source": "solver_generated"},
                "solver": {
                    "material_model": "sph_liquid",
                    "initial_volume": {
                        "shape": "cylinder",
                        "frame": {"type": "body_local", "body_id": "container"},
                        "position_m": [0.0, 0.0, 0.0],
                        "euler_xyz_deg": [0.0, 0.0, 0.0],
                        "radius_m": 0.025,
                        "height_m": 0.06,
                    },
                },
            },
        ],
        "solver_scene": {
            "type": "rigid_sph",
            "initialization": {"state": "settled", "pre_roll_s": 0.25, "capture_after_pre_roll": True},
            "measurements": [
                {"id": "inside", "type": "body_interior_fraction", "body_id": "container"},
                {
                    "id": "near_surface",
                    "type": "plane_proximity_fraction",
                    "body_id": "surface",
                    "distance_m": 0.01,
                },
                {"id": "span", "type": "axis_span", "axes": ["x", "y"]},
            ],
            "assertions": [
                {
                    "id": "starts_inside",
                    "measurement_id": "inside",
                    "reduction": "initial",
                    "operator": ">=",
                    "value": 0.8,
                },
                {
                    "id": "reaches_surface",
                    "measurement_id": "near_surface",
                    "reduction": "final",
                    "operator": ">=",
                    "value": 0.3,
                },
            ],
        },
    }
