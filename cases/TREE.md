# Case 目录导航（自动生成）

> 生成命令：`python scripts/harness_case_tree.py`；CI/本地检查：`python scripts/harness_case_tree.py --check`。请勿手改本文件。

## 三类位置

- `repo/cases/`：可维护的 CaseSpec 与模板，是输入契约；不放 MP4、EXR、OBJ 或临时 run。
- `$SIM_HARNESS_WORKSPACE/cases/<case_id>/<variant_id>/`：给人和下游工具使用的两层媒体目录；变体内固定为 `rgb/`、`depth/`、`segmentation/`、`overall/` 和 `variant.json`。
- `$SIM_HARNESS_WORKSPACE/runs/`：内部 source run、日志、缓存与质量证据；历史路径名仅用于定位。
- 本脚本只生成导航文档；规划器、backend 和 verifier 均不读取目录名来决定物理流程。

## 目录树

```text
cases/
├── agent_action/
│   ├── agent_pick_and_place_on_table.json
│   ├── agent_pick_object_lift.json
│   ├── agent_push_box_contact.json
│   ├── agent_throw_ball_release.json
│   ├── agent_throw_object_into_bin.json
│   ├── negative_missing_action_trace.json
│   ├── negative_no_post_action_motion.json
│   ├── negative_place_outside_goal.json
│   └── negative_target_preaction_motion.json
├── billiards/
│   ├── angled_grazing_contact.json
│   ├── low_speed_single_contact.json
│   ├── medium_speed_six_ball_break.json
│   ├── multi_ball_chain_contact.json
│   ├── negative_hidden_target_velocity.json
│   ├── negative_precontact_motion.json
│   ├── six_ball_triangle_low_speed.json
│   └── sixteen_ball_reference_break.json
├── bounce/
│   ├── high_restitution_bounce.json
│   ├── low_restitution_bounce.json
│   ├── negative_energy_gain.json
│   ├── negative_missing_contact.json
│   └── negative_no_rebound.json
├── bowling/
│   ├── bowling_pin_chain_contact.json
│   └── negative_pin_precontact_motion.json
├── constraint/
│   ├── negative_constraint_length_drift.json
│   ├── negative_missing_constraint_label.json
│   ├── negative_teleporting_body.json
│   ├── pendulum_length_preserved.json
│   └── pendulum_swing_crosses_center.json
├── domino/
│   ├── five_domino_chain.json
│   ├── negative_simultaneous_motion.json
│   └── six_domino_chain.json
├── elastic_constraint/
│   ├── bungee_parameter_matrix/
│   │   ├── baseline_h1p0_m0p8.json
│   │   ├── height_h1p2_m0p8.json
│   │   ├── length_l1p0_profile.json
│   │   ├── length_l1p4_h1p0_m0p8.json
│   │   ├── length_l1p5_profile.json
│   │   └── mass_h1p0_m1p05.json
│   ├── bungee_rebound.json
│   ├── elastic_rope_rebound.json
│   ├── negative_missing_constraint_trace.json
│   ├── negative_no_rebound.json
│   └── negative_overstretch.json
├── elastic_launch/
│   ├── negative_energy_gain.json
│   ├── negative_missing_release_event.json
│   ├── negative_no_launch_response.json
│   ├── spring_launch_forward_arc.json
│   └── vertical_spring_launch.json
├── falling/
│   ├── falling_block_on_floor.json
│   ├── negative_floating_block.json
│   └── stacked_blocks_contact.json
├── field_force/
│   └── magnetic/
│       └── v001_attract_repel/
│           ├── attract.json
│           └── repel.json
├── fluid/
│   ├── container_fill_stirring/
│   │   └── v001_swirl_release.json
│   ├── container_to_container_transfer/
│   │   ├── v001_spout_release.json
│   │   └── v002_wine_glass_to_teacup.json
│   ├── container_to_surface_spill/
│   │   ├── coffee_mug_import_request.json
│   │   └── v001_coffee_mug_table_spill.json
│   ├── drop_in_liquid/
│   │   └── rubber_and_lead_balls.json
│   ├── fluid_drop_height_matrix/
│   │   ├── drop_z_0p55.json
│   │   ├── drop_z_0p65.json
│   │   └── drop_z_0p75.json
│   ├── fountain/
│   │   └── v001_jet_pulse.json
│   ├── fluid_drop_in_basin.json
│   └── negative_no_gravity_response.json
├── fracture/
│   ├── glass_energy_response_matrix/
│   │   ├── glass_panel_e04_crack.json
│   │   ├── glass_panel_e16_shatter.json
│   │   └── glass_panel_e36_burst.json
│   ├── glass_impact_position_matrix/
│   │   ├── left_x_m0p45.json
│   │   └── right_x_p0p45.json
│   ├── steel_ball_board_energy_matrix/
│   │   ├── steel_ball_board_e02_intact.json
│   │   ├── steel_ball_board_e08_intact.json
│   │   └── steel_ball_board_e18_fracture.json
│   ├── glass_panel_impact_fracture.json
│   ├── glass_panel_impact_fracture_ue_probe.json
│   ├── negative_below_threshold_fracture.json
│   ├── negative_fracture_before_contact.json
│   ├── negative_missing_fracture_event.json
│   ├── negative_too_few_fragments.json
│   └── wood_crate_impact_fracture.json
├── impulse_chain/
│   ├── negative_contact_order_violation.json
│   ├── negative_passive_prechain_motion.json
│   ├── negative_terminal_no_response.json
│   ├── newton_cradle_five_ball_transfer.json
│   └── three_body_impulse_transfer.json
├── magnetic/
│   ├── attract_magnetic_body.json
│   ├── negative_missing_magnetic_label.json
│   ├── negative_wrong_magnetic_direction.json
│   └── repel_magnetic_body.json
├── mass_ratio/
│   ├── heavy_striker_light_target.json
│   ├── light_striker_heavy_target.json
│   ├── negative_missing_mass_label.json
│   ├── negative_momentum_gain.json
│   └── negative_wrong_velocity_order.json
├── projectile/
│   ├── low_angle_forward_throw.json
│   ├── negative_missing_landing_contact.json
│   ├── negative_no_gravity_float.json
│   └── upward_throw_arc.json
├── ramp/
│   ├── negative_no_friction_sensitivity.json
│   ├── negative_uphill_without_force.json
│   ├── ramp_roll_low_friction.json
│   └── ramp_slide_high_friction_short_travel.json
├── rigid_collision/
│   └── newton_cradle/
│       └── v001_release_angle_ofat/
│           ├── release_25deg.json
│           ├── release_35deg.json
│           └── release_45deg.json
├── rigid_motion/
│   └── ramp_roll_slide/
│       └── v001_friction_regime_ofat/
│           ├── high_friction_roll.json
│           ├── low_friction_slide.json
│           └── medium_friction_partial_roll.json
├── rolling/
│   ├── high_friction_short_roll.json
│   ├── medium_friction_roll.json
│   ├── negative_excessive_friction_stop.json
│   ├── negative_missing_contact.json
│   └── negative_no_deceleration.json
├── sliding/
│   ├── medium_friction_slide.json
│   ├── negative_missing_contact.json
│   ├── negative_no_deceleration.json
│   ├── negative_static_threshold_violation.json
│   └── static_threshold_hold.json
├── soft_body/
│   ├── cloth_drape/
│   │   └── v001_taichi_cloth_over_sphere.json
│   ├── elastic_collision/
│   │   └── v001_youngs_modulus_ofat/
│   │       ├── high_e200k.json
│   │       ├── low_e50k.json
│   │       └── mid_e100k.json
│   └── flag_wind/
│       ├── v002_wind_speed_ofat/
│       │   ├── high_wind_6p5.json
│       │   └── low_wind_3p0.json
│       └── v001_taichi_pinned_flag_wind.json
├── spin/
│   ├── high_damping_spin_decay.json
│   ├── low_damping_spin_decay.json
│   ├── negative_missing_angular_velocity_label.json
│   ├── negative_no_spin_decay.json
│   └── negative_spin_gain.json
├── templates/
│   ├── agent_rigidbody_action.template.json
│   ├── angular_damping_spin.template.json
│   ├── billiards_collision.template.json
│   ├── bounce_restitution.template.json
│   ├── bowling_pin_chain.template.json
│   ├── brittle_impact_fracture.template.json
│   ├── constraint_momentum_transfer.template.json
│   ├── domino_chain.template.json
│   ├── elastic_constraint_rebound.template.json
│   ├── elastic_energy_launch.template.json
│   ├── falling_blocks.template.json
│   ├── magnetic_force_field.template.json
│   ├── mass_ratio_collision.template.json
│   ├── pendulum_contact.template.json
│   ├── projectile_motion.template.json
│   ├── ramp_sliding.template.json
│   ├── rolling_friction.template.json
│   ├── sliding_crate_friction.template.json
│   └── wind_balloon_drift.template.json
└── wind/
    ├── east_wind_balloon_drift.json
    ├── negative_missing_wind_label.json
    ├── negative_no_wind_drift.json
    ├── negative_wrong_direction.json
    └── northeast_wind_light_body_drift.json
```

