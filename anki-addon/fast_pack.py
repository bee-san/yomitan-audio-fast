from __future__ import annotations

import hashlib
import json
import mmap
import os
import platform
import re
import shutil
import sqlite3
import stat
import struct
import time
import uuid

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


FORMAT_NAME = "local-audio-fast-pack-v1"
CHECKPOINT_FORMAT = "local-audio-fast-build-checkpoint-v2"
CHECKPOINT_NAME = "checkpoint.json"
VERSION_MANIFEST_NAME = "manifest.json"
INTEGRITY_FORMAT = "yomitan-audio-fast-integrity-v1"
INTEGRITY_FILE_NAME = "integrity.sha256.json"
PACK_FORMAT_LAF = "lafpack1"
PACK_FORMAT_RUST = "rust-yaf-v1"
INDEX_MAGIC = b"LAFAPX01"
PACK_MAGIC = b"LAFPACK1"
INDEX_HEADER = struct.Struct("<8sIIQQQQQ8x")
PACK_HEADER = struct.Struct("<8s56x")
RECORD = struct.Struct("<QIBB2x")
RECORD_VALID = 1
RUST_INDEX_MAGIC = b"YAFIDX1\0"
RUST_HEADER_SIZE = 160
RUST_AUDIO_RECORD = struct.Struct("<QQIIQHB5x")
MAX_AUDIO_BYTES = 64 * 1024 * 1024
CHECKPOINT_INTERVAL_SECONDS = 2.0
PROGRESS_INTERVAL_SECONDS = 0.1
BUILD_LOCK_NAME = "build.lock"
PACKED_ONLY_MARKER_NAME = "packed-only-v1.json"
LOOSE_AUDIO_QUARANTINE_NAME = "loose-audio-originals-v1"
VERSION_RE = re.compile(r"^[0-9a-f]{16}$")
BUILD_STAGE_RE = re.compile(r"^\.building-[0-9a-f]{32}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")

MIME_BY_ID = {
    1: "audio/mpeg",
    2: "audio/aac",
    3: "audio/mp4",
    4: "audio/ogg",
    5: "audio/flac",
    6: "audio/wav",
}
MIME_ID_BY_SUFFIX = {
    ".mp3": 1,
    ".aac": 2,
    ".m4a": 3,
    ".ogg": 4,
    ".oga": 4,
    ".opus": 4,
    ".flac": 5,
    ".wav": 6,
}


@dataclass(frozen=True)
class PackedAudio:
    audio_id: int
    offset: int
    length: int
    mime_type: str


@dataclass(frozen=True)
class PackProgress:
    stage: str
    current: int
    total: int
    message: str
    resumed: bool = False


class PackBuildCancelled(Exception):
    """A cooperative cancellation that leaves resumable staging data intact."""

    def __init__(self, progress: PackProgress) -> None:
        super().__init__(progress.message)
        self.progress = progress


