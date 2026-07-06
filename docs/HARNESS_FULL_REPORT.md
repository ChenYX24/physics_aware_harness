# Physics-Aware Harness 完整使用与设计报告

本文是下载仓库后的主操作文档，说明 harness 的设计、当前功能、初始化方式、UE 环境变量、资产导入、已有模板、Python 工具、TODO，以及其他人如何继续优化这个 harness。

## 1. 项目定位

本仓库是一个 **给 code agent 使用的物理感知仿真 harness**。

它不是：

- 不是前端优先的展示 demo；
- 不是普通 prompt-to-video 应用；
- 不是只服务台球场景的模板工程；
- 不是存放生成视频、私有资产、运行结果的大仓库。

它的目标是让 agent 把一个任务或 prompt 编译成可执行、可验证、可复现的物理仿真包：

```text
Agent task / prompt
  -> capability planning
  -> case spec 或 generated case
  -> 静态场景与资产意图解析
  -> runtime backend：UE 用于真实生产，fallback 用于 verifier 调试
  -> trajectory / contact / render artifact collection
  -> physics verifier
  -> diagnosis / repair suggestion
  -> dataset-ready artifact package
```

本 harness 的两大核心能力是：

1. **动态物理模拟能力**
   - 把物理现象变成参数化 case、trajectory/contact 证据、verifier invariant、正负例和可复现实验。
2. **静态场景摆放构建能力**
   - 在物理运行前，明确对象角色、资产需求、物理关键资产、摆放关系、材质绑定、相机/灯光需求和静态检查。

## 2. 仓库结构

```text
harness/
  assets/          # asset intent、asset registry、asset resolver
  core/            # case/artifact/camera/sync schema 和管理器
  planning/        # capability planner、prompt-to-case wrapper
  runtime/         # fallback 和 UE runtime backend
  verification/    # physics verifier、render sync checker
  packaging/       # dataset package helper

capabilities/      # 机器可读 capability contract
cases/             # golden cases 和 parameterized templates
scripts/           # agent 可调用 CLI 工具
assets/            # 只放 public-safe 示例 registry，不放大资产
ue_template/       # 最小 UE 工程和 ADPPhysicsRuntime plugin 源码
docs/              # setup、schema、authoring、usage、报告
tests/             # regression tests
```

以下路径是本地产物或私有内容，不应提交：

```text
runs/
outputs/
artifacts/
cases/generated/
agent-docs/
_local_inputs/
assets/ue_imports/
assets/downloads/
assets/cache/
assets/*.local.json
```

## 3. 当前已有能力

### 3.1 Pipeline / Agent 阶段能力

| Capability | 当前状态 | 验证内容 |
|---|---|---|
| `pipeline_stage_orchestration` | 已可用 | 将 capability planning、case spec、scene layout、asset binding、runtime、verifier、diagnosis、dataset packaging 作为显式阶段，不允许 silent fallback。 |
| `asset_intent_resolution` | 已可用 | 区分 physics-critical、visual-only、skeletal/animation、blueprint/logic、scene/map 等资产意图，并检索候选资产。 |
| `asset_runtime_binding_invocation` | 已可用 | 记录 top-k candidates、selected asset、proxy fallback reason，并要求 runtime actor binding 对齐 object id。 |
| `static_scene_placement` | 已可用 | 从 case spec + asset resolution 生成 `scene_layout.json`，并检查 object id、support relation、non-overlap、camera coverage 和 physics graph membership。 |
| `scene_spec_compilation` | 部分可用 | 定义 scene spec contract；UE actor placement compiler 仍是 TODO。 |
| `physics_property_constraint_validation` | 新增 contract | 检查 mass、friction、restitution、damping、gravity、material、parameter sweep 的结构化范围和方向性响应。 |
| `capability_runtime_artifact_bridge` | 已可用 | 把 runtime artifact 标准化为 verifier 输入。 |

### 3.2 物理行为能力 / Case Family

