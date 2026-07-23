from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from harness.core.artifact_schema import read_json, write_json
from harness.core.camera_sync import camera_trajectory_from_plan
from harness.core.physics_logger import build_physics_trace
from harness.core.sync_validator import validate_world_model_run


WORLD_MODEL_SCHEMA_VERSION = "world_model_run.v2.3"
COMPLETE_DELIVERY_SCHEMA_VERSION = "harness_complete_case_delivery_v4"
COMPLETE_DELIVERY_MIN_SOURCE_RUNS = 3
COMPLETE_DELIVERY_MIN_STATIC_CAMERAS = 3
COMPLETE_DELIVERY_MIN_MOVING_CAMERAS = 2
DELIVERY_REQUIRED_STATIC_CAMERAS = frozenset({"front_static", "side_static", "top_down"})
DELIVERY_REQUIRED_MOVING_CAMERAS = frozenset({"tracking_subject", "event_closeup"})
DELIVERY_CASE_COMPARISON_CAMERA = "event_closeup"
EXACT_REPEAT_COMPARISON_POLICY = "exact_runtime_input_fingerprint_v2"
DECLARED_CONDITION_COMPARISON_POLICY = "declared_condition_matrix_v1"
DELIVERY_DYNAMIC_CAMERA_MODES = frozenset({"object_bound", "trajectory"})
DELIVERY_STATIC_CAMERA_MODES = frozenset({"fixed", "static"})
DELIVERY_MODALITY_FILES = {
    "rgb": "rgb.mp4",
    "depth": "depth_preview.mp4",
    "segmentation": "segmentation_preview.mp4",
}
DELIVERY_ACQUISITION_INPUT_FILES = ("inputs/camera.json", "inputs/render_config.json")
DELIVERY_PUBLICATION_TIERS = frozenset({"reference", "local_preview", "unverified", "rejected"})
DELIVERY_REVIEW_TILE_WIDTH = 640
DELIVERY_REVIEW_TILE_HEIGHT = 360


class DeliveryError(ValueError):
    pass


