from __future__ import annotations

import http.client
import hashlib
import importlib
import importlib.util
import json
import os
import gc
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlsplit
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
PACKAGE = "_local_audio_fast_test_addon"
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


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        first_root = self.root / "s1"
        forvo_root = self.root / "forvo"
        first_root.mkdir()
        (forvo_root / "alice").mkdir(parents=True)
        self.first_audio = first_root / "tone.opus"
        self.same_audio = first_root / "same.opus"
        self.forvo_audio = forvo_root / "alice" / "voice.mp3"
        self.first_bytes = b"OggS-fast-audio-fixture"
        self.forvo_bytes = b"ID3-forvo-audio-fixture"
        self.first_audio.write_bytes(self.first_bytes)
        self.same_audio.write_bytes(self.first_bytes)
        self.forvo_audio.write_bytes(self.forvo_bytes)
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
            connection.executemany(
                "INSERT INTO entries VALUES (?,?,?,?,?,?,?)",
                (
                    (1, "猫", "ねこ", "s1", None, "[0]", "tone.opus"),
                    (2, "猫", None, "s1", None, None, "same.opus"),
                    (3, "猫", "ねこ", "forvo", "alice", "alice", "alice/voice.mp3"),
                    (4, "犬", "いぬ", "s1", None, "[1]", "tone.opus"),
                ),
            )
            connection.execute(
                "CREATE INDEX idx_expr_reading ON entries(expression, reading)"
            )
            connection.commit()
        finally:
            connection.close()
        self.sources = {
            "s1": FakeSource("s1", "Source %s", first_root),
            "forvo": FakeSource("forvo", "Forvo (%s)", forvo_root),
        }
        self.pack_root = self.root / "fast_audio"
        self.server = server_module.OptimizedHTTPServer(("127.0.0.1", 0))
        port = self.server.server_address[1]
        self.store = fast_store.LookupStore(
            self.db_path,
            self.sources,
            f"http://127.0.0.1:{port}",
            self.pack_root,
            response_cache_size=64,
            row_cache_size=64,
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

    def request(self, method: str, path: str, headers: dict | None = None):
        self.connection.request(method, path, headers=headers or {})
        response = self.connection.getresponse()
        payload = response.read()
        return response, payload

    @staticmethod
    def lookup_path(term: str, reading: str | None = None, suffix: str = "") -> str:
        path = f"/?term={quote(term)}"
        if reading is not None:
            path += f"&reading={quote(reading)}"
        return path + suffix

    def test_yomitan_compatibility_order_filters_and_keepalive(self) -> None:
        response, payload = self.request("GET", self.lookup_path("猫", "ねこ"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.version, 11)
        parsed = json.loads(payload)
        self.assertEqual(parsed["type"], "audioSourceList")
        self.assertEqual(
            [entry["name"] for entry in parsed["audioSources"]],
            ["Source %s", "Source [0]", "Forvo (alice)"],
        )
        first_socket = self.connection.sock
        response, payload = self.request(
            "GET", self.lookup_path("猫", "ねこ", "&sources=forvo&user=alice")
        )
        self.assertEqual(response.status, 200)
        self.assertIs(self.connection.sock, first_socket)
        self.assertEqual(
            [entry["name"] for entry in json.loads(payload)["audioSources"]],
            ["Forvo (alice)"],
        )
        response, payload = self.request("GET", "/?expression=%E7%8C%AB&reading=%E3%81%AD%E3%81%93")
        self.assertEqual(response.status, 200)
        self.assertEqual(len(json.loads(payload)["audioSources"]), 3)

    def test_legacy_audio_head_range_and_secure_path(self) -> None:
        _, payload = self.request("GET", self.lookup_path("猫", "ねこ"))
        audio_url = json.loads(payload)["audioSources"][0]["url"]
        audio_path = urlsplit(audio_url).path
        response, body = self.request("GET", audio_path)
        self.assertEqual((response.status, body), (200, self.first_bytes))
        response, body = self.request("HEAD", audio_path)
        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(response.getheader("Content-Length")), len(self.first_bytes))
        response, body = self.request("GET", audio_path, {"Range": "bytes=4-8"})
        self.assertEqual(response.status, 206)
        self.assertEqual(body, self.first_bytes[4:9])
        self.assertEqual(response.getheader("Content-Range"), f"bytes 4-8/{len(self.first_bytes)}")
        response, _ = self.request("GET", audio_path, {"Range": "bytes=999-1000"})
        self.assertEqual(response.status, 416)
        response, _ = self.request("GET", "/s1/%2e%2e/entries.db")
        self.assertEqual(response.status, 404)

    def test_direct_play_and_candidates(self) -> None:
        query = "?term=%E7%8C%AB&reading=%E3%81%AD%E3%81%93"
        response, payload = self.request("GET", "/v1/candidates" + query)
        self.assertEqual(response.status, 200)
        candidates = json.loads(payload)
        self.assertEqual(candidates["version"], "legacy")
        self.assertEqual([item["audioId"] for item in candidates["candidates"]], [2, 1, 3])
        self.assertEqual(candidates["candidates"][0]["source"], "s1")
        response, payload = self.request("GET", "/v1/play" + query)
        self.assertEqual((response.status, payload), (200, self.first_bytes))
        response, payload = self.request(
            "GET", "/v1/play" + query, {"Range": "bytes=-4"}
        )
        self.assertEqual((response.status, payload), (206, self.first_bytes[-4:]))
        response, _ = self.request("GET", "/v1/play?term=missing")
        self.assertEqual(response.status, 404)

    def test_first_audio_fast_path_returns_one_unfiltered_candidate(self) -> None:
        query = "?term=%E7%8C%AB&reading=%E3%81%AD%E3%81%93"
        response, payload = self.request(
            "GET", "/v1/first" + query + "&sources=forvo&user=alice"
        )
        self.assertEqual(response.status, 200)
        parsed = json.loads(payload)
        self.assertEqual(parsed["type"], "audioSourceList")
        self.assertEqual(len(parsed["audioSources"]), 1)
        self.assertEqual(parsed["audioSources"][0]["name"], "Source %s")
        self.assertTrue(parsed["audioSources"][0]["url"].endswith("/s1/same.opus"))

        response, payload = self.request("GET", "/v1/first?term=missing")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload)["audioSources"], [])

    def test_first_audio_fast_path_keeps_cache_and_reading_semantics(self) -> None:
        query = "?term=%E7%8C%AB&reading=%E3%81%AD%E3%81%93"
        _, full_payload = self.request("GET", "/" + query)
        _, first_payload = self.request("GET", "/v1/first" + query)
        self.assertEqual(len(json.loads(full_payload)["audioSources"]), 3)
        self.assertEqual(len(json.loads(first_payload)["audioSources"]), 1)
        _, full_payload = self.request("GET", "/" + query)
        self.assertEqual(len(json.loads(full_payload)["audioSources"]), 3)

        response, payload = self.request("HEAD", "/v1/first" + query)
        self.assertEqual(response.status, 200)
        self.assertEqual(payload, b"")

        # A NULL-reading recording remains eligible for a mismatched reading.
        _, payload = self.request(
            "GET", "/v1/first?term=%E7%8C%AB&reading=%E9%81%95%E3%81%86"
        )
        self.assertTrue(
            json.loads(payload)["audioSources"][0]["url"].endswith("/s1/same.opus")
        )
        _, payload = self.request(
            "GET", "/v1/first?term=%E7%8A%AC&reading=%E9%81%95%E3%81%86"
        )
        self.assertEqual(json.loads(payload)["audioSources"], [])

    def test_first_audio_fast_path_tracks_source_priority_changes(self) -> None:
        query = "/v1/first?term=%E7%8C%AB&reading=%E3%81%AD%E3%81%93"
        _, payload = self.request("GET", query)
        self.assertTrue(
            json.loads(payload)["audioSources"][0]["url"].endswith("/s1/same.opus")
        )

        self.store.replace_sources(
            {"forvo": self.sources["forvo"], "s1": self.sources["s1"]}
        )
        _, payload = self.request("GET", query)
        self.assertTrue(
            json.loads(payload)["audioSources"][0]["url"].endswith(
                "/forvo/alice/voice.mp3"
            )
        )

    def test_versioned_pack_deduplicates_bytes_and_survives_source_removal(self) -> None:
        result = fast_pack.build_audio_pack(
            self.db_path,
            self.pack_root,
            self.sources,
            workers=4,
        )
        self.assertEqual(result["valid_rows"], 4)
        self.assertEqual(result["missing_files"], 0)
        self.assertGreaterEqual(result["content_duplicates"], 1)
        self.assertLess(
            result["pack_bytes"],
            fast_pack.PACK_HEADER.size
            + len(self.first_bytes) * 3
            + len(self.forvo_bytes),
        )
        self.assertTrue(self.store.reload_pack())
        _, payload = self.request("GET", self.lookup_path("猫", "ねこ"))
        entries = json.loads(payload)["audioSources"]
        self.assertTrue(all("/v/" in entry["url"] for entry in entries))
        self.first_audio.unlink()
        self.same_audio.unlink()
        response, body = self.request("GET", "/s1/tone.opus")
        self.assertEqual((response.status, body), (200, self.first_bytes))
        self.assertEqual(response.getheader("Cache-Control"), "public, max-age=3600")
        response, body = self.request("GET", urlsplit(entries[0]["url"]).path)
        self.assertEqual((response.status, body), (200, self.first_bytes))
        response, body = self.request(
            "GET", urlsplit(entries[0]["url"]).path, {"Range": "bytes=1-3"}
        )
        self.assertEqual((response.status, body), (206, self.first_bytes[1:4]))

    def test_rust_bundle_import_hardlinks_pack_and_supports_offset_zero(self) -> None:
        bundle_root = self.root / "rust-bundle"
        version_root = bundle_root / "versions" / "0123456789abcdef"
        version_root.mkdir(parents=True)
        assets = (
            ("forvo", "alice/voice.mp3", self.forvo_bytes, 1, 1),
            ("s1", "same.opus", self.first_bytes, 0, 4),
            ("s1", "tone.opus", self.first_bytes, 0, 4),
        )
        pack_bytes = b"".join(asset[2] for asset in assets)
        source_pack = version_root / "audio.pack"
        source_pack.write_bytes(pack_bytes)
        strings = bytearray()
        audio_records = bytearray()
        pack_offset = 0
        for _source, filename, payload, source_index, mime_id in assets:
            path = filename.encode("utf-8")
            path_offset = len(strings)
            strings.extend(path)
            audio_records.extend(
                fast_pack.RUST_AUDIO_RECORD.pack(
                    pack_offset,
                    len(payload),
                    path_offset,
                    len(path),
                    0,
                    source_index,
                    mime_id,
                )
            )
            pack_offset += len(payload)
        strings_offset = fast_pack.RUST_HEADER_SIZE + len(audio_records)
        lookup = bytearray(strings_offset + len(strings))
        lookup[:8] = fast_pack.RUST_INDEX_MAGIC
        struct.pack_into("<II", lookup, 8, 1, fast_pack.RUST_HEADER_SIZE)
        struct.pack_into("<Q", lookup, 24, 4)
        struct.pack_into("<Q", lookup, 32, len(assets))
        struct.pack_into("<Q", lookup, 64, fast_pack.RUST_HEADER_SIZE)
        struct.pack_into("<Q", lookup, 72, strings_offset)
        struct.pack_into("<Q", lookup, 80, len(strings))
        lookup[fast_pack.RUST_HEADER_SIZE:strings_offset] = audio_records
        lookup[strings_offset:] = strings
        (version_root / "lookup.bin").write_bytes(lookup)
        manifest = {
            "formatVersion": 1,
            "bundleVersion": "0123456789abcdef",
            "lookupFile": "versions/0123456789abcdef/lookup.bin",
            "packFile": "versions/0123456789abcdef/audio.pack",
            "lookupBlake3": "0" * 64,
            "packBlake3": "1" * 64,
            "recordCount": 4,
            "audioCount": 3,
            "uniqueBlobCount": 3,
            "identicalContentAssets": 0,
            "deduplicatedBytes": 0,
            "packBytes": len(pack_bytes),
            "sources": [{"id": "s1"}, {"id": "forvo"}],
        }
        (bundle_root / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        lookup_relative = manifest["lookupFile"]
        pack_relative = manifest["packFile"]
        integrity = {
            "format": fast_pack.INTEGRITY_FORMAT,
            "bundleVersion": manifest["bundleVersion"],
            "files": {
                lookup_relative: {
                    "bytes": len(lookup),
                    "sha256": hashlib.sha256(lookup).hexdigest(),
                },
                pack_relative: {
                    "bytes": len(pack_bytes),
                    "sha256": hashlib.sha256(pack_bytes).hexdigest(),
                },
            },
        }
        (bundle_root / fast_pack.INTEGRITY_FILE_NAME).write_text(
            json.dumps(integrity), encoding="utf-8"
        )

        result = fast_pack.import_rust_bundle(
            self.db_path, self.pack_root, bundle_root
        )
        imported_pack = (
            self.pack_root / result["pack"]
        )
        self.assertTrue(result["hardlinked_pack"])
        self.assertTrue(os.path.samefile(source_pack, imported_pack))
        opened = fast_pack.AudioPack.open_active(self.pack_root, self.db_path)
        self.assertIsNotNone(opened)
        assert opened is not None
        try:
            first = opened.get(3)
            self.assertIsNotNone(first)
            assert first is not None
            self.assertEqual(first.offset, 0)
            self.assertEqual(
                bytes(opened.view(first, 0, first.length)), self.forvo_bytes
            )
        finally:
            opened.close()

    def test_atomic_database_publish_invalidates_connections_and_caches(self) -> None:
        self.request("GET", self.lookup_path("猫", "ねこ"))
        replacement = self.root / "replacement.db"
        source_connection = sqlite3.connect(self.db_path)
        destination_connection = sqlite3.connect(replacement)
        try:
            source_connection.backup(destination_connection)
            destination_connection.execute(
                "INSERT INTO entries VALUES (?,?,?,?,?,?,?)",
                (5, "鳥", "とり", "s1", None, "[2]", "tone.opus"),
            )
            destination_connection.commit()
        finally:
            source_connection.close()
            destination_connection.close()
        self.store.publish_database(replacement)
        self.assertFalse(replacement.exists())
        response, payload = self.request("GET", self.lookup_path("鳥", "とり"))
        self.assertEqual(response.status, 200)
        self.assertEqual(
            [entry["name"] for entry in json.loads(payload)["audioSources"]],
            ["Source [2]"],
        )

    def test_database_publish_cannot_cross_pack_maintenance_lock(self) -> None:
        replacement = self.root / "locked-replacement.db"
        shutil.copy2(self.db_path, replacement)
        before = self.db_path.read_bytes()
        with fast_pack._PackBuildLock(
            self.pack_root / fast_pack.BUILD_LOCK_NAME
        ):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                self.store.publish_database(replacement)
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertTrue(replacement.is_file())

    def test_legacy_path_lookup_cannot_deadlock_pack_reload(self) -> None:
        fast_pack.build_audio_pack(
            self.db_path, self.pack_root, self.sources, workers=1
        )
        self.assertTrue(self.store.reload_pack())
        entered_snapshot = threading.Event()
        continue_snapshot = threading.Event()
        results = []
        errors = []
        original_snapshot = self.store._leased_pack_snapshot

        def delayed_snapshot(*args, **kwargs):
            self.assertTrue(kwargs.get("state_already_leased"))
            entered_snapshot.set()
            if not continue_snapshot.wait(5):
                raise TimeoutError("test did not release the pack snapshot")
            return original_snapshot(*args, **kwargs)

        def lookup() -> None:
            try:
                results.append(
                    self.store.packed_target_for_legacy_path("s1", "tone.opus")
                )
            except BaseException as error:
                errors.append(error)

        def reload() -> None:
            try:
                self.store.reload_pack()
            except BaseException as error:
                errors.append(error)

        with patch.object(self.store, "_leased_pack_snapshot", delayed_snapshot):
            lookup_thread = threading.Thread(target=lookup)
            reload_thread = threading.Thread(target=reload)
            lookup_thread.start()
            self.assertTrue(entered_snapshot.wait(5))
            reload_thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with self.store._state_changed:
                    if self.store._state_transition:
                        break
                time.sleep(0.001)
            else:
                self.fail("pack reload did not enter its state transition")
            continue_snapshot.set()
            lookup_thread.join(5)
            reload_thread.join(5)
        self.assertFalse(lookup_thread.is_alive())
        self.assertFalse(reload_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [(self.store._pack.version, 1)])

    def test_database_pool_reuses_connections_across_http_threads(self) -> None:
        port = self.server.server_address[1]
        for _ in range(40):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", self.lookup_path("猫", "ねこ"))
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
        info = self.store.info()
        self.assertLessEqual(info["databaseConnections"], 16)
        self.assertEqual(
            info["databaseConnections"], info["idleDatabaseConnections"]
        )


if __name__ == "__main__":
    unittest.main()
