# Harness 架构说明

## 项目定位

本项目是 **physics-aware harness for code agents**。它不是单纯 prompt-to-video pipeline，不是只为了 UE 渲染，也不是 frontend-first demo。

核心目标是让 code agent 可以按需调用物理仿真能力：

```text
Agent task / prompt
  -> capability planning
  -> case spec / scene spec
  -> asset intent resolution
  -> runtime adapter
  -> trajectory/contact/render artifact collection
  -> physics verifier
  -> diagnosis / repair suggestion
  -> dataset-ready artifact package
```

## Agent 使用方式

Agent 可以：

- 调用 `harness/planning/capability_planner.py` 根据 prompt 选择 capability。
- 编辑或生成 `cases/**/*.json` 作为可执行 case spec。
- 调用 `harness/assets/asset_resolver.py` 生成 asset intent、top-k candidates、selected asset、fallback reason。
- 调用 `scripts/harness_run_case.py` 用 fallback 或 UE backend 执行 case。
- 调用 `scripts/harness_verify_run.py` 读取 artifact 并输出 verifier report。
- 读取 `artifact_manifest.json`、`harness_verifier.json`、`capability_diagnosis.md` 后决定修 case、修 verifier、修 backend 或修 asset。

## 核心对象

| 对象 | 含义 | 当前位置 |
|---|---|---|
| Capability | 可复用物理能力契约 | `capabilities/*.json` |
| CaseSpec | 最小可执行 case | `cases/**/*.json` |
| SceneSpec | 从 case/assets 编译出的场景契约 | `harness/core/scene_spec.py` |
| AssetIntent | 对象级资产需求 | `harness/assets/asset_intent.py` |
| RuntimeArtifact | trajectory/contact/summary/readiness/pass manifest | `harness/runtime/artifact_collector.py` |
| VerifierReport | 统一物理验证报告 | `harness/core/verifier_schema.py` |
| Diagnosis | failure -> repair suggestion | `harness/verification/diagnosis.py` |
| DatasetPackage | 可打包 artifact manifest 集合 | `harness/packaging/dataset_packager.py` |

## Capability 必须包含

- `id`
- `description`
- `physical_assumptions`
- `required_signals`
- `required_assets`
- `verifier_rules`
- `failure_taxonomy`
- `repair_suggestions`
- `smoke_cases`
- `regression_cases`

## Capability Profile

`config/harness_capability_profile.json` 是 agent 能力索引。它不是运行产物，也不包含本地 run、agent-docs 或 secret。Agent 可以先读取 profile，再决定调用哪个 capability contract。

当前 public profile 覆盖：

- prompt/case capability planning
- generic rigid-body contact causality
- rigid body gravity/collision
- sequential contact propagation
- physical property constraints: restitution, rolling/sliding friction, wind/force field, mass-ratio momentum, angular damping spin decay, agent action to rigid-body coupling, fixed-distance/joint constraint motion, constrained impulse-chain transfer, elastic energy launch, elastic tether rebound, brittle impact fracture
- asset intent resolution
- asset runtime binding invocation
- scene spec compilation
- explicit physics control surface
- physics property constraint validation
- runtime artifact bridge
- canonical multi-signal capture
- physics verifier truth gate
- dataset artifact packaging

`billiard_causality_compiler` 不属于 active capability，也不再作为 capability JSON 发布。它只在读取旧 artifact 时被解释为 deprecated alias；新 planner 和新 case 不应依赖它。台球只是 `rigid_body_contact_causality` 的 regression/smoke family。
同理，`constraint_distance_pendulum_motion` 是距离/绳长/关节约束 invariant，不是单摆模板。Agent 应该先选择通用物理约束能力，再让 case template 决定是否用单摆、绳索、链条或关节作为实例。
`constraint_momentum_transfer` 是受约束刚体链的冲量/动量传递 invariant，不是牛顿摆模板；牛顿摆只是这个能力的 smoke/regression family。

## Case Templates

固定 golden cases 只用于 regression。可扩展测试应通过 `cases/templates/*.template.json` 生成：

```text
template + seed + parameter ranges -> generated case specs -> backend -> verifier
```

动态生成入口：

```bash
python3.13 scripts/harness_generate_cases.py --suite billiards --count 20 --seed 42 --out cases/generated/billiards_seed42
```

## Runtime Backends

`fallback` 是 deterministic toy backend，只用于开发 verifier、case schema 和 CLI。它不能作为真实物理或视频质量证据。

`ue` 是 production backend。当前 UE path 通过：

- `harness/runtime/ue_backend.py`
- `scripts/harness_local_ue_runner.py`
- `scripts/native_ue_physics_phenomena_scene.py`
- `ue_template/Plugins/ADPPhysicsRuntime`

输出同步多视角 RGB/depth/segmentation，以及 trajectory/contact/camera timeline。

生产评估不应把 fallback 结果混入 UE 统计。UE 失败必须 fail-clear，不允许 silent fallback。

## Slim Main Repository Policy

main 分支只保留 harness 主路径：

