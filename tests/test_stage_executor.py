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

    def test_empty_plan_writes_execute_sidecar_before_raising(self) -> None:
        compilation = SimpleNamespace(selected_backend="solver_a", artifacts={"runtime_plan": {"stages": []}})
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(StageExecutionError):
                execute_runtime_plan(
                    self.case,
                    temporary,
                    compilation=compilation,
                    requested_views=None,
                    render_passes=None,
                    camera_strategy="bounds_auto_v1",
                    profile="smoke",
                    width=320,
                    height=180,
                    complete_sensor_contract=False,
                )

            result = read_json(Path(temporary) / "generic_staged_case_solver_a" / "stage_results" / "execute.json")
            self.assertEqual(result["failure_code"], "runtime_plan_empty")

    def test_keyboard_interrupt_writes_interrupted_execute_sidecar(self) -> None:
        class _InterruptedBackend:
            def run_case(self, *_args: object, **_kwargs: object) -> Path:
                raise KeyboardInterrupt()

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(KeyboardInterrupt):
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
                    backend_factories={"solver_a": _InterruptedBackend},
                )

            result = read_json(Path(temporary) / "generic_staged_case_solver_a" / "stage_results" / "execute.json")
            self.assertEqual(result["status"], "interrupted")
            self.assertEqual(result["failure_class"], "interrupted")

    def test_compatible_backends_are_discovered_by_shared_contract(self) -> None:
        handoff = stage_handoff_contract("taichi_cloth", "ue")

        self.assertIsNotNone(handoff)
        self.assertEqual(handoff["contract_id"], "deformable_mesh_cache_v1")

        particle_handoff = stage_handoff_contract("genesis_sph", "ue")
        self.assertEqual(
            particle_handoff["numeric_tolerances"],
            {"spatial_measurement_absolute": 1e-6},
        )

    def test_backends_without_shared_contract_are_rejected(self) -> None:
        self.assertIsNone(stage_handoff_contract("genesis_fem", "fallback"))

    def test_particle_handoff_solves_once_and_is_immutable_across_profiles(self) -> None:
        handoff = {
            "contract_id": "particle_surface_cache_v1",
            "schema_version": "harness_particle_cache_v1",
            "producer_backend": "solver_p",
            "consumer_backend": "renderer_p",
            "required_artifacts": ["particle_cache.json"],
            "adapter_contract": "surface_mesh_sequence_replay_v1",
        }
        solver_configuration = {
            "schema_version": "harness_rigid_sph_solver_configuration_v1",
            "qualification_policy_id": "genesis_wcsph_surface_v1",
            "parameters": {"duration_s": 1.0},
        }
        compilation = SimpleNamespace(
            selected_backend="solver_p",
            artifacts={
                "solver_configuration": solver_configuration,
                "runtime_plan": {
                    "stages": [
                        {"id": "solve", "kind": "solve", "backend": "solver_p", "handoff_contract": handoff},
                        {"id": "render", "kind": "render", "backend": "renderer_p", "handoff_contract": handoff},
                    ]
                },
            },
        )
        particle_case = RuntimeCase({**self.case.data, "capability_id": "fluid_particle_dynamics"})
        solve_count = 0

        class _ParticleBackend:
            def run_case(inner_self, case: RuntimeCase, output_root: str | Path) -> Path:
                nonlocal solve_count
                solve_count += 1
                run_dir = Path(output_root) / f"{case.case_id}_solver_p"
                (run_dir / "surface_frames").mkdir(parents=True)
                (run_dir / "surface_frames" / "frame_0000.obj").write_text("v 0 0 0\nf 1 1 1\n", encoding="utf-8")
                write_json(run_dir / "solver_configuration.json", solver_configuration)
                write_json(
                    run_dir / "particle_cache.json",
                    {
                        "schema_version": "harness_particle_cache_v1",
                        "frames": [{"frame": 0, "surface": {"path": "surface_frames/frame_0000.obj"}}],
                    },
                )
                return run_dir

        rendered: list[str] = []

        def render(_run_dir: str | Path, *, profile: str, **_kwargs: object) -> dict:
            rendered.append(profile)
            return {"status": "completed"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handoff_root = root / "physics_handoff"
            for profile in ("smoke", "candidate"):
                execute_runtime_plan(
                    particle_case,
                    root / profile,
                    compilation=compilation,
                    requested_views=None,
                    render_passes=None,
                    camera_strategy="bounds_auto_v1",
                    profile=profile,
                    width=320,
                    height=180,
                    complete_sensor_contract=False,
                    backend_factories={"solver_p": _ParticleBackend},
                    render_adapters={"renderer_p": render},
                    physics_handoff_root=handoff_root,
                )

            self.assertEqual(solve_count, 1)
            self.assertEqual(rendered, ["smoke", "candidate"])
            self.assertTrue((root / "candidate" / "generic_staged_case_solver_p" / "physics_handoff.json").is_file())

            manifest = read_json(handoff_root / "manifest.json")
            cached = handoff_root / "files" / manifest["files"][0]["path"]
            cached.write_bytes(cached.read_bytes() + b"mutated")
            with self.assertRaisesRegex(StageExecutionError, "changed"):
                execute_runtime_plan(
                    particle_case,
                    root / "publish",
                    compilation=compilation,
                    requested_views=None,
                    render_passes=None,
                    camera_strategy="bounds_auto_v1",
                    profile="candidate",
                    width=320,
                    height=180,
                    complete_sensor_contract=False,
                    backend_factories={"solver_p": _ParticleBackend},
                    render_adapters={"renderer_p": render},
                    physics_handoff_root=handoff_root,
                )


if __name__ == "__main__":
    unittest.main()
