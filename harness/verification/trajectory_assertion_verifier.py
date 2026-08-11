from __future__ import annotations

import math
from typing import Any, Mapping


def verify_trajectory_assertions(
    case_spec: Mapping[str, Any],
    trajectory: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    integrity_failure = trajectory_integrity_failure(trajectory)
    if integrity_failure is not None:
        return "trajectory_state_invalid", integrity_failure, []
    assertions = [
        item
        for item in case_spec.get("verification_assertions") or []
        if isinstance(item, Mapping)
    ]
    if not assertions:
        assertions = [{"id": "trajectory_integrity", "type": "trajectory_integrity"}]
    results: list[dict[str, Any]] = []
    for index, assertion in enumerate(assertions):
        result = evaluate_assertion(assertion, trajectory)
        result["id"] = str(assertion.get("id") or f"assertion_{index}")
        results.append(result)
        if not result["passed"]:
            return (
                "declared_assertion_failed",
                {
                    "object_id": str(result.get("object_id") or "trajectory"),
                    "frame": int(result.get("frame") or trajectory[-1].get("frame") or 0),
                    "time": result.get("time_s", trajectory[-1].get("time_s")),
                    "metric": str(result["id"]),
                    "value": result,
                },
                [{"type": "trajectory_assertions", "results": results}],
            )
    return None, None, [{"type": "trajectory_assertions", "results": results}]


def trajectory_integrity_failure(trajectory: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not trajectory:
        return {"object_id": "trajectory", "frame": 0, "time": 0.0, "metric": "frame_count", "value": 0}
    previous_time = -math.inf
    for index, frame in enumerate(trajectory):
        time_s = frame.get("time_s", frame.get("time"))
        if not finite_number(time_s) or float(time_s) <= previous_time:
            return {"object_id": "trajectory", "frame": index, "time": time_s, "metric": "monotonic_time", "value": time_s}
        previous_time = float(time_s)
        objects = frame.get("objects")
        if not isinstance(objects, Mapping):
            return {"object_id": "trajectory", "frame": index, "time": time_s, "metric": "objects", "value": objects}
        for object_id, state in objects.items():
            if not isinstance(state, Mapping):
                return {"object_id": str(object_id), "frame": index, "time": time_s, "metric": "state", "value": state}
            for field in ("position_m", "velocity_m_s", "rotation_deg"):
                value = state.get(field)
                if value is not None and not finite_vector(value):
                    return {"object_id": str(object_id), "frame": index, "time": time_s, "metric": field, "value": value}
    return None


def evaluate_assertion(assertion: Mapping[str, Any], trajectory: list[dict[str, Any]]) -> dict[str, Any]:
    assertion_type = str(assertion.get("type") or "")
    if assertion_type in {"trajectory_integrity", "artifact_complete"}:
        return {"type": assertion_type, "passed": True, "measured": len(trajectory)}
    if assertion_type in {"event_exists", "event_count"}:
        events = matching_events(assertion, trajectory)
        measured = len(events)
        if assertion_type == "event_exists" and assertion.get("operator") is None:
            passed = measured > 0
            expected = 1
            operator = ">="
        else:
            operator = str(assertion.get("operator") or ">=")
            expected = float(assertion.get("value", 1))
            passed = compare(float(measured), operator, expected)
        return {"type": assertion_type, "passed": passed, "measured": measured, "operator": operator, "expected": expected}
    if assertion_type == "event_sequence":
        pairs = assertion_pairs(assertion)
        observed = [first_event_frame(pair, trajectory) for pair in pairs]
        passed = bool(pairs) and all(value is not None for value in observed) and all(
            int(before) < int(after) for before, after in zip(observed, observed[1:], strict=False)
        )
        return {"type": assertion_type, "passed": passed, "pairs": pairs, "first_frames": observed}
    if assertion_type in {"state_delta", "state_value"}:
        object_id = str(assertion.get("object_id") or first(assertion.get("objects")) or "")
        field = str(assertion.get("field") or "position_m.z")
        values = state_series(trajectory, object_id, field)
        if not values:
            return {"type": assertion_type, "passed": False, "object_id": object_id, "field": field, "measured": None}
        if assertion_type == "state_delta":
            measured = values[-1] - values[0]
        else:
            reduction = str(assertion.get("reduction") or "final")
            measured = {"initial": values[0], "final": values[-1], "min": min(values), "max": max(values)}.get(reduction)
        operator = str(assertion.get("operator") or ">=")
        expected = float(assertion.get("value", 0.0))
        return {
            "type": assertion_type,
            "passed": measured is not None and compare(float(measured), operator, expected),
            "object_id": object_id,
            "field": field,
            "measured": measured,
            "operator": operator,
            "expected": expected,
        }
    return {"type": assertion_type, "passed": False, "reason": "unsupported_generic_assertion"}


def matching_events(assertion: Mapping[str, Any], trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    event_type = str(assertion.get("event") or "contact")
    pair = assertion_pair(assertion)
    events: list[dict[str, Any]] = []
    for frame in trajectory:
        frame_events = frame.get("contacts") if event_type == "contact" else frame.get("events")
        for event in frame_events or []:
            if not isinstance(event, Mapping):
                continue
            if event_type != "contact" and str(event.get("type") or "") != event_type:
                continue
            if pair and frozenset(event_objects(event)) != frozenset(pair):
                continue
            events.append(dict(event))
    return events


def assertion_pairs(assertion: Mapping[str, Any]) -> list[list[str]]:
    pairs = assertion.get("pairs")
    if isinstance(pairs, list):
        return [[str(value) for value in pair[:2]] for pair in pairs if isinstance(pair, list) and len(pair) >= 2]
    objects = [str(value) for value in assertion.get("objects") or []]
    return [[left, right] for left, right in zip(objects, objects[1:], strict=False)]


def assertion_pair(assertion: Mapping[str, Any]) -> list[str]:
    objects = assertion.get("objects")
    return [str(value) for value in objects[:2]] if isinstance(objects, list) and len(objects) >= 2 else []


def first_event_frame(pair: list[str], trajectory: list[dict[str, Any]]) -> int | None:
    for index, frame in enumerate(trajectory):
        for event in frame.get("contacts") or []:
            if isinstance(event, Mapping) and frozenset(event_objects(event)) == frozenset(pair):
                return int(frame.get("frame", index))
    return None


def event_objects(event: Mapping[str, Any]) -> list[str]:
    for keys in (("a", "b"), ("object_a", "object_b"), ("source", "target")):
        if event.get(keys[0]) is not None and event.get(keys[1]) is not None:
            return [str(event[keys[0]]), str(event[keys[1]])]
    objects = event.get("objects")
    return [str(value) for value in objects[:2]] if isinstance(objects, list) else []


def state_series(trajectory: list[dict[str, Any]], object_id: str, field: str) -> list[float]:
    path = field.split(".")
    axis = {"x": 0, "y": 1, "z": 2}
    values: list[float] = []
    for frame in trajectory:
        value: Any = (frame.get("objects") or {}).get(object_id)
        for token in path:
            if isinstance(value, Mapping):
                value = value.get(token)
            elif isinstance(value, list) and token in axis and len(value) > axis[token]:
                value = value[axis[token]]
            else:
                value = None
                break
        if not finite_number(value):
            return []
        values.append(float(value))
    return values


def compare(measured: float, operator: str, expected: float) -> bool:
    return {
        ">=": measured >= expected,
        "<=": measured <= expected,
        ">": measured > expected,
        "<": measured < expected,
        "==": math.isclose(measured, expected, rel_tol=1e-9, abs_tol=1e-9),
    }.get(operator, False)


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def finite_vector(value: Any) -> bool:
    return isinstance(value, list) and all(finite_number(item) for item in value)


def first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else None
