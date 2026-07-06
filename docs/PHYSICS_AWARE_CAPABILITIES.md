# Physics-Aware Harness Capability Profile

This report is generated from project memory, docs, tests, and runtime code. It distills reusable harness capabilities without committing raw private sessions.

## Extraction Method

- WHAT/HOW/FLOW/BRIDGE pattern mining
- decision-chain re-anchoring
- failure taxonomy extraction
- capability quality checklist

## Capability Summary

## Capability Abstraction Update

`billiard_causality_compiler` is no longer treated as a core capability. It is a
compatibility alias for old runs and tests. New planners, case templates, and
agent-facing docs should use:

- `prompt_case_capability_planning`: prompt/task -> capability layers, case
  family, required signals, and repairable execution plan.
- `rigid_body_contact_causality`: generic active/passive contact transfer across
  billiards, bowling/pins, crates, mass-ratio impacts, and other rigid-body
  collision cases.
- `static_scene_placement`: object-level static layout, support surfaces,
  non-overlap, camera coverage, and physics graph membership before runtime.
- `asset_runtime_binding_invocation`: top-k asset retrieval, selected asset or
  proxy fallback, and runtime actor binding with collider/material metadata.
- `physics_property_constraint_validation`: structured mass/friction/restitution/
  damping/gravity/material/force constraints and parameter-sensitivity checks.
- `explicit_physics_control_surface`: typed replayable gravity/material/
  rigid-body/constraint/force/time controls with runtime echo checks.
- `pipeline_stage_orchestration`: explicit stage inputs/outputs from planning to
  artifact package, with no silent fallback.
- `canonical_signal_capture`: synchronized trajectory/contact/camera/render pass
  evidence on one timebase.
- `physics_verifier_truth_gate`: verifier report as the source of truth for
  readiness, separate from UI preview or render success.
- `dataset_artifact_packaging`: readiness-gated packaging with lineage, hashes,
  signal availability, and failed-sample visibility.
- `bounce_restitution_ball`: a concrete restitution validator that checks descent,
  support contact, rebound height, and energy-envelope violations. It is a
  reusable restitution invariant; the ball drop is only the smoke family.
- `rolling_friction_ball`: a concrete rolling-friction validator that checks
  support contact, speed decay, and friction-bounded travel distance. The rolling
  ball is only the smoke family.
- `sliding_crate_friction`: a concrete sliding/static-friction validator that
  checks support contact, dynamic-friction stop distance, and below-threshold
  static hold. The crate is only the smoke family.
- `force_field_wind_drift`: a concrete force-field validator that checks explicit
  wind vector labels, drift direction, bounded displacement, and altitude
  envelope. The balloon is only the smoke family.
- `mass_ratio_momentum_transfer`: a concrete momentum-transfer validator that
  checks mass labels, contact evidence, post-collision velocity ordering, and
  restitution-bounded energy gain. The two-body collision is only the smoke
  family.
- `angular_damping_spin_decay`: a concrete rotational-damping validator that
  checks angular velocity labels, angular damping, spin speed decay, and
  rotation trace evidence. The spinning sphere is only the smoke family.

This keeps the harness useful beyond billiards: the same contact-causality
contract can verify pool, bowling, crate impacts, and other contact-driven scenes.

## Agent-Facing Capability Layers

| Layer | Capability IDs | What The Agent Should Do |
|---|---|---|
| Prompt/case planning | `prompt_case_capability_planning` | Select a reusable physical invariant and case family; do not select a one-off object template. |
| Scene/static layout | `scene_spec_compilation`, `static_scene_placement` | Compile stable object ids, transforms, support surfaces, camera coverage, and collision graph. |
| Asset retrieval/call | `asset_intent_resolution`, `asset_runtime_binding_invocation` | Retrieve top-k typed candidates, choose/proxy explicitly, bind collider/mass/material/collision profile into runtime. |
| Physics controls | `explicit_physics_control_surface`, `physics_property_constraint_validation` | Store gravity, mass, friction, restitution, damping, force, time, agent, and render-physics bridge controls as typed replayable fields. |
| Runtime evidence | `capability_runtime_artifact_bridge`, `canonical_signal_capture` | Normalize UE/fallback trajectory, contacts, camera path, RGB/depth/segmentation, and render metadata. |
| Verification/package | `physics_verifier_truth_gate`, `dataset_artifact_packaging` | Gate sample readiness and package only auditable, schema-valid artifacts. |

### Prompt / Case Capability Planning

- id: `prompt_case_capability_planning`
- pattern: `pipeline_stage`
- stages: `capability_planning, case_spec_compilation`
- confidence: `0.95` from `116` matched lines

