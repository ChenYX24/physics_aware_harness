# Physics-Aware Harness Capability Profile

This report is generated from project memory, docs, tests, and runtime code. It distills reusable harness capabilities without committing raw private sessions.

## Extraction Method

- WHAT/HOW/FLOW/BRIDGE pattern mining
- decision-chain re-anchoring
- failure taxonomy extraction
- capability quality checklist

## 能力抽象原则

- capability id 必须命名可复用的不变量或 pipeline 阶段，不能命名成某个单独场景模板。
- `billiard_causality_compiler` 不作为 active capability，也不作为 capability JSON 发布；它只是旧 artifact 的 deprecated alias。
- 台球、保龄球、箱体撞击等都属于 `rigid_body_contact_causality` 的 case family。
- 资产能力拆成 `asset_intent_resolution` 和 `asset_runtime_binding_invocation`：先检索/筛选，再绑定 runtime actor。
- 物理能力必须绑定 verifier invariant、required signals、failure taxonomy 和 repair suggestions。

## Capability Taxonomy

| Layer | Capability IDs |
|---|---|
| `pipeline_execution_order` | `prompt_case_capability_planning`, `asset_intent_resolution`, `scene_spec_compilation`, `static_scene_placement`, `asset_runtime_binding_invocation`, `runtime_actor_placement_compilation`, `runtime_backend_execution`, `capability_runtime_artifact_bridge`, `canonical_signal_capture`, `render_signal_sync_validation`, `physics_verifier_truth_gate`, `dataset_artifact_packaging`, `pipeline_stage_orchestration` |
| `pipeline_stage_capabilities` | `prompt_case_capability_planning`, `scene_spec_compilation`, `static_scene_placement`, `runtime_actor_placement_compilation`, `runtime_backend_execution`, `pipeline_stage_orchestration`, `capability_runtime_artifact_bridge`, `canonical_signal_capture`, `dataset_artifact_packaging` |
| `asset_operation_capabilities` | `asset_intent_resolution`, `asset_runtime_binding_invocation` |
| `runtime_bridge_capabilities` | `capability_runtime_artifact_bridge`, `canonical_signal_capture` |
| `physical_property_constraint_capabilities` | `explicit_physics_control_surface`, `physics_property_constraint_validation` |
| `physics_behavior_capabilities` | `rigid_body_contact_causality`, `rigid_body_gravity_collision`, `sequential_contact_propagation`, `ramp_sliding_friction`, `projectile_gravity_motion`, `bounce_restitution_ball`, `rolling_friction_ball`, `sliding_crate_friction`, `force_field_wind_drift`, `magnetic_force_field`, `mass_ratio_momentum_transfer`, `angular_damping_spin_decay`, `agent_rigidbody_action_coupling`, `constraint_distance_pendulum_motion`, `constraint_momentum_transfer`, `elastic_energy_launch`, `elastic_constraint_rebound`, `brittle_impact_fracture` |
| `verification_capabilities` | `physics_verifier_truth_gate`, `render_signal_sync_validation` |
| `dataset_packaging_capabilities` | `dataset_artifact_packaging` |
| `deprecated_aliases` | `billiard_causality_compiler` -> `rigid_body_contact_causality` |

## Capability Summary

### Prompt To Object-Level Scene Compiler

- id: `prompt_case_capability_planning`
- pattern: `FLOW`
- stages: `planner, scene_spec, asset_resolution`
- confidence: `0.95` from `35` matched lines

Compile natural language into explicit objects, roles, asset intents, camera needs, lighting, and runtime scene requests instead of a free-form video description.

Key iteration moves:
- Add missing object roles to prompt expansion.
- Inspect asset_requests.json and runtime_scene_request.json before rerendering.
- Rerun planner with stricter object-id and role constraints.

Evidence:
- `tools/capability_verifier.py:203`: "F1_scene_parsing_failure": "repair prompt expansion and object roles before execution",
- `README.md:16`: -> asset intent resolution
- `README.md:180`: | `scene_spec_compilation` | Builds runtime scene contracts from capability/case/assets. |

### Static Scene Placement Preflight

- id: `static_scene_placement`
- pattern: `FLOW`
- stages: `asset_resolution, static_scene_layout, camera_planning, preflight_validation`
- confidence: `0.95` from `139` matched lines

Compile case objects and asset selections into a pre-runtime static layout with object nodes, support relations, overlap checks, physics graph membership, and camera coverage.

Key iteration moves:
- Run scripts/harness_build_static_scene.py on the case before runtime.
- Adjust case object positions or support surfaces if static_scene_report.json fails.
- Update asset registry physics metadata before retrying UE actor placement.

Evidence:
- `docs/CAPABILITY_SYSTEM.md:18`: | Static scene preflight | `static_scene_placement` | case spec, asset resolution | `scene_layout.json`, object nodes, support relations, non-overlap report, camera plan |
- `harness/verification/static_scene_verifier.py:36`: return fail_report(case_id, "F3_invalid_initial_physics_state", "initial_overlap_pair", overlap_pairs[0], checks)
- `README.md:181`: | `static_scene_placement` | Validates object ids, transforms, support relations, non-overlap, camera coverage, and physics graph membership before runtime. |