def delivery_comparison_contract(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify source runs as exact repeats or an explicitly named condition matrix."""
    if not runs:
        raise DeliveryError("a delivery comparison group cannot be empty")
    fingerprints: list[str] = []
    conditions: list[str | None] = []
    for row in runs:
        run_dir = Path(str(row.get("run_dir") or "")).expanduser().resolve(strict=False)
        fingerprint = str(row.get("comparison_fingerprint") or "").strip().casefold()
        if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
            raise DeliveryError(f"source run is missing a valid comparison fingerprint: {run_dir}")
        condition = row.get("condition")
        conditions.append(condition.strip() if isinstance(condition, str) and condition.strip() else None)
        fingerprints.append(fingerprint)

    unique_fingerprints = set(fingerprints)
    if len(unique_fingerprints) == 1:
        declared_conditions = {condition for condition in conditions if condition is not None}
        if len(declared_conditions) > 1:
            raise DeliveryError("one exact input fingerprint cannot use multiple declared condition labels")
        if declared_conditions and any(condition is None for condition in conditions):
            raise DeliveryError("exact-repeat conditions must be declared for every source run or omitted for all")
        return {
            "comparison_mode": "exact_repeat",
            "comparison_policy": EXACT_REPEAT_COMPARISON_POLICY,
            "comparison_fingerprint": fingerprints[0],
            "condition_count": 1,
        }

    if any(condition is None for condition in conditions):
        raise DeliveryError(
            "different comparison input fingerprints require an explicit condition on every source run"
        )
    condition_fingerprints: dict[str, set[str]] = {}
    fingerprint_conditions: dict[str, set[str]] = {}
    for condition, fingerprint in zip(conditions, fingerprints, strict=True):
        assert condition is not None
        condition_fingerprints.setdefault(condition, set()).add(fingerprint)
        fingerprint_conditions.setdefault(fingerprint, set()).add(condition)
    if any(len(values) != 1 for values in condition_fingerprints.values()):
        raise DeliveryError("one declared condition cannot identify multiple exact input fingerprints")
    if any(len(values) != 1 for values in fingerprint_conditions.values()):
        raise DeliveryError("one exact input fingerprint cannot use multiple declared condition labels")
    return {
        "comparison_mode": "declared_condition_matrix",
        "comparison_policy": DECLARED_CONDITION_COMPARISON_POLICY,
        "comparison_fingerprint": None,
        "condition_count": len(condition_fingerprints),
    }


def delivery_acquisition_fingerprint(run_dir: str | Path) -> str:
    """Fingerprint camera pose/profile and render timebase/resolution for comparable previews."""
    run_dir = Path(run_dir).expanduser().resolve(strict=False)
    digest = hashlib.sha256()
    for relative in DELIVERY_ACQUISITION_INPUT_FILES:
        path = run_dir / relative
        if not path.is_file():
            raise DeliveryError(f"delivery acquisition input is missing: {path}")
        try:
            payload = read_json(path)
        except (OSError, ValueError) as exc:
            raise DeliveryError(f"delivery acquisition input is invalid: {path}") from exc
        if not isinstance(payload, dict):
            raise DeliveryError(f"delivery acquisition input must be an object: {path}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(json_canonical_bytes(payload))
        digest.update(b"\0")
    return digest.hexdigest()


def delivery_group_contract(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate comparison semantics plus one shared camera/render acquisition context."""
    comparison = delivery_comparison_contract(runs)
    acquisition_fingerprints = {
        delivery_acquisition_fingerprint(str(row.get("run_dir") or ""))
        for row in runs
    }
    if len(acquisition_fingerprints) != 1:
        raise DeliveryError(
            "delivery source runs do not share one camera/render acquisition context; "
            "use a separate case route for different timebase, resolution, duration, or camera pose"
        )
    frame_count_profiles = {
        tuple(sorted(delivery_rgb_frame_count_profile(str(row.get("run_dir") or "")).items()))
        for row in runs
    }
    if len(frame_count_profiles) != 1:
        raise DeliveryError("delivery source runs do not share one RGB frame-count profile")
    return {
        **comparison,
        "acquisition_fingerprint": next(iter(acquisition_fingerprints)),
        "acquisition_policy": "exact_camera_and_render_config_v1",
        "rgb_frame_count_profile": dict(next(iter(frame_count_profiles))),
    }


def delivery_rgb_frame_count_profile(run_dir: str | Path) -> dict[str, int]:
    run_dir = Path(run_dir).expanduser().resolve(strict=False)
    views = ordered_delivery_views(run_dir)
    if not views:
        raise DeliveryError(f"delivery source run has no camera views: {run_dir}")
    profile: dict[str, int] = {}
    for view in views:
        frame_count = read_optional_json(run_dir / "views" / view / "meta.json").get("frame_count_rgb")
        if isinstance(frame_count, bool) or not isinstance(frame_count, int) or frame_count <= 1:
            raise DeliveryError(f"delivery RGB frame count is invalid for {view}: {run_dir}")
        profile[view] = frame_count
    return profile


class ArtifactManager:
    """Owns the canonical M2.3 run layout.

    Existing harness files at the run root remain for backward compatibility,
    but dataset consumers should use manifest.json plus inputs/, passes/,
    sync/, and logs/.
    """

    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir)

    def prepare(self) -> None:
        for rel in (
            "inputs",
            "passes/rgb/frames",
            "passes/data",
            "sync",
            "logs",
        ):
            (self.run_dir / rel).mkdir(parents=True, exist_ok=True)

    def write_inputs(
        self,
        *,
        case_spec: dict[str, Any],
        scene_spec: dict[str, Any],
        camera_plan: dict[str, Any],
        render_config: dict[str, Any],
    ) -> None:
        self.prepare()
        write_json(self.run_dir / "inputs" / "case.json", case_spec)
        write_json(self.run_dir / "inputs" / "scene.json", scene_spec)
        write_json(self.run_dir / "inputs" / "camera.json", camera_plan)
        write_json(self.run_dir / "inputs" / "render_config.json", render_config)

    def finalize(
        self,
        *,
        run_id: str,
        case_id: str,
        mode: str,
        seed: int,
        camera_plan: dict[str, Any],
        render_config: dict[str, Any],
        rgb_video_source: str | Path | None = None,
    ) -> dict[str, Any]:
        self.prepare()
        fps = int(render_config.get("fps") or 60)
        frame_count = infer_frame_count(self.run_dir, fps)
        self.copy_rgb_pass(rgb_video_source)
        self.copy_data_pass()
        self.write_sync_payloads(camera_plan=camera_plan, fps=fps, frame_count=frame_count)
        self.copy_logs()
        sync_report = validate_world_model_run(self.run_dir, write=True)
        views = {}
        for view in camera_plan.get("views", []):
            if not isinstance(view, dict) or not view.get("camera_id"):
                continue
            camera_id = str(view["camera_id"])
            view_dir = self.run_dir / "views" / camera_id
            view_artifacts = {
                name: f"views/{camera_id}/{filename}"
                for name, filename in (
                    ("rgb", "rgb.mp4"),
                    ("depth", "depth.exr"),
                    ("depth_preview", "depth_preview.mp4"),
                    ("segmentation", "segmentation.exr"),
                    ("segmentation_preview", "segmentation_preview.mp4"),
                    ("meta", "meta.json"),
                )
                if (view_dir / filename).is_file()
            }
            for name, dirname in (
                ("depth_frames_dir", "depth_frames"),
                ("segmentation_frames_dir", "segmentation_frames"),
            ):
                if (view_dir / dirname).is_dir():
                    view_artifacts[name] = f"views/{camera_id}/{dirname}/"
            raw_segmentation_dir = next(
                (
                    path
                    for path in (
                        self.run_dir / "logs" / "native_combined" / "segmentation_raw" / camera_id,
                        self.run_dir / "logs" / "native_data" / "segmentation_raw" / camera_id,
                    )
                    if path.is_dir()
                ),
                None,
            )
            if raw_segmentation_dir is not None:
                view_artifacts["raw_segmentation_dir"] = f"{raw_segmentation_dir.relative_to(self.run_dir).as_posix()}/"
            views[camera_id] = view_artifacts
        intermediate_candidates = {
            "case_spec": "case_spec.json",
            "scene_spec": "scene_spec.json",
            "asset_resolution": "asset_resolution.json",
            "scene_layout": "scene_layout.json",
            "runtime_actor_placement": "runtime_actor_placement.json",
            "studio_runtime_scene": "studio_runtime_scene.json",
            "solver_trajectory": "solver_trajectory.json",
            "sampling_map": "sampling_map.json",
            "trajectory": "trajectory.json",
            "contact_events": "contact_events.json",
            "camera_trajectory": "camera_trajectory.json",
            "map_report": "map_report.json",
            "sensor_state": "sensor_state.json",
            "render_sync_report": "render_sync_report.json",
            "run_readiness": "run_readiness.json",
            "quality_report": "quality_report.json",
            "raw_solver_capture": next(
                (
                    path
                    for path in (
                        "logs/native_combined/cpp_physics_capture.json",
                        "logs/native_rgb/cpp_physics_capture.json",
                        "logs/native_data/cpp_physics_capture.json",
                    )
                    if (self.run_dir / path).is_file()
                ),
                "logs/native_rgb/cpp_physics_capture.json",
            ),
        }
        manifest = {
            "schema_version": WORLD_MODEL_SCHEMA_VERSION,
            "artifact_schema_version": "2.3",
            "run_id": run_id,
            "case_id": case_id,
            "mode": mode,
            "seed": seed,
            "deterministic": True,
            "ue_renderer_only": True,
            "harness_identity": harness_git_identity(),
            "layout": {
                "inputs": "inputs/",
                "rgb_pass": "passes/rgb/",
                "data_pass": "passes/data/",
                "sync": "sync/",
                "logs": "logs/",
            },
            "render_config": render_config,
            "camera_count": len(camera_plan.get("views", [])) if isinstance(camera_plan, dict) else 0,
            "views": views,
            "intermediates": {
                name: rel
                for name, rel in intermediate_candidates.items()
                if (self.run_dir / rel).is_file()
            },
            "frame_count": frame_count,
            "sync_report": "sync/sync_report.json",
            "sync_status": sync_report["status"],
            "artifacts": {
                "case": "inputs/case.json",
                "scene": "inputs/scene.json",
                "camera": "inputs/camera.json",
                "render_config": "inputs/render_config.json",
                "rgb_video": "passes/rgb/video.mp4",
                "depth": "passes/data/depth.exr",
                "segmentation": "passes/data/segmentation.exr",
                "mask": "passes/data/segmentation.exr",
                "mask_format": "openexr",
                "instance": "passes/data/instance.json",
                "camera_trajectory": "sync/camera_trajectory.json",
                "physics_trace": "sync/physics_trace.json",
                "sync_report": "sync/sync_report.json",
            },
        }
        write_json(self.run_dir / "manifest.json", manifest)
        return manifest

    def copy_rgb_pass(self, source: str | Path | None) -> None:
        target = self.run_dir / "passes" / "rgb" / "video.mp4"
        source_path = Path(source) if source else self.run_dir / "video.mp4"
        if source_path.exists() and source_path.stat().st_size > 0:
            link_or_copy(source_path, target)

    def copy_data_pass(self) -> None:
        first = first_view_dir(self.run_dir)
        if not first:
            return
        copy_if_present(first / "depth.exr", self.run_dir / "passes" / "data" / "depth.exr")
        segmentation = self.run_dir / "passes" / "data" / "segmentation.exr"
        copy_if_present(first / "segmentation.exr", segmentation)
        meta = read_optional_json(first / "meta.json")
        write_json(
            self.run_dir / "passes" / "data" / "instance.json",
            {
                "schema_version": "instance_mask.v2.3",
                "source_view": first.name,
                "segmentation_type": meta.get("segmentation_type", "instance"),
                "instance_level": bool(meta.get("instance_level")),
                "instance_count": int(meta.get("instance_count") or 0),
                "instance_mapping": meta.get("instance_mapping") or [],
            },
        )

    def write_sync_payloads(self, *, camera_plan: dict[str, Any], fps: int, frame_count: int) -> None:
        camera_source = self.run_dir / "camera_trajectory.json"
        if camera_source.exists() and camera_source.stat().st_size > 0:
            copy_if_present(camera_source, self.run_dir / "sync" / "camera_trajectory.json")
        else:
            write_json(
                self.run_dir / "sync" / "camera_trajectory.json",
                camera_trajectory_from_plan(camera_plan, frame_count=frame_count, fps=fps),
            )
        trajectory = read_optional_list(self.run_dir / "trajectory.json")
        contacts = read_optional_list(self.run_dir / "contact_events.json")
        render_config = read_optional_json(self.run_dir / "inputs" / "render_config.json")
        write_json(
            self.run_dir / "sync" / "physics_trace.json",
            build_physics_trace(trajectory, contacts, fps=fps, timebase=render_config.get("timebase")),
        )

    def copy_logs(self) -> None:
        for name in ("runner_stdout.json", "runner_stderr.json", "ue_backend_report.json", "local_ue_runner_report.json"):
            copy_if_present(self.run_dir / name, self.run_dir / "logs" / name)
        for source_name, target_name in (
            ("logs/native_combined/ue_process_stdout.log", "ue_combined_stdout.log"),
            ("logs/native_combined/ue_process_stderr.log", "ue_combined_stderr.log"),
            ("logs/native_rgb/ue_process_stdout.log", "ue_rgb_stdout.log"),
            ("logs/native_rgb/ue_process_stderr.log", "ue_rgb_stderr.log"),
            ("logs/native_data/ue_process_stdout.log", "ue_data_stdout.log"),
            ("logs/native_data/ue_process_stderr.log", "ue_data_stderr.log"),
            ("ue_native_output/ue_process_stdout.log", "ue_stdout.log"),
            ("ue_native_output/ue_process_stderr.log", "ue_stderr.log"),
        ):
            copy_if_present(self.run_dir / source_name, self.run_dir / "logs" / target_name)

    def publish_videos(self, video_root: str | Path, *, case_id: str, backend: str) -> list[Path]:
        """Copy real MP4 outputs into one flat, searchable project directory."""
        video_root = Path(video_root)
        run_overall = self.publish_run_overall()
        views_root = self.run_dir / "views"
        preview_names = (("rgb.mp4", ""), ("depth_preview.mp4", "depth"), ("segmentation_preview.mp4", "segmentation"))
        view_sources = [
            ("__".join(part for part in (view_dir.name, modality) if part), view_dir / filename)
            for view_dir in sorted(views_root.iterdir())
            for filename, modality in preview_names
            if view_dir.is_dir() and is_mp4(view_dir / filename)
        ] if views_root.exists() else []
        overall_sources = [
            ("__".join(part for part in ("overall", modality if modality != "rgb" else "") if part), path)
            for modality, path in run_overall.items()
        ]
        sources = view_sources + overall_sources or [("main", self.run_dir / "video.mp4")]

        published: list[Path] = []
        run_group = safe_filename(self.run_dir.parent.name)
        prefix = "__".join(safe_filename(value) for value in (case_id, backend, run_group))
        for label, source in sources:
            if not is_mp4(source):
                continue
            target = video_root / f"{prefix}__{safe_filename(label)}.mp4"
            target.parent.mkdir(parents=True, exist_ok=True)
            link_or_copy(source, target)
            published.append(target)
        return published

    def publish_run_overall(self, *, ffmpeg: str = "ffmpeg") -> dict[str, Path]:
        """Create one five-camera review grid per available modality inside the run."""
        views = ordered_delivery_views(self.run_dir)
        if not views:
            return {}
        overall: dict[str, Path] = {}
        for modality, filename in DELIVERY_MODALITY_FILES.items():
            sources = [self.run_dir / "views" / view / filename for view in views]
            if not all(is_mp4(source) for source in sources):
                continue
            target = self.run_dir / "overall" / f"{modality}.mp4"
            if len(sources) == 1:
                link_or_copy(sources[0], target)
            else:
                render_video_grid(sources, target, columns=len(sources), rows=1, ffmpeg=ffmpeg)
            overall[modality] = target
        if overall:
            write_json(
                self.run_dir / "overall" / "manifest.json",
                {
                    "schema_version": "harness_run_overall_v1",
                    "views": views,
                    "layout": {
                        "columns": views,
                        "rows": 1,
                        "tile_resolution": [DELIVERY_REVIEW_TILE_WIDTH, DELIVERY_REVIEW_TILE_HEIGHT],
                    },
                    "videos": {
                        modality: {
                            "file": path.relative_to(self.run_dir).as_posix(),
                            "sha256": file_sha256(path),
                        }
                        for modality, path in overall.items()
                    },
                },
            )
        return overall


