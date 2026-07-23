from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from harness.core.artifact_manager import DeliveryError, exr_sequence_provenance
from harness.core.artifact_schema import write_json
from harness.verification.run_quality import evaluate_run


DEFAULT_WORKSPACE = Path.home() / "SimulatorWorkspace" / "physics_aware_harness"
WORKSPACE_ENV = "SIM_HARNESS_WORKSPACE"
WORKSPACE_DIRS = (
    "cases",
    "runs",
    "catalog",
    "review",
    "review/inbox",
    "review/kept",
    "review/rejected",
    "cache",
    "envs",
    "tmp",
)
VIDEO_SUFFIXES = frozenset({".avi", ".mkv", ".mov", ".mp4", ".webm"})
REPO_ROOT = Path(__file__).resolve().parents[2]
REVIEW_TRANSACTION_SCHEMA_VERSION = "harness_review_decision_transaction_v1"


class WorkspaceError(ValueError):
    pass


def workspace_root(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    environ = os.environ if env is None else env
    raw = path if path is not None else environ.get(WORKSPACE_ENV, DEFAULT_WORKSPACE)
    if not str(raw).strip():
        raise WorkspaceError(f"{WORKSPACE_ENV} must not be empty")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise WorkspaceError("workspace path must be absolute")
    root = root.resolve(strict=False)
    if root == Path(root.anchor):
        raise WorkspaceError("filesystem root cannot be used as the workspace")
    git_ancestor = _git_ancestor(root)
    if git_ancestor is not None:
        raise WorkspaceError(f"workspace must be outside a Git working tree: {git_ancestor}")
    return root


def init_workspace(path: str | Path | None = None) -> Path:
    root = workspace_root(path)
    for directory in (root, *(root / relative for relative in WORKSPACE_DIRS)):
        if _lexists(directory) and (directory.is_symlink() or not directory.is_dir()):
            raise WorkspaceError(f"workspace directory is not a real directory: {directory}")
    for relative in WORKSPACE_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)
    return root


def bootstrap_workspace(
    path: str | Path | None = None,
    *,
    adp_content: str | Path | None = None,
    repo_root: str | Path = REPO_ROOT,
) -> Path:
    """Initialize the external workspace and optionally mount operator-supplied UE assets."""
    root = init_workspace(path)
    if adp_content is not None:
        configure_ue_mount(adp_content, root, repo_root=repo_root)
    return root


