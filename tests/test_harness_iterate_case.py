from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import harness_iterate_case


def write_comparison_inputs(run_dir: Path, *, preset: str = "data_neutral", target_z: float = 0.1) -> None:
    for relative, payload in (
        ("case_spec.json", {"case_id": "domino", "scene": {"target_z": target_z}}),
        ("camera_plan.json", {"views": [{"camera_id": "event_closeup", "target": [0, 0, target_z]}]}),
        ("inputs/camera.json", {"views": [{"camera_id": "event_closeup", "target": [0, 0, target_z]}]}),
        ("runtime_actor_placement.json", {"actor_bindings": [{"object_id": "domino_0"}]}),
        ("inputs/render_config.json", {"width": 1280, "height": 720, "fps": 24}),
        ("inputs/scene.json", {"map": {"requested_package": "/Game/Maps/Day"}}),
        (
            "logs/studio_runtime_scene_rgb.json",
            {"map_lighting_controls": {"preset": "harness_rgb_editor_viewport"}},
        ),
        (
            "logs/studio_runtime_scene_data.json",
            {"map_lighting_controls": {"preset": preset}},
        ),
    ):
        path = run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


class HarnessIterateCaseTests(unittest.TestCase):
    def test_default_runs_every_preset_and_selects_highest_passing_score(self) -> None:
        result, runner = self.run_main([70.0, 90.0, 80.0])

        self.assertEqual(result["attempted"], 3)
        self.assertEqual(result["best_attempt"], 2)
        self.assertEqual(runner.call_count, 3)

    def test_stop_on_first_pass_is_explicit_opt_out(self) -> None:
        result, runner = self.run_main([70.0, 90.0, 80.0], stop_on_first_pass=True)

        self.assertEqual(result["attempted"], 1)
        self.assertEqual(runner.call_count, 1)

    def test_case_route_bundle_links_source_and_updates_case_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            group = root / "cases" / "rigid_collision" / "billiards" / "v003_speed"
            mp4 = b"\x00\x00\x00\x18ftypisom00000000"
            comparison_runs = []
            camera_ids = ("front_static", "side_static", "top_down", "tracking_subject", "event_closeup")
            for index in range(3):
                run_dir = group / "runs" / f"session_{index}" / "attempt_01" / "case_ue"
                (run_dir / "inputs").mkdir(parents=True)
                (run_dir / "inputs" / "camera.json").write_text(
                    json.dumps({"views": [{"camera_id": camera_id} for camera_id in camera_ids]}),
                    encoding="utf-8",
                )
                (run_dir / "inputs" / "render_config.json").write_text(
                    json.dumps({"width": 1280, "height": 720, "fps": 24, "duration_s": 1 / 24}),
                    encoding="utf-8",
                )
                (run_dir / "quality_report.json").write_text(
                    json.dumps(
                        {
                            "hard_gate_passed": True,
                            "ranking": {"technical_score": 88.5},
                            "source_reports": {
                                "run_readiness": {"backend": "ue", "ue_render_real": True},
                                "map_report": {"map_opened": True, "package_match": True},
                                "asset_resolution": {
                                    "selected_count": 1,
                                    "proxy_count": 0,
                                    "geometry_match": True,
                                },
                            },
                            "camera_motion": {
                                "views": {
                                    camera_id: {
                                        "moving": camera_id in {"tracking_subject", "event_closeup"},
                                        "mode": (
                                            "object_bound" if camera_id == "tracking_subject"
                                            else "trajectory" if camera_id == "event_closeup"
                                            else "fixed"
                                        ),
                                        "frame_count": 2,
                                    }
                                    for camera_id in camera_ids
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                for view_id in camera_ids:
                    view = run_dir / "views" / view_id
                    view.mkdir(parents=True)
                    for filename in ("rgb.mp4", "depth_preview.mp4", "segmentation_preview.mp4"):
                        (view / filename).write_bytes(mp4)
                    (view / "meta.json").write_text(json.dumps({"frame_count_rgb": 2}), encoding="utf-8")
                    for sequence in ("depth_frames", "segmentation_frames"):
                        (view / sequence).mkdir()
                        for frame in range(2):
                            (view / sequence / f"frame_{frame:06d}.exr").write_bytes(
                                b"\x76\x2f\x31\x01" + bytes([frame])
                            )
                comparison_runs.append(
                    {
                        "label": f"repeat_{index}",
                        "run_dir": run_dir,
                        "comparison_fingerprint": chr(ord("a") + index) * 64,
                        "condition": f"pitch_{-18 - index * 2}",
                    }
                )
            run_dir = Path(comparison_runs[-1]["run_dir"])
            old_staging = root / "review" / "inbox" / ".speed_case__ue__20260714T000000__attempt_01.staging"
            old_staging.mkdir(parents=True)
            (old_staging / "sentinel").write_text("do not delete", encoding="utf-8")

            def render_grid(_sources, target, **_kwargs):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(mp4)

            with patch("harness.core.artifact_manager.render_video_grid", side_effect=render_grid):
                bundle, videos = harness_iterate_case.publish_candidate_bundle(
                    publication_root=root / "review" / "inbox",
                    run_dir=run_dir,
                    group=group,
                    case_route="rigid_collision/billiards/v003_speed",
                    case_id="speed_case",
                    backend="ue",
                    timestamp="20260714T000000",
                    attempt=1,
                    review_role="review_candidate",
                    publication_tier="local_preview",
                    quality={"hard_gate_passed": True, "ranking": {"technical_score": 88.5}},
                    comparison_runs=comparison_runs,
                )

            self.assertIsNotNone(bundle)
            assert bundle is not None
            self.assertEqual(len(videos), 57)
            manifest = json.loads(next(bundle.glob("*.review.json")).read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_run"], str(run_dir.resolve()))
            self.assertEqual(manifest["overall"], {
                "rgb": "overall/rgb.mp4",
                "depth": "overall/depth.mp4",
                "segmentation": "overall/segmentation.mp4",
            })
            self.assertEqual(manifest["case_status"], str(group / "case_status.json"))
            self.assertEqual(manifest["review_role"], "review_candidate")
            self.assertEqual(manifest["publication_tier"], "local_preview")
            self.assertEqual(manifest["comparison_policy"], "declared_condition_matrix_v1")
            self.assertEqual(manifest["comparison_mode"], "declared_condition_matrix")
            self.assertEqual(len(manifest["source_runs"]), 3)
            self.assertTrue(all(row.get("overall") for row in manifest["source_runs"]))
            self.assertEqual(set(manifest["run_overall"]), {row["label"] for row in manifest["source_runs"]})
            status = json.loads((group / "case_status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "review_pending")
            self.assertEqual(status["review"]["candidate"], bundle.name)
            self.assertEqual(status["review"]["comparison_mode"], "declared_condition_matrix")
            self.assertEqual(
                status["review"]["conditions"],
                ["pitch_-18", "pitch_-20", "pitch_-22"],
            )
            self.assertEqual((old_staging / "sentinel").read_text(encoding="utf-8"), "do not delete")

    def test_run_index_keeps_distinct_sessions_and_rejects_duplicate_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            group = root / "cases" / "rigid_collision" / "billiards" / "v003_speed"
            index = harness_iterate_case.load_run_index(
                group / "run_index.json",
                case_route="rigid_collision/billiards/v003_speed",
            )
            for session_id in ("session_a", "session_b"):
                run_dir = group / "runs" / session_id / "attempt_01" / "case_ue"
                write_comparison_inputs(run_dir)
                (run_dir / "quality_report.json").write_text(
                    json.dumps({"hard_gate_passed": True}),
                    encoding="utf-8",
                )
                report = group / "runs" / session_id / "iteration_report.json"
                report.write_text("{}", encoding="utf-8")
                harness_iterate_case.register_session(
                    index,
                    session_id=session_id,
                    report_path=report,
                    group=group,
                    case_spec=root / "case.json",
                    candidates=[
                        {
                            "attempt": 1,
                            "lighting_preset": "data_neutral",
                            "run_dir": str(run_dir),
                            "quality": {"hard_gate_passed": True},
                        }
                    ],
                )

            comparison = harness_iterate_case.indexed_comparison_runs(index)
            self.assertEqual(len(comparison), 2)
            self.assertEqual({row["session_id"] for row in comparison}, {"session_a", "session_b"})
            self.assertEqual(len({row["run_dir"] for row in comparison}), 2)

            index["sessions"][0]["status"] = "publication_failed"
            active = harness_iterate_case.indexed_comparison_runs(index)
            self.assertEqual([row["session_id"] for row in active], ["session_b"])
            index["sessions"][0]["status"] = "rendered"

            index["sessions"][1]["passing_runs"][0]["run_dir"] = index["sessions"][0]["passing_runs"][0]["run_dir"]
            index["sessions"][1]["passing_runs"][0]["quality_report"] = index["sessions"][0]["passing_runs"][0]["quality_report"]
            with self.assertRaisesRegex(RuntimeError, "duplicate source run"):
                harness_iterate_case.indexed_comparison_runs(index)

    def test_incompatible_acquisition_is_rejected_before_run_index_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            case = root / "case.json"
            case.write_text("{}", encoding="utf-8")
            case_route = "rigid_motion/gravity_bounce_projectile/v001_test"
            group = workspace / "runs" / "case_routes" / Path(case_route)
            index_path = group / "run_index.json"
            existing_run = group / "runs" / "session_existing" / "attempt_01" / "case_ue"
            write_comparison_inputs(existing_run)
            for camera_id in ("front_static", "side_static", "top_down", "tracking_subject", "event_closeup"):
                view = existing_run / "views" / camera_id
                view.mkdir(parents=True)
                (view / "meta.json").write_text(json.dumps({"frame_count_rgb": 2}), encoding="utf-8")
            (existing_run / "quality_report.json").write_text(
                json.dumps({"hard_gate_passed": True}),
                encoding="utf-8",
            )
            existing_report = group / "runs" / "session_existing" / "iteration_report.json"
            existing_report.write_text("{}", encoding="utf-8")
            with harness_iterate_case.locked_run_index(index_path, case_route=case_route) as index:
                harness_iterate_case.register_session(
                    index,
                    session_id="session_existing",
                    report_path=existing_report,
                    group=group,
                    case_spec=case,
                    candidates=[{
                        "attempt": 1,
                        "lighting_preset": "neutral",
                        "condition": "baseline",
                        "run_dir": str(existing_run),
                        "quality": {"hard_gate_passed": True},
                    }],
                    selected_run_dir=existing_run,
                )
            index_before = index_path.read_bytes()

            def run_case(command, **_kwargs):
                output_root = Path(command[command.index("--output-root") + 1])
                run_dir = output_root / "case_ue"
                write_comparison_inputs(run_dir)
                (run_dir / "inputs" / "render_config.json").write_text(
                    json.dumps({"width": 1920, "height": 1080, "fps": 24}),
                    encoding="utf-8",
                )
                for camera_id in ("front_static", "side_static", "top_down", "tracking_subject", "event_closeup"):
                    view = run_dir / "views" / camera_id
                    view.mkdir(parents=True)
                    (view / "meta.json").write_text(json.dumps({"frame_count_rgb": 2}), encoding="utf-8")
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps({"status": "passed", "run_dir": str(run_dir)}),
                    stderr="",
                )

            def evaluate(run_dir, *, write):
                quality = {"hard_gate_passed": True, "ranking": {"technical_score": 90.0}}
                if write:
                    (Path(run_dir) / "quality_report.json").write_text(json.dumps(quality), encoding="utf-8")
                return quality

            argv = [
                "harness_iterate_case.py",
                str(case),
                "--case-route",
                case_route,
                "--lighting-presets",
                "neutral",
                "--condition",
                "wide_capture",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.dict(os.environ, {"SIM_HARNESS_WORKSPACE": str(workspace)}, clear=False),
                patch.object(harness_iterate_case, "new_session_id", return_value="session_incompatible"),
                patch.object(harness_iterate_case.subprocess, "run", side_effect=run_case),
                patch.object(harness_iterate_case, "evaluate_run", side_effect=evaluate),
                self.assertRaisesRegex(RuntimeError, "incompatible with case route"),
            ):
                harness_iterate_case.main()

            self.assertEqual(index_path.read_bytes(), index_before)
            index_after = harness_iterate_case.load_run_index(index_path, case_route=case_route)
            self.assertEqual([row["session_id"] for row in index_after["sessions"]], ["session_existing"])

    def test_run_index_compares_only_exact_input_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            group = root / "cases" / "rigid_collision" / "domino" / "v001"
            index = harness_iterate_case.load_run_index(
                group / "run_index.json",
                case_route="rigid_collision/domino/v001",
            )

            def register(session_id: str, target_z: float, preset: str = "data_neutral") -> str:
                run_dir = group / "runs" / session_id / "attempt_01" / "case_ue"
                write_comparison_inputs(run_dir, preset=preset, target_z=target_z)
                (run_dir / "quality_report.json").write_text(json.dumps({"hard_gate_passed": True}), encoding="utf-8")
                report = group / "runs" / session_id / "iteration_report.json"
                report.write_text("{}", encoding="utf-8")
                harness_iterate_case.register_session(
                    index,
                    session_id=session_id,
                    report_path=report,
                    group=group,
                    case_spec=root / "case.json",
                    candidates=[{
                        "attempt": 1,
                        "lighting_preset": preset,
                        "run_dir": str(run_dir),
                        "quality": {"hard_gate_passed": True},
                    }],
                )
                return harness_iterate_case.comparison_input_fingerprint(run_dir)

            first = register("session_a", 0.1)
            register("session_b", 0.6)
            matching = register("session_c", 0.1)
            register("session_d", 0.1, "cinematic_subject_key_fill")

            comparison = harness_iterate_case.indexed_comparison_runs(
                index,
                comparison_fingerprint=first,
            )

            self.assertEqual(first, matching)
            self.assertEqual({row["session_id"] for row in comparison}, {"session_a", "session_c"})
            self.assertTrue(all(row["comparison_fingerprint"] == first for row in comparison))

            drifted = group / "runs" / "session_a" / "attempt_01" / "case_ue" / "camera_plan.json"
            drifted.write_text(json.dumps({"views": [{"camera_id": "event_closeup", "target": [1, 2, 3]}]}))
            with self.assertRaisesRegex(RuntimeError, "fingerprint drifted after indexing"):
                harness_iterate_case.indexed_comparison_runs(index, comparison_fingerprint=first)

    def test_comparison_fingerprint_supports_single_pass_combined_runtime_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "combined_run"
            write_comparison_inputs(run_dir, preset="data_neutral", target_z=0.1)
            rgb = run_dir / "logs" / "studio_runtime_scene_rgb.json"
            data = run_dir / "logs" / "studio_runtime_scene_data.json"
            combined = run_dir / "logs" / "studio_runtime_scene_combined.json"
            rgb.rename(combined)
            data.unlink()

            fingerprint = harness_iterate_case.comparison_input_fingerprint(run_dir)

            self.assertEqual(len(fingerprint), 64)

    def test_locked_run_index_prevents_lost_same_process_concurrent_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            group = root / "cases" / "rigid_collision" / "billiards" / "v003_speed"
            index_path = group / "run_index.json"
            case_route = "rigid_collision/billiards/v003_speed"
            first_entered = threading.Event()
            release_first = threading.Event()
            second_attempted = threading.Event()
            second_entered = threading.Event()
            errors: list[BaseException] = []

            def register(session_id: str, *, hold: bool = False) -> None:
                try:
                    report = group / "runs" / session_id / "iteration_report.json"
                    report.parent.mkdir(parents=True)
                    report.write_text("{}", encoding="utf-8")
                    if not hold:
                        second_attempted.set()
                    with harness_iterate_case.locked_run_index(index_path, case_route=case_route) as index:
                        if hold:
                            first_entered.set()
                        else:
                            second_entered.set()
                        harness_iterate_case.register_session(
                            index,
                            session_id=session_id,
                            report_path=report,
                            group=group,
                            case_spec=root / "case.json",
                            candidates=[],
                        )
                        if hold and not release_first.wait(2):
                            raise TimeoutError("test did not release the first run-index transaction")
                except BaseException as exc:
                    errors.append(exc)

            first = threading.Thread(target=register, args=("session_a",), kwargs={"hold": True})
            second = threading.Thread(target=register, args=("session_b",))
            first.start()
            self.assertTrue(first_entered.wait(1))
            second.start()
            self.assertTrue(second_attempted.wait(1))
            self.assertFalse(second_entered.wait(0.1), "second writer entered while the first held the lock")
            release_first.set()
            first.join(2)
            second.join(2)

            self.assertFalse(first.is_alive() or second.is_alive(), "run-index lock deadlocked within one process")
            self.assertEqual(errors, [])
            index = harness_iterate_case.load_run_index(index_path, case_route=case_route)
            self.assertEqual({row["session_id"] for row in index["sessions"]}, {"session_a", "session_b"})

    def test_formal_iteration_rejects_non_ue_backend_before_running(self) -> None:
        with self.assertRaisesRegex(SystemExit, "supports only --backend ue"):
            harness_iterate_case.validate_complete_delivery_request(
                "top_down,event_closeup",
                "rgb,depth,segmentation",
                backend="fallback",
                mode="both",
            )

    def test_formal_iteration_requires_three_canonical_static_and_two_moving_cameras(self) -> None:
        with self.assertRaisesRegex(SystemExit, "at least five camera views"):
            harness_iterate_case.validate_complete_delivery_request(
                "top_down,event_closeup",
                "rgb,depth,segmentation",
                backend="ue",
                mode="both",
            )
        with self.assertRaisesRegex(SystemExit, "missing side_static"):
            harness_iterate_case.validate_complete_delivery_request(
                "front_static,top_down,tracking_subject,event_closeup,extra_static",
                "rgb,depth,segmentation",
                backend="ue",
                mode="both",
            )
        with self.assertRaisesRegex(SystemExit, "missing tracking_subject"):
            harness_iterate_case.validate_complete_delivery_request(
                "front_static,side_static,top_down,event_closeup,extra_moving",
                "rgb,depth,segmentation",
                backend="ue",
                mode="both",
            )
        harness_iterate_case.validate_complete_delivery_request(
            "front_static,side_static,top_down,tracking_subject,event_closeup",
            "rgb,depth,segmentation",
            backend="ue",
            mode="both",
        )

    def test_formal_iteration_requires_a_stable_case_route(self) -> None:
        with patch.object(sys, "argv", ["harness_iterate_case.py", "case.json"]), self.assertRaisesRegex(
            SystemExit,
            "requires --case-route",
        ):
            harness_iterate_case.main()

    def test_publication_rolls_back_bundle_if_case_status_update_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "source"
            run_dir.mkdir()
            publication_root = root / "review" / "inbox"
            group = root / "cases" / "rigid_collision" / "billiards" / "v003_speed"

            def publish_complete(_runs, staging, **_kwargs):
                video = Path(staging) / "overall" / "rgb.mp4"
                video.parent.mkdir(parents=True)
                video.write_bytes(b"\x00\x00\x00\x18ftypisom")
                return {
                    "runs": [],
                    "contract": {"per_view_modalities": ["rgb", "depth", "segmentation"]},
                    "layout": {},
                    "views": ["top_down", "event_closeup"],
                    "overall": {"rgb": "overall/rgb.mp4"},
                    "videos": [{"file": "overall/rgb.mp4", "sha256": harness_iterate_case.file_sha256(video)}],
                }

            with (
                patch.object(harness_iterate_case, "publish_complete_case_delivery", side_effect=publish_complete),
                patch.object(harness_iterate_case, "update_case_status_for_review", side_effect=OSError("disk full")),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                harness_iterate_case.publish_candidate_bundle(
                    publication_root=publication_root,
                    run_dir=run_dir,
                    group=group,
                    case_route="rigid_collision/billiards/v003_speed",
                    case_id="speed_case",
                    backend="ue",
                    timestamp="session_a",
                    attempt=1,
                    review_role="review_candidate",
                    publication_tier="local_preview",
                    quality={"hard_gate_passed": True},
                    comparison_runs=[{"label": "session_a", "run_dir": run_dir}],
                )

            self.assertFalse((publication_root / "speed_case__ue__session_a__attempt_01").exists())
            self.assertEqual(list(publication_root.glob(".*.staging-*")), [])

    def run_main(self, scores: list[float], *, stop_on_first_pass: bool = False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = root / "case.json"
            case.write_text("{}", encoding="utf-8")
            scores_iter = iter(scores)

            def run_case(command, **_kwargs):
                output_root = Path(command[command.index("--output-root") + 1])
                run_dir = output_root / "case_ue"
                (run_dir / "views").mkdir(parents=True)
                write_comparison_inputs(run_dir, preset="neutral")
                camera_ids = ("front_static", "side_static", "top_down", "tracking_subject", "event_closeup")
                (run_dir / "inputs" / "camera.json").write_text(
                    json.dumps({"views": [{"camera_id": camera_id} for camera_id in camera_ids]}),
                    encoding="utf-8",
                )
                for camera_id in camera_ids:
                    view = run_dir / "views" / camera_id
                    view.mkdir()
                    (view / "meta.json").write_text(json.dumps({"frame_count_rgb": 2}), encoding="utf-8")
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=json.dumps({"status": "passed", "run_dir": str(run_dir)}),
                    stderr="",
                )

            def evaluate(run_dir, *, write):
                quality = {
                    "hard_gate_passed": True,
                    "hard_gate": {"failure_count": 0},
                    "ranking": {"technical_score": next(scores_iter)},
                }
                if write:
                    (Path(run_dir) / "quality_report.json").write_text(json.dumps(quality), encoding="utf-8")
                return quality
            argv = [
                "harness_iterate_case.py",
                str(case),
                "--case-route",
                "rigid_collision/domino/v001_test",
                "--lighting-presets",
                "neutral,neutral,neutral",
                "--condition",
                "nominal",
            ]
            if stop_on_first_pass:
                argv.append("--stop-on-first-pass")
            output = io.StringIO()
            def publish_complete(runs, video_root):
                video = Path(video_root) / "overall" / "rgb.mp4"
                video.parent.mkdir(parents=True, exist_ok=True)
                video.write_bytes(b"\x00\x00\x00\x18ftypisom")
                return {
                    "runs": [
                        {"label": row["label"], "source_run": str(Path(row["run_dir"]).resolve())}
                        for row in runs
                    ],
                    "contract": {"per_view_modalities": ["rgb", "depth", "segmentation"]},
                    "layout": {"columns": [row["label"] for row in runs], "rows": ["top_down", "event_closeup"]},
                    "views": ["top_down", "event_closeup"],
                    "overall": {"rgb": "overall/rgb.mp4"},
                    "videos": [{"file": "overall/rgb.mp4", "sha256": harness_iterate_case.file_sha256(video)}],
                }

            with (
                patch.object(sys, "argv", argv),
                patch.dict(os.environ, {"SIM_HARNESS_WORKSPACE": str(root / "workspace")}, clear=False),
                patch.object(harness_iterate_case, "new_session_id", return_value="session_test"),
                patch.object(harness_iterate_case.subprocess, "run", side_effect=run_case) as runner,
                patch.object(harness_iterate_case, "evaluate_run", side_effect=evaluate),
                patch.object(harness_iterate_case, "publish_complete_case_delivery", side_effect=publish_complete) as publish,
                redirect_stdout(output),
            ):
                code = harness_iterate_case.main()
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["publication_tier"], "unverified")
            self.assertEqual(result["review_role"], "comparison_pending")
            self.assertEqual(result["comparison_run_count"], 1)
            self.assertEqual(result["condition"], "nominal")
            self.assertIsNone(result["review_bundle"])
            self.assertEqual(publish.call_count, 0)
            index = json.loads(
                (
                    root
                    / "workspace"
                    / "runs"
                    / "case_routes"
                    / "rigid_collision"
                    / "domino"
                    / "v001_test"
                    / "run_index.json"
                ).read_text(encoding="utf-8")
            )
            selected = index["sessions"][0]["passing_runs"]
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["attempt"], 1 if stop_on_first_pass else 2)
            self.assertEqual(selected[0]["condition"], "nominal")
            for call in runner.call_args_list:
                command = call.args[0]
                attempt_root = Path(command[command.index("--output-root") + 1])
                self.assertEqual(attempt_root.parent.name, "session_test")
                staging = Path(command[command.index("--video-root") + 1])
                self.assertEqual(staging.name, "_unvalidated_review")
                self.assertFalse(staging.exists())
            return result, runner


if __name__ == "__main__":
    unittest.main()
