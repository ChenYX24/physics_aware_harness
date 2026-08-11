# Case Spec Schema

当前主流程只接受一个源 schema version：

```text
harness_case_spec_v2
```

文件输入必须是 CaseSpec V2；自然语言/图片输入经 Expansion 后生成一次结构化 CaseSpec V2，
校验失败时最多进行一次约束修复。`--case-spec-version` 仅保留 `v2` 这个显式值。

V2 主字段：

| 字段 | 含义 |
|---|---|
| `identity` | case id、用户原始描述、预期 pass/fail |
| `capabilities` | primary 与 required capability |
| `scene` / `timebase` | 坐标系、边界、时间步与时长 |
| `backend_constraints` | backend 与 solver 能力硬约束 |
| `asset_policy` / `objects` | 资产来源、对象语义、物理与求解器契约 |
| `relations` / `events` | 对象关系和时序事件 |
| `expected_behavior` | 通用可测量物理预期 |
| `observation_requirements` | camera、modality、signal |
| `verification_requirements` | measurement 与 assertion |
| `variant` / `provenance` | 变体和来源信息 |

CaseSpec V2 经 schema 与跨字段校验后，编译为独立的
`harness_runtime_case_v2` 执行契约。原始 V2 保存为 `case_spec_v2.json`；运行时契约保存为
`runtime_case.json`，并写入 runner 使用的 `case_spec.json`。该过程不经过其他 CaseSpec schema、
adapter 或兼容校验器。

当前 Runtime Compiler 每次 compilation 只执行一个 `capabilities.primary`。因此
`capabilities.required` 必须只包含该 primary（允许其兼容 alias）；额外 capability 即使已经
登记，也会以 `additional_required_capability_unsupported` fail closed，直到有明确的多 capability
compiler、verifier 和证据合并契约。`backend_constraints.required_solver_capabilities` 则是 backend
选择的硬条件，所选 backend 必须逐项提供，否则 compilation 在执行前失败。除明确登记的
fluid/soft-body specialized solver 外，当前通用 physics capability 只允许 `fallback` 或 `ue`，
不能通过省略 `required_solver_capabilities` 绕过该能力门。

V2 对象的结构化 `physics.body_type` 和 `physics.collision_required` 是 runtime physics contract
真值；`role` 只表达开放语义，不能决定对象是否参与物理。投影后的 dynamic 对象必须启用模拟，
显式要求 collision 的对象必须启用碰撞，否则 Actor Placement 在 backend 调用前 fail closed。
Static Scene 同样使用该结构化 contract：static/kinematic collision 对象可作为潜在支撑面且自身
不需要支撑关系，dynamic 对象允许从非接触初态开始自由运动。
V1 缺少这些结构化字段时才继续使用既有 role 兼容分类。

资产硬条件中 `asset_type` 表示 backend 资产类（例如 `StaticMesh`），`geometry_type` 表示几何
形状（例如 `box`、`sphere`、`cylinder`），二者独立校验。`procedural_generation` route 对应的
规范 source kind 同样是 `procedural_generation`；编译器只把既有同义输入 `procedural`、
`local_procedural`、`generated_procedural` 规范化到该 token，不放宽其他 source-kind 硬条件。
资产硬条件 `physics_role` 使用 `dynamic_rigid_body`、`static_rigid_body` 或
`kinematic_rigid_body`；编译器会把对应的 `physics.body_type` 短值规范化到这些 token。
确定性 procedural Provider 生成的居中网格以 Catalog `authored_size_m` 作为 Scene bounds 真值，
并在 UE runtime 保留 authored scale；解析代理仍按其受控 collider 尺寸独立缩放。

对象的 `asset.acquisition.route` 可由文字或图片请求明确指定：

```text
default / local_catalog / external_site / procedural_generation / model_generation
```

