from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from harness.core.prompt_lineage import (
    append_prompt_stage,
    build_refiner_prompt,
    new_prompt_lineage,
    validate_prompt_lineage,
)
from harness.core.review_feedback import active_review_requirements
from harness.planning.capability_planner import CapabilityPlanner


def prompt_to_case(prompt: str, *, case_id: str = "generated_case") -> dict[str, Any]:
    """Compile a prompt into a valid, conservative CaseSpec draft."""
    normalized = " ".join(prompt.split())
    if not normalized:
        raise ValueError("prompt must not be empty")
    plan = CapabilityPlanner().plan(normalized)
    capability_id = str(plan["primary_capability_id"])
    break_case = capability_id == "rigid_body_contact_causality" and _is_billiards_break(normalized)
    template = _billiards_break_template() if break_case else case_template(capability_id)
    canonical_prompt = _canonical_generation_prompt(normalized, capability_id, break_case=break_case)
    appearance_requirements, preservation_requirements, review_quality_gates = _refinement_contract(
        capability_id,
        break_case=break_case,
    )
    expanded_prompt = (
        f"{canonical_prompt} Produce synchronized trajectory, contact/event evidence, static-camera RGB, "
        "OpenEXR depth, and instance segmentation; reject physically inconsistent output."
    )
    refiner_prompt = build_refiner_prompt(
        canonical_prompt,
        appearance_requirements=appearance_requirements,
        preservation_requirements=preservation_requirements,
    )
    lineage = new_prompt_lineage(case_id, prompt)
    append_prompt_stage(
        lineage,
        stage_id="canonical_generation_prompt",
        stage_kind="canonical_generation",
        content=canonical_prompt,
        producer="deterministic_prompt_compiler_v2",
        purpose="Shared verbatim input for UE scene construction and every prompt-only video-model baseline.",
        parent_stage_ids=("user_request",),
        artifact_path="prompt_lineage.json",
    )
    append_prompt_stage(
        lineage,
        stage_id="case_spec_expansion_prompt",
        stage_kind="case_spec_expansion",
        content=expanded_prompt,
        producer="deterministic_prompt_compiler_v2",
        purpose="Adds evidence and sensor requirements without changing the canonical scene intent.",
        parent_stage_ids=("canonical_generation_prompt",),
        artifact_path="prompt_lineage.json",
    )
    append_prompt_stage(
        lineage,
        stage_id="refiner_appearance_prompt",
        stage_kind="appearance_only_refinement",
        content=refiner_prompt,
        producer="deterministic_prompt_compiler_v2",
        purpose="Improves appearance while treating UE motion and identities as immutable.",
        parent_stage_ids=("canonical_generation_prompt",),
        artifact_path="prompt_lineage.json",
    )
    lineage["canonical_stage_id"] = "canonical_generation_prompt"
    lineage["refiner_stage_id"] = "refiner_appearance_prompt"
    validate_prompt_lineage(lineage)
    speed = first_number(normalized, r"(-?\d+(?:\.\d+)?)\s*(?:m/s|米每秒)")
    if speed is not None and template["active_objects"]:
        active_id = template["active_objects"][0]
        for obj in template["objects"]:
            if obj["id"] == active_id:
                obj["initial_velocity_m_s"] = [speed, 0.0, 0.0]
                break
        template["physical_parameters"]["requested_speed_m_s"] = speed
    return {
        "schema_version": "harness_case_spec_v1",
        "case_id": case_id,
        "capability_id": capability_id,
        "source_prompt": prompt,
        "prompt": canonical_prompt,
        "expanded_prompt": expanded_prompt,
        "refiner_prompt": refiner_prompt,
        "prompt_lineage": lineage,
        "appearance_requirements": appearance_requirements,
        "preservation_requirements": preservation_requirements,
        "review_quality_gates": review_quality_gates,
        "task_type": template["task_type"],
        "scene": template["scene"],
        "physical_parameters": template["physical_parameters"],
        "expected_physics": {
            **template["expected_physics"],
            "source": "deterministic_prompt_compiler_v1",
            "needs_agent_review": True,
        },
        "objects": template["objects"],
        "active_objects": template["active_objects"],
        "passive_objects": template["passive_objects"],
        "required_assets": template["required_assets"],
        "required_signals": ["trajectory", "contact_events", "camera_trajectory", "rgb", "depth", "segmentation"],
        "asset_requirements": {"acquisition_modes": ["preimported", "harness_generate", "harness_find_at_runtime"]},
        "allowed_proxy_policy": "analytic_proxy_for_local_draft_only",
        "verifier_expectation": {"status": "pass"},
        "should_pass": True,
        "notes": "Executable draft with conservative defaults; review dimensions, materials, and parameter ranges before reference publication.",
        "planning_trace": {
            **plan,
            "prompt_contract": {
                "compiler": "deterministic_prompt_compiler_v2",
                "canonical_stage_id": "canonical_generation_prompt",
                "refiner_stage_id": "refiner_appearance_prompt",
                "all_prompt_only_models_must_match_canonical_verbatim": True,
                "ue_scene_must_use_canonical_verbatim": True,
            },
            "template_source": "cases/billiards/sixteen_ball_reference_break.json" if break_case else "inline_conservative_template",
        },
    }