def publish_complete_case_delivery(
    runs: list[dict[str, Any]],
    destination: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    publication_tier: str = "unverified",
) -> dict[str, Any]:
    """Publish a reference-eligible run/view/modality delivery.

    This is the formal case-delivery path. One-off smoke previews continue to use
    ``ArtifactManager.publish_videos`` and are intentionally not review candidates.
    """
    return _publish_case_delivery(
        runs,
        destination,
        ffmpeg=ffmpeg,
        publication_tier=publication_tier,
        review_role="review_candidate",
        known_limitations=[],
        require_hard_gate=True,
    )


def publish_diagnostic_case_delivery(
    runs: list[dict[str, Any]],
    destination: str | Path,
    *,
    known_limitations: list[str],
    ffmpeg: str = "ffmpeg",
    publication_tier: str = "unverified",
) -> dict[str, Any]:
    """Publish complete diagnostic evidence without claiming reference readiness."""
    limitations = [str(value).strip() for value in known_limitations if str(value).strip()]
    if not limitations:
        raise DeliveryError("diagnostic case delivery requires at least one known limitation")
    if publication_tier == "reference":
        raise DeliveryError("diagnostic case delivery cannot use the reference publication tier")
    return _publish_case_delivery(
        runs,
        destination,
        ffmpeg=ffmpeg,
        publication_tier=publication_tier,
        review_role="diagnostic_probe",
        known_limitations=limitations,
        require_hard_gate=False,
    )