- code: `harness/`, `scripts/harness_*.py`, `scripts/native_ue_physics_phenomena_scene.py`
- contracts: `capabilities/`, `cases/`, `config/harness_capability_profile.json`
- UE support: `ue_template/`
- docs/tests/agent entry: `docs/`, `tests/`, `skill/`

不进入 main：

- generated cases under `cases/generated/`
- `runs/`, `outputs/`, `artifacts/`
- `agent-docs/`
- old frontend/API
- large private asset dumps

```bash
python3.13 scripts/harness_generate_cases.py \
  --template cases/templates/billiards_collision.template.json \
  --num-cases 10 \
  --seed 42 \
  --out cases/generated/billiards_seed42
```

`cases/generated/` 是本地产物，不进入 public commit。

## Case Navigation Contract

- 仓库 `cases/TREE.md` 由 `scripts/harness_case_tree.py` 从全部 JSON CaseSpec/模板生成，解释每个输入目录、Case 和应由 verifier 记住的不变量。
- 本地 `$SIM_HARNESS_WORKSPACE/cases/TREE.md` 由同一命令加 `--workspace-root` 生成，只展示 `case_id/variant_id` 两层业务语义。每个 variant 固定包含 `{rgb,depth,segmentation,overall}` 与 `variant.json`；canonical run、日志和缓存保存在 `runs/case_routes/<physics>/<scenario>/<version>/`，由 manifest 的 `source_run` 指向。
- 时间戳、attempt 和 camera/pass 不得继续升格为 case 语义层；它们只能存在于版本内部 manifest/index。
- `python scripts/harness_case_tree.py --check --workspace-root "$SIM_HARNESS_WORKSPACE"` 是新增、删除、keep/reject 和 probe 清理后的导航关卡。

## Runtime Backend Abstraction

Backend contract 位于 `harness/runtime/`：

| Backend | 当前状态 | 约束 |
|---|---|---|
| `fallback` | 可运行 | deterministic toy trajectory，只能验证 schema/invariant，不代表真实 UE |
| `ue` | fail-fast contract | 未配置时必须返回 `F6_runtime_or_render_failure`，不能 silent fallback |
| `genesis_sph` | 隔离原型 | 已输出 canonical particle cache、逐帧 surface、统一 artifact/readiness/verifier；在 UE 多模态耦合完成前不得宣称 reference-ready |

所有 backend 都应写统一 artifact directory，让 verifier 不依赖具体执行器。

Genesis 的 `particle_cache.json` 是流体状态真值；统一 `trajectory.json` 只是每帧粒子质心/平均速度投影。当前 solver 未导出粒子-盆体接触事件，因此 `contact_events.json` 明确为空、readiness 中 `contact_events_ready=false`，不能把空文件解释为“确认没有接触”。

PhysInOne 对照确认液体画质是完整交换链问题：其液体使用 SPH/Doriflow，特殊材料使用 Taichi MPM，渲染同时产出多机位 RGB、depth、mask、trajectory/material/camera truth。当前 Harness 的 Genesis 粒子/cache 可继续作为 solver truth，但 SplashSurf OBJ→UE 的 surface/material/lighting 与 instance pass 尚未达到可见性门；baseline 通过前不运行落高矩阵。后续按 `solver truth → per-frame surface reconstruction → Blender/UE render adapter → sensors` 分层，允许流体使用 Blender/Houdini renderer，不强制所有现象都由 UE 完成。

## Artifact Schema

每个 harness run 至少应包含：

```text
case_spec.json
run_control.json
run_control.html
artifact_manifest.json
harness_artifact.json
harness_verifier.json
<backend>_output/trajectory.json
<backend>_output/contact_events.json
<backend>_output/summary.json
<backend>_output/run_readiness.json
<backend>_output/render_manifest.json
```

`run_control.json` 是 `harness_run_control_v1` 可执行契约，冻结 CaseSpec 哈希、
backend、机位、模态、分辨率和复现命令。`run_control.html` 是同一契约的自包含
控制页：有 variant plan 时开放已声明 JSON Pointer；没有 plan 时保持 CaseSpec
只读，只允许调整捕获配置。复现输出固定进入原 run 的 `reproductions/`，不得覆盖
原始证据。成功和失败 run 都必须保留这两个文件。

UE v002 已生成 `camera_trajectory.json`、双机位 RGB 视频、逐帧 depth/segmentation 与 pass manifest；后续还必须补 normal/audio、完整五机位 profile 与原生 Chaos substep/contact impulse trace。

## 旧 Pipeline 到 Harness 的映射

| 旧 pipeline 步骤 | Harness 解释 |
|---|---|
| prompt rewrite | intent/case/capability planning tool |
| asset search | asset intent resolver capability |
| scene generation | scene spec compiler |
| UE render | runtime backend |
| trajectory/contact extraction | runtime artifact collector |
| verifier | physics capability verifier |
| report/frontend | diagnosis/artifact viewer |

## 为什么前端不是主线

资产/结果浏览器仍是 optional viewer。每个 run 的 `run_control.html` 不是 viewer，
而是 `harness_run_control_v1` 的人机界面，与 `run_control.json` 一起属于 core
artifact contract。物理验收仍以 verifier、manifest 和回归测试为准。
