# External reference capture

Use this for a specific official HTTPS page or file that informs a case, prompt, asset decision, or benchmark protocol:

```bash
python3 scripts/harness_capture_reference.py \
  https://example.org/official-spec.pdf \
  --name official-spec.pdf \
  --usage-note "Defines the benchmark protocol" \
  --license-note "Review before redistribution"
```

The downloaded bytes and adjacent `*.reference.json` manifest are written to the external Harness workspace, not the Git repository. The manifest records source URLs without query strings, retrieval time, media type, byte count, and SHA-256.

This captures provenance; it does not treat a web source as verified physics truth or prove redistribution rights.
