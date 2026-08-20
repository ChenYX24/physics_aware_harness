from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "video_repair_spec_v1"
REPAIRABILITY = {"grounded", "conditional"}


class VideoRepairSpecError(ValueError):
    pass


def load_video_repair_spec(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_video_repair_spec(payload)
    return payload


def validate_video_repair_spec(payload: Mapping[str, Any]) -> None:
    errors: list[str] = []

    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")
    if not str(payload.get("repair_id") or "").strip():
        errors.append("repair_id is required")

    source = _mapping(payload.get("source"), "source", errors)
    duration_s = _positive_number(source.get("duration_s"), "source.duration_s", errors)
    _positive_number(source.get("fps"), "source.fps", errors)
    locator = str(source.get("locator") or "")
    if "://" not in locator or locator.startswith(("/", "file://")):
        errors.append("source.locator must be a portable non-file locator")
    if not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256") or "")):
        errors.append("source.sha256 must be a lowercase SHA-256 digest")
    resolution = source.get("resolution_px")
    if not (
        isinstance(resolution, list)
        and len(resolution) == 2
        and all(isinstance(value, int) and value > 0 for value in resolution)
    ):
        errors.append("source.resolution_px must contain positive integer width and height")

    diagnosis = _mapping(payload.get("diagnosis"), "diagnosis", errors)
    repairability = diagnosis.get("repairability")
    if repairability not in REPAIRABILITY:
        errors.append(f"diagnosis.repairability must be one of {sorted(REPAIRABILITY)}")
    if repairability == "conditional" and not str(diagnosis.get("uncertainty_note") or "").strip():
        errors.append("conditional repairs require diagnosis.uncertainty_note")
    error_interval = _interval(diagnosis.get("error_interval_s"), "diagnosis.error_interval_s", duration_s, errors)

    assets = payload.get("core_assets")
    asset_ids: set[str] = set()
    if not isinstance(assets, list) or not assets:
        errors.append("core_assets must be a non-empty list")
    else:
        for index, asset in enumerate(assets):
            item = _mapping(asset, f"core_assets[{index}]", errors)
            asset_id = str(item.get("asset_id") or "")
            if not asset_id or asset_id in asset_ids:
                errors.append(f"core_assets[{index}].asset_id must be non-empty and unique")
            asset_ids.add(asset_id)
            if not item.get("identity_constraints"):
                errors.append(f"core_assets[{index}].identity_constraints must be non-empty")

    tracks = diagnosis.get("roi_tracks")
    if not isinstance(tracks, list) or not tracks:
        errors.append("diagnosis.roi_tracks must be a non-empty list")
    else:
        for index, track in enumerate(tracks):
            item = _mapping(track, f"diagnosis.roi_tracks[{index}]", errors)
            if item.get("asset_id") not in asset_ids:
                errors.append(f"diagnosis.roi_tracks[{index}].asset_id must reference a core asset")
            _interval(item.get("interval_s"), f"diagnosis.roi_tracks[{index}].interval_s", duration_s, errors)
            bbox = item.get("bbox_normalized")
            if not (
                isinstance(bbox, list)
                and len(bbox) == 4
                and all(_is_number(value) and 0.0 <= float(value) <= 1.0 for value in bbox)
                and float(bbox[0]) < float(bbox[2])
                and float(bbox[1]) < float(bbox[3])
            ):
                errors.append(f"diagnosis.roi_tracks[{index}].bbox_normalized must be [x0,y0,x1,y1] in [0,1]")

    boundaries = _mapping(payload.get("boundary_states"), "boundary_states", errors)
    before = _mapping(boundaries.get("before"), "boundary_states.before", errors)
    after = _mapping(boundaries.get("after"), "boundary_states.after", errors)
    before_time = _bounded_time(before.get("time_s"), "boundary_states.before.time_s", duration_s, errors)
    after_time = _bounded_time(after.get("time_s"), "boundary_states.after.time_s", duration_s, errors)
    if error_interval and before_time is not None and before_time > error_interval[0]:
        errors.append("boundary_states.before must not be after the error interval starts")
    if error_interval and after_time is not None and after_time < error_interval[1]:
        errors.append("boundary_states.after must not be before the error interval ends")
    for name, boundary in (("before", before), ("after", after)):
        if not boundary.get("state_assertions"):
            errors.append(f"boundary_states.{name}.state_assertions must be non-empty")

    graph = _mapping(payload.get("target_event_graph"), "target_event_graph", errors)
    nodes = graph.get("nodes")
    node_ids: set[str] = set()
    if not isinstance(nodes, list) or not nodes:
        errors.append("target_event_graph.nodes must be a non-empty list")
    else:
        for index, node in enumerate(nodes):
            item = _mapping(node, f"target_event_graph.nodes[{index}]", errors)
            node_id = str(item.get("id") or "")
            if not node_id or node_id in node_ids:
                errors.append(f"target_event_graph.nodes[{index}].id must be non-empty and unique")
            node_ids.add(node_id)
    edges = graph.get("edges")
    if not isinstance(edges, list) or not edges:
        errors.append("target_event_graph.edges must be a non-empty list")
    else:
        for index, edge in enumerate(edges):
            if not isinstance(edge, list) or len(edge) != 3 or edge[0] not in node_ids or edge[1] not in node_ids:
                errors.append(f"target_event_graph.edges[{index}] must be [known_source, known_target, relation]")

    stage3 = _mapping(payload.get("stage3_contract"), "stage3_contract", errors)
    stage4 = _mapping(payload.get("stage4_contract"), "stage4_contract", errors)
    for field in ("required_evidence", "pass_criteria", "forbidden_shortcuts"):
        if not stage3.get(field):
            errors.append(f"stage3_contract.{field} must be non-empty")
    for field in ("must_preserve", "forbidden_changes", "success_criteria"):
        if not stage4.get(field):
            errors.append(f"stage4_contract.{field} must be non-empty")

    if errors:
        raise VideoRepairSpecError("; ".join(errors))


def _mapping(value: Any, path: str, errors: list[str]) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    errors.append(f"{path} must be an object")
    return {}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive_number(value: Any, path: str, errors: list[str]) -> float | None:
    if not _is_number(value) or float(value) <= 0:
        errors.append(f"{path} must be positive")
        return None
    return float(value)


def _bounded_time(value: Any, path: str, duration_s: float | None, errors: list[str]) -> float | None:
    if not _is_number(value):
        errors.append(f"{path} must be numeric")
        return None
    result = float(value)
    if result < 0 or (duration_s is not None and result > duration_s):
        errors.append(f"{path} must be within the source duration")
    return result


def _interval(value: Any, path: str, duration_s: float | None, errors: list[str]) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) != 2:
        errors.append(f"{path} must be [start_s, end_s]")
        return None
    start = _bounded_time(value[0], f"{path}[0]", duration_s, errors)
    end = _bounded_time(value[1], f"{path}[1]", duration_s, errors)
    if start is not None and end is not None and start >= end:
        errors.append(f"{path} must have start_s < end_s")
    return None if start is None or end is None else (start, end)
