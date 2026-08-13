from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import get_config_path, read_config
from .db_utils import init_db, update_db_version
from .fast_pack import (
    BUILD_LOCK_NAME,
    _PackBuildLock,
    build_audio_pack,
    protected_cleanup_state_exists,
)
from .fast_pack import PackBuildCancelled, PackProgress
from .source.audio_source import AudioSourceData


REQUIRED_ENTRY_COLUMNS = {
    "id",
    "expression",
    "reading",
    "source",
    "speaker",
    "display",
    "file",
}
AUTO_MARKER_NAME = "auto-build-v1.json"
_PROCESS_TOKEN = uuid.uuid4().hex
SUPPORTED_AUDIO_SUFFIXES = {
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".flac",
    ".wav",
}


@dataclass(frozen=True)
class ExistingCollection:
    selected: Path
    collection_root: Path
    database: Optional[Path]
    source_paths: dict[str, Path]


def _resolved_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"selected path is not a directory: {resolved}")
    return resolved


def discover_existing_collection(selected: Path, sources: dict) -> ExistingCollection:
    """Resolve an add-on root, user_files root, collection, or source folder."""

    selected = _resolved_directory(selected)
    folder_by_source = {
        source_id: Path(source.data.media_dir).name
        for source_id, source in sources.items()
    }
    roots: list[Path] = []

    def add_root(path: Path) -> None:
        if path.is_dir() and path not in roots:
            roots.append(path)

    add_root(selected / "user_files")
    add_root(selected)
    if selected.name in folder_by_source.values():
        add_root(selected.parent)

    database = None
    for root in roots:
        candidate = root / "entries.db"
        if candidate.is_file():
            database = candidate.resolve(strict=True)
            break

    discovered: dict[str, Path] = {}
    for source_id, folder_name in folder_by_source.items():
        if selected.name == folder_name:
            discovered[source_id] = selected
            continue
        for root in roots:
            candidate = root / folder_name
            if candidate.is_dir():
                discovered[source_id] = candidate.resolve(strict=True)
                break

    if database is None and not discovered:
        expected = ", ".join(sorted(set(folder_by_source.values())))
        raise ValueError(
            "No entries.db or recognized audio source folder was found. "
            f"Expected one of: {expected}"
        )
    collection_root = (
        database.parent
        if database is not None
        else next(iter(discovered.values())).parent
    )
    return ExistingCollection(selected, collection_root, database, discovered)


