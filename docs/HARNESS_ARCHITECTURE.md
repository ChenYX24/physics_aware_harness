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

`billiard_causality_compiler` 不属于 active capability。若保留旧 JSON，也只是 artifact compatibility alias；新 planner 和新 case 不应依赖它。台球只是 `rigid_body_contact_causality` 的 regression/smoke family。
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
- docs/tests: `docs/`, `tests/`

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

## Runtime Backend Abstraction

Backend contract 位于 `harness/runtime/`：

| Backend | 当前状态 | 约束 |
|---|---|---|
| `fallback` | 可运行 | deterministic toy trajectory，只能验证 schema/invariant，不代表真实 UE |
| `ue` | fail-fast contract | 未配置时必须返回 `F6_runtime_or_render_failure`，不能 silent fallback |

所有 backend 都应写统一 artifact directory，让 verifier 不依赖具体执行器。

## Artifact Schema

每个 harness run 至少应包含：

```text
case_spec.json
artifact_manifest.json
harness_artifact.json
harness_verifier.json
<backend>_output/trajectory.json
<backend>_output/contact_events.json
<backend>_output/summary.json
<backend>_output/run_readiness.json
<backend>_output/render_manifest.json
```

真实 UE 后续还必须补 `camera_trajectory.json`、视频/frame sequence、depth/normal/audio/pass manifest。

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

Agent-facing harness 首先需要 CLI、API、artifact schema 和 tests。前端可以作为 optional viewer 展示视频、trajectory、capability verifier 和 diagnosis，但不应该阻塞 core harness。核心验收以 CLI smoke、verifier report、case regression 和 dataset artifact 为准。
