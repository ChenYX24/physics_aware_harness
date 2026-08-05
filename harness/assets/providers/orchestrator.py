from __future__ import annotations

import copy
import hashlib
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from harness.assets.asset_intent_compiler import CompiledAssetIntent
from harness.assets.asset_registry import AssetRegistry, candidate_matches_search_intent
from harness.assets.asset_resolver import asset_quality_gate
from harness.assets.providers.backend_importer import BackendImporterAdapter, UECommandImporterAdapter
from harness.assets.providers.contracts import (
    BACKEND_IMPORT_REQUEST_SCHEMA,
    PROVIDER_BATCH_SCHEMA,
    PROVIDER_RECEIPT_SCHEMA,
    PROVIDER_REQUEST_SCHEMA,
    PROVIDER_RESULT_SCHEMA,
    SUCCESSFUL_LIFECYCLE,
    BackendImportRequest,
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
    recipe_for_shape,
    recipe_digest,
    stable_asset_id,
)


PROVIDER_ROUTES = {"external_site", "procedural_generation", "model_generation"}
DEFAULT_WORKSPACE = Path.home() / "SimulatorWorkspace" / "physics_aware_harness"
SOURCE_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ProviderOrchestration:
    batch: dict[str, Any]
    results: dict[tuple[str, str], dict[str, Any]]
    receipts: tuple[dict[str, Any], ...]


class AssetProviderOrchestrator:
    def __init__(
        self,
        *,
        workspace: str | Path | None = None,
        importer: BackendImporterAdapter | None = None,
        redistribution_evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.workspace = Path(workspace or os.environ.get("SIM_HARNESS_WORKSPACE", DEFAULT_WORKSPACE)).resolve()
        self.importer = importer or UECommandImporterAdapter()
        self.redistribution_evidence = dict(redistribution_evidence or {})

    def fulfill(
        self,
        *,
        case_id: str,
        source_case_spec: Mapping[str, Any],
        compiled_intents: tuple[CompiledAssetIntent, ...] | list[CompiledAssetIntent],
        target_backend: str,
        registry: AssetRegistry,
    ) -> ProviderOrchestration:
        requests: list[dict[str, Any]] = []
        results: dict[tuple[str, str], dict[str, Any]] = {}
        receipts: list[dict[str, Any]] = []
        objects = {
            str(item.get("id")): item
            for item in source_case_spec.get("objects") or []
            if isinstance(item, Mapping) and item.get("id")
        }
        policy = source_case_spec.get("asset_policy") if isinstance(source_case_spec.get("asset_policy"), Mapping) else {}
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
            requests.append(request)
            result, receipt = self._fulfill_one(request, intent=intent, registry=registry)
            results[(intent.object_id, intent.slot)] = result
            if receipt is not None:
                receipts.append(receipt)
        batch = ProviderBatch.from_dict(
            {
                "schema_version": PROVIDER_BATCH_SCHEMA,
                "case_id": case_id,
                "requests": requests,
                "results": [results[key] for key in sorted(results)],
                "receipt_ids": [str(receipt["receipt_id"]) for receipt in receipts],
            }
        ).to_dict()
        return ProviderOrchestration(batch=batch, results=results, receipts=tuple(receipts))

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
            "recipe_id": str(intent.acquisition.get("provider_hint") or recipe_for_shape(shape_hint) or ""),
            "recipe_version": "v1",
            "shape": shape_hint,
            "size_m": copy.deepcopy(geometry.get("approx_size_m")),
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
        digest = stable_digest(identity)
        return ProviderRequest.from_dict(
            {
                "schema_version": PROVIDER_REQUEST_SCHEMA,
                "request_id": f"asset-provider.{digest[:24]}",
                "request_digest": digest,
                **identity,
            }
        ).to_dict()

    def _fulfill_one(
        self,
        request: dict[str, Any],
        *,
        intent: CompiledAssetIntent,
        registry: AssetRegistry,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        route = str(request["route"])
        if route in {"external_site", "model_generation"}:
            return (
                provider_failure(
                    request,
                    status="blocked",
                    code="unsupported_provider_route",
                    message=f"Provider route is not implemented in this phase: {route}",
                ),
                None,
            )
        if not registry.writable:
            return (
                provider_failure(
                    request,
                    status="blocked",
                    code="catalog_not_writable",
                    message="Provider fulfillment requires a writable SQLite Catalog",
                ),
                None,
            )
        invalid_inputs = [
            str(row.get("input_id") or "")
            for row in request.get("reference_inputs") or []
            if not _is_sha256(str(row.get("sha256") or ""))
        ]
        if invalid_inputs:
            return (
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
            return (
                provider_failure(request, status="failed", code=exc.code, message=exc.message),
                None,
            )
        try:
            self.workspace.relative_to(SOURCE_ROOT)
        except ValueError:
            pass
        else:
            return (
                provider_failure(
                    request,
                    status="blocked",
                    code="workspace_inside_source_repository",
                    message="Provider outputs cannot be written inside the source repository",
                ),
                None,
            )
        request_dir = self.workspace / "providers" / PROVIDER_ID / request["request_digest"]
        generated = generate_procedural_obj(normalized, request_dir / f"{normalized['recipe_id']}.obj")
        asset_id = stable_asset_id(normalized)
        lifecycle = SUCCESSFUL_LIFECYCLE[:5]
        import_request_payload = {
            "schema_version": BACKEND_IMPORT_REQUEST_SCHEMA,
            "request_id": f"backend-import.{request['request_digest'][:24]}",
            "asset_id": asset_id,
            "target_backend": request["target_backend"],
            "class_name": "StaticMesh",
            "source_files": [self._file_record(generated["path"], role="generated_source", file_format="obj")],
            "desired_name": asset_id.replace(".", "_"),
            "expected_size_m": list(normalized["size_m"]),
        }
        import_request_payload["request_digest"] = stable_digest(import_request_payload)
        import_request = BackendImportRequest.from_dict(import_request_payload)
        import_result = self.importer.import_asset(import_request, work_dir=request_dir, workspace=self.workspace)
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
                    str(row.get("dependency_id") or row.get("package")) for row in dependencies
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
