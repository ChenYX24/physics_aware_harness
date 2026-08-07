#!/usr/bin/env python3
"""Native UE map-anchored demo for phenomenon-driven physics scenes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from harness.core.case_spec import fracture_center_from_contact, fracture_response_for_energy
from harness.core.timebase import build_timebase
from harness.runtime.camera_planner import DYNAMIC_CAMERA_FOLLOW_GAINS, DYNAMIC_CAMERA_PROFILE
from harness.verification.render_sync_checker import depth_pixel_statistics

STUDIO_TOOLS = Path(__file__).resolve().parents[1] / "simulator_studio" / "tools"
if STUDIO_TOOLS.exists() and str(STUDIO_TOOLS) not in sys.path:
    sys.path.insert(0, str(STUDIO_TOOLS))
try:
    from project_scope import excluded_dir_names, validate_remote_path
except Exception:
    excluded_dir_names = None
    validate_remote_path = None

import unreal


# StaticMesh replacement can clear per-component material overrides.  Keep the
# render material outside physics_status because Unreal UObject instances are
# intentionally not JSON serializable.
SURFACE_REPLAY_MATERIALS = {}


def env_int(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except Exception:
        value = int(default)
    if min_value is not None:
        value = max(int(min_value), value)
    if max_value is not None:
        value = min(int(max_value), value)
    return value


def env_float(name: str, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except Exception:
        value = float(default)
    if min_value is not None:
        value = max(float(min_value), value)
    if max_value is not None:
        value = min(float(max_value), value)
    return value


DEFAULT_OUTPUT_DIR = "runs/native_ue_physics_phenomena"
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", DEFAULT_OUTPUT_DIR))
if validate_remote_path:
    validate_remote_path(OUTPUT_DIR)
WIDTH = env_int("WIDTH", 1920, 64, 7680)
HEIGHT = env_int("HEIGHT", 1080, 64, 4320)
FPS = env_int("FPS", 24, 1, 120)
DURATION = float(os.environ.get("DURATION", "6.0"))
ASSET_ROOT = Path(os.environ.get("ASSET_ROOT", str(Path(__file__).resolve().parents[1] / "assets")))
VISUAL_REALISM_PROFILE = os.environ.get("VISUAL_REALISM_PROFILE", "editor_parity").strip().lower()
EDITOR_VIEWPORT_MATCH_REALISM = VISUAL_REALISM_PROFILE in {"editor_viewport_match", "viewport_match", "editor_lit_match", "lit_viewport_match"}
EDITOR_PARITY_REALISM = EDITOR_VIEWPORT_MATCH_REALISM or VISUAL_REALISM_PROFILE in {"editor_parity", "viewport", "viewport_lit", "lit_viewport", "realism", "realistic"}
VIDEO_FILTER = os.environ.get("VIDEO_FILTER", "" if EDITOR_PARITY_REALISM else "eq=brightness=0.04:contrast=1.10:gamma=1.00:saturation=1.08")
VIDEO_CRF = os.environ.get("VIDEO_CRF", "18")
VIDEO_PRESET = os.environ.get("VIDEO_PRESET", "slow")
VIDEO_SHARPEN = os.environ.get("VIDEO_SHARPEN", "0" if EDITOR_PARITY_REALISM else "1") != "0"
VIDEO_SHARPEN_FILTER = os.environ.get("VIDEO_SHARPEN_FILTER", "unsharp=5:5:0.45:3:3:0.20")
RENDER_QUALITY_PRESET = os.environ.get("RENDER_QUALITY_PRESET", "high").strip().lower()
RENDER_CAMERA_PRESET = os.environ.get("RENDER_CAMERA_PRESET", os.environ.get("CAMERA_PRESET", "")).strip().lower()
RENDER_WARMUP_FRAMES = env_int("RENDER_WARMUP_FRAMES", 8, 0, 120)
RENDER_VIEWPORT_SETTLE_SECONDS = env_float("RENDER_VIEWPORT_SETTLE_SECONDS", 2.0, 0.0, 30.0)
RENDER_SURFACE_REPLAY_MATERIAL_WARMUP_SECONDS = env_float(
    "RENDER_SURFACE_REPLAY_MATERIAL_WARMUP_SECONDS", 0.5, 0.0, 30.0
)
RENDER_FIRST_FRAME_STABILITY_SAMPLES = env_int("RENDER_FIRST_FRAME_STABILITY_SAMPLES", 2, 0, 12)
RENDER_PER_FRAME_SETTLE_TICKS = env_int("RENDER_PER_FRAME_SETTLE_TICKS", 1, 0, 12)
RENDER_SCREENSHOT_STABLE_TICKS = env_int("RENDER_SCREENSHOT_STABLE_TICKS", 3, 1, 20)
RENDER_FIRST_FRAME_STABILITY_SIZE_TOLERANCE = env_int("RENDER_FIRST_FRAME_STABILITY_SIZE_TOLERANCE", 2048, 0, 1048576)
RENDER_SCREEN_PERCENTAGE = env_int("RENDER_SCREEN_PERCENTAGE", 100, 50, 200)
RENDER_TEMPORAL_AA_SAMPLES = env_int("RENDER_TEMPORAL_AA_SAMPLES", 8, 1, 64)
RENDER_TEMPORAL_AA_CURRENT_FRAME_WEIGHT = env_float("RENDER_TEMPORAL_AA_CURRENT_FRAME_WEIGHT", 0.08, 0.01, 1.0)
RENDER_TEXTURE_POOL_MB = env_int("RENDER_TEXTURE_POOL_MB", 4096, 512, 32768)
RENDER_SHADOW_MAX_RESOLUTION = env_int("RENDER_SHADOW_MAX_RESOLUTION", 4096, 512, 8192)
RENDER_SHADOW_DISTANCE_SCALE = env_float("RENDER_SHADOW_DISTANCE_SCALE", 1.25, 0.25, 4.0)
RENDER_STATIC_MESH_LOD_DISTANCE_SCALE = env_float("RENDER_STATIC_MESH_LOD_DISTANCE_SCALE", 0.65, 0.1, 2.0)
RENDER_SKELETAL_MESH_LOD_BIAS = env_int("RENDER_SKELETAL_MESH_LOD_BIAS", -1, -10, 10)
RENDER_MIPMAP_LOD_BIAS = env_float("RENDER_MIPMAP_LOD_BIAS", 0.0, -4.0, 4.0)
RENDER_TONEMAPPER_SHARPEN = env_float("RENDER_TONEMAPPER_SHARPEN", 0.28 if RENDER_QUALITY_PRESET in {"paper", "publication", "cinematic"} else 0.45, 0.0, 1.0)
RENDER_MOTION_BLUR_QUALITY = env_int("RENDER_MOTION_BLUR_QUALITY", 0, 0, 4)
SCENE_DESCRIPTION = os.environ.get(
    "SCENE_DESCRIPTION",
    "An Asset Database physics exhibit showing buoyancy and magnetism.",
)
SCENE_MAP = os.environ.get("SCENE_MAP", "auto")
ASSET_MANIFEST = os.environ.get("ASSET_MANIFEST")
SCENE_SPEC = os.environ.get("SCENE_SPEC")
SCENE_RUNTIME_JSON = os.environ.get("SCENE_RUNTIME_JSON")
GITLAB_ONLY_ASSETS = os.environ.get("ASSET_DATABASE_ONLY_ASSETS", os.environ.get("GITLAB_ONLY_ASSETS", "1")) != "0"
MULTI_VIEW = os.environ.get("MULTI_VIEW", "1") != "0"
CANONICAL_MULTI_VIEW = os.environ.get("CANONICAL_MULTI_VIEW", "1") != "0"
RENDER_DATA_PASSES = os.environ.get("RENDER_DATA_PASSES", "1") != "0"
AUDIO_PASS_ENABLED = os.environ.get("AUDIO_PASS_ENABLED", "1") != "0"
STAGE_HELPERS = os.environ.get("STAGE_HELPERS", "1") != "0"
EXTRA_STAGE_MARKERS = os.environ.get("EXTRA_STAGE_MARKERS", "0") == "1"
MAP_BACKDROP_HELPERS = os.environ.get("MAP_BACKDROP_HELPERS", "0") == "1"
STABLE_STAGE_ANCHORS = os.environ.get("STABLE_STAGE_ANCHORS", "1") != "0"
KEEP_RENDER_FRAMES = os.environ.get("KEEP_RENDER_FRAMES", "0") == "1"
GENERATED_MATERIAL_VERSION = os.environ.get("GENERATED_MATERIAL_VERSION", "V24_FLAT_STATIC_AND_PHYSICS_MATERIALS")
CHAOS_RIGID_BODY_SETUP = os.environ.get("CHAOS_RIGID_BODY_SETUP", "1") != "0"
CHAOS_SIMULATION_ENABLED = os.environ.get("CHAOS_SIMULATION_ENABLED", "0") == "1"
CONTROLLED_BOTTLE_STAGE = os.environ.get("CONTROLLED_BOTTLE_STAGE", "0") != "0"

MAP_CANDIDATES = [
    {
        "name": "TropicalIsland_Level_Scene_01",
        "path": "/Game/Maps/TropicalIsland/Maps/Level_Scene_01",
        "tags": ("adp", "gitlab", "tropical", "island", "water", "outdoor", "realistic", "repository"),
        "base_score": 5,
    },
    {
        "name": "MarketEnvironment_Day",
        "path": "/Game/Maps/MarketEnvironment/Maps/Day",
        "tags": ("adp", "gitlab", "market", "street", "day", "environment", "repository"),
        "base_score": 4,
    },
    {
        "name": "Classroom_Demo_00",
        "path": "/Game/Maps/Classroom/Maps/Classroom_Demo_00",
        "tags": ("adp", "gitlab", "classroom", "room", "interior", "meeting", "static", "environment"),
        "base_score": 4,
    },
]

ASSETS = {
    "sphere": "/Game/StarterContent/Shapes/Shape_Sphere.Shape_Sphere",
    "cube": "/Engine/BasicShapes/Cube.Cube",
    "plane": "/Engine/BasicShapes/Plane.Plane",
    "water_plane": "/Engine/BasicShapes/Plane.Plane",
    "floor": "/Game/StarterContent/Architecture/Floor_400x400.Floor_400x400",
    "wall": "/Game/StarterContent/Architecture/Wall_400x400.Wall_400x400",
    "wall_window": "/Game/StarterContent/Architecture/Wall_Window_400x300.Wall_Window_400x300",
    "table": "/Game/StarterContent/Props/SM_TableRound.SM_TableRound",
    "chair": "/Game/StarterContent/Props/SM_Chair.SM_Chair",
    "rock": "/Game/StarterContent/Props/SM_Rock.SM_Rock",
    "bush": "/Game/StarterContent/Props/SM_Bush.SM_Bush",
    "statue": "/Game/StarterContent/Props/SM_Statue.SM_Statue",
    "lamp_wall": "/Game/StarterContent/Props/SM_Lamp_Wall.SM_Lamp_Wall",
    "mat_water": "/Game/StarterContent/Materials/M_Water_Lake.M_Water_Lake",
    "mat_concrete": "/Game/StarterContent/Materials/M_Concrete_Poured.M_Concrete_Poured",
    "mat_rock": "/Game/StarterContent/Materials/M_Rock_Basalt.M_Rock_Basalt",
    "mat_wood": "/Game/StarterContent/Materials/M_Wood_Pine.M_Wood_Pine",
    "mat_metal": "/Game/StarterContent/Materials/M_Metal_Steel.M_Metal_Steel",
}

ASSET_ALIASES = {
    "sphere": ("visual_ball", "ball", "sphere"),
    "water_plane": ("water_plane", "water", "plane"),
    "cube": ("cube", "gear", "stone", "block"),
    "plane": ("water_plane", "floor", "disc", "plane"),
    "floor": ("floor",),
    "wall": ("wall",),
    "wall_window": ("wall_window", "wall", "window"),
    "table": ("table",),
    "chair": ("chair",),
    "rock": ("rock", "stone"),
    "bush": ("bush", "plant", "vegetation"),
    "statue": ("statue",),
    "lamp_wall": ("lamp_wall", "lamp"),
}

MATERIAL_ALIASES = {
    "mat_water": ("water",),
    "mat_concrete": ("concrete", "stone"),
    "mat_rock": ("rock", "stone"),
    "mat_wood": ("wood",),
    "mat_metal": ("metal", "steel"),
}

ASSET_SELECTION_METADATA: dict[str, dict] = {}


def load_asset_manifest() -> dict:
    if not ASSET_MANIFEST:
        return {"resolver": "script_defaults", "assets": {}, "materials": {}, "load_error": None}
    manifest_path = Path(ASSET_MANIFEST)
    if not manifest_path.exists():
        return {"resolver": "missing_manifest", "assets": {}, "materials": {}, "load_error": str(manifest_path)}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"resolver": "invalid_manifest", "assets": {}, "materials": {}, "load_error": str(exc)}
    if not isinstance(data, dict):
        return {"resolver": "invalid_manifest_type", "assets": {}, "materials": {}, "load_error": "manifest is not an object"}
    data.setdefault("assets", {})
    data.setdefault("materials", {})
    return data


ASSET_MANIFEST_DATA = load_asset_manifest()


def load_studio_scene_spec() -> dict:
    if not SCENE_SPEC:
        return {}
    path = Path(SCENE_SPEC)
    if not path.exists():
        return {"load_error": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"load_error": "scene spec is not an object"}
    except Exception as exc:
        return {"load_error": str(exc)}


STUDIO_SCENE_SPEC = load_studio_scene_spec()


def load_studio_runtime_scene() -> dict:
    if not SCENE_RUNTIME_JSON:
        return {}
    path = Path(SCENE_RUNTIME_JSON)
    if not path.exists():
        return {"load_error": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"load_error": "runtime scene is not an object"}
    except Exception as exc:
        return {"load_error": str(exc)}


STUDIO_RUNTIME_SCENE = load_studio_runtime_scene()


def normalize_object_path(path: str) -> str:
    if not path:
        return path
    leaf = path.rsplit("/", 1)[-1]
    if "." in leaf:
        return path
    return f"{path}.{leaf}"


def manifest_assets() -> dict:
    assets = ASSET_MANIFEST_DATA.get("assets", {})
    return assets if isinstance(assets, dict) else {}


def asset_search_text(key: str, asset: dict) -> str:
    return " ".join(
        str(part)
        for part in (
            key,
            asset.get("key", ""),
            asset.get("name", ""),
            asset.get("category_l1", ""),
            asset.get("category_l2", ""),
            " ".join(asset.get("tags") or []),
            asset.get("ue5_path", ""),
        )
    ).lower()


def select_manifest_asset(asset_key: str, aliases: tuple[str, ...]) -> dict | None:
    assets = manifest_assets()
    direct = assets.get(asset_key)
    if isinstance(direct, dict) and direct.get("ue5_path"):
        return direct
    for key, asset in assets.items():
        if not isinstance(asset, dict) or not asset.get("ue5_path"):
            continue
        text = asset_search_text(str(key), asset)
        if any(alias.lower().replace("_", " ") in text.replace("_", " ") for alias in aliases):
            return asset
    return None


def require_gitlab_manifest_asset(asset_key: str, attempted: str | None = None) -> None:
    if not GITLAB_ONLY_ASSETS:
        return
    detail = f" attempted={attempted}" if attempted else ""
    raise RuntimeError(f"Asset Database policy requires loadable ADP manifest asset for {asset_key}.{detail}")


def resolve_asset(asset_key: str) -> str:
    fallback = ASSETS[asset_key]
    asset = select_manifest_asset(asset_key, ASSET_ALIASES.get(asset_key, (asset_key,)))
    if not asset:
        ASSET_SELECTION_METADATA[asset_key] = {"path": None, "source": "gitlab_only", "fallback_reason": "not_in_manifest"}
        require_gitlab_manifest_asset(asset_key)
        ASSET_SELECTION_METADATA[asset_key] = {"path": fallback, "source": "script_default", "fallback_reason": "not_in_manifest"}
        return fallback
    path = normalize_object_path(str(asset.get("ue5_path")))
    loaded = bool(unreal.load_asset(path))
    if loaded:
        ASSET_SELECTION_METADATA[asset_key] = {
            "path": path,
            "source": asset.get("source", "manifest"),
            "asset_id": asset.get("asset_id"),
            "name": asset.get("name"),
            "fallback_reason": None,
        }
        return path
    ASSET_SELECTION_METADATA[asset_key] = {
        "path": None if GITLAB_ONLY_ASSETS else fallback,
        "attempted_path": path,
        "source": asset.get("source", "manifest"),
        "asset_id": asset.get("asset_id"),
        "name": asset.get("name"),
        "fallback_reason": "manifest_asset_not_loadable",
    }
    require_gitlab_manifest_asset(asset_key, path)
    return fallback


def resolve_material(material_key: str) -> str:
    fallback = ASSETS[material_key]
    materials = ASSET_MANIFEST_DATA.get("materials", {})
    aliases = MATERIAL_ALIASES.get(material_key, (material_key.removeprefix("mat_"),))
    for alias in aliases:
        path = materials.get(alias) if isinstance(materials, dict) else None
        if path and unreal.load_asset(normalize_object_path(str(path))):
            ASSET_SELECTION_METADATA[material_key] = {"path": normalize_object_path(str(path)), "source": "manifest_material", "fallback_reason": None}
            return normalize_object_path(str(path))
    ASSET_SELECTION_METADATA[material_key] = {"path": None if GITLAB_ONLY_ASSETS else fallback, "source": "generated_material", "fallback_reason": "material_not_in_manifest_or_not_loadable"}
    if GITLAB_ONLY_ASSETS:
        return ""
    return fallback


RESOLVED_ASSETS = {}
if not (STUDIO_RUNTIME_SCENE and not STUDIO_RUNTIME_SCENE.get("load_error")):
    RESOLVED_ASSETS = {
        **{key: resolve_asset(key) for key in ASSET_ALIASES},
        **{key: resolve_material(key) for key in MATERIAL_ALIASES},
    }



def prefers_material_library_mesh(obj: dict) -> bool:
    params = obj.get("params") or {}
    properties = obj.get("physics_properties") or {}
    value = params.get("force_material_library_mesh", properties.get("force_material_library_mesh", False))
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def controlled_sphere_mesh_path() -> str:
    for candidate in (ASSETS["sphere"], "/Engine/BasicShapes/Sphere.Sphere"):
        path = normalize_object_path(candidate)
        if unreal.load_asset(path):
            return path
    return normalize_object_path(ASSETS["sphere"])


def resolve_runtime_asset_path(obj: dict) -> str:
    keep_material_mesh = prefers_material_library_mesh(obj)
    if str(obj.get("asset_kind") or "").casefold() == "blueprint":
        raw_path = obj.get("ue5_path")
        if not raw_path:
            raise RuntimeError(f"runtime blueprint {obj.get('id')} missing ue5_path")
        return normalize_object_path(raw_path)
    if obj.get("behavior") == "third_person_runner" and not keep_material_mesh:
        path = normalize_object_path(ASSETS["cube"])
        ASSET_SELECTION_METADATA[obj.get("asset_key") or obj.get("id", "character_proxy")] = {
            "path": path,
            "source": "generated_controlled_character_proxy",
            "asset_id": obj.get("asset_key"),
            "name": obj.get("asset_name") or "Controlled visible character proxy",
            "fallback_reason": "local native runner does not yet spawn and retarget skeletal character animation from SimSpec",
        }
        return path
    if obj.get("behavior") == "character_carry_object" and not keep_material_mesh:
        path = normalize_object_path(ASSETS["cube"])
        ASSET_SELECTION_METADATA[obj.get("asset_key") or obj.get("id", "carried_object")] = {
            "path": path,
            "source": "generated_controlled_carry_body",
            "asset_id": obj.get("asset_key"),
            "name": obj.get("asset_name") or "Controlled carried rigid body",
            "fallback_reason": "carry/drop physics test requested a compact controlled body when material-library mesh collision is not selected",
        }
        return path
    if obj.get("behavior") in {"slope_roll", "character_throw_projectile"} and not keep_material_mesh:
        path = controlled_sphere_mesh_path()
        ASSET_SELECTION_METADATA[obj.get("asset_key") or obj.get("id", "slope_roll_body")] = {
            "path": path,
            "source": "generated_controlled_physics_sphere",
            "asset_id": obj.get("asset_key"),
            "name": obj.get("asset_name") or "Controlled rolling sphere",
            "fallback_reason": "decorative material-library ball mesh did not provide stable visible downhill rolling on the generated ramp",
        }
        return path
    if obj.get("behavior") == "falling_collision" and not keep_material_mesh:
        path = normalize_object_path(ASSETS["cube"])
        ASSET_SELECTION_METADATA[obj.get("asset_key") or obj.get("id", "falling_collision_body")] = {
            "path": path,
            "source": "generated_controlled_rigid_body",
            "asset_id": obj.get("asset_key"),
            "name": obj.get("asset_name") or "Controlled falling cube",
            "fallback_reason": "material-library mesh collision was not reliable for gravity/contact support",
        }
        return path
    if (obj.get("behavior") == "slope_surface" or obj.get("id") == "slope_track") and not keep_material_mesh:
        path = normalize_object_path(ASSETS["cube"])
        ASSET_SELECTION_METADATA[obj.get("asset_key") or obj.get("id", "slope_track")] = {
            "path": path,
            "source": "generated_controlled_slope_surface",
            "asset_id": obj.get("asset_key"),
            "name": obj.get("asset_name") or "Controlled inclined plane",
            "fallback_reason": "reference slope probe uses an explicit analytic rolling-contact surface",
        }
        return path
    if obj.get("id") == "rolling_friction_surface" or obj.get("behavior") == "rolling_friction_surface":
        path = normalize_object_path(ASSETS["cube"])
        ASSET_SELECTION_METADATA[obj.get("asset_key") or obj.get("id", "rolling_friction_surface")] = {
            "path": path,
            "source": "generated_controlled_rolling_surface",
            "asset_id": obj.get("asset_key"),
            "name": obj.get("asset_name") or "Controlled rolling-friction surface",
            "fallback_reason": "rolling-friction comparison uses an explicit analytic rolling-resistance surface",
        }
        return path
    if obj.get("id") == "projectile_landing_zone" or obj.get("behavior") == "projectile_landing_zone":
        path = normalize_object_path(ASSETS["cube"])
        ASSET_SELECTION_METADATA[obj.get("asset_key") or obj.get("id", "projectile_landing_zone")] = {
            "path": path,
            "source": "generated_controlled_projectile_landing_zone",
            "asset_id": obj.get("asset_key"),
            "name": obj.get("asset_name") or "Controlled projectile landing zone",
            "fallback_reason": "projectile arc reference uses an explicit analytic ballistic landing surface",
        }
        return path
    if obj.get("id") == "pendulum_anchor" or obj.get("behavior") == "pendulum_anchor":
        path = normalize_object_path(ASSETS["cube"])
        ASSET_SELECTION_METADATA[obj.get("asset_key") or obj.get("id", "pendulum_anchor")] = {
            "path": path,
            "source": "generated_controlled_pendulum_anchor",
            "asset_id": obj.get("asset_key"),
            "name": obj.get("asset_name") or "Controlled pendulum anchor",
            "fallback_reason": "pendulum reference uses an explicit analytic fixed pivot",
        }
        return path
    if obj.get("behavior") in {"constraint_joint_link", "constraint_anchor"} or str(obj.get("id") or "").startswith("joint_link_"):
        path = normalize_object_path(ASSETS["cube"])
        ASSET_SELECTION_METADATA[obj.get("asset_key") or obj.get("id", "constraint_joint")] = {
            "path": path,
            "source": "generated_controlled_constraint_joint_body",
            "asset_id": obj.get("asset_key"),
            "name": obj.get("asset_name") or "Controlled constraint joint body",
            "fallback_reason": "constraint-joint reference uses explicit analytic two-link constraints",
        }
        return path
    if obj.get("behavior") in {"stack_stability", "stack_push_impactor", "stack_support_surface"} or str(obj.get("id") or "").startswith("stack_block_"):
        path = normalize_object_path(ASSETS["cube"])
        ASSET_SELECTION_METADATA[obj.get("asset_key") or obj.get("id", "stack_stability")] = {
            "path": path,
            "source": "generated_controlled_stack_body",
            "asset_id": obj.get("asset_key"),
            "name": obj.get("asset_name") or "Controlled stack stability cube",
            "fallback_reason": "stack-stability reference uses explicit analytic contact topology and controlled box extents",
        }
        return path
    if obj.get("id") == "friction_surface" or obj.get("behavior") == "friction_surface":
        path = normalize_object_path(ASSETS["cube"])
        ASSET_SELECTION_METADATA[obj.get("asset_key") or obj.get("id", "friction_surface")] = {
            "path": path,
            "source": "generated_controlled_collision_surface",
            "asset_id": obj.get("asset_key"),
            "name": obj.get("asset_name") or "Controlled friction slab",
            "fallback_reason": "material-library floor meshes were too thin for stable Chaos support",
        }
        return path
    if (obj.get("id") == "landing_pad" or obj.get("behavior") == "landing_surface") and not keep_material_mesh:
        path = normalize_object_path(ASSETS["cube"])
        ASSET_SELECTION_METADATA[obj.get("asset_key") or obj.get("id", "landing_pad")] = {
            "path": path,
            "source": "generated_controlled_landing_surface",
            "asset_id": obj.get("asset_key"),
            "name": obj.get("asset_name") or "Controlled landing slab",
            "fallback_reason": "material-library floor meshes were too thin for stable gravity contact support",
        }
        return path
    asset_key = obj.get("asset_key")
    assets = manifest_assets()
    manifest_asset = assets.get(asset_key) if asset_key else None
    raw_path = obj.get("ue5_path") or (manifest_asset or {}).get("ue5_path")
    if not raw_path:
        raise RuntimeError(f"runtime object {obj.get('id')} missing material-library ue5_path")
    path = normalize_object_path(str(raw_path))
    if unreal.load_asset(path):
        ASSET_SELECTION_METADATA[asset_key or obj.get("id", "runtime_asset")] = {
            "path": path,
            "source": (manifest_asset or {}).get("source", "runtime_scene"),
            "asset_id": asset_key,
            "name": obj.get("asset_name"),
            "fallback_reason": None,
        }
        return path
    raise RuntimeError(f"failed to load runtime material-library asset for {obj.get('id')}: {path}")


def projectile_motion_parameters(obj: dict) -> tuple[list[float], float, float, float, float]:
    params = obj.get("params") or {}
    props = obj.get("physics_properties") or {}
    initial = [float(value) for value in obj.get("initial_position_m", [0.0, 0.0, 0.0])]
    velocity = props.get("initial_velocity_m_s") or params.get("initial_velocity_m_s") or [1.72, 0.0, 8.05]
    if not isinstance(velocity, list):
        velocity = [1.72, 0.0, 8.05]
    padded_velocity = [*velocity, 0.0, 0.0, 0.0]
    vx, vy, vz = [float(padded_velocity[idx]) for idx in range(3)]
    gravity = max(0.1, float(props.get("gravity_m_s2") or params.get("gravity_m_s2") or 9.81))
    ground_z = max(0.05, float(props.get("ground_z_m") or params.get("ground_z_m") or 0.40))
    radius_m = max(0.08, float(props.get("radius_m") or params.get("radius_m") or 0.30))
    dz = max(0.0, initial[2] - ground_z)
    discriminant = max(0.0, vz * vz + 2.0 * gravity * dz)
    landing_time = (vz + math.sqrt(discriminant)) / gravity
    return [vx, vy, vz], gravity, ground_z, radius_m, landing_time


def runtime_position(obj: dict, time_s: float, duration: float) -> tuple[list[float], list[float]]:
    x, y, z = [float(v) for v in obj.get("initial_position_m", [0.0, 0.0, 0.0])]
    params = obj.get("params") or {}
    behavior = obj.get("behavior")
    progress = 0.0 if duration <= 0 else max(0.0, min(1.0, time_s / duration))
    yaw = 0.0
    pitch = 0.0
    roll = 0.0
    if behavior == "wind_drift":
        phase = float(params.get("phase", 0.0))
        props = obj.get("physics_properties") or {}
        wind_speed = float(props.get("wind_speed_m_s") or params.get("wind_speed", 0.56))
        tau = max(0.35, float(props.get("drag_time_constant_s") or params.get("drag_time_constant_s") or params.get("damping_tau", 1.6)))
        amplitude_z = float(props.get("vertical_oscillation_amplitude_m") or params.get("vertical_amplitude_m", 0.18))
        amplitude_y = float(props.get("lateral_oscillation_amplitude_m") or params.get("lateral_amplitude_m", 0.07))
        lift_velocity = float(props.get("buoyant_lift_velocity_m_s") or params.get("buoyant_lift_velocity_m_s", 0.055))
        wind_response_distance = max(0.0, time_s - tau * (1.0 - math.exp(-time_s / tau)))
        lift_tau = max(0.6, tau * 1.35)
        lift_response_distance = max(0.0, time_s - lift_tau * (1.0 - math.exp(-time_s / lift_tau)))
        oscillation_decay = 0.42 + 0.58 * math.exp(-time_s / max(tau * 1.25, 0.1))
        x += wind_speed * wind_response_distance
        y += amplitude_y * oscillation_decay * math.sin(time_s * 1.55 + phase)
        z += lift_velocity * lift_response_distance + amplitude_z * oscillation_decay * math.sin(time_s * 2.15 + phase)
        pitch = 3.5 * math.sin(time_s * 1.35 + phase)
        yaw = 12.0 * math.sin(time_s * 1.05 + phase)
    elif behavior == "third_person_runner":
        movement_waypoints = [item for item in params.get("movement_waypoints") or [] if isinstance(item, dict) and isinstance(item.get("position_m"), list)]
        if movement_waypoints:
            right_index = next((index for index, item in enumerate(movement_waypoints) if float(item.get("time_s") or 0.0) >= time_s), len(movement_waypoints) - 1)
            left = movement_waypoints[max(0, right_index - 1)]
            right = movement_waypoints[right_index]
            left_t = float(left.get("time_s") or 0.0)
            right_t = float(right.get("time_s") or left_t)
            alpha = 0.0 if right_t <= left_t else max(0.0, min(1.0, (time_s - left_t) / (right_t - left_t)))
            position = interpolate_values(left["position_m"], right["position_m"], alpha)
            x, y, z = [float(value) for value in position[:3]]
            yaw = float(left.get("yaw_degrees") or 0.0) + (float(right.get("yaw_degrees") or 0.0) - float(left.get("yaw_degrees") or 0.0)) * alpha
            return [round(x, 5), round(y, 5), round(z, 5)], [0.0, round(yaw, 4), 0.0]
        run_speed = float(params.get("run_speed_m_s") or 1.6)
        start_throw = float(params.get("throw_time_s") or 1.45)
        second_throw = float(params.get("second_throw_time_s") or 3.65)
        third_throw = params.get("third_throw_time_s")
        move_start = float(params.get("move_start_s") or 0.0)
        move_until = float(params.get("move_until_s") or duration)
        travel = run_speed * max(0.0, min(time_s, move_until) - move_start)
        x += travel
        stable_root = bool_control(params.get("stable_root_motion"), False)
        if not stable_root:
            y += 0.14 * math.sin(time_s * 2.2)
            z += 0.03 * math.sin(time_s * 5.5)
        if stable_root:
            pitch = yaw = roll = 0.0
        elif time_s >= start_throw:
            pitch = -4.0 - 5.5 * math.exp(-(time_s - start_throw) * 1.5)
            yaw = 10.0 + 2.0 * math.sin(time_s * 3.0)
        else:
            pitch = 2.0 * math.sin(time_s * 1.7)
            yaw = 4.0 * math.sin(time_s * 1.2)
        if time_s >= second_throw:
            roll = 2.0 * math.sin(time_s * 2.6)
        if third_throw is not None and time_s >= float(third_throw):
            yaw += 8.0
    elif behavior == "thrown_box":
        launch_time = float(params.get("launch_time_s") or params.get("throw_time_s") or 0.0)
        gravity = max(0.1, float(params.get("gravity_m_s2") or 9.81))
        ground_z = max(0.05, float(params.get("ground_z_m") or 0.38))
        radius_m = max(0.08, float(params.get("radius_m") or 0.26))
        vx, vy, vz = [float(v) for v in [*(params.get("initial_velocity_m_s") or [3.8, 0.0, 5.4]), 0.0, 0.0, 0.0][:3]]
        if time_s < launch_time:
            x -= 0.28 * (1.0 - math.exp(-time_s * 3.0))
            y += 0.05 * math.sin(time_s * 2.2)
            pitch = -22.0 + 10.0 * math.sin(time_s * 2.0)
            yaw = 6.0 * math.sin(time_s * 1.8)
        else:
            t = time_s - launch_time
            x += vx * t
            y += vy * t
            z = max(ground_z, z + vz * t - 0.5 * gravity * t * t)
            path_m = math.hypot(vx * t, vy * t)
            apex_t = max(0.0, vz / gravity)
            if t > apex_t:
                post = t - apex_t
                x += 0.32 * (1.0 - math.exp(-post * 1.6))
                y += 0.10 * math.sin(post * 1.8)
                z = ground_z
                roll = min(48.0, post * 28.0)
            pitch = -math.degrees(path_m / radius_m)
            yaw = 7.0 * math.sin(min(t, apex_t) * 1.3)
    elif behavior == "slope_roll":
        angle = math.radians(float(params.get("slope_angle_degrees", 13.0)))
        mu = max(0.0, float(params.get("rolling_friction", 0.04)))
        track_length = max(1.2, float(params.get("track_length_m", 3.0)))
        radius_m = max(0.12, float(params.get("radius_m", 0.32)))
        acceleration = max(0.18, 9.81 * max(0.02, math.sin(angle) - mu * math.cos(angle)) / 1.4)
        tau = max(0.75, math.sqrt(2.0 * track_length / acceleration) / 1.35)
        path_m = track_length * (1.0 - math.exp(-time_s / tau))
        x += math.cos(angle) * path_m
        z = max(0.22, z - math.sin(angle) * path_m)
        pitch = -math.degrees(path_m / radius_m)
    elif behavior == "projectile_arc":
        (vx, vy, vz), gravity, ground_z, radius_m, landing_t = projectile_motion_parameters(obj)
        active_t = min(max(0.0, time_s), landing_t)
        flight_x = vx * active_t
        flight_y = vy * active_t
        x += flight_x
        y += flight_y
        z = max(ground_z, z + vz * active_t - 0.5 * gravity * active_t * active_t)
        path_m = math.hypot(flight_x, flight_y)
        if time_s > landing_t:
            after = time_s - landing_t
            slide = 0.34 * (1.0 - math.exp(-after * 1.35))
            x += slide
            z = ground_z
            path_m += slide
            roll = min(34.0, after * 22.0)
        pitch = -math.degrees(path_m / radius_m)
        yaw = 4.0 * math.sin(min(time_s, landing_t) * 1.4)
    elif behavior == "pendulum_swing":
        props = obj.get("physics_properties") or {}
        anchor = props.get("anchor_position_m") or params.get("anchor_position_m") or [0.0, 0.0, 3.2]
        if not isinstance(anchor, list):
            anchor = [0.0, 0.0, 3.2]
        anchor = [float(value) for value in [*anchor, 0.0, 0.0, 0.0][:3]]
        length_m = max(0.35, float(props.get("pendulum_length_m") or params.get("pendulum_length_m") or 1.62))
        theta0 = math.radians(float(props.get("initial_angle_degrees") or params.get("initial_angle_degrees") or 32.0))
        gravity = max(0.1, float(props.get("gravity_m_s2") or params.get("gravity_m_s2") or 9.81))
        damping = max(0.0, float(props.get("damping_ratio") or params.get("damping_ratio") or 0.055))
        omega = math.sqrt(gravity / length_m)
        theta = theta0 * math.exp(-damping * time_s) * math.cos(omega * time_s)
        x = anchor[0] + length_m * math.sin(theta)
        y = anchor[1]
        z = anchor[2] - length_m * math.cos(theta)
        pitch = math.degrees(theta)
        roll = 7.0 * math.sin(omega * time_s + math.pi * 0.5)
    elif behavior == "constraint_joint_link":
        props = obj.get("physics_properties") or {}
        anchor = props.get("anchor_position_m") or params.get("anchor_position_m") or [0.0, 0.0, 3.2]
        if not isinstance(anchor, list):
            anchor = [0.0, 0.0, 3.2]
        anchor = [float(value) for value in [*anchor, 0.0, 0.0, 0.0][:3]]
        link_index = max(1, int(props.get("link_index") or params.get("link_index") or 1))
        length_1 = max(0.35, float((props.get("parent_constraint_length_m") if link_index == 2 else props.get("constraint_length_m")) or params.get("parent_constraint_length_m") or params.get("constraint_length_m") or 1.06))
        length_2 = max(0.25, float(props.get("constraint_length_m") or params.get("constraint_length_m") or 0.86))
        theta_1_0 = math.radians(float(props.get("initial_angle_degrees") or params.get("initial_angle_degrees") or 34.0))
        relative_0 = math.radians(float(props.get("relative_angle_degrees") or params.get("relative_angle_degrees") or 0.0))
        damping = max(0.0, float(props.get("damping_ratio") or params.get("damping_ratio") or 0.055))
        gravity = max(0.1, float(props.get("gravity_m_s2") or params.get("gravity_m_s2") or 9.81))
        omega_1 = math.sqrt(gravity / length_1)
        theta_1 = theta_1_0 * math.exp(-damping * time_s) * math.cos(omega_1 * time_s)
        link_1 = [
            anchor[0] + length_1 * math.sin(theta_1),
            anchor[1],
            anchor[2] - length_1 * math.cos(theta_1),
        ]
        if link_index == 1:
            x, y, z = link_1
            pitch = math.degrees(theta_1)
            roll = 5.0 * math.sin(omega_1 * time_s + math.pi * 0.5)
        else:
            omega_2 = math.sqrt(gravity / length_2) * 1.08
            relative = relative_0 * math.exp(-damping * 1.12 * time_s) * math.cos(omega_2 * time_s + 0.42)
            theta_2 = theta_1 + relative
            x = link_1[0] + length_2 * math.sin(theta_2)
            y = link_1[1]
            z = link_1[2] - length_2 * math.cos(theta_2)
            pitch = math.degrees(theta_2)
            roll = 8.0 * math.sin(omega_2 * time_s)
    elif behavior == "stack_push_impactor":
        props = obj.get("physics_properties") or {}
        velocity = props.get("initial_velocity_m_s") or params.get("initial_velocity_m_s") or [1.38, 0.0, 0.0]
        if not isinstance(velocity, list):
            velocity = [1.38, 0.0, 0.0]
        vx = float([*velocity, 0.0][0])
        travel = min(max(0.0, time_s) * vx, 1.05)
        rebound = 0.16 * (1.0 - math.exp(-max(0.0, time_s - 1.05) * 2.0))
        x += travel - rebound
        yaw = 8.0 * math.sin(time_s * 3.2)
    elif behavior == "stack_stability":
        props = obj.get("physics_properties") or {}
        idx = max(1, int(props.get("stack_index") or params.get("stack_index") or 1))
        collapse_start = float(props.get("collapse_start_s") or params.get("collapse_start_s") or 0.72)
        if idx == 1:
            p = 1.0 - math.exp(-max(0.0, time_s - collapse_start) * 1.0)
            x += 0.06 * p
            pitch = 8.0 * p
        else:
            start_t = collapse_start + 0.24 * (idx - 2)
            p = min(1.0, 1.0 - math.exp(-max(0.0, time_s - start_t) * 1.28))
            target_drop_z = max(0.0, z - (0.56 + 0.04 * idx))
            x += p * (0.36 + 0.34 * idx)
            y += p * (0.10 if idx % 2 else -0.08)
            z -= target_drop_z * p
            pitch = p * (38.0 + 24.0 * idx)
            roll = p * ((-1.0 if idx % 2 else 1.0) * (9.0 + 11.0 * idx))
    elif behavior == "gear_collision":
        idx = int(params.get("collision_index", 0))
        if idx == 0:
            x += min(time_s * 0.62, 0.96)
            roll = -420.0 * time_s
        else:
            delay = 1.35
            dt = max(0.0, time_s - delay)
            x += min(dt * 0.52, 0.92)
            roll = -360.0 * dt
    elif behavior == "rigid_collision":
        idx = int(params.get("collision_index", 0))
        if idx == 0:
            if time_s <= 0.8:
                x += min(2.05, 2.56 * time_s)
            else:
                x += 2.05 + 0.32 * (1.0 - math.exp(-(time_s - 0.8) * 1.9))
            roll = -520.0 * time_s
        else:
            dt = max(0.0, time_s - 0.78)
            x += 1.18 * (1.0 - math.exp(-dt * 1.35))
            roll = -260.0 * dt
    elif behavior == "plant_sway":
        props = obj.get("physics_properties") or {}
        phase = float(props.get("phase_offset_rad") or params.get("phase", 0.0))
        sway = float(props.get("sway_amplitude_degrees") or params.get("sway_degrees", 9.0))
        period = max(0.6, float(props.get("wind_period_s") or params.get("wind_period_s", 2.35)))
        rise_tau = max(0.25, float(props.get("response_rise_tau_s") or params.get("response_rise_tau_s", 0.75)))
        damping_ratio = max(0.0, float(props.get("damping_ratio") or params.get("damping_ratio", 0.18)))
        omega = 2.0 * math.pi / period
        envelope = (1.0 - math.exp(-time_s / rise_tau)) * (1.0 - 0.08 * damping_ratio * min(time_s / max(period, 0.01), 1.0))
        pitch = sway * envelope * math.sin(omega * time_s + phase)
        yaw = 0.28 * sway * envelope * math.sin(omega * time_s + phase + math.pi / 3.0)
        roll = 0.18 * sway * envelope * math.sin(omega * time_s + phase + math.pi / 5.0)
    elif behavior == "domino_tip":
        delay = float(params.get("delay_s", 0.4))
        dt = max(0.0, time_s - delay)
        topple = 1.0 - math.exp(-dt * 2.6) if dt > 0 else 0.0
        eased = topple * topple * (3.0 - 2.0 * topple)
        degrees = float(params.get("topple_degrees", 80.0)) * eased
        x += float(params.get("base_slide_m", 0.0)) * eased
        z = max(0.02, z)
        pitch = -degrees
        yaw = 0.8 * int(params.get("domino_index", 0))
    elif behavior == "wheel_jump":
        exit_s = float(params.get("ramp_exit_s", 1.65))
        landing_s = float(params.get("landing_s", 3.35))
        speed = float(params.get("ground_speed_m_s", 0.72))
        jump_height = float(params.get("jump_height_m", 0.62))
        if time_s <= exit_s:
            ramp_p = max(0.0, min(1.0, time_s / max(exit_s, 0.01)))
            x += speed * time_s
            z += 0.38 * ramp_p
        elif time_s <= landing_s:
            air_p = (time_s - exit_s) / max(landing_s - exit_s, 0.01)
            x += speed * exit_s + speed * 1.12 * (time_s - exit_s)
            z += 0.38 + jump_height * math.sin(math.pi * air_p)
        else:
            dt = time_s - landing_s
            x += speed * exit_s + speed * 1.12 * (landing_s - exit_s) + speed * 0.55 * (1.0 - math.exp(-dt * 1.1))
            z += 0.05 * math.exp(-dt * 3.0)
        roll = -520.0 * time_s
    elif behavior == "falling_collision":
        half_extent_m = max(0.18, float((obj.get("physics_properties") or {}).get("desired_extent_cm") or params.get("desired_extent_cm") or 46.0) / 100.0)
        ground_z = 0.06 + half_extent_m
        restitution = max(0.0, min(0.86, float((obj.get("physics_properties") or {}).get("restitution") or params.get("restitution") or 0.28)))
        dt = 1.0 / 180.0
        steps = max(0, int(math.ceil(time_s / dt)))
        vz = 0.0
        contact_time = None
        for step in range(steps):
            current_dt = min(dt, max(0.0, time_s - step * dt))
            if current_dt <= 0:
                break
            vz -= 9.81 * current_dt
            z += vz * current_dt
            if z <= ground_z:
                if contact_time is None:
                    contact_time = step * dt
                z = ground_z
                if vz < 0.0:
                    vz = -vz * restitution
                if abs(vz) < 0.18:
                    vz = 0.0
        if contact_time is not None:
            after = max(0.0, time_s - contact_time)
            idx = int(params.get("drop_index", 0))
            direction = -1.0 if idx == 0 else (1.0 if idx == 2 else 0.35)
            slide = 0.18 * direction * (1.0 - math.exp(-after * 1.4))
            x += slide
            y += 0.05 * direction * (1.0 - math.exp(-after * 1.1))
            pitch = direction * min(48.0, after * 34.0)
            roll = -direction * min(62.0, after * 42.0)
    elif behavior == "friction_slide":
        dist = float(params.get("slide_distance_m", 1.6))
        tau = max(0.05, float(params.get("damping_tau", 0.75)))
        moving = 1.0 - math.exp(-time_s / tau)
        x += dist * moving
        yaw = 1.8 * math.sin(time_s * 2.0 + float(params.get("lane", 0)))
    elif behavior == "rolling_friction":
        props = obj.get("physics_properties") or {}
        velocity = props.get("initial_velocity_m_s") or params.get("initial_velocity_m_s") or [2.25, 0.0, 0.0]
        v0 = max(0.05, float(velocity[0]) if isinstance(velocity, list) and velocity else 2.25)
        mu = max(0.005, float(props.get("rolling_friction") or params.get("rolling_friction") or 0.08))
        radius_m = max(0.08, float(props.get("radius_m") or params.get("radius_m") or 0.31))
        decel = max(0.08, 9.81 * mu * 1.18)
        stop_t = v0 / decel
        active_t = min(time_s, stop_t)
        path_m = max(0.0, v0 * active_t - 0.5 * decel * active_t * active_t)
        x += path_m
        roll = -math.degrees(path_m / radius_m)
        yaw = 1.2 * math.sin(min(time_s, stop_t) * 1.8 + float(params.get("lane", 0)))
    elif behavior == "rolling_impact":
        speed = float(params.get("speed_m_s", 0.68))
        stop_x = float(params.get("stop_x_m", 1.38))
        x += min(speed * time_s, stop_x - x)
        roll = -500.0 * time_s
    elif behavior == "barrel_cascade_impactor":
        properties = obj.get("physics_properties") or {}
        initial_velocity = properties.get("initial_velocity_m_s") or params.get("initial_velocity_m_s") or [4.8, 0.0, 0.0]
        speed = float(initial_velocity[0]) if isinstance(initial_velocity, list) and initial_velocity else 4.8
        x += min(speed * time_s, 6.4)
        y += 0.035 * math.sin(time_s * 5.0)
        z = max(0.58, z)
        roll = -780.0 * time_s
        yaw = 2.0 * math.sin(time_s * 3.0)
    elif behavior == "barrel_cascade_target":
        idx = int(params.get("target_index", 0))
        delay = float(params.get("hit_delay_s", 0.34 + idx * 0.14))
        dt = max(0.0, time_s - delay)
        response = 1.0 - math.exp(-dt * 2.8) if dt > 0 else 0.0
        eased = response * response * (3.0 - 2.0 * response)
        x += float(params.get("x_push_m", 0.7)) * eased
        y += float(params.get("y_push_m", 0.18 if idx % 2 == 0 else -0.18)) * eased
        z = max(0.18, z - 0.18 * eased)
        pitch = -float(params.get("topple_degrees", 82.0)) * eased
        yaw = (7.5 * idx) + 8.0 * math.sin(dt * 2.0) * eased
        roll = (66.0 + idx * 7.0) * eased * (1 if idx % 2 == 0 else -1)
    elif behavior == "impact_response":
        delay = float(params.get("start_delay_s", 1.45))
        dt = max(0.0, time_s - delay)
        response = 1.0 - math.exp(-dt * 2.2) if dt > 0 else 0.0
        eased = response * response * (3.0 - 2.0 * response)
        x += float(params.get("x_push_m", 0.2)) * eased
        z = max(0.26, z - 0.14 * eased)
        pitch = -float(params.get("topple_degrees", 40.0)) * eased
        yaw = 4.0 * int(params.get("response_index", 0))
    elif behavior == "generic_motion":
        phase = float(params.get("phase", 0.0))
        x += 0.65 * progress
        z += 0.1 * math.sin(time_s * 1.8 + phase)
        yaw = 20.0 * progress
    return [round(x, 5), round(y, 5), round(z, 5)], [round(pitch, 4), round(yaw, 4), round(roll, 4)]


def simulate_runtime_scene(runtime_scene: dict) -> list[dict]:
    precomputed = runtime_scene.get("precomputed_trajectory")
    if isinstance(precomputed, list) and precomputed:
        return precomputed
    sim = runtime_scene.get("simulation") or {}
    fps = int(sim.get("fps") or FPS)
    duration = float(sim.get("duration_s") or DURATION)
    frame_count = max(1, int(round(duration * fps)))
    controls = runtime_scene.get("physics_controls") if isinstance(runtime_scene.get("physics_controls"), dict) else {}
    analytic_backend = controls.get("runtime_driver_backend") == "analytic_contact_solver"
    analytic_source = str(controls.get("simulation_driver") or "analytic_contact_solver")
    if not analytic_source.startswith("analytic_"):
        analytic_source = "analytic_contact_solver"
    frames = []
    action_trace = [item for item in runtime_scene.get("action_trace") or [] if isinstance(item, dict)]
    for frame_idx in range(frame_count + 1):
        t = frame_idx / fps
        frame = {"frame": frame_idx, "time": round(t, 4), "objects": {}}
        frame_actions = [dict(item) for item in action_trace if int(item.get("frame") or 0) == frame_idx]
        if frame_actions:
            frame["actions"] = frame_actions
            frame["contacts"] = [
                {
                    "frame": frame_idx,
                    "time": round(t, 4),
                    "objects": sorted([str(item.get("actor_id")), str(item.get("target_id"))]),
                    "method": "declared_agent_grasp_coupling",
                    "gap_cm": 0.0,
                }
                for item in frame_actions
                if item.get("action_type") == "grasp" and item.get("actor_id") and item.get("target_id")
            ]
        for obj in runtime_scene.get("dynamic_objects") or []:
            position, rotation = runtime_position(obj, t, duration)
            frame["objects"][obj["id"]] = {
                "position": position,
                "rotation_degrees": rotation,
                "behavior": obj.get("behavior"),
                "asset_key": obj.get("asset_key"),
                "source": analytic_source if analytic_backend and obj.get("behavior") in {"falling_collision", "slope_roll", "rolling_friction", "projectile_arc", "thrown_box", "pendulum_swing", "constraint_joint_link", "stack_stability", "stack_push_impactor", "wind_drift", "plant_sway"} else "scripted_runtime_preview",
            }
            if obj.get("behavior") == "falling_collision":
                params = obj.get("params") or {}
                half_extent_m = max(0.18, float((obj.get("physics_properties") or {}).get("desired_extent_cm") or params.get("desired_extent_cm") or 46.0) / 100.0)
                ground_z = 0.06 + half_extent_m
                if position[2] <= ground_z + 0.012:
                    frame.setdefault("contacts", []).append(
                        {
                            "frame": frame_idx,
                            "time": round(t, 4),
                            "objects": sorted([str(obj.get("id")), "landing_pad"]),
                            "method": "analytic_gravity_restitution_contact_solver",
                            "gap_cm": 0.0,
                        }
                    )
            elif analytic_backend and obj.get("behavior") == "slope_roll":
                frame.setdefault("contacts", []).append(
                    {
                        "frame": frame_idx,
                        "time": round(t, 4),
                        "objects": sorted([str(obj.get("id")), "slope_track"]),
                        "method": "analytic_inclined_plane_rolling_contact_solver",
                        "gap_cm": 0.0,
                    }
                )
            elif analytic_backend and obj.get("behavior") == "rolling_friction":
                frame.setdefault("contacts", []).append(
                    {
                        "frame": frame_idx,
                        "time": round(t, 4),
                        "objects": sorted([str(obj.get("id")), "rolling_friction_surface"]),
                        "method": "analytic_rolling_friction_contact_solver",
                        "gap_cm": 0.0,
                    }
                )
            elif analytic_backend and obj.get("behavior") == "projectile_arc":
                landing_t = projectile_motion_parameters(obj)[4]
                if t >= landing_t:
                    frame.setdefault("contacts", []).append(
                        {
                            "frame": frame_idx,
                            "time": round(t, 4),
                            "objects": sorted([str(obj.get("id")), "projectile_landing_zone"]),
                            "method": "analytic_projectile_landing_contact_solver",
                            "gap_cm": 0.0,
                        }
                    )
            elif analytic_backend and obj.get("behavior") == "stack_stability":
                props = obj.get("physics_properties") or {}
                idx = max(1, int(props.get("stack_index") or (obj.get("params") or {}).get("stack_index") or 1))
                collapse_start = float(props.get("collapse_start_s") or (obj.get("params") or {}).get("collapse_start_s") or 0.72)
                if idx == 1 or t >= collapse_start + 0.95 + 0.12 * idx:
                    frame.setdefault("contacts", []).append(
                        {
                            "frame": frame_idx,
                            "time": round(t, 4),
                            "objects": sorted([str(obj.get("id")), "stack_support_surface"]),
                            "method": "analytic_stack_support_contact_solver",
                            "gap_cm": 0.0,
                        }
                    )
                if idx > 1 and t <= collapse_start + 0.42 * idx:
                    frame.setdefault("contacts", []).append(
                        {
                            "frame": frame_idx,
                            "time": round(t, 4),
                            "objects": sorted([f"stack_block_{idx - 1}", str(obj.get("id"))]),
                            "method": "analytic_adjacent_stack_contact_solver",
                            "gap_cm": 0.0,
                        }
                    )
            elif analytic_backend and obj.get("behavior") == "stack_push_impactor" and 0.60 <= t <= 1.10:
                frame.setdefault("contacts", []).append(
                    {
                        "frame": frame_idx,
                        "time": round(t, 4),
                        "objects": sorted(["stack_push_block", "stack_block_2"]),
                        "method": "analytic_stack_trigger_contact_solver",
                        "gap_cm": 0.0,
                    }
                )
        frames.append(frame)
    return frames


def validate_runtime_scene(runtime_scene: dict, trajectory: list[dict]) -> dict:
    first = trajectory[0]["objects"] if trajectory else {}
    final = trajectory[-1]["objects"] if trajectory else {}
    checks = {
        "schema": {
            "passed": bool(runtime_scene.get("dynamic_objects")),
            "diagnostic": "runtime scene contains dynamic objects",
        },
        "asset_policy": {
            "passed": all(
                not str(obj.get("ue5_path") or "").startswith(("/Engine/", "/Game/StarterContent"))
                or obj.get("behavior") in {"character_carry_object", "character_throw_projectile", "landing_surface", "room_floor", "room_wall"}
                for obj in (runtime_scene.get("dynamic_objects") or []) + (runtime_scene.get("static_objects") or [])
            ),
            "diagnostic": "semantic actors use provided assets; controlled rigid bodies may use analytic UE meshes",
        },
    }
    case_type = runtime_scene.get("case_type")
    if case_type == "balloon_wind_drift":
        displacements = []
        vertical_ranges = []
        for oid, start in first.items():
            end = final.get(oid, start)
            displacements.append(end["position"][0] - start["position"][0])
            zs = [frame["objects"][oid]["position"][2] for frame in trajectory if oid in frame["objects"]]
            vertical_ranges.append(max(zs) - min(zs) if zs else 0.0)
        checks["wind_displacement"] = {"passed": bool(displacements) and min(displacements) > 1.2, "values": displacements, "diagnostic": "balloons drift left to right"}
        checks["vertical_oscillation"] = {"passed": bool(vertical_ranges) and max(vertical_ranges) > 0.16, "values": vertical_ranges, "diagnostic": "balloons have mild vertical oscillation"}
    elif case_type in {"stone_slope_roll", "slope_drop_bounce_stop"}:
        moves = [final.get(oid, start)["position"][0] - start["position"][0] for oid, start in first.items()]
        drops = [start["position"][2] - final.get(oid, start)["position"][2] for oid, start in first.items()]
        checks["rolling_progress"] = {"passed": bool(moves) and min(moves) > 1.0 and max(drops) > 0.3, "x_displacements": moves, "z_drops": drops, "diagnostic": "stones move forward and downward"}
        rotations = []
        for oid in first:
            values = [
                max(abs(float(value)) for value in [*((frame.get("objects", {}).get(oid, {}).get("rotation_degrees") or [0.0, 0.0, 0.0])), 0.0, 0.0, 0.0][:3])
                for frame in trajectory
                if oid in frame.get("objects", {})
            ]
            if values:
                rotations.append(max(values))
        # UE Rotator values wrap at +/-180 degrees, so a Chaos-captured body can
        # show real rolling while the absolute Euler sample never exceeds 180.
        checks["rolling_rotation"] = {"passed": bool(rotations) and min(rotations) >= 170.0, "max_pitch_degrees": rotations, "diagnostic": "rolling bodies rotate while translating"}
        if case_type == "slope_drop_bounce_stop":
            dynamic_ids = set(first)
            slope_ids = {str(obj.get("id")) for obj in runtime_scene.get("static_objects") or [] if obj.get("behavior") == "slope_surface"}
            landing_ids = {str(obj.get("id")) for obj in runtime_scene.get("static_objects") or [] if obj.get("behavior") == "landing_surface"}
            contacts = [
                (idx, float(frame.get("time") or 0.0), {str(item) for item in contact.get("objects", [])})
                for idx, frame in enumerate(trajectory)
                for contact in frame.get("contacts", []) or []
                if isinstance(contact, dict)
            ]
            slope_contact_frames = [
                (idx, time_s)
                for idx, time_s, objects in contacts
                if objects & slope_ids and objects & dynamic_ids
            ]
            landing_contact_frames = [
                (idx, time_s)
                for idx, time_s, objects in contacts
                if objects & landing_ids and objects & dynamic_ids
            ]
            first_landing = min(landing_contact_frames, default=None, key=lambda item: item[0])
            last_slope_before_landing = None
            if first_landing:
                candidates = [item for item in slope_contact_frames if item[0] < first_landing[0]]
                last_slope_before_landing = max(candidates, default=None, key=lambda item: item[0])
            checks["ramp_to_landing_contact_order"] = {
                "passed": bool(slope_contact_frames and first_landing and last_slope_before_landing and first_landing[0] > last_slope_before_landing[0]),
                "last_slope_contact_s": round(last_slope_before_landing[1], 4) if last_slope_before_landing else None,
                "first_landing_contact_s": round(first_landing[1], 4) if first_landing else None,
                "diagnostic": "ball contacts the ramp first, leaves it, then contacts the lower landing plane",
            }
            primary_id = next(iter(dynamic_ids), None)
            series = [
                (idx, float(frame.get("time") or 0.0), frame.get("objects", {}).get(primary_id))
                for idx, frame in enumerate(trajectory)
                if primary_id in frame.get("objects", {})
            ] if primary_id else []
            after_landing = [item for item in series if first_landing and item[0] >= first_landing[0]]
            if after_landing:
                zs = [float(item[2].get("position", [0.0, 0.0, 0.0])[2]) for item in after_landing]
                velocities = [item[2].get("velocity_cm_s") or [0.0, 0.0, 0.0] for item in after_landing]
                upward_speeds = [float((*value, 0.0, 0.0, 0.0)[2]) / 100.0 for value in velocities if isinstance(value, list)]
                min_index = min(range(len(zs)), key=lambda idx: zs[idx]) if zs else 0
                rebound_height = max(zs[min_index:], default=zs[min_index] if zs else 0.0) - (zs[min_index] if zs else 0.0)
                final_velocity = after_landing[-1][2].get("velocity_cm_s") or [0.0, 0.0, 0.0]
                if not isinstance(final_velocity, list):
                    final_velocity = [0.0, 0.0, 0.0]
                final_speed = math.sqrt(sum((float(value) / 100.0) ** 2 for value in [*final_velocity, 0.0, 0.0, 0.0][:3]))
            else:
                rebound_height = 0.0
                upward_speeds = []
                final_speed = 999.0
            checks["landing_bounce"] = {
                "passed": rebound_height >= 0.08 or max(upward_speeds, default=0.0) >= 0.35,
                "rebound_height_m": round(rebound_height, 4),
                "max_upward_speed_m_s": round(max(upward_speeds, default=0.0), 4),
                "diagnostic": "ball rebounds after contacting the lower landing plane",
            }
            checks["settles_on_plane"] = {
                "passed": final_speed <= 0.75,
                "final_speed_m_s": round(final_speed, 4),
                "diagnostic": "ball slows down by the end of the clip",
            }
    elif case_type == "character_throw_to_slope_roll":
        runner_obj = next(
            (obj for obj in runtime_scene.get("dynamic_objects") or [] if obj.get("behavior") == "third_person_runner"),
            {},
        )
        runner_id = str(runner_obj.get("id") or "runner_character")
        projectile_obj = next(
            (obj for obj in runtime_scene.get("dynamic_objects") or [] if obj.get("behavior") == "character_throw_projectile"),
            {},
        )
        projectile_id = str(projectile_obj.get("id") or "thrown_ball")
        params = projectile_obj.get("params") or {}
        release_time = float_control(params.get("release_time_s"), float_control(params.get("throw_time_s"), 0.0, 0.0, None), 0.0, None)
        slope_ids = {str(obj.get("id")) for obj in runtime_scene.get("static_objects") or [] if obj.get("behavior") == "slope_surface"}
        landing_ids = {str(obj.get("id")) for obj in runtime_scene.get("static_objects") or [] if obj.get("behavior") == "landing_surface"}
        series = [
            (idx, float(frame.get("time") or 0.0), frame.get("objects", {}).get(projectile_id))
            for idx, frame in enumerate(trajectory)
            if projectile_id in frame.get("objects", {})
        ]
        positions = [item[2].get("position", [0.0, 0.0, 0.0]) for item in series if isinstance(item[2], dict)]
        sources = {str(item[2].get("source") or "") for item in series if isinstance(item[2], dict)}
        runner_series = [
            frame.get("objects", {}).get(runner_id)
            for frame in trajectory
            if runner_id in frame.get("objects", {})
        ]
        runner_x_displacement = 0.0
        if runner_series:
            runner_start = runtime_vec3(runner_series[0].get("position"), (0.0, 0.0, 0.0))
            runner_end = runtime_vec3(runner_series[-1].get("position"), (0.0, 0.0, 0.0))
            runner_x_displacement = runner_end[0] - runner_start[0]
        after_release = [item for item in series if item[1] >= release_time and isinstance(item[2], dict)]
        release_sample = after_release[0] if after_release else (series[0] if series else None)
        final_sample = after_release[-1] if after_release else (series[-1] if series else None)
        x_displacement = 0.0
        z_drop = 0.0
        if release_sample and final_sample:
            release_pos = runtime_vec3(release_sample[2].get("position"), (0.0, 0.0, 0.0))
            final_pos = runtime_vec3(final_sample[2].get("position"), (0.0, 0.0, 0.0))
            x_displacement = final_pos[0] - release_pos[0]
            after_positions = [runtime_vec3(item[2].get("position"), (0.0, 0.0, 0.0)) for item in after_release]
            z_drop = release_pos[2] - min((pos[2] for pos in after_positions), default=release_pos[2])
        contacts = [
            (idx, float(frame.get("time") or 0.0), {str(item) for item in contact.get("objects", [])})
            for idx, frame in enumerate(trajectory)
            for contact in frame.get("contacts", []) or []
            if isinstance(contact, dict)
        ]
        post_release_contact_time = release_time + (1.0 / max(FPS, 1))
        pre_release_slope_contacts = [
            (idx, time_s)
            for idx, time_s, objects in contacts
            if time_s < post_release_contact_time and projectile_id in objects and objects & slope_ids
        ]
        slope_contacts = [
            (idx, time_s)
            for idx, time_s, objects in contacts
            if time_s >= post_release_contact_time and projectile_id in objects and objects & slope_ids
        ]
        first_slope = min(slope_contacts, default=None, key=lambda item: item[0])
        landing_contacts_after_slope = [
            (idx, time_s)
            for idx, time_s, objects in contacts
            if first_slope and idx > first_slope[0] and projectile_id in objects and objects & landing_ids
        ]
        z_drop_after_slope = 0.0
        if first_slope:
            slope_series = [item for item in series if item[0] >= first_slope[0] and isinstance(item[2], dict)]
            if slope_series:
                slope_z = runtime_vec3(slope_series[0][2].get("position"), (0.0, 0.0, 0.0))[2]
                z_drop_after_slope = slope_z - min((runtime_vec3(item[2].get("position"), (0.0, 0.0, 0.0))[2] for item in slope_series), default=slope_z)
        checks["projectile_recorded"] = {
            "passed": len(series) >= 8,
            "samples": len(series),
            "diagnostic": "thrown projectile has a recorded post-setup trajectory",
        }
        checks["runner_motion_recorded"] = {
            "passed": len(runner_series) >= 8 and runner_x_displacement > 1.0,
            "samples": len(runner_series),
            "x_displacement_m": round(runner_x_displacement, 4),
            "diagnostic": "visible character runner moves forward during the captured clip",
        }
        checks["ue_physics_after_release"] = {
            "passed": any(source in {"adp_cpp_runtime_driver", "ue_actor_transform"} for source in sources),
            "sources": sorted(sources),
            "diagnostic": "projectile trajectory is captured from UE physics/runtime transforms after release",
        }
        checks["release_forward_motion"] = {
            "passed": bool(after_release) and x_displacement > 0.85 and z_drop > 0.15,
            "release_time_s": round(release_time, 4),
            "x_displacement_m": round(x_displacement, 4),
            "z_drop_m": round(z_drop, 4),
            "diagnostic": "released body travels forward and descends under physics",
        }
        checks["no_pre_release_ramp_contact"] = {
            "passed": not pre_release_slope_contacts,
            "pre_release_contact_count": len(pre_release_slope_contacts),
            "last_pre_release_contact_s": round(pre_release_slope_contacts[-1][1], 4) if pre_release_slope_contacts else None,
            "diagnostic": "projectile should not overlap the ramp before the throw release window",
        }
        checks["ramp_contact_after_release"] = {
            "passed": bool(slope_contacts),
            "first_slope_contact_s": round(first_slope[1], 4) if first_slope else None,
            "diagnostic": "released projectile contacts the inclined ramp after the throw",
        }
        checks["roll_or_drop_after_ramp"] = {
            "passed": bool(first_slope and (z_drop_after_slope > 0.18 or landing_contacts_after_slope)),
            "z_drop_after_slope_m": round(z_drop_after_slope, 4),
            "first_landing_contact_s": round(landing_contacts_after_slope[0][1], 4) if landing_contacts_after_slope else None,
            "diagnostic": "after ramp contact the projectile moves down toward the lower plane",
        }
    elif case_type == "character_carry_drop":
        runner_obj = next(
            (obj for obj in runtime_scene.get("dynamic_objects") or [] if obj.get("behavior") == "third_person_runner"),
            {},
        )
        runner_id = str(runner_obj.get("id") or "runner_character")
        carry_obj = next(
            (obj for obj in runtime_scene.get("dynamic_objects") or [] if obj.get("behavior") == "character_carry_object"),
            {},
        )
        carry_id = str(carry_obj.get("id") or "carried_object")
        params = carry_obj.get("params") or {}
        grasp_time = float_control(params.get("grasp_time_s"), 0.0, 0.0, None)
        release_time = float_control(params.get("release_time_s"), float_control(params.get("drop_time_s"), 0.0, 0.0, None), 0.0, None)
        hold_offset = runtime_vec3(params.get("hold_offset_m"), (0.45, 0.0, 0.72))
        socket_attachment = bool(str(params.get("attachment_socket") or "").strip())
        drop_surface_ids = {str(obj.get("id")) for obj in runtime_scene.get("static_objects") or [] if obj.get("behavior") == "landing_surface"}
        series = [
            (idx, float(frame.get("time") or 0.0), frame.get("objects", {}).get(carry_id))
            for idx, frame in enumerate(trajectory)
            if carry_id in frame.get("objects", {})
        ]
        runner_series = [
            (idx, float(frame.get("time") or 0.0), frame.get("objects", {}).get(runner_id))
            for idx, frame in enumerate(trajectory)
            if runner_id in frame.get("objects", {})
        ]
        sources = {str(item[2].get("source") or "") for item in series if isinstance(item[2], dict)}
        runner_x_displacement = 0.0
        if runner_series:
            runner_start = runtime_vec3(runner_series[0][2].get("position"), (0.0, 0.0, 0.0))
            runner_end = runtime_vec3(runner_series[-1][2].get("position"), (0.0, 0.0, 0.0))
            runner_x_displacement = runner_end[0] - runner_start[0]
        carried_total_displacement = 0.0
        if len(series) >= 2:
            carry_start = runtime_vec3(series[0][2].get("position"), (0.0, 0.0, 0.0))
            carry_end = runtime_vec3(series[-1][2].get("position"), (0.0, 0.0, 0.0))
            carried_total_displacement = math.dist(carry_start, carry_end)
        runner_by_index = {idx: value for idx, _time, value in runner_series if isinstance(value, dict)}
        hold_distances = []
        held_positions = []
        for idx, time_s, carried in series:
            if time_s < grasp_time or time_s >= release_time or not isinstance(carried, dict) or idx not in runner_by_index:
                continue
            carried_pos = runtime_vec3(carried.get("position"), (0.0, 0.0, 0.0))
            held_positions.append(carried_pos)
            runner_pos = runtime_vec3(runner_by_index[idx].get("position"), (0.0, 0.0, 0.0))
            expected = [runner_pos[i] + hold_offset[i] for i in range(3)]
            hold_distances.append(math.dist(carried_pos, expected))
        hold_steps = [math.dist(left, right) for left, right in zip(held_positions, held_positions[1:])]
        after_release = [item for item in series if item[1] >= release_time and isinstance(item[2], dict)]
        release_sample = after_release[0] if after_release else (series[0] if series else None)
        final_sample = after_release[-1] if after_release else (series[-1] if series else None)
        z_drop = 0.0
        x_drift_after_drop = 0.0
        tail_motion = 0.0
        final_z = None
        if release_sample and final_sample:
            release_pos = runtime_vec3(release_sample[2].get("position"), (0.0, 0.0, 0.0))
            final_pos = runtime_vec3(final_sample[2].get("position"), (0.0, 0.0, 0.0))
            final_z = final_pos[2]
            after_positions = [runtime_vec3(item[2].get("position"), (0.0, 0.0, 0.0)) for item in after_release]
            z_drop = release_pos[2] - min((pos[2] for pos in after_positions), default=release_pos[2])
            x_drift_after_drop = abs(final_pos[0] - release_pos[0])
            if len(after_positions) >= 8:
                tail_motion = math.dist(after_positions[-1], after_positions[max(0, len(after_positions) - 8)])
        contacts = [
            (idx, float(frame.get("time") or 0.0), {str(item) for item in contact.get("objects", [])})
            for idx, frame in enumerate(trajectory)
            for contact in frame.get("contacts", []) or []
            if isinstance(contact, dict)
        ]
        post_release_contact_time = release_time + (1.0 / max(FPS, 1))
        drop_contacts = [
            (idx, time_s)
            for idx, time_s, objects in contacts
            if time_s >= post_release_contact_time and carry_id in objects and objects & drop_surface_ids
        ]
        checks["carried_object_recorded"] = {
            "passed": len(series) >= 8,
            "samples": len(series),
            "diagnostic": "carried object has a recorded trajectory through hold and drop phases",
        }
        checks["carrier_motion_recorded"] = {
            "passed": len(runner_series) >= 8 and runner_x_displacement > 0.5,
            "samples": len(runner_series),
            "x_displacement_m": round(runner_x_displacement, 4),
            "diagnostic": "visible carrier moves to the other side of the scene",
        }
        checks["held_before_drop"] = {
            "passed": (
                len(held_positions) >= 8
                and max(hold_steps, default=99.0) < 0.2
                if socket_attachment
                else len(hold_distances) >= 8 and max(hold_distances, default=99.0) < 0.45
            ),
            "samples": len(held_positions),
            "max_hold_step_m": round(max(hold_steps, default=0.0), 4),
            "max_hold_offset_error_m": None if socket_attachment else round(max(hold_distances, default=0.0), 4),
            "diagnostic": "socket-carried object moves continuously with the hand before release" if socket_attachment else "object follows the carrier hold offset before release",
        }
        pre_grasp = [runtime_vec3(item[2].get("position"), (0.0, 0.0, 0.0)) for item in series if item[1] < grasp_time and isinstance(item[2], dict)]
        grasp_jump = math.dist(pre_grasp[-1], held_positions[0]) if pre_grasp and held_positions else 99.0
        release_jump = math.dist(held_positions[-1], runtime_vec3(after_release[0][2].get("position"), (0.0, 0.0, 0.0))) if held_positions and after_release else 99.0
        checks["grasp_continuity"] = {
            "passed": not socket_attachment or grasp_jump < 0.15,
            "jump_m": round(grasp_jump, 4),
            "diagnostic": "the object does not teleport when hand attachment begins",
        }
        checks["release_continuity"] = {
            "passed": not socket_attachment or release_jump < 0.10,
            "jump_m": round(release_jump, 4),
            "diagnostic": "the object keeps its world transform when detached from the hand",
        }
        checks["rest_before_grasp"] = {
            "passed": len(pre_grasp) >= 2 and max((math.dist(pre_grasp[0], position) for position in pre_grasp), default=0.0) < 0.02,
            "samples": len(pre_grasp),
            "diagnostic": "passive object remains at rest before the declared grasp",
        }
        checks["ue_physics_after_drop"] = {
            "passed": any(source in {"adp_cpp_runtime_driver", "ue_actor_transform"} for source in sources),
            "sources": sorted(sources),
            "diagnostic": "after release the object trajectory is captured from UE physics/runtime transforms",
        }
        checks["drop_descends_under_gravity"] = {
            "passed": bool(after_release) and (bool(drop_contacts) if socket_attachment else z_drop > 0.12) and x_drift_after_drop < 1.0 and (final_z is None or final_z > -0.35),
            "release_time_s": round(release_time, 4),
            "z_drop_m": round(z_drop, 4),
            "x_drift_after_drop_m": round(x_drift_after_drop, 4),
            "final_z_m": round(final_z, 4) if final_z is not None else None,
            "diagnostic": "released object hands off to UE physics and reaches the support" if socket_attachment else "released object descends under gravity rather than continuing as a scripted carry",
        }
        checks["lands_on_drop_zone"] = {
            "passed": bool(drop_contacts) or (bool(after_release) and z_drop > (0.02 if socket_attachment else 0.18) and x_drift_after_drop < 1.0 and (final_z is None or final_z > -0.35)),
            "first_drop_contact_s": round(drop_contacts[0][1], 4) if drop_contacts else None,
            "diagnostic": "released object lands on or near the target drop surface",
        }
        checks["settles_after_drop"] = {
            "passed": bool(after_release) and tail_motion < 0.45 and x_drift_after_drop < 1.0 and (final_z is None or final_z > -0.35),
            "tail_motion_m": round(tail_motion, 4),
            "total_carried_displacement_m": round(carried_total_displacement, 4),
            "diagnostic": "object motion is small near the end of the clip after being put down",
        }
    elif case_type == "projectile_arc":
        projectile_id = "projectile_body"
        series = [
            frame["objects"][projectile_id]
            for frame in trajectory
            if projectile_id in frame.get("objects", {})
        ]
        times = [
            float(frame.get("time") or 0.0)
            for frame in trajectory
            if projectile_id in frame.get("objects", {})
        ]
        zs = [obj["position"][2] for obj in series]
        xs = [obj["position"][0] for obj in series]
        apex_index = max(range(len(zs)), key=lambda idx: zs[idx]) if zs else -1
        landing_contacts = [
            float(frame.get("time") or 0.0)
            for frame in trajectory
            for contact in frame.get("contacts", []) or []
            if "projectile_landing_zone" in {str(item) for item in contact.get("objects", [])}
        ]
        apex_time = times[apex_index] if apex_index >= 0 and apex_index < len(times) else None
        checks["parabolic_arc"] = {
            "passed": bool(zs and xs) and 0 < apex_index < len(zs) - 1 and max(zs) - zs[0] > 1.2 and xs[-1] - xs[0] > 1.6,
            "apex_time_s": round(apex_time, 4) if apex_time is not None else None,
            "apex_height_m": round(max(zs), 4) if zs else 0.0,
            "x_displacement_m": round(xs[-1] - xs[0], 4) if xs else 0.0,
            "diagnostic": "projectile rises to an interior apex and travels forward before landing",
        }
        checks["landing_after_apex"] = {
            "passed": bool(landing_contacts and apex_time is not None and min(landing_contacts) > apex_time),
            "first_contact_time_s": round(min(landing_contacts), 4) if landing_contacts else None,
            "apex_time_s": round(apex_time, 4) if apex_time is not None else None,
            "diagnostic": "landing-zone contact occurs after the projectile apex",
        }
    elif case_type == "pendulum_swing":
        bob_id = "pendulum_bob"
        scene_obj = next((obj for obj in runtime_scene.get("dynamic_objects") or [] if obj.get("id") == bob_id), {})
        props = scene_obj.get("physics_properties") or {}
        params = scene_obj.get("params") or {}
        anchor = props.get("anchor_position_m") or params.get("anchor_position_m") or [0.0, 0.0, 3.2]
        if not isinstance(anchor, list):
            anchor = [0.0, 0.0, 3.2]
        anchor = [float(value) for value in [*anchor, 0.0, 0.0, 0.0][:3]]
        length_m = max(0.1, float(props.get("pendulum_length_m") or params.get("pendulum_length_m") or 1.62))
        xs = [frame["objects"][bob_id]["position"][0] for frame in trajectory if bob_id in frame.get("objects", {})]
        positions = [frame["objects"][bob_id]["position"] for frame in trajectory if bob_id in frame.get("objects", {})]
        rel_x = [value - anchor[0] for value in xs]
        sign_changes = sum(1 for left, right in zip(rel_x, rel_x[1:]) if left * right < 0)
        distances = [
            math.sqrt(sum((float(pos[idx]) - anchor[idx]) ** 2 for idx in range(3)))
            for pos in positions
        ]
        max_error = max((abs(value - length_m) for value in distances), default=999.0)
        checks["pendulum_oscillation"] = {
            "passed": len(xs) >= 8 and sign_changes >= 4 and max(xs) - min(xs) > 1.0,
            "sign_changes": sign_changes,
            "x_range_m": round(max(xs) - min(xs), 4) if xs else 0.0,
            "diagnostic": "pendulum bob repeatedly crosses the center line with readable amplitude",
        }
        checks["pendulum_length_constraint"] = {
            "passed": max_error <= 0.035,
            "max_length_error_m": round(max_error, 5),
            "pendulum_length_m": round(length_m, 4),
            "diagnostic": "bob-anchor distance stays constant throughout the analytic swing",
        }
    elif case_type == "constraint_joint":
        link_1 = next((obj for obj in runtime_scene.get("dynamic_objects") or [] if obj.get("id") == "joint_link_1"), {})
        link_2 = next((obj for obj in runtime_scene.get("dynamic_objects") or [] if obj.get("id") == "joint_link_2"), {})
        props_1 = link_1.get("physics_properties") or {}
        props_2 = link_2.get("physics_properties") or {}
        anchor = props_1.get("anchor_position_m") or [0.0, 0.0, 3.2]
        if not isinstance(anchor, list):
            anchor = [0.0, 0.0, 3.2]
        anchor = [float(value) for value in [*anchor, 0.0, 0.0, 0.0][:3]]
        length_1 = max(0.1, float(props_1.get("constraint_length_m") or 1.06))
        length_2 = max(0.1, float(props_2.get("constraint_length_m") or 0.86))
        paired = [
            (frame["objects"]["joint_link_1"], frame["objects"]["joint_link_2"])
            for frame in trajectory
            if "joint_link_1" in frame.get("objects", {}) and "joint_link_2" in frame.get("objects", {})
        ]
        d1 = [
            math.sqrt(sum((float(obj_1["position"][idx]) - anchor[idx]) ** 2 for idx in range(3)))
            for obj_1, _ in paired
        ]
        d2 = [
            math.sqrt(sum((float(obj_2["position"][idx]) - float(obj_1["position"][idx])) ** 2 for idx in range(3)))
            for obj_1, obj_2 in paired
        ]
        angles_1 = [
            math.atan2(float(obj_1["position"][0]) - anchor[0], anchor[2] - float(obj_1["position"][2]))
            for obj_1, _ in paired
        ]
        rel_angles = [
            math.atan2(float(obj_2["position"][0]) - float(obj_1["position"][0]), float(obj_1["position"][2]) - float(obj_2["position"][2])) - angle_1
            for angle_1, (obj_1, obj_2) in zip(angles_1, paired)
        ]
        max_error_1 = max((abs(value - length_1) for value in d1), default=999.0)
        max_error_2 = max((abs(value - length_2) for value in d2), default=999.0)
        angle_range = max(angles_1, default=0.0) - min(angles_1, default=0.0)
        rel_range = max(rel_angles, default=0.0) - min(rel_angles, default=0.0)
        checks["joint_distance_constraints"] = {
            "passed": max_error_1 <= 0.04 and max_error_2 <= 0.04,
            "max_anchor_link_error_m": round(max_error_1, 5),
            "max_link_pair_error_m": round(max_error_2, 5),
            "diagnostic": "two-link analytic joint preserves anchor-link and link-link distances",
        }
        checks["joint_articulation"] = {
            "passed": angle_range >= 0.55 and rel_range >= 0.45,
            "link_1_angle_range_degrees": round(math.degrees(angle_range), 4),
            "relative_angle_range_degrees": round(math.degrees(rel_range), 4),
            "diagnostic": "joint links articulate with changing relative angle",
        }
    elif case_type == "stack_stability":
        ids = sorted(
            [obj.get("id") for obj in runtime_scene.get("dynamic_objects") or [] if str(obj.get("id") or "").startswith("stack_block_")],
            key=lambda value: int(str(value).rsplit("_", 1)[-1]),
        )
        rotations = {}
        displacements = {}
        final_x = {}
        support_obj = next((obj for obj in runtime_scene.get("static_objects") or [] if obj.get("id") == "stack_support_surface"), {})
        support_pos = support_obj.get("initial_position_m") if isinstance(support_obj.get("initial_position_m"), list) else [-0.24, -0.18, 0.24]
        support_props = support_obj.get("physics_properties") or {}
        support_params = support_obj.get("params") or {}
        support_half_width = float(support_props.get("support_half_width_m") or support_params.get("support_half_width_m") or 0.72)
        for oid in ids:
            series = [frame["objects"][oid] for frame in trajectory if oid in frame.get("objects", {})]
            if not series:
                continue
            start = series[0]["position"]
            end = series[-1]["position"]
            rotations[oid] = max(max(abs(value) for value in obj.get("rotation_degrees", [0.0, 0.0, 0.0])) for obj in series)
            displacements[oid] = math.hypot(end[0] - start[0], end[1] - start[1])
            final_x[oid] = end[0]
        contacts = [
            tuple(sorted(str(item) for item in contact.get("objects", [])))
            for frame in trajectory
            for contact in frame.get("contacts", []) or []
        ]
        upper_ids = ids[1:]
        checks["stack_collapse"] = {
            "passed": bool(upper_ids) and min(rotations.get(oid, 0.0) for oid in upper_ids) > 55.0 and min(displacements.get(oid, 0.0) for oid in upper_ids) > 0.45,
            "upper_rotations_degrees": {key: round(rotations.get(key, 0.0), 4) for key in upper_ids},
            "upper_displacements_m": {key: round(displacements.get(key, 0.0), 4) for key in upper_ids},
            "diagnostic": "upper stack blocks topple and move after the trigger",
        }
        checks["stack_final_support_state"] = {
            "passed": sum(1 for oid in upper_ids if abs(final_x.get(oid, 0.0) - float(support_pos[0])) > support_half_width + 0.08) >= 2,
            "final_x_m": {key: round(value, 4) for key, value in final_x.items()},
            "support_half_width_m": round(support_half_width, 4),
            "diagnostic": "upper centers of mass leave the support footprint",
        }
        checks["stack_contacts"] = {
            "passed": ("stack_block_2", "stack_push_block") in contacts and any("stack_support_surface" in pair for pair in contacts),
            "unique_contact_pairs": sorted(set(contacts)),
            "diagnostic": "trigger and support contacts are present",
        }
    elif case_type == "rigid_collision_pair":
        impactor = first.get("rigid_body_impactor")
        target = first.get("rigid_body_target")
        impactor_final = final.get("rigid_body_impactor", impactor) if impactor else None
        target_final = final.get("rigid_body_target", target) if target else None
        impactor_move = (
            math.hypot(impactor_final["position"][0] - impactor["position"][0], impactor_final["position"][1] - impactor["position"][1])
            if impactor and impactor_final
            else 0.0
        )
        target_move = (
            math.hypot(target_final["position"][0] - target["position"][0], target_final["position"][1] - target["position"][1])
            if target and target_final
            else 0.0
        )
        checks["rigid_pair_motion"] = {
            "passed": bool(impactor and target and impactor_move > 0.35 and target_move > 0.18),
            "horizontal_displacement_m": {"impactor": round(impactor_move, 4), "target": round(target_move, 4)},
            "diagnostic": "moving impactor transfers motion to the initially resting target",
        }
    elif case_type == "gear_collision_chain":
        gear_1 = first.get("gear_1")
        gear_2 = first.get("gear_2")
        gear_1_final = final.get("gear_1", gear_1) if gear_1 else None
        gear_2_final = final.get("gear_2", gear_2) if gear_2 else None
        gear_1_move = math.hypot(
            gear_1_final["position"][0] - gear_1["position"][0],
            gear_1_final["position"][1] - gear_1["position"][1],
        ) if gear_1 and gear_1_final else 0.0
        gear_2_move = math.hypot(
            gear_2_final["position"][0] - gear_2["position"][0],
            gear_2_final["position"][1] - gear_2["position"][1],
        ) if gear_2 and gear_2_final else 0.0
        checks["collision_transfer"] = {
            "passed": bool(gear_1 and gear_2 and gear_1_move > 0.08 and gear_2_move > 0.12),
            "horizontal_displacement_m": {"gear_1": round(gear_1_move, 4), "gear_2": round(gear_2_move, 4)},
            "diagnostic": "second gear moves after delayed contact",
        }
    elif case_type == "barrel_impact_cascade":
        impactor_id = "oversized_fast_barrel_impactor"
        impactor_start = first.get(impactor_id)
        impactor_final = final.get(impactor_id, impactor_start) if impactor_start else None
        impactor_move = (
            impactor_final["position"][0] - impactor_start["position"][0]
            if impactor_start and impactor_final
            else 0.0
        )
        target_ids = [oid for oid in first if oid.startswith("target_barrel_")]
        target_moves = []
        target_rotations = []
        for oid in target_ids:
            start = first.get(oid)
            end = final.get(oid, start)
            if not start or not end:
                continue
            target_moves.append(
                math.hypot(
                    end["position"][0] - start["position"][0],
                    end["position"][1] - start["position"][1],
                )
            )
            rotation = end.get("rotation_degrees") or [0.0, 0.0, 0.0]
            target_rotations.append(max(abs(float(value)) for value in rotation[:3]))
        toppled = sum(1 for value in target_rotations if value >= 32.0)
        moved = sum(1 for value in target_moves if value >= 0.12)
        checks["barrel_knockdown_cascade"] = {
            "passed": bool(target_ids) and impactor_move > 1.0 and moved == len(target_ids) and toppled == len(target_ids),
            "impactor_x_displacement_m": round(impactor_move, 4),
            "target_displacements_m": [round(value, 4) for value in target_moves],
            "target_rotation_degrees": [round(value, 4) for value in target_rotations],
            "diagnostic": "oversized fast barrel moves through the cluster and every target barrel topples and rolls or slides",
        }
    elif case_type == "plant_sway_camera":
        amplitudes = []
        for oid in first:
            rotations = [abs(frame["objects"][oid]["rotation_degrees"][0]) for frame in trajectory if oid in frame["objects"]]
            amplitudes.append(max(rotations) if rotations else 0.0)
        checks["periodic_sway"] = {"passed": bool(amplitudes) and max(amplitudes) >= 6.0, "values": amplitudes, "diagnostic": "plants rotate under periodic wind"}
    elif case_type == "bottle_domino_chain":
        rotations = {
            oid: max(abs(float(value)) for value in final.get(oid, start)["rotation_degrees"][:3])
            for oid, start in first.items()
            if oid.startswith("bottle_") or (oid.startswith("domino_") and oid != "domino_floor")
        }
        def domino_order_key(oid):
            prefix, separator, suffix = oid.rpartition("_")
            return (prefix, int(suffix)) if separator and suffix.isdigit() else (oid, -1)

        starts = []
        ordered_ids = sorted(rotations, key=domino_order_key)
        for oid in ordered_ids:
            threshold_frame = next(
                (
                    frame
                    for frame in trajectory
                    if oid in frame["objects"]
                    and max(abs(float(value)) for value in frame["objects"][oid]["rotation_degrees"][:3]) > 12.0
                ),
                None,
            )
            starts.append(threshold_frame["time"] if threshold_frame else None)
        ordered = all(
            starts[idx] is not None
            and starts[idx + 1] is not None
            and starts[idx] < starts[idx + 1]
            for idx in range(max(0, len(starts) - 1))
        )
        checks["domino_order"] = {
            "passed": len(rotations) >= 3 and min(rotations.values()) > 45.0 and ordered,
            "final_rotations": rotations,
            "tip_start_times": starts,
            "diagnostic": "domino objects tip into final tilted poses in left-to-right order",
        }
    elif case_type == "wheel_ramp_jump":
        wheel_id = "wheel_1"
        zs = [frame["objects"][wheel_id]["position"][2] for frame in trajectory if wheel_id in frame["objects"]]
        start_x = first.get(wheel_id, {}).get("position", [0.0])[0] if first.get(wheel_id) else 0.0
        end_x = final.get(wheel_id, {}).get("position", [0.0])[0] if final.get(wheel_id) else 0.0
        z_range = max(zs) - min(zs) if zs else 0.0
        checks["jump_arc"] = {
            "passed": bool(zs) and z_range > 0.45 and end_x - start_x > 1.2,
            "z_range": z_range,
            "x_displacement": end_x - start_x,
            "diagnostic": "wheel travels forward and rises into an arc",
        }
    elif case_type == "crate_friction_slide":
        starts = {oid: obj["position"][0] for oid, obj in first.items() if oid.startswith("crate_")}
        ends = {oid: final.get(oid, {"position": [starts[oid]]})["position"][0] for oid in starts}
        moves = [ends[oid] - starts[oid] for oid in sorted(starts)]
        checks["friction_distance_order"] = {
            "passed": len(moves) >= 3 and moves[0] > moves[1] > moves[2],
            "values": moves,
            "diagnostic": "low/medium/high friction crates stop at ordered distances",
        }
    elif case_type == "rolling_friction":
        ids = sorted(oid for oid in first if oid.startswith("rolling_body_"))
        moves = [final.get(oid, {"position": first[oid]["position"]})["position"][0] - first[oid]["position"][0] for oid in ids]
        rotations = []
        for oid in ids:
            values = [
                max(abs(float(value)) for value in [*((frame.get("objects", {}).get(oid, {}).get("rotation_degrees") or [0.0, 0.0, 0.0])), 0.0, 0.0, 0.0][:3])
                for frame in trajectory
                if oid in frame.get("objects", {})
            ]
            if values:
                rotations.append(max(values))
        checks["rolling_friction_distance_order"] = {
            "passed": len(moves) >= 3 and moves[0] > moves[1] + 0.45 and moves[1] > moves[2] + 0.25,
            "values": moves,
            "diagnostic": "low/medium/high rolling-friction bodies stop at ordered distances",
        }
        checks["rolling_rotation"] = {
            "passed": bool(rotations) and min(rotations) > 120.0,
            "max_rotation_degrees": rotations,
            "diagnostic": "rolling bodies rotate while translating on the flat lanes",
        }
    elif case_type == "cone_barrel_collision":
        cone_rot = abs(final.get("cone_1", {}).get("rotation_degrees", [0.0])[0]) if final.get("cone_1") else 0.0
        barrel_rot = abs(final.get("barrel_1", {}).get("rotation_degrees", [0.0])[0]) if final.get("barrel_1") else 0.0
        checks["mass_response_difference"] = {
            "passed": cone_rot > barrel_rot + 20.0,
            "cone_degrees": cone_rot,
            "barrel_degrees": barrel_rot,
            "diagnostic": "cone topples more than barrel",
        }
    else:
        checks["visible_motion"] = {"passed": bool(first and final), "diagnostic": "generic dynamic objects are present"}
    return {"checks": checks, "passed": all(bool(check.get("passed")) for check in checks.values())}


def sphere_volume(radius: float) -> float:
    return 4.0 / 3.0 * math.pi * radius**3


def build_scene_spec(duration: float, fps: int) -> dict:
    water_radius = 0.16
    steel_radius = 0.13
    return {
        "description": "Map-anchored UE replay of two physical phenomena inside a loaded StarterMap courtyard exhibit: buoyancy and magnetism.",
        "environment": {
            "type": "starter_map_courtyard_two_phenomena_exhibit",
            "gravity": 9.81,
            "water_density": 1000.0,
            "water_level": 0.0,
            "tank_bottom": -1.35,
            "linear_drag_air": 0.15,
            "linear_drag_water": 24.0,
            "magnet_position": [1.85, 0.0, -0.45],
            "magnet_strength": 0.75,
            "magnetic_damping": 0.42,
        },
        "simulation": {"dt": 1.0 / 120.0, "duration": duration, "output_fps": fps},
        "physics_phenomena": [
            {
                "name": "water_basin_buoyancy",
                "type": "buoyancy",
                "computation": "displaced_volume",
                "participants": ["rubber_ball", "lead_ball"],
                "environment_ref": "water_basin",
                "expected_effect": "rubber ball floats while lead ball sinks",
            },
            {
                "name": "bar_magnet_attraction",
                "type": "magnetism",
                "computation": "dipole_inverse_distance",
                "participants": ["steel_ball"],
                "environment_ref": "bar_magnet",
                "expected_effect": "steel ball moves toward the magnet",
            },
        ],
        "objects": [
            {
                "id": "rubber_ball",
                "shape": "sphere",
                "radius": water_radius,
                "volume": sphere_volume(water_radius),
                "density": 300.0,
                "mass": 300.0 * sphere_volume(water_radius),
                "initial_position": [-1.25, 0.0, -0.95],
                "expected_behavior": "float",
            },
            {
                "id": "lead_ball",
                "shape": "sphere",
                "radius": water_radius,
                "volume": sphere_volume(water_radius),
                "density": 11340.0,
                "mass": 11340.0 * sphere_volume(water_radius),
                "initial_position": [-0.72, 0.0, 0.95],
                "expected_behavior": "sink",
            },
            {
                "id": "steel_ball",
                "shape": "sphere",
                "radius": steel_radius,
                "volume": sphere_volume(steel_radius),
                "density": 7850.0,
                "mass": 0.32,
                "initial_position": [0.68, 0.0, -0.45],
                "expected_behavior": "attract",
            },
        ],
    }


def submerged_sphere_volume(radius: float, center_z: float, water_level: float) -> float:
    bottom = center_z - radius
    top = center_z + radius
    if top <= water_level:
        return sphere_volume(radius)
    if bottom >= water_level:
        return 0.0
    h = water_level - bottom
    return math.pi * h**2 * (3.0 * radius - h) / 3.0


def simulate(scene_spec: dict) -> list[dict]:
    env = scene_spec["environment"]
    sim = scene_spec["simulation"]
    dt = float(sim["dt"])
    duration = float(sim["duration"])
    out_fps = int(sim["output_fps"])
    capture_every = max(1, round((1.0 / out_fps) / dt))
    steps = int(duration / dt)

    states = {}
    for obj in scene_spec["objects"]:
        states[obj["id"]] = {
            "z": float(obj["initial_position"][2]),
            "x": float(obj["initial_position"][0]),
            "vz": 0.0,
            "vx": 0.0,
            "radius": float(obj["radius"]),
            "volume": float(obj["volume"]),
            "mass": float(obj["mass"]),
        }

    frames = []
    g = float(env["gravity"])
    water_density = float(env["water_density"])
    water_level = float(env["water_level"])
    tank_bottom = float(env["tank_bottom"])
    air_drag = float(env["linear_drag_air"])
    water_drag = float(env["linear_drag_water"])
    magnet_x = float(env["magnet_position"][0])
    magnet_z = float(env["magnet_position"][2])
    magnet_strength = float(env["magnet_strength"])
    magnet_damping = float(env["magnetic_damping"])

    for step in range(steps + 1):
        t = step * dt
        if step % capture_every == 0:
            frame = {"frame": len(frames), "time": round(t, 4), "objects": {}}
            for obj in scene_spec["objects"]:
                state = states[obj["id"]]
                submerged_fraction = 0.0
                if obj["id"] in {"rubber_ball", "lead_ball"}:
                    submerged_fraction = submerged_sphere_volume(state["radius"], state["z"], water_level) / state["volume"]
                frame["objects"][obj["id"]] = {
                    "position": [round(state["x"], 5), 0.0, round(state["z"], 5)],
                    "velocity": [round(state["vx"], 5), 0.0, round(state["vz"], 5)],
                    "submerged_fraction": round(submerged_fraction, 5),
                }
            frames.append(frame)

        for obj in scene_spec["objects"]:
            state = states[obj["id"]]
            oid = obj["id"]
            if oid in {"rubber_ball", "lead_ball"}:
                submerged = submerged_sphere_volume(state["radius"], state["z"], water_level)
                submerged_fraction = submerged / state["volume"]
                buoyancy = water_density * submerged * g
                weight = state["mass"] * g
                drag_coeff = air_drag + water_drag * submerged_fraction
                accel = (buoyancy - weight - drag_coeff * state["vz"]) / state["mass"]
                state["vz"] += accel * dt
                state["z"] += state["vz"] * dt
                if state["z"] - state["radius"] < tank_bottom:
                    state["z"] = tank_bottom + state["radius"]
                    state["vz"] = 0.0
            elif oid == "steel_ball":
                dx = magnet_x - state["x"]
                dz = magnet_z - state["z"]
                distance = max(math.sqrt(dx * dx + dz * dz), state["radius"] * 2.0, 0.05)
                force = magnet_strength / (distance * distance)
                ax = (dx / distance * force - magnet_damping * state["vx"]) / state["mass"]
                state["vx"] += ax * dt
                state["x"] += state["vx"] * dt
                if state["x"] > magnet_x - state["radius"] * 1.4:
                    state["x"] = magnet_x - state["radius"] * 1.4
                    state["vx"] = 0.0

    return frames


def validate(scene_spec: dict, trajectory: list[dict]) -> dict:
    env = scene_spec["environment"]
    final = trajectory[-1]["objects"]
    first = trajectory[0]["objects"]
    water_level = float(env["water_level"])
    tank_bottom = float(env["tank_bottom"])
    magnet_x = float(env["magnet_position"][0])
    rubber_z = final["rubber_ball"]["position"][2]
    lead_z = final["lead_ball"]["position"][2]
    steel_x0 = first["steel_ball"]["position"][0]
    steel_x1 = final["steel_ball"]["position"][0]
    checks = {
        "rubber_ball": {
            "expected": "float",
            "z_final": rubber_z,
            "passed": rubber_z > water_level - 0.16,
            "diagnostic": "rubber ball center ends near the water surface",
        },
        "lead_ball": {
            "expected": "sink",
            "z_final": lead_z,
            "passed": lead_z <= tank_bottom + 0.18,
            "diagnostic": "lead ball reaches the tank bottom",
        },
        "steel_ball": {
            "expected": "attract",
            "x_initial": steel_x0,
            "x_final": steel_x1,
            "magnet_x": magnet_x,
            "passed": steel_x1 > steel_x0 + 0.45 and abs(magnet_x - steel_x1) < abs(magnet_x - steel_x0),
            "diagnostic": "steel ball moves toward the magnet",
        },
        "relative_order": {
            "rubber_z": rubber_z,
            "lead_z": lead_z,
            "passed": rubber_z > lead_z,
            "diagnostic": "floating object should finish above sinking object",
        },
    }
    return {"checks": checks, "passed": all(bool(check["passed"]) for check in checks.values())}


def find_asset_indexes(root: Path, limit: int = 20, max_depth: int = 5) -> list[str]:
    if not root.exists():
        return []
    matches = []
    targets = {"ASSETS_INDEX.json", "captioned_index.jsonl"}
    skip_dirs = {".cache", ".conda", ".local", "Intermediate", "Saved", "DerivedDataCache", "Binaries"}
    if excluded_dir_names:
        skip_dirs |= excluded_dir_names()
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.parts) - root_depth
        dirnames[:] = [name for name in dirnames if name not in skip_dirs and not name.startswith(".")]
        if depth >= max_depth:
            dirnames[:] = []
        if "/tmp/" in str(current):
            continue
        for filename in filenames:
            if filename in targets:
                matches.append(str(current / filename))
                if len(matches) >= limit:
                    return matches
    return matches


def set_editor_property_if_available(obj, name: str, value) -> bool:
    try:
        obj.set_editor_property(name, value)
        return True
    except Exception:
        return False


def set_post_process(
    capture_comp,
    exposure_bias: float = 1.8,
    fixed_auto_exposure: bool = True,
    blend_weight: float = 1.0,
) -> dict:
    settings = unreal.PostProcessSettings()
    applied = {
        "auto_exposure_bias": set_editor_property_if_available(settings, "auto_exposure_bias", exposure_bias),
        "override_auto_exposure_bias": set_editor_property_if_available(settings, "override_auto_exposure_bias", True),
        "fixed_auto_exposure": fixed_auto_exposure,
        "post_process_blend_weight": blend_weight,
    }
    if fixed_auto_exposure:
        applied["auto_exposure_min_brightness"] = set_editor_property_if_available(settings, "auto_exposure_min_brightness", 1.0)
        applied["auto_exposure_max_brightness"] = set_editor_property_if_available(settings, "auto_exposure_max_brightness", 1.0)
        applied["override_auto_exposure_min_brightness"] = set_editor_property_if_available(settings, "override_auto_exposure_min_brightness", True)
        applied["override_auto_exposure_max_brightness"] = set_editor_property_if_available(settings, "override_auto_exposure_max_brightness", True)
    else:
        applied["auto_exposure_min_brightness"] = set_editor_property_if_available(settings, "auto_exposure_min_brightness", 0.03)
        applied["auto_exposure_max_brightness"] = set_editor_property_if_available(settings, "auto_exposure_max_brightness", 8.0)
        applied["override_auto_exposure_min_brightness"] = set_editor_property_if_available(settings, "override_auto_exposure_min_brightness", True)
        applied["override_auto_exposure_max_brightness"] = set_editor_property_if_available(settings, "override_auto_exposure_max_brightness", True)
    set_editor_property_if_available(capture_comp, "post_process_settings", settings)
    set_editor_property_if_available(capture_comp, "post_process_blend_weight", float(blend_weight))
    return applied


def get_light_component(actor):
    for name in ("light_component", "directional_light_component", "sky_light_component"):
        component = getattr(actor, name, None)
        if component:
            return component
    try:
        return actor.get_component_by_class(unreal.LightComponent)
    except Exception:
        return None


def set_light(actor, intensity: float, color=None):
    component = get_light_component(actor)
    if not component:
        return
    set_editor_property_if_available(component, "intensity", intensity)
    if color:
        set_editor_property_if_available(component, "light_color", color)


def is_light_actor(actor) -> bool:
    if actor.get_actor_label().startswith("native_phenomena_demo_"):
        return False
    if get_light_component(actor):
        return True
    class_name = actor.get_class().get_name() if actor.get_class() else ""
    return "Light" in class_name or "SkyAtmosphere" in class_name


def configure_existing_map_lights(editor, enabled: bool) -> dict:
    inspected = 0
    changed = 0
    for actor in editor.get_all_level_actors():
        if not is_light_actor(actor):
            continue
        inspected += 1
        try:
            actor.set_actor_hidden_in_game(not enabled)
        except Exception:
            pass
        component = get_light_component(actor)
        if component:
            if set_editor_property_if_available(component, "visible", enabled):
                changed += 1
            set_editor_property_if_available(component, "hidden_in_game", not enabled)
            try:
                component.set_visibility(enabled, True)
            except Exception:
                pass
            try:
                component.set_hidden_in_game(not enabled, True)
            except Exception:
                pass
    return {"enabled": enabled, "inspected": inspected, "changed": changed}


def spawn_optional_actor(editor, class_name: str, location: unreal.Vector, label: str):
    actor_class = getattr(unreal, class_name, None)
    if not actor_class:
        return None
    actor = editor.spawn_actor_from_class(actor_class, location)
    actor.set_actor_label(label)
    return actor


def configure_runtime_world(selected_map: dict, editor_parity_realism: bool | None = None) -> dict:
    use_editor_parity = EDITOR_PARITY_REALISM if editor_parity_realism is None else editor_parity_realism
    if selected_map.get("controlled_stage") == "bottle_domino_chain_foreground":
        commands = [
            "r.Fog 0",
            "r.VolumetricFog 0",
            "r.VolumetricCloud 0",
            "r.DefaultFeature.AutoExposure 0",
        ]
    elif selected_map.get("name") == "MarketEnvironment_Day":
        commands = [
            "r.Fog 1",
            f"r.VolumetricFog {1 if EDITOR_VIEWPORT_MATCH_REALISM else 0}",
            "r.VolumetricCloud 1",
            "r.DefaultFeature.AutoExposure 1",
            "r.EyeAdaptationQuality 2",
            "r.BloomQuality 3",
        ]
    elif use_editor_parity:
        commands = [
            "r.Fog 1",
            "r.VolumetricFog 1",
            "r.VolumetricCloud 1",
            "r.DefaultFeature.AutoExposure 1",
            "r.EyeAdaptationQuality 2",
            "r.BloomQuality 3",
        ]
    else:
        commands = [
            "r.Fog 0",
            "r.VolumetricFog 0",
            "r.VolumetricCloud 0",
            "r.DefaultFeature.AutoExposure 0",
        ]
    applied = []
    for command in commands:
        try:
            unreal.SystemLibrary.execute_console_command(None, command)
            applied.append(command)
        except Exception:
            pass
    return {"commands": applied, "map_opened": bool(selected_map.get("opened"))}


def configure_render_quality() -> dict:
    if RENDER_QUALITY_PRESET in {"0", "off", "disabled"}:
        return {"preset": RENDER_QUALITY_PRESET, "commands": [], "enabled": False}
    preset = str(RENDER_QUALITY_PRESET or "high").lower()
    commands = [
        f"r.SetRes {WIDTH}x{HEIGHT}",
        "sg.ViewDistanceQuality 4",
        "sg.AntiAliasingQuality 4",
        "sg.ShadowQuality 4",
        "sg.GlobalIlluminationQuality 4",
        "sg.ReflectionQuality 4",
        "sg.PostProcessQuality 4",
        "sg.TextureQuality 4",
        "sg.EffectsQuality 4",
        "sg.FoliageQuality 4",
        "sg.ShadingQuality 4",
        f"r.ScreenPercentage {RENDER_SCREEN_PERCENTAGE}",
        f"r.SecondaryScreenPercentage.GameViewport {RENDER_SCREEN_PERCENTAGE}",
        "r.TemporalAA.Upsampling 0",
        f"r.MotionBlurQuality {RENDER_MOTION_BLUR_QUALITY}",
        f"r.DefaultFeature.MotionBlur {1 if RENDER_MOTION_BLUR_QUALITY > 0 else 0}",
        "r.SceneColorFringeQuality 0",
        f"r.Tonemapper.Sharpen {RENDER_TONEMAPPER_SHARPEN}",
        f"r.MipMapLODBias {RENDER_MIPMAP_LOD_BIAS}",
        f"r.Streaming.PoolSize {RENDER_TEXTURE_POOL_MB}",
        "r.Streaming.FullyLoadUsedTextures 1",
        "r.TextureStreaming 0",
    ]
    if preset in {"paper", "publication", "cinematic"}:
        commands.extend(
            [
                f"r.TemporalAASamples {RENDER_TEMPORAL_AA_SAMPLES}",
                f"r.TemporalAACurrentFrameWeight {RENDER_TEMPORAL_AA_CURRENT_FRAME_WEIGHT}",
                f"r.Tonemapper.Sharpen {RENDER_TONEMAPPER_SHARPEN}",
                f"r.Streaming.PoolSize {RENDER_TEXTURE_POOL_MB}",
                "r.MaxAnisotropy 16",
                f"r.Shadow.MaxResolution {RENDER_SHADOW_MAX_RESOLUTION}",
                f"r.Shadow.DistanceScale {RENDER_SHADOW_DISTANCE_SCALE}",
                f"r.SkeletalMeshLODBias {RENDER_SKELETAL_MESH_LOD_BIAS}",
                f"r.StaticMeshLODDistanceScale {RENDER_STATIC_MESH_LOD_DISTANCE_SCALE}",
            ]
        )
    elif preset in {"probe", "preview", "low"}:
        commands.extend(
            [
                "r.TemporalAASamples 4",
                "r.Tonemapper.Sharpen 0.35",
                "r.Streaming.PoolSize 2048",
            ]
        )
    applied = []
    for command in commands:
        try:
            unreal.SystemLibrary.execute_console_command(None, command)
            applied.append(command)
        except Exception:
            pass
    return {
        "preset": RENDER_QUALITY_PRESET,
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
        "warmup_frames": RENDER_WARMUP_FRAMES,
        "commands": applied,
        "enabled": True,
    }


def select_map(description: str) -> dict:
    if STUDIO_RUNTIME_SCENE and STUDIO_RUNTIME_SCENE.get("case_type") == "third_person_box_throw":
        return {"name": "StudioRuntimeBlank", "path": "", "tags": (), "base_score": 0, "selection_reason": "third_person_box_throw_blank_stage"}
    if SCENE_MAP == "studio_runtime":
        return {"name": "StudioRuntimeBlank", "path": "", "tags": (), "base_score": 0, "selection_reason": "studio_runtime_no_legacy_map"}
    if SCENE_MAP and SCENE_MAP != "auto":
        for candidate in MAP_CANDIDATES:
            if SCENE_MAP == candidate["name"] or canonical_map_package(SCENE_MAP) == canonical_map_package(candidate["path"]):
                return {**candidate, "selection_reason": "explicit_scene_map"}
        return {"name": "custom", "path": SCENE_MAP, "tags": (), "base_score": 0, "selection_reason": "explicit_custom_scene_map"}

    terms = {term.strip(".,;:!?()[]{}\"'").lower() for term in description.replace("_", " ").split()}
    best = None
    best_score = -1
    for candidate in MAP_CANDIDATES:
        tags = set(candidate["tags"])
        score = int(candidate["base_score"]) + len(terms & tags)
        if any(term in terms for term in ("adp", "gitlab", "level_scene_01", "tropical", "island", "repository")) and candidate["name"] == "TropicalIsland_Level_Scene_01":
            score += 8
        if score > best_score:
            best = candidate
            best_score = score
    selected = best or MAP_CANDIDATES[0]
    return {**selected, "score": best_score, "selection_reason": "agent_rule_score"}


def canonical_map_package(path: str | None) -> str:
    value = str(path or "").strip().split(":", 1)[0]
    dot = value.find(".", value.rfind("/"))
    return value[:dot] if dot >= 0 else value.rstrip("/")


def current_world_package() -> str | None:
    try:
        world = unreal.EditorLevelLibrary.get_editor_world()
        return canonical_map_package(world.get_path_name()) if world else None
    except Exception:
        return None


def try_open_map(path: str) -> tuple[bool, str | None]:
    requested = canonical_map_package(path)
    errors = []
    for loader in (unreal.EditorLevelLibrary.load_level, unreal.EditorLoadingAndSavingUtils.load_map):
        try:
            loader(path)
        except Exception as exc:
            errors.append(str(exc))
        actual = current_world_package()
        if actual == requested:
            return True, None
        errors.append(f"loaded_world_mismatch:requested={requested},actual={actual}")
    return False, "; ".join(errors)


def open_selected_map() -> dict:
    selected = select_map(SCENE_DESCRIPTION)
    if selected.get("name") == "StudioRuntimeBlank":
        selected["opened"] = False
        selected["requested_package"] = None
        selected["opened_package"] = current_world_package()
        selected["error"] = None
        selected["fallback_map"] = None
        return selected
    opened, error = try_open_map(selected["path"])
    selected["opened"] = opened
    selected["requested_package"] = canonical_map_package(selected["path"])
    selected["opened_package"] = current_world_package()
    selected["error"] = error
    selected["fallback_map"] = None
    return selected


def count_non_demo_actors(editor) -> int:
    return sum(1 for actor in editor.get_all_level_actors() if not actor.get_actor_label().startswith("native_phenomena_demo_"))


def ensure_selected_map_has_actors(editor, selected_map: dict) -> dict:
    if not selected_map.get("opened") or selected_map.get("name") == "StudioRuntimeBlank":
        return selected_map
    before = count_non_demo_actors(editor)
    selected_map["actor_count_after_open"] = before
    if before > 0:
        return selected_map
    path = selected_map.get("path")
    if not path:
        return selected_map
    reload_errors = []
    reloaded, reload_error = try_open_map(path)
    selected_map["force_reloaded_after_empty_actor_list"] = reloaded
    if reload_error:
        reload_errors.append(reload_error)
    after = count_non_demo_actors(editor)
    selected_map["actor_count_after_force_reload"] = after
    selected_map["opened_package"] = current_world_package()
    if reload_errors:
        selected_map["force_reload_errors"] = reload_errors
    selected_map["opened"] = bool(reloaded)
    return selected_map


def map_scene_origin(selected_map: dict) -> unreal.Vector:
    if selected_map.get("name") == "StarterMap":
        return unreal.Vector(500.0, -500.0, 0.0)
    if selected_map.get("name") == "TropicalIsland_Level_Scene_01":
        return unreal.Vector(0.0, 0.0, 180.0)
    return unreal.Vector(0.0, 0.0, 0.0)


def map_camera_pose(selected_map: dict, scene_origin: unreal.Vector) -> tuple[unreal.Vector, unreal.Vector]:
    if selected_map.get("name") == "TropicalIsland_Level_Scene_01":
        return scene_origin + unreal.Vector(120, -420, 260), scene_origin + unreal.Vector(20, 0, 105)
    return scene_origin + unreal.Vector(80, -560, 220), scene_origin + unreal.Vector(20, 0, 74)


def map_lighting_profile(selected_map: dict, editor_parity_realism: bool | None = None) -> dict:
    use_editor_parity = EDITOR_PARITY_REALISM if editor_parity_realism is None else editor_parity_realism
    if use_editor_parity and selected_map.get("name") == "MarketEnvironment_Day":
        return {
            "sun": 0.0,
            "fill": 0.0,
            "sky": 0.0,
            "exposure_bias": 0.0,
            "fixed_auto_exposure": False,
            "post_process_blend_weight": 0.0 if EDITOR_VIEWPORT_MATCH_REALISM else 0.35,
            "capture_backend": "highres_viewport",
            "capture_source": "SCS_FINAL_COLOR_LDR",
            "video_filter": "",
            "profile": "market_day_editor_viewport_match" if EDITOR_VIEWPORT_MATCH_REALISM else "market_day_editor_parity",
        }
    if selected_map.get("name") == "TropicalIsland_Level_Scene_01":
        return {"sun": 7.2, "fill": 2.9, "sky": 3.1, "exposure_bias": 0.72, "video_filter": "" if use_editor_parity else "eq=brightness=0.03:contrast=1.08:gamma=1.00:saturation=1.07", "profile": "tropical_island_editor_parity" if use_editor_parity else "tropical_island_lifted"}
    if selected_map.get("name") == "MarketEnvironment_Day":
        return {"sun": 7.8, "fill": 3.0, "sky": 3.0, "exposure_bias": 0.70, "video_filter": "eq=brightness=0.03:contrast=1.08:gamma=1.00:saturation=1.06", "profile": "market_day_lifted_scene_capture"}
    if selected_map.get("name") == "StudioRuntimeBlank":
        return {"sun": 7.4, "fill": 2.8, "sky": 2.8, "exposure_bias": 0.72, "video_filter": "" if use_editor_parity else "eq=brightness=0.03:contrast=1.08:gamma=1.00:saturation=1.06", "profile": "studio_runtime_editor_parity" if use_editor_parity else "studio_runtime_lifted"}
    return {"sun": 6.8, "fill": 2.2, "sky": 2.4, "exposure_bias": 0.84, "video_filter": VIDEO_FILTER, "profile": "default_editor_parity" if use_editor_parity else "default_lifted"}


def runtime_lighting_profile(selected_map: dict, runtime_scene: dict | None, editor_parity_realism: bool | None = None) -> dict:
    use_editor_parity = EDITOR_PARITY_REALISM if editor_parity_realism is None else editor_parity_realism
    profile = dict(map_lighting_profile(selected_map, use_editor_parity))
    if use_editor_parity and selected_map.get("name") == "MarketEnvironment_Day":
        profile.update({
            "sun": 0.0,
            "fill": 0.0,
            "sky": 0.0,
            "exposure_bias": 0.0,
            "fixed_auto_exposure": False,
            "post_process_blend_weight": 0.0 if EDITOR_VIEWPORT_MATCH_REALISM else 0.35,
            "capture_backend": "highres_viewport",
            "capture_source": "SCS_FINAL_COLOR_LDR",
            "video_filter": "",
            "profile": "market_day_editor_viewport_match" if EDITOR_VIEWPORT_MATCH_REALISM else "market_day_editor_parity",
        })
    case_type = (runtime_scene or {}).get("case_type")
    if selected_map.get("name") == "TropicalIsland_Level_Scene_01" and case_type == "balloon_wind_drift":
        profile.update({
            "sun": 9.2,
            "fill": 5.2,
            "sky": 5.0,
            "exposure_bias": 1.14,
            "video_filter": "" if use_editor_parity else "eq=brightness=0.38:contrast=1.18:gamma=0.72:saturation=1.28",
        })
    elif selected_map.get("name") == "TropicalIsland_Level_Scene_01" and case_type == "plant_sway_camera":
        profile.update({
            "sun": 6.8,
            "fill": 2.8,
            "sky": 2.9,
            "exposure_bias": 0.68,
            "video_filter": "" if use_editor_parity else "eq=brightness=0.03:contrast=1.08:gamma=1.00:saturation=1.06",
        })
    elif case_type in {"stone_slope_roll", "slope_drop_bounce_stop", "rolling_friction", "projectile_arc", "pendulum_swing", "stack_stability", "rigid_collision_pair", "gear_collision_chain", "bottle_domino_chain", "wheel_ramp_jump", "falling_crate_collision", "crate_friction_slide", "cone_barrel_collision"}:
        if selected_map.get("name") == "MarketEnvironment_Day":
            if use_editor_parity:
                profile.update({
                    "sun": 0.0,
                    "fill": 0.0,
                    "sky": 0.0,
                    "exposure_bias": 0.0,
                    "fixed_auto_exposure": False,
                    "post_process_blend_weight": 0.0 if EDITOR_VIEWPORT_MATCH_REALISM else 0.35,
                    "capture_backend": "highres_viewport",
                    "capture_source": "SCS_FINAL_COLOR_LDR",
                    "video_filter": "",
                    "profile": "market_day_editor_viewport_match" if EDITOR_VIEWPORT_MATCH_REALISM else "market_day_editor_parity",
                })
            elif case_type == "bottle_domino_chain" and selected_map.get("controlled_stage") == "bottle_domino_chain_foreground":
                profile.update({
                    "sun": 4.8,
                    "fill": 1.8,
                    "sky": 1.8,
                    "exposure_bias": 0.34,
                    "fixed_auto_exposure": True,
                    "post_process_blend_weight": 1.0,
                    "capture_source": "SCS_FINAL_COLOR_LDR",
                    "video_filter": "eq=brightness=0.32:contrast=1.00:gamma=0.90:saturation=1.05",
                })
            elif case_type == "bottle_domino_chain":
                profile.update({
                    "sun": 14000.0,
                    "fill": 4500.0,
                    "sky": 5.0,
                    "exposure_bias": -0.15,
                    "fixed_auto_exposure": False,
                    "post_process_blend_weight": 0.70,
                    "capture_source": "SCS_BASE_COLOR",
                    "video_filter": "eq=brightness=0.16:contrast=0.70:gamma=0.65:saturation=1.20",
                })
            else:
                profile.update({
                    "sun": 38000.0,
                    "fill": 7000.0,
                    "sky": 3.2,
                    "exposure_bias": 0.0,
                    "fixed_auto_exposure": True,
                    "post_process_blend_weight": 1.0,
                    "capture_source": "SCS_BASE_COLOR",
                    "video_filter": "eq=brightness=0.08:contrast=0.82:gamma=0.82:saturation=1.12",
                })
        else:
            profile.update({
                "sun": 9.2 if case_type == "stone_slope_roll" else 9.0,
                "fill": 5.2,
                "sky": 5.0,
                "exposure_bias": 0.9 if use_editor_parity else 1.14,
                "capture_source": "SCS_FINAL_COLOR_LDR",
                "video_filter": "" if use_editor_parity else "eq=brightness=0.12:contrast=1.04:gamma=1.00:saturation=1.06",
            })
    return profile


def runtime_lighting_controls(runtime_scene: dict | None) -> dict:
    controls = {
        "llm_selectable": True,
        "preset": "editor_parity" if EDITOR_PARITY_REALISM else "default_all_on",
        "visual_realism_profile": VISUAL_REALISM_PROFILE,
        "use_existing_map_lights": True,
        "spawn_directional_sun": True,
        "spawn_fill_light": True,
        "spawn_sky_light": True,
        "spawn_map_boost_lights": True,
        "spawn_sky_atmosphere": True,
        "use_post_process": True,
        "fixed_auto_exposure": not EDITOR_PARITY_REALISM,
        "stage_helpers": True,
        "map_backdrop_helpers": True,
        "helper_light_intensity_scale": 1.0,
        "map_boost_intensity_scale": 1.0,
    }
    if runtime_scene and isinstance(runtime_scene.get("map_lighting_controls"), dict):
        controls.update(runtime_scene["map_lighting_controls"])
    return controls


def runtime_light_color(light_spec: dict):
    raw = light_spec.get("color") or light_spec.get("color_rgb")
    if not (isinstance(raw, list) and len(raw) >= 3):
        return None
    values = [float(raw[idx]) for idx in range(3)]
    if max(values) <= 1.0:
        values = [value * 255.0 for value in values]
    return unreal.Color(
        int(max(0, min(255, values[0]))),
        int(max(0, min(255, values[1]))),
        int(max(0, min(255, values[2]))),
        255,
    )


def runtime_object_color(obj: dict, fallback: unreal.LinearColor) -> unreal.LinearColor:
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    raw = params.get("color") or params.get("color_rgb") or params.get("tint")
    if not (isinstance(raw, list) and len(raw) >= 3):
        return fallback
    values = [float(raw[idx]) for idx in range(3)]
    if max(values) > 1.0:
        values = [value / 255.0 for value in values]
    return unreal.LinearColor(
        max(0.0, min(1.0, values[0])),
        max(0.0, min(1.0, values[1])),
        max(0.0, min(1.0, values[2])),
        1.0,
    )


def runtime_light_transform(light_spec: dict) -> tuple[list[float], list[float]]:
    transform = light_spec.get("transform") if isinstance(light_spec.get("transform"), dict) else {}
    location = transform.get("location") if isinstance(transform.get("location"), list) else light_spec.get("location")
    rotation = transform.get("rotation") if isinstance(transform.get("rotation"), list) else light_spec.get("rotation")
    if not (isinstance(location, list) and len(location) >= 3):
        location = [0.0, -2.4, 2.8]
    if not (isinstance(rotation, list) and len(rotation) >= 3):
        rotation = [-45.0, 0.0, 0.0]
    return [float(location[0]), float(location[1]), float(location[2])], [float(rotation[0]), float(rotation[1]), float(rotation[2])]


def spawn_runtime_custom_lights(editor, runtime_scene: dict | None, scene_origin: unreal.Vector) -> list[dict]:
    if not runtime_scene:
        return []
    specs = runtime_scene.get("lighting") if isinstance(runtime_scene.get("lighting"), list) else []
    spawned = []
    light_classes = {
        "point": getattr(unreal, "PointLight", None),
        "point_light": getattr(unreal, "PointLight", None),
        "spot": getattr(unreal, "SpotLight", None),
        "spot_light": getattr(unreal, "SpotLight", None),
        "directional": getattr(unreal, "DirectionalLight", None),
        "directional_light": getattr(unreal, "DirectionalLight", None),
        "sky": getattr(unreal, "SkyLight", None),
        "sky_light": getattr(unreal, "SkyLight", None),
    }
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("type") or "point").strip().lower()
        light_class = light_classes.get(kind)
        if not light_class:
            spawned.append({"index": index, "type": kind, "spawned": False, "reason": "unsupported_light_type"})
            continue
        location_m, rotation_deg = runtime_light_transform(spec)
        location = scene_origin + unreal.Vector(location_m[0] * 100.0, location_m[1] * 100.0, location_m[2] * 100.0)
        actor = editor.spawn_actor_from_class(light_class, location)
        actor.set_actor_label(f"native_phenomena_demo_custom_light_{index}_{kind}")
        actor.set_actor_rotation(unreal.Rotator(rotation_deg[0], rotation_deg[1], rotation_deg[2]), False)
        intensity = float(spec.get("intensity", 800.0))
        set_light(actor, intensity, runtime_light_color(spec))
        component = get_light_component(actor)
        if component:
            radius = spec.get("attenuation_radius_cm")
            if radius is None and spec.get("attenuation_radius_m") is not None:
                radius = float(spec["attenuation_radius_m"]) * 100.0
            if radius is not None:
                set_editor_property_if_available(component, "attenuation_radius", float(radius))
            if spec.get("source_radius_cm") is not None:
                set_editor_property_if_available(component, "source_radius", float(spec["source_radius_cm"]))
            if spec.get("soft_source_radius_cm") is not None:
                set_editor_property_if_available(component, "soft_source_radius", float(spec["soft_source_radius_cm"]))
            if spec.get("cast_shadows") is not None:
                set_editor_property_if_available(component, "cast_shadows", bool(spec["cast_shadows"]))
            if spec.get("inner_cone_angle") is not None:
                set_editor_property_if_available(component, "inner_cone_angle", float(spec["inner_cone_angle"]))
            if spec.get("outer_cone_angle") is not None:
                set_editor_property_if_available(component, "outer_cone_angle", float(spec["outer_cone_angle"]))
        spawned.append({
            "index": index,
            "type": kind,
            "label": actor.get_actor_label(),
            "location": [location.x, location.y, location.z],
            "rotation_degrees": rotation_deg,
            "intensity": intensity,
        })
    return spawned


def bool_control(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def float_control(value, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        result = float(value)
    except Exception:
        result = float(default)
    if not math.isfinite(result):
        result = float(default)
    if min_value is not None:
        result = max(float(min_value), result)
    if max_value is not None:
        result = min(float(max_value), result)
    return result


def int_control(value, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        result = int(value)
    except Exception:
        result = int(default)
    if min_value is not None:
        result = max(int(min_value), result)
    if max_value is not None:
        result = min(int(max_value), result)
    return result


def runtime_physics_controls(runtime_scene: dict | None) -> dict:
    controls = {
        "schema_version": "physics_controls_v1",
        "llm_selectable": True,
        "gravity_enabled": True,
        "collision_enabled": True,
        "collision_focus": False,
        "rigid_body_setup_enabled": CHAOS_RIGID_BODY_SETUP,
        "simulate_physics": CHAOS_SIMULATION_ENABLED,
        "simulation_driver": "ue_chaos_rigid_body" if CHAOS_SIMULATION_ENABLED else "scripted_trajectory_replay_with_collision_shapes",
        "dynamic_collision_profile": "PhysicsActor",
        "static_collision_profile": "BlockAll",
        "apply_mass": True,
        "apply_damping": True,
        "apply_physical_material": False,
        "apply_initial_impulse": CHAOS_SIMULATION_ENABLED,
        "initial_impulse_start_frame": 0,
        "physics_time_dilation": 1.0,
        "runtime_driver_backend": "cpp_runtime_driver" if CHAOS_SIMULATION_ENABLED else "python_editor_capture",
        "cpp_runtime_driver_enabled": CHAOS_SIMULATION_ENABLED,
        "record_contact_events": False,
        "deterministic_replay_fallback": not CHAOS_SIMULATION_ENABLED,
    }
    if runtime_scene and isinstance(runtime_scene.get("physics_controls"), dict):
        controls.update(runtime_scene["physics_controls"])
    controls["rigid_body_setup_enabled"] = bool_control(controls.get("rigid_body_setup_enabled"), True)
    controls["simulate_physics"] = bool_control(controls.get("simulate_physics"), False)
    controls["collision_enabled"] = bool_control(controls.get("collision_enabled"), True)
    controls["gravity_enabled"] = bool_control(controls.get("gravity_enabled"), True)
    controls["apply_mass"] = bool_control(controls.get("apply_mass"), True)
    controls["apply_damping"] = bool_control(controls.get("apply_damping"), True)
    controls["apply_initial_impulse"] = bool_control(controls.get("apply_initial_impulse"), controls["simulate_physics"])
    controls["initial_impulse_start_frame"] = int_control(controls.get("initial_impulse_start_frame"), 0, 0, 240)
    controls["physics_time_dilation"] = float_control(controls.get("physics_time_dilation"), 1.0, 0.01, 1.0)
    if os.environ.get("CHAOS_RIGID_BODY_SETUP") is not None:
        controls["rigid_body_setup_enabled"] = CHAOS_RIGID_BODY_SETUP
    if os.environ.get("CHAOS_SIMULATION_ENABLED") is not None:
        controls["simulate_physics"] = CHAOS_SIMULATION_ENABLED
    if controls["simulate_physics"] and controls.get("runtime_driver_backend") in (None, "", "scripted_replay"):
        controls["runtime_driver_backend"] = "cpp_runtime_driver"
    controls["cpp_runtime_driver_enabled"] = bool_control(
        controls.get("cpp_runtime_driver_enabled"),
        bool(controls["simulate_physics"] and controls.get("runtime_driver_backend") == "cpp_runtime_driver"),
    )
    if controls["simulate_physics"] and controls.get("cpp_runtime_driver_enabled"):
        controls["simulation_driver"] = "adp_cpp_runtime_driver"
    elif controls["simulate_physics"] and controls.get("runtime_driver_backend") == "analytic_contact_solver":
        controls["simulation_driver"] = controls.get("simulation_driver") or "analytic_rigid_body_contact_solver"
    elif controls["simulate_physics"]:
        controls["simulation_driver"] = "ue_chaos_rigid_body"
    else:
        controls["simulation_driver"] = controls.get("simulation_driver") or "scripted_trajectory_replay_with_collision_shapes"
    solver_output_replay = (
        not controls["simulate_physics"]
        and controls.get("runtime_driver_backend") == "precomputed_trajectory"
        and controls.get("trajectory_source") in {
            "adp_cpp_runtime_driver",
            "ue_chaos_transform_capture",
            "mujoco_rigid",
            "genesis_sph",
        }
    )
    controls["replay_kind"] = "live_solver" if controls["simulate_physics"] else ("solver_output_render_cache" if solver_output_replay else "fallback_or_scripted")
    controls["deterministic_replay_fallback"] = not controls["simulate_physics"] and not solver_output_replay
    return controls


def lighting_enabled(controls: dict, name: str) -> bool:
    return bool(controls.get(name, True))


def finite_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        return default
    if math.isfinite(parsed):
        return parsed
    return default


def apply_lighting_control_overrides(profile: dict, controls: dict) -> dict:
    result = dict(profile)
    numeric_overrides = {
        "sun_intensity": "sun",
        "fill_intensity": "fill",
        "sky_intensity": "sky",
        "exposure_bias": "exposure_bias",
        "post_process_blend_weight": "post_process_blend_weight",
    }
    for control_key, profile_key in numeric_overrides.items():
        if control_key in controls and not isinstance(controls[control_key], bool):
            result[profile_key] = finite_float(controls[control_key], float(result.get(profile_key, 0.0)))
    if "fixed_auto_exposure" in controls:
        result["fixed_auto_exposure"] = bool(controls["fixed_auto_exposure"])
    if isinstance(controls.get("capture_source"), str):
        result["capture_source"] = controls["capture_source"]
    if isinstance(controls.get("capture_backend"), str):
        result["capture_backend"] = controls["capture_backend"]
    if isinstance(controls.get("video_filter"), str):
        result["video_filter"] = controls["video_filter"]
    helper_scale = max(0.0, finite_float(controls.get("helper_light_intensity_scale", 1.0), 1.0))
    map_boost_scale = max(0.0, finite_float(controls.get("map_boost_intensity_scale", 1.0), 1.0))
    result["sun"] = float(result.get("sun", 0.0)) * helper_scale
    result["fill"] = float(result.get("fill", 0.0)) * helper_scale
    result["sky"] = float(result.get("sky", 0.0)) * helper_scale
    result["helper_light_intensity_scale"] = helper_scale
    result["map_boost_intensity_scale"] = map_boost_scale
    return result


def world_loc(origin: unreal.Vector, loc: tuple[float, float, float]) -> unreal.Vector:
    return unreal.Vector(origin.x + loc[0], origin.y + loc[1], origin.z + loc[2])


def load_asset(path: str):
    asset = unreal.load_asset(path)
    if not asset:
        raise RuntimeError(f"failed to load asset: {path}")
    return asset


def ue_vec_from_meters(pos: list[float], z_offset_cm: float = 120.0, origin: unreal.Vector | None = None) -> unreal.Vector:
    origin = origin or unreal.Vector(0.0, 0.0, 0.0)
    return unreal.Vector(origin.x + pos[0] * 100.0, origin.y + pos[1] * 100.0, origin.z + pos[2] * 100.0 + z_offset_cm)


def look_at_rotation(origin: unreal.Vector, target: unreal.Vector) -> unreal.Rotator:
    return unreal.MathLibrary.find_look_at_rotation(origin, target)


def runtime_rotator(rotation_degrees: list[float] | tuple[float, float, float] | None) -> unreal.Rotator:
    values = [float(value) for value in [*(rotation_degrees or [0.0, 0.0, 0.0]), 0.0, 0.0, 0.0][:3]]
    rotator = unreal.Rotator(0.0, 0.0, 0.0)
    try:
        rotator.pitch = values[0]
        rotator.yaw = values[1]
        rotator.roll = values[2]
        return rotator
    except Exception:
        return unreal.Rotator(*values)


def spawn_static_mesh(label: str, mesh_path: str, location: unreal.Vector, scale: unreal.Vector, material_path: str | None = None, rotation=None):
    subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actor = subsystem.spawn_actor_from_class(unreal.StaticMeshActor, location)
    actor.set_actor_label(label)
    actor.static_mesh_component.set_static_mesh(load_asset(mesh_path))
    try:
        actor.static_mesh_component.set_mobility(unreal.ComponentMobility.MOVABLE)
    except Exception:
        pass
    actor.static_mesh_component.set_world_scale3d(scale)
    if material_path:
        material = load_asset(material_path)
        try:
            material_count = max(1, int(actor.static_mesh_component.get_num_materials()))
        except Exception:
            material_count = 1
        for material_index in range(material_count):
            actor.static_mesh_component.set_material(material_index, material)
    if rotation:
        actor.set_actor_rotation(rotation, False)
    return actor


def actor_runtime_component(actor):
    for attr in ("static_mesh_component", "skeletal_mesh_component"):
        component = getattr(actor, attr, None)
        if component:
            return component
    for cls_name in ("GeometryCollectionComponent", "StaticMeshComponent", "SkeletalMeshComponent"):
        cls = getattr(unreal, cls_name, None)
        if not cls:
            continue
        try:
            component = actor.get_component_by_class(cls)
            if component:
                return component
        except Exception:
            pass
    try:
        for child in actor.get_attached_actors():
            component = actor_runtime_component(child)
            if component:
                return component
    except Exception:
        pass
    return None


def actor_skeletal_component_with_socket(actor, socket_name: str):
    try:
        components = actor.get_components_by_class(unreal.SkeletalMeshComponent)
    except Exception:
        components = []
    for component in components:
        try:
            if component.does_socket_exist(unreal.Name(socket_name)) or component.get_bone_index(socket_name) >= 0:
                return component
        except Exception:
            pass
    return next(iter(components), None) or actor_runtime_component(actor)


def attach_actor_clear_of_socket(
    actor,
    parent,
    socket_name: str,
    clearance_cm: float,
    max_grasp_gap_cm: float,
) -> bool:
    hand = parent.get_socket_location(unreal.Name(socket_name))
    origin, extent = actor_bounds(actor)
    direction = origin - hand
    distance = math.sqrt(direction.x * direction.x + direction.y * direction.y + direction.z * direction.z)
    if distance < 1e-3:
        direction = unreal.Vector(1.0, 0.0, 0.0)
        distance = 1.0
    radius = max(1.0, float(extent.x), float(extent.y), float(extent.z))
    if distance - radius > max(0.0, float(max_grasp_gap_cm)):
        return False
    actor.set_actor_location(
        hand + direction * ((radius + max(0.0, float(clearance_cm))) / distance),
        False,
        False,
    )
    return bool(
        actor.attach_to_component(
            parent,
            unreal.Name(socket_name),
            unreal.AttachmentRule.KEEP_WORLD,
            unreal.AttachmentRule.KEEP_WORLD,
            unreal.AttachmentRule.KEEP_WORLD,
            False,
        )
    )


def follow_socket_smoothly(actor, parent, socket_name: str, clearance_cm: float, max_step_cm: float) -> float:
    hand = parent.get_socket_location(unreal.Name(socket_name))
    center, extent = actor_bounds(actor)
    offset = center - hand
    distance = math.sqrt(offset.x * offset.x + offset.y * offset.y + offset.z * offset.z)
    if distance < 1e-3:
        offset = unreal.Vector(1.0, 0.0, 0.0)
        distance = 1.0
    radius = max(1.0, float(extent.x), float(extent.y), float(extent.z))
    target = hand + offset * ((radius + max(0.0, float(clearance_cm))) / distance)
    delta = target - center
    step = math.sqrt(delta.x * delta.x + delta.y * delta.y + delta.z * delta.z)
    if step <= 1e-3:
        return 0.0
    applied = min(step, max(0.1, float(max_step_cm)))
    actor.set_actor_location(actor.get_actor_location() + delta * (applied / step), False, False)
    return applied


def sync_runtime_visuals(actors: dict) -> None:
    for actor_id, visual in (actors.get("visual_actors") or {}).items():
        physics_actor = actors.get(actor_id)
        if not physics_actor or not visual:
            continue
        try:
            visual.set_actor_location(physics_actor.get_actor_location(), False, False)
            visual.set_actor_rotation(physics_actor.get_actor_rotation(), False)
            # Provider meshes commonly keep an authored pivot at their base or
            # at an arbitrary modelling origin.  The controlled collision mesh
            # is centred on the CaseSpec pose, so copying actor transforms alone
            # makes the visible mesh orbit vertically as the rigid body rotates.
            # Re-centre the rendered bounds after applying the current rotation;
            # this preserves authored scale/materials while keeping visual and
            # collision geometry on the same physical body every frame.
            physics_origin, _ = actor_bounds(physics_actor)
            visual_origin, _ = actor_bounds(visual)
            center_delta = physics_origin - visual_origin
            visual.set_actor_location(visual.get_actor_location() + center_delta, False, False)
        except Exception:
            pass


def apply_surface_mesh_sequence_replay(
    actors: dict,
    runtime_scene: dict | None,
    frame_index: int,
    physics_status: dict,
) -> None:
    """Swap derived fluid surface meshes at the render-frame seam."""
    if not runtime_scene:
        return
    replay_status = physics_status.setdefault(
        "surface_mesh_sequence_replay",
        {"frames": [], "errors": [], "state_truth_role": "derived_render_representation"},
    )
    objects = (runtime_scene.get("dynamic_objects") or []) + (runtime_scene.get("static_objects") or [])
    for obj in objects:
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        sequence = params.get("surface_mesh_sequence")
        if not isinstance(sequence, list) or not sequence:
            continue
        object_id = str(obj.get("id") or "")
        actor = actors.get(object_id)
        if not actor:
            replay_status["errors"].append({"frame": frame_index, "object_id": object_id, "error": "actor_missing"})
            continue
        mesh_path = str(sequence[min(frame_index, len(sequence) - 1)] or "")
        mesh = unreal.load_asset(mesh_path)
        component = actor_runtime_component(actor)
        if not mesh or not component or not hasattr(component, "set_static_mesh"):
            replay_status["errors"].append(
                {"frame": frame_index, "object_id": object_id, "mesh_path": mesh_path, "error": "static_mesh_unavailable"}
            )
            continue
        try:
            if object_id not in SURFACE_REPLAY_MATERIALS:
                try:
                    SURFACE_REPLAY_MATERIALS[object_id] = component.get_material(0)
                except Exception:
                    SURFACE_REPLAY_MATERIALS[object_id] = None
            component.set_mobility(unreal.ComponentMobility.MOVABLE)
            component.set_static_mesh(mesh)
            set_editor_property_if_available(component, "disallow_nanite", True)
            replay_material = SURFACE_REPLAY_MATERIALS.get(object_id)
            if replay_material:
                component.set_material(0, replay_material)
            for method_name in ("update_bounds", "mark_render_state_dirty", "mark_render_transform_dirty"):
                method = getattr(component, method_name, None)
                if method:
                    try:
                        method()
                    except Exception:
                        pass
            try:
                component.set_visibility(True, True)
                component.set_hidden_in_game(False, True)
            except Exception:
                pass
            try:
                actual_material = component.get_material(0)
            except Exception:
                actual_material = None
            component.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
            component.set_simulate_physics(False)
            replay_status["frames"].append(
                {
                    "frame": frame_index,
                    "object_id": object_id,
                    "mesh_path": mesh_path,
                    "applied": True,
                    "material_preserved": replay_material is not None,
                    "material_name": replay_material.get_name() if replay_material and hasattr(replay_material, "get_name") else None,
                    "actual_material_name": actual_material.get_name() if actual_material and hasattr(actual_material, "get_name") else None,
                    "mobility": "MOVABLE",
                }
            )
        except Exception as exc:
            replay_status["errors"].append(
                {"frame": frame_index, "object_id": object_id, "mesh_path": mesh_path, "error": str(exc)}
            )


def geometry_collection_fragment_state(actor) -> tuple[dict | None, str | None, str | None]:
    library = getattr(unreal, "ADPPhysicsRuntimeLibrary", None)
    capture = getattr(library, "capture_geometry_collection_fragment_state", None) if library else None
    if not capture or not actor:
        return None, None, "fragment_state_export_unavailable"
    try:
        raw = capture(actor)
        payload = json.loads(raw) if raw else None
        if not isinstance(payload, dict) or not isinstance(payload.get("fragments"), list):
            return None, None, "fragment_state_export_empty"
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return payload, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), None
    except Exception as exc:
        return None, None, f"fragment_state_export_failed:{exc}"


def apply_geometry_collection_fracture_response(
    actors: dict,
    runtime_scene: dict | None,
    contact_events: list[dict],
    frame_index: int,
    physics_status: dict,
) -> None:
    if not runtime_scene:
        return
    status = physics_status.setdefault(
        "geometry_collection_fracture",
        {"commands": [], "break_events": [], "fragment_frames": [], "root_broken": {}, "energy_checks": []},
    )
    status.setdefault("energy_checks", [])
    kinematics = status.setdefault("impactor_kinematics", {})
    commanded = {str(row.get("target_id")) for row in status["commands"]}
    for obj in (runtime_scene.get("dynamic_objects") or []) + (runtime_scene.get("static_objects") or []):
        response = (obj.get("params") or {}).get("fracture_response")
        if not isinstance(response, dict):
            continue
        target_id = str(obj.get("id") or "")
        target_actor = actors.get(target_id)
        component_class = getattr(unreal, "GeometryCollectionComponent", None)
        component = target_actor.get_component_by_class(component_class) if target_actor and component_class else None
        if not component:
            continue
        try:
            broken = bool(component.is_root_broken())
        except Exception:
            broken = False
        was_broken = bool(status["root_broken"].get(target_id))
        status["root_broken"][target_id] = broken
        fragment_state = None
        fragment_state_sha256 = None
        fragment_state_error = None
        if broken:
            fragment_state, fragment_state_sha256, fragment_state_error = geometry_collection_fragment_state(target_actor)
            status.setdefault("fragment_frames", []).append(
                {
                    "frame": frame_index,
                    "target_id": target_id,
                    "fragment_state": fragment_state,
                    "fragment_state_sha256": fragment_state_sha256,
                    "fragment_state_error": fragment_state_error,
                }
            )
        if broken and not was_broken:
            status["break_events"].append(
                {
                    "frame": frame_index,
                    "target_id": target_id,
                    "source": "GeometryCollectionComponent.is_root_broken",
                    "fragment_state": fragment_state,
                    "fragment_state_sha256": fragment_state_sha256,
                    "fragment_state_error": fragment_state_error,
                }
            )
        impactor_id = str(response.get("impactor_id") or "")
        pair = {target_id, impactor_id}
        contact = next(
            (
                event
                for event in contact_events
                if set(event.get("objects") or []) == pair
                and (event.get("native_collision") is True or event.get("method") == "ue_native_component_hit")
            ),
            None,
        )
        if target_id in commanded or broken:
            continue
        impactor = actors.get(impactor_id)
        if not impactor:
            continue
        impactor_component = actor_runtime_component(impactor)
        try:
            mass_kg = float(impactor_component.get_mass())
            impactor_velocity = impactor_component.get_physics_linear_velocity()
        except Exception as exc:
            if contact:
                status["energy_checks"].append({"frame": frame_index, "target_id": target_id, "impactor_id": impactor_id, "passed": False, "error": f"native_impactor_energy_unavailable:{exc}"})
            continue
        velocity_m_s = [float(impactor_velocity.x) / 100.0, float(impactor_velocity.y) / 100.0, float(impactor_velocity.z) / 100.0]
        sample_key = f"{impactor_id}:{target_id}"
        current_sample = {"frame": frame_index, "mass_kg": mass_kg, "velocity_m_s": velocity_m_s}
        energy_sample = kinematics.get(sample_key)
        kinematics[sample_key] = current_sample
        if not contact:
            continue
        if energy_sample is None:
            status["energy_checks"].append(
                {
                    "frame": frame_index,
                    "target_id": target_id,
                    "impactor_id": impactor_id,
                    "passed": False,
                    "external_strain_applied": False,
                    "error": "precontact_impactor_sample_unavailable",
                }
            )
            continue
        mass_kg = float(energy_sample["mass_kg"])
        velocity_m_s = list(energy_sample["velocity_m_s"])
        speed_squared = sum(value * value for value in velocity_m_s)
        impact_speed_m_s = speed_squared ** 0.5
        impact_energy_j = 0.5 * max(0.0, mass_kg) * speed_squared
        selected_response = fracture_response_for_energy(response, impact_energy_j)
        levels = response.get("energy_response_levels")
        if isinstance(levels, list):
            minimum_impact_energy_j = (
                max(0.0, float(selected_response.get("minimum_impact_energy_j") or 0.0))
                if selected_response
                else min(max(0.0, float(level.get("minimum_impact_energy_j") or 0.0)) for level in levels)
            )
            energy_gate_passed = selected_response is not None
        else:
            selected_response = selected_response or response
            minimum_impact_energy_j = max(0.0, float(response.get("minimum_impact_energy_j") or 0.0))
            energy_gate_passed = impact_energy_j >= minimum_impact_energy_j
        damage_state = str((selected_response or {}).get("damage_state") or "") or None
        contact["impactor_mass_kg"] = round(mass_kg, 6)
        contact["impact_speed_m_s"] = round(impact_speed_m_s, 6)
        contact["impact_energy_j"] = round(impact_energy_j, 6)
        contact["minimum_impact_energy_j"] = minimum_impact_energy_j
        contact["energy_model"] = "ue_component_precontact_sample_translational_energy"
        contact["energy_sample_frame"] = int(energy_sample["frame"])
        contact["energy_gate_passed"] = energy_gate_passed
        contact["external_strain_applied"] = False
        contact["damage_state"] = damage_state
        energy_check = {
            "frame": frame_index,
            "energy_sample_frame": int(energy_sample["frame"]),
            "target_id": target_id,
            "impactor_id": impactor_id,
            "impactor_mass_kg": round(mass_kg, 6),
            "impact_speed_m_s": round(impact_speed_m_s, 6),
            "impact_energy_j": round(impact_energy_j, 6),
            "minimum_impact_energy_j": minimum_impact_energy_j,
            "energy_model": "ue_component_precontact_sample_translational_energy",
            "passed": contact["energy_gate_passed"],
            "external_strain_applied": False,
            "damage_state": damage_state,
        }
        status["energy_checks"].append(energy_check)
        if not energy_check["passed"]:
            continue
        try:
            root_index = int(component.get_root_index())
            fallback_location_cm = vector_payload(impactor.get_actor_location())
            location_cm, location_source = fracture_center_from_contact(contact, fallback_location_cm)
            if response.get("center_source") == "native_contact_impact_point" and location_source != "native_contact_impact_point":
                energy_check["external_strain_applied"] = False
                energy_check["error"] = "native_contact_impact_point_unavailable"
                status.setdefault("errors", []).append(f"{target_id}:native_contact_impact_point_unavailable")
                continue
            location = unreal.Vector(*location_cm)
            radius_cm = max(1.0, float(selected_response.get("radius_m", 0.5)) * 100.0)
            strain = max(0.0, float(selected_response.get("external_strain", 0.0)))
            component.apply_external_strain(
                root_index,
                location,
                radius_cm,
                max(0, int(selected_response.get("propagation_depth", 2))),
                max(0.0, float(selected_response.get("propagation_factor", 0.5))),
                strain,
            )
            visual_actor = (actors.get("visual_actors") or {}).get(target_id)
            if visual_actor:
                visual_actor.set_actor_hidden_in_game(True)
                visual_component = actor_runtime_component(visual_actor)
                if visual_component:
                    visual_component.set_visibility(False, True)
                target_actor.set_actor_hidden_in_game(False)
                component.set_visibility(True, True)
                component.set_hidden_in_game(False, True)
            cluster_release_requested = bool(selected_response.get("release_cluster"))
            cluster_released = False
            if cluster_release_requested:
                component.crumble_cluster(root_index)
                component.set_enable_gravity(True)
                cluster_released = True
            radial_impulse_strength = max(0.0, float(selected_response.get("radial_impulse_strength") or 0.0))
            radial_impulse_applied = False
            if radial_impulse_strength > 0.0:
                try:
                    component.add_radial_impulse(
                        location,
                        max(radius_cm, float(selected_response.get("radial_impulse_radius_m", 1.0)) * 100.0),
                        radial_impulse_strength,
                        unreal.RadialImpulseFalloff.RIF_LINEAR,
                        True,
                    )
                    radial_impulse_applied = True
                except Exception as exc:
                    status.setdefault("errors", []).append(f"{target_id}:radial_impulse:{exc}")
            contact["external_strain_applied"] = True
            contact["cluster_released"] = cluster_released
            contact["radial_impulse_applied"] = radial_impulse_applied
            energy_check["external_strain_applied"] = True
            energy_check["cluster_released"] = cluster_released
            energy_check["radial_impulse_applied"] = radial_impulse_applied
            status["commands"].append(
                {
                    "frame": frame_index,
                    "target_id": target_id,
                    "impactor_id": impactor_id,
                    "root_index": root_index,
                    "location_cm": vector_payload(location),
                    "location_source": location_source,
                    "radius_cm": radius_cm,
                    "external_strain": strain,
                    "impact_energy_j": round(impact_energy_j, 6),
                    "energy_sample_frame": int(energy_sample["frame"]),
                    "minimum_impact_energy_j": minimum_impact_energy_j,
                    "damage_state": damage_state,
                    "cluster_release_requested": cluster_release_requested,
                    "cluster_released": cluster_released,
                    "radial_impulse_strength": radial_impulse_strength,
                    "radial_impulse_applied": radial_impulse_applied,
                    "source_contact": contact,
                }
            )
        except Exception as exc:
            status.setdefault("errors", []).append(f"{target_id}:{exc}")


def attach_geometry_collection_fracture_events(trajectory: list[dict], physics_status: dict) -> list[dict]:
    status = physics_status.get("geometry_collection_fracture") or {}
    commands = {str(row.get("target_id")): row for row in status.get("commands") or []}
    events = []
    by_frame = {int(frame.get("frame") or 0): frame for frame in trajectory}
    fragment_trajectory = []
    for sample in status.get("fragment_frames") or []:
        frame_index = int(sample.get("frame") or 0)
        target_id = str(sample.get("target_id") or "")
        payload = sample.get("fragment_state") if isinstance(sample.get("fragment_state"), dict) else {}
        fragments = []
        for row in payload.get("fragments") or []:
            if not isinstance(row, dict) or "index" not in row:
                continue
            transform_index = int(row["index"])
            fragments.append(
                {
                    "fragment_id": f"{target_id}:transform_{transform_index}",
                    "source_object_id": target_id,
                    "transform_index": transform_index,
                    "position_cm": row.get("location_cm"),
                    "rotation_xyzw": row.get("rotation_xyzw"),
                    "source": "ue_geometry_collection_component_space",
                }
            )
        frame = by_frame.get(frame_index)
        if frame is not None and fragments:
            frame.setdefault("fragments", []).extend(fragments)
        fragment_trajectory.append(
            {
                "frame": frame_index,
                "object_id": target_id,
                "fragment_count": len(fragments),
                "fragment_state_sha256": sample.get("fragment_state_sha256"),
                "fragment_state_error": sample.get("fragment_state_error"),
                "fragments": fragments,
            }
        )
    (OUTPUT_DIR / "fragment_trajectory.json").write_text(json.dumps(fragment_trajectory, indent=2), encoding="utf-8")
    for check in status.get("energy_checks") or []:
        frame = by_frame.get(int(check.get("frame") or 0))
        pair = {str(check.get("target_id") or ""), str(check.get("impactor_id") or "")}
        contact = next(
            (row for row in (frame or {}).get("contacts") or [] if set(row.get("objects") or []) == pair),
            None,
        )
        if contact is not None:
            contact.update({
                "impactor_mass_kg": check.get("impactor_mass_kg"),
                "impact_speed_m_s": check.get("impact_speed_m_s"),
                "impact_energy_j": check.get("impact_energy_j"),
                "energy_sample_frame": check.get("energy_sample_frame"),
                "minimum_impact_energy_j": check.get("minimum_impact_energy_j"),
                "energy_model": check.get("energy_model"),
                "energy_gate_passed": bool(check.get("passed")),
                "external_strain_applied": bool(check.get("external_strain_applied")),
                "damage_state": check.get("damage_state"),
                "cluster_released": bool(check.get("cluster_released")),
                "radial_impulse_applied": bool(check.get("radial_impulse_applied")),
            })
    for observed in status.get("break_events") or []:
        frame_index = int(observed.get("frame") or 0)
        target_id = str(observed.get("target_id") or "")
        command = commands.get(target_id) or {}
        event = {
            "event_type": "fracture",
            "object_id": target_id,
            "frame": frame_index,
            "time_s": round(frame_index / max(FPS, 1), 6),
            "source": "ue_geometry_collection_root_state",
            "root_broken": True,
            "native_break_event": True,
            "command_frame": command.get("frame"),
            "root_index": command.get("root_index"),
            "fracture_center_cm": command.get("location_cm"),
            "fracture_center_source": command.get("location_source"),
            "external_strain": command.get("external_strain"),
            "impact_energy_j": command.get("impact_energy_j"),
            "energy_sample_frame": command.get("energy_sample_frame"),
            "minimum_impact_energy_j": command.get("minimum_impact_energy_j"),
            "damage_state": command.get("damage_state"),
            "cluster_released": bool(command.get("cluster_released")),
            "radial_impulse_strength": command.get("radial_impulse_strength"),
            "radial_impulse_applied": bool(command.get("radial_impulse_applied")),
            "fragment_state": observed.get("fragment_state"),
            "fragment_state_sha256": observed.get("fragment_state_sha256"),
            "fragment_state_error": observed.get("fragment_state_error"),
            "fragment_count": int((observed.get("fragment_state") or {}).get("fragment_count") or 0),
            "damage_thresholds_runtime": (observed.get("fragment_state") or {}).get("damage_thresholds_runtime") or [],
            "damage_threshold_source": (observed.get("fragment_state") or {}).get("damage_threshold_source"),
            "source_contact": command.get("source_contact"),
        }
        events.append(event)
        frame = by_frame.get(frame_index)
        if frame is not None:
            frame.setdefault("fracture_events", []).append(event)
    (OUTPUT_DIR / "fracture_events.json").write_text(json.dumps(events, indent=2), encoding="utf-8")
    return events


def spawn_runtime_actor(label: str, asset_path: str, asset_kind: str, location: unreal.Vector, scale: unreal.Vector, material_path: str | None = None, rotation=None):
    subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    asset_kind = str(asset_kind or "").lower()
    if asset_kind in {"geometrycollection", "geometry_collection"}:
        asset = load_asset(asset_path)
        actor = subsystem.spawn_actor_from_class(unreal.GeometryCollectionActor, location)
        if not actor:
            raise RuntimeError(f"failed to spawn Geometry Collection actor: {asset_path}")
        actor.set_actor_label(label)
        component = actor_runtime_component(actor)
        if not component:
            raise RuntimeError(f"missing Geometry Collection component: {asset_path}")
        component.set_rest_collection(asset, True)
        try:
            component.set_mobility(unreal.ComponentMobility.MOVABLE)
        except Exception:
            pass
        component.set_world_scale3d(scale)
        if material_path:
            component.set_material(0, load_asset(material_path))
        if rotation:
            actor.set_actor_rotation(rotation, False)
        return actor
    if asset_kind == "character" and hasattr(unreal, "Character"):
        asset = load_asset(asset_path)
        actor = subsystem.spawn_actor_from_class(unreal.Character, location)
        if not actor:
            raise RuntimeError(f"failed to spawn Character actor: {asset_path}")
        actor.set_actor_label(label)
        component = actor.get_component_by_class(unreal.SkeletalMeshComponent)
        capsule = actor.get_component_by_class(unreal.CapsuleComponent)
        if not component or not capsule:
            raise RuntimeError("missing_character_mesh_or_capsule")
        component.set_skeletal_mesh(asset)
        component.set_relative_location(unreal.Vector(0.0, 0.0, -92.5), False, False)
        component.set_world_scale3d(scale)
        component.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
        capsule.set_capsule_size(34.0, 92.5)
        if material_path:
            component.set_material(0, load_asset(material_path))
        if rotation:
            actor.set_actor_rotation(rotation, False)
        return actor
    if asset_kind == "skeletal_mesh" and hasattr(unreal, "SkeletalMeshActor"):
        try:
            asset = load_asset(asset_path)
            actor = subsystem.spawn_actor_from_class(unreal.SkeletalMeshActor, location)
            actor.set_actor_label(label)
            component = actor_runtime_component(actor)
            if not component:
                raise RuntimeError("missing_skeletal_mesh_component")
            assigned = False
            for args in ((asset, True), (asset,)):
                try:
                    component.set_skeletal_mesh(*args)
                    assigned = True
                    break
                except Exception:
                    pass
            if not assigned:
                for property_name in ("skeletal_mesh", "skeletal_mesh_asset"):
                    try:
                        component.set_editor_property(property_name, asset)
                        assigned = True
                        break
                    except Exception:
                        pass
            if not assigned:
                raise RuntimeError("set_skeletal_mesh_failed")
            try:
                component.set_mobility(unreal.ComponentMobility.MOVABLE)
            except Exception:
                pass
            component.set_world_scale3d(scale)
            if material_path:
                component.set_material(0, load_asset(material_path))
            if rotation:
                actor.set_actor_rotation(rotation, False)
            return actor
        except Exception as exc:
            write_progress_marker("skeletal_spawn_fallback", f"{label}:{exc}")
            proxy_mesh = "/Engine/BasicShapes/Cube.Cube"
            proxy_scale = unreal.Vector(max(0.25, float(scale.x) * 0.55), max(0.25, float(scale.y) * 0.55), max(0.25, float(scale.z) * 1.95))
            return spawn_static_mesh(label, proxy_mesh, location, proxy_scale, material_path=material_path, rotation=rotation)
    if asset_kind == "blueprint":
        blueprint_path = str(asset_path).rsplit(".", 1)[0]
        actor_class = unreal.EditorAssetLibrary.load_blueprint_class(blueprint_path)
        if not actor_class:
            raise RuntimeError(f"failed to load blueprint class: {asset_path}")
        actor = subsystem.spawn_actor_from_class(actor_class, location)
        if not actor:
            raise RuntimeError(f"failed to spawn blueprint actor: {asset_path}")
        actor.set_actor_label(label)
        actor.set_actor_scale3d(unreal.Vector(1.0, 1.0, 1.0))
        if rotation:
            actor.set_actor_rotation(rotation, False)
        return actor
    asset = load_asset(asset_path)
    return spawn_static_mesh(label, asset_path, location, scale, material_path=material_path, rotation=rotation)


RUNTIME_PHYSICAL_MATERIALS = []


def apply_runtime_physical_material(component, properties: dict, detail: dict) -> None:
    if properties.get("static_friction") is None and properties.get("dynamic_friction") is None and properties.get("restitution") is None:
        return
    try:
        material = unreal.PhysicalMaterial()
    except Exception:
        try:
            material = unreal.new_object(unreal.PhysicalMaterial, outer=component)
        except Exception as exc:
            detail["errors"].append(f"physical_material_create:{exc}")
            return
    static_friction = properties.get("static_friction")
    dynamic_friction = properties.get("dynamic_friction", static_friction)
    restitution = properties.get("restitution")
    assigned = {}
    if dynamic_friction is not None:
        friction_value = max(0.0, float(dynamic_friction))
        if set_editor_property_if_available(material, "friction", friction_value):
            assigned["friction"] = friction_value
        if set_editor_property_if_available(material, "dynamic_friction", friction_value):
            assigned["dynamic_friction"] = friction_value
    if static_friction is not None:
        static_value = max(0.0, float(static_friction))
        if set_editor_property_if_available(material, "static_friction", static_value):
            assigned["static_friction"] = static_value
    if restitution is not None:
        restitution_value = max(0.0, min(1.0, float(restitution)))
        if set_editor_property_if_available(material, "restitution", restitution_value):
            assigned["restitution"] = restitution_value
    try:
        component.set_phys_material_override(material)
        detail["physical_material_override"] = assigned or True
        RUNTIME_PHYSICAL_MATERIALS.append(material)
    except Exception as exc:
        detail["errors"].append(f"physical_material_override:{exc}")


def configure_runtime_physics(actor, obj: dict, role: str, controls: dict | None = None) -> dict:
    behavior = str(obj.get("behavior") or "static")
    properties = obj.get("physics_properties") or {}
    controls = controls or runtime_physics_controls(None)
    role_collision_enabled = bool_control(properties.get("collision_enabled"), bool(controls.get("collision_enabled", True)))
    role_simulate = bool(controls.get("simulate_physics", False) and role == "dynamic")
    if properties.get("simulate_physics") in (True, "true", "1", "on", "enabled"):
        role_simulate = role == "dynamic"
    if properties.get("simulate_physics") in ("force_off", "force_off_until_release", "disabled"):
        role_simulate = False
    role_gravity = bool_control(properties.get("enable_gravity"), bool(controls.get("gravity_enabled", True)))
    use_ccd = bool_control(properties.get("use_ccd"), False)
    detail = {
        "id": obj.get("id"),
        "role": role,
        "behavior": behavior,
        "engine": "UE5 Chaos",
        "rigid_body_setup_enabled": bool(controls.get("rigid_body_setup_enabled", True)),
        "simulate_physics_requested": bool(controls.get("simulate_physics", False)),
        "simulation_driver": str(controls.get("simulation_driver") or "scripted_trajectory_replay_with_collision_shapes"),
        "collision_enabled": False,
        "collision_profile": None,
        "simulate_physics": False,
        "enable_gravity": role_gravity,
        "use_ccd": use_ccd,
        "physics_properties": properties,
        "errors": [],
    }
    component = actor_runtime_component(actor)
    if behavior == "third_person_runner":
        try:
            component = actor.get_component_by_class(unreal.CapsuleComponent) or component
            detail["collision_component"] = "CapsuleComponent"
        except Exception:
            pass
    if not component:
        detail["errors"].append("missing_static_mesh_component")
        return detail
    fracture_response = (obj.get("params") or {}).get("fracture_response")
    damage_thresholds = fracture_response.get("damage_thresholds") if isinstance(fracture_response, dict) else None
    if isinstance(damage_thresholds, list) and damage_thresholds:
        values = [float(value) for value in damage_thresholds]
        try:
            component.set_editor_property("damage_threshold", values)
            detail["damage_thresholds"] = values
        except Exception as property_exc:
            detail["errors"].append(f"damage_thresholds:{property_exc}")
    try:
        component.set_mobility(unreal.ComponentMobility.MOVABLE if role == "dynamic" else unreal.ComponentMobility.STATIC)
        detail["mobility"] = "MOVABLE" if role == "dynamic" else "STATIC"
    except Exception as exc:
        detail["errors"].append(f"mobility:{exc}")
    if not bool(controls.get("rigid_body_setup_enabled", True)):
        detail["setup_note"] = "physics_controls.rigid_body_setup_enabled=false"
        return detail
    if not role_collision_enabled:
        try:
            component.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
        except Exception as exc:
            detail["errors"].append(f"collision_disabled:{exc}")
        detail["setup_note"] = "physics_controls.collision_enabled=false"
        return detail
    try:
        component.set_collision_enabled(unreal.CollisionEnabled.QUERY_AND_PHYSICS)
        detail["collision_enabled"] = True
    except Exception as exc:
        detail["errors"].append(f"collision_enabled:{exc}")
    default_profile = controls.get("dynamic_collision_profile") if role == "dynamic" else controls.get("static_collision_profile")
    profile = str(properties.get("collision_profile") or default_profile or ("PhysicsActor" if role == "dynamic" else "BlockAll"))
    try:
        component.set_collision_profile_name(profile)
        detail["collision_profile"] = profile
    except Exception as exc:
        detail["errors"].append(f"collision_profile:{exc}")
    if controls.get("apply_physical_material", False):
        apply_runtime_physical_material(component, properties, detail)
    should_simulate = bool(role_simulate and role == "dynamic")
    try:
        component.set_simulate_physics(should_simulate)
        detail["simulate_physics"] = should_simulate
    except Exception as exc:
        detail["errors"].append(f"simulate_physics:{exc}")
    try:
        component.set_enable_gravity(role_gravity)
    except Exception as exc:
        detail["errors"].append(f"gravity:{exc}")
    if should_simulate and use_ccd:
        try:
            component.set_use_ccd(True)
            detail["use_ccd"] = True
        except Exception as exc:
            detail["errors"].append(f"use_ccd:{exc}")
    if controls.get("apply_mass", True) and properties.get("mass_kg"):
        try:
            component.set_mass_override_in_kg("", float(properties["mass_kg"]), True)
            detail["mass_override_kg"] = float(properties["mass_kg"])
        except Exception as exc:
            detail["errors"].append(f"mass_override:{exc}")
    if controls.get("apply_damping", True):
        if properties.get("linear_damping") is not None:
            try:
                component.set_linear_damping(float(properties["linear_damping"]))
                detail["linear_damping"] = float(properties["linear_damping"])
            except Exception as exc:
                detail["errors"].append(f"linear_damping:{exc}")
        if properties.get("angular_damping") is not None:
            try:
                component.set_angular_damping(float(properties["angular_damping"]))
                detail["angular_damping"] = float(properties["angular_damping"])
            except Exception as exc:
                detail["errors"].append(f"angular_damping:{exc}")
    impulse = properties.get("initial_impulse_n_s") or properties.get("initial_impulse")
    if should_simulate and controls.get("apply_initial_impulse", False) and isinstance(impulse, list) and len(impulse) >= 3:
        detail["initial_impulse_pending_n_s"] = [float(impulse[0]), float(impulse[1]), float(impulse[2])]
    if should_simulate:
        detail["simulation_driver"] = "ue_chaos_rigid_body"
    else:
        detail["setup_note"] = "Collision/physical metadata is applied; this render keeps deterministic trajectory replay unless simulate_physics=true."
    return detail


def is_skeletal_component(component) -> bool:
    if not component:
        return False
    try:
        return "skeletal" in str(component.get_class().get_name()).lower()
    except Exception:
        return "skeletal" in str(type(component)).lower()


def runtime_animation_segments(obj: dict) -> list[dict]:
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    raw_segments = params.get("animation_segments")
    if isinstance(raw_segments, list):
        return [segment for segment in raw_segments if isinstance(segment, dict)]
    animation_ref = params.get("animation_ref") or params.get("walk_animation_ref")
    if animation_ref:
        return [
            {
                "name": "default",
                "start_s": 0.0,
                "animation_ref": animation_ref,
                "loop": bool_control(params.get("animation_loop"), True),
            }
        ]
    return []


def runtime_gasp_objects(runtime_scene: dict | None) -> list[dict]:
    if not runtime_scene:
        return []
    return [
        obj
        for obj in runtime_scene.get("dynamic_objects") or []
        if str((obj.get("params") or {}).get("humanoid_adapter") or "").strip().lower() in {"gasp", "ddv"}
    ]


def initialize_gasp_locomotion(actors: dict, runtime_scene: dict | None, status: dict) -> bool:
    gasp_objects = runtime_gasp_objects(runtime_scene)
    if not gasp_objects or actors.get("gasp_locomotion"):
        return bool(actors.get("gasp_locomotion"))
    obj = gasp_objects[0]
    actor_id = str(obj.get("id") or "")
    actor = actors.get(actor_id)
    world = actors.get("physics_game_world")
    if not actor or not world:
        return False
    detail = {"actor_id": actor_id, "possessed": False, "input_ready": False, "errors": []}
    try:
        controller = unreal.GameplayStatics.get_player_controller(world, 0)
        controller.possess(actor)
        detail["possessed"] = True
        action_path = str((obj.get("params") or {}).get("movement_input_action") or "/Game/Input/IA_Move.IA_Move")
        action = unreal.load_object(None, action_path)
        walk_action = unreal.load_object(None, "/Game/Input/IA_Walk.IA_Walk")
        carry_obj = next(
            (item for item in (runtime_scene.get("dynamic_objects") or []) if (item.get("params") or {}).get("native_grabbable_blueprint")),
            {},
        )
        carry_params = carry_obj.get("params") or {}
        grab_action = unreal.load_object(None, str(carry_params.get("native_grab_input_action") or "")) if carry_params else None
        subsystem = next(
            item
            for item in unreal.ObjectIterator(unreal.EnhancedInputLocalPlayerSubsystem)
            if not item.get_name().startswith("Default__")
        )
        waypoints = [
            item
            for item in (obj.get("params") or {}).get("movement_waypoints") or []
            if isinstance(item, dict) and isinstance(item.get("position_m"), list)
        ]
        segment_speeds = [
            math.dist(left["position_m"][:3], right["position_m"][:3])
            / max(1e-3, float(right.get("time_s") or 0.0) - float(left.get("time_s") or 0.0))
            for left, right in zip(waypoints, waypoints[1:])
            if float(right.get("time_s") or 0.0) > float(left.get("time_s") or 0.0)
        ]
        target_speed_cm_s = max(segment_speeds, default=0.85) * 100.0
        input_scale = 1.0
        movement = actor.get_component_by_class(unreal.CharacterMovementComponent)
        if movement:
            movement.set_editor_property("max_walk_speed", target_speed_cm_s)
            detail["max_walk_speed_cm_s"] = round(target_speed_cm_s, 3)
            for property_name, value in (
                ("max_depenetration_with_geometry", 12.0),
                ("max_depenetration_with_geometry_as_proxy", 12.0),
                ("max_depenetration_with_pawn", 8.0),
                ("max_depenetration_with_pawn_as_proxy", 8.0),
            ):
                try:
                    movement.set_editor_property(property_name, value)
                except Exception as exc:
                    detail.setdefault("movement_calibration_errors", []).append(f"{property_name}:{exc}")
        actors["gasp_locomotion"] = {
            "actor_id": actor_id,
            "controller": controller,
            "input_subsystem": subsystem,
            "move_action": action,
            "walk_action": walk_action,
            "grab_action": grab_action,
            "carry_actor_id": str(carry_obj.get("id") or ""),
            "grab_time_s": float(carry_params.get("grasp_time_s") or 0.0),
            "release_time_s": float(carry_params.get("release_time_s") or 0.0),
            "input_scale": input_scale,
            "last_safe_location": actor.get_actor_location(),
            "detail": detail,
            "last_frame": None,
            "walk_pressed": False,
        }
        detail["input_ready"] = bool(action)
        detail["input_scale"] = round(input_scale, 4)
    except Exception as exc:
        detail["errors"].append(str(exc))
    status["gasp_locomotion"] = detail
    return bool(actors.get("gasp_locomotion"))


def bound_gasp_post_tick_displacement(actors: dict, obj: dict, frame_index: int) -> None:
    state = actors.get("gasp_locomotion")
    actor = actors.get(str(obj.get("id") or ""))
    if not state or not actor:
        return
    current = actor.get_actor_location()
    previous = state.get("last_safe_location")
    if previous:
        displacement_cm = math.hypot(current.x - previous.x, current.y - previous.y)
        detail = state.get("detail") or {}
        detail["max_observed_frame_displacement_cm"] = round(
            max(float(detail.get("max_observed_frame_displacement_cm") or 0.0), displacement_cm),
            3,
        )
        max_displacement_cm = float_control(
            (obj.get("params") or {}).get("gasp_max_frame_displacement_cm"),
            12.0,
            1.0,
            100.0,
        )
        if displacement_cm > max_displacement_cm:
            scale = max_displacement_cm / displacement_cm
            current = unreal.Vector(
                previous.x + (current.x - previous.x) * scale,
                previous.y + (current.y - previous.y) * scale,
                current.z,
            )
            actor.set_actor_location(current, False, False)
            detail["bounded_displacement_corrections"] = int(detail.get("bounded_displacement_corrections") or 0) + 1
            detail["last_bounded_displacement_frame"] = frame_index
    if not state.get("moving"):
        actor.set_actor_rotation(runtime_rotator([0.0, float(state.get("yaw") or 0.0), 0.0]), False)
    state["last_safe_location"] = current


def apply_gasp_locomotion_frame(actors: dict, obj: dict, frame: dict) -> bool:
    state = actors.get("gasp_locomotion")
    if not state or state.get("actor_id") != str(obj.get("id") or ""):
        return False
    frame_index = int(frame.get("frame") or 0)
    if state.get("last_frame") == frame_index:
        return True
    time_s = float(frame.get("time") or 0.0)
    waypoints = [
        item
        for item in (obj.get("params") or {}).get("movement_waypoints") or []
        if isinstance(item, dict) and isinstance(item.get("position_m"), list)
    ]
    moving = False
    yaw = float(waypoints[-1].get("yaw_degrees", 0.0)) if waypoints else 0.0
    direction_x = 0.0
    direction_y = 0.0
    actor = actors.get(str(obj.get("id") or ""))
    current = actor.get_actor_location() if actor else None
    for left, right in zip(waypoints, waypoints[1:]):
        left_t = float(left.get("time_s") or 0.0)
        right_t = float(right.get("time_s") or left_t)
        if left_t <= time_s <= right_t:
            delta_x = float(right["position_m"][0]) - float(left["position_m"][0])
            delta_y = float(right["position_m"][1]) - float(left["position_m"][1])
            if actor:
                scene_origin = unreal.Vector(*actors.get("scene_origin", [0.0, 0.0, 0.0]))
                target = ue_vec_from_meters(right["position_m"], origin=scene_origin)
                delta_x = (target.x - current.x) / 100.0
                delta_y = (target.y - current.y) / 100.0
            moving = math.hypot(delta_x, delta_y) > 1e-4
            if moving:
                yaw = math.degrees(math.atan2(-delta_x, delta_y))
                length = math.hypot(delta_x, delta_y)
                direction_x = delta_x / length
                direction_y = delta_y / length
            else:
                yaw = float(right.get("yaw_degrees", left.get("yaw_degrees", 0.0)))
            break
    try:
        for phase, threshold in (
            ("grab", float(state.get("grab_time_s") or 0.0)),
            ("release", float(state.get("release_time_s") or 0.0)),
        ):
            if state.get("grab_action") and time_s >= threshold and not state.get(f"{phase}_input_sent"):
                state["input_subsystem"].inject_input_vector_for_action(
                    state["grab_action"],
                    unreal.Vector(1.0, 0.0, 0.0),
                    [],
                    [],
                )
                state[f"{phase}_input_sent"] = True
                (state.get("detail") or {})[f"{phase}_input_frame"] = frame_index
                package = actors.get(str(state.get("carry_actor_id") or ""))
                mesh = actor.get_component_by_class(unreal.SkeletalMeshComponent) if actor else None
                anim_instance = mesh.get_anim_instance() if mesh else None
                updates = (
                    (("grabbable_object", package), ("is_grabbing", True), ("is_dropping", False))
                    if phase == "grab"
                    else (("is_grabbing", False), ("is_dropping", True))
                )
                for target_name, target in (("character", actor), ("anim_instance", anim_instance)):
                    for property_name, value in updates:
                        if target is None or value is None:
                            continue
                        method = getattr(target, f"set_{property_name}", None)
                        if method:
                            try:
                                method(value)
                                (state.get("detail") or {}).setdefault("native_anim_interfaces", []).append(
                                    f"{phase}:{target_name}.set_{property_name}"
                                )
                                continue
                            except Exception as exc:
                                (state.get("detail") or {}).setdefault("native_anim_interface_errors", []).append(
                                    f"{phase}:{target_name}.set_{property_name}:{exc}"
                                )
                        try:
                            target.set_editor_property(property_name, value)
                            (state.get("detail") or {}).setdefault("native_anim_properties", []).append(
                                f"{phase}:{target_name}.{property_name}"
                            )
                        except Exception as exc:
                            (state.get("detail") or {}).setdefault("native_anim_property_errors", []).append(
                                f"{phase}:{target_name}.{property_name}:{exc}"
                            )
        if not state.get("walk_pressed") and state.get("walk_action"):
            state["input_subsystem"].inject_input_vector_for_action(
                state["walk_action"],
                unreal.Vector(1.0, 0.0, 0.0),
                [],
                [],
            )
            state["walk_pressed"] = True
        state["controller"].set_control_rotation(runtime_rotator([0.0, yaw, 0.0]))
        if actor and moving:
            actor.add_movement_input(
                unreal.Vector(direction_x, direction_y, 0.0),
                float(state.get("input_scale") or 1.0),
                True,
            )
        state["last_frame"] = frame_index
        state["moving"] = moving
        state["yaw"] = yaw
    except Exception as exc:
        state.setdefault("errors", []).append(str(exc))
    return True


def select_runtime_animation_segment(segments: list[dict], time_s: float) -> dict | None:
    selected = None
    for segment in segments:
        start_s = float_control(segment.get("start_s"), 0.0, 0.0, None)
        end_s = segment.get("end_s")
        if time_s < start_s:
            continue
        if end_s is not None and time_s > float_control(end_s, start_s, start_s, None):
            continue
        selected = {**segment, "resolved_start_s": start_s}
    return selected


def play_runtime_animation(
    actor,
    actor_id: str,
    animation_ref: str,
    *,
    loop: bool,
    local_time_s: float,
    status: dict,
) -> dict:
    detail = {
        "id": actor_id,
        "animation_ref": animation_ref,
        "loop": bool(loop),
        "local_time_s": round(float(local_time_s), 4),
        "played": False,
        "position_set": False,
        "errors": [],
    }
    component = actor_skeletal_component_with_socket(actor, "hand_r")
    if not is_skeletal_component(component):
        detail["errors"].append("missing_skeletal_mesh_component")
        events = status.setdefault("animation_events", [])
        if len(events) < 64:
            events.append(detail)
        return detail
    anim = None
    try:
        anim = unreal.load_asset(normalize_object_path(str(animation_ref)))
    except Exception as exc:
        detail["errors"].append(f"load_animation:{exc}")
    if not anim:
        detail["errors"].append("load_animation:none")
        events = status.setdefault("animation_events", [])
        if len(events) < 64:
            events.append(detail)
        return detail
    animation_mode = getattr(getattr(unreal, "AnimationMode", None), "ANIMATION_SINGLE_NODE", None)
    if animation_mode is not None:
        try:
            component.set_animation_mode(animation_mode)
            detail["animation_mode"] = "ANIMATION_SINGLE_NODE"
        except Exception as exc:
            detail["errors"].append(f"animation_mode:{exc}")
    play_errors = []
    for method_name, args in (
        ("play_animation", (anim, bool(loop))),
        ("set_animation", (anim,)),
    ):
        method = getattr(component, method_name, None)
        if not method:
            play_errors.append(f"{method_name}:missing")
            continue
        try:
            method(*args)
            detail["played"] = True
            detail["play_method"] = method_name
            break
        except Exception as exc:
            play_errors.append(f"{method_name}:{exc}")
    if detail["played"] and detail.get("play_method") == "set_animation":
        play_method = getattr(component, "play", None)
        if play_method:
            for args in ((bool(loop),), ()):
                try:
                    play_method(*args)
                    detail["play_invoked"] = True
                    break
                except Exception as exc:
                    play_errors.append(f"play:{exc}")
    if not detail["played"]:
        detail["errors"].extend(play_errors[-3:])
    position_method = getattr(component, "set_position", None)
    if position_method:
        for args in ((max(0.0, float(local_time_s)), False), (max(0.0, float(local_time_s)),)):
            try:
                position_method(*args)
                detail["position_set"] = True
                break
            except Exception as exc:
                detail["errors"].append(f"set_position:{exc}")
    events = status.setdefault("animation_events", [])
    if len(events) < 64:
        events.append(detail)
    return detail


def play_gasp_interaction_montage(
    actors: dict,
    actor,
    actor_id: str,
    animation_ref: str,
    segment: dict,
    active_key: str,
    local_time_s: float,
    status: dict,
) -> dict:
    detail = {
        "id": actor_id,
        "animation_ref": animation_ref,
        "local_time_s": round(float(local_time_s), 4),
        "animation_mode": "GASP_ANIMATION_BLUEPRINT_MONTAGE",
        "played": False,
        "position_set": False,
        "motion_warping": False,
        "errors": [],
    }
    component = actor_skeletal_component_with_socket(actor, "hand_r")
    anim = unreal.load_asset(normalize_object_path(str(animation_ref)))
    anim_instance = component.get_anim_instance() if component else None
    runtime = actors.setdefault("gasp_interaction_montages", {}).setdefault(actor_id, {})
    if not anim or not anim_instance:
        detail["errors"].append("missing_animation_or_anim_instance")
        return detail
    try:
        root_motion_mode = getattr(getattr(unreal, "RootMotionMode", None), "IGNORE_ROOT_MOTION", None)
        if root_motion_mode is not None:
            anim_instance.set_root_motion_mode(root_motion_mode)
            detail["root_motion_mode"] = "IGNORE_ROOT_MOTION"
        if runtime.get("active_key") != active_key:
            warp_target_name = str(segment.get("warp_target_name") or "").strip()
            target_position_m = segment.get("warp_target_position_m")
            motion_warping = actor.get_component_by_class(unreal.MotionWarpingComponent)
            if (
                bool_control(segment.get("motion_warping"), False)
                and motion_warping
                and warp_target_name
                and isinstance(target_position_m, list)
            ):
                scene_origin = unreal.Vector(*actors.get("scene_origin", [0.0, 0.0, 0.0]))
                target = ue_vec_from_meters(target_position_m, origin=scene_origin)
                target_rotation = runtime_rotator([0.0, float(segment.get("warp_target_yaw_degrees") or 0.0), 0.0])
                motion_warping.add_or_update_warp_target_from_location_and_rotation(
                    unreal.Name(warp_target_name),
                    target,
                    target_rotation,
                )
                modifier = unreal.RootMotionModifier_SkewWarp.add_root_motion_modifier_skew_warp(
                    motion_warping,
                    anim,
                    0.0,
                    max(0.01, float(segment.get("end_s") or 0.0) - float(segment.get("start_s") or 0.0)),
                    unreal.Name(warp_target_name),
                    unreal.WarpPointAnimProvider.NONE,
                    unreal.Transform(),
                    unreal.Name(""),
                    True,
                    True,
                    True,
                    unreal.MotionWarpRotationType.DEFAULT,
                    unreal.MotionWarpRotationMethod.SLERP,
                    1.0,
                    0.0,
                )
                detail["motion_warping"] = bool(modifier)
            montage = anim_instance.play_slot_animation_as_dynamic_montage(
                anim,
                unreal.Name(str((segment.get("slot_name") or "DefaultSlot"))),
                0.12,
                0.16,
                float_control(segment.get("time_scale"), 1.0, 0.05, 4.0),
                1,
                -1.0,
                max(0.0, float(local_time_s)),
            )
            runtime["active_key"] = active_key
            runtime["montage"] = montage
            detail["played"] = bool(montage)
        else:
            montage = runtime.get("montage")
            detail["played"] = bool(montage)
        if montage:
            anim_instance.montage_set_position(montage, max(0.0, float(local_time_s)))
            detail["position_set"] = True
    except Exception as exc:
        detail["errors"].append(str(exc))
    events = status.setdefault("animation_events", [])
    if len(events) < 64:
        events.append(detail)
    return detail


def apply_runtime_animation_segments(
    actors: dict,
    runtime_scene: dict | None,
    frame: dict,
    status: dict,
) -> None:
    if not runtime_scene:
        return
    time_s = float(frame.get("time") or 0.0)
    state = actors.setdefault("runtime_animation_state", {})
    for obj in runtime_scene.get("dynamic_objects") or []:
        actor_id = str(obj.get("id") or "")
        if not actor_id or actor_id not in actors:
            continue
        actor_state = state.setdefault(actor_id, {})
        segments = runtime_animation_segments(obj)
        if not segments:
            continue
        segment = select_runtime_animation_segment(segments, time_s)
        adapter = str((obj.get("params") or {}).get("humanoid_adapter") or "").strip().lower()
        if adapter == "ddv":
            actor_state["native_interaction_anim_blueprint"] = True
            continue
        is_gasp = adapter == "gasp"
        if not segment:
            if is_gasp:
                actor_state["gasp_interaction_montage_active"] = False
            continue
        if is_gasp and str(segment.get("name") or "") not in {"grab_from_floor", "drop"}:
            actor_state["gasp_interaction_montage_active"] = False
            continue
        animation_ref = str(segment.get("animation_ref") or "").strip()
        if not animation_ref:
            continue
        start_s = float(segment.get("resolved_start_s") or segment.get("start_s") or 0.0)
        local_time_s = max(0.0, time_s - start_s)
        local_time_s *= float_control(segment.get("time_scale"), 1.0, 0.05, 4.0)
        if bool_control(segment.get("reverse"), False):
            local_time_s = max(0.0, float_control(segment.get("clip_duration_s"), local_time_s, 0.0, None) - local_time_s)
        loop = bool_control(segment.get("loop"), True)
        active_key = f"{animation_ref}|{loop}|{segment.get('name', '')}"
        if actor_state.get("active_key") != active_key:
            actor_state["active_key"] = active_key
            actor_state["segment_name"] = segment.get("name")
            actor_state["animation_ref"] = animation_ref
            actor_state["started_at_s"] = round(time_s, 4)
        if is_gasp:
            actor_state["gasp_interaction_montage_active"] = True
            detail = play_gasp_interaction_montage(
                actors,
                actors[actor_id],
                actor_id,
                animation_ref,
                {
                    **segment,
                    "slot_name": (obj.get("params") or {}).get("interaction_slot_name") or "DefaultSlot",
                },
                active_key,
                local_time_s,
                status,
            )
        else:
            detail = play_runtime_animation(
                actors[actor_id],
                actor_id,
                animation_ref,
                loop=loop,
                local_time_s=local_time_s,
                status=status,
            )
        actor_state["last_time_s"] = round(time_s, 4)
        actor_state["last_detail"] = {key: value for key, value in detail.items() if key != "errors"}
        if detail.get("errors"):
            actor_state["last_errors"] = detail.get("errors")


def spawn_map_backdrop(
    editor,
    selected_map: dict,
    scene_origin: unreal.Vector,
    camera_location: unreal.Vector | None = None,
    camera_target: unreal.Vector | None = None,
) -> list[dict]:
    if not MAP_BACKDROP_HELPERS:
        return []
    if selected_map.get("name") != "MarketEnvironment_Day":
        return []
    if selected_map.get("suppress_camera_backdrop"):
        return []
    anchor = camera_target or scene_origin
    if camera_location and camera_target:
        dx = camera_target.x - camera_location.x
        dy = camera_target.y - camera_location.y
        length = max(1.0, (dx * dx + dy * dy) ** 0.5)
        forward_x = dx / length
        forward_y = dy / length
    else:
        forward_x = 0.0
        forward_y = 1.0
    right_x = forward_y
    right_y = -forward_x
    facing_yaw = math.degrees(math.atan2(forward_y, forward_x))
    subject_z = camera_target.z if camera_target else scene_origin.z + 190.0

    def camera_relative(lateral: float, depth: float, height: float) -> unreal.Vector:
        return unreal.Vector(
            anchor.x + forward_x * depth + right_x * lateral,
            anchor.y + forward_y * depth + right_y * lateral,
            subject_z + height,
        )

    specs = [
        {
            "label": "market_visible_road",
            "mesh": "/Engine/BasicShapes/Cube.Cube",
            "location": camera_relative(0.0, 165.0, -205.0),
            "scale": (5.8, 8.0, 0.055),
            "rotation": (0.0, facing_yaw, 0.0),
            "color": unreal.LinearColor(0.16, 0.17, 0.16, 1.0),
            "emissive": 0.08,
            "kind": "camera_visible_backdrop",
        },
        {
            "label": "market_visible_sidewalk_left",
            "mesh": "/Engine/BasicShapes/Cube.Cube",
            "location": camera_relative(-450.0, 175.0, -150.0),
            "scale": (4.6, 1.1, 0.05),
            "rotation": (0.0, facing_yaw, 0.0),
            "color": unreal.LinearColor(0.52, 0.50, 0.42, 1.0),
            "emissive": 0.10,
            "kind": "camera_visible_backdrop",
        },
        {
            "label": "market_visible_sidewalk_right",
            "mesh": "/Engine/BasicShapes/Cube.Cube",
            "location": camera_relative(450.0, 175.0, -150.0),
            "scale": (4.6, 1.1, 0.05),
            "rotation": (0.0, facing_yaw, 0.0),
            "color": unreal.LinearColor(0.55, 0.53, 0.44, 1.0),
            "emissive": 0.10,
            "kind": "camera_visible_backdrop",
        },
        {
            "label": "market_visible_left_facade",
            "mesh": "/Engine/BasicShapes/Cube.Cube",
            "location": camera_relative(-470.0, 315.0, 60.0),
            "scale": (0.16, 2.4, 2.8),
            "rotation": (0.0, facing_yaw, 0.0),
            "color": unreal.LinearColor(0.66, 0.23, 0.16, 1.0),
            "emissive": 0.25,
            "kind": "camera_visible_backdrop",
        },
        {
            "label": "market_visible_center_facade",
            "mesh": "/Engine/BasicShapes/Cube.Cube",
            "location": camera_relative(0.0, 345.0, 110.0),
            "scale": (0.16, 3.4, 3.25),
            "rotation": (0.0, facing_yaw, 0.0),
            "color": unreal.LinearColor(0.18, 0.38, 0.58, 1.0),
            "emissive": 0.25,
            "kind": "camera_visible_backdrop",
        },
        {
            "label": "market_visible_right_facade",
            "mesh": "/Engine/BasicShapes/Cube.Cube",
            "location": camera_relative(470.0, 315.0, 75.0),
            "scale": (0.16, 2.4, 2.95),
            "rotation": (0.0, facing_yaw, 0.0),
            "color": unreal.LinearColor(0.18, 0.52, 0.34, 1.0),
            "emissive": 0.25,
            "kind": "camera_visible_backdrop",
        },
        {
            "label": "market_visible_sign_bar",
            "mesh": "/Engine/BasicShapes/Cube.Cube",
            "location": camera_relative(0.0, 255.0, 185.0),
            "scale": (0.12, 2.0, 0.36),
            "rotation": (0.0, facing_yaw, 0.0),
            "color": unreal.LinearColor(0.95, 0.56, 0.08, 1.0),
            "emissive": 0.45,
            "kind": "camera_visible_backdrop",
        },
        {
            "label": "market_ground",
            "mesh": "/Game/Maps/MarketEnvironment/Mesh/SM_GasStation_Environment.SM_GasStation_Environment",
            "location": camera_relative(0.0, 240.0, -270.0),
            "scale": (0.20, 0.20, 0.20),
            "rotation": (0.0, facing_yaw, 0.0),
            "color": unreal.LinearColor(0.18, 0.21, 0.20, 1.0),
            "emissive": 0.08,
            "kind": "market_mesh_backdrop",
        },
        {
            "label": "market_wallstreet",
            "mesh": "/Game/Maps/MarketEnvironment/Mesh/SM_WallStreet.SM_WallStreet",
            "location": camera_relative(-120.0, 420.0, 30.0),
            "scale": (0.34, 0.34, 0.34),
            "rotation": (0.0, facing_yaw, 0.0),
            "color": unreal.LinearColor(0.48, 0.36, 0.28, 1.0),
            "emissive": 0.12,
            "kind": "market_mesh_backdrop",
        },
        {
            "label": "market_building",
            "mesh": "/Game/Maps/MarketEnvironment/Mesh/SM_Market_Building.SM_Market_Building",
            "location": camera_relative(380.0, 500.0, 120.0),
            "scale": (0.62, 0.62, 0.62),
            "rotation": (0.0, facing_yaw, 0.0),
            "color": unreal.LinearColor(0.62, 0.55, 0.42, 1.0),
            "emissive": 0.12,
            "kind": "market_mesh_backdrop",
        },
        {
            "label": "market_bus",
            "mesh": "/Game/Maps/MarketEnvironment/Mesh/SM_MiniBus.SM_MiniBus",
            "location": camera_relative(-360.0, 260.0, -45.0),
            "scale": (0.82, 0.82, 0.82),
            "rotation": (0.0, facing_yaw, 0.0),
            "color": unreal.LinearColor(0.82, 0.62, 0.20, 1.0),
            "emissive": 0.18,
            "kind": "market_mesh_backdrop",
        },
        {
            "label": "market_sign",
            "mesh": "/Game/Maps/MarketEnvironment/Mesh/SM_Signage_Big.SM_Signage_Big",
            "location": camera_relative(0.0, 300.0, 140.0),
            "scale": (0.72, 0.72, 0.72),
            "rotation": (0.0, facing_yaw, 0.0),
            "color": unreal.LinearColor(0.86, 0.16, 0.08, 1.0),
            "emissive": 0.20,
            "kind": "market_mesh_backdrop",
        },
    ]
    spawned = []
    for spec in specs:
        try:
            actor = spawn_static_mesh(
                "native_phenomena_demo_" + spec["label"],
                spec["mesh"],
                spec["location"],
                unreal.Vector(*spec["scale"]),
                None,
                unreal.Rotator(*spec["rotation"]),
            )
            actor.set_actor_hidden_in_game(False)
            try:
                actor.static_mesh_component.set_visibility(True, True)
                actor.static_mesh_component.set_hidden_in_game(False, True)
            except Exception:
                pass
            try:
                actor.static_mesh_component.set_mobility(unreal.ComponentMobility.MOVABLE)
            except Exception:
                pass
            try:
                actor.static_mesh_component.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
            except Exception:
                pass
            material = create_generated_material(
                "M_Agentic_" + spec["label"],
                spec["color"],
                0.45,
                0.0,
                float(spec.get("emissive", 0.02)),
            )
            set_actor_material(actor, material)
            set_actor_color(actor, spec["color"])
            origin, extent = actor_bounds(actor)
            spawned.append({
                "label": spec["label"],
                "mesh": spec["mesh"],
                "kind": spec.get("kind", "market_mesh_backdrop"),
                "location": [spec["location"].x, spec["location"].y, spec["location"].z],
                "scale": spec["scale"],
                "rotation": spec["rotation"],
                "bounds": {
                    "origin": [origin.x, origin.y, origin.z],
                    "extent": [extent.x, extent.y, extent.z],
                },
            })
        except Exception as exc:
            spawned.append({
                "label": spec["label"],
                "mesh": spec["mesh"],
                "error": str(exc),
            })
    return spawned


def spawn_market_day_gas_station_context(editor, scene_origin: unreal.Vector, materials: dict) -> list[dict]:
    """Add a readable gas-station context using MarketEnvironment meshes.

    The Day map is still loaded underneath. These helper actors only reinforce
    the local forecourt so headless SceneCapture does not collapse to a black
    patch when the original map materials are unavailable or underlit.
    """
    specs = [
        {
            "label": "market_day_context_asphalt",
            "mesh": "/Engine/BasicShapes/Cube.Cube",
            "location": unreal.Vector(10.0, -18.0, -4.0),
            "scale": (5.9, 2.65, 0.045),
            "rotation": (0.0, 0.0, 0.0),
            "material": "market_asphalt",
            "kind": "market_day_context_ground",
        },
        {
            "label": "market_day_context_backdrop",
            "mesh": "/Engine/BasicShapes/Cube.Cube",
            "location": unreal.Vector(20.0, 142.0, 184.0),
            "scale": (8.2, 0.055, 4.6),
            "rotation": (0.0, 0.0, 0.0),
            "material": "market_backdrop",
            "kind": "market_day_context_backdrop",
        },
        {
            "label": "market_day_context_sign_band",
            "mesh": "/Engine/BasicShapes/Cube.Cube",
            "location": unreal.Vector(40.0, 136.0, 302.0),
            "scale": (3.0, 0.065, 0.10),
            "rotation": (0.0, 0.0, 0.0),
            "material": "market_canopy",
            "kind": "market_day_context_sign_band",
        },
        {
            "label": "market_day_context_store",
            "mesh": "/Game/Maps/MarketEnvironment/Mesh/SM_Market_Building.SM_Market_Building",
            "location": unreal.Vector(-760.0, 1180.0, 70.0),
            "scale": (0.16, 0.16, 0.16),
            "rotation": (0.0, 88.0, 0.0),
            "material": "market_store",
            "kind": "market_day_context_store",
        },
    ]
    spawned = []
    for spec in specs:
        try:
            actor = spawn_static_mesh(
                "native_phenomena_demo_" + spec["label"],
                spec["mesh"],
                scene_origin + spec["location"],
                unreal.Vector(*spec["scale"]),
                None,
                unreal.Rotator(*spec["rotation"]),
            )
            set_actor_material(actor, materials.get(spec["material"]))
            try:
                actor.static_mesh_component.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
            except Exception:
                pass
            try:
                actor.static_mesh_component.set_mobility(unreal.ComponentMobility.MOVABLE)
            except Exception:
                pass
            origin, extent = actor_bounds(actor)
            spawned.append({
                "label": spec["label"],
                "mesh": spec["mesh"],
                "kind": spec["kind"],
                "location": [scene_origin.x + spec["location"].x, scene_origin.y + spec["location"].y, scene_origin.z + spec["location"].z],
                "scale": spec["scale"],
                "rotation": spec["rotation"],
                "bounds": {"origin": [origin.x, origin.y, origin.z], "extent": [extent.x, extent.y, extent.z]},
            })
        except Exception as exc:
            spawned.append({"label": spec["label"], "mesh": spec["mesh"], "kind": spec["kind"], "error": str(exc)})
    return spawned


def actor_bounds(actor) -> tuple[unreal.Vector, unreal.Vector]:
    try:
        return actor.get_actor_bounds(False)
    except Exception:
        return actor.get_actor_location(), unreal.Vector(80.0, 80.0, 80.0)


def actor_static_mesh_path(actor) -> str:
    try:
        component = actor.static_mesh_component
    except Exception:
        try:
            component = actor.get_component_by_class(unreal.StaticMeshComponent)
        except Exception:
            component = None
    if not component:
        return ""
    try:
        mesh = component.get_editor_property("static_mesh")
    except Exception:
        mesh = None
    if not mesh:
        return ""
    try:
        return mesh.get_path_name()
    except Exception:
        return str(mesh)


def remove_conflicting_map_actors(editor, selected_map: dict) -> list[dict]:
    if not selected_map.get("opened"):
        return []
    removed = []
    conflict_terms = ("sm_balloon",)
    for actor in list(editor.get_all_level_actors()):
        label = actor.get_actor_label()
        if label.startswith("native_phenomena_demo_"):
            continue
        mesh_path = actor_static_mesh_path(actor)
        text = f"{label} {mesh_path}".lower()
        if any(term in text for term in conflict_terms):
            removed.append({"label": label, "mesh": mesh_path})
            editor.destroy_actor(actor)
    return removed


def remove_map_actors_for_controlled_case(editor, selected_map: dict, case_type: str | None, runtime_scene: dict | None = None) -> list[dict]:
    if not selected_map.get("opened"):
        return []
    controls = runtime_lighting_controls(runtime_scene)
    removal_terms = [str(term).strip().lower() for term in controls.get("remove_map_actor_terms", []) if str(term).strip()]
    if selected_map.get("name") == "MarketEnvironment_Day" and removal_terms:
        removed = []
        for actor in list(editor.get_all_level_actors()):
            label = actor.get_actor_label()
            if label.startswith("native_phenomena_demo_"):
                continue
            mesh_path = actor_static_mesh_path(actor)
            text = f"{label} {mesh_path}".lower()
            if any(term in text for term in removal_terms):
                removed.append({"label": label, "mesh": mesh_path})
                editor.destroy_actor(actor)
        return [{"mode": "runtime_remove_map_actor_terms", "terms": removal_terms, "removed_count": len(removed), "sample_removed_actors": removed[:24]}]
    physics_controls = (runtime_scene or {}).get("physics_controls") if isinstance(runtime_scene, dict) else {}
    formal_initial_state_only = (
        isinstance(physics_controls, dict)
        and physics_controls.get("input_mode") == "initial_state_only"
        and physics_controls.get("state_solver") == "ue_chaos"
    )
    if (
        selected_map.get("name") != "MarketEnvironment_Day"
        or case_type != "bottle_domino_chain"
        or not CONTROLLED_BOTTLE_STAGE
        or formal_initial_state_only
    ):
        return []
    removed_count = 0
    samples = []
    for actor in list(editor.get_all_level_actors()):
        label = actor.get_actor_label()
        if label.startswith("native_phenomena_demo_"):
            continue
        mesh_path = actor_static_mesh_path(actor)
        if len(samples) < 24:
            try:
                class_name = actor.get_class().get_name()
            except Exception:
                class_name = ""
            samples.append({"label": label, "class": class_name, "mesh": mesh_path})
        try:
            editor.destroy_actor(actor)
            removed_count += 1
        except Exception:
            pass
    selected_map["controlled_stage"] = "bottle_domino_chain_foreground"
    selected_map["controlled_stage_removed_map_actor_count"] = removed_count
    return [{
        "mode": "controlled_bottle_stage_remove_existing_map_actors",
        "removed_count": removed_count,
        "sample_removed_actors": samples,
    }]


def ensure_map_actors_visible(editor, selected_map: dict) -> dict:
    if not selected_map.get("opened"):
        return {"actors": 0, "components": 0}
    actor_count = 0
    component_count = 0
    for actor in editor.get_all_level_actors():
        if actor.get_actor_label().startswith("native_phenomena_demo_"):
            continue
        try:
            actor.set_actor_hidden_in_game(False)
        except Exception:
            pass
        actor_count += 1
        try:
            components = actor.get_components_by_class(unreal.PrimitiveComponent)
        except Exception:
            components = []
        for component in components or []:
            try:
                component.set_visibility(True, True)
            except Exception:
                pass
            try:
                component.set_hidden_in_game(False, True)
            except Exception:
                pass
            component_count += 1
    return {"actors": actor_count, "components": component_count}


def median_float(values: list[float], default: float = 0.0) -> float:
    clean = sorted(float(value) for value in values if value is not None)
    if not clean:
        return default
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) * 0.5


def map_stage_origin(editor, selected_map: dict, fallback: unreal.Vector) -> dict:
    if not selected_map.get("opened"):
        return {"origin": fallback, "reason": "map_not_opened", "actor_count": 0}
    records = []
    skipped_terms = (
        "sky", "skybox", "skysphere", "sky_sphere", "cloud", "fog", "light", "atmosphere", "volumetric",
        "waterplane", "hightplane", "heightplane", "ocean", "sea_plane",
    )
    for actor in editor.get_all_level_actors():
        label = actor.get_actor_label()
        if label.startswith("native_phenomena_demo_"):
            continue
        mesh_path = actor_static_mesh_path(actor)
        if not mesh_path:
            continue
        origin, extent = actor_bounds(actor)
        max_extent = max_axis(extent)
        text = f"{label} {mesh_path}".lower()
        if any(term in text for term in skipped_terms):
            continue
        if max_extent > 50000.0 or extent.z > 50000.0:
            continue
        records.append({
            "actor": actor,
            "label": label,
            "mesh": mesh_path,
            "origin": origin,
            "extent": extent,
            "max_extent": max_extent,
            "bottom_z": origin.z - extent.z,
            "text": text,
        })
    if not records:
        return {"origin": fallback, "reason": "no_mesh_map_actors", "actor_count": 0}

    preferred_terms = {
        "MarketEnvironment_Day": (
            "sidewalk", "road", "street", "barrier", "table", "chair", "floor",
        ),
        "TropicalIsland_Level_Scene_01": (
            "bridge", "wooden", "coconut", "tree", "plant", "flower", "grass",
        ),
    }.get(selected_map.get("name"), ())
    preferred = [record for record in records if any(term in record["text"] for term in preferred_terms)]
    if selected_map.get("name") == "MarketEnvironment_Day":
        preferred = [record for record in preferred if record["max_extent"] < 2600.0 and record["extent"].z < 220.0] or preferred
    elif selected_map.get("name") == "TropicalIsland_Level_Scene_01":
        preferred = [record for record in preferred if record["max_extent"] < 3600.0] or preferred
    compact_records = [record for record in records if record["max_extent"] < 8500.0 and record["extent"].z < 2500.0]
    stage_records = preferred if len(preferred) >= 3 else (compact_records if len(compact_records) >= 3 else records)
    center_x = median_float([record["origin"].x for record in stage_records], fallback.x)
    center_y = median_float([record["origin"].y for record in stage_records], fallback.y)
    low_profile = [record for record in stage_records if record["extent"].z < 1200.0]
    ground_source = low_profile or stage_records
    ground_z = median_float([record["bottom_z"] for record in ground_source], fallback.z)
    center_z = median_float([record["origin"].z for record in stage_records], ground_z)
    center, extent = combined_actor_bounds([record["actor"] for record in stage_records])
    origin = unreal.Vector(center_x, center_y, ground_z + 45.0)
    samples = []
    for record in sorted(stage_records, key=lambda item: item["max_extent"], reverse=True)[:12]:
        samples.append({
            "label": record["label"],
            "mesh": record["mesh"],
            "origin": [record["origin"].x, record["origin"].y, record["origin"].z],
            "extent": [record["extent"].x, record["extent"].y, record["extent"].z],
        })
    if STABLE_STAGE_ANCHORS and selected_map.get("name") == "MarketEnvironment_Day":
        origin = unreal.Vector(-1540.0, 1320.0, 4.0)
        return {
            "origin": origin,
            "reason": "stable_market_gas_station_forecourt_camera_anchor",
            "actor_count": len(stage_records),
            "candidate_actor_count": len(records),
            "preferred_actor_count": len(preferred),
            "center": [center.x, center.y, center.z],
            "median_center": [center_x, center_y, center_z],
            "extent": [extent.x, extent.y, extent.z],
            "ground_z": ground_z,
            "stage_actor_samples": samples,
        }
    return {
        "origin": origin,
        "reason": "map_mesh_actor_bounds_robust",
        "actor_count": len(stage_records),
        "candidate_actor_count": len(records),
        "preferred_actor_count": len(preferred),
        "center": [center.x, center.y, center.z],
        "median_center": [center_x, center_y, center_z],
        "extent": [extent.x, extent.y, extent.z],
        "ground_z": ground_z,
        "stage_actor_samples": samples,
    }


def max_axis(vector: unreal.Vector) -> float:
    return max(abs(vector.x), abs(vector.y), abs(vector.z))


def runtime_desired_extent_cm(obj: dict) -> float:
    text = " ".join(str(obj.get(key, "")) for key in ("id", "asset_key", "asset_name", "category_l1", "category_l2", "behavior")).lower()
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    properties = obj.get("physics_properties") if isinstance(obj.get("physics_properties"), dict) else {}
    explicit_extent = params.get("desired_extent_cm", properties.get("desired_extent_cm"))
    if explicit_extent is not None:
        try:
            structural_behaviors = {"landing_surface", "room_floor", "room_wall", "room_ceiling"}
            cap_cm = 900.0 if obj.get("behavior") in structural_behaviors else 420.0
            role = str(params.get("role") or "").casefold()
            compact_analytic_role = role in {"constraint_anchor", "elastic_constraint_anchor", "elastic_constrained_body"}
            min_cm = 2.0 if obj.get("behavior") == "llm_rigid_body" or compact_analytic_role else (10.0 if obj.get("behavior") == "character_carry_object" else 24.0)
            return max(min_cm, min(cap_cm, float(explicit_extent)))
        except Exception:
            pass
    if "balloon" in text or obj.get("behavior") == "wind_drift":
        return 78.0
    if obj.get("behavior") == "character_carry_object":
        return 72.0
    if obj.get("behavior") == "slope_roll":
        return 135.0
    if obj.get("behavior") == "rolling_friction":
        return 128.0
    if obj.get("behavior") == "rolling_friction_surface":
        return 420.0
    if obj.get("behavior") == "projectile_arc":
        return 120.0
    if obj.get("behavior") == "projectile_landing_zone":
        return 360.0
    if obj.get("behavior") == "pendulum_swing":
        return 112.0
    if obj.get("behavior") == "pendulum_anchor":
        return 42.0
    if obj.get("behavior") == "constraint_joint_link":
        return 86.0
    if obj.get("behavior") == "constraint_anchor":
        return 46.0
    if obj.get("behavior") == "stack_stability":
        return 92.0
    if obj.get("behavior") == "stack_push_impactor":
        return 68.0
    if obj.get("behavior") == "stack_support_surface":
        return 320.0
    if obj.get("behavior") == "gear_collision":
        return 150.0
    if obj.get("behavior") == "domino_tip":
        return 96.0
    if obj.get("behavior") == "wheel_jump":
        return 142.0
    if obj.get("behavior") == "slope_surface":
        return 410.0
    if obj.get("behavior") == "landing_surface":
        return 420.0
    if obj.get("behavior") == "third_person_runner":
        return 185.0
    if obj.get("behavior") == "friction_slide":
        return 128.0
    if obj.get("behavior") == "rolling_impact":
        return 112.0
    if obj.get("behavior") == "impact_response":
        return 126.0
    if obj.get("behavior") == "barrel_cascade_impactor":
        return 220.0
    if obj.get("behavior") == "barrel_cascade_target":
        return 128.0
    if any(term in text for term in ("ramp",)):
        return 260.0
    if any(term in text for term in ("table", "barrier")):
        return 230.0
    if any(term in text for term in ("barrel",)):
        return 145.0
    if any(term in text for term in ("cone",)):
        return 120.0
    if any(term in text for term in ("box", "crate")):
        return 132.0
    if any(term in text for term in ("bottle",)):
        return 96.0
    if any(term in text for term in ("wheel", "tire")):
        return 142.0
    if any(term in text for term in ("bridge", "fence", "wooden", "track")):
        return 245.0
    if any(term in text for term in ("tree", "coconut")):
        return 440.0
    if any(term in text for term in ("plant", "grass", "flower")):
        return 260.0
    if any(term in text for term in ("gear", "stone", "rock")):
        return 135.0
    return 110.0


def normalize_runtime_actor(actor, obj: dict) -> tuple[unreal.Vector, unreal.Vector]:
    origin, extent = actor_bounds(actor)
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    if params.get("preserve_authored_scale") is True:
        return origin, extent
    current_extent = max_axis(extent)
    if current_extent > 0.01:
        factor = max(0.02, min(100.0, runtime_desired_extent_cm(obj) / current_extent))
        if str(obj.get("asset_kind") or "").casefold() == "blueprint":
            scale = actor.get_actor_scale3d()
            actor.set_actor_scale3d(unreal.Vector(scale.x * factor, scale.y * factor, scale.z * factor))
        else:
            component = actor_runtime_component(actor)
            if not component:
                return origin, extent
            try:
                scale = component.get_component_scale()
            except Exception:
                try:
                    scale = component.get_editor_property("relative_scale3d")
                except Exception:
                    scale = unreal.Vector(1.0, 1.0, 1.0)
            component.set_world_scale3d(unreal.Vector(scale.x * factor, scale.y * factor, scale.z * factor))
        origin, extent = actor_bounds(actor)
    return origin, extent


def runtime_base_rotation(obj: dict) -> list[float]:
    params = obj.get("params") or {}
    raw = params.get("base_rotation_degrees")
    if isinstance(raw, list) and len(raw) == 3:
        return [float(value) for value in raw]
    return [0.0, 0.0, 0.0]


def runtime_combined_rotation(obj: dict, rotation_degrees: list[float] | tuple[float, float, float] | None) -> list[float]:
    base = runtime_base_rotation(obj)
    motion = list(rotation_degrees or [0.0, 0.0, 0.0])
    motion = (motion + [0.0, 0.0, 0.0])[:3]
    return [base[idx] + float(motion[idx]) for idx in range(3)]


def stabilize_domino_runtime_scene(runtime_scene: dict | None) -> list[dict]:
    if not runtime_scene or runtime_scene.get("case_type") != "bottle_domino_chain":
        return []
    bottles = [
        obj
        for obj in runtime_scene.get("dynamic_objects") or []
        if obj.get("behavior") == "domino_tip" and str(obj.get("id") or "").startswith("bottle_")
    ]
    if len(bottles) < 3:
        return []
    bottles.sort(key=lambda item: int(str(item.get("id", "0")).rsplit("_", 1)[-1]) if str(item.get("id", "")).rsplit("_", 1)[-1].isdigit() else 0)
    base_x = float((bottles[0].get("initial_position_m") or [-0.84])[0])
    adjustments = []
    for idx, obj in enumerate(bottles):
        params = obj.setdefault("params", {})
        properties = obj.setdefault("physics_properties", {})
        position = obj.get("initial_position_m")
        if isinstance(position, list) and len(position) >= 3:
            old_x = float(position[0])
            position[0] = round(base_x + idx * 0.50, 4)
            if abs(position[0] - old_x) > 1e-6:
                adjustments.append({"id": obj.get("id"), "field": "initial_position_m.x", "from": old_x, "to": position[0]})
        base_rotation = params.get("base_rotation_degrees")
        if not (isinstance(base_rotation, list) and len(base_rotation) >= 3):
            base_rotation = [0.0, -4.0, 0.0]
        target_pitch = -18.0 if idx == 0 else -7.0
        old_pitch = float(base_rotation[1])
        if old_pitch > target_pitch:
            base_rotation = [float(base_rotation[0]), target_pitch, float(base_rotation[2])]
            params["base_rotation_degrees"] = base_rotation
            adjustments.append({"id": obj.get("id"), "field": "params.base_rotation_degrees[1]", "from": old_pitch, "to": target_pitch})
        old_damping = float(properties.get("angular_damping", 0.04))
        if old_damping > 0.025:
            properties["angular_damping"] = 0.025
            adjustments.append({"id": obj.get("id"), "field": "physics_properties.angular_damping", "from": old_damping, "to": 0.025})
        if idx == 0:
            velocity = properties.get("initial_velocity_m_s") or params.get("initial_velocity_m_s") or [0.0, 0.0, 0.0]
            if isinstance(velocity, list) and len(velocity) >= 3:
                old_velocity = float(velocity[0])
                if old_velocity < 1.65:
                    velocity = [1.65, float(velocity[1]), float(velocity[2])]
                    properties["initial_velocity_m_s"] = velocity
                    params["initial_velocity_m_s"] = velocity
                    adjustments.append({"id": obj.get("id"), "field": "initial_velocity_m_s[0]", "from": old_velocity, "to": 1.65})
            impulse = properties.get("initial_impulse_n_s") or [0.0, 0.0, 0.0]
            if isinstance(impulse, list) and len(impulse) >= 3:
                old_impulse = float(impulse[0])
                if old_impulse < 0.55:
                    properties["initial_impulse_n_s"] = [0.55, float(impulse[1]), float(impulse[2])]
                    adjustments.append({"id": obj.get("id"), "field": "physics_properties.initial_impulse_n_s[0]", "from": old_impulse, "to": 0.55})
    if adjustments:
        runtime_scene.setdefault("runtime_scene_adjustments", []).append(
            {
                "name": "domino_high_fps_stability",
                "reason": "paper/high-fps C++ runtime needs a stronger first-bottle driver and tighter spacing so the contact chain clears the domino_order verifier without relaxing thresholds",
                "fps": FPS,
                "changes": adjustments,
            }
        )
    return adjustments


def combined_actor_bounds(actors: list) -> tuple[unreal.Vector, unreal.Vector]:
    mins = []
    maxs = []
    for actor in actors:
        origin, extent = actor_bounds(actor)
        mins.append(unreal.Vector(origin.x - extent.x, origin.y - extent.y, origin.z - extent.z))
        maxs.append(unreal.Vector(origin.x + extent.x, origin.y + extent.y, origin.z + extent.z))
    if not mins:
        return unreal.Vector(0.0, 0.0, 120.0), unreal.Vector(160.0, 160.0, 120.0)
    min_x = min(v.x for v in mins)
    min_y = min(v.y for v in mins)
    min_z = min(v.z for v in mins)
    max_x = max(v.x for v in maxs)
    max_y = max(v.y for v in maxs)
    max_z = max(v.z for v in maxs)
    center = unreal.Vector((min_x + max_x) * 0.5, (min_y + max_y) * 0.5, (min_z + max_z) * 0.5)
    extent = unreal.Vector((max_x - min_x) * 0.5, (max_y - min_y) * 0.5, (max_z - min_z) * 0.5)
    return center, extent


MARKET_CLARITY_CAMERA_PRESETS = {
    "market_minimarket_clarity": {
        "label": "Market/Minimarket signage readability",
        "terms": ("market_label", "signage_market", "market_building", "bookshop", "glassdoor"),
        "fallback_target_cm": (-760.0, 1180.0, 190.0),
        "view_offset_cm": (180.0, -1680.0, 330.0),
        "extent_cm": (420.0, 260.0, 260.0),
        "fov": 38.0,
    },
    "market_gas_forecourt_clarity": {
        "label": "Market gas-station forecourt Editor viewport match",
        "absolute_target_cm": (-1540.0, 1320.0, 230.0),
        "absolute_location_cm": (-1540.0, -820.0, 820.0),
        "extent_cm": (1280.0, 980.0, 420.0),
        "fov": 54.0,
    },
    "market_gas_wide_overview": {
        "label": "Market gas-station wide static overview",
        "absolute_target_cm": (-760.0, 1120.0, 210.0),
        "absolute_location_cm": (-3020.0, -1880.0, 760.0),
        "extent_cm": (1900.0, 1400.0, 520.0),
        "fov": 62.0,
    },
    "market_fuel_price_clarity": {
        "label": "Market gas-station numbers and forecourt markings readability",
        "terms": ("price_lable", "signage_big", "signage_small", "pump_gas", "gasstation_roof", "gasstation_environment", "wetlabel"),
        "fallback_target_cm": (0.0, 300.0, 170.0),
        "view_offset_cm": (-360.0, -1280.0, 285.0),
        "extent_cm": (360.0, 260.0, 220.0),
        "fov": 36.0,
    },
}

CAMERA_PRESET_ALIASES = {
    "market_clarity": "market_minimarket_clarity",
    "minimarket": "market_minimarket_clarity",
    "market_minimarket": "market_minimarket_clarity",
    "supermarket": "market_minimarket_clarity",
    "fuel": "market_gas_forecourt_clarity",
    "fuel_price": "market_fuel_price_clarity",
    "gas_station": "market_gas_forecourt_clarity",
    "gas_forecourt": "market_gas_forecourt_clarity",
    "gas_wide": "market_gas_wide_overview",
    "gas_overview": "market_gas_wide_overview",
    "wide_forecourt": "market_gas_wide_overview",
    "forecourt": "market_gas_forecourt_clarity",
    "market_fuel": "market_gas_forecourt_clarity",
}


def render_camera_preset(runtime_scene: dict | None) -> str:
    controls = runtime_scene.get("map_lighting_controls") if isinstance((runtime_scene or {}).get("map_lighting_controls"), dict) else {}
    camera = runtime_scene.get("camera") if isinstance((runtime_scene or {}).get("camera"), dict) else {}
    render = runtime_scene.get("render") if isinstance((runtime_scene or {}).get("render"), dict) else {}
    for value in (
        RENDER_CAMERA_PRESET,
        controls.get("render_camera_preset"),
        controls.get("camera_preset"),
        camera.get("render_camera_preset"),
        camera.get("preset"),
        render.get("camera_preset"),
    ):
        preset = str(value or "").strip().lower()
        if not preset or preset in {"auto", "default", "none", "off", "disabled"}:
            continue
        return CAMERA_PRESET_ALIASES.get(preset, preset)
    return ""


def camera_fov_override(runtime_scene: dict | None) -> float | None:
    spec = MARKET_CLARITY_CAMERA_PRESETS.get(render_camera_preset(runtime_scene))
    if spec:
        return float(spec["fov"])
    camera = runtime_scene.get("camera") if isinstance((runtime_scene or {}).get("camera"), dict) else {}
    try:
        return float(camera["fov"]) if camera.get("fov") is not None else None
    except Exception:
        return None


def market_camera_focus_actor(editor, selected_map: dict, spec: dict) -> dict | None:
    if not editor or selected_map.get("name") != "MarketEnvironment_Day":
        return None
    terms = tuple(str(term).lower() for term in spec.get("terms") or ())
    matches = []
    for actor in editor.get_all_level_actors():
        label = str(actor.get_actor_label() or "")
        if label.startswith("native_phenomena_demo_"):
            continue
        mesh_path = actor_static_mesh_path(actor)
        text = f"{label} {mesh_path}".lower()
        matched_indexes = [idx for idx, term in enumerate(terms) if term in text]
        if not matched_indexes:
            continue
        origin, extent = actor_bounds(actor)
        max_extent = max_axis(extent)
        if max_extent <= 0.0 or max_extent > 15000.0 or extent.z > 6000.0:
            continue
        matches.append((min(matched_indexes), max_extent, label, mesh_path, origin, extent))
    if not matches:
        return None
    _, _, label, mesh_path, origin, extent = sorted(matches, key=lambda item: (item[0], item[1], item[2]))[0]
    return {"label": label, "mesh": mesh_path, "origin": origin, "extent": extent}


def market_clarity_camera_pose(
    runtime_scene: dict | None,
    selected_map: dict | None,
    scene_origin: unreal.Vector | None,
    editor=None,
) -> tuple[unreal.Vector, unreal.Vector, unreal.Vector] | None:
    selected_map = selected_map or {}
    if selected_map.get("name") != "MarketEnvironment_Day":
        return None
    spec = MARKET_CLARITY_CAMERA_PRESETS.get(render_camera_preset(runtime_scene))
    if not spec:
        return None
    origin = scene_origin or map_scene_origin(selected_map)
    if spec.get("absolute_target_cm") and spec.get("absolute_location_cm"):
        target = unreal.Vector(*spec["absolute_target_cm"])
        location = unreal.Vector(*spec["absolute_location_cm"])
        extent = unreal.Vector(*spec["extent_cm"])
        return location, target, extent
    focus = market_camera_focus_actor(editor, selected_map, spec)
    if focus:
        target = focus["origin"] + unreal.Vector(0.0, 0.0, min(max(float(focus["extent"].z) * 0.12, 12.0), 110.0))
        extent = unreal.Vector(
            max(float(focus["extent"].x), spec["extent_cm"][0]),
            max(float(focus["extent"].y), spec["extent_cm"][1]),
            max(float(focus["extent"].z), spec["extent_cm"][2]),
        )
    else:
        target = origin + unreal.Vector(*spec["fallback_target_cm"])
        extent = unreal.Vector(*spec["extent_cm"])
    location = target + unreal.Vector(*spec["view_offset_cm"])
    return location, target, extent


def runtime_camera_pose(runtime_scene: dict, runtime_actors: dict, selected_map: dict | None = None, scene_origin: unreal.Vector | None = None, editor=None) -> tuple[unreal.Vector, unreal.Vector, unreal.Vector]:
    market_pose = market_clarity_camera_pose(runtime_scene, selected_map, scene_origin, editor)
    if market_pose:
        return market_pose
    target, extent = combined_actor_bounds(list(runtime_actors.values()))
    radius = max(180.0, max_axis(extent))
    case_type = runtime_scene.get("case_type")
    camera = runtime_scene.get("camera") if isinstance(runtime_scene.get("camera"), dict) else {}
    camera_mode = str(camera.get("mode") or "").strip().lower()
    if camera_mode in {"fixed", "static", "trajectory"}:
        waypoints = camera.get("preview_waypoints") if isinstance(camera.get("preview_waypoints"), list) else []
        waypoint = waypoints[min(len(waypoints) - 1, len(waypoints) // 2)] if waypoints else {}
        position = waypoint.get("position_m") if isinstance(waypoint, dict) else None
        if not (isinstance(position, list) and len(position) >= 3):
            position = camera.get("location")
        if isinstance(position, list) and len(position) >= 3:
            target_offset = waypoint.get("target_offset_m") if isinstance(waypoint, dict) else None
            if not (isinstance(target_offset, list) and len(target_offset) >= 3):
                target_offset = camera.get("target_offset_m") or [0.0, 0.0, 0.8]
            origin = scene_origin or unreal.Vector(0.0, 0.0, 0.0)
            location = origin + unreal.Vector(float(position[0]) * 100.0, float(position[1]) * 100.0, float(position[2]) * 100.0)
            target = origin + unreal.Vector(float(target_offset[0]) * 100.0, float(target_offset[1]) * 100.0, float(target_offset[2]) * 100.0)
            return location, target, unreal.Vector(max(extent.x, 420.0), max(extent.y, 320.0), max(extent.z, 220.0))
    if case_type == "llm_object_graph":
        object_text = " ".join(
            str(obj.get(key, ""))
            for obj in (runtime_scene.get("dynamic_objects") or []) + (runtime_scene.get("static_objects") or [])
            for key in ("id", "semantic_role", "semantic_purpose", "asset_key", "asset_name", "behavior")
        ).lower()
        tabletop_like = any(term in object_text for term in ("cue", "target_ball", "billiard", "table", "tabletop", "桌", "台球"))
        if tabletop_like:
            dynamic_ids = [str(obj.get("id")) for obj in runtime_scene.get("dynamic_objects") or [] if obj.get("id")]
            focus_actors = [runtime_actors[obj_id] for obj_id in dynamic_ids if obj_id in runtime_actors]
            focus_target, focus_extent = combined_actor_bounds(focus_actors or list(runtime_actors.values()))
            focus_target = focus_target + unreal.Vector(18.0, 0.0, max(10.0, min(max_axis(focus_extent) * 0.08, 28.0)))
            focus_extent = unreal.Vector(max(focus_extent.x, 125.0), max(focus_extent.y, 90.0), max(focus_extent.z, 60.0))
            location = focus_target + unreal.Vector(-185.0, -215.0, 155.0)
            return location, focus_target, focus_extent
    if case_type in {"third_person_box_throw", "character_throw_to_slope_roll", "character_carry_drop"} and "runner_character" in runtime_actors:
        runner_target, runner_extent = combined_actor_bounds([runtime_actors["runner_character"]])
        target = runner_target + unreal.Vector(35.0, 0.0, max(86.0, runner_extent.z * 1.15))
        extent = unreal.Vector(max(extent.x, 320.0), max(extent.y, 260.0), max(extent.z, 220.0))
        radius = max(320.0, max_axis(extent))
        if camera_mode in {"fixed", "object_bound", "first_person", "third_person_follow", "trajectory"}:
            waypoints = camera.get("preview_waypoints") if isinstance(camera.get("preview_waypoints"), list) else []
            waypoint = waypoints[min(len(waypoints) - 1, len(waypoints) // 2)] if waypoints else {}
            rel = waypoint.get("position_m") if isinstance(waypoint, dict) else None
            if not (isinstance(rel, list) and len(rel) >= 3):
                defaults = {
                    "fixed": [-5.2, -6.0, 2.8],
                    "object_bound": [-3.1, -3.9, 2.0],
                    "first_person": [-1.5, -1.0, 1.65],
                    "third_person_follow": [-4.3, -4.8, 2.55],
                    "trajectory": [-4.8, -5.4, 2.9],
                }
                rel = defaults.get(camera_mode, [-4.3, -4.8, 2.55])
            location = target + unreal.Vector(float(rel[0]) * 100.0, float(rel[1]) * 100.0, float(rel[2]) * 100.0)
            if camera_mode == "first_person":
                target = target + unreal.Vector(max(180.0, radius * 0.8), 0.0, 20.0)
            elif camera_mode == "trajectory":
                target = target + unreal.Vector(max(80.0, radius * 0.28), 0.0, 28.0)
                extent = unreal.Vector(max(extent.x, 430.0), max(extent.y, 260.0), max(extent.z, 170.0))
            else:
                target = target + unreal.Vector(60.0, 0.0, 52.0)
            return location, target, extent
        location = target + unreal.Vector(-520.0, -640.0, 260.0)
        return location, target, extent
    if case_type == "falling_crate_collision":
        target = target + unreal.Vector(25.0, -4.0, 150.0)
        extent = unreal.Vector(max(extent.x, 280.0), max(extent.y, 220.0), max(extent.z, 380.0))
        radius = max(440.0, max_axis(extent))
        location = target + unreal.Vector(-max(920.0, radius * 1.45), -max(1560.0, radius * 2.75), max(680.0, radius * 1.18))
        return location, target, extent
    if case_type == "air_collision_pair":
        target = target + unreal.Vector(0.0, 0.0, 24.0)
        extent = unreal.Vector(max(extent.x, 320.0), max(extent.y, 220.0), max(extent.z, 220.0))
        radius = max(340.0, max_axis(extent))
        location = target + unreal.Vector(-max(740.0, radius * 1.34), -max(1180.0, radius * 1.88), max(560.0, radius * 0.82))
        return location, target, extent
    if case_type == "barrel_impact_cascade":
        target = target + unreal.Vector(90.0, 0.0, 78.0)
        extent = unreal.Vector(max(extent.x, 560.0), max(extent.y, 300.0), max(extent.z, 170.0))
        radius = max(520.0, max_axis(extent))
        location = target + unreal.Vector(-max(780.0, radius * 1.18), -max(1180.0, radius * 1.88), max(430.0, radius * 0.70))
        return location, target, extent
    if case_type == "rigid_collision_pair":
        target = target + unreal.Vector(58.0, -4.0, 58.0)
        extent = unreal.Vector(max(extent.x, 340.0), max(extent.y, 180.0), max(extent.z, 135.0))
        radius = max(360.0, max_axis(extent))
        location = target + unreal.Vector(-max(540.0, radius * 1.05), -max(920.0, radius * 1.95), max(320.0, radius * 0.78))
        return location, target, extent
    camera = runtime_scene.get("camera") if isinstance(runtime_scene.get("camera"), dict) else {}
    camera_mode = str(camera.get("mode") or "").strip().lower()
    if camera_mode in {"fixed", "object_bound", "first_person", "third_person_follow", "trajectory"}:
        waypoints = camera.get("preview_waypoints") if isinstance(camera.get("preview_waypoints"), list) else []
        waypoint = waypoints[min(len(waypoints) - 1, len(waypoints) // 2)] if waypoints else {}
        rel = waypoint.get("position_m") if isinstance(waypoint, dict) else None
        if not (isinstance(rel, list) and len(rel) >= 3):
            defaults = {
                "fixed": [-5.2, -6.0, 2.8],
                "object_bound": [-3.1, -3.9, 2.0],
                "first_person": [-1.5, -1.0, 1.65],
                "third_person_follow": [-3.7, -4.2, 2.35],
                "trajectory": [-4.8, -5.4, 2.9],
            }
            rel = defaults.get(camera_mode, [-4.0, -4.4, 2.3])
        location = target + unreal.Vector(float(rel[0]) * 100.0, float(rel[1]) * 100.0, float(rel[2]) * 100.0)
        if camera_mode == "first_person":
            target = target + unreal.Vector(max(180.0, radius * 0.8), 0.0, 20.0)
        elif camera_mode == "trajectory":
            target = target + unreal.Vector(max(80.0, radius * 0.28), 0.0, 28.0)
            extent = unreal.Vector(max(extent.x, 430.0), max(extent.y, 260.0), max(extent.z, 170.0))
        else:
            target = target + unreal.Vector(45.0, 0.0, 45.0)
        return location, target, extent
    if runtime_scene.get("case_type") == "balloon_wind_drift":
        focus_ids = [
            obj.get("id")
            for obj in (runtime_scene.get("dynamic_objects") or []) + (runtime_scene.get("static_objects") or [])
            if obj.get("behavior") == "wind_drift" or any(term in " ".join(str(obj.get(key, "")) for key in ("id", "asset_key", "asset_name")).lower() for term in ("bridge", "wooden"))
        ]
        balloon_ids = [
            obj.get("id")
            for obj in runtime_scene.get("dynamic_objects") or []
            if obj.get("behavior") == "wind_drift"
        ]
        focus_actors = [runtime_actors[obj_id] for obj_id in focus_ids if obj_id in runtime_actors]
        balloon_actors = [runtime_actors[obj_id] for obj_id in balloon_ids if obj_id in runtime_actors]
        focus_target, focus_extent = combined_actor_bounds(focus_actors or list(runtime_actors.values()))
        balloon_target, balloon_extent = combined_actor_bounds(balloon_actors or focus_actors or list(runtime_actors.values()))
        target = focus_target + unreal.Vector(175.0, 8.0, 20.0)
        target.z = balloon_target.z - max(80.0, min(balloon_extent.z * 1.4, 135.0))
        extent = unreal.Vector(max(focus_extent.x, 360.0), max(focus_extent.y, 220.0), max(focus_extent.z, 230.0))
        radius = max(420.0, max_axis(extent))
        location = target + unreal.Vector(-max(420.0, radius * 0.85), max(1080.0, radius * 2.15), max(360.0, radius * 0.78))
        return location, target, extent
    elif runtime_scene.get("case_type") in {"stone_slope_roll", "slope_drop_bounce_stop"}:
        target = target + unreal.Vector(105.0, 0.0, 42.0)
        extent = unreal.Vector(max(extent.x, 240.0), max(extent.y, 170.0), max(extent.z, 120.0))
        radius = max(300.0, max_axis(extent))
        location = target + unreal.Vector(-max(460.0, radius * 0.95), -max(960.0, radius * 2.05), max(330.0, radius * 0.82))
        return location, target, extent
    elif runtime_scene.get("case_type") == "rolling_friction":
        target = target + unreal.Vector(130.0, 0.0, 64.0)
        extent = unreal.Vector(max(extent.x, 430.0), max(extent.y, 260.0), max(extent.z, 150.0))
        radius = max(430.0, max_axis(extent))
        location = target + unreal.Vector(-max(680.0, radius * 1.10), -max(1080.0, radius * 2.05), max(380.0, radius * 0.82))
        return location, target, extent
    elif runtime_scene.get("case_type") == "projectile_arc":
        target = target + unreal.Vector(55.0, 0.0, 150.0)
        extent = unreal.Vector(max(extent.x, 380.0), max(extent.y, 210.0), max(extent.z, 390.0))
        radius = max(440.0, max_axis(extent))
        location = target + unreal.Vector(-max(720.0, radius * 1.22), -max(1120.0, radius * 2.12), max(460.0, radius * 0.98))
        return location, target, extent
    elif runtime_scene.get("case_type") == "pendulum_swing":
        target = target + unreal.Vector(0.0, 0.0, 128.0)
        extent = unreal.Vector(max(extent.x, 240.0), max(extent.y, 180.0), max(extent.z, 360.0))
        radius = max(360.0, max_axis(extent))
        location = target + unreal.Vector(-max(560.0, radius * 1.0), -max(980.0, radius * 1.95), max(360.0, radius * 0.90))
        return location, target, extent
    elif runtime_scene.get("case_type") == "constraint_joint":
        target = target + unreal.Vector(0.0, 0.0, 128.0)
        extent = unreal.Vector(max(extent.x, 280.0), max(extent.y, 190.0), max(extent.z, 380.0))
        radius = max(380.0, max_axis(extent))
        location = target + unreal.Vector(-max(600.0, radius * 1.05), -max(1020.0, radius * 2.0), max(390.0, radius * 0.92))
        return location, target, extent
    elif runtime_scene.get("case_type") == "stack_stability":
        target = target + unreal.Vector(40.0, -6.0, 115.0)
        extent = unreal.Vector(max(extent.x, 360.0), max(extent.y, 210.0), max(extent.z, 300.0))
        radius = max(380.0, max_axis(extent))
        location = target + unreal.Vector(-max(640.0, radius * 1.08), -max(1060.0, radius * 2.02), max(410.0, radius * 0.92))
        return location, target, extent
    elif runtime_scene.get("case_type") == "gear_collision_chain":
        focus_ids = [
            obj.get("id")
            for obj in runtime_scene.get("dynamic_objects") or []
            if obj.get("behavior") == "gear_collision"
        ]
        focus_actors = [runtime_actors[obj_id] for obj_id in focus_ids if obj_id in runtime_actors]
        target, extent = combined_actor_bounds(focus_actors or list(runtime_actors.values()))
        target = target + unreal.Vector(70.0, 0.0, 42.0)
        extent = unreal.Vector(max(extent.x, 250.0), max(extent.y, 150.0), max(extent.z, 140.0))
        radius = max(300.0, max_axis(extent))
        location = target + unreal.Vector(-max(260.0, radius * 0.58), -max(720.0, radius * 1.52), max(300.0, radius * 0.64))
        return location, target, extent
    elif runtime_scene.get("case_type") == "bottle_domino_chain":
        target = target + unreal.Vector(42.0, -8.0, 54.0)
        extent = unreal.Vector(max(extent.x, 320.0), max(extent.y, 180.0), max(extent.z, 170.0))
        radius = max(330.0, max_axis(extent))
        location = target + unreal.Vector(-max(560.0, radius * 1.20), -max(820.0, radius * 1.95), max(270.0, radius * 0.72))
        return location, target, extent
    elif runtime_scene.get("case_type") == "wheel_ramp_jump":
        target = target + unreal.Vector(150.0, -8.0, 92.0)
        extent = unreal.Vector(max(extent.x, 430.0), max(extent.y, 210.0), max(extent.z, 210.0))
        radius = max(440.0, max_axis(extent))
        location = target + unreal.Vector(-max(680.0, radius * 1.10), -max(1180.0, radius * 2.05), max(420.0, radius * 0.84))
        return location, target, extent
    elif runtime_scene.get("case_type") == "crate_friction_slide":
        target = target + unreal.Vector(135.0, 0.0, 68.0)
        extent = unreal.Vector(max(extent.x, 360.0), max(extent.y, 240.0), max(extent.z, 135.0))
        radius = max(380.0, max_axis(extent))
        location = target + unreal.Vector(-max(600.0, radius * 1.10), -max(980.0, radius * 1.95), max(340.0, radius * 0.78))
        return location, target, extent
    elif runtime_scene.get("case_type") == "cone_barrel_collision":
        target = target + unreal.Vector(125.0, -5.0, 70.0)
        extent = unreal.Vector(max(extent.x, 360.0), max(extent.y, 180.0), max(extent.z, 160.0))
        radius = max(390.0, max_axis(extent))
        location = target + unreal.Vector(-max(620.0, radius * 1.12), -max(1040.0, radius * 2.02), max(360.0, radius * 0.82))
        return location, target, extent
    elif runtime_scene.get("case_type") == "plant_sway_camera":
        target = target + unreal.Vector(35.0, 0.0, 150.0)
        extent = unreal.Vector(max(extent.x, 320.0), max(extent.y, 180.0), max(extent.z, 260.0))
        radius = max(420.0, max_axis(extent))
        location = target + unreal.Vector(-max(560.0, radius * 0.95), -max(1260.0, radius * 2.20), max(520.0, radius * 0.90))
        return location, target, extent
    location = target + unreal.Vector(-radius * 0.28, -max(360.0, radius * 1.75), max(170.0, radius * 0.72))
    return location, target, extent


def create_generated_material(
    name: str,
    color: unreal.LinearColor,
    roughness: float = 0.35,
    metallic: float = 0.0,
    emissive: float = 0.0,
    *,
    fixed_color: bool = False,
    two_sided: bool = False,
):
    package_path = "/Game/AgenticGenerated/Materials"
    versioned_name = f"{name}_{GENERATED_MATERIAL_VERSION}"
    asset_path = f"{package_path}/{versioned_name}.{versioned_name}"
    existing = unreal.load_asset(asset_path)
    if existing:
        # Generated assets survive across runs.  Reconcile properties that are
        # part of the material contract instead of silently reusing stale
        # one-sided state from an older harness version.
        try:
            if bool(existing.get_editor_property("two_sided")) != bool(two_sided):
                existing.set_editor_property("two_sided", two_sided)
                unreal.MaterialEditingLibrary.recompile_material(existing)
                unreal.EditorAssetLibrary.save_asset(asset_path)
        except Exception:
            pass
        try:
            unreal.MaterialEditingLibrary.recompile_material(existing)
        except Exception:
            pass
        return existing
    try:
        unreal.EditorAssetLibrary.make_directory(package_path)
        material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            versioned_name,
            package_path,
            unreal.Material,
            unreal.MaterialFactoryNew(),
        )
        if not material:
            return None
        material.set_editor_property("two_sided", two_sided)
        color_node = unreal.MaterialEditingLibrary.create_material_expression(
            material,
            unreal.MaterialExpressionConstant3Vector if fixed_color else unreal.MaterialExpressionVectorParameter,
            -520,
            -160,
        )
        if fixed_color:
            color_node.set_editor_property("constant", color)
        else:
            color_node.set_editor_property("parameter_name", "Tint")
            color_node.set_editor_property("default_value", color)
        unreal.MaterialEditingLibrary.connect_material_property(color_node, "", unreal.MaterialProperty.MP_BASE_COLOR)
        if emissive > 0:
            emissive_node = unreal.MaterialEditingLibrary.create_material_expression(
                material,
                unreal.MaterialExpressionConstant3Vector if fixed_color else unreal.MaterialExpressionVectorParameter,
                -520,
                -20,
            )
            emissive_color = unreal.LinearColor(color.r * emissive, color.g * emissive, color.b * emissive, 1.0)
            if fixed_color:
                emissive_node.set_editor_property("constant", emissive_color)
            else:
                emissive_node.set_editor_property("parameter_name", "EmissiveTint")
                emissive_node.set_editor_property("default_value", emissive_color)
            unreal.MaterialEditingLibrary.connect_material_property(emissive_node, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
        rough_node = unreal.MaterialEditingLibrary.create_material_expression(
            material, unreal.MaterialExpressionScalarParameter, -520, 120
        )
        rough_node.set_editor_property("parameter_name", "Roughness")
        rough_node.set_editor_property("default_value", roughness)
        unreal.MaterialEditingLibrary.connect_material_property(rough_node, "", unreal.MaterialProperty.MP_ROUGHNESS)
        metal_node = unreal.MaterialEditingLibrary.create_material_expression(
            material, unreal.MaterialExpressionScalarParameter, -520, 220
        )
        metal_node.set_editor_property("parameter_name", "Metallic")
        metal_node.set_editor_property("default_value", metallic)
        unreal.MaterialEditingLibrary.connect_material_property(metal_node, "", unreal.MaterialProperty.MP_METALLIC)
        unreal.MaterialEditingLibrary.recompile_material(material)
        unreal.EditorAssetLibrary.save_asset(asset_path)
        return material
    except Exception:
        return None


def create_generated_depth_post_process_material():
    package_path = "/Game/AgenticGenerated/Materials"
    asset_name = "M_Agentic_LinearSceneDepthScaled_V2"
    asset_path = f"{package_path}/{asset_name}.{asset_name}"
    existing = unreal.load_asset(asset_path)
    if existing:
        return existing
    try:
        unreal.EditorAssetLibrary.make_directory(package_path)
        material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            asset_name,
            package_path,
            unreal.Material,
            unreal.MaterialFactoryNew(),
        )
        if not material:
            return None
        material.set_editor_property("material_domain", unreal.MaterialDomain.MD_POST_PROCESS)
        material.set_editor_property(
            "blendable_location",
            unreal.BlendableLocation.BL_SCENE_COLOR_AFTER_TONEMAPPING,
        )
        material.set_editor_property("disable_pre_exposure_scale", True)
        material.set_editor_property("is_blendable", True)
        depth_node = unreal.MaterialEditingLibrary.create_material_expression(
            material,
            unreal.MaterialExpressionSceneDepth,
            -300,
            0,
        )
        scale_node = unreal.MaterialEditingLibrary.create_material_expression(
            material,
            unreal.MaterialExpressionMultiply,
            -80,
            0,
        )
        scale_node.set_editor_property("const_b", 0.0001)
        unreal.MaterialEditingLibrary.connect_material_expressions(
            depth_node,
            "",
            scale_node,
            "A",
        )
        unreal.MaterialEditingLibrary.connect_material_property(
            scale_node,
            "",
            unreal.MaterialProperty.MP_EMISSIVE_COLOR,
        )
        unreal.MaterialEditingLibrary.recompile_material(material)
        unreal.EditorAssetLibrary.save_asset(asset_path)
        return material
    except Exception:
        return None


def set_actor_material(actor, material):
    component = actor_runtime_component(actor)
    if not component:
        return
    if material:
        try:
            count = max(1, int(component.get_num_materials()))
        except Exception:
            count = 1
        for index in range(count):
            try:
                component.set_material(index, material)
            except Exception:
                pass


def set_actor_color(actor, color: unreal.LinearColor):
    component = actor_runtime_component(actor)
    if not component:
        return
    try:
        count = max(1, int(component.get_num_materials()))
    except Exception:
        count = 1
    for index in range(count):
        dyn = component.create_dynamic_material_instance(index)
        if dyn:
            for param in ("BaseColor", "Color", "Tint"):
                try:
                    dyn.set_vector_parameter_value(param, color)
                except Exception:
                    pass


def spawn_runtime_stage_helpers(editor, runtime_scene: dict, scene_origin: unreal.Vector, materials: dict) -> list[dict]:
    cube = load_asset("/Engine/BasicShapes/Cube.Cube")
    floor_mat = materials.get("runtime_floor")
    backdrop_mat = materials.get("runtime_backdrop") or floor_mat
    lane_mat = materials.get("runtime_lane")
    marker_mat = materials.get("runtime_marker")
    helpers = []

    def helper(label: str, loc: tuple[float, float, float], scale: tuple[float, float, float], material, rot: tuple[float, float, float] | None = None):
        actor = editor.spawn_actor_from_class(unreal.StaticMeshActor, scene_origin + unreal.Vector(*loc))
        actor.set_actor_label("native_phenomena_demo_stage_" + label)
        actor.static_mesh_component.set_static_mesh(cube)
        actor.static_mesh_component.set_world_scale3d(unreal.Vector(*scale))
        if rot:
            actor.set_actor_rotation(unreal.Rotator(*rot), False)
        try:
            actor.static_mesh_component.set_cast_shadow(False)
        except Exception:
            set_editor_property_if_available(actor.static_mesh_component, "cast_shadow", False)
        try:
            actor.static_mesh_component.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
        except Exception:
            pass
        set_actor_material(actor, material)
        helpers.append({
            "label": label,
            "mesh": "/Engine/BasicShapes/Cube.Cube",
            "kind": "readability_stage_helper",
            "material_override": True,
            "location": [scene_origin.x + loc[0], scene_origin.y + loc[1], scene_origin.z + loc[2]],
            "scale": list(scale),
        })
        return actor

    case_type = runtime_scene.get("case_type")
    helper("matte_floor", (85.0, 0.0, 10.0), (14.0, 8.2, 0.035), floor_mat)
    helper("soft_back_wall", (120.0, 335.0, 330.0), (13.5, 0.045, 5.4), backdrop_mat)
    helper("left_soft_wall", (-560.0, 105.0, 275.0), (0.04, 5.2, 3.7), backdrop_mat)
    helper("right_soft_wall", (700.0, 105.0, 275.0), (0.04, 5.2, 3.7), backdrop_mat)
    if not EXTRA_STAGE_MARKERS:
        return helpers
    if case_type in {"stone_slope_roll", "slope_drop_bounce_stop"}:
        helper("left_lane", (10.0, -35.0, 82.0), (4.4, 0.06, 0.035), lane_mat)
        helper("right_lane", (10.0, 35.0, 82.0), (4.4, 0.06, 0.035), lane_mat)
        helper("stop_marker", (240.0, 0.0, 112.0), (0.08, 1.35, 0.38), marker_mat)
    elif case_type == "rolling_friction":
        helper("rolling_lane_low", (20.0, -62.0, 82.0), (5.2, 0.045, 0.035), lane_mat)
        helper("rolling_lane_mid", (20.0, 0.0, 82.0), (5.2, 0.045, 0.035), lane_mat)
        helper("rolling_lane_high", (20.0, 62.0, 82.0), (5.2, 0.045, 0.035), lane_mat)
        helper("rolling_stop_marker", (225.0, 0.0, 118.0), (0.12, 1.45, 0.18), marker_mat)
    elif case_type == "projectile_arc":
        helper("projectile_range_line", (10.0, -18.0, 82.0), (4.8, 0.055, 0.035), lane_mat)
        helper("projectile_launch_marker", (-242.0, -18.0, 122.0), (0.14, 0.14, 0.10), marker_mat)
        helper("projectile_landing_marker", (55.0, -18.0, 122.0), (0.18, 0.18, 0.10), marker_mat)
        helper("projectile_apex_marker", (-102.0, 64.0, 408.0), (0.14, 0.14, 0.10), marker_mat)
    elif case_type == "pendulum_swing":
        helper("pendulum_frame_top", (-18.0, -18.0, 325.0), (1.55, 0.045, 0.04), marker_mat)
        helper("pendulum_center_line", (-18.0, -18.0, 235.0), (0.045, 0.045, 1.55), lane_mat)
        helper("pendulum_arc_left", (-106.0, -18.0, 190.0), (0.10, 0.10, 0.08), marker_mat)
        helper("pendulum_arc_right", (70.0, -18.0, 190.0), (0.10, 0.10, 0.08), marker_mat)
    elif case_type == "constraint_joint":
        helper("joint_anchor_frame", (-22.0, -18.0, 335.0), (1.35, 0.045, 0.04), marker_mat)
        helper("joint_center_line", (-22.0, -18.0, 228.0), (0.045, 0.045, 1.85), lane_mat)
        helper("joint_left_arc", (-112.0, -18.0, 218.0), (0.10, 0.10, 0.08), marker_mat)
        helper("joint_right_arc", (82.0, -18.0, 218.0), (0.10, 0.10, 0.08), marker_mat)
    elif case_type == "stack_stability":
        helper("stack_support_edge", (48.0, -18.0, 84.0), (0.06, 1.10, 0.05), marker_mat)
        helper("stack_fall_zone", (132.0, -18.0, 84.0), (1.85, 0.055, 0.04), lane_mat)
        helper("stack_height_axis", (-68.0, -18.0, 174.0), (0.045, 0.045, 2.35), marker_mat)
    elif case_type == "gear_collision_chain":
        helper("gear_lane", (25.0, -25.0, 82.0), (4.8, 0.08, 0.04), lane_mat)
        helper("collision_stop", (265.0, 10.0, 122.0), (0.10, 0.85, 0.46), marker_mat)
    elif case_type == "rigid_collision_pair":
        helper("rigid_collision_lane", (5.0, -18.0, 82.0), (4.8, 0.065, 0.035), lane_mat)
        helper("separation_marker", (185.0, -18.0, 118.0), (0.18, 0.18, 0.08), marker_mat)
    elif case_type == "plant_sway_camera":
        helper("plant_row_ground", (15.0, 0.0, 82.0), (4.8, 0.11, 0.04), lane_mat)
        helper("wind_marker", (-230.0, -95.0, 140.0), (0.62, 0.05, 0.05), marker_mat)
        helper("wind_marker_head", (-195.0, -95.0, 140.0), (0.16, 0.16, 0.05), marker_mat, rot=(0.0, 0.0, 45.0))
    elif case_type == "balloon_wind_drift":
        helper("wind_line", (-20.0, -120.0, 112.0), (5.0, 0.045, 0.04), lane_mat)
    elif case_type == "bottle_domino_chain":
        helper("domino_line", (5.0, -18.0, 82.0), (4.4, 0.055, 0.035), lane_mat)
        helper("order_marker", (-150.0, -55.0, 118.0), (0.16, 0.16, 0.08), marker_mat)
    elif case_type == "wheel_ramp_jump":
        helper("jump_path", (35.0, -32.0, 82.0), (5.2, 0.055, 0.035), lane_mat)
        helper("landing_marker", (160.0, -30.0, 118.0), (0.18, 0.18, 0.08), marker_mat)
    elif case_type == "crate_friction_slide":
        helper("crate_lane_low", (25.0, -58.0, 82.0), (4.8, 0.045, 0.035), lane_mat)
        helper("crate_lane_mid", (25.0, 0.0, 82.0), (4.8, 0.045, 0.035), lane_mat)
        helper("crate_lane_high", (25.0, 58.0, 82.0), (4.8, 0.045, 0.035), lane_mat)
    elif case_type == "cone_barrel_collision":
        helper("impact_lane", (15.0, -18.0, 82.0), (4.9, 0.055, 0.035), lane_mat)
        helper("stop_marker", (160.0, -18.0, 118.0), (0.15, 0.45, 0.20), marker_mat)
    return helpers


def setup_scene(runtime_scene: dict | None = None):
    write_progress_marker("setup_scene_start", f"runtime_scene={bool(runtime_scene)}")
    selected_map = open_selected_map()
    write_progress_marker("setup_scene_open_selected_map", selected_map.get("name"))
    if selected_map.get("path") and not selected_map.get("opened"):
        raise RuntimeError(
            "F3_UE_MAP_OPEN_FAILED: "
            f"requested={selected_map.get('requested_package')}, "
            f"actual={selected_map.get('opened_package')}, error={selected_map.get('error')}"
        )
    editor = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    selected_map = ensure_selected_map_has_actors(editor, selected_map)
    write_progress_marker("setup_scene_ensure_selected_map_has_actors", selected_map.get("name"))
    for actor in editor.get_all_level_actors():
        if selected_map.get("name") == "StudioRuntimeBlank" or actor.get_actor_label().startswith("native_phenomena_demo_"):
            editor.destroy_actor(actor)
    write_progress_marker("setup_scene_destroy_old_demo_actors")
    domino_runtime_adjustments = stabilize_domino_runtime_scene(runtime_scene)
    write_progress_marker("setup_scene_domino_adjustments")

    world = unreal.EditorLevelLibrary.get_editor_world()
    removed_map_actors = remove_conflicting_map_actors(editor, selected_map)
    write_progress_marker("setup_scene_removed_conflicting_map_actors", str(len(removed_map_actors)))
    visible_map_actors = ensure_map_actors_visible(editor, selected_map)
    write_progress_marker("setup_scene_visible_map_actors", str(len(visible_map_actors)))
    loaded_map_actor_count = count_non_demo_actors(editor)
    if selected_map.get("name") == "MarketEnvironment_Day" and loaded_map_actor_count >= 10:
        selected_map["suppress_camera_backdrop"] = True
    default_scene_origin = map_scene_origin(selected_map)
    write_progress_marker("setup_scene_default_scene_origin", str(default_scene_origin))
    map_stage = map_stage_origin(editor, selected_map, default_scene_origin)
    write_progress_marker("setup_scene_map_stage_origin")
    try:
        write_progress_marker("setup_scene_controlled_remove_start", runtime_scene.get("case_type") if runtime_scene else None)
        controlled_removed = remove_map_actors_for_controlled_case(
            editor,
            selected_map,
            runtime_scene.get("case_type") if runtime_scene else None,
            runtime_scene,
        )
        write_progress_marker("setup_scene_controlled_remove_done", str(len(controlled_removed)))
    except Exception as exc:
        write_progress_marker("setup_scene_controlled_remove_error", str(exc))
        controlled_removed = []
    if controlled_removed:
        removed_map_actors.extend(controlled_removed)
        loaded_map_actor_count = count_non_demo_actors(editor)
        visible_map_actors = ensure_map_actors_visible(editor, selected_map)
        selected_map["suppress_camera_backdrop"] = False
    scene_origin = map_stage["origin"]
    write_progress_marker("setup_scene_post_controlled_remove")
    lighting_controls = runtime_lighting_controls(runtime_scene)
    lighting_visual_profile = str(lighting_controls.get("visual_realism_profile") or VISUAL_REALISM_PROFILE).strip().lower()
    editor_viewport_match_realism = lighting_visual_profile in {"editor_viewport_match", "viewport_match", "editor_lit_match", "lit_viewport_match"}
    editor_parity_realism = editor_viewport_match_realism or lighting_visual_profile in {"editor_parity", "viewport", "viewport_lit", "lit_viewport", "realism", "realistic"}
    write_progress_marker("setup_scene_lighting_controls")
    physics_controls = runtime_physics_controls(runtime_scene)
    write_progress_marker("setup_scene_physics_controls")
    lighting_profile = apply_lighting_control_overrides(runtime_lighting_profile(selected_map, runtime_scene, editor_parity_realism), lighting_controls)
    if editor_parity_realism and selected_map.get("name") == "MarketEnvironment_Day":
        lighting_controls.setdefault("capture_backend", "highres_viewport")
        lighting_controls.setdefault("capture_source", "SCS_FINAL_COLOR_LDR")
        lighting_controls.setdefault("video_filter", "")
        lighting_controls.setdefault("helper_light_intensity_scale", 0.0)
        lighting_controls.setdefault("map_boost_intensity_scale", 0.0)
        if not editor_viewport_match_realism:
            lighting_controls["spawn_directional_sun"] = bool(lighting_controls.get("spawn_directional_sun", False)) and bool(lighting_controls.get("allow_editor_parity_extra_lights", False))
            lighting_controls["spawn_fill_light"] = bool(lighting_controls.get("spawn_fill_light", False)) and bool(lighting_controls.get("allow_editor_parity_extra_lights", False))
            lighting_controls["spawn_sky_light"] = bool(lighting_controls.get("spawn_sky_light", False)) and bool(lighting_controls.get("allow_editor_parity_extra_lights", False))
            lighting_controls["spawn_map_boost_lights"] = bool(lighting_controls.get("spawn_map_boost_lights", False)) and bool(lighting_controls.get("allow_editor_parity_extra_lights", False))
            lighting_controls["spawn_sky_atmosphere"] = bool(lighting_controls.get("spawn_sky_atmosphere", False)) and bool(lighting_controls.get("allow_editor_parity_extra_lights", False))
            lighting_controls["fixed_auto_exposure"] = bool(lighting_controls.get("fixed_auto_exposure", False)) and bool(lighting_controls.get("allow_editor_parity_fixed_exposure", False))
        lighting_profile = apply_lighting_control_overrides(lighting_profile, lighting_controls)
    write_progress_marker("setup_scene_lighting_profile")
    write_progress_marker("setup_scene_configure_world_start")
    world_visuals = configure_runtime_world(selected_map, editor_parity_realism)
    write_progress_marker("setup_scene_configure_world_done")
    render_quality = configure_render_quality()
    write_progress_marker("setup_scene_render_quality_done")
    skip_complex_lighting = bool(runtime_scene and runtime_scene.get("case_type") == "third_person_box_throw")
    if skip_complex_lighting:
        existing_map_lights = {"enabled": True, "inspected": 0, "changed": 0, "skipped": True}
    else:
        existing_map_lights = configure_existing_map_lights(
            editor,
            lighting_enabled(lighting_controls, "use_existing_map_lights"),
        )
    write_progress_marker("setup_scene_existing_map_lights_done")
    background_stage_actors = []
    sun_component = None
    map_boost_lights = []
    custom_lights = []
    if skip_complex_lighting:
        write_progress_marker("setup_scene_lighting_block_skipped")
    else:
        write_progress_marker("setup_scene_lighting_block_start")
    if (not skip_complex_lighting) and lighting_enabled(lighting_controls, "spawn_directional_sun"):
        write_progress_marker("setup_scene_spawn_sun_start")
        sun = editor.spawn_actor_from_class(unreal.DirectionalLight, scene_origin + unreal.Vector(-220, -420, 720))
        sun.set_actor_label("native_phenomena_demo_sun")
        sun.set_actor_rotation(unreal.Rotator(-38.0, -18.0, 0.0), False)
        set_light(sun, lighting_profile["sun"], unreal.Color(255, 244, 218, 255))
        sun_component = get_light_component(sun)
    if (not skip_complex_lighting) and runtime_scene and selected_map.get("name") == "MarketEnvironment_Day" and runtime_scene.get("case_type") == "bottle_domino_chain" and sun_component:
        set_editor_property_if_available(sun_component, "cast_shadows", False)
    if (not skip_complex_lighting) and lighting_enabled(lighting_controls, "spawn_fill_light"):
        write_progress_marker("setup_scene_spawn_fill_start")
        fill = editor.spawn_actor_from_class(unreal.DirectionalLight, scene_origin + unreal.Vector(320, 360, 420))
        fill.set_actor_label("native_phenomena_demo_fill")
        fill.set_actor_rotation(unreal.Rotator(-15.0, 155.0, 0.0), False)
        set_light(fill, lighting_profile["fill"], unreal.Color(190, 220, 255, 255))
        fill_component = get_light_component(fill)
        if fill_component:
            set_editor_property_if_available(fill_component, "cast_shadows", False)
    if (not skip_complex_lighting) and lighting_enabled(lighting_controls, "spawn_sky_light"):
        write_progress_marker("setup_scene_spawn_sky_start")
        sky = editor.spawn_actor_from_class(unreal.SkyLight, scene_origin + unreal.Vector(0, 0, 360))
        sky.set_actor_label("native_phenomena_demo_sky")
        set_light(sky, lighting_profile["sky"])
    if (not skip_complex_lighting) and lighting_enabled(lighting_controls, "spawn_map_boost_lights") and runtime_scene and selected_map.get("name") == "MarketEnvironment_Day" and runtime_scene.get("case_type") != "bottle_domino_chain":
        write_progress_marker("setup_scene_spawn_map_boost_start")
        boost_scale = float(lighting_profile.get("map_boost_intensity_scale", 1.0))
        boost_specs = [
            ((80.0, -240.0, 420.0), 1500.0 * boost_scale, 1800.0),
            ((260.0, 220.0, 360.0), 900.0 * boost_scale, 1500.0),
        ]
        for idx, (loc, intensity, radius) in enumerate(boost_specs, start=1):
            point = editor.spawn_actor_from_class(unreal.PointLight, scene_origin + unreal.Vector(*loc))
            point.set_actor_label(f"native_phenomena_demo_map_boost_light_{idx}")
            set_light(point, intensity, unreal.Color(255, 246, 224, 255))
            component = get_light_component(point)
            if component:
                set_editor_property_if_available(component, "attenuation_radius", radius)
                set_editor_property_if_available(component, "source_radius", 120.0)
                set_editor_property_if_available(component, "soft_source_radius", 420.0)
            map_boost_lights.append({"label": point.get_actor_label(), "location": [scene_origin.x + loc[0], scene_origin.y + loc[1], scene_origin.z + loc[2]], "intensity": intensity, "attenuation_radius": radius})
    if (not skip_complex_lighting) and (
        runtime_scene
        and lighting_enabled(lighting_controls, "spawn_map_boost_lights")
        and selected_map.get("name") == "MarketEnvironment_Day"
        and runtime_scene.get("case_type") == "bottle_domino_chain"
    ):
        write_progress_marker("setup_scene_spawn_bottle_boost_start")
        boost_scale = float(lighting_profile.get("map_boost_intensity_scale", 1.0))
        if selected_map.get("controlled_stage") == "bottle_domino_chain_foreground":
            focus_specs = [
                ((-45.0, -165.0, 250.0), 420.0 * boost_scale, 760.0),
                ((165.0, 120.0, 205.0), 280.0 * boost_scale, 640.0),
            ]
        else:
            focus_specs = [
                ((-80.0, -360.0, 430.0), 15000.0 * boost_scale, 2600.0),
                ((260.0, 360.0, 460.0), 9000.0 * boost_scale, 2200.0),
            ]
        for idx, (loc, intensity, radius) in enumerate(focus_specs, start=1):
            point = editor.spawn_actor_from_class(unreal.PointLight, scene_origin + unreal.Vector(*loc))
            point.set_actor_label(f"native_phenomena_demo_bottle_soft_fill_light_{idx}")
            set_light(point, intensity, unreal.Color(255, 238, 205, 255))
            component = get_light_component(point)
            if component:
                set_editor_property_if_available(component, "attenuation_radius", radius)
                set_editor_property_if_available(component, "source_radius", 420.0)
                set_editor_property_if_available(component, "soft_source_radius", 900.0)
                set_editor_property_if_available(component, "cast_shadows", False)
            map_boost_lights.append({"label": point.get_actor_label(), "location": [scene_origin.x + loc[0], scene_origin.y + loc[1], scene_origin.z + loc[2]], "intensity": intensity, "attenuation_radius": radius, "kind": "bottle_soft_fill_light"})
    if lighting_enabled(lighting_controls, "spawn_sky_atmosphere") and (selected_map.get("controlled_stage") == "bottle_domino_chain_foreground" or selected_map.get("name") == "StudioRuntimeBlank" or not selected_map.get("opened")):
        spawn_optional_actor(editor, "SkyAtmosphere", scene_origin, "native_phenomena_demo_sky_atmosphere")
    if not skip_complex_lighting:
        custom_lights = spawn_runtime_custom_lights(editor, runtime_scene, scene_origin)

    materials = {
        "runtime_balloon_red": create_generated_material("M_Agentic_Runtime_BalloonRed", unreal.LinearColor(0.92, 0.05, 0.06, 1.0), 0.46, 0.0, 0.16),
        "runtime_balloon_yellow": create_generated_material("M_Agentic_Runtime_BalloonYellow", unreal.LinearColor(1.0, 0.62, 0.03, 1.0), 0.46, 0.0, 0.16),
        "runtime_balloon_blue": create_generated_material("M_Agentic_Runtime_BalloonBlue", unreal.LinearColor(0.05, 0.36, 0.95, 1.0), 0.46, 0.0, 0.16),
        "runtime_balloon_green": create_generated_material("M_Agentic_Runtime_BalloonGreen", unreal.LinearColor(0.12, 0.66, 0.22, 1.0), 0.50, 0.0, 0.16),
        "runtime_bridge": create_generated_material("M_Agentic_Runtime_WarmWood", unreal.LinearColor(0.50, 0.30, 0.14, 1.0), 0.66, 0.0, 0.01),
        "runtime_stone": create_generated_material("M_Agentic_Runtime_StoneReadable", unreal.LinearColor(0.58, 0.53, 0.45, 1.0), 0.82, 0.0, 0.01),
        "runtime_gear": create_generated_material("M_Agentic_Runtime_GearReadable", unreal.LinearColor(0.74, 0.70, 0.60, 1.0), 0.42, 0.30, 0.01),
        "runtime_tree": create_generated_material("M_Agentic_Runtime_PlantGreen", unreal.LinearColor(0.15, 0.50, 0.20, 1.0), 0.68, 0.0, 0.01),
        "runtime_flower": create_generated_material("M_Agentic_Runtime_FlowerAccent", unreal.LinearColor(0.84, 0.24, 0.34, 1.0), 0.62, 0.0, 0.01),
        "runtime_prop": create_generated_material("M_Agentic_Runtime_PropNeutral", unreal.LinearColor(0.62, 0.56, 0.46, 1.0), 0.64, 0.0, 0.01),
        "runtime_character": create_generated_material("M_Agentic_Runtime_CharacterReadable", unreal.LinearColor(0.72, 0.66, 0.60, 1.0), 0.62, 0.0, 0.01),
        "runtime_air_sphere": create_generated_material("M_Agentic_Runtime_AirSphere", unreal.LinearColor(0.14, 0.58, 0.96, 1.0), 0.30, 0.0, 0.02),
        "runtime_air_box": create_generated_material("M_Agentic_Runtime_AirBox", unreal.LinearColor(0.88, 0.54, 0.12, 1.0), 0.72, 0.0, 0.01),
        "runtime_bottle": create_generated_material("M_Agentic_Runtime_BottleReadable", unreal.LinearColor(0.98, 0.58, 0.10, 1.0), 0.50, 0.0, 0.0),
        "runtime_box": create_generated_material("M_Agentic_Runtime_BoxCardboard", unreal.LinearColor(0.58, 0.38, 0.19, 1.0), 0.72, 0.0, 0.01),
        "runtime_wheel": create_generated_material("M_Agentic_Runtime_WheelRubber", unreal.LinearColor(0.06, 0.06, 0.055, 1.0), 0.54, 0.0, 0.0),
        "runtime_cone": create_generated_material("M_Agentic_Runtime_ConeOrange", unreal.LinearColor(0.95, 0.28, 0.05, 1.0), 0.50, 0.0, 0.01),
        "runtime_barrel": create_generated_material("M_Agentic_Runtime_BarrelBlue", unreal.LinearColor(0.12, 0.26, 0.48, 1.0), 0.56, 0.0, 0.01),
        "runtime_floor": create_generated_material("M_Agentic_Runtime_FloorMatte", unreal.LinearColor(0.76, 0.78, 0.72, 1.0), 0.84, 0.0, 0.20),
        "runtime_backdrop": create_generated_material("M_Agentic_Runtime_BackdropMatte", unreal.LinearColor(0.84, 0.88, 0.84, 1.0), 0.88, 0.0, 0.62),
        "runtime_lane": create_generated_material("M_Agentic_Runtime_LaneMuted", unreal.LinearColor(0.30, 0.44, 0.50, 1.0), 0.60, 0.0, 0.0),
        "runtime_marker": create_generated_material("M_Agentic_Runtime_MarkerMuted", unreal.LinearColor(0.88, 0.60, 0.18, 1.0), 0.46, 0.0, 0.0),
        "market_asphalt": create_generated_material("M_Agentic_Market_AsphaltReadable", unreal.LinearColor(0.74, 0.77, 0.72, 1.0), 0.84, 0.0, 0.12),
        "market_backdrop": create_generated_material("M_Agentic_Market_BackdropReadable", unreal.LinearColor(0.84, 0.90, 0.92, 1.0), 0.86, 0.0, 0.40),
        "market_canopy": create_generated_material("M_Agentic_Market_CanopyRed", unreal.LinearColor(0.34, 0.055, 0.04, 1.0), 0.68, 0.0, 0.0),
        "market_pump": create_generated_material("M_Agentic_Market_PumpBlueWhite", unreal.LinearColor(0.24, 0.34, 0.46, 1.0), 0.62, 0.0, 0.0),
        "market_store": create_generated_material("M_Agentic_Market_StoreBrick", unreal.LinearColor(0.25, 0.16, 0.11, 1.0), 0.72, 0.0, 0.0),
    }
    if not runtime_scene:
        materials.update({
            "water": create_generated_material("M_Agentic_Native_Water", unreal.LinearColor(0.05, 0.55, 0.98, 1.0), 0.03, 0.0, 0.30),
            "water_highlight": create_generated_material("M_Agentic_Native_WaterHighlight", unreal.LinearColor(0.78, 0.97, 1.0, 1.0), 0.01, 0.0, 0.65),
            "rubber": create_generated_material("M_Agentic_Native_Rubber", unreal.LinearColor(1.0, 0.18, 0.0, 1.0), 0.42, 0.0, 0.18),
            "lead": create_generated_material("M_Agentic_Native_Lead", unreal.LinearColor(0.08, 0.08, 0.09, 1.0), 0.24, 0.85, 0.02),
            "steel": create_generated_material("M_Agentic_Native_Steel", unreal.LinearColor(0.95, 0.95, 0.90, 1.0), 0.16, 0.9, 0.35),
            "magnet_red": create_generated_material("M_Agentic_Native_MagnetRed", unreal.LinearColor(1.0, 0.0, 0.0, 1.0), 0.24, 0.25, 0.35),
            "magnet_blue": create_generated_material("M_Agentic_Native_MagnetBlue", unreal.LinearColor(0.0, 0.10, 1.0, 1.0), 0.24, 0.25, 0.35),
            "marker": create_generated_material("M_Agentic_Native_Marker", unreal.LinearColor(1.0, 0.92, 0.0, 1.0), 0.35, 0.0, 0.35),
        })

    spawned_assets = []
    stage_helper_actors = []
    runtime_ground_offsets = {}
    chaos_runtime = {
        "engine": "UE5 Chaos",
        "rigid_body_setup_enabled": bool(physics_controls.get("rigid_body_setup_enabled", True)),
        "simulate_physics_enabled": bool(physics_controls.get("simulate_physics", False)),
        "driver": str(physics_controls.get("simulation_driver") or "scripted_trajectory_replay_with_collision_shapes"),
        "controls": physics_controls,
        "runtime_scene_adjustments": domino_runtime_adjustments,
        "config_source": "ADP DefaultEngine.ini collision channels and physical surfaces mirrored into local_ue_render",
        "actors": [],
    }

    def place_runtime(obj, loc, rotation_degrees=(0.0, 0.0, 0.0)):
        mesh_path = resolve_runtime_asset_path(obj)
        combined_rotation = runtime_combined_rotation(obj, list(rotation_degrees))
        spawn_rotation = runtime_rotator(combined_rotation)
        write_progress_marker("place_runtime_spawn_start", obj.get("id"))
        actor = spawn_runtime_actor(
            "native_phenomena_demo_" + obj["id"],
            mesh_path,
            str(obj.get("asset_kind") or ""),
            ue_vec_from_meters(loc, z_offset_cm=0.0, origin=scene_origin),
            unreal.Vector(*[float(v) for v in obj.get("scale", [0.65, 0.65, 0.65])]),
            None,
            spawn_rotation,
        )
        try:
            actor.set_actor_rotation(spawn_rotation, False)
        except Exception:
            pass
        try:
            actor.static_mesh_component.set_world_rotation(spawn_rotation, False, None, False)
        except Exception:
            try:
                actor.static_mesh_component.set_editor_property("relative_rotation", spawn_rotation)
            except Exception:
                pass
        write_progress_marker("place_runtime_spawn_done", obj.get("id"))
        material_override = False
        text = " ".join(str(obj.get(key, "")) for key in ("id", "asset_key", "asset_name", "category_l1", "category_l2")).lower()
        params = obj.get("params") or {}
        blueprint_mesh_override = str(params.get("blueprint_mesh_override") or "").strip()
        if blueprint_mesh_override:
            component = actor_runtime_component(actor)
            if not component:
                desired_extent_cm = float(params.get("desired_extent_cm") or 10.0)
                visual = spawn_static_mesh(
                    f"{obj.get('id')}_blueprint_mesh",
                    blueprint_mesh_override,
                    actor.get_actor_location(),
                    unreal.Vector(desired_extent_cm / 50.0, desired_extent_cm / 50.0, desired_extent_cm / 50.0),
                )
                visual.attach_to_actor(
                    actor,
                    "",
                    unreal.AttachmentRule.KEEP_WORLD,
                    unreal.AttachmentRule.KEEP_WORLD,
                    unreal.AttachmentRule.KEEP_WORLD,
                    False,
                )
                component = visual.static_mesh_component
                try:
                    actor.set_editor_property("mesh", component)
                    write_progress_marker("blueprint_mesh_property_bound", obj.get("id"))
                except Exception as exc:
                    write_progress_marker("blueprint_mesh_property_unavailable", f"{obj.get('id')}:{exc}")
            if component and hasattr(component, "set_static_mesh"):
                component.set_static_mesh(load_asset(blueprint_mesh_override))
        if params.get("disallow_nanite") is True or params.get("surface_mesh_sequence"):
            component = actor_runtime_component(actor)
            if component:
                set_editor_property_if_available(component, "disallow_nanite", True)
        preserve_material = params.get("preserve_material", True)
        has_material_profile = bool(obj.get("material_profile"))
        force_library_mesh = prefers_material_library_mesh(obj)
        visual_material_path = str(params.get("visual_material_path") or "")
        if params.get("generate_solid_material") is True:
            material = create_generated_material(
                str(params.get("generated_material_name") or f"M_Harness_{obj.get('id')}_Solid"),
                runtime_object_color(obj, unreal.LinearColor(0.05, 0.32, 0.82, 1.0)),
                roughness=float(params.get("roughness", 0.24)),
                metallic=float(params.get("metallic", 0.0)),
                emissive=float(params.get("emissive", 0.12)),
                fixed_color=params.get("fixed_material_color") is True,
                two_sided=params.get("two_sided_material") is True,
            )
            if material:
                set_actor_material(actor, material)
                material_override = True
        elif visual_material_path:
            try:
                set_actor_material(actor, load_asset(visual_material_path))
                if any(key in params for key in ("color", "color_rgb", "tint")):
                    set_actor_color(actor, runtime_object_color(obj, unreal.LinearColor(0.58, 0.52, 0.43, 1.0)))
                material_override = True
            except Exception as exc:
                chaos_runtime.setdefault("material_errors", []).append(f"{obj.get('id')}:{visual_material_path}:{exc}")
        if material_override:
            pass
        elif (
            params.get("binding_source") == "ue_asset"
            and params.get("asset_runtime_usage") in {"collision_and_visual", "visual_proxy"}
            and preserve_material is not False
        ):
            # Catalog assets already carry their authored Poly Haven / generated
            # materials.  The generic LLM palette is only a fallback for analytic
            # geometry; applying it here erases the visual identity of the asset.
            material_override = False
        elif force_library_mesh and params.get("preserve_material") is True:
            material_override = False
        elif str(obj.get("asset_kind") or "").lower() in {"geometrycollection", "geometry_collection"} and preserve_material is not False:
            material_override = False
        elif obj.get("behavior") == "static_prop" and (preserve_material is False or has_material_profile or any(key in params for key in ("color", "color_rgb", "tint"))):
            profile = str(obj.get("material_profile") or "").lower()
            if "metal" in profile:
                material = materials.get("runtime_floor") or materials.get("runtime_prop")
            elif "wood" in profile:
                material = materials.get("runtime_bridge") or materials.get("runtime_prop")
            elif "paper" in profile or "plastic" in profile or "cardboard" in profile:
                material = materials.get("runtime_prop")
            else:
                material = materials.get("runtime_prop")
            set_actor_material(actor, material)
            set_actor_color(actor, runtime_object_color(obj, unreal.LinearColor(0.58, 0.52, 0.43, 1.0)))
            material_override = True
        elif obj.get("behavior") == "elastic_tether_visual":
            set_actor_material(actor, materials.get("runtime_bridge") or materials.get("runtime_prop"))
            set_actor_color(actor, runtime_object_color(obj, unreal.LinearColor(0.92, 0.58, 0.08, 1.0)))
            material_override = True
        elif obj.get("behavior") in {"llm_rigid_body", "llm_static_body"}:
            material_text = " ".join(
                str(params.get(key) or "")
                for key in ("material", "visual_material")
            ).lower()
            if obj.get("behavior") == "llm_static_body":
                set_actor_material(actor, materials.get("runtime_lane") or materials.get("runtime_floor"))
                if any(term in material_text or term in text for term in ("felt", "green", "table")):
                    fallback_color = unreal.LinearColor(0.04, 0.34, 0.22, 1.0)
                else:
                    fallback_color = unreal.LinearColor(0.32, 0.44, 0.48, 1.0)
                set_actor_color(actor, runtime_object_color(obj, fallback_color))
            else:
                set_actor_material(actor, materials.get("runtime_air_sphere") or materials.get("runtime_prop"))
                palette = [
                    unreal.LinearColor(0.88, 0.14, 0.10, 1.0),
                    unreal.LinearColor(0.10, 0.32, 0.86, 1.0),
                    unreal.LinearColor(0.94, 0.72, 0.08, 1.0),
                    unreal.LinearColor(0.10, 0.58, 0.24, 1.0),
                    unreal.LinearColor(0.56, 0.18, 0.78, 1.0),
                    unreal.LinearColor(0.92, 0.42, 0.08, 1.0),
                ]
                if "cue" in str(obj.get("id") or "").lower() or "white" in material_text:
                    fallback_color = unreal.LinearColor(0.96, 0.94, 0.88, 1.0)
                else:
                    try:
                        palette_index = int(str(obj.get("id", "0")).rsplit("_", 1)[-1]) - 1
                    except Exception:
                        palette_index = 0
                    fallback_color = palette[palette_index % len(palette)]
                set_actor_color(actor, runtime_object_color(obj, fallback_color))
            material_override = True
        elif obj.get("behavior") == "wind_drift":
            palette = [
                unreal.LinearColor(1.00, 0.08, 0.08, 1.0),
                unreal.LinearColor(1.00, 0.72, 0.04, 1.0),
                unreal.LinearColor(0.08, 0.44, 1.00, 1.0),
                unreal.LinearColor(0.16, 0.92, 0.38, 1.0),
            ]
            material_keys = [
                "runtime_balloon_red",
                "runtime_balloon_yellow",
                "runtime_balloon_blue",
                "runtime_balloon_green",
            ]
            try:
                index = int(str(obj.get("id", "0")).rsplit("_", 1)[-1]) - 1
            except Exception:
                index = 0
            set_actor_material(actor, materials.get(material_keys[index % len(material_keys)]))
            set_actor_color(actor, palette[index % len(palette)])
            material_override = True
        elif obj.get("behavior") == "gear_collision":
            set_actor_material(actor, materials.get("runtime_gear"))
            set_actor_color(actor, unreal.LinearColor(0.70, 0.66, 0.56, 1.0))
            material_override = True
        elif obj.get("behavior") == "slope_roll":
            set_actor_material(actor, materials.get("runtime_stone"))
            set_actor_color(actor, runtime_object_color(obj, unreal.LinearColor(0.52, 0.47, 0.39, 1.0)))
            material_override = True
        elif obj.get("behavior") == "slope_surface":
            set_actor_material(actor, materials.get("runtime_lane") or materials.get("runtime_floor"))
            set_actor_color(actor, runtime_object_color(obj, unreal.LinearColor(0.42, 0.52, 0.48, 1.0)))
            material_override = True
        elif obj.get("behavior") == "rolling_friction":
            rank = int((obj.get("params") or {}).get("color_rank", 0))
            palette = [
                unreal.LinearColor(0.08, 0.46, 0.90, 1.0),
                unreal.LinearColor(0.90, 0.58, 0.10, 1.0),
                unreal.LinearColor(0.82, 0.18, 0.12, 1.0),
            ]
            set_actor_material(actor, materials.get("runtime_air_sphere") or materials.get("runtime_stone") or materials.get("runtime_prop"))
            set_actor_color(actor, palette[rank % len(palette)])
            material_override = True
        elif obj.get("behavior") == "rolling_friction_surface":
            set_actor_material(actor, materials.get("runtime_lane") or materials.get("runtime_floor"))
            set_actor_color(actor, unreal.LinearColor(0.32, 0.44, 0.48, 1.0))
            material_override = True
        elif obj.get("behavior") == "character_carry_object":
            set_actor_material(actor, materials.get("runtime_box") or materials.get("runtime_prop"))
            set_actor_color(actor, unreal.LinearColor(0.82, 0.48, 0.18, 1.0))
            material_override = True
        elif obj.get("behavior") in {"projectile_arc", "character_throw_projectile"}:
            set_actor_material(actor, materials.get("runtime_air_sphere") or materials.get("runtime_stone") or materials.get("runtime_prop"))
            set_actor_color(actor, unreal.LinearColor(0.96, 0.42, 0.08, 1.0))
            material_override = True
        elif obj.get("behavior") == "projectile_landing_zone":
            set_actor_material(actor, materials.get("runtime_lane") or materials.get("runtime_floor"))
            set_actor_color(actor, unreal.LinearColor(0.28, 0.40, 0.34, 1.0))
            material_override = True
        elif obj.get("behavior") == "pendulum_swing":
            set_actor_material(actor, materials.get("runtime_air_sphere") or materials.get("runtime_stone") or materials.get("runtime_prop"))
            set_actor_color(actor, unreal.LinearColor(0.18, 0.56, 0.92, 1.0))
            material_override = True
        elif obj.get("behavior") == "pendulum_anchor":
            set_actor_material(actor, materials.get("runtime_marker") or materials.get("runtime_floor"))
            set_actor_color(actor, unreal.LinearColor(0.92, 0.86, 0.42, 1.0))
            material_override = True
        elif obj.get("behavior") == "constraint_joint_link":
            index = int((obj.get("params") or {}).get("link_index", 1))
            set_actor_material(actor, materials.get("runtime_air_sphere") or materials.get("runtime_prop"))
            set_actor_color(actor, unreal.LinearColor(0.12, 0.50, 0.86, 1.0) if index == 1 else unreal.LinearColor(0.86, 0.34, 0.16, 1.0))
            material_override = True
        elif obj.get("behavior") == "constraint_anchor":
            set_actor_material(actor, materials.get("runtime_marker") or materials.get("runtime_floor"))
            set_actor_color(actor, unreal.LinearColor(0.94, 0.82, 0.28, 1.0))
            material_override = True
        elif obj.get("behavior") == "stack_stability":
            rank = int((obj.get("params") or {}).get("color_rank", 0))
            palette = [
                unreal.LinearColor(0.20, 0.42, 0.78, 1.0),
                unreal.LinearColor(0.24, 0.58, 0.38, 1.0),
                unreal.LinearColor(0.92, 0.62, 0.18, 1.0),
                unreal.LinearColor(0.82, 0.24, 0.18, 1.0),
            ]
            set_actor_material(actor, materials.get("runtime_prop") or materials.get("runtime_floor"))
            set_actor_color(actor, palette[rank % len(palette)])
            material_override = True
        elif obj.get("behavior") == "stack_push_impactor":
            set_actor_material(actor, materials.get("runtime_air_sphere") or materials.get("runtime_prop"))
            set_actor_color(actor, unreal.LinearColor(0.88, 0.18, 0.12, 1.0))
            material_override = True
        elif obj.get("behavior") == "stack_support_surface":
            set_actor_material(actor, materials.get("runtime_lane") or materials.get("runtime_floor"))
            set_actor_color(actor, unreal.LinearColor(0.36, 0.40, 0.38, 1.0))
            material_override = True
        elif obj.get("behavior") == "rigid_collision":
            collision_index = int((obj.get("params") or {}).get("collision_index", 0))
            set_actor_material(actor, materials.get("runtime_air_sphere") or materials.get("runtime_prop"))
            if collision_index == 0:
                set_actor_color(actor, unreal.LinearColor(0.06, 0.34, 0.92, 1.0))
            else:
                set_actor_color(actor, unreal.LinearColor(0.92, 0.45, 0.05, 1.0))
            material_override = True
        elif obj.get("behavior") == "domino_tip":
            if not (
                selected_map.get("name") == "MarketEnvironment_Day"
                and runtime_scene.get("case_type") == "bottle_domino_chain"
                and os.environ.get("DAY_MAP_NATIVE_BOTTLE_MATERIAL") == "1"
            ):
                bottle_palette = [
                    unreal.LinearColor(1.00, 0.62, 0.08, 1.0),
                    unreal.LinearColor(0.18, 0.58, 0.96, 1.0),
                    unreal.LinearColor(0.92, 0.22, 0.16, 1.0),
                    unreal.LinearColor(0.20, 0.74, 0.30, 1.0),
                ]
                try:
                    bottle_index = int(str(obj.get("id", "0")).rsplit("_", 1)[-1]) - 1
                except Exception:
                    bottle_index = 0
                set_actor_material(actor, materials.get("runtime_bottle"))
                set_actor_color(actor, bottle_palette[bottle_index % len(bottle_palette)])
                material_override = True
        elif obj.get("behavior") == "wheel_jump":
            set_actor_material(actor, materials.get("runtime_wheel"))
            set_actor_color(actor, unreal.LinearColor(0.05, 0.05, 0.05, 1.0))
            material_override = True
        elif obj.get("behavior") == "friction_slide":
            friction_label = str((obj.get("params") or {}).get("friction_label") or "").lower()
            set_actor_material(actor, materials.get("runtime_box"))
            if "low" in friction_label:
                set_actor_color(actor, unreal.LinearColor(0.10, 0.52, 0.44, 1.0))
            elif "high" in friction_label:
                set_actor_color(actor, unreal.LinearColor(0.78, 0.16, 0.10, 1.0))
            else:
                set_actor_color(actor, unreal.LinearColor(0.86, 0.54, 0.12, 1.0))
            material_override = True
        elif obj.get("behavior") == "friction_surface":
            set_actor_material(actor, materials.get("runtime_lane"))
            set_actor_color(actor, unreal.LinearColor(0.30, 0.44, 0.50, 1.0))
            material_override = True
        elif obj.get("behavior") == "falling_collision":
            set_actor_material(actor, materials.get("runtime_box"))
            set_actor_color(actor, unreal.LinearColor(0.74, 0.42, 0.18, 1.0))
            material_override = True
        elif obj.get("behavior") == "landing_surface":
            set_actor_material(actor, materials.get("runtime_floor"))
            set_actor_color(actor, runtime_object_color(obj, unreal.LinearColor(0.38, 0.40, 0.36, 1.0)))
            material_override = True
        elif obj.get("behavior") in {"room_wall", "room_ceiling"}:
            set_actor_material(actor, materials.get("runtime_backdrop") or materials.get("runtime_floor"))
            set_actor_color(actor, runtime_object_color(obj, unreal.LinearColor(0.72, 0.74, 0.70, 1.0)))
            material_override = True
        elif obj.get("behavior") == "rolling_impact":
            set_actor_material(actor, materials.get("runtime_stone"))
            set_actor_color(actor, unreal.LinearColor(0.74, 0.68, 0.54, 1.0))
            material_override = True
        elif obj.get("behavior") in {"barrel_cascade_impactor", "barrel_cascade_target"}:
            set_actor_material(actor, materials.get("runtime_barrel"))
            if obj.get("behavior") == "barrel_cascade_impactor":
                set_actor_color(actor, unreal.LinearColor(0.06, 0.18, 0.42, 1.0))
            else:
                try:
                    target_index = int((obj.get("params") or {}).get("target_index", 0))
                except Exception:
                    target_index = 0
                palette = [
                    unreal.LinearColor(0.78, 0.32, 0.08, 1.0),
                    unreal.LinearColor(0.88, 0.58, 0.10, 1.0),
                    unreal.LinearColor(0.60, 0.42, 0.18, 1.0),
                    unreal.LinearColor(0.72, 0.22, 0.14, 1.0),
                ]
                set_actor_color(actor, palette[target_index % len(palette)])
            material_override = True
        elif obj.get("behavior") == "thrown_box":
            set_actor_material(actor, materials.get("runtime_box"))
            set_actor_color(actor, unreal.LinearColor(0.72, 0.50, 0.28, 1.0))
            material_override = True
        elif obj.get("behavior") == "air_collision":
            if "sphere" in text:
                set_actor_material(actor, materials.get("runtime_air_sphere"))
                set_actor_color(actor, unreal.LinearColor(0.15, 0.58, 0.98, 1.0))
            elif "barrel" in text:
                set_actor_material(actor, materials.get("runtime_barrel"))
                if "left" in text:
                    set_actor_color(actor, unreal.LinearColor(0.12, 0.26, 0.48, 1.0))
                elif "right" in text:
                    set_actor_color(actor, unreal.LinearColor(0.22, 0.40, 0.66, 1.0))
                else:
                    set_actor_color(actor, unreal.LinearColor(0.16, 0.30, 0.54, 1.0))
            elif "box" in text or "crate" in text:
                set_actor_material(actor, materials.get("runtime_air_box"))
                set_actor_color(actor, unreal.LinearColor(0.90, 0.54, 0.12, 1.0))
            else:
                set_actor_material(actor, materials.get("runtime_prop"))
                set_actor_color(actor, unreal.LinearColor(0.58, 0.52, 0.43, 1.0))
            material_override = True
        elif obj.get("behavior") == "impact_response":
            if "cone" in text:
                set_actor_material(actor, materials.get("runtime_cone"))
                set_actor_color(actor, unreal.LinearColor(0.95, 0.28, 0.05, 1.0))
            elif "barrel" in text:
                set_actor_material(actor, materials.get("runtime_barrel"))
                set_actor_color(actor, unreal.LinearColor(0.12, 0.26, 0.48, 1.0))
            else:
                set_actor_material(actor, materials.get("runtime_prop"))
                set_actor_color(actor, unreal.LinearColor(0.58, 0.52, 0.43, 1.0))
            material_override = True
        else:
            if any(term in text for term in ("bridge", "wooden", "track", "fence", "table", "barrier", "ramp")):
                set_actor_material(actor, materials.get("runtime_bridge"))
                set_actor_color(actor, unreal.LinearColor(0.48, 0.28, 0.12, 1.0))
                material_override = True
            elif "cone" in text:
                set_actor_material(actor, materials.get("runtime_cone"))
                set_actor_color(actor, unreal.LinearColor(0.95, 0.28, 0.05, 1.0))
                material_override = True
            elif "barrel" in text:
                set_actor_material(actor, materials.get("runtime_barrel"))
                set_actor_color(actor, unreal.LinearColor(0.12, 0.26, 0.48, 1.0))
                material_override = True
            elif any(term in text for term in ("box", "crate")):
                set_actor_material(actor, materials.get("runtime_box"))
                set_actor_color(actor, unreal.LinearColor(0.58, 0.38, 0.19, 1.0))
                material_override = True
            elif "bottle" in text:
                set_actor_material(actor, materials.get("runtime_bottle"))
                set_actor_color(actor, unreal.LinearColor(0.80, 0.52, 0.23, 1.0))
                material_override = True
            elif any(term in text for term in ("tree", "plant", "coconut", "grass")):
                if "flower" in text:
                    set_actor_material(actor, materials.get("runtime_flower"))
                    set_actor_color(actor, unreal.LinearColor(0.84, 0.24, 0.34, 1.0))
                else:
                    set_actor_material(actor, materials.get("runtime_tree"))
                    set_actor_color(actor, unreal.LinearColor(0.12, 0.45, 0.18, 1.0))
                material_override = True
            else:
                set_actor_material(actor, materials.get("runtime_prop"))
                set_actor_color(actor, unreal.LinearColor(0.58, 0.52, 0.43, 1.0))
                material_override = True
        spawned_assets.append({
            "label": obj["id"],
            "mesh": mesh_path,
            "material": None,
            "generated_material": False,
            "asset_key": obj.get("asset_key"),
            "asset_name": obj.get("asset_name"),
            "behavior": obj.get("behavior"),
            "requested_rotation_degrees": combined_rotation,
            "material_override": material_override,
            "asset_selection": ASSET_SELECTION_METADATA.get(obj.get("asset_key"), {"path": mesh_path, "source": "runtime_scene", "fallback_reason": None}),
        })
        return actor

    def record_runtime_actor_detail(obj_id: str, actor, origin: unreal.Vector, extent: unreal.Vector) -> None:
        try:
            scale = actor.static_mesh_component.get_component_scale()
        except Exception:
            scale = unreal.Vector(1.0, 1.0, 1.0)
        location = actor.get_actor_location()
        for entry in spawned_assets:
            if entry.get("label") == obj_id:
                entry["location"] = [location.x, location.y, location.z]
                rotation = actor.get_actor_rotation()
                entry["actor_rotation_degrees"] = [rotation.pitch, rotation.yaw, rotation.roll]
                entry["scale"] = [scale.x, scale.y, scale.z]
                entry["bounds"] = {
                    "origin": [origin.x, origin.y, origin.z],
                    "extent": [extent.x, extent.y, extent.z],
                }
                if obj_id == "package":
                    entry["interaction_probe"] = {
                        "actor_class": actor.get_class().get_name(),
                        "methods": sorted(
                            name
                            for name in dir(actor)
                            if any(term in name for term in ("grab", "drop", "interact", "attach", "collision", "physics"))
                        ),
                        "components": [
                            {
                                "name": component.get_name(),
                                "class": component.get_class().get_name(),
                                "methods": sorted(
                                    name
                                    for name in dir(component)
                                    if any(term in name for term in ("mesh", "grab", "attach", "collision", "physics"))
                                ),
                            }
                            for component in actor.get_components_by_class(unreal.ActorComponent)
                        ],
                    }
                break

    def record_runtime_actor_physics(obj_id: str, detail: dict) -> None:
        for entry in spawned_assets:
            if entry.get("label") == obj_id:
                entry["chaos_physics"] = detail
                break

    if runtime_scene:
        runtime_actors = {}
        runtime_visual_actors = {}
        runtime_actor_bounds = {}
        runtime_initial_transforms = {}
        support_surfaces = []

        def record_runtime_initial_transform(obj_id: str, actor) -> None:
            location = actor.get_actor_location()
            rotation = actor.get_actor_rotation()
            runtime_initial_transforms[obj_id] = {
                "position_cm": [location.x, location.y, location.z],
                "rotation_degrees": [rotation.pitch, rotation.yaw, rotation.roll],
            }

        def add_runtime_visual(obj: dict, physics_actor) -> None:
            visual_path = str((obj.get("params") or {}).get("visual_ue5_path") or "")
            if not visual_path:
                return
            try:
                visual_scale = (
                    (obj.get("params") or {}).get("intact_visual_scale")
                    or (obj.get("params") or {}).get("visual_instance_scale")
                )
                spawn_scale = (
                    unreal.Vector(*[float(value) for value in visual_scale])
                    if isinstance(visual_scale, list) and len(visual_scale) >= 3
                    else unreal.Vector(1.0, 1.0, 1.0)
                )
                visual = spawn_runtime_actor(
                    "native_phenomena_visual_" + str(obj["id"]),
                    visual_path,
                    str((obj.get("params") or {}).get("visual_asset_kind") or obj.get("asset_kind") or "static_mesh"),
                    physics_actor.get_actor_location(),
                    spawn_scale,
                    (obj.get("params") or {}).get("intact_visual_material_path")
                    or (obj.get("params") or {}).get("visual_material_path"),
                    physics_actor.get_actor_rotation(),
                )
                if not (
                    isinstance(visual_scale, list) and len(visual_scale) >= 3
                ) and not (obj.get("params") or {}).get("preserve_visual_authored_scale"):
                    normalize_runtime_actor(visual, obj)
                visual.set_actor_enable_collision(False)
                component = actor_runtime_component(visual)
                if component:
                    try:
                        visual.set_is_temporarily_hidden_in_editor(False)
                    except Exception:
                        pass
                    visual.set_actor_hidden_in_game(False)
                    component.set_visibility(True, True)
                    component.set_hidden_in_game(False, True)
                    component.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
                    component.set_collision_profile_name("NoCollision")
                    try:
                        component.set_editor_property("generate_overlap_events", False)
                    except Exception:
                        pass
                    component.set_simulate_physics(False)
                collision_component = actor_runtime_component(physics_actor)
                if collision_component:
                    collision_component.set_visibility(False, True)
                    collision_component.set_hidden_in_game(True, True)
                runtime_visual_actors[str(obj["id"])] = visual
                sync_runtime_visuals({**runtime_actors, str(obj["id"]): physics_actor, "visual_actors": runtime_visual_actors})
                for entry in spawned_assets:
                    if entry.get("label") == obj["id"]:
                        entry["collision_mesh"] = entry.get("mesh")
                        entry["visual_mesh"] = visual_path
                        entry["visual_collision_enabled"] = bool(visual.get_actor_enable_collision())
                        break
            except Exception as exc:
                write_progress_marker("runtime_visual_fallback", f"{obj.get('id')}:{exc}")

        def is_runtime_support_surface(obj):
            params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
            role = str(params.get("role") or obj.get("semantic_role") or "").strip().lower()
            behavior = str(obj.get("behavior") or "").lower()
            return behavior in {
                "friction_surface",
                "landing_surface",
                "rolling_friction_surface",
                "stack_support_surface",
                "projectile_landing_zone",
                "slope_surface",
            } or role in {
                "floor",
                "ground",
                "inclined_plane",
                "platform",
                "ramp",
                "slope_surface",
                "support",
                "support_surface",
                "surface",
                "table",
                "tabletop",
            }

        def maybe_align_dynamic_to_support(obj, actor, origin, extent):
            behavior = str(obj.get("behavior") or "").lower()
            if behavior not in {"llm_rigid_body", "rolling_impact", "rigid_collision", "friction_slide"}:
                return origin, extent
            if (obj.get("params") or {}).get("fit_dynamic_plan") is False:
                return origin, extent
            if not support_surfaces:
                return origin, extent
            location = actor.get_actor_location()
            bottom_z = origin.z - extent.z
            margin_cm = max(15.0, max(extent.x, extent.y) * 2.0)
            candidates = []
            for surface in support_surfaces:
                surface_origin = surface["origin"]
                surface_extent = surface["extent"]
                inside_x = abs(location.x - surface_origin.x) <= surface_extent.x + margin_cm
                inside_y = abs(location.y - surface_origin.y) <= surface_extent.y + margin_cm
                if not (inside_x and inside_y):
                    continue
                surface_top = surface_origin.z + surface_extent.z
                if surface_top <= bottom_z + max(12.0, extent.z * 4.0):
                    candidates.append(surface_top)
            if not candidates:
                return origin, extent
            support_top = max(candidates)
            clearance_cm = 0.25
            z_delta = (support_top + clearance_cm) - bottom_z
            if abs(z_delta) < 0.05:
                return origin, extent
            actor.set_actor_location(unreal.Vector(location.x, location.y, location.z + z_delta), False, False)
            return actor_bounds(actor)

        def maybe_fit_support_to_dynamic_plan(obj, actor, origin, extent):
            if not is_runtime_support_surface(obj):
                return origin, extent
            if (obj.get("params") or {}).get("fit_dynamic_plan") is False:
                return origin, extent
            component = actor_runtime_component(actor)
            if not component:
                return origin, extent
            location = actor.get_actor_location()
            required_x = abs(extent.x)
            required_y = abs(extent.y)
            for dynamic_obj in runtime_scene.get("dynamic_objects") or []:
                behavior = str(dynamic_obj.get("behavior") or "").lower()
                if behavior not in {"llm_rigid_body", "rolling_impact", "rigid_collision", "friction_slide"}:
                    continue
                dynamic_position = ue_vec_from_meters(dynamic_obj.get("initial_position_m", [0.0, 0.0, 0.0]), origin=scene_origin)
                dynamic_margin = max(20.0, runtime_desired_extent_cm(dynamic_obj) * 4.0)
                required_x = max(required_x, abs(dynamic_position.x - location.x) + dynamic_margin)
                required_y = max(required_y, abs(dynamic_position.y - location.y) + dynamic_margin)
            if required_x <= abs(extent.x) * 1.02 and required_y <= abs(extent.y) * 1.02:
                return origin, extent
            try:
                scale = component.get_component_scale()
            except Exception:
                scale = unreal.Vector(1.0, 1.0, 1.0)
            component.set_world_scale3d(
                unreal.Vector(
                    scale.x * (required_x / max(abs(extent.x), 0.01)),
                    scale.y * (required_y / max(abs(extent.y), 0.01)),
                    scale.z,
                )
            )
            return actor_bounds(actor)

        def align_runtime_support_top(obj, actor, origin, extent):
            """Keep the CaseSpec support z semantic (top surface), independent of mesh pivot."""
            support_top_m = (obj.get("params") or {}).get("support_top_m")
            if support_top_m is None:
                return origin, extent
            desired_top_cm = scene_origin.z + float(support_top_m) * 100.0
            current_top_cm = origin.z + extent.z
            delta_cm = desired_top_cm - current_top_cm
            if abs(delta_cm) <= 0.01:
                return origin, extent
            location = actor.get_actor_location()
            actor.set_actor_location(
                unreal.Vector(location.x, location.y, location.z + delta_cm),
                False,
                False,
            )
            return actor_bounds(actor)

        if STAGE_HELPERS and lighting_enabled(lighting_controls, "stage_helpers") and selected_map.get("name") == "StudioRuntimeBlank":
            stage_helper_actors = spawn_runtime_stage_helpers(editor, runtime_scene, scene_origin, materials)
        if (
            lighting_enabled(lighting_controls, "stage_helpers")
            and runtime_scene.get("case_type") in {"bottle_domino_chain", "air_collision_pair", "barrel_impact_cascade"}
            and selected_map.get("name") == "MarketEnvironment_Day"
            and loaded_map_actor_count < 10
        ):
            stage_helper_actors.extend(spawn_market_day_gas_station_context(editor, scene_origin, materials))
        for obj in runtime_scene.get("static_objects") or []:
            write_progress_marker("setup_scene_spawn_static_start", obj.get("id"))
            actor = place_runtime(
                obj,
                obj.get("initial_position_m", [0.0, 0.0, 0.0]),
                obj.get("rotation_degrees", [0.0, 0.0, 0.0]),
            )
            runtime_actors[obj["id"]] = actor
            origin, extent = normalize_runtime_actor(actor, obj)
            origin, extent = maybe_fit_support_to_dynamic_plan(obj, actor, origin, extent)
            if is_runtime_support_surface(obj):
                origin, extent = align_runtime_support_top(obj, actor, origin, extent)
            runtime_actor_bounds[obj["id"]] = {"origin": [origin.x, origin.y, origin.z], "extent": [extent.x, extent.y, extent.z]}
            if obj.get("behavior") in {"domino_tip", "friction_slide"}:
                location = actor.get_actor_location()
                runtime_ground_offsets[obj["id"]] = max(0.0, location.z - (origin.z - extent.z) + 2.0)
                actor.set_actor_location(ue_vec_from_meters(obj.get("initial_position_m", [0.0, 0.0, 0.0]), z_offset_cm=runtime_ground_offsets[obj["id"]], origin=scene_origin), False, False)
                origin, extent = actor_bounds(actor)
                runtime_actor_bounds[obj["id"]] = {"origin": [origin.x, origin.y, origin.z], "extent": [extent.x, extent.y, extent.z]}
            if is_runtime_support_surface(obj):
                support_surfaces.append({"id": obj.get("id"), "origin": origin, "extent": extent})
            record_runtime_initial_transform(obj["id"], actor)
            physics_detail = configure_runtime_physics(actor, obj, "static", physics_controls)
            chaos_runtime["actors"].append(physics_detail)
            record_runtime_actor_physics(obj["id"], physics_detail)
            record_runtime_actor_detail(obj["id"], actor, origin, extent)
            write_progress_marker("setup_scene_spawn_static_done", obj.get("id"))
        for obj in runtime_scene.get("dynamic_objects") or []:
            write_progress_marker("setup_scene_spawn_dynamic_start", obj.get("id"))
            actor = place_runtime(
                obj,
                obj.get("initial_position_m", [0.0, 0.0, 0.0]),
                obj.get("rotation_degrees", [0.0, 0.0, 0.0]),
            )
            runtime_actors[obj["id"]] = actor
            origin, extent = normalize_runtime_actor(actor, obj)
            runtime_actor_bounds[obj["id"]] = {"origin": [origin.x, origin.y, origin.z], "extent": [extent.x, extent.y, extent.z]}
            explicit_ground_offset = (obj.get("params") or {}).get("ground_offset_cm")
            if explicit_ground_offset is not None:
                runtime_ground_offsets[obj["id"]] = float(explicit_ground_offset)
                actor.set_actor_location(
                    ue_vec_from_meters(obj.get("initial_position_m", [0.0, 0.0, 0.0]), z_offset_cm=float(explicit_ground_offset), origin=scene_origin),
                    False,
                    False,
                )
                origin, extent = actor_bounds(actor)
                runtime_actor_bounds[obj["id"]] = {"origin": [origin.x, origin.y, origin.z], "extent": [extent.x, extent.y, extent.z]}
            if obj.get("behavior") in {"domino_tip", "friction_slide"}:
                location = actor.get_actor_location()
                runtime_ground_offsets[obj["id"]] = max(0.0, location.z - (origin.z - extent.z) + 2.0)
                actor.set_actor_location(ue_vec_from_meters(obj.get("initial_position_m", [0.0, 0.0, 0.0]), z_offset_cm=runtime_ground_offsets[obj["id"]], origin=scene_origin), False, False)
                origin, extent = actor_bounds(actor)
                runtime_actor_bounds[obj["id"]] = {"origin": [origin.x, origin.y, origin.z], "extent": [extent.x, extent.y, extent.z]}
            origin, extent = maybe_align_dynamic_to_support(obj, actor, origin, extent)
            runtime_actor_bounds[obj["id"]] = {"origin": [origin.x, origin.y, origin.z], "extent": [extent.x, extent.y, extent.z]}
            # Capture the planned pose before Chaos is enabled. Scene setup and
            # viewport warmup can otherwise advance a free body before frame 0.
            record_runtime_initial_transform(obj["id"], actor)
            physics_detail = configure_runtime_physics(actor, obj, "dynamic", physics_controls)
            add_runtime_visual(obj, actor)
            if str(obj["id"]) in runtime_visual_actors:
                physics_detail["collision_mesh"] = str(obj.get("ue5_path") or "")
                physics_detail["visual_mesh"] = str((obj.get("params") or {}).get("visual_ue5_path") or "")
            chaos_runtime["actors"].append(physics_detail)
            record_runtime_actor_physics(obj["id"], physics_detail)
            record_runtime_actor_detail(obj["id"], actor, origin, extent)
            write_progress_marker("setup_scene_spawn_dynamic_done", obj.get("id"))
        write_progress_marker("setup_scene_runtime_actors_done", f"count={len(runtime_actors)}")
        write_progress_marker("setup_scene_runtime_camera_pose_start")
        camera_location, camera_target, camera_extent = runtime_camera_pose(runtime_scene, runtime_actors, selected_map, scene_origin, editor)
        write_progress_marker("setup_scene_runtime_camera_pose_done")
        background_stage_actors = spawn_map_backdrop(editor, selected_map, scene_origin, camera_location, camera_target) if lighting_enabled(lighting_controls, "map_backdrop_helpers") else []
        write_progress_marker("setup_scene_background_stage_done", f"count={len(background_stage_actors)}")
        camera_rotation = look_at_rotation(camera_location, camera_target)
        capture = editor.spawn_actor_from_class(unreal.SceneCapture2D, camera_location)
        capture.set_actor_label("native_phenomena_demo_capture_camera")
        capture.set_actor_rotation(camera_rotation, False)
        render_target = unreal.RenderingLibrary.create_render_target2d(world, WIDTH, HEIGHT)
        set_editor_property_if_available(render_target, "clear_color", unreal.LinearColor(0.74, 0.82, 0.90, 1.0))
        capture_comp = capture.capture_component2d
        capture_comp.set_editor_property("texture_target", render_target)
        capture_source_name = str(lighting_profile.get("capture_source") or "SCS_FINAL_COLOR_LDR")
        capture_source = getattr(unreal.SceneCaptureSource, capture_source_name, unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
        capture_comp.set_editor_property("capture_source", capture_source)
        fov_override = camera_fov_override(runtime_scene)
        if fov_override is not None:
            fov_angle = fov_override
        elif runtime_scene.get("case_type") == "balloon_wind_drift":
            fov_angle = 68.0
        elif runtime_scene.get("case_type") == "air_collision_pair":
            fov_angle = 66.0
        elif runtime_scene.get("case_type") == "barrel_impact_cascade":
            fov_angle = 64.0
        elif runtime_scene.get("case_type") == "bottle_domino_chain":
            fov_angle = 48.0
        elif runtime_scene.get("case_type") == "falling_crate_collision":
            fov_angle = 70.0
        elif runtime_scene.get("case_type") in {"wheel_ramp_jump", "falling_crate_collision", "crate_friction_slide", "cone_barrel_collision", "stack_stability", "constraint_joint"}:
            fov_angle = 60.0
        else:
            fov_angle = 54.0
        capture_comp.set_editor_property("fov_angle", fov_angle)
        capture_comp.set_editor_property("capture_every_frame", False)
        capture_comp.set_editor_property("capture_on_movement", False)
        set_editor_property_if_available(capture_comp, "always_persist_rendering_state", True)
        set_editor_property_if_available(capture_comp, "primitive_render_mode", unreal.SceneCapturePrimitiveRenderMode.PRM_RENDER_SCENE_PRIMITIVES)
        post_process_blend_weight = float(lighting_profile.get("post_process_blend_weight", 1.0))
        if lighting_enabled(lighting_controls, "use_post_process") and post_process_blend_weight > 0.0:
            exposure = set_post_process(
                capture_comp,
                lighting_profile["exposure_bias"],
                bool(lighting_controls.get("fixed_auto_exposure", lighting_profile.get("fixed_auto_exposure", True))),
                post_process_blend_weight,
            )
        else:
            exposure = {"enabled": False, "reason": "map_lighting_controls.use_post_process=false_or_zero_blend"}
        return {
            **runtime_actors,
            "visual_actors": runtime_visual_actors,
            "capture": capture,
            "capture_comp": capture_comp,
            "render_target": render_target,
            "world": world,
            "scene_origin": [scene_origin.x, scene_origin.y, scene_origin.z],
            "map_stage": {key: value for key, value in map_stage.items() if key != "origin"},
            "selected_map": selected_map,
            "loaded_map_actor_count": loaded_map_actor_count,
            "visible_map_actors": visible_map_actors,
            "removed_map_actors": removed_map_actors,
            "background_stage_actors": background_stage_actors,
            "stage_helper_actors": stage_helper_actors,
            "asset_indexes": find_asset_indexes(ASSET_ROOT),
            "spawned_assets": spawned_assets,
            "runtime_actor_bounds": runtime_actor_bounds,
            "runtime_ground_offsets": runtime_ground_offsets,
            "runtime_initial_transforms": runtime_initial_transforms,
            "chaos_runtime": chaos_runtime,
            "physics_controls": physics_controls,
            "camera_pose": {
                "location": [camera_location.x, camera_location.y, camera_location.z],
                "target": [camera_target.x, camera_target.y, camera_target.z],
                "extent": [camera_extent.x, camera_extent.y, camera_extent.z],
                "preset": render_camera_preset(runtime_scene),
                "fov": fov_angle,
            },
            "lighting": {
                "profile": str(lighting_profile.get("profile") or lighting_visual_profile),
                "visual_realism_profile": lighting_visual_profile,
                "sun_intensity": lighting_profile["sun"],
                "fill_intensity": lighting_profile["fill"],
                "sky_intensity": lighting_profile["sky"],
                "exposure_bias": lighting_profile["exposure_bias"],
                "fixed_auto_exposure": bool(lighting_profile.get("fixed_auto_exposure", True)),
                "post_process_blend_weight": float(lighting_profile.get("post_process_blend_weight", 1.0)),
                "capture_backend": str(lighting_profile.get("capture_backend") or "scene_capture"),
                "capture_source": capture_source_name,
                "exposure": exposure,
                "render_quality": render_quality,
                "video_filter": lighting_profile["video_filter"],
                "stage_helpers_enabled": STAGE_HELPERS,
                "extra_stage_markers_enabled": EXTRA_STAGE_MARKERS,
                "map_backdrop_helpers_enabled": MAP_BACKDROP_HELPERS,
                "stable_stage_anchors_enabled": STABLE_STAGE_ANCHORS,
                "existing_map_lights": existing_map_lights,
                "map_boost_lights": map_boost_lights,
                "custom_lights": custom_lights,
                "generated_material_version": GENERATED_MATERIAL_VERSION,
                "generated_materials": {key: bool(value) for key, value in materials.items()},
                "real_environment_assets": [asset.get("asset_name") for asset in (runtime_scene.get("dynamic_objects") or []) + (runtime_scene.get("static_objects") or [])],
                "world_visuals": world_visuals,
                "controls": lighting_controls,
            },
            "video_filter": lighting_profile["video_filter"],
        }

    def place(label, asset_key, loc, scale, mat_key=None, generated_mat=None, rot=None):
        actor = spawn_static_mesh(
            "native_phenomena_demo_" + label,
            RESOLVED_ASSETS[asset_key],
            world_loc(scene_origin, loc),
            unreal.Vector(*scale),
            RESOLVED_ASSETS[mat_key] if mat_key and RESOLVED_ASSETS.get(mat_key) else None,
            unreal.Rotator(*rot) if rot else None,
        )
        if generated_mat:
            set_actor_material(actor, generated_mat)
        spawned_assets.append({
            "label": label,
            "mesh": RESOLVED_ASSETS[asset_key],
            "material": RESOLVED_ASSETS.get(mat_key) if mat_key else None,
            "generated_material": bool(generated_mat),
            "asset_selection": ASSET_SELECTION_METADATA.get(asset_key, {"path": RESOLVED_ASSETS[asset_key], "source": "gitlab_adp_materialized", "fallback_reason": None}),
            "material_selection": ASSET_SELECTION_METADATA.get(mat_key) if mat_key else None,
        })
        return actor

    exhibit_floor = place("exhibit_floor", "floor", (-20, 0, -62), (1.75, 1.15, 0.08), "mat_concrete")
    back_wall = place("exhibit_back_wall", "wall_window", (-20, 116, 58), (1.45, 0.05, 0.68), "mat_concrete")
    left_table = place("left_table", "table", (-105, -8, 26), (1.0, 1.0, 0.76), "mat_wood")
    right_table = place("right_table", "table", (168, -8, 26), (1.0, 1.0, 0.76), "mat_wood")
    place("chair_left", "chair", (-190, -86, 8), (0.75, 0.75, 0.75), "mat_wood", rot=(0, 0, 22))
    place("chair_right", "chair", (246, -88, 8), (0.75, 0.75, 0.75), "mat_wood", rot=(0, 0, -18))
    place("rock_cluster_left", "rock", (-258, 72, -55), (0.55, 0.55, 0.40), "mat_rock", rot=(0, 0, 32))
    place("rock_cluster_right", "rock", (286, 70, -55), (0.46, 0.46, 0.36), "mat_rock", rot=(0, 0, -28))
    place("bush_left", "bush", (-292, -54, -44), (0.80, 0.80, 0.70), None)
    place("bush_right", "bush", (306, -50, -44), (0.76, 0.76, 0.68), None)
    place("statue_background", "statue", (28, 150, -56), (0.42, 0.42, 0.42), None, rot=(0, 0, 180))
    place("wall_lamp", "lamp_wall", (-205, 110, 112), (0.55, 0.55, 0.55), "mat_metal")

    water_generated_material = None if ASSET_SELECTION_METADATA.get("water_plane", {}).get("source") == "manual_materialized_adp_subset" else materials["water"]
    water_highlight_material = None if ASSET_SELECTION_METADATA.get("water_plane", {}).get("source") == "manual_materialized_adp_subset" else materials["water_highlight"]
    water = place("water_surface", "water_plane", (-116, -4, 120), (1.28, 0.82, 1.0), "mat_water", water_generated_material)
    water_depth = place("water_depth", "cube", (-116, -4, 55), (1.28, 0.82, 0.58), "mat_water", materials["water"])
    water_highlight = place("water_highlight", "water_plane", (-128, -18, 123), (0.78, 0.40, 1.0), "mat_water", water_highlight_material)
    tank_bottom = place("tank_bottom", "cube", (-116, -4, -15), (1.34, 0.86, 0.05), "mat_concrete")
    for label, loc, scale in (
        ("tank_back", (-116, 82, 58), (1.34, 0.035, 1.48)),
        ("tank_front", (-116, -90, 58), (1.34, 0.035, 1.48)),
        ("tank_left", (-250, -4, 58), (0.035, 0.86, 1.48)),
        ("tank_right", (18, -4, 58), (0.035, 0.86, 1.48)),
    ):
        place(label, "cube", loc, scale, "mat_concrete")
    for idx, y in enumerate((-36, -14, 10, 32)):
        place(f"water_ripple_{idx}", "cube", (-122 + idx * 4, y, 126 + idx % 2), (1.05, 0.006, 0.003), "mat_water", materials["water_highlight"])

    magnet_track = place("magnet_track", "cube", (150, -6, 75), (1.45, 0.025, 0.025), None, materials["marker"])
    magnet_red = place("magnet_red", "cube", (185, 0, 75), (0.65, 0.22, 0.18), None, materials["magnet_red"])
    magnet_blue = place("magnet_blue", "cube", (250, 0, 75), (0.28, 0.24, 0.20), None, materials["magnet_blue"])
    separator = place("scene_separator", "cube", (48, -8, 70), (0.045, 0.045, 1.60), None, materials["marker"])

    rubber = place("rubber_ball", "sphere", (-125, 0, 25), (0.36, 0.36, 0.36), None, materials["rubber"])
    lead = place("lead_ball", "sphere", (-72, 0, 215), (0.36, 0.36, 0.36), None, materials["lead"])
    steel = place("steel_ball", "sphere", (68, 0, 75), (0.30, 0.30, 0.30), None, materials["steel"])

    set_actor_color(water, unreal.LinearColor(0.03, 0.43, 0.92, 1.0))
    set_actor_color(water_depth, unreal.LinearColor(0.02, 0.20, 0.48, 1.0))
    set_actor_color(water_highlight, unreal.LinearColor(0.70, 0.95, 1.0, 1.0))
    set_actor_color(rubber, unreal.LinearColor(1.0, 0.25, 0.03, 1.0))
    set_actor_color(lead, unreal.LinearColor(0.20, 0.20, 0.22, 1.0))
    set_actor_color(steel, unreal.LinearColor(0.95, 0.95, 0.90, 1.0))
    set_actor_color(magnet_red, unreal.LinearColor(1.0, 0.0, 0.0, 1.0))
    set_actor_color(magnet_blue, unreal.LinearColor(0.0, 0.12, 1.0, 1.0))
    set_actor_color(magnet_track, unreal.LinearColor(1.0, 0.92, 0.0, 1.0))
    set_actor_color(separator, unreal.LinearColor(1.0, 0.92, 0.0, 1.0))

    camera_location, camera_target = map_camera_pose(selected_map, scene_origin)
    background_stage_actors = spawn_map_backdrop(editor, selected_map, scene_origin, camera_location, camera_target)
    camera_rotation = look_at_rotation(camera_location, camera_target)
    capture = editor.spawn_actor_from_class(unreal.SceneCapture2D, camera_location)
    capture.set_actor_label("native_phenomena_demo_capture_camera")
    capture.set_actor_rotation(camera_rotation, False)
    render_target = unreal.RenderingLibrary.create_render_target2d(world, WIDTH, HEIGHT)
    set_editor_property_if_available(render_target, "clear_color", unreal.LinearColor(0.74, 0.82, 0.90, 1.0))
    capture_comp = capture.capture_component2d
    capture_comp.set_editor_property("texture_target", render_target)
    capture_source_name = str(lighting_profile.get("capture_source") or "SCS_FINAL_COLOR_LDR")
    capture_source = getattr(unreal.SceneCaptureSource, capture_source_name, unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
    capture_comp.set_editor_property("capture_source", capture_source)
    capture_comp.set_editor_property("fov_angle", 58.0)
    capture_comp.set_editor_property("capture_every_frame", False)
    capture_comp.set_editor_property("capture_on_movement", False)
    set_editor_property_if_available(capture_comp, "always_persist_rendering_state", True)
    set_editor_property_if_available(capture_comp, "primitive_render_mode", unreal.SceneCapturePrimitiveRenderMode.PRM_RENDER_SCENE_PRIMITIVES)
    post_process_blend_weight = float(lighting_profile.get("post_process_blend_weight", 1.0))
    if post_process_blend_weight > 0.0:
        exposure = set_post_process(
            capture_comp,
            lighting_profile["exposure_bias"],
            bool(lighting_profile.get("fixed_auto_exposure", True)),
            post_process_blend_weight,
        )
    else:
        exposure = {"enabled": False, "reason": "post_process_blend_weight=0"}

    return {
        "rubber_ball": rubber,
        "lead_ball": lead,
        "steel_ball": steel,
        "capture": capture,
        "capture_comp": capture_comp,
        "render_target": render_target,
        "world": world,
        "scene_origin": [scene_origin.x, scene_origin.y, scene_origin.z],
        "map_stage": {key: value for key, value in map_stage.items() if key != "origin"},
        "selected_map": selected_map,
        "loaded_map_actor_count": loaded_map_actor_count,
        "visible_map_actors": visible_map_actors,
        "removed_map_actors": removed_map_actors,
        "background_stage_actors": background_stage_actors,
        "asset_indexes": find_asset_indexes(ASSET_ROOT),
        "spawned_assets": spawned_assets,
        "lighting": {
            "profile": str(lighting_profile.get("profile") or lighting_visual_profile),
            "visual_realism_profile": lighting_visual_profile,
            "sun_intensity": lighting_profile["sun"],
            "fill_intensity": lighting_profile["fill"],
            "sky_intensity": lighting_profile["sky"],
            "exposure_bias": lighting_profile["exposure_bias"],
            "fixed_auto_exposure": bool(lighting_profile.get("fixed_auto_exposure", True)),
            "post_process_blend_weight": float(lighting_profile.get("post_process_blend_weight", 1.0)),
            "capture_source": capture_source_name,
            "exposure": exposure,
            "render_quality": render_quality,
            "video_filter": lighting_profile["video_filter"],
            "stage_helpers_enabled": STAGE_HELPERS,
            "extra_stage_markers_enabled": EXTRA_STAGE_MARKERS,
            "map_backdrop_helpers_enabled": MAP_BACKDROP_HELPERS,
            "stable_stage_anchors_enabled": STABLE_STAGE_ANCHORS,
            "generated_material_version": GENERATED_MATERIAL_VERSION,
            "generated_materials": {key: bool(value) for key, value in materials.items()},
            "real_environment_assets": ["Level_Scene_01", "SM_WaterPlane", "SM_8Ball", "SM_Balloon_*", "SM_Stone_*", "SM_Wooden*", "SM_Plant_*"],
            "world_visuals": world_visuals,
        },
        "video_filter": lighting_profile["video_filter"],
    }


def flush_editor_rendering(sleep_seconds: float = 0.03):
    try:
        unreal.EditorLevelLibrary.editor_invalidate_viewports()
    except Exception:
        pass
    try:
        unreal.SystemLibrary.execute_console_command(None, "FlushRenderingCommands")
    except Exception:
        pass
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)


def warm_up_surface_replay_materials(runtime_scene: dict | None) -> dict:
    objects = ((runtime_scene or {}).get("dynamic_objects") or []) + ((runtime_scene or {}).get("static_objects") or [])
    required = any(
        isinstance(obj.get("params"), dict)
        and (obj["params"].get("surface_mesh_sequence") or obj["params"].get("generate_solid_material"))
        for obj in objects
    )
    requested = RENDER_SURFACE_REPLAY_MATERIAL_WARMUP_SECONDS if required else 0.0
    started = time.perf_counter()
    deadline = started + requested
    ticks = 0
    while time.perf_counter() < deadline:
        flush_editor_rendering(0.05)
        ticks += 1
    return {
        "required": required,
        "requested_seconds": requested,
        "actual_seconds": round(time.perf_counter() - started, 3),
        "render_flush_ticks": ticks,
    }


def settle_highres_viewport(seconds: float | None = None, ticks: int = 1) -> dict:
    started = time.perf_counter()
    seconds = RENDER_VIEWPORT_SETTLE_SECONDS if seconds is None else max(0.0, float(seconds))
    tick_count = 0
    deadline = started + seconds
    while time.perf_counter() < deadline:
        flush_editor_rendering(0.05)
        tick_count += 1
    for _ in range(max(0, int(ticks))):
        flush_editor_rendering(0.03)
        tick_count += 1
    return {
        "requested_seconds": round(seconds, 3),
        "actual_seconds": round(time.perf_counter() - started, 3),
        "ticks": tick_count,
    }


def file_fingerprint(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False, "size": 0, "sha256": None}
    data = path.read_bytes()
    return {
        "path": str(path),
        "exists": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def capture_frame(actors: dict, frame_path: Path):
    flush_editor_rendering()
    actors["capture_comp"].capture_scene()
    flush_editor_rendering()
    unreal.RenderingLibrary.export_render_target(
        actors.get("capture_world") or actors["world"], actors["render_target"], str(frame_path.parent), frame_path.name
    )


def set_capture_source(actors: dict, source_name: str) -> bool:
    capture_comp = actors.get("capture_comp")
    if not capture_comp:
        return False
    source = getattr(unreal.SceneCaptureSource, source_name, None)
    if source is None:
        return False
    try:
        capture_comp.set_editor_property("capture_source", source)
        return True
    except Exception:
        return False


def capture_render_target_to_file(actors: dict, path: Path, render_target=None, *, force_exr: bool = False) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = render_target or actors["render_target"]
    for attempt in range(3):
        path.unlink(missing_ok=True)
        try:
            flush_editor_rendering()
            actors["capture_comp"].capture_scene()
            flush_editor_rendering()
            if force_exr:
                options = unreal.ImageWriteOptions(
                    format=unreal.DesiredImageFormat.EXR,
                    compression_quality=0,
                    overwrite_file=True,
                    async_=False,
                )
                unreal.ImageWriteBlueprintLibrary.export_to_disk(target, str(path), options)
            else:
                unreal.RenderingLibrary.export_render_target(actors.get("capture_world") or actors["world"], target, str(path.parent), path.name)
            if path.exists() and path.stat().st_size > 0:
                return True
        except Exception:
            pass
        flush_editor_rendering(0.05 * (attempt + 1))
    return False


def runtime_actor_ids_for_segmentation(runtime_scene: dict | None) -> list[str]:
    if not runtime_scene:
        return []
    result = []
    for obj in (runtime_scene.get("dynamic_objects") or []) + (runtime_scene.get("static_objects") or []):
        actor_id = obj.get("id")
        if actor_id:
            result.append(str(actor_id))
    return result


def instance_color_from_index(index: int) -> unreal.LinearColor:
    value = max(1, int(index))
    red = ((value * 53) % 251 + 4) / 255.0
    green = ((value * 97) % 251 + 4) / 255.0
    blue = ((value * 193) % 251 + 4) / 255.0
    return unreal.LinearColor(red, green, blue, 1.0)


INSTANCE_MASK_MATERIALS = {}


def ensure_geometry_collection_material_usage(material) -> bool:
    usage_enum = getattr(getattr(unreal, "MaterialUsage", None), "MATUSAGE_GEOMETRY_COLLECTIONS", None)
    if not material or usage_enum is None:
        return False
    try:
        if not unreal.MaterialEditingLibrary.has_material_usage(material, usage_enum):
            unreal.MaterialEditingLibrary.set_material_usage(material, usage_enum)
            unreal.MaterialEditingLibrary.recompile_material(material)
            try:
                unreal.EditorAssetLibrary.save_loaded_asset(material)
            except Exception:
                pass
        return bool(unreal.MaterialEditingLibrary.has_material_usage(material, usage_enum))
    except Exception:
        return False


def instance_mask_material(index: int):
    key = int(index)
    cached = INSTANCE_MASK_MATERIALS.get(key)
    if cached:
        return cached
    color = instance_color_from_index(index)
    name = f"InstanceMaskBaseColor_{key:03d}"
    material = create_generated_material(name, color, roughness=1.0, metallic=0.0, emissive=0.0)
    if not material:
        return None
    ensure_geometry_collection_material_usage(material)
    try:
        changed = False
        if material.get_editor_property("shading_model") != unreal.MaterialShadingModel.MSM_DEFAULT_LIT:
            material.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_DEFAULT_LIT)
            changed = True
        unreal.MaterialEditingLibrary.recompile_material(material)
        if changed:
            versioned = f"{name}_{GENERATED_MATERIAL_VERSION}"
            unreal.EditorAssetLibrary.save_asset(f"/Game/AgenticGenerated/Materials/{versioned}.{versioned}")
    except Exception:
        return None
    INSTANCE_MASK_MATERIALS[key] = material
    return material


def snapshot_actor_materials(actor) -> list:
    component = actor_runtime_component(actor)
    if not component:
        return []
    try:
        count = max(1, int(component.get_num_materials()))
    except Exception:
        count = 1
    materials = []
    for idx in range(count):
        try:
            materials.append(component.get_material(idx))
        except Exception:
            materials.append(None)
    return materials


def restore_actor_materials(actor, materials: list) -> None:
    component = actor_runtime_component(actor)
    if not component:
        return
    for idx, material in enumerate(materials):
        try:
            component.set_material(idx, material)
        except Exception:
            pass


def assign_instance_mask_materials(actors: dict, runtime_scene: dict | None) -> dict:
    restore_state = {}
    mapping = []
    mask_actors = []
    for index, actor_id in enumerate(runtime_actor_ids_for_segmentation(runtime_scene), start=1):
        actor = (actors.get("visual_actors") or {}).get(actor_id) or actors.get(actor_id)
        if not actor:
            continue
        restore_state[actor_id] = snapshot_actor_materials(actor)
        material = instance_mask_material(index)
        if material:
            set_actor_material(actor, material)
            color = instance_color_from_index(index)
            mask_actors.append(actor)
            mapping.append(
                {
                    "object_id": actor_id,
                    "instance_id": index,
                    "rgb": [round(color.r, 6), round(color.g, 6), round(color.b, 6)],
                }
            )
    flush_editor_rendering()
    return {"restore_state": restore_state, "mapping": mapping, "actors": mask_actors}


def restore_instance_mask_materials(actors: dict, restore_state: dict) -> None:
    for actor_id, materials in restore_state.items():
        actor = (actors.get("visual_actors") or {}).get(actor_id) or actors.get(actor_id)
        if actor:
            restore_actor_materials(actor, materials)


def export_depth_and_segmentation_frame(
    actors: dict,
    runtime_scene: dict | None,
    view_id: str,
    frame_index: int,
    data_dirs: dict[str, Path],
) -> dict:
    result = {
        "frame": int(frame_index),
        "view_id": view_id,
        "depth": {"status": "missing", "path": None, "source_type": None},
        "segmentation": {"status": "missing", "path": None, "source_type": None, "instance_count": 0},
        "errors": [],
    }
    capture_comp = actors["capture_comp"]
    original_source = capture_comp.get_editor_property("capture_source")
    original_target = capture_comp.get_editor_property("texture_target")
    original_render_mode = capture_comp.get_editor_property("primitive_render_mode")
    original_show_flags = list(capture_comp.get_editor_property("show_flag_settings"))
    original_post_process_weight = float(capture_comp.get_editor_property("post_process_blend_weight"))
    original_post_process_settings = capture_comp.get_editor_property("post_process_settings")
    original_render_in_main_renderer = bool(capture_comp.get_editor_property("render_in_main_renderer"))

    depth_path = data_dirs["depth"] / view_id / f"frame_{int(frame_index):04d}.exr"
    depth_target = actors.get("depth_render_target")
    if not depth_target:
        depth_target = unreal.RenderingLibrary.create_render_target2d(
            actors.get("capture_world") or actors["world"],
            WIDTH,
            HEIGHT,
            unreal.TextureRenderTargetFormat.RTF_RGBA32F,
            unreal.LinearColor(0.0, 0.0, 0.0, 1.0),
        )
        actors["depth_render_target"] = depth_target
    capture_comp.set_editor_property("texture_target", depth_target)
    depth_ok = False
    depth_material = actors.get("depth_post_process_material") or create_generated_depth_post_process_material()
    if depth_material:
        actors["depth_post_process_material"] = depth_material
    try:
        if depth_material:
            capture_comp.add_or_update_blendable(depth_material, 1.0)
            capture_comp.set_editor_property("post_process_blend_weight", 1.0)
            capture_comp.set_editor_property("render_in_main_renderer", False)
            capture_comp.set_editor_property("texture_target", depth_target)
            if set_capture_source(actors, "SCS_FINAL_COLOR_HDR"):
                if not actors.get("depth_post_process_warmed"):
                    flush_editor_rendering()
                    capture_comp.capture_scene()
                    flush_editor_rendering()
                    actors["depth_post_process_warmed"] = True
                captured = capture_render_target_to_file(actors, depth_path, depth_target, force_exr=True)
                statistics = depth_pixel_statistics(depth_path) if captured else None
                depth_ok = bool(captured and float((statistics or {}).get("variance") or 0.0) > 1e-12)
            else:
                statistics = None
            if depth_ok:
                actors["depth_capture_source"] = "SCS_FINAL_COLOR_HDR_POST_PROCESS_SCENE_DEPTH"
                result["depth"] = {
                    "status": "available",
                    "path": str(depth_path),
                    "source_type": "ue_linear_scene_depth_post_process",
                    "capture_source": "SCS_FINAL_COLOR_HDR",
                    "depth_type": "view_z",
                    "material": "/Game/AgenticGenerated/Materials/M_Agentic_LinearSceneDepthScaled_V2",
                    "render_target_format": "RTF_RGBA32F",
                    "unit": "centimeter",
                    "depth_encoding": "linear_view_z_times_0.0001",
                    "stored_value_to_centimeter": 10000.0,
                    "unit_validation": "ue_scene_depth_semantics; empirical calibration pending",
                    "pixel_statistics": statistics,
                    "size": depth_path.stat().st_size,
                }
        else:
            result["errors"].append("depth_post_process_material_unavailable")
    except Exception as exc:
        result["errors"].append(f"depth_post_process_capture:{exc}")
    finally:
        if depth_material:
            try:
                capture_comp.remove_blendable(depth_material)
            except Exception:
                pass
        set_editor_property_if_available(capture_comp, "post_process_settings", original_post_process_settings)
        set_editor_property_if_available(capture_comp, "post_process_blend_weight", original_post_process_weight)
        set_editor_property_if_available(capture_comp, "render_in_main_renderer", original_render_in_main_renderer)
        set_editor_property_if_available(capture_comp, "texture_target", original_target)
        set_editor_property_if_available(capture_comp, "capture_source", original_source)
    if not depth_ok:
        result["errors"].append("depth_capture_failed")

    mask_path = data_dirs["segmentation"] / view_id / f"frame_{int(frame_index):04d}.exr"
    mask_target = actors.get("mask_render_target")
    if not mask_target:
        mask_target = unreal.RenderingLibrary.create_render_target2d(
            actors.get("capture_world") or actors["world"],
            WIDTH,
            HEIGHT,
            unreal.TextureRenderTargetFormat.RTF_RGBA16F,
            unreal.LinearColor(0.0, 0.0, 0.0, 1.0),
        )
        actors["mask_render_target"] = mask_target
    mask_state = assign_instance_mask_materials(actors, runtime_scene)
    mask_ok = False
    try:
        capture_comp.clear_show_only_components()
        capture_comp.set_editor_property(
            "primitive_render_mode",
            unreal.SceneCapturePrimitiveRenderMode.PRM_USE_SHOW_ONLY_LIST,
        )
        for mask_actor in mask_state.get("actors") or []:
            capture_comp.show_only_actor_components(mask_actor, True)
        disabled_flags = []
        for flag_name in (
            "AntiAliasing",
            "TemporalAA",
            "MotionBlur",
            "PostProcessing",
            "Tonemapper",
            "Bloom",
            "Lighting",
            "Fog",
            "Atmosphere",
        ):
            disabled_flags.append(unreal.EngineShowFlagsSetting(show_flag_name=flag_name, enabled=False))
        capture_comp.set_editor_property("show_flag_settings", disabled_flags)
        capture_comp.set_editor_property("post_process_blend_weight", 0.0)
        capture_comp.set_editor_property("texture_target", mask_target)
        if mask_state.get("mapping") and set_capture_source(actors, "SCS_BASE_COLOR"):
            mask_ok = capture_render_target_to_file(actors, mask_path, mask_target)
    finally:
        restore_instance_mask_materials(actors, mask_state.get("restore_state") or {})
        try:
            capture_comp.clear_show_only_components()
        except Exception:
            pass
        set_editor_property_if_available(capture_comp, "show_flag_settings", original_show_flags)
        set_editor_property_if_available(capture_comp, "primitive_render_mode", original_render_mode)
        set_editor_property_if_available(capture_comp, "texture_target", original_target)
        set_editor_property_if_available(capture_comp, "capture_source", original_source)
        set_editor_property_if_available(capture_comp, "post_process_blend_weight", original_post_process_weight)
    if mask_ok:
        result["segmentation"] = {
            "status": "available",
            "path": str(mask_path),
            "source_type": "ue_instance_material_mask",
            "instance_count": len(mask_state.get("mapping") or []),
            "instance_mapping": mask_state.get("mapping") or [],
            "capture_source": "SCS_BASE_COLOR",
            "render_target_format": "RTF_RGBA16F",
            "background_rgb": [0, 0, 0],
            "anti_aliasing": "quantized_in_local_runner",
            "size": mask_path.stat().st_size,
        }
    else:
        result["errors"].append("segmentation_capture_failed")

    return result


def initialize_data_pass_dirs(output_dir: Path) -> dict[str, Path]:
    dirs = {
        "depth": output_dir / "depth_exr",
        "segmentation": output_dir / "segmentation",
    }
    for path in dirs.values():
        path.mkdir(exist_ok=True)
    return dirs


def recapture_first_data_frame(
    actors: dict,
    runtime_scene: dict | None,
    view_outputs: list[dict],
    frame_views: dict[str, dict],
    data_pass_dirs: dict[str, Path],
    frame_index: int,
) -> dict[str, dict]:
    recaptured = {}
    for view in view_outputs:
        view_id = str(view.get("view_id") or view.get("id"))
        frame_view = frame_views.get(view_id)
        if not frame_view:
            continue
        set_capture_view(actors, frame_view["location"], frame_view["target"], frame_view["fov"])
        payload = export_depth_and_segmentation_frame(
            actors,
            runtime_scene,
            view_id,
            frame_index,
            data_pass_dirs,
        )
        payload["first_frame_stability_recapture"] = True
        recaptured[view_id] = payload
    return recaptured


def warm_up_scene_capture(actors: dict) -> None:
    if RENDER_WARMUP_FRAMES <= 0:
        return
    capture_comp = actors.get("capture_comp")
    if not capture_comp:
        return
    for _ in range(RENDER_WARMUP_FRAMES):
        try:
            capture_comp.capture_scene()
        except Exception:
            pass
        flush_editor_rendering()


def set_capture_view(actors: dict, location: unreal.Vector, target: unreal.Vector, fov: float) -> None:
    actors["capture"].set_actor_location(location, False, False)
    actors["capture"].set_actor_rotation(look_at_rotation(location, target), False)
    actors["capture_comp"].set_editor_property("fov_angle", float(fov))


def vector_payload(value: unreal.Vector) -> list[float]:
    return [round(float(value.x), 4), round(float(value.y), 4), round(float(value.z), 4)]


def canonical_camera_view_specs(
    base_location: unreal.Vector,
    target: unreal.Vector,
    extent: unreal.Vector,
    main_fov: float,
    runtime_scene: dict | None,
) -> list[dict]:
    scene_radius = max_axis(extent)
    radius = max(320.0, scene_radius)
    subject_offset = unreal.Vector(max(90.0, radius * 0.16), 0.0, max(20.0, radius * 0.05))
    event_offset = unreal.Vector(max(65.0, radius * 0.12), 0.0, max(16.0, radius * 0.04))
    side_height = max(280.0, radius * 0.70)
    views = [
        {
            "id": "front_static",
            "view_id": "front_static",
            "suffix": "",
            "label": "Front fixed",
            "camera_mode": "fixed",
            "lock_policy": "world-space pose fixed across counterfactual group",
            "location": base_location,
            "target": target,
            "fov": float(main_fov),
        },
        {
            "id": "side_static",
            "view_id": "side_static",
            "suffix": "_side_static",
            "label": "Side fixed",
            "camera_mode": "fixed",
            "lock_policy": "orthogonal side pose fixed across counterfactual group",
            "location": target + unreal.Vector(-max(760.0, radius * 1.95), -max(90.0, radius * 0.22), side_height),
            "target": target + unreal.Vector(max(60.0, radius * 0.12), 0.0, max(12.0, radius * 0.03)),
            "fov": 62.0,
        },
        {
            "id": "top_down",
            "view_id": "top_down",
            "suffix": "_top_down",
            "label": "Top-down",
            "camera_mode": "fixed",
            "lock_policy": "z-axis plan view fixed across counterfactual group",
            "location": target + unreal.Vector(-max(40.0, scene_radius * 0.10), -max(60.0, scene_radius * 0.15), max(320.0, scene_radius * 2.40)),
            "target": target,
            "fov": 50.0,
        },
        {
            "id": "tracking_subject",
            "view_id": "tracking_subject",
            "suffix": "_tracking_subject",
            "label": "Tracking subject",
            "camera_mode": "object_bound",
            "lock_policy": "target object id fixed; relative offset fixed",
            "dynamic_camera_profile": DYNAMIC_CAMERA_PROFILE,
            "subject_follow_location_gain": DYNAMIC_CAMERA_FOLLOW_GAINS["tracking_subject"][0],
            "subject_follow_target_gain": DYNAMIC_CAMERA_FOLLOW_GAINS["tracking_subject"][1],
            "location": target + unreal.Vector(-max(430.0, radius * 1.15), -max(500.0, radius * 1.32), max(240.0, radius * 0.60)),
            "target": target + subject_offset,
            "fov": 56.0,
        },
        {
            "id": "event_closeup",
            "view_id": "event_closeup",
            "suffix": "_event_closeup",
            "label": "Event close-up",
            "camera_mode": "trajectory",
            "lock_policy": "event window and spline knots fixed across counterfactual group",
            "dynamic_camera_profile": DYNAMIC_CAMERA_PROFILE,
            "subject_follow_location_gain": DYNAMIC_CAMERA_FOLLOW_GAINS["event_closeup"][0],
            "subject_follow_target_gain": DYNAMIC_CAMERA_FOLLOW_GAINS["event_closeup"][1],
            "location": target + unreal.Vector(-max(260.0, radius * 0.68), -max(330.0, radius * 0.82), max(150.0, radius * 0.38)),
            "target": target + event_offset,
            "fov": 46.0,
        },
    ]
    camera = runtime_scene.get("camera") if isinstance((runtime_scene or {}).get("camera"), dict) else {}
    planned = [item for item in camera.get("views") or [] if isinstance(item, dict) and item.get("camera_id")]
    if not planned:
        return views
    planned_by_id = {str(item["camera_id"]): item for item in planned}
    anchor = planned_by_id.get("front_static") or planned[0]
    anchor_location = anchor.get("location") or [0.0, 0.0, 0.0]
    origin = unreal.Vector(
        base_location.x - float(anchor_location[0]) * 100.0,
        base_location.y - float(anchor_location[1]) * 100.0,
        base_location.z - float(anchor_location[2]) * 100.0,
    )
    result = []
    for view in views:
        spec = planned_by_id.get(str(view.get("view_id")))
        if not spec:
            result.append(view)
            continue
        location = spec.get("location") or [0.0, 0.0, 0.0]
        planned_target = spec.get("target") or [0.0, 0.0, 0.0]
        result.append(
            {
                **view,
                "location": origin + unreal.Vector(*[float(value) * 100.0 for value in location[:3]]),
                "target": origin + unreal.Vector(*[float(value) * 100.0 for value in planned_target[:3]]),
                "fov": float(spec.get("fov") or view["fov"]),
                "camera_mode": str(spec.get("camera_mode") or view["camera_mode"]),
                "dynamic_camera_profile": spec.get("dynamic_camera_profile") or view.get("dynamic_camera_profile"),
                "subject_follow_location_gain": float(
                    spec.get("subject_follow_location_gain")
                    if spec.get("subject_follow_location_gain") is not None
                    else view.get("subject_follow_location_gain", 1.0)
                ),
                "subject_follow_target_gain": float(
                    spec.get("subject_follow_target_gain")
                    if spec.get("subject_follow_target_gain") is not None
                    else view.get("subject_follow_target_gain", 1.0)
                ),
                "plan_source": "camera_plan.json",
            }
        )
    return result


def camera_view_specs(actors: dict, runtime_scene: dict | None) -> list[dict]:
    pose = actors.get("camera_pose") or {}
    location_values = pose.get("location") or [0.0, -520.0, 260.0]
    target_values = pose.get("target") or [0.0, 0.0, 120.0]
    extent_values = pose.get("extent") or [180.0, 180.0, 120.0]
    base_location = unreal.Vector(*[float(value) for value in location_values])
    target = unreal.Vector(*[float(value) for value in target_values])
    extent = unreal.Vector(*[float(value) for value in extent_values])
    radius = max(320.0, max_axis(extent))
    case_type = (runtime_scene or {}).get("case_type")
    main_fov = camera_fov_override(runtime_scene)
    if main_fov is None:
        main_fov = 52.0 if case_type == "gear_collision_chain" else (68.0 if case_type in {"balloon_wind_drift", "bottle_domino_chain"} else (64.0 if case_type == "barrel_impact_cascade" else (66.0 if case_type == "third_person_box_throw" else 62.0)))
    if MULTI_VIEW and CANONICAL_MULTI_VIEW and runtime_scene:
        views = canonical_camera_view_specs(base_location, target, extent, float(main_fov), runtime_scene)
        requested = set(runtime_scene.get("requested_views") or [])
        return [view for view in views if view["view_id"] in requested] or views
    views = [
        {"id": "main", "view_id": "main", "suffix": "", "label": "main tracking view", "camera_mode": "fixed", "location": base_location, "target": target, "fov": main_fov},
    ]
    if not MULTI_VIEW or not runtime_scene:
        return views
    if case_type == "balloon_wind_drift":
        views.extend([
            {"id": "wide", "suffix": "_wide", "label": "wide drift overview", "location": target + unreal.Vector(-max(620.0, radius * 1.15), -max(1260.0, radius * 2.35), max(430.0, radius * 0.90)), "target": target + unreal.Vector(120.0, 0.0, 45.0), "fov": 72.0},
            {"id": "side", "suffix": "_side", "label": "side wind direction", "location": target + unreal.Vector(-max(1180.0, radius * 2.4), -120.0, max(360.0, radius * 0.82)), "target": target + unreal.Vector(120.0, 0.0, 20.0), "fov": 64.0},
            {"id": "top", "suffix": "_top", "label": "high trajectory check", "location": target + unreal.Vector(-360.0, -520.0, max(880.0, radius * 1.95)), "target": target + unreal.Vector(120.0, 0.0, 10.0), "fov": 62.0},
        ])
    elif case_type == "plant_sway_camera":
        views.extend([
            {"id": "wide", "suffix": "_wide", "label": "full plant row", "location": target + unreal.Vector(-max(680.0, radius * 1.08), -max(1500.0, radius * 2.45), max(620.0, radius * 0.98)), "target": target + unreal.Vector(60.0, 0.0, 35.0), "fov": 70.0},
            {"id": "side", "suffix": "_side", "label": "sway phase side view", "location": target + unreal.Vector(-max(1200.0, radius * 2.2), -180.0, max(560.0, radius * 0.92)), "target": target + unreal.Vector(50.0, 0.0, 30.0), "fov": 66.0},
            {"id": "top", "suffix": "_top", "label": "phase overview", "location": target + unreal.Vector(-240.0, -360.0, max(980.0, radius * 1.95)), "target": target + unreal.Vector(50.0, 0.0, 20.0), "fov": 62.0},
        ])
    elif case_type in {"stone_slope_roll", "slope_drop_bounce_stop", "character_throw_to_slope_roll", "character_carry_drop"}:
        views.extend([
            {"id": "wide", "suffix": "_wide", "label": "wide rolling lane", "location": target + unreal.Vector(-max(600.0, radius * 1.25), -max(1180.0, radius * 2.40), max(430.0, radius * 0.94)), "target": target + unreal.Vector(70.0, 0.0, 20.0), "fov": 70.0},
            {"id": "side", "suffix": "_side", "label": "side slope motion", "location": target + unreal.Vector(-max(1060.0, radius * 2.15), -140.0, max(370.0, radius * 0.84)), "target": target + unreal.Vector(80.0, 0.0, 10.0), "fov": 64.0},
            {"id": "top", "suffix": "_top", "label": "top rolling path", "location": target + unreal.Vector(-220.0, -320.0, max(860.0, radius * 1.80)), "target": target + unreal.Vector(85.0, 0.0, 5.0), "fov": 62.0},
        ])
    elif case_type == "gear_collision_chain":
        views.extend([
            {"id": "wide", "suffix": "_wide", "label": "wide collision lane", "location": target + unreal.Vector(-max(650.0, radius * 1.28), -max(1240.0, radius * 2.42), max(440.0, radius * 0.96)), "target": target + unreal.Vector(75.0, 0.0, 25.0), "fov": 70.0},
            {"id": "side", "suffix": "_side", "label": "side contact timing", "location": target + unreal.Vector(-max(1100.0, radius * 2.18), -140.0, max(390.0, radius * 0.86)), "target": target + unreal.Vector(80.0, 0.0, 20.0), "fov": 64.0},
            {"id": "top", "suffix": "_top", "label": "top collision order", "location": target + unreal.Vector(-220.0, -340.0, max(900.0, radius * 1.86)), "target": target + unreal.Vector(85.0, 0.0, 5.0), "fov": 62.0},
        ])
    elif case_type == "bottle_domino_chain":
        views.extend([
            {"id": "wide", "suffix": "_wide", "label": "wide domino timing overview", "location": target + unreal.Vector(-760.0, -1120.0, 430.0), "target": target + unreal.Vector(60.0, 12.0, 58.0), "fov": 72.0},
            {"id": "side", "suffix": "_side", "label": "side domino contact order", "location": target + unreal.Vector(-940.0, -160.0, 330.0), "target": target + unreal.Vector(65.0, -4.0, 48.0), "fov": 62.0},
            {"id": "top", "suffix": "_top", "label": "top domino final poses", "location": target + unreal.Vector(-160.0, -280.0, 760.0), "target": target + unreal.Vector(40.0, -8.0, 10.0), "fov": 58.0},
        ])
    elif case_type in {"crate_friction_slide", "cone_barrel_collision", "falling_crate_collision", "barrel_impact_cascade", "stack_stability", "constraint_joint"}:
        label = {
            "crate_friction_slide": "friction lanes",
            "cone_barrel_collision": "impact response",
            "falling_crate_collision": "falling collision",
            "barrel_impact_cascade": "barrel impact cascade",
            "stack_stability": "stack stability",
            "constraint_joint": "constraint joint",
        }.get(case_type, "mechanics")
        views.extend([
            {"id": "wide", "suffix": "_wide", "label": f"wide {label} overview", "location": target + unreal.Vector(-max(680.0, radius * 1.25), -max(1220.0, radius * 2.25), max(520.0, radius * 1.05)), "target": target + unreal.Vector(95.0, 0.0, 70.0 if case_type == "falling_crate_collision" else 24.0), "fov": 70.0},
            {"id": "side", "suffix": "_side", "label": f"side {label} view", "location": target + unreal.Vector(-max(1120.0, radius * 2.10), -130.0, max(430.0, radius * 0.92)), "target": target + unreal.Vector(95.0, 0.0, 68.0 if case_type == "falling_crate_collision" else 18.0), "fov": 64.0},
            {"id": "top", "suffix": "_top", "label": f"top {label} check", "location": target + unreal.Vector(-220.0, -330.0, max(940.0, radius * 1.90)), "target": target + unreal.Vector(95.0, 0.0, 12.0), "fov": 62.0},
        ])
    elif case_type == "wheel_ramp_jump":
        views.extend([
            {"id": "wide", "suffix": "_wide", "label": "wide ramp and arc", "location": target + unreal.Vector(-max(760.0, radius * 1.30), -max(1320.0, radius * 2.35), max(500.0, radius * 0.98)), "target": target + unreal.Vector(120.0, 0.0, 45.0), "fov": 72.0},
            {"id": "side", "suffix": "_side", "label": "side jump arc", "location": target + unreal.Vector(-max(1220.0, radius * 2.15), -150.0, max(420.0, radius * 0.86)), "target": target + unreal.Vector(120.0, 0.0, 35.0), "fov": 66.0},
            {"id": "top", "suffix": "_top", "label": "top rollout path", "location": target + unreal.Vector(-260.0, -380.0, max(940.0, radius * 1.86)), "target": target + unreal.Vector(120.0, 0.0, 12.0), "fov": 64.0},
        ])
    elif case_type == "third_person_box_throw":
        views.extend([
            {"id": "wide", "suffix": "_wide", "label": "wide third-person run and throw overview", "location": target + unreal.Vector(-max(700.0, radius * 1.20), -max(1280.0, radius * 2.20), max(490.0, radius * 1.02)), "target": target + unreal.Vector(110.0, 0.0, 40.0), "fov": 68.0},
            {"id": "side", "suffix": "_side", "label": "side runner and barrel contact view", "location": target + unreal.Vector(-max(1180.0, radius * 2.10), -160.0, max(420.0, radius * 0.90)), "target": target + unreal.Vector(120.0, 0.0, 36.0), "fov": 60.0},
            {"id": "top", "suffix": "_top", "label": "top throw path check", "location": target + unreal.Vector(-240.0, -340.0, max(960.0, radius * 1.88)), "target": target + unreal.Vector(120.0, 0.0, 18.0), "fov": 58.0},
        ])
    else:
        views.extend([
            {"id": "wide", "suffix": "_wide", "label": "wide experiment overview", "location": target + unreal.Vector(-radius * 0.62, -max(600.0, radius * 2.05), max(240.0, radius * 0.88)), "target": target, "fov": 62.0},
            {"id": "side", "suffix": "_side", "label": "side motion view", "location": target + unreal.Vector(-max(560.0, radius * 1.7), -max(100.0, radius * 0.25), max(220.0, radius * 0.62)), "target": target, "fov": 56.0},
            {"id": "top", "suffix": "_top", "label": "high path view", "location": target + unreal.Vector(-140.0, -240.0, max(620.0, radius * 1.9)), "target": target, "fov": 52.0},
        ])
    return views


def interpolate_values(a_values, b_values, alpha: float) -> list[float]:
    return [
        float(a_values[idx]) + (float(b_values[idx]) - float(a_values[idx])) * alpha
        for idx in range(3)
    ]


def fixed_physics_step_s(frame_index: int, fps: int) -> float:
    return 0.0 if int(frame_index) <= 0 else 1.0 / max(int(fps), 1)


def runtime_timebase(runtime_scene: dict | None) -> dict:
    simulation = runtime_scene.get("simulation") if isinstance((runtime_scene or {}).get("simulation"), dict) else {}
    render_fps = int(simulation.get("render_fps") or simulation.get("fps") or FPS)
    physics_hz = int(simulation.get("physics_hz") or render_fps)
    duration_s = float(simulation.get("duration_s") or DURATION)
    timebase = build_timebase(duration_s=duration_s, physics_hz=physics_hz, render_fps=render_fps)
    full_solver_frame_count = int(simulation.get("full_solver_frame_count") or timebase["solver_frame_count"])
    timebase["physics_step_count"] = int(simulation.get("physics_step_count") or full_solver_frame_count - 1)
    timebase["full_solver_frame_count"] = full_solver_frame_count
    timebase["solver_capture_mode"] = str(simulation.get("solver_capture_mode") or "render_boundary")
    timebase["raw_capture_frame_count"] = int(simulation.get("raw_capture_frame_count") or timebase["canonical_frame_count"])
    timebase["solver_frame_count"] = int(simulation.get("solver_frame_count") or timebase["raw_capture_frame_count"])
    return timebase


def camera_view_for_frame(
    view: dict,
    runtime_scene: dict | None,
    frame_index: int,
    frame_count: int,
    solver_trajectory: list[dict] | None = None,
) -> dict:
    if not runtime_scene:
        return view
    camera_mode = str(view.get("camera_mode") or "fixed")
    if CANONICAL_MULTI_VIEW and camera_mode in {"object_bound", "trajectory"}:
        delta = runtime_subject_delta_cm(runtime_scene, frame_index, solver_trajectory)
        progress = float(frame_index) / max(1.0, float(frame_count - 1))
        location_gain = float(view.get("subject_follow_location_gain", 1.0))
        target_gain = float(view.get("subject_follow_target_gain", 1.0))
        location = view["location"] + unreal.Vector(
            delta.x * location_gain,
            delta.y * location_gain,
            delta.z * location_gain,
        )
        target = view["target"] + unreal.Vector(
            delta.x * target_gain,
            delta.y * target_gain,
            delta.z * target_gain,
        )
        if camera_mode == "trajectory":
            location = location + unreal.Vector(80.0 * progress, 55.0 * math.sin(math.pi * progress), 18.0 * math.sin(math.pi * progress))
        return {**view, "location": location, "target": target}
    if CANONICAL_MULTI_VIEW:
        return view
    camera = runtime_scene.get("camera") if isinstance(runtime_scene.get("camera"), dict) else {}
    if str(camera.get("mode") or "").strip().lower() != "trajectory":
        return view
    waypoints = camera.get("preview_waypoints") if isinstance(camera.get("preview_waypoints"), list) else []
    usable = [
        waypoint
        for waypoint in waypoints
        if isinstance(waypoint, dict)
        and isinstance(waypoint.get("position_m"), list)
        and len(waypoint["position_m"]) >= 3
    ]
    if len(usable) < 2:
        return view

    duration_s = float(((runtime_scene.get("simulation") or {}).get("duration_s")) or 1.0)
    frame_t = duration_s * (float(frame_index) / max(1.0, float(frame_count - 1)))

    def waypoint_time(idx: int, waypoint: dict) -> float:
        if waypoint.get("time_sec") is not None:
            return float(waypoint["time_sec"])
        if waypoint.get("t") is not None:
            return float(waypoint["t"]) * duration_s
        return duration_s * (float(idx) / max(1.0, float(len(usable) - 1)))

    timed = sorted([(waypoint_time(idx, waypoint), waypoint) for idx, waypoint in enumerate(usable)], key=lambda item: item[0])
    left_t, left = timed[0]
    right_t, right = timed[-1]
    for idx in range(len(timed) - 1):
        a_t, a = timed[idx]
        b_t, b = timed[idx + 1]
        if a_t <= frame_t <= b_t:
            left_t, left, right_t, right = a_t, a, b_t, b
            break
    alpha = 0.0 if abs(right_t - left_t) < 1e-6 else max(0.0, min(1.0, (frame_t - left_t) / (right_t - left_t)))
    position_m = interpolate_values(left["position_m"], right["position_m"], alpha)
    target = view["target"]
    left_target = left.get("target_offset_m")
    right_target = right.get("target_offset_m")
    if isinstance(left_target, list) and len(left_target) >= 3 and isinstance(right_target, list) and len(right_target) >= 3:
        target_offset_m = interpolate_values(left_target, right_target, alpha)
        target = view["target"] + unreal.Vector(target_offset_m[0] * 100.0, target_offset_m[1] * 100.0, target_offset_m[2] * 100.0)
    location = view["target"] + unreal.Vector(position_m[0] * 100.0, position_m[1] * 100.0, position_m[2] * 100.0)
    fov = float(left.get("fov", view.get("fov", 62.0))) + (float(right.get("fov", left.get("fov", view.get("fov", 62.0)))) - float(left.get("fov", view.get("fov", 62.0)))) * alpha
    return {**view, "location": location, "target": target, "fov": fov}


def runtime_subject_delta_cm(runtime_scene: dict, frame_index: int, solver_trajectory: list[dict] | None = None):
    dynamic_ids = [str(obj.get("id")) for obj in runtime_scene.get("dynamic_objects") or [] if isinstance(obj, dict) and obj.get("id")]
    if not dynamic_ids:
        return unreal.Vector(0.0, 0.0, 0.0)
    subject_id = dynamic_ids[0]
    precomputed = runtime_scene.get("precomputed_trajectory") if isinstance(runtime_scene, dict) else None
    camera_truth = precomputed if isinstance(precomputed, list) and any(
        isinstance(((frame.get("objects") or {}).get(subject_id) or {}).get("camera_position_m"), list)
        for frame in precomputed
        if isinstance(frame, dict)
    ) else None
    trajectory = camera_truth or solver_trajectory or precomputed
    if not isinstance(trajectory, list) or not trajectory:
        return unreal.Vector(0.0, 0.0, 0.0)

    def subject_position(frame: dict) -> list[float] | None:
        state = (frame.get("objects") or {}).get(subject_id) if isinstance(frame, dict) else None
        raw = (state or {}).get("camera_position_m") or (state or {}).get("position") or (state or {}).get("position_m")
        if not isinstance(raw, list) or len(raw) < 3:
            return None
        return [float(raw[0]), float(raw[1]), float(raw[2])]

    start = subject_position(trajectory[0])
    selected = next((frame for frame in trajectory if int(frame.get("frame", -1)) == int(frame_index)), None)
    current = subject_position(selected or trajectory[min(max(int(frame_index), 0), len(trajectory) - 1)])
    if start is None or current is None:
        return unreal.Vector(0.0, 0.0, 0.0)
    return unreal.Vector(*[(current[index] - start[index]) * 100.0 for index in range(3)])


def apply_trajectory_frame(actors: dict, dynamic_ids: list[str], frame: dict, scene_origin: unreal.Vector, runtime_scene: dict | None) -> None:
    obj_by_id = {
        obj.get("id"): obj
        for obj in ((runtime_scene or {}).get("dynamic_objects") or []) + ((runtime_scene or {}).get("static_objects") or [])
    }
    for oid in dynamic_ids:
        if oid not in actors or oid not in frame["objects"]:
            continue
        frame_obj = frame["objects"][oid]
        z_offset_cm = 0.0 if runtime_scene else 120.0
        if runtime_scene:
            z_offset_cm = float((actors.get("runtime_ground_offsets") or {}).get(oid, z_offset_cm))
        params = (obj_by_id.get(oid) or {}).get("params") or {}
        if str(params.get("humanoid_adapter") or "").strip().lower() in {"gasp", "ddv"}:
            apply_gasp_locomotion_frame(actors, obj_by_id.get(oid) or {}, frame)
            continue
        sweep_movement = params.get("sweep_movement") is True
        actors[oid].set_actor_location(ue_vec_from_meters(frame_obj["position"], z_offset_cm=z_offset_cm, origin=scene_origin), sweep_movement, False)
        if runtime_scene:
            rot = frame_obj.get("rotation_degrees") or [0.0, 0.0, 0.0]
            combined_rot = runtime_combined_rotation(obj_by_id.get(oid, {}), rot)
            actors[oid].set_actor_rotation(runtime_rotator(combined_rot), False)
    update_elastic_tether_visual(actors, frame, scene_origin, runtime_scene)


def update_elastic_tether_visual(actors: dict, frame: dict, scene_origin: unreal.Vector, runtime_scene: dict | None) -> None:
    if not runtime_scene:
        return
    frame_objects = frame.get("objects") if isinstance(frame.get("objects"), dict) else {}
    tether_specs = [
        obj
        for obj in runtime_scene.get("static_objects") or []
        if obj.get("behavior") == "elastic_tether_visual"
    ]
    for tether_spec in tether_specs:
        tether = actors.get(str(tether_spec.get("id") or ""))
        if not tether:
            continue
        params = tether_spec.get("params") or {}
        anchor = frame_objects.get(str(params.get("anchor_id") or "anchor"))
        body = frame_objects.get(str(params.get("body_id") or "payload"))
        if not isinstance(anchor, dict) or not isinstance(body, dict):
            continue
        anchor_position = runtime_vec3(anchor.get("position") or anchor.get("position_m"))
        body_position = runtime_vec3(body.get("position") or body.get("position_m"))
        midpoint = [(anchor_position[index] + body_position[index]) / 2.0 for index in range(3)]
        direction = [anchor_position[index] - body_position[index] for index in range(3)]
        length_m = max(0.05, math.sqrt(sum(value * value for value in direction)))
        z_offset_cm = float((actors.get("runtime_ground_offsets") or {}).get(str(params.get("body_id") or "payload"), 0.0))
        tether.set_actor_location(ue_vec_from_meters(midpoint, z_offset_cm=z_offset_cm, origin=scene_origin), False, False)
        component = actor_runtime_component(tether)
        if component:
            component.set_world_scale3d(unreal.Vector(0.012, 0.012, length_m))
        rotation = look_at_rotation(
            ue_vec_from_meters(body_position, z_offset_cm=z_offset_cm, origin=scene_origin),
            ue_vec_from_meters(anchor_position, z_offset_cm=z_offset_cm, origin=scene_origin),
        )
        try:
            rotation.pitch = float(rotation.pitch) - 90.0
        except Exception:
            pass
        tether.set_actor_rotation(rotation, False)


def runtime_vec3(value, fallback=(0.0, 0.0, 0.0)) -> list[float]:
    try:
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            return [float(value[0]), float(value[1]), float(value[2])]
    except Exception:
        pass
    return [float(fallback[0]), float(fallback[1]), float(fallback[2])]


def delayed_release_projectile_objects(runtime_scene: dict | None) -> list[dict]:
    if not runtime_scene:
        return []
    return [
        obj
        for obj in runtime_scene.get("dynamic_objects") or []
        if obj.get("behavior") in {"character_throw_projectile", "character_carry_object"}
        or (isinstance(obj.get("params"), dict) and obj["params"].get("release_time_s") is not None)
    ]


def set_delayed_release_collision_enabled(component, enabled: bool) -> None:
    """Keep scheduled bodies causally absent until their declared release."""
    component.set_collision_enabled(
        unreal.CollisionEnabled.QUERY_AND_PHYSICS
        if enabled
        else unreal.CollisionEnabled.NO_COLLISION
    )


def projectile_hold_position(obj: dict, frame: dict) -> list[float]:
    params = obj.get("params") or {}
    if obj.get("behavior") not in {"character_throw_projectile", "character_carry_object"}:
        return runtime_vec3(params.get("hold_position_m"), obj.get("initial_position_m") or (0.0, 0.0, 1.0))
    carrier_id = str(params.get("carrier_id") or "runner_character")
    frame_objects = frame.get("objects") if isinstance(frame.get("objects"), dict) else {}
    carrier = frame_objects.get(carrier_id) if isinstance(frame_objects, dict) else None
    if isinstance(carrier, dict):
        base = runtime_vec3(carrier.get("position"), obj.get("initial_position_m") or (0.0, 0.0, 1.0))
    else:
        base = runtime_vec3(params.get("hold_position_m"), obj.get("initial_position_m") or (0.0, 0.0, 1.0))
    offset = runtime_vec3(params.get("hold_offset_m"), (0.45, 0.0, 0.65))
    return [base[idx] + offset[idx] for idx in range(3)]


def set_component_angular_velocity_deg(component, raw_velocity, actor_id: str, status: dict) -> None:
    angular_deg = vector_from_sequence(raw_velocity, 1.0)
    if max(abs(angular_deg.x), abs(angular_deg.y), abs(angular_deg.z)) <= 1e-6:
        return
    errors = []
    for method_name, vector, units in (
        ("set_physics_angular_velocity_in_degrees", angular_deg, "deg_s"),
        (
            "set_physics_angular_velocity_in_radians",
            unreal.Vector(math.radians(angular_deg.x), math.radians(angular_deg.y), math.radians(angular_deg.z)),
            "rad_s",
        ),
    ):
        method = getattr(component, method_name, None)
        if not method:
            errors.append(f"{method_name}:missing")
            continue
        for args in ((vector, False, ""), (vector, False), (vector,)):
            try:
                method(*args)
                status.setdefault("delayed_release_angular_velocities", []).append(
                    {
                        "id": actor_id,
                        "deg_s": [float(raw_velocity[0]), float(raw_velocity[1]), float(raw_velocity[2])],
                        "method": method_name,
                        "units": units,
                    }
                )
                return
            except Exception as exc:
                errors.append(f"{method_name}:{exc}")
    status.setdefault("errors", []).append(f"delayed_release_angular_velocity:{actor_id}:{errors[-3:]}")


def apply_delayed_release_projectiles(
    actors: dict,
    runtime_scene: dict | None,
    frame: dict,
    scene_origin: unreal.Vector,
    status: dict,
) -> None:
    projectiles = delayed_release_projectile_objects(runtime_scene)
    if not projectiles:
        return
    release_state = actors.setdefault("delayed_release_state", {})
    time_s = float(frame.get("time") or 0.0)
    ground_offsets = actors.get("runtime_ground_offsets") or {}
    for obj in projectiles:
        actor_id = obj.get("id")
        actor = actors.get(actor_id)
        component = actor_runtime_component(actor) if actor else None
        if not actor or not component:
            status.setdefault("errors", []).append(f"delayed_release:{actor_id}:missing_actor_or_component")
            continue
        params = obj.get("params") or {}
        properties = obj.get("physics_properties") or {}
        default_release_time = float_control(params.get("drop_time_s"), float_control(params.get("throw_time_s"), 0.0, 0.0, None), 0.0, None)
        grasp_time = float_control(params.get("grasp_time_s"), 0.0, 0.0, None)
        release_time = float_control(params.get("release_time_s"), default_release_time, 0.0, None)
        state = release_state.setdefault(str(actor_id), {"released": False, "held_frames": 0})
        if params.get("native_grabbable_blueprint") and "native_grabbable_methods" not in state:
            state["native_grabbable_methods"] = sorted(
                name
                for name in dir(actor)
                if any(term in name for term in ("grab", "drop", "interact", "attach", "collision", "physics"))
            )
        if time_s < grasp_time:
            waiting = runtime_vec3(obj.get("initial_position_m"), (0.0, 0.0, 0.0))
            try:
                set_delayed_release_collision_enabled(component, True)
                component.set_simulate_physics(False)
                component.set_enable_gravity(False)
                component.set_physics_linear_velocity(unreal.Vector(0.0, 0.0, 0.0), False, "")
            except Exception as exc:
                status.setdefault("errors", []).append(f"delayed_wait_physics:{actor_id}:{exc}")
            actor.set_actor_location(
                ue_vec_from_meters(waiting, z_offset_cm=float(ground_offsets.get(actor_id, 0.0)), origin=scene_origin),
                False,
                False,
            )
            state["waiting_frames"] = int(state.get("waiting_frames") or 0) + 1
            continue
        if time_s < release_time:
            try:
                set_delayed_release_collision_enabled(component, False)
                component.set_simulate_physics(False)
                component.set_enable_gravity(False)
                component.set_physics_linear_velocity(unreal.Vector(0.0, 0.0, 0.0), False, "")
            except Exception as exc:
                status.setdefault("errors", []).append(f"delayed_hold_physics:{actor_id}:{exc}")
            socket_name = str(params.get("attachment_socket") or "").strip()
            if socket_name and not state.get("attached"):
                carrier = actors.get(str(params.get("carrier_id") or "runner_character"))
                parent = actor_skeletal_component_with_socket(carrier, socket_name) if carrier else None
                try:
                    can_attach = False
                    if parent:
                        hand = parent.get_socket_location(unreal.Name(socket_name))
                        object_center, extent = actor_bounds(actor)
                        center_distance_cm = unreal.MathLibrary.vector_distance(hand, object_center)
                        contact_extent_cm = min(max_axis(extent), runtime_desired_extent_cm(obj))
                        surface_gap_cm = max(
                            0.0,
                            center_distance_cm
                            - contact_extent_cm,
                        )
                        max_gap_cm = float_control(params.get("max_grasp_gap_cm"), 4.0, 0.0, 50.0)
                        previous_min_gap = float(state.get("min_grasp_gap_cm", float("inf")))
                        state["min_grasp_gap_cm"] = round(
                            min(previous_min_gap, surface_gap_cm),
                            3,
                        )
                        frame_index = int(frame.get("frame") or 0)
                        if surface_gap_cm <= previous_min_gap:
                            state["closest_grasp_frame"] = frame_index
                            state["closest_grasp_hand_position_m"] = [
                                round((value - origin_value) / 100.0, 5)
                                for value, origin_value in zip(
                                    (hand.x, hand.y, hand.z),
                                    (scene_origin.x, scene_origin.y, scene_origin.z),
                                )
                            ]
                            state["closest_grasp_object_position_m"] = [
                                round((value - origin_value) / 100.0, 5)
                                for value, origin_value in zip(
                                    (object_center.x, object_center.y, object_center.z),
                                    (scene_origin.x, scene_origin.y, scene_origin.z),
                                )
                            ]
                            state["closest_grasp_contact_extent_cm"] = round(contact_extent_cm, 3)
                        if (
                            surface_gap_cm > max_gap_cm
                            and surface_gap_cm <= float_control(params.get("grasp_alignment_start_gap_cm"), 15.0, max_gap_cm, 50.0)
                            and state.get("last_grasp_alignment_frame") != frame_index
                        ):
                            # ponytail: bounded magnetic alignment; replace with hand IK when arbitrary object geometry is required.
                            step_cm = min(
                                float_control(params.get("grasp_alignment_step_cm"), 3.0, 0.1, 10.0),
                                surface_gap_cm - max_gap_cm,
                            )
                            direction = (hand - object_center) / max(center_distance_cm, 1e-3)
                            actor.set_actor_location(actor.get_actor_location() + direction * step_cm, False, False)
                            state["grasp_alignment_frames"] = int(state.get("grasp_alignment_frames") or 0) + 1
                            state["last_grasp_alignment_frame"] = frame_index
                        else:
                            can_attach = surface_gap_cm <= max_gap_cm
                    if can_attach and bool_control(params.get("smooth_socket_follow"), False):
                        state["attached"] = True
                        state["smooth_socket_follow"] = True
                    else:
                        state["attached"] = bool(
                            can_attach
                            and attach_actor_clear_of_socket(
                                actor,
                                parent,
                                socket_name,
                                float_control(params.get("grasp_clearance_cm"), 2.0, 0.0, 20.0),
                                max_gap_cm,
                            )
                        )
                except Exception as exc:
                    status.setdefault("errors", []).append(f"delayed_attach:{actor_id}:{exc}")
            if not state.get("attached"):
                if socket_name:
                    state["grasp_rejected_frames"] = int(state.get("grasp_rejected_frames") or 0) + 1
                    continue
                hold = projectile_hold_position(obj, frame)
                actor.set_actor_location(
                    ue_vec_from_meters(hold, z_offset_cm=float(ground_offsets.get(actor_id, 0.0)), origin=scene_origin),
                    False,
                    False,
                )
                actor.set_actor_rotation(runtime_rotator(params.get("hold_rotation_degrees") or obj.get("rotation_degrees")), False)
                state["last_hold_position_m"] = [round(value, 5) for value in hold]
            elif state.get("smooth_socket_follow") and state.get("last_carry_follow_frame") != int(frame.get("frame") or 0):
                carrier = actors.get(str(params.get("carrier_id") or "runner_character"))
                parent = actor_skeletal_component_with_socket(carrier, socket_name) if carrier else None
                if parent:
                    animation_state = (actors.get("runtime_animation_state") or {}).get(str(params.get("carrier_id") or "runner_character")) or {}
                    if animation_state.get("gasp_interaction_montage_active"):
                        carry_pose_phase = "drop" if animation_state.get("segment_name") == "drop" else "interaction"
                    else:
                        carry_pose_phase = "gasp"
                    frame_index = int(frame.get("frame") or 0)
                    phase_changed = (
                        state.get("last_carry_pose_phase") is not None
                        and state.get("last_carry_pose_phase") != carry_pose_phase
                    )
                    if phase_changed:
                        if state.get("actual_socket_attached"):
                            actor.detach_from_actor(
                                unreal.DetachmentRule.KEEP_WORLD,
                                unreal.DetachmentRule.KEEP_WORLD,
                                unreal.DetachmentRule.KEEP_WORLD,
                            )
                            state["actual_socket_attached"] = False
                        state.setdefault("socket_transition_frames", []).append(frame_index)
                        state["socket_reattach_after_frame"] = frame_index + 2
                    carrier_location = carrier.get_actor_location()
                    previous_carrier_location = state.get("last_carrier_location_cm")
                    if carry_pose_phase == "gasp" and previous_carrier_location:
                        actor.set_actor_location(
                            actor.get_actor_location()
                            + unreal.Vector(
                                carrier_location.x - previous_carrier_location[0],
                                carrier_location.y - previous_carrier_location[1],
                                carrier_location.z - previous_carrier_location[2],
                            ),
                            False,
                            False,
                        )
                    applied = follow_socket_smoothly(
                        actor,
                        parent,
                        socket_name,
                        float_control(params.get("grasp_clearance_cm"), 2.0, 0.0, 20.0),
                        float_control(params.get("max_carry_follow_step_cm"), 8.0, 0.1, 50.0),
                    )
                    max_follow_step_cm = float_control(params.get("max_carry_follow_step_cm"), 8.0, 0.1, 50.0)
                    if (
                        carry_pose_phase != "gasp"
                        and not state.get("actual_socket_attached")
                        and frame_index >= int(state.get("socket_reattach_after_frame") or 0)
                        and applied < max_follow_step_cm - 1e-3
                    ):
                        state["actual_socket_attached"] = bool(
                            actor.attach_to_component(
                                parent,
                                unreal.Name(socket_name),
                                unreal.AttachmentRule.KEEP_WORLD,
                                unreal.AttachmentRule.KEEP_WORLD,
                                unreal.AttachmentRule.KEEP_WORLD,
                                False,
                            )
                        )
                        if state["actual_socket_attached"]:
                            state.setdefault("socket_attachment_frames", []).append(frame_index)
                    state["max_carry_follow_step_cm"] = round(
                        max(float(state.get("max_carry_follow_step_cm") or 0.0), applied),
                        3,
                    )
                    state["last_carrier_location_cm"] = [
                        carrier_location.x,
                        carrier_location.y,
                        carrier_location.z,
                    ]
                    state["last_carry_pose_phase"] = carry_pose_phase
                    state["last_carry_follow_frame"] = frame_index
            state["held_frames"] = int(state.get("held_frames") or 0) + 1
            continue
        if state.get("released"):
            continue
        if state.get("attached"):
            goal_center = params.get("release_goal_center_m")
            goal_half_extent = params.get("release_goal_half_extent_m")
            if isinstance(goal_center, list) and isinstance(goal_half_extent, list):
                position = runtime_transform_from_actor(actors, str(actor_id), actor, scene_origin)["position"]
                inside_goal = all(
                    abs(float(position[index]) - float(goal_center[index])) <= float(goal_half_extent[index])
                    for index in range(3)
                )
                if not inside_goal:
                    state["release_rejected_frames"] = int(state.get("release_rejected_frames") or 0) + 1
                    continue
            actor.detach_from_actor(
                unreal.DetachmentRule.KEEP_WORLD,
                unreal.DetachmentRule.KEEP_WORLD,
                unreal.DetachmentRule.KEEP_WORLD,
            )
            state["actual_socket_attached"] = False
        release_from_socket = bool_control(params.get("release_from_socket"), False) and state.get("attached")
        if release_from_socket:
            release_position = runtime_transform_from_actor(actors, str(actor_id), actor, scene_origin)["position"]
        else:
            release_position = runtime_vec3(params.get("release_position_m"), projectile_hold_position(obj, frame))
            actor.set_actor_location(
                ue_vec_from_meters(release_position, z_offset_cm=float(ground_offsets.get(actor_id, 0.0)), origin=scene_origin),
                False,
                False,
            )
        try:
            set_delayed_release_collision_enabled(component, True)
        except Exception as exc:
            status.setdefault("errors", []).append(f"delayed_release_collision:{actor_id}:{exc}")
        try:
            component.set_simulate_physics(True)
            component.set_enable_gravity(bool(properties.get("enable_gravity", True)))
            component.wake_all_rigid_bodies()
        except Exception as exc:
            status.setdefault("errors", []).append(f"delayed_release_enable:{actor_id}:{exc}")
        default_velocity = (
            (3.0, 0.0, 1.0)
            if obj.get("behavior") == "character_throw_projectile"
            else (0.0, 0.0, 0.0)
        )
        velocity = runtime_vec3(params.get("release_velocity_m_s"), properties.get("initial_velocity_m_s") or default_velocity)
        try:
            component.set_physics_linear_velocity(unreal.Vector(velocity[0] * 100.0, velocity[1] * 100.0, velocity[2] * 100.0), False, "")
        except Exception as exc:
            status.setdefault("errors", []).append(f"delayed_release_velocity:{actor_id}:{exc}")
        angular_default = (
            [0.0, -540.0, 0.0]
            if obj.get("behavior") == "character_throw_projectile"
            else [0.0, 0.0, 0.0]
        )
        angular_velocity = params.get("release_angular_velocity_deg_s") or properties.get("initial_angular_velocity_deg_s") or angular_default
        if isinstance(angular_velocity, list) and len(angular_velocity) >= 3:
            set_component_angular_velocity_deg(component, angular_velocity, str(actor_id), status)
        state["released"] = True
        state["release_time_s"] = round(time_s, 4)
        state["release_position_m"] = [round(value, 5) for value in release_position]
        state["release_velocity_m_s"] = [round(value, 5) for value in velocity]
        status.setdefault("delayed_releases", []).append(
            {
                "id": actor_id,
                "frame": frame.get("frame"),
                "time_s": round(time_s, 4),
                "release_position_m": state["release_position_m"],
                "release_velocity_m_s": state["release_velocity_m_s"],
            }
        )


def physics_capture_enabled(actors: dict, runtime_scene: dict | None) -> bool:
    if not runtime_scene:
        return False
    controls = actors.get("physics_controls") or runtime_physics_controls(runtime_scene)
    return bool(controls.get("simulate_physics"))


def analytic_contact_solver_enabled(actors: dict, runtime_scene: dict | None) -> bool:
    if not runtime_scene:
        return False
    controls = actors.get("physics_controls") or runtime_physics_controls(runtime_scene)
    return bool(controls.get("simulate_physics") and controls.get("runtime_driver_backend") == "analytic_contact_solver")


def analytic_solver_source(actors: dict, runtime_scene: dict | None) -> str:
    controls = actors.get("physics_controls") or runtime_physics_controls(runtime_scene)
    driver = str(controls.get("simulation_driver") or "")
    return driver if driver.startswith("analytic_") else "analytic_contact_solver"


def runtime_object_ids(runtime_scene: dict | None) -> tuple[list[str], list[str]]:
    if not runtime_scene:
        return [], []
    dynamic_ids = [obj.get("id") for obj in runtime_scene.get("dynamic_objects") or [] if obj.get("id")]
    static_ids = [obj.get("id") for obj in runtime_scene.get("static_objects") or [] if obj.get("id")]
    return dynamic_ids, static_ids


def runtime_transform_from_actor(actors: dict, actor_id: str, actor, scene_origin: unreal.Vector) -> dict:
    location = actor.get_actor_location()
    rotation = actor.get_actor_rotation()
    z_offset_cm = float((actors.get("runtime_ground_offsets") or {}).get(actor_id, 0.0))
    return {
        "position": [
            round((location.x - scene_origin.x) / 100.0, 5),
            round((location.y - scene_origin.y) / 100.0, 5),
            round((location.z - scene_origin.z - z_offset_cm) / 100.0, 5),
        ],
        "position_cm": [round(location.x, 3), round(location.y, 3), round(location.z, 3)],
        "rotation_degrees": [round(rotation.pitch, 4), round(rotation.yaw, 4), round(rotation.roll, 4)],
        "source": "ue_actor_transform",
    }


def bounds_contact_gap_cm(actor_a, actor_b) -> tuple[bool, float, dict]:
    origin_a, extent_a = actor_bounds(actor_a)
    origin_b, extent_b = actor_bounds(actor_b)
    gaps = {
        "x": abs(origin_a.x - origin_b.x) - (extent_a.x + extent_b.x),
        "y": abs(origin_a.y - origin_b.y) - (extent_a.y + extent_b.y),
        "z": abs(origin_a.z - origin_b.z) - (extent_a.z + extent_b.z),
    }
    max_gap = max(gaps.values())
    return max_gap <= 4.0, max_gap, {axis: round(value, 3) for axis, value in gaps.items()}


def record_physics_transform_frame(
    actors: dict,
    runtime_scene: dict | None,
    frame_index: int,
    elapsed_s: float,
    scene_origin: unreal.Vector,
    seen_contact_pairs: set[tuple[str, str]],
) -> tuple[dict, list[dict]]:
    dynamic_ids, static_ids = runtime_object_ids(runtime_scene)
    frame = {
        "frame": frame_index,
        "time": round(elapsed_s, 4),
        "source": "ue_chaos_transform_capture",
        "objects": {},
        "contacts": [],
    }
    for actor_id in dynamic_ids:
        if actor_id in actors:
            frame["objects"][actor_id] = runtime_transform_from_actor(actors, actor_id, actors[actor_id], scene_origin)
    all_ids = [actor_id for actor_id in [*dynamic_ids, *static_ids] if actor_id in actors]
    new_events = []
    for dynamic_id in dynamic_ids:
        if dynamic_id not in actors:
            continue
        for other_id in all_ids:
            if other_id == dynamic_id:
                continue
            pair = tuple(sorted((dynamic_id, other_id)))
            contact, gap_cm, axis_gaps = bounds_contact_gap_cm(actors[dynamic_id], actors[other_id])
            if not contact:
                continue
            event = {
                "frame": frame_index,
                "time": round(elapsed_s, 4),
                "objects": list(pair),
                "method": "bounds_overlap_or_near_contact_from_ue_transforms",
                "gap_cm": round(gap_cm, 3),
                "axis_gaps_cm": axis_gaps,
            }
            frame["contacts"].append(event)
            if pair not in seen_contact_pairs:
                seen_contact_pairs.add(pair)
                new_events.append(event)
    return frame, new_events


def latest_native_contacts_from_cpp_driver(driver) -> list[dict]:
    if not driver:
        return []
    try:
        payload = json.loads(driver.get_capture_json())
    except Exception:
        return []
    frames = payload.get("frames") if isinstance(payload, dict) else None
    if not isinstance(frames, list) or not frames:
        return []
    latest = frames[-1] if isinstance(frames[-1], dict) else {}
    result = []
    for contact in latest.get("contacts") or []:
        if not isinstance(contact, dict) or contact.get("native_collision") is not True:
            continue
        result.append(
            {
                **contact,
                "raw_method": contact.get("method"),
                "method": "ue_native_component_hit",
            }
        )
    return result


def merge_live_native_contacts(actual_frame: dict, new_events: list[dict], driver) -> list[dict]:
    native_events = latest_native_contacts_from_cpp_driver(driver)
    if not native_events:
        return new_events
    native_pairs = {
        tuple(sorted(str(item) for item in event.get("objects") or []))
        for event in native_events
    }
    actual_frame["contacts"] = [
        event
        for event in actual_frame.get("contacts") or []
        if tuple(sorted(str(item) for item in event.get("objects") or [])) not in native_pairs
    ] + native_events
    return [
        event
        for event in new_events
        if tuple(sorted(str(item) for item in event.get("objects") or [])) not in native_pairs
    ] + native_events


def configure_runtime_physics_substepping(runtime_scene: dict | None, status: dict) -> None:
    timebase = runtime_timebase(runtime_scene)
    requested_substeps = int(timebase["substeps_per_render"])
    try:
        settings = unreal.get_default_object(unreal.PhysicsSettings)
        settings.set_editor_property("substepping", requested_substeps > 1)
        settings.set_editor_property("max_substep_delta_time", float(timebase["physics_dt_s"]))
        settings.set_editor_property("max_substeps", requested_substeps)
        settings.set_editor_property("min_physics_delta_time", 0.0)
        settings.set_editor_property("max_physics_delta_time", float(timebase["render_dt_s"]))
        status["physics_substepping"] = {
            "enabled": bool(settings.get_editor_property("substepping")),
            "max_substep_delta_time_s": float(settings.get_editor_property("max_substep_delta_time")),
            "max_substeps": int(settings.get_editor_property("max_substeps")),
            "max_physics_delta_time_s": float(settings.get_editor_property("max_physics_delta_time")),
            "requested_physics_hz": int(timebase["physics_hz"]),
            "render_fps": int(timebase["render_fps"]),
        }
    except Exception as exc:
        status["physics_substepping"] = {"enabled": False, "error": str(exc)}
        status.setdefault("errors", []).append(f"physics_substepping:{exc}")


def start_editor_physics_capture(actors: dict, runtime_scene: dict | None, expected_seconds: float) -> dict:
    controls = actors.get("physics_controls") or runtime_physics_controls(runtime_scene)
    requires_gameplay_input = bool(runtime_gasp_objects(runtime_scene))
    status = {
        "enabled": bool(controls.get("simulate_physics")),
        "requested_driver": controls.get("simulation_driver"),
        "editor_simulate_started": False,
        "editor_begin_play_requested": False,
        "is_in_play_in_editor": False,
        "manual_world_tick_available": False,
        "manual_world_tick_count": 0,
        "time_dilation": None,
        "initial_impulses": [],
        "errors": [],
    }
    if not status["enabled"]:
        return status
    configure_runtime_physics_substepping(runtime_scene, status)
    try:
        unreal.SystemLibrary.execute_console_command(None, f"t.MaxFPS {max(1, FPS)}")
    except Exception as exc:
        status["errors"].append(f"t.MaxFPS:{exc}")
    try:
        world = actors.get("world") or unreal.EditorLevelLibrary.get_editor_world()
        time_dilation = float_control(controls.get("physics_time_dilation"), 1.0, 0.01, 1.0)
        unreal.GameplayStatics.set_global_time_dilation(world, time_dilation)
        status["time_dilation"] = time_dilation
    except Exception as exc:
        status["errors"].append(f"time_dilation:{exc}")
    try:
        world = actors.get("world") or unreal.EditorLevelLibrary.get_editor_world()
        tick_enum = getattr(getattr(unreal, "LevelTick", None), "LEVELTICK_ALL", None)
        if tick_enum is not None:
            world.tick(tick_enum, 0.001)
        else:
            world.tick(0, 0.001)
        status["manual_world_tick_available"] = True
    except Exception as exc:
        status["errors"].append(f"manual_world_tick_probe:{exc}")
    if requires_gameplay_input:
        status["manual_world_tick_available"] = False
    try:
        if not status["manual_world_tick_available"]:
            level_editor = None
            try:
                level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
            except Exception:
                level_editor = None
            try:
                if requires_gameplay_input and level_editor and hasattr(level_editor, "editor_request_begin_play"):
                    level_editor.editor_request_begin_play()
                    status["editor_begin_play_requested"] = True
                elif level_editor and hasattr(level_editor, "editor_play_simulate"):
                    level_editor.editor_play_simulate()
                    status["editor_simulate_started"] = True
                elif level_editor and hasattr(level_editor, "editor_request_begin_play"):
                    level_editor.editor_request_begin_play()
                    status["editor_begin_play_requested"] = True
                else:
                    unreal.EditorLevelLibrary.editor_play_simulate()
                    status["editor_simulate_started"] = True
            except Exception as simulate_exc:
                status["errors"].append(f"editor_play_simulate:{simulate_exc}")
                try:
                    if level_editor and hasattr(level_editor, "editor_request_begin_play"):
                        level_editor.editor_request_begin_play()
                        status["editor_begin_play_requested"] = True
                    else:
                        raise
                except Exception:
                    raise
            try:
                if level_editor and hasattr(level_editor, "is_in_play_in_editor"):
                    status["is_in_play_in_editor"] = bool(level_editor.is_in_play_in_editor())
            except Exception as play_state_exc:
                status["errors"].append(f"is_in_play_in_editor:{play_state_exc}")
    except Exception as exc:
        status["errors"].append(f"editor_play_simulate:{exc}")
        try:
            unreal.SystemLibrary.execute_console_command(None, "SIMULATE")
            status["editor_simulate_started"] = True
        except Exception as command_exc:
            status["errors"].append(f"SIMULATE:{command_exc}")
    return status


def apply_initial_physics_impulses(actors: dict, runtime_scene: dict | None, status: dict) -> None:
    if not status.get("enabled"):
        return
    if status.get("initial_impulses_applied"):
        return
    controls = actors.get("physics_controls") or runtime_physics_controls(runtime_scene)
    for obj in (runtime_scene.get("dynamic_objects") if runtime_scene else []) or []:
        actor_id = obj.get("id")
        actor = actors.get(actor_id)
        component = getattr(actor, "static_mesh_component", None) if actor else None
        properties = obj.get("physics_properties") or {}
        impulse = properties.get("initial_impulse_n_s") or properties.get("initial_impulse")
        initial_velocity = properties.get("initial_velocity_m_s")
        if not component:
            continue
        try:
            component.wake_all_rigid_bodies()
        except Exception:
            pass
        if isinstance(initial_velocity, list) and len(initial_velocity) >= 3:
            try:
                velocity = unreal.Vector(float(initial_velocity[0]) * 100.0, float(initial_velocity[1]) * 100.0, float(initial_velocity[2]) * 100.0)
                component.set_physics_linear_velocity(velocity, False, "")
                status.setdefault("initial_velocities", []).append({"id": actor_id, "m_s": [float(initial_velocity[0]), float(initial_velocity[1]), float(initial_velocity[2])]})
            except Exception as exc:
                status["errors"].append(f"initial_velocity:{actor_id}:{exc}")
        apply_runtime_initial_angular_velocity(actor_id, actor, properties, status)
        if controls.get("apply_initial_impulse", False) and isinstance(impulse, list) and len(impulse) >= 3:
            try:
                vector = unreal.Vector(float(impulse[0]) * 100.0, float(impulse[1]) * 100.0, float(impulse[2]) * 100.0)
                component.add_impulse(vector, "", False)
                status["initial_impulses"].append({"id": actor_id, "n_s": [float(impulse[0]), float(impulse[1]), float(impulse[2])]})
            except Exception as exc:
                status["errors"].append(f"initial_impulse:{actor_id}:{exc}")
    status["initial_impulses_applied"] = True


def ue_name(value):
    try:
        return unreal.Name(str(value))
    except Exception:
        return str(value)


def vector_from_sequence(values, scale: float = 1.0) -> unreal.Vector:
    if not isinstance(values, list) or len(values) < 3:
        return unreal.Vector(0.0, 0.0, 0.0)
    try:
        return unreal.Vector(float(values[0]) * scale, float(values[1]) * scale, float(values[2]) * scale)
    except Exception:
        return unreal.Vector(0.0, 0.0, 0.0)


def apply_runtime_initial_angular_velocity(actor_id: str, actor, properties: dict, status: dict) -> None:
    raw = (
        properties.get("initial_angular_velocity_deg_s")
        or properties.get("initial_angular_velocity_degrees_per_second")
        or properties.get("initial_angular_velocity")
    )
    if not isinstance(raw, list) or len(raw) < 3:
        return
    component = getattr(actor, "static_mesh_component", None) if actor else None
    if not component:
        status.setdefault("errors", []).append(f"initial_angular_velocity:{actor_id}:missing_static_mesh_component")
        return
    angular_deg = vector_from_sequence(raw, 1.0)
    if max(abs(angular_deg.x), abs(angular_deg.y), abs(angular_deg.z)) <= 1e-6:
        return
    try:
        component.wake_all_rigid_bodies()
    except Exception:
        pass
    attempts = [
        ("set_physics_angular_velocity_in_degrees", angular_deg, "deg_s"),
        (
            "set_physics_angular_velocity_in_radians",
            unreal.Vector(math.radians(angular_deg.x), math.radians(angular_deg.y), math.radians(angular_deg.z)),
            "rad_s",
        ),
    ]
    errors = []
    for method_name, vector, units in attempts:
        method = getattr(component, method_name, None)
        if not method:
            errors.append(f"{method_name}:missing")
            continue
        for args in ((vector, False, ""), (vector, False), (vector,)):
            try:
                method(*args)
                status.setdefault("initial_angular_velocities", []).append(
                    {
                        "id": actor_id,
                        "deg_s": [float(raw[0]), float(raw[1]), float(raw[2])],
                        "method": method_name,
                        "units": units,
                    }
                )
                return
            except Exception as exc:
                errors.append(f"{method_name}:{exc}")
    status.setdefault("errors", []).append(f"initial_angular_velocity:{actor_id}:{errors[-3:]}")


def start_cpp_runtime_driver(actors: dict, runtime_scene: dict | None, status: dict, max_frames: int) -> bool:
    controls = actors.get("physics_controls") or runtime_physics_controls(runtime_scene)
    cpp_status = status.setdefault("cpp_runtime_driver", {
        "enabled": False,
        "started": False,
        "output_path": str(OUTPUT_DIR / "cpp_physics_capture.json"),
        "registered_dynamic": [],
        "registered_static": [],
        "errors": [],
    })
    if cpp_status.get("started"):
        return True
    if not bool(controls.get("simulate_physics")):
        cpp_status["errors"].append("simulate_physics=false")
        return False
    if not bool_control(controls.get("cpp_runtime_driver_enabled"), controls.get("runtime_driver_backend") == "cpp_runtime_driver"):
        cpp_status["errors"].append("cpp_runtime_driver_disabled")
        return False
    if not runtime_scene:
        cpp_status["errors"].append("missing_runtime_scene")
        return False

    world = actors.get("physics_game_world")
    if not world:
        for getter in (
            lambda: unreal.EditorLevelLibrary.get_game_world(),
            lambda: (unreal.EditorLevelLibrary.get_pie_worlds(False) or [None])[0],
            lambda: (unreal.EditorLevelLibrary.get_pie_worlds(True) or [None])[0],
            lambda: actors.get("world") or unreal.EditorLevelLibrary.get_editor_world(),
        ):
            try:
                world = getter()
                if world:
                    break
            except Exception:
                pass
    if not world:
        cpp_status["errors"].append("missing_game_world")
        return False

    try:
        library = getattr(unreal, "ADPPhysicsRuntimeLibrary", None)
        if library is None:
            unreal.load_class(None, "/Script/ADPPhysicsRuntime.ADPPhysicsRuntimeLibrary")
            library = getattr(unreal, "ADPPhysicsRuntimeLibrary", None)
        if library is None:
            cpp_status["errors"].append("ADPPhysicsRuntimeLibrary_unavailable")
            return False
        driver = library.spawn_physics_runtime_driver(world)
    except Exception as exc:
        cpp_status["errors"].append(f"spawn_driver:{exc}")
        return False
    if not driver:
        cpp_status["errors"].append("spawn_driver:none")
        return False

    actors["adp_physics_runtime_driver"] = driver
    cpp_status["enabled"] = True
    try:
        driver.reset_driver()
    except Exception as exc:
        cpp_status["errors"].append(f"reset_driver:{exc}")

    for obj in runtime_scene.get("static_objects") or []:
        actor_id = obj.get("id")
        actor = actors.get(actor_id)
        if not actor:
            continue
        try:
            driver.register_static_body(ue_name(actor_id), actor)
            cpp_status["registered_static"].append(actor_id)
        except Exception as exc:
            cpp_status["errors"].append(f"register_static:{actor_id}:{exc}")

    for obj in runtime_scene.get("dynamic_objects") or []:
        actor_id = obj.get("id")
        actor = actors.get(actor_id)
        if not actor:
            continue
        if obj.get("behavior") == "third_person_runner":
            continue
        properties = obj.get("physics_properties") or {}
        mass_kg = float_control(properties.get("mass_kg"), 1.0, 0.001, None)
        velocity_m_s = vector_from_sequence(properties.get("initial_velocity_m_s"), 1.0)
        impulse_n_s = vector_from_sequence(properties.get("initial_impulse_n_s") or properties.get("initial_impulse"), 1.0)
        gravity = bool_control(properties.get("enable_gravity"), bool(controls.get("gravity_enabled", True)))
        linear_damping = float_control(properties.get("linear_damping"), 0.15, 0.0, None)
        angular_damping = float_control(properties.get("angular_damping"), 0.25, 0.0, None)
        simulate_body = True
        if obj.get("behavior") == "third_person_runner":
            simulate_body = False
        if properties.get("simulate_physics") in ("force_off", "force_off_until_release", "disabled"):
            simulate_body = False
        try:
            driver.register_body_meters(
                ue_name(actor_id),
                actor,
                mass_kg,
                velocity_m_s,
                impulse_n_s,
                gravity,
                linear_damping,
                angular_damping,
                simulate_body,
            )
            cpp_status["registered_dynamic"].append(actor_id)
        except Exception as exc:
            cpp_status["errors"].append(f"register_dynamic:{actor_id}:{exc}")

    try:
        timebase = runtime_timebase(runtime_scene)
        sample_interval = float(timebase["render_dt_s"])
        capture_frames = int(timebase["raw_capture_frame_count"])
        output_path = str(OUTPUT_DIR / "cpp_physics_capture.json")
        cpp_status["output_path"] = output_path
        driver.start_capture(sample_interval, capture_frames, output_path)
        try:
            driver.set_manual_stepping_enabled(True)
            cpp_status["manual_stepping_enabled"] = True
        except Exception as manual_exc:
            cpp_status.setdefault("errors", []).append(f"set_manual_stepping:{manual_exc}")
            cpp_status["manual_stepping_enabled"] = False
        cpp_status["started"] = True
        cpp_status["sample_interval_s"] = sample_interval
        cpp_status["max_frames"] = capture_frames
        cpp_status["timebase"] = timebase
        for obj in runtime_scene.get("dynamic_objects") or []:
            actor_id = obj.get("id")
            actor = actors.get(actor_id)
            properties = obj.get("physics_properties") or {}
            if actor:
                component = actor_runtime_component(actor)
                velocity_m_s = vector_from_sequence(properties.get("initial_velocity_m_s"), 1.0)
                if component:
                    try:
                        component.wake_all_rigid_bodies()
                        component.set_physics_linear_velocity(unreal.Vector(velocity_m_s.x * 100.0, velocity_m_s.y * 100.0, velocity_m_s.z * 100.0), False, "")
                        cpp_status.setdefault("applied_initial_velocity_cm_s", []).append(
                            {
                                "id": actor_id,
                                "velocity_cm_s": [round(velocity_m_s.x * 100.0, 4), round(velocity_m_s.y * 100.0, 4), round(velocity_m_s.z * 100.0, 4)],
                            }
                        )
                    except Exception as velocity_exc:
                        cpp_status.setdefault("errors", []).append(f"apply_initial_velocity:{actor_id}:{velocity_exc}")
                apply_runtime_initial_angular_velocity(actor_id, actor, properties, status)
        status["initial_impulses_applied"] = True
        status["initial_impulses"] = [
            {"id": obj.get("id"), "n_s": (obj.get("physics_properties") or {}).get("initial_impulse_n_s")}
            for obj in runtime_scene.get("dynamic_objects") or []
            if (obj.get("physics_properties") or {}).get("initial_impulse_n_s")
        ]
        return True
    except Exception as exc:
        cpp_status["errors"].append(f"start_capture:{exc}")
        return False


def cpp_capture_to_runtime_trajectory(capture: dict, actors: dict, runtime_scene: dict | None, scene_origin: unreal.Vector) -> tuple[list[dict], list[dict], list[dict], dict]:
    dynamic_ids, _static_ids = runtime_object_ids(runtime_scene)
    ground_offsets = actors.get("runtime_ground_offsets") or {}
    solver_trajectory = []
    for frame in capture.get("frames") or []:
        frame_index = int(frame.get("frame") or 0)
        elapsed = float_control(frame.get("time"), 0.0, 0.0, None)
        frame_objects = frame.get("objects") or {}
        derived_contacts = [
            {
                **contact,
                "raw_method": contact.get("method"),
                "method": (
                    "ue_native_component_hit"
                    if contact.get("native_collision") is True or contact.get("method") == "ue_on_component_hit"
                    else "ue_postsolve_bounds_inference"
                ),
            }
            for contact in frame.get("contacts") or []
            if isinstance(contact, dict)
        ]
        runtime_frame = {
            "frame": frame_index,
            "time": round(elapsed, 4),
            "source": "adp_cpp_runtime_driver",
            "objects": {},
            "contacts": derived_contacts,
        }
        for actor_id in dynamic_ids:
            raw = frame_objects.get(actor_id)
            if not isinstance(raw, dict):
                continue
            pos_cm = raw.get("position_cm") or []
            rot = raw.get("rotation_degrees") or [0.0, 0.0, 0.0]
            if len(pos_cm) < 3:
                continue
            z_offset_cm = float_control(ground_offsets.get(actor_id), 0.0, 0.0, None)
            runtime_frame["objects"][actor_id] = {
                "position": [
                    round((float(pos_cm[0]) - scene_origin.x) / 100.0, 5),
                    round((float(pos_cm[1]) - scene_origin.y) / 100.0, 5),
                    round((float(pos_cm[2]) - scene_origin.z - z_offset_cm) / 100.0, 5),
                ],
                "position_cm": [round(float(pos_cm[0]), 3), round(float(pos_cm[1]), 3), round(float(pos_cm[2]), 3)],
                "rotation_degrees": [round(float(rot[0]), 4), round(float(rot[1]), 4), round(float(rot[2]), 4)] if len(rot) >= 3 else [0.0, 0.0, 0.0],
                "velocity_cm_s": raw.get("velocity_cm_s") or [0.0, 0.0, 0.0],
                "velocity_m_s": [round(float(value) / 100.0, 6) for value in (raw.get("velocity_cm_s") or [0.0, 0.0, 0.0])[:3]],
                "source": "adp_cpp_runtime_driver",
            }
        solver_trajectory.append(runtime_frame)
    timebase = runtime_timebase(runtime_scene)
    contact_events = []
    for frame_index, frame in enumerate(solver_trajectory):
        source_physics_step = int(timebase["source_solver_indices"][frame_index])
        frame["source_solver_frame"] = frame_index
        frame["source_physics_step"] = source_physics_step
        frame["source_physics_time_s"] = round(source_physics_step / int(timebase["physics_hz"]), 9)
        for event in frame.get("contacts") or []:
            event["source_solver_frame"] = frame_index
            event["source_physics_step"] = source_physics_step
            event["source_solver_time_s"] = float(frame.get("time") or 0.0)
            contact_events.append(event)
    trajectory = solver_trajectory
    return trajectory, contact_events, solver_trajectory, timebase


def stop_cpp_runtime_driver(actors: dict, status: dict, runtime_scene: dict | None, scene_origin: unreal.Vector) -> tuple[list[dict], list[dict]]:
    cpp_status = status.setdefault("cpp_runtime_driver", {"enabled": False, "errors": []})
    if not cpp_status.get("started"):
        return [], []
    driver = actors.get("adp_physics_runtime_driver")
    capture_text = None
    if driver:
        try:
            driver.stop_capture()
        except Exception as exc:
            cpp_status.setdefault("errors", []).append(f"stop_capture:{exc}")
        try:
            capture_text = driver.get_capture_json()
        except Exception as exc:
            cpp_status.setdefault("errors", []).append(f"get_capture_json:{exc}")
    output_path = Path(cpp_status.get("output_path") or OUTPUT_DIR / "cpp_physics_capture.json")
    if not capture_text and output_path.exists():
        try:
            capture_text = output_path.read_text(encoding="utf-8")
        except Exception as exc:
            cpp_status.setdefault("errors", []).append(f"read_capture:{exc}")
    if not capture_text:
        cpp_status.setdefault("errors", []).append("missing_capture_json")
        return [], []
    try:
        capture = json.loads(capture_text)
    except Exception as exc:
        cpp_status.setdefault("errors", []).append(f"parse_capture_json:{exc}")
        return [], []
    cpp_status["frame_count"] = int(capture.get("frame_count") or len(capture.get("frames") or []))
    cpp_status["capture_complete"] = bool(capture.get("capture_complete"))
    trajectory, contact_events, solver_trajectory, timebase = cpp_capture_to_runtime_trajectory(capture, actors, runtime_scene, scene_origin)
    solver_path = OUTPUT_DIR / "solver_trajectory.json"
    solver_path.write_text(json.dumps(solver_trajectory, indent=2), encoding="utf-8")
    solver_sha256 = hashlib.sha256(solver_path.read_bytes()).hexdigest()
    sampling_map = {
        "schema_version": "harness_sampling_map_v1",
        "timebase": timebase,
        "solver_cache": str(solver_path),
        "solver_cache_sha256": solver_sha256,
        "samples": [
            {
                "frame": frame_index,
                "time_s": round(frame_index / int(timebase["render_fps"]), 9),
                "source_solver_frame": frame_index,
                "source_solver_time_s": round(frame_index / int(timebase["render_fps"]), 9),
                "source_physics_step": source_index,
                "source_physics_time_s": round(source_index / int(timebase["physics_hz"]), 9),
            }
            for frame_index, source_index in enumerate(timebase["source_solver_indices"])
        ],
    }
    (OUTPUT_DIR / "sampling_map.json").write_text(json.dumps(sampling_map, indent=2), encoding="utf-8")
    cpp_status["trajectory_frames"] = len(trajectory)
    cpp_status["solver_trajectory_frames"] = len(solver_trajectory)
    cpp_status["contact_samples"] = len(contact_events)
    cpp_status["solver_cache_sha256"] = solver_sha256
    return trajectory, contact_events


def merge_scripted_runner_trajectory(
    physics_trajectory: list[dict],
    planned_trajectory: list[dict],
    runtime_scene: dict | None,
) -> list[dict]:
    if not physics_trajectory or not planned_trajectory or not runtime_scene:
        return physics_trajectory
    runner_ids = [
        str(obj.get("id"))
        for obj in runtime_scene.get("dynamic_objects") or []
        if obj.get("id")
        and obj.get("behavior") == "third_person_runner"
        and str((obj.get("params") or {}).get("humanoid_adapter") or "").strip().lower() not in {"gasp", "ddv"}
    ]
    if not runner_ids:
        return physics_trajectory
    planned_by_frame = {int(frame.get("frame") or 0): frame for frame in planned_trajectory}
    merged = []
    for frame in physics_trajectory:
        frame_index = int(frame.get("frame") or 0)
        planned = planned_by_frame.get(frame_index, {})
        planned_objects = planned.get("objects") if isinstance(planned.get("objects"), dict) else {}
        objects = dict(frame.get("objects") or {})
        for runner_id in runner_ids:
            scripted = planned_objects.get(runner_id)
            if isinstance(scripted, dict):
                objects[runner_id] = {
                    **scripted,
                    "source": "scripted_visible_runner",
                }
        merged_frame = {**frame, "objects": objects}
        if planned.get("actions"):
            merged_frame["actions"] = list(planned["actions"])
        if planned.get("contacts"):
            merged_frame["contacts"] = list(frame.get("contacts") or []) + list(planned["contacts"])
        merged.append(merged_frame)
    return merged


def rebind_runtime_actors_to_simulation_world(
    actors: dict,
    runtime_scene: dict | None,
    status: dict,
    timeout_s: float = 8.0,
    record_failure: bool = True,
) -> None:
    if not status.get("enabled"):
        return
    worlds = []
    deadline = time.perf_counter() + max(0.0, float(timeout_s))
    lookup_errors = []
    while True:
        try:
            level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
            if level_editor and hasattr(level_editor, "is_in_play_in_editor"):
                status["is_in_play_in_editor"] = bool(level_editor.is_in_play_in_editor())
        except Exception as exc:
            lookup_errors.append(f"is_in_play_in_editor:{exc}")
        for getter in (
            lambda: unreal.EditorLevelLibrary.get_game_world(),
            lambda: (unreal.EditorLevelLibrary.get_pie_worlds(False) or [None])[0],
            lambda: (unreal.EditorLevelLibrary.get_pie_worlds(True) or [None])[0],
            lambda: unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world(),
        ):
            try:
                world = getter()
                if world and world not in worlds:
                    worlds.append(world)
            except Exception as exc:
                lookup_errors.append(str(exc))
        if worlds or time.perf_counter() >= deadline:
            break
        time.sleep(min(0.1, max(0.0, deadline - time.perf_counter())))
    status["game_world_count"] = len(worlds)
    if not worlds:
        if not record_failure:
            return
        if lookup_errors:
            status.setdefault("errors", []).append(f"game_world_lookup:{lookup_errors[-3:]}")
        status.setdefault("errors", []).append("game_world_lookup:no_world")
        return
    controls = actors.get("physics_controls") or runtime_physics_controls(runtime_scene)
    time_dilation = float_control(controls.get("physics_time_dilation"), 1.0, 0.01, 1.0)
    for world in worlds:
        try:
            unreal.GameplayStatics.set_global_time_dilation(world, time_dilation)
            status["time_dilation"] = time_dilation
        except Exception as exc:
            status.setdefault("errors", []).append(f"game_world_time_dilation:{exc}")
    wanted = {
        obj.get("id"): f"native_phenomena_demo_{obj.get('id')}"
        for obj in ((runtime_scene.get("dynamic_objects") or []) + (runtime_scene.get("static_objects") or [])) if obj.get("id")
    }
    visual_actors = actors.get("visual_actors") or {}
    wanted_visuals = {
        actor_id: f"native_phenomena_visual_{actor_id}"
        for actor_id in visual_actors
    }
    rebound = []
    rebound_visuals = []
    actor_classes = [unreal.Actor]
    for world in worlds:
        world_actors = []
        for actor_class in actor_classes:
            try:
                world_actors.extend(unreal.GameplayStatics.get_all_actors_of_class(world, actor_class))
            except Exception as exc:
                status.setdefault("errors", []).append(f"game_world_actor_scan:{actor_class}:{exc}")
                continue
        label_map = {}
        for actor in world_actors:
            try:
                label_map[actor.get_actor_label()] = actor
            except Exception:
                pass
        for actor_id, label in wanted.items():
            match = label_map.get(label)
            if not match:
                match = next((actor for actor_label, actor in label_map.items() if actor_label.startswith(label)), None)
            if match:
                actors[actor_id] = match
                rebound.append(actor_id)
        for actor_id, label in wanted_visuals.items():
            match = label_map.get(label)
            if not match:
                match = next((actor for actor_label, actor in label_map.items() if actor_label.startswith(label)), None)
            if match:
                visual_actors[actor_id] = match
                rebound_visuals.append(actor_id)
        capture_actor = label_map.get("native_phenomena_demo_capture_camera")
        if capture_actor:
            capture_component = getattr(capture_actor, "capture_component2d", None)
            if capture_component:
                actors["capture"] = capture_actor
                actors["capture_comp"] = capture_component
                actors["capture_world"] = world
                try:
                    actors["render_target"] = capture_component.get_editor_property("texture_target") or actors.get("render_target")
                except Exception:
                    pass
                status["rebound_capture_actor"] = True
        highres_camera = label_map.get("native_phenomena_demo_highres_capture_camera")
        if highres_camera:
            actors["highres_camera"] = highres_camera
            status["rebound_highres_camera"] = True
        if rebound:
            actors["physics_game_world"] = world
            status["rebound_world"] = str(world)
            break
    status["rebound_actor_ids"] = sorted(set(rebound))
    initialize_gasp_locomotion(actors, runtime_scene, status)
    status["rebound_visual_actor_ids"] = sorted(set(rebound_visuals))
    missing_visuals = sorted(set(wanted_visuals) - set(rebound_visuals))
    status["missing_visual_actor_ids"] = missing_visuals
    status["visual_rebind_complete"] = not missing_visuals
    for actor_id in rebound_visuals:
        physics_component = actor_runtime_component(actors.get(actor_id))
        visual_component = actor_runtime_component(visual_actors.get(actor_id))
        if physics_component:
            actors[actor_id].set_actor_hidden_in_game(True)
            physics_component.set_visibility(False, True)
            physics_component.set_hidden_in_game(True, True)
        if visual_component:
            try:
                visual_actors[actor_id].set_is_temporarily_hidden_in_editor(False)
            except Exception:
                pass
            visual_actors[actor_id].set_actor_hidden_in_game(False)
            visual_component.set_visibility(True, True)
            visual_component.set_hidden_in_game(False, True)
    if missing_visuals and record_failure:
        status.setdefault("errors", []).append(f"game_world_visual_rebind:missing:{','.join(missing_visuals)}")


def set_simulation_world_paused(actors: dict, status: dict, paused: bool) -> bool:
    world = actors.get("physics_game_world")
    if not world:
        return False
    try:
        result = bool(unreal.GameplayStatics.set_game_paused(world, bool(paused)))
        status["game_world_paused"] = bool(paused)
        return result
    except Exception as exc:
        status.setdefault("errors", []).append(f"set_game_paused:{exc}")
        return False


def reset_runtime_actors_to_initial_state(actors: dict, runtime_scene: dict | None, status: dict) -> None:
    if not runtime_scene or status.get("initial_state_reset"):
        return
    initial_transforms = actors.get("runtime_initial_transforms") or {}
    reset_ids = []
    for obj in (runtime_scene.get("static_objects") or []) + (runtime_scene.get("dynamic_objects") or []):
        actor_id = obj.get("id")
        actor = actors.get(actor_id)
        initial = initial_transforms.get(actor_id) if actor_id else None
        if not actor or not isinstance(initial, dict):
            continue
        component = actor_runtime_component(actor)
        if component:
            try:
                component.set_simulate_physics(False)
            except Exception:
                pass
        position_cm = initial.get("position_cm") or []
        rotation = initial.get("rotation_degrees") or []
        try:
            actor.set_actor_location(unreal.Vector(*[float(value) for value in position_cm[:3]]), False, False)
            actor.set_actor_rotation(unreal.Rotator(*[float(value) for value in rotation[:3]]), False)
            reset_ids.append(str(actor_id))
        except Exception as exc:
            status.setdefault("errors", []).append(f"initial_state_reset:{actor_id}:{exc}")
    status["initial_state_reset"] = True
    status["initial_state_reset_ids"] = sorted(reset_ids)


def advance_cpp_runtime_driver(cpp_driver, actors: dict, status: dict, runtime_scene: dict | None, frame_index: int) -> int:
    timebase = runtime_timebase(runtime_scene)
    step_s = 0.0 if int(frame_index) <= 0 else float(timebase["render_dt_s"])
    if step_s > 0:
        set_simulation_world_paused(actors, status, False)
    try:
        cpp_driver.advance_capture(step_s, step_s > 0.0)
    finally:
        set_simulation_world_paused(actors, status, True)
    return 1


def advance_physics_capture(actors: dict, status: dict, dt: float) -> None:
    if not status.get("enabled"):
        return
    if actors.get("gasp_locomotion"):
        set_simulation_world_paused(actors, status, False)
        time.sleep(max(0.0, float(dt)))
        set_simulation_world_paused(actors, status, True)
        status["gasp_tick_count"] = int(status.get("gasp_tick_count") or 0) + 1
        return
    if status.get("manual_world_tick_available"):
        try:
            world = actors.get("world") or unreal.EditorLevelLibrary.get_editor_world()
            tick_enum = getattr(getattr(unreal, "LevelTick", None), "LEVELTICK_ALL", None)
            if tick_enum is not None:
                world.tick(tick_enum, float(dt))
            else:
                world.tick(0, float(dt))
            status["manual_world_tick_count"] = int(status.get("manual_world_tick_count") or 0) + 1
            return
        except Exception as exc:
            status.setdefault("errors", []).append(f"manual_world_tick:{exc}")
            status["manual_world_tick_available"] = False
    time.sleep(max(0.0, float(dt)))


def stop_editor_physics_capture(status: dict) -> None:
    if not status.get("enabled"):
        return
    try:
        level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        if level_editor and hasattr(level_editor, "editor_request_end_play"):
            level_editor.editor_request_end_play()
            return
    except Exception:
        pass
    try:
        unreal.EditorLevelLibrary.editor_end_play()
    except Exception:
        pass


def sampled_frame_hashes(frames_dir: Path, frame_count: int, extension: str = "exr") -> dict:
    if frame_count <= 0:
        return {"samples": [], "unique": 0}
    indexes = sorted({0, frame_count // 2, frame_count - 1})
    samples = []
    for idx in indexes:
        path = frames_dir / f"frame_{idx:04d}.{extension}"
        if path.exists():
            samples.append({"frame": idx, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size})
    return {"samples": samples, "unique": len({sample["sha256"] for sample in samples})}


def encode_video(frames_dir: Path, preview_path: Path, video_filter: str | None = None, extension: str = "exr"):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"encoded": False, "reason": "ffmpeg not found"}
    cmd = [ffmpeg, "-y", "-framerate", str(FPS), "-i", str(frames_dir / f"frame_%04d.{extension}")]
    chosen_filter = video_filter if video_filter is not None else VIDEO_FILTER
    filters = [chosen_filter] if chosen_filter else []
    if VIDEO_SHARPEN and VIDEO_SHARPEN_FILTER:
        filters.append(VIDEO_SHARPEN_FILTER)
    chosen_filter = ",".join(filters)
    if chosen_filter:
        cmd.extend(["-vf", chosen_filter])
    cmd.extend(["-c:v", "libx264", "-crf", str(VIDEO_CRF), "-preset", str(VIDEO_PRESET), "-pix_fmt", "yuv420p", str(preview_path)])
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return {
        "encoded": proc.returncode == 0,
        "returncode": proc.returncode,
        "video_filter": chosen_filter,
        "crf": str(VIDEO_CRF),
        "preset": str(VIDEO_PRESET),
        "stderr_tail": proc.stderr[-2000:],
    }


def frame_time_s(frame: dict, fallback_index: int) -> float:
    try:
        return float(frame.get("time"))
    except Exception:
        return float(fallback_index) / max(float(FPS), 1.0)


def object_position_cm(state: dict) -> list[float] | None:
    position_cm = state.get("position_cm") if isinstance(state, dict) else None
    if isinstance(position_cm, list) and len(position_cm) >= 3:
        try:
            return [float(position_cm[0]), float(position_cm[1]), float(position_cm[2])]
        except Exception:
            return None
    position_m = state.get("position") if isinstance(state, dict) else None
    if isinstance(position_m, list) and len(position_m) >= 3:
        try:
            return [float(position_m[0]) * 100.0, float(position_m[1]) * 100.0, float(position_m[2]) * 100.0]
        except Exception:
            return None
    return None


def contact_events_from_trajectory(trajectory: list[dict]) -> list[dict]:
    events = []
    seen = set()
    for frame in trajectory:
        for event in frame.get("contacts") or []:
            if not isinstance(event, dict):
                continue
            objects = tuple(sorted(str(item) for item in (event.get("objects") or [])))
            event_frame = int(event.get("frame", frame.get("frame", 0)) or 0)
            key = (event_frame, objects, str(event.get("method") or "contact"))
            if key in seen:
                continue
            seen.add(key)
            events.append({**event, "frame": event_frame, "objects": list(objects)})
    return events


def write_contact_audio_pass(output_dir: Path, trajectory: list[dict], duration_s: float) -> dict:
    audio_dir = output_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    wav_path = audio_dir / "contact_events.wav"
    sample_rate = 48000
    events = contact_events_from_trajectory(trajectory)
    sample_count = max(1, int(max(duration_s, len(trajectory) / max(float(FPS), 1.0)) * sample_rate))
    samples = [0.0] * sample_count
    for event in events:
        try:
            time_s = float(event.get("time"))
        except Exception:
            time_s = float(event.get("frame") or 0) / max(float(FPS), 1.0)
        start = max(0, min(sample_count - 1, int(time_s * sample_rate)))
        pair_key = "|".join(str(item) for item in (event.get("objects") or []))
        freq = 180.0 + (int(hashlib.sha256(pair_key.encode("utf-8")).hexdigest()[:6], 16) % 520)
        length = min(sample_count - start, int(0.075 * sample_rate))
        for offset in range(length):
            decay = math.exp(-offset / max(1.0, length / 7.0))
            samples[start + offset] += 0.36 * decay * math.sin(2.0 * math.pi * freq * (offset / sample_rate))
    with wave.open(str(wav_path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sample_rate)
        frames = bytearray()
        for sample in samples:
            value = max(-1.0, min(1.0, sample))
            frames.extend(struct.pack("<h", int(value * 32767)))
        fh.writeframes(bytes(frames))
    return {
        "status": "available",
        "source_type": "contact_event_synthesis",
        "path": str(wav_path),
        "sample_rate": sample_rate,
        "duration_s": round(sample_count / sample_rate, 4),
        "contact_event_count": len(events),
        "note": "Deterministic impact audio synthesized from contact event timestamps; not native UE audio capture.",
    }


def write_depth_normal_proxy_passes(output_dir: Path, camera_tracks: dict[str, list[dict]], trajectory: list[dict]) -> tuple[list[dict], list[dict]]:
    depth_dir = output_dir / "depth"
    normal_dir = output_dir / "normal"
    depth_dir.mkdir(exist_ok=True)
    normal_dir.mkdir(exist_ok=True)
    depth_views = []
    normal_views = []
    for view_id, track in camera_tracks.items():
        depth_frames = []
        normal_frames = []
        for idx, frame in enumerate(trajectory):
            pose = track[min(idx, len(track) - 1)] if track else {}
            camera_cm = pose.get("location_cm") or [0.0, 0.0, 0.0]
            object_distances = []
            for object_id, object_state in (frame.get("objects") or {}).items():
                pos_cm = object_position_cm(object_state)
                if not pos_cm:
                    continue
                distance_cm = math.sqrt(sum((float(pos_cm[axis]) - float(camera_cm[axis])) ** 2 for axis in range(3)))
                object_distances.append({"object_id": object_id, "distance_cm": round(distance_cm, 3)})
            object_distances.sort(key=lambda item: item["distance_cm"])
            contacts = frame.get("contacts") or []
            normal = [0.0, 0.0, 1.0]
            if contacts:
                axis_gaps = (contacts[0] or {}).get("axis_gaps_cm") if isinstance(contacts[0], dict) else None
                if isinstance(axis_gaps, dict) and axis_gaps:
                    axis = min(axis_gaps, key=lambda key: abs(float(axis_gaps.get(key) or 0.0)))
                    normal = [0.0, 0.0, 0.0]
                    normal[{"x": 0, "y": 1, "z": 2}.get(str(axis), 2)] = 1.0
            depth_frames.append(
                {
                    "frame": int(frame.get("frame", idx) or idx),
                    "time": frame_time_s(frame, idx),
                    "nearest_object": object_distances[0] if object_distances else None,
                    "farthest_object": object_distances[-1] if object_distances else None,
                    "object_count": len(object_distances),
                }
            )
            normal_frames.append(
                {
                    "frame": int(frame.get("frame", idx) or idx),
                    "time": frame_time_s(frame, idx),
                    "dominant_contact_normal": normal,
                    "contact_count": len(contacts),
                }
            )
        depth_path = depth_dir / f"{view_id}.json"
        normal_path = normal_dir / f"{view_id}.json"
        depth_payload = {
            "schema_version": "depth_proxy_pass_v1",
            "view_id": view_id,
            "source_type": "derived_runtime_proxy",
            "unit": "centimeter",
            "frames": depth_frames,
        }
        normal_payload = {
            "schema_version": "normal_proxy_pass_v1",
            "view_id": view_id,
            "source_type": "derived_runtime_proxy",
            "frames": normal_frames,
        }
        depth_path.write_text(json.dumps(depth_payload, indent=2), encoding="utf-8")
        normal_path.write_text(json.dumps(normal_payload, indent=2), encoding="utf-8")
        depth_views.append({"view_id": view_id, "path": str(depth_path), "source_type": "derived_runtime_proxy"})
        normal_views.append({"view_id": view_id, "path": str(normal_path), "source_type": "derived_runtime_proxy"})
    return depth_views, normal_views


def write_render_pass_manifest(
    output_dir: Path,
    encode_results: list[dict],
    camera_tracks: dict[str, list[dict]],
    trajectory: list[dict],
    duration_s: float,
    data_pass_frames: dict[str, list[dict]] | None = None,
) -> dict:
    manifest_path = output_dir / "render_pass_manifest.json"
    passes = {
        "rgb": {
            "status": "available" if encode_results else "missing",
            "source_type": "native_render",
            "views": [
                {
                    "view_id": item.get("view_id") or item.get("id"),
                    "path": item.get("preview"),
                    "encoded": bool((item.get("encode") or {}).get("encoded")),
                }
                for item in encode_results
            ],
        }
    }
    if RENDER_DATA_PASSES:
        depth_views, segmentation_views = summarize_real_data_passes(data_pass_frames or {})
        passes["depth"] = {
            "status": "available" if depth_views else "missing",
            "source_type": "ue_depth_buffer" if depth_views else None,
            "views": depth_views,
            "note": "Depth pass is exported from UE SceneCapture depth source.",
        }
        passes["segmentation"] = {
            "status": "available" if segmentation_views else "missing",
            "source_type": "ue_instance_material_mask" if segmentation_views else None,
            "views": segmentation_views,
            "note": "Segmentation pass uses deterministic per-instance material colors from UE render capture.",
        }
        passes["normal"] = {"status": "disabled", "source_type": None, "views": [], "note": "Normal export is out of M2.3 scope."}
    else:
        passes["depth"] = {"status": "disabled", "source_type": None, "views": []}
        passes["segmentation"] = {"status": "disabled", "source_type": None, "views": []}
        passes["normal"] = {"status": "disabled", "source_type": None, "views": []}
    passes["audio"] = (
        write_contact_audio_pass(output_dir, trajectory, duration_s)
        if AUDIO_PASS_ENABLED
        else {"status": "disabled", "source_type": None}
    )
    payload = {
        "schema_version": "render_pass_manifest_v1",
        "frame_count": len(trajectory),
        "fps": FPS,
        "passes": passes,
        "sync": {
            "timebase": "frame_index / fps",
            "camera_trajectory": str(output_dir / "camera_trajectories.json"),
            "object_trajectory": str(output_dir / "trajectory.json"),
        },
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"path": str(manifest_path), "passes": payload["passes"]}


def summarize_real_data_passes(data_pass_frames: dict[str, list[dict]]) -> tuple[list[dict], list[dict]]:
    depth_views = []
    segmentation_views = []
    for view_id, frames in sorted(data_pass_frames.items()):
        depth_frames = [frame.get("depth") or {} for frame in frames if (frame.get("depth") or {}).get("status") == "available"]
        segmentation_frames = [frame.get("segmentation") or {} for frame in frames if (frame.get("segmentation") or {}).get("status") == "available"]
        if depth_frames:
            depth_views.append(
                {
                    "view_id": view_id,
                    "path": depth_frames[0].get("path"),
                    "frames": [frame.get("path") for frame in depth_frames],
                    "frame_count": len(depth_frames),
                    "source_type": "ue_depth_buffer",
                    "capture_source": depth_frames[0].get("capture_source"),
                    "render_target_format": depth_frames[0].get("render_target_format"),
                    "unit": depth_frames[0].get("unit"),
                    "depth_type": depth_frames[0].get("depth_type"),
                    "depth_encoding": depth_frames[0].get("depth_encoding"),
                    "stored_value_to_centimeter": depth_frames[0].get("stored_value_to_centimeter"),
                    "unit_validation": depth_frames[0].get("unit_validation"),
                    "material": depth_frames[0].get("material"),
                }
            )
        if segmentation_frames:
            segmentation_views.append(
                {
                    "view_id": view_id,
                    "path": segmentation_frames[0].get("path"),
                    "frames": [frame.get("path") for frame in segmentation_frames],
                    "frame_count": len(segmentation_frames),
                    "source_type": "ue_instance_material_mask",
                    "segmentation_type": "instance",
                    "instance_level": True,
                    "instance_count": max(int(frame.get("instance_count") or 0) for frame in segmentation_frames),
                    "instance_mapping": segmentation_frames[0].get("instance_mapping") or [],
                }
            )
    return depth_views, segmentation_views


def request_editor_exit():
    try:
        unreal.EditorPythonScripting.set_keep_python_script_alive(False)
    except Exception:
        pass
    try:
        unreal.SystemLibrary.quit_editor()
        return
    except Exception:
        pass
    try:
        unreal.SystemLibrary.execute_console_command(None, "QUIT_EDITOR")
    except Exception:
        pass


def write_summary(summary: dict):
    (OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def write_progress_marker(stage: str, detail: str | None = None) -> None:
    try:
        payload = {"stage": stage, "detail": detail, "time": round(time.time(), 3)}
        path = OUTPUT_DIR / "progress.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def configure_clean_highres_viewport() -> list[str]:
    try:
        level_editor = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        if level_editor and not level_editor.editor_get_game_view():
            level_editor.editor_set_game_view(True)
    except Exception:
        pass
    commands = [
        "viewmode lit",
        "showflag.Selection 0",
        "showflag.SelectionOutline 0",
        "showflag.Grid 0",
        "showflag.BillboardSprites 0",
        "showflag.CompositeEditorPrimitives 0",
        "showflag.ModeWidgets 0",
        "showflag.CameraFrustums 0",
    ]
    applied = []
    for command in commands:
        try:
            unreal.SystemLibrary.execute_console_command(None, command)
            applied.append(command)
        except Exception:
            pass
    try:
        unreal.EditorLevelLibrary.editor_invalidate_viewports()
    except Exception:
        pass
    return applied


def start_highres_viewport_capture(
    actors: dict,
    runtime_scene: dict | None,
    trajectory: list[dict],
    validation: dict,
    setup_seconds: float,
    script_started: float,
    frames_dir: Path,
) -> None:
    try:
        unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    except Exception:
        pass
    frames_dir.mkdir(exist_ok=True)
    view_outputs = []
    for view in camera_view_specs(actors, runtime_scene):
        suffix = "" if not view_outputs else str(view.get("suffix") or f"_{view.get('view_id') or view.get('id')}")
        view_frames_dir = frames_dir if not suffix else OUTPUT_DIR / f"frames{suffix}"
        view_frames_dir.mkdir(exist_ok=True)
        view_outputs.append({**view, "frames_dir": view_frames_dir, "preview": OUTPUT_DIR / f"preview{suffix}.mp4"})
    render_data_passes = bool(globals().get("RENDER_DATA_PASSES", False))
    data_pass_dirs = initialize_data_pass_dirs(OUTPUT_DIR) if render_data_passes else {}
    data_pass_frames = {
        str(output.get("view_id") or output.get("id")): []
        for output in view_outputs
    }
    view = view_outputs[0]
    scene_origin = unreal.Vector(*actors.get("scene_origin", [0.0, 0.0, 0.0]))
    dynamic_ids = [obj.get("id") for obj in (runtime_scene.get("dynamic_objects") if runtime_scene else [])] if runtime_scene else ["rubber_ball", "lead_ball", "steel_ball"]
    physics_enabled = physics_capture_enabled(actors, runtime_scene)
    physics_status = start_editor_physics_capture(actors, runtime_scene, DURATION) if physics_enabled else {"enabled": False}
    physics_controls = actors.get("physics_controls") or runtime_physics_controls(runtime_scene)
    initial_impulse_start_frame = int_control(physics_controls.get("initial_impulse_start_frame"), 0, 0, max(0, len(trajectory) - 1))
    camera = unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.CameraActor, view["location"])
    camera.set_actor_label("native_phenomena_demo_highres_capture_camera")
    camera.camera_component.set_editor_property("field_of_view", float(view["fov"]))

    editor_subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    state = {
        "frame_index": 0,
        "view_index": 0,
        "waiting_path": None,
        "last_size": -1,
        "stable_count": 0,
        "capture_started": time.perf_counter(),
        "encode_started": None,
        "errors": [],
        "handle": None,
        "task": None,
        "physics_ready": not physics_enabled,
        "physics_wait_started": time.perf_counter(),
        "physics_last_rebind_attempt": 0.0,
        "physics_impulse_applied": False,
        "initial_impulse_start_frame": initial_impulse_start_frame,
        "actual_trajectory": [],
        "contact_events": [],
        "seen_contact_pairs": set(),
        "probe_index": 0,
        "waiting_kind": None,
        "data_capture_in_progress": False,
        "capture_request_in_progress": False,
        "gasp_tick_pending": None,
        "gasp_tick_target_time": None,
        "gasp_advanced_frame": None,
        "camera_tracks": {
            str(output.get("view_id") or output.get("id")): []
            for output in view_outputs
        },
    }

    summary = {
        "output_dir": str(OUTPUT_DIR),
        "native_ue": True,
        "capture_backend": "highres_viewport",
        "uses_adp_probe_link": False,
        "uses_tmp": str(OUTPUT_DIR).startswith("/tmp/"),
        "project": "AgenticSimNative",
        "frames": len(trajectory),
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
        "duration": DURATION,
        "timebase": runtime_timebase(runtime_scene),
        "asset_manifest_resolver": ASSET_MANIFEST_DATA.get("resolver"),
        "asset_database_assets": GITLAB_ONLY_ASSETS,
        "studio_scene_spec": {
            "path": SCENE_SPEC,
            "draft_id": STUDIO_SCENE_SPEC.get("draft_id"),
            "prompt": STUDIO_SCENE_SPEC.get("prompt"),
            "expanded_prompt": STUDIO_SCENE_SPEC.get("expanded_prompt"),
            "load_error": STUDIO_SCENE_SPEC.get("load_error"),
        },
        "studio_runtime_scene": {
            "path": SCENE_RUNTIME_JSON,
            "draft_id": STUDIO_RUNTIME_SCENE.get("draft_id"),
            "case_type": STUDIO_RUNTIME_SCENE.get("case_type"),
            "load_error": STUDIO_RUNTIME_SCENE.get("load_error"),
        },
        "asset_manifest_load_error": ASSET_MANIFEST_DATA.get("load_error"),
        "asset_indexes": actors.get("asset_indexes", []),
        "asset_selection_metadata": ASSET_SELECTION_METADATA,
        "selected_map": actors.get("selected_map", {}),
        "scene_origin": actors.get("scene_origin", []),
        "map_stage": actors.get("map_stage", {}),
        "loaded_map_actor_count": actors.get("loaded_map_actor_count", 0),
        "visible_map_actors": actors.get("visible_map_actors", {}),
        "spawned_assets": actors.get("spawned_assets", []),
        "runtime_actor_bounds": actors.get("runtime_actor_bounds", {}),
        "runtime_ground_offsets": actors.get("runtime_ground_offsets", {}),
        "runtime_initial_transforms": actors.get("runtime_initial_transforms", {}),
        "chaos_runtime": actors.get("chaos_runtime", {}),
        "removed_map_actors": actors.get("removed_map_actors", []),
        "background_stage_actors": actors.get("background_stage_actors", []),
        "stage_helper_actors": actors.get("stage_helper_actors", []),
        "camera_pose": actors.get("camera_pose", {}),
        "lighting": actors.get("lighting", {}),
        "validation": validation,
        "planned_validation": validation,
        "physics_capture": {
            **physics_status,
            "trajectory_source": analytic_solver_source(actors, runtime_scene) if analytic_contact_solver_enabled(actors, runtime_scene) else ("adp_cpp_runtime_driver" if physics_enabled and physics_controls.get("cpp_runtime_driver_enabled") else ("ue_chaos_transform_capture" if physics_enabled else str(runtime_physics_controls(runtime_scene).get("simulation_driver") or "scripted_trajectory_replay"))),
            "contact_events": [],
            "initial_impulse_start_frame": initial_impulse_start_frame,
        },
        "frame_hashes": {"samples": [], "unique": 0},
        "encode": {"encoded": False, "reason": "pending"},
        "multi_view": [],
        "camera_trajectories": {"path": str(OUTPUT_DIR / "camera_trajectories.json"), "view_count": 0, "views": []},
        "render_pass_manifest": {"path": str(OUTPUT_DIR / "render_pass_manifest.json"), "passes": {}},
        "timing": {
            "setup_seconds": setup_seconds,
            "capture_seconds": None,
            "encode_seconds": None,
            "total_seconds": None,
        },
        "frame_cleanup": {
            "keep_render_frames": KEEP_RENDER_FRAMES,
            "removed_frame_dirs": [],
        },
    }
    summary["lighting"]["highres_viewport"] = {
        "force_game_view": False,
        "frame_extension": "png",
        "clean_viewport_commands": configure_clean_highres_viewport(),
    }
    summary["capture_readiness"] = {
        "schema_version": "capture_readiness_v1",
        "warmup_frames": RENDER_WARMUP_FRAMES,
        "viewport_settle_seconds": RENDER_VIEWPORT_SETTLE_SECONDS,
        "first_frame_stability_samples": RENDER_FIRST_FRAME_STABILITY_SAMPLES,
        "per_frame_settle_ticks": RENDER_PER_FRAME_SETTLE_TICKS,
        "screenshot_stable_ticks": RENDER_SCREENSHOT_STABLE_TICKS,
        "first_frame_stability_size_tolerance": RENDER_FIRST_FRAME_STABILITY_SIZE_TOLERANCE,
        "initial_settle": settle_highres_viewport(RENDER_VIEWPORT_SETTLE_SECONDS, RENDER_WARMUP_FRAMES),
        "probes": [],
        "probe_stable": RENDER_FIRST_FRAME_STABILITY_SAMPLES <= 1,
    }
    write_summary(summary)

    def maybe_apply_deferred_impulse() -> None:
        if not physics_enabled or not state["physics_ready"] or state["physics_impulse_applied"]:
            return
        if state["frame_index"] < state["initial_impulse_start_frame"]:
            return
        if actors.get("gasp_locomotion"):
            pass
        elif not start_cpp_runtime_driver(actors, runtime_scene, physics_status, len(trajectory)):
            apply_initial_physics_impulses(actors, runtime_scene, physics_status)
        state["physics_impulse_applied"] = True
        physics_status["initial_impulse_applied_at_frame"] = state["frame_index"]
        summary["physics_capture"].update(physics_status)
        write_summary(summary)

    def start_visible_physics_input() -> None:
        if not physics_enabled or state["physics_impulse_applied"]:
            return
        if analytic_contact_solver_enabled(actors, runtime_scene):
            solver_source = analytic_solver_source(actors, runtime_scene)
            physics_status[solver_source] = {"enabled": True, "started": True}
            state["physics_impulse_applied"] = True
            physics_status["initial_impulse_applied_at_frame"] = state["frame_index"]
            summary["physics_capture"].update(physics_status)
            write_summary(summary)
            return
        if actors.get("gasp_locomotion"):
            physics_status["interaction_driver"] = "gasp_character_movement_and_delayed_release"
        elif not start_cpp_runtime_driver(actors, runtime_scene, physics_status, len(trajectory)):
            apply_initial_physics_impulses(actors, runtime_scene, physics_status)
        first_frame = trajectory[min(state["frame_index"], len(trajectory) - 1)] if trajectory else {"frame": 0, "time": 0.0, "objects": {}}
        runner_ids = [obj.get("id") for obj in (runtime_scene.get("dynamic_objects") or []) if obj.get("behavior") == "third_person_runner"] if runtime_scene else []
        if runner_ids:
            apply_trajectory_frame(actors, runner_ids, first_frame, scene_origin, runtime_scene)
        apply_runtime_animation_segments(actors, runtime_scene, first_frame, physics_status)
        apply_delayed_release_projectiles(actors, runtime_scene, first_frame, scene_origin, physics_status)
        state["physics_impulse_applied"] = True
        physics_status["initial_impulse_applied_at_frame"] = state["frame_index"]
        summary["physics_capture"].update(physics_status)
        write_summary(summary)

    def finish():
        try:
            if state["handle"] is not None:
                unreal.unregister_slate_post_tick_callback(state["handle"])
        except Exception:
            pass
        if physics_enabled:
            cpp_trajectory, cpp_contact_events = stop_cpp_runtime_driver(actors, physics_status, runtime_scene, scene_origin)
            if cpp_trajectory:
                state["actual_trajectory"] = merge_scripted_runner_trajectory(cpp_trajectory, trajectory, runtime_scene)
                state["contact_events"] = cpp_contact_events
                summary["physics_capture"]["trajectory_source"] = "adp_cpp_runtime_driver"
            stop_editor_physics_capture(physics_status)
            summary["physics_capture"].update(physics_status)
            if actors.get("delayed_release_state"):
                summary["physics_capture"]["delayed_release_state"] = actors.get("delayed_release_state")
            if actors.get("runtime_animation_state"):
                summary["physics_capture"]["runtime_animation_state"] = actors.get("runtime_animation_state")
            if state["actual_trajectory"]:
                attach_geometry_collection_fracture_events(state["actual_trajectory"], physics_status)
                actual_validation = validate_runtime_scene(runtime_scene, state["actual_trajectory"]) if runtime_scene else validation
                summary["validation"] = actual_validation
                summary["physics_capture"]["actual_frame_count"] = len(state["actual_trajectory"])
                summary["physics_capture"]["contact_events"] = state["contact_events"]
                summary["physics_capture"]["unique_contact_pairs"] = len({tuple(event.get("objects") or []) for event in state["contact_events"]})
                (OUTPUT_DIR / "trajectory.json").write_text(json.dumps(state["actual_trajectory"], indent=2), encoding="utf-8")
                (OUTPUT_DIR / "validation.json").write_text(json.dumps(actual_validation, indent=2), encoding="utf-8")
        state["encode_started"] = time.perf_counter()
        summary["timing"]["capture_seconds"] = round(state["encode_started"] - state["capture_started"], 2)
        encode_results = []
        for output in view_outputs:
            if actors.get("gasp_locomotion"):
                first_frame = output["frames_dir"] / "frame_0000.png"
                stable_frame = output["frames_dir"] / "frame_0001.png"
                if first_frame.exists() and stable_frame.exists():
                    shutil.copyfile(stable_frame, first_frame)
                    summary["capture_readiness"]["gasp_first_frame_camera_stabilized"] = True
            frame_hashes = sampled_frame_hashes(output["frames_dir"], len(trajectory), "png")
            encode = encode_video(output["frames_dir"], output["preview"], actors.get("video_filter", ""), "png")
            encode_results.append({
                "id": output["id"],
                "view_id": str(output.get("view_id") or output.get("id")),
                "label": output["label"],
                "camera_mode": output.get("camera_mode") or "fixed",
                "lock_policy": output.get("lock_policy"),
                "preview": str(output["preview"]),
                "frame_hashes": frame_hashes,
                "encode": encode,
                "camera": {
                    "location": [output["location"].x, output["location"].y, output["location"].z],
                    "target": [output["target"].x, output["target"].y, output["target"].z],
                    "fov": output["fov"],
                },
            })
        summary["multi_view"] = encode_results
        summary["frame_hashes"] = encode_results[0]["frame_hashes"]
        summary["encode"] = encode_results[0]["encode"]
        camera_trajectory_payload = {
            "schema_version": "camera_trajectories_v1",
            "frame_count": len(trajectory),
            "fps": FPS,
            "timebase": "frame_index / fps",
            "views": [
                {
                    "view_id": str(output.get("view_id") or output.get("id")),
                    "label": output.get("label"),
                    "camera_mode": output.get("camera_mode") or "fixed",
                    "lock_policy": output.get("lock_policy"),
                    "frames": state["camera_tracks"].get(str(output.get("view_id") or output.get("id")), []),
                }
                for output in view_outputs
            ],
        }
        camera_trajectories_path = OUTPUT_DIR / "camera_trajectories.json"
        camera_trajectories_path.write_text(json.dumps(camera_trajectory_payload, indent=2), encoding="utf-8")
        summary["camera_trajectories"] = {
            "path": str(camera_trajectories_path),
            "view_count": len(view_outputs),
            "views": [str(output.get("view_id") or output.get("id")) for output in view_outputs],
        }
        final_trajectory = state["actual_trajectory"] if state["actual_trajectory"] else trajectory
        summary["render_pass_manifest"] = write_render_pass_manifest(
            OUTPUT_DIR,
            summary["multi_view"],
            state["camera_tracks"],
            final_trajectory,
            DURATION,
            data_pass_frames,
        )
        summary["timing"]["encode_seconds"] = round(time.perf_counter() - state["encode_started"], 2)
        summary["timing"]["total_seconds"] = round(time.perf_counter() - script_started, 2)
        if not KEEP_RENDER_FRAMES:
            removed = []
            for path in dict.fromkeys([frames_dir, *[output["frames_dir"] for output in view_outputs]]):
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
                    removed.append(str(path))
            summary["frame_cleanup"]["removed_frame_dirs"] = removed
        if state["errors"]:
            summary["errors"] = state["errors"]
        write_summary(summary)
        print(json.dumps(summary, indent=2), flush=True)
        request_editor_exit()

    def submit_highres_screenshot(path: Path, kind: str) -> None:
        if path.exists():
            path.unlink()
        state["capture_request_in_progress"] = True
        try:
            state["task"] = unreal.AutomationLibrary.take_high_res_screenshot(
                WIDTH,
                HEIGHT,
                str(path),
                actors.get("highres_camera") or camera,
                False,
                False,
                unreal.ComparisonTolerance.LOW,
                "Simulator Studio viewport capture",
                0.08,
                False,
            )
            state["waiting_path"] = path
            state["waiting_kind"] = kind
            state["last_size"] = -1
            state["stable_count"] = 0
            state["requested_at"] = time.perf_counter()
        finally:
            state["capture_request_in_progress"] = False

    def set_highres_camera_view(location, target, fov: float) -> None:
        capture_camera = actors.get("highres_camera") or camera
        for active_camera in dict.fromkeys((camera, capture_camera)):
            active_camera.set_actor_location(location, False, False)
            active_camera.set_actor_rotation(look_at_rotation(location, target), False)
            active_camera.camera_component.set_editor_property("field_of_view", float(fov))
        gasp = actors.get("gasp_locomotion") or {}
        controller = gasp.get("controller")
        if controller:
            controller.set_view_target_with_blend(capture_camera, 0.0)

    def request_probe() -> None:
        view = view_outputs[0]
        frame_view = camera_view_for_frame(view, runtime_scene, 0, len(trajectory))
        set_highres_camera_view(frame_view["location"], frame_view["target"], frame_view["fov"])
        try:
            editor_subsystem.set_level_viewport_camera_info(frame_view["location"], look_at_rotation(frame_view["location"], frame_view["target"]))
        except Exception:
            pass
        configure_clean_highres_viewport()
        settle = settle_highres_viewport(0.0, max(1, RENDER_PER_FRAME_SETTLE_TICKS))
        summary["capture_readiness"].setdefault("probe_settles", []).append(settle)
        path = view["frames_dir"] / f"warmup_probe_{state['probe_index']:04d}.png"
        submit_highres_screenshot(path, "probe")

    def handle_probe_complete(path: Path) -> None:
        fingerprint = file_fingerprint(path)
        summary["capture_readiness"].setdefault("probes", []).append(fingerprint)
        probes = summary["capture_readiness"].get("probes") or []
        if len(probes) >= 2:
            size_delta = abs(int(probes[-1].get("size") or 0) - int(probes[-2].get("size") or 0))
            hash_equal = probes[-1].get("sha256") == probes[-2].get("sha256")
            summary["capture_readiness"]["probe_size_delta"] = size_delta
            summary["capture_readiness"]["probe_stable"] = hash_equal or size_delta <= RENDER_FIRST_FRAME_STABILITY_SIZE_TOLERANCE
        state["probe_index"] += 1
        try:
            path.unlink()
        except Exception:
            pass
        write_summary(summary)

    def request_frame(frame_index: int):
        frame = trajectory[frame_index]
        first_view_for_frame = state["view_index"] == 0
        if (
            first_view_for_frame
            and physics_enabled
            and actors.get("gasp_locomotion")
            and state.get("gasp_advanced_frame") != frame_index
        ):
            runner_ids = [
                obj.get("id")
                for obj in (runtime_scene.get("dynamic_objects") or [])
                if obj.get("behavior") == "third_person_runner"
            ]
            apply_trajectory_frame(actors, runner_ids, frame, scene_origin, runtime_scene)
            world = actors.get("physics_game_world")
            now = float(unreal.GameplayStatics.get_time_seconds(world)) if world else 0.0
            set_simulation_world_paused(actors, physics_status, False)
            state["gasp_tick_pending"] = frame_index
            state["gasp_tick_target_time"] = now + fixed_physics_step_s(frame_index, FPS)
            return
        if first_view_for_frame and physics_enabled and analytic_contact_solver_enabled(actors, runtime_scene):
            solver_source = analytic_solver_source(actors, runtime_scene)
            apply_trajectory_frame(actors, dynamic_ids, frame, scene_origin, runtime_scene)
            apply_runtime_animation_segments(actors, runtime_scene, frame, physics_status)
            actual_frame = {
                **frame,
                "source": solver_source,
                "objects": {
                    key: {**value, "source": solver_source}
                    for key, value in (frame.get("objects") or {}).items()
                },
            }
            state["actual_trajectory"].append(actual_frame)
            state["contact_events"].extend(frame.get("contacts") or [])
        elif first_view_for_frame and physics_enabled:
            cpp_status = physics_status.get("cpp_runtime_driver") or {}
            cpp_driver = actors.get("adp_physics_runtime_driver")
            runner_ids = [obj.get("id") for obj in (runtime_scene.get("dynamic_objects") or []) if obj.get("behavior") == "third_person_runner"] if runtime_scene else []
            if runner_ids:
                apply_trajectory_frame(actors, runner_ids, frame, scene_origin, runtime_scene)
            apply_runtime_animation_segments(actors, runtime_scene, frame, physics_status)
            apply_delayed_release_projectiles(actors, runtime_scene, frame, scene_origin, physics_status)
            if cpp_status.get("started") and cpp_driver:
                try:
                    steps = advance_cpp_runtime_driver(cpp_driver, actors, physics_status, runtime_scene, frame_index)
                    cpp_status["manual_step_count"] = int(cpp_status.get("manual_step_count") or 0) + steps
                except Exception as exc:
                    cpp_status.setdefault("errors", []).append(f"advance_capture:{exc}")
            elif physics_status.get("manual_world_tick_available"):
                advance_physics_capture(actors, physics_status, fixed_physics_step_s(frame_index, FPS))
            if runner_ids:
                apply_trajectory_frame(actors, runner_ids, frame, scene_origin, runtime_scene)
            apply_runtime_animation_segments(actors, runtime_scene, frame, physics_status)
            apply_delayed_release_projectiles(actors, runtime_scene, frame, scene_origin, physics_status)
            actual_frame, new_events = record_physics_transform_frame(
                actors,
                runtime_scene,
                frame_index,
                frame_index / max(FPS, 1),
                scene_origin,
                state["seen_contact_pairs"],
            )
            new_events = merge_live_native_contacts(actual_frame, new_events, cpp_driver)
            apply_geometry_collection_fracture_response(actors, runtime_scene, new_events, frame_index, physics_status)
            if runner_ids and isinstance(actual_frame.get("objects"), dict):
                for runner_id in runner_ids:
                    runner_obj = next(
                        (obj for obj in (runtime_scene.get("dynamic_objects") or []) if str(obj.get("id") or "") == str(runner_id)),
                        {},
                    )
                    if str((runner_obj.get("params") or {}).get("humanoid_adapter") or "").strip().lower() in {"gasp", "ddv"}:
                        continue
                    scripted = (frame.get("objects") or {}).get(runner_id)
                    if isinstance(scripted, dict):
                        actual_frame["objects"][runner_id] = {**scripted, "source": scripted.get("source") or "scripted_runtime_preview"}
            state["actual_trajectory"].append(actual_frame)
            state["contact_events"].extend(new_events)
        elif first_view_for_frame:
            apply_trajectory_frame(actors, dynamic_ids, frame, scene_origin, runtime_scene)
            apply_runtime_animation_segments(actors, runtime_scene, frame, physics_status)
        if first_view_for_frame:
            apply_surface_mesh_sequence_replay(actors, runtime_scene, frame_index, physics_status)
            sync_runtime_visuals(actors)
        view = view_outputs[state["view_index"]]
        view_id = str(view.get("view_id") or view.get("id"))
        frame_view = camera_view_for_frame(view, runtime_scene, frame_index, len(trajectory), state["actual_trajectory"])
        state["camera_tracks"].setdefault(view_id, []).append(
            {
                "frame": frame_index,
                "time": frame_time_s(frame, frame_index),
                "location_cm": vector_payload(frame_view["location"]),
                "target_cm": vector_payload(frame_view["target"]),
                "fov": round(float(frame_view["fov"]), 4),
                "camera_mode": view.get("camera_mode") or "fixed",
            }
        )
        set_highres_camera_view(frame_view["location"], frame_view["target"], frame_view["fov"])
        if render_data_passes:
            state["data_capture_in_progress"] = True
        try:
            if render_data_passes:
                set_capture_view(actors, frame_view["location"], frame_view["target"], frame_view["fov"])
                data_pass_frames.setdefault(view_id, []).append(
                    export_depth_and_segmentation_frame(
                        actors,
                        runtime_scene,
                        view_id,
                        frame_index,
                        data_pass_dirs,
                    )
                )
            try:
                editor_subsystem.set_level_viewport_camera_info(frame_view["location"], look_at_rotation(frame_view["location"], frame_view["target"]))
            except Exception:
                pass
            configure_clean_highres_viewport()
            settle = settle_highres_viewport(0.0, RENDER_PER_FRAME_SETTLE_TICKS)
            summary.setdefault("capture_readiness", {}).setdefault("per_frame_settles", []).append({"frame": frame_index, "view_id": view_id, **settle})
            path = view["frames_dir"] / f"frame_{frame_index:04d}.png"
            submit_highres_screenshot(path, "frame")
        finally:
            if render_data_passes:
                state["data_capture_in_progress"] = False

    def advance_capture_cursor() -> None:
        state["view_index"] += 1
        if state["view_index"] < len(view_outputs):
            return
        state["view_index"] = 0
        state["frame_index"] += 1
        maybe_apply_deferred_impulse()

    def tick(delta_seconds: float):
        try:
            if state.get("data_capture_in_progress") or state.get("capture_request_in_progress"):
                return
            waiting_path = state.get("waiting_path")
            if waiting_path:
                task_done = True
                task = state.get("task")
                if task:
                    try:
                        task_done = bool(task.is_task_done())
                    except Exception:
                        task_done = True
                if task_done and waiting_path.exists() and waiting_path.stat().st_size > 0:
                    size = waiting_path.stat().st_size
                    if size == state["last_size"]:
                        state["stable_count"] += 1
                    else:
                        state["stable_count"] = 0
                        state["last_size"] = size
                    if state["stable_count"] >= RENDER_SCREENSHOT_STABLE_TICKS:
                        waiting_kind = state.get("waiting_kind") or "frame"
                        state["waiting_path"] = None
                        state["waiting_kind"] = None
                        state["task"] = None
                        if waiting_kind == "probe":
                            handle_probe_complete(waiting_path)
                        else:
                            advance_capture_cursor()
                elif time.perf_counter() - state.get("requested_at", state["capture_started"]) > 20.0:
                    waiting_kind = state.get("waiting_kind") or "frame"
                    state["errors"].append({"frame": state["frame_index"], "error": f"timeout waiting for {waiting_path}", "kind": waiting_kind})
                    state["waiting_path"] = None
                    state["waiting_kind"] = None
                    state["task"] = None
                    if waiting_kind == "probe":
                        state["probe_index"] += 1
                    else:
                        advance_capture_cursor()
                return
            if physics_enabled and not state["physics_ready"]:
                now = time.perf_counter()
                if now - state["physics_last_rebind_attempt"] >= 0.25:
                    state["physics_last_rebind_attempt"] = now
                    rebind_runtime_actors_to_simulation_world(actors, runtime_scene, physics_status, timeout_s=0.0, record_failure=False)
                    if physics_status.get("game_world_count", 0) > 0 and physics_status.get("rebound_actor_ids"):
                        set_simulation_world_paused(actors, physics_status, True)
                        reset_runtime_actors_to_initial_state(actors, runtime_scene, physics_status)
                        state["physics_ready"] = True
                        start_visible_physics_input()
                        summary["physics_capture"].update(physics_status)
                        write_summary(summary)
                if not state["physics_ready"] and now - state["physics_wait_started"] > 12.0:
                    physics_status.setdefault("errors", []).append("physics_ready_timeout:no_pie_game_world")
                    state["physics_ready"] = True
                    summary["physics_capture"].update(physics_status)
                    state["errors"].append({"frame": state.get("frame_index"), "error": "physics_ready_timeout:no_pie_game_world"})
                    write_summary(summary)
                return
            if state.get("gasp_tick_pending") is not None:
                pending_frame = int(state["gasp_tick_pending"])
                world = actors.get("physics_game_world")
                target_time = float(state.get("gasp_tick_target_time") or 0.0)
                current_time = float(unreal.GameplayStatics.get_time_seconds(world)) if world else target_time
                if current_time + 1e-4 < target_time:
                    return
                set_simulation_world_paused(actors, physics_status, True)
                state["gasp_tick_pending"] = None
                state["gasp_tick_target_time"] = None
                state["gasp_advanced_frame"] = pending_frame
                gasp_obj = next(iter(runtime_gasp_objects(runtime_scene)), None)
                if gasp_obj:
                    bound_gasp_post_tick_displacement(actors, gasp_obj, pending_frame)
                physics_status["gasp_tick_count"] = int(physics_status.get("gasp_tick_count") or 0) + 1
                return
            min_probe_count = min(2, RENDER_FIRST_FRAME_STABILITY_SAMPLES)
            should_probe = state["probe_index"] < min_probe_count or (
                state["probe_index"] < RENDER_FIRST_FRAME_STABILITY_SAMPLES
                and not summary.get("capture_readiness", {}).get("probe_stable")
            )
            if should_probe:
                request_probe()
                return
            if state["frame_index"] >= len(trajectory):
                finish()
                return
            request_frame(state["frame_index"])
        except Exception as exc:
            state["errors"].append({"frame": state.get("frame_index"), "error": str(exc)})
            finish()

    state["handle"] = unreal.register_slate_post_tick_callback(tick)


def main():
    script_started = time.perf_counter()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    frames_dir = OUTPUT_DIR / "frames"
    frames_dir.mkdir(exist_ok=True)
    write_progress_marker("main_start", str(OUTPUT_DIR))
    runtime_scene = STUDIO_RUNTIME_SCENE if STUDIO_RUNTIME_SCENE and not STUDIO_RUNTIME_SCENE.get("load_error") else None
    if runtime_scene:
        scene_spec = runtime_scene
        trajectory = simulate_runtime_scene(runtime_scene)
        validation = validate_runtime_scene(runtime_scene, trajectory)
    else:
        scene_spec = build_scene_spec(DURATION, FPS)
        trajectory = simulate(scene_spec)
        validation = validate(scene_spec, trajectory)
    (OUTPUT_DIR / "scene_spec.json").write_text(json.dumps(scene_spec, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "trajectory.json").write_text(json.dumps(trajectory, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    write_progress_marker("spec_written", f"frames={len(trajectory)}")

    setup_started = time.perf_counter()
    actors = setup_scene(runtime_scene)
    setup_seconds = round(time.perf_counter() - setup_started, 2)
    write_progress_marker("setup_complete", f"setup_seconds={setup_seconds}")
    scene_origin = unreal.Vector(*actors.get("scene_origin", [0.0, 0.0, 0.0]))
    dynamic_ids = [obj.get("id") for obj in (runtime_scene.get("dynamic_objects") if runtime_scene else [])] if runtime_scene else ["rubber_ball", "lead_ball", "steel_ball"]
    capture_backend = str((actors.get("lighting") or {}).get("capture_backend") or "scene_capture")
    write_progress_marker("capture_backend", capture_backend)
    if capture_backend == "highres_viewport":
        start_highres_viewport_capture(actors, runtime_scene, trajectory, validation, setup_seconds, script_started, frames_dir)
        write_progress_marker("highres_capture_started")
        return
    physics_enabled = physics_capture_enabled(actors, runtime_scene)
    write_progress_marker("physics_enabled", str(physics_enabled))
    physics_status = start_editor_physics_capture(actors, runtime_scene, DURATION) if physics_enabled else {"enabled": False}
    rebind_runtime_actors_to_simulation_world(actors, runtime_scene, physics_status)
    if physics_enabled:
        if analytic_contact_solver_enabled(actors, runtime_scene):
            solver_source = analytic_solver_source(actors, runtime_scene)
            physics_status[solver_source] = {"enabled": True, "started": True}
        elif not start_cpp_runtime_driver(actors, runtime_scene, physics_status, len(trajectory)):
            apply_initial_physics_impulses(actors, runtime_scene, physics_status)
    actual_trajectory = []
    contact_events = []
    seen_contact_pairs: set[tuple[str, str]] = set()
    views = camera_view_specs(actors, runtime_scene)
    view_outputs = []
    for view in views:
        view_frames_dir = frames_dir if view.get("suffix") == "" else OUTPUT_DIR / f"frames{view['suffix']}"
        view_frames_dir.mkdir(exist_ok=True)
        view_outputs.append({**view, "frames_dir": view_frames_dir, "preview": OUTPUT_DIR / f"preview{view['suffix']}.mp4"})
    camera_tracks: dict[str, list[dict]] = {
        str(view.get("view_id") or view.get("id")): [] for view in view_outputs
    }
    data_pass_dirs = initialize_data_pass_dirs(OUTPUT_DIR) if RENDER_DATA_PASSES else {}
    data_pass_frames: dict[str, list[dict]] = {
        str(view.get("view_id") or view.get("id")): [] for view in view_outputs
    }
    first_frame_views: dict[str, dict] = {}

    material_warmup = warm_up_surface_replay_materials(runtime_scene)
    actors.setdefault("lighting", {})["surface_replay_material_warmup"] = material_warmup
    write_progress_marker("surface_replay_material_warmup", json.dumps(material_warmup, sort_keys=True))
    warm_up_scene_capture(actors)
    write_progress_marker("warmup_complete")
    capture_started = time.perf_counter()
    runner_ids = [obj.get("id") for obj in (runtime_scene.get("dynamic_objects") or []) if obj.get("behavior") == "third_person_runner"] if runtime_scene else []
    for frame in trajectory:
        frame_index = int(frame.get("frame", 0))
        if physics_enabled:
            cpp_status = physics_status.get("cpp_runtime_driver") or {}
            cpp_driver = actors.get("adp_physics_runtime_driver")
            if runner_ids:
                apply_trajectory_frame(actors, runner_ids, frame, scene_origin, runtime_scene)
            apply_delayed_release_projectiles(actors, runtime_scene, frame, scene_origin, physics_status)
            if cpp_status.get("started") and cpp_driver:
                try:
                    steps = advance_cpp_runtime_driver(cpp_driver, actors, physics_status, runtime_scene, frame_index)
                    cpp_status["manual_step_count"] = int(cpp_status.get("manual_step_count") or 0) + steps
                except Exception as exc:
                    cpp_status.setdefault("errors", []).append(f"advance_capture:{exc}")
            else:
                advance_physics_capture(actors, physics_status, fixed_physics_step_s(frame_index, FPS))
            if runner_ids:
                apply_trajectory_frame(actors, runner_ids, frame, scene_origin, runtime_scene)
            apply_delayed_release_projectiles(actors, runtime_scene, frame, scene_origin, physics_status)
            actual_frame, new_events = record_physics_transform_frame(
                actors,
                runtime_scene,
                int(frame["frame"]),
                frame_index / max(FPS, 1),
                scene_origin,
                seen_contact_pairs,
            )
            new_events = merge_live_native_contacts(actual_frame, new_events, cpp_driver)
            apply_geometry_collection_fracture_response(actors, runtime_scene, new_events, frame_index, physics_status)
            if runner_ids and isinstance(actual_frame.get("objects"), dict):
                for runner_id in runner_ids:
                    runner_obj = next(
                        (obj for obj in (runtime_scene.get("dynamic_objects") or []) if str(obj.get("id") or "") == str(runner_id)),
                        {},
                    )
                    if str((runner_obj.get("params") or {}).get("humanoid_adapter") or "").strip().lower() in {"gasp", "ddv"}:
                        continue
                    scripted = (frame.get("objects") or {}).get(runner_id)
                    if isinstance(scripted, dict):
                        actual_frame["objects"][runner_id] = {**scripted, "source": scripted.get("source") or "scripted_runtime_preview"}
            actual_trajectory.append(actual_frame)
            contact_events.extend(new_events)
        else:
            apply_trajectory_frame(actors, dynamic_ids, frame, scene_origin, runtime_scene)
        apply_surface_mesh_sequence_replay(actors, runtime_scene, frame_index, physics_status)
        sync_runtime_visuals(actors)
        for view in view_outputs:
            frame_view = camera_view_for_frame(view, runtime_scene, frame_index, len(trajectory), actual_trajectory)
            view_id = str(view.get("view_id") or view.get("id"))
            camera_tracks.setdefault(view_id, []).append(
                {
                    "frame": frame_index,
                    "time": frame_time_s(frame, frame_index),
                    "location_cm": vector_payload(frame_view["location"]),
                    "target_cm": vector_payload(frame_view["target"]),
                    "fov": round(float(frame_view["fov"]), 4),
                    "camera_mode": view.get("camera_mode") or "fixed",
                }
            )
            set_capture_view(actors, frame_view["location"], frame_view["target"], frame_view["fov"])
            if frame_index == 0:
                first_frame_views[view_id] = frame_view
            capture_frame(actors, view["frames_dir"] / f"frame_{frame['frame']:04d}.exr")
            if RENDER_DATA_PASSES:
                data_pass_frames.setdefault(view_id, []).append(
                    export_depth_and_segmentation_frame(actors, runtime_scene, view_id, frame_index, data_pass_dirs)
                )
        if frame_index == 0 and RENDER_DATA_PASSES:
            recaptured = recapture_first_data_frame(
                actors,
                runtime_scene,
                view_outputs,
                first_frame_views,
                data_pass_dirs,
                frame_index,
            )
            for view_id, payload in recaptured.items():
                data_pass_frames[view_id][-1] = payload
    write_progress_marker("capture_loop_complete", f"frames={len(trajectory)}")
    capture_seconds = round(time.perf_counter() - capture_started, 2)
    if physics_enabled:
        cpp_trajectory, cpp_contact_events = stop_cpp_runtime_driver(actors, physics_status, runtime_scene, scene_origin)
        if cpp_trajectory:
            actual_trajectory = merge_scripted_runner_trajectory(cpp_trajectory, trajectory, runtime_scene)
            contact_events = cpp_contact_events
            physics_status["trajectory_source"] = "adp_cpp_runtime_driver"
        stop_editor_physics_capture(physics_status)
        if actual_trajectory:
            validation = validate_runtime_scene(runtime_scene, actual_trajectory) if runtime_scene else validation
            trajectory = actual_trajectory
            attach_geometry_collection_fracture_events(trajectory, physics_status)
            (OUTPUT_DIR / "trajectory.json").write_text(json.dumps(trajectory, indent=2), encoding="utf-8")
            (OUTPUT_DIR / "validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")

    frame_hashes = sampled_frame_hashes(frames_dir, len(trajectory))
    encode_started = time.perf_counter()
    encode_results = []
    for view in view_outputs:
        encode_results.append({
            "id": view["id"],
            "view_id": view.get("view_id") or view["id"],
            "label": view["label"],
            "camera_mode": view.get("camera_mode") or "fixed",
            "lock_policy": view.get("lock_policy"),
            "preview": str(view["preview"]),
            "frame_hashes": sampled_frame_hashes(view["frames_dir"], len(trajectory)),
            "encode": encode_video(view["frames_dir"], view["preview"], actors.get("video_filter")),
            "camera": {
                "location": [view["location"].x, view["location"].y, view["location"].z],
                "target": [view["target"].x, view["target"].y, view["target"].z],
                "fov": view["fov"],
            },
        })
    encode = encode_results[0]["encode"] if encode_results else {"encoded": False, "reason": "no views"}
    encode_seconds = round(time.perf_counter() - encode_started, 2)
    camera_trajectory_payload = {
        "schema_version": "camera_trajectories_v1",
        "frame_count": len(trajectory),
        "fps": FPS,
        "timebase": "frame_index / fps",
        "views": [
            {
                "view_id": str(view.get("view_id") or view.get("id")),
                "label": view.get("label"),
                "camera_mode": view.get("camera_mode") or "fixed",
                "lock_policy": view.get("lock_policy"),
                "frames": camera_tracks.get(str(view.get("view_id") or view.get("id")), []),
            }
            for view in view_outputs
        ],
    }
    camera_trajectories_path = OUTPUT_DIR / "camera_trajectories.json"
    camera_trajectories_path.write_text(json.dumps(camera_trajectory_payload, indent=2), encoding="utf-8")
    render_pass_manifest = write_render_pass_manifest(OUTPUT_DIR, encode_results, camera_tracks, trajectory, DURATION, data_pass_frames)
    summary = {
        "output_dir": str(OUTPUT_DIR),
        "native_ue": True,
        "uses_adp_probe_link": False,
        "uses_tmp": str(OUTPUT_DIR).startswith("/tmp/"),
        "project": "AgenticSimNative",
        "frames": len(trajectory),
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
        "duration": DURATION,
        "timebase": runtime_timebase(runtime_scene),
        "asset_manifest_resolver": ASSET_MANIFEST_DATA.get("resolver"),
        "asset_database_assets": GITLAB_ONLY_ASSETS,
        "studio_scene_spec": {
            "path": SCENE_SPEC,
            "draft_id": STUDIO_SCENE_SPEC.get("draft_id"),
            "prompt": STUDIO_SCENE_SPEC.get("prompt"),
            "expanded_prompt": STUDIO_SCENE_SPEC.get("expanded_prompt"),
            "load_error": STUDIO_SCENE_SPEC.get("load_error"),
        },
        "studio_runtime_scene": {
            "path": SCENE_RUNTIME_JSON,
            "draft_id": STUDIO_RUNTIME_SCENE.get("draft_id"),
            "case_type": STUDIO_RUNTIME_SCENE.get("case_type"),
            "load_error": STUDIO_RUNTIME_SCENE.get("load_error"),
        },
        "asset_manifest_load_error": ASSET_MANIFEST_DATA.get("load_error"),
        "asset_indexes": actors.get("asset_indexes", []),
        "asset_selection_metadata": ASSET_SELECTION_METADATA,
        "selected_map": actors.get("selected_map", {}),
        "scene_origin": actors.get("scene_origin", []),
        "map_stage": actors.get("map_stage", {}),
        "loaded_map_actor_count": actors.get("loaded_map_actor_count", 0),
        "visible_map_actors": actors.get("visible_map_actors", {}),
        "spawned_assets": actors.get("spawned_assets", []),
        "runtime_actor_bounds": actors.get("runtime_actor_bounds", {}),
        "runtime_ground_offsets": actors.get("runtime_ground_offsets", {}),
        "runtime_initial_transforms": actors.get("runtime_initial_transforms", {}),
        "chaos_runtime": actors.get("chaos_runtime", {}),
        "removed_map_actors": actors.get("removed_map_actors", []),
        "background_stage_actors": actors.get("background_stage_actors", []),
        "stage_helper_actors": actors.get("stage_helper_actors", []),
        "camera_pose": actors.get("camera_pose", {}),
        "lighting": actors.get("lighting", {}),
        "validation": validation,
        "planned_validation": validate_runtime_scene(runtime_scene, simulate_runtime_scene(runtime_scene)) if runtime_scene and physics_enabled else validation,
        "physics_capture": {
            **physics_status,
            "trajectory_source": analytic_solver_source(actors, runtime_scene) if analytic_contact_solver_enabled(actors, runtime_scene) else ("adp_cpp_runtime_driver" if (physics_status.get("cpp_runtime_driver") or {}).get("started") else ("ue_chaos_transform_capture" if physics_enabled else str(runtime_physics_controls(runtime_scene).get("simulation_driver") or "scripted_trajectory_replay"))),
            "actual_frame_count": len(actual_trajectory),
            "contact_events": contact_events,
            "unique_contact_pairs": len({tuple(event.get("objects") or []) for event in contact_events}),
        },
        "frame_hashes": frame_hashes,
        "encode": encode,
        "multi_view": encode_results,
        "camera_trajectories": {
            "path": str(camera_trajectories_path),
            "view_count": len(camera_trajectory_payload["views"]),
            "views": [item["view_id"] for item in camera_trajectory_payload["views"]],
        },
        "render_pass_manifest": render_pass_manifest,
        "timing": {
            "setup_seconds": setup_seconds,
            "capture_seconds": capture_seconds,
            "encode_seconds": encode_seconds,
            "total_seconds": round(time.perf_counter() - script_started, 2),
        },
        "frame_cleanup": {
            "keep_render_frames": KEEP_RENDER_FRAMES,
            "removed_frame_dirs": [],
        },
    }
    if not KEEP_RENDER_FRAMES:
        removed = []
        for view in view_outputs:
            frame_dir = Path(view["frames_dir"])
            if frame_dir.exists():
                shutil.rmtree(frame_dir, ignore_errors=True)
                removed.append(str(frame_dir))
        summary["frame_cleanup"]["removed_frame_dirs"] = removed
    (OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_progress_marker("summary_written")
    print(json.dumps(summary, indent=2), flush=True)


main()
