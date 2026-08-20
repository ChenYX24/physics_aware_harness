from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Iterable, Mapping


PROMPT_LINEAGE_SCHEMA_VERSION = "harness_prompt_lineage_v1"
_STAGE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def prompt_digest(content: Any) -> str:
    rendered = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def new_prompt_lineage(case_id: str, user_prompt: Any) -> dict[str, Any]:
    case_id = str(case_id).strip()
    if not case_id:
        raise ValueError("prompt lineage case_id must not be empty")
    lineage: dict[str, Any] = {
        "schema_version": PROMPT_LINEAGE_SCHEMA_VERSION,
        "case_id": case_id,
        "canonical_stage_id": None,
        "refiner_stage_id": None,
        "stages": [],
    }
    append_prompt_stage(
        lineage,
        stage_id="user_request",
        stage_kind="user_input",
        content=user_prompt,
        producer="user",
        purpose="Original request before normalization or optimization.",
    )
    return lineage


def append_prompt_stage(
    lineage: dict[str, Any],
    *,
    stage_id: str,
    stage_kind: str,
    content: Any,
    producer: str,
    purpose: str,
    parent_stage_ids: Iterable[str] = (),
    artifact_path: str | None = None,
) -> dict[str, Any]:
    if lineage.get("schema_version") != PROMPT_LINEAGE_SCHEMA_VERSION:
        raise ValueError("unsupported prompt lineage schema_version")
    stage_id = str(stage_id).strip()
    if not _STAGE_ID.fullmatch(stage_id):
        raise ValueError("prompt stage_id must use letters, numbers, dot, dash, or underscore")
    stages = lineage.get("stages")
    if not isinstance(stages, list):
        raise ValueError("prompt lineage stages must be a list")
    existing = {str(stage.get("stage_id")) for stage in stages if isinstance(stage, Mapping)}
    if stage_id in existing:
        raise ValueError(f"duplicate prompt stage_id: {stage_id}")
    parents = [str(value) for value in parent_stage_ids]
    if any(parent not in existing for parent in parents):
        raise ValueError(f"prompt stage parent must already exist: {stage_id}")
    if not isinstance(content, (str, dict, list)) or not content:
        raise ValueError("prompt stage content must be a non-empty string, object, or array")
    if not all(str(value).strip() for value in (stage_kind, producer, purpose)):
        raise ValueError("prompt stage kind, producer, and purpose must not be empty")
    stage = {
        "stage_id": stage_id,
        "stage_kind": str(stage_kind),
        "producer": str(producer),
        "purpose": str(purpose),
        "parent_stage_ids": parents,
        "content": copy.deepcopy(content),
        "content_sha256": prompt_digest(content),
        "artifact_path": str(artifact_path) if artifact_path else None,
    }
    stages.append(stage)
    return stage


def prompt_stage_text(lineage: Mapping[str, Any], stage_id: str) -> str:
    for stage in lineage.get("stages") or []:
        if isinstance(stage, Mapping) and stage.get("stage_id") == stage_id:
            content = stage.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"prompt stage is not text: {stage_id}")
            return content
    raise ValueError(f"prompt stage not found: {stage_id}")


def validate_prompt_lineage(lineage: Mapping[str, Any]) -> None:
    if lineage.get("schema_version") != PROMPT_LINEAGE_SCHEMA_VERSION:
        raise ValueError("unsupported prompt lineage schema_version")
    if not str(lineage.get("case_id") or "").strip():
        raise ValueError("prompt lineage case_id must not be empty")
    stages = lineage.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("prompt lineage stages must be a non-empty list")
    seen: set[str] = set()
    for stage in stages:
        if not isinstance(stage, Mapping):
            raise ValueError("prompt lineage stages must be objects")
        stage_id = str(stage.get("stage_id") or "")
        if not _STAGE_ID.fullmatch(stage_id) or stage_id in seen:
            raise ValueError(f"invalid or duplicate prompt stage_id: {stage_id}")
        parents = stage.get("parent_stage_ids")
        if not isinstance(parents, list) or any(str(parent) not in seen for parent in parents):
            raise ValueError(f"prompt stage parents must precede child: {stage_id}")
        content = stage.get("content")
        if not isinstance(content, (str, dict, list)) or not content:
            raise ValueError(f"prompt stage content must be a non-empty string, object, or array: {stage_id}")
        if not all(str(stage.get(field) or "").strip() for field in ("stage_kind", "producer", "purpose")):
            raise ValueError(f"prompt stage kind, producer, and purpose must not be empty: {stage_id}")
        if prompt_digest(content) != stage.get("content_sha256"):
            raise ValueError(f"prompt stage digest mismatch: {stage_id}")
        seen.add(stage_id)
    for role in ("canonical_stage_id", "refiner_stage_id"):
        value = lineage.get(role)
        if value is not None and str(value) not in seen:
            raise ValueError(f"prompt lineage {role} does not resolve")


def build_refiner_prompt(
    canonical_prompt: str,
    *,
    appearance_requirements: Iterable[str],
    preservation_requirements: Iterable[str],
) -> str:
    appearance = [str(value).strip() for value in appearance_requirements if str(value).strip()]
    preserve = [str(value).strip() for value in preservation_requirements if str(value).strip()]
    if not canonical_prompt.strip() or not appearance or not preserve:
        raise ValueError("refiner prompt requires canonical, appearance, and preservation requirements")
    return (
        "Appearance-only edit of the input UE video. Canonical scene intent: "
        f"{canonical_prompt.strip()} Improve only: {'; '.join(appearance)}. "
        f"Preserve exactly: {'; '.join(preserve)}. "
        "Do not add, remove, merge, split, recolor, teleport, or replace tracked objects; "
        "do not change contacts, trajectories, event timing, camera motion, or final state. "
        "If realism conflicts with those constraints, preserve the input physics."
    )
