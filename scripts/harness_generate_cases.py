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
    parser.add_argument("--suite", choices=["billiards", "domino", "falling", "ramp", "projectile", "bounce", "rolling", "sliding", "wind", "mass_ratio", "spin", "agent_action", "pendulum", "impulse_chain", "elastic_launch", "elastic_constraint", "basic_physics"], help="Named case suite shortcut.")
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
        "projectile": "cases/templates/projectile_motion.template.json",
        "bounce": "cases/templates/bounce_restitution.template.json",
        "rolling": "cases/templates/rolling_friction.template.json",
        "sliding": "cases/templates/sliding_crate_friction.template.json",
        "wind": "cases/templates/wind_balloon_drift.template.json",
        "mass_ratio": "cases/templates/mass_ratio_collision.template.json",
        "spin": "cases/templates/angular_damping_spin.template.json",
        "agent_action": "cases/templates/agent_rigidbody_action.template.json",
        "pendulum": "cases/templates/pendulum_contact.template.json",
        "impulse_chain": "cases/templates/constraint_momentum_transfer.template.json",
        "elastic_launch": "cases/templates/elastic_energy_launch.template.json",
        "elastic_constraint": "cases/templates/elastic_constraint_rebound.template.json",
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
    if template_id == "projectile_motion":
        return projectile_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "bounce_restitution":
        return bounce_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "rolling_friction":
        return rolling_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "sliding_crate_friction":
        return sliding_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "wind_balloon_drift":
        return wind_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "mass_ratio_collision":
        return mass_ratio_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "angular_damping_spin":
        return spin_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "agent_rigidbody_action":
        return agent_action_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "pendulum_contact":
        return pendulum_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "constraint_momentum_transfer":
        return impulse_chain_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "elastic_energy_launch":
        return elastic_launch_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    if template_id == "elastic_constraint_rebound":
        return elastic_constraint_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
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


def projectile_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    speed = float(params["launch_speed_m_s"])
    angle = float(params["launch_angle_deg"])
    angle_rad = math.radians(angle)
    vx = round(speed * math.cos(angle_rad), 4)
    vz = round(speed * math.sin(angle_rad), 4)
    gravity = float(params["gravity_m_s2"])
    time_to_ground = max(0.2, (vz + math.sqrt(max(vz * vz + 2 * gravity * 0.18, 0.0))) / gravity)
    expected_range = vx * time_to_ground
    objects = [
        {
            "id": "projectile",
            "role": "projectile",
            "shape": "sphere",
            "radius_m": 0.1,
            "mass_kg": params["projectile_mass_kg"],
            "initial_position_m": [0.0, 0.0, 0.18],
            "initial_velocity_m_s": [vx, 0.0, vz],
        },
        {"id": "ground", "role": "ground", "shape": "box", "initial_position_m": [0.0, 0.0, 0.0]},
    ]
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated projectile case: a rigid body is launched at {angle:.1f} degrees and follows gravity until landing.",
            "expected_physics": {
                "coordinate_system": "z_up",
                "gravity_m_s2": gravity,
                "launch_angle_deg": angle,
                "launch_speed_m_s": speed,
                "expected_min_forward_displacement_m": round(max(0.08, expected_range * 0.25), 4),
                "expected_range_proxy_m": round(expected_range, 4),
                "support": "ground",
            },
            "objects": objects,
            "active_objects": ["projectile"],
            "passive_objects": [],
        }
    )
    return add_m2_case_contract(case, template, params)


def bounce_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    drop_height = float(params["drop_height_m"])
    restitution = float(params["restitution"])
    radius = 0.12
    min_ratio = max(0.02, restitution * restitution * 0.55)
    max_ratio = min(1.1, restitution * restitution * 1.35 + 0.04)
    objects = [
        {
            "id": "bounce_ball",
            "role": "bouncing_body",
            "shape": "sphere",
            "radius_m": radius,
            "mass_kg": params["ball_mass_kg"],
            "restitution": restitution,
            "initial_position_m": [0.0, 0.0, round(drop_height + radius, 4)],
            "initial_velocity_m_s": [0.0, 0.0, 0.0],
        },
        {"id": "floor", "role": "support", "shape": "box", "initial_position_m": [0.0, 0.0, 0.0]},
    ]
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated bounce case: a rigid body drops from {drop_height:.2f} m and rebounds according to restitution {restitution:.2f}.",
            "expected_physics": {
                "coordinate_system": "z_up",
                "gravity_m_s2": params["gravity_m_s2"],
                "drop_height_m": drop_height,
                "restitution": restitution,
                "expected_min_rebound_ratio": round(min_ratio, 4),
                "expected_max_rebound_ratio": round(max_ratio, 4),
                "support": "floor",
            },
            "objects": objects,
            "active_objects": [],
            "passive_objects": ["bounce_ball"],
        }
    )
    return add_m2_case_contract(case, template, params)


