from __future__ import annotations

import subprocess
from pathlib import Path

from harness.core.artifact_schema import read_json, write_json
from harness.core.case_spec import CaseSpec
from harness.core.physics_contract import infer_scene_domain
from harness.runtime.genesis_sph_backend import genesis_python


ROOT = Path(__file__).resolve().parents[2]


class GenesisFEMBackend:
    name = "genesis_fem"

    def run_case(self, case: CaseSpec, output_root: str | Path, **_: object) -> Path:
        if infer_scene_domain(case.data) != "deformable":
            raise ValueError("genesis_fem requires a deformable-domain scene contract")
        run_dir = Path(output_root) / f"{case.case_id}_{self.name}"
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(run_dir / "case_spec.json", case.data)
        executable = genesis_python()
        command = [
            str(executable),
            str(ROOT / "scripts" / "harness_genesis_fem.py"),
            "--case", str(run_dir / "case_spec.json"),
            "--output-dir", str(run_dir),
        ]
        if not executable.is_file():
            raise RuntimeError(f"Genesis environment missing: {executable}")
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
        verifier = read_json(run_dir / "harness_verifier.json") if (run_dir / "harness_verifier.json").is_file() else {}
        status = "completed" if result.returncode == 0 and verifier.get("status") == "pass" else "failed"
        write_json(run_dir / "genesis_fem_backend_report.json", {
            "schema_version": "harness_genesis_fem_backend_report_v1",
            "status": status,
            "case_id": case.case_id,
            "backend": self.name,
            "process_isolation": str(executable),
            "command": command,
            "returncode": result.returncode,
            "verification_status": verifier.get("status", "missing"),
            "stdout": result.stdout,
            "stderr": result.stderr,
        })
        if status != "completed":
            raise RuntimeError(f"Genesis FEM backend failed; see {run_dir / 'genesis_fem_backend_report.json'}")
        return run_dir
