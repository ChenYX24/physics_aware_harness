# ICLR Core100 Protocol

## Objective

Core100 evaluates Physics-Aware Harness as an agent-controlled system that compiles text or image-text requests into auditable physics video episodes. The first production pass creates one independent candidate for every case. A later pass adds two independent candidates per case so the same frozen roster supports VTS@1, VTS@3, candidate pass rate, and AllPass@3.

The benchmark measures the agent and Harness together. A valid result therefore requires more than an MP4: it must preserve the request, native Agent submission, immutable CaseSpec revision, compilation and asset lineage, real backend evidence, synchronized observations, deterministic verification, Quality Gate, and the fixed review protocol.

## Frozen experimental unit

One Core100 case is one stable input condition plus one executable physical contract. A case may share an asset family with another case, but it counts separately only when its event structure, initial condition, material/constraint regime, or required physical outcome is materially different.

The following do not create a new case by themselves:

- changing only the background Map;
- changing only a camera;
- changing only a render seed;
- changing a scalar while preserving the same qualitative contract;
- re-running a failed job;
- producing candidate 2 or candidate 3 for VTS@3.

Historical jobs and videos are development evidence only. Even representative cases that were previously completed must be created again as clean Core100 jobs after the protocol and roster are frozen.

## Roster and input quotas

The initial roster has exactly 100 cases:

- 80 rigid-body cases, including contacts, gravity, friction, stability, constraints, forces, and bounded fracture;
- 10 simple fluid cases;
- 10 simple articulated-body cases;
- 85 text-only inputs;
- 15 image-text inputs whose image identity is frozen before the evaluated run.

Deformable bodies are not a hard quota in Core100 v1. They may be reported as a separate capability demonstration. A roster item cannot be replaced because a baseline or evaluated model performs poorly on it.

Before freeze, replacement is allowed only when a read-only capability audit or a clean pilot proves one of the following:

1. the required backend primitive is not implemented on the selected source branch;
2. the required asset cannot be licensed or qualified before the freeze deadline;
3. the case cannot expose its required evidence through the current versioned artifact contracts;
4. the case duplicates another executable physical contract.

Every replacement records the old ID, reason, evidence, replacement ID, and approval. The domain and input-mode quotas must remain unchanged unless the user explicitly amends the protocol.

## Candidate protocol

Development and evaluated candidates are strictly separated.

### Development

- Run 10 named pilot cases first.
- Debug runs, importer probes, software smokes, and asset qualification runs do not count as candidates.
- Fixes must be expressed through generic contracts, compiler behavior, backend adapters, verification, or evidence handling. Runtime and verifier code must not branch on case ID, directory, prompt word, or phenomenon name.
- A pilot may be repeated until the system is ready, but no development artifact is silently promoted into the evaluated set.

### Evaluated pass 1

- Run candidate index `1` for all 100 frozen cases using clean Jobs.
- Use the frozen request and image identity.
- Do not reuse a pilot Job or its mutable context.
- Record every attempted case, including infrastructure failures and user-blocked asset requests.

### Evaluated pass 2

- After `100 x 1` is complete, create candidate indices `2` and `3` independently.
- Candidate independence applies to the Agent Job and native generation submission. Deterministic UE replay of the same CaseSpec is a reproducibility check, not another candidate.
- No prompt or requirement changes are allowed between candidate indices.

## Agent model configuration

The first evaluated agent configuration is:

```text
provider: OpenAI
model: gpt-5.6-sol
reasoning_effort: high
reasoning_mode: standard
```

The exact model slug is pinned instead of relying only on the mutable `gpt-5.6` alias. Each invocation must produce an experiment receipt containing the configured provider, model slug, reasoning effort/mode, Codex version, thread identity, Job identity, candidate index, start/end time, and available token/cost usage. This receipt is experiment evidence and does not replace Controller-owned budget truth. Controller `agent_reported` metadata remains explicitly agent-reported.

Later Claude, DeepSeek, and Kimi runs use the same frozen inputs, Controller, backend configuration, asset catalog, technical gates, and reviewer policy. Provider-specific tool adapters may differ, but their permissions and budget envelopes must be documented before evaluation.

## Reviewer and evaluation separation

- Deterministic verifier, render sync, and Full Quality Gate remain the primary machine gates.
- Semantic Reviewer configuration is fixed across evaluated agent models and reported separately.
- Semantic Reviewer is not treated as the evaluated planning agent and cannot override a technical failure.
- Human evaluation reports realism and physical correctness separately and uses blinded randomized presentation.

## Validity and completion

An evaluated candidate is successful only when the existing Job Controller reaches `completed`. In particular, it must include:

- valid native generation context, submission, ack, Intent Contract, and CaseSpec V2;
- one Runtime Compiler transaction and exactly one Asset Resolve per compilation;
- real configured backend execution, not fallback or a declared-initial-state preview;
- required trajectories, contacts/events, camera state, RGB, depth, segmentation, and synchronization evidence;
- Candidate verifier, render sync, and Full Quality Gate pass;
- complete Evidence Bundle and valid fixed-policy Semantic Review pass;
- artifact completeness and target publication eligibility.

`awaiting_semantic_review`, `local_preview` produced by an ignored release gate, an RGB-only run, a fallback run, or an infrastructure-successful but physics-invalid video is not a completed evaluated candidate.

## Background policy

The physical experiment remains explicit CaseSpec geometry. A prepared Map supplies visual environment geometry, lighting, and distant context; it must not silently become a support surface, collider, force, or source of task events.

Every Map must pass Catalog registration, provenance/license checks, materialization, UE load, actor-count, camera observability, lighting normalization, depth/segmentation, and render-sync smoke. When a prepared Map is not qualified, the case stays blocked or uses its predeclared controlled stage. It never silently falls back to an unrelated Map.

## Storage and authority

Git stores protocol, code, schemas, and handoff documentation. Experiment state and all runtime data live under:

```text
/Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/experiments/iclr_core100_v1
```

Authority order is:

1. immutable Job Controller artifacts and authoritative leaf Stage Result;
2. external `experiment_manifest.json` and `registry/core100_cases.csv`;
3. generated status snapshots and dashboards;
4. Markdown summaries.

Markdown and dashboards never drive runtime routing or completion decisions.

## Initial pilot set

The pilot set intentionally re-runs the representative scenarios already used during development:

1. ball bouncing down steps;
2. straight domino chain;
3. barrel rolling off a table;
4. pendulum ball striking a block;
5. spinning top slowing and falling;
6. lower block pulled from a tower;
7. compressed spring launching a cart;
8. billiards break;
9. coffee spilling from a cup;
10. mannequin turning while holding an object.

The first eight are rigid-body cases, the ninth is fluid, and the tenth is articulated. Their purpose is to validate the current branch and experiment workflow before expanding to the full roster.
