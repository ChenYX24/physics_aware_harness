# Agent Usage

Use this repository as a CLI/toolkit harness. The frontend is not the main
interface.

## Agent-Facing Tool Flow

1. Discover capabilities.
2. Select or generate case specs.
3. Resolve asset intent and registry paths.
4. Run fallback for schema/verifier debugging or UE for real artifacts.
5. Read verifier/report JSON.
6. Repair case/backend/capability based on diagnosis.
7. Package dataset-ready artifacts.

## Commands

List capabilities:

```bash
python3.13 scripts/harness_list_capabilities.py --json
```

Run smoke:

```bash
python3.13 scripts/harness_smoke.py --backend fallback
```

Run one case:

```bash
python3.13 scripts/harness_run_case.py \
  cases/billiards/low_speed_single_contact.json \
  --backend fallback \
  --output-root runs/harness_cases
```

Generate cases:

```bash
python3.13 scripts/harness_generate_cases.py \
  --suite billiards \
  --count 20 \
  --seed 42 \
  --out cases/generated/billiards_seed42
```

Run batch:

```bash
python3.13 scripts/harness_run_case_batch.py \
  cases/generated/billiards_seed42 \
  --backend fallback
```

Run UE:

```bash
python3.13 scripts/harness_run_case.py \
  cases/billiards/six_ball_triangle_low_speed.json \
  --backend ue \
  --mode both \
  --views front_static,side_static,top_down,tracking_subject,event_closeup \
  --render-passes rgb,depth,segmentation
```

## Paths Agents Must Fill

Required for UE:

| Variable | Meaning |
|---|---|
| `SIM_STUDIO_UE_PROJECT` | Absolute path to `.uproject`. |
| `SIM_STUDIO_UE_EXECUTABLE` | `UnrealEditor-Cmd` or `UnrealEditor`. |
| `SIM_STUDIO_UE_MAP` | UE map package path. |
| `SIM_STUDIO_UE_ACTOR_CLASS` | Actor class used by the runner, usually `/Script/Engine.StaticMeshActor`. |
| `SIM_STUDIO_ASSET_REGISTRY` | JSON asset registry with physics-critical metadata. |
| `SIM_STUDIO_UE_CONTACT_EXPORT` | Must be `1` for UE verification. |
| `SIM_STUDIO_UE_RUNNER_CMD` | Usually `python3.13 scripts/harness_local_ue_runner.py`. |

Optional:

| Variable | Meaning |
|---|---|
| `SIM_STUDIO_UE_WIDTH` / `SIM_STUDIO_UE_HEIGHT` | Render resolution. |
| `SIM_STUDIO_UE_FPS` | Frame rate and physics trace sampling target. |
| `SIM_STUDIO_UE_RENDER_MODE` | `rgb`, `data`, or `both`. |
| `SIM_STUDIO_UE_RGB_CAPTURE_BACKEND` | Use `scene_capture` for stable synchronized output. |
| `SIM_STUDIO_UE_GRAPHICS_ADAPTER` | Non-negative UE adapter index for a shared multi-GPU host. |

## What To Read After A Run

Primary files:

- `run_readiness.json`
- `verifier_report.json`
- `render_sync_report.json`
- `manifest.json`
- `trajectory.json`
- `contact_events.json`
- `views/<camera_id>/meta.json`

Decision rules:

- `render_sync_report.status=pass`: RGB/depth/segmentation alignment is valid.
- `verifier_report.status=pass`: physics causality invariant passed.
- `run_readiness.reference_ready=true`: both rendering and physics verifier gates passed.

## Capability Layers

Prompt planning returns a primary physics capability plus supporting stage
capabilities. For a contact/collision prompt, the agent should expect:

- `prompt_case_capability_planning`: prompt -> capability/case family.
- `asset_intent_resolution`: object roles -> top-k asset candidates.
- `asset_runtime_binding_invocation`: selected asset/proxy -> runtime actor.
- `scene_spec_compilation`: capability + case + assets -> executable scene.
- `explicit_physics_control_surface`: typed gravity/material/rigid-body/force controls.
- `physics_property_constraint_validation`: parameter ranges and sensitivity checks.
- `capability_runtime_artifact_bridge`: runtime outputs -> verifier trace.
- `physics_verifier_truth_gate`: machine-readable pass/fail and diagnosis.
- `dataset_artifact_packaging`: readiness-gated dataset package.

Physical-property prompts should map to reusable constraint capabilities, not
object templates. Examples: `rolling_friction_ball` for rolling stop distance,
`sliding_crate_friction` for static/dynamic friction, `mass_ratio_momentum_transfer`
for post-contact velocity ordering, and `angular_damping_spin_decay` for angular
velocity / damping / rotation-trace checks. Constraint prompts should use
`constraint_distance_pendulum_motion`: the runtime must export anchor/body
trajectory plus `constraint_trace`, and the verifier checks distance preservation
and no teleporting body. Constrained impulse-chain prompts should use
`constraint_momentum_transfer`: the runtime must export ordered adjacent contact
events, mass labels, `constraint_trace`, and terminal receiver velocity.
Agent-interaction prompts should use `agent_rigidbody_action_coupling`: the
agent action must be an explicit `action_trace`, and target rigid-body motion
must happen after action/contact or release/impulse evidence.
Elastic launch prompts should use `elastic_energy_launch`: spring/catapult-like
examples are smoke families, while the reusable invariant is stored energy,
explicit release event, still payload before release, and bounded post-release
kinetic response.
Elastic rope or bungee prompts should use `elastic_constraint_rebound`: bungee is
only a smoke family, while the reusable invariant is rest length, bounded
extension, constraint trace, and rebound velocity toward the anchor.
Brittle or destructible prompts should use `brittle_impact_fracture`: glass,
mirror, cup, and crate breakage are asset/case families. The reusable invariant
is contact impact energy above threshold, post-contact fracture event timing,
and fragment manifest evidence.

## Generic Contact Causality Rule

Do not use `billiard_causality_compiler` for new work. It is not an active
capability contract and is kept only as a deprecated alias when reading old
artifacts. Use reusable invariants such as `rigid_body_contact_causality`,
`mass_ratio_momentum_transfer`, or `brittle_impact_fracture` depending on the
physical behavior you need to verify.

The old billiards failure was caused by passive target balls receiving hidden
initial velocity. The generic rule is:

- active bodies may have initial velocity or impulse;
- passive bodies must start below velocity epsilon;
- passive movement must be caused by contact events;
- expected collision graph edges must appear in `contact_events`;
- visually plausible motion without trajectory/contact evidence is not accepted.

Use `cases/billiards/negative_hidden_target_velocity.json` as one regression
case for this generic rule.
