from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.core.case_spec import CaseSpec
from harness.planning.runtime_compiler import RuntimeCompilation, compile_runtime_case
from harness.runtime.artifact_collector import write_runtime_artifacts
from harness.runtime.observation_planner import camera_ids_from_observation_plan, render_passes_from_observation_plan


class FallbackBackend:
    name = "fallback"

    def run_case(
        self,
        case: CaseSpec,
        output_root: str | Path,
        *,
        requested_views: list[str] | None = None,
        render_passes: list[str] | None = None,
        camera_strategy: str = "bounds_auto_v1",
        compilation: RuntimeCompilation | None = None,
    ) -> Path:
        run_dir = Path(output_root) / f"{case.case_id}_fallback"
        compilation = compilation or compile_runtime_case(
            case,
            requested_backend="fallback",
            requested_views=requested_views,
            render_passes=render_passes,
            camera_strategy=camera_strategy,
        )
        compilation.write(run_dir)
        observation_plan = compilation.artifacts["observation_plan"]
        return write_runtime_artifacts(
            run_dir,
            case_spec=case.data,
            trajectory=trajectory_for_case(case.data),
            backend=self.name,
            requested_views=camera_ids_from_observation_plan(observation_plan),
            render_passes=render_passes_from_observation_plan(observation_plan),
            camera_strategy=camera_strategy,
            camera_plan=compilation.artifacts["camera_plan"],
        )


def trajectory_for_case(case_spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a non-reference kinematic preview of the declared initial state.

    No collision, constraint, fracture, or other physical event is invented.
    Named process labels and case IDs are deliberately ignored.
    """
    objects = [item for item in case_spec.get("objects") or [] if isinstance(item, dict)]
    physical = case_spec.get("physical_parameters") if isinstance(case_spec.get("physical_parameters"), dict) else {}
    gravity_raw = physical.get("gravity_m_s2", [0.0, 0.0, -9.81])
    gravity = [0.0, 0.0, -abs(float(gravity_raw))] if isinstance(gravity_raw, (int, float)) else vec3(gravity_raw)
    scene = case_spec.get("scene") if isinstance(case_spec.get("scene"), dict) else {}
    duration = max(0.1, float(scene.get("duration_s") or 1.0))
    frames = []
    for frame_id, time_s in enumerate((0.0, duration / 2.0, duration)):
        states = {}
        for index, obj in enumerate(objects):
            object_id = str(obj.get("id") or obj.get("object_id") or f"object_{index}")
            position = vec3(obj.get("initial_position_m") or obj.get("position_m") or [0.0, 0.0, 0.0])
            velocity = vec3(obj.get("initial_velocity_m_s") or [0.0, 0.0, 0.0])
            body_type = str(obj.get("body_type") or "").casefold()
            role = str(obj.get("role") or "").casefold()
            fixed = (
                obj.get("dynamic") is False
                or obj.get("kinematic") is True
                or body_type in {"static", "kinematic"}
                or role in {"support", "floor", "ground"}
            )
            acceleration = [0.0, 0.0, 0.0] if fixed or obj.get("enable_gravity") is False else gravity
            states[object_id] = state(
                [position[axis] + velocity[axis] * time_s + 0.5 * acceleration[axis] * time_s * time_s for axis in range(3)],
                [velocity[axis] + acceleration[axis] * time_s for axis in range(3)],
                vec3(obj.get("initial_rotation_deg") or [0.0, 0.0, 0.0]),
            )
        frames.append({"frame": frame_id, "time_s": round(time_s, 8), "objects": states, "contacts": []})
    return frames


def state(position: list[float], velocity: list[float], rotation: list[float]) -> dict[str, Any]:
    return {
        "position_m": [round(value, 6) for value in position],
        "velocity_m_s": [round(value, 6) for value in velocity],
        "rotation_deg": rotation,
    }


def vec3(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return [0.0, 0.0, 0.0]
    padded = [*value, 0.0, 0.0, 0.0]
    return [float(padded[0]), float(padded[1]), float(padded[2])]
