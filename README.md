# Physics-Aware Simulation Harness

面向 Agent 的物理仿真控制层：把 prompt/case 转成资产选择、场景、物理初态、UE 渲染、多模态产物和可审计质量报告。Harness 不自研通用物理引擎；当前接触刚体主路径是 **CaseSpec 只定义初始状态，UE Chaos 固定步进自主求解，再把求解后的状态缓存交给多机位渲染**。MuJoCo 保留为显式 sweep、对照和离线 replay 后端。

```text
prompt / CaseSpec
  -> prompt 改写与能力规划
  -> 资产检索、依赖 bundle、map/相机/灯光预设
  -> 场景构建与物理参数初始化
  -> UE Chaos simulator.step(dt) -> solver state cache
  -> 必要的耦合处理（例如粒子 -> 流体表面）
  -> UE render(state, cameras, RGB/depth/segmentation)
  -> 硬门校验 -> best-of-N -> 人工 keep/reject
```

帧数不要求固定为 60。每次 run 必须在 manifest 中声明时间采样，并保证物理状态、相机和各传感器通道按同一 frame id 对齐。

## 仓库与本地工作区边界

| 位置 | 用途 | 是否进入 GitHub |
|---|---|---|
| `<repo>` | 代码、CaseSpec、能力契约、测试和公共安全示例 | 是，人工验收后再提交 |
| `$SIM_HARNESS_WORKSPACE` | 真实资产目录、UE mount、运行结果、视频、缓存、隔离环境、review 状态 | 否 |
| `<repo>/agent-docs` | 本地 Agent 计划、ADR、研究与检查报告 | 否，由 `.gitignore` 排除 |
| `$SIM_HARNESS_ADP_ROOT` | 操作者提供的 UE 资产源 | 否，只读来源 |
| `<legacy-archive>` | 历史实现参考 | 否，只读 |

仓库内不再使用 `runs/` 或根目录 `videos/` 存真实输出。相对输出路径由 harness 自动解析到本地工作区；单次/批量未校验预览默认进入 `review/probes/`，`harness_iterate_case.py` 只把 hard-gate 胜者发布到 `review/inbox/`。不要把 MP4、EXR、OBJ、UE 资产、虚拟环境或缓存提交到 GitHub。

正式本地 case 使用统一路由：`cases/<physics>/<scenario>/<vNNN_description>/`。例如 `--case-route rigid_collision/billiards/v002_complete_angle_matrix`；该路径由 `harness.core.workspace.case_output_root()` 校验并保证位于 Git 工作树之外。

两份自动导航分别回答“能跑什么”和“已经跑了什么”：仓库 [cases/TREE.md](cases/TREE.md) 索引所有 CaseSpec/模板；本地 workspace 的 `cases/TREE.md` 索引实际三层 case route、版本用途和标准子目录。新增/清理 case 后执行：

```bash
python3.13 scripts/harness_case_tree.py \
  --workspace-root "$SIM_HARNESS_WORKSPACE"
python3.13 scripts/harness_case_tree.py --check \
  --workspace-root "$SIM_HARNESS_WORKSPACE"
```

完整 Excel Case 表的仓库级索引是 [`config/case_catalog.json`](config/case_catalog.json)。它保留 22 行业务 Case、理论变量组合数、当前 capability 和已有 CaseSpec 的对应关系；没有实现的能力保持空映射，不能用相似 case 冒充完成。

第一次规划变量空间时使用机器可读 variant plan。plan 记录全部 Cartesian 组合，但默认只选择 baseline 加单因素变化（OFAT）做首轮渲染。每个 level 通过 JSON Pointer 同步修改 CaseSpec 中的所有相关字段，因此后续无需 LLM，直接编辑 JSON 后即可 materialize 或渲染：

```bash
python3.13 scripts/harness_case_library.py show \
  config/variant_plans/newton_cradle_release_angle.json
python3.13 scripts/harness_case_library.py catalog billiards_break
python3.13 scripts/harness_case_library.py materialize \
  config/variant_plans/newton_cradle_release_angle.json \
  --output-dir /tmp/newton-variants
python3.13 scripts/harness_case_library.py render \
  config/variant_plans/newton_cradle_release_angle.json \
  --variant release_angle-25deg

# 同一入口请求三模态；Harness 复用 solver state，顺序执行 RGB 与 data pass
python3.13 scripts/harness_case_library.py render \
  config/variant_plans/newton_cradle_release_angle.json \
  --variant release_angle-25deg \
  --render-passes rgb,depth,segmentation

# 只有正式五机位三模态 hard-gate 交付才使用 formal
python3.13 scripts/harness_case_library.py render \
  config/variant_plans/newton_cradle_release_angle.json \
  --variant release_angle-25deg \
  --formal
```

