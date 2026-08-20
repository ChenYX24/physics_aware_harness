from __future__ import annotations

import hashlib
import socket
import tempfile
import unittest
from pathlib import Path

from harness.core.external_reference import _PublicHTTPSRedirectHandler, capture_external_reference


def public_resolver(*_args: object, **_kwargs: object) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


class FakeResponse:
    def __init__(self, body: bytes, url: str) -> None:
        self.body = body
        self.url = url
        self.headers = {"Content-Type": "text/plain; charset=utf-8", "ETag": '"v1"'}

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body)
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class ExternalReferenceTests(unittest.TestCase):
    def test_capture_writes_bytes_and_redacts_query_from_lineage(self) -> None:
        body = b"official source"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "source.txt"
            record = capture_external_reference(
                "https://docs.example/spec?signature=secret",
                output,
                usage_note="Physics contract reference",
                opener=lambda *_args, **_kwargs: FakeResponse(body, "https://cdn.example/spec?token=secret"),
                resolver=public_resolver,
            )

            self.assertEqual(output.read_bytes(), body)
            self.assertEqual(record["sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(record["source_url"], "https://docs.example/spec")
            self.assertEqual(record["resolved_url"], "https://cdn.example/spec")

    def test_capture_rejects_local_or_non_https_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for url in ("http://example/spec", "https://127.0.0.1/spec"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    capture_external_reference(url, Path(temporary) / "out", usage_note="test")

    def test_capture_rejects_dns_names_resolving_to_private_addresses(self) -> None:
        private_resolver = lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443))
        ]
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(ValueError):
            capture_external_reference(
                "https://internal.example/spec",
                Path(temporary) / "out",
                usage_note="test",
                opener=lambda *_args, **_kwargs: self.fail("network must not be reached"),
                resolver=private_resolver,
            )

    def test_redirect_is_validated_before_following(self) -> None:
        handler = _PublicHTTPSRedirectHandler(
            lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        )
        with self.assertRaises(ValueError):
            handler.redirect_request(
                object(), None, 302, "Found", {}, "https://redirect.example/private"
            )


if __name__ == "__main__":
    unittest.main()
