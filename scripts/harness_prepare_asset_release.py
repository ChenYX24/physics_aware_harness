#!/usr/bin/env python3
"""Build a public-release audit bundle without copying unlicensed assets."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "harness_asset_release_v1"
UNVERIFIED_LICENSE_TERMS = ("unknown", "unverified", "pending")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_content_path(value: Any, content_root: Path) -> str | None:
    if not value:
        return None
    path = Path(str(value)).expanduser().resolve()
    try:
        return path.relative_to(content_root).as_posix()
    except ValueError:
        return None


def redistribution_gate(asset: dict[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    license_name = str(asset.get("license") or "").strip()
    source_uri = str(asset.get("source_uri") or "").strip()
    redistribution = asset.get("redistribution") if isinstance(asset.get("redistribution"), dict) else {}

    if not license_name or any(term in license_name.casefold() for term in UNVERIFIED_LICENSE_TERMS):
        failures.append("missing_or_unverified_license")
    if not source_uri:
        failures.append("missing_source_uri")
    if redistribution.get("allowed") is not True:
        failures.append("redistribution_not_explicitly_allowed")
    for field in ("rights_holder", "evidence_uri", "verified_at"):
        if not str(redistribution.get(field) or "").strip():
            failures.append(f"missing_redistribution_{field}")
    return not failures, failures


def prepare_release(registry_path: Path, content_root: Path, output_dir: Path) -> dict[str, Any]:
    registry_path = registry_path.expanduser().resolve()
    content_root = content_root.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not registry_path.is_file():
        raise ValueError(f"registry does not exist: {registry_path}")
    if not content_root.is_dir():
        raise ValueError(f"content root does not exist: {content_root}")
    try:
        output_dir.relative_to(content_root)
    except ValueError:
        pass
    else:
        raise ValueError("output directory must be outside the content root")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assets = registry.get("assets")
    if not isinstance(assets, list):
        raise ValueError("registry.assets must be a list")

    asset_rows: list[dict[str, Any]] = []
    file_references: dict[str, list[bool]] = defaultdict(list)
    blocker_counts: Counter[str] = Counter()
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        eligible, blockers = redistribution_gate(asset)
        blocker_counts.update(blockers)
        adp = asset.get("adp") if isinstance(asset.get("adp"), dict) else {}
        primary = relative_content_path(adp.get("repo_file"), content_root)
        dependencies = sorted(
            path
            for value in adp.get("dependency_files") or []
            if (path := relative_content_path(value, content_root))
        )
        if primary:
            file_references[primary].append(eligible)
        for dependency in dependencies:
            file_references[dependency].append(eligible)
        if not primary:
            eligible = False
            blockers = [*blockers, "missing_or_external_content_file"]
            blocker_counts["missing_or_external_content_file"] += 1
        ue = asset.get("ue") if isinstance(asset.get("ue"), dict) else {}
        redistribution = asset.get("redistribution") if isinstance(asset.get("redistribution"), dict) else {}
        asset_rows.append(
            {
                "asset_id": asset.get("asset_id"),
                "name": asset.get("name"),
                "category_l1": asset.get("category_l1"),
                "category_l2": asset.get("category_l2"),
                "class_name": ue.get("class_name"),
                "ue_path": asset.get("ue_path"),
                "content_file": primary,
                "dependency_files": dependencies,
                "source_kind": asset.get("source_kind"),
                "source_uri": asset.get("source_uri"),
                "license": asset.get("license"),
                "redistribution": {
                    key: redistribution.get(key)
                    for key in ("allowed", "rights_holder", "evidence_uri", "verified_at")
                },
                "publication_eligible": eligible,
                "publication_blockers": blockers,
            }
        )

    file_rows: list[dict[str, Any]] = []
    total_bytes = 0
    eligible_bytes = 0
    for path in sorted(path for path in content_root.rglob("*") if path.is_file()):
        relative = path.relative_to(content_root).as_posix()
        size = path.stat().st_size
        references = file_references.get(relative, [])
        eligible = bool(references) and all(references)
        digest = sha256_file(path)
        total_bytes += size
        if eligible:
            eligible_bytes += size
        file_rows.append(
            {
                "path": relative,
                "size_bytes": size,
                "sha256": digest,
                "publication_eligible": eligible,
                "publication_blocker": None if eligible else (
                    "blocked_asset_reference" if references else "unmapped_or_unverified_file"
                ),
            }
        )

    eligible_assets = sum(bool(row["publication_eligible"]) for row in asset_rows)
    eligible_files = sum(bool(row["publication_eligible"]) for row in file_rows)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "asset_count": len(asset_rows),
        "eligible_asset_count": eligible_assets,
        "blocked_asset_count": len(asset_rows) - eligible_assets,
        "file_count": len(file_rows),
        "eligible_file_count": eligible_files,
        "blocked_file_count": len(file_rows) - eligible_files,
        "total_bytes": total_bytes,
        "eligible_bytes": eligible_bytes,
        "publication_ready": eligible_assets == len(asset_rows) and eligible_files == len(file_rows),
        "blocker_counts": dict(sorted(blocker_counts.items())),
    }

    write_json(output_dir / "release_summary.json", summary)
    write_jsonl(output_dir / "assets.jsonl", asset_rows)
    write_jsonl(output_dir / "files.jsonl", file_rows)
    (output_dir / "checksums.sha256").write_text(
        "".join(f"{row['sha256']}  Content/{row['path']}\n" for row in file_rows),
        encoding="utf-8",
    )
    (output_dir / "blocked_assets.tsv").write_text(
        "asset_id\tlicense\tblockers\n"
        + "".join(
            f"{row['asset_id']}\t{row['license'] or ''}\t{','.join(row['publication_blockers'])}\n"
            for row in asset_rows
            if not row["publication_eligible"]
        ),
        encoding="utf-8",
    )
    (output_dir / "README.md").write_text(release_readme(summary), encoding="utf-8")
    return summary


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def release_readme(summary: dict[str, Any]) -> str:
    status = "PASS" if summary["publication_ready"] else "BLOCKED"
    return f"""---
license: other
---

# Physics-Aware Harness Asset Release Audit

Publication gate: **{status}**

| Item | Total | Eligible | Blocked |
|---|---:|---:|---:|
| Indexed assets | {summary['asset_count']} | {summary['eligible_asset_count']} | {summary['blocked_asset_count']} |
| Content files | {summary['file_count']} | {summary['eligible_file_count']} | {summary['blocked_file_count']} |

This audit bundle contains metadata and SHA-256 checksums only. Binary UE assets may be uploaded only when every asset and dependency has an explicit redistribution grant, rights holder, evidence URI, verification date, source URI, and verified license. A repository-level software license does not relicense third-party content.

Files:

- `release_summary.json`: publication decision and counts.
- `assets.jsonl`: sanitized asset, dependency, source, license, and redistribution records.
- `files.jsonl`: relative file paths, sizes, hashes, and file-level gates.
- `blocked_assets.tsv`: actionable license/provenance backlog.
- `checksums.sha256`: content integrity manifest.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--content-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    summary = prepare_release(args.registry, args.content_root, args.output_dir)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
