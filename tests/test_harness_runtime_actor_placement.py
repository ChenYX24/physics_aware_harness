from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeActorPlacementTests(unittest.TestCase):
    def load_case(self, relative_path: str) -> dict:
        return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))

    def test_bowling_case_compiles_runtime_actor_bindings(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement

        case = self.load_case("cases/bowling/bowling_pin_chain_contact.json")
        asset_resolution = resolve_asset_intents(case)
        scene_layout = build_static_scene_layout(case, asset_resolution=asset_resolution)

        placement = compile_runtime_actor_placement(case, scene_layout, asset_resolution=asset_resolution)
        report = verify_runtime_actor_placement(case, placement)

        self.assertEqual(placement["schema_version"], "harness_runtime_actor_placement_v1")
        self.assertEqual(report["status"], "pass")
        actor_ids = {binding["runtime_actor_id"] for binding in placement["actor_bindings"]}
        self.assertEqual(len(actor_ids), len(placement["actor_bindings"]))
        by_object = {binding["object_id"]: binding for binding in placement["actor_bindings"]}
        self.assertIn("bowling_ball", by_object)
        self.assertEqual(by_object["bowling_ball"]["runtime_actor_id"], "actor_bowling_ball")
        self.assertTrue(by_object["bowling_ball"]["physics"]["simulate_physics"])
        self.assertTrue(by_object["lane"]["physics"]["kinematic"])
        self.assertIn(by_object["pin_1"]["asset"]["binding_source"], {"ue_asset", "analytic_proxy"})
        self.assertTrue(by_object["pin_1"]["asset"]["ue_path"] or by_object["pin_1"]["asset"]["proxy"])
        self.assertIn(["bowling_ball", "pin_1"], placement["physics_graph"]["collision_edges"])
        self.assertTrue(placement["camera_bindings"])

    def test_magnetic_case_keeps_force_source_bound_but_not_simulated(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement

        case = self.load_case("cases/magnetic/attract_magnetic_body.json")
        asset_resolution = resolve_asset_intents(case)
        scene_layout = build_static_scene_layout(case, asset_resolution=asset_resolution)

        placement = compile_runtime_actor_placement(case, scene_layout, asset_resolution=asset_resolution)
        report = verify_runtime_actor_placement(case, placement)

        self.assertEqual(report["status"], "pass")
        by_object = {binding["object_id"]: binding for binding in placement["actor_bindings"]}
        self.assertFalse(by_object["magnet_source"]["physics"]["simulate_physics"])
        self.assertEqual(by_object["magnet_source"]["physics"]["collision_enabled"], False)
        self.assertTrue(by_object["steel_ball"]["physics"]["simulate_physics"])

    def test_verifier_rejects_physics_object_without_asset_or_proxy(self) -> None:
        from harness.assets.asset_resolver import resolve_asset_intents
        from harness.planning.static_scene_builder import build_static_scene_layout
        from harness.runtime.actor_placement import compile_runtime_actor_placement
        from harness.verification.runtime_actor_placement_verifier import verify_runtime_actor_placement

        case = self.load_case("cases/bowling/bowling_pin_chain_contact.json")
        asset_resolution = resolve_asset_intents(case)
        scene_layout = build_static_scene_layout(case, asset_resolution=asset_resolution)
        bad_layout = deepcopy(scene_layout)
        first = bad_layout["object_nodes"][0]
        first["asset_binding"]["selected_asset_ue_path"] = None
        first["asset_binding"]["fallback_reason"] = None
        first["physics"]["proxy"] = False

        placement = compile_runtime_actor_placement(case, bad_layout, asset_resolution=asset_resolution)
        report = verify_runtime_actor_placement(case, placement)

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["failure_type"], "F2_asset_missing")
        self.assertEqual(report["first_failure"]["metric"], "missing_asset_or_proxy_binding")

    def test_actor_placement_cli_writes_contract_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "harness_compile_actor_placement.py"),
                    str(ROOT / "cases" / "bowling" / "bowling_pin_chain_contact.json"),
                    "--output-dir",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["actor_count"], 6)
            self.assertTrue((Path(tmp) / "runtime_actor_placement.json").exists())
            self.assertTrue((Path(tmp) / "runtime_actor_placement_report.json").exists())
            self.assertTrue((Path(tmp) / "scene_layout.json").exists())
            self.assertTrue((Path(tmp) / "asset_resolution.json").exists())


if __name__ == "__main__":
    unittest.main()
