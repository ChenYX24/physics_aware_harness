from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.core.artifact_manager import link_or_copy
from harness.core.artifact_schema import read_json, write_json
from harness.runtime.camera_planner import CameraPlan, camera_plan_from_dict, camera_plan_to_dict
from harness.verification.render_sync_checker import ARTIFACT_SCHEMA_VERSION, check_render_sync, detect_image_format, has_mp4_magic, has_openexr_magic


RENDER_PASS_SCHEMA_VERSION = "render_pass_manifest.v2.3"
DEFAULT_FRAME_COUNT = 1
DEFAULT_FPS = 30
STRICT_UE_PASSES = ["rgb", "depth", "segmentation"]


def write_render_contract_artifacts(
    run_dir: str | Path,
    *,
    backend: str,
    case_id: str,
    camera_plan: CameraPlan | dict[str, Any],
    render_passes: list[str],
    allow_placeholders: bool,
    source: str,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(camera_plan, dict):
        camera_plan = camera_plan_from_dict(camera_plan)
    camera_plan_data = camera_plan_to_dict(camera_plan)
    write_json(run_dir / "camera_plan.json", camera_plan_data)
    normalized_passes = normalize_passes(render_passes)

    for view in camera_plan.views:
        view_dir = run_dir / "views" / view.camera_id
        view_dir.mkdir(parents=True, exist_ok=True)
        rgb_path = view_dir / "rgb.mp4"
        if not rgb_path.exists() and allow_placeholders and "rgb" in normalized_passes:
            rgb_path.write_bytes(b"FALLBACK_DEBUG_RGB")
        if allow_placeholders:
            write_debug_placeholder_files(view_dir, view.camera_id, normalized_passes, source)

    overview = run_dir / "views" / (camera_plan.views[0].camera_id if camera_plan.views else "overview") / "rgb.mp4"
    if overview.exists() and not (run_dir / "video.mp4").exists():
        link_or_copy(overview, run_dir / "video.mp4")

    manifest = build_render_pass_manifest(
        run_dir,
        backend=backend,
        case_id=case_id,
        camera_plan=camera_plan_data,
        render_passes=normalized_passes,
        source=source,
        allow_placeholders=allow_placeholders,
    )
    write_json(run_dir / "render_pass_manifest.json", manifest)
    return manifest


def write_debug_placeholder_files(view_dir: Path, camera_id: str, render_passes: list[str], source: str) -> None:
    if "depth" in render_passes:
        placeholder = view_dir / "depth_placeholder.json"
        if not placeholder.exists():
            write_json(
                placeholder,
                {
                    "schema_version": "depth_placeholder.v1",
                    "camera_id": camera_id,
                    "frame_index": 1,
                    "placeholder": True,
                    "source": source,
                    "note": "debug-only fallback placeholder; not a UE depth buffer",
                },
            )
    if "segmentation" in render_passes or "object_mask" in render_passes:
        placeholder = view_dir / "segmentation_placeholder.json"
        if not placeholder.exists():
            write_json(
                placeholder,
                {
                    "schema_version": "segmentation_placeholder.v1",
                    "camera_id": camera_id,
                    "frame_index": 1,
                    "placeholder": True,
                    "source": source,
                    "note": "debug-only fallback placeholder; not instance-level UE segmentation",
                },
            )


def build_render_pass_manifest(
    run_dir: Path,
    *,
    backend: str,
    case_id: str,
    camera_plan: dict[str, Any],
    render_passes: list[str],
    source: str,
    allow_placeholders: bool,
) -> dict[str, Any]:
    required_passes = set(normalize_passes(render_passes))
    views: dict[str, Any] = {}
    frame_counts: set[int] = set()
    fps_values: set[int] = set()
    all_depth_from_ue = True
    all_valid = True
    for view in camera_plan.get("views", []):
        camera_id = str(view["camera_id"])
        view_dir = run_dir / "views" / camera_id
        meta = read_optional_json(view_dir / "meta.json")
        rgb = view_dir / "rgb.mp4"
        depth = view_dir / "depth.exr"
        segmentation = view_dir / "segmentation.exr"
        legacy_segmentation = view_dir / "segmentation.png"
        frame_count_rgb = int(meta.get("frame_count_rgb") or meta.get("frame_count") or (DEFAULT_FRAME_COUNT if rgb.exists() else 0))
        frame_count_depth = int(meta.get("frame_count_depth") or 0)
        frame_count_segmentation = int(meta.get("frame_count_segmentation") or len(meta.get("segmentation_frames") or []))
        fps = int(meta.get("fps") or DEFAULT_FPS)
        depth_source = str(meta.get("depth_source") or "missing")
        rgb_ready = "rgb" not in required_passes or (has_mp4_magic(rgb) and frame_count_rgb > 0)
        depth_ready = "depth" not in required_passes or (
            has_openexr_magic(depth)
            and depth_source == "ue"
            and float(meta.get("depth_variance") or 0.0) > 0
            and frame_count_depth == frame_count_rgb
        )
        segmentation_ready = "segmentation" not in required_passes or (
            has_openexr_magic(segmentation)
            and frame_count_segmentation == frame_count_rgb
            and bool(meta.get("instance_level") or meta.get("segmentation_type") == "instance")
        )
        render_pass_valid = bool(meta and rgb_ready and depth_ready and segmentation_ready)
        frame_counts.add(frame_count_rgb)
        fps_values.add(fps)
        all_depth_from_ue = all_depth_from_ue and (
            "depth" not in required_passes or depth_source == "ue"
        )
        all_valid = all_valid and render_pass_valid
        views[camera_id] = {
            "camera_id": camera_id,
            "rgb_video": relative_or_empty(rgb, run_dir),
            "rgb_path": relative_or_empty(rgb, run_dir),
            "depth_path": relative_or_empty(depth, run_dir),
            "segmentation_path": relative_or_empty(segmentation, run_dir),
            "segmentation_format": detect_image_format(segmentation),
            "legacy_segmentation_path": relative_or_empty(legacy_segmentation, run_dir),
            "segmentation_extension_mismatch": detect_image_format(legacy_segmentation) == "openexr" and not segmentation.exists(),
            "meta_path": relative_or_empty(view_dir / "meta.json", run_dir),
            "frame_count": frame_count_rgb,
            "frame_count_rgb": frame_count_rgb,
            "frame_count_depth": frame_count_depth,
            "frame_count_segmentation": frame_count_segmentation,
            "fps": fps,
            "duration_sec": round(frame_count_rgb / fps, 4) if fps else 0.0,
            "timebase": "shared_sim_time",
            "depth_source": depth_source,
            "depth_variance": float(meta.get("depth_variance") or 0.0),
            "segmentation_instance_level": bool(meta.get("instance_level") or meta.get("segmentation_type") == "instance"),
            "ready": render_pass_valid,
            "placeholder": allow_placeholders,
            "source": source,
        }
    expected_frame_count = max(frame_counts) if frame_counts else 0
    frame_count_consistent = len(frame_counts) <= 1
    fps_consistent = len(fps_values) <= 1
    has_views = bool(views)
    render_pass_valid = has_views and all_valid
    multi_view_sync_ok = render_pass_valid and frame_count_consistent and fps_consistent
    return {
        "schema_version": RENDER_PASS_SCHEMA_VERSION,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "sync_group_id": case_id,
        "backend": backend,
        "camera_plan": camera_plan,
        "views": views,
        "passes": render_passes,
        "ue_render_real": backend == "ue" and render_pass_valid and all_depth_from_ue,
        "depth_source": (
            "not_requested"
            if "depth" not in required_passes
            else "ue" if all_depth_from_ue and has_views else "missing"
        ),
        "multi_view_sync_ok": multi_view_sync_ok,
        "render_pass_valid": render_pass_valid,
        "render_observability_fail": 0 if render_pass_valid else 1,
        "sync": {
            "mode": "shared_sim_time",
            "expected_frame_count": expected_frame_count,
            "frame_count_consistent": frame_count_consistent,
            "fps_consistent": fps_consistent,
        },
        "warnings": list(camera_plan.get("warnings", [])),
        "placeholder": allow_placeholders,
        "source": source,
    }


def verify_render_observability(
    run_dir: str | Path,
    *,
    require_multiview: bool = False,
    require_depth: bool = False,
    min_view_count: int = 1,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    manifest_path = run_dir / "render_pass_manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    camera_plan = read_json(run_dir / "camera_plan.json") if (run_dir / "camera_plan.json").exists() else {}
    sync_report = check_render_sync(run_dir, camera_plan=camera_plan, require_depth=require_depth, require_segmentation=False, write=False)
    failures = filter_loose_observability_failures(sync_report.get("failures", []), require_depth=require_depth)
    warnings: list[str] = []

    if manifest.get("schema_version") != RENDER_PASS_SCHEMA_VERSION:
        failures.append({"code": "F_RENDER_PASS_MANIFEST_INVALID", "message": "render_pass_manifest.json schema invalid"})
    manifest_views = manifest.get("views") if isinstance(manifest.get("views"), dict) else {}
    view_count = int(sync_report.get("view_count") or len(manifest_views))
    if require_multiview and view_count < min_view_count:
        failures.append({"code": "F_VIEW_MISMATCH", "message": f"expected at least {min_view_count} views, got {view_count}"})
    if manifest and not manifest.get("multi_view_sync_ok", False):
        warnings.append("render_pass_manifest reports multi_view_sync_ok=false")

    failure_codes = {str(item.get("code")) for item in failures}
    rgb_missing = False
    for view in manifest_views.values():
        rgb_path = run_dir / str(view.get("rgb_path") or view.get("rgb_video") or "")
        if not rgb_path.exists() or rgb_path.stat().st_size == 0:
            rgb_missing = True
            break
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "camera_plan_ready": bool(camera_plan),
        "multi_view_ready": view_count >= min_view_count and not rgb_missing,
        "depth_ready": (not require_depth) or "F_DEPTH_MISSING" not in failure_codes,
        "render_pass_ready": "F_RENDER_PASS_MANIFEST_INVALID" not in failure_codes,
        "sync_ready": "F_RENDER_SYNC_FAIL" not in failure_codes,
        "ue_render_real": bool(sync_report.get("ue_render_real")),
        "depth_source": sync_report.get("depth_source", "missing"),
        "multi_view_sync_ok": bool(sync_report.get("multi_view_sync_ok")),
        "render_pass_valid": bool(sync_report.get("render_pass_valid")),
        "render_observability_fail": 1 if failures else 0,
        "view_count": view_count,
        "camera_ids": sorted(sync_report.get("expected_camera_ids") or (manifest.get("views") or {}).keys()),
        "failures": failures,
        "warnings": warnings,
    }


def normalize_passes(render_passes: list[str] | None) -> list[str]:
    result: list[str] = []
    for item in render_passes or ["rgb"]:
        key = str(item).strip().lower()
        if key and key not in result:
            result.append(key)
    return result or ["rgb"]


def filter_loose_observability_failures(failures: list[dict[str, Any]], *, require_depth: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for failure in failures:
        code = str(failure.get("code") or "")
        message = str(failure.get("message") or "")
        if code == "F_DEPTH_MISSING" and require_depth:
            result.append(failure)
        elif code == "F_VIEW_MISMATCH" and ("view directory missing" in message or "rgb.mp4 missing" in message):
            result.append(failure)
        elif code == "F_RENDER_SYNC_FAIL" and require_depth and "frame count mismatch" in message:
            result.append(failure)
    return result


def enforce_ue_render_passes(render_passes: list[str] | None) -> list[str]:
    result = normalize_passes(render_passes or STRICT_UE_PASSES)
    for required in STRICT_UE_PASSES:
        if required not in result:
            result.append(required)
    return result


def read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = read_json(path)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def relative_or_empty(path: Path, root: Path) -> str:
    return str(path.relative_to(root)) if path.exists() else ""
