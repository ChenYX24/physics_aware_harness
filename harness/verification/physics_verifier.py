from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.core.artifact_schema import read_json, write_json
from harness.core.capability import canonical_capability_id
from harness.core.verifier_schema import verifier_report
from harness.verification.agent_action_verifier import verify_agent_action
from harness.verification.brittle_fracture_verifier import verify_brittle_fracture
from harness.verification.bounce_verifier import verify_bounce
from harness.verification.constraint_verifier import verify_constraint_motion
from harness.verification.contact_causality_verifier import verify_contact_causality
from harness.verification.diagnosis import repair_suggestion
from harness.verification.ordered_contact_verifier import verify_ordered_contact_propagation
from harness.verification.elastic_constraint_verifier import verify_elastic_constraint
from harness.verification.elastic_launch_verifier import verify_elastic_launch
from harness.verification.falling_verifier import verify_falling
from harness.verification.impulse_chain_verifier import verify_impulse_chain
from harness.verification.mass_ratio_verifier import verify_mass_ratio
from harness.verification.magnetic_verifier import verify_magnetic
from harness.verification.particle_cache_verifier import verify_particle_cache
from harness.verification.projectile_verifier import verify_projectile
from harness.verification.ramp_verifier import verify_ramp
from harness.verification.rolling_verifier import verify_rolling
from harness.verification.sliding_verifier import verify_sliding
from harness.verification.spin_verifier import verify_spin
from harness.verification.wind_verifier import verify_wind


class PhysicsVerifier:
    def verify_run_dir(self, run_dir: str | Path, *, write: bool = False) -> dict[str, Any]:
        run_dir = Path(run_dir)
        case_spec = read_json(run_dir / "case_spec.json")
        if canonical_capability_id(str(case_spec["capability_id"])) == "fluid_particle_dynamics":
            report = verify_fluid_run(case_spec, run_dir)
            if write:
                write_json(run_dir / "harness_verifier.json", report)
                write_json(run_dir / "verifier_report.json", report)
                write_json(
                    run_dir / "verifier.json",
                    {
                        "reference_ready": False,
                        "physics_ready": report["status"] == "pass",
                        "harness_verifier": report,
                    },
                )
            return report
        ue_backend_report_path = run_dir / "ue_backend_report.json"
        if ue_backend_report_path.exists():
            ue_backend_report = read_json(ue_backend_report_path)
            if ue_backend_report.get("status") != "completed":
                report = verifier_report(
                    case_id=str(case_spec["case_id"]),
                    capability_id=str(case_spec["capability_id"]),
                    status="fail",
                    failure_type=str(ue_backend_report.get("failure_code") or "F6_RUNTIME_OR_RENDER_FAILURE"),
                    first_failure={
                        "object_id": "ue_backend",
                        "frame": 0,
                        "time": 0.0,
                        "metric": str(ue_backend_report.get("phase") or "ue_backend"),
                        "value": str(ue_backend_report.get("failure_message") or "UE backend failed"),
                    },
                    evidence=[{"type": "ue_backend_report", "path": "ue_backend_report.json"}],
                    repair_suggestions=[str(ue_backend_report.get("next_required_action") or "Fix UE backend configuration and rerun.")],
                    artifact_completeness=artifact_completeness(resolve_output_dir(run_dir), []),
                )
                report["failure_category"] = ue_backend_report.get("failure_category")
                if write:
                    write_json(run_dir / "harness_verifier.json", report)
                    write_json(run_dir / "verifier_report.json", report)
                    write_json(run_dir / "verifier.json", {"reference_ready": False, "harness_verifier": report})
                return report
        output_dir = resolve_output_dir(run_dir)
        trajectory_path = output_dir / "trajectory.json"
        trajectory = read_json(trajectory_path) if trajectory_path.exists() else []
        if not trajectory and (run_dir / "trajectory.json").exists():
            trajectory = read_json(run_dir / "trajectory.json")
        report = self.verify(case_spec, trajectory, output_dir=output_dir)
        if write:
            write_json(run_dir / "harness_verifier.json", report)
            write_json(run_dir / "verifier_report.json", report)
            write_json(run_dir / "verifier.json", {"reference_ready": report["status"] == "pass", "harness_verifier": report})
        return report

    def verify(self, case_spec: dict[str, Any], trajectory: list[dict[str, Any]], *, output_dir: str | Path | None = None) -> dict[str, Any]:
        capability_id = canonical_capability_id(str(case_spec["capability_id"]))
        if capability_id == "rigid_body_contact_causality":
            failure_type, first_failure, evidence = verify_contact_causality(case_spec, trajectory)
        elif capability_id == "sequential_contact_propagation":
            failure_type, first_failure, evidence = verify_ordered_contact_propagation(case_spec, trajectory)
        elif capability_id == "rigid_body_gravity_collision":
            failure_type, first_failure, evidence = verify_falling(case_spec, trajectory)
        elif capability_id == "ramp_sliding_friction":
            failure_type, first_failure, evidence = verify_ramp(case_spec, trajectory)
        elif capability_id == "projectile_gravity_motion":
            failure_type, first_failure, evidence = verify_projectile(case_spec, trajectory)
        elif capability_id == "bounce_restitution_ball":
            failure_type, first_failure, evidence = verify_bounce(case_spec, trajectory)
        elif capability_id == "rolling_friction_ball":
            failure_type, first_failure, evidence = verify_rolling(case_spec, trajectory)
        elif capability_id == "sliding_crate_friction":
            failure_type, first_failure, evidence = verify_sliding(case_spec, trajectory)
        elif capability_id == "force_field_wind_drift":
            failure_type, first_failure, evidence = verify_wind(case_spec, trajectory)
        elif capability_id == "magnetic_force_field":
            failure_type, first_failure, evidence = verify_magnetic(case_spec, trajectory)
        elif capability_id == "mass_ratio_momentum_transfer":
            failure_type, first_failure, evidence = verify_mass_ratio(case_spec, trajectory)
        elif capability_id == "angular_damping_spin_decay":
            failure_type, first_failure, evidence = verify_spin(case_spec, trajectory)
        elif capability_id == "agent_rigidbody_action_coupling":
            failure_type, first_failure, evidence = verify_agent_action(case_spec, trajectory)
        elif capability_id == "constraint_distance_pendulum_motion":
            failure_type, first_failure, evidence = verify_constraint_motion(case_spec, trajectory)
        elif capability_id == "constraint_momentum_transfer":
            failure_type, first_failure, evidence = verify_impulse_chain(case_spec, trajectory)
        elif capability_id == "elastic_energy_launch":
            failure_type, first_failure, evidence = verify_elastic_launch(case_spec, trajectory)
        elif capability_id == "elastic_constraint_rebound":
            failure_type, first_failure, evidence = verify_elastic_constraint(case_spec, trajectory)
        elif capability_id == "brittle_impact_fracture":
            failure_type, first_failure, evidence = verify_brittle_fracture(case_spec, trajectory)
        else:
            failure_type, first_failure, evidence = "F7_runtime_artifact_incomplete", {"object_id": capability_id, "frame": 0, "time": 0, "metric": "unsupported_capability", "value": capability_id}, []
        return verifier_report(
            case_id=str(case_spec["case_id"]),
            capability_id=capability_id,
            status="pass" if failure_type is None else "fail",
            failure_type=failure_type,
            first_failure=first_failure,
            evidence=evidence,
            repair_suggestions=repair_suggestion(failure_type),
            artifact_completeness=artifact_completeness(output_dir, trajectory),
        )