`catalog <case-id>` 会为 22 行全部返回轴数、完整组合数和首轮选择数。Excel
只给数量、没有给具体 level 值时，catalog 使用 `level_01` 等符号层级并明确
标记 `explicit_json_pointer_edits_required`；只有绑定了真实 CaseSpec 值和 JSON
Pointer 的 `config/variant_plans/*.json` 才能直接 render，不能把符号规划冒充
可执行 case。

### 参数编辑器与多变体批次

[`tools/case_parameter_editor.html`](tools/case_parameter_editor.html) 是零依赖的
本地参数工作台。它直接解析 variant plan 和基础 CaseSpec，不调用 LLM：

```bash
python3.13 -m http.server 8000
# 浏览器打开 http://127.0.0.1:8000/tools/case_parameter_editor.html
```

variant plan 把可编辑内容分成三层：

- `axes[].tier=primary`：模型第一次规划时明确要研究的变量。`levels` 提供默认档，
  `value_pointer` 和 `input` 允许人工输入自定义值。
- `ui.fields[].tier=common`：质量、时长、FPS 等常用设置；一个字段可以同步写入多个
  JSON Pointer。
- `ui.fields[].tier=advanced`：物理解算频率、响应阈值、UE Map 等不建议日常修改的
  字段；页面折叠显示并保留风险提示。
- `ui.computed_edits`：改动速度、质量或撞击时间后自动更新能量、初态和期望状态。
  只支持 JSON Pointer 与 `add/sub/mul/div/pow/bands` 数据算子，不执行模型提供的代码。

可运行示例是
[`config/variant_plans/glass_panel_impact_speed.json`](config/variant_plans/glass_panel_impact_speed.json)。
它预先考虑 1/2/3 m/s 三档，但页面只默认勾选 baseline；其他模型规划档和人工
自定义档都进入“变体队列”，由用户决定哪些需要渲染。保存当前参数后，页面会把
完整 CaseSpec、机位和 RGB/depth/segmentation 选择写入一个
`harness_parameter_batch_v1` JSON。先检查、再直接串行渲染：

```bash
python3.13 scripts/harness_render_parameter_batch.py \
  ./glass_panel_e16_shatter__render_batch.json --dry-run
python3.13 scripts/harness_render_parameter_batch.py \
  ./glass_panel_e16_shatter__render_batch.json --execute
```

批次脚本会验证每个内嵌 CaseSpec，把输入持久化到对应 case route 的
`inputs/parameter_batches/`，再按每个变体自己的 `views × passes` 调用现有 UE
入口；任何一项失败默认停止，避免继续消耗渲染资源。

UE 的 RGB 与 metric depth/instance segmentation 依赖不同 render pass，不能在
同一张 capture 中同时取得三份 canonical truth；上面的三模态命令会在同一个
solver state cache 上顺序捕获并按 frame id 对齐。一次命令可以请求多个机位；
UE 会复用同一份 solver state，逐机位生成各自的完整序列。`render` 默认五机位 RGB，
`--render-passes` 可随时切换，整个过程不需要 LLM。`harness_run_case.py` 与
batch runner 的自定义 probe 也默认 RGB；`candidate/publish` profile 和
`--formal` 仍强制完整传感器契约。

本地历史 run 的规范化视图由 `harness_case_library.py organize` 生成。先 dry-run，再 `--apply`；它只为 canonical source 建硬链接，不复制视频/EXR，也不删除旧目录：

```text
cases/<physics>/<scenario>/<version>/
  case_index.json
  variants/<variant>/
    variant_manifest.json
    rgb/<camera>.mp4
    depth/<camera>/{preview.mp4,frames/*.exr}
    segmentation/<camera>/{preview.mp4,frames/*.exr}
    overall/{rgb,depth,segmentation}.mp4
```

`variant_manifest.json` 和 `case_index.json` 会把每个 source 标成
`hard_gate_passed`、`hard_gate_failed` 或 `legacy_unverified`。organize 只整理
媒体，不提升质量状态；只有正式质量门和 review keep 能晋级候选。

## 批量执行成本漏斗

所有新 case 必须按 `solver/cache → smoke → candidate → publish` 晋级，不能直接批量跑高分辨率完整交付：

