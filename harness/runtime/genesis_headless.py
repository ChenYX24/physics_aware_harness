from __future__ import annotations

import importlib
import sys
from types import ModuleType


def import_headless_genesis() -> ModuleType:
    """Import Genesis without allowing its optional macOS viewer to create Tk."""
    sys.modules["tkinter"] = None
    return importlib.import_module("genesis")
