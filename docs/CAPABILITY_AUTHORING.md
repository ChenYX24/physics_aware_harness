# Capability Authoring

新增能力时，先写 capability JSON，再写最小正负 case，再写 verifier 或 adapter。

## 文件

```text
capabilities/<capability_id>.json
cases/<family>/<case>.json
harness/verification/<family>_verifier.py
tests/test_<family>_verifier.py
```

## 必填内容

Capability 必须包含：

- `capability_type`: `pipeline_stage`、`physics_constraint`、`verification`、`asset_operation`、`runtime_bridge`、`dataset_packaging` 或 `compatibility_alias`
- `stage_ids`: 该能力在哪些 pipeline 阶段被调用
- physical assumptions
- required signals
- required assets
- verifier rules
- failure taxonomy
- repair suggestions
- smoke cases
- regression cases

## 原则

- 不要把 capability 写成某个 prompt 模板。
- 不要把 capability 写成某个具体物体模板，例如“台球编译器”。应抽象成 active/passive contact causality、gravity collision、friction response、restitution envelope、force-field drift 等不变量。
- case family 只能放在 `smoke_cases`、`regression_cases` 和 `cases/templates/` 中。
- 要写清楚物理因果规则。
- 要写清楚这个能力属于 pipeline stage、物理约束、校验、资产操作、runtime bridge 还是 dataset packaging。
- 资产能力要区分 retrieval 和 invocation：`asset_intent_resolution` 找候选，`asset_runtime_binding_invocation` 负责把 selected asset/proxy 绑定到 runtime actor。
- 约束能力要区分 visual helper 和 physics graph：绳子/链条/杆件可以是 visual-only，但 distance/joint/hinge constraint 必须进入 `expected_physics` 和 `constraint_trace`。
- negative case 必须能稳定 fail。
- fallback backend 可以先用 deterministic toy trajectory，但必须显式标记 source。

## 推荐能力粒度

| 不推荐 | 推荐 |
|---|---|
| `billiard_causality_compiler` 作为主能力 | `rigid_body_contact_causality`，台球/保龄球/箱体撞击作为 case family |
| `pretty_video_generator` | `canonical_signal_capture` + `physics_verifier_truth_gate` |
| `find_assets` | `asset_intent_resolution` + `asset_runtime_binding_invocation` |
| `friction_demo_template` | `physics_property_constraint_validation` + `rolling_friction_ball` / `sliding_crate_friction` |
| `scene_prompt_rewrite` | `prompt_case_capability_planning` + `scene_spec_compilation` |
| `pendulum_template` 作为主能力 | `constraint_distance_pendulum_motion`，单摆/绳索/铰链作为 case family |

## 验证

```bash
python3 -m json.tool capabilities/<capability_id>.json >/dev/null
python3.13 scripts/harness_smoke.py --backend fallback
python3.13 -m unittest discover -s tests -p 'test*.py'
```
