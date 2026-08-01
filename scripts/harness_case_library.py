from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.artifact_schema import read_json, write_json
from harness.core.case_library import (
    CaseLibraryError,
    catalog_case_plan,
    create_variant_plan,
    load_variant_plan,
    materialize_variant,
    migrate_workspace_case_layout,
    organize_workspace_cases,
    variant_render_command,
    write_case_editor,
)
from harness.core.workspace import case_output_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan editable CaseSpec variants, render them without an LLM, and organize runtime media."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    catalog = commands.add_parser("catalog", help="Show the bounded workbook variable plan for one case.")
    catalog.add_argument("case_id")

    plan = commands.add_parser("plan", help="Create a full variable-space plan with a small OFAT render set.")
    plan.add_argument("--base-case", required=True)
    plan.add_argument("--case-route", required=True)
    plan.add_argument("--axes", required=True, help="JSON list, or an object containing an axes list.")
    plan.add_argument("--output", required=True)

    show = commands.add_parser("show", help="Print one validated variant plan.")
    show.add_argument("plan")

    editor = commands.add_parser("editor", help="Build a standalone HTML editor for one variant plan.")
    editor.add_argument("plan")
    editor.add_argument("--output", help="Defaults to the plan path with an .html suffix.")

    materialize = commands.add_parser("materialize", help="Write selected CaseSpecs without rendering.")
    materialize.add_argument("plan")
    materialize.add_argument("--variant", action="append", help="Repeat to select variants; defaults to all selected.")
    materialize.add_argument("--output-dir", required=True)

    render = commands.add_parser(
        "render",
        help="Materialize and render one variant; defaults to five-view RGB and needs no LLM.",
    )
    render.add_argument("plan")
    render.add_argument("--variant", required=True)
    render.add_argument("--workspace", help="Absolute SIM_HARNESS_WORKSPACE override.")
    render.add_argument("--output-case", help="Materialized CaseSpec path; defaults inside the case workspace.")
    render.add_argument(
        "--render-passes",
        help="Comma-separated rgb/depth/segmentation; defaults to rgb, or all three with --formal.",
    )
    render.add_argument(
        "--views",
        default="front_static,side_static,top_down,tracking_subject,event_closeup",
        help="Comma-separated cameras for a non-formal render.",
    )
    render.add_argument(
        "--formal",
        action="store_true",
        help="Use the complete five-view, three-modality hard-gate iterator.",
    )
    render.add_argument("--max-attempts", type=int)
    render.add_argument("--lighting-presets")
    render.add_argument("--stop-on-first-pass", action="store_true")

    migrate = commands.add_parser(
        "migrate",
        help="Move legacy nested runtime routes into internal runs/case_routes storage.",
    )
    migrate.add_argument("--workspace", required=True)
    migrate.add_argument("--apply", action="store_true", help="Move routes; default is dry-run.")

    organize = commands.add_parser(
        "organize",
        help="Build flat cases/<case>/<variant> media views using hardlinks.",
    )
    organize.add_argument("--workspace", required=True)
    organize.add_argument("--route", action="append", help="Repeat to limit organization to exact case routes.")
    organize.add_argument("--apply", action="store_true", help="Create the hardlinked view; default is dry-run.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "catalog":
            result = catalog_case_plan(args.case_id)
        elif args.command == "plan":
            axes_payload = read_json(args.axes)
            axes = axes_payload.get("axes") if isinstance(axes_payload, dict) else axes_payload
            plan = create_variant_plan(
                args.base_case,
                case_route=args.case_route,
                axes=axes,
            )
            write_json(args.output, plan)
            editor_output = write_case_editor(args.output)
            result = {
                "action": "plan",
                "output": str(Path(args.output).resolve()),
                "editor": str(editor_output),
                **plan,
            }
        elif args.command == "show":
            result = load_variant_plan(args.plan)
        elif args.command == "editor":
            output = write_case_editor(args.plan, args.output)
            result = {"action": "editor", "plan": str(Path(args.plan).resolve()), "output": str(output)}
        elif args.command == "materialize":
            plan = load_variant_plan(args.plan)
            variants = args.variant or [row["id"] for row in plan["selected_variants"]]
            output_dir = Path(args.output_dir).expanduser().resolve(strict=False)
            outputs = []
            for variant in variants:
                output = output_dir / f"{variant}.json"
                materialize_variant(args.plan, variant, output)
                outputs.append(str(output))
            result = {"action": "materialize", "count": len(outputs), "outputs": outputs}
        elif args.command == "render":
            plan = load_variant_plan(args.plan)
            workspace = Path(args.workspace).expanduser().resolve(strict=False) if args.workspace else None
            output = (
                Path(args.output_case).expanduser().resolve(strict=False)
                if args.output_case
                else case_output_root(plan["case_route"], workspace)
                / "inputs"
                / "variants"
                / f"{args.variant}.json"
            )
            passes = (
                tuple(item.strip() for item in args.render_passes.split(",") if item.strip())
                if args.render_passes
                else None
            )
            command = variant_render_command(
                args.plan,
                args.variant,
                output,
                formal=args.formal,
                render_passes=passes,
                views=args.views,
            )
            if args.max_attempts is not None:
                if not args.formal:
                    parser.error("--max-attempts requires --formal")
                command.extend(("--max-attempts", str(args.max_attempts)))
            if args.lighting_presets:
                if not args.formal:
                    parser.error("--lighting-presets requires --formal")
                command.extend(("--lighting-presets", args.lighting_presets))
            if args.stop_on_first_pass:
                if not args.formal:
                    parser.error("--stop-on-first-pass requires --formal")
                command.append("--stop-on-first-pass")
            env = os.environ.copy()
            if workspace is not None:
                env["SIM_HARNESS_WORKSPACE"] = str(workspace)
            completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
            result = {
                "action": "render",
                "variant": args.variant,
                "materialized_case": str(output),
                "formal": args.formal,
                "render_passes": list(
                    passes
                    or (("rgb", "depth", "segmentation") if args.formal else ("rgb",))
                ),
                "returncode": completed.returncode,
            }
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return completed.returncode
        elif args.command == "migrate":
            result = migrate_workspace_case_layout(args.workspace, apply=args.apply)
        else:
            result = organize_workspace_cases(
                args.workspace,
                routes=args.route,
                apply=args.apply,
            )
    except (CaseLibraryError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
