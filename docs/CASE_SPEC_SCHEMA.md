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
| `constraint_momentum_transfer` | `chain_objects`, `active_object_id`, `receiver_object_id`, `expected_contact_chain`, `expected_min_receiver_speed_m_s` |
| `elastic_energy_launch` | `launcher_object_id`, `launched_object_id`, `spring_constant_n_m`, `compression_m`, `payload_mass_kg`, `expected_max_energy_ratio` |
| `elastic_constraint_rebound` | `anchor_object_id`, `constrained_object_id`, `rest_length_m`, `max_extension_m`, `constraint_stiffness_n_m`, `expected_min_rebound_speed_m_s` |
| `brittle_impact_fracture` | `impactor_object_id`, `brittle_object_id`, `fracture_threshold_j`, `impact_energy_j`, `expected_min_fragment_count`, `expected_contact_pair` |

物理参数必须结构化放在 `expected_physics` 或 object 字段里；不要只写在 prompt 文本中。
