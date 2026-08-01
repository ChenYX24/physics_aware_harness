from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.assets.asset_registry import AssetRegistry, searchable_text
from harness.assets.asset_resolver import asset_quality_gate
from harness.core.artifact_schema import read_json
from harness.core.workspace import workspace_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the read-only asset qualification and runtime-binding browser."
    )
    parser.add_argument("--registry", help="Asset registry JSON; defaults to the workspace ADP catalog.")
    parser.add_argument(
        "--binding-report",
        action="append",
        default=[],
        help="Optional runtime_actor_placement.json; repeat to merge runtime evidence.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the browser after the server starts.")
    return parser


def default_registry_path() -> Path:
    configured = os.environ.get("SIM_STUDIO_ASSET_REGISTRY")
    candidates = [
        Path(configured).expanduser() if configured else None,
        workspace_root() / "catalog" / "adp" / "asset_registry.local.json",
        ROOT / "assets" / "asset_registry.local.json",
        ROOT / "assets" / "asset_registry.example.json",
    ]
    path = next(
        (path for path in candidates if path is not None and path.is_file()),
        None,
    )
    if path is None:
        raise FileNotFoundError("no asset registry found; pass --registry")
    return path


def load_binding_evidence(paths: list[Path]) -> dict[str, list[dict[str, Any]]]:
    evidence: dict[str, list[dict[str, Any]]] = {}
    for path in paths:
        payload = read_json(path)
        if not isinstance(payload, dict):
            raise ValueError(f"binding report must be an object: {path}")
        for binding in payload.get("actor_bindings") or []:
            if not isinstance(binding, dict):
                continue
            asset = binding.get("asset") if isinstance(binding.get("asset"), dict) else {}
            asset_id = asset.get("selected_asset_id")
            if not isinstance(asset_id, str) or not asset_id:
                continue
            physics = binding.get("physics") if isinstance(binding.get("physics"), dict) else {}
            evidence.setdefault(asset_id, []).append(
                {
                    "case_id": payload.get("case_id"),
                    "object_id": binding.get("object_id"),
                    "runtime_actor_id": binding.get("runtime_actor_id"),
                    "binding_source": asset.get("binding_source"),
                    "runtime_usage": asset.get("runtime_usage"),
                    "collision_geometry_verification": physics.get(
                        "collision_geometry_verification"
                    ),
                    "report": str(path),
                }
            )
    return evidence


def is_physics_candidate(asset: dict[str, Any]) -> bool:
    kind = " ".join(
        str(asset.get(key) or "")
        for key in ("category", "category_l1", "type", "asset_kind")
    ).casefold()
    return any(
        token in kind
        for token in ("physics_critical", "staticmesh", "static_mesh", "geometrycollection")
    )


