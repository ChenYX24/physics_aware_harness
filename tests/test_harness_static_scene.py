from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StaticScenePlacementTests(unittest.TestCase):
    def load_case(self, relative_path: str) -> dict:
        return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))

    def test_billiards_case_builds_valid_static_scene_layout(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.verification.static_scene_verifier import verify_static_scene_layout

        case = self.load_case("cases/billiards/low_speed_single_contact.json")
        asset_resolution = resolve_asset_intents(case)
        layout = build_static_scene_layout(case, asset_resolution=asset_resolution)
        report = verify_static_scene_layout(case, layout)

        self.assertEqual(layout["schema_version"], "harness_scene_layout_v1")
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["checks"]["overlap_pair_count"], 0)
        self.assertGreaterEqual(report["checks"]["physics_critical_count"], 3)
        self.assertTrue(layout["camera_plan"]["views"])
        self.assertIn(["cue_ball", "target_ball_1"], layout["physics_graph"]["collision_edges"])

    def test_static_scene_verifier_rejects_initial_overlap(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.verification.static_scene_verifier import verify_static_scene_layout

        case = self.load_case("cases/billiards/low_speed_single_contact.json")
        case = deepcopy(case)
        case["objects"][1]["initial_position_m"] = [-0.95, 0.0, 0.09]
        asset_resolution = resolve_asset_intents(case)

        report = verify_static_scene_layout(case, build_static_scene_layout(case, asset_resolution=asset_resolution))

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F3_invalid_initial_physics_state")
        self.assertEqual(report["first_failure"]["metric"], "initial_overlap_pair")

    def test_thin_panel_does_not_use_horizontal_bounding_circle_for_overlap(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout

        case = self.load_case("cases/fracture/glass_energy_response_matrix/glass_panel_e16_shatter.json")
        layout = build_static_scene_layout(case, asset_resolution=resolve_asset_intents(case))

        self.assertEqual(layout["overlap_pairs"], [])

    def test_static_scene_verifier_rejects_missing_physics_asset_binding(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.verification.static_scene_verifier import verify_static_scene_layout

        case = self.load_case("cases/billiards/low_speed_single_contact.json")
        empty_resolution = {"schema_version": "harness_asset_resolution_v1", "assets": []}

        report = verify_static_scene_layout(case, build_static_scene_layout(case, asset_resolution=empty_resolution))

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F2_asset_missing")
        self.assertEqual(report["first_failure"]["metric"], "missing_physics_asset_binding")

    def test_falling_case_records_support_relation(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.verification.static_scene_verifier import verify_static_scene_layout

        case = self.load_case("cases/falling/falling_block_on_floor.json")
        asset_resolution = resolve_asset_intents(case)
        layout = build_static_scene_layout(case, asset_resolution=asset_resolution)
        report = verify_static_scene_layout(case, layout)

        self.assertEqual(report["status"], "pass")
        relation = layout["support_relations"][0]
        self.assertEqual(relation["object_id"], "falling_block")
        self.assertEqual(relation["support_id"], "floor")
        self.assertIn(relation["status"], {"above_support", "contact_at_rest"})

    def test_elastic_anchor_and_suspended_payload_are_valid_free_bodies(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.verification.static_scene_verifier import verify_static_scene_layout

        case = self.load_case("cases/elastic_constraint/bungee_rebound.json")
        layout = build_static_scene_layout(case, asset_resolution=resolve_asset_intents(case))
        report = verify_static_scene_layout(case, layout)

        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(
            {row["object_id"]: row["status"] for row in layout["support_relations"]},
            {"anchor": "free_body_allowed", "payload": "free_body_allowed"},
        )

    def test_newton_cradle_anchors_and_balls_are_valid_suspended_bodies(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.verification.static_scene_verifier import verify_static_scene_layout

        case = self.load_case("cases/rigid_collision/newton_cradle/v001_release_angle_ofat/release_25deg.json")
        layout = build_static_scene_layout(case, asset_resolution=resolve_asset_intents(case))
        report = verify_static_scene_layout(case, layout)

        self.assertEqual(report["status"], "pass", report)
        self.assertTrue(all(row["status"] == "above_support" for row in layout["support_relations"]))

    def test_inclined_ramp_uses_surface_normal_for_support_gap(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout

        case = self.load_case("cases/rigid_motion/ramp_roll_slide/v001_friction_regime_ofat/medium_friction_partial_roll.json")
        layout = build_static_scene_layout(case)

        relation = next(row for row in layout["support_relations"] if row["object_id"] == "ramp_subject")
        self.assertEqual(relation["status"], "contact_at_rest")
        self.assertAlmostEqual(relation["vertical_gap_m"], 0.002, places=4)

    def test_support_relation_rejects_object_outside_horizontal_footprint(self) -> None:
        from harness.planning.static_scene_builder import support_relation

        subject = {
            "object_id": "crate",
            "role": "dynamic crate",
            "shape": "box",
            "transform": {"position_m": [2.9, 0.0, 0.25]},
            "bounds": {"extents_m": [0.4, 0.3, 0.25], "bottom_z": 0.0},
            "physics": {"body_type": "dynamic"},
        }
        support = {
            "object_id": "table",
            "role": "static support table",
            "shape": "box",
            "transform": {"position_m": [0.0, 0.0, -0.05]},
            "bounds": {"extents_m": [2.5, 1.0, 0.05], "top_z": 0.0},
            "physics": {"body_type": "static"},
        }
        relation = support_relation(subject, support)
        self.assertEqual(relation["status"], "outside_support_footprint")
        self.assertLess(relation["horizontal_margin_m"][0], 0.0)

    def test_v2_explicit_support_snap_uses_resolved_inclined_geometry(self) -> None:
        from harness.planning.static_scene_builder import align_v2_explicit_supports, support_relation

        subject = {
            "object_id": "barrel",
            "role": "dynamic barrel",
            "shape": "cylinder",
            "transform": {"position_m": [1.0, 0.0, 0.49]},
            "bounds": {"extents_m": [0.28, 0.28, 0.44], "bottom_z": 0.05, "top_z": 0.93},
            "physics": {"body_type": "dynamic"},
        }
        ramp = {
            "object_id": "ramp",
            "role": "static inclined ramp",
            "shape": "box",
            "transform": {"position_m": [2.445, 0.0, 0.52], "rotation_deg": [-12.0, 0.0, 0.0]},
            "bounds": {"extents_m": [2.5, 1.0, 0.05], "bottom_z": 0.47, "top_z": 0.57},
            "physics": {"body_type": "static"},
        }
        case = {
            "v2_projection": {"source_schema_version": "harness_case_spec_v2"},
            "expected_physics": {"support": {"barrel": "ramp"}},
        }
        adjustments = align_v2_explicit_supports(case, [subject, ramp])
        self.assertEqual(len(adjustments), 1)
        self.assertEqual(support_relation(subject, ramp)["status"], "contact_at_rest")

    def test_v2_static_support_transform_is_not_rewritten_by_single_support_snap(self) -> None:
        from harness.planning.static_scene_builder import align_v2_explicit_supports

        ramp = {
            "object_id": "ramp",
            "role": "static inclined ramp",
            "shape": "box",
            "transform": {"position_m": [-2.4, 0.0, 0.74], "rotation_deg": [15.0, 0.0, 0.0]},
            "bounds": {"extents_m": [2.5, 0.5, 0.1], "bottom_z": 0.64, "top_z": 0.84},
            "physics": {"body_type": "static", "collision_required": True},
        }
        ground = {
            "object_id": "ground",
            "role": "ground",
            "shape": "box",
            "transform": {"position_m": [0.0, 0.0, 0.0]},
            "bounds": {"extents_m": [10.0, 10.0, 0.05], "bottom_z": -0.05, "top_z": 0.05},
            "physics": {"body_type": "static", "collision_required": True},
        }
        case = {
            "v2_projection": {"source_schema_version": "harness_case_spec_v2"},
            "expected_physics": {"support": {"ramp": "ground"}},
        }

        adjustments = align_v2_explicit_supports(case, [ramp, ground])

        self.assertEqual(adjustments, [])
        self.assertEqual(ramp["transform"]["position_m"], [-2.4, 0.0, 0.74])

    def test_v2_uniform_fit_and_resolved_bounds_separate_dynamic_chain(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout

        case = {
            "case_id": "resolved_bounds_chain",
            "capability_id": "sequential_contact_propagation",
            "v2_projection": {"source_schema_version": "harness_case_spec_v2"},
            "expected_physics": {
                "support": {"target_1": "floor", "target_2": "floor", "target_3": "floor"},
                "collision_graph": [["target_1", "target_2"], ["target_2", "target_3"]],
            },
            "objects": [
                {
                    "id": "target_1",
                    "role": "passive target",
                    "shape": "box",
                    "size_m": [0.3, 0.3, 0.4],
                    "asset_scale_policy": "fit_uniform_to_approx_size",
                    "initial_position_m": [-4.85, 0.0, 0.2],
                    "body_type": "dynamic",
                    "collision_required": True,
                },
                {
                    "id": "target_2",
                    "role": "passive target",
                    "shape": "box",
                    "size_m": [0.3, 0.3, 0.5],
                    "asset_scale_policy": "fit_uniform_to_approx_size",
                    "initial_position_m": [-5.15, 0.0, 0.25],
                    "body_type": "dynamic",
                    "collision_required": True,
                },
                {
                    "id": "target_3",
                    "role": "passive target",
                    "shape": "box",
                    "size_m": [0.25, 0.35, 0.35],
                    "asset_scale_policy": "fit_uniform_to_approx_size",
                    "initial_position_m": [-5.425, 0.0, 0.175],
                    "body_type": "dynamic",
                    "collision_required": True,
                },
                {
                    "id": "floor",
                    "role": "ground",
                    "shape": "box",
                    "size_m": [20.0, 20.0, 0.1],
                    "initial_position_m": [0.0, 0.0, 0.0],
                    "body_type": "static",
                    "collision_required": True,
                },
            ],
        }
        authored_sizes = {
            "target_1": [0.36697, 0.342222, 0.549862],
            "target_2": [0.40001, 0.270534, 0.304724],
            "target_3": [0.505502, 0.405502, 0.25409],
            "floor": [20.0, 20.0, 0.1],
        }
        assets = {
            "schema_version": "harness_asset_resolution_v1",
            "assets": [
                {
                    "intent": {"object_id": object_id},
                    "selected_asset": {
                        "asset_id": f"asset.{object_id}",
                        "asset_kind": "StaticMesh",
                        "source_kind": "external_site" if object_id != "floor" else "procedural_generation",
                        "ue_path": f"/Game/Test/{object_id}.{object_id}",
                        "authored_size_m": size,
                        "bbox_size_m": size,
                        "preserve_authored_scale": True,
                        "collider": "box",
                    },
                    "fallback_reason": None,
                }
                for object_id, size in authored_sizes.items()
            ],
        }

        layout = build_static_scene_layout(case, asset_resolution=assets)
        nodes = {node["object_id"]: node for node in layout["object_nodes"]}
        target_positions = [nodes[f"target_{index}"]["transform"]["position_m"][0] for index in range(1, 4)]

        self.assertEqual(layout["overlap_pairs"], [])
        self.assertGreater(target_positions[0], target_positions[1])
        self.assertGreater(target_positions[1], target_positions[2])
        self.assertTrue(
            any(row["type"] == "dynamic_overlap_bounds_separation" for row in layout["placement_adjustments"])
        )
        for index in range(1, 4):
            node = nodes[f"target_{index}"]
            binding = node["asset_binding"]
            target_diagonal = sum(value * value for value in binding["target_size_m"]) ** 0.5
            effective_diagonal = sum(value * value for value in binding["effective_size_m"]) ** 0.5
            self.assertAlmostEqual(target_diagonal, effective_diagonal, places=5)
            self.assertEqual(binding["scale_policy"], "fit_uniform_to_approx_size")
            self.assertTrue(binding["scale_applied"])
            self.assertFalse(binding["preserve_authored_scale"])

    def test_v2_overlap_repair_does_not_guess_order_for_coincident_bodies(self) -> None:
        from harness.planning.static_scene_builder import separate_v2_dynamic_overlaps

        nodes = [
            {
                "object_id": object_id,
                "physics_critical": True,
                "transform": {"position_m": [0.0, 0.0, 0.25]},
                "bounds": {"extents_m": [0.2, 0.2, 0.25]},
                "physics": {"body_type": "dynamic", "collision_required": True},
            }
            for object_id in ("left", "right")
        ]
        case = {
            "v2_projection": {"source_schema_version": "harness_case_spec_v2"},
            "expected_physics": {"collision_graph": [["left", "right"]]},
        }

        adjustments = separate_v2_dynamic_overlaps(case, nodes, [["left", "right"]])

        self.assertEqual(adjustments, [])
        self.assertEqual(nodes[0]["transform"]["position_m"], nodes[1]["transform"]["position_m"])

    def test_static_scene_cli_writes_layout_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_build_static_scene.py"),
                    str(ROOT / "cases" / "billiards" / "low_speed_single_contact.json"),
                    "--output-dir",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["status"], "pass")
            self.assertTrue((Path(tmp) / "scene_layout.json").exists())
            self.assertTrue((Path(tmp) / "static_scene_report.json").exists())
            self.assertTrue((Path(tmp) / "asset_resolution.json").exists())


if __name__ == "__main__":
    unittest.main()
