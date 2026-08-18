# Asset 来源约束与连续碰撞链修复交接

日期：2026-08-08

## 目标

以最小、通用的 Harness 契约修复以下两个问题：

1. 用户明确指定一组对象必须来自一个或多个 Provider 时，该要求不能在 Expansion → CaseSpec V2 → Provider → Asset Resolve 之间丢失，也不能在 Provider 失败后静默变成 analytic proxy。
2. `sequential_contact_propagation` 已声明有序碰撞链时，后续目标应按解析后的实际 bounds 紧密、同轴排列，使链条在既定速度和摩擦下可以真实传播；不能仅把 relation 当作事后验证愿望。

本任务优先加强 Harness，不要求更换规划 LLM。

## 失败样例与证据

运行目录：

```text
/Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/runs/
polyhaven_table_chain_uniform_fit_smooth_2/
polyhaven_table_chain_uniform_fit_smooth_2_ue
```

用户要求球和四个动态目标自主来自 Poly Haven，并声明五个物体的连续碰撞链。结构化产物显示：

- `case_spec_v2.json` 把全部 external-site acquisition 写成 `requirement=preferred`、`origin=llm_inferred`，同时全局 `allow_analytic_proxy=true`。
- 球因一次可重试 SSL EOF 下载失败而降级；`obstacle_2` 至 `obstacle_4` 因检索意图自相矛盾而降级。只有 `obstacle_1` 成功绑定 `Cardboard Box 01`。
- `runtime_actor_placement.json` 的 `proxy_actor_count=4`。后三个目标实际是 `binding_source=analytic_proxy`、`source_kind=analytic_proxy` 的立方体。
- 矛盾示例：对象 taxonomy 要求 `barrel`，但 `must_not.category` 又包含 `container`；Provider 的通用 container 子类展开会正确地把 barrel 排除。crate/bucket、jar/pot 请求存在同类矛盾。
- Runtime Compiler 仍报告 pass 并启动渲染，因为这些 route 被标成 preferred，而不是用户明确的 required。

碰撞证据：

- CaseSpec 中动态物体 X 中心依次为 `0.5, 1.5, 2.5, 3.5, 4.5 m`。
- 使用解析后的 bounds，球到目标一表面约有 `0.62 m` 间隙；目标一到目标二约有 `0.52 m` 间隙。
- `trajectory.json`：球最大速度约 `1.99 m/s`；目标一最大速度约 `0.811 m/s`，最终只移动约 `0.330 m`；目标二至目标四移动量均为 0。
- `contact_events.json` 中唯一动态物体间原生碰撞是 `sphere_impactor → obstacle_1`。
- `verifier_report.json` 正确以 `F2_missing_contact_events` 失败，首个断点为 `obstacle_1 → obstacle_2`。

诊断不依赖视频内容。

## 必做修复一：结构化、多 Provider 的来源约束

不要在用户原文中精确匹配 `poly haven`、`meshy` 等字符串，也不要为某个网站写 case-specific 规则。

在 Expansion contract 中增加结构化数组，建议最小形态如下；字段名可根据现有 contract 风格微调，但必须表达相同语义：

```json
{
  "asset_source_constraints": [
    {
      "scope": {
        "object_ids": ["sphere_impactor", "obstacle_1", "obstacle_2", "obstacle_3", "obstacle_4"]
      },
      "allowed_routes": ["external_site", "model_generation"],
      "allowed_providers": ["poly_haven", "meshy"],
      "requirement": "required",
      "fallback_order": ["meshy"],
      "allow_proxy": false
    }
  ]
}
```

要求：

