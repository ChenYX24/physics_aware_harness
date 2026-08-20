from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.artifact_schema import read_json, write_json
from harness.core.review_feedback import compile_review_feedback
from harness.core.workspace import workspace_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile review-board decisions into reusable Harness constraints.")
    parser.add_argument("decisions", help="Exported case-curation decision JSON.")
    parser.add_argument("--output", help="Defaults to review/learned_constraints/active_review_feedback.json.")
    parser.add_argument("--weekly-json", help="Defaults next to --output as weekly_candidates.json.")
    parser.add_argument("--weekly-markdown", help="Defaults next to --output as weekly_candidates.md.")
    parser.add_argument(
        "--verified-execution-statuses",
        help="Optional JSON object from a trusted manifest/gate verifier; browser-reported status is not regression evidence.",
    )
    args = parser.parse_args()
    output = Path(args.output) if args.output else workspace_root() / "review/learned_constraints/active_review_feedback.json"
    verified = read_json(args.verified_execution_statuses) if args.verified_execution_statuses else None
    compiled = compile_review_feedback(read_json(args.decisions), verified_execution_statuses=verified)
    weekly_json = Path(args.weekly_json) if args.weekly_json else output.with_name("weekly_candidates.json")
    weekly_markdown = Path(args.weekly_markdown) if args.weekly_markdown else output.with_name("weekly_candidates.md")
    write_json(output, compiled)
    write_json(weekly_json, {
        "schema_version": "harness_weekly_candidates_v1",
        "source_catalog_sha256": compiled.get("source_catalog_sha256"),
        "cases": compiled["weekly_candidates"],
    })
    weekly_markdown.parent.mkdir(parents=True, exist_ok=True)
    weekly_markdown.write_text(render_weekly_markdown(compiled["weekly_candidates"]), encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "weekly_json": str(weekly_json),
        "weekly_markdown": str(weekly_markdown),
        "weekly_candidate_count": len(compiled["weekly_candidates"]),
        "rule_count": len(compiled["rules"]),
        "pending_diagnosis_count": len(compiled["pending_diagnosis"]),
    }, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def render_weekly_markdown(cases: list[dict]) -> str:
    lines = [
        "# 周报候选（由审阅板保留决定生成）",
        "",
        "> 这里只收录用户在全量审阅板中选择“保留”的 case；通过、失败和 legacy 状态不会被混写。",
        "",
    ]
    for case in cases:
        lines.extend([
            f"## {case['title']}",
            "",
            f"- Case：`{case['case_id']}`",
            f"- 运行状态：`{case['execution_status']}`",
            f"- 路由：`{case.get('case_route') or '-'}`",
            f"- 问题标签：{', '.join(case.get('issue_ids') or []) or '无'}",
            f"- 反馈：{case.get('feedback') or '无'}",
            "- 保留视频：",
            "",
        ])
        artifacts = case.get("artifacts") or []
        lines.extend(
            f"  - `{item.get('label') or item['artifact_id']}`：`{item.get('path') or '-'}`"
            for item in artifacts
        )
        if not artifacts:
            lines.append("  - 无单视频保留项")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
