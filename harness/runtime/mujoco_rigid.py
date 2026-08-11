from __future__ import annotations

import math
from typing import Any
from xml.sax.saxutils import quoteattr


def simulate_rigid_case(
    case_spec: dict[str, Any],
    actor_placement: dict[str, Any],
    *,
    fps: int,
    duration_s: float,
) -> list[dict[str, Any]]:
    """Solve one declarative rigid-body object graph with MuJoCo."""
    try:
        import mujoco
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("MuJoCo rigid simulation requires `python -m pip install mujoco==3.10.0`.") from exc

    bindings = [item for item in actor_placement.get("actor_bindings") or [] if isinstance(item, dict)]
    if not bindings:
        raise RuntimeError("MuJoCo rigid simulation requires runtime actor placement bindings.")
    objects = {str(item.get("id")): item for item in case_spec.get("objects") or [] if isinstance(item, dict)}
    dynamic = [item for item in bindings if bool((item.get("physics") or {}).get("simulate_physics"))]
    model = mujoco.MjModel.from_xml_string(_mjcf(bindings, _gravity(case_spec)))
    data = mujoco.MjData(model)
    for binding in dynamic:
        object_id = str(binding["object_id"])
        object_spec = objects.get(object_id) or {}
        joint_id = int(model.body(object_id).jntadr[0])
        qvel_adr = int(model.jnt_dofadr[joint_id])
        data.qvel[qvel_adr : qvel_adr + 3] = _vector(object_spec.get("initial_velocity_m_s"), [0.0, 0.0, 0.0])
        data.qvel[qvel_adr + 3 : qvel_adr + 6] = _vector(object_spec.get("initial_angular_velocity_rad_s"), [0.0, 0.0, 0.0])
        model.dof_damping[qvel_adr : qvel_adr + 3] = max(0.0, float(object_spec.get("linear_damping") or 0.0))
        model.dof_damping[qvel_adr + 3 : qvel_adr + 6] = max(0.0, float(object_spec.get("angular_damping") or 0.0))

    solver_state = _solver_state(mujoco, model, bindings)
    mujoco.mj_forward(model, data)
    frame_count = max(1, int(round(duration_s * fps)))
    steps_per_frame = max(1, int(round((1.0 / fps) / model.opt.timestep)))
    frames = [_frame(model, data, dynamic, 0, fps, solver_state)]
    for frame_index in range(1, frame_count + 1):
        contacts_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
        for _ in range(steps_per_frame):
            mujoco.mj_step(model, data)
            for contact in _contacts(model, data, frame_index, fps):
                contacts_by_pair[tuple(contact["objects"])] = contact
        frames.append(_frame(model, data, dynamic, frame_index, fps, solver_state, list(contacts_by_pair.values())))
    return frames


def _mjcf(bindings: list[dict[str, Any]], gravity: list[float]) -> str:
    geoms = []
    for binding in bindings:
        object_id = str(binding["object_id"])
        physics = binding.get("physics") or {}
        bounds = binding.get("bounds") or {}
        transform = binding.get("transform") or {}
        extents = _vector(bounds.get("extents_m"), [0.25, 0.25, 0.25])
        position = _vector(transform.get("position_m"), [0.0, 0.0, 0.0])
        if bounds.get("bottom_z") is not None and bounds.get("top_z") is not None:
            position[2] = (float(bounds["bottom_z"]) + float(bounds["top_z"])) / 2.0
        material = physics.get("material") or {}
        friction = max(0.001, float(material.get("dynamic_friction") or 0.001))
        restitution = max(0.0, min(1.0, float(material.get("restitution") or 0.0)))
        geom = (
            f'<geom name={quoteattr(object_id)} {_shape(str(physics.get("collider") or "box"), extents)} '
            f'friction="{friction} 0.001 0.0001" solref="0.02 {1.0 - 0.5 * restitution}" condim="3"'
        )
        if physics.get("simulate_physics"):
            mass = max(0.001, float(physics.get("mass_kg") or 1.0))
            geoms.append(f'<body name={quoteattr(object_id)} pos="{_vec(position)}"><joint type="free"/>{geom} mass="{mass}"/></body>')
        else:
            geoms.append(f'{geom} pos="{_vec(position)}"/>')
    return f'<mujoco><option timestep="0.0041666667" gravity="{_vec(gravity)}"/><worldbody>{"".join(geoms)}</worldbody></mujoco>'


