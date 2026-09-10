from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from harness.assets.search_intent import SearchIntent


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RETRIEVAL_CONFIG = ROOT / "config" / "asset_retrieval_v1.json"


@dataclass(frozen=True)
class RecallHit:
    asset_id: str
    raw_score: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RankingConfig:
    schema_version: str
    rrf_k: int
    channel_weights: dict[str, float]
    exact_priority: bool
    materialized_bonus: float
    taxonomy_match_bonus: float
    preference_scale: float
    abstention_enabled: bool = True
    abstention_require_no_lexical_hits: bool = True
    minimum_vector_similarity: float = 0.55
    minimum_top1_margin: float = 0.01

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> RankingConfig:
        rrf = data.get("rrf") if isinstance(data.get("rrf"), Mapping) else {}
        rules = data.get("rules") if isinstance(data.get("rules"), Mapping) else {}
        abstention = data.get("abstention") if isinstance(data.get("abstention"), Mapping) else {}
        weights = rrf.get("weights") if isinstance(rrf.get("weights"), Mapping) else {}
        rrf_k = int(rrf.get("k", 60))
        if rrf_k <= 0:
            raise ValueError("Asset retrieval RRF k must be positive")
        minimum_vector_similarity = float(abstention.get("minimum_vector_similarity", 0.55))
        minimum_top1_margin = float(abstention.get("minimum_top1_margin", 0.01))
        if not -1.0 <= minimum_vector_similarity <= 1.0:
            raise ValueError("Asset retrieval minimum_vector_similarity must be between -1 and 1")
        if minimum_top1_margin < 0.0:
            raise ValueError("Asset retrieval minimum_top1_margin must be non-negative")
        return cls(
            schema_version=str(data.get("schema_version") or "asset_retrieval_config_v1"),
            rrf_k=rrf_k,
            channel_weights={str(key): float(value) for key, value in weights.items()},
            exact_priority=bool(rules.get("exact_priority", True)),
            materialized_bonus=float(rules.get("materialized_bonus", 0.001)),
            taxonomy_match_bonus=float(rules.get("taxonomy_match_bonus", 0.002)),
            preference_scale=float(rules.get("preference_scale", 0.001)),
            abstention_enabled=bool(abstention.get("enabled", True)),
            abstention_require_no_lexical_hits=bool(abstention.get("require_no_lexical_hits", True)),
            minimum_vector_similarity=minimum_vector_similarity,
            minimum_top1_margin=minimum_top1_margin,
        )


def load_retrieval_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else DEFAULT_RETRIEVAL_CONFIG
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Asset retrieval config must be a JSON object: {config_path}")
    if data.get("schema_version") != "asset_retrieval_config_v1":
        raise ValueError(f"Unsupported asset retrieval config: {config_path}")
    return data


def fuse_ranked_channels(
    channels: Mapping[str, Sequence[RecallHit]],
    *,
    assets: Mapping[str, Mapping[str, Any]],
    intent: SearchIntent,
    config: RankingConfig,
) -> list[dict[str, Any]]:
    fused: dict[str, dict[str, Any]] = {}
    for channel_name in sorted(channels):
        weight = config.channel_weights.get(channel_name, 1.0)
        seen: set[str] = set()
        for one_based_rank, hit in enumerate(channels[channel_name], start=1):
            if hit.asset_id in seen:
                continue
            seen.add(hit.asset_id)
            contribution = weight / (config.rrf_k + one_based_rank)
            row = fused.setdefault(
                hit.asset_id,
                {
                    "asset_id": hit.asset_id,
                    "rrf_score": 0.0,
                    "rule_score": 0.0,
                    "final_score": 0.0,
                    "exact_priority": 0,
                    "channels": {},
                    "rules": {},
                },
            )
            row["rrf_score"] += contribution
            row["channels"][channel_name] = {
                "rank": one_based_rank,
                "weight": weight,
                "rrf_contribution": contribution,
                "raw_score": hit.raw_score,
                "reason": hit.reason,
            }
            if channel_name == "exact":
                row["exact_priority"] = max(int(row["exact_priority"]), 2)
            elif channel_name == "alias":
                row["exact_priority"] = max(int(row["exact_priority"]), 1)

    for asset_id, row in fused.items():
        asset = assets.get(asset_id, {})
        rule_score, rules = deterministic_rule_score(asset, intent, config)
        row["rule_score"] = rule_score
        row["rules"] = rules
        row["final_score"] = float(row["rrf_score"]) + rule_score

    return sorted(
        fused.values(),
        key=lambda row: (
            -int(row["exact_priority"]) if config.exact_priority else 0,
            -float(row["final_score"]),
            str(row["asset_id"]),
        ),
    )


