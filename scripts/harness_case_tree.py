#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "cases"
OUTPUT = CASES / "TREE.md"

FOLDER_DESCRIPTIONS = {
    "agent_action": "Agent 动作与刚体因果：动作发生后目标才允许运动。",
    "billiards": "台球/多球刚体碰撞：验证被动球静止、接触传播、速度与角度响应。",
    "bounce": "恢复系数与反弹：验证接触、回弹方向和能量不凭空增加。",
    "bowling": "保龄球链式碰撞：验证球到瓶的接触传播。",
    "constraint": "刚性约束与摆：验证约束长度、连续运动和禁止瞬移。",
    "domino": "多米诺顺序传播：只给初态，后续倾倒与接触顺序由 solver 产生。",
    "elastic_constraint": "弹性绳/蹦极：验证伸长上限、回弹和约束 trace。",
    "elastic_launch": "弹簧发射：验证储能、释放事件、发射响应与能量边界。",
    "falling": "重力落体：验证下落、地面接触和堆叠。",
    "field_force": "非接触场力：只定义初态与力场参数，轨迹必须由 solver 积分产生。",
    "field_force/magnetic": "磁力吸引/排斥：验证力方向、有限作用范围、终态稳定和 force trace。",
    "fluid": "流体粒子动力学：solver 粒子真值、表面重建、UE/Blender 渲染与传感器。",
    "fluid/fluid_drop_height_matrix": "流体落高 OFAT：只改变初始高度，比较触底时间与碰前速度。",
    "fluid/container_fill_stirring": "容器内搅拌：预填充后只初始化一次旋涡速度场，之后由 Genesis 自主演化。",
    "fluid/container_to_container_transfer": "刚体-SPH 场景：v001 验证有限液柱，v002 以真实资产、声明式轴对称碰撞和刚体运动验证统一求解契约。",
    "fluid/fountain": "喷泉：v001 是有限竖直喷流脉冲；连续 emitter 必须先完成 active-particle lineage。",
    "fluid/drop_in_liquid": "深水容器中的流固耦合：静水预滚动后一次释放，按有效密度验证上浮、下沉与入水水花；workspace v003 使用真实方形石质 Planter_A。",
    "fracture": "脆性破碎：验证先碰撞、再过能量门、再破碎。",
    "fracture/glass_energy_response_matrix": "玻璃 4/16/36 J 响应：重力弹道、原生撞点中心、裂纹/碎裂/爆开。",
    "fracture/glass_impact_position_matrix": "玻璃撞点 OFAT：固定 16 J，只改变左/右目标点并绑定对应的径向预切资产。",
    "fracture/steel_ball_board_energy_matrix": "钢球撞板 2/8/18 J：低能不碎、高能才碎。",
    "impulse_chain": "冲量链/牛顿摆：验证接触顺序与末端响应。",
    "magnetic": "磁力吸引/排斥：验证方向、标签与磁力响应。",
    "mass_ratio": "质量比碰撞：验证轻重物体速度次序与动量边界。",
    "projectile": "抛体：验证重力弧线、顶点与落地接触。",
    "ramp": "斜坡摩擦：验证滚/滑、摩擦敏感性及禁止无力上坡。",
    "rolling": "滚动摩擦：验证滚动减速和高摩擦短行程。",
    "sliding": "滑动/静摩擦：验证减速与静摩擦阈值。",
    "soft_body": "软体与布料：外部 solver 产生固定拓扑顶点真值，再由 UE 回放网格并采集传感器。",
    "soft_body/cloth_drape": "布料覆盖刚体：验证重力下落、包覆、拉伸上限、碰撞非穿透和事件尾段稳定。",
    "soft_body/elastic_collision": "体积软体碰撞：验证四面体 FEM 的压缩、体积保持、接触、回弹和刚度因果方向。",
    "soft_body/flag_wind": "固定点旗帜受风：验证固定边稳定、三角面风压响应、拉伸上限和多模态同步。",
    "spin": "角阻尼：验证自旋衰减且不允许角速度凭空增加。",
    "templates": "Case 模板，不直接运行；定义参数范围、负例模式和默认不变量。",
    "wind": "风场漂移：验证风向、受力标签与位移方向。",
}

