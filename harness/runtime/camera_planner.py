from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable


DEFAULT_VIEWS = ["front_static", "side_static", "top_down", "tracking_subject", "event_closeup"]
MIN_EXTENT = 0.5
DYNAMIC_CAMERA_PROFILE = "damped_event_context_v1"
DYNAMIC_CAMERA_FOLLOW_GAINS = {
    "tracking_subject": (0.65, 0.65),
    "event_closeup": (0.20, 0.10),
}
DYNAMIC_CAMERA_FOV = {
    "tracking_subject": 56.0,
    "event_closeup": 46.0,
}


@dataclass(frozen=True)
class SceneBounds:
    center: tuple[float, float, float]
    extent: tuple[float, float, float]


@dataclass(frozen=True)
class CameraViewSpec:
    camera_id: str
    role: str
    location: tuple[float, float, float]
    rotation: tuple[float, float, float]
    fov: float
    target: tuple[float, float, float]
    near_clip: float | None = None
    far_clip: float | None = None
    dynamic_camera_profile: str | None = None
    subject_follow_location_gain: float | None = None
    subject_follow_target_gain: float | None = None
    camera_mode: str = "fixed"


@dataclass(frozen=True)
class CameraPlan:
    scene_bounds: SceneBounds
    views: list[CameraViewSpec]
    strategy: str
    warnings: list[str]


def plan_cameras_for_scene(
    scene_bounds: SceneBounds,
    requested_views: list[str] | None = None,
    min_distance_multiplier: float = 2.2,
    fov: float = 60.0,
) -> CameraPlan:
    roles = normalize_views(requested_views or DEFAULT_VIEWS)
    bounds, warnings = sanitize_bounds(scene_bounds)
    cx, cy, cz = bounds.center
    ex, ey, ez = bounds.extent
    # SceneBounds.extent is a full span (max - min), so framing uses its half-span.
    radius = max(ex, ey, ez, MIN_EXTENT) / 2.0
    distance = max(radius * min_distance_multiplier, 1.5)
    vertical = max(ez * 1.8, distance)
    far_clip = round(max(distance * 6.0, radius * 8.0, 10.0), 4)
    views: list[CameraViewSpec] = []
    for role in roles:
        if role in {"overview", "front_static"}:
            location = (cx + distance, cy - distance, cz + vertical)
            rotation = look_at_rotation(location, bounds.center)
        elif role == "front":
            location = (cx, cy - distance * 1.35, cz + ez * 0.55)
            rotation = look_at_rotation(location, bounds.center)
        elif role in {"side", "side_static"}:
            location = (cx + distance * 1.35, cy, cz + max(ez * 1.2, radius * 0.55, 0.8))
            rotation = look_at_rotation(location, bounds.center)
        elif role in {"top", "top_down"}:
            location = (cx, cy, cz + max(distance * 1.8, ez * 3.0, 2.0))
            rotation = (-90.0, 0.0, 0.0)
        elif role == "tracking_subject":
            location = (cx - distance * 1.15, cy - distance * 1.25, cz + max(ez * 0.8, distance * 0.72))
            rotation = look_at_rotation(location, bounds.center)
        elif role == "event_closeup":
            location = (cx - distance * 0.72, cy - distance * 0.82, cz + max(ez * 0.58, distance * 0.46))
            rotation = look_at_rotation(location, bounds.center)
        else:
            warnings.append(f"unknown view role dropped: {role}")
            continue
        dynamic_gains = DYNAMIC_CAMERA_FOLLOW_GAINS.get(role)
        views.append(
            CameraViewSpec(
                camera_id=role,
                role=role,
                location=round_vec(location),
                rotation=round_vec(rotation),
                fov=DYNAMIC_CAMERA_FOV.get(role, float(fov)),
                target=round_vec(bounds.center),
                near_clip=1.0,
                far_clip=far_clip,
                dynamic_camera_profile=DYNAMIC_CAMERA_PROFILE if dynamic_gains else None,
                subject_follow_location_gain=dynamic_gains[0] if dynamic_gains else None,
                subject_follow_target_gain=dynamic_gains[1] if dynamic_gains else None,
                camera_mode=(
                    "object_bound"
                    if role == "tracking_subject"
                    else "trajectory" if role == "event_closeup" else "fixed"
                ),
            )
        )
    return CameraPlan(scene_bounds=bounds, views=views, strategy="bounds_auto_v1", warnings=warnings)


