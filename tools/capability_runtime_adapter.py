from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.core.physics_contract import execution_capability_id, infer_scene_domain
from tools.capability_planner import CapabilityPlanner, DEFAULT_PROFILE_PATH
from tools.capability_verifier import CapabilityVerifier


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR_CANDIDATES = ("ue_output", "debug_preview", "fallback_output")
VIDEO_CANDIDATES = ("video.mp4", "preview.mp4")


def verify_capability_run(
    run_dir: str | Path,
    *,
    output_dir: str | Path | None = None,
    profile_path: str | Path = DEFAULT_PROFILE_PATH,
    write: bool = True,
) -> dict[str, Any]:
    adapter = CapabilityRuntimeAdapter(profile_path=profile_path)
    result = adapter.verify_run(run_dir, output_dir=output_dir)
    if write:
        write_verification_artifacts(result)
    return result


class CapabilityRuntimeAdapter:
    """Convert existing runtime artifacts into the capability verifier trace contract."""

    def __init__(self, *, profile_path: str | Path = DEFAULT_PROFILE_PATH) -> None:
        self.planner = CapabilityPlanner(profile_path)
        self.verifier = CapabilityVerifier()

    def verify_run(self, run_dir: str | Path, *, output_dir: str | Path | None = None) -> dict[str, Any]:
        run_dir = Path(run_dir)
        if not run_dir.exists():
            raise FileNotFoundError(f"run directory does not exist: {run_dir}")
        resolved_output_dir = Path(output_dir) if output_dir else resolve_runtime_output_dir(run_dir)
        spec = read_json(run_dir / "spec.json")
        summary = read_json(resolved_output_dir / "summary.json")
        readiness = read_json(resolved_output_dir / "run_readiness.json")
        pass_manifest = read_json(resolved_output_dir / "render_pass_manifest.json")
        trajectory = read_json(resolved_output_dir / "trajectory.json", default=[])
        if not isinstance(trajectory, list):
            trajectory = []

        prompt = prompt_from_spec_or_summary(spec, summary, run_dir.name)
        plan = existing_plan_or_plan_prompt(spec, prompt, self.planner)
        execution = build_capability_execution_trace(
            run_dir=run_dir,
            output_dir=resolved_output_dir,
            spec=spec,
            summary=summary,
            readiness=readiness,
            pass_manifest=pass_manifest,
            trajectory=trajectory,
            capability_plan=plan,
        )
        verifier_report = self.verifier.verify(plan, execution)
        diagnosis_md = render_capability_runtime_diagnosis(run_dir, resolved_output_dir, plan, verifier_report, execution)
        return {
            "schema_version": "capability_runtime_verification_bundle_v1",
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "output_dir": str(resolved_output_dir),
            "capability_plan": plan,
            "execution_trace": execution,
            "verifier_report": verifier_report,
            "diagnosis_md": diagnosis_md,
            "artifact_paths": {
                "capability_plan": str(run_dir / "capability_plan.json"),
                "capability_execution_trace": str(run_dir / "capability_execution_trace.json"),
                "capability_verifier": str(run_dir / "capability_verifier.json"),
                "capability_diagnosis": str(run_dir / "capability_diagnosis.md"),
            },
        }


def build_capability_execution_trace(
    *,
    run_dir: Path,
    output_dir: Path,
    spec: dict[str, Any],
    summary: dict[str, Any],
    readiness: dict[str, Any],
    pass_manifest: dict[str, Any],
    trajectory: list[dict[str, Any]],
    capability_plan: dict[str, Any],
) -> dict[str, Any]:
    objects = normalize_objects(spec, trajectory)
    normalized_trajectory = normalize_trajectory(trajectory)
    return {
        "schema_version": "capability_execution_trace_v1",
        "case_id": run_dir.name,
        "source_type": classify_source_type(output_dir, summary),
        "capability_plan": {
            "primary_capability_id": capability_plan.get("primary_capability_id"),
            "case_family": capability_plan.get("case_family"),
        },
        "environment": normalize_environment(spec),
        "objects": objects,
        "trajectory": normalized_trajectory,
        "verification_assertions": list(spec.get("verification_assertions") or []),
        "render_evidence": render_evidence(output_dir, summary, readiness, pass_manifest, normalized_trajectory),
        "source_artifacts": {
            "spec": str(run_dir / "spec.json"),
            "summary": str(output_dir / "summary.json"),
            "trajectory": str(output_dir / "trajectory.json"),
            "run_readiness": str(output_dir / "run_readiness.json"),
            "render_pass_manifest": str(output_dir / "render_pass_manifest.json"),
        },
    }