def rolling_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    speed = float(params["initial_speed_m_s"])
    friction = float(params["friction_dynamic"])
    gravity = float(params["gravity_m_s2"])
    radius = 0.12
    physics_stop_distance = speed * speed / max(2.0 * friction * gravity, 1e-6)
    proxy_distance = max(0.08, min(3.2, physics_stop_distance * 0.75))
    min_distance = max(0.03, proxy_distance * 0.45)
    max_distance = max(min_distance + 0.05, proxy_distance * 1.65)
    final_speed_max = max(0.08, speed * max(0.12, 0.38 - friction * 0.22))
    objects = [
        {
            "id": "rolling_ball",
            "role": "rolling_body",
            "shape": "sphere",
            "radius_m": radius,
            "mass_kg": params["ball_mass_kg"],
            "friction_dynamic": friction,
            "initial_position_m": [0.0, 0.0, radius],
            "initial_velocity_m_s": [speed, 0.0, 0.0],
        },
        {"id": "floor", "role": "support", "shape": "box", "friction_dynamic": friction, "initial_position_m": [0.0, 0.0, 0.0]},
    ]
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated rolling-friction case: a rigid body starts at {speed:.2f} m/s and slows on a flat surface with friction {friction:.2f}.",
            "expected_physics": {
                "coordinate_system": "z_up",
                "gravity_m_s2": gravity,
                "initial_speed_m_s": speed,
                "friction_dynamic": friction,
                "expected_min_roll_distance_m": round(min_distance, 4),
                "expected_max_roll_distance_m": round(max_distance, 4),
                "expected_final_speed_max_m_s": round(final_speed_max, 4),
                "fallback_roll_distance_m": round(proxy_distance, 4),
                "support": "floor",
            },
            "objects": objects,
            "active_objects": ["rolling_ball"],
            "passive_objects": [],
        }
    )
    return add_m2_case_contract(case, template, params)


def sliding_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    mode = "static_threshold" if negative_mode == "static_threshold_violation" or (should_pass and index % 4 == 0) else "sliding_stop"
    mass = float(params["crate_mass_kg"])
    friction_dynamic = float(params["friction_dynamic"])
    friction_static = max(float(params["friction_static"]), friction_dynamic + 0.05)
    gravity = float(params["gravity_m_s2"])
    speed = float(params["initial_speed_m_s"])
    z = 0.25
    if mode == "static_threshold":
        static_limit = mass * gravity * friction_static
        applied_force = round(static_limit * 0.55, 4)
        expected = {
            "coordinate_system": "z_up",
            "gravity_m_s2": gravity,
            "mode": "static_threshold",
            "crate_mass_kg": mass,
            "friction_dynamic": friction_dynamic,
            "friction_static": friction_static,
            "applied_force_n": applied_force,
            "static_friction_limit_n": round(static_limit, 4),
            "max_static_displacement_m": 0.02,
            "expected_final_speed_max_m_s": 0.02,
            "support": "floor",
        }
        initial_velocity = [0.0, 0.0, 0.0]
    else:
        stop_distance = speed * speed / max(2.0 * friction_dynamic * gravity, 1e-6)
        proxy_distance = max(0.06, min(2.5, stop_distance * 0.8))
        expected = {
            "coordinate_system": "z_up",
            "gravity_m_s2": gravity,
            "mode": "sliding_stop",
            "initial_speed_m_s": speed,
            "friction_dynamic": friction_dynamic,
            "friction_static": friction_static,
            "expected_min_slide_distance_m": round(max(0.03, proxy_distance * 0.45), 4),
            "expected_max_slide_distance_m": round(max(0.1, proxy_distance * 1.6), 4),
            "expected_final_speed_max_m_s": round(max(0.06, speed * 0.15), 4),
            "fallback_slide_distance_m": round(proxy_distance, 4),
            "support": "floor",
        }
        initial_velocity = [speed, 0.0, 0.0]
    objects = [
        {
            "id": "sliding_crate",
            "role": "sliding_body",
            "shape": "box",
            "mass_kg": mass,
            "friction_dynamic": friction_dynamic,
            "friction_static": friction_static,
            "initial_position_m": [0.0, 0.0, z],
            "initial_velocity_m_s": initial_velocity,
        },
        {"id": "floor", "role": "support", "shape": "box", "friction_dynamic": friction_dynamic, "friction_static": friction_static, "initial_position_m": [0.0, 0.0, 0.0]},
    ]
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated sliding-friction case: mode={mode}, dynamic friction={friction_dynamic:.2f}, static friction={friction_static:.2f}.",
            "expected_physics": expected,
            "objects": objects,
            "active_objects": ["sliding_crate"] if mode == "sliding_stop" else [],
            "passive_objects": [] if mode == "sliding_stop" else ["sliding_crate"],
        }
    )
    return add_m2_case_contract(case, template, params)


