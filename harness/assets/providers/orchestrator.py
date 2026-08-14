from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from harness.assets.asset_intent_compiler import CompiledAssetIntent
from harness.assets.asset_registry import AssetRegistry, candidate_matches_search_intent
from harness.assets.asset_resolver import asset_quality_gate
from harness.assets.providers.backend_importer import (
    IMPORTER_CONTRACT_VERSION,
    BackendImporterAdapter,
    UECommandImporterAdapter,
    validate_import_result,
)
from harness.assets.providers.input_manifest import ProviderInputError, bind_provider_reference_inputs
from harness.assets.providers.contracts import (
    BACKEND_IMPORT_RESULT_SCHEMA,
    BACKEND_IMPORT_REQUEST_SCHEMA,
    PROVIDER_BATCH_SCHEMA,
    PROVIDER_RECEIPT_SCHEMA,
    PROVIDER_REQUEST_SCHEMA,
    PROVIDER_RESULT_SCHEMA,
    SUCCESSFUL_LIFECYCLE,
    BackendImportRequest,
    BackendImportResult,
    ProviderBatch,
    ProviderReceipt,
    ProviderRequest,
    ProviderResult,
    provider_failure,
    stable_digest,
)
from harness.assets.providers.local_procedural_mesh import (
    GENERATOR_SOURCE_VERSION,
    PROVIDER_ID,
    PROVIDER_VERSION,
    ProceduralGenerationError,
    generate_procedural_obj,
    normalize_generation_spec,
    recipe_for_provider_hint,
    recipe_digest,
    stable_asset_id,
)
from harness.assets.providers.remote import (
    MeshyModelGenerationAdapter,
    PolyHavenExternalSiteAdapter,
    RemoteAcquisition,
    RemoteProviderAdapter,
    RemoteProviderError,
)
from harness.core.artifact_schema import read_json, write_json


PROVIDER_ROUTES = {"external_site", "procedural_generation", "model_generation"}
DEFAULT_WORKSPACE = Path.home() / "SimulatorWorkspace" / "physics_aware_harness"
SOURCE_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ProviderOrchestration:
    batch: dict[str, Any]
    results: dict[tuple[str, str], dict[str, Any]]
    receipts: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PreparedProviderImport:
    kind: str
    request: dict[str, Any]
    intent: CompiledAssetIntent
    request_dir: Path
    import_request: BackendImportRequest
    normalized: dict[str, Any] | None = None
    generated: dict[str, Any] | None = None
    acquisition: RemoteAcquisition | None = None


