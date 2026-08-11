from __future__ import annotations

import unittest

from harness.runtime.camera_planner import SceneBounds, camera_plan_from_case_spec, plan_cameras_for_scene


class CameraPlannerTests(unittest.TestCase):
    def test_default_bounds_generate_canonical_five_views(self) -> None:
        plan = plan_cameras_for_scene(SceneBounds(center=(0.0, 0.0, 0.5), extent=(2.0, 2.0, 1.0)))
        self.assertEqual([view.camera_id for view in plan.views], ["front_static", "side_static", "top_down", "tracking_subject", "event_closeup"])
        self.assertEqual({view.role for view in plan.views}, {"front_static", "side_static", "top_down", "tracking_subject", "event_closeup"})
        self.assertEqual(plan.views[0].location, (2.2, -2.2, 2.7))
        tracking = next(view for view in plan.views if view.role == "tracking_subject")
        event = next(view for view in plan.views if view.role == "event_closeup")
        self.assertEqual(tracking.dynamic_camera_profile, "damped_event_context_v1")
        self.assertEqual(tracking.camera_mode, "object_bound")
        self.assertEqual((tracking.subject_follow_location_gain, tracking.subject_follow_target_gain), (0.65, 0.65))
        self.assertEqual(event.dynamic_camera_profile, "damped_event_context_v1")
        self.assertEqual(event.camera_mode, "trajectory")
        self.assertEqual((event.subject_follow_location_gain, event.subject_follow_target_gain), (0.2, 0.1))
        self.assertEqual(event.fov, 46.0)

    def test_tiny_bounds_do_not_crash(self) -> None:
        plan = plan_cameras_for_scene(SceneBounds(center=(0.0, 0.0, 0.0), extent=(0.0, 0.0, 0.0)))
        self.assertEqual(len(plan.views), 5)
        self.assertTrue(plan.warnings)

    def test_planner_is_deterministic(self) -> None:
        bounds = SceneBounds(center=(1.0, 2.0, 3.0), extent=(4.0, 5.0, 6.0))
        first = plan_cameras_for_scene(bounds)
        second = plan_cameras_for_scene(bounds)
        self.assertEqual(first, second)

    def test_camera_ids_are_unique(self) -> None:
        plan = plan_cameras_for_scene(SceneBounds(center=(0.0, 0.0, 0.0), extent=(1.0, 1.0, 1.0)), requested_views=["top", "top", "side"])
        ids = [view.camera_id for view in plan.views]
        self.assertEqual(ids, ["top", "side"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_case_spec_bounds_parser_is_tolerant(self) -> None:
        case_spec = {
            "objects": [
                {"id": "a", "initial_position_m": [-1, 0, 0]},
                {"id": "b", "location": [3, 2, 1]},
            ]
        }
        plan = camera_plan_from_case_spec(case_spec, requested_views=["overview", "front", "side", "top"])
        self.assertEqual(len(plan.views), 4)
        self.assertEqual(plan.scene_bounds.center, (1.0, 1.0, 0.5))

    def test_task_label_does_not_change_bounds_framing(self) -> None:
        case = {
            "task_type": "billiards_collision",
            "objects": [
                {"id": "cue", "initial_position_m": [-1.5, 0, 0.1]},
                {"id": "rack", "initial_position_m": [0.8, 0.4, 0.1]},
            ],
        }

        plan = camera_plan_from_case_spec(case, requested_views=["front_static", "side_static"])

        self.assertEqual(plan.views[0].fov, 60.0)
        self.assertGreater(plan.views[1].location[2], 0.8)

    def test_case_can_override_one_camera_without_changing_other_views(self) -> None:
        case = {
            "scene": {
                "scene_bounds": {"center": [0, 0, 1], "extent": [2, 2, 2]},
                "camera_overrides": {
                    "top_down": {"role": "high_oblique_static", "location": [2.42, -2.42, 5.21], "target": [0, 0, 1.25], "fov": 45}
                },
            }
        }

        plan = camera_plan_from_case_spec(case, requested_views=["front_static", "top_down"])

        self.assertEqual(plan.views[0].camera_id, "front_static")
        self.assertEqual(plan.views[1].role, "high_oblique_static")
        self.assertEqual(plan.views[1].location, (2.42, -2.42, 5.21))
        self.assertEqual(plan.views[1].target, (0.0, 0.0, 1.25))
        self.assertEqual(plan.views[1].fov, 45.0)

    def test_side_view_observes_dominant_horizontal_axis_broadside(self) -> None:
        plan = plan_cameras_for_scene(
            SceneBounds(center=(0.0, 0.0, 0.7), extent=(6.0, 1.0, 1.4)),
            requested_views=["side_static"],
        )

        side = plan.views[0]
        self.assertEqual(side.location[0], 0.0)
        self.assertLess(side.location[1], 0.0)


if __name__ == "__main__":
    unittest.main()