Compile natural language into explicit objects, roles, asset intents, camera needs, lighting, and runtime scene requests instead of a free-form video description.

Key iteration moves:
- Add missing object roles to prompt expansion.
- Inspect asset_requests.json and runtime_scene_request.json before rerendering.
- Rerun planner with stricter object-id and role constraints.

Evidence:
- `README.md:188`: | `runtime_scene_request.json` | Normalized runtime object graph |
- `README.md:207`: | 4. Scene Spec | Object graph, assets | `scene_spec.json`, runtime request | `tools/draft_builder.py`, `contracts/`, `configs/` | Change object layout, camera defaults, scene schema, or artifact contracts |
- `docs/PHYSICS_AWARE_HARNESS.md:153`: | `F1_scene_parsing_failure` | prompt or scene intent cannot form a valid object-level plan |

### Generic Rigid-Body Contact Causality

- id: `rigid_body_contact_causality`
- pattern: `FLOW`
- stages: `capability_planning, case_spec_compilation, scene_spec_compilation, runtime_artifact_collection, physics_verification`
- confidence: `0.95` from `267` matched lines

Compile active-to-passive rigid-body contact scenes into causal simulation programs where passive bodies move only after runtime contact evidence exists. Billiards is a smoke family, not a standalone core capability.

Key iteration moves:
- Inspect physics.json for passive initial velocities.
- Inspect first trajectory frames for target speed before first active contact.
- Adjust radius/extent and initial spacing before increasing speed.

Evidence:
- `capabilities/rigid_body_contact_causality.json`: generic active/passive contact contract with trajectory and contact-event requirements.
- `tools/capability_closed_loop.py:18`: "prompt": "Create a billiards / pool scene where one cue ball hits a compact rack of passive target balls. Targets must stay still until contact.",
- `tools/capability_planner.py:27`: terms=("billiard", "billiards", "pool", "cue ball", "cue_ball", "台球", "白球", "目标球", "ball collision"),

### Typed Asset Physics Binding

- id: `asset_physics_binding`
- pattern: `HOW`
- stages: `asset_resolution, scene_spec, physics_control, verifier`
- confidence: `0.95` from `1043` matched lines

Classify assets by physical role before simulation so meshes, colliders, rigid bodies, maps, skeletal assets, and visual-only materials are handled differently.

Key iteration moves:
- Rebuild asset_physics_index.json after changing asset sources.
- Audit asset_candidates.json and asset_selection.json before runtime.
- Add role profiles for repeated physical object families.

Evidence:
- `capabilities/asset_intent_resolution.json:6`: "Physics-critical assets require collider, mass, rigid body, and collision profile.",
- `docs/PHYSICS_AWARE_HARNESS.md:278`: - Decide whether an asset can be used for visual-only, physics-critical, skeletal, map, or logic roles.
- `README.md:250`: | `asset_physics_binding` | Assets are typed before simulation and physics-critical assets bind colliders/rigid bodies |

### Rigid Body Gravity Collision

- id: `rigid_body_gravity_collision`
- pattern: `FLOW`
- stages: `planner, physics_control, runtime, verifier`
- confidence: `0.95` from `386` matched lines

Compile falling or stacking prompts into gravity-driven rigid-body traces where objects descend under gravity and collide with a support surface instead of being visually animated.

Key iteration moves:
- Fix initial height and support plane before changing material properties.
- Check trajectory z-series and contact pairs.
- Tune mass, restitution, damping, and solver substeps after gravity/contact validity passes.

Evidence:
- `tools/capability_planner.py:33`: terms=("falling block", "falling blocks", "falling crate", "falling object", "stacking", "gravity", "下落", "坠落", "堆叠", "重力", "落到地面"),
- `README.md:286`: | falling blocks | `rigid_body_gravity_collision` | gravity/collision enabled, z decreases, support contact exists |
- `docs/PHYSICS_AWARE_HARNESS.md:136`: | falling blocks / stacking / gravity | `rigid_body_gravity_collision` |

### Sequential Contact Propagation

- id: `sequential_contact_propagation`
- pattern: `FLOW`
- stages: `planner, physics_control, runtime, verifier`
- confidence: `0.95` from `125` matched lines

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
- confidence: `0.95` from `777` matched lines

Represent gravity, material, rigid-body, constraint, force, time, agent, and render-physics bridge controls as typed fields that can be replayed and swept.

Key iteration moves:
- Run baseline, implicit, and structured-control variants.
- Sweep one parameter across fixed seeds.
- Compare reproduction variance and verifier pass rate.