### Generic Rigid-Body Contact Causality

- id: `rigid_body_contact_causality`
- pattern: `FLOW`
- stages: `capability_planning, case_spec_compilation, scene_spec_compilation, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `547` matched lines

Compile active-to-passive rigid-body contact scenes into causal simulation programs where passive bodies move only after runtime contact evidence exists.

Key iteration moves:
- Inspect physics.json for passive initial velocities.
- Inspect first trajectory frames for target speed before first active contact.
- Adjust radius/extent and initial spacing before increasing speed.

Evidence:
- `tools/capability_planner.py:27`: terms=("billiard", "billiards", "pool", "cue ball", "cue_ball", "bowling", "pins", "rigid-body impact", "rigid body impact", "crate impact", "mass ratio collision", "台球", "白球", "目标球", "保龄球", "球瓶", "刚体碰撞", "ball collisio…
- `scripts/harness_generate_cases.py:253`: "prompt": f"Generated billiards collision with one cue ball and {target_count} passive target balls.",
- `tools/capability_closed_loop.py:19`: "prompt": "Create a billiards / pool scene where one cue ball hits a compact rack of passive target balls. Targets must stay still until contact.",

### Magnetic Attract/Repel Force Field

- id: `magnetic_force_field`
- pattern: `physics_constraint`
- stages: `case_spec_compilation, physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `66` matched lines

Validate magnetic attraction or repulsion using explicit source/subject/mode/strength labels and source-relative radial trajectory evidence.

Key iteration moves:
- Run magnetic golden and negative cases before tuning visuals.
- Inspect radial distance evidence in verifier_report.json.
- Fix magnetic labels and runtime force direction before changing assets.

Evidence:
- `docs/CAPABILITY_SYSTEM.md:59`: | `magnetic_force_field` | 磁吸/排斥必须声明 source、subject、mode、strength，并按径向距离变化验证。 | 磁吸球、磁排斥体 |
- `capabilities/magnetic_force_field.json:44`: "Write `expected_physics.magnetic_mode`, `source_object_id`, `magnetic_subject_id`, and `magnetic_strength` before runtime.",
- `cases/magnetic/attract_magnetic_body.json:22`: "required_assets": ["magnetic_source", "magnetized_body"],

### Asset Intent Resolution

- id: `asset_intent_resolution`
- pattern: `HOW`
- stages: `asset_intent_resolution, asset_retrieval`
- confidence: `0.95` from `528` matched lines

Classify object-level asset needs into typed intents and retrieve top-k candidates before runtime binding.

Key iteration moves:
- Rebuild asset registry or physics index after changing asset sources.
- Audit top-k candidates before runtime binding.
- Add role-specific tags for repeated physical object families.

Evidence:
- `docs/CAPABILITY_SYSTEM.md:34`: Physics-critical asset 必须有 collider、mass、rigid body、collision profile。Visual-only asset 可以被替换或随机化，但不能进入 physics graph。
- `capabilities/asset_intent_resolution.json:6`: "Physics-critical assets require collider, mass, rigid body, and collision profile.",
- `capabilities/static_scene_placement.json:25`: "Physics-critical assets have collider, mass/material, and collision profile.",

### Asset Runtime Binding Invocation

- id: `asset_runtime_binding_invocation`
- pattern: `HOW`
- stages: `asset_runtime_binding, runtime_actor_binding, preflight_validation`
- confidence: `0.95` from `322` matched lines

Bind selected real assets or analytic proxies into runtime actors while preserving collider, mass, material, and collision-profile metadata.

Key iteration moves:
- Fix asset registry metadata before changing physics verifier thresholds.
- Use analytic proxy only for smoke/debug and mark proxy=true.
- Add binding audit output for missing collider, wrong mass, wrong constraint, and missing binding.

Evidence:
- `docs/CAPABILITY_SYSTEM.md:32`: | `asset_runtime_binding_invocation` | 把 selected asset 或 analytic proxy 绑定到 runtime actor。 | object_id, runtime_actor_id, collider, mass, material, collision_profile, proxy flag |
- `docs/PHYSICS_AWARE_HARNESS.md:221`: | `asset_runtime_binding_invocation` | physics-critical assets must bind colliders, mass/material metadata, collision profile, and runtime actor ids |
- `docs/CAPABILITY_AUTHORING.md:37`: - 资产能力要区分 retrieval 和 invocation：`asset_intent_resolution` 找候选，`asset_runtime_binding_invocation` 负责把 selected asset/proxy 绑定到 runtime actor。

### Rigid Body Gravity Collision

- id: `rigid_body_gravity_collision`
- pattern: `FLOW`
- stages: `planner, physics_control, runtime, verifier`
- confidence: `0.95` from `211` matched lines

Compile falling or stacking prompts into gravity-driven rigid-body traces where objects descend under gravity and collide with a support surface instead of being visually animated.