def _publish_case_delivery(
    runs: list[dict[str, Any]],
    destination: str | Path,
    *,
    ffmpeg: str,
    publication_tier: str,
    review_role: str,
    known_limitations: list[str],
    require_hard_gate: bool,
) -> dict[str, Any]:
    if publication_tier not in DELIVERY_PUBLICATION_TIERS:
        raise DeliveryError(f"unsupported publication tier: {publication_tier}")
    if len(runs) < COMPLETE_DELIVERY_MIN_SOURCE_RUNS:
        raise DeliveryError(
            f"a complete case delivery requires at least {COMPLETE_DELIVERY_MIN_SOURCE_RUNS} source runs"
        )
    normalized_runs: list[tuple[dict[str, Any], Path, str]] = []
    labels: set[str] = set()
    source_runs: set[Path] = set()
    for index, row in enumerate(runs, start=1):
        run_dir = Path(str(row.get("run_dir") or "")).expanduser().resolve(strict=False)
        label = safe_filename(row.get("label") or f"run_{index:02d}")
        if label in labels:
            raise DeliveryError(f"duplicate delivery run label: {label}")
        labels.add(label)
        if run_dir in source_runs:
            raise DeliveryError(f"duplicate resolved source run: {run_dir}")
        source_runs.add(run_dir)
        normalized_runs.append((row, run_dir, label))
    comparison_contract = delivery_group_contract(runs)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    prepared: list[dict[str, Any]] = []
    expected_views: list[str] | None = None
    expected_rgb_frame_counts: dict[str, int] | None = None
    videos: list[dict[str, Any]] = []
    for row, run_dir, label in normalized_runs:
        quality_path = run_dir / "quality_report.json"
        quality = read_optional_json(quality_path)
        hard_gate_passed = bool(quality.get("hard_gate_passed"))
        if require_hard_gate and not hard_gate_passed:
            raise DeliveryError(f"source run did not pass its hard gate: {run_dir}")
        if require_hard_gate:
            source_reports = quality.get("source_reports") or {}
            readiness = source_reports.get("run_readiness") or {}
            map_report = source_reports.get("map_report") or {}
            assets = source_reports.get("asset_resolution") or {}
            if readiness.get("backend") != "ue" or readiness.get("ue_render_real") is not True:
                raise DeliveryError(f"review candidate requires native UE RGB provenance: {run_dir}")
            if map_report.get("map_opened") is not True or map_report.get("package_match") is not True:
                raise DeliveryError(f"review candidate requires the requested UE map to be opened: {run_dir}")
            if int(assets.get("selected_count") or 0) <= int(assets.get("proxy_count") or 0):
                raise DeliveryError(f"review candidate requires at least one non-proxy 3D asset: {run_dir}")
            if assets.get("geometry_match") is False:
                raise DeliveryError(f"review candidate asset geometry does not match solver geometry: {run_dir}")

        views = ordered_delivery_views(run_dir)
        missing_required_views = (
            DELIVERY_REQUIRED_STATIC_CAMERAS | DELIVERY_REQUIRED_MOVING_CAMERAS
        ).difference(views)
        if missing_required_views:
            raise DeliveryError(
                f"complete delivery is missing required camera views "
                f"{','.join(sorted(missing_required_views))}: {run_dir}"
            )
        minimum_camera_count = COMPLETE_DELIVERY_MIN_STATIC_CAMERAS + COMPLETE_DELIVERY_MIN_MOVING_CAMERAS
        if len(views) < minimum_camera_count:
            raise DeliveryError(
                f"complete delivery requires at least {minimum_camera_count} camera views: {run_dir}"
            )
        camera_motion = ((quality.get("camera_motion") or {}).get("views") or {})
        moving_views: list[str] = []
        static_views: list[str] = []
        rgb_frame_counts: dict[str, int] = {}
        for view in views:
            motion = camera_motion.get(view)
            if not isinstance(motion, dict):
                raise DeliveryError(f"camera motion truth is missing for {view}: {run_dir}")
            mode = str(motion.get("camera_mode") or motion.get("mode") or "")
            moving = motion.get("moving")
            motion_frame_count = motion.get("frame_count")
            meta_frame_count = read_optional_json(run_dir / "views" / view / "meta.json").get("frame_count_rgb")
            if (
                isinstance(motion_frame_count, bool)
                or not isinstance(motion_frame_count, int)
                or motion_frame_count <= 1
                or isinstance(meta_frame_count, bool)
                or not isinstance(meta_frame_count, int)
                or meta_frame_count <= 1
                or motion_frame_count != meta_frame_count
            ):
                raise DeliveryError(
                    f"camera motion/meta RGB frame counts must match and exceed one for {view}: "
                    f"motion={motion_frame_count!r}, meta={meta_frame_count!r}, run={run_dir}"
                )
            rgb_frame_counts[view] = meta_frame_count
            if mode in DELIVERY_DYNAMIC_CAMERA_MODES and moving is True:
                moving_views.append(view)
            elif mode in DELIVERY_STATIC_CAMERA_MODES and moving is False:
                static_views.append(view)
            else:
                raise DeliveryError(
                    f"camera mode/moving truth is inconsistent for {view}: "
                    f"mode={mode!r}, moving={moving!r}, run={run_dir}"
                )
        if len(moving_views) < COMPLETE_DELIVERY_MIN_MOVING_CAMERAS:
            raise DeliveryError(
                f"complete delivery requires at least {COMPLETE_DELIVERY_MIN_MOVING_CAMERAS} "
                f"verified moving cameras: {run_dir}"
            )
        if len(static_views) < COMPLETE_DELIVERY_MIN_STATIC_CAMERAS:
            raise DeliveryError(
                f"complete delivery requires at least {COMPLETE_DELIVERY_MIN_STATIC_CAMERAS} "
                f"verified static cameras: {run_dir}"
            )
        missing_static_truth = DELIVERY_REQUIRED_STATIC_CAMERAS.difference(static_views)
        missing_moving_truth = DELIVERY_REQUIRED_MOVING_CAMERAS.difference(moving_views)
        if missing_static_truth:
            raise DeliveryError(
                f"required static camera roles are not verified: "
                f"{','.join(sorted(missing_static_truth))}: {run_dir}"
            )
        if missing_moving_truth:
            raise DeliveryError(
                f"required moving camera roles are not verified: "
                f"{','.join(sorted(missing_moving_truth))}: {run_dir}"
            )
        if expected_rgb_frame_counts is None:
            expected_rgb_frame_counts = rgb_frame_counts
        elif rgb_frame_counts != expected_rgb_frame_counts:
            raise DeliveryError(
                f"delivery source runs do not share one RGB frame-count profile: "
                f"expected={expected_rgb_frame_counts}, actual={rgb_frame_counts}, run={run_dir}"
            )
        if expected_views is None:
            expected_views = views
        elif set(views) != set(expected_views):
            raise DeliveryError(
                f"comparison runs must expose the same camera set: expected={expected_views}, actual={views}, run={run_dir}"
            )

        source_truth: dict[str, dict[str, dict[str, Any]]] = {}
        for view in expected_views:
            view_dir = run_dir / "views" / view
            source_truth[view] = {
                "depth": require_exr_sequence(
                    view_dir / "depth_frames",
                    run_dir=run_dir,
                    modality="depth",
                    view=view,
                    expected_frame_count=rgb_frame_counts[view],
                ),
                "segmentation": require_exr_sequence(
                    view_dir / "segmentation_frames",
                    run_dir=run_dir,
                    modality="segmentation",
                    view=view,
                    expected_frame_count=rgb_frame_counts[view],
                ),
            }
            for modality, frames_name in (
                ("depth", "depth_frames"),
                ("segmentation", "segmentation_frames"),
            ):
                delivery_frames = (
                    destination
                    / "variants"
                    / label
                    / modality
                    / safe_filename(view)
                    / "frames"
                )
                for frame in sorted((view_dir / frames_name).glob("*.exr")):
                    link_or_copy(frame, delivery_frames / frame.name)
                source_truth[view][modality]["delivery_path"] = (
                    delivery_frames.relative_to(destination).as_posix()
                )
            for modality, filename in DELIVERY_MODALITY_FILES.items():
                source = view_dir / filename
                if not is_mp4(source):
                    raise DeliveryError(f"missing valid {modality} preview for {label}/{view}: {source}")
                target = delivery_variant_preview_path(
                    destination,
                    label=label,
                    view=view,
                    modality=modality,
                )
                link_or_copy(source, target)
                videos.append(
                    {
                        "file": target.relative_to(destination).as_posix(),
                        "sha256": file_sha256(target),
                        "role": "per_view",
                        "run": label,
                        "view": view,
                        "modality": modality,
                    }
                )
        run_overall: dict[str, str] = {}
        for modality in DELIVERY_MODALITY_FILES:
            sources = [
                delivery_variant_preview_path(
                    destination,
                    label=label,
                    view=view,
                    modality=modality,
                )
                for view in expected_views
            ]
            target = destination / "variants" / label / "overall" / f"{modality}.mp4"
            render_video_grid(
                sources,
                target,
                columns=len(expected_views),
                rows=1,
                ffmpeg=ffmpeg,
            )
            relative = target.relative_to(destination).as_posix()
            run_overall[modality] = relative
            videos.append(
                {
                    "file": relative,
                    "sha256": file_sha256(target),
                    "role": "run_overall",
                    "run": label,
                    "modality": modality,
                }
            )
        prepared.append(
            {
                "label": label,
                "source_run": str(run_dir),
                "source_quality_report": str(quality_path),
                "source_quality_report_sha256": file_sha256(quality_path),
                "hard_gate_passed": hard_gate_passed,
                "source_reference_ready": (
                    (((quality.get("source_reports") or {}).get("run_readiness") or {}).get("reference_ready"))
                    is True
                ),
                "technical_score": (quality.get("ranking") or {}).get("technical_score"),
                "moving_views": moving_views,
                "static_views": static_views,
                "source_truth": source_truth,
                "overall": run_overall,
                "acquisition_fingerprint": delivery_acquisition_fingerprint(run_dir),
                **{
                    key: row[key]
                    for key in ("attempt", "lighting_preset", "condition", "comparison_fingerprint")
                    if row.get(key) is not None
                },
            }
        )

    assert expected_views is not None
    if DELIVERY_CASE_COMPARISON_CAMERA not in expected_views:
        raise DeliveryError(
            f"case comparison camera is missing: {DELIVERY_CASE_COMPARISON_CAMERA}"
        )
    overall: dict[str, str] = {}
    for modality in DELIVERY_MODALITY_FILES:
        sources = [
            delivery_variant_preview_path(
                destination,
                label=run["label"],
                view=DELIVERY_CASE_COMPARISON_CAMERA,
                modality=modality,
            )
            for run in prepared
        ]
        target = destination / "overall" / f"{modality}.mp4"
        render_video_grid(
            sources,
            target,
            columns=len(prepared),
            rows=1,
            ffmpeg=ffmpeg,
        )
        relative = target.relative_to(destination).as_posix()
        overall[modality] = relative
        videos.append(
            {
                "file": relative,
                "sha256": file_sha256(target),
                "role": "overall",
                "modality": modality,
            }
        )

    all_sources_reference_ready = all(run["source_reference_ready"] for run in prepared)
    if publication_tier == "reference" and not all_sources_reference_ready:
        raise DeliveryError("reference publication requires reference-ready source runs")
    delivery = {
        "schema_version": COMPLETE_DELIVERY_SCHEMA_VERSION,
        "review_role": review_role,
        "publication_tier": publication_tier,
        "reference_ready": (
            require_hard_gate
            and publication_tier == "reference"
            and all(run["hard_gate_passed"] for run in prepared)
            and all_sources_reference_ready
        ),
        "known_limitations": known_limitations,
        "contract": {
            "minimum_source_run_count": COMPLETE_DELIVERY_MIN_SOURCE_RUNS,
            "minimum_camera_count": COMPLETE_DELIVERY_MIN_STATIC_CAMERAS + COMPLETE_DELIVERY_MIN_MOVING_CAMERAS,
            "minimum_static_camera_count": COMPLETE_DELIVERY_MIN_STATIC_CAMERAS,
            "minimum_moving_camera_count": COMPLETE_DELIVERY_MIN_MOVING_CAMERAS,
            "requires_verified_static_camera": True,
            "requires_verified_moving_camera": True,
            "requires_hard_gate_passed": require_hard_gate,
            "per_view_modalities": list(DELIVERY_MODALITY_FILES),
            "canonical_sensor_truth": (
                "RGB is the native UE MP4; depth and segmentation truth are source-run per-frame OpenEXR "
                "sequences, while their delivery MP4s are review previews"
            ),
            "modality_truth": {
                "rgb": {"format": "mp4", "source": "native_ue_capture"},
                "depth": {"format": "openexr_sequence", "delivery_preview_format": "mp4"},
                "segmentation": {"format": "openexr_sequence", "delivery_preview_format": "mp4"},
            },
            "run_overall_video_count_per_modality": 1,
            "case_overall_video_count_per_modality": 1,
            "case_overall_comparison_view": DELIVERY_CASE_COMPARISON_CAMERA,
            "variant_directory": "variants/",
            "variant_layout": {
                "rgb": "variants/<variant>/rgb/<camera>.mp4",
                "depth": "variants/<variant>/depth/<camera>/{preview.mp4,frames/*.exr}",
                "segmentation": "variants/<variant>/segmentation/<camera>/{preview.mp4,frames/*.exr}",
                "overall": "variants/<variant>/overall/{rgb,depth,segmentation}.mp4",
            },
            **comparison_contract,
            "comparison_policy_description": (
                "exact-input runs are repeat evidence; differing inputs are allowed only when each exact input "
                "has one explicit condition label, and are causal variants rather than winner candidates"
            ),
        },
        "layout": {
            "variant_root": "variants/",
            "columns": [run["label"] for run in prepared],
            "rows": [DELIVERY_CASE_COMPARISON_CAMERA],
            "comparison_view": DELIVERY_CASE_COMPARISON_CAMERA,
            "tile_resolution": [DELIVERY_REVIEW_TILE_WIDTH, DELIVERY_REVIEW_TILE_HEIGHT],
        },
        "runs": prepared,
        "views": expected_views,
        "run_overall": {run["label"]: run["overall"] for run in prepared},
        "overall": overall,
        "videos": videos,
    }
    write_json(destination / "delivery_manifest.json", delivery)
    return delivery


