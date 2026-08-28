from __future__ import annotations

import math
from pathlib import Path
from typing import Any


def verify_particle_cache(
    cache: dict[str, Any],
    *,
    root: str | Path | None = None,
    expected_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    frames = cache.get("frames") if isinstance(cache.get("frames"), list) else []
    particles = cache.get("particles") if isinstance(cache.get("particles"), dict) else {}
    expected_count = int(particles.get("count") or 0)
    stable_ids = particles.get("stable_ids") if isinstance(particles.get("stable_ids"), list) else []
    if stable_ids != list(range(expected_count)):
        failures.append(failure("particle_ids_unstable", 0, {"count": len(stable_ids), "expected": expected_count}))
    previous_time = -math.inf
    environment = cache.get("environment") if isinstance(cache.get("environment"), dict) else {}
    if environment.get("type") != "rigid_sph_scene":
        failures.append(failure("particle_environment_contract", 0, environment.get("type")))
    if expected_contract is not None:
        for field in ("workspace_bounds_m", "measurements", "assertions"):
            if environment.get(field) != expected_contract.get(field):
                failures.append(
                    failure(
                        "particle_contract_mismatch",
                        0,
                        {"field": field, "expected": expected_contract.get(field), "actual": environment.get(field)},
                    )
                )
    for frame in frames:
        frame_id = int(frame.get("frame") or 0)
        time_s = float(frame.get("time_s") or 0.0)
        positions = frame.get("positions_m") if isinstance(frame.get("positions_m"), list) else []
        velocities = frame.get("velocities_m_s") if isinstance(frame.get("velocities_m_s"), list) else []
        if time_s <= previous_time:
            failures.append(failure("non_monotonic_time", frame_id, time_s))
        previous_time = time_s
        if len(positions) != expected_count or len(velocities) != expected_count:
            failures.append(failure("particle_count_changed", frame_id, {"positions": len(positions), "velocities": len(velocities), "expected": expected_count}))
        if not finite_vec3_rows(positions) or not finite_vec3_rows(velocities):
            failures.append(failure("non_finite_particle_state", frame_id, None))
        if finite_vec3_rows(positions) and outside_basin(positions, environment):
            failures.append(failure("container_penetration", frame_id, particle_bounds(positions)))
        surface = frame.get("surface") if isinstance(frame.get("surface"), dict) else {}
        if int(surface.get("vertex_count") or 0) <= 0 or int(surface.get("triangle_count") or 0) <= 0:
            failures.append(failure("surface_mesh_empty", frame_id, surface))
        if surface.get("topology_consistent") is False:
            failures.append(failure("surface_topology_invalid", frame_id, surface.get("topology_issue")))
        bounds = surface.get("bounds_m") if isinstance(surface.get("bounds_m"), dict) else None
        if bounds is not None and surface_bounds_outside_basin(bounds, environment):
            failures.append(failure("surface_container_penetration", frame_id, bounds))
        surface_intersection_metric_applied = environment.get("surface_container_intersection_metric") != "not_applied_for_boundary_contacting_fluid"
        if surface_intersection_metric_applied and int(surface.get("rigid_intersection_vertex_count") or 0) > 0:
            failures.append(
                failure(
                    "surface_rigid_intersection",
                    frame_id,
                    int(surface["rigid_intersection_vertex_count"]),
                )
            )
        if root and surface.get("path"):
            path = Path(root) / str(surface["path"])
            if not path.is_file() or path.stat().st_size == 0:
                failures.append(failure("surface_mesh_missing", frame_id, str(path)))
    if cache.get("schema_version") != "harness_particle_cache_v1":
        failures.append(failure("particle_cache_schema", 0, cache.get("schema_version")))
    if expected_count <= 0 or not frames:
        failures.append(failure("particle_cache_empty", 0, {"particle_count": expected_count, "frame_count": len(frames)}))
    surface_volume_error = verify_surface_volume(frames, environment, failures)
    declared_checks = verify_declared_measurements(frames, environment, failures)
    return {
        "schema_version": "harness_particle_cache_report_v1",
        "status": "pass" if not failures else "fail",
        "failure_codes": sorted({item["code"] for item in failures}),
        "failures": failures,
        "checks": {
            "frame_count": len(frames),
            "particle_count": expected_count,
            "stable_particle_count": not any(item["code"] == "particle_count_changed" for item in failures),
            "stable_particle_ids": not any(item["code"] == "particle_ids_unstable" for item in failures),
            "surface_frame_count": sum(1 for frame in frames if int((frame.get("surface") or {}).get("triangle_count") or 0) > 0),
            "surface_topology_consistent": not any(item["code"] == "surface_topology_invalid" for item in failures),
            "container_bounds_respected": not any(item["code"] == "container_penetration" for item in failures),
            "surface_container_bounds_respected": not any(item["code"] == "surface_container_penetration" for item in failures),
            "surface_rigid_intersections_absent": (
                not any(item["code"] == "surface_rigid_intersection" for item in failures)
                if environment.get("surface_container_intersection_metric") != "not_applied_for_boundary_contacting_fluid"
                else None
            ),
            "final_surface_volume_relative_error": surface_volume_error,
            **declared_checks,
        },
    }


def verify_surface_volume(
    frames: list[dict[str, Any]],
    environment: dict[str, Any],
    failures: list[dict[str, Any]],
) -> float | None:
    if not frames:
        return None
    expected = environment.get("initial_liquid_volume_m3")
    tolerances = environment.get("verification_tolerances") if isinstance(environment.get("verification_tolerances"), dict) else {}
    maximum = tolerances.get("surface_volume_relative_error_max")
    surface = frames[-1].get("surface") if isinstance(frames[-1].get("surface"), dict) else {}
    actual = surface.get("enclosed_volume_m3")
    if maximum is None:
        return None
    if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in (expected, maximum, actual)):
        failures.append(failure("surface_volume_evidence_missing", int(frames[-1].get("frame") or 0), None))
        return None
    if float(expected) <= 0.0 or float(maximum) < 0.0:
        failures.append(failure("surface_volume_contract_invalid", int(frames[-1].get("frame") or 0), {"expected": expected, "maximum": maximum}))
        return None
    error = abs(float(actual) - float(expected)) / float(expected)
    if error > float(maximum):
        failures.append(
            failure(
                "surface_volume_error_exceeded",
                int(frames[-1].get("frame") or 0),
                {"relative_error": error, "maximum": float(maximum)},
            )
        )
    return error