Key iteration moves:
- Fix initial height and support plane before changing material properties.
- Check trajectory z-series and contact pairs.
- Tune mass, restitution, damping, and solver substeps after gravity/contact validity passes.

Evidence:
- `tools/capability_planner.py:33`: terms=("falling block", "falling blocks", "falling crate", "falling object", "stacking", "gravity", "下落", "坠落", "堆叠", "重力", "落到地面"),
- `tests/test_capability_closed_loop.py:52`: plan = self.planner.plan("Falling blocks under gravity collide with the ground.")
- `tools/capability_closed_loop.py:23`: "prompt": "Create falling blocks under gravity. Rigid bodies fall and collide with the ground and each other; the motion must not be a visual-only animation.",

### Sequential Contact Propagation

- id: `sequential_contact_propagation`
- pattern: `FLOW`
- stages: `planner, physics_control, runtime, verifier`
- confidence: `0.95` from `97` matched lines

Compile domino or chain-reaction prompts into ordered contact propagation where only the first body is actively triggered and later bodies move after upstream contact.

Key iteration moves:
- Check initial angular velocity on all non-first objects.
- Inspect adjacent contact pair coverage.
- Adjust spacing, mass, friction, and angular damping after sequence validity passes.

Evidence:
- `tools/capability_planner.py:39`: terms=("domino", "dominoes", "chain reaction", "sequential contact", "contact propagation", "多米诺", "连锁反应", "链式碰撞", "依次倒下"),
- `tools/capability_closed_loop.py:27`: "prompt": "Create a domino chain reaction. The first domino is actively triggered and the later dominoes tip only through sequential contact propagation.",
- `scripts/harness_generate_cases.py:327`: "prompt": f"Generated domino chain with {count} dominoes and sequential contact propagation.",

### Explicit Physics Control Surface

- id: `explicit_physics_control_surface`
- pattern: `HOW`
- stages: `planner, physics_control, benchmark`
- confidence: `0.95` from `595` matched lines

Represent gravity, material, rigid-body, constraint, force, time, agent, and render-physics bridge controls as typed fields that can be replayed and swept.

Key iteration moves:
- Run baseline, implicit, and structured-control variants.
- Sweep one parameter across fixed seeds.
- Compare reproduction variance and verifier pass rate.

Evidence:
- `README.md:184`: | `physics_property_constraint_validation` | Checks mass, friction, restitution, damping, gravity, material, and parameter-sweep constraints. |
- `capabilities/physics_property_constraint_validation.json:4`: "description": "Validate physical property ranges and parameter sensitivity for mass, friction, restitution, damping, gravity, material density, fracture threshold, buoyancy, and force-field controls.",
- `docs/CAPABILITY_SYSTEM.md:41`: | `physics_property_constraint_validation` | 检查 mass、friction、restitution、damping、gravity、material density、fracture threshold 等字段范围和 sensitivity 方向。 |

### Canonical Multi-Signal Capture

- id: `canonical_signal_capture`
- pattern: `FLOW`
- stages: `runtime, signals, dataset`
- confidence: `0.95` from `589` matched lines

Capture video and aligned evidence streams on one timebase: RGB, trajectory, contacts, camera path, depth proxy, normal proxy, audio, engine states, and semantic labels.

Key iteration moves:
- Run pass quality report before dataset packaging.
- Add missing view or signal manifests before rerendering a full suite.
- Keep camera/action/initial_state fixed for control sensitivity runs.

Evidence:
- `capabilities/canonical_signal_capture.json:10`: "description": "Collect RGB, depth, segmentation, trajectory, contact events, camera trajectory, and engine-state labels on a single deterministic timebase.",
- `README.md:187`: | `canonical_signal_capture` | Keeps trajectory, contacts, camera paths, RGB/depth/segmentation, and render metadata on one timebase. |
- `capabilities/render_signal_sync_validation.json:4`: "description": "Validate that RGB, depth, segmentation, camera trajectory, and physics trace outputs are frame-aligned and complete for every planned view.",

### Verifier As Runtime Truth Gate

- id: `physics_verifier_truth_gate`
- pattern: `HOW`
- stages: `verifier, dataset`
- confidence: `0.95` from `255` matched lines

Use verifier output, not UI preview or successful rendering, as the final readiness decision for reference-ready samples.

Key iteration moves:
- Use verifier failure type to decide whether to replan, rebind assets, or rerun runtime.
- Promote only passing artifacts into dataset shards.
- Track verifier pass rate across seeds and prompt variants.

Evidence:
- `tests/test_capability_runtime_adapter.py:75`: write_json(output_dir / "run_readiness.json", {"passed": True, "reference_ready": True, "physics_ready": True, "visual_ready": True})
- `tests/test_capability_runtime_adapter.py:134`: write_json(output_dir / "run_readiness.json", {"passed": True, "reference_ready": True, "physics_ready": True, "visual_ready": True})
- `docs/AGENT_USAGE.md:107`: - `run_readiness.reference_ready=true`: both rendering and physics verifier gates passed.

