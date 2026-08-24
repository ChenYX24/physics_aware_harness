from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable, Mapping

from harness.core.scene_layout import rotate_local_vector_ue


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
    target_object_ids: tuple[str, ...] = ()


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
        if role == "overview":
            location = (cx + distance, cy - distance, cz + vertical)
            rotation = look_at_rotation(location, bounds.center)
        elif role in {"front", "front_static"}:
            location = (cx, cy - framing_distance(ex, ez, fov), cz)
            rotation = look_at_rotation(location, bounds.center)
        elif role in {"side", "side_static"}:
            # Observe the dominant horizontal scene axis broadside so motion
            # along a long ramp or track remains visible instead of end-on.
            location = (
                cx if ex >= ey else cx + distance * 1.35,
                cy - distance * 1.35 if ex >= ey else cy,
                cz + max(ez * 1.2, radius * 0.55, 0.8),
            )
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


def camera_plan_from_case_spec(
    case_spec: dict[str, Any],
    requested_views: list[str] | None = None,
    camera_strategy: str = "bounds_auto_v1",
    camera_intents: list[dict[str, Any]] | None = None,
    subject_frames: Mapping[str, Mapping[str, Any]] | None = None,
) -> CameraPlan:
    bounds, warnings = bounds_from_case_spec(case_spec)
    plan = plan_cameras_for_scene(
        bounds,
        requested_views=requested_views,
        min_distance_multiplier=2.2,
        fov=60.0,
    )
    scene = case_spec.get("scene") if isinstance(case_spec.get("scene"), dict) else {}
    overrides = case_spec.get("camera_overrides") or scene.get("camera_overrides")
    views = apply_subject_framing(
        plan.views,
        case_spec,
        camera_intents,
        warnings,
        subject_frames=subject_frames,
    )
    views = apply_camera_overrides(views, overrides, warnings)
    views = apply_explicit_camera_poses(views, camera_intents)
    views = apply_camera_targets(views, camera_intents)
    if camera_strategy != "bounds_auto_v1":
        warnings.append(f"unsupported camera strategy requested, using bounds_auto_v1: {camera_strategy}")
    return CameraPlan(scene_bounds=plan.scene_bounds, views=views, strategy=plan.strategy, warnings=[*warnings, *plan.warnings])


def apply_camera_targets(
    views: list[CameraViewSpec],
    camera_intents: list[dict[str, Any]] | None,
) -> list[CameraViewSpec]:
    intents = {
        str(intent.get("role")): intent
        for intent in camera_intents or []
        if isinstance(intent, dict) and intent.get("role")
    }
    return [
        replace(
            view,
            target_object_ids=tuple(
                str(object_id)
                for object_id in (intents.get(view.role) or {}).get("target_objects") or []
                if object_id
            ),
        )
        for view in views
    ]


