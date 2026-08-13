from __future__ import annotations

import json
import os
import sqlite3
import uuid

from dataclasses import dataclass, field
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

from .config import ALL_SOURCES
from .consts import DB_VERSION_FILE_NAME, JMDICT_FORMS_JSON_FILE_NAME
from .jp_util import is_hiragana
from .util import QueryComponents, get_db_path, get_program_root_path, get_version_file_path


UPDATE_VERSIONS = [(1, 3, 0)]
INSERT_ROW_SQL = (
    "INSERT INTO entries (expression, reading, source, speaker, display, file) "
    "VALUES (?,?,?,?,?,?)"
)
SEARCH_QUERY = (
    "SELECT id, expression, reading, source, speaker, display, file "
    "FROM entries WHERE expression=? AND reading=?"
)

ROWID = 0
EXPRESSION = 1
READING = 2
SOURCE = 3
SPEAKER = 4
DISPLAY = 5
FILE = 6


class ExpressionInfo(TypedDict, total=False):
    kanji: str
    reading: str
    override_reading: str


class ExpressionGroup(TypedDict):
    reading: str
    expressions: list[ExpressionInfo]


@dataclass
class ExpressionMeta:
    expression: str
    reading: str
    found_audio_row_slices: set[tuple] = field(default_factory=set)


def _readonly_connection() -> sqlite3.Connection:
    path = get_db_path().resolve()
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def table_exists_and_has_data() -> bool:
    path = get_db_path()
    if not path.is_file():
        return False
    try:
        with closing(_readonly_connection()) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries'"
            ).fetchone()
            if exists is None:
                return False
            return connection.execute("SELECT 1 FROM entries LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def update_check(
    previous: tuple[int, ...], latest: tuple[int, ...], versions: list[tuple[int, ...]]
) -> bool:
    if previous >= latest:
        return False
    return any(previous < version <= latest for version in versions)


def _read_version(path: Path) -> Optional[tuple[int, int, int]]:
    try:
        parts = tuple(int(value) for value in path.read_text().strip().split("."))
    except (OSError, ValueError):
        return None
    return parts if len(parts) == 3 else None


def table_must_be_updated() -> bool:
    previous = _read_version(get_program_root_path() / DB_VERSION_FILE_NAME)
    latest = _read_version(get_version_file_path())
    if previous is None or latest is None:
        return True
    return update_check(previous, latest, UPDATE_VERSIONS)


def attempt_init_db() -> None:
    if not table_exists_and_has_data() or table_must_be_updated():
        init_db()