### Capability Runtime Artifact Bridge

- id: `capability_runtime_artifact_bridge`
- pattern: `BRIDGE`
- stages: `runtime, signals, verifier`
- confidence: `0.95` from `109` matched lines

Convert existing UE or fallback run artifacts into the capability verifier trace contract so real runtime evidence can be checked with the same causality rules as deterministic dry runs.

Key iteration moves:
- Run scripts/verify_capability_run.py on an existing run.
- Inspect capability_verifier.json before changing prompt or scene parameters.
- Use capability_diagnosis.md to decide whether the issue is planning, runtime, evidence, or verifier.

Evidence:
- `harness/verification/physics_verifier.py:125`: for name in ("fallback_output", "ue_output", "debug_preview"):
- `tests/test_capability_runtime_adapter.py:8`: from tools.capability_runtime_adapter import CapabilityRuntimeAdapter, resolve_runtime_output_dir, verify_capability_run
- `tests/test_capability_runtime_adapter.py:12`: def test_resolves_ue_output_before_debug_preview(self) -> None:

### Trajectory-Based Benchmark Iteration

- id: `trajectory_benchmark_iteration`
- pattern: `FLOW`
- stages: `benchmark, runtime, verifier`
- confidence: `0.95` from `141` matched lines

Evaluate real runtime trajectories across seed sweeps and variants instead of dry control signals or mixed-backend stability numbers.

Key iteration moves:
- Run full pipeline with seed sweep.
- Inspect coverage, signal purity, and stability reports.
- Cluster failures before changing planner or schema.

Evidence:
- `README.md:221`: The old billiards failure mode is still preserved as a regression: plausible
- `README.md:36`: | `tests/` | Regression tests for CLI, capabilities, verifier, render sync, artifacts. |
- `README.md:76`: --seed 42 \

### Angular Damping Spin Decay

- id: `angular_damping_spin_decay`
- pattern: `physics_constraint`
- stages: `physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `16` matched lines

Validate rotational damping for spinning rigid bodies using explicit angular velocity, angular damping labels, rotation trace, and monotonic spin-decay evidence.

Key iteration moves:
- Move spin controls into initial_angular_velocity_deg_s and expected_physics.angular_damping.
- Bind spinning bodies as physics-critical assets with inertia/damping metadata.
- Add negative cases for missing labels, no decay, and unexplained spin gain.

Evidence:
- `tools/capability_planner.py:99`: terms=("angular damping", "spin decay", "spinning body", "angular velocity", "rotational damping", "spin slows", "spin down", "角阻尼", "角速度", "旋转衰减", "自转", "旋转变慢"),
- `capabilities/angular_damping_spin_decay.json:10`: "description": "Validate rotational damping for spinning rigid bodies using explicit angular velocity, angular damping labels, rotation trace, and monotonic spin-decay evidence.",
- `README.md:206`: | `angular_damping_spin_decay` | Spinning rigid bodies must declare angular velocity and damping, then show monotonic spin decay in angular velocity and rotation trace evidence. |

### Agent Action To Rigid-Body Coupling

- id: `agent_rigidbody_action_coupling`
- pattern: `physics_constraint`
- stages: `action_trace_planning, physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.8` from `9` matched lines

Validate that agent or controller actions cause rigid-body motion through explicit action trace, contact or impulse evidence, and post-action trajectory response.

Key iteration moves:
- Add action_trace to expected_physics or runtime artifacts.
- Bind the target as a physics-critical rigid body and the agent as an action-producing actor.
- Use negative cases for pre-action motion, missing action trace, and no post-action response.

Evidence:
- `tools/capability_planner.py:105`: terms=("agent pushes", "agent push", "robot pushes", "robot push", "character pushes", "character throws", "agent throws", "agent throw", "robot throws", "action trace", "agent-to-rigidbody", "agent rigid body", "推箱子",…
- `README.md:207`: | `agent_rigidbody_action_coupling` | Agent or controller actions must be explicit action traces, and target rigid bodies may move only after action/contact or release/impulse evidence. |
- `capabilities/agent_rigidbody_action_coupling.json:11`: "description": "Validate that agent or controller actions cause rigid-body motion through an explicit action trace, contact/impulse evidence, and post-action trajectory response. Pushing a box or throwing a ball are smo…

### Distance Constraint Motion Validation

- id: `constraint_distance_pendulum_motion`
- pattern: `physics_constraint`
- stages: `case_spec_compilation, physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `42` matched lines

Validate fixed-distance or joint-constrained rigid-body motion using anchor/body trajectory, constraint length labels, constraint trace, and continuity checks.

Key iteration moves:
- Export constraint_trace before tuning rope visuals.
- Fix constraint_length_m, anchor/body ids, and solver timestep before changing camera or materials.
- Add negative cases for missing constraint label, length drift, and teleporting body.