| Capability | 当前状态 | 验证内容 |
|---|---|---|
| `rigid_body_contact_causality` | 已可用 | active body 可以主动运动；passive rigid bodies 初始必须静止，只能 runtime contact 后运动。台球、保龄球只是其中的 case family。 |
| `sequential_contact_propagation` | 已可用 | domino/chain reaction 必须按 contact 顺序传播。 |
| `rigid_body_gravity_collision` | 已可用 | falling object 必须下降，并产生 support contact。 |
| `ramp_sliding_friction` | 当前 ramp 分支已可用 | 物体沿斜面下滑，z 下降，位移符合 friction-aware 范围。 |
| `projectile_gravity_motion` | 当前分支已可用 | 抛体必须上升到 apex、随后下降、向前位移并产生落地 contact。 |
| `bounce_restitution_ball` | 当前分支已可用 | 物体必须先下降、产生 support contact，再按 restitution 约束反弹高度，拒绝无接触反弹和能量增益。 |
| `rolling_friction_ball` | 当前分支已可用 | 物体必须有初始水平速度、保持 support contact、速度衰减，滚动距离符合 friction-bounded envelope。 |
| `sliding_crate_friction` | 当前分支已可用 | 箱体/刚体滑动必须保持 support contact、按动摩擦减速并停在距离范围内；低于静摩擦阈值时必须基本不动。 |
| `force_field_wind_drift` | 当前分支已可用 | 风场/力场驱动的轻物体必须声明 wind vector，轨迹漂移方向和距离要符合 wind-aligned envelope，海拔保持在范围内。 |
| `magnetic_force_field` | 当前分支已可用 | 磁吸/排斥必须声明 source、subject、mode、strength；trajectory 必须按 source-relative radial direction 响应。 |
| `mass_ratio_momentum_transfer` | 当前分支已可用 | 接触碰撞必须声明质量标签；碰撞后速度顺序要符合质量比和 restitution envelope，并拒绝无解释能量增益。 |
| `angular_damping_spin_decay` | 当前分支已可用 | 自转刚体必须声明初始角速度和角阻尼，runtime 输出 angular velocity + rotation trace，verifier 检查单调衰减和无外力增益。 |
| `agent_rigidbody_action_coupling` | 当前分支已可用 | agent/robot/character 动作必须写成 action trace；目标刚体只能在 action/contact 或 release/impulse 证据后运动。 |
| `constraint_distance_pendulum_motion` | 当前分支已可用 | 距离/绳长/关节约束必须声明 anchor、constrained body、constraint length，并输出 constraint_trace；单摆只是 smoke family。 |
| `constraint_momentum_transfer` | 当前分支已可用 | 受约束刚体链必须按相邻 contact 顺序传递冲量，末端 receiver 响应必须由 contact 链解释；牛顿摆只是 smoke family。 |
| `elastic_energy_launch` | 当前分支已可用 | 弹性势能释放必须声明 spring/compression/mass，输出 release event；payload 初始静止，释放后速度/能量响应必须在 stored-energy envelope 内。 |
| `elastic_constraint_rebound` | 当前分支已可用 | 弹性绳/蹦极约束必须声明 rest length、max extension、stiffness，并输出 constraint_trace；达到最大拉伸后必须朝 anchor 回弹。 |
| `brittle_impact_fracture` | 当前分支已可用 | 可破碎刚体必须声明 fracture threshold、impact energy、fracture event 和 fragment manifest；玻璃/镜子/杯子/木箱只是 case family。 |

注意：如果从 `main` 使用，`ramp_sliding_friction` 需要先合并当前 ramp 分支。

## 4. 下载仓库后的初始化

推荐 Python 版本：3.13。

```bash
git clone https://github.com/ChenYX24/Agentic-Data-Platform.git
cd Agentic-Data-Platform

python3.13 -m unittest discover -s tests -p 'test*.py'
python3.13 scripts/harness_list_capabilities.py
python3.13 scripts/harness_smoke.py --backend fallback
```

fallback 不需要 UE。它只用于 schema、case、verifier、artifact 逻辑开发，不代表真实物理。

## 5. UE 运行需要填写的变量

真实渲染和真实物理证据需要 UE backend。运行前必须设置以下环境变量：

