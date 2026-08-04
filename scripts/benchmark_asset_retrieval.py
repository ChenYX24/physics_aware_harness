#!/usr/bin/env python3
"""Run a deterministic offline benchmark against an Asset Catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.assets.asset_registry import AssetRegistry
from harness.assets.embedding_index import stable_digest
from harness.assets.hybrid_ranking import DEFAULT_RETRIEVAL_CONFIG
from harness.assets.search_intent import SearchIntent
from harness.assets.sqlite_catalog import default_catalog_path


DEFAULT_QUERIES = ROOT / "tests" / "fixtures" / "asset_retrieval_benchmark_v1.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-path", default=str(default_catalog_path()))
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--config", default=str(DEFAULT_RETRIEVAL_CONFIG))
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--output", default=None)
    return parser.parse_args()


def run_benchmark(
    registry: AssetRegistry,
    queries: list[dict[str, Any]],
    *,
    top_k: int,
    repeat: int,
) -> dict[str, Any]:
    if top_k <= 0 or repeat <= 0:
        raise ValueError("top_k and repeat must be positive")
    records: list[dict[str, Any]] = []
    latencies_ms: list[float] = []
    reciprocal_ranks: list[float] = []
    recall_hits = 0
    positive_evaluated = 0
    no_result_evaluated = 0
    no_result_hits = 0
    for query in queries:
        intent = SearchIntent.from_dict(query["search_intent"])
        detailed: dict[str, Any] | None = None
        timings: list[float] = []
        for _ in range(repeat):
            started = time.perf_counter()
            detailed = registry.search_detailed(intent, top_k=top_k)
            timings.append((time.perf_counter() - started) * 1000.0)
        assert detailed is not None
        ids = [str(entry["asset"].get("asset_id") or entry["asset"].get("id")) for entry in detailed["results"]]
        expected = {str(value) for value in query.get("expected_asset_ids") or []}
        expect_no_results = bool(query.get("expect_no_results"))
        reciprocal_rank = 0.0
        no_result_correct: bool | None = None
        if expected:
            positive_evaluated += 1
            first_rank = next((index + 1 for index, asset_id in enumerate(ids) if asset_id in expected), None)
            if first_rank:
                recall_hits += 1
                reciprocal_rank = 1.0 / first_rank
            reciprocal_ranks.append(reciprocal_rank)
        elif expect_no_results:
            no_result_evaluated += 1
            expected_status = str(query.get("expected_match_status") or "no_relevant_asset")
            actual_status = str((detailed["retrieval"].get("match_decision") or {}).get("status") or "")
            no_result_correct = not ids and actual_status == expected_status
            if no_result_correct:
                no_result_hits += 1
        median_ms = statistics.median(timings)
        latencies_ms.append(median_ms)
        records.append(
            {
                "query_id": query["query_id"],
                "result_asset_ids": ids,
                "median_latency_ms": median_ms,
                "reciprocal_rank": reciprocal_rank if expected else None,
                "no_result_correct": no_result_correct,
                "retrieval": detailed["retrieval"],
            }
        )
    result_digest = hashlib.sha256(
        json.dumps(
            [(row["query_id"], row["result_asset_ids"]) for row in records],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    report = {
        "schema_version": "asset_retrieval_benchmark_v1",
        "query_count": len(records),
        "evaluated_query_count": positive_evaluated + no_result_evaluated,
        "positive_evaluated_query_count": positive_evaluated,
        "no_result_evaluated_query_count": no_result_evaluated,
        "top_k": top_k,
        "repeat": repeat,
        "recall_at_k": recall_hits / positive_evaluated if positive_evaluated else None,
        "mean_reciprocal_rank": statistics.fmean(reciprocal_ranks) if reciprocal_ranks else None,
        "no_result_accuracy": no_result_hits / no_result_evaluated if no_result_evaluated else None,
        "median_latency_ms": statistics.median(latencies_ms) if latencies_ms else 0.0,
        "max_latency_ms": max(latencies_ms, default=0.0),
        "result_digest": result_digest,
        "queries": records,
    }
    catalog = getattr(registry, "_sqlite", None)
    if catalog is not None:
        with catalog.connect() as connection:
            report["catalog"] = {
                "path": str(catalog.path),
                "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
                "asset_count": int(connection.execute("SELECT count(*) FROM assets").fetchone()[0]),
            }
        report["ranking_config"] = {
            "schema_version": catalog.ranking_config.schema_version,
            "digest": stable_digest(catalog.retrieval_config),
        }
        report["vector_index"] = catalog.vector_index_status()
    return report


def main() -> None:
    args = parse_args()
    payload = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    queries = payload.get("queries") if isinstance(payload, dict) else None
    if not isinstance(queries, list):
        raise ValueError("Benchmark fixture requires a queries list")
    registry = AssetRegistry(args.catalog_path, retrieval_config_path=args.config)
    report = run_benchmark(registry, queries, top_k=args.top_k, repeat=args.repeat)
    text = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
