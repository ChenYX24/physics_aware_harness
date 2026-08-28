from __future__ import annotations

import copy
import math
from typing import Any, Mapping


RIGID_SPH_QUALIFICATION_POLICY_ID = "genesis_wcsph_surface_v2"

_CURRENT_POLICY = {
    "particle_size_m": 0.005,
    "fps": 24,
    "steps_per_frame": 100,
    "pre_roll_s": 0.25,
    "sph_material": {
        "pressure_solver": "WCSPH",
        "rest_density_kg_m3": 1000.0,
        "stiffness_pa": 50000.0,
        "equation_of_state_exponent": 7.0,
        "viscosity_pa_s": 0.005,
        "surface_tension_n_m": 0.01,
    },
    "surface_reconstruction": {
        "smoothing_length_in_particle_radii": 2.5,
        "cube_size_in_particle_radii": 1.0,
        "iso_surface_threshold": 0.35,
    },
    "verification": {
        "particle_count_relative_tolerance": 0.0,
        "penetration_tolerance_m": 0.005,
        "surface_volume_relative_error_max": 0.35,
        "measurement_absolute_tolerance": 1e-6,
    },
}


def compile_rigid_sph_solver_configuration(case_spec: Mapping[str, Any]) -> dict[str, Any]:
    scene = case_spec.get("scene") if isinstance(case_spec.get("scene"), Mapping) else {}
    duration_s = float(scene.get("duration_s") or 0.0)
    if not math.isfinite(duration_s) or duration_s <= 0.0:
        raise ValueError("rigid_sph solver configuration requires positive scene.duration_s")
    parameters = copy.deepcopy(_CURRENT_POLICY)
    parameters["duration_s"] = duration_s
    parameters["solver_dt_s"] = 1.0 / (parameters["fps"] * parameters["steps_per_frame"])
    return {
        "schema_version": "harness_rigid_sph_solver_configuration_v1",
        "qualification_policy_id": RIGID_SPH_QUALIFICATION_POLICY_ID,
        "backend": "genesis_sph",
        "handoff_contract_id": "particle_surface_cache_v1",
        "parameters": parameters,
    }


def rigid_sph_parameters(configuration: Mapping[str, Any]) -> dict[str, Any]:
    if configuration.get("schema_version") != "harness_rigid_sph_solver_configuration_v1":
        raise ValueError("unsupported rigid_sph solver configuration schema")
    if configuration.get("qualification_policy_id") != RIGID_SPH_QUALIFICATION_POLICY_ID:
        raise ValueError("rigid_sph solver configuration is not the current qualification policy")
    parameters = configuration.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ValueError("rigid_sph solver configuration has no parameters")
    return copy.deepcopy(dict(parameters))
