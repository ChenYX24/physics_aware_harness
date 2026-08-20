from __future__ import annotations

import json
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Mapping


REVIEW_FEEDBACK_SCHEMA_VERSION = "harness_review_feedback_v1"
REVIEW_FEEDBACK_ENV = "SIM_HARNESS_REVIEW_FEEDBACK"
EXECUTION_EVIDENCE_SCHEMA_VERSION = "harness_execution_evidence_v1"

ISSUE_RULES: dict[str, dict[str, Any]] = {
    "classification_mixed": {
        "target": "comparison_gate",
        "requirement": "Reject comparisons that mix text generation, conditional repair, UE reconstruction, and benchmark continuation under one branch label.",
    },
    "prompt_not_fixed": {
        "target": "comparison_gate",
        "requirement": "All prompt-only providers and UE scene construction must use one canonical prompt verbatim.",
    },
    "missing_prompt_provenance": {
        "target": "lineage_gate",
        "requirement": "Every generated run must write prompt_lineage.json and bind every submitted prompt to one hashed stage.",
    },
    "asset_material_unrealistic": {
        "target": "appearance_prompt",
        "requirement": "Replace proxy geometry and flat materials with qualified real-scale assets and photorealistic materials.",
    },
    "background_unrealistic": {
        "target": "appearance_prompt",
        "requirement": "Replace synthetic or blockout backgrounds with a coherent photorealistic environment and lighting.",
    },
    "h3_overpreserves_ue": {
        "target": "appearance_prompt",
        "requirement": "The Refiner must visibly remove low-fidelity UE styling while preserving UE physics and camera truth.",
    },
    "refiner_breaks_physics": {
        "target": "preservation_prompt",
        "requirement": "Reject outputs that alter identity, count, contacts, trajectories, event timing, or final state.",
    },
    "object_identity_count_color": {
        "target": "preservation_prompt",
        "requirement": "Preserve every tracked object's identity, count, color, marking, scale, and temporal continuity.",
    },
    "billiards_table_topology": {
        "target": "appearance_prompt",
        "capability_ids": ["rigid_body_contact_causality"],
        "requirement": "Use a regulation six-pocket table with four corner pockets, two side pockets, jaws, liners, rails, cushions, and real cloth.",
    },
    "ue_render_quality": {
        "target": "source_quality_gate",
        "requirement": "UE source must meet the selected delivery profile for resolution, anti-aliasing, exposure, and temporal sampling before refinement.",
    },
    "motion_feels_synthetic": {
        "target": "source_quality_gate",
        "requirement": "UE motion must pass contact, penetration, cadence, acceleration, rolling/spin, damping, and settling checks before refinement.",
    },
    "ue_penetration": {
        "target": "source_quality_gate",
        "requirement": "Reject UE teachers with visible or geometric penetration before submitting a Refiner job.",
    },
    "prompt_teacher_mismatch": {
        "target": "comparison_gate",
        "requirement": "Reject any UE teacher whose objects, event, initial state, or terminal condition does not match the canonical prompt.",
    },
    "ue_physics_wrong": {
        "target": "source_quality_gate",
        "requirement": "Reject UE teachers whose contact order, forces, motion, constraints, or terminal state violate the CaseSpec.",
    },
    "missing_direct_repair": {
        "target": "repair_matrix_gate",
        "requirement": "A repair comparison must include a video-model branch conditioned on the original error video, not a prompt-only regeneration mislabeled as repair.",
    },
    "direct_repair_failed": {
        "target": "repair_matrix_gate",
        "requirement": "A conditioned repair remains a failure when the labeled anomaly persists; generated-file success cannot substitute for frame-window re-evaluation.",
    },
    "source_label_corrected": {
        "target": "curation_gate",
        "requirement": "Preserve the frozen source label as provenance, but require dense temporal identity/count evidence before disappearance, materialization, or pocket-event claims and publish any corrected diagnosis separately.",
    },
    "missing_splice": {
        "target": "repair_matrix_gate",
        "requirement": "A repair case is incomplete until the repaired frame window is inserted into the original timeline with the original audio.",
    },
    "missing_joint_crops": {
        "target": "multiview_gate",
        "requirement": "A joint multiview ablation must retain the joint output and deterministic per-view crops before comparison with separately refined views.",
    },
    "cross_view_inconsistent": {
        "target": "multiview_gate",
        "requirement": "Reject multiview outputs that change object identity, event timing, state, geometry, or final outcome across camera views.",
    },
    "missing_ue_benchmark": {
        "target": "benchmark_gate",
        "requirement": "A benchmark case must include the UE continuation and UE-plus-Refiner branches under the same frame and canonical prompt condition.",
    },
    "missing_official_score": {
        "target": "benchmark_gate",
        "requirement": "Do not report a benchmark result until its official evaluator, frozen protocol, denominator, and source hashes are recorded.",
    },
    "missing_model_matrix": {
        "target": "comparison_gate",
        "requirement": "The declared provider comparison is incomplete until every required provider has direct and UE-conditioned outputs under the same canonical prompt.",
    },
    "audio_problem": {
        "target": "splice_gate",
        "requirement": "Repair insertion must preserve the original audio stream and verify duration and synchronization after remux.",
    },
    "splice_inconsistent": {
        "target": "splice_gate",
        "requirement": "Reject repair insertion with visible camera, identity, geometry, color, lighting, motion, or temporal discontinuity at either boundary.",
    },
    "hard_gate_failed": {
        "target": "regression_gate",
        "requirement": "Preserve the manifest, failing gate names, metrics, and representative media as a negative regression before any artifact deletion.",
    },
    "legacy_unverified": {
        "target": "qualification_gate",
        "requirement": "Legacy or unverified media may be reviewed but cannot become physics truth, a positive regression, or a publication claim without revalidation.",
    },
    "fallback_not_ue": {
        "target": "solver_provenance_gate",
        "requirement": "Fallback media must remain labeled diagnostic and must never be promoted as UE, solver truth, or physics evidence.",
    },
}


