from __future__ import annotations

import os
import subprocess
from pathlib import Path

from harness.core.artifact_schema import read_json, write_json
from harness.core.runtime_case import RuntimeCase
from harness.core.physics_contract import infer_scene_domain
from harness.core.workspace import workspace_root


ROOT = Path(__file__).resolve().parents[2]


class TaichiClothBackend:
    name = "taichi_cloth"

    def run_case(
        self,
        case: RuntimeCase,
        output_root: str | Path,
        **_: object,
    ) -> Path:
        if infer_scene_domain(case.data) != "deformable":
            raise ValueError("taichi_cloth requires a deformable-domain scene contract")
        run_dir = Path(output_root) / f"{case.case_id}_{self.name}"
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(run_dir / "case_spec.json", case.data)
        executable = taichi_python()
        command = [
            str(executable),
            str(ROOT / "scripts" / "harness_taichi_cloth.py"),
            "--case",
            str(run_dir / "case_spec.json"),
            "--output-dir",
            str(run_dir),
        ]
        if not executable.is_file():
            write_json(run_dir / "taichi_cloth_backend_report.json", {
                "schema_version": "harness_taichi_cloth_backend_report_v1",
                "status": "failed_unavailable",
                "case_id": case.case_id,
                "backend": self.name,
                "process_isolation": str(executable),
                "command": command,
                "returncode": None,
                "stderr": "Taichi environment missing",
            })
            raise RuntimeError(
                "Taichi environment missing. Set SIM_TAICHI_PYTHON or create "
                f"{workspace_root() / 'envs' / 'taichi'} with Python 3.11 and taichi==1.7.4."
            )
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
        verifier_path = run_dir / "harness_verifier.json"
        verification_status = read_json(verifier_path).get("status") if verifier_path.is_file() else "missing"
        status = "completed" if result.returncode == 0 and verification_status == "pass" else "failed"
        write_json(run_dir / "taichi_cloth_backend_report.json", {
            "schema_version": "harness_taichi_cloth_backend_report_v1",
            "status": status,
            "case_id": case.case_id,
            "capability_id": case.capability_id,
            "backend": self.name,
            "process_isolation": str(executable),
            "command": command,
            "returncode": result.returncode,
            "verification_status": verification_status,
            "stdout": result.stdout,
            "stderr": result.stderr,
        })
        if status != "completed":
            raise RuntimeError(f"Taichi cloth backend failed; see {run_dir / 'taichi_cloth_backend_report.json'}")
        return run_dir


def taichi_python() -> Path:
    configured = os.environ.get("SIM_TAICHI_PYTHON")
    if configured:
        return Path(configured).expanduser()
    return workspace_root() / "envs" / "taichi" / "bin" / "python"