def delivery_variant_preview_path(
    destination: str | Path,
    *,
    label: str,
    view: str,
    modality: str,
) -> Path:
    root = Path(destination) / "variants" / safe_filename(label)
    camera = safe_filename(view)
    if modality == "rgb":
        return root / "rgb" / f"{camera}.mp4"
    if modality in {"depth", "segmentation"}:
        return root / modality / camera / "preview.mp4"
    raise DeliveryError(f"unsupported delivery modality: {modality}")


def ordered_delivery_views(run_dir: Path) -> list[str]:
    views_root = run_dir / "views"
    available = {path.name for path in views_root.iterdir() if path.is_dir()} if views_root.is_dir() else set()
    camera = read_optional_json(run_dir / "inputs" / "camera.json")
    planned = [
        str(row.get("camera_id"))
        for row in (camera.get("views") or [])
        if isinstance(row, dict) and row.get("camera_id") in available
    ]
    return planned + sorted(available.difference(planned))


def json_canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def require_exr_sequence(
    path: Path,
    *,
    run_dir: Path,
    modality: str,
    view: str,
    expected_frame_count: int,
) -> dict[str, Any]:
    try:
        provenance = exr_sequence_provenance(path, relative_to=run_dir)
    except DeliveryError as exc:
        raise DeliveryError(f"canonical {modality} EXR sequence is invalid for {view}: {run_dir}: {exc}") from exc
    if provenance["frame_count"] != expected_frame_count:
        raise DeliveryError(
            f"canonical {modality} EXR frame count does not match RGB for {view}: "
            f"exr={provenance['frame_count']}, rgb={expected_frame_count}, run={run_dir}"
        )
    return provenance


