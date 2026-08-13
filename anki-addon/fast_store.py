from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Iterable, Optional, TypeVar
from urllib.parse import quote

from .fast_pack import (
    AudioPack,
    BUILD_LOCK_NAME,
    MIME_BY_ID,
    MIME_ID_BY_SUFFIX,
    _PackBuildLock,
    protected_cleanup_state_exists,
)


TKey = TypeVar("TKey")
TValue = TypeVar("TValue")
LookupRow = tuple[int, Optional[str], str, Optional[str], Optional[str], str]

TERM_QUERY = (
    "SELECT id, reading, source, speaker, display, file "
    "FROM entries WHERE expression = ?"
)
READING_QUERY = (
    "SELECT id, reading, source, speaker, display, file "
    "FROM entries WHERE expression = ? AND (reading IS NULL OR reading = ?)"
)


@dataclass(frozen=True)
class LookupRequest:
    expression: str
    reading: Optional[str]
    sources: tuple[str, ...]
    users: tuple[str, ...]


class LruCache(Generic[TKey, TValue]):
    def __init__(self, capacity: int) -> None:
        self.capacity = max(0, capacity)
        self._values: OrderedDict[TKey, TValue] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: TKey) -> tuple[bool, Optional[TValue]]:
        if self.capacity == 0:
            return False, None
        with self._lock:
            try:
                value = self._values.pop(key)
            except KeyError:
                return False, None
            self._values[key] = value
            return True, value

    def put(self, key: TKey, value: TValue) -> None:
        if self.capacity == 0:
            return
        with self._lock:
            self._values.pop(key, None)
            self._values[key] = value
            if len(self._values) > self.capacity:
                self._values.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)


