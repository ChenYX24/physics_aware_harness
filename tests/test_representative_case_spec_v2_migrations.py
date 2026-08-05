from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from harness.assets.asset_registry import AssetRegistry
from harness.assets.asset_resolver import resolve_asset_intents
from harness.core.case_spec import CaseSpec, load_case_spec, load_case_spec_document
from harness.core.case_spec_v2 import CaseSpecV2, project_case_spec_v2_to_v1
from harness.planning.runtime_compiler import compile_runtime_case


ROOT = Path(__file__).resolve().parents[1]
REPRESENTATIVE_CASES = (
    (
        ROOT / "cases" / "falling" / "falling_block_on_floor.json",
        ROOT / "cases_v2" / "representative" / "falling_block_on_floor.json",
    ),
    (
        ROOT / "cases" / "billiards" / "low_speed_single_contact.json",
        ROOT / "cases_v2" / "representative" / "low_speed_single_contact.json",
    ),
    (
        ROOT / "cases" / "domino" / "five_domino_chain.json",
        ROOT / "cases_v2" / "representative" / "five_domino_chain.json",
    ),
    (
        ROOT / "cases" / "projectile" / "upward_throw_arc.json",
        ROOT / "cases_v2" / "representative" / "upward_throw_arc.json",
    ),
)


class RepresentativeCaseSpecV2MigrationTests(unittest.TestCase):
    def registry(self) -> AssetRegistry:
        return AssetRegistry(ROOT / "assets" / "asset_registry.example.json")

    def test_v2_documents_preserve_v1_physics_contracts_after_projection(self) -> None:
        for v1_path, v2_path in REPRESENTATIVE_CASES:
            with self.subTest(case=v2_path.stem):
                v1 = load_case_spec(v1_path)
                v2 = load_case_spec_document(v2_path)
                self.assertIsInstance(v1, CaseSpec)
                self.assertIsInstance(v2, CaseSpecV2)

                projected = project_case_spec_v2_to_v1(v2)
                self.assertEqual(projected.case_id, v1.case_id)
                self.assertEqual(projected.capability_id, v1.capability_id)
                self.assertEqual(projected.should_pass, v1.should_pass)
                self.assertEqual(
                    [obj["id"] for obj in projected.objects],
                    [obj["id"] for obj in v1.objects],
                )
                for key, value in v1.data["expected_physics"].items():
                    self.assertEqual(projected.data["expected_physics"].get(key), value, key)
                v2_evidence = set(projected.data["required_signals"]) | set(
                    v2.data["observation_requirements"].get("modalities") or []
                )
                self.assertTrue(set(v1.data["required_signals"]).issubset(v2_evidence))
                self._assert_object_physics_preserved(v1, projected)

                if "scene" in v1.data:
                    for key in ("layout", "duration_s", "coordinate_system", "map_preference"):
                        if key in v1.data["scene"]:
                            self.assertEqual(projected.data["scene"].get(key), v1.data["scene"][key])
                if "timebase" in v1.data:
                    for key in ("physics_hz", "render_fps", "sample_phase"):
                        if key in v1.data["timebase"]:
                            self.assertEqual(projected.data["timebase"].get(key), v1.data["timebase"][key])
                if "physical_parameters" in v1.data:
                    self.assertEqual(projected.data["physical_parameters"], v1.data["physical_parameters"])

    def test_each_v2_migration_compiles_with_exactly_one_asset_resolve_and_no_provider_request(self) -> None:
        for _, v2_path in REPRESENTATIVE_CASES:
            with self.subTest(case=v2_path.stem):
                v2 = load_case_spec_document(v2_path)
                with patch(
                    "harness.planning.runtime_compiler.resolve_asset_intents",
                    wraps=resolve_asset_intents,
                ) as resolver:
                    compilation = compile_runtime_case(
                        v2,
                        requested_backend="fallback",
                        registry=self.registry(),
                    )

                self.assertEqual(resolver.call_count, 1)
                self.assertEqual(compilation.report["asset_resolve_invocation_count"], 1)
                self.assertEqual(compilation.selected_backend, "fallback")
                self.assertEqual(compilation.artifacts["static_scene_report"]["status"], "pass")
                self.assertEqual(compilation.artifacts["runtime_actor_placement_report"]["status"], "pass")
                self.assertEqual(compilation.artifacts["asset_provider_batch"]["requests"], [])
                self.assertEqual(compilation.artifacts["asset_provider_batch"]["results"], [])
                if v2.case_id == "five_domino_chain":
                    self.assertEqual(
                        [error["code"] for error in compilation.errors],
                        ["F3_UE_MAP_UNRESOLVED"],
                    )
                else:
                    self.assertEqual(compilation.status, "pass", compilation.errors)

    def test_v2_migrations_bind_the_intended_analytic_geometry(self) -> None:
        expected_assets = {
            "falling_block_on_floor": ["analytic_crate_box", "analytic_low_friction_table"],
            "low_speed_single_contact": [
                "analytic_sphere_billiard_ball",
                "analytic_sphere_billiard_ball",
                "analytic_low_friction_table",
            ],
            "five_domino_chain": [
                "analytic_crate_box",
                "analytic_crate_box",
                "analytic_crate_box",
                "analytic_crate_box",
                "analytic_crate_box",
                "analytic_low_friction_table",
            ],
            "upward_throw_arc": ["analytic_projectile_sphere", "analytic_low_friction_table"],
        }
        for _, v2_path in REPRESENTATIVE_CASES:
            with self.subTest(case=v2_path.stem):
                compilation = compile_runtime_case(
                    load_case_spec_document(v2_path),
                    requested_backend="fallback",
                    registry=self.registry(),
                )
                actual = [
                    row["selected_asset"]["asset_id"] if row.get("selected_asset") else None
                    for row in compilation.artifacts["asset_resolution"]["assets"]
                ]
                self.assertEqual(actual, expected_assets[compilation.runtime_case.case_id])

    def _assert_object_physics_preserved(self, v1: CaseSpec, projected: CaseSpec) -> None:
        projected_by_id = {obj["id"]: obj for obj in projected.objects}
        exact_fields = (
            "role",
            "mass_kg",
            "size_m",
            "radius_m",
            "collider",
            "collision_profile",
            "material",
            "linear_damping",
            "angular_damping",
            "initial_position_m",
            "initial_rotation_deg",
            "initial_velocity_m_s",
            "initial_angular_velocity_rad_s",
        )
        for original in v1.objects:
            migrated = projected_by_id[original["id"]]
            for field in exact_fields:
                if field in original:
                    self.assertEqual(migrated.get(field), original[field], f"{original['id']}.{field}")
            if original.get("shape") == "sphere":
                self.assertEqual(migrated.get("shape"), "sphere")
            if original.get("shape") == "box":
                self.assertEqual(migrated.get("collider"), "box")


if __name__ == "__main__":
    unittest.main()