def camera_plan_from_case_spec(case_spec: dict[str, Any], requested_views: list[str] | None = None, camera_strategy: str = "bounds_auto_v1") -> CameraPlan:
    bounds, warnings = bounds_from_case_spec(case_spec)
    tabletop = str(case_spec.get("task_type") or "").casefold() in {"billiards_collision", "pool_collision"}
    plan = plan_cameras_for_scene(
        bounds,
        requested_views=requested_views,
        min_distance_multiplier=1.6 if tabletop else 2.2,
        fov=52.0 if tabletop else 60.0,
    )
    scene = case_spec.get("scene") if isinstance(case_spec.get("scene"), dict) else {}
    overrides = case_spec.get("camera_overrides") or scene.get("camera_overrides")
    views = apply_camera_overrides(plan.views, overrides, warnings)
    if camera_strategy != "bounds_auto_v1":
        warnings.append(f"unsupported camera strategy requested, using bounds_auto_v1: {camera_strategy}")
    return CameraPlan(scene_bounds=plan.scene_bounds, views=views, strategy=plan.strategy, warnings=[*warnings, *plan.warnings])


def apply_camera_overrides(
    views: list[CameraViewSpec],
    raw_overrides: Any,
    warnings: list[str],
) -> list[CameraViewSpec]:
    if not isinstance(raw_overrides, dict):
        return views
    result = []
    for view in views:
        override = raw_overrides.get(view.camera_id)
        if not isinstance(override, dict):
            result.append(view)
            continue
        location = vec3(override.get("location")) or list(view.location)
        target = vec3(override.get("target")) or list(view.target)
        try:
            fov = max(10.0, min(120.0, float(override.get("fov", view.fov))))
        except (TypeError, ValueError):
            fov = view.fov
            warnings.append(f"invalid camera override fov ignored: {view.camera_id}")
        result.append(
            replace(
                view,
                role=str(override.get("role") or view.role),
                location=round_vec(location),
                target=round_vec(target),
                rotation=round_vec(look_at_rotation(tuple(location), tuple(target))),
                fov=fov,
            )
        )
    return result


def bounds_from_case_spec(case_spec: dict[str, Any]) -> tuple[SceneBounds, list[str]]:
    warnings: list[str] = []
    explicit = case_spec.get("scene_bounds") or (case_spec.get("scene") or {}).get("scene_bounds")
    if isinstance(explicit, dict):
        center = vec3(explicit.get("center"))
        extent = vec3(explicit.get("extent"))
        if center and extent:
            return SceneBounds(tuple(center), tuple(extent)), warnings
    points: list[tuple[float, float, float]] = []
    collect_points(case_spec.get("objects"), points)
    collect_points(case_spec.get("actors"), points)
    collect_points((case_spec.get("scene") or {}).get("objects"), points)
    initial_state = case_spec.get("initial_state")
    if isinstance(initial_state, dict):
        collect_points(initial_state.values(), points)
    if not points:
        warnings.append("scene bounds missing; using default bounds")
        return SceneBounds((0.0, 0.0, 0.5), (2.0, 2.0, 1.0)), warnings
    xs, ys, zs = zip(*points)
    center = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0, (min(zs) + max(zs)) / 2.0)
    extent = (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    return SceneBounds(round_vec(center), round_vec(extent)), warnings


def collect_points(items: Any, points: list[tuple[float, float, float]]) -> None:
    if not isinstance(items, Iterable) or isinstance(items, (str, bytes, dict)):
        if isinstance(items, dict):
            point = point_from_dict(items)
            if point:
                points.append(point)
        return
    for item in items:
        if isinstance(item, dict):
            point = point_from_dict(item)
            if point:
                points.append(point)


def point_from_dict(data: dict[str, Any]) -> tuple[float, float, float] | None:
    for key in ("initial_position_m", "position_m", "position", "location", "initial_location", "center"):
        value = data.get(key)
        point = vec3(value)
        if point:
            return tuple(point)
    transform = data.get("transform")
    if isinstance(transform, dict):
        return point_from_dict(transform)
    return None


def sanitize_bounds(bounds: SceneBounds) -> tuple[SceneBounds, list[str]]:
    warnings: list[str] = []
    extent = tuple(max(abs(float(value)), MIN_EXTENT) for value in bounds.extent)
    if extent != bounds.extent:
        warnings.append("scene extent was tiny or zero; clamped to minimum extent")
    center = tuple(float(value) for value in bounds.center)
    return SceneBounds(round_vec(center), round_vec(extent)), warnings


def normalize_views(views: list[str]) -> list[str]:
    result: list[str] = []
    for view in views:
        key = str(view).strip().lower()
        if key and key not in result:
            result.append(key)
    return result or ["overview"]


def look_at_rotation(location: tuple[float, float, float], target: tuple[float, float, float]) -> tuple[float, float, float]:
    dx = target[0] - location[0]
    dy = target[1] - location[1]
    dz = target[2] - location[2]
    horizontal = math.sqrt(dx * dx + dy * dy) or 1e-6
    pitch = math.degrees(math.atan2(dz, horizontal))
    yaw = math.degrees(math.atan2(dy, dx))
    return (pitch, 0.0, yaw)


def vec3(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)):
        return None
    padded = [*value, 0.0, 0.0, 0.0]
    try:
        return [float(padded[0]), float(padded[1]), float(padded[2])]
    except (TypeError, ValueError):
        return None