| profile | 默认内容 | 资格 |
|---|---|---|
| `smoke` | 320×180、`event_closeup`、RGB | diagnostic only；先验证事件和主体可见性 |
| `candidate` | 640×360、3 静态+2 运动、RGB/depth/segmentation | 完整 hard gate 通过后供 review |
| `publish` | 1280×720、五机位三模态 | 仅在用户 keep candidate 后运行 |

```bash
# 先跑廉价单机位，失败立即停止
python3.13 scripts/harness_run_case.py cases/domino/six_domino_chain.json \
  --backend ue --profile smoke

# smoke 通过后才跑完整候选
python3.13 scripts/harness_run_case.py cases/domino/six_domino_chain.json \
  --backend ue --profile candidate
```

每个 profile run 写 `execution_profile.json` 与 `efficiency_report.json`，包含实际 view/modality/frame 数、solver pass 数、capture/native/wall 时间和 throughput。`smoke` 永远不能进入正式 review delivery。质量门失败会撤销 `local_preview_ready/reference_ready`，但独立保留 `physics_ready`，便于区分“求解正确、传感器或画面失败”。

仓库主要目录：

| 路径 | 内容 |
|---|---|
| `harness/` | planning、asset、runtime、verification、artifact 代码 |
| `capabilities/` | 机器可读能力契约 |
| `cases/` | 可复现 CaseSpec；[TREE.md](cases/TREE.md) 自动解释每个文件夹、Case 和 Harness 不变量 |
| `scripts/harness_*.py` | Agent/人可调用 CLI |
| `ue_template/` | 最小 UE 项目模板和 `ADPPhysicsRuntime` plugin |
| `tests/` | 不变量与回归测试 |
| `agent-docs/` | 本地计划、ADR、知识、调试和检查报告；不放核心代码，当前不随 GitHub 分发 |

## 当前真实状态

以下 review 状态核对于 **2026-07-14**；机器可读 SSOT 始终是各版本的 `case_status.json` 与 review manifest。

- UE 5.7.4、MuJoCo 3.10.0、Python 3.13、ffmpeg/ffprobe 已可用。
- 16 球 case 已跑通 `initial_state_only -> UE Chaos Game World -> raw C++ state capture -> multi-view replay/render`。输入不含预计算轨迹；轨迹只作为求解后的证据与统一渲染缓存。
- map gate 会验证请求 package 对应的 `.umap` 是否存在，并核对运行时实际打开的 package；不再以字符串或假报告代替加载。
- 相机计划支持 `front_static`、`side_static`、`top_down` 三静态和 `tracking_subject`、`event_closeup` 两动态；完整 case 必须显式选择交付机位，默认单机位只用于 smoke。
- 正式 complete case 的 RGB 真值是 native UE MP4；depth/segmentation 真值是源 run 中逐帧 OpenEXR。每个 run 的 `overall/*.mp4` 与版本根三个 `overall/*.mp4` 都是人工 review 预览，不能替代米制深度或精确 instance ID。
- v9 已被用户 reject 并删除：画面中球看起来未运动，且实测 depth EXR 为常量。
- v1 分为历史 RGB 参考和当前 Harness 五角度完整复现两层，均保存在 `$SIM_HARNESS_WORKSPACE/review/kept/`；SSOT 是相应版本根的 `case_status.json`。
- v2 的 `-12/-6/0/+6/+12°` 真实 UE 技术矩阵已完成并由用户 keep：每条件 5 秒、24 FPS，`event_closeup + top_down` 每机位 RGB/depth/21-instance segmentation 各 121 帧，五个条件全部通过 solver provenance、严格 camera ID、跨模态同步、palette closure、15/15 接触传播和 metric depth 几何门。
- v3 速度矩阵包含 `1.8/2.8/4.2 m/s` 三个真实 UE run，固定其他条件用于因果比较；当前为 `kept + local_preview`。

