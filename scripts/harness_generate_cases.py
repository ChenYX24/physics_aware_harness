from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.artifact_schema import write_json
from harness.core.case_spec import CASE_SPEC_SCHEMA_VERSION, validate_case_spec


TEMPLATE_SCHEMA_VERSION = "harness_case_template_v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate parameterized harness case specs from a template.")
    parser.add_argument("--template", help="Path to cases/templates/*.template.json")
    parser.add_argument("--suite", choices=["billiards", "domino", "falling", "ramp", "basic_physics"], help="Named case suite shortcut.")
    parser.add_argument("--num-cases", type=int, help="Number of case specs to generate.")
    parser.add_argument("--count", type=int, help="Alias for --num-cases.")
    parser.add_argument("--seed", type=int, default=0, help="Deterministic generation seed.")
    parser.add_argument("--out", required=True, help="Output directory for generated case specs.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    template_arg = args.template or template_for_suite(args.suite)
    if not template_arg:
        raise ValueError("provide either --template or --suite")
    num_cases = args.num_cases if args.num_cases is not None else args.count
    if num_cases is None:
        raise ValueError("provide either --num-cases or --count")
    if num_cases <= 0:
        raise ValueError("case count must be positive")

    template_path = Path(template_arg)
    if not template_path.is_absolute():
        template_path = ROOT / template_path
    template = json.loads(template_path.read_text(encoding="utf-8"))
    if template.get("schema_version") != TEMPLATE_SCHEMA_VERSION:
        raise ValueError("template schema_version must be harness_case_template_v1")
    if not template.get("fallback_supported"):
        raise ValueError(f"template is contract-only and cannot generate runnable fallback cases: {template.get('template_id')}")

    output_dir = Path(args.out)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("*.json"):
        stale.unlink()

    rng = random.Random(args.seed)
    cases = []
    generation_plan = build_generation_plan(template, rng, num_cases)
    for index, negative_mode in enumerate(generation_plan):
        case = generate_case(template, rng, index=index, seed=args.seed, negative_mode=negative_mode)
        validate_case_spec(case)
        rel_path = f"{case['case_id']}.json"
        write_json(output_dir / rel_path, case)
        cases.append({"case_id": case["case_id"], "path": rel_path, "should_pass": case["should_pass"], "negative_mode": case.get("negative_mode")})

    manifest = {
        "schema_version": "harness_generated_case_manifest_v1",
        "template_id": template["template_id"],
        "capability_id": template["capability_id"],
        "seed": args.seed,
        "num_cases": num_cases,
        "positive_count": sum(1 for case in cases if case["should_pass"]),
        "negative_count": sum(1 for case in cases if not case["should_pass"]),
        "cases": cases,
    }
    write_json(output_dir / "manifest.json", manifest)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


def template_for_suite(suite: str | None) -> str | None:
    if suite is None:
        return None
    return {
        "billiards": "cases/templates/billiards_collision.template.json",
        "domino": "cases/templates/domino_chain.template.json",
        "falling": "cases/templates/falling_blocks.template.json",
        "ramp": "cases/templates/ramp_sliding.template.json",
        "basic_physics": "cases/templates/falling_blocks.template.json",
    }[suite]


def build_generation_plan(template: dict[str, Any], rng: random.Random, count: int) -> list[str | None]:
    negative_modes = [str(item) for item in template.get("negative_modes", [])]
    if not negative_modes:
        return [None] * count
    positive_ratio = float((template.get("perturbation_policy") or {}).get("positive_ratio", 0.7))
    negative_count = max(1, math.floor(count * max(0.0, 1.0 - positive_ratio) + 1e-9))
    if count >= 10:
        negative_count = max(negative_count, 3)
    negative_count = min(count, negative_count)
    plan: list[str | None] = [None] * (count - negative_count)
    plan.extend(negative_modes[index % len(negative_modes)] for index in range(negative_count))
    rng.shuffle(plan)
    return plan


def generate_case(template: dict[str, Any], rng: random.Random, *, index: int, seed: int, negative_mode: str | None) -> dict[str, Any]:
    template_id = str(template["template_id"])
    params = sample_parameters(template, rng)
    should_pass = negative_mode is None
    if template_id == "billiards_collision":
        return billiards_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "domino_chain":
        return domino_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "falling_blocks":
        return falling_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "ramp_sliding":
        return ramp_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    raise ValueError(f"unsupported runnable template: {template_id}")


def sample_parameters(template: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, spec in (template.get("parameter_ranges") or {}).items():
        if spec.get("type") == "int":
            params[key] = rng.randint(int(spec["min"]), int(spec["max"]))
        elif spec.get("type") == "float":
            params[key] = round(rng.uniform(float(spec["min"]), float(spec["max"])), 4)
        else:
            raise ValueError(f"unsupported parameter type for {key}: {spec.get('type')}")
    return params


def base_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    case_id = f"{template['template_id']}_seed{seed}_{index:03d}"
    if negative_mode:
        case_id += f"_negative_{negative_mode}"
    expected_failure = expected_failure_for(negative_mode)
    return {
        "schema_version": CASE_SPEC_SCHEMA_VERSION,
        "case_id": case_id,
        "capability_id": template["capability_id"],
        "prompt": "",
        "expected_physics": {},
        "objects": [],
        "active_objects": [],
        "passive_objects": [],
        "required_assets": list(template.get("physics_critical_assets", [])),
        "required_signals": required_signals_for(str(template["capability_id"])),
        "verifier_expectation": {"status": "pass"} if should_pass else {"status": "fail", "failure_type": expected_failure},
        "should_pass": should_pass,
        "notes": "Generated from parameterized harness template.",
        "template_id": template["template_id"],
        "seed": seed,
        "parameter_sample": params,
        "expected_invariants": list(template.get("expected_invariants", [])),
    } | ({"negative_mode": negative_mode} if negative_mode else {})


def billiards_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    radius = float(params["ball_radius_m"])
    target_count = int(params["target_ball_count"])
    gap = float(params["target_gap_m"])
    spacing = 2 * radius + gap
    cue_x = -max(0.9, spacing * 3.0)
    target_start_x = 0.0
    objects = [
        {
            "id": "cue_ball",
            "role": "active_striker",
            "shape": "sphere",
            "radius_m": radius,
            "mass_kg": params["mass_kg"],
            "restitution": params["restitution"],
            "initial_position_m": [round(cue_x, 4), 0.0, radius],
            "initial_velocity_m_s": [params["cue_speed_m_s"], 0.0, 0.0],
        }
    ]
    for idx in range(target_count):
        x = target_start_x + idx * spacing
        if negative_mode == "initial_overlap" and idx == 0:
            x = cue_x + radius * 1.2
        objects.append(
            {
                "id": f"target_ball_{idx + 1}",
                "role": "passive_target",
                "shape": "sphere",
                "radius_m": radius,
                "mass_kg": params["mass_kg"],
                "restitution": params["restitution"],
                "initial_position_m": [round(x, 4), 0.0, radius],
                "initial_velocity_m_s": [0.0, 0.0, 0.0],
            }
        )
    objects.append({"id": "table", "role": "support", "shape": "box", "initial_position_m": [0.0, 0.0, 0.0]})
    graph = [["cue_ball", "target_ball_1"]]
    graph += [[f"target_ball_{idx}", f"target_ball_{idx + 1}"] for idx in range(1, target_count)]
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated billiards collision with one cue ball and {target_count} passive target balls.",
            "expected_physics": {"collision_graph": graph, "coordinate_system": "z_up", "camera": {"mode": "top_down_fixed"}},
            "objects": objects,
            "active_objects": ["cue_ball"],
            "passive_objects": [f"target_ball_{idx + 1}" for idx in range(target_count)],
        }
    )
    return add_m2_case_contract(case, template, params)


