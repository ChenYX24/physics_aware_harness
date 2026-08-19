from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from harness.assets.asset_intent_compiler import CompiledAssetIntent, compile_v2_asset_intents
from harness.assets.asset_registry import AssetRegistry
from harness.assets.asset_resolver import requested_map_reference, resolve_asset_intents
from harness.assets.providers.orchestrator import AssetProviderOrchestrator
from harness.core.artifact_schema import read_json, write_json
from harness.core.stage_result import (
    artifact_ref,
    failure_stage_result,
    stage_result_from_compilation_report,
    stage_result_from_provider_batch,
    StageResult,
    write_stage_result,
)
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
COMPILATION_TRANSACTION_SCHEMA_VERSION = "harness_runtime_compilation_transaction_v1"


class RuntimeCompilationPaused(RuntimeError):
    """A recoverable compilation boundary was persisted before returning control."""

    def __init__(self, stage_result: Mapping[str, Any]) -> None:
        result = StageResult.from_dict(stage_result).to_dict()
        super().__init__(str(result.get("message") or result.get("failure_code") or "compilation paused"))
        self.stage_result = result
        self.code = str(result.get("failure_code") or "runtime_compilation_paused")
        self.retryable = bool(result.get("retryable"))
        self.checkpoint_ref = result.get("checkpoint_ref")
        self.request_identities = list(result.get("request_identities") or [])
        self._harness_stage = str(result["stage"])
        self._harness_invocation_count = int(result.get("invocation_count") or 0)


@dataclass(frozen=True)
class RuntimeCompilation:
    source_case_spec: dict[str, Any]
    runtime_case: RuntimeCase
    backend_selection: dict[str, Any]
    compiled_asset_intents: tuple[CompiledAssetIntent, ...]
    artifacts: dict[str, dict[str, Any]]
    report: dict[str, Any]
    provider_receipts: tuple[dict[str, Any], ...] = ()
    stage_result: dict[str, Any] | None = None

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
        write_stage_result(
            destination,
            self.stage_result or stage_result_from_compilation_report(self.report),
        )
        provider_batch = self.artifacts.get("asset_provider_batch")
        if isinstance(provider_batch, Mapping):
            context = self.stage_result or {}
            write_stage_result(
                destination,
                stage_result_from_provider_batch(
                    provider_batch,
                    job_id=context.get("job_id"),
                    attempt_id=context.get("attempt_id"),
                ),
            )
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
    stage_result_dir: str | Path | None = None,
    job_id: str | None = None,
    attempt_id: str | None = None,
    transaction_dir: str | Path | None = None,
    compile_config: Mapping[str, Any] | None = None,
) -> RuntimeCompilation:
    started = time.perf_counter()
    try:
        compilation = _compile_runtime_case_impl(
            case_spec,
            requested_backend=requested_backend,
            requested_views=requested_views,
            render_passes=render_passes,
            camera_strategy=camera_strategy,
            registry=registry,
            provider_orchestrator=provider_orchestrator,
            provider_input_manifest=provider_input_manifest,
            transaction_dir=transaction_dir,
            job_id=job_id,
            attempt_id=attempt_id,
            compile_config=compile_config,
        )
    except BaseException as exc:
        persisted_result = getattr(exc, "stage_result", None)
        if isinstance(persisted_result, Mapping):
            stage_result = StageResult.from_dict(persisted_result).to_dict()
            if stage_result_dir is not None:
                write_stage_result(stage_result_dir, stage_result)
            raise
        source_schema_version = case_spec.data.get("schema_version") if isinstance(case_spec, CaseSpecV2) else None
        failed_stage = str(getattr(exc, "_harness_stage", "compile"))
        stage_result = failure_stage_result(
            stage=failed_stage,
            failure_code=str(
                getattr(
                    exc,
                    "code",
                    "provider_execution_exception" if failed_stage == "provider" else "runtime_compilation_exception",
                )
            ),
            message=str(exc) or type(exc).__name__,
            retryable=getattr(exc, "retryable", None),
            source_status="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else None,
            job_id=job_id,
            attempt_id=attempt_id,
            checkpoint_ref=getattr(exc, "checkpoint_ref", None),
            artifact_refs=(
                [artifact_ref("case_spec", "case_spec_v2.json", source_schema_version)]
                if failed_stage == "compile"
                else []
            ),
            elapsed_seconds=time.perf_counter() - started,
            invocation_count=int(getattr(exc, "_harness_invocation_count", 1)),
            request_identities=list(getattr(exc, "request_identities", []) or []),
        )
        if stage_result_dir is not None:
            write_stage_result(stage_result_dir, stage_result)
        raise
    stage_result = stage_result_from_compilation_report(
        compilation.report,
        job_id=job_id,
        attempt_id=attempt_id,
        elapsed_seconds=time.perf_counter() - started,
    )
    if stage_result_dir is not None:
        write_stage_result(stage_result_dir, stage_result)
        provider_batch = compilation.artifacts.get("asset_provider_batch")
        if isinstance(provider_batch, Mapping):
            write_stage_result(
                stage_result_dir,
                stage_result_from_provider_batch(provider_batch, job_id=job_id, attempt_id=attempt_id),
            )
    return replace(compilation, stage_result=stage_result)