| 变量 | 是否必须 | 含义 |
|---|---:|---|
| `SIM_STUDIO_UE_PROJECT` | 是 | `.uproject` 绝对路径，通常是 `ue_template/SimulatorStudioTemplate.uproject`。 |
| `SIM_STUDIO_UE_EXECUTABLE` | 是 | `UnrealEditor-Cmd` 或 `UnrealEditor` 路径。 |
| `SIM_STUDIO_UE_MAP` | 是 | UE map package path，例如 `/Game/Maps/MarketEnvironment/Maps/Day.Day`。 |
| `SIM_STUDIO_UE_ACTOR_CLASS` | 是 | runner 使用的 actor class，常用 `/Script/Engine.StaticMeshActor`。 |
| `SIM_STUDIO_ASSET_REGISTRY` | 是 | 带资产路径和物理 metadata 的 JSON registry。 |
| `SIM_STUDIO_UE_CONTACT_EXPORT` | 是 | 必须为 `1`，否则 contact-driven verifier 不可信。 |
| `SIM_STUDIO_UE_RUNNER_CMD` | 是 | 通常是 `python3.13 scripts/harness_local_ue_runner.py`。 |
| `SIM_STUDIO_UE_RENDER_MODE` | 否 | `rgb`、`data` 或 `both`。 |
| `SIM_STUDIO_UE_RGB_CAPTURE_BACKEND` | 否 | 推荐 `scene_capture`，这是当前稳定同步路径。 |
| `SIM_STUDIO_UE_WIDTH` / `SIM_STUDIO_UE_HEIGHT` | 否 | 渲染分辨率。 |
| `SIM_STUDIO_UE_FPS` | 否 | 帧率和 trace 采样目标。 |

示例：

```bash
export SIM_STUDIO_UE_PROJECT="$PWD/ue_template/SimulatorStudioTemplate.uproject"
export SIM_STUDIO_UE_EXECUTABLE="/Users/Shared/Epic Games/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd"
export SIM_STUDIO_UE_MAP="/Game/Maps/MarketEnvironment/Maps/Day.Day"
export SIM_STUDIO_UE_ACTOR_CLASS="/Script/Engine.StaticMeshActor"
export SIM_STUDIO_ASSET_REGISTRY="$PWD/assets/asset_registry.example.json"
export SIM_STUDIO_UE_CONTACT_EXPORT=1
export SIM_STUDIO_UE_RUNNER_CMD="python3.13 scripts/harness_local_ue_runner.py"
export SIM_STUDIO_UE_RENDER_MODE=both
export SIM_STUDIO_UE_RGB_CAPTURE_BACKEND=scene_capture
export SIM_STUDIO_UE_WIDTH=1280
export SIM_STUDIO_UE_HEIGHT=720
export SIM_STUDIO_UE_FPS=60
```

运行单个 UE case：

```bash
python3.13 scripts/harness_run_case.py \
  cases/billiards/six_ball_triangle_low_speed.json \
  --backend ue \
  --mode both \
  --views front_static,side_static,top_down,tracking_subject,event_closeup \
  --render-passes rgb,depth,segmentation \
  --output-root runs/ue_cases
```

## 6. 资产如何导入和组织

不要把大资产提交进 git。建议本地结构：

```text
assets/
  asset_registry.example.json       # 已跟踪，public-safe 示例
  asset_registry.local.json         # 本地真实 registry，忽略
  physics_materials.local.json      # 本地材质表，忽略
  ue_imports/                       # UE 导入资产，忽略
  downloads/                        # 下载包，忽略
  cache/                            # 生成索引，忽略
```

让 harness 使用真实 registry：

```bash
export SIM_STUDIO_ASSET_REGISTRY="$PWD/assets/asset_registry.local.json"
```

physics-critical 资产至少应该有：

- `asset_id`
- `ue_path`
- category/type
- collider 类型
- mass 或 density
- material friction/restitution
- collision profile
- `proxy=true|false`

visual-only 资产可以没有 rigid body 信息，但不能进入 physics graph。

### 从外部 ADP/UE 资产索引导入

