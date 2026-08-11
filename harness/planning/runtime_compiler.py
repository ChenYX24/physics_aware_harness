from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from harness.assets.asset_intent_compiler import CompiledAssetIntent, compile_v2_asset_intents
from harness.assets.asset_registry import AssetRegistry
from harness.assets.asset_resolver import resolve_asset_intents
from harness.assets.providers.orchestrator import AssetProviderOrchestrator
from harness.core.artifact_schema import write_json
from harness.core.case_spec_v2 import CaseSpecV2, compile_case_spec_v2_runtime
from harness.core.runtime_case import RuntimeCase
from harness.planning.backend_planner import plan_backend
from harness.planning.static_scene_builder import build_static_scene_layout
from harness.planning.verification_compiler import compile_verification_plan
from harness.runtime.actor_placement import compile_runtime_actor_placement
from harness.runtime.observation_planner import camera_plan_from_observation_plan, compile_observation_plan
from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement
from harness.verification.static_scene_verifier import verify_static_scene_layout


ARTIFACT_FILENAMES = {
    "asset_resolution": "asset_resolution.json",
    "scene_layout": "scene_layout.json",
    "static_scene_report": "static_scene_report.json",
    "verification_plan": "verification_plan.json",
    "observation_plan": "observation_plan.json",
    "camera_plan": "camera_plan.json",
    "runtime_actor_placement": "runtime_actor_placement.json",
    "runtime_actor_placement_report": "runtime_actor_placement_report.json",
    "runtime_plan": "runtime_plan.json",
    "asset_provider_batch": "asset_provider_batch.json",
    "provider_input_manifest": "provider_input_manifest.json",
}
COMPILATION_STAGE_ORDER = [
    "backend_planner",
    "asset_intent_compiler",
    "provider_orchestrator",
    "asset_resolver",
    "scene_layout_compiler",
    "verification_compiler",
    "observation_planner",
    "runtime_binding_and_stage_compiler",
]
@dataclass(frozen=True)
class RuntimeCompilation:
    source_case_spec: dict[str, Any]
    runtime_case: RuntimeCase
    backend_selection: dict[str, Any]
    compiled_asset_intents: tuple[CompiledAssetIntent, ...]
    artifacts: dict[str, dict[str, Any]]
    report: dict[str, Any]
    provider_receipts: tuple[dict[str, Any], ...] = ()

    @property
    def status(self) -> str:
        return str(self.report.get("status") or "fail")

    @property
    def selected_backend(self) -> str:
        return str(self.backend_selection["selected_backend"])

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.report.get("errors") or [] if isinstance(item, dict)]

    def write(self, run_dir: str | Path) -> Path:
        destination = Path(run_dir)
        destination.mkdir(parents=True, exist_ok=True)
        write_json(destination / "case_spec.json", self.runtime_case.data)
        write_json(destination / "runtime_case.json", self.runtime_case.data)
        write_json(destination / "case_spec_v2.json", self.source_case_spec)
        for key, filename in ARTIFACT_FILENAMES.items():
            if key in self.artifacts:
                write_json(destination / filename, self.artifacts[key])
        for receipt in self.provider_receipts:
            write_json(destination / "provider_receipts" / f"{receipt['receipt_id']}.json", receipt)
        write_json(destination / "runtime_compilation_report.json", self.report)
        return destination


