from __future__ import annotations

from pathlib import Path
from typing import Protocol

from harness.core.case_spec import CaseSpec


def require_executable_case(case: CaseSpec) -> None:
    options = case.data.get("backend_options")
    if isinstance(options, dict) and options.get("execution_status") == "blocked":
        raise RuntimeError(str(options.get("blocked_reason") or "case execution is blocked by its contract"))


class RuntimeBackend(Protocol):
    name: str

    def run_case(self, case: CaseSpec, output_root: str | Path) -> Path:
        """Run a case and return the run directory."""
