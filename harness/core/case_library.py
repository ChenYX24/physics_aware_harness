from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Iterable

from harness.core.artifact_manager import ArtifactManager, file_sha256, is_mp4, safe_filename
from harness.core.artifact_schema import read_json, write_json
from harness.core.runtime_case import validate_runtime_case
from harness.core.workspace import WorkspaceError, workspace_root


REPO_ROOT = Path(__file__).resolve().parents[2]
VARIANT_PLAN_SCHEMA_VERSION = "harness_variant_plan_v1"
VARIANT_MANIFEST_SCHEMA_VERSION = "harness_case_variant_v1"
CASE_INDEX_SCHEMA_VERSION = "harness_case_index_v1"
CATALOG_PLAN_SCHEMA_VERSION = "harness_catalog_case_plan_v1"
RUN_CONTROL_SCHEMA_VERSION = "harness_run_control_v1"
FROZEN_RUN_CONTROL_PLAN_SCHEMA_VERSION = "harness_frozen_run_control_plan_v1"
MODALITY_FILES = {
    "rgb": "rgb.mp4",
    "depth": "depth_preview.mp4",
    "segmentation": "segmentation_preview.mp4",
}


class CaseLibraryError(ValueError):
    pass


def _validate_variant_case(case_spec: dict[str, Any]) -> None:
    """Keep the historical variant editor isolated from the V2 runtime path."""
    from harness.core.case_spec import validate_case_spec

    validate_case_spec(case_spec)


