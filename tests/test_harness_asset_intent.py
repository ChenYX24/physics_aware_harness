from __future__ import annotations

import unittest
import hashlib
import json
import tempfile
from pathlib import Path

from harness.assets.asset_intent import intent_from_object
from harness.assets.asset_registry import AssetRegistry
from harness.assets.asset_resolver import asset_quality_gate, resolve_asset_intents
from harness.assets.search_intent import SearchIntent, search_intent_from_v2_asset_intent
from harness.assets.sqlite_catalog import initialize_catalog


class HarnessAssetIntentTests(unittest.TestCase):
    def test_search_intent_v2_adapter_and_hard_field_validation(self) -> None:
        intent = search_intent_from_v2_asset_intent(
            {
                "intent_id": "chair_01",
                "query": "brown wooden chair",
                "backend": "unreal",
                "physics_role": "static_support",
                "constraints": {
                    "real_3d_geometry": True,
                    "collision_required": True,
                    "approx_size_cm": [60, 60, 100],
                },
            }
        )
        self.assertEqual(intent.raw_query, "brown wooden chair")
        self.assertEqual(intent.must["backend"], "unreal")
        self.assertTrue(intent.must["collision"])
        self.assertEqual(intent.must["physics_role"], "static_support")
        self.assertEqual(intent.must["approx_size_m"], [0.6, 0.6, 1.0])
        with self.assertRaisesRegex(ValueError, "unsupported hard fields"):
            SearchIntent.from_dict({"raw_query": "chair", "must": {"made_up_constraint": True}})
        with self.assertRaisesRegex(ValueError, "unsupported constraints"):
            search_intent_from_v2_asset_intent({"query": "chair", "constraints": {"made_up_constraint": True}})

    def test_physics_critical_and_visual_only_classification(self) -> None:
        rigid = intent_from_object({"id": "ball", "role": "passive_target", "shape": "sphere"})
        visual = intent_from_object({"id": "label", "role": "decal", "asset_query": "logo decal"})
        self.assertTrue(rigid.physics_critical)
        self.assertIn("collider", rigid.required_properties)
        self.assertFalse(visual.physics_critical)
        self.assertEqual(visual.category, "visual_only")

    def test_structured_physics_contract_overrides_free_form_role_vocabulary(self) -> None:
        dynamic = intent_from_object(
            {
                "id": "custom_body",
                "role": "arbitrary_semantic_name",
                "shape": "sphere",
                "body_type": "dynamic",
                "collision_required": True,
            }
        )
        self.assertTrue(dynamic.physics_critical)
        self.assertEqual(dynamic.category, "physics_critical")
        self.assertIn("collider", dynamic.required_properties)

    def test_ramp_roles_are_physics_critical(self) -> None:
        subject = intent_from_object({"id": "ramp_subject", "role": "rolling_subject", "shape": "sphere"})
        ramp = intent_from_object({"id": "ramp", "role": "ramp", "shape": "inclined_plane"})
        self.assertTrue(subject.physics_critical)
        self.assertTrue(ramp.physics_critical)

    def test_bounce_role_is_physics_critical(self) -> None:
        subject = intent_from_object({"id": "bounce_ball", "role": "bouncing_body", "shape": "sphere"})
        self.assertTrue(subject.physics_critical)
        self.assertIn("rigid_body", subject.required_properties)

    def test_rolling_role_is_physics_critical(self) -> None:
        subject = intent_from_object({"id": "rolling_ball", "role": "rolling_body", "shape": "sphere"})
        self.assertTrue(subject.physics_critical)
        self.assertIn("collider", subject.required_properties)

    def test_sliding_role_is_physics_critical(self) -> None:
        subject = intent_from_object({"id": "sliding_crate", "role": "sliding_body", "shape": "box"})
        self.assertTrue(subject.physics_critical)
        self.assertIn("rigid_body", subject.required_properties)

    def test_wind_role_is_physics_critical(self) -> None:
        subject = intent_from_object({"id": "balloon", "role": "wind_drift_body", "shape": "sphere"})
        self.assertTrue(subject.physics_critical)
        self.assertIn("collision_profile", subject.required_properties)

    def test_magnetic_roles_are_physics_critical(self) -> None:
        source = intent_from_object({"id": "magnet", "role": "magnetic_source", "shape": "fixed_point"})
        subject = intent_from_object({"id": "steel_ball", "role": "magnetized_body", "shape": "sphere"})
        self.assertTrue(source.physics_critical)
        self.assertTrue(subject.physics_critical)
        self.assertIn("rigid_body", subject.required_properties)

    def test_spinning_body_role_is_physics_critical(self) -> None:
        subject = intent_from_object({"id": "spinner", "role": "spinning_body", "shape": "sphere"})
        self.assertTrue(subject.physics_critical)
        self.assertIn("rigid_body", subject.required_properties)

    def test_agent_action_roles_are_physics_critical(self) -> None:
        agent = intent_from_object({"id": "agent", "role": "active_agent", "shape": "capsule"})
        target = intent_from_object({"id": "box", "role": "action_coupled_body", "shape": "box"})
        self.assertTrue(agent.physics_critical)
        self.assertTrue(target.physics_critical)
        self.assertIn("collision_profile", target.required_properties)

    def test_constraint_roles_are_physics_critical(self) -> None:
        anchor = intent_from_object({"id": "anchor", "role": "constraint_anchor", "shape": "fixed_point"})
        bob = intent_from_object({"id": "bob", "role": "constrained_body", "shape": "sphere"})
        self.assertTrue(anchor.physics_critical)
        self.assertTrue(bob.physics_critical)
        self.assertIn("collider", bob.required_properties)

    def test_impulse_chain_roles_are_physics_critical(self) -> None:
        driver = intent_from_object({"id": "driver", "role": "active_chain_driver", "shape": "sphere"})
        receiver = intent_from_object({"id": "receiver", "role": "constrained_chain_body", "shape": "sphere"})
        self.assertTrue(driver.physics_critical)
        self.assertTrue(receiver.physics_critical)

    def test_elastic_launch_roles_are_physics_critical(self) -> None:
        launcher = intent_from_object({"id": "spring", "role": "elastic_launcher", "shape": "spring_proxy"})
        payload = intent_from_object({"id": "payload", "role": "launched_body", "shape": "sphere"})
        self.assertTrue(launcher.physics_critical)
        self.assertTrue(payload.physics_critical)
        self.assertIn("collision_profile", launcher.required_properties)

    def test_elastic_constraint_roles_are_physics_critical(self) -> None:
        anchor = intent_from_object({"id": "anchor", "role": "elastic_constraint_anchor", "shape": "fixed_point"})
        payload = intent_from_object({"id": "payload", "role": "elastic_constrained_body", "shape": "sphere"})
        tether = intent_from_object({"id": "tether", "role": "elastic_tether_constraint", "shape": "constraint"})
        self.assertTrue(anchor.physics_critical)
        self.assertTrue(payload.physics_critical)
        self.assertTrue(tether.physics_critical)

    def test_brittle_fracture_roles_are_physics_critical(self) -> None:
        impactor = intent_from_object({"id": "striker", "role": "active_impactor", "shape": "sphere"})
        brittle = intent_from_object({"id": "panel", "role": "brittle_fracture_body", "shape": "thin_box"})
        fragment = intent_from_object({"id": "frag", "role": "fracture_fragment", "shape": "shard"})
        self.assertTrue(impactor.physics_critical)
        self.assertTrue(brittle.physics_critical)
        self.assertTrue(fragment.physics_critical)

    def test_example_registry_resolves_core_static_scene_assets(self) -> None:
        case_spec = {
            "case_id": "asset_smoke",
            "objects": [
                {"id": "cue_ball", "role": "active_striker", "shape": "sphere"},
                {"id": "ramp", "role": "ramp", "shape": "inclined_plane"},
                {"id": "projectile", "role": "projectile", "shape": "sphere"},
                {"id": "bounce_ball", "role": "bouncing_body", "shape": "sphere"},
                {"id": "rolling_ball", "role": "rolling_body", "shape": "sphere"},
                {"id": "sliding_crate", "role": "sliding_body", "shape": "box"},
                {"id": "wind_body", "role": "wind_drift_body", "shape": "sphere"},
                {"id": "magnet", "role": "magnetic_source", "shape": "fixed_point"},
                {"id": "steel_ball", "role": "magnetized_body", "shape": "sphere"},
                {"id": "spinner", "role": "spinning_body", "shape": "sphere"},
                {"id": "agent", "role": "active_agent", "shape": "capsule"},
                {"id": "payload", "role": "action_coupled_body", "shape": "box"},
                {"id": "anchor", "role": "constraint_anchor", "shape": "fixed_point"},
                {"id": "bob", "role": "constrained_body", "shape": "sphere"},
                {"id": "chain_driver", "role": "active_chain_driver", "shape": "sphere"},
                {"id": "chain_receiver", "role": "constrained_chain_body", "shape": "sphere"},
                {"id": "spring", "role": "elastic_launcher", "shape": "spring_proxy"},
                {"id": "spring_payload", "role": "launched_body", "shape": "sphere"},
                {"id": "elastic_anchor", "role": "elastic_constraint_anchor", "shape": "fixed_point"},
                {"id": "elastic_payload", "role": "elastic_constrained_body", "shape": "sphere"},
                {"id": "elastic_tether", "role": "elastic_tether_constraint", "shape": "constraint"},
                {"id": "striker", "role": "active_impactor", "shape": "sphere"},
                {"id": "glass_panel", "role": "brittle_fracture_body", "shape": "thin_box"},
                {"id": "fragment", "role": "fracture_fragment", "shape": "shard"},
            ],
        }
        result = resolve_asset_intents(case_spec, top_k=2)
        self.assertEqual(result["case_id"], "asset_smoke")
        self.assertEqual(result["capability_id"], "asset_intent_resolution")
        self.assertEqual(result["invocation_contract"]["next_capability_id"], "asset_runtime_binding_invocation")
        self.assertEqual(result["physics_critical_count"], 24)
        self.assertEqual(len(result["assets"]), 24)
        self.assertTrue(all(row["selected_asset"] for row in result["assets"]))
        self.assertTrue(all(row["runtime_binding_requirements"] for row in result["assets"]))
        self.assertTrue(all(row["selected_asset"]["quality_gate"]["status"] == "pass" for row in result["assets"]))

    def test_role_disambiguates_generic_box_proxy(self) -> None:
        result = resolve_asset_intents(
            {"case_id": "support_asset", "objects": [{"id": "table", "role": "support", "shape": "box"}]}
        )

        row = result["assets"][0]
        self.assertEqual(row["intent"]["query"], "support box")
        self.assertEqual(row["selected_asset"]["asset_id"], "analytic_low_friction_table")

    def test_explicit_analytic_policy_resolves_a_catalog_recipe_asset(self) -> None:
        result = resolve_asset_intents(
            {"case_id": "generated_rail", "objects": [{"id": "rail", "role": "support", "shape": "box", "asset_policy": "analytic_proxy"}]}
        )

        row = result["assets"][0]
        self.assertEqual(row["selected_asset"]["asset_id"], "analytic_low_friction_table")
        self.assertTrue(row["selected_asset"]["proxy"])
        self.assertEqual(row["selected_asset"]["analytic_recipe"]["provider"], "builtin_catalog")
        self.assertEqual(row["selection_reason"], "explicit_analytic_recipe_policy")
        self.assertIsNone(row["fallback_mode"])

    def test_explicit_analytic_policy_resolves_from_sqlite_core_catalog(self) -> None:
        core_registry = json.loads((Path(__file__).parents[1] / "assets" / "asset_registry.example.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            catalog = initialize_catalog(Path(tmp) / "catalog.sqlite")
            catalog.import_registry(core_registry)
            result = resolve_asset_intents(
                {
                    "case_id": "analytic_sqlite",
                    "objects": [
                        {
                            "id": "projectile",
                            "role": "projectile",
                            "shape": "sphere",
                            "force_analytic_proxy": True,
                        }
                    ],
                },
                registry=AssetRegistry(catalog.path),
            )

        selected = result["assets"][0]["selected_asset"]
        self.assertEqual(selected["asset_id"], "analytic_projectile_sphere")
        self.assertEqual(selected["source_uri"], "ue://Engine/BasicShapes/Sphere.Sphere")
        self.assertTrue(selected["analytic_recipe"])

    def test_scene_map_is_selected_by_asset_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            map_file = root / "Chosen.umap"
            map_file.write_bytes(b"map")
            registry_path = root / "registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "assets": [
                            {
                                "asset_id": "chosen_map",
                                "name": "Chosen",
                                "category_l1": "map",
                                "type": "World",
                                "ue_path": "/Game/Maps/Chosen.Chosen",
                                "source_kind": "harness_generated",
                                "source_uri": "harness://tests/maps/chosen",
                                "license": "CC0-1.0",
                                "quality_status": "approved",
                                "materialized": True,
                                "sha256": hashlib.sha256(map_file.read_bytes()).hexdigest(),
                                "paths": {"local_file": str(map_file)},
                                "ue": {
                                    "object_path": "/Game/Maps/Chosen.Chosen",
                                    "class_name": "World",
                                    "dependencies": [],
                                },
                                "backend_bindings": {
                                    "unreal": {
                                        "object_path": "/Game/Maps/Chosen.Chosen",
                                        "class_name": "World",
                                        "materialized": True,
                                        "runtime_ready": True,
                                    }
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = resolve_asset_intents(
                {
                    "case_id": "map_resolution",
                    "objects": [],
                    "scene": {"map_preference": "/Game/Maps/Chosen.Chosen"},
                },
                registry=AssetRegistry(registry_path),
            )
            unresolved = resolve_asset_intents(
                {
                    "case_id": "map_resolution_miss",
                    "objects": [],
                    "scene": {"map_preference": "/Game/Maps/Other.Other"},
                },
                registry=AssetRegistry(registry_path),
            )

        self.assertEqual(result["scene_map"]["selected_asset"]["asset_id"], "chosen_map")
        self.assertIsNone(result["scene_map"]["fallback_reason"])
        self.assertEqual(result["quality_gate"]["fallback_count"], 0)
        self.assertIsNone(unresolved["scene_map"]["selected_asset"])
        self.assertEqual(unresolved["quality_gate"]["fallback_count"], 1)

    def test_resolver_skips_unlicensed_asset_and_preserves_provenance(self) -> None:
        registry_data = {
            "assets": [
                {
                    "asset_id": "unlicensed_ball",
                    "tags": ["sphere", "ball"],
                    "ue_path": "/Game/Unlicensed.Ball",
                    "source_kind": "open_source",
                    "source_uri": "https://example.invalid/ball",
                    "license": "unknown",
                    "quality_status": "approved",
                    "collider": "sphere",
                    "mass_kg": 1.0,
                    "material": {},
                    "collision_profile": "PhysicsActor",
                },
                {
                    "asset_id": "approved_ball",
                    "tags": ["sphere", "ball"],
                    "ue_path": "/Engine/BasicShapes/Sphere.Sphere",
                    "source_kind": "engine_builtin",
                    "source_uri": "ue://Engine/BasicShapes/Sphere.Sphere",
                    "license": "Unreal Engine EULA",
                    "quality_status": "approved_proxy",
                    "collider": "sphere",
                    "mass_kg": 1.0,
                    "material": {},
                    "collision_profile": "PhysicsActor",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            path.write_text(json.dumps(registry_data), encoding="utf-8")
            result = resolve_asset_intents(
                {"case_id": "quality_gate", "objects": [{"id": "ball", "role": "passive_target", "shape": "sphere"}]},
                registry=AssetRegistry(path),
            )

        row = result["assets"][0]
        self.assertEqual(row["selected_asset"]["asset_id"], "approved_ball")
        self.assertEqual(row["selected_asset"]["source_uri"], "ue://Engine/BasicShapes/Sphere.Sphere")
        self.assertIn("missing_or_unverified_license", row["rejected_candidates"][0]["quality_gate"]["failure_codes"])
        self.assertIn("missing_or_invalid_sha256", row["rejected_candidates"][0]["quality_gate"]["failure_codes"])

    def test_quality_gate_checks_file_hash_dependencies_and_local_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset_file = root / "Board.uasset"
            dependency_file = root / "M_Board.uasset"
            asset_file.write_bytes(b"geometry collection")
            dependency_file.write_bytes(b"material")
            asset = {
                "asset_id": "generated_board",
                "ue_path": "/Game/HarnessGenerated/Board.Board",
                "source_kind": "harness_generated",
                "source_uri": "harness://tests/board",
                "license": "UNVERIFIED_LOCAL_ENTITLEMENT",
                "quality_status": "local_preview",
                "materialized": True,
                "sha256": hashlib.sha256(asset_file.read_bytes()).hexdigest(),
                "paths": {"local_file": str(asset_file)},
                "ue": {"dependencies": ["/Game/Materials/M_Board"]},
                "bundle": {
                    "dependencies": [
                        {
                            "package": "/Game/Materials/M_Board",
                            "local_path": str(dependency_file),
                            "materialized": True,
                            "sha256": hashlib.sha256(dependency_file.read_bytes()).hexdigest(),
                        }
                    ]
                },
                "backend_bindings": {"ue_5_7": {"runtime_ready": True}},
                "collider": "geometry_collection",
                "mass_kg": 12.0,
                "material": {},
                "collision_profile": "PhysicsActor",
                "bbox_size_m": [1.6, 0.08, 1.0],
            }

            preview = asset_quality_gate(asset, physics_critical=True, allow_local_preview=True)
            self.assertEqual(preview["status"], "pass_local_preview")
            self.assertTrue(preview["dependency_status"]["complete"])

            asset["bundle"]["dependencies"][0]["local_path"] = str(root / "Missing_M_Board.uasset")
            failed = asset_quality_gate(asset, physics_critical=True, allow_local_preview=True)
            self.assertEqual(failed["status"], "fail")
            self.assertIn("dependency_closure_incomplete", failed["execution_failure_codes"])
            self.assertEqual(failed["dependency_status"]["missing_files"], ["/Game/Materials/M_Board"])

            asset["bundle"]["dependencies"][0]["local_path"] = str(dependency_file)
            asset["bundle"]["dependencies"][0]["sha256"] = "0" * 64
            failed = asset_quality_gate(asset, physics_critical=True, allow_local_preview=True)
            self.assertEqual(failed["dependency_status"]["hash_mismatches"], ["/Game/Materials/M_Board"])

            asset["bundle"]["dependencies"][0]["sha256"] = hashlib.sha256(dependency_file.read_bytes()).hexdigest()
            asset["sha256"] = "0" * 64
            failed = asset_quality_gate(asset, physics_critical=True, allow_local_preview=True)
            self.assertEqual(failed["status"], "fail")
            self.assertIn("sha256_mismatch", failed["execution_failure_codes"])

    def test_reference_gate_requires_distribution_authorization(self) -> None:
        base = {
            "asset_id": "restricted_asset",
            "ue_path": "/Engine/BasicShapes/Cube.Cube",
            "source_kind": "engine_builtin",
            "source_uri": "ue://Engine/BasicShapes/Cube.Cube",
            "quality_status": "approved",
        }
        restricted = asset_quality_gate(
            {**base, "license": "All Rights Reserved", "license_tier": "reference"},
            physics_critical=False,
        )
        self.assertEqual(restricted["status"], "fail")
        self.assertFalse(restricted["reference_approved"])
        self.assertIn("reference_license_evidence_missing", restricted["reference_blockers"])

        local_only = {**base, "license": "CC0-1.0", "license_tier": "local_preview"}
        self.assertEqual(asset_quality_gate(local_only, physics_critical=False)["status"], "fail")
        preview = asset_quality_gate(local_only, physics_critical=False, allow_local_preview=True)
        self.assertEqual(preview["status"], "pass_local_preview")
        self.assertFalse(preview["reference_approved"])

    def test_asset_resolution_matches_v1_golden(self) -> None:
        registry_data = {
            "assets": [
                {
                    "asset_id": "analytic_sphere",
                    "name": "Analytic Sphere",
                    "aliases": ["ball"],
                    "tags": ["passive_target", "sphere"],
                    "category": "physics_critical",
                    "type": "StaticMesh",
                    "ue_path": "/Engine/BasicShapes/Sphere.Sphere",
                    "source_kind": "engine_builtin",
                    "source_uri": "ue://Engine/BasicShapes/Sphere.Sphere",
                    "license": "Unreal Engine EULA",
                    "quality_status": "approved_proxy",
                    "collider": "sphere",
                    "mass_kg": 1.0,
                    "material": {"static_friction": 0.2, "dynamic_friction": 0.1, "restitution": 0.5},
                    "collision_profile": "PhysicsActor",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "registry.json"
            registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
            actual = resolve_asset_intents(
                {"case_id": "asset_golden", "objects": [{"id": "ball", "role": "passive_target", "shape": "sphere"}]},
                top_k=1,
                registry=AssetRegistry(registry_path),
            )
        expected = json.loads((Path(__file__).parent / "fixtures" / "asset_resolution_v1_golden.json").read_text(encoding="utf-8"))
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