- 约束必须是数组；不同对象组可以有不同 Provider 集合和 fallback 顺序。
- `allowed_providers` 必须允许多个 Provider，不能退化成单个全局 provider 字段。
- `scope.object_ids` 引用 Expansion 的稳定建议对象 ID；禁止通过后续对象描述的模糊文本重新匹配。
- Expansion 仍负责理解用户自然语言；Harness 负责在 CaseSpec 生成后逐项核对，不依赖 CaseSpec LLM 自觉继承。
- 当约束为 required 且 `allow_proxy=false` 时，作用域内对象的 acquisition 必须满足：允许的 route/provider、`requirement=required`、`origin=user_explicit`、没有未授权 fallback。现有全局 `asset_policy.allow_analytic_proxy` 可以继续服务其他对象，但不能覆盖该对象的 required Provider route。
- 若 Expansion 没有识别出用户要求，不能用字符串特判修补；应把缺失作为规划质量/eval 问题记录。若 Expansion 已识别而 CaseSpec 丢失，必须在既有 bounded repair 中给出结构化路径错误。
- 原始用户消息仍继续提供给 CaseSpec 调用；该数组是可审计的硬约束投影，不是替代原文的摘要。

建议修改位置：

- `harness/planning/case_generation.py`：Expansion 字段、contract、规范化、CaseSpec/repair 输入与生成后一致性校验。
- `harness/core/case_spec_v2.py`：只增加确实需要的跨字段门；不要新增与现有 acquisition contract 重复的 CaseSpec 字段。
- 相关生成测试：`tests/test_case_generation_v2.py`、`tests/test_case_spec_v2.py`。

## 必做修复二：拒绝自相矛盾的资产意图

Provider 检索前检查正向身份与硬排除是否冲突。例如 taxonomy/object type 展开为 barrel，而 `must_not=container` 展开后也包含 barrel 时，返回明确的：

```text
contradictory_asset_constraints
```

要求：

- 使用 Provider 已有的通用语义 token/alias 规则，不写特定资产 ID 或本用例对象名。
- 泛类到具体类可以单向展开；具体类排除不能误排除全部同级类。当前 `remote.py` 已开始采用这一方向，接手者应在现有 dirty worktree 上继续，不要回退。
- 错误需包含冲突的正向 token、排除 token 和对象/request ID，便于 bounded repair 或用户定位。
- 不允许在矛盾条件下随意选择一个“看起来接近”的资产。
- CaseSpec planning prompt 应补一句：`must_not` 不得排除请求类别自身的父类；不要靠继续堆大量示例解决。

建议修改位置：

- `harness/assets/providers/remote.py`
- 必要时在 AssetIntent 编译后、Provider 调用前增加 provider-neutral 的精确冲突检查；不要建立新的完整 ontology。
- `tests/test_remote_asset_providers.py`

## 必做修复三：required Provider 失败必须阻断渲染

当来源约束要求 external/model Provider 且禁止 proxy 时：

- 网络下载失败、无相关候选、导入失败或资格门失败必须使 compilation/preflight 失败。
- 不得生成 analytic proxy actor，不得进入真实 UE 渲染，也不得把 proxy 视频标成该请求的可用结果。
- 可重试网络错误可以明确标记 `retriable=true`，但本任务不实现复杂下载重试框架。
- 验证 `asset_resolution.json`、`runtime_actor_placement.json`、最终 CLI status 三处状态一致。

现有 required route 本身已经接近 fail-closed；重点是保证 Expansion 的用户约束不会被 CaseSpec 写成 preferred。

## 必做修复四：连续碰撞链按真实 bounds 收紧

当前 `harness/planning/static_scene_builder.py` 的 `align_v2_ordered_dynamic_chain()` 已能修正反向或横向排列，但对“方向正确、间隙过大”的链保持原位，因此本例仍失败。

采用最小确定性规则，不实现通用动力学求解器：

- 只作用于 V2 `sequential_contact_propagation` 的简单、连续、有向动态链。
- 第一条边确定主要水平传播轴和正负方向；第一驱动物体到第一个目标的 authored launch gap 可以保留。
- 从第二条边开始，使用 Asset Resolve 后的 `effective_size_m` / 保守 collider extents。如果后继反向、主要沿横轴，或表面间隙大于链条小间距，则把后继放在前驱之后，表面 clearance 使用现有约 `0.005 m`。
- 同轴修正只改变水平位置；支撑关系仅验证 CaseSpec 已声明的 Z，不改写变换。
- 分支、环或不能确定方向的图不要猜测；保持 fail-closed/验证失败。
- 修正后重新运行 overlap、support 和 static scene checks。

