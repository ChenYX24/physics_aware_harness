# UE Setup

This harness uses Unreal Engine as the production renderer and signal source.
Fallback is for verifier/debug development only.

## What "Setup" Means

Setup is a clean-machine onboarding test, not a list of tools already installed
on the current developer workstation. Starting from a cloned repository and an
empty external workspace, an operator must be able to:

1. validate Python 3.13, FFmpeg/ffprobe, Unreal Engine 5.7, and the native plugin
   build environment;
2. initialize the Git-external workspace;
3. import or mount an operator-supplied, licensed asset source and build its
   registry, dependency groups, map catalog, and thumbnails;
4. create/open the content-only Unreal project and load `ADPPhysicsRuntime`;
5. run a real UE/Chaos smoke case and produce synchronized RGB, metric-depth,
   instance-segmentation, solver-state, contact, and camera-state artifacts; and
6. pass `scripts/harness_evaluate_run.py` without fallback or silent asset/map
   substitution.

The current status is **PARTIAL**. The repository now provides one
`bootstrap` entry point and one fail-closed `doctor`, while a dependency lock
and a verified second-machine cold start remain open. Asset bytes and their
license/entitlement evidence are deliberately not stored in Git and must be
supplied by the operator.

## Clone, Bootstrap, and Doctor

```bash
git clone https://github.com/ChenYX24/physics_aware_harness.git
cd physics_aware_harness

export SIM_HARNESS_WORKSPACE="$HOME/SimulatorWorkspace/physics_aware_harness"
python3.13 scripts/harness_workspace.py bootstrap \
  --adp-content /absolute/path/to/AgenticDataPlatform/Content
python3.13 scripts/harness_workspace.py doctor \
  --ue-executable /absolute/path/to/UnrealEditor-Cmd \
  --asset-content /absolute/path/to/AgenticDataPlatform/Content
```

`bootstrap` creates the Git-external workspace and materializes
`ue/SimulatorWorkspace.uproject` from `ue_template/`. Supplying
`--adp-content` mounts the operator-owned asset source; it does not copy those
assets into Git. `doctor` reports `contract_ready`, `ue_config_ready`, and
`ue_ready` separately. `ue_config_ready` validates the UE 5.7
`Build.version`, enabled `ADPPhysicsRuntime` source, operator asset packages,
and workspace Content mount. `ue_ready` additionally requires
`--native-smoke-run` pointing to a hard-gate-passing native UE run under
`workspace/cases`; an executable stub cannot unlock this state.

## Clean-Machine Acceptance

A setup is accepted only when a new empty workspace produces at least one fixed
and one moving camera view, each with RGB, per-frame depth EXR, per-frame
instance-segmentation EXR, trajectory/contact/camera state, and a passing
`quality_report.json`. A one-view, one-second low-resolution run may be used as
an installation smoke, but it is not the final setup acceptance case. The run
must identify the real UE runtime and initial-state Chaos solver; fallback or a
precomputed trajectory does not count.

## Required UE Version

The local runner has been exercised with UE 5.7. A macOS installation normally
exposes:

```text
/path/to/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd
```

Other UE 5.x versions may work, but the Python API and screenshot/MRQ behavior
must be revalidated.

## Repository UE Template

The repository includes:

```text
ue_template/
  SimulatorStudioTemplate.uproject
  Plugins/ADPPhysicsRuntime/
```

The plugin provides the runtime physics capture bridge used by the harness.
When opening the template project for the first time, let UE rebuild the plugin
if prompted.

## Configuration and Environment Variables

For Controller jobs, persist the importer as a strict argv array in `config/harness.json`:

```json
"ue_asset_importer": {
  "command": ["/absolute/path/to/python3.13", "/absolute/path/to/scripts/harness_ue_asset_importer.py"]
}
```

`SIM_HARNESS_UE_ASSET_IMPORTER_CMD` remains a higher-precedence compatibility override for temporary testing.

Set these before running `--backend ue`:

```bash
export SIM_HARNESS_WORKSPACE=/Users/cyx/SimulatorWorkspace/physics_aware_harness
export SIM_STUDIO_UE_EXECUTABLE="/Users/Shared/Epic Games/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd"
export SIM_STUDIO_UE_MAP="/Game/Maps/MarketEnvironment/Maps/Day.Day"
export SIM_STUDIO_UE_ACTOR_CLASS="/Script/Engine.StaticMeshActor"
export SIM_STUDIO_ASSET_REGISTRY="$PWD/assets/asset_registry.example.json"
export SIM_STUDIO_UE_CONTACT_EXPORT=1
export SIM_STUDIO_UE_RUNNER_CMD="python3.13 scripts/harness_local_ue_runner.py"
```