def wind_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    speed = float(params["wind_speed_m_s"])
    angle = float(params["wind_angle_deg"])
    angle_rad = math.radians(angle)
    wind = [round(speed * math.cos(angle_rad), 4), round(speed * math.sin(angle_rad), 4), 0.0]
    mass = float(params["body_mass_kg"])
    buoyancy = float(params["buoyancy_scale"])
    drift = max(0.12, min(2.2, speed * buoyancy * 0.25 / max(mass, 0.04)))
    expected = {
        "coordinate_system": "z_up",
        "wind_vector_m_s": wind,
        "wind_speed_m_s": speed,
        "wind_angle_deg": angle,
        "body_mass_kg": mass,
        "buoyancy_scale": buoyancy,
        "expected_min_wind_aligned_drift_m": round(max(0.04, drift * 0.45), 4),
        "expected_max_wind_aligned_drift_m": round(max(0.12, drift * 1.65), 4),
        "fallback_wind_drift_m": round(drift, 4),
        "expected_min_altitude_m": 0.82,
        "expected_max_altitude_m": 1.3,
    }
    if negative_mode == "missing_wind_label":
        expected.pop("wind_vector_m_s", None)
    objects = [
        {
            "id": "wind_body",
            "role": "wind_drift_body",
            "shape": "sphere",
            "radius_m": 0.16,
            "mass_kg": mass,
            "buoyancy_scale": buoyancy,
            "initial_position_m": [0.0, 0.0, 1.0],
            "initial_velocity_m_s": [0.0, 0.0, 0.0],
        }
    ]
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated wind-drift case: a light rigid body is pushed by a {speed:.2f} m/s wind at {angle:.1f} degrees.",
            "expected_physics": expected,
            "objects": objects,
            "active_objects": [],
            "passive_objects": ["wind_body"],
        }
    )
    return add_m2_case_contract(case, template, params)


def mass_ratio_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    striker_mass = float(params["striker_mass_kg"])
    target_mass = float(params["target_mass_kg"])
    speed = float(params["initial_speed_m_s"])
    restitution = float(params["restitution"])
    v1_post, v2_post = collision_speeds(striker_mass, target_mass, speed, restitution)
    if striker_mass >= target_mass:
        order = "target_faster_than_striker"
    else:
        order = "target_slower_than_initial"
    target_min = max(0.03, abs(v2_post) * 0.55)
    target_max = max(target_min + 0.05, abs(v2_post) * 1.45 + 0.04)
    striker_abs_max = max(0.05, abs(v1_post) * 1.55 + 0.05)
    expected = {
        "coordinate_system": "z_up",
        "collision_axis": "+x",
        "collision_graph": [["striker", "target"]],
        "striker_mass_kg": striker_mass,
        "target_mass_kg": target_mass,
        "initial_speed_m_s": speed,
        "restitution": restitution,
        "expected_velocity_order": order,
        "expected_target_speed_min_m_s": round(target_min, 4),
        "expected_target_speed_max_m_s": round(target_max, 4),
        "expected_striker_speed_abs_max_m_s": round(striker_abs_max, 4),
        "expected_energy_ratio_max": 1.05,
    }
    objects = [
        {
            "id": "striker",
            "role": "active_striker",
            "shape": "sphere",
            "radius_m": 0.12,
            "mass_kg": striker_mass,
            "restitution": restitution,
            "initial_position_m": [-0.5, 0.0, 0.12],
            "initial_velocity_m_s": [speed, 0.0, 0.0],
        },
        {
            "id": "target",
            "role": "passive_target",
            "shape": "sphere",
            "radius_m": 0.12,
            "mass_kg": target_mass,
            "restitution": restitution,
            "initial_position_m": [0.0, 0.0, 0.12],
            "initial_velocity_m_s": [0.0, 0.0, 0.0],
        },
        {"id": "floor", "role": "support", "shape": "box", "initial_position_m": [0.0, 0.0, 0.0]},
    ]
    if negative_mode == "missing_mass_label":
        objects[1].pop("mass_kg", None)
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated mass-ratio collision: active mass={striker_mass:.2f} kg hits passive mass={target_mass:.2f} kg at {speed:.2f} m/s.",
            "expected_physics": expected,
            "objects": objects,
            "active_objects": ["striker"],
            "passive_objects": ["target"],
        }
    )
    return add_m2_case_contract(case, template, params)


