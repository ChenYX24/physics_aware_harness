from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import numpy as np

from harness.core.case_spec import load_case_spec
from harness.verification.deformable_mesh_cache_verifier import (
    verify_deformable_mesh_cache,
    verify_deformable_solid_impact,
    verify_stiffness_response_matrix,
    verify_wind_speed_response_matrix,
)
from scripts.harness_genesis_fem import finite_float, surface_edges, vector3


ROOT = Path(__file__).resolve().parents[1]


class DeformableMeshCacheVerifierTests(unittest.TestCase):
    def test_cloth_case_and_minimal_cache_pass(self) -> None:
        case = load_case_spec(ROOT / "cases/soft_body/cloth_drape/v001_taichi_cloth_over_sphere.json")
        self.assertEqual(case.capability_id, "soft_body_deformation")
        arrays = cache()
        report = verify_deformable_mesh_cache(
            arrays,
            expected={
                "minimum_mean_downward_displacement_m": 0.4,
                "minimum_sphere_contact_frames": 1,
                "maximum_structural_edge_stretch_ratio": 1.01,
                "penetration_tolerance_m": 0.002,
            },
            colliders=[sphere_collider()],
            floor_z_m=0.0,
            collision_thickness_m=0.025,
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["checks"]["sphere_contact_frame_count"], 1)

    def test_penetrating_vertex_fails(self) -> None:
        arrays = copy.deepcopy(cache())
        arrays["positions_m"][1, 0] = [0.0, 0.0, 0.2]
        report = verify_deformable_mesh_cache(
            arrays,
            expected={"penetration_tolerance_m": 0.002},
            colliders=[sphere_collider()],
            floor_z_m=0.0,
            collision_thickness_m=0.025,
        )
        self.assertIn("rigid_collider_penetration", report["failure_codes"])

    def test_invalid_topology_fails_closed_without_numpy_error(self) -> None:
        arrays = copy.deepcopy(cache())
        arrays["structural_edges"] = np.asarray([[0, 99]], dtype=np.int32)
        report = verify_deformable_mesh_cache(
            arrays,
            expected={},
            colliders=[sphere_collider()],
            floor_z_m=0.0,
            collision_thickness_m=0.025,
        )
        self.assertEqual(report["status"], "fail")
        self.assertIn("structural_edges_invalid", report["failure_codes"])

    def test_pinned_flag_reports_constraint_and_wind_response(self) -> None:
        initial = np.asarray([
            [0.0, 0.0, 1.0],
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 1.0],
            [1.0, 0.0, 0.0],
        ], dtype=np.float32)
        final = initial.copy()
        final[2:, 1] = 0.5
        arrays = {
            "positions_m": np.stack([initial, final]),
            "velocities_m_s": np.zeros((2, 4, 3), dtype=np.float32),
            "faces": np.asarray([[0, 2, 3], [0, 3, 1]], dtype=np.int32),
            "structural_edges": np.asarray([[0, 1], [0, 2], [1, 3], [2, 3]], dtype=np.int32),
            "times_s": np.asarray([0.0, 1.0 / 24.0]),
        }
        report = verify_deformable_mesh_cache(
            arrays,
            expected={
                "maximum_pinned_vertex_displacement_m": 0.0001,
                "minimum_mean_wind_axis_displacement_m": 0.2,
                "minimum_final_vertical_span_m": 0.8,
            },
            colliders=[],
            floor_z_m=None,
            collision_thickness_m=0.0,
            pinned_indices=[0, 1],
            wind_axis=[0.0, 1.0, 0.0],
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["checks"]["maximum_pinned_vertex_displacement_m"], 0.0)
        self.assertEqual(report["checks"]["maximum_mean_wind_axis_displacement_m"], 0.25)

    def test_wind_speed_matrix_requires_monotonic_response(self) -> None:
        report = verify_wind_speed_response_matrix([
            row(3.0, 0.63),
            row(5.5, 0.72),
            row(6.5, 0.74),
        ])
        self.assertEqual(report["status"], "pass")

        failed = verify_wind_speed_response_matrix([
            row(3.0, 0.63),
            row(5.5, 0.72),
            row(6.5, 0.70),
        ])
        self.assertIn("wind_response_not_monotonic", failed["failure_codes"])

    def test_soft_solid_impact_reports_compression_volume_and_rebound(self) -> None:
        arrays = solid_impact_cache()
        report = verify_deformable_solid_impact(
            arrays,
            expected={
                "minimum_compression_ratio": 0.25,
                "maximum_relative_volume_error": 0.8,
                "minimum_floor_contact_frames": 1,
                "minimum_rebound_center_height_m": 0.2,
                "penetration_tolerance_m": 0.005,
            },
            floor_z_m=0.0,
        )
        self.assertEqual(report["status"], "pass")
        self.assertGreater(report["checks"]["maximum_compression_ratio"], 0.25)

        collapsed = copy.deepcopy(arrays)
        collapsed["positions_m"][1, :, 2] = 0.0
        failed = verify_deformable_solid_impact(
            collapsed,
            expected={"maximum_relative_volume_error": 0.8, "penetration_tolerance_m": 0.005},
            floor_z_m=0.0,
        )
        self.assertIn("solid_volume_error_exceeded", failed["failure_codes"])

    def test_stiffness_matrix_requires_compression_to_decrease(self) -> None:
        report = verify_stiffness_response_matrix([
            stiffness_row(50000, 0.42),
            stiffness_row(100000, 0.31),
            stiffness_row(200000, 0.22),
        ])
        self.assertEqual(report["status"], "pass")

        failed = verify_stiffness_response_matrix([
            stiffness_row(50000, 0.42),
            stiffness_row(100000, 0.31),
            stiffness_row(200000, 0.35),
        ])
        self.assertIn("compression_response_not_monotonic", failed["failure_codes"])

    def test_genesis_fem_stiffness_cases_are_one_factor_at_a_time(self) -> None:
        paths = sorted((ROOT / "cases/soft_body/elastic_collision/v001_youngs_modulus_ofat").glob("*.json"))
        cases = [json.loads(path.read_text()) for path in paths]
        self.assertEqual(
            [case["objects"][0]["material"]["youngs_modulus_pa"] for case in cases],
            [200000, 50000, 100000],
        )
        normalized = []
        for case in cases:
            clone = copy.deepcopy(case)
            clone["case_id"] = "normalized"
            clone["objects"][0]["material"]["youngs_modulus_pa"] = 100000
            clone["sweep_metadata"]["value"] = 100000
            clone["notes"] = "normalized"
            normalized.append(clone)
        self.assertEqual(normalized[0], normalized[1])
        self.assertEqual(normalized[1], normalized[2])

    def test_surface_edges_are_unique(self) -> None:
        edges = surface_edges(np.asarray([[0, 1, 2], [2, 1, 3]], dtype=np.int32))
        self.assertEqual(edges.tolist(), [[0, 1], [0, 2], [1, 2], [1, 3], [2, 3]])

    def test_genesis_fem_inputs_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            finite_float(float("nan"), "radius_m", minimum=0.0)
        with self.assertRaises(ValueError):
            vector3([0.0, 1.0], "initial_center_m")


