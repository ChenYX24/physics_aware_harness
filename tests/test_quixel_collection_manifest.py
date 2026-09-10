from __future__ import annotations

import unittest

from scripts.build_quixel_collection_manifest import physical_dimensions, preview_uri


class QuixelCollectionManifestTests(unittest.TestCase):
    def test_extracts_ordered_meter_dimensions(self) -> None:
        payload = {
            "meta": [
                {"key": "height", "value": "0.05m"},
                {"key": "length", "value": "0.32m"},
                {"key": "width", "value": "0.29m"},
            ]
        }
        self.assertEqual(physical_dimensions(payload), [0.32, 0.29, 0.05])

    def test_prefers_non_inline_thumbnail(self) -> None:
        payload = {
            "previews": {
                "images": [
                    {"uri": "data:image/png;base64,abc", "tags": ["thumb"]},
                    {"uri": "/assets/thumb.jpg", "tags": ["thumb", "jpeg"]},
                ]
            }
        }
        self.assertEqual(preview_uri(payload), "/assets/thumb.jpg")


if __name__ == "__main__":
    unittest.main()
