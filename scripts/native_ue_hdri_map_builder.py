"""Import selected HDRIs and build reusable HDRIBackdrop prepared Maps in UE."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import unreal


MANIFEST = Path(os.environ["SIM_HARNESS_HDRI_POOL_MANIFEST"]).expanduser().resolve()
RESULT = Path(os.environ["SIM_HARNESS_HDRI_MAP_RESULT"]).expanduser().resolve()
SELECTED = {
    item.strip()
    for item in os.environ.get("SIM_HARNESS_HDRI_ASSET_IDS", "").split(",")
    if item.strip()
}
CONTENT_ROOT = "/Game/PolyHavenHDRI"
TEXTURE_ROOT = f"{CONTENT_ROOT}/Textures"
MAP_ROOT = f"{CONTENT_ROOT}/Maps"


def safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_") or "unnamed"


def import_hdr(source: Path, asset_id: str):
    name = safe_name(f"{asset_id}_2k")
    object_path = f"{TEXTURE_ROOT}/{name}.{name}"
    existing = unreal.load_asset(object_path)
    if existing:
        return existing, object_path, False
    task = unreal.AssetImportTask()
    task.set_editor_property("automated", True)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("destination_path", TEXTURE_ROOT)
    task.set_editor_property("filename", str(source))
    task.set_editor_property("replace_existing", False)
    task.set_editor_property("save", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    imported_paths = list(task.get_editor_property("imported_object_paths") or [])
    texture = unreal.load_asset(object_path)
    if texture is None and imported_paths:
        object_path = str(imported_paths[0])
        texture = unreal.load_asset(object_path)
    if texture is None:
        raise RuntimeError(f"HDR import produced no loadable TextureCube: {source}")
    return texture, object_path, True


def build_map(asset_id: str, texture) -> tuple[str, bool]:
    map_name = safe_name(asset_id)
    package = f"{MAP_ROOT}/{map_name}"
    level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    actor_editor = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    existed = unreal.EditorAssetLibrary.does_asset_exist(package)
    opened = level_editor.load_level(package) if existed else level_editor.new_level(package)
    if not opened:
        raise RuntimeError(f"could not open or create HDRI Map: {package}")
    for actor in actor_editor.get_all_level_actors():
        if actor.get_actor_label().startswith(("Sky_HDRIBackdrop", "HDRI_SkyLight", "HDRI_KeyLight")):
            actor_editor.destroy_actor(actor)
    actor_class = unreal.EditorAssetLibrary.load_blueprint_class("/HDRIBackdrop/Blueprints/HDRIBackdrop")
    if actor_class is None:
        raise RuntimeError("HDRIBackdrop plugin Blueprint is unavailable")
    actor = actor_editor.spawn_actor_from_class(
        actor_class,
        unreal.Vector(0.0, 0.0, -45.0),
        unreal.Rotator(0.0, 0.0, 0.0),
        transient=False,
    )
    if actor is None:
        raise RuntimeError(f"could not spawn HDRIBackdrop for {asset_id}")
    actor.set_actor_label(f"Sky_HDRIBackdrop_{asset_id}")
    actor.set_actor_location(unreal.Vector(0.0, 0.0, -45.0), False, False)
    actor.set_actor_rotation(unreal.Rotator(0.0, 0.0, 0.0), False)
    actor.set_editor_property("cubemap", texture)
    actor.set_editor_property("intensity", 1.0)
    actor.set_editor_property("size", 15.0)
    sky = actor_editor.spawn_actor_from_class(unreal.SkyLight, unreal.Vector(0.0, 0.0, 300.0))
    if sky is None:
        raise RuntimeError(f"could not spawn HDRI SkyLight for {asset_id}")
    sky.set_actor_label(f"HDRI_SkyLight_{asset_id}")
    sky.set_actor_location(unreal.Vector(0.0, 0.0, 300.0), False, False)
    sky_component = sky.get_component_by_class(unreal.SkyLightComponent)
    sky_component.set_mobility(unreal.ComponentMobility.MOVABLE)
    sky_component.set_editor_property("source_type", unreal.SkyLightSourceType.SLS_SPECIFIED_CUBEMAP)
    sky_component.set_editor_property("cubemap", texture)
    sky_component.set_editor_property("intensity", 1.25)
    key = actor_editor.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0.0, 0.0, 300.0))
    if key is None:
        raise RuntimeError(f"could not spawn HDRI key light for {asset_id}")
    key.set_actor_label(f"HDRI_KeyLight_{asset_id}")
    key.set_actor_location(unreal.Vector(0.0, 0.0, 300.0), False, False)
    key.set_actor_rotation(unreal.Rotator(-35.0, -35.0, 0.0), False)
    key_component = key.get_component_by_class(unreal.DirectionalLightComponent)
    key_component.set_mobility(unreal.ComponentMobility.MOVABLE)
    key_component.set_editor_property("intensity", 1.5)
    if not level_editor.save_current_level():
        raise RuntimeError(f"could not save HDRI Map: {package}")
    return package, not existed


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows = manifest.get("assets") or []
    if SELECTED:
        rows = [row for row in rows if str(row.get("source_asset_id")) in SELECTED]
    if not rows:
        raise RuntimeError("no selected HDRIs were found in the pool manifest")
    unreal.EditorAssetLibrary.make_directory(TEXTURE_ROOT)
    unreal.EditorAssetLibrary.make_directory(MAP_ROOT)
    results = []
    for row in rows:
        asset_id = str(row["source_asset_id"])
        source = Path(row["local_path"]).expanduser().resolve()
        if not source.is_file():
            raise RuntimeError(f"HDRI source is missing: {source}")
        texture, texture_path, imported = import_hdr(source, asset_id)
        package, created = build_map(asset_id, texture)
        results.append(
            {
                "source_asset_id": asset_id,
                "source_uri": row["source_uri"],
                "texture_object_path": texture_path,
                "map_package": package,
                "texture_imported": imported,
                "map_created": created,
            }
        )
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    temporary = RESULT.with_suffix(RESULT.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema_version": "harness_ue_hdri_map_build_v1",
                "status": "pass",
                "asset_count": len(results),
                "assets": results,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(RESULT)
    print("HARNESS_HDRI_MAP_BUILD=" + json.dumps({"status": "pass", "asset_count": len(results)}))


main()
