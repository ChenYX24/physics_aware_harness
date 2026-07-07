from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CAPABILITY_SCHEMA_VERSION = "harness_capability_v1"
DEPRECATED_CAPABILITY_ALIASES = {
    "billiard_causality_compiler": "rigid_body_contact_causality",
}
PIPELINE_EXECUTION_ORDER = (
    "prompt_case_capability_planning",
    "asset_intent_resolution",
    "scene_spec_compilation",
    "static_scene_placement",
    "asset_runtime_binding_invocation",
    "runtime_actor_placement_compilation",
    "runtime_backend_execution",
    "capability_runtime_artifact_bridge",
    "canonical_signal_capture",
    "render_signal_sync_validation",
    "physics_verifier_truth_gate",
    "dataset_artifact_packaging",
    "pipeline_stage_orchestration",
)


@dataclass(frozen=True)
class Capability:
    id: str
    description: str
    physical_assumptions: list[str]
    required_signals: list[str]
    required_assets: list[str]
    verifier_rules: list[str]
    failure_taxonomy: list[str]
    repair_suggestions: list[str]
    smoke_cases: list[str]
    regression_cases: list[str]
    capability_type: str
    stage_ids: list[str]
    deprecated_by: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Capability":
        validate_capability_dict(data)
        return cls(
            id=str(data["id"]),
            description=str(data["description"]),
            physical_assumptions=[str(item) for item in data.get("physical_assumptions", [])],
            required_signals=[str(item) for item in data.get("required_signals", [])],
            required_assets=[str(item) for item in data.get("required_assets", [])],
            verifier_rules=[str(item) for item in data.get("verifier_rules", [])],
            failure_taxonomy=[str(item) for item in data.get("failure_taxonomy", [])],
            repair_suggestions=[str(item) for item in data.get("repair_suggestions", [])],
            smoke_cases=[str(item) for item in data.get("smoke_cases", [])],
            regression_cases=[str(item) for item in data.get("regression_cases", [])],
            capability_type=str(data.get("capability_type") or infer_capability_type(str(data["id"]))),
            stage_ids=[str(item) for item in data.get("stage_ids", [])],
            deprecated_by=str(data["deprecated_by"]) if data.get("deprecated_by") else None,
        )

    def to_summary(self) -> dict[str, Any]:
        summary = {
            "id": self.id,
            "capability_type": self.capability_type,
            "stage_ids": self.stage_ids,
            "description": self.description,
            "required_signals": self.required_signals,
            "required_assets": self.required_assets,
            "smoke_cases": self.smoke_cases,
            "regression_cases": self.regression_cases,
        }
        if self.deprecated_by:
            summary["deprecated_by"] = self.deprecated_by
        return summary


class CapabilityStore:
    def __init__(self, root: str | Path = ROOT / "capabilities") -> None:
        self.root = Path(root)

    def list(self) -> list[Capability]:
        return [Capability.from_dict(read_json(path)) for path in sorted(self.root.glob("*.json"))]

    def get(self, capability_id: str) -> Capability:
        path = self.root / f"{capability_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"capability not found: {capability_id}")
        return Capability.from_dict(read_json(path))

    def taxonomy(self, *, include_deprecated: bool = False) -> dict[str, Any]:
        capabilities = self.list()
        active = [item for item in capabilities if item.capability_type != "compatibility_alias"]
        aliases = {item.id: item.deprecated_by for item in capabilities if item.capability_type == "compatibility_alias" and item.deprecated_by}
        aliases.update(DEPRECATED_CAPABILITY_ALIASES)
        active_ids = {item.id for item in active}
        taxonomy = {
            "principle": "Capabilities describe reusable pipeline stages, asset operations, verification gates, or physics invariants. Scene families such as billiards are cases, not primary capabilities.",
            "pipeline_execution_order": [capability_id for capability_id in PIPELINE_EXECUTION_ORDER if capability_id in active_ids],
            "pipeline_stage_capabilities": order_capability_ids(
                (item.id for item in active if item.capability_type == "pipeline_stage"),
                preferred_order=PIPELINE_EXECUTION_ORDER,
            ),
            "asset_operation_capabilities": sorted(item.id for item in active if item.capability_type == "asset_operation"),
            "runtime_bridge_capabilities": sorted(item.id for item in active if item.capability_type == "runtime_bridge"),
            "physical_property_constraint_capabilities": sorted(
                item.id
                for item in active
                if item.id in {"explicit_physics_control_surface", "physics_parameter_semantics", "physics_property_constraint_validation"}
            ),
            "physics_behavior_capabilities": sorted(
                item.id
                for item in active
                if item.capability_type == "physics_constraint"
                and item.id not in {"explicit_physics_control_surface", "physics_property_constraint_validation"}
            ),
            "verification_capabilities": sorted(item.id for item in active if item.capability_type == "verification"),
            "dataset_packaging_capabilities": sorted(item.id for item in active if item.capability_type == "dataset_packaging"),
        }
        if include_deprecated:
            taxonomy["compatibility_alias_capabilities"] = sorted(item.id for item in capabilities if item.capability_type == "compatibility_alias")
        taxonomy["deprecated_aliases"] = aliases
        return taxonomy


def validate_capability_dict(data: dict[str, Any]) -> None:
    if data.get("schema_version") != CAPABILITY_SCHEMA_VERSION:
        raise ValueError("capability schema_version must be harness_capability_v1")
    required = [
        "id",
        "description",
        "physical_assumptions",
        "required_signals",
        "required_assets",
        "verifier_rules",
        "failure_taxonomy",
        "repair_suggestions",
        "smoke_cases",
        "regression_cases",
    ]
    for key in required:
        if key not in data:
            raise ValueError(f"capability missing field: {key}")
    for key in required[2:]:
        if not isinstance(data.get(key), list):
            raise ValueError(f"capability field must be list: {key}")
    if "stage_ids" in data and not isinstance(data["stage_ids"], list):
        raise ValueError("capability field must be list: stage_ids")


def canonical_capability_id(capability_id: str) -> str:
    return DEPRECATED_CAPABILITY_ALIASES.get(str(capability_id), str(capability_id))


def order_capability_ids(capability_ids: Any, *, preferred_order: tuple[str, ...]) -> list[str]:
    values = [str(item) for item in capability_ids]
    order = {capability_id: index for index, capability_id in enumerate(preferred_order)}
    return sorted(values, key=lambda item: (order.get(item, len(order)), item))


def infer_capability_type(capability_id: str) -> str:
    if capability_id in {"asset_intent_resolution", "asset_runtime_binding_invocation"}:
        return "asset_operation"
    if capability_id in {"blueprint_function_invocation", "capability_runtime_artifact_bridge", "canonical_signal_capture"}:
        return "runtime_bridge"
    if capability_id in {"pipeline_stage_orchestration", "scene_spec_compilation", "static_scene_placement", "prompt_case_capability_planning", "runtime_actor_placement_compilation", "runtime_backend_execution"}:
        return "pipeline_stage"
    if capability_id in {"physics_parameter_semantics", "physics_property_constraint_validation", "explicit_physics_control_surface"}:
        return "physics_constraint"
    if capability_id in {"physics_verifier_truth_gate", "render_signal_sync_validation"}:
        return "verification"
    if capability_id in {"dataset_artifact_packaging"}:
        return "dataset_packaging"
    if capability_id in {"billiard_causality_compiler"}:
        return "compatibility_alias"
    return "physics_constraint"


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected object JSON: {path}")
    return data
