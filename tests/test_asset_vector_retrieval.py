from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from harness.assets.asset_registry import AssetRegistry
from harness.assets.embedding_index import EmbeddingModelSpec, OpenCLIPEmbeddingProvider
from harness.assets.hybrid_ranking import RankingConfig, RecallHit, fuse_ranked_channels, load_retrieval_config
from harness.assets.search_intent import SearchIntent
from harness.assets.sqlite_catalog import initialize_catalog


SQLITE_VEC_AVAILABLE = importlib.util.find_spec("sqlite_vec") is not None


class FixtureEmbeddingProvider:
    def __init__(self) -> None:
        self.text_call_count = 0
        self.image_call_count = 0
        self._spec = EmbeddingModelSpec(
            provider="fixture",
            model_name="semantic-fixture",
            pretrained="v1",
            dimension=4,
            document_version="asset_semantic_document_v1",
            library_version="test",
            checkpoint_sha256="0" * 64,
        )

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self._spec

    def encode_texts(self, values: list[str]) -> list[list[float]]:
        self.text_call_count += 1
        return [self._text_vector(value) for value in values]

    def encode_images(self, paths: list[Path]) -> list[list[float]]:
        self.image_call_count += 1
        return [[0.0, 0.0, 1.0, 0.0] for _ in paths]

    def _text_vector(self, value: str) -> list[float]:
        normalized = value.casefold()
        if any(token in normalized for token in ("chair", "seat", "seating", "workspace", "办公椅")):
            return [1.0, 0.0, 0.0, 0.0]
        if any(token in normalized for token in ("ball", "sphere")):
            return [0.0, 1.0, 0.0, 0.0]
        return [0.0, 0.0, 0.0, 1.0]


class HybridRankingTests(unittest.TestCase):
    def test_openclip_cache_resolves_under_workspace_without_downloading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = OpenCLIPEmbeddingProvider.from_config(
                load_retrieval_config(),
                workspace=tmp,
            )

            self.assertEqual(provider.cache_dir, Path(tmp) / "models" / "openclip")
            self.assertFalse(provider.allow_download)
            self.assertEqual(provider.model_name, "xlm-roberta-base-ViT-B-32")
            self.assertEqual(provider.pretrained, "laion5b_s13b_b90k")

    def test_default_offline_cache_is_configured_before_openclip_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            fake_module = root / "open_clip.py"
            fake_module.write_text(
                textwrap.dedent(
                    """
                    import os

                    IMPORTED_ENV = {
                        key: os.environ.get(key)
                        for key in (
                            "HF_HOME",
                            "HUGGINGFACE_HUB_CACHE",
                            "TORCH_HOME",
                            "HF_HUB_OFFLINE",
                            "TRANSFORMERS_OFFLINE",
                        )
                    }

                    class _Model:
                        def eval(self):
                            return self

                    def create_model_and_transforms(*args, **kwargs):
                        return _Model(), None, object()

                    def get_tokenizer(*args, **kwargs):
                        return object()
                    """
                ),
                encoding="utf-8",
            )
            probe = textwrap.dedent(
                """
                import json
                import os
                from pathlib import Path

                from harness.assets.embedding_index import OpenCLIPEmbeddingProvider

                workspace = Path(os.environ["SIM_HARNESS_WORKSPACE"])
                provider = OpenCLIPEmbeddingProvider(
                    model_name="fixture",
                    pretrained="fixture",
                    dimension=4,
                    document_version="fixture_v1",
                    cache_dir=workspace / "models" / "openclip",
                )
                provider._ensure_loaded()
                import open_clip
                print(json.dumps(open_clip.IMPORTED_ENV, sort_keys=True))
                """
            )
            environment = dict(os.environ)
            for key in (
                "HF_HOME",
                "HUGGINGFACE_HUB_CACHE",
                "TRANSFORMERS_CACHE",
                "TORCH_HOME",
                "HF_HUB_OFFLINE",
                "TRANSFORMERS_OFFLINE",
            ):
                environment.pop(key, None)
            environment["SIM_HARNESS_WORKSPACE"] = str(workspace)
            source_root = Path(__file__).parents[1]
            environment["PYTHONPATH"] = os.pathsep.join((str(root), str(source_root)))

            completed = subprocess.run(
                [sys.executable, "-c", probe],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )

            imported = json.loads(completed.stdout)
            model_root = workspace / "models"
            self.assertEqual(imported["HF_HOME"], str(model_root / "huggingface"))
            self.assertEqual(imported["HUGGINGFACE_HUB_CACHE"], str(model_root / "huggingface" / "hub"))
            self.assertEqual(imported["TORCH_HOME"], str(model_root / "torch"))
            self.assertEqual(imported["HF_HUB_OFFLINE"], "1")
            self.assertEqual(imported["TRANSFORMERS_OFFLINE"], "1")

    def test_openclip_spec_hashes_an_explicit_local_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "open_clip_pytorch_model.bin"
            checkpoint.write_bytes(b"checkpoint fixture")
            provider = OpenCLIPEmbeddingProvider(
                model_name="xlm-roberta-base-ViT-B-32",
                pretrained="laion5b_s13b_b90k",
                dimension=512,
                document_version="asset_semantic_document_v1",
                cache_dir=Path(tmp) / "cache",
                checkpoint_path=checkpoint,
            )

            expected = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
            self.assertEqual(provider.spec.checkpoint_sha256, expected)
            self.assertIn(expected, provider.spec.model_id)

    def test_rrf_is_one_based_weighted_and_deterministic(self) -> None:
        config = RankingConfig(
            schema_version="asset_retrieval_config_v1",
            rrf_k=60,
            channel_weights={"exact": 2.0, "fts": 1.0, "text_vector": 1.0},
            exact_priority=True,
            materialized_bonus=0.0,
            taxonomy_match_bonus=0.0,
            preference_scale=0.0,
        )
        ranked = fuse_ranked_channels(
            {
                "exact": [RecallHit("exact_asset")],
                "fts": [RecallHit("semantic_asset"), RecallHit("exact_asset")],
                "text_vector": [RecallHit("semantic_asset"), RecallHit("exact_asset")],
            },
            assets={"exact_asset": {}, "semantic_asset": {}},
            intent=SearchIntent(raw_query="query"),
            config=config,
        )

        self.assertEqual([row["asset_id"] for row in ranked], ["exact_asset", "semantic_asset"])
        self.assertAlmostEqual(ranked[0]["channels"]["exact"]["rrf_contribution"], 2.0 / 61.0)
        self.assertEqual(ranked[0]["channels"]["fts"]["rank"], 2)