class _PackBuildLock:
    """An OS-owned lock that disappears automatically after a crash."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RuntimeError("audio pack build lock path is a symbolic link")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise RuntimeError("audio pack build lock could not be opened safely") from error
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            os.close(descriptor)
            raise RuntimeError("audio pack build lock is not a private regular file")
        self.file = os.fdopen(descriptor, "r+b", buffering=0)
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"\0")
            self.file.flush()
        self.file.seek(0)
        try:
            if platform.system() == "Windows":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as error:
            self.file.close()
            self.file = None
            raise RuntimeError("another audio pack build is already running") from error
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        if self.file is None:
            return
        try:
            self.file.seek(0)
            if platform.system() == "Windows":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


def protected_cleanup_state_exists(pack_root: Path) -> bool:
    """Fail closed if cleanup metadata or its reserved quarantine is present."""

    return os.path.lexists(pack_root / PACKED_ONLY_MARKER_NAME) or os.path.lexists(
        pack_root / LOOSE_AUDIO_QUARANTINE_NAME
    )


class AudioPack:
    """Immutable, mmap-backed audio pack selected by a tiny atomic manifest."""

    def __init__(
        self,
        version: str,
        index_path: Path,
        pack_path: Path,
        maximum_id: int,
        valid_rows: int,
        minimum_pack_offset: int,
        db_size: int,
        db_mtime_ns: int,
    ) -> None:
        self.version = version
        self.index_path = index_path
        self.pack_path = pack_path
        self.maximum_id = maximum_id
        self.valid_rows = valid_rows
        self.minimum_pack_offset = minimum_pack_offset
        self.db_size = db_size
        self.db_mtime_ns = db_mtime_ns
        self._index_file = index_path.open("rb")
        self._pack_file = pack_path.open("rb")
        self._index = mmap.mmap(self._index_file.fileno(), 0, access=mmap.ACCESS_READ)
        self._pack = mmap.mmap(self._pack_file.fileno(), 0, access=mmap.ACCESS_READ)

    @classmethod
    def open_version(cls, pack_root: Path, version: str) -> Optional["AudioPack"]:
        if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
            return None
        try:
            versions_root = (pack_root / "versions").resolve()
            version_root = (versions_root / version).resolve()
            version_root.relative_to(versions_root)
            manifest_path = version_root / VERSION_MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("format") != FORMAT_NAME or manifest.get("version") != version:
                return None
            pack_format = manifest.get("pack_format")
            if pack_format not in (PACK_FORMAT_LAF, PACK_FORMAT_RUST):
                return None
            if manifest.get("index_file") != "audio.idx" or manifest.get("pack_file") != "audio.pack":
                return None
            index_path = version_root / "audio.idx"
            pack_path = version_root / "audio.pack"
            index_stat = index_path.stat()
            pack_stat = pack_path.stat()
            if (
                manifest.get("index_bytes") != index_stat.st_size
                or manifest.get("pack_bytes") != pack_stat.st_size
                or not isinstance(manifest.get("index_sha256"), str)
                or HASH_RE.fullmatch(manifest["index_sha256"].lower()) is None
                or not isinstance(manifest.get("pack_sha256"), str)
                or HASH_RE.fullmatch(manifest["pack_sha256"].lower()) is None
            ):
                return None
            index_file = index_path.open("rb")
            try:
                header = index_file.read(INDEX_HEADER.size)
            finally:
                index_file.close()
            (
                magic,
                format_version,
                record_size,
                maximum_id,
                valid_rows,
                pack_size,
                db_size,
                db_mtime_ns,
            ) = INDEX_HEADER.unpack(header)
            if (
                magic != INDEX_MAGIC
                or format_version != 1
                or record_size != RECORD.size
                or index_stat.st_size != INDEX_HEADER.size + (maximum_id + 1) * RECORD.size
                or pack_stat.st_size != pack_size
            ):
                return None
            minimum_pack_offset = 0
            if pack_format == PACK_FORMAT_LAF:
                pack_file = pack_path.open("rb")
                try:
                    if pack_file.read(PACK_HEADER.size)[:8] != PACK_MAGIC:
                        return None
                finally:
                    pack_file.close()
                minimum_pack_offset = PACK_HEADER.size
            return cls(
                version,
                index_path,
                pack_path,
                maximum_id,
                valid_rows,
                minimum_pack_offset,
                db_size,
                db_mtime_ns,
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, struct.error):
            return None

    @classmethod
    def open_active(cls, pack_root: Path, db_path: Path) -> Optional["AudioPack"]:
        manifest_path = pack_root / "active.json"
        if not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("format") != FORMAT_NAME:
                return None
            version = manifest.get("version")
            pack = cls.open_version(pack_root, version)
            if pack is None:
                return None
            db_stat = db_path.stat()
            if (
                pack.db_size != db_stat.st_size
                or pack.db_mtime_ns != db_stat.st_mtime_ns
            ):
                pack.close()
                return None
            return pack
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def get(self, audio_id: int) -> Optional[PackedAudio]:
        if audio_id < 0 or audio_id > self.maximum_id:
            return None
        offset = INDEX_HEADER.size + audio_id * RECORD.size
        pack_offset, length, mime_id, flags = RECORD.unpack_from(self._index, offset)
        mime_type = MIME_BY_ID.get(mime_id)
        if (
            flags & RECORD_VALID == 0
            or mime_type is None
            or length == 0
            or pack_offset < self.minimum_pack_offset
            or pack_offset + length > len(self._pack)
        ):
            return None
        return PackedAudio(audio_id, pack_offset, length, mime_type)

    def view(self, audio: PackedAudio, start: int, length: int) -> memoryview:
        absolute_start = audio.offset + start
        return memoryview(self._pack)[absolute_start : absolute_start + length]

    def close(self) -> None:
        self._index.close()
        self._pack.close()
        self._index_file.close()
        self._pack_file.close()


@dataclass(frozen=True)
class _AudioGroup:
    source: str
    filename: str
    row_ids: tuple[int, ...]


@dataclass(frozen=True)
class _ReadResult:
    group: _AudioGroup
    data: Optional[bytes]
    digest: Optional[bytes]
    mime_id: int
    source_state: bytes


def _group_rows(
    connection: sqlite3.Connection,
    after_key: Optional[tuple[str, str]] = None,
    through_key: Optional[tuple[str, str]] = None,
) -> Iterable[_AudioGroup]:
    conditions = []
    parameters: list[str] = []
    if after_key is not None:
        conditions.append("(source > ? OR (source = ? AND file > ?))")
        parameters.extend((after_key[0], after_key[0], after_key[1]))
    if through_key is not None:
        conditions.append("(source < ? OR (source = ? AND file <= ?))")
        parameters.extend((through_key[0], through_key[0], through_key[1]))
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    cursor = connection.execute(
        "SELECT id, source, file FROM entries"
        + where
        + " ORDER BY source, file, id",
        parameters,
    )
    current_key: Optional[tuple[str, str]] = None
    current_ids: list[int] = []
    for row_id, source, filename in cursor:
        key = source, filename
        if current_key is not None and key != current_key:
            yield _AudioGroup(current_key[0], current_key[1], tuple(current_ids))
            current_ids.clear()
        current_key = key
        current_ids.append(row_id)
    if current_key is not None:
        yield _AudioGroup(current_key[0], current_key[1], tuple(current_ids))


def _source_state_token(
    group: _AudioGroup,
    source_roots: dict[str, Path],
) -> tuple[Optional[Path], int, bytes]:
    """Return a safe audio path plus stable metadata used to validate a resume."""

    state: list[Any] = [group.source, group.filename]
    source_root = source_roots.get(group.source)
    if source_root is None:
        state.append("unknown-source")
        return None, 0, json.dumps(state, ensure_ascii=False).encode("utf-8")
    try:
        relative = Path(group.filename.replace("\\", "/"))
        if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
            state.append("unsafe-path")
            return None, 0, json.dumps(state, ensure_ascii=False).encode("utf-8")
        path = (source_root / relative).resolve(strict=True)
        path.relative_to(source_root)
        mime_id = MIME_ID_BY_SUFFIX.get(path.suffix.lower(), 0)
        if mime_id == 0:
            state.append("unsupported")
            return None, 0, json.dumps(state, ensure_ascii=False).encode("utf-8")
        file_stat = path.stat()
        if not path.is_file():
            state.append("not-file")
            return None, 0, json.dumps(state, ensure_ascii=False).encode("utf-8")
        state.extend(
            (
                "file",
                file_stat.st_dev,
                file_stat.st_ino,
                file_stat.st_mode,
                file_stat.st_size,
                file_stat.st_mtime_ns,
                file_stat.st_ctime_ns,
            )
        )
        return path, mime_id, json.dumps(
            state, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (OSError, ValueError):
        state.append("missing")
        return None, 0, json.dumps(
            state, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")


def _read_group(group: _AudioGroup, source_roots: dict[str, Path]) -> _ReadResult:
    path, mime_id, source_state = _source_state_token(group, source_roots)
    if path is None:
        return _ReadResult(group, None, None, 0, source_state)
    try:
        data = path.read_bytes()
        if len(data) <= 0 or len(data) > MAX_AUDIO_BYTES:
            return _ReadResult(group, None, None, 0, source_state)
        _path_after, _mime_after, state_after = _source_state_token(
            group, source_roots
        )
        if state_after != source_state:
            # A final source-state scan will reject publication. Returning no
            # bytes here also ensures a torn read is never placed in the pack.
            return _ReadResult(group, None, None, 0, source_state)
        digest = hashlib.blake2b(data, digest_size=32).digest()
        return _ReadResult(group, data, digest, mime_id, source_state)
    except (OSError, ValueError):
        return _ReadResult(group, None, None, 0, source_state)


def _manifest_file(bundle_root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("bundle manifest contains an invalid file path")
    relative = Path(value.replace("\\", "/"))
    if relative.is_absolute() or any(
        part in ("", ".", "..") for part in relative.parts
    ) or "\\" in value or relative.as_posix() != value:
        raise ValueError("bundle manifest contains an unsafe file path")
    result = (bundle_root / relative).resolve(strict=True)
    result.relative_to(bundle_root)
    if not result.is_file():
        raise ValueError("bundle manifest path is not a file")
    return result


def _integer_field(manifest: dict, name: str) -> int:
    value = manifest.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"bundle manifest field {name!r} is invalid")
    return value


def _file_fingerprint(stat: os.stat_result) -> tuple[int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _sha256_file_stable(
    path: Path,
    expected_size: Optional[int] = None,
    progress_callback: Optional[Callable[[PackProgress], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    stage: str = "verifying",
    resumed: bool = False,
) -> tuple[str, tuple[int, int, int, int]]:
    before = path.stat()
    if expected_size is not None and before.st_size != expected_size:
        raise ValueError(f"integrity size mismatch for {path.name}")
    digest = hashlib.sha256()
    processed = 0
    last_progress_at = 0.0
    if should_cancel is not None and should_cancel():
        raise PackBuildCancelled(
            PackProgress(
                stage,
                0,
                before.st_size,
                f"Paused before verifying {path.name}",
                resumed,
            )
        )
    with path.open("rb", buffering=4 * 1024 * 1024) as file:
        while chunk := file.read(4 * 1024 * 1024):
            if should_cancel is not None and should_cancel():
                raise PackBuildCancelled(
                    PackProgress(
                        stage,
                        processed,
                        before.st_size,
                        f"Paused while verifying {path.name}",
                        resumed,
                    )
                )
            digest.update(chunk)
            processed += len(chunk)
            now = time.monotonic()
            if progress_callback is not None and (
                processed == before.st_size
                or now - last_progress_at >= PROGRESS_INTERVAL_SECONDS
            ):
                progress_callback(
                    PackProgress(
                        stage,
                        processed,
                        before.st_size,
                        f"Verifying {path.name}: {processed:,}/{before.st_size:,} bytes",
                        resumed,
                    )
                )
                last_progress_at = now
    after = path.stat()
    before_fingerprint = _file_fingerprint(before)
    if _file_fingerprint(after) != before_fingerprint:
        raise RuntimeError(f"file changed during integrity hashing: {path}")
    return digest.hexdigest(), before_fingerprint


def _verify_rust_integrity(
    bundle_root: Path,
    manifest: dict,
    lookup_path: Path,
    pack_path: Path,
) -> dict:
    integrity_path = bundle_root / INTEGRITY_FILE_NAME
    integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
    if (
        integrity.get("format") != INTEGRITY_FORMAT
        or integrity.get("bundleVersion") != manifest.get("bundleVersion")
    ):
        raise ValueError("Rust integrity sidecar format/version mismatch")
    lookup_relative = manifest.get("lookupFile")
    pack_relative = manifest.get("packFile")
    files = integrity.get("files")
    if not isinstance(files, dict) or set(files) != {lookup_relative, pack_relative}:
        raise ValueError("Rust integrity sidecar file set does not match manifest")
    verified = {}
    for relative, path in (
        (lookup_relative, lookup_path),
        (pack_relative, pack_path),
    ):
        item = files.get(relative)
        if not isinstance(item, dict):
            raise ValueError("Rust integrity sidecar entry is invalid")
        expected_bytes = item.get("bytes")
        expected_sha256 = item.get("sha256")
        if (
            not isinstance(expected_bytes, int)
            or isinstance(expected_bytes, bool)
            or expected_bytes < 0
            or not isinstance(expected_sha256, str)
            or HASH_RE.fullmatch(expected_sha256.lower()) is None
        ):
            raise ValueError("Rust integrity sidecar size/hash is invalid")
        actual_sha256, fingerprint = _sha256_file_stable(path, expected_bytes)
        if actual_sha256 != expected_sha256.lower():
            raise ValueError(f"Rust integrity SHA-256 mismatch for {relative}")
        verified[relative] = {
            "bytes": expected_bytes,
            "sha256": actual_sha256,
            "fingerprint": fingerprint,
        }
    return verified


def _write_version_manifest(
    version_root: Path,
    version: str,
    pack_format: str,
    index_sha256: str,
    pack_sha256: str,
) -> dict:
    manifest = {
        "format": FORMAT_NAME,
        "version": version,
        "pack_format": pack_format,
        "index_file": "audio.idx",
        "pack_file": "audio.pack",
        "index_bytes": (version_root / "audio.idx").stat().st_size,
        "pack_bytes": (version_root / "audio.pack").stat().st_size,
        "index_sha256": index_sha256,
        "pack_sha256": pack_sha256,
    }
    _atomic_json_file(version_root / VERSION_MANIFEST_NAME, manifest)
    return manifest


def import_rust_bundle(
    db_path: Path,
    pack_root: Path,
    bundle_root: Path,
    callback: Optional[Callable[[str], None]] = None,
) -> dict:
    pack_root = pack_root.resolve()
    packed_only_marker = pack_root / PACKED_ONLY_MARKER_NAME
    if protected_cleanup_state_exists(pack_root):
        raise RuntimeError(
            "Rust bundle import is blocked while packed-only-v1.json exists"
        )
    with _PackBuildLock(pack_root / BUILD_LOCK_NAME):
        if protected_cleanup_state_exists(pack_root):
            raise RuntimeError(
                "Rust bundle import became blocked before publication"
            )
        return _import_rust_bundle_locked(
            db_path,
            pack_root,
            bundle_root,
            callback,
        )


def _import_rust_bundle_locked(
    db_path: Path,
    pack_root: Path,
    bundle_root: Path,
    callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """Publish a row-id index over a verified Rust pack without rereading audio.

    The Rust compiler stores one sorted audio-table record per distinct
    ``(source, file)`` pair.  The legacy database is scanned in the identical
    SQLite order, so this importer can merge the two streams and emit the
    add-on's direct 16-byte row-id index in linear time.  ``audio.pack`` is an
    NTFS hardlink: no 1.7 GiB copy and no duplicate on-disk payload.
    """

    started = __import__("time").perf_counter()
    db_path = db_path.resolve(strict=True)
    bundle_root = bundle_root.resolve(strict=True)
    versions_root = pack_root / "versions"
    versions_root.mkdir(parents=True, exist_ok=True)
    manifest_path = bundle_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("formatVersion") != 1:
        raise ValueError("unsupported Rust bundle format")
    upstream_version = manifest.get("bundleVersion")
    if not isinstance(upstream_version, str) or VERSION_RE.fullmatch(upstream_version) is None:
        raise ValueError("invalid Rust bundle version")
    for hash_name in ("lookupBlake3", "packBlake3"):
        value = manifest.get(hash_name)
        if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
            raise ValueError(f"invalid Rust bundle {hash_name}")
    lookup_path = _manifest_file(bundle_root, manifest.get("lookupFile"))
    source_pack_path = _manifest_file(bundle_root, manifest.get("packFile"))
    verified_integrity = _verify_rust_integrity(
        bundle_root, manifest, lookup_path, source_pack_path
    )
    lookup_integrity = verified_integrity[manifest["lookupFile"]]
    pack_integrity = verified_integrity[manifest["packFile"]]
    pack_size = source_pack_path.stat().st_size
    if pack_size != _integer_field(manifest, "packBytes"):
        raise ValueError("Rust pack size does not match its manifest")
    source_specs = manifest.get("sources")
    if not isinstance(source_specs, list) or not source_specs:
        raise ValueError("Rust bundle has no source table")
    source_ids: list[str] = []
    for source in source_specs:
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            raise ValueError("Rust bundle source table is invalid")
        source_id = source["id"]
        if not source_id or source_id in source_ids:
            raise ValueError("Rust bundle source IDs must be non-empty and unique")
        source_ids.append(source_id)

    db_stat = db_path.stat()
    uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    lookup_file = None
    lookup_map: Optional[mmap.mmap] = None
    index_file = None
    index_map: Optional[mmap.mmap] = None
    build_root = versions_root / f".building-{uuid.uuid4().hex}"
    build_root.mkdir()
    index_path = build_root / "audio.idx"
    pack_path = build_root / "audio.pack"
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA mmap_size=268435456")
        maximum_id, total_rows = connection.execute(
            "SELECT COALESCE(MAX(id), 0), COUNT(*) FROM entries"
        ).fetchone()
        if total_rows != _integer_field(manifest, "recordCount"):
            raise ValueError("Rust bundle and entries.db have different row counts")
        audio_count = _integer_field(manifest, "audioCount")
        if audio_count == 0:
            raise ValueError("Rust bundle audio table is empty")

        lookup_file = lookup_path.open("rb")
        lookup_map = mmap.mmap(lookup_file.fileno(), 0, access=mmap.ACCESS_READ)
        if len(lookup_map) < RUST_HEADER_SIZE:
            raise ValueError("Rust lookup header is truncated")
        if lookup_map[:8] != RUST_INDEX_MAGIC:
            raise ValueError("Rust lookup magic is invalid")
        format_version, header_size = struct.unpack_from("<II", lookup_map, 8)
        if format_version != 1 or header_size != RUST_HEADER_SIZE:
            raise ValueError("Rust lookup header version is unsupported")
        lookup_record_count, lookup_audio_count = struct.unpack_from(
            "<QQ", lookup_map, 24
        )
        audio_table_offset, strings_offset, strings_length = struct.unpack_from(
            "<QQQ", lookup_map, 64
        )
        if lookup_record_count != total_rows or lookup_audio_count != audio_count:
            raise ValueError("Rust lookup counts do not match the manifest/database")
        audio_table_end = audio_table_offset + audio_count * RUST_AUDIO_RECORD.size
        strings_end = strings_offset + strings_length
        if (
            audio_table_offset < RUST_HEADER_SIZE
            or audio_table_end > strings_offset
            or strings_offset < RUST_HEADER_SIZE
            or strings_end > len(lookup_map)
        ):
            raise ValueError("Rust lookup audio/string table is outside the file")

        index_file = index_path.open("w+b", buffering=1024 * 1024)
        index_file.truncate(INDEX_HEADER.size + (maximum_id + 1) * RECORD.size)
        index_file.flush()
        index_map = mmap.mmap(index_file.fileno(), 0, access=mmap.ACCESS_WRITE)
        groups = iter(_group_rows(connection))
        previous_key: Optional[tuple[str, str]] = None
        valid_rows = 0
        for audio_id in range(audio_count):
            position = audio_table_offset + audio_id * RUST_AUDIO_RECORD.size
            (
                pack_offset,
                length,
                path_offset,
                path_length,
                _content_hash_prefix,
                source_index,
                mime_id,
            ) = RUST_AUDIO_RECORD.unpack_from(lookup_map, position)
            if source_index >= len(source_ids) or mime_id not in MIME_BY_ID:
                raise ValueError(f"Rust audio record {audio_id} has invalid metadata")
            path_start = strings_offset + path_offset
            path_end = path_start + path_length
            if path_start < strings_offset or path_end > strings_end:
                raise ValueError(f"Rust audio record {audio_id} has an invalid path")
            try:
                filename = lookup_map[path_start:path_end].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(
                    f"Rust audio record {audio_id} path is not UTF-8"
                ) from error
            relative = Path(filename.replace("\\", "/"))
            if relative.is_absolute() or any(
                part in ("", ".", "..") for part in relative.parts
            ):
                raise ValueError(f"Rust audio record {audio_id} path is unsafe")
            if length <= 0 or length > MAX_AUDIO_BYTES or pack_offset > pack_size - length:
                raise ValueError(f"Rust audio record {audio_id} is outside audio.pack")
            key = source_ids[source_index], filename
            if previous_key is not None and key <= previous_key:
                raise ValueError("Rust audio table is not strictly sorted by source/path")
            previous_key = key
            try:
                group = next(groups)
            except StopIteration as error:
                raise ValueError("Rust audio table has more assets than entries.db") from error
            if (group.source, group.filename) != key:
                raise ValueError(
                    "Rust bundle does not match entries.db at "
                    f"{key!r} != {(group.source, group.filename)!r}"
                )
            for row_id in group.row_ids:
                RECORD.pack_into(
                    index_map,
                    INDEX_HEADER.size + row_id * RECORD.size,
                    pack_offset,
                    length,
                    mime_id,
                    RECORD_VALID,
                )
            valid_rows += len(group.row_ids)
            if callback is not None and (audio_id + 1) % 50000 == 0:
                callback(
                    f"Indexed {audio_id + 1:,}/{audio_count:,} Rust audio assets"
                )
        try:
            unexpected_group = next(groups)
        except StopIteration:
            unexpected_group = None
        if unexpected_group is not None:
            raise ValueError("entries.db has assets missing from the Rust audio table")
        if valid_rows != total_rows:
            raise ValueError("not every entries.db row was assigned packed audio")
        final_db_stat = db_path.stat()
        if (
            final_db_stat.st_size != db_stat.st_size
            or final_db_stat.st_mtime_ns != db_stat.st_mtime_ns
        ):
            raise RuntimeError("entries.db changed during Rust bundle import")

        if _file_fingerprint(lookup_path.stat()) != lookup_integrity["fingerprint"]:
            raise RuntimeError("Rust lookup changed after integrity verification")
        if _file_fingerprint(source_pack_path.stat()) != pack_integrity["fingerprint"]:
            raise RuntimeError("Rust pack changed after integrity verification")
        os.link(source_pack_path, pack_path)
        if (
            not os.path.samefile(source_pack_path, pack_path)
            or _file_fingerprint(pack_path.stat()) != pack_integrity["fingerprint"]
        ):
            raise RuntimeError("audio.pack publication did not create a hardlink")
        INDEX_HEADER.pack_into(
            index_map,
            0,
            INDEX_MAGIC,
            1,
            RECORD.size,
            maximum_id,
            valid_rows,
            pack_size,
            db_stat.st_size,
            db_stat.st_mtime_ns,
        )
        index_map.flush()
        index_file.flush()
        os.fsync(index_file.fileno())
    except BaseException:
        if index_map is not None:
            index_map.close()
            index_map = None
        if index_file is not None:
            index_file.close()
            index_file = None
        if lookup_map is not None:
            lookup_map.close()
            lookup_map = None
        if lookup_file is not None:
            lookup_file.close()
            lookup_file = None
        connection.close()
        resolved_build = build_root.resolve()
        resolved_build.relative_to(versions_root.resolve())
        if resolved_build.name.startswith(".building-"):
            shutil.rmtree(resolved_build)
        raise
    finally:
        if index_map is not None:
            index_map.close()
        if index_file is not None:
            index_file.close()
        if lookup_map is not None:
            lookup_map.close()
        if lookup_file is not None:
            lookup_file.close()
        connection.close()

    version_seed = (
        f"{upstream_version}\0{db_stat.st_size}\0{db_stat.st_mtime_ns}".encode()
    )
    version = hashlib.sha256(version_seed).hexdigest()[:16]
    index_sha256, _index_fingerprint = _sha256_file_stable(index_path)
    pack_sha256 = pack_integrity["sha256"]
    version_root = versions_root / version
    if version_root.exists():
        existing_index = version_root / "audio.idx"
        existing_pack = version_root / "audio.pack"
        compatible = False
        if existing_index.is_file() and existing_pack.is_file():
            existing_index_sha256, _ = _sha256_file_stable(existing_index)
            compatible = (
                existing_index_sha256 == index_sha256
                and os.path.samefile(source_pack_path, existing_pack)
                and _file_fingerprint(existing_pack.stat())
                == pack_integrity["fingerprint"]
            )
        if not compatible:
            shutil.rmtree(build_root)
            raise FileExistsError(f"incompatible published pack version exists: {version}")
        _write_version_manifest(
            version_root,
            version,
            PACK_FORMAT_RUST,
            index_sha256,
            pack_sha256,
        )
        opened = AudioPack.open_version(pack_root, version)
        if opened is None:
            shutil.rmtree(build_root)
            raise ValueError("existing packed version failed structural validation")
        opened.close()
        shutil.rmtree(build_root)
    else:
        _write_version_manifest(
            build_root,
            version,
            PACK_FORMAT_RUST,
            index_sha256,
            pack_sha256,
        )
        os.replace(build_root, version_root)
        opened = AudioPack.open_version(pack_root, version)
        if opened is None:
            raise ValueError("published packed version failed structural validation")
        opened.close()
    result = {
        "format": FORMAT_NAME,
        "pack_format": PACK_FORMAT_RUST,
        "version": version,
        "index": f"versions/{version}/audio.idx",
        "pack": f"versions/{version}/audio.pack",
        "database": {
            "size": db_stat.st_size,
            "mtime_ns": db_stat.st_mtime_ns,
            "maximum_id": maximum_id,
            "rows": total_rows,
        },
        "valid_rows": valid_rows,
        "mapping_rows_reusing_source_paths": total_rows - audio_count,
        "source_path_references": audio_count,
        "readable_source_paths": audio_count,
        "unique_files": _integer_field(manifest, "uniqueBlobCount"),
        "content_duplicates": _integer_field(manifest, "identicalContentAssets"),
        "deduplicated_bytes_saved": _integer_field(manifest, "deduplicatedBytes"),
        "missing_files": 0,
        "pack_bytes": pack_size,
        "hardlinked_pack": True,
        "integrity": {
            "index_sha256": index_sha256,
            "pack_sha256": pack_sha256,
            "rust_lookup_sha256": lookup_integrity["sha256"],
        },
        "upstream_bundle": {
            "format_version": 1,
            "version": upstream_version,
            "lookup_blake3": manifest["lookupBlake3"],
            "pack_blake3": manifest["packBlake3"],
        },
    }
    _atomic_json_file(pack_root / "active.json", result)
    result["elapsed_seconds"] = __import__("time").perf_counter() - started
    return result


def _atomic_json_file(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            file.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _remove_build_root(build_root: Path, versions_root: Path) -> None:
    if build_root.is_symlink() or not build_root.exists():
        return
    resolved_versions = versions_root.resolve()
    resolved_build = build_root.resolve()
    resolved_build.relative_to(resolved_versions)
    if (
        resolved_build.parent != resolved_versions
        or not resolved_build.name.startswith(".building-")
    ):
        raise ValueError("refusing to remove an unsafe pack staging directory")
    shutil.rmtree(resolved_build)


def _cleanup_stale_build_roots(versions_root: Path, keep: Path) -> None:
    """Remove only generated stages while the cross-process lock is held."""

    for candidate in versions_root.iterdir():
        if candidate == keep or BUILD_STAGE_RE.fullmatch(candidate.name) is None:
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        _remove_build_root(candidate, versions_root)


def _pack_build_identity(
    db_path: Path,
    db_stat: os.stat_result,
    maximum_id: int,
    total_rows: int,
    source_roots: dict[str, Path],
) -> tuple[str, dict]:
    identity = {
        "format": CHECKPOINT_FORMAT,
        "database": {
            "path": str(db_path),
            "size": db_stat.st_size,
            "mtime_ns": db_stat.st_mtime_ns,
            "maximum_id": maximum_id,
            "rows": total_rows,
        },
        "source_roots": [
            [source_id, str(root)] for source_id, root in sorted(source_roots.items())
        ],
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32], identity


_CHECKPOINT_COUNTERS = (
    "processed_source_paths",
    "processed_rows",
    "valid_rows",
    "readable_source_paths",
    "unique_files",
    "content_duplicates",
    "deduplicated_bytes_saved",
    "missing_files",
)


def _load_build_checkpoint(
    build_root: Path,
    build_id: str,
    identity: dict,
    expected_index_bytes: int,
) -> Optional[dict]:
    try:
        checkpoint = json.loads(
            (build_root / CHECKPOINT_NAME).read_text(encoding="utf-8")
        )
        if (
            checkpoint.get("format") != CHECKPOINT_FORMAT
            or checkpoint.get("build_id") != build_id
            or checkpoint.get("identity") != identity
            or checkpoint.get("phase") not in ("packing", "finalizing")
        ):
            return None
        last_key = checkpoint.get("last_key")
        if last_key is not None and (
            not isinstance(last_key, list)
            or len(last_key) != 2
            or not all(isinstance(value, str) for value in last_key)
        ):
            return None
        for name in _CHECKPOINT_COUNTERS + ("pack_bytes",):
            value = checkpoint.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return None
        final_version = checkpoint.get("final_version")
        if final_version is not None and (
            not isinstance(final_version, str)
            or VERSION_RE.fullmatch(final_version) is None
        ):
            return None
        source_state_sha256 = checkpoint.get("source_state_sha256")
        if (
            not isinstance(source_state_sha256, str)
            or HASH_RE.fullmatch(source_state_sha256) is None
        ):
            return None
        index_path = build_root / "audio.idx"
        pack_path = build_root / "audio.pack"
        with pack_path.open("rb") as pack_file:
            pack_magic = pack_file.read(PACK_HEADER.size)
        if (
            index_path.stat().st_size != expected_index_bytes
            or checkpoint["pack_bytes"] < PACK_HEADER.size
            or pack_path.stat().st_size < checkpoint["pack_bytes"]
            or pack_magic != PACK_HEADER.pack(PACK_MAGIC)
        ):
            return None
        return checkpoint
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _clear_group_records(index_map: mmap.mmap, group: _AudioGroup) -> None:
    empty = bytes(RECORD.size)
    for row_id in group.row_ids:
        start = INDEX_HEADER.size + row_id * RECORD.size
        index_map[start : start + RECORD.size] = empty


def _restore_partial_build(
    connection: sqlite3.Connection,
    index_map: mmap.mmap,
    pack_file,
    checkpoint: dict,
    source_roots: dict[str, Path],
    progress_callback: Optional[Callable[[PackProgress], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> tuple[
    dict[str, int],
    dict[tuple[bytes, int], list[tuple[int, int]]],
    Any,
    Any,
]:
    counters = {name: 0 for name in _CHECKPOINT_COUNTERS}
    digest_to_records: dict[tuple[bytes, int], list[tuple[int, int]]] = {}
    seen_offsets: set[int] = set()
    length_by_offset: dict[int, int] = {}
    digest_by_record: dict[tuple[int, int], bytes] = {}
    version_hash = hashlib.sha256()
    source_state_hash = hashlib.sha256()
    database = checkpoint["identity"]["database"]
    version_hash.update(str(database["size"]).encode())
    version_hash.update(str(database["mtime_ns"]).encode())
    last_key_value = checkpoint.get("last_key")
    last_key = tuple(last_key_value) if last_key_value is not None else None
    actual_last_key = None
    last_progress_at = 0.0
    for group in _group_rows(connection, through_key=last_key):
        if should_cancel is not None and should_cancel():
            raise PackBuildCancelled(
                PackProgress(
                    "resuming",
                    counters["processed_rows"],
                    database["rows"],
                    "Resume paused safely; the existing checkpoint was kept.",
                    True,
                )
            )
        actual_last_key = (group.source, group.filename)
        _path, _mime_id, source_state = _source_state_token(group, source_roots)
        source_state_hash.update(source_state)
        counters["processed_source_paths"] += 1
        counters["processed_rows"] += len(group.row_ids)
        records = [
            RECORD.unpack_from(index_map, INDEX_HEADER.size + row_id * RECORD.size)
            for row_id in group.row_ids
        ]
        first = records[0]
        if any(record != first for record in records):
            raise ValueError("checkpoint contains inconsistent row index records")
        pack_offset, length, mime_id, flags = first
        if flags & RECORD_VALID:
            if (
                mime_id not in MIME_BY_ID
                or length <= 0
                or length > MAX_AUDIO_BYTES
                or pack_offset < PACK_HEADER.size
                or pack_offset + length > checkpoint["pack_bytes"]
            ):
                raise ValueError("checkpoint contains an invalid packed-audio record")
            record_key = pack_offset, length
            previous_length = length_by_offset.setdefault(pack_offset, length)
            if previous_length != length:
                raise ValueError("checkpoint reuses a pack offset with another length")
            digest = digest_by_record.get(record_key)
            if digest is None:
                pack_file.seek(pack_offset)
                data = pack_file.read(length)
                if len(data) != length:
                    raise ValueError("checkpoint audio pack is truncated")
                digest = hashlib.blake2b(data, digest_size=32).digest()
                digest_by_record[record_key] = digest
            dedupe_key = digest, length
            if pack_offset in seen_offsets:
                counters["content_duplicates"] += 1
                counters["deduplicated_bytes_saved"] += length
            else:
                seen_offsets.add(pack_offset)
                digest_to_records.setdefault(dedupe_key, []).append(
                    (pack_offset, length)
                )
                counters["unique_files"] += 1
            counters["readable_source_paths"] += 1
            counters["valid_rows"] += len(group.row_ids)
            version_hash.update(group.source.encode("utf-8"))
            version_hash.update(b"\0")
            version_hash.update(group.filename.encode("utf-8"))
            version_hash.update(b"\0")
            version_hash.update(digest)
        else:
            if first != (0, 0, 0, 0):
                raise ValueError("checkpoint contains a malformed missing-file record")
            counters["missing_files"] += 1
        now = time.monotonic()
        if (
            progress_callback is not None
            and now - last_progress_at >= PROGRESS_INTERVAL_SECONDS
        ):
            progress_callback(
                PackProgress(
                    "resuming",
                    counters["processed_rows"],
                    database["rows"],
                    "Restoring saved pack state and checking source files: "
                    f"{counters['processed_rows']:,}/{database['rows']:,} rows",
                    True,
                )
            )
            last_progress_at = now
    if actual_last_key != last_key:
        raise ValueError("checkpoint boundary no longer matches entries.db")
    if any(counters[name] != checkpoint[name] for name in _CHECKPOINT_COUNTERS):
        raise ValueError("checkpoint counters do not match its pack/index data")
    if source_state_hash.hexdigest() != checkpoint["source_state_sha256"]:
        raise ValueError("source audio changed since the pack checkpoint")
    pack_file.seek(checkpoint["pack_bytes"])
    return counters, digest_to_records, version_hash, source_state_hash


def build_audio_pack(
    db_path: Path,
    pack_root: Path,
    sources: dict,
    callback: Optional[Callable[[str], None]] = None,
    workers: Optional[int] = None,
    progress_callback: Optional[Callable[[PackProgress], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Build, checkpoint, resume, validate, then atomically activate an audio pack."""

    pack_root = pack_root.resolve()
    # A packed-only marker means loose originals were intentionally removed and
    # this pack is the serving copy. This core check protects standalone callers
    # as well as the Anki GUI from publishing an incomplete replacement.
    packed_only_marker = pack_root / PACKED_ONLY_MARKER_NAME
    if protected_cleanup_state_exists(pack_root):
        raise RuntimeError(
            "audio pack rebuild is blocked while packed-only-v1.json exists; "
            "restore and verify the cleanup quarantine first"
        )
    with _PackBuildLock(pack_root / BUILD_LOCK_NAME):
        if protected_cleanup_state_exists(pack_root):
            raise RuntimeError(
                "audio pack rebuild is blocked while packed-only-v1.json exists"
            )
        return _build_audio_pack_locked(
            db_path,
            pack_root,
            sources,
            callback=callback,
            workers=workers,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
        )


