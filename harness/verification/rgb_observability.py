from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from harness.core.artifact_schema import write_json


def verify_expected_color_observability(
    run_dir: str | Path,
    *,
    expected_rgb: list[float],
    view_ids: list[str],
    sample_size: int = 64,
    minimum_channel_margin: int = 12,
    minimum_pixel_fraction: float = 0.002,
    minimum_visible_frame_fraction: float = 0.6,
    write: bool = True,
) -> dict[str, Any]:
    """Cheap RGB diagnostic for a case with an intentionally distinctive material.

    This is not a realism score or a publication gate. Geometry and identity
    remain the responsibility of depth/segmentation and semantic review.
    """
    if len(expected_rgb) != 3:
        raise ValueError("expected_rgb must contain three channels")
    dominant_channel = max(range(3), key=lambda index: float(expected_rgb[index]))
    if list(expected_rgb).count(expected_rgb[dominant_channel]) > 1:
        raise ValueError("expected_rgb must have one dominant channel")

    per_view: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    for view_id in view_ids:
        video = Path(run_dir) / "views" / view_id / "rgb.mp4"
        frames = decode_rgb_video(video, sample_size=sample_size)
        visible: list[bool] = []
        pixel_fractions: list[float] = []
        for frame in frames:
            dominant_pixels = 0
            pixel_count = sample_size * sample_size
            for offset in range(0, len(frame), 3):
                channels = frame[offset : offset + 3]
                other_max = max(channels[index] for index in range(3) if index != dominant_channel)
                if channels[dominant_channel] - other_max >= minimum_channel_margin:
                    dominant_pixels += 1
            fraction = dominant_pixels / pixel_count
            pixel_fractions.append(fraction)
            visible.append(fraction >= minimum_pixel_fraction)
        visible_fraction = sum(visible) / len(visible) if visible else 0.0
        status = "pass" if frames and visible_fraction >= minimum_visible_frame_fraction else "fail"
        per_view[view_id] = {
            "status": status,
            "path": str(video),
            "frame_count": len(frames),
            "visible_frame_count": sum(visible),
            "visible_frame_fraction": round(visible_fraction, 6),
            "minimum_pixel_fraction": minimum_pixel_fraction,
            "pixel_fraction_min": round(min(pixel_fractions), 6) if pixel_fractions else None,
            "pixel_fraction_median": round(sorted(pixel_fractions)[len(pixel_fractions) // 2], 6) if pixel_fractions else None,
            "pixel_fraction_max": round(max(pixel_fractions), 6) if pixel_fractions else None,
        }
        if status == "fail":
            failures.append(
                {
                    "code": "F_RGB_EXPECTED_SUBJECT_NOT_OBSERVABLE",
                    "view_id": view_id,
                    "message": "RGB video does not show the expected distinctive subject color in enough frames",
                }
            )

    report = {
        "schema_version": "harness_rgb_observability_v1",
        "status": "pass" if not failures else "fail",
        "expected_rgb": [float(value) for value in expected_rgb],
        "dominant_channel": ("red", "green", "blue")[dominant_channel],
        "minimum_channel_margin": minimum_channel_margin,
        "minimum_visible_frame_fraction": minimum_visible_frame_fraction,
        "views": per_view,
        "failure_codes": sorted({str(item["code"]) for item in failures}),
        "failures": failures,
        "enforcement": "advisory",
        "scope": "cheap color observability diagnostic; not a perceptual-quality or physical-correctness metric",
    }
    if write:
        write_json(Path(run_dir) / "rgb_observability_report.json", report)
    return report


def decode_rgb_video(path: Path, *, sample_size: int) -> list[bytes]:
    if not path.is_file():
        return []
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-vf",
                f"scale={sample_size}:{sample_size}:flags=neighbor",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    frame_bytes = sample_size * sample_size * 3
    output = completed.stdout
    if completed.returncode != 0 or not isinstance(output, bytes) or not output or len(output) % frame_bytes:
        return []
    return [output[start : start + frame_bytes] for start in range(0, len(output), frame_bytes)]
