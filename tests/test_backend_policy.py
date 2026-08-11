from __future__ import annotations

import unittest

from harness.runtime.backend_policy import backend_plan


class BackendPolicyTests(unittest.TestCase):
    def test_policies_are_state_domains_not_named_processes(self) -> None:
        self.assertEqual(backend_plan("rigid_body_dynamics")["preferred_backend"], "ue")
        self.assertEqual(backend_plan("fluid_particle_dynamics")["preferred_backend"], "genesis_sph")
        self.assertEqual(backend_plan("deformable_body_dynamics")["preferred_backend"], "taichi_cloth")

    def test_legacy_process_label_is_not_a_backend_route(self) -> None:
        self.assertEqual(backend_plan("rigid_body_gravity_collision")["status"], "unsupported")
        self.assertEqual(backend_plan("sequential_contact_propagation")["status"], "unsupported")

    def test_unknown_capability_is_not_silently_routed(self) -> None:
        self.assertEqual(backend_plan("unknown_effect")["status"], "unsupported")


if __name__ == "__main__":
    unittest.main()
