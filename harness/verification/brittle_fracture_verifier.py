from __future__ import annotations

from typing import Any


def verify_brittle_fracture(case_spec: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    if not trajectory:
        return "F1_missing_trajectory", failure("trajectory", 0, 0, "frame_count", 0), evidence

    expected = dict(case_spec.get("expected_physics") or {})
    impactor_id = str(expected.get("impactor_object_id") or first_role(case_spec, {"active_impactor", "active_striker"}) or "impactor")
    brittle_id = str(expected.get("brittle_object_id") or first_role(case_spec, {"brittle_fracture_body", "breakable_body", "destructible_body"}) or "brittle_body")
    threshold = positive_float(expected.get("fracture_threshold_j")) or object_threshold(case_spec, brittle_id)
    if threshold is None:
        return "F3_invalid_initial_physics_state", failure(brittle_id, 0, 0, "fracture_threshold_j", expected.get("fracture_threshold_j")), evidence
    min_fragments = int(expected.get("expected_min_fragment_count") or 2)

    contact = first_contact_event(trajectory, impactor_id, brittle_id)
    fracture = first_fracture_event(trajectory, brittle_id)
    if fracture and contact and int(fracture.get("frame") or 0) < int(contact.get("frame") or 0):
        detail = failure(brittle_id, int(fracture.get("frame") or 0), float(fracture.get("time_s") or 0.0), "fracture_frame_before_contact", int(fracture.get("frame") or 0))
        detail["contact_frame"] = int(contact.get("frame") or 0)
        return "F4_causality_violation", detail, evidence
    if contact is None:
        if fracture:
            return "F4_causality_violation", failure(brittle_id, int(fracture.get("frame") or 0), float(fracture.get("time_s") or 0.0), "fracture_without_contact", True), evidence
        return "F2_missing_contact_events", failure(f"{impactor_id}:{brittle_id}", 0, 0, "contact_event_present", False), evidence

    impact_energy = positive_float(contact.get("impact_energy_j") or contact.get("energy_j"))
    if impact_energy is None:
        return "F7_runtime_artifact_incomplete", failure(f"{impactor_id}:{brittle_id}", int(contact.get("frame") or 0), float(contact.get("time_s") or 0.0), "impact_energy_j_present", False), evidence

    if fracture is None:
        if impact_energy >= threshold:
            return "F7_runtime_artifact_incomplete", failure(brittle_id, int(contact.get("frame") or 0), float(contact.get("time_s") or 0.0), "fracture_event_present", False), evidence
        return None, None, [{"brittle_object_id": brittle_id, "impact_energy_j": round(impact_energy, 6), "fractured": False}]

    fracture_frame = int(fracture.get("frame") or 0)
    contact_frame = int(contact.get("frame") or 0)
    if fracture_frame < contact_frame:
        detail = failure(brittle_id, fracture_frame, float(fracture.get("time_s") or 0.0), "fracture_frame_before_contact", fracture_frame)
        detail["contact_frame"] = contact_frame
        return "F4_causality_violation", detail, evidence
    event_energy = positive_float(fracture.get("impact_energy_j") or fracture.get("energy_j")) or impact_energy
    if event_energy < threshold:
        detail = failure(brittle_id, fracture_frame, float(fracture.get("time_s") or 0.0), "impact_energy_j", round(event_energy, 6))
        detail["fracture_threshold_j"] = round(threshold, 6)
        return "F4_causality_violation", detail, evidence

    fragment_count = int(fracture.get("fragment_count") or fragment_count_from_frames(trajectory, brittle_id, fracture_frame))
    if fragment_count < min_fragments:
        detail = failure(brittle_id, fracture_frame, float(fracture.get("time_s") or 0.0), "fragment_count", fragment_count)
        detail["expected_min_fragment_count"] = min_fragments
        return "F4_causality_violation", detail, evidence

    evidence.append(
        {
            "impactor_object_id": impactor_id,
            "fractured_object_id": brittle_id,
            "contact_frame": contact_frame,
            "fracture_frame": fracture_frame,
            "impact_energy_j": round(event_energy, 6),
            "fracture_threshold_j": round(threshold, 6),
            "fragment_count": fragment_count,
        }
    )
    return None, None, evidence


def first_contact_event(trajectory: list[dict[str, Any]], a: str, b: str) -> dict[str, Any] | None:
    for frame in trajectory:
        frame_id = int(frame.get("frame") or 0)
        time_s = float(frame.get("time_s") or 0.0)
        for contact in frame.get("contacts") or []:
            if not isinstance(contact, dict):
                continue
            objects = [str(item) for item in contact.get("objects") or contact.get("pair") or []]
            if {a, b}.issubset(set(objects)):
                result = dict(contact)
                result.setdefault("frame", frame_id)
                result.setdefault("time_s", time_s)
                return result
    return None


def first_fracture_event(trajectory: list[dict[str, Any]], object_id: str) -> dict[str, Any] | None:
    for frame in trajectory:
        frame_id = int(frame.get("frame") or 0)
        time_s = float(frame.get("time_s") or 0.0)
        for event in frame.get("fracture_events") or []:
            if not isinstance(event, dict):
                continue
            if str(event.get("event_type") or "fracture") != "fracture":
                continue
            if str(event.get("object_id") or event.get("source_object_id") or object_id) != object_id:
                continue
            result = dict(event)
            result.setdefault("frame", frame_id)
            result.setdefault("time_s", time_s)
            return result
    return None


def fragment_count_from_frames(trajectory: list[dict[str, Any]], object_id: str, start_frame: int) -> int:
    ids: set[str] = set()
    for frame in trajectory:
        if int(frame.get("frame") or 0) < start_frame:
            continue
        for fragment in frame.get("fragments") or []:
            if not isinstance(fragment, dict):
                continue
            if str(fragment.get("source_object_id") or object_id) != object_id:
                continue
            ids.add(str(fragment.get("fragment_id") or len(ids)))
    return len(ids)


def first_role(case_spec: dict[str, Any], roles: set[str]) -> str | None:
    for obj in case_spec.get("objects") or []:
        if isinstance(obj, dict) and str(obj.get("role") or "") in roles:
            return str(obj.get("id"))
    return None


def object_threshold(case_spec: dict[str, Any], object_id: str) -> float | None:
    for obj in case_spec.get("objects") or []:
        if isinstance(obj, dict) and str(obj.get("id")) == object_id:
            return positive_float(obj.get("fracture_threshold_j"))
    return None


def positive_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0.0 else None


def failure(object_id: str, frame: int, time: float, metric: str, value: Any) -> dict[str, Any]:
    return {"object_id": object_id, "frame": frame, "time": time, "metric": metric, "value": value}
