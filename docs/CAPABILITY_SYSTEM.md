# Capability System

本文定义 harness 的能力分层。能力不是 prompt 模板，也不是单个 demo 名称；能力必须对应可复用的 pipeline 阶段、物理不变量、资产操作、校验门或数据打包动作。

## 总原则

- 场景名不是能力：台球、牛顿摆、单摆、玻璃破碎只是 case family。
- 能力必须有 contract：required signals、required assets、verifier rules、failure taxonomy、repair suggestions。
- Agent 应先选能力层，再生成 case/template/scene/runtime 参数。
- `billiard_causality_compiler` 不作为 active capability。旧 JSON 若存在，只是 legacy artifact alias。

## 1. Pipeline 阶段能力

| Stage | Capability | 输入 | 输出 |
|---|---|---|---|
| Prompt/task -> case intent | `prompt_case_capability_planning` | prompt, capability profile | capability plan, case family, required signals |
| Case/assets -> executable scene | `scene_spec_compilation` | case spec, asset resolution | scene spec, collision graph, camera/render requirements |
| Static scene preflight | `static_scene_placement` | case spec, asset resolution | `scene_layout.json`, object nodes, support relations, non-overlap report, camera plan |
| Runtime artifact bridge | `capability_runtime_artifact_bridge` | UE/fallback outputs | normalized trajectory/contact/render artifacts |
| Signal synchronization | `canonical_signal_capture` | RGB/depth/segmentation/trajectory/contact/camera | aligned signal manifest |
| Dataset packaging | `dataset_artifact_packaging` | verifier-gated run directory | dataset-ready manifest/package |
| Full orchestration | `pipeline_stage_orchestration` | stage artifacts | staged lineage and failure attribution |

## 2. Asset 检索与调用能力

| Capability | 职责 | 必须记录 |
|---|---|---|
| `asset_intent_resolution` | 从 object role / asset query 检索 top-k candidate。 | intent, category, physics-critical flag, candidates, selected asset, fallback reason |
| `asset_runtime_binding_invocation` | 把 selected asset 或 analytic proxy 绑定到 runtime actor。 | object_id, runtime_actor_id, collider, mass, material, collision_profile, proxy flag |

Physics-critical asset 必须有 collider、mass、rigid body、collision profile。Visual-only asset 可以被替换或随机化，但不能进入 physics graph。

## 3. 物理控制与属性约束能力

| Capability | 职责 |
|---|---|
| `explicit_physics_control_surface` | 将 gravity、material、rigid body、constraint、force field、time control、agent control、render-physics bridge 写成可回放结构。 |
| `physics_property_constraint_validation` | 检查 mass、friction、restitution、damping、gravity、material density、fracture threshold 等字段范围和 sensitivity 方向。 |

这些能力通常作为 supporting capabilities 出现在每个物理 case 中，不应该被写成一个具体物体 demo。

## 4. 物理行为能力

| Capability | 通用不变量 | Case families |
|---|---|---|
| `rigid_body_contact_causality` | passive body 只能在 contact evidence 后运动。 | 台球、保龄球、普通箱体碰撞 |
| `mass_ratio_momentum_transfer` | contact 后速度顺序和能量范围要符合质量比/restition。 | 重物撞轻物、轻物撞重物 |
| `sequential_contact_propagation` | 链条/多体运动必须按相邻 contact 顺序传播。 | 多米诺、瓶子连锁 |
| `rigid_body_gravity_collision` | 物体应在重力下下降并产生 support contact。 | 掉落方块、堆叠掉落 |
| `ramp_sliding_friction` | 斜面运动应符合重力方向和摩擦范围。 | 斜面滚动、下滑 |
| `projectile_gravity_motion` | 抛体要有上升/下降/落地 contact 证据。 | 上抛、斜抛 |
| `bounce_restitution_ball` | 反弹高度受 restitution envelope 约束。 | 弹球、掉落反弹 |
| `rolling_friction_ball` | support contact 下滚动距离和速度衰减受摩擦约束。 | 球滚停距 |
| `sliding_crate_friction` | 滑动/静摩擦阈值决定是否移动和停距。 | 箱体滑动、推不动 |
| `force_field_wind_drift` | 漂移方向/距离要和显式风向/力场一致。 | 气球、纸片、轻物体 |
| `angular_damping_spin_decay` | angular velocity 必须随 damping 衰减，不能无外力增益。 | 陀螺、自转球 |
| `agent_rigidbody_action_coupling` | 目标刚体只能在 action/contact/release evidence 后响应。 | 机器人推箱、角色抛球 |
| `constraint_distance_pendulum_motion` | 约束体必须保持 anchor-body 距离并连续运动。 | 单摆、绳索、铰链 |
| `constraint_momentum_transfer` | 受约束刚体链必须按 contact 顺序传递冲量。 | 牛顿摆、悬挂球链 |
| `elastic_energy_launch` | stored elastic energy 通过 release event 转成 bounded kinetic response。 | 弹簧发射、弹射器 |
| `elastic_constraint_rebound` | 弹性约束必须记录 extension，并在峰值后朝 anchor 回弹。 | 蹦极、弹力绳 |
| `brittle_impact_fracture` | fracture 只能在 contact energy 超过 threshold 后发生，并输出 fragments。 | 玻璃、镜子、杯子、木箱 |

## 5. 校验能力

`physics_verifier_truth_gate` 是最终 truth gate。它应分层判断：

- schema validity
- initial physics validity
- runtime causality validity
- asset binding validity
- render/signal evidence validity
- diagnosis / repair suggestion

视频存在不等于通过。没有 trajectory、contact events、camera trajectory、render pass manifest 或 required physical labels 的样本不能作为 reference-ready 数据。

## 6. Agent 调用顺序

推荐顺序：

```text
prompt/task
  -> CapabilityPlanner.plan
  -> template/case generation
  -> asset_intent_resolution
  -> asset_runtime_binding_invocation
  -> scene_spec_compilation
  -> runtime backend
  -> capability_runtime_artifact_bridge
  -> physics_verifier_truth_gate
  -> dataset_artifact_packaging
```

快速命令：

```bash
python3.13 scripts/harness_list_capabilities.py --json
python3.13 scripts/harness_build_static_scene.py cases/billiards/low_speed_single_contact.json --output-dir runs/static_scene/low_speed_single_contact
python3.13 scripts/harness_generate_cases.py --suite fracture --count 10 --seed 58 --out cases/generated/fracture_seed58
python3.13 scripts/harness_run_case_batch.py cases/generated/fracture_seed58 --backend fallback
```