class AssetProviderOrchestrator:
    def __init__(
        self,
        *,
        workspace: str | Path | None = None,
        importer: BackendImporterAdapter | None = None,
        redistribution_evidence: Mapping[str, Any] | None = None,
        remote_providers: Mapping[str, RemoteProviderAdapter] | None = None,
        max_paid_submissions: int | None = None,
        paid_submission_ledger_path: str | Path | None = None,
        ue_launch_ledger_path: str | Path | None = None,
        usage_job_id: str | None = None,
        usage_attempt_id: str | None = None,
        max_ue_launches: int | None = None,
    ) -> None:
        self.workspace = Path(workspace or os.environ.get("SIM_HARNESS_WORKSPACE", DEFAULT_WORKSPACE)).resolve()
        self.importer = importer or UECommandImporterAdapter()
        self.redistribution_evidence = dict(redistribution_evidence or {})
        if max_paid_submissions is not None and (
            not isinstance(max_paid_submissions, int)
            or isinstance(max_paid_submissions, bool)
            or max_paid_submissions < 0
        ):
            raise ValueError("max_paid_submissions must be a non-negative integer or null")
        self.max_paid_submissions = max_paid_submissions
        self.paid_submission_ledger_path = (
            Path(paid_submission_ledger_path) if paid_submission_ledger_path is not None else None
        )
        self.ue_launch_ledger_path = Path(ue_launch_ledger_path) if ue_launch_ledger_path is not None else None
        self.usage_job_id = str(usage_job_id or "")
        self.usage_attempt_id = str(usage_attempt_id or "")
        if self.ue_launch_ledger_path is not None and (not self.usage_job_id or not self.usage_attempt_id):
            raise ValueError("UE launch accounting requires job and attempt identities")
        if max_ue_launches is not None and (
            not isinstance(max_ue_launches, int) or isinstance(max_ue_launches, bool) or max_ue_launches < 0
        ):
            raise ValueError("max_ue_launches must be a non-negative integer or null")
        self.max_ue_launches = max_ue_launches
        self._paid_submission_reservations: dict[str, dict[str, Any]] = {}
        self.remote_providers = dict(
            remote_providers
            if remote_providers is not None
            else {
                "model_generation": MeshyModelGenerationAdapter(),
                "external_site": PolyHavenExternalSiteAdapter(),
            }
        )

    def fulfill(
        self,
        *,
        case_id: str,
        source_case_spec: Mapping[str, Any],
        compiled_intents: tuple[CompiledAssetIntent, ...] | list[CompiledAssetIntent],
        target_backend: str,
        registry: AssetRegistry,
        input_manifest: Mapping[str, Any] | None = None,
    ) -> ProviderOrchestration:
        try:
            return self._fulfill(
                case_id=case_id,
                source_case_spec=source_case_spec,
                compiled_intents=compiled_intents,
                target_backend=target_backend,
                registry=registry,
                input_manifest=input_manifest,
            )
        except BaseException as exc:
            setattr(exc, "_harness_stage", "provider")
            setattr(exc, "_harness_invocation_count", getattr(exc, "_harness_invocation_count", 1))
            raise

    def _fulfill(
        self,
        *,
        case_id: str,
        source_case_spec: Mapping[str, Any],
        compiled_intents: tuple[CompiledAssetIntent, ...] | list[CompiledAssetIntent],
        target_backend: str,
        registry: AssetRegistry,
        input_manifest: Mapping[str, Any] | None = None,
    ) -> ProviderOrchestration:
        started = time.perf_counter()
        requests: list[dict[str, Any]] = []
        results: dict[tuple[str, str], dict[str, Any]] = {}
        receipts: list[dict[str, Any]] = []
        objects = {
            str(item.get("id")): item
            for item in source_case_spec.get("objects") or []
            if isinstance(item, Mapping) and item.get("id")
        }
        policy = source_case_spec.get("asset_policy") if isinstance(source_case_spec.get("asset_policy"), Mapping) else {}
        prepared_imports: list[PreparedProviderImport] = []
        for intent in compiled_intents:
            route = str(intent.acquisition.get("route") or "default")
            if route not in PROVIDER_ROUTES:
                continue
            request = self._build_request(
                case_id=case_id,
                intent=intent,
                source_object=objects.get(intent.object_id, {}),
                target_backend=target_backend,
                required_license_tier=str(policy.get("required_license_tier") or "local_preview"),
            )
            adapter = self.remote_providers.get(route)
            hint = str(request.get("provider_hint") or "").strip().casefold()
            meshy_hint_accepted = (
                adapter is not None
                and adapter.provider_id == "meshy_model_generation_v1"
                and hint in {"", "meshy", "meshy_v1", adapter.provider_id}
            )
            if route == "model_generation" and registry.writable and meshy_hint_accepted:
                try:
                    references = bind_provider_reference_inputs(
                        request.get("reference_inputs") or [],
                        input_manifest,
                        provider="meshy",
                    )
                    request = self._request_with_reference_inputs(request, references)
                except ProviderInputError as exc:
                    requests.append(request)
                    results[(intent.object_id, intent.slot)] = provider_failure(
                        request,
                        status="blocked",
                        code=exc.code,
                        message=exc.message,
                    )
                    continue
            requests.append(request)
            prepared, immediate = self._prepare_one(request, intent=intent, registry=registry)
            if prepared is not None:
                prepared_imports.append(prepared)
            else:
                assert immediate is not None
                result, receipt = immediate
                results[(intent.object_id, intent.slot)] = result
                if receipt is not None:
                    receipts.append(receipt)
        import_results, import_summary = self._run_prepared_imports(prepared_imports, registry=registry)
        for prepared in prepared_imports:
            import_result = import_results[prepared.import_request.data["request_digest"]]
            result, receipt = self._complete_prepared(prepared, import_result=import_result, registry=registry)
            results[(prepared.intent.object_id, prepared.intent.slot)] = result
            if receipt is not None:
                receipts.append(receipt)
        batch = ProviderBatch.from_dict(
            {
                "schema_version": PROVIDER_BATCH_SCHEMA,
                "case_id": case_id,
                "requests": requests,
                "results": [results[key] for key in sorted(results)],
                "receipt_ids": [str(receipt["receipt_id"]) for receipt in receipts],
                "import_summary": import_summary,
                "elapsed_seconds": round(time.perf_counter() - started, 6),
            }
        ).to_dict()
        return ProviderOrchestration(batch=batch, results=results, receipts=tuple(receipts))

    @staticmethod
    def _request_with_reference_inputs(
        request: Mapping[str, Any],
        references: list[dict[str, Any]],
    ) -> dict[str, Any]:
        identity = {
            key: copy.deepcopy(value)
            for key, value in request.items()
            if key not in {"schema_version", "request_id", "request_digest"}
        }
        identity["reference_inputs"] = references
        digest = stable_digest(identity)
        return ProviderRequest.from_dict(
            {
                "schema_version": PROVIDER_REQUEST_SCHEMA,
                "request_id": f"asset-provider.{digest[:24]}",
                "request_digest": digest,
                **identity,
            }
        ).to_dict()

    def _build_request(
        self,
        *,
        case_id: str,
        intent: CompiledAssetIntent,
        source_object: Mapping[str, Any],
        target_backend: str,
        required_license_tier: str,
    ) -> dict[str, Any]:
        geometry = source_object.get("geometry") if isinstance(source_object.get("geometry"), Mapping) else {}
        shape_hint = str(geometry.get("shape_hint") or "").strip().casefold()
        generation_spec = {
            "recipe_id": str(
                recipe_for_provider_hint(shape_hint, intent.acquisition.get("provider_hint")) or ""
            ),
            "recipe_version": "v1",
            "shape": shape_hint,
            "size_m": copy.deepcopy(geometry.get("approx_size_m")),
            "scale_policy": str(geometry.get("scale_policy") or "preserve_authored"),
        }
        identity = {
            "case_id": case_id,
            "object_id": intent.object_id,
            "slot": intent.slot,
            "route": intent.acquisition["route"],
            "requirement": intent.acquisition["requirement"],
            "origin": intent.acquisition["origin"],
            "provider_hint": intent.acquisition.get("provider_hint"),
            "source_uri_hint": intent.acquisition.get("source_uri_hint"),
            "reference_inputs": copy.deepcopy(intent.acquisition.get("reference_inputs") or []),
            "search_intent": intent.search_intent.to_dict(),
            "target_backend": target_backend,
            "required_license_tier": required_license_tier,
            "generation_spec": generation_spec,
        }
        if "texture_prompt" in intent.acquisition:
            identity["texture_prompt"] = intent.acquisition.get("texture_prompt")
        digest = stable_digest(identity)
        return ProviderRequest.from_dict(
            {
                "schema_version": PROVIDER_REQUEST_SCHEMA,
                "request_id": f"asset-provider.{digest[:24]}",
                "request_digest": digest,
                **identity,
            }
        ).to_dict()

    def _prepare_one(
        self,
        request: dict[str, Any],
        *,
        intent: CompiledAssetIntent,
        registry: AssetRegistry,
    ) -> tuple[
        PreparedProviderImport | None,
        tuple[dict[str, Any], dict[str, Any] | None] | None,
    ]:
        if not registry.writable:
            return None, (
                provider_failure(
                    request,
                    status="blocked",
                    code="catalog_not_writable",
                    message="Provider fulfillment requires a writable SQLite Catalog",
                ),
                None,
            )
        route = str(request["route"])
        if route in {"external_site", "model_generation"}:
            adapter = self.remote_providers.get(route)
            if adapter is None:
                return None, (
                    provider_failure(
                        request,
                        status="blocked",
                        code="unsupported_provider_route",
                        message=f"Provider route is not configured: {route}",
                    ),
                    None,
                )
            return self._prepare_remote(request, intent=intent, adapter=adapter)
        invalid_inputs = [
            str(row.get("input_id") or "")
            for row in request.get("reference_inputs") or []
            if not _is_sha256(str(row.get("sha256") or ""))
        ]
        if invalid_inputs:
            return None, (
                provider_failure(
                    request,
                    status="blocked",
                    code="input_hash_missing",
                    message=f"Provider reference inputs require verified SHA-256 identities: {invalid_inputs}",
                ),
                None,
            )
        try:
            normalized = normalize_generation_spec(request["generation_spec"])
        except ProceduralGenerationError as exc:
            return None, (provider_failure(request, status="failed", code=exc.code, message=exc.message), None)
        workspace_failure = self._workspace_failure(request)
        if workspace_failure is not None:
            return None, workspace_failure
        request_dir = self.workspace / "providers" / PROVIDER_ID / request["request_digest"]
        generated = generate_procedural_obj(normalized, request_dir / f"{normalized['recipe_id']}.obj")
        asset_id = stable_asset_id(normalized)
        payload = {
            "schema_version": BACKEND_IMPORT_REQUEST_SCHEMA,
            "asset_id": asset_id,
            "target_backend": request["target_backend"],
            "class_name": "StaticMesh",
            "source_files": [self._file_record(generated["path"], role="generated_source", file_format="obj")],
            "desired_name": asset_id.replace(".", "_"),
            "expected_size_m": list(normalized["size_m"]),
            "provider_id": PROVIDER_ID,
            "provider_version": PROVIDER_VERSION,
            "importer_contract_version": IMPORTER_CONTRACT_VERSION,
        }
        return (
            PreparedProviderImport(
                kind="procedural",
                request=request,
                intent=intent,
                request_dir=request_dir,
                import_request=self._backend_import_request(payload),
                normalized=normalized,
                generated=generated,
            ),
            None,
        )

    def _prepare_remote(
        self,
        request: dict[str, Any],
        *,
        intent: CompiledAssetIntent,
        adapter: RemoteProviderAdapter,
    ) -> tuple[
        PreparedProviderImport | None,
        tuple[dict[str, Any], dict[str, Any] | None] | None,
    ]:
        workspace_failure = self._workspace_failure(request)
        if workspace_failure is not None:
            return None, workspace_failure
        request_dir = self.workspace / "providers" / adapter.provider_id / request["request_digest"]
        if request.get("route") == "model_generation":
            budget_failure = self._reserve_paid_submission(
                request,
                provider_id=adapter.provider_id,
                request_dir=request_dir,
            )
            if budget_failure is not None:
                return None, (budget_failure, None)
        legacy_empty_submission = (
            adapter.provider_id == "meshy_model_generation_v1"
            and request_dir.is_dir()
            and not any(request_dir.iterdir())
        )
        request_path = request_dir / "provider_request.json"
        if request_path.is_file():
            try:
                saved_request = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                error = RemoteProviderError(
                    "provider_request_invalid",
                    f"saved Provider request cannot be read: {exc}",
                    status="blocked",
                )
                receipt = self._remote_failure_receipt(
                    request=request,
                    adapter=adapter,
                    request_dir=request_dir,
                    error=error,
                )
                return None, (
                    provider_failure(
                        request,
                        status=error.status,
                        code=error.code,
                        message=error.message,
                        receipt_ids=[receipt["receipt_id"]],
                    ),
                    receipt,
                )
            if saved_request != request:
                error = RemoteProviderError(
                    "provider_request_mismatch",
                    "saved Provider request differs from the compiled request with the same digest",
                    status="blocked",
                )
                receipt = self._remote_failure_receipt(
                    request=request,
                    adapter=adapter,
                    request_dir=request_dir,
                    error=error,
                )
                return None, (
                    provider_failure(
                        request,
                        status=error.status,
                        code=error.code,
                        message=error.message,
                        receipt_ids=[receipt["receipt_id"]],
                    ),
                    receipt,
                )
        else:
            write_json(request_path, request)
        if legacy_empty_submission:
            write_json(
                request_dir / "submission_attempt.json",
                {
                    "schema_version": "harness_meshy_submission_attempt_v1",
                    "provider_id": adapter.provider_id,
                    "request_identity": request["request_digest"],
                    "state": "unknown",
                    "failure_code": "legacy_post_result_unknown",
                },
            )
        try:
            acquisition = adapter.acquire(request, destination=request_dir, workspace=self.workspace)
        except RemoteProviderError as exc:
            receipt = self._remote_failure_receipt(request=request, adapter=adapter, request_dir=request_dir, error=exc)
            return None, (
                provider_failure(
                    request,
                    status=exc.status,
                    code=exc.code,
                    message=exc.message,
                    retriable=exc.retriable,
                    receipt_ids=[receipt["receipt_id"]],
                ),
                receipt,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            error = RemoteProviderError("provider_execution_failed", f"remote provider adapter failed closed: {exc}")
            receipt = self._remote_failure_receipt(request=request, adapter=adapter, request_dir=request_dir, error=error)
            return None, (
                provider_failure(
                    request,
                    status="failed",
                    code=error.code,
                    message=error.message,
                    receipt_ids=[receipt["receipt_id"]],
                ),
                receipt,
            )
        import_record = next(
            (row for row in acquisition.files if Path(str(row["path"])).resolve() == acquisition.import_file.resolve()),
            None,
        )
        if import_record is None:
            return None, (
                provider_failure(
                    request,
                    status="failed",
                    code="provider_output_invalid",
                    message="remote acquisition import file is absent from the verified output manifest",
                ),
                None,
            )
        payload: dict[str, Any] = {
            "schema_version": BACKEND_IMPORT_REQUEST_SCHEMA,
            "asset_id": acquisition.asset_id,
            "target_backend": request["target_backend"],
            "class_name": "StaticMesh",
            "source_files": [
                {
                    "role": "provider_import_source",
                    "local_path": str(acquisition.import_file),
                    "format": str(import_record["format"]),
                    "sha256": str(import_record["sha256"]),
                    "byte_size": int(import_record["byte_size"]),
                    "materialized": True,
                }
            ],
            "desired_name": acquisition.asset_id.replace(".", "_"),
            "source_kind": acquisition.source_kind,
            "provider_id": acquisition.provider_id,
            "provider_version": acquisition.provider_version,
            "importer_contract_version": IMPORTER_CONTRACT_VERSION,
        }
        if acquisition.expected_size_m is not None:
            payload["expected_size_m"] = list(acquisition.expected_size_m)
        return (
            PreparedProviderImport(
                kind="remote",
                request=request,
                intent=intent,
                request_dir=request_dir,
                import_request=self._backend_import_request(payload),
                acquisition=acquisition,
            ),
            None,
        )

    def _reserve_paid_submission(
        self,
        request: Mapping[str, Any],
        *,
        provider_id: str,
        request_dir: Path,
    ) -> dict[str, Any] | None:
        """Reserve a job-scoped paid slot before an adapter can issue a new POST.

        Existing acquisition/task evidence is recovery work and consumes no new
        slot. A reservation is durable before the adapter call so a crash cannot
        reopen the batch budget for another distinct request.
        """
        if self.max_paid_submissions is None:
            return None
        if (request_dir / "acquisition.json").is_file() or (request_dir / "task_checkpoint.json").is_file():
            return None
        submission_path = request_dir / "submission_attempt.json"
        if submission_path.is_file():
            try:
                submission = read_json(submission_path)
            except (OSError, ValueError, TypeError):
                submission = {}
            if str(submission.get("state") or "") in {"attempting", "unknown", "acknowledged"}:
                return None

        ledger = self._load_paid_submission_ledger()
        requests = ledger["requests"]
        digest = str(request.get("request_digest") or "")
        if digest in requests:
            return None
        if len(requests) >= self.max_paid_submissions:
            return provider_failure(
                request,
                status="blocked",
                code="paid_provider_budget_exhausted",
                message="the job paid Provider submission budget is exhausted before this request",
            )
        requests[digest] = {
            "request_digest": digest,
            "provider_id": provider_id,
            "submission_state": "reserved",
            "reserved_at_epoch": time.time(),
        }
        self._write_paid_submission_ledger(ledger)
        return None

    def _load_paid_submission_ledger(self) -> dict[str, Any]:
        if self.paid_submission_ledger_path is None:
            return {
                "schema_version": "harness_agent_provider_usage_v1",
                "requests": self._paid_submission_reservations,
            }
        if not self.paid_submission_ledger_path.is_file():
            return {"schema_version": "harness_agent_provider_usage_v1", "requests": {}}
        value = read_json(self.paid_submission_ledger_path)
        if (
            not isinstance(value, Mapping)
            or value.get("schema_version") != "harness_agent_provider_usage_v1"
            or not isinstance(value.get("requests"), Mapping)
        ):
            raise ValueError("paid Provider submission ledger is invalid")
        return {"schema_version": "harness_agent_provider_usage_v1", "requests": dict(value["requests"])}

    def _write_paid_submission_ledger(self, ledger: Mapping[str, Any]) -> None:
        if self.paid_submission_ledger_path is None:
            self._paid_submission_reservations = dict(ledger["requests"])
            return
        write_json(self.paid_submission_ledger_path, dict(ledger))

    def _workspace_failure(
        self,
        request: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
        try:
            self.workspace.relative_to(SOURCE_ROOT)
        except ValueError:
            return None
        return (
            provider_failure(
                request,
                status="blocked",
                code="workspace_inside_source_repository",
                message="Provider outputs cannot be written inside the source repository",
            ),
            None,
        )

    @staticmethod
    def _backend_import_request(payload: Mapping[str, Any]) -> BackendImportRequest:
        identity = dict(payload)
        digest_identity = copy.deepcopy(identity)
        digest_identity["source_files"] = [
            {key: value for key, value in row.items() if key != "local_path"}
            for row in identity.get("source_files") or []
        ]
        digest = stable_digest(digest_identity)
        return BackendImportRequest.from_dict(
            {
                **identity,
                "request_id": f"backend-import.{digest[:24]}",
                "request_digest": digest,
            }
        )

    def _run_prepared_imports(
        self,
        prepared_imports: list[PreparedProviderImport],
        *,
        registry: AssetRegistry,
    ) -> tuple[dict[str, BackendImportResult], dict[str, Any]]:
        results: dict[str, BackendImportResult] = {}
        misses: list[PreparedProviderImport] = []
        batch_capable = hasattr(self.importer, "import_assets")
        for prepared in prepared_imports:
            cached = self._cached_import_result(prepared, registry=registry)
            if cached is None:
                misses.append(prepared)
            else:
                results[prepared.import_request.data["request_digest"]] = cached
        if misses:
            inputs = [(item.import_request, item.request_dir) for item in misses]
            if batch_capable:
                batch_requests = [item.import_request for item in misses]
                imported = (
                    self.importer.import_assets(inputs, workspace=self.workspace)
                    if self._record_ue_importer_launch(batch_requests)
                    else [self._ue_launch_budget_failure(request) for request in batch_requests]
                )
            else:
                imported = []
                for request, work_dir in inputs:
                    imported.append(
                        self.importer.import_asset(request, work_dir=work_dir, workspace=self.workspace)
                        if self._record_ue_importer_launch([request])
                        else self._ue_launch_budget_failure(request)
                    )
            if len(imported) != len(misses):
                raise RuntimeError("backend importer returned the wrong number of batch results")
            for prepared, result in zip(misses, imported):
                results[prepared.import_request.data["request_digest"]] = result
                if result.data.get("status") == "fulfilled":
                    try:
                        validate_import_result(prepared.import_request, result, workspace=self.workspace)
                    except ValueError:
                        continue
                    write_json(self._import_cache_path(prepared), result.to_dict())
        return results, {
            "request_count": len(prepared_imports),
            "cache_hit_count": len(prepared_imports) - len(misses),
            "cache_miss_count": len(misses),
            "importer_invocation_count": (1 if batch_capable else len(misses)) if misses else 0,
            "batch_imported_count": len(misses),
        }

    def _record_ue_importer_launch(self, requests: list[BackendImportRequest]) -> bool:
        if self.ue_launch_ledger_path is None:
            return True
        path = self.ue_launch_ledger_path
        ledger = read_json(path) if path.is_file() else {
            "schema_version": "harness_agent_ue_launch_ledger_v1",
            "job_id": self.usage_job_id,
            "baseline_launches": 0,
            "launches": [],
        }
        if (
            not isinstance(ledger, Mapping)
            or ledger.get("schema_version") != "harness_agent_ue_launch_ledger_v1"
            or ledger.get("job_id") != self.usage_job_id
            or not isinstance(ledger.get("baseline_launches"), int)
            or isinstance(ledger.get("baseline_launches"), bool)
            or int(ledger.get("baseline_launches")) < 0
            or not isinstance(ledger.get("launches"), list)
        ):
            raise ValueError("UE launch ledger is invalid")
        launches = [dict(row) for row in ledger["launches"] if isinstance(row, Mapping)]
        if (
            self.max_ue_launches is not None
            and int(ledger["baseline_launches"]) + len(launches) >= self.max_ue_launches
        ):
            return False
        launches.append(
            {
                "sequence": len(launches) + 1,
                "kind": "asset_importer",
                "attempt_id": self.usage_attempt_id,
                "request_digests": sorted(request.data["request_digest"] for request in requests),
                "recorded_at_epoch": time.time(),
            }
        )
        write_json(path, {**dict(ledger), "launches": launches})
        return True

    @staticmethod
    def _ue_launch_budget_failure(request: BackendImportRequest) -> BackendImportResult:
        return BackendImportResult.from_dict(
            {
                "schema_version": BACKEND_IMPORT_RESULT_SCHEMA,
                "request_id": request.data["request_id"],
                "request_digest": request.data["request_digest"],
                "asset_id": request.data["asset_id"],
                "status": "blocked",
                "failure": {
                    "code": "ue_launch_budget_exhausted",
                    "message": "UE launch budget is exhausted before backend import",
                    "retriable": False,
                },
                "stdout": "",
                "stderr": "",
                "returncode": None,
            }
        )

    def _cached_import_result(
        self,
        prepared: PreparedProviderImport,
        *,
        registry: AssetRegistry,
    ) -> BackendImportResult | None:
        existing = registry.get_asset_by_id(str(prepared.import_request.data["asset_id"]))
        qualification = existing.get("qualification") if isinstance(existing, Mapping) else None
        if (
            not isinstance(existing, Mapping)
            or existing.get("lifecycle_status") != "runtime_bound"
            or not isinstance(qualification, Mapping)
            or not str(qualification.get("status") or "").startswith("pass")
        ):
            return None
        parsed: BackendImportResult | None = None
        for result_path in (self._import_cache_path(prepared), prepared.request_dir / "backend_import_result.json"):
            try:
                raw = json.loads(result_path.read_text(encoding="utf-8"))
                candidate = BackendImportResult.from_dict(raw)
                validate_import_result(prepared.import_request, candidate, workspace=self.workspace)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            parsed = candidate
            break
        if parsed is None:
            return None
        if parsed.data.get("status") != "fulfilled" or existing.get("ue_path") != parsed.data.get("object_path"):
            return None
        reused = copy.deepcopy(parsed.to_dict())
        reused["cache_hit"] = True
        reused["importer_invoked"] = False
        return BackendImportResult.from_dict(reused)

    def _import_cache_path(self, prepared: PreparedProviderImport) -> Path:
        digest = str(prepared.import_request.data["request_digest"])
        return self.workspace / "providers" / "_import_cache" / digest / "backend_import_result.json"

    def _complete_prepared(
        self,
        prepared: PreparedProviderImport,
        *,
        import_result: BackendImportResult,
        registry: AssetRegistry,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if prepared.kind == "remote":
            return self._complete_remote(prepared, import_result=import_result, registry=registry)
        if prepared.kind != "procedural":
            raise AssertionError(f"unknown prepared provider kind: {prepared.kind}")
        return self._complete_procedural(prepared, import_result=import_result, registry=registry)

    def _complete_procedural(
        self,
        prepared: PreparedProviderImport,
        *,
        import_result: BackendImportResult,
        registry: AssetRegistry,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        request = prepared.request
        intent = prepared.intent
        normalized = prepared.normalized
        generated = prepared.generated
        assert normalized is not None and generated is not None
        asset_id = stable_asset_id(normalized)
        lifecycle = SUCCESSFUL_LIFECYCLE[:5]
        import_request = prepared.import_request
        importer_request_digest = stable_digest(import_request.to_dict())
        importer_result_digest = stable_digest(import_result.to_dict())
        receipt_digest = stable_digest(
            {
                "request_digest": request["request_digest"],
                "provider_version": PROVIDER_VERSION,
                "generator_source_version": GENERATOR_SOURCE_VERSION,
                "importer_request_digest": importer_request_digest,
                "importer_result_digest": importer_result_digest,
            }
        )
        receipt_id = f"provider-receipt.{receipt_digest}"
        if import_result.data["status"] != "fulfilled":
            failure = import_result.data["failure"]
            receipt = self._receipt(
                receipt_id=receipt_id,
                request=request,
                normalized=normalized,
                generated=generated,
                lifecycle=lifecycle,
                status=import_result.data["status"],
                importer_request_digest=importer_request_digest,
                importer_result_digest=importer_result_digest,
                backend_binding={},
                importer_result=import_result.data,
            )
            return (
                provider_failure(
                    request,
                    status=str(import_result.data["status"]),
                    code=str(failure["code"]),
                    message=str(failure["message"]),
                    retriable=bool(failure["retriable"]),
                    receipt_ids=[receipt_id],
                ),
                receipt,
            )
        lifecycle = SUCCESSFUL_LIFECYCLE[:6]
        asset = self._catalog_asset(
            request=request,
            intent=intent,
            normalized=normalized,
            generated=generated,
            import_result=import_result.data,
            receipt_id=receipt_id,
        )
        existing_asset = registry.get_asset_by_id(asset_id)
        if (
            existing_asset
            and existing_asset.get("lifecycle_status") == "runtime_bound"
            and existing_asset.get("sha256") == asset.get("sha256")
            and existing_asset.get("ue_path") == asset.get("ue_path")
        ):
            asset["lifecycle_status"] = "runtime_bound"
            asset["qualification"] = copy.deepcopy(existing_asset.get("qualification") or {})
        registration = registry.register_asset(asset)
        if registration.get("status") != "registered" or registry.get_asset_by_id(asset_id) is None:
            receipt = self._receipt(
                receipt_id=receipt_id,
                request=request,
                normalized=normalized,
                generated=generated,
                lifecycle=lifecycle,
                status="failed",
                importer_request_digest=importer_request_digest,
                importer_result_digest=importer_result_digest,
                backend_binding=asset["backend_bindings"]["unreal"],
                importer_result=import_result.data,
            )
            return (
                provider_failure(
                    request,
                    status="failed",
                    code=str(registration.get("code") or "catalog_registration_failed"),
                    message=str(registration.get("message") or "Catalog registration failed"),
                    receipt_ids=[receipt_id],
                ),
                receipt,
            )
        lifecycle = SUCCESSFUL_LIFECYCLE[:7]
        registered = registry.get_asset_by_id(asset_id)
        assert registered is not None
        quality = asset_quality_gate(
            registered,
            physics_critical=intent.legacy_intent.physics_critical,
            allow_local_preview=request["required_license_tier"] == "local_preview",
        )
        hard_constraints_match = candidate_matches_search_intent(registered, intent.search_intent)
        if not hard_constraints_match or not str(quality["status"]).startswith("pass"):
            qualification_failures = [*quality["failure_codes"]]
            if not hard_constraints_match:
                qualification_failures.append("hard_constraint_mismatch")
            receipt = self._receipt(
                receipt_id=receipt_id,
                request=request,
                normalized=normalized,
                generated=generated,
                lifecycle=lifecycle,
                status="failed",
                importer_request_digest=importer_request_digest,
                importer_result_digest=importer_result_digest,
                backend_binding=asset["backend_bindings"]["unreal"],
                importer_result=import_result.data,
            )
            return (
                provider_failure(
                    request,
                    status="failed",
                    code="asset_qualification_failed",
                    message=f"registered generated asset failed qualification: {qualification_failures}",
                    receipt_ids=[receipt_id],
                ),
                receipt,
            )
        runtime_bound_asset = copy.deepcopy(registered)
        runtime_bound_asset["lifecycle_status"] = "runtime_bound"
        runtime_bound_asset["qualification"] = copy.deepcopy(quality)
        final_registration = registry.register_asset(runtime_bound_asset)
        final_asset = registry.get_asset_by_id(asset_id)
        if final_registration.get("status") != "registered" or not final_asset or final_asset.get("lifecycle_status") != "runtime_bound":
            receipt = self._receipt(
                receipt_id=receipt_id,
                request=request,
                normalized=normalized,
                generated=generated,
                lifecycle=SUCCESSFUL_LIFECYCLE[:8],
                status="failed",
                importer_request_digest=importer_request_digest,
                importer_result_digest=importer_result_digest,
                backend_binding=asset["backend_bindings"]["unreal"],
                importer_result=import_result.data,
            )
            return (
                provider_failure(
                    request,
                    status="failed",
                    code="runtime_binding_registration_failed",
                    message="qualified asset could not be persisted as runtime_bound",
                    receipt_ids=[receipt_id],
                ),
                receipt,
            )
        lifecycle = list(SUCCESSFUL_LIFECYCLE)
        receipt = self._receipt(
            receipt_id=receipt_id,
            request=request,
            normalized=normalized,
            generated=generated,
            lifecycle=lifecycle,
            status="fulfilled",
            importer_request_digest=importer_request_digest,
            importer_result_digest=importer_result_digest,
            backend_binding=asset["backend_bindings"]["unreal"],
            importer_result=import_result.data,
        )
        result = ProviderResult.from_dict(
            {
                "schema_version": PROVIDER_RESULT_SCHEMA,
                "request_id": request["request_id"],
                "request_digest": request["request_digest"],
                "object_id": request["object_id"],
                "slot": request["slot"],
                "status": "fulfilled",
                "catalog_asset_ids": [asset_id],
                "receipt_ids": [receipt_id],
            }
        ).to_dict()
        return result, receipt

    def _complete_remote(
        self,
        prepared: PreparedProviderImport,
        *,
        import_result: BackendImportResult,
        registry: AssetRegistry,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        request = prepared.request
        intent = prepared.intent
        acquisition = prepared.acquisition
        assert acquisition is not None
        import_request = prepared.import_request
        importer_request_digest = stable_digest(import_request.to_dict())
        importer_result_digest = stable_digest(import_result.to_dict())
        receipt_digest = stable_digest(
            {
                "request_digest": request["request_digest"],
                "provider_id": acquisition.provider_id,
                "provider_version": acquisition.provider_version,
                "source_asset_id": acquisition.source_asset_id,
                "outputs": [{"format": row["format"], "sha256": row["sha256"]} for row in acquisition.files],
                "importer_request_digest": importer_request_digest,
                "importer_result_digest": importer_result_digest,
            }
        )
        receipt_id = f"provider-receipt.{receipt_digest}"
        if import_result.data["status"] != "fulfilled":
            failure = import_result.data["failure"]
            receipt = self._remote_receipt(
                receipt_id=receipt_id,
                request=request,
                acquisition=acquisition,
                lifecycle=SUCCESSFUL_LIFECYCLE[:5],
                status=str(import_result.data["status"]),
                importer_request_digest=importer_request_digest,
                importer_result_digest=importer_result_digest,
                backend_binding={},
                importer_result=import_result.data,
            )
            return (
                provider_failure(
                    request,
                    status=str(import_result.data["status"]),
                    code=str(failure["code"]),
                    message=str(failure["message"]),
                    retriable=bool(failure["retriable"]),
                    receipt_ids=[receipt_id],
                ),
                receipt,
            )
        asset = self._remote_catalog_asset(
            request=request,
            intent=intent,
            acquisition=acquisition,
            import_result=import_result.data,
            receipt_id=receipt_id,
        )
        existing_asset = registry.get_asset_by_id(acquisition.asset_id)
        if (
            existing_asset
            and existing_asset.get("lifecycle_status") == "runtime_bound"
            and existing_asset.get("sha256") == asset.get("sha256")
            and existing_asset.get("ue_path") == asset.get("ue_path")
        ):
            asset["lifecycle_status"] = "runtime_bound"
            asset["qualification"] = copy.deepcopy(existing_asset.get("qualification") or {})
        registration = registry.register_asset(asset)
        if registration.get("status") != "registered" or registry.get_asset_by_id(acquisition.asset_id) is None:
            receipt = self._remote_receipt(
                receipt_id=receipt_id,
                request=request,
                acquisition=acquisition,
                lifecycle=SUCCESSFUL_LIFECYCLE[:6],
                status="failed",
                importer_request_digest=importer_request_digest,
                importer_result_digest=importer_result_digest,
                backend_binding=asset["backend_bindings"]["unreal"],
                importer_result=import_result.data,
            )
            return (
                provider_failure(
                    request,
                    status="failed",
                    code=str(registration.get("code") or "catalog_registration_failed"),
                    message=str(registration.get("message") or "Catalog registration failed"),
                    receipt_ids=[receipt_id],
                ),
                receipt,
            )
        registered = registry.get_asset_by_id(acquisition.asset_id)
        assert registered is not None
        quality = asset_quality_gate(
            registered,
            physics_critical=intent.legacy_intent.physics_critical,
            allow_local_preview=request["required_license_tier"] == "local_preview",
        )
        hard_constraints_match = candidate_matches_search_intent(registered, intent.search_intent)
        if not hard_constraints_match or not str(quality["status"]).startswith("pass"):
            failures = [*quality["failure_codes"]]
            if not hard_constraints_match:
                failures.append("hard_constraint_mismatch")
            receipt = self._remote_receipt(
                receipt_id=receipt_id,
                request=request,
                acquisition=acquisition,
                lifecycle=SUCCESSFUL_LIFECYCLE[:7],
                status="failed",
                importer_request_digest=importer_request_digest,
                importer_result_digest=importer_result_digest,
                backend_binding=asset["backend_bindings"]["unreal"],
                importer_result=import_result.data,
            )
            return (
                provider_failure(
                    request,
                    status="failed",
                    code="asset_qualification_failed",
                    message=f"registered remote asset failed qualification: {failures}",
                    receipt_ids=[receipt_id],
                ),
                receipt,
            )
        runtime_bound_asset = copy.deepcopy(registered)
        runtime_bound_asset["lifecycle_status"] = "runtime_bound"
        runtime_bound_asset["qualification"] = copy.deepcopy(quality)
        final_registration = registry.register_asset(runtime_bound_asset)
        final_asset = registry.get_asset_by_id(acquisition.asset_id)
        if final_registration.get("status") != "registered" or not final_asset or final_asset.get("lifecycle_status") != "runtime_bound":
            receipt = self._remote_receipt(
                receipt_id=receipt_id,
                request=request,
                acquisition=acquisition,
                lifecycle=SUCCESSFUL_LIFECYCLE[:8],
                status="failed",
                importer_request_digest=importer_request_digest,
                importer_result_digest=importer_result_digest,
                backend_binding=asset["backend_bindings"]["unreal"],
                importer_result=import_result.data,
            )
            return (
                provider_failure(
                    request,
                    status="failed",
                    code="runtime_binding_registration_failed",
                    message="qualified remote asset could not be persisted as runtime_bound",
                    receipt_ids=[receipt_id],
                ),
                receipt,
            )
        receipt = self._remote_receipt(
            receipt_id=receipt_id,
            request=request,
            acquisition=acquisition,
            lifecycle=list(SUCCESSFUL_LIFECYCLE),
            status="fulfilled",
            importer_request_digest=importer_request_digest,
            importer_result_digest=importer_result_digest,
            backend_binding=asset["backend_bindings"]["unreal"],
            importer_result=import_result.data,
        )
        result = ProviderResult.from_dict(
            {
                "schema_version": PROVIDER_RESULT_SCHEMA,
                "request_id": request["request_id"],
                "request_digest": request["request_digest"],
                "object_id": request["object_id"],
                "slot": request["slot"],
                "status": "fulfilled",
                "catalog_asset_ids": [acquisition.asset_id],
                "receipt_ids": [receipt_id],
            }
        ).to_dict()
        return result, receipt

    def _remote_failure_receipt(
        self,
        *,
        request: Mapping[str, Any],
        adapter: RemoteProviderAdapter,
        request_dir: Path,
        error: RemoteProviderError,
    ) -> dict[str, Any]:
        details = copy.deepcopy(error.details)
        task_id = str(details.get("task_id") or "").strip()
        source_kind = str(getattr(adapter, "source_kind", request.get("route") or "remote_provider"))
        source_uri = (
            f"meshy://multi-image-to-3d/{task_id}"
            if task_id and source_kind == "model_generation"
            else f"provider://{adapter.provider_id}/{request['request_digest']}"
        )
        outputs: list[dict[str, Any]] = []
        checkpoint = request_dir / "task_checkpoint.json"
        if checkpoint.is_file():
            try:
                checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                checkpoint_payload = {"checkpoint_unreadable": True}
            audit_payload = {
                "schema_version": "harness_remote_provider_failure_audit_v1",
                "provider_id": adapter.provider_id,
                "request_digest": request["request_digest"],
                "failure": {
                    "code": error.code,
                    "status": error.status,
                    "retriable": error.retriable,
                },
                "task_checkpoint": checkpoint_payload,
            }
            audit_path = request_dir / "failure_audits" / f"{stable_digest(audit_payload)}.json"
            write_json(audit_path, audit_payload)
            outputs.append(self._receipt_file(audit_path, role="provider_task_checkpoint", file_format="json"))
        submission_attempt = request_dir / "submission_attempt.json"
        if submission_attempt.is_file():
            outputs.append(
                self._receipt_file(
                    submission_attempt,
                    role="provider_submission_attempt",
                    file_format="json",
                )
            )
        request_path = request_dir / "provider_request.json"
        if request_path.is_file():
            outputs.append(self._receipt_file(request_path, role="provider_request", file_format="json"))
        inputs = [
            {"input_id": str(row["input_id"]), "sha256": str(row["sha256"])}
            for row in request.get("reference_inputs") or []
            if isinstance(row, Mapping) and row.get("input_id") and _is_sha256(str(row.get("sha256") or ""))
        ]
        receipt_digest = stable_digest(
            {
                "request_digest": request["request_digest"],
                "provider_id": adapter.provider_id,
                "provider_version": adapter.provider_version,
                "failure_code": error.code,
                "task_id": task_id,
                "outputs": [{"path": row["path"], "sha256": row["sha256"]} for row in outputs],
            }
        )
        recipe_parameters = {
            "provider_hint": request.get("provider_hint"),
            "source_uri_hint": request.get("source_uri_hint"),
        }
        if "texture_prompt" in request:
            recipe_parameters["texture_prompt"] = request.get("texture_prompt")
        receipt = {
            "schema_version": PROVIDER_RECEIPT_SCHEMA,
            "receipt_id": f"provider-receipt.{receipt_digest}",
            "status": error.status,
            "provider_id": adapter.provider_id,
            "provider_version": adapter.provider_version,
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "recipe_id": task_id or request["request_digest"],
            "recipe_version": adapter.provider_version,
            "recipe_parameters": recipe_parameters,
            "generator_source_version": adapter.provider_version,
            "input_identities": inputs,
            "output_files": outputs,
            "source_kind": source_kind,
            "source_uri": source_uri,
            "author": "Meshy" if source_kind == "model_generation" else adapter.provider_id,
            "license": "All Rights Reserved" if source_kind == "model_generation" else "Unknown",
            "redistribution": {},
            "lifecycle_transitions": ["requested"],
            "importer_request_digest": "0" * 64,
            "importer_result_digest": "0" * 64,
            "provider_execution": {
                "status": error.status,
                "failure_code": error.code,
                "retriable": error.retriable,
                **details,
            },
            "importer_execution": {},
            "backend_binding": {},
        }
        return ProviderReceipt.from_dict(receipt).to_dict()

    def _remote_catalog_asset(
        self,
        *,
        request: Mapping[str, Any],
        intent: CompiledAssetIntent,
        acquisition: RemoteAcquisition,
        import_result: Mapping[str, Any],
        receipt_id: str,
    ) -> dict[str, Any]:
        imported_files = [dict(row) for row in import_result.get("files") or []]
        for index, row in enumerate(imported_files):
            row.setdefault("role", "primary" if index == 0 else "imported_dependency")
            row.setdefault("format", Path(str(row.get("local_path") or "")).suffix.lstrip("."))
        primary = next((row for row in imported_files if row.get("role") == "primary"), imported_files[0])
        dependencies = [dict(row) for row in import_result.get("dependencies") or []]
        import_validation = import_result.get("import_validation") if isinstance(import_result.get("import_validation"), Mapping) else {}
        actual_size_cm = import_validation.get("actual_size_cm")
        size = (
            [float(value) / 100.0 for value in actual_size_cm]
            if isinstance(actual_size_cm, list)
            and len(actual_size_cm) == 3
            and all(isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) > 0.0 for value in actual_size_cm)
            else list(acquisition.expected_size_m)
            if acquisition.expected_size_m is not None
            else None
        )
        volume_m3 = math.prod(size) if size else 0.001
        requested_geometry = next(
            (
                value.casefold()
                for value in _string_values(intent.search_intent.must.get("geometry_type"))
                if value.casefold() in {"box", "sphere", "cylinder", "capsule", "convex", "mesh"}
            ),
            None,
        )
        tags = [
            intent.legacy_intent.role,
            *_string_values(intent.search_intent.must.get("physics_role")),
            acquisition.source_kind,
            *[str(value) for value in acquisition.metadata.get("tags") or []],
        ]
        source_files = [
            {
                "role": str(row["role"]),
                "local_path": str(row["path"]),
                "format": str(row["format"]),
                "sha256": str(row["sha256"]),
                "byte_size": int(row["byte_size"]),
                "materialized": True,
            }
            for row in acquisition.files
        ]
        requested_categories = _string_values(intent.search_intent.must.get("category"))
        requested_category = (
            requested_categories[0]
            if requested_categories
            else str(intent.search_intent.taxonomy.get("category") or intent.legacy_intent.category)
        )
        requested_subcategory = str(intent.search_intent.taxonomy.get("subcategory") or "").strip()
        return {
            "asset_id": acquisition.asset_id,
            "name": acquisition.name,
            "semantic_name": str(intent.search_intent.raw_query),
            "description": acquisition.description,
            "aliases": [acquisition.asset_id, acquisition.source_asset_id, acquisition.name],
            "tags": list(dict.fromkeys(value for value in tags if value)),
            "category": requested_category,
            "category_l1": requested_category,
            "category_l2": requested_subcategory or None,
            "type": "StaticMesh",
            "asset_kind": "StaticMesh",
            "source_kind": acquisition.source_kind,
            "source_uri": acquisition.source_uri,
            "author": acquisition.author,
            "license": acquisition.license,
            "license_tier": acquisition.license_tier,
            "redistribution": {},
            "quality_status": "approved",
            "lifecycle_status": "registered",
            "materialized": True,
            "ue_path": import_result["object_path"],
            "class_name": import_result["class_name"],
            "local_path": str(primary["local_path"]),
            "sha256": str(primary["sha256"]),
            "byte_size": int(primary.get("byte_size") or Path(str(primary["local_path"])).stat().st_size),
            "bbox_size_m": size,
            "authored_size_m": size,
            "provider_reported_size_m": (
                list(acquisition.expected_size_m) if acquisition.expected_size_m is not None else None
            ),
            "preserve_authored_scale": True,
            # This is the semantic geometry contract used to choose the
            # provider asset.  Keep it separate from `collider`, which records
            # the imported mesh's actual generic simple-convex BodySetup.
            "shape": requested_geometry,
            "collider": "box",
            "collision_profile": "PhysicsActor",
            "mass_kg": max(volume_m3 * 1000.0, 0.001),
            "material": {"static_friction": 0.5, "dynamic_friction": 0.4, "restitution": 0.1},
            "collision": {"present": True, "kind": "simple_convex"},
            "files": [*imported_files, *source_files],
            "ue": {
                "object_path": import_result["object_path"],
                "class_name": import_result["class_name"],
                # Qualification keys dependency records by package when one is
                # available. Keep this declaration in the same identity domain;
                # Unreal object paths include an additional `.ObjectName` suffix.
                "dependencies": [str(row.get("package") or row.get("dependency_id")) for row in dependencies],
            },
            "bundle": {"dependencies": dependencies},
            "backend_bindings": {
                "unreal": {
                    "backend": "unreal",
                    "object_path": import_result["object_path"],
                    "class_name": import_result["class_name"],
                    "materialized": True,
                    "runtime_ready": True,
                    "files": imported_files,
                    "dependencies": dependencies,
                }
            },
            "provenance": {
                "provider_id": acquisition.provider_id,
                "provider_version": acquisition.provider_version,
                "receipt_id": receipt_id,
                "source_asset_id": acquisition.source_asset_id,
                "canonical_file_sha256": next(
                    str(row["sha256"])
                    for row in acquisition.files
                    if Path(str(row["path"])).resolve() == acquisition.canonical_file.resolve()
                ),
                "provider_metadata": copy.deepcopy(acquisition.metadata),
            },
        }

    def _remote_receipt(
        self,
        *,
        receipt_id: str,
        request: Mapping[str, Any],
        acquisition: RemoteAcquisition,
        lifecycle: list[str],
        status: str,
        importer_request_digest: str,
        importer_result_digest: str,
        backend_binding: Mapping[str, Any],
        importer_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        outputs = [
            self._receipt_file(
                Path(str(row["path"])),
                role=str(row["role"]),
                file_format=str(row["format"]),
            )
            for row in acquisition.files
        ]
        request_path = (
            self.workspace
            / "providers"
            / acquisition.provider_id
            / str(request["request_digest"])
            / "provider_request.json"
        )
        if request_path.is_file():
            outputs.append(self._receipt_file(request_path, role="provider_request", file_format="json"))
        for row in importer_result.get("files") or []:
            outputs.append(
                self._receipt_file(
                    Path(str(row["local_path"])),
                    role=str(row.get("role") or "imported_asset"),
                    file_format=str(row.get("format") or Path(str(row["local_path"])).suffix.lstrip(".")),
                )
            )
        for row in importer_result.get("dependencies") or []:
            if row.get("local_path"):
                outputs.append(
                    self._receipt_file(
                        Path(str(row["local_path"])),
                        role="dependency",
                        file_format=str(row.get("format") or Path(str(row["local_path"])).suffix.lstrip(".")),
                    )
                )
        receipt = {
            "schema_version": PROVIDER_RECEIPT_SCHEMA,
            "receipt_id": receipt_id,
            "status": status,
            "provider_id": acquisition.provider_id,
            "provider_version": acquisition.provider_version,
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "recipe_id": acquisition.source_asset_id,
            "recipe_version": acquisition.provider_version,
            "recipe_parameters": copy.deepcopy(acquisition.request_parameters),
            "generator_source_version": acquisition.provider_version,
            "input_identities": [dict(row) for row in acquisition.input_identities],
            "output_files": outputs,
            "source_kind": acquisition.source_kind,
            "source_uri": acquisition.source_uri,
            "author": acquisition.author,
            "license": acquisition.license,
            "redistribution": {},
            "lifecycle_transitions": lifecycle,
            "importer_request_digest": importer_request_digest,
            "importer_result_digest": importer_result_digest,
            "provider_execution": copy.deepcopy(acquisition.metadata),
            "importer_execution": {
                "status": importer_result.get("status"),
                "stdout": str(importer_result.get("stdout") or ""),
                "stderr": str(importer_result.get("stderr") or ""),
                "returncode": importer_result.get("returncode"),
                "cache_hit": bool(importer_result.get("cache_hit")),
                "importer_invoked": importer_result.get("importer_invoked", not bool(importer_result.get("cache_hit"))),
                "batch_size": importer_result.get("batch_size"),
            },
            "backend_binding": self._receipt_binding(backend_binding),
        }
        return ProviderReceipt.from_dict(receipt).to_dict()

    def _catalog_asset(
        self,
        *,
        request: Mapping[str, Any],
        intent: CompiledAssetIntent,
        normalized: Mapping[str, Any],
        generated: Mapping[str, Any],
        import_result: Mapping[str, Any],
        receipt_id: str,
    ) -> dict[str, Any]:
        asset_id = stable_asset_id(normalized)
        imported_files = [dict(row) for row in import_result.get("files") or []]
        for index, row in enumerate(imported_files):
            row.setdefault("role", "primary" if index == 0 else "imported_dependency")
            row.setdefault("format", Path(str(row.get("local_path") or "")).suffix.lstrip("."))
        primary = next((row for row in imported_files if row.get("role") == "primary"), imported_files[0])
        dependencies = [dict(row) for row in import_result.get("dependencies") or []]
        size = [float(value) for value in normalized["size_m"]]
        shape = str(normalized["shape"])
        volume_m3 = _primitive_volume_m3(shape, size)
        mass = volume_m3 * 1000.0
        redistribution = copy.deepcopy(self.redistribution_evidence)
        requested_tier = str(request["required_license_tier"])
        license_name = "All Rights Reserved"
        declared_tier = "reference" if requested_tier == "reference" else "local_preview"
        return {
            "asset_id": asset_id,
            "name": f"Generated {shape.title()} {asset_id.rsplit('.', 1)[-1]}",
            "semantic_name": str(intent.search_intent.raw_query),
            "description": f"Deterministic centered {shape} mesh generated by {normalized['recipe_id']}",
            "aliases": [asset_id, f"generated {shape}", f"{shape} mesh"],
            "tags": list(
                dict.fromkeys(
                    [
                        intent.legacy_intent.role,
                        *_string_values(intent.search_intent.must.get("physics_role")),
                        shape,
                        "procedural_generation",
                    ]
                )
            ),
            "category": intent.legacy_intent.category,
            "category_l1": intent.legacy_intent.category,
            "type": "StaticMesh",
            "asset_kind": "StaticMesh",
            "source_kind": "procedural_generation",
            "source_uri": f"provider://{PROVIDER_ID}/{recipe_digest(normalized)}",
            "author": "Physics-Aware Harness deterministic generator",
            "license": license_name,
            "license_tier": declared_tier,
            "redistribution": redistribution,
            "quality_status": "approved",
            "lifecycle_status": "registered",
            "materialized": True,
            "ue_path": import_result["object_path"],
            "class_name": import_result["class_name"],
            "local_path": str(primary["local_path"]),
            "sha256": str(primary["sha256"]),
            "byte_size": int(primary.get("byte_size") or Path(str(primary["local_path"])).stat().st_size),
            "bbox_size_m": size,
            "authored_size_m": size,
            "preserve_authored_scale": True,
            "collider": shape,
            "collision_profile": "PhysicsActor",
            "mass_kg": mass,
            "material": {"static_friction": 0.5, "dynamic_friction": 0.4, "restitution": 0.1},
            "collision": {"present": True, "kind": shape},
            "files": [
                *imported_files,
                {
                    "role": "generated_source",
                    "local_path": str(generated["path"]),
                    "format": "obj",
                    "sha256": generated["sha256"],
                    "byte_size": generated["byte_size"],
                    "materialized": True,
                },
            ],
            "ue": {
                "object_path": import_result["object_path"],
                "class_name": import_result["class_name"],
                "dependencies": [
                    str(row.get("package") or row.get("dependency_id")) for row in dependencies
                ],
            },
            "bundle": {"dependencies": dependencies},
            "backend_bindings": {
                "unreal": {
                    "backend": "unreal",
                    "object_path": import_result["object_path"],
                    "class_name": import_result["class_name"],
                    "materialized": True,
                    "runtime_ready": True,
                    "files": imported_files,
                    "dependencies": dependencies,
                }
            },
            "provenance": {
                "provider_id": PROVIDER_ID,
                "provider_version": PROVIDER_VERSION,
                "receipt_id": receipt_id,
                "recipe_id": normalized["recipe_id"],
                "recipe_version": normalized["recipe_version"],
                "recipe_digest": recipe_digest(normalized),
                "generator_source_version": GENERATOR_SOURCE_VERSION,
            },
        }

    def _receipt(
        self,
        *,
        receipt_id: str,
        request: Mapping[str, Any],
        normalized: Mapping[str, Any],
        generated: Mapping[str, Any],
        lifecycle: list[str],
        status: str,
        importer_request_digest: str,
        importer_result_digest: str,
        backend_binding: Mapping[str, Any],
        importer_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        outputs = [self._receipt_file(generated["path"], role="generated_source", file_format="obj")]
        for row in importer_result.get("files") or []:
            outputs.append(
                self._receipt_file(
                    Path(str(row["local_path"])),
                    role=str(row.get("role") or "imported_asset"),
                    file_format=str(row.get("format") or Path(str(row["local_path"])).suffix.lstrip(".")),
                )
            )
        for row in importer_result.get("dependencies") or []:
            if row.get("local_path"):
                outputs.append(
                    self._receipt_file(
                        Path(str(row["local_path"])),
                        role="dependency",
                        file_format=str(row.get("format") or Path(str(row["local_path"])).suffix.lstrip(".")),
                    )
                )
        receipt = {
            "schema_version": PROVIDER_RECEIPT_SCHEMA,
            "receipt_id": receipt_id,
            "status": status,
            "provider_id": PROVIDER_ID,
            "provider_version": PROVIDER_VERSION,
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "recipe_id": normalized["recipe_id"],
            "recipe_version": normalized["recipe_version"],
            "recipe_parameters": dict(normalized),
            "generator_source_version": GENERATOR_SOURCE_VERSION,
            "input_identities": [
                {
                    "input_id": str(row.get("input_id")),
                    "sha256": str(row.get("sha256")),
                }
                for row in request.get("reference_inputs") or []
                if row.get("input_id") and row.get("sha256")
            ],
            "output_files": outputs,
            "source_kind": "procedural_generation",
            "source_uri": f"provider://{PROVIDER_ID}/{recipe_digest(normalized)}",
            "author": "Physics-Aware Harness deterministic generator",
            "license": "All Rights Reserved",
            "redistribution": copy.deepcopy(self.redistribution_evidence),
            "lifecycle_transitions": lifecycle,
            "importer_request_digest": importer_request_digest,
            "importer_result_digest": importer_result_digest,
            "importer_execution": {
                "status": importer_result.get("status"),
                "stdout": str(importer_result.get("stdout") or ""),
                "stderr": str(importer_result.get("stderr") or ""),
                "returncode": importer_result.get("returncode"),
                "cache_hit": bool(importer_result.get("cache_hit")),
                "importer_invoked": importer_result.get("importer_invoked", not bool(importer_result.get("cache_hit"))),
                "batch_size": importer_result.get("batch_size"),
            },
            "backend_binding": self._receipt_binding(backend_binding),
        }
        return ProviderReceipt.from_dict(receipt).to_dict()

    def _file_record(self, path: Path, *, role: str, file_format: str) -> dict[str, Any]:
        return {
            "role": role,
            "local_path": str(path),
            "format": file_format,
            "sha256": self._sha256_file(path),
            "byte_size": path.stat().st_size,
            "materialized": True,
        }

    def _receipt_file(self, path: Path, *, role: str, file_format: str) -> dict[str, Any]:
        relative = path.resolve().relative_to(self.workspace)
        return {
            "path": relative.as_posix(),
            "role": role,
            "format": file_format,
            "sha256": self._sha256_file(path),
            "byte_size": path.stat().st_size,
        }

    def _receipt_binding(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        if not binding:
            return {}
        return {
            "backend": str(binding.get("backend") or "unreal"),
            "object_path": binding.get("object_path"),
            "class_name": binding.get("class_name"),
            "materialized": bool(binding.get("materialized")),
            "runtime_ready": bool(binding.get("runtime_ready")),
            "files": [
                self._receipt_file(
                    Path(str(row["local_path"])),
                    role=str(row.get("role") or "imported_asset"),
                    file_format=str(row.get("format") or Path(str(row["local_path"])).suffix.lstrip(".")),
                )
                for row in binding.get("files") or []
                if isinstance(row, Mapping) and row.get("local_path")
            ],
            "dependencies": [
                {
                    "dependency_id": str(row.get("dependency_id") or row.get("package") or ""),
                    **self._receipt_file(
                        Path(str(row["local_path"])),
                        role="dependency",
                        file_format=str(row.get("format") or Path(str(row["local_path"])).suffix.lstrip(".")),
                    ),
                }
                for row in binding.get("dependencies") or []
                if isinstance(row, Mapping) and row.get("local_path")
            ],
        }

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def _is_sha256(value: str) -> bool:
    normalized = str(value).casefold()
    return len(normalized) == 64 and all(character in "0123456789abcdef" for character in normalized)


def _string_values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [str(item) for item in values if str(item).strip()]


def _primitive_volume_m3(shape: str, size: list[float]) -> float:
    if shape == "sphere":
        return math.pi * size[0] ** 3 / 6.0
    if shape == "cylinder":
        return math.pi * (size[0] / 2.0) ** 2 * size[2]
    return size[0] * size[1] * size[2]
