from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from harness.core.capability import CapabilityStore, canonical_capability_id
from harness.core.case_spec import CaseSpec, validate_case_spec


CASE_SPEC_V2_SCHEMA_VERSION = "harness_case_spec_v2"
ACQUISITION_ROUTES = {
    "default",
    "local_catalog",
    "external_site",
    "procedural_generation",
    "model_generation",
}
ACQUISITION_REQUIREMENTS = {"preferred", "required"}
ACQUISITION_ORIGINS = {"user_explicit", "llm_inferred", "system_default"}
REFERENCE_INPUT_USAGES = {
    "similarity_search",
    "generation_condition",
    "geometry_reference",
    "style_reference",
    "texture_source",
}
CAMERA_ROLES = {
    "overview",
    "front",
    "front_static",
    "side",
    "side_static",
    "top",
    "top_down",
    "tracking_subject",
    "event_closeup",
}
OBSERVATION_MODALITIES = {"rgb", "depth", "segmentation", "instance_segmentation"}
VERIFICATION_ASSERTION_TYPES = {
    "action_effect_occurs",
    "angular_speed_decay",
    "ballistic_gravity_impact",
    "constraint_impulse_transfer",
    "constraint_preserved",
    "contact_causality",
    "contact_occurs",
    "displacement_along_force",
    "elastic_launch_occurs",
    "elastic_rebound_occurs",
    "fracture_after_contact",
    "frictional_ramp_motion",
    "gravity_then_support_contact",
    "magnetic_response",
    "mesh_cache_complete",
    "momentum_transfer",
    "ordered_contact_propagation",
    "particle_cache_complete",
    "restitution_envelope",
    "rolling_deceleration",
    "sliding_deceleration",
}
LICENSE_TIERS = {"local_preview", "reference"}
BODY_TYPES = {"dynamic", "static", "kinematic"}
GEOMETRY_SCALE_POLICIES = {"preserve_authored", "fit_uniform_to_approx_size"}
BACKEND_SOLVERS = {"fallback", "genesis_fem", "genesis_sph", "taichi_cloth", "ue"}
BACKEND_SOLVER_CAPABILITIES = {
    "fallback": {"rigid_body", "contact_events", "trajectory"},
    "ue": {"rigid_body", "contact_events", "trajectory", "fracture_events", "geometry_collection"},
    "genesis_sph": {"particle_dynamics", "fluid_dynamics", "particle_cache", "surface_mesh_cache", "trajectory"},
    "genesis_fem": {"soft_body", "finite_element", "mesh_cache", "deformable_mesh_cache", "trajectory"},
    "taichi_cloth": {"soft_body", "cloth", "mesh_cache", "trajectory"},
}
REGISTERED_SOLVER_CAPABILITIES = frozenset().union(*BACKEND_SOLVER_CAPABILITIES.values())
CAPABILITY_ALLOWED_SOLVERS = {
    "fluid_particle_dynamics": {"fallback", "genesis_sph"},
    "soft_body_deformation": {"fallback", "genesis_fem", "taichi_cloth"},
}
DEFAULT_CAPABILITY_ALLOWED_SOLVERS = {"fallback", "ue"}
RESOURCE_KINDS = {
    "mesh_3d",
    "skeletal_mesh",
    "geometry_collection",
    "blueprint_actor",
    "material",
    "texture_2d",
    "animation",
    "map",
    "vfx",
}
ASSET_REQUEST_FIELDS = {
    "description",
    "resource_kind",
    "must",
    "preferences",
    "acquisition",
    "taxonomy",
    "must_not",
    "semantic_text",
    "relaxation_policy",
}
ASSET_MUST_FIELDS = {
    "approx_size_m",
    "asset_type",
    "backend",
    "category",
    "class_name",
    "collision",
    "geometry_type",
    "license_tier",
    "materialized",
    "physics_role",
    "real_3d_geometry",
    "runtime_ready",
    "source_kind",
}
ASSET_MUST_NOT_FIELDS = {
    "asset_type",
    "backend",
    "category",
    "class_name",
    "geometry_type",
    "license_tier",
    "source_kind",
}
TOP_LEVEL_FIELDS = {
    "schema_version",
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
    "notes",
}


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


class CaseSpecV2ValidationError(ValueError):
    def __init__(self, issues: Iterable[ValidationIssue]) -> None:
        self.issues = list(issues)
        summary = "; ".join(f"{issue.path}: {issue.message}" for issue in self.issues[:8])
        if len(self.issues) > 8:
            summary += f"; ... ({len(self.issues)} issues total)"
        super().__init__(summary or "CaseSpec V2 validation failed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "harness_case_spec_v2_validation_errors_v1",
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class CaseSpecV2:
    data: dict[str, Any]

    @property
    def case_id(self) -> str:
        return str((self.data.get("identity") or {}).get("case_id") or "")

    @property
    def capability_id(self) -> str:
        return canonical_capability_id(str((self.data.get("capabilities") or {}).get("primary") or ""))

    @property
    def objects(self) -> list[dict[str, Any]]:
        return [item for item in self.data.get("objects", []) if isinstance(item, dict)]


def load_case_spec_v2(path: str | Path) -> CaseSpecV2:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"case spec must be a JSON object: {path}")
    return case_spec_v2_from_dict(data)


def case_spec_v2_from_dict(
    data: Mapping[str, Any],
    *,
    available_input_ids: Iterable[str] | None = None,
) -> CaseSpecV2:
    normalized = normalize_case_spec_v2(data)
    validate_case_spec_v2(normalized, available_input_ids=available_input_ids)
    return CaseSpecV2(normalized)