def apply_subject_framing(
    views: list[CameraViewSpec],
    case_spec: dict[str, Any],
    camera_intents: list[dict[str, Any]] | None,
    warnings: list[str],
    *,
    subject_frames: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[CameraViewSpec]:
    intents = {
        str(intent.get("role")): intent
        for intent in camera_intents or []
        if isinstance(intent, dict) and intent.get("role")
    }
    objects = {
        str(item.get("id")): item
        for item in case_spec.get("objects") or []
        if isinstance(item, dict) and item.get("id")
    }
    result: list[CameraViewSpec] = []
    for view in views:
        intent = intents.get(view.role)
        if not isinstance(intent, dict) or intent.get("framing") != "full_subject":
            result.append(view)
            continue
        subject_id = str(intent.get("subject") or "")
        frame = subject_frame(objects.get(subject_id), (subject_frames or {}).get(subject_id))
        if frame is None:
            raise ValueError(f"full_subject framing requires registered bounds for subject: {subject_id}")
        center, size, yaw_deg = frame
        yaw = math.radians(yaw_deg)
        forward = (math.cos(yaw), math.sin(yaw))
        if view.role in {"side", "side_static"}:
            direction = (-forward[1], forward[0])
            horizontal_size = size[0]
        else:
            direction = forward
            horizontal_size = size[1]
        distance = framing_distance(horizontal_size, size[2], view.fov)
        location = (
            center[0] + direction[0] * distance,
            center[1] + direction[1] * distance,
            center[2],
        )
        result.append(
            replace(
                view,
                location=round_vec(location),
                target=round_vec(center),
                rotation=round_vec(look_at_rotation(location, center)),
                far_clip=round(max(distance * 6.0, 10.0), 4),
            )
        )
    return result


def subject_frame(
    subject: dict[str, Any] | None,
    registered_frame: Mapping[str, Any] | None = None,
) -> tuple[tuple[float, float, float], tuple[float, float, float], float] | None:
    if isinstance(registered_frame, Mapping):
        center = vec3(registered_frame.get("center_m"))
        size = vec3(registered_frame.get("size_m"))
        yaw = registered_frame.get("yaw_deg")
        if center is not None and size is not None and all(component > 0.0 for component in size):
            return tuple(center), tuple(size), float(yaw or 0.0)
    if not isinstance(subject, dict):
        return None
    position = point_from_dict(subject)
    solver = subject.get("solver") if isinstance(subject.get("solver"), dict) else {}
    collision_geometry = (
        subject.get("collision_geometry")
        if isinstance(subject.get("collision_geometry"), dict)
        else {}
    )
    size = (
        vec3(solver.get("authored_size_m"))
        or vec3(subject.get("size_m"))
        or vec3(collision_geometry.get("size_m"))
    )
    rotation = vec3(subject.get("initial_rotation_deg")) or [0.0, 0.0, 0.0]
    if position is None or size is None or any(component <= 0.0 for component in size):
        return None
    center = position
    if solver.get("type") == "articulated_body":
        center = (position[0], position[1], position[2] + size[2] * 0.5)
    return tuple(center), tuple(size), float(rotation[1])


def apply_explicit_camera_poses(
    views: list[CameraViewSpec],
    camera_intents: list[dict[str, Any]] | None,
) -> list[CameraViewSpec]:
    intents = {
        str(intent.get("role")): intent
        for intent in camera_intents or []
        if isinstance(intent, dict) and intent.get("role")
    }
    result: list[CameraViewSpec] = []
    for view in views:
        intent = intents.get(view.role)
        if not isinstance(intent, dict) or intent.get("position_m") is None:
            result.append(view)
            continue
        location = vec3(intent.get("position_m"))
        target = vec3(intent.get("look_at_m"))
        if location is None or target is None or intent.get("coordinate_frame") != "world":
            raise ValueError(f"invalid explicit world camera pose: {view.role}")
        fov = float(intent.get("fov_deg"))
        distance = math.dist(location, target)
        result.append(
            replace(
                view,
                location=round_vec(location),
                target=round_vec(target),
                rotation=round_vec(look_at_rotation(tuple(location), tuple(target))),
                fov=fov,
                far_clip=round(max(distance * 6.0, 10.0), 4),
                dynamic_camera_profile=None,
                subject_follow_location_gain=None,
                subject_follow_target_gain=None,
                camera_mode="fixed",
            )
        )
    return result


def subject_frames_from_scene_layout(scene_layout: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    frames: dict[str, dict[str, Any]] = {}
    for node in scene_layout.get("object_nodes") or []:
        if not isinstance(node, Mapping) or not node.get("object_id"):
            continue
        transform = node.get("transform") if isinstance(node.get("transform"), Mapping) else {}
        bounds = node.get("bounds") if isinstance(node.get("bounds"), Mapping) else {}
        position = vec3(transform.get("position_m"))
        rotation = vec3(transform.get("rotation_deg"))
        half_extent = vec3(bounds.get("extents_m"))
        local_center = vec3(bounds.get("local_center_offset_m"))
        if None in (position, rotation, half_extent, local_center):
            continue
        world_offset = rotate_local_vector_ue(local_center, rotation)
        frames[str(node["object_id"])] = {
            "center_m": [position[index] + world_offset[index] for index in range(3)],
            "size_m": [component * 2.0 for component in half_extent],
            "yaw_deg": rotation[1],
        }
    return frames


def framing_distance(width: float, height: float, horizontal_fov_deg: float) -> float:
    horizontal_fov = math.radians(horizontal_fov_deg)
    vertical_fov = 2.0 * math.atan(math.tan(horizontal_fov / 2.0) / (16.0 / 9.0))
    margin = 1.15
    return max(
        (height * 0.5 * margin) / math.tan(vertical_fov / 2.0),
        (width * 0.5 * margin) / math.tan(horizontal_fov / 2.0),
        1.5,
    )


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
    return (pitch, yaw, 0.0)


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
                target_object_ids=tuple(str(value) for value in raw.get("target_object_ids") or []),
            )
        )
    return CameraPlan(
        scene_bounds=SceneBounds(center=tuple(center), extent=tuple(extent)),
        views=views,
        strategy=str(data.get("strategy") or "bounds_auto_v1"),
        warnings=[str(value) for value in data.get("warnings") or []],
    )
