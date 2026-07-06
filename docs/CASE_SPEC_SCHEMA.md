# Case Spec Schema

当前 schema version：

```text
harness_case_spec_v1
```

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

物理参数必须结构化放在 `expected_physics` 或 object 字段里；不要只写在 prompt 文本中。
