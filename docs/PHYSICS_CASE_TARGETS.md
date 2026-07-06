# Physics Case Targets

本文件把早期 `benchmark_prompts/physics_taxonomy.json` 中的物理 case 表整理为 harness backlog。目标不是一次性全做完，而是逐个把 case 变成：

1. capability contract
2. parameterized case template
3. runtime artifact requirement
4. invariant verifier
5. smoke / regression / benchmark

| 目标 case | 物理现象 | Harness 状态 | 下一步 |
|---|---|---|---|
| `collision_billiard_break` | rigid body collision / causality | 已由 `rigid_body_contact_causality` 覆盖；台球只是 case family；本轮新增 dynamic template | 接真实 UE contact/trajectory |
| `collision_domino_chain` | chain collision | 已支持 capability + verifier；本轮新增 dynamic template | 接真实 UE rotation/contact order |
| `falling_stack_boxes` | gravity/contact | 已支持 capability + verifier；本轮新增 dynamic template | 支持多 block stack 和 penetration metric |
| `ramp_roll_gravity` | inclined plane | 已支持 capability + fallback trajectory + friction-aware verifier；新增 dynamic template | 接真实 UE ramp collider/contact 和 friction material |
| `projectile_gravity_motion` | projectile / upward throw | 已支持 capability + fallback trajectory + apex/descent/contact verifier；新增 dynamic template | 接真实 UE projectile trajectory/contact |
| `pendulum_constraint` | distance / joint constraint motion | 已支持 `constraint_distance_pendulum_motion` capability + fallback trajectory + constraint_trace + distance/continuity verifier；单摆只是 smoke family | 接真实 UE PhysicsConstraint/Chaos joint trace 和 solver drift metric |
| `bounce_restitution_ball` | restitution | 已支持 capability + fallback trajectory + restitution-bounded verifier；新增 dynamic template | 接真实 UE restitution material/contact/export |
| `rolling_friction_ball` | rolling friction | 已支持 capability + fallback trajectory + friction-bounded verifier；新增 dynamic template | 接真实 UE rolling friction material/contact/export |
| `sliding_crate_friction` | sliding friction | 已支持 capability + fallback trajectory + stop-distance/static-threshold verifier；新增 dynamic template | 接真实 UE static/dynamic friction material/contact/export |
| `wind_balloon_drift` | force field wind | 已支持 `force_field_wind_drift` capability + fallback trajectory + wind-vector drift verifier；新增 dynamic template | 接真实 UE force field / wind volume / trajectory export |
| `mass_ratio_collision` | momentum transfer | 已支持 `mass_ratio_momentum_transfer` capability + fallback trajectory + mass-ratio velocity/energy verifier；新增 dynamic template | 接真实 UE mass labels / contact impulse / post-collision velocity export |
| `angular_damping_spin` | rotational damping | 已支持 `angular_damping_spin_decay` capability + fallback angular velocity trace + spin-decay verifier；新增 dynamic template | 接真实 UE angular velocity / angular damping / inertia export |
| `agent_push_box` / `agent_throw_ball` | agent-to-rigidbody | 已支持 `agent_rigidbody_action_coupling` capability + fallback action trace + causality verifier；新增 dynamic template | 接真实 UE agent action trace / skeletal controller / impulse export |
| `newton_cradle` | constrained impulse / momentum transfer | 已支持 `constraint_momentum_transfer` capability + fallback trajectory + ordered contact-chain verifier；牛顿摆只是 smoke family | 接真实 UE suspension/constraint trace、contact impulse 和末端 receiver velocity |
| `spring_launch_motion` | elastic stored energy / release causality | 已支持 `elastic_energy_launch` capability + fallback trajectory + spring_events + energy-envelope verifier；弹簧发射只是 smoke family | 接真实 UE spring/release event、stored energy label、payload velocity export |
| `elastic_rope_bungee` | elastic tether / rebound constraint | 已支持 `elastic_constraint_rebound` capability + fallback trajectory + constraint_trace + max-extension/rebound verifier；蹦极只是 smoke family | 接真实 UE elastic PhysicsConstraint、extension trace、rebound velocity export |
| `fixed_camera_comparison` | multi-view alignment | 依赖 UE render pass | 接 camera trajectory/timebase verifier |
| `engine_state_timeline` | runtime state alignment | 依赖 UE instrumentation | 接 engine states / Chaos trace |
