from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from harness.core.artifact_schema import read_json, write_json


@dataclass(frozen=True)
class ExecutionProfile:
    """One small interface for the capture cost/quality contract."""

    name: str
    width: int
    height: int
    render_fps: int
    physics_hz: int
    artifact_eligibility: str
    purpose: str

    def environment(self) -> dict[str, str]:
        return {
            "SIM_STUDIO_UE_WIDTH": str(self.width),
            "SIM_STUDIO_UE_HEIGHT": str(self.height),
            "SIM_STUDIO_UE_FPS": str(self.render_fps),
            "SIM_STUDIO_UE_PHYSICS_HZ": str(self.physics_hz),
        }


EXECUTION_PROFILES: dict[str, ExecutionProfile] = {
    "smoke": ExecutionProfile(
        name="smoke",
        width=640,
        height=360,
        render_fps=24,
        physics_hz=120,
        artifact_eligibility="diagnostic_only",
        purpose="Low-resolution technical preflight.",
    ),
    "candidate": ExecutionProfile(
        name="candidate",
        width=1920,
        height=1080,
        render_fps=24,
        physics_hz=120,
        artifact_eligibility="review_candidate",
        purpose="Full-HD review candidate.",
    ),
    "local_preview": ExecutionProfile(
        name="local_preview",
        width=1280,
        height=720,
        render_fps=24,
        physics_hz=120,
        artifact_eligibility="local_preview",
        purpose="HD local preview with physics, camera, and semantic verification.",
    ),
    "publish": ExecutionProfile(
        name="publish",
        width=3840,
        height=2160,
        render_fps=24,
        physics_hz=120,
        artifact_eligibility="publish_candidate",
        purpose="4K rerender after a candidate is explicitly kept.",
    ),
}


def execution_profile(name: str) -> ExecutionProfile:
    try:
        return EXECUTION_PROFILES[str(name).strip().casefold()]
    except KeyError as exc:
        raise ValueError(f"unknown execution profile: {name}") from exc


def execution_profile_names() -> tuple[str, ...]:
    """Return the single registered profile set shared by every adapter."""
    return tuple(EXECUTION_PROFILES)


def write_execution_reports(
    run_dir: str | Path,
    profile: ExecutionProfile,
    *,
    wall_seconds: float,
    status: str,
) -> dict[str, Any]:
    """Persist the profile and normalized throughput without changing run truth."""
    run_dir = Path(run_dir)
    profile_payload = {
        "schema_version": "harness_execution_profile_v1",
        **asdict(profile),
    }
    write_json(run_dir / "execution_profile.json", profile_payload)

    render_config = optional_json(run_dir / "inputs" / "render_config.json")
    observation_plan = optional_json(run_dir / "observation_plan.json")
    frame_count = profile_frame_count(run_dir, render_config)
    actual_views = (
        list(render_config["views"])
        if isinstance(render_config.get("views"), list)
        else [
            str(camera["camera_id"])
            for camera in observation_plan.get("cameras") or []
            if isinstance(camera, dict) and camera.get("camera_id")
        ]
    )
    actual_passes = (
        list(render_config["passes"])
        if isinstance(render_config.get("passes"), list)
        else list(observation_plan.get("modalities") or [])
    )
    work_units = frame_count * len(actual_views) * len(actual_passes)
    native_timing, native_summary = native_timing_for_run(run_dir)
    measured_total = positive_float(native_timing.get("total_seconds")) or max(0.0, float(wall_seconds))
    capture_seconds = positive_float(native_timing.get("capture_seconds"))
    efficiency = {
        "schema_version": "harness_efficiency_report_v1",
        "profile": profile.name,
        "status": status,
        "artifact_eligibility": profile.artifact_eligibility,
        "resolution": [
            int(render_config.get("width") or profile.width),
            int(render_config.get("height") or profile.height),
        ],
        "frame_count": frame_count,
        "view_count": len(actual_views),
        "modality_count": len(actual_passes),
        "camera_modality_frames": work_units,
        "solver_pass_count": solver_pass_count(render_config, run_dir),
        "timing_seconds": {
            "setup": positive_float(native_timing.get("setup_seconds")),
            "capture": capture_seconds,
            "encode": positive_float(native_timing.get("encode_seconds")),
            "native_total": positive_float(native_timing.get("total_seconds")),
            "wall_total": round(max(0.0, float(wall_seconds)), 6),
        },
        "throughput": {
            "camera_modality_frames_per_capture_second": (
                round(work_units / capture_seconds, 6) if capture_seconds > 0 else None
            ),
            "camera_modality_frames_per_total_second": (
                round(work_units / measured_total, 6) if measured_total > 0 else None
            ),
        },
        "native_summary": str(native_summary.relative_to(run_dir)) if native_summary else None,
        "promotion": promotion(profile, status),
    }
    write_json(run_dir / "efficiency_report.json", efficiency)
    return efficiency


