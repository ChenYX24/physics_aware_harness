from __future__ import annotations

import unittest

from scripts.harness_render_deformable_ue import (
    configure_analytic_replay_prop,
    kinematic_replay_state,
)


class DeformableUEReplayTests(unittest.TestCase):
    def test_configures_declared_analytic_shape_and_full_size(self) -> None:
        runtime_obj = {
            "ue5_path": "/Engine/BasicShapes/Cube.Cube",
            "scale": [0.15, 0.15, 0.15],
            "initial_position_m": [0.0, 0.0, 0.0],
            "params": {},
        }
        case_obj = {
            "id": "collider",
            "role": "rigid_collider",
            "shape": "cylinder",
            "size_m": [0.3, 0.3, 0.6],
            "initial_position_m": [1.0, 2.0, 3.0],
            "initial_rotation_deg": [0.0, 25.0, 10.0],
            "visual_representation": {"source": "none"},
        }

        configure_analytic_replay_prop(runtime_obj, case_obj)

        self.assertEqual(runtime_obj["ue5_path"], "/Engine/BasicShapes/Cylinder.Cylinder")
        self.assertEqual(runtime_obj["scale"], [0.3, 0.3, 0.6])
        self.assertEqual(runtime_obj["initial_position_m"], [1.0, 2.0, 3.0])
        self.assertEqual(runtime_obj["rotation_degrees"], [0.0, 25.0, 10.0])

    def test_support_surface_position_is_declared_top_height(self) -> None:
        runtime_obj = {"params": {}}
        case_obj = {
            "id": "floor",
            "role": "support_surface",
            "shape": "box",
            "size_m": [4.0, 4.0, 0.1],
            "initial_position_m": [0.0, 0.0, 0.2],
            "visual_representation": {"source": "none"},
        }

        configure_analytic_replay_prop(runtime_obj, case_obj)

        self.assertEqual(runtime_obj["initial_position_m"][:2], [0.0, 0.0])
        self.assertAlmostEqual(runtime_obj["initial_position_m"][2], 0.15)
        self.assertEqual(runtime_obj["scale"], [4.0, 4.0, 0.1])

    def test_asset_visual_is_not_rewritten(self) -> None:
        runtime_obj = {
            "ue5_path": "/Game/Props/SM_Table.SM_Table",
            "scale": [1.0, 1.0, 1.0],
            "params": {},
        }
        case_obj = {
            "shape": "box",
            "size_m": [2.0, 1.0, 0.2],
            "visual_representation": {"source": "asset"},
        }

        configure_analytic_replay_prop(runtime_obj, case_obj)

        self.assertEqual(runtime_obj["ue5_path"], "/Game/Props/SM_Table.SM_Table")
        self.assertEqual(runtime_obj["scale"], [1.0, 1.0, 1.0])

    def test_asset_visual_honors_explicit_solid_color(self) -> None:
        runtime_obj = {
            "ue5_path": "/Game/Props/SM_Magnet.SM_Magnet",
            "scale": [1.0, 1.0, 1.0],
            "params": {"preserve_material": True},
        }
        case_obj = {
            "id": "magnet",
            "visual_representation": {"source": "asset"},
            "fixed_material_color": True,
            "color_rgb": [0.72, 0.03, 0.02],
        }

        configure_analytic_replay_prop(runtime_obj, case_obj)

        self.assertEqual(runtime_obj["ue5_path"], "/Game/Props/SM_Magnet.SM_Magnet")
        self.assertFalse(runtime_obj["params"]["preserve_material"])
        self.assertTrue(runtime_obj["params"]["generate_solid_material"])
        self.assertEqual(runtime_obj["params"]["color_rgb"], [0.72, 0.03, 0.02])

    def test_kinematic_replay_samples_declared_linear_trajectory(self) -> None:
        obj = {
            "initial_rotation_deg": [0.0, 0.0, 15.0],
            "kinematic_motion": {
                "interpolation": "linear",
                "keyframes": [
                    {"time_s": 0.0, "position_m": [0.0, 0.0, 1.0], "rotation_deg": [0.0, 0.0, 15.0]},
                    {"time_s": 2.0, "position_m": [2.0, 0.0, 1.0], "rotation_deg": [0.0, 90.0, 15.0]},
                ],
            },
        }

        state = kinematic_replay_state(obj, 1.0)

        self.assertEqual(state["position"], [1.0, 0.0, 1.0])
        self.assertAlmostEqual(state["velocity"][0], 1.0, places=5)
        self.assertEqual(state["rotation_degrees"], [0.0, 45.0, 15.0])


if __name__ == "__main__":
    unittest.main()
