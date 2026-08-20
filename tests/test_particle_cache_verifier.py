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

    def test_legacy_environment_contract_is_rejected(self) -> None:
        cache = particle_cache()
        cache["environment"] = {"type": "five_plane_basin"}

        report = verify_particle_cache(cache)

        self.assertIn("particle_environment_contract", report["failure_codes"])

    def test_cache_assertions_must_match_compiled_contract(self) -> None:
        cache = particle_cache()
        expected = {
            field: cache["environment"][field]
            for field in ("workspace_bounds_m", "measurements", "assertions")
        }
        cache["environment"]["assertions"] = [
            assertion("weakened", "level", "final", "<=", 100.0),
        ]

        report = verify_particle_cache(cache, expected_contract=expected)

        self.assertIn("particle_contract_mismatch", report["failure_codes"])

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
            "bounds_m": {"min_m": [-1.01, -0.2, 0.0], "max_m": [0.2, 0.2, 1.0]},
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

    def test_container_bounds_follow_declared_workspace(self) -> None:
        cache = particle_cache()
        cache["environment"]["workspace_bounds_m"] = {
            "min_m": [0.0, -3.0, -0.1],
            "max_m": [2.0, -1.0, 2.0],
        }
        for frame in cache["frames"]:
            frame["positions_m"] = [[row[0] + 1.0, row[1] - 2.0, row[2]] for row in frame["positions_m"]]
            frame["surface"] = {
                **frame["surface"],
                "bounds_m": {"min_m": [1.0, -2.0, 0.0], "max_m": [1.1, -1.9, 1.0]},
            }

        report = verify_particle_cache(cache)

        self.assertEqual(report["status"], "pass", report)

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

    def test_rigid_body_state_measurement_is_bound_to_structured_cache_state(self) -> None:
        cache = particle_cache()
        declaration = {
            "id": "body_speed",
            "type": "rigid_body_state",
            "body_id": "body",
            "field": "linear_velocity_m_s",
            "component": "magnitude",
        }
        cache["environment"] = {
            **declared_environment([assertion("moves", "body_speed", "max", ">=", 1.0)]),
            "measurements": [declaration],
        }
        for frame, velocity in zip(cache["frames"], ([0.0, 0.0, 0.0], [3.0, 4.0, 0.0]), strict=True):
            frame["rigid_objects"] = {"body": {"linear_velocity_m_s": velocity}}
            frame["measurements"] = {
                "body_speed": sum(value * value for value in velocity) ** 0.5,
            }

        self.assertEqual(verify_particle_cache(cache)["status"], "pass")

        cache["frames"][1]["measurements"]["body_speed"] = 4.0
        report = verify_particle_cache(cache)
        self.assertIn("rigid_body_state_measurement_mismatch", report["failure_codes"])

    def test_float_and_sink_expectations_are_declared_rigid_body_measurements(self) -> None:
        cache = particle_cache()
        declarations = [
            {"id": "light_height", "type": "rigid_body_state", "body_id": "light", "field": "position_m", "component": "z"},
            {"id": "dense_height", "type": "rigid_body_state", "body_id": "dense", "field": "position_m", "component": "z"},
        ]
        cache["environment"] = {
            **declared_environment([
                assertion("light_remains_high", "light_height", "final", ">=", 0.5),
                assertion("dense_reaches_low", "dense_height", "final", "<=", 0.1),
            ]),
            "measurements": declarations,
        }
        for frame, light_z, dense_z in zip(cache["frames"], (0.8, 0.7), (0.8, 0.05), strict=True):
            frame["rigid_objects"] = {
                "light": {"position_m": [0.0, 0.0, light_z]},
                "dense": {"position_m": [0.0, 0.0, dense_z]},
            }
            frame["measurements"] = {"light_height": light_z, "dense_height": dense_z}

        self.assertEqual(verify_particle_cache(cache)["status"], "pass")

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
        "environment": declared_environment([
            assertion("bounded_final_level", "level", "final", "<=", 1.0),
        ]),
        "frames": [
            {"frame": 0, "time_s": 0.0, "positions_m": [[0, 0, 1], [0.1, 0, 1]], "velocities_m_s": [[0, 0, 0], [0, 0, 0]], "measurements": {"level": 1.0, "width": 0.1}, "surface": surface},
            {"frame": 1, "time_s": 0.1, "positions_m": [[0, 0, 0.9], [0.1, 0, 0.9]], "velocities_m_s": [[0, 0, -1], [0, 0, -1]], "measurements": {"level": 0.9, "width": 0.1}, "surface": surface},
        ],
    }


if __name__ == "__main__":
    unittest.main()