When `SIM_HARNESS_WORKSPACE` is explicitly set and its initialized `ue/SimulatorWorkspace.uproject` exists, the Harness resolves that project automatically. Set `SIM_STUDIO_UE_PROJECT=/absolute/path/Other.uproject` only to override it. An explicit override wins; missing or invalid projects still fail preflight.

The Provider importer command receives `--request` and `--result` from the Harness. The repository launcher starts a separate Unreal Editor process and imports the deterministic OBJ into
`$SIM_HARNESS_WORKSPACE/ue/Content/Generated/Provider`. It validates the saved StaticMesh dimensions,
LOD0 render sections, generated simple collision, package existence, and SHA-256 before returning a
runtime-ready binding. The generated package remains outside Git.

For a natural-language V2 smoke, configure the planning LLM variables as well, then run:

```bash
python3.13 scripts/harness_run_case.py \
  --prompt "Generate a 0.4 by 0.6 by 0.8 meter rigid box, drop it onto a floor, and render the collision." \
  --case-spec-version v2 \
  --case-id local_provider_box_smoke \
  --backend ue \
  --width 320 --height 180 \
  --views front_static \
  --render-passes rgb \
  --output-root runs/provider_ue_smoke \
  --video-root review/provider_ue_smoke
```

Acceptance requires the Provider batch to be fulfilled, the receipt lifecycle to end in
`runtime_bound`, `runtime_compilation_report.json` to report one Asset Resolve invocation, the selected
asset ID to start with `generated.local.box_mesh_v1.`, the UE backend report to record a real UE
invocation, and at least one non-empty published video. A compilation-only or fake-importer result does
not satisfy this smoke.

Optional render controls:

```bash
export SIM_STUDIO_UE_RENDER_MODE=both        # rgb | data | both
export SIM_STUDIO_UE_RGB_CAPTURE_BACKEND=scene_capture
export SIM_STUDIO_UE_WIDTH=1280
export SIM_STUDIO_UE_HEIGHT=720
export SIM_STUDIO_UE_FPS=60
export SIM_STUDIO_UE_RENDER_QUALITY=high
```

## Asset Registry

Do not commit large assets to this repository. Put generated local metadata in
the external workspace:

```text
$SIM_HARNESS_WORKSPACE/catalog/adp/asset_registry.local.json
$SIM_HARNESS_WORKSPACE/catalog/adp/asset_group_index.json
$SIM_HARNESS_WORKSPACE/catalog/adp/map_catalog.json
```

Then point the harness at it:

```bash
export SIM_STUDIO_ASSET_REGISTRY="$SIM_HARNESS_WORKSPACE/catalog/adp/asset_registry.local.json"
```

Physics-critical assets should provide:

- `ue_path`
- collider type
- mass
- material friction/restitution
- collision profile
- whether analytic proxy is allowed

Visual-only assets can be omitted from the physics graph.

## Run One UE Case

```bash
python3.13 scripts/harness_run_case.py \
  cases/billiards/six_ball_triangle_low_speed.json \
  --backend ue \
  --mode both \
  --views front_static,side_static,top_down,tracking_subject,event_closeup \
  --render-passes rgb,depth,segmentation \
  --output-root runs/ue_cases
```

## Output Contract

The UE backend must produce:

```text
case_spec.json
scene_spec.json
camera_plan.json
trajectory.json
contact_events.json
camera_trajectory.json
render_manifest.json
render_pass_manifest.json
render_sync_report.json
verifier_report.json
run_readiness.json
views/<camera_id>/rgb.mp4
views/<camera_id>/depth.exr
views/<camera_id>/segmentation.exr
views/<camera_id>/depth_frames/frame_*.exr
views/<camera_id>/segmentation_frames/frame_*.exr
views/<camera_id>/meta.json
```

Missing depth, segmentation, views, or sync should be a hard failure.

## Highres Viewport Contract

当前 native high-resolution path 会在同一个 solver frame 内逐个捕获声明视角，全部完成后才推进一次 physics。Runner 只接受 manifest 中 exact camera id 对应的 RGB 与 camera trajectory；不允许按位置回退。若当前启动模式无法生成 highres frame，必须 fail-clear，不能用其他视角或 SceneCapture RGB 静默替代。

## Current Physics Caveat

当前 16 球 Chaos initial-state path 的双视角 RGB/depth/segmentation 已通过同步与 metric depth 几何门。仍未完成的是 120 Hz 全 substep state callback、同一完整 trace 的 24/60 FPS 重采样，以及 Chaos 原生 contact normal/impulse；24 FPS render-boundary cache 不得冒充完整 120 Hz trace。
