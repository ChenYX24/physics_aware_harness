---
name: run-physics-video
description: Create, inspect, advance, pause, resume, or cancel durable Physics-Aware Harness agent jobs from the normal Codex TUI through scripts/harness_agent_job.py and a job_id. Use when a user requests a physics simulation video or asks to continue an existing Harness job. Do not use for Harness source development or for direct legacy case, backend, batch, or review execution.
---

# Run Physics Video

Operate one durable Physics-Aware Harness job from the normal Codex TUI. Use the repository Controller as the control-plane truth; do not implement a second state machine in the conversation.

## Establish the job

Work from the repository root containing `harness/` and `scripts/harness_agent_job.py`. Keep the Catalog, jobs, runs, media, caches, and imported assets in the external Harness workspace. Never create runtime output in the Git worktree.

Before creating or resuming a job, display the secret-free effective control-plane configuration:

```bash
python3 scripts/harness_config.py inspect
```

Pass the same explicit configuration flags to this display command and `harness_agent_job.py` when overrides are needed. The display is advisory input for the Agent only; never feed its JSON back into the Controller. The Controller independently loads the same strict config contract, and its job inspection must report the same `effective_config_digest`. If the digests differ, stop as a configuration problem instead of choosing one as truth.

For an existing `job_id`, inspect it before doing anything else, including in a new Codex session:

```bash
python3 scripts/harness_agent_job.py inspect <job_id>
```

Pass `--workspace <external-workspace>` before the subcommand when `SIM_HARNESS_WORKSPACE` is not set. Treat the returned `harness_agent_job_inspection_v1` and its on-disk manifest as truth; conversation history and stdout events are not job state.

For a new request, create exactly one job. Preserve the user's text and image identities. Omit `--backend` unless the user explicitly requires one. Keep the default `reference` publication tier unless the user explicitly chooses `local_preview` or `diagnostic_only` in advance.

```bash
python3 scripts/harness_agent_job.py create --prompt '<immutable user request>'
```

New jobs use Agent-native generation by default. Do not pass `--generation-mode legacy` for a production job; it exists only for an explicit migration comparison and is never an automatic fallback.

Add image paths and authorization flags only when supplied by the current user request. For text-plus-image input, add `--planning-images-required` only when the user explicitly says planning depends on the image pixels; image-only input is projected to required automatically. Treat planning-model image upload, Meshy image upload, Semantic Reviewer image upload, external Provider use, and paid Provider submission as separate authorizations. Never infer one authorization from another. A planning-image blocker may be resumed only with `--allow-planning-image-upload`. Never enable paid submission without explicit approval; the default paid-submission budget is zero.

Report the new `job_id` immediately so a later session can resume it.

## Advance through the Controller

Let the Controller run L0 readiness and prepare the immutable native-generation context:

```bash
python3 scripts/harness_agent_job.py --jsonl advance-until-blocked <job_id>
```

Use JSONL for long operations. After the command stops or is interrupted, run `inspect` again. Read the manifest's `state`, `current_stage`, `blocker`, and `allowed_next_actions`, then use `current_leaf_stage_result`, which includes the authoritative leaf `harness_stage_result_v1` path and Controller-validated content. Do not search run directories or select a Stage Result by recency.

Controller commands that can launch a Provider, importer, UE, a simulation backend, or the isolated Reviewer are long operations. When invoking `advance-until-blocked`, `resume`, `apply-revision`, or `review` through a Codex shell tool, set its host `timeout_ms` to at least `2100000` (35 minutes); do not rely on the host's default 20-second timeout. The Controller's own bounded subprocess and budget limits remain authoritative. A host timeout is not a structured Job result.

Real UE importer and render child processes require execution outside the Codex workspace sandbox on macOS. For every Controller command that may reach a UE importer or UE render stage, request sandbox escalation for the entire Controller command before launching it. The Skill does not grant that permission. If the current session uses `approval_policy=never` or otherwise cannot request escalation, stop before running the Controller and tell the user to restart the project session with an interactive approval policy such as `codex -a on-request -s workspace-write`. Never consume a UE launch or retry merely to test whether the sandbox permits it, and never replace this targeted approval with a blanket `danger-full-access` instruction.

