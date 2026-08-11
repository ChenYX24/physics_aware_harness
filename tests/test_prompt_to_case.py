from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from harness.core.workspace import workspace_root
from scripts import harness_run_case


ROOT = Path(__file__).resolve().parents[1]


class PromptToCaseTests(unittest.TestCase):
    def test_v1_named_template_classifier_is_removed(self) -> None:
        self.assertIsNone(importlib.util.find_spec("harness.planning.prompt_to_case"))

    def test_run_case_does_not_import_named_template_classifier(self) -> None:
        source = (ROOT / "scripts" / "harness_run_case.py").read_text(encoding="utf-8")
        self.assertNotIn("from harness.planning.prompt_to_case", source)
        self.assertIn('default="v2"', source)

    def test_run_case_has_workspace_root_for_provider_input_manifest(self) -> None:
        self.assertIs(harness_run_case.workspace_root, workspace_root)


if __name__ == "__main__":
    unittest.main()
