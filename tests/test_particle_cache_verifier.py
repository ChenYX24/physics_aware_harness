from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.verification.particle_cache_verifier import verify_particle_cache


class ParticleCacheVerifierTests(unittest.TestCase):
    def test_valid_particle_and_surface_cache_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "frame.obj").write_text("v 0 0 0\nf 1 1 1\n", encoding="utf-8")
            cache = particle_cache()
            report = verify_particle_cache(cache, root=root)

        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["checks"]["stable_particle_count"])
        self.assertTrue(report["checks"]["container_bounds_respected"])
        self.assertTrue(report["checks"]["surface_topology_consistent"])
        self.assertTrue(report["checks"]["surface_container_bounds_respected"])
        self.assertTrue(report["checks"]["surface_rigid_intersections_absent"])

    def test_particle_loss_and_missing_surface_fail(self) -> None:
        cache = particle_cache()
        cache["frames"][1]["positions_m"].pop()

        report = verify_particle_cache(cache, root="/missing")

        self.assertEqual(report["status"], "fail")
        self.assertIn("particle_count_changed", report["failure_codes"])
        self.assertIn("surface_mesh_missing", report["failure_codes"])

    def test_container_penetration_fails(self) -> None:
        cache = particle_cache()
        cache["frames"][1]["positions_m"][0] = [0.0, 0.0, -0.2]

        report = verify_particle_cache(cache)

        self.assertEqual(report["status"], "fail")
        self.assertIn("container_penetration", report["failure_codes"])

    def test_invalid_surface_topology_fails(self) -> None:
        cache = particle_cache()
        cache["frames"][1]["surface"] = {**cache["frames"][1]["surface"], "topology_consistent": False, "topology_issue": "open edge"}

        report = verify_particle_cache(cache)

        self.assertIn("surface_topology_invalid", report["failure_codes"])

    def test_reconstructed_surface_outside_basin_fails(self) -> None:
        cache = particle_cache()
        cache["frames"][1]["surface"] = {
            **cache["frames"][1]["surface"],
            "bounds_m": {"min_m": [-0.31, -0.2, 0.0], "max_m": [0.2, 0.2, 1.0]},
        }

        report = verify_particle_cache(cache)

        self.assertEqual(report["status"], "fail")
        self.assertIn("surface_container_penetration", report["failure_codes"])

    def test_reconstructed_surface_inside_rigid_body_fails(self) -> None:
        cache = particle_cache()
        cache["frames"][1]["surface"] = {
            **cache["frames"][1]["surface"],
            "rigid_intersection_vertex_count": 4,
        }

        report = verify_particle_cache(cache)

        self.assertEqual(report["status"], "fail")
        self.assertIn("surface_rigid_intersection", report["failure_codes"])

    def test_container_bounds_follow_declared_basin_center(self) -> None:
        cache = particle_cache()
        cache["environment"]["center_xy_m"] = [1.0, -2.0]
        for frame in cache["frames"]:
            frame["positions_m"] = [[row[0] + 1.0, row[1] - 2.0, row[2]] for row in frame["positions_m"]]
            frame["surface"] = {
                **frame["surface"],
                "bounds_m": {"min_m": [1.0, -2.0, 0.0], "max_m": [1.1, -1.9, 1.0]},
            }

        report = verify_particle_cache(cache)

        self.assertEqual(report["status"], "pass", report)

    def test_buoyant_and_dense_bodies_require_separation_and_splash(self) -> None:
        cache = particle_cache()
        cache["environment"].update({
            "initial_condition": {"type": "container_fill"},
            "initial_liquid_surface_z_m": 0.9,
            "minimum_splash_rise_m": 0.05,
            "minimum_float_sink_separation_m": 0.04,
            "maximum_initial_surface_outlier_m": 0.11,
            "rigid_objects": [
                {"id": "rubber", "radius_m": 0.05, "expected_response": "float"},
                {"id": "lead", "radius_m": 0.05, "expected_response": "sink"},
            ],
        })
        cache["frames"][0]["rigid_objects"] = {"rubber": {"position_m": [0, 0, 1]}, "lead": {"position_m": [0, 0, 1]}}
        cache["frames"][1]["rigid_objects"] = {"rubber": {"position_m": [0, 0, 0.12]}, "lead": {"position_m": [0, 0, 0.05]}}
        cache["frames"][1]["positions_m"][0][2] = 0.96

        report = verify_particle_cache(cache)

        self.assertEqual(report["status"], "pass", report)
        self.assertGreaterEqual(report["checks"]["float_sink_separation_m"], 0.04)
        self.assertGreaterEqual(report["checks"]["splash_rise_m"], 0.05)
        self.assertEqual(report["checks"]["splash_measurement_start_frame"], 1)

    def test_preimpact_residual_droplet_does_not_inflate_splash(self) -> None:
        cache = particle_cache()
        cache["environment"].update({
            "initial_condition": {"type": "container_fill"},
            "initial_liquid_surface_z_m": 0.2,
            "minimum_splash_rise_m": 0.05,
            "minimum_float_sink_separation_m": 0.04,
            "maximum_initial_surface_outlier_m": 1.0,
            "rigid_objects": [
                {"id": "rubber", "radius_m": 0.05, "expected_response": "float"},
                {"id": "lead", "radius_m": 0.05, "expected_response": "sink"},
            ],
        })
        cache["frames"][0]["positions_m"] = [[0, 0, 1.0], [0.1, 0, 0.2]]
        cache["frames"][0]["rigid_objects"] = {"rubber": {"position_m": [0, 0, 1]}, "lead": {"position_m": [0, 0, 1]}}
        cache["frames"][1]["positions_m"] = [[0, 0, 0.24], [0.1, 0, 0.26]]
        cache["frames"][1]["rigid_objects"] = {"rubber": {"position_m": [0, 0, 0.12]}, "lead": {"position_m": [0, 0, 0.05]}}

        report = verify_particle_cache(cache)

        self.assertEqual(report["status"], "pass", report)
        self.assertAlmostEqual(report["checks"]["splash_rise_m"], 0.06)

    def test_unsettled_container_fill_is_rejected_before_render(self) -> None:
        cache = particle_cache()
        cache["environment"].update({
            "initial_condition": {"type": "container_fill"},
            "initial_liquid_surface_z_m": 0.2,
            "maximum_initial_surface_outlier_m": 0.08,
        })
        cache["frames"][0]["positions_m"] = [[0, 0, 0.2], [0.1, 0, 0.6]]

        report = verify_particle_cache(cache)

        self.assertEqual(report["status"], "fail")
        self.assertIn("initial_surface_not_settled", report["failure_codes"])

    def test_uniform_initial_flow_requires_speed_direction_and_displacement(self) -> None:
        cache = particle_cache()
        cache["environment"].update({
            "initial_condition": {
                "type": "container_fill",
                "shape": "box",
                "velocity_field": {"type": "uniform", "velocity_m_s": [0.5, 0.0, 0.0]},
            },
            "initial_liquid_surface_z_m": 1.0,
            "maximum_initial_surface_outlier_m": 0.1,
            "minimum_initial_flow_speed_m_s": 0.4,
            "minimum_horizontal_displacement_m": 0.05,
        })
        cache["frames"][0]["velocities_m_s"] = [[0.5, 0.0, 0.0], [0.5, 0.0, 0.0]]
        cache["frames"][1]["positions_m"] = [[0.1, 0.0, 0.9], [0.2, 0.0, 0.9]]

        report = verify_particle_cache(cache)

        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(report["checks"]["initial_flow_type"], "uniform")
        self.assertGreaterEqual(report["checks"]["horizontal_displacement_m"], 0.05)

    def test_fragmented_final_surface_is_rejected(self) -> None:
        cache = particle_cache()
        cache["environment"]["minimum_final_surface_component_fraction"] = 0.8
        cache["frames"][-1]["surface"] = {
            **cache["frames"][-1]["surface"],
            "connected_component_count": 12,
            "largest_component_triangle_fraction": 0.3,
        }

        report = verify_particle_cache(cache)

        self.assertEqual(report["status"], "fail")
        self.assertIn("final_surface_too_fragmented", report["failure_codes"])

    def test_declared_measurements_and_assertions_pass(self) -> None:
        cache = particle_cache()
        cache["environment"] = declared_environment([
            assertion("initial_level", "level", "initial", ">=", 0.9),
            assertion("final_level", "level", "final", "<=", 0.1),
            assertion("level_change", "level", "initial_minus_final", ">=", 0.8),
            assertion("minimum_width", "width", "max", ">=", 0.25),
        ])
        cache["frames"][0]["measurements"] = {"level": 1.0, "width": 0.05}
        cache["frames"][1]["measurements"] = {"level": 0.05, "width": 0.28}

        report = verify_particle_cache(cache)

        self.assertEqual(report["status"], "pass", report)
        self.assertTrue(report["checks"]["declared_measurements_checked"])
        self.assertAlmostEqual(report["checks"]["measurement_reductions"]["level_change"], 0.95)

    def test_generic_assertion_failure_has_no_scenario_specific_code(self) -> None:
        cache = particle_cache()
        cache["environment"] = declared_environment([
            assertion("required_width", "width", "final", ">=", 0.2),
        ])
        cache["frames"][0]["measurements"] = {"level": 1.0, "width": 0.05}
        cache["frames"][1]["measurements"] = {"level": 0.0, "width": 0.08}

        report = verify_particle_cache(cache)

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_codes"], ["solver_assertion_failed"])
        self.assertEqual(report["checks"]["assertion_results"][0]["id"], "required_width")

    def test_missing_declared_measurement_fails(self) -> None:
        cache = particle_cache()
        cache["environment"] = declared_environment([
            assertion("required_width", "width", "final", ">=", 0.2),
        ])
        cache["frames"][0]["measurements"] = {"level": 1.0}
        cache["frames"][1]["measurements"] = {"level": 0.0}

        report = verify_particle_cache(cache)

        self.assertIn("declared_measurement_missing", report["failure_codes"])
        self.assertIn("solver_assertion_failed", report["failure_codes"])

