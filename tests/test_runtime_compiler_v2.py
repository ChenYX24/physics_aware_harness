from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from harness.assets.asset_registry import AssetRegistry
from harness.assets.asset_resolver import resolve_asset_intents
from harness.core.artifact_schema import read_json
from harness.core.case_spec_v2 import case_spec_v2_from_dict
from harness.planning.backend_planner import BackendPlanningError, plan_backend
from harness.planning.runtime_compiler import compile_runtime_case
from harness.runtime.fallback_backend import FallbackBackend
from harness.runtime.ue_backend import UEBackend, UEBackendUnavailable, empty_preflight
from tests.case_spec_v2_fixture import case_spec_v2_fixture


ROOT = Path(__file__).resolve().parents[1]


class RuntimeCompilerV2Tests(unittest.TestCase):
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

    def test_v2_compiles_all_runtime_artifacts_with_one_asset_resolve(self) -> None:
        case = case_spec_v2_from_dict(case_spec_v2_fixture())
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
            )

        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(compilation.status, "pass")
        self.assertEqual(compilation.report["asset_resolve_invocation_count"], 1)
        self.assertIn("event_closeup", compilation.artifacts["observation_plan"]["verifier_evidence_merged"]["camera_roles"])
        self.assertEqual(
            compilation.artifacts["scene_layout"]["camera_plan"],
            compilation.artifacts["camera_plan"],
        )
        self.assertEqual(compilation.artifacts["runtime_plan"]["backend_selection"]["selected_backend"], "fallback")
        self.assertEqual(
            compilation.artifacts["runtime_plan"]["backend_selection"]["required_case_capabilities"],
            ["rigid_body_contact_causality"],
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
                "runtime_case_spec_v1.json",
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
            self.assertFalse((Path(temporary) / "backend_plan.json").exists())

    def test_non_ue_solver_remains_single_backend_unless_renderer_is_explicit(self) -> None:
        data = case_spec_v2_fixture()
        data["capabilities"] = {
            "primary": "soft_body_deformation",
            "required": ["soft_body_deformation"],
        }
        data["backend_constraints"]["required_solver_capabilities"] = ["soft_body", "mesh_cache"]
        case = case_spec_v2_from_dict(data)

        standalone = plan_backend(
            {"capability_id": "soft_body_deformation"},
            source_case_spec=case,
            requested_backend="genesis_fem",
        )
        self.assertFalse(standalone["multi_backend"])
        self.assertTrue(standalone["execution_supported"])
        self.assertEqual(standalone["render_backend"], "genesis_fem")

        data["backend_constraints"]["render_backend"] = "ue"
        staged_case = case_spec_v2_from_dict(data)
        staged = plan_backend(
            {"capability_id": "soft_body_deformation"},
            source_case_spec=staged_case,
            requested_backend="genesis_fem",
        )
        self.assertTrue(staged["multi_backend"])
        self.assertFalse(staged["execution_supported"])
        self.assertEqual([stage["id"] for stage in staged["stages"]], ["solve", "render"])

    def test_required_solver_capability_must_be_provided_by_selected_backend(self) -> None:
        data = case_spec_v2_fixture()
        data["backend_constraints"]["required_solver_capabilities"].append("quantum_entanglement")
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

    def test_required_model_generation_is_structurally_blocked_until_provider_phase(self) -> None:
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
        self.assertIn("unsupported_provider_route", {error["code"] for error in compilation.errors})
        row = compilation.artifacts["asset_resolution"]["assets"][0]
        self.assertEqual(row["acquisition"]["status"], "provider_blocked")
        self.assertIsNone(row["selected_asset"])
        self.assertEqual(row["fallback_mode"], "provider_required")

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
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            run_dir = output_root / "v2_ball_contact_fallback"
            self.assertTrue((run_dir / "case_spec_v2.json").is_file())
            self.assertTrue((run_dir / "observation_plan.json").is_file())
            self.assertTrue((run_dir / "verification_plan.json").is_file())
            self.assertTrue((run_dir / "runtime_plan.json").is_file())


if __name__ == "__main__":
    unittest.main()
