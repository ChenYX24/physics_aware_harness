# ICLR Core100 Squash Handoff

This branch condenses the ICLR Physics-Aware Harness preparation work that was developed on `feat/iclr-core100` onto the original baseline `rollback/job_6a887135`.

## Scope

- Controller-owned durable video jobs, immutable attempts, bounded revisions, and compact evaluated-result curation.
- Prepared UE maps, local/Fab/Poly Haven/Meshy asset intake, asset inventory, and qualified runtime bindings.
- Rigid-body constraints, kinematic motion, continuous/radial/wind forces, fracture presentation, cloth replay, Genesis SPH replay, and the narrowed GASP character path.
- Formal Core80 roster/status documents, Smoke review batches, reference Candidates, and Fluid10 preparation.
- `layout-preview`: a Controller diagnostic that renders a few 640x360 RGB stills from the compiled production cameras without advancing the Job stage. It is for scale, placement, contact/grounding, background alignment, attachment gaps, and framing—not for judging dynamics.

## Current experiment state

- Formal rigid evaluated results: 78.
- R026 and R060 corrected Smokes pass machine verification and await human review in Review-G.
- R061 is curated as a reference Candidate.
- Fluid F001-F009 pass formal Smoke; F010 remains an explicit rigid-SPH hydrostatic-buoyancy capability hold.
- Completed selected Jobs retain RGB/depth/segmentation MP4 as required and no per-frame PNG/EXR.

Authoritative detailed status is in `docs/iclr/ICLR_PREPARATION_STATUS.md`. Runtime jobs, media, catalogs, caches, and evaluated artifacts remain under `/Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness`; they are intentionally not copied into Git.

## Operating constraints

- Manage every production Case through `scripts/harness_agent_job.py`; do not create a second workflow around it.
- Any operation that can reach UE or an importer must start with the approved absolute wrapper `/Users/laplace/phyawareharness/physics_aware_harness/scripts/run_ue_agent_job.sh`.
- Use one UE worker, keep Reviewer disabled unless explicitly re-enabled, and stop formal first runs at the Smoke boundary for human review.
- Keep source changes generic: no Case-ID routing, hidden compensating forces, assertion weakening, or workflow-specific fallbacks.
