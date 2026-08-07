from __future__ import annotations

import base64
import copy
import hashlib
import json
import mimetypes
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from harness.core.artifact_schema import write_json
from harness.core.case_spec_v2 import (
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
    case_spec_v2_from_dict,
    normalize_case_spec_v2,
    stable_case_spec_digest,
)


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
    "ambiguities",
    "assumptions",
)
REQUESTED_BACKENDS = {"fallback", "genesis_fem", "genesis_sph", "taichi_cloth", "ue"}


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


@dataclass(frozen=True)
class CaseGenerationResult:
    request: dict[str, Any]
    expansion: dict[str, Any]
    case_spec: CaseSpecV2
    llm_trace: dict[str, Any]

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
    ) -> None:
        self.base_url = str(
            base_url
            or os.environ.get("SIM_HARNESS_LLM_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("SIM_HARNESS_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self.model = str(model or os.environ.get("SIM_HARNESS_LLM_MODEL") or os.environ.get("OPENAI_MODEL") or "").strip()
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
            raise RuntimeError("Set SIM_HARNESS_LLM_MODEL (or OPENAI_MODEL) for CaseSpec V2 generation.")
        if self.base_url.startswith("https://api.openai.com/") and not self.api_key:
            raise RuntimeError("Set SIM_HARNESS_LLM_API_KEY (or OPENAI_API_KEY) for the configured LLM endpoint.")
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
        endpoint = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(endpoint, data=encoded, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"LLM {purpose} request failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM {purpose} request failed: {exc.reason}") from exc
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise RuntimeError(f"LLM {purpose} response must be a JSON object")
        payload = _completion_payload(decoded)
        receipt = {
            "schema_version": "harness_llm_call_receipt_v1",
            "purpose": purpose,
            "response_id": decoded.get("id"),
            "model": decoded.get("model") or self.model,
            "usage": decoded.get("usage") or {},
            "request_sha256": hashlib.sha256(encoded).hexdigest(),
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "endpoint_kind": "openai_compatible_chat_completions",
        }
        return LLMJSONResponse(payload=payload, receipt=receipt)


def build_case_request(
    *,
    case_id: str,
    text: str | None = None,
    image_paths: list[str | Path] | None = None,
    allow_image_upload: bool = False,
    requested_backend: str | None = None,
) -> dict[str, Any]:
    normalized_text = " ".join(str(text or "").split())
    paths = [Path(value).expanduser().resolve() for value in image_paths or []]
    if not normalized_text and not paths:
        raise ValueError("CaseSpec V2 generation requires text, at least one image, or both")
    if paths and not allow_image_upload:
        raise ValueError("Uploading reference images to an LLM requires --allow-image-upload")
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
                "external_upload_authorized": True,
            }
        )
    backend = str(requested_backend or "").strip()
    if backend and backend not in REQUESTED_BACKENDS:
        raise ValueError(f"requested_backend must be one of {sorted(REQUESTED_BACKENDS)}")
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "case_id": str(case_id),
        "text": normalized_text,
        "inputs": images,
        "execution_constraints": {"requested_backend": backend or None},
    }


