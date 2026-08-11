from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.case_spec import load_case_spec
from scripts.harness_taichi_cloth import object_with_role, write_run


class _SimulationOnlyRasterizer:
    """Keep Genesis FEM usable on macOS sessions without an attached display."""

    def build(self) -> None:
        pass

    def destroy(self) -> None:
        pass


def finite_float(value: Any, name: str, *, minimum: float | None = None) -> float:
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return result


def vector3(value: Any, name: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} must contain exactly three finite numbers")
    x, y, z = (finite_float(component, name) for component in value)
    return x, y, z


def simulate(case_data: dict[str, Any]) -> dict[str, Any]:
    import genesis as gs

    solid = object_with_role(case_data, "deformable_solid")
    floor = object_with_role(case_data, "support_surface")
    material = solid.get("material") or {}
    options = case_data.get("backend_options") or {}
    fps = int(options.get("fps") or 24)
    duration_s = float(options.get("duration_s") or 2.5)
    solver_hz = int(options.get("solver_hz") or 240)
    if fps <= 0 or duration_s <= 0.0 or solver_hz <= 0 or solver_hz % fps:
        raise ValueError("Genesis FEM requires positive fps/duration and solver_hz divisible by fps")
    radius = finite_float(solid.get("radius_m") or 0.18, "radius_m", minimum=1e-6)
    center = vector3(solid.get("initial_center_m") or [0.0, 0.0, 0.7], "initial_center_m")
    initial_velocity = vector3(solid.get("initial_velocity_m_s") or [0.0, 0.0, 0.0], "initial_velocity_m_s")
    floor_z = finite_float(floor.get("z_m") or 0.0, "floor_z_m")
    youngs_modulus = finite_float(material["youngs_modulus_pa"], "youngs_modulus_pa", minimum=1e-6)
    poisson_ratio = finite_float(material.get("poisson_ratio") or 0.43, "poisson_ratio")
    density = finite_float(material.get("density_kg_m3") or 900.0, "density_kg_m3", minimum=1e-6)
    if not -1.0 < poisson_ratio < 0.5:
        raise ValueError("poisson_ratio must be between -1 and 0.5")
    dt = 1.0 / solver_hz
    steps_per_frame = solver_hz // fps

    gs.init(backend=gs.cpu, precision="64", logging_level="warning")
    scene = gs.Scene(
        sim_options=gs.options.SimOptions(dt=dt, substeps=1, gravity=(0.0, 0.0, -9.81)),
        fem_options=gs.options.FEMOptions(
            dt=dt,
            gravity=(0.0, 0.0, -9.81),
            floor_height=floor_z,
            use_implicit_solver=True,
            n_newton_iterations=int(options.get("newton_iterations") or 3),
            n_pcg_iterations=int(options.get("pcg_iterations") or 120),
            damping_alpha=float(options.get("damping_alpha") or 0.03),
            damping_beta=float(options.get("damping_beta") or 0.0002),
        ),
        coupler_options=gs.options.SAPCouplerOptions(
            fem_floor_contact_type="tet",
            enable_fem_self_tet_contact=False,
            n_sap_iterations=int(options.get("sap_iterations") or 8),
            n_pcg_iterations=int(options.get("sap_pcg_iterations") or 120),
        ),
        show_viewer=False,
    )
    # Genesis 1.2.1 still creates an OpenGL rasterizer with show_viewer=False.
    # This backend exports solver state only; UE owns all rendering and sensors.
    scene._visualizer._rasterizer = _SimulationOnlyRasterizer()
    scene._visualizer._renderer = None
    entity = scene.add_entity(
        material=gs.materials.FEM.Elastic(
            E=youngs_modulus,
            nu=poisson_ratio,
            rho=density,
            friction_mu=float(material.get("friction") or 0.25),
            hydroelastic_modulus=float(material.get("hydroelastic_modulus_pa") or 2.0e6),
            model="linear_corotated",
        ),
        morph=gs.morphs.Sphere(pos=center, radius=radius),
    )
    scene.build()
    if any(initial_velocity):
        entity.set_velocity(initial_velocity)

    frame_count = int(round(duration_s * fps)) + 1
    positions = np.empty((frame_count, entity.n_vertices, 3), dtype=np.float64)
    velocities = np.empty_like(positions)
    for frame in range(frame_count):
        state = entity.get_state()
        positions[frame] = state.pos[0].numpy()
        velocities[frame] = state.vel[0].numpy()
        if frame + 1 < frame_count:
            for _ in range(steps_per_frame):
                scene.step()

    faces = np.asarray(entity.surface_triangles, dtype=np.int32)
    tetrahedra = np.asarray(entity.elems, dtype=np.int32)
    return {
        "positions_m": positions,
        "velocities_m_s": velocities,
        "faces": faces,
        "structural_edges": surface_edges(faces),
        "tetrahedra": tetrahedra,
        "pinned_indices": np.asarray([], dtype=np.int32),
        "times_s": np.arange(frame_count, dtype=np.float64) / fps,
        "parameters": {
            "backend": "genesis_fem_cpu",
            "genesis_version": str(gs.__version__),
            "constitutive_model": "linear_corotated",
            "contact_solver": "sap_tetrahedral_floor",
            "precision": "float64",
            "vertex_count": int(entity.n_vertices),
            "tetrahedron_count": int(entity.n_elements),
            "fps": fps,
            "duration_s": duration_s,
            "substeps_per_frame": steps_per_frame,
            "solver_dt_s": dt,
            "collision_thickness_m": 0.0,
            "pinned_boundary": "none",
            "floor_z_m": floor_z,
            "youngs_modulus_pa": youngs_modulus,
            "poisson_ratio": poisson_ratio,
            "density_kg_m3": density,
        },
    }


def surface_edges(faces: np.ndarray) -> np.ndarray:
    edges = {
        tuple(sorted((int(face[index]), int(face[(index + 1) % 3]))))
        for face in faces
        for index in range(3)
    }
    return np.asarray(sorted(edges), dtype=np.int32)


def main() -> int:
    parser = argparse.ArgumentParser(description="Solve one deformable solid impact with Genesis FEM.")
    parser.add_argument("--case", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    case = load_case_spec(args.case)
    from harness.core.physics_contract import infer_scene_domain

    if infer_scene_domain(case.data) != "deformable":
        raise SystemExit("Genesis FEM backend requires a deformable-domain scene contract")
    result = simulate(case.data)
    verification = write_run(Path(args.output_dir).expanduser().resolve(), case.data, result)
    print(json.dumps(verification, indent=2, ensure_ascii=False))
    return 0 if verification["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