def _shape(collider: str, extents: list[float]) -> str:
    if "sphere" in collider.casefold():
        return f'type="sphere" size="{max(extents)}"'
    if "cylinder" in collider.casefold() or "capsule" in collider.casefold():
        return f'type="cylinder" size="{max(extents[0], extents[1])} {extents[2]}"'
    return f'type="box" size="{_vec(extents)}"'


def _frame(model: Any, data: Any, dynamic: list[dict[str, Any]], frame_index: int, fps: int, solver_state: dict[str, Any], substep_contacts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    objects = {}
    for binding in dynamic:
        object_id = str(binding["object_id"])
        body = data.body(object_id)
        joint_id = int(model.body(object_id).jntadr[0])
        qvel_adr = int(model.jnt_dofadr[joint_id])
        objects[object_id] = {
            "position": [round(float(value), 6) for value in body.xpos],
            "rotation_degrees": _quat_to_degrees(body.xquat),
            "velocity_m_s": [round(float(value), 6) for value in data.qvel[qvel_adr : qvel_adr + 3]],
            "angular_velocity_rad_s": [round(float(value), 6) for value in data.qvel[qvel_adr + 3 : qvel_adr + 6]],
            "source": "mujoco_rigid",
        }
    contacts = {tuple(item["objects"]): item for item in (substep_contacts or [])}
    for item in _contacts(model, data, frame_index, fps):
        contacts[tuple(item["objects"])] = item
    return {"frame": frame_index, "time": round(frame_index / fps, 6), "source": "mujoco_rigid", "objects": objects, "contacts": list(contacts.values()), "solver_state": solver_state}


def _contacts(model: Any, data: Any, frame_index: int, fps: int) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for contact in data.contact[: data.ncon]:
        pair = tuple(sorted((model.geom(int(contact.geom1)).name, model.geom(int(contact.geom2)).name)))
        if pair in seen:
            continue
        seen.add(pair)
        result.append({"frame": frame_index, "time": round(frame_index / fps, 6), "objects": list(pair), "method": "mujoco_contact", "distance_m": round(float(contact.dist), 8)})
    return result


def _solver_state(mujoco: Any, model: Any, bindings: list[dict[str, Any]]) -> dict[str, Any]:
    objects = {}
    for binding in bindings:
        object_id = str(binding["object_id"])
        geom_id = int(model.geom(object_id).id)
        physics = binding.get("physics") or {}
        material = physics.get("material") or {}
        body_id = int(model.body(object_id).id) if physics.get("simulate_physics") else 0
        dof_adr = int(model.jnt_dofadr[int(model.body(object_id).jntadr[0])]) if body_id else 0
        objects[object_id] = {
            "simulate_physics": bool(physics.get("simulate_physics")),
            "mass_kg": round(float(model.body_mass[body_id]), 8) if body_id else 0.0,
            "friction": [round(float(value), 8) for value in model.geom_friction[geom_id]],
            "requested_restitution": round(float(material.get("restitution") or 0.0), 8),
            "linear_damping": [round(float(value), 8) for value in model.dof_damping[dof_adr : dof_adr + 3]] if body_id else [],
            "angular_damping": [round(float(value), 8) for value in model.dof_damping[dof_adr + 3 : dof_adr + 6]] if body_id else [],
        }
    return {"backend": "mujoco_rigid", "version": str(getattr(mujoco, "__version__", "unknown")), "timestep_s": round(float(model.opt.timestep), 10), "gravity_m_s2": [round(float(value), 8) for value in model.opt.gravity], "objects": objects}


def _quat_to_degrees(quat: Any) -> list[float]:
    w, x, y, z = (float(value) for value in quat)
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return [round(math.degrees(pitch), 5), round(math.degrees(yaw), 5), round(math.degrees(roll), 5)]


def _gravity(case_spec: dict[str, Any]) -> list[float]:
    parameters = case_spec.get("physical_parameters") if isinstance(case_spec.get("physical_parameters"), dict) else {}
    return _vector(parameters.get("gravity_m_s2") or case_spec.get("gravity_m_s2"), [0.0, 0.0, -9.81])


def _vector(value: Any, default: list[float]) -> list[float]:
    values = list(value) if isinstance(value, (list, tuple)) else list(default)
    padded = [*values, *default]
    return [float(padded[0]), float(padded[1]), float(padded[2])]


def _vec(values: list[float]) -> str:
    return " ".join(str(float(value)) for value in values)