台球 v1/v2/v3 与 5 枚多米诺骨牌 v001 现在作为回归基线；多米诺只定义首枚初始倾角，后续倾倒/接触均来自 UE Chaos，39-MP4 历史包已由用户 keep。玻璃 v003 已改为启用重力的弹道钢球、原生 UE 接触点驱动 strain、撞点区域径向预切拓扑；120 Hz smoke 的 native contact 在 frame 7、fracture 在 frame 8，中心误差为 `0.000042 cm`。这仍不是 Chaos 在任意撞点运行时重建拓扑；未知撞点要走 `contact probe → impact-centered asset build/cache → deterministic rerun`。Genesis→UE 流体技术链已通但视觉/分割/运动相机门未通过，所以落高矩阵暂不批量运行。新正式交付固定 `front_static,side_static,top_down` 三静态与 `tracking_subject,event_closeup` 两运动机位，并为每个 run 和整个 bundle分别生成 RGB/depth/segmentation overall。
- depth 使用 post-process `SceneDepth` 的 `view_z` float EXR，存储值乘 `10000` 得厘米；逐帧门用 table instance mask、runtime camera echo 与射线/平面几何验证。Viridis `depth_preview.mp4` 只供人工查看，不改变 canonical EXR。
- 本地资产源有 2892/2892 个已物化 UE package，registry 已为 2892/2892 条目关联缩略图；21 个 map 均有目录缩略图和 3 组预设定义，但尚未批量生成 63 张真实“map×灯光/相机”预览。
- `ue_template/Content/AgenticGenerated` 中曾物理残留的 35 个 ignored 本地 `.uasset` 已迁移到 workspace `catalog/local_generated/ue_template_agentic_generated_v24_20260714/`；模板目录不再混放本地二进制资产。
- 所有本地资产目前标记为 `UNVERIFIED_LOCAL_ENTITLEMENT`。缺少可发布的 provenance/license，因此 run 的 `publication_tier=local_preview`、`reference_ready=false`；这不是渲染失败。
- Genesis 1.2.1 WCSPH + SplashSurf 已完成 1728 粒子、19 帧 cache、19 个 OBJ 与 UE StaticMesh sequence replay 技术链；同一 solver cache 已生成五机位 RGB/depth/instance segmentation，UE replay 正确声明 `solver_pass_count=0`。当前流体候选仍被自动门 reject：RGB 主体不可持续观察、instance palette closure 缺失、tracking camera 静止。粒子/cache/depth 技术证据可保留，但不能批量扩展或进入 review。落高 0.55/0.65/0.75 m 三档 CaseSpec 已就绪，baseline 修复前不运行。
- 弹性、磁力、风力、破碎仍是后续能力；PhysX/Flow/Blast 与 SPHinXsys 当前只作设计或高保真对照参考。
- prompt compiler 已接到 `harness_run_case.py --prompt`；CaseSpec 的 `scene.map_preference` 与 `scene.lighting_preset` 会在没有显式环境变量覆盖时进入 UE 主链。它仍是少量确定性模板，不是通用自然语言场景生成器。
- 首个刚体切片已贯通重力、质量、摩擦、恢复、初始线/角速度、线/角阻尼，并记录 solver 计算的惯量；自定义惯量张量和更广泛约束参数仍待扩展。

## 新机器 Setup

这里的 Setup 是从一台没有本项目 workspace/cache 的机器开始：clone 仓库，检查 Python 3.13、FFmpeg/ffprobe、UE 5.7 与插件编译环境；在 Git 仓外初始化 workspace；导入并索引用户提供且有权限使用的真实资产；最后用真实 UE backend 生成 RGB、metric depth、instance segmentation、solver/camera state，并通过质量硬门。

仓库已经提供统一 `bootstrap` 和 `doctor`；当前仍缺 dependency lock 和第二台干净机器的真实 UE cold-start 验收，所以状态是 **PARTIAL**，不能称为一键安装。资产源及其许可/entitlement 也必须由操作者提供，Harness 不会从 Git 猜测或静默下载。

使用本机 Python 3.13：

```bash
git clone https://github.com/ChenYX24/physics_aware_harness.git
cd physics_aware_harness
export SIM_HARNESS_WORKSPACE="$HOME/SimulatorWorkspace/physics_aware_harness"
export SIM_HARNESS_ADP_ROOT=/absolute/path/to/AgenticDataPlatform

python3.13 scripts/harness_workspace.py bootstrap \
  --adp-content "$SIM_HARNESS_ADP_ROOT/Content"
python3.13 scripts/harness_workspace.py build-ue-plugin \
  --ue-executable /absolute/path/to/UnrealEditor-Cmd
python3.13 scripts/harness_workspace.py doctor \
  --ue-executable /absolute/path/to/UnrealEditor-Cmd \
  --asset-content "$SIM_HARNESS_ADP_ROOT/Content"
python3.13 scripts/harness_workspace.py status
```

`build-ue-plugin` 会使用 UE 自带的 `RunUAT.sh BuildPlugin`，把与当前 UE
版本和宿主平台匹配的插件编译到 workspace cache，再原子切换 workspace
插件软链接；不会把 Linux/Mac 二进制写回 Git。第一次 doctor 只应达到
`ue_config_ready=true`；`ue_ready` 还要求一次已通过
hard gate 的真实 UE smoke。完成 smoke 后再提供其 run 目录：

