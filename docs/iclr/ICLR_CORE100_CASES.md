# ICLR Core100 Cases

## Source of truth

The complete machine-readable roster is:

```text
/Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/experiments/iclr_core100_v1/registry/core100_cases.csv
```

This Markdown file summarizes the roster; it does not duplicate or override all 100 rows.

Current roster state is `draft_pre_freeze`. It contains exactly 80 rigid, 10 fluid, and 10 articulated cases, including exactly 15 image-text conditions. Case IDs remain stable after freeze. Pre-freeze replacements follow `ICLR_CORE100_PROTOCOL.md` and cannot depend on evaluated model performance.

## Family allocation

| Domain | Families | Count |
|---|---|---:|
| Rigid | gravity/projectile/bounce | 12 |
| Rigid | collision/contact chains | 14 |
| Rigid | friction/rolling/spin | 14 |
| Rigid | stability/support/inertia | 14 |
| Rigid | constraints/elastic mechanics | 16 |
| Rigid | external forces/fracture/material response | 10 |
| Fluid | transfer/emitter/stirring/buoyancy | 10 |
| Articulated | simple fixed-mannequin motion/control/ragdoll | 10 |
| **Total** |  | **100** |

## Pilot order

| Order | Case ID | Case | Domain | Input | Initial background |
|---:|---|---|---|---|---|
| 1 | R001 | Ball bouncing down five steps | rigid | text | controlled stage |
| 2 | R013 | Twelve-domino straight chain | rigid | text | warehouse/controlled stage |
| 3 | R011 | Barrel rolling off a table | rigid | image-text | warehouse/home interior |
| 4 | R010 | Pendulum ball striking a block | rigid | text | home interior |
| 5 | R036 | Spinning top slowing and falling | rigid | image-text | home interior |
| 6 | R048 | Lower block pulled from a six-block tower | rigid | image-text | warehouse/controlled stage |
| 7 | R061 | Compressed spring launching a cart | rigid | image-text | warehouse/controlled stage |
| 8 | R016 | Fifteen-ball billiards break | rigid | image-text | controlled tabletop stage |
| 9 | F001 | Coffee spilling from a tilted cup | fluid | image-text | home interior |
| 10 | A001 | Mannequin turning while holding an object | articulated | image-text | home interior |

Image-text status means the final evaluated condition requires a frozen input image. A text-only development smoke may be used before that image arrives, but it cannot count as the evaluated candidate.

## Per-case lifecycle

```text
draft_pre_freeze
→ capability_audited
→ assets_ready
→ development_job_created
→ development_technical_pass
→ freeze_ready
→ frozen
→ candidate_1_created
→ candidate_1_terminal
→ candidate_2_created / candidate_3_created
→ complete
```

Blockers remain explicit states such as `needs_asset`, `needs_user_terminal`, `harness_bug`, `capability_missing`, or `model_submission_failed`; they are not collapsed into `failed` in the research registry.
