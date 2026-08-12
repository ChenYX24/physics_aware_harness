from __future__ import annotations

import json
import hashlib
import math
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from harness.core.artifact_schema import write_json
from harness.core.stage_result import stage_result_from_quality_report, write_stage_result
from harness.runtime.backend_policy import backend_plan
from harness.verification.depth_geometry_verifier import verify_depth_geometry
from harness.verification.render_sync_checker import depth_pixel_statistics, sequence_evidence_for_view


SCHEMA_VERSION = "harness_run_quality_v1"
EXR_MAGIC = b"\x76\x2f\x31\x01"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def evaluate_run(
    run_dir: str | Path,
    *,
    ffprobe: str = "ffprobe",
    write: bool = True,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise NotADirectoryError(f"run directory does not exist: {run_dir}")

    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    report_summaries, raw_reports = summarize_source_reports(run_dir, failures, warnings)

    view_ids = discover_view_ids(run_dir, raw_reports.get("render_sync"))
    media = validate_media(run_dir, view_ids, ffprobe, failures, warnings)
    sensor_sequences = validate_sensor_sequences(run_dir, view_ids, media, failures)
    trajectory, trajectory_frames = validate_trajectory(run_dir, failures)
    solver_execution = validate_solver_execution(run_dir, trajectory_frames, failures)
    refresh_ue_readiness_summary(
        report_summaries.get("run_readiness"),
        raw_reports.get("run_readiness"),
        solver_execution,
    )
    validate_source_gates(report_summaries, failures, warnings)
    contacts = validate_contacts(run_dir, trajectory_frames, failures)
    camera_motion = validate_camera_motion(run_dir, failures)
    depth_geometry = verify_depth_geometry(run_dir, write=write)
    if depth_geometry.get("status") == "fail":
        failures.append(
            issue(
                "F_DEPTH_GEOMETRY_FAILED",
                "analytic support-surface depth calibration did not pass",
                failure_codes=depth_geometry.get("failure_codes") or [],
                report_path="depth_geometry_report.json",
            )
        )

    hard_gate_passed = not failures
    ranking = build_ranking_score(
        hard_gate_passed=hard_gate_passed,
        reports=report_summaries,
        media=media,
        trajectory=trajectory,
        contacts=contacts,
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "run_dir": str(run_dir.resolve()),
        "status": "pass" if hard_gate_passed else "fail",
        "hard_gate_passed": hard_gate_passed,
        "hard_gate": {
            "status": "pass" if hard_gate_passed else "fail",
            "passed": hard_gate_passed,
            "failure_count": len(failures),
            "failures": failures,
        },
        "warnings": warnings,
        "source_reports": report_summaries,
        "media": media,
        "sensor_sequences": sensor_sequences,
        "trajectory": trajectory,
        "solver_execution": solver_execution,
        "contacts": contacts,
        "camera_motion": camera_motion,
        "depth_geometry": depth_geometry,
        "ranking": ranking,
    }
    if write:
        write_json(run_dir / "quality_report.json", report)
        write_stage_result(run_dir, stage_result_from_quality_report(report))
        synchronize_run_readiness(run_dir, report)
    return report


def synchronize_run_readiness(run_dir: Path, quality_report: dict[str, Any]) -> None:
    readiness_path = run_dir / "run_readiness.json"
    readiness = read_optional_json(readiness_path)
    if not isinstance(readiness, dict) or not readiness:
        return
    provenance = quality_report.get("solver_execution") if isinstance(quality_report.get("solver_execution"), dict) else {}
    provenance_ready = provenance.get("status") in {"pass", "not_required"}
    readiness["physics_provenance"] = provenance
    readiness["physics_ready"] = provenance_ready
    refresh_ue_readiness_summary(readiness, readiness, provenance)
    quality_ready = bool(quality_report.get("hard_gate_passed"))
    if not provenance_ready or not quality_ready:
        readiness.update(
            {
                "execution_ready": False,
                "reference_ready": False,
                "local_preview_ready": False,
                "publication_tier": "rejected",
            }
        )
    if not provenance_ready:
        readiness["physics_ready"] = False
    render_sync = ((quality_report.get("source_reports") or {}).get("render_sync") or {})
    if render_sync.get("status") == "fail":
        readiness["sensor_ready"] = False
    readiness["quality_gate_passed"] = quality_ready
    write_json(readiness_path, readiness)
    output_path = run_dir / "ue_output" / "run_readiness.json"
    if output_path.parent.is_dir():
        write_json(output_path, readiness)


def refresh_ue_readiness_summary(summary: Any, readiness: Any, provenance: Any) -> None:
    if not isinstance(summary, dict) or not isinstance(readiness, dict) or readiness.get("backend") != "ue":
        return
    provenance_ready = isinstance(provenance, dict) and provenance.get("status") in {"pass", "not_required"}
    detailed_execution_keys = (
        "map_ready",
        "camera_plan_ready",
        "multi_view_ready",
        "render_pass_ready",
        "sync_ready",
        "depth_ready",
        "camera_state_ready",
        "sensor_state_ready",
    )
    if not all(key in readiness for key in detailed_execution_keys):
        return
    execution_ready = provenance_ready and readiness.get("verifier_status") == "pass" and all(
        readiness.get(key) is True for key in detailed_execution_keys
    )
    assets_reference_ready = readiness.get("assets_reference_ready") is True
    local_asset_ready = int(readiness.get("local_preview_asset_count") or 0) > 0 or readiness.get("asset_catalog_reference_ready") is True
    reference_ready = execution_ready and assets_reference_ready
    local_preview_ready = execution_ready and not assets_reference_ready and local_asset_ready
    publication_tier = "reference" if reference_ready else "local_preview" if local_preview_ready else "rejected"
    readiness.update(
        {
            "execution_ready": execution_ready,
            "physics_ready": provenance_ready,
            "reference_ready": reference_ready,
            "local_preview_ready": local_preview_ready,
            "publication_tier": publication_tier,
        }
    )
    summary.update(
        {
            "reference_ready": reference_ready,
            "local_preview_ready": local_preview_ready,
            "publication_tier": publication_tier,
        }
    )


def summarize_source_reports(
    run_dir: Path,
    failures: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = {
        "run_readiness": [run_dir / "run_readiness.json", run_dir / "ue_output" / "run_readiness.json"],
        "verifier": [run_dir / "harness_verifier.json", run_dir / "verifier_report.json", run_dir / "verifier.json"],
        "render_sync": [run_dir / "render_sync_report.json", run_dir / "sync" / "sync_report.json"],
        "map_report": [
            run_dir / "map_report.json",
            run_dir / "ue_output" / "map_report.json",
            run_dir / "logs" / "native_combined" / "map_report.json",
            run_dir / "logs" / "native_data" / "map_report.json",
            run_dir / "logs" / "native_rgb" / "map_report.json",
        ],
        "asset_resolution": [run_dir / "asset_resolution.json"],
        "sensor_state": [run_dir / "sensor_state.json"],
    }
    summaries: dict[str, Any] = {}
    raw: dict[str, Any] = {}
    for name, paths in candidates.items():
        path = next((item for item in paths if item.is_file()), None)
        if path is None:
            summaries[name] = {"present": False, "path": None}
            raw[name] = None
            if name in {"map_report", "asset_resolution"}:
                warnings.append(issue("W_SOURCE_REPORT_MISSING", f"{name} is missing", name=name))
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            summaries[name] = {"present": True, "path": relative(path, run_dir), "read_error": str(exc)}
            raw[name] = None
            failures.append(issue("F_SOURCE_REPORT_INVALID", f"{name} is unreadable or invalid JSON", path=relative(path, run_dir)))
            continue
        raw[name] = value
        summaries[name] = summarize_report(name, value, relative(path, run_dir))
    return summaries, raw


def summarize_report(name: str, value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"present": True, "path": path, "invalid_type": type(value).__name__}
    if name == "run_readiness":
        return {
            "present": True,
            "path": path,
            "backend": value.get("backend"),
            "reference_ready": value.get("reference_ready"),
            "local_preview_ready": value.get("local_preview_ready"),
            "publication_tier": value.get("publication_tier"),
            "verifier_status": value.get("verifier_status"),
            "view_count": value.get("view_count"),
            "ue_render_real": value.get("ue_render_real"),
            "failures": len(value.get("failures") or []),
            "warnings": len(value.get("warnings") or []),
        }
    if name == "verifier":
        verifier = value.get("harness_verifier") if isinstance(value.get("harness_verifier"), dict) else value
        status = verifier.get("status")
        if status is None and "reference_ready" in value:
            status = "pass" if value.get("reference_ready") else "fail"
        return {
            "present": True,
            "path": path,
            "status": status,
            "failure_type": verifier.get("failure_type"),
            "first_failure": verifier.get("first_failure"),
        }
    if name == "render_sync":
        return {
            "present": True,
            "path": path,
            "status": value.get("status"),
            "view_count": value.get("view_count") or len(value.get("views") or {}),
            "multi_view_sync_ok": value.get("multi_view_sync_ok"),
            "render_pass_valid": value.get("render_pass_valid"),
            "failure_count": len(value.get("failures") or []),
        }
    if name == "map_report":
        selected = value.get("selected_map") if isinstance(value.get("selected_map"), dict) else {}
        opened = selected.get("map_opened")
        if opened is None:
            opened = value.get("opened") if value.get("opened") is not None else value.get("map_opened")
        requested = value.get("requested_package")
        opened_package = value.get("opened_package") or selected.get("path") or selected.get("name") or value.get("map_path")
        return {
            "present": True,
            "path": path,
            "status": value.get("status"),
            "selected_map": opened_package,
            "requested_map": requested,
            "package_match": not requested or canonical_ue_package(requested) == canonical_ue_package(opened_package),
            "map_opened": opened,
            "fallback_map": selected.get("fallback_map") or value.get("fallback_map"),
            "warnings": len(value.get("warnings") or []),
        }
    if name == "sensor_state":
        return {
            "present": True,
            "path": path,
            "frame_count": int(value.get("frame_count") or 0),
            "view_count": len(value.get("views") or []),
            "depth_source": (value.get("depth") or {}).get("source"),
            "instance_segmentation": bool((value.get("segmentation") or {}).get("instance_level")),
        }
    assets = value.get("assets") if isinstance(value.get("assets"), list) else []
    selected = [item.get("selected_asset") for item in assets if isinstance(item, dict) and isinstance(item.get("selected_asset"), dict)]
    fallback_count = sum(bool(item.get("fallback_reason")) for item in assets if isinstance(item, dict))
    quality = value.get("quality_gate") if isinstance(value.get("quality_gate"), dict) else {}
    return {
        "present": True,
        "path": path,
        "asset_count": len(assets),
        "selected_count": len(selected),
        "fallback_count": fallback_count,
        "unresolved_count": max(0, len(assets) - len(selected) - fallback_count),
        "proxy_count": sum(bool(item.get("proxy")) for item in selected),
        "reference_assets_ready": quality.get("reference_assets_ready"),
        "geometry_match": quality.get("geometry_match"),
        "local_preview_count": int(quality.get("local_preview_count") or 0),
    }


def validate_source_gates(summaries: dict[str, Any], failures: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> None:
    for name in ("run_readiness", "verifier", "render_sync", "map_report", "asset_resolution", "sensor_state"):
        summary = summaries[name]
        if not summary.get("present"):
            failures.append(issue("F_SOURCE_REPORT_MISSING", f"required {name} report is missing", name=name))
        elif summary.get("invalid_type"):
            failures.append(issue("F_SOURCE_REPORT_INVALID", f"{name} must be a JSON object", name=name))

    readiness = summaries["run_readiness"]
    if readiness.get("present") and readiness.get("reference_ready") is not True:
        if readiness.get("local_preview_ready") is True:
            warnings.append(issue("W_LOCAL_PREVIEW_ONLY", "run passed execution gates but asset provenance blocks reference publication"))
        else:
            failures.append(issue("F_RUN_NOT_READY", "run_readiness declares neither reference_ready nor local_preview_ready"))
    verifier = summaries["verifier"]
    if verifier.get("present") and verifier.get("status") != "pass":
        failures.append(issue("F_VERIFIER_FAILED", "physics verifier did not pass", status=verifier.get("status")))
    render = summaries["render_sync"]
    if render.get("present") and render.get("status") != "pass":
        failures.append(issue("F_RENDER_SYNC_FAILED", "render synchronization report did not pass", status=render.get("status")))
    if render.get("present") and render.get("multi_view_sync_ok") is False:
        failures.append(issue("F_RENDER_SYNC_FAILED", "multi-view synchronization is false"))
    if render.get("present") and render.get("render_pass_valid") is False:
        failures.append(issue("F_RENDER_PASS_INVALID", "render pass contract is false"))
    map_report = summaries["map_report"]
    if map_report.get("present") and (map_report.get("status") != "pass" or map_report.get("map_opened") is False or map_report.get("package_match") is False):
        failures.append(issue("F_MAP_GATE_FAILED", "map report did not prove the requested package was opened", selected_map=map_report.get("selected_map"), requested_map=map_report.get("requested_map")))
    assets = summaries["asset_resolution"]
    if assets.get("present") and int(assets.get("unresolved_count") or 0) > 0:
        failures.append(issue("F_ASSET_UNRESOLVED", "asset resolution contains unresolved objects", count=assets.get("unresolved_count")))
    if assets.get("present") and assets.get("geometry_match") is False:
        failures.append(issue("F_ASSET_GEOMETRY_MISMATCH", "selected render asset does not match the solver container geometry"))
    sensor = summaries["sensor_state"]
    if sensor.get("present") and (int(sensor.get("frame_count") or 0) <= 0 or int(sensor.get("view_count") or 0) <= 0 or not sensor.get("instance_segmentation")):
        failures.append(issue("F_SENSOR_STATE_INVALID", "sensor_state lacks frames, views, or instance segmentation"))


def discover_view_ids(run_dir: Path, render_sync: Any) -> list[str]:
    result: set[str] = set()
    if isinstance(render_sync, dict):
        views = render_sync.get("views")
        if isinstance(views, dict):
            result.update(str(item) for item in views)
        expected = render_sync.get("expected_camera_ids")
        if isinstance(expected, list):
            result.update(str(item) for item in expected)
    views_dir = run_dir / "views"
    if views_dir.is_dir():
        result.update(item.name for item in views_dir.iterdir() if item.is_dir())
    return sorted(result)


def validate_media(
    run_dir: Path,
    view_ids: list[str],
    ffprobe: str,
    failures: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    if not view_ids:
        failures.append(issue("F_VIEW_MISSING", "no canonical views were found"))
    views: dict[str, Any] = {}
    videos: list[dict[str, Any]] = []
    for camera_id in view_ids:
        view_dir = run_dir / "views" / camera_id
        video = validate_video(view_dir / "rgb.mp4", run_dir, ffprobe, failures, warnings)
        depth = validate_exr(view_dir / "depth.exr", run_dir, "depth", failures)
        depth_pixel_check = validate_depth_pixels(view_dir, run_dir, failures)
        depth["pixel_check"] = depth_pixel_check
        if depth_pixel_check["status"] == "fail":
            depth["status"] = "fail"
        segmentation = validate_segmentation(view_dir, run_dir, failures)
        pixel_check = validate_segmentation_pixels(view_dir, run_dir, failures)
        segmentation["pixel_check"] = pixel_check
        if pixel_check["status"] == "fail":
            segmentation["status"] = "fail"
        videos.append(video)
        views[camera_id] = {
            "status": "pass" if video["status"] == depth["status"] == segmentation["status"] == "pass" else "fail",
            "rgb": video,
            "depth": depth,
            "segmentation": segmentation,
        }
    return {
        "view_count": len(view_ids),
        "valid_view_count": sum(item["status"] == "pass" for item in views.values()),
        "views": views,
        "videos": videos,
    }


def validate_video(
    path: Path,
    run_dir: Path,
    ffprobe: str,
    failures: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    rel = relative(path, run_dir)
    result: dict[str, Any] = {"path": rel, "status": "fail", "magic_valid": False}
    if not path.is_file():
        failures.append(issue("F_VIDEO_MISSING", "canonical RGB video is missing", path=rel))
        return result
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError as exc:
        failures.append(issue("F_VIDEO_UNREADABLE", str(exc), path=rel))
        return result
    result["size_bytes"] = path.stat().st_size
    result["magic_valid"] = len(header) >= 8 and header[4:8] == b"ftyp"
    if not result["magic_valid"]:
        failures.append(issue("F_VIDEO_MAGIC_INVALID", "MP4 ftyp magic is missing", path=rel))
        return result

    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_frames",
                "-show_entries",
                "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames,nb_read_frames,duration,bit_rate:format=duration,size,bit_rate,format_name",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        failures.append(issue("F_FFPROBE_TIMEOUT", "ffprobe exceeded 60 seconds", path=rel))
        return result
    except OSError as exc:
        failures.append(issue("F_FFPROBE_UNAVAILABLE", str(exc), path=rel))
        return result
    if completed.returncode != 0:
        failures.append(issue("F_FFPROBE_FAILED", completed.stderr.strip() or "ffprobe failed", path=rel, returncode=completed.returncode))
        return result
    try:
        probe = json.loads(completed.stdout)
    except json.JSONDecodeError:
        failures.append(issue("F_FFPROBE_INVALID_JSON", "ffprobe did not return JSON", path=rel))
        return result
    streams = probe.get("streams") if isinstance(probe, dict) else None
    stream = streams[0] if isinstance(streams, list) and streams and isinstance(streams[0], dict) else {}
    fmt = probe.get("format") if isinstance(probe, dict) and isinstance(probe.get("format"), dict) else {}
    width = positive_int(stream.get("width"))
    height = positive_int(stream.get("height"))
    fps = ratio(stream.get("avg_frame_rate")) or ratio(stream.get("r_frame_rate"))
    frame_count = positive_int(stream.get("nb_frames")) or positive_int(stream.get("nb_read_frames"))
    duration = positive_float(stream.get("duration")) or positive_float(fmt.get("duration"))
    bitrate = positive_int(stream.get("bit_rate")) or positive_int(fmt.get("bit_rate"))
    metrics = {
        "codec": stream.get("codec_name"),
        "width": width,
        "height": height,
        "fps": round(fps, 6) if fps else 0.0,
        "frame_count": frame_count,
        "duration_sec": round(duration, 6) if duration else 0.0,
        "bitrate_bps": bitrate,
    }
    result.update(metrics)
    invalid = [("resolution", width and height), ("fps", fps), ("frame_count", frame_count), ("duration", duration), ("bitrate", bitrate)]
    for metric, valid in invalid:
        if not valid:
            failures.append(issue("F_VIDEO_METADATA_INVALID", f"video {metric} is missing or non-positive", path=rel, metric=metric))
    if all(valid for _, valid in invalid):
        expected_frames = fps * duration
        tolerance = max(2.0, expected_frames * 0.05)
        if abs(frame_count - expected_frames) > tolerance:
            warnings.append(issue("W_VIDEO_FRAME_TIMING_MISMATCH", "frame count differs from fps × duration", path=rel, expected=round(expected_frames, 3), actual=frame_count))
        result["status"] = "pass"
    return result


def validate_sensor_sequences(
    run_dir: Path,
    view_ids: list[str],
    media: dict[str, Any],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    views: dict[str, Any] = {}
    for camera_id in view_ids:
        meta = read_optional_json(run_dir / "views" / camera_id / "meta.json")
        evidence = sequence_evidence_for_view(run_dir, camera_id, meta)
        rgb_count = int((((media.get("views") or {}).get(camera_id) or {}).get("rgb") or {}).get("frame_count") or 0)
        view_status = "pass"
        for modality in ("depth", "segmentation"):
            count = int((evidence.get(modality) or {}).get("frame_count") or 0)
            if count <= 0:
                failures.append(
                    issue(
                        f"F_{modality.upper()}_SEQUENCE_EVIDENCE_MISSING",
                        f"{modality} frame-count evidence is missing",
                        camera_id=camera_id,
                    )
                )
                view_status = "fail"
            elif rgb_count > 0 and count != rgb_count:
                failures.append(
                    issue(
                        f"F_{modality.upper()}_FRAME_COUNT_MISMATCH",
                        f"{modality} frame count differs from decoded RGB",
                        camera_id=camera_id,
                        rgb_frame_count=rgb_count,
                        modality_frame_count=count,
                        evidence_source=(evidence.get(modality) or {}).get("source"),
                    )
                )
                view_status = "fail"
        views[camera_id] = {"status": view_status, "rgb_frame_count": rgb_count, "evidence": evidence}
    return {"status": "pass" if views and all(row["status"] == "pass" for row in views.values()) else "fail", "views": views}


def validate_exr(path: Path, run_dir: Path, modality: str, failures: list[dict[str, Any]]) -> dict[str, Any]:
    rel = relative(path, run_dir)
    result = {"path": rel, "status": "fail", "detected_format": "missing"}
    if not path.is_file():
        failures.append(issue(f"F_{modality.upper()}_MISSING", f"{modality}.exr is missing", path=rel))
        return result
    detected = detect_image_format(path)
    result["detected_format"] = detected
    result["size_bytes"] = path.stat().st_size
    if detected != "openexr":
        failures.append(issue(f"F_{modality.upper()}_MAGIC_INVALID", f"{modality}.exr does not contain OpenEXR magic", path=rel, detected_format=detected))
        return result
    result["status"] = "pass"
    return result


def validate_segmentation(view_dir: Path, run_dir: Path, failures: list[dict[str, Any]]) -> dict[str, Any]:
    expected = view_dir / "segmentation.exr"
    if expected.is_file():
        return validate_exr(expected, run_dir, "segmentation", failures)
    legacy = view_dir / "segmentation.png"
    if legacy.is_file():
        detected = detect_image_format(legacy)
        result = {
            "path": relative(legacy, run_dir),
            "expected_path": relative(expected, run_dir),
            "status": "fail",
            "detected_format": detected,
            "size_bytes": legacy.stat().st_size,
        }
        if detected == "openexr":
            failures.append(
                issue(
                    "F_SEGMENTATION_EXTENSION_MISMATCH",
                    "legacy segmentation.png contains OpenEXR data; rename/write it as segmentation.exr",
                    path=relative(legacy, run_dir),
                    detected_format=detected,
                )
            )
        else:
            failures.append(issue("F_SEGMENTATION_EXR_MISSING", "segmentation.exr is required by the quality contract", path=relative(legacy, run_dir), detected_format=detected))
        return result
    failures.append(issue("F_SEGMENTATION_MISSING", "segmentation.exr is missing", path=relative(expected, run_dir)))
    return {"path": relative(expected, run_dir), "status": "fail", "detected_format": "missing"}


def validate_depth_pixels(
    view_dir: Path,
    run_dir: Path,
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    meta = read_optional_json(view_dir / "meta.json")
    frame_paths = meta.get("depth_frames") if isinstance(meta, dict) else None
    if not isinstance(frame_paths, list) or not frame_paths:
        return {"status": "not_checked", "reason": "depth frame list is unavailable"}
    samples = []
    for frame_index in sorted({0, len(frame_paths) // 2}):
        raw_path = Path(str(frame_paths[frame_index]))
        depth_path = raw_path if raw_path.is_absolute() else run_dir / raw_path
        statistics = depth_pixel_statistics(depth_path)
        if not statistics:
            failure = issue("F_DEPTH_PIXEL_DECODE_FAILED", "depth pixel sample could not be decoded", camera_id=view_dir.name, frame=frame_index, path=relative(depth_path, run_dir))
            failures.append(failure)
            return {"status": "fail", "samples": samples, "failure": failure}
        sample = {"frame": frame_index, **statistics}
        samples.append(sample)
        if statistics["maximum"] - statistics["minimum"] <= 1e-6 or statistics["variance"] <= 1e-12:
            failure = issue("F_DEPTH_CONSTANT", "depth frame is constant and contains no scene geometry", camera_id=view_dir.name, **sample)
            failures.append(failure)
            return {"status": "fail", "samples": samples, "failure": failure}
    return {"status": "pass", "samples": samples}


def validate_segmentation_pixels(
    view_dir: Path,
    run_dir: Path,
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    meta = read_optional_json(view_dir / "meta.json")
    frame_paths = meta.get("segmentation_frames") if isinstance(meta, dict) else None
    if not isinstance(frame_paths, list) or not frame_paths:
        return {"status": "not_checked", "reason": "segmentation frame list is unavailable"}

    sample_indices = sorted({0, len(frame_paths) // 2})
    instance_count = positive_int(meta.get("instance_count")) or len(meta.get("instance_mapping") or [])
    instance_mapping = meta.get("instance_mapping") if isinstance(meta.get("instance_mapping"), list) else []
    closure_required = meta.get("segmentation_palette_closure") is True
    palette_values = meta.get("segmentation_palette_rgb8") if isinstance(meta.get("segmentation_palette_rgb8"), list) else []
    palette = [tuple(int(channel) for channel in color[:3]) for color in palette_values if isinstance(color, list) and len(color) >= 3]
    maximum_colors = len(palette) if closure_required and palette else max(64, (instance_count + 1) * 8)
    samples = []
    pixel_failures = []
    failure_codes = set()
    if instance_mapping and not closure_required:
        pixel_failures.append(
            issue(
                "F_SEGMENTATION_PALETTE_CONTRACT_MISSING",
                "instance segmentation declares an instance mapping but not palette closure",
                camera_id=view_dir.name,
            )
        )
        failure_codes.add("F_SEGMENTATION_PALETTE_CONTRACT_MISSING")
    for frame_index in sample_indices:
        raw_path = Path(str(frame_paths[frame_index]))
        segmentation_path = raw_path if raw_path.is_absolute() else run_dir / raw_path
        segmentation = decode_rgb_sample(segmentation_path)
        rgb = decode_rgb_sample(view_dir / "rgb.mp4", frame_index=frame_index)
        if segmentation is None or rgb is None or len(segmentation) != len(rgb):
            failure = issue(
                "F_SEGMENTATION_PIXEL_DECODE_FAILED",
                "segmentation/RGB pixel sample could not be decoded",
                camera_id=view_dir.name,
                frame=frame_index,
                path=relative(segmentation_path, run_dir),
            )
            failures.append(failure)
            return {"status": "fail", "samples": samples, "failure": failure}

        differences = [abs(left - right) for left, right in zip(segmentation, rgb)]
        mean_absolute_error = sum(differences) / len(differences)
        near_equal_ratio = sum(value <= 4 for value in differences) / len(differences)
        colors = set(zip(segmentation[0::3], segmentation[1::3], segmentation[2::3]))
        off_palette_colors = []
        if closure_required:
            off_palette_colors = [
                color
                for color in colors
                if not any(max(abs(color[channel] - declared[channel]) for channel in range(3)) <= 1 for declared in palette)
            ]
        sample = {
            "frame": frame_index,
            "mean_absolute_error": round(mean_absolute_error, 4),
            "near_equal_ratio": round(near_equal_ratio, 6),
            "rgb_correlation": round(byte_correlation(segmentation, rgb), 6),
            "unique_color_count": len(colors),
            "maximum_expected_colors": maximum_colors,
            "palette_closure_required": closure_required,
            "off_palette_color_count": len(off_palette_colors),
        }
        samples.append(sample)
        if mean_absolute_error <= 4.0 and sample["rgb_correlation"] >= 0.995 and "F_SEGMENTATION_RGB_DUPLICATE" not in failure_codes:
            pixel_failures.append(
                issue(
                    "F_SEGMENTATION_RGB_DUPLICATE",
                    "segmentation is nearly identical to RGB",
                    camera_id=view_dir.name,
                    **sample,
                )
            )
            failure_codes.add("F_SEGMENTATION_RGB_DUPLICATE")
        if sample["unique_color_count"] > maximum_colors and "F_SEGMENTATION_COLOR_CARDINALITY" not in failure_codes:
            pixel_failures.append(
                issue(
                    "F_SEGMENTATION_COLOR_CARDINALITY",
                    "segmentation contains too many color categories for its instance mapping",
                    camera_id=view_dir.name,
                    **sample,
                )
            )
            failure_codes.add("F_SEGMENTATION_COLOR_CARDINALITY")
        if closure_required and (
            not palette
            or off_palette_colors
            or not any(color != (0, 0, 0) for color in colors)
        ) and "F_SEGMENTATION_PALETTE_CLOSURE" not in failure_codes:
            pixel_failures.append(
                issue(
                    "F_SEGMENTATION_PALETTE_CLOSURE",
                    "quantized segmentation contains pixels outside its declared instance palette",
                    camera_id=view_dir.name,
                    **sample,
                )
            )
            failure_codes.add("F_SEGMENTATION_PALETTE_CLOSURE")
    if pixel_failures:
        failures.extend(pixel_failures)
        return {"status": "fail", "samples": samples, "failures": pixel_failures}
    return {"status": "pass", "samples": samples}


def byte_correlation(left: bytes, right: bytes) -> float:
    count = len(left)
    sum_left = sum(left)
    sum_right = sum(right)
    numerator = count * sum(a * b for a, b in zip(left, right)) - sum_left * sum_right
    variance = (count * sum(value * value for value in left) - sum_left**2) * (
        count * sum(value * value for value in right) - sum_right**2
    )
    if variance <= 0:
        return 1.0 if left == right else 0.0
    return numerator / math.sqrt(variance)


def decode_rgb_sample(path: Path, *, frame_index: int | None = None) -> bytes | None:
    filters = []
    if frame_index is not None:
        filters.append(f"select=eq(n\\,{frame_index})")
    filters.append("scale=64:64:flags=neighbor")
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-vf",
                ",".join(filters),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout
    return output if completed.returncode == 0 and isinstance(output, bytes) and len(output) == 64 * 64 * 3 else None


def detect_image_format(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            header = handle.read(8)
    except OSError:
        return "unreadable"
    if header.startswith(EXR_MAGIC):
        return "openexr"
    if header.startswith(PNG_MAGIC):
        return "png"
    return "unknown"


def validate_trajectory(run_dir: Path, failures: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = next((item for item in (run_dir / "trajectory.json", run_dir / "ue_output" / "trajectory.json") if item.is_file()), None)
    if path is None:
        failures.append(issue("F_TRAJECTORY_MISSING", "trajectory.json is missing"))
        return {"present": False, "status": "fail", "frame_count": 0}, []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        failures.append(issue("F_TRAJECTORY_INVALID", str(exc), path=relative(path, run_dir)))
        return {"present": True, "path": relative(path, run_dir), "status": "fail", "frame_count": 0}, []
    frames = value.get("frames") if isinstance(value, dict) else value
    if not isinstance(frames, list) or not frames:
        failures.append(issue("F_TRAJECTORY_EMPTY", "trajectory must contain at least one frame", path=relative(path, run_dir)))
        return {"present": True, "path": relative(path, run_dir), "status": "fail", "frame_count": 0}, []

    nonfinite = find_nonfinite(frames)
    if nonfinite:
        failures.append(issue("F_TRAJECTORY_NONFINITE", "trajectory contains NaN or Infinity", path=relative(path, run_dir), locations=nonfinite[:20]))
    times: list[float] = []
    missing_time: list[int] = []
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            missing_time.append(index)
            continue
        raw = frame["time_s"] if "time_s" in frame else frame.get("time")
        try:
            timestamp = float(raw)
        except (TypeError, ValueError):
            missing_time.append(index)
            continue
        if not math.isfinite(timestamp):
            missing_time.append(index)
            continue
        times.append(timestamp)
    if missing_time:
        failures.append(issue("F_TRAJECTORY_TIME_INVALID", "trajectory frames have missing or invalid time", frame_indices=missing_time[:20]))
    monotonic = len(times) == len(frames) and all(current >= previous for previous, current in zip(times, times[1:]))
    if not monotonic:
        failures.append(issue("F_TRAJECTORY_TIME_NON_MONOTONIC", "trajectory time must be monotonically non-decreasing"))
    status = "pass" if not nonfinite and not missing_time and monotonic else "fail"
    return {
        "present": True,
        "path": relative(path, run_dir),
        "status": status,
        "frame_count": len(frames),
        "finite": not nonfinite,
        "time_monotonic": monotonic,
        "start_time": times[0] if times else None,
        "end_time": times[-1] if times else None,
    }, [item for item in frames if isinstance(item, dict)]


def find_nonfinite(value: Any, path: str = "$", found: list[str] | None = None) -> list[str]:
    found = found if found is not None else []
    if isinstance(value, float) and not math.isfinite(value):
        found.append(path)
    elif isinstance(value, str) and value.strip().lower() in {"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
        found.append(path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            find_nonfinite(item, f"{path}[{index}]", found)
    elif isinstance(value, dict):
        for key, item in value.items():
            find_nonfinite(item, f"{path}.{key}", found)
    return found


def validate_solver_execution(
    run_dir: Path,
    trajectory: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    *,
    backend: str | None = None,
) -> dict[str, Any]:
    case = read_case_spec(run_dir)
    expected = case.get("expected_physics") if isinstance(case.get("expected_physics"), dict) else {}
    explicit_contract = expected.get("simulation_contract") if isinstance(expected.get("simulation_contract"), dict) else {}
    readiness = read_optional_json(run_dir / "run_readiness.json")
    run_backend = backend or (readiness.get("backend") if isinstance(readiness, dict) else None)
    if run_backend is None and (run_dir / "ue_backend_report.json").is_file():
        run_backend = "ue"
    from harness.core.physics_contract import execution_capability_id

    policy_plan = backend_plan(execution_capability_id(case))
    policy_requires_chaos = (
        run_backend == "ue"
        and policy_plan.get("preferred_backend") == "ue"
    )
    explicit_requires_chaos = explicit_contract.get("input_mode") == "initial_state_only"
    if not policy_requires_chaos and not explicit_requires_chaos:
        return {"required": False, "status": "not_required", "contract": explicit_contract, "contract_source": None}

    policy_contract = {
        "input_mode": "initial_state_only",
        "state_solver": "ue_chaos",
        "trajectory_role": "solver_output_render_cache",
    }
    normalized_contract = dict(explicit_contract)
    if normalized_contract.get("trajectory_role") == "output_evidence_only":
        normalized_contract["trajectory_role"] = "solver_output_render_cache"
    contract_source = "case_spec" if explicit_contract else "backend_policy"
    contract = {**policy_contract, **normalized_contract} if policy_requires_chaos else normalized_contract
    contract_violations = [
        f"simulation_contract_{key}"
        for key, value in policy_contract.items()
        if contract.get(key) != value
    ]

    summary_candidates = [
        path
        for path in (
            run_dir / "logs" / "native_combined" / "summary.json",
            run_dir / "logs" / "native_rgb" / "summary.json",
            run_dir / "logs" / "native_data" / "summary.json",
            run_dir / "ue_output" / "summary.json",
        )
        if path.is_file()
    ]
    summary_path = next((path for path in summary_candidates if (path.parent / "cpp_physics_capture.json").is_file()), None)
    summary_path = summary_path or next(iter(summary_candidates), None)
    capture_path = summary_path.parent / "cpp_physics_capture.json" if summary_path else None
    summary = read_optional_json(summary_path) if summary_path else {}
    declared_runtime = ((summary.get("studio_runtime_scene") or {}).get("path")) if isinstance(summary.get("studio_runtime_scene"), dict) else None
    runtime_path: Path | None = None
    runtime_path_violation: str | None = None
    if declared_runtime:
        candidate = Path(str(declared_runtime))
        candidate = candidate if candidate.is_absolute() else run_dir / candidate
        try:
            candidate.resolve().relative_to(run_dir.resolve())
        except ValueError:
            runtime_path_violation = "native_runtime_scene_outside_run"
        else:
            if candidate.is_file():
                runtime_path = candidate
            else:
                runtime_path_violation = "native_runtime_scene_missing"
    if runtime_path is None and not declared_runtime:
        runtime_path = next(
            (
                path
                for path in (
                    run_dir / "studio_runtime_scene.json",
                    run_dir / "logs" / "studio_runtime_scene_combined.json",
                    run_dir / "logs" / "studio_runtime_scene_rgb.json",
                    run_dir / "logs" / "studio_runtime_scene_data.json",
                )
                if path.is_file()
            ),
            None,
        )
    runtime = read_optional_json(runtime_path) if runtime_path else {}
    raw_capture = read_optional_json(capture_path) if capture_path else {}
    sampling_map = read_optional_json(run_dir / "sampling_map.json")
    violations: list[str] = list(contract_violations)
    if runtime_path_violation:
        violations.append(runtime_path_violation)

    if not runtime_path or not isinstance(runtime, dict):
        violations.append("runtime_scene_missing")
    controls = runtime.get("physics_controls") if isinstance(runtime.get("physics_controls"), dict) else {}
    precomputed = runtime.get("precomputed_trajectory")
    if precomputed:
        violations.append("precomputed_trajectory_present")
    required_controls = {
        "simulate_physics": True,
        "runtime_driver_backend": "cpp_runtime_driver",
        "cpp_runtime_driver_enabled": True,
        "deterministic_replay_fallback": False,
    }
    for key, required in required_controls.items():
        if controls.get(key) != required:
            violations.append(f"runtime_control_{key}")

    dynamic_objects = runtime.get("dynamic_objects") if isinstance(runtime.get("dynamic_objects"), list) else []
    dynamic_ids = {
        str(item.get("id"))
        for item in dynamic_objects
        if isinstance(item, dict) and item.get("id")
    }
    if not dynamic_ids:
        violations.append("dynamic_objects_missing")

    physics_capture = summary.get("physics_capture") if isinstance(summary.get("physics_capture"), dict) else {}
    cpp_status = physics_capture.get("cpp_runtime_driver") if isinstance(physics_capture.get("cpp_runtime_driver"), dict) else {}
    chaos_runtime = summary.get("chaos_runtime") if isinstance(summary.get("chaos_runtime"), dict) else {}
    chaos_controls = chaos_runtime.get("controls") if isinstance(chaos_runtime.get("controls"), dict) else {}
    if not summary_path:
        violations.append("native_summary_missing")
    if physics_capture.get("enabled") is not True:
        violations.append("physics_capture_disabled")
    if cpp_status.get("started") is not True:
        violations.append("cpp_driver_not_started")
    if cpp_status.get("capture_complete") is not True:
        violations.append("cpp_capture_incomplete")
    if int(physics_capture.get("game_world_count") or 0) < 1:
        violations.append("game_world_missing")
    if physics_capture.get("initial_state_reset") is not True:
        violations.append("initial_state_not_reset_in_game_world")
    registered_dynamic = {str(item) for item in cpp_status.get("registered_dynamic") or []}
    if registered_dynamic != dynamic_ids:
        violations.append("registered_dynamic_mismatch")
    for key, required in required_controls.items():
        if chaos_controls.get(key) != required:
            violations.append(f"chaos_control_{key}")

    actor_status = {
        str(item.get("id")): item
        for item in chaos_runtime.get("actors") or []
        if isinstance(item, dict) and item.get("id") and item.get("role") == "dynamic"
    }
    for object_id in sorted(dynamic_ids):
        actor = actor_status.get(object_id)
        if not actor:
            violations.append(f"dynamic_actor_missing:{object_id}")
        elif actor.get("simulate_physics") is not True or actor.get("collision_enabled") is not True or actor.get("errors"):
            violations.append(f"dynamic_actor_not_live:{object_id}")

    if not capture_path or not isinstance(raw_capture, dict):
        violations.append("cpp_capture_missing")
    raw_frames = raw_capture.get("frames") if isinstance(raw_capture.get("frames"), list) else []
    raw_count = int(raw_capture.get("frame_count") or len(raw_frames)) if isinstance(raw_capture, dict) else 0
    expected_count = len(trajectory)
    simulation = runtime.get("simulation") if isinstance(runtime.get("simulation"), dict) else {}
    render_fps = int(simulation.get("render_fps") or simulation.get("fps") or 0)
    physics_hz = int(simulation.get("physics_hz") or render_fps or 0)
    substeps_per_render = int(simulation.get("substeps_per_render") or 1)
    capture_mode = str(simulation.get("solver_capture_mode") or "full_solver")
    declared_solver_frame_count = int(simulation.get("solver_frame_count") or expected_count)
    full_solver_frame_count = int(simulation.get("full_solver_frame_count") or declared_solver_frame_count)
    physics_step_count = int(simulation.get("physics_step_count") or max(0, full_solver_frame_count - 1))
    timebase_declared = render_fps > 0
    physics_source_indices = [int(value) for value in simulation.get("source_solver_indices") or []]
    if not physics_source_indices:
        physics_source_indices = list(range(expected_count))
    source_indices = list(range(expected_count)) if capture_mode == "render_boundary" else physics_source_indices
    expected_raw_count = int(
        simulation.get("raw_capture_frame_count") or expected_count
        if capture_mode == "render_boundary"
        else simulation.get("solver_frame_count") or expected_count
    )
    if timebase_declared and (physics_hz < render_fps or physics_hz % render_fps or substeps_per_render != physics_hz // render_fps):
        violations.append("solver_timebase_invalid")
    if capture_mode == "render_boundary":
        expected_full_solver_frame_count = max(0, expected_count - 1) * substeps_per_render + (1 if expected_count else 0)
        if declared_solver_frame_count != expected_raw_count:
            violations.append("declared_solver_frame_count_mismatch")
        if physics_step_count + 1 != full_solver_frame_count:
            violations.append("physics_step_count_mismatch")
        if full_solver_frame_count != expected_full_solver_frame_count:
            violations.append("full_solver_frame_count_mismatch")
    if len(physics_source_indices) != expected_count or len(source_indices) != expected_count or (source_indices and source_indices[-1] >= raw_count):
        violations.append("solver_sampling_map_invalid")
    if physics_hz > render_fps:
        samples = sampling_map.get("samples") if isinstance(sampling_map.get("samples"), list) else []
        sampled_indices = [int(item.get("source_solver_frame") or 0) for item in samples if isinstance(item, dict)]
        if sampled_indices != source_indices:
            violations.append("solver_sampling_map_missing_or_mismatch")
        solver_path = run_dir / "solver_trajectory.json"
        expected_sha = str(sampling_map.get("solver_cache_sha256") or "")
        actual_sha = hashlib.sha256(solver_path.read_bytes()).hexdigest() if solver_path.is_file() else ""
        if not expected_sha or expected_sha != actual_sha:
            violations.append("solver_cache_hash_mismatch")
        if capture_mode == "render_boundary":
            substepping = physics_capture.get("physics_substepping") if isinstance(physics_capture.get("physics_substepping"), dict) else {}
            if (
                substepping.get("enabled") is not True
                or int(substepping.get("max_substeps") or 0) < substeps_per_render
                or abs(float(substepping.get("max_substep_delta_time_s") or 0.0) - 1.0 / physics_hz) > 1e-6
            ):
                violations.append("chaos_substepping_not_applied")
    if raw_capture.get("driver") != "ADPPhysicsRuntimeDriver":
        violations.append("cpp_capture_driver_invalid")
    if raw_capture.get("capture_complete") is not True:
        violations.append("raw_capture_incomplete")
    if raw_count != expected_raw_count or int(physics_capture.get("actual_frame_count") or 0) != expected_count or int(cpp_status.get("trajectory_frames") or 0) != expected_count:
        violations.append("solver_frame_count_mismatch")
    if physics_hz > render_fps and int(cpp_status.get("solver_trajectory_frames") or 0) != expected_raw_count:
        violations.append("solver_raw_frame_count_mismatch")

    raw_by_frame = {
        int(frame.get("frame") or 0): frame
        for frame in raw_frames
        if isinstance(frame, dict)
    }
    canonical_frame_ids = [int(frame.get("frame") or 0) for frame in trajectory]
    raw_frame_ids = [int(frame.get("frame") or 0) for frame in raw_frames if isinstance(frame, dict)]
    if canonical_frame_ids != list(range(expected_count)) or raw_frame_ids != list(range(raw_count)):
        violations.append("solver_frame_id_mismatch")
    sample_interval = number(raw_capture.get("sample_interval_s"))
    if expected_count > 1 and (sample_interval is None or sample_interval <= 0):
        violations.append("solver_sample_interval_missing_or_invalid")
    expected_sample_interval = (1.0 / render_fps) if capture_mode == "render_boundary" and render_fps else (1.0 / physics_hz if physics_hz else None)
    if expected_sample_interval is not None and sample_interval is not None and abs(sample_interval - expected_sample_interval) > 1e-6:
        violations.append("solver_physics_dt_mismatch")
    frame_time_mismatch = False
    for canonical_index, canonical_frame in enumerate(trajectory):
        frame_index = int(canonical_frame.get("frame") or 0)
        source_index = source_indices[canonical_index] if canonical_index < len(source_indices) else frame_index
        raw_frame = raw_by_frame.get(source_index)
        if not raw_frame:
            frame_time_mismatch = True
            continue
        canonical_time = number(canonical_frame.get("time_s", canonical_frame.get("time")))
        raw_time = number(raw_frame.get("time_s", raw_frame.get("time")))
        if canonical_time is None or raw_time is None or abs(canonical_time - raw_time) > 1e-3:
            frame_time_mismatch = True
        if sample_interval is not None and (raw_time is None or abs(raw_time - source_index * sample_interval) > 1e-3):
            frame_time_mismatch = True
        source_solver_frame = canonical_frame.get("source_solver_frame")
        if physics_hz > render_fps and int(-1 if source_solver_frame is None else source_solver_frame) != source_index:
            frame_time_mismatch = True
        source_physics_step = canonical_frame.get("source_physics_step")
        if capture_mode == "render_boundary" and physics_hz > render_fps and int(-1 if source_physics_step is None else source_physics_step) != physics_source_indices[canonical_index]:
            frame_time_mismatch = True
    if frame_time_mismatch:
        violations.append("solver_frame_time_mismatch")

    mismatch_count = 0
    for canonical_index, canonical_frame in enumerate(trajectory):
        frame_index = int(canonical_frame.get("frame") or 0)
        source_index = source_indices[canonical_index] if canonical_index < len(source_indices) else frame_index
        raw_frame = raw_by_frame.get(source_index)
        if not raw_frame or raw_frame.get("source") != "adp_cpp_runtime_driver" or canonical_frame.get("source") != "adp_cpp_runtime_driver":
            mismatch_count += 1
            continue
        canonical_objects = canonical_frame.get("objects") if isinstance(canonical_frame.get("objects"), dict) else {}
        raw_objects = raw_frame.get("objects") if isinstance(raw_frame.get("objects"), dict) else {}
        for object_id in dynamic_ids:
            canonical_state = canonical_objects.get(object_id)
            raw_state = raw_objects.get(object_id)
            if not isinstance(canonical_state, dict) or not isinstance(raw_state, dict):
                mismatch_count += 1
                continue
            if canonical_state.get("source") != "adp_cpp_runtime_driver" or raw_state.get("source") != "adp_cpp_runtime_driver":
                mismatch_count += 1
                continue
            for field in ("position_cm", "rotation_degrees", "velocity_cm_s"):
                if not vectors_close(canonical_state.get(field), raw_state.get(field), tolerance=1e-3):
                    mismatch_count += 1
                    break
    if mismatch_count:
        violations.append("canonical_trace_not_derived_from_cpp_capture")

    canonical_contact_payload = read_optional_json(run_dir / "contact_events.json")
    canonical_contact_events = canonical_contact_payload.get("events") if isinstance(canonical_contact_payload, dict) else canonical_contact_payload
    canonical_contact_events = canonical_contact_events if isinstance(canonical_contact_events, list) else []
    contact_method_mapping = {
        "adp_cpp_runtime_bounds_overlap_or_near_contact": "ue_postsolve_bounds_inference",
        "ue_on_component_hit": "ue_native_component_hit",
    }
    raw_contact_signatures: Counter[tuple[int, tuple[str, str]]] = Counter()
    raw_contact_method_valid = True
    for raw_frame in raw_frames:
        if not isinstance(raw_frame, dict):
            continue
        frame_index = int(raw_frame.get("frame") or 0)
        for event in raw_frame.get("contacts") or []:
            signature = contact_signature(frame_index, event)
            if signature:
                raw_contact_signatures[signature] += 1
            if not isinstance(event, dict) or event.get("method") not in contact_method_mapping:
                raw_contact_method_valid = False
    canonical_contact_signatures: Counter[tuple[int, tuple[str, str]]] = Counter()
    canonical_contact_method_valid = True
    for event in canonical_contact_events:
        if not isinstance(event, dict):
            continue
        signature = contact_signature(int(event.get("source_solver_frame", event.get("frame") or 0)), event)
        if signature:
            canonical_contact_signatures[signature] += 1
        raw_method = event.get("raw_method")
        if event.get("method") != contact_method_mapping.get(raw_method):
            canonical_contact_method_valid = False
    contacts_match = raw_contact_signatures == canonical_contact_signatures and raw_contact_method_valid and canonical_contact_method_valid
    if not contacts_match:
        violations.append("canonical_contacts_not_derived_from_cpp_capture")

    effective_initial = summary.get("runtime_initial_transforms") if isinstance(summary.get("runtime_initial_transforms"), dict) else {}
    if trajectory and dynamic_objects:
        first_objects = trajectory[0].get("objects") if isinstance(trajectory[0].get("objects"), dict) else {}
        for obj in dynamic_objects:
            if not isinstance(obj, dict) or not obj.get("id"):
                continue
            object_id = str(obj["id"])
            state = first_objects.get(object_id) if isinstance(first_objects.get(object_id), dict) else {}
            initial_position_cm = (effective_initial.get(object_id) or {}).get("position_cm") if isinstance(effective_initial.get(object_id), dict) else None
            initial_velocity = ((obj.get("physics_properties") or {}).get("initial_velocity_m_s")) if isinstance(obj.get("physics_properties"), dict) else None
            if not vectors_close(state.get("position_cm"), initial_position_cm, tolerance=1e-3):
                violations.append(f"initial_position_not_captured:{object_id}")
            if isinstance(initial_velocity, list):
                velocity_cm_s = [float(value) * 100.0 for value in initial_velocity[:3]]
                if not vectors_close(state.get("velocity_cm_s"), velocity_cm_s, tolerance=1e-3):
                    violations.append(f"initial_velocity_not_captured:{object_id}")

    violations = list(dict.fromkeys(violations))
    if violations:
        failures.append(
            issue(
                "F_RIGID_SOLVER_PROVENANCE",
                "initial-state UE Chaos execution was not proven from raw solver capture",
                violations=violations,
            )
        )
    return {
        "required": True,
        "status": "fail" if violations else "pass",
        "contract": contract,
        "declared_contract": explicit_contract,
        "contract_source": contract_source,
        "runtime_scene": relative(runtime_path, run_dir) if runtime_path else None,
        "native_summary": relative(summary_path, run_dir) if summary_path else None,
        "raw_capture": relative(capture_path, run_dir) if capture_path else None,
        "solver": "ue_chaos",
        "driver": raw_capture.get("driver") if isinstance(raw_capture, dict) else None,
        "dynamic_ids": sorted(dynamic_ids),
        "trajectory_frame_count": expected_count,
        "raw_frame_count": raw_count,
        "timebase": {
            "physics_hz": physics_hz,
            "render_fps": render_fps,
            "substeps_per_render": substeps_per_render,
            "physics_step_count": physics_step_count,
            "full_solver_frame_count": full_solver_frame_count,
            "solver_frame_count": declared_solver_frame_count,
            "canonical_frame_count": expected_count,
            "solver_capture_mode": capture_mode,
        },
        "trace_mismatch_count": mismatch_count,
        "contact_evidence": (
            "ue_native_component_hit"
            if contacts_match and any(event.get("method") == "ue_native_component_hit" for event in canonical_contact_events if isinstance(event, dict))
            else "ue_postsolve_bounds_inference"
            if contacts_match
            else None
        ),
        "violations": violations,
    }


def vectors_close(left: Any, right: Any, *, tolerance: float = 1e-5) -> bool:
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)) or len(left) < 3 or len(right) < 3:
        return False
    try:
        return all(abs(float(a) - float(b)) <= tolerance for a, b in zip(left[:3], right[:3]))
    except (TypeError, ValueError):
        return False


def number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def contact_signature(frame_index: int, event: Any) -> tuple[int, tuple[str, str]] | None:
    if not isinstance(event, dict):
        return None
    objects = event.get("objects")
    if not isinstance(objects, list) or len(objects) < 2:
        return None
    pair = tuple(sorted((str(objects[0]), str(objects[1]))))
    return frame_index, pair


def validate_contacts(run_dir: Path, trajectory: list[dict[str, Any]], failures: list[dict[str, Any]]) -> dict[str, Any]:
    case = read_case_spec(run_dir)
    path = run_dir / "contact_events.json"
    events: list[dict[str, Any]] = []
    if path.is_file():
        value = read_optional_json(path, default=[])
        raw_events = value.get("events") if isinstance(value, dict) else value
        if isinstance(raw_events, list):
            events = [item for item in raw_events if isinstance(item, dict)]
    if not events:
        for frame in trajectory:
            frame_id = frame.get("frame")
            for contact in frame.get("contacts") or []:
                if isinstance(contact, dict):
                    item = dict(contact)
                    item.setdefault("frame", frame_id)
                    events.append(item)

    graph = collision_graph(case)
    positive = positive_collision_case(case, graph)
    first_by_edge: dict[tuple[str, str], int] = {}
    for event in events:
        objects = event.get("objects")
        if not isinstance(objects, list) or len(objects) < 2:
            continue
        pair = tuple(sorted((str(objects[0]), str(objects[1]))))
        frame = integer(event.get("frame"), default=-1)
        if frame >= 0:
            first_by_edge[pair] = min(frame, first_by_edge.get(pair, frame))

    expected: list[dict[str, Any]] = []
    checked_frames: list[int] = []
    if positive and graph:
        for pair in graph:
            frame = first_by_edge.get(pair)
            expected.append({"objects": list(pair), "first_frame": frame})
            if frame is None:
                failures.append(issue("F_EXPECTED_CONTACT_MISSING", "expected collision edge was not observed", objects=list(pair)))
            else:
                checked_frames.append(frame)
                if frame == 0:
                    failures.append(issue("F_CONTACT_AT_INITIAL_FRAME", "positive collision evidence starts at frame 0", objects=list(pair), frame=0))
    elif positive:
        support = {str(item.get("id")) for item in case.get("objects", []) if isinstance(item, dict) and item.get("role") == "support"}
        checked_frames = [frame for pair, frame in first_by_edge.items() if not set(pair) & support]
        if not checked_frames:
            failures.append(issue("F_EXPECTED_CONTACT_MISSING", "positive collision case has no non-support contact"))
        elif min(checked_frames) == 0:
            failures.append(issue("F_CONTACT_AT_INITIAL_FRAME", "positive collision evidence starts at frame 0", frame=0))

    initial_expected_contact_free = not positive or bool(checked_frames and min(checked_frames) > 0)
    return {
        "event_count": len(events),
        "positive_collision_case": positive,
        "expected_edges": expected,
        "first_positive_contact_frame": min(checked_frames) if checked_frames else None,
        "initial_expected_contact_free": initial_expected_contact_free,
        "initial_contact_scope": "expected_collision_graph" if graph else "non_support_contacts",
    }


def state_position(state: dict[str, Any]) -> list[float]:
    raw = state.get("position") or state.get("position_m") or [0.0, 0.0, 0.0]
    values = list(raw) if isinstance(raw, (list, tuple)) else []
    values.extend([0.0, 0.0, 0.0])
    return [float(values[0]), float(values[1]), float(values[2])]


def validate_camera_motion(run_dir: Path, failures: list[dict[str, Any]]) -> dict[str, Any]:
    path = next((item for item in (run_dir / "camera_trajectory.json", run_dir / "sync" / "camera_trajectory.json") if item.is_file()), None)
    if path is None:
        failures.append(issue("F_CAMERA_TRAJECTORY_MISSING", "camera trajectory is missing"))
        return {"status": "fail", "views": {}}
    payload = read_optional_json(path)
    views = payload.get("views") if isinstance(payload, dict) else None
    if not isinstance(views, list) or not views:
        failures.append(issue("F_CAMERA_TRAJECTORY_INVALID", "camera trajectory contains no views", path=relative(path, run_dir)))
        return {"status": "fail", "views": {}}
    result: dict[str, Any] = {}
    for view in views:
        if not isinstance(view, dict):
            continue
        view_id = str(view.get("view_id") or view.get("camera_id") or "")
        mode = str(view.get("camera_mode") or "fixed")
        frames = view.get("frames") if isinstance(view.get("frames"), list) else []
        locations = {
            tuple(round(float(value), 4) for value in (frame.get("location_cm") or [])[:3])
            for frame in frames
            if isinstance(frame, dict) and isinstance(frame.get("location_cm"), list) and len(frame["location_cm"]) >= 3
        }
        moving = len(locations) > 1
        if mode in {"object_bound", "trajectory"} and len(frames) > 1 and not moving:
            failures.append(issue("F_DYNAMIC_CAMERA_STATIC", "dynamic camera did not move", camera_id=view_id, camera_mode=mode))
        if mode in {"fixed", "static"} and moving:
            failures.append(issue("F_STATIC_CAMERA_MOVED", "fixed camera changed position", camera_id=view_id))
        result[view_id] = {"camera_mode": mode, "frame_count": len(frames), "unique_location_count": len(locations), "moving": moving}
    return {"status": "pass" if result else "fail", "views": result}


def read_case_spec(run_dir: Path) -> dict[str, Any]:
    for path in (run_dir / "inputs" / "case.json", run_dir / "case_spec.json"):
        value = read_optional_json(path)
        if value:
            return value
    return {}


def collision_graph(case: dict[str, Any]) -> list[tuple[str, str]]:
    raw = (case.get("expected_physics") or {}).get("collision_graph") if isinstance(case.get("expected_physics"), dict) else []
    result: list[tuple[str, str]] = []
    for edge in raw or []:
        if isinstance(edge, list) and len(edge) >= 2:
            result.append(tuple(sorted((str(edge[0]), str(edge[1])))))
    return result


def positive_collision_case(case: dict[str, Any], graph: list[tuple[str, str]]) -> bool:
    if not case or case.get("should_pass") is False or case.get("negative_or_boundary") is True:
        return False
    expected = case.get("verifier_expectation")
    if isinstance(expected, dict) and expected.get("status") == "fail":
        return False
    return bool(graph)


def build_ranking_score(
    *,
    hard_gate_passed: bool,
    reports: dict[str, Any],
    media: dict[str, Any],
    trajectory: dict[str, Any],
    contacts: dict[str, Any],
) -> dict[str, Any]:
    disclaimer = "Technical ranking within comparable harness runs only; this is not an absolute claim of physical or perceptual realism."
    if not hard_gate_passed:
        return {"eligible": False, "technical_score": None, "scale": 100, "components": [], "disclaimer": disclaimer}

    videos = [item for item in media.get("videos", []) if item.get("status") == "pass"]
    video_scores: list[float] = []
    for item in videos:
        pixels = float(item["width"] * item["height"])
        video_scores.append(
            8.0
            + 7.0 * min(pixels / (1920 * 1080), 1.0)
            + 5.0 * min(float(item["fps"]) / 60.0, 1.0)
            + 3.0 * min(float(item["duration_sec"]) / 4.0, 1.0)
            + 2.0 * min(float(item["bitrate_bps"]) / 3_000_000.0, 1.0)
        )
    video_score = sum(video_scores) / len(video_scores) if video_scores else 0.0
    sensor_score = 14.0 + 6.0 * min(float(media.get("valid_view_count") or 0) / 5.0, 1.0)
    physics_score = 15.0
    if not contacts.get("positive_collision_case") or contacts.get("initial_expected_contact_free"):
        physics_score += 5.0
    map_summary = reports.get("map_report") or {}
    asset_summary = reports.get("asset_resolution") or {}
    map_score = 4.0 if map_summary.get("present") and map_summary.get("map_opened") is not False else 0.0
    selected = int(asset_summary.get("selected_count") or 0)
    proxies = int(asset_summary.get("proxy_count") or 0)
    asset_score = 0.0 if selected <= 0 else 6.0 * max(0.0, (selected - proxies) / selected)
    components = [
        {"name": "source_contracts", "earned": 25.0, "maximum": 25.0, "formula": "readiness, verifier, and render-sync hard gates passed"},
        {
            "name": "video_technical",
            "earned": round(video_score, 3),
            "maximum": 25.0,
            "formula": "per-view average: 8 valid + 7×min(pixels/1080p,1) + 5×min(fps/60,1) + 3×min(duration/4s,1) + 2×min(bitrate/3Mbps,1)",
        },
        {"name": "sensor_modalities", "earned": round(sensor_score, 3), "maximum": 20.0, "formula": "14 valid EXR modalities + 6×min(valid_views/5,1)"},
        {"name": "trajectory_and_contact", "earned": round(physics_score, 3), "maximum": 20.0, "formula": "15 finite monotonic trajectory + 5 contact after frame 0 (or collision not applicable)"},
        {"name": "map_and_assets", "earned": round(map_score + asset_score, 3), "maximum": 10.0, "formula": "4 map report/open status + 6×non-proxy selected-asset ratio"},
    ]
    total = round(sum(float(item["earned"]) for item in components), 3)
    return {
        "eligible": True,
        "technical_score": total,
        "scale": 100,
        "components": components,
        "disclaimer": disclaimer,
        "trajectory_frames": trajectory.get("frame_count", 0),
    }


def read_optional_json(path: Path, *, default: Any = None) -> Any:
    if not path.is_file():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {} if default is None else default


def issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def canonical_ue_package(value: Any) -> str:
    text = str(value or "").strip().split(":", 1)[0]
    dot = text.find(".", text.rfind("/"))
    return text[:dot] if dot >= 0 else text.rstrip("/")


def ratio(value: Any) -> float:
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) and result > 0 else 0.0
    text = str(value or "")
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            result = float(numerator) / float(denominator)
        else:
            result = float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def positive_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def integer(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
