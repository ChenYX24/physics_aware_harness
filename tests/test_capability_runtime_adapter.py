from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.capability_runtime_adapter import CapabilityRuntimeAdapter, resolve_runtime_output_dir, verify_capability_run


class CapabilityRuntimeAdapterTests(unittest.TestCase):
    def test_resolves_ue_output_before_debug_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run_a"
            (run_dir / "debug_preview").mkdir(parents=True)
            (run_dir / "ue_output").mkdir(parents=True)
            write_json(run_dir / "debug_preview" / "summary.json", {})
            write_json(run_dir / "ue_output" / "trajectory.json", [])
            self.assertEqual(resolve_runtime_output_dir(run_dir), run_dir / "ue_output")

    def test_adapter_converts_ue_billiard_trace_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = write_billiard_run(Path(tmp), pre_contact_target_velocity=[0.0, 0.0, 0.0])
            result = verify_capability_run(run_dir)
            report = result["verifier_report"]
            execution = result["execution_trace"]
            self.assertEqual(result["capability_plan"]["primary_capability_id"], "rigid_body_dynamics")
            self.assertEqual(execution["render_evidence"]["source_type"], "UE")
            self.assertTrue(report["capability_ready"])
            self.assertTrue(report["reference_video_ready"])
            self.assertTrue((run_dir / "capability_verifier.json").exists())

    def test_adapter_rejects_runtime_pre_contact_passive_motion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = write_billiard_run(Path(tmp), pre_contact_target_velocity=[0.2, 0.0, 0.0])
            result = CapabilityRuntimeAdapter().verify_run(run_dir)
            report = result["verifier_report"]
            self.assertFalse(report["capability_ready"])
            self.assertEqual(report["primary_failure_type"], "F4_causality_violation")

    def test_adapter_ignores_historical_process_wording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = write_falling_run(Path(tmp))
            result = CapabilityRuntimeAdapter().verify_run(run_dir)
            self.assertEqual(result["capability_plan"]["primary_capability_id"], "rigid_body_dynamics")
            self.assertTrue(result["verifier_report"]["capability_ready"])


def write_billiard_run(root: Path, *, pre_contact_target_velocity: list[float]) -> Path:
    run_dir = root / "billiards_run"
    output_dir = run_dir / "ue_output"
    output_dir.mkdir(parents=True)
    write_json(
        run_dir / "spec.json",
        {
            "schema_version": "simulator_scene_spec_v1",
            "prompt": "A pool table scene where one cue ball hits passive target balls.",
            "scene": {
                "objects": [
                    {"id": "cue_ball", "dynamic": True, "role": "active striker", "initial_position_m": [-1.0, 0.0, 0.0], "initial_velocity_m_s": [1.0, 0.0, 0.0]},
                    {"id": "target_ball", "dynamic": True, "role": "passive target", "initial_position_m": [0.0, 0.0, 0.0], "initial_velocity_m_s": [0.0, 0.0, 0.0]},
                ],
            },
            "verification_assertions": [
                {"id": "contact", "type": "event_exists", "event": "contact", "objects": ["cue_ball", "target_ball"]},
                {"id": "initial_target_speed", "type": "state_value", "object_id": "target_ball", "field": "velocity_m_s.x", "reduction": "initial", "operator": "<=", "value": 0.01},
            ],
        },
    )
    write_json(
        output_dir / "summary.json",
        {
            "native_ue": True,
            "frames": 3,
            "fps": 24,
            "physics_capture": {"trajectory_source": "adp_cpp_runtime_driver"},
        },
    )
    write_json(output_dir / "run_readiness.json", {"passed": True, "reference_ready": True, "physics_ready": True, "visual_ready": True})
    write_json(
        output_dir / "render_pass_manifest.json",
        {
            "schema_version": "render_pass_manifest_v1",
            "passes": {
                "rgb": {"status": "available"},
                "depth": {"status": "available"},
                "normal": {"status": "available"},
                "audio": {"status": "available"},
            },
            "sync": {"camera_trajectory": "camera_trajectories.json"},
        },
    )
    (output_dir / "preview.mp4").write_bytes(b"video")
    write_json(
        output_dir / "trajectory.json",
        [
            {
                "frame": 0,
                "time": 0.0,
                "objects": {
                    "cue_ball": {"position": [-1.0, 0.0, 0.0], "velocity_cm_s": [100.0, 0.0, 0.0]},
                    "target_ball": {"position": [0.0, 0.0, 0.0], "velocity_m_s": pre_contact_target_velocity},
                },
                "contacts": [],
            },
            {
                "frame": 1,
                "time": 0.1,
                "objects": {
                    "cue_ball": {"position": [-0.1, 0.0, 0.0], "velocity_cm_s": [50.0, 0.0, 0.0]},
                    "target_ball": {"position": [0.03, 0.0, 0.0], "velocity_cm_s": [35.0, 0.0, 0.0]},
                },
                "contacts": [{"objects": ["cue_ball", "target_ball"], "time": 0.1, "frame": 1}],
            },
        ],
    )
    return run_dir


def write_falling_run(root: Path) -> Path:
    run_dir = root / "falling_run"
    output_dir = run_dir / "ue_output"
    output_dir.mkdir(parents=True)
    write_json(
        run_dir / "spec.json",
        {
            "schema_version": "simulator_scene_spec_v1",
            "prompt": "Falling blocks under gravity collide with the floor.",
            "scene": {
                "objects": [
                    {"id": "falling_block", "dynamic": True, "role": "falling body", "initial_position_m": [0.0, 0.0, 1.2]},
                    {"id": "floor", "dynamic": False, "role": "support floor", "initial_position_m": [0.0, 0.0, 0.0]},
                ],
            },
            "verification_assertions": [
                {"id": "descent", "type": "state_delta", "object_id": "falling_block", "field": "position_m.z", "operator": "<=", "value": -0.5},
                {"id": "contact", "type": "event_exists", "event": "contact", "objects": ["falling_block", "floor"]},
            ],
        },
    )
    write_json(output_dir / "summary.json", {"native_ue": True, "physics_capture": {"trajectory_source": "adp_cpp_runtime_driver"}})
    write_json(output_dir / "run_readiness.json", {"passed": True, "reference_ready": True, "physics_ready": True, "visual_ready": True})
    write_json(output_dir / "render_pass_manifest.json", {"passes": {"rgb": {"status": "available"}}})
    write_json(
        output_dir / "trajectory.json",
        [
            {"frame": 0, "time": 0.0, "objects": {"falling_block": {"position": [0, 0, 1.2], "velocity_cm_s": [0, 0, 0]}, "floor": {"position": [0, 0, 0], "velocity_cm_s": [0, 0, 0]}}, "contacts": []},
            {"frame": 1, "time": 0.2, "objects": {"falling_block": {"position": [0, 0, 0.5], "velocity_cm_s": [0, 0, -220]}, "floor": {"position": [0, 0, 0], "velocity_cm_s": [0, 0, 0]}}, "contacts": []},
            {"frame": 2, "time": 0.4, "objects": {"falling_block": {"position": [0, 0, 0.1], "velocity_cm_s": [0, 0, 0]}, "floor": {"position": [0, 0, 0], "velocity_cm_s": [0, 0, 0]}}, "contacts": [{"objects": ["falling_block", "floor"], "time": 0.4, "frame": 2}]},
        ],
    )
    return run_dir


def write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
