from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


HARNESS_CONFIG_SCHEMA_VERSION = "harness_config_v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "harness.json"
DEFAULT_WORKSPACE = Path.home() / "SimulatorWorkspace" / "physics_aware_harness"
IMAGE_CAPABILITIES = frozenset({"supported", "unsupported", "unknown"})
PUBLICATION_TIERS = frozenset({"reference"})

_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "planning_llm",
        "meshy",
        "codex_reviewer",
        "ue_asset_importer",
        "ue_runtime",
        "paths",
        "safety",
    }
)
_NESTED_FIELDS = {
    "planning_llm": frozenset({"base_url", "model", "image_capability", "api_key_env"}),
    "meshy": frozenset({"api_key_env"}),
    "codex_reviewer": frozenset({"executable"}),
    "ue_asset_importer": frozenset({"command"}),
    "ue_runtime": frozenset(
        {"map_package", "actor_class", "asset_registry", "contact_export", "runner_command"}
    ),
    "paths": frozenset({"workspace", "catalog", "ue_project", "ue_executable"}),
    "safety": frozenset({"default_publication_tier", "external_calls_default_allow"}),
}
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class HarnessConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EffectiveHarnessConfig:
    planning_base_url: str
    planning_model: str
    planning_image_capability: str
    planning_api_key_env: str
    meshy_api_key_env: str
    codex_executable: Path | None
    ue_asset_importer_command: tuple[str, ...]
    ue_map_package: str
    ue_actor_class: str
    ue_asset_registry: Path | None
    ue_contact_export: bool
    ue_runner_command: tuple[str, ...]
    workspace: Path
    catalog: Path
    ue_project: Path
    ue_executable: Path | None
    default_publication_tier: str
    external_calls_default_allow: bool
    sources: dict[str, dict[str, str]]
    config_path: Path

    @property
    def schema_version(self) -> str:
        return HARNESS_CONFIG_SCHEMA_VERSION

    def identity(self) -> dict[str, Any]:
        """Secret-free effective configuration identity used by caches and receipts."""
        return {
            "schema_version": self.schema_version,
            "planning_llm": {
                "endpoint_identity": endpoint_identity(self.planning_base_url),
                "model": self.planning_model,
                "image_capability": self.planning_image_capability,
                "api_key_env": self.planning_api_key_env,
            },
            "meshy": {"api_key_env": self.meshy_api_key_env},
            "codex_reviewer": {
                "executable": str(self.codex_executable) if self.codex_executable is not None else None,
            },
            "ue_asset_importer": {"command": list(self.ue_asset_importer_command)},
            "ue_runtime": {
                "map_package": self.ue_map_package,
                "actor_class": self.ue_actor_class,
                "asset_registry": str(self.ue_asset_registry) if self.ue_asset_registry is not None else None,
                "contact_export": self.ue_contact_export,
                "runner_command": list(self.ue_runner_command),
            },
            "paths": {
                "workspace": str(self.workspace),
                "catalog": str(self.catalog),
                "ue_project": str(self.ue_project),
                "ue_executable": str(self.ue_executable) if self.ue_executable is not None else None,
            },
            "safety": {
                "default_publication_tier": self.default_publication_tier,
                "external_calls_default_allow": self.external_calls_default_allow,
            },
        }

    @property
    def digest(self) -> str:
        return _stable_digest(self.identity())

    @property
    def planning_target_digest(self) -> str:
        return _stable_digest(
            {
                "endpoint_identity": endpoint_identity(self.planning_base_url),
                "model": self.planning_model,
                "api_key_env": self.planning_api_key_env,
            }
        )

    def planning_secret_env_name(self, env: Mapping[str, str] | None = None) -> str:
        environ = os.environ if env is None else env
        candidates = [self.planning_api_key_env]
        if self.planning_api_key_env == "SIM_HARNESS_LLM_API_KEY":
            candidates.append("OPENAI_API_KEY")
        return next((name for name in candidates if str(environ.get(name, "")).strip()), candidates[0])

    def planning_api_key(self, env: Mapping[str, str] | None = None) -> str | None:
        environ = os.environ if env is None else env
        value = str(environ.get(self.planning_secret_env_name(environ), "")).strip()
        return value or None

    def ue_map_package_for_case(self, case_spec: Mapping[str, Any] | None = None) -> str:
        scene = case_spec.get("scene") if isinstance(case_spec, Mapping) else None
        case_map = (
            str(scene.get("map_preference") or scene.get("map_package") or "").strip()
            if isinstance(scene, Mapping)
            else ""
        )
        source_layer = self.sources["ue_runtime.map_package"]["layer"]
        if source_layer in {"cli", "environment"} and self.ue_map_package:
            return self.ue_map_package
        return case_map or self.ue_map_package

    def ue_compile_identity(self, case_spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "schema_version": "harness_ue_compile_config_v1",
            "map_package": self.ue_map_package_for_case(case_spec),
            "ue_project": str(self.ue_project),
            "catalog": str(self.catalog),
        }

    def ue_execution_identity(self, case_spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "schema_version": "harness_ue_execution_config_v1",
            "map_package": self.ue_map_package_for_case(case_spec),
            "ue_project": str(self.ue_project),
            "ue_executable": str(self.ue_executable) if self.ue_executable is not None else None,
            "actor_class": self.ue_actor_class,
            "asset_registry": str(self.ue_asset_registry) if self.ue_asset_registry is not None else None,
            "contact_export": self.ue_contact_export,
            "runner_command": list(self.ue_runner_command),
        }

    def meshy_api_key(self, env: Mapping[str, str] | None = None) -> str | None:
        environ = os.environ if env is None else env
        value = str(environ.get(self.meshy_api_key_env, "")).strip()
        return value or None

    def inspect(self, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        environ = os.environ if env is None else env
        planning_secret_name = self.planning_secret_env_name(environ)
        codex = self.codex_executable or _which_path("codex", environ)
        importer_executable = _command_executable(self.ue_asset_importer_command, environ)
        importer_scripts = _command_scripts(self.ue_asset_importer_command)
        runner_executable = _command_executable(self.ue_runner_command, environ)
        runner_scripts = _command_scripts(self.ue_runner_command)
        map_file = _ue_map_file(self.ue_project, self.ue_map_package)
        paths = {
            "workspace": self.workspace,
            "catalog": self.catalog,
            "ue_project": self.ue_project,
            "ue_executable": self.ue_executable,
            "codex_executable": codex,
        }
        planning_key_present = bool(str(environ.get(planning_secret_name, "")).strip())
        planning_requires_key = urllib.parse.urlsplit(self.planning_base_url).hostname == "api.openai.com"
        return {
            "schema_version": "harness_effective_config_inspection_v1",
            "effective_config_digest": self.digest,
            "planning_target_digest": self.planning_target_digest,
            "sources": self.sources,
            "planning_llm": {
                "endpoint_identity": endpoint_identity(self.planning_base_url),
                "model": self.planning_model,
                "image_capability": self.planning_image_capability,
            },
            "paths": {
                name: {
                    "value": str(path) if path is not None else None,
                    "exists": bool(path is not None and path.exists()),
                }
                for name, path in paths.items()
            },
            "providers": {
                "planning_llm": {
                    "available": bool(self.planning_model and (planning_key_present or not planning_requires_key)),
                },
                "meshy": {"available": self.meshy_api_key(environ) is not None},
                "codex_reviewer": {"available": bool(codex and codex.is_file() and os.access(codex, os.X_OK))},
                "ue_asset_importer": {
                    "available": bool(
                        importer_executable
                        and importer_executable.is_file()
                        and os.access(importer_executable, os.X_OK)
                        and all(path.is_file() for path in importer_scripts)
                    ),
                    "command": list(self.ue_asset_importer_command),
                },
            },
            "ue_runtime": {
                "map_package": {
                    "value": self.ue_map_package,
                    "source": self.sources["ue_runtime.map_package"],
                    "resolved_umap": str(map_file) if map_file is not None else None,
                    "exists": bool(map_file is not None and map_file.is_file()),
                },
                "actor_class": self.ue_actor_class,
                "asset_registry": {
                    "value": str(self.ue_asset_registry) if self.ue_asset_registry is not None else None,
                    "exists": bool(self.ue_asset_registry is not None and self.ue_asset_registry.is_file()),
                },
                "contact_export": self.ue_contact_export,
                "runner_command": {
                    "value": list(self.ue_runner_command),
                    "executable_exists": bool(
                        runner_executable
                        and runner_executable.is_file()
                        and os.access(runner_executable, os.X_OK)
                    ),
                    "scripts_exist": all(path.is_file() for path in runner_scripts),
                    "available": bool(
                        runner_executable
                        and runner_executable.is_file()
                        and os.access(runner_executable, os.X_OK)
                        and all(path.is_file() for path in runner_scripts)
                    ),
                },
            },
            "secrets": {
                "planning_llm_api_key": {"environment_variable": planning_secret_name, "present": planning_key_present},
                "meshy_api_key": {
                    "environment_variable": self.meshy_api_key_env,
                    "present": self.meshy_api_key(environ) is not None,
                },
            },
            "safety": {
                "default_publication_tier": self.default_publication_tier,
                "external_calls_default_allow": self.external_calls_default_allow,
            },
        }


def load_harness_config(
    *,
    config_path: str | Path | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    repo_root: str | Path = REPO_ROOT,
) -> EffectiveHarnessConfig:
    environ = os.environ if env is None else env
    root = Path(repo_root).expanduser().resolve(strict=False)
    path = Path(config_path).expanduser() if config_path is not None else root / "config" / "harness.json"
    if not path.is_absolute():
        path = root / path
    path = path.resolve(strict=False)
    document: dict[str, Any] = {}
    if path.is_file():
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HarnessConfigError(f"cannot read a valid Harness config document: {path}") from exc
        if not isinstance(decoded, dict):
            raise HarnessConfigError("Harness config root must be an object")
        document = decoded
        _validate_document(document)
    elif config_path is not None:
        raise HarnessConfigError(f"Harness config file does not exist: {path}")

    flat_config = _flatten(document)
    overrides = dict(cli_overrides or {})
    unknown_overrides = set(overrides) - set(_defaults())
    if unknown_overrides:
        raise HarnessConfigError(f"unknown CLI config fields: {sorted(unknown_overrides)}")

    values: dict[str, Any] = {}
    sources: dict[str, dict[str, str]] = {}
    defaults = _defaults()
    env_keys = _environment_keys()
    for field, default in defaults.items():
        if field in overrides and overrides[field] is not None:
            value = overrides[field]
            source = {"layer": "cli", "key": field}
        else:
            selected_env = next((name for name in env_keys.get(field, ()) if str(environ.get(name, "")).strip()), None)
            if selected_env is not None:
                value = environ[selected_env]
                source = {"layer": "environment", "key": selected_env}
            elif field in flat_config and flat_config[field] is not None:
                value = flat_config[field]
                source = {"layer": "config", "key": field}
            else:
                value = default
                source = {"layer": "default", "key": field}
        values[field] = value
        sources[field] = source

    workspace = _path(values["paths.workspace"], root, "paths.workspace", required=True)
    if values["paths.catalog"] is None:
        catalog = workspace / "catalog" / "assets" / "catalog.sqlite"
        sources["paths.catalog"] = {"layer": "derived_default", "key": "paths.workspace"}
    else:
        catalog = _path(values["paths.catalog"], root, "paths.catalog", required=True)
    if values["paths.ue_project"] is None:
        ue_project = workspace / "ue" / "SimulatorWorkspace.uproject"
        sources["paths.ue_project"] = {"layer": "derived_default", "key": "paths.workspace"}
    else:
        ue_project = _path(values["paths.ue_project"], root, "paths.ue_project", required=True)

    base_url = _validated_base_url(values["planning_llm.base_url"])
    model = _string(values["planning_llm.model"], "planning_llm.model", allow_empty=True)
    capability = _string(values["planning_llm.image_capability"], "planning_llm.image_capability")
    if capability not in IMAGE_CAPABILITIES:
        raise HarnessConfigError(f"planning_llm.image_capability must be one of {sorted(IMAGE_CAPABILITIES)}")
    planning_key_env = _env_name(values["planning_llm.api_key_env"], "planning_llm.api_key_env")
    meshy_key_env = _env_name(values["meshy.api_key_env"], "meshy.api_key_env")
    tier = _string(values["safety.default_publication_tier"], "safety.default_publication_tier")
    external_default = values["safety.external_calls_default_allow"]
    if tier not in PUBLICATION_TIERS or external_default is not False:
        raise HarnessConfigError("safety defaults are fixed to reference tier and external-calls denied")

    codex_executable = _path(values["codex_reviewer.executable"], root, "codex_reviewer.executable")
    if codex_executable is None:
        codex_executable = _which_path("codex", environ)
        if codex_executable is not None:
            sources["codex_reviewer.executable"] = {"layer": "derived_default", "key": "PATH"}
    importer_command = _command(values["ue_asset_importer.command"], root, "ue_asset_importer.command")
    ue_map_package = _ue_package(values["ue_runtime.map_package"], "ue_runtime.map_package", allow_empty=True)
    ue_actor_class = _string(values["ue_runtime.actor_class"], "ue_runtime.actor_class", allow_empty=True)
    ue_asset_registry = _path(values["ue_runtime.asset_registry"], root, "ue_runtime.asset_registry")
    ue_contact_export = _boolean(
        values["ue_runtime.contact_export"],
        "ue_runtime.contact_export",
        allow_compat_string=sources["ue_runtime.contact_export"]["layer"] == "environment",
    )
    ue_runner_command = _command(values["ue_runtime.runner_command"], root, "ue_runtime.runner_command")

    return EffectiveHarnessConfig(
        planning_base_url=base_url,
        planning_model=model,
        planning_image_capability=capability,
        planning_api_key_env=planning_key_env,
        meshy_api_key_env=meshy_key_env,
        codex_executable=codex_executable,
        ue_asset_importer_command=importer_command,
        ue_map_package=ue_map_package,
        ue_actor_class=ue_actor_class,
        ue_asset_registry=ue_asset_registry,
        ue_contact_export=ue_contact_export,
        ue_runner_command=ue_runner_command,
        workspace=workspace,
        catalog=catalog,
        ue_project=ue_project,
        ue_executable=_path(values["paths.ue_executable"], root, "paths.ue_executable"),
        default_publication_tier=tier,
        external_calls_default_allow=external_default,
        sources=sources,
        config_path=path,
    )


def endpoint_identity(value: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(value)
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "path": parsed.path.rstrip("/") or "/",
    }


def _validate_document(data: Mapping[str, Any]) -> None:
    unknown = set(data) - _ROOT_FIELDS
    if unknown:
        raise HarnessConfigError(f"unknown Harness config fields: {sorted(unknown)}")
    if data.get("schema_version") != HARNESS_CONFIG_SCHEMA_VERSION:
        raise HarnessConfigError(f"schema_version must be {HARNESS_CONFIG_SCHEMA_VERSION}")
    for section, fields in _NESTED_FIELDS.items():
        raw = data.get(section, {})
        if not isinstance(raw, Mapping):
            raise HarnessConfigError(f"{section} must be an object")
        nested_unknown = set(raw) - fields
        if nested_unknown:
            raise HarnessConfigError(f"unknown {section} fields: {sorted(nested_unknown)}")


def _flatten(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        f"{section}.{field}": value
        for section, fields in _NESTED_FIELDS.items()
        for field, value in (data.get(section, {}) or {}).items()
    }


def _defaults() -> dict[str, Any]:
    return {
        "planning_llm.base_url": "https://api.openai.com/v1",
        "planning_llm.model": "",
        "planning_llm.image_capability": "unknown",
        "planning_llm.api_key_env": "SIM_HARNESS_LLM_API_KEY",
        "meshy.api_key_env": "SIM_HARNESS_MESHY_API_KEY",
        "codex_reviewer.executable": None,
        "ue_asset_importer.command": None,
        "ue_runtime.map_package": "",
        "ue_runtime.actor_class": "",
        "ue_runtime.asset_registry": None,
        "ue_runtime.contact_export": False,
        "ue_runtime.runner_command": None,
        "paths.workspace": str(DEFAULT_WORKSPACE),
        "paths.catalog": None,
        "paths.ue_project": None,
        "paths.ue_executable": None,
        "safety.default_publication_tier": "reference",
        "safety.external_calls_default_allow": False,
    }


def _environment_keys() -> dict[str, tuple[str, ...]]:
    return {
        "planning_llm.base_url": ("SIM_HARNESS_LLM_BASE_URL", "OPENAI_BASE_URL"),
        "planning_llm.model": ("SIM_HARNESS_LLM_MODEL", "OPENAI_MODEL"),
        "planning_llm.image_capability": ("SIM_HARNESS_LLM_IMAGE_CAPABILITY",),
        "planning_llm.api_key_env": ("SIM_HARNESS_LLM_API_KEY_ENV",),
        "meshy.api_key_env": ("SIM_HARNESS_MESHY_API_KEY_ENV",),
        "codex_reviewer.executable": ("SIM_HARNESS_CODEX_EXECUTABLE",),
        "ue_asset_importer.command": ("SIM_HARNESS_UE_ASSET_IMPORTER_CMD",),
        "ue_runtime.map_package": ("SIM_STUDIO_UE_MAP",),
        "ue_runtime.actor_class": ("SIM_STUDIO_UE_ACTOR_CLASS",),
        "ue_runtime.asset_registry": ("SIM_STUDIO_ASSET_REGISTRY",),
        "ue_runtime.contact_export": ("SIM_STUDIO_UE_CONTACT_EXPORT",),
        "ue_runtime.runner_command": ("SIM_STUDIO_UE_RUNNER_CMD",),
        "paths.workspace": ("SIM_HARNESS_WORKSPACE",),
        "paths.catalog": ("SIM_HARNESS_ASSET_CATALOG",),
        "paths.ue_project": ("SIM_STUDIO_UE_PROJECT",),
        "paths.ue_executable": ("SIM_STUDIO_UE_EXECUTABLE",),
    }


def _validated_base_url(value: Any) -> str:
    raw = _string(value, "planning_llm.base_url").rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise HarnessConfigError("planning_llm.base_url is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HarnessConfigError("planning_llm.base_url must be an HTTP(S) URL with a host")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise HarnessConfigError("planning_llm.base_url must not contain userinfo, query, or fragment")
    return raw


def _path(value: Any, root: Path, field: str, *, required: bool = False) -> Path | None:
    if value is None:
        if required:
            raise HarnessConfigError(f"{field} is required")
        return None
    raw = _string(value, field)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve(strict=False)
    if path == Path(path.anchor):
        raise HarnessConfigError(f"{field} cannot be a filesystem root")
    return path


def _env_name(value: Any, field: str) -> str:
    raw = _string(value, field)
    if not _ENV_NAME.fullmatch(raw):
        raise HarnessConfigError(f"{field} must be an environment variable name")
    return raw


def _boolean(value: Any, field: str, *, allow_compat_string: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if allow_compat_string and isinstance(value, str) and value.strip() in {"0", "1"}:
        return value.strip() == "1"
    raise HarnessConfigError(f"{field} must be a boolean")


def _ue_package(value: Any, field: str, *, allow_empty: bool = False) -> str:
    raw = _string(value, field, allow_empty=allow_empty)
    if not raw:
        return raw
    if not re.fullmatch(r"/Game/[A-Za-z0-9_./-]+", raw) or ".." in raw:
        raise HarnessConfigError(f"{field} must be a /Game/... package or object path")
    return raw


def _command(value: Any, root: Path, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            values = shlex.split(value)
        except ValueError as exc:
            raise HarnessConfigError(f"{field} must be a valid command string") from exc
    elif isinstance(value, (list, tuple)) and not isinstance(value, (bytes, bytearray)):
        values = list(value)
    else:
        raise HarnessConfigError(f"{field} must be a command string or argv array")
    if not values or any(not isinstance(item, str) or not item.strip() for item in values):
        raise HarnessConfigError(f"{field} must contain one or more non-empty strings")
    command = [item.strip() for item in values]
    executable = Path(command[0]).expanduser()
    if "/" in command[0] and not executable.is_absolute():
        command[0] = str((root / executable).resolve(strict=False))
    elif executable.is_absolute():
        command[0] = str(executable.resolve(strict=False))
    for index, item in enumerate(command[1:], start=1):
        script = Path(item).expanduser()
        if script.suffix.casefold() != ".py":
            continue
        if not script.is_absolute():
            script = root / script
        command[index] = str(script.resolve(strict=False))
    return tuple(command)


def _command_executable(command: tuple[str, ...], env: Mapping[str, str]) -> Path | None:
    if not command:
        return None
    executable = Path(command[0]).expanduser()
    if executable.is_absolute() or "/" in command[0]:
        return executable.resolve(strict=False)
    return _which_path(command[0], env)


def _command_scripts(command: tuple[str, ...]) -> tuple[Path, ...]:
    return tuple(Path(item) for item in command[1:] if Path(item).suffix.casefold() == ".py")


def _ue_map_file(project: Path, package: str) -> Path | None:
    if not package.startswith("/Game/"):
        return None
    relative = package[len("/Game/") :].split(".", 1)[0]
    parts = relative.split("/")
    if not relative or any(part in {"", ".", ".."} for part in parts):
        return None
    return project.parent / "Content" / Path(*parts).with_suffix(".umap")


def _string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise HarnessConfigError(f"{field} must be a string")
    raw = value.strip()
    if not raw and not allow_empty:
        raise HarnessConfigError(f"{field} must be non-empty")
    return raw


def _which_path(name: str, env: Mapping[str, str]) -> Path | None:
    found = shutil.which(name, path=env.get("PATH"))
    return Path(found).resolve(strict=False) if found else None


def _stable_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
