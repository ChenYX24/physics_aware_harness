from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class EmbeddingModelSpec:
    provider: str
    model_name: str
    pretrained: str
    dimension: int
    document_version: str
    library_version: str = "unknown"
    checkpoint_sha256: str | None = None

    @property
    def model_id(self) -> str:
        parts = (
            self.provider,
            self.model_name,
            self.pretrained,
            self.document_version,
            str(self.dimension),
            self.library_version,
            self.checkpoint_sha256 or "unverified-checkpoint",
        )
        return ":".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_name": self.model_name,
            "pretrained": self.pretrained,
            "dimension": self.dimension,
            "document_version": self.document_version,
            "library_version": self.library_version,
            "checkpoint_sha256": self.checkpoint_sha256,
            "model_id": self.model_id,
        }


class EmbeddingProvider(Protocol):
    @property
    def spec(self) -> EmbeddingModelSpec: ...

    def encode_texts(self, values: Sequence[str]) -> list[list[float]]: ...

    def encode_images(self, paths: Sequence[Path]) -> list[list[float]]: ...


class OpenCLIPEmbeddingProvider:
    def __init__(
        self,
        *,
        model_name: str,
        pretrained: str,
        dimension: int,
        document_version: str,
        cache_dir: str | Path,
        device: str = "cpu",
        allow_download: bool = False,
        checkpoint_path: str | Path | None = None,
        source_uri: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.pretrained = pretrained
        self.dimension = int(dimension)
        self.document_version = document_version
        self.cache_dir = Path(cache_dir)
        self.device = device
        self.allow_download = allow_download
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        self.source_uri = source_uri
        self._resolved_checkpoint_path: Path | None = self.checkpoint_path
        self._checkpoint_sha256: str | None = None
        self._model: Any = None
        self._preprocess: Any = None
        self._tokenizer: Any = None

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        workspace: str | Path,
        allow_download: bool | None = None,
    ) -> OpenCLIPEmbeddingProvider:
        embedding = config.get("embedding") if isinstance(config.get("embedding"), Mapping) else {}
        raw_cache = str(embedding.get("model_cache") or "${SIM_HARNESS_WORKSPACE}/models/openclip")
        cache_dir = raw_cache.replace("${SIM_HARNESS_WORKSPACE}", str(workspace))
        return cls(
            model_name=str(embedding.get("model_name") or "xlm-roberta-base-ViT-B-32"),
            pretrained=str(embedding.get("pretrained") or "laion5b_s13b_b90k"),
            dimension=int(embedding.get("dimension") or 512),
            document_version=str(embedding.get("document_version") or "asset_semantic_document_v1"),
            cache_dir=cache_dir,
            device=str(embedding.get("device") or "cpu"),
            allow_download=bool(embedding.get("allow_download", False)) if allow_download is None else allow_download,
            checkpoint_path=embedding.get("checkpoint_path"),
            source_uri=str(embedding.get("source_uri") or "") or None,
        )

    @property
    def spec(self) -> EmbeddingModelSpec:
        try:
            library_version = importlib.metadata.version("open_clip_torch")
        except importlib.metadata.PackageNotFoundError:
            library_version = "not-installed"
        checkpoint_hash = self._resolve_checkpoint_hash()
        return EmbeddingModelSpec(
            provider="open_clip",
            model_name=self.model_name,
            pretrained=self.pretrained,
            dimension=self.dimension,
            document_version=self.document_version,
            library_version=library_version,
            checkpoint_sha256=checkpoint_hash,
        )

    def encode_texts(self, values: Sequence[str]) -> list[list[float]]:
        if not values:
            return []
        self._ensure_loaded()
        import torch

        tokens = self._tokenizer(list(values)).to(self.device)
        with torch.inference_mode():
            features = self._model.encode_text(tokens)
            features = features / features.norm(dim=-1, keepdim=True)
        return [[float(component) for component in row] for row in features.detach().cpu().float().tolist()]

    def encode_images(self, paths: Sequence[Path]) -> list[list[float]]:
        if not paths:
            return []
        self._ensure_loaded()
        import torch
        from PIL import Image

        tensors = []
        for path in paths:
            with Image.open(path) as image:
                tensors.append(self._preprocess(image.convert("RGB")))
        batch = torch.stack(tensors).to(self.device)
        with torch.inference_mode():
            features = self._model.encode_image(batch)
            features = features / features.norm(dim=-1, keepdim=True)
        return [[float(component) for component in row] for row in features.detach().cpu().float().tolist()]

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        pretrained = str(self.checkpoint_path) if self.checkpoint_path else self.pretrained
        with _model_cache_environment(self.cache_dir, allow_download=self.allow_download):
            try:
                import open_clip
            except ImportError as exc:
                raise RuntimeError(
                    "OpenCLIP is not installed; install requirements-asset-retrieval.txt before rebuilding vectors"
                ) from exc
            try:
                model, _, preprocess = open_clip.create_model_and_transforms(
                    self.model_name,
                    pretrained=pretrained,
                    device=self.device,
                    cache_dir=str(self.cache_dir),
                    weights_only=True,
                )
                tokenizer = open_clip.get_tokenizer(self.model_name, cache_dir=str(self.cache_dir))
            except Exception as exc:
                mode = "download enabled" if self.allow_download else "offline mode"
                raise RuntimeError(
                    f"Unable to load OpenCLIP {self.model_name}/{self.pretrained} from {self.cache_dir} ({mode})"
                ) from exc
        model.eval()
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = tokenizer
        self._resolve_checkpoint_hash()

    def _resolve_checkpoint_hash(self) -> str | None:
        if self._checkpoint_sha256:
            return self._checkpoint_sha256
        path = self._resolved_checkpoint_path
        if path is None or not path.is_file():
            candidates = sorted(self.cache_dir.rglob("open_clip_pytorch_model.bin")) if self.cache_dir.is_dir() else []
            if self.source_uri:
                repo_name = self.source_uri.rstrip("/").rsplit("/", 1)[-1].casefold()
                matching = [candidate for candidate in candidates if repo_name in str(candidate).casefold()]
                if matching:
                    candidates = matching
            if len(candidates) == 1:
                path = candidates[0]
                self._resolved_checkpoint_path = path
        if path is not None and path.is_file():
            self._checkpoint_sha256 = sha256_file(path)
        return self._checkpoint_sha256


