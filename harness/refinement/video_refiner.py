from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from harness.core.artifact_schema import read_json, write_json
from harness.core.prompt_lineage import prompt_digest, prompt_stage_text, validate_prompt_lineage


JOB_SCHEMA_VERSION = "harness_video_refinement_job_v1"
MANIFEST_SCHEMA_VERSION = "harness_video_refinement_manifest_v1"
SPLICE_MANIFEST_SCHEMA_VERSION = "harness_video_splice_manifest_v1"
MULTIVIEW_SPLIT_MANIFEST_SCHEMA_VERSION = "harness_multiview_split_manifest_v1"
ARK_MODEL_ENDPOINTS = {
    "2.0-fast": "ep-20260805223817-8b4zv",
    "2.5": "ep-20260807200104-j8rs8",
}
ARK_DEFAULT_MODEL = ARK_MODEL_ENDPOINTS["2.0-fast"]


@dataclass(frozen=True)
class RefinementJob:
    job_id: str
    provider: str
    model: str
    prompt: str
    prompt_lineage: dict[str, Any] | None
    prompt_stage_id: str | None
    job_role: str | None
    teacher_validation_path: Path | None
    input_video: Path
    reference_video_uri: str | None
    reference_video_uris: tuple[str, ...]
    reference_image_uris: tuple[str, ...]
    use_reference_video: bool
    output_video: Path
    duration_seconds: int
    aspect_ratio: str
    seed: int
    generate_audio: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RefinementJob:
        if data.get("schema_version") != JOB_SCHEMA_VERSION:
            raise ValueError("unsupported refinement job schema_version")
        provider = data.get("provider")
        if provider not in {"h3_sglang", "ark_seedance"}:
            raise ValueError("unsupported refinement provider")
        model = data.get("model") or (ARK_DEFAULT_MODEL if provider == "ark_seedance" else None)
        required_strings = ("job_id", "provider", "prompt", "input_video", "output_video", "aspect_ratio")
        if any(not isinstance(data.get(key), str) or not data[key].strip() for key in required_strings):
            raise ValueError("refinement job string fields must be non-empty strings")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("refinement model must be a non-empty string")
        if not isinstance(data.get("use_reference_video"), bool):
            raise ValueError("use_reference_video must be a boolean")
        if "generate_audio" in data and not isinstance(data["generate_audio"], bool):
            raise ValueError("generate_audio must be a boolean")
        duration = data.get("duration_seconds")
        adaptive_edit = (
            provider == "ark_seedance"
            and model == ARK_MODEL_ENDPOINTS["2.5"]
            and data.get("use_reference_video") is True
            and data.get("aspect_ratio") == "adaptive"
            and duration == -1
        )
        if isinstance(duration, bool) or not isinstance(duration, int) or not (4 <= duration <= 15 or adaptive_edit):
            raise ValueError("duration_seconds must be 4..15, or -1 for Seedance 2.5 adaptive reference editing")
        seed = data.get("seed", 0)
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        reference_uri = data.get("reference_video_uri")
        if reference_uri is not None and (not isinstance(reference_uri, str) or not reference_uri.strip()):
            raise ValueError("reference_video_uri must be null or a non-empty string")
        reference_uris = data.get("reference_video_uris", [])
        if (
            not isinstance(reference_uris, list)
            or any(not isinstance(uri, str) or not uri.strip() for uri in reference_uris)
            or (reference_uri is not None and reference_uris)
        ):
            raise ValueError("reference_video_uris must be a string list and cannot be combined with reference_video_uri")
        reference_image_uris = data.get("reference_image_uris", [])
        if not isinstance(reference_image_uris, list) or any(
            not isinstance(uri, str) or not uri.strip() for uri in reference_image_uris
        ):
            raise ValueError("reference_image_uris must be a string list")
        prompt_lineage = data.get("prompt_lineage")
        prompt_stage_id = data.get("prompt_stage_id")
        job_role = str(data["job_role"]) if data.get("job_role") else None
        teacher_validation_path = data.get("teacher_validation_path")
        if teacher_validation_path is not None and (
            not isinstance(teacher_validation_path, str) or not teacher_validation_path.strip()
        ):
            raise ValueError("teacher_validation_path must be null or a non-empty string")
        if job_role in {"direct_repair", "ue_refiner"}:
            if not data["use_reference_video"]:
                raise ValueError(f"{job_role} jobs require a reference video")
            if data.get("generate_audio", False):
                raise ValueError(f"{job_role} jobs must disable generated audio; final splice preserves source audio")
        if job_role == "ue_refiner" and teacher_validation_path is None:
            raise ValueError("UE Refiner jobs require teacher_validation_path")
        if (prompt_lineage is None) != (prompt_stage_id is None):
            raise ValueError("prompt_lineage and prompt_stage_id must be provided together")
        if prompt_lineage is not None:
            if not isinstance(prompt_lineage, dict) or not isinstance(prompt_stage_id, str):
                raise ValueError("prompt_lineage must be an object and prompt_stage_id must be a string")
            validate_prompt_lineage(prompt_lineage)
            if str(data["prompt"]) != prompt_stage_text(prompt_lineage, prompt_stage_id):
                raise ValueError("refinement prompt must equal its prompt lineage stage")
            if not data["use_reference_video"] and prompt_stage_id != prompt_lineage.get("canonical_stage_id"):
                raise ValueError("prompt-only generation must use the canonical prompt lineage stage")
            if data.get("job_role") == "ue_refiner" and prompt_stage_id != prompt_lineage.get("refiner_stage_id"):
                raise ValueError("UE Refiner jobs must use the refiner prompt lineage stage")
            if job_role == "direct_repair" and prompt_stage_id != prompt_lineage.get("canonical_stage_id"):
                raise ValueError("direct repair jobs must use the canonical prompt lineage stage")
        return cls(
            job_id=str(data["job_id"]),
            provider=str(provider),
            model=model,
            prompt=str(data["prompt"]),
            prompt_lineage=copy.deepcopy(prompt_lineage),
            prompt_stage_id=prompt_stage_id,
            job_role=job_role,
            teacher_validation_path=Path(teacher_validation_path) if teacher_validation_path else None,
            input_video=Path(data["input_video"]),
            reference_video_uri=reference_uri,
            reference_video_uris=tuple(reference_uris),
            reference_image_uris=tuple(reference_image_uris),
            use_reference_video=data["use_reference_video"],
            output_video=Path(data["output_video"]),
            duration_seconds=duration,
            aspect_ratio=str(data["aspect_ratio"]),
            seed=seed,
            generate_audio=data.get("generate_audio", False),
        )

    def with_reference_video(self, enabled: bool) -> RefinementJob:
        return replace(self, use_reference_video=enabled)

    def with_model(self, model: str) -> RefinementJob:
        if self.provider != "ark_seedance" or not self.use_reference_video:
            return replace(self, model=model)
        if model == ARK_MODEL_ENDPOINTS["2.5"]:
            return replace(self, model=model, duration_seconds=-1, aspect_ratio="adaptive")
        if self.duration_seconds == -1 or self.aspect_ratio == "adaptive":
            raise ValueError("Seedance 2.0 reference edits require a fixed duration and aspect ratio in the job")
        return replace(self, model=model)


