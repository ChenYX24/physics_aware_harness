# Physics Case Targets

本文件把早期 `benchmark_prompts/physics_taxonomy.json` 中的物理 case 表整理为 harness backlog。目标不是一次性全做完，而是逐个把 case 变成：

1. capability contract
2. parameterized case template
3. runtime artifact requirement
4. invariant verifier
5. smoke / regression / benchmark

| 目标 case | 物理现象 | Harness 状态 | 下一步 |
|---|---|---|---|
| `collision_billiard_break` | rigid body collision / causality | 已支持 capability + verifier；本轮新增 dynamic template | 接真实 UE contact/trajectory |
| `collision_domino_chain` | chain collision | 已支持 capability + verifier；本轮新增 dynamic template | 接真实 UE rotation/contact order |
| `falling_stack_boxes` | gravity/contact | 已支持 capability + verifier；本轮新增 dynamic template | 支持多 block stack 和 penetration metric |
| `ramp_roll_gravity` | inclined plane | 已支持 capability + fallback trajectory + friction-aware verifier；新增 dynamic template | 接真实 UE ramp collider/contact 和 friction material |
| `pendulum_constraint` | constraint motion | 本轮新增 template contract，不跑 runtime | 接 constraint trace / distance preservation metric |
| `bounce_restitution_ball` | restitution | 未实现 | 加 bounce height / energy stability invariant |
| `rolling_friction_ball` | rolling friction | 未实现 | 加 rolling distance vs friction response |
| `sliding_crate_friction` | sliding friction | 未实现 | 加 stop distance / static friction threshold |
| `wind_balloon_drift` | force field wind | 未实现 | 加 wind vector and displacement invariant |
| `mass_ratio_collision` | momentum transfer | 部分可复用 billiards | 加 mass ratio / post-collision velocity ordering |
| `angular_damping_spin` | rotational damping | 未实现 | 加 spin decay trace |
| `agent_push_box` / `agent_throw_ball` | agent-to-rigidbody | 未实现 | 需要 action trace + skeletal/rigid body coupling |
| `fixed_camera_comparison` | multi-view alignment | 依赖 UE render pass | 接 camera trajectory/timebase verifier |
| `engine_state_timeline` | runtime state alignment | 依赖 UE instrumentation | 接 engine states / Chaos trace |
