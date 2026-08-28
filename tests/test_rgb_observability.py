from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.core.artifact_schema import read_json, write_json
from harness.verification.rgb_observability import verify_expected_color_observability


class RGBObservabilityTests(unittest.TestCase):
    def test_distinctive_subject_must_remain_visible_across_frames(self) -> None:
        blue = bytes([8, 40, 210])
        gray = bytes([80, 80, 80])
        pixels = 64 * 64
        visible_frame = blue * 16 + gray * (pixels - 16)
        hidden_frame = gray * pixels
        output = visible_frame * 3 + hidden_frame
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "views" / "event_closeup" / "rgb.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"mp4")
            completed = subprocess.CompletedProcess([], 0, output, b"")
            with patch("harness.verification.rgb_observability.subprocess.run", return_value=completed):
                report = verify_expected_color_observability(
                    tmp,
                    expected_rgb=[0.03, 0.30, 0.78],
                    view_ids=["event_closeup"],
                    write=False,
                )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["views"]["event_closeup"]["visible_frame_count"], 3)

    def test_syntactically_valid_video_without_subject_color_fails(self) -> None:
        gray_frames = bytes([80, 80, 80]) * (64 * 64 * 4)
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "views" / "event_closeup" / "rgb.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"mp4")
            completed = subprocess.CompletedProcess([], 0, gray_frames, b"")
            with patch("harness.verification.rgb_observability.subprocess.run", return_value=completed):
                report = verify_expected_color_observability(
                    tmp,
                    expected_rgb=[0.03, 0.30, 0.78],
                    view_ids=["event_closeup"],
                    write=False,
                )
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["enforcement"], "advisory")
        self.assertEqual(report["failure_codes"], ["F_RGB_EXPECTED_SUBJECT_NOT_OBSERVABLE"])

    def test_failed_diagnostic_does_not_mutate_readiness(self) -> None:
        gray_frames = bytes([80, 80, 80]) * (64 * 64 * 4)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "views" / "event_closeup" / "rgb.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"mp4")
            readiness = {"local_preview_ready": True, "visual_ready": True}
            write_json(root / "run_readiness.json", readiness)
            completed = subprocess.CompletedProcess([], 0, gray_frames, b"")
            with patch("harness.verification.rgb_observability.subprocess.run", return_value=completed):
                verify_expected_color_observability(
                    root,
                    expected_rgb=[0.03, 0.30, 0.78],
                    view_ids=["event_closeup"],
                    write=True,
                )

            self.assertEqual(read_json(root / "run_readiness.json"), readiness)


if __name__ == "__main__":
    unittest.main()