def compile_runtime_case(
    case_spec: CaseSpecV2,
    *,
    requested_backend: str | None = None,
    requested_views: list[str] | None = None,
    render_passes: list[str] | None = None,
    camera_strategy: str = "bounds_auto_v1",
    registry: AssetRegistry | None = None,
    provider_orchestrator: AssetProviderOrchestrator | None = None,
    provider_input_manifest: Mapping[str, Any] | None = None,
) -> RuntimeCompilation:
    if not isinstance(case_spec, CaseSpecV2):
        raise TypeError("Runtime Compiler accepts only a validated CaseSpec V2")
    runtime_case = compile_case_spec_v2_runtime(case_spec)
    source_data = copy.deepcopy(case_spec.data)
    registry = registry or AssetRegistry()

    backend_selection = plan_backend(
        runtime_case.data,
        source_case_spec=case_spec,
        requested_backend=requested_backend,
    )
    target_asset_backend = str(backend_selection.get("target_asset_backend") or backend_selection["render_backend"])
    compiled_intents = tuple(
        compile_v2_asset_intents(
            case_spec,
            runtime_case.data,
            target_backend=target_asset_backend,
        )
    )
    provider_orchestration = (provider_orchestrator or AssetProviderOrchestrator()).fulfill(
        case_id=runtime_case.case_id,
        source_case_spec=case_spec.data,
        compiled_intents=compiled_intents,
        target_backend=target_asset_backend,
        registry=registry,
        input_manifest=provider_input_manifest,
    )
    asset_resolution = resolve_asset_intents(
        runtime_case.data,
        registry=registry,
        compiled_intents=list(compiled_intents),
        provider_results=provider_orchestration.results,
        target_backend=target_asset_backend,
        allow_local_preview=(case_spec.data.get("asset_policy") or {}).get("required_license_tier") == "local_preview",
    )
    solver_contract_error = bind_resolved_solver_assets(runtime_case.data, asset_resolution)
    scene_layout = build_static_scene_layout(
        runtime_case.data,
        asset_resolution=asset_resolution,
        requested_views=requested_views,
        camera_strategy=camera_strategy,
        camera_plan={},
    )
    verification_plan = compile_verification_plan(runtime_case.data, source_case_spec=case_spec)
    observation_plan = compile_observation_plan(
        runtime_case.data,
        scene_layout,
        verification_plan,
        source_case_spec=case_spec,
        requested_views=requested_views,
        render_passes=render_passes,
        camera_strategy=camera_strategy,
    )
    camera_plan = camera_plan_from_observation_plan(observation_plan)
    scene_layout["camera_plan"] = copy.deepcopy(camera_plan)
    static_scene_report = verify_static_scene_layout(runtime_case.data, scene_layout)
    runtime_actor_placement = compile_runtime_actor_placement(
        runtime_case.data,
        scene_layout,
        asset_resolution=asset_resolution,
        target_backend=str(backend_selection.get("render_backend") or backend_selection["selected_backend"]),
    )
    actor_report = verify_runtime_actor_placement(runtime_case.data, runtime_actor_placement)
    runtime_plan = _compile_runtime_plan(
        runtime_case.data,
        backend_selection,
        verification_plan,
        observation_plan,
        provider_enabled=True,
        provider_input_manifest_enabled=provider_input_manifest is not None,
    )
    errors = _compilation_errors(
        case_spec,
        asset_resolution,
        static_scene_report,
        actor_report,
        verification_plan,
        backend_selection,
        solver_contract_error,
    )
    artifacts = {
        "asset_resolution": asset_resolution,
        "scene_layout": scene_layout,
        "static_scene_report": static_scene_report,
        "verification_plan": verification_plan,
        "observation_plan": observation_plan,
        "camera_plan": camera_plan,
        "runtime_actor_placement": runtime_actor_placement,
        "runtime_actor_placement_report": actor_report,
        "runtime_plan": runtime_plan,
    }
    artifacts["asset_provider_batch"] = provider_orchestration.batch
    if provider_input_manifest is not None:
        artifacts["provider_input_manifest"] = copy.deepcopy(dict(provider_input_manifest))
    report = {
        "schema_version": "harness_runtime_compilation_report_v1",
        "case_id": runtime_case.case_id,
        "source_schema_version": source_data.get("schema_version"),
        "runtime_contract_schema_version": runtime_case.data.get("schema_version"),
        "status": "fail" if errors else "pass",
        "stage_order": list(COMPILATION_STAGE_ORDER),
        "completed_stages": list(COMPILATION_STAGE_ORDER),
        "asset_resolve_invocation_count": 1,
        "backend_selection": copy.deepcopy(backend_selection),
        "artifact_schemas": {
            ARTIFACT_FILENAMES[key]: value.get("schema_version")
            for key, value in artifacts.items()
            if key in ARTIFACT_FILENAMES
        },
        "errors": errors,
    }
    return RuntimeCompilation(
        source_case_spec=source_data,
        runtime_case=runtime_case,
        backend_selection=backend_selection,
        compiled_asset_intents=compiled_intents,
        artifacts=artifacts,
        report=report,
        provider_receipts=provider_orchestration.receipts,
    )