Evidence:
- `tools/capability_planner.py:111`: terms=("pendulum", "swinging pendulum", "distance constraint", "fixed length", "constraint length", "joint constraint", "rope constraint", "hinge constraint", "单摆", "摆锤", "距离约束", "固定长度", "约束长度", "铰链约束", "绳长约束"),
- `capabilities/constraint_distance_pendulum_motion.json:10`: "description": "Validate fixed-distance constraint motion such as pendulums using anchor/body trajectory, constraint length labels, and continuity checks. A pendulum bob is only the smoke family.",
- `tests/test_capability_closed_loop.py:60`: plan = self.planner.plan("A pendulum swings while preserving a fixed length rope constraint.")

### Constrained Impulse Chain Transfer

- id: `constraint_momentum_transfer`
- pattern: `physics_constraint`
- stages: `case_spec_compilation, physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `12` matched lines

Validate ordered contact-driven impulse and momentum transfer through a chain of constrained rigid bodies.

Key iteration moves:
- Fix chain_objects and expected_contact_chain before changing visuals.
- Check passive initial velocities before tuning restitution.
- Add negative cases for pre-chain motion, terminal no-response, and contact order violations.

Evidence:
- `tools/capability_planner.py:117`: terms=("newton cradle", "newton's cradle", "impulse chain", "constrained impulse", "momentum chain", "chain momentum transfer", "suspended ball chain", "constraint momentum transfer", "牛顿摆", "冲量链", "动量链", "悬挂球", "受约束动量传…
- `capabilities/constraint_momentum_transfer.json:11`: "description": "Validate constrained impulse-chain momentum transfer across adjacent rigid bodies. Newton's cradle is only the smoke family; the reusable invariant is ordered contact-driven transfer through constrained…
- `docs/CAPABILITY_AUTHORING.md:52`: | `newton_cradle_template` 作为主能力 | `constraint_momentum_transfer`，牛顿摆/悬挂球链作为 case family |

### Elastic Energy Launch

- id: `elastic_energy_launch`
- pattern: `physics_constraint`
- stages: `case_spec_compilation, physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `40` matched lines

Validate stored elastic energy release into launched rigid-body motion using explicit release events, spring parameters, payload mass, and bounded kinetic-energy response.

Key iteration moves:
- Fix spring_constant, compression, payload mass, and release event before tuning camera.
- Inspect spring_events.json and trajectory frames around release.
- Add negative cases for missing release, no launch response, and energy gain.

Evidence:
- `tools/capability_planner.py:123`: terms=("spring launch", "spring launcher", "compressed spring", "elastic launch", "elastic energy", "catapult", "弹簧", "弹簧发射", "压缩弹簧", "弹射", "弹性势能"),
- `docs/CAPABILITY_SYSTEM.md:64`: | `elastic_energy_launch` | stored elastic energy 通过 release event 转成 bounded kinetic response。 | 弹簧发射、弹射器 |
- `scripts/harness_generate_cases.py:1044`: "prompt": f"Generated elastic launch case: a compressed spring releases {stored_energy:.2f} J into a payload at {launch_angle:.1f} degrees.",

### Elastic Constraint Rebound

- id: `elastic_constraint_rebound`
- pattern: `physics_constraint`
- stages: `case_spec_compilation, physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `60` matched lines

Validate elastic tether or bungee-style constrained motion using rest length, bounded extension, constraint trace, and rebound velocity toward the anchor.

Key iteration moves:
- Fix rest_length_m, max_extension_m, stiffness, and damping before tuning visuals.
- Inspect constraint_trace.json around maximum extension.
- Add negative cases for missing trace, overstretch, and no rebound.

Evidence:
- `tools/capability_planner.py:129`: terms=("bungee", "elastic rope", "elastic tether", "stretchy rope", "rope rebound", "tether rebound", "elastic constraint", "蹦极", "弹性绳", "弹力绳", "弹性约束", "绳子回弹", "拉伸回弹"),
- `docs/CAPABILITY_AUTHORING.md:54`: | `bungee_template` 作为主能力 | `elastic_constraint_rebound`，蹦极/弹性绳作为 case family |
- `docs/CAPABILITY_SYSTEM.md:65`: | `elastic_constraint_rebound` | 弹性约束必须记录 extension，并在峰值后朝 anchor 回弹。 | 蹦极、弹力绳 |

### Brittle Impact Fracture

- id: `brittle_impact_fracture`
- pattern: `physics_constraint`
- stages: `case_spec_compilation, physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `175` matched lines

Validate brittle/destructible fracture as a contact-energy threshold invariant with explicit fracture events and fragment evidence.

Key iteration moves:
- Export impact_energy_j and fracture_events before tuning visual debris.
- Adjust threshold/energy labels before changing mesh materials.
- Use negative cases for missing event, pre-contact fracture, below-threshold fracture, and too few fragments.