`requirement` 为 `preferred` 或 `required`。LLM 自行推断的路线只能是 `preferred`；
只有用户明确要求、并记录 `origin: user_explicit` 时才能设为 `required`。图片输入还需用
`reference_inputs[].usage` 区分
`similarity_search`、`generation_condition`、`geometry_reference`、`style_reference` 和
`texture_source`。Provider 尚未接入时，要求外部获取或生成的 V2 会在 compilation 阶段以
`procedural_generation` 当前由统一 Provider Orchestrator 支持 `box_mesh_v1`、
`sphere_mesh_v1` 和 `cylinder_mesh_v1`；它必须先生成、
校验 hash/license/provenance、经显式 UE importer 导入、通过 `AssetRegistry` 注册和资格门，
最后才由单次 Asset Resolve 选择。`external_site` 和 `model_generation` 仍以结构化
`unsupported_provider_route` 阻断，不会静默改用本地相似素材。Provider 失败后只有
`fallback_order` 显式包含 `local_catalog` 时才允许本地检索。

本地程序化 Provider 根据 `geometry.shape_hint` 判断 recipe；`acquisition.provider_hint` 可显式
使用上述三个已登记 ID，也可省略以由 Provider 推断。`box/plate/wall` 使用 box，
`sphere/ball` 使用 sphere，`cylinder/rod/pole/column/disc` 使用 cylinder。
`geometry.approx_size_m` 始终是完整的 x/y/z 包围盒尺寸：sphere 三轴直径必须相等，cylinder
的 x/y 直径必须相等且 z 为长度。不支持的形状或不一致的显式 hint 会结构化失败，不会静默
变成长方体。UE importer 命令通过
`SIM_HARNESS_UE_ASSET_IMPORTER_CMD` 显式配置；未配置时返回
`backend_importer_unavailable`，不会触发普通 UE runner。仓库提供的真实命令入口为
`python3.13 scripts/harness_ue_asset_importer.py`；它启动 UE 内部
`native_ue_asset_importer.py`，执行米→厘米单位转换、StaticMesh bounds、LOD0 section、简单碰撞、
保存后 package 文件与 SHA-256 校验，成功后才允许注册为 runtime-ready。launcher 使用临时、
可删除的厘米规范化 OBJ，因为 UE 5.7 的 OBJ/Interchange 路径不采用 FbxImportUI 的缩放参数；
原始米制 Provider OBJ、hash 和 request identity 保持不变。native 脚本原子写出结果后由 launcher
负责结束 Editor，避免 headless `quit_editor()` 在 LevelEditor 初始化前触发退出崩溃。

下一阶段的本地 Provider 实现边界、数据契约、负向测试和验收命令见
[`LOCAL_PROVIDER_IMPLEMENTATION_PLAN.md`](LOCAL_PROVIDER_IMPLEMENTATION_PLAN.md)。

```json
{
  "description": "按用户文字设计生成一个破碎木板",
  "resource_kind": "geometry_collection",
  "acquisition": {
    "route": "model_generation",
    "requirement": "required",
    "origin": "user_explicit",
    "reference_inputs": [],
    "fallback_order": []
  }
}
```

本地检索 Catalog 与旧 UE runner JSON registry 分开配置：前者使用
`SIM_HARNESS_ASSET_CATALOG`，后者继续使用 `SIM_STUDIO_ASSET_REGISTRY`。

## V2 LLM 配置

两次正常调用依次生成 `expansion.json` 和 CaseSpec V2；schema/跨字段失败时至多追加一次
受限修复。使用 OpenAI-compatible `chat/completions` 接口：

```bash
export SIM_HARNESS_LLM_BASE_URL="https://provider.example/v1"
export SIM_HARNESS_LLM_API_KEY="..."
export SIM_HARNESS_LLM_MODEL="provider-model-id"

python scripts/harness_run_case.py \
  --prompt "一颗球撞击另一颗球" \
  --case-spec-version v2 \
  --backend auto
```

图片需要重复传入 `--image`，并显式增加 `--allow-image-upload`。凭据、原图和模型缓存均不
进入源码仓库。
