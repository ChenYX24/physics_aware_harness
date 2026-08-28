from __future__ import annotations

import math
from typing import Any, Mapping

from harness.core.scene_layout import rotate_local_vector_ue


RUNTIME_CONSTRAINT_LINEAR_TOLERANCE_M = 0.01


def verify_trajectory_assertions(
    case_spec: Mapping[str, Any],
    trajectory: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    integrity_failure = trajectory_integrity_failure(trajectory)
    if integrity_failure is not None:
        return "trajectory_state_invalid", integrity_failure, []
    constraint_failure, constraint_evidence = verify_rigid_constraint_residuals(case_spec, trajectory)
    if constraint_failure is not None:
        return "F_RUNTIME_CONSTRAINT_ENFORCEMENT_FAILED", constraint_failure, constraint_evidence
    state_failure, state_evidence = verify_constraint_state_trace(case_spec, trajectory)
    constraint_evidence.extend(state_evidence)
    if state_failure is not None:
        return "F_RUNTIME_CONSTRAINT_STATE_INVALID", state_failure, constraint_evidence
    force_failure, force_evidence = verify_continuous_force_trace(case_spec, trajectory)
    constraint_evidence.extend(force_evidence)
    if force_failure is not None:
        return "F_RUNTIME_CONTINUOUS_FORCE_TRACE_INVALID", force_failure, constraint_evidence
    assertions = [
        item
        for item in case_spec.get("verification_assertions") or []
        if isinstance(item, Mapping)
    ]
    if not assertions:
        assertions = [{"id": "trajectory_integrity", "type": "trajectory_integrity"}]
    results: list[dict[str, Any]] = []
    first_failed_result: dict[str, Any] | None = None
    for index, assertion in enumerate(assertions):
        result = evaluate_assertion(assertion, trajectory)
        result["id"] = str(assertion.get("id") or f"assertion_{index}")
        results.append(result)
        if not result["passed"] and first_failed_result is None:
            first_failed_result = result
    evidence = [*constraint_evidence, {"type": "trajectory_assertions", "results": results}]
    if first_failed_result is not None:
        return (
            "declared_assertion_failed",
            {
                "object_id": str(first_failed_result.get("object_id") or "trajectory"),
                "frame": int(first_failed_result.get("frame") or trajectory[-1].get("frame") or 0),
                "time": first_failed_result.get("time_s", trajectory[-1].get("time_s")),
                "metric": str(first_failed_result["id"]),
                "value": first_failed_result,
            },
            evidence,
        )
    return None, None, evidence


def verify_rigid_constraint_residuals(
    case_spec: Mapping[str, Any],
    trajectory: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    constraints = [
        item
        for item in case_spec.get("constraints") or []
        if isinstance(item, Mapping)
    ]
    if not constraints:
        return None, []
    objects = {
        str(item.get("id") or ""): item
        for item in case_spec.get("objects") or []
        if isinstance(item, Mapping) and item.get("id")
    }
    results: list[dict[str, Any]] = []
    first_failure: dict[str, Any] | None = None
    for constraint in constraints:
        constraint_id = str(constraint.get("id") or "constraint")
        body_a = str(constraint.get("body_a") or "")
        body_b = str(constraint.get("body_b") or "")
        maxima = {axis: 0.0 for axis in ("x", "y", "z")}
        worst_samples: dict[str, dict[str, Any]] = {}
        result = {
            "constraint_id": constraint_id,
            "body_a": body_a,
            "body_b": body_b,
            "passed": True,
            "linear_tolerance_m": RUNTIME_CONSTRAINT_LINEAR_TOLERANCE_M,
            "max_residual_m": maxima,
        }
        for frame_index, frame in enumerate(trajectory):
            frame_objects = frame.get("objects") if isinstance(frame.get("objects"), Mapping) else {}
            pose_a = _constraint_body_pose(objects.get(body_a), frame_objects.get(body_a))
            pose_b = _constraint_body_pose(objects.get(body_b), frame_objects.get(body_b))
            if pose_a is None or pose_b is None:
                missing_body = body_a if pose_a is None else body_b
                sample = {
                    "object_id": constraint_id,
                    "frame": int(frame.get("frame", frame_index)),
                    "time": frame.get("time_s", frame.get("time")),
                    "metric": "constraint_body_state_missing",
                    "value": {"constraint_id": constraint_id, "body_id": missing_body},
                }
                result["passed"] = False
                result["failure"] = sample["value"]
                if first_failure is None:
                    first_failure = sample
                break
            world_frame_a = _constraint_world_frame(pose_a, constraint.get("frame_a"))
            world_frame_b = _constraint_world_frame(pose_b, constraint.get("frame_b"))
            if world_frame_a is None or world_frame_b is None:
                sample = {
                    "object_id": constraint_id,
                    "frame": int(frame.get("frame", frame_index)),
                    "time": frame.get("time_s", frame.get("time")),
                    "metric": "constraint_frame_invalid",
                    "value": {"constraint_id": constraint_id},
                }
                result["passed"] = False
                result["failure"] = sample["value"]
                if first_failure is None:
                    first_failure = sample
                break
            origin_a, axes_a = world_frame_a
            origin_b, _ = world_frame_b
            delta = [origin_b[index] - origin_a[index] for index in range(3)]
            motion = constraint.get("linear_motion") if isinstance(constraint.get("linear_motion"), Mapping) else {}
            linear_limit = float(constraint.get("linear_limit_m") or 0.0)
            for axis_name, world_axis in zip(("x", "y", "z"), axes_a, strict=True):
                mode = str(motion.get(axis_name) or "free")
                if mode == "free":
                    continue
                residual = abs(sum(delta[index] * world_axis[index] for index in range(3)))
                if residual > maxima[axis_name]:
                    maxima[axis_name] = residual
                    worst_samples[axis_name] = {
                        "frame": int(frame.get("frame", frame_index)),
                        "time_s": frame.get("time_s", frame.get("time")),
                    }
                allowed = RUNTIME_CONSTRAINT_LINEAR_TOLERANCE_M + (
                    linear_limit if mode == "limited" else 0.0
                )
                if residual <= allowed:
                    continue
                sample_value = {
                    "constraint_id": constraint_id,
                    "body_a": body_a,
                    "body_b": body_b,
                    "axis": axis_name,
                    "motion": mode,
                    "residual_m": round(residual, 6),
                    "allowed_m": round(allowed, 6),
                }
                result["passed"] = False
                result["failure"] = sample_value
                if first_failure is None:
                    first_failure = {
                        "object_id": constraint_id,
                        "frame": int(frame.get("frame", frame_index)),
                        "time": frame.get("time_s", frame.get("time")),
                        "metric": "constraint_linear_residual_m",
                        "value": sample_value,
                    }
        result["max_residual_m"] = {
            axis: round(value, 6) for axis, value in maxima.items()
        }
        result["worst_samples"] = worst_samples
        results.append(result)
    return first_failure, [{"type": "rigid_constraint_residuals", "results": results}]


def verify_constraint_state_trace(
    case_spec: Mapping[str, Any],
    trajectory: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    configured = {
        str(item.get("id") or ""): item
        for item in case_spec.get("constraints") or []
        if isinstance(item, Mapping)
        and item.get("id")
        and any(item.get(field) is not None for field in ("linear_drive", "unilateral_distance_spring", "angular_drive", "break_thresholds"))
    }
    if not configured:
        return None, []
    results = {
        constraint_id: {
            "constraint_id": constraint_id,
            "sample_count": 0,
            "max_elastic_energy_residual_j": 0.0,
            "max_distance_spring_force_residual_n": 0.0,
            "last_distance_spring_evaluation_count": 0,
            "last_distance_spring_active_evaluation_count": 0,
            "broken_observed": False,
            "passed": True,
        }
        for constraint_id in configured
    }
    first_failure = None
    for frame_index, frame in enumerate(trajectory):
        samples = {
            str(item.get("constraint_id") or ""): item
            for item in frame.get("constraints") or []
            if isinstance(item, Mapping) and item.get("constraint_id")
        }
        for constraint_id, declaration in configured.items():
            sample = samples.get(constraint_id)
            if sample is None:
                first_failure = first_failure or {
                    "object_id": constraint_id,
                    "frame": int(frame.get("frame", frame_index)),
                    "time": frame.get("time_s", frame.get("time")),
                    "metric": "constraint_trace_missing",
                    "value": {"constraint_id": constraint_id},
                }
                results[constraint_id]["passed"] = False
                continue
            results[constraint_id]["sample_count"] += 1
            if sample.get("source") != "adp_cpp_runtime_driver":
                first_failure = first_failure or {
                    "object_id": constraint_id,
                    "frame": int(frame.get("frame", frame_index)),
                    "time": frame.get("time_s", frame.get("time")),
                    "metric": "constraint_trace_source",
                    "value": sample.get("source"),
                }
                results[constraint_id]["passed"] = False
                continue
            declared_spring = declaration.get("unilateral_distance_spring")
            sampled_spring = sample.get("unilateral_distance_spring")
            if isinstance(declared_spring, Mapping):
                expected_parameters = {
                    key: float(declared_spring[key])
                    for key in ("rest_length_m", "stiffness_n_m", "damping_n_s_m")
                }
                actual_parameters = {
                    key: sampled_spring.get(key)
                    for key in expected_parameters
                } if isinstance(sampled_spring, Mapping) else None
                if actual_parameters != expected_parameters:
                    first_failure = first_failure or {
                        "object_id": constraint_id,
                        "frame": int(frame.get("frame", frame_index)),
                        "time": frame.get("time_s", frame.get("time")),
                        "metric": "constraint_unilateral_distance_spring_mismatch",
                        "value": {"expected": expected_parameters, "actual": actual_parameters},
                    }
                    results[constraint_id]["passed"] = False
                    continue
                spring_scalars = (
                    "distance_m",
                    "extension_m",
                    "separation_speed_m_s",
                    "tension_n",
                    "cumulative_evaluation_count",
                    "cumulative_active_evaluation_count",
                )
                if any(not finite_number(sampled_spring.get(field)) for field in spring_scalars) or any(
                    not finite_vector(sampled_spring.get(field)) or len(sampled_spring.get(field)) != 3
                    for field in ("direction_a_to_b", "force_on_body_b_n")
                ):
                    first_failure = first_failure or {
                        "object_id": constraint_id,
                        "frame": int(frame.get("frame", frame_index)),
                        "time": frame.get("time_s", frame.get("time")),
                        "metric": "constraint_unilateral_distance_spring_trace_shape",
                        "value": sampled_spring,
                    }
                    results[constraint_id]["passed"] = False
                    continue
                distance = float(sampled_spring["distance_m"])
                extension = float(sampled_spring["extension_m"])
                separation_speed = float(sampled_spring["separation_speed_m_s"])
                tension = float(sampled_spring["tension_n"])
                expected_extension = max(0.0, distance - expected_parameters["rest_length_m"])
                expected_tension = max(
                    0.0,
                    expected_parameters["stiffness_n_m"] * expected_extension
                    + expected_parameters["damping_n_s_m"] * separation_speed,
                ) if expected_extension > 0.0 else 0.0
                direction = [float(value) for value in sampled_spring["direction_a_to_b"]]
                force_on_b = [float(value) for value in sampled_spring["force_on_body_b_n"]]
                extension_residual = abs(extension - expected_extension)
                force_residual = max(
                    abs(tension - expected_tension),
                    *(abs(force_on_b[index] + direction[index] * tension) for index in range(3)),
                )
                results[constraint_id]["max_distance_spring_force_residual_n"] = max(
                    float(results[constraint_id]["max_distance_spring_force_residual_n"]),
                    force_residual,
                )
                evaluation_count = int(sampled_spring["cumulative_evaluation_count"])
                active_evaluation_count = int(sampled_spring["cumulative_active_evaluation_count"])
                if (
                    force_residual > 1e-4
                    or extension_residual > 1e-6
                    or evaluation_count < int(results[constraint_id]["last_distance_spring_evaluation_count"])
                    or active_evaluation_count < int(results[constraint_id]["last_distance_spring_active_evaluation_count"])
                    or active_evaluation_count > evaluation_count
                ):
                    first_failure = first_failure or {
                        "object_id": constraint_id,
                        "frame": int(frame.get("frame", frame_index)),
                        "time": frame.get("time_s", frame.get("time")),
                        "metric": "constraint_unilateral_distance_spring_force_law",
                        "value": sampled_spring,
                    }
                    results[constraint_id]["passed"] = False
                results[constraint_id]["last_distance_spring_evaluation_count"] = evaluation_count
                results[constraint_id]["last_distance_spring_active_evaluation_count"] = active_evaluation_count
            required_vectors = (
                "translation_m",
                "position_target_m",
                "deformation_m",
                "relative_velocity_m_s",
                "linear_force_n",
                "angular_torque_n_m",
                "stiffness_n_m",
            )
            if any(not finite_vector(sample.get(field)) or len(sample.get(field)) != 3 for field in required_vectors):
                first_failure = first_failure or {
                    "object_id": constraint_id,
                    "frame": int(frame.get("frame", frame_index)),
                    "time": frame.get("time_s", frame.get("time")),
                    "metric": "constraint_trace_shape",
                    "value": dict(sample),
                }
                results[constraint_id]["passed"] = False
                continue
            drive = declaration.get("linear_drive") if isinstance(declaration.get("linear_drive"), Mapping) else {}
            stiffness = [float(value) for value in drive.get("stiffness_n_m") or [0.0, 0.0, 0.0]]
            deformation = [float(value) for value in sample["deformation_m"]]
            expected_energy = sum(0.5 * stiffness[index] * deformation[index] ** 2 for index in range(3))
            if isinstance(declared_spring, Mapping) and isinstance(sampled_spring, Mapping):
                expected_energy += 0.5 * float(declared_spring["stiffness_n_m"]) * float(sampled_spring["extension_m"]) ** 2
            actual_energy = sample.get("elastic_potential_j")
            if not finite_number(actual_energy):
                energy_residual = math.inf
            else:
                energy_residual = abs(float(actual_energy) - expected_energy)
            results[constraint_id]["max_elastic_energy_residual_j"] = max(
                float(results[constraint_id]["max_elastic_energy_residual_j"]),
                energy_residual,
            )
            if energy_residual > max(1e-6, abs(expected_energy) * 1e-5):
                first_failure = first_failure or {
                    "object_id": constraint_id,
                    "frame": int(frame.get("frame", frame_index)),
                    "time": frame.get("time_s", frame.get("time")),
                    "metric": "constraint_elastic_energy_residual_j",
                    "value": {
                        "expected_j": expected_energy,
                        "actual_j": actual_energy,
                        "residual_j": energy_residual,
                    },
                }
                results[constraint_id]["passed"] = False
            broken = sample.get("broken")
            if not isinstance(broken, bool):
                first_failure = first_failure or {
                    "object_id": constraint_id,
                    "frame": int(frame.get("frame", frame_index)),
                    "time": frame.get("time_s", frame.get("time")),
                    "metric": "constraint_broken_state_missing",
                    "value": broken,
                }
                results[constraint_id]["passed"] = False
            elif broken:
                results[constraint_id]["broken_observed"] = True
                if declaration.get("break_thresholds") is None:
                    first_failure = first_failure or {
                        "object_id": constraint_id,
                        "frame": int(frame.get("frame", frame_index)),
                        "time": frame.get("time_s", frame.get("time")),
                        "metric": "unbreakable_constraint_broken",
                        "value": {"constraint_id": constraint_id},
                    }
                    results[constraint_id]["passed"] = False
    for constraint_id, result in results.items():
        required_evaluations = max(0, int(result["sample_count"] - 2))
        result["required_min_distance_spring_evaluation_count"] = required_evaluations
        final_time_s = float((trajectory[-1] if trajectory else {}).get("time_s", (trajectory[-1] if trajectory else {}).get("time") or 0.0))
        result["observed_distance_spring_evaluation_hz"] = round(
            float(result["last_distance_spring_evaluation_count"]) / final_time_s,
            6,
        ) if final_time_s > 0.0 else 0.0
        if (
            isinstance(configured[constraint_id].get("unilateral_distance_spring"), Mapping)
            and result["sample_count"] > 1
            and result["last_distance_spring_evaluation_count"] < required_evaluations
        ):
            first_failure = first_failure or {
                "object_id": constraint_id,
                "frame": int((trajectory[-1] if trajectory else {}).get("frame", max(0, len(trajectory) - 1))),
                "time": (trajectory[-1] if trajectory else {}).get("time_s", (trajectory[-1] if trajectory else {}).get("time")),
                "metric": "constraint_unilateral_distance_spring_evaluations_missing",
                "value": {
                    "constraint_id": constraint_id,
                    "required_min": required_evaluations,
                    "actual": result["last_distance_spring_evaluation_count"],
                },
            }
            result["passed"] = False
        result["max_elastic_energy_residual_j"] = round(float(result["max_elastic_energy_residual_j"]), 9)
        result["max_distance_spring_force_residual_n"] = round(float(result["max_distance_spring_force_residual_n"]), 9)
    return first_failure, [{"type": "constraint_state_trace", "results": list(results.values())}]


def verify_continuous_force_trace(
    case_spec: Mapping[str, Any],
    trajectory: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    configured = {
        str(item.get("id") or ""): item
        for item in case_spec.get("forces") or []
        if isinstance(item, Mapping) and item.get("type") == "continuous_force" and item.get("id")
    }
    if not configured:
        return None, []
    results = {
        force_id: {"force_id": force_id, "sample_count": 0, "passed": True}
        for force_id in configured
    }
    first_failure: dict[str, Any] | None = None
    for frame_index, frame in enumerate(trajectory):
        time_s = frame.get("time_s", frame.get("time"))
        if not finite_number(time_s):
            continue
        samples = {
            str(item.get("force_id") or ""): item
            for item in frame.get("forces") or []
            if isinstance(item, Mapping) and item.get("force_id")
        }
        for force_id, declaration in configured.items():
            start = float(declaration["start_time_s"])
            end = float(declaration["end_time_s"])
            if float(time_s) + 1e-9 < start or float(time_s) > end + 1e-9:
                continue
            sample = samples.get(force_id)
            expected_vector = [float(value) for value in declaration["vector_n"]]
            valid = bool(
                sample
                and sample.get("source") == "adp_cpp_runtime_driver"
                and str(sample.get("object") or "") == str(declaration.get("object") or "")
                and finite_vector(sample.get("vector_n"))
                and len(sample["vector_n"]) == 3
                and all(abs(float(sample["vector_n"][axis]) - expected_vector[axis]) <= 1e-6 for axis in range(3))
            )
            if valid:
                results[force_id]["sample_count"] += 1
                continue
            results[force_id]["passed"] = False
            first_failure = first_failure or {
                "object_id": str(declaration.get("object") or force_id),
                "frame": int(frame.get("frame", frame_index)),
                "time": float(time_s),
                "metric": "continuous_force_trace",
                "value": {"force_id": force_id, "expected": dict(declaration), "actual": sample},
            }
    for force_id, result in results.items():
        if result["sample_count"] > 0:
            continue
        result["passed"] = False
        declaration = configured[force_id]
        first_failure = first_failure or {
            "object_id": str(declaration.get("object") or force_id),
            "frame": 0,
            "time": float(declaration["start_time_s"]),
            "metric": "continuous_force_trace_missing",
            "value": {"force_id": force_id},
        }
    return first_failure, [{"type": "continuous_force_trace", "results": list(results.values())}]


def _constraint_body_pose(
    declared: Mapping[str, Any] | None,
    state: Any,
) -> tuple[list[float], list[float]] | None:
    if declared is None:
        return None
    if isinstance(state, Mapping):
        position = _first_vector(state, "position_m", "position")
        rotation = _first_vector(state, "rotation_deg", "rotation_degrees", "rotation")
        if position is not None and rotation is not None:
            return position, rotation
        return None
    if str(declared.get("body_type") or "") != "static":
        return None
    position = _first_vector(declared, "initial_position_m", "position_m", "position")
    rotation = _first_vector(declared, "initial_rotation_deg", "rotation_deg", "rotation_degrees")
    if position is None or rotation is None:
        return None
    return position, rotation


def _constraint_world_frame(
    pose: tuple[list[float], list[float]],
    raw_frame: Any,
) -> tuple[list[float], list[list[float]]] | None:
    if not isinstance(raw_frame, Mapping):
        return None
    frame = raw_frame
    position, rotation = pose
    local_position = _first_vector(frame, "position_m")
    primary = _first_vector(frame, "primary_axis")
    secondary = _first_vector(frame, "secondary_axis")
    if local_position is None or primary is None or secondary is None:
        return None
    world_offset = rotate_local_vector_ue(local_position, rotation)
    world_primary = rotate_local_vector_ue(primary, rotation)
    world_secondary = rotate_local_vector_ue(secondary, rotation)
    world_tertiary = [
        world_primary[1] * world_secondary[2] - world_primary[2] * world_secondary[1],
        world_primary[2] * world_secondary[0] - world_primary[0] * world_secondary[2],
        world_primary[0] * world_secondary[1] - world_primary[1] * world_secondary[0],
    ]
    return (
        [position[index] + world_offset[index] for index in range(3)],
        [world_primary, world_secondary, world_tertiary],
    )


def _first_vector(value: Mapping[str, Any], *keys: str) -> list[float] | None:
    for key in keys:
        candidate = value.get(key)
        if finite_vector(candidate) and len(candidate) == 3:
            return [float(item) for item in candidate]
    return None


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
        return {
            "type": assertion_type,
            "passed": passed,
            "pairs": pairs,
            "first_frames": observed,
            "pair_results": [
                {"objects": pair, "first_frame": frame, "observed": frame is not None}
                for pair, frame in zip(pairs, observed)
            ],
        }
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