Evidence:
- `tools/capability_planner.py:93`: terms=("brittle", "fracture", "shatter", "breakable", "destructible", "glass break", "glass shatter", "mirror breaks", "wood crate breaks", "crate breaks", "破碎", "碎裂", "断裂", "玻璃碎", "镜子碎", "玻璃杯碎", "木箱破碎", "可破坏"),
- `README.md:212`: | `brittle_impact_fracture` | Brittle/destructible bodies must declare fracture threshold, contact impact energy, fracture events, and fragment evidence. Glass panels, mirrors, cups, and crates are case families. |
- `capabilities/brittle_impact_fracture.json:4`: "description": "Validate brittle or destructible rigid-body fracture as a contact-energy threshold invariant rather than a glass-specific visual template.",

### Verified Multi-View Dataset Packaging

- id: `dataset_artifact_packaging`
- pattern: `FLOW`
- stages: `dataset, signals, verifier`
- confidence: `0.95` from `157` matched lines

Package only readiness-gated runs into a dataset layout with video, synchronized signals, physics labels, asset metadata, and hashes.

Key iteration moves:
- Run package_dataset.py after verifier updates.
- Audit sample tiers before publishing.
- Use benchmark taxonomy to grow cases systematically.

Evidence:
- `README.md:163`: - `scene_capture` RGB/depth/segmentation multi-view path is the stable data path.
- `README.md:188`: | `render_signal_sync_validation` | Validates RGB/depth/segmentation/camera/physics alignment and fails missing views or placeholder passes. |
- `capabilities/canonical_signal_capture.json:30`: "Every planned view has RGB/depth/segmentation metadata or a hard failure.",

### Failure-Driven Replanning And Refinement

- id: `failure_driven_refinement_loop`
- pattern: `BRIDGE`
- stages: `planner, runtime, verifier, benchmark`
- confidence: `0.95` from `399` matched lines

Use structured failure evidence to decide the next minimal change: prompt expansion, asset binding, scene layout, physics controls, runtime settings, or verifier thresholds.

Key iteration moves:
- Mine failure evidence from verifier and trajectory.
- Change exactly one stage owner file or prompt contract.
- Rerun tests and a small prompt suite before publishing.

Evidence:
- `docs/PHYSICS_AWARE_HARNESS.md:244`: 每个失败必须归因到 failure type，例如：
- `harness/verification/physics_verifier.py:48`: "value": str(ue_backend_report.get("failure_message") or "UE backend failed"),
- `tools/capability_runtime_adapter.py:319`: lines.append(f"- `{failure.get('failure_type')}`: {failure.get('reason')}")

### Lineage-Based Harness Capability Extraction

- id: `lineage_based_capability_extraction`
- pattern: `BRIDGE`
- stages: `meta, docs, tests`
- confidence: `0.95` from `30` matched lines

Mine repeated project memory, reports, and final artifacts into reusable harness capabilities without publishing raw private sessions or secrets.

Key iteration moves:
- Run local extraction from memory and reports.
- Run public extraction from README, docs, tests, and code.
- Diff the capability profile after new benchmark or prompt iterations.

Evidence:
- `capabilities/dataset_artifact_packaging.json:10`: "description": "Package only verifier-gated runtime artifacts into dataset-ready sample layouts with lineage, hashes, signal availability, and failure visibility.",
- `docs/CAPABILITY_SYSTEM.md:25`: | Full orchestration | `pipeline_stage_orchestration` | stage artifacts | staged lineage and failure attribution |
- `README.md:10`: ## What Is Included

### Bounce Restitution Ball

- id: `bounce_restitution_ball`
- pattern: `physics_constraint`
- stages: `physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.75` from `1` matched lines

Validate restitution-controlled rigid-body rebound after impact. The smoke family uses a ball dropping onto a support surface, but the invariant applies to any bouncing rigid body.

Key iteration moves:
- Enable gravity and collision for the bouncing body.
- Bind a support collider and export contact events.
- Move restitution into structured physical_parameters or expected_physics.

Evidence:
- `capabilities/bounce_restitution_ball.json`: Validate restitution-controlled rigid-body rebound after impact. The smoke family uses a ball dropping onto a support surface, but the invariant applies to any bouncing rigid body.

### Force Field Wind Drift

- id: `force_field_wind_drift`
- pattern: `physics_constraint`
- stages: `physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.75` from `1` matched lines

Validate wind or force-field driven drift for light rigid bodies or buoyant objects using explicit wind vector labels and trajectory displacement evidence. The balloon case is only a smoke family.

Key iteration moves:
- Move wind speed and direction into `expected_physics.wind_vector_m_s` or `physics_control.force_field.wind`.
- Bind the wind subject as a physics-critical light body, not a visual-only sprite.
- Check that runtime exports force-field labels and trajectory frames before tuning wind speed.

Evidence:
- `capabilities/force_field_wind_drift.json`: Validate wind or force-field driven drift for light rigid bodies or buoyant objects using explicit wind vector labels and trajectory displacement evidence. The balloon case is only a smoke family.

### Mass Ratio Momentum Transfer

- id: `mass_ratio_momentum_transfer`
- pattern: `physics_constraint`
- stages: `physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.75` from `1` matched lines

Validate one-dimensional rigid-body momentum transfer across a contact event using explicit mass ratio, restitution, and post-collision velocity evidence. The two-body collision is only a smoke family.

