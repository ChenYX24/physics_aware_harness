from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.core.artifact_schema import read_json, write_json
from harness.core.case_spec import load_case_spec
from harness.core.runtime_case import RUNTIME_CASE_SCHEMA_VERSION, RuntimeCase
from harness.core.scene_layout import rotate_local_vector_ue
from harness.runtime.fluid_surface_adapter import convert_obj_m_rh_to_cm_lh, file_sha256
from harness.runtime.kinematic_motion import sample_linear_kinematic_position, sample_linear_kinematic_rotation


class DeformableSurfaceAdapterError(RuntimeError):
    pass


def load_deformable_case_data(path: str | Path) -> dict[str, Any]:
    raw = read_json(Path(path))
    if not isinstance(raw, dict):
        raise ValueError("deformable case root must be an object")
    if raw.get("schema_version") == RUNTIME_CASE_SCHEMA_VERSION:
        return RuntimeCase(raw).data
    if raw.get("schema_version") == "harness_case_spec_v1":
        return load_case_spec(path).data
    raise ValueError("deformable backend requires a runtime CaseSpec V2 or legacy CaseSpec V1")


def object_position_m(
    obj: dict[str, Any],
    default: list[float],
    *,
    legacy_field: str = "position_m",
) -> list[float]:
    value = obj.get("initial_position_m")
    if value is None:
        value = obj.get(legacy_field)
    return [float(component) for component in (value if value is not None else default)]


def support_height_m(obj: dict[str, Any]) -> float:
    if obj.get("z_m") is not None:
        return float(obj["z_m"])
    return object_position_m(obj, [0.0, 0.0, 0.0])[2]


def objects_with_role(case_data: dict[str, Any], role: str) -> list[dict[str, Any]]:
    return [
        item
        for item in case_data.get("objects") or []
        if isinstance(item, dict) and item.get("role") == role
    ]