def verified_run_status(run_dir: str | Path) -> str:
    """Return pass only when physics and the requested render contract both pass."""
    run_dir = Path(run_dir)
    verifier = optional_json(run_dir / "harness_verifier.json")
    render_sync = optional_json(run_dir / "render_sync_report.json")
    if verifier.get("status") != "pass" or render_sync.get("status") != "pass":
        return "fail"
    return "pass"


def promotion(profile: ExecutionProfile, status: str) -> dict[str, Any]:
    passed = status in {"completed", "pass"}
    if profile.name == "smoke":
        return {
            "eligible": passed,
            "next_profile": "candidate" if passed else None,
            "reason": "smoke_passed" if passed else "do_not_spend_full_capture_budget",
        }
    if profile.name == "candidate":
        return {
            "eligible": passed,
            "next_profile": "publish" if passed else None,
            "reason": "requires_explicit_keep_before_publish" if passed else "candidate_failed",
        }
    if profile.name == "local_preview":
        return {
            "eligible": False,
            "next_profile": None,
            "reason": "terminal_local_preview" if passed else "local_preview_failed",
        }
    return {"eligible": False, "next_profile": None, "reason": "terminal_profile"}


def profile_frame_count(run_dir: Path, render_config: dict[str, Any]) -> int:
    timebase = render_config.get("timebase") if isinstance(render_config.get("timebase"), dict) else {}
    for value in (timebase.get("canonical_frame_count"), render_config.get("frame_count")):
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            return parsed
    for meta_path in sorted((run_dir / "views").glob("*/meta.json")):
        meta = optional_json(meta_path)
        try:
            parsed = int(meta.get("frame_count_rgb") or 0)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            return parsed
    return 0


def native_timing_for_run(run_dir: Path) -> tuple[dict[str, Any], Path | None]:
    candidates = (
        run_dir / "logs" / "native_combined" / "summary.json",
        run_dir / "logs" / "native_rgb" / "summary.json",
        run_dir / "logs" / "native_data" / "summary.json",
    )
    aggregate: dict[str, float] = {}
    selected: Path | None = None
    for path in candidates:
        payload = optional_json(path)
        timing = payload.get("timing") if isinstance(payload.get("timing"), dict) else {}
        if not timing:
            continue
        selected = selected or path
        for key, value in timing.items():
            parsed = positive_float(value)
            if parsed > 0:
                aggregate[str(key)] = aggregate.get(str(key), 0.0) + parsed
    return aggregate, selected


def solver_pass_count(render_config: dict[str, Any], run_dir: Path) -> int:
    strategy = str(render_config.get("execution_strategy") or "")
    if strategy.startswith("genesis_") and "replay" in strategy:
        return 0
    if strategy == "single_process_shared_solver_multimodal" or (run_dir / "logs" / "native_combined").is_dir():
        return 1
    return sum((run_dir / "logs" / name).is_dir() for name in ("native_rgb", "native_data")) or 1


def optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def positive_float(value: Any) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return round(parsed, 6) if parsed > 0 else 0.0