def generate_case_spec_v2(
    request: Mapping[str, Any],
    *,
    client: JSONCompletionClient | None = None,
    artifact_dir: str | Path | None = None,
) -> CaseGenerationResult:
    validated_request = _validate_request(request)
    client = client or OpenAICompatibleJSONClient()
    destination = Path(artifact_dir) if artifact_dir is not None else None
    if destination is not None:
        write_json(destination / "request.json", validated_request)
    images = [dict(item) for item in validated_request.get("inputs") or [] if item.get("kind") == "image"]
    expansion_response = client.complete_json(
        system_prompt=_expansion_system_prompt(),
        user_payload={
            "request": _request_for_model(validated_request),
            "planning_contract": {
                "executable_primary_capabilities": _executable_primary_capabilities(),
            },
            "expansion_contract": _expansion_contract(),
        },
        images=images,
        purpose="expansion",
    )
    if destination is not None:
        write_json(destination / "expansion_raw.json", expansion_response.payload)
        write_json(destination / "expansion_call_receipt.json", expansion_response.receipt)
    expansion = _normalize_expansion(expansion_response.payload)
    if destination is not None:
        write_json(destination / "expansion.json", expansion)
    generation_response = client.complete_json(
        system_prompt=_case_spec_system_prompt(),
        user_payload={
            "request": _request_for_model(validated_request),
            "expansion": expansion,
            "case_spec_contract": _case_spec_contract(),
        },
        images=None,
        purpose="case_spec_generation",
    )
    if destination is not None:
        write_json(destination / "case_spec_generation_raw.json", generation_response.payload)
        write_json(destination / "case_spec_generation_call_receipt.json", generation_response.receipt)
    raw_case_spec = _unwrap_case_spec(generation_response.payload)
    raw_case_spec = _apply_request_identity(raw_case_spec, validated_request)
    receipts = [expansion_response.receipt, generation_response.receipt]
    repair_count = 0
    try:
        case_spec = case_spec_v2_from_dict(
            raw_case_spec,
            available_input_ids=[str(item["input_id"]) for item in validated_request.get("inputs") or []],
        )
    except CaseSpecV2ValidationError as validation_error:
        if destination is not None:
            write_json(destination / "case_spec_validation_errors.json", validation_error.to_dict())
        repair_response = client.complete_json(
            system_prompt=_repair_system_prompt(),
            user_payload={
                "invalid_case_spec": normalize_case_spec_v2(raw_case_spec),
                "validation_errors": validation_error.to_dict(),
                "repair_constraints": {
                    "maximum_repairs": 1,
                    "preserve_user_intent": True,
                    "do_not_change_valid_fields_unless_required_by_an_error": True,
                },
                "case_spec_contract": _case_spec_contract(),
            },
            images=None,
            purpose="case_spec_validation_repair",
        )
        if destination is not None:
            write_json(destination / "case_spec_repair_raw.json", repair_response.payload)
            write_json(destination / "case_spec_repair_call_receipt.json", repair_response.receipt)
        repair_count = 1
        receipts.append(repair_response.receipt)
        repaired = _apply_request_identity(_unwrap_case_spec(repair_response.payload), validated_request)
        try:
            case_spec = case_spec_v2_from_dict(
                repaired,
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
        },
    )


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
    constraints = data.get("execution_constraints") or {}
    if not isinstance(constraints, Mapping):
        raise ValueError("request execution_constraints must be an object")
    requested_backend = constraints.get("requested_backend")
    if requested_backend is not None and requested_backend not in REQUESTED_BACKENDS:
        raise ValueError(f"request requested_backend must be one of {sorted(REQUESTED_BACKENDS)}")
    data["execution_constraints"] = dict(constraints)
    return data


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
        "execution_constraints": dict(request.get("execution_constraints") or {}),
    }