Evidence:
- `tools/draft_builder.py:77`: "initial_velocity_m_s, mass_kg, friction, restitution, collision_shape, material, and expected_event. "
- `README.md:208`: | 5. Physics Control | Scene objects, prompt physics intent | `physics.json`, `physics_control` | `core/physics_control.py`, `configs/physics_schema.json`, `config/material_profiles.yaml` | Add typed parameters, default…
- `README.md:373`: | `materials[]` | static/dynamic friction, restitution, density, damping, hardness |

### Canonical Multi-Signal Capture

- id: `canonical_signal_capture`
- pattern: `FLOW`
- stages: `runtime, signals, dataset`
- confidence: `0.95` from `712` matched lines

Capture video and aligned evidence streams on one timebase: RGB, trajectory, contacts, camera path, depth proxy, normal proxy, audio, engine states, and semantic labels.

Key iteration moves:
- Run pass quality report before dataset packaging.
- Add missing view or signal manifests before rerendering a full suite.
- Keep camera/action/initial_state fixed for control sensitivity runs.

Evidence:
- `README.md:16`: -> Stage 7: RGB / trajectory / contacts / camera / depth / normal / audio signals
- `tools/dataset_protocol.py:165`: "physeditworld": "RGB, depth, normal, audio, action trace, camera trajectory, engine states, semantics, and gravity labels are synchronized.",
- `tools/dataset_protocol.py:543`: "当前 RGB 来自 UE native render；audio 可由 contact events 确定性合成；depth / normal 已作为 runtime-derived proxy pass 输出，并在 render_pass_manifest.json 中显式标记 source_type，后续接 UE GBuffer 时可替换为 native_render。",

### Physics Verifier Truth Gate

- id: `physics_verifier_truth_gate`
- pattern: `verification`
- stages: `physics_verification, render_verification, diagnosis`
- confidence: `0.95` from `251` matched lines

Use verifier output, not UI preview or successful rendering, as the final readiness decision for reference-ready samples.

Key iteration moves:
- Use verifier failure type to decide whether to replan, rebind assets, or rerun runtime.
- Promote only passing artifacts into dataset shards.
- Track verifier pass rate across seeds and prompt variants.

Evidence:
- `tests/test_capability_runtime_adapter.py:75`: write_json(output_dir / "run_readiness.json", {"passed": True, "reference_ready": True, "physics_ready": True, "visual_ready": True})
- `tests/test_capability_runtime_adapter.py:134`: write_json(output_dir / "run_readiness.json", {"passed": True, "reference_ready": True, "physics_ready": True, "visual_ready": True})
- `README.md:211`: | 8. Verification | Video and physics evidence | pass/fail readiness | `tools/case_verifier.py`, `core/verifier.py`, `tools/run_readiness.py` | Add causality, speed, spread, asset, visual, or signal-consistency checks |

### Capability Runtime Artifact Bridge

- id: `capability_runtime_artifact_bridge`
- pattern: `BRIDGE`
- stages: `runtime, signals, verifier`
- confidence: `0.95` from `147` matched lines

Convert existing UE or fallback run artifacts into the capability verifier trace contract so real runtime evidence can be checked with the same causality rules as deterministic dry runs.

Key iteration moves:
- Run scripts/verify_capability_run.py on an existing run.
- Inspect capability_verifier.json before changing prompt or scene parameters.
- Use capability_diagnosis.md to decide whether the issue is planning, runtime, evidence, or verifier.

Evidence:
- `docs/PHYSICS_AWARE_HARNESS.md:190`: | `ue_output/trajectory.json` or `debug_preview/trajectory.json` | normalized object states and contact events |
- `README.md:189`: | `ue_output/trajectory.json` | Runtime trajectory |
- `README.md:191`: | `ue_output/render_pass_manifest.json` | RGB/depth/normal/audio signal manifest |

### Trajectory-Based Benchmark Iteration

- id: `trajectory_benchmark_iteration`
- pattern: `FLOW`
- stages: `benchmark, runtime, verifier`
- confidence: `0.95` from `90` matched lines

Evaluate real runtime trajectories across seed sweeps and variants instead of dry control signals or mixed-backend stability numbers.

Key iteration moves:
- Run full pipeline with seed sweep.
- Inspect coverage, signal purity, and stability reports.
- Cluster failures before changing planner or schema.

Evidence:
- `README.md:42`: | Benchmark engine | `benchmark_suite/` | Canonical trajectory evaluation, pipeline state machine, regression gates |
- `README.md:212`: | 9. Benchmark | Prompt suite and run artifacts | Metrics and regression reports | `benchmark_suite/`, `benchmark_prompts/` | Add benchmark cases, canonical metrics, gates, or seed sweeps |
- `README.md:393`: python3.13 benchmark_suite/run_full_pipeline.py --seed-sweep 20 --strict --resume