def normalize_objects(spec: dict[str, Any], trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scene_objects = list(((spec.get("scene") or {}).get("objects") or []))
    if not scene_objects:
        runtime_scene = spec.get("runtime_scene") if isinstance(spec.get("runtime_scene"), dict) else {}
        scene_objects = list(runtime_scene.get("objects") or [])
    trajectory_initial = first_frame_objects(trajectory)
    ids_from_trajectory = sorted(str(item) for item in trajectory_initial)
    by_id: dict[str, dict[str, Any]] = {}
    for item in scene_objects:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        by_id[str(item["id"])] = item
    for object_id in ids_from_trajectory:
        by_id.setdefault(object_id, {"id": object_id, "dynamic": True})

    objects = []
    for object_id in sorted(by_id):
        raw = by_id[object_id]
        initial_state = normalize_initial_state(raw, trajectory_initial.get(object_id) or {})
        dynamic = bool(raw.get("dynamic", infer_dynamic(object_id, raw)))
        objects.append(
            {
                "id": object_id,
                "role": str(raw.get("role") or ""),
                "dynamic": dynamic,
                "asset_key": raw.get("asset_key") or raw.get("asset_id") or raw.get("asset_name"),
                "initial_state": initial_state,
                "physics": {
                    "gravity_enabled": bool((raw.get("physics") or {}).get("gravity_enabled", dynamic)),
                    "collision_enabled": bool((raw.get("physics") or {}).get("collision_enabled", True)),
                    "mass_kg": number_or_default((raw.get("physics") or {}).get("mass_kg") or raw.get("mass_kg"), 1.0),
                },
            }
        )
    return objects


def normalize_initial_state(raw: dict[str, Any], first_state: dict[str, Any]) -> dict[str, Any]:
    raw_state = raw.get("initial_state") if isinstance(raw.get("initial_state"), dict) else {}
    velocity = (
        raw.get("initial_velocity_m_s")
        or raw_state.get("linear_velocity_m_s")
        or raw_state.get("velocity_m_s")
        or [0.0, 0.0, 0.0]
    )
    angular = (
        raw.get("initial_angular_velocity_deg_s")
        or raw_state.get("angular_velocity_deg_s")
        or raw_state.get("angular_velocity")
        or [0.0, 0.0, 0.0]
    )
    return {
        "position_m": vec3(raw.get("initial_position_m") or raw_state.get("position_m") or first_state.get("position_m") or first_state.get("position")),
        "linear_velocity_m_s": vec3(velocity),
        "angular_velocity_deg_s": vec3(angular),
    }


def normalize_trajectory(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for index, frame in enumerate(trajectory):
        if not isinstance(frame, dict):
            continue
        raw_objects = frame.get("objects") if isinstance(frame.get("objects"), dict) else {}
        objects = {str(object_id): normalize_state(state) for object_id, state in raw_objects.items() if isinstance(state, dict)}
        normalized.append(
            {
                "frame": int(frame.get("frame", index) or index),
                "time_s": float(frame.get("time_s") or frame.get("time") or 0.0),
                "objects": objects,
                "contacts": normalize_contacts(frame.get("contacts") or []),
            }
        )
    return normalized


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "position_m": vec3(state.get("position_m") or state.get("position")),
        "velocity_m_s": velocity_m_s(state),
        "rotation_deg": vec3(state.get("rotation_deg") or state.get("rotation_degrees")),
        "source": state.get("source"),
        "asset_key": state.get("asset_key"),
    }


def normalize_contacts(contacts: list[Any]) -> list[dict[str, Any]]:
    normalized = []
    for item in contacts:
        if not isinstance(item, dict):
            continue
        objects = [str(value) for value in item.get("objects") or []]
        if len(objects) < 2:
            continue
        contact = {
            "objects": objects,
            "time_s": float(item.get("time_s") or item.get("time") or 0.0),
            "frame": item.get("frame"),
            "method": item.get("method"),
        }
        if isinstance(item.get("gap_cm"), (int, float)):
            contact["gap_cm"] = float(item["gap_cm"])
        normalized.append(contact)
    return normalized


def render_evidence(
    output_dir: Path,
    summary: dict[str, Any],
    readiness: dict[str, Any],
    pass_manifest: dict[str, Any],
    trajectory: list[dict[str, Any]],
) -> dict[str, Any]:
    pass_status = pass_manifest.get("passes") if isinstance(pass_manifest.get("passes"), dict) else {}
    return {
        "source_type": classify_source_type(output_dir, summary),
        "runtime_status": "failed" if readiness.get("passed") is False and not trajectory else "completed",
        "video_available": any((output_dir / name).is_file() and (output_dir / name).stat().st_size > 0 for name in VIDEO_CANDIDATES),
        "video_required": True,
        "trajectory_available": bool(trajectory),
        "contact_events_available": any(frame.get("contacts") for frame in trajectory),
        "camera_trajectory_available": (output_dir / "camera_trajectories.json").is_file() or "camera_trajectory" in (pass_manifest.get("sync") or {}),
        "render_pass_manifest_available": bool(pass_manifest),
        "rgb_available": pass_available(pass_status, "rgb"),
        "depth_available": pass_available(pass_status, "depth"),
        "normal_available": pass_available(pass_status, "normal"),
        "audio_available": pass_available(pass_status, "audio"),
        "readiness_reference_ready": readiness.get("reference_ready"),
        "readiness_physics_ready": readiness.get("physics_ready"),
        "readiness_visual_ready": readiness.get("visual_ready"),
        "trajectory_source": trajectory_source(summary),
    }


def existing_plan_or_plan_prompt(spec: dict[str, Any], prompt: str, planner: CapabilityPlanner) -> dict[str, Any]:
    existing = spec.get("capability_plan") if isinstance(spec.get("capability_plan"), dict) else {}
    if existing.get("schema_version") == "capability_plan_v2":
        return existing
    plan = planner.plan(prompt)
    plan["primary_capability_id"] = execution_capability_id(spec)
    plan["scene_domain"] = infer_scene_domain(spec)
    if existing:
        plan["source_capability_plan"] = existing
    return plan


def prompt_from_spec_or_summary(spec: dict[str, Any], summary: dict[str, Any], fallback: str) -> str:
    for value in (
        spec.get("prompt"),
        spec.get("expanded_prompt"),
        ((summary.get("studio_scene_spec") or {}).get("prompt") if isinstance(summary.get("studio_scene_spec"), dict) else None),
    ):
        if isinstance(value, str) and value.strip():
            return value
    return fallback


def resolve_runtime_output_dir(run_dir: Path) -> Path:
    for name in OUTPUT_DIR_CANDIDATES:
        candidate = run_dir / name
        if (candidate / "trajectory.json").is_file() or (candidate / "summary.json").is_file():
            return candidate
    if (run_dir / "trajectory.json").is_file() or (run_dir / "summary.json").is_file():
        return run_dir
    raise FileNotFoundError(f"no UE/fallback runtime output found under {run_dir}")


def write_verification_artifacts(result: dict[str, Any]) -> None:
    paths = result["artifact_paths"]
    write_json(Path(paths["capability_plan"]), result["capability_plan"])
    write_json(Path(paths["capability_execution_trace"]), result["execution_trace"])
    write_json(Path(paths["capability_verifier"]), result["verifier_report"])
    Path(paths["capability_diagnosis"]).write_text(result["diagnosis_md"], encoding="utf-8")


def render_capability_runtime_diagnosis(
    run_dir: Path,
    output_dir: Path,
    plan: dict[str, Any],
    report: dict[str, Any],
    execution: dict[str, Any],
) -> str:
    evidence = execution.get("render_evidence") or {}
    failures = report.get("failure_modes") or []
    lines = [
        "# Capability Runtime Verification",
        "",
        f"- run_id: `{run_dir.name}`",
        f"- output_dir: `{output_dir.name}`",
        f"- capability: `{plan.get('primary_capability_id')}`",
        f"- source_type: `{evidence.get('source_type')}`",
        f"- capability_ready: `{report.get('capability_ready')}`",
        f"- reference_video_ready: `{report.get('reference_video_ready')}`",
        f"- primary_failure_type: `{report.get('primary_failure_type')}`",
        f"- trajectory_frames: `{len(execution.get('trajectory') or [])}`",
        f"- contact_events_available: `{evidence.get('contact_events_available')}`",
        "",
        "## Diagnosis",
        "",
        f"- root_cause: {report.get('diagnosis', {}).get('root_cause')}",
        f"- repair_suggestion: {report.get('diagnosis', {}).get('repair_suggestion')}",
    ]
    if failures:
        lines.extend(["", "## Failure Modes", ""])
        for failure in failures:
            lines.append(f"- `{failure.get('failure_type')}`: {failure.get('reason')}")
    return "\n".join(lines) + "\n"


def classify_source_type(output_dir: Path, summary: dict[str, Any]) -> str:
    source = trajectory_source(summary).casefold()
    if summary.get("native_ue") is True or "ue" in source or "adp_cpp_runtime_driver" in source:
        return "UE"
    if "fallback" in output_dir.name.casefold() or "debug" in output_dir.name.casefold() or "preview" in source:
        return "FALLBACK"
    if source.startswith("analytic_"):
        return "SIM_PROXY"
    return "UNKNOWN"


def trajectory_source(summary: dict[str, Any]) -> str:
    physics_capture = summary.get("physics_capture") if isinstance(summary.get("physics_capture"), dict) else {}
    return str(physics_capture.get("trajectory_source") or summary.get("trajectory_source") or "")


def normalize_environment(spec: dict[str, Any]) -> dict[str, Any]:
    environment = ((spec.get("physics_control") or {}).get("environment_physics") or {}) if isinstance(spec.get("physics_control"), dict) else {}
    gravity = value_from_control(environment.get("gravity"), 9.81)
    return {
        "gravity_m_s2": abs(number_or_default(gravity, 9.81)),
        "fixed_delta_time": number_or_default(value_from_control(environment.get("fixed_delta_time"), 1.0 / 24.0), 1.0 / 24.0),
    }


def infer_dynamic(object_id: str, raw: dict[str, Any]) -> bool:
    if raw.get("body_type") is not None:
        return str(raw.get("body_type")).casefold() == "dynamic"
    if raw.get("kinematic") is not None:
        return not bool(raw.get("kinematic"))
    return bool(raw.get("dynamic", True))


def first_frame_objects(trajectory: list[dict[str, Any]]) -> dict[str, Any]:
    if not trajectory or not isinstance(trajectory[0], dict):
        return {}
    objects = trajectory[0].get("objects")
    return objects if isinstance(objects, dict) else {}


def pass_available(pass_status: dict[str, Any], name: str) -> bool:
    item = pass_status.get(name) if isinstance(pass_status, dict) else None
    return isinstance(item, dict) and item.get("status") == "available"


def read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def value_from_control(value: Any, default: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return default if value is None else value


def number_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    try:
        return [float(padded[0]), float(padded[1]), float(padded[2])]
    except (TypeError, ValueError):
        return [0.0, 0.0, 0.0]


def velocity_m_s(state: dict[str, Any]) -> list[float]:
    if isinstance(state.get("velocity_m_s"), (list, tuple)):
        return vec3(state.get("velocity_m_s"))
    if isinstance(state.get("velocity_cm_s"), (list, tuple)):
        return [round(value / 100.0, 6) for value in vec3(state.get("velocity_cm_s"))]
    return [0.0, 0.0, 0.0]
