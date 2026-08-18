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
    def test_constraint_pair_collision_flag_controls_initial_overlap_check(self) -> None:
        from harness.planning.static_scene_builder import find_overlap_pairs

        nodes = [
            {
                "object_id": object_id,
                "physics_critical": True,
                "physics": {
                    "state_kind": "rigid",
                    "collision_required": True,
                    "collision_geometry": {
                        "shape": "sphere",
                        "size_m": [0.4, 0.4, 0.4],
                        "world_center_m": position,
                    },
                },
                "bounds": {"extents_m": [0.2, 0.2, 0.2]},
                "transform": {"position_m": position, "rotation_deg": [0.0, 0.0, 0.0]},
            }
            for object_id, position in (("rod", [0.0, 0.0, 0.0]), ("bob", [0.1, 0.0, 0.0]))
        ]

        self.assertEqual(len(find_overlap_pairs(nodes)), 1)
        self.assertEqual(
            find_overlap_pairs(nodes, collision_disabled_pairs={frozenset(("rod", "bob"))}),
            [],
        )

    def load_case(self, relative_path: str) -> dict:
        return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))

    def test_declared_collision_offset_rotates_with_object_and_drives_preflight(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout

        case = {
            "case_id": "rotated_collision_offset",
            "capability_id": "rigid_body_dynamics",
            "objects": [
                {
                    "id": "offset_box",
                    "role": "dynamic body",
                    "shape": "box",
                    "size_m": [4.0, 4.0, 4.0],
                    "initial_position_m": [0.0, 0.0, 0.0],
                    "initial_rotation_deg": [0.0, 90.0, 0.0],
                    "body_type": "dynamic",
                    "collision_required": True,
                    "collision_geometry": {
                        "shape": "box",
                        "size_m": [0.2, 0.2, 0.2],
                        "local_center_offset_m": [1.0, 0.0, 0.0],
                    },
                },
                {
                    "id": "target",
                    "role": "dynamic body",
                    "shape": "box",
                    "size_m": [0.2, 0.2, 0.2],
                    "initial_position_m": [0.0, 1.05, 0.0],
                    "body_type": "dynamic",
                    "collision_required": True,
                    "collision_geometry": {
                        "shape": "box",
                        "size_m": [0.2, 0.2, 0.2],
                        "local_center_offset_m": [0.0, 0.0, 0.0],
                    },
                },
            ],
        }

        layout = build_static_scene_layout(case)
        offset_geometry = layout["object_nodes"][0]["physics"]["collision_geometry"]
        overlap = layout["overlap_pairs"][0]

        self.assertEqual(offset_geometry["world_center_m"], [0.0, 1.0, 0.0])
        self.assertEqual(overlap["world_collision_centers_m"], [[0.0, 1.0, 0.0], [0.0, 1.05, 0.0]])
        self.assertAlmostEqual(overlap["penetration_depth_m"], 0.15, places=6)
        self.assertEqual(overlap["tolerance_m"], 0.002)
        self.assertEqual(len(overlap["minimum_translation_axis"]), 3)

    def test_asset_body_setup_is_used_only_without_explicit_collision_geometry(self) -> None:
        from harness.core.scene_layout import build_object_node

        obj = {
            "id": "asset_body",
            "role": "dynamic body",
            "shape": "box",
            "size_m": [1.0, 1.0, 1.0],
            "body_type": "dynamic",
            "collision_required": True,
            "visual_representation": {"source": "asset", "visible": True},
        }
        selected = {
            "asset_id": "qualified_mesh",
            "ue_path": "/Game/Generated/SM_Qualified.SM_Qualified",
            "asset_kind": "StaticMesh",
            "collider": "box",
            "collision_profile": "PhysicsActor",
            "collision": {"present": True, "kind": "simple_convex"},
            "mass_kg": 1.0,
            "material": {"dynamic_friction": 0.4},
            "authored_size_m": [1.0, 1.0, 1.0],
        }

        node = build_object_node(obj, {"selected_asset": selected})
        self.assertIsNone(node["physics"]["collision_geometry"])
        self.assertEqual(node["physics"]["collision_binding_source"], "asset_body_setup")
        self.assertTrue(node["asset_binding"]["collision_body_setup_verified"])

        selected["collision"] = {"present": False}
        invalid = build_object_node(obj, {"selected_asset": selected})
        self.assertEqual(invalid["physics"]["collision_binding_source"], "unverified_asset_body_setup")
        self.assertFalse(invalid["asset_binding"]["collision_body_setup_verified"])

    def test_initial_preflight_has_no_static_or_visibility_exemption(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout

        objects = []
        for object_id, visible in (("hidden_static", False), ("visible_static", True)):
            objects.append({
                "id": object_id,
                "role": "support",
                "shape": "box",
                "size_m": [1.0, 1.0, 1.0],
                "initial_position_m": [0.0, 0.0, 0.0],
                "body_type": "static",
                "collision_required": True,
                "visual_representation": {"source": "asset", "visible": visible},
                "collision_geometry": {
                    "shape": "box",
                    "size_m": [0.2, 0.2, 0.2],
                    "local_center_offset_m": [0.0, 0.0, 0.0],
                },
            })

        layout = build_static_scene_layout({
            "case_id": "static_hidden_overlap",
            "capability_id": "rigid_body_dynamics",
            "objects": objects,
        })

        self.assertEqual(len(layout["overlap_pairs"]), 1)
        self.assertEqual(layout["overlap_pairs"][0]["object_ids"], ["hidden_static", "visible_static"])

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
        self.assertEqual(layout["support_relations"], [])

    def test_newton_cradle_anchors_and_balls_are_valid_suspended_bodies(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.verification.static_scene_verifier import verify_static_scene_layout

        case = self.load_case("cases/rigid_collision/newton_cradle/v001_release_angle_ofat/release_25deg.json")
        layout = build_static_scene_layout(case, asset_resolution=resolve_asset_intents(case))
        report = verify_static_scene_layout(case, layout)

        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(layout["support_relations"], [])

    def test_only_explicit_support_relations_are_validated(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout

        case = {
            "case_id": "explicit_support_only",
            "capability_id": "rigid_body_dynamics",
            "objects": [
                {
                    "id": "table_block",
                    "role": "dynamic block",
                    "shape": "box",
                    "size_m": [0.2, 0.2, 0.2],
                    "initial_position_m": [0.0, 0.0, 0.15],
                    "body_type": "dynamic",
                    "collision_required": True,
                },
                {
                    "id": "pendulum_ball",
                    "role": "pendulum bob",
                    "shape": "sphere",
                    "size_m": [0.2, 0.2, 0.2],
                    "initial_position_m": [-1.4, 0.0, 1.5],
                    "body_type": "dynamic",
                    "collision_required": True,
                },
                {
                    "id": "table_top",
                    "role": "support",
                    "shape": "box",
                    "size_m": [2.0, 1.0, 0.1],
                    "initial_position_m": [0.0, 0.0, 0.05],
                    "body_type": "static",
                    "collision_required": True,
                },
            ],
            "expected_physics": {"support": {"table_block": "table_top"}},
        }

        layout = build_static_scene_layout(case)

        self.assertEqual(len(layout["support_relations"]), 1)
        self.assertEqual(layout["support_relations"][0]["object_id"], "table_block")
        self.assertEqual(layout["support_relations"][0]["support_id"], "table_top")

    def test_inclined_ramp_uses_surface_normal_for_support_gap(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout

        case = self.load_case("cases/rigid_motion/ramp_roll_slide/v001_friction_regime_ofat/medium_friction_partial_roll.json")
        case["expected_physics"]["support"] = "ramp"
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

    def test_declared_support_uses_collision_offsets_without_rewriting_transform(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout

        case = {
            "case_id": "collision_offset_support",
            "capability_id": "rigid_body_dynamics",
            "objects": [
                {
                    "id": "barrel",
                    "role": "dynamic barrel",
                    "shape": "box",
                    "size_m": [1.0, 1.0, 1.0],
                    "initial_position_m": [0.0, 0.0, 1.294],
                    "body_type": "dynamic",
                    "collision_required": True,
                    "collision_geometry": {
                        "shape": "box",
                        "size_m": [0.4, 0.4, 0.488],
                        "local_center_offset_m": [0.0, 0.0, -0.25],
                    },
                },
                {
                    "id": "table",
                    "role": "support",
                    "shape": "box",
                    "size_m": [2.0, 2.0, 1.0],
                    "initial_position_m": [0.0, 0.0, 0.55],
                    "body_type": "static",
                    "collision_required": True,
                    "collision_geometry": {
                        "shape": "box",
                        "size_m": [2.0, 2.0, 0.1],
                        "local_center_offset_m": [0.0, 0.0, 0.2],
                    },
                },
            ],
            "expected_physics": {"support": {"barrel": "table"}},
        }

        layout = build_static_scene_layout(case)
        barrel = next(node for node in layout["object_nodes"] if node["object_id"] == "barrel")
        relation = layout["support_relations"][0]

        self.assertEqual(barrel["transform"]["position_m"], [0.0, 0.0, 1.294])
        self.assertEqual(layout["placement_adjustments"], [])
        self.assertEqual(relation["status"], "contact_at_rest")
        self.assertEqual(relation["signed_surface_gap_m"], 0.0)
        self.assertEqual(relation["suggested_translation_m"], [0.0, 0.0, 0.0])

    def test_declared_support_reports_penetration_without_repairing_it(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout

        case = {
            "case_id": "penetrating_support",
            "capability_id": "rigid_body_dynamics",
            "objects": [
                {
                    "id": "body",
                    "role": "dynamic body",
                    "shape": "box",
                    "size_m": [0.5, 0.5, 0.5],
                    "initial_position_m": [0.0, 0.0, 0.2],
                    "body_type": "dynamic",
                    "collision_required": True,
                    "collision_geometry": {"shape": "box", "size_m": [0.5, 0.5, 0.5]},
                },
                {
                    "id": "support",
                    "role": "support",
                    "shape": "box",
                    "size_m": [2.0, 2.0, 0.1],
                    "initial_position_m": [0.0, 0.0, 0.0],
                    "body_type": "static",
                    "collision_required": True,
                    "collision_geometry": {"shape": "box", "size_m": [2.0, 2.0, 0.1]},
                },
            ],
            "expected_physics": {"support": {"body": "support"}},
        }

        layout = build_static_scene_layout(case)
        body = next(node for node in layout["object_nodes"] if node["object_id"] == "body")
        relation = layout["support_relations"][0]

        self.assertEqual(body["transform"]["position_m"], [0.0, 0.0, 0.2])
        self.assertEqual(layout["placement_adjustments"], [])
        self.assertEqual(relation["status"], "penetrating_support")
        self.assertEqual(relation["signed_surface_gap_m"], -0.1)
        self.assertEqual(relation["suggested_translation_m"], [0.0, 0.0, 0.1])

    def test_declared_support_gap_fails_static_validation_without_repair(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.verification.static_scene_verifier import verify_static_scene_layout

        case = {
            "case_id": "unsupported_gap",
            "capability_id": "rigid_body_dynamics",
            "objects": [
                {
                    "id": "body",
                    "role": "dynamic body",
                    "shape": "box",
                    "size_m": [0.2, 0.2, 0.2],
                    "initial_position_m": [0.0, 0.0, 0.4],
                    "body_type": "dynamic",
                    "collision_required": True,
                    "collision_geometry": {"shape": "box", "size_m": [0.2, 0.2, 0.2]},
                },
                {
                    "id": "support",
                    "role": "support",
                    "shape": "box",
                    "size_m": [2.0, 2.0, 0.1],
                    "initial_position_m": [0.0, 0.0, 0.0],
                    "body_type": "static",
                    "collision_required": True,
                    "collision_geometry": {"shape": "box", "size_m": [2.0, 2.0, 0.1]},
                },
            ],
            "expected_physics": {"support": {"body": "support"}},
        }

        layout = build_static_scene_layout(case)
        report = verify_static_scene_layout(case, layout)
        relation = layout["support_relations"][0]

        self.assertEqual(relation["status"], "unsupported_gap")
        self.assertEqual(relation["suggested_translation_m"], [0.0, 0.0, -0.25])
        self.assertEqual(report["failure_type"], "F3_invalid_initial_physics_state")
        self.assertEqual(report["first_failure"]["metric"], "invalid_support_relation")

    def test_v2_layout_does_not_move_body_outside_support(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout

        case = {
            "case_id": "outside_support",
            "capability_id": "static_scene_placement",
            "objects": [
                {
                    "id": "crate",
                    "role": "dynamic crate",
                    "shape": "box",
                    "size_m": [0.8, 0.6, 0.6],
                    "initial_position_m": [3.1, 0.0, 0.3],
                    "body_type": "dynamic",
                    "collision_required": True,
                },
                {
                    "id": "table",
                    "role": "support",
                    "shape": "box",
                    "size_m": [5.0, 2.0, 0.1],
                    "initial_position_m": [0.0, 0.0, -0.05],
                    "body_type": "static",
                    "collision_required": True,
                },
            ],
            "expected_physics": {"support": {"crate": "table"}},
        }

        layout = build_static_scene_layout(case)
        crate = next(node for node in layout["object_nodes"] if node["object_id"] == "crate")

        self.assertEqual(crate["transform"]["position_m"][:2], [3.1, 0.0])
        self.assertFalse(any(row["type"] == "explicit_support_footprint_fit" for row in layout["placement_adjustments"]))

    def test_v2_uniform_fit_preserves_authored_overlapping_chain(self) -> None:
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

        self.assertTrue(layout["overlap_pairs"])
        self.assertEqual(target_positions, [-4.85, -5.15, -5.425])
        self.assertFalse(
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

    def test_v2_tight_triangular_sphere_rack_is_not_moved_by_aabb_overlap(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout

        rack_positions = {
            "ball_1": [1.2, 0.0, 0.057],
            "ball_2": [1.2987, 0.057, 0.057],
            "ball_3": [1.2987, -0.057, 0.057],
            "ball_4": [1.3974, 0.114, 0.057],
            "ball_5": [1.3974, 0.0, 0.057],
            "ball_6": [1.3974, -0.114, 0.057],
        }
        case = {
            "case_id": "tight_sphere_rack",
            "capability_id": "rigid_body_contact_causality",
            "v2_projection": {"source_schema_version": "harness_case_spec_v2"},
            "expected_physics": {
                "support": {object_id: "table" for object_id in rack_positions},
                "collision_graph": [["ball_1", "ball_2"]],
            },
            "objects": [
                {
                    "id": object_id,
                    "role": "target_ball",
                    "shape": "sphere",
                    "size_m": [0.114, 0.114, 0.114],
                    "radius_m": 0.057,
                    "initial_position_m": position,
                    "body_type": "dynamic",
                    "collision_required": True,
                    "collider": "sphere",
                }
                for object_id, position in rack_positions.items()
            ] + [
                {
                    "id": "table",
                    "role": "support",
                    "shape": "box",
                    "size_m": [2.92, 1.4, 0.05],
                    "initial_position_m": [0.0, 0.0, -0.025],
                    "body_type": "static",
                    "collision_required": True,
                    "collider": "box",
                }
            ],
        }

        layout = build_static_scene_layout(case)

        nodes = {node["object_id"]: node for node in layout["object_nodes"]}
        self.assertEqual(layout["overlap_pairs"], [])
        self.assertFalse(
            any(
                adjustment["type"] == "dynamic_overlap_bounds_separation"
                for adjustment in layout["placement_adjustments"]
            )
        )
        for object_id, position in rack_positions.items():
            self.assertEqual(nodes[object_id]["transform"]["position_m"][:2], position[:2])

    def test_v2_does_not_infer_collision_edges_from_object_order(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout

        case = {
            "case_id": "v2_without_declared_edges",
            "capability_id": "sequential_contact_propagation",
            "v2_projection": {"source_schema_version": "harness_case_spec_v2"},
            "expected_physics": {},
            "objects": [
                {
                    "id": "body",
                    "role": "dynamic body",
                    "shape": "box",
                    "size_m": [0.2, 0.2, 0.2],
                    "initial_position_m": [0.0, 0.0, 0.2],
                    "body_type": "dynamic",
                    "collision_required": True,
                },
                {
                    "id": "floor",
                    "role": "ground",
                    "shape": "box",
                    "size_m": [2.0, 2.0, 0.1],
                    "initial_position_m": [0.0, 0.0, 0.0],
                    "body_type": "static",
                    "collision_required": True,
                },
            ],
        }

        layout = build_static_scene_layout(case)

        self.assertEqual(layout["physics_graph"]["collision_edges"], [])

    def test_v2_collision_chain_preserves_authored_curved_layout(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout

        positions = {
            "body_1": [-0.6, 0.0, 0.2],
            "body_2": [-0.2, -0.3, 0.2],
            "body_3": [0.2, -0.3, 0.2],
            "body_4": [0.6, 0.0, 0.2],
        }
        case = {
            "case_id": "authored_curved_chain",
            "capability_id": "sequential_contact_propagation",
            "expected_physics": {
                "collision_graph": [
                    ["body_1", "body_2"],
                    ["body_2", "body_3"],
                    ["body_3", "body_4"],
                ]
            },
            "objects": [
                {
                    "id": object_id,
                    "role": "dynamic body",
                    "shape": "box",
                    "size_m": [0.1, 0.2, 0.4],
                    "initial_position_m": list(position),
                    "body_type": "dynamic",
                    "collision_required": True,
                }
                for object_id, position in positions.items()
            ],
        }

        layout = build_static_scene_layout(case)
        compiled = {
            node["object_id"]: node["transform"]["position_m"]
            for node in layout["object_nodes"]
        }

        self.assertEqual(compiled, positions)
        self.assertEqual(layout["placement_adjustments"], [])

    def test_rotated_box_sat_rejects_aabb_false_positive(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.verification.static_scene_verifier import verify_static_scene_layout

        case = {
            "case_id": "rotated_separated_boxes",
            "capability_id": "rigid_body_dynamics",
            "objects": [
                {
                    "id": "left",
                    "role": "dynamic body",
                    "shape": "box",
                    "size_m": [0.06, 0.18, 0.4],
                    "initial_position_m": [-1.225, 0.0, 0.205],
                    "initial_rotation_deg": [10.0, 47.1, 0.0],
                    "body_type": "dynamic",
                    "collision_required": True,
                    "collider": "box",
                },
                {
                    "id": "right",
                    "role": "dynamic body",
                    "shape": "box",
                    "size_m": [0.06, 0.18, 0.4],
                    "initial_position_m": [-1.05, 0.1883, 0.2],
                    "initial_rotation_deg": [0.0, 44.1, 0.0],
                    "body_type": "dynamic",
                    "collision_required": True,
                    "collider": "box",
                },
            ],
        }

        layout = build_static_scene_layout(case)
        report = verify_static_scene_layout(case, layout)

        self.assertEqual(layout["overlap_pairs"], [])
        self.assertEqual(report["checks"]["overlap_pair_count"], 0)
        self.assertEqual(report["checks"]["overlap_narrow_phase"], "oriented_box_sat")
        closest = report["checks"]["closest_oriented_box_pair"]
        self.assertEqual(closest["object_ids"], ["left", "right"])
        self.assertGreater(closest["signed_margin_m"], 0.15)
        self.assertEqual(closest["tested_axis_count"], 15)

    def test_rotated_box_sat_reports_true_overlap(self) -> None:
        from harness.planning.static_scene_builder import build_static_scene_layout

        case = {
            "case_id": "rotated_overlapping_boxes",
            "capability_id": "rigid_body_dynamics",
            "objects": [
                {
                    "id": object_id,
                    "role": "dynamic body",
                    "shape": "box",
                    "size_m": [0.2, 0.4, 0.6],
                    "initial_position_m": position,
                    "initial_rotation_deg": rotation,
                    "body_type": "dynamic",
                    "collision_required": True,
                    "collider": "box",
                }
                for object_id, position, rotation in (
                    ("left", [0.0, 0.0, 0.3], [15.0, 30.0, 5.0]),
                    ("right", [0.05, 0.02, 0.3], [-10.0, -20.0, 8.0]),
                )
            ],
        }

        layout = build_static_scene_layout(case)

        self.assertEqual(len(layout["overlap_pairs"]), 1)
        self.assertEqual(layout["overlap_pairs"][0]["overlap_test"], "oriented_box_sat")
        self.assertLess(layout["overlap_pairs"][0]["signed_margin_m"], 0.0)

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
