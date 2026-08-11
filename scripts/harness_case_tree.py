#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "cases"
OUTPUT = CASES / "TREE.md"

NAVIGATION_DESCRIPTION = "历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。"
NAVIGATION_MEMORY = "运行时只读取对象、状态域、约束、求解器要求与通用断言。"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or verify cases/TREE.md from CaseSpec JSON files.")
    parser.add_argument("--check", action="store_true", help="Fail when cases/TREE.md is stale.")
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="Also maintain <workspace>/cases/TREE.md for actual run folders.",
    )
    args = parser.parse_args()
    rendered = render_tree()
    workspace_root = args.workspace_root
    if workspace_root is None and os.environ.get("SIM_HARNESS_WORKSPACE"):
        workspace_root = Path(os.environ["SIM_HARNESS_WORKSPACE"])
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.is_file() else ""
        if current != rendered:
            raise SystemExit("cases/TREE.md is stale; run scripts/harness_case_tree.py")
        if workspace_root is not None:
            check_workspace_tree(workspace_root)
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(OUTPUT)
    if workspace_root is not None:
        workspace_output = workspace_root / "cases" / "TREE.md"
        workspace_output.parent.mkdir(parents=True, exist_ok=True)
        workspace_output.write_text(render_workspace_tree(workspace_root), encoding="utf-8")
        print(workspace_output)
    return 0


def check_workspace_tree(workspace_root: Path) -> None:
    output = workspace_root / "cases" / "TREE.md"
    current = output.read_text(encoding="utf-8") if output.is_file() else ""
    expected = render_workspace_tree(workspace_root)
    if current != expected:
        raise SystemExit(
            f"{output} is stale; run scripts/harness_case_tree.py --workspace-root {workspace_root}"
        )


def render_tree() -> str:
    files = sorted(CASES.rglob("*.json"))
    directories = sorted({path.parent.relative_to(CASES).as_posix() for path in files})
    lines = [
        "# Case 目录导航（自动生成）",
        "",
        "> 生成命令：`python scripts/harness_case_tree.py`；CI/本地检查：`python scripts/harness_case_tree.py --check`。请勿手改本文件。",
        "",
        "## 三类位置",
        "",
        "- `repo/cases/`：可维护的 CaseSpec 与模板，是输入契约；不放 MP4、EXR、OBJ 或临时 run。",
        "- `$SIM_HARNESS_WORKSPACE/cases/<case_id>/<variant_id>/`：给人和下游工具使用的两层媒体目录；变体内固定为 `rgb/`、`depth/`、`segmentation/`、`overall/` 和 `variant.json`。",
        "- `$SIM_HARNESS_WORKSPACE/runs/`：内部 source run、日志、缓存与质量证据；历史路径名仅用于定位。",
        "- 本脚本只生成导航文档；规划器、backend 和 verifier 均不读取目录名来决定物理流程。",
        "",
        "## 目录树",
        "",
        "```text",
        "cases/",
        *tree_lines(files),
        "```",
        "",
        "## 文件夹说明",
        "",
        "| 文件夹 | 是什么 / 体现什么 | Harness 必须记住 |",
        "|---|---|---|",
    ]
    for directory in directories:
        description = NAVIGATION_DESCRIPTION
        memory = NAVIGATION_MEMORY
        lines.append(f"| `{directory}/` | {description} | {memory} |")
    lines.extend(["", "## 每个 Case / 模板", "", "| Case | 类型 | 兼容标签 | 说明 | Harness 必须记住 |", "|---|---|---|---|---|"])
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        relative = path.relative_to(CASES).as_posix()
        is_template = payload.get("schema_version") == "harness_case_template_v1"
        kind = "模板" if is_template else ("正向" if payload.get("should_pass") is True else "负向/边界")
        capability = str(payload.get("capability_id") or "-")
        description = str(payload.get("prompt") or payload.get("description") or payload.get("notes") or "-")
        remember = case_memory(payload, is_template)
        lines.append(
            f"| [`{escape(relative)}`]({escape(relative)}) | {kind} | `{escape(capability)}` | {escape(description)} | {escape(remember)} |"
        )
    lines.extend(
        [
            "",
            "## 维护规则",
            "",
            "1. 新增/删除/移动 CaseSpec 后必须重新生成本文件并运行 `--check`。",
            "2. `negative_*` 是 verifier 负例，不是待交付视频；它们必须失败才能证明关卡有效。",
            "3. 参数矩阵只做因果方向判断，不把不同参数条件评为画质 winner。",
            "4. 每个整理后的变体固定四个媒体目录；RGB-only 历史结果允许 depth/segmentation 为空，但 manifest 必须明确。正式完整 run 仍要求多机位 RGB/depth/segmentation 和三个 overall。",
            "5. CaseSpec 只定义初态、物理参数和期望事件；不得逐帧注入物体轨迹。",
            "",
        ]
    )
    return "\n".join(lines)


