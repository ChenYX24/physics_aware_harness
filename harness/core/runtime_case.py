from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.core.artifact_schema import read_json


RUNTIME_CASE_SCHEMA_VERSION = "harness_runtime_case_v2"


def validate_runtime_case(data: dict[str, Any]) -> None:
    """Validate the canonical executable contract at a runtime boundary."""
    if data.get("schema_version") != RUNTIME_CASE_SCHEMA_VERSION:
        raise ValueError(
            f"runtime case schema_version must be {RUNTIME_CASE_SCHEMA_VERSION}"
        )
    for field in ("case_id", "capability_id", "prompt"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"runtime case {field} must be a non-empty string")
    if not isinstance(data.get("should_pass"), bool):
        raise ValueError("runtime case should_pass must be a boolean")
    objects = data.get("objects")
    if not isinstance(objects, list):
        raise ValueError("runtime case objects must be a list")
    object_ids = [item.get("id") for item in objects if isinstance(item, dict)]
    if len(object_ids) != len(objects) or any(
        not isinstance(object_id, str) or not object_id.strip()
        for object_id in object_ids
    ):
        raise ValueError("every runtime object must have a non-empty string id")
    if len(set(object_ids)) != len(object_ids):
        raise ValueError("runtime object ids must be unique")


@dataclass(frozen=True)
class RuntimeCase:
    """Canonical executable contract compiled from one validated CaseSpec V2."""

    data: dict[str, Any]

    def __post_init__(self) -> None:
        validate_runtime_case(self.data)

    @property
    def case_id(self) -> str:
        return str(self.data["case_id"])

    @property
    def capability_id(self) -> str:
        return str(self.data["capability_id"])

    @property
    def should_pass(self) -> bool:
        return bool(self.data["should_pass"])

    @property
    def objects(self) -> list[dict[str, Any]]:
        return [item for item in self.data.get("objects", []) if isinstance(item, dict)]


def load_runtime_case(path: str | Path) -> RuntimeCase:
    data = read_json(Path(path))
    if not isinstance(data, dict):
        raise ValueError("runtime case root must be a JSON object")
    return RuntimeCase(data)
