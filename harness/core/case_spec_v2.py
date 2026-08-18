from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from harness.assets.providers.local_procedural_mesh import (
    GENERIC_PROVIDER_ALIASES,
    RECIPE_BY_SHAPE,
)
from harness.core.capability import CapabilityStore, canonical_capability_id
from harness.core.physics_contract import allowed_backends_for_scene
from harness.core.runtime_case import RUNTIME_CASE_SCHEMA_VERSION, RuntimeCase


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
    "artifact_complete",
    "event_count",
    "event_exists",
    "event_sequence",
    "state_delta",
    "state_value",
    "trajectory_integrity",
}
LICENSE_TIERS = {"local_preview", "reference"}
BODY_TYPES = {"dynamic", "static", "kinematic"}
COLLISION_GEOMETRY_SHAPES = {"box", "sphere", "cylinder"}
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
    "solver_scene",
    "workspace_bounds_m",
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
    solver_scene = normalized.get("solver_scene")
    if isinstance(solver_scene, dict) and solver_scene.get("type") == "rigid_sph":
        initialization = solver_scene.setdefault("initialization", {})
        if isinstance(initialization, dict):
            initialization.setdefault("state", "settled")
            initialization.setdefault("pre_roll_s", 0.25)
            initialization.setdefault("capture_after_pre_roll", True)
    for obj in normalized.get("objects") or []:
        if not isinstance(obj, dict):
            continue
        representation = obj.setdefault("visual_representation", {})
        if isinstance(representation, dict):
            representation.setdefault("source", "asset")
            representation.setdefault("visible", True)
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
    for relation in normalized.get("relations") or []:
        if isinstance(relation, dict) and relation.get("type") is not None:
            relation["type"] = _canonical_relation_type(relation.get("type"))
    return normalized


def validate_case_spec_v2(
    data: Mapping[str, Any],
    *,
    available_input_ids: Iterable[str] | None = None,
) -> None:
    issues = collect_case_spec_v2_issues(data, available_input_ids=available_input_ids)
    if issues:
        raise CaseSpecV2ValidationError(issues)


def validate_agent_case_spec_contract(data: Mapping[str, Any]) -> None:
    """Validate strict Agent-facing contracts that old persisted V2 files may predate.

    The ordinary V2 reader remains able to inspect historical jobs. New native
    submissions and revisions use this stricter boundary before any Provider is
    called.
    """
    issues = collect_agent_case_spec_contract_issues(data)
    if issues:
        raise CaseSpecV2ValidationError(issues)