def build_payload(job: RefinementJob) -> dict[str, Any]:
    reference_uris = job.reference_video_uris or ((job.reference_video_uri,) if job.reference_video_uri else ())
    if job.use_reference_video and not (reference_uris or job.reference_image_uris):
        raise ValueError("a reference URI is required when reference video is enabled")
    if job.provider == "h3_sglang":
        for uri in (*job.reference_image_uris, *reference_uris):
            _validate_reference_uri("h3_sglang", uri)
        if len(reference_uris) > 3:
            raise ValueError("H3 Ref2VA supports at most three reference videos")
        if len(job.reference_image_uris) > 9:
            raise ValueError("H3 Ref2VA supports at most nine reference images")
        conditions = (
            [
                *({"type": "image", "uri": uri, "role": "reference"} for uri in job.reference_image_uris),
                *({"type": "video", "uri": uri, "role": "reference"} for uri in reference_uris),
            ]
            if job.use_reference_video
            else []
        )
        return {
            "model": job.model,
            "prompt": job.prompt,
            "seconds": job.duration_seconds,
            "task": "ref2va" if conditions else "t2va",
            "conditions": conditions,
            "target": {
                "short_edge": 768,
                "aspect_ratio": job.aspect_ratio,
                "duration_seconds": float(job.duration_seconds),
            },
            "num_outputs_per_prompt": 1,
            "num_inference_steps": 50,
            "flow_shift": 12.0,
            "audio_flow_shift": 3.0,
            "seed": job.seed,
        }
    if job.provider == "ark_seedance":
        for uri in (*job.reference_image_uris, *reference_uris):
            _validate_reference_uri("ark_seedance", uri)
        content: list[dict[str, Any]] = [{"type": "text", "text": job.prompt}]
        if job.use_reference_video:
            content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": uri},
                    "role": "reference_image",
                }
                for uri in job.reference_image_uris
            )
            content.extend(
                {
                    "type": "video_url",
                    "video_url": {"url": uri},
                    "role": "reference_video",
                }
                for uri in reference_uris
            )
        return {
            "model": job.model,
            "content": content,
            "generate_audio": job.generate_audio,
            "ratio": job.aspect_ratio,
            "duration": job.duration_seconds,
            "seed": job.seed,
            "watermark": False,
        }
    raise ValueError(f"unsupported refinement provider: {job.provider}")