def _compile_runtime_plan(
    case_spec: Mapping[str, Any],
    backend_selection: Mapping[str, Any],
    verification_plan: Mapping[str, Any],
    observation_plan: Mapping[str, Any],
    *,
    provider_enabled: bool = False,
    provider_input_manifest_enabled: bool = False,
) -> dict[str, Any]:
    plan = {
        "schema_version": "harness_runtime_plan_v1",
        "case_id": case_spec.get("case_id"),
        "backend_selection": {
            "selected_backend": backend_selection.get("selected_backend"),
            "solver_backend": backend_selection.get("solver_backend"),
            "render_backend": backend_selection.get("render_backend"),
            "required_capabilities": list(backend_selection.get("required_capabilities") or []),
            "provided_solver_capabilities": list(backend_selection.get("provided_solver_capabilities") or []),
            "required_case_capabilities": list(backend_selection.get("required_case_capabilities") or []),
            "selection_policy": backend_selection.get("selection_policy"),
            "reason": backend_selection.get("selection_reason"),
            "multi_backend": bool(backend_selection.get("multi_backend")),
            "execution_supported": bool(backend_selection.get("execution_supported")),
        },
        "stages": copy.deepcopy(backend_selection.get("stages") or []),
        "artifacts": {
            "asset_resolution": "asset_resolution.json",
            "scene_layout": "scene_layout.json",
            "actor_placement": "runtime_actor_placement.json",
            "observation_plan": "observation_plan.json",
            "verification_plan": "verification_plan.json",
        },
        "evidence_contract": {
            "signals": list(observation_plan.get("signals") or []),
            "modalities": list(observation_plan.get("modalities") or []),
            "assertion_count": len(verification_plan.get("assertions") or []),
        },
    }
    if provider_enabled:
        plan["artifacts"]["asset_provider_batch"] = "asset_provider_batch.json"
        plan["artifacts"]["provider_receipts"] = "provider_receipts/"
    if provider_input_manifest_enabled:
        plan["artifacts"]["provider_input_manifest"] = "provider_input_manifest.json"
    return plan


