---
name: physics-aware-harness
description: Operate and extend the Physics-Aware Harness for rigid-body, Genesis SPH fluid, and Unreal Engine rendering cases. Use when an agent needs to choose a simulation backend, run a CaseSpec, inspect reference-readiness or particle/surface quality gates, find review videos, add a capability/backend without silent fallback, or diagnose artifacts in the external harness workspace.
---

# Physics Aware Harness

Work from the repository containing `harness/`, `capabilities/`, and `cases/`. Treat executable contracts, tests, and reports as truth; use `ACTIVE_CONTEXT.md` only as the navigation anchor.

Runtime data lives outside Git. Resolve relative `runs/...` and `review/...` paths under `SIM_HARNESS_WORKSPACE` (default `~/SimulatorWorkspace/physics_aware_harness`); never recreate repository-root `runs/` or `videos/` directories.

## Choose the path

- Use `fallback` only for schema, CLI, and verifier development. Never call it physical truth or reference-ready.
- Use `ue` for validated rigid cases. Contact-causality cases pass only initial state to UE Chaos; MuJoCo is an explicit sweep/reference backend, never the default trajectory source.
- Use `genesis_sph` for `fluid_particle_dynamics`. It runs with `SIM_GENESIS_PYTHON` or `<workspace>/envs/genesis/bin/python`, preserves particle truth, and reconstructs per-frame OBJ surfaces.
- Read `harness/runtime/backend_policy.py` before routing a new phenomenon. Unsupported capabilities must remain explicit.

## Run the smallest case

Validate contracts first:

```bash
python3.13 -m unittest discover -s tests -p 'test_*.py'
python3.13 scripts/harness_list_capabilities.py
python3.13 scripts/harness_workspace.py status
```

Run a contract-only smoke:

```bash
python3.13 scripts/harness_run_case.py cases/falling/falling_block_on_floor.json --backend fallback --output-root runs/harness_cases
```

Run the canonical fluid slice:

```bash
python3.13 scripts/harness_run_case.py cases/fluid/fluid_drop_in_basin.json --backend genesis_sph --output-root runs/fluid_cases
```

Run a real UE rigid slice only after `SIM_STUDIO_*` is configured:

```bash
python3.13 scripts/harness_run_case.py cases/falling/falling_block_on_floor.json --backend ue --output-root runs/ue_cases
```

Custom single-run and batch probes default to RGB. For an editable variant plan,
use one no-LLM entry point; it materializes the selected CaseSpec and renders
all five views:

- For every new parameterized CaseSpec, write one `harness_variant_plan_v1`.
  The model only declares `base_case`, `case_route`, and `axes`; never generate
  case-specific HTML. `harness_case_library.py plan` automatically writes the
  standalone HTML beside its JSON, and `editor <plan>` regenerates it.

```bash
# RGB by default
python3.13 scripts/harness_case_library.py render \
  config/variant_plans/newton_cradle_release_angle.json \
  --variant release_angle-25deg

# Same solver-state workflow, sequential RGB and sensor passes
python3.13 scripts/harness_case_library.py render \
  config/variant_plans/newton_cradle_release_angle.json \
  --variant release_angle-25deg \
  --render-passes rgb,depth,segmentation
```

UE does not produce RGB, metric depth, and instance IDs from one identical
capture pass. One Harness command may request all three, but it must reuse the
same solver states across ordered RGB/data captures and preserve frame IDs.
Treat custom commands as probes. Publish a formal complete case only through
`harness_case_library.py render --formal` or the UE-only iterator:

```bash
python3.13 scripts/harness_iterate_case.py cases/domino/five_domino_chain.json \
  --backend ue --case-route rigid_collision/domino/v002_pitch_matrix \
  --condition initial_pitch_m20deg \
  --views front_static,side_static,top_down,tracking_subject,event_closeup \
  --render-passes rgb,depth,segmentation --mode both
```

## Enforce the complete-case contract

- Store internal source routes at `<workspace>/runs/case_routes/<physics>/<scenario>/<version>/`. Keep every invocation under `runs/<unique-session-id>/attempt_N`; only the best hard-gate passing attempt from one session becomes a registered source run. Build the user-facing hardlinked view at `<workspace>/cases/<case_id>/<variant_id>/`. Never hand-edit the index or relabel one resolved run directory as two runs.
- Require at least three selected source runs. Identical fingerprints form `exact_repeat`; differing inputs require one stable `--condition` per exact fingerprint and form `declared_condition_matrix`. Never rank different conditions as a quality winner.
- Require one shared acquisition fingerprint across compared runs: identical camera plan/pose, render resolution, FPS, duration, timebase, and per-view RGB frame counts. Use a new version route for a different acquisition context.
- Require the same declared camera set for every compared run: `front_static,side_static,top_down,tracking_subject,event_closeup` (three verified fixed plus two verified moving cameras). Require matching motion/meta RGB frame counts greater than one.
- Deliver every `variant × view × {rgb,depth,segmentation}` MP4, canonical Depth/Segmentation EXR frames under `variants/<variant>/<modality>/<view>/frames/`, three `variants/<variant>/overall/{rgb,depth,segmentation}.mp4` previews per variant, and three bundle-level `overall/{rgb,depth,segmentation}.mp4` previews.
- Treat native UE RGB MP4 as RGB truth. Treat per-frame depth/segmentation EXR as canonical sensor truth; their MP4s are review previews. Preserve EXR frame counts and aggregate SHA-256 provenance in the manifest.
- Keep `review_role` separate from `publication_tier`: `review_candidate` versus `diagnostic_probe` describes hard-gate/review workflow; `reference`, `local_preview`, `unverified`, or `rejected` describes license/provenance readiness. A `local_preview` may still be a valid review candidate.
- Use only `--backend ue --mode both` for formal iteration. Route fallback, MuJoCo, Genesis, and incomplete UE runs through explicit probes or backend experiments.

