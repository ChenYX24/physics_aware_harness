from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from harness.assets.asset_registry import AssetRegistry
from harness.assets.asset_resolver import resolve_asset_intents
from harness.core.case_spec_v2 import compile_case_spec_v2_runtime, load_case_spec_v2
from harness.core.runtime_case import RuntimeCase
from harness.planning.runtime_compiler import compile_runtime_case


ROOT = Path(__file__).resolve().parents[1]
REPRESENTATIVE_CASES = (
    ROOT / "cases_v2" / "representative" / "falling_block_on_floor.json",
    ROOT / "cases_v2" / "representative" / "low_speed_single_contact.json",
    ROOT / "cases_v2" / "representative" / "five_domino_chain.json",
    ROOT / "cases_v2" / "representative" / "upward_throw_arc.json",
)


class RepresentativeCaseSpecV2MigrationTests(unittest.TestCase):
    def registry(self) -> AssetRegistry:
        return AssetRegistry(ROOT / "assets" / "asset_registry.example.json")

    def test_v2_documents_compile_to_canonical_runtime_contracts(self) -> None:
        for v2_path in REPRESENTATIVE_CASES:
            with self.subTest(case=v2_path.stem):
                source = load_case_spec_v2(v2_path)
                runtime = compile_case_spec_v2_runtime(source)

                self.assertIsInstance(runtime, RuntimeCase)
                self.assertEqual(runtime.data["schema_version"], "harness_runtime_case_v2")
                self.assertEqual(runtime.case_id, source.case_id)
                self.assertEqual(runtime.capability_id, "rigid_body_dynamics")
                self.assertEqual([obj["id"] for obj in runtime.objects], [obj["id"] for obj in source.objects])
                self.assertEqual(runtime.data["source_contract"]["source_schema_version"], "harness_case_spec_v2")

    def test_each_v2_migration_compiles_with_exactly_one_asset_resolve_and_no_provider_request(self) -> None:
        for v2_path in REPRESENTATIVE_CASES:
            with self.subTest(case=v2_path.stem):
                v2 = load_case_spec_v2(v2_path)
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
        for v2_path in REPRESENTATIVE_CASES:
            with self.subTest(case=v2_path.stem):
                compilation = compile_runtime_case(
                    load_case_spec_v2(v2_path),
                    requested_backend="fallback",
                    registry=self.registry(),
                )
                actual = [
                    row["selected_asset"]["asset_id"] if row.get("selected_asset") else None
                    for row in compilation.artifacts["asset_resolution"]["assets"]
                ]
                self.assertEqual(actual, expected_assets[compilation.runtime_case.case_id])

if __name__ == "__main__":
    unittest.main()