def _is_billiards_break(prompt: str) -> bool:
    lowered = prompt.casefold()
    return any(token in lowered for token in ("billiards break", "pool break", "break shot", "台球开球", "开球"))


def _canonical_generation_prompt(prompt: str, capability_id: str, *, break_case: bool) -> str:
    if break_case:
        return (
            f"{prompt} Show one white cue ball breaking a tightly racked set of exactly fifteen distinct "
            "numbered and colored object balls on a regulation six-pocket table with four corner pockets "
            "and two side pockets. Use real-scale cloth, rails, cushions, pocket jaws and liners. Preserve "
            "rigid-body contact causality, rolling and spin, frictional slowdown, no overlap or penetration, "
            "and natural settling in one continuous camera take."
        )
    suffixes = {
        "rigid_body_contact_causality": " Preserve object identity, contact order, momentum transfer, frictional slowdown, and natural settling in one continuous take.",
        "sequential_contact_propagation": " Prescribe only the initial trigger; preserve the exact requested domino count, sequential contact activation, and natural settling in one continuous take.",
        "fluid_particle_dynamics": " Preserve gravity-driven flow, container contact, volume, splash timing, surface reconstruction, and settling in one continuous take.",
    }
    return prompt + suffixes.get(
        capability_id,
        " Preserve gravity, contact timing, object identity, and natural settling in one continuous take.",
    )


def _refinement_contract(capability_id: str, *, break_case: bool) -> tuple[list[str], list[str], list[str]]:
    appearance = [
        "photorealistic materials, textures, lighting, reflections, shadows, and anti-aliased edges",
        "replace blockout geometry and flat proxy surfaces with qualified real-scale assets",
        "natural motion blur and exposure without changing positions or timing",
    ]
    preserve = [
        "camera path, framing, duration, and frame cadence",
        "every object identity, count, shape, color, marking, and scale",
        "all contacts, trajectories, spin, deformation, event times, and final poses",
    ]
    if break_case:
        appearance.insert(
            0,
            "a regulation six-pocket billiards table with realistic green worsted cloth, hardwood rails, rubber cushions, pocket jaws and dark liners",
        )
        appearance.append("standard distinct numbered solids and stripes with realistic resin gloss")
        preserve.append("exactly one cue ball and fifteen object balls with unchanged numbers and colors")
    elif capability_id == "fluid_particle_dynamics":
        appearance.append("clear low-viscosity water, realistic refraction, vessel materials, and coherent thin streams")
        preserve.append("liquid volume, source drainage, receiver fill, stream path, and splash timing")
    elif capability_id == "sequential_contact_propagation":
        appearance.append("real domino materials, beveled edges, floor contact, and environment depth")
        preserve.append("domino count, activation order, adjacent contacts, and fall directions")
    learned = active_review_requirements(capability_id)
    appearance.extend(learned.get("appearance_prompt", []))
    preserve.extend(learned.get("preservation_prompt", []))
    quality_gates = learned.get("source_quality_gate", [])
    return list(dict.fromkeys(appearance)), list(dict.fromkeys(preserve)), list(dict.fromkeys(quality_gates))


def _billiards_break_template() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "cases/billiards/sixteen_ball_reference_break.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        key: copy.deepcopy(data[key])
        for key in (
            "task_type",
            "scene",
            "physical_parameters",
            "expected_physics",
            "objects",
            "active_objects",
            "passive_objects",
            "required_assets",
        )
    }


