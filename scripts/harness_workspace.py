from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.workspace import (
    WorkspaceError,
    bootstrap_workspace,
    build_ue_plugin,
    configure_ue_mount,
    init_workspace,
    prune_rejected,
    review_candidate,
    setup_doctor,
    workspace_status,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage local harness runs and review artifacts outside Git.")
    parser.add_argument("--workspace", "-w", help="Absolute workspace path; defaults to SIM_HARNESS_WORKSPACE or ~/SimulatorWorkspace/physics_aware_harness.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create the local workspace layout.")
    bootstrap = commands.add_parser("bootstrap", help="Initialize the workspace and optionally mount licensed UE assets.")
    bootstrap.add_argument("--adp-content", help="Operator-supplied Unreal Content directory.")
    doctor = commands.add_parser("doctor", help="Check contract-test and real-UE setup readiness.")
    doctor.add_argument("--ue-executable", help="Absolute UnrealEditor-Cmd path.")
    doctor.add_argument("--asset-content", help="Operator-supplied Unreal Content directory.")
    doctor.add_argument("--native-smoke-run", help="Hard-gate-passing native UE run under workspace/cases.")
    build_plugin = commands.add_parser("build-ue-plugin", help="Build and activate ADPPhysicsRuntime for this UE host.")
    build_plugin.add_argument("--ue-executable", required=True, help="Absolute UnrealEditor-Cmd path.")
    build_plugin.add_argument("--max-parallel-actions", type=int, default=4)
    commands.add_parser("status", help="Show workspace, review, and UE mount status.")
    mount = commands.add_parser("mount-ue", help="Create a content-only UE project with local asset links.")
    mount.add_argument("--adp-content", required=True, help="Path to the imported ADP Unreal Content directory.")
    review = commands.add_parser("review", help="Move one inbox candidate to kept or rejected.")
    review.add_argument("decision", choices=("keep", "reject"))
    review.add_argument("candidate", help="Direct child directory or video name in review/inbox.")
    prune = commands.add_parser("prune", help="List old rejected candidates; delete only with --apply.")
    prune.add_argument("--older-than-days", type=float, required=True)
    prune.add_argument("--apply", action="store_true", help="Delete the listed review/rejected candidates.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_workspace(args.workspace)
            result = {"action": "init", **workspace_status(args.workspace)}
        elif args.command == "bootstrap":
            bootstrap_workspace(args.workspace, adp_content=args.adp_content)
            result = {
                "action": "bootstrap",
                **workspace_status(args.workspace),
                "doctor": setup_doctor(
                    args.workspace,
                    asset_content=args.adp_content,
                ),
            }
        elif args.command == "doctor":
            result = setup_doctor(
                args.workspace,
                ue_executable=args.ue_executable,
                asset_content=args.asset_content,
                native_smoke_run=args.native_smoke_run,
            )
        elif args.command == "build-ue-plugin":
            result = build_ue_plugin(
                args.workspace,
                ue_executable=args.ue_executable,
                max_parallel_actions=args.max_parallel_actions,
            )
        elif args.command == "status":
            result = workspace_status(args.workspace)
        elif args.command == "mount-ue":
            result = {"action": "mount-ue", **configure_ue_mount(args.adp_content, args.workspace)}
        elif args.command == "review":
            destination = review_candidate(args.candidate, args.decision, args.workspace)
            result = {"action": args.decision, "destination": str(destination)}
        else:
            candidates = prune_rejected(args.older_than_days, args.workspace, dry_run=not args.apply)
            result = {
                "action": "prune",
                "dry_run": not args.apply,
                "count": len(candidates),
                "candidates": [str(candidate) for candidate in candidates],
            }
    except (WorkspaceError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
