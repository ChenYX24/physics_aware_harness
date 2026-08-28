from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.agent.evidence_bundle import (
    EvidenceBundleError,
    build_evidence_bundle,
    prune_validated_sensor_exr,
    semantic_review_requirements,
)
from harness.agent.job_schema import stable_digest
from harness.agent.review_schema import EvidenceBundleManifest
from harness.core.artifact_schema import read_json, write_json
from harness.core.stage_result import build_stage_result, write_stage_result


class EvidenceBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.attempt_dir = Path(self.temporary.name) / "attempt_001"
        self.run_dir = self.attempt_dir / "runs" / "candidate" / "evidence_case_fallback"
        (self.run_dir / "views" / "front").mkdir(parents=True)
        (self.run_dir / "views" / "side").mkdir(parents=True)
        for view in ("front", "side"):
            (self.run_dir / "views" / view / "rgb.mp4").write_bytes(b"fixture-mp4")
        write_json(
            self.run_dir / "trajectory.json",
            [
                {"frame": 0, "time_s": 0.0, "objects": {"ball": {"position": [0, 0, 1], "velocity_m_s": [0, 0, -1], "status": "falling"}}},
                {"frame": 1, "time_s": 0.5, "objects": {"ball": {"position": [0, 0, 0.5], "velocity_m_s": [0, 0, -2], "status": "impact"}}},
                {"frame": 2, "time_s": 1.0, "objects": {"ball": {"position": [0, 0, 0], "velocity_m_s": [0, 0, 0], "status": "resting"}}},
            ],
        )
        write_json(
            self.run_dir / "contact_events.json",
            {"events": [{"frame": 1, "time_s": 0.5, "objects": ["ball", "floor"], "kind": "impact"}]},
        )
        write_json(self.run_dir / "harness_verifier.json", {"schema_version": "fixture", "status": "pass"})
        write_json(self.run_dir / "render_sync_report.json", {"schema_version": "fixture", "status": "pass"})
        write_json(
            self.run_dir / "quality_report.json",
            {
                "schema_version": "harness_run_quality_v1",
                "status": "pass",
                "hard_gate_passed": True,
                "media": {"views": {"side": {"status": "pass"}, "front": {"status": "pass"}}},
            },
        )
        for stage in ("verifier", "render_sync", "quality_gate"):
            write_stage_result(self.run_dir, build_stage_result(stage=stage, status="completed"))
        self.case_spec = {
            "schema_version": "harness_case_spec_v2",
            "case_id": "evidence_case",
            "verification_requirements": {
                "assertions": [
                    {
                        "id": "ball_contacts_floor",
                        "type": "event_exists",
                        "event": "contact",
                        "objects": ["ball", "floor"],
                    }
                ]
            },
        }
        write_json(self.attempt_dir / "case_spec.json", self.case_spec)
        write_json(self.attempt_dir / "candidate_run.json", {"run_dir": str(self.run_dir), "fingerprint": "f" * 64})
        self.attempt = {
            "attempt_id": "attempt_001",
            "case_spec_digest": stable_digest(self.case_spec),
            "intent_contract_digest": "b" * 64,
        }
        self.request = {
            "schema_version": "harness_case_request_v1",
            "case_id": "evidence_case",
            "text": "drop the ball",
            "inputs": [],
            "execution_constraints": {},
        }
        self.intent = {
            "hard_requirements": [{"id": "original_user_request", "text": "drop the ball", "frozen": True}]
        }
        self.result_provenance = {
            "schema_version": "harness_evidence_result_provenance_v1",
            "publication": {"requested_tier": "local_preview", "achieved_tier": "local_preview"},
            "assets": {"items": []},
            "provider_usage": {"external_provider_used": False, "paid_submissions": 0},
        }

    @staticmethod
    def fake_ffmpeg(command):
        destination = Path(command[-1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_uniform_bundle_has_locatable_hashed_multiview_evidence(self) -> None:
        result = build_evidence_bundle(
            job_id="job_evidence_bundle",
            attempt=self.attempt,
            attempt_dir=self.attempt_dir,
            candidate_run_dir=self.run_dir,
            request=self.request,
            intent_contract=self.intent,
            result_provenance=self.result_provenance,
            ffmpeg="/usr/bin/true",
            command_runner=self.fake_ffmpeg,
        )

        manifest = EvidenceBundleManifest.from_dict(result["manifest"]).to_dict()
        self.assertEqual(manifest["event_selection"]["strategy"], "uniform_interval")
        self.assertEqual([row["time_s"] for row in manifest["event_selection"]["points"]], [0.0, 0.5, 1.0])
        self.assertEqual([row["view_id"] for row in manifest["artifacts"] if row["kind"] == "keyframe"], [
            "front", "side", "front", "side", "front", "side"
        ])
        self.assertEqual([row for row in manifest["artifacts"] if row["kind"] == "multi_view_montage"], [])
        trajectory = manifest["trajectory_summary"]
        self.assertEqual(len(trajectory["sampled_frames"]), 3)
        ball = trajectory["sampled_frames"][1]["objects"][0]
        self.assertEqual(ball["transform"]["position"]["values"], [0.0, 0.0, 0.5])
        self.assertEqual(ball["linear_velocity"]["values"], [0.0, 0.0, -2.0])
        self.assertEqual(ball["state"], [{"field": "status", "value": "impact"}])
        self.assertEqual([row["after"][0]["value"] for row in trajectory["state_transitions"]], ["impact", "resting"])
        self.assertEqual(trajectory["readable_ranges"][1]["event_refs"], ["contact_000"])
        self.assertIn("intent_amendments_snapshot", {row["artifact_id"] for row in manifest["artifacts"]})
        summary = read_json(self.attempt_dir / "evidence_bundle" / "evidence_summary.json")
        self.assertEqual(summary["result_provenance"], self.result_provenance)
        self.assertTrue(Path(result["manifest_path"]).is_file())
        self.assertEqual(result["stage_result"]["status"], "completed")

    def test_single_view_bundle_uses_keyframes_without_montage(self) -> None:
        quality = read_json(self.run_dir / "quality_report.json")
        quality["media"]["views"] = {"front": {"status": "pass"}}
        write_json(self.run_dir / "quality_report.json", quality)

        result = build_evidence_bundle(
            job_id="job_single_view_evidence",
            attempt=self.attempt,
            attempt_dir=self.attempt_dir,
            candidate_run_dir=self.run_dir,
            request=self.request,
            intent_contract=self.intent,
            result_provenance=self.result_provenance,
            ffmpeg="/usr/bin/true",
            command_runner=self.fake_ffmpeg,
        )

        artifacts = result["manifest"]["artifacts"]
        self.assertEqual(len([row for row in artifacts if row["kind"] == "keyframe"]), 3)
        self.assertEqual([row for row in artifacts if row["kind"] == "multi_view_montage"], [])

    def test_validated_sensor_exr_is_pruned_but_metrics_and_previews_remain(self) -> None:
        view_dir = self.run_dir / "views" / "front"
        (view_dir / "segmentation_frames").mkdir()
        (view_dir / "segmentation_frames" / "frame_0000.exr").write_bytes(b"mask")
        (view_dir / "segmentation.exr").write_bytes(b"mask")
        (view_dir / "segmentation_preview.mp4").write_bytes(b"preview")
        write_json(
            view_dir / "meta.json",
            {"frame_count_segmentation": 1, "segmentation_frames": ["views/front/segmentation_frames/frame_0000.exr"]},
        )

        report = prune_validated_sensor_exr(self.run_dir)

        self.assertEqual(report["status"], "pass")
        self.assertFalse((view_dir / "segmentation_frames").exists())
        self.assertFalse((view_dir / "segmentation.exr").exists())
        self.assertTrue((view_dir / "segmentation_preview.mp4").is_file())
        meta = read_json(view_dir / "meta.json")
        self.assertEqual(meta["frame_count_segmentation"], 1)
        self.assertEqual(meta["segmentation_frames"], [])

    def test_bundle_uses_particle_state_for_deforming_surface_evidence(self) -> None:
        write_json(
            self.run_dir / "trajectory.json",
            [
                {
                    "frame": index,
                    "time_s": index * 0.5,
                    "objects": {
                        "fluid_surface": {
                            "position": [0.0, 0.0, -0.05],
                            "velocity": [0.0, 0.0, 0.0],
                        }
                    },
                }
                for index in range(3)
            ],
        )
        write_json(
            self.run_dir / "particle_cache.json",
            {
                "schema_version": "harness_particle_cache_v1",
                "frames": [
                    {
                        "frame": 0,
                        "time_s": 0.0,
                        "positions_m": [[0.0, 0.0, 0.2], [0.1, 0.0, 0.2]],
                        "velocities_m_s": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
                        "measurements": {"inside": 1.0},
                    },
                    {
                        "frame": 1,
                        "time_s": 0.5,
                        "positions_m": [[0.2, 0.0, 0.1], [0.4, 0.0, 0.1]],
                        "velocities_m_s": [[0.4, 0.0, -0.2], [0.6, 0.0, -0.2]],
                        "measurements": {"inside": 0.5},
                    },
                    {
                        "frame": 2,
                        "time_s": 1.0,
                        "positions_m": [[0.5, 0.0, 0.0], [0.8, 0.0, 0.0]],
                        "velocities_m_s": [[0.6, 0.0, 0.0], [0.8, 0.0, 0.0]],
                        "measurements": {"inside": 0.0},
                    },
                ],
            },
        )

        build_evidence_bundle(
            job_id="job_deformable_evidence",
            attempt=self.attempt,
            attempt_dir=self.attempt_dir,
            candidate_run_dir=self.run_dir,
            request=self.request,
            intent_contract=self.intent,
            result_provenance=self.result_provenance,
            ffmpeg="/usr/bin/true",
            command_runner=self.fake_ffmpeg,
        )

        summary = read_json(self.attempt_dir / "evidence_bundle" / "evidence_summary.json")
        deformable = summary["deformable_state_truth"]
        self.assertEqual(deformable["authority"], "particle_cache")
        self.assertEqual(deformable["subject_id"], "fluid_surface")
        self.assertEqual(deformable["sampled_frames"][0]["centroid_m"], [0.05, 0.0, 0.2])
        self.assertEqual(deformable["sampled_frames"][-1]["centroid_m"], [0.65, 0.0, 0.0])
        self.assertEqual(deformable["sampled_frames"][-1]["measurements"], {"inside": 0.0})
        self.assertGreater(deformable["sampled_frames"][-1]["max_speed_m_s"], 0.0)

    def test_bundle_exposes_required_user_asset_identity(self) -> None:
        source_uri = "local-input://sha256/" + "a" * 64 + "/required.fbx"
        self.case_spec["objects"] = [
            {
                "id": "required_asset",
                "asset": {
                    "acquisition": {
                        "requirement": "required",
                        "origin": "user_explicit",
                        "source_uri_hint": source_uri,
                    }
                },
            }
        ]
        write_json(self.attempt_dir / "case_spec.json", self.case_spec)
        self.attempt["case_spec_digest"] = stable_digest(self.case_spec)
        write_json(
            self.run_dir / "asset_resolution.json",
            {
                "assets": [
                    {
                        "intent": {"object_id": "required_asset"},
                        "selection_reason": "required_source_uri_exact_match",
                        "selected_asset": {
                            "asset_id": "local_input.required",
                            "source_uri": source_uri,
                            "sha256": "a" * 64,
                            "ue_path": "/Game/Generated/Required.Required",
                            "geometry_registration": {"status": "verified"},
                        },
                    }
                ]
            },
        )
        write_json(
            self.run_dir / "provider_input_manifest.json",
            {
                "inputs": [
                    {
                        "input_id": "local_asset_required",
                        "source_uri": source_uri,
                        "sha256": "a" * 64,
                        "local_path": "/fixtures/required.fbx",
                    }
                ]
            },
        )

        build_evidence_bundle(
            job_id="job_explicit_asset_evidence",
            attempt=self.attempt,
            attempt_dir=self.attempt_dir,
            candidate_run_dir=self.run_dir,
            request=self.request,
            intent_contract=self.intent,
            result_provenance=self.result_provenance,
            ffmpeg="/usr/bin/true",
            command_runner=self.fake_ffmpeg,
        )

        summary = read_json(self.attempt_dir / "evidence_bundle" / "evidence_summary.json")
        binding = summary["explicit_asset_bindings"]["items"][0]
        self.assertEqual(binding["object_id"], "required_asset")
        self.assertEqual(binding["asset_id"], "local_input.required")
        self.assertEqual(binding["source_uri"], source_uri)
        self.assertEqual(binding["sha256"], "a" * 64)
        self.assertEqual(binding["selection_reason"], "required_source_uri_exact_match")
        self.assertEqual(binding["input_local_path"], "/fixtures/required.fbx")
        self.assertEqual(binding["geometry_registration_status"], "verified")

    def test_declared_contact_ignores_earlier_contact_and_preserves_constraint_states(self) -> None:
        write_json(
            self.run_dir / "trajectory.json",
            [
                {
                    "frame": index,
                    "time_s": index * 0.5,
                    "objects": {
                        "ball": {"position": [float(index), 0, 0]},
                        "target": {"position": [2.0, 0, 0]},
                    },
                    "constraints": [
                        {
                            "constraint_id": "constraint_a",
                            "position_target_m": [1.0, 0.0, 0.0],
                            "stiffness_n_m": [20.0, 0.0, 0.0],
                            "deformation_m": [0.5 - index * 0.1, 0.0, 0.0],
                            "source": "runtime_driver",
                        }
                    ],
                }
                for index in range(5)
            ],
        )
        write_json(
            self.run_dir / "contact_events.json",
            {
                "events": [
                    {"frame": 0, "time_s": 0.0, "objects": ["ball", "floor"], "kind": "support"},
                    {"frame": 2, "time_s": 1.0, "objects": ["ball", "target"], "kind": "contact"},
                    {"frame": 3, "time_s": 1.5, "objects": ["ball", "target"], "kind": "contact"},
                ]
            },
        )
        self.case_spec["verification_requirements"] = {
            "assertions": [
                {
                    "id": "declared_contact",
                    "type": "event_exists",
                    "event": "contact",
                    "objects": ["ball", "target"],
                }
            ]
        }
        write_json(self.attempt_dir / "case_spec.json", self.case_spec)
        self.attempt["case_spec_digest"] = stable_digest(self.case_spec)

        result = build_evidence_bundle(
            job_id="job_declared_contact",
            attempt=self.attempt,
            attempt_dir=self.attempt_dir,
            candidate_run_dir=self.run_dir,
            request=self.request,
            intent_contract=self.intent,
            result_provenance=self.result_provenance,
            ffmpeg="/usr/bin/true",
            command_runner=self.fake_ffmpeg,
        )

        selection = result["manifest"]["event_selection"]
        self.assertEqual(selection["strategy"], "uniform_interval")
        self.assertEqual([point["frame_index"] for point in selection["points"]], [0, 1, 2, 3, 4])
        self.assertTrue(all(not point["event_refs"] for point in selection["points"]))
        summary = read_json(self.attempt_dir / "evidence_bundle" / "evidence_summary.json")
        self.assertEqual(summary["constraint_states"]["constraint_ids"], ["constraint_a"])
        first_state = summary["constraint_states"]["sampled_frames"][0]["constraints"][0]
        self.assertEqual(first_state["stiffness_n_m"], [20.0, 0.0, 0.0])
        self.assertEqual(first_state["source"], "runtime_driver")

    def test_event_sequence_drives_temporally_distributed_visual_evidence(self) -> None:
        write_json(
            self.run_dir / "trajectory.json",
            [
                {
                    "frame": index,
                    "time_s": index * 0.5,
                    "objects": {"ball": {"position": [float(index), 0, 0]}},
                }
                for index in range(10)
            ],
        )
        write_json(
            self.run_dir / "contact_events.json",
            {
                "events": [
                    {"frame": 1, "time_s": 0.5, "objects": ["ball", "noise"]},
                    {"frame": 2, "time_s": 1.0, "objects": ["ball", "step_3"]},
                    {"frame": 5, "time_s": 2.5, "objects": ["ball", "step_2"]},
                    {"frame": 8, "time_s": 4.0, "objects": ["ball", "step_1"]},
                    {"frame": 9, "time_s": 4.5, "objects": ["ball", "ground"]},
                ]
            },
        )
        self.case_spec["verification_requirements"] = {
            "assertions": [
                {
                    "id": "ordered_contacts",
                    "type": "event_sequence",
                    "pairs": [["ball", "step_3"], ["ball", "step_2"], ["ball", "step_1"]],
                },
                {
                    "id": "ball_reaches_ground",
                    "type": "event_exists",
                    "event": "contact",
                    "objects": ["ball", "ground"],
                },
            ]
        }
        write_json(self.attempt_dir / "case_spec.json", self.case_spec)
        self.attempt["case_spec_digest"] = stable_digest(self.case_spec)

        result = build_evidence_bundle(
            job_id="job_event_sequence_evidence",
            attempt=self.attempt,
            attempt_dir=self.attempt_dir,
            candidate_run_dir=self.run_dir,
            request=self.request,
            intent_contract=self.intent,
            result_provenance=self.result_provenance,
            ffmpeg="/usr/bin/true",
            command_runner=self.fake_ffmpeg,
        )

        selection = result["manifest"]["event_selection"]
        self.assertEqual(selection["strategy"], "event_anchored")
        self.assertEqual(selection["reason"], "anchored to verification assertion ordered_contacts")
        self.assertEqual([point["frame_index"] for point in selection["points"]], [1, 5, 9])
        self.assertEqual(selection["points"][1]["event_refs"], ["contact_002"])

    def test_two_contact_sequence_selects_support_loss_transition(self) -> None:
        write_json(
            self.run_dir / "trajectory.json",
            [
                {"frame": index, "time_s": index / 24.0, "objects": {"barrel": {"position": [0, 0, 1]}}}
                for index in range(105)
            ],
        )
        write_json(
            self.run_dir / "contact_events.json",
            {
                "events": [
                    *[
                        {"frame": index, "objects": ["barrel", "table"], "kind": "support"}
                        for index in range(1, 94)
                    ],
                    {"frame": 99, "objects": ["barrel", "ground"], "kind": "contact"},
                ]
            },
        )
        self.case_spec["verification_requirements"] = {
            "assertions": [
                {
                    "id": "table_to_ground",
                    "type": "event_sequence",
                    "pairs": [["barrel", "table"], ["barrel", "ground"]],
                }
            ]
        }
        write_json(self.attempt_dir / "case_spec.json", self.case_spec)
        self.attempt["case_spec_digest"] = stable_digest(self.case_spec)

        result = build_evidence_bundle(
            job_id="job_two_contact_transition",
            attempt=self.attempt,
            attempt_dir=self.attempt_dir,
            candidate_run_dir=self.run_dir,
            request=self.request,
            intent_contract=self.intent,
            result_provenance=self.result_provenance,
            ffmpeg="/usr/bin/true",
            command_runner=self.fake_ffmpeg,
        )

        selection = result["manifest"]["event_selection"]
        self.assertEqual(selection["strategy"], "event_sequence_transition")
        self.assertEqual([point["frame_index"] for point in selection["points"]], [47, 93, 96, 99])
        self.assertEqual(
            [point["label"] for point in selection["points"]],
            ["supported_motion", "support_end", "unsupported_midpoint", "new_contact"],
        )
        self.assertEqual(
            len([artifact for artifact in result["manifest"]["artifacts"] if artifact["kind"] == "keyframe"]),
            8,
        )

    def test_ambiguity_decision_projects_to_stable_semantic_requirement(self) -> None:
        ambiguity_id = "ambiguity_001_fixture"
        requirements = semantic_review_requirements(
            self.intent,
            [
                {
                    "schema_version": "harness_intent_contract_amendment_v1",
                    "ambiguity_resolutions": [
                        {"ambiguity_id": ambiguity_id, "decision": "the cue ball moves first"}
                    ],
                }
            ],
        )

        self.assertEqual(
            [row["id"] for row in requirements],
            ["original_user_request", f"ambiguity_decision_{stable_digest(ambiguity_id)[:16]}"],
        )
        self.assertEqual(requirements[1]["decision"], "the cue ball moves first")
        self.assertTrue(requirements[1]["frozen"])

    def test_reuse_fails_closed_when_a_bundle_hash_changes(self) -> None:
        result = build_evidence_bundle(
            job_id="job_evidence_tamper",
            attempt=self.attempt,
            attempt_dir=self.attempt_dir,
            candidate_run_dir=self.run_dir,
            request=self.request,
            intent_contract=self.intent,
            result_provenance=self.result_provenance,
            ffmpeg="/usr/bin/true",
            command_runner=self.fake_ffmpeg,
        )
        manifest = read_json(result["manifest_path"])
        target = self.attempt_dir / "evidence_bundle" / manifest["artifacts"][0]["path"]
        target.write_bytes(b"tampered")

        with self.assertRaisesRegex(EvidenceBundleError, "identity mismatch"):
            build_evidence_bundle(
                job_id="job_evidence_tamper",
                attempt=self.attempt,
                attempt_dir=self.attempt_dir,
                candidate_run_dir=self.run_dir,
                request=self.request,
                intent_contract=self.intent,
                result_provenance=self.result_provenance,
                ffmpeg="/usr/bin/true",
                command_runner=self.fake_ffmpeg,
            )

    def test_reuse_rejects_a_symlinked_bundle_artifact(self) -> None:
        result = build_evidence_bundle(
            job_id="job_evidence_symlink",
            attempt=self.attempt,
            attempt_dir=self.attempt_dir,
            candidate_run_dir=self.run_dir,
            request=self.request,
            intent_contract=self.intent,
            result_provenance=self.result_provenance,
            ffmpeg="/usr/bin/true",
            command_runner=self.fake_ffmpeg,
        )
        manifest = read_json(result["manifest_path"])
        target = self.attempt_dir / "evidence_bundle" / manifest["artifacts"][0]["path"]
        replacement = self.attempt_dir / "replacement.json"
        replacement.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(replacement)

        with self.assertRaisesRegex(EvidenceBundleError, "identity mismatch"):
            build_evidence_bundle(
                job_id="job_evidence_symlink",
                attempt=self.attempt,
                attempt_dir=self.attempt_dir,
                candidate_run_dir=self.run_dir,
                request=self.request,
                intent_contract=self.intent,
                result_provenance=self.result_provenance,
                ffmpeg="/usr/bin/true",
                command_runner=self.fake_ffmpeg,
            )

    def test_reuse_rejects_foreign_or_stale_bundle_identity(self) -> None:
        for field, value, code in (
            ("job_id", "job_foreign_bundle", "identity"),
            ("attempt_id", "attempt_999", "identity"),
            ("case_spec_digest", "c" * 64, "identity"),
            ("intent_contract_digest", "d" * 64, "identity"),
        ):
            with self.subTest(field=field):
                bundle_dir = self.attempt_dir / "evidence_bundle"
                if bundle_dir.exists():
                    for path in sorted(bundle_dir.rglob("*"), reverse=True):
                        if path.is_file():
                            path.unlink()
                        elif path.is_dir():
                            path.rmdir()
                    bundle_dir.rmdir()
                result = build_evidence_bundle(
                    job_id="job_evidence_identity",
                    attempt=self.attempt,
                    attempt_dir=self.attempt_dir,
                    candidate_run_dir=self.run_dir,
                    request=self.request,
                    intent_contract=self.intent,
                    result_provenance=self.result_provenance,
                    ffmpeg="/usr/bin/true",
                    command_runner=self.fake_ffmpeg,
                )
                manifest = read_json(result["manifest_path"])
                manifest[field] = value
                write_json(result["manifest_path"], manifest)
                with self.assertRaises(EvidenceBundleError) as raised:
                    build_evidence_bundle(
                        job_id="job_evidence_identity",
                        attempt=self.attempt,
                        attempt_dir=self.attempt_dir,
                        candidate_run_dir=self.run_dir,
                        request=self.request,
                        intent_contract=self.intent,
                        result_provenance=self.result_provenance,
                        ffmpeg="/usr/bin/true",
                        command_runner=self.fake_ffmpeg,
                    )
                self.assertEqual(raised.exception.code, f"evidence_bundle_{code}_mismatch")

    def test_reuse_rejects_changed_candidate_or_input_snapshot(self) -> None:
        result = build_evidence_bundle(
            job_id="job_evidence_current_inputs",
            attempt=self.attempt,
            attempt_dir=self.attempt_dir,
            candidate_run_dir=self.run_dir,
            request=self.request,
            intent_contract=self.intent,
            result_provenance=self.result_provenance,
            ffmpeg="/usr/bin/true",
            command_runner=self.fake_ffmpeg,
        )
        candidate = read_json(self.attempt_dir / "candidate_run.json")
        candidate["fingerprint"] = "a" * 64
        write_json(self.attempt_dir / "candidate_run.json", candidate)
        with self.assertRaisesRegex(EvidenceBundleError, "Candidate identity"):
            build_evidence_bundle(
                job_id="job_evidence_current_inputs",
                attempt=self.attempt,
                attempt_dir=self.attempt_dir,
                candidate_run_dir=self.run_dir,
                request=self.request,
                intent_contract=self.intent,
                result_provenance=self.result_provenance,
                ffmpeg="/usr/bin/true",
                command_runner=self.fake_ffmpeg,
            )

        candidate["fingerprint"] = "f" * 64
        write_json(self.attempt_dir / "candidate_run.json", candidate)
        changed_request = {**self.request, "text": "a different immutable request"}
        with self.assertRaisesRegex(EvidenceBundleError, "snapshot is stale"):
            build_evidence_bundle(
                job_id="job_evidence_current_inputs",
                attempt=self.attempt,
                attempt_dir=self.attempt_dir,
                candidate_run_dir=self.run_dir,
                request=changed_request,
                intent_contract=self.intent,
                result_provenance=self.result_provenance,
                ffmpeg="/usr/bin/true",
                command_runner=self.fake_ffmpeg,
            )


if __name__ == "__main__":
    unittest.main()
