from __future__ import annotations

import copy
from typing import Any, Mapping

from harness.core.case_spec_v2 import CaseSpecV2
from harness.runtime.camera_planner import (
    camera_plan_from_case_spec,
    camera_plan_to_dict,
    subject_frames_from_scene_layout,
)


MODALITY_ALIASES = {
    "instance_segmentation": "segmentation",
    "instance_mask": "segmentation",
    "object_mask": "segmentation",
    "colour": "rgb",
}


def compile_observation_plan(
    runtime_case_spec: Mapping[str, Any],
    scene_layout: Mapping[str, Any],
    verification_plan: Mapping[str, Any],
    *,
    source_case_spec: CaseSpecV2,
    requested_views: list[str] | None = None,
    render_passes: list[str] | None = None,
    camera_strategy: str = "bounds_auto_v1",
) -> dict[str, Any]:
    requirements = (
        source_case_spec.data.get("observation_requirements")
        if isinstance(source_case_spec.data.get("observation_requirements"), dict)
        else {}
    )
    camera_intents = [
        copy.deepcopy(item)
        for item in requirements.get("cameras") or []
        if isinstance(item, dict)
    ]
    intent_roles = [str(item.get("role")) for item in camera_intents if item.get("role")]
    evidence = verification_plan.get("evidence_requirements") if isinstance(verification_plan.get("evidence_requirements"), dict) else {}
    verifier_roles = [str(value) for value in evidence.get("camera_roles") or []]
    if requested_views is not None:
        camera_roles = list(requested_views)
        precedence = "runtime_override"
    elif intent_roles:
        camera_roles = intent_roles
        precedence = "case_spec_observation_intent"
    else:
        camera_roles = []
        precedence = "camera_planner_default"
    camera_roles = list(dict.fromkeys([*camera_roles, *verifier_roles]))
    camera_plan = camera_plan_from_case_spec(
        dict(runtime_case_spec),
        requested_views=camera_roles or None,
        camera_strategy=camera_strategy,
        camera_intents=camera_intents,
        subject_frames=subject_frames_from_scene_layout(scene_layout),
    )
    camera_plan_data = camera_plan_to_dict(camera_plan)
    requested_modalities = render_passes if render_passes is not None else requirements.get("modalities") or ["rgb"]
    modalities = _normalize_modalities([*requested_modalities, *(evidence.get("modalities") or [])])
    case_signals = [str(value) for value in requirements.get("signals") or runtime_case_spec.get("required_signals") or []]
    signals = list(dict.fromkeys([*case_signals, *(str(value) for value in evidence.get("signals") or [])]))
    timebase = source_case_spec.data.get("timebase") or {}
    physics_hz = int(timebase.get("physics_hz") or 120)
    observation_fps = int(timebase.get("observation_fps") or 24)
    sampling = copy.deepcopy(requirements.get("sampling") or {})
    sampling.setdefault("physics_hz", physics_hz)
    sampling.setdefault("observation_fps", observation_fps)
    sampling.setdefault("physics_steps_per_observation", physics_hz // observation_fps)
    synchronization = copy.deepcopy(requirements.get("synchronization") or {})
    synchronization.setdefault("clock", "shared_sim_time")
    synchronization.setdefault("frame_alignment", "exact_sample_index")
    synchronization.setdefault("modalities_share_camera_state", True)
    return {
        "schema_version": "harness_observation_plan_v1",
        "case_id": runtime_case_spec.get("case_id"),
        "source_case_schema_version": source_case_spec.data.get("schema_version"),
        "camera_intents": camera_intents,
        "camera_precedence": precedence,
        "cameras": copy.deepcopy(camera_plan_data["views"]),
        "modalities": modalities,
        "signals": signals,
        "sampling": sampling,
        "synchronization": synchronization,
        "verifier_evidence_merged": {
            "camera_roles": verifier_roles,
            "modalities": list(evidence.get("modalities") or []),
            "signals": list(evidence.get("signals") or []),
        },
        "scene_layout": {
            "schema_version": scene_layout.get("schema_version"),
            "object_count": len(scene_layout.get("object_nodes") or []),
        },
        "camera_plan_compatibility_projection": camera_plan_data,
    }


def camera_plan_from_observation_plan(observation_plan: Mapping[str, Any]) -> dict[str, Any]:
    projection = observation_plan.get("camera_plan_compatibility_projection")
    if not isinstance(projection, Mapping):
        raise ValueError("observation plan is missing camera_plan_compatibility_projection")
    return copy.deepcopy(dict(projection))


def render_passes_from_observation_plan(observation_plan: Mapping[str, Any]) -> list[str]:
    return _normalize_modalities([str(value) for value in observation_plan.get("modalities") or []])


def render_mode_for_passes(render_passes: list[str]) -> str:
    has_rgb = "rgb" in render_passes
    has_data = bool({"depth", "segmentation"}.intersection(render_passes))
    return "both" if has_rgb and has_data else "data" if has_data else "rgb"


def camera_ids_from_observation_plan(observation_plan: Mapping[str, Any]) -> list[str]:
    return [
        str(camera.get("camera_id"))
        for camera in observation_plan.get("cameras") or []
        if isinstance(camera, Mapping) and camera.get("camera_id")
    ]


def _normalize_modalities(values: list[str]) -> list[str]:
    normalized = [MODALITY_ALIASES.get(str(value).casefold(), str(value).casefold()) for value in values if str(value).strip()]
    return list(dict.fromkeys(normalized))
