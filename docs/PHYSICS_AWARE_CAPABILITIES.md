# Physics-Aware Harness Capability Profile

This report is generated from project memory, docs, tests, and runtime code. It distills reusable harness capabilities without committing raw private sessions.

## Extraction Method

- WHAT/HOW/FLOW/BRIDGE pattern mining
- decision-chain re-anchoring
- failure taxonomy extraction
- capability quality checklist

## 能力抽象原则

- capability id 必须命名可复用的不变量或 pipeline 阶段，不能命名成某个单独场景模板。
- `billiard_causality_compiler` 不进入 public profile；旧 JSON 只作为 legacy artifact alias 保留。
- 台球、保龄球、箱体撞击等都属于 `rigid_body_contact_causality` 的 case family。
- 资产能力拆成 `asset_intent_resolution` 和 `asset_runtime_binding_invocation`：先检索/筛选，再绑定 runtime actor。
- 物理能力必须绑定 verifier invariant、required signals、failure taxonomy 和 repair suggestions。

## Capability Summary

### Prompt To Object-Level Scene Compiler

- id: `prompt_case_capability_planning`
- pattern: `FLOW`
- stages: `planner, scene_spec, asset_resolution`
- confidence: `0.95` from `31` matched lines

Compile natural language into explicit objects, roles, asset intents, camera needs, lighting, and runtime scene requests instead of a free-form video description.

Key iteration moves:
- Add missing object roles to prompt expansion.
- Inspect asset_requests.json and runtime_scene_request.json before rerendering.
- Rerun planner with stricter object-id and role constraints.

Evidence:
- `tools/capability_verifier.py:202`: "F1_scene_parsing_failure": "repair prompt expansion and object roles before execution",
- `README.md:16`: -> asset intent resolution
- `README.md:162`: | `scene_spec_compilation` | Builds runtime scene contracts from capability/case/assets. |

### Generic Rigid-Body Contact Causality