def create_variant_plan(
    base_case: str | Path,
    *,
    case_route: str,
    axes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Record a full Cartesian parameter space and select a small OFAT render set."""
    _validate_case_route(case_route)
    base_path = _resolve_base_case(base_case)
    base = read_json(base_path)
    if not isinstance(base, dict):
        raise CaseLibraryError(f"base case must be a JSON object: {base_path}")
    _validate_variant_case(base)
    normalized_axes = _normalize_axes(axes)
    baseline = {axis["id"]: axis["baseline"] for axis in normalized_axes}
    selected = [{"id": "baseline", "levels": baseline}]
    for axis in normalized_axes:
        for level in axis["levels"]:
            if level["id"] == axis["baseline"]:
                continue
            levels = dict(baseline)
            levels[axis["id"]] = level["id"]
            selected.append({"id": f"{axis['id']}-{level['id']}", "levels": levels})
    combination_count = math.prod(len(axis["levels"]) for axis in normalized_axes)
    plan = {
        "schema_version": VARIANT_PLAN_SCHEMA_VERSION,
        "case_route": case_route,
        "base_case": str(base_case),
        "selection_strategy": "baseline_plus_one_factor_at_a_time",
        "combination_count": combination_count,
        "axes": normalized_axes,
        "selected_variants": selected,
    }
    _validate_variant_plan(plan, source=base_path)
    return plan


def materialize_variant(
    plan_path: str | Path,
    variant_id: str,
    output_path: str | Path,
) -> dict[str, Any]:
    plan_path = Path(plan_path).expanduser().resolve(strict=True)
    plan = _load_variant_plan(plan_path)
    variant = next(
        (row for row in plan["selected_variants"] if row["id"] == variant_id),
        None,
    )
    if variant is None:
        raise CaseLibraryError(f"unknown selected variant: {variant_id}")
    base_path = _plan_base_case(plan, plan_path)
    payload = copy.deepcopy(read_json(base_path))
    edits: dict[str, Any] = {}
    for axis in plan["axes"]:
        level_id = variant["levels"][axis["id"]]
        level = next(level for level in axis["levels"] if level["id"] == level_id)
        for pointer, value in level["edits"].items():
            if pointer in edits and edits[pointer] != value:
                raise CaseLibraryError(
                    f"variant {variant_id} assigns conflicting values to {pointer}"
                )
            edits[pointer] = copy.deepcopy(value)
    for pointer, value in edits.items():
        _set_json_pointer(payload, pointer, value)
    computed_edits = _variant_computed_edits(plan)
    for pointer, expression in computed_edits.items():
        _set_json_pointer(payload, pointer, _evaluate_plan_expression(expression, payload))
    payload["case_id"] = f"{payload['case_id']}__{safe_filename(variant_id)}"
    payload["variant_plan"] = {
        "schema_version": VARIANT_PLAN_SCHEMA_VERSION,
        "plan": _portable_plan_reference(plan_path),
        "plan_sha256": file_sha256(plan_path),
        "variant": variant_id,
        "levels": variant["levels"],
        "computed_pointers": list(computed_edits),
    }
    _validate_variant_case(payload)
    write_json(Path(output_path), payload)
    return payload


def load_variant_plan(plan_path: str | Path) -> dict[str, Any]:
    return _load_variant_plan(Path(plan_path).expanduser().resolve(strict=True))


def write_case_editor(
    plan_path: str | Path,
    output_path: str | Path | None = None,
) -> Path:
    """Write one standalone editor containing its validated plan and base CaseSpec."""
    plan_path = Path(plan_path).expanduser().resolve(strict=True)
    plan = _load_variant_plan(plan_path)
    base_case = read_json(_plan_base_case(plan, plan_path))
    _validate_variant_case(base_case)
    output = (
        Path(output_path).expanduser().resolve(strict=False)
        if output_path
        else plan_path.with_suffix(".html")
    )
    if output == plan_path:
        raise CaseLibraryError("editor output must not overwrite the variant plan")
    return _write_case_editor_document(
        output,
        plan=plan,
        base_case=base_case,
        plan_name=_portable_plan_reference(plan_path),
    )


def write_run_control_page(
    run_dir: str | Path,
    case_spec: dict[str, Any],
    *,
    execution: dict[str, Any],
    reproduce_command: str,
    status: str,
) -> Path:
    """Write the machine-readable and standalone per-run reproduction contract."""
    validate_runtime_case(case_spec)
    if status not in {"prepared", "completed", "failed"}:
        raise CaseLibraryError(f"invalid run control status: {status}")
    if not reproduce_command.strip():
        raise CaseLibraryError("run control requires a reproduction command")
    run_dir = Path(run_dir).expanduser().resolve(strict=False)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "case_spec.json", case_spec)
    plan, plan_name, plan_record = _run_control_plan(case_spec, execution)
    control = {
        "schema_version": RUN_CONTROL_SCHEMA_VERSION,
        "status": status,
        "case_id": case_spec["case_id"],
        "case_sha256": _json_sha256(case_spec),
        "control_mode": "variable" if plan_record["status"] == "resolved" else "frozen",
        "plan": plan_record,
        "execution": copy.deepcopy(execution),
        "reproduce_command": reproduce_command,
        "paths": {
            "case_spec": "case_spec.json",
            "run_control": "run_control.json",
            "html": "run_control.html",
            "reproductions": "reproductions/",
        },
    }
    write_json(run_dir / "run_control.json", control)
    output = _write_case_editor_document(
        run_dir / "run_control.html",
        plan=plan,
        base_case=case_spec,
        plan_name=plan_name,
        run_control=control,
    )
    _declare_run_control_artifacts(run_dir)
    return output


def build_run_control_execution(
    run_dir: str | Path,
    output_root: str | Path,
    *,
    backend: str,
    views: list[str],
    render_passes: list[str],
    mode: str,
    width: int,
    height: int,
    camera_strategy: str,
    profile: str = "custom",
    case_route: str | None = None,
    lighting_preset: str | None = None,
    source_case_filename: str,
) -> tuple[dict[str, Any], str]:
    run_dir = Path(run_dir).expanduser().resolve(strict=False)
    reproduction_root = run_dir / "reproductions"
    execution = {
        "backend": backend,
        "case_route": case_route,
        "output_root": str(Path(output_root).expanduser().resolve(strict=False)),
        "reproduction_output_root": str(reproduction_root),
        "views": list(views),
        "render_passes": list(render_passes),
        "mode": mode,
        "width": int(width),
        "height": int(height),
        "camera_strategy": camera_strategy,
        "profile": profile,
        "python": sys.executable,
        "runner": str(REPO_ROOT / "scripts" / "harness_run_case.py"),
        "lighting_preset": lighting_preset,
    }
    command = [
        sys.executable,
        execution["runner"],
        str(run_dir / source_case_filename),
        "--backend",
        backend,
        "--output-root",
        str(reproduction_root),
        "--video-root",
        str(reproduction_root / "review"),
        "--camera-strategy",
        camera_strategy,
    ]
    if profile != "custom":
        command.extend(("--profile", profile))
    else:
        command.extend(
            (
                "--views",
                ",".join(views),
                "--render-passes",
                ",".join(render_passes),
                "--mode",
                mode,
                "--width",
                str(width),
                "--height",
                str(height),
            )
        )
    if lighting_preset:
        command = ["env", f"SIM_STUDIO_UE_LIGHTING_PRESET={lighting_preset}", *command]
    return execution, shlex.join(command)


def _write_case_editor_document(
    output: Path,
    *,
    plan: dict[str, Any],
    base_case: dict[str, Any],
    plan_name: str,
    run_control: dict[str, Any] | None = None,
) -> Path:
    marker = '<script src="case_parameter_editor.js"></script>'
    template = (REPO_ROOT / "tools" / "case_parameter_editor.html").read_text(
        encoding="utf-8"
    )
    if template.count(marker) != 1:
        raise CaseLibraryError("case parameter editor script marker is missing or duplicated")
    data = json.dumps(
        {
            "plan": plan,
            "base_case": base_case,
            "plan_name": plan_name,
            "run_control": run_control,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")
    script = (REPO_ROOT / "tools" / "case_parameter_editor.js").read_text(encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        template.replace(
            marker,
            f'<script id="case-editor-data" type="application/json">{data}</script>\n'
            f"<script>{script}</script>",
        ),
        encoding="utf-8",
    )
    return output


def _run_control_plan(
    case_spec: dict[str, Any],
    execution: dict[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    variant = case_spec.get("variant_plan")
    reference = str(variant.get("plan") or "") if isinstance(variant, dict) else ""
    candidate = (REPO_ROOT / reference).resolve(strict=False) if reference else None
    if candidate and candidate.is_file() and candidate.is_relative_to(REPO_ROOT):
        actual_hash = file_sha256(candidate)
        expected_hash = str(variant.get("plan_sha256") or "")
        if not expected_hash or expected_hash == actual_hash:
            return (
                _load_variant_plan(candidate),
                _portable_plan_reference(candidate),
                {
                    "status": "resolved",
                    "path": _portable_plan_reference(candidate),
                    "sha256": actual_hash,
                },
            )
        issue = "plan_sha256_mismatch"
    else:
        issue = "plan_missing" if reference else "plan_not_declared"
    width = int(execution["width"])
    height = int(execution["height"])
    frozen = {
        "schema_version": FROZEN_RUN_CONTROL_PLAN_SCHEMA_VERSION,
        "case_route": execution.get("case_route")
        or "reproduction/exact/v001_run_control",
        "base_case": "case_spec.json",
        "axes": [],
        "selected_variants": [],
        "ui": {
            "title": case_spec["case_id"],
            "summary": case_spec["prompt"],
            "fields": [],
            "render": {
                "resolution": {
                    "id": "exact_run",
                    "width": width,
                    "height": height,
                },
                "passes": list(execution["render_passes"]),
                "available_passes": ["rgb", "depth", "segmentation"],
                "views": list(execution["views"]),
            },
        },
    }
    return frozen, "frozen exact CaseSpec", {
        "status": "unavailable",
        "path": reference or None,
        "sha256": None,
        "issue": issue,
    }


def _json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _declare_run_control_artifacts(run_dir: Path) -> None:
    for name, key in (
        ("harness_artifact.json", "paths"),
        ("artifact_manifest.json", "artifacts"),
        ("manifest.json", "intermediates"),
    ):
        path = run_dir / name
        if not path.is_file():
            continue
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        payload.setdefault(key, {}).update(
            {
                "run_control": "run_control.json",
                "run_control_html": "run_control.html",
            }
        )
        write_json(path, payload)


def variant_render_command(
    plan_path: str | Path,
    variant_id: str,
    materialized_case: str | Path,
    *,
    python_executable: str | Path = sys.executable,
    formal: bool = False,
    render_passes: Iterable[str] | None = None,
    views: str = "front_static,side_static,top_down,tracking_subject,event_closeup",
) -> list[str]:
    plan_path = Path(plan_path).expanduser().resolve(strict=True)
    plan = _load_variant_plan(plan_path)
    materialized_case = Path(materialized_case).expanduser().resolve(strict=False)
    materialize_variant(plan_path, variant_id, materialized_case)
    passes = tuple(render_passes or (("rgb", "depth", "segmentation") if formal else ("rgb",)))
    if not passes or len(set(passes)) != len(passes) or any(
        item not in {"rgb", "depth", "segmentation"} for item in passes
    ):
        raise CaseLibraryError(f"render passes must be unique rgb/depth/segmentation values: {passes}")
    if formal:
        if set(passes) != {"rgb", "depth", "segmentation"}:
            raise CaseLibraryError("formal delivery requires rgb, depth, and segmentation")
        return [
            str(python_executable),
            str(REPO_ROOT / "scripts" / "harness_iterate_case.py"),
            str(materialized_case),
            "--case-route",
            plan["case_route"],
            "--condition",
            variant_id,
        ]
    mode = "both" if "rgb" in passes and len(passes) > 1 else ("rgb" if passes == ("rgb",) else "data")
    return [
        str(python_executable),
        str(REPO_ROOT / "scripts" / "harness_run_case.py"),
        str(materialized_case),
        "--backend",
        "ue",
        "--case-route",
        plan["case_route"],
        "--views",
        views,
        "--render-passes",
        ",".join(passes),
        "--mode",
        mode,
    ]


def organize_workspace_cases(
    workspace: str | Path,
    *,
    routes: Iterable[str] | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Build ``cases/<case>/<variant>`` as a hardlinked view over internal runs."""
    raw_root = Path(workspace).expanduser()
    if raw_root.is_symlink():
        raise CaseLibraryError(f"workspace root must not be a symlink: {raw_root}")
    try:
        root = workspace_root(raw_root)
    except WorkspaceError as exc:
        raise CaseLibraryError(str(exc)) from exc
    source_root = root / "runs" / "case_routes"
    if (
        not root.is_dir()
        or root.is_symlink()
        or not (root / "cases").is_dir()
        or not source_root.is_dir()
    ):
        raise CaseLibraryError(
            f"initialized harness workspace with real cases and runs/case_routes directories is required: {root}"
        )
    versions = _case_versions(source_root, routes)
    cases: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    organized_count = 0
    file_count = 0
    overall_generation_count = 0
    qualification_counts: dict[str, int] = {}
    for version in versions:
        route = version.relative_to(source_root).as_posix()
        case_id = flat_case_id(route)
        case_root = root / "cases" / case_id
        variants: list[dict[str, Any]] = []
        labels: set[str] = set()
        indexed_labels = _indexed_run_labels(version)
        for run in _discover_run_dirs(version):
            view_modalities = _run_view_modalities(run)
            if not view_modalities:
                skipped.append({"run": str(run), "reason": "no real RGB camera view"})
                continue
            views = list(view_modalities)
            overall = _run_overall(run, apply=apply)
            expected_overall = set().union(*view_modalities.values())
            if any(modality not in overall for modality in expected_overall):
                overall_generation_count += 1
            label = _unique_variant_label(
                version,
                run,
                labels,
                preferred=_declared_variant_label(run, indexed_labels),
            )
            labels.add(label)
            qualification = _run_qualification(run)
            qualification_counts[qualification["status"]] = (
                qualification_counts.get(qualification["status"], 0) + 1
            )
            target = case_root / label
            artifacts: dict[str, Any] = {"rgb": {}, "depth": {}, "segmentation": {}, "overall": {}}
            if apply:
                for modality in ("rgb", "depth", "segmentation", "overall"):
                    (target / modality).mkdir(parents=True, exist_ok=True)
            for view, available_modalities in view_modalities.items():
                view_dir = run / "views" / view
                rgb_target = target / "rgb" / f"{safe_filename(view)}.mp4"
                file_count += _organize_file(
                    view_dir / "rgb.mp4",
                    rgb_target,
                    apply=apply,
                    source_root=run,
                )
                artifacts["rgb"][view] = rgb_target.relative_to(target).as_posix()
                for modality, preview_name, frames_name in (
                    ("depth", "depth_preview.mp4", "depth_frames"),
                    ("segmentation", "segmentation_preview.mp4", "segmentation_frames"),
                ):
                    if modality not in available_modalities:
                        continue
                    modality_root = target / modality / safe_filename(view)
                    preview_target = modality_root / "preview.mp4"
                    file_count += _organize_file(
                        view_dir / preview_name,
                        preview_target,
                        apply=apply,
                        source_root=run,
                    )
                    frame_targets: list[str] = []
                    for frame in sorted((view_dir / frames_name).glob("*.exr")):
                        frame_target = modality_root / "frames" / frame.name
                        file_count += _organize_file(
                            frame,
                            frame_target,
                            apply=apply,
                            source_root=run,
                        )
                        frame_targets.append(frame_target.relative_to(target).as_posix())
                    artifacts[modality][view] = {
                        "preview": preview_target.relative_to(target).as_posix(),
                        "frames": frame_targets,
                    }
            for modality, source in overall.items():
                overall_target = target / "overall" / f"{modality}.mp4"
                if apply or is_mp4(source):
                    file_count += _organize_file(
                        source,
                        overall_target,
                        apply=apply,
                        source_root=run,
                    )
                else:
                    file_count += 1
                artifacts["overall"][modality] = overall_target.relative_to(target).as_posix()
            manifest = {
                "schema_version": VARIANT_MANIFEST_SCHEMA_VERSION,
                "case_id": case_id,
                "case_route": route,
                "variant": label,
                "source_run": str(run),
                "views": views,
                "modalities": {
                    modality: sorted(
                        view
                        for view, available in view_modalities.items()
                        if modality in available
                    )
                    for modality in ("rgb", "depth", "segmentation")
                },
                "artifacts": artifacts,
                "storage": "hardlink_view_over_canonical_run",
                "qualification": qualification,
            }
            if apply:
                write_json(target / "variant.json", manifest)
            variants.append(manifest)
            organized_count += 1
        case_index = {
            "schema_version": CASE_INDEX_SCHEMA_VERSION,
            "case_id": case_id,
            "case_route": route,
            "variants": [
                {
                    "id": row["variant"],
                    "path": row["variant"],
                    "source_run": row["source_run"],
                    "views": row["views"],
                    "modalities": row["modalities"],
                    "qualification": row["qualification"],
                }
                for row in variants
            ],
        }
        if apply and variants:
            write_json(case_root / "case.json", case_index)
        cases.append(case_index)
    return {
        "schema_version": "harness_case_organization_report_v1",
        "workspace": str(root),
        "dry_run": not apply,
        "case_count": len(versions),
        "organized_variant_count": organized_count,
        "linked_file_count": file_count,
        "overall_generation_count": overall_generation_count,
        "qualification_counts": qualification_counts,
        "skipped_run_count": len(skipped),
        "skipped_runs": skipped,
        "cases": cases,
    }


def flat_case_id(route: str) -> str:
    """Return the stable two-level library folder for a three-part run route."""
    _validate_case_route(route)
    _, scenario, version = Path(route).parts
    return f"{safe_filename(scenario)}__{safe_filename(version)}"


def migrate_workspace_case_layout(
    workspace: str | Path,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Move legacy route trees from ``cases`` into internal ``runs/case_routes``."""
    root = workspace_root(Path(workspace).expanduser())
    cases_root = root / "cases"
    source_root = root / "runs" / "case_routes"
    if not cases_root.is_dir() or cases_root.is_symlink():
        raise CaseLibraryError(f"workspace cases must be a real directory: {cases_root}")
    if not source_root.is_dir():
        if not apply:
            raise CaseLibraryError(
                f"workspace runs/case_routes must exist before migration dry-run: {source_root}"
            )
        source_root.mkdir(parents=True, exist_ok=True)
    legacy_versions = _case_versions(cases_root, routes=None)
    moves: list[dict[str, str]] = []
    for version in legacy_versions:
        route = version.relative_to(cases_root).as_posix()
        target = source_root / route
        if target.exists():
            raise CaseLibraryError(f"internal case route already exists: {target}")
        moves.append(
            {
                "case_route": route,
                "flat_case_id": flat_case_id(route),
                "source": str(version),
                "target": str(target),
            }
        )
    if apply:
        for row in moves:
            source = Path(row["source"])
            target = Path(row["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
        for scenario in sorted(cases_root.glob("*/*"), reverse=True):
            if scenario.is_dir() and not scenario.is_symlink() and not any(scenario.iterdir()):
                scenario.rmdir()
        for physics in sorted(cases_root.iterdir(), reverse=True):
            if physics.is_dir() and not physics.is_symlink() and not any(physics.iterdir()):
                physics.rmdir()
        write_json(
            root / "catalog" / "case_layout_migration.json",
            {
                "schema_version": "harness_case_layout_migration_v1",
                "layout": "cases/<case_id>/<variant_id>",
                "internal_run_root": str(source_root),
                "moved_route_count": len(moves),
                "moves": moves,
            },
        )
    return {
        "schema_version": "harness_case_layout_migration_report_v1",
        "workspace": str(root),
        "dry_run": not apply,
        "moved_route_count": len(moves),
        "moves": moves,
    }


def catalog_case_plan(case_id: str) -> dict[str, Any]:
    """Expose every workbook variable axis as a symbolic, bounded plan."""
    catalog = read_json(REPO_ROOT / "config" / "case_catalog.json")
    rows = catalog.get("cases") if isinstance(catalog, dict) else None
    if not isinstance(rows, list):
        raise CaseLibraryError("case catalog is invalid")
    row = next(
        (item for item in rows if isinstance(item, dict) and item.get("id") == case_id),
        None,
    )
    if row is None:
        raise CaseLibraryError(f"unknown catalog case: {case_id}")
    axes = _catalog_axes(str(row.get("variable_space") or ""))
    combination_count = math.prod(axis["level_count"] for axis in axes)
    if combination_count != row.get("combination_count"):
        raise CaseLibraryError(
            f"catalog combination count drift for {case_id}: "
            f"declared={row.get('combination_count')}, parsed={combination_count}"
        )
    return {
        "schema_version": CATALOG_PLAN_SCHEMA_VERSION,
        "case_id": case_id,
        "name": row.get("name"),
        "combination_count": combination_count,
        "axes": axes,
        "selection_strategy": "baseline_plus_one_factor_at_a_time",
        "selected_variant_count": 1 + sum(axis["level_count"] - 1 for axis in axes),
        "case_specs": list(row.get("case_specs") or []),
        "capability_ids": list(row.get("capability_ids") or []),
        "render_binding": (
            "explicit_json_pointer_edits_required"
            if row.get("case_specs")
            else "blocked_missing_case_spec"
        ),
    }


def _load_variant_plan(path: Path) -> dict[str, Any]:
    plan = read_json(path)
    if not isinstance(plan, dict):
        raise CaseLibraryError(f"variant plan must be a JSON object: {path}")
    _validate_variant_plan(plan, source=path)
    return plan


def _validate_variant_plan(plan: dict[str, Any], *, source: Path) -> None:
    if plan.get("schema_version") != VARIANT_PLAN_SCHEMA_VERSION:
        raise CaseLibraryError("variant plan schema_version must be harness_variant_plan_v1")
    _validate_case_route(str(plan.get("case_route") or ""))
    axes = _normalize_axes(plan.get("axes"))
    expected_count = math.prod(len(axis["levels"]) for axis in axes)
    if plan.get("combination_count") != expected_count:
        raise CaseLibraryError(
            f"variant plan combination_count drift: expected {expected_count}"
        )
    selected = plan.get("selected_variants")
    if not isinstance(selected, list) or not selected:
        raise CaseLibraryError("variant plan selected_variants must be a non-empty list")
    axis_levels = {
        axis["id"]: {level["id"] for level in axis["levels"]}
        for axis in axes
    }
    ids: set[str] = set()
    for row in selected:
        if not isinstance(row, dict):
            raise CaseLibraryError("selected variant must be an object")
        variant_id = str(row.get("id") or "")
        if not variant_id or safe_filename(variant_id) != variant_id or variant_id in ids:
            raise CaseLibraryError(f"selected variant id is invalid or duplicate: {variant_id!r}")
        ids.add(variant_id)
        levels = row.get("levels")
        if not isinstance(levels, dict) or set(levels) != set(axis_levels):
            raise CaseLibraryError(f"selected variant must choose one level from every axis: {variant_id}")
        for axis_id, level_id in levels.items():
            if level_id not in axis_levels[axis_id]:
                raise CaseLibraryError(f"unknown {axis_id} level in {variant_id}: {level_id}")
    _variant_computed_edits(plan)
    _plan_base_case(plan, source)


def _normalize_axes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise CaseLibraryError("variant plan axes must be a non-empty list")
    axes: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw_axis in value:
        if not isinstance(raw_axis, dict):
            raise CaseLibraryError("variant axis must be an object")
        axis_id = str(raw_axis.get("id") or "")
        if not axis_id or safe_filename(axis_id) != axis_id or axis_id in ids:
            raise CaseLibraryError(f"variant axis id is invalid or duplicate: {axis_id!r}")
        ids.add(axis_id)
        raw_levels = raw_axis.get("levels")
        if not isinstance(raw_levels, list) or not raw_levels:
            raise CaseLibraryError(f"variant axis {axis_id} requires levels")
        levels: list[dict[str, Any]] = []
        level_ids: set[str] = set()
        for raw_level in raw_levels:
            if not isinstance(raw_level, dict):
                raise CaseLibraryError(f"variant axis {axis_id} level must be an object")
            level_id = str(raw_level.get("id") or "")
            edits = raw_level.get("edits")
            if (
                not level_id
                or safe_filename(level_id) != level_id
                or level_id in level_ids
                or not isinstance(edits, dict)
                or any(not isinstance(pointer, str) or not pointer.startswith("/") for pointer in edits)
            ):
                raise CaseLibraryError(f"variant axis {axis_id} has an invalid level: {level_id!r}")
            level_ids.add(level_id)
            levels.append({"id": level_id, "edits": copy.deepcopy(edits)})
        baseline = str(raw_axis.get("baseline") or "")
        if baseline not in level_ids:
            raise CaseLibraryError(f"variant axis {axis_id} baseline is not a declared level")
        axes.append({"id": axis_id, "baseline": baseline, "levels": levels})
    return axes


def _plan_base_case(plan: dict[str, Any], source: Path) -> Path:
    raw = plan.get("base_case")
    if not isinstance(raw, str) or not raw.strip():
        raise CaseLibraryError("variant plan base_case must be a path")
    path = Path(raw).expanduser()
    candidates = [path] if path.is_absolute() else [REPO_ROOT / path, source.parent / path]
    resolved = next((candidate.resolve(strict=True) for candidate in candidates if candidate.is_file()), None)
    if resolved is None:
        raise CaseLibraryError(f"variant plan base_case does not exist: {raw}")
    return resolved


def _portable_plan_reference(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def _resolve_base_case(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    return candidate.resolve(strict=True)


def _set_json_pointer(payload: Any, pointer: str, value: Any) -> None:
    parts = _json_pointer_parts(pointer)
    parent = payload
    for part in parts[:-1]:
        if isinstance(parent, list):
            try:
                parent = parent[int(part)]
            except (ValueError, IndexError) as exc:
                raise CaseLibraryError(f"JSON pointer does not resolve: {pointer}") from exc
        elif isinstance(parent, dict) and part in parent:
            parent = parent[part]
        else:
            raise CaseLibraryError(f"JSON pointer does not resolve: {pointer}")
    last = parts[-1]
    if isinstance(parent, list):
        try:
            parent[int(last)] = copy.deepcopy(value)
        except (ValueError, IndexError) as exc:
            raise CaseLibraryError(f"JSON pointer does not resolve: {pointer}") from exc
    elif isinstance(parent, dict) and last in parent:
        parent[last] = copy.deepcopy(value)
    else:
        raise CaseLibraryError(f"JSON pointer does not resolve: {pointer}")


def _get_json_pointer(payload: Any, pointer: str) -> Any:
    value = payload
    for part in _json_pointer_parts(pointer):
        if isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError) as exc:
                raise CaseLibraryError(f"JSON pointer does not resolve: {pointer}") from exc
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise CaseLibraryError(f"JSON pointer does not resolve: {pointer}")
    return value


def _json_pointer_parts(pointer: str) -> list[str]:
    parts = [
        part.replace("~1", "/").replace("~0", "~")
        for part in pointer.removeprefix("/").split("/")
    ]
    if not pointer.startswith("/") or not parts or any(part == "" for part in parts):
        raise CaseLibraryError(f"invalid JSON pointer: {pointer}")
    return parts


def _variant_computed_edits(plan: dict[str, Any]) -> dict[str, Any]:
    ui = plan.get("ui")
    if ui is None:
        return {}
    if not isinstance(ui, dict):
        raise CaseLibraryError("variant plan ui must be an object")
    computed = ui.get("computed_edits", {})
    if not isinstance(computed, dict):
        raise CaseLibraryError("variant plan ui.computed_edits must be an object")
    for pointer, expression in computed.items():
        if not isinstance(pointer, str):
            raise CaseLibraryError("computed edit JSON pointers must be strings")
        _json_pointer_parts(pointer)
        _validate_plan_expression(expression)
    return computed


def _validate_plan_expression(expression: Any) -> None:
    if expression is None or isinstance(expression, (str, int, float, bool)):
        return
    if not isinstance(expression, dict):
        raise CaseLibraryError("computed edit expression must be a literal or object")
    if set(expression) == {"path"} and isinstance(expression["path"], str):
        _json_pointer_parts(expression["path"])
        return
    op = expression.get("op")
    if op in {"add", "sub", "mul", "div", "pow"}:
        args = expression.get("args")
        required = 2 if op in {"sub", "div", "pow"} else 1
        if not isinstance(args, list) or len(args) < required or (
            op in {"sub", "div", "pow"} and len(args) != required
        ):
            raise CaseLibraryError(f"computed edit {op} has invalid args")
        for arg in args:
            _validate_plan_expression(arg)
        return
    if op == "bands":
        _validate_plan_expression(expression.get("value"))
        bands = expression.get("bands")
        if not isinstance(bands, list) or not bands:
            raise CaseLibraryError("computed edit bands requires non-empty bands")
        for band in bands:
            if not isinstance(band, dict) or "result" not in band or set(band) - {"lt", "result"}:
                raise CaseLibraryError("computed edit band must contain result and optional lt")
            if "lt" in band:
                _validate_plan_expression(band["lt"])
        return
    raise CaseLibraryError(f"unsupported computed edit operator: {op!r}")


def _evaluate_plan_expression(expression: Any, payload: dict[str, Any]) -> Any:
    if expression is None or isinstance(expression, (str, int, float, bool)):
        return copy.deepcopy(expression)
    if set(expression) == {"path"}:
        return copy.deepcopy(_get_json_pointer(payload, expression["path"]))
    op = expression["op"]
    if op == "bands":
        value = _numeric_value(
            _evaluate_plan_expression(expression["value"], payload),
            operator=op,
        )
        for band in expression["bands"]:
            if "lt" not in band or value < _numeric_value(
                _evaluate_plan_expression(band["lt"], payload),
                operator=op,
            ):
                return copy.deepcopy(band["result"])
        raise CaseLibraryError("computed edit bands has no matching or default result")
    args = [
        _numeric_value(_evaluate_plan_expression(arg, payload), operator=op)
        for arg in expression["args"]
    ]
    try:
        if op == "add":
            result = sum(args)
        elif op == "mul":
            result = math.prod(args)
        elif op == "sub":
            result = args[0] - args[1]
        elif op == "div":
            result = args[0] / args[1]
        else:
            result = args[0] ** args[1]
    except (OverflowError, ZeroDivisionError, ValueError) as exc:
        raise CaseLibraryError(f"computed edit {op} failed: {exc}") from exc
    return _numeric_value(result, operator=op)


def _numeric_value(value: Any, *, operator: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CaseLibraryError(f"computed edit {operator} requires finite numbers")
    return value


def _case_versions(source_root: Path, routes: Iterable[str] | None) -> list[Path]:
    if routes is not None:
        versions = []
        for route in routes:
            _validate_case_route(route)
            version = source_root / Path(route)
            if (
                not version.is_dir()
                or _contains_symlink(source_root, version)
                or not version.resolve(strict=False).is_relative_to(source_root)
            ):
                raise CaseLibraryError(f"case route does not exist in workspace: {route}")
            versions.append(version.resolve(strict=False))
        return sorted(set(versions))
    return sorted(
        version
        for physics in source_root.iterdir() if physics.is_dir()
        for scenario in physics.iterdir() if scenario.is_dir()
        for version in scenario.iterdir()
        if (
            version.is_dir()
            and version.name.startswith("v")
            and not _contains_symlink(source_root, version)
        )
    ) if source_root.is_dir() else []


def _validate_case_route(route: str) -> None:
    parts = Path(route).parts
    if (
        Path(route).is_absolute()
        or len(parts) != 3
        or not parts[2].startswith("v")
        or any(safe_filename(part) != part for part in parts)
    ):
        raise CaseLibraryError(
            "case route must be physics/scenario/vNNN_description using safe path names"
        )


def _discover_run_dirs(version: Path) -> list[Path]:
    runs = {
        views.parent.resolve()
        for views in version.rglob("views")
        if views.is_dir()
        and not _contains_symlink(version, views)
        and "variants" not in views.relative_to(version).parts
        and any(path.is_dir() and is_mp4(path / "rgb.mp4") for path in views.iterdir())
    }
    return sorted(runs)


def _run_view_modalities(run: Path) -> dict[str, set[str]]:
    views: dict[str, set[str]] = {}
    for view in sorted((run / "views").iterdir()):
        if not view.is_dir():
            continue
        if _contains_symlink(run, view):
            return {}
        if not is_mp4(view / "rgb.mp4"):
            continue
        modalities = {"rgb"}
        for modality, preview_name, frames_name in (
            ("depth", "depth_preview.mp4", "depth_frames"),
            ("segmentation", "segmentation_preview.mp4", "segmentation_frames"),
        ):
            frames = view / frames_name
            if frames.is_symlink() or (view / preview_name).is_symlink():
                return {}
            if (
                is_mp4(view / preview_name)
                and frames.is_dir()
                and not _contains_symlink(run, frames)
                and any(frames.glob("*.exr"))
            ):
                modalities.add(modality)
        views[view.name] = modalities
    return views


def _run_overall(run: Path, *, apply: bool) -> dict[str, Path]:
    if (run / "overall").is_symlink():
        raise CaseLibraryError(f"run overall directory must not be a symlink: {run / 'overall'}")
    overall = ArtifactManager(run).publish_run_overall() if apply else {}
    for modality in MODALITY_FILES:
        path = run / "overall" / f"{modality}.mp4"
        if is_mp4(path):
            overall[modality] = path
    return overall


def _unique_variant_label(
    version: Path,
    run: Path,
    used: set[str],
    *,
    preferred: str | None = None,
) -> str:
    relative = run.relative_to(version)
    markers = ("runs", "matrix", "ue_runs", "representatives", "rerender_current_harness_20260714")
    label = preferred or run.name
    if preferred == "baseline_repeat":
        label = f"baseline_repeat_{1 + sum(item.startswith('baseline_repeat_') for item in used):02d}"
    if preferred is None:
        for marker in markers:
            if marker in relative.parts:
                index = relative.parts.index(marker)
                if index + 1 < len(relative.parts):
                    label = relative.parts[index + 1]
                    break
    label = safe_filename(label)
    if label not in used:
        return label
    digest = hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()[:8]
    return f"{label}-{digest}"


def _indexed_run_labels(version: Path) -> dict[Path, str]:
    index_path = version / "run_index.json"
    if not index_path.is_file():
        return {}
    payload = read_json(index_path)
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, list):
        raise CaseLibraryError(f"case run index is invalid: {index_path}")
    labels: dict[Path, str] = {}
    for session in sessions:
        passing = session.get("passing_runs") if isinstance(session, dict) else None
        if not isinstance(passing, list):
            continue
        for row in passing:
            if not isinstance(row, dict) or not row.get("run_dir"):
                continue
            run = Path(str(row["run_dir"])).expanduser().resolve(strict=False)
            condition = str(row.get("condition") or "").strip()
            if condition:
                label = condition
            else:
                label = "baseline_repeat"
            if label:
                labels[run] = safe_filename(label)
    return labels


def _declared_variant_label(run: Path, indexed: dict[Path, str]) -> str | None:
    case_spec = run / "case_spec.json"
    if case_spec.is_file() and not case_spec.is_symlink():
        payload = read_json(case_spec)
        variant_plan = payload.get("variant_plan") if isinstance(payload, dict) else None
        variant = str((variant_plan or {}).get("variant") or "").strip()
        if variant:
            return safe_filename(variant)
    return indexed.get(run.resolve(strict=False))


def _run_qualification(run: Path) -> dict[str, Any]:
    quality_path = run / "quality_report.json"
    if not quality_path.is_file() or quality_path.is_symlink():
        return {
            "status": "legacy_unverified",
            "hard_gate_passed": None,
            "quality_report": None,
            "quality_report_sha256": None,
        }
    quality = read_json(quality_path)
    hard_gate_passed = quality.get("hard_gate_passed") if isinstance(quality, dict) else None
    return {
        "status": "hard_gate_passed" if hard_gate_passed is True else "hard_gate_failed",
        "hard_gate_passed": hard_gate_passed is True,
        "quality_report": str(quality_path),
        "quality_report_sha256": file_sha256(quality_path),
    }


def _contains_symlink(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _catalog_axes(value: str) -> list[dict[str, Any]]:
    axes: list[dict[str, Any]] = []
    for index, token in enumerate(value.split("*"), start=1):
        match = re.fullmatch(r"(.+?)(\d+)", token.strip())
        if match is None:
            raise CaseLibraryError(f"catalog variable axis is invalid: {token!r}")
        label, count_raw = match.groups()
        count = int(count_raw)
        if count < 1:
            raise CaseLibraryError(f"catalog variable axis has no levels: {token!r}")
        axes.append(
            {
                "id": f"axis_{index:02d}",
                "label": label,
                "level_count": count,
                "levels": [f"level_{level:02d}" for level in range(1, count + 1)],
            }
        )
    if not axes:
        raise CaseLibraryError("catalog variable space must declare at least one axis")
    return axes


def _organize_file(
    source: Path,
    target: Path,
    *,
    apply: bool,
    source_root: Path | None = None,
) -> int:
    if (
        not source.is_file()
        or source.is_symlink()
        or (
            source_root is not None
            and (
                _contains_symlink(source_root, source)
                or not source.resolve(strict=False).is_relative_to(source_root.resolve(strict=False))
            )
        )
    ):
        raise CaseLibraryError(f"variant source must be a real file: {source}")
    if not apply:
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            if source.samefile(target):
                return 1
        except OSError:
            pass
        if target.is_file() and not target.is_symlink() and file_sha256(target) == file_sha256(source):
            return 1
        raise CaseLibraryError(f"refusing to overwrite different organized artifact: {target}")
    try:
        os.link(source, target)
    except OSError as exc:
        raise CaseLibraryError(
            f"hardlink failed; refusing to duplicate runtime data: {source} -> {target}: {exc}"
        ) from exc
    return 1