def domino_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    count = int(params["domino_count"])
    spacing = float(params["spacing_m"])
    objects = [
        {"id": f"domino_{idx}", "role": "domino", "initial_position_m": [round(idx * spacing, 4), 0.0, round(float(params["domino_height_m"]) / 2, 4)]}
        for idx in range(count)
    ]
    ordered_chain = [f"domino_{idx}" for idx in range(count)]
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated domino chain with {count} dominoes and sequential contact propagation.",
            "expected_physics": {"ordered_chain": ordered_chain, "trigger_rotation_deg": params["trigger_rotation_deg"]},
            "objects": objects,
            "active_objects": ["domino_0"],
            "passive_objects": ordered_chain[1:],
        }
    )
    return add_m2_case_contract(case, template, params)


def falling_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    count = int(params["block_count"])
    drop_height = float(params["drop_height_m"])
    objects = []
    for idx in range(count):
        role = "falling_body" if idx == 0 else "stack_block"
        objects.append(
            {
                "id": f"falling_block_{idx + 1}",
                "role": role,
                "shape": "box",
                "mass_kg": params["block_mass_kg"],
                "initial_position_m": [round(idx * 0.18, 4), 0.0, round(drop_height + idx * 0.12, 4)],
            }
        )
    objects.append({"id": "floor", "role": "support", "shape": "box", "initial_position_m": [0.0, 0.0, 0.0]})
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated falling-block gravity/contact case with {count} falling bodies.",
            "expected_physics": {"gravity_m_s2": params["gravity_m_s2"], "support": "floor", "coordinate_system": "z_up"},
            "objects": objects,
            "active_objects": [],
            "passive_objects": [f"falling_block_{idx + 1}" for idx in range(count)],
        }
    )
    return add_m2_case_contract(case, template, params)


