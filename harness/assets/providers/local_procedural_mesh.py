from __future__ import annotations

import hashlib
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from harness.assets.providers.contracts import stable_digest


PROVIDER_ID = "local_procedural_mesh_v1"
PROVIDER_VERSION = "1.1.0"
GENERATOR_SOURCE_VERSION = "primitive_mesh_v1_obj_writer_v1"
SPHERE_LATITUDE_SEGMENTS = 16
RADIAL_SEGMENTS = 32

SHAPE_ALIASES = {
    "box": "box",
    "cube": "box",
    "cuboid": "box",
    "plate": "box",
    "wall": "box",
    "sphere": "sphere",
    "ball": "sphere",
    "cylinder": "cylinder",
    "rod": "cylinder",
    "pole": "cylinder",
    "column": "cylinder",
    "disc": "cylinder",
    "disk": "cylinder",
}
RECIPE_BY_SHAPE = {
    "box": "box_mesh_v1",
    "sphere": "sphere_mesh_v1",
    "cylinder": "cylinder_mesh_v1",
}


class ProceduralGenerationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def recipe_for_shape(shape: Any) -> str | None:
    canonical = SHAPE_ALIASES.get(str(shape or "").strip().casefold())
    return RECIPE_BY_SHAPE.get(canonical or "")


def normalize_generation_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    raw_shape = str(value.get("shape") or "").strip().casefold()
    shape = SHAPE_ALIASES.get(raw_shape)
    recipe_version = str(value.get("recipe_version") or "v1").strip()
    recipe_id = str(value.get("recipe_id") or recipe_for_shape(raw_shape) or "").strip()
    expected_recipe = RECIPE_BY_SHAPE.get(shape or "")
    if expected_recipe is None or recipe_id != expected_recipe or recipe_version != "v1":
        raise ProceduralGenerationError(
            "unsupported_generation_recipe",
            "local provider supports box_mesh_v1, sphere_mesh_v1, and cylinder_mesh_v1 at v1; "
            f"got {recipe_id or '<none>'}/{recipe_version} shape={raw_shape or '<none>'}",
        )
    size = value.get("size_m")
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
    if shape == "sphere" and not _approximately_equal(normalized_size):
        raise ProceduralGenerationError(
            "invalid_generation_spec",
            "sphere_mesh_v1 requires equal x/y/z diameters in size_m",
        )
    if shape == "cylinder" and not math.isclose(
        normalized_size[0], normalized_size[1], rel_tol=1e-9, abs_tol=1e-12
    ):
        raise ProceduralGenerationError(
            "invalid_generation_spec",
            "cylinder_mesh_v1 requires equal x/y diameters; size_m z is the cylinder length",
        )
    return {
        "recipe_id": recipe_id,
        "recipe_version": recipe_version,
        "shape": shape,
        "size_m": normalized_size,
    }


def recipe_digest(spec: Mapping[str, Any]) -> str:
    return stable_digest(normalize_generation_spec(spec))


def stable_asset_id(spec: Mapping[str, Any]) -> str:
    normalized = normalize_generation_spec(spec)
    return f"generated.local.{normalized['recipe_id']}.{stable_digest(normalized)[:24]}"


def generate_procedural_obj(spec: Mapping[str, Any], destination: str | Path) -> dict[str, Any]:
    normalized = normalize_generation_spec(spec)
    shape = normalized["shape"]
    if shape == "box":
        vertices, faces = _box_geometry(normalized["size_m"])
    elif shape == "sphere":
        vertices, faces = _sphere_geometry(normalized["size_m"][0])
    else:
        vertices, faces = _cylinder_geometry(normalized["size_m"])
    lines = [f"# deterministic centered {normalized['recipe_id']}"]
    lines.extend("v " + " ".join(_format_float(value) for value in vertex) for vertex in vertices)
    lines.extend("f " + " ".join(str(index) for index in face) for face in faces)
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    path = _atomic_write(destination, payload)
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


