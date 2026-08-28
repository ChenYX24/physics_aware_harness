from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path
from typing import Any

from harness.core.artifact_schema import read_json, write_json
from harness.runtime.rigid_sph_scene import evaluate_measurements


class FluidSurfaceAdapterError(RuntimeError):
    def __init__(self, message: str, *, code: str = "fluid_surface_adapter_failed") -> None:
        super().__init__(message)
        self.code = code


def ue_position_m(position: list[float]) -> list[float]:
    if len(position) != 3:
        raise FluidSurfaceAdapterError("Genesis-to-UE position must contain three values")
    return [float(position[0]), -float(position[1]), float(position[2])]


def particle_centers_m(cache: dict[str, Any]) -> list[list[float]]:
    """Return one canonical particle center per contiguous output frame."""
    frames = cache.get("frames") if isinstance(cache.get("frames"), list) else []
    if not frames:
        raise FluidSurfaceAdapterError("particle cache has no frames")
    centers: list[list[float]] = []
    for expected_index, frame in enumerate(frames):
        if not isinstance(frame, dict) or int(frame.get("frame") or 0) != expected_index:
            raise FluidSurfaceAdapterError("particle frames must be contiguous and start at zero")
        positions = frame.get("positions_m") if isinstance(frame.get("positions_m"), list) else []
        if not positions:
            raise FluidSurfaceAdapterError(f"particle positions are missing for frame {expected_index}")
        try:
            center = [sum(float(row[channel]) for row in positions) / len(positions) for channel in range(3)]
        except (IndexError, TypeError, ValueError) as exc:
            raise FluidSurfaceAdapterError(f"invalid particle positions for frame {expected_index}") from exc
        centers.append([round(value, 8) for value in center])
    return centers


def prepare_ue_surface_replay(
    particle_cache_path: str | Path,
    destination: str | Path,
    *,
    ue_asset_root: str,
    spatial_measurement_tolerance: float,
) -> dict[str, Any]:
    """Convert Genesis OBJ frames into an explicit UE replay contract.

    Particle state remains truth. The generated OBJ files are a render adapter:
    metres become centimetres, Genesis +Y becomes UE -Y, and triangle winding is
    reversed so the handedness conversion does not invert surface normals.
    """
    particle_cache_path = Path(particle_cache_path).expanduser().resolve()
    source_root = particle_cache_path.parent
    destination = Path(destination).expanduser().resolve()
    frame_root = destination / "obj_frames_cm_lh"
    frame_root.mkdir(parents=True, exist_ok=True)
    cache = read_json(particle_cache_path)
    frames = cache.get("frames") if isinstance(cache.get("frames"), list) else []
    if not frames:
        raise FluidSurfaceAdapterError("particle cache has no frames")
    fps = int(((cache.get("timebase") or {}).get("fps") or 0))
    if fps <= 0:
        raise FluidSurfaceAdapterError("particle cache has no positive FPS")
    spatial_registration = validate_spatial_registration(
        cache,
        tolerance=spatial_measurement_tolerance,
    )

    replay_frames: list[dict[str, Any]] = []
    for expected_index, frame in enumerate(frames):
        if not isinstance(frame, dict) or int(frame.get("frame") or 0) != expected_index:
            raise FluidSurfaceAdapterError("surface frames must be contiguous and start at zero")
        surface = frame.get("surface") if isinstance(frame.get("surface"), dict) else {}
        source_relative = str(surface.get("path") or "")
        source = (source_root / source_relative).resolve()
        if not source.is_file() or not source_relative:
            raise FluidSurfaceAdapterError(f"surface OBJ is missing for frame {expected_index}: {source}")
        output = frame_root / f"frame_{expected_index:04d}.obj"
        vertex_count, face_count = convert_obj_m_rh_to_cm_lh(source, output)
        declared_vertices = int(surface.get("vertex_count") or 0)
        declared_faces = int(surface.get("triangle_count") or 0)
        if vertex_count != declared_vertices or face_count != declared_faces:
            raise FluidSurfaceAdapterError(
                f"surface OBJ count mismatch at frame {expected_index}: "
                f"declared=({declared_vertices},{declared_faces}), parsed=({vertex_count},{face_count})"
            )
        replay_frames.append(
            {
                "frame": expected_index,
                "time_s": round(float(frame.get("time_s") or 0.0), 8),
                "source_surface": source_relative,
                "ue_obj": output.relative_to(destination).as_posix(),
                "ue_asset_path": f"{ue_asset_root.rstrip('/')}/SM_Fluid_{expected_index:04d}",
                "vertex_count": vertex_count,
                "triangle_count": face_count,
                "sha256": file_sha256(output),
            }
        )

    manifest = {
        "schema_version": "harness_fluid_surface_replay_v1",
        "adapter": "genesis_obj_sequence_to_ue_static_mesh_swap_v1",
        "state_truth": str(particle_cache_path),
        "state_truth_sha256": file_sha256(particle_cache_path),
        "surface_truth_role": "derived_render_representation",
        "coordinate_transform": {
            "source": "Genesis right-handed z-up metres",
            "target": "Unreal left-handed z-up centimetres",
            "position": "(x, y, z) m -> (100*x, -100*y, 100*z) cm",
            "triangle_winding": "reversed",
            "applies_to": ["surface_vertices", "particle_positions", "rigid_body_positions"],
        },
        "spatial_registration": spatial_registration,
        "timebase": {
            "fps": fps,
            "frame_count": len(replay_frames),
            "frame_selection": "surface frame index equals render frame index",
        },
        "ue": {
            "asset_root": ue_asset_root.rstrip("/"),
            "actor_id": "fluid_surface",
            "collision_enabled": False,
            "segmentation_id_policy": "one stable instance id for every mesh frame",
            "material_role": "water_translucent",
            "replay_method": "swap preimported static mesh at the render-frame seam",
        },
        "frames": replay_frames,
    }
    write_json(destination / "fluid_surface_replay.json", manifest)
    return manifest


