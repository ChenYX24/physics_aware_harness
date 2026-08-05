from __future__ import annotations

import json
import os
import re
import sqlite3
import struct
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from harness.assets.embedding_index import (
    EmbeddingProvider,
    OpenCLIPEmbeddingProvider,
    normalize_vector,
    preview_paths,
    semantic_document,
    sha256_file,
    sha256_text,
    stable_digest,
)
from harness.assets.hybrid_ranking import (
    RankingConfig,
    RecallHit,
    fuse_ranked_channels,
    load_retrieval_config,
    retrieval_match_decision,
)
from harness.assets.search_intent import SearchIntent, asset_matches_approx_size, taxonomy_relaxation_values


CATALOG_SCHEMA_VERSION = 2
DEFAULT_WORKSPACE = Path.home() / "SimulatorWorkspace" / "physics_aware_harness"


def default_catalog_path() -> Path:
    workspace = Path(os.environ.get("SIM_HARNESS_WORKSPACE", DEFAULT_WORKSPACE))
    return workspace / "catalog" / "assets" / "catalog.sqlite"


def initialize_catalog(path: str | Path) -> SQLiteCatalog:
    catalog_path = Path(path)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(catalog_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _apply_migrations(connection)
    finally:
        connection.close()
    return SQLiteCatalog(catalog_path)


class SQLiteCatalog:
    def __init__(
        self,
        path: str | Path,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        retrieval_config_path: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.embedding_provider = embedding_provider
        self.retrieval_config = load_retrieval_config(retrieval_config_path)
        self.ranking_config = RankingConfig.from_mapping(self.retrieval_config)
        self._provider_error: str | None = None
        if not self.path.is_file():
            raise FileNotFoundError(f"Asset catalog does not exist: {self.path}")
        with closing(self.connect()) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version < CATALOG_SCHEMA_VERSION:
                _apply_migrations(connection)
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != CATALOG_SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported asset catalog schema version {version}; expected {CATALOG_SCHEMA_VERSION}: {self.path}"
            )

    def connect(self, *, load_vector_extension: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if load_vector_extension:
            _load_sqlite_vec(connection)
        return connection

    def is_writable(self) -> bool:
        try:
            with closing(self.connect()) as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                connection.execute(f"PRAGMA user_version = {version}")
                connection.rollback()
        except (OSError, sqlite3.Error):
            return False
        return True

    def import_registry(self, payload: Mapping[str, Any] | list[dict[str, Any]]) -> dict[str, int]:
        rows = _registry_rows(payload)
        imported = 0
        changed = 0
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for raw in rows:
                asset = _normalize_asset(raw)
                asset_id = str(asset.get("asset_id") or "").strip()
                if not asset_id:
                    continue
                if _upsert_asset(connection, asset):
                    changed += 1
                imported += 1
            if changed:
                connection.execute("UPDATE vector_index_state SET status = 'stale'")
            connection.commit()
            total = int(connection.execute("SELECT count(*) FROM assets").fetchone()[0])
        return {
            "imported_count": imported,
            "changed_count": changed,
            "catalog_asset_count": total,
            "schema_version": CATALOG_SCHEMA_VERSION,
        }

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT row_json FROM assets WHERE asset_id = ?",
                (str(asset_id),),
            ).fetchone()
        return json.loads(str(row["row_json"])) if row else None

    def search(self, intent: SearchIntent, *, top_k: int = 5) -> list[dict[str, Any]]:
        return [entry["asset"] for entry in self.search_detailed(intent, top_k=top_k)["results"]]

    def search_detailed(self, intent: SearchIntent, *, top_k: int = 5) -> dict[str, Any]:
        if top_k <= 0:
            return {
                "results": [],
                "retrieval": {"eligible_count": 0, "channels": {}, "vector_status": "not_requested"},
            }
        abstained: dict[str, Any] | None = None
        for requested_category in taxonomy_relaxation_values(intent):
            detailed = self._search_one_category(intent, top_k=top_k, requested_category=requested_category)
            if detailed["results"]:
                return detailed
            if detailed["retrieval"].get("match_decision", {}).get("status") == "no_relevant_asset":
                abstained = detailed
        if abstained is not None:
            return abstained
        return {
            "results": [],
            "retrieval": {"eligible_count": 0, "channels": {}, "vector_status": "no_candidates"},
        }

    def _search_one_category(
        self,
        intent: SearchIntent,
        *,
        top_k: int,
        requested_category: str | None,
    ) -> dict[str, Any]:
        where_sql, parameters = _hard_filter_sql(intent, requested_category=requested_category)
        query = normalize_search_value(intent.raw_query)
        channels: dict[str, list[RecallHit]] = {"exact": [], "alias": [], "fts": []}
        with closing(self.connect()) as connection:
            if "approx_size_m" in intent.must:
                size_rows = connection.execute(
                    f"SELECT a.asset_id, a.row_json FROM assets a WHERE {where_sql}",
                    parameters,
                ).fetchall()
                eligible_ids = [
                    str(row["asset_id"])
                    for row in size_rows
                    if asset_matches_approx_size(json.loads(str(row["row_json"])), intent)
                ]
                if eligible_ids:
                    placeholders = ",".join("?" for _ in eligible_ids)
                    candidate_where = f"a.asset_id IN ({placeholders})"
                    candidate_parameters: list[Any] = eligible_ids
                else:
                    candidate_where = "0 = 1"
                    candidate_parameters = []
                eligible_count = len(eligible_ids)
            else:
                candidate_where = where_sql
                candidate_parameters = list(parameters)
                eligible_count = int(
                    connection.execute(
                        f"SELECT count(*) FROM assets a WHERE {candidate_where}",
                        candidate_parameters,
                    ).fetchone()[0]
                )
            if eligible_count == 0:
                return {
                    "results": [],
                    "retrieval": {
                        "category": requested_category,
                        "eligible_count": 0,
                        "channels": {},
                        "vector_status": "hard_filter_empty",
                    },
                }
            if query:
                exact_rows = connection.execute(
                    f"""
                    SELECT a.asset_id,
                           CASE
                             WHEN a.normalized_id = ? THEN 0
                             WHEN a.normalized_name = ? THEN 1
                             ELSE 2
                           END AS exact_rank
                    FROM assets a
                    WHERE {candidate_where}
                      AND (a.normalized_id = ? OR a.normalized_name = ?)
                    ORDER BY exact_rank, a.asset_id
                    """,
                    [query, query, *candidate_parameters, query, query],
                ).fetchall()
                channels["exact"] = [
                    RecallHit(
                        str(row["asset_id"]),
                        raw_score=float(row["exact_rank"]),
                        reason="asset_id" if int(row["exact_rank"]) == 0 else "name",
                    )
                    for row in exact_rows
                ]
                alias_rows = connection.execute(
                    f"""
                    SELECT aa.asset_id
                    FROM asset_aliases aa
                    JOIN assets a ON a.asset_id = aa.asset_id
                    WHERE {candidate_where} AND aa.normalized_alias = ?
                    ORDER BY aa.asset_id
                    """,
                    [*candidate_parameters, query],
                ).fetchall()
                channels["alias"] = [
                    RecallHit(str(row["asset_id"]), raw_score=0.0, reason="alias") for row in alias_rows
                ]
                fts_query = _fts_query(intent.semantic_text or intent.raw_query)
                if fts_query:
                    fts_rows = connection.execute(
                        f"""
                        SELECT a.asset_id, bm25(asset_search_fts) AS fts_score
                        FROM asset_search_fts
                        JOIN assets a ON a.asset_id = asset_search_fts.asset_id
                        WHERE asset_search_fts MATCH ? AND {candidate_where}
                        ORDER BY fts_score, a.asset_id
                        LIMIT ?
                        """,
                        [fts_query, *candidate_parameters, max(top_k * 16, top_k)],
                    ).fetchall()
                    channels["fts"] = [
                        RecallHit(str(row["asset_id"]), raw_score=float(row["fts_score"]), reason="fts5_bm25")
                        for row in fts_rows
                    ]

        vector_status: dict[str, Any] = {}
        requested_vector_statuses: list[dict[str, Any]] = []
        text_hits, text_status = self._vector_recall(
            intent.semantic_text or intent.raw_query,
            modality="text",
            candidate_where=candidate_where,
            candidate_parameters=candidate_parameters,
            top_k=max(top_k * 16, top_k),
        )
        if text_hits:
            channels["text_vector"] = text_hits
        vector_status["text"] = text_status
        requested_vector_statuses.append(text_status)
        if intent.reference_image:
            image_hits, image_status = self._vector_recall(
                intent.reference_image,
                modality="image",
                candidate_where=candidate_where,
                candidate_parameters=candidate_parameters,
                top_k=max(top_k * 16, top_k),
                query_is_image=True,
            )
            if image_hits:
                channels["image_vector"] = image_hits
            vector_status["image"] = image_status
            requested_vector_statuses.append(image_status)
        vector_status["status"] = _aggregate_vector_status(requested_vector_statuses)

        non_empty_channels = {name: hits for name, hits in channels.items() if hits}
        match_decision = retrieval_match_decision(non_empty_channels, config=self.ranking_config)
        if match_decision["status"] == "no_relevant_asset":
            return {
                "results": [],
                "retrieval": {
                    "category": requested_category,
                    "eligible_count": eligible_count,
                    "channels": {name: len(hits) for name, hits in non_empty_channels.items()},
                    "vector_status": vector_status,
                    "ranking_config": self.ranking_config.schema_version,
                    "match_decision": match_decision,
                },
            }
        recalled_ids = sorted({hit.asset_id for hits in non_empty_channels.values() for hit in hits})
        assets: dict[str, dict[str, Any]] = {}
        if recalled_ids:
            placeholders = ",".join("?" for _ in recalled_ids)
            with closing(self.connect()) as connection:
                rows = connection.execute(
                    f"SELECT asset_id, row_json FROM assets WHERE asset_id IN ({placeholders})",
                    recalled_ids,
                ).fetchall()
            assets = {str(row["asset_id"]): json.loads(str(row["row_json"])) for row in rows}
        ranked = fuse_ranked_channels(
            non_empty_channels,
            assets=assets,
            intent=intent,
            config=self.ranking_config,
        )
        if not ranked and (_filterable_category(requested_category) or not query):
            with closing(self.connect()) as connection:
                fallback_rows = connection.execute(
                    f"""
                    SELECT a.asset_id, a.row_json
                    FROM assets a
                    WHERE {candidate_where}
                    ORDER BY a.materialized DESC, a.asset_id
                    LIMIT ?
                    """,
                    [*candidate_parameters, max(top_k * 4, top_k)],
                ).fetchall()
            assets = {str(row["asset_id"]): json.loads(str(row["row_json"])) for row in fallback_rows}
            ranked = [
                {
                    "asset_id": str(row["asset_id"]),
                    "rrf_score": 0.0,
                    "rule_score": 0.0,
                    "final_score": 0.0,
                    "exact_priority": 0,
                    "channels": {"category_fallback": {"rank": index + 1}},
                    "rules": {},
                }
                for index, row in enumerate(fallback_rows)
            ]
        selected = ranked[:top_k]
        return {
            "results": [{"asset": assets[row["asset_id"]], "score": row} for row in selected],
            "retrieval": {
                "category": requested_category,
                "eligible_count": eligible_count,
                "channels": {name: len(hits) for name, hits in non_empty_channels.items()},
                "vector_status": vector_status,
                "ranking_config": self.ranking_config.schema_version,
                "match_decision": match_decision,
            },
        }

    def _vector_recall(
        self,
        query: str,
        *,
        modality: str,
        candidate_where: str,
        candidate_parameters: list[Any],
        top_k: int,
        query_is_image: bool = False,
    ) -> tuple[list[RecallHit], dict[str, Any]]:
        state = self.vector_index_status()
        if not state or state.get("status") != "ready":
            return [], {"status": "not_ready" if not state else str(state.get("status"))}
        provider = self._embedding_provider_for_search()
        if provider is None:
            return [], {"status": "provider_unavailable", "reason": self._provider_error}
        if provider.spec.model_id != state.get("model_id"):
            return [], {
                "status": "model_mismatch",
                "expected": state.get("model_id"),
                "actual": provider.spec.model_id,
            }
        try:
            if query_is_image:
                query_path = Path(query)
                if not query_path.is_file():
                    return [], {"status": "reference_image_missing", "path": str(query_path)}
                vector = provider.encode_images([query_path])[0]
            else:
                vector = provider.encode_texts([query])[0]
            normalized = normalize_vector(vector, dimension=provider.spec.dimension)
            blob = _serialize_float32(normalized)
            table_name = _safe_identifier(str(state["table_name"]))
            with closing(self.connect(load_vector_extension=True)) as connection:
                rows = connection.execute(
                    f"""
                    SELECT v.asset_id, MIN(vec_distance_cosine(v.embedding, ?)) AS distance
                    FROM {table_name} v
                    JOIN assets a ON a.asset_id = v.asset_id
                    WHERE v.modality = ? AND v.model_id = ? AND {candidate_where}
                    GROUP BY v.asset_id
                    ORDER BY distance, v.asset_id
                    LIMIT ?
                    """,
                    [blob, modality, provider.spec.model_id, *candidate_parameters, top_k],
                ).fetchall()
        except Exception as exc:
            return [], {"status": "query_failed", "reason": f"{type(exc).__name__}: {exc}"}
        return (
            [
                RecallHit(
                    str(row["asset_id"]),
                    raw_score=1.0 - float(row["distance"]),
                    reason=f"{modality}_cosine",
                )
                for row in rows
            ],
            {"status": "ready", "model_id": provider.spec.model_id, "result_count": len(rows)},
        )

    def _embedding_provider_for_search(self) -> EmbeddingProvider | None:
        if self.embedding_provider is not None:
            return self.embedding_provider
        if self._provider_error:
            return None
        try:
            self.embedding_provider = OpenCLIPEmbeddingProvider.from_config(
                self.retrieval_config,
                workspace=_workspace_root(self.path),
                allow_download=False,
            )
        except Exception as exc:
            self._provider_error = f"{type(exc).__name__}: {exc}"
            return None
        return self.embedding_provider

    def vector_index_status(self) -> dict[str, Any] | None:
        table_name = str(
            (self.retrieval_config.get("vector_index") or {}).get("table_name")
            or "asset_embedding_vec_v1"
        )
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM vector_index_state WHERE table_name = ?",
                (table_name,),
            ).fetchone()
        return dict(row) if row else None

    def rebuild_vector_index(
        self,
        provider: EmbeddingProvider,
        *,
        include_images: bool = True,
        batch_size: int = 32,
        force: bool = False,
    ) -> dict[str, Any]:
        if batch_size <= 0:
            raise ValueError("Embedding batch_size must be positive")
        spec = provider.spec
        if spec.dimension <= 0:
            raise ValueError("Embedding dimension must be positive")
        table_name = _safe_identifier(
            str((self.retrieval_config.get("vector_index") or {}).get("table_name") or "asset_embedding_vec_v1")
        )
        distance_metric = str(
            (self.retrieval_config.get("vector_index") or {}).get("distance_metric") or "cosine"
        )
        if distance_metric not in {"cosine", "l2"}:
            raise ValueError(f"Unsupported sqlite-vec distance metric: {distance_metric}")

        with closing(self.connect(load_vector_extension=True)) as connection:
            sqlite_vec_version = str(connection.execute("SELECT vec_version()").fetchone()[0])
            expected_vec_version = str(
                (self.retrieval_config.get("vector_index") or {}).get("sqlite_vec_runtime_version") or ""
            )
            if expected_vec_version and sqlite_vec_version != expected_vec_version:
                raise RuntimeError(
                    f"sqlite-vec version mismatch: expected {expected_vec_version}, got {sqlite_vec_version}"
                )
            asset_rows = connection.execute("SELECT asset_id, row_json FROM assets ORDER BY asset_id").fetchall()
            existing_rows = connection.execute(
                """
                SELECT asset_id, modality, source_uri, source_sha256, embedding
                FROM asset_embeddings WHERE model_id = ?
                """,
                (spec.model_id,),
            ).fetchall()
        existing = {
            (str(row["asset_id"]), str(row["modality"]), str(row["source_uri"])): {
                "source_sha256": str(row["source_sha256"]),
                "embedding": bytes(row["embedding"]),
            }
            for row in existing_rows
        }

        expected: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in asset_rows:
            asset_id = str(row["asset_id"])
            asset = json.loads(str(row["row_json"]))
            document = semantic_document(asset, document_version=spec.document_version)
            expected[(asset_id, "text", "semantic_document")] = {
                "asset_id": asset_id,
                "modality": "text",
                "source_uri": "semantic_document",
                "source_sha256": sha256_text(document),
                "value": document,
            }
            if include_images:
                for role, path in preview_paths(asset):
                    digest = sha256_file(path)
                    if digest:
                        expected[(asset_id, "image", f"preview:{role}")] = {
                            "asset_id": asset_id,
                            "modality": "image",
                            "source_uri": f"preview:{role}",
                            "source_sha256": digest,
                            "value": path,
                        }

        pending_text = [
            entry
            for key, entry in expected.items()
            if entry["modality"] == "text"
            and (force or key not in existing or existing[key]["source_sha256"] != entry["source_sha256"])
        ]
        pending_images = [
            entry
            for key, entry in expected.items()
            if entry["modality"] == "image"
            and (force or key not in existing or existing[key]["source_sha256"] != entry["source_sha256"])
        ]
        encoded: dict[tuple[str, str, str], bytes] = {
            key: value["embedding"]
            for key, value in existing.items()
            if key in expected and not force and value["source_sha256"] == expected[key]["source_sha256"]
        }
        for batch in _batches(pending_text, batch_size):
            vectors = provider.encode_texts([str(entry["value"]) for entry in batch])
            if len(vectors) != len(batch):
                raise RuntimeError("Embedding provider returned the wrong number of text vectors")
            for entry, vector in zip(batch, vectors):
                key = (entry["asset_id"], entry["modality"], entry["source_uri"])
                encoded[key] = _serialize_float32(normalize_vector(vector, dimension=spec.dimension))
        for batch in _batches(pending_images, batch_size):
            vectors = provider.encode_images([Path(entry["value"]) for entry in batch])
            if len(vectors) != len(batch):
                raise RuntimeError("Embedding provider returned the wrong number of image vectors")
            for entry, vector in zip(batch, vectors):
                key = (entry["asset_id"], entry["modality"], entry["source_uri"])
                encoded[key] = _serialize_float32(normalize_vector(vector, dimension=spec.dimension))
        resolved_spec = provider.spec
        if resolved_spec.dimension != spec.dimension:
            raise RuntimeError("Embedding provider dimension changed while loading the model")
        spec = resolved_spec
        missing = sorted(set(expected).difference(encoded))
        if missing:
            raise RuntimeError(f"Embedding rebuild left {len(missing)} entries without vectors")

        index_digest_payload = [
            {
                "asset_id": entry["asset_id"],
                "modality": entry["modality"],
                "source_uri": entry["source_uri"],
                "source_sha256": entry["source_sha256"],
            }
            for _, entry in sorted(expected.items())
        ]
        index_digest = stable_digest(
            {"model": spec.to_dict(), "entries": index_digest_payload, "distance_metric": distance_metric}
        )
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.connect(load_vector_extension=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO embedding_models(
                    model_id, provider, model_name, pretrained, dimension, document_version,
                    library_version, checkpoint_sha256, model_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_id) DO UPDATE SET
                    library_version=excluded.library_version,
                    checkpoint_sha256=excluded.checkpoint_sha256,
                    model_json=excluded.model_json
                """,
                (
                    spec.model_id,
                    spec.provider,
                    spec.model_name,
                    spec.pretrained,
                    spec.dimension,
                    spec.document_version,
                    spec.library_version,
                    spec.checkpoint_sha256,
                    json.dumps(spec.to_dict(), sort_keys=True, ensure_ascii=False),
                ),
            )
            connection.execute("DELETE FROM asset_embeddings WHERE model_id = ?", (spec.model_id,))
            connection.executemany(
                """
                INSERT INTO asset_embeddings(
                    asset_id, modality, model_id, source_uri, source_sha256, dimension, embedding, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        entry["asset_id"],
                        entry["modality"],
                        spec.model_id,
                        entry["source_uri"],
                        entry["source_sha256"],
                        spec.dimension,
                        encoded[key],
                        now,
                    )
                    for key, entry in sorted(expected.items())
                ],
            )
            connection.execute(f"DROP TABLE IF EXISTS {table_name}")
            connection.execute(
                f"""
                CREATE VIRTUAL TABLE {table_name} USING vec0(
                    embedding float[{spec.dimension}] distance_metric={distance_metric},
                    asset_id text,
                    modality text partition key,
                    model_id text partition key
                )
                """
            )
            rows = connection.execute(
                """
                SELECT embedding_id, embedding, asset_id, modality, model_id
                FROM asset_embeddings WHERE model_id = ? ORDER BY embedding_id
                """,
                (spec.model_id,),
            ).fetchall()
            connection.executemany(
                f"INSERT INTO {table_name}(rowid, embedding, asset_id, modality, model_id) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        int(row["embedding_id"]),
                        bytes(row["embedding"]),
                        str(row["asset_id"]),
                        str(row["modality"]),
                        str(row["model_id"]),
                    )
                    for row in rows
                ],
            )
            connection.execute(
                """
                INSERT INTO vector_index_state(
                    index_name, table_name, model_id, dimension, distance_metric, row_count,
                    source_digest, sqlite_vec_version, status, rebuilt_at, config_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)
                ON CONFLICT(index_name) DO UPDATE SET
                    table_name=excluded.table_name, model_id=excluded.model_id,
                    dimension=excluded.dimension, distance_metric=excluded.distance_metric,
                    row_count=excluded.row_count, source_digest=excluded.source_digest,
                    sqlite_vec_version=excluded.sqlite_vec_version, status='ready',
                    rebuilt_at=excluded.rebuilt_at, config_json=excluded.config_json
                """,
                (
                    table_name,
                    table_name,
                    spec.model_id,
                    spec.dimension,
                    distance_metric,
                    len(rows),
                    index_digest,
                    sqlite_vec_version,
                    now,
                    json.dumps(self.retrieval_config, sort_keys=True, ensure_ascii=False),
                ),
            )
            connection.commit()
        self.embedding_provider = provider
        self._provider_error = None
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "model": spec.to_dict(),
            "sqlite_vec_version": sqlite_vec_version,
            "asset_count": len(asset_rows),
            "embedding_count": len(expected),
            "encoded_count": len(pending_text) + len(pending_images),
            "reused_count": len(expected) - len(pending_text) - len(pending_images),
            "text_embedding_count": sum(1 for entry in expected.values() if entry["modality"] == "text"),
            "image_embedding_count": sum(1 for entry in expected.values() if entry["modality"] == "image"),
            "source_digest": index_digest,
            "table_name": table_name,
        }


