from __future__ import annotations

import copy
import unittest
from pathlib import Path

from harness.core.case_spec import load_case_spec
from harness.core.video_repair_spec import VideoRepairSpecError, load_video_repair_spec, validate_video_repair_spec


ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = ROOT / "config" / "video_repair_specs"
CASE_DIR = ROOT / "cases" / "video_repair"


class VideoRepairSpecTests(unittest.TestCase):
    def test_all_frozen_repair_specs_are_valid(self) -> None:
        specs = [load_video_repair_spec(path) for path in sorted(SPEC_DIR.glob("*.json"))]

        self.assertEqual({spec["repair_id"] for spec in specs}, {
            "person_turn_discontinuity",
            "bag_vehicle_disappearance",
            "billiards_detailed_contact",
            "glass_secondary_fracture",
        })

    def test_natural_video_boundaries_keep_observed_uncertainty_explicit(self) -> None:
        turn = load_video_repair_spec(SPEC_DIR / "person_turn_discontinuity.json")
        bag = load_video_repair_spec(SPEC_DIR / "bag_vehicle_disappearance.json")

        self.assertEqual(turn["diagnosis"]["error_interval_s"], [8.791667, 8.833333])
        self.assertEqual(bag["diagnosis"]["error_interval_s"], [3.125, 3.166667])
        self.assertEqual(bag["boundary_states"]["after"]["time_s"], 3.166667)
        self.assertEqual(bag["diagnosis"]["repairability"], "grounded")
        self.assertIn("frame 76", bag["diagnosis"]["uncertainty_note"])
        self.assertEqual(bag["edit_plan"]["source_replacement_frame_range"], [54, 222])

    def test_video_a_declares_a_continuous_fixed_position_turn(self) -> None:
        case = load_case_spec(CASE_DIR / "video_a_person_turn_in_place.json").data
        expected = case["expected_physics"]
        person = next(item for item in case["objects"] if item["id"] == "person")

        self.assertEqual(expected["coupling_type"], "turn_in_place")
        self.assertEqual(expected["turn_contract"]["start_yaw_degrees"], 180.0)
        self.assertEqual(expected["turn_contract"]["end_yaw_degrees"], 0.0)
        self.assertTrue(expected["turn_contract"]["require_fixed_support_region"])
        self.assertIn("SKM_Quinn", person["ue5_path"])
        self.assertEqual(case["video_repair"]["asset_selection"], "workspace://review/assets/laplace_asset_selection.json")

    def test_video_b_declares_drop_clip_and_sportbike_support(self) -> None:
        case = load_case_spec(CASE_DIR / "video_b_bag_to_sportbike.json").data
        expected = case["expected_physics"]
        bag = next(item for item in case["objects"] if item["id"] == "plastic_bag")
        sportbikes = [item for item in case["objects"] if item["id"].startswith("sportbike")]

        self.assertIn("A_Drop_BIEN", expected["place_animation_ref"])
        self.assertFalse(expected["place_animation_reverse"])
        self.assertTrue(expected["start_with_object_held"])
        self.assertEqual(expected["attachment_socket"], "hand_l")
        self.assertIn("SM_Trash_bug_1", bag["visual_ue5_path"])
        self.assertEqual(len(sportbikes), 4)
        self.assertEqual(
            expected["object_effect"]["final_goal_region"]["center_m"],
            [0.68, -0.49, 0.5],
        )
        self.assertEqual(case["video_repair"]["asset_selection"], "workspace://review/assets/laplace_asset_selection.json")

    def test_unknown_event_edge_fails_closed(self) -> None:
        spec = load_video_repair_spec(SPEC_DIR / "glass_secondary_fracture.json")
        invalid = copy.deepcopy(spec)
        invalid["target_event_graph"]["edges"].append(["missing", "secondary_break", "causes"])

        with self.assertRaises(VideoRepairSpecError):
            validate_video_repair_spec(invalid)


if __name__ == "__main__":
    unittest.main()
