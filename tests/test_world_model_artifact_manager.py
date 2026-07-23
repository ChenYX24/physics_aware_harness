from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.core.artifact_manager import (
    ArtifactManager,
    DeliveryError,
    publish_complete_case_delivery,
    publish_diagnostic_case_delivery,
    render_video_grid,
)
from harness.core.artifact_schema import read_json, write_json


MP4 = b"\x00\x00\x00\x18ftypisom00000000"
FINGERPRINT = "a" * 64


def write_delivery_run(run_dir: Path, *, frame_count: int = 2, hard_gate_passed: bool = True) -> None:
    write_json(
        run_dir / "inputs" / "render_config.json",
        {
            "width": 1280,
            "height": 720,
            "fps": 24,
            "duration_s": (frame_count - 1) / 24,
            "passes": ["rgb", "depth", "segmentation"],
        },
    )
    write_json(
        run_dir / "inputs" / "camera.json",
        {
            "views": [
                {"camera_id": "front_static"},
                {"camera_id": "side_static"},
                {"camera_id": "top_down"},
                {"camera_id": "tracking_subject"},
                {"camera_id": "event_closeup"},
            ]
        },
    )
    write_json(
        run_dir / "quality_report.json",
        {
            "hard_gate_passed": hard_gate_passed,
            "ranking": {"technical_score": 80},
            "source_reports": {
                "run_readiness": {"backend": "ue", "ue_render_real": True},
                "map_report": {"map_opened": True, "package_match": True},
                "asset_resolution": {"selected_count": 1, "proxy_count": 0, "geometry_match": True},
            },
            "camera_motion": {
                "views": {
                    "front_static": {
                        "camera_mode": "fixed",
                        "frame_count": frame_count,
                        "moving": False,
                    },
                    "side_static": {
                        "camera_mode": "fixed",
                        "frame_count": frame_count,
                        "moving": False,
                    },
                    "top_down": {
                        "camera_mode": "fixed",
                        "frame_count": frame_count,
                        "moving": False,
                    },
                    "tracking_subject": {
                        "camera_mode": "object_bound",
                        "frame_count": frame_count,
                        "moving": True,
                    },
                    "event_closeup": {
                        "camera_mode": "trajectory",
                        "frame_count": frame_count,
                        "moving": True,
                    },
                }
            },
        },
    )
    for view in ("front_static", "side_static", "top_down", "tracking_subject", "event_closeup"):
        view_dir = run_dir / "views" / view
        view_dir.mkdir(parents=True)
        write_json(view_dir / "meta.json", {"frame_count_rgb": frame_count})
        for filename in ("rgb.mp4", "depth_preview.mp4", "segmentation_preview.mp4"):
            (view_dir / filename).write_bytes(MP4)
        for sequence in ("depth_frames", "segmentation_frames"):
            (view_dir / sequence).mkdir()
            for frame in range(frame_count):
                (view_dir / sequence / f"frame_{frame:06d}.exr").write_bytes(
                    b"\x76\x2f\x31\x01" + bytes([frame])
                )


