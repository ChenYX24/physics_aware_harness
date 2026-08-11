from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from harness.core.capability import CapabilityStore


ROOT = Path(__file__).resolve().parents[1]
GENERIC_PHYSICS_CAPABILITIES = {"rigid_body_dynamics", "fluid_particle_dynamics", "deformable_body_dynamics"}


def extract_capability_profile(
    root: str | Path = ROOT,
    *,
    source_paths: Iterable[str] | None = None,
    source_preset: str = "public",
    include_private_sources: bool = False,
) -> dict[str, Any]:
    """Inventory registered execution domains and pipeline stages.

    Source text is recorded as provenance only. It is never keyword-classified
    into a prepared physical process.
    """
    root = Path(root)
    store_root = root / "capabilities"
    if not store_root.is_dir():
        store_root = ROOT / "capabilities"
    store = CapabilityStore(store_root)
    active = [item for item in store.list() if item.capability_type != "compatibility_alias"]
    selected = [item for item in active if item.id in GENERIC_PHYSICS_CAPABILITIES or item.capability_type != "physics_constraint"]
    provenance = []
    suppressed = []
    for relative in source_paths or ():
        path = root / relative
        private = any(part in {"agent-docs", ".agents", ".codex"} for part in path.parts)
        if private and not include_private_sources:
            suppressed.append(str(relative))
        elif path.is_file():
            provenance.append(str(relative))
    taxonomy = store.taxonomy(include_deprecated=False)
    taxonomy["physics_behavior_capabilities"] = sorted(GENERIC_PHYSICS_CAPABILITIES.intersection(item.id for item in active))
    return {
        "schema_version": "physics_aware_harness_capabilities_v2",
        "source_preset": source_preset,
        "source_files": provenance,
        "private_sources_suppressed": suppressed,
        "classification_policy": "state_domain_and_pipeline_stage_only",
        "capabilities": [item.to_summary() for item in selected],
        "capability_taxonomy": taxonomy,
    }


def render_markdown_report(profile: dict[str, Any]) -> str:
    lines = [
        "# Physics-Aware Harness Capability Profile",
        "",
        "Capabilities are solver/state domains and reusable pipeline stages. Named physical phenomena are case data, not routing classes.",
        "",
        "## Capability Taxonomy",
        "",
    ]
    for item in profile.get("capabilities") or []:
        lines.append(f"- `{item.get('id')}` — {item.get('description')}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inventory generic solver-domain and pipeline capabilities.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--output", default="config/harness_capability_profile.json")
    parser.add_argument("--report-output", default="docs/CAPABILITY_PROFILE.md")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root)
    profile = extract_capability_profile(root)
    output = root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(profile, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    report = root / args.report_output
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_markdown_report(profile), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