def spin_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    initial_speed = float(params["initial_angular_speed_deg_s"])
    damping = float(params["angular_damping"])
    duration = float(params["spin_duration_s"])
    final_speed = initial_speed * math.exp(-damping * duration)
    speed_drop = initial_speed - final_speed
    rotation_delta = (initial_speed - final_speed) / max(damping, 1e-6)
    expected = {
        "coordinate_system": "z_up",
        "spin_axis": "z",
        "initial_angular_speed_deg_s": round(initial_speed, 4),
        "angular_damping": round(damping, 4),
        "spin_duration_s": round(duration, 4),
        "expected_final_angular_speed_max_deg_s": round(final_speed * 1.25 + 15.0, 4),
        "expected_min_angular_speed_drop_deg_s": round(max(10.0, speed_drop * 0.55), 4),
        "expected_min_rotation_delta_deg": round(max(30.0, rotation_delta * 0.45), 4),
    }
    objects = [
        {
            "id": "spinner",
            "role": "spinning_body",
            "shape": "sphere",
            "radius_m": 0.16,
            "mass_kg": params["body_mass_kg"],
            "angular_damping": damping,
            "initial_position_m": [0.0, 0.0, 0.2],
            "initial_velocity_m_s": [0.0, 0.0, 0.0],
            "initial_rotation_deg": [0.0, 0.0, 0.0],
            "initial_angular_velocity_deg_s": [0.0, 0.0, round(initial_speed, 4)],
        }
    ]
    if negative_mode == "missing_angular_velocity_label":
        expected.pop("initial_angular_speed_deg_s", None)
        expected.pop("angular_damping", None)
        objects[0].pop("initial_angular_velocity_deg_s", None)
        objects[0].pop("angular_damping", None)
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated angular-damping case: a rigid body spins at {initial_speed:.1f} deg/s with damping {damping:.2f}.",
            "expected_physics": expected,
            "objects": objects,
            "active_objects": ["spinner"],
            "passive_objects": [],
        }
    )
    return add_m2_case_contract(case, template, params)


def agent_action_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    action_time = float(params["action_time_s"])
    action_frame = 1
    coupling_type = "throw" if index % 3 == 1 and negative_mode != "no_post_action_motion" else "push"
    target_id = "ball" if coupling_type == "throw" else "box"
    target_shape = "sphere" if coupling_type == "throw" else "box"
    target_z = 0.8 if coupling_type == "throw" else 0.25
    impulse = [round(float(params["throw_speed_m_s"]) * 0.75, 4), 0.0, round(float(params["throw_speed_m_s"]) * 0.5, 4)] if coupling_type == "throw" else [round(float(params["push_impulse_n_s"]), 4), 0.0, 0.0]
    min_speed = max(0.2, float(params["throw_speed_m_s"]) * 0.35) if coupling_type == "throw" else max(0.12, float(params["push_impulse_n_s"]) * 0.22)
    min_displacement = 0.28 if coupling_type == "throw" else 0.14
    expected = {
        "coordinate_system": "z_up",
        "coupling_type": coupling_type,
        "action_actor_id": "agent",
        "target_object_id": target_id,
        "action_type": coupling_type,
        "action_frame": action_frame,
        "action_time_s": round(action_time, 4),
        "expected_contact_pair": ["agent", target_id],
        "expected_min_target_displacement_m": round(min_displacement, 4),
        "expected_min_post_action_speed_m_s": round(min_speed, 4),
        "passive_pre_action_velocity_epsilon_m_s": 0.05,
        "action_trace": [
            {
                "frame": action_frame,
                "time_s": round(action_time, 4),
                "actor_id": "agent",
                "target_id": target_id,
                "action_type": coupling_type,
                "release": coupling_type == "throw",
                "impulse_n_s": impulse,
            }
        ],
    }
    if negative_mode == "missing_action_trace":
        expected.pop("action_trace", None)
    objects = [
        {
            "id": "agent",
            "role": "active_agent",
            "shape": "capsule",
            "initial_position_m": [-0.35, 0.0, 0.45 if coupling_type == "push" else 0.75],
            "initial_velocity_m_s": [0.0, 0.0, 0.0],
        },
        {
            "id": target_id,
            "role": "action_coupled_body",
            "shape": target_shape,
            "mass_kg": params["target_mass_kg"],
            "initial_position_m": [0.0, 0.0, target_z],
            "initial_velocity_m_s": [0.0, 0.0, 0.0],
        },
        {"id": "floor", "role": "support", "shape": "box", "initial_position_m": [0.0, 0.0, 0.0]},
    ]
    if target_shape == "sphere":
        objects[1]["radius_m"] = 0.12
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated agent-action case: an agent performs a {coupling_type} action on a rigid body; the target can move only after action evidence.",
            "expected_physics": expected,
            "objects": objects,
            "active_objects": ["agent"],
            "passive_objects": [target_id],
        }
    )
    return add_m2_case_contract(case, template, params)


