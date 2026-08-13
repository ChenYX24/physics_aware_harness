from __future__ import annotations

import hashlib
import math
import mimetypes
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from harness.agent.job_schema import stable_digest, utc_now
from harness.agent.review_schema import EVIDENCE_BUNDLE_SCHEMA_VERSION, EvidenceBundleManifest
from harness.core.artifact_schema import read_json, write_json
from harness.core.stage_result import StageResult, artifact_ref, build_stage_result


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
_MAX_TRAJECTORY_SAMPLE_FRAMES = 24
_MAX_STATE_TRANSITIONS = 100
_MAX_EVENT_WINDOWS = 8


class EvidenceBundleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = False


def build_evidence_bundle(
    *,
    job_id: str,
    attempt: Mapping[str, Any],
    attempt_dir: str | Path,
    candidate_run_dir: str | Path,
    request: Mapping[str, Any],
    intent_contract: Mapping[str, Any],
    intent_amendments: Sequence[Mapping[str, Any]] = (),
    ffmpeg: str = "ffmpeg",
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    raw_attempt_dir = Path(attempt_dir).absolute()
    attempt_dir = raw_attempt_dir.resolve(strict=True)
    raw_run_dir = Path(candidate_run_dir).absolute()
    run_dir = raw_run_dir.resolve(strict=True)
    candidate_root = (attempt_dir / "runs" / "candidate").resolve(strict=True)
    raw_candidate_root = raw_attempt_dir / "runs" / "candidate"
    if not run_dir.is_relative_to(candidate_root) or _path_chain_has_symlink(raw_run_dir, raw_candidate_root):
        raise EvidenceBundleError("evidence_candidate_path_invalid", "Candidate run escapes the current attempt candidate root")
    raw_bundle_dir = attempt_dir / "evidence_bundle"
    if raw_bundle_dir.is_symlink() or (raw_bundle_dir.exists() and not raw_bundle_dir.is_dir()):
        raise EvidenceBundleError("evidence_artifact_path_invalid", "Evidence Bundle root must be an in-attempt directory")
    raw_bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir = raw_bundle_dir.resolve(strict=True)
    manifest_path = bundle_dir / "manifest.json"
    snapshots = current_evidence_snapshots(
        attempt_dir=attempt_dir,
        request=request,
        intent_contract=intent_contract,
        intent_amendments=intent_amendments,
    )
    if manifest_path.is_file():
        manifest = validate_current_evidence_bundle(
            manifest_path=manifest_path,
            job_id=job_id,
            attempt=attempt,
            attempt_dir=attempt_dir,
            expected_candidate_run_dir=run_dir,
            expected_intent_contract_digest=str(attempt["intent_contract_digest"]),
            expected_snapshots=snapshots,
        )
        return {
            "manifest": manifest,
            "manifest_path": str(manifest_path),
            "stage_result": build_stage_result(
                stage="evidence_bundle",
                status="completed",
                job_id=job_id,
                attempt_id=str(attempt["attempt_id"]),
                invocation_count=0,
                artifact_refs=[artifact_ref("evidence_bundle", manifest_path, EVIDENCE_BUNDLE_SCHEMA_VERSION)],
            ),
        }

    technical_gates = _technical_gates(attempt_dir, run_dir)
    trajectory_path, _, frames = _trajectory(run_dir)
    timeline = _contact_timeline(run_dir, frames)
    selection = _select_event_points(frames, timeline)
    views = _canonical_views(run_dir)
    if not views:
        raise EvidenceBundleError("evidence_canonical_view_missing", "Candidate contains no canonical RGB views")

    inputs_dir = bundle_dir / "inputs"
    keyframes_dir = bundle_dir / "keyframes"
    montages_dir = bundle_dir / "montages"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    keyframes_dir.mkdir(parents=True, exist_ok=True)
    montages_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[dict[str, Any]] = []
    for name, value in snapshots.items():
        destination = inputs_dir / f"{name}.json"
        write_json(destination, value)
        artifacts.append(_bundle_artifact(bundle_dir, f"{name}_snapshot", "input_snapshot", destination, source_ref=None))

    for input_row in request.get("inputs") or []:
        if not isinstance(input_row, Mapping) or input_row.get("kind") != "image":
            continue
        source = Path(str(input_row.get("local_path") or "")).expanduser().resolve(strict=True)
        expected = str(input_row.get("sha256") or "")
        if _sha256_file(source) != expected:
            raise EvidenceBundleError("request_input_identity_mismatch", "Original request image hash changed before semantic review")
        input_id = str(input_row.get("input_id") or "input")
        suffix = source.suffix.lower() or mimetypes.guess_extension(str(input_row.get("mime_type") or "")) or ".bin"
        destination = inputs_dir / f"{_safe_id(input_id)}{suffix}"
        shutil.copyfile(source, destination)
        artifacts.append(
            _bundle_artifact(
                bundle_dir,
                f"original_{_safe_id(input_id)}",
                "original_input_snapshot",
                destination,
                source_ref=None,
                mime_type=str(input_row.get("mime_type") or "application/octet-stream"),
            )
        )

    trajectory_summary = _trajectory_summary(attempt_dir, trajectory_path, frames, timeline, selection)
    summary_path = bundle_dir / "evidence_summary.json"
    write_json(
        summary_path,
        {
            "schema_version": "harness_evidence_summary_v2",
            "hard_requirements": list(intent_contract.get("hard_requirements") or []),
            "semantic_requirements": semantic_review_requirements(intent_contract, intent_amendments),
            "event_selection": selection,
            "trajectory": trajectory_summary,
            "contacts": timeline,
            "technical_gates": technical_gates,
        },
    )
    artifacts.append(_bundle_artifact(bundle_dir, "evidence_summary", "structured_summary", summary_path, source_ref=None))

    runner = command_runner or _subprocess_runner
    ffmpeg_path = shutil.which(ffmpeg) if Path(ffmpeg).name == ffmpeg else str(Path(ffmpeg).expanduser())
    if not ffmpeg_path:
        raise EvidenceBundleError("evidence_ffmpeg_unavailable", "ffmpeg is required to extract Semantic Review keyframes")
    for point in selection["points"]:
        label = str(point["label"])
        time_s = float(point["time_s"])
        point_images: list[Path] = []
        for view_id in views:
            raw_video = run_dir / "views" / view_id / "rgb.mp4"
            if _path_chain_has_symlink(raw_video, run_dir):
                raise EvidenceBundleError("evidence_video_path_invalid", "Canonical RGB video must not be a symlink")
            video = raw_video.resolve(strict=True)
            if not video.is_relative_to(run_dir):
                raise EvidenceBundleError("evidence_video_path_invalid", "Canonical RGB video path is not a regular in-run file")
            destination = keyframes_dir / f"{label}__{_safe_id(view_id)}.png"
            _extract_frame(ffmpeg_path, video, time_s, destination, runner)
            point_images.append(destination)
            artifacts.append(
                _bundle_artifact(
                    bundle_dir,
                    f"keyframe_{label}_{_safe_id(view_id)}",
                    "keyframe",
                    destination,
                    source_ref=video.relative_to(attempt_dir).as_posix(),
                    time_s=time_s,
                    view_id=view_id,
                    mime_type="image/png",
                )
            )
        montage = montages_dir / f"{label}.png"
        _render_montage(ffmpeg_path, point_images, montage, runner)
        artifacts.append(
            _bundle_artifact(
                bundle_dir,
                f"montage_{label}",
                "multi_view_montage",
                montage,
                source_ref=None,
                time_s=time_s,
                mime_type="image/png",
            )
        )

    manifest = EvidenceBundleManifest.from_dict(
        {
            "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
            "job_id": job_id,
            "attempt_id": attempt["attempt_id"],
            "case_spec_digest": attempt["case_spec_digest"],
            "intent_contract_digest": attempt["intent_contract_digest"],
            "candidate_run": {
                "path": run_dir.relative_to(attempt_dir).as_posix(),
                "fingerprint": stable_digest(read_json(attempt_dir / "candidate_run.json")),
            },
            "technical_gates": technical_gates,
            "event_selection": selection,
            "trajectory_summary": trajectory_summary,
            "contact_timeline": timeline,
            "artifacts": artifacts,
            "created_at": utc_now(),
        }
    ).to_dict()
    write_json(manifest_path, manifest)
    manifest = validate_current_evidence_bundle(
        manifest_path=manifest_path,
        job_id=job_id,
        attempt=attempt,
        attempt_dir=attempt_dir,
        expected_candidate_run_dir=run_dir,
        expected_intent_contract_digest=str(attempt["intent_contract_digest"]),
        expected_snapshots=snapshots,
        expected_manifest_digest=stable_digest(manifest),
    )
    return {
        "manifest": manifest,
        "manifest_path": str(manifest_path),
        "stage_result": build_stage_result(
            stage="evidence_bundle",
            status="completed",
            job_id=job_id,
            attempt_id=str(attempt["attempt_id"]),
            artifact_refs=[artifact_ref("evidence_bundle", manifest_path, EVIDENCE_BUNDLE_SCHEMA_VERSION)],
        ),
    }


def validate_current_evidence_bundle(
    *,
    manifest_path: str | Path,
    job_id: str,
    attempt: Mapping[str, Any],
    attempt_dir: str | Path,
    expected_candidate_run_dir: str | Path | None = None,
    expected_intent_contract_digest: str | None = None,
    expected_snapshots: Mapping[str, Any] | None = None,
    expected_manifest_digest: str | None = None,
) -> dict[str, Any]:
    """Validate that a materialized bundle is the current attempt's exact evidence."""

    try:
        return _validate_current_evidence_bundle(
            manifest_path=manifest_path,
            job_id=job_id,
            attempt=attempt,
            attempt_dir=attempt_dir,
            expected_candidate_run_dir=expected_candidate_run_dir,
            expected_intent_contract_digest=expected_intent_contract_digest,
            expected_snapshots=expected_snapshots,
            expected_manifest_digest=expected_manifest_digest,
        )
    except EvidenceBundleError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise EvidenceBundleError(
            "evidence_bundle_validation_failed",
            "Evidence Bundle identity or materialized content cannot be validated",
        ) from exc


def _validate_current_evidence_bundle(
    *,
    manifest_path: str | Path,
    job_id: str,
    attempt: Mapping[str, Any],
    attempt_dir: str | Path,
    expected_candidate_run_dir: str | Path | None,
    expected_intent_contract_digest: str | None,
    expected_snapshots: Mapping[str, Any] | None,
    expected_manifest_digest: str | None,
) -> dict[str, Any]:
    raw_attempt_dir = Path(attempt_dir).absolute()
    resolved_attempt_dir = raw_attempt_dir.resolve(strict=True)
    raw_manifest_path = Path(manifest_path).absolute()
    canonical_manifest_path = resolved_attempt_dir / "evidence_bundle" / "manifest.json"
    try:
        manifest_file = raw_manifest_path.resolve(strict=True)
    except OSError as exc:
        raise EvidenceBundleError("evidence_manifest_missing", "Evidence Bundle manifest is missing") from exc
    if (
        manifest_file != canonical_manifest_path
        or _path_chain_has_symlink(raw_manifest_path, resolved_attempt_dir)
        or not manifest_file.is_file()
    ):
        raise EvidenceBundleError("evidence_manifest_path_invalid", "Evidence Bundle manifest path is not canonical")

    manifest = EvidenceBundleManifest.from_dict(read_json(manifest_file)).to_dict()
    manifest_digest = stable_digest(manifest)
    if expected_manifest_digest is not None and manifest_digest != expected_manifest_digest:
        raise EvidenceBundleError("evidence_manifest_identity_mismatch", "Evidence Bundle manifest changed")

    attempt_id = str(attempt.get("attempt_id") or "")
    current_intent_digest = str(expected_intent_contract_digest or attempt.get("intent_contract_digest") or "")
    case_spec_path = resolved_attempt_dir / "case_spec.json"
    try:
        case_spec_digest = stable_digest(read_json(case_spec_path))
    except (OSError, ValueError, TypeError) as exc:
        raise EvidenceBundleError("evidence_case_spec_identity_mismatch", "Current CaseSpec cannot be validated") from exc
    if (
        manifest["job_id"] != job_id
        or manifest["attempt_id"] != attempt_id
        or str(attempt.get("job_id") or job_id) != job_id
        or manifest["case_spec_digest"] != str(attempt.get("case_spec_digest") or "")
        or manifest["case_spec_digest"] != case_spec_digest
        or manifest["intent_contract_digest"] != str(attempt.get("intent_contract_digest") or "")
        or manifest["intent_contract_digest"] != current_intent_digest
    ):
        raise EvidenceBundleError(
            "evidence_bundle_identity_mismatch",
            "Evidence Bundle does not belong to the current Job, attempt, CaseSpec, and Intent Contract",
        )

    candidate_path = resolved_attempt_dir / "candidate_run.json"
    try:
        candidate = read_json(candidate_path)
        raw_run_dir = Path(str(candidate["run_dir"])).absolute()
        run_dir = raw_run_dir.resolve(strict=True)
        candidate_root = (resolved_attempt_dir / "runs" / "candidate").resolve(strict=True)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise EvidenceBundleError("evidence_candidate_identity_mismatch", "Current Candidate identity cannot be validated") from exc
    manifest_run_dir = (resolved_attempt_dir / manifest["candidate_run"]["path"]).resolve(strict=True)
    expected_manifest_run_path = run_dir.relative_to(resolved_attempt_dir).as_posix()
    if (
        not run_dir.is_relative_to(candidate_root)
        or _path_chain_has_symlink(raw_run_dir, resolved_attempt_dir / "runs" / "candidate")
        or manifest["candidate_run"]["path"] != expected_manifest_run_path
        or manifest_run_dir != run_dir
        or manifest["candidate_run"]["fingerprint"] != stable_digest(candidate)
    ):
        raise EvidenceBundleError("evidence_candidate_identity_mismatch", "Evidence Bundle Candidate identity changed")
    if expected_candidate_run_dir is not None and run_dir != Path(expected_candidate_run_dir).resolve(strict=True):
        raise EvidenceBundleError("evidence_candidate_identity_mismatch", "Evidence Bundle uses a different Candidate run")

    expected_reports = {
        "verifier": {"harness_verifier.json", "verifier_report.json"},
        "render_sync": {"render_sync_report.json"},
        "quality_gate": {"quality_report.json"},
    }
    for name, gate in manifest["technical_gates"].items():
        raw_gate_path = resolved_attempt_dir / gate["path"]
        gate_path = raw_gate_path.resolve(strict=True)
        sidecar = run_dir / "stage_results" / f"{'quality_gate' if name == 'quality_gate' else name}.json"
        if (
            gate_path.parent != run_dir
            or gate_path.name not in expected_reports[name]
            or _path_chain_has_symlink(raw_gate_path, resolved_attempt_dir)
            or _sha256_file(gate_path) != gate["sha256"]
            or StageResult.from_dict(read_json(sidecar)).data["status"] != "completed"
        ):
            raise EvidenceBundleError("evidence_technical_gate_identity_mismatch", f"Current {name} gate identity changed")
        report = read_json(gate_path)
        passed = report.get("hard_gate_passed") is True if name == "quality_gate" else report.get("status") == "pass"
        if not passed:
            raise EvidenceBundleError("evidence_technical_gate_identity_mismatch", f"Current {name} gate no longer passes")

    trajectory = manifest["trajectory_summary"]
    raw_trajectory_path = resolved_attempt_dir / trajectory["source_path"]
    trajectory_path = raw_trajectory_path.resolve(strict=True)
    if (
        not trajectory_path.is_relative_to(run_dir)
        or _path_chain_has_symlink(raw_trajectory_path, resolved_attempt_dir)
        or _sha256_file(trajectory_path) != trajectory["source_sha256"]
    ):
        raise EvidenceBundleError("evidence_trajectory_identity_mismatch", "Current trajectory identity changed")

    bundle_dir = manifest_file.parent
    _validate_materialized_bundle(bundle_dir, manifest)
    artifacts = {str(row["artifact_id"]): row for row in manifest["artifacts"]}
    if expected_snapshots is not None:
        for name, expected in expected_snapshots.items():
            artifact_id = f"{name}_snapshot"
            artifact = artifacts.get(artifact_id)
            expected_path = f"inputs/{name}.json"
            if artifact is None or artifact["kind"] != "input_snapshot" or artifact["path"] != expected_path:
                raise EvidenceBundleError("evidence_input_snapshot_mismatch", f"Evidence Bundle lacks current {name} snapshot")
            if stable_digest(read_json(bundle_dir / expected_path)) != stable_digest(expected):
                raise EvidenceBundleError("evidence_input_snapshot_mismatch", f"Evidence Bundle {name} snapshot is stale")
        request = expected_snapshots.get("user_request")
        if isinstance(request, Mapping):
            for row in request.get("inputs") or []:
                if not isinstance(row, Mapping) or row.get("kind") != "image":
                    continue
                artifact = artifacts.get(f"original_{_safe_id(str(row.get('input_id') or 'input'))}")
                if artifact is None or artifact["kind"] != "original_input_snapshot" or artifact["sha256"] != str(row.get("sha256") or ""):
                    raise EvidenceBundleError("evidence_input_snapshot_mismatch", "Evidence Bundle original image snapshot is stale")
        intent = expected_snapshots.get("intent_contract")
        amendments_snapshot = expected_snapshots.get("intent_amendments")
        amendment_items = (
            amendments_snapshot.get("items")
            if isinstance(amendments_snapshot, Mapping)
            else None
        )
        summary_artifact = artifacts.get("evidence_summary")
        if (
            not isinstance(intent, Mapping)
            or not isinstance(amendment_items, list)
            or summary_artifact is None
            or summary_artifact["kind"] != "structured_summary"
        ):
            raise EvidenceBundleError(
                "evidence_semantic_requirements_mismatch",
                "Evidence Bundle cannot establish its effective Semantic Review requirements",
            )
        summary = read_json(bundle_dir / summary_artifact["path"])
        if summary.get("semantic_requirements") != semantic_review_requirements(intent, amendment_items):
            raise EvidenceBundleError(
                "evidence_semantic_requirements_mismatch",
                "Evidence Bundle Semantic Review requirements are stale",
            )
    return manifest


def current_evidence_snapshots(
    *,
    attempt_dir: Path,
    request: Mapping[str, Any],
    intent_contract: Mapping[str, Any],
    intent_amendments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "user_request": dict(request),
        "intent_contract": dict(intent_contract),
        "intent_amendments": {
            "schema_version": "harness_effective_intent_amendments_v1",
            "items": [dict(value) for value in intent_amendments],
        },
        "case_spec": read_json(attempt_dir / "case_spec.json"),
    }


def semantic_review_requirements(
    intent_contract: Mapping[str, Any],
    intent_amendments: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project immutable hard requirements and ambiguity decisions for review."""
    requirements = [dict(value) for value in intent_contract.get("hard_requirements") or []]
    seen = {str(value.get("id") or "") for value in requirements}
    if "" in seen or len(seen) != len(requirements):
        raise ValueError("Intent Contract hard requirement identities must be non-empty and unique")
    resolved_ambiguities: set[str] = set()
    for amendment in intent_amendments:
        if amendment.get("schema_version") != "harness_intent_contract_amendment_v1":
            continue
        resolutions = amendment.get("ambiguity_resolutions")
        if not isinstance(resolutions, list):
            raise ValueError("Intent ambiguity amendment resolutions must be a list")
        for resolution in resolutions:
            if not isinstance(resolution, Mapping):
                raise ValueError("Intent ambiguity decisions must be objects")
            ambiguity_id = str(resolution.get("ambiguity_id") or "").strip()
            decision = str(resolution.get("decision") or "").strip()
            if not ambiguity_id or not decision or ambiguity_id in resolved_ambiguities:
                raise ValueError("Intent ambiguity decisions must have unique identities and non-empty decisions")
            resolved_ambiguities.add(ambiguity_id)
            requirement_id = f"ambiguity_decision_{stable_digest(ambiguity_id)[:16]}"
            if requirement_id in seen:
                raise ValueError("Semantic Review requirement identities must be unique")
            seen.add(requirement_id)
            requirements.append(
                {
                    "id": requirement_id,
                    "text": f"Follow the resolved ambiguity decision for {ambiguity_id}: {decision}",
                    "frozen": True,
                    "source": "intent_ambiguity_decision",
                    "ambiguity_id": ambiguity_id,
                    "decision": decision,
                }
            )
    return requirements


def _technical_gates(attempt_dir: Path, run_dir: Path) -> dict[str, Any]:
    reports = {
        "verifier": next((path for path in (run_dir / "harness_verifier.json", run_dir / "verifier_report.json") if path.is_file()), None),
        "render_sync": run_dir / "render_sync_report.json",
        "quality_gate": run_dir / "quality_report.json",
    }
    result: dict[str, Any] = {}
    for name, path in reports.items():
        if path is None or not path.is_file():
            raise EvidenceBundleError("evidence_technical_gate_missing", f"Required {name} report is missing")
        stage = "quality_gate" if name == "quality_gate" else name
        sidecar = run_dir / "stage_results" / f"{stage}.json"
        if not sidecar.is_file() or StageResult.from_dict(read_json(sidecar)).data["status"] != "completed":
            raise EvidenceBundleError("evidence_technical_gate_incomplete", f"Required {name} Stage Result did not complete")
        report = read_json(path)
        passed = report.get("hard_gate_passed") is True if name == "quality_gate" else report.get("status") == "pass"
        if not passed:
            raise EvidenceBundleError("evidence_technical_gate_failed", f"Required {name} report did not pass")
        result[name] = {
            "status": "pass",
            "path": path.resolve().relative_to(attempt_dir).as_posix(),
            "sha256": _sha256_file(path),
        }
    return result


def _trajectory(run_dir: Path) -> tuple[Path, Any, list[dict[str, Any]]]:
    path = next((value for value in (run_dir / "trajectory.json", run_dir / "ue_output" / "trajectory.json") if value.is_file()), None)
    if path is None:
        raise EvidenceBundleError("evidence_trajectory_missing", "Candidate trajectory is missing")
    payload = read_json(path)
    raw = payload.get("frames") if isinstance(payload, Mapping) else payload
    frames = [dict(row) for row in raw or [] if isinstance(row, Mapping)] if isinstance(raw, list) else []
    if not frames:
        raise EvidenceBundleError("evidence_trajectory_empty", "Candidate trajectory contains no frames")
    times = [_frame_time(row) for row in frames]
    if any(value is None for value in times):
        raise EvidenceBundleError("evidence_trajectory_time_invalid", "Candidate trajectory contains an invalid timestamp")
    return path.resolve(), payload, frames


def _contact_timeline(run_dir: Path, frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw: list[Any] = []
    path = run_dir / "contact_events.json"
    if path.is_file():
        payload = read_json(path)
        value = payload.get("events") if isinstance(payload, Mapping) else payload
        raw = value if isinstance(value, list) else []
    if not raw:
        for frame_index, frame in enumerate(frames):
            for contact in frame.get("contacts") or []:
                if isinstance(contact, Mapping):
                    raw.append({**dict(contact), "frame": contact.get("frame", frame.get("frame", frame_index))})
    frame_id_to_index = {
        _integer(frame.get("frame"), default=index): index
        for index, frame in enumerate(frames)
    }
    timeline = []
    for index, event in enumerate(raw[:200]):
        if not isinstance(event, Mapping):
            continue
        raw_frame = _integer(event.get("frame"), default=-1)
        frame_index = frame_id_to_index.get(raw_frame, raw_frame)
        if frame_index < 0 or frame_index >= len(frames):
            continue
        event_time = _number_or_none(event.get("time_s", event.get("time")))
        if event_time is None:
            event_time = _frame_time(frames[frame_index])
        timeline.append(
            {
                "event_id": f"contact_{index:03d}",
                "frame_index": frame_index,
                "time_s": event_time,
                "objects": [str(value) for value in event.get("objects") or []],
                "kind": str(event.get("kind") or event.get("type") or event.get("method") or "contact"),
            }
        )
    return timeline


def _select_event_points(frames: list[dict[str, Any]], timeline: list[dict[str, Any]]) -> dict[str, Any]:
    if timeline:
        during = min(max(0, int(timeline[0]["frame_index"])), len(frames) - 1)
        indices = [max(0, during - 1), during, min(len(frames) - 1, during + 1)]
        strategy = "event_anchored"
        reason = f"anchored to {timeline[0]['event_id']}"
    else:
        indices = [0, (len(frames) - 1) // 2, len(frames) - 1]
        strategy = "start_mid_end"
        reason = "no locatable contact/event existed; deterministic start/mid/end fallback"
    labels = ("before", "during", "after")
    return {
        "strategy": strategy,
        "reason": reason,
        "points": [
            {
                "label": label,
                "time_s": float(_frame_time(frames[index]) or 0.0),
                "frame_index": index,
                "event_refs": [timeline[0]["event_id"]] if timeline and label == "during" else [],
            }
            for label, index in zip(labels, indices)
        ],
    }


def _canonical_views(run_dir: Path) -> list[str]:
    quality = read_json(run_dir / "quality_report.json")
    media_views = ((quality.get("media") or {}).get("views") or {}) if isinstance(quality, Mapping) else {}
    candidates = sorted(str(value) for value in media_views) if isinstance(media_views, Mapping) else []
    if not candidates:
        views_root = run_dir / "views"
        candidates = sorted(path.name for path in views_root.iterdir() if path.is_dir()) if views_root.is_dir() else []
    return [view for view in candidates if (run_dir / "views" / view / "rgb.mp4").is_file()]


def _trajectory_summary(
    attempt_dir: Path,
    path: Path,
    frames: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    selection: Mapping[str, Any],
) -> dict[str, Any]:
    object_ids: set[str] = set()
    for frame in frames:
        objects = frame.get("objects")
        if isinstance(objects, Mapping):
            object_ids.update(str(value) for value in objects)
    transitions = _state_transitions(frames)
    reasons: dict[int, set[str]] = {}

    def select(index: int, reason: str) -> None:
        if 0 <= index < len(frames):
            reasons.setdefault(index, set()).add(reason)

    select(0, "trajectory_start")
    select((len(frames) - 1) // 2, "trajectory_midpoint")
    select(len(frames) - 1, "trajectory_end")
    for point in selection.get("points") or []:
        if isinstance(point, Mapping):
            select(_integer(point.get("frame_index"), default=-1), f"event_selection_{point.get('label')}")
    for event in timeline[:_MAX_EVENT_WINDOWS]:
        event_index = _integer(event.get("frame_index"), default=-1)
        for offset, label in ((-1, "before"), (0, "during"), (1, "after")):
            select(event_index + offset, f"{event.get('event_id')}_{label}")
    for transition in transitions:
        select(int(transition["frame_index"]), "discrete_state_transition")
    if len(frames) > 1:
        uniform_slots = min(_MAX_TRAJECTORY_SAMPLE_FRAMES, len(frames))
        for slot in range(uniform_slots):
            index = round(slot * (len(frames) - 1) / max(1, uniform_slots - 1))
            select(index, "uniform_sample")

    required_indices = {
        _integer(point.get("frame_index"), default=-1)
        for point in selection.get("points") or []
        if isinstance(point, Mapping)
    } | {0, (len(frames) - 1) // 2, len(frames) - 1}
    ordered = sorted(reasons)
    if len(ordered) > _MAX_TRAJECTORY_SAMPLE_FRAMES:
        retained = sorted(index for index in required_indices if index in reasons)
        for index in ordered:
            if len(retained) >= _MAX_TRAJECTORY_SAMPLE_FRAMES:
                break
            if index not in retained:
                retained.append(index)
        ordered = sorted(retained)
    samples = [
        {
            "frame_index": index,
            "time_s": float(_frame_time(frames[index]) or 0.0),
            "reasons": sorted(reasons[index]),
            "objects": _sampled_object_states(frames[index]),
        }
        for index in ordered
    ]
    readable_ranges = [
        {
            "range_id": "trajectory_full",
            "start_frame_index": ordered[0],
            "end_frame_index": ordered[-1],
            "start_time_s": float(_frame_time(frames[ordered[0]]) or 0.0),
            "end_time_s": float(_frame_time(frames[ordered[-1]]) or 0.0),
            "sample_frame_indices": ordered,
            "event_refs": [],
        }
    ]
    selected_indices = set(ordered)
    for event in timeline[:_MAX_EVENT_WINDOWS]:
        event_index = _integer(event.get("frame_index"), default=-1)
        window_indices = sorted(
            index for index in {max(0, event_index - 1), event_index, min(len(frames) - 1, event_index + 1)}
            if index in selected_indices
        )
        if event_index not in selected_indices or not window_indices:
            continue
        readable_ranges.append(
            {
                "range_id": f"event_window_{_safe_id(str(event['event_id']))}",
                "start_frame_index": window_indices[0],
                "end_frame_index": window_indices[-1],
                "start_time_s": float(_frame_time(frames[window_indices[0]]) or 0.0),
                "end_time_s": float(_frame_time(frames[window_indices[-1]]) or 0.0),
                "sample_frame_indices": window_indices,
                "event_refs": [str(event["event_id"])],
            }
        )
    return {
        "source_path": path.relative_to(attempt_dir).as_posix(),
        "source_sha256": _sha256_file(path),
        "frame_count": len(frames),
        "start_time_s": float(_frame_time(frames[0]) or 0.0),
        "end_time_s": float(_frame_time(frames[-1]) or 0.0),
        "objects": sorted(object_ids),
        "sampling": {
            "strategy": "uniform_event_state_v1",
            "max_sample_frames": _MAX_TRAJECTORY_SAMPLE_FRAMES,
            "selected_frame_count": len(samples),
            "omitted_frame_count": len(frames) - len(samples),
            "state_transition_count": len(transitions),
            "state_transitions_included": min(len(transitions), _MAX_STATE_TRANSITIONS),
        },
        "sampled_frames": samples,
        "readable_ranges": readable_ranges,
        "state_transitions": transitions[:_MAX_STATE_TRANSITIONS],
    }


def _sampled_object_states(frame: Mapping[str, Any]) -> list[dict[str, Any]]:
    objects = frame.get("objects")
    if not isinstance(objects, Mapping):
        return []
    result = []
    for object_id in sorted(str(value) for value in objects):
        raw = objects.get(object_id)
        state = raw if isinstance(raw, Mapping) else {}
        transform = state.get("transform") if isinstance(state.get("transform"), Mapping) else {}
        consumed: set[str] = {"transform"}

        def vector(*fields: str, nested: bool = False) -> dict[str, Any] | None:
            source = transform if nested else state
            for field in fields:
                value = source.get(field)
                if isinstance(value, (list, tuple)) and 1 <= len(value) <= 4:
                    values = [_number_or_none(item) for item in value]
                    if all(item is not None for item in values):
                        if not nested:
                            consumed.add(field)
                        return {"field": f"transform.{field}" if nested else field, "values": [float(item) for item in values]}
            return None

        position = vector("position_m", "position_cm", "position") or vector(
            "position_m", "position_cm", "position", nested=True
        )
        rotation = vector("rotation_deg", "rotation_degrees", "rotation", "quaternion") or vector(
            "rotation_deg", "rotation_degrees", "rotation", "quaternion", nested=True
        )
        scale = vector("scale", "scale_xyz") or vector("scale", "scale_xyz", nested=True)
        linear_velocity = vector(
            "linear_velocity_m_s", "velocity_m_s", "velocity_cm_s", "velocity"
        )
        angular_velocity = vector(
            "angular_velocity_rad_s", "angular_velocity_deg_s", "angular_velocity"
        )
        discrete = _discrete_state_fields(state, excluded=consumed)
        result.append(
            {
                "object_id": object_id,
                "transform": {"position": position, "rotation": rotation, "scale": scale},
                "linear_velocity": linear_velocity,
                "angular_velocity": angular_velocity,
                "state": discrete,
            }
        )
    return result


def _discrete_state_fields(state: Mapping[str, Any], *, excluded: set[str] | None = None) -> list[dict[str, Any]]:
    ignored = set(excluded or ())
    candidates: dict[str, Any] = {}
    nested = state.get("state")
    if isinstance(nested, Mapping):
        for field, value in nested.items():
            if _is_json_scalar(value):
                candidates[f"state.{field}"] = value
    names = {
        "active",
        "awake",
        "body_state",
        "enabled",
        "fractured",
        "kinematic",
        "motion_state",
        "sleeping",
        "source",
        "state",
        "status",
        "visible",
    }
    for field, value in state.items():
        name = str(field)
        if name in ignored or isinstance(value, Mapping):
            continue
        if (name in names or name.endswith("_state") or name.endswith("_status")) and _is_json_scalar(value):
            candidates[name] = value
    return [{"field": field, "value": candidates[field]} for field in sorted(candidates)]


def _state_transitions(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prior: dict[str, list[dict[str, Any]]] = {}
    transitions: list[dict[str, Any]] = []
    for frame_index, frame in enumerate(frames):
        objects = frame.get("objects")
        if not isinstance(objects, Mapping):
            continue
        for object_id in sorted(str(value) for value in objects):
            raw = objects.get(object_id)
            current = _discrete_state_fields(raw if isinstance(raw, Mapping) else {})
            if object_id in prior and prior[object_id] != current:
                transitions.append(
                    {
                        "object_id": object_id,
                        "frame_index": frame_index,
                        "time_s": float(_frame_time(frame) or 0.0),
                        "before": prior[object_id],
                        "after": current,
                    }
                )
            prior[object_id] = current
    return transitions


def _is_json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool, int)) or (
        isinstance(value, float) and math.isfinite(value)
    )


def _extract_frame(ffmpeg: str, video: Path, time_s: float, destination: Path, runner: CommandRunner) -> None:
    temporary = destination.with_name(f".{destination.name}.tmp.png")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, time_s):.6f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        "scale=640:-2:flags=lanczos",
        str(temporary),
    ]
    completed = runner(command)
    if completed.returncode != 0 or not _is_png(temporary):
        raise EvidenceBundleError("evidence_keyframe_extraction_failed", f"ffmpeg failed to extract {destination.name}: {completed.stderr[-1000:]}")
    temporary.replace(destination)


def _render_montage(ffmpeg: str, images: list[Path], destination: Path, runner: CommandRunner) -> None:
    if not images:
        raise EvidenceBundleError("evidence_montage_input_missing", "Multi-view montage has no input frames")
    temporary = destination.with_name(f".{destination.name}.tmp.png")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for image in images:
        command.extend(("-i", str(image)))
    filters = [
        f"[{index}:v]scale=480:270:force_original_aspect_ratio=decrease,pad=480:270:(ow-iw)/2:(oh-ih)/2:black[v{index}]"
        for index in range(len(images))
    ]
    filters.append("".join(f"[v{index}]" for index in range(len(images))) + f"hstack=inputs={len(images)}[out]")
    command.extend(("-filter_complex", ";".join(filters), "-map", "[out]", "-frames:v", "1", str(temporary)))
    completed = runner(command)
    if completed.returncode != 0 or not _is_png(temporary):
        raise EvidenceBundleError("evidence_montage_failed", f"ffmpeg failed to create {destination.name}: {completed.stderr[-1000:]}")
    temporary.replace(destination)


def _bundle_artifact(
    bundle_dir: Path,
    artifact_id: str,
    kind: str,
    path: Path,
    *,
    source_ref: str | None,
    time_s: float | None = None,
    view_id: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(bundle_dir) or _path_chain_has_symlink(path, bundle_dir) or not resolved.is_file():
        raise EvidenceBundleError("evidence_artifact_path_invalid", "Evidence artifact escapes the bundle or is a symlink")
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "path": resolved.relative_to(bundle_dir).as_posix(),
        "sha256": _sha256_file(resolved),
        "mime_type": mime_type or mimetypes.guess_type(resolved.name)[0] or "application/json",
        "time_s": time_s,
        "view_id": view_id,
        "source_ref": source_ref,
    }


def _validate_materialized_bundle(bundle_dir: Path, manifest: Mapping[str, Any]) -> None:
    root = bundle_dir.resolve(strict=True)
    for artifact in manifest["artifacts"]:
        raw_path = root / artifact["path"]
        path = raw_path.resolve(strict=True)
        if (
            not path.is_relative_to(root)
            or _path_chain_has_symlink(raw_path, root)
            or not path.is_file()
            or _sha256_file(path) != artifact["sha256"]
        ):
            raise EvidenceBundleError("evidence_artifact_identity_mismatch", f"Evidence artifact identity mismatch: {artifact['artifact_id']}")


def _subprocess_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(command), capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvidenceBundleError("evidence_ffmpeg_failed", str(exc)) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_png(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 8 and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def _frame_time(frame: Mapping[str, Any]) -> float | None:
    return _number_or_none(frame.get("time_s", frame.get("time")))


def _number_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_id(value: str) -> str:
    normalized = "".join(character if character.isalnum() else "_" for character in value.casefold()).strip("_")
    return normalized[:64] or "item"


def _path_chain_has_symlink(path: Path, root: Path) -> bool:
    candidate = path if path.is_absolute() else path.absolute()
    raw_root = root if root.is_absolute() else root.absolute()
    resolved_root = raw_root.resolve(strict=True)
    try:
        relative = candidate.relative_to(raw_root)
    except ValueError:
        matching_root = next(
            (
                parent
                for parent in candidate.parents
                if parent.exists() and parent.resolve(strict=True) == resolved_root
            ),
            None,
        )
        if matching_root is None:
            return True
        relative = candidate.relative_to(matching_root)
        raw_root = matching_root
    current = raw_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False
