from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.case_spec import load_case_spec
from harness.runtime.taichi_cloth_backend import TaichiClothBackend


ROOT = Path(__file__).resolve().parents[1]


class RoboDojoGarmentCaseTests(unittest.TestCase):
    def test_contract_loads_but_unsupported_solver_fails_closed(self) -> None:
        case = load_case_spec(ROOT / "cases/soft_body/garment_folding/v001_robodojo_top_long_fold.json")
        self.assertTrue(case.data["expected_physics"]["self_collision_required"])
        screened = case.data["objects"][0]["asset_contract"]["local_catalog_screening"]
        self.assertTrue(screened["materialized"])
        self.assertEqual(screened["decision"], "rejected_for_robodojo_fold")
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(RuntimeError, "self-collision"):
            TaichiClothBackend().run_case(case, directory)


if __name__ == "__main__":
    unittest.main()
