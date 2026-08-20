from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.runtime.fluid_surface_adapter import prepare_ue_surface_replay
from harness.runtime.stage_contracts import stage_handoff_contract


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a Genesis surface cache for deterministic UE mesh replay.")
    parser.add_argument("particle_cache")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ue-asset-root", default="/Game/HarnessGenerated/Fluid/FluidDropV001")
    args = parser.parse_args()
    handoff = stage_handoff_contract("genesis_sph", "ue")
    if handoff is None:
        raise RuntimeError("Genesis-to-UE particle handoff contract is unavailable")
    manifest = prepare_ue_surface_replay(
        args.particle_cache,
        args.output_dir,
        ue_asset_root=args.ue_asset_root,
        spatial_measurement_tolerance=float(handoff["numeric_tolerances"]["spatial_measurement_absolute"]),
    )
    print(
        json.dumps(
            {
                "status": "completed",
                "manifest": str(Path(args.output_dir).resolve() / "fluid_surface_replay.json"),
                "frame_count": manifest["timebase"]["frame_count"],
                "ue_asset_root": manifest["ue"]["asset_root"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