def pendulum_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    length = float(params["constraint_length_m"])
    release_angle = float(params["release_angle_deg"])
    stiffness = float(params["constraint_stiffness"])
    angle_rad = math.radians(release_angle)
    anchor_position = [0.0, 0.0, 1.6]
    bob_position = [round(math.sin(angle_rad) * length, 4), 0.0, round(anchor_position[2] - math.cos(angle_rad) * length, 4)]
    expected = {
        "coordinate_system": "z_up",
        "anchor_object_id": "anchor",
        "constrained_object_id": "bob",
        "constraint_length_m": round(length, 4),
        "constraint_tolerance_m": round(max(0.03, length * 0.04), 4),
        "release_angle_deg": round(release_angle, 4),
        "constraint_stiffness": round(stiffness, 4),
        "expected_max_step_displacement_m": round(max(0.35, length * 0.72), 4),
        "require_center_crossing": True,
    }
    if negative_mode == "missing_constraint_label":
        expected.pop("constraint_length_m", None)
    objects = [
        {
            "id": "anchor",
            "role": "constraint_anchor",
            "shape": "fixed_point",
            "initial_position_m": anchor_position,
            "initial_velocity_m_s": [0.0, 0.0, 0.0],
            "kinematic": True,
        },
        {
            "id": "bob",
            "role": "constrained_body",
            "shape": "sphere",
            "radius_m": 0.12,
            "mass_kg": params["bob_mass_kg"],
            "initial_position_m": bob_position,
            "initial_velocity_m_s": [0.0, 0.0, 0.0],
        },
    ]
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated fixed-distance constraint case: a rigid bob swings from a {release_angle:.1f} degree release while preserving length {length:.2f} m.",
            "expected_physics": expected,
            "objects": objects,
            "active_objects": [],
            "passive_objects": ["bob"],
        }
    )
    return add_m2_case_contract(case, template, params)


def impulse_chain_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    count = int(params["chain_body_count"])
    spacing = float(params["spacing_m"])
    mass = float(params["body_mass_kg"])
    speed = float(params["initial_speed_m_s"])
    restitution = float(params["restitution"])
    chain = [f"chain_body_{idx}" for idx in range(count)]
    receiver_speed = speed * max(0.25, restitution * 0.68)
    expected = {
        "coordinate_system": "z_up",
        "chain_objects": chain,
        "active_object_id": chain[0],
        "receiver_object_id": chain[-1],
        "expected_contact_chain": [[chain[idx], chain[idx + 1]] for idx in range(count - 1)],
        "expected_min_receiver_speed_m_s": round(max(0.12, receiver_speed * 0.55), 4),
        "expected_max_intermediate_displacement_m": round(max(0.035, spacing * 0.38), 4),
        "expected_energy_ratio_max": 1.1,
        "restitution": round(restitution, 4),
    }
    objects = []
    for idx, object_id in enumerate(chain):
        objects.append(
            {
                "id": object_id,
                "role": "active_chain_driver" if idx == 0 else "constrained_chain_body",
                "shape": "sphere",
                "radius_m": round(spacing * 0.45, 4),
                "mass_kg": mass,
                "restitution": restitution,
                "initial_position_m": [round(idx * spacing, 4), 0.0, 0.9],
                "initial_velocity_m_s": [round(speed, 4), 0.0, 0.0] if idx == 0 else [0.0, 0.0, 0.0],
            }
        )
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated constrained impulse-chain case with {count} bodies; the first body drives contact transfer to the terminal receiver.",
            "expected_physics": expected,
            "objects": objects,
            "active_objects": [chain[0]],
            "passive_objects": chain[1:],
        }
    )
    return add_m2_case_contract(case, template, params)


