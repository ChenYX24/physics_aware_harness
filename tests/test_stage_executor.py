from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness.core.artifact_schema import read_json, write_json
from harness.core.runtime_case import RuntimeCase
from harness.runtime.stage_contracts import stage_handoff_contract
from harness.runtime.stage_executor import StageExecutionError, execute_runtime_plan


class _CacheBackend:
    def run_case(self, case: RuntimeCase, output_root: str | Path) -> Path:
        run_dir = Path(output_root) / f"{case.case_id}_solver"
        run_dir.mkdir(parents=True)
        write_json(
            run_dir / "deformable_cache.json",
            {"schema_version": "harness_deformable_mesh_cache_v1"},
        )
        (run_dir / "deformable_cache.npz").write_bytes(b"cache")
        return run_dir


class StageExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = RuntimeCase(
            {
                "schema_version": "harness_runtime_case_v2",
                "case_id": "generic_staged_case",
                "capability_id": "deformable_body_dynamics",
                "prompt": "generic deformable state",
                "should_pass": True,
                "objects": [],
            }
        )
        handoff = {
            "contract_id": "deformable_mesh_cache_v1",
            "schema_version": "harness_deformable_mesh_cache_v1",
            "producer_backend": "solver_a",
            "consumer_backend": "renderer_b",
            "required_artifacts": ["deformable_cache.json", "deformable_cache.npz"],
            "adapter_contract": "surface_mesh_sequence_replay_v1",
        }
        self.compilation = SimpleNamespace(
            selected_backend="solver_a",
            artifacts={
                "runtime_plan": {
                    "stages": [
                        {"id": "solve", "kind": "solve", "backend": "solver_a", "handoff_contract": handoff},
                        {"id": "render", "kind": "render", "backend": "renderer_b", "handoff_contract": handoff},
                    ]
                }
            },
        )

    def test_executes_stages_by_artifact_contract_without_backend_pair_routing(self) -> None:
        rendered: list[tuple[Path, str]] = []

        def render(run_dir: str | Path, *, handoff_contract: dict, **_: object) -> dict:
            rendered.append((Path(run_dir), handoff_contract["contract_id"]))
            return {"status": "completed"}

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = execute_runtime_plan(
                self.case,
                temporary,
                compilation=self.compilation,
                requested_views=None,
                render_passes=None,
                camera_strategy="bounds_auto_v1",
                profile="smoke",
                width=320,
                height=180,
                complete_sensor_contract=False,
                backend_factories={"solver_a": _CacheBackend},
                render_adapters={"renderer_b": render},
            )

            self.assertEqual(rendered, [(run_dir, "deformable_mesh_cache_v1")])
            self.assertTrue((run_dir / "stage_execution_report.json").is_file())
            stage_result = read_json(run_dir / "stage_results" / "execute.json")
            self.assertEqual(stage_result["status"], "completed")

    def test_rejects_missing_handoff_artifacts_before_render(self) -> None:
        class _IncompleteBackend:
            def run_case(self, case: RuntimeCase, output_root: str | Path) -> Path:
                run_dir = Path(output_root) / f"{case.case_id}_solver"
                run_dir.mkdir(parents=True)
                return run_dir

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(StageExecutionError) as context:
                execute_runtime_plan(
                    self.case,
                    temporary,
                    compilation=self.compilation,
                    requested_views=None,
                    render_passes=None,
                    camera_strategy="bounds_auto_v1",
                    profile="smoke",
                    width=320,
                    height=180,
                    complete_sensor_contract=False,
                    backend_factories={"solver_a": _IncompleteBackend},
                    render_adapters={"renderer_b": lambda *_args, **_kwargs: {}},
                )

            self.assertEqual(context.exception.code, "stage_handoff_incomplete")
            stage_result = read_json(Path(temporary) / "generic_staged_case_solver" / "stage_results" / "execute.json")
            self.assertEqual(stage_result["failure_class"], "artifact_incomplete")

    def test_compatible_backends_are_discovered_by_shared_contract(self) -> None:
        handoff = stage_handoff_contract("taichi_cloth", "ue")

        self.assertIsNotNone(handoff)
        self.assertEqual(handoff["contract_id"], "deformable_mesh_cache_v1")

    def test_backends_without_shared_contract_are_rejected(self) -> None:
        self.assertIsNone(stage_handoff_contract("genesis_fem", "fallback"))


if __name__ == "__main__":
    unittest.main()
