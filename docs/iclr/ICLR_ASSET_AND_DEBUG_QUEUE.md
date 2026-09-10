# ICLR Asset and Debug Queue

## Purpose

This document is the human-readable queue for assets, prepared Maps, and generic Harness defects encountered during Core100. Machine status lives under the external experiment root. A case-specific workaround is not an acceptable resolution.

## Prepared Map intake

| ID | Source | Intended role | Current state | Next evidence |
|---|---|---|---|---|
| MAP-001 | `HomeInterior` Fab content package | Home, office-like interior, tabletop and articulated background | Map load qualified; 102 meshes registered, 100 collision-ready | Observation smoke during the first Map-backed pilot |
| MAP-002 | `warehouse_low` FBX/JPG collection | Warehouse background and industrial props | 57/57 imported, scanned, collision-ready, and Catalog-registered | Assemble one reusable warehouse scene only when a selected case needs it |
| MAP-003 | controlled `BallBoardSmoke` | Existing minimal diagnostic stage | Already registered local generated Map | Retain for controlled pilot fallback only when predeclared; do not present as a prepared realistic environment |

## User-provided authorization

The user attests that HomeInterior, warehouse_low, and future user-supplied assets are legally obtained and may be used for research execution, generated videos, paper display, and internal training data; original asset files will not be redistributed. Exact input-image bytes and per-service upload permission are still frozen separately when a text-image case is prepared.

The corresponding attestation receipt is stored under the external experiment root.

## Debug classification

Every issue must be assigned one class:

- `case_spec`: invalid explicit scene declaration or unsupported requested primitive;
- `asset_missing`: required visual/physics asset is absent;
- `asset_qualification`: source, license, hash, scale, collision, dependency, import, or runtime binding is incomplete;
- `map_import`: prepared Map cannot be materialized, opened, observed, or normalized generically;
- `harness_bug`: compiler, Controller, backend, evidence, verifier, or Quality Gate violates its declared contract;
- `environment`: UE executable, project, plugin, Python environment, disk, credential, or permission issue;
- `model_output`: native Agent submission fails the frozen context/schema/contract without a Harness defect;
- `semantic_failure`: technically valid candidate fails or is uncertain under the fixed semantic protocol.

## Resolution rules

- Fix `harness_bug` in reusable code and add a regression test.
- Fix `case_spec` only through an allowed bounded revision or a new clean development Job.
- Never relax assertions, evidence, collision, provenance, or publication gates to obtain a pass.
- Never add a case ID, scenario name, prompt token, or asset-name switch to runtime or verifier code.
- Preserve every failed Job and artifact; do not overwrite it with a successful retry.
- When user action is required, report the exact asset or terminal command, why it is required, expected outputs, and whether it consumes budget.

## Pilot asset expectations

| Pilot | Main non-primitive assets | Expected source path |
|---|---|---|
| Ball down steps | staircase or explicit box steps | local procedural primitives initially |
| Domino chain | domino visual mesh optional | local procedural boxes initially |
| Barrel off table | barrel/cylinder and table | existing Catalog or user asset request |
| Pendulum block | ball, rod/visual, block | procedural primitives plus explicit constraints |
| Spinning top | qualified top mesh | existing Catalog or user asset request |
| Tower block pull | blocks and support | local procedural boxes |
| Spring cart | cart and spring visual | existing Catalog/procedural bodies; constraint drive is physics truth |
| Billiards break | table, balls, rails | existing completed-case assets require requalification check |
| Coffee spill | cup/container collision mesh | existing coffee cup source and Genesis/UE handoff check |
| Turn while holding | fixed mannequin and held object | fixed mannequin bundle plus explicit attachment |

## Current collection coverage and gaps

HomeInterior provides tables, chairs, shelves, books, cup, jug, bottle, glass, wineglass, bowl-like dish, bucket, sink, tap, ladder, kitchen props, and ordinary held objects. warehouse_low provides barrels, boxes, crates, drums, pallets, shelves/racking, workbench, hand truck, ladders, chain, beam, cable, spool, jerrycan, tank, tarp, and industrial dressing. Together with qualified analytic primitives, these cover most Core100 bodies and backgrounds.

Specialized assets still requested are: one open basket visual, one bowling pin, a physics-qualified funnel, a physics-qualified spinning top, one coil-spring axial visual, and qualified glass/wood Geometry Collections. The spring-cart body itself may be a generic compound cart; the spring constraint remains physics truth.

Strict post-registration retrieval probes pass for barrel, table, Chinese-language workbench, and hand truck, and correctly abstain for funnel, spinning top, and open basket. Registered cup/glass/dish/bucket assets are visual and ordinary rigid-collision evidence only; fluid containment topology remains part of the selected SPH smoke.

Do not request a separate realistic mesh where a primitive is the physical subject and a visual shell would add no paper value. Do request a qualified irregular asset when containment, center alignment, fracture topology, or recognizable appearance is central to the case.

## Open generic risks before pilot execution

1. Prepared Maps still need generic stage-origin and observation behavior validated by a Map-backed pilot; exact-package load is already qualified for HomeInterior.
2. The two visual-only HomeInterior meshes are environment shells and are not selected as physics bodies.
3. warehouse_low is a registered prop collection, not a prepared Map; assemble a reusable scene only when required by a selected case.
4. Fluid must be tested on the selected rollback-derived branch rather than inferred from the separate M5 line.
5. Articulated pose overlay/IK/head-look support is committed but some real UE control-layer evidence remains pending; the pilot uses the simpler turn-and-hold path first.

## Completed ordinary-terminal asset actions

These commands produced the retained scan/import receipts and did not create or consume Core100 Candidate Jobs.

Build and activate the updated ADPPhysicsRuntime plugin:

```bash
cd /Users/laplace/phyawareharness/physics_aware_harness
SIM_HARNESS_WORKSPACE=/Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness python3 scripts/harness_workspace.py build-ue-plugin --ue-executable /Volumes/TiPlus7100s/UnrealEngine/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd --max-parallel-actions 4
```

Scan HomeInterior meshes after the build exits:

```bash
python3 scripts/harness_ue_asset_inventory.py scan --package-root /Game/Home_Interior/Meshes --result /Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/experiments/iclr_core100_v1/receipts/assets/home_interior_mesh_scan.json --ue-project /Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/ue/SimulatorWorkspace.uproject --ue-executable /Volumes/TiPlus7100s/UnrealEngine/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd --timeout 900
```

Import the prepared 57-item warehouse batch, then scan only its isolated package root:

```bash
python3 scripts/harness_ue_asset_importer.py --batch-request /Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/experiments/iclr_core100_v1/receipts/assets/warehouse_low_import/batch_request.json --batch-result /Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/experiments/iclr_core100_v1/receipts/assets/warehouse_low_import/batch_result.json --ue-project /Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/ue/SimulatorWorkspace.uproject --ue-executable /Volumes/TiPlus7100s/UnrealEngine/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd --timeout 3600
python3 scripts/harness_ue_asset_inventory.py scan --package-root /Game/Imported/WarehouseLow --result /Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/experiments/iclr_core100_v1/receipts/assets/warehouse_low_mesh_scan.json --ue-project /Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/ue/SimulatorWorkspace.uproject --ue-executable /Volumes/TiPlus7100s/UnrealEngine/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd --timeout 900
```
