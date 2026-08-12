from __future__ import annotations

from typing import Any


# Multi-backend compatibility is expressed only through versioned artifact
# contracts.  Adding another producer or consumer must not require a solver /
# renderer pair entry or a named-phenomenon route.
BACKEND_STAGE_IO: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
    "fallback": {"produces": {}, "consumes": {}},
    "genesis_fem": {
        "produces": {
            "deformable_mesh_cache_v1": {
                "schema_version": "harness_deformable_mesh_cache_v1",
                "required_artifacts": ["deformable_cache.json", "deformable_cache.npz"],
            }
        },
        "consumes": {},
    },
    "genesis_sph": {
        "produces": {
            "particle_surface_cache_v1": {
                "schema_version": "harness_particle_cache_v1",
                "required_artifacts": ["particle_cache.json"],
            }
        },
        "consumes": {},
    },
    "taichi_cloth": {
        "produces": {
            "deformable_mesh_cache_v1": {
                "schema_version": "harness_deformable_mesh_cache_v1",
                "required_artifacts": ["deformable_cache.json", "deformable_cache.npz"],
            }
        },
        "consumes": {},
    },
    "ue": {
        "produces": {},
        "consumes": {
            "particle_surface_cache_v1": {
                "schema_version": "harness_particle_cache_v1",
                "adapter_contract": "surface_mesh_sequence_replay_v1",
            },
            "deformable_mesh_cache_v1": {
                "schema_version": "harness_deformable_mesh_cache_v1",
                "adapter_contract": "surface_mesh_sequence_replay_v1",
            },
        },
    },
}


def stage_handoff_contract(producer: str, consumer: str) -> dict[str, Any] | None:
    produced = (BACKEND_STAGE_IO.get(producer) or {}).get("produces") or {}
    consumed = (BACKEND_STAGE_IO.get(consumer) or {}).get("consumes") or {}
    compatible = sorted(set(produced).intersection(consumed))
    if not compatible:
        return None
    contract_id = compatible[0]
    producer_contract = produced[contract_id]
    consumer_contract = consumed[contract_id]
    if producer_contract.get("schema_version") != consumer_contract.get("schema_version"):
        return None
    return {
        "contract_id": contract_id,
        "schema_version": producer_contract["schema_version"],
        "producer_backend": producer,
        "consumer_backend": consumer,
        "required_artifacts": list(producer_contract.get("required_artifacts") or []),
        "adapter_contract": consumer_contract.get("adapter_contract"),
    }
