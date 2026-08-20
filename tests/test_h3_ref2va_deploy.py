from __future__ import annotations

import subprocess
import unittest
import os
from pathlib import Path


class H3Ref2VADeployTests(unittest.TestCase):
    def test_print_config_is_localhost_ref2va_with_disk_reserve(self) -> None:
        script = Path(__file__).parents[1] / "deploy" / "minimax_h3_ref2va.sh"
        output = subprocess.run(
            ["bash", str(script), "print-config"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        self.assertIn("variant=ref2va", output)
        self.assertIn("host=127.0.0.1", output)
        self.assertIn("gpus=0,1,2,3", output)
        self.assertIn("min_free_gib=100", output)
        self.assertNotIn("0.0.0.0", output)

        fl2va = subprocess.run(
            ["bash", str(script.parent / "start_h3_fl2va.sh"), "print-config"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("variant=fl2va", fl2va)
        self.assertIn("port=30010", fl2va)
        self.assertIn("gpus=4,5,6,7", fl2va)

        source = script.read_text(encoding="utf-8")
        self.assertIn('sglang[diffusion]==$H3_SGLANG_VERSION', source)
        self.assertIn("sgl-project.github.io/whl/cu129", source)
        self.assertIn("--ulysses-degree 4", source)
        self.assertNotIn("--tp-size 2", source)
        self.assertIn("H3_VENV must equal H3_ROOT/venv", source)
        self.assertNotIn("unsafe-best-match", source)
        self.assertIn("refusing to stop unverified pid", source)
        self.assertNotIn("stop it before switching variants", source)

    def test_validate_config_rejects_duplicate_gpu_ids(self) -> None:
        script = Path(__file__).parents[1] / "deploy" / "minimax_h3_ref2va.sh"
        environment = {**os.environ, "H3_GPU_LIST": "0,0,1,2"}

        result = subprocess.run(
            ["bash", str(script), "validate-config"],
            env=environment,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("four unique GPUs", result.stderr)

    def test_validate_config_rejects_venv_path_traversal(self) -> None:
        script = Path(__file__).parents[1] / "deploy" / "minimax_h3_ref2va.sh"
        environment = {
            **os.environ,
            "H3_ROOT": "/tmp/h3-safe-root",
            "H3_VENV": "/tmp/h3-safe-root/../victim",
        }

        result = subprocess.run(
            ["bash", str(script), "validate-config"],
            env=environment,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("H3_VENV must equal H3_ROOT/venv", result.stderr)


if __name__ == "__main__":
    unittest.main()
