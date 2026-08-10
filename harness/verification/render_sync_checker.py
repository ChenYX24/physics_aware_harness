from __future__ import annotations

import math
import struct
import subprocess
from pathlib import Path
from typing import Any

from harness.core.artifact_schema import read_json, write_json


ARTIFACT_SCHEMA_VERSION = "2.3"
RENDER_SYNC_SCHEMA_VERSION = "render_sync_report.v2.3"
EXR_MAGIC = b"\x76\x2f\x31\x01"


def depth_pixel_statistics(path: Path) -> dict[str, float] | None:
    try:
        completed = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=64:64:flags=neighbor", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "grayf32le", "pipe:1"],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout
    if completed.returncode != 0 or not isinstance(output, bytes) or len(output) != 64 * 64 * 4:
        return None
    values = [value[0] for value in struct.iter_unpack("<f", output)]
    if any(not math.isfinite(value) for value in values):
        return None
    mean = sum(values) / len(values)
    return {
        "minimum": min(values),
        "maximum": max(values),
        "variance": sum((value - mean) ** 2 for value in values) / len(values),
    }


def check_render_sync(
    run_dir: str | Path,
    *,
    camera_plan: dict[str, Any] | None = None,
    require_depth: bool = True,
    require_segmentation: bool = True,
    write: bool = True,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    plan = camera_plan or read_optional_json(run_dir / "camera_plan.json")
    expected_camera_ids = camera_ids_from_plan(plan)
    planned_views = {str(view.get("camera_id")): view for view in plan.get("views", []) if isinstance(view, dict) and view.get("camera_id")}
    camera_trajectory = read_optional_json(run_dir / "camera_trajectory.json")
    camera_views = {str(view.get("view_id")): view for view in camera_trajectory.get("views", []) if isinstance(view, dict) and view.get("view_id")}
    native_camera_echo = camera_trajectory.get("schema_version") == "camera_trajectories_v1"
    scene_origin_cm = list(read_optional_json(run_dir / "map_report.json").get("scene_origin_cm") or [0.0, 0.0, 0.0])
    failures: list[dict[str, Any]] = []
    per_view: dict[str, Any] = {}

    if not expected_camera_ids:
        failures.append(
            {
                "code": "F_VIEW_MISMATCH",
                "camera_id": "",
                "message": "camera_plan.json is missing or has no views",
            }
        )

    for camera_id in expected_camera_ids:
        view_report = validate_view(
            run_dir,
            camera_id,
            require_depth=require_depth,
            require_segmentation=require_segmentation,
            planned_view=planned_views.get(camera_id),
            camera_echo=camera_views.get(camera_id) if native_camera_echo else None,
            scene_origin_cm=scene_origin_cm,
        )
        per_view[camera_id] = view_report
        failures.extend(view_report["failures"])

    fracture_sensor_state_path = run_dir / "fracture_sensor_state_report.json"
    fracture_sensor_state = read_optional_json(fracture_sensor_state_path)
    fracture_sensor_state_ready: bool | None = None
    if fracture_sensor_state_path.exists():
        fracture_sensor_state_ready = fracture_sensor_state.get("status") == "pass"
        if fracture_sensor_state_ready and fracture_sensor_state.get("comparison_required"):
            rgb_hashes = fracture_sensor_state.get("rgb_fragment_state_hashes") or {}
            data_hashes = fracture_sensor_state.get("data_fragment_state_hashes") or {}
            if not rgb_hashes or not data_hashes:
                fracture_sensor_state_ready = False
                fracture_sensor_state["failure_codes"] = ["F_FRACTURE_FRAGMENT_STATE_MISSING"]
            elif rgb_hashes != data_hashes:
                fracture_sensor_state_ready = False
                fracture_sensor_state["failure_codes"] = ["F_FRACTURE_SENSOR_STATE_MISMATCH"]
        if not fracture_sensor_state_ready:
            fracture_failure_codes = fracture_sensor_state.get("failure_codes") or ["F_FRACTURE_SENSOR_STATE_MISMATCH"]
            for code in sorted(set(str(item) for item in fracture_failure_codes)):
                failures.append(
                    {
                        "code": code,
                        "camera_id": "",
                        "message": "RGB and data passes do not contain matching fracture events and fragment state",
                        "report": fracture_sensor_state_path.name,
                        "rgb_event_keys": fracture_sensor_state.get("rgb_event_keys") or [],
                        "data_event_keys": fracture_sensor_state.get("data_event_keys") or [],
                        "rgb_fragment_state_hashes": fracture_sensor_state.get("rgb_fragment_state_hashes") or {},
                        "data_fragment_state_hashes": fracture_sensor_state.get("data_fragment_state_hashes") or {},
                    }
                )

    failure_codes = [str(item["code"]) for item in failures]
    status = "pass" if not failures else "fail"
    all_depth_from_ue = bool(expected_camera_ids) and all(
        str(view.get("depth_source")) == "ue" for view in per_view.values()
    )
    all_rgb_from_ue = bool(expected_camera_ids) and all(
        view.get("native_ue_rgb") is True or (require_depth and str(view.get("depth_source")) == "ue")
        for view in per_view.values()
    )
    camera_state_ready = bool(expected_camera_ids) and native_camera_echo and all(view.get("camera_state_ready") for view in per_view.values())
    report = {
        "schema_version": RENDER_SYNC_SCHEMA_VERSION,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "status": status,
        "ue_render_real": status == "pass" and all_rgb_from_ue,
        "depth_source": "ue" if all_depth_from_ue else "missing",
        "multi_view_sync_ok": status == "pass",
        "render_pass_valid": status == "pass",
        "render_observability_fail": 0 if status == "pass" else 1,
        "camera_state_ready": camera_state_ready,
        "fracture_sensor_state_ready": fracture_sensor_state_ready,
        "failure_codes": sorted(set(failure_codes)),
        "failures": failures,
        "expected_camera_ids": expected_camera_ids,
        "view_count": len(expected_camera_ids),
        "views": per_view,
        "per_camera_statistics": build_per_camera_statistics(per_view),
        "avg_render_time": average_render_time(per_view),
    }
    if write:
        write_json(run_dir / "render_sync_report.json", report)
    return report


def validate_view(
    run_dir: Path,
    camera_id: str,
    *,
    require_depth: bool,
    require_segmentation: bool,
    planned_view: dict[str, Any] | None = None,
    camera_echo: dict[str, Any] | None = None,
    scene_origin_cm: list[Any] | None = None,
) -> dict[str, Any]:
    view_dir = run_dir / "views" / camera_id
    rgb_path = view_dir / "rgb.mp4"
    depth_path = view_dir / "depth.exr"
    segmentation_path = view_dir / "segmentation.exr"
    legacy_segmentation_path = view_dir / "segmentation.png"
    meta_path = view_dir / "meta.json"
    failures: list[dict[str, Any]] = []

    if not view_dir.exists():
        failures.append({"code": "F_VIEW_MISMATCH", "camera_id": camera_id, "message": "view directory missing"})
    if not file_nonempty(rgb_path):
        failures.append({"code": "F_VIEW_MISMATCH", "camera_id": camera_id, "message": "rgb.mp4 missing or empty"})
    elif not has_mp4_magic(rgb_path):
        failures.append({"code": "F_RGB_MAGIC_INVALID", "camera_id": camera_id, "message": "rgb.mp4 is not an MP4 ftyp stream"})
    if require_depth:
        if not file_nonempty(depth_path):
            failures.append({"code": "F_DEPTH_MISSING", "camera_id": camera_id, "message": "depth.exr missing or empty"})
        elif not has_openexr_magic(depth_path):
            failures.append({"code": "F_DEPTH_MAGIC_INVALID", "camera_id": camera_id, "message": "depth.exr does not contain OpenEXR magic"})
    legacy_segmentation_format = detect_image_format(legacy_segmentation_path)
    if require_segmentation:
        if not file_nonempty(segmentation_path):
            if file_nonempty(legacy_segmentation_path) and legacy_segmentation_format == "openexr":
                failures.append(
                    {
                        "code": "F_SEGMENTATION_EXTENSION_MISMATCH",
                        "camera_id": camera_id,
                        "message": "legacy segmentation.png contains OpenEXR data; canonical name is segmentation.exr",
                    }
                )
            elif file_nonempty(legacy_segmentation_path):
                failures.append(
                    {
                        "code": "F_SEGMENTATION_EXR_MISSING",
                        "camera_id": camera_id,
                        "message": f"segmentation.exr is missing; legacy segmentation.png is {legacy_segmentation_format}",
                    }
                )
            else:
                failures.append({"code": "F_SEGMENTATION_MISSING", "camera_id": camera_id, "message": "segmentation.exr missing or empty"})
        elif not has_openexr_magic(segmentation_path):
            failures.append({"code": "F_SEGMENTATION_MAGIC_INVALID", "camera_id": camera_id, "message": "segmentation.exr does not contain OpenEXR magic"})

    meta = read_optional_json(meta_path)
    if not meta:
        failures.append({"code": "F_VIEW_MISMATCH", "camera_id": camera_id, "message": "meta.json missing or invalid"})
    source_native_view_id = str(meta.get("source_native_view_id") or "")
    if source_native_view_id != camera_id:
        failures.append(
            {
                "code": "F_VIEW_MISMATCH",
                "camera_id": camera_id,
                "source_native_view_id": source_native_view_id,
                "message": "source_native_view_id is missing or does not match planned camera_id",
            }
        )
    sequence_evidence = sequence_evidence_for_view(run_dir, camera_id, meta)
    frame_count_rgb = int(sequence_evidence["rgb"]["frame_count"] or 0)
    frame_count_depth = int(sequence_evidence["depth"]["frame_count"] or 0)
    frame_count_segmentation = int(sequence_evidence["segmentation"]["frame_count"] or 0)
    timestamps_rgb = list(sequence_evidence["rgb"].get("timestamps") or [])
    timestamps_depth = list(sequence_evidence["depth"].get("timestamps") or [])
    timestamps_segmentation = list(sequence_evidence["segmentation"].get("timestamps") or [])
    depth_source = str(meta.get("depth_source") or "missing")
    native_ue_rgb = meta.get("native_ue_rgb") is True
    depth_variance = float(meta.get("depth_variance") or 0.0)
    segmentation_instance_level = bool(meta.get("instance_level") or meta.get("segmentation_type") == "instance")
    instance_mapping = meta.get("instance_mapping") if isinstance(meta.get("instance_mapping"), list) else []
    segmentation_palette_closure = meta.get("segmentation_palette_closure") is True
    render_time = float(meta.get("render_time_sec") or 0.0)

    if require_segmentation and segmentation_instance_level and instance_mapping and not segmentation_palette_closure:
        failures.append(
            {
                "code": "F_SEGMENTATION_PALETTE_CONTRACT_MISSING",
                "camera_id": camera_id,
                "message": "instance segmentation declares an instance mapping but not palette closure",
            }
        )

    if frame_count_rgb <= 0:
        failures.append({"code": "F_RENDER_SYNC_FAIL", "camera_id": camera_id, "message": "rgb frame count is missing or zero"})
    if require_depth and frame_count_depth <= 0:
        failures.append({"code": "F_RENDER_SYNC_FAIL", "camera_id": camera_id, "message": "depth frame count is missing or zero"})
    if require_depth and frame_count_rgb != frame_count_depth:
        failures.append(
            {
                "code": "F_RENDER_SYNC_FAIL",
                "camera_id": camera_id,
                "message": "rgb/depth frame count mismatch",
                "frame_count_rgb": frame_count_rgb,
                "frame_count_depth": frame_count_depth,
            }
        )
    if require_segmentation and frame_count_segmentation <= 0:
        failures.append({"code": "F_RENDER_SYNC_FAIL", "camera_id": camera_id, "message": "segmentation frame-count evidence is missing or zero"})
    if require_segmentation and frame_count_rgb != frame_count_segmentation:
        failures.append(
            {
                "code": "F_RENDER_SYNC_FAIL",
                "camera_id": camera_id,
                "message": "rgb/segmentation frame count mismatch",
                "frame_count_rgb": frame_count_rgb,
                "frame_count_segmentation": frame_count_segmentation,
            }
        )
    if require_depth and depth_source != "ue":
        failures.append({"code": "F_DEPTH_MISSING", "camera_id": camera_id, "message": "depth_source is not ue"})
    if require_depth and depth_variance <= 0:
        failures.append({"code": "F_DEPTH_MISSING", "camera_id": camera_id, "message": "depth variance is zero or missing"})
    if require_depth and not timestamps_aligned(timestamps_rgb, timestamps_depth):
        failures.append({"code": "F_RENDER_SYNC_FAIL", "camera_id": camera_id, "message": "rgb/depth timestamps are missing or not aligned"})
    if require_segmentation and timestamps_segmentation and not timestamps_aligned(timestamps_rgb, timestamps_segmentation):
        failures.append({"code": "F_RENDER_SYNC_FAIL", "camera_id": camera_id, "message": "rgb/segmentation timestamps are not aligned"})
    if require_segmentation and not segmentation_instance_level:
        failures.append({"code": "F_VIEW_MISMATCH", "camera_id": camera_id, "message": "segmentation is not instance-level"})
    camera_failures, camera_state_ready = validate_camera_echo(
        camera_id,
        planned_view,
        camera_echo,
        scene_origin_cm or [0.0, 0.0, 0.0],
        frame_count_rgb,
    )
    failures.extend(camera_failures)

    return {
        "camera_id": camera_id,
        "view_dir": relative_or_str(view_dir, run_dir),
        "rgb_path": relative_or_str(rgb_path, run_dir),
        "depth_path": relative_or_str(depth_path, run_dir),
        "segmentation_path": relative_or_str(segmentation_path, run_dir),
        "segmentation_format": detect_image_format(segmentation_path),
        "legacy_segmentation_path": relative_or_str(legacy_segmentation_path, run_dir) if legacy_segmentation_path.exists() else "",
        "legacy_segmentation_format": legacy_segmentation_format,
        "segmentation_extension_mismatch": legacy_segmentation_format == "openexr" and not segmentation_path.exists(),
        "meta_path": relative_or_str(meta_path, run_dir),
        "source_native_view_id": source_native_view_id,
        "frame_count_rgb": frame_count_rgb,
        "frame_count_depth": frame_count_depth,
        "frame_count_segmentation": frame_count_segmentation,
        "timestamp_count_rgb": len(timestamps_rgb),
        "timestamp_count_depth": len(timestamps_depth),
        "timestamp_count_segmentation": len(timestamps_segmentation),
        "sequence_evidence": sequence_evidence,
        "depth_source": depth_source,
        "native_ue_rgb": native_ue_rgb,
        "depth_variance": depth_variance,
        "segmentation_instance_level": segmentation_instance_level,
        "segmentation_palette_closure": segmentation_palette_closure,
        "render_time_sec": render_time,
        "camera_state_ready": camera_state_ready,
        "status": "pass" if not failures else "fail",
        "failures": failures,
    }


def sequence_evidence_for_view(run_dir: Path, camera_id: str, meta: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Read stable sequence counts; frame files may be pruned after native capture."""
    result = {
        modality: {"frame_count": 0, "source": None, "timestamps": []}
        for modality in ("rgb", "depth", "segmentation")
    }

    def record(modality: str, count: Any, source: str, timestamps: Any = None) -> None:
        try:
            parsed = int(count or 0)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0 and not result[modality]["frame_count"]:
            result[modality] = {
                "frame_count": parsed,
                "source": source,
                "timestamps": list(timestamps) if isinstance(timestamps, list) else [],
            }

    manifest_paths = (
        run_dir / "logs" / "native_combined" / "render_pass_manifest.json",
        run_dir / "logs" / "native_data" / "render_pass_manifest.json",
        run_dir / "ue_native_output" / "render_pass_manifest.json",
        run_dir / "render_manifest.json",
        run_dir / "render_pass_manifest.json",
    )
    for path in manifest_paths:
        manifest = read_optional_json(path)
        passes = manifest.get("passes") if isinstance(manifest.get("passes"), dict) else {}
        for modality in result:
            views = (passes.get(modality) or {}).get("views") if isinstance(passes.get(modality), dict) else []
            row = next(
                (
                    item
                    for item in views or []
                    if isinstance(item, dict) and str(item.get("view_id") or item.get("camera_id") or "") == camera_id
                ),
                None,
            )
            if row:
                record(modality, row.get("frame_count") or len(row.get("frames") or []), str(path.relative_to(run_dir)))
        canonical_views = manifest.get("views") if isinstance(manifest.get("views"), dict) else {}
        canonical = canonical_views.get(camera_id) if isinstance(canonical_views.get(camera_id), dict) else {}
        if canonical:
            record("rgb", canonical.get("frame_count_rgb") or canonical.get("frame_count"), str(path.relative_to(run_dir)))
            record("depth", canonical.get("frame_count_depth"), str(path.relative_to(run_dir)))
            record("segmentation", canonical.get("frame_count_segmentation"), str(path.relative_to(run_dir)))

    sensor = read_optional_json(run_dir / "sensor_state.json")
    sensor_view = next(
        (item for item in sensor.get("views") or [] if isinstance(item, dict) and str(item.get("camera_id") or "") == camera_id),
        {},
    )
    for modality in result:
        record(modality, sensor_view.get(f"frame_count_{modality}"), "sensor_state.json")

    meta = meta if isinstance(meta, dict) else read_optional_json(run_dir / "views" / camera_id / "meta.json")
    record("rgb", meta.get("frame_count_rgb") or meta.get("frame_count"), f"views/{camera_id}/meta.json", meta.get("timestamps_rgb"))
    record(
        "depth",
        meta.get("frame_count_depth") or len(meta.get("depth_frames") or []),
        f"views/{camera_id}/meta.json",
        meta.get("timestamps_depth"),
    )
    record(
        "segmentation",
        meta.get("frame_count_segmentation") or len(meta.get("segmentation_frames") or []),
        f"views/{camera_id}/meta.json",
        meta.get("timestamps_segmentation"),
    )

    # Preserve timestamps even when an earlier manifest supplied the authoritative count.
    for modality in result:
        timestamps = meta.get(f"timestamps_{modality}")
        if not result[modality]["timestamps"] and isinstance(timestamps, list):
            result[modality]["timestamps"] = list(timestamps)
    return result


def validate_camera_echo(
    camera_id: str,
    planned_view: dict[str, Any] | None,
    camera_echo: dict[str, Any] | None,
    scene_origin_cm: list[Any],
    frame_count_rgb: int,
) -> tuple[list[dict[str, Any]], bool]:
    if camera_echo is None:
        return [], False
    frames = camera_echo.get("frames") if isinstance(camera_echo.get("frames"), list) else []
    failures: list[dict[str, Any]] = []
    if len(frames) != frame_count_rgb:
        failures.append({"code": "F_CAMERA_STATE_MISMATCH", "camera_id": camera_id, "message": "camera/rgb frame count mismatch", "camera_frames": len(frames), "rgb_frames": frame_count_rgb})
    if planned_view and frames and str(camera_echo.get("camera_mode") or "fixed") == "fixed":
        origin = padded_vec3(scene_origin_cm)
        expected_location = add_vec(scale_vec(planned_view.get("location"), 100.0), origin)
        expected_target = add_vec(scale_vec(planned_view.get("target"), 100.0), origin)
        expected_fov = float(planned_view.get("fov") or 0.0)
        for frame in frames:
            if not vec_close(frame.get("location_cm"), expected_location, 0.2) or not vec_close(frame.get("target_cm"), expected_target, 0.2) or abs(float(frame.get("fov") or 0.0) - expected_fov) > 1e-4:
                failures.append({"code": "F_CAMERA_STATE_MISMATCH", "camera_id": camera_id, "message": "runtime camera pose or FOV differs from camera plan", "planned": {"location_cm": expected_location, "target_cm": expected_target, "fov": expected_fov}, "actual": frame})
                break
    return failures, bool(frames) and not failures


def padded_vec3(value: Any) -> list[float]:
    values = list(value) if isinstance(value, (list, tuple)) else []
    values.extend([0.0, 0.0, 0.0])
    return [float(values[0]), float(values[1]), float(values[2])]


def scale_vec(value: Any, scale: float) -> list[float]:
    return [entry * scale for entry in padded_vec3(value)]


def add_vec(left: list[float], right: list[float]) -> list[float]:
    return [left[index] + right[index] for index in range(3)]


def vec_close(left: Any, right: Any, tolerance: float) -> bool:
    lhs = padded_vec3(left)
    rhs = padded_vec3(right)
    return all(abs(lhs[index] - rhs[index]) <= tolerance for index in range(3))


def camera_ids_from_plan(plan: dict[str, Any]) -> list[str]:
    views = plan.get("views") if isinstance(plan, dict) else []
    result: list[str] = []
    if isinstance(views, list):
        for view in views:
            if isinstance(view, dict) and view.get("camera_id"):
                result.append(str(view["camera_id"]))
    return result


def file_nonempty(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def has_mp4_magic(path: Path) -> bool:
    return read_header(path, 8)[4:8] == b"ftyp"


def has_openexr_magic(path: Path) -> bool:
    return read_header(path, len(EXR_MAGIC)) == EXR_MAGIC


def detect_image_format(path: Path) -> str:
    if not file_nonempty(path):
        return "missing"
    header = read_header(path, 8)
    if header.startswith(EXR_MAGIC):
        return "openexr"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return "unknown"


def read_header(path: Path, size: int) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = read_json(path)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def timestamps_aligned(left: list[Any], right: list[Any], *, tolerance: float = 1e-6) -> bool:
    if not left or not right or len(left) != len(right):
        return False
    for lhs, rhs in zip(left, right):
        if abs(float(lhs) - float(rhs)) > tolerance:
            return False
    return True


def build_per_camera_statistics(per_view: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for camera_id, view in per_view.items():
        result[camera_id] = {
            "status": view.get("status"),
            "frame_count_rgb": view.get("frame_count_rgb", 0),
            "frame_count_depth": view.get("frame_count_depth", 0),
            "frame_count_segmentation": view.get("frame_count_segmentation", 0),
            "depth_variance": view.get("depth_variance", 0.0),
            "render_time_sec": view.get("render_time_sec", 0.0),
            "failure_codes": sorted({str(item["code"]) for item in view.get("failures", [])}),
        }
    return result


def average_render_time(per_view: dict[str, Any]) -> float:
    values = [float(view.get("render_time_sec") or 0.0) for view in per_view.values()]
    values = [value for value in values if value > 0]
    return round(sum(values) / len(values), 6) if values else 0.0


def relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