class WorldModelArtifactManagerTests(unittest.TestCase):
    def test_video_grid_uses_legacy_compatible_xstack_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [root / "front.mp4", root / "event.mp4"]
            for source in sources:
                source.write_bytes(MP4)
            target = root / "overall.mp4"

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(MP4)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with patch("harness.core.artifact_manager.subprocess.run", side_effect=fake_run) as runner:
                render_video_grid(sources, target, columns=2, rows=1)

            command = runner.call_args.args[0]
            filter_graph = command[command.index("-filter_complex") + 1]
            self.assertIn("xstack=inputs=2:layout=0_0|640_0:shortest=1", filter_graph)
            self.assertNotIn(":fill=", filter_graph)
            self.assertTrue(target.is_file())

    def test_one_run_publish_creates_and_exports_three_multiview_overalls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run_01" / "case_ue"
            write_delivery_run(run_dir)

            def render_grid(_sources, target, **_kwargs):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(MP4)

            with patch("harness.core.artifact_manager.render_video_grid", side_effect=render_grid) as render:
                published = ArtifactManager(run_dir).publish_videos(
                    root / "review",
                    case_id="case",
                    backend="ue",
                )

            self.assertEqual(render.call_count, 3)
            self.assertEqual(len(published), 18)
            self.assertTrue((run_dir / "overall" / "rgb.mp4").is_file())
            self.assertTrue((run_dir / "overall" / "depth.mp4").is_file())
            self.assertTrue((run_dir / "overall" / "segmentation.mp4").is_file())
            manifest = read_json(run_dir / "overall" / "manifest.json")
            self.assertEqual(manifest["views"], [
                "front_static", "side_static", "top_down", "tracking_subject", "event_closeup"
            ])
            self.assertEqual(manifest["layout"]["tile_resolution"], [640, 360])

    def test_diagnostic_delivery_is_manifested_but_never_reference_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = []
            for index in range(3):
                run_dir = root / f"run_{index}"
                write_delivery_run(run_dir, hard_gate_passed=False)
                runs.append({
                    "label": f"energy_{index}",
                    "condition": f"energy_{index}",
                    "run_dir": run_dir,
                    "comparison_fingerprint": chr(ord("a") + index) * 64,
                })

            def render_grid(_sources, target, **_kwargs):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(MP4)

            destination = root / "diagnostic"
            with patch("harness.core.artifact_manager.render_video_grid", side_effect=render_grid):
                delivery = publish_diagnostic_case_delivery(
                    runs,
                    destination,
                    known_limitations=["native fragment transforms are not exported"],
                )

            self.assertEqual(delivery["review_role"], "diagnostic_probe")
            self.assertEqual(delivery["publication_tier"], "unverified")
            self.assertFalse(delivery["reference_ready"])
            self.assertEqual(delivery["known_limitations"], ["native fragment transforms are not exported"])
            self.assertFalse(delivery["runs"][0]["hard_gate_passed"])
            self.assertTrue((destination / "delivery_manifest.json").is_file())
            with self.assertRaisesRegex(DeliveryError, "cannot use the reference publication tier"):
                publish_diagnostic_case_delivery(
                    runs,
                    root / "invalid_reference_diagnostic",
                    known_limitations=["native fragment transforms are not exported"],
                    publication_tier="reference",
                )

    def test_complete_delivery_contains_each_run_view_modality_and_run_and_case_comparison_overalls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = []
            for index in (1, 2, 3):
                run_dir = root / f"run_{index}"
                write_delivery_run(run_dir)
                runs.append({
                    "label": f"repeat_{index}",
                    "run_dir": run_dir,
                    "attempt": index,
                    "comparison_fingerprint": FINGERPRINT,
                })

            def render_grid(_sources, target, **_kwargs):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(MP4)

            with patch("harness.core.artifact_manager.render_video_grid", side_effect=render_grid) as render:
                delivery = publish_complete_case_delivery(runs, root / "delivery")

            self.assertEqual(render.call_count, 12)
            self.assertEqual(delivery["layout"]["columns"], ["repeat_1", "repeat_2", "repeat_3"])
            self.assertEqual(delivery["layout"]["rows"], ["event_closeup"])
            self.assertEqual(delivery["layout"]["comparison_view"], "event_closeup")
            self.assertEqual(delivery["layout"]["tile_resolution"], [640, 360])
            self.assertEqual(delivery["review_role"], "review_candidate")
            self.assertEqual(delivery["publication_tier"], "unverified")
            self.assertFalse(delivery["reference_ready"])
            for call in render.call_args_list[-3:]:
                self.assertEqual(len(call.args[0]), 3)
                self.assertTrue(
                    all(path.stem == "event_closeup" or "event_closeup" in path.parts for path in call.args[0])
                )
                self.assertEqual(call.kwargs["rows"], 1)
            self.assertEqual(delivery["overall"], {
                "rgb": "overall/rgb.mp4",
                "depth": "overall/depth.mp4",
                "segmentation": "overall/segmentation.mp4",
            })
            self.assertEqual(len(delivery["videos"]), 57)
            self.assertEqual(sum(row["role"] == "overall" for row in delivery["videos"]), 3)
            self.assertEqual(sum(row["role"] == "run_overall" for row in delivery["videos"]), 9)
            self.assertTrue((root / "delivery" / "variants" / "repeat_1" / "rgb" / "event_closeup.mp4").is_file())
            self.assertTrue(
                (
                    root
                    / "delivery"
                    / "variants"
                    / "repeat_1"
                    / "depth"
                    / "event_closeup"
                    / "frames"
                    / "frame_000000.exr"
                ).is_file()
            )
            self.assertEqual(delivery["runs"][0]["overall"], {
                "rgb": "variants/repeat_1/overall/rgb.mp4",
                "depth": "variants/repeat_1/overall/depth.mp4",
                "segmentation": "variants/repeat_1/overall/segmentation.mp4",
            })
            self.assertTrue((root / "delivery" / "variants" / "repeat_1" / "overall" / "rgb.mp4").is_file())
            self.assertEqual(delivery["run_overall"]["repeat_1"], delivery["runs"][0]["overall"])
            truth = delivery["runs"][0]["source_truth"]["event_closeup"]["depth"]
            self.assertEqual(truth["frame_count"], 2)
            self.assertRegex(truth["aggregate_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(delivery["contract"]["modality_truth"]["rgb"]["source"], "native_ue_capture")
            self.assertEqual(delivery["contract"]["minimum_source_run_count"], 3)
            self.assertEqual(delivery["contract"]["minimum_static_camera_count"], 3)
            self.assertEqual(delivery["contract"]["minimum_moving_camera_count"], 2)
            self.assertEqual(delivery["contract"]["comparison_mode"], "exact_repeat")
            self.assertEqual(delivery["contract"]["case_overall_comparison_view"], "event_closeup")

    def test_complete_delivery_rejects_non_ue_and_proxy_only_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = []
            for index in range(3):
                run_dir = root / f"run_{index}"
                write_delivery_run(run_dir)
                runs.append({"run_dir": run_dir, "comparison_fingerprint": FINGERPRINT})

            quality_path = Path(runs[0]["run_dir"]) / "quality_report.json"
            quality = read_json(quality_path)
            quality["source_reports"]["run_readiness"]["backend"] = "genesis"
            write_json(quality_path, quality)
            with self.assertRaisesRegex(DeliveryError, "native UE RGB provenance"):
                publish_complete_case_delivery(runs, root / "non_ue")

            quality["source_reports"]["run_readiness"]["backend"] = "ue"
            quality["source_reports"]["asset_resolution"] = {"selected_count": 1, "proxy_count": 1}
            write_json(quality_path, quality)
            with self.assertRaisesRegex(DeliveryError, "non-proxy 3D asset"):
                publish_complete_case_delivery(runs, root / "proxy_only")

            quality["source_reports"]["asset_resolution"] = {
                "selected_count": 1,
                "proxy_count": 0,
                "geometry_match": False,
            }
            write_json(quality_path, quality)
            with self.assertRaisesRegex(DeliveryError, "does not match solver geometry"):
                publish_complete_case_delivery(runs, root / "geometry_mismatch")

    def test_complete_delivery_rejects_duplicate_source_and_invalid_camera_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            write_delivery_run(run_dir)

            with self.assertRaisesRegex(DeliveryError, "duplicate resolved source run"):
                publish_complete_case_delivery(
                    [
                        {"label": "one", "run_dir": run_dir, "comparison_fingerprint": FINGERPRINT},
                        {"label": "two", "run_dir": run_dir / ".", "comparison_fingerprint": FINGERPRINT},
                        {"label": "three", "run_dir": root / "missing", "comparison_fingerprint": FINGERPRINT},
                    ],
                    root / "duplicate_delivery",
                )

            camera_runs = []
            for index in range(3):
                camera_run = root / f"camera_run_{index}"
                write_delivery_run(camera_run)
                camera_runs.append({
                    "label": f"repeat_{index}",
                    "run_dir": camera_run,
                    "comparison_fingerprint": FINGERPRINT,
                })
            quality_path = Path(camera_runs[0]["run_dir"]) / "quality_report.json"
            quality = read_json(quality_path)
            quality["camera_motion"]["views"]["event_closeup"]["camera_mode"] = "fixed"
            write_json(quality_path, quality)
            with self.assertRaisesRegex(DeliveryError, "mode/moving truth is inconsistent"):
                publish_complete_case_delivery(
                    camera_runs,
                    root / "bad_mode",
                )

            quality["camera_motion"]["views"]["event_closeup"]["camera_mode"] = "trajectory"
            quality["camera_motion"]["views"]["event_closeup"]["frame_count"] = 3
            write_json(quality_path, quality)
            with self.assertRaisesRegex(DeliveryError, "frame counts must match"):
                publish_complete_case_delivery(
                    camera_runs,
                    root / "bad_count",
                )

    def test_complete_delivery_requires_three_runs_and_declares_different_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = []
            for index in range(3):
                run_dir = root / f"run_{index}"
                write_delivery_run(run_dir)
                runs.append({
                    "label": f"repeat_{index}",
                    "run_dir": run_dir,
                    "comparison_fingerprint": FINGERPRINT,
                })

            with self.assertRaisesRegex(DeliveryError, "at least 3 source runs"):
                publish_complete_case_delivery(runs[:2], root / "too_few_repeats")

            mismatched = [dict(row) for row in runs]
            mismatched[-1]["comparison_fingerprint"] = "b" * 64
            with self.assertRaisesRegex(DeliveryError, "explicit condition"):
                publish_complete_case_delivery(mismatched, root / "mismatched_inputs")

            aliased = [dict(row, condition=f"pitch_{index}") for index, row in enumerate(runs)]
            with self.assertRaisesRegex(DeliveryError, "cannot use multiple declared condition labels"):
                publish_complete_case_delivery(aliased, root / "aliased_exact_inputs")

            for index, row in enumerate(mismatched):
                row["condition"] = f"initial_pitch_{-18 - index * 2}deg"
                row["comparison_fingerprint"] = chr(ord("a") + index) * 64

            def render_grid(_sources, target, **_kwargs):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(MP4)

            with patch("harness.core.artifact_manager.render_video_grid", side_effect=render_grid):
                delivery = publish_complete_case_delivery(mismatched, root / "condition_matrix")
            self.assertEqual(delivery["contract"]["comparison_mode"], "declared_condition_matrix")
            self.assertEqual(delivery["contract"]["condition_count"], 3)

            drifted_render = read_json(Path(mismatched[-1]["run_dir"]) / "inputs" / "render_config.json")
            drifted_render["width"] = 1920
            write_json(Path(mismatched[-1]["run_dir"]) / "inputs" / "render_config.json", drifted_render)
            with self.assertRaisesRegex(DeliveryError, "camera/render acquisition context"):
                publish_complete_case_delivery(mismatched, root / "mismatched_acquisition")
            drifted_render["width"] = 1280
            write_json(Path(mismatched[-1]["run_dir"]) / "inputs" / "render_config.json", drifted_render)

            drifted_camera_path = Path(mismatched[-1]["run_dir"]) / "inputs" / "camera.json"
            drifted_camera = read_json(drifted_camera_path)
            event_camera = next(view for view in drifted_camera["views"] if view["camera_id"] == "event_closeup")
            event_camera["subject_follow_location_gain"] = 0.2
            write_json(drifted_camera_path, drifted_camera)
            with self.assertRaisesRegex(DeliveryError, "camera/render acquisition context"):
                publish_complete_case_delivery(mismatched, root / "mismatched_camera_profile")
            event_camera.pop("subject_follow_location_gain")
            write_json(drifted_camera_path, drifted_camera)

            frame_mismatch = Path(mismatched[-1]["run_dir"])
            frame_quality = read_json(frame_mismatch / "quality_report.json")
            for motion in frame_quality["camera_motion"]["views"].values():
                motion["frame_count"] = 3
            write_json(frame_mismatch / "quality_report.json", frame_quality)
            for view in frame_quality["camera_motion"]["views"]:
                meta = read_json(frame_mismatch / "views" / view / "meta.json")
                meta["frame_count_rgb"] = 3
                write_json(frame_mismatch / "views" / view / "meta.json", meta)
            with self.assertRaisesRegex(DeliveryError, "RGB frame-count profile"):
                publish_complete_case_delivery(mismatched, root / "mismatched_frame_count")
            for motion in frame_quality["camera_motion"]["views"].values():
                motion["frame_count"] = 2
            write_json(frame_mismatch / "quality_report.json", frame_quality)
            for view in frame_quality["camera_motion"]["views"]:
                write_json(frame_mismatch / "views" / view / "meta.json", {"frame_count_rgb": 2})

            first_quality = read_json(Path(runs[0]["run_dir"]) / "quality_report.json")
            first_quality["camera_motion"]["views"]["side_static"].update(
                {"camera_mode": "trajectory", "moving": True}
            )
            write_json(Path(runs[0]["run_dir"]) / "quality_report.json", first_quality)
            with self.assertRaisesRegex(DeliveryError, "at least 3 verified static cameras"):
                publish_complete_case_delivery(runs, root / "too_few_static")

            first_quality["camera_motion"]["views"]["side_static"].update(
                {"camera_mode": "fixed", "moving": False}
            )
            first_quality["camera_motion"]["views"]["tracking_subject"].update(
                {"camera_mode": "fixed", "moving": False}
            )
            write_json(Path(runs[0]["run_dir"]) / "quality_report.json", first_quality)
            with self.assertRaisesRegex(DeliveryError, "at least 2 verified moving cameras"):
                publish_complete_case_delivery(runs, root / "too_few_moving")

            custom_runs = []
            for index in range(3):
                custom_run = root / f"custom_camera_run_{index}"
                write_delivery_run(custom_run)
                camera = read_json(custom_run / "inputs" / "camera.json")
                for view in camera["views"]:
                    if view["camera_id"] == "tracking_subject":
                        view["camera_id"] = "custom_tracking"
                write_json(custom_run / "inputs" / "camera.json", camera)
                (custom_run / "views" / "tracking_subject").rename(custom_run / "views" / "custom_tracking")
                quality = read_json(custom_run / "quality_report.json")
                quality["camera_motion"]["views"]["custom_tracking"] = quality["camera_motion"]["views"].pop(
                    "tracking_subject"
                )
                write_json(custom_run / "quality_report.json", quality)
                custom_runs.append({
                    "label": f"custom_{index}",
                    "run_dir": custom_run,
                    "comparison_fingerprint": FINGERPRINT,
                })
            with self.assertRaisesRegex(DeliveryError, "missing required camera views tracking_subject"):
                publish_complete_case_delivery(custom_runs, root / "custom_camera_ids")

    def test_complete_delivery_rejects_incomplete_exr_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = []
            for index in range(3):
                run_dir = root / f"run_{index}"
                write_delivery_run(run_dir)
                runs.append({
                    "label": f"repeat_{index}",
                    "run_dir": run_dir,
                    "comparison_fingerprint": FINGERPRINT,
                })
            (Path(runs[0]["run_dir"]) / "views" / "top_down" / "depth_frames" / "frame_000001.exr").unlink()

            with self.assertRaisesRegex(DeliveryError, "EXR frame count does not match RGB"):
                publish_complete_case_delivery(
                    runs,
                    root / "delivery",
                )

    def test_write_json_preserves_old_file_if_atomic_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            path.write_text('{"old": true}\n', encoding="utf-8")

            with patch("harness.core.artifact_schema.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    write_json(path, {"new": True})

            self.assertEqual(path.read_text(encoding="utf-8"), '{"old": true}\n')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_publish_videos_flattens_only_real_mp4_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "batch_20260711" / "case_a_ue"
            (run_dir / "views" / "front_static").mkdir(parents=True)
            (run_dir / "views" / "debug").mkdir(parents=True)
            mp4 = b"\x00\x00\x00\x18ftypisom00000000"
            (run_dir / "video.mp4").write_bytes(mp4)
            (run_dir / "views" / "front_static" / "rgb.mp4").write_bytes(mp4)
            (run_dir / "views" / "front_static" / "depth_preview.mp4").write_bytes(mp4)
            (run_dir / "views" / "front_static" / "segmentation_preview.mp4").write_bytes(mp4)
            (run_dir / "views" / "debug" / "rgb.mp4").write_text("placeholder", encoding="utf-8")

            published = ArtifactManager(run_dir).publish_videos(root / "videos", case_id="case_a", backend="ue")

            self.assertEqual([path.name for path in published], [
                "case_a__ue__batch_20260711__front_static.mp4",
                "case_a__ue__batch_20260711__front_static__depth.mp4",
                "case_a__ue__batch_20260711__front_static__segmentation.mp4",
            ])
            self.assertTrue(all(path.is_file() for path in published))
            self.assertTrue((run_dir / "views" / "front_static" / "rgb.mp4").samefile(published[0]))
            self.assertTrue((run_dir / "views" / "front_static" / "depth_preview.mp4").samefile(published[1]))
            self.assertTrue((run_dir / "views" / "front_static" / "segmentation_preview.mp4").samefile(published[2]))

    def test_finalize_writes_canonical_dataset_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            camera_plan = {
                "views": [
                    {"camera_id": "front_static", "location": [1, -1, 1], "rotation": [0, 0, 0], "target": [0, 0, 0], "fov": 60},
                ]
            }
            render_config = {"fps": 60, "width": 320, "height": 180, "mode": "both"}
            manager = ArtifactManager(run_dir)
            manager.write_inputs(case_spec={"case_id": "case_a"}, scene_spec={"case_id": "case_a"}, camera_plan=camera_plan, render_config=render_config)
            write_json(
                run_dir / "trajectory.json",
                [
                    {"frame": 0, "time": 0.0, "objects": {"body": {"position": [0, 0, 1], "velocity": [0, 0, 0]}}},
                    {"frame": 1, "time": 1 / 60, "objects": {"body": {"position": [0, 0, 0.9], "velocity": [0, 0, -1]}}},
                ],
            )
            write_json(run_dir / "contact_events.json", [{"frame": 1, "objects": ["body", "floor"]}])
            write_json(
                run_dir / "render_sync_report.json",
                {
                    "status": "pass",
                    "multi_view_sync_ok": True,
                    "render_pass_valid": True,
                    "per_camera_statistics": {"front_static": {"frame_count_rgb": 2}},
                },
            )
            view_dir = run_dir / "views" / "front_static"
            view_dir.mkdir(parents=True)
            (view_dir / "rgb.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom")
            (view_dir / "depth.exr").write_bytes(b"\x76\x2f\x31\x01depth")
            (view_dir / "segmentation.exr").write_bytes(b"\x76\x2f\x31\x01mask")
            (view_dir / "depth_frames").mkdir()
            (view_dir / "segmentation_frames").mkdir()
            (view_dir / "depth_frames" / "frame_000000.exr").write_bytes(b"\x76\x2f\x31\x01depth")
            (view_dir / "segmentation_frames" / "frame_000000.exr").write_bytes(b"\x76\x2f\x31\x01mask")
            write_json(view_dir / "meta.json", {"instance_level": True, "instance_count": 1, "instance_mapping": [{"id": 1, "actor": "body"}]})
            (run_dir / "video.mp4").write_bytes(b"hero rgb")

            identity = {
                "repository": "/repo",
                "branch": "feature/test",
                "commit": "a" * 40,
                "dirty": False,
            }
            with patch("harness.core.artifact_manager.harness_git_identity", return_value=identity):
                manifest = manager.finalize(
                    run_id="run_a",
                    case_id="case_a",
                    mode="both",
                    seed=42,
                    camera_plan=camera_plan,
                    render_config=render_config,
                )

            self.assertEqual(manifest["schema_version"], "world_model_run.v2.3")
            for rel in (
                "manifest.json",
                "inputs/case.json",
                "inputs/scene.json",
                "inputs/camera.json",
                "inputs/render_config.json",
                "passes/rgb/video.mp4",
                "passes/data/depth.exr",
                "passes/data/segmentation.exr",
                "passes/data/segmentation.exr",
                "passes/data/instance.json",
                "sync/camera_trajectory.json",
                "sync/physics_trace.json",
                "sync/sync_report.json",
            ):
                self.assertTrue((run_dir / rel).exists(), rel)
                self.assertGreater((run_dir / rel).stat().st_size, 0, rel)
            sync = read_json(run_dir / "sync" / "sync_report.json")
            self.assertEqual(sync["status"], "pass")
            self.assertEqual(manifest["artifacts"]["segmentation"], "passes/data/segmentation.exr")
            self.assertEqual(manifest["harness_identity"], identity)
            self.assertEqual(
                manifest["views"]["front_static"],
                {
                    "rgb": "views/front_static/rgb.mp4",
                    "depth": "views/front_static/depth.exr",
                    "depth_frames_dir": "views/front_static/depth_frames/",
                    "segmentation": "views/front_static/segmentation.exr",
                    "segmentation_frames_dir": "views/front_static/segmentation_frames/",
                    "meta": "views/front_static/meta.json",
                },
            )
            self.assertTrue((run_dir / "video.mp4").samefile(run_dir / "passes/rgb/video.mp4"))
            self.assertTrue((view_dir / "depth.exr").samefile(run_dir / "passes/data/depth.exr"))
            self.assertTrue((view_dir / "segmentation.exr").samefile(run_dir / "passes/data/segmentation.exr"))
            self.assertTrue((view_dir / "segmentation.exr").samefile(run_dir / "passes/data/segmentation.exr"))

    def test_finalize_does_not_declare_missing_view_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            camera_plan = {"views": [{"camera_id": "missing_view"}]}
            manager = ArtifactManager(run_dir)
            manager.write_inputs(
                case_spec={"case_id": "case_a"},
                scene_spec={"case_id": "case_a"},
                camera_plan=camera_plan,
                render_config={"fps": 24},
            )

            manifest = manager.finalize(
                run_id="run_a",
                case_id="case_a",
                mode="data",
                seed=0,
                camera_plan=camera_plan,
                render_config={"fps": 24},
            )

            self.assertEqual(manifest["views"]["missing_view"], {})


if __name__ == "__main__":
    unittest.main()
