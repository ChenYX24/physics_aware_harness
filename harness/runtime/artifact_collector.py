from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.core.artifact_schema import runtime_summary, write_json
from harness.runtime.camera_planner import camera_plan_from_case_spec
from harness.runtime.render_pass_contract import verify_render_observability, write_render_contract_artifacts


def write_runtime_artifacts(
    run_dir: str | Path,
    *,
    case_spec: dict[str, Any],
    trajectory: list[dict[str, Any]],
    backend: str,
    requested_views: list[str] | None = None,
    render_passes: list[str] | None = None,
    camera_strategy: str = "bounds_auto_v1",
) -> Path:
    run_dir = Path(run_dir)
    output_dir = run_dir / "fallback_output" if backend == "fallback" else run_dir / f"{backend}_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_dir.name
    contact_events = extract_contact_events(trajectory)
    action_trace = extract_action_trace(trajectory)
    constraint_trace = extract_constraint_trace(trajectory)
    spring_events = extract_spring_events(trajectory)
    fracture_events = extract_fracture_events(trajectory)
    fragment_manifest = extract_fragments(trajectory)
    write_json(run_dir / "case_spec.json", case_spec)
    write_json(output_dir / "trajectory.json", trajectory)
    write_json(output_dir / "contact_events.json", contact_events)
    write_json(output_dir / "action_trace.json", action_trace)
    write_json(output_dir / "constraint_trace.json", constraint_trace)
    write_json(output_dir / "spring_events.json", spring_events)
    write_json(output_dir / "fracture_events.json", fracture_events)
    write_json(output_dir / "fragment_manifest.json", fragment_manifest)
    write_json(run_dir / "trajectory.json", trajectory)
    write_json(run_dir / "contact_events.json", contact_events)
    write_json(run_dir / "action_trace.json", action_trace)
    write_json(run_dir / "constraint_trace.json", constraint_trace)
    write_json(run_dir / "spring_events.json", spring_events)
    write_json(run_dir / "fracture_events.json", fracture_events)
    write_json(run_dir / "fragment_manifest.json", fragment_manifest)
    write_json(
        run_dir / "camera_trajectory.json",
        {
            "schema_version": "harness_camera_trajectory_v1",
            "available": False,
            "backend": backend,
            "reason": "fallback backend does not render a real camera trajectory",
            "frames": [],
        },
    )
    write_json(
        output_dir / "summary.json",
        {
            **runtime_summary(run_id, str(case_spec["case_id"]), str(case_spec["capability_id"]), backend),
            "trajectory_source": f"{backend}_deterministic_toy",
            "frames": len(trajectory),
            "contact_event_count": len(contact_events),
            "action_event_count": len(action_trace),
            "constraint_event_count": len(constraint_trace),
            "spring_event_count": len(spring_events),
            "fracture_event_count": len(fracture_events),
            "fragment_count": len(fragment_manifest),
            "runtime_boundary": "deterministic toy backend; not proof of native UE physics",
        },
    )
    camera_plan = camera_plan_from_case_spec(case_spec, requested_views=requested_views, camera_strategy=camera_strategy)
    manifest = write_render_contract_artifacts(
        run_dir,
        backend=backend,
        case_id=str(case_spec["case_id"]),
        camera_plan=camera_plan,
        render_passes=render_passes or ["rgb"],
        allow_placeholders=backend == "fallback",
        source=f"{backend}_contract_placeholder",
    )
    required_depth = "depth" in set(render_passes or [])
    observability = verify_render_observability(run_dir, require_multiview=bool(requested_views and len(requested_views) > 1), require_depth=required_depth, min_view_count=len(requested_views or ["overview"]))
    write_json(
        output_dir / "run_readiness.json",
        {
            "schema_version": "harness_run_readiness_v1",
            "reference_ready": False,
            "physics_ready": bool(trajectory),
            "visual_ready": False,
            "backend": backend,
            "case_id": case_spec["case_id"],
            **observability,
        },
    )
    write_json(
        run_dir / "run_readiness.json",
        {
            "schema_version": "harness_run_readiness_v1",
            "reference_ready": False,
            "physics_ready": bool(trajectory),
            "visual_ready": False,
            "backend": backend,
            "case_id": case_spec["case_id"],
            **observability,
        },
    )
    write_json(
        output_dir / "render_pass_manifest.json",
        {
            "schema_version": "render_pass_manifest_v1",
            "passes": {
                "rgb": {"status": "missing", "source_type": backend},
                "depth": {"status": "missing", "source_type": backend},
                "normal": {"status": "missing", "source_type": backend},
                "audio": {"status": "missing", "source_type": backend},
            },
            "sync": {"object_trajectory": "trajectory.json"},
        },
    )
    write_json(
        run_dir / "render_pass_manifest.json",
        manifest,
    )
    write_json(
        output_dir / "render_manifest.json",
        {
            "schema_version": "harness_render_manifest_v1",
            "backend": backend,
            "render_available": False,
            "reason": "fallback backend emits trajectory/contact artifacts only",
            "passes": [],
        },
    )
    write_json(
        run_dir / "render_manifest.json",
        {
            "schema_version": "harness_render_manifest_v1",
            "backend": backend,
            "render_available": False,
            "reason": "fallback backend emits trajectory/contact artifacts only",
            "passes": [],
        },
    )
    write_json(
        run_dir / "harness_artifact.json",
        {
            "schema_version": "harness_runtime_artifact_package_v1",
            "run_id": run_id,
            "case_id": case_spec["case_id"],
            "capability_id": case_spec["capability_id"],
            "backend": backend,
            "runtime_boundary": "fallback is deterministic toy trajectory, not native physics",
            "camera_plan_ready": observability["camera_plan_ready"],
            "multi_view_ready": observability["multi_view_ready"],
            "depth_ready": observability["depth_ready"],
            "render_pass_ready": observability["render_pass_ready"],
            "sync_ready": observability["sync_ready"],
            "view_count": observability["view_count"],
            "camera_ids": observability["camera_ids"],
            "paths": {
                "case_spec": "case_spec.json",
                "trajectory": "trajectory.json",
                "contact_events": "contact_events.json",
                "action_trace": "action_trace.json",
                "constraint_trace": "constraint_trace.json",
                "spring_events": "spring_events.json",
                "fracture_events": "fracture_events.json",
                "fragment_manifest": "fragment_manifest.json",
                "camera_trajectory": "camera_trajectory.json",
                "camera_plan": "camera_plan.json",
                "summary": f"{output_dir.name}/summary.json",
                "run_readiness": "run_readiness.json",
                "render_manifest": "render_manifest.json",
                "render_pass_manifest": "render_pass_manifest.json",
                "video": "video.mp4",
            },
        },
    )
    write_json(
        run_dir / "artifact_manifest.json",
        {
            "schema_version": "harness_artifact_manifest_v1",
            "run_id": run_id,
            "case_id": case_spec["case_id"],
            "backend": backend,
            "artifacts": {
                "case_spec": "case_spec.json",
                "trajectory": "trajectory.json",
                "contact_events": "contact_events.json",
                "action_trace": "action_trace.json",
                "constraint_trace": "constraint_trace.json",
                "spring_events": "spring_events.json",
                "fracture_events": "fracture_events.json",
                "fragment_manifest": "fragment_manifest.json",
                "camera_trajectory": "camera_trajectory.json",
                "camera_plan": "camera_plan.json",
                "summary": f"{output_dir.name}/summary.json",
                "run_readiness": "run_readiness.json",
                "render_manifest": "render_manifest.json",
                "render_pass_manifest": "render_pass_manifest.json",
                "video": "video.mp4",
                "views": "views",
            },
        },
    )
    return run_dir


