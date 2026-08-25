# Articulated Animation V1

## Goal

Add the minimum generic human-animation path needed for declared animation clips, root motion, Character Movement and hand/foot IK. The runtime must consume structured CaseSpec fields and must never select behavior from request text, case IDs, object IDs or action-name keywords.

## Contract

`articulated_body` keeps one fixed model and declares two orthogonal sources:

```json
{
  "type": "articulated_body",
  "model": "harness_ue_mannequin_v1",
  "mode": "kinematic",
  "pose_source": {
    "type": "pose_keyframes | animation_sequence"
  },
  "root_transform_source": {
    "type": "root_keyframes | animation_root_motion | character_movement"
  }
}
```

- `pose_keyframes` contains the existing component-space joint keyframes.
- `animation_sequence` contains explicit timed segments. Each segment references a registered animation asset ID and declares start/end time, play rate and loop behavior.
- `root_keyframes` contains the existing root position and rotation offsets.
- `animation_root_motion` gives the selected animation exclusive ownership of root translation and rotation.
- `character_movement` contains explicit timed path keyframes and movement limits. It drives one `ACharacter`; an in-place animation sequence still provides the skeletal pose.
- Optional `ik_targets` are a post-animation pose layer. Each target names one fixed hand or foot goal and supplies timed world-space transforms plus weight.
- Optional `pose_overlay` is a weighted additive local-rotation layer on registered non-root bones. Optional pole positions remain fields of their corresponding hand/foot IK chain; one optional head-look target is evaluated in the same Control Rig layer.
- Existing attachment declarations remain explicit object/bone/time/local-frame constraints.
- Ragdoll remains an explicit mode/time transition and is never an automatic fallback.

The compiler accepts exactly one pose source and one root-transform source. Root translation and rotation always share the same owner.

## Required cross-field rules

- `animation_root_motion` requires `pose_source.type=animation_sequence` and a qualified clip whose metadata proves root motion is present.
- `character_movement` requires `pose_source.type=animation_sequence`; its clips must be in-place and animation root motion must be disabled.
- `pose_keyframes` initially pairs only with `root_keyframes`.
- Animation segments must be ordered, bounded by scene duration and reference assets compatible with the fixed mannequin skeleton.
- IK goals are fixed contract identifiers, not prompt/action aliases. Unsupported goals fail validation.
- No source is inferred and no missing source falls back to another source.

## Fixed assets

The versioned mannequin bundle contains:

- skeletal mesh, skeleton and PhysicsAsset;
- qualified idle, walk, run and jump Animation Sequences compatible with that skeleton;
- at least one qualified clip with real root translation/rotation for the root-motion smoke;
- one minimal Control Rig for left/right hand and foot goals.

CaseSpec references stable asset IDs. UE package paths are compiled metadata, not Agent-authored runtime routing.

## UE execution

- Preserve the current `PoseableMeshComponent` path for declared pose keyframes.
- Add one generic `ACharacter` path with capsule, skeletal mesh and `CharacterMovementComponent` for animation playback, root motion and Character Movement.
- Animation playback, root-motion mode and Character Movement are configured explicitly from the compiled contract.
- For control layers, a hidden non-rendering skeletal component evaluates the animation sequence while the single visible mesh remains owned by Control Rig output. Bone-local overlay rotations are composed onto that pose before `Backwards Solve` derives coupled controls; `Pre Forwards Solve` then writes IK/head commands. Visible bones are sampled after `Post Forwards Solve`.
- Runtime changes to the `ACharacter` mesh offset are committed through `CacheInitialMeshOffset`; the first Walking tick must preserve the grounded mesh root instead of restoring the constructor-time offset.
- Readability-stage helpers are render-only and remain non-colliding in both editor and PIE worlds, so grounding is determined only by the declared support body.
- Ragdoll changes the same skeletal body to Chaos only at the declared time.

## Commanded and observed evidence

Commanded contract data and UE-observed execution data are separate artifacts. Execution truth is sampled post-tick, after animation, root motion, Character Movement and Control Rig have evaluated.

Each observed articulated sample records:

- capsule and actor/root world transforms;
- selected bone world transforms;
- animation asset ID, animation time and playback state;
- `movement_mode` and observed velocity;
- actual per-frame `root_motion_delta` translation and rotation;
- IK target transform, observed effector transform and error;
- Control Rig lifecycle events, immediate command readback and separate post-solve bone output;
- ragdoll state and attachment state where applicable.

Verifier and Quality Gate use only observed data to prove execution. Commanded values may define expected tolerances but may not be copied into observed fields.

## V1 verification

- Runtime binding proves the expected actor class, skeletal mesh/skeleton, animation asset, capsule, Character Movement and optional Control Rig binding.
- Animation time advances consistently inside active segments.
- Exactly one root-transform owner is active.
- Character Movement path and velocity remain within declared tolerances.
- Root-motion clips produce observed deltas and the capsule/root follows them.
- IK effector error remains within the declared tolerance when target weight is nonzero.
- Frame 0 and the first Walking tick preserve the initialized capsule and visible mesh root within the declared movement bound.
- Post-ragdoll samples come from Chaos, not scripted pose data.

## Non-goals

V1 does not add navigation, AI pathfinding, Motion Matching, arbitrary skeleton retargeting, IK Rig retarget assets, a general AnimBP state machine, action-text matching, case-specific adapters, automatic pose synthesis or runtime fallback.

## Acceptance

Focused software tests cover contract combinations and compilation. Real UE smokes cover: in-place clip playback, Character Movement walking, root-motion execution, hand IK plus attachment, and explicit ragdoll transition. A natural-language Job is created only after those isolated smokes pass.

When a CaseSpec does not explicitly request a catalog map, UE uses the controlled runtime stage; the preflight/startup map is not reused as scene geometry. Smokes that require visible subjects request instance segmentation, and each camera's declared `target_objects` must have observed mask pixels. This is geometry-based and does not depend on characteristic colors or action names.

Compiled and UE camera rotations use the shared `(pitch, yaw, roll)` convention.

## Current implementation checkpoint

- Implemented: versioned orthogonal contract, fixed idle/walk/run/jump assets, generic `ACharacter`, Character Movement path command, post-tick observed evidence, binding checks and articulated Quality Gate.
- Implemented asset work: the fixed UE 5.7 mannequin bundle now materializes its matching mesh, skeleton, PhysicsAsset, animations and `CR_Mannequin_Body`; no second mannequin or runtime fallback is retained.
- Pending asset work: one qualified root-motion sequence. Root-motion declarations remain rejected until that fixed asset exists.
- Pending execution evidence: a real UE smoke for pose overlay, hand/foot IK with optional pole, head look, one-time support alignment and zero-time frame-0 evaluation.
- Current execution evidence: the isolated Character Movement walk smoke passes with the fixed UE 5.7 Manny bundle.