def round_vec(value: tuple[float, float, float] | list[float]) -> tuple[float, float, float]:
    return (round(float(value[0]), 4), round(float(value[1]), 4), round(float(value[2]), 4))


def camera_plan_to_dict(plan: CameraPlan) -> dict[str, Any]:
    return {
        "strategy": plan.strategy,
        "scene_bounds": asdict(plan.scene_bounds),
        "views": [asdict(view) for view in plan.views],
        "warnings": list(plan.warnings),
    }


def camera_plan_from_dict(data: dict[str, Any]) -> CameraPlan:
    bounds = data.get("scene_bounds") if isinstance(data.get("scene_bounds"), dict) else {}
    center = vec3(bounds.get("center")) or [0.0, 0.0, 0.0]
    extent = vec3(bounds.get("extent")) or [1.0, 1.0, 1.0]
    views: list[CameraViewSpec] = []
    for raw in data.get("views") or []:
        if not isinstance(raw, dict):
            continue
        location = vec3(raw.get("location")) or [0.0, -2.0, 1.0]
        rotation = vec3(raw.get("rotation")) or [0.0, 0.0, 0.0]
        target = vec3(raw.get("target")) or center
        views.append(
            CameraViewSpec(
                camera_id=str(raw.get("camera_id") or raw.get("role") or f"camera_{len(views):02d}"),
                role=str(raw.get("role") or raw.get("camera_id") or "planned_view"),
                location=tuple(location),
                rotation=tuple(rotation),
                fov=float(raw.get("fov") or 60.0),
                target=tuple(target),
                near_clip=float(raw["near_clip"]) if raw.get("near_clip") is not None else None,
                far_clip=float(raw["far_clip"]) if raw.get("far_clip") is not None else None,
                dynamic_camera_profile=str(raw["dynamic_camera_profile"]) if raw.get("dynamic_camera_profile") else None,
                subject_follow_location_gain=float(raw["subject_follow_location_gain"]) if raw.get("subject_follow_location_gain") is not None else None,
                subject_follow_target_gain=float(raw["subject_follow_target_gain"]) if raw.get("subject_follow_target_gain") is not None else None,
                camera_mode=str(raw.get("camera_mode") or "fixed"),
            )
        )
    return CameraPlan(
        scene_bounds=SceneBounds(center=tuple(center), extent=tuple(extent)),
        views=views,
        strategy=str(data.get("strategy") or "bounds_auto_v1"),
        warnings=[str(value) for value in data.get("warnings") or []],
    )