- id: `rigid_body_contact_causality`
- pattern: `FLOW`
- stages: `capability_planning, case_spec_compilation, scene_spec_compilation, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `388` matched lines

Compile active-to-passive rigid-body contact scenes into causal simulation programs where passive bodies move only after runtime contact evidence exists.

Key iteration moves:
- Inspect physics.json for passive initial velocities.
- Inspect first trajectory frames for target speed before first active contact.
- Adjust radius/extent and initial spacing before increasing speed.

Evidence:
- `tools/capability_planner.py:27`: terms=("billiard", "billiards", "pool", "cue ball", "cue_ball", "bowling", "pins", "rigid-body impact", "rigid body impact", "crate impact", "mass ratio collision", "台球", "白球", "目标球", "保龄球", "球瓶", "刚体碰撞", "ball collisio…
- `tools/capability_closed_loop.py:18`: "prompt": "Create a billiards / pool scene where one cue ball hits a compact rack of passive target balls. Targets must stay still until contact.",
- `README.md:174`: | `rigid_body_contact_causality` | Active bodies may move; passive rigid bodies must remain still until runtime contact evidence. Billiards/pool is one case family. |

### Asset Intent Resolution

- id: `asset_intent_resolution`
- pattern: `HOW`
- stages: `asset_intent_resolution, asset_retrieval`
- confidence: `0.95` from `281` matched lines

Classify object-level asset needs into typed intents and retrieve top-k candidates before runtime binding.

Key iteration moves:
- Rebuild asset registry or physics index after changing asset sources.
- Audit top-k candidates before runtime binding.
- Add role-specific tags for repeated physical object families.

Evidence:
- `capabilities/asset_intent_resolution.json:6`: "Physics-critical assets require collider, mass, rigid body, and collision profile.",
- `capabilities/static_scene_placement.json:25`: "Physics-critical assets have collider, mass/material, and collision profile.",
- `docs/PHYSICS_AWARE_HARNESS.md:166`: | `asset_runtime_binding_invocation` | physics-critical assets must bind colliders, mass/material metadata, collision profile, and runtime actor ids |

### Asset Runtime Binding Invocation

- id: `asset_runtime_binding_invocation`
- pattern: `HOW`
- stages: `asset_runtime_binding, runtime_actor_binding, preflight_validation`
- confidence: `0.95` from `136` matched lines

Bind selected real assets or analytic proxies into runtime actors while preserving collider, mass, material, and collision-profile metadata.

Key iteration moves:
- Fix asset registry metadata before changing physics verifier thresholds.
- Use analytic proxy only for smoke/debug and mark proxy=true.
- Add binding audit output for missing collider, wrong mass, wrong constraint, and missing binding.

Evidence:
- `docs/PHYSICS_AWARE_HARNESS.md:166`: | `asset_runtime_binding_invocation` | physics-critical assets must bind colliders, mass/material metadata, collision profile, and runtime actor ids |
- `docs/CAPABILITY_AUTHORING.md:36`: - 资产能力要区分 retrieval 和 invocation：`asset_intent_resolution` 找候选，`asset_runtime_binding_invocation` 负责把 selected asset/proxy 绑定到 runtime actor。
- `harness/assets/asset_intent.py:89`: required = ["collider", "mass", "rigid_body", "collision_profile"] if physics_critical else ["visual_proxy"]

### Rigid Body Gravity Collision

- id: `rigid_body_gravity_collision`
- pattern: `FLOW`
- stages: `planner, physics_control, runtime, verifier`
- confidence: `0.95` from `162` matched lines

Compile falling or stacking prompts into gravity-driven rigid-body traces where objects descend under gravity and collide with a support surface instead of being visually animated.

Key iteration moves:
- Fix initial height and support plane before changing material properties.
- Check trajectory z-series and contact pairs.
- Tune mass, restitution, damping, and solver substeps after gravity/contact validity passes.

Evidence:
- `tools/capability_planner.py:33`: terms=("falling block", "falling blocks", "falling crate", "falling object", "stacking", "gravity", "下落", "坠落", "堆叠", "重力", "落到地面"),
- `tests/test_capability_closed_loop.py:51`: plan = self.planner.plan("Falling blocks under gravity collide with the ground.")
- `tools/capability_closed_loop.py:22`: "prompt": "Create falling blocks under gravity. Rigid bodies fall and collide with the ground and each other; the motion must not be a visual-only animation.",

### Sequential Contact Propagation

- id: `sequential_contact_propagation`
- pattern: `FLOW`
- stages: `planner, physics_control, runtime, verifier`
- confidence: `0.95` from `83` matched lines

Compile domino or chain-reaction prompts into ordered contact propagation where only the first body is actively triggered and later bodies move after upstream contact.

Key iteration moves:
- Check initial angular velocity on all non-first objects.
- Inspect adjacent contact pair coverage.
- Adjust spacing, mass, friction, and angular damping after sequence validity passes.

Evidence:
- `tools/capability_planner.py:39`: terms=("domino", "dominoes", "chain reaction", "sequential contact", "contact propagation", "多米诺", "连锁反应", "链式碰撞", "依次倒下"),
- `tools/capability_closed_loop.py:26`: "prompt": "Create a domino chain reaction. The first domino is actively triggered and the later dominoes tip only through sequential contact propagation.",
- `capabilities/sequential_contact_propagation.json:4`: "description": "Compile domino or chain-reaction prompts into ordered contact propagation where only the first object is actively triggered.",

### Explicit Physics Control Surface

- id: `explicit_physics_control_surface`
- pattern: `HOW`
- stages: `planner, physics_control, benchmark`
- confidence: `0.95` from `352` matched lines

Represent gravity, material, rigid-body, constraint, force, time, agent, and render-physics bridge controls as typed fields that can be replayed and swept.

Key iteration moves:
- Run baseline, implicit, and structured-control variants.
- Sweep one parameter across fixed seeds.
- Compare reproduction variance and verifier pass rate.

Evidence:
- `README.md:164`: | `physics_property_constraint_validation` | Checks mass, friction, restitution, damping, gravity, material, and parameter-sweep constraints. |
- `capabilities/physics_property_constraint_validation.json:4`: "description": "Validate physical property ranges and parameter sensitivity for mass, friction, restitution, damping, gravity, material density, fracture threshold, buoyancy, and force-field controls.",
- `capabilities/physics_property_constraint_validation.json:23`: "Mass, radius, friction, restitution, gravity, and damping stay in capability-specific ranges.",

### Canonical Multi-Signal Capture

- id: `canonical_signal_capture`
- pattern: `FLOW`
- stages: `runtime, signals, dataset`
- confidence: `0.95` from `469` matched lines

Capture video and aligned evidence streams on one timebase: RGB, trajectory, contacts, camera path, depth proxy, normal proxy, audio, engine states, and semantic labels.

Key iteration moves:
- Run pass quality report before dataset packaging.
- Add missing view or signal manifests before rerendering a full suite.
- Keep camera/action/initial_state fixed for control sensitivity runs.

Evidence:
- `capabilities/canonical_signal_capture.json:10`: "description": "Collect RGB, depth, segmentation, trajectory, contact events, camera trajectory, and engine-state labels on a single deterministic timebase.",
- `README.md:167`: | `canonical_signal_capture` | Keeps trajectory, contacts, camera paths, RGB/depth/segmentation, and render metadata on one timebase. |
- `docs/HARNESS_ARCHITECTURE.md:108`: 输出同步多视角 RGB/depth/segmentation，以及 trajectory/contact/camera timeline。

### Verifier As Runtime Truth Gate

- id: `physics_verifier_truth_gate`
- pattern: `HOW`
- stages: `verifier, dataset`
- confidence: `0.95` from `216` matched lines

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
- confidence: `0.95` from `107` matched lines

Convert existing UE or fallback run artifacts into the capability verifier trace contract so real runtime evidence can be checked with the same causality rules as deterministic dry runs.

Key iteration moves:
- Run scripts/verify_capability_run.py on an existing run.
- Inspect capability_verifier.json before changing prompt or scene parameters.
- Use capability_diagnosis.md to decide whether the issue is planning, runtime, evidence, or verifier.

Evidence:
- `harness/verification/physics_verifier.py:118`: for name in ("fallback_output", "ue_output", "debug_preview"):
- `tests/test_capability_runtime_adapter.py:8`: from tools.capability_runtime_adapter import CapabilityRuntimeAdapter, resolve_runtime_output_dir, verify_capability_run
- `tests/test_capability_runtime_adapter.py:12`: def test_resolves_ue_output_before_debug_preview(self) -> None:

### Trajectory-Based Benchmark Iteration

- id: `trajectory_benchmark_iteration`
- pattern: `FLOW`
- stages: `benchmark, runtime, verifier`
- confidence: `0.95` from `61` matched lines

Evaluate real runtime trajectories across seed sweeps and variants instead of dry control signals or mixed-backend stability numbers.

Key iteration moves:
- Run full pipeline with seed sweep.
- Inspect coverage, signal purity, and stability reports.
- Cluster failures before changing planner or schema.

Evidence:
- `README.md:198`: The old billiards failure mode is still preserved as a regression: plausible
- `README.md:36`: | `tests/` | Regression tests for CLI, capabilities, verifier, render sync, artifacts. |
- `README.md:74`: --seed 42 \

### Angular Damping Spin Decay

- id: `angular_damping_spin_decay`
- pattern: `physics_constraint`
- stages: `physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `15` matched lines

