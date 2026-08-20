from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.core.case_spec import validate_case_spec
from harness.core.prompt_lineage import prompt_stage_text
from harness.core.review_feedback import REVIEW_FEEDBACK_ENV, compile_review_feedback
from harness.planning.prompt_to_case import prompt_to_case


class PromptToCaseTests(unittest.TestCase):
    def test_billiards_prompt_compiles_to_executable_reviewable_case(self) -> None:
        case = prompt_to_case("A cue ball hits a stationary billiard ball at 3 m/s", case_id="prompt_billiards")

        validate_case_spec(case)
        self.assertTrue(case["objects"])
        self.assertTrue(case["expected_physics"]["needs_agent_review"])
        cue = next(item for item in case["objects"] if item["id"] == "cue_ball")
        self.assertEqual(cue["initial_velocity_m_s"], [3.0, 0.0, 0.0])
        self.assertIn("segmentation", case["required_signals"])

    def test_empty_prompt_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            prompt_to_case("  ")

    def test_billiards_break_reuses_full_table_contract_and_records_prompt_lineage(self) -> None:
        case = prompt_to_case("台球开球", case_id="prompt_billiards_break")

        validate_case_spec(case)
        self.assertGreaterEqual(len(case["objects"]), 21)
        self.assertIn("regulation six-pocket table", case["prompt"])
        self.assertIn("exactly one cue ball and fifteen object balls", case["refiner_prompt"])
        self.assertEqual(
            case["prompt"],
            prompt_stage_text(case["prompt_lineage"], "canonical_generation_prompt"),
        )
        self.assertTrue(case["planning_trace"]["prompt_contract"]["all_prompt_only_models_must_match_canonical_verbatim"])

    def test_domino_prompt_keeps_requested_count_in_the_model_prompt(self) -> None:
        case = prompt_to_case("Six upright dominoes tip in order.", case_id="six_dominoes")

        self.assertIn("preserve the exact requested domino count", case["prompt"])

    def test_review_issue_tags_become_future_prompt_and_source_quality_constraints(self) -> None:
        compiled = compile_review_feedback(
            {
                "schema_version": "physics_harness_case_curation_decisions_v2",
                "catalog_sha256": "catalog",
                "decisions": {
                    "T01_BILLIARDS": {
                        "decision": "keep",
                        "issues": ["billiards_table_topology", "object_identity_count_color", "ue_render_quality"],
                        "feedback": "Keep the physics, replace the proxy table.",
                    }
                },
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "active.json"
            path.write_text(json.dumps(compiled), encoding="utf-8")
            with patch.dict(os.environ, {REVIEW_FEEDBACK_ENV: str(path)}):
                case = prompt_to_case("台球开球", case_id="learned_billiards")

        self.assertTrue(any("regulation six-pocket table" in value for value in case["appearance_requirements"]))
        self.assertTrue(any("identity, count, color" in value for value in case["preservation_requirements"]))
        self.assertTrue(any("resolution" in value for value in case["review_quality_gates"]))

    def test_english_fluid_prompt_reaches_the_fluid_case_template(self) -> None:
        case = prompt_to_case("A water drop splashes into a rigid basin", case_id="prompt_fluid_en")

        validate_case_spec(case)
        self.assertEqual(case["capability_id"], "fluid_particle_dynamics")
        self.assertEqual(case["task_type"], "fluid_drop_in_basin")
        self.assertEqual(case["planning_trace"]["execution_strategy"]["preferred_runtime"], "GenesisSPH")

    def test_chinese_fluid_prompt_reaches_the_fluid_case_template(self) -> None:
        case = prompt_to_case("一团流体落入刚性盆中并产生水花", case_id="prompt_fluid_zh")

        validate_case_spec(case)
        self.assertEqual(case["capability_id"], "fluid_particle_dynamics")
        self.assertEqual(case["objects"][0]["role"], "fluid_volume")


if __name__ == "__main__":
    unittest.main()
