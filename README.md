# Agentic Data Platform: Physics-Aware Harness

A production-oriented **physics-aware simulation harness for code agents**.

This repository provides a reproducible path from an agent task to executable
physics cases, UE runtime execution, synchronized artifacts, physics-aware
verification, and dataset-ready packaging. It is not a frontend-first demo and
not a generic prompt-to-video application.

## Core Contract

```text
agent task / prompt
  -> capability planning
  -> case spec or generated cases
  -> asset retrieval
  -> asset placement and runtime binding
  -> physics parameter control
  -> UE runtime / debug fallback runtime
  -> trajectory/contact/render artifact collection
  -> physics and render synchronization verification
  -> diagnosis and dataset-ready manifest
```

UE is the production renderer and physics runtime. The fallback backend is only
for deterministic schema, verifier, and CLI development.

## Repository Layout

| Path | Purpose |
|---|---|
| `harness/` | Core planning, assets, runtime, verification, and artifact modules. |
| `capabilities/` | Machine-readable capability contracts. |
| `cases/` | Golden cases and parameterized case templates. |
| `scripts/harness_*.py` | Agent-callable CLI tools. |
| `scripts/harness_local_ue_runner.py` | Harness-compatible local UE runner. |
| `scripts/native_ue_physics_phenomena_scene.py` | UE Python scene/capture script. |
| `ue_template/` | Minimal UE project and `ADPPhysicsRuntime` plugin source. |
| `assets/` | Public-safe example asset registry and asset docs. |
| `docs/` | Architecture, usage, schemas, UE setup, authoring, and report docs. |
| `tests/` | Regression coverage for capabilities, CLI, runtime, verifier, and artifacts. |

Generated outputs and private data must stay local:

```text
runs/
outputs/
artifacts/
cases/generated/
agent-docs/
_local_inputs/
assets/downloads/
assets/ue_imports/
assets/cache/
assets/*.local.json
```

## Install And Validate

Use Python 3.13.

```bash
python3.13 -m unittest discover -s tests -p 'test*.py'
python3.13 scripts/harness_list_capabilities.py
python3.13 scripts/harness_smoke.py --backend fallback
```

Run one debug fallback case:

```bash
python3.13 scripts/harness_run_case.py \
  cases/falling/falling_block_on_floor.json \
  --backend fallback \
  --output-root runs/harness_cases
```

Generate parameterized cases:

```bash
python3.13 scripts/harness_generate_cases.py \
  --suite billiards \
  --count 20 \
  --seed 42 \
  --out cases/generated/billiards_seed42
```

Run a generated batch:

```bash
python3.13 scripts/harness_run_case_batch.py \
  cases/generated/billiards_seed42 \
  --backend fallback
```

## UE Runtime Setup

Required environment variables for production UE execution:

| Variable | Meaning |
|---|---|
| `SIM_STUDIO_UE_PROJECT` | Absolute path to a valid `.uproject`. |
| `SIM_STUDIO_UE_EXECUTABLE` | `UnrealEditor-Cmd` or equivalent UE executable. |
| `SIM_STUDIO_UE_MAP` | UE map package path. |
| `SIM_STUDIO_UE_ACTOR_CLASS` | Blueprint/C++ actor class used by the runner. |
| `SIM_STUDIO_ASSET_REGISTRY` | JSON registry containing UE package paths and physical metadata. |
| `SIM_STUDIO_UE_CONTACT_EXPORT` | Must be `1` for reference-ready UE verification. |
| `SIM_STUDIO_UE_RUNNER_CMD` | Harness-compatible runner command. |

Recommended local configuration:

```bash
export SIM_STUDIO_UE_PROJECT="$PWD/ue_template/SimulatorStudioTemplate.uproject"
export SIM_STUDIO_UE_EXECUTABLE="/Users/Shared/Epic Games/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd"
export SIM_STUDIO_UE_MAP="/Game/Maps/MarketEnvironment/Maps/Day.Day"
export SIM_STUDIO_UE_ACTOR_CLASS="/Script/Engine.StaticMeshActor"
export SIM_STUDIO_ASSET_REGISTRY="$PWD/assets/asset_registry.example.json"
export SIM_STUDIO_UE_CONTACT_EXPORT=1
export SIM_STUDIO_UE_RUNNER_CMD="python3.13 scripts/harness_local_ue_runner.py"
export SIM_STUDIO_UE_RENDER_MODE=both
export SIM_STUDIO_UE_RGB_CAPTURE_BACKEND=scene_capture
export SIM_STUDIO_UE_WIDTH=1280
export SIM_STUDIO_UE_HEIGHT=720
export SIM_STUDIO_UE_FPS=60
```

Run one UE case:

```bash
python3.13 scripts/harness_run_case.py \
  cases/falling/falling_block_on_floor.json \
  --backend ue \
  --mode both \
  --views front_static,side_static,top_down,tracking_subject,event_closeup \
  --render-passes rgb,depth,segmentation \
  --output-root runs/ue_cases
```

## Capability Layers

