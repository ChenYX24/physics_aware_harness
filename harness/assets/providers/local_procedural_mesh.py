from __future__ import annotations

import hashlib
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from harness.assets.providers.contracts import stable_digest


PROVIDER_ID = "local_procedural_mesh_v1"
PROVIDER_VERSION = "1.0.0"
GENERATOR_SOURCE_VERSION = "box_mesh_v1_obj_writer_v1"


class ProceduralGenerationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_generation_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    recipe_id = str(value.get("recipe_id") or "box_mesh_v1").strip()
    recipe_version = str(value.get("recipe_version") or "v1").strip()
    shape = str(value.get("shape") or "").strip().casefold()
    size = value.get("size_m")
    if recipe_id != "box_mesh_v1" or recipe_version != "v1" or shape != "box":
        raise ProceduralGenerationError(
            "unsupported_generation_recipe",
            f"local provider supports only box_mesh_v1/v1 shape=box, got {recipe_id}/{recipe_version} shape={shape}",
        )
    if not isinstance(size, list) or len(size) != 3:
        raise ProceduralGenerationError("invalid_generation_spec", "size_m must contain three positive finite numbers")
    normalized_size: list[float] = []
    for component in size:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise ProceduralGenerationError("invalid_generation_spec", "size_m must contain numbers")
        number = float(component)
        if not math.isfinite(number) or number <= 0.0:
            raise ProceduralGenerationError("invalid_generation_spec", "size_m values must be positive and finite")
        normalized_size.append(number)
    return {
        "recipe_id": recipe_id,
        "recipe_version": recipe_version,
        "shape": shape,
        "size_m": normalized_size,
    }


def recipe_digest(spec: Mapping[str, Any]) -> str:
    return stable_digest(normalize_generation_spec(spec))


def stable_asset_id(spec: Mapping[str, Any]) -> str:
    return f"generated.local.box_mesh_v1.{recipe_digest(spec)[:24]}"


def generate_box_obj(spec: Mapping[str, Any], destination: str | Path) -> dict[str, Any]:
    normalized = normalize_generation_spec(spec)
    sx, sy, sz = (component / 2.0 for component in normalized["size_m"])
    vertices = [
        (-sx, -sy, -sz),
        (sx, -sy, -sz),
        (sx, sy, -sz),
        (-sx, sy, -sz),
        (-sx, -sy, sz),
        (sx, -sy, sz),
        (sx, sy, sz),
        (-sx, sy, sz),
    ]
    faces = [
        (1, 4, 3, 2),
        (5, 6, 7, 8),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (3, 4, 8, 7),
        (4, 1, 5, 8),
    ]
    lines = ["# deterministic centered box_mesh_v1"]
    lines.extend("v " + " ".join(_format_float(value) for value in vertex) for vertex in vertices)
    lines.extend("f " + " ".join(str(index) for index in face) for face in faces)
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
    return {
        "path": path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "format": "obj",
        "role": "generated_source",
        "asset_id": stable_asset_id(normalized),
        "recipe_digest": recipe_digest(normalized),
        "generation_spec": normalized,
    }


def _format_float(value: float) -> str:
    text = format(float(value), ".17g")
    return "0" if text in {"-0", "-0.0"} else text
