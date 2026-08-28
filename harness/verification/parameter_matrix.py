from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

from harness.core.artifact_schema import read_json


SCHEMA_VERSION = "harness_parameter_matrix_evaluation_v1"
SUPPORTED_PARAMETERS = frozenset({"cue_speed_m_s"})


def evaluate_parameter_matrix(
    parameter: str,
    expected: str,
    runs: Iterable[tuple[float, str | Path]],
) -> dict[str, Any]:
    if parameter not in SUPPORTED_PARAMETERS:
        raise ValueError(f"unsupported parameter: {parameter}; supported={','.join(sorted(SUPPORTED_PARAMETERS))}")
    if expected not in {"decreasing", "increasing"}:
        raise ValueError("expected must be 'decreasing' or 'increasing'")

    ordered = sorted((float(value), Path(path).resolve()) for value, path in runs)
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    normalized_specs: list[tuple[Path, dict[str, Any]]] = []
    timebases: list[tuple[Path, dict[str, int | float]]] = []
    publication_tiers: list[tuple[Path, str, bool]] = []

    if len(ordered) < 2:
        add_failure(failures, "F_MATRIX_TOO_SMALL", "parameter matrix requires at least two runs")
    values = [value for value, _ in ordered]
    if len(values) != len(set(values)):
        add_failure(failures, "F_PARAMETER_VALUE_DUPLICATE", "parameter values must be unique")
    run_paths = [path for _, path in ordered]
    if len(run_paths) != len(set(run_paths)):
        add_failure(failures, "F_SOURCE_RUN_DUPLICATE", "source run paths must be unique")

    for value, run_dir in ordered:
        row: dict[str, Any] = {"value": value, "run_path": str(run_dir)}
        rows.append(row)
        try:
            quality = read_object(run_dir / "quality_report.json")
            case_spec = read_object(run_dir / "case_spec.json")
            readiness = read_object(run_dir / "run_readiness.json")
        except (OSError, ValueError) as exc:
            add_failure(failures, "F_RUN_ARTIFACT_INVALID", str(exc), value=value, run_path=str(run_dir))
            continue

        hard_gate_passed = (
            quality.get("status") == "pass"
            and quality.get("hard_gate_passed") is True
            and readiness.get("quality_gate_passed") is True
            and readiness.get("verifier_status") == "pass"
        )
        row["hard_gate_passed"] = hard_gate_passed
        row["technical_score"] = ((quality.get("ranking") or {}).get("technical_score"))
        row["backend"] = readiness.get("backend")
        ignore_provenance_and_release_gates = readiness.get("provenance_and_release_gate_override") is True
        if readiness.get("backend") != "ue":
            add_failure(failures, "F_BACKEND_NOT_UE", "formal parameter matrix requires UE source runs", value=value, run_path=str(run_dir))
        if not hard_gate_passed:
            add_failure(failures, "F_HARD_GATE", "run hard gates did not pass", value=value, run_path=str(run_dir))

        provenance = readiness.get("physics_provenance") if isinstance(readiness.get("physics_provenance"), dict) else {}
        if provenance.get("status") != "pass":
            add_failure(
                warnings if ignore_provenance_and_release_gates else failures,
                "W_PHYSICS_PROVENANCE_IGNORED" if ignore_provenance_and_release_gates else "F_PHYSICS_PROVENANCE",
                "physics provenance is not pass",
                value=value,
                run_path=str(run_dir),
            )
        raw_timebase = provenance.get("timebase") if isinstance(provenance.get("timebase"), dict) else {}
        timebase = {
            "physics_hz": positive_number(raw_timebase.get("physics_hz")),
            "render_fps": positive_number(raw_timebase.get("render_fps")),
        }
        row["timebase"] = timebase
        if not all(timebase.values()):
            add_failure(failures, "F_TIMEBASE_MISSING", "physics_hz or render_fps is missing", value=value, run_path=str(run_dir))
        else:
            timebases.append((run_dir, timebase))

        tier = str(readiness.get("publication_tier") or "")
        row["publication_tier"] = tier or None
        if not tier:
            add_failure(
                warnings if ignore_provenance_and_release_gates else failures,
                "W_PUBLICATION_TIER_IGNORED" if ignore_provenance_and_release_gates else "F_PUBLICATION_TIER_MISSING",
                "publication tier is missing",
                value=value,
                run_path=str(run_dir),
            )
        else:
            publication_tiers.append((run_dir, tier, ignore_provenance_and_release_gates))
            tier_ready = (
                readiness.get("local_preview_ready") is True
                if tier == "local_preview"
                else readiness.get("reference_ready") is True
            )
            if not tier_ready:
                add_failure(
                    warnings if ignore_provenance_and_release_gates else failures,
                    "W_PUBLICATION_TIER_IGNORED" if ignore_provenance_and_release_gates else "F_PUBLICATION_TIER_NOT_READY",
                    f"{tier} tier is not ready",
                    value=value,
                    run_path=str(run_dir),
                )

        propagation = ((quality.get("contacts") or {}).get("complete_passive_propagation") or {})
        propagation_passed = (
            propagation.get("required_passive_count") == 15
            and propagation.get("positively_contacted_count") == 15
            and propagation.get("moved_at_least_1cm_count") == 15
            and not propagation.get("missing_contacts")
            and not propagation.get("insufficient_motion")
        )
        row["passive_contacted"] = int(propagation.get("positively_contacted_count") or 0)
        row["passive_moved_1cm"] = int(propagation.get("moved_at_least_1cm_count") or 0)
        if not propagation_passed:
            add_failure(failures, "F_PASSIVE_PROPAGATION_INCOMPLETE", "run does not have 15/15 passive contact and motion", value=value, run_path=str(run_dir))

        contact_frame = integer_or_none((quality.get("contacts") or {}).get("first_positive_contact_frame"))
        row["first_contact_frame"] = contact_frame
        render_fps = timebase["render_fps"]
        if contact_frame is None or not render_fps:
            row["first_contact_time_s"] = None
            add_failure(failures, "F_CONTACT_TIME_MISSING", "first positive contact frame or render_fps is missing", value=value, run_path=str(run_dir))
        else:
            row["first_contact_time_s"] = round(contact_frame / float(render_fps), 6)

        normalized, echo_failures = normalize_case_spec(case_spec, parameter=parameter, expected_value=value)
        for message in echo_failures:
            add_failure(failures, "F_PARAMETER_ECHO_MISMATCH", message, value=value, run_path=str(run_dir))
        normalized_specs.append((run_dir, normalized))
        row["case_contract_sha256"] = canonical_hash(normalized)

    shared_timebase = timebases[0][1] if timebases else None
    timebase_consistent = bool(shared_timebase) and len(timebases) == len(rows)
    for run_dir, timebase in timebases[1:]:
        if timebase != shared_timebase:
            timebase_consistent = False
            add_failure(
                failures,
                "F_TIMEBASE_MISMATCH",
                "physics_hz/render_fps differ across runs",
                run_path=str(run_dir),
                expected=shared_timebase,
                actual=timebase,
            )

    baseline_spec = normalized_specs[0][1] if normalized_specs else None
    for run_dir, normalized in normalized_specs[1:]:
        if normalized != baseline_spec:
            add_failure(
                failures,
                "F_CASE_SPEC_DRIFT",
                "CaseSpec differs outside case_id, sweep_metadata, and the requested parameter",
                run_path=str(run_dir),
                expected_sha256=canonical_hash(baseline_spec),
                actual_sha256=canonical_hash(normalized),
            )

    publication_tier = publication_tiers[0][1] if publication_tiers else None
    baseline_tier_override = publication_tiers[0][2] if publication_tiers else False
    for run_dir, tier, tier_override in publication_tiers[1:]:
        if tier != publication_tier:
            add_failure(
                warnings if baseline_tier_override and tier_override else failures,
                "W_PUBLICATION_TIER_IGNORED" if baseline_tier_override and tier_override else "F_PUBLICATION_TIER_MISMATCH",
                "publication tier differs across runs",
                run_path=str(run_dir),
                expected=publication_tier,
                actual=tier,
            )

    contact_times = [row.get("first_contact_time_s") for row in rows]
    strict_monotonic = len(contact_times) == len(rows) and all(value is not None for value in contact_times)
    if strict_monotonic:
        pairs = zip(contact_times, contact_times[1:])
        strict_monotonic = all(left > right for left, right in pairs) if expected == "decreasing" else all(left < right for left, right in pairs)
    if len(rows) >= 2 and not strict_monotonic:
        add_failure(
            failures,
            "F_DIRECTIONAL_MONOTONICITY",
            f"first positive contact time is not strictly {expected}",
            values=values,
            contact_times_s=contact_times,
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if not failures else "fail",
        "parameter": parameter,
        "expected": expected,
        "publication_tier": publication_tier,
        "shared_contract": shared_timebase if timebase_consistent else None,
        "geometry_and_case_spec_consistent": not any(item["code"] == "F_CASE_SPEC_DRIFT" for item in failures),
        "directional_check": {
            "metric": "first_positive_contact_time_s",
            "expected": expected,
            "strict_monotonic": strict_monotonic,
            "values": values,
            "observed_s": contact_times,
        },
        "runs": rows,
        "failure_codes": sorted({str(item["code"]) for item in failures}),
        "failures": failures,
        "warnings": warnings,
    }


