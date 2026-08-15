from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import stat
import time
import uuid

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterable, Optional

from .fast_pack import (
    AudioPack,
    BUILD_LOCK_NAME,
    FORMAT_NAME,
    HASH_RE,
    LOOSE_AUDIO_QUARANTINE_NAME,
    MIME_ID_BY_SUFFIX,
    PackBuildCancelled,
    PackProgress,
    VERSION_MANIFEST_NAME,
    _PackBuildLock,
    _sha256_file_stable,
)


PACKED_ONLY_FORMAT = "local-audio-fast-packed-only-v1"
PACKED_ONLY_MARKER_NAME = "packed-only-v1.json"
INVENTORY_FORMAT = "local-audio-fast-cleanup-inventory-v1"
INVENTORY_NAME = "packed-only-inventory-v1.sqlite3"
ACTIVE_CLEANUP_STATUSES = {
    "staging",
    "paused",
    "quarantined",
    "trash-failed",
    "recovery-required",
    "completed",
}
TRASH_BATCH_SIZE = 256
STAGING_DIR_NAME = LOOSE_AUDIO_QUARANTINE_NAME
PROGRESS_INTERVAL_SECONDS = 0.1
CHUNK_BYTES = 1024 * 1024


class CleanupSafetyError(RuntimeError):
    """The loose originals cannot be removed without risking audio loss."""


class PackedOnlyStateError(CleanupSafetyError):
    """A packed-only marker exists but cannot be trusted."""


class LooseAudioChangedError(CleanupSafetyError):
    """A loose or restored audio file changed while it was being verified.

    Raised only when a file's identity/size shifts mid-hash, which usually means
    another program, sync service, or copy is touching the folder. The presentation
    layer uses this type to reassure the user that nothing was deleted and to point
    at the folder-stability cause rather than at an internal invariant.
    """


class PackMismatchError(CleanupSafetyError):
    """Loose or staged audio no longer matches the verified pack byte-for-byte.

    The presentation layer uses this type to explain that the original was kept and
    that the fast pack should be rebuilt from the current files before retrying.
    """


@dataclass(frozen=True)
class _AudioGroup:
    source: str
    filename: str
    row_ids: tuple[int, ...]


