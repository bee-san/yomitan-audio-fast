from __future__ import annotations

import gc
import http.client
import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit


ROOT = Path(__file__).parents[1]
PACKAGE = "_local_audio_fast_http_semantics_test_addon"
os.environ["LOCAL_AUDIO_FAST_STANDALONE"] = "1"
specification = importlib.util.spec_from_file_location(
    PACKAGE,
    ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
assert specification is not None and specification.loader is not None
module = importlib.util.module_from_spec(specification)
sys.modules[PACKAGE] = module
specification.loader.exec_module(module)

config_module = importlib.import_module(f"{PACKAGE}.config")
fast_pack = importlib.import_module(f"{PACKAGE}.fast_pack")
fast_store = importlib.import_module(f"{PACKAGE}.fast_store")
server_module = importlib.import_module(f"{PACKAGE}.server")


@dataclass
class FakeData:
    id: str
    display: str


class FakeSource:
    def __init__(self, source_id: str, display: str, root: Path) -> None:
        self.data = FakeData(source_id, display)
        self.root = root

    def get_media_dir_path(self) -> Path:
        return self.root


class HttpSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.audio = self.media_root / "tone.opus"
        self.audio_bytes = b"OggS-http-semantics-fixture"
        self.audio.write_bytes(self.audio_bytes)
        self.db_path = self.root / "entries.db"
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                """
                CREATE TABLE entries (
                    id INTEGER PRIMARY KEY NOT NULL,
                    expression TEXT NOT NULL,
                    reading TEXT,
                    source TEXT NOT NULL,
                    speaker TEXT,
                    display TEXT,
                    file TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO entries VALUES (1,?,?,?,?,?,?)",
                ("term", "reading", "fixture", None, "voice", "tone.opus"),
            )
            connection.execute(
                "CREATE INDEX idx_expr_reading ON entries(expression, reading)"
            )
            connection.commit()
        finally:
            connection.close()
        self.sources = {
            "fixture": FakeSource("fixture", "Fixture %s", self.media_root)
        }
        self.pack_root = self.root / "fast_audio"
        self.server = server_module.OptimizedHTTPServer(("127.0.0.1", 0))
        port = self.server.server_address[1]
        self.store = fast_store.LookupStore(
            self.db_path,
            self.sources,
            f"http://127.0.0.1:{port}",
            self.pack_root,
            response_cache_size=16,
            row_cache_size=16,
        )
        self.server.runtime = SimpleNamespace(store=self.store, version="test")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

    def tearDown(self) -> None:
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.store.close()
        del self.store
        gc.collect()
        self.temporary.cleanup()

    def request(self, method: str, path: str, headers: dict[str, str] | None = None):
        self.connection.request(method, path, headers=headers or {})
        response = self.connection.getresponse()
        payload = response.read()
        return response, payload

    def activate_pack(self) -> str:
        fast_pack.build_audio_pack(
            self.db_path,
            self.pack_root,
            self.sources,
            workers=1,
        )
        self.assertTrue(self.store.reload_pack())
        _, payload = self.request("GET", "/?term=term&reading=reading")
        return urlsplit(json.loads(payload)["audioSources"][0]["url"]).path

    def test_all_valid_full_span_ranges_are_206_for_get_and_head(self) -> None:
        size = len(self.audio_bytes)
        expected_range = f"bytes 0-{size - 1}/{size}"
        for method, value in (
            ("GET", "bytes=0-"),
            ("GET", "bytes=0-999999"),
            ("GET", "bytes=-999999"),
            ("HEAD", f"bytes=0-{size - 1}"),
        ):
            with self.subTest(method=method, value=value):
                response, payload = self.request(
                    method,
                    "/fixture/tone.opus",
                    {"Range": value},
                )
                self.assertEqual(response.status, 206)
                self.assertEqual(response.getheader("Content-Range"), expected_range)
                self.assertEqual(int(response.getheader("Content-Length")), size)
                self.assertEqual(payload, b"" if method == "HEAD" else self.audio_bytes)

    def test_not_modified_has_no_representation_headers_or_body(self) -> None:
        response, payload = self.request("GET", "/fixture/tone.opus")
        self.assertEqual((response.status, payload), (200, self.audio_bytes))
        etag = response.getheader("ETag")
        self.assertIsNotNone(etag)
        response, payload = self.request(
            "GET",
            "/fixture/tone.opus",
            {"If-None-Match": etag},
        )
        self.assertEqual((response.status, payload), (304, b""))
        self.assertIsNone(response.getheader("Content-Type"))
        self.assertIsNone(response.getheader("Content-Length"))
        self.assertEqual(response.getheader("ETag"), etag)

    def test_direct_play_is_no_store_for_legacy_and_packed_audio(self) -> None:
        query = "/v1/play?term=term&reading=reading"
        response, payload = self.request("GET", query)
        self.assertEqual((response.status, payload), (200, self.audio_bytes))
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

        packed_path = self.activate_pack()
        self.assertIn("/v/", packed_path)
        response, payload = self.request("GET", query)
        self.assertEqual((response.status, payload), (200, self.audio_bytes))
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

        size = len(self.audio_bytes)
        response, payload = self.request(
            "HEAD",
            query,
            {"Range": "bytes=-999999"},
        )
        self.assertEqual((response.status, payload), (206, b""))
        self.assertEqual(
            response.getheader("Content-Range"),
            f"bytes 0-{size - 1}/{size}",
        )
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_options_allows_conditional_audio_header(self) -> None:
        response, payload = self.request("OPTIONS", "/fixture/tone.opus")
        self.assertEqual((response.status, payload), (204, b""))
        allowed = {
            value.strip().lower()
            for value in response.getheader("Access-Control-Allow-Headers").split(",")
        }
        self.assertIn("if-none-match", allowed)


class ConfigSemanticsTests(unittest.TestCase):
    def test_non_dict_server_override_preserves_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default_path = root / "default.json"
            user_path = root / "user.json"
            default_path.write_text(
                json.dumps(
                    {
                        "server": {
                            "port": 5059,
                            "lookup_mode": "memory",
                            "response_cache_entries": 123,
                            "row_cache_entries": 456,
                        },
                        "sources": [],
                    }
                ),
                encoding="utf-8",
            )
            for invalid in (None, [], "invalid", 42):
                with self.subTest(invalid=invalid):
                    user_path.write_text(
                        json.dumps({"server": invalid}),
                        encoding="utf-8",
                    )
                    with patch.object(
                        config_module,
                        "get_default_config_path",
                        return_value=default_path,
                    ), patch.object(
                        config_module,
                        "get_config_path",
                        return_value=user_path,
                    ):
                        self.assertEqual(
                            config_module.get_server_config(),
                            {
                                "port": 5059,
                                "lookup_mode": "memory",
                                "response_cache_entries": 123,
                                "row_cache_entries": 456,
                            },
                        )


if __name__ == "__main__":
    unittest.main()