Key iteration moves:
- Declare mass_kg on both colliding bodies.
- Remove hidden initial velocity from passive receivers.
- Export the active-passive contact event before checking post-collision velocities.

Evidence:
- `capabilities/mass_ratio_momentum_transfer.json`: Validate one-dimensional rigid-body momentum transfer across a contact event using explicit mass ratio, restitution, and post-collision velocity evidence. The two-body collision is only a smoke family.

### Physics Property Constraint Validation

- id: `physics_property_constraint_validation`
- pattern: `physics_constraint`
- stages: `physics_control, case_spec_validation, sensitivity_validation`
- confidence: `0.75` from `1` matched lines

Validate physical property ranges and parameter sensitivity for mass, friction, restitution, damping, gravity, material density, fracture threshold, buoyancy, and force-field controls.

Key iteration moves:
- Move physical values into `physical_parameters` and object fields.
- Add material profile entries for friction/restitution/density.
- Add generated sensitivity cases for the parameter under test.

Evidence:
- `capabilities/physics_property_constraint_validation.json`: Validate physical property ranges and parameter sensitivity for mass, friction, restitution, damping, gravity, material density, fracture threshold, buoyancy, and force-field controls.

### Pipeline Stage Orchestration

- id: `pipeline_stage_orchestration`
- pattern: `pipeline_stage`
- stages: `capability_planning, case_spec_compilation, asset_resolution, runtime_execution, verification, packaging`
- confidence: `0.75` from `1` matched lines

Coordinate the harness stages from prompt/case planning through scene layout, asset binding, runtime execution, artifact collection, verification, diagnosis, and dataset packaging.

Key iteration moves:
- Rerun from the failed stage using the previous artifact as input.
- Inspect verifier_report.json and run_readiness.json before modifying runtime code.
- Keep generated cases and runs out of public git commits.

Evidence:
- `capabilities/pipeline_stage_orchestration.json`: Coordinate the harness stages from prompt/case planning through scene layout, asset binding, runtime execution, artifact collection, verification, diagnosis, and dataset packaging.

### Projectile Gravity Motion

- id: `projectile_gravity_motion`
- pattern: `physics_constraint`
- stages: `physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.75` from `1` matched lines

Compile upward-throw/projectile prompts into gravity-driven trajectories with apex, descent, and landing contact evidence.

Key iteration moves:
- Set a structured initial velocity instead of keyframing the path.
- Enable gravity on the projectile.
- Add ground collider and contact event capture.

Evidence:
- `capabilities/projectile_gravity_motion.json`: Compile upward-throw/projectile prompts into gravity-driven trajectories with apex, descent, and landing contact evidence.

### Ramp Sliding Friction

- id: `ramp_sliding_friction`
- pattern: `physics_constraint`
- stages: `physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.75` from `1` matched lines

Compile inclined-plane prompts into deterministic ramp rolling/sliding cases with friction-sensitive downhill motion evidence.

Key iteration moves:
- Ensure the ramp collider is physics-critical and participates in contact events.
- Check the downhill axis and coordinate system labels.
- Bind dynamic friction to the subject/ramp physics material.

Evidence:
- `capabilities/ramp_sliding_friction.json`: Compile inclined-plane prompts into deterministic ramp rolling/sliding cases with friction-sensitive downhill motion evidence.

### Render Signal Sync Validation

- id: `render_signal_sync_validation`
- pattern: `verification`
- stages: `signal_capture, render_sync_validation, physics_verification`
- confidence: `0.75` from `1` matched lines

Validate that RGB, depth, segmentation, camera trajectory, and physics trace outputs are frame-aligned and complete for every planned view.

Key iteration moves:
- Inspect render_sync_report.json before changing physics parameters.
- Regenerate the run with the same camera plan and render passes.
- Fail the sample instead of substituting placeholder depth or segmentation.

Evidence:
- `capabilities/render_signal_sync_validation.json`: Validate that RGB, depth, segmentation, camera trajectory, and physics trace outputs are frame-aligned and complete for every planned view.

### Rolling Friction Ball

- id: `rolling_friction_ball`
- pattern: `physics_constraint`
- stages: `physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.75` from `1` matched lines

Validate friction-controlled rolling motion on a flat support surface. The smoke family uses a ball with initial horizontal velocity, but the invariant applies to any rolling rigid body with contact friction.

Key iteration moves:
- Declare initial horizontal velocity and support surface contact.
- Bind friction material metadata to the rolling body and surface.
- Tune friction or solver substeps only after trajectory/contact evidence exists.

Evidence:
- `capabilities/rolling_friction_ball.json`: Validate friction-controlled rolling motion on a flat support surface. The smoke family uses a ball with initial horizontal velocity, but the invariant applies to any rolling rigid body with contact friction.

### Runtime Actor Placement Compilation

- id: `runtime_actor_placement_compilation`
- pattern: `pipeline_stage`
- stages: `static_scene_layout, asset_runtime_binding, runtime_actor_placement`
- confidence: `0.75` from `1` matched lines

