from __future__ import annotations

from typing import Any

from harness.verification.ordered_contact_verifier import verify_ordered_contact_propagation


def verify_domino(
    case_spec: dict[str, Any],
    trajectory: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    """Backward-compatible entry point for legacy callers."""
    return verify_ordered_contact_propagation(case_spec, trajectory)
