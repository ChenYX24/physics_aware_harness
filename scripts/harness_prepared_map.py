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
from harness.assets.prepared_map_input import prepare_map_input, qualify_map_input
from harness.core.artifact_schema import read_json, write_json


UE_SCRIPT = ROOT / "scripts" / "native_ue_map_qualifier.py"
DEFAULT_UE_EXECUTABLE = Path("/Users/Shared/Epic Games/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize, register, and qualify an explicit prepared Unreal Map.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--source-content-root", required=True)
    prepare.add_argument("--map-package", required=True)
    prepare.add_argument("--ue-project", required=True)
    prepare.add_argument("--registration-root", required=True)
    prepare.add_argument("--catalog", required=True)
    prepare.add_argument("--source-uri")
    prepare.add_argument("--license", default="UNVERIFIED_LOCAL_ENTITLEMENT")
    prepare.add_argument("--license-tier", choices=["local_preview", "reference"], default="local_preview")

    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--registration", required=True)
    smoke.add_argument("--result", required=True)
    smoke.add_argument("--ue-executable", default=str(DEFAULT_UE_EXECUTABLE))
    smoke.add_argument("--timeout", type=float, default=600.0)

    qualify = subparsers.add_parser("qualify")
    qualify.add_argument("--registration", required=True)
    qualify.add_argument("--qualification", required=True)
    qualify.add_argument("--catalog", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "prepare":
        result = prepare_map_input(
            args.source_content_root,
            map_package=args.map_package,
            ue_project=args.ue_project,
            registration_root=args.registration_root,
            registry=AssetRegistry(args.catalog),
            source_uri=args.source_uri,
            license_name=args.license,
            license_tier=args.license_tier,
        )
    elif args.command == "smoke":
        result = run_smoke(
            Path(args.registration),
            result_path=Path(args.result),
            ue_executable=Path(args.ue_executable),
            timeout_s=float(args.timeout),
        )
    else:
        result = qualify_map_input(
            args.registration,
            args.qualification,
            registry=AssetRegistry(args.catalog),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def run_smoke(
    registration_path: Path,
    *,
    result_path: Path,
    ue_executable: Path,
    timeout_s: float,
) -> dict[str, object]:
    registration = read_json(registration_path.expanduser().resolve())
    ue_project = Path(registration["ue_project"]).expanduser().resolve()
    executable = ue_executable.expanduser().resolve()
    if not executable.is_file():
        raise ValueError(f"UnrealEditor-Cmd is missing: {executable}")
    if not ue_project.is_file():
        raise ValueError(f"UE project is missing: {ue_project}")
    result = result_path.expanduser().resolve()
    result.parent.mkdir(parents=True, exist_ok=True)
    request_path = result.parent / "ue_map_qualification_request.json"
    request = {
        "schema_version": "harness_prepared_map_qualification_request_v1",
        "asset_id": registration["asset_id"],
        "map_package": registration["map_package"],
        "map_file": registration["materialized_map_file"],
        "map_sha256": registration["map_sha256"],
        "bundle_tree_sha256": registration["bundle_inventory"]["tree_sha256"],
        "ue_project": str(ue_project),
    }
    write_json(request_path, request)
    result.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "SIM_HARNESS_UE_MAP_QUALIFICATION_REQUEST": str(request_path),
            "SIM_HARNESS_UE_MAP_QUALIFICATION_RESULT": str(result),
        }
    )
    command = [
        str(executable),
        f"-project={ue_project}",
        "-RenderOffScreen",
        "-unattended",
        "-nosplash",
        "-NoScreenMessages",
        "-stdout",
        "-FullStdOutLogOutput",
        f"-ExecutePythonScript={UE_SCRIPT}",
    ]
    started = time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment, shell=False)
    try:
        while process.poll() is None and not result.is_file():
            if time.monotonic() - started > timeout_s:
                process.terminate()
                raise TimeoutError(f"prepared Map UE smoke exceeded {timeout_s:g}s")
            time.sleep(0.25)
        if result.is_file():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        elif process.returncode:
            raise RuntimeError(f"UnrealEditor-Cmd exited with {process.returncode} before writing a qualification receipt")
    finally:
        if process.poll() is None:
            process.terminate()
    return read_json(result)


if __name__ == "__main__":
    raise SystemExit(main())