def elastic_launch_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    spring_constant = float(params["spring_constant_n_m"])
    compression = float(params["compression_m"])
    payload_mass = float(params["payload_mass_kg"])
    launch_angle = float(params["launch_angle_deg"])
    stored_energy = 0.5 * spring_constant * compression * compression
    launch_speed = math.sqrt(max(2.0 * stored_energy / max(payload_mass, 1e-6), 0.0)) * 0.88
    angle_rad = math.radians(launch_angle)
    vx = launch_speed * math.cos(angle_rad)
    vz = launch_speed * math.sin(angle_rad)
    expected = {
        "coordinate_system": "z_up",
        "launcher_object_id": "spring",
        "launched_object_id": "payload",
        "spring_constant_n_m": round(spring_constant, 4),
        "compression_m": round(compression, 4),
        "payload_mass_kg": round(payload_mass, 4),
        "launch_angle_deg": round(launch_angle, 4),
        "stored_energy_j": round(stored_energy, 6),
        "release_frame": 1,
        "release_time_s": 0.2,
        "expected_min_launch_speed_m_s": round(max(0.08, launch_speed * 0.55), 4),
        "expected_min_height_gain_m": round(max(0.04, vz * 0.12), 4),
        "expected_min_forward_displacement_m": round(max(0.015, abs(vx) * 0.12), 4),
        "expected_max_energy_ratio": 1.25,
    }
    objects = [
        {
            "id": "spring",
            "role": "elastic_launcher",
            "shape": "spring_proxy",
            "spring_constant_n_m": spring_constant,
            "compression_m": compression,
            "initial_position_m": [0.0, 0.0, 0.1],
            "initial_velocity_m_s": [0.0, 0.0, 0.0],
            "kinematic": True,
        },
        {
            "id": "payload",
            "role": "launched_body",
            "shape": "sphere",
            "radius_m": 0.12,
            "mass_kg": payload_mass,
            "initial_position_m": [0.0, 0.0, 0.24],
            "initial_velocity_m_s": [0.0, 0.0, 0.0],
        },
        {"id": "floor", "role": "support", "shape": "box", "initial_position_m": [0.0, 0.0, 0.0]},
    ]
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated elastic launch case: a compressed spring releases {stored_energy:.2f} J into a payload at {launch_angle:.1f} degrees.",
            "expected_physics": expected,
            "objects": objects,
            "active_objects": ["spring"],
            "passive_objects": ["payload"],
        }
    )
    return add_m2_case_contract(case, template, params)


def elastic_constraint_case(template: dict[str, Any], params: dict[str, Any], *, index: int, seed: int, should_pass: bool, negative_mode: str | None) -> dict[str, Any]:
    rest_length = float(params["rest_length_m"])
    max_extension = float(params["max_extension_m"])
    stiffness = float(params["constraint_stiffness_n_m"])
    damping = float(params["damping_ratio"])
    mass = float(params["payload_mass_kg"])
    anchor_z = round(rest_length + 0.8, 4)
    payload_z = round(anchor_z - rest_length + 0.12, 4)
    expected = {
        "coordinate_system": "z_up",
        "anchor_object_id": "anchor",
        "constrained_object_id": "payload",
        "rest_length_m": round(rest_length, 4),
        "max_extension_m": round(max_extension, 4),
        "expected_min_extension_m": round(max(0.08, max_extension * 0.55), 4),
        "expected_min_rebound_speed_m_s": round(max(0.12, stiffness * max_extension / max(mass, 1e-6) * 0.004), 4),
        "constraint_stiffness_n_m": round(stiffness, 4),
        "damping_ratio": round(damping, 4),
        "payload_mass_kg": round(mass, 4),
    }
    objects = [
        {
            "id": "anchor",
            "role": "elastic_constraint_anchor",
            "shape": "fixed_point",
            "initial_position_m": [0.0, 0.0, anchor_z],
            "initial_velocity_m_s": [0.0, 0.0, 0.0],
            "kinematic": True,
        },
        {
            "id": "payload",
            "role": "elastic_constrained_body",
            "shape": "sphere",
            "radius_m": 0.12,
            "mass_kg": mass,
            "initial_position_m": [0.0, 0.0, payload_z],
            "initial_velocity_m_s": [0.0, 0.0, 0.0],
        },
    ]
    case = base_case(template, params, index=index, seed=seed, should_pass=should_pass, negative_mode=negative_mode)
    case.update(
        {
            "prompt": f"Generated elastic-constraint case: a payload stretches an elastic tether by up to {max_extension:.2f} m and rebounds toward the anchor.",
            "expected_physics": expected,
            "objects": objects,
            "active_objects": [],
            "passive_objects": ["payload"],
        }
    )
    return add_m2_case_contract(case, template, params)