脚本：

```bash
python3.13 scripts/import_adp_asset_index.py \
  --source /path/to/AgenticDataPlatform \
  --output-dir assets
```

它会读取外部仓库的 `AssetIndex/ASSETS_INDEX.json`，并转换成 harness 可检索的 registry/report。

重要输出：

| 输出 | 作用 |
|---|---|
| `assets/full_asset_registry.json` | 完整转换后的资产 registry。 |
| `assets/asset_database_manifest.json` | 常用角色的 materialized asset 选择。 |
| `assets/search_report.json` | 常见 query 的 top-k 检索报告。 |
| `assets/scenario_manifest.json` | 可用地图/world 资产。 |

注意：这个脚本不是下载器。它的作用是转换 metadata。真实大资产仍应放在本地、Git LFS、ModelScope 或其他外部存储。

## 7. 静态场景摆放构建能力

静态场景构建是核心能力，不是 UI 附属功能。

当前已经实现：

- `harness/assets/asset_intent.py`
  - 根据 object role 判断 physics-critical / visual-only。
- `harness/assets/asset_registry.py`
  - 按 query、tag、path、category 搜索资产。
- `harness/assets/asset_resolver.py`
  - 把 case object 解析成 top-k candidate、selected asset 或 fallback reason。
- `capabilities/scene_spec_compilation.json`
  - 定义可执行 scene spec contract。
- `capabilities/asset_intent_resolution.json`
  - 定义资产解析 contract。
- `harness/core/scene_layout.py`
  - 定义 object node、bounds、support surface、analytic proxy physics metadata。
- `harness/planning/static_scene_builder.py`
  - 从 case spec 和 asset resolution 生成 object-level layout。
- `harness/verification/static_scene_verifier.py`
  - 检查 non-overlap、support relation、collider/mass/material/collision profile、camera plan。
- `scripts/harness_build_static_scene.py`
  - 生成 `asset_resolution.json`、`scene_layout.json`、`static_scene_report.json`。

当前验证结果：

- `six_ball_triangle_low_speed`：8 个 physics-critical object，8/8 resolved。
- `ramp_roll_low_friction`：2 个 physics-critical object，2/2 resolved。
- `low_speed_single_contact`：static scene layout pass。
- `falling_block_on_floor`：static scene layout pass。
- 人工重叠 negative case：`F3_invalid_initial_physics_state` 被抓到。
- 缺 asset binding negative case：`F2_asset_missing` 被抓到。

仍然缺：

1. UE actor placement compiler。
   - 读取 `scene_layout.json`。
   - 在 UE 中创建 actor、设置 transform、collider、mass、material、collision profile。
   - 生成 camera rig / light rig，并把 runtime actor id 回写 artifact。

## 8. 动态物理 case 系统

每新增一个物理现象，应该遵循这个结构：

```text
capabilities/<capability_id>.json
cases/templates/<template_id>.template.json
cases/<family>/<positive_or_negative_case>.json
harness/verification/<family>_verifier.py
tests/test_harness_<family>_verifier.py
```

最低验收标准：

1. 至少一个正例 golden case 通过。
2. 至少一个负例 regression case 按预期 failure_type 失败。
3. generator 可以生成正负混合 case。
4. fallback backend 可以产出 deterministic trajectory/contact artifact。
5. verifier 检查物理 invariant，不只是检查 video 是否存在。
6. UE path 要么产出真实 artifact，要么明确 fail-fast，不能 silent fallback。

## 9. 已有 templates