def verify_declared_measurements(
    frames: list[dict[str, Any]], environment: dict[str, Any], failures: list[dict[str, Any]]
) -> dict[str, Any]:
    if environment.get("type") != "rigid_sph_scene" or not frames:
        return {}
    declarations = environment.get("measurements") if isinstance(environment.get("measurements"), list) else []
    assertions = environment.get("assertions") if isinstance(environment.get("assertions"), list) else []
    if not declarations or not assertions:
        failures.append(
            failure(
                "declared_measurement_contract_missing",
                0,
                {"measurement_count": len(declarations), "assertion_count": len(assertions)},
            )
        )
        return {"declared_measurements_checked": False}
    series: dict[str, list[float]] = {}
    for declaration in declarations:
        measurement_id = str(declaration.get("id") or "") if isinstance(declaration, dict) else ""
        values: list[float] = []
        for frame in frames:
            measurements = frame.get("measurements") if isinstance(frame.get("measurements"), dict) else {}
            value = measurements.get(measurement_id)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                failures.append(failure("declared_measurement_missing", int(frame.get("frame") or 0), measurement_id))
                values = []
                break
            if declaration.get("type") == "rigid_body_state":
                expected = rigid_body_state_measurement(frame, declaration)
                if expected is None:
                    failures.append(
                        failure(
                            "rigid_body_state_missing",
                            int(frame.get("frame") or 0),
                            {"measurement_id": measurement_id, "body_id": declaration.get("body_id")},
                        )
                    )
                    values = []
                    break
                if abs(float(value) - expected) > 1e-9:
                    failures.append(
                        failure(
                            "rigid_body_state_measurement_mismatch",
                            int(frame.get("frame") or 0),
                            {"measurement_id": measurement_id, "measured": float(value), "state_value": expected},
                        )
                    )
                    values = []
                    break
            values.append(float(value))
        if values:
            series[measurement_id] = values
    reductions: dict[str, float | None] = {}
    assertion_results: list[dict[str, Any]] = []
    for assertion in assertions:
        if not isinstance(assertion, dict):
            continue
        assertion_id = str(assertion.get("id") or "")
        measurement_id = str(assertion.get("measurement_id") or "")
        values = series.get(measurement_id)
        measured = reduce_measurement(values, assertion, frames) if values else None
        reductions[assertion_id] = measured
        expected = float(assertion.get("value") or 0.0)
        operator = str(assertion.get("operator") or "")
        passed = measured is not None and ((measured >= expected) if operator == ">=" else (measured <= expected))
        result = {
            "id": assertion_id,
            "measurement_id": measurement_id,
            "reduction": str(assertion.get("reduction") or ""),
            "operator": operator,
            "expected": expected,
            "measured": measured,
            "passed": passed,
        }
        assertion_results.append(result)
        if not passed:
            failures.append(failure("solver_assertion_failed", int(frames[-1].get("frame") or 0), result))
    return {
        "declared_measurements_checked": True,
        "measurement_reductions": reductions,
        "assertion_results": assertion_results,
    }