def resolve_output_dir(run_dir: Path) -> Path:
    for name in ("fallback_output", "ue_output", "genesis_sph_output", "debug_preview"):
        candidate = run_dir / name
        if candidate.exists():
            return candidate
    return run_dir


def artifact_completeness(output_dir: str | Path | None, trajectory: list[dict[str, Any]]) -> dict[str, Any]:
    if output_dir is None:
        return {"trajectory": bool(trajectory), "summary": False, "run_readiness": False, "render_pass_manifest": False, "render_sync_report": False}
    output_dir = Path(output_dir)
    run_dir = output_dir.parent if output_dir.name.endswith("_output") else output_dir
    return {
        "trajectory": bool(trajectory),
        "summary": (output_dir / "summary.json").exists(),
        "run_readiness": (output_dir / "run_readiness.json").exists(),
        "render_pass_manifest": (output_dir / "render_pass_manifest.json").exists(),
        "render_manifest": (output_dir / "render_manifest.json").exists(),
        "render_sync_report": (run_dir / "render_sync_report.json").exists(),
        "contact_events_file": (output_dir / "contact_events.json").exists(),
        "contact_events": any(frame.get("contacts") for frame in trajectory),
    }


def verify_fluid_run(case_spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    candidates = (run_dir / "particle_cache.json", run_dir / "genesis_sph_output" / "particle_cache.json")
    cache_path = next((path for path in candidates if path.is_file()), None)
    cache = read_json(cache_path) if cache_path else {}
    particle_report = verify_particle_cache(cache, root=cache_path.parent if cache_path else run_dir)
    first = (particle_report.get("failures") or [None])[0]
    failure_type = str(first["code"]) if isinstance(first, dict) else None
    first_failure = None
    if isinstance(first, dict):
        first_failure = {
            "object_id": "fluid_particles",
            "frame": int(first.get("frame") or 0),
            "time": None,
            "metric": str(first.get("code") or "particle_cache"),
            "value": first.get("value"),
        }
    output_dir = resolve_output_dir(run_dir)
    checks = particle_report.get("checks") if isinstance(particle_report.get("checks"), dict) else {}
    completeness = {
        "particle_cache": cache_path is not None,
        "particle_count": int(checks.get("particle_count") or 0),
        "frame_count": int(checks.get("frame_count") or 0),
        "surface_sequence": bool(checks.get("frame_count")) and checks.get("surface_frame_count") == checks.get("frame_count"),
        "summary": (output_dir / "summary.json").is_file(),
        "run_readiness": (output_dir / "run_readiness.json").is_file(),
        "render_pass_manifest": (output_dir / "render_pass_manifest.json").is_file(),
        "render_manifest": (output_dir / "render_manifest.json").is_file(),
        "video": (run_dir / "video.mp4").is_file() and (run_dir / "video.mp4").stat().st_size > 0,
        "trajectory": (run_dir / "trajectory.json").is_file(),
        "contact_events_file": (run_dir / "contact_events.json").is_file(),
        "contact_events_available": False,
    }
    return verifier_report(
        case_id=str(case_spec["case_id"]),
        capability_id="fluid_particle_dynamics",
        status="pass" if particle_report["status"] == "pass" else "fail",
        failure_type=failure_type,
        first_failure=first_failure,
        evidence=[{"type": "particle_cache", "path": str(cache_path.relative_to(run_dir))}] if cache_path else [],
        repair_suggestions=[] if failure_type is None else ["Inspect particle_cache.json and rerun the Genesis SPH case after correcting the first failed invariant."],
        artifact_completeness=completeness,
    )