Validate rotational damping for spinning rigid bodies using explicit angular velocity, angular damping labels, rotation trace, and monotonic spin-decay evidence.

Key iteration moves:
- Move spin controls into initial_angular_velocity_deg_s and expected_physics.angular_damping.
- Bind spinning bodies as physics-critical assets with inertia/damping metadata.
- Add negative cases for missing labels, no decay, and unexplained spin gain.

Evidence:
- `tools/capability_planner.py:87`: terms=("angular damping", "spin decay", "spinning body", "angular velocity", "rotational damping", "spin slows", "spin down", "角阻尼", "角速度", "旋转衰减", "自转", "旋转变慢"),
- `capabilities/angular_damping_spin_decay.json:10`: "description": "Validate rotational damping for spinning rigid bodies using explicit angular velocity, angular damping labels, rotation trace, and monotonic spin-decay evidence.",
- `README.md:184`: | `angular_damping_spin_decay` | Spinning rigid bodies must declare angular velocity and damping, then show monotonic spin decay in angular velocity and rotation trace evidence. |

### Agent Action To Rigid-Body Coupling

- id: `agent_rigidbody_action_coupling`
- pattern: `physics_constraint`
- stages: `action_trace_planning, physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.75` from `8` matched lines

Validate that agent or controller actions cause rigid-body motion through explicit action trace, contact or impulse evidence, and post-action trajectory response.

Key iteration moves:
- Add action_trace to expected_physics or runtime artifacts.
- Bind the target as a physics-critical rigid body and the agent as an action-producing actor.
- Use negative cases for pre-action motion, missing action trace, and no post-action response.