def _compile_runtime_case_impl(
    case_spec: CaseSpecV2,
    *,
    requested_backend: str | None = None,
    requested_views: list[str] | None = None,
    render_passes: list[str] | None = None,
    camera_strategy: str = "bounds_auto_v1",
    registry: AssetRegistry | None = None,
    provider_orchestrator: AssetProviderOrchestrator | None = None,
    provider_input_manifest: Mapping[str, Any] | None = None,
    transaction_dir: str | Path | None = None,
    job_id: str | None = None,
    attempt_id: str | None = None,
    compile_config: Mapping[str, Any] | None = None,
) -> RuntimeCompilation:
    if not isinstance(case_spec, CaseSpecV2):
        raise TypeError("Runtime Compiler accepts only a validated CaseSpec V2")
    runtime_case = compile_case_spec_v2_runtime(case_spec)
    source_data = copy.deepcopy(case_spec.data)
    registry = registry or AssetRegistry()
    effective_compile_config = _compile_config_identity(
        compile_config,
        case_spec=case_spec,
        registry=registry,
    )

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
    transaction_root = Path(transaction_dir) if transaction_dir is not None else None
    transaction = _begin_compilation_transaction(
        transaction_root,
        case_spec=case_spec,
        requested_backend=requested_backend,
        requested_views=requested_views,
        render_passes=render_passes,
        camera_strategy=camera_strategy,
        backend_selection=backend_selection,
        runtime_case=runtime_case,
        compiled_intents=compiled_intents,
        compile_config=effective_compile_config,
    )
    provider_orchestration = _provider_for_compilation_transaction(
        transaction_root,
        transaction,
        orchestrator=provider_orchestrator or AssetProviderOrchestrator(),
        case_spec=case_spec,
        runtime_case=runtime_case,
        compiled_intents=compiled_intents,
        target_asset_backend=target_asset_backend,
        registry=registry,
        provider_input_manifest=provider_input_manifest,
        job_id=job_id,
        attempt_id=attempt_id,
    )
    asset_resolution = _resolve_for_compilation_transaction(
        transaction_root,
        transaction,
        runtime_case=runtime_case,
        case_spec=case_spec,
        compiled_intents=compiled_intents,
        provider_orchestration=provider_orchestration,
        target_asset_backend=target_asset_backend,
        registry=registry,
        job_id=job_id,
        attempt_id=attempt_id,
        requested_map_package=effective_compile_config["map_package"],
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
        handoff_contract=backend_selection.get("handoff_contract"),
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
        "asset_resolve_invocation_count": int(transaction.get("asset_resolve_invocation_count") or 1),
        "compile_config_digest": _stable_digest(effective_compile_config),
        "backend_selection": copy.deepcopy(backend_selection),
        "artifact_schemas": {
            ARTIFACT_FILENAMES[key]: value.get("schema_version")
            for key, value in artifacts.items()
            if key in ARTIFACT_FILENAMES
        },
        "errors": errors,
    }
    if transaction_root is not None:
        for key, value in artifacts.items():
            if key in ARTIFACT_FILENAMES:
                write_json(transaction_root / ARTIFACT_FILENAMES[key], value)
        for receipt in provider_orchestration.receipts:
            write_json(transaction_root / "provider_receipts" / f"{receipt['receipt_id']}.json", receipt)
        write_json(transaction_root / "runtime_compilation_report.json", report)
        transaction.update({"state": "completed", "updated_at_epoch": time.time()})
        _write_compilation_transaction(transaction_root, transaction)
    return RuntimeCompilation(
        source_case_spec=source_data,
        runtime_case=runtime_case,
        backend_selection=backend_selection,
        compiled_asset_intents=compiled_intents,
        artifacts=artifacts,
        report=report,
        provider_receipts=provider_orchestration.receipts,
    )


