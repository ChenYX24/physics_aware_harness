from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from harness.core.artifact_schema import read_json
from harness.core.harness_config import EffectiveHarnessConfig


LAYOUT_PREVIEW_SCHEMA_VERSION = "harness_layout_preview_v1"


def _stable_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_frozen_compilation(compilation_dir: str | Path) -> dict[str, Any]:
    root = Path(compilation_dir)
    required = {
        name: read_json(root / name)
        for name in (
            "case_spec_v2.json",
            "runtime_case.json",
            "backend_selection.json",
            "camera_plan.json",
            "runtime_actor_placement.json",
            "runtime_compilation_report.json",
            "compilation_transaction.json",
        )
    }
    transaction = required["compilation_transaction.json"]
    report = required["runtime_compilation_report.json"]
    backend = required["backend_selection.json"]
    if transaction.get("state") != "completed" or report.get("status") != "pass":
        raise ValueError("layout preview requires a completed, passing frozen compilation")
    if transaction.get("input_identity", {}).get("case_spec_digest") != _stable_digest(
        required["case_spec_v2.json"]
    ):
        raise ValueError("frozen compilation CaseSpec digest mismatch")
    if report.get("compile_config_digest") != transaction.get("input_identity", {}).get(
        "compile_config_digest"
    ):
        raise ValueError("frozen compilation config digest mismatch")
    if "ue" not in {
        str(backend.get("selected_backend") or ""),
        str(backend.get("render_backend") or ""),
    }:
        raise ValueError("layout preview currently requires a UE-rendered compilation")
    return required


def execute_layout_preview(
    *,
    compilation_dir: str | Path,
    output_dir: str | Path,
    config: EffectiveHarnessConfig,
    time_s: float,
) -> dict[str, Any]:
    compiled = validate_frozen_compilation(compilation_dir)
    root = Path(compilation_dir)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    transaction = compiled["compilation_transaction.json"]
    compile_config = transaction.get("compile_config") or {}
    command = list(config.ue_runner_command)
    if not command:
        raise ValueError("UE runner command is not configured")
    values = {
        "--case-spec": root / "runtime_case.json",
        "--run-dir": destination,
        "--camera-plan": root / "camera_plan.json",
        "--actor-placement": root / "runtime_actor_placement.json",
        "--passes": "rgb",
        "--mode": "rgb",
        "--map": str(compile_config.get("map_package") or ""),
        "--actor-class": config.ue_actor_class,
        "--asset-registry": str(config.ue_asset_registry or ""),
        "--ue-project": config.ue_project,
        "--ue-executable": config.ue_executable or "",
        "--layout-preview-time-s": f"{float(time_s):.8g}",
    }
    for option, value in values.items():
        command.extend([option, str(value)])
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    report_path = destination / "layout_preview_manifest.json"
    report = read_json(report_path) if report_path.is_file() else {}
    if completed.returncode != 0 or report.get("status") != "pass":
        raise RuntimeError(
            "layout preview runner failed: "
            + (completed.stderr[-2000:] or completed.stdout[-2000:] or f"exit {completed.returncode}")
        )
    return {
        **report,
        "schema_version": LAYOUT_PREVIEW_SCHEMA_VERSION,
        "compilation_id": transaction.get("transaction_id"),
    }


def layout_preview_request_digest(
    *, compilation_id: str, compile_config_digest: str, time_s: float
) -> str:
    return _stable_digest(
        {
            "schema_version": LAYOUT_PREVIEW_SCHEMA_VERSION,
            "compilation_id": compilation_id,
            "compile_config_digest": compile_config_digest,
            "time_s": float(time_s),
            "width": 640,
            "height": 360,
        }
    )