def _apply_migrations(connection: sqlite3.Connection) -> None:
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > CATALOG_SCHEMA_VERSION:
        raise RuntimeError(f"Asset catalog schema {current} is newer than supported {CATALOG_SCHEMA_VERSION}")
    if current < 1:
        try:
            connection.executescript(
                """
                BEGIN;
                CREATE TABLE IF NOT EXISTS catalog_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY,
                    normalized_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    semantic_name TEXT,
                    description TEXT,
                    category_l1 TEXT,
                    category_l2 TEXT,
                    asset_type TEXT,
                    collider TEXT,
                    collision_profile TEXT,
                    lifecycle_status TEXT,
                    materialized INTEGER NOT NULL DEFAULT 0,
                    quality_status TEXT,
                    license_tier TEXT,
                    row_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_assets_category ON assets(category_l1, category_l2);
                CREATE INDEX IF NOT EXISTS idx_assets_type ON assets(asset_type);
                CREATE INDEX IF NOT EXISTS idx_assets_materialized ON assets(materialized);
                CREATE TABLE IF NOT EXISTS asset_aliases (
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    PRIMARY KEY(asset_id, normalized_alias)
                );
                CREATE INDEX IF NOT EXISTS idx_alias_lookup ON asset_aliases(normalized_alias);
                CREATE TABLE IF NOT EXISTS asset_tags (
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    tag TEXT NOT NULL,
                    normalized_tag TEXT NOT NULL,
                    PRIMARY KEY(asset_id, normalized_tag)
                );
                CREATE TABLE IF NOT EXISTS asset_files (
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    file_role TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    file_format TEXT,
                    sha256 TEXT,
                    version TEXT,
                    byte_size INTEGER,
                    materialized INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(asset_id, file_role, local_path)
                );
                CREATE TABLE IF NOT EXISTS asset_sources (
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    source_kind TEXT,
                    source_uri TEXT NOT NULL,
                    author TEXT,
                    license_name TEXT,
                    license_tier TEXT,
                    provenance_json TEXT,
                    PRIMARY KEY(asset_id, source_uri)
                );
                CREATE TABLE IF NOT EXISTS asset_features (
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    feature_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    PRIMARY KEY(asset_id, feature_key)
                );
                CREATE TABLE IF NOT EXISTS asset_dependencies (
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    dependency_id TEXT NOT NULL,
                    dependency_kind TEXT,
                    local_path TEXT,
                    materialized INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(asset_id, dependency_id)
                );
                CREATE TABLE IF NOT EXISTS backend_bindings (
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    backend TEXT NOT NULL,
                    object_path TEXT,
                    class_name TEXT,
                    materialized INTEGER NOT NULL DEFAULT 0,
                    runtime_ready INTEGER NOT NULL DEFAULT 0,
                    binding_json TEXT NOT NULL,
                    PRIMARY KEY(asset_id, backend)
                );
                CREATE INDEX IF NOT EXISTS idx_binding_ready ON backend_bindings(backend, runtime_ready);
                CREATE VIRTUAL TABLE IF NOT EXISTS asset_search_fts USING fts5(
                    asset_id UNINDEXED,
                    name,
                    aliases,
                    tags,
                    description,
                    taxonomy,
                    object_path,
                    tokenize = 'unicode61 remove_diacritics 2'
                );
                INSERT OR IGNORE INTO catalog_migrations(version) VALUES (1);
                PRAGMA user_version = 1;
                COMMIT;
                """
            )
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if "fts5" in str(exc).casefold():
                raise RuntimeError("SQLite FTS5 support is required for the Asset Catalog") from exc
            raise
    if current < 2:
        connection.executescript(
            """
            BEGIN;
            CREATE TABLE IF NOT EXISTS embedding_models (
                model_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                model_name TEXT NOT NULL,
                pretrained TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                document_version TEXT NOT NULL,
                library_version TEXT NOT NULL,
                checkpoint_sha256 TEXT,
                model_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS asset_embeddings (
                embedding_id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                modality TEXT NOT NULL CHECK(modality IN ('text', 'image')),
                model_id TEXT NOT NULL REFERENCES embedding_models(model_id) ON DELETE CASCADE,
                source_uri TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                embedding BLOB NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(asset_id, modality, model_id, source_uri)
            );
            CREATE INDEX IF NOT EXISTS idx_asset_embeddings_lookup
                ON asset_embeddings(model_id, modality, asset_id);
            CREATE TABLE IF NOT EXISTS vector_index_state (
                index_name TEXT PRIMARY KEY,
                table_name TEXT NOT NULL UNIQUE,
                model_id TEXT NOT NULL REFERENCES embedding_models(model_id),
                dimension INTEGER NOT NULL,
                distance_metric TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                source_digest TEXT NOT NULL,
                sqlite_vec_version TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ready', 'stale', 'building', 'failed')),
                rebuilt_at TEXT NOT NULL,
                config_json TEXT NOT NULL
            );
            INSERT OR IGNORE INTO catalog_migrations(version) VALUES (2);
            PRAGMA user_version = 2;
            COMMIT;
            """
        )