def normalize_case_spec_v2(data: Mapping[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(dict(data))
    normalized.setdefault("schema_version", CASE_SPEC_V2_SCHEMA_VERSION)
    identity = _ensure_dict(normalized, "identity")
    identity.setdefault("title", identity.get("case_id") or "Untitled physics case")
    identity.setdefault("source_request", "")
    capabilities = _ensure_dict(normalized, "capabilities")
    primary = str(capabilities.get("primary") or "").strip()
    capabilities.setdefault("required", [primary] if primary else [])
    scene = _ensure_dict(normalized, "scene")
    scene.setdefault("environment_intent", "minimal physics test environment")
    scene.setdefault("coordinate_system", "z_up")
    scene.setdefault("duration_s", 3.0)
    scene.setdefault("bounds_hint_m", None)
    timebase = _ensure_dict(normalized, "timebase")
    timebase.setdefault("physics_hz", 120)
    timebase.setdefault("observation_fps", 24)
    timebase.setdefault("deterministic_seed", 42)
    constraints = _ensure_dict(normalized, "backend_constraints")
    constraints.setdefault("required_solver_capabilities", [])
    constraints.setdefault("allowed_solvers", [])
    constraints.setdefault("render_backend", None)
    constraints.setdefault("allow_multi_backend", True)
    policy = _ensure_dict(normalized, "asset_policy")
    policy.setdefault("allow_local", True)
    policy.setdefault("allow_external", False)
    policy.setdefault("allow_generation", False)
    policy.setdefault("allow_analytic_proxy", True)
    policy.setdefault("required_license_tier", "local_preview")
    for key, default in (
        ("objects", []),
        ("relations", []),
        ("events", []),
        ("expected_behavior", {}),
        ("observation_requirements", {}),
        ("verification_requirements", {}),
        ("variant", {}),
        ("provenance", {}),
    ):
        normalized.setdefault(key, copy.deepcopy(default))
    normalized.setdefault("notes", "")
    for obj in normalized.get("objects") or []:
        if not isinstance(obj, dict):
            continue
        asset = obj.get("asset")
        for request in asset_requests(asset):
            acquisition = request.setdefault("acquisition", {})
            if not isinstance(acquisition, dict):
                continue
            acquisition.setdefault("route", "default")
            acquisition.setdefault("requirement", "preferred")
            acquisition.setdefault("origin", "system_default")
            acquisition.setdefault("provider_hint", None)
            acquisition.setdefault("source_uri_hint", None)
            acquisition.setdefault("reference_inputs", [])
            acquisition.setdefault("fallback_order", [])
    return normalized


def validate_case_spec_v2(
    data: Mapping[str, Any],
    *,
    available_input_ids: Iterable[str] | None = None,
) -> None:
    issues = collect_case_spec_v2_issues(data, available_input_ids=available_input_ids)
    if issues:
        raise CaseSpecV2ValidationError(issues)


def collect_case_spec_v2_issues(
    data: Mapping[str, Any],
    *,
    available_input_ids: Iterable[str] | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if data.get("schema_version") != CASE_SPEC_V2_SCHEMA_VERSION:
        _issue(issues, "/schema_version", "invalid_schema_version", "must be harness_case_spec_v2")
    unknown = sorted(str(key) for key in data if str(key) not in TOP_LEVEL_FIELDS)
    if unknown:
        _issue(issues, "/", "unknown_fields", f"unsupported fields: {', '.join(unknown)}")

    identity = _mapping(data.get("identity"), "/identity", issues)
    case_id = _nonempty_string(identity.get("case_id"), "/identity/case_id", issues)
    _nonempty_string(identity.get("title"), "/identity/title", issues)
    if not isinstance(identity.get("source_request"), str):
        _issue(issues, "/identity/source_request", "invalid_type", "must be a string")

    capabilities = _mapping(data.get("capabilities"), "/capabilities", issues)
    primary = _nonempty_string(capabilities.get("primary"), "/capabilities/primary", issues)
    required_capabilities = _string_list(capabilities.get("required"), "/capabilities/required", issues)
    canonical_primary = canonical_capability_id(primary)
    if primary and canonical_primary not in {canonical_capability_id(value) for value in required_capabilities}:
        _issue(issues, "/capabilities/required", "primary_missing", "must contain the primary capability")
    if primary:
        try:
            CapabilityStore().get(canonical_primary)
        except FileNotFoundError:
            _issue(issues, "/capabilities/primary", "unsupported_capability", f"capability is not registered: {primary}")
    for index, capability_id in enumerate(required_capabilities):
        canonical_required = canonical_capability_id(capability_id)
        try:
            CapabilityStore().get(canonical_required)
        except FileNotFoundError:
            _issue(
                issues,
                f"/capabilities/required/{index}",
                "unsupported_capability",
                f"capability is not registered: {capability_id}",
            )
            continue
        if primary and canonical_required != canonical_primary:
            _issue(
                issues,
                f"/capabilities/required/{index}",
                "additional_required_capability_unsupported",
                "the current Runtime Compiler executes exactly one primary capability; "
                "additional required capabilities must wait for an explicit multi-capability compiler",
            )

    scene = _mapping(data.get("scene"), "/scene", issues)
    _nonempty_string(scene.get("environment_intent"), "/scene/environment_intent", issues)
    if scene.get("coordinate_system") != "z_up":
        _issue(issues, "/scene/coordinate_system", "unsupported_coordinate_system", "only z_up is currently supported")
    _positive_number(scene.get("duration_s"), "/scene/duration_s", issues)
    if scene.get("bounds_hint_m") is not None:
        _positive_vec3(scene.get("bounds_hint_m"), "/scene/bounds_hint_m", issues)

    timebase = _mapping(data.get("timebase"), "/timebase", issues)
    physics_hz = _positive_integer(timebase.get("physics_hz"), "/timebase/physics_hz", issues)
    observation_fps = _positive_integer(timebase.get("observation_fps"), "/timebase/observation_fps", issues)
    if physics_hz and observation_fps and physics_hz % observation_fps != 0:
        _issue(issues, "/timebase", "non_integral_sampling_ratio", "physics_hz must be an integer multiple of observation_fps")
    if not isinstance(timebase.get("deterministic_seed"), int) or isinstance(timebase.get("deterministic_seed"), bool):
        _issue(issues, "/timebase/deterministic_seed", "invalid_type", "must be an integer")

    backend = _mapping(data.get("backend_constraints"), "/backend_constraints", issues)
    required_solver_capabilities = _string_list(
        backend.get("required_solver_capabilities"),
        "/backend_constraints/required_solver_capabilities",
        issues,
    )
    for index, solver_capability in enumerate(required_solver_capabilities):
        if solver_capability not in REGISTERED_SOLVER_CAPABILITIES:
            _issue(
                issues,
                f"/backend_constraints/required_solver_capabilities/{index}",
                "unsupported_solver_capability",
                f"solver capability is not registered: {solver_capability}",
            )
    allowed_solvers = _string_list(backend.get("allowed_solvers"), "/backend_constraints/allowed_solvers", issues)
    unsupported_solvers = [value for value in allowed_solvers if value not in BACKEND_SOLVERS]
    if unsupported_solvers:
        _issue(
            issues,
            "/backend_constraints/allowed_solvers",
            "unsupported_backend",
            f"unregistered solvers: {', '.join(unsupported_solvers)}",
        )
    capability_solvers = CAPABILITY_ALLOWED_SOLVERS.get(
        canonical_primary,
        DEFAULT_CAPABILITY_ALLOWED_SOLVERS,
    )
    incompatible_solvers = [
        value
        for value in allowed_solvers
        if value in BACKEND_SOLVERS and value not in capability_solvers
    ]
    if incompatible_solvers:
        _issue(
            issues,
            "/backend_constraints/allowed_solvers",
            "unsupported_capability_backend",
            f"{primary} does not support: {', '.join(incompatible_solvers)}",
        )
    render_backend = backend.get("render_backend")
    if render_backend is not None and not isinstance(render_backend, str):
        _issue(issues, "/backend_constraints/render_backend", "invalid_type", "must be null or a string")
    elif render_backend is not None and render_backend not in BACKEND_SOLVERS:
        _issue(
            issues,
            "/backend_constraints/render_backend",
            "unsupported_backend",
            f"backend is not registered: {render_backend}",
        )
    if not isinstance(backend.get("allow_multi_backend"), bool):
        _issue(issues, "/backend_constraints/allow_multi_backend", "invalid_type", "must be boolean")

    policy = _mapping(data.get("asset_policy"), "/asset_policy", issues)
    for key in ("allow_local", "allow_external", "allow_generation", "allow_analytic_proxy"):
        if not isinstance(policy.get(key), bool):
            _issue(issues, f"/asset_policy/{key}", "invalid_type", "must be boolean")
    if policy.get("required_license_tier") not in LICENSE_TIERS:
        _issue(issues, "/asset_policy/required_license_tier", "invalid_enum", f"must be one of {sorted(LICENSE_TIERS)}")

    objects = data.get("objects")
    if not isinstance(objects, list) or not objects:
        _issue(issues, "/objects", "missing_objects", "must be a non-empty list")
        objects = []
    object_ids: list[str] = []
    allowed_inputs = set(str(value) for value in available_input_ids) if available_input_ids is not None else None
    for index, obj in enumerate(objects):
        path = f"/objects/{index}"
        if not isinstance(obj, Mapping):
            _issue(issues, path, "invalid_type", "must be an object")
            continue
        object_id = _nonempty_string(obj.get("id"), f"{path}/id", issues)
        if object_id:
            object_ids.append(object_id)
        _nonempty_string(obj.get("role"), f"{path}/role", issues)
        geometry = _mapping(obj.get("geometry"), f"{path}/geometry", issues, required=False)
        if geometry.get("approx_size_m") is not None:
            _positive_vec3(geometry.get("approx_size_m"), f"{path}/geometry/approx_size_m", issues)
        scale_policy = geometry.get("scale_policy")
        if scale_policy is not None and scale_policy not in GEOMETRY_SCALE_POLICIES:
            _issue(
                issues,
                f"{path}/geometry/scale_policy",
                "invalid_enum",
                f"must be one of {sorted(GEOMETRY_SCALE_POLICIES)}",
            )
        if scale_policy == "fit_uniform_to_approx_size" and geometry.get("approx_size_m") is None:
            _issue(
                issues,
                f"{path}/geometry/scale_policy",
                "scale_target_missing",
                "fit_uniform_to_approx_size requires geometry.approx_size_m",
            )
        physics = _mapping(obj.get("physics"), f"{path}/physics", issues, required=False)
        if physics:
            if physics.get("body_type") not in BODY_TYPES:
                _issue(issues, f"{path}/physics/body_type", "invalid_enum", f"must be one of {sorted(BODY_TYPES)}")
            if physics.get("mass_kg") is not None:
                _positive_number(physics.get("mass_kg"), f"{path}/physics/mass_kg", issues)
            if physics.get("collision_required") is not None and not isinstance(physics.get("collision_required"), bool):
                _issue(issues, f"{path}/physics/collision_required", "invalid_type", "must be boolean")
        initial = _mapping(obj.get("initial_state"), f"{path}/initial_state", issues, required=False)
        for field in ("position_m", "rotation_deg", "linear_velocity_m_s", "angular_velocity_rad_s"):
            if initial.get(field) is not None:
                _finite_vec3(initial.get(field), f"{path}/initial_state/{field}", issues)
        role_text = f"{obj.get('role', '')} {geometry.get('shape_hint', '')}".casefold()
        rotation = initial.get("rotation_deg")
        if (
            "box" in str(geometry.get("shape_hint") or "").casefold()
            and any(token in role_text for token in ("ramp", "inclined", "slope"))
            and isinstance(rotation, list)
            and len(rotation) == 3
            and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in rotation)
            and abs(float(rotation[0])) <= 1e-9
            and abs(float(rotation[2])) <= 1e-9
        ):
            _issue(
                issues,
                f"{path}/initial_state/rotation_deg",
                "ramp_has_no_incline_rotation",
                "rotation_deg is [pitch, yaw, roll]; a box ramp needs non-zero pitch or roll, not yaw alone",
            )
        behavior = _mapping(obj.get("behavior"), f"{path}/behavior", issues, required=False)
        declared_energy = behavior.get("initial_kinetic_energy_j")
        if declared_energy is not None:
            _validate_initial_kinetic_energy(physics, initial, declared_energy, f"{path}/behavior/initial_kinetic_energy_j", issues)
        raw_asset = obj.get("asset")
        if raw_asset is not None and not isinstance(raw_asset, Mapping):
            _issue(issues, f"{path}/asset", "invalid_type", "must be an object")
        elif isinstance(raw_asset, Mapping) and raw_asset and not {
            "description",
            "resource_kind",
            "must",
            "preferences",
            "acquisition",
        }.intersection(raw_asset):
            _issue(
                issues,
                f"{path}/asset",
                "unsupported_asset_shape",
                "V2 currently supports one direct asset request per object",
            )
        if not asset_requests(raw_asset) and not policy.get("allow_analytic_proxy"):
            _issue(
                issues,
                f"{path}/asset",
                "asset_required",
                "an asset request is required when analytic proxies are disabled",
            )
        for request_index, request in enumerate(asset_requests(raw_asset)):
            request_path = f"{path}/asset" if request_index == 0 else f"{path}/asset/slot_{request_index}"
            _validate_asset_request(request, request_path, policy, allowed_inputs, issues)

    duplicates = sorted({value for value in object_ids if object_ids.count(value) > 1})
    if duplicates:
        _issue(issues, "/objects", "duplicate_object_ids", f"duplicate ids: {', '.join(duplicates)}")
    known_objects = set(object_ids)
    _validate_references(data.get("relations"), "/relations", known_objects, issues)
    _validate_references(data.get("events"), "/events", known_objects, issues)
    _validate_event_payloads(data.get("events"), issues)
    _validate_support_footprints(objects, data.get("relations"), issues)
    verification = _mapping(data.get("verification_requirements"), "/verification_requirements", issues)
    assertions = verification.get("assertions")
    _validate_references(assertions, "/verification_requirements/assertions", known_objects, issues)
    if isinstance(assertions, list):
        for index, assertion in enumerate(assertions):
            if not isinstance(assertion, Mapping):
                continue
            assertion_type = _nonempty_string(
                assertion.get("type"),
                f"/verification_requirements/assertions/{index}/type",
                issues,
            )
            if assertion_type and assertion_type not in VERIFICATION_ASSERTION_TYPES:
                _issue(
                    issues,
                    f"/verification_requirements/assertions/{index}/type",
                    "unsupported_verification_assertion",
                    f"unsupported assertion type: {assertion_type}",
                )
    observation = _mapping(data.get("observation_requirements"), "/observation_requirements", issues)
    cameras = observation.get("cameras")
    _validate_references(cameras, "/observation_requirements/cameras", known_objects, issues)
    if isinstance(cameras, list):
        for index, camera in enumerate(cameras):
            if not isinstance(camera, Mapping):
                continue
            role = _nonempty_string(camera.get("role"), f"/observation_requirements/cameras/{index}/role", issues)
            if role and role not in CAMERA_ROLES:
                _issue(
                    issues,
                    f"/observation_requirements/cameras/{index}/role",
                    "unsupported_camera_role",
                    f"unsupported camera role: {role}",
                )
    for key in ("modalities", "signals"):
        if observation.get(key) is not None:
            values = _string_list(observation.get(key), f"/observation_requirements/{key}", issues)
            if key == "modalities":
                unsupported_modalities = [value for value in values if value not in OBSERVATION_MODALITIES]
                if unsupported_modalities:
                    _issue(
                        issues,
                        "/observation_requirements/modalities",
                        "unsupported_modality",
                        f"unsupported modalities: {', '.join(unsupported_modalities)}",
                    )
    _mapping(data.get("expected_behavior"), "/expected_behavior", issues)
    variant = _mapping(data.get("variant"), "/variant", issues)
    if variant.get("should_pass") is not None and not isinstance(variant.get("should_pass"), bool):
        _issue(issues, "/variant/should_pass", "invalid_type", "must be boolean")
    _mapping(data.get("provenance"), "/provenance", issues)
    if not isinstance(data.get("notes"), str):
        _issue(issues, "/notes", "invalid_type", "must be a string")
    if case_id and not all(character.isalnum() or character in {"_", "-", "."} for character in case_id):
        _issue(issues, "/identity/case_id", "invalid_case_id", "may contain only letters, numbers, underscore, dash, and dot")
    return issues


def project_case_spec_v2_to_v1(case_spec: CaseSpecV2) -> CaseSpec:
    data = case_spec.data
    allow_analytic_proxy = bool((data.get("asset_policy") or {}).get("allow_analytic_proxy"))
    projected_objects = [
        _project_object(
            obj,
            force_analytic_proxy=allow_analytic_proxy and not asset_requests(obj.get("asset")),
        )
        for obj in case_spec.objects
    ]
    _project_release_events(data.get("events") or [], projected_objects)
    active, passive = _infer_active_passive(projected_objects)
    capability_id = case_spec.capability_id
    capability = CapabilityStore().get(capability_id)
    observation = data.get("observation_requirements") or {}
    expected = copy.deepcopy(data.get("expected_behavior") or {})
    expected.setdefault("coordinate_system", (data.get("scene") or {}).get("coordinate_system", "z_up"))
    collision_graph = _collision_graph(data.get("relations") or [])
    if collision_graph and "collision_graph" not in expected:
        expected["collision_graph"] = collision_graph
    support_map = _support_map(data.get("relations") or [])
    if support_map and "support" not in expected:
        expected["support"] = support_map
    assertions = [item for item in (data.get("verification_requirements") or {}).get("assertions", []) if isinstance(item, dict)]
    verification_rules = [str(item.get("type")) for item in assertions if item.get("type")]
    required_assets = [
        str(request.get("description"))
        for obj in case_spec.objects
        for request in asset_requests(obj.get("asset"))
        if request.get("description")
    ]
    requested_signals = [str(value) for value in observation.get("signals") or []]
    required_signals = list(dict.fromkeys([*capability.required_signals, *requested_signals]))
    scene = data.get("scene") or {}
    variant = data.get("variant") or {}
    projected_timebase = copy.deepcopy(data.get("timebase") or {})
    projected_timebase["render_fps"] = int(projected_timebase.get("observation_fps") or 24)
    projection = {
        "schema_version": "harness_case_spec_v1",
        "case_id": case_spec.case_id,
        "capability_id": capability_id,
        "prompt": str((data.get("identity") or {}).get("source_request") or (data.get("identity") or {}).get("title") or ""),
        "expanded_prompt": str((data.get("identity") or {}).get("title") or ""),
        "task_type": str(variant.get("task_type") or capability_id),
        "scene": {
            "layout": str(scene.get("layout") or "v2_semantic_layout"),
            "duration_s": float(scene.get("duration_s") or 3.0),
            "coordinate_system": str(scene.get("coordinate_system") or "z_up"),
            **({"map_preference": scene["map_preference"]} if scene.get("map_preference") else {}),
        },
        "timebase": projected_timebase,
        "seed": int(projected_timebase.get("deterministic_seed") or 0),
        "physical_parameters": copy.deepcopy(variant.get("physical_parameters") or {}),
        "expected_physics": expected,
        "objects": projected_objects,
        "active_objects": active,
        "passive_objects": passive,
        "required_assets": required_assets,
        "required_signals": required_signals,
        "verification_rules": verification_rules,
        "asset_requirements": {
            "acquisition_routes": list(
                dict.fromkeys(
                    str((request.get("acquisition") or {}).get("route") or "default")
                    for obj in case_spec.objects
                    for request in asset_requests(obj.get("asset"))
                )
            ),
            "required_license_tier": (data.get("asset_policy") or {}).get("required_license_tier"),
        },
        "allowed_proxy_policy": (
            "analytic_proxy_allowed" if (data.get("asset_policy") or {}).get("allow_analytic_proxy") else "no_proxy"
        ),
        "verifier_expectation": {"status": "pass" if variant.get("should_pass", True) else "fail"},
        "should_pass": bool(variant.get("should_pass", True)),
        "notes": str(data.get("notes") or ""),
        "v2_projection": {
            "source_schema_version": CASE_SPEC_V2_SCHEMA_VERSION,
            "projection_version": "harness_case_spec_v2_to_v1_projection_v1",
            "source_digest": stable_case_spec_digest(data),
            "source_provenance": copy.deepcopy(data.get("provenance") or {}),
        },
    }
    validate_case_spec(projection)
    return CaseSpec(projection)


def asset_requests(asset: Any) -> list[dict[str, Any]]:
    if not isinstance(asset, dict):
        return []
    direct_fields = {"description", "resource_kind", "must", "preferences", "acquisition"}
    if direct_fields.intersection(asset):
        return [asset]
    requests: list[dict[str, Any]] = []
    for value in asset.values():
        if isinstance(value, dict):
            requests.append(value)
    return requests


def stable_case_spec_digest(data: Mapping[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _project_object(
    obj: Mapping[str, Any],
    *,
    force_analytic_proxy: bool,
) -> dict[str, Any]:
    geometry = obj.get("geometry") if isinstance(obj.get("geometry"), Mapping) else {}
    physics = obj.get("physics") if isinstance(obj.get("physics"), Mapping) else {}
    initial = obj.get("initial_state") if isinstance(obj.get("initial_state"), Mapping) else {}
    behavior = obj.get("behavior") if isinstance(obj.get("behavior"), Mapping) else {}
    requests = asset_requests(obj.get("asset"))
    primary = requests[0] if requests else {}
    shape = str(geometry.get("shape_hint") or "box")
    projected: dict[str, Any] = {
        "id": str(obj.get("id") or ""),
        "role": str(obj.get("role") or "visual_object"),
        "shape": shape,
        "initial_position_m": _vec3_or_default(initial.get("position_m"), [0.0, 0.0, 0.0]),
        "initial_rotation_deg": _vec3_or_default(initial.get("rotation_deg"), [0.0, 0.0, 0.0]),
        "initial_velocity_m_s": _vec3_or_default(initial.get("linear_velocity_m_s"), [0.0, 0.0, 0.0]),
        "asset_query": str(primary.get("description") or obj.get("role") or obj.get("id") or "asset"),
    }
    if force_analytic_proxy:
        projected["force_analytic_proxy"] = True
        projected["asset_policy"] = "analytic_proxy"
    size = geometry.get("approx_size_m")
    if isinstance(size, list) and len(size) == 3:
        projected["size_m"] = [float(value) for value in size]
        if "sphere" in shape.casefold():
            projected["radius_m"] = max(float(value) for value in size) / 2.0
    if geometry.get("scale_policy") is not None:
        projected["asset_scale_policy"] = str(geometry["scale_policy"])
    for source, target in (
        ("mass_kg", "mass_kg"),
        ("material", "material"),
        ("collision_profile", "collision_profile"),
        ("linear_damping", "linear_damping"),
        ("angular_damping", "angular_damping"),
        ("enable_gravity", "enable_gravity"),
        ("use_ccd", "use_ccd"),
    ):
        if physics.get(source) is not None:
            projected[target] = copy.deepcopy(physics[source])
    if physics.get("collision_required"):
        projected["collider"] = str(physics.get("collider") or _collider_for_shape(shape))
    body_type = str(physics.get("body_type") or "dynamic")
    projected["kinematic"] = body_type in {"static", "kinematic"}
    if physics.get("body_type") is not None:
        projected["body_type"] = body_type
    if physics.get("collision_required") is not None:
        projected["collision_required"] = bool(physics["collision_required"])
    if (
        "use_ccd" not in projected
        and body_type == "dynamic"
        and physics.get("collision_required") is not False
        and math.sqrt(sum(float(value) ** 2 for value in projected["initial_velocity_m_s"])) >= 2.0
    ):
        projected["use_ccd"] = True
    if initial.get("angular_velocity_rad_s") is not None:
        projected["initial_angular_velocity_rad_s"] = copy.deepcopy(initial["angular_velocity_rad_s"])
    fracture = behavior.get("fracture") if isinstance(behavior.get("fracture"), Mapping) else None
    if fracture:
        projected["fracture_response"] = copy.deepcopy(dict(fracture))
    return projected


def _infer_active_passive(objects: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    active: list[str] = []
    passive: list[str] = []
    for obj in objects:
        object_id = str(obj.get("id") or "")
        role = str(obj.get("role") or "").casefold()
        velocity = obj.get("initial_velocity_m_s") or [0.0, 0.0, 0.0]
        moving = any(abs(float(value)) > 1e-9 for value in velocity)
        if moving or any(token in role for token in ("active", "projectile", "striker", "driver", "falling", "launched")):
            active.append(object_id)
        elif not any(token in role for token in ("support", "floor", "ground", "field", "anchor")):
            passive.append(object_id)
    return active, passive


def _project_release_events(events: Iterable[Any], objects: list[dict[str, Any]]) -> None:
    """Carry V2 delayed-release semantics into the legacy UE runtime contract."""
    by_id = {str(obj.get("id") or ""): obj for obj in objects}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = str(event.get("type") or "").casefold().replace("-", "_").replace(" ", "_")
        if event_type not in {"release", "delayed_release", "staged_release"}:
            continue
        object_id = str(event.get("object") or event.get("actor") or "")
        projected = by_id.get(object_id)
        if projected is None:
            continue
        raw_time = event.get("time_s", event.get("time"))
        if raw_time is None or isinstance(raw_time, bool):
            continue
        try:
            release_time = float(raw_time)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(release_time) or release_time < 0.0:
            continue
        projected["release_time_s"] = release_time
        event_velocity = event.get("linear_velocity_m_s")
        projected["release_velocity_m_s"] = (
            [float(value) for value in event_velocity]
            if _is_finite_vec3(event_velocity)
            else copy.deepcopy(projected["initial_velocity_m_s"])
        )
        angular_velocity_deg = event.get("angular_velocity_deg_s")
        angular_velocity_rad = event.get("angular_velocity_rad_s")
        if _is_finite_vec3(angular_velocity_deg):
            projected["release_angular_velocity_deg_s"] = [float(value) for value in angular_velocity_deg]
        elif _is_finite_vec3(angular_velocity_rad):
            projected["release_angular_velocity_deg_s"] = [
                math.degrees(float(value)) for value in angular_velocity_rad
            ]
        if release_time > 0.0:
            projected["hold_position_m"] = copy.deepcopy(projected["initial_position_m"])
            projected["release_position_m"] = copy.deepcopy(projected["initial_position_m"])


def _collision_graph(relations: Iterable[Any]) -> list[list[str]]:
    result: list[list[str]] = []
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        relation_type = str(relation.get("type") or "").casefold()
        if relation_type not in {"contact", "collision", "cascade_collision", "impacts", "hits", "contact_order"}:
            continue
        values = relation.get("objects")
        if not isinstance(values, list):
            values = [relation.get("source"), relation.get("target")]
        pair = [str(value) for value in values if value]
        if len(pair) >= 2:
            result.append(pair[:2])
    return result


def _support_map(relations: Iterable[Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        relation_type = str(relation.get("type") or "").casefold().replace("-", "_").replace(" ", "_")
        source = relation.get("source")
        target = relation.get("target")
        if not source or not target:
            continue
        if relation_type in {"support", "supported_by", "rests_on", "on"}:
            result[str(source)] = str(target)
        elif relation_type == "supports":
            result[str(target)] = str(source)
    return result


def _validate_asset_request(
    request: Mapping[str, Any],
    path: str,
    policy: Mapping[str, Any],
    available_input_ids: set[str] | None,
    issues: list[ValidationIssue],
) -> None:
    unknown_fields = sorted(str(key) for key in request if str(key) not in ASSET_REQUEST_FIELDS)
    if unknown_fields:
        _issue(issues, path, "unknown_asset_fields", f"unsupported fields: {', '.join(unknown_fields)}")
    if request.get("description") is not None and not isinstance(request.get("description"), str):
        _issue(issues, f"{path}/description", "invalid_type", "must be a string")
    resource_kind = request.get("resource_kind")
    if resource_kind is not None and resource_kind not in RESOURCE_KINDS:
        _issue(issues, f"{path}/resource_kind", "invalid_enum", f"must be one of {sorted(RESOURCE_KINDS)}")
    must = _mapping(request.get("must"), f"{path}/must", issues, required=False)
    unknown_must = sorted(str(key) for key in must if str(key) not in ASSET_MUST_FIELDS)
    if unknown_must:
        _issue(issues, f"{path}/must", "unknown_hard_fields", f"unsupported fields: {', '.join(unknown_must)}")
    must_not = _mapping(request.get("must_not"), f"{path}/must_not", issues, required=False)
    unknown_must_not = sorted(str(key) for key in must_not if str(key) not in ASSET_MUST_NOT_FIELDS)
    if unknown_must_not:
        _issue(issues, f"{path}/must_not", "unknown_hard_fields", f"unsupported fields: {', '.join(unknown_must_not)}")
    if must.get("approx_size_m") is not None:
        _positive_vec3(must.get("approx_size_m"), f"{path}/must/approx_size_m", issues)
    _mapping(request.get("preferences"), f"{path}/preferences", issues, required=False)
    _mapping(request.get("taxonomy"), f"{path}/taxonomy", issues, required=False)
    _mapping(request.get("relaxation_policy"), f"{path}/relaxation_policy", issues, required=False)
    acquisition = _mapping(request.get("acquisition"), f"{path}/acquisition", issues, required=False)
    if not acquisition:
        return
    route = str(acquisition.get("route") or "default")
    requirement = str(acquisition.get("requirement") or "preferred")
    origin = str(acquisition.get("origin") or "system_default")
    if route not in ACQUISITION_ROUTES:
        _issue(issues, f"{path}/acquisition/route", "invalid_enum", f"must be one of {sorted(ACQUISITION_ROUTES)}")
    if requirement not in ACQUISITION_REQUIREMENTS:
        _issue(issues, f"{path}/acquisition/requirement", "invalid_enum", f"must be one of {sorted(ACQUISITION_REQUIREMENTS)}")
    if origin not in ACQUISITION_ORIGINS:
        _issue(issues, f"{path}/acquisition/origin", "invalid_enum", f"must be one of {sorted(ACQUISITION_ORIGINS)}")
    if requirement == "required" and origin != "user_explicit":
        _issue(
            issues,
            f"{path}/acquisition/requirement",
            "inferred_hard_requirement",
            "required acquisition routes must originate from an explicit user requirement",
        )
    if requirement == "required" and route == "default":
        _issue(
            issues,
            f"{path}/acquisition/route",
            "required_default_route",
            "a required acquisition route must name a specific method",
        )
    _validate_route_policy(route, policy, f"{path}/acquisition/route", issues)
    for field in ("provider_hint", "source_uri_hint"):
        if acquisition.get(field) is not None and not isinstance(acquisition.get(field), str):
            _issue(issues, f"{path}/acquisition/{field}", "invalid_type", "must be null or a string")
    fallback = _string_list(acquisition.get("fallback_order"), f"{path}/acquisition/fallback_order", issues)
    invalid_fallback = [value for value in fallback if value not in ACQUISITION_ROUTES or value == "default"]
    if invalid_fallback:
        _issue(issues, f"{path}/acquisition/fallback_order", "invalid_route", f"invalid fallback routes: {', '.join(invalid_fallback)}")
    if requirement == "required" and fallback:
        _issue(issues, f"{path}/acquisition/fallback_order", "required_route_has_fallback", "required acquisition routes cannot silently change source")
    if len(fallback) != len(set(fallback)) or route in fallback:
        _issue(
            issues,
            f"{path}/acquisition/fallback_order",
            "invalid_fallback_order",
            "fallback routes must be unique and must not repeat the primary route",
        )
    for index, fallback_route in enumerate(fallback):
        _validate_route_policy(
            fallback_route,
            policy,
            f"{path}/acquisition/fallback_order/{index}",
            issues,
        )
    reference_inputs = acquisition.get("reference_inputs") or []
    if not isinstance(reference_inputs, list):
        _issue(issues, f"{path}/acquisition/reference_inputs", "invalid_type", "must be a list")
        return
    for index, reference in enumerate(reference_inputs):
        reference_path = f"{path}/acquisition/reference_inputs/{index}"
        if not isinstance(reference, Mapping):
            _issue(issues, reference_path, "invalid_type", "must be an object")
            continue
        input_id = _nonempty_string(reference.get("input_id"), f"{reference_path}/input_id", issues)
        if available_input_ids is not None and input_id and input_id not in available_input_ids:
            _issue(issues, f"{reference_path}/input_id", "unknown_input", f"request input does not exist: {input_id}")
        usage = _string_list(reference.get("usage"), f"{reference_path}/usage", issues)
        invalid_usage = [value for value in usage if value not in REFERENCE_INPUT_USAGES]
        if invalid_usage:
            _issue(issues, f"{reference_path}/usage", "invalid_usage", f"unsupported usages: {', '.join(invalid_usage)}")
        allow_search = reference.get("allow_similarity_search", True)
        if not isinstance(allow_search, bool):
            _issue(issues, f"{reference_path}/allow_similarity_search", "invalid_type", "must be boolean")
        if "similarity_search" in usage and allow_search is False:
            _issue(issues, reference_path, "contradictory_image_use", "similarity_search usage conflicts with allow_similarity_search=false")


def _validate_route_policy(
    route: str,
    policy: Mapping[str, Any],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    if route == "local_catalog" and not policy.get("allow_local"):
        _issue(issues, path, "route_disallowed", "local_catalog conflicts with asset_policy.allow_local=false")
    if route == "external_site" and not policy.get("allow_external"):
        _issue(issues, path, "route_disallowed", "external_site requires asset_policy.allow_external=true")
    if route in {"procedural_generation", "model_generation"} and not policy.get("allow_generation"):
        _issue(issues, path, "route_disallowed", f"{route} requires asset_policy.allow_generation=true")


def _validate_references(
    values: Any,
    path: str,
    known_objects: set[str],
    issues: list[ValidationIssue],
) -> None:
    if values is None:
        return
    if not isinstance(values, list):
        _issue(issues, path, "invalid_type", "must be a list")
        return
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            _issue(issues, f"{path}/{index}", "invalid_type", "must be an object")
            continue
        references: list[str] = []
        for key in ("object", "source", "target", "actor", "target_object", "subject"):
            if value.get(key):
                references.append(str(value[key]))
        for key in ("objects", "object_ids", "target_objects"):
            if isinstance(value.get(key), list):
                references.extend(str(item) for item in value[key])
        for reference in references:
            if reference not in known_objects:
                _issue(issues, f"{path}/{index}", "unknown_object_reference", f"unknown object id: {reference}")


def _validate_support_footprints(
    objects: Iterable[Any],
    relations: Any,
    issues: list[ValidationIssue],
) -> None:
    if not isinstance(relations, list):
        return
    by_id: dict[str, tuple[int, Mapping[str, Any]]] = {
        str(obj.get("id")): (index, obj)
        for index, obj in enumerate(objects)
        if isinstance(obj, Mapping) and obj.get("id")
    }
    for subject_id, support_id in _support_map(relations).items():
        subject_entry = by_id.get(subject_id)
        support_entry = by_id.get(support_id)
        if subject_entry is None or support_entry is None:
            continue
        _, subject = subject_entry
        support_index, support = support_entry
        subject_geometry = subject.get("geometry") if isinstance(subject.get("geometry"), Mapping) else {}
        support_geometry = support.get("geometry") if isinstance(support.get("geometry"), Mapping) else {}
        subject_initial = subject.get("initial_state") if isinstance(subject.get("initial_state"), Mapping) else {}
        support_initial = support.get("initial_state") if isinstance(support.get("initial_state"), Mapping) else {}
        subject_size = subject_geometry.get("approx_size_m")
        support_size = support_geometry.get("approx_size_m")
        subject_position = subject_initial.get("position_m")
        support_position = support_initial.get("position_m")
        vectors = (subject_size, support_size, subject_position, support_position)
        if not all(
            isinstance(vector, list)
            and len(vector) == 3
            and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in vector)
            for vector in vectors
        ):
            continue
        margins = [
            float(support_size[axis]) / 2.0
            - abs(float(subject_position[axis]) - float(support_position[axis]))
            - float(subject_size[axis]) / 2.0
            for axis in (0, 1)
        ]
        if any(margin < 0.0 for margin in margins):
            _issue(
                issues,
                f"/objects/{support_index}/geometry/approx_size_m",
                "support_footprint_too_small",
                f"support {support_id} does not contain the full horizontal bounds of {subject_id}; enlarge or reposition it",
            )


def _validate_event_payloads(events: Any, issues: list[ValidationIssue]) -> None:
    if not isinstance(events, list):
        return
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            continue
        event_type = str(event.get("type") or "").casefold().replace("-", "_").replace(" ", "_")
        if event_type not in {"release", "delayed_release", "staged_release"}:
            continue
        path = f"/events/{index}"
        raw_time = event.get("time_s", event.get("time"))
        if (
            not isinstance(raw_time, (int, float))
            or isinstance(raw_time, bool)
            or not math.isfinite(float(raw_time))
            or float(raw_time) < 0.0
        ):
            _issue(issues, f"{path}/time_s", "invalid_release_time", "must be a non-negative finite number")
        for field in ("linear_velocity_m_s", "angular_velocity_rad_s", "angular_velocity_deg_s"):
            if event.get(field) is not None:
                _finite_vec3(event.get(field), f"{path}/{field}", issues)


def _mapping(
    value: Any,
    path: str,
    issues: list[ValidationIssue],
    *,
    required: bool = True,
) -> Mapping[str, Any]:
    if value is None and not required:
        return {}
    if not isinstance(value, Mapping):
        _issue(issues, path, "invalid_type", "must be an object")
        return {}
    return value


def _ensure_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        value = {}
        data[key] = value
    return value if isinstance(value, dict) else {}


def _nonempty_string(value: Any, path: str, issues: list[ValidationIssue]) -> str:
    if not isinstance(value, str):
        _issue(issues, path, "invalid_type", "must be a string")
        return ""
    text = value.strip()
    if not text:
        _issue(issues, path, "missing_value", "must be a non-empty string")
    return text


def _string_list(value: Any, path: str, issues: list[ValidationIssue]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        _issue(issues, path, "invalid_type", "must be a list of non-empty strings")
        return []
    return [item.strip() for item in value]


def _positive_integer(value: Any, path: str, issues: list[ValidationIssue]) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        _issue(issues, path, "invalid_number", "must be a positive integer")
        return 0
    return value


def _positive_number(value: Any, path: str, issues: list[ValidationIssue]) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0.0:
        _issue(issues, path, "invalid_number", "must be a positive finite number")
        return 0.0
    return float(value)


def _finite_vec3(value: Any, path: str, issues: list[ValidationIssue]) -> list[float]:
    if not _is_finite_vec3(value):
        _issue(issues, path, "invalid_vector", "must contain three finite numbers")
        return []
    return [float(item) for item in value]


def _is_finite_vec3(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(
        isinstance(item, (int, float))
        and not isinstance(item, bool)
        and math.isfinite(float(item))
        for item in value
    )


def _positive_vec3(value: Any, path: str, issues: list[ValidationIssue]) -> list[float]:
    values = _finite_vec3(value, path, issues)
    if values and any(item <= 0.0 for item in values):
        _issue(issues, path, "invalid_vector", "all components must be positive")
        return []
    return values


def _validate_initial_kinetic_energy(
    physics: Mapping[str, Any],
    initial: Mapping[str, Any],
    declared: Any,
    path: str,
    issues: list[ValidationIssue],
) -> None:
    if not isinstance(declared, (int, float)) or isinstance(declared, bool) or float(declared) < 0.0:
        _issue(issues, path, "invalid_number", "must be a non-negative number")
        return
    mass = physics.get("mass_kg")
    velocity = initial.get("linear_velocity_m_s")
    if (
        not isinstance(mass, (int, float))
        or isinstance(mass, bool)
        or not math.isfinite(float(mass))
        or not isinstance(velocity, list)
        or len(velocity) != 3
        or any(
            not isinstance(component, (int, float))
            or isinstance(component, bool)
            or not math.isfinite(float(component))
            for component in velocity
        )
    ):
        _issue(issues, path, "energy_inputs_missing", "requires physics.mass_kg and initial_state.linear_velocity_m_s")
        return
    expected = 0.5 * float(mass) * sum(float(component) ** 2 for component in velocity)
    if not math.isclose(float(declared), expected, rel_tol=1e-6, abs_tol=1e-6):
        _issue(issues, path, "kinetic_energy_mismatch", f"declared={float(declared)} but 0.5*m*v^2={expected}")


def _vec3_or_default(value: Any, default: list[float]) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        return list(default)
    return [float(item) for item in value]


def _collider_for_shape(shape: str) -> str:
    normalized = shape.casefold()
    if "sphere" in normalized or "ball" in normalized:
        return "sphere"
    if "capsule" in normalized:
        return "capsule"
    if "cylinder" in normalized:
        return "cylinder"
    return "box"


def _issue(issues: list[ValidationIssue], path: str, code: str, message: str) -> None:
    issues.append(ValidationIssue(path=path, code=code, message=message))
