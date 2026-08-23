from __future__ import annotations

import unittest

from harness.assets.asset_resolver import asset_quality_gate


class ProvenanceReleaseOverrideTests(unittest.TestCase):
    def test_asset_rights_and_identity_findings_become_nonblocking(self) -> None:
        asset = {
            "ue_path": "/Game/Test/Asset.Asset",
            "materialized": True,
            "local_path": __file__,
            "license": "UNVERIFIED",
            "license_tier": "blocked",
            "quality_status": "pending",
            "sha256": "0" * 64,
        }

        report = asset_quality_gate(
            asset,
            physics_critical=False,
            ignore_provenance_and_release_gates=True,
        )

        self.assertEqual(report["status"], "pass_local_preview")
        self.assertEqual(report["execution_failure_codes"], [])
        self.assertTrue(report["ignored_provenance_and_release_codes"])
        self.assertFalse(report["reference_approved"])


if __name__ == "__main__":
    unittest.main()
