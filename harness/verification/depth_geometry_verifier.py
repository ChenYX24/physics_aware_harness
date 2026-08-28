from __future__ import annotations

import array
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

from harness.core.artifact_schema import write_json


SCHEMA_VERSION = "harness_depth_geometry_report_v1"
MIN_SUPPORT_PIXELS = 1_000
MAX_MAE_CM = 1.0
MAX_P95_CM = 2.0
MIN_SCALE_SLOPE = 0.995
MAX_SCALE_SLOPE = 1.005
DEPTH_SCALE_CM = 10_000.0


def verify_depth_geometry(
    run_dir: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    write: bool = True,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    case_spec = read_optional_json(run_dir / "case_spec.json") or read_optional_json(run_dir / "inputs" / "case.json")
    reference = case_spec.get("depth_geometry_reference") if isinstance(case_spec.get("depth_geometry_reference"), dict) else {}
    object_id = str(reference.get("object_id") or "")
    bounds = reference_surface_bounds(run_dir, object_id) if object_id else None
    applicable = bool(object_id and bounds is not None)
    if not applicable:
        report = base_report(
            run_dir,
            "not_applicable",
            False,
            "explicit_depth_geometry_reference_missing" if not object_id else "reference_surface_bounds_missing",
        )
        if write:
            write_json(run_dir / "depth_geometry_report.json", report)
        return report

    assert bounds is not None
    failures: list[dict[str, Any]] = []
    views: dict[str, Any] = {}
    run_scale_numerator = 0.0
    run_scale_denominator = 0.0
    camera_views, camera_sources = load_camera_views(run_dir)
    view_dirs = sorted(path for path in (run_dir / "views").iterdir() if path.is_dir()) if (run_dir / "views").is_dir() else []
    if not view_dirs:
        failures.append(failure("F_DEPTH_GEOMETRY_SEQUENCE", "no canonical view directories found"))

    for view_dir in view_dirs:
        camera_id = view_dir.name
        meta = read_optional_json(view_dir / "meta.json")
        view_failures = validate_metadata(camera_id, meta)
        depth_paths = sorted((view_dir / "depth_frames").glob("*.exr"))
        segmentation_paths = sorted((view_dir / "segmentation_frames").glob("*.exr"))
        camera_frames = camera_views.get(camera_id, {})
        if len(depth_paths) != len(segmentation_paths) or not depth_paths:
            view_failures.append(
                failure(
                    "F_DEPTH_GEOMETRY_SEQUENCE",
                    "depth and segmentation sequences must be non-empty and have equal frame counts",
                    camera_id=camera_id,
                    depth_frame_count=len(depth_paths),
                    segmentation_frame_count=len(segmentation_paths),
                )
            )

        frame_reports: list[dict[str, Any]] = []
        view_scale_numerator = 0.0
        view_scale_denominator = 0.0
        intrinsics = meta.get("camera_intrinsics") if isinstance(meta.get("camera_intrinsics"), dict) else {}
        width = positive_int(intrinsics.get("width"))
        height = positive_int(intrinsics.get("height"))
        table_rgb = reference_instance_rgb(meta, object_id)
        for frame_index, (depth_path, segmentation_path) in enumerate(zip(depth_paths, segmentation_paths)):
            frame_failures: list[dict[str, Any]] = []
            metrics: dict[str, Any] = {"support_pixel_count": 0, "mae_cm": None, "p95_cm": None}
            pose = camera_frames.get(frame_index)
            if not width or not height or table_rgb is None:
                frame_failures.append(
                    failure(
                        "F_DEPTH_GEOMETRY_METADATA",
                        "camera intrinsics or reference-surface instance color is missing",
                        camera_id=camera_id,
                        frame=frame_index,
                    )
                )
            elif not isinstance(pose, dict):
                frame_failures.append(
                    failure(
                        "F_DEPTH_GEOMETRY_CAMERA",
                        "per-frame UE runtime camera echo is missing",
                        camera_id=camera_id,
                        frame=frame_index,
                    )
                )
            else:
                try:
                    depth_planes = decode_exr_planes(depth_path, width, height, "gbrapf32le", ffmpeg=ffmpeg)
                    segmentation_planes = decode_exr_planes(segmentation_path, width, height, "gbrpf32le", ffmpeg=ffmpeg)
                    metrics = frame_geometry_metrics(
                        depth_planes[0],
                        segmentation_planes,
                        width,
                        height,
                        intrinsics,
                        pose,
                        bounds,
                        table_rgb,
                    )
                    view_scale_numerator += float(metrics.pop("_scale_numerator"))
                    view_scale_denominator += float(metrics.pop("_scale_denominator"))
                except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    frame_failures.append(
                        failure(
                            "F_DEPTH_GEOMETRY_DECODE",
                            str(exc),
                            camera_id=camera_id,
                            frame=frame_index,
                        )
                    )
                    metrics = {"support_pixel_count": 0, "mae_cm": None, "p95_cm": None}
                if metrics["support_pixel_count"] < MIN_SUPPORT_PIXELS:
                    frame_failures.append(
                        failure(
                            "F_DEPTH_GEOMETRY_SUPPORT",
                            "analytic reference surface has too few visible pixels",
                            camera_id=camera_id,
                            frame=frame_index,
                            actual=metrics["support_pixel_count"],
                            required=MIN_SUPPORT_PIXELS,
                        )
                    )
                if metrics["mae_cm"] is not None and metrics["mae_cm"] > MAX_MAE_CM:
                    frame_failures.append(
                        failure(
                            "F_DEPTH_GEOMETRY_MAE",
                            "reference-plane depth MAE exceeds threshold",
                            camera_id=camera_id,
                            frame=frame_index,
                            actual_cm=metrics["mae_cm"],
                            maximum_cm=MAX_MAE_CM,
                        )
                    )
                if metrics["p95_cm"] is not None and metrics["p95_cm"] > MAX_P95_CM:
                    frame_failures.append(
                        failure(
                            "F_DEPTH_GEOMETRY_P95",
                            "reference-plane depth P95 exceeds threshold",
                            camera_id=camera_id,
                            frame=frame_index,
                            actual_cm=metrics["p95_cm"],
                            maximum_cm=MAX_P95_CM,
                        )
                    )
            frame_reports.append(
                {
                    "frame": frame_index,
                    "status": "pass" if not frame_failures else "fail",
                    **metrics,
                    "failures": frame_failures,
                }
            )
            view_failures.extend(frame_failures)

        aggregate_slope = view_scale_numerator / view_scale_denominator if view_scale_denominator > 0 else None
        if aggregate_slope is not None and not MIN_SCALE_SLOPE <= aggregate_slope <= MAX_SCALE_SLOPE:
            view_failures.append(
                failure(
                    "F_DEPTH_GEOMETRY_SCALE",
                    "aggregate measured-versus-expected depth slope is outside tolerance",
                    camera_id=camera_id,
                    actual=round(aggregate_slope, 9),
                    minimum=MIN_SCALE_SLOPE,
                    maximum=MAX_SCALE_SLOPE,
                )
            )
        run_scale_numerator += view_scale_numerator
        run_scale_denominator += view_scale_denominator
        views[camera_id] = {
            "status": "pass" if not view_failures else "fail",
            "camera_source": camera_sources.get(camera_id),
            "frame_count": len(frame_reports),
            "aggregate_slope": round(aggregate_slope, 9) if aggregate_slope is not None else None,
            "frames": frame_reports,
            "failures": view_failures,
        }
        failures.extend(view_failures)

    report = base_report(run_dir, "pass" if not failures else "fail", True, None)
    report.update(
        {
            "support_surface": {
                "object_id": object_id,
                "plane": "world_z_top",
                "origin_cm": bounds["origin"],
                "extent_cm": bounds["extent"],
                "plane_z_cm": bounds["origin"][2] + bounds["extent"][2],
            },
            "views": views,
            "aggregate_slope": round(run_scale_numerator / run_scale_denominator, 9) if run_scale_denominator > 0 else None,
            "failure_codes": sorted({item["code"] for item in failures}),
            "failures": failures,
        }
    )
    if write:
        write_json(run_dir / "depth_geometry_report.json", report)
    return report


def frame_geometry_metrics(
    depth: list[float],
    segmentation: list[list[float]],
    width: int,
    height: int,
    intrinsics: dict[str, Any],
    pose: dict[str, Any],
    bounds: dict[str, list[float]],
    table_rgb: list[float],
) -> dict[str, Any]:
    pixel_count = width * height
    if len(depth) != pixel_count or len(segmentation) != 3 or any(len(plane) != pixel_count for plane in segmentation):
        raise ValueError("decoded EXR dimensions do not match camera intrinsics")
    location = vector3(pose.get("location_cm"), "camera location")
    target = vector3(pose.get("target_cm"), "camera target")
    forward = normalize([target[index] - location[index] for index in range(3)])
    right_cross = cross([0.0, 0.0, 1.0], forward)
    right = normalize(right_cross) if length(right_cross) > 1e-9 else [0.0, 1.0, 0.0]
    camera_up = normalize(cross(forward, right))
    fx = positive_float(intrinsics.get("fx"))
    fy = positive_float(intrinsics.get("fy"))
    cx = finite_float(intrinsics.get("cx"))
    cy = finite_float(intrinsics.get("cy"))
    if not fx or not fy or cx is None or cy is None:
        raise ValueError("camera intrinsics fx/fy/cx/cy are invalid")

    origin = bounds["origin"]
    extent = bounds["extent"]
    plane_z = origin[2] + extent[2]
    segmentation_target = [table_rgb[1], table_rgb[2], table_rgb[0]]  # gbrpf32le plane order
    tolerance = 1.0 / 255.0 + 1e-6
    errors: list[float] = []
    scale_numerator = 0.0
    scale_denominator = 0.0
    for pixel_index in range(pixel_count):
        if any(abs(segmentation[channel][pixel_index] - segmentation_target[channel]) > tolerance for channel in range(3)):
            continue
        y, x = divmod(pixel_index, width)
        x_normalized = (x + 0.5 - cx) / fx
        y_normalized = (y + 0.5 - cy) / fy
        ray = [
            forward[index] + x_normalized * right[index] - y_normalized * camera_up[index]
            for index in range(3)
        ]
        if abs(ray[2]) <= 1e-9:
            continue
        expected_view_z = (plane_z - location[2]) / ray[2]
        if expected_view_z <= 0:
            continue
        hit_x = location[0] + expected_view_z * ray[0]
        hit_y = location[1] + expected_view_z * ray[1]
        if not (origin[0] - extent[0] <= hit_x <= origin[0] + extent[0]):
            continue
        if not (origin[1] - extent[1] <= hit_y <= origin[1] + extent[1]):
            continue
        measured = float(depth[pixel_index]) * DEPTH_SCALE_CM
        if math.isfinite(measured):
            errors.append(abs(measured - expected_view_z))
            scale_numerator += expected_view_z * measured
            scale_denominator += expected_view_z * expected_view_z

    errors.sort()
    if not errors:
        return {"support_pixel_count": 0, "mae_cm": None, "p95_cm": None, "slope": None, "_scale_numerator": 0.0, "_scale_denominator": 0.0}
    return {
        "support_pixel_count": len(errors),
        "mae_cm": round(sum(errors) / len(errors), 6),
        "p95_cm": round(errors[math.ceil(0.95 * len(errors)) - 1], 6),
        "slope": round(scale_numerator / scale_denominator, 9),
        "_scale_numerator": scale_numerator,
        "_scale_denominator": scale_denominator,
    }


def decode_exr_planes(
    path: Path,
    width: int,
    height: int,
    pixel_format: str,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[list[float]]:
    plane_count = 4 if pixel_format == "gbrapf32le" else 3
    completed = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", pixel_format, "pipe:1"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    expected_bytes = width * height * plane_count * 4
    if completed.returncode != 0 or len(completed.stdout) != expected_bytes:
        message = completed.stderr.decode("utf-8", errors="replace").strip() or "ffmpeg returned invalid raw EXR bytes"
        raise ValueError(message)
    values = array.array("f")
    values.frombytes(completed.stdout)
    if sys.byteorder != "little":
        values.byteswap()
    plane_size = width * height
    return [list(values[index * plane_size : (index + 1) * plane_size]) for index in range(plane_count)]


def validate_metadata(camera_id: str, meta: dict[str, Any]) -> list[dict[str, Any]]:
    expected = {
        "depth_type": "view_z",
        "depth_encoding": "linear_view_z_times_0.0001",
        "depth_stored_value_to_centimeter": DEPTH_SCALE_CM,
        "depth_unit": "centimeter",
    }
    actual = {key: meta.get(key) for key in expected}
    if actual == expected:
        return []
    return [
        failure(
            "F_DEPTH_GEOMETRY_METADATA",
            "depth metadata does not declare the calibrated view-Z encoding",
            camera_id=camera_id,
            expected=expected,
            actual=actual,
        )
    ]


def load_camera_views(run_dir: Path) -> tuple[dict[str, dict[int, dict[str, Any]]], dict[str, str]]:
    views: dict[str, dict[int, dict[str, Any]]] = {}
    sources: dict[str, str] = {}
    candidates = [
        run_dir / "logs" / "native_combined" / "camera_trajectories.json",
        run_dir / "logs" / "native_rgb" / "camera_trajectories.json",
        run_dir / "camera_trajectory.json",
    ]
    for path in candidates:
        payload = read_optional_json(path)
        for view in payload.get("views", []) if isinstance(payload.get("views"), list) else []:
            if not isinstance(view, dict) or not view.get("view_id"):
                continue
            camera_id = str(view["view_id"])
            if camera_id in views:
                continue
            frames = view.get("frames") if isinstance(view.get("frames"), list) else []
            views[camera_id] = {
                int(frame.get("frame")): frame
                for frame in frames
                if isinstance(frame, dict) and isinstance(frame.get("frame"), int)
            }
            sources[camera_id] = str(path.relative_to(run_dir))
    return views, sources


def reference_surface_bounds(run_dir: Path, object_id: str) -> dict[str, list[float]] | None:
    summary = next(
        (
            payload
            for path in (
                run_dir / "logs" / "native_combined" / "summary.json",
                run_dir / "logs" / "native_data" / "summary.json",
            )
            if (payload := read_optional_json(path))
        ),
        {},
    )
    raw = (summary.get("runtime_actor_bounds") or {}).get(object_id)
    if not isinstance(raw, dict):
        return None
    try:
        origin = vector3(raw.get("origin"), "reference origin")
        extent = vector3(raw.get("extent"), "reference extent")
    except ValueError:
        return None
    if any(value <= 0 for value in extent):
        return None
    return {"origin": origin, "extent": extent}


def reference_instance_rgb(meta: dict[str, Any], object_id: str) -> list[float] | None:
    mapping = meta.get("instance_mapping") if isinstance(meta.get("instance_mapping"), list) else []
    for item in mapping:
        if isinstance(item, dict) and item.get("object_id") == object_id:
            try:
                return vector3(item.get("rgb"), "reference RGB")
            except ValueError:
                return None
    return None


def base_report(run_dir: Path, status: str, applicable: bool, reason: str | None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_dir": str(run_dir.resolve()),
        "status": status,
        "applicable": applicable,
        "reason": reason,
        "depth_decode": {"pixel_format": "gbrapf32le", "channel": "G/plane_0", "stored_value_to_centimeter": DEPTH_SCALE_CM},
        "thresholds": {
            "minimum_support_pixels_per_frame": MIN_SUPPORT_PIXELS,
            "maximum_mae_cm": MAX_MAE_CM,
            "maximum_p95_cm": MAX_P95_CM,
            "aggregate_slope_minimum": MIN_SCALE_SLOPE,
            "aggregate_slope_maximum": MAX_SCALE_SLOPE,
        },
        "views": {},
        "failure_codes": [],
        "failures": [],
    }


def failure(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def read_optional_json(path: Path) -> dict[str, Any]:
    try:
        import json

        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def vector3(value: Any, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{name} must contain three finite numbers")
    result = [float(item) for item in value]
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{name} must contain three finite numbers")
    return result


def length(value: list[float]) -> float:
    return math.sqrt(sum(item * item for item in value))


def normalize(value: list[float]) -> list[float]:
    magnitude = length(value)
    if magnitude <= 1e-9:
        raise ValueError("camera direction vector has zero length")
    return [item / magnitude for item in value]


def cross(left: list[float], right: list[float]) -> list[float]:
    return [
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    ]


def finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def positive_float(value: Any) -> float:
    result = finite_float(value)
    return result if result is not None and result > 0 else 0.0


def positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0