def normalize_case_spec(
    case_spec: dict[str, Any],
    *,
    parameter: str,
    expected_value: float,
) -> tuple[dict[str, Any], list[str]]:
    normalized = copy.deepcopy(case_spec)
    normalized.pop("case_id", None)
    normalized.pop("sweep_metadata", None)
    failures: list[str] = []
    physical_parameters = normalized.get("physical_parameters") if isinstance(normalized.get("physical_parameters"), dict) else {}
    echoed = physical_parameters.pop(parameter, None)
    if not numbers_close(echoed, expected_value):
        failures.append(f"physical_parameters.{parameter}={echoed!r} does not match {expected_value}")

    if parameter == "cue_speed_m_s":
        active_id = str((normalized.get("initial_state") or {}).get("active_striker") or next(iter(normalized.get("active_objects") or []), ""))
        active = next((item for item in normalized.get("objects") or [] if isinstance(item, dict) and str(item.get("id")) == active_id), None)
        velocity = active.get("initial_velocity_m_s") if isinstance(active, dict) else None
        if not isinstance(velocity, list) or len(velocity) < 3:
            failures.append("active striker initial_velocity_m_s is missing")
        else:
            speed = math.sqrt(sum(float(component) ** 2 for component in velocity[:3]))
            if not numbers_close(speed, expected_value):
                failures.append(f"active striker speed={speed!r} does not match {expected_value}")
            if speed > 0:
                active["initial_velocity_m_s"] = [round(float(component) / speed, 12) for component in velocity[:3]]
    return normalized, failures


def read_object(path: Path) -> dict[str, Any]:
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def positive_number(value: Any) -> int | float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(parsed) or parsed <= 0:
        return 0
    return int(parsed) if parsed.is_integer() else parsed


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def numbers_close(left: Any, right: float) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def canonical_hash(payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def add_failure(failures: list[dict[str, Any]], code: str, message: str, **details: Any) -> None:
    failures.append({"code": code, "message": message, **details})
