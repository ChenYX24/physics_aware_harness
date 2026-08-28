# Harness 架构说明

## 架构原则

本项目是供 code agent 使用的 physics-aware harness。它把用户文本和图片编译为声明式物理场景，调用注册的数值引擎，收集状态/事件/传感器证据，再执行通用断言。

“统一求解器”表示统一的编译、执行、证据和验证契约，不表示所有状态都由同一个引擎计算。UE Chaos、Genesis SPH/FEM 和 Taichi 可以作为不同 backend；它们只能解释通用 primitive 和 state domain，不能实现“坠落模式”“连续撞击流程”“平面泼洒模式”等现象专用路径。

```text
request
  -> CaseSpec V2
  -> Provider / Catalog / Asset Resolve
  -> Runtime Compiler
  -> scene-domain Backend Planner
  -> backend solver
  -> canonical artifacts
  -> generic assertions
  -> readiness and review
```

## 三种执行域

| `scene_domain` | capability | 说明 |
|---|---|---|
| `rigid_body` | `rigid_body_dynamics` | 刚体状态、碰撞几何、材料、约束、外力和事件 |
| `particle` | `fluid_particle_dynamics` | 粒子状态及刚体—粒子 coupling |
| `deformable` | `deformable_body_dynamics` | 固定/变化拓扑网格、材料模型及接触 |

`harness/core/physics_contract.py` 只做上述状态域推断。Backend Planner 还可读取显式 `allowed_solvers` 和 `required_solver_capabilities`。case id、目录、旧 capability label 和自然语言现象词不得参与 backend 选择。

## 核心对象

| 对象 | 职责 |
|---|---|
| CaseSpec | 对象、几何、材料、初态、关系、backend constraints、观察要求与断言 |
| AssetIntent | 待解析的对象/环境资源需求 |
| Catalog binding | 已物化、带 hash/license/依赖和 backend binding 的资产候选 |
| RuntimeCompilation | backend、scene layout、actor placement、camera 和 verification plan 的唯一编译结果 |
| RuntimeArtifact | canonical trajectory/cache、event、camera、render 与 readiness 证据 |
| VerifierReport | 通用断言及 artifact completeness 的结果 |

Provider 只能发现、下载或生成资产。任何本地文件、Meshy 输出、engine builtin、Map 或 procedural recipe 都必须注册并通过 Asset Resolve，不能直接绕过编译器进入 runtime。

## Runtime 与验证

- `fallback`：非参考预览，只按初态和显式速度/重力生成简单 kinematic trace；不创造接触或其他事件。
- `ue`：刚体 production backend；需要真实 UE/Chaos capture provenance。
- `genesis_sph`：particle-domain solver。
- `genesis_fem`、`taichi_cloth`：deformable-domain solver adapters。

刚体 verification plan 只包含通用断言：

- `trajectory_integrity`
- `state_value`
- `state_delta`
- `event_exists`
- `event_count`
- `event_sequence`
- `artifact_complete`

Particle 和 deformable verifier 检查各自 canonical cache，再把测量结果映射到同一声明式验证边界。任何 backend 都不得通过现象名称选择专用 verifier。

## UE 对象图

`scripts/harness_local_ue_runner.py` 始终产生 `case_type=llm_object_graph`。对象的 transform、asset binding、body type、collider、material、mass、initial velocity、gravity、CCD 和 constraint 来自 Runtime Compilation。

`scripts/native_ue_scene.py` 的入口拒绝其他 case type。旧命名模式不会由当前 CLI/Runtime Compiler 生成，也不构成受支持执行路径。新增 UE 功能必须扩展对象图字段或通用 runtime component，不能恢复 case-type dispatch。

## Case 导航不是路由器

`scripts/harness_case_tree.py` 只枚举文件并生成 Markdown 导航。`cases/TREE.md` 和 workspace TREE 不会被 Runtime Compiler 读取。历史目录可以保留人类可读名称，但不得影响 solver、camera、asset qualification 或 verifier。

`scripts/harness_generate_cases.py` 不接受 named suite，只复制一个完整声明的 CaseSpec 并记录 seed/index。参数变化必须来自显式 variant/JSON patch 契约，而不是按现象选择模板代码。

## Artifact 最小契约

```text
case_spec.json
backend_selection.json
scene_layout.json
runtime_actor_placement.json
verification_plan.json
trajectory.json or canonical domain cache
contact_events.json / generic events
camera_trajectory.json
render_manifest.json
run_readiness.json
harness_verifier.json
artifact_manifest.json
run_control.json
run_control.html
```

成功和失败都要保留机器可读报告。视频存在不等于验证通过；fake/fallback 产物不得标记为真实物理或真实 UE render。

## 兼容边界

旧 V1 case 与旧 capability JSON 可继续读取。现象命名 capability 在 registry 中是 `compatibility_alias`，会投影到通用执行 capability；它们不在 active profile 中，也不会选择 backend/verifier。旧 negative case 若没有显式通用 assertions，不再触发隐藏 verifier 规则。

## 维护规则

1. 新能力先判断是否是通用 primitive、state representation 或 coupling；现象名称不能成为代码分支。
2. 缺失能力时 fail closed，并报告缺少的 solver capability。
3. 资产获取与生成不能绕过 Catalog/Asset Resolve。
4. fallback/fake 只用于协议测试。
5. 用 `tests/test_no_process_dispatch.py` 防止标签重新影响执行路径。
6. 完整回归：`conda run -n base python -m unittest discover -s tests -p 'test_*.py'`。
