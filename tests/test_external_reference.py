from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from harness.core.external_reference import capture_external_reference


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


if __name__ == "__main__":
    unittest.main()
