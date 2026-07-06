# Physics-Aware Harness

本文档定义当前主路径。Simulator Studio / Agentic Data Platform 现在首先是给 code agent 调用的 physics-aware simulation harness，不是普通 prompt-to-video demo，也不是 frontend-first 项目。

## Stage 0: Capability Planning

输入：

- agent task / natural language prompt
- `config/harness_capability_profile.json`
- `capabilities/*.json`

输出：

- primary physics capability id
- layered pipeline capabilities
- required signals
- expected invariants
- failure taxonomy

入口：

```bash
python3.13 scripts/harness_list_capabilities.py
```

`CapabilityPlanner.plan(prompt)` 不应该只返回一个 case 名。它必须返回：

| Layer | Capability Examples | 作用 |
|---|---|---|
| Pipeline stages | `prompt_case_capability_planning`, `scene_spec_compilation`, `static_scene_placement`, `runtime_actor_placement_compilation`, `runtime_backend_execution`, `pipeline_stage_orchestration` | 把 prompt 变成可执行 case、静态布局、runtime actor bindings 和 stage graph。 |
| Asset operations | `asset_intent_resolution`, `asset_runtime_binding_invocation` | 检索 top-k 资产、选择/降级、绑定 UE actor 和物理 metadata。 |
| Physics constraints | `rigid_body_contact_causality`, `constraint_distance_pendulum_motion`, `constraint_momentum_transfer`, `explicit_physics_control_surface`, `physics_property_constraint_validation` | 约束运动、材质、质量、摩擦、恢复系数、重力、力场、时间步等。 |
| Runtime/signal bridge | `capability_runtime_artifact_bridge`, `canonical_signal_capture` | 把 UE/fallback 输出标准化成 trajectory/contact/camera/render evidence。 |
| Verification/package | `render_signal_sync_validation`, `physics_verifier_truth_gate`, `dataset_artifact_packaging` | 先检查 render/signal 同步，再以 physics verifier 为真值门，只打包 readiness-gated artifacts。 |

`billiard_causality_compiler` 不再是 active capability，也不再作为 capability contract 发布。读取旧 artifact 时可把这个名字当 deprecated alias；新 case 中台球、保龄球、箱体撞击应走 `rigid_body_contact_causality`，质量差异应叠加 `mass_ratio_momentum_transfer`，可破碎对象应走 `brittle_impact_fracture`。
`constraint_distance_pendulum_motion` 同样不是“单摆模板”，而是距离/绳长/关节约束的通用 invariant；单摆只是 smoke case。
`constraint_momentum_transfer` 也不是“牛顿摆模板”，而是受约束刚体链中的 ordered contact / impulse transfer invariant；牛顿摆只是 smoke case。

## Stage 1: Case Spec

Case spec 是 harness 的最小可执行任务。它包含：

- `case_id`
- `capability_id`
- prompt
- objects
- active/passive object roles
- required assets
- required signals
- verifier expectation
- `should_pass`

静态 golden cases 位于：

```text
cases/billiards/
cases/domino/
cases/falling/
```

动态 templates 位于：

```text
cases/templates/
```

生成命令：

```bash
python3.13 scripts/harness_generate_cases.py \
  --template cases/templates/billiards_collision.template.json \
  --num-cases 20 \
  --seed 42 \
  --out cases/generated/billiards_seed42
```

## Stage 2: Asset Intent Resolution

旧系统的“找资产”现在被降级为 `asset_intent_resolution` capability。它不应该只返回一个 mesh path，而应该明确：

- object id
- asset category
- physics-critical or visual-only
- required collider / mass / rigid body / collision profile
- top-k candidates
- selected asset
- fallback reason

当前 resolver 仍是 lightweight/stub，主要用于 contract 和测试。真实资产库接入需要使用资产索引和 physics index。

resolver 输出必须能驱动下一阶段调用，而不是只给 UI 展示：

- `capability_id=asset_intent_resolution`
- `stage_id=asset_resolution`
- `invocation_contract.next_capability_id=asset_runtime_binding_invocation`
- 每个 object 的 `runtime_binding_requirements`
- physics-critical count / visual-only count

## Stage 3: Static Scene Placement

静态场景摆放是 runtime 前的 preflight，不是 UI 预览。它把 case spec 和 asset resolution 编译成：

- `scene_layout.json`
- object nodes
- support relations
- initial overlap checks
- physics graph membership
- camera plan
- `static_scene_report.json`

入口：

```bash
python3.13 scripts/harness_build_static_scene.py \
  cases/billiards/low_speed_single_contact.json \
  --output-dir runs/static_scene/low_speed_single_contact
```

当前会检查：

- object id 是否唯一；
- physics-critical object 是否有 selected asset 或 analytic proxy fallback；
- collider / mass / material / collision profile 是否可用于 runtime binding；
- 初始状态是否有重叠或支撑穿透；
- camera plan 是否存在。

`runtime_actor_placement_compilation` 是下一层 runtime preflight：它读取 `scene_layout.json` 和 asset resolution，生成 UE/runner 可消费的 actor bindings、camera bindings 和 physics graph bindings。当前 compiler 已可用；下一步是让真实 UE runner 直接消费 `runtime_actor_placement.json` 来创建 actor、设置 transform、collider、mass、material、collision profile 和 camera rig。