def deformable_analytic_colliders(case_data: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for obj in objects_with_role(case_data, "rigid_collider"):
        shape = str(obj.get("shape") or "").casefold()
        size = [float(value) for value in obj.get("size_m") or []]
        if shape not in {"sphere", "box", "cylinder"}:
            raise ValueError(f"unsupported deformable analytic collider shape: {shape}")
        if len(size) != 3 or min(size) <= 0.0:
            raise ValueError("deformable analytic collider size_m must contain three positive values")
        rotation = [float(value) for value in obj.get("initial_rotation_deg") or [0.0, 0.0, 0.0]]
        world_from_local = [
            rotate_local_vector_ue(
                [1.0 if axis == basis else 0.0 for axis in range(3)],
                rotation,
            )
            for basis in range(3)
        ]
        result.append({
            "id": str(obj.get("id") or ""),
            "shape": shape,
            "size_m": size,
            "initial_position_m": object_position_m(obj, [0.0, 0.0, 0.0]),
            "initial_rotation_deg": rotation,
            "world_from_local_columns": world_from_local,
            "dynamic_friction": object_material_number(obj, "dynamic_friction", legacy_field="friction"),
            "restitution": object_material_number(obj, "restitution"),
            "kinematic_motion": dict(obj["kinematic_motion"]) if isinstance(obj.get("kinematic_motion"), dict) else None,
        })
    return result


def collider_position_m(collider: dict[str, Any], time_s: float) -> list[float]:
    motion = collider.get("kinematic_motion")
    if isinstance(motion, dict):
        return sample_linear_kinematic_position(motion, time_s)
    return [float(value) for value in collider["initial_position_m"]]


def collider_rotation_deg(collider: dict[str, Any], time_s: float) -> list[float]:
    motion = collider.get("kinematic_motion")
    initial = [float(value) for value in collider.get("initial_rotation_deg") or [0.0, 0.0, 0.0]]
    if isinstance(motion, dict):
        return sample_linear_kinematic_rotation(motion, time_s, initial)
    return initial


def collider_world_from_local_columns(collider: dict[str, Any], time_s: float) -> list[list[float]]:
    rotation = collider_rotation_deg(collider, time_s)
    return [
        rotate_local_vector_ue(
            [1.0 if axis == basis else 0.0 for axis in range(3)],
            rotation,
        )
        for basis in range(3)
    ]


def object_material_number(
    obj: dict[str, Any] | None,
    field: str,
    *,
    legacy_field: str | None = None,
) -> float:
    if not obj:
        return 0.0
    material = obj.get("material") if isinstance(obj.get("material"), dict) else {}
    value = material.get(field)
    if value is None and legacy_field is not None:
        value = obj.get(legacy_field)
    if value is None:
        value = obj.get(field)
    return float(value or 0.0)


def wind_field_parameters(wind: dict[str, Any] | None, target_id: str) -> dict[str, Any] | None:
    if not wind:
        return None
    solver = wind.get("solver") if isinstance(wind.get("solver"), dict) else {}
    if solver.get("type") == "aerodynamic_wind":
        target = next(
            (
                item
                for item in solver.get("targets") or []
                if isinstance(item, dict) and str(item.get("object") or "") == target_id
            ),
            None,
        )
        if target is None:
            return None
        return {
            "velocity_m_s": [float(value) for value in solver["velocity_m_s"]],
            "air_density_kg_m3": float(solver["air_density_kg_m3"]),
            "drag_coefficient": float(target["drag_coefficient"]),
            "gust_amplitude": float(solver["gust_amplitude"]),
            "gust_frequency_hz": float(solver["gust_frequency_hz"]),
        }
    return {
        "velocity_m_s": [float(value) for value in wind.get("velocity_m_s") or [0.0, 0.0, 0.0]],
        "air_density_kg_m3": float(wind.get("air_density_kg_m3") or 1.225),
        "drag_coefficient": float(wind.get("drag_coefficient") or 1.0),
        "gust_amplitude": float(wind.get("gust_amplitude") or 0.0),
        "gust_frequency_hz": float(wind.get("gust_frequency_hz") or 0.0),
    }


def prepare_ue_deformable_replay(
    cache_manifest_path: str | Path,
    destination: str | Path,
    *,
    ue_asset_root: str,
) -> dict[str, Any]:
    cache_manifest_path = Path(cache_manifest_path).expanduser().resolve()
    source_root = cache_manifest_path.parent
    destination = Path(destination).expanduser().resolve()
    output_root = destination / "obj_frames_cm_lh"
    output_root.mkdir(parents=True, exist_ok=True)
    cache = read_json(cache_manifest_path)
    frames = cache.get("frames") if isinstance(cache.get("frames"), list) else []
    timebase = cache.get("timebase") if isinstance(cache.get("timebase"), dict) else {}
    if cache.get("schema_version") != "harness_deformable_mesh_cache_v1" or not frames:
        raise DeformableSurfaceAdapterError("deformable cache manifest is missing frames")
    if not ue_asset_root.startswith("/Game/"):
        raise DeformableSurfaceAdapterError("UE asset root must start with /Game/")
    canonical_state = (source_root / str(cache.get("canonical_state") or "")).resolve()
    if not canonical_state.is_file() or not canonical_state.is_relative_to(source_root):
        raise DeformableSurfaceAdapterError("canonical deformable state is missing or outside the cache root")
    declared_state_hash = str(cache.get("canonical_state_sha256") or "")
    if not declared_state_hash or file_sha256(canonical_state) != declared_state_hash:
        raise DeformableSurfaceAdapterError("canonical state hash mismatch")

    replay_frames: list[dict[str, Any]] = []
    for expected_frame, frame in enumerate(frames):
        if int(frame.get("frame") or 0) != expected_frame:
            raise DeformableSurfaceAdapterError("deformable frames must be contiguous and start at zero")
        source = (source_root / str(frame.get("surface") or "")).resolve()
        if not source.is_file() or not source.is_relative_to(source_root):
            raise DeformableSurfaceAdapterError(f"deformable surface frame is missing: {source}")
        declared_surface_hash = str(frame.get("sha256") or "")
        if not declared_surface_hash or file_sha256(source) != declared_surface_hash:
            raise DeformableSurfaceAdapterError(f"deformable surface hash mismatch at frame {expected_frame}")
        output = output_root / f"frame_{expected_frame:04d}.obj"
        vertex_count, triangle_count = convert_obj_m_rh_to_cm_lh(source, output)
        if vertex_count != int(frame.get("vertex_count") or 0) or triangle_count != int(frame.get("triangle_count") or 0):
            raise DeformableSurfaceAdapterError(f"deformable OBJ count mismatch at frame {expected_frame}")
        replay_frames.append({
            "frame": expected_frame,
            "time_s": round(float(frame.get("time_s") or 0.0), 8),
            "source_surface": source.relative_to(source_root).as_posix(),
            "ue_obj": output.relative_to(destination).as_posix(),
            "ue_asset_path": f"{ue_asset_root.rstrip('/')}/SM_Cloth_{expected_frame:04d}",
            "vertex_count": vertex_count,
            "triangle_count": triangle_count,
            "sha256": file_sha256(output),
        })

    replay = {
        "schema_version": "harness_deformable_surface_replay_v1",
        "adapter": "fixed_topology_obj_sequence_to_ue_static_mesh_swap_v1",
        "state_truth": str(canonical_state),
        "state_truth_sha256": declared_state_hash,
        "surface_truth_role": "derived_render_representation",
        "coordinate_transform": {
            "source": "right-handed z-up metres",
            "target": "Unreal left-handed z-up centimetres",
            "position": "(x, y, z) m -> (100*x, -100*y, 100*z) cm",
            "triangle_winding": "reversed",
        },
        "timebase": {
            "fps": int(timebase.get("fps") or 0),
            "frame_count": len(replay_frames),
            "frame_selection": "surface frame index equals render frame index",
        },
        "ue": {
            "asset_root": ue_asset_root.rstrip("/"),
            "actor_id": "cloth_surface",
            "collision_enabled": False,
            "segmentation_id_policy": "one stable instance id for every mesh frame",
            "material_role": "fabric_opaque_two_sided",
            "replay_method": "swap preimported static mesh at the render-frame seam",
        },
        "frames": replay_frames,
    }
    write_json(destination / "deformable_surface_replay.json", replay)
    return replay
