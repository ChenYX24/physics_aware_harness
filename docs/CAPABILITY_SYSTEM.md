# Capability System

Capability 描述可复用的软件/求解契约，不描述自然语言中的物理现象。

## Physics domains

| Capability | State domain | Backend examples |
|---|---|---|
| `rigid_body_dynamics` | rigid transforms, velocities, contacts, constraints | UE Chaos |
| `fluid_particle_dynamics` | particle state and rigid-particle coupling | Genesis SPH |
| `deformable_body_dynamics` | deformable mesh/tetrahedral state | Genesis FEM, Taichi cloth |

Backend Planner 只使用对象状态表示、显式 solver capability 和 backend constraints。旧名称如 `rigid_body_gravity_collision`、`sequential_contact_propagation`、`projectile_gravity_motion` 仅是 compatibility aliases。

## Pipeline capabilities

- prompt/case planning
- asset intent resolution
- Provider/Catalog registration and qualification
- scene spec compilation
- static scene placement
- runtime actor placement
- backend execution
- canonical signal capture
- render synchronization
- generic physics verification
- dataset packaging

## Generic assertions

- trajectory integrity
- state value/delta
- event existence/count/sequence
- artifact completeness
- particle/deformable cache measurements

Case 可以用这些断言组合表达任意目标行为。Verifier 不读取 case family、目录、prompt 或旧 capability label。

## Compatibility

V1 case 可继续读取，但旧 verifier rules 不会触发隐藏过程逻辑。旧 negative case 只有在显式声明通用 assertions 时才拥有负向语义。

`scripts/harness_case_tree.py` 仅生成导航；`scripts/harness_generate_cases.py` 只接受 `--case`，不接受 `--suite`。