Capabilities are reusable contracts, not scene names. Billiards, bowling,
dominoes, pendulums, glass breakage, and ramps are case families under generic
capabilities.

Important layers:

| Layer | Capabilities |
|---|---|
| Pipeline | `prompt_case_capability_planning`, `scene_spec_compilation`, `static_scene_placement`, `runtime_actor_placement_compilation`, `runtime_backend_execution`, `pipeline_stage_orchestration` |
| Assets | `asset_intent_resolution`, `asset_runtime_binding_invocation` |
| Physics controls | `explicit_physics_control_surface`, `physics_parameter_semantics`, `physics_property_constraint_validation` |
| Runtime bridge | `blueprint_function_invocation`, `capability_runtime_artifact_bridge`, `canonical_signal_capture` |
| Verification | `physics_verifier_truth_gate`, `render_signal_sync_validation` |
| Packaging | `dataset_artifact_packaging` |

`billiard_causality_compiler` is intentionally not an active capability. Old
artifacts may map it to `rigid_body_contact_causality` as a deprecated alias.

## Asset Retrieval And Placement

Asset handling is split into two separate operations:

1. `asset_intent_resolution`
   - converts case objects into typed asset intents;
   - retrieves top-k candidates from the configured asset registry;
   - classifies physics-critical, visual-only, skeletal, Blueprint/logic, and
     scene/map assets;
   - records selected asset or explicit analytic proxy fallback.

2. `asset_runtime_binding_invocation`
   - turns selected assets into runtime actor bindings;
   - requires physics-critical metadata: collider, mass or density, material,
     collision profile, and rigid-body behavior;
   - excludes visual-only assets from the physics graph;
   - produces deterministic input for UE actor spawning and physics setup.

Static placement and runtime actor binding:

```bash
python3.13 scripts/harness_build_static_scene.py \
  cases/bowling/bowling_pin_chain_contact.json \
  --output-dir runs/static_scene/bowling

python3.13 scripts/harness_compile_actor_placement.py \
  cases/bowling/bowling_pin_chain_contact.json \
  --output-dir runs/actor_placement/bowling
```

## Physics Parameter Semantics

`physics_parameter_semantics` tells agents what each physical control means and
how a change should affect runtime evidence. Examples:

| Parameter | Meaning | Expected Effect |
|---|---|---|
| `mass_kg` | Rigid-body mass. | Same impulse produces smaller velocity change for larger mass. |
| `inertia_scale` | Rotational inertia multiplier. | Higher value resists spin-up and spin change from torque. |
| `linear_damping` | Velocity-proportional linear drag. | Higher value shortens travel distance and slows faster. |
| `angular_damping` | Velocity-proportional rotational drag. | Higher value makes spin decay faster. |
| `friction_static` | Threshold resisting start of sliding. | Higher value requires larger force before motion starts. |
| `friction_dynamic` | Sliding friction after motion starts. | Higher value shortens sliding distance. |
| `restitution` | Impact bounciness. | Higher value increases rebound height or separation speed. |
| `collision_profile` | Engine collision behavior. | `NoCollision` should not generate contact events. |
| `constraint_stiffness` | Constraint/spring resistance. | Higher value reduces stretch and increases rebound force. |

The full machine-readable table is in
`capabilities/physics_parameter_semantics.json`.

## Blueprint And C++ Runtime Invocation

`blueprint_function_invocation` treats UE Blueprint, C++ plugin, and Python
function calls as first-class runtime actions. Physics-mutating calls must be
ordered, replayable, and logged.

Supported call families include:

- actor spawn and transform;
- mesh and material binding;
- collision and rigid-body setup;
- mass, damping, velocity, impulse, gravity, and collision profile assignment;
- ADPPhysicsRuntime body registration and capture;
- camera, depth, segmentation, and render capture.

Relevant UE-side components:

- `ue_template/Plugins/ADPPhysicsRuntime/`
- `scripts/harness_local_ue_runner.py`
- `scripts/native_ue_physics_phenomena_scene.py`

## Artifact Contract

Reference-ready runs should contain:

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

## Current Integration Status

- Fallback smoke and verifier development path are stable.
- UE backend has strict preflight and no silent fallback.
- UE backend now generates asset resolution, static scene layout, and runtime
  actor placement before runner invocation.
- Local UE runner can consume `runtime_actor_placement.json`.
- SceneCapture is the stable synchronized RGB/depth/segmentation path.
- High-quality RGB should move to Movie Render Queue / Level Sequence while
  sharing the same camera trajectory with data passes.
- No UE-specific MCP tool is currently available in this Codex session; only a
  non-UE MCP server is exposed. UE integration is therefore local CLI/script
  based for now.

## Main Docs

- `docs/HARNESS_FULL_REPORT.md`
- `docs/CAPABILITY_SYSTEM.md`
- `docs/AGENT_USAGE.md`
- `docs/UE_SETUP.md`
- `docs/ARTIFACT_SCHEMA.md`
- `docs/CAPABILITY_AUTHORING.md`
- `docs/PHYSICS_CASE_TARGETS.md`
