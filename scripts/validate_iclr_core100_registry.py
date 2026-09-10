#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


EXPECTED_COLUMNS = {
    "case_id",
    "domain",
    "family",
    "title",
    "input_mode",
    "scene_class",
    "contract_focus",
    "readiness_tier",
    "pilot_order",
    "status",
}


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_frozen_case(experiment_root: Path, case_id: str, input_mode: str) -> list[str]:
    errors: list[str] = []
    case_dir = experiment_root / "cases" / case_id
    receipt_path = experiment_root / "receipts" / "cases" / case_id / "freeze_receipt.json"
    expected = {
        "cases/{case_id}/request.json": "harness_iclr_frozen_request_v1",
        "cases/{case_id}/case_spec.json": "harness_case_spec_v2",
        "cases/{case_id}/asset_lock.json": "harness_iclr_asset_lock_v1",
        "cases/{case_id}/condition.json": "harness_iclr_frozen_condition_v1",
    }
    paths = {template.format(case_id=case_id): experiment_root / template.format(case_id=case_id) for template in expected}
    if not receipt_path.is_file():
        return [f"frozen_receipt_missing:{case_id}"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("schema_version") != "harness_iclr_case_freeze_receipt_v1" or receipt.get("case_id") != case_id:
        errors.append(f"frozen_receipt_invalid:{case_id}")
    receipt_rows = {
        str(item.get("path") or ""): item
        for item in receipt.get("artifacts") or []
        if isinstance(item, dict)
    }
    payloads: dict[str, Any] = {}
    for relative, path in paths.items():
        if not path.is_file():
            errors.append(f"frozen_artifact_missing:{case_id}:{relative}")
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        payloads[relative] = payload
        if payload.get("schema_version") != expected[relative.replace(case_id, "{case_id}")]:
            errors.append(f"frozen_artifact_schema_invalid:{case_id}:{relative}")
        if payload.get("case_id") != case_id and relative != f"cases/{case_id}/case_spec.json":
            errors.append(f"frozen_artifact_identity_invalid:{case_id}:{relative}")
        if relative == f"cases/{case_id}/case_spec.json" and (payload.get("identity") or {}).get("case_id") != case_id:
            errors.append(f"frozen_artifact_identity_invalid:{case_id}:{relative}")
        receipt_row = receipt_rows.get(relative)
        if not receipt_row:
            errors.append(f"frozen_receipt_artifact_missing:{case_id}:{relative}")
        elif receipt_row.get("artifact_digest") != _stable_digest(payload) or receipt_row.get("file_sha256") != _file_sha256(path):
            errors.append(f"frozen_artifact_digest_mismatch:{case_id}:{relative}")
    request = payloads.get(f"cases/{case_id}/request.json")
    case_spec = payloads.get(f"cases/{case_id}/case_spec.json")
    asset_lock = payloads.get(f"cases/{case_id}/asset_lock.json")
    condition = payloads.get(f"cases/{case_id}/condition.json")
    if all(isinstance(item, dict) for item in (request, case_spec, asset_lock, condition)):
        expected_digests = {
            "request_digest": _stable_digest(request),
            "case_spec_digest": _stable_digest(case_spec),
            "asset_lock_digest": _stable_digest(asset_lock),
            "assertions_digest": _stable_digest((case_spec.get("verification_requirements") or {}).get("assertions") or []),
        }
        if any(condition.get(key) != value for key, value in expected_digests.items()):
            errors.append(f"frozen_condition_digest_mismatch:{case_id}")
        images = request.get("input_images")
        if request.get("input_mode") != input_mode or not isinstance(images, list):
            errors.append(f"frozen_request_input_mode_invalid:{case_id}")
            images = []
        if (input_mode == "text" and images) or (input_mode == "text_image" and not images):
            errors.append(f"frozen_request_image_count_invalid:{case_id}")
        image_ids: set[str] = set()
        expected_image_root = (case_dir / "inputs").resolve()
        for image in images:
            if not isinstance(image, dict):
                errors.append(f"frozen_request_image_invalid:{case_id}")
                continue
            input_id = str(image.get("input_id") or "")
            relative = str(image.get("path") or "")
            image_path = (experiment_root / relative).resolve()
            if not input_id or input_id in image_ids or image.get("kind") != "image":
                errors.append(f"frozen_request_image_identity_invalid:{case_id}:{input_id}")
            image_ids.add(input_id)
            try:
                image_path.relative_to(expected_image_root)
            except ValueError:
                errors.append(f"frozen_request_image_path_invalid:{case_id}:{input_id}")
                continue
            if not image_path.is_file():
                errors.append(f"frozen_request_image_missing:{case_id}:{input_id}")
                continue
            digest = _file_sha256(image_path)
            if (
                not str(image.get("mime_type") or "").startswith("image/")
                or image.get("sha256") != digest
                or image.get("byte_size") != image_path.stat().st_size
            ):
                errors.append(f"frozen_request_image_digest_mismatch:{case_id}:{input_id}")
            receipt_row = receipt_rows.get(relative)
            if not receipt_row or receipt_row.get("file_sha256") != digest or receipt_row.get("artifact_digest") != digest:
                errors.append(f"frozen_receipt_image_mismatch:{case_id}:{input_id}")
        if len(receipt_rows) != len(paths) + len(images):
            errors.append(f"frozen_receipt_artifact_count_invalid:{case_id}")
    if receipt.get("historical_jobs_promoted") != 0:
        errors.append(f"frozen_receipt_historical_promotion_invalid:{case_id}")
    return errors


def validate_experiment(root: str | Path) -> dict[str, Any]:
    experiment_root = Path(root).expanduser().resolve()
    manifest = json.loads((experiment_root / "experiment_manifest.json").read_text(encoding="utf-8"))
    cases_path = experiment_root / str(manifest["registries"]["cases"])
    with cases_path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        columns = set(reader.fieldnames or [])
    errors: list[str] = []
    if manifest.get("schema_version") != "harness_iclr_core100_experiment_v1":
        errors.append("experiment_manifest_schema_invalid")
    missing_columns = sorted(EXPECTED_COLUMNS - columns)
    if missing_columns:
        errors.append(f"case_registry_columns_missing:{','.join(missing_columns)}")
    expected_total = int(manifest["protocol"]["evaluated_case_count"])
    if len(rows) != expected_total:
        errors.append(f"case_count_mismatch:expected={expected_total},actual={len(rows)}")
    identities = [row["case_id"] for row in rows]
    duplicates = sorted(case_id for case_id, count in Counter(identities).items() if count > 1)
    if duplicates:
        errors.append(f"duplicate_case_ids:{','.join(duplicates)}")
    domain_counts = Counter(row["domain"] for row in rows)
    input_counts = Counter(row["input_mode"] for row in rows)
    if dict(domain_counts) != manifest["quotas"]["domains"]:
        errors.append(f"domain_quota_mismatch:{dict(domain_counts)}")
    if dict(input_counts) != manifest["quotas"]["input_modes"]:
        errors.append(f"input_mode_quota_mismatch:{dict(input_counts)}")
    pilots = [row for row in rows if row["pilot_order"]]
    expected_pilots = int(manifest["protocol"]["development_pilot_count"])
    pilot_orders = sorted(int(row["pilot_order"]) for row in pilots)
    if len(pilots) != expected_pilots or pilot_orders != list(range(1, expected_pilots + 1)):
        errors.append(f"pilot_order_invalid:{pilot_orders}")
    invalid_tiers = sorted({row["readiness_tier"] for row in rows} - {"A", "B", "C"})
    if invalid_tiers:
        errors.append(f"readiness_tier_invalid:{','.join(invalid_tiers)}")
    frozen_rows = [row for row in rows if row.get("status") == "frozen"]
    for row in frozen_rows:
        errors.extend(_validate_frozen_case(experiment_root, row["case_id"], row["input_mode"]))
    status_relative = manifest.get("registries", {}).get("status")
    if status_relative:
        status = json.loads((experiment_root / str(status_relative)).read_text(encoding="utf-8"))
        if int((status.get("counts") or {}).get("frozen", -1)) != len(frozen_rows):
            errors.append("status_frozen_count_mismatch")
    return {
        "schema_version": "harness_iclr_core100_registry_validation_v1",
        "status": "pass" if not errors else "fail",
        "experiment_root": str(experiment_root),
        "case_registry": str(cases_path),
        "case_count": len(rows),
        "domain_counts": dict(sorted(domain_counts.items())),
        "input_mode_counts": dict(sorted(input_counts.items())),
        "readiness_counts": dict(sorted(Counter(row["readiness_tier"] for row in rows).items())),
        "frozen_case_ids": [row["case_id"] for row in frozen_rows],
        "pilot_case_ids": [row["case_id"] for row in sorted(pilots, key=lambda row: int(row["pilot_order"]))],
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the frozen-shape ICLR Core100 manifest and case roster.")
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    report = validate_experiment(args.root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
