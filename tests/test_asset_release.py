from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.harness_prepare_asset_release import prepare_release


class AssetReleaseTests(unittest.TestCase):
    def test_unverified_assets_are_audited_without_leaking_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "Content"
            package = content / "Props" / "Ball.uasset"
            package.parent.mkdir(parents=True)
            package.write_bytes(b"asset")
            registry = root / "registry.json"
            registry.write_text(json.dumps({"assets": [{
                "asset_id": "ball",
                "name": "Ball",
                "source_uri": "ue://Game/Props/Ball",
                "license": "UNVERIFIED_LOCAL_ENTITLEMENT",
                "adp": {"repo_file": str(package), "dependency_files": []},
            }]}), encoding="utf-8")

            output = root / "release"
            summary = prepare_release(registry, content, output)

            self.assertFalse(summary["publication_ready"])
            self.assertEqual(summary["blocked_asset_count"], 1)
            self.assertEqual(summary["blocked_file_count"], 1)
            combined = "".join(path.read_text(encoding="utf-8") for path in output.iterdir())
            self.assertNotIn(str(root), combined)
            self.assertIn("Props/Ball.uasset", combined)

    def test_explicit_redistribution_evidence_allows_asset_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "Content"
            package = content / "Generated" / "Cube.uasset"
            package.parent.mkdir(parents=True)
            package.write_bytes(b"owned")
            registry = root / "registry.json"
            registry.write_text(json.dumps({"assets": [{
                "asset_id": "cube",
                "source_uri": "https://example.org/cube",
                "license": "Apache-2.0",
                "redistribution": {
                    "allowed": True,
                    "rights_holder": "Example Lab",
                    "evidence_uri": "https://example.org/license",
                    "verified_at": "2026-08-02",
                },
                "adp": {"repo_file": str(package), "dependency_files": []},
            }]}), encoding="utf-8")

            summary = prepare_release(registry, content, root / "release")

            self.assertTrue(summary["publication_ready"])
            self.assertEqual(summary["eligible_asset_count"], 1)
            self.assertEqual(summary["eligible_file_count"], 1)

    def test_output_must_stay_outside_content_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "Content"
            content.mkdir()
            registry = root / "registry.json"
            registry.write_text('{"assets": []}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "outside the content root"):
                prepare_release(registry, content, content / "release")


if __name__ == "__main__":
    unittest.main()