def cache() -> dict[str, np.ndarray]:
    initial = np.asarray([
        [-0.1, -0.1, 1.0],
        [0.1, -0.1, 1.0],
        [-0.1, 0.1, 1.0],
        [0.1, 0.1, 1.0],
    ], dtype=np.float32)
    final = initial.copy()
    final[:, 2] = 0.5
    return {
        "positions_m": np.stack([initial, final]),
        "velocities_m_s": np.zeros((2, 4, 3), dtype=np.float32),
        "faces": np.asarray([[0, 2, 3], [0, 3, 1]], dtype=np.int32),
        "structural_edges": np.asarray([[0, 1], [0, 2], [1, 3], [2, 3]], dtype=np.int32),
        "times_s": np.asarray([0.0, 1.0 / 24.0]),
    }


def sphere_collider() -> dict[str, object]:
    return {
        "id": "sphere",
        "shape": "sphere",
        "size_m": [1.0, 1.0, 1.0],
        "initial_position_m": [0.0, 0.0, 0.0],
        "world_from_local_columns": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        "kinematic_motion": None,
    }


def row(speed: float, displacement: float) -> dict[str, object]:
    return {
        "wind_speed_m_s": speed,
        "verifier_status": "pass",
        "maximum_mean_wind_axis_displacement_m": displacement,
    }


def stiffness_row(youngs_modulus_pa: float, compression: float) -> dict[str, object]:
    return {
        "youngs_modulus_pa": youngs_modulus_pa,
        "verifier_status": "pass",
        "maximum_compression_ratio": compression,
    }


def solid_impact_cache() -> dict[str, np.ndarray]:
    initial = np.asarray([
        [-0.1, -0.1, 0.4],
        [0.1, -0.1, 0.4],
        [0.0, 0.1, 0.4],
        [0.0, 0.0, 0.6],
    ], dtype=np.float32)
    contact = initial.copy()
    contact[:, 2] = [0.0, 0.0, 0.0, 0.12]
    rebound = initial.copy()
    rebound[:, 2] += 0.1
    return {
        "positions_m": np.stack([initial, contact, rebound]),
        "velocities_m_s": np.zeros((3, 4, 3), dtype=np.float32),
        "faces": np.asarray([[0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3]], dtype=np.int32),
        "structural_edges": np.asarray([[0, 1], [1, 2], [2, 0], [0, 3], [1, 3], [2, 3]], dtype=np.int32),
        "tetrahedra": np.asarray([[0, 1, 2, 3]], dtype=np.int32),
        "times_s": np.asarray([0.0, 0.1, 0.2]),
    }


if __name__ == "__main__":
    unittest.main()