@unittest.skipUnless(SQLITE_VEC_AVAILABLE, "sqlite-vec optional dependency is not installed")
class AssetVectorRetrievalTests(unittest.TestCase):
    def test_rebuild_is_idempotent_and_vector_respects_hard_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = FixtureEmbeddingProvider()
            catalog = initialize_catalog(root / "catalog.sqlite")
            payload = self.registry_payload(root)
            catalog.import_registry(payload)

            first = catalog.rebuild_vector_index(provider, include_images=True, batch_size=2)
            first_text_calls = provider.text_call_count
            first_image_calls = provider.image_call_count
            second = catalog.rebuild_vector_index(provider, include_images=True, batch_size=2)

            self.assertEqual(first["embedding_count"], 5)
            self.assertEqual(first["text_embedding_count"], 4)
            self.assertEqual(first["image_embedding_count"], 1)
            self.assertEqual(second["source_digest"], first["source_digest"])
            self.assertEqual(second["encoded_count"], 0)
            self.assertEqual(second["reused_count"], 5)
            self.assertEqual(provider.text_call_count, first_text_calls)
            self.assertEqual(provider.image_call_count, first_image_calls)

            registry = AssetRegistry(catalog.path, embedding_provider=provider)
            detailed = registry.search_detailed(
                SearchIntent.from_dict(
                    {
                        "raw_query": "ergonomic workspace seating",
                        "semantic_text": "ergonomic workspace seating",
                        "must": {
                            "backend": "unreal",
                            "real_3d_geometry": True,
                            "collision": True,
                        },
                    }
                ),
                top_k=4,
            )

            result_ids = [entry["asset"]["asset_id"] for entry in detailed["results"]]
            self.assertEqual(result_ids[0], "modern_wood_chair")
            self.assertNotIn("chair_without_collision", result_ids)
            self.assertNotIn("chair_reference_texture", result_ids)
            self.assertEqual(detailed["retrieval"]["eligible_count"], 2)
            self.assertEqual(detailed["retrieval"]["vector_status"]["status"], "ready")
            self.assertEqual(detailed["retrieval"]["vector_status"]["text"]["status"], "ready")
            self.assertEqual(detailed["retrieval"]["match_decision"]["status"], "accepted")
            self.assertIn("text_vector", detailed["results"][0]["score"]["channels"])

    def test_low_similarity_vector_only_query_returns_no_relevant_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = FixtureEmbeddingProvider()
            catalog = initialize_catalog(root / "catalog.sqlite")
            catalog.import_registry(self.registry_payload(root))
            catalog.rebuild_vector_index(provider)

            detailed = AssetRegistry(catalog.path, embedding_provider=provider).search_detailed(
                SearchIntent(
                    raw_query="unrelated quantum violin",
                    semantic_text="unrelated quantum violin",
                    must={"backend": "unreal", "real_3d_geometry": True, "collision": True},
                ),
                top_k=4,
            )

            self.assertEqual(detailed["results"], [])
            self.assertEqual(detailed["retrieval"]["vector_status"]["status"], "ready")
            decision = detailed["retrieval"]["match_decision"]
            self.assertEqual(decision["status"], "no_relevant_asset")
            self.assertEqual(decision["reason"], "vector_similarity_below_threshold")
            self.assertEqual(decision["lexical_hit_count"], 0)
            self.assertEqual(decision["top_vector_similarity"], 0.0)

    def test_close_high_similarity_candidates_are_ambiguous_not_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = FixtureEmbeddingProvider()
            catalog = initialize_catalog(root / "catalog.sqlite")
            catalog.import_registry(self.registry_payload(root))
            catalog.rebuild_vector_index(provider)

            detailed = AssetRegistry(catalog.path, embedding_provider=provider).search_detailed(
                SearchIntent(
                    raw_query="ergonomic workspace seating",
                    semantic_text="ergonomic workspace seating",
                    must={"backend": "unreal", "real_3d_geometry": True},
                ),
                top_k=4,
            )

            self.assertTrue(detailed["results"])
            decision = detailed["retrieval"]["match_decision"]
            self.assertEqual(decision["status"], "ambiguous_candidates")
            self.assertEqual(decision["reason"], "top1_margin_below_threshold")
            self.assertEqual(decision["top_vector_similarity"], 1.0)
            self.assertEqual(decision["top1_margin"], 0.0)

    def test_reference_image_uses_image_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = FixtureEmbeddingProvider()
            catalog = initialize_catalog(root / "catalog.sqlite")
            catalog.import_registry(self.registry_payload(root))
            catalog.rebuild_vector_index(provider, include_images=True)
            reference = root / "query.png"
            reference.write_bytes(b"query image")

            detailed = AssetRegistry(catalog.path, embedding_provider=provider).search_detailed(
                SearchIntent(
                    raw_query="visual reference",
                    semantic_text="visual reference",
                    reference_image=str(reference),
                    must={"backend": "unreal", "real_3d_geometry": True, "collision": True},
                ),
                top_k=1,
            )

            self.assertEqual(detailed["results"][0]["asset"]["asset_id"], "modern_wood_chair")
            self.assertIn("image_vector", detailed["results"][0]["score"]["channels"])
            self.assertEqual(detailed["retrieval"]["vector_status"]["status"], "ready")

    def test_changed_import_marks_index_stale_until_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = FixtureEmbeddingProvider()
            catalog = initialize_catalog(root / "catalog.sqlite")
            payload = self.registry_payload(root)
            catalog.import_registry(payload)
            catalog.rebuild_vector_index(provider)

            unchanged = catalog.import_registry(payload)
            self.assertEqual(unchanged["changed_count"], 0)
            self.assertEqual(catalog.vector_index_status()["status"], "ready")

            payload["assets"][0]["description"] = "Updated ergonomic workspace seating"
            changed = catalog.import_registry(payload)
            self.assertEqual(changed["changed_count"], 1)
            self.assertEqual(catalog.vector_index_status()["status"], "stale")
            rebuilt = catalog.rebuild_vector_index(provider)
            self.assertEqual(rebuilt["encoded_count"], 1)
            self.assertEqual(catalog.vector_index_status()["status"], "ready")

    def registry_payload(self, root: Path) -> dict[str, object]:
        preview = root / "chair.png"
        preview.write_bytes(b"fixture preview")
        base = {
            "semantic_name": "office chair",
            "description": "Comfortable wooden chair for a modern office",
            "aliases": ["办公椅", "木椅"],
            "tags": ["chair", "wood", "modern"],
            "category_l1": "furniture",
            "category_l2": "chair",
            "source_kind": "harness_generated",
            "license": "CC0-1.0",
            "quality_status": "approved",
            "materialized": True,
            "collider": "mesh",
            "collision_profile": "PhysicsActor",
            "backend_bindings": {
                "unreal": {
                    "object_path": "/Game/Props/Chair.Chair",
                    "class_name": "StaticMesh",
                    "materialized": True,
                    "runtime_ready": True,
                }
            },
        }
        chair = {
            **base,
            "asset_id": "modern_wood_chair",
            "name": "Modern Wood Chair",
            "type": "StaticMesh",
            "thumbnail": str(preview),
        }
        no_collision = {
            **base,
            "asset_id": "chair_without_collision",
            "name": "Chair Without Collision",
            "type": "StaticMesh",
            "collider": None,
            "collision_profile": None,
            "thumbnail": None,
        }
        texture = {
            **base,
            "asset_id": "chair_reference_texture",
            "name": "Office Chair Texture",
            "type": "texture",
            "thumbnail": None,
        }
        ball = {
            **base,
            "asset_id": "physics_ball",
            "name": "Physics Ball",
            "semantic_name": "sphere ball",
            "description": "A rigid sphere",
            "aliases": [],
            "tags": ["ball", "sphere"],
            "category_l1": "prop",
            "category_l2": "ball",
            "type": "StaticMesh",
            "thumbnail": None,
        }
        return {"assets": [chair, no_collision, texture, ball]}


if __name__ == "__main__":
    unittest.main()