def semantic_document(asset: Mapping[str, Any], *, document_version: str) -> str:
    values: list[str] = [document_version]
    for key in (
        "asset_id",
        "id",
        "name",
        "semantic_name",
        "description",
        "aliases",
        "tags",
        "usage_groups",
        "category",
        "category_l1",
        "category_l2",
        "type",
        "asset_kind",
        "physics_role",
        "collider",
        "source_kind",
    ):
        value = asset.get(key)
        if isinstance(value, Mapping):
            values.extend(str(item) for item in value.values())
        elif isinstance(value, (list, tuple, set)):
            values.extend(str(item) for item in value)
        elif value is not None:
            values.append(str(value))
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = " ".join(str(value).split()).strip()
        normalized = text.casefold()
        if text and normalized not in seen:
            seen.add(normalized)
            ordered.append(text)
    return " | ".join(ordered)


def preview_paths(asset: Mapping[str, Any]) -> list[tuple[str, Path]]:
    paths = asset.get("paths") if isinstance(asset.get("paths"), Mapping) else {}
    raw: list[tuple[str, Any]] = [
        ("thumbnail", asset.get("thumbnail")),
        ("paths.thumbnail", paths.get("thumbnail")),
        ("preview", asset.get("preview")),
    ]
    previews = asset.get("previews")
    if isinstance(previews, list):
        raw.extend(
            (f"previews.{index}", value.get("local_path") if isinstance(value, Mapping) else value)
            for index, value in enumerate(previews)
        )
    result: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for role, value in raw:
        if not value:
            continue
        path = Path(str(value))
        identity = str(path)
        if identity not in seen and path.is_file():
            seen.add(identity)
            result.append((role, path))
    return result


def normalize_vector(vector: Iterable[float], *, dimension: int) -> list[float]:
    values = [float(value) for value in vector]
    if len(values) != dimension:
        raise ValueError(f"Embedding dimension mismatch: expected {dimension}, got {len(values)}")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("Embedding contains a non-finite value")
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0.0:
        raise ValueError("Embedding has zero norm")
    return [value / norm for value in values]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path | None) -> str | None:
    if path is None:
        return None
    target = Path(path)
    if not target.is_file():
        return None
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@contextmanager
def _model_cache_environment(cache_dir: Path, *, allow_download: bool):
    model_root = cache_dir.parent
    updates = {
        "HF_HOME": str(model_root / "huggingface"),
        "HUGGINGFACE_HUB_CACHE": str(model_root / "huggingface" / "hub"),
        "TORCH_HOME": str(model_root / "torch"),
    }
    if not allow_download:
        updates.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