If a host command ends or times out and `inspect` reports `state=running`, use the read-only `interrupted_recovery` field. When `interrupted_recovery.available=true`, recover only through the audited Controller action:

```bash
python3 scripts/harness_agent_job.py recover-interrupted <job_id>
```

This action acquires the Job lock, reconciles Provider usage, writes an interrupted checkpoint and Stage Result, and returns the same Job to `paused_interrupted`. If the lock is still held, treat the Controller as active and do not force recovery. Inspect again, then call the permitted `resume` action with the long host timeout. Never infer stale-running state from elapsed wall time alone and never edit a manifest or lock file.

When generation stops with `native_generation_submission_required`, read only the `native_generation_context` path and copy the `native_generation_context_digest` returned by `inspect`. The context contains the immutable request, submission and Intent draft contracts, current CaseSpec contract, capability vocabulary, backend artifact I/O, and current examples. Generate one submission matching that context; do not invent a second schema or call the legacy planning LLM.

The submission contains exactly `intent_draft`, `case_spec`, and `agent_reported` plus its schema, Job identity, and generation-context digest. Report TUI thread/model/provider/turn information only when known; use `null` when unknown. These fields are audit declarations and are not Controller-observed usage. Report image input IDs as used only when the context marks planning images required and the Job authorization permits their use; optional images remain metadata-only.

For local procedural assets, the built-in recipes are exactly `box_mesh_v1`, `sphere_mesh_v1`, and `cylinder_mesh_v1`; their CaseSpec `shape_hint` values are exactly `box`, `sphere`, and `cylinder`. Express proportions and orientation through `approx_size_m` and UE `[pitch, yaw, roll]`, never by putting prose into `shape_hint`. When the user has not required local procedural generation, treat these recipes as an efficient option for matching primitives, not as a mandatory route; for non-primitive assets prefer an authorized Catalog/external/model source when it better matches the request. Do not claim that a custom procedural recipe exists when the Controller has not registered one.

Write the candidate submission to a temporary file outside the Git worktree and Job artifact tree, then submit it only through the Controller:

```bash
python3 scripts/harness_agent_job.py submit-generation <job_id> --submission '<temporary-submission.json>'
python3 scripts/harness_agent_job.py --jsonl advance-until-blocked <job_id>
```

The Controller validates the context digest, image-use declaration, Intent adjustment bounds, CaseSpec V2, request identity, backend constraints, and immutable ack. A rejected submission must be corrected at its source; never edit the context, ack, Intent Contract, or attempt by hand. Re-submitting the exact payload is idempotent; different content under an existing ack is rejected.

Make control decisions only from stable structured fields: `status`, `failure_class`, `failure_code`, `retryable`, `checkpoint_ref`, `artifact_refs`, `allowed_next_actions`, and `required_user_action`. Do not classify failures from traceback or message wording, and do not copy the schema, failure-code registry, retry loop, budgets, or checkpoints into this Skill.

## Choose the next action

