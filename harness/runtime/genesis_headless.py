from __future__ import annotations

import importlib
import sys
from types import ModuleType


def import_headless_genesis() -> ModuleType:
    """Import Genesis without allowing its optional macOS viewer to create Tk."""
    sys.modules["tkinter"] = None
    genesis = importlib.import_module("genesis")
    cpuinfo = sys.modules.get("cpuinfo")
    get_cpu_info = getattr(cpuinfo, "get_cpu_info", None)
    if callable(get_cpu_info):
        info = get_cpu_info()
        if not any(info.get(key) for key in ("brand_raw", "hardware_raw", "vendor_id_raw")):
            def get_cpu_info_with_label() -> dict:
                result = dict(get_cpu_info())
                result["brand_raw"] = "CPU"
                return result

            cpuinfo.get_cpu_info = get_cpu_info_with_label
    return genesis
