from __future__ import annotations

import sqlite3
import threading
import time

from contextlib import closing
from pathlib import Path
from typing import Optional

from aqt import gui_hooks, mw
from aqt.operations import QueryOp
from aqt.qt import QAction, qconnect
from aqt.utils import showInfo, showWarning

from .config import ALL_SOURCES
from .db_utils import (
    get_count,
    get_num_files_per_source,
    get_unique_count,
    init_db,
    table_exists_and_has_data,
    table_must_be_updated,
)
from .fast_pack import PackBuildCancelled, build_audio_pack
from .migration import (
    claim_automatic_pack_build,
    discover_existing_collection,
    finish_automatic_pack_build,
    process_existing_collection,
    validate_entries_database,
)
from .server import get_runtime
from .util import get_db_path, get_program_root_path


_gui_initialized = False
_job_lock = threading.Lock()
_active_job: Optional[str] = None


def _claim_job(name: str, quiet: bool = False) -> bool:
    global _active_job
    with _job_lock:
        if _active_job is not None:
            if not quiet:
                showInfo(f"Local Audio Server is already {_active_job}.")
            return False
        _active_job = name
        return True


def _finish_job() -> None:
    global _active_job
    with _job_lock:
        _active_job = None


def _operation_failure(error: Exception) -> None:
    _finish_job()
    showWarning(f"Local Audio Server operation failed:\n\n{error}")


def _pack_operation_failure(error: Exception, progress) -> None:
    try:
        progress.close()
    finally:
        _finish_job()
    if isinstance(error, PackBuildCancelled):
        completed = error.progress.current
        total = error.progress.total
        position = ""
        if total:
            position = f" ({completed / total:.0%})"
        showInfo(
            f"{error.progress.message}{position}\n\n"
            "Open the build/import action again—or restart Anki—to continue "
            "from the saved checkpoint. The current audio server remains usable."
        )
    else:
        showWarning(f"Local Audio Server operation failed:\n\n{error}")


def _with_failure(operation, callback=_operation_failure):
    failure = getattr(operation, "failure", None)
    return failure(callback) if failure is not None else operation


def attempt_init_db_gui() -> None:
    if not table_exists_and_has_data():
        regenerate_database_operation()
    elif table_must_be_updated():
        regenerate_database_operation("Updating local audio database.")
    else:
        maybe_automatic_pack_build()


def regenerate_database_operation(msg: Optional[str] = None) -> None:
    if not _claim_job("regenerating the desktop database"):
        return
    if not msg:
        msg = "Generating local audio database."
    base_msg = f"{msg}\nThis may take a while."
    started = time.perf_counter()
    operation = QueryOp(
        parent=mw,
        op=lambda _: regenerate_database_action(base_msg),
        success=lambda _: _regenerate_database_finished(started),
    )
    operation = _with_failure(operation)
    first_source = next(iter(ALL_SOURCES.values()), None)
    start_msg = base_msg
    if first_source is not None:
        start_msg += f"\n\nAdding entries from {first_source.data.id}..."
    operation.with_progress(start_msg).run_in_background()


def _regenerate_database_finished(started: float) -> None:
    _finish_job()
    regenerate_database_success(started)


def regenerate_database_action(progress_msg: str) -> int:
    def callback(message: str) -> None:
        mw.taskman.run_on_main(
            lambda: mw.progress.update(label=progress_msg + "\n\n" + message)
        )

    runtime = get_runtime()
    publisher = runtime.store.publish_database if runtime is not None else None
    init_db(callback, publisher=publisher)
    return 1


def regenerate_database_success(started: float) -> None:
    elapsed = time.perf_counter() - started
    showInfo(f"Local audio database regenerated in {elapsed:.1f} seconds.")
    maybe_automatic_pack_build()


def build_fast_pack_operation() -> None:
    if not _claim_job("building or importing audio"):
        return
    from .progress_ui import OperationProgress

    progress = OperationProgress(
        "Local Audio Server",
        "Preparing the fast desktop audio pack…",
    )
    started = time.perf_counter()
    operation = QueryOp(
        parent=mw,
        op=lambda _: build_fast_pack_action(progress),
        success=lambda result: _build_fast_pack_finished(started, result, progress),
    )
    operation = _with_failure(
        operation, lambda error: _pack_operation_failure(error, progress)
    )
    operation.run_in_background()


def _build_fast_pack_finished(started: float, result: dict, progress=None) -> None:
    try:
        if progress is not None:
            progress.close()
    finally:
        _finish_job()
    build_fast_pack_success(started, result)


def build_fast_pack_action(progress=None) -> dict:
    result = build_audio_pack(
        get_db_path(),
        get_program_root_path() / "user_files" / "fast_audio",
        ALL_SOURCES,
        callback=None if progress is not None else (
            lambda message: mw.taskman.run_on_main(
                lambda: mw.progress.update(label=message)
            )
        ),
        progress_callback=progress.emit_progress if progress is not None else None,
        should_cancel=progress.cancelled if progress is not None else None,
    )
    runtime = get_runtime()
    if runtime is not None:
        runtime.store.reload_pack()
    return result


def build_fast_pack_success(started: float, result: dict) -> None:
    elapsed = time.perf_counter() - started
    showInfo(
        "Fast desktop audio pack is active.\n\n"
        f"Version: {result['version']}\n"
        f"Rows covered: {result['valid_rows']:,}\n"
        f"Distinct source/path files: {result['source_path_references']:,}\n"
        f"Mapping rows sharing those paths: {result['mapping_rows_reusing_source_paths']:,}\n"
        f"Unique audio blobs: {result['unique_files']:,}\n"
        f"Byte-identical files deduplicated: {result['content_duplicates']:,}\n"
        f"Duplicate bytes saved: {result['deduplicated_bytes_saved'] / (1024 ** 2):.1f} MiB\n"
        f"Missing/invalid files: {result['missing_files']:,}\n"
        f"Pack size: {result['pack_bytes'] / (1024 ** 3):.2f} GiB\n"
        f"Build time: {elapsed:.1f} seconds"
    )


