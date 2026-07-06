from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.core.artifact_schema import read_json, write_json
from harness.core.verifier_schema import verifier_report
from harness.verification.billiards_verifier import verify_billiards
from harness.verification.bounce_verifier import verify_bounce
from harness.verification.diagnosis import repair_suggestion
from harness.verification.domino_verifier import verify_domino
from harness.verification.falling_verifier import verify_falling
from harness.verification.mass_ratio_verifier import verify_mass_ratio
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
        capability_id = str(case_spec["capability_id"])
        if capability_id in {"rigid_body_contact_causality", "billiard_causality_compiler"}:
            failure_type, first_failure, evidence = verify_billiards(case_spec, trajectory)
        elif capability_id == "sequential_contact_propagation":
            failure_type, first_failure, evidence = verify_domino(case_spec, trajectory)
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
        elif capability_id == "mass_ratio_momentum_transfer":
            failure_type, first_failure, evidence = verify_mass_ratio(case_spec, trajectory)
        elif capability_id == "angular_damping_spin_decay":
            failure_type, first_failure, evidence = verify_spin(case_spec, trajectory)
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
    for name in ("fallback_output", "ue_output", "debug_preview"):
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
