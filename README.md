# Agentic Data Platform: Physics-Aware Harness

A **physics-aware simulation harness for code agents**.

This repository is the harness-facing core, not a frontend-first demo and not a
generic prompt-to-video app. It gives an agent a stable way to compile a task
into a capability-backed case, run UE or fallback execution, collect synchronized
artifacts, verify physical causality, and package dataset-ready evidence.

## What Is Included

```text
Agent task / prompt
  -> capability planning
  -> case spec or generated case
  -> asset intent resolution
  -> runtime backend
  -> trajectory/contact/render artifact collection
  -> physics verifier
  -> diagnosis / repair suggestion
  -> dataset-ready artifact package
```

Main paths:

| Path | Purpose |
|---|---|
| `harness/` | Core harness modules: planning, runtime, verification, packaging. |
| `capabilities/` | Machine-readable capability contracts. |
| `cases/` | Golden cases and parameterized templates. |
| `scripts/harness_*.py` | Agent-callable CLI tools. |
| `scripts/native_ue_physics_phenomena_scene.py` | UE Python scene/capture script used by the local runner. |
| `ue_template/` | Minimal UE project and `ADPPhysicsRuntime` plugin source. |
| `assets/` | Public-safe examples only; real assets stay local. |
| `docs/` | Agent usage, UE setup, schemas, authoring notes. |
| `tests/` | Regression tests for CLI, capabilities, verifier, render sync, artifacts. |

For the complete design, setup, asset import, tool usage, and extension guide,
see [`docs/HARNESS_FULL_REPORT.md`](docs/HARNESS_FULL_REPORT.md).
For the machine/actionable capability layering model, see
[`docs/CAPABILITY_SYSTEM.md`](docs/CAPABILITY_SYSTEM.md).

Not included in the public main path:

- old Studio frontend/API;
- generated `runs/`, `outputs/`, `artifacts/`;
- `agent-docs/` working notes;
- local asset dumps or private dataset materializations;
- API keys, ModelScope tokens, GitHub tokens, or `.env`.

## Quickstart

Use Python 3.13.

```bash
python3.13 -m unittest discover -s tests -p 'test*.py'
python3.13 scripts/harness_list_capabilities.py
python3.13 scripts/harness_smoke.py --backend fallback
```

Run one fallback case:

```bash
python3.13 scripts/harness_run_case.py \
  cases/billiards/low_speed_single_contact.json \
  --backend fallback \
  --output-root runs/harness_cases
```

Generate dynamic billiards cases:

```bash
python3.13 scripts/harness_generate_cases.py \
  --suite billiards \
  --count 20 \
  --seed 42 \
  --out cases/generated/billiards_seed42
```

Run a batch:

```bash
python3.13 scripts/harness_run_case_batch.py \
  cases/generated/billiards_seed42 \
  --backend fallback
```

Verify a run:

```bash
python3.13 scripts/harness_verify_run.py runs/harness_cases/low_speed_single_contact_fallback
```

## UE Rendering

The UE path is the production renderer. Fallback is only for deterministic
debugging of schema and verifier behavior.

Set the required paths:

```bash
export SIM_STUDIO_UE_PROJECT="$PWD/ue_template/SimulatorStudioTemplate.uproject"
export SIM_STUDIO_UE_EXECUTABLE="/Users/Shared/Epic Games/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd"
export SIM_STUDIO_UE_MAP="/Game/Maps/MarketEnvironment/Maps/Day.Day"
export SIM_STUDIO_UE_ACTOR_CLASS="/Script/Engine.StaticMeshActor"
export SIM_STUDIO_ASSET_REGISTRY="$PWD/assets/asset_registry.example.json"
export SIM_STUDIO_UE_CONTACT_EXPORT=1
export SIM_STUDIO_UE_RUNNER_CMD="python3.13 scripts/harness_local_ue_runner.py"
```

Stable UE multi-view data pass:

```bash
SIM_STUDIO_UE_RENDER_MODE=both \
SIM_STUDIO_UE_RGB_CAPTURE_BACKEND=scene_capture \
SIM_STUDIO_UE_WIDTH=1280 \
SIM_STUDIO_UE_HEIGHT=720 \
SIM_STUDIO_UE_FPS=60 \
python3.13 scripts/harness_run_case.py \
  cases/falling/falling_block_on_floor.json \
  --backend ue \
  --mode both \
  --views front_static,side_static,top_down,tracking_subject,event_closeup \
  --render-passes rgb,depth,segmentation \
  --output-root runs/ue_smoke
```

Expected artifact layout:

```text
runs/<run_id>/
  manifest.json
  inputs/{case.json,scene.json,camera.json,render_config.json}
  passes/rgb/video.mp4
  passes/data/{depth.exr,mask.png,instance.json}
  sync/{camera_trajectory.json,physics_trace.json,sync_report.json}
  views/<camera_id>/{rgb.mp4,depth.exr,segmentation.png,meta.json}
  trajectory.json
  contact_events.json
  render_sync_report.json
  verifier_report.json
  run_readiness.json
```

Current renderer status:

- `scene_capture` RGB/depth/segmentation multi-view path is the stable data path.
- `highres_viewport` is kept as a debug path and can fail in headless/offscreen
  UE because editor screenshot frames are not produced reliably.
- Production high-quality RGB should move to Movie Render Queue / Level Sequence,
  while data passes stay on SceneCapture and share the same camera trajectory.

## Capabilities

The harness separates reusable pipeline-stage capabilities from physics case
families. Billiards is one smoke family under generic contact causality, not an
agent-facing compiler capability.

| Pipeline Capability | Current Role |
|---|---|
| `prompt_case_capability_planning` | Maps prompt/task intent into capability layers, case family, required signals, and failure taxonomy. |
| `asset_intent_resolution` | Classifies physics-critical vs visual-only assets. |
| `asset_runtime_binding_invocation` | Resolves top-k asset candidates and binds selected real assets or analytic proxies into runtime actors. |
| `scene_spec_compilation` | Builds runtime scene contracts from capability/case/assets. |
| `static_scene_placement` | Validates object ids, transforms, support relations, non-overlap, camera coverage, and physics graph membership before runtime. |
| `physics_property_constraint_validation` | Checks mass, friction, restitution, damping, gravity, material, and parameter-sweep constraints. |
| `explicit_physics_control_surface` | Represents gravity/material/rigid-body/constraint/force/time controls as typed replayable fields. |
| `capability_runtime_artifact_bridge` | Adapts runtime artifacts into verifier inputs. |
| `canonical_signal_capture` | Keeps trajectory, contacts, camera paths, RGB/depth/segmentation, and render metadata on one timebase. |
| `physics_verifier_truth_gate` | Makes verifier evidence the readiness source of truth instead of UI preview or render success. |
| `dataset_artifact_packaging` | Packages only readiness-gated artifacts with lineage, hashes, and signal availability. |
| `pipeline_stage_orchestration` | Keeps capability planning, case spec, scene layout, asset binding, runtime, verifier, diagnosis, and dataset packaging as explicit stages. |