| Template | Suite | 状态 | 用途 |
|---|---|---|---|
| `billiards_collision.template.json` | `--suite billiards` | 可运行 | cue/target 碰撞因果。 |
| `bowling_pin_chain.template.json` | `--suite bowling` | 当前分支可运行 | 保龄球主动撞击被动球瓶，球瓶只能由 contact 链激活。 |
| `domino_chain.template.json` | `--suite domino` | 可运行 | 顺序 contact propagation。 |
| `falling_blocks.template.json` | `--suite falling` | 可运行 | 重力/support contact。 |
| `ramp_sliding.template.json` | `--suite ramp` | 当前 ramp 分支可运行 | 斜面摩擦响应。 |
| `projectile_motion.template.json` | `--suite projectile` | 当前分支可运行 | 上抛/抛体轨迹、apex/descent/landing contact。 |
| `bounce_restitution.template.json` | `--suite bounce` | 当前分支可运行 | 恢复系数反弹 envelope。 |
| `rolling_friction.template.json` | `--suite rolling` | 当前分支可运行 | 滚动摩擦、停距、速度衰减。 |
| `sliding_crate_friction.template.json` | `--suite sliding` | 当前分支可运行 | 滑动摩擦和静摩擦阈值。 |
| `wind_balloon_drift.template.json` | `--suite wind` | 当前分支可运行 | 风场/力场方向漂移。 |
| `magnetic_force_field.template.json` | `--suite magnetic` | 当前分支可运行 | 磁吸/排斥径向响应。 |
| `mass_ratio_collision.template.json` | `--suite mass_ratio` | 当前分支可运行 | 质量比碰撞和动量传递。 |
| `angular_damping_spin.template.json` | `--suite spin` | 当前分支可运行 | 角阻尼和旋转衰减。 |
| `agent_rigidbody_action.template.json` | `--suite agent_action` | 当前分支可运行 | agent action trace 到刚体响应。 |
| `pendulum_contact.template.json` | `--suite pendulum` | 当前分支可运行 | 距离/关节约束、constraint_trace、长度保持和连续运动。 |
| `constraint_momentum_transfer.template.json` | `--suite impulse_chain` | 当前分支可运行 | 受约束冲量链、相邻接触顺序、末端 receiver 响应。 |
| `elastic_energy_launch.template.json` | `--suite elastic_launch` | 当前分支可运行 | 弹性势能释放和 energy envelope。 |
| `elastic_constraint_rebound.template.json` | `--suite elastic_constraint` | 当前分支可运行 | 弹性绳/约束回弹。 |
| `brittle_impact_fracture.template.json` | `--suite fracture` | 当前分支可运行 | 接触能量阈值破碎、fracture event、fragment manifest。 |

生成 case：

```bash
python3.13 scripts/harness_generate_cases.py \
  --suite billiards \
  --count 10 \
  --seed 42 \
  --out cases/generated/billiards_seed42
```

运行 generated cases：

```bash
python3.13 scripts/harness_run_case_batch.py \
  cases/generated/billiards_seed42 \
  --backend fallback
```

```bash
python3.13 scripts/harness_generate_cases.py --suite bowling --count 10 --seed 72 --out cases/generated/bowling_seed72
python3.13 scripts/harness_run_case_batch.py cases/generated/bowling_seed72 --backend fallback
# 10 cases: positive pass + negative caught, unexpected 0
```

## 10. Python 脚本工具说明

| 脚本 | 作用 | 常用命令 |
|---|---|---|
| `scripts/harness_list_capabilities.py` | 列出 capability contracts。 | `python3.13 scripts/harness_list_capabilities.py --json` |
| `scripts/harness_smoke.py` | 跑最小 golden smoke suite。 | `python3.13 scripts/harness_smoke.py --backend fallback` |
| `scripts/harness_generate_cases.py` | 从 template 生成参数化 cases。 | `python3.13 scripts/harness_generate_cases.py --suite billiards --count 20 --seed 42 --out cases/generated/billiards_seed42` |
| `scripts/harness_run_case.py` | 跑单个 case。 | `python3.13 scripts/harness_run_case.py cases/billiards/low_speed_single_contact.json --backend fallback` |
| `scripts/harness_run_case_batch.py` | 跑目录下所有 case。 | `python3.13 scripts/harness_run_case_batch.py cases/billiards --backend fallback` |
| `scripts/harness_verify_run.py` | 验证单个 run directory。 | `python3.13 scripts/harness_verify_run.py runs/...` |
| `scripts/harness_verify_batch.py` | 验证/汇总 batch run。 | `python3.13 scripts/harness_verify_batch.py runs/...` |
| `scripts/harness_package_dataset.py` | 打包 run artifacts。 | `python3.13 scripts/harness_package_dataset.py runs/...` |
| `scripts/harness_local_ue_runner.py` | 本地 UE runner bridge。 | 设置 `SIM_STUDIO_UE_RUNNER_CMD` 指向它。 |
| `scripts/native_ue_physics_phenomena_scene.py` | UE Python scene/capture 实现。 | 通常由 `harness_local_ue_runner.py` 调用。 |
| `scripts/import_adp_asset_index.py` | 把外部 ADP asset index 转换成 harness registry。 | `python3.13 scripts/import_adp_asset_index.py --source /path/to/adp --output-dir assets` |
| `run_experiment.py` | dual-pass experiment 入口。 | `python3.13 run_experiment.py --mode both --batch 10 --seed 42` |