## Inspect before claiming success

- For reference-tier UE publication, require `run_readiness.json.reference_ready=true`. A technically valid local asset run may instead declare `local_preview_ready=true` and enter review with `publication_tier=local_preview`; never call it reference-ready. Inspect `verifier_report.json`, `render_sync_report.json`, `sensor_state.json`, `map_report.json`, and `asset_resolution.json`.
- For Genesis SPH, require `fluid_report.json.status=pass`, stable particle count, every surface frame present, topology consistency, and respected container bounds. Inspect `particle_cache.json` for solver/timebase truth.
- Confirm MP4 integrity with `ffprobe`. Visually inspect initial, interaction, and final frames; a passing structural report does not prove a convincing render.
- Find validated user-facing candidate bundles under `<workspace>/review/inbox/`; unvalidated one-off/batch previews default to `<workspace>/review/probes/`. Put durable source runs under `<workspace>/runs/case_routes/<physics>/<scenario>/<vNNN_description>/` via `--case-route`, then organize them into `<workspace>/cases/<case_id>/<variant_id>/`. Never copy runtime media into the Git repository.
- Move an accepted item with `python3.13 scripts/harness_workspace.py review keep <name>`; keep verifies the complete video hashes and source EXR provenance before moving it to `review/kept`. Reject with `review reject`; both decisions reject symlinks, require bidirectional candidate/case-status binding, update the manifest and case status, and preserve canonical source runs. The CLI serializes decisions and writes a durable recovery journal; after an interrupted decision, run the review command again so it recovers before proceeding. Never move candidate folders manually. Dry-run `prune` before deleting rejected review items.

Current local status, verified 2026-07-14:

- v1 legacy RGB is kept at `<workspace>/review/kept/v001_approach_angle_matrix_rgb_reference`; its current-Harness complete five-angle reproduction is also kept at `<workspace>/review/kept/v001_complete_reproduction_20260714` (33 videos). Do not claim the latter reconstructs the deleted historical runner.
- v2 is kept at `<workspace>/review/kept/v002_angle_matrix`; its SSOT is `<workspace>/cases/rigid_collision/billiards/v002_complete_angle_matrix/case_status.json` (33 videos).
- v3 `1.8/2.8/4.2 m/s` is kept as `local_preview` at `<workspace>/review/kept/v003_speed_matrix__ue__20260714T013712__attempt_01`; its SSOT is `<workspace>/cases/rigid_collision/billiards/v003_speed_matrix/case_status.json` (21 videos).
- The five-object true-Chaos domino v1 is kept as `local_preview` at `<workspace>/review/kept/five_domino_chain__ue__20260714T040227_731233000_68743_a781e31b__attempt_01` (39 videos under the frozen v1 delivery contract).
- Treat billiards v1/v2/v3 and domino v1 as regression baselines. The next formal slice is `rigid_motion/gravity_bounce_projectile/v001_restitution_matrix` with declared `restitution_0p35`, `restitution_0p60`, and `restitution_0p85` conditions under the five-camera delivery contract.

## Extend without weakening truth

- Add or edit the machine-readable capability and CaseSpec before backend code.
- Keep each external solver isolated behind a runtime backend and a canonical state cache.
- Add a verifier for state invariants before optimizing visuals.
- Preserve exact timebase and sampling phase; frame count need not equal 60.
- Reject silent backend substitution, placeholder sensor data, unlicensed assets, missing provenance, and visual-only collision proxies.
- Use `publish_complete_case_delivery` for formal review candidates. Reserve `ArtifactManager.publish_videos` for diagnostic probes; it does not satisfy the complete-case contract.
- Treat depth/segmentation MP4s as derived review previews only. Canonical validation reads per-frame depth/segmentation data and must reject constant depth or off-palette masks.

## External tools

- Do not install an Unreal/Blender skill or MCP based on popularity alone. Record license, supported UE version/platform, network binding, arbitrary-code tools, and a read-only smoke result first.
- Prefer an MCP for editor introspection, asset search, build, and test orchestration. Keep physics stepping and truth verification inside the harness.
- Never expose editor-control MCP ports beyond loopback.