WORKSPACE_CASE_DESCRIPTIONS = {
    "rigid_collision/billiards/v001_approach_angle_matrix_rgb_reference": "用户最早认可的五角度台球 RGB 参考与当前 Harness 复现入口。",
    "rigid_collision/billiards/v002_complete_angle_matrix": "五角度完整台球矩阵；包含 RGB、depth、instance segmentation 与多机位。",
    "rigid_collision/billiards/v003_speed_matrix": "固定角度、只改变开球速度的 OFAT 因果矩阵。",
    "rigid_collision/billiards/v004_mass_restitution_friction": "质量、恢复系数、摩擦参数的后续矩阵占位；不能与速度矩阵混作质量排名。",
    "rigid_collision/billiards/v005_angle_extension": "更大入射角扩展占位，用于验证接触传播与边库响应。",
    "rigid_collision/domino/v001_true_chaos_chain": "五骨牌真实 UE Chaos 连锁倾倒回归基线。",
    "rigid_collision/domino/v002_six_domino_terminal_propagation": "六骨牌终端传播 diagnostic；验证末牌被接触后继续掉落。",
    "brittle_fracture/steel_ball_board/v001_energy_matrix": "钢球撞板 2/8/18 J 能量门矩阵；低能不碎、高能破碎。",
    "brittle_fracture/glass_panel/v001_energy_response": "旧玻璃材质 proxy 三档；保留历史对比，不作为原生玻璃拓扑证据。",
    "brittle_fracture/glass_panel/v002_native_gc_energy_response": "Harness 生成的真实玻璃 Geometry Collection，4/16/36 J 五机位三模态候选。",
    "brittle_fracture/glass_panel/v003_impact_centered_ballistic": "重力弹道钢球 + 原生撞点 strain + 撞点附近径向预切玻璃；当前为 smoke。",
    "brittle_fracture/glass_panel/v004_impact_position_matrix": "固定 16 J 的左/中/右撞点矩阵；五机位三模态，已由用户 keep。",
    "fluid_dynamics/fluid_drop_in_basin/v001_genesis_surface_reconstruction": "Genesis SPH 粒子真值到表面重建、再到 UE replay 的技术竖切；当前视觉/分割/运动机位门拒绝。",
    "fluid_dynamics/fluid_drop_in_basin/v002_genesis_native_render": "同一 Genesis/OBJ cache 的原生 Rasterizer 重放；当前单机位 RGB/depth/instance smoke 已通过。",
    "fluid_dynamics/drop_in_liquid/v001_rubber_vs_lead": "同一预填充水槽的皮球/铅球入水；验证水花、上浮与下沉。",
    "fluid_dynamics/drop_in_liquid/v002_deep_tank": "加深水体、延长静水预滚动和事件尾段的皮球/铅球入水；Genesis 两机位三模态待 review，UE RGB 仍 blocked。",
    "fluid_dynamics/drop_in_liquid/v003_stone_planter": "真实方形石质 Planter_A 与显式非等比尺度的 UE 五机位三模态皮球/铅球入水；已由用户 keep。",
    "soft_body/cloth_drape/v001_taichi_cloth_over_sphere": "Taichi 固定拓扑布料落到偏心刚性球体；NPZ 是 solver truth，逐帧 OBJ/UE StaticMesh 是回放代理。",
    "soft_body/flag_wind/v001_taichi_pinned_flag_wind": "Taichi 固定左边界旗帜受阵风；顶点/固定点是 solver truth，UE 回放杆、地面和多模态。",
    "soft_body/flag_wind/v002_wind_speed_ofat": "旗帜风速 3.0/5.5/6.5 m/s OFAT；三档保持同材质、质量、seed、时基和相机，验证风向位移单调增加。",
    "soft_body/elastic_collision/v001_youngs_modulus_ofat": "Genesis FEM 软球撞地，50/100/200 kPa 只改杨氏模量；canonical tetrahedral state 经 UE surface replay 输出五机位三模态。",
    "field_force/magnetic/v001_attract_repel": "MuJoCo 有限范围磁力吸引/排斥初态实验；真实资产经 UE 回放并输出五机位三模态。",
}

