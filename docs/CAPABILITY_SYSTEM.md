# Capability System

本文定义 harness 的能力分层。能力不是 prompt 模板，也不是单个 demo 名称；能力必须对应可复用的 pipeline 阶段、物理不变量、资产操作、校验门或数据打包动作。

## 总原则

- 场景名不是能力：台球、牛顿摆、单摆、玻璃破碎只是 case family。
- 能力必须有 contract：required signals、required assets、verifier rules、failure taxonomy、repair suggestions。
- Agent 应先选能力层，再生成 case/template/scene/runtime 参数。
- `billiard_causality_compiler` 不作为 active capability，也不再作为 `capabilities/*.json` 发布；只在读取旧 artifact 时当 deprecated alias 解释。

## 1. Pipeline 阶段能力

| Stage | Capability | 输入 | 输出 |
|---|---|---|---|
| Prompt/task -> case intent | `prompt_case_capability_planning` | prompt, capability profile | capability plan, case family, required signals |
| Case/assets -> executable scene | `scene_spec_compilation` | case spec, asset resolution | scene spec, collision graph, camera/render requirements |
| Static scene preflight | `static_scene_placement` | case spec, asset resolution | `scene_layout.json`, object nodes, support relations, non-overlap report, camera plan |
| Runtime actor placement | `runtime_actor_placement_compilation` | `scene_layout.json`, asset resolution, camera plan | deterministic runtime actor bindings, camera bindings, physics graph bindings |
| Runtime execution | `runtime_backend_execution` | case spec, actor bindings, render config, backend env | trajectory/contact/camera/render artifacts or explicit preflight/runtime failure |
| Runtime artifact bridge | `capability_runtime_artifact_bridge` | UE/fallback outputs | normalized trajectory/contact/render artifacts |
| Signal synchronization | `canonical_signal_capture` | RGB/depth/segmentation/trajectory/contact/camera | aligned signal manifest |
| Render sync validation | `render_signal_sync_validation` | camera plan, render manifest, RGB/depth/segmentation/camera/physics traces | `render_sync_report.json`, observability failures |
| Dataset packaging | `dataset_artifact_packaging` | verifier-gated run directory | dataset-ready manifest/package |
| Full orchestration | `pipeline_stage_orchestration` | stage artifacts | staged lineage and failure attribution |

## 2. Asset 检索与调用能力

| Capability | 职责 | 必须记录 |
|---|---|---|
| `asset_intent_resolution` | 只负责资产检索：从 object role / asset query 检索 top-k candidate，并按 physics-critical、visual-only、skeletal/animation、blueprint/logic、scene/map 分类。 | intent, category, physics-critical flag, top-k candidates, selected asset, fallback reason |
| `asset_runtime_binding_invocation` | 只负责资产放置/调用：把 selected asset 或 analytic proxy 绑定到 runtime actor，并把物理关键 metadata 写入 actor binding。 | object_id, runtime_actor_id, collider, mass, material, collision_profile, transform, proxy flag |

Physics-critical asset 必须有 collider、mass、rigid body、collision profile。Visual-only asset 可以被替换或随机化，但不能进入 physics graph。

资产能力分成两段是为了避免检索结果直接污染物理执行：

```text
case object
  -> asset_intent_resolution: top-k 检索和候选解释
  -> static_scene_placement: 静态位置/support/non-overlap/camera 检查
  -> runtime_actor_placement_compilation: object_id -> runtime_actor_id
  -> asset_runtime_binding_invocation: UE path/proxy + collider/mass/material/collision profile
  -> blueprint_function_invocation: SpawnActor/SetMesh/SetCollision/SetMass/SetVelocity/StartCapture
```

### 2.1 资产类型

| Asset type | 是否进入 physics graph | 必须字段 | 说明 |
|---|---:|---|---|
| Physics-critical static mesh | 是 | `ue_path`, `collider`, `mass_kg` 或 density, `material`, `collision_profile` | 墙、地面、球、箱体、坡面、可破碎物。 |
| Visual-only asset | 否 | `ue_path`, visual category | 贴图、decal、VFX、纯装饰材质，可随机化。 |
| Skeletal / animation asset | 可选 | skeleton/clip/IK binding, motion target | 影响 action trace 或 agent trajectory。 |
| Blueprint / logic asset | 可选 | actor class, function call contract | 驱动 agent、state machine、trigger、event system。 |
| Scene / map asset | 是，作为结构约束 | map package path, nav/spawn info | 作为 root-level scene constraint。 |

## 3. 物理控制与属性约束能力

| Capability | 职责 |
|---|---|
| `explicit_physics_control_surface` | 将 gravity、material、rigid body、constraint、force field、time control、agent control、render-physics bridge 写成可回放结构。 |
| `physics_parameter_semantics` | 给模型提供物理参数的单位、含义、预期效果和 sensitivity 方向，避免把“惯性/阻尼/摩擦”等只写成自然语言。 |
| `physics_property_constraint_validation` | 检查 mass、friction、restitution、damping、gravity、material density、fracture threshold 等字段范围和 sensitivity 方向。 |

这些能力通常作为 supporting capabilities 出现在每个物理 case 中，不应该被写成一个具体物体 demo。

