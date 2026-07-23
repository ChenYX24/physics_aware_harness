from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.artifact_schema import read_json, write_json
from harness.core.case_library import (
    CaseLibraryError,
    catalog_case_plan,
    create_variant_plan,
    materialize_variant,
    organize_workspace_cases,
    variant_render_command,
)


MP4 = b"\x00\x00\x00\x18ftypmp42"
ROOT = Path(__file__).resolve().parents[1]


def base_case() -> dict:
    return {
        "schema_version": "harness_case_spec_v1",
        "case_id": "demo_case",
        "capability_id": "rigid_body_contact_causality",
        "prompt": "A minimal parameterized case.",
        "physical_parameters": {"speed": 2.0, "friction": 0.3},
        "expected_physics": {},
        "objects": [{"id": "body", "initial_velocity_m_s": [2.0, 0.0, 0.0]}],
        "active_objects": ["body"],
        "passive_objects": [],
        "required_assets": [],
        "required_signals": [],
        "verifier_expectation": {"status": "pass"},
        "should_pass": True,
        "notes": "test",
    }


class VariantPlanTests(unittest.TestCase):
    def test_glass_speed_plan_recomputes_coupled_case_fields(self) -> None:
        plan = ROOT / "config" / "variant_plans" / "glass_panel_impact_speed.json"
        expected = {
            "impact_speed-1p0_m_s": (1.0, 4.0, -0.33, "cracked"),
            "baseline": (2.0, 16.0, -0.58, "shattered"),
            "impact_speed-3p0_m_s": (3.0, 36.0, -0.83, "burst"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for variant, (speed, energy, start_y, state) in expected.items():
                payload = materialize_variant(plan, variant, Path(tmp) / f"{variant}.json")
                self.assertEqual(payload["physical_parameters"]["impact_speed_m_s"], speed)
                self.assertEqual(payload["physical_parameters"]["nominal_incident_energy_j"], energy)
                self.assertEqual(payload["expected_physics"]["nominal_incident_energy_j"], energy)
                self.assertAlmostEqual(payload["objects"][0]["initial_position_m"][1], start_y)
                self.assertEqual(payload["objects"][0]["initial_velocity_m_s"][1], speed)
                self.assertEqual(payload["expected_physics"]["expected_damage_state"], state)

    def test_plan_records_full_space_but_selects_only_baseline_and_ofat_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case_path = root / "base.json"
            write_json(case_path, base_case())
            plan = create_variant_plan(
                case_path,
                case_route="rigid_collision/demo/v001_parameter_space",
                axes=[
                    {
                        "id": "speed",
                        "baseline": "medium",
                        "levels": [
                            {"id": "slow", "edits": {"/physical_parameters/speed": 1.0, "/objects/0/initial_velocity_m_s/0": 1.0}},
                            {"id": "medium", "edits": {"/physical_parameters/speed": 2.0, "/objects/0/initial_velocity_m_s/0": 2.0}},
                            {"id": "fast", "edits": {"/physical_parameters/speed": 3.0, "/objects/0/initial_velocity_m_s/0": 3.0}},
                        ],
                    },
                    {
                        "id": "friction",
                        "baseline": "normal",
                        "levels": [
                            {"id": "low", "edits": {"/physical_parameters/friction": 0.1}},
                            {"id": "normal", "edits": {"/physical_parameters/friction": 0.3}},
                        ],
                    },
                ],
            )
            self.assertEqual(plan["combination_count"], 6)
            self.assertEqual(
                [row["id"] for row in plan["selected_variants"]],
                ["baseline", "speed-slow", "speed-fast", "friction-low"],
            )

            plan_path = root / "plan.json"
            write_json(plan_path, plan)
            output = root / "speed-fast.json"
            materialized = materialize_variant(plan_path, "speed-fast", output)
            self.assertEqual(materialized["physical_parameters"]["speed"], 3.0)
            self.assertEqual(materialized["objects"][0]["initial_velocity_m_s"][0], 3.0)
            self.assertEqual(materialized["physical_parameters"]["friction"], 0.3)
            self.assertEqual(materialized["case_id"], "demo_case__speed-fast")
            self.assertEqual(read_json(output), materialized)

    def test_render_command_reuses_formal_iterator_with_explicit_condition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case_path = root / "base.json"
            plan_path = root / "plan.json"
            output = root / "variant.json"
            write_json(case_path, base_case())
            write_json(
                plan_path,
                create_variant_plan(
                    case_path,
                    case_route="rigid_collision/demo/v001_parameter_space",
                    axes=[
                        {
                            "id": "speed",
                            "baseline": "medium",
                            "levels": [
                                {"id": "medium", "edits": {"/physical_parameters/speed": 2.0}},
                                {"id": "fast", "edits": {"/physical_parameters/speed": 3.0}},
                            ],
                        }
                    ],
                ),
            )
            command = variant_render_command(
                plan_path,
                "speed-fast",
                output,
                python_executable="/python",
                formal=True,
            )
            self.assertEqual(command[0], "/python")
            self.assertIn("scripts/harness_iterate_case.py", command[1])
            self.assertEqual(command[2], str(output.resolve()))
            self.assertEqual(command[-4:], [
                "--case-route",
                "rigid_collision/demo/v001_parameter_space",
                "--condition",
                "speed-fast",
            ])

    def test_default_render_command_is_five_view_rgb_probe_without_an_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case_path = root / "base.json"
            plan_path = root / "plan.json"
            output = root / "variant.json"
            write_json(case_path, base_case())
            write_json(
                plan_path,
                create_variant_plan(
                    case_path,
                    case_route="rigid_collision/demo/v001_parameter_space",
                    axes=[
                        {
                            "id": "speed",
                            "baseline": "medium",
                            "levels": [
                                {"id": "medium", "edits": {"/physical_parameters/speed": 2.0}},
                                {"id": "fast", "edits": {"/physical_parameters/speed": 3.0}},
                            ],
                        }
                    ],
                ),
            )

            command = variant_render_command(
                plan_path,
                "speed-fast",
                output,
                python_executable="/python",
            )

            self.assertIn("scripts/harness_run_case.py", command[1])
            self.assertIn("--backend", command)
            self.assertEqual(command[command.index("--render-passes") + 1], "rgb")
            self.assertEqual(command[command.index("--mode") + 1], "rgb")
            self.assertEqual(
                command[command.index("--views") + 1],
                "front_static,side_static,top_down,tracking_subject,event_closeup",
            )

    def test_multimodal_probe_derives_both_mode_from_one_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case_path = root / "base.json"
            plan_path = root / "plan.json"
            output = root / "variant.json"
            write_json(case_path, base_case())
            write_json(
                plan_path,
                create_variant_plan(
                    case_path,
                    case_route="rigid_collision/demo/v001_parameter_space",
                    axes=[
                        {
                            "id": "speed",
                            "baseline": "medium",
                            "levels": [
                                {"id": "medium", "edits": {"/physical_parameters/speed": 2.0}},
                            ],
                        }
                    ],
                ),
            )

            command = variant_render_command(
                plan_path,
                "baseline",
                output,
                python_executable="/python",
                render_passes=("rgb", "depth", "segmentation"),
            )

            self.assertEqual(command[command.index("--render-passes") + 1], "rgb,depth,segmentation")
            self.assertEqual(command[command.index("--mode") + 1], "both")


class CaseOrganizationTests(unittest.TestCase):
    def test_complete_runs_gain_idempotent_hardlinked_variant_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            version = workspace / "cases" / "rigid_collision" / "demo" / "v001_parameter_space"
            run = version / "runs" / "speed_fast" / "demo_ue"
            view = run / "views" / "front_static"
            (view / "depth_frames").mkdir(parents=True)
            (view / "segmentation_frames").mkdir()
            for path in (
                view / "rgb.mp4",
                view / "depth_preview.mp4",
                view / "segmentation_preview.mp4",
            ):
                path.write_bytes(MP4)
            (view / "depth_frames" / "frame_000000.exr").write_bytes(b"depth")
            (view / "segmentation_frames" / "frame_000000.exr").write_bytes(b"segmentation")
            write_json(view / "meta.json", {"frame_count_rgb": 1})
            write_json(run / "manifest.json", {"schema_version": "world_model_run.v2.3"})
            case_payload = base_case()
            case_payload["variant_plan"] = {
                "schema_version": "harness_variant_plan_v1",
                "plan": "config/variant_plans/demo.json",
                "plan_sha256": "a" * 64,
                "variant": "speed-fast",
                "levels": {"speed": "fast"},
            }
            write_json(run / "case_spec.json", case_payload)
            dry_run = organize_workspace_cases(
                workspace,
                routes=["rigid_collision/demo/v001_parameter_space"],
                apply=False,
            )
            self.assertEqual(dry_run["organized_variant_count"], 1)
            self.assertEqual(dry_run["overall_generation_count"], 1)
            self.assertFalse((version / "variants").exists())

            for modality in ("rgb", "depth", "segmentation"):
                path = run / "overall" / f"{modality}.mp4"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(MP4)

            first = organize_workspace_cases(
                workspace,
                routes=["rigid_collision/demo/v001_parameter_space"],
                apply=True,
            )
            second = organize_workspace_cases(
                workspace,
                routes=["rigid_collision/demo/v001_parameter_space"],
                apply=True,
            )

            target = version / "variants" / "speed-fast"
            self.assertEqual(first["organized_variant_count"], 1)
            self.assertEqual(second["organized_variant_count"], 1)
            self.assertEqual(first["overall_generation_count"], 0)
            self.assertEqual(second["overall_generation_count"], 0)
            self.assertTrue((target / "rgb" / "front_static.mp4").samefile(view / "rgb.mp4"))
            self.assertTrue(
                (target / "depth" / "front_static" / "frames" / "frame_000000.exr").samefile(
                    view / "depth_frames" / "frame_000000.exr"
                )
            )
            self.assertTrue(
                (target / "segmentation" / "front_static" / "frames" / "frame_000000.exr").samefile(
                    view / "segmentation_frames" / "frame_000000.exr"
                )
            )
            self.assertTrue((target / "overall" / "rgb.mp4").samefile(run / "overall" / "rgb.mp4"))
            manifest = read_json(target / "variant_manifest.json")
            self.assertEqual(manifest["source_run"], str(run.resolve()))
            self.assertEqual(manifest["views"], ["front_static"])
            self.assertEqual(manifest["qualification"]["status"], "legacy_unverified")

    def test_organizer_uses_run_index_condition_and_rejects_git_or_symlink_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            version = workspace / "cases" / "rigid_collision" / "demo" / "v001_parameter_space"
            run = version / "runs" / "opaque_session" / "attempt_01" / "demo_ue"
            view = run / "views" / "front_static"
            (view / "depth_frames").mkdir(parents=True)
            (view / "segmentation_frames").mkdir()
            for path in (
                view / "rgb.mp4",
                view / "depth_preview.mp4",
                view / "segmentation_preview.mp4",
            ):
                path.write_bytes(MP4)
            (view / "depth_frames" / "frame_000000.exr").write_bytes(b"depth")
            (view / "segmentation_frames" / "frame_000000.exr").write_bytes(b"segmentation")
            write_json(
                version / "run_index.json",
                {
                    "schema_version": "harness_case_run_index_v1",
                    "case_route": "rigid_collision/demo/v001_parameter_space",
                    "sessions": [
                        {
                            "passing_runs": [
                                {
                                    "run_dir": str(run),
                                    "condition": "friction-low",
                                    "label": "opaque_session__attempt_01",
                                }
                            ]
                        }
                    ],
                },
            )
            report = organize_workspace_cases(workspace, apply=False)
            self.assertEqual(report["cases"][0]["variants"][0]["id"], "friction-low")

            with self.assertRaises(CaseLibraryError):
                organize_workspace_cases(ROOT, apply=False)

            alias = Path(tmp) / "workspace_alias"
            alias.symlink_to(workspace, target_is_directory=True)
            with self.assertRaises(CaseLibraryError):
                organize_workspace_cases(alias, apply=False)

    def test_organizer_never_reads_frames_through_an_internal_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = base / "workspace"
            run = (
                workspace
                / "cases"
                / "rigid_collision"
                / "demo"
                / "v001_parameter_space"
                / "runs"
                / "baseline"
                / "demo_ue"
            )
            view = run / "views" / "front_static"
            view.mkdir(parents=True)
            outside = base / "outside_depth"
            outside.mkdir()
            (outside / "frame_000000.exr").write_bytes(b"outside")
            (view / "depth_frames").symlink_to(outside, target_is_directory=True)
            (view / "segmentation_frames").mkdir()
            (view / "segmentation_frames" / "frame_000000.exr").write_bytes(b"seg")
            for path in (
                view / "rgb.mp4",
                view / "depth_preview.mp4",
                view / "segmentation_preview.mp4",
            ):
                path.write_bytes(MP4)

            report = organize_workspace_cases(workspace, apply=False)

            self.assertEqual(report["organized_variant_count"], 0)
            self.assertEqual(report["skipped_run_count"], 1)

    def test_case_catalog_preserves_all_excel_rows_and_only_links_existing_repo_artifacts(self) -> None:
        catalog = read_json(ROOT / "config" / "case_catalog.json")
        self.assertEqual(catalog["source"]["record_count"], 22)
        self.assertEqual(len(catalog["cases"]), 22)
        for case in catalog["cases"]:
            for case_spec in case["case_specs"]:
                self.assertTrue((ROOT / case_spec).is_file(), case_spec)
            for capability_id in case["capability_ids"]:
                self.assertTrue((ROOT / "capabilities" / f"{capability_id}.json").is_file(), capability_id)
            plan = catalog_case_plan(case["id"])
            self.assertEqual(plan["combination_count"], case["combination_count"])
            self.assertGreater(plan["selected_variant_count"], 0)


if __name__ == "__main__":
    unittest.main()