WORKSPACE_FAMILY_MEMORY = {
    "rigid_collision/billiards": "只初始化球位姿/速度/材质参数；碰撞传播必须来自 Chaos；完整 run 要有多机位三模态和 run overall。",
    "rigid_collision/domino": "只允许首牌初态触发；每一段接触与倾倒顺序必须来自 solver，禁止逐帧 transform。",
    "brittle_fracture/steel_ball_board": "碰前能量样本必须早于接触；能量门、strain 命令和 fracture event 必须闭环。",
    "brittle_fracture/glass_panel": "区分 proxy、原生 GC、预切拓扑与运行时撞点；玻璃材质不能冒充玻璃破碎资产。",
    "fluid_dynamics/fluid_drop_in_basin": "粒子/cache 是 solver truth，surface mesh 是交换层；RGB 可见、metric depth、instance ID 与运动机位必须分别过门。",
    "fluid_dynamics/drop_in_liquid": "刚体轨迹和流体表面均来自同一次双向耦合求解；圆碗/洗手池不得套方形 solver cache；必须记录 solver/asset geometry、scale lineage、RGB/depth/instance 同步和每 run 三个 overall。",
    "soft_body/cloth_drape": "顶点 NPZ 是 canonical truth；OBJ/StaticMesh/Geometry Cache 只是 UE render proxy；必须检查固定拓扑、边长拉伸、刚体/地面穿透、末速和多模态时基。",
    "soft_body/elastic_collision": "四面体顶点/单元是 solver truth；表面 OBJ/UE mesh 只是 render proxy；刚度 OFAT 以最大压缩严格下降为因果门。",
    "soft_body/flag_wind": "固定点不可漂移；风响应必须沿声明轴；风速 OFAT 需至少三档且位移方向单调；OBJ/UE mesh 只是 canonical NPZ 的回放代理。",
    "field_force/magnetic": "磁力 case 只声明 source、subject、初态和力场参数；每帧刚体状态与 force trace 必须来自 solver，吸引/排斥是因果模式，不互相做质量排名。",
}


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
        "- `$SIM_HARNESS_WORKSPACE/runs/case_routes/<physics>/<scenario>/<version>/`：内部 source run、日志、缓存与质量证据；不再混入对外 Case 目录。",
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
        description = FOLDER_DESCRIPTIONS.get(directory, "同一现象的参数矩阵或专用 CaseSpec 集合。")
        memory = folder_memory(directory)
        lines.append(f"| `{directory}/` | {description} | {memory} |")
    lines.extend(["", "## 每个 Case / 模板", "", "| Case | 类型 | 能力 | 说明 | Harness 必须记住 |", "|---|---|---|---|---|"])
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
        route = str(manifest.get("case_route") or "")
        description = WORKSPACE_CASE_DESCRIPTIONS.get(route, case_dir.name.replace("_", " "))
        variants = manifest.get("variants") if isinstance(manifest.get("variants"), list) else []
        contents = f"`{len(variants)}` 个变体"
        family = "/".join(route.split("/")[:2])
        memory = WORKSPACE_FAMILY_MEMORY.get(
            family,
            "CaseSpec 只定义初态；正式产物必须可追溯、可验证，并按 keep/reject 清理。",
        )
        lines.append(f"| `{case_dir.name}/` | {description} | {contents} | {memory} |")
    lines.extend(
        [
            "",
            "## Harness 维护规则",
            "",
            "1. 每次正式 run、probe 清理、keep/reject 或新增版本后，重新生成本文件。",
            "2. 不把 physics/scenario/version 重新展开到 `cases/`；内部 route 只保存在 `runs/case_routes/` 和 manifest。",
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


def folder_memory(directory: str) -> str:
    if directory == "templates":
        return "模板只生成 CaseSpec；不能当成真实 solver 证据。"
    if "matrix" in directory:
        return "固定其余条件、显式 condition、逐档独立 run；比较因果方向。"
    if directory == "fluid":
        return "粒子/cache 是仿真真值；表面重建是中间层；RGB 可见与传感器门必须单独通过。"
    if directory == "fluid/drop_in_liquid":
        return "首帧水面离群高度、刚体入水后的水花、浮沉分离与传感器分别过门；solver/可见容器几何和显式 scale 必须绑定，圆碗不得套方形 cache。"
    if directory == "fracture":
        return "必须有原生碰撞、碰前能量样本、阈值选择和稳定碎片状态。"
    if directory in {"soft_body", "soft_body/cloth_drape", "soft_body/flag_wind"}:
        return "顶点/cache 是 solver truth；固定拓扑、边长拉伸、碰撞非穿透、末速和 UE 回放同步必须分别过门。"
    return "正例必须通过目标不变量；negative Case 必须被 verifier 拒绝。"


def case_memory(payload: dict, is_template: bool) -> str:
    keys = payload.get("expected_invariants") if is_template else payload.get("verification_rules")
    if not keys:
        keys = payload.get("required_signals") or payload.get("expected_artifact_contract") or []
    values = [str(value) for value in keys if value]
    if len(values) > 6:
        values = [*values[:6], "…"]
    return ", ".join(values) if values else "按 capability verifier 与 artifact hard gate 执行。"


def escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
