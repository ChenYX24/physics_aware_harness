from __future__ import annotations

from pathlib import Path
from typing import Protocol

from harness.core.runtime_case import RuntimeCase


class RuntimeBackend(Protocol):
    name: str

    def run_case(self, case: RuntimeCase, output_root: str | Path) -> Path:
        """Run a case and return the run directory."""
