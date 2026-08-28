from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.capability_planner import CapabilityPlanner
from tools.capability_verifier import CapabilityVerifier


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_CASES = (
    {
        "case_id": "rigid_contract_integrity",
        "prompt": "A declarative rigid-body object graph with trajectory evidence.",
        "assertions": [{"id": "integrity", "type": "trajectory_integrity"}],
    },
    {
        "case_id": "rigid_contract_state_delta",
        "prompt": "A declarative rigid-body object graph with explicit state assertions.",
        "assertions": [{"id": "x_changed", "type": "state_delta", "object_id": "body_a", "field": "position_m.x", "operator": ">=", "value": 0.1}],
    },
    {
        "case_id": "rigid_contract_event",
        "prompt": "A declarative rigid-body object graph with explicit event assertions.",
        "assertions": [{"id": "contact_recorded", "type": "event_exists", "event": "contact", "objects": ["body_a", "body_b"]}],
    },
)


def run_closed_loop_demo(root: str | Path = ROOT, *, timestamp: str | None = None) -> dict[str, Any]:
    root = Path(root)
    timestamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / "runs" / "physics_contract_closed_loop" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    planner = CapabilityPlanner(root / "config" / "harness_capability_profile.json")
    verifier = CapabilityVerifier()
    results = []
    for item in CONTRACT_CASES:
        case_dir = run_dir / item["case_id"]
        case_dir.mkdir(parents=True, exist_ok=True)
        plan = planner.plan(item["prompt"])
        execution = simulate_execution_trace(item["case_id"], plan, item["assertions"])
        report = verifier.verify(plan, execution)
        write_json(case_dir / "capability_plan.json", plan)
        write_json(case_dir / "execution_trace.json", execution)
        write_json(case_dir / "verifier_report.json", report)
        results.append(
            {
                "case_id": item["case_id"],
                "primary_capability_id": plan["primary_capability_id"],
                "scene_domain": plan["scene_domain"],
                "capability_ready": report["capability_ready"],
                "reference_video_ready": report["reference_video_ready"],
                "artifact_tier": report["artifact_tier"],
                "primary_failure_type": report["primary_failure_type"],
            }
        )
    summary = {
        "schema_version": "physics_contract_closed_loop_summary_v2",
        "run_id": timestamp,
        "mode": "generic_contract_trace",
        "ue_render_executed": False,
        "case_count": len(results),
        "capability_ready_count": sum(1 for result in results if result["capability_ready"]),
        "reference_video_ready_count": sum(1 for result in results if result["reference_video_ready"]),
    }
    write_json(run_dir / "summary.json", summary)
    write_json(run_dir / "case_results.json", {"schema_version": "physics_contract_case_results_v2", "cases": results})
    return {"run_dir": str(run_dir), "summary": summary, "cases": results}


def simulate_execution_trace(case_id: str, plan: dict[str, Any], assertions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "capability_execution_trace_v1",
        "case_id": case_id,
        "capability_plan": {"primary_capability_id": plan["primary_capability_id"], "scene_domain": plan["scene_domain"]},
        "objects": [{"id": "body_a", "dynamic": True}, {"id": "body_b", "dynamic": True}],
        "trajectory": [
            {"frame": 0, "time_s": 0.0, "objects": {"body_a": state(0.0), "body_b": state(1.0)}, "contacts": []},
            {"frame": 1, "time_s": 0.1, "objects": {"body_a": state(0.2), "body_b": state(1.0)}, "contacts": [{"objects": ["body_a", "body_b"], "time_s": 0.1}]},
        ],
        "verification_assertions": assertions or [{"id": "integrity", "type": "trajectory_integrity"}],
        "render_evidence": {
            "source_type": "SIM_PROXY",
            "runtime_status": "completed",
            "video_available": False,
            "trajectory_available": True,
        },
    }


def state(x: float) -> dict[str, Any]:
    return {"position_m": [x, 0.0, 0.0], "velocity_m_s": [0.0, 0.0, 0.0], "rotation_deg": [0.0, 0.0, 0.0]}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