| Physics Capability | Current Role |
|---|---|
| `rigid_body_contact_causality` | Active bodies may move; passive rigid bodies must remain still until runtime contact evidence. Billiards/pool is one case family. |
| `sequential_contact_propagation` | Domino/chain activation order must be contact-driven. |
| `rigid_body_gravity_collision` | Falling bodies should descend and contact support. |
| `ramp_sliding_friction` | Rolling/sliding bodies on an inclined plane must respond to gravity and friction. |
| `projectile_gravity_motion` | Thrown bodies must show launch, apex/descent, forward displacement, and landing/contact evidence. |
| `bounce_restitution_ball` | Bouncing rigid bodies must descend, contact support, and rebound within a restitution-bounded height envelope. |
| `rolling_friction_ball` | Rolling rigid bodies must maintain support contact, slow down, and travel within a friction-bounded distance envelope. |
| `sliding_crate_friction` | Sliding rigid bodies must maintain support contact, decelerate within stop-distance bounds, or stay still below static-friction threshold. |
| `force_field_wind_drift` | Wind/force-field driven light bodies must declare an explicit wind vector and drift along it within bounded displacement and altitude ranges. |
| `mass_ratio_momentum_transfer` | Contact-driven rigid bodies must declare mass labels and produce post-collision velocity ordering consistent with mass ratio and restitution. |
| `angular_damping_spin_decay` | Spinning rigid bodies must declare angular velocity and damping, then show monotonic spin decay in angular velocity and rotation trace evidence. |
| `agent_rigidbody_action_coupling` | Agent or controller actions must be explicit action traces, and target rigid bodies may move only after action/contact or release/impulse evidence. |
| `constraint_distance_pendulum_motion` | Distance/joint-constrained rigid bodies must preserve anchor-body length within tolerance and export constraint trace evidence. Pendulum is one smoke family. |
| `constraint_momentum_transfer` | Constrained rigid-body chains must transfer impulse through ordered adjacent contacts; terminal receiver motion must be contact-driven. Newton's cradle is one smoke family. |
| `elastic_energy_launch` | Elastic stored-energy release must declare spring/compression/mass labels, export release events, and keep post-release kinetic response inside the stored-energy envelope. Spring launch is one smoke family. |
| `elastic_constraint_rebound` | Elastic tether or bungee-style constraints must export rest length, extension trace, max-stretch bounds, and rebound velocity toward the anchor. Bungee is one smoke family. |
| `brittle_impact_fracture` | Brittle/destructible bodies must declare fracture threshold, contact impact energy, fracture events, and fragment evidence. Glass panels, mirrors, cups, and crates are case families. |

`billiard_causality_compiler` is not an active capability. If the legacy JSON is
present, treat it only as a compatibility alias for old artifacts. New agents
should use reusable invariants such as `rigid_body_contact_causality`,
`mass_ratio_momentum_transfer`, or `brittle_impact_fracture` depending on what
must be verified.

The old billiards failure mode is still preserved as a regression: plausible
videos can be faked by giving passive bodies hidden velocity. The verifier
rejects that by requiring passive bodies to start still and move only after
runtime contact evidence.

Planner output is layered. `CapabilityPlanner.plan(prompt)` returns a primary
physics capability plus:

- `capability_layers.pipeline_stages`
- `capability_layers.physics_constraints`
- `capability_layers.asset_operations`
- `capability_layers.verification`
- `supporting_capabilities`

Agents should call these stage capabilities in order rather than treating a case
family as a template.

## Customization Points

Agents can safely customize:

- case JSON under `cases/`;
- template parameter ranges under `cases/templates/`;
- capability contracts under `capabilities/`;
- asset registry path through `SIM_STUDIO_ASSET_REGISTRY`;
- camera list through `--views`;
- render passes through `--render-passes`;
- UE render size/FPS through `SIM_STUDIO_UE_WIDTH`, `SIM_STUDIO_UE_HEIGHT`,
  `SIM_STUDIO_UE_FPS`;
- RGB backend through `SIM_STUDIO_UE_RGB_CAPTURE_BACKEND=scene_capture`.

Agents should not commit generated media, local asset downloads, `runs/`, or
secret-bearing config.

## Validation Gates

```bash
python3.13 -m py_compile \
  run_experiment.py \
  scripts/harness_generate_cases.py \
  scripts/harness_local_ue_runner.py \
  scripts/harness_run_case.py \
  scripts/harness_run_case_batch.py \
  scripts/harness_verify_batch.py \
  scripts/native_ue_physics_phenomena_scene.py

python3.13 -m unittest discover -s tests -p 'test*.py'
git diff --check
```

## Known Next Work

1. Replace `highres_viewport` with MRQ/Level Sequence for paper-quality RGB.
2. Finish true rigid-body gravity advancement in the UE SceneCapture path.
3. Expand generated case coverage for contact, constraint, gravity, friction, force-field, fracture, and agent-action capabilities.
4. Add asset import tooling for ModelScope/GitHub-hosted asset registries without
   committing large assets to git.
