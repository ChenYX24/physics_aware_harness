# ICLR Core100 Capability Audit

## Audit result

The draft roster is structurally valid: 100 unique cases, the 80/10/10 domain quota and 85/15 input quota are exact, and pilot orders 1–10 are contiguous. The machine check is:

```text
python3 scripts/validate_iclr_core100_registry.py \
  --root /Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/experiments/iclr_core100_v1
```

All 100 rows have been mapped to a current capability family. This is a shared-capability audit, not proof that every final CaseSpec will pass without case-level composition and tuning. After the 2026-09-03 real-UE angular-drive and fracture-threshold smokes, the current registry contains 51 tier A, 43 tier B, and 6 tier C cases.

## Family mapping

| Roster family | Cases | Primary current contract | Audit state |
|---|---:|---|---|
| gravity, projectile, bounce | R001–R012 | `rigid_body_dynamics`, explicit initial state, contacts and atomic trajectory assertions | present |
| collision/contact chain | R013–R026 | `rigid_body_dynamics`, native contacts, mass/material declarations and ordered assertions | present |
| friction, rolling, spin | R027–R040 | `rigid_body_dynamics`, material friction, damping and angular trajectory | present |
| stability, support, inertia | R041–R054 | `rigid_body_dynamics`, explicit support geometry/contact graph | present except R050–R051: no generic time-keyframed translating-support motion contract |
| constraints and elasticity | R055–R070 | V2 rigid constraints, linear/angular drives, damping and break thresholds | present; real UE position/velocity angular-drive smoke passes for R066/R070 |
| forces, fracture, material | R071–R080 | rigid contacts/materials, generic bounded force, and threshold-gated Geometry Collection fracture | force and both below/above-threshold fracture smokes pass; final glass/crate Geometry Collections still need preparation |
| fluid | F001–F010 | `fluid_particle_dynamics`, Genesis SPH truth and UE replay | contract present; selected branch smoke required |
| articulated | A001–A010 | GASP CharacterMovement/Motion Matching for natural motion; fixed mannequin pose/ragdoll for controlled experiments | idle/walk/run/jump/crouch, moving turns, stationary turns through 65 degrees, fixed-facing world-target gaze, and fixed bone attachment are qualified; simultaneous independent gaze/body turns, exact stationary 180-degree turn, true grasp, and A010 release remain unqualified |

## Pre-freeze issues

1. `R034` is now `rolling_ball_crosses_rough_strip_and_slows`; explicit inertia tensor and hollow-body support are not implemented.
2. Magnetic `R073`–`R075` are replaced by centered/off-center impact and spinning ballistic rod cases using current rigid contracts.
3. Resolved: `R071`–`R072` retain wind semantics through one reusable declarative continuous-force contract. Its real-UE smoke passed with 29 native force-trace frames and 1.28999 m measured x displacement.
4. `R050` and `R051` require a translating support with a time schedule. Source and schema audit confirms that pure-rigid V2 currently has no such generic CaseSpec field, so these two rows remain tier C pending one small shared capability addition and a real smoke.
5. `R066` and `R070` are qualified by `job_iclr_smoke_angular_drive_v1_004`: the position-driven body reaches `62.0143 degrees`, the velocity drive reaches `1.998136 rad/s`, and both constraints retain zero measured locked-axis residual. `R076`–`R079` share the threshold-fracture path qualified by positive and negative real-UE smokes; R079 moves from C to B because its remaining work is asset preparation rather than runtime capability.
6. Of the nine rigid text-image rows, the exact R011 condition image is present and eight remain to be supplied or authored before those individual Cases are frozen. Authorization/provenance bookkeeping is not treated as a physics-capability blocker.

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
| 10 | A001 turn and hold | grounded GASP, fixed attachment, SceneCapture, bounded stationary turns, moving turns, and fixed-facing gaze pass separately | closed capability-limited after visual rejection: the requested natural head-led 180-degree composite and true grasp are outside the current adapter scope |

The first executable sequence should therefore begin with R001 and R013. Image-conditioned pilots are not converted to text-only for convenience because that would change the frozen input-mode quota and experimental unit.