入口：

```bash
python3.13 scripts/harness_compile_actor_placement.py \
  cases/bowling/bowling_pin_chain_contact.json \
  --output-dir runs/actor_placement/bowling_pin_chain_contact
```

输出：

- `asset_resolution.json`
- `scene_layout.json`
- `static_scene_report.json`
- `runtime_actor_placement.json`
- `runtime_actor_placement_report.json`

当前已能检查：

- 每个 physics-critical object 是否有 runtime actor binding；
- runtime actor id 是否唯一稳定；
- selected UE asset 或 analytic proxy 是否明确；
- collider / mass / collision profile 是否满足 runtime 绑定；
- collision graph 和 camera bindings 是否可被 runtime 读取。

## Stage 4: Runtime Backend

支持两个 backend：

| Backend | 状态 | 用途 |
|---|---|---|
| `fallback` | 可运行 | deterministic toy trajectory，用于 verifier 和 CLI regression |
| `ue` | fail-clear contract | 目标真实 backend，当前还未接 legacy UE runner |

运行单 case：

```bash
python3.13 scripts/harness_run_case.py cases/billiards/low_speed_single_contact.json --backend fallback
```

批量运行：

```bash
python3.13 scripts/harness_run_case_batch.py cases/generated/billiards_seed42 --backend fallback
```

## Stage 5: Artifact Collection

每个 run 应写出统一 artifact：

```text
case_spec.json
artifact_manifest.json
harness_artifact.json
harness_verifier.json
<backend>_output/
  trajectory.json
  contact_events.json
  constraint_trace.json
  summary.json
  run_readiness.json
  render_pass_manifest.json
```

真实 UE 后续还需要补：

- multi-view RGB
- depth / normal
- audio / contact audio
- camera trajectory
- engine states
- blueprint/runtime parameters
- Chaos/contact trace

## Stage 5: Physics Verifier

Verifier 不检查“画面是否动了”，而检查 capability invariants：

| Capability | Invariant |
|---|---|
| `rigid_body_contact_causality` | passive rigid bodies must not move before runtime contact evidence |
| `sequential_contact_propagation` | chain activation must follow predecessor contact order |
| `rigid_body_gravity_collision` | object must descend under gravity and reach support/contact |
| `physics_property_constraint_validation` | mass/friction/restitution/damping/gravity controls must be typed, bounded, and echoed by runtime artifacts |
| `angular_damping_spin_decay` | angular velocity and damping must be explicit, and spin speed must decay without unexplained gain |
| `agent_rigidbody_action_coupling` | target rigid bodies must remain still before explicit agent action trace and respond after contact/impulse evidence |
| `constraint_distance_pendulum_motion` | constrained bodies must preserve anchor-body distance within tolerance and move continuously without teleporting |
| `constraint_momentum_transfer` | constrained chain bodies must start still, adjacent contacts must be ordered, and terminal receiver response must follow final contact |
| `elastic_energy_launch` | launched body must start still, release event must exist, and post-release speed/energy must match the stored elastic-energy envelope |
| `elastic_constraint_rebound` | elastic tether must export constraint trace, stay within max extension, and rebound toward the anchor after peak stretch |
| `brittle_impact_fracture` | fracture events must occur after causal contact, impact energy must exceed threshold, and fragments must be exported |
| `asset_runtime_binding_invocation` | physics-critical assets must bind colliders, mass/material metadata, collision profile, and runtime actor ids |

验证命令：

```bash
python3.13 scripts/harness_verify_run.py runs/<run_id>
```

输出统一 `harness_verifier_report_v1`：

```json
{
  "status": "pass",
  "failure_type": null,
  "first_failure": null,
  "evidence": [],
  "repair_suggestions": [],
  "artifact_completeness": {}
}
```

## Stage 6: Diagnosis and Repair

每个失败必须归因到 failure type，例如：

- `F1_missing_trajectory`
- `F2_missing_contact_events`
- `F3_initial_overlap`
- `F4_causality_violation`
- `F5_passive_precontact_motion`
- `F6_runtime_or_render_failure`
- `F7_runtime_artifact_incomplete`

Diagnosis 应告诉 agent：

- 哪个 object 失败；
- 第一个失败 frame/time；
- 测量值；
- 最近 contact frame；
- 可执行 repair suggestion。

## Stage 7: Dataset Package

Dataset packaging 不是简单收视频，而是收：

- case spec
- capability plan
- scene/runtime contract
- trajectory/contact events
- render pass manifest
- verifier report
- diagnosis

入口：

```bash
python3.13 scripts/harness_package_dataset.py runs/<run_id>
```

## Optional Viewer

前端 viewer 是 optional。核心验收不依赖前端。恢复前端时，它应该展示 capability verifier、diagnosis、artifact manifest 和多机位/信号同步，而不是只展示视频。

见 `docs/OPTIONAL_VIEWER.md` 和 `docs/LEGACY_NOTES.md`。