Evidence:
- `tools/capability_planner.py:93`: terms=("agent pushes", "agent push", "robot pushes", "robot push", "character pushes", "character throws", "agent throws", "agent throw", "robot throws", "action trace", "agent-to-rigidbody", "agent rigid body", "推箱子",…
- `README.md:185`: | `agent_rigidbody_action_coupling` | Agent or controller actions must be explicit action traces, and target rigid bodies may move only after action/contact or release/impulse evidence. |
- `capabilities/agent_rigidbody_action_coupling.json:11`: "description": "Validate that agent or controller actions cause rigid-body motion through an explicit action trace, contact/impulse evidence, and post-action trajectory response. Pushing a box or throwing a ball are smo…

### Distance Constraint Motion Validation

- id: `constraint_distance_pendulum_motion`
- pattern: `physics_constraint`
- stages: `case_spec_compilation, physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `32` matched lines

Validate fixed-distance or joint-constrained rigid-body motion using anchor/body trajectory, constraint length labels, constraint trace, and continuity checks.

Key iteration moves:
- Export constraint_trace before tuning rope visuals.
- Fix constraint_length_m, anchor/body ids, and solver timestep before changing camera or materials.
- Add negative cases for missing constraint label, length drift, and teleporting body.

Evidence:
- `tools/capability_planner.py:99`: terms=("pendulum", "swinging pendulum", "distance constraint", "fixed length", "constraint length", "joint constraint", "rope constraint", "hinge constraint", "单摆", "摆锤", "距离约束", "固定长度", "约束长度", "铰链约束", "绳长约束"),
- `capabilities/constraint_distance_pendulum_motion.json:10`: "description": "Validate fixed-distance constraint motion such as pendulums using anchor/body trajectory, constraint length labels, and continuity checks. A pendulum bob is only the smoke family.",
- `tests/test_capability_closed_loop.py:59`: plan = self.planner.plan("A pendulum swings while preserving a fixed length rope constraint.")

### Constrained Impulse Chain Transfer

- id: `constraint_momentum_transfer`
- pattern: `physics_constraint`
- stages: `case_spec_compilation, physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.8` from `9` matched lines

Validate ordered contact-driven impulse and momentum transfer through a chain of constrained rigid bodies.

Key iteration moves:
- Fix chain_objects and expected_contact_chain before changing visuals.
- Check passive initial velocities before tuning restitution.
- Add negative cases for pre-chain motion, terminal no-response, and contact order violations.

Evidence:
- `tools/capability_planner.py:105`: terms=("newton cradle", "newton's cradle", "impulse chain", "constrained impulse", "momentum chain", "chain momentum transfer", "suspended ball chain", "constraint momentum transfer", "牛顿摆", "冲量链", "动量链", "悬挂球", "受约束动量传…
- `capabilities/constraint_momentum_transfer.json:11`: "description": "Validate constrained impulse-chain momentum transfer across adjacent rigid bodies. Newton's cradle is only the smoke family; the reusable invariant is ordered contact-driven transfer through constrained…
- `docs/CAPABILITY_AUTHORING.md:51`: | `newton_cradle_template` 作为主能力 | `constraint_momentum_transfer`，牛顿摆/悬挂球链作为 case family |

### Elastic Energy Launch

- id: `elastic_energy_launch`
- pattern: `physics_constraint`
- stages: `case_spec_compilation, physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `33` matched lines

Validate stored elastic energy release into launched rigid-body motion using explicit release events, spring parameters, payload mass, and bounded kinetic-energy response.

Key iteration moves:
- Fix spring_constant, compression, payload mass, and release event before tuning camera.
- Inspect spring_events.json and trajectory frames around release.
- Add negative cases for missing release, no launch response, and energy gain.

Evidence:
- `tools/capability_planner.py:111`: terms=("spring launch", "spring launcher", "compressed spring", "elastic launch", "elastic energy", "catapult", "弹簧", "弹簧发射", "压缩弹簧", "弹射", "弹性势能"),
- `tests/test_capability_closed_loop.py:67`: plan = self.planner.plan("A compressed spring launches a payload from elastic energy.")
- `docs/AGENT_USAGE.md:137`: Elastic launch prompts should use `elastic_energy_launch`: spring/catapult-like

### Elastic Constraint Rebound

- id: `elastic_constraint_rebound`
- pattern: `physics_constraint`
- stages: `case_spec_compilation, physics_control, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `47` matched lines

Validate elastic tether or bungee-style constrained motion using rest length, bounded extension, constraint trace, and rebound velocity toward the anchor.

Key iteration moves:
- Fix rest_length_m, max_extension_m, stiffness, and damping before tuning visuals.
- Inspect constraint_trace.json around maximum extension.
- Add negative cases for missing trace, overstretch, and no rebound.

Evidence:
- `tools/capability_planner.py:117`: terms=("bungee", "elastic rope", "elastic tether", "stretchy rope", "rope rebound", "tether rebound", "elastic constraint", "蹦极", "弹性绳", "弹力绳", "弹性约束", "绳子回弹", "拉伸回弹"),
- `docs/CAPABILITY_AUTHORING.md:53`: | `bungee_template` 作为主能力 | `elastic_constraint_rebound`，蹦极/弹性绳作为 case family |
- `README.md:189`: | `elastic_constraint_rebound` | Elastic tether or bungee-style constraints must export rest length, extension trace, max-stretch bounds, and rebound velocity toward the anchor. Bungee is one smoke family. |

### Verified Multi-View Dataset Packaging

- id: `dataset_artifact_packaging`
- pattern: `FLOW`
- stages: `dataset, signals, verifier`
- confidence: `0.95` from `121` matched lines

Package only readiness-gated runs into a dataset layout with video, synchronized signals, physics labels, asset metadata, and hashes.

Key iteration moves:
- Run package_dataset.py after verifier updates.
- Audit sample tiers before publishing.
- Use benchmark taxonomy to grow cases systematically.

Evidence:
- `README.md:145`: - `scene_capture` RGB/depth/segmentation multi-view path is the stable data path.
- `capabilities/canonical_signal_capture.json:30`: "Every planned view has RGB/depth/segmentation metadata or a hard failure.",
- `docs/HARNESS_ARCHITECTURE.md:166`: 真实 UE 后续还必须补 `camera_trajectory.json`、视频/frame sequence、depth/normal/audio/pass manifest。

### Failure-Driven Replanning And Refinement

- id: `failure_driven_refinement_loop`
- pattern: `BRIDGE`
- stages: `planner, runtime, verifier, benchmark`
- confidence: `0.95` from `288` matched lines

Use structured failure evidence to decide the next minimal change: prompt expansion, asset binding, scene layout, physics controls, runtime settings, or verifier thresholds.

Key iteration moves:
- Mine failure evidence from verifier and trajectory.
- Change exactly one stage owner file or prompt contract.
- Rerun tests and a small prompt suite before publishing.

Evidence:
- `docs/PHYSICS_AWARE_HARNESS.md:189`: 每个失败必须归因到 failure type，例如：
- `harness/verification/physics_verifier.py:45`: "value": str(ue_backend_report.get("failure_message") or "UE backend failed"),
- `tools/capability_runtime_adapter.py:318`: lines.append(f"- `{failure.get('failure_type')}`: {failure.get('reason')}")

### Lineage-Based Harness Capability Extraction

- id: `lineage_based_capability_extraction`
- pattern: `BRIDGE`
- stages: `meta, docs, tests`
- confidence: `0.95` from `22` matched lines

Mine repeated project memory, reports, and final artifacts into reusable harness capabilities without publishing raw private sessions or secrets.

Key iteration moves:
- Run local extraction from memory and reports.
- Run public extraction from README, docs, tests, and code.
- Diff the capability profile after new benchmark or prompt iterations.

Evidence:
- `capabilities/dataset_artifact_packaging.json:10`: "description": "Package only verifier-gated runtime artifacts into dataset-ready sample layouts with lineage, hashes, signal availability, and failure visibility.",
- `README.md:10`: ## What Is Included
- `README.md:166`: | `capability_runtime_artifact_bridge` | Adapts runtime artifacts into verifier inputs. |

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

### Static Scene Placement

- id: `static_scene_placement`
- pattern: `pipeline_stage`
- stages: `static_scene_layout, camera_planning, preflight_validation`
- confidence: `0.75` from `1` matched lines

Build executable object-level scene layouts before simulation, including support surfaces, non-overlap placement, scale, orientation, camera coverage, and physics graph membership.

Key iteration moves:
- Generate scene_layout.json before runtime execution.
- Bind missing physics metadata or mark an analytic proxy.
- Adjust object positions, scales, and support surfaces.

Evidence:
- `capabilities/static_scene_placement.json`: Build executable object-level scene layouts before simulation, including support surfaces, non-overlap placement, scale, orientation, camera coverage, and physics graph membership.

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
