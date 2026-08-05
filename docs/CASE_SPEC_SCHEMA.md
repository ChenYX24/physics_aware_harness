# Case Spec Schema

当前支持两个显式 schema version：

```text
harness_case_spec_v1
harness_case_spec_v2
```

V1 继续作为兼容默认。文件输入按 `schema_version` 分派；自然语言输入只有显式传入
`--case-spec-version v2` 才调用 Expansion/CaseSpec 两阶段 LLM planner。

必填字段：

| 字段 | 含义 |
|---|---|
| `case_id` | 稳定 case id |
| `capability_id` | 绑定的 capability |
| `prompt` | 自然语言意图 |
| `expected_physics` | 物理预期、坐标系、碰撞图等 |
| `objects` | 对象列表，必须有稳定 id/role |
| `active_objects` | 可主动受力/初速度对象 |
| `passive_objects` | 必须由物理事件触发的对象 |
| `required_assets` | 资产需求 |
| `required_signals` | 运行必须产出的信号 |
| `verifier_expectation` | 预期 pass/fail 和 failure type |
| `should_pass` | smoke 中的期望 |
| `notes` | 人类说明 |

示例：

```bash
python3 -m json.tool cases/billiards/low_speed_single_contact.json >/dev/null
```

Case spec 是可执行 contract，不是 prompt 模板。

## `expected_physics` 示例字段

不同 capability 会读取不同字段。常见字段：

| Capability | 关键字段 |
|---|---|
| `rigid_body_contact_causality` | `collision_graph`, active/passive object ids, velocity epsilon |
| `rigid_body_gravity_collision` | `gravity_m_s2`, `support`, `coordinate_system` |
| `physics_property_constraint_validation` | mass/friction/restitution/damping/material ranges |
| `agent_rigidbody_action_coupling` | `action_trace`, `action_actor_id`, `target_object_id`, `expected_contact_pair` |
| `constraint_distance_pendulum_motion` | `anchor_object_id`, `constrained_object_id`, `constraint_length_m`, `constraint_tolerance_m`, `expected_max_step_displacement_m` |
| `constraint_momentum_transfer` | `chain_objects`, `active_object_id`, `receiver_object_id`, `expected_contact_chain`, `expected_min_receiver_speed_m_s` |
| `elastic_energy_launch` | `launcher_object_id`, `launched_object_id`, `spring_constant_n_m`, `compression_m`, `payload_mass_kg`, `expected_max_energy_ratio` |
| `elastic_constraint_rebound` | `anchor_object_id`, `constrained_object_id`, `rest_length_m`, `max_extension_m`, `constraint_stiffness_n_m`, `expected_min_rebound_speed_m_s` |
| `brittle_impact_fracture` | `impactor_object_id`, `brittle_object_id`, `fracture_threshold_j`, `impact_energy_j`, `expected_min_fragment_count`, `expected_contact_pair` |

物理参数必须结构化放在 `expected_physics` 或 object 字段里；不要只写在 prompt 文本中。

## CaseSpec V2

V2 是语义 contract，主字段为：

```text
identity / capabilities / scene / timebase / backend_constraints
asset_policy / objects / relations / events / expected_behavior
observation_requirements / verification_requirements / variant / provenance
```

V2 经 schema 与跨字段校验后，通过内存 adapter 投影到现有 V1 runtime contract。原始
V2 保存为 `case_spec_v2.json`；`runtime_case_spec_v1.json` 和 `case_spec.json` 是迁移期
runner 兼容投影。

当前 Runtime Compiler 每次 compilation 只执行一个 `capabilities.primary`。因此
`capabilities.required` 必须只包含该 primary（允许其兼容 alias）；额外 capability 即使已经
登记，也会以 `additional_required_capability_unsupported` fail closed，直到有明确的多 capability
compiler、verifier 和证据合并契约。`backend_constraints.required_solver_capabilities` 则是 backend
选择的硬条件，所选 backend 必须逐项提供，否则 compilation 在执行前失败。除明确登记的
fluid/soft-body specialized solver 外，当前通用 physics capability 只允许 `fallback` 或 `ue`，
不能通过省略 `required_solver_capabilities` 绕过该能力门。

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
