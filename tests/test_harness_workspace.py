from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.core import workspace as workspace_module
from harness.core.artifact_manager import exr_sequence_provenance
from harness.core.artifact_schema import write_json
from harness.core.workspace import (
    WORKSPACE_DIRS,
    WorkspaceError,
    bootstrap_workspace,
    build_ue_plugin,
    case_output_root,
    configure_ue_mount,
    init_workspace,
    prune_rejected,
    review_candidate,
    setup_doctor,
    workspace_root,
    workspace_status,
    workspace_path,
)


class HarnessWorkspaceTests(unittest.TestCase):
    def test_setup_doctor_separates_contract_and_real_ue_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = bootstrap_workspace(base / "workspace")
            ue = base / "UE_5.7" / "Engine" / "Binaries" / "Mac" / "UnrealEditor-Cmd"
            ue.parent.mkdir(parents=True)
            ue.write_text("#!/bin/sh\n", encoding="utf-8")
            ue.chmod(0o755)
            assets = base / "Content"
            (assets / "Props").mkdir(parents=True)
            (assets / "Props" / "Chair.uasset").write_bytes(b"ue-package")

            with patch("harness.core.workspace.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"):
                before_mount = setup_doctor(root, ue_executable=ue, asset_content=assets)
                configure_ue_mount(assets, root)
                fake_engine = setup_doctor(root, ue_executable=ue, asset_content=assets)
                write_json(
                    base / "UE_5.7" / "Engine" / "Build" / "Build.version",
                    {"MajorVersion": 5, "MinorVersion": 7, "PatchVersion": 4},
                )
                configured = setup_doctor(root, ue_executable=ue, asset_content=assets)
                smoke = root / "cases" / "rigid_collision" / "demo" / "v001" / "runs" / "ue_smoke"
                write_json(
                    smoke / "quality_report.json",
                    {
                        "hard_gate_passed": True,
                        "source_reports": {
                            "run_readiness": {
                                "backend": "ue",
                                "ue_render_real": True,
                                "local_preview_ready": True,
                                "view_count": 2,
                            },
                            "render_sync": {
                                "status": "pass",
                                "multi_view_sync_ok": True,
                            },
                        },
                    },
                )
                fake_report = setup_doctor(
                    root,
                    ue_executable=ue,
                    asset_content=assets,
                    native_smoke_run=smoke,
                )
                with patch(
                    "harness.core.workspace.evaluate_run",
                    return_value={
                        "hard_gate_passed": True,
                        "source_reports": {
                            "run_readiness": {
                                "backend": "ue",
                                "ue_render_real": True,
                                "local_preview_ready": True,
                                "view_count": 2,
                            },
                            "render_sync": {
                                "status": "pass",
                                "multi_view_sync_ok": True,
                            },
                        },
                        "camera_motion": {
                            "views": {
                                "front_static": {
                                    "camera_mode": "fixed",
                                    "moving": False,
                                    "unique_location_count": 1,
                                },
                                "tracking_subject": {
                                    "camera_mode": "object_bound",
                                    "moving": True,
                                    "unique_location_count": 2,
                                },
                            }
                        },
                    },
                ):
                    accepted = setup_doctor(
                        root,
                        ue_executable=ue,
                        asset_content=assets,
                        native_smoke_run=smoke,
                    )
                with patch(
                    "harness.core.workspace.evaluate_run",
                    return_value={
                        "hard_gate_passed": True,
                        "source_reports": {
                            "run_readiness": {
                                "backend": "ue",
                                "ue_render_real": True,
                                "local_preview_ready": True,
                                "view_count": 2,
                            },
                            "render_sync": {
                                "status": "pass",
                                "multi_view_sync_ok": True,
                            },
                        },
                        "camera_motion": {
                            "views": {
                                "front_static": {
                                    "camera_mode": "fixed",
                                    "moving": False,
                                    "unique_location_count": 1,
                                },
                                "top_down": {
                                    "camera_mode": "fixed",
                                    "moving": False,
                                    "unique_location_count": 1,
                                },
                            }
                        },
                    },
                ):
                    all_static = setup_doctor(
                        root,
                        ue_executable=ue,
                        asset_content=assets,
                        native_smoke_run=smoke,
                    )

            self.assertTrue(before_mount["contract_ready"])
            self.assertFalse(before_mount["ue_ready"])
            self.assertIn("workspace UE project", before_mount["missing_for_ue"])
            self.assertFalse(fake_engine["ue_config_ready"])
            self.assertIn("Unreal Engine 5.7 Build.version", fake_engine["missing_for_ue"])
            self.assertTrue(configured["checks"]["adp_physics_runtime_binary"])
            self.assertTrue(configured["ue_config_ready"])
            self.assertFalse(configured["ue_ready"])
            self.assertIn("hard-gate-passing native UE smoke run", configured["missing_for_ue"])
            self.assertFalse(fake_report["ue_ready"])
            self.assertTrue(accepted["ue_ready"])
            self.assertFalse(all_static["ue_ready"])

    def test_build_ue_plugin_packages_and_activates_host_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = init_workspace(base / "workspace")
            ue = base / "UE_5.7" / "Engine" / "Binaries" / "Linux" / "UnrealEditor-Cmd"
            ue.parent.mkdir(parents=True)
            ue.write_text("#!/bin/sh\n", encoding="utf-8")
            ue.chmod(0o755)
            uat = base / "UE_5.7" / "Engine" / "Build" / "BatchFiles" / "RunUAT.sh"
            uat.parent.mkdir(parents=True)
            uat.write_text("#!/bin/sh\n", encoding="utf-8")
            uat.chmod(0o755)

            def fake_build(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                package = Path(next(part.split("=", 1)[1] for part in command if part.startswith("-Package=")))
                shutil.copytree(workspace_module.REPO_ROOT / "ue_template" / "Plugins" / "ADPPhysicsRuntime", package)
                binaries = package / "Binaries" / "Linux"
                binaries.mkdir(parents=True)
                (binaries / "UnrealEditor.modules").write_text("{}", encoding="utf-8")
                (binaries / "libUnrealEditor-ADPPhysicsRuntime.so").write_bytes(b"linux-plugin")
                return subprocess.CompletedProcess(command, 0)

            (root / "ue" / "Plugins").mkdir(parents=True)
            source_link = root / "ue" / "Plugins" / "ADPPhysicsRuntime"
            source_link.symlink_to(
                workspace_module.REPO_ROOT / "ue_template" / "Plugins" / "ADPPhysicsRuntime",
                target_is_directory=True,
            )
            with patch("harness.core.workspace.subprocess.run", side_effect=fake_build):
                report = build_ue_plugin(root, ue_executable=ue)

            self.assertFalse(report["reused"])
            self.assertTrue(source_link.is_symlink())
            self.assertTrue((source_link / "Binaries" / "Linux" / "libUnrealEditor-ADPPhysicsRuntime.so").is_file())

    def test_relative_runtime_paths_resolve_under_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            resolved = workspace_path("runs/case_a", default_relative="runs/default", workspace=root)
            default_resolved = workspace_path(None, default_relative="runs/default", workspace=root)

            self.assertEqual(resolved, root.resolve() / "runs" / "case_a")
            self.assertEqual(default_resolved, root.resolve() / "runs" / "default")
            self.assertTrue((root / "review" / "kept").is_dir())

    def test_explicit_absolute_runtime_path_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "explicit"
            unused_workspace = Path(tmp) / "workspace"

            self.assertEqual(
                workspace_path(explicit, default_relative="runs/default", workspace=unused_workspace),
                explicit.resolve(),
            )
            self.assertFalse(unused_workspace.exists())

    def test_relative_runtime_path_cannot_escape_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"

            for escaped in ("../escape", "runs/../../escape"):
                with self.subTest(escaped=escaped), self.assertRaisesRegex(
                    WorkspaceError,
                    "must stay inside the workspace",
                ):
                    workspace_path(escaped, default_relative="runs/default", workspace=root)

            outside = Path(tmp) / "outside"
            outside.mkdir()
            (root / "escape_link").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(WorkspaceError, "must stay inside the workspace"):
                workspace_path("escape_link/output", default_relative="runs/default", workspace=root)

    def test_case_route_resolves_to_physics_scenario_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"

            resolved = case_output_root("rigid_collision/billiards/v002_complete_angle_matrix", root)

            self.assertEqual(resolved, root.resolve() / "cases" / "rigid_collision" / "billiards" / "v002_complete_angle_matrix")
            with self.assertRaises(WorkspaceError):
                case_output_root("rigid_collision/../escape", root)

    def test_init_uses_environment_and_creates_only_local_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            with patch.dict(os.environ, {"SIM_HARNESS_WORKSPACE": str(root)}):
                self.assertEqual(init_workspace(), root.resolve())
                status = workspace_status()
            self.assertTrue(status["initialized"])
            self.assertTrue(all((root / relative).is_dir() for relative in WORKSPACE_DIRS))

    def test_workspace_status_ignores_hidden_review_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = init_workspace(Path(tmp) / "workspace")
            inbox = root / "review" / "inbox"
            (inbox / ".DS_Store").write_bytes(b"finder")
            (inbox / "candidate").mkdir()
            (root / "review" / ".review-decisions.lock").write_bytes(b"")

            status = workspace_status(root)

            self.assertEqual(status["review"]["inbox"], 1)

    def test_workspace_inside_git_tree_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            with self.assertRaisesRegex(WorkspaceError, "outside a Git working tree"):
                workspace_root(repo / "local-output")
            with self.assertRaisesRegex(WorkspaceError, "outside a Git working tree"):
                workspace_path(repo / "runtime-output", default_relative="runs/default")

    def test_mount_ue_copies_content_only_project_and_links_top_level_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "workspace"
            repo = base / "source-repo"
            template = repo / "ue_template"
            plugin = template / "Plugins" / "ADPPhysicsRuntime"
            plugin.mkdir(parents=True)
            project_payload = {
                "FileVersion": 3,
                "Description": "content-only test",
                "Plugins": [{"Name": "ADPPhysicsRuntime", "Enabled": True}],
            }
            project_bytes = (json.dumps(project_payload) + "\n").encode()
            (template / "SimulatorStudioTemplate.uproject").write_bytes(project_bytes)
            content = base / "ADP" / "Content"
            for name in ("Maps", "Props"):
                (content / name).mkdir(parents=True)
            (content / "AssetRegistry.bin").write_bytes(b"ignored top-level file")

            result = configure_ue_mount(content, root, repo_root=repo)
            project = Path(result["project"])
            self.assertEqual(project.read_bytes(), project_bytes)
            self.assertNotIn("Modules", json.loads(project.read_text()))
            self.assertEqual((root / "ue" / "Content" / "Maps").resolve(), (content / "Maps").resolve())
            self.assertEqual((root / "ue" / "Content" / "Props").resolve(), (content / "Props").resolve())
            self.assertEqual((root / "ue" / "Plugins" / "ADPPhysicsRuntime").resolve(), plugin.resolve())
            self.assertEqual(configure_ue_mount(content, root, repo_root=repo), result)

    def test_review_and_prune_never_delete_kept_or_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = init_workspace(Path(tmp) / "workspace")
            inbox = root / "review" / "inbox"
            kept_video = inbox / "good.mp4"
            kept_video.write_bytes(b"video")
            self.assertEqual(review_candidate("good.mp4", "keep", root), root / "review" / "kept" / "good.mp4")
            rejected_run = inbox / "bad-run"
            rejected_run.mkdir()
            (rejected_run / "video.mp4").write_bytes(b"video")
            rejected_run = review_candidate("bad-run", "reject", root)
            pending = inbox / "pending.mp4"
            pending.write_bytes(b"video")
            old = 1_000_000.0
            for candidate in (rejected_run, root / "review" / "kept" / "good.mp4", pending):
                os.utime(candidate, (old, old))

            preview = prune_rejected(7, root, now=old + 8 * 86_400)
            self.assertEqual(preview, [rejected_run])
            self.assertTrue(rejected_run.exists())
            deleted = prune_rejected(7, root, dry_run=False, now=old + 8 * 86_400)
            self.assertEqual(deleted, [rejected_run])
            self.assertFalse(rejected_run.exists())
            self.assertTrue((root / "review" / "kept" / "good.mp4").exists())
            self.assertTrue(pending.exists())
            with self.assertRaises(WorkspaceError):
                review_candidate("../pending.mp4", "reject", root)

    def test_review_bundle_updates_its_linked_case_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = init_workspace(Path(tmp) / "workspace")
            case_route = "rigid_collision/billiards/v002_angle_matrix"
            status_path = root / "cases" / "rigid_collision" / "billiards" / "v002_angle_matrix" / "case_status.json"
            status_path.parent.mkdir(parents=True)
            bundle = root / "review" / "inbox" / "v002_angle_matrix"
            bundle.mkdir()
            (bundle / "preview.mp4").write_bytes(b"video")
            manifest_path = bundle / "v002.review.json"
            write_json(
                status_path,
                {
                    "case_route": case_route,
                    "status": "review_pending",
                    "decision": "awaiting_user",
                    "review": {
                        "candidate": bundle.name,
                        "inbox": str(bundle),
                        "manifest": str(manifest_path),
                    },
                },
            )
            write_json(
                manifest_path,
                {
                    "candidate": bundle.name,
                    "case_route": case_route,
                    "case_status": str(status_path),
                    "videos": [
                        {
                            "file": "preview.mp4",
                            "sha256": hashlib.sha256(b"video").hexdigest(),
                        }
                    ],
                },
            )

            destination = review_candidate(bundle.name, "keep", root)

            self.assertEqual(destination, root / "review" / "kept" / bundle.name)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "kept")
            self.assertEqual(status["decision"], "user_keep")
            self.assertEqual(status["review"]["decision"], "keep")
            self.assertEqual(status["review"]["destination"], str(destination))
            self.assertEqual(status["review"]["source_inbox"], str(bundle))
            self.assertNotIn("inbox", status["review"])
            self.assertEqual(status["review"]["manifest"], str(destination / "v002.review.json"))
            review_manifest = json.loads((destination / "v002.review.json").read_text(encoding="utf-8"))
            self.assertEqual(review_manifest["status"], "kept")
            self.assertEqual(review_manifest["decision"], "user_keep")
            self.assertEqual(review_manifest["destination"], str(destination))

    def test_review_decision_rolls_back_bundle_status_and_manifest_on_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, bundle, status_path, manifest_path = self.bound_review_bundle(Path(tmp) / "workspace")
            destination = root / "review" / "kept" / bundle.name
            original_status = status_path.read_bytes()
            original_manifest = manifest_path.read_bytes()
            real_write_json = workspace_module.write_json

            def fail_manifest_write(path, payload):
                if Path(path) == destination / manifest_path.name:
                    raise OSError("disk full")
                return real_write_json(path, payload)

            with (
                patch.object(workspace_module, "write_json", side_effect=fail_manifest_write),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                review_candidate(bundle.name, "keep", root)

            self.assertTrue(bundle.is_dir())
            self.assertFalse(destination.exists())
            self.assertEqual(status_path.read_bytes(), original_status)
            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            transactions = root / "review" / ".transactions"
            self.assertEqual(list(transactions.iterdir()), [])

    def test_next_review_operation_recovers_interrupted_prepared_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, bundle, status_path, manifest_path = self.bound_review_bundle(Path(tmp) / "workspace")
            destination = root / "review" / "kept" / bundle.name
            original_status = status_path.read_bytes()
            original_manifest = manifest_path.read_bytes()
            transaction = workspace_module._begin_review_transaction(
                root=root,
                source=bundle,
                destination=destination,
                status_path=status_path,
                manifest_path=manifest_path,
            )
            bundle.rename(destination)
            write_json(status_path, {"corrupt": "interrupted after rename"})
            write_json(destination / manifest_path.name, {"corrupt": "interrupted after rename"})
            self.assertTrue(transaction.is_file())

            with self.assertRaisesRegex(WorkspaceError, "decision must be"):
                review_candidate(bundle.name, "invalid", root)

            self.assertTrue(bundle.is_dir())
            self.assertFalse(destination.exists())
            self.assertEqual(status_path.read_bytes(), original_status)
            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            self.assertFalse(transaction.exists())

    def test_tampered_transaction_cannot_delete_backup_outside_transaction_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = init_workspace(Path(tmp) / "workspace")
            victim = root / "cases" / "do_not_delete.bak"
            victim.write_bytes(b"canonical evidence")
            transactions = root / "review" / ".transactions"
            transactions.mkdir()
            journal = transactions / "tampered.json"
            write_json(
                journal,
                {
                    "schema_version": workspace_module.REVIEW_TRANSACTION_SCHEMA_VERSION,
                    "state": "committed",
                    "source": "review/inbox/candidate",
                    "destination": "review/kept/candidate",
                    "status_snapshot": {
                        "target": "cases/status.json",
                        "backup": "cases/do_not_delete.bak",
                        "sha256": hashlib.sha256(victim.read_bytes()).hexdigest(),
                    },
                    "manifest_snapshot": None,
                },
            )

            with self.assertRaisesRegex(WorkspaceError, "must be one .bak file"):
                workspace_module._recover_review_decisions(root)

            self.assertEqual(victim.read_bytes(), b"canonical evidence")
            self.assertTrue(journal.is_file())

    def test_keep_rejects_tampered_or_unlisted_bundle_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = init_workspace(Path(tmp) / "workspace")
            bundle = root / "review" / "inbox" / "candidate"
            bundle.mkdir()
            (bundle / "preview.mp4").write_bytes(b"original")
            (bundle / "candidate.review.json").write_text(
                json.dumps(
                    {
                        "candidate": bundle.name,
                        "videos": [
                            {
                                "file": "preview.mp4",
                                "sha256": hashlib.sha256(b"original").hexdigest(),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (bundle / "preview.mp4").write_bytes(b"tampered")

            with self.assertRaisesRegex(WorkspaceError, "SHA-256 mismatch"):
                review_candidate(bundle.name, "keep", root)

            (bundle / "preview.mp4").write_bytes(b"original")
            (bundle / "unlisted.mp4").write_bytes(b"extra")
            with self.assertRaisesRegex(WorkspaceError, "video set does not match"):
                review_candidate(bundle.name, "keep", root)

            self.assertTrue(bundle.is_dir())

    def test_keep_validates_nested_complete_delivery_videos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = init_workspace(Path(tmp) / "workspace")
            bundle = root / "review" / "inbox" / "complete"
            video = bundle / "runs" / "attempt_01" / "top_down" / "rgb.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"nested-video")
            (bundle / "complete.review.json").write_text(
                json.dumps(
                    {
                        "candidate": bundle.name,
                        "videos": [
                            {
                                "file": "runs/attempt_01/top_down/rgb.mp4",
                                "sha256": hashlib.sha256(b"nested-video").hexdigest(),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            destination = review_candidate(bundle.name, "keep", root)

            self.assertTrue((destination / "runs" / "attempt_01" / "top_down" / "rgb.mp4").is_file())

    def test_keep_recomputes_source_truth_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = init_workspace(Path(tmp) / "workspace")
            run_dir = root / "cases" / "rigid_collision" / "billiards" / "v003_speed_matrix" / "runs" / "one"
            quality_path = run_dir / "quality_report.json"
            write_json(quality_path, {"hard_gate_passed": True})
            source_truth = {}
            for view in ("event_closeup", "top_down"):
                source_truth[view] = {}
                write_json(run_dir / "views" / view / "meta.json", {"frame_count_rgb": 2})
                for modality in ("depth", "segmentation"):
                    sequence = run_dir / "views" / view / f"{modality}_frames"
                    sequence.mkdir(parents=True)
                    for frame in range(2):
                        (sequence / f"frame_{frame:06d}.exr").write_bytes(b"exr" + bytes([frame]))
                    source_truth[view][modality] = exr_sequence_provenance(sequence, relative_to=run_dir)

            bundle = root / "review" / "inbox" / "complete"
            bundle.mkdir()
            (bundle / "preview.mp4").write_bytes(b"video")
            write_json(
                bundle / "complete.review.json",
                {
                    "candidate": bundle.name,
                    "views": ["event_closeup", "top_down"],
                    "source_runs": [
                        {
                            "source_run": str(run_dir),
                            "source_quality_report": str(quality_path),
                            "source_quality_report_sha256": hashlib.sha256(quality_path.read_bytes()).hexdigest(),
                            "source_truth": source_truth,
                        }
                    ],
                    "videos": [
                        {
                            "file": "preview.mp4",
                            "sha256": hashlib.sha256(b"video").hexdigest(),
                        }
                    ],
                },
            )
            (run_dir / "views" / "event_closeup" / "depth_frames" / "frame_000001.exr").write_bytes(b"tampered")

            with self.assertRaisesRegex(WorkspaceError, "source truth aggregate_sha256 mismatch"):
                review_candidate(bundle.name, "keep", root)

    def test_keep_recomputes_delivery_depth_and_segmentation_truth_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = init_workspace(Path(tmp) / "workspace")
            run_dir = root / "cases" / "rigid_collision" / "demo" / "v001" / "runs" / "one"
            quality_path = run_dir / "quality_report.json"
            write_json(quality_path, {"hard_gate_passed": True})
            bundle = root / "review" / "inbox" / "complete"
            bundle.mkdir()
            (bundle / "preview.mp4").write_bytes(b"video")
            source_truth = {"front_static": {}}
            write_json(run_dir / "views" / "front_static" / "meta.json", {"frame_count_rgb": 2})
            for modality in ("depth", "segmentation"):
                source = run_dir / "views" / "front_static" / f"{modality}_frames"
                delivery = bundle / "variants" / "baseline" / modality / "front_static" / "frames"
                source.mkdir(parents=True)
                delivery.mkdir(parents=True)
                for frame in range(2):
                    payload = b"exr" + bytes([frame])
                    (source / f"frame_{frame:06d}.exr").write_bytes(payload)
                    (delivery / f"frame_{frame:06d}.exr").write_bytes(payload)
                declared = exr_sequence_provenance(source, relative_to=run_dir)
                declared["delivery_path"] = delivery.relative_to(bundle).as_posix()
                source_truth["front_static"][modality] = declared
            write_json(
                bundle / "complete.review.json",
                {
                    "candidate": bundle.name,
                    "views": ["front_static"],
                    "source_runs": [
                        {
                            "source_run": str(run_dir),
                            "source_quality_report": str(quality_path),
                            "source_quality_report_sha256": hashlib.sha256(quality_path.read_bytes()).hexdigest(),
                            "source_truth": source_truth,
                        }
                    ],
                    "videos": [
                        {
                            "file": "preview.mp4",
                            "sha256": hashlib.sha256(b"video").hexdigest(),
                        }
                    ],
                },
            )
            (
                bundle
                / "variants"
                / "baseline"
                / "segmentation"
                / "front_static"
                / "frames"
                / "frame_000001.exr"
            ).write_bytes(b"tampered")

            with self.assertRaisesRegex(WorkspaceError, "delivery truth aggregate_sha256 mismatch"):
                review_candidate(bundle.name, "keep", root)

    def test_review_rejects_internal_symlinks_for_keep_and_reject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = init_workspace(Path(tmp) / "workspace")
            outside = Path(tmp) / "outside.mp4"
            outside.write_bytes(b"video")
            for decision in ("keep", "reject"):
                with self.subTest(decision=decision):
                    bundle = root / "review" / "inbox" / f"candidate_{decision}"
                    bundle.mkdir()
                    (bundle / "linked.mp4").symlink_to(outside)
                    with self.assertRaisesRegex(WorkspaceError, "must not contain symlinks"):
                        review_candidate(bundle.name, decision, root)

    def test_linked_review_requires_bidirectional_case_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = init_workspace(Path(tmp) / "workspace")
            case_route = "rigid_collision/billiards/v004_binding_check"
            status_path = root / "cases" / "rigid_collision" / "billiards" / "v004_binding_check" / "case_status.json"
            status_path.parent.mkdir(parents=True)
            bundle = root / "review" / "inbox" / "binding_candidate"
            bundle.mkdir()
            manifest_path = bundle / "binding.review.json"
            write_json(
                status_path,
                {
                    "case_route": case_route,
                    "review": {
                        "candidate": "another_candidate",
                        "inbox": str(bundle),
                        "manifest": str(manifest_path),
                    },
                },
            )
            write_json(
                manifest_path,
                {
                    "candidate": bundle.name,
                    "case_route": case_route,
                    "case_status": str(status_path),
                    "videos": [{"file": "preview.mp4", "sha256": hashlib.sha256(b"video").hexdigest()}],
                },
            )
            (bundle / "preview.mp4").write_bytes(b"video")

            with self.assertRaisesRegex(WorkspaceError, "review binding does not match"):
                review_candidate(bundle.name, "reject", root)

    def bound_review_bundle(self, workspace: Path) -> tuple[Path, Path, Path, Path]:
        root = init_workspace(workspace)
        case_route = "rigid_collision/billiards/v002_angle_matrix"
        status_path = root / "cases" / "rigid_collision" / "billiards" / "v002_angle_matrix" / "case_status.json"
        status_path.parent.mkdir(parents=True)
        bundle = root / "review" / "inbox" / "v002_angle_matrix"
        bundle.mkdir()
        (bundle / "preview.mp4").write_bytes(b"video")
        manifest_path = bundle / "v002.review.json"
        write_json(
            status_path,
            {
                "case_route": case_route,
                "status": "review_pending",
                "decision": "awaiting_user",
                "review": {
                    "candidate": bundle.name,
                    "inbox": str(bundle),
                    "manifest": str(manifest_path),
                },
            },
        )
        write_json(
            manifest_path,
            {
                "candidate": bundle.name,
                "case_route": case_route,
                "case_status": str(status_path),
                "videos": [
                    {
                        "file": "preview.mp4",
                        "sha256": hashlib.sha256(b"video").hexdigest(),
                    }
                ],
            },
        )
        return root, bundle, status_path, manifest_path


if __name__ == "__main__":
    unittest.main()
