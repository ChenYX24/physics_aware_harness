from __future__ import annotations

import hashlib
import json
import os
import traceback
from pathlib import Path
from typing import Any

import unreal


REQUEST_PATH = Path(os.environ["SIM_HARNESS_UE_MAP_QUALIFICATION_REQUEST"]).expanduser().resolve()
RESULT_PATH = Path(os.environ["SIM_HARNESS_UE_MAP_QUALIFICATION_RESULT"]).expanduser().resolve()
SCHEMA = "harness_prepared_map_qualification_v1"


def main() -> None:
    request = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
    try:
        requested = canonical_map_package(str(request["map_package"]))
        map_file = Path(request["map_file"]).expanduser().resolve()
        if not map_file.is_file():
            raise RuntimeError(f"materialized Map file is missing: {map_file}")
        map_sha256 = sha256_file(map_file)
        if map_sha256 != request["map_sha256"]:
            raise RuntimeError("materialized Map hash differs from the registration receipt")
        opened, errors = try_open_map(requested)
        opened_package = current_world_package()
        if not opened or opened_package != requested:
            raise RuntimeError("; ".join(errors) or f"could not open requested Map: {requested}")
        editor = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        actors = list(editor.get_all_level_actors()) if editor is not None else []
        class_counts: dict[str, int] = {}
        for actor in actors:
            class_name = actor.get_class().get_name()
            class_counts[class_name] = class_counts.get(class_name, 0) + 1
        if not actors:
            raise RuntimeError("opened Map contains no loaded actors")
        result = {
            "schema_version": SCHEMA,
            "status": "pass",
            "asset_id": request["asset_id"],
            "requested_package": requested,
            "opened_package": opened_package,
            "map_file": str(map_file),
            "map_sha256": map_sha256,
            "loaded_actor_count": len(actors),
            "actor_class_counts": dict(sorted(class_counts.items())),
            "world_class": unreal.EditorLevelLibrary.get_editor_world().get_class().get_name(),
            "qualification_scope": "map_load_and_actor_inventory",
            "observability_smoke_required_next": True,
        }
    except Exception as exc:
        result = {
            "schema_version": SCHEMA,
            "status": "fail",
            "asset_id": request.get("asset_id"),
            "requested_package": request.get("map_package"),
            "opened_package": current_world_package(),
            "map_file": request.get("map_file"),
            "map_sha256": request.get("map_sha256"),
            "loaded_actor_count": 0,
            "failure_code": "prepared_map_ue_qualification_failed",
            "failure_message": str(exc),
            "traceback": traceback.format_exc(limit=20),
        }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def canonical_map_package(value: str) -> str:
    text = str(value or "").strip().split(":", 1)[0]
    dot = text.find(".", text.rfind("/"))
    return text[:dot] if dot >= 0 else text.rstrip("/")


def current_world_package() -> str | None:
    try:
        world = unreal.EditorLevelLibrary.get_editor_world()
        return canonical_map_package(world.get_path_name()) if world else None
    except Exception:
        return None


def try_open_map(path: str) -> tuple[bool, list[str]]:
    requested = canonical_map_package(path)
    errors: list[str] = []
    for loader in (unreal.EditorLevelLibrary.load_level, unreal.EditorLoadingAndSavingUtils.load_map):
        try:
            loader(requested)
        except Exception as exc:
            errors.append(str(exc))
        actual = current_world_package()
        if actual == requested:
            return True, errors
        errors.append(f"loaded_world_mismatch:requested={requested},actual={actual}")
    return False, errors


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


main()
