from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.prepare_quixel_ue_import_batch import prepare_batch


class PrepareQuixelUEImportBatchTests(unittest.TestCase):
    def test_prepares_hashed_backend_import_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fbx = root / "source.fbx"
            fbx.write_bytes(b"fbx")
            manifest = root / "source_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "harness_quixel_collection_manifest_v1",
                        "assets": [
                            {
                                "asset_id": "abc123",
                                "semantic_name": "Rusty Metal Barrel",
                                "authored_size_m": [0.6, 0.6, 0.9],
                                "local_fbx": {
                                    "local_path": str(fbx),
                                    "sha256": hashlib.sha256(b"fbx").hexdigest(),
                                    "byte_size": 3,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            batch = prepare_batch(manifest, root / "prepared", destination_path="/Game/Imported/Test")

            self.assertEqual(batch["item_count"], 1)
            request = json.loads(Path(batch["items"][0]["request_path"]).read_text(encoding="utf-8"))
            self.assertEqual(request["destination_path"], "/Game/Imported/Test")
            self.assertEqual(request["expected_size_m"], [0.6, 0.6, 0.9])
            self.assertEqual(request["source_kind"], "external_site")
            self.assertTrue(request["desired_name"].endswith("abc123"))

    def test_rejects_destination_outside_game(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "source_manifest.json"
            manifest.write_text(
                json.dumps({"schema_version": "harness_quixel_collection_manifest_v1", "assets": [{}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "destination_path"):
                prepare_batch(manifest, root / "prepared", destination_path="/Engine/Test")


if __name__ == "__main__":
    unittest.main()