def _begin_compilation_transaction(
    root: Path | None,
    *,
    case_spec: CaseSpecV2,
    requested_backend: str | None,
    requested_views: list[str] | None,
    render_passes: list[str] | None,
    camera_strategy: str,
    backend_selection: Mapping[str, Any],
    runtime_case: RuntimeCase,
    compiled_intents: tuple[CompiledAssetIntent, ...],
    compile_config: Mapping[str, str],
) -> dict[str, Any]:
    identity = {
        "case_spec_digest": _stable_digest(case_spec.data),
        "requested_backend": requested_backend,
        "compile_config_digest": _stable_digest(compile_config),
    }
    transaction_id = f"compilation_{_stable_digest(identity)[:24]}"
    fresh = {
        "schema_version": COMPILATION_TRANSACTION_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "input_identity": identity,
        "compile_config": dict(compile_config),
        "latest_projection": {
            "requested_views": list(requested_views) if requested_views is not None else None,
            "render_passes": list(render_passes) if render_passes is not None else None,
            "camera_strategy": camera_strategy,
        },
        "state": "planned",
        "asset_resolve_invocation_count": 0,
        "catalog_snapshot": None,
        "updated_at_epoch": time.time(),
    }
    if root is None:
        return fresh
    root.mkdir(parents=True, exist_ok=True)
    path = root / "compilation_transaction.json"
    if path.is_file():
        existing = read_json(path)
        if not isinstance(existing, Mapping) or existing.get("schema_version") != COMPILATION_TRANSACTION_SCHEMA_VERSION:
            raise ValueError("compilation transaction checkpoint has an unsupported schema")
        if existing.get("transaction_id") != transaction_id or existing.get("input_identity") != identity:
            raise ValueError("compilation transaction input digest changed; create a new CaseSpec attempt")
        resumed = dict(existing)
        resumed["latest_projection"] = fresh["latest_projection"]
        _write_compilation_transaction(root, resumed)
        return resumed
    write_json(root / "runtime_case.json", runtime_case.data)
    write_json(root / "backend_selection.json", dict(backend_selection))
    write_json(root / "compiled_asset_intents.json", [intent.to_dict() for intent in compiled_intents])
    _write_compilation_transaction(root, fresh)
    return fresh


def _provider_for_compilation_transaction(
    root: Path | None,
    transaction: dict[str, Any],
    *,
    orchestrator: AssetProviderOrchestrator,
    case_spec: CaseSpecV2,
    runtime_case: RuntimeCase,
    compiled_intents: tuple[CompiledAssetIntent, ...],
    target_asset_backend: str,
    registry: AssetRegistry,
    provider_input_manifest: Mapping[str, Any] | None,
    job_id: str | None,
    attempt_id: str | None,
) -> Any:
    cached_batch = read_json(root / "asset_provider_batch.json") if root is not None and (root / "asset_provider_batch.json").is_file() else None
    cached_result = (
        stage_result_from_provider_batch(cached_batch, job_id=job_id, attempt_id=attempt_id)
        if isinstance(cached_batch, Mapping)
        else None
    )
    if cached_result is not None and cached_result.get("status") == "completed":
        return _provider_orchestration_from_artifacts(root, cached_batch)
    try:
        orchestration = orchestrator.fulfill(
            case_id=runtime_case.case_id,
            source_case_spec=case_spec.data,
            compiled_intents=compiled_intents,
            target_backend=target_asset_backend,
            registry=registry,
            input_manifest=provider_input_manifest,
        )
    except BaseException as exc:
        setattr(exc, "_harness_stage", "provider")
        setattr(exc, "_harness_invocation_count", getattr(exc, "_harness_invocation_count", 1))
        raise
    if root is not None:
        write_json(root / "asset_provider_batch.json", orchestration.batch)
        for receipt in orchestration.receipts:
            write_json(root / "provider_receipts" / f"{receipt['receipt_id']}.json", receipt)
    provider_result = stage_result_from_provider_batch(orchestration.batch, job_id=job_id, attempt_id=attempt_id)
    if root is not None:
        write_stage_result(root, provider_result)
        transaction.update({"state": "provider_completed" if provider_result["status"] == "completed" else "provider_paused", "updated_at_epoch": time.time()})
        _write_compilation_transaction(root, transaction)
    if root is not None and provider_result["status"] != "completed":
        checkpoint = str(root / "compilation_transaction.json")
        paused = dict(provider_result)
        paused["checkpoint_ref"] = checkpoint
        paused = StageResult.from_dict(paused).to_dict()
        write_stage_result(root, paused)
        raise RuntimeCompilationPaused(paused)
    return orchestration