class LookupStore:
    """Thread-safe lookup engine with retained immutable SQLite connections."""

    def __init__(
        self,
        db_path: Path,
        sources: dict,
        base_url: str,
        pack_root: Path,
        response_cache_size: int = 16384,
        row_cache_size: int = 16384,
        lookup_mode: str = "sqlite",
    ) -> None:
        self.db_path = db_path.resolve()
        self.sources = sources
        self.source_ids = tuple(sources.keys())
        self.base_url = base_url.rstrip("/")
        self.pack_root = pack_root.resolve()
        self.lookup_mode = lookup_mode if lookup_mode in ("sqlite", "memory") else "sqlite"
        self._state_guard = threading.RLock()
        self._state_changed = threading.Condition(self._state_guard)
        self._state_transition = False
        self._state_readers = 0
        self._closed = False
        self.response_cache: LruCache[tuple[int, int, LookupRequest], bytes] = LruCache(
            response_cache_size
        )
        self.row_cache: LruCache[
            tuple[int, str, Optional[str]], tuple[LookupRow, ...]
        ] = LruCache(row_cache_size)
        self._database_guard = threading.RLock()
        self._database_changed = threading.Condition(self._database_guard)
        self._publishing_database = False
        self._active_queries = 0
        self._database_epoch = 0
        self._connections: set[sqlite3.Connection] = set()
        self._available_connections: list[sqlite3.Connection] = []
        self._maximum_idle_connections = 16
        self._memory_rows: Optional[dict[str, tuple[LookupRow, ...]]] = None
        self._memory_load_seconds = 0.0
        self._legacy_path_rows: Optional[dict[tuple[str, str], int]] = None
        self._pack_guard = threading.RLock()
        self._pack: Optional[AudioPack] = None
        self._pack_epoch = 0
        self._pack_leases: dict[AudioPack, int] = {}
        self._retired_packs: set[AudioPack] = set()
        self._historical_packs: dict[str, AudioPack] = {}
        self.reload_pack()
        if self.lookup_mode == "memory":
            try:
                self._load_memory_rows()
            except MemoryError:
                self.lookup_mode = "sqlite"
                self._memory_rows = None

    def _database_uri(self) -> str:
        return f"file:{self.db_path.as_posix()}?mode=ro&immutable=1"

    @contextmanager
    def _leased_state(self):
        with self._state_changed:
            while self._state_transition and not self._closed:
                self._state_changed.wait()
            if self._closed:
                raise RuntimeError("lookup store is closed")
            self._state_readers += 1
        try:
            yield
        finally:
            with self._state_changed:
                self._state_readers -= 1
                if self._state_readers == 0:
                    self._state_changed.notify_all()

    def _begin_state_transition(self, allow_closed: bool = False) -> bool:
        with self._state_changed:
            while self._state_transition:
                self._state_changed.wait()
            if self._closed:
                if allow_closed:
                    return False
                raise RuntimeError("lookup store is closed")
            self._state_transition = True
            while self._state_readers:
                self._state_changed.wait()
            return True

    def _end_state_transition(self) -> None:
        with self._state_changed:
            self._state_transition = False
            self._state_changed.notify_all()

    def _connection_locked(self) -> sqlite3.Connection:
        if self._available_connections:
            return self._available_connections.pop()
        connection = sqlite3.connect(
            self._database_uri(),
            uri=True,
            check_same_thread=False,
            isolation_level=None,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA mmap_size=268435456")
        connection.execute("PRAGMA cache_size=-8192")
        self._connections.add(connection)
        return connection

    @contextmanager
    def _leased_connection(self):
        with self._database_changed:
            while self._publishing_database:
                self._database_changed.wait()
            connection = self._connection_locked()
            self._active_queries += 1
        try:
            yield connection
        finally:
            with self._database_changed:
                self._active_queries -= 1
                if (
                    not self._publishing_database
                    and connection in self._connections
                ):
                    if len(self._available_connections) < self._maximum_idle_connections:
                        self._available_connections.append(connection)
                    else:
                        self._connections.discard(connection)
                        try:
                            connection.close()
                        except sqlite3.Error:
                            pass
                if self._active_queries == 0:
                    self._database_changed.notify_all()

    def _build_memory_rows(
        self, db_path: Optional[Path] = None
    ) -> tuple[dict[str, tuple[LookupRow, ...]], float]:
        started = time.perf_counter()
        rows_by_expression: dict[str, list[LookupRow]] = defaultdict(list)
        path = (db_path or self.db_path).resolve(strict=True)
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA mmap_size=268435456")
        try:
            cursor = connection.execute(
                "SELECT expression, id, reading, source, speaker, display, file FROM entries"
            )
            for expression, row_id, reading, source, speaker, display, filename in cursor:
                rows_by_expression[expression].append(
                    (row_id, reading, source, speaker, display, filename)
                )
        finally:
            connection.close()
        rows = {
            expression: tuple(rows) for expression, rows in rows_by_expression.items()
        }
        return rows, time.perf_counter() - started

    def _load_memory_rows(self) -> None:
        rows, elapsed = self._build_memory_rows()
        self._memory_rows = rows
        self._memory_load_seconds = elapsed

    def _rows_uncached(
        self, expression: str, reading: Optional[str]
    ) -> tuple[LookupRow, ...]:
        if self._memory_rows is not None:
            rows = self._memory_rows.get(expression, ())
            if reading is None:
                return rows
            return tuple(row for row in rows if row[1] is None or row[1] == reading)
        with self._leased_connection() as connection:
            if reading is None:
                return tuple(connection.execute(TERM_QUERY, (expression,)).fetchall())
            return tuple(connection.execute(READING_QUERY, (expression, reading)).fetchall())

    def _rows_unleased(
        self, expression: str, reading: Optional[str]
    ) -> tuple[LookupRow, ...]:
        with self._database_changed:
            while self._publishing_database:
                self._database_changed.wait()
            database_epoch = self._database_epoch
        key = database_epoch, expression, reading
        found, cached = self.row_cache.get(key)
        if found:
            return cached or ()
        rows = self._rows_uncached(expression, reading)
        self.row_cache.put(key, rows)
        return rows

    def rows(self, expression: str, reading: Optional[str]) -> tuple[LookupRow, ...]:
        with self._leased_state():
            return self._rows_unleased(expression, reading)

    @staticmethod
    def _select_rows(rows: Iterable[LookupRow], request: LookupRequest) -> list[LookupRow]:
        source_set = set(request.sources)
        user_set = set(request.users)
        selected = [
            row
            for row in rows
            if row[2] in source_set
            and (not user_set or row[3] is None or row[3] in user_set)
        ]
        source_rank: dict[str, int] = {}
        user_rank: dict[str, int] = {}
        for rank, source in enumerate(request.sources):
            source_rank.setdefault(source, rank)
        for rank, user in enumerate(request.users):
            user_rank.setdefault(user, rank)
        selected.sort(
            key=lambda row: (
                source_rank.get(row[2], len(source_rank)),
                -1 if row[3] is None else user_rank.get(row[3], len(user_rank)),
                (row[1] is not None, row[1] or ""),
                row[0],
            )
        )
        return selected

    def _display_name(self, source_id: str, display: Optional[str]) -> Optional[str]:
        source = self.sources.get(source_id)
        if source is None:
            return None
        template = source.data.display
        if display is None:
            return template
        try:
            return template % display
        except (TypeError, ValueError):
            return template

    def _legacy_audio_url(self, source_id: str, filename: str) -> str:
        safe_source = quote(source_id, safe="")
        normalized = filename.replace("\\", "/")
        safe_filename = quote(normalized, safe="/!$&'()*+,;=:@-._~")
        return f"{self.base_url}/{safe_source}/{safe_filename}"

    def _audio_url_with_pack(
        self,
        pack: Optional[AudioPack],
        row_id: int,
        source_id: str,
        filename: str,
    ) -> str:
        if pack is not None and pack.get(row_id) is not None:
            return f"{self.base_url}/v/{pack.version}/audio/{row_id}"
        return self._legacy_audio_url(source_id, filename)

    def _lookup_with_pack(
        self, request: LookupRequest, pack: Optional[AudioPack]
    ) -> bytes:
        selected = self._selected_rows_unleased(request)
        entries = []
        for row_id, _reading, source_id, _speaker, display, filename in selected:
            name = self._display_name(source_id, display)
            if name is None:
                continue
            entries.append(
                {
                    "name": name,
                    "url": self._audio_url_with_pack(
                        pack, row_id, source_id, filename
                    ),
                }
            )
        return json.dumps(
            {"type": "audioSourceList", "audioSources": entries},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def lookup_uncached(self, request: LookupRequest) -> bytes:
        with self._leased_state():
            with self._leased_pack_snapshot(state_already_leased=True) as (
                pack,
                _pack_epoch,
            ):
                return self._lookup_with_pack(request, pack)

    def _selected_rows_unleased(self, request: LookupRequest) -> list[LookupRow]:
        return self._select_rows(
            self._rows_unleased(request.expression, request.reading), request
        )

    def selected_rows(self, request: LookupRequest) -> list[LookupRow]:
        with self._leased_state():
            return self._selected_rows_unleased(request)

    def best_audio_url(self, request: LookupRequest) -> Optional[str]:
        with self._leased_state():
            selected = self._selected_rows_unleased(request)
            if not selected:
                return None
            row_id, _reading, source_id, _speaker, _display, filename = selected[0]
            with self._leased_pack_snapshot(state_already_leased=True) as (
                pack,
                _pack_epoch,
            ):
                return self._audio_url_with_pack(pack, row_id, source_id, filename)

    def candidates_payload(self, request: LookupRequest) -> bytes:
        candidates = []
        with self._leased_state():
            selected = self._selected_rows_unleased(request)
            with self._leased_pack_snapshot(state_already_leased=True) as (
                pack,
                _pack_epoch,
            ):
                pack_version = pack.version if pack is not None else "legacy"
                for row_id, reading, source_id, speaker, display, filename in selected:
                    name = self._display_name(source_id, display)
                    if name is None:
                        continue
                    packed = pack.get(row_id) if pack is not None else None
                    if packed is not None:
                        mime_type = packed.mime_type
                        url = f"{self.base_url}/v/{pack.version}/audio/{row_id}"
                    else:
                        mime_id = MIME_ID_BY_SUFFIX.get(Path(filename).suffix.lower(), 0)
                        mime_type = MIME_BY_ID.get(mime_id, "application/octet-stream")
                        url = self._legacy_audio_url(source_id, filename)
                    candidates.append(
                        {
                            "audioId": row_id,
                            "source": source_id,
                            "speaker": speaker,
                            "reading": reading,
                            "name": name,
                            "mime": mime_type,
                            "url": url,
                        }
                    )
        return json.dumps(
            {"version": pack_version, "candidates": candidates},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def lookup(self, request: LookupRequest) -> bytes:
        with self._leased_state():
            with self._database_guard:
                database_epoch = self._database_epoch
            with self._leased_pack_snapshot(state_already_leased=True) as (
                pack,
                pack_epoch,
            ):
                cache_key = database_epoch, pack_epoch, request
                found, cached = self.response_cache.get(cache_key)
                if found:
                    return cached or b""
                payload = self._lookup_with_pack(request, pack)
                self.response_cache.put(cache_key, payload)
                return payload

    @contextmanager
    def leased_packed_audio(self, version: str, audio_id: int):
        with self._leased_pack_snapshot(version) as (pack, _pack_epoch):
            if pack is None:
                yield None, None
                return
            yield pack, pack.get(audio_id)

    def packed_target_for_legacy_path(
        self, source_id: str, filename: str
    ) -> Optional[tuple[str, int]]:
        """Resolve a cached pre-pack URL after verified loose-file cleanup."""

        normalized = filename.replace("\\", "/")
        with self._leased_state():
            with self._database_guard:
                if self._legacy_path_rows is None:
                    by_path: dict[tuple[str, str], int] = {}
                    with self._leased_connection() as connection:
                        for row_id, row_source, row_filename in connection.execute(
                            "SELECT id,source,file FROM entries ORDER BY id"
                        ):
                            key = row_source, row_filename.replace("\\", "/")
                            by_path.setdefault(key, int(row_id))
                    self._legacy_path_rows = by_path
                row_id = self._legacy_path_rows.get((source_id, normalized))
            if row_id is None:
                return None
            with self._leased_pack_snapshot(state_already_leased=True) as (
                pack,
                _pack_epoch,
            ):
                if pack is None:
                    return None
                if pack.get(row_id) is not None:
                    return pack.version, row_id
        return None

    @contextmanager
    def _leased_pack_snapshot(
        self,
        version: Optional[str] = None,
        *,
        state_already_leased: bool = False,
    ):
        # Take the state and pack locks together only long enough to snapshot and
        # increment the identity lease.  Lifecycle transitions cannot slip
        # between the closed check and the increment, while slow socket writes
        # hold neither lock.
        if state_already_leased:
            with self._pack_guard:
                pack, pack_epoch = self._acquire_pack_snapshot_locked(version)
        else:
            with self._state_changed:
                while self._state_transition and not self._closed:
                    self._state_changed.wait()
                if self._closed:
                    raise RuntimeError("lookup store is closed")
                with self._pack_guard:
                    pack, pack_epoch = self._acquire_pack_snapshot_locked(version)
        try:
            yield pack, pack_epoch
        finally:
            close_pack = None
            if pack is not None:
                with self._pack_guard:
                    remaining = self._pack_leases.get(pack, 0) - 1
                    if remaining > 0:
                        self._pack_leases[pack] = remaining
                    else:
                        self._pack_leases.pop(pack, None)
                        if pack is not self._pack:
                            if self._historical_packs.get(pack.version) is pack:
                                self._historical_packs.pop(pack.version, None)
                            self._retired_packs.discard(pack)
                            close_pack = pack
            if close_pack is not None:
                self._close_pack_safely(close_pack)

    def _acquire_pack_snapshot_locked(
        self, version: Optional[str]
    ) -> tuple[Optional[AudioPack], int]:
        pack = None
        if version is None:
            pack = self._pack
        elif self._pack is not None and self._pack.version == version:
            pack = self._pack
        else:
            pack = self._historical_packs.get(version)
            if pack is None:
                pack = AudioPack.open_version(self.pack_root, version)
                if pack is not None:
                    self._historical_packs[version] = pack
        pack_epoch = self._pack_epoch
        if pack is not None:
            self._pack_leases[pack] = self._pack_leases.get(pack, 0) + 1
        return pack, pack_epoch

    @staticmethod
    def _close_pack_safely(pack: AudioPack) -> None:
        try:
            pack.close()
        except (BufferError, OSError):
            pass

    def _retire_pack_locked(self, pack: Optional[AudioPack]) -> Optional[AudioPack]:
        if pack is None:
            return None
        if self._pack_leases.get(pack, 0):
            existing = self._historical_packs.get(pack.version)
            if existing is None:
                self._historical_packs[pack.version] = pack
            elif existing is not pack:
                self._retired_packs.add(pack)
            return None
        if self._historical_packs.get(pack.version) is pack:
            self._historical_packs.pop(pack.version, None)
        self._retired_packs.discard(pack)
        return pack

    def _reload_pack_unleased(self) -> bool:
        replacement = AudioPack.open_active(self.pack_root, self.db_path)
        with self._pack_guard:
            previous = self._pack
            self._pack = replacement
            self._pack_epoch += 1
            close_pack = self._retire_pack_locked(previous)
        self.response_cache.clear()
        if close_pack is not None:
            self._close_pack_safely(close_pack)
        return replacement is not None

    def reload_pack(self) -> bool:
        self._begin_state_transition()
        try:
            return self._reload_pack_unleased()
        finally:
            self._end_state_transition()

    def _close_database_locked(self) -> None:
        for connection in tuple(self._connections):
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self._connections.clear()
        self._available_connections.clear()
        self.row_cache.clear()
        self.response_cache.clear()

    def _begin_database_publish(self) -> None:
        self._publishing_database = True
        while self._active_queries:
            self._database_changed.wait()
        self._close_database_locked()

    def _end_database_publish(self) -> None:
        self._publishing_database = False
        self._database_changed.notify_all()

    def publish_database(self, temporary: Path) -> None:
        temporary = temporary.resolve(strict=True)
        with _PackBuildLock(self.pack_root / BUILD_LOCK_NAME):
            self._publish_database_locked(temporary)

    def _publish_database_locked(self, temporary: Path) -> None:
        """Publish while holding the same OS lock as pack build/cleanup."""

        packed_only_marker = self.pack_root / "packed-only-v1.json"
        if protected_cleanup_state_exists(self.pack_root):
            raise RuntimeError(
                "database publication is blocked while packed-only-v1.json exists"
            )
        replacement_rows = None
        replacement_seconds = 0.0
        memory_fallback = False
        if self.lookup_mode == "memory":
            try:
                replacement_rows, replacement_seconds = self._build_memory_rows(
                    temporary
                )
            except MemoryError:
                memory_fallback = True
        self._begin_state_transition()
        try:
            if protected_cleanup_state_exists(self.pack_root):
                raise RuntimeError(
                    "database publication became blocked before activation"
                )
            with self._database_changed:
                self._begin_database_publish()
                try:
                    os.replace(temporary, self.db_path)
                    if self.lookup_mode == "memory":
                        if memory_fallback:
                            self.lookup_mode = "sqlite"
                            self._memory_rows = None
                        else:
                            self._memory_rows = replacement_rows
                            self._memory_load_seconds = replacement_seconds
                    self._database_epoch += 1
                    self._legacy_path_rows = None
                    self.row_cache.clear()
                    self.response_cache.clear()
                    # Keep the publication gate closed while the compatible
                    # pack is selected, so new DB rows can never be paired with
                    # a row-id index for the previous database.
                    self._reload_pack_unleased()
                finally:
                    self._end_database_publish()
        finally:
            self._end_state_transition()

    def invalidate_database(self) -> None:
        with _PackBuildLock(self.pack_root / BUILD_LOCK_NAME):
            if protected_cleanup_state_exists(self.pack_root):
                raise RuntimeError(
                    "database invalidation is blocked while packed-only-v1.json exists"
                )
            self._begin_state_transition()
            try:
                with self._database_changed:
                    self._begin_database_publish()
                    try:
                        if self.lookup_mode == "memory":
                            try:
                                rows, elapsed = self._build_memory_rows()
                                self._memory_rows = rows
                                self._memory_load_seconds = elapsed
                            except MemoryError:
                                self.lookup_mode = "sqlite"
                                self._memory_rows = None
                        self._database_epoch += 1
                        self._legacy_path_rows = None
                        self.row_cache.clear()
                        self.response_cache.clear()
                        self._reload_pack_unleased()
                    finally:
                        self._end_database_publish()
            finally:
                self._end_state_transition()

    def replace_sources(self, sources: dict) -> None:
        """Atomically replace source roots after an existing-data import."""

        self._begin_state_transition()
        try:
            self.sources = dict(sources)
            self.source_ids = tuple(self.sources.keys())
            self._legacy_path_rows = None
            self.response_cache.clear()
        finally:
            self._end_state_transition()

    def close(self) -> None:
        if not self._begin_state_transition(allow_closed=True):
            return
        close_packs: list[AudioPack] = []
        try:
            # Mark closed while the transition gate is held.  New lookups and
            # direct audio leases will reject once the gate is released.
            with self._state_changed:
                self._closed = True
            with self._database_changed:
                self._begin_database_publish()
                try:
                    self._memory_rows = None
                    self._legacy_path_rows = None
                    self._database_epoch += 1
                finally:
                    self._end_database_publish()
            with self._pack_guard:
                packs = set(self._historical_packs.values())
                packs.update(self._retired_packs)
                if self._pack is not None:
                    packs.add(self._pack)
                self._pack = None
                self._historical_packs.clear()
                self._retired_packs.clear()
                self._pack_epoch += 1
                for pack in packs:
                    if self._pack_leases.get(pack, 0):
                        self._retired_packs.add(pack)
                    else:
                        close_packs.append(pack)
            self.row_cache.clear()
            self.response_cache.clear()
        finally:
            self._end_state_transition()
        for pack in close_packs:
            self._close_pack_safely(pack)

    def info(self) -> dict:
        with self._leased_state():
            with self._database_guard:
                database_connections = len(self._connections)
                idle_connections = len(self._available_connections)
            with self._pack_guard:
                pack = self._pack
                active_pack_leases = sum(self._pack_leases.values())
                retired_packs = len(self._retired_packs)
                historical_packs = len(self._historical_packs)
                pack_info = None
                if pack is not None:
                    pack_info = {
                        "version": pack.version,
                        "validRows": pack.valid_rows,
                        "packBytes": pack.pack_path.stat().st_size,
                    }
            return {
                "lookupMode": self.lookup_mode,
                "memoryLoadSeconds": self._memory_load_seconds,
                "responseCacheEntries": len(self.response_cache),
                "rowCacheEntries": len(self.row_cache),
                "databaseConnections": database_connections,
                "idleDatabaseConnections": idle_connections,
                "sources": list(self.source_ids),
                "audioPack": pack_info,
                "activePackLeases": active_pack_leases,
                "retiredPacksAwaitingLeases": retired_packs,
                "historicalPacksLeased": historical_packs,
            }
