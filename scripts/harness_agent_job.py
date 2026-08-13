from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.agent.job_controller import AgentJobController
from harness.assets.providers.input_manifest import build_provider_input_manifest
from harness.core.artifact_schema import read_json
from harness.core.workspace import workspace_root
from harness.planning.case_generation import build_case_request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persistent single-task Harness Agent Job Controller")
    parser.add_argument("--workspace", help="External Harness workspace; defaults to SIM_HARNESS_WORKSPACE.")
    parser.add_argument("--jsonl", action="store_true", help="Stream versioned controller events as JSON Lines.")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="Freeze a request and create a durable job.")
    source = create.add_mutually_exclusive_group(required=True)
    source.add_argument("--request", help="Existing harness_case_request_v1 JSON.")
    source.add_argument("--prompt", help="Natural-language physics video request.")
    create.add_argument("--image", action="append", default=[])
    create.add_argument("--case-id", default="generated_case")
    create.add_argument("--backend", choices=["fallback", "genesis_fem", "genesis_sph", "taichi_cloth", "ue"])
    create.add_argument("--allow-planning-image-upload", action="store_true")
    create.add_argument("--planning-images-required", action="store_true")
    create.add_argument("--allow-meshy-upload", action="store_true")
    create.add_argument("--allow-external-provider", action="store_true")
    create.add_argument("--allow-paid-provider", action="store_true")
    create.add_argument("--allow-semantic-reviewer-image-upload", action="store_true")
    create.add_argument("--publication-tier", choices=["diagnostic_only", "local_preview", "reference"], default="reference")
    create.add_argument("--budget", help="JSON object overriding Controller budget defaults.")
    create.add_argument("--seed-case-spec", help="Validated CaseSpec V2 fixture; skips LLM generation but remains audited.")
    create.add_argument("--job-id")

    for name in ("inspect", "advance-until-blocked", "review", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("job_id")

    revise = commands.add_parser("apply-revision", help="Materialize an Intent-authorized automatic revision proposal.")
    revise.add_argument("job_id")
    revise.add_argument("--revised-case-spec", required=True)
    revise.add_argument("--revision-reason", required=True)

    resume = commands.add_parser("resume")
    resume.add_argument("job_id")
    resume.add_argument("--budget-extension-seconds", type=int, default=0)
    resume.add_argument("--max-paid-submissions", type=int)
    resume.add_argument("--allow-planning-image-upload", action="store_true")
    resume.add_argument("--allow-meshy-upload", action="store_true")
    resume.add_argument("--allow-external-provider", action="store_true")
    resume.add_argument("--allow-paid-provider", action="store_true")
    resume.add_argument("--allow-semantic-reviewer-image-upload", action="store_true")
    resume.add_argument("--revised-case-spec")
    resume.add_argument("--revision-reason")
    resume.add_argument("--intent-amendment", help="JSON ambiguity-resolution amendment.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    signal.signal(signal.SIGTERM, _interrupt)
    sink = (lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)) if args.jsonl else None
    controller = AgentJobController(args.workspace, event_sink=sink)
    if args.command == "create":
        if args.request:
            request = read_json(args.request)
        else:
            request = build_case_request(
                case_id=args.case_id,
                text=args.prompt,
                image_paths=args.image,
                allow_image_upload=args.allow_planning_image_upload,
                planning_images_required=args.planning_images_required,
                requested_backend=args.backend,
            )
        provider_manifest = build_provider_input_manifest(
            request.get("inputs") or [],
            workspace=workspace_root(args.workspace),
            meshy_upload_authorized=args.allow_meshy_upload,
        )
        budget = _json_object(args.budget) if args.budget else {}
        if args.allow_paid_provider and "max_paid_submissions" not in budget:
            budget["max_paid_submissions"] = 1
        result = controller.create(
            request,
            provider_input_manifest=provider_manifest,
            job_id=args.job_id,
            budget=budget,
            authorizations={
                "planning_llm_upload": args.allow_planning_image_upload,
                "meshy_upload": args.allow_meshy_upload,
                "external_provider": args.allow_external_provider,
                "paid_provider_submission": args.allow_paid_provider,
                "semantic_reviewer_image_upload": args.allow_semantic_reviewer_image_upload,
            },
            publication_tier=args.publication_tier,
            seed_case_spec=read_json(args.seed_case_spec) if args.seed_case_spec else None,
        )
    elif args.command == "inspect":
        result = controller.inspect(args.job_id)
    elif args.command == "advance-until-blocked":
        result = controller.advance_until_blocked(args.job_id)
    elif args.command == "review":
        result = controller.run_semantic_review(args.job_id)
    elif args.command == "apply-revision":
        result = controller.apply_revision_proposal(
            args.job_id,
            read_json(args.revised_case_spec),
            reason=args.revision_reason,
        )
    elif args.command == "resume":
        authorizations = {}
        for field, enabled in (
            ("planning_llm_upload", args.allow_planning_image_upload),
            ("meshy_upload", args.allow_meshy_upload),
            ("external_provider", args.allow_external_provider),
            ("paid_provider_submission", args.allow_paid_provider),
            ("semantic_reviewer_image_upload", args.allow_semantic_reviewer_image_upload),
        ):
            if enabled:
                authorizations[field] = True
        result = controller.resume(
            args.job_id,
            budget_extension_seconds=args.budget_extension_seconds,
            max_paid_submissions=args.max_paid_submissions,
            authorizations=authorizations,
            intent_amendment=read_json(args.intent_amendment) if args.intent_amendment else None,
            revised_case_spec=read_json(args.revised_case_spec) if args.revised_case_spec else None,
            revision_reason=args.revision_reason,
        )
    else:
        result = controller.cancel(args.job_id)
    if not args.jsonl:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _json_object(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("budget must be a JSON object")
    return decoded


def _interrupt(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt()


if __name__ == "__main__":
    raise SystemExit(main())
