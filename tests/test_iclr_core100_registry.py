from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.validate_iclr_core100_registry import validate_experiment


class ICLRCore100RegistryTests(unittest.TestCase):
    def test_validates_manifest_quotas_and_contiguous_pilot_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "registry").mkdir()
            manifest = {
                "schema_version": "harness_iclr_core100_experiment_v1",
                "protocol": {"evaluated_case_count": 2, "development_pilot_count": 1},
                "quotas": {
                    "domains": {"rigid_body_dynamics": 1, "fluid_particle_dynamics": 1},
                    "input_modes": {"text": 1, "text_image": 1},
                },
                "registries": {"cases": "registry/core100_cases.csv"},
            }
            (root / "experiment_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            columns = [
                "case_id", "domain", "family", "title", "input_mode", "scene_class",
                "contract_focus", "readiness_tier", "pilot_order", "status",
            ]
            with (root / "registry" / "core100_cases.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerows(
                    [
                        {"case_id": "R001", "domain": "rigid_body_dynamics", "family": "a", "title": "a", "input_mode": "text", "scene_class": "stage", "contract_focus": "a", "readiness_tier": "A", "pilot_order": "1", "status": "pilot_selected"},
                        {"case_id": "F001", "domain": "fluid_particle_dynamics", "family": "b", "title": "b", "input_mode": "text_image", "scene_class": "stage", "contract_focus": "b", "readiness_tier": "B", "pilot_order": "", "status": "planned"},
                    ]
                )

            report = validate_experiment(root)

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["pilot_case_ids"], ["R001"])


if __name__ == "__main__":
    unittest.main()
