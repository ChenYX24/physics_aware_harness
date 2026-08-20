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