def collision_speeds(striker_mass: float, target_mass: float, initial_speed: float, restitution: float) -> tuple[float, float]:
    denominator = max(striker_mass + target_mass, 1e-9)
    striker_post = ((striker_mass - restitution * target_mass) / denominator) * initial_speed
    target_post = (((1.0 + restitution) * striker_mass) / denominator) * initial_speed
    return round(striker_post, 4), round(target_post, 4)


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
            "angular_velocity_deg_s": obj.get("initial_angular_velocity_deg_s", [0.0, 0.0, 0.0]),
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
    if capability_id in {"rigid_body_contact_causality", "billiard_causality_compiler"}:
        return {"type": "rigid_body_contact", "collision_graph": expected_physics.get("collision_graph", [])}
    if capability_id == "sequential_contact_propagation":
        return {"type": "ordered_contact_chain", "ordered_chain": expected_physics.get("ordered_chain", [])}
    if capability_id == "rigid_body_gravity_collision":
        return {"type": "gravity_support_contact", "support": expected_physics.get("support", "floor")}
    if capability_id == "ramp_sliding_friction":
        return {"type": "friction_sensitive_downhill_motion", "contact_surface": expected_physics.get("contact_surface", "ramp")}
    if capability_id == "projectile_gravity_motion":
        return {"type": "projectile_landing", "support": expected_physics.get("support", "ground")}
    if capability_id == "bounce_restitution_ball":
        return {"type": "restitution_bounce", "support": expected_physics.get("support", "floor")}
    if capability_id == "rolling_friction_ball":
        return {"type": "friction_bounded_roll", "support": expected_physics.get("support", "floor")}
    if capability_id == "sliding_crate_friction":
        return {"type": "friction_bounded_slide_or_static_hold", "support": expected_physics.get("support", "floor"), "mode": expected_physics.get("mode", "sliding_stop")}
    if capability_id == "force_field_wind_drift":
        return {"type": "force_field_wind_drift", "wind_vector_m_s": expected_physics.get("wind_vector_m_s")}
    if capability_id == "mass_ratio_momentum_transfer":
        return {"type": "mass_ratio_momentum_transfer", "collision_graph": expected_physics.get("collision_graph", []), "expected_velocity_order": expected_physics.get("expected_velocity_order")}
    if capability_id == "angular_damping_spin_decay":
        return {"type": "angular_damping_spin_decay", "spin_axis": expected_physics.get("spin_axis", "z")}
    if capability_id == "agent_rigidbody_action_coupling":
        return {
            "type": "agent_rigidbody_action_coupling",
            "action_type": expected_physics.get("action_type"),
            "actor_id": expected_physics.get("action_actor_id"),
            "target_id": expected_physics.get("target_object_id"),
            "action_frame": expected_physics.get("action_frame"),
        }
    if capability_id == "constraint_distance_pendulum_motion":
        return {
            "type": "distance_constraint_motion",
            "anchor_object_id": expected_physics.get("anchor_object_id"),
            "constrained_object_id": expected_physics.get("constrained_object_id"),
            "constraint_length_m": expected_physics.get("constraint_length_m"),
        }
    if capability_id == "constraint_momentum_transfer":
        return {
            "type": "constrained_impulse_chain_transfer",
            "chain_objects": expected_physics.get("chain_objects", []),
            "receiver_object_id": expected_physics.get("receiver_object_id"),
            "expected_contact_chain": expected_physics.get("expected_contact_chain", []),
        }
    if capability_id == "elastic_energy_launch":
        return {
            "type": "elastic_energy_release",
            "launcher_object_id": expected_physics.get("launcher_object_id"),
            "launched_object_id": expected_physics.get("launched_object_id"),
            "release_frame": expected_physics.get("release_frame"),
            "stored_energy_j": expected_physics.get("stored_energy_j"),
        }
    if capability_id == "elastic_constraint_rebound":
        return {
            "type": "elastic_constraint_rebound",
            "anchor_object_id": expected_physics.get("anchor_object_id"),
            "constrained_object_id": expected_physics.get("constrained_object_id"),
            "rest_length_m": expected_physics.get("rest_length_m"),
            "max_extension_m": expected_physics.get("max_extension_m"),
        }
    return {"type": "trajectory_event"}