def ramp_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    slope_angle = float(params["slope_angle_deg"])
    friction = float(params["friction_dynamic"])
    travel = ramp_expected_travel(slope_angle, friction)
    min_travel = max(0.04, travel * 0.7)
    max_travel = max(min_travel + 0.02, travel * 1.35)
    objects = [
        {
            "id": "ramp_subject",
            "role": "rolling_subject",
            "shape": "sphere",
            "radius_m": 0.12,
            "mass_kg": params["subject_mass_kg"],
            "friction_dynamic": friction,
            "initial_position_m": [0.0, 0.0, 0.85],
            "initial_velocity_m_s": [0.0, 0.0, 0.0],
        },
        {
            "id": "ramp",
            "role": "ramp",
            "shape": "inclined_plane",
            "friction_dynamic": friction,
            "slope_angle_deg": slope_angle,
            "initial_position_m": [0.6, 0.0, 0.4],
            "initial_rotation_deg": [0.0, round(-slope_angle, 4), 0.0],
        },
    ]
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated ramp case: a passive rigid body starts at rest and rolls/slides downhill on a {slope_angle:.1f} degree ramp.",
            "expected_physics": {
                "coordinate_system": "z_up",
                "downhill_axis": "+x",
                "slope_angle_deg": slope_angle,
                "friction_dynamic": friction,
                "expected_min_downhill_displacement_m": round(min_travel, 4),
                "expected_max_downhill_displacement_m": round(max_travel, 4),
                "fallback_downhill_displacement_m": round(travel, 4),
                "contact_surface": "ramp",
            },
            "objects": objects,
            "active_objects": [],
            "passive_objects": ["ramp_subject"],
        }
    )
    return add_m2_case_contract(case, template, params)


def ramp_expected_travel(slope_angle_deg: float, friction_dynamic: float) -> float:
    slope_factor = max(0.05, math.sin(math.radians(slope_angle_deg)))
    friction_factor = max(0.08, 1.0 - friction_dynamic)
    return round(1.6 * slope_factor * friction_factor, 4)


def add_m2_case_contract(case: dict[str, Any], template: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    expected_physics = dict(case.get("expected_physics") or {})
    camera_policy = dict(template.get("camera_policy") or {})
    objects = [obj for obj in case.get("objects", []) if isinstance(obj, dict)]
    initial_state = {}
    for obj in objects:
        object_id = str(obj.get("id"))
        initial_state[object_id] = {
            "position_m": obj.get("initial_position_m", [0.0, 0.0, 0.0]),
            "velocity_m_s": obj.get("initial_velocity_m_s", [0.0, 0.0, 0.0]),
            "rotation_deg": obj.get("initial_rotation_deg", obj.get("initial_rotation", [0.0, 0.0, 0.0])),
        }
    case.update(
        {
            "task_type": template["template_id"],
            "scene": {
                "coordinate_system": expected_physics.get("coordinate_system", "z_up"),
                "environment": "analytic_harness_scene",
                "camera_policy": {
                    "selected": camera_policy.get("default", "side_static"),
                    "allowed": camera_policy.get("allowed", []),
                },
            },
            "initial_state": initial_state,
            "physical_parameters": {
                "sampled": params,
                "expected_physics": expected_physics,
                "active_objects": case.get("active_objects", []),
                "passive_objects": case.get("passive_objects", []),
            },
            "expected_event": expected_event_for(case),
            "negative_or_boundary": not bool(case.get("should_pass")),
            "asset_requirements": {
                "physics_critical": list(template.get("physics_critical_assets", [])),
                "visual_only": list(template.get("visual_only_assets", [])),
            },
            "allowed_proxy_policy": {
                "analytic_proxy_allowed": True,
                "physics_critical_proxy_must_be_marked": True,
                "fallback_backend_is_toy": True,
            },
            "verification_rules": {
                "expected_invariants": list(case.get("expected_invariants", [])),
                "verifier_expectation": case.get("verifier_expectation", {}),
            },
        }
    )
    return case


def expected_event_for(case: dict[str, Any]) -> dict[str, Any]:
    capability_id = str(case.get("capability_id"))
    expected_physics = dict(case.get("expected_physics") or {})
    if capability_id == "billiard_causality_compiler":
        return {"type": "rigid_body_contact", "collision_graph": expected_physics.get("collision_graph", [])}
    if capability_id == "sequential_contact_propagation":
        return {"type": "ordered_contact_chain", "ordered_chain": expected_physics.get("ordered_chain", [])}
    if capability_id == "rigid_body_gravity_collision":
        return {"type": "gravity_support_contact", "support": expected_physics.get("support", "floor")}
    if capability_id == "ramp_sliding_friction":
        return {"type": "friction_sensitive_downhill_motion", "contact_surface": expected_physics.get("contact_surface", "ramp")}
    return {"type": "trajectory_event"}


def required_signals_for(capability_id: str) -> list[str]:
    if capability_id == "billiard_causality_compiler":
        return ["trajectory", "contact_events", "camera_trajectory"]
    if capability_id == "sequential_contact_propagation":
        return ["trajectory", "contact_events", "rotation"]
    if capability_id == "rigid_body_gravity_collision":
        return ["trajectory", "contact_events", "gravity_label"]
    if capability_id == "ramp_sliding_friction":
        return ["trajectory", "contact_events", "ramp_angle_label", "material_friction_label"]
    return ["trajectory"]


def expected_failure_for(negative_mode: str | None) -> str:
    if negative_mode == "precontact_motion":
        return "F5_passive_precontact_motion"
    if negative_mode in {"missing_contact"}:
        return "F2_missing_contact_events"
    return "F4_causality_violation"


if __name__ == "__main__":
    raise SystemExit(main())