def exr_sequence_provenance(path: Path, *, relative_to: Path | None = None) -> dict[str, Any]:
    """Return a reproducible filename-and-content hash for one real EXR sequence."""
    if path.is_symlink() or not path.is_dir():
        raise DeliveryError(f"EXR sequence directory is missing or a symlink: {path}")
    frames = sorted(path.glob("*.exr"), key=lambda item: item.name)
    if not frames or any(frame.is_symlink() or not frame.is_file() for frame in frames):
        raise DeliveryError(f"EXR sequence contains no frames or an unsafe frame: {path}")
    digest = hashlib.sha256()
    for frame in frames:
        digest.update(frame.name.encode("utf-8"))
        digest.update(b"\0")
        with frame.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    sequence_path = path.relative_to(relative_to).as_posix() if relative_to is not None else str(path)
    return {
        "path": sequence_path,
        "format": "openexr_sequence",
        "frame_count": len(frames),
        "aggregate_sha256": digest.hexdigest(),
        "hash_algorithm": "sha256(filename_nul_content_nul)",
    }


def render_video_grid(
    sources: list[Path],
    target: Path,
    *,
    columns: int,
    rows: int,
    ffmpeg: str = "ffmpeg",
) -> None:
    if not sources or columns < 1 or rows < 1 or len(sources) != columns * rows:
        raise DeliveryError("video grid dimensions do not match the source count")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.tmp{target.suffix}")
    temporary.unlink(missing_ok=True)
    filters = [
        f"[{index}:v]setpts=PTS-STARTPTS,"
        f"scale={DELIVERY_REVIEW_TILE_WIDTH}:{DELIVERY_REVIEW_TILE_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={DELIVERY_REVIEW_TILE_WIDTH}:{DELIVERY_REVIEW_TILE_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black[v{index}]"
        for index in range(len(sources))
    ]
    layout = "|".join(
        f"{(index % columns) * DELIVERY_REVIEW_TILE_WIDTH}_{(index // columns) * DELIVERY_REVIEW_TILE_HEIGHT}"
        for index in range(len(sources))
    )
    filters.append(
        "".join(f"[v{index}]" for index in range(len(sources)))
        + f"xstack=inputs={len(sources)}:layout={layout}:shortest=1[vout]"
    )
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for source in sources:
        command.extend(("-i", str(source)))
    command.extend(
        (
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-an",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(temporary),
        )
    )
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        temporary.unlink(missing_ok=True)
        raise DeliveryError(f"failed to render overall video {target}: {exc}") from exc
    if completed.returncode != 0 or not is_mp4(temporary):
        temporary.unlink(missing_ok=True)
        raise DeliveryError(
            f"ffmpeg failed to render overall video {target}: {completed.stderr[-2000:].strip()}"
        )
    temporary.replace(target)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def harness_git_identity() -> dict[str, Any]:
    """Capture the source revision that produced a run without mutating Git."""
    root = Path(__file__).resolve().parents[2]
    identity: dict[str, Any] = {"repository": str(root), "branch": None, "commit": None, "dirty": None}
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v2", "--branch", "--untracked-files=normal"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return identity
    if completed.returncode != 0:
        return identity
    lines = completed.stdout.splitlines()
    for line in lines:
        if line.startswith("# branch.oid "):
            identity["commit"] = line.removeprefix("# branch.oid ")
        elif line.startswith("# branch.head "):
            identity["branch"] = line.removeprefix("# branch.head ")
    identity["dirty"] = any(line and not line.startswith("# ") for line in lines)
    return identity