@dataclass(frozen=True)
class _FileState:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _marker_path(pack_root: Path) -> Path:
    return pack_root / PACKED_ONLY_MARKER_NAME


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _fsync_directory(path: Path) -> None:
    """Make atomic marker/directory renames durable where the platform supports it."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        # Windows does not expose a portable directory fsync through os.open.
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _durable_mkdirs(base: Path, directory: Path) -> None:
    """Create a contained directory chain, syncing each new parent link."""

    relative = directory.relative_to(base)
    current = base
    for part in relative.parts:
        child = current / part
        if child.exists():
            if child.is_symlink() or not child.is_dir():
                raise CleanupSafetyError(f"staging directory is unsafe: {child}")
        else:
            child.mkdir()
            _fsync_directory(current)
        current = child


def load_packed_only_state(pack_root: Path) -> Optional[dict]:
    """Return the durable packed-only state used to guard future maintenance."""

    path = _marker_path(pack_root)
    if not os.path.lexists(path):
        return None
    try:
        marker_state = os.lstat(path)
        if _is_reparse(marker_state) or not stat.S_ISREG(marker_state.st_mode):
            raise PackedOnlyStateError(
                "the packed-only safety marker is not a regular managed file; "
                "maintenance remains blocked"
            )
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise PackedOnlyStateError(
            "the packed-only safety marker is unreadable or corrupt; maintenance "
            "is blocked until it is repaired"
        ) from error
    if (
        not isinstance(value, dict)
        or value.get("format") != PACKED_ONLY_FORMAT
        or value.get("status") not in ACTIVE_CLEANUP_STATUSES
        or not isinstance(value.get("version"), str)
    ):
        raise PackedOnlyStateError(
            "the packed-only safety marker is invalid; maintenance is blocked "
            "until it is repaired"
        )
    return value


def clear_packed_only_state(pack_root: Path, *, replacement_verified: bool = False) -> None:
    """Clear a packed-only guard after a complete replacement collection succeeds."""

    if not replacement_verified:
        raise CleanupSafetyError(
            "packed-only state can be cleared only by a verified complete replacement"
        )
    try:
        _marker_path(pack_root).unlink(missing_ok=True)
        _fsync_directory(pack_root)
    except OSError as error:
        raise CleanupSafetyError(
            "the old packed-only safety marker could not be cleared"
        ) from error


def packed_only_marker_exists(pack_root: Path) -> bool:
    return os.path.lexists(_marker_path(pack_root))


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve(strict=True).as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _groups(connection: sqlite3.Connection) -> Iterable[_AudioGroup]:
    cursor = connection.execute(
        "SELECT id,source,file FROM entries ORDER BY source,file,id"
    )
    current_key: Optional[tuple[str, str]] = None
    row_ids: list[int] = []
    for row_id, source, filename in cursor:
        if not isinstance(source, str) or not isinstance(filename, str):
            raise CleanupSafetyError("entries.db contains a non-text source or file path")
        key = source, filename
        if current_key is not None and key != current_key:
            yield _AudioGroup(current_key[0], current_key[1], tuple(row_ids))
            row_ids.clear()
        current_key = key
        row_ids.append(int(row_id))
    if current_key is not None:
        yield _AudioGroup(current_key[0], current_key[1], tuple(row_ids))


def _state(path: Path) -> _FileState:
    value = os.stat(path, follow_symlinks=False)
    return _FileState(
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _same_moved_file(before: _FileState, after: _FileState) -> bool:
    # Renaming changes ctime on POSIX; content-bearing and inode fields must stay.
    return (
        before.device,
        before.inode,
        before.mode,
        before.size,
        before.mtime_ns,
    ) == (
        after.device,
        after.inode,
        after.mode,
        after.size,
        after.mtime_ns,
    )


def _is_reparse(value: os.stat_result) -> bool:
    if stat.S_ISLNK(value.st_mode):
        return True
    # Windows directory junctions are not consistently reported as symlinks.
    return bool(getattr(value, "st_file_attributes", 0) & 0x400)


def _reject_reparse_components(
    base: Path, path: Path, *, allow_missing: bool = False
) -> None:
    try:
        relative = path.relative_to(base)
    except ValueError as error:
        raise CleanupSafetyError(f"path is outside the managed root: {path}") from error
    current = base
    paths = (
        base,
        *(
            base.joinpath(*relative.parts[:index])
            for index in range(1, len(relative.parts) + 1)
        ),
    )
    for current in paths:
        try:
            value = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                return
            raise CleanupSafetyError(f"required path is missing: {current}")
        except OSError as error:
            raise CleanupSafetyError(f"could not inspect path safely: {current}") from error
        if _is_reparse(value):
            raise CleanupSafetyError(
                f"cleanup refuses symbolic links or directory junctions: {current}"
            )


def _owned_source_roots(
    source_ids: set[str],
    sources: dict,
    program_root: Path,
    pack_root: Path,
) -> dict[str, Path]:
    program_lexical = program_root.absolute()
    user_lexical = (program_lexical / "user_files").absolute()
    _reject_reparse_components(program_lexical, user_lexical)
    user_files = user_lexical.resolve(strict=True)
    pack_resolved = pack_root.resolve()
    roots: dict[str, Path] = {}
    for source_id in sorted(source_ids):
        source = sources.get(source_id)
        if source is None:
            raise CleanupSafetyError(
                f"entries.db references an unconfigured source: {source_id}"
            )
        root_lexical = source.get_media_dir_path().absolute()
        if ".." in root_lexical.parts:
            raise CleanupSafetyError(
                f"source {source_id!r} uses an unsafe parent path"
            )
        try:
            root_lexical.relative_to(user_lexical)
        except ValueError as error:
            raise CleanupSafetyError(
                f"source {source_id!r} is outside this add-on's user_files; "
                "external or shared folders are never removed"
            ) from error
        _reject_reparse_components(user_lexical, root_lexical)
        root = root_lexical.resolve(strict=True)
        try:
            root.relative_to(user_files)
        except ValueError as error:
            raise CleanupSafetyError(
                f"source {source_id!r} escapes this add-on's user_files"
            ) from error
        if (
            root == user_files
            or root == pack_resolved
            or pack_resolved in root.parents
            or root in pack_resolved.parents
        ):
            raise CleanupSafetyError(
                f"source {source_id!r} overlaps protected add-on data"
            )
        if not root.is_dir():
            raise CleanupSafetyError(f"source folder is missing: {root}")
        roots[source_id] = root
    return roots


def _owned_marker_roots(
    source_roots: dict, program_root: Path, pack_root: Path
) -> dict[str, Path]:
    """Revalidate journaled roots without trusting mutable current config."""

    program_lexical = program_root.absolute()
    user_lexical = (program_lexical / "user_files").absolute()
    _reject_reparse_components(program_lexical, user_lexical)
    user_files = user_lexical.resolve(strict=True)
    pack_resolved = pack_root.resolve()
    roots: dict[str, Path] = {}
    for source_id, raw_path in sorted(source_roots.items()):
        if not isinstance(source_id, str) or not isinstance(raw_path, str):
            raise CleanupSafetyError("cleanup journal contains an invalid source root")
        root_lexical = Path(raw_path)
        if not root_lexical.is_absolute() or ".." in root_lexical.parts:
            raise CleanupSafetyError(
                f"cleanup journal contains an unsafe source root: {raw_path!r}"
            )
        try:
            root_lexical.relative_to(user_lexical)
        except ValueError as error:
            raise CleanupSafetyError(
                f"journaled source {source_id!r} is outside this add-on's user_files"
            ) from error
        _reject_reparse_components(user_lexical, root_lexical)
        root = root_lexical.resolve(strict=True)
        if (
            root == user_files
            or root == pack_resolved
            or pack_resolved in root.parents
            or root in pack_resolved.parents
        ):
            raise CleanupSafetyError(
                f"journaled source {source_id!r} overlaps protected add-on data"
            )
        roots[source_id] = root
    return roots


def _managed_pack_root(program_root: Path, pack_root: Path) -> Path:
    program_lexical = program_root.absolute()
    user_lexical = (program_lexical / "user_files").absolute()
    pack_lexical = pack_root.absolute()
    try:
        pack_lexical.relative_to(user_lexical)
    except ValueError as error:
        raise CleanupSafetyError("fast_audio is outside this add-on's user_files") from error
    _reject_reparse_components(
        user_lexical, pack_lexical, allow_missing=not pack_lexical.exists()
    )
    resolved = pack_lexical.resolve()
    try:
        resolved.relative_to(user_lexical.resolve(strict=True))
    except ValueError as error:
        raise CleanupSafetyError("fast_audio escapes this add-on's user_files") from error
    return resolved


def _managed_database(program_root: Path, db_path: Path) -> Path:
    program_lexical = program_root.absolute()
    user_lexical = (program_lexical / "user_files").absolute()
    database_lexical = db_path.absolute()
    if database_lexical != user_lexical / "entries.db":
        raise CleanupSafetyError(
            "entries.db is not the managed database for this add-on"
        )
    _reject_reparse_components(user_lexical, database_lexical)
    value = os.stat(database_lexical, follow_symlinks=False)
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise CleanupSafetyError(
            "entries.db must be one private regular file inside this add-on"
        )
    return database_lexical.resolve(strict=True)


def _relative_audio_path(filename: str) -> tuple[str, ...]:
    normalized = filename.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(filename)
    if (
        not normalized
        or normalized.startswith("/")
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or "//" in normalized
        or any(part in ("", ".", "..") for part in posix.parts)
        or posix.suffix.lower() not in MIME_ID_BY_SUFFIX
    ):
        raise CleanupSafetyError(f"entries.db contains an unsafe audio path: {filename!r}")
    return posix.parts


def _safe_candidate(
    root: Path,
    filename: str,
    protected: tuple[Path, ...],
    *,
    allow_missing: bool,
) -> tuple[Path, Optional[_FileState]]:
    relative = _relative_audio_path(filename)
    candidate = root.joinpath(*relative)
    _reject_reparse_components(root, candidate, allow_missing=allow_missing)
    try:
        value = _state(candidate)
    except FileNotFoundError:
        if allow_missing:
            return candidate, None
        raise CleanupSafetyError(f"referenced audio is missing: {candidate}")
    except OSError as error:
        raise CleanupSafetyError(f"could not inspect referenced audio: {candidate}") from error
    if not stat.S_ISREG(value.mode):
        raise CleanupSafetyError(f"referenced audio is not a regular file: {candidate}")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise CleanupSafetyError(f"referenced audio escapes its source root: {candidate}") from error
    for protected_path in protected:
        try:
            if protected_path.exists() and os.path.samefile(candidate, protected_path):
                raise CleanupSafetyError(
                    f"referenced audio aliases protected add-on data: {candidate}"
                )
        except OSError as error:
            raise CleanupSafetyError(f"could not compare protected path: {candidate}") from error
    return resolved, value


def _read_json(path: Path, description: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise CleanupSafetyError(f"could not read {description}") from error
    if not isinstance(value, dict):
        raise CleanupSafetyError(f"{description} is invalid")
    return value


def _emit(
    callback: Optional[Callable[[PackProgress], None]], progress: PackProgress
) -> None:
    if callback is not None:
        callback(progress)


def _check_cancel(
    should_cancel: Optional[Callable[[], bool]], progress: PackProgress
) -> None:
    if should_cancel is not None and should_cancel():
        raise PackBuildCancelled(progress)


def _verified_active_pack(
    db_path: Path,
    pack_root: Path,
    progress_callback: Optional[Callable[[PackProgress], None]],
    should_cancel: Optional[Callable[[], bool]],
) -> tuple[AudioPack, dict, str]:
    active_path = pack_root / "active.json"
    active = _read_json(active_path, "the active pack manifest")
    if active.get("format") != FORMAT_NAME or active.get("missing_files") != 0:
        raise CleanupSafetyError("the active pack does not cover every source file")
    version = active.get("version")
    if not isinstance(version, str):
        raise CleanupSafetyError("the active pack version is invalid")
    pack = AudioPack.open_active(pack_root, db_path)
    if pack is None or pack.version != version:
        if pack is not None:
            pack.close()
        raise CleanupSafetyError("the active pack is missing or no longer matches entries.db")
    try:
        version_root = pack.index_path.parent.resolve(strict=True)
        managed_versions = (pack_root / "versions").resolve(strict=True)
        try:
            version_root.relative_to(managed_versions)
        except ValueError as error:
            raise CleanupSafetyError("the active pack version escapes managed storage") from error
        protected_files = (
            pack_root / "active.json",
            pack.index_path.parent / VERSION_MANIFEST_NAME,
            pack.index_path,
            pack.pack_path,
        )
        for path in protected_files:
            _reject_reparse_components(pack_root, path)
            value = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(value.st_mode):
                raise CleanupSafetyError(
                    f"active pack storage is not a regular managed file: {path}"
                )
            # Rust imports intentionally use hardlinks. A cleanup makes the pack
            # the sole serving copy, so externally mutable aliases are unsafe.
            if value.st_nlink != 1:
                raise CleanupSafetyError(
                    "active pack storage has external hardlink aliases; build a "
                    "private Python pack before removing loose originals"
                )
        version_manifest = _read_json(
            pack.index_path.parent / VERSION_MANIFEST_NAME,
            "the version pack manifest",
        )
        integrity = active.get("integrity")
        if not isinstance(integrity, dict):
            raise CleanupSafetyError("the active pack has no integrity record")
        expected_index = integrity.get("index_sha256")
        expected_pack = integrity.get("pack_sha256")
        if (
            not isinstance(expected_index, str)
            or HASH_RE.fullmatch(expected_index.lower()) is None
            or not isinstance(expected_pack, str)
            or HASH_RE.fullmatch(expected_pack.lower()) is None
            or version_manifest.get("index_sha256") != expected_index
            or version_manifest.get("pack_sha256") != expected_pack
        ):
            raise CleanupSafetyError("the active and version integrity records disagree")
        index_hash, _ = _sha256_file_stable(
            pack.index_path,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
            stage="cleanup-verifying-index",
        )
        if index_hash != expected_index:
            raise CleanupSafetyError("audio.idx failed its fresh SHA-256 check")
        pack_hash, _ = _sha256_file_stable(
            pack.pack_path,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
            stage="cleanup-verifying-pack",
        )
        if pack_hash != expected_pack:
            raise CleanupSafetyError("audio.pack failed its fresh SHA-256 check")
        db_hash, _ = _sha256_file_stable(
            db_path,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
            stage="cleanup-verifying-database",
        )
        return pack, active, db_hash
    except BaseException:
        pack.close()
        raise


def _protected_paths(
    db_path: Path, pack_root: Path, pack: AudioPack, program_root: Path
) -> tuple[Path, ...]:
    return (
        db_path.resolve(),
        (program_root / "user_files" / "config.json").resolve(),
        (program_root / "user_files" / "entries_version.txt").resolve(),
        (program_root / "user_files" / "jmdict_forms.json").resolve(),
        pack_root.resolve(),
        (pack_root / "active.json").resolve(),
        _marker_path(pack_root).resolve(),
        pack.index_path.resolve(),
        pack.pack_path.resolve(),
    )


def _scan_plan(
    db_path: Path,
    pack: AudioPack,
    roots: dict[str, Path],
    protected: tuple[Path, ...],
    allow_missing: bool,
    progress_callback: Optional[Callable[[PackProgress], None]],
    should_cancel: Optional[Callable[[], bool]],
) -> dict:
    seen: dict[str, tuple[str, str]] = {}
    files = 0
    present_bytes = 0
    missing = 0
    processed_rows = 0
    last_progress = 0.0
    with closing(_readonly_connection(db_path)) as connection:
        row = connection.execute(
            "SELECT COUNT(*),COALESCE(MAX(id),0) FROM entries"
        ).fetchone()
        total_rows, maximum_id = int(row[0]), int(row[1])
        if total_rows <= 0:
            raise CleanupSafetyError("entries.db contains no audio mappings")
        if pack.valid_rows != total_rows or pack.maximum_id != maximum_id:
            raise CleanupSafetyError(
                "the active pack index does not exactly cover the current database"
            )
        for group in _groups(connection):
            _check_cancel(
                should_cancel,
                PackProgress(
                    "cleanup-planning",
                    processed_rows,
                    total_rows,
                    "Cleanup cancelled before any originals were moved.",
                ),
            )
            first_audio = None
            for row_id in group.row_ids:
                audio = pack.get(row_id)
                if audio is None:
                    raise CleanupSafetyError(
                        f"pack record {row_id} is missing or invalid"
                    )
                if first_audio is None:
                    first_audio = audio
                elif (
                    audio.offset,
                    audio.length,
                    audio.mime_type,
                ) != (
                    first_audio.offset,
                    first_audio.length,
                    first_audio.mime_type,
                ):
                    raise CleanupSafetyError(
                        "database rows sharing one path have inconsistent pack records"
                    )
            root = roots.get(group.source)
            if root is None or first_audio is None:
                raise CleanupSafetyError(
                    f"pack references an unmanaged source: {group.source}"
                )
            candidate, state = _safe_candidate(
                root,
                group.filename,
                protected,
                allow_missing=allow_missing,
            )
            key = os.path.normcase(str(candidate))
            prior = seen.get(key)
            raw_identity = group.source, group.filename
            if prior is not None and prior != raw_identity:
                raise CleanupSafetyError(
                    "multiple entries.db paths resolve to the same loose audio "
                    f"file: {candidate}; keep originals and repair the ambiguous "
                    "mappings before cleanup"
                )
            if prior is None:
                seen[key] = raw_identity
                files += 1
                if state is None:
                    missing += 1
                else:
                    if state.size != first_audio.length:
                        raise PackMismatchError(
                            f"loose audio size no longer matches the pack: {candidate}"
                        )
                    present_bytes += state.size
            processed_rows += len(group.row_ids)
            now = time.monotonic()
            if now - last_progress >= PROGRESS_INTERVAL_SECONDS:
                _emit(
                    progress_callback,
                    PackProgress(
                        "cleanup-planning",
                        processed_rows,
                        total_rows,
                        "Checking full pack coverage and safe file paths: "
                        f"{processed_rows:,}/{total_rows:,} rows",
                    ),
                )
                last_progress = now
    _emit(
        progress_callback,
        PackProgress(
            "cleanup-planning",
            total_rows,
            total_rows,
            f"Verified {files:,} managed loose audio files.",
        ),
    )
    return {
        "rows": total_rows,
        "files": files,
        "present_bytes": present_bytes,
        "missing": missing,
    }


def _compare_with_pack(
    path: Path,
    expected_state: _FileState,
    pack: AudioPack,
    audio,
    should_cancel: Optional[Callable[[], bool]],
    current: int,
    total: int,
) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CleanupSafetyError(f"could not open loose audio safely: {path}") from error
    try:
        opened_state_value = os.fstat(descriptor)
        opened_state = _FileState(
            opened_state_value.st_dev,
            opened_state_value.st_ino,
            opened_state_value.st_mode,
            opened_state_value.st_size,
            opened_state_value.st_mtime_ns,
            opened_state_value.st_ctime_ns,
        )
        if opened_state != expected_state or not stat.S_ISREG(opened_state.mode):
            raise LooseAudioChangedError(f"loose audio changed during verification: {path}")
        position = 0
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as file:
            while position < audio.length:
                _check_cancel(
                    should_cancel,
                    PackProgress(
                        "cleanup-comparing-audio",
                        current,
                        total,
                        "Cleanup paused safely; run the cleanup action again to continue.",
                    ),
                )
                length = min(CHUNK_BYTES, audio.length - position)
                loose = file.read(length)
                digest.update(loose)
                packed = pack.view(audio, position, length)
                try:
                    if len(loose) != length or loose != packed.tobytes():
                        raise PackMismatchError(
                            f"loose audio bytes no longer match the verified pack: {path}"
                        )
                finally:
                    packed.release()
                position += length
        final_value = os.fstat(descriptor)
        final_state = _FileState(
            final_value.st_dev,
            final_value.st_ino,
            final_value.st_mode,
            final_value.st_size,
            final_value.st_mtime_ns,
            final_value.st_ctime_ns,
        )
        if final_state != expected_state:
            raise LooseAudioChangedError(f"loose audio changed during verification: {path}")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _same_collection(marker: dict, version: str, db_hash: str, roots: dict[str, Path]) -> bool:
    database = marker.get("database")
    return (
        marker.get("version") == version
        and isinstance(database, dict)
        and database.get("sha256") == db_hash
        and marker.get("source_roots")
        == {key: str(value) for key, value in sorted(roots.items())}
    )


def _active_version(pack_root: Path) -> Optional[str]:
    try:
        value = json.loads((pack_root / "active.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value.get("version") if isinstance(value, dict) else None


def _source_slots(roots: dict[str, Path]) -> dict[str, str]:
    return {
        source_id: f"source-{index:04d}-{hashlib.sha256(source_id.encode()).hexdigest()[:8]}"
        for index, source_id in enumerate(sorted(roots))
    }


def _stage_root(pack_root: Path) -> Path:
    return pack_root / STAGING_DIR_NAME


def _inventory_path(pack_root: Path) -> Path:
    return pack_root / INVENTORY_NAME


def _remove_empty_stage_tree(stage_root: Path) -> bool:
    """Remove only an empty generated hierarchy; never remove a file."""

    if not os.path.lexists(stage_root):
        return True
    if stage_root.is_symlink() or not stage_root.is_dir():
        return False
    try:
        directories = sorted(
            (path for path in stage_root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            directory.rmdir()
        stage_root.rmdir()
    except OSError:
        return False
    _fsync_directory(stage_root.parent)
    return True


def _stage_candidate(
    stage_root: Path, slot: str, filename: str, *, allow_missing: bool
) -> Path:
    relative = _relative_audio_path(filename)
    candidate = stage_root / slot / Path(*relative)
    _reject_reparse_components(stage_root, candidate, allow_missing=allow_missing)
    return candidate


def _update_marker(
    pack_root: Path,
    marker: dict,
    status: str,
    moved_files: int,
    moved_bytes: int,
    detail: Optional[str] = None,
) -> None:
    marker["status"] = status
    marker["moved_files"] = moved_files
    marker["moved_bytes"] = moved_bytes
    marker["updated_at_ns"] = time.time_ns()
    if detail:
        marker["detail"] = detail
    else:
        marker.pop("detail", None)
    _atomic_json(_marker_path(pack_root), marker)


def _reverify_bound_pack(
    db_path: Path,
    pack_root: Path,
    marker: dict,
    progress_callback: Optional[Callable[[PackProgress], None]],
    should_cancel: Optional[Callable[[], bool]],
) -> None:
    verified, active, db_hash = _verified_active_pack(
        db_path, pack_root, progress_callback, should_cancel
    )
    try:
        if (
            verified.version != marker["version"]
            or db_hash != marker["database"]["sha256"]
            or active.get("integrity") != marker.get("integrity")
        ):
            raise CleanupSafetyError(
                "the database or active pack changed during cleanup"
            )
    finally:
        verified.close()


def _create_cleanup_inventory(
    db_path: Path,
    pack_root: Path,
    pack: AudioPack,
    roots: dict[str, Path],
    protected: tuple[Path, ...],
    progress_callback: Optional[Callable[[PackProgress], None]],
    should_cancel: Optional[Callable[[], bool]],
) -> dict:
    """Persist an independent restore map before any loose file is moved."""

    destination = _inventory_path(pack_root)
    temporary = destination.with_name(destination.name + f".{uuid.uuid4().hex}.tmp")
    files = 0
    total_bytes = 0
    processed_rows = 0
    last_progress = 0.0
    seen: dict[str, tuple[str, str]] = {}
    try:
        with closing(sqlite3.connect(temporary)) as inventory, closing(
            _readonly_connection(db_path)
        ) as database:
            inventory.execute("PRAGMA journal_mode=OFF")
            inventory.execute("PRAGMA synchronous=OFF")
            inventory.execute(
                "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            inventory.execute(
                "CREATE TABLE files ("
                "source TEXT NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL, "
                "sha256 TEXT NOT NULL, PRIMARY KEY(source,filename)) WITHOUT ROWID"
            )
            total_rows = int(
                database.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            )
            inventory.execute(
                "INSERT INTO metadata VALUES ('format',?)", (INVENTORY_FORMAT,)
            )
            for group in _groups(database):
                _check_cancel(
                    should_cancel,
                    PackProgress(
                        "cleanup-inventory",
                        processed_rows,
                        total_rows,
                        "Cleanup cancelled before any originals were moved.",
                    ),
                )
                root = roots[group.source]
                candidate, state = _safe_candidate(
                    root, group.filename, protected, allow_missing=False
                )
                assert state is not None
                key = os.path.normcase(str(candidate))
                raw_identity = group.source, group.filename
                prior = seen.get(key)
                if prior is not None and prior != raw_identity:
                    raise CleanupSafetyError(
                        "multiple database paths resolve to the same loose audio; "
                        "cleanup requires unambiguous mappings"
                    )
                if prior is None:
                    seen[key] = raw_identity
                    first_audio = None
                    for row_id in group.row_ids:
                        audio = pack.get(row_id)
                        if audio is None:
                            raise CleanupSafetyError(
                                f"pack record {row_id} is missing or invalid"
                            )
                        if first_audio is None:
                            first_audio = audio
                        elif (
                            audio.offset,
                            audio.length,
                            audio.mime_type,
                        ) != (
                            first_audio.offset,
                            first_audio.length,
                            first_audio.mime_type,
                        ):
                            raise CleanupSafetyError(
                                "database rows sharing one path have inconsistent "
                                "pack records"
                            )
                    if first_audio is None or state.size != first_audio.length:
                        raise PackMismatchError(
                            f"loose audio no longer matches the pack: {candidate}"
                        )
                    digest = _compare_with_pack(
                        candidate,
                        state,
                        pack,
                        first_audio,
                        should_cancel,
                        processed_rows,
                        total_rows,
                    )
                    normalized = PurePosixPath(
                        *_relative_audio_path(group.filename)
                    ).as_posix()
                    inventory.execute(
                        "INSERT INTO files VALUES (?,?,?,?)",
                        (group.source, normalized, state.size, digest),
                    )
                    files += 1
                    total_bytes += state.size
                processed_rows += len(group.row_ids)
                now = time.monotonic()
                if now - last_progress >= PROGRESS_INTERVAL_SECONDS:
                    _emit(
                        progress_callback,
                        PackProgress(
                            "cleanup-inventory",
                            processed_rows,
                            total_rows,
                            "Building an independent recovery inventory: "
                            f"{processed_rows:,}/{total_rows:,} rows",
                        ),
                    )
                    last_progress = now
            inventory.executemany(
                "INSERT INTO metadata VALUES (?,?)",
                (
                    ("files", str(files)),
                    ("bytes", str(total_bytes)),
                    ("rows", str(total_rows)),
                ),
            )
            inventory.commit()
            if inventory.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise CleanupSafetyError("cleanup recovery inventory failed quick_check")
        with temporary.open("rb") as file:
            os.fsync(file.fileno())
        os.replace(temporary, destination)
        _fsync_directory(pack_root)
        inventory_hash, inventory_bytes = _sha256_file_stable(
            destination,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
            stage="cleanup-verifying-inventory",
        )
        _emit(
            progress_callback,
            PackProgress(
                "cleanup-inventory",
                processed_rows,
                processed_rows,
                f"Recorded {files:,} independently restorable loose files.",
            ),
        )
        return {
            "format": INVENTORY_FORMAT,
            "name": INVENTORY_NAME,
            "sha256": inventory_hash,
            "bytes_on_disk": inventory_bytes,
            "files": files,
            "audio_bytes": total_bytes,
            "rows": processed_rows,
        }
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _validated_inventory(pack_root: Path, marker: dict) -> Path:
    inventory = marker.get("inventory")
    if (
        not isinstance(inventory, dict)
        or inventory.get("format") != INVENTORY_FORMAT
        or inventory.get("name") != INVENTORY_NAME
        or not isinstance(inventory.get("sha256"), str)
        or HASH_RE.fullmatch(inventory["sha256"].lower()) is None
    ):
        raise CleanupSafetyError(
            "the cleanup journal has no valid independent recovery inventory"
        )
    path = _inventory_path(pack_root)
    _reject_reparse_components(pack_root, path)
    value = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise CleanupSafetyError(
            "the cleanup recovery inventory is not a private managed file"
        )
    actual_hash, _ = _sha256_file_stable(path)
    if actual_hash != inventory["sha256"]:
        raise CleanupSafetyError("the cleanup recovery inventory failed SHA-256")
    with closing(_readonly_connection(path)) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise CleanupSafetyError("the cleanup recovery inventory is corrupt")
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        row = connection.execute("SELECT COUNT(*),COALESCE(SUM(size),0) FROM files").fetchone()
    if (
        metadata.get("format") != INVENTORY_FORMAT
        or int(metadata.get("files", -1)) != int(row[0])
        or int(metadata.get("bytes", -1)) != int(row[1])
        or int(metadata.get("rows", -1)) != int(marker.get("total_rows", -2))
        or int(row[0]) != int(inventory.get("files", -1))
        or int(row[1]) != int(inventory.get("audio_bytes", -1))
        or int(row[0]) != int(marker.get("total_files", -2))
        or int(row[1]) != int(marker.get("total_bytes", -2))
    ):
        raise CleanupSafetyError(
            "the cleanup recovery inventory totals do not match the journal"
        )
    return path


def _hash_regular_file(
    path: Path,
    expected_size: int,
    should_cancel: Optional[Callable[[], bool]],
    current: int,
    total: int,
) -> str:
    before = _state(path)
    if not stat.S_ISREG(before.mode) or before.size != expected_size:
        raise CleanupSafetyError(f"restored audio has an unexpected type or size: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CleanupSafetyError(f"could not open restored audio safely: {path}") from error
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.device,
            before.inode,
            before.size,
        ):
            raise CleanupSafetyError(f"restored audio changed before verification: {path}")
        while chunk := os.read(descriptor, CHUNK_BYTES):
            _check_cancel(
                should_cancel,
                PackProgress(
                    "cleanup-verifying-restored",
                    current,
                    total,
                    "Restore paused safely during independent file verification.",
                ),
            )
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            before.device,
            before.inode,
            before.size,
            before.mtime_ns,
        ):
            raise LooseAudioChangedError(f"restored audio changed during verification: {path}")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _stage_verified_files(
    db_path: Path,
    pack_root: Path,
    pack: AudioPack,
    roots: dict[str, Path],
    protected: tuple[Path, ...],
    marker: dict,
    progress_callback: Optional[Callable[[PackProgress], None]],
    should_cancel: Optional[Callable[[], bool]],
    checkpoint_size: int,
) -> tuple[int, int]:
    stage_root = _stage_root(pack_root)
    if stage_root.is_symlink() or (stage_root.exists() and not stage_root.is_dir()):
        raise CleanupSafetyError("the cleanup staging path is not a safe directory")
    _durable_mkdirs(pack_root, stage_root)
    _reject_reparse_components(pack_root, stage_root)
    slots = marker["source_slots"]
    moved_files = 0
    moved_bytes = 0
    since_checkpoint = 0
    last_progress = 0.0
    seen: set[str] = set()

    try:
        with closing(_readonly_connection(db_path)) as connection:
            for group in _groups(connection):
                _check_cancel(
                    should_cancel,
                    PackProgress(
                        "cleanup-staging",
                        moved_files,
                        int(marker["total_files"]),
                        "Cleanup paused safely; verified files remain in protected "
                        "staging and can be resumed.",
                    ),
                )
                root = roots[group.source]
                original, original_state = _safe_candidate(
                    root,
                    group.filename,
                    protected,
                    allow_missing=True,
                )
                key = os.path.normcase(str(original))
                if key in seen:
                    continue
                seen.add(key)
                staged = _stage_candidate(
                    stage_root,
                    slots[group.source],
                    group.filename,
                    allow_missing=True,
                )
                staged_exists = os.path.lexists(staged)
                if original_state is None:
                    if not staged_exists:
                        raise CleanupSafetyError(
                            f"audio is absent from both its source and protected staging: {original}"
                        )
                    staged_state = _state(staged)
                    if not stat.S_ISREG(staged_state.mode):
                        raise CleanupSafetyError(
                            f"protected staged audio is not a regular file: {staged}"
                        )
                    audio = pack.get(group.row_ids[0])
                    if audio is None or staged_state.size != audio.length:
                        raise PackMismatchError(
                            f"protected staged audio no longer matches the pack: {staged}"
                        )
                    _compare_with_pack(
                        staged,
                        staged_state,
                        pack,
                        audio,
                        should_cancel,
                        moved_files,
                        int(marker["total_files"]),
                    )
                    moved_files += 1
                    moved_bytes += staged_state.size
                    now = time.monotonic()
                    if now - last_progress >= PROGRESS_INTERVAL_SECONDS:
                        _emit(
                            progress_callback,
                            PackProgress(
                                "cleanup-staging",
                                moved_files,
                                int(marker["total_files"]),
                                f"Rechecked {moved_files:,}/"
                                f"{int(marker['total_files']):,} files already in "
                                "protected quarantine.",
                            ),
                        )
                        last_progress = now
                    continue
                if staged_exists:
                    raise CleanupSafetyError(
                        f"audio exists in both its source and protected staging: {original}"
                    )
                audio = pack.get(group.row_ids[0])
                if audio is None or original_state.size != audio.length:
                    raise PackMismatchError(
                        f"pack no longer matches loose audio: {original}"
                    )
                _compare_with_pack(
                    original,
                    original_state,
                    pack,
                    audio,
                    should_cancel,
                    moved_files,
                    int(marker["total_files"]),
                )
                _durable_mkdirs(stage_root, staged.parent)
                _reject_reparse_components(stage_root, staged.parent)
                # Atomic same-volume quarantine happens before the external Trash
                # API sees any path. A post-rename inode/state check detects a
                # parent swap and allows immediate rollback instead of deleting it.
                try:
                    os.replace(original, staged)
                except OSError as error:
                    raise CleanupSafetyError(
                        f"could not quarantine verified audio safely: {original}"
                    ) from error
                try:
                    if not _same_moved_file(original_state, _state(staged)):
                        try:
                            os.replace(staged, original)
                        except OSError:
                            pass
                        raise CleanupSafetyError(
                            f"audio path changed during atomic quarantine: {original}"
                        )
                except FileNotFoundError as error:
                    raise CleanupSafetyError(
                        f"quarantined audio disappeared unexpectedly: {staged}"
                    ) from error
                _fsync_directory(staged.parent)
                _fsync_directory(original.parent)
                moved_files += 1
                moved_bytes += original_state.size
                since_checkpoint += 1
                if since_checkpoint >= checkpoint_size:
                    _update_marker(
                        pack_root,
                        marker,
                        "staging",
                        moved_files,
                        moved_bytes,
                    )
                    since_checkpoint = 0
                now = time.monotonic()
                if now - last_progress >= PROGRESS_INTERVAL_SECONDS:
                    _emit(
                        progress_callback,
                        PackProgress(
                            "cleanup-staging",
                            moved_files,
                            int(marker["total_files"]),
                            f"Secured {moved_files:,}/{int(marker['total_files']):,} "
                            "verified files for one recoverable Trash operation.",
                        ),
                    )
                    last_progress = now
    except PackBuildCancelled as error:
        staged_files_exist = stage_root.is_dir() and any(
            path.is_file() for path in stage_root.rglob("*")
        )
        if moved_files == 0 and not staged_files_exist and int(
            marker.get("moved_files", 0)
        ) == 0:
            if _remove_empty_stage_tree(stage_root):
                _marker_path(pack_root).unlink(missing_ok=True)
                _fsync_directory(pack_root)
            else:
                _update_marker(
                    pack_root,
                    marker,
                    "paused",
                    0,
                    0,
                    "Cleanup paused with an empty protected staging hierarchy; "
                    "retry is safe.",
                )
        else:
            moved_files = max(moved_files, int(marker.get("moved_files", 0)))
            moved_bytes = max(moved_bytes, int(marker.get("moved_bytes", 0)))
            _update_marker(
                pack_root,
                marker,
                "paused",
                moved_files,
                moved_bytes,
                str(error),
            )
        raise
    _update_marker(
        pack_root,
        marker,
        "quarantined",
        moved_files,
        moved_bytes,
    )
    _emit(
        progress_callback,
        PackProgress(
            "cleanup-staging",
            moved_files,
            int(marker["total_files"]),
            f"Secured all {moved_files:,} verified files in protected quarantine.",
        ),
    )
    return moved_files, moved_bytes


def _verify_stage_inventory(
    db_path: Path,
    stage_root: Path,
    pack: AudioPack,
    roots: dict[str, Path],
    marker: dict,
    progress_callback: Optional[Callable[[PackProgress], None]],
    should_cancel: Optional[Callable[[], bool]],
) -> _FileState:
    """Require quarantine to contain exactly the verified DB-referenced files."""

    root_state = _state(stage_root)
    if not stat.S_ISDIR(root_state.mode):
        raise CleanupSafetyError("the protected cleanup staging path is not a directory")
    expected_files: set[str] = set()
    expected_directories: set[str] = {"."}
    seen_originals: set[str] = set()
    verified_files = 0
    verified_bytes = 0
    last_progress = 0.0
    slots = marker["source_slots"]
    with closing(_readonly_connection(db_path)) as connection:
        for group in _groups(connection):
            relative_parts = _relative_audio_path(group.filename)
            original = roots[group.source].joinpath(*relative_parts)
            original_key = os.path.normcase(str(original))
            if original_key in seen_originals:
                continue
            seen_originals.add(original_key)
            staged = _stage_candidate(
                stage_root,
                slots[group.source],
                group.filename,
                allow_missing=False,
            )
            relative = staged.relative_to(stage_root)
            relative_key = relative.as_posix()
            if relative_key in expected_files:
                raise CleanupSafetyError(
                    f"two source files map to one quarantine path: {staged}"
                )
            expected_files.add(relative_key)
            parent = relative.parent
            while parent != Path("."):
                expected_directories.add(parent.as_posix())
                parent = parent.parent
            expected_directories.add(".")
            staged_state = _state(staged)
            if not stat.S_ISREG(staged_state.mode):
                raise CleanupSafetyError(
                    f"protected staged audio is not a regular file: {staged}"
                )
            audio = pack.get(group.row_ids[0])
            if audio is None or staged_state.size != audio.length:
                raise PackMismatchError(
                    f"protected staged audio no longer matches the pack: {staged}"
                )
            _compare_with_pack(
                staged,
                staged_state,
                pack,
                audio,
                should_cancel,
                verified_files,
                int(marker["total_files"]),
            )
            verified_files += 1
            verified_bytes += staged_state.size
            now = time.monotonic()
            if now - last_progress >= PROGRESS_INTERVAL_SECONDS:
                _emit(
                    progress_callback,
                    PackProgress(
                        "cleanup-verifying-quarantine",
                        verified_files,
                        int(marker["total_files"]),
                        f"Reverified {verified_files:,} quarantined audio files.",
                    ),
                )
                last_progress = now

    actual_files: set[str] = set()
    actual_directories: set[str] = {"."}
    for current, directory_names, file_names in os.walk(
        stage_root, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        _reject_reparse_components(stage_root, current_path)
        for name in directory_names:
            directory = current_path / name
            value = os.lstat(directory)
            if _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
                raise CleanupSafetyError(
                    f"unexpected link or non-directory in quarantine: {directory}"
                )
            actual_directories.add(directory.relative_to(stage_root).as_posix())
        for name in file_names:
            path = current_path / name
            value = os.lstat(path)
            if _is_reparse(value) or not stat.S_ISREG(value.st_mode):
                raise CleanupSafetyError(
                    f"unexpected link or non-file in quarantine: {path}"
                )
            actual_files.add(path.relative_to(stage_root).as_posix())

    if actual_files != expected_files or actual_directories != expected_directories:
        raise CleanupSafetyError(
            "protected quarantine contains an unexpected or missing path; nothing "
            "was sent to Trash"
        )
    if (
        verified_files != int(marker["total_files"])
        or verified_bytes != int(marker["total_bytes"])
    ):
        raise CleanupSafetyError(
            "protected quarantine totals do not match the durable cleanup journal"
        )
    _emit(
        progress_callback,
        PackProgress(
            "cleanup-verifying-quarantine",
            verified_files,
            int(marker["total_files"]),
            f"Reverified all {verified_files:,} quarantined audio files.",
        ),
    )
    final_root_state = _state(stage_root)
    if final_root_state != root_state:
        raise CleanupSafetyError(
            "protected quarantine changed while it was being verified"
        )
    return final_root_state


def _trash_quarantine(
    db_path: Path,
    pack_root: Path,
    pack: AudioPack,
    roots: dict[str, Path],
    marker: dict,
    trash: Callable[[list[str]], None],
    progress_callback: Optional[Callable[[PackProgress], None]],
    should_cancel: Optional[Callable[[], bool]],
) -> dict:
    stage_root = _stage_root(pack_root)
    total_files = int(marker["total_files"])
    total_bytes = int(marker["total_bytes"])
    _check_cancel(
        should_cancel,
        PackProgress(
            "cleanup-quarantined",
            total_files,
            total_files,
            "Cleanup paused with verified originals in protected staging.",
        ),
    )
    _reverify_bound_pack(
        db_path, pack_root, marker, progress_callback, should_cancel
    )
    if stage_root.is_symlink() or not stage_root.is_dir():
        raise CleanupSafetyError("the protected cleanup staging directory is missing")
    _reject_reparse_components(pack_root, stage_root)
    stage_state = _verify_stage_inventory(
        db_path,
        stage_root,
        pack,
        roots,
        marker,
        progress_callback,
        should_cancel,
    )
    _reject_reparse_components(pack_root, stage_root)
    if _state(stage_root) != stage_state:
        raise CleanupSafetyError(
            "protected quarantine changed immediately before the Trash request"
        )
    try:
        # The Trash integration receives one generated directory only. It never
        # receives source paths, preventing a late source-parent swap from
        # redirecting cleanup at unrelated files.
        trash([os.fspath(stage_root)])
    except BaseException as error:
        status = "trash-failed" if stage_root.exists() else "recovery-required"
        _update_marker(
            pack_root,
            marker,
            status,
            total_files,
            total_bytes,
            str(error),
        )
        raise CleanupSafetyError(
            "the operating system Trash operation did not complete cleanly; the "
            "journal was kept and no permanent-delete fallback was used"
        ) from error
    if os.path.lexists(stage_root):
        _update_marker(
            pack_root,
            marker,
            "trash-failed",
            total_files,
            total_bytes,
            "the protected staging directory remained after the Trash request",
        )
        raise CleanupSafetyError(
            "the operating system left protected staging in place; retry is safe"
        )
    try:
        _reverify_bound_pack(
            db_path, pack_root, marker, progress_callback, should_cancel=None
        )
    except BaseException as error:
        _update_marker(
            pack_root,
            marker,
            "recovery-required",
            total_files,
            total_bytes,
            str(error),
        )
        raise CleanupSafetyError(
            "the database or pack changed after originals reached Trash; restore "
            f"{STAGING_DIR_NAME} from Trash before continuing"
        ) from error
    marker["status"] = "completed"
    marker["moved_files"] = total_files
    marker["moved_bytes"] = total_bytes
    marker["completed_at_ns"] = time.time_ns()
    marker["updated_at_ns"] = marker["completed_at_ns"]
    marker.pop("detail", None)
    _atomic_json(_marker_path(pack_root), marker)
    _emit(
        progress_callback,
        PackProgress(
            "cleanup-complete",
            total_files,
            total_files,
            f"Moved {total_files:,} verified loose audio files to Trash.",
        ),
    )
    return {
        "version": marker["version"],
        "files": total_files,
        "bytes": total_bytes,
        "already_removed": 0,
        "status": "completed",
    }


def inspect_managed_cleanup(
    db_path: Path,
    pack_root: Path,
    sources: dict,
    program_root: Path,
) -> dict:
    """Cheap UI eligibility check; destructive work still revalidates everything."""

    try:
        pack_root = _managed_pack_root(program_root, pack_root)
        db_path = _managed_database(program_root, db_path)
        existing = load_packed_only_state(pack_root)
        if existing is None and os.path.lexists(_stage_root(pack_root)):
            raise CleanupSafetyError(
                "the generated cleanup staging path already exists without a "
                "journal; move it aside and inspect it before starting cleanup"
            )
    except (CleanupSafetyError, PackedOnlyStateError) as error:
        return {
            "eligible": False,
            "status": "invalid",
            "files": 0,
            "bytes": 0,
            "reason": str(error),
        }
    if existing is not None:
        status = existing.get("status")
        return {
            "eligible": status not in ("completed", "recovery-required"),
            "status": status,
            "files": int(existing.get("total_files", 0)),
            "bytes": int(existing.get("total_bytes", 0)),
            "moved_files": int(existing.get("moved_files", 0)),
            "moved_bytes": int(existing.get("moved_bytes", 0)),
            "reason": (
                "The verified loose audio has already been moved to Trash."
                if status == "completed"
                else (
                    f"Restore {STAGING_DIR_NAME} from Trash before continuing."
                    if status == "recovery-required"
                    else "A previous cleanup can be resumed safely."
                )
            ),
        }
    try:
        active = _read_json(pack_root / "active.json", "the active pack manifest")
        pack = AudioPack.open_active(pack_root, db_path)
        if pack is None:
            raise CleanupSafetyError("Build and activate a complete fast pack first.")
        try:
            with closing(_readonly_connection(db_path)) as connection:
                rows = int(connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
                source_ids = {
                    row[0]
                    for row in connection.execute("SELECT DISTINCT source FROM entries")
                }
            if (
                active.get("missing_files") != 0
                or active.get("valid_rows") != rows
                or pack.valid_rows != rows
            ):
                raise CleanupSafetyError(
                    "The active pack is incomplete, so originals must be kept."
                )
            _owned_source_roots(source_ids, sources, program_root, pack_root)
            estimated = max(
                0,
                int(active.get("pack_bytes", 0))
                + int(active.get("deduplicated_bytes_saved", 0)),
            )
            return {
                "eligible": True,
                "status": "ready",
                "files": int(active.get("source_path_references", 0)),
                "bytes": estimated,
                "reason": "",
            }
        finally:
            pack.close()
    except (CleanupSafetyError, OSError, sqlite3.Error, ValueError) as error:
        return {
            "eligible": False,
            "status": "unavailable",
            "files": 0,
            "bytes": 0,
            "reason": str(error),
        }


def trash_verified_loose_audio(
    db_path: Path,
    pack_root: Path,
    sources: dict,
    program_root: Path,
    trash: Callable[[list[str]], None],
    progress_callback: Optional[Callable[[PackProgress], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    batch_size: int = TRASH_BATCH_SIZE,
) -> dict:
    """Verify the sole pack copy, then move exact managed loose audio to Trash."""

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    program_root = program_root.resolve(strict=True)
    db_path = _managed_database(program_root, db_path)
    pack_root = _managed_pack_root(program_root, pack_root)
    with _PackBuildLock(pack_root / BUILD_LOCK_NAME):
        existing = load_packed_only_state(pack_root)
        if existing is None and os.path.lexists(_stage_root(pack_root)):
            raise CleanupSafetyError(
                "the generated cleanup staging path already exists without a "
                "journal; move it aside and inspect it before starting cleanup"
            )
        if existing is not None and existing.get("status") == "completed":
            return {
                "version": existing["version"],
                "files": int(existing.get("total_files", 0)),
                "bytes": int(existing.get("total_bytes", 0)),
                "already_removed": int(existing.get("total_files", 0)),
                "status": "completed",
            }
        if (
            existing is not None
            and existing.get("status") in ("quarantined", "trash-failed")
            and int(existing.get("moved_files", -1))
            == int(existing.get("total_files", -2))
            and not os.path.lexists(_stage_root(pack_root))
        ):
            existing["status"] = "recovery-required"
            existing["detail"] = (
                "protected staging is no longer local; restore "
                f"{STAGING_DIR_NAME} from the operating system Trash"
            )
            existing["updated_at_ns"] = time.time_ns()
            _atomic_json(_marker_path(pack_root), existing)
            raise CleanupSafetyError(existing["detail"])
        pack, active, db_hash = _verified_active_pack(
            db_path,
            pack_root,
            progress_callback,
            should_cancel,
        )
        try:
            with closing(_readonly_connection(db_path)) as connection:
                source_ids = {
                    row[0]
                    for row in connection.execute("SELECT DISTINCT source FROM entries")
                }
            roots = _owned_source_roots(
                source_ids,
                sources,
                program_root,
                pack_root,
            )
            protected = _protected_paths(db_path, pack_root, pack, program_root)
            allow_missing = existing is not None
            plan = _scan_plan(
                db_path,
                pack,
                roots,
                protected,
                allow_missing,
                progress_callback,
                should_cancel,
            )
            db_stat = db_path.stat()
            if existing is not None:
                if not _same_collection(existing, pack.version, db_hash, roots):
                    raise CleanupSafetyError(
                        "the cleanup journal belongs to a different database, pack, "
                        "or source layout"
                    )
                if int(existing.get("total_files", -1)) != plan["files"]:
                    raise CleanupSafetyError(
                        "the referenced file set changed after cleanup began"
                    )
                total_bytes = int(existing.get("total_bytes", -1))
                if total_bytes < plan["present_bytes"]:
                    raise CleanupSafetyError("the cleanup journal byte count is invalid")
                _validated_inventory(pack_root, existing)
                marker = dict(existing)
            else:
                total_bytes = plan["present_bytes"]
                inventory = _create_cleanup_inventory(
                    db_path,
                    pack_root,
                    pack,
                    roots,
                    protected,
                    progress_callback,
                    should_cancel,
                )
                if (
                    inventory["files"] != plan["files"]
                    or inventory["audio_bytes"] != total_bytes
                    or inventory["rows"] != plan["rows"]
                ):
                    raise CleanupSafetyError(
                        "independent recovery inventory does not match the cleanup plan"
                    )
                marker = {
                    "format": PACKED_ONLY_FORMAT,
                    "status": "staging",
                    "version": pack.version,
                    "database": {
                        "size": db_stat.st_size,
                        "mtime_ns": db_stat.st_mtime_ns,
                        "sha256": db_hash,
                    },
                    "integrity": dict(active["integrity"]),
                    "inventory": inventory,
                    "source_roots": {
                        key: str(value) for key, value in sorted(roots.items())
                    },
                    "source_slots": _source_slots(roots),
                    "staging_directory": STAGING_DIR_NAME,
                    "total_rows": plan["rows"],
                    "total_files": plan["files"],
                    "total_bytes": total_bytes,
                    "moved_files": 0,
                    "moved_bytes": 0,
                    "created_at_ns": time.time_ns(),
                }
            if marker.get("source_slots") != _source_slots(roots):
                raise CleanupSafetyError("the cleanup journal source slots are invalid")
            marker["already_removed"] = plan["missing"]
            marker["moved_files"] = plan["missing"]
            marker["moved_bytes"] = total_bytes - plan["present_bytes"]
            marker["status"] = "staging"
            marker["updated_at_ns"] = time.time_ns()
            marker.pop("detail", None)
            _check_cancel(
                should_cancel,
                PackProgress(
                    "cleanup-ready",
                    0,
                    plan["files"],
                    "Cleanup cancelled before any originals were moved.",
                ),
            )
            if _active_version(pack_root) != pack.version:
                raise CleanupSafetyError("the active pack changed before cleanup started")
            final_db_hash, _ = _sha256_file_stable(db_path)
            if final_db_hash != db_hash:
                raise CleanupSafetyError("entries.db changed before cleanup started")
            # The journal is durable before the first non-transactional Trash call.
            _atomic_json(_marker_path(pack_root), marker)
            moved_files, moved_bytes = _stage_verified_files(
                db_path,
                pack_root,
                pack,
                roots,
                protected,
                marker,
                progress_callback,
                should_cancel,
                batch_size,
            )
            if moved_files != int(marker["total_files"]):
                raise CleanupSafetyError(
                    "protected staging does not contain every referenced audio file"
                )
            if moved_bytes != int(marker["total_bytes"]):
                raise CleanupSafetyError(
                    "protected staging byte count does not match the cleanup journal"
                )
            return _trash_quarantine(
                db_path,
                pack_root,
                pack,
                roots,
                marker,
                trash,
                progress_callback,
                should_cancel,
            )
        finally:
            pack.close()


def restore_quarantined_audio(
    db_path: Path,
    pack_root: Path,
    sources: dict,
    program_root: Path,
    progress_callback: Optional[Callable[[PackProgress], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Restore from the independent inventory, even if the pack/DB is damaged."""

    program_root = program_root.resolve(strict=True)
    pack_root = _managed_pack_root(program_root, pack_root)
    with _PackBuildLock(pack_root / BUILD_LOCK_NAME):
        marker = load_packed_only_state(pack_root)
        if marker is None:
            raise CleanupSafetyError("this collection is not in packed-only mode")
        source_roots_value = marker.get("source_roots")
        slots_value = marker.get("source_slots")
        if not isinstance(source_roots_value, dict) or not isinstance(slots_value, dict):
            raise CleanupSafetyError("the cleanup journal has no safe restore mapping")
        roots = _owned_marker_roots(source_roots_value, program_root, pack_root)
        if {key: str(value) for key, value in sorted(roots.items())} != source_roots_value:
            raise CleanupSafetyError("configured source roots changed since cleanup")
        if _source_slots(roots) != slots_value:
            raise CleanupSafetyError("cleanup restore slots do not match the source roots")
        inventory_path = _validated_inventory(pack_root, marker)
        stage_root = _stage_root(pack_root)
        if os.path.lexists(stage_root):
            if stage_root.is_symlink() or not stage_root.is_dir():
                raise CleanupSafetyError("restored cleanup staging is not a safe directory")
            _reject_reparse_components(pack_root, stage_root)
        source_by_slot = {slot: source_id for source_id, slot in slots_value.items()}

        # Reject any restored payload that is not named in the durable inventory.
        if stage_root.is_dir():
            with closing(_readonly_connection(inventory_path)) as inventory:
                for current, directory_names, file_names in os.walk(
                    stage_root, topdown=True, followlinks=False
                ):
                    current_path = Path(current)
                    _reject_reparse_components(stage_root, current_path)
                    for name in directory_names:
                        value = os.lstat(current_path / name)
                        if _is_reparse(value) or not stat.S_ISDIR(value.st_mode):
                            raise CleanupSafetyError(
                                f"unsafe directory in restored quarantine: {current_path / name}"
                            )
                    for name in file_names:
                        staged = current_path / name
                        value = os.lstat(staged)
                        if _is_reparse(value) or not stat.S_ISREG(value.st_mode):
                            raise CleanupSafetyError(
                                f"unsafe file in restored quarantine: {staged}"
                            )
                        relative = staged.relative_to(stage_root)
                        if len(relative.parts) < 2:
                            raise CleanupSafetyError(
                                f"unexpected file in restored quarantine: {staged}"
                            )
                        source_id = source_by_slot.get(relative.parts[0])
                        filename = PurePosixPath(*relative.parts[1:]).as_posix()
                        if source_id is None or inventory.execute(
                            "SELECT 1 FROM files WHERE source=? AND filename=?",
                            (source_id, filename),
                        ).fetchone() is None:
                            raise CleanupSafetyError(
                                f"restored quarantine contains an untracked file: {staged}"
                            )

        restored = 0
        verified = 0
        total_files = int(marker["total_files"])
        last_progress = 0.0
        with closing(_readonly_connection(inventory_path)) as inventory:
            for source_id, filename, size, expected_hash in inventory.execute(
                "SELECT source,filename,size,sha256 FROM files ORDER BY source,filename"
            ):
                _check_cancel(
                    should_cancel,
                    PackProgress(
                        "cleanup-restoring",
                        verified,
                        total_files,
                        "Restore paused safely; run the restore action again to continue.",
                    ),
                )
                root = roots.get(source_id)
                slot = slots_value.get(source_id)
                if root is None or not isinstance(slot, str):
                    raise CleanupSafetyError(
                        f"inventory references an unknown source: {source_id}"
                    )
                relative = _relative_audio_path(filename)
                staged = stage_root / slot / Path(*relative)
                destination = root.joinpath(*relative)
                _reject_reparse_components(root, destination, allow_missing=True)
                staged_exists = os.path.lexists(staged)
                destination_exists = os.path.lexists(destination)
                if staged_exists and destination_exists:
                    raise CleanupSafetyError(
                        f"audio exists in both quarantine and its source path: {destination}"
                    )
                if not staged_exists and not destination_exists:
                    raise CleanupSafetyError(
                        "audio is missing from both its source and restored Trash folder: "
                        f"{destination}"
                    )
                candidate = staged if staged_exists else destination
                if staged_exists:
                    _reject_reparse_components(stage_root, staged)
                actual_hash = _hash_regular_file(
                    candidate, int(size), should_cancel, verified, total_files
                )
                if actual_hash != expected_hash:
                    raise CleanupSafetyError(
                        f"restored audio failed its independent SHA-256 check: {candidate}"
                    )
                if staged_exists:
                    before = _state(staged)
                    _durable_mkdirs(root, destination.parent)
                    _reject_reparse_components(root, destination.parent)
                    if os.path.lexists(destination):
                        raise CleanupSafetyError(
                            f"restore refuses to overwrite an existing file: {destination}"
                        )
                    os.replace(staged, destination)
                    if not _same_moved_file(before, _state(destination)):
                        try:
                            os.replace(destination, staged)
                        except OSError:
                            pass
                        raise CleanupSafetyError(
                            f"file identity changed during restore: {destination}"
                        )
                    _fsync_directory(destination.parent)
                    _fsync_directory(staged.parent)
                    restored += 1
                verified += 1
                now = time.monotonic()
                if now - last_progress >= PROGRESS_INTERVAL_SECONDS:
                    _emit(
                        progress_callback,
                        PackProgress(
                            "cleanup-restoring",
                            verified,
                            total_files,
                            f"Restored/verified {verified:,}/{total_files:,} loose files.",
                        ),
                    )
                    last_progress = now

        if stage_root.is_dir() and not _remove_empty_stage_tree(stage_root):
            raise CleanupSafetyError(
                "restored quarantine still contains an unexpected path"
            )

        # Re-read every destination against the independent hashes before the
        # guard is cleared. This also makes partial/cancelled restores idempotent.
        verified = 0
        last_progress = 0.0
        with closing(_readonly_connection(inventory_path)) as inventory:
            for source_id, filename, size, expected_hash in inventory.execute(
                "SELECT source,filename,size,sha256 FROM files ORDER BY source,filename"
            ):
                destination = roots[source_id].joinpath(*_relative_audio_path(filename))
                _reject_reparse_components(roots[source_id], destination)
                if _hash_regular_file(
                    destination, int(size), should_cancel, verified, total_files
                ) != expected_hash:
                    raise CleanupSafetyError(
                        f"restored source failed final SHA-256: {destination}"
                    )
                verified += 1
                now = time.monotonic()
                if now - last_progress >= PROGRESS_INTERVAL_SECONDS:
                    _emit(
                        progress_callback,
                        PackProgress(
                            "cleanup-verifying-restored",
                            verified,
                            total_files,
                            f"Final restore verification: {verified:,}/{total_files:,} files",
                        ),
                    )
                    last_progress = now
        if verified != total_files:
            raise CleanupSafetyError("not every inventoried loose audio file was restored")

        pack_valid = True
        try:
            managed_db = _managed_database(program_root, db_path)
            verified_pack, active, db_hash = _verified_active_pack(
                managed_db, pack_root, progress_callback, should_cancel
            )
            try:
                if (
                    verified_pack.version != marker.get("version")
                    or db_hash != marker.get("database", {}).get("sha256")
                    or active.get("integrity") != marker.get("integrity")
                ):
                    raise CleanupSafetyError(
                        "the active database or pack no longer matches the cleanup journal"
                    )
            finally:
                verified_pack.close()
        except (CleanupSafetyError, OSError, sqlite3.Error, ValueError):
            pack_valid = False
            active_path = pack_root / "active.json"
            if os.path.lexists(active_path):
                value = os.lstat(active_path)
                if _is_reparse(value):
                    active_path.unlink()
                elif stat.S_ISREG(value.st_mode) and value.st_nlink == 1:
                    disabled = pack_root / f"active.disabled-{uuid.uuid4().hex}.json"
                    os.replace(active_path, disabled)
                else:
                    raise CleanupSafetyError(
                        "restored originals are safe, but unsafe active.json could "
                        "not be disabled automatically"
                    )
                _fsync_directory(pack_root)

        clear_packed_only_state(pack_root, replacement_verified=True)
        try:
            inventory_path.unlink()
            _fsync_directory(pack_root)
        except OSError:
            pass
        _emit(
            progress_callback,
            PackProgress(
                "cleanup-restored",
                total_files,
                total_files,
                f"Restored and independently verified all {total_files:,} files.",
            ),
        )
        return {
            "files": restored,
            "rows": int(marker.get("total_rows", 0)),
            "status": "restored",
            "pack_valid": pack_valid,
        }