def render_workspace_tree(workspace_root: Path) -> str:
    cases_root = workspace_root / "cases"
    cases = workspace_cases(cases_root)
    lines = [
        "# 本地 Case 产出导航（自动生成）",
        "",
        "> 这里索引真实运行产物，不是 Git 输入契约。生成命令：",
        f"> `python scripts/harness_case_tree.py --workspace-root {workspace_root}`",
        "",
        "## 怎么读路径",
        "",
        "`cases/<case_id>/<variant_id>/` 只保留两层业务语义；每个变体的子目录固定：",
        "",
        "| 子目录 | 含义 | 规则 |",
        "|---|---|---|",
        "| `rgb/` | 每个机位一个 RGB MP4。 | 任何可整理变体都必须有。 |",
        "| `depth/` | 每个机位的 preview MP4 与 metric EXR frames。 | RGB-only 变体可为空；正式候选必须完整。 |",
        "| `segmentation/` | 每个机位的 preview MP4 与 instance EXR frames。 | RGB-only 变体可为空；正式候选必须完整。 |",
        "| `overall/` | 当前变体按模态合成的多机位总体视频。 | 只生成实际存在的模态。 |",
        "| `variant.json` | source run、route、机位、模态、质量门和 hardlink provenance。 | 不得省略。 |",
        "",
        "## 两层 Case 树",
        "",
        "```text",
        "cases/",
        *workspace_case_tree(cases),
        "```",
        "",
        "## 每个本地 Case",
        "",
        "| 路径 | 是什么 / 体现什么 | 当前内容 | Harness 必须记住 |",
        "|---|---|---|---|",
    ]
    for case_dir, manifest in cases:
        description = case_dir.name.replace("_", " ")
        variants = manifest.get("variants") if isinstance(manifest.get("variants"), list) else []
        contents = f"`{len(variants)}` 个变体"
        memory = NAVIGATION_MEMORY
        lines.append(f"| `{case_dir.name}/` | {description} | {contents} | {memory} |")
    lines.extend(
        [
            "",
            "## Harness 维护规则",
            "",
            "1. 每次正式 run、probe 清理、keep/reject 或新增版本后，重新生成本文件。",
            "2. 历史 route 仅作产物定位，不可参与 backend、solver 或 verifier 选择。",
            "3. smoke 默认 1280×720；candidate 默认 1920×1080 五机位三模态；publish 只在用户 keep 后以 3840×2160 运行。",
            "4. 破碎的 `fracture_center_source`、流体的 solver/cache/surface lineage、刚体的 contact provenance 都必须在 run 内有机器可读证据。",
            "5. 本文件只做导航，不替代 CaseSpec、manifest、verifier 或 review 状态。",
            "",
        ]
    )
    return "\n".join(lines)


def workspace_cases(cases_root: Path) -> list[tuple[Path, dict]]:
    if not cases_root.is_dir():
        return []
    rows = []
    for case_dir in sorted(path for path in cases_root.iterdir() if path.is_dir()):
        manifest_path = case_dir / "case.json"
        if not manifest_path.is_file():
            continue
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            rows.append((case_dir, payload))
    return rows


def workspace_case_tree(cases: list[tuple[Path, dict]]) -> list[str]:
    result: list[str] = []
    for case_index, (case_dir, manifest) in enumerate(cases):
        last_case = case_index == len(cases) - 1
        result.append(f"{'└── ' if last_case else '├── '}{case_dir.name}/")
        variants = manifest.get("variants") if isinstance(manifest.get("variants"), list) else []
        prefix = "    " if last_case else "│   "
        for variant_index, row in enumerate(variants):
            last_variant = variant_index == len(variants) - 1
            result.append(
                f"{prefix}{'└── ' if last_variant else '├── '}{row.get('id')}/"
            )
    return result


def tree_lines(files: list[Path]) -> list[str]:
    root: dict[str, dict] = {}
    for path in files:
        parts = path.relative_to(CASES).parts
        node = root
        for part in parts:
            node = node.setdefault(part, {})
    result: list[str] = []

    def visit(node: dict[str, dict], prefix: str) -> None:
        entries = sorted(node.items(), key=lambda item: (not item[1], item[0]))
        for index, (name, children) in enumerate(entries):
            last = index == len(entries) - 1
            result.append(f"{prefix}{'└── ' if last else '├── '}{name}{'/' if children else ''}")
            if children:
                visit(children, prefix + ("    " if last else "│   "))

    visit(root, "")
    return result


def case_memory(payload: dict, is_template: bool) -> str:
    keys = payload.get("expected_invariants") if is_template else payload.get("verification_assertions")
    if not keys:
        keys = payload.get("required_signals") or payload.get("expected_artifact_contract") or []
    values = [str(value) for value in keys if value]
    if len(values) > 6:
        values = [*values[:6], "…"]
    return ", ".join(values) if values else NAVIGATION_MEMORY


def escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
