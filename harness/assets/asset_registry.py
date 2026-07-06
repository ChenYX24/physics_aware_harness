from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


class AssetRegistry:
    def __init__(self, path: str | Path = ROOT / "assets" / "asset_physics_index.json") -> None:
        self.path = Path(path)
        self.assets = self._load()

    def search(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        if not self.assets:
            return []
        q = query.casefold()
        scored = []
        for item in self.assets:
            text = searchable_text(item)
            score = sum(1 for token in q.split() if token and token in text)
            if score:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("id") or pair[1].get("name") or "")))
        return [item for _, item in scored[:top_k]]

    def _load(self) -> list[dict[str, Any]]:
        path = self.path
        if not path.exists() and self.path.name == "asset_physics_index.json":
            path = ROOT / "assets" / "asset_registry.example.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("assets", "items", "entries"):
                if isinstance(data.get(key), list):
                    return [item for item in data[key] if isinstance(item, dict)]
        return []


def searchable_text(item: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("id", "asset_id", "name", "path", "ue_path", "tags", "category", "type", "collider", "shape"):
        value = item.get(key)
        if isinstance(value, list):
            values.extend(str(entry) for entry in value)
        elif isinstance(value, dict):
            values.extend(str(entry) for entry in value.values())
        elif value is not None:
            values.append(str(value))
    return " ".join(values).casefold()
