from __future__ import annotations

import unittest

from harness.core.prompt_lineage import new_prompt_lineage, prompt_digest, validate_prompt_lineage


class PromptLineageTests(unittest.TestCase):
    def test_validator_rejects_forged_empty_stage_content(self) -> None:
        lineage = new_prompt_lineage("case", "prompt")
        for content in (None, "", {}, []):
            forged = {**lineage, "stages": [{**lineage["stages"][0], "content": content, "content_sha256": prompt_digest(content)}]}
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "content must be"):
                validate_prompt_lineage(forged)


if __name__ == "__main__":
    unittest.main()
