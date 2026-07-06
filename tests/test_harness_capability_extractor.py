from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.harness_capability_extractor import extract_capability_profile, render_markdown_report


class HarnessCapabilityExtractorTests(unittest.TestCase):
    def test_extracts_generic_contact_causality_from_project_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MEMORY.md").write_text(
                "\n".join(
                    [
                        "台球 causality 修复：passive target balls 在 first active contact 前不能自走。",
                        "cue_ball 可以有 initial velocity，target balls 初始速度必须为 0。",
                    ]
                ),
                encoding="utf-8",
            )
            profile = extract_capability_profile(
                root,
                source_paths=["MEMORY.md"],
                source_preset="local",
                include_private_sources=True,
            )
            capabilities = {capability["id"]: capability for capability in profile["capabilities"]}
            contact = capabilities["rigid_body_contact_causality"]
            self.assertGreaterEqual(contact["evidence_count"], 2)
            self.assertIn("Passive bodies start with zero unexplained linear and angular velocity.", contact["runtime_contract"])
            self.assertIn("Reject if passive bodies move above threshold before first causal contact.", profile["contact_causality_reference_workflow"][4]["contract"])
            self.assertNotIn("billiard_causality_compiler", capabilities)
            self.assertIn("asset_intent_resolution", capabilities)
            self.assertIn("asset_runtime_binding_invocation", capabilities)
            self.assertIn("elastic_energy_launch", capabilities)
            self.assertIn("elastic_constraint_rebound", capabilities)

    def test_public_profile_suppresses_private_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "agent-docs" / "check_report"
            private.mkdir(parents=True)
            (private / "local.md").write_text("台球 billiard cue_ball passive first active contact", encoding="utf-8")
            profile = extract_capability_profile(
                root,
                source_paths=["agent-docs/check_report/local.md"],
                source_preset="local",
                include_private_sources=False,
            )
            contact = next(capability for capability in profile["capabilities"] if capability["id"] == "rigid_body_contact_causality")
            self.assertEqual(contact["evidence"], [])
            self.assertGreaterEqual(contact["private_evidence_suppressed"], 1)

    def test_profile_and_markdown_are_serializable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("physics_control trajectory contact events verifier dataset", encoding="utf-8")
            profile = extract_capability_profile(root, source_paths=["README.md"], source_preset="public")
            encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True)
            self.assertIn("physics_aware_harness_capabilities_v1", encoded)
            self.assertNotIn("asset_physics_binding", encoded)
            report = render_markdown_report(profile)
            self.assertIn("Physics-Aware Harness Capability Profile", report)


if __name__ == "__main__":
    unittest.main()
