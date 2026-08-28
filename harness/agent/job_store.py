from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from harness.agent.job_schema import AttemptManifest, IntentContract, JobManifest, validate_attempt_id, validate_job_id
from harness.core.artifact_schema import read_json, write_json
from harness.core.workspace import init_workspace


class JobStoreError(ValueError):
    pass


class JobStore:
    def __init__(self, workspace: str | Path | None = None) -> None:
        self.workspace = init_workspace(workspace)
        self.jobs_root = self.workspace / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        identity = validate_job_id(job_id)
        path = (self.jobs_root / identity).resolve(strict=False)
        if not path.is_relative_to(self.jobs_root.resolve()):
            raise JobStoreError("job path escapes jobs root")
        return path

    def create(self, manifest: Mapping[str, Any]) -> Path:
        validated = JobManifest.from_dict(manifest).to_dict()
        root = self.job_dir(validated["job_id"])
        try:
            root.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise JobStoreError(f"job already exists: {validated['job_id']}") from exc
        for relative in ("request", "attempts", "checkpoints", "receipts", "events"):
            (root / relative).mkdir()
        write_json(root / "job_manifest.json", validated)
        return root

    def load_manifest(self, job_id: str) -> dict[str, Any]:
        path = self.job_dir(job_id) / "job_manifest.json"
        if not path.is_file():
            raise JobStoreError(f"job does not exist: {job_id}")
        return JobManifest.from_dict(read_json(path)).to_dict()

    def write_manifest(self, manifest: Mapping[str, Any]) -> Path:
        validated = JobManifest.from_dict(manifest).to_dict()
        path = self.job_dir(validated["job_id"]) / "job_manifest.json"
        if not path.parent.is_dir():
            raise JobStoreError(f"job does not exist: {validated['job_id']}")
        write_json(path, validated)
        return path

    def write_request_artifact(self, job_id: str, filename: str, value: Any, *, immutable: bool = True) -> Path:
        if not filename or Path(filename).name != filename or not filename.endswith(".json"):
            raise JobStoreError("request artifact filename must be a simple JSON filename")
        path = self.job_dir(job_id) / "request" / filename
        if immutable and path.exists():
            existing = read_json(path)
            if existing != value:
                raise JobStoreError(f"immutable request artifact already differs: {filename}")
            return path
        write_json(path, value)
        return path

    def write_intent_contract(self, contract: Mapping[str, Any]) -> Path:
        validated = IntentContract.from_dict(contract).to_dict()
        return self.write_request_artifact(validated["job_id"], "intent_contract.json", validated)

    def attempt_dir(self, job_id: str, attempt_id: str) -> Path:
        validate_attempt_id(attempt_id)
        return self.job_dir(job_id) / "attempts" / attempt_id

    def create_attempt(self, manifest: Mapping[str, Any], case_spec: Mapping[str, Any]) -> Path:
        validated = AttemptManifest.from_dict(manifest).to_dict()
        root = self.attempt_dir(validated["job_id"], validated["attempt_id"])
        try:
            root.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise JobStoreError(f"attempt already exists: {validated['attempt_id']}") from exc
        for relative in ("stage_results", "compilation", "runs/smoke", "runs/candidate", "evidence_bundle"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        write_json(root / "case_spec.json", case_spec)
        write_json(root / "attempt_manifest.json", validated)
        return root

    def load_attempt(self, job_id: str, attempt_id: str) -> dict[str, Any]:
        path = self.attempt_dir(job_id, attempt_id) / "attempt_manifest.json"
        if not path.is_file():
            raise JobStoreError(f"attempt does not exist: {attempt_id}")
        return AttemptManifest.from_dict(read_json(path)).to_dict()

    def write_attempt(self, manifest: Mapping[str, Any]) -> Path:
        validated = AttemptManifest.from_dict(manifest).to_dict()
        path = self.attempt_dir(validated["job_id"], validated["attempt_id"]) / "attempt_manifest.json"
        if not path.parent.is_dir():
            raise JobStoreError(f"attempt does not exist: {validated['attempt_id']}")
        write_json(path, validated)
        return path

    def write_checkpoint(self, job_id: str, stage: str, value: Mapping[str, Any]) -> Path:
        if not stage or not stage.replace("_", "").isalnum():
            raise JobStoreError("checkpoint stage is invalid")
        path = self.job_dir(job_id) / "checkpoints" / f"{stage}.json"
        write_json(path, value)
        return path

    def read_optional(self, job_id: str, relative: str | Path) -> Any:
        path = self._job_relative(job_id, relative)
        return read_json(path) if path.is_file() else None

    def append_event(self, job_id: str, event: Mapping[str, Any]) -> Path:
        root = self.job_dir(job_id) / "events"
        sequence = len(list(root.glob("*.json"))) + 1
        path = root / f"{sequence:06d}.json"
        write_json(path, dict(event))
        return path

    @contextmanager
    def lock(self, job_id: str, *, blocking: bool = False) -> Iterator[None]:
        root = self.job_dir(job_id)
        if not root.is_dir():
            raise JobStoreError(f"job does not exist: {job_id}")
        descriptor = os.open(root / ".controller.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(descriptor, operation)
            except BlockingIOError as exc:
                raise JobStoreError(f"job is already being advanced: {job_id}") from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _job_relative(self, job_id: str, relative: str | Path) -> Path:
        root = self.job_dir(job_id).resolve()
        path = (root / Path(relative)).resolve(strict=False)
        if not path.is_relative_to(root):
            raise JobStoreError("job artifact path escapes job root")
        return path