def required_signals_for(capability_id: str) -> list[str]:
    if capability_id in {"rigid_body_contact_causality", "billiard_causality_compiler"}:
        return ["trajectory", "contact_events", "camera_trajectory"]
    if capability_id == "sequential_contact_propagation":
        return ["trajectory", "contact_events", "rotation"]
    if capability_id == "rigid_body_gravity_collision":
        return ["trajectory", "contact_events", "gravity_label"]
    if capability_id == "ramp_sliding_friction":
        return ["trajectory", "contact_events", "ramp_angle_label", "material_friction_label"]
    if capability_id == "projectile_gravity_motion":
        return ["trajectory", "contact_events", "gravity_label", "initial_velocity"]
    if capability_id == "bounce_restitution_ball":
        return ["trajectory", "contact_events", "gravity_label", "material_restitution_label"]
    if capability_id == "rolling_friction_ball":
        return ["trajectory", "contact_events", "initial_velocity", "material_friction_label"]
    if capability_id == "sliding_crate_friction":
        return ["trajectory", "contact_events", "initial_velocity", "material_friction_label", "applied_force_label"]
    if capability_id == "force_field_wind_drift":
        return ["trajectory", "wind_vector_label", "force_field_label"]
    if capability_id == "mass_ratio_momentum_transfer":
        return ["trajectory", "contact_events", "mass_labels", "post_collision_velocity"]
    if capability_id == "angular_damping_spin_decay":
        return ["trajectory", "rotation_trace", "angular_velocity", "angular_damping_label"]
    if capability_id == "agent_rigidbody_action_coupling":
        return ["trajectory", "action_trace", "contact_events", "object_roles", "post_action_velocity"]
    if capability_id == "constraint_distance_pendulum_motion":
        return ["trajectory", "constraint_trace", "constraint_parameter_labels", "object_roles"]
    if capability_id == "constraint_momentum_transfer":
        return ["trajectory", "contact_events", "constraint_trace", "mass_labels", "post_chain_velocity"]
    if capability_id == "elastic_energy_launch":
        return ["trajectory", "spring_events", "energy_labels", "post_release_velocity"]
    if capability_id == "elastic_constraint_rebound":
        return ["trajectory", "constraint_trace", "elastic_constraint_labels", "post_stretch_velocity"]
    return ["trajectory"]


def expected_failure_for(negative_mode: str | None) -> str:
    if negative_mode == "precontact_motion":
        return "F5_passive_precontact_motion"
    if negative_mode in {"missing_contact"}:
        return "F2_missing_contact_events"
    if negative_mode in {"missing_wind_label"}:
        return "F3_invalid_initial_physics_state"
    if negative_mode in {"missing_mass_label"}:
        return "F3_invalid_initial_physics_state"
    if negative_mode in {"missing_angular_velocity_label"}:
        return "F3_invalid_initial_physics_state"
    if negative_mode in {"missing_action_trace"}:
        return "F7_runtime_artifact_incomplete"
    if negative_mode in {"missing_constraint_label"}:
        return "F3_invalid_initial_physics_state"
    if negative_mode in {"preaction_motion"}:
        return "F5_passive_precontact_motion"
    if negative_mode in {"passive_prechain_motion"}:
        return "F5_passive_precontact_motion"
    if negative_mode in {"missing_release_event"}:
        return "F7_runtime_artifact_incomplete"
    if negative_mode in {"missing_constraint_trace"}:
        return "F7_runtime_artifact_incomplete"
    return "F4_causality_violation"


if __name__ == "__main__":
    raise SystemExit(main())