def infer_frame_count(run_dir: Path, fps: int) -> int:
    sync = read_optional_json(run_dir / "render_sync_report.json")
    stats = sync.get("per_camera_statistics") if isinstance(sync.get("per_camera_statistics"), dict) else {}
    counts = [int(row.get("frame_count_rgb") or 0) for row in stats.values() if isinstance(row, dict)]
    if counts:
        return max(counts)
    trajectory = read_optional_list(run_dir / "trajectory.json")
    if trajectory:
        return len(trajectory)
    return max(1, fps)


def first_view_dir(run_dir: Path) -> Path | None:
    views_root = run_dir / "views"
    if not views_root.exists():
        return None
    for path in sorted(views_root.iterdir()):
        if path.is_dir():
            return path
    return None


def copy_if_present(source: Path, target: Path) -> None:
    if source.exists() and source.is_file() and source.stat().st_size > 0:
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.casefold() in {".json", ".log"}:
            shutil.copyfile(source, target)
        else:
            link_or_copy(source, target)


def link_or_copy(source: Path, target: Path) -> None:
    """Reuse file data on one filesystem; copy only when hard links are unavailable."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.exists() and source.samefile(target):
            return
    except OSError:
        pass
    if target.exists() or target.is_symlink():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def is_mp4(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 8:
        return False
    with path.open("rb") as handle:
        return handle.read(8)[4:8] == b"ftyp"


def safe_filename(value: Any) -> str:
    text = str(value).strip()
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in text) or "unnamed"


def read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = read_json(path)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def read_optional_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        value = read_json(path)
    except Exception:
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