def convert_obj_m_rh_to_cm_lh(source: Path, destination: Path) -> tuple[int, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    vertex_count = 0
    face_count = 0
    output: list[str] = ["# Harness generated Genesis-to-UE surface frame\n"]
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.startswith("v "):
            parts = line.split()
            if len(parts) < 4:
                raise FluidSurfaceAdapterError(f"invalid OBJ vertex in {source}")
            converted = ue_position_m([float(parts[index]) for index in (1, 2, 3)])
            output.append(f"v {converted[0] * 100.0:.8f} {converted[1] * 100.0:.8f} {converted[2] * 100.0:.8f}\n")
            vertex_count += 1
        elif line.startswith("f "):
            parts = line.split()
            if len(parts) != 4:
                raise FluidSurfaceAdapterError(f"only triangular OBJ faces are supported: {source}")
            output.append(f"f {parts[1]} {parts[3]} {parts[2]}\n")
            face_count += 1
        elif line.startswith(("vn ", "vt ")):
            continue
        elif line.strip() and not line.startswith("#"):
            output.append(line + "\n")
    destination.write_text("".join(output), encoding="utf-8")
    return vertex_count, face_count


def validate_spatial_registration(cache: dict[str, Any], *, tolerance: float) -> dict[str, Any]:
    if not math.isfinite(float(tolerance)) or float(tolerance) < 0.0:
        raise FluidSurfaceAdapterError("handoff spatial measurement tolerance must be finite and non-negative")
    environment = cache.get("environment") if isinstance(cache.get("environment"), dict) else {}
    definitions = [item for item in environment.get("measurements") or [] if isinstance(item, dict)]
    if not definitions:
        return {
            "status": "not_required",
            "absolute_tolerance": float(tolerance),
            "measurement_count": 0,
            "sample_count": 0,
            "maximum_absolute_error": 0.0,
        }
    body_templates = {
        str(body.get("id")): body
        for body in environment.get("rigid_bodies") or []
        if isinstance(body, dict) and body.get("id")
    }
    failures: list[dict[str, Any]] = []
    sample_count = 0
    maximum_error = 0.0
    for frame in cache.get("frames") or []:
        frame_index = int(frame.get("frame") or 0)
        positions = frame.get("positions_m") if isinstance(frame.get("positions_m"), list) else []
        states = frame.get("rigid_objects") if isinstance(frame.get("rigid_objects"), dict) else {}
        declared = frame.get("measurements") if isinstance(frame.get("measurements"), dict) else {}
        bodies: dict[str, dict[str, Any]] = {}
        for body_id, template in body_templates.items():
            body = copy.deepcopy(template)
            state = states.get(body_id) if isinstance(states.get(body_id), dict) else {}
            transform = body.get("transform") if isinstance(body.get("transform"), dict) else {}
            if len(state.get("position_m") or []) == 3:
                transform["position_m"] = list(state["position_m"])
            if len(state.get("solver_rotation_xyz_deg") or []) == 3:
                transform["euler_xyz_deg"] = list(state["solver_rotation_xyz_deg"])
            if len(state.get("ue_rotation_pyr_deg") or []) == 3:
                transform["ue_rotation_pyr_deg"] = list(state["ue_rotation_pyr_deg"])
            body["transform"] = transform
            bodies[body_id] = body
        try:
            measured = evaluate_measurements(positions, bodies, states, definitions)
        except (KeyError, TypeError, ValueError) as exc:
            raise FluidSurfaceAdapterError(
                f"cannot recompute handoff spatial measurements at frame {frame_index}: {exc}",
                code="handoff_spatial_registration_failed",
            ) from exc
        for definition in definitions:
            measurement_id = str(definition.get("id") or "")
            if measurement_id not in declared or measurement_id not in measured:
                failures.append({"frame": frame_index, "measurement_id": measurement_id, "code": "measurement_missing"})
                continue
            error = abs(float(declared[measurement_id]) - float(measured[measurement_id]))
            maximum_error = max(maximum_error, error)
            sample_count += 1
            if not math.isfinite(error) or error > float(tolerance):
                failures.append(
                    {
                        "frame": frame_index,
                        "measurement_id": measurement_id,
                        "code": "measurement_mismatch",
                        "declared": float(declared[measurement_id]),
                        "recomputed": float(measured[measurement_id]),
                        "absolute_error": error,
                    }
                )
    report = {
        "status": "fail" if failures else "pass",
        "absolute_tolerance": float(tolerance),
        "measurement_count": len(definitions),
        "sample_count": sample_count,
        "maximum_absolute_error": maximum_error,
        "failures": failures,
    }
    if failures:
        raise FluidSurfaceAdapterError(
            f"Genesis-to-UE spatial registration exceeds handoff tolerance: {failures[0]}",
            code="handoff_spatial_registration_failed",
        )
    return report


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
