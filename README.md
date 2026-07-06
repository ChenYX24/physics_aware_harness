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

| Capability | Current Role |
|---|---|
| `billiard_causality_compiler` | Active cue ball may move; passive target balls must remain still until contact. |
| `sequential_contact_propagation` | Domino/chain activation order must be contact-driven. |
| `rigid_body_gravity_collision` | Falling bodies should descend and contact support. |
| `asset_intent_resolution` | Classifies physics-critical vs visual-only assets. |
| `scene_spec_compilation` | Builds runtime scene contracts from capability/case/assets. |
| `capability_runtime_artifact_bridge` | Adapts runtime artifacts into verifier inputs. |

The billiards cases include the old failure mode that produced plausible-looking
videos by giving passive balls hidden velocity. The verifier rejects that:
passive balls must have zero initial velocity and only move after contact.

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
3. Expand billiards, domino, falling, ramp, pendulum, and rolling-friction cases.
4. Add asset import tooling for ModelScope/GitHub-hosted asset registries without
   committing large assets to git.