def maybe_automatic_pack_build() -> None:
    """One automatic attempt per unchanged existing DB/source collection."""

    runtime = get_runtime()
    if runtime is None or not _claim_job("processing existing audio", quiet=True):
        return
    pack_root = get_program_root_path() / "user_files" / "fast_audio"
    try:
        fingerprint = claim_automatic_pack_build(
            get_db_path(),
            pack_root,
            ALL_SOURCES,
            runtime.store.info().get("audioPack") is not None,
        )
    except Exception as error:
        _finish_job()
        showWarning(f"Could not inspect existing audio for acceleration:\n\n{error}")
        return
    if fingerprint is None:
        _finish_job()
        return

    started = time.perf_counter()
    from .progress_ui import OperationProgress

    progress = OperationProgress(
        "Local Audio Server",
        "Accelerating the existing audio collection…",
    )

    def success(result: dict) -> None:
        try:
            finish_automatic_pack_build(pack_root, fingerprint, "completed")
        finally:
            progress.close()
            _finish_job()
        build_fast_pack_success(started, result)

    def failure(error: Exception) -> None:
        status = "paused" if isinstance(error, PackBuildCancelled) else "failed"
        try:
            finish_automatic_pack_build(pack_root, fingerprint, status, str(error))
        finally:
            _pack_operation_failure(error, progress)

    operation = QueryOp(
        parent=mw,
        op=lambda _: build_fast_pack_action(progress),
        success=success,
    )
    operation = _with_failure(operation, failure)
    operation.run_in_background()


def import_existing_audio_operation() -> None:
    from .import_dialog import choose_existing_audio_directory

    def validate_selected(path: Path) -> None:
        discovery = discover_existing_collection(path, ALL_SOURCES)
        if discovery.database is not None:
            try:
                validate_entries_database(discovery.database, set(ALL_SOURCES))
            except sqlite3.DatabaseError as error:
                raise ValueError(str(error)) from error

    selected = choose_existing_audio_directory(
        mw, get_program_root_path(), validate_selected
    )
    if selected is None:
        return
    if not _claim_job("building or importing audio"):
        return
    from .progress_ui import OperationProgress

    progress = OperationProgress(
        "Import existing audio collection",
        "Validating the dropped audio collection…",
    )
    started = time.perf_counter()

    def action() -> dict:
        runtime = get_runtime()
        return process_existing_collection(
            Path(selected),
            ALL_SOURCES,
            get_db_path(),
            get_program_root_path() / "user_files" / "fast_audio",
            callback=progress.emit_message,
            progress_callback=progress.emit_progress,
            should_cancel=progress.cancelled,
            publisher=runtime.store.publish_database if runtime is not None else None,
            replace_sources=runtime.store.replace_sources if runtime is not None else None,
            reload_pack=runtime.store.reload_pack if runtime is not None else None,
        )

    def success(result: dict) -> None:
        migrated_sources = result.pop("_sources")
        ALL_SOURCES.clear()
        ALL_SOURCES.update(migrated_sources)
        try:
            progress.close()
        finally:
            _finish_job()
        pack = result["pack"]
        elapsed = time.perf_counter() - started
        showInfo(
            "Existing audio collection processed without moving or deleting originals.\n\n"
            f"Database: {result['database']['mode']}\n"
            f"Source folders detected: {len(result['source_paths'])}\n"
            f"Rows covered: {pack['valid_rows']:,}\n"
            f"Fast pack: {pack['pack_bytes'] / (1024 ** 3):.2f} GiB\n"
            f"Time: {elapsed:.1f} seconds"
        )

    operation = QueryOp(parent=mw, op=lambda _: action(), success=success)
    operation = _with_failure(
        operation, lambda error: _pack_operation_failure(error, progress)
    )
    operation.run_in_background()


def show_stats() -> None:
    uri = f"file:{get_db_path().resolve().as_posix()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        count = get_count(connection)
        files_per_source = get_num_files_per_source(connection)
        unique_count = get_unique_count(connection)
    lines = [f"{source}: {source_count:,}" for source, source_count in files_per_source.items()]
    lines.extend(("", f"Unique expressions: {unique_count:,}", f"Total mappings: {count:,}"))
    runtime = get_runtime()
    if runtime is not None:
        pack = runtime.store.info().get("audioPack")
        lines.append("")
        lines.append(
            f"Fast pack: {pack['version']} ({pack['packBytes'] / (1024 ** 3):.2f} GiB)"
            if pack
            else "Fast pack: not built; serving original files"
        )
    showInfo("<br>".join(lines), title="Local Audio Statistics")


def init_gui() -> None:
    global _gui_initialized
    if _gui_initialized:
        return
    menu = mw.form.menuTools.addMenu("Local Audio Server")
    regenerate = QAction("Regenerate desktop database", mw)
    qconnect(regenerate.triggered, regenerate_database_operation)
    menu.addAction(regenerate)
    import_existing = QAction("Import existing audio collection…", mw)
    qconnect(import_existing.triggered, import_existing_audio_operation)
    menu.addAction(import_existing)
    build_pack = QAction("Build/rebuild fast desktop audio pack", mw)
    qconnect(build_pack.triggered, build_fast_pack_operation)
    menu.addAction(build_pack)
    statistics = QAction("Show statistics", mw)
    qconnect(statistics.triggered, show_stats)
    menu.addAction(statistics)
    gui_hooks.main_window_did_init.append(attempt_init_db_gui)
    _gui_initialized = True