def _normalize_expansion(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("expansion") if isinstance(payload.get("expansion"), Mapping) else payload
    expansion = dict(raw)
    expansion["schema_version"] = EXPANSION_SCHEMA_VERSION
    for field in EXPANSION_FIELDS:
        if field not in expansion:
            expansion[field] = [] if field in {"object_analysis", "event_and_relation_analysis", "asset_analysis", "ambiguities", "assumptions"} else {}
    if not isinstance(expansion.get("request_summary"), str):
        raise ValueError("expansion.request_summary must be a string")
    for field in ("object_analysis", "event_and_relation_analysis", "asset_analysis", "ambiguities", "assumptions"):
        value = expansion.get(field)
        if isinstance(value, Mapping):
            expansion[field] = _analysis_mapping_to_list(value)
        elif not isinstance(value, list):
            raise ValueError(f"expansion.{field} must be a list")
    for field in (
        "capability_analysis",
        "scene_analysis",
        "expected_behavior_analysis",
        "observation_analysis",
        "backend_constraints",
    ):
        if not isinstance(expansion.get(field), dict):
            raise ValueError(f"expansion.{field} must be an object")
    return expansion


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


def _apply_request_identity(case_spec: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
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
        backend["render_backend"] = requested_backend
        backend["allow_multi_backend"] = False
        result["backend_constraints"] = backend
    return result


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
4. object_analysis: an array with one object per distinct simulated or rendered object. For each, propose
   a stable machine-friendly suggested_id, semantic role, geometry and dimensions, body behavior,
   material/physics needs, initial-state intent, and whether it requires an asset.
5. event_and_relation_analysis: an array of temporal events and relations among proposed objects, such
   as falling, contact, collision order, support, attachment, fracture, or settling. Refer to proposed
   object IDs consistently.
6. asset_analysis: an array with one entry per asset need. Separate the logical object from its asset.
   State whether the need is satisfied by default/local Catalog retrieval, external_site acquisition,
   procedural_generation, or model_generation. Preserve explicit routes; inferred routes are soft.
   Prefer procedural_generation for simple rule-based primitives that can be described exactly as a
   box, sphere, or z-axis cylinder; plates/walls are thin boxes and rods/poles/columns/discs are cylinders.
7. expected_behavior_analysis: an object describing observable preconditions, event ordering, causal
   response, and postconditions without claiming that the run passed.
8. observation_analysis: an object describing useful camera roles, modalities (RGB/depth/segmentation),
   solver signals, and what evidence is needed. Do not emit exact camera transforms.
9. backend_constraints: an object describing required solver capabilities and the explicit requested
   backend, if any. Never contradict request.execution_constraints.requested_backend.
10. ambiguities: an array of unresolved questions that could materially change the case.
11. assumptions: an array of conservative assumptions used to make the case executable. Assumptions
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
            "expected_behavior_analysis": "object",
            "observation_analysis": "object",
            "backend_constraints": "object",
            "ambiguities": "array",
            "assumptions": "array",
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
2. capabilities: this must be an object. primary must be one registered executable capability;
   required must be an array containing that same primary and no unrelated capability.
3. scene: describe environment_intent, z_up coordinates, positive duration_s, and optional positive
   bounds_hint_m. Do not put UE map packages or runtime paths here.
4. timebase: use positive integer physics_hz and observation_fps with physics_hz exactly divisible by
   observation_fps; deterministic_seed must be an integer.
5. backend_constraints: required_solver_capabilities must use only registered solver_capability enum
   tokens such as rigid_body and contact_events, never natural-language phrases. If request.execution_constraints has a
   requested_backend, use it in allowed_solvers and render_backend and do not substitute another backend.
   Otherwise choose only registered compatible backends. Use allow_multi_backend only for an intentional
   separate solver/render plan.
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
   and independent of an eventual asset ID. role is semantic. geometry uses shape_hint and positive
   approx_size_m. physics.body_type is exactly dynamic, static, or kinematic. behavior is always an
   object. Use finite three-number arrays for positions, rotations, and velocities. rotation_deg is
   exactly [pitch, yaw, roll], matching UE Rotator semantics: incline a box ramp along X with pitch
   (the first value), not yaw. Place initially resting objects with a small positive clearance (about
   0.002-0.005 m) above the supporting surface. Use low restitution (normally <=0.1) unless a bounce is
   requested, and set use_ccd=true for small or fast-moving collision bodies.
8. object.asset: description and optional semantic_text are natural-language search/generation intent;
   resource_kind must use its enum. must contains hard filters that every candidate must satisfy;
   must_not contains hard exclusions; preferences contains soft ranking preferences and can never override
   must/must_not. taxonomy contains string hierarchy labels such as domain, category, subcategory, and
   object_type. relaxation_policy contains boolean allow_parent_category and allow_format_conversion;
   enable relaxation only when the user allows a broader match. acquisition.route is default,
   local_catalog, external_site, procedural_generation, or model_generation. Use required only when the
   user explicitly demanded that exact route and origin=user_explicit; otherwise use preferred. Required
   routes have no fallback. For exact rule-based primitives, prefer procedural_generation and use only
   the registered provider hints box_mesh_v1, sphere_mesh_v1, or cylinder_mesh_v1; alternatively leave
   provider_hint null so the Provider can infer it from shape_hint. Never invent a recipe ID. Use box for
   boxes/plates/walls, sphere for balls/spheres, and cylinder for rods/poles/columns/discs. approx_size_m
   is the full x/y/z bounding-box size in meters; sphere dimensions must be equal and cylinder x/y must
   be equal with z as its length. Irregular or articulated objects are not local primitives. If
   must.source_kind is used with procedural_generation, write the exact token procedural_generation,
   not procedural or descriptive prose. asset_type is the backend asset class such as StaticMesh;
   geometry_type is the shape such as box, sphere, or cylinder. If must.physics_role is needed, use
   dynamic_rigid_body, static_rigid_body, or kinematic_rigid_body to match physics.body_type.
9. acquisition.reference_inputs: every entry is an object with input_id copied exactly from
   request.inputs, usage as an array of registered usage enums, and allow_similarity_search as a boolean.
   Do not copy local paths, hashes, image bytes, captions, or invented IDs into this object.
10. relations and events: use canonical reference-bearing objects. A binary relation is
    {"type": string, "source": exact_id, "target": exact_id}; a group relation may use
    {"type": string, "objects": [exact_ids]}; an object event is
    {"type": string, "object": exact_id}. Additional semantic parameters may be objects or scalars, but
    must not replace these exact ID references. Express support canonically as
    {"type":"supported_by","source":subject_id,"target":support_id}. A delayed release event uses
    {"type":"release","object":exact_id,"time_s":nonnegative_number}; until that time the runtime holds
    the object at its declared initial transform. Never use phrases such as "box with floor" as a reference.
    Size every support surface so its horizontal footprint contains every supported object's full initial
    bounds plus at least 0.25 m margin, and ensure scene.bounds_hint_m contains all full object bounds.
    For a collision chain, include enough support area for the staged objects and expected interaction path.
    A declared collision order must be physically reachable from the initial positions, velocities, gravity,
    friction, and release times: keep intended targets close enough, aim the mover toward them, and do not
    rely on equal-acceleration followers magically catching a leading body. Do not substitute several small
    vertical drops when the request asks for an impact, transfer, cascade, or ramp collision process.
11. expected_behavior: describe causal and observable outcomes without claiming success.
12. observation_requirements: cameras use registered camera roles and exact target object IDs; modalities
    use registered values; signals name evidence required by the capability and assertions. Do not emit
    exact camera coordinates.
13. verification_requirements: each assertion is an object with a registered type and exact object IDs.
    Choose assertions that test the primary physical invariant. thresholds and time_window are global
    verifier configuration objects passed unchanged to the selected verifier. Use {} unless the selected
    capability contract or an explicit user requirement supplies a named numeric tolerance/window; do not
    invent threshold names, measured values, or pass/fail evidence.
14. variant: should_pass is boolean. provenance is an object. notes is a string.

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
registered type. For support_footprint_too_small, enlarge or reposition the named support so it contains
the subject's full horizontal bounds; for ramp_has_no_incline_rotation, remember rotation_deg is
[pitch,yaw,roll] and use pitch (or roll) rather than yaw as the incline. Do not redesign the scene, add a Provider, add UE paths, relax a required route, invent
evidence, or perform free-form regeneration.

Return exactly one valid JSON object. No Markdown, comments, trailing commas, single quotes, placeholders,
or prose outside JSON.
"""


def _executable_primary_capabilities() -> list[str]:
    # Local import avoids making the schema layer depend on verifier modules.
    from harness.planning.verification_compiler import VERIFIER_BY_CAPABILITY

    return sorted(VERIFIER_BY_CAPABILITY)


def _case_spec_contract() -> dict[str, Any]:
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
        "object_shape": {
            "required": ["id", "role"],
            "semantic_sections": ["asset", "geometry", "physics", "initial_state", "behavior"],
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
                "role": "semantic role string",
                "geometry": {"shape_hint": "string", "approx_size_m": ["positive x", "positive y", "positive z"]},
                "physics": {
                    "body_type": "dynamic, static, or kinematic",
                    "mass_kg": "positive number",
                    "collision_required": "boolean",
                    "material": "object",
                },
                "initial_state": {
                    "position_m": ["x", "y", "z"],
                    "rotation_deg": ["x", "y", "z"],
                    "linear_velocity_m_s": ["x", "y", "z"],
                },
                "behavior": "object, never a string",
            },
            "asset_acquisition": {
                "route": "one acquisition_route enum",
                "requirement": "preferred or required",
                "origin": "user_explicit, llm_inferred, or system_default",
                "provider_hint": "string or null",
                "source_uri_hint": "string or null",
                "reference_inputs": [],
                "fallback_order": [],
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
            "binary_relation": {"type": "string", "source": "exact object.id", "target": "exact object.id"},
            "group_relation": {"type": "string", "objects": ["exact object.id"]},
            "object_event": {"type": "string", "object": "exact object.id"},
            "verification_assertion": {
                "type": "one verification_assertion enum string",
                "objects": ["exact object.id", "exact object.id"],
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
        "hard_rules": [
            "capabilities must be an object and capabilities.required must contain exactly capabilities.primary",
            "every object behavior must be an object and every physics.body_type must use the body_type enum",
            "every relation, event, camera, and assertion object reference must exactly equal one declared object.id; never use a phrase",
            "every verification assertion requires a type from verification_assertion and an objects array of exact IDs",
            "must and must_not are hard filters; preferences is soft ranking and cannot weaken a hard filter",
            "thresholds and time_window are passed to the verifier unchanged; use only names defined by the selected capability contract",
            "required acquisition is legal only when origin=user_explicit and route is specific",
            "LLM inferred acquisition must use requirement=preferred",
            "reference input_id must come from request.inputs",
            "do not emit UE paths, runtime stages, exact camera poses, or verifier implementations",
        ],
        "valid_structure_example_do_not_copy_values": _valid_case_spec_structure_example(),
    }


def _valid_case_spec_structure_example() -> dict[str, Any]:
    return {
        "schema_version": CASE_SPEC_V2_SCHEMA_VERSION,
        "identity": {"case_id": "example", "title": "Example", "source_request": "Example request"},
        "capabilities": {
            "primary": "rigid_body_gravity_collision",
            "required": ["rigid_body_gravity_collision"],
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
                "role": "falling_body",
                "geometry": {"shape_hint": "box", "approx_size_m": [0.4, 0.6, 0.8]},
                "physics": {
                    "body_type": "dynamic",
                    "mass_kg": 1.0,
                    "collision_required": True,
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
                "geometry": {"shape_hint": "box", "approx_size_m": [3.0, 3.0, 0.1]},
                "physics": {"body_type": "static", "collision_required": True},
                "initial_state": {"position_m": [0.0, 0.0, 0.0]},
                "behavior": {},
            },
        ],
        "relations": [{"type": "collision", "source": "generated_box", "target": "floor"}],
        "events": [{"type": "gravity_drop", "object": "generated_box"}],
        "expected_behavior": {"contact_required": True},
        "observation_requirements": {
            "cameras": [{"role": "front_static", "target_objects": ["generated_box", "floor"]}],
            "modalities": ["rgb"],
            "signals": ["trajectory", "contact_events"],
        },
        "verification_requirements": {
            "assertions": [
                {"type": "gravity_then_support_contact", "objects": ["generated_box", "floor"]}
            ],
            "thresholds": {},
        },
        "variant": {"should_pass": True},
        "provenance": {},
        "notes": "Structure example only.",
    }