```bash
python3.13 scripts/harness_workspace.py doctor \
  --ue-executable /absolute/path/to/UnrealEditor-Cmd \
  --asset-content "$SIM_HARNESS_ADP_ROOT/Content" \
  --native-smoke-run "$SIM_HARNESS_WORKSPACE/cases/<physics>/<scenario>/<version>/<run>"
```

重新生成本地资产、map 和依赖分组目录：

```bash
python3.13 scripts/import_adp_asset_index.py \
  --source "$SIM_HARNESS_ADP_ROOT" \
  --repo-root "$SIM_HARNESS_ADP_ROOT" \
  --output-dir "$SIM_HARNESS_WORKSPACE/catalog/adp"
```

输出包括：

```text
catalog/adp/
  asset_registry.local.json   # 描述、分类、材质猜测、缩略图、UE path、质量状态
  asset_group_index.json      # 同名、同用途和依赖/Blueprint bundle
  map_catalog.json            # map 描述、依赖与相机/灯光预设
```

资产获取有三个明确层级：

1. `preimported`：先由人导入 UE 资产，再让 harness 建目录；当前唯一完整路径。
2. `harness_generate`：记录生成输入、导入结果和 provenance；协议已预留，端到端生成器未完成。
3. `harness_find_at_runtime`：运行时发现候选；必须先 materialize 并完成许可检查，才能用于 reference 发布。

## 配置并运行 UE

```bash
cd /path/to/physics_aware_harness
export SIM_HARNESS_WORKSPACE="$HOME/SimulatorWorkspace/physics_aware_harness"
export SIM_STUDIO_UE_EXECUTABLE="/Users/Shared/Epic Games/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd"
export SIM_STUDIO_UE_MAP="/Game/Maps/MarketEnvironment/Maps/Day.Day"
export SIM_STUDIO_UE_ACTOR_CLASS="/Script/Engine.StaticMeshActor"
export SIM_STUDIO_ASSET_REGISTRY="$SIM_HARNESS_WORKSPACE/catalog/adp/asset_registry.local.json"
export SIM_STUDIO_UE_CONTACT_EXPORT=1
export SIM_STUDIO_UE_RUNNER_CMD="python3.13 scripts/harness_local_ue_runner.py"
export SIM_STUDIO_UE_RGB_CAPTURE_BACKEND=scene_capture
# Optional on a shared multi-GPU host:
export SIM_STUDIO_UE_GRAPHICS_ADAPTER=3
export SIM_HARNESS_ALLOW_LOCAL_PREVIEW_ASSETS=1
export SIM_STUDIO_UE_RIGID_MODE=chaos_live
```

默认 workspace 是 `~/SimulatorWorkspace/physics_aware_harness`；`harness_run_case.py` 会把默认或显式 workspace 传给 backend，并自动使用其中的 `ue/SimulatorWorkspace.uproject`。只有自定义 workspace 时才需要导出 `SIM_HARNESS_WORKSPACE`，改用其他 UE 工程时才设置 `SIM_STUDIO_UE_PROJECT`；显式值始终优先，找不到真实 `.uproject` 时仍会 fail-closed。UE executable、asset registry、actor class 和 runner command 仍是机器级配置，必须按上面的真实路径提供。

需要“对象 A 先落下，之后再释放对象 B”时，CaseSpec 可在 B 上声明 `release_time_s`，并按需提供 `hold_position_m`、`release_position_m`、`release_velocity_m_s` 与 `release_angular_velocity_deg_s`。这只在释放帧初始化一次；之后的位置和碰撞完全由 solver 产生，不能用于逐帧轨迹注入。

复现当前 16 球 0° 双机位技术路径时，先写入 transient `runs/`，避免覆盖 canonical/review 中的用户候选：

```bash
export SIM_STUDIO_UE_WIDTH=640
export SIM_STUDIO_UE_HEIGHT=360
export SIM_STUDIO_UE_FPS=24
export SIM_STUDIO_UE_LIGHTING_PRESET=map_lights_balanced_fill

python3.13 scripts/harness_run_case.py \
  cases/billiards/sixteen_ball_reference_break.json \
  --backend ue \
  --output-root runs/v002_repro \
  --views event_closeup,top_down \
  --render-passes rgb,depth,segmentation \
  --mode both
```

