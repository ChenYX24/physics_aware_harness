from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from harness.core.artifact_schema import read_json, write_json
from harness.core.harness_config import EffectiveHarnessConfig
from scripts.harness_local_ue_runner import run_precompiled_ue_scene


REPLAY_PREVIEW_SCHEMA_VERSION = "harness_replay_preview_v7"


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def replay_preview_request_digest(
    *, source_run: str | Path, time_s: float, offsets_m: Mapping[str, Sequence[float]], rotations_deg: Mapping[str, Sequence[float]]
) -> str:
    source = Path(source_run).resolve()
    runtime_path = cached_runtime_scene_path(source)
    return _digest(
        {
            "schema_version": REPLAY_PREVIEW_SCHEMA_VERSION,
            "source_runtime_sha256": hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
            "time_s": float(time_s),
            "offsets_m": {key: list(value) for key, value in sorted(offsets_m.items())},
            "rotations_deg": {key: list(value) for key, value in sorted(rotations_deg.items())},
            "width": 640,
            "height": 360,
        }
    )


def cached_runtime_scene_path(source_run: str | Path) -> Path:
    root = Path(source_run).resolve()
    for relative in (
        "logs/studio_runtime_scene_combined.json",
        "logs/studio_runtime_scene_rgb.json",
        "studio_runtime_scene.json",
    ):
        candidate = root / relative
        if candidate.is_file():
            return candidate
    raise ValueError("replay preview requires a completed cached runtime scene")


def _vec3(value: Sequence[float], *, label: str) -> list[float]:
    if len(value) != 3:
        raise ValueError(f"{label} must contain exactly three numbers")
    return [float(component) for component in value]


def apply_runtime_transform_overrides(
    runtime_scene: Mapping[str, Any],
    *,
    offsets_m: Mapping[str, Sequence[float]] | None = None,
    rotations_deg: Mapping[str, Sequence[float]] | None = None,
) -> dict[str, Any]:
    """Return a diagnostic-only cached scene with generic object transform deltas."""
    scene = json.loads(json.dumps(runtime_scene))
    offsets = {key: _vec3(value, label=f"offset for {key}") for key, value in (offsets_m or {}).items()}
    rotations = {key: _vec3(value, label=f"rotation for {key}") for key, value in (rotations_deg or {}).items()}
    declared = {
        str(row.get("id"))
        for group in ("dynamic_objects", "static_objects")
        for row in scene.get(group) or []
        if isinstance(row, dict) and row.get("id")
    }
    unknown = sorted((set(offsets) | set(rotations)) - declared)
    if unknown:
        raise ValueError("replay preview transform references unknown object ids: " + ", ".join(unknown))

    for group in ("dynamic_objects", "static_objects"):
        for row in scene.get(group) or []:
            object_id = str(row.get("id") or "")
            params = row.setdefault("params", {})
            if isinstance(params.get("surface_mesh_sequence"), list):
                params.update(
                    {
                        "generate_translucent_fluid_material": False,
                        "generate_solid_material": True,
                        "preserve_material": False,
                        "fixed_material_color": True,
                        "generated_material_name": "M_Harness_ReplayPreviewSurface",
                        "color_rgb": [0.0, 0.45, 1.0],
                        "roughness": 0.25,
                        "metallic": 0.0,
                    }
                )
            elif object_id in offsets or object_id in rotations:
                params.update(
                    {
                        "generate_translucent_fluid_material": True,
                        "generate_solid_material": False,
                        "preserve_material": False,
                        "color_rgb": [1.0, 0.16, 0.02],
                        "roughness": 0.38,
                        "opacity": 0.32,
                        "refraction": 1.0,
                    }
                )
            if object_id in offsets:
                base = _vec3(row.get("initial_position_m") or [0.0, 0.0, 0.0], label=f"position for {object_id}")
                row["initial_position_m"] = [base[index] + offsets[object_id][index] for index in range(3)]
            if object_id in rotations:
                base = _vec3(params.get("base_rotation_degrees") or [0.0, 0.0, 0.0], label=f"rotation for {object_id}")
                params["base_rotation_degrees"] = [base[index] + rotations[object_id][index] for index in range(3)]

    for frame in scene.get("precomputed_trajectory") or []:
        objects = frame.get("objects") if isinstance(frame, dict) else None
        if not isinstance(objects, dict):
            continue
        for object_id, offset in offsets.items():
            state = objects.get(object_id)
            if not isinstance(state, dict):
                continue
            key = "position" if isinstance(state.get("position"), list) else "position_m"
            if isinstance(state.get(key), list):
                base = _vec3(state[key], label=f"trajectory position for {object_id}")
                state[key] = [base[index] + offset[index] for index in range(3)]
        for object_id, rotation in rotations.items():
            state = objects.get(object_id)
            if not isinstance(state, dict):
                continue
            key = "rotation_degrees" if isinstance(state.get("rotation_degrees"), list) else "rotation_deg"
            if isinstance(state.get(key), list):
                base = _vec3(state[key], label=f"trajectory rotation for {object_id}")
                state[key] = [base[index] + rotation[index] for index in range(3)]
    return scene


