from __future__ import annotations

import unittest

from harness.runtime.camera_planner import SceneBounds, camera_plan_from_case_spec, plan_cameras_for_scene


class CameraPlannerTests(unittest.TestCase):
    def test_camera_plan_preserves_declared_target_objects(self) -> None:
        plan = camera_plan_from_case_spec(
            {"objects": []},
            requested_views=["front_static"],
            camera_intents=[{"role": "front_static", "target_objects": ["person", "ball"]}],
        )

        self.assertEqual(plan.views[0].target_object_ids, ("person", "ball"))
        self.assertEqual(plan.views[0].rotation, (0.0, 90.0, 0.0))

    def test_default_bounds_generate_canonical_five_views(self) -> None:
        plan = plan_cameras_for_scene(SceneBounds(center=(0.0, 0.0, 0.5), extent=(2.0, 2.0, 1.0)))
        self.assertEqual([view.camera_id for view in plan.views], ["front_static", "side_static", "top_down", "tracking_subject", "event_closeup"])
        self.assertEqual({view.role for view in plan.views}, {"front_static", "side_static", "top_down", "tracking_subject", "event_closeup"})
        tracking = next(view for view in plan.views if view.role == "tracking_subject")
        event = next(view for view in plan.views if view.role == "event_closeup")
        self.assertEqual(plan.views[0].location, (0.0, -1.9919, 0.5))
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

    def test_full_subject_front_uses_subject_orientation_and_authored_bounds(self) -> None:
        case = {
            "objects": [
                {
                    "id": "person",
                    "initial_position_m": [1.0, 2.0, 0.0],
                    "initial_rotation_deg": [0.0, 90.0, 0.0],
                    "solver": {
                        "type": "articulated_body",
                        "authored_size_m": [0.58, 0.36, 1.92],
                    },
                }
            ]
        }

        plan = camera_plan_from_case_spec(
            case,
            requested_views=["front_static"],
            camera_intents=[{"role": "front_static", "subject": "person", "framing": "full_subject"}],
        )

        front = plan.views[0]
        self.assertEqual(front.target, (1.0, 2.0, 0.96))
        self.assertEqual(front.location[0], 1.0)
        self.assertGreater(front.location[1], 5.0)
        self.assertEqual(front.location[2], 0.96)
        self.assertEqual(front.rotation[0], 0.0)

    def test_full_subject_prefers_registered_scene_bounds(self) -> None:
        plan = camera_plan_from_case_spec(
            {
                "objects": [{
                    "id": "subject",
                    "initial_position_m": [0.0, 0.0, 0.0],
                    "size_m": [0.1, 0.1, 0.1],
                }],
            },
            requested_views=["front_static"],
            camera_intents=[{
                "role": "front_static",
                "subject": "subject",
                "framing": "full_subject",
            }],
            subject_frames={
                "subject": {
                    "center_m": [1.0, 2.0, 1.0],
                    "size_m": [0.6, 0.4, 2.0],
                    "yaw_deg": 90.0,
                },
            },
        )

        self.assertEqual(plan.views[0].target, (1.0, 2.0, 1.0))
        self.assertEqual(plan.views[0].location[0], 1.0)
        self.assertGreater(plan.views[0].location[1], 5.0)

    def test_explicit_world_pose_overrides_automatic_framing(self) -> None:
        plan = camera_plan_from_case_spec(
            {
                "objects": [{
                    "id": "subject",
                    "initial_position_m": [0.0, 0.0, 0.0],
                    "size_m": [0.6, 0.4, 2.0],
                }],
            },
            requested_views=["front_static"],
            camera_intents=[{
                "role": "front_static",
                "subject": "subject",
                "framing": "full_subject",
                "coordinate_frame": "world",
                "position_m": [4.0, -4.0, 1.4],
                "look_at_m": [0.0, 0.0, 0.9],
                "fov_deg": 50.0,
            }],
        )

        view = plan.views[0]
        self.assertEqual(view.location, (4.0, -4.0, 1.4))
        self.assertEqual(view.target, (0.0, 0.0, 0.9))
        self.assertEqual(view.fov, 50.0)
        self.assertEqual(view.camera_mode, "fixed")


if __name__ == "__main__":
    unittest.main()