def assertion(assertion_id: str, measurement_id: str, reduction: str, operator: str, value: float) -> dict:
    return {
        "id": assertion_id,
        "measurement_id": measurement_id,
        "reduction": reduction,
        "operator": operator,
        "value": value,
    }


def declared_environment(assertions: list[dict]) -> dict:
    return {
        "type": "rigid_sph_scene",
        "workspace_bounds_m": {"min_m": [-1.0, -1.0, -0.1], "max_m": [1.0, 1.0, 2.0]},
        "penetration_tolerance_m": 0.01,
        "measurements": [{"id": "level"}, {"id": "width"}],
        "assertions": assertions,
    }


def particle_cache() -> dict:
    surface = {
        "path": "frame.obj",
        "vertex_count": 3,
        "triangle_count": 1,
        "bounds_m": {"min_m": [0.0, 0.0, 0.0], "max_m": [0.1, 0.1, 1.0]},
        "rigid_intersection_vertex_count": 0,
    }
    return {
        "schema_version": "harness_particle_cache_v1",
        "solver": {"gravity_m_s2": [0, 0, -9.81]},
        "particles": {"count": 2, "stable_ids": [0, 1]},
        "environment": {"type": "five_plane_basin", "floor_z_m": 0.0, "wall_half_extent_m": 0.3, "penetration_tolerance_m": 0.01},
        "frames": [
            {"frame": 0, "time_s": 0.0, "positions_m": [[0, 0, 1], [0.1, 0, 1]], "velocities_m_s": [[0, 0, 0], [0, 0, 0]], "surface": surface},
            {"frame": 1, "time_s": 0.1, "positions_m": [[0, 0, 0.9], [0.1, 0, 0.9]], "velocities_m_s": [[0, 0, -1], [0, 0, -1]], "surface": surface},
        ],
    }


if __name__ == "__main__":
    unittest.main()