def bind_resolved_solver_assets(
    case_spec: dict[str, Any],
    asset_resolution: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Bind Catalog-selected assets before validating explicit solver geometry."""
    solver_scene = case_spec.get("solver_scene") if isinstance(case_spec.get("solver_scene"), dict) else None
    if solver_scene is None:
        return None
    selected_by_object = {}
    for row in asset_resolution.get("assets") or []:
        if not isinstance(row, Mapping):
            continue
        intent = row.get("intent") if isinstance(row.get("intent"), Mapping) else {}
        selected = row.get("selected_asset") if isinstance(row.get("selected_asset"), Mapping) else None
        object_id = str(intent.get("object_id") or "")
        if object_id and selected is not None:
            selected_by_object[object_id] = selected
    for obj in case_spec.get("objects") or []:
        if not isinstance(obj, dict) or obj.get("role") != "rigid_body":
            continue
        selected = selected_by_object.get(str(obj.get("id") or ""))
        if selected is None:
            continue
        unreal_binding = (
            (selected.get("backend_bindings") or {}).get("unreal")
            if isinstance(selected.get("backend_bindings"), Mapping)
            else {}
        )
        unreal_binding = unreal_binding if isinstance(unreal_binding, Mapping) else {}
        ue_path = str(selected.get("ue_path") or unreal_binding.get("object_path") or "")
        bbox_m = selected.get("bbox_size_m") or selected.get("effective_size_m") or selected.get("authored_size_m")
        obj["asset"] = {
            "ue_path": ue_path,
            "material_path": str(selected.get("material_path") or ""),
            "sha256": str(selected.get("sha256") or ""),
            "proxy": bool(selected.get("proxy", False)),
            "catalog_source": str(selected.get("source_kind") or selected.get("source_uri") or "catalog"),
            "bbox_m": copy.deepcopy(bbox_m),
        }
    register_model_generated_solver_frames(case_spec, selected_by_object)
    align_model_generated_supported_bodies(case_spec, selected_by_object)
    try:
        from harness.runtime.rigid_sph_scene import compile_rigid_sph_scene

        compile_rigid_sph_scene(case_spec)
    except (TypeError, ValueError) as exc:
        return {
            "stage": "solver_contract",
            "code": "F3_invalid_solver_contract",
            "message": str(exc),
        }
    return None


def register_model_generated_solver_frames(
    case_spec: dict[str, Any],
    selected_by_object: Mapping[str, Mapping[str, Any]],
) -> None:
    """Register estimated solver-local geometry to resolved visual bounds."""
    objects = [obj for obj in case_spec.get("objects") or [] if isinstance(obj, dict)]
    by_id = {str(obj.get("id") or ""): obj for obj in objects}
    for object_id, obj in by_id.items():
        if obj.get("role") != "rigid_body":
            continue
        selected = selected_by_object.get(object_id)
        if not isinstance(selected, Mapping) or str(selected.get("source_kind") or "") != "model_generation":
            continue
        solver = obj.get("solver") if isinstance(obj.get("solver"), dict) else {}
        collision = solver.get("collision") if isinstance(solver.get("collision"), dict) else {}
        if collision.get("type") != "axisymmetric_profile" or isinstance(collision.get("geometry_registration"), dict):
            continue
        profile = collision.get("inner_profile")
        bbox_m = selected.get("bbox_size_m") or selected.get("effective_size_m") or selected.get("authored_size_m")
        if not isinstance(profile, list) or len(profile) < 2 or not _positive_vec3_values(bbox_m):
            continue
        points = [point for point in profile if isinstance(point, dict)]
        if len(points) != len(profile):
            continue
        z_values = [float(point.get("z_m")) for point in points]
        radii = [float(point.get("radius_m")) for point in points]
        thickness = float(collision.get("wall_thickness_m") or 0.0)
        if not all(math.isfinite(value) for value in [*z_values, *radii, thickness]) or min(radii) <= 0.0 or thickness <= 0.0:
            continue
        center_z = (min(z_values) + max(z_values)) / 2.0
        visual_minor_radius = min(float(bbox_m[0]), float(bbox_m[1])) / 2.0
        radial_scale = min(1.0, max(0.0, visual_minor_radius - thickness) / max(radii))
        if radial_scale <= 0.0:
            continue
        for point in points:
            point["z_m"] = float(point["z_m"]) - center_z
            point["radius_m"] = float(point["radius_m"]) * radial_scale
        motion = solver.get("motion") if isinstance(solver.get("motion"), dict) else None
        if motion is not None and _finite_vec3_values(motion.get("pivot_local_m")):
            pivot = [float(value) for value in motion["pivot_local_m"]]
            motion["pivot_local_m"] = [pivot[0] * radial_scale, pivot[1] * radial_scale, pivot[2] - center_z]
        for candidate in objects:
            candidate_solver = candidate.get("solver") if isinstance(candidate.get("solver"), dict) else {}
            initial = candidate_solver.get("initial_volume") if isinstance(candidate_solver.get("initial_volume"), dict) else {}
            frame = initial.get("frame") if isinstance(initial.get("frame"), dict) else {}
            if frame.get("type") != "body_local" or str(frame.get("body_id") or "") != object_id:
                continue
            if _finite_vec3_values(initial.get("position_m")):
                position = [float(value) for value in initial["position_m"]]
                initial["position_m"] = [position[0] * radial_scale, position[1] * radial_scale, position[2] - center_z]
            if isinstance(initial.get("radius_m"), (int, float)) and not isinstance(initial.get("radius_m"), bool):
                initial["radius_m"] = float(initial["radius_m"]) * radial_scale
        registration = {
            "status": "verified",
            "method": "resolved_visual_bounds_axisymmetric_registration_v1",
            "asset_sha256": str(selected.get("sha256") or ""),
            "visual_bounds_size_m": [float(value) for value in bbox_m],
            "solver_local_translation_m": [0.0, 0.0, -center_z],
            "solver_local_radial_scale": radial_scale,
        }
        collision["geometry_registration"] = registration
        collision["fit_method"] = registration["method"]
        asset = obj.get("asset") if isinstance(obj.get("asset"), dict) else {}
        asset["geometry_registration"] = copy.deepcopy(registration)


def align_model_generated_supported_bodies(
    case_spec: dict[str, Any],
    selected_by_object: Mapping[str, Mapping[str, Any]],
) -> None:
    expected = case_spec.get("expected_physics") if isinstance(case_spec.get("expected_physics"), dict) else {}
    support_map = expected.get("support") if isinstance(expected.get("support"), dict) else {}
    objects = [obj for obj in case_spec.get("objects") or [] if isinstance(obj, dict)]
    by_id = {str(obj.get("id") or ""): obj for obj in objects}
    for object_id, support_id in support_map.items():
        obj = by_id.get(str(object_id))
        support = by_id.get(str(support_id))
        selected = selected_by_object.get(str(object_id))
        if obj is None or support is None or not isinstance(selected, Mapping):
            continue
        if str(selected.get("source_kind") or "") != "model_generation":
            continue
        bbox_m = selected.get("bbox_size_m") or selected.get("effective_size_m") or selected.get("authored_size_m")
        if not _positive_vec3_values(bbox_m):
            continue
        support_solver = support.get("solver") if isinstance(support.get("solver"), dict) else {}
        support_collision = support_solver.get("collision") if isinstance(support_solver.get("collision"), dict) else {}
        if support_collision.get("type") != "plane" or not _finite_vec3_values(support_collision.get("normal")):
            continue
        normal = [float(value) for value in support_collision["normal"]]
        if abs(normal[0]) > 1e-6 or abs(normal[1]) > 1e-6 or normal[2] <= 0.0:
            continue
        support_top_z = float((support_collision.get("position_m") or [0.0, 0.0, 0.0])[2])
        solver = obj.get("solver") if isinstance(obj.get("solver"), dict) else {}
        transform = solver.get("transform") if isinstance(solver.get("transform"), dict) else {}
        if not _finite_vec3_values(transform.get("position_m")):
            continue
        position = [float(value) for value in transform["position_m"]]
        original_z = position[2]
        position[2] = support_top_z + float(bbox_m[2]) / 2.0
        transform["position_m"] = position
        if _finite_vec3_values(obj.get("initial_position_m")):
            initial_position = [float(value) for value in obj["initial_position_m"]]
            initial_position[2] = position[2]
            obj["initial_position_m"] = initial_position
        registration = {
            "status": "verified",
            "method": "resolved_visual_bounds_supported_by_plane_v1",
            "support_id": str(support_id),
            "original_center_z_m": original_z,
            "registered_center_z_m": position[2],
        }
        asset = obj.get("asset") if isinstance(obj.get("asset"), dict) else {}
        asset["support_registration"] = registration


def _finite_vec3_values(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item))
        for item in value
    )


def _positive_vec3_values(value: Any) -> bool:
    return _finite_vec3_values(value) and all(float(item) > 0.0 for item in value)


def _compilation_errors(
    source_v2: CaseSpecV2,
    asset_resolution: Mapping[str, Any],
    static_scene_report: Mapping[str, Any],
    actor_report: Mapping[str, Any],
    verification_plan: Mapping[str, Any],
    backend_selection: Mapping[str, Any],
    solver_contract_error: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if solver_contract_error is not None:
        errors.append(dict(solver_contract_error))
    policy = source_v2.data.get("asset_policy") if isinstance(source_v2.data.get("asset_policy"), dict) else {}
    for row in asset_resolution.get("assets") or []:
        if not isinstance(row, Mapping):
            continue
        acquisition = row.get("acquisition") if isinstance(row.get("acquisition"), Mapping) else {}
        requested = acquisition.get("requested") if isinstance(acquisition.get("requested"), Mapping) else {}
        requirement = str(requested.get("requirement") or "preferred")
        route = str(requested.get("route") or "default")
        required_resolved = acquisition.get("status") in {
            "resolved_provider" if route in {"external_site", "procedural_generation", "model_generation"} else "resolved_local_catalog"
        }
        if requirement == "required" and not required_resolved:
            provider_result = acquisition.get("provider_result") if isinstance(acquisition.get("provider_result"), Mapping) else {}
            provider_failure = provider_result.get("failure") if isinstance(provider_result.get("failure"), Mapping) else {}
            if provider_failure.get("code"):
                failure_code = str(provider_failure["code"])
            elif provider_result.get("status") == "fulfilled":
                failure_code = "provider_asset_unresolved"
            elif route in {"external_site", "procedural_generation", "model_generation"}:
                failure_code = "provider_required"
            else:
                failure_code = "required_asset_route_unresolved"
            errors.append(
                {
                    "stage": "asset_resolution",
                    "code": failure_code,
                    "object_id": (row.get("intent") or {}).get("object_id"),
                    "message": str(row.get("fallback_reason") or "required acquisition route did not resolve"),
                }
            )
        if not row.get("selected_asset") and not policy.get("allow_analytic_proxy", True):
            errors.append(
                {
                    "stage": "asset_resolution",
                    "code": "analytic_proxy_disallowed",
                    "object_id": (row.get("intent") or {}).get("object_id"),
                    "message": "no selected asset and CaseSpec V2 disallows analytic proxy fallback",
                }
            )
    scene_map = asset_resolution.get("scene_map") if isinstance(asset_resolution.get("scene_map"), Mapping) else None
    if scene_map is not None and not scene_map.get("selected_asset"):
        errors.append(
            {
                "stage": "asset_resolution",
                "code": "F3_UE_MAP_UNRESOLVED",
                "message": "requested map did not pass Catalog qualification",
            }
        )
    if static_scene_report.get("status") != "pass":
        errors.append(
            {
                "stage": "scene_layout",
                "code": static_scene_report.get("failure_type") or "static_scene_invalid",
                "message": "static scene verification failed",
            }
        )
    if verification_plan.get("status") != "ready":
        errors.append(
            {
                "stage": "verification",
                "code": verification_plan.get("failure_code") or "verification_plan_invalid",
                "message": "no registered verifier can satisfy the CaseSpec capability",
            }
        )
    if actor_report.get("status") != "pass":
        errors.append(
            {
                "stage": "runtime_binding",
                "code": actor_report.get("failure_type") or "runtime_actor_placement_invalid",
                "message": "runtime actor placement verification failed",
            }
        )
    if not backend_selection.get("execution_supported"):
        errors.append(
            {
                "stage": "runtime_plan",
                "code": backend_selection.get("execution_blocker") or "backend_execution_unsupported",
                "message": "runtime plan is valid but its multi-backend stage executor is not implemented",
            }
        )
    return _dedupe_errors(errors)


def _dedupe_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for error in errors:
        identity = (error.get("stage"), error.get("code"), error.get("object_id"))
        if identity not in seen:
            seen.add(identity)
            result.append(error)
    return result