def build_asset_view(
    asset: dict[str, Any],
    binding_evidence: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    asset_id = str(asset.get("asset_id") or asset.get("id") or asset.get("name") or "")
    acquisition = asset.get("acquisition") if isinstance(asset.get("acquisition"), dict) else {}
    adp = asset.get("adp") if isinstance(asset.get("adp"), dict) else {}
    ue = asset.get("ue") if isinstance(asset.get("ue"), dict) else {}
    source_kind = str(asset.get("source_kind") or "unknown")
    ue_path = str(asset.get("ue_path") or "")
    built_in = source_kind in {"engine_builtin", "analytic_proxy"} or ue_path.startswith(
        ("/Engine/", "/Script/")
    )
    raw_dependencies = ue.get("dependencies")
    dependencies = raw_dependencies if isinstance(raw_dependencies, list) else []
    materialized_dependencies = adp.get("dependency_materialized_count")
    dependencies_ready = (
        not dependencies
        or materialized_dependencies is None
        or materialized_dependencies == len(dependencies)
    )
    catalog_ready = bool(
        ue_path
        and (
            built_in
            or asset.get("materialized")
            or acquisition.get("status") == "materialized"
        )
        and dependencies_ready
    )
    physics_candidate = is_physics_candidate(asset)
    execution_gate = asset_quality_gate(
        asset,
        physics_critical=physics_candidate,
        allow_local_preview=True,
    )
    reference_gate = asset_quality_gate(
        asset,
        physics_critical=physics_candidate,
        allow_local_preview=False,
    )
    runtime_evidence = binding_evidence.get(asset_id, [])
    binding_status = (
        "runtime_verified"
        if runtime_evidence
        else "catalog_ready"
        if catalog_ready
        else "blocked"
    )
    qualification = (
        "reference_ready"
        if reference_gate["status"] == "pass"
        else "local_preview"
        if catalog_ready and execution_gate["status"] in {"pass", "pass_local_preview"}
        else "blocked"
    )
    thumbnail = asset.get("thumbnail") or (
        (asset.get("paths") or {}).get("thumbnail")
        if isinstance(asset.get("paths"), dict)
        else None
    )
    return {
        "asset_id": asset_id,
        "name": asset.get("semantic_name") or asset.get("name") or asset_id,
        "technical_name": asset.get("name") or asset_id,
        "description": asset.get("description") or "",
        "category": asset.get("category_l1") or asset.get("category") or "uncategorized",
        "subcategory": asset.get("category_l2") or "",
        "type": asset.get("type") or ue.get("class_name") or "unknown",
        "source_kind": source_kind,
        "quality_status": asset.get("quality_status") or "unknown",
        "license": asset.get("license") or "missing",
        "ue_path": ue_path,
        "materialized": bool(built_in or asset.get("materialized")),
        "thumbnail": str(thumbnail) if thumbnail else None,
        "tags": asset.get("tags") or [],
        "dependency_count": len(dependencies),
        "dependencies_ready": dependencies_ready,
        "physics_candidate": physics_candidate,
        "qualification": qualification,
        "binding_status": binding_status,
        "execution_gate": execution_gate,
        "reference_gate": reference_gate,
        "runtime_evidence": runtime_evidence,
        "raw": asset,
    }


def filter_assets(
    assets: list[dict[str, Any]],
    *,
    query: str = "",
    category: str = "",
    qualification: str = "",
    binding: str = "",
    source: str = "",
) -> list[dict[str, Any]]:
    tokens = [token for token in query.casefold().split() if token]
    rows = []
    for row in assets:
        if tokens and not all(token in row["_search"] for token in tokens):
            continue
        if category and row["category"] != category:
            continue
        if qualification and row["qualification"] != qualification:
            continue
        if binding and row["binding_status"] != binding:
            continue
        if source and row["source_kind"] != source:
            continue
        rows.append(row)
    return rows


def compact_asset(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if key not in {"raw", "_search", "execution_gate", "reference_gate", "runtime_evidence", "thumbnail"}
    } | {
        "thumbnail_url": f"/api/thumbnail?id={quote(row['asset_id'], safe='')}"
        if row["thumbnail"]
        else None
    }


def facets(assets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    def counts(key: str) -> list[dict[str, Any]]:
        return [
            {"value": value, "count": count}
            for value, count in sorted(Counter(row[key] for row in assets).items())
        ]

    return {
        "categories": counts("category"),
        "qualifications": counts("qualification"),
        "bindings": counts("binding_status"),
        "sources": counts("source_kind"),
    }


class AssetBrowserHandler(BaseHTTPRequestHandler):
    assets: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    asset_facets: dict[str, list[dict[str, Any]]] = {}
    registry_path: Path

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/asset_browser.html"}:
            return self.send_file(ROOT / "tools" / "asset_browser.html")
        if parsed.path == "/asset_browser.js":
            return self.send_file(ROOT / "tools" / "asset_browser.js")
        if parsed.path == "/case_parameter_editor.html":
            return self.send_file(ROOT / "tools" / "case_parameter_editor.html")
        if parsed.path == "/case_parameter_editor.js":
            return self.send_file(ROOT / "tools" / "case_parameter_editor.js")
        if parsed.path == "/config/variant_plans/glass_panel_impact_speed.json":
            return self.send_file(
                ROOT / "config" / "variant_plans" / "glass_panel_impact_speed.json"
            )
        if (
            parsed.path
            == "/cases/fracture/glass_energy_response_matrix/glass_panel_e16_shatter.json"
        ):
            return self.send_file(
                ROOT
                / "cases"
                / "fracture"
                / "glass_energy_response_matrix"
                / "glass_panel_e16_shatter.json"
            )
        if parsed.path == "/api/assets":
            return self.send_assets(parse_qs(parsed.query))
        if parsed.path == "/api/asset":
            return self.send_asset(parse_qs(parsed.query))
        if parsed.path == "/api/thumbnail":
            return self.send_thumbnail(parse_qs(parsed.query))
        self.send_error(404)

    def send_assets(self, query: dict[str, list[str]]) -> None:
        value = lambda key: unquote((query.get(key) or [""])[0]).strip()
        try:
            offset = max(0, int(value("offset") or 0))
            limit = min(96, max(1, int(value("limit") or 36)))
        except ValueError:
            return self.send_error(400, "offset and limit must be integers")
        rows = filter_assets(
            self.assets,
            query=value("q"),
            category=value("category"),
            qualification=value("qualification"),
            binding=value("binding"),
            source=value("source"),
        )
        self.send_json(
            {
                "schema_version": "harness_asset_browser_page_v1",
                "registry": str(self.registry_path),
                "total": len(self.assets),
                "filtered": len(rows),
                "offset": offset,
                "limit": limit,
                "facets": self.asset_facets,
                "assets": [compact_asset(row) for row in rows[offset : offset + limit]],
            }
        )

    def send_asset(self, query: dict[str, list[str]]) -> None:
        asset_id = unquote((query.get("id") or [""])[0])
        row = self.by_id.get(asset_id)
        if row is None:
            return self.send_error(404, "unknown asset id")
        self.send_json(
            {
                "schema_version": "harness_asset_browser_detail_v1",
                **{key: value for key, value in row.items() if key != "_search"},
                "thumbnail_url": f"/api/thumbnail?id={quote(asset_id, safe='')}"
                if row["thumbnail"]
                else None,
            }
        )

    def send_thumbnail(self, query: dict[str, list[str]]) -> None:
        asset_id = unquote((query.get("id") or [""])[0])
        row = self.by_id.get(asset_id)
        path = Path(row["thumbnail"]).expanduser() if row and row["thumbnail"] else None
        if path is None or not path.is_file() or path.is_symlink():
            return self.send_error(404, "thumbnail unavailable")
        self.send_file(path)

    def send_json(self, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path: Path) -> None:
        if not path.is_file():
            return self.send_error(404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    registry_path = (
        Path(args.registry).expanduser().resolve(strict=True)
        if args.registry
        else default_registry_path().resolve(strict=True)
    )
    report_paths = [
        Path(path).expanduser().resolve(strict=True) for path in args.binding_report
    ]
    registry = AssetRegistry(registry_path)
    evidence = load_binding_evidence(report_paths)
    views = [build_asset_view(asset, evidence) for asset in registry.assets]
    for row in views:
        row["_search"] = searchable_text(row["raw"])

    AssetBrowserHandler.assets = views
    AssetBrowserHandler.by_id = {row["asset_id"]: row for row in views}
    AssetBrowserHandler.asset_facets = facets(views)
    AssetBrowserHandler.registry_path = registry_path
    server = ThreadingHTTPServer((args.host, args.port), AssetBrowserHandler)
    url = f"http://{args.host}:{server.server_port}/"
    print(
        json.dumps(
            {
                "schema_version": "harness_asset_browser_server_v1",
                "url": url,
                "registry": str(registry_path),
                "asset_count": len(views),
                "runtime_evidence_assets": len(evidence),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