### 3.1 给模型的物理参数表

| 参数 | 单位 | 模型应该理解的含义 | 预期效果 |
|---|---|---|---|
| `mass_kg` | kg | 刚体质量。 | 同样 impulse 下质量越大速度变化越小；碰撞动量传递随质量比改变。 |
| `inertia_scale` | ratio | 转动惯量倍率。 | 越大越不容易被扭矩或偏心碰撞改变角速度。 |
| `linear_velocity_init` | m/s | 初始线速度，只应给 active/launch body。 | 决定 frame 0 的移动方向和速度；passive target 通常必须为 0。 |
| `angular_velocity_init` | rad/s 或显式单位 | 初始角速度。 | 产生自转；应被 angular damping 衰减。 |
| `linear_damping` | 1/s | 线速度阻尼。 | 越大越快减速，移动距离更短。 |
| `angular_damping` | 1/s | 角速度阻尼。 | 越大 spin 衰减越快。 |
| `friction_static` | coefficient | 启动滑动前的静摩擦阈值。 | 越大越难被推/滑动。 |
| `friction_dynamic` | coefficient | 已滑动后的动摩擦。 | 越大滑行更短、速度衰减更快。 |
| `restitution` | coefficient | 碰撞弹性/反弹。 | 越大反弹更高；大于 1 通常是能量增益，应被 verifier 拒绝。 |
| `gravity` | m/s² | 世界重力加速度。 | 越大下落更快、接触更早、冲击更强。 |
| `collision_profile` | engine enum/string | UE 碰撞行为。 | `NoCollision` 不应生成接触；physics-critical actor 必须启用 collision。 |
| `constraint_stiffness` | N/m 或 engine 等效 | 约束/弹簧抗形变能力。 | 越大拉伸更小、回弹更强，也更可能数值不稳定。 |
| `break_threshold` | force/energy as declared | 断裂/破碎阈值。 | 只有超过阈值的 runtime contact/force 才能触发 fracture/break。 |

完整 machine-readable 版本见 `capabilities/physics_parameter_semantics.json`。

## 3.2 Blueprint / UE 函数调用能力

`blueprint_function_invocation` 把 UE Blueprint、C++ plugin、Python 暴露函数调用纳入能力层。凡是会改变物理状态的调用都必须可排序、可复现、可记录。

| Call family | 示例 | 必须证据 |
|---|---|---|
| actor spawn/transform | `SpawnActor`, `SetActorTransform` | runtime_actor_id, transform echo |
| asset/material binding | `SetStaticMesh`, `SetMaterial` | selected asset id/path, material echo |
| collision/rigid-body setup | `SetCollisionEnabled`, `SetCollisionProfileName`, `SetSimulatePhysics`, `SetMassOverrideInKg` | engine_state_before/after |
| velocity/impulse | `SetPhysicsLinearVelocity`, `AddImpulse` | action/release frame, target actor, no hidden passive velocity |
| ADPPhysicsRuntime capture | `RegisterBodyMeters`, `RegisterStaticBody`, `StartCapture`, `AdvanceCapture`, `StopCapture`, `WriteCaptureJson` | trajectory/contact export |
| render/camera capture | `SceneCaptureComponent2D.CaptureScene`, depth/mask export | RGB/depth/segmentation/camera sync |

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
| `magnetic_force_field` | 磁吸/排斥必须声明 source、subject、mode、strength，并按径向距离变化验证。 | 磁吸球、磁排斥体 |
| `angular_damping_spin_decay` | angular velocity 必须随 damping 衰减，不能无外力增益。 | 陀螺、自转球 |
| `agent_rigidbody_action_coupling` | 目标刚体只能在 action/contact/release evidence 后响应。 | 机器人推箱、角色抛球 |
| `constraint_distance_pendulum_motion` | 约束体必须保持 anchor-body 距离并连续运动。 | 单摆、绳索、铰链 |
| `constraint_momentum_transfer` | 受约束刚体链必须按 contact 顺序传递冲量。 | 牛顿摆、悬挂球链 |
| `elastic_energy_launch` | stored elastic energy 通过 release event 转成 bounded kinetic response。 | 弹簧发射、弹射器 |
| `elastic_constraint_rebound` | 弹性约束必须记录 extension，并在峰值后朝 anchor 回弹。 | 蹦极、弹力绳 |
| `brittle_impact_fracture` | fracture 只能在 contact energy 超过 threshold 后发生，并输出 fragments。 | 玻璃、镜子、杯子、木箱 |

## 5. 校验能力

校验能力分两层：

| Capability | 职责 |
|---|---|
| `render_signal_sync_validation` | 检查 multi-view RGB/depth/segmentation/camera/physics trace 是否帧对齐、视角齐全、depth 非占位。 |
| `physics_verifier_truth_gate` | 对 schema、initial physics、runtime causality、asset binding 和 render/signal evidence 做最终 readiness 判定。 |

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
  -> static_scene_placement
  -> physics_parameter_semantics
  -> runtime_actor_placement_compilation
  -> blueprint_function_invocation
  -> runtime_backend_execution
  -> capability_runtime_artifact_bridge
  -> canonical_signal_capture
  -> render_signal_sync_validation
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