def case_template(capability_id: str) -> dict[str, Any]:
    if capability_id == "rigid_body_contact_causality":
        return {
            "task_type": "billiards_collision",
            "scene": {"layout": "flat_table_single_target", "duration_s": 3.0, "coordinate_system": "z_up"},
            "physical_parameters": {"restitution": 0.86, "table_dynamic_friction": 0.035},
            "expected_physics": {"collision_graph": [["cue_ball", "target_ball_1"]], "passive_stationary_until_contact": True},
            "objects": [
                ball("cue_ball", "active_striker", [-1.2, 0.0, 0.09], [1.2, 0.0, 0.0]),
                ball("target_ball_1", "passive_target", [0.0, 0.0, 0.09], [0.0, 0.0, 0.0]),
                support("table", [3.0, 1.6, 0.1]),
            ],
            "active_objects": ["cue_ball"],
            "passive_objects": ["target_ball_1"],
            "required_assets": ["billiard ball", "low-friction table collider"],
        }
    if capability_id == "sequential_contact_propagation":
        dominoes = [
            {
                "id": f"domino_{index + 1:02d}",
                "role": "active_chain_driver" if index == 0 else "passive_target",
                "shape": "box",
                "size_m": [0.06, 0.18, 0.42],
                "mass_kg": 0.08,
                "initial_position_m": [index * 0.16, 0.0, 0.21],
                "initial_velocity_m_s": [0.35, 0.0, 0.0] if index == 0 else [0.0, 0.0, 0.0],
                "asset_query": "domino block",
            }
            for index in range(5)
        ]
        return {
            "task_type": "domino_chain",
            "scene": {"layout": "linear_domino_chain", "duration_s": 3.0, "coordinate_system": "z_up"},
            "physical_parameters": {"restitution": 0.25, "dynamic_friction": 0.45},
            "expected_physics": {"ordered_contact_propagation": True},
            "objects": [*dominoes, support("floor", [2.0, 1.0, 0.1])],
            "active_objects": ["domino_01"],
            "passive_objects": [item["id"] for item in dominoes[1:]],
            "required_assets": ["domino block", "floor collider"],
        }
    if capability_id == "fluid_particle_dynamics":
        return {
            "task_type": "fluid_drop_in_basin",
            "scene": {"layout": "fluid_source_over_basin", "duration_s": 1.0, "coordinate_system": "z_up"},
            "physical_parameters": {"density_kg_m3": 1000.0, "particle_size_m": 0.025},
            "expected_physics": {"particle_count_conserved": True, "surface_reconstruction_required": True},
            "objects": [
                {"id": "fluid_source", "role": "fluid_volume", "shape": "box", "size_m": [0.3, 0.3, 0.3], "initial_position_m": [0.0, 0.0, 0.7], "asset_query": "water material"},
                {"id": "basin", "role": "support", "shape": "box", "size_m": [1.0, 1.0, 0.1], "initial_position_m": [0.0, 0.0, 0.0], "asset_query": "basin container"},
            ],
            "active_objects": ["fluid_source"],
            "passive_objects": ["basin"],
            "required_assets": ["water material", "basin collider", "surface reconstruction cache"],
        }
    return {
        "task_type": "gravity_drop",
        "scene": {"layout": "body_over_floor", "duration_s": 3.0, "coordinate_system": "z_up"},
        "physical_parameters": {"gravity_m_s2": [0.0, 0.0, -9.81], "restitution": 0.25},
        "expected_physics": {"downward_acceleration_before_contact": True, "support_contact_required": True},
        "objects": [
            {"id": "falling_body", "role": "falling_body", "shape": "box", "size_m": [0.3, 0.3, 0.3], "mass_kg": 1.0, "initial_position_m": [0.0, 0.0, 1.5], "initial_velocity_m_s": [0.0, 0.0, 0.0], "asset_query": "rigid crate"},
            support("floor", [3.0, 3.0, 0.1]),
        ],
        "active_objects": ["falling_body"],
        "passive_objects": ["floor"],
        "required_assets": ["rigid body collider", "floor collider"],
    }


def ball(object_id: str, role: str, position: list[float], velocity: list[float]) -> dict[str, Any]:
    return {
        "id": object_id,
        "role": role,
        "shape": "sphere",
        "radius_m": 0.09,
        "collider": "sphere",
        "mass_kg": 0.17,
        "initial_position_m": position,
        "initial_velocity_m_s": velocity,
        "asset_query": "/Game/Props/Decorative/SM_8Ball.SM_8Ball",
    }


def support(object_id: str, size: list[float]) -> dict[str, Any]:
    return {
        "id": object_id,
        "role": "support",
        "shape": "box",
        "size_m": size,
        "collider": "box",
        "initial_position_m": [0.0, 0.0, 0.0],
        "asset_query": "analytic low friction support",
    }


def first_number(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return float(match.group(1)) if match else None
