# Optional Viewer

`apps/demo_frontend/` 是 optional viewer，不是核心 harness。

## 当前定位

前端可用于：

- 浏览 run artifact。
- 查看视频或 preview。
- 展示 asset selection。
- 展示 render pass / signal 状态。

## 本地工具

资产资格与 runtime binding 浏览器：

```bash
python3.13 scripts/harness_asset_browser.py --open
```

默认读取 workspace 的 `catalog/adp/asset_registry.local.json`。要显示真实
runtime binding 证据，显式传入一个或多个 placement 文件：

```bash
python3.13 scripts/harness_asset_browser.py \
  --binding-report /absolute/run/runtime_actor_placement.json \
  --open
```

同一只读 server 也提供 `case_parameter_editor.html`。编辑器导出的批次先准备
持久化生成表，不启动渲染：

```bash
python3.13 scripts/harness_render_parameter_batch.py batch.json --prepare
```

确认 `batch_queue.json` 后再执行；失败项会保留旧 attempt 并新建一次再生成：

```bash
python3.13 scripts/harness_render_parameter_batch.py batch.json --execute
python3.13 scripts/harness_render_parameter_batch.py batch.json --execute --retry-failed
```

队列逐项维护 `file`、`render`、`validation` 与 `regeneration_count`。每次
attempt 使用独立输入和输出目录，避免覆盖上一次失败证据。

每次 `harness_run_case.py` 生成的 `run_control.json` 与自包含
`run_control.html` 属于 core run artifact，不属于 optional viewer。页面冻结本次
CaseSpec 与捕获配置；有 variant plan 时可调整已声明变量，并始终把复现结果写入
原 run 的 `reproductions/`。

但 harness 的核心验收不依赖前端。核心路径是：

```text
CLI/API -> artifact schema -> verifier report -> diagnosis -> tests
```

## 为什么降级

- Code agent 更需要稳定 JSON/CLI/API。
- 前端容易让项目被误解为 prompt-to-video demo。
- 物理 correctness 必须由 verifier gate 决定，不由 UI 或视频观感决定。

## 后续前端如果恢复主力展示，应优先展示

- capability id
- verifier status
- failure type
- first failing object/frame
- trajectory/contact evidence
- repair suggestions
- artifact manifest

不要只展示视频。
