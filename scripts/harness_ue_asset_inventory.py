#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.assets.asset_registry import AssetRegistry
from harness.assets.ue_asset_inventory import register_ue_asset_inventory
from harness.core.artifact_schema import read_json, write_json


UE_SCRIPT = ROOT / "scripts" / "native_ue_asset_inventory.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan UE StaticMeshes and register their real runtime bindings in Catalog.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan")
    scan.add_argument("--package-root", action="append", required=True)
    scan.add_argument("--result", required=True)
    scan.add_argument("--ue-project", required=True)
    scan.add_argument("--ue-executable", required=True)
    scan.add_argument("--timeout", type=float, default=900.0)
    register = subparsers.add_parser("register")
    register.add_argument("--scan", required=True)
    register.add_argument("--catalog", required=True)
    register.add_argument("--receipt", required=True)
    register.add_argument("--source-uri-root", required=True)
    register.add_argument("--source-name", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "scan":
        result = run_scan(
            package_roots=args.package_root,
            result_path=Path(args.result),
            ue_project=Path(args.ue_project),
            ue_executable=Path(args.ue_executable),
            timeout_s=float(args.timeout),
        )
    else:
        result = register_ue_asset_inventory(
            args.scan,
            registry=AssetRegistry(args.catalog),
            receipt_path=args.receipt,
            source_uri_root=args.source_uri_root,
            source_name=args.source_name,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"pass", "registered"} else 2


def run_scan(
    *,
    package_roots: list[str],
    result_path: Path,
    ue_project: Path,
    ue_executable: Path,
    timeout_s: float,
) -> dict[str, object]:
    project = ue_project.expanduser().resolve()
    executable = ue_executable.expanduser().resolve()
    result = result_path.expanduser().resolve()
    if not project.is_file() or not executable.is_file():
        raise ValueError("UE project and executable must be materialized files")
    request_path = result.parent / f"{result.stem}_request.json"
    write_json(
        request_path,
        {
            "schema_version": "harness_ue_asset_inventory_scan_request_v1",
            "package_roots": package_roots,
            "project_content_root": str(project.parent / "Content"),
        },
    )
    result.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "SIM_HARNESS_UE_ASSET_SCAN_REQUEST": str(request_path),
            "SIM_HARNESS_UE_ASSET_SCAN_RESULT": str(result),
        }
    )
    command = [
        str(executable), f"-project={project}", "-RenderOffScreen", "-unattended", "-nosplash",
        "-NoScreenMessages", "-stdout", "-FullStdOutLogOutput", f"-ExecutePythonScript={UE_SCRIPT}",
    ]
    result.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment, shell=False)
    started = time.monotonic()
    try:
        while process.poll() is None and not result.is_file():
            if time.monotonic() - started > timeout_s:
                process.terminate()
                raise TimeoutError(f"UE asset inventory scan exceeded {timeout_s:g}s")
            time.sleep(0.25)
        if result.is_file():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        elif process.returncode:
            raise RuntimeError(f"UnrealEditor-Cmd exited with {process.returncode} before writing the scan result")
    finally:
        if process.poll() is None:
            process.terminate()
    return read_json(result)


if __name__ == "__main__":
    raise SystemExit(main())
