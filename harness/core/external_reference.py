from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


REFERENCE_SCHEMA_VERSION = "harness_external_reference_v1"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024


def capture_external_reference(
    url: str,
    destination: str | Path,
    *,
    usage_note: str,
    license_note: str = "unverified",
    opener: Callable[..., Any] | None = None,
    resolver: Callable[..., Any] = socket.getaddrinfo,
    timeout: float = 30.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    _validate_public_https(url, resolver)
    if not usage_note.strip():
        raise ValueError("external reference usage_note must not be empty")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "PhysicsAwareHarness/1.0"}, method="GET")
    opener = opener or build_opener(_PublicHTTPSRedirectHandler(resolver)).open
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with opener(request, timeout=timeout) as response, os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            final_url = response.geturl()
            _validate_public_https(final_url, resolver)
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                byte_size += len(chunk)
                if byte_size > max_bytes:
                    raise ValueError(f"external reference exceeds {max_bytes} bytes")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
            headers = response.headers
        os.replace(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
    return {
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "source_url": _without_query(url),
        "resolved_url": _without_query(final_url),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "content_type": str(headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0],
        "etag": headers.get("ETag"),
        "last_modified": headers.get("Last-Modified"),
        "sha256": digest.hexdigest(),
        "byte_size": byte_size,
        "artifact_path": str(destination),
        "usage_note": usage_note.strip(),
        "license_note": license_note.strip() or "unverified",
        "claim_boundary": "This record proves retrieved bytes and source lineage only; it does not verify truth, license, or benchmark compatibility.",
    }


class _PublicHTTPSRedirectHandler(HTTPRedirectHandler):
    def __init__(self, resolver: Callable[..., Any]) -> None:
        self._resolver = resolver
        super().__init__()

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        _validate_public_https(newurl, self._resolver)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_public_https(url: str, resolver: Callable[..., Any] = socket.getaddrinfo) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("external references must use public HTTPS without credentials or fragments")
    if parsed.hostname.casefold() in {"localhost", "localhost.localdomain"}:
        raise ValueError("external reference host must be public")
    try:
        addresses = {row[4][0] for row in resolver(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
    except OSError as error:
        raise ValueError("external reference host could not be resolved") from error
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("external reference host must be public")


def _without_query(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
