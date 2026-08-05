# Local Asset Provider Implementation Plan

## Objective

Implement the first unified Asset Provider path for CaseSpec V2 using one deterministic local
procedural 3D generator. The implementation must prove this invariant:

```text
V2 acquisition request
-> Provider fulfillment
-> hash/license/provenance receipt
-> materialize/import
-> Catalog registration and qualification
-> the single Asset Resolve invocation
-> scene/runtime binding
```

A Provider result is never a runtime asset. It may return only stable Catalog asset IDs and receipts;
Asset Resolve remains the only component allowed to select a runtime candidate.

## Scope and non-goals

In scope:

- a versioned provider contract and orchestrator;
- `procedural_generation` routing;
- one deterministic `box_mesh_v1` generator that writes a centered Wavefront OBJ using only local
  computation;
- a backend-import adapter contract and a command adapter for UE import;
- Catalog registration, existing qualification gates, receipts, failure artifacts, and tests;
- V2 Runtime Compiler integration before its one Asset Resolve call.

Out of scope:

- `external_site` and `model_generation` implementations;
- network access, paid APIs, MCP, Blender automation, or interactive UI;
- a second resolver, direct candidate injection, hard-coded `/Game/...` paths, or V1 behavior changes;
- automatic promotion to `reference` without explicit redistribution evidence.

## Required module boundaries

Add these modules (names are part of the handoff contract):

```text
harness/assets/providers/contracts.py
harness/assets/providers/orchestrator.py
harness/assets/providers/local_procedural_mesh.py
harness/assets/providers/backend_importer.py
```

`AssetRegistry` remains the facade. Add facade methods for idempotent asset registration and lookup by
stable asset ID; Provider code must not open SQLite directly. JSON registries remain read-only and must
return a structured `catalog_not_writable` blocker for Provider fulfillment.

## Pipeline integration

For V2, `compile_runtime_case()` must execute exactly this order:

1. Backend Planner.
2. V2 AssetIntent Compiler.
3. Provider Orchestrator.
4. `resolve_asset_intents()` exactly once.
5. Existing scene, verification, observation, and runtime-binding stages.

V1 must skip the Provider Orchestrator and retain current artifacts and behavior.

The orchestrator returns a result keyed by `(object_id, slot)`. A fulfilled result contains only
`catalog_asset_ids` and receipt references. Extend Asset Resolve to consume those IDs:

- fulfilled Provider route: load only those IDs through `AssetRegistry`, apply the existing quality
  gate, and let Resolver select deterministically;
- failed/blocked Provider route with explicit `local_catalog` fallback: perform the existing Catalog
  search and record `actual_route=local_catalog` and `route_honored=false`;
- failed/blocked route without that fallback: do not search the local Catalog;
- required route that is not fulfilled and selected must fail compilation before any backend call.

Do not increment `asset_resolve_invocation_count`; it must remain exactly one.

## Versioned contracts

Use these schema names:

```text
harness_asset_provider_request_v1
harness_asset_provider_result_v1
harness_asset_provider_receipt_v1
harness_asset_provider_batch_v1
harness_backend_asset_import_request_v1
harness_backend_asset_import_result_v1
```

Provider request required fields:

- deterministic `request_id` and `request_digest`;
- `case_id`, `object_id`, `slot`, requested route/requirement/origin;
- provider/source hints and reference-input metadata;
- compiled SearchIntent, target backend, required license tier;
- normalized generation spec containing `recipe_id`, `recipe_version`, `shape`, and `size_m`.

Provider result status is exactly one of `fulfilled`, `blocked`, or `failed`:

- `fulfilled`: non-empty registered `catalog_asset_ids` plus receipt IDs;
- `blocked`: deterministic unmet prerequisite such as unsupported route, missing writable Catalog, or
  missing importer;
- `failed`: attempted work failed validation or execution;
- blockers/failures include stable `code`, human-readable `message`, and `retriable` boolean.

Receipt required fields:

- provider ID/version and request identity/digest;
- recipe ID/version/parameters and generator source version;
- input identities and hashes;
- every output file's workspace-relative path, role, format, SHA-256, and byte size;
- source kind/URI, author when known, license and redistribution evidence;
- ordered lifecycle transitions;
- importer request/result digests and resulting backend binding;
- no credentials, image bytes, absolute paths from another machine, or unverified claims.

Persist `asset_provider_batch.json` and individual receipts under `provider_receipts/` in the run
directory. Generated binaries belong under:

```text
$SIM_HARNESS_WORKSPACE/providers/local_procedural_mesh_v1/<request_digest>/
```

They must never be written into the source repository.

## Lifecycle and qualification

The only successful lifecycle order is:

```text
requested
-> generated
-> hashed_and_license_recorded
-> normalized
-> materialized
-> imported
-> registered
-> qualified
-> runtime_bound
```

Never infer later states from Provider success. In particular:

- `materialized=true` requires the file to exist and match its recorded hash;
- `imported` requires a validated importer result;
- `registered` requires lookup of the same stable ID through `AssetRegistry` after registration;
- `qualified` requires the existing `asset_quality_gate()` result;
- `runtime_bound` requires a materialized, runtime-ready backend binding and complete dependencies.

The local generator defaults to `local_preview`. It may become `reference` only when a trusted
deployment policy supplies explicit, verifiable redistribution evidence accepted by the existing
license gate. A user's request by itself is not rights evidence. Tests may use repository-owned fixture
evidence; production code must not fabricate rights.

## Local procedural reference provider

Provider identity is `local_procedural_mesh_v1`. Initially support only:

```text
route = procedural_generation
recipe_id = box_mesh_v1
shape = box
size_m = three positive finite numbers
```

Generate a centered, consistently wound OBJ with deterministic vertex/face order and LF newlines. The
stable asset ID is `generated.local.box_mesh_v1.<first-24-hex-of-recipe-digest>`. Repeating the same
normalized request must produce the same ID, bytes, hash, and Catalog row without duplicates. Unsupported
shapes fail with `unsupported_generation_recipe`; they must not silently become boxes.

## UE importer adapter

Core software communicates with the external importer only through JSON request/result files. The
command comes from explicit configuration, not a hard-coded executable or project path. The adapter
must use an argv list, a bounded timeout, captured stdout/stderr receipts, and no shell execution.

If no importer is configured, return `blocked/backend_importer_unavailable`. A successful response must
be rejected unless it identifies the same request and asset, provides a valid `/Game/...` object path,
class name, materialization state, runtime-ready state, and dependency/file hashes. Register the binding
only after those checks. Missing UE configuration must never trigger the normal UE runner.

## Required tests

Add focused tests covering all of the following:

1. contract parsing rejects unknown schema versions, invalid states, and fulfilled results without IDs;
2. identical generation requests are byte-for-byte and ID-for-ID idempotent;
3. generated files are outside the repo and their receipt hashes match;
4. fake successful importer -> register -> lookup -> qualify -> Resolver selects the returned ID;
5. required route with missing importer fails before backend invocation;
6. tampered file, bad hash, invalid UE path, incomplete dependency, and LFS pointer all fail closed;
7. `reference` policy fails without accepted redistribution evidence and passes only with explicit test
   fixture evidence;
8. Provider-returned but unregistered IDs cannot be selected;
9. preferred Provider failure uses local assets only when `fallback_order` explicitly includes
   `local_catalog`;
10. `external_site` and `model_generation` remain structured unsupported blockers;
11. V1 invokes no Provider and preserves its golden artifacts;
12. every V2 compilation invokes Asset Resolve once, including Provider success and failure paths;
13. registration marks the vector index stale without rebuilding it during compilation;
14. two repeated compilations do not create duplicate assets or receipts with conflicting identities.

Use temporary workspaces and fake importers in automated tests. Do not require UE, network, external
assets, OpenCLIP downloads, or the production Catalog for unit tests.

## Acceptance commands and handoff evidence

Run with conda `base` (Python 3.13):

```bash
conda run -n base python -m unittest \
  tests.test_asset_provider_contract \
  tests.test_local_procedural_provider \
  tests.test_runtime_compiler_v2

conda run -n base python -m unittest discover -s tests
git diff --check
```

Before handoff, report:

- commit hash and clean inner worktree;
- focused/full test counts and environment skips;
- one successful fake-import receipt and one missing-importer failure artifact;
- proof that `asset_resolve_invocation_count == 1` on both paths;
- proof that generated files are in the temporary/external workspace, not Git;
- any ordinary-terminal UE import smoke separately from unit-test truth.

The phase is incomplete if any generated object reaches scene/runtime directly, if a Provider opens a
parallel Catalog/resolver path, if required routes silently fall back, or if license/dependency/hash
evidence is synthesized.
