from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness.runtime.ue_backend import run_runner_process_group
from scripts.harness_local_ue_runner import run_ue_until_artifacts


class UERunnerProcessGroupTests(unittest.TestCase):
    def test_timeout_terminates_the_whole_process_group(self) -> None:
        proc = MagicMock()
        proc.pid = 4242
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["runner"], 1),
            ("partial stdout", "partial stderr"),
        ]
        with patch("harness.runtime.ue_backend.subprocess.Popen", return_value=proc) as popen, patch(
            "harness.runtime.ue_backend.os.killpg"
        ) as killpg:
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                run_runner_process_group(["runner"], cwd=Path("/tmp"), timeout=1)

        popen.assert_called_once_with(
            ["runner"],
            cwd=Path("/tmp"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        killpg.assert_called_once()
        self.assertEqual(raised.exception.stdout, "partial stdout")
        self.assertEqual(raised.exception.stderr, "partial stderr")

    def test_keyboard_interrupt_terminates_the_whole_process_group(self) -> None:
        proc = MagicMock()
        proc.pid = 4242
        proc.poll.return_value = None
        proc.communicate.side_effect = [KeyboardInterrupt(), ("", "")]
        with patch("harness.runtime.ue_backend.subprocess.Popen", return_value=proc), patch(
            "harness.runtime.ue_backend.os.killpg"
        ) as killpg:
            with self.assertRaises(KeyboardInterrupt):
                run_runner_process_group(["runner"], cwd=Path("/tmp"), timeout=1)

        killpg.assert_called_once()

    def test_local_runner_interrupt_removes_partial_frame_directories(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as tmp:
            native_output = Path(tmp) / "native_rgb"
            with patch("scripts.harness_local_ue_runner.subprocess.Popen", return_value=proc), patch(
                "scripts.harness_local_ue_runner.native_artifact_signature", return_value=None
            ), patch("scripts.harness_local_ue_runner.time.sleep", side_effect=KeyboardInterrupt), patch(
                "scripts.harness_local_ue_runner.terminate_process"
            ) as terminate:
                frames = native_output / "frames_front"
                # The native output is recreated before Popen, so stage the partial
                # capture from the first poll.
                def poll_with_frame():
                    frames.mkdir(parents=True, exist_ok=True)
                    (frames / "frame_0000.png").write_bytes(b"partial")
                    return None

                proc.poll.side_effect = poll_with_frame
                with self.assertRaises(KeyboardInterrupt):
                    run_ue_until_artifacts(
                        ["UnrealEditor-Cmd"],
                        env={},
                        native_output=native_output,
                        timeout=60,
                    )

            terminate.assert_called_once_with(proc)
            self.assertFalse(frames.exists())