def update_db_version() -> None:
    destination = get_program_root_path() / DB_VERSION_FILE_NAME
    temporary = destination.with_name(destination.name + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        get_version_file_path().read_text(encoding="utf-8").strip() + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def get_num_files_per_source(connection: sqlite3.Connection) -> dict[str, int]:
    return dict(
        connection.execute(
            "SELECT source, COUNT(*) FROM entries GROUP BY source ORDER BY source"
        ).fetchall()
    )


def get_count(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]


def get_unique_count(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT COUNT(DISTINCT expression) FROM entries").fetchone()[0]


def backfill_jmdict_forms_rows(
    connection: sqlite3.Connection,
    group: ExpressionGroup,
    new_rows: list[tuple],
    new_rows_set: set[tuple],
) -> None:
    group_reading = group["reading"]
    metadata = [
        ExpressionMeta(
            expression["kanji"],
            expression.get("reading", expression.get("override_reading", group_reading)),
        )
        for expression in group["expressions"]
    ]
    source_rows = []
    for meta in metadata:
        if not is_hiragana(meta.expression):
            source_rows.extend(
                connection.execute(SEARCH_QUERY, (meta.expression, meta.reading)).fetchall()
            )
    if not source_rows:
        return
    for row in source_rows:
        for meta in metadata:
            if meta.expression == row[EXPRESSION] and meta.reading == row[READING]:
                meta.found_audio_row_slices.add(tuple(row[SOURCE:]))
    for row in source_rows:
        audio_slice = tuple(row[SOURCE:])
        for meta in metadata:
            if audio_slice in meta.found_audio_row_slices:
                continue
            meta.found_audio_row_slices.add(audio_slice)
            new_row = (meta.expression, meta.reading) + audio_slice
            if new_row not in new_rows_set:
                new_rows.append(new_row)
                new_rows_set.add(new_row)


def fill_jmdict_forms(
    connection: sqlite3.Connection,
    callback: Optional[Callable[[str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> int:
    forms_path = get_program_root_path() / JMDICT_FORMS_JSON_FILE_NAME
    if not forms_path.is_file():
        return 0
    groups: list[ExpressionGroup] = json.loads(forms_path.read_text(encoding="utf-8"))
    new_rows: list[tuple] = []
    new_rows_set: set[tuple] = set()
    for index, group in enumerate(groups):
        if should_cancel is not None and should_cancel():
            raise InterruptedError("database generation cancelled")
        backfill_jmdict_forms_rows(connection, group, new_rows, new_rows_set)
        if callback is not None and index and index % 20000 == 0:
            callback(f"JMdict forms: {index:,}/{len(groups):,} groups")
    connection.executemany(INSERT_ROW_SQL, new_rows)
    connection.commit()
    return len(new_rows)


def _initialize_schema(connection: sqlite3.Connection) -> None:
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


def init_db(
    callback: Optional[Callable[[str], None]] = None,
    publisher: Optional[Callable[[Path], None]] = None,
    sources: Optional[dict] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> None:
    """Build off to the side with deferred indexes, verify, then publish atomically."""

    destination = get_db_path().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".building-{uuid.uuid4().hex}")
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA cache_size=-131072")
            connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            _initialize_schema(connection)
            source_map = sources if sources is not None else ALL_SOURCES
            for source in source_map.values():
                if should_cancel is not None and should_cancel():
                    raise InterruptedError("database generation cancelled")
                if callback is not None:
                    callback(f"Adding entries from {source.data.id}...")
                if should_cancel is None:
                    source.add_entries(connection)
                else:
                    source.add_entries(connection, should_cancel=should_cancel)
                if should_cancel is not None and should_cancel():
                    raise InterruptedError("database generation cancelled")
            if callback is not None:
                callback("Building the expression/reading index...")
            connection.execute(
                "CREATE INDEX idx_expr_reading ON entries(expression, reading)"
            )
            connection.commit()
            if callback is not None:
                callback("Backfilling entries using JMdict forms...")
            fill_jmdict_forms(connection, callback, should_cancel)
            connection.execute("ANALYZE")
            connection.execute("PRAGMA optimize")
            connection.commit()
            check = connection.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise sqlite3.DatabaseError(f"generated database failed quick_check: {check}")
        if publisher is None:
            os.replace(temporary, destination)
        else:
            publisher(temporary)
        update_db_version()
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def execute_query(
    connection: sqlite3.Connection, query: QueryComponents
) -> list[tuple[Any, ...]]:
    if query.reading is None:
        rows = connection.execute(
            "SELECT id,expression,reading,source,speaker,display,file "
            "FROM entries WHERE expression=?",
            (query.expression,),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT id,expression,reading,source,speaker,display,file "
            "FROM entries WHERE expression=? AND (reading IS NULL OR reading=?)",
            (query.expression, query.reading),
        ).fetchall()
    source_set = set(query.sources)
    user_set = set(query.user)
    rows = [
        row
        for row in rows
        if row[SOURCE] in source_set
        and (not user_set or row[SPEAKER] is None or row[SPEAKER] in user_set)
    ]
    source_rank = {source: rank for rank, source in enumerate(query.sources)}
    user_rank = {user: rank for rank, user in enumerate(query.user)}
    rows.sort(
        key=lambda row: (
            source_rank.get(row[SOURCE], len(source_rank)),
            -1
            if row[SPEAKER] is None
            else user_rank.get(row[SPEAKER], len(user_rank)),
            (row[READING] is not None, row[READING] or ""),
            row[ROWID],
        )
    )
    return rows