本任务不要求一般化的质量—恢复系数—摩擦—速度可达性求解器。对明确的 sequential chain 使用紧密排列即可，避免过度设计。

建议修改位置：

- `harness/planning/static_scene_builder.py`
- `tests/test_harness_static_scene.py`
- `harness/verification/ordered_contact_verifier.py` 无需放宽；它已经正确报告首个断边。

## 验收标准

1. Expansion contract 能表达多个约束、每个约束多个 Provider，以及不同对象组的不同来源策略。
2. 不存在对原始 prompt 的 `poly haven` 等精确字符串匹配。
3. Expansion 已声明 required/no-proxy 后，CaseSpec 若输出 preferred、错误 provider 或允许 proxy，会触发现有 bounded repair；repair 后仍不满足则规划失败。
4. `taxonomy=barrel + must_not=container` 等冲突在下载前以 `contradictory_asset_constraints` 失败；合法的 `taxonomy=crate + must_not=[barrel, box]` 仍能选择 crate。
5. required external asset 的下载/检索/导入失败不会产生 analytic proxy，不会调用正式 UE renderer。
6. 正确方向但中心间隔 1 m 的四目标顺序链，使用解析 bounds 编译后，第二条及后续边的表面 gap 约为 `0.005 m`，且无初始穿模。
7. 真实 UE 复跑时，五个动态物体全部具有非 proxy 的外部资产 binding；四条动态碰撞边均有原生 contact event，ordered verifier 通过。
8. 保持 V1 行为、非 sequential capability、分支图与已有 Provider/Catalog/单次 Resolve 契约不变。

建议测试命令（conda base）：

```bash
cd /Users/laplace/phyawareharness/physics_aware_harness
conda run -n base python -m unittest -q \
  tests.test_case_generation_v2 \
  tests.test_case_spec_v2 \
  tests.test_remote_asset_providers \
  tests.test_harness_static_scene \
  tests.test_harness_domino_verifier \
  tests.test_runtime_compiler_v2 \
  tests.test_harness_cli
conda run -n base python -m compileall -q harness scripts
git diff --check
```

## 非目标

- 不更换 LLM 或增加第二套规划器。
- 不做 prompt/provider 名称硬编码、特定 CaseSpec ID、固定坐标或固定 Poly Haven asset ID 补丁。
- 不实现完整 ontology、全局多 Provider 分配优化器、通用物理可达性求解器或复杂网络重试系统。
- 不放宽 verifier 来接受缺失碰撞，也不把 synthetic bounds contact 当作原生碰撞。
- 不用 analytic/fake 资产掩盖 required Provider 失败。

## 接手注意事项

- 正式源码位于 `/Users/laplace/phyawareharness/physics_aware_harness`，Python 使用 conda `base`；UE、Catalog、Provider cache、资产和 run 均位于外接盘。
- 内层源码当前是 dirty worktree，包含本轮已完成但尚未提交的通用修复和用户已有改动。不要 reset、checkout 或覆盖无关变更；先阅读 `git diff` 的相关文件。
- 已存在并应保留的修复包括：Poly Haven identity/exclusion gate、具体类别与 generic container 的单向 alias 展开、provider cache version、外部 FBX bounds 容差、V2 relation `impact` 规范化、支撑/真实 bounds 布局、横向/反向链条对齐、ordered contact verifier 和验证失败 CLI 状态。
- 最近一次相关聚焦回归为 132 项通过；系统 `/usr/bin/python3` 为 3.9，部分测试使用的新 `pathlib` 参数仅在 conda base Python 3.13 正常，因此以 conda base 为准。

## 2026-08-10 Meshy 后续修复计划

### 已确认的产品决定

