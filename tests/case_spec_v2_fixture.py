from __future__ import annotations

from copy import deepcopy
from typing import Any


def case_spec_v2_fixture() -> dict[str, Any]:
    return deepcopy(
        {
            "schema_version": "harness_case_spec_v2",
            "identity": {
                "case_id": "v2_ball_contact",
                "title": "One ball contacts another",
                "source_request": "Make one ball hit another ball on a floor.",
            },
            "capabilities": {
                "primary": "rigid_body_contact_causality",
                "required": ["rigid_body_contact_causality"],
            },
            "scene": {
                "environment_intent": "minimal flat test floor",
                "coordinate_system": "z_up",
                "duration_s": 2.0,
                "bounds_hint_m": [3.0, 2.0, 1.0],
            },
            "timebase": {
                "physics_hz": 120,
                "observation_fps": 24,
                "deterministic_seed": 17,
            },
            "backend_constraints": {
                "required_solver_capabilities": ["rigid_body", "contact_events"],
                "allowed_solvers": [],
                "render_backend": None,
                "allow_multi_backend": True,
            },
            "asset_policy": {
                "allow_local": True,
                "allow_external": True,
                "allow_generation": True,
                "allow_analytic_proxy": True,
                "required_license_tier": "local_preview",
            },
            "objects": [
                {
                    "id": "cue_ball",
                    "role": "active_striker",
                    "geometry": {"shape_hint": "sphere", "approx_size_m": [0.18, 0.18, 0.18]},
                    "physics": {
                        "body_type": "dynamic",
                        "mass_kg": 0.17,
                        "collision_required": True,
                        "material": {"dynamic_friction": 0.035, "restitution": 0.86},
                    },
                    "initial_state": {
                        "position_m": [-0.8, 0.0, 0.09],
                        "rotation_deg": [0.0, 0.0, 0.0],
                        "linear_velocity_m_s": [1.0, 0.0, 0.0],
                    },
                    "behavior": {},
                },
                {
                    "id": "target_ball",
                    "role": "passive_target",
                    "geometry": {"shape_hint": "sphere", "approx_size_m": [0.18, 0.18, 0.18]},
                    "physics": {
                        "body_type": "dynamic",
                        "mass_kg": 0.17,
                        "collision_required": True,
                        "material": {"dynamic_friction": 0.035, "restitution": 0.86},
                    },
                    "initial_state": {
                        "position_m": [0.0, 0.0, 0.09],
                        "rotation_deg": [0.0, 0.0, 0.0],
                        "linear_velocity_m_s": [0.0, 0.0, 0.0],
                    },
                    "behavior": {},
                },
                {
                    "id": "floor",
                    "role": "support",
                    "geometry": {"shape_hint": "box", "approx_size_m": [3.0, 2.0, 0.1]},
                    "physics": {
                        "body_type": "static",
                        "mass_kg": 100.0,
                        "collision_required": True,
                        "material": {"dynamic_friction": 0.04, "restitution": 0.15},
                    },
                    "initial_state": {
                        "position_m": [0.0, 0.0, 0.0],
                        "rotation_deg": [0.0, 0.0, 0.0],
                        "linear_velocity_m_s": [0.0, 0.0, 0.0],
                    },
                    "behavior": {},
                },
            ],
            "relations": [{"type": "collision", "source": "cue_ball", "target": "target_ball"}],
            "events": [{"type": "initial_motion", "object": "cue_ball"}],
            "expected_behavior": {
                "passive_stationary_until_contact": True,
                "contact_required": True,
            },
            "observation_requirements": {
                "cameras": [{"role": "front_static", "target_objects": ["cue_ball", "target_ball"]}],
                "modalities": ["rgb"],
                "signals": ["trajectory", "contact_events"],
            },
            "verification_requirements": {
                "assertions": [{"type": "contact_occurs", "objects": ["cue_ball", "target_ball"]}],
                "thresholds": {},
            },
            "variant": {"should_pass": True},
            "provenance": {},
            "notes": "V2 contract fixture.",
        }
    )