## 11. Artifact Contract

一个 run 应该输出：

```text
runs/<run_id>/
  case_spec.json
  scene_spec.json
  camera_plan.json
  trajectory.json
  contact_events.json
  constraint_trace.json
  camera_trajectory.json
  render_manifest.json
  render_pass_manifest.json
  render_sync_report.json
  verifier_report.json
  run_readiness.json
  views/<camera_id>/
    rgb.mp4
    depth.exr
    segmentation.png
    meta.json
```

fallback 可以写 placeholder render artifact，但必须标记为非 UE。UE production run 不能把 fallback artifact 当成真实证据。

## 12. 当前验证快照

当前分支最近跑过：

```bash
python3.13 -m unittest discover -s tests -p 'test*.py'
# 93 tests OK
```

```bash
python3.13 scripts/harness_smoke.py --backend fallback
# 16/16 expectation met
```

```bash
python3.13 scripts/harness_run_case_batch.py cases/ramp --backend fallback
# 4 cases: 2 positive pass, 2 negative caught, unexpected 0
```

```bash
python3.13 scripts/harness_generate_cases.py --suite ramp --count 10 --seed 45 --out cases/generated/pdf_ramp_seed45
python3.13 scripts/harness_run_case_batch.py cases/generated/pdf_ramp_seed45 --backend fallback
# 10 cases: 7 positive pass, 3 negative caught, unexpected 0
```

```bash
python3.13 scripts/harness_generate_cases.py --suite projectile --count 10 --seed 46 --out cases/generated/projectile_seed46
python3.13 scripts/harness_run_case_batch.py cases/generated/projectile_seed46 --backend fallback
# 10 cases: 7 positive pass, 3 negative caught, unexpected 0
```

```bash
python3.13 scripts/harness_generate_cases.py --suite bounce --count 10 --seed 47 --out cases/generated/bounce_seed47
python3.13 scripts/harness_run_case_batch.py cases/generated/bounce_seed47 --backend fallback
# 10 cases: 7 positive pass, 3 negative caught, unexpected 0
```

```bash
python3.13 scripts/harness_generate_cases.py --suite rolling --count 10 --seed 48 --out cases/generated/rolling_seed48
python3.13 scripts/harness_run_case_batch.py cases/generated/rolling_seed48 --backend fallback
# 10 cases: 7 positive pass, 3 negative caught, unexpected 0
```

```bash
python3.13 scripts/harness_generate_cases.py --suite sliding --count 10 --seed 49 --out cases/generated/sliding_seed49
python3.13 scripts/harness_run_case_batch.py cases/generated/sliding_seed49 --backend fallback
# 10 cases: 7 positive pass, 3 negative caught, unexpected 0
```

```bash
python3.13 scripts/harness_generate_cases.py --suite wind --count 10 --seed 50 --out cases/generated/wind_seed50
python3.13 scripts/harness_run_case_batch.py cases/generated/wind_seed50 --backend fallback
# 10 cases: 7 positive pass, 3 negative caught, unexpected 0
```

```bash
python3.13 scripts/harness_generate_cases.py --suite magnetic --count 10 --seed 59 --out cases/generated/magnetic_seed59
python3.13 scripts/harness_run_case_batch.py cases/generated/magnetic_seed59 --backend fallback
# 10 cases: 7 positive pass, 3 negative caught, unexpected 0
```

