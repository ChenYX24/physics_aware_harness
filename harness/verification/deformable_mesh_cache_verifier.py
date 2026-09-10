from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import numpy as np

from harness.runtime.deformable_surface_adapter import collider_position_m, collider_world_from_local_columns


def verify_deformable_mesh_cache(
    arrays: Mapping[str, Any],
    *,
    expected: Mapping[str, Any],
    colliders: list[Mapping[str, Any]] | None,
    floor_z_m: float | None,
    collision_thickness_m: float,
    pinned_indices: list[int] | None = None,
    wind_axis: list[float] | None = None,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    positions = np.asarray(arrays.get("positions_m"))
    velocities = np.asarray(arrays.get("velocities_m_s"))
    faces = np.asarray(arrays.get("faces"))
    edges = np.asarray(arrays.get("structural_edges"))
    times = np.asarray(arrays.get("times_s"))

    valid_shape = (
        positions.ndim == 3
        and positions.shape[-1] == 3
        and positions.shape[0] > 0
        and positions.shape[1] > 0
        and velocities.shape == positions.shape
        and faces.ndim == 2
        and faces.shape[-1] == 3
        and edges.ndim == 2
        and edges.shape[-1] == 2
        and times.shape == (positions.shape[0],)
    )
    if not valid_shape:
        return report(failures + [failure("deformable_cache_shape", {
            "positions": list(positions.shape),
            "velocities": list(velocities.shape),
            "faces": list(faces.shape),
            "edges": list(edges.shape),
            "times": list(times.shape),
        })])

    vertex_count = positions.shape[1]
    if not np.isfinite(positions).all() or not np.isfinite(velocities).all() or not np.isfinite(times).all():
        failures.append(failure("non_finite_vertex_state", None))
    if len(times) < 2 or not np.all(np.diff(times) > 0.0):
        failures.append(failure("cache_timebase_mismatch", times.tolist()))
    faces_invalid = faces.size == 0 or int(faces.min()) < 0 or int(faces.max()) >= vertex_count
    edges_invalid = edges.size == 0 or int(edges.min()) < 0 or int(edges.max()) >= vertex_count
    if faces_invalid:
        failures.append(failure("topology_changed", {"vertex_count": vertex_count, "face_count": len(faces)}))
    if edges_invalid:
        failures.append(failure("structural_edges_invalid", {"vertex_count": vertex_count, "edge_count": len(edges)}))
    if faces_invalid or edges_invalid:
        return report(
            failures,
            checks={
                "frame_count": int(positions.shape[0]),
                "vertex_count": int(vertex_count),
                "triangle_count": int(faces.shape[0]),
                "structural_edge_count": int(edges.shape[0]),
            },
        )

    rest = np.linalg.norm(positions[0, edges[:, 0]] - positions[0, edges[:, 1]], axis=1)
    lengths = np.linalg.norm(positions[:, edges[:, 0]] - positions[:, edges[:, 1]], axis=2)
    maximum_stretch = float(np.max(lengths / np.maximum(rest[None, :], 1e-8)))
    maximum_allowed_stretch = float(expected.get("maximum_structural_edge_stretch_ratio") or 0.0)
    if maximum_allowed_stretch > 0.0 and maximum_stretch > maximum_allowed_stretch:
        failures.append(failure("edge_stretch_exceeded", maximum_stretch))

    penetration_tolerance = float(expected.get("penetration_tolerance_m") or 0.0)
    collider_checks: list[dict[str, Any]] = []
    collider_frame_contacts = np.zeros(positions.shape[0], dtype=bool)
    first_sphere_clearance = None
    first_sphere_contact_frames = None
    for collider in colliders or []:
        clearances = analytic_collider_clearances(positions, times, collider)
        minimum_clearance = float(np.min(clearances))
        contact_limit = float(collision_thickness_m) + penetration_tolerance
        frame_contacts = np.min(clearances, axis=1) <= contact_limit
        contact_frames = int(np.count_nonzero(frame_contacts))
        collider_frame_contacts |= frame_contacts
        check = {
            "object_id": str(collider.get("id") or ""),
            "shape": str(collider.get("shape") or ""),
            "minimum_clearance_m": minimum_clearance,
            "contact_frame_count": contact_frames,
        }
        collider_checks.append(check)
        if minimum_clearance < -penetration_tolerance:
            failures.append(failure("rigid_collider_penetration", check))
        if check["shape"] == "sphere" and first_sphere_clearance is None:
            first_sphere_clearance = minimum_clearance
            first_sphere_contact_frames = contact_frames

    total_collider_contact_frames = int(np.count_nonzero(collider_frame_contacts))
    minimum_collider_contact_frames = int(expected.get("minimum_collider_contact_frames") or 0)
    if total_collider_contact_frames < minimum_collider_contact_frames:
        failures.append(failure("collider_contact_missing", total_collider_contact_frames))
    minimum_sphere_contact_frames = int(expected.get("minimum_sphere_contact_frames") or 0)
    if minimum_sphere_contact_frames > 0 and (first_sphere_contact_frames or 0) < minimum_sphere_contact_frames:
        failures.append(failure("sphere_contact_missing", first_sphere_contact_frames or 0))
    minimum_by_collider = expected.get("minimum_contact_frames_by_collider")
    if isinstance(minimum_by_collider, Mapping):
        checks_by_id = {str(check["object_id"]): check for check in collider_checks}
        for object_id, required_count in minimum_by_collider.items():
            check = checks_by_id.get(str(object_id))
            if check is None:
                failures.append(failure("declared_collider_missing", str(object_id)))
            elif int(check["contact_frame_count"]) < int(required_count):
                failures.append(failure("collider_contact_missing", {
                    "object_id": str(object_id),
                    "required": int(required_count),
                    "observed": int(check["contact_frame_count"]),
                }))

    floor_clearance = None
    if floor_z_m is not None:
        floor_clearance = float(np.min(positions[:, :, 2] - float(floor_z_m)))
        if floor_clearance < -penetration_tolerance:
            failures.append(failure("floor_penetration", floor_clearance))
    mean_z = np.mean(positions[:, :, 2], axis=1)
    downward_displacement = float(mean_z[0] - np.min(mean_z))
    minimum_downward = float(expected.get("minimum_mean_downward_displacement_m") or 0.0)
    if downward_displacement < minimum_downward:
        failures.append(failure("gravity_response_missing", downward_displacement))
    final_vertical_span = float(np.ptp(positions[-1, :, 2]))
    minimum_vertical_span = float(expected.get("minimum_final_vertical_span_m") or 0.0)
    if final_vertical_span < minimum_vertical_span:
        failures.append(failure("drape_shape_missing", final_vertical_span))
    final_mean_speed = float(np.mean(np.linalg.norm(velocities[-1], axis=1)))
    maximum_final_mean_speed = float(expected.get("maximum_final_mean_speed_m_s") or 0.0)
    if maximum_final_mean_speed > 0.0 and final_mean_speed > maximum_final_mean_speed:
        failures.append(failure("post_event_motion_too_high", final_mean_speed))

    maximum_pinned_displacement = None
    if pinned_indices:
        pinned = np.asarray(pinned_indices, dtype=np.int64)
        if int(pinned.min()) < 0 or int(pinned.max()) >= vertex_count:
            failures.append(failure("pinned_indices_invalid", pinned.tolist()))
        else:
            pinned_displacements = np.linalg.norm(positions[:, pinned] - positions[0, pinned], axis=2)
            maximum_pinned_displacement = float(np.max(pinned_displacements))
            maximum_allowed_pinned = float(expected.get("maximum_pinned_vertex_displacement_m") or 0.0)
            if maximum_allowed_pinned > 0.0 and maximum_pinned_displacement > maximum_allowed_pinned:
                failures.append(failure("pinned_constraint_drift", maximum_pinned_displacement))

    maximum_wind_displacement = None
    if wind_axis is not None:
        axis = np.asarray(wind_axis, dtype=np.float64)
        axis_norm = float(np.linalg.norm(axis))
        if axis.shape != (3,) or axis_norm <= 1e-8:
            failures.append(failure("wind_axis_invalid", wind_axis))
        else:
            axis /= axis_norm
            displacement = positions - positions[0, None, :, :]
            maximum_wind_displacement = float(np.max(np.mean(displacement @ axis, axis=1)))
            minimum_wind_displacement = float(expected.get("minimum_mean_wind_axis_displacement_m") or 0.0)
            if maximum_wind_displacement < minimum_wind_displacement:
                failures.append(failure("wind_response_missing", maximum_wind_displacement))

    return report(
        failures,
        checks={
            "frame_count": int(positions.shape[0]),
            "vertex_count": int(vertex_count),
            "triangle_count": int(faces.shape[0]),
            "structural_edge_count": int(edges.shape[0]),
            "maximum_structural_edge_stretch_ratio": maximum_stretch,
            "analytic_colliders": collider_checks,
            "collider_contact_frame_count": total_collider_contact_frames,
            "minimum_sphere_clearance_m": first_sphere_clearance,
            "sphere_contact_frame_count": first_sphere_contact_frames,
            "minimum_floor_clearance_m": floor_clearance,
            "maximum_mean_downward_displacement_m": downward_displacement,
            "final_vertical_span_m": final_vertical_span,
            "final_mean_speed_m_s": final_mean_speed,
            "maximum_pinned_vertex_displacement_m": maximum_pinned_displacement,
            "maximum_mean_wind_axis_displacement_m": maximum_wind_displacement,
        },
    )


def verify_deformable_mesh_cache_file(
    path: str | Path,
    **kwargs: Any,
) -> dict[str, Any]:
    with np.load(Path(path), allow_pickle=False) as arrays:
        return verify_deformable_mesh_cache(arrays, **kwargs)


def analytic_collider_clearances(
    positions: np.ndarray,
    times: np.ndarray,
    collider: Mapping[str, Any],
) -> np.ndarray:
    centers = np.asarray(
        [collider_position_m(dict(collider), float(time_s)) for time_s in times],
        dtype=np.float64,
    )
    world_from_local = np.asarray(
        [collider_world_from_local_columns(dict(collider), float(time_s)) for time_s in times],
        dtype=np.float64,
    )
    local = np.einsum("fvi,fji->fvj", positions - centers[:, None, :], world_from_local)
    half_size = np.asarray(collider["size_m"], dtype=np.float64) / 2.0
    shape = str(collider.get("shape") or "")
    if shape == "sphere":
        return np.linalg.norm(local, axis=2) - half_size[0]
    if shape == "box":
        delta = np.abs(local) - half_size[None, None, :]
        outside = np.linalg.norm(np.maximum(delta, 0.0), axis=2)
        inside = np.minimum(np.max(delta, axis=2), 0.0)
        return outside + inside
    if shape == "cylinder":
        delta = np.stack(
            (
                np.linalg.norm(local[:, :, :2], axis=2) - half_size[0],
                np.abs(local[:, :, 2]) - half_size[2],
            ),
            axis=2,
        )
        outside = np.linalg.norm(np.maximum(delta, 0.0), axis=2)
        inside = np.minimum(np.max(delta, axis=2), 0.0)
        return outside + inside
    raise ValueError(f"unsupported analytic collider shape: {shape}")


def verify_deformable_solid_impact(
    arrays: Mapping[str, Any],
    *,
    expected: Mapping[str, Any],
    floor_z_m: float,
) -> dict[str, Any]:
    base = verify_deformable_mesh_cache(
        arrays,
        expected=expected,
        colliders=None,
        floor_z_m=floor_z_m,
        collision_thickness_m=0.0,
    )
    failures = list(base["failures"])
    checks = dict(base["checks"])
    positions = np.asarray(arrays.get("positions_m"))
    tetrahedra = np.asarray(arrays.get("tetrahedra"))
    vertex_count = positions.shape[1] if positions.ndim == 3 else 0
    if (
        tetrahedra.ndim != 2
        or tetrahedra.shape[-1] != 4
        or tetrahedra.size == 0
        or int(tetrahedra.min()) < 0
        or int(tetrahedra.max()) >= vertex_count
    ):
        failures.append(failure("tetrahedral_topology_invalid", list(tetrahedra.shape)))
        return solid_report(failures, checks)

    vertices = positions[:, tetrahedra]
    signed_six_volume = np.einsum(
        "fti,fti->ft",
        vertices[:, :, 1] - vertices[:, :, 0],
        np.cross(vertices[:, :, 2] - vertices[:, :, 0], vertices[:, :, 3] - vertices[:, :, 0]),
    )
    volumes = np.sum(np.abs(signed_six_volume), axis=1) / 6.0
    initial_volume = float(volumes[0])
    if initial_volume <= 1e-10:
        failures.append(failure("initial_solid_volume_invalid", initial_volume))
        return solid_report(failures, checks)
    maximum_volume_error = float(np.max(np.abs(volumes - initial_volume) / initial_volume))
    maximum_allowed_volume_error = float(expected.get("maximum_relative_volume_error") or 0.0)
    if maximum_allowed_volume_error > 0.0 and maximum_volume_error > maximum_allowed_volume_error:
        failures.append(failure("solid_volume_error_exceeded", maximum_volume_error))

    vertical_spans = np.ptp(positions[:, :, 2], axis=1)
    initial_span = float(vertical_spans[0])
    maximum_compression = float(1.0 - np.min(vertical_spans) / max(initial_span, 1e-10))
    minimum_compression = float(expected.get("minimum_compression_ratio") or 0.0)
    maximum_allowed_compression = float(expected.get("maximum_compression_ratio") or 0.0)
    if maximum_compression < minimum_compression:
        failures.append(failure("solid_compression_missing", maximum_compression))
    if maximum_allowed_compression > 0.0 and maximum_compression > maximum_allowed_compression:
        failures.append(failure("solid_compression_exceeded", maximum_compression))

    tolerance = float(expected.get("penetration_tolerance_m") or 0.0)
    minimum_z = np.min(positions[:, :, 2], axis=1)
    contact_indices = np.flatnonzero(minimum_z <= float(floor_z_m) + tolerance)
    contact_frames = int(len(contact_indices))
    minimum_contact_frames = int(expected.get("minimum_floor_contact_frames") or 0)
    if contact_frames < minimum_contact_frames:
        failures.append(failure("floor_contact_missing", contact_frames))
    first_contact_frame = int(contact_indices[0]) if contact_frames else None
    centers_z = np.mean(positions[:, :, 2], axis=1)
    rebound_center_height = (
        float(np.max(centers_z[first_contact_frame + 1 :]))
        if first_contact_frame is not None and first_contact_frame + 1 < len(centers_z)
        else None
    )
    minimum_rebound = float(expected.get("minimum_rebound_center_height_m") or 0.0)
    if minimum_rebound > 0.0 and (rebound_center_height is None or rebound_center_height < minimum_rebound):
        failures.append(failure("solid_rebound_missing", rebound_center_height))

    checks.update({
        "tetrahedron_count": int(len(tetrahedra)),
        "initial_volume_m3": initial_volume,
        "maximum_relative_volume_error": maximum_volume_error,
        "maximum_compression_ratio": maximum_compression,
        "floor_contact_frame_count": contact_frames,
        "first_floor_contact_frame": first_contact_frame,
        "rebound_center_height_m": rebound_center_height,
    })
    return solid_report(failures, checks)


def verify_deformable_solid_impact_file(path: str | Path, **kwargs: Any) -> dict[str, Any]:
    with np.load(Path(path), allow_pickle=False) as arrays:
        return verify_deformable_solid_impact(arrays, **kwargs)


def verify_wind_speed_response_matrix(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Verify a three-or-more-point OFAT wind-speed response."""
    failures: list[dict[str, Any]] = []
    observations = sorted(
        (
            {
                "wind_speed_m_s": float(row["wind_speed_m_s"]),
                "maximum_mean_wind_axis_displacement_m": float(
                    row["maximum_mean_wind_axis_displacement_m"]
                ),
                "verifier_status": str(row["verifier_status"]),
            }
            for row in rows
        ),
        key=lambda row: row["wind_speed_m_s"],
    )
    speeds = [row["wind_speed_m_s"] for row in observations]
    responses = [row["maximum_mean_wind_axis_displacement_m"] for row in observations]
    if len(observations) < 3:
        failures.append(failure("insufficient_wind_speed_conditions", len(observations)))
    if len(set(speeds)) != len(speeds):
        failures.append(failure("duplicate_wind_speed", speeds))
    failed_runs = [row for row in observations if row["verifier_status"] != "pass"]
    if failed_runs:
        failures.append(failure("constituent_run_failed", failed_runs))
    if any(next_response <= response for response, next_response in zip(responses, responses[1:])):
        failures.append(failure("wind_response_not_monotonic", observations))
    return {
        "schema_version": "harness_wind_speed_response_matrix_report_v1",
        "status": "pass" if not failures else "fail",
        "failure_codes": sorted({item["code"] for item in failures}),
        "failures": failures,
        "checks": {"conditions": observations},
    }


def verify_stiffness_response_matrix(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    observations = sorted(
        ({
            "youngs_modulus_pa": float(row["youngs_modulus_pa"]),
            "maximum_compression_ratio": float(row["maximum_compression_ratio"]),
            "verifier_status": str(row["verifier_status"]),
        } for row in rows),
        key=lambda row: row["youngs_modulus_pa"],
    )
    moduli = [row["youngs_modulus_pa"] for row in observations]
    compressions = [row["maximum_compression_ratio"] for row in observations]
    if len(observations) < 3:
        failures.append(failure("insufficient_stiffness_conditions", len(observations)))
    if len(set(moduli)) != len(moduli):
        failures.append(failure("duplicate_youngs_modulus", moduli))
    failed_runs = [row for row in observations if row["verifier_status"] != "pass"]
    if failed_runs:
        failures.append(failure("constituent_run_failed", failed_runs))
    if any(next_value >= value for value, next_value in zip(compressions, compressions[1:])):
        failures.append(failure("compression_response_not_monotonic", observations))
    return {
        "schema_version": "harness_stiffness_response_matrix_report_v1",
        "status": "pass" if not failures else "fail",
        "failure_codes": sorted({item["code"] for item in failures}),
        "failures": failures,
        "checks": {"conditions": observations},
    }


def failure(code: str, observed: Any) -> dict[str, Any]:
    return {"code": code, "observed": observed}


def report(failures: list[dict[str, Any]], *, checks: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "harness_deformable_mesh_cache_report_v1",
        "status": "pass" if not failures else "fail",
        "failure_codes": sorted({item["code"] for item in failures}),
        "failures": failures,
        "checks": checks or {},
    }


def solid_report(failures: list[dict[str, Any]], checks: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness_deformable_solid_impact_report_v1",
        "status": "pass" if not failures else "fail",
        "failure_codes": sorted({item["code"] for item in failures}),
        "failures": failures,
        "checks": checks,
    }