def compile_review_feedback(
    payload: Mapping[str, Any],
    *,
    verified_execution_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if payload.get("schema_version") not in {
        "physics_harness_case_curation_decisions_v1",
        "physics_harness_case_curation_decisions_v2",
    }:
        raise ValueError("unsupported case-curation decision schema_version")
    decisions = payload.get("decisions")
    if not isinstance(decisions, Mapping):
        raise ValueError("case-curation decisions must be an object")
    case_index = payload.get("case_index") or {}
    if not isinstance(case_index, Mapping):
        raise ValueError("case_index must be an object")
    verified_execution_evidence = verified_execution_evidence or {}
    verified_execution_statuses = _validate_execution_evidence(verified_execution_evidence)
    sources: dict[str, dict[str, set[str]]] = {}
    feedback: list[dict[str, str]] = []
    case_lessons: list[dict[str, Any]] = []
    weekly_candidates: list[dict[str, Any]] = []
    positive_regressions: list[str] = []
    negative_regressions: list[str] = []
    quarantined_legacy: list[str] = []
    pending_evidence: list[str] = []
    pending_diagnosis: list[str] = []
    unknown_issue_ids: set[str] = set()
    for case_id, value in decisions.items():
        if not isinstance(value, Mapping):
            raise ValueError(f"decision must be an object: {case_id}")
        decision = str(value.get("decision") or "")
        if decision not in {"keep", "improve", "delete", "unreviewed"}:
            raise ValueError(f"unsupported decision for {case_id}: {decision}")
        issues = value.get("issues") or []
        if not isinstance(issues, list) or any(not isinstance(issue, str) for issue in issues):
            raise ValueError(f"issues must be a string list: {case_id}")
        for issue_id in issues:
            if issue_id not in ISSUE_RULES:
                unknown_issue_ids.add(issue_id)
                continue
            source = sources.setdefault(issue_id, {"case_ids": set(), "decisions": set()})
            source["case_ids"].add(str(case_id))
            source["decisions"].add(decision)
        text = str(value.get("feedback") or "").strip()
        if text:
            feedback.append({"case_id": str(case_id), "decision": decision, "text": text})
        artifact_decisions = value.get("artifacts") or {}
        if not isinstance(artifact_decisions, Mapping):
            raise ValueError(f"artifacts must be an object: {case_id}")
        if any(result not in {"keep", "rerun", "delete", "unreviewed"} for result in artifact_decisions.values()):
            raise ValueError(f"unsupported artifact decision: {case_id}")
        index_row = case_index.get(case_id) if isinstance(case_index.get(case_id), Mapping) else {}
        artifact_index = index_row.get("artifacts") if isinstance(index_row.get("artifacts"), Mapping) else {}
        kept_artifacts = []
        for artifact_id, result in artifact_decisions.items():
            if result != "keep":
                continue
            metadata = artifact_index.get(artifact_id)
            kept_artifacts.append({
                "artifact_id": str(artifact_id),
                **(dict(metadata) if isinstance(metadata, Mapping) else {}),
            })
        execution_status = str(index_row.get("execution_status") or "unknown")
        verified_execution_status = verified_execution_statuses.get(str(case_id))
        lesson = {
            "case_id": str(case_id),
            "title": str(index_row.get("title") or case_id),
            "decision": decision,
            "category": index_row.get("category"),
            "collection": index_row.get("collection"),
            "execution_status": execution_status,
            "verified_execution_status": verified_execution_status,
            "case_route": index_row.get("case_route"),
            "issue_ids": sorted(issues),
            "feedback": text,
            "auto_diagnose": bool(value.get("autoFix", True)),
            "artifact_decisions": {str(key): str(result) for key, result in artifact_decisions.items()},
        }
        case_lessons.append(lesson)
        if decision == "keep":
            weekly_candidates.append({
                "case_id": str(case_id),
                "title": lesson["title"],
                "category": lesson["category"],
                "collection": lesson["collection"],
                "execution_status": execution_status,
                "case_route": lesson["case_route"],
                "prompt": index_row.get("prompt"),
                "artifacts": kept_artifacts,
                "issue_ids": lesson["issue_ids"],
                "feedback": text,
            })
        if verified_execution_status == "pass" and decision == "keep":
            positive_regressions.append(str(case_id))
        elif verified_execution_status in {"fail", "mixed"}:
            negative_regressions.append(str(case_id))
        elif verified_execution_status == "legacy":
            quarantined_legacy.append(str(case_id))
        elif execution_status in {"pass", "fail", "mixed", "legacy"}:
            pending_evidence.append(str(case_id))
        if decision in {"improve", "delete"} and not issues and not text and bool(value.get("autoFix", True)):
            pending_diagnosis.append(str(case_id))
    rules = []
    for issue_id in sorted(sources):
        rule = ISSUE_RULES[issue_id]
        rules.append(
            {
                "issue_id": issue_id,
                "target": rule["target"],
                "requirement": rule["requirement"],
                "capability_ids": list(rule.get("capability_ids") or []),
                "source_case_ids": sorted(sources[issue_id]["case_ids"]),
                "source_decisions": sorted(sources[issue_id]["decisions"]),
            }
        )
    return {
        "schema_version": REVIEW_FEEDBACK_SCHEMA_VERSION,
        "source_catalog_sha256": payload.get("catalog_sha256"),
        "rules": rules,
        "case_lessons": case_lessons,
        "weekly_candidates": weekly_candidates,
        "regression_candidates": {
            "positive": sorted(positive_regressions),
            "negative": sorted(negative_regressions),
            "quarantined_legacy": sorted(quarantined_legacy),
            "pending_evidence": sorted(pending_evidence),
        },
        "pending_diagnosis": sorted(pending_diagnosis),
        "freeform_feedback": feedback,
        "unknown_issue_ids": sorted(unknown_issue_ids),
        "claim_boundary": "Kept cases become weekly candidates. Browser-reported status remains descriptive only; hashed local case_spec.json and quality_report.json evidence is required before positive or negative regression classification. Legacy media remains pending until revalidated. Known tags become constraints; free-form feedback remains traceable until mapped to a tested rule.",
    }


def verified_execution_evidence_from_run_dirs(run_dirs: list[str | Path]) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for value in run_dirs:
        run_dir = Path(value).expanduser().resolve()
        case_path = run_dir / "case_spec.json"
        quality_path = run_dir / "quality_report.json"
        if not case_path.is_file() or case_path.is_symlink() or not quality_path.is_file() or quality_path.is_symlink():
            raise ValueError(f"verified run requires real case_spec.json and quality_report.json: {run_dir}")
        case = json.loads(case_path.read_text(encoding="utf-8"))
        quality = json.loads(quality_path.read_text(encoding="utf-8"))
        if not isinstance(case, dict) or not isinstance(quality, dict):
            raise ValueError(f"verified run evidence must contain JSON objects: {run_dir}")
        case_id = str(case.get("case_id") or "")
        hard_gate = quality.get("hard_gate")
        passed = (
            quality.get("schema_version") == "harness_run_quality_v1"
            and quality.get("run_dir") == str(run_dir)
            and isinstance(hard_gate, dict)
            and quality.get("hard_gate_passed") is True
            and quality.get("status") == "pass"
            and hard_gate.get("passed") is True
            and hard_gate.get("status") == "pass"
        )
        failed = (
            quality.get("schema_version") == "harness_run_quality_v1"
            and quality.get("run_dir") == str(run_dir)
            and isinstance(hard_gate, dict)
            and quality.get("hard_gate_passed") is False
            and quality.get("status") == "fail"
            and hard_gate.get("passed") is False
            and hard_gate.get("status") == "fail"
        )
        if not case_id or not (passed or failed):
            raise ValueError(f"verified run has inconsistent case or hard-gate evidence: {run_dir}")
        if case_id in evidence:
            raise ValueError(f"duplicate verified execution evidence for case: {case_id}")
        evidence[case_id] = {
            "schema_version": EXECUTION_EVIDENCE_SCHEMA_VERSION,
            "case_id": case_id,
            "status": "pass" if passed else "fail",
            "run_dir": str(run_dir),
            "case_spec_sha256": _file_sha256(case_path),
            "quality_report_sha256": _file_sha256(quality_path),
            "evaluator_schema": "harness_run_quality_v1",
        }
    return evidence


def _validate_execution_evidence(evidence: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    if not isinstance(evidence, Mapping):
        raise ValueError("verified_execution_evidence must be an object")
    statuses: dict[str, str] = {}
    for case_id, record in evidence.items():
        run_dir = record.get("run_dir") if isinstance(record, Mapping) else None
        if (
            not isinstance(record, Mapping)
            or record.get("schema_version") != EXECUTION_EVIDENCE_SCHEMA_VERSION
            or record.get("case_id") != case_id
            or record.get("status") not in {"pass", "fail"}
            or not isinstance(run_dir, str)
            or record.get("evaluator_schema") != "harness_run_quality_v1"
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(record.get(field) or "")) for field in ("case_spec_sha256", "quality_report_sha256"))
        ):
            raise ValueError(f"invalid verified execution evidence: {case_id}")
        derived = verified_execution_evidence_from_run_dirs([run_dir]).get(str(case_id))
        if derived != dict(record):
            raise ValueError(f"verified execution evidence does not match local run artifacts: {case_id}")
        statuses[str(case_id)] = str(record["status"])
    return statuses


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def active_review_requirements(capability_id: str) -> dict[str, list[str]]:
    path = os.environ.get(REVIEW_FEEDBACK_ENV)
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != REVIEW_FEEDBACK_SCHEMA_VERSION or not isinstance(data.get("rules"), list):
        raise ValueError("active review feedback file has an unsupported schema")
    result: dict[str, list[str]] = {}
    for row in data["rules"]:
        if not isinstance(row, Mapping):
            raise ValueError("active review feedback rules must be objects")
        issue_id = str(row.get("issue_id") or "")
        canonical = ISSUE_RULES.get(issue_id)
        if canonical is None:
            continue
        capabilities = set(canonical.get("capability_ids") or [])
        if capabilities and capability_id not in capabilities:
            continue
        result.setdefault(str(canonical["target"]), []).append(str(canonical["requirement"]))
    return result