def setup_doctor(
    path: str | Path | None = None,
    *,
    ue_executable: str | Path | None = None,
    asset_content: str | Path | None = None,
    native_smoke_run: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Report contract-test and real-UE readiness without installing or mutating dependencies."""
    environ = os.environ if env is None else env
    root = workspace_root(path, env=environ)
    commands = {
        name: shutil.which(name)
        for name in ("git", "ffmpeg", "ffprobe")
    }
    initialized = all(_is_real_dir(root / relative) for relative in WORKSPACE_DIRS)
    project = root / "ue" / "SimulatorWorkspace.uproject"
    ue_raw = ue_executable or environ.get("SIM_STUDIO_UE_EXECUTABLE")
    ue_path = Path(ue_raw).expanduser().resolve(strict=False) if ue_raw else None
    asset_raw = asset_content or environ.get("SIM_HARNESS_ADP_CONTENT")
    asset_path = Path(asset_raw).expanduser().resolve(strict=False) if asset_raw else None
    smoke_raw = native_smoke_run or environ.get("SIM_HARNESS_NATIVE_SMOKE_RUN")
    smoke_path = Path(smoke_raw).expanduser().resolve(strict=False) if smoke_raw else None
    ue_version = _unreal_version(ue_path)
    checks = {
        "python_3_13": sys.version_info >= (3, 13),
        "git": bool(commands["git"]),
        "ffmpeg": bool(commands["ffmpeg"]),
        "ffprobe": bool(commands["ffprobe"]),
        "workspace_initialized": initialized,
        "workspace_ue_project": project.is_file() and not project.is_symlink(),
        "ue_executable": bool(
            ue_path
            and ue_path.is_file()
            and os.access(ue_path, os.X_OK)
        ),
        "ue_version_5_7": bool(
            ue_version
            and ue_version.get("MajorVersion") == 5
            and ue_version.get("MinorVersion") == 7
        ),
        "adp_physics_runtime_source": _project_plugin_source_ready(project),
        "adp_physics_runtime_binary": _project_plugin_binary_ready(project, ue_path),
        "asset_content": bool(
            asset_path
            and asset_path.is_dir()
            and not asset_path.is_symlink()
            and _contains_ue_package(asset_path)
        ),
        "asset_mount": _asset_mount_ready(root, asset_path),
        "native_smoke_accepted": _native_smoke_accepted(root, smoke_path),
    }
    contract_keys = ("python_3_13", "git", "ffmpeg", "ffprobe", "workspace_initialized")
    ue_config_keys = (
        *contract_keys,
        "workspace_ue_project",
        "ue_executable",
        "ue_version_5_7",
        "adp_physics_runtime_source",
        "adp_physics_runtime_binary",
        "asset_content",
        "asset_mount",
    )
    ue_keys = (*ue_config_keys, "native_smoke_accepted")
    labels = {
        "python_3_13": "Python 3.13",
        "git": "git",
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "workspace_initialized": "initialized external workspace",
        "workspace_ue_project": "workspace UE project",
        "ue_executable": "executable UnrealEditor-Cmd",
        "ue_version_5_7": "Unreal Engine 5.7 Build.version",
        "adp_physics_runtime_source": "enabled ADPPhysicsRuntime plugin source",
        "adp_physics_runtime_binary": "ADPPhysicsRuntime editor binary for the UE host platform",
        "asset_content": "operator-supplied UE asset Content with .uasset/.umap packages",
        "asset_mount": "workspace UE Content mount",
        "native_smoke_accepted": "hard-gate-passing native UE smoke run",
    }
    return {
        "schema_version": "harness_setup_doctor_v1",
        "workspace": str(root),
        "contract_ready": all(checks[key] for key in contract_keys),
        "ue_config_ready": all(checks[key] for key in ue_config_keys),
        "ue_ready": all(checks[key] for key in ue_keys),
        "checks": checks,
        "paths": {
            "git": commands["git"],
            "ffmpeg": commands["ffmpeg"],
            "ffprobe": commands["ffprobe"],
            "ue_executable": str(ue_path) if ue_path else None,
            "asset_content": str(asset_path) if asset_path else None,
            "workspace_ue_project": str(project),
            "native_smoke_run": str(smoke_path) if smoke_path else None,
        },
        "missing_for_contract": [labels[key] for key in contract_keys if not checks[key]],
        "missing_for_ue": [labels[key] for key in ue_keys if not checks[key]],
    }


def _unreal_version(executable: Path | None) -> dict[str, Any] | None:
    if executable is None:
        return None
    version_path = executable.parents[2] / "Build" / "Build.version" if len(executable.parents) > 2 else None
    if version_path is None or not version_path.is_file() or version_path.is_symlink():
        return None
    try:
        payload = json.loads(version_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _project_plugin_source_ready(project: Path) -> bool:
    if not project.is_file() or project.is_symlink():
        return False
    try:
        payload = json.loads(project.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    plugins = payload.get("Plugins") if isinstance(payload, dict) else None
    enabled = any(
        isinstance(row, dict)
        and row.get("Name") == "ADPPhysicsRuntime"
        and row.get("Enabled") is True
        for row in (plugins or [])
    )
    plugin = project.parent / "Plugins" / "ADPPhysicsRuntime"
    return bool(
        enabled
        and (plugin / "ADPPhysicsRuntime.uplugin").is_file()
        and (plugin / "Source" / "ADPPhysicsRuntime" / "ADPPhysicsRuntime.Build.cs").is_file()
    )


def _project_plugin_binary_ready(project: Path, ue_executable: Path | None) -> bool:
    platform = _ue_host_platform(ue_executable)
    if platform is None or not _project_plugin_source_ready(project):
        return False
    return _plugin_binary_ready(project.parent / "Plugins" / "ADPPhysicsRuntime", platform)


def _ue_host_platform(ue_executable: Path | None) -> str | None:
    if ue_executable is None:
        return None
    platform = ue_executable.parent.name
    return platform if platform in {"Linux", "Mac", "Win64"} else None


def _plugin_binary_ready(plugin: Path, platform: str) -> bool:
    binaries = plugin / "Binaries" / platform
    expected = {
        "Linux": "libUnrealEditor-ADPPhysicsRuntime.so",
        "Mac": "UnrealEditor-ADPPhysicsRuntime.dylib",
        "Win64": "UnrealEditor-ADPPhysicsRuntime.dll",
    }
    return bool(
        (binaries / "UnrealEditor.modules").is_file()
        and (binaries / expected[platform]).is_file()
    )


def build_ue_plugin(
    path: str | Path | None,
    *,
    ue_executable: str | Path,
    repo_root: str | Path = REPO_ROOT,
    max_parallel_actions: int = 4,
) -> dict[str, object]:
    """Build and activate the runtime plugin without modifying the Git checkout."""
    root = workspace_root(path)
    _require_initialized(root)
    if max_parallel_actions < 1:
        raise WorkspaceError("max_parallel_actions must be at least 1")
    ue_path = Path(ue_executable).expanduser().resolve(strict=False)
    if not ue_path.is_file() or not os.access(ue_path, os.X_OK):
        raise WorkspaceError(f"UE executable is missing or not executable: {ue_path}")
    platform = _ue_host_platform(ue_path)
    if platform not in {"Linux", "Mac"}:
        raise WorkspaceError(f"automatic plugin build is supported on Linux and Mac, not {platform or 'unknown'}")
    engine_root = ue_path.parents[2]
    uat = engine_root / "Build" / "BatchFiles" / "RunUAT.sh"
    if not uat.is_file() or not os.access(uat, os.X_OK):
        raise WorkspaceError(f"RunUAT.sh is missing or not executable: {uat}")
    source = _existing_directory(
        Path(repo_root).expanduser().resolve(strict=False) / "ue_template" / "Plugins" / "ADPPhysicsRuntime",
        "template runtime plugin",
    )
    cache_key = _plugin_build_cache_key(source, engine_root / "Build" / "Build.version")
    build_root = root / "cache" / "ue_plugins"
    package = build_root / f"ADPPhysicsRuntime_{platform}_{cache_key}"
    log_path = build_root / f"ADPPhysicsRuntime_{platform}_{cache_key}.log"
    reused = _plugin_binary_ready(package, platform)
    command: list[str] = []
    if not reused:
        if _lexists(package):
            raise WorkspaceError(f"cached plugin package is incomplete; inspect without overwriting it: {package}")
        build_root.mkdir(parents=True, exist_ok=True)
        staging = build_root / f".ADPPhysicsRuntime_{platform}_{cache_key}.building-{os.getpid()}-{time.time_ns()}"
        command = [
            str(uat),
            "BuildPlugin",
            f"-Plugin={source / 'ADPPhysicsRuntime.uplugin'}",
            f"-Package={staging}",
            f"-TargetPlatforms={platform}",
            f"-MaxParallelActions={max_parallel_actions}",
        ]
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                command,
                cwd=Path(repo_root).expanduser().resolve(strict=False),
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if completed.returncode != 0:
            raise WorkspaceError(f"UE plugin build failed with exit code {completed.returncode}; inspect {log_path}")
        if not _plugin_binary_ready(staging, platform):
            raise WorkspaceError(f"UE plugin build completed without the expected {platform} editor binary; inspect {log_path}")
        staging.rename(package)

    link = root / "ue" / "Plugins" / "ADPPhysicsRuntime"
    if _lexists(link) and not link.is_symlink():
        raise WorkspaceError(f"refusing to replace non-symlink runtime plugin: {link}")
    if link.is_symlink() and link.resolve(strict=False) != package:
        link.unlink()
    if not link.is_symlink():
        link.symlink_to(package, target_is_directory=True)
    return {
        "schema_version": "harness_ue_plugin_build_v1",
        "platform": platform,
        "package": str(package),
        "runtime_plugin": str(link),
        "reused": reused,
        "log": str(log_path) if log_path.exists() else None,
        "command": command,
    }


def _plugin_build_cache_key(source: Path, build_version: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((row for row in source.rglob("*") if row.is_file() and not row.is_symlink()), key=lambda row: row.as_posix()):
        digest.update(path.relative_to(source).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    if build_version.is_file() and not build_version.is_symlink():
        digest.update(build_version.read_bytes())
    return digest.hexdigest()[:12]


def _contains_ue_package(content: Path) -> bool:
    return any(next(content.rglob(pattern), None) is not None for pattern in ("*.uasset", "*.umap"))


def _asset_mount_ready(root: Path, content: Path | None) -> bool:
    mount = root / "ue" / "Content"
    if content is None or not mount.is_dir():
        return False
    expected = content.resolve(strict=False)
    for child in mount.iterdir():
        if not child.is_symlink():
            continue
        try:
            if child.resolve(strict=True).is_relative_to(expected):
                return True
        except OSError:
            continue
    return False


def _native_smoke_accepted(root: Path, run: Path | None) -> bool:
    if run is None or run.is_symlink() or not run.is_dir():
        return False
    cases = (root / "cases").resolve(strict=False)
    if not run.resolve(strict=False).is_relative_to(cases):
        return False
    try:
        quality = evaluate_run(
            run,
            ffprobe=shutil.which("ffprobe") or "ffprobe",
            write=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    readiness = ((quality.get("source_reports") or {}).get("run_readiness") or {})
    sync = ((quality.get("source_reports") or {}).get("render_sync") or {})
    camera_views = ((quality.get("camera_motion") or {}).get("views") or {})
    has_static_view = any(
        isinstance(row, dict)
        and row.get("camera_mode") == "fixed"
        and row.get("moving") is False
        for row in camera_views.values()
    )
    has_moving_view = any(
        isinstance(row, dict)
        and row.get("camera_mode") in {"object_bound", "trajectory"}
        and row.get("moving") is True
        and int(row.get("unique_location_count") or 0) > 1
        for row in camera_views.values()
    )
    return bool(
        quality.get("hard_gate_passed") is True
        and readiness.get("backend") == "ue"
        and readiness.get("ue_render_real") is True
        and readiness.get("local_preview_ready") is True
        and int(readiness.get("view_count") or 0) >= 2
        and sync.get("status") == "pass"
        and sync.get("multi_view_sync_ok") is True
        and has_static_view
        and has_moving_view
    )


def workspace_path(
    value: str | Path | None,
    *,
    default_relative: str | Path,
    workspace: str | Path | None = None,
) -> Path:
    """Resolve explicit absolute paths as-is and relative runtime paths under the local workspace."""
    raw = Path(value if value is not None else default_relative).expanduser()
    if raw.is_absolute():
        resolved = raw.resolve(strict=False)
        git_ancestor = _git_ancestor(resolved)
        if git_ancestor is not None:
            raise WorkspaceError(f"runtime output must be outside a Git working tree: {git_ancestor}")
        return resolved
    root = init_workspace(workspace)
    resolved = (root / raw).resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise WorkspaceError(f"relative runtime output must stay inside the workspace: {raw}")
    return resolved


def case_output_root(route: str, workspace: str | Path | None = None) -> Path:
    """Resolve physics/scenario/version into the canonical local case directory."""
    candidate = Path(str(route).strip())
    parts = candidate.parts
    if candidate.is_absolute() or len(parts) != 3 or any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", part) for part in parts):
        raise WorkspaceError("case route must be physics/scenario/vNNN_description using lowercase letters, digits, '_' or '-'")
    if not re.fullmatch(r"v\d+_[a-z0-9][a-z0-9_-]*", parts[2]):
        raise WorkspaceError("case route version must be vNNN_description")
    return workspace_path(None, default_relative=Path("cases").joinpath(*parts), workspace=workspace)


def workspace_status(path: str | Path | None = None) -> dict[str, object]:
    root = workspace_root(path)
    directories = {relative: _is_real_dir(root / relative) for relative in WORKSPACE_DIRS}
    review = {
        decision: _entry_count(root / "review" / decision)
        for decision in ("inbox", "kept", "rejected")
    }
    content = root / "ue" / "Content"
    plugin = root / "ue" / "Plugins" / "ADPPhysicsRuntime"
    mounts = sorted(child.name for child in content.iterdir() if child.is_symlink()) if _is_real_dir(content) else []
    return {
        "schema_version": "harness_workspace_status_v1",
        "workspace": str(root),
        "initialized": all(directories.values()),
        "directories": directories,
        "review": review,
        "ue": {
            "project": str(root / "ue" / "SimulatorWorkspace.uproject"),
            "project_exists": (root / "ue" / "SimulatorWorkspace.uproject").is_file(),
            "content_mounts": mounts,
            "runtime_plugin_linked": plugin.is_symlink(),
        },
    }


def configure_ue_mount(
    adp_content: str | Path,
    path: str | Path | None = None,
    *,
    repo_root: str | Path = REPO_ROOT,
) -> dict[str, object]:
    root = workspace_root(path)
    content_source = _existing_directory(adp_content, "ADP Content")
    template_root = Path(repo_root).expanduser().resolve(strict=False) / "ue_template"
    project_template = template_root / "SimulatorStudioTemplate.uproject"
    plugin_source = template_root / "Plugins" / "ADPPhysicsRuntime"
    project_bytes = _content_only_project(project_template)
    plugin_source = _existing_directory(plugin_source, "template runtime plugin")
    content_sources = sorted(
        ((child.name, child.resolve(strict=True)) for child in content_source.iterdir() if child.is_dir()),
        key=lambda item: item[0].casefold(),
    )
    if not content_sources:
        raise WorkspaceError(f"ADP Content has no top-level directories: {content_source}")

    init_workspace(root)
    ue_root = root / "ue"
    content_root = ue_root / "Content"
    plugins_root = ue_root / "Plugins"
    for directory in (ue_root, content_root, plugins_root):
        if _lexists(directory) and (directory.is_symlink() or not directory.is_dir()):
            raise WorkspaceError(f"UE mount directory is not a real directory: {directory}")

    project = ue_root / "SimulatorWorkspace.uproject"
    links = [(source, content_root / name) for name, source in content_sources]
    links.append((plugin_source, plugins_root / "ADPPhysicsRuntime"))
    if _lexists(project) and (project.is_symlink() or not project.is_file() or project.read_bytes() != project_bytes):
        raise WorkspaceError(f"refusing to overwrite existing UE project: {project}")
    for source, destination in links:
        _validate_link_destination(source, destination)

    content_root.mkdir(parents=True, exist_ok=True)
    plugins_root.mkdir(parents=True, exist_ok=True)
    if not project.exists():
        project.write_bytes(project_bytes)
    for source, destination in links:
        if not destination.is_symlink():
            destination.symlink_to(source, target_is_directory=True)
    return {
        "project": str(project),
        "content_mounts": [str(content_root / name) for name, _ in content_sources],
        "runtime_plugin": str(plugins_root / "ADPPhysicsRuntime"),
    }


def review_candidate(
    candidate: str,
    decision: str,
    path: str | Path | None = None,
) -> Path:
    root = workspace_root(path)
    _require_initialized(root)
    with _review_decision_lock(root):
        _recover_review_decisions(root)
        return _review_candidate_locked(candidate, decision, root)


def _review_candidate_locked(candidate: str, decision: str, root: Path) -> Path:
    if decision not in {"keep", "reject"}:
        raise WorkspaceError("review decision must be 'keep' or 'reject'")
    name = _candidate_name(candidate)
    source = root / "review" / "inbox" / name
    destination = root / "review" / ("kept" if decision == "keep" else "rejected") / name
    if source.is_symlink() or not source.exists():
        raise WorkspaceError(f"candidate does not exist or is a symlink: {source}")
    if not source.is_dir() and not (source.is_file() and source.suffix.lower() in VIDEO_SUFFIXES):
        raise WorkspaceError("candidate must be a directory or a supported video file")
    if _lexists(destination):
        raise WorkspaceError(f"review destination already exists: {destination}")
    if source.is_dir():
        _reject_candidate_symlinks(source)
    if decision == "keep" and source.is_dir():
        _validate_review_bundle_videos(source, root)
    manifest_path: Path | None = None
    if source.is_dir():
        loaded_manifest = _review_manifest(source, required=False)
        if loaded_manifest is not None:
            manifest_path = loaded_manifest[0]
    linked_case_status = _linked_case_status_update(source, root, decision, destination)
    status_path = linked_case_status[0] if linked_case_status is not None else None
    transaction = _begin_review_transaction(
        root=root,
        source=source,
        destination=destination,
        status_path=status_path,
        manifest_path=manifest_path,
    )
    try:
        source.rename(destination)
        if linked_case_status is not None:
            status_path, payload = linked_case_status
            write_json(status_path, payload)
        _mark_review_manifest_decided(destination, decision)
        _commit_review_transaction(transaction, root)
    except BaseException as exc:
        try:
            _recover_review_transaction(transaction, root)
        except BaseException as rollback_exc:
            raise WorkspaceError(f"review decision failed and durable rollback was incomplete: {rollback_exc}") from exc
        raise
    return destination


@contextmanager
def _review_decision_lock(root: Path) -> Iterator[None]:
    lock_path = root / "review" / ".review-decisions.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkspaceError(f"review decision lock must be a regular file: {lock_path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _begin_review_transaction(
    *,
    root: Path,
    source: Path,
    destination: Path,
    status_path: Path | None,
    manifest_path: Path | None,
) -> Path:
    transactions = root / "review" / ".transactions"
    transactions.mkdir(parents=True, exist_ok=True)
    if transactions.is_symlink() or not transactions.is_dir():
        raise WorkspaceError(f"review transaction directory must be real: {transactions}")
    transaction = transactions / f"{destination.parent.name}__{source.name}.json"
    if _lexists(transaction):
        raise WorkspaceError(f"review transaction already exists after recovery: {transaction}")

    payload: dict[str, Any] = {
        "schema_version": REVIEW_TRANSACTION_SCHEMA_VERSION,
        "state": "prepared",
        "source": source.relative_to(root).as_posix(),
        "destination": destination.relative_to(root).as_posix(),
        "status_snapshot": None,
        "manifest_snapshot": None,
    }
    created: list[Path] = []
    try:
        if status_path is not None:
            backup = transactions / f"{transaction.stem}.status.bak"
            payload["status_snapshot"] = _create_review_snapshot(status_path, backup, root=root)
            created.append(backup)
        if manifest_path is not None:
            backup = transactions / f"{transaction.stem}.manifest.bak"
            snapshot = _create_review_snapshot(manifest_path, backup, root=root)
            snapshot["candidate_relative_path"] = manifest_path.relative_to(source).as_posix()
            payload["manifest_snapshot"] = snapshot
            created.append(backup)
        write_json(transaction, payload)
        _fsync_directory(transactions)
        return transaction
    except BaseException:
        transaction.unlink(missing_ok=True)
        for created_path in created:
            created_path.unlink(missing_ok=True)
        raise


def _create_review_snapshot(source: Path, backup: Path, *, root: Path) -> dict[str, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(backup, flags, 0o600)
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "wb") as target, source.open("rb") as original:
            descriptor = -1
            for chunk in iter(lambda: original.read(1024 * 1024), b""):
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return {
        "target": source.relative_to(root).as_posix(),
        "backup": backup.relative_to(root).as_posix(),
        "sha256": digest.hexdigest(),
    }


def _commit_review_transaction(transaction: Path, root: Path) -> None:
    payload = _read_review_transaction(transaction, root)
    payload["state"] = "committed"
    write_json(transaction, payload)
    try:
        _fsync_directory(transaction.parent)
        _finalize_review_transaction(transaction, payload, root)
    except OSError:
        # The durable committed marker makes cleanup safely retryable on the next review operation.
        pass


def _recover_review_decisions(root: Path) -> None:
    transactions = root / "review" / ".transactions"
    if not transactions.exists():
        return
    if transactions.is_symlink() or not transactions.is_dir():
        raise WorkspaceError(f"review transaction directory must be real: {transactions}")
    for transaction in sorted(transactions.glob("*.json")):
        _recover_review_transaction(transaction, root)
    # A process can die after creating a backup but before its journal is durable.
    # The global review lock guarantees no live transaction owns an unreferenced backup here.
    referenced: set[Path] = set()
    for transaction in sorted(transactions.glob("*.json")):
        payload = _read_review_transaction(transaction, root)
        for key in ("status_snapshot", "manifest_snapshot"):
            snapshot = payload.get(key)
            if isinstance(snapshot, dict) and snapshot.get("backup"):
                referenced.add(_review_transaction_path(root, snapshot["backup"], label=f"{key} backup"))
    for backup in transactions.glob("*.bak"):
        if backup not in referenced and not backup.is_symlink():
            backup.unlink(missing_ok=True)


def _recover_review_transaction(transaction: Path, root: Path) -> None:
    payload = _read_review_transaction(transaction, root)
    if payload.get("state") == "committed":
        _finalize_review_transaction(transaction, payload, root)
        return

    source = _review_transaction_path(root, payload.get("source"), label="source")
    destination = _review_transaction_path(root, payload.get("destination"), label="destination")
    if source.parent != root / "review" / "inbox" or destination.parent not in {
        root / "review" / "kept",
        root / "review" / "rejected",
    } or source.name != destination.name:
        raise WorkspaceError(f"review transaction has invalid candidate routes: {transaction}")
    source_exists = _lexists(source)
    destination_exists = _lexists(destination)
    if source_exists == destination_exists:
        raise WorkspaceError(
            f"review transaction cannot determine one candidate location: source={source_exists}, destination={destination_exists}"
        )
    candidate = source if source_exists else destination
    _restore_review_snapshot(payload.get("status_snapshot"), root=root)
    manifest_snapshot = payload.get("manifest_snapshot")
    if manifest_snapshot is not None:
        if not isinstance(manifest_snapshot, dict):
            raise WorkspaceError(f"review transaction manifest snapshot is invalid: {transaction}")
        relative = _safe_relative_path(
            manifest_snapshot.get("candidate_relative_path"),
            label="review transaction manifest",
        )
        _restore_review_snapshot(manifest_snapshot, root=root, target_override=candidate / relative)
    if destination_exists:
        destination.rename(source)
    _finalize_review_transaction(transaction, payload, root)


def _read_review_transaction(transaction: Path, root: Path) -> dict[str, Any]:
    if transaction.is_symlink() or transaction.parent != root / "review" / ".transactions":
        raise WorkspaceError(f"unsafe review transaction: {transaction}")
    try:
        payload = json.loads(transaction.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"invalid review transaction: {transaction}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != REVIEW_TRANSACTION_SCHEMA_VERSION
        or payload.get("state") not in {"prepared", "committed"}
    ):
        raise WorkspaceError(f"unsupported review transaction: {transaction}")
    return payload


def _restore_review_snapshot(
    snapshot: Any,
    *,
    root: Path,
    target_override: Path | None = None,
) -> None:
    if snapshot is None:
        return
    if not isinstance(snapshot, dict):
        raise WorkspaceError("review transaction snapshot must be an object")
    target = target_override or _review_transaction_path(root, snapshot.get("target"), label="snapshot target")
    backup = _review_transaction_backup_path(root, snapshot.get("backup"), label="snapshot backup")
    expected = snapshot.get("sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise WorkspaceError("review transaction snapshot has an invalid SHA-256")
    if backup.is_file() and not backup.is_symlink():
        if _sha256_file(backup) != expected:
            raise WorkspaceError(f"review transaction backup hash mismatch: {backup}")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(backup, target)
    elif not target.is_file() or target.is_symlink() or _sha256_file(target) != expected:
        raise WorkspaceError(f"review transaction snapshot is missing or unrestorable: {backup}")


def _review_transaction_path(root: Path, value: Any, *, label: str) -> Path:
    relative = _safe_relative_path(value, label=f"review transaction {label}")
    path = (root / relative).resolve(strict=False)
    if not path.is_relative_to(root):
        raise WorkspaceError(f"review transaction {label} escapes workspace: {value!r}")
    return path


def _review_transaction_backup_path(root: Path, value: Any, *, label: str) -> Path:
    path = _review_transaction_path(root, value, label=label)
    transactions = root / "review" / ".transactions"
    if path.parent != transactions or path.suffix != ".bak":
        raise WorkspaceError(f"review transaction {label} must be one .bak file under {transactions}: {value!r}")
    return path


def _finalize_review_transaction(transaction: Path, payload: dict[str, Any], root: Path) -> None:
    for key in ("status_snapshot", "manifest_snapshot"):
        snapshot = payload.get(key)
        if isinstance(snapshot, dict) and snapshot.get("backup"):
            _review_transaction_backup_path(root, snapshot["backup"], label=f"{key} backup").unlink(missing_ok=True)
    transaction.unlink(missing_ok=True)
    _fsync_directory(transaction.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            # Some filesystems do not support directory fsync; file-level fsync and atomic rename still apply.
            pass
    finally:
        os.close(descriptor)


def _reject_candidate_symlinks(candidate: Path) -> None:
    for path in candidate.rglob("*"):
        if path.is_symlink():
            raise WorkspaceError(f"review candidate must not contain symlinks: {path}")


def _review_manifest(candidate: Path, *, required: bool = True) -> tuple[Path, dict[str, Any]] | None:
    manifests = sorted(candidate.glob("*.review.json"))
    if not manifests and not required:
        return None
    if len(manifests) != 1 or manifests[0].is_symlink():
        raise WorkspaceError("review bundle must contain exactly one real *.review.json manifest")
    try:
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"invalid review manifest: {manifests[0]}") from exc
    if not isinstance(manifest, dict):
        raise WorkspaceError(f"review manifest must be a JSON object: {manifests[0]}")
    declared_candidate = manifest.get("candidate")
    if declared_candidate != candidate.name:
        raise WorkspaceError(
            f"review manifest candidate does not match its directory: {declared_candidate!r} != {candidate.name!r}"
        )
    return manifests[0], manifest


def _validate_review_bundle_videos(candidate: Path, workspace: Path) -> None:
    loaded = _review_manifest(candidate)
    assert loaded is not None
    _, manifest = loaded
    videos = manifest.get("videos") if isinstance(manifest, dict) else None
    if not isinstance(videos, list) or not videos:
        raise WorkspaceError("review bundle manifest must declare at least one hashed video before keep")
    declared: set[str] = set()
    for row in videos:
        name = row.get("file") if isinstance(row, dict) else None
        expected = row.get("sha256") if isinstance(row, dict) else None
        relative = Path(name) if isinstance(name, str) else Path()
        if (
            not isinstance(name, str)
            or not name
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.as_posix() != name
        ):
            raise WorkspaceError(f"review manifest contains an unsafe video filename: {name!r}")
        if name in declared:
            raise WorkspaceError(f"review manifest declares a duplicate video: {name}")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise WorkspaceError(f"review manifest has an invalid SHA-256 for video: {name}")
        path = candidate / relative
        if (
            any(parent.is_symlink() for parent in (path, *path.parents) if parent != candidate.parent)
            or not path.is_file()
            or path.suffix.lower() not in VIDEO_SUFFIXES
        ):
            raise WorkspaceError(f"review bundle video is missing, unsafe, or unsupported: {path}")
        if _sha256_file(path) != expected.lower():
            raise WorkspaceError(f"review bundle video SHA-256 mismatch: {name}")
        declared.add(name)
    actual = {
        path.relative_to(candidate).as_posix()
        for path in candidate.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in VIDEO_SUFFIXES
    }
    if actual != declared:
        raise WorkspaceError(
            f"review manifest video set does not match bundle files: declared={sorted(declared)}, actual={sorted(actual)}"
        )
    _validate_source_truth(manifest, workspace, candidate)


def _validate_source_truth(
    manifest: dict[str, Any],
    workspace: Path,
    candidate: Path,
) -> None:
    source_runs = manifest.get("source_runs")
    if source_runs is None:
        return
    if not isinstance(source_runs, list) or not source_runs:
        raise WorkspaceError("complete review manifest source_runs must be a non-empty list")
    expected_views = manifest.get("views")
    if (
        not isinstance(expected_views, list)
        or not expected_views
        or any(not isinstance(view, str) or not view for view in expected_views)
        or len(set(expected_views)) != len(expected_views)
    ):
        raise WorkspaceError("complete review manifest must declare a unique non-empty camera view list")
    cases_root = (workspace / "cases").resolve(strict=False)
    for row in source_runs:
        source_run = row.get("source_run") if isinstance(row, dict) else None
        source_truth = row.get("source_truth") if isinstance(row, dict) else None
        if not isinstance(source_run, str) or not source_run or not isinstance(source_truth, dict) or not source_truth:
            raise WorkspaceError("complete review manifest source run is missing its source_truth provenance")
        run_dir = Path(source_run).expanduser().resolve(strict=False)
        if not run_dir.is_relative_to(cases_root) or run_dir.is_symlink() or not run_dir.is_dir():
            raise WorkspaceError(f"source run must be a real directory under workspace/cases: {run_dir}")
        quality_path = Path(str(row.get("source_quality_report") or "")).expanduser().resolve(strict=False)
        expected_quality_hash = row.get("source_quality_report_sha256")
        if quality_path != run_dir / "quality_report.json" or quality_path.is_symlink() or not quality_path.is_file():
            raise WorkspaceError(f"source quality report is missing or does not belong to its run: {quality_path}")
        if not isinstance(expected_quality_hash, str) or _sha256_file(quality_path) != expected_quality_hash.lower():
            raise WorkspaceError(f"source quality report SHA-256 mismatch: {quality_path}")
        if any(not isinstance(view, str) or not view for view in source_truth):
            raise WorkspaceError(f"invalid source truth camera identifier in run: {run_dir}")
        if set(source_truth) != set(expected_views):
            raise WorkspaceError(
                f"source truth camera set does not match delivery views: "
                f"declared={sorted(source_truth)}, expected={sorted(expected_views)}"
            )
        for view, modalities in source_truth.items():
            if not isinstance(view, str) or not view or not isinstance(modalities, dict):
                raise WorkspaceError(f"invalid source truth view provenance in run: {run_dir}")
            meta = run_dir / "views" / view / "meta.json"
            if meta.is_symlink():
                raise WorkspaceError(f"source truth RGB metadata must not be a symlink: {meta}")
            try:
                rgb_frame_count = json.loads(meta.read_text(encoding="utf-8")).get("frame_count_rgb")
            except (AttributeError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkspaceError(f"source truth RGB metadata is invalid: {meta}") from exc
            if isinstance(rgb_frame_count, bool) or not isinstance(rgb_frame_count, int) or rgb_frame_count <= 1:
                raise WorkspaceError(f"source truth RGB frame count must exceed one: {meta}")
            for modality in ("depth", "segmentation"):
                declared = modalities.get(modality)
                if not isinstance(declared, dict):
                    raise WorkspaceError(f"source truth is missing {modality} provenance for {view}: {run_dir}")
                relative = _safe_relative_path(declared.get("path"), label=f"{view}/{modality} source truth")
                sequence = (run_dir / relative).resolve(strict=False)
                if not sequence.is_relative_to(run_dir):
                    raise WorkspaceError(f"source truth path escapes its run: {relative}")
                try:
                    actual_provenance = exr_sequence_provenance(sequence, relative_to=run_dir)
                except (DeliveryError, ValueError) as exc:
                    raise WorkspaceError(f"source truth sequence is invalid: {sequence}") from exc
                for key in ("path", "format", "frame_count", "aggregate_sha256", "hash_algorithm"):
                    if declared.get(key) != actual_provenance[key]:
                        raise WorkspaceError(
                            f"source truth {key} mismatch for {view}/{modality}: "
                            f"declared={declared.get(key)!r}, actual={actual_provenance[key]!r}"
                        )
                if actual_provenance["frame_count"] != rgb_frame_count:
                    raise WorkspaceError(
                        f"source truth frame count does not match RGB for {view}/{modality}: "
                        f"exr={actual_provenance['frame_count']}, rgb={rgb_frame_count}"
                    )
                if "delivery_path" in declared:
                    delivery_relative = _safe_relative_path(
                        declared.get("delivery_path"),
                        label=f"{view}/{modality} delivery truth",
                    )
                    delivery_sequence = (candidate / delivery_relative).resolve(strict=False)
                    if (
                        not delivery_sequence.is_relative_to(candidate)
                        or any(
                            parent.is_symlink()
                            for parent in (candidate / delivery_relative, *(candidate / delivery_relative).parents)
                            if parent != candidate.parent
                        )
                    ):
                        raise WorkspaceError(
                            f"delivery truth path is unsafe for {view}/{modality}: {delivery_relative}"
                        )
                    try:
                        delivery_provenance = exr_sequence_provenance(
                            delivery_sequence,
                            relative_to=candidate,
                        )
                    except (DeliveryError, ValueError) as exc:
                        raise WorkspaceError(
                            f"delivery truth sequence is invalid: {delivery_sequence}"
                        ) from exc
                    expected_delivery = {
                        "path": delivery_relative.as_posix(),
                        "format": declared.get("format"),
                        "frame_count": declared.get("frame_count"),
                        "aggregate_sha256": declared.get("aggregate_sha256"),
                        "hash_algorithm": declared.get("hash_algorithm"),
                    }
                    for key, expected in expected_delivery.items():
                        if delivery_provenance.get(key) != expected:
                            raise WorkspaceError(
                                f"delivery truth {key} mismatch for {view}/{modality}: "
                                f"declared={expected!r}, actual={delivery_provenance.get(key)!r}"
                            )


def _safe_relative_path(value: Any, *, label: str) -> Path:
    relative = Path(value) if isinstance(value, str) else Path()
    if (
        not isinstance(value, str)
        or not value
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.as_posix() != value
    ):
        raise WorkspaceError(f"review manifest contains an unsafe {label} path: {value!r}")
    return relative


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mark_review_manifest_decided(destination: Path, decision: str) -> None:
    if not destination.is_dir():
        return
    manifests = sorted(destination.glob("*.review.json"))
    if len(manifests) != 1:
        return
    try:
        payload = json.loads(manifests[0].read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    payload["status"] = "kept" if decision == "keep" else "rejected"
    payload["decision"] = f"user_{decision}"
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    payload["destination"] = str(destination)
    write_json(manifests[0], payload)


def _linked_case_status_update(
    candidate: Path,
    workspace: Path,
    decision: str,
    destination: Path,
) -> tuple[Path, dict[str, Any]] | None:
    if not candidate.is_dir():
        return None
    loaded = _review_manifest(candidate, required=False)
    if loaded is None:
        return None
    manifest_path, manifest = loaded
    raw_status = manifest.get("case_status") if isinstance(manifest, dict) else None
    case_route = manifest.get("case_route")
    if raw_status is None and case_route is None:
        return None
    if (
        manifest.get("candidate") != candidate.name
        or not isinstance(case_route, str)
        or not case_route
        or not isinstance(raw_status, str)
        or not raw_status
    ):
        raise WorkspaceError("linked review manifest requires matching candidate, case_route, and case_status")
    try:
        expected_status = case_output_root(case_route, workspace) / "case_status.json"
    except WorkspaceError as exc:
        raise WorkspaceError(f"review manifest has an invalid case_route: {case_route!r}") from exc
    status_path = Path(raw_status).expanduser().resolve(strict=False)
    if status_path != expected_status or status_path.is_symlink() or not status_path.is_file():
        raise WorkspaceError(f"review manifest case_status does not match case_route: {status_path}")
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"invalid linked case status: {status_path}") from exc
    if not isinstance(payload, dict):
        raise WorkspaceError(f"linked case status must be a JSON object: {status_path}")
    review = payload.get("review")
    expected_manifest = manifest_path.resolve(strict=False)
    if payload.get("case_route") != case_route or not isinstance(review, dict):
        raise WorkspaceError(f"linked case status does not point back to case_route/review: {status_path}")
    review_inbox = Path(str(review.get("inbox") or "")).expanduser().resolve(strict=False)
    review_manifest = Path(str(review.get("manifest") or "")).expanduser().resolve(strict=False)
    if (
        review.get("candidate") != candidate.name
        or review_inbox != candidate.resolve(strict=False)
        or review_manifest != expected_manifest
    ):
        raise WorkspaceError(f"linked case status review binding does not match candidate/manifest: {status_path}")
    payload["status"] = "kept" if decision == "keep" else "rejected"
    payload["decision"] = f"user_{decision}"
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    review["decision"] = decision
    review["destination"] = str(destination)
    review["source_inbox"] = str(candidate)
    review["manifest"] = str(destination / manifest_path.name)
    review.pop("inbox", None)
    return status_path, payload


def prune_rejected(
    older_than_days: int | float,
    path: str | Path | None = None,
    *,
    dry_run: bool = True,
    now: float | None = None,
) -> list[Path]:
    root = workspace_root(path)
    _require_initialized(root)
    if isinstance(older_than_days, bool):
        raise WorkspaceError("older_than_days must be a non-negative number")
    days = float(older_than_days)
    if not math.isfinite(days) or days < 0:
        raise WorkspaceError("older_than_days must be a non-negative number")
    rejected = root / "review" / "rejected"
    cutoff = (time.time() if now is None else now) - days * 86_400
    candidates = []
    for candidate in sorted(rejected.iterdir(), key=lambda item: item.name.casefold()):
        try:
            if candidate.lstat().st_mtime < cutoff:
                candidates.append(candidate)
        except FileNotFoundError:
            continue
    if not dry_run:
        for candidate in candidates:
            _remove_rejected_candidate(candidate, rejected)
    return candidates


def _require_initialized(root: Path) -> None:
    missing = [relative for relative in WORKSPACE_DIRS if not _is_real_dir(root / relative)]
    if missing:
        raise WorkspaceError(f"workspace is not initialized; missing: {', '.join(missing)}")


def _content_only_project(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise WorkspaceError(f"UE project template does not exist: {path}")
    data = path.read_bytes()
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"invalid UE project template: {path}") from exc
    if not isinstance(payload, dict) or payload.get("Modules"):
        raise WorkspaceError(f"UE project template must be content-only: {path}")
    plugins = payload.get("Plugins") or []
    if not any(plugin.get("Name") == "ADPPhysicsRuntime" and plugin.get("Enabled") for plugin in plugins if isinstance(plugin, dict)):
        raise WorkspaceError("UE project template must enable ADPPhysicsRuntime")
    return data


def _existing_directory(path: str | Path, label: str) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise WorkspaceError(f"{label} directory does not exist: {path}") from exc
    if not resolved.is_dir():
        raise WorkspaceError(f"{label} is not a directory: {resolved}")
    return resolved


def _validate_link_destination(source: Path, destination: Path) -> None:
    if destination.is_symlink():
        if destination.resolve(strict=False) != source:
            raise WorkspaceError(f"symlink points to a different source: {destination}")
        return
    if _lexists(destination):
        raise WorkspaceError(f"refusing to overwrite UE mount entry: {destination}")


def _candidate_name(value: str) -> str:
    if not value or value in {".", ".."} or "\0" in value or "/" in value or "\\" in value or Path(value).name != value:
        raise WorkspaceError("candidate must be one direct child name from review/inbox")
    return value


def _remove_rejected_candidate(candidate: Path, rejected: Path) -> None:
    if candidate.parent != rejected:
        raise WorkspaceError(f"refusing to prune outside review/rejected: {candidate}")
    if candidate.is_symlink() or not candidate.is_dir():
        candidate.unlink(missing_ok=True)
    else:
        shutil.rmtree(candidate)


def _entry_count(path: Path) -> int:
    return sum(1 for entry in path.iterdir() if not entry.name.startswith(".")) if _is_real_dir(path) else 0


def _is_real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _git_ancestor(path: Path) -> Path | None:
    return next((parent for parent in (path, *path.parents) if (parent / ".git").exists()), None)