运行后必须对实际 run 目录调用 `scripts/harness_evaluate_run.py`；进程退出 0 不能替代质量门。正式 v002 的 L1 状态在 workspace `cases/rigid_collision/billiards/v002_complete_angle_matrix/case_status.json`。

也可以从 prompt 直接生成保守 CaseSpec 并运行；产出的最终 CaseSpec 会写入对应 run：

```bash
python3.13 scripts/harness_run_case.py \
  --prompt "A cue ball strikes a rack at 2.8 m/s" \
  --case-id prompt_billiards_draft \
  --backend fallback
```

这里的 `fallback` 只验证 prompt→CaseSpec→artifact 契约；要取得物理真值与 UE 图像，必须使用已配置的 `ue` 后端。

产物位于：

```text
$SIM_HARNESS_WORKSPACE/
  cases/rigid_collision/billiards/v001_approach_angle_matrix_rgb_reference/
  cases/rigid_collision/billiards/v002_complete_angle_matrix/
    case_status.json
    inputs/case_specs/{angle_m12,angle_m6,angle_0,angle_p6,angle_p12}.json
    attempt_11_full_5s_zero/sixteen_ball_reference_break_ue/
    matrix/angle_{m12,m6,p6,p12}/<run>/
  cases/rigid_collision/billiards/v003_speed_matrix/
  cases/rigid_collision/billiards/v004_mass_restitution_friction/
  cases/rigid_collision/billiards/v005_angle_extension/
  review/inbox/
```

## 产物契约

关键产物如下；帧序列目录会按配置保留或清理。

```text
<run>/
  case_spec.json
  asset_resolution.json
  scene_layout.json
  runtime_actor_placement.json
  map_report.json
  camera_plan.json
  solver_trajectory.json
  trajectory.json
  contact_events.json
  camera_trajectory.json
  sensor_state.json
  render_sync_report.json
  verifier_report.json
  run_readiness.json
  quality_report.json
  logs/native_rgb/cpp_physics_capture.json
  logs/native_data/segmentation_raw/<camera_id>/frame_*.exr
  passes/rgb/video.mp4
  passes/data/{depth.exr,segmentation.exr,instance.json}
  views/<camera_id>/{rgb.mp4,depth.exr,segmentation.exr,depth_preview.mp4,segmentation_preview.mp4,meta.json}
```

文件后缀与实际格式必须一致：分割图的 canonical 名称是 `segmentation.exr`，不再用伪装成 PNG 的 EXR。

### 正式 complete case 交付不变量

`scripts/harness_iterate_case.py` 是正式 complete-case 入口，当前只接受 `--backend ue`、`--mode both`，并要求 RGB、depth、segmentation 三个 pass与全部五个正式机位。其他 backend 继续通过 `harness_run_case.py` 做 probe、显式 sweep 或后端实验，不能冒充正式交付。

使用 `--case-route <physics>/<scenario>/<version>` 时，版本、运行会话和尝试不会互相覆盖：

```text
cases/<physics>/<scenario>/<version>/
  run_index.json
  latest_iteration.json
  case_status.json
  runs/<unique-session-id>/
    iteration_report.json
    attempt_01/<run>/
    attempt_02/<run>/
```

`session-id` 由时间、纳秒片段、进程号和随机 token 组成；attempt 只在所属 session 内编号。一个 session 可以尝试多种灯光，但只有 hard-gate 通过且技术分最高的一个 attempt 登记为 source run。`run_index.json` 的每次读改写都持有版本级 OS 文件锁；发布器拒绝重复的 resolved `run_dir`，不会用另一标签把同一 run 伪装成重复实验。

台球 v1/v2/v3 source 目录早于这套 session/index 结构，因此没有反向伪造 `run_index.json`。多米诺 v001 已完成三次独立 session 的 exact-input 端到端验收并由用户 keep。后续不强制每组三次完全相同：若输入不同，每个精确输入必须用唯一 `--condition` 标注，并按因果条件矩阵交付。

每个正式候选必须满足：

1. 至少三个 selected source run；同输入自动归为 `exact_repeat`，不同输入必须使用 `--condition`，并且一个 condition 只能对应一个精确输入指纹。注册前即检查兼容性，漏填或冲突 condition 不会写入该版本的 active comparison group。
2. 所有 run 使用相同五机位，并共享相同相机 pose、分辨率、FPS、时长、时基与逐机位 RGB 帧数；motion mode、moving 状态和帧数必须与 `views/<camera>/meta.json` 一致。每个 `run × view` 都交付 RGB/depth/segmentation。
3. 每个 `variants/<label>/overall/` 恰好生成 RGB/depth/segmentation 三个总体预览；bundle 根 `overall/` 再生成同样三个。
4. RGB 来源是 native UE MP4；depth/segmentation 的 canonical source truth 是逐帧 EXR。manifest 为每个 EXR 序列记录帧数和聚合 SHA-256，MP4 只承担 review 预览。

