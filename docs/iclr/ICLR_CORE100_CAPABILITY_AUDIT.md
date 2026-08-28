# ICLR Core100 Capability Audit

## Audit result

The draft roster is structurally valid: 100 unique cases, the 80/10/10 domain quota and 85/15 input quota are exact, and pilot orders 1–10 are contiguous. The machine check is:

```text
python3 scripts/validate_iclr_core100_registry.py \
  --root /Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/experiments/iclr_core100_v1
```

All 100 rows have been mapped to a current capability family. This is a contract-level audit, not proof that every CaseSpec compiles or every real backend run passes. After the approved pre-freeze replacements, readiness is 56 tier A, 38 tier B, and 6 tier C.

## Family mapping

| Roster family | Cases | Primary current contract | Audit state |
|---|---:|---|---|
| gravity, projectile, bounce | R001–R012 | `rigid_body_dynamics`, explicit initial state, contacts and atomic trajectory assertions | present |
| collision/contact chain | R013–R026 | `rigid_body_dynamics`, native contacts, mass/material declarations and ordered assertions | present |
| friction, rolling, spin | R027–R040 | `rigid_body_dynamics`, material friction, damping and angular trajectory | present |
| stability, support, inertia | R041–R054 | `rigid_body_dynamics`, explicit support geometry/contact graph | present; translating-support schedules need an isolated smoke |
| constraints and elasticity | R055–R070 | V2 rigid constraints, linear/angular drives, damping and break thresholds | present; tier B/C drives need real Chaos smoke |
| forces, fracture, material | R071–R080 | rigid contacts/materials and generic bounded continuous force are present | R071–R072 passed the reusable real-Chaos force smoke; R079 needs fracture smoke |
| fluid | F001–F010 | `fluid_particle_dynamics`, Genesis SPH truth and UE replay | contract present; selected branch smoke required |
| articulated | A001–A010 | `articulated_body_motion`, fixed mannequin, explicit pose/root/attachment/ragdoll | present; A010 release path exists but needs real UE smoke |

## Pre-freeze issues

1. `R034` is now `rolling_ball_crosses_rough_strip_and_slows`; explicit inertia tensor and hollow-body support are not implemented.
2. Magnetic `R073`–`R075` are replaced by centered/off-center impact and spinning ballistic rod cases using current rigid contracts.
3. Resolved: `R071`–`R072` retain wind semantics through one reusable declarative continuous-force contract. Its real-UE smoke passed with 29 native force-trace frames and 1.28999 m measured x displacement.
4. `R050` and `R051` require a translating support with a time schedule. Current constraints/drives may express bounded motors, but a generic start/stop kinematic schedule has not yet passed an isolated V2/Chaos smoke.
5. `F009`, `F010`, `R066`, `R070`, `R079`, and `A010` remain tier C until an isolated contract/backend smoke passes; no evaluated Job may start while its row is tier C.
6. Every image-text row remains blocked for evaluated use until its exact image bytes and upload authorizations are frozen.

## Pilot readiness

| Order | Case | Contract evidence | Immediate blocker before development Job |
|---:|---|---|---|
| 1 | R001 steps | rigid contacts and bounce are current | none; use controlled stage |
| 2 | R013 dominoes | ordered native contacts are current | none; use procedural boxes |
| 3 | R011 barrel | cylinder rolling/contact is current; qualified warehouse barrels are registered | condition image; assemble a warehouse stage or keep the predeclared controlled stage |
| 4 | R010 pendulum impact | rigid constraint and contact are current | HomeInterior load/observability smoke if using that Map |
| 5 | R036 spinning top | angular damping/contact is current | condition image and qualified top visual asset |
| 6 | R048 tower pull | support/contact graph is current | condition image; explicit force/motion declaration must compile without a case-specific path |
| 7 | R061 spring cart | linear constraint drive/release contract is current | condition image; real Chaos drive smoke |
| 8 | R016 billiards | multi-contact rigid execution is current | condition image and historical asset requalification |
| 9 | F001 coffee spill | fluid transfer contract exists; HomeInterior cup visual is registered | condition image and selected-branch Genesis/container-boundary/cache/UE smoke |
| 10 | A001 turn and hold | fixed mannequin root rotation and attachment exist | condition image and real UE attachment/overlay smoke |

The first executable sequence should therefore begin with R001 and R013. Image-conditioned pilots are not converted to text-only for convenience because that would change the frozen input-mode quota and experimental unit.
