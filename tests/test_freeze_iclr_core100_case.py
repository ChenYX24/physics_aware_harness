from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from harness.core.artifact_schema import read_json
from scripts.freeze_iclr_core100_case import freeze_case
from scripts.validate_iclr_core100_registry import validate_experiment


ROOT = Path(__file__).resolve().parents[1]


class FreezeICLRCore100CaseTests(unittest.TestCase):
    def test_freezes_text_case_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "registry").mkdir()
            manifest = {
                "schema_version": "harness_iclr_core100_experiment_v1",
                "experiment_id": "test_core100",
                "protocol": {"evaluated_case_count": 1, "development_pilot_count": 1},
                "quotas": {
                    "domains": {"rigid_body_dynamics": 1},
                    "input_modes": {"text": 1},
                },
                "registries": {"cases": "registry/core100_cases.csv", "status": "status/status_snapshot.json"},
            }
            (root / "experiment_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            columns = [
                "case_id", "domain", "family", "title", "input_mode", "scene_class",
                "contract_focus", "readiness_tier", "pilot_order", "status",
            ]
            with (root / "registry" / "core100_cases.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerow(
                    {
                        "case_id": "R001", "domain": "rigid_body_dynamics", "family": "gravity",
                        "title": "falling_block", "input_mode": "text", "scene_class": "controlled_stage",
                        "contract_focus": "contact", "readiness_tier": "A", "pilot_order": "1",
                        "status": "pilot_selected",
                    }
                )
            (root / "status").mkdir()
            (root / "status" / "status_snapshot.json").write_text(
                json.dumps(
                    {
                        "generated_at": "old", "phase": "pilot_setup",
                        "counts": {"assets_ready": 0, "frozen": 0},
                        "current_case_id": "R001", "current_job_id": None, "next_action": "freeze",
                    }
                ),
                encoding="utf-8",
            )
            case_spec = read_json(ROOT / "cases_v2" / "representative" / "falling_block_on_floor.json")
            case_spec["identity"]["case_id"] = "R001"
            case_spec["constraints"] = []
            for obj in case_spec["objects"]:
                obj["visual_representation"] = {"source": "asset", "visible": True}
            case_path = root / "prepared_case.json"
            case_path.write_text(json.dumps(case_spec), encoding="utf-8")
            assets = []
            for index, obj in enumerate(case_spec["objects"]):
                route = ((obj.get("asset") or {}).get("acquisition") or {}).get("route", "default")
                assets.append(
                    {
                        "intent": {"object_id": obj["id"]},
                        "acquisition": {"requested": {"route": route}, "actual_route": route, "route_honored": True},
                        "selected_asset": {
                            "asset_id": f"asset.{obj['id']}", "source_kind": "engine_builtin",
                            "license_tier": "local_preview", "sha256": "a" * 64,
                            ("quality_gate" if index == 0 else "qualification"): {"status": "pass_local_preview"},
                            "backend_bindings": {
                                "unreal": {"object_path": f"/Engine/{obj['id']}", "runtime_ready": True}
                            },
                        },
                    }
                )
            resolution = {
                "assets": assets,
                "scene_map": {
                    "selected_asset": {
                        "asset_id": "Map_Test", "source_kind": "local_generated",
                        "license_tier": "local_preview", "sha256": "b" * 64,
                        "quality_gate": {"status": "pass_local_preview", "license_tier": "local_preview"},
                        "backend_bindings": {
                            "ue_5_7": {"object_path": "/Game/Test.Test", "runtime_ready": True}
                        },
                    }
                },
            }
            resolution["assets"][0]["selected_asset"]["sha256"] = ""
            resolution["scene_map"]["selected_asset"]["sha256"] = ""
            resolution_path = root / "resolution.json"
            resolution_path.write_text(json.dumps(resolution), encoding="utf-8")

            first = freeze_case(
                root, case_id="R001", case_spec_path=case_path, asset_resolution_path=resolution_path,
                source_branch="feat/test", source_commit="abc123", frozen_at="2026-08-27T00:00:00+00:00",
            )
            second = freeze_case(
                root, case_id="R001", case_spec_path=case_path, asset_resolution_path=resolution_path,
                source_branch="feat/test", source_commit="abc123", frozen_at="later",
            )

            self.assertEqual(first, second)
            status = read_json(root / "status" / "status_snapshot.json")
            self.assertEqual(status["counts"]["frozen"], 1)
            self.assertEqual(status["phase"], "pilot_frozen_waiting_development_run")
            self.assertEqual(status["next_action"], "run_development_pilot_R001_no_candidate_job_created")
            with (root / "registry" / "core100_cases.csv").open(encoding="utf-8", newline="") as stream:
                self.assertEqual(next(csv.DictReader(stream))["status"], "frozen")
            self.assertEqual(first["candidate_jobs_created"], 0)
            self.assertEqual(first["historical_jobs_promoted"], 0)
            self.assertEqual(read_json(root / "cases" / "R001" / "asset_lock.json")["objects"][0]["content_sha256"], "")
            self.assertEqual(read_json(root / "cases" / "R001" / "asset_lock.json")["scene_map"]["content_sha256"], "")
            self.assertEqual(read_json(root / "cases" / "R001" / "asset_lock.json")["scene_map"]["license_tier"], "local_preview")
            self.assertEqual(validate_experiment(root)["frozen_case_ids"], ["R001"])

    def test_freezes_and_validates_text_image_condition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "registry").mkdir()
            manifest = {
                "schema_version": "harness_iclr_core100_experiment_v1",
                "experiment_id": "test_core100",
                "protocol": {"evaluated_case_count": 1, "development_pilot_count": 1},
                "quotas": {"domains": {"rigid_body_dynamics": 1}, "input_modes": {"text_image": 1}},
                "registries": {"cases": "registry/core100_cases.csv", "status": "status/status_snapshot.json"},
            }
            (root / "experiment_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            columns = [
                "case_id", "domain", "family", "title", "input_mode", "scene_class",
                "contract_focus", "readiness_tier", "pilot_order", "status",
            ]
            with (root / "registry" / "core100_cases.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerow(
                    {
                        "case_id": "R011", "domain": "rigid_body_dynamics", "family": "rolling",
                        "title": "can_rolls_off_table", "input_mode": "text_image", "scene_class": "home",
                        "contract_focus": "support_loss", "readiness_tier": "A", "pilot_order": "1",
                        "status": "pilot_selected",
                    }
                )
            (root / "status").mkdir()
            (root / "status" / "status_snapshot.json").write_text(
                json.dumps(
                    {
                        "generated_at": "old", "phase": "pilot_setup", "counts": {"assets_ready": 0, "frozen": 0},
                        "current_case_id": "R011", "current_job_id": None, "next_action": "freeze",
                    }
                ),
                encoding="utf-8",
            )
            case_spec = read_json(ROOT / "cases_v2" / "representative" / "falling_block_on_floor.json")
            case_spec["identity"]["case_id"] = "R011"
            case_spec["constraints"] = []
            for obj in case_spec["objects"]:
                obj["visual_representation"] = {"source": "asset", "visible": True}
            case_path = root / "prepared_case.json"
            case_path.write_text(json.dumps(case_spec), encoding="utf-8")
            assets = []
            for obj in case_spec["objects"]:
                route = ((obj.get("asset") or {}).get("acquisition") or {}).get("route", "default")
                assets.append(
                    {
                        "intent": {"object_id": obj["id"]},
                        "acquisition": {"requested": {"route": route}, "actual_route": route, "route_honored": True},
                        "selected_asset": {
                            "asset_id": f"asset.{obj['id']}", "source_kind": "engine_builtin",
                            "license_tier": "local_preview", "sha256": "",
                            "qualification": {"status": "pass_local_preview"},
                            "backend_bindings": {
                                "unreal": {"object_path": f"/Engine/{obj['id']}", "runtime_ready": True}
                            },
                        },
                    }
                )
            resolution_path = root / "resolution.json"
            resolution_path.write_text(
                json.dumps(
                    {
                        "assets": assets,
                        "scene_map": {
                            "selected_asset": {
                                "asset_id": "Map_Test", "source_kind": "local_generated",
                                "license_tier": "local_preview", "sha256": "",
                                "quality_gate": {"status": "pass_local_preview"},
                                "backend_bindings": {
                                    "ue_5_7": {"object_path": "/Game/Test.Test", "runtime_ready": True}
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            image = root / "condition.png"
            image.write_bytes(b"test-image-condition")

            receipt = freeze_case(
                root, case_id="R011", case_spec_path=case_path, asset_resolution_path=resolution_path,
                source_branch="feat/test", source_commit="abc123", image_paths=[image],
                frozen_at="2026-08-27T00:00:00+00:00",
            )

            frozen = root / "cases" / "R011" / "inputs" / "request_image_0.png"
            request = read_json(root / "cases" / "R011" / "request.json")
            self.assertEqual(frozen.read_bytes(), image.read_bytes())
            self.assertEqual(request["input_mode"], "text_image")
            self.assertEqual(request["input_images"][0]["path"], "cases/R011/inputs/request_image_0.png")
            self.assertEqual(len(receipt["artifacts"]), 5)
            self.assertEqual(validate_experiment(root)["status"], "pass")


if __name__ == "__main__":
    unittest.main()
