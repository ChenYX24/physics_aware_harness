from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.harness_capability_extractor import extract_capability_profile, render_markdown_report


class HarnessCapabilityExtractorTests(unittest.TestCase):
    def test_source_text_is_provenance_not_process_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MEMORY.md").write_text("台球 坠落 多米诺 反弹", encoding="utf-8")
            profile = extract_capability_profile(root, source_paths=["MEMORY.md"], source_preset="local", include_private_sources=True)
            ids = {item["id"] for item in profile["capabilities"]}
            self.assertIn("rigid_body_dynamics", ids)
            self.assertNotIn("sequential_contact_propagation", ids)
            self.assertEqual(profile["source_files"], ["MEMORY.md"])

    def test_private_sources_are_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "agent-docs" / "local.md"
            path.parent.mkdir(parents=True)
            path.write_text("content", encoding="utf-8")
            profile = extract_capability_profile(root, source_paths=["agent-docs/local.md"], include_private_sources=False)
            self.assertEqual(profile["source_files"], [])
            self.assertEqual(profile["private_sources_suppressed"], ["agent-docs/local.md"])

    def test_profile_and_markdown_are_serializable(self) -> None:
        profile = extract_capability_profile(Path(__file__).resolve().parents[1])
        self.assertIn("physics_aware_harness_capabilities_v2", json.dumps(profile))
        report = render_markdown_report(profile)
        self.assertIn("Capability Taxonomy", report)
        self.assertIn("Named physical phenomena are case data", report)


if __name__ == "__main__":
    unittest.main()
