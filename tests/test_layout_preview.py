from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.agent.job_schema import stable_digest
from harness.core.artifact_schema import write_json
from harness.runtime.layout_preview import (
    layout_preview_request_digest,
    validate_frozen_compilation,
)
from harness.runtime.replay_preview import apply_runtime_transform_overrides
from scripts.harness_local_ue_runner import native_artifact_signature, native_output_ready


class LayoutPreviewTest(unittest.TestCase):
    def test_frozen_compilation_validation_and_request_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_spec = {"schema_version": "case_spec_v2", "case_id": "layout"}
            config_digest = stable_digest({"map": "/Game/Test"})
            payloads = {
                "case_spec_v2.json": case_spec,
                "runtime_case.json": {
                    "schema_version": "harness_runtime_case_v2",
                    "case_id": "layout",
                },
                "backend_selection.json": {
                    "selected_backend": "ue",
                    "render_backend": "ue",
                },
                "camera_plan.json": {"views": []},
                "runtime_actor_placement.json": {"actor_bindings": []},
                "runtime_compilation_report.json": {
                    "status": "pass",
                    "compile_config_digest": config_digest,
                },
                "compilation_transaction.json": {
                    "state": "completed",
                    "transaction_id": "compilation_test",
                    "compile_config": {"map_package": "/Game/Test"},
                    "input_identity": {
                        "case_spec_digest": stable_digest(case_spec),
                        "compile_config_digest": config_digest,
                    },
                },
            }
            for name, value in payloads.items():
                write_json(root / name, value)
            compiled = validate_frozen_compilation(root)
            self.assertEqual(compiled["compilation_transaction.json"]["transaction_id"], "compilation_test")
            self.assertEqual(
                layout_preview_request_digest(
                    compilation_id="compilation_test",
                    compile_config_digest=config_digest,
                    time_s=0.0,
                ),
                layout_preview_request_digest(
                    compilation_id="compilation_test",
                    compile_config_digest=config_digest,
                    time_s=0.0,
                ),
            )

    def test_layout_preview_readiness_needs_manifest_and_images_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "layout_front.png"
            image.write_bytes(b"preview")
            write_json(
                root / "layout_preview_manifest.json",
                {"status": "pass", "images": [{"view_id": "front", "path": str(image)}]},
            )
            self.assertTrue(native_output_ready(root, layout_preview=True))
            self.assertIsNotNone(native_artifact_signature(root, layout_preview=True))
            self.assertFalse((root / "frames").exists())

    def test_replay_preview_applies_generic_transform_deltas_to_cached_states(self) -> None:
        runtime_scene = {
            "dynamic_objects": [
                {
                    "id": "vessel",
                    "initial_position_m": [1.0, 2.0, 3.0],
                    "params": {"base_rotation_degrees": [0.0, 10.0, 0.0]},
                },
                {
                    "id": "cloth_surface",
                    "params": {"surface_mesh_sequence": ["/Game/Test/Frame0"]},
                },
            ],
            "static_objects": [],
            "precomputed_trajectory": [
                {
                    "objects": {
                        "vessel": {
                            "position": [1.0, 2.0, 3.0],
                            "rotation_degrees": [0.0, 10.0, 0.0],
                        }
                    }
                }
            ],
        }
        preview = apply_runtime_transform_overrides(
            runtime_scene,
            offsets_m={"vessel": [0.0, -0.02, 0.01]},
            rotations_deg={"vessel": [0.0, 0.0, 180.0]},
        )
        self.assertEqual(preview["dynamic_objects"][0]["initial_position_m"], [1.0, 1.98, 3.01])
        self.assertEqual(preview["dynamic_objects"][0]["params"]["base_rotation_degrees"], [0.0, 10.0, 180.0])
        state = preview["precomputed_trajectory"][0]["objects"]["vessel"]
        self.assertEqual(state["position"], [1.0, 1.98, 3.01])
        self.assertEqual(state["rotation_degrees"], [0.0, 10.0, 180.0])
        self.assertEqual(runtime_scene["dynamic_objects"][0]["initial_position_m"], [1.0, 2.0, 3.0])
        vessel_params = preview["dynamic_objects"][0]["params"]
        self.assertTrue(vessel_params["generate_translucent_fluid_material"])
        self.assertFalse(vessel_params["generate_solid_material"])
        self.assertFalse(vessel_params["preserve_material"])
        surface_params = preview["dynamic_objects"][1]["params"]
        self.assertTrue(surface_params["generate_solid_material"])
        self.assertFalse(surface_params["generate_translucent_fluid_material"])

    def test_replay_preview_rejects_unknown_transform_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown object ids"):
            apply_runtime_transform_overrides(
                {"dynamic_objects": [], "static_objects": [], "precomputed_trajectory": []},
                offsets_m={"missing": [0.0, 0.0, 0.0]},
            )


if __name__ == "__main__":
    unittest.main()