def _registry_rows(payload: Mapping[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, Mapping)]
    defaults = payload.get("provenance_defaults") if isinstance(payload.get("provenance_defaults"), Mapping) else {}
    for key in ("assets", "items", "entries"):
        values = payload.get(key)
        if isinstance(values, list):
            return [{**defaults, **dict(row)} for row in values if isinstance(row, Mapping)]
    return []


def _normalize_asset(item: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    paths = normalized.get("paths") if isinstance(normalized.get("paths"), Mapping) else {}
    ue = normalized.get("ue") if isinstance(normalized.get("ue"), Mapping) else {}
    physics = normalized.get("physics") if isinstance(normalized.get("physics"), Mapping) else {}
    normalized.setdefault("asset_id", normalized.get("id") or normalized.get("name"))
    normalized.setdefault("ue_path", paths.get("ue5") or ue.get("object_path"))
    normalized.setdefault("category", normalized.get("category_l1"))
    normalized.setdefault("type", normalized.get("asset_kind") or ue.get("class_name"))
    normalized.setdefault("thumbnail", paths.get("thumbnail"))
    normalized.setdefault("mass_kg", physics.get("estimated_mass_kg"))
    normalized.setdefault("collision_profile", physics.get("collision_profile"))
    normalized.setdefault("collider", physics.get("collider"))
    if not isinstance(normalized.get("material"), Mapping) and isinstance(physics.get("material_properties"), Mapping):
        normalized["material"] = dict(physics["material_properties"])
    if not normalized.get("source_uri") and normalized.get("source_kind") == "engine_builtin" and normalized.get("ue_path"):
        normalized["source_uri"] = f"ue://{str(normalized['ue_path']).lstrip('/')}"
    return normalized


def _upsert_asset(connection: sqlite3.Connection, asset: dict[str, Any]) -> bool:
    asset_id = str(asset["asset_id"])
    name = str(asset.get("name") or asset_id)
    source_kind = str(asset.get("source_kind") or "")
    license_name = str(asset.get("license") or "")
    license_tier = effective_license_tier(
        license_name,
        asset.get("quality_status"),
        declared_tier=asset.get("license_tier"),
        source_kind=source_kind,
        redistribution=asset.get("redistribution") or (asset.get("release_audit") or {}).get("redistribution"),
    )
    row_json = json.dumps(asset, sort_keys=True, ensure_ascii=False)
    previous = connection.execute("SELECT row_json FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
    changed = previous is None or str(previous[0]) != row_json
    connection.execute(
        """
        INSERT INTO assets(
            asset_id, normalized_id, name, normalized_name, semantic_name, description,
            category_l1, category_l2, asset_type, collider, collision_profile,
            lifecycle_status, materialized, quality_status, license_tier, row_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset_id) DO UPDATE SET
            normalized_id=excluded.normalized_id, name=excluded.name,
            normalized_name=excluded.normalized_name, semantic_name=excluded.semantic_name,
            description=excluded.description, category_l1=excluded.category_l1,
            category_l2=excluded.category_l2, asset_type=excluded.asset_type,
            collider=excluded.collider, collision_profile=excluded.collision_profile,
            lifecycle_status=excluded.lifecycle_status, materialized=excluded.materialized,
            quality_status=excluded.quality_status, license_tier=excluded.license_tier,
            row_json=excluded.row_json
        """,
        (
            asset_id,
            normalize_search_value(asset_id),
            name,
            normalize_search_value(name),
            str(asset.get("semantic_name") or ""),
            str(asset.get("description") or ""),
            str(asset.get("category_l1") or asset.get("category") or ""),
            str(asset.get("category_l2") or ""),
            str(asset.get("type") or asset.get("asset_kind") or ""),
            str(asset.get("collider") or ""),
            str(asset.get("collision_profile") or ""),
            str(asset.get("lifecycle_status") or (asset.get("acquisition") or {}).get("status") or ""),
            int(bool(asset.get("materialized"))),
            str(asset.get("quality_status") or ""),
            license_tier,
            row_json,
        ),
    )
    for table in (
        "asset_aliases",
        "asset_tags",
        "asset_files",
        "asset_sources",
        "asset_features",
        "asset_dependencies",
        "backend_bindings",
    ):
        connection.execute(f"DELETE FROM {table} WHERE asset_id = ?", (asset_id,))
    connection.execute("DELETE FROM asset_search_fts WHERE asset_id = ?", (asset_id,))

    aliases = _unique_strings([asset.get("name"), asset.get("semantic_name"), *(asset.get("aliases") or [])])
    tags = _unique_strings([*(asset.get("tags") or []), *(asset.get("usage_groups") or [])])
    connection.executemany(
        "INSERT INTO asset_aliases(asset_id, alias, normalized_alias) VALUES (?, ?, ?)",
        [(asset_id, alias, normalize_search_value(alias)) for alias in aliases],
    )
    connection.executemany(
        "INSERT INTO asset_tags(asset_id, tag, normalized_tag) VALUES (?, ?, ?)",
        [(asset_id, tag, normalize_search_value(tag)) for tag in tags],
    )
    for file_row in _file_rows(asset):
        connection.execute(
            """
            INSERT INTO asset_files(asset_id, file_role, local_path, file_format, sha256, version, byte_size, materialized)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                file_row["role"],
                file_row["local_path"],
                file_row.get("format"),
                file_row.get("sha256"),
                file_row.get("version"),
                file_row.get("byte_size"),
                int(bool(file_row.get("materialized"))),
            ),
        )
    source_uri = str(asset.get("source_uri") or f"catalog://{asset_id}")
    connection.execute(
        """
        INSERT INTO asset_sources(asset_id, source_kind, source_uri, author, license_name, license_tier, provenance_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id,
            source_kind,
            source_uri,
            asset.get("author"),
            license_name,
            license_tier,
            json.dumps(asset.get("provenance") or {}, sort_keys=True, ensure_ascii=False),
        ),
    )
    for key in ("bbox_size_m", "authored_size_m", "poly_count", "lod_count", "collider", "collision_profile"):
        if asset.get(key) is not None:
            connection.execute(
                "INSERT INTO asset_features(asset_id, feature_key, value_json) VALUES (?, ?, ?)",
                (asset_id, key, json.dumps(asset[key], sort_keys=True, ensure_ascii=False)),
            )
    for dependency in _dependency_rows(asset):
        connection.execute(
            """
            INSERT INTO asset_dependencies(asset_id, dependency_id, dependency_kind, local_path, materialized)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                dependency["dependency_id"],
                dependency.get("kind"),
                dependency.get("local_path"),
                int(bool(dependency.get("materialized"))),
            ),
        )
    bindings = _binding_rows(asset)
    for binding in bindings:
        connection.execute(
            """
            INSERT INTO backend_bindings(asset_id, backend, object_path, class_name, materialized, runtime_ready, binding_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                binding["backend"],
                binding.get("object_path"),
                binding.get("class_name"),
                int(bool(binding.get("materialized"))),
                int(bool(binding.get("runtime_ready"))),
                json.dumps(binding, sort_keys=True, ensure_ascii=False),
            ),
        )
    object_path = str(next((row.get("object_path") for row in bindings if row["backend"] == "unreal"), "") or "")
    taxonomy = " ".join(
        str(asset.get(key) or "") for key in ("category", "category_l1", "category_l2", "type", "asset_kind")
    )
    connection.execute(
        "INSERT INTO asset_search_fts(asset_id, name, aliases, tags, description, taxonomy, object_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            asset_id,
            name,
            " ".join(aliases),
            " ".join(tags),
            " ".join(str(asset.get(key) or "") for key in ("semantic_name", "description")),
            taxonomy,
            object_path,
        ),
    )
    return changed


def _file_rows(asset: Mapping[str, Any]) -> list[dict[str, Any]]:
    explicit = asset.get("files")
    if isinstance(explicit, list):
        rows = [dict(row) for row in explicit if isinstance(row, Mapping) and row.get("local_path")]
        if rows:
            return rows
    paths = asset.get("paths") if isinstance(asset.get("paths"), Mapping) else {}
    adp = asset.get("adp") if isinstance(asset.get("adp"), Mapping) else {}
    local_path = asset.get("local_path") or paths.get("local_file") or adp.get("repo_file")
    if not local_path:
        return []
    path = Path(str(local_path))
    return [
        {
            "role": "primary",
            "local_path": str(path),
            "format": path.suffix.casefold().lstrip("."),
            "sha256": asset.get("sha256"),
            "version": asset.get("version"),
            "byte_size": asset.get("byte_size"),
            "materialized": asset.get("materialized"),
        }
    ]


def _dependency_rows(asset: Mapping[str, Any]) -> list[dict[str, Any]]:
    ue = asset.get("ue") if isinstance(asset.get("ue"), Mapping) else {}
    bundle = asset.get("bundle") if isinstance(asset.get("bundle"), Mapping) else {}
    adp = asset.get("adp") if isinstance(asset.get("adp"), Mapping) else {}
    explicit = bundle.get("dependencies") if isinstance(bundle.get("dependencies"), list) else []
    by_id = {
        str(row.get("package") or row.get("dependency_id")): dict(row)
        for row in explicit
        if isinstance(row, Mapping) and (row.get("package") or row.get("dependency_id"))
    }
    dependency_files = list(adp.get("dependency_files") or [])
    result: list[dict[str, Any]] = []
    for index, value in enumerate(ue.get("dependencies") or by_id):
        dependency_id = str(value)
        row = by_id.get(dependency_id, {})
        local_path = row.get("local_path") or (dependency_files[index] if index < len(dependency_files) else None)
        result.append(
            {
                "dependency_id": dependency_id,
                "kind": row.get("kind"),
                "local_path": local_path,
                "materialized": row.get("materialized", bool(local_path and Path(str(local_path)).is_file())),
            }
        )
    return result


def _binding_rows(asset: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = asset.get("backend_bindings")
    rows: list[dict[str, Any]] = []
    if isinstance(raw, Mapping):
        for backend, value in raw.items():
            if isinstance(value, Mapping):
                rows.append({**dict(value), "backend": normalize_backend_name(backend)})
    elif isinstance(raw, list):
        rows.extend(
            {
                **dict(row),
                "backend": normalize_backend_name(row.get("backend")),
            }
            for row in raw
            if isinstance(row, Mapping) and row.get("backend")
        )
    if rows:
        for row in rows:
            row.setdefault("object_path", asset.get("ue_path"))
            row.setdefault("class_name", asset.get("class_name") or asset.get("type"))
            row.setdefault("materialized", bool(asset.get("materialized")))
            row.setdefault("runtime_ready", bool(row.get("object_path") and row.get("materialized")))
        return rows
    ue = asset.get("ue") if isinstance(asset.get("ue"), Mapping) else {}
    object_path = asset.get("ue_path") or ue.get("object_path")
    if not object_path:
        return []
    source_kind = str(asset.get("source_kind") or "")
    materialized = bool(asset.get("materialized")) or source_kind in {"engine_builtin", "analytic_proxy"}
    return [
        {
            "backend": "unreal",
            "object_path": object_path,
            "class_name": ue.get("class_name") or asset.get("class_name") or asset.get("type"),
            "materialized": materialized,
            "runtime_ready": materialized,
        }
    ]


def _hard_filter_sql(intent: SearchIntent, *, requested_category: str | None) -> tuple[str, list[Any]]:
    clauses = ["1 = 1"]
    parameters: list[Any] = []
    must = intent.must
    if "backend" in must:
        values = _backend_values(must["backend"])
        placeholders = ",".join("?" for _ in values)
        clauses.append(
            f"EXISTS (SELECT 1 FROM backend_bindings b WHERE b.asset_id = a.asset_id AND b.backend IN ({placeholders}))"
        )
        parameters.extend(values)
    if "runtime_ready" in must:
        clauses.append(
            "EXISTS (SELECT 1 FROM backend_bindings b WHERE b.asset_id = a.asset_id AND b.runtime_ready = ?)"
        )
        parameters.append(int(bool(must["runtime_ready"])))
    if "materialized" in must:
        clauses.append("a.materialized = ?")
        parameters.append(int(bool(must["materialized"])))
    if "collision" in must:
        operator = "<>" if bool(must["collision"]) else "="
        clauses.append(f"a.collider {operator} '' AND a.collision_profile {operator} ''")
    if bool(must.get("real_3d_geometry")):
        clauses.append("lower(a.asset_type) NOT IN ('image', 'texture', 'material', 'material_only', 'decal')")
    if "source_kind" in must:
        values = _as_values(must["source_kind"])
        placeholders = ",".join("?" for _ in values)
        clauses.append(
            f"EXISTS (SELECT 1 FROM asset_sources s WHERE s.asset_id = a.asset_id AND lower(s.source_kind) IN ({placeholders}))"
        )
        parameters.extend(value.casefold() for value in values)
    for field, column in (
        ("asset_type", "a.asset_type"),
        ("geometry_type", "a.asset_type"),
        ("license_tier", "a.license_tier"),
    ):
        if field in must:
            _append_value_filter(clauses, parameters, column, must[field], negate=False)
    if "class_name" in must:
        values = _as_values(must["class_name"])
        placeholders = ",".join("?" for _ in values)
        clauses.append(
            f"(lower(a.asset_type) IN ({placeholders}) OR EXISTS ("
            f"SELECT 1 FROM backend_bindings b WHERE b.asset_id = a.asset_id AND lower(b.class_name) IN ({placeholders})"
            "))"
        )
        parameters.extend(value.casefold() for value in values)
        parameters.extend(value.casefold() for value in values)
    if _filterable_category(requested_category):
        values = _as_values(requested_category)
        placeholders = ",".join("?" for _ in values)
        clauses.append(f"(lower(a.category_l1) IN ({placeholders}) OR lower(a.category_l2) IN ({placeholders}))")
        parameters.extend(value.casefold() for value in values)
        parameters.extend(value.casefold() for value in values)
    if "physics_role" in must:
        values = _as_values(must["physics_role"])
        placeholders = ",".join("?" for _ in values)
        clauses.append(
            f"EXISTS (SELECT 1 FROM asset_tags t WHERE t.asset_id = a.asset_id AND t.normalized_tag IN ({placeholders}))"
        )
        parameters.extend(normalize_search_value(value) for value in values)
    for field, column in (
        ("asset_type", "a.asset_type"),
        ("geometry_type", "a.asset_type"),
        ("license_tier", "a.license_tier"),
    ):
        if field in intent.must_not:
            _append_value_filter(clauses, parameters, column, intent.must_not[field], negate=True)
    if "class_name" in intent.must_not:
        values = _as_values(intent.must_not["class_name"])
        placeholders = ",".join("?" for _ in values)
        clauses.append(
            f"lower(a.asset_type) NOT IN ({placeholders}) AND NOT EXISTS ("
            f"SELECT 1 FROM backend_bindings b WHERE b.asset_id = a.asset_id AND lower(b.class_name) IN ({placeholders})"
            ")"
        )
        parameters.extend(value.casefold() for value in values)
        parameters.extend(value.casefold() for value in values)
    if "source_kind" in intent.must_not:
        values = _as_values(intent.must_not["source_kind"])
        placeholders = ",".join("?" for _ in values)
        clauses.append(
            f"NOT EXISTS (SELECT 1 FROM asset_sources s WHERE s.asset_id = a.asset_id AND lower(s.source_kind) IN ({placeholders}))"
        )
        parameters.extend(value.casefold() for value in values)
    if "backend" in intent.must_not:
        values = _backend_values(intent.must_not["backend"])
        placeholders = ",".join("?" for _ in values)
        clauses.append(
            f"NOT EXISTS (SELECT 1 FROM backend_bindings b WHERE b.asset_id = a.asset_id AND b.backend IN ({placeholders}))"
        )
        parameters.extend(values)
    if "category" in intent.must_not:
        values = _as_values(intent.must_not["category"])
        placeholders = ",".join("?" for _ in values)
        clauses.append(f"lower(a.category_l1) NOT IN ({placeholders}) AND lower(a.category_l2) NOT IN ({placeholders})")
        parameters.extend(value.casefold() for value in values)
        parameters.extend(value.casefold() for value in values)
    return " AND ".join(clauses), parameters


def _append_value_filter(
    clauses: list[str], parameters: list[Any], column: str, raw_value: Any, *, negate: bool
) -> None:
    values = _as_values(raw_value)
    placeholders = ",".join("?" for _ in values)
    operator = "NOT IN" if negate else "IN"
    clauses.append(f"lower({column}) {operator} ({placeholders})")
    parameters.extend(value.casefold() for value in values)


def _as_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def _backend_values(value: Any) -> list[str]:
    return [normalize_backend_name(item) for item in _as_values(value)]


def normalize_backend_name(value: Any) -> str:
    normalized = str(value or "").casefold()
    return "unreal" if normalized == "ue" or normalized.startswith("ue_") or normalized.startswith("unreal") else normalized


def _filterable_category(value: str | None) -> bool:
    return bool(value and value.casefold() not in {"physics_critical", "visual_only"})


def _aggregate_vector_status(statuses: list[Mapping[str, Any]]) -> str:
    values = [str(status.get("status") or "unknown") for status in statuses]
    if values and all(value == "ready" for value in values):
        return "ready"
    if "ready" in values:
        return "partial"
    if values and len(set(values)) == 1:
        return values[0]
    return "unavailable"


def _load_sqlite_vec(connection: sqlite3.Connection) -> None:
    try:
        import sqlite_vec
    except ImportError as exc:
        raise RuntimeError(
            "sqlite-vec is required for vector index operations; install requirements-asset-retrieval.txt"
        ) from exc
    connection.enable_load_extension(True)
    try:
        sqlite_vec.load(connection)
    finally:
        connection.enable_load_extension(False)


def _serialize_float32(values: Iterable[float]) -> bytes:
    normalized = [float(value) for value in values]
    return struct.pack(f"<{len(normalized)}f", *normalized)


def _safe_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Unsafe SQLite identifier: {value!r}")
    return value


def _workspace_root(catalog_path: Path) -> Path:
    if catalog_path.parent.name == "assets" and catalog_path.parent.parent.name == "catalog":
        return catalog_path.parents[2]
    return catalog_path.parent


def _batches(values: list[dict[str, Any]], batch_size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(values), batch_size):
        yield values[start : start + batch_size]


def _fts_query(value: str) -> str:
    tokens = [token for token in normalize_search_value(value).split() if token]
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"*' for token in tokens)


def normalize_search_value(value: Any) -> str:
    return " ".join(token for token in re.split(r"[^\w]+", str(value or "").casefold()) if token)


REFERENCE_LICENSE_NAMES = {
    "apache-2.0",
    "apache license 2.0",
    "bsd-2-clause",
    "bsd-3-clause",
    "cc-by-4.0",
    "cc-by-sa-4.0",
    "cc0",
    "cc0-1.0",
    "mit",
    "mit license",
    "mpl-2.0",
    "public domain",
}


def redistribution_evidence_allows_reference(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("allowed") is not True:
        return False
    return all(str(value.get(field) or "").strip() for field in ("rights_holder", "evidence_uri", "verified_at"))


def reference_license_authorized(
    license_name: str,
    *,
    source_kind: Any = None,
    redistribution: Any = None,
) -> bool:
    normalized = " ".join(str(license_name or "").casefold().split())
    if redistribution_evidence_allows_reference(redistribution):
        return True
    if normalized in REFERENCE_LICENSE_NAMES:
        return True
    return str(source_kind or "").casefold() == "engine_builtin" and normalized == "unreal engine eula"


def infer_license_tier(
    license_name: str,
    quality_status: Any = None,
    *,
    source_kind: Any = None,
    redistribution: Any = None,
) -> str:
    normalized = str(license_name or "").casefold()
    if reference_license_authorized(
        license_name,
        source_kind=source_kind,
        redistribution=redistribution,
    ) and str(quality_status or "").casefold() != "local_preview":
        return "reference"
    if normalized and not any(term in normalized for term in ("unknown", "unverified", "pending")):
        return "local_preview"
    return "local_preview" if str(quality_status or "").casefold() == "local_preview" else "blocked"


def effective_license_tier(
    license_name: str,
    quality_status: Any = None,
    *,
    declared_tier: Any = None,
    source_kind: Any = None,
    redistribution: Any = None,
) -> str:
    inferred = infer_license_tier(
        license_name,
        quality_status,
        source_kind=source_kind,
        redistribution=redistribution,
    )
    declared = str(declared_tier or "").casefold()
    if declared == "reference":
        return "reference" if inferred == "reference" else inferred
    if declared in {"local_preview", "blocked"}:
        return declared
    return inferred


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        normalized = normalize_search_value(text)
        if text and normalized not in seen:
            seen.add(normalized)
            result.append(text)
    return result