def _build_audio_pack_locked(
    db_path: Path,
    pack_root: Path,
    sources: dict,
    callback: Optional[Callable[[str], None]] = None,
    workers: Optional[int] = None,
    progress_callback: Optional[Callable[[PackProgress], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Implementation guarded by the cross-process pack build lock."""

    db_path = db_path.resolve(strict=True)
    versions_root = pack_root / "versions"
    versions_root.mkdir(parents=True, exist_ok=True)
    source_roots = {
        source_id: source.get_media_dir_path().resolve()
        for source_id, source in sources.items()
    }
    workers = workers or min(32, max(4, (os.cpu_count() or 4) * 2))
    window = max(64, workers * 4)
    db_stat = db_path.stat()
    uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA mmap_size=268435456")
    maximum_id, total_rows = connection.execute(
        "SELECT COALESCE(MAX(id), 0), COUNT(*) FROM entries"
    ).fetchone()
    build_id, identity = _pack_build_identity(
        db_path, db_stat, maximum_id, total_rows, source_roots
    )
    build_root = versions_root / f".building-{build_id}"
    _cleanup_stale_build_roots(versions_root, build_root)
    if build_root.is_symlink() or (build_root.exists() and not build_root.is_dir()):
        connection.close()
        raise ValueError("audio pack staging path is not a safe directory")
    expected_index_bytes = INDEX_HEADER.size + (maximum_id + 1) * RECORD.size
    checkpoint = None
    if build_root.is_dir() and not build_root.is_symlink():
        checkpoint = _load_build_checkpoint(
            build_root, build_id, identity, expected_index_bytes
        )
    if checkpoint is None and build_root.exists():
        _remove_build_root(build_root, versions_root)
    if not build_root.exists():
        build_root.mkdir()
    index_path = build_root / "audio.idx"
    pack_path = build_root / "audio.pack"
    resumed = checkpoint is not None
    started = time.perf_counter()

    def emit(progress: PackProgress) -> None:
        if progress_callback is not None:
            progress_callback(progress)
        elif callback is not None:
            callback(progress.message)

    counters = {name: 0 for name in _CHECKPOINT_COUNTERS}
    digest_to_records: dict[tuple[bytes, int], list[tuple[int, int]]] = {}
    version_hash = hashlib.sha256()
    source_state_hash = hashlib.sha256()
    version_hash.update(str(db_stat.st_size).encode())
    version_hash.update(str(db_stat.st_mtime_ns).encode())
    last_key: Optional[tuple[str, str]] = None
    final_version: Optional[str] = None
    index_file = None
    pack_file = None
    index_map: Optional[mmap.mmap] = None
    checkpoint_writable = False

    def packing_progress(message: Optional[str] = None) -> PackProgress:
        prefix = "Resumed. " if resumed else ""
        label = message or (
            f"{prefix}Processed {counters['processed_source_paths']:,} source paths; "
            f"packed {counters['unique_files']:,} unique blobs; "
            f"skipped {counters['missing_files']:,}"
        )
        return PackProgress(
            "packing",
            counters["processed_rows"],
            total_rows,
            label,
            resumed,
        )

    def check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise PackBuildCancelled(
                packing_progress(
                    "Pack build paused safely; completed work will resume next time."
                )
            )

    def write_checkpoint(phase: str, version: Optional[str] = None) -> None:
        assert index_file is not None and index_map is not None and pack_file is not None
        pack_file.seek(0, os.SEEK_END)
        pack_bytes = pack_file.tell()
        pack_file.flush()
        os.fsync(pack_file.fileno())
        index_map.flush()
        index_file.flush()
        os.fsync(index_file.fileno())
        value = {
            "format": CHECKPOINT_FORMAT,
            "build_id": build_id,
            "identity": identity,
            "phase": phase,
            "last_key": list(last_key) if last_key is not None else None,
            "pack_bytes": pack_bytes,
            "final_version": version,
            "source_state_sha256": source_state_hash.hexdigest(),
        }
        value.update(counters)
        _atomic_json_file(build_root / CHECKPOINT_NAME, value)

    try:
        if resumed:
            assert checkpoint is not None
            index_file = index_path.open("r+b", buffering=1024 * 1024)
            pack_file = pack_path.open("r+b", buffering=1024 * 1024)
            pack_file.truncate(checkpoint["pack_bytes"])
            pack_file.flush()
            index_map = mmap.mmap(index_file.fileno(), 0, access=mmap.ACCESS_WRITE)
            try:
                (
                    counters,
                    digest_to_records,
                    version_hash,
                    source_state_hash,
                ) = _restore_partial_build(
                    connection,
                    index_map,
                    pack_file,
                    checkpoint,
                    source_roots,
                    progress_callback=emit,
                    should_cancel=should_cancel,
                )
            except (OSError, ValueError, struct.error):
                index_map.close()
                index_map = None
                index_file.close()
                index_file = None
                pack_file.close()
                pack_file = None
                _remove_build_root(build_root, versions_root)
                build_root.mkdir()
                index_file = index_path.open("w+b", buffering=1024 * 1024)
                pack_file = pack_path.open("w+b", buffering=1024 * 1024)
                index_file.truncate(expected_index_bytes)
                index_file.flush()
                index_map = mmap.mmap(index_file.fileno(), 0, access=mmap.ACCESS_WRITE)
                pack_file.write(PACK_HEADER.pack(PACK_MAGIC))
                counters = {name: 0 for name in _CHECKPOINT_COUNTERS}
                digest_to_records = {}
                version_hash = hashlib.sha256()
                version_hash.update(str(db_stat.st_size).encode())
                version_hash.update(str(db_stat.st_mtime_ns).encode())
                source_state_hash = hashlib.sha256()
                resumed = False
                checkpoint = None
                checkpoint_writable = True
            else:
                last_key_value = checkpoint.get("last_key")
                last_key = (
                    tuple(last_key_value) if last_key_value is not None else None
                )
                final_version = checkpoint.get("final_version")
                checkpoint_writable = True
        else:
            index_file = index_path.open("w+b", buffering=1024 * 1024)
            pack_file = pack_path.open("w+b", buffering=1024 * 1024)
            index_file.truncate(expected_index_bytes)
            index_file.flush()
            index_map = mmap.mmap(index_file.fileno(), 0, access=mmap.ACCESS_WRITE)
            pack_file.write(PACK_HEADER.pack(PACK_MAGIC))
            checkpoint_writable = True

        assert index_map is not None and pack_file is not None
        if checkpoint is None:
            write_checkpoint("packing")
        emit(packing_progress())
        check_cancel()

        def consume(result: _ReadResult) -> None:
            group = result.group
            _clear_group_records(index_map, group)
            source_state_hash.update(result.source_state)
            counters["processed_source_paths"] += 1
            counters["processed_rows"] += len(group.row_ids)
            if result.data is None or result.digest is None:
                counters["missing_files"] += 1
                return
            counters["readable_source_paths"] += 1
            dedupe_key = result.digest, len(result.data)
            pack_offset = -1
            for candidate_offset, candidate_length in digest_to_records.get(
                dedupe_key, ()
            ):
                end_position = pack_file.tell()
                pack_file.flush()
                pack_file.seek(candidate_offset)
                collision_check = pack_file.read(candidate_length)
                pack_file.seek(end_position)
                if collision_check == result.data:
                    pack_offset = candidate_offset
                    break
            if pack_offset < 0:
                pack_file.seek(0, os.SEEK_END)
                pack_offset = pack_file.tell()
                pack_file.write(result.data)
                digest_to_records.setdefault(dedupe_key, []).append(
                    (pack_offset, len(result.data))
                )
                counters["unique_files"] += 1
            else:
                counters["content_duplicates"] += 1
                counters["deduplicated_bytes_saved"] += len(result.data)
            length = len(result.data)
            for row_id in group.row_ids:
                RECORD.pack_into(
                    index_map,
                    INDEX_HEADER.size + row_id * RECORD.size,
                    pack_offset,
                    length,
                    result.mime_id,
                    RECORD_VALID,
                )
            counters["valid_rows"] += len(group.row_ids)
            version_hash.update(group.source.encode("utf-8"))
            version_hash.update(b"\0")
            version_hash.update(group.filename.encode("utf-8"))
            version_hash.update(b"\0")
            version_hash.update(result.digest)

        pending: deque[Future[_ReadResult]] = deque()
        executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="audio-pack"
        )
        last_checkpoint_at = time.monotonic()
        last_progress_at = 0.0
        try:
            for group in _group_rows(connection, after_key=last_key):
                check_cancel()
                pending.append(executor.submit(_read_group, group, source_roots))
                if len(pending) >= window:
                    check_cancel()
                    result = pending.popleft().result()
                    consume(result)
                    last_key = (result.group.source, result.group.filename)
                    now = time.monotonic()
                    if now - last_progress_at >= PROGRESS_INTERVAL_SECONDS:
                        emit(packing_progress())
                        last_progress_at = now
                    if now - last_checkpoint_at >= CHECKPOINT_INTERVAL_SECONDS:
                        write_checkpoint("packing")
                        last_checkpoint_at = now
            while pending:
                check_cancel()
                result = pending.popleft().result()
                consume(result)
                last_key = (result.group.source, result.group.filename)
                now = time.monotonic()
                if now - last_progress_at >= PROGRESS_INTERVAL_SECONDS:
                    emit(packing_progress())
                    last_progress_at = now
                if now - last_checkpoint_at >= CHECKPOINT_INTERVAL_SECONDS:
                    write_checkpoint("packing")
                    last_checkpoint_at = now
        finally:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)

        check_cancel()
        final_db_stat = db_path.stat()
        if (
            final_db_stat.st_size != db_stat.st_size
            or final_db_stat.st_mtime_ns != db_stat.st_mtime_ns
        ):
            raise RuntimeError("entries.db changed during audio pack build")
        pack_file.seek(0, os.SEEK_END)
        pack_size = pack_file.tell()
        INDEX_HEADER.pack_into(
            index_map,
            0,
            INDEX_MAGIC,
            1,
            RECORD.size,
            maximum_id,
            counters["valid_rows"],
            pack_size,
            db_stat.st_size,
            db_stat.st_mtime_ns,
        )
        computed_version = version_hash.hexdigest()[:16]
        if final_version is not None and final_version != computed_version:
            raise ValueError("checkpoint final version does not match its contents")
        final_version = computed_version
        write_checkpoint("finalizing", final_version)
        emit(
            PackProgress(
                "packing",
                total_rows,
                total_rows,
                f"Packed {counters['unique_files']:,} unique audio blobs; verifying output...",
                resumed,
            )
        )
    except PackBuildCancelled:
        if (
            checkpoint_writable
            and index_map is not None
            and index_file is not None
            and pack_file is not None
        ):
            write_checkpoint("packing", final_version)
        raise
    except BaseException:
        if index_map is not None:
            index_map.close()
            index_map = None
        if index_file is not None:
            index_file.close()
            index_file = None
        if pack_file is not None:
            pack_file.close()
            pack_file = None
        connection.close()
        raise
    finally:
        if index_map is not None:
            index_map.close()
        if index_file is not None:
            index_file.close()
        if pack_file is not None:
            pack_file.close()
        connection.close()

    assert final_version is not None
    try:
        index_sha256, _ = _sha256_file_stable(
            index_path,
            progress_callback=emit,
            should_cancel=should_cancel,
            stage="verifying-index",
            resumed=resumed,
        )
        pack_sha256, _ = _sha256_file_stable(
            pack_path,
            progress_callback=emit,
            should_cancel=should_cancel,
            stage="verifying-audio",
            resumed=resumed,
        )
        verified_source_state = hashlib.sha256()
        verified_rows = 0
        last_source_progress_at = 0.0
        verification_connection = sqlite3.connect(uri, uri=True)
        try:
            verification_connection.execute("PRAGMA query_only=ON")
            for group in _group_rows(verification_connection):
                if should_cancel is not None and should_cancel():
                    raise PackBuildCancelled(
                        PackProgress(
                            "verifying-sources",
                            verified_rows,
                            total_rows,
                            "Paused while checking source audio; the checkpoint was kept.",
                            resumed,
                        )
                    )
                _path, _mime_id, source_state = _source_state_token(
                    group, source_roots
                )
                verified_source_state.update(source_state)
                verified_rows += len(group.row_ids)
                now = time.monotonic()
                if now - last_source_progress_at >= PROGRESS_INTERVAL_SECONDS:
                    emit(
                        PackProgress(
                            "verifying-sources",
                            verified_rows,
                            total_rows,
                            "Checking that source audio stayed unchanged: "
                            f"{verified_rows:,}/{total_rows:,} rows",
                            resumed,
                        )
                    )
                    last_source_progress_at = now
        finally:
            verification_connection.close()
        if verified_source_state.digest() != source_state_hash.digest():
            raise RuntimeError("source audio changed during audio pack build")
        final_db_stat = db_path.stat()
        if (
            final_db_stat.st_size != db_stat.st_size
            or final_db_stat.st_mtime_ns != db_stat.st_mtime_ns
        ):
            raise RuntimeError("entries.db changed before audio pack publication")
        if should_cancel is not None and should_cancel():
            raise PackBuildCancelled(
                PackProgress(
                    "ready",
                    1,
                    1,
                    "Pack build paused safely before activation.",
                    resumed,
                )
            )

        version = final_version
        version_root = versions_root / version
        if version_root.exists():
            existing_index = version_root / "audio.idx"
            existing_pack = version_root / "audio.pack"
            existing_index_sha256 = None
            existing_pack_sha256 = None
            if existing_index.is_file() and existing_pack.is_file():
                existing_index_sha256, _ = _sha256_file_stable(
                    existing_index,
                    progress_callback=emit,
                    should_cancel=should_cancel,
                    stage="verifying-existing-index",
                    resumed=resumed,
                )
                existing_pack_sha256, _ = _sha256_file_stable(
                    existing_pack,
                    progress_callback=emit,
                    should_cancel=should_cancel,
                    stage="verifying-existing-audio",
                    resumed=resumed,
                )
            if (
                existing_index_sha256 == index_sha256
                and existing_pack_sha256 == pack_sha256
            ):
                _write_version_manifest(
                    version_root,
                    version,
                    PACK_FORMAT_LAF,
                    index_sha256,
                    pack_sha256,
                )
                _remove_build_root(build_root, versions_root)
            else:
                version = hashlib.sha256(
                    version_hash.digest() + uuid.uuid4().bytes
                ).hexdigest()[:16]
                version_root = versions_root / version
                _write_version_manifest(
                    build_root,
                    version,
                    PACK_FORMAT_LAF,
                    index_sha256,
                    pack_sha256,
                )
                os.replace(build_root, version_root)
        else:
            _write_version_manifest(
                build_root,
                version,
                PACK_FORMAT_LAF,
                index_sha256,
                pack_sha256,
            )
            os.replace(build_root, version_root)
        try:
            (version_root / CHECKPOINT_NAME).unlink(missing_ok=True)
        except OSError:
            pass
        opened = AudioPack.open_version(pack_root, version)
        if opened is None:
            raise ValueError("published packed version failed structural validation")
        opened.close()
    except PackBuildCancelled:
        raise
    except BaseException:
        raise

    manifest = {
        "format": FORMAT_NAME,
        "pack_format": PACK_FORMAT_LAF,
        "version": version,
        "index": f"versions/{version}/audio.idx",
        "pack": f"versions/{version}/audio.pack",
        "database": {
            "size": db_stat.st_size,
            "mtime_ns": db_stat.st_mtime_ns,
            "maximum_id": maximum_id,
            "rows": total_rows,
        },
        "valid_rows": counters["valid_rows"],
        "mapping_rows_reusing_source_paths": (
            counters["valid_rows"] - counters["readable_source_paths"]
        ),
        "source_path_references": counters["processed_source_paths"],
        "readable_source_paths": counters["readable_source_paths"],
        "unique_files": counters["unique_files"],
        "content_duplicates": counters["content_duplicates"],
        "deduplicated_bytes_saved": counters["deduplicated_bytes_saved"],
        "missing_files": counters["missing_files"],
        "pack_bytes": pack_size,
        "resumed": resumed,
        "integrity": {
            "index_sha256": index_sha256,
            "pack_sha256": pack_sha256,
        },
    }
    _atomic_json_file(pack_root / "active.json", manifest)
    manifest["elapsed_seconds"] = time.perf_counter() - started
    emit(PackProgress("complete", 1, 1, "Fast audio pack is ready.", resumed))
    return manifest
