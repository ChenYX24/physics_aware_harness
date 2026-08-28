from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from harness.assets.asset_registry import AssetRegistry
from harness.assets.asset_intent_compiler import compile_v2_asset_intents
from harness.assets.asset_resolver import resolve_asset_intents
from harness.assets.sqlite_catalog import initialize_catalog
from harness.assets.providers.orchestrator import ProviderOrchestration
from harness.core.artifact_schema import read_json
from harness.core.case_spec_v2 import CaseSpecV2, case_spec_v2_from_dict, compile_case_spec_v2_runtime
from harness.planning.backend_planner import BackendPlanningError, plan_backend
from harness.planning.runtime_compiler import RuntimeCompilationPaused, bind_resolved_solver_assets, compile_runtime_case
from harness.runtime.fallback_backend import FallbackBackend
from harness.runtime.ue_backend import UEBackend, UEBackendUnavailable, compile_minimal_scene_spec, empty_preflight
from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement
from tests.case_spec_v2_fixture import case_spec_v2_fixture


ROOT = Path(__file__).resolve().parents[1]


class RuntimeCompilerV2Tests(unittest.TestCase):
    def test_catalog_map_id_is_bound_to_qualified_runtime_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            map_file = root / "CatalogMap.umap"
            map_file.write_bytes(b"catalog-map")
            registry_data = json.loads((ROOT / "assets" / "asset_registry.example.json").read_text(encoding="utf-8"))
            registry_data["assets"].append(
                {
                    "asset_id": "prepared_map.catalog_map.v1",
                    "name": "CatalogMap",
                    "category_l1": "map",
                    "type": "World",
                    "ue_path": "/Game/Harness/CatalogMap.CatalogMap",
                    "source_kind": "harness_generated",
                    "source_uri": "harness://tests/maps/catalog-map",
                    "license": "CC0-1.0",
                    "quality_status": "approved",
                    "materialized": True,
                    "sha256": hashlib.sha256(map_file.read_bytes()).hexdigest(),
                    "paths": {"local_file": str(map_file)},
                    "ue": {
                        "object_path": "/Game/Harness/CatalogMap.CatalogMap",
                        "class_name": "World",
                        "dependencies": [],
                    },
                    "backend_bindings": {
                        "unreal": {
                            "object_path": "/Game/Harness/CatalogMap.CatalogMap",
                            "class_name": "World",
                            "materialized": True,
                            "runtime_ready": True,
                        }
                    },
                }
            )
            registry_path = root / "registry.json"
            registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
            case_data = case_spec_v2_fixture()
            case_data["scene"]["map_preference"] = "prepared_map.catalog_map.v1"
            compilation = compile_runtime_case(
                case_spec_v2_from_dict(case_data),
                requested_backend="fallback",
                registry=AssetRegistry(registry_path),
                transaction_dir=root / "transaction",
                compile_config={
                    "schema_version": "harness_ue_compile_config_v1",
                    "map_package": "prepared_map.catalog_map.v1",
                    "ue_project": str(root / "Project.uproject"),
                    "catalog": str(registry_path),
                },
            )

        self.assertEqual(
            compilation.artifacts["asset_resolution"]["scene_map"]["selected_asset"]["asset_id"],
            "prepared_map.catalog_map.v1",
        )
        self.assertEqual(
            compilation.runtime_case.data["scene"]["map_preference"],
            "/Game/Harness/CatalogMap.CatalogMap",
        )

    def test_ue_runtime_failure_artifacts_preserve_runtime_phase(self) -> None:
        from types import SimpleNamespace

        from harness.runtime.ue_backend import write_failed_ue_artifacts

        camera_plan = {
            "scene_bounds": {"center": [0.0, 0.0, 0.0], "extent": [1.0, 1.0, 1.0]},
            "views": [{"camera_id": "front", "role": "front_static"}],
        }
        report = {
            "failure_code": "F_UE_NATIVE_SCRIPT_EXCEPTION",
            "failure_message": "native script failed",
            "failure_category": "runtime_failure",
            "phase": "runtime",
            "whether_real_ue_invoked": True,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            output_dir = run_dir / "ue_output"
            output_dir.mkdir(parents=True)
            write_failed_ue_artifacts(
                run_dir,
                output_dir,
                SimpleNamespace(case_id="case", capability_id="rigid_body_dynamics"),
                "case_ue",
                report,
                camera_plan,
                ["rgb"],
                1,
            )
            manifest = read_json(run_dir / "render_pass_manifest.json")

        self.assertEqual(manifest["source"], "ue_runtime_failure")

    def test_ue_backend_exception_exposes_stable_failure_code(self) -> None:
        error = UEBackendUnavailable(
            "native script failed",
            Path("/tmp/run"),
            "F_UE_NATIVE_SCRIPT_EXCEPTION",
            {},
        )

        self.assertEqual(error.code, "F_UE_NATIVE_SCRIPT_EXCEPTION")

    def test_map_compile_config_enters_transaction_identity_and_scene_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            map_file = root / "Configured.umap"
            map_file.write_bytes(b"configured-map")
            registry_data = json.loads((ROOT / "assets" / "asset_registry.example.json").read_text(encoding="utf-8"))
            registry_data["assets"].append(
                {
                    "asset_id": "configured_map",
                    "name": "Configured",
                    "category_l1": "map",
                    "type": "World",
                    "ue_path": "/Game/Harness/Configured.Configured",
                    "source_kind": "harness_generated",
                    "source_uri": "harness://tests/maps/configured",
                    "license": "CC0-1.0",
                    "quality_status": "approved",
                    "materialized": True,
                    "sha256": hashlib.sha256(map_file.read_bytes()).hexdigest(),
                    "paths": {"local_file": str(map_file)},
                    "ue": {
                        "object_path": "/Game/Harness/Configured.Configured",
                        "class_name": "World",
                        "dependencies": [],
                    },
                    "backend_bindings": {
                        "unreal": {
                            "object_path": "/Game/Harness/Configured.Configured",
                            "class_name": "World",
                            "materialized": True,
                            "runtime_ready": True,
                        }
                    },
                }
            )
            registry_path = root / "registry.json"
            registry_path.write_text(json.dumps(registry_data), encoding="utf-8")
            registry = AssetRegistry(registry_path)
            case = case_spec_v2_from_dict(case_spec_v2_fixture())
            first_config = {
                "schema_version": "harness_ue_compile_config_v1",
                "map_package": "/Game/Harness/Configured.Configured",
                "ue_project": str(root / "Project.uproject"),
                "catalog": str(registry_path),
            }
            first = compile_runtime_case(
                case,
                requested_backend="fallback",
                registry=registry,
                transaction_dir=root / "first",
                compile_config=first_config,
            )
            second = compile_runtime_case(
                case,
                requested_backend="fallback",
                registry=registry,
                transaction_dir=root / "second",
                compile_config={**first_config, "map_package": "/Game/Harness/Other.Other"},
            )
            first_transaction = read_json(root / "first" / "compilation_transaction.json")
            second_transaction = read_json(root / "second" / "compilation_transaction.json")

        self.assertEqual(
            first.artifacts["asset_resolution"]["scene_map"]["requested_reference"],
            "/Game/Harness/Configured.Configured",
        )
        self.assertEqual(
            compile_minimal_scene_spec(case.data, first.artifacts["asset_resolution"])["map"]["requested_package"],
            "/Game/Harness/Configured.Configured",
        )
        self.assertNotEqual(first_transaction["transaction_id"], second_transaction["transaction_id"])
        self.assertEqual(first_transaction["asset_resolve_invocation_count"], 1)
        self.assertEqual(second_transaction["asset_resolve_invocation_count"], 1)

    def test_transaction_resumes_provider_and_never_repeats_asset_resolve(self) -> None:
        class _CheckpointedOrchestrator:
            def __init__(self) -> None:
                self.calls = 0

            def fulfill(self, **_: object) -> ProviderOrchestration:
                self.calls += 1
                if self.calls == 1:
                    return ProviderOrchestration(
                        batch={
                            "schema_version": "harness_asset_provider_batch_v1",
                            "case_id": "v2_ball_contact",
                            "requests": [{"request_digest": "a" * 64}],
                            "results": [
                                {
                                    "status": "failed",
                                    "failure": {
                                        "code": "provider_network_error",
                                        "message": "reset",
                                        "retriable": True,
                                    },
                                }
                            ],
                            "receipt_ids": [],
                        },
                        results={},
                        receipts=(),
                    )
                return ProviderOrchestration(
                    batch={
                        "schema_version": "harness_asset_provider_batch_v1",
                        "case_id": "v2_ball_contact",
                        "requests": [],
                        "results": [],
                        "receipt_ids": [],
                    },
                    results={},
                    receipts=(),
                )

        case = case_spec_v2_from_dict(case_spec_v2_fixture())
        orchestrator = _CheckpointedOrchestrator()
        resolve_calls = 0
        from harness.planning import runtime_compiler as module

        original_resolve = module.resolve_asset_intents

        def counted_resolve(*args, **kwargs):
            nonlocal resolve_calls
            resolve_calls += 1
            return original_resolve(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary, patch(
            "harness.planning.runtime_compiler.resolve_asset_intents",
            side_effect=counted_resolve,
        ):
            transaction = Path(temporary) / "compilation"
            with self.assertRaises(RuntimeCompilationPaused) as context:
                compile_runtime_case(
                    case,
                    requested_backend="fallback",
                    registry=self.registry(),
                    provider_orchestrator=orchestrator,
                    transaction_dir=transaction,
                )
            self.assertTrue(context.exception.retryable)
            self.assertEqual(resolve_calls, 0)
            self.assertEqual(read_json(transaction / "compilation_transaction.json")["asset_resolve_invocation_count"], 0)

            smoke = compile_runtime_case(
                case,
                requested_backend="fallback",
                render_passes=["rgb"],
                registry=self.registry(),
                provider_orchestrator=orchestrator,
                transaction_dir=transaction,
            )
            candidate = compile_runtime_case(
                case,
                requested_backend="fallback",
                render_passes=["rgb", "depth", "segmentation"],
                registry=self.registry(),
                provider_orchestrator=orchestrator,
                transaction_dir=transaction,
            )

        self.assertEqual(smoke.status, "pass")
        self.assertEqual(candidate.status, "pass")
        self.assertEqual(resolve_calls, 1)
        self.assertEqual(orchestrator.calls, 2)
        self.assertEqual(candidate.report["asset_resolve_invocation_count"], 1)
        self.assertEqual(
            [view["camera_id"] for view in smoke.artifacts["observation_plan"]["cameras"]],
            ["front_static", "event_closeup"],
        )
        self.assertEqual(
            [view["camera_id"] for view in candidate.artifacts["observation_plan"]["cameras"]],
            ["front_static", "event_closeup"],
        )

    def test_provider_exception_is_landed_at_provider_stage_not_compile(self) -> None:
        class _ProviderFailure(RuntimeError):
            code = "provider_network_error"
            retryable = True
            request_identities = ["c" * 64]

        class _FailingOrchestrator:
            def fulfill(self, **_: object) -> object:
                raise _ProviderFailure("provider reset")

        case = case_spec_v2_from_dict(case_spec_v2_fixture())
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(_ProviderFailure):
                compile_runtime_case(
                    case,
                    requested_backend="fallback",
                    registry=self.registry(),
                    provider_orchestrator=_FailingOrchestrator(),
                    stage_result_dir=temporary,
                )

            sidecars = sorted(path.name for path in (Path(temporary) / "stage_results").glob("*.json"))
            self.assertEqual(sidecars, ["provider.json"])
            result = read_json(Path(temporary) / "stage_results" / "provider.json")
            self.assertEqual(result["stage"], "provider")
            self.assertEqual(result["failure_code"], "provider_network_error")
            self.assertEqual(result["request_identities"], ["c" * 64])

    def test_compile_exception_lands_structured_failure_and_still_raises(self) -> None:
        case = case_spec_v2_from_dict(case_spec_v2_fixture())
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(BackendPlanningError):
                compile_runtime_case(
                    case,
                    requested_backend="unknown_backend",
                    registry=self.registry(),
                    stage_result_dir=temporary,
                )

            stage_result = read_json(Path(temporary) / "stage_results" / "compile.json")
            self.assertEqual(stage_result["failure_class"], "capability_missing")
            self.assertEqual(stage_result["failure_code"], "unsupported_backend")

    def test_model_generated_solver_frame_registration_preserves_authored_transform(self) -> None:
        case = {
            "solver_scene": {
                "type": "rigid_sph",
                "measurements": [{"id": "span", "type": "axis_span", "axes": ["x"]}],
                "assertions": [
                    {"id": "span", "measurement_id": "span", "reduction": "final", "operator": ">=", "value": 0.01}
                ],
            },
            "expected_physics": {"support": {"mug": "table"}},
            "workspace_bounds_m": {"min_m": [-1.0, -1.0, -0.1], "max_m": [1.0, 1.0, 1.0]},
            "objects": [
                {
                    "id": "mug",
                    "role": "rigid_body",
                    "initial_position_m": [0.0, 0.0, 0.0],
                    "solver": {
                        "mobility": "kinematic",
                        "transform": {
                            "position_m": [0.0, 0.0, 0.0],
                            "euler_xyz_deg": [0.0, 0.0, 0.0],
                            "ue_rotation_pyr_deg": [0.0, 0.0, 0.0],
                        },
                        "collision": {
                            "type": "axisymmetric_profile",
                            "asset_geometry_match": True,
                            "fit_method": "estimated_from_request_dimensions",
                            "inner_profile": [{"z_m": 0.0, "radius_m": 0.045}, {"z_m": 0.09, "radius_m": 0.05}],
                            "wall_thickness_m": 0.005,
                            "panel_count": 16,
                        },
                        "motion": {
                            "type": "pivot_rotation",
                            "start_time_s": 0.3,
                            "duration_s": 1.5,
                            "pivot_local_m": [-0.05, 0.0, 0.09],
                            "solver_end_rotation_xyz_deg": [0.0, 110.0, 0.0],
                            "ue_end_rotation_pyr_deg": [-110.0, 0.0, 0.0],
                        },
                    },
                },
                {
                    "id": "table",
                    "role": "rigid_body",
                    "solver": {
                        "mobility": "static",
                        "transform": {
                            "position_m": [0.0, 0.0, -0.025],
                            "euler_xyz_deg": [0.0, 0.0, 0.0],
                            "ue_rotation_pyr_deg": [0.0, 0.0, 0.0],
                        },
                        "collision": {
                            "type": "plane",
                            "position_m": [0.0, 0.0, 0.0],
                            "normal": [0.0, 0.0, 1.0],
                            "asset_geometry_match": True,
                        },
                    },
                },
                {
                    "id": "water",
                    "role": "fluid",
                    "solver": {
                        "material_model": "sph_liquid",
                        "initial_volume": {
                            "shape": "cylinder",
                            "frame": {"type": "body_local", "body_id": "mug"},
                            "position_m": [0.0, 0.0, 0.04],
                            "euler_xyz_deg": [0.0, 0.0, 0.0],
                            "radius_m": 0.04,
                            "height_m": 0.06,
                        },
                    },
                },
            ],
        }
        resolution = {
            "assets": [
                {
                    "intent": {"object_id": "mug"},
                    "selected_asset": {
                        "ue_path": "/Game/Generated/Mug.Mug",
                        "sha256": "a" * 64,
                        "bbox_size_m": [0.13157, 0.098743, 0.106715],
                        "source_kind": "model_generation",
                        "geometry_analysis": {
                            "schema_version": "harness_asset_geometry_analysis_v1",
                            "axisymmetric_z_frame": {
                                "status": "verified",
                                "method": "robust_horizontal_ring_fit_v1",
                                "frame_origin_cm": [1.0, -2.0, 4.5],
                                "axis_direction": [0.0, 0.0, 1.0],
                                "ring_count": 24,
                                "center_residual_cm": 0.01,
                                "axial_coverage": 0.9,
                            },
                        },
                        "proxy": False,
                    },
                },
                {
                    "intent": {"object_id": "table"},
                    "selected_asset": {
                        "ue_path": "/Game/Generated/Table.Table",
                        "sha256": "b" * 64,
                        "bbox_size_m": [0.8, 0.8, 0.05],
                        "source_kind": "procedural_generation",
                        "provenance": {
                            "provider_id": "local_procedural_mesh_v1",
                            "generator_source_version": "primitive_mesh_v1_obj_writer_v1",
                        },
                        "proxy": False,
                    },
                },
            ]
        }
        unregistered_case = deepcopy(case)
        unregistered_resolution = deepcopy(resolution)
        del unregistered_resolution["assets"][0]["selected_asset"]["geometry_analysis"]

        error = bind_resolved_solver_assets(case, resolution)

        self.assertIsNone(error)
        mug = case["objects"][0]
        water = case["objects"][2]
        profile = mug["solver"]["collision"]["inner_profile"]
        radial_scale = (0.098743 / 2.0 - 0.005) / 0.05
        self.assertEqual([point["z_m"] for point in profile], [-0.045, 0.045])
        self.assertAlmostEqual(profile[-1]["radius_m"], 0.05 * radial_scale)
        self.assertAlmostEqual(mug["solver"]["motion"]["pivot_local_m"][0], -0.05 * radial_scale)
        self.assertAlmostEqual(mug["solver"]["motion"]["pivot_local_m"][2], 0.045)
        self.assertAlmostEqual(water["solver"]["initial_volume"]["radius_m"], 0.04 * radial_scale)
        self.assertAlmostEqual(water["solver"]["initial_volume"]["position_m"][2], -0.005)
        self.assertEqual(mug["solver"]["transform"]["position_m"], [0.0, 0.0, 0.0])
        self.assertEqual(mug["initial_position_m"], mug["solver"]["transform"]["position_m"])
        registration = mug["solver"]["collision"]["geometry_registration"]
        self.assertEqual(registration["status"], "verified")
        self.assertEqual(registration["asset_sha256"], "a" * 64)
        self.assertEqual(
            registration["solver_to_visual"],
            {
                "translation_m": [-0.01, 0.02, -0.045],
                "solver_rotation_xyz_deg": [0.0, 0.0, 0.0],
                "ue_rotation_pyr_deg": [0.0, 0.0, 0.0],
            },
        )
        self.assertEqual(mug["asset"]["geometry_registration"], registration)
        table_registration = case["objects"][1]["asset"]["geometry_registration"]
        self.assertEqual(table_registration["solver_to_visual"]["translation_m"], [0.0, 0.0, 0.0])

        missing_registration = bind_resolved_solver_assets(unregistered_case, unregistered_resolution)
        self.assertEqual(missing_registration["code"], "F3_invalid_solver_contract")
        self.assertIn("no verified axisymmetric geometry registration", missing_registration["message"])

    def test_asset_intents_only_include_asset_visual_representations(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["visual_representation"] = {"source": "solver_generated"}
        data["objects"][0]["solver"] = {"output": "renderable_geometry"}
        data["objects"][1]["visual_representation"] = {"source": "none"}
        data["objects"][1]["physics"]["collision_geometry"] = {
            "shape": "sphere",
            "size_m": [0.18, 0.18, 0.18],
        }
        data["objects"][2]["visual_representation"] = {"source": "asset"}
        data["objects"][2]["asset"] = {
            "description": "visible floor asset",
            "resource_kind": "mesh_3d",
            "acquisition": {"route": "default", "requirement": "preferred", "origin": "system_default"},
        }
        source = case_spec_v2_from_dict(data)
        runtime = compile_case_spec_v2_runtime(source)

        intents = compile_v2_asset_intents(source, runtime.data, target_backend="unreal")

        self.assertEqual([intent.object_id for intent in intents], ["floor"])
        resolution = resolve_asset_intents(
            runtime.data,
            registry=self.registry(),
            compiled_intents=intents,
            provider_results={},
            target_backend="unreal",
            allow_local_preview=True,
        )
        self.assertEqual(
            [row["intent"]["object_id"] for row in resolution["assets"]],
            ["floor"],
        )

    def test_semantic_asset_hints_compile_as_taxonomy_and_preferences(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["asset"] = {
            "description": "industrial rolling barrel",
            "resource_kind": "mesh_3d",
            "must": {
                "category": "industrial_prop",
                "physics_role": "dynamic_rigid_body",
                "collision": True,
            },
            "taxonomy": {
                "domain": "prop",
                "object_type": "steel_oil_barrel",
            },
            "relaxation_policy": {"allow_parent_category": True},
            "acquisition": {
                "route": "default",
                "requirement": "preferred",
                "origin": "system_default",
            },
        }
        source = case_spec_v2_from_dict(data)
        runtime = compile_case_spec_v2_runtime(source)

        intent = compile_v2_asset_intents(source, runtime.data, target_backend="unreal")[0].search_intent

        self.assertNotIn("category", intent.must)
        self.assertNotIn("physics_role", intent.must)
        self.assertEqual(intent.taxonomy["category"], "industrial_prop")
        self.assertIn(
            ("physics_role", "dynamic_rigid_body"),
            {(preference.field, preference.value) for preference in intent.should},
        )

    def test_solver_generated_fluid_compiles_with_cache_binding_and_no_asset(self) -> None:
        data = case_spec_v2_fixture()
        data["identity"]["case_id"] = "solver_generated_fluid"
        data["capabilities"] = {
            "primary": "fluid_particle_dynamics",
            "required": ["fluid_particle_dynamics"],
        }
        data["backend_constraints"] = {
            "required_solver_capabilities": [
                "particle_dynamics",
                "particle_cache",
                "surface_mesh_cache",
            ],
            "allowed_solvers": ["genesis_sph"],
            "render_backend": "ue",
            "allow_multi_backend": True,
        }
        data["workspace_bounds_m"] = {
            "min_m": [-1.0, -1.0, -0.1],
            "max_m": [1.0, 1.0, 1.0],
        }
        data["solver_scene"] = {
            "type": "rigid_sph",
            "initialization": {
                "state": "settled",
                "pre_roll_s": 0.25,
                "capture_after_pre_roll": True,
            },
            "measurements": [{"id": "span", "type": "axis_span", "axes": ["x", "y"]}],
            "assertions": [
                {
                    "id": "span_final",
                    "measurement_id": "span",
                    "reduction": "final",
                    "operator": ">=",
                    "value": 0.01,
                }
            ],
        }
        water = data["objects"][0]
        water.update(
            {
                "id": "water",
                "role": "fluid",
                "visual_representation": {"source": "solver_generated", "visible": True},
                "physics": {"body_type": "dynamic", "collision_required": False},
                "solver": {
                    "material_model": "sph_liquid",
                    "initial_volume": {
                        "shape": "cylinder",
                        "frame": {"type": "world"},
                        "position_m": [0.0, 0.0, 0.2],
                        "radius_m": 0.05,
                        "height_m": 0.1,
                    },
                },
            }
        )
        floor = data["objects"][2]
        floor.update(
            {
                "id": "floor",
                "role": "rigid_body",
                "visual_representation": {"source": "asset", "visible": True},
                "asset": {
                    "description": "registered test floor",
                    "resource_kind": "mesh_3d",
                    "acquisition": {
                        "route": "local_catalog",
                        "requirement": "required",
                        "origin": "user_explicit",
                        "source_uri_hint": "harness://tests/fluid-floor",
                    },
                },
                "solver": {
                    "mobility": "static",
                    "transform": {
                        "position_m": [0.0, 0.0, 0.0],
                        "euler_xyz_deg": [0.0, 0.0, 0.0],
                        "ue_rotation_pyr_deg": [0.0, 0.0, 0.0],
                    },
                    "collision": {
                        "type": "plane",
                        "position_m": [0.0, 0.0, 0.05],
                        "normal": [0.0, 0.0, 1.0],
                        "asset_geometry_match": True,
                    },
                },
            }
        )
        data["objects"] = [water, floor]
        data["relations"] = []
        data["events"] = []
        data["expected_behavior"] = {}
        data["observation_requirements"]["cameras"][0]["target_objects"] = ["water"]
        data["verification_requirements"]["assertions"] = []

        floor_asset = {
            "asset_id": "registered_fluid_floor",
            "name": "registered fluid floor",
            "source_kind": "engine_builtin",
            "source_uri": "harness://tests/fluid-floor",
            "ue_path": "/Game/Harness/FluidFloor.FluidFloor",
            "sha256": "a" * 64,
            "license": "CC0-1.0",
            "license_tier": "reference",
            "quality_status": "approved",
            "materialized": True,
            "bbox_size_m": [3.0, 2.0, 0.1],
            "authored_size_m": [3.0, 2.0, 0.1],
            "preserve_authored_scale": True,
            "collider": "box",
            "collision_profile": "PhysicsActor",
            "collision": {"present": True, "kind": "simple_box"},
            "geometry_registration": {
                "status": "verified",
                "method": "fixture_shared_frame_identity_v1",
                "asset_sha256": "a" * 64,
                "solver_to_visual": {
                    "translation_m": [0.0, 0.0, 0.0],
                    "solver_rotation_xyz_deg": [0.0, 0.0, 0.0],
                    "ue_rotation_pyr_deg": [0.0, 0.0, 0.0],
                },
            },
            "mass_kg": 100.0,
            "material": {"dynamic_friction": 0.04, "restitution": 0.15},
            "proxy": False,
        }
        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "registry.json"
            registry_path.write_text(json.dumps({"assets": [floor_asset]}), encoding="utf-8")
            compilation = compile_runtime_case(
                case_spec_v2_from_dict(data),
                requested_backend="genesis_sph",
                registry=AssetRegistry(registry_path),
            )

        self.assertEqual(compilation.status, "pass")
        water_binding = next(
            binding
            for binding in compilation.artifacts["runtime_actor_placement"]["actor_bindings"]
            if binding["object_id"] == "water"
        )
        self.assertIsNone(water_binding["asset"]["ue_path"])
        self.assertFalse(water_binding["asset"]["proxy"])
        self.assertEqual(water_binding["render_binding"]["kind"], "solver_generated")
        self.assertEqual(
            water_binding["render_binding"]["cache_contract"]["contract_id"],
            "particle_surface_cache_v1",
        )
        solve_stage = next(stage for stage in compilation.artifacts["runtime_plan"]["stages"] if stage["kind"] == "solve")
        self.assertIn("declared_measurements", solve_stage["outputs"])
        self.assertNotIn("contact_events", solve_stage["outputs"])
        solver_configuration = compilation.artifacts["solver_configuration"]
        self.assertEqual(solver_configuration["qualification_policy_id"], "genesis_wcsph_surface_v2")
        self.assertEqual(solver_configuration["parameters"]["steps_per_frame"], 100)
        self.assertEqual(
            solver_configuration["parameters"]["surface_reconstruction"],
            {
                "smoothing_length_in_particle_radii": 2.5,
                "cube_size_in_particle_radii": 1.0,
                "iso_surface_threshold": 0.35,
            },
        )
        workspace = compilation.runtime_case.data["workspace_bounds_m"]
        expected_extent = [
            high - low
            for low, high in zip(workspace["min_m"], workspace["max_m"], strict=True)
        ]
        self.assertEqual(list(compilation.artifacts["camera_plan"]["scene_bounds"]["extent"]), expected_extent)

    def test_rigid_sph_solver_targets_ue_replay_asset_bindings(self) -> None:
        source = CaseSpecV2(
            {
                "backend_constraints": {
                    "allowed_solvers": ["genesis_sph"],
                    "required_solver_capabilities": ["particle_dynamics", "surface_mesh_cache"],
                    "render_backend": "ue",
                    "allow_multi_backend": True,
                },
                "capabilities": {
                    "primary": "fluid_particle_dynamics",
                    "required": ["fluid_particle_dynamics"],
                },
            }
        )
        plan = plan_backend(
            {
                "capability_id": "fluid_particle_dynamics",
                "solver_scene": {"type": "rigid_sph"},
                "objects": [{"id": "water", "role": "fluid"}],
            },
            source_case_spec=source,
            requested_backend="genesis_sph",
        )

        self.assertEqual(plan["selected_backend"], "genesis_sph")
        self.assertEqual(plan["render_backend"], "ue")
        self.assertTrue(plan["execution_supported"])
        self.assertEqual(plan["target_asset_backend"], "unreal")

    def test_registered_genesis_to_ue_stage_pair_is_executable(self) -> None:
        source = CaseSpecV2(
            {
                "backend_constraints": {
                    "allowed_solvers": ["genesis_sph"],
                    "required_solver_capabilities": ["particle_dynamics", "surface_mesh_cache"],
                    "render_backend": "ue",
                    "allow_multi_backend": True,
                },
                "capabilities": {
                    "primary": "fluid_particle_dynamics",
                    "required": ["fluid_particle_dynamics"],
                },
            }
        )
        plan = plan_backend(
            {
                "capability_id": "fluid_particle_dynamics",
                "solver_scene": {"type": "rigid_sph"},
                "objects": [{"id": "water", "role": "fluid"}],
            },
            source_case_spec=source,
            requested_backend="genesis_sph",
        )

        self.assertTrue(plan["multi_backend"])
        self.assertTrue(plan["execution_supported"])
        self.assertIsNone(plan["execution_blocker"])
        self.assertEqual([stage["id"] for stage in plan["stages"]], ["solve", "render"])

    def test_resolved_catalog_asset_is_bound_before_solver_contract_validation(self) -> None:
        case = {
            "solver_scene": {
                "type": "rigid_sph",
                "measurements": [{"id": "span", "type": "axis_span", "axes": ["x"]}],
                "assertions": [
                    {"id": "span", "measurement_id": "span", "reduction": "final", "operator": ">=", "value": 0.01}
                ],
            },
            "workspace_bounds_m": {"min_m": [-1.0, -1.0, -0.1], "max_m": [1.0, 1.0, 1.0]},
            "objects": [
                {
                    "id": "water",
                    "role": "fluid",
                    "solver": {
                        "material_model": "sph_liquid",
                        "initial_volume": {
                            "shape": "cylinder",
                            "frame": {"type": "world"},
                            "position_m": [0.0, 0.0, 0.1],
                            "radius_m": 0.03,
                            "height_m": 0.06,
                        },
                    },
                },
                {
                    "id": "table",
                    "role": "rigid_body",
                    "solver": {
                        "mobility": "static",
                        "transform": {
                            "position_m": [0.0, 0.0, -0.025],
                            "euler_xyz_deg": [0.0, 0.0, 0.0],
                            "ue_rotation_pyr_deg": [0.0, 0.0, 0.0],
                        },
                        "collision": {
                            "type": "plane",
                            "position_m": [0.0, 0.0, 0.0],
                            "normal": [0.0, 0.0, 1.0],
                            "asset_geometry_match": True,
                        },
                    },
                },
            ],
        }
        resolution = {
            "assets": [
                {
                    "intent": {"object_id": "table"},
                    "selected_asset": {
                        "ue_path": "/Game/Generated/Table.Table",
                        "sha256": "a" * 64,
                        "bbox_size_m": [1.0, 1.0, 0.05],
                        "source_kind": "procedural_generation",
                        "provenance": {
                            "provider_id": "local_procedural_mesh_v1",
                            "generator_source_version": "primitive_mesh_v1_obj_writer_v1",
                        },
                        "proxy": False,
                    },
                }
            ]
        }

        error = bind_resolved_solver_assets(case, resolution)

        self.assertIsNone(error)
        self.assertEqual(case["objects"][1]["asset"]["ue_path"], "/Game/Generated/Table.Table")
        self.assertFalse(case["objects"][1]["asset"]["proxy"])

    def test_dynamic_rigid_sph_asset_binding_fails_closed_as_capability_missing(self) -> None:
        from tests.test_rigid_sph_scene import dynamic_asset_case

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "irregular.obj"
            source.write_text("v 0 0 0\nv 0.1 0 0\nv 0 0.2 0\nf 1 2 3\n", encoding="utf-8")
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            case = dynamic_asset_case(source)
            resolution = {
                "assets": [
                    {
                        "intent": {"object_id": "floor"},
                        "selected_asset": {
                            "ue_path": "/Game/Generated/Floor.Floor",
                            "sha256": "a" * 64,
                            "bbox_size_m": [1.0, 1.0, 0.05],
                            "source_kind": "procedural_generation",
                            "provenance": {
                                "provider_id": "local_procedural_mesh_v1",
                                "generator_source_version": "primitive_mesh_v1_obj_writer_v1",
                            },
                            "proxy": False,
                        },
                    },
                    {
                        "intent": {"object_id": "irregular_body"},
                        "selected_asset": {
                            "ue_path": "/Game/Generated/Irregular.Irregular",
                            "sha256": "b" * 64,
                            "bbox_size_m": [0.1, 0.2, 0.1],
                            "source_kind": "model_generation",
                            "proxy": False,
                            "collision": {
                                "present": True,
                                "kind": "simple_convex",
                                "portable_mesh": {
                                    "schema_version": "harness_portable_collision_mesh_v1",
                                    "role": "qualified_collision_mesh",
                                    "local_path": str(source),
                                    "format": "obj",
                                    "sha256": source_sha256,
                                    "byte_size": source.stat().st_size,
                                    "materialized": True,
                                    "coordinate_system": "asset_local_z_up_m",
                                    "artifact_to_asset_transform": {
                                        "matrix4x4": [
                                            [1.0, 0.0, 0.0, 0.0],
                                            [0.0, 1.0, 0.0, 0.0],
                                            [0.0, 0.0, 1.0, 0.0],
                                            [0.0, 0.0, 0.0, 1.0],
                                        ]
                                    },
                                },
                            },
                            "files": [
                                {
                                    "role": "import_source",
                                    "local_path": str(source),
                                    "format": "obj",
                                    "sha256": source_sha256,
                                    "materialized": True,
                                }
                            ],
                        },
                    },
                ]
            }

            self.assertIsNone(bind_resolved_solver_assets(case, resolution))
            self.assertEqual(
                case["objects"][1]["asset"]["collision"]["portable_mesh"]["local_path"],
                str(source),
            )

            unqualified = dynamic_asset_case(source)
            resolution["assets"][1]["selected_asset"]["collision"]["present"] = False
            error = bind_resolved_solver_assets(unqualified, resolution)
            self.assertEqual(error["code"], "capability_missing")

    def registry(self) -> AssetRegistry:
        return AssetRegistry(ROOT / "assets" / "asset_registry.example.json")

    def test_search_catalog_can_be_separate_from_legacy_ue_runner_registry(self) -> None:
        catalog = ROOT / "assets" / "asset_registry.example.json"
        with patch.dict(
            os.environ,
            {
                "SIM_HARNESS_ASSET_CATALOG": str(catalog),
                "SIM_STUDIO_ASSET_REGISTRY": "/runner/compat/registry.json",
            },
        ):
            registry = AssetRegistry()
        self.assertEqual(registry.path, catalog)

    def test_legacy_ue_runner_registry_never_overrides_writable_workspace_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            catalog = workspace / "catalog" / "assets" / "catalog.sqlite"
            initialize_catalog(catalog)
            legacy_registry = workspace / "asset_registry.local.json"
            legacy_registry.write_text('{"assets": []}', encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "SIM_HARNESS_WORKSPACE": str(workspace),
                    "SIM_STUDIO_ASSET_REGISTRY": str(legacy_registry),
                },
                clear=True,
            ):
                registry = AssetRegistry()
            self.assertEqual(registry.path, catalog)
            self.assertTrue(registry.writable)

    def test_v2_compiles_all_runtime_artifacts_with_one_asset_resolve(self) -> None:
        source = case_spec_v2_fixture()
        source["observation_requirements"]["cameras"][0].update({
            "subject": "cue_ball",
            "framing": "full_subject",
        })
        case = case_spec_v2_from_dict(source)
        with patch(
            "harness.planning.runtime_compiler.resolve_asset_intents",
            wraps=resolve_asset_intents,
        ) as resolver:
            compilation = compile_runtime_case(
                case,
                requested_backend="fallback",
                requested_views=["front_static"],
                render_passes=["rgb"],
                registry=self.registry(),
                job_id="job_test",
                attempt_id="attempt_001",
            )

        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(compilation.status, "pass")
        front = compilation.artifacts["observation_plan"]["cameras"][0]
        self.assertEqual(front["target"], (-0.8, 0.0, 0.09))
        self.assertGreater(front["location"][0], front["target"][0])
        self.assertEqual(compilation.report["asset_resolve_invocation_count"], 1)
        self.assertIn("event_closeup", compilation.artifacts["observation_plan"]["verifier_evidence_merged"]["camera_roles"])
        self.assertEqual(
            compilation.artifacts["scene_layout"]["camera_plan"],
            compilation.artifacts["camera_plan"],
        )
        self.assertEqual(compilation.artifacts["runtime_plan"]["backend_selection"]["selected_backend"], "fallback")
        self.assertNotIn("solver_configuration", compilation.artifacts)
        rigid_stage = compilation.artifacts["runtime_plan"]["stages"][0]
        self.assertIn("contact_events", rigid_stage["outputs"])
        self.assertNotIn("declared_measurements", rigid_stage["outputs"])
        self.assertEqual(
            compilation.artifacts["runtime_plan"]["backend_selection"]["required_case_capabilities"],
            ["rigid_body_dynamics"],
        )
        self.assertTrue(
            {"rigid_body", "contact_events"}.issubset(
                compilation.artifacts["runtime_plan"]["backend_selection"]["provided_solver_capabilities"]
            )
        )
        self.assertEqual(compilation.compiled_asset_intents[0].search_intent.must["backend"], "fallback")
        self.assertEqual(
            compilation.compiled_asset_intents[0].search_intent.must["source_kind"],
            ["analytic_proxy", "engine_builtin"],
        )

        with tempfile.TemporaryDirectory() as temporary:
            compilation.write(temporary)
            for name in (
                "case_spec_v2.json",
                "runtime_case.json",
                "asset_resolution.json",
                "scene_layout.json",
                "verification_plan.json",
                "observation_plan.json",
                "camera_plan.json",
                "runtime_actor_placement.json",
                "runtime_plan.json",
                "runtime_compilation_report.json",
            ):
                self.assertTrue((Path(temporary) / name).is_file(), name)
            self.assertTrue((Path(temporary) / "stage_results" / "compile.json").is_file())
            self.assertTrue((Path(temporary) / "stage_results" / "provider.json").is_file())
            compile_stage = read_json(Path(temporary) / "stage_results" / "compile.json")
            provider_stage = read_json(Path(temporary) / "stage_results" / "provider.json")
            self.assertEqual(compile_stage["job_id"], "job_test")
            self.assertEqual(provider_stage["attempt_id"], "attempt_001")
            self.assertFalse((Path(temporary) / "backend_plan.json").exists())

    def test_v2_dynamic_contract_does_not_depend_on_free_form_role(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["role"] = "falling object"
        data["objects"][0]["initial_state"]["position_m"][2] = 1.0
        data["objects"][1]["initial_state"]["position_m"][2] = 0.14
        data["objects"][2]["role"] = "static collision surface"
        compilation = compile_runtime_case(
            case_spec_v2_from_dict(data),
            requested_backend="fallback",
            registry=self.registry(),
        )
        self.assertEqual(compilation.status, "pass", compilation.errors)
        projected = compilation.runtime_case.data["objects"][0]
        self.assertEqual(projected["body_type"], "dynamic")
        self.assertTrue(projected["collision_required"])
        binding = next(
            row
            for row in compilation.artifacts["runtime_actor_placement"]["actor_bindings"]
            if row["object_id"] == "cue_ball"
        )
        self.assertTrue(binding["physics_critical"])
        self.assertTrue(binding["physics"]["simulate_physics"])
        self.assertTrue(binding["physics"]["collision_enabled"])
        support_relations = compilation.artifacts["scene_layout"]["support_relations"]
        self.assertEqual(support_relations, [])

        bad_placement = deepcopy(compilation.artifacts["runtime_actor_placement"])
        bad_binding = next(row for row in bad_placement["actor_bindings"] if row["object_id"] == "cue_ball")
        bad_binding["physics"]["simulate_physics"] = False
        report = verify_runtime_actor_placement(compilation.runtime_case.data, bad_placement)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["first_failure"]["metric"], "dynamic_object_not_simulated")

    def test_v2_release_events_project_to_runtime_hold_controls(self) -> None:
        data = case_spec_v2_fixture()
        data["events"] = [
            {"type": "release", "object": "cue_ball", "time": 0.35},
            {"type": "release", "object": "target_ball", "time_s": 0.0},
        ]
        projected = compile_case_spec_v2_runtime(case_spec_v2_from_dict(data)).data
        by_id = {obj["id"]: obj for obj in projected["objects"]}
        self.assertEqual(by_id["cue_ball"]["release_time_s"], 0.35)
        self.assertEqual(by_id["cue_ball"]["hold_position_m"], by_id["cue_ball"]["initial_position_m"])
        self.assertEqual(by_id["cue_ball"]["release_position_m"], by_id["cue_ball"]["initial_position_m"])
        self.assertEqual(by_id["cue_ball"]["release_velocity_m_s"], by_id["cue_ball"]["initial_velocity_m_s"])
        self.assertEqual(by_id["target_ball"]["release_time_s"], 0.0)
        self.assertNotIn("hold_position_m", by_id["target_ball"])

    def test_non_ue_solver_remains_single_backend_unless_renderer_is_explicit(self) -> None:
        data = case_spec_v2_fixture()
        data["capabilities"] = {
            "primary": "deformable_body_dynamics",
            "required": ["deformable_body_dynamics"],
        }
        data["objects"][0]["role"] = "deformable mesh"
        data["objects"][0]["physics"]["material_model"] = "fem"
        data["backend_constraints"]["required_solver_capabilities"] = ["soft_body", "mesh_cache"]
        case = case_spec_v2_from_dict(data)

        standalone = plan_backend(
            compile_case_spec_v2_runtime(case).data,
            source_case_spec=case,
            requested_backend="genesis_fem",
        )
        self.assertFalse(standalone["multi_backend"])
        self.assertTrue(standalone["execution_supported"])
        self.assertEqual(standalone["render_backend"], "genesis_fem")

        data["backend_constraints"]["render_backend"] = "ue"
        staged_case = case_spec_v2_from_dict(data)
        staged = plan_backend(
            compile_case_spec_v2_runtime(staged_case).data,
            source_case_spec=staged_case,
            requested_backend="genesis_fem",
        )
        self.assertTrue(staged["multi_backend"])
        self.assertTrue(staged["execution_supported"])
        self.assertEqual(staged["handoff_contract"]["contract_id"], "deformable_mesh_cache_v1")
        self.assertEqual([stage["id"] for stage in staged["stages"]], ["solve", "render"])

    def test_required_solver_capability_must_be_provided_by_selected_backend(self) -> None:
        data = case_spec_v2_fixture()
        data["backend_constraints"]["required_solver_capabilities"].append("geometry_collection")
        case = case_spec_v2_from_dict(data)
        with self.assertRaises(BackendPlanningError) as context:
            compile_runtime_case(case, requested_backend="fallback", registry=self.registry())
        self.assertEqual(context.exception.code, "unsupported_solver_capabilities")

    def test_fallback_execution_consumes_merged_observation_plan(self) -> None:
        case = case_spec_v2_from_dict(case_spec_v2_fixture())
        compilation = compile_runtime_case(
            case,
            requested_backend="fallback",
            render_passes=["depth"],
            registry=self.registry(),
        )
        self.assertEqual(compilation.artifacts["observation_plan"]["modalities"], ["depth", "rgb"])
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = FallbackBackend().run_case(
                compilation.runtime_case,
                temporary,
                render_passes=["depth"],
                compilation=compilation,
            )
            manifest = read_json(run_dir / "render_pass_manifest.json")
        self.assertEqual(manifest["passes"], ["depth", "rgb"])
        self.assertIn(
            "event_closeup",
            {view["camera_id"] for view in manifest["camera_plan"]["views"]},
        )

    def test_ue_execution_consumes_merged_observation_plan(self) -> None:
        case = case_spec_v2_from_dict(case_spec_v2_fixture())
        compilation = compile_runtime_case(
            case,
            requested_backend="ue",
            render_passes=["depth"],
            registry=self.registry(),
        )
        preflight = empty_preflight(case.case_id)
        preflight.update(
            failure_code="F1_UPROJECT_MISSING",
            failure_message="test preflight stop",
            next_required_action="test only",
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "harness.runtime.ue_backend.build_ue_preflight_report",
            return_value=preflight,
        ):
            with self.assertRaises(UEBackendUnavailable) as context:
                UEBackend().run_case(
                    compilation.runtime_case,
                    temporary,
                    render_passes=["depth"],
                    complete_sensor_contract=False,
                    compilation=compilation,
                )
            manifest = read_json(context.exception.run_dir / "render_pass_manifest.json")
        self.assertEqual(manifest["passes"], ["depth", "rgb"])
        self.assertIn(
            "event_closeup",
            {view["camera_id"] for view in manifest["camera_plan"]["views"]},
        )

    def test_default_route_does_not_search_local_catalog_when_policy_disallows_it(self) -> None:
        data = case_spec_v2_fixture()
        data["asset_policy"]["allow_local"] = False
        data["objects"][0]["asset"] = {
            "description": "sphere ball",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "default",
                "requirement": "preferred",
                "origin": "system_default",
                "fallback_order": [],
            },
        }
        compilation = compile_runtime_case(
            case_spec_v2_from_dict(data),
            requested_backend="fallback",
            registry=self.registry(),
        )
        row = compilation.artifacts["asset_resolution"]["assets"][0]
        self.assertEqual(row["candidates"], [])
        self.assertIsNone(row["selected_asset"])
        self.assertEqual(row["acquisition"]["status"], "local_catalog_unresolved")
        self.assertTrue(compilation.artifacts["asset_resolution"]["assets"][1]["selected_asset"])

    def test_required_local_source_uri_cannot_be_replaced_by_semantic_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path = root / "catalog.sqlite"
            initialize_catalog(catalog_path)
            registry = AssetRegistry(catalog_path)
            exact_file = root / "coffee_mug.fbx"
            similar_file = root / "meshy_mug.fbx"
            exact_file.write_bytes(b"exact-user-mug")
            similar_file.write_bytes(b"similar-meshy-mug")
            exact_uri = "local-input://sha256/exact/coffee_mug.fbx"

            def asset(asset_id: str, path: Path, source_uri: str, source_kind: str) -> dict:
                return {
                    "asset_id": asset_id,
                    "name": "Coffee Mug",
                    "semantic_name": "coffee mug",
                    "description": "coffee mug",
                    "aliases": ["coffee mug"],
                    "tags": ["active_striker"],
                    "category": "rigid_body",
                    "type": "StaticMesh",
                    "asset_kind": "StaticMesh",
                    "source_kind": source_kind,
                    "source_uri": source_uri,
                    "license": "All Rights Reserved",
                    "license_tier": "local_preview",
                    "quality_status": "approved",
                    "lifecycle_status": "runtime_bound",
                    "materialized": True,
                    "ue_path": f"/Game/Test/{asset_id}.{asset_id}",
                    "class_name": "StaticMesh",
                    "local_path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "byte_size": path.stat().st_size,
                    "bbox_size_m": [0.1, 0.1, 0.1],
                    "collider": "box",
                    "collision_profile": "PhysicsActor",
                    "mass_kg": 0.2,
                    "material": {"static_friction": 0.5},
                    "collision": {"present": True, "kind": "simple_convex"},
                    "backend_bindings": {
                        "unreal": {
                            "object_path": f"/Game/Test/{asset_id}.{asset_id}",
                            "class_name": "StaticMesh",
                            "materialized": True,
                            "runtime_ready": True,
                        }
                    },
                }

            registry.register_asset(asset("user_mug", exact_file, exact_uri, "local_input"))
            registry.register_asset(asset("meshy_mug", similar_file, "meshy://mug", "model_generation"))
            data = case_spec_v2_fixture()
            data["objects"][0]["asset"] = {
                "description": "coffee mug",
                "resource_kind": "mesh_3d",
                "acquisition": {
                    "route": "local_catalog",
                    "requirement": "required",
                    "origin": "user_explicit",
                    "source_uri_hint": exact_uri,
                    "reference_inputs": [],
                    "fallback_order": [],
                },
            }
            source = case_spec_v2_from_dict(data)
            runtime = compile_case_spec_v2_runtime(source)
            intents = compile_v2_asset_intents(source, runtime.data, target_backend="unreal")

            resolution = resolve_asset_intents(
                runtime.data,
                registry=registry,
                compiled_intents=intents,
                target_backend="unreal",
                allow_local_preview=True,
            )
            row = resolution["assets"][0]
            self.assertEqual(row["selected_asset"]["asset_id"], "user_mug")
            self.assertEqual(row["selection_reason"], "required_source_uri_exact_match")
            self.assertNotIn("meshy_mug", {item["asset_id"] for item in row["candidates"]})

            data["objects"][0]["asset"]["acquisition"]["source_uri_hint"] = "local-input://missing"
            missing_source = case_spec_v2_from_dict(data)
            missing_runtime = compile_case_spec_v2_runtime(missing_source)
            missing_intents = compile_v2_asset_intents(missing_source, missing_runtime.data, target_backend="unreal")
            missing = resolve_asset_intents(
                missing_runtime.data,
                registry=registry,
                compiled_intents=missing_intents,
                target_backend="unreal",
                allow_local_preview=True,
            )["assets"][0]
            self.assertIsNone(missing["selected_asset"])
            self.assertEqual(missing["candidates"], [])
            self.assertIsNone(missing["fallback_mode"])

    def test_required_model_generation_fails_closed_without_writable_catalog(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["asset"] = {
            "description": "a ball generated from a textual design",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "model_generation",
                "requirement": "required",
                "origin": "user_explicit",
                "reference_inputs": [],
                "fallback_order": [],
            },
        }
        compilation = compile_runtime_case(
            case_spec_v2_from_dict(data),
            requested_backend="fallback",
            registry=self.registry(),
        )

        self.assertEqual(compilation.status, "fail")
        self.assertIn("catalog_not_writable", {error["code"] for error in compilation.errors})
        row = compilation.artifacts["asset_resolution"]["assets"][0]
        self.assertEqual(row["acquisition"]["status"], "provider_blocked")
        self.assertIsNone(row["selected_asset"])
        self.assertEqual(row["fallback_mode"], "provider_required")
        placement = compilation.artifacts["runtime_actor_placement"]
        binding = next(item for item in placement["actor_bindings"] if item["object_id"] == "cue_ball")
        self.assertFalse(binding["asset"]["proxy"])
        self.assertEqual(binding["asset"]["binding_source"], "unbound")
        self.assertEqual(binding["asset"]["runtime_usage"], "unbound_required_asset")
        self.assertIsNone(binding["asset"]["source_kind"])
        self.assertEqual(binding["physics"]["collision_geometry_source"], "unbound_required_asset")

    def test_generation_reference_is_not_sent_to_similarity_retrieval(self) -> None:
        data = case_spec_v2_fixture()
        data["objects"][0]["asset"] = {
            "description": "reconstruct the pictured ball",
            "resource_kind": "mesh_3d",
            "acquisition": {
                "route": "model_generation",
                "requirement": "required",
                "origin": "user_explicit",
                "reference_inputs": [
                    {
                        "input_id": "request_image_0",
                        "usage": ["generation_condition", "geometry_reference"],
                        "allow_similarity_search": False,
                    }
                ],
                "fallback_order": [],
            },
        }
        case = case_spec_v2_from_dict(data, available_input_ids=["request_image_0"])
        compilation = compile_runtime_case(case, requested_backend="fallback", registry=self.registry())
        search = compilation.compiled_asset_intents[0].search_intent
        self.assertIsNone(search.reference_image)

    def test_cli_dispatches_v2_and_runs_the_compatible_fallback_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_path = root / "case_v2.json"
            case_path.write_text(json.dumps(case_spec_v2_fixture()), encoding="utf-8")
            output_root = root / "runs"
            environment = os.environ.copy()
            environment["SIM_HARNESS_WORKSPACE"] = str(root / "workspace")
            environment["SIM_STUDIO_ASSET_REGISTRY"] = str(ROOT / "assets" / "asset_registry.example.json")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_run_case.py"),
                    str(case_path),
                    "--backend",
                    "fallback",
                    "--output-root",
                    str(output_root),
                    "--video-root",
                    str(root / "review"),
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2, completed.stderr or completed.stdout)
            run_dir = output_root / "v2_ball_contact_fallback"
            self.assertTrue((run_dir / "case_spec_v2.json").is_file())
            self.assertTrue((run_dir / "observation_plan.json").is_file())
            self.assertTrue((run_dir / "verification_plan.json").is_file())
            self.assertTrue((run_dir / "runtime_plan.json").is_file())
            report = json.loads((run_dir / "harness_verifier.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fail")


if __name__ == "__main__":
    unittest.main()