因此，v2 delivery 中 `R` 个 run、`V` 个机位的正式 bundle 包含 `R × V × 3 + R × 3 + 3` 个视频；标准三 run、五机位是 `45 + 9 + 3 = 57`。台球与多米诺旧 keep 产物继续按各自历史公式冻结。

`review_role` 和 `publication_tier` 是两个独立维度：

- `review_role=review_candidate` 表示 run 已通过技术硬门，可以进入 `review/inbox` 等待人工判断；`diagnostic_probe` 表示只进入 `review/probes`，不能 keep 成正式候选。
- `publication_tier` 表示资产许可与 provenance readiness，取 `reference`、`local_preview`、`unverified` 或 `rejected`。例如当前 v3 是合法的 `review_candidate`，但因本地资产许可尚未核实，tier 仍是 `local_preview`。

## 校验、重试和人工保留

完整测试和单 run 技术质量门：

```bash
python3.13 -m unittest discover -s tests -p 'test_*.py'
python3.13 scripts/harness_evaluate_run.py <actual-run-directory>
```

质量门核对真实 map、资产等级、MP4/EXR 格式、帧数与 fps、RGB/depth/segmentation 完整性、metric depth 几何、实例 palette、轨迹有限且单调、接触因果、strict camera ID 和跨通道同步。backend policy 会为所有 UE contact-causality run 默认启用 `F_RIGID_SOLVER_PROVENANCE`：它绑定 native summary 实际使用的 solver runtime，拒绝输入轨迹、非 Game World、未完成的 C++ capture、动态 actor 缺失及 raw/canonical frame/time/state/contact 不一致；结果同时决定 `run_readiness.physics_ready`。技术排序仅在硬门通过后参与 best-of-N。

对于 `expected_spread=full_rack_break` 的 16 球 case，质量门额外要求全部 15 个被动球都有正接触且位移至少 1 cm。深度与分割序列必须逐视角给出完整帧数证据并与解码后的 RGB 帧数一致；canonical 分割必须非空，且所有抽样像素闭包在声明 palette 的 ±1 RGB8 容差内。黑背景是允许的 palette 值，但画面被已声明实例完全覆盖时不强制出现。

自动尝试多个灯光候选并选择最佳：

```bash
python3.13 scripts/harness_iterate_case.py \
  cases/billiards/sixteen_ball_reference_break.json \
  --backend ue \
  --case-route rigid_collision/billiards/v004_five_view_matrix \
  --condition baseline_speed_2p8 \
  --views front_static,side_static,top_down,tracking_subject,event_closeup \
  --render-passes rgb,depth,segmentation \
  --mode both \
  --max-attempts 3 \
  --lighting-presets data_neutral,map_lights_balanced_fill,cinematic_subject_key_fill
```

选择策略：一次 session 内先淘汰硬门失败 attempt，通过者按技术分选一个 source run；若全部失败，只保留诊断证据。正式发布会把该版本 `run_index.json` 中仍可用的 selected source runs 纳入比较；不足三个时保持 `comparison_pending`。不同输入必须分别运行并传入唯一 `--condition`。发布采用同目录 staging→rename；manifest 记录媒体 SHA-256、source run、质量报告、EXR provenance、comparison mode 和 `case_status` 双向链接。最终仍需人工查看视频。

`publish_complete_case_delivery` 只接受 hard-gate 通过的 reference 候选；`publish_diagnostic_case_delivery` 用于完整保留失败实验的多机位/多模态证据，必须显式给出非空 `known_limitations`，固定写出 `reference_ready=false` 的 `delivery_manifest.json`。两者都生成每 Run 三个五机位 Overall，以及 Case 根三个仅用 `event_closeup` 的条件对比 Overall；diagnostic 不是绕过 physics gate 的发布通道。

```bash
# 查看 inbox/kept/rejected 数量
python3.13 scripts/harness_workspace.py status

# 用户认可后保留；candidate 必须是 inbox 的直接子项名称
python3.13 scripts/harness_workspace.py review keep <candidate-name>

# 用户否决后移入 rejected
python3.13 scripts/harness_workspace.py review reject <candidate-name>

# 先 dry-run，再显式删除 7 天前的 rejected
python3.13 scripts/harness_workspace.py prune --older-than-days 7
python3.13 scripts/harness_workspace.py prune --older-than-days 7 --apply
```