def generate_box_obj(spec: Mapping[str, Any], destination: str | Path) -> dict[str, Any]:
    """Compatibility entry point retained for callers of the original provider."""
    return generate_procedural_obj(spec, destination)


def _box_geometry(size: list[float]) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
    sx, sy, sz = (component / 2.0 for component in size)
    vertices = [
        (-sx, -sy, -sz), (sx, -sy, -sz), (sx, sy, -sz), (-sx, sy, -sz),
        (-sx, -sy, sz), (sx, -sy, sz), (sx, sy, sz), (-sx, sy, sz),
    ]
    faces = [
        (1, 4, 3, 2), (5, 6, 7, 8), (1, 2, 6, 5),
        (2, 3, 7, 6), (3, 4, 8, 7), (4, 1, 5, 8),
    ]
    return vertices, faces


def _sphere_geometry(diameter: float) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
    radius = diameter / 2.0
    vertices: list[tuple[float, float, float]] = [(0.0, 0.0, radius)]
    for latitude in range(1, SPHERE_LATITUDE_SEGMENTS):
        phi = math.pi * latitude / SPHERE_LATITUDE_SEGMENTS
        ring_radius = radius * math.sin(phi)
        z = radius * math.cos(phi)
        for segment in range(RADIAL_SEGMENTS):
            theta = 2.0 * math.pi * segment / RADIAL_SEGMENTS
            vertices.append((ring_radius * math.cos(theta), ring_radius * math.sin(theta), z))
    vertices.append((0.0, 0.0, -radius))
    faces: list[tuple[int, ...]] = []
    first_ring = 2
    for segment in range(RADIAL_SEGMENTS):
        current = first_ring + segment
        following = first_ring + (segment + 1) % RADIAL_SEGMENTS
        faces.append((1, current, following))
    for latitude in range(SPHERE_LATITUDE_SEGMENTS - 2):
        upper = first_ring + latitude * RADIAL_SEGMENTS
        lower = upper + RADIAL_SEGMENTS
        for segment in range(RADIAL_SEGMENTS):
            following = (segment + 1) % RADIAL_SEGMENTS
            faces.append((upper + segment, lower + segment, lower + following, upper + following))
    bottom = len(vertices)
    last_ring = bottom - RADIAL_SEGMENTS
    for segment in range(RADIAL_SEGMENTS):
        current = last_ring + segment
        following = last_ring + (segment + 1) % RADIAL_SEGMENTS
        faces.append((current, bottom, following))
    return vertices, faces


def _cylinder_geometry(size: list[float]) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
    radius = size[0] / 2.0
    half_length = size[2] / 2.0
    bottom = []
    top = []
    for segment in range(RADIAL_SEGMENTS):
        theta = 2.0 * math.pi * segment / RADIAL_SEGMENTS
        x = radius * math.cos(theta)
        y = radius * math.sin(theta)
        bottom.append((x, y, -half_length))
        top.append((x, y, half_length))
    vertices = [*bottom, *top]
    faces: list[tuple[int, ...]] = [tuple(range(RADIAL_SEGMENTS, 0, -1))]
    faces.append(tuple(range(RADIAL_SEGMENTS + 1, 2 * RADIAL_SEGMENTS + 1)))
    for segment in range(RADIAL_SEGMENTS):
        following = (segment + 1) % RADIAL_SEGMENTS
        faces.append((segment + 1, following + 1, RADIAL_SEGMENTS + following + 1, RADIAL_SEGMENTS + segment + 1))
    return vertices, faces


def _atomic_write(destination: str | Path, payload: bytes) -> Path:
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
    return path


def _approximately_equal(values: list[float]) -> bool:
    return all(math.isclose(values[0], value, rel_tol=1e-9, abs_tol=1e-12) for value in values[1:])


def _format_float(value: float) -> str:
    text = format(float(value), ".17g")
    return "0" if text in {"-0", "-0.0"} else text