def execute_replay_preview(
    *,
    source_run: str | Path,
    output_dir: str | Path,
    config: EffectiveHarnessConfig,
    time_s: float,
    offsets_m: Mapping[str, Sequence[float]] | None = None,
    rotations_deg: Mapping[str, Sequence[float]] | None = None,
) -> dict[str, Any]:
    source = Path(source_run).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    runtime_path = cached_runtime_scene_path(source)
    runtime_scene = apply_runtime_transform_overrides(
        read_json(runtime_path), offsets_m=offsets_m, rotations_deg=rotations_deg
    )
    trajectory = runtime_scene.get("precomputed_trajectory")
    if not isinstance(trajectory, list) or not trajectory:
        raise ValueError("replay preview requires an existing precomputed trajectory cache")
    simulation = runtime_scene.get("simulation") or {}
    duration_s = float(simulation.get("duration_s") or 0.0)
    if time_s < 0.0 or time_s > duration_s:
        raise ValueError(f"replay preview time must be between 0 and {duration_s:g} seconds")

    studio_source = source / "studio_scene_spec.json"
    studio_scene = read_json(studio_source) if studio_source.is_file() else {"schema_version": "harness_replay_preview_scene_v1"}
    runtime_output = destination / "studio_runtime_scene.json"
    studio_output = destination / "studio_scene_spec.json"
    write_json(runtime_output, runtime_scene)
    write_json(studio_output, studio_scene)
    native_output = destination / "logs" / "native_replay_preview"
    background = runtime_scene.get("background_map") or {}
    map_path = str(background.get("ue5_path") or "").split(".", 1)[0]
    fps = int(simulation.get("render_fps") or simulation.get("fps") or 24)
    command = [
        str(config.ue_executable),
        f"-project={config.ue_project}",
        "-RenderOffScreen",
        "-unattended",
        "-nosplash",
        "-NoScreenMessages",
        "-stdout",
        "-FullStdOutLogOutput",
        f"-ExecutePythonScript={Path(__file__).resolve().parents[2] / 'scripts' / 'native_ue_scene.py'}",
    ]
    result = run_precompiled_ue_scene(
        command,
        studio_scene_path=studio_output,
        runtime_scene_path=runtime_output,
        native_output=native_output,
        environment={
            "SCENE_MAP": map_path,
            "WIDTH": "640",
            "HEIGHT": "360",
            "FPS": str(fps),
            "DURATION": str(duration_s),
            "MULTI_VIEW": "1",
            "CANONICAL_MULTI_VIEW": "1",
            "RENDER_DATA_PASSES": "0",
            "CHAOS_SIMULATION_ENABLED": "0",
            "SIM_STUDIO_UE_CAPTURE_BACKEND": "scene_capture",
            "WORLD_MODEL_RENDER_PASS_MODE": "rgb",
            "KEEP_RENDER_FRAMES": "0",
            "LAYOUT_PREVIEW_TIME_S": str(float(time_s)),
        },
        timeout=900,
    )
    manifest_path = native_output / "layout_preview_manifest.json"
    manifest = read_json(manifest_path) if manifest_path.is_file() else {}
    if result.get("status") != "completed" or manifest.get("status") != "pass":
        raise RuntimeError("cached replay preview did not produce a passing image manifest")
    images = []
    for row in manifest.get("images") or []:
        path = Path(str(row.get("path") or ""))
        if path.is_file():
            images.append({**row, "path": str(path)})
    return {
        "schema_version": REPLAY_PREVIEW_SCHEMA_VERSION,
        "status": "pass" if images else "fail",
        "source_run": str(source),
        "time_s": float(time_s),
        "physics_qualification": False,
        "solver_invoked": False,
        "diagnostic_surface_material": "opaque_high_contrast",
        "surface_mesh_sequence_replay": manifest.get("surface_mesh_sequence_replay"),
        "offsets_m": {key: list(value) for key, value in (offsets_m or {}).items()},
        "rotations_deg": {key: list(value) for key, value in (rotations_deg or {}).items()},
        "images": images,
    }
