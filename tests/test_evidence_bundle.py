from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.agent.evidence_bundle import EvidenceBundleError, build_evidence_bundle
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
                {"frame": 0, "time_s": 0.0, "objects": {"ball": {"position": [0, 0, 1]}}},
                {"frame": 1, "time_s": 0.5, "objects": {"ball": {"position": [0, 0, 0.5]}}},
                {"frame": 2, "time_s": 1.0, "objects": {"ball": {"position": [0, 0, 0]}}},
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
        self.case_spec = {"schema_version": "harness_case_spec_v2", "case_id": "evidence_case"}
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

    @staticmethod
    def fake_ffmpeg(command):
        destination = Path(command[-1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_event_anchored_bundle_has_locatable_hashed_multiview_evidence(self) -> None:
        result = build_evidence_bundle(
            job_id="job_evidence_bundle",
            attempt=self.attempt,
            attempt_dir=self.attempt_dir,
            candidate_run_dir=self.run_dir,
            request=self.request,
            intent_contract=self.intent,
            ffmpeg="/usr/bin/true",
            command_runner=self.fake_ffmpeg,
        )

        manifest = EvidenceBundleManifest.from_dict(result["manifest"]).to_dict()
        self.assertEqual(manifest["event_selection"]["strategy"], "event_anchored")
        self.assertEqual([row["time_s"] for row in manifest["event_selection"]["points"]], [0.0, 0.5, 1.0])
        self.assertEqual([row["view_id"] for row in manifest["artifacts"] if row["kind"] == "keyframe"], [
            "front", "side", "front", "side", "front", "side"
        ])
        self.assertEqual(len([row for row in manifest["artifacts"] if row["kind"] == "multi_view_montage"]), 3)
        self.assertIn("intent_amendments_snapshot", {row["artifact_id"] for row in manifest["artifacts"]})
        self.assertTrue(Path(result["manifest_path"]).is_file())
        self.assertEqual(result["stage_result"]["status"], "completed")

    def test_reuse_fails_closed_when_a_bundle_hash_changes(self) -> None:
        result = build_evidence_bundle(
            job_id="job_evidence_tamper",
            attempt=self.attempt,
            attempt_dir=self.attempt_dir,
            candidate_run_dir=self.run_dir,
            request=self.request,
            intent_contract=self.intent,
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
                ffmpeg="/usr/bin/true",
                command_runner=self.fake_ffmpeg,
            )


if __name__ == "__main__":
    unittest.main()