def validate_entries_database(path: Path, allowed_sources: set[str]) -> dict:
    path = path.resolve(strict=True)
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise sqlite3.DatabaseError(f"entries.db quick_check failed: {check}")
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(entries)").fetchall()
        }
        if not REQUIRED_ENTRY_COLUMNS.issubset(columns):
            missing = ", ".join(sorted(REQUIRED_ENTRY_COLUMNS - columns))
            raise sqlite3.DatabaseError(f"entries.db is missing columns: {missing}")
        rows = int(connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
        if rows <= 0:
            raise sqlite3.DatabaseError("entries.db contains no audio mappings")
        database_sources = {
            row[0] for row in connection.execute("SELECT DISTINCT source FROM entries")
        }
    unknown = database_sources - allowed_sources
    if unknown:
        raise ValueError(
            "entries.db references unconfigured sources: " + ", ".join(sorted(unknown))
        )
    return {"rows": rows, "sources": sorted(database_sources), "quick_check": check}


def inspect_installed_collection(path: Path, sources: dict) -> dict:
    """Describe the preserved same-ID collection for the one-click prompt."""

    path = path.resolve(strict=True)
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(entries)")
        }
        if not REQUIRED_ENTRY_COLUMNS.issubset(columns):
            raise sqlite3.DatabaseError("entries.db does not have the expected schema")
        rows = int(connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
        if rows <= 0:
            raise sqlite3.DatabaseError("entries.db contains no audio mappings")
        database_sources = {
            row[0] for row in connection.execute("SELECT DISTINCT source FROM entries")
        }
    unknown = database_sources - set(sources)
    if unknown:
        raise ValueError(
            "entries.db references unconfigured sources: "
            + ", ".join(sorted(unknown))
        )
    configured_roots = 0
    for source_id in database_sources:
        source = sources.get(source_id)
        if source is not None and source.get_media_dir_path().is_dir():
            configured_roots += 1
    if configured_roots != len(database_sources):
        raise ValueError(
            "only "
            f"{configured_roots} of {len(database_sources)} configured source "
            "folders are available; use Import existing audio collection to "
            "choose the complete collection"
        )
    return {
        "rows": rows,
        "database_sources": len(database_sources),
        "source_folders": configured_roots,
    }


def remap_sources(sources: dict, source_paths: dict[str, Path]) -> dict:
    remapped = {}
    for source_id, source in sources.items():
        path = source_paths.get(source_id)
        if path is None:
            remapped[source_id] = source
            continue
        data = AudioSourceData(source_id, str(path.resolve(strict=True)), source.data.display)
        remapped[source_id] = source.__class__(data)
    return remapped


def persist_source_paths(source_paths: dict[str, Path]) -> Path:
    """Atomically merge only source paths into the add-on's user config."""

    config_path = get_config_path().resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        user_config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(user_config, dict):
            user_config = {}
    except (OSError, ValueError, json.JSONDecodeError):
        user_config = {}
    merged = read_config()
    persisted_sources = []
    for item in merged.get("sources", []):
        if not isinstance(item, dict):
            continue
        updated = dict(item)
        source_id = updated.get("id")
        if source_id in source_paths:
            updated["path"] = str(source_paths[source_id].resolve(strict=True))
        persisted_sources.append(updated)
    user_config["sources"] = persisted_sources
    temporary = config_path.with_name(config_path.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(user_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, config_path)
    return config_path


def _copy_database_for_publish(
    source: Path,
    destination: Path,
    allowed_sources: set[str],
    publisher: Optional[Callable[[Path], None]],
    progress_callback: Optional[Callable[[PackProgress], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    try:
        same_file = destination.is_file() and os.path.samefile(source, destination)
    except OSError:
        same_file = False
    if same_file:
        validation = validate_entries_database(source, allowed_sources)
        validation["mode"] = "existing"
        return validation

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".import-{uuid.uuid4().hex}")
    before = source.stat()
    try:
        copied = 0
        with source.open("rb") as source_file, temporary.open("wb") as target_file:
            while chunk := source_file.read(4 * 1024 * 1024):
                if should_cancel is not None and should_cancel():
                    raise PackBuildCancelled(
                        PackProgress(
                            "copying-database",
                            copied,
                            before.st_size,
                            "Import paused before publishing entries.db.",
                        )
                    )
                target_file.write(chunk)
                copied += len(chunk)
                if progress_callback is not None:
                    progress_callback(
                        PackProgress(
                            "copying-database",
                            copied,
                            before.st_size,
                            f"Copying entries.db: {copied:,}/{before.st_size:,} bytes",
                        )
                    )
            target_file.flush()
            os.fsync(target_file.fileno())
        shutil.copystat(source, temporary)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("source entries.db changed while it was being copied")
        validation = validate_entries_database(temporary, allowed_sources)
        if publisher is None:
            inferred_pack_root = destination.parent / "fast_audio"
            with _PackBuildLock(inferred_pack_root / BUILD_LOCK_NAME):
                if protected_cleanup_state_exists(inferred_pack_root):
                    raise RuntimeError(
                        "database import became blocked before publication"
                    )
                os.replace(temporary, destination)
        else:
            publisher(temporary)
        validation["mode"] = "copied"
        return validation
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def process_existing_collection(
    selected: Path,
    sources: dict,
    db_path: Path,
    pack_root: Path,
    callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[PackProgress], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    publisher: Optional[Callable[[Path], None]] = None,
    replace_sources: Optional[Callable[[dict], None]] = None,
    reload_pack: Optional[Callable[[], bool]] = None,
) -> dict:
    """Import metadata by copy, never audio, then build and atomically publish a pack."""

    from .cleanup import load_packed_only_state

    if protected_cleanup_state_exists(pack_root):
        raise RuntimeError(
            "import is blocked while this collection is packed-only. Restore the "
            "original source audio to its configured folders first; a future "
            "recovery action must verify the full replacement before publication."
        )

    def check_cancel(stage: str, message: str) -> None:
        if should_cancel is not None and should_cancel():
            raise PackBuildCancelled(PackProgress(stage, 0, 0, message))

    check_cancel("discovering", "Import paused before collection discovery.")
    discovery = discover_existing_collection(selected, sources)
    if callback is not None:
        callback("Validated selected collection; preserving all original files.")
    migrated_sources = remap_sources(sources, discovery.source_paths)
    check_cancel("validating-database", "Import paused before database validation.")
    if discovery.database is not None:
        validate_entries_database(discovery.database, set(migrated_sources))

    if discovery.database is not None:
        if callback is not None:
            callback("Validating and copying the existing entries.db...")
        database_result = _copy_database_for_publish(
            discovery.database,
            db_path,
            set(migrated_sources),
            publisher,
            progress_callback,
            should_cancel,
        )
        update_db_version()
    else:
        if callback is not None:
            callback("No entries.db found; regenerating metadata from source folders...")
        try:
            init_db(
                callback=callback,
                publisher=publisher,
                sources=migrated_sources,
                should_cancel=should_cancel,
            )
        except InterruptedError as error:
            raise PackBuildCancelled(
                PackProgress(
                    "generating-database",
                    0,
                    0,
                    "Import paused before publishing generated metadata.",
                )
            ) from error
        database_result = {"mode": "regenerated"}

    # Commit source configuration only after the database publication succeeds.
    # If packing subsequently fails, every layer consistently serves the new
    # collection through secure loose-file fallback and a manual retry is safe.
    with _PackBuildLock(pack_root / BUILD_LOCK_NAME):
        if protected_cleanup_state_exists(pack_root):
            raise RuntimeError(
                "import became blocked before source configuration publication"
            )
        persist_source_paths(discovery.source_paths)
        if replace_sources is not None:
            replace_sources(migrated_sources)
        sources.clear()
        sources.update(migrated_sources)

    check_cancel(
        "packing",
        "Import paused safely. The imported database can serve loose audio on restart.",
    )
    if callback is not None:
        callback("Building the immutable fast desktop audio pack...")
    pack_options = {"callback": callback}
    if progress_callback is not None:
        pack_options["progress_callback"] = progress_callback
    if should_cancel is not None:
        pack_options["should_cancel"] = should_cancel
    pack_result = build_audio_pack(
        db_path,
        pack_root,
        migrated_sources,
        **pack_options,
    )
    if reload_pack is not None:
        reload_pack()
    return {
        "selected": str(discovery.selected),
        "collection_root": str(discovery.collection_root),
        "source_paths": {key: str(value) for key, value in discovery.source_paths.items()},
        "database": database_result,
        "pack": pack_result,
        "_sources": migrated_sources,
    }


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def auto_pack_fingerprint(db_path: Path, sources: dict) -> Optional[str]:
    if not db_path.is_file():
        return None
    stat = db_path.stat()
    source_roots = {}
    for source_id, source in sources.items():
        root = source.get_media_dir_path().resolve()
        if root.is_dir():
            source_roots[source_id] = root
    first_audio = None
    try:
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            for source_id, filename in connection.execute(
                "SELECT source,file FROM entries"
            ):
                root = source_roots.get(source_id)
                if root is None or not isinstance(filename, str):
                    continue
                relative = Path(filename.replace("\\", "/"))
                if (
                    relative.is_absolute()
                    or relative.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES
                ):
                    continue
                try:
                    candidate = (root / relative).resolve(strict=True)
                    candidate.relative_to(root)
                    if not candidate.is_file():
                        continue
                    audio_stat = candidate.stat()
                    first_audio = (
                        source_id,
                        relative.as_posix(),
                        audio_stat.st_size,
                        audio_stat.st_mtime_ns,
                    )
                    break
                except (OSError, ValueError):
                    continue
    except sqlite3.Error:
        return None
    if first_audio is None:
        return None
    payload = json.dumps(
        {
            "db": [stat.st_size, stat.st_mtime_ns],
            "sources": sorted((key, str(value)) for key, value in source_roots.items()),
            "first_audio": first_audio,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def claim_automatic_pack_build(
    db_path: Path, pack_root: Path, sources: dict, pack_is_active: bool
) -> Optional[str]:
    if pack_is_active:
        return None
    fingerprint = auto_pack_fingerprint(db_path, sources)
    if fingerprint is None:
        return None
    marker = pack_root / AUTO_MARKER_NAME
    try:
        previous = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        previous = {}
    if previous.get("fingerprint") == fingerprint:
        if previous.get("status") in ("failed", "declined"):
            return None
        if (
            previous.get("status") == "started"
            and previous.get("owner") == _PROCESS_TOKEN
        ):
            return None
    value = {"fingerprint": fingerprint, "status": "started", "owner": _PROCESS_TOKEN}
    if (
        previous.get("fingerprint") == fingerprint
        and previous.get("status") in ("started", "paused")
    ):
        value["resumed_from"] = previous["status"]
    _atomic_json(marker, value)
    return fingerprint


def automatic_pack_build_state(pack_root: Path, fingerprint: str) -> Optional[str]:
    try:
        marker = json.loads(
            (pack_root / AUTO_MARKER_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if marker.get("fingerprint") != fingerprint:
        return None
    status = marker.get("resumed_from")
    return status if isinstance(status, str) else None


def finish_automatic_pack_build(
    pack_root: Path, fingerprint: str, status: str, detail: Optional[str] = None
) -> None:
    value = {"fingerprint": fingerprint, "status": status}
    if detail:
        value["detail"] = detail
    _atomic_json(pack_root / AUTO_MARKER_NAME, value)