- Continue only when the manifest permits `advance`.
- Submit native generation only when the manifest permits `submit_native_generation`.
- Let the Controller consume its transient retry budget. A transient failure or `paused_interrupted` resumes the same job and attempt from its checkpoint; never create a CaseSpec revision for it.
- If a terminal failed Job reports `failed_stage_retry.available=true`, request explicit user approval only after the external cause has been corrected. Then call `retry-failed <job_id> --reason '<correction>'`, inspect the audited reopening, and use the permitted `resume` action. This preserves the same Job, attempt, compilation transaction, Provider request identity, paid-submission ledger, and accumulated usage. Never use this action for a non-transient failure or to sample repeatedly for success.
- If `case_spec_contract_repair.available=true`, the Controller has proven that a deterministic Provider contract failure can be repaired through the returned exact `allowed_adjustments`. Revise only those listed source leaves to their declared enum values and call `resume` with `--revised-case-spec` and `--revision-reason`. This creates a new attempt and compilation while preserving the failed transaction and accumulated usage. Do not change the whole `objects` array, any unlisted object property, frozen assertion, backend, asset policy, or Provider route.
- If `configuration_recompile.available=true`, the corrected Map/UE project/Catalog configuration changes compilation identity and ordinary resume is intentionally insufficient. Call `recompile-after-config <job_id> --reason '<correction>'`, inspect the immutable receipt and archived compilation, then use the permitted `resume`. This action is only for an F3 Map configuration blocker; actor class, runner command, contact export, and runner asset-registry fixes continue through ordinary resume.
- If `reviewer_contract_retry.available=true`, a terminal schema-invalid Review was produced under an older Reviewer prompt/output contract. Obtain explicit user approval for exactly one additional Reviewer turn, then call `retry-review-after-contract-fix <job_id> --reason '<contract correction>'`, inspect the immutable old/new input-digest receipt, and explicitly call `review`. This action must reuse the current validated Evidence Bundle, preserve every prior invocation and usage counter, and never be used for semantic `fail`/`uncertain`, unchanged contracts, or repeated sampling.
- When blocked on configuration, credentials, authorization, budget, publication tier, or ambiguity, request only the structured `required_user_action`. Resume with the matching CLI option after the user supplies it. Never probe by submitting paid work or uploading an image.
- When `resume_with_revision` is allowed, inspect the immutable Intent Contract, current CaseSpec, attempt manifest, and cited artifacts. Propose the smallest source CaseSpec change and obtain user approval before materializing it. Keep every change inside `allowed_adjustments`; never weaken frozen assertions, backend constraints, asset policy, or publication tier. Supply both `--revised-case-spec` and `--revision-reason` to `resume`.
- When the Controller reaches `awaiting_semantic_review`, confirm that the Evidence Bundle stage completed, then explicitly run `python3 scripts/harness_agent_job.py review <job_id>`. Do not replace this action with an inline self-review. If an image authorization blocker is returned, request only `semantic_reviewer_image_upload` authorization, resume to the review boundary, and invoke `review` again.
- A semantic `fail` may expose `apply_revision_proposal`. In that case inspect the immutable review and Intent Contract, construct the smallest source CaseSpec change, and call `apply-revision` without another approval only when every changed path was suggested by that Review, is inside `allowed_adjustments`, and matches the reported repair layer. The Controller recomputes the diff, writes the proposal, and creates the new attempt. A semantic `uncertain`, an unsuggested or unlisted path, a frozen requirement, or an out-of-range value requires a user decision.
- Resolve an Intent ambiguity only with an explicit user decision and an immutable `--intent-amendment` matching the reported ambiguity identity.
- For capability missing, artifact corruption, execution provenance failure, or a Harness bug, stop the production job and present a reproducible development issue with artifact references. Do not edit Harness source to make that job pass.
- Call `cancel` only after the user explicitly cancels the job. Treat Esc/SIGINT as a resumable pause, not cancellation; do not cancel an already-submitted paid remote task merely because local polling stopped.

Use `resume --help` and the inspected `allowed_next_actions` to select supported resume flags. Never invent a Controller command or edit a manifest directly.

## Preserve the in-flight request

Do not apply ordinary new requirements to a running job. Defer them until the current task finishes. Accept input for the current job only when it answers a structured blocker or when the user has interrupted the job and is choosing its recovery action.

Never edit CaseSpec compiler outputs, receipts, checkpoints, stage results, Catalog rows, or run evidence by hand. Never invoke standalone case, backend, batch, review, Provider, importer, solver, or renderer entry points for a production Agent job. CaseSpec V2 and the Runtime Compiler remain the only runtime source and compilation path.

## Report status accurately

After every stop, report the `job_id`, current state and stage, structured blocker if any, budget/usage relevant to the next action, and durable artifact paths.

Treat `awaiting_semantic_review` as technical gates and Evidence Bundle passed but not completion. Only the Controller-managed app-server Reviewer may produce `semantic_review.json`; it must use a new thread and a restricted read-only Evidence Bundle root. Report `completed` only when the manifest itself reaches that state after semantic `pass`. Semantic `fail` and `uncertain` are not completion and must never be retried merely to sample for a pass.