Compile static scene layout, asset resolution, camera plan, and physics graph membership into deterministic runtime actor bindings for UE or another execution backend.

Key iteration moves:
- Run static scene placement before actor placement.
- Update asset registry metadata for missing collider or material fields.
- Use analytic proxy only when the selected real asset cannot satisfy physics-critical metadata.

Evidence:
- `capabilities/runtime_actor_placement_compilation.json`: Compile static scene layout, asset resolution, camera plan, and physics graph membership into deterministic runtime actor bindings for UE or another execution backend.

### Runtime Backend Execution

- id: `runtime_backend_execution`
- pattern: `pipeline_stage`
- stages: `runtime_preflight, runtime_execution, runtime_artifact_collection`
- confidence: `0.75` from `1` matched lines

Execute a compiled case through the selected runtime backend while preserving deterministic inputs, explicit source labels, and hard failures for missing UE configuration or artifacts.

Key iteration moves:
- Set SIM_STUDIO_UE_PROJECT, SIM_STUDIO_UE_EXECUTABLE, SIM_STUDIO_ASSET_REGISTRY, and SIM_STUDIO_UE_RUNNER_CMD for UE runs.
- Inspect ue_preflight_report.json before changing case physics.
- Do not use fallback artifacts to claim production UE success.

Evidence:
- `capabilities/runtime_backend_execution.json`: Execute a compiled case through the selected runtime backend while preserving deterministic inputs, explicit source labels, and hard failures for missing UE configuration or artifacts.

### Scene Spec Compilation

- id: `scene_spec_compilation`
- pattern: `pipeline_stage`
- stages: `scene_spec_compilation`
- confidence: `0.75` from `1` matched lines

Compile capability, case spec, and assets into an executable scene spec with object roles, initial states, collision graph, camera, render requirements, and required signals.

Key iteration moves:
- Add missing object roles and stable ids.
- Move initial velocities from prompt prose into structured fields.
- Record camera and signal requirements in scene spec.

Evidence:
- `capabilities/scene_spec_compilation.json`: Compile capability, case spec, and assets into an executable scene spec with object roles, initial states, collision graph, camera, render requirements, and required signals.

### Sliding Crate Friction

- id: `sliding_crate_friction`
- pattern: `physics_constraint`
- stages: `physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.75` from `1` matched lines

Validate flat-surface sliding friction and static-friction threshold behavior. The smoke family uses a crate, but the invariant applies to any sliding rigid body with a support contact and friction material.

Key iteration moves:
- Declare initial velocity or applied force mode explicitly.
- Bind static/dynamic friction material metadata to body and support.
- Export support contact events before validating friction response.

Evidence:
- `capabilities/sliding_crate_friction.json`: Validate flat-surface sliding friction and static-friction threshold behavior. The smoke family uses a crate, but the invariant applies to any sliding rigid body with a support contact and friction material.

## Rigid-Body Contact Reference Workflow

1. **Compile object graph**: Create active driver bodies and passive receiver bodies with stable ids.
2. **Bind physical assets**: Use colliders, mass, rigid body, material, and no-overlap semantics for every physics-critical object.
3. **Set physics controls**: Active velocity or impulse encodes requested action; passive bodies start below velocity epsilon.
4. **Execute runtime**: Runtime, not the LLM or keyframes, produces passive motion through contact events.
5. **Verify causality**: Reject if passive bodies move above threshold before first causal contact.
6. **Iterate controls**: Tune speed, restitution, friction, spacing, mass ratio, and solver substeps only after causality passes.

## Closed-Loop Demo Cases

| Case | Capability | Verified Contract |
|---|---|---|
| Contact causality, including billiards | `rigid_body_contact_causality` | Active bodies move first; passive bodies move only after contact propagation |
| Falling blocks | `rigid_body_gravity_collision` | Gravity/collision are enabled, z decreases, and support contact is recorded |
| Domino chain | `sequential_contact_propagation` | First domino is actively triggered; downstream dominoes tip through ordered adjacent contacts |
| Spin decay | `angular_damping_spin_decay` | Angular velocity and damping are explicit, and angular speed decays without unexplained gain |
| Distance constraint / pendulum | `constraint_distance_pendulum_motion` | Anchor-body length stays within tolerance, motion is continuous, and constraint trace is exported |
| Constrained impulse chain | `constraint_momentum_transfer` | Adjacent contacts are ordered, passive chain members start still, and terminal receiver motion is contact-driven |
| Elastic energy launch | `elastic_energy_launch` | Release event exists, payload starts still, and post-release kinetic response stays within stored-energy bounds |

## Iteration Playbook

- **mine**: Use WHAT/HOW/FLOW/BRIDGE extraction; do not publish raw sessions.
- **distill**: Remove private paths, secrets, and raw logs; keep owner files and checks.
- **exercise**: Run several prompts and classify failures by stage before changing code.
- **tighten**: Change one responsible stage at a time and rerun tests.