## 文件夹说明

| 文件夹 | 是什么 / 体现什么 | Harness 必须记住 |
|---|---|---|
| `agent_action/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `billiards/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `bounce/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `bowling/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `constraint/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `domino/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `elastic_constraint/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `elastic_constraint/bungee_parameter_matrix/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `elastic_launch/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `falling/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `field_force/magnetic/v001_attract_repel/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `fluid/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `fluid/container_fill_stirring/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `fluid/container_to_container_transfer/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `fluid/container_to_surface_spill/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `fluid/drop_in_liquid/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `fluid/fluid_drop_height_matrix/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `fluid/fountain/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `fracture/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `fracture/glass_energy_response_matrix/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `fracture/glass_impact_position_matrix/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `fracture/steel_ball_board_energy_matrix/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `impulse_chain/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `magnetic/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `mass_ratio/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `projectile/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `ramp/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `rigid_collision/newton_cradle/v001_release_angle_ofat/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `rigid_motion/ramp_roll_slide/v001_friction_regime_ofat/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `rolling/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `sliding/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `soft_body/cloth_drape/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `soft_body/elastic_collision/v001_youngs_modulus_ofat/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `soft_body/flag_wind/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `soft_body/flag_wind/v002_wind_speed_ofat/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `spin/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `templates/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| `wind/` | 历史目录名；仅用于定位声明式 CaseSpec，不参与规划、求解或验证分派。 | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |

## 每个 Case / 模板

| Case | 类型 | 兼容标签 | 说明 | Harness 必须记住 |
|---|---|---|---|---|
| [`agent_action/agent_pick_and_place_on_table.json`](agent_action/agent_pick_and_place_on_table.json) | 正向 | `agent_rigidbody_action_coupling` | A person picks up a small package from one low table, carries it, and places it on a second table. The package must be grasped, visibly carried by the hand, released inside the target region, contact the target table, and settle. | trajectory, action_trace, contact_events, object_roles, final_object_state |
| [`agent_action/agent_pick_object_lift.json`](agent_action/agent_pick_object_lift.json) | 正向 | `agent_rigidbody_action_coupling` | A person grasps a package that starts at rest and lifts it clear of the floor. The package, not only the hand animation, must rise and finish held still. | trajectory, action_trace, contact_events, object_roles, final_object_state |
| [`agent_action/agent_push_box_contact.json`](agent_action/agent_push_box_contact.json) | 正向 | `agent_rigidbody_action_coupling` | An agent pushes a box from rest. The box must remain still before the agent push action and move only after contact/action evidence. | trajectory, action_trace, contact_events, object_roles, post_action_velocity |
| [`agent_action/agent_throw_ball_release.json`](agent_action/agent_throw_ball_release.json) | 正向 | `agent_rigidbody_action_coupling` | An agent throws a ball. The ball starts still, then receives a release impulse from the agent action and moves after the action frame. | trajectory, action_trace, contact_events, object_roles, post_action_velocity |
| [`agent_action/agent_throw_object_into_bin.json`](agent_action/agent_throw_object_into_bin.json) | 正向 | `agent_rigidbody_action_coupling` | A person throws a ball into a bin. The ball must start still, be released with an impulse, enter the bin target region, contact the bin, and settle. | trajectory, action_trace, contact_events, object_roles, final_object_state |
| [`agent_action/negative_missing_action_trace.json`](agent_action/negative_missing_action_trace.json) | 负向/边界 | `agent_rigidbody_action_coupling` | A box moves as if pushed, but there is no structured action trace proving which agent action caused the motion. | trajectory, action_trace, contact_events, object_roles, post_action_velocity |
| [`agent_action/negative_no_post_action_motion.json`](agent_action/negative_no_post_action_motion.json) | 负向/边界 | `agent_rigidbody_action_coupling` | An agent push action is recorded, but the target rigid body never responds after the action. | trajectory, action_trace, contact_events, object_roles, post_action_velocity |
| [`agent_action/negative_place_outside_goal.json`](agent_action/negative_place_outside_goal.json) | 负向/边界 | `agent_rigidbody_action_coupling` | A person performs grasp and release actions, but the package is released outside the table target region. | trajectory, action_trace, contact_events, object_roles, final_object_state |
| [`agent_action/negative_target_preaction_motion.json`](agent_action/negative_target_preaction_motion.json) | 负向/边界 | `agent_rigidbody_action_coupling` | A target box starts moving before the agent action occurs, which must be rejected as hidden pre-action motion. | trajectory, action_trace, contact_events, object_roles, post_action_velocity |
| [`billiards/angled_grazing_contact.json`](billiards/angled_grazing_contact.json) | 正向 | `rigid_body_contact_causality` | Cue ball approaches at a shallow angle and grazes the first passive target ball. Passive targets must not move before contact. | trajectory, contact_events, camera_trajectory |
| [`billiards/low_speed_single_contact.json`](billiards/low_speed_single_contact.json) | 正向 | `rigid_body_contact_causality` | A low-speed cue ball hits one passive target ball on a flat low-friction table. | trajectory, contact_events, camera_trajectory |
| [`billiards/medium_speed_six_ball_break.json`](billiards/medium_speed_six_ball_break.json) | 正向 | `rigid_body_contact_causality` | Medium-speed cue ball breaks a compact six-ball rack. Target balls are passive and may only move after collision propagation. | trajectory, contact_events, camera_trajectory, depth, segmentation |
| [`billiards/multi_ball_chain_contact.json`](billiards/multi_ball_chain_contact.json) | 正向 | `rigid_body_contact_causality` | A cue ball hits a passive target ball which then contacts a second passive target ball. | trajectory, contact_events, camera_trajectory |
| [`billiards/negative_hidden_target_velocity.json`](billiards/negative_hidden_target_velocity.json) | 负向/边界 | `rigid_body_contact_causality` | Negative billiards case: a passive target is given hidden initial velocity before contact. | trajectory, contact_events |
| [`billiards/negative_precontact_motion.json`](billiards/negative_precontact_motion.json) | 负向/边界 | `rigid_body_contact_causality` | Negative billiards case: passive target moves before cue ball contact. | trajectory, contact_events |
| [`billiards/six_ball_triangle_low_speed.json`](billiards/six_ball_triangle_low_speed.json) | 正向 | `rigid_body_contact_causality` | Low-speed billiards break: one cue ball gently contacts a compact six-ball triangle on a low-friction table. Passive balls must not move before contact. | trajectory, contact_events, camera_trajectory, depth, segmentation |
| [`billiards/sixteen_ball_reference_break.json`](billiards/sixteen_ball_reference_break.json) | 正向 | `rigid_body_contact_causality` | A polished cue ball breaks a tightly racked fifteen-ball triangle on a green felt billiards table. Preserve passive-ball causality and render clear static, top-down, tracking, and event views. | trajectory, contact_events, camera_trajectory, rgb, depth, segmentation |
| [`bounce/high_restitution_bounce.json`](bounce/high_restitution_bounce.json) | 正向 | `bounce_restitution_ball` | A rigid ball drops vertically onto a floor and rebounds with high restitution. The rebound must happen only after contact. | trajectory, contact_events, gravity_label, material_restitution_label |
| [`bounce/low_restitution_bounce.json`](bounce/low_restitution_bounce.json) | 正向 | `bounce_restitution_ball` | A rigid ball drops onto a floor and makes a small low-restitution rebound after impact. | trajectory, contact_events, gravity_label, material_restitution_label |
| [`bounce/negative_energy_gain.json`](bounce/negative_energy_gain.json) | 负向/边界 | `bounce_restitution_ball` | A rigid ball drops onto a floor but the runtime trace incorrectly shows a rebound higher than restitution allows. | trajectory, contact_events, gravity_label, material_restitution_label |
| [`bounce/negative_missing_contact.json`](bounce/negative_missing_contact.json) | 负向/边界 | `bounce_restitution_ball` | A rigid ball drops and rebounds, but the runtime artifact incorrectly omits the impact contact event. | trajectory, contact_events, gravity_label, material_restitution_label |
| [`bounce/negative_no_rebound.json`](bounce/negative_no_rebound.json) | 负向/边界 | `bounce_restitution_ball` | A rigid ball drops onto a floor but the runtime trace incorrectly shows no rebound despite restitution. | trajectory, contact_events, gravity_label, material_restitution_label |
| [`bowling/bowling_pin_chain_contact.json`](bowling/bowling_pin_chain_contact.json) | 正向 | `rigid_body_contact_causality` | A bowling ball rolls into a short line of passive pins. Pins must stay still until the bowling ball or another already-contacted pin hits them. | trajectory, contact_events, camera_trajectory |
| [`bowling/negative_pin_precontact_motion.json`](bowling/negative_pin_precontact_motion.json) | 负向/边界 | `rigid_body_contact_causality` | A bowling pin moves before any ball or contacted pin touches it, which should be rejected as hidden pre-contact motion. | trajectory, contact_events, camera_trajectory |
| [`constraint/negative_constraint_length_drift.json`](constraint/negative_constraint_length_drift.json) | 负向/边界 | `constraint_distance_pendulum_motion` | A pendulum bob drifts away from its declared constraint length, which must be rejected. | trajectory, constraint_trace, constraint_parameter_labels |
| [`constraint/negative_missing_constraint_label.json`](constraint/negative_missing_constraint_label.json) | 负向/边界 | `constraint_distance_pendulum_motion` | A pendulum-like visual appears, but the case spec does not declare the constraint length. | trajectory, constraint_trace, constraint_parameter_labels |
| [`constraint/negative_teleporting_body.json`](constraint/negative_teleporting_body.json) | 负向/边界 | `constraint_distance_pendulum_motion` | A constrained bob teleports between frames instead of moving continuously along a swing arc. | trajectory, constraint_trace, constraint_parameter_labels |
| [`constraint/pendulum_length_preserved.json`](constraint/pendulum_length_preserved.json) | 正向 | `constraint_distance_pendulum_motion` | A pendulum bob is released from an angle and swings while preserving a fixed anchor-to-bob distance. | trajectory, constraint_trace, constraint_parameter_labels |
| [`constraint/pendulum_swing_crosses_center.json`](constraint/pendulum_swing_crosses_center.json) | 正向 | `constraint_distance_pendulum_motion` | A pendulum swings across the vertical center line while keeping the same constraint length. | trajectory, constraint_trace, constraint_parameter_labels |
| [`domino/five_domino_chain.json`](domino/five_domino_chain.json) | 正向 | `sequential_contact_propagation` | Five upright dominoes tip in order after the first domino starts from a small unstable angle. Only the initial state is prescribed; Unreal Engine Chaos solves every later transform and contact. | trajectory, contact_events, rotation, camera_trajectory, rgb, depth, … |
| [`domino/negative_simultaneous_motion.json`](domino/negative_simultaneous_motion.json) | 负向/边界 | `sequential_contact_propagation` | Negative domino case: multiple passive dominoes start moving without contact. | trajectory, contact_events, rotation |
| [`domino/six_domino_chain.json`](domino/six_domino_chain.json) | 正向 | `sequential_contact_propagation` | Six upright dominoes tip in order after the first domino starts from a small unstable angle. Only the initial state is prescribed; Unreal Engine Chaos solves every later transform and contact. | trajectory, contact_events, rotation, camera_trajectory, rgb, depth, … |
| [`elastic_constraint/bungee_parameter_matrix/baseline_h1p0_m0p8.json`](elastic_constraint/bungee_parameter_matrix/baseline_h1p0_m0p8.json) | 正向 | `elastic_constraint_rebound` | A 0.8 kg payload starts at 1.0 m, falls on an elastic bungee tether, rebounds, and settles. | trajectory, constraint_trace, elastic_constraint_labels |
| [`elastic_constraint/bungee_parameter_matrix/height_h1p2_m0p8.json`](elastic_constraint/bungee_parameter_matrix/height_h1p2_m0p8.json) | 正向 | `elastic_constraint_rebound` | A 0.8 kg payload starts higher at 1.2 m, falls farther before the bungee tether becomes taut, rebounds, and settles. | trajectory, constraint_trace, elastic_constraint_labels |
| [`elastic_constraint/bungee_parameter_matrix/length_l1p0_profile.json`](elastic_constraint/bungee_parameter_matrix/length_l1p0_profile.json) | 正向 | `elastic_constraint_rebound` | A 0.8 kg payload starts at 1.0 m on a short 1.0 m bungee tether, rebounds, and settles in a vertical profile view. | trajectory, constraint_trace, elastic_constraint_labels |
| [`elastic_constraint/bungee_parameter_matrix/length_l1p4_h1p0_m0p8.json`](elastic_constraint/bungee_parameter_matrix/length_l1p4_h1p0_m0p8.json) | 正向 | `elastic_constraint_rebound` | A 0.8 kg payload starts at 1.0 m on a longer 1.4 m bungee tether, falls lower, rebounds, and settles. | trajectory, constraint_trace, elastic_constraint_labels |
| [`elastic_constraint/bungee_parameter_matrix/length_l1p5_profile.json`](elastic_constraint/bungee_parameter_matrix/length_l1p5_profile.json) | 正向 | `elastic_constraint_rebound` | A 0.8 kg payload starts at 1.0 m on a long 1.5 m bungee tether, falls near the ground, rebounds, and settles in a vertical profile view. | trajectory, constraint_trace, elastic_constraint_labels |
| [`elastic_constraint/bungee_parameter_matrix/mass_h1p0_m1p05.json`](elastic_constraint/bungee_parameter_matrix/mass_h1p0_m1p05.json) | 正向 | `elastic_constraint_rebound` | A heavier 1.05 kg payload starts at 1.0 m, stretches the same bungee tether farther, rebounds for longer, and settles. | trajectory, constraint_trace, elastic_constraint_labels |
| [`elastic_constraint/bungee_rebound.json`](elastic_constraint/bungee_rebound.json) | 正向 | `elastic_constraint_rebound` | A payload falls on an elastic bungee tether, stretches it, then rebounds upward toward the anchor. | trajectory, constraint_trace, elastic_constraint_labels |
| [`elastic_constraint/elastic_rope_rebound.json`](elastic_constraint/elastic_rope_rebound.json) | 正向 | `elastic_constraint_rebound` | A rigid body stretches an elastic rope below its rest length limit and rebounds back toward the anchor. | trajectory, constraint_trace, elastic_constraint_labels |
| [`elastic_constraint/negative_missing_constraint_trace.json`](elastic_constraint/negative_missing_constraint_trace.json) | 负向/边界 | `elastic_constraint_rebound` | Negative elastic tether case: the payload appears to rebound, but no elastic constraint trace is exported. | trajectory, constraint_trace, elastic_constraint_labels |
| [`elastic_constraint/negative_no_rebound.json`](elastic_constraint/negative_no_rebound.json) | 负向/边界 | `elastic_constraint_rebound` | Negative elastic tether case: the payload reaches peak stretch but does not rebound toward the anchor. | trajectory, constraint_trace, elastic_constraint_labels |
| [`elastic_constraint/negative_overstretch.json`](elastic_constraint/negative_overstretch.json) | 负向/边界 | `elastic_constraint_rebound` | Negative elastic tether case: the payload stretches beyond the declared maximum extension. | trajectory, constraint_trace, elastic_constraint_labels |
| [`elastic_launch/negative_energy_gain.json`](elastic_launch/negative_energy_gain.json) | 负向/边界 | `elastic_energy_launch` | A payload launches with far more kinetic energy than the declared spring compression can provide. | trajectory, spring_events, energy_labels |
| [`elastic_launch/negative_missing_release_event.json`](elastic_launch/negative_missing_release_event.json) | 负向/边界 | `elastic_energy_launch` | A payload appears to launch, but no structured release event is exported. | trajectory, spring_events, energy_labels |
| [`elastic_launch/negative_no_launch_response.json`](elastic_launch/negative_no_launch_response.json) | 负向/边界 | `elastic_energy_launch` | A release event exists, but the payload does not gain post-release speed or displacement. | trajectory, spring_events, energy_labels |
| [`elastic_launch/spring_launch_forward_arc.json`](elastic_launch/spring_launch_forward_arc.json) | 正向 | `elastic_energy_launch` | A compressed spring releases a payload upward and forward in a controlled arc. | trajectory, spring_events, energy_labels |
| [`elastic_launch/vertical_spring_launch.json`](elastic_launch/vertical_spring_launch.json) | 正向 | `elastic_energy_launch` | A vertical compressed launcher releases a payload upward with bounded elastic energy. | trajectory, spring_events, energy_labels |
| [`falling/falling_block_on_floor.json`](falling/falling_block_on_floor.json) | 正向 | `rigid_body_gravity_collision` | A rigid block falls under gravity and contacts the floor. | trajectory, contact_events, gravity_label |
| [`falling/negative_floating_block.json`](falling/negative_floating_block.json) | 负向/边界 | `rigid_body_gravity_collision` | Negative falling case: block floats without descending or contacting the floor. | trajectory, contact_events, gravity_label |
| [`falling/stacked_blocks_contact.json`](falling/stacked_blocks_contact.json) | 正向 | `rigid_body_gravity_collision` | A falling block descends and contacts a support floor in a stacked-block style setup. | trajectory, contact_events, gravity_label |
| [`field_force/magnetic/v001_attract_repel/attract.json`](field_force/magnetic/v001_attract_repel/attract.json) | 正向 | `magnetic_force_field` | A steel sphere on a laboratory platform is pulled toward a fixed red magnet by a declared magnetic field, then settles after contact. | trajectory, magnetic_field_label, force_field_label, source_relative_distance, force_trace |
| [`field_force/magnetic/v001_attract_repel/repel.json`](field_force/magnetic/v001_attract_repel/repel.json) | 正向 | `magnetic_force_field` | A steel sphere on a laboratory platform is pushed away from a fixed red magnet by a declared repulsive magnetic field, then settles outside the effective field range. | trajectory, magnetic_field_label, force_field_label, source_relative_distance, force_trace |
| [`fluid/container_fill_stirring/v001_swirl_release.json`](fluid/container_fill_stirring/v001_swirl_release.json) | 正向 | `fluid_particle_dynamics` | Water in a deep basin is released from a measured swirling initial state after stirring, then the vortex evolves and decays under the fluid solver without any scripted particle trajectories. | particle_positions, particle_velocities, surface_mesh_sequence, solver_timebase, rgb, metric_depth, … |
| [`fluid/container_to_container_transfer/v001_spout_release.json`](fluid/container_to_container_transfer/v001_spout_release.json) | 正向 | `fluid_particle_dynamics` | A short cylindrical stream is released from a source spout with lateral and downward velocity into a larger receiving basin, where it splashes, spreads, and settles under gravity. | particle_positions, particle_velocities, surface_mesh_sequence, solver_timebase, rgb, metric_depth, … |
| [`fluid/container_to_container_transfer/v002_wine_glass_to_teacup.json`](fluid/container_to_container_transfer/v002_wine_glass_to_teacup.json) | 正向 | `fluid_particle_dynamics` | A real stemmed wine glass starts upright with settled water, then tilts past its pour angle above a real shallow teacup. Water has zero scripted velocity, leaves under gravity, falls through the air, splashes into the teacup, and settles before the video ends. | particle_positions, particle_velocities, stable_particle_ids, declared_measurements, solver_assertion_results, surface_mesh_sequence, … |
| [`fluid/container_to_surface_spill/coffee_mug_import_request.json`](fluid/container_to_surface_spill/coffee_mug_import_request.json) | 负向/边界 | `-` | - | 运行时只读取对象、状态域、约束、求解器要求与通用断言。 |
| [`fluid/container_to_surface_spill/v001_coffee_mug_table_spill.json`](fluid/container_to_surface_spill/v001_coffee_mug_table_spill.json) | 正向 | `fluid_particle_dynamics` | A real coffee mug starts upright just above a flat tabletop with settled coffee inside. The mug tilts around its rim, and gravity pours the coffee onto the tabletop where it splashes and spreads horizontally. | particle_positions, particle_velocities, stable_particle_ids, declared_measurements, solver_assertion_results, surface_mesh_sequence, … |
| [`fluid/drop_in_liquid/rubber_and_lead_balls.json`](fluid/drop_in_liquid/rubber_and_lead_balls.json) | 正向 | `fluid_particle_dynamics` | A hollow rubber ball and a solid lead ball fall into a prefilled water basin, produce visible splashes, then separate as the rubber ball floats and the lead ball sinks. | particle_positions, particle_velocities, rigid_body_states, surface_mesh_sequence, solver_timebase, rgb, … |
| [`fluid/fluid_drop_height_matrix/drop_z_0p55.json`](fluid/fluid_drop_height_matrix/drop_z_0p55.json) | 正向 | `fluid_particle_dynamics` | A 0.3 m cubic water volume is released from z=0.55 m into a rigid square basin; this is the low drop-height condition. | particle_positions, particle_velocities, surface_mesh_sequence, solver_timebase, rgb, depth, … |
| [`fluid/fluid_drop_height_matrix/drop_z_0p65.json`](fluid/fluid_drop_height_matrix/drop_z_0p65.json) | 正向 | `fluid_particle_dynamics` | A 0.3 m cubic water volume is released from z=0.65 m into a rigid square basin; this is the baseline drop-height condition. | particle_positions, particle_velocities, surface_mesh_sequence, solver_timebase, rgb, depth, … |
| [`fluid/fluid_drop_height_matrix/drop_z_0p75.json`](fluid/fluid_drop_height_matrix/drop_z_0p75.json) | 正向 | `fluid_particle_dynamics` | A 0.3 m cubic water volume is released from z=0.75 m into a rigid square basin; this is the high drop-height condition. | particle_positions, particle_velocities, surface_mesh_sequence, solver_timebase, rgb, depth, … |
| [`fluid/fluid_drop_in_basin.json`](fluid/fluid_drop_in_basin.json) | 正向 | `fluid_particle_dynamics` | A cubic volume of water falls into a rigid square basin, splashes, and begins to settle. | particle_positions, particle_velocities, surface_mesh_sequence, solver_timebase |
| [`fluid/fountain/v001_jet_pulse.json`](fluid/fountain/v001_jet_pulse.json) | 正向 | `fluid_particle_dynamics` | A narrow vertical water column is launched upward from the center of a basin, forms a fountain-like jet, then falls and settles under gravity. | particle_positions, particle_velocities, surface_mesh_sequence, solver_timebase, rgb, metric_depth, … |
| [`fluid/negative_no_gravity_response.json`](fluid/negative_no_gravity_response.json) | 负向/边界 | `fluid_particle_dynamics` | Invalid regression artifact: water remains at its initial height despite declared downward gravity. | particle_positions, particle_velocities, surface_mesh_sequence, solver_timebase, container_collision_bounds |
| [`fracture/glass_energy_response_matrix/glass_panel_e04_crack.json`](fracture/glass_energy_response_matrix/glass_panel_e04_crack.json) | 正向 | `brittle_impact_fracture` | An 8 kg steel ball strikes a vertical glass-material Chaos panel at 1 m/s. The measured incident energy should select the localized cracked response, not the shatter or burst response. | trajectory, native_collision_events, impact_energy_j, damage_state, fracture_events, camera_trajectory, … |
| [`fracture/glass_energy_response_matrix/glass_panel_e16_shatter.json`](fracture/glass_energy_response_matrix/glass_panel_e16_shatter.json) | 正向 | `brittle_impact_fracture` | An 8 kg steel ball strikes a vertical glass-material Chaos panel at 2 m/s. The measured incident energy should select the shattered response, producing broader fragmentation without the burst impulse. | trajectory, native_collision_events, impact_energy_j, damage_state, fracture_events, camera_trajectory, … |
| [`fracture/glass_energy_response_matrix/glass_panel_e36_burst.json`](fracture/glass_energy_response_matrix/glass_panel_e36_burst.json) | 正向 | `brittle_impact_fracture` | An 8 kg steel ball strikes a vertical glass-material Chaos panel at 3 m/s. The measured incident energy should select the burst response, combining deep fracture propagation with an outward Chaos radial impulse. | trajectory, native_collision_events, impact_energy_j, damage_state, fracture_events, camera_trajectory, … |
| [`fracture/glass_impact_position_matrix/left_x_m0p45.json`](fracture/glass_impact_position_matrix/left_x_m0p45.json) | 正向 | `brittle_impact_fracture` | An 8 kg steel ball follows a gravity-driven arc and strikes the left side of a vertical glass panel at 2 m/s. The impact-centered radial fracture should originate at the left contact point. | trajectory, native_collision_events, impact_energy_j, damage_state, fracture_events, camera_trajectory, … |
| [`fracture/glass_impact_position_matrix/right_x_p0p45.json`](fracture/glass_impact_position_matrix/right_x_p0p45.json) | 正向 | `brittle_impact_fracture` | An 8 kg steel ball follows a gravity-driven arc and strikes the right side of a vertical glass panel at 2 m/s. The impact-centered radial fracture should originate at the right contact point. | trajectory, native_collision_events, impact_energy_j, damage_state, fracture_events, camera_trajectory, … |
| [`fracture/glass_panel_impact_fracture.json`](fracture/glass_panel_impact_fracture.json) | 正向 | `brittle_impact_fracture` | A rigid striker impacts a brittle panel. The panel fractures only after contact energy exceeds its threshold. | trajectory, contact_events, fracture_events, fragment_manifest, energy_labels |
| [`fracture/glass_panel_impact_fracture_ue_probe.json`](fracture/glass_panel_impact_fracture_ue_probe.json) | 正向 | `brittle_impact_fracture` | Diagnostic: a heavy rigid sphere starts from a declared initial state and strikes a destructible board. A declared contact-triggered external-strain command initiates damage; Unreal Engine Chaos resolves the resulting Geometry Collection break. | trajectory, native_collision_events, fracture_events, fragment_manifest, fragment_state_hash, camera_trajectory, … |
| [`fracture/negative_below_threshold_fracture.json`](fracture/negative_below_threshold_fracture.json) | 负向/边界 | `brittle_impact_fracture` | Negative brittle fracture case: a fracture is emitted even though contact energy is below threshold. | trajectory, contact_events, fracture_events, fragment_manifest, energy_labels |
| [`fracture/negative_fracture_before_contact.json`](fracture/negative_fracture_before_contact.json) | 负向/边界 | `brittle_impact_fracture` | Negative brittle fracture case: fracture is emitted before the striker contacts the brittle body. | trajectory, contact_events, fracture_events, fragment_manifest, energy_labels |
| [`fracture/negative_missing_fracture_event.json`](fracture/negative_missing_fracture_event.json) | 负向/边界 | `brittle_impact_fracture` | Negative brittle fracture case: contact energy exceeds threshold but no fracture event is exported. | trajectory, contact_events, fracture_events, fragment_manifest, energy_labels |
| [`fracture/negative_too_few_fragments.json`](fracture/negative_too_few_fragments.json) | 负向/边界 | `brittle_impact_fracture` | Negative brittle fracture case: fracture event exists but not enough fragments are exported. | trajectory, contact_events, fracture_events, fragment_manifest, energy_labels |
| [`fracture/steel_ball_board_energy_matrix/steel_ball_board_e02_intact.json`](fracture/steel_ball_board_energy_matrix/steel_ball_board_e02_intact.json) | 正向 | `brittle_impact_fracture` | A 4 kg steel sphere starts at 1 m/s and strikes a Chaos Geometry Collection board. Its nominal 2 J incident energy is below the declared 10 J gate, so contact occurs but the board must remain intact. | trajectory, native_collision_events, impact_energy_j, camera_trajectory, rgb, depth, … |
| [`fracture/steel_ball_board_energy_matrix/steel_ball_board_e08_intact.json`](fracture/steel_ball_board_energy_matrix/steel_ball_board_e08_intact.json) | 正向 | `brittle_impact_fracture` | A 4 kg steel sphere starts at 2 m/s and strikes a Chaos Geometry Collection board. Its nominal 8 J incident energy is below the declared 10 J gate, so contact occurs but the board must remain intact. | trajectory, native_collision_events, impact_energy_j, camera_trajectory, rgb, depth, … |
| [`fracture/steel_ball_board_energy_matrix/steel_ball_board_e18_fracture.json`](fracture/steel_ball_board_energy_matrix/steel_ball_board_e18_fracture.json) | 正向 | `brittle_impact_fracture` | A 4 kg steel sphere starts at 3 m/s and strikes a Chaos Geometry Collection board. Its nominal 18 J incident energy exceeds the declared 10 J gate, so the contact-triggered strain may fracture the board and Chaos must resolve the fragments. | trajectory, native_collision_events, impact_energy_j, fracture_events, fragment_manifest, fragment_state_hash, … |
| [`fracture/wood_crate_impact_fracture.json`](fracture/wood_crate_impact_fracture.json) | 正向 | `brittle_impact_fracture` | A heavy striker hits a brittle wooden crate. The crate breaks only after sufficient impact energy. | trajectory, contact_events, fracture_events, fragment_manifest, energy_labels |
| [`impulse_chain/negative_contact_order_violation.json`](impulse_chain/negative_contact_order_violation.json) | 负向/边界 | `constraint_momentum_transfer` | A constrained impulse-chain case with out-of-order contacts, which must be rejected. | trajectory, contact_events, constraint_trace, mass_labels |
| [`impulse_chain/negative_passive_prechain_motion.json`](impulse_chain/negative_passive_prechain_motion.json) | 负向/边界 | `constraint_momentum_transfer` | A constrained impulse-chain case where a passive middle body already moves before causal contact, which must be rejected. | trajectory, contact_events, constraint_trace, mass_labels |
| [`impulse_chain/negative_terminal_no_response.json`](impulse_chain/negative_terminal_no_response.json) | 负向/边界 | `constraint_momentum_transfer` | A constrained impulse-chain case where adjacent contacts occur but the terminal receiver never moves. | trajectory, contact_events, constraint_trace, mass_labels |
| [`impulse_chain/newton_cradle_five_ball_transfer.json`](impulse_chain/newton_cradle_five_ball_transfer.json) | 正向 | `constraint_momentum_transfer` | A five-body constrained impulse chain transfers motion from the first suspended ball to the final receiver. | trajectory, contact_events, constraint_trace, mass_labels |
| [`impulse_chain/three_body_impulse_transfer.json`](impulse_chain/three_body_impulse_transfer.json) | 正向 | `constraint_momentum_transfer` | A short constrained impulse chain transfers an active impulse through one middle body to a final receiver. | trajectory, contact_events, constraint_trace, mass_labels |
| [`magnetic/attract_magnetic_body.json`](magnetic/attract_magnetic_body.json) | 正向 | `magnetic_force_field` | A magnetized steel ball is attracted toward a fixed magnetic source. The motion must be caused by a declared magnetic field, not a hidden animation. | trajectory, magnetic_field_label, force_field_label |
| [`magnetic/negative_missing_magnetic_label.json`](magnetic/negative_missing_magnetic_label.json) | 负向/边界 | `magnetic_force_field` | A magnetized body visibly moves, but the case spec omits the structured magnetic mode and field strength. | trajectory, magnetic_field_label, force_field_label |
| [`magnetic/negative_wrong_magnetic_direction.json`](magnetic/negative_wrong_magnetic_direction.json) | 负向/边界 | `magnetic_force_field` | A magnetized body is labeled as attracted to a source, but the runtime trace moves it away. | trajectory, magnetic_field_label, force_field_label |
| [`magnetic/repel_magnetic_body.json`](magnetic/repel_magnetic_body.json) | 正向 | `magnetic_force_field` | A magnetized body is repelled away from a fixed magnetic source. The trajectory should move radially outward under a declared magnetic field. | trajectory, magnetic_field_label, force_field_label |
| [`mass_ratio/heavy_striker_light_target.json`](mass_ratio/heavy_striker_light_target.json) | 正向 | `mass_ratio_momentum_transfer` | A heavy active striker collides with a lighter passive target. The target should move faster after contact while the striker slows down. | trajectory, contact_events, mass_labels, post_collision_velocity |
| [`mass_ratio/light_striker_heavy_target.json`](mass_ratio/light_striker_heavy_target.json) | 正向 | `mass_ratio_momentum_transfer` | A light active striker collides with a heavier passive target. The heavy target should move only modestly while the light striker slows or rebounds. | trajectory, contact_events, mass_labels, post_collision_velocity |
| [`mass_ratio/negative_missing_mass_label.json`](mass_ratio/negative_missing_mass_label.json) | 负向/边界 | `mass_ratio_momentum_transfer` | A contact collision is requested, but the passive target has no mass label, so mass-ratio transfer cannot be verified. | trajectory, contact_events, mass_labels, post_collision_velocity |
| [`mass_ratio/negative_momentum_gain.json`](mass_ratio/negative_momentum_gain.json) | 负向/边界 | `mass_ratio_momentum_transfer` | A mass-ratio collision trace injects too much kinetic energy after contact without a declared external force. | trajectory, contact_events, mass_labels, post_collision_velocity |
| [`mass_ratio/negative_wrong_velocity_order.json`](mass_ratio/negative_wrong_velocity_order.json) | 负向/边界 | `mass_ratio_momentum_transfer` | A heavy striker hits a light target, but the runtime trace makes the light target too slow relative to the striker. | trajectory, contact_events, mass_labels, post_collision_velocity |
| [`projectile/low_angle_forward_throw.json`](projectile/low_angle_forward_throw.json) | 正向 | `projectile_gravity_motion` | A rigid object is thrown forward at a low angle. It travels horizontally while gravity pulls it down to ground contact. | trajectory, contact_events, gravity_label, initial_velocity |
| [`projectile/negative_missing_landing_contact.json`](projectile/negative_missing_landing_contact.json) | 负向/边界 | `projectile_gravity_motion` | Invalid projectile case: the path rises and descends but the landing contact event is missing. | trajectory, contact_events, gravity_label, initial_velocity |
| [`projectile/negative_no_gravity_float.json`](projectile/negative_no_gravity_float.json) | 负向/边界 | `projectile_gravity_motion` | Invalid projectile case: the object keeps floating upward without descending under gravity. | trajectory, contact_events, gravity_label, initial_velocity |
| [`projectile/upward_throw_arc.json`](projectile/upward_throw_arc.json) | 正向 | `projectile_gravity_motion` | A rigid ball is thrown upward and forward. It rises to an apex, descends under gravity, and lands on the ground. | trajectory, contact_events, gravity_label, initial_velocity |
| [`ramp/negative_no_friction_sensitivity.json`](ramp/negative_no_friction_sensitivity.json) | 负向/边界 | `ramp_sliding_friction` | Invalid ramp case: a low-friction ramp produces almost no downhill travel, contradicting the expected friction response. | trajectory, contact_events, ramp_angle_label, material_friction_label |
| [`ramp/negative_uphill_without_force.json`](ramp/negative_uphill_without_force.json) | 负向/边界 | `ramp_sliding_friction` | Invalid ramp case: a passive object starts at rest but moves uphill without any force or actuator. | trajectory, contact_events, ramp_angle_label, material_friction_label |
| [`ramp/ramp_roll_low_friction.json`](ramp/ramp_roll_low_friction.json) | 正向 | `ramp_sliding_friction` | A passive ball starts at rest near the top of a low-friction inclined ramp and rolls downhill under gravity. | trajectory, contact_events, ramp_angle_label, material_friction_label |
| [`ramp/ramp_slide_high_friction_short_travel.json`](ramp/ramp_slide_high_friction_short_travel.json) | 正向 | `ramp_sliding_friction` | A passive crate starts at rest on a higher-friction inclined ramp and slides a shorter distance downhill. | trajectory, contact_events, ramp_angle_label, material_friction_label |
| [`rigid_collision/newton_cradle/v001_release_angle_ofat/release_25deg.json`](rigid_collision/newton_cradle/v001_release_angle_ofat/release_25deg.json) | 正向 | `constraint_momentum_transfer` | A five-ball Newton's cradle in an open market demonstration area releases the left ball from 25 degrees and transfers the impulse to the right receiver. | trajectory, contact_events, constraint_trace, mass_labels, solver_provenance |
| [`rigid_collision/newton_cradle/v001_release_angle_ofat/release_35deg.json`](rigid_collision/newton_cradle/v001_release_angle_ofat/release_35deg.json) | 正向 | `constraint_momentum_transfer` | A five-ball Newton's cradle in an open market demonstration area releases the left ball from 35 degrees and transfers the impulse to the right receiver. | trajectory, contact_events, constraint_trace, mass_labels, solver_provenance |
| [`rigid_collision/newton_cradle/v001_release_angle_ofat/release_45deg.json`](rigid_collision/newton_cradle/v001_release_angle_ofat/release_45deg.json) | 正向 | `constraint_momentum_transfer` | A five-ball Newton's cradle in an open market demonstration area releases the left ball from 45 degrees and transfers the stronger impulse to the right receiver. | trajectory, contact_events, constraint_trace, mass_labels, solver_provenance |
| [`rigid_motion/ramp_roll_slide/v001_friction_regime_ofat/high_friction_roll.json`](rigid_motion/ramp_roll_slide/v001_friction_regime_ofat/high_friction_roll.json) | 正向 | `ramp_sliding_friction` | A marked billiard ball starts from rest on an industrial ramp, rolls without meaningful slip under high contact friction, enters a high-friction runout, and settles before the video ends. | trajectory, contact_events, angular_velocity, material_friction_label, solver_provenance |
| [`rigid_motion/ramp_roll_slide/v001_friction_regime_ofat/low_friction_slide.json`](rigid_motion/ramp_roll_slide/v001_friction_regime_ofat/low_friction_slide.json) | 正向 | `ramp_sliding_friction` | A marked billiard ball starts from rest on an industrial ramp, slides with very low contact friction, enters a high-friction runout, and settles before the video ends. | trajectory, contact_events, angular_velocity, material_friction_label, solver_provenance |
| [`rigid_motion/ramp_roll_slide/v001_friction_regime_ofat/medium_friction_partial_roll.json`](rigid_motion/ramp_roll_slide/v001_friction_regime_ofat/medium_friction_partial_roll.json) | 正向 | `ramp_sliding_friction` | A marked billiard ball starts from rest on an industrial ramp, partially rolls with medium contact friction, enters a high-friction runout, and settles before the video ends. | trajectory, contact_events, angular_velocity, material_friction_label, solver_provenance |
| [`rolling/high_friction_short_roll.json`](rolling/high_friction_short_roll.json) | 正向 | `rolling_friction_ball` | A rigid ball rolls on a high-friction floor and stops after a short distance. | trajectory, contact_events, initial_velocity, material_friction_label |
| [`rolling/medium_friction_roll.json`](rolling/medium_friction_roll.json) | 正向 | `rolling_friction_ball` | A rigid ball rolls across a flat floor with medium friction. It starts with horizontal velocity and slows down while staying in contact. | trajectory, contact_events, initial_velocity, material_friction_label |
| [`rolling/negative_excessive_friction_stop.json`](rolling/negative_excessive_friction_stop.json) | 负向/边界 | `rolling_friction_ball` | A rigid ball has initial speed on a low-friction floor but the runtime trace incorrectly stops it almost immediately. | trajectory, contact_events, initial_velocity, material_friction_label |
| [`rolling/negative_missing_contact.json`](rolling/negative_missing_contact.json) | 负向/边界 | `rolling_friction_ball` | A rigid ball rolls across a floor, but the runtime artifact omits support contact evidence. | trajectory, contact_events, initial_velocity, material_friction_label |
| [`rolling/negative_no_deceleration.json`](rolling/negative_no_deceleration.json) | 负向/边界 | `rolling_friction_ball` | A rigid ball rolls on a frictional floor but the runtime trace incorrectly shows almost no deceleration. | trajectory, contact_events, initial_velocity, material_friction_label |
| [`sliding/medium_friction_slide.json`](sliding/medium_friction_slide.json) | 正向 | `sliding_crate_friction` | A crate slides across a flat floor with medium dynamic friction. It starts with horizontal velocity and slows to a stop. | trajectory, contact_events, initial_velocity, material_friction_label |
| [`sliding/negative_missing_contact.json`](sliding/negative_missing_contact.json) | 负向/边界 | `sliding_crate_friction` | A crate slides across a floor, but the runtime artifact omits support contact evidence. | trajectory, contact_events, initial_velocity, material_friction_label |
| [`sliding/negative_no_deceleration.json`](sliding/negative_no_deceleration.json) | 负向/边界 | `sliding_crate_friction` | A crate slides on a frictional floor but the runtime trace incorrectly shows almost no deceleration. | trajectory, contact_events, initial_velocity, material_friction_label |
| [`sliding/negative_static_threshold_violation.json`](sliding/negative_static_threshold_violation.json) | 负向/边界 | `sliding_crate_friction` | A crate is pushed below static-friction threshold, but the runtime trace incorrectly shows it sliding. | trajectory, contact_events, applied_force_label, material_friction_label |
| [`sliding/static_threshold_hold.json`](sliding/static_threshold_hold.json) | 正向 | `sliding_crate_friction` | A crate is pushed on a flat floor with a force below static-friction threshold, so it should remain nearly stationary. | trajectory, contact_events, applied_force_label, material_friction_label |
| [`soft_body/cloth_drape/v001_taichi_cloth_over_sphere.json`](soft_body/cloth_drape/v001_taichi_cloth_over_sphere.json) | 正向 | `soft_body_deformation` | A square fabric sheet falls under gravity onto an off-centre rigid sphere, wraps around it, and settles against the floor without stretching excessively or passing through the collider. | vertex_positions, vertex_velocities, triangle_topology, solver_timebase, surface_mesh_sequence, rgb, … |
| [`soft_body/elastic_collision/v001_youngs_modulus_ofat/high_e200k.json`](soft_body/elastic_collision/v001_youngs_modulus_ofat/high_e200k.json) | 正向 | `soft_body_deformation` | A deformable rubber sphere is released from rest above a rigid floor, compresses on impact, rebounds under its own FEM dynamics, and settles without being driven by scripted trajectories. | vertex_positions, vertex_velocities, tetrahedral_topology, surface_triangle_topology, solver_timebase, floor_contact, … |
| [`soft_body/elastic_collision/v001_youngs_modulus_ofat/low_e50k.json`](soft_body/elastic_collision/v001_youngs_modulus_ofat/low_e50k.json) | 正向 | `soft_body_deformation` | A deformable rubber sphere is released from rest above a rigid floor, compresses on impact, rebounds under its own FEM dynamics, and settles without being driven by scripted trajectories. | vertex_positions, vertex_velocities, tetrahedral_topology, surface_triangle_topology, solver_timebase, floor_contact, … |
| [`soft_body/elastic_collision/v001_youngs_modulus_ofat/mid_e100k.json`](soft_body/elastic_collision/v001_youngs_modulus_ofat/mid_e100k.json) | 正向 | `soft_body_deformation` | A deformable rubber sphere is released from rest above a rigid floor, compresses on impact, rebounds under its own FEM dynamics, and settles without being driven by scripted trajectories. | vertex_positions, vertex_velocities, tetrahedral_topology, surface_triangle_topology, solver_timebase, floor_contact, … |
| [`soft_body/flag_wind/v001_taichi_pinned_flag_wind.json`](soft_body/flag_wind/v001_taichi_pinned_flag_wind.json) | 正向 | `soft_body_deformation` | A rectangular fabric flag is fixed along its left edge to a rigid pole and deforms freely under gravity and a gusting crosswind without detaching or stretching excessively. | vertex_positions, vertex_velocities, triangle_topology, pinned_vertex_indices, wind_state, solver_timebase, … |
| [`soft_body/flag_wind/v002_wind_speed_ofat/high_wind_6p5.json`](soft_body/flag_wind/v002_wind_speed_ofat/high_wind_6p5.json) | 正向 | `soft_body_deformation` | A rectangular fabric flag is fixed along its left edge to a rigid pole and deforms freely under gravity and a gusting crosswind without detaching or stretching excessively. | vertex_positions, vertex_velocities, triangle_topology, pinned_vertex_indices, wind_state, solver_timebase, … |
| [`soft_body/flag_wind/v002_wind_speed_ofat/low_wind_3p0.json`](soft_body/flag_wind/v002_wind_speed_ofat/low_wind_3p0.json) | 正向 | `soft_body_deformation` | A rectangular fabric flag is fixed along its left edge to a rigid pole and deforms freely under gravity and a gusting crosswind without detaching or stretching excessively. | vertex_positions, vertex_velocities, triangle_topology, pinned_vertex_indices, wind_state, solver_timebase, … |
| [`spin/high_damping_spin_decay.json`](spin/high_damping_spin_decay.json) | 正向 | `angular_damping_spin_decay` | A spinning rigid body starts with a high angular velocity and strong angular damping. Its angular speed should decay clearly while the rotation trace shows visible spin. | trajectory, rotation_trace, angular_velocity, angular_damping_label |
| [`spin/low_damping_spin_decay.json`](spin/low_damping_spin_decay.json) | 正向 | `angular_damping_spin_decay` | A spinning rigid body has lower angular damping, so the angular speed decays modestly but still decreases over the trace. | trajectory, rotation_trace, angular_velocity, angular_damping_label |
| [`spin/negative_missing_angular_velocity_label.json`](spin/negative_missing_angular_velocity_label.json) | 负向/边界 | `angular_damping_spin_decay` | A body rotates visually, but the case spec does not declare initial angular velocity or damping labels. | trajectory, rotation_trace, angular_velocity, angular_damping_label |
| [`spin/negative_no_spin_decay.json`](spin/negative_no_spin_decay.json) | 负向/边界 | `angular_damping_spin_decay` | A spinning body declares angular damping but the runtime angular velocity barely decays. | trajectory, rotation_trace, angular_velocity, angular_damping_label |
| [`spin/negative_spin_gain.json`](spin/negative_spin_gain.json) | 负向/边界 | `angular_damping_spin_decay` | A spinning body has positive damping but its angular speed increases without an external torque label. | trajectory, rotation_trace, angular_velocity, angular_damping_label |
| [`templates/agent_rigidbody_action.template.json`](templates/agent_rigidbody_action.template.json) | 模板 | `agent_rigidbody_action_coupling` | - | agent action trace is explicit, target rigid body starts still, target motion starts after action frame, push actions have contact evidence, throw actions have impulse or release metadata, pick/place actions have ordered grasp/release evidence, … |
| [`templates/angular_damping_spin.template.json`](templates/angular_damping_spin.template.json) | 模板 | `angular_damping_spin_decay` | - | initial angular velocity is explicit, angular damping is explicit, angular speed decays over time, rotation trace shows non-trivial spin, spin speed cannot increase without external torque |
| [`templates/billiards_collision.template.json`](templates/billiards_collision.template.json) | 模板 | `rigid_body_contact_causality` | Parameterized rigid-body billiards-style collision. Passive balls must remain still until contact. | no_passive_object_moves_before_contact, collision_response_occurs_after_contact, expected_collision_graph_edges_have_contact_events |
| [`templates/bounce_restitution.template.json`](templates/bounce_restitution.template.json) | 模板 | `bounce_restitution_ball` | Runnable template for restitution-controlled rigid-body rebound after ground contact. | descends_before_contact, impact_contact_required, rebound_after_contact, restitution_bounded_rebound_height |
| [`templates/bowling_pin_chain.template.json`](templates/bowling_pin_chain.template.json) | 模板 | `rigid_body_contact_causality` | - | bowling ball is the only active striker, pins are passive at frame 0, pin motion is caused by ball or previous pin contact, collision graph edges are exported as contact events |
| [`templates/brittle_impact_fracture.template.json`](templates/brittle_impact_fracture.template.json) | 模板 | `brittle_impact_fracture` | Parameterized brittle/destructible impact-fracture cases. Fracture is allowed only after contact energy exceeds the declared threshold. | fracture_requires_contact, fracture_requires_threshold_energy, fragments_require_fracture_event, fragment_count_above_minimum |
| [`templates/constraint_momentum_transfer.template.json`](templates/constraint_momentum_transfer.template.json) | 模板 | `constraint_momentum_transfer` | Parameterized template for constrained impulse-chain momentum transfer. Newton's cradle is a smoke family for ordered contact-driven transfer through constrained rigid bodies. | passive_chain_members_start_still, adjacent_contacts_are_ordered, terminal_receiver_moves_after_final_contact, intermediate_bodies_remain_displacement_bounded, energy_gain_stays_bounded |
| [`templates/domino_chain.template.json`](templates/domino_chain.template.json) | 模板 | `sequential_contact_propagation` | Parameterized domino/chain-reaction case. Passive dominoes must activate after predecessor contact. | contact_order_matches_causal_chain, no_passive_domino_activates_without_predecessor_contact, no_simultaneous_unexplained_passive_activation |
| [`templates/elastic_constraint_rebound.template.json`](templates/elastic_constraint_rebound.template.json) | 模板 | `elastic_constraint_rebound` | Parameterized template for elastic tether or bungee-style constrained rebound. Bungee is a smoke family for generic elastic-constraint verification. | constraint_trace_exists, extension_stays_below_max, minimum_stretch_is_reached, post_stretch_rebound_toward_anchor |
| [`templates/elastic_energy_launch.template.json`](templates/elastic_energy_launch.template.json) | 模板 | `elastic_energy_launch` | Parameterized template for elastic stored-energy release into rigid-body launch motion. Spring launcher is the smoke family for generic elastic-energy launch. | launched_body_starts_still, release_event_exists, post_release_speed_matches_energy_envelope, height_gain_and_forward_displacement_are_positive, energy_gain_stays_bounded |
| [`templates/falling_blocks.template.json`](templates/falling_blocks.template.json) | 模板 | `rigid_body_gravity_collision` | Parameterized gravity/contact case for falling rigid blocks. | falling_object_descends_under_gravity, support_contact_exists, no_floor_penetration |
| [`templates/magnetic_force_field.template.json`](templates/magnetic_force_field.template.json) | 模板 | `magnetic_force_field` | - | magnetic mode is explicit, magnetic strength is non-zero, attract moves radially inward, repel moves radially outward, radial displacement is bounded |
| [`templates/mass_ratio_collision.template.json`](templates/mass_ratio_collision.template.json) | 模板 | `mass_ratio_momentum_transfer` | - | passive body starts with zero velocity, active-passive contact exists, post-collision velocity direction follows collision axis, velocity ordering follows mass ratio, kinetic energy gain stays bounded |
| [`templates/pendulum_contact.template.json`](templates/pendulum_contact.template.json) | 模板 | `constraint_distance_pendulum_motion` | Parameterized template for pendulum/fixed-distance constraint motion. The pendulum bob is a smoke family for generic distance-constraint verification. | constraint_distance_preserved, swing_direction_changes_after_release, contact_response_occurs_after_contact_if_target_present |
| [`templates/projectile_motion.template.json`](templates/projectile_motion.template.json) | 模板 | `projectile_gravity_motion` | Runnable template for upward throw/projectile motion with gravity, apex, descent, and landing contact. | apex_after_launch, descent_after_apex, forward_displacement_positive, landing_contact_required |
| [`templates/ramp_sliding.template.json`](templates/ramp_sliding.template.json) | 模板 | `ramp_sliding_friction` | Runnable template for a body rolling or sliding down a ramp with friction-sensitive downhill motion. | downhill_displacement_positive, friction_changes_travel_distance, rotation_translation_coupled_if_rolling |
| [`templates/rolling_friction.template.json`](templates/rolling_friction.template.json) | 模板 | `rolling_friction_ball` | Runnable template for friction-controlled rolling on a flat support surface. | initial_horizontal_velocity_required, support_contact_required, velocity_decay_required, friction_bounded_roll_distance |
| [`templates/sliding_crate_friction.template.json`](templates/sliding_crate_friction.template.json) | 模板 | `sliding_crate_friction` | Runnable template for flat-surface sliding friction and static-friction threshold validation. | support_contact_required, dynamic_friction_deceleration, friction_bounded_stop_distance, static_threshold_hold |
| [`templates/wind_balloon_drift.template.json`](templates/wind_balloon_drift.template.json) | 模板 | `force_field_wind_drift` | - | wind vector is explicit and non-zero, horizontal drift aligns with wind direction, drift distance is bounded by expected range, altitude stays inside envelope |
| [`wind/east_wind_balloon_drift.json`](wind/east_wind_balloon_drift.json) | 正向 | `force_field_wind_drift` | A light balloon-like rigid body drifts east under a steady wind field. The wind vector is explicit; the body should move with the wind and stay within a small altitude envelope. | trajectory, wind_vector_label, force_field_label |
| [`wind/negative_missing_wind_label.json`](wind/negative_missing_wind_label.json) | 负向/边界 | `force_field_wind_drift` | A light body appears to drift, but the case spec does not declare a wind vector or force-field label. | trajectory, wind_vector_label, force_field_label |
| [`wind/negative_no_wind_drift.json`](wind/negative_no_wind_drift.json) | 负向/边界 | `force_field_wind_drift` | A wind-driven light body is expected to drift under a clear east wind, but the runtime trace barely moves it. | trajectory, wind_vector_label, force_field_label |
| [`wind/negative_wrong_direction.json`](wind/negative_wrong_direction.json) | 负向/边界 | `force_field_wind_drift` | A light body should drift east under wind, but the runtime trace moves it west. | trajectory, wind_vector_label, force_field_label |
| [`wind/northeast_wind_light_body_drift.json`](wind/northeast_wind_light_body_drift.json) | 正向 | `force_field_wind_drift` | A lightweight physics-critical body drifts diagonally under a steady northeast wind field. The trajectory should align with the wind vector rather than a fixed x-only animation. | trajectory, wind_vector_label, force_field_label |

## 维护规则

1. 新增/删除/移动 CaseSpec 后必须重新生成本文件并运行 `--check`。
2. `negative_*` 是 verifier 负例，不是待交付视频；它们必须失败才能证明关卡有效。
3. 参数矩阵只做因果方向判断，不把不同参数条件评为画质 winner。
4. 每个整理后的变体固定四个媒体目录；RGB-only 历史结果允许 depth/segmentation 为空，但 manifest 必须明确。正式完整 run 仍要求多机位 RGB/depth/segmentation 和三个 overall。
5. CaseSpec 只定义初态、物理参数和期望事件；不得逐帧注入物体轨迹。