def _resolve_for_compilation_transaction(
    root: Path | None,
    transaction: dict[str, Any],
    *,
    runtime_case: RuntimeCase,
    case_spec: CaseSpecV2,
    compiled_intents: tuple[CompiledAssetIntent, ...],
    provider_orchestration: Any,
    target_asset_backend: str,
    registry: AssetRegistry,
    job_id: str | None,
    attempt_id: str | None,
    requested_map_package: str,
) -> dict[str, Any]:
    resolution_path = root / ARTIFACT_FILENAMES["asset_resolution"] if root is not None else None
    if resolution_path is not None and resolution_path.is_file():
        if int(transaction.get("asset_resolve_invocation_count") or 0) != 1:
            raise ValueError("asset resolution artifact exists without exactly one recorded invocation")
        return dict(read_json(resolution_path))
    if root is not None and transaction.get("state") == "asset_resolve_started":
        result = failure_stage_result(
            stage="compile",
            failure_code="asset_resolve_completion_unknown",
            message="Asset Resolve started but no committed result exists; refusing a second invocation",
            job_id=job_id,
            attempt_id=attempt_id,
            checkpoint_ref=str(root / "compilation_transaction.json"),
        )
        raise RuntimeCompilationPaused(result)
    transaction.update(
        {
            "state": "asset_resolve_started",
            "asset_resolve_invocation_count": 1,
            "catalog_snapshot": _catalog_snapshot(registry),
            "updated_at_epoch": time.time(),
        }
    )
    if root is not None:
        _write_compilation_transaction(root, transaction)
    resolution = resolve_asset_intents(
        runtime_case.data,
        registry=registry,
        compiled_intents=list(compiled_intents),
        provider_results=provider_orchestration.results,
        target_backend=target_asset_backend,
        allow_local_preview=(case_spec.data.get("asset_policy") or {}).get("required_license_tier") == "local_preview",
        requested_map_package=requested_map_package,
    )
    if resolution_path is not None:
        write_json(resolution_path, resolution)
        transaction.update({"state": "asset_resolved", "updated_at_epoch": time.time()})
        _write_compilation_transaction(root, transaction)
    return resolution


def _provider_orchestration_from_artifacts(root: Path, batch: Mapping[str, Any]) -> Any:
    from harness.assets.providers.orchestrator import ProviderOrchestration

    results = {
        (str(row.get("object_id") or ""), str(row.get("slot") or "primary")): dict(row)
        for row in batch.get("results") or []
        if isinstance(row, Mapping)
    }
    receipts = []
    for receipt_id in batch.get("receipt_ids") or []:
        path = root / "provider_receipts" / f"{receipt_id}.json"
        if not path.is_file():
            raise ValueError(f"provider receipt checkpoint is missing: {receipt_id}")
        receipts.append(dict(read_json(path)))
    return ProviderOrchestration(batch=dict(batch), results=results, receipts=tuple(receipts))


def _write_compilation_transaction(root: Path, transaction: Mapping[str, Any]) -> None:
    write_json(root / "compilation_transaction.json", dict(transaction))


def _catalog_snapshot(registry: AssetRegistry) -> dict[str, Any]:
    path = registry.path.resolve(strict=False)
    return {
        "path": str(path),
        "sha256": _sha256_file(path) if path.is_file() else None,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compile_config_identity(
    value: Mapping[str, Any] | None,
    *,
    case_spec: CaseSpecV2,
    registry: AssetRegistry,
) -> dict[str, str]:
    data = (
        {
            "schema_version": "harness_ue_compile_config_v1",
            "map_package": requested_map_reference(case_spec.data),
            "ue_project": os.environ.get("SIM_STUDIO_UE_PROJECT", "").strip(),
            "catalog": str(registry.path.resolve(strict=False)),
        }
        if value is None
        else dict(value)
    )
    expected = {"schema_version", "map_package", "ue_project", "catalog"}
    if set(data) != expected or data.get("schema_version") != "harness_ue_compile_config_v1":
        raise ValueError("compile_config must be a strict harness_ue_compile_config_v1 object")
    for field in ("map_package", "ue_project", "catalog"):
        if not isinstance(data.get(field), str):
            raise ValueError(f"compile_config.{field} must be a string")
    catalog = str(Path(data["catalog"]).expanduser().resolve(strict=False))
    actual_catalog = str(registry.path.expanduser().resolve(strict=False))
    if catalog != actual_catalog:
        raise ValueError("compile_config.catalog must match the AssetRegistry used by Runtime Compiler")
    return {
        "schema_version": "harness_ue_compile_config_v1",
        "map_package": data["map_package"].strip(),
        "ue_project": data["ue_project"].strip(),
        "catalog": catalog,
    }


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
            "collision": copy.deepcopy(selected.get("collision") or {}),
        }
    register_model_generated_solver_frames(case_spec, selected_by_object)
    try:
        from harness.runtime.rigid_sph_scene import RigidSphCapabilityMissing, compile_rigid_sph_scene

        compile_rigid_sph_scene(case_spec)
    except RigidSphCapabilityMissing as exc:
        return {
            "stage": "solver_contract",
            "code": "capability_missing",
            "message": str(exc),
        }
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
                "message": "solver and renderer do not share a compatible versioned handoff contract",
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