def collect_agent_case_spec_contract_issues(data: Mapping[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    recipe_shapes = {recipe: shape for shape, recipe in RECIPE_BY_SHAPE.items()}
    supported_hints = set(recipe_shapes) | set(GENERIC_PROVIDER_ALIASES)
    for index, obj in enumerate(data.get("objects") or []):
        if not isinstance(obj, Mapping):
            continue
        geometry = obj.get("geometry") if isinstance(obj.get("geometry"), Mapping) else {}
        shape = str(geometry.get("shape_hint") or "").strip().casefold()
        for request in asset_requests(obj.get("asset")):
            acquisition = request.get("acquisition") if isinstance(request.get("acquisition"), Mapping) else {}
            if acquisition.get("route") != "procedural_generation":
                continue
            shape_path = f"/objects/{index}/geometry/shape_hint"
            if shape not in RECIPE_BY_SHAPE:
                _issue(
                    issues,
                    shape_path,
                    "procedural_shape_hint_not_canonical",
                    "built-in procedural generation requires shape_hint box, sphere, or cylinder",
                )
            provider_hint = str(acquisition.get("provider_hint") or "").strip().casefold()
            if provider_hint and provider_hint not in supported_hints:
                _issue(
                    issues,
                    f"/objects/{index}/asset/acquisition/provider_hint",
                    "unsupported_procedural_provider_hint",
                    "use box_mesh_v1, sphere_mesh_v1, cylinder_mesh_v1, a registered generic local provider, or null",
                )
            expected_shape = recipe_shapes.get(provider_hint)
            if expected_shape is not None and shape in RECIPE_BY_SHAPE and shape != expected_shape:
                _issue(
                    issues,
                    shape_path,
                    "procedural_recipe_shape_mismatch",
                    f"{provider_hint} requires shape_hint={expected_shape}",
                )
            must = request.get("must") if isinstance(request.get("must"), Mapping) else {}
            geometry_type = str(must.get("geometry_type") or "").strip().casefold()
            if geometry_type and geometry_type not in RECIPE_BY_SHAPE:
                _issue(
                    issues,
                    f"/objects/{index}/asset/must/geometry_type",
                    "procedural_geometry_type_not_canonical",
                    "built-in procedural generation requires geometry_type box, sphere, or cylinder",
                )
            elif geometry_type and shape in RECIPE_BY_SHAPE and geometry_type != shape:
                _issue(
                    issues,
                    f"/objects/{index}/asset/must/geometry_type",
                    "procedural_geometry_type_mismatch",
                    "asset.must.geometry_type must match geometry.shape_hint",
                )
    return issues


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
    capability_solvers = allowed_backends_for_scene(data)
    incompatible_solvers = [
        value
        for value in allowed_solvers
        if value in BACKEND_SOLVERS and value not in capability_solvers
    ]
    if incompatible_solvers:
        _issue(
            issues,
            "/backend_constraints/allowed_solvers",
            "unsupported_scene_backend",
            f"the declared scene primitives do not support: {', '.join(incompatible_solvers)}",
        )
    registered_allowed_solvers = [solver for solver in allowed_solvers if solver in BACKEND_SOLVER_CAPABILITIES]
    required_solver_set = set(required_solver_capabilities)
    if (
        registered_allowed_solvers
        and required_solver_set
        and not any(required_solver_set <= BACKEND_SOLVER_CAPABILITIES[solver] for solver in registered_allowed_solvers)
    ):
        missing_by_solver = "; ".join(
            f"{solver}: {', '.join(sorted(required_solver_set - BACKEND_SOLVER_CAPABILITIES[solver]))}"
            for solver in registered_allowed_solvers
        )
        _issue(
            issues,
            "/backend_constraints/required_solver_capabilities",
            "solver_capability_mismatch",
            f"no allowed solver provides every required capability ({missing_by_solver})",
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
        color_rgb = obj.get("color_rgb")
        if color_rgb is not None:
            color_values = _finite_vec3(color_rgb, f"{path}/color_rgb", issues)
            if color_values and any(value < 0.0 or value > 1.0 for value in color_values):
                _issue(
                    issues,
                    f"{path}/color_rgb",
                    "invalid_color",
                    "components must be between 0 and 1",
                )
        if obj.get("fixed_material_color") is not None and not isinstance(obj.get("fixed_material_color"), bool):
            _issue(issues, f"{path}/fixed_material_color", "invalid_type", "must be boolean")
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
            if physics.get("use_ccd") is not None and not isinstance(physics.get("use_ccd"), bool):
                _issue(issues, f"{path}/physics/use_ccd", "invalid_type", "must be boolean")
            collision_geometry = physics.get("collision_geometry")
            if collision_geometry is not None:
                collision_geometry = _mapping(
                    collision_geometry,
                    f"{path}/physics/collision_geometry",
                    issues,
                )
                unknown_collision_fields = sorted(
                    str(key)
                    for key in collision_geometry
                    if str(key) not in {"shape", "size_m", "local_center_offset_m"}
                )
                if unknown_collision_fields:
                    _issue(
                        issues,
                        f"{path}/physics/collision_geometry",
                        "unknown_collision_geometry_fields",
                        f"unsupported fields: {', '.join(unknown_collision_fields)}",
                    )
                collision_shape = str(collision_geometry.get("shape") or "")
                if collision_shape not in COLLISION_GEOMETRY_SHAPES:
                    _issue(
                        issues,
                        f"{path}/physics/collision_geometry/shape",
                        "invalid_collision_geometry_shape",
                        f"must be one of {sorted(COLLISION_GEOMETRY_SHAPES)}",
                    )
                collision_size = _positive_vec3(
                    collision_geometry.get("size_m"),
                    f"{path}/physics/collision_geometry/size_m",
                    issues,
                )
                if collision_geometry.get("local_center_offset_m") is not None:
                    _finite_vec3(
                        collision_geometry.get("local_center_offset_m"),
                        f"{path}/physics/collision_geometry/local_center_offset_m",
                        issues,
                    )
                if collision_shape == "sphere" and collision_size and not _components_equal(collision_size):
                    _issue(
                        issues,
                        f"{path}/physics/collision_geometry/size_m",
                        "invalid_sphere_collision_size",
                        "sphere requires x=y=z and the value is its diameter",
                    )
                if collision_shape == "cylinder" and collision_size and not math.isclose(
                    collision_size[0], collision_size[1], rel_tol=0.0, abs_tol=1e-9
                ):
                    _issue(
                        issues,
                        f"{path}/physics/collision_geometry/size_m",
                        "invalid_cylinder_collision_size",
                        "cylinder requires x=y as its diameter and z as its height",
                    )
                if physics.get("collision_required") is False:
                    _issue(
                        issues,
                        f"{path}/physics/collision_geometry",
                        "collision_geometry_without_collision",
                        "collision_geometry cannot be declared when collision_required is false",
                    )
        initial = _mapping(obj.get("initial_state"), f"{path}/initial_state", issues, required=False)
        for field in ("position_m", "rotation_deg", "linear_velocity_m_s", "angular_velocity_rad_s"):
            if initial.get(field) is not None:
                _finite_vec3(initial.get(field), f"{path}/initial_state/{field}", issues)
        behavior = _mapping(obj.get("behavior"), f"{path}/behavior", issues, required=False)
        if behavior.get("use_ccd") is not None:
            _issue(
                issues,
                f"{path}/behavior/use_ccd",
                "misplaced_physics_field",
                "use_ccd belongs in object.physics.use_ccd, not object.behavior",
            )
        declared_energy = behavior.get("initial_kinetic_energy_j")
        if declared_energy is not None:
            _validate_initial_kinetic_energy(physics, initial, declared_energy, f"{path}/behavior/initial_kinetic_energy_j", issues)
        if obj.get("solver") is not None:
            _mapping(obj.get("solver"), f"{path}/solver", issues)
        representation = _mapping(
            obj.get("visual_representation"),
            f"{path}/visual_representation",
            issues,
        )
        representation_source = str(representation.get("source") or "")
        if representation_source not in {"asset", "solver_generated", "none"}:
            _issue(
                issues,
                f"{path}/visual_representation/source",
                "invalid_visual_representation_source",
                "must be asset, solver_generated, or none",
            )
        if not isinstance(representation.get("visible"), bool):
            _issue(
                issues,
                f"{path}/visual_representation/visible",
                "invalid_type",
                "must be boolean",
            )
        if (
            representation_source == "none"
            and physics.get("collision_required") is True
            and not isinstance(physics.get("collision_geometry"), Mapping)
        ):
            _issue(
                issues,
                f"{path}/physics/collision_geometry",
                "missing_hidden_collision_geometry",
                "source=none with collision_required=true requires explicit collision_geometry",
            )
        if representation_source == "solver_generated" and not isinstance(obj.get("solver"), Mapping):
            _issue(
                issues,
                f"{path}/visual_representation/source",
                "solver_generated_representation_without_solver",
                "solver_generated requires object.solver",
            )
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
        requests = asset_requests(raw_asset)
        if representation_source != "asset" and requests:
            _issue(
                issues,
                f"{path}/asset",
                "visual_representation_conflict",
                f"asset must be omitted when visual_representation.source is {representation_source}",
            )
        if representation_source == "asset" and not requests and not policy.get("allow_analytic_proxy"):
            _issue(
                issues,
                f"{path}/asset",
                "asset_required",
                "an asset request is required when analytic proxies are disabled",
            )
        for request_index, request in enumerate(requests):
            request_path = f"{path}/asset" if request_index == 0 else f"{path}/asset/slot_{request_index}"
            _validate_asset_request(request, request_path, policy, allowed_inputs, issues)
        _validate_procedural_primitive_size(geometry, requests, path, issues)

    duplicates = sorted({value for value in object_ids if object_ids.count(value) > 1})
    if duplicates:
        _issue(issues, "/objects", "duplicate_object_ids", f"duplicate ids: {', '.join(duplicates)}")
    known_objects = set(object_ids)
    _validate_references(data.get("relations"), "/relations", known_objects, issues)
    _validate_relation_surface_gaps(data.get("relations"), issues)
    _validate_references(data.get("events"), "/events", known_objects, issues)
    _validate_event_payloads(data.get("events"), issues)
    _validate_release_impact_directions(objects, data.get("relations"), data.get("events"), issues)
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
            if assertion_type == "event_sequence":
                _validate_event_sequence_assertion(
                    assertion,
                    f"/verification_requirements/assertions/{index}",
                    known_objects,
                    issues,
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
    if data.get("solver_scene") is not None:
        solver_scene = _mapping(data.get("solver_scene"), "/solver_scene", issues)
        if solver_scene.get("type") == "rigid_sph":
            _validate_rigid_sph_declarations(solver_scene, objects, issues)
        elif solver_scene:
            _issue(
                issues,
                "/solver_scene/type",
                "unsupported_solver_scene",
                "the only registered explicit solver scene is rigid_sph",
            )
    if data.get("workspace_bounds_m") is not None:
        workspace_bounds = _mapping(data.get("workspace_bounds_m"), "/workspace_bounds_m", issues)
        minimum = _finite_vec3(workspace_bounds.get("min_m"), "/workspace_bounds_m/min_m", issues)
        maximum = _finite_vec3(workspace_bounds.get("max_m"), "/workspace_bounds_m/max_m", issues)
        if minimum and maximum and any(minimum[index] >= maximum[index] for index in range(3)):
            _issue(issues, "/workspace_bounds_m", "invalid_bounds", "min_m must be less than max_m on every axis")
    variant = _mapping(data.get("variant"), "/variant", issues)
    if variant.get("should_pass") is not None and not isinstance(variant.get("should_pass"), bool):
        _issue(issues, "/variant/should_pass", "invalid_type", "must be boolean")
    _mapping(data.get("provenance"), "/provenance", issues)
    if not isinstance(data.get("notes"), str):
        _issue(issues, "/notes", "invalid_type", "must be a string")
    if case_id and not all(character.isalnum() or character in {"_", "-", "."} for character in case_id):
        _issue(issues, "/identity/case_id", "invalid_case_id", "may contain only letters, numbers, underscore, dash, and dot")
    return issues


def compile_case_spec_v2_runtime(case_spec: CaseSpecV2) -> RuntimeCase:
    data = case_spec.data
    allow_analytic_proxy = bool((data.get("asset_policy") or {}).get("allow_analytic_proxy"))
    capability_id = case_spec.capability_id
    projected_objects = [
        _project_object(
            obj,
            force_analytic_proxy=(
                visual_representation_source(obj) == "asset"
                and allow_analytic_proxy
                and not asset_requests(obj.get("asset"))
            ),
        )
        for obj in case_spec.objects
    ]
    _project_release_events(data.get("events") or [], projected_objects)
    active, passive = _infer_active_passive(projected_objects)
    capability = CapabilityStore().get(capability_id)
    observation = data.get("observation_requirements") or {}
    expected = copy.deepcopy(data.get("expected_behavior") or {})
    expected.setdefault("coordinate_system", (data.get("scene") or {}).get("coordinate_system", "z_up"))
    collision_graph = _collision_graph(data.get("relations") or [])
    if collision_graph and "collision_graph" not in expected:
        expected["collision_graph"] = collision_graph
    collision_surface_gaps = _collision_surface_gaps(data.get("relations") or [])
    if collision_surface_gaps:
        expected["collision_surface_gaps_m"] = collision_surface_gaps
    support_map = _support_map(data.get("relations") or [])
    for object_id, support_id in _initial_contact_support_map(
        data.get("objects") or [],
        data.get("relations") or [],
    ).items():
        support_map.setdefault(object_id, support_id)
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
    runtime_contract = {
        "schema_version": RUNTIME_CASE_SCHEMA_VERSION,
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
        "verification_assertions": copy.deepcopy(assertions),
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
        "source_contract": {
            "source_schema_version": CASE_SPEC_V2_SCHEMA_VERSION,
            "compiler_version": "case_spec_v2_runtime_compiler_v1",
            "source_digest": stable_case_spec_digest(data),
            "source_provenance": copy.deepcopy(data.get("provenance") or {}),
        },
    }
    if isinstance(data.get("solver_scene"), Mapping):
        runtime_contract["solver_scene"] = copy.deepcopy(dict(data["solver_scene"]))
    if isinstance(data.get("workspace_bounds_m"), Mapping):
        runtime_contract["workspace_bounds_m"] = copy.deepcopy(dict(data["workspace_bounds_m"]))
    return RuntimeCase(runtime_contract)


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


def visual_representation_source(obj: Mapping[str, Any]) -> str:
    representation = obj.get("visual_representation")
    if not isinstance(representation, Mapping):
        return "asset"
    return str(representation.get("source") or "asset")


def visual_representation_visible(obj: Mapping[str, Any]) -> bool:
    representation = obj.get("visual_representation")
    if not isinstance(representation, Mapping):
        return True
    return representation.get("visible") is not False


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
        "visual_representation": {
            "source": visual_representation_source(obj),
            "visible": visual_representation_visible(obj),
        },
    }
    for appearance_field in ("color_rgb", "fixed_material_color"):
        if obj.get(appearance_field) is not None:
            projected[appearance_field] = copy.deepcopy(obj[appearance_field])
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
    if isinstance(physics.get("collision_geometry"), Mapping):
        collision_geometry = physics["collision_geometry"]
        projected["collision_geometry"] = {
            "shape": str(collision_geometry.get("shape") or ""),
            "size_m": [float(value) for value in collision_geometry.get("size_m") or []],
            "local_center_offset_m": _vec3_or_default(
                collision_geometry.get("local_center_offset_m"),
                [0.0, 0.0, 0.0],
            ),
        }
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
    if isinstance(obj.get("solver"), Mapping):
        projected["solver"] = copy.deepcopy(dict(obj["solver"]))
    return projected


def _infer_active_passive(objects: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    active: list[str] = []
    passive: list[str] = []
    for obj in objects:
        object_id = str(obj.get("id") or "")
        if obj.get("kinematic") is True or str(obj.get("body_type") or "").casefold() in {"static", "kinematic"}:
            continue
        velocity = obj.get("initial_velocity_m_s") or [0.0, 0.0, 0.0]
        release_velocity = obj.get("release_velocity_m_s") or [0.0, 0.0, 0.0]
        moving = any(abs(float(value)) > 1e-9 for value in velocity)
        released = obj.get("release_time_s") is not None
        release_moving = any(abs(float(value)) > 1e-9 for value in release_velocity)
        if moving or released or release_moving or obj.get("enable_gravity") is True:
            active.append(object_id)
        else:
            passive.append(object_id)
    return active, passive


def _project_release_events(events: Iterable[Any], objects: list[dict[str, Any]]) -> None:
    """Compile delayed-release semantics into the canonical runtime contract."""
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
        relation_type = _canonical_relation_type(relation.get("type"))
        # A plain contact relation describes the initial/static scene contract.
        # Runtime propagation edges must be declared as collision/impacts so a
        # resting support contact cannot masquerade as a completed impact.
        if relation_type not in {"collision", "cascade_collision", "impacts", "hits", "contact_order"}:
            continue
        values = relation.get("objects")
        if not isinstance(values, list):
            values = [relation.get("source"), relation.get("target")]
        pair = [str(value) for value in values if value]
        if len(pair) >= 2:
            result.append(pair[:2])
    return result


def _collision_surface_gaps(relations: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for relation in relations:
        if not isinstance(relation, Mapping) or relation.get("surface_gap_m") is None:
            continue
        values = relation.get("objects")
        if not isinstance(values, list):
            values = [relation.get("source"), relation.get("target")]
        pair = [str(value) for value in values if value]
        if len(pair) < 2:
            continue
        result.append(
            {
                "source": pair[0],
                "target": pair[1],
                "surface_gap_m": float(relation["surface_gap_m"]),
            }
        )
    return result


def _canonical_relation_type(value: Any) -> str:
    relation_type = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return {
        "impact": "impacts",
        "hit": "hits",
    }.get(relation_type, relation_type)


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


def _initial_contact_support_map(objects: Iterable[Any], relations: Iterable[Any]) -> dict[str, str]:
    """Project a nearby stationary dynamic-to-static contact as initial support.

    V2 historically allowed ``contact`` for a body initially resting on an
    inclined surface.  Preserve that meaning without treating future contacts
    (for example a falling body and the floor) as support.
    """
    by_id = {
        str(obj.get("id")): obj
        for obj in objects
        if isinstance(obj, Mapping) and obj.get("id")
    }
    result: dict[str, str] = {}
    for relation in relations:
        if not isinstance(relation, Mapping) or str(relation.get("type") or "").casefold() != "contact":
            continue
        source_id = str(relation.get("source") or "")
        target_id = str(relation.get("target") or "")
        source = by_id.get(source_id)
        target = by_id.get(target_id)
        if source is None or target is None:
            continue
        source_physics = source.get("physics") if isinstance(source.get("physics"), Mapping) else {}
        target_physics = target.get("physics") if isinstance(target.get("physics"), Mapping) else {}
        if str(source_physics.get("body_type") or "dynamic").casefold() != "dynamic":
            continue
        if str(target_physics.get("body_type") or "dynamic").casefold() not in {"static", "kinematic"}:
            continue
        initial = source.get("initial_state") if isinstance(source.get("initial_state"), Mapping) else {}
        velocity = initial.get("linear_velocity_m_s")
        if isinstance(velocity, list) and any(abs(float(value)) > 1e-9 for value in velocity):
            continue
        if _is_near_initial_support(source, target):
            result[source_id] = target_id
    return result


def _is_near_initial_support(subject: Mapping[str, Any], support: Mapping[str, Any]) -> bool:
    subject_geometry = subject.get("geometry") if isinstance(subject.get("geometry"), Mapping) else {}
    support_geometry = support.get("geometry") if isinstance(support.get("geometry"), Mapping) else {}
    subject_initial = subject.get("initial_state") if isinstance(subject.get("initial_state"), Mapping) else {}
    support_initial = support.get("initial_state") if isinstance(support.get("initial_state"), Mapping) else {}
    subject_size = subject_geometry.get("approx_size_m")
    support_size = support_geometry.get("approx_size_m")
    subject_position = subject_initial.get("position_m")
    support_position = support_initial.get("position_m")
    if not all(_is_finite_vec3(value) for value in (subject_size, support_size, subject_position, support_position)):
        return False
    subject_half = [float(value) / 2.0 for value in subject_size]
    support_half = [float(value) / 2.0 for value in support_size]
    delta = [float(subject_position[index]) - float(support_position[index]) for index in range(3)]
    rotation = support_initial.get("rotation_deg")
    pitch = math.radians(float(rotation[0])) if _is_finite_vec3(rotation) else 0.0
    tangent = [math.cos(pitch), 0.0, -math.sin(pitch)]
    normal = [math.sin(pitch), 0.0, math.cos(pitch)]
    tangent_coordinate = sum(delta[index] * tangent[index] for index in range(3))
    if abs(tangent_coordinate) > support_half[0] + 0.05:
        return False
    if abs(delta[1]) > support_half[1] + 0.05:
        return False
    normal_radius = sum(abs(normal[index]) * subject_half[index] for index in range(3))
    gap = sum(delta[index] * normal[index] for index in range(3)) - support_half[2] - normal_radius
    tolerance = max(0.05, min(subject_half) * 0.75)
    return abs(gap) <= tolerance


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
    if "texture_prompt" in acquisition:
        texture_prompt = acquisition.get("texture_prompt")
        if not isinstance(texture_prompt, str):
            _issue(issues, f"{path}/acquisition/texture_prompt", "invalid_type", "must be a string when present")
        elif len(texture_prompt) > 600:
            _issue(
                issues,
                f"{path}/acquisition/texture_prompt",
                "texture_prompt_too_long",
                "must contain at most 600 characters",
            )
        if route != "model_generation":
            _issue(
                issues,
                f"{path}/acquisition/texture_prompt",
                "texture_prompt_route_mismatch",
                "is supported only for model_generation acquisition",
            )
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


def _validate_procedural_primitive_size(
    geometry: Mapping[str, Any],
    requests: list[dict[str, Any]],
    object_path: str,
    issues: list[ValidationIssue],
) -> None:
    if not any(
        isinstance(request.get("acquisition"), Mapping)
        and request["acquisition"].get("route") == "procedural_generation"
        for request in requests
    ):
        return
    size = geometry.get("approx_size_m")
    if not _is_finite_vec3(size) or any(float(value) <= 0.0 for value in size):
        return
    shape = str(geometry.get("shape_hint") or "").strip().casefold()
    if shape in {"sphere", "ball"} and not all(
        math.isclose(float(size[0]), float(size[index]), rel_tol=1e-9, abs_tol=1e-12)
        for index in (1, 2)
    ):
        _issue(
            issues,
            f"{object_path}/geometry/approx_size_m",
            "procedural_sphere_size_mismatch",
            "procedural spheres require equal x/y/z diameters",
        )
    if shape in {"cylinder", "rod", "pole", "column", "disc", "disk"} and not math.isclose(
        float(size[0]),
        float(size[1]),
        rel_tol=1e-9,
        abs_tol=1e-12,
    ):
        _issue(
            issues,
            f"{object_path}/geometry/approx_size_m",
            "procedural_cylinder_local_axis_size_mismatch",
            (
                "procedural cylinders use local Z as their axis: x/y must be equal diameters, z is length; "
                "use initial_state.rotation_deg to orient the axis in world space"
            ),
        )


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


def _validate_event_sequence_assertion(
    assertion: Mapping[str, Any],
    path: str,
    known_objects: set[str],
    issues: list[ValidationIssue],
) -> None:
    pairs = assertion.get("pairs")
    objects = assertion.get("objects")
    if pairs is not None and objects is not None:
        _issue(
            issues,
            path,
            "ambiguous_event_sequence",
            "event_sequence must use either pairs or objects, not both",
        )
        return
    if pairs is not None:
        if not isinstance(pairs, list) or len(pairs) < 2:
            _issue(
                issues,
                f"{path}/pairs",
                "event_sequence_requires_multiple_events",
                "event_sequence.pairs must contain at least two ordered object pairs",
            )
            return
        for index, pair in enumerate(pairs):
            pair_path = f"{path}/pairs/{index}"
            if not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(value, str) or not value for value in pair):
                _issue(issues, pair_path, "invalid_event_pair", "each event_sequence pair must contain exactly two object IDs")
                continue
            for reference in pair:
                if reference not in known_objects:
                    _issue(issues, pair_path, "unknown_object_reference", f"unknown object id: {reference}")
        return
    if not isinstance(objects, list) or len(objects) < 3:
        _issue(
            issues,
            f"{path}/objects",
            "event_sequence_requires_multiple_events",
            "event_sequence.objects must contain at least three ordered object IDs",
        )


def _validate_relation_surface_gaps(relations: Any, issues: list[ValidationIssue]) -> None:
    if not isinstance(relations, list):
        return
    collision_types = {"collision", "cascade_collision", "impacts", "hits", "contact_order"}
    for index, relation in enumerate(relations):
        if not isinstance(relation, Mapping) or relation.get("surface_gap_m") is None:
            continue
        path = f"/relations/{index}/surface_gap_m"
        gap = relation.get("surface_gap_m")
        if isinstance(gap, bool) or not isinstance(gap, (int, float)) or not math.isfinite(float(gap)) or float(gap) < 0.0:
            _issue(issues, path, "invalid_surface_gap", "must be a finite nonnegative number")
        if _canonical_relation_type(relation.get("type")) not in collision_types:
            _issue(
                issues,
                path,
                "surface_gap_requires_collision_relation",
                "is supported only on a collision/impacts propagation relation",
            )


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
        subject_physics = subject.get("physics") if isinstance(subject.get("physics"), Mapping) else {}
        if str(subject_physics.get("body_type") or "dynamic").casefold() in {"static", "kinematic"}:
            # Structural supports commonly contact only one part of a static
            # body, such as a block under the high end of a ramp. Full-footprint
            # containment is required for resting dynamic bodies, not for this
            # local structural contact.
            continue
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


def _validate_release_impact_directions(
    objects: Iterable[Any],
    relations: Any,
    events: Any,
    issues: list[ValidationIssue],
) -> None:
    if not isinstance(relations, list) or not isinstance(events, list):
        return
    positions = {
        str(obj.get("id")): initial.get("position_m")
        for obj in objects
        if isinstance(obj, Mapping)
        and obj.get("id")
        and isinstance((initial := obj.get("initial_state")), Mapping)
        and _is_finite_vec3(initial.get("position_m"))
    }
    impact_targets: dict[str, list[str]] = {}
    for relation in relations:
        if (
            not isinstance(relation, Mapping)
            or _canonical_relation_type(relation.get("type")) not in {"impacts", "hits"}
        ):
            continue
        source = str(relation.get("source") or "")
        target = str(relation.get("target") or "")
        if source and target:
            impact_targets.setdefault(source, []).append(target)
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            continue
        event_type = str(event.get("type") or "").casefold().replace("-", "_").replace(" ", "_")
        if event_type not in {"release", "delayed_release", "staged_release"}:
            continue
        source = str(event.get("object") or "")
        targets = list(dict.fromkeys(impact_targets.get(source, [])))
        velocity = event.get("linear_velocity_m_s")
        if len(targets) != 1 or not _is_finite_vec3(velocity):
            continue
        source_position = positions.get(source)
        target_position = positions.get(targets[0])
        if not _is_finite_vec3(source_position) or not _is_finite_vec3(target_position):
            continue
        direction = [float(target_position[axis]) - float(source_position[axis]) for axis in range(3)]
        speed_squared = sum(float(value) ** 2 for value in velocity)
        distance_squared = sum(value * value for value in direction)
        if speed_squared <= 1e-18 or distance_squared <= 1e-18:
            continue
        if sum(float(velocity[axis]) * direction[axis] for axis in range(3)) <= 0.0:
            _issue(
                issues,
                f"/events/{index}/linear_velocity_m_s",
                "release_velocity_points_away_from_impact_target",
                f"release velocity for {source} must point toward its impacts target {targets[0]}",
            )


def _validate_rigid_sph_declarations(
    scene: Mapping[str, Any],
    objects: Iterable[Any],
    issues: list[ValidationIssue],
) -> None:
    initialization = _mapping(scene.get("initialization"), "/solver_scene/initialization", issues)
    initialization_state = str(initialization.get("state") or "")
    if initialization_state not in {"as_authored", "settled"}:
        _issue(
            issues,
            "/solver_scene/initialization/state",
            "invalid_solver_initialization_state",
            "must be as_authored or settled",
        )
    pre_roll_s = _finite_number(
        initialization.get("pre_roll_s"),
        "/solver_scene/initialization/pre_roll_s",
        issues,
    )
    if pre_roll_s is not None and pre_roll_s < 0.0:
        _issue(
            issues,
            "/solver_scene/initialization/pre_roll_s",
            "invalid_solver_initialization_time",
            "must be non-negative",
        )
    if not isinstance(initialization.get("capture_after_pre_roll"), bool):
        _issue(
            issues,
            "/solver_scene/initialization/capture_after_pre_roll",
            "invalid_type",
            "must be boolean",
        )
    if initialization_state == "settled" and (
        pre_roll_s is None
        or pre_roll_s <= 0.0
        or initialization.get("capture_after_pre_roll") is not True
    ):
        _issue(
            issues,
            "/solver_scene/initialization",
            "invalid_settled_initialization",
            "settled requires positive pre_roll_s and capture_after_pre_roll=true",
        )
    entries = [(index, obj) for index, obj in enumerate(objects) if isinstance(obj, Mapping)]
    fluids = [(index, obj) for index, obj in entries if str(obj.get("role") or "") in {"fluid", "fluid_volume"}]
    rigid_candidates = [(index, obj) for index, obj in entries if (index, obj) not in fluids]

    if len(fluids) != 1:
        _issue(
            issues,
            "/objects",
            "invalid_rigid_sph_fluid_count",
            "rigid_sph requires exactly one object with role fluid or fluid_volume",
        )
    if not rigid_candidates:
        _issue(
            issues,
            "/objects",
            "rigid_sph_rigid_body_missing",
            "rigid_sph requires at least one rigid_body",
        )

    rigid_ids: set[str] = set()
    collision_types: dict[str, str] = {}
    rigid_profiles: dict[str, list[dict[str, float]]] = {}
    for index, body in rigid_candidates:
        path = f"/objects/{index}"
        body_id = str(body.get("id") or "")
        if body.get("role") != "rigid_body":
            _issue(
                issues,
                f"{path}/role",
                "rigid_sph_role_required",
                "every non-fluid rigid_sph participant must use role rigid_body",
            )
        if body_id:
            rigid_ids.add(body_id)
        solver = _mapping(body.get("solver"), f"{path}/solver", issues)
        mobility = str(solver.get("mobility") or "")
        if mobility not in {"static", "kinematic"}:
            _issue(
                issues,
                f"{path}/solver/mobility",
                "unsupported_rigid_sph_mobility",
                "must be static or kinematic",
            )
        transform = _mapping(solver.get("transform"), f"{path}/solver/transform", issues)
        _finite_vec3(transform.get("position_m"), f"{path}/solver/transform/position_m", issues)
        solver_rotation = _finite_vec3(
            transform.get("euler_xyz_deg"),
            f"{path}/solver/transform/euler_xyz_deg",
            issues,
        )
        ue_rotation = _finite_vec3(
            transform.get("ue_rotation_pyr_deg"),
            f"{path}/solver/transform/ue_rotation_pyr_deg",
            issues,
        )
        if solver_rotation and ue_rotation and not _ue_rotation_matches_solver(solver_rotation, ue_rotation):
            _issue(
                issues,
                f"{path}/solver/transform/ue_rotation_pyr_deg",
                "rigid_sph_rotation_mapping_mismatch",
                "must equal [-solver_y, -solver_z, solver_x]",
            )
        if transform.get("scale") is not None:
            _positive_vec3(transform.get("scale"), f"{path}/solver/transform/scale", issues)

        collision = _mapping(solver.get("collision"), f"{path}/solver/collision", issues)
        collision_type = str(collision.get("type") or "")
        if body_id:
            collision_types[body_id] = collision_type
        if collision_type == "plane":
            _finite_vec3(collision.get("position_m"), f"{path}/solver/collision/position_m", issues)
            normal = _finite_vec3(collision.get("normal"), f"{path}/solver/collision/normal", issues)
            if normal and math.sqrt(sum(value * value for value in normal)) <= 1e-12:
                _issue(issues, f"{path}/solver/collision/normal", "invalid_plane_normal", "must be non-zero")
            if collision.get("asset_geometry_match") is not True:
                _issue(
                    issues,
                    f"{path}/solver/collision/asset_geometry_match",
                    "asset_geometry_match_required",
                    "must be true for a declared plane collision",
                )
        elif collision_type == "axisymmetric_profile":
            if collision.get("asset_geometry_match") is not True:
                _issue(
                    issues,
                    f"{path}/solver/collision/asset_geometry_match",
                    "asset_geometry_match_required",
                    "must be true for an axisymmetric_profile collision",
                )
            panel_count = collision.get("panel_count")
            if not isinstance(panel_count, int) or isinstance(panel_count, bool) or panel_count < 12:
                _issue(
                    issues,
                    f"{path}/solver/collision/panel_count",
                    "invalid_panel_count",
                    "must be an integer of at least 12",
                )
            _positive_number(
                collision.get("wall_thickness_m"),
                f"{path}/solver/collision/wall_thickness_m",
                issues,
            )
            if not isinstance(collision.get("fit_method"), str) or not str(collision.get("fit_method")).strip():
                _issue(
                    issues,
                    f"{path}/solver/collision/fit_method",
                    "missing_collision_fit_evidence",
                    "must be a non-empty method identifying how the profile was fitted to the render asset",
                )
            profile = collision.get("inner_profile")
            if not isinstance(profile, list) or len(profile) < 2:
                _issue(
                    issues,
                    f"{path}/solver/collision/inner_profile",
                    "invalid_inner_profile",
                    "must contain at least two {z_m, radius_m} points",
                )
            else:
                previous_z: float | None = None
                compiled_profile: list[dict[str, float]] = []
                profile_is_valid = True
                for point_index, point in enumerate(profile):
                    point_path = f"{path}/solver/collision/inner_profile/{point_index}"
                    point_map = _mapping(point, point_path, issues)
                    z_m = _finite_number(point_map.get("z_m"), f"{point_path}/z_m", issues)
                    radius_m = _positive_number(point_map.get("radius_m"), f"{point_path}/radius_m", issues)
                    if z_m is not None and previous_z is not None and z_m <= previous_z:
                        profile_is_valid = False
                        _issue(
                            issues,
                            f"{point_path}/z_m",
                            "non_increasing_profile",
                            "z_m values must be strictly increasing",
                        )
                    if z_m is not None:
                        previous_z = z_m
                    if z_m is not None and radius_m > 0.0:
                        compiled_profile.append({"z_m": z_m, "radius_m": radius_m})
                    else:
                        profile_is_valid = False
                if body_id and profile_is_valid and len(compiled_profile) == len(profile):
                    rigid_profiles[body_id] = compiled_profile
        else:
            _issue(
                issues,
                f"{path}/solver/collision/type",
                "unsupported_rigid_sph_collision",
                "must be exactly plane or axisymmetric_profile; composite collisions are not registered",
            )

        motion = solver.get("motion")
        if motion is not None:
            motion_map = _mapping(motion, f"{path}/solver/motion", issues)
            if mobility != "kinematic" or motion_map.get("type") != "pivot_rotation":
                _issue(
                    issues,
                    f"{path}/solver/motion/type",
                    "unsupported_rigid_sph_motion",
                    "motion must be pivot_rotation on a kinematic rigid_body",
                )
            start_time = _finite_number(
                motion_map.get("start_time_s"),
                f"{path}/solver/motion/start_time_s",
                issues,
            )
            if start_time is not None and start_time < 0.0:
                _issue(
                    issues,
                    f"{path}/solver/motion/start_time_s",
                    "invalid_motion_time",
                    "must be non-negative",
                )
            _positive_number(motion_map.get("duration_s"), f"{path}/solver/motion/duration_s", issues)
            _finite_vec3(motion_map.get("pivot_local_m"), f"{path}/solver/motion/pivot_local_m", issues)
            solver_end_rotation = _finite_vec3(
                motion_map.get("solver_end_rotation_xyz_deg"),
                f"{path}/solver/motion/solver_end_rotation_xyz_deg",
                issues,
            )
            ue_end_rotation = _finite_vec3(
                motion_map.get("ue_end_rotation_pyr_deg"),
                f"{path}/solver/motion/ue_end_rotation_pyr_deg",
                issues,
            )
            if solver_end_rotation and ue_end_rotation and not _ue_rotation_matches_solver(
                solver_end_rotation,
                ue_end_rotation,
            ):
                _issue(
                    issues,
                    f"{path}/solver/motion/ue_end_rotation_pyr_deg",
                    "rigid_sph_rotation_mapping_mismatch",
                    "must equal [-solver_y, -solver_z, solver_x]",
                )

    for index, fluid in fluids:
        path = f"/objects/{index}/solver"
        solver = _mapping(fluid.get("solver"), path, issues)
        if solver.get("material_model") != "sph_liquid":
            _issue(issues, f"{path}/material_model", "unsupported_fluid_model", "must be sph_liquid")
        initial = _mapping(solver.get("initial_volume"), f"{path}/initial_volume", issues)
        if initial.get("shape") != "cylinder":
            _issue(
                issues,
                f"{path}/initial_volume/shape",
                "unsupported_fluid_volume",
                "rigid_sph currently requires a cylinder",
            )
        frame = _mapping(initial.get("frame"), f"{path}/initial_volume/frame", issues)
        frame_type = str(frame.get("type") or "")
        if frame_type not in {"world", "body_local"}:
            _issue(
                issues,
                f"{path}/initial_volume/frame/type",
                "invalid_rigid_sph_frame",
                "must be world or body_local",
            )
        if frame_type == "body_local":
            body_id = str(frame.get("body_id") or "")
            if body_id not in rigid_ids:
                _issue(
                    issues,
                    f"{path}/initial_volume/frame/body_id",
                    "unknown_rigid_sph_body",
                    "must reference a declared rigid_body",
                )
        position_m = _finite_vec3(initial.get("position_m"), f"{path}/initial_volume/position_m", issues)
        euler_xyz_deg = [0.0, 0.0, 0.0]
        if initial.get("euler_xyz_deg") is not None:
            euler_xyz_deg = _finite_vec3(
                initial.get("euler_xyz_deg"),
                f"{path}/initial_volume/euler_xyz_deg",
                issues,
            )
        radius_m = _positive_number(initial.get("radius_m"), f"{path}/initial_volume/radius_m", issues)
        height_m = _positive_number(initial.get("height_m"), f"{path}/initial_volume/height_m", issues)
        if (
            frame_type == "body_local"
            and body_id in rigid_profiles
            and position_m
            and euler_xyz_deg
            and radius_m > 0.0
            and height_m > 0.0
            and not _body_local_cylinder_has_clearance(
                position_m,
                euler_xyz_deg,
                radius_m,
                height_m,
                rigid_profiles[body_id],
                clearance_m=0.003,
            )
        ):
            _issue(
                issues,
                f"{path}/initial_volume",
                "insufficient_initial_fluid_clearance",
                "body-local cylinder must clear the profile wall, bottom, and rim by at least 0.003 m",
            )

    measurements = scene.get("measurements")
    measurement_ids: set[str] = set()
    if not isinstance(measurements, list) or not measurements:
        _issue(issues, "/solver_scene/measurements", "missing_rigid_sph_measurements", "must be a non-empty list")
        measurements = []
    for index, measurement in enumerate(measurements):
        path = f"/solver_scene/measurements/{index}"
        item = _mapping(measurement, path, issues)
        measurement_id = _nonempty_string(item.get("id"), f"{path}/id", issues)
        if measurement_id in measurement_ids:
            _issue(issues, f"{path}/id", "duplicate_measurement_id", "must be unique")
        if measurement_id:
            measurement_ids.add(measurement_id)
        kind = str(item.get("type") or "")
        if kind == "body_interior_fraction":
            body_id = str(item.get("body_id") or "")
            if body_id not in rigid_ids or collision_types.get(body_id) != "axisymmetric_profile":
                _issue(
                    issues,
                    f"{path}/body_id",
                    "invalid_measurement_body",
                    "must reference an axisymmetric_profile rigid_body",
                )
        elif kind == "outside_body_interiors_fraction":
            body_ids = _string_list(item.get("body_ids"), f"{path}/body_ids", issues)
            if not body_ids or any(body_id not in rigid_ids for body_id in body_ids):
                _issue(issues, f"{path}/body_ids", "invalid_measurement_body", "must reference known rigid_bodies")
        elif kind == "plane_proximity_fraction":
            body_id = str(item.get("body_id") or "")
            if body_id not in rigid_ids or collision_types.get(body_id) != "plane":
                _issue(issues, f"{path}/body_id", "invalid_measurement_body", "must reference a plane rigid_body")
            _positive_number(item.get("distance_m"), f"{path}/distance_m", issues)
        elif kind == "axis_span":
            axes = _string_list(item.get("axes"), f"{path}/axes", issues)
            if not axes or any(axis not in {"x", "y", "z"} for axis in axes):
                _issue(issues, f"{path}/axes", "invalid_measurement_axes", "must use x, y, or z")
        else:
            _issue(
                issues,
                f"{path}/type",
                "unsupported_rigid_sph_measurement",
                "must be body_interior_fraction, outside_body_interiors_fraction, plane_proximity_fraction, or axis_span",
            )

    assertions = scene.get("assertions")
    assertion_ids: set[str] = set()
    if not isinstance(assertions, list) or not assertions:
        _issue(issues, "/solver_scene/assertions", "missing_rigid_sph_assertions", "must be a non-empty list")
        assertions = []
    reductions = {
        "initial",
        "final",
        "max",
        "min",
        "initial_minus_final",
        "max_frame_decrease",
        "threshold_crossing_duration",
    }
    for index, assertion in enumerate(assertions):
        path = f"/solver_scene/assertions/{index}"
        item = _mapping(assertion, path, issues)
        assertion_id = _nonempty_string(item.get("id"), f"{path}/id", issues)
        if assertion_id in assertion_ids:
            _issue(issues, f"{path}/id", "duplicate_assertion_id", "must be unique")
        if assertion_id:
            assertion_ids.add(assertion_id)
        measurement_id = str(item.get("measurement_id") or "")
        if measurement_id not in measurement_ids:
            _issue(
                issues,
                f"{path}/measurement_id",
                "unknown_measurement_id",
                "must reference a declared solver_scene measurement",
            )
        reduction = str(item.get("reduction") or "")
        if reduction not in reductions:
            _issue(
                issues,
                f"{path}/reduction",
                "unsupported_rigid_sph_reduction",
                f"must be one of {sorted(reductions)}",
            )
        if item.get("operator") not in {">=", "<="}:
            _issue(
                issues,
                f"{path}/operator",
                "unsupported_rigid_sph_operator",
                "must be >= or <=",
            )
        _finite_number(item.get("value"), f"{path}/value", issues)
        if reduction == "threshold_crossing_duration":
            if item.get("start_delta") is not None:
                _positive_number(item.get("start_delta"), f"{path}/start_delta", issues)
            if item.get("end_value") is not None:
                _finite_number(item.get("end_value"), f"{path}/end_value", issues)


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


def _finite_number(value: Any, path: str, issues: list[ValidationIssue]) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        _issue(issues, path, "invalid_number", "must be a finite number")
        return None
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


def _ue_rotation_matches_solver(solver_xyz_deg: list[float], ue_pyr_deg: list[float]) -> bool:
    expected = [-solver_xyz_deg[1], -solver_xyz_deg[2], solver_xyz_deg[0]]
    return all(
        abs((actual - target + 180.0) % 360.0 - 180.0) <= 1e-6
        for actual, target in zip(ue_pyr_deg, expected, strict=True)
    )


def _body_local_cylinder_has_clearance(
    center_m: list[float],
    euler_xyz_deg: list[float],
    radius_m: float,
    height_m: float,
    profile: list[dict[str, float]],
    *,
    clearance_m: float,
) -> bool:
    x, y, z = [math.radians(value) for value in euler_xyz_deg]
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    axis = [cz * sy * cx + sz * sx, sz * sy * cx - cz * sx, cy * cx]
    radial_axis = math.hypot(axis[0], axis[1])
    half_height = height_m / 2.0
    radial_extent = math.hypot(center_m[0], center_m[1]) + radius_m + half_height * radial_axis
    vertical_extent = half_height * abs(axis[2]) + radius_m * radial_axis
    minimum_z = center_m[2] - vertical_extent
    maximum_z = center_m[2] + vertical_extent
    if minimum_z < profile[0]["z_m"] + clearance_m or maximum_z > profile[-1]["z_m"] - clearance_m:
        return False
    candidate_z = [minimum_z, maximum_z]
    candidate_z.extend(point["z_m"] for point in profile if minimum_z < point["z_m"] < maximum_z)
    minimum_radius = min(_profile_radius_at(profile, candidate) for candidate in candidate_z)
    return radial_extent <= minimum_radius - clearance_m


def _profile_radius_at(profile: list[dict[str, float]], z_m: float) -> float:
    for lower, upper in zip(profile, profile[1:]):
        if lower["z_m"] <= z_m <= upper["z_m"]:
            fraction = (z_m - lower["z_m"]) / (upper["z_m"] - lower["z_m"])
            return lower["radius_m"] + fraction * (upper["radius_m"] - lower["radius_m"])
    raise ValueError("z_m lies outside axisymmetric profile")


def _positive_vec3(value: Any, path: str, issues: list[ValidationIssue]) -> list[float]:
    values = _finite_vec3(value, path, issues)
    if values and any(item <= 0.0 for item in values):
        _issue(issues, path, "invalid_vector", "all components must be positive")
        return []
    return values


def _components_equal(values: list[float], *, tolerance: float = 1e-9) -> bool:
    return all(math.isclose(values[0], value, rel_tol=0.0, abs_tol=tolerance) for value in values[1:])


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