def _validate_reference_uri(provider: str, uri: str) -> None:
    parsed = urlsplit(uri)
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("reference URIs cannot contain credentials or fragments")
    if provider == "h3_sglang" and parsed.scheme == "file" and not parsed.netloc and parsed.path.startswith("/"):
        return
    if parsed.scheme != "https" or not parsed.hostname or _host_is_local(parsed.hostname):
        label = "Ark" if provider == "ark_seedance" else "H3"
        suffix = "must use public HTTPS" if provider == "ark_seedance" else "must use an absolute file URI or public HTTPS"
        raise ValueError(f"{label} reference URIs {suffix}")


def _host_is_local(hostname: str) -> bool:
    if hostname.casefold() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return not address.is_global


def validate_comparison_jobs(jobs: list[RefinementJob]) -> dict[str, Any]:
    if not jobs:
        raise ValueError("comparison requires at least one job")
    if any(job.prompt_lineage is None for job in jobs):
        raise ValueError("comparison jobs require prompt_lineage; legacy jobs are not fair-comparison evidence")
    canonical_prompts = {
        prompt_stage_text(job.prompt_lineage or {}, str((job.prompt_lineage or {}).get("canonical_stage_id") or ""))
        for job in jobs
    }
    if len(canonical_prompts) != 1:
        raise ValueError("comparison jobs must share one canonical generation prompt")
    prompt_only = [job for job in jobs if not job.use_reference_video]
    direct_repairs = [job for job in jobs if job.job_role == "direct_repair"]
    if prompt_only and direct_repairs:
        raise ValueError("prompt-only generation and direct video repair are different comparison workflows")
    if any(job.prompt not in canonical_prompts for job in [*prompt_only, *direct_repairs]):
        raise ValueError("every prompt-only model must receive the canonical generation prompt verbatim")
    if len({job.input_video.resolve() for job in direct_repairs}) > 1:
        raise ValueError("every direct repair model must use the same original error video")
    ue_refiners = [job for job in jobs if job.job_role == "ue_refiner"]
    if len({job.prompt for job in ue_refiners}) > 1:
        raise ValueError("every UE Refiner model must receive the same appearance-only prompt verbatim")
    if len({job.input_video.resolve() for job in ue_refiners}) > 1:
        raise ValueError("every UE Refiner model must use the same validated UE teacher")
    teacher_receipts = [validate_ue_teacher(job) for job in ue_refiners]
    return {
        "schema_version": "harness_video_comparison_validation_v1",
        "status": "pass",
        "job_count": len(jobs),
        "comparison_kind": "video_repair" if direct_repairs else "fixed_prompt_generation",
        "prompt_only_job_count": len(prompt_only),
        "direct_repair_job_count": len(direct_repairs),
        "ue_refiner_job_count": len(ue_refiners),
        "teacher_validation_sha256": prompt_digest(teacher_receipts[0]) if teacher_receipts else None,
        "canonical_prompt_sha256": prompt_digest(next(iter(canonical_prompts))),
        "refiner_prompt_sha256": prompt_digest(ue_refiners[0].prompt) if ue_refiners else None,
        "job_ids": [job.job_id for job in jobs],
    }