```bash
python3.13 scripts/harness_generate_cases.py --suite mass_ratio --count 10 --seed 51 --out cases/generated/mass_ratio_seed51
python3.13 scripts/harness_run_case_batch.py cases/generated/mass_ratio_seed51 --backend fallback
# 10 cases: 7 positive pass, 3 negative caught, unexpected 0
```

```bash
python3.13 scripts/harness_generate_cases.py --suite spin --count 10 --seed 52 --out cases/generated/spin_seed52
python3.13 scripts/harness_run_case_batch.py cases/generated/spin_seed52 --backend fallback
# 10 cases: 7 positive pass, 3 negative caught, unexpected 0
```

```bash
python3.13 scripts/harness_generate_cases.py --suite agent_action --count 10 --seed 53 --out cases/generated/agent_action_seed53
python3.13 scripts/harness_run_case_batch.py cases/generated/agent_action_seed53 --backend fallback
# 10 cases: 7 positive pass, 3 negative caught, unexpected 0
```

```bash
python3.13 scripts/harness_generate_cases.py --suite pendulum --count 10 --seed 54 --out cases/generated/pendulum_seed54
python3.13 scripts/harness_run_case_batch.py cases/generated/pendulum_seed54 --backend fallback
# 10 cases: 7 positive pass, 3 negative caught, unexpected 0
```

```bash
python3.13 scripts/harness_generate_cases.py --suite impulse_chain --count 10 --seed 55 --out cases/generated/impulse_chain_seed55
python3.13 scripts/harness_run_case_batch.py cases/generated/impulse_chain_seed55 --backend fallback
# 10 cases: 7 positive pass, 3 negative caught, unexpected 0
```

静态资产解析：

- 台球三角阵 case：8/8 physics-critical assets resolved。
- 斜面 case：2/2 physics-critical assets resolved。

## 13. PDF 派生物理 TODO

来源：用户提供的物理模拟场景 PDF。public 文档不保留本机绝对路径。

PDF 隐含的统一参数体系：

| 参数组 | 例子 |
|---|---|
| object parameters | mass、volume、material、shape、hardness、elasticity、density |
| initial motion | velocity、direction、angle、height、initial position |
| environment | gravity、friction、air drag、wind speed、water density |
| result controls | fracture threshold、restitution、damping、magnetic strength、buoyancy |

TODO：

| 优先级 | Case | Harness capability | 状态 |
|---|---|---|---|
| P0 | 台球/保龄球/球体接触碰撞 | `rigid_body_contact_causality` | fallback/verifier 已有；台球和保龄球都是 case family；UE contact path TODO |
| P0 | 多米诺/保龄球链式碰撞 | `sequential_contact_propagation` 扩展 | domino 已有；bowling contact-causality family 已有；更复杂 pin fan-out 仍可扩展 |
| P0 | 掉落/上抛/抛体 | `projectile_gravity_motion` | 当前分支已有 fallback/verifier；UE TODO |
| P0 | 斜面滚动/下滑/上滚 | `ramp_sliding_friction` | ramp 分支已有 fallback/verifier；UE TODO |
| P1 | 皮球/刚体反弹 | `bounce_restitution_ball` | 当前分支已有 fallback/verifier；UE restitution material/contact TODO |
| P1 | 滚动摩擦/停距 | `rolling_friction_ball` | 当前分支已有 fallback/verifier；UE rolling friction material/contact TODO |
| P1 | 滑动箱体/静摩擦阈值 | `sliding_crate_friction` | 当前分支已有 fallback/verifier；UE static/dynamic friction material/contact TODO |
| P1 | 质量比碰撞/动量传递 | `mass_ratio_momentum_transfer` | 当前分支已有 fallback/verifier；UE mass metadata/contact impulse TODO |
| P1 | 角阻尼/自转衰减 | `angular_damping_spin_decay` | 当前分支已有 fallback/verifier；UE angular velocity / damping / inertia export TODO |
| P1 | agent 推/抛刚体 | `agent_rigidbody_action_coupling` | 当前分支已有 fallback/verifier；UE action trace / skeletal controller / impulse export TODO |
| P1 | 弹簧/弹射/弹性势能发射 | `elastic_energy_launch` | 当前分支已有 fallback/verifier；UE spring/release event / energy label export TODO |
| P1 | 单摆/绳长/距离约束 | `constraint_distance_pendulum_motion` | 当前分支已有 fallback/verifier；UE PhysicsConstraint / Chaos joint trace TODO |
| P1 | 牛顿摆/悬挂球链 | `constraint_momentum_transfer` | 当前分支已有 fallback/verifier；UE suspension constraint / contact impulse / receiver velocity TODO |
| P1 | 绳子/蹦极弹性 | `elastic_constraint_rebound` | 当前分支已有 fallback/verifier；UE elastic PhysicsConstraint / extension trace / rebound velocity TODO |
| P1 | 玻璃/镜子/玻璃杯/木箱破碎 | `brittle_impact_fracture` | fallback/verifier 已有；UE Chaos/destructible fracture event、impact energy、fragment export TODO |
| P2 | 风吹纸片/气球 | `force_field_wind_drift` | 当前分支已有 fallback/verifier；UE force field / wind volume TODO |
| P2 | 磁吸/排斥 | `magnetic_force_field` | fallback/verifier 已有；UE magnetic field / radial force volume TODO |
| P3 | 浮力/水流/搅拌流体 | fluid capability family | 暂缓，等待 fluid backend |