def extract_contact_events(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for frame in trajectory:
        frame_id = int(frame.get("frame") or 0)
        time_s = float(frame.get("time_s") or 0.0)
        for contact in frame.get("contacts") or []:
            if not isinstance(contact, dict):
                continue
            row = dict(contact)
            row.setdefault("frame", frame_id)
            row.setdefault("time_s", time_s)
            events.append(row)
    return events


def extract_action_trace(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for frame in trajectory:
        frame_id = int(frame.get("frame") or 0)
        time_s = float(frame.get("time_s") or 0.0)
        for action in frame.get("actions") or []:
            if not isinstance(action, dict):
                continue
            row = dict(action)
            row.setdefault("frame", frame_id)
            row.setdefault("time_s", time_s)
            events.append(row)
    return events


def extract_constraint_trace(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for frame in trajectory:
        frame_id = int(frame.get("frame") or 0)
        time_s = float(frame.get("time_s") or 0.0)
        for constraint in frame.get("constraints") or []:
            if not isinstance(constraint, dict):
                continue
            row = dict(constraint)
            row.setdefault("frame", frame_id)
            row.setdefault("time_s", time_s)
            events.append(row)
    return events


def extract_spring_events(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for frame in trajectory:
        frame_id = int(frame.get("frame") or 0)
        time_s = float(frame.get("time_s") or 0.0)
        for event in (frame.get("spring_events") or frame.get("elastic_events") or []):
            if not isinstance(event, dict):
                continue
            row = dict(event)
            row.setdefault("frame", frame_id)
            row.setdefault("time_s", time_s)
            events.append(row)
    return events


def extract_fracture_events(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for frame in trajectory:
        frame_id = int(frame.get("frame") or 0)
        time_s = float(frame.get("time_s") or 0.0)
        for event in frame.get("fracture_events") or []:
            if not isinstance(event, dict):
                continue
            row = dict(event)
            row.setdefault("frame", frame_id)
            row.setdefault("time_s", time_s)
            events.append(row)
    return events


def extract_fragments(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fragments: dict[str, dict[str, Any]] = {}
    for frame in trajectory:
        frame_id = int(frame.get("frame") or 0)
        time_s = float(frame.get("time_s") or 0.0)
        for fragment in frame.get("fragments") or []:
            if not isinstance(fragment, dict):
                continue
            fragment_id = str(fragment.get("fragment_id") or f"fragment_{len(fragments)}")
            row = dict(fragment)
            row.setdefault("fragment_id", fragment_id)
            row.setdefault("first_observed_frame", frame_id)
            row.setdefault("first_observed_time_s", time_s)
            fragments.setdefault(fragment_id, row)
    return list(fragments.values())