def rigid_body_state_measurement(frame: dict[str, Any], declaration: dict[str, Any]) -> float | None:
    states = frame.get("rigid_objects") if isinstance(frame.get("rigid_objects"), dict) else {}
    state = states.get(str(declaration.get("body_id") or ""))
    if not isinstance(state, dict):
        return None
    vector = state.get(str(declaration.get("field") or ""))
    if not (
        isinstance(vector, list)
        and len(vector) == 3
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            for value in vector
        )
    ):
        return None
    values = [float(value) for value in vector]
    component = str(declaration.get("component") or "")
    if component == "magnitude":
        return math.sqrt(sum(value * value for value in values))
    index = {"x": 0, "y": 1, "z": 2}.get(component)
    return values[index] if index is not None else None


def reduce_measurement(values: list[float], assertion: dict[str, Any], frames: list[dict[str, Any]]) -> float | None:
    reduction = str(assertion.get("reduction") or "")
    if reduction == "initial":
        return values[0]
    if reduction == "final":
        return values[-1]
    if reduction == "max":
        return max(values)
    if reduction == "min":
        return min(values)
    if reduction == "initial_minus_final":
        return values[0] - values[-1]
    if reduction == "max_frame_decrease":
        return max((before - after for before, after in zip(values, values[1:], strict=False)), default=0.0)
    if reduction == "threshold_crossing_duration":
        start_delta = float(assertion.get("start_delta") or 0.0)
        end_value = float(assertion.get("end_value") or 0.0)
        start = next((index for index, value in enumerate(values) if value <= values[0] - start_delta), None)
        end = next((index for index, value in enumerate(values) if value <= end_value), None)
        if start is None or end is None or end < start:
            return None
        return float(frames[end].get("time_s") or 0.0) - float(frames[start].get("time_s") or 0.0)
    return None


def finite_vec3_rows(rows: list[Any]) -> bool:
    return all(isinstance(row, list) and len(row) == 3 and all(math.isfinite(float(value)) for value in row) for row in rows)


def outside_basin(rows: list[Any], environment: dict[str, Any]) -> bool:
    bounds = environment.get("workspace_bounds_m") if isinstance(environment.get("workspace_bounds_m"), dict) else {}
    minimum = bounds.get("min_m") if isinstance(bounds.get("min_m"), list) else []
    maximum = bounds.get("max_m") if isinstance(bounds.get("max_m"), list) else []
    if len(minimum) != 3 or len(maximum) != 3:
        return True
    tolerance = float(environment.get("penetration_tolerance_m") or 0.0)
    return any(
        any(
            float(row[axis]) < float(minimum[axis]) - tolerance
            or float(row[axis]) > float(maximum[axis]) + tolerance
            for axis in range(3)
        )
        for row in rows
    )


def particle_bounds(rows: list[Any]) -> dict[str, list[float]]:
    return {
        "min_m": [min(float(row[axis]) for row in rows) for axis in range(3)],
        "max_m": [max(float(row[axis]) for row in rows) for axis in range(3)],
    }


def surface_bounds_outside_basin(bounds: dict[str, Any], environment: dict[str, Any]) -> bool:
    workspace = environment.get("workspace_bounds_m") if isinstance(environment.get("workspace_bounds_m"), dict) else {}
    allowed_minimum = workspace.get("min_m") if isinstance(workspace.get("min_m"), list) else []
    allowed_maximum = workspace.get("max_m") if isinstance(workspace.get("max_m"), list) else []
    minimum = bounds.get("min_m") if isinstance(bounds.get("min_m"), list) else []
    maximum = bounds.get("max_m") if isinstance(bounds.get("max_m"), list) else []
    if any(len(value) != 3 for value in (allowed_minimum, allowed_maximum, minimum, maximum)):
        return True
    tolerance = 1e-6
    return any(
        float(minimum[axis]) < float(allowed_minimum[axis]) - tolerance
        or float(maximum[axis]) > float(allowed_maximum[axis]) + tolerance
        for axis in range(3)
    )


def failure(code: str, frame: int, value: Any) -> dict[str, Any]:
    return {"code": code, "frame": frame, "value": value}