`keep` 会在移动前拒绝 bundle 内任何 symlink，核对唯一 manifest、完整视频集合和视频 SHA-256，并复算源质量报告及 depth/segmentation EXR 的帧数与聚合 SHA-256；随后把候选移入 `review/kept`，同步更新 manifest 与绑定的 `case_status.json`。`reject` 同样拒绝 symlink 并严格检查 candidate、case route、case status、inbox 和 manifest 的双向绑定，然后移入 `review/rejected`。两种操作由全局 review lock 串行化，并在移动前写入带原始 status/manifest 快照的 durable transaction journal；写入失败会立即回滚，进程中断则在下一次 review 操作时恢复到 inbox。两种操作都不删除 canonical source run。

`kept` 不自动删除。`prune` 只在显式 `--apply` 后删除到期的 rejected review 项，且不猜测删除关联 run。代码、case、测试和文档只有在技术门通过且用户明确 `keep` 后，才整理成 Git commit/PR；本轮不自动 push。

当前 `review keep/reject/prune` 只管理评审视频或显式候选目录；否决 canonical run 的清理仍需在质量报告与失败原因保留后单独执行。

## 物理与真实性评测边界

当前技术门回答“产物是否真实、同步、可复现、符合已声明的不变量”，不等于“画面已达到真实世界观感”。建议的分层校验是：

1. 状态/事件硬门：初始条件、能量/动量趋势、接触顺序、参数敏感性。
2. 渲染硬门：map、相机、RGB/depth/segmentation、frame id、格式与同步。
3. 同类候选技术排序：只用于 best-of-N。
4. 人工视觉 rubric：构图、尺度、材质、曝光、运动可信度、穿模/抖动。
5. 有参考视频时再加 PhysInOne PMF 等频谱指标；无参考时不能把它当绝对物理分数。

`quality_report.json.ranking.technical_score` 是交付技术分，主要反映契约、媒体规格、视角、map 和资产完整性；它不是物理真实性分数。两个完整视频的正式比较采用以下顺序：

1. CaseSpec/初态/相机/时间轴不可比时输出 `not_comparable`；不同速度、质量、摩擦等参数属于因果 sweep，不选“最好”。
2. hard-gate pass 胜过 fail；两者都 fail 时不产生正式 winner。
3. 同初态 run 先比较事件/接触顺序、世界坐标轨迹、穿透、动量/能量/恢复与 repeat band。
4. 物理相当后比较 depth/segmentation 几何与时序一致性，再比较 LPIPS/SSIM、闪烁、材质、构图和盲评。
5. 只有两个 RGB MP4、没有 reference/state/depth/segmentation 时，只能判断观感，物理结论必须是 `insufficient_evidence`。

FVD 是数据集级分布指标，不能拿两个单独 MP4 计算 winner；PMF 需要物理参考视频，且只能补充状态/事件指标。详细指标、论文来源和拟议的 `A/B/tie/not_comparable` 协议见本地周报 `agent-docs/check_report/本周仿真Harness进展周报_2026-07-14-01-23.md`。

研究与复用决策保存在本地 `agent-docs/knowledge/simulator-harness-research-20260711_2026-07-11-16-30.md`，该文件当前不随 GitHub 分发。

## 可选：安装 Go 1.25.2

当前 harness 不需要 Go。只有继续构建本地 `mcp-unreal` 时才需要精确的 Go 1.25.2；本机当前缺失。Apple Silicon 安装步骤：

```bash
cd ~/Downloads
curl -fLO https://go.dev/dl/go1.25.2.darwin-arm64.pkg
printf '%s  %s\n' a93e05e80c88d3ca909ca9b8f12180a6e4a3de420c8ff3c77f091dd166ff026a go1.25.2.darwin-arm64.pkg | shasum -a 256 -c -
sudo installer -pkg go1.25.2.darwin-arm64.pkg -target /
```

关闭并重开终端，然后校验：

```bash
command -v go
go version
go env GOOS GOARCH GOROOT
```

预期分别包含 `go1.25.2`、`darwin`、`arm64` 和 `/usr/local/go`。若 `command -v go` 为空，先在当前 shell 执行：

```bash
export PATH="/usr/local/go/bin:$PATH"
```

确认有效后，把同一行加入 `~/.zprofile`，再重开终端。Go 版本校验通过前不要构建 `mcp-unreal`；它不阻塞当前 UE Chaos 或 MuJoCo harness。