def validate_ue_teacher(job: RefinementJob) -> dict[str, Any]:
    if job.job_role != "ue_refiner" or job.teacher_validation_path is None:
        raise ValueError("teacher validation is only defined for UE Refiner jobs")
    if not job.input_video.is_file():
        raise ValueError(f"UE teacher video does not exist: {job.input_video}")
    receipt = read_json(job.teacher_validation_path)
    required_checks = {"canonical_prompt_match", "event_contract", "physics_hard_gate", "no_penetration"}
    checks = receipt.get("checks")
    canonical_prompt = prompt_stage_text(job.prompt_lineage or {}, str((job.prompt_lineage or {}).get("canonical_stage_id") or ""))
    if (
        receipt.get("schema_version") != "harness_ue_teacher_validation_v1"
        or receipt.get("status") != "pass"
        or receipt.get("input_sha256") != _sha256(job.input_video)
        or receipt.get("canonical_prompt_sha256") != prompt_digest(canonical_prompt)
        or not isinstance(checks, dict)
        or any(checks.get(name) != "pass" for name in required_checks)
    ):
        raise ValueError("UE teacher validation must bind the canonical prompt and pass event, physics, and penetration gates")
    return receipt


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_video(path: Path, ffprobe: str) -> dict[str, Any]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-count_frames",
        "-show_entries",
        "stream=codec_type,width,height,avg_frame_rate,nb_read_frames:format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not isinstance(video, dict):
        raise ValueError(f"video stream not found: {path}")
    try:
        frame_count = int(video["nb_read_frames"])
        fps = Fraction(video["avg_frame_rate"])
        width = int(video["width"])
        height = int(video["height"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise ValueError(f"incomplete video metadata: {path}") from error
    if frame_count <= 0 or fps <= 0 or width <= 0 or height <= 0:
        raise ValueError(f"invalid video metadata: {path}")
    duration = data.get("format", {}).get("duration")
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "frame_count": frame_count,
        "duration_seconds": float(duration) if duration is not None else float(Fraction(frame_count, 1) / fps),
        "fps": f"{fps.numerator}/{fps.denominator}",
        "width": width,
        "height": height,
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def split_multiview_grid(
    input_video: str | Path,
    output_dir: str | Path,
    view_ids: list[str] | tuple[str, ...],
    *,
    columns: int = 3,
    rows: int = 2,
    manifest_path: str | Path | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> dict[str, Any]:
    source_path = Path(input_video)
    destination = Path(output_dir)
    if not source_path.is_file():
        raise ValueError(f"input grid does not exist: {source_path}")
    if columns <= 0 or rows <= 0 or not view_ids or len(view_ids) > columns * rows:
        raise ValueError("view_ids must fit inside a positive rows-by-columns grid")
    if len(set(view_ids)) != len(view_ids) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", view) for view in view_ids):
        raise ValueError("view_ids must be unique filesystem-safe names")
    source = _probe_video(source_path, ffprobe)
    cell_width = source["width"] // columns // 2 * 2
    cell_height = source["height"] // rows // 2 * 2
    if cell_width <= 0 or cell_height <= 0:
        raise ValueError("grid cells are too small to encode")
    destination.mkdir(parents=True, exist_ok=True)
    outputs = []
    for index, view_id in enumerate(view_ids):
        output = destination / f"{view_id}.mp4"
        x = index % columns * cell_width
        y = index // columns * cell_height
        subprocess.run(
            [
                ffmpeg, "-y", "-v", "error", "-i", str(source_path),
                "-vf", f"crop={cell_width}:{cell_height}:{x}:{y}",
                "-an", "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
                str(output),
            ],
            check=True,
        )
        probe = _probe_video(output, ffprobe)
        if probe["frame_count"] != source["frame_count"]:
            raise ValueError(f"cropped view frame count mismatch: {view_id}")
        outputs.append({"view_id": view_id, "grid_index": index, "crop": [x, y, cell_width, cell_height], **probe})
    result = {
        "schema_version": MULTIVIEW_SPLIT_MANIFEST_SCHEMA_VERSION,
        "source": source,
        "grid": {"columns": columns, "rows": rows, "cell_width": cell_width, "cell_height": cell_height},
        "outputs": outputs,
    }
    target_manifest = Path(manifest_path) if manifest_path else destination / "split_manifest.json"
    if target_manifest.resolve() in {source_path.resolve(), *(Path(item["path"]).resolve() for item in outputs)}:
        raise ValueError("split manifest must not overwrite source or output videos")
    write_json(target_manifest, result)
    return result


def _video_filter(
    input_index: int,
    start_frame: int,
    end_frame: int,
    label: str,
    *,
    fps: Fraction,
    width: int,
    height: int,
) -> str:
    fps_value = f"{float(fps):.12g}"
    return (
        f"[{input_index}:v:0]trim=start_frame={start_frame}:end_frame={end_frame},"
        f"setpts=N/({fps_value}*TB),"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[{label}]"
    )


def _audio_filter(
    input_index: int,
    has_audio: bool,
    start_seconds: float,
    duration_seconds: float,
    label: str,
) -> str:
    start = f"{start_seconds:.12g}"
    duration = f"{duration_seconds:.12g}"
    if has_audio:
        source = f"[{input_index}:a:0]atrim=start={start}:duration={duration}"
    else:
        source = f"anullsrc=r=48000:cl=stereo,atrim=duration={duration}"
    return (
        f"{source},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,"
        f"apad=pad_dur={duration},atrim=duration={duration}[{label}]"
    )


def splice_video(
    source: str | Path,
    replacement: str | Path,
    output: str | Path,
    *,
    start_frame: int,
    end_frame: int,
    mode: str,
    manifest_path: str | Path,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> dict[str, Any]:
    source = Path(source)
    replacement = Path(replacement)
    output = Path(output)
    manifest_path = Path(manifest_path)
    if not source.is_file() or not replacement.is_file():
        raise ValueError("source and replacement videos must exist")
    if output.resolve() in {source.resolve(), replacement.resolve()}:
        raise ValueError("splice output must not overwrite an input")
    if manifest_path.resolve() in {source.resolve(), replacement.resolve(), output.resolve()}:
        raise ValueError("splice manifest must not overwrite a video")
    if mode not in {"replace", "insert"}:
        raise ValueError("splice mode must be replace or insert")

    source_info = _probe_video(source, ffprobe)
    replacement_info = _probe_video(replacement, ffprobe)
    if source_info["fps"] != replacement_info["fps"]:
        raise ValueError("splice requires matching CFR frame rates; normalize the replacement first")
    source_frames = source_info["frame_count"]
    if not 0 <= start_frame <= end_frame <= source_frames:
        raise ValueError("splice frame interval is outside the source video")
    if mode == "replace" and start_frame == end_frame:
        raise ValueError("replace requires a non-empty half-open frame interval")
    if mode == "insert" and start_frame != end_frame:
        raise ValueError("insert requires start_frame == end_frame")
    replacement_start = 0
    replacement_end = replacement_info["frame_count"]
    if mode == "replace":
        if replacement_info["frame_count"] >= source_frames:
            replacement_start, replacement_end = start_frame, end_frame
        elif replacement_info["frame_count"] != end_frame - start_frame:
            raise ValueError("replace requires an aligned full video or one replacement frame per source frame")

    fps = Fraction(source_info["fps"])
    segments: list[tuple[int, int, int, dict[str, Any]]] = []
    if start_frame:
        segments.append((0, 0, start_frame, source_info))
    segments.append((1, replacement_start, replacement_end, replacement_info))
    tail_start = end_frame if mode == "replace" else start_frame
    if tail_start < source_frames:
        segments.append((0, tail_start, source_frames, source_info))

    filters: list[str] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []
    for index, (input_index, frame_start, frame_end, info) in enumerate(segments):
        video_label = f"v{index}"
        audio_label = f"a{index}"
        frame_duration = float(Fraction(frame_end - frame_start, 1) / fps)
        filters.append(
            _video_filter(
                input_index,
                frame_start,
                frame_end,
                video_label,
                fps=fps,
                width=source_info["width"],
                height=source_info["height"],
            )
        )
        video_labels.append(f"[{video_label}]")
        if mode == "insert":
            filters.append(
                _audio_filter(
                    input_index,
                    info["has_audio"],
                    float(Fraction(frame_start, 1) / fps) if input_index == 0 else 0.0,
                    frame_duration,
                    audio_label,
                )
            )
            audio_labels.append(f"[{audio_label}]")
    filters.append(f"{''.join(video_labels)}concat=n={len(segments)}:v=1:a=0[outv]")
    if mode == "insert":
        filters.append(f"{''.join(audio_labels)}concat=n={len(segments)}:v=0:a=1[outa]")
    elif not source_info["has_audio"]:
        filters.append(_audio_filter(0, False, 0.0, float(Fraction(source_frames, 1) / fps), "outa"))

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.stem}.", suffix=output.suffix, dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    expected_frames = source_frames - (end_frame - start_frame) + replacement_end - replacement_start
    copy_source_audio = mode == "replace" and source_info["has_audio"]
    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-i",
        str(replacement),
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[outv]",
        "-map",
        "0:a:0" if copy_source_audio else "[outa]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-r",
        source_info["fps"],
        "-fps_mode",
        "cfr",
        *( ["-c:a", "copy"] if copy_source_audio else ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"] ),
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    manifest: dict[str, Any] = {
        "schema_version": SPLICE_MANIFEST_SCHEMA_VERSION,
        "status": "running",
        "failure_reason": None,
        "mode": mode,
        "frame_interval": {"start": start_frame, "end": end_frame, "semantics": "half_open"},
        "ffmpeg_command": command,
        "audio_strategy": (
            "copy_original_source_audio_stream"
            if copy_source_audio
            else "insert_source_or_silence_segments_as_aac" if mode == "insert" else "source_has_no_audio_generate_silence"
        ),
        "source": source_info,
        "replacement": replacement_info,
        "expected_output_frames": expected_frames,
        "output": None,
    }
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"ffmpeg exited with {result.returncode}")
        staged_info = _probe_video(temporary, ffprobe)
        if staged_info["frame_count"] != expected_frames:
            raise RuntimeError(
                f"frame count mismatch: expected {expected_frames}, got {staged_info['frame_count']}"
            )
        os.replace(temporary, output)
        staged_info.update(path=str(output), sha256=_sha256(output))
        manifest.update(status="succeeded", output=staged_info)
        write_json(manifest_path, manifest)
        return manifest
    except Exception as error:
        manifest.update(status="failed", failure_reason=str(error))
        write_json(manifest_path, manifest)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _redact(value: Any, *, key: str = "") -> Any:
    if key.lower() in {"api_key", "authorization", "token", "access_token"}:
        return "<redacted>"
    if isinstance(value, dict):
        return {item_key: _redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and (parsed.query or parsed.fragment):
            return urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    "<redacted>" if parsed.query else "",
                    "<redacted>" if parsed.fragment else "",
                )
            )
    return value


def dry_run_plan(job: RefinementJob, *, base_url: str) -> dict[str, Any]:
    if not job.input_video.is_file():
        raise ValueError(f"input video does not exist: {job.input_video}")
    teacher_validation = validate_ue_teacher(job) if job.job_role == "ue_refiner" else None
    root = base_url.rstrip("/")
    if job.provider == "h3_sglang":
        _validate_h3_base_url(job, root)
        submit_url = f"{root}/v1/videos"
        headers = {"Content-Type": "application/json"}
    elif job.provider == "ark_seedance":
        _validate_ark_base_url(root)
        submit_url = f"{root}/contents/generations/tasks"
        headers = {"Content-Type": "application/json", "Authorization": "<redacted>"}
    else:
        raise ValueError(f"unsupported refinement provider: {job.provider}")
    return {
        "dry_run": True,
        "job_id": job.job_id,
        "provider": job.provider,
        "job_role": job.job_role,
        "prompt": job.prompt,
        "prompt_sha256": prompt_digest(job.prompt),
        "prompt_stage_id": job.prompt_stage_id,
        "prompt_lineage": copy.deepcopy(job.prompt_lineage),
        "legacy_prompt_lineage_missing": job.prompt_lineage is None,
        "teacher_validation_path": str(job.teacher_validation_path) if job.teacher_validation_path else None,
        "teacher_validation_sha256": prompt_digest(teacher_validation) if teacher_validation else None,
        "submit_url": submit_url,
        "headers": headers,
        "payload": _redact(build_payload(job)),
        "input_sha256": _sha256(job.input_video),
        "output_video": str(job.output_video),
    }


def _validate_h3_base_url(job: RefinementJob, base_url: str) -> None:
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"invalid H3 base URL: {base_url}") from error
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.query or parsed.fragment:
        raise ValueError("H3 base URL must be a local HTTP endpoint without query or fragment")
    expected_ports = {30011, 30012} if job.use_reference_video else {30010}
    profile = "Ref2VA" if job.use_reference_video else "FL2VA"
    if port not in expected_ports:
        ports = " or ".join(str(item) for item in sorted(expected_ports))
        raise ValueError(f"H3 {profile} jobs require the matching service on port {ports}")


def _validate_ark_base_url(base_url: str) -> None:
    parsed = urlsplit(base_url)
    allowed = {"ark.cn-beijing.volces.com", "ark-cn-beijing.bytedance.net"}
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed
        or parsed.path.rstrip("/") != "/api/v3"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Ark base URL must use an approved HTTPS /api/v3 endpoint")


def _validate_refinement_paths(job: RefinementJob, manifest_path: Path, prompt_lineage_path: Path | None = None) -> None:
    paths = [job.input_video.resolve(), job.output_video.resolve(), manifest_path.resolve()]
    if prompt_lineage_path is not None:
        paths.append(prompt_lineage_path.resolve())
    if job.teacher_validation_path is not None:
        paths.append(job.teacher_validation_path.resolve())
    if len(set(paths)) != len(paths):
        raise ValueError("input, output, manifest, prompt lineage, and teacher validation paths must be distinct")


def _request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None,
    headers: dict[str, str],
    opener: Callable[..., Any],
    timeout: float,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    with opener(request, timeout=timeout) as response:
        body = response.read()
    decoded = json.loads(body)
    if not isinstance(decoded, dict):
        raise ValueError("refiner response must be a JSON object")
    return decoded


def _failure_reason(response: dict[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        if code and message:
            return f"{code}: {message}"
        if message or code:
            return str(message or code)
    if isinstance(error, str) and error:
        return error
    return "refinement failed"


def _download(
    url: str,
    output: Path,
    *,
    headers: dict[str, str],
    opener: Callable[..., Any],
    timeout: float,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            request = Request(url, headers=headers, method="GET")
            with opener(request, timeout=timeout) as response:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def run_refinement(
    job: RefinementJob,
    *,
    base_url: str,
    manifest_path: str | Path,
    opener: Callable[..., Any] = urlopen,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 1.0,
    max_polls: int = 720,
    timeout: float = 60.0,
) -> dict[str, Any]:
    if not job.input_video.is_file():
        raise ValueError(f"input video does not exist: {job.input_video}")
    teacher_validation = validate_ue_teacher(job) if job.job_role == "ue_refiner" else None
    payload = build_payload(job)
    input_sha256 = _sha256(job.input_video)
    prompt_sha256 = prompt_digest(job.prompt)
    payload_sha256 = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest_path = Path(manifest_path)
    prompt_lineage_path = (
        manifest_path.with_name(f"{manifest_path.stem}.prompt_lineage.json")
        if job.prompt_lineage is not None
        else None
    )
    _validate_refinement_paths(job, manifest_path, prompt_lineage_path)
    root = base_url.rstrip("/")
    if job.provider == "h3_sglang":
        _validate_h3_base_url(job, root)
    elif job.provider == "ark_seedance":
        _validate_ark_base_url(root)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "job_id": job.job_id,
        "provider": job.provider,
        "model": job.model,
        "job_role": job.job_role,
        "task_id": None,
        "status": "submitting",
        "failure_reason": None,
        "use_reference_video": job.use_reference_video,
        "prompt": job.prompt,
        "prompt_sha256": prompt_sha256,
        "prompt_stage_id": job.prompt_stage_id,
        "prompt_lineage_path": str(prompt_lineage_path) if prompt_lineage_path else None,
        "prompt_lineage_sha256": prompt_digest(job.prompt_lineage) if job.prompt_lineage is not None else None,
        "legacy_prompt_lineage_missing": job.prompt_lineage is None,
        "teacher_validation_path": str(job.teacher_validation_path) if job.teacher_validation_path else None,
        "teacher_validation_sha256": prompt_digest(teacher_validation) if teacher_validation else None,
        "input_video": str(job.input_video),
        "input_sha256": input_sha256,
        "output_video": str(job.output_video),
        "output_sha256": None,
        "payload_sha256": payload_sha256,
        "base_url": root,
    }
    task_id: str | None = None
    if manifest_path.exists():
        previous = read_json(manifest_path)
        expected = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "job_id": job.job_id,
            "provider": job.provider,
            "model": job.model,
            "use_reference_video": job.use_reference_video,
            "input_sha256": input_sha256,
            "payload_sha256": payload_sha256,
            "output_video": str(job.output_video),
            "base_url": root,
        }
        if job.prompt_lineage is not None:
            expected.update(
                prompt_sha256=prompt_sha256,
                prompt_stage_id=job.prompt_stage_id,
                prompt_lineage_path=str(prompt_lineage_path),
                prompt_lineage_sha256=prompt_digest(job.prompt_lineage),
            )
        if teacher_validation is not None:
            expected.update(
                teacher_validation_path=str(job.teacher_validation_path),
                teacher_validation_sha256=prompt_digest(teacher_validation),
            )
        if not isinstance(previous, dict) or any(previous.get(key) != value for key, value in expected.items()):
            raise ValueError("existing manifest does not match refinement job")
        manifest = previous
        if manifest.get("status") == "succeeded":
            if not job.output_video.is_file() or manifest.get("output_sha256") != _sha256(job.output_video):
                raise ValueError("completed refinement output does not match manifest")
            if manifest.get("failure_reason") is not None:
                manifest["failure_reason"] = None
                write_json(manifest_path, manifest)
            return manifest
        if manifest.get("status") == "failed":
            return manifest
        task_id = manifest.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            # ponytail: a crash between POST and task-id persistence is ambiguous;
            # add provider-side idempotency or task-list recovery before retrying it.
            raise ValueError("existing manifest has an indeterminate submission; refusing duplicate submit")
    if job.provider == "h3_sglang":
        headers = {"Content-Type": "application/json"}
        submit_url = f"{root}/v1/videos"
        poll_url = lambda identifier: f"{root}/v1/videos/{identifier}"
    elif job.provider == "ark_seedance":
        api_key = os.environ.get("ARK_API_KEY")
        if not api_key:
            raise ValueError("ARK_API_KEY is required for Ark refinement")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        submit_url = f"{root}/contents/generations/tasks"
        poll_url = lambda identifier: f"{root}/contents/generations/tasks/{identifier}"
    else:
        raise ValueError(f"unsupported refinement provider: {job.provider}")

    if task_id is None:
        if prompt_lineage_path is not None:
            write_json(prompt_lineage_path, job.prompt_lineage)
        write_json(manifest_path, manifest)
        try:
            submitted = _request_json(
                "POST", submit_url, payload=payload, headers=headers, opener=opener, timeout=timeout
            )
            task_id = submitted.get("id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("refiner submit response is missing task id")
        except HTTPError as error:
            detail = ""
            try:
                response = json.loads(error.read(4096))
                if isinstance(response, dict):
                    detail = _failure_reason(response)
            except Exception:
                pass
            error.close()
            reason = f"submit rejected: HTTP {error.code}"
            if detail and detail != "refinement failed":
                reason = f"{reason}: {detail[:500]}"
            manifest.update(status="failed", failure_reason=reason)
            write_json(manifest_path, manifest)
            return manifest
        except ValueError as error:
            manifest.update(status="failed", failure_reason=str(error))
            write_json(manifest_path, manifest)
            return manifest
        except URLError as error:
            if isinstance(error.reason, ConnectionRefusedError):
                manifest.update(status="failed", failure_reason="submit not sent: connection refused")
                write_json(manifest_path, manifest)
                return manifest
            manifest.update(status="indeterminate", failure_reason="submit outcome unknown: URLError")
            write_json(manifest_path, manifest)
            raise
        except Exception as error:
            manifest.update(status="indeterminate", failure_reason=f"submit outcome unknown: {type(error).__name__}")
            write_json(manifest_path, manifest)
            raise
        manifest.update(task_id=task_id, status="submitted")
        write_json(manifest_path, manifest)

    for _attempt in range(max_polls):
        try:
            response = _request_json(
                "GET", poll_url(task_id), payload=None, headers=headers, opener=opener, timeout=timeout
            )
        except HTTPError as error:
            error.close()
            if error.code != 404:
                raise
            manifest.update(status="failed", failure_reason="poll failed: HTTP 404 task not found")
            write_json(manifest_path, manifest)
            return manifest
        except (URLError, ConnectionError):
            sleep(poll_interval)
            continue
        status = response.get("status")
        if status in {"completed", "succeeded"}:
            if job.provider == "h3_sglang":
                content_url = f"{root}/v1/videos/{task_id}/content"
                download_headers = headers
            else:
                content = response.get("content")
                content_url = content.get("video_url") if isinstance(content, dict) else None
                if not isinstance(content_url, str) or not content_url:
                    raise ValueError("Ark success response is missing content.video_url")
                _validate_reference_uri("ark_seedance", content_url)
                download_headers = {}
            _download(
                content_url,
                job.output_video,
                headers=download_headers,
                opener=opener,
                timeout=timeout,
            )
            manifest.update(
                status="succeeded",
                failure_reason=None,
                output_sha256=_sha256(job.output_video),
            )
            write_json(manifest_path, manifest)
            return manifest
        if status in {"failed", "cancelled"}:
            manifest.update(status="failed", failure_reason=_failure_reason(response))
            write_json(manifest_path, manifest)
            return manifest
        manifest.update(
            status="running" if status in {"running", "in_progress", "processing"} else "queued",
            failure_reason=None,
        )
        write_json(manifest_path, manifest)
        sleep(poll_interval)

    manifest["failure_reason"] = "poll limit exceeded; rerun to resume task"
    write_json(manifest_path, manifest)
    return manifest
