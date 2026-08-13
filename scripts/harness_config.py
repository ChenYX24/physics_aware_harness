from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.core.harness_config import load_harness_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect the strict effective Physics-Aware Harness configuration")
    parser.add_argument("--config", help="Config document; relative paths resolve from the inner repository root.")
    parser.add_argument("--workspace")
    parser.add_argument("--catalog")
    parser.add_argument("--ue-project")
    parser.add_argument("--ue-executable")
    parser.add_argument("--codex-executable")
    parser.add_argument("--planning-base-url")
    parser.add_argument("--planning-model")
    parser.add_argument("--planning-image-capability", choices=["supported", "unsupported", "unknown"])
    parser.add_argument("--planning-api-key-env")
    parser.add_argument("--meshy-api-key-env")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    overrides = {
        "paths.workspace": args.workspace,
        "paths.catalog": args.catalog,
        "paths.ue_project": args.ue_project,
        "paths.ue_executable": args.ue_executable,
        "codex_reviewer.executable": args.codex_executable,
        "planning_llm.base_url": args.planning_base_url,
        "planning_llm.model": args.planning_model,
        "planning_llm.image_capability": args.planning_image_capability,
        "planning_llm.api_key_env": args.planning_api_key_env,
        "meshy.api_key_env": args.meshy_api_key_env,
    }
    config = load_harness_config(config_path=args.config, cli_overrides=overrides)
    print(json.dumps(config.inspect(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
