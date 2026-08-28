#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.agent.job_schema import stable_digest
from harness.core.artifact_schema import read_json, write_json
from harness.core.case_spec_v2 import validate_case_spec_v2


FROZEN_OR_READY_STATES = {"assets_ready", "freeze_ready", "frozen"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = -1
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _requested_route(obj: Mapping[str, Any]) -> str:
    asset = obj.get("asset") if isinstance(obj.get("asset"), Mapping) else {}
    acquisition = asset.get("acquisition") if isinstance(asset.get("acquisition"), Mapping) else {}
    return str(acquisition.get("route") or "default")


def _runtime_binding(selected: Mapping[str, Any]) -> tuple[str, bool]:
    bindings = selected.get("backend_bindings") if isinstance(selected.get("backend_bindings"), Mapping) else {}
    for backend in ("unreal", "ue_5_7", "ue"):
        binding = bindings.get(backend)
        if isinstance(binding, Mapping):
            return str(binding.get("object_path") or ""), bool(binding.get("runtime_ready"))
    ue = selected.get("ue") if isinstance(selected.get("ue"), Mapping) else {}
    return str(ue.get("object_path") or selected.get("ue_path") or ""), bool(selected.get("materialized"))


def build_asset_lock(case_spec: Mapping[str, Any], resolution: Mapping[str, Any]) -> dict[str, Any]:
    object_specs = {
        str(obj.get("id") or ""): obj
        for obj in case_spec.get("objects") or []
        if isinstance(obj, Mapping) and str(obj.get("id") or "")
    }
    resolved: dict[str, Mapping[str, Any]] = {}
    for row in resolution.get("assets") or []:
        if not isinstance(row, Mapping):
            continue
        intent = row.get("intent") if isinstance(row.get("intent"), Mapping) else {}
        object_id = str(intent.get("object_id") or "")
        if object_id:
            if object_id in resolved:
                raise ValueError(f"duplicate asset resolution for {object_id}")
            resolved[object_id] = row
    if set(resolved) != set(object_specs):
        raise ValueError(
            "asset resolution object mismatch: "
            f"expected={sorted(object_specs)}, actual={sorted(resolved)}"
        )

    locks: list[dict[str, Any]] = []
    for object_id in sorted(object_specs):
        row = resolved[object_id]
        acquisition = row.get("acquisition") if isinstance(row.get("acquisition"), Mapping) else {}
        requested = acquisition.get("requested") if isinstance(acquisition.get("requested"), Mapping) else {}
        expected_route = _requested_route(object_specs[object_id])
        if str(requested.get("route") or "default") != expected_route:
            raise ValueError(f"asset route mismatch for {object_id}")
        if not bool(acquisition.get("route_honored")):
            raise ValueError(f"asset route was not honored for {object_id}")
        selected = row.get("selected_asset") if isinstance(row.get("selected_asset"), Mapping) else {}
        qualification = selected.get("qualification") if isinstance(selected.get("qualification"), Mapping) else {}
        if not qualification and isinstance(selected.get("quality_gate"), Mapping):
            qualification = selected["quality_gate"]
        if str(qualification.get("status") or "") not in {"pass", "pass_local_preview"}:
            raise ValueError(f"asset qualification did not pass for {object_id}")
        object_path, runtime_ready = _runtime_binding(selected)
        if not runtime_ready or not object_path:
            raise ValueError(f"asset runtime binding is not ready for {object_id}")
        content_sha256 = str(selected.get("sha256") or "")
        locks.append(
            {
                "object_id": object_id,
                "requested_route": expected_route,
                "actual_route": str(acquisition.get("actual_route") or ""),
                "asset_id": str(selected.get("asset_id") or ""),
                "source_kind": str(selected.get("source_kind") or ""),
                "license_tier": str(selected.get("license_tier") or ""),
                "content_sha256": content_sha256,
                "qualification_status": str(qualification.get("status") or ""),
                "ue_object_path": object_path,
            }
        )

    scene_map = resolution.get("scene_map") if isinstance(resolution.get("scene_map"), Mapping) else {}
    selected_map = scene_map.get("selected_asset") if isinstance(scene_map.get("selected_asset"), Mapping) else {}
    map_gate = selected_map.get("quality_gate") if isinstance(selected_map.get("quality_gate"), Mapping) else {}
    map_path, map_ready = _runtime_binding(selected_map)
    if str(map_gate.get("status") or "") not in {"pass", "pass_local_preview"} or not map_ready or not map_path:
        raise ValueError("scene Map is not qualified and runtime ready")
    map_sha256 = str(selected_map.get("sha256") or "")
    return {
        "schema_version": "harness_iclr_asset_lock_v1",
        "case_id": str(case_spec["identity"]["case_id"]),
        "objects": locks,
        "scene_map": {
            "asset_id": str(selected_map.get("asset_id") or ""),
            "source_kind": str(selected_map.get("source_kind") or ""),
            "license_tier": str(selected_map.get("license_tier") or map_gate.get("license_tier") or ""),
            "content_sha256": map_sha256,
            "qualification_status": str(map_gate.get("status") or ""),
            "ue_object_path": map_path,
        },
    }


def freeze_case(
    root: str | Path,
    *,
    case_id: str,
    case_spec_path: str | Path,
    asset_resolution_path: str | Path,
    source_branch: str,
    source_commit: str,
    image_paths: list[str | Path] | None = None,
    source_evidence: list[str] | None = None,
    frozen_at: str | None = None,
) -> dict[str, Any]:
    experiment_root = Path(root).expanduser().resolve()
    manifest = read_json(experiment_root / "experiment_manifest.json")
    cases_path = experiment_root / str(manifest["registries"]["cases"])
    with cases_path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    matches = [row for row in rows if row.get("case_id") == case_id]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one registry row for {case_id}")
    row = matches[0]
    input_mode = str(row.get("input_mode") or "")
    if input_mode not in {"text", "text_image"}:
        raise ValueError(f"unsupported freeze input_mode={input_mode}")
    sources = [Path(value).expanduser().resolve() for value in image_paths or []]
    if input_mode == "text" and sources:
        raise ValueError("text-only cases cannot freeze image inputs")
    if input_mode == "text_image" and not sources:
        raise ValueError("text_image cases require at least one image input")
    if row.get("status") not in {"pilot_selected", *FROZEN_OR_READY_STATES}:
        raise ValueError(f"case is not freezeable from status={row.get('status')}")

    image_inputs: list[dict[str, Any]] = []
    image_copies: list[tuple[Path, Path]] = []
    case_dir = experiment_root / "cases" / case_id
    for index, source in enumerate(sources):
        if not source.is_file():
            raise FileNotFoundError(f"condition image does not exist: {source}")
        mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        if not mime_type.startswith("image/"):
            raise ValueError(f"condition input is not a recognized image: {source}")
        relative = Path("cases") / case_id / "inputs" / f"request_image_{index}{source.suffix.lower()}"
        destination = experiment_root / relative
        image_inputs.append(
            {
                "input_id": f"request_image_{index}",
                "kind": "image",
                "path": relative.as_posix(),
                "mime_type": mime_type,
                "sha256": _sha256_file(source),
                "byte_size": source.stat().st_size,
            }
        )
        image_copies.append((source, destination))

    case_spec = read_json(Path(case_spec_path))
    validate_case_spec_v2(case_spec, available_input_ids=[item["input_id"] for item in image_inputs])
    identity = case_spec.get("identity") if isinstance(case_spec.get("identity"), Mapping) else {}
    if identity.get("case_id") != case_id:
        raise ValueError("CaseSpec identity.case_id does not match the registry")
    request_text = str(identity.get("source_request") or "").strip()
    if not request_text:
        raise ValueError("CaseSpec source request is empty")
    resolution = read_json(Path(asset_resolution_path))
    asset_lock = build_asset_lock(case_spec, resolution)
    request = {
        "schema_version": "harness_iclr_frozen_request_v1",
        "experiment_id": str(manifest["experiment_id"]),
        "case_id": case_id,
        "input_mode": input_mode,
        "text": request_text,
        "input_images": image_inputs,
    }
    condition = {
        "schema_version": "harness_iclr_frozen_condition_v1",
        "experiment_id": str(manifest["experiment_id"]),
        "case_id": case_id,
        "pilot_order": int(row["pilot_order"]) if row.get("pilot_order") else None,
        "roster": {
            "domain": row["domain"],
            "family": row["family"],
            "title": row["title"],
            "scene_class": row["scene_class"],
            "contract_focus": row["contract_focus"],
        },
        "request_digest": stable_digest(request),
        "case_spec_digest": stable_digest(case_spec),
        "assertions_digest": stable_digest((case_spec.get("verification_requirements") or {}).get("assertions") or []),
        "asset_lock_digest": stable_digest(asset_lock),
        "source": {
            "branch": source_branch,
            "commit": source_commit,
            "development_evidence_only": list(source_evidence or []),
        },
    }

    receipt_path = experiment_root / "receipts" / "cases" / case_id / "freeze_receipt.json"
    artifacts = {
        case_dir / "request.json": request,
        case_dir / "case_spec.json": case_spec,
        case_dir / "asset_lock.json": asset_lock,
        case_dir / "condition.json": condition,
    }
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        for path, payload in artifacts.items():
            if not path.is_file() or stable_digest(read_json(path)) != stable_digest(payload):
                raise ValueError(f"existing frozen artifact differs: {path}")
        for source, destination in image_copies:
            if not destination.is_file() or _sha256_file(destination) != _sha256_file(source):
                raise ValueError(f"existing frozen image differs: {destination}")
        timestamp = str(receipt["frozen_at"])
    else:
        existing = [str(path) for path in [*artifacts, *(destination for _, destination in image_copies)] if path.exists()]
        if existing:
            raise ValueError(f"partial frozen artifacts exist without receipt: {existing}")
        for path, payload in artifacts.items():
            write_json(path, payload)
        for source, destination in image_copies:
            _atomic_copy(source, destination)
        timestamp = frozen_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        receipt = {
            "schema_version": "harness_iclr_case_freeze_receipt_v1",
            "experiment_id": str(manifest["experiment_id"]),
            "case_id": case_id,
            "frozen_at": timestamp,
            "source_branch": source_branch,
            "source_commit": source_commit,
            "artifacts": [
                {
                    "path": str(path.relative_to(experiment_root)),
                    "artifact_digest": stable_digest(payload),
                    "file_sha256": _sha256_file(path),
                }
                for path, payload in artifacts.items()
            ] + [
                {
                    "path": image["path"],
                    "artifact_digest": image["sha256"],
                    "file_sha256": image["sha256"],
                }
                for image in image_inputs
            ],
            "candidate_jobs_created": 0,
            "historical_jobs_promoted": 0,
        }
        write_json(receipt_path, receipt)

    row["status"] = "frozen"
    _atomic_write_csv(cases_path, rows, fieldnames)
    status_path = experiment_root / str(manifest["registries"]["status"])
    status = read_json(status_path)
    frozen_count = sum(item.get("status") == "frozen" for item in rows)
    assets_ready_count = sum(item.get("status") in FROZEN_OR_READY_STATES for item in rows)
    status["generated_at"] = timestamp
    status["phase"] = "pilot_frozen_waiting_development_run"
    status["counts"]["assets_ready"] = assets_ready_count
    status["counts"]["frozen"] = frozen_count
    status["current_case_id"] = case_id
    status["current_job_id"] = None
    status["next_action"] = f"run_development_pilot_{case_id}_no_candidate_job_created"
    write_json(status_path, status)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze one prepared text or text-image ICLR Core100 case condition.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--case-spec", required=True)
    parser.add_argument("--asset-resolution", required=True)
    parser.add_argument("--source-branch", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--source-evidence", action="append", default=[])
    parser.add_argument("--frozen-at")
    args = parser.parse_args()
    receipt = freeze_case(
        args.root,
        case_id=args.case_id,
        case_spec_path=args.case_spec,
        asset_resolution_path=args.asset_resolution,
        source_branch=args.source_branch,
        source_commit=args.source_commit,
        image_paths=args.image,
        source_evidence=args.source_evidence,
        frozen_at=args.frozen_at,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
