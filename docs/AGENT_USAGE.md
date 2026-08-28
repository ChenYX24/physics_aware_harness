# Agent Usage

Harness 的主接口是 CaseSpec 与 CLI。Agent 应生成声明式对象/状态/约束/断言，不应选择预制物理过程。

## 常用命令

列出 active capabilities：

```bash
python scripts/harness_list_capabilities.py --json
```

运行一个已保存 case：

```bash
python scripts/harness_run_case.py \
  --case cases/falling/falling_block_on_floor.json \
  --backend fallback \
  --output-root /tmp/harness_runs
```

从一个完整 case 生成可复现副本（不会按现象修改参数）：

```bash
python scripts/harness_generate_cases.py \
  --case cases/falling/falling_block_on_floor.json \
  --count 4 --seed 42 --out /tmp/generated_cases
```

运行目录：

```bash
python scripts/harness_run_case_batch.py /tmp/generated_cases --backend fallback --output-root /tmp/batch
```

真实 UE 运行将 `--backend` 改为 `ue`，并配置 `.uproject`、UE executable、Map、Catalog/registry、contact export 和 runner command。未配置时必须 fail closed。

## 图片与 Provider

图片输入通过 `--image` 登记。规划 LLM 上传与 Meshy 上传分别需要 `--allow-image-upload` 和 `--allow-meshy-upload`；二者不互相授权。Provider 产物必须经过 Catalog 注册、qualification 与 Asset Resolve，不能直接注入 runtime。

## 运行后读取

- `backend_selection.json`
- `runtime_actor_placement.json`
- `verification_plan.json`
- `trajectory.json` 或 domain cache
- `contact_events.json`
- `run_readiness.json`
- `harness_verifier.json`
- `render_sync_report.json`
- `artifact_manifest.json`

视频存在不等于通过。Fallback/fake runner 不是真实物理或真实 UE 证据。

## 强制规则

- 不按 case id、目录或 prompt 词选择 solver/verifier。
- 不新增“坠落模式”“泼洒模式”“连续碰撞 verifier”等代码。
- 缺少 primitive/coupling 时报告 capability missing，或扩展通用契约。
- `harness_case_tree.py` 只生成导航，不是 runtime router。
