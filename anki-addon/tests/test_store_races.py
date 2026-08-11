from __future__ import annotations

import http.client
import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit


ROOT = Path(__file__).parents[1]
PACKAGE = "_local_audio_fast_store_race_addon"
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


class FakeSource:
    def __init__(self, source_id: str, root: Path) -> None:
        self.data = SimpleNamespace(id=source_id, display="Source %s")
        self.root = root

    def get_media_dir_path(self) -> Path:
        return self.root


class StoreRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.first_path = self.media_root / "first.opus"
        self.second_path = self.media_root / "second.opus"
        self.v1_first = b"OggS-version-one-first"
        self.v1_second = b"OggS-version-one-second"
        self.v2_first = b"OggS-version-two-first"
        self.v2_second = b"OggS-version-two-second"
        self.first_path.write_bytes(self.v1_first)
        self.second_path.write_bytes(self.v1_second)
        self.db_path = self.root / "entries.db"
        self._create_database(self.db_path, "old-a", "old-b")
        self.sources = {"s1": FakeSource("s1", self.media_root)}
        self.pack_root = self.root / "fast_audio"
        self.v1 = fast_pack.build_audio_pack(
            self.db_path, self.pack_root, self.sources, workers=2
        )["version"]
        self.store = fast_store.LookupStore(
            self.db_path,
            self.sources,
            "http://127.0.0.1:9",
            self.pack_root,
            response_cache_size=32,
            row_cache_size=32,
        )
        self.request = fast_store.LookupRequest("cat", "reading", ("s1",), ())

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    @staticmethod
    def _create_database(path: Path, first_display: str, second_display: str) -> None:
        connection = sqlite3.connect(path)
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
                    (1, "cat", "reading", "s1", None, first_display, "first.opus"),
                    (2, "cat", "reading", "s1", None, second_display, "second.opus"),
                ),
            )
            connection.execute(
                "CREATE INDEX idx_expr_reading ON entries(expression, reading)"
            )
            connection.commit()
        finally:
            connection.close()

    def _replacement_database(self, first: str = "new-a", second: str = "new-b") -> Path:
        replacement = self.root / f"replacement-{time.time_ns()}.db"
        self._create_database(replacement, first, second)
        return replacement

    def _build_v2(self) -> str:
        self.first_path.write_bytes(self.v2_first)
        self.second_path.write_bytes(self.v2_second)
        result = fast_pack.build_audio_pack(
            self.db_path, self.pack_root, self.sources, workers=2
        )
        self.assertNotEqual(result["version"], self.v1)
        return result["version"]

    @staticmethod
    def _payload_versions(payload: bytes) -> set[str]:
        entries = json.loads(payload)["audioSources"]
        return {urlsplit(item["url"]).path.split("/")[2] for item in entries}

    def test_duplicate_filter_ranks_use_first_occurrence(self) -> None:
        rows = (
            (1, "r", "s1", "alice", None, "a"),
            (2, "r", "s1", "bob", None, "b"),
            (3, "r", "forvo", "alice", None, "c"),
        )
        request = fast_store.LookupRequest(
            "x", "r", ("s1", "forvo", "s1"), ("alice", "bob", "alice")
        )
        selected = self.store._select_rows(rows, request)
        self.assertEqual([row[0] for row in selected], [1, 2, 3])

    def test_sqlite_epoch_prevents_stale_row_and_response_cache_insertion(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        original = self.store._rows_uncached

        def delayed(expression, reading):
            rows = original(expression, reading)
            entered.set()
            self.assertTrue(release.wait(5))
            return rows

        self.store._rows_uncached = delayed
        old_result: list[bytes] = []
        lookup_thread = threading.Thread(
            target=lambda: old_result.append(self.store.lookup(self.request))
        )
        lookup_thread.start()
        self.assertTrue(entered.wait(5))
        replacement = self._replacement_database()
        publish_thread = threading.Thread(
            target=lambda: self.store.publish_database(replacement)
        )
        publish_thread.start()
        time.sleep(0.05)
        self.assertTrue(publish_thread.is_alive(), "publish must wait for the old lookup")
        release.set()
        lookup_thread.join(5)
        publish_thread.join(5)
        self.assertFalse(lookup_thread.is_alive())
        self.assertFalse(publish_thread.is_alive())
        self.store._rows_uncached = original

        self.assertIn(b"old-a", old_result[0])
        new_result = self.store.lookup(self.request)
        self.assertIn(b"new-a", new_result)
        self.assertNotIn(b"old-a", new_result)
        with self.store.row_cache._lock:
            row_epochs = {key[0] for key in self.store.row_cache._values}
        with self.store.response_cache._lock:
            response_epochs = {key[0] for key in self.store.response_cache._values}
        self.assertEqual(row_epochs, {self.store._database_epoch})
        self.assertEqual(response_epochs, {self.store._database_epoch})

    def test_memory_publish_swaps_rows_before_waiters_resume(self) -> None:
        self.store.close()
        self.store = fast_store.LookupStore(
            self.db_path,
            self.sources,
            "http://127.0.0.1:9",
            self.pack_root,
            lookup_mode="memory",
            response_cache_size=16,
            row_cache_size=16,
        )
        replacement = self._replacement_database("memory-new-a", "memory-new-b")
        inside_transition = threading.Event()
        release = threading.Event()
        original_reload = self.store._reload_pack_unleased

        def delayed_reload():
            inside_transition.set()
            self.assertTrue(release.wait(5))
            return original_reload()

        self.store._reload_pack_unleased = delayed_reload
        publish_thread = threading.Thread(
            target=lambda: self.store.publish_database(replacement)
        )
        publish_thread.start()
        self.assertTrue(inside_transition.wait(5))
        results: list[bytes] = []
        lookup_thread = threading.Thread(
            target=lambda: results.append(self.store.lookup(self.request))
        )
        lookup_thread.start()
        time.sleep(0.05)
        self.assertTrue(lookup_thread.is_alive(), "lookup must wait for atomic swap")
        release.set()
        publish_thread.join(5)
        lookup_thread.join(5)
        self.assertFalse(publish_thread.is_alive())
        self.assertFalse(lookup_thread.is_alive())
        self.assertIn(b"memory-new-a", results[0])
        self.assertNotIn(b"old-a", results[0])

    def test_reload_during_serialization_is_single_version_and_not_stale_cached(self) -> None:
        v2 = self._build_v2()
        entered = threading.Event()
        release = threading.Event()
        original_display = self.store._display_name
        first_call = True

        def delayed_display(source_id, display):
            nonlocal first_call
            if first_call:
                first_call = False
                entered.set()
                self.assertTrue(release.wait(5))
            return original_display(source_id, display)

        self.store._display_name = delayed_display
        payloads: list[bytes] = []
        lookup_thread = threading.Thread(
            target=lambda: payloads.append(self.store.lookup(self.request))
        )
        lookup_thread.start()
        self.assertTrue(entered.wait(5))
        reload_thread = threading.Thread(target=self.store.reload_pack)
        reload_thread.start()
        time.sleep(0.05)
        self.assertTrue(reload_thread.is_alive(), "reload must wait for serialization")
        release.set()
        lookup_thread.join(5)
        reload_thread.join(5)
        self.assertFalse(lookup_thread.is_alive())
        self.assertFalse(reload_thread.is_alive())
        self.store._display_name = original_display

        self.assertEqual(self._payload_versions(payloads[0]), {self.v1})
        self.assertEqual(self._payload_versions(self.store.lookup(self.request)), {v2})

    def test_blocked_audio_lease_does_not_block_lookup_or_reload(self) -> None:
        v2 = self._build_v2()
        entered = threading.Event()
        release = threading.Event()
        held_bytes: list[bytes] = []
        held_pack = []

        def blocked_client() -> None:
            with self.store.leased_packed_audio(self.v1, 1) as (pack, audio):
                assert pack is not None and audio is not None
                held_pack.append(pack)
                entered.set()
                self.assertTrue(release.wait(5))
                held_bytes.append(bytes(pack.view(audio, 0, audio.length)))

        first_client = threading.Thread(target=blocked_client)
        first_client.start()
        self.assertTrue(entered.wait(5))
        with ThreadPoolExecutor(max_workers=2) as executor:
            lookup_future = executor.submit(self.store.lookup, self.request)

            def second_audio() -> bytes:
                with self.store.leased_packed_audio(self.v1, 2) as (pack, audio):
                    assert pack is not None and audio is not None
                    view = pack.view(audio, 0, audio.length)
                    try:
                        return bytes(view)
                    finally:
                        view.release()

            audio_future = executor.submit(second_audio)
            self.assertEqual(self._payload_versions(lookup_future.result(2)), {self.v1})
            self.assertEqual(audio_future.result(2), self.v1_second)
        self.assertTrue(self.store.reload_pack())
        self.assertEqual(self._payload_versions(self.store.lookup(self.request)), {v2})
        release.set()
        first_client.join(5)
        self.assertFalse(first_client.is_alive())
        self.assertEqual(held_bytes, [self.v1_first])
        self.assertTrue(held_pack[0]._pack.closed)

    def test_old_version_survives_reload_restart_and_play_selection_swap(self) -> None:
        old_url = self.store.best_audio_url(self.request)
        assert old_url is not None
        old_path = urlsplit(old_url).path
        v2 = self._build_v2()

        server = server_module.OptimizedHTTPServer(("127.0.0.1", 0))
        self.store.base_url = f"http://127.0.0.1:{server.server_address[1]}"
        server.runtime = SimpleNamespace(store=self.store, version="test")
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        original_best = self.store.best_audio_url

        def swap_after_selection(request):
            selected = original_best(request)
            self.assertTrue(self.store.reload_pack())
            return selected

        self.store.best_audio_url = swap_after_selection
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request("GET", "/v1/play?term=cat&reading=reading")
            response = connection.getresponse()
            body = response.read()
            self.assertEqual((response.status, body), (200, self.v1_first))
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            connection.request("GET", old_path)
            response = connection.getresponse()
            self.assertEqual((response.status, response.read()), (200, self.v1_first))
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            server_thread.join(5)
            self.store.best_audio_url = original_best

        self.store.close()
        self.store = fast_store.LookupStore(
            self.db_path,
            self.sources,
            "http://127.0.0.1:9",
            self.pack_root,
            response_cache_size=8,
            row_cache_size=8,
        )
        self.assertEqual(self.store.info()["audioPack"]["version"], v2)
        with self.store.leased_packed_audio(self.v1, 1) as (pack, audio):
            assert pack is not None and audio is not None
            view = pack.view(audio, 0, audio.length)
            try:
                self.assertEqual(bytes(view), self.v1_first)
            finally:
                view.release()

    def test_reload_close_are_serialized_and_post_close_leases_reject(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        original_reload = self.store._reload_pack_unleased

        def delayed_reload():
            entered.set()
            self.assertTrue(release.wait(5))
            return original_reload()

        self.store._reload_pack_unleased = delayed_reload
        reload_thread = threading.Thread(target=self.store.reload_pack)
        reload_thread.start()
        self.assertTrue(entered.wait(5))
        close_thread = threading.Thread(target=self.store.close)
        close_thread.start()
        time.sleep(0.05)
        self.assertTrue(close_thread.is_alive(), "close must serialize behind reload")
        release.set()
        reload_thread.join(5)
        close_thread.join(5)
        self.assertFalse(reload_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        with self.assertRaises(RuntimeError):
            self.store.reload_pack()
        with self.assertRaises(RuntimeError):
            self.store.lookup(self.request)
        with self.assertRaises(RuntimeError):
            with self.store.leased_packed_audio(self.v1, 1):
                pass
        self.store.close()


if __name__ == "__main__":
    unittest.main()