### Verified Multi-View Dataset Packaging

- id: `dataset_artifact_packaging`
- pattern: `dataset_packaging`
- stages: `artifact_manifest, readiness_gate, dataset_packaging`
- confidence: `0.95` from `272` matched lines

Package only readiness-gated runs into a dataset layout with video, synchronized signals, physics labels, asset metadata, and hashes.

Key iteration moves:
- Run package_dataset.py after verifier updates.
- Audit sample tiers before publishing.
- Use benchmark taxonomy to grow cases systematically.

Evidence:
- `tools/dataset_protocol.py:165`: "physeditworld": "RGB, depth, normal, audio, action trace, camera trajectory, engine states, semantics, and gravity labels are synchronized.",
- `README.md:16`: -> Stage 7: RGB / trajectory / contacts / camera / depth / normal / audio signals
- `README.md:191`: | `ue_output/render_pass_manifest.json` | RGB/depth/normal/audio signal manifest |

### Failure-Driven Replanning And Refinement

- id: `failure_driven_refinement_loop`
- pattern: `BRIDGE`
- stages: `planner, runtime, verifier, benchmark`
- confidence: `0.95` from `291` matched lines

Use structured failure evidence to decide the next minimal change: prompt expansion, asset binding, scene layout, physics controls, runtime settings, or verifier thresholds.

Key iteration moves:
- Mine failure evidence from verifier and trajectory.
- Change exactly one stage owner file or prompt contract.
- Rerun tests and a small prompt suite before publishing.

Evidence:
- `docs/PHYSICS_AWARE_HARNESS.md:78`: | Failure | Type | Fix |
- `docs/PHYSICS_AWARE_HARNESS.md:158`: | `F6_runtime_or_render_failure` | backend execution or render failed |
- `docs/PHYSICS_AWARE_HARNESS.md:266`: | Failure | Type | Fix |

### Lineage-Based Harness Capability Extraction

- id: `lineage_based_capability_extraction`
- pattern: `BRIDGE`
- stages: `meta, docs, tests`
- confidence: `0.95` from `97` matched lines

Mine repeated project memory, reports, and final artifacts into reusable harness capabilities without publishing raw private sessions or secrets.

Key iteration moves:
- Run local extraction from memory and reports.
- Run public extraction from README, docs, tests, and code.
- Diff the capability profile after new benchmark or prompt iterations.

Evidence:
- `docs/PHYSICS_AWARE_HARNESS.md:34`: - extracted session-skill methods: WHAT/HOW/FLOW/BRIDGE pattern mining, decision-chain re-anchoring, failure taxonomy extraction
- `README.md:223`: The repo includes a small extractor that turns project evidence into a reusable harness capability profile. This is how the billiard-style collision work is preserved without hardcoding a billiard template: the extracte…
- `README.md:314`: This is the bridge from generated video runs back into the physics-aware harness. If the video exists but passive objects move before runtime contact, the run fails with `F4_causality_violation`; the MP4 alone is not co…

## Rigid-Body Contact Reference Workflow

1. **Compile object graph**: Create active driver/impactor bodies and passive receiver bodies with stable ids.
2. **Bind physical assets**: Use colliders, mass, rigid body, material, collision profile, and no-overlap semantics for every physics-critical object.
3. **Set physics controls**: Driver velocity or impulse encodes requested action; passive bodies start below velocity epsilon.
4. **Execute runtime**: Runtime, not the LLM, produces passive motion through contact events.
5. **Verify causality**: Reject if passive bodies move above threshold before first causal contact.
6. **Iterate controls**: Tune speed, restitution, friction, spacing, mass ratio, and solver substeps only after causality passes.

## Closed-Loop Demo Cases

| Case | Capability | Verified Contract |
|---|---|---|
| Contact causality, including billiards | `rigid_body_contact_causality` | Active bodies move first; passive bodies move only after contact propagation |
| Falling blocks | `rigid_body_gravity_collision` | Gravity/collision are enabled, z decreases, and support contact is recorded |
| Domino chain | `sequential_contact_propagation` | First domino is actively triggered; downstream dominoes tip through ordered adjacent contacts |
| Spin decay | `angular_damping_spin_decay` | Angular velocity and damping are explicit, and angular speed decays without unexplained gain |

## Iteration Playbook

- **mine**: Use WHAT/HOW/FLOW/BRIDGE extraction; do not publish raw sessions.
- **distill**: Remove private paths, secrets, and raw logs; keep owner files and checks.
- **exercise**: Run several prompts and classify failures by stage before changing code.
- **tighten**: Change one responsible stage at a time and rerun tests.