def retrieval_match_decision(
    channels: Mapping[str, Sequence[RecallHit]],
    *,
    config: RankingConfig,
) -> dict[str, Any]:
    lexical_hit_count = sum(len(channels.get(name, ())) for name in ("exact", "alias", "fts"))
    decision: dict[str, Any] = {
        "status": "accepted",
        "reason": "abstention_disabled" if not config.abstention_enabled else "lexical_evidence",
        "lexical_hit_count": lexical_hit_count,
        "thresholds": {
            "minimum_vector_similarity": config.minimum_vector_similarity,
            "minimum_top1_margin": config.minimum_top1_margin,
        },
    }
    if not config.abstention_enabled:
        return decision
    if config.abstention_require_no_lexical_hits and lexical_hit_count:
        return decision

    best_channel: str | None = None
    best_scores: list[float] = []
    for channel_name in ("text_vector", "image_vector"):
        scores = sorted(
            (
                float(hit.raw_score)
                for hit in channels.get(channel_name, ())
                if hit.raw_score is not None and math.isfinite(float(hit.raw_score))
            ),
            reverse=True,
        )
        if scores and (not best_scores or scores[0] > best_scores[0]):
            best_channel = channel_name
            best_scores = scores
    if not best_scores:
        decision.update(status="no_relevant_asset", reason="no_semantic_evidence")
        return decision

    top_similarity = best_scores[0]
    top1_margin = top_similarity - best_scores[1] if len(best_scores) > 1 else None
    decision.update(
        vector_channel=best_channel,
        top_vector_similarity=top_similarity,
        top1_margin=top1_margin,
    )
    if top_similarity < config.minimum_vector_similarity:
        decision.update(status="no_relevant_asset", reason="vector_similarity_below_threshold")
    elif top1_margin is not None and top1_margin < config.minimum_top1_margin:
        decision.update(status="ambiguous_candidates", reason="top1_margin_below_threshold")
    else:
        decision.update(status="accepted", reason="vector_evidence")
    return decision


def deterministic_rule_score(
    asset: Mapping[str, Any], intent: SearchIntent, config: RankingConfig
) -> tuple[float, dict[str, float]]:
    rules: dict[str, float] = {}
    if asset.get("materialized"):
        rules["materialized"] = config.materialized_bonus

    category_values = {
        str(asset.get(key) or "").casefold()
        for key in ("category", "category_l1", "category_l2")
        if asset.get(key)
    }
    taxonomy_matches = sum(
        1
        for key in ("domain", "category", "subcategory", "object_type")
        if str(intent.taxonomy.get(key) or "").casefold() in category_values
    )
    if taxonomy_matches:
        rules["taxonomy"] = taxonomy_matches * config.taxonomy_match_bonus

    searchable = _searchable_text(asset)
    preference_total = 0.0
    for preference in intent.should:
        values = preference.value if isinstance(preference.value, list) else [preference.value]
        if any(str(value).casefold() in searchable for value in values):
            preference_total += preference.weight * preference.confidence
    if preference_total:
        rules["preferences"] = preference_total * config.preference_scale
    return sum(rules.values()), rules


def _searchable_text(asset: Mapping[str, Any]) -> str:
    values: list[str] = []
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
        "source_kind",
    ):
        value = asset.get(key)
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item) for item in value)
        elif value is not None:
            values.append(str(value))
    return " ".join(values).casefold()
