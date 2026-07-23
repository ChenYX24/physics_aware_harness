from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness.runtime.ue_backend import run_runner_process_group


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
