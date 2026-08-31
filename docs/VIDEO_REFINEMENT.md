# Video refinement

`scripts/harness_refine_video.py` reads `harness_video_refinement_job_v1` JSON. Ark defaults to Seedance 2.0 Fast; `--ark-model 2.5` switches a reference edit to the 2.5 adaptive contract. Credentials are read only from the caller's `ARK_API_KEY` environment variable and are redacted from dry runs and manifests.

```bash
python3 scripts/harness_refine_video.py job.json --manifest output.manifest.json
```

H3 FL2VA uses local port `30010`; Ref2VA uses `30011` or `30012`. The adapter rejects mismatched local ports. Ark references must be public HTTPS; H3 references may be absolute `file://` URIs or public HTTPS.

Existing manifests resume a recorded task ID instead of submitting a duplicate job. Success requires downloadable output and records the input, prompt, payload, and output hashes.

Frame-exact replacement uses a half-open interval `[start,end)` and preserves the original audio:

```bash
python3 scripts/harness_refine_video.py --splice \
  --source-video source.mp4 --replacement-video repaired.mp4 \
  --output-video final.mp4 --start-frame 54 --end-frame 222 \
  --mode replace --manifest final.splice.json
```

Generated-file success is not a physics pass. UE teachers and refined outputs remain subject to the declared teacher, identity, temporal, and physics gates.

## Synthesis constraint prompt

The model must receive one source of truth per concern:

- **UE/reference video = motion, camera, timing, contact, and final-state teacher.**
- **Original-video keyframes = appearance, identity, material, lighting, and background teacher only.**
- Do not pass the erroneous original as a second motion reference unless the experiment explicitly studies dual-video conditioning; it can overpower the UE motion and reproduce the original error.

Use this template for every model in a comparison, replacing only bracketed case facts:

```text
Create one continuous fixed-camera shot with no cut, transition, zoom, reframing,
mirroring, reset, or scene change.

REFERENCE PRIORITY
1. Treat the UE reference video as the immutable temporal and geometric backbone.
   Preserve its camera, duration, frame cadence, trajectories, contact order,
   support-loss timing, gravity-driven acceleration, collisions, settling, and
   final poses frame by frame.
2. Use the original-video keyframes only as an appearance and identity sheet.
   Match their scene, materials, colors, lighting, shadows, background, scale,
   and object identities. Never copy an erroneous motion or later state from
   those images.
3. If appearance conflicts with UE motion, causal timing, or identity/count
   conservation, preserve the UE motion and the declared identities/count.

TIMELINE
- [0.00, T0) s: preserve the original pre-error state and camera continuously.
- [T0, T1) s: follow UE frames [U0, U1) for the local physical correction:
  [case-specific action, first causal response, contacts, and expected fall].
- [T1, END] s: preserve the UE final state and transition continuously back to
  the original post-error appearance; do not jump, reset, or teleport.

HARD INVARIANTS
- Keep exactly [N] persistent whole objects: [ordered identities and colors].
- The first visible causal response begins at [event/time], while [support or
  actor] is still moving; no hovering or delayed fall after support loss.
- Preserve gravity-scale acceleration, collision response, and natural settling.
- Never add, remove, duplicate, split, merge, recolor, substitute, or hide an
  object. No ghosting, double exposure, dissolve, morphing, or identity swap.
- No white line, bar, rod, string, seam, guide, support proxy, mask boundary,
  dropped frame, flash, or control artifact.
- Preserve brightness and color distribution from the source keyframes; do not
  wash out highlights or darken the scene.

Return only the repaired video. If a requested appearance change would violate
motion, causal timing, continuity, or exact object conservation, skip that
appearance change.
```

For a whole-shot UE restyle, replace the three timeline bullets with: `Use the UE reference video for the entire output timeline; source keyframes constrain appearance only.` For a frame-exact local repair, keep the prompt interval consistent with the splice interval `[start_frame,end_frame)` recorded in the manifest.

### Model input contract

| Model | Motion input | Appearance input | Required run setting |
|---|---|---|---|
| MiniMax H3 Ref2VA | one validated UE video | 1--3 source keyframes | Ref2VA worker; generated audio off |
| Seedance 2.0 Fast | one validated UE video | 1--3 source keyframes | fixed duration and aspect ratio; generated audio off |
| Seedance 2.5 | one validated UE video | 1--3 source keyframes | adaptive reference edit; generated audio off |

Every H3/Seedance row for the same case must share the same prompt text, UE teacher, source keyframes, and seed where supported. Record the prompt hash, input hashes, task ID, endpoint, output hash, and `generated_pending_user_acceptance`; API success alone is not repair success.

## Reference projects

These are design references, not vendored dependencies:

| Project | Relevant idea | Boundary for this harness |
|---|---|---|
| [VideoCoCo](https://github.com/micky-li-hd/VideoCoCo) | executable physics proxy followed by video restyling; closest public analogue | its released toy data does not establish local repair or exact preservation outside an error interval |
| [GS-Agent](https://github.com/UMass-Embodied-AGI/gs-agent) | agent-built executable physical worlds with engine feedback | world construction, not repair of an existing generated video |
| [PhysInOne](https://github.com/vLAR-group/PhysInOne) | large physical simulation data suite and video-generation applications | dataset/benchmark reference; not a drop-in local refiner |
| [VACE](https://github.com/ali-vilab/VACE) | unified video creation/editing with reference and local controls | appearance and edit-control reference; no executable physics guarantee |
| [EasyV2V](https://snap-research.github.io/easy-v2v/) | video, mask, text, timing, and optional reference-image conditioning | useful future masked-repair baseline; current production interface is not integrated here |
| [AnyV2V](https://github.com/TIGER-AI-Lab/AnyV2V) | motion/layout preservation with image-based appearance editing | temporal preservation reference; no contact or conservation verifier |
| [TokenFlow](https://github.com/omerbt/TokenFlow) | cross-frame feature propagation for consistent edits | continuity reference; no causal physics contract |
| [Rerender A Video](https://github.com/williamyang1991/Rerender_A_Video) | keyframe translation plus temporal propagation and blending | style-transfer reference; no exact event timing guarantee |
| [CoCoCo](https://github.com/zibojia/COCOCO) | mask-localized, temporally consistent video inpainting | relevant when a reliable repair mask exists; not used to infer physical truth |
| [VideoComposer](https://github.com/ali-vilab/videocomposer) | compositional reference-video and motion control | conditioning reference; does not replace UE evidence or post-generation verification |