- 暂不新增跨 LLM 输出归一化的“付费任务身份”，也不按图片哈希和 API payload 合并不同 Provider request。现有 request digest/checkpoint 身份继续保留。
- Multi-Image API 的 `texture_prompt` 作为可选字段，由规划 LLM 根据用户的材质、颜色或风格意图决定是否填写；它只指导纹理，不得被描述成几何生成 prompt。
- 当用户要求保留照片中的原始纹理/颜色且没有额外风格要求时，LLM 应省略 `texture_prompt`。若填写，必须原样进入 Provider request、Meshy POST payload 和脱敏 receipt，并校验官方 600 字符上限。
- 不根据 prompt 中的 `meshy` 等精确字符串注入 `texture_prompt`，也不为椅子或当前 case 添加专用规则。

### 当前失败状态

- 完整 prompt 每次重新规划时，LLM 会改变对象 ID、description、shape、reference usage 等非 Meshy API 字段，从而得到不同的 Provider request digest。连续运行已创建多个任务，不能继续用“重跑完整 prompt”代替恢复。
- 已知 checkpoint：`f69d2f7c...` 对应任务 `019fe6ba-b8cf-7cc4-9949-2ba4422f0c41`，本地最后进度 99%；`58d9376e...` 对应任务 `019fe6c8-5313-7fd6-8828-68c09f4db925`，本地最后进度 22%。
- `19beaeed...` 在 POST 等待响应时超时，没有 task ID/checkpoint；服务端是否已创建任务未知。该状态不得自动重复 POST。
- Expansion 还会为仅明确 `procedural_generation` route 的对象生成 `allowed_providers=[]`，而当前校验器要求非空 Provider 列表，导致规划在 Meshy 前失败。

### 必做修复

1. 增加基于已保存 Provider request、provider input manifest 和 checkpoint 的直接恢复入口；恢复不得重新调用规划 LLM，也不得重新 POST 已有 task ID，成功后继续下载、导入、Catalog、Resolve 和 UE 主链。
2. 对已有 task ID 的 GET 轮询，以及任务成功后的模型/贴图下载，增加小规模、有上限的超时、SSL EOF、429 和 5xx 重试；保留同一 task ID，不创建替代任务。不得形成复杂通用网络框架。
3. POST 前原子记录 submission-attempt 状态；若 POST 超时且未取得 task ID，将该 request 标为 `provider_submission_state_unknown`。同一 request 后续必须 fail closed，除非用户显式处置，禁止自动再次 POST。
4. 允许来源约束只限制 route 而不限制 Provider：`allowed_providers` 为空/省略表示“未声明 Provider 限制”，不是矛盾；`allowed_routes`、required、no-proxy 和 fallback 仍必须严格贯穿。只有用户明确限定 Provider 时才要求非空 Provider 集合。
5. 为 CaseSpec/Provider contract 增加最小的可选 `texture_prompt` 贯穿，加入长度、类型、receipt 脱敏和 API payload 测试；不得把它用于改变几何、对象身份或物理参数。

### 聚焦验收

- 直接恢复 `f69d2f7c...` 时只出现 GET/下载，不出现 POST，也不调用规划 LLM。
- GET/下载经历可重试 SSL EOF 后仍使用原 task ID；超过上限结构化失败并保留 checkpoint。
- POST 未知状态再次恢复时拒绝重提，不增加 Meshy 消耗。
- route-only procedural 约束可通过 Expansion 校验并由已注册 Provider 正常解析；显式 Provider 约束仍不可被扩大。
- `texture_prompt` 缺省时不出现在 Meshy payload；LLM 明确填写时逐字进入 payload/receipt，超过 600 字符在网络前失败。

### 本阶段非目标

- 不实现跨不同 request digest 的付费任务合并或全局去重。
- 不自动猜测 POST 超时对应的远端任务，也不通过重复 POST 探测。
- 不把自由文本 description、role 或 shape 自动拼接成 `texture_prompt`。