推荐下一步顺序：

1. 提交 bowling chain case family。
2. 做 `UE actor placement compiler`。
3. 回到 UE actor placement compiler，让 `scene_layout.json` 驱动真实 actor 生成。
4. 让 billiards/ramp/falling/projectile/fracture/magnetic 通过真实 trajectory/contact verifier。

## 14. 其他人如何扩展/优化 harness

新增一个物理现象时：

1. 新增 capability contract：
   - `capabilities/<capability_id>.json`
2. 新增 golden cases：
   - `cases/<family>/<case>.json`
3. 新增参数化 template：
   - `cases/templates/<family>.template.json`
4. 更新 case generator：
   - `scripts/harness_generate_cases.py`
5. 更新 fallback trajectory：
   - `harness/runtime/fallback_backend.py`
6. 新增 verifier：
   - `harness/verification/<family>_verifier.py`
   - 从 `harness/verification/physics_verifier.py` 路由
7. 新增 tests：
   - 正例必须 pass；
   - 负例必须按预期 failure_type fail；
   - generated cases 不应出现 unexpected pass/fail。
8. 如果需要资产，更新：
   - public proxy 示例：`assets/asset_registry.example.json`
   - 本地真实资产：`assets/asset_registry.local.json`
9. 如果需要 UE，更新：
   - `scripts/harness_local_ue_runner.py`
   - `scripts/native_ue_physics_phenomena_scene.py`
   - 如需 C++ trace，再改 `ue_template/Plugins/ADPPhysicsRuntime/`

质量关卡：

```bash
python3.13 -m py_compile \
  scripts/harness_generate_cases.py \
  scripts/harness_run_case.py \
  scripts/harness_run_case_batch.py \
  harness/runtime/fallback_backend.py \
  harness/verification/physics_verifier.py

python3.13 -m unittest discover -s tests -p 'test*.py'
find capabilities cases config assets -name '*.json' -print0 | xargs -0 -n1 python3.13 -m json.tool >/dev/null
git diff --check
```

没有正例和负例的 capability，不应标记为完成。

## 15. 当前限制

- fallback backend 是 deterministic toy/proxy，只适合 verifier 开发，不是 ground-truth physics。
- UE SceneCapture multi-view RGB/depth/segmentation 是当前稳定 data path；highres viewport 只作为 debug。
- 一些 UE rigid-body trajectory 还需要 runtime stepping 修复，才能让真实物理 trace 通过 verifier。
- 静态场景摆放已有 builder/verifier；UE actor placement compiler 仍未接入。
- 流体类 case 暂缓，等真实 fluid backend 或可靠 proxy 方案。
