# Core80 feedback diagnosis — 2026-09-09 D

This checkpoint records diagnosis only. No rejected CaseSpec or Harness behavior is changed before the user's approval. Production remains Controller-owned, serial in UE, and all UE/importer work uses the approved absolute wrapper.

## Human decisions and Candidate queue

- Every `R*` case not named in the current rejection list is accepted. R031 and R056 completed Candidate/Quality Gate/Evidence Bundle and compact curation during this audit; the remaining accepted R queue is `R079 -> R081 -> R082`, with R079 active.
- `C002`, `C009`, and `C012` are accepted. `C001`, `C003`–`C008`, `C010`, and `C011` require revision; the prior C001/C004 evaluated copies are no longer final truth and must not be counted as accepted results.

## Evidence-backed case diagnosis

- **R026**: s13 already contains a four-band, 24-facet-per-band frustum wall (96 ordinary box facets). The missing behavior is not a frustum collider capability. The Meshy visual funnel's true outlet silhouette does not match the invisible 9 cm physical outlet, producing apparent wall penetration. Regenerate/replace or rescale the visual funnel to the declared aperture and keep one aligned physical shell; no Harness change.
- **R036**: the top is not physically floating. Native top/platform contacts continue through the final frame with zero reported gap. The visible pointed shell is offset from its cylindrical collision proxy, and the lighting leaves a detached-looking shadow. Use the qualified asset convex collision or recalibrate the one collision offset; no Harness change.
- **R040**: this is not ground penetration. At 3 s the sphere is already at x=4.46 m while the landing apron ends at x=3.95 m; both bodies then leave all finite supports and free-fall to z=-49.34/-39.46 m by 6 s. Enlarge the hidden runout floor or shorten/damp the runout; no Harness change.
- **R049**: the exact 6-3-1 packing is stable before the declared pull. Through 1.25 s all ball-center drift is only millimetric; the force begins at 1.35 s and collapse becomes clear later. Friction is no longer the blocker. Make the removal visually legible by delaying/shortening the same physical pull; no Harness change or hidden support.
- **R051**: the crate's resolved visual x-size is 0.557432 m while its declared box collider x-size is only 0.46 m. The final physics tangency therefore looks like about 4.9 cm of wall penetration. Match the collider to resolved bounds or reduce the visual size; no Harness change.
- **R054**: the imported tire is authored with its axle on local Y, while the declared analytic cylinder uses local Z. This orientation mismatch causes tire/block interpenetration. Reuse the tire asset's qualified convex collision instead of the misoriented primitive; no Harness change.
- **R060/R061**: CaseSpec already places the visual cart endpoint on the near face (`frame_b.x=-0.22 m`), and solver telemetry keeps the fixed anchor fixed. The rendered spring nevertheless changes side/appears on both sides of the cart. This isolates a generic `axial_visual` world/local spline update defect rather than a spring-physics error. A Harness fix is required.
- **R067**: s11–s14 establish that Chaos releases the overloaded joint under this manual fixed-step path but exports neither `IsBroken`, `IsTerminated`, nor the broken delegate. Threshold-unit guessing was disproved and reverted. Keeping a truthful break-threshold case requires a generic Harness-managed force/torque threshold evaluator that explicitly breaks and latches the constraint state; otherwise R067 must remain blocked.
- **Magnet material**: the source base-color texture is red with white/silver pole tips. R081/R082/R084 explicitly set `fixed_material_color=true` red, which overwrites the source texture; the import is not inherently all-red. Remove that override to use the source material. No Harness change.
- **R068**: replacing the angular-stop scene with a raised pendulum striking a high-restitution wall uses existing hinge/contact/material parameters. This is a CaseSpec redesign only.
- **R084**: the wrong swing plane is explicit: magnets lie on X while the hinge frees Y, so motion occurs in the magnets' X-Z plane. Free X instead for the perpendicular Y-Z plane. The large yellow blocks are also diagnosed: legacy `native_ue_scene.py` special-cases an object named `pendulum_anchor`, discards its resolved 16 cm sphere, and renders an unscaled Engine cube. This needs the generic asset-first selection correction below, not an ID rename workaround.
- **R085**: every spring currently terminates at the slider center and the visual diameter is declared as 8 cm. Set the endpoint to the ball's lower surface and reduce spring cross-section to about 4–5 cm while retaining the existing 12 generic guides/springs. No Harness change.

## Minimal generic Harness changes proposed

1. **Resolved-asset precedence**: when a valid runtime visual asset is present, use it before old ID/behavior fallback meshes. This removes the `pendulum_anchor` cube without adding any new special case.
2. **Axial visual endpoint correctness**: build spline segments from the two current constraint-frame world points in a neutral world transform, keep the source mesh hidden, and emit endpoint telemetry. Physical constraint semantics remain untouched.
3. **Cloth surface contact**: retain the existing sphere/box/cylinder colliders, but evaluate edge midpoints and triangle centroids in addition to vertices and distribute each correction/velocity response barycentrically to the owning vertices. This addresses rigid edges passing through cloth faces without per-case routing or a new solver.
4. **Truthful managed break threshold**: at fixed physics steps, evaluate the declared generic constraint force/torque threshold; on crossing, explicitly break the constraint and latch/export the real transition. First qualify SI conversion and the slack/locked pre-break state with one capability smoke.

These are four narrow reusable corrections. They introduce no Case-ID branch, object-type dispatch, duplicate solver, timed fake break, or compatibility layer. All other feedback is ordinary asset/collider/layout/parameter work.
