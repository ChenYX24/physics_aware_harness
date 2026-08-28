# ICLR Core100 Working Context

## Current source line

```text
repository: /Users/laplace/phyawareharness/physics_aware_harness
base branch: rollback/job_6a887135
base commit: 74d0cf6 fix articulated overlay evaluation
working branch: feat/iclr-core100
```

This task must not be implemented on the outer handoff repository's history. The outer repository contains `PROJECT.md`, `CURRENT.md`, and research notes; the inner repository above is the formal source repository.

## Durable experiment paths

```text
experiment root:
  /Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/experiments/iclr_core100_v1

Job Controller workspace:
  /Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness

asset source root:
  /Volumes/TiPlus7100s/PhysicsAssetSource

workspace UE project:
  /Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/ue/SimulatorWorkspace.uproject
```

Large assets, Catalog bytes, Jobs, runs, images, receipts, reports, and dataset outputs remain outside Git.

## Architecture constraints that remain in force

- CaseSpec V2 is the only runtime source.
- Runtime Compiler is the only compilation entry.
- A compilation invokes Asset Resolve exactly once.
- Provider/importer output must enter Catalog, become qualified, and resolve before runtime binding.
- Observation Plan is the only camera/modality/signal execution input.
- Backend selection is capability/artifact-contract based, not phenomenon-name based.
- Runtime code, verifier code, and fallback behavior must never select a route from case ID, prompt keywords, dataset directories, or scenario labels.
- Explicit initial layout is truth. The compiler must not auto-arrange or auto-repair objects to make a case pass.
- Controller and leaf Stage Results own status, retry, budget, checkpoint, and completion truth.
- Existing completed historical Jobs are read-only and are never resumed as Core100 candidates.

## Current capability boundary

### Rigid bodies

UE/Chaos supports declarative rigid bodies, contacts, materials, constraints, drive stiffness/damping, break thresholds, fracture evidence, trajectory assertions, and synchronized UE evidence. Each proposed constraint or force case still requires a CaseSpec-level capability check before freeze.

### Fluid

Genesis SPH owns particle truth and UE may consume the versioned surface cache. The selected rollback-derived branch does not contain the later importer/SPH/collision-topology commits from the separate M5 feature line. Fluid cases are therefore limited to 10 simple candidates and must pass an early isolated solver/cache/replay smoke before freeze.

### Articulated body

The fixed `harness_ue_mannequin_v1` supports explicit idle, walk, run, jump, pose/root keyframes, Character Movement, pose overlays, hand/foot IK, head look, attachment, and an explicit ragdoll transition. Arbitrary skeletons, navigation, text-to-action routing, automatic pose synthesis, and runtime fallback are outside V1. The current branch contains the latest committed overlay evaluation fix at the experiment base.

## Prepared environments

### HomeInterior

```text
source:
  /Volumes/TiPlus7100s/PhysicsAssetSource/fab/environments/HomeInterior
content root:
  data/Content
bundle root:
  data/Content/Home_Interior
maps:
  /Game/Home_Interior/Maps/Home_Interior.Home_Interior
  /Game/Home_Interior/Maps/Home_Interior_Overview.Home_Interior_Overview
```

Static inventory found 335 UAssets, 105 meshes, and two Maps. It is a Fab content package rather than a standalone `.uproject`. The full `Home_Interior` bundle is materialized under the workspace UE project's `Content` directory. The Map passed exact-package load and actor-inventory qualification and is Catalog `runtime_ready=true`, `local_preview`; individual meshes still require the prepared real-UE scan before registration. User research-use and non-redistribution attestation is recorded externally.

### warehouse_low

```text
source:
  /Volumes/TiPlus7100s/PhysicsAssetSource/fab/environments/warehouse_low
```

Static inventory found 57 FBX meshes and 505 JPG textures across 85 top-level asset folders. It contains no `.uproject` or `.umap`; it is an asset collection, not a ready Map. All 57 FBX IDs now have resolved Quixel metadata in the external source manifest. It still requires batch import, material binding, real-UE scan, and explicit scene assembly before it can become a prepared environment.

## Required preparation artifacts

The external experiment root contains:

```text
experiment_manifest.json              frozen experiment-level configuration
registry/core100_cases.csv             complete 100-case roster
registry/asset_requests.csv            missing assets/provenance and user requests
registry/map_sources.json              source inventories and qualification state
status/status_snapshot.json            generated progress summary
inputs/images/                         frozen image conditions
receipts/                              model-launch and external-action receipts
reports/                               aggregate evaluations and audit reports
dashboard/                             optional generated read-only dashboard
```

## Agent handoff procedure

At the start of a new Agent session:

1. Read `PROJECT.md` and `CURRENT.md` from the outer repository in separate chunks.
2. Read this file and `ICLR_CORE100_PROTOCOL.md` completely.
3. Read `ICLR_PREPARATION_STATUS.md` for the compact current preparation checkpoint.
4. Confirm the inner repository is on `feat/iclr-core100` and inspect uncommitted changes.
5. Read the external experiment manifest, roster row for the current case, status snapshot, and authoritative Job inspect output.
6. Continue only the current case's declared next action. Do not infer status from directory timestamps or a previous conversation.

For a Job, use only the existing Controller CLI:

```text
scripts/harness_agent_job.py create
scripts/harness_agent_job.py inspect
scripts/harness_agent_job.py advance-until-blocked
scripts/harness_agent_job.py submit-generation
scripts/harness_agent_job.py resume
scripts/harness_agent_job.py review
scripts/harness_agent_job.py apply-revision
scripts/harness_agent_job.py cancel
```

Do not run a direct backend, legacy batch runner, or custom completion check as a substitute for Controller state.

## UE execution and user-terminal handoff

The current managed Codex environment may not reliably launch real UE. Software tests, compilation, read-only inspection, and artifact validation may run here. When a real UE smoke is required:

1. prepare an exact command with absolute repository, config, workspace, project, executable, Map, Job, and output identities;
2. explain expected runtime and success artifacts;
3. ask the user to run it in a normal macOS terminal;
4. inspect the resulting artifacts read-only before deciding the next action;
5. never ask the user to rerun without identifying what changed and whether it consumes Job budget.

## Queue and dashboard boundary

Do not build a general asynchronous queue before the first 10 pilots expose the real manual-intervention pattern. If scheduling becomes useful, implement a single UE worker with an exclusive resource lock that only invokes the existing Controller. Planning and asset preparation may be queued ahead of it, but no scheduler may duplicate generation, compilation, retry, verification, or completion logic.

An optional dashboard is read-only and generated from experiment snapshots. It is never another control plane.

## Current next actions

1. complete the HomeInterior individual-mesh scan in a normal user terminal;
2. import, scan, and register the warehouse collection, then rebuild the vector index;
3. replace R034 and R073-R075, implement/smoke the generic continuous external force, and freeze the capability-first list;
4. align the final asset gaps and pilot order with the user;
5. create clean pilot Jobs one at a time, beginning with R001 and R013.
