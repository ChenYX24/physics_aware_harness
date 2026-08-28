# Physics-Aware Harness 简明使用说明

本项目把自然语言（可选参考图片）编译为 CaseSpec，通过资产检索或 Provider 生成资产，
再交给 UE 等 backend 执行物理模拟和视频渲染。

## 1. 运行前提

在仓库根目录执行命令，并使用 Python 3.13：

```bash
cd /path/to/physics_aware_harness
python3.13 --version
```

如果使用 conda、venv 或其他环境管理器，请先激活包含项目依赖的环境；下文的
`python3.13` 可替换为该环境中的 `python`。

本地 UE 运行需要事先配置 `SIM_HARNESS_WORKSPACE`、Catalog、UE executable/project/map、
runner 和 Asset Importer 等环境变量。生成资产和运行产物只写入外部 workspace，不写入源码仓库。

## 2. 最简自然语言运行

```bash
python3.13 scripts/harness_run_case.py \
  --prompt "For local preview, procedurally generate a 0.3 meter rigid sphere, drop it onto a static floor, and render the collision." \
  --case-spec-version v2 \
  --case-id sphere_drop \
  --backend ue
```

省略渲染参数时：

- 视角和 render passes 来自 LLM 生成的 CaseSpec，再由 Observation Planner 合并验证器证据需求。
- LLM 描述要观察什么；具体相机位置由确定性 Camera Planner 计算。
- 分辨率不由 LLM 决定，使用 UE runner 环境配置或默认值（通常为 1920×1080）。
- 默认输出到外部 workspace 的 `runs/harness_cases` 和 `review/probes`。

## 3. 显式指定分辨率和多视角

```bash
python3.13 scripts/harness_run_case.py \
  --prompt "Generate a rigid box above a static floor and render its fall and impact." \
  --case-spec-version v2 \
  --case-id box_drop_multiview \
  --backend ue \
  --width 1280 --height 720 \
  --views front_static,side_static,event_closeup \
  --render-passes rgb \
  --output-root runs/provider_examples \
  --video-root review/provider_examples
```

通常每个请求视角产生一个视频，系统还会发布一个 `overall` 汇总视频。常用视角包括
`front_static`、`side_static`、`top_down`、`tracking_subject` 和 `event_closeup`。

## 4. 本地确定性几何生成

当前本地 procedural Provider 支持以下规范 primitive：

- `box_mesh_v1`：箱体、板、墙和规则平面实体。
- `sphere_mesh_v1`：球体。
- `cylinder_mesh_v1`：圆柱、杆和圆盘。

示例：让一根倾斜圆柱杆撞击水平地面并倾倒：

```bash
python3.13 scripts/harness_run_case.py \
  --prompt "For local preview, procedurally generate a dynamic rigid cylindrical rod exactly 0.12 meters in diameter and 1.20 meters long. Keep the static floor horizontal with its top at z=0. Rotate the rod itself 20 degrees about the world Y axis from vertical, place its lowest rim just above the floor, enable gravity and collision, and render the impact and toppling." \
  --case-spec-version v2 \
  --case-id rod_topple \
  --backend ue \
  --width 1280 --height 720 \
  --views front_static,side_static,event_closeup \
  --render-passes rgb \
  --output-root runs/provider_examples \
  --video-root review/provider_examples
```

Provider 成功后只返回 Catalog asset ID 和 receipt。资产经过 hash、导入、Catalog 注册和资格门，
再由单次 Asset Resolve 选中并绑定场景。

## 5. 使用参考图片

```bash
python3.13 scripts/harness_run_case.py \
  --prompt "Use the reference image to describe the requested object and create a physics-ready scene." \
  --image /path/to/reference.png \
  --allow-image-upload \
  --case-spec-version v2 \
  --case-id image_reference_case \
  --backend ue
```

`--allow-image-upload` 是上传图片给规划 LLM 的显式授权。当前图片可用于 CaseSpec 分析和资产意图；
本地确定性 Provider 尚不根据图片重建复杂模型或材质。

## 6. 使用已保存的 CaseSpec 重跑

重跑保存的 CaseSpec 可以排除 LLM 输出波动：

```bash
python3.13 scripts/harness_run_case.py \
  --case /path/to/case_spec_v2.json \
  --backend ue \
  --width 1280 --height 720 \
  --views front_static,side_static,event_closeup \
  --render-passes rgb \
  --output-root runs/replay \
  --video-root review/replay
```

## 7. 主要产物

每次运行目录通常包含：

- `case_spec_v2.json`、`runtime_case.json`（同时写入执行入口 `case_spec.json`）
- `asset_provider_batch.json`、`provider_receipts/`
- `asset_resolution.json`（V2 compilation 的调用数必须为 1）
- `scene_layout.json`、`runtime_actor_placement.json`
- `observation_plan.json`、`camera_plan.json`
- `trajectory.json`、`contact_events.json`
- `ue_backend_report.json`、`run_control.html`
- `views/<camera_id>/rgb.mp4` 和 `overall/rgb.mp4`

CLI 最终 JSON 中 `status: "completed"` 表示运行完成；`failed_unavailable` 或
`failed_compilation` 会同时给出结构化 `failure_type` 和 `reason`。

## 8. 当前边界

- `external_site` 和 `model_generation` Provider 尚未实现，会结构化阻断，不会静默 fallback。
- 本地生成目前只覆盖规则 box、sphere 和 cylinder；复杂、非规则或关节对象需要后续 Provider。
- 自动测试不依赖真实网络、生产 Catalog、OpenCLIP 权重下载或真实 UE。
- 本地预览资产不等于已获准再分发；reference 资格仍要求可信许可和 redistribution 证据。
