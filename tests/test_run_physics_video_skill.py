from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents" / "skills" / "run-physics-video" / "SKILL.md"
OLD_SKILL = ROOT / "skill" / "physics-aware-harness" / "SKILL.md"


class RunPhysicsVideoSkillTests(unittest.TestCase):
    def test_skill_uses_the_repo_discovery_path(self) -> None:
        self.assertTrue(SKILL.is_file())
        self.assertFalse(OLD_SKILL.exists())

    def test_frontmatter_is_minimal_and_renamed(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        _, frontmatter, _ = text.split("---", 2)
        fields = {
            line.split(":", 1)[0].strip()
            for line in frontmatter.splitlines()
            if ":" in line
        }
        self.assertEqual(fields, {"name", "description"})
        self.assertIn("name: run-physics-video", frontmatter)

    def test_skill_is_controller_only_and_preserves_m3_boundary(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        for required in (
            "scripts/harness_agent_job.py",
            "job_id",
            "advance-until-blocked",
            "allowed_next_actions",
            "paused_interrupted",
            "awaiting_semantic_review",
            "apply-revision",
            "semantic_reviewer_image_upload",
            "Do not edit Harness source",
        ):
            self.assertIn(required, text)
        for legacy_entrypoint in (
            "harness_run_case.py",
            "harness_case_library.py",
            "harness_iterate_case.py",
        ):
            self.assertNotIn(legacy_entrypoint, text)


if __name__ == "__main__":
    unittest.main()
