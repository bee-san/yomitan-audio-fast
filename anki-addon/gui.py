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
from aqt.utils import askUser, showInfo, showWarning

from .cleanup import (
    LooseAudioChangedError,
    PackMismatchError,
    PackedOnlyStateError,
    inspect_managed_cleanup,
    load_packed_only_state,
    restore_quarantined_audio,
    trash_verified_loose_audio,
)
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
from .fast_pack import protected_cleanup_state_exists
from .migration import (
    claim_automatic_pack_build,
    automatic_pack_build_state,
    discover_existing_collection,
    finish_automatic_pack_build,
    inspect_installed_collection,
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
    pack_root = get_program_root_path() / "user_files" / "fast_audio"
    try:
        packed_only = load_packed_only_state(pack_root)
    except PackedOnlyStateError as error:
        showWarning(
            f"Local Audio Server maintenance is blocked:\n\n{error}\n\n"
            "The current database and pack were left unchanged. Restore "
            "loose-audio-originals-v1 from Trash, then run Restore/verify loose "
            "audio originals."
        )
        return
    if packed_only is None and protected_cleanup_state_exists(pack_root):
        showWarning(
            "Local Audio Server maintenance is blocked because protected "
            "loose-audio quarantine exists without its cleanup journal.\n\n"
            "Nothing was rebuilt or removed. Inspect "
            "user_files/fast_audio/loose-audio-originals-v1 and restore its "
            "audio before continuing."
        )
        return
    if not table_exists_and_has_data():
        if packed_only is not None:
            showWarning(
                "The loose audio originals were moved to Trash, but entries.db is "
                "missing or empty. Automatic regeneration is blocked so the sole "
                "verified pack is not invalidated. Restore the original cleanup "
                "folder from Trash, then run Restore/verify loose audio "
                "originals."
            )
            return
        regenerate_database_operation()
    elif table_must_be_updated():
        if packed_only is not None:
            showWarning(
                "A database update is available, but the loose audio originals were "
                "moved to Trash. The current database and verified pack were kept "
                "unchanged. Restore the cleanup folder from Trash, then run "
                "Restore/verify loose audio originals before updating."
            )
            return
        regenerate_database_operation("Updating local audio database.")
    else:
        if packed_only is not None:
            status = packed_only.get("status")
            stage_root = (
                get_program_root_path()
                / "user_files"
                / "fast_audio"
                / "loose-audio-originals-v1"
            )
            if stage_root.is_dir() and status in ("completed", "recovery-required"):
                if askUser(
                    "A Local Audio Server cleanup folder was restored from Trash. "
                    "Put the verified loose audio back into its original source "
                    "folders and leave packed-only mode now?",
                    title="Restored audio found",
                    defaultno=False,
                ):
                    restore_loose_audio_operation()
            elif status != "completed":
                remove_loose_audio_operation()
            return
        maybe_automatic_pack_build(confirm_existing=True)


def _packed_only_blocks_maintenance(action: str) -> bool:
    pack_root = get_program_root_path() / "user_files" / "fast_audio"
    try:
        state = load_packed_only_state(pack_root)
    except PackedOnlyStateError as error:
        showWarning(f"Cannot {action}:\n\n{error}")
        return True
    if state is None and not protected_cleanup_state_exists(pack_root):
        return False
    if state is None:
        showWarning(
            f"Cannot {action} while protected loose-audio quarantine exists.\n\n"
            "The cleanup journal is missing, so maintenance is blocked to avoid "
            "building from a partial collection. Inspect "
            "user_files/fast_audio/loose-audio-originals-v1 and restore the "
            "journal or audio before continuing."
        )
        return True
    showWarning(
        f"Cannot {action} while this collection is packed-only.\n\n"
        "The verified pack and entries.db are now the serving copy because the "
        "loose audio was moved to Trash. Restore loose-audio-originals-v1 from "
        "Trash into user_files/fast_audio, then run Restore/verify loose audio "
        "originals before maintenance."
    )
    return True


def regenerate_database_operation(msg: Optional[str] = None) -> None:
    if _packed_only_blocks_maintenance("regenerate the database"):
        return
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
    maybe_automatic_pack_build(confirm_existing=False)


def build_fast_pack_operation() -> None:
    if _packed_only_blocks_maintenance("rebuild the audio pack"):
        return
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
    coverage = (
        "Every mapping is packed. Your loose originals are still in place."
        if result["missing_files"] == 0
        else (
            f"{result['missing_files']:,} source files were missing or invalid, so "
            "loose-file cleanup is unavailable."
        )
    )
    showInfo(
        "Fast desktop audio pack is active.\n\n"
        f"Version: {result['version']}\n"
        f"Audio mappings packed: {result['valid_rows']:,}\n"
        f"Pack size: {result['pack_bytes'] / (1024 ** 3):.2f} GiB\n"
        f"Build time: {elapsed:.1f} seconds\n\n"
        f"{coverage}"
    )
    _offer_managed_cleanup()


def maybe_automatic_pack_build(confirm_existing: bool = False) -> None:
    """One automatic attempt per unchanged existing DB/source collection."""

    pack_root = get_program_root_path() / "user_files" / "fast_audio"
    try:
        if load_packed_only_state(pack_root) is not None:
            return
    except PackedOnlyStateError as error:
        showWarning(f"Could not inspect packed-only state:\n\n{error}")
        return
    if protected_cleanup_state_exists(pack_root):
        showWarning(
            "Automatic audio migration is blocked because protected cleanup "
            "quarantine exists without a valid journal."
        )
        return
    runtime = get_runtime()
    if runtime is None or not _claim_job("processing existing audio", quiet=True):
        return
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

    if confirm_existing:
        try:
            existing = inspect_installed_collection(get_db_path(), ALL_SOURCES)
            previous_status = automatic_pack_build_state(pack_root, fingerprint)
            resuming = previous_status in ("paused", "started")
            title = "Resume audio migration" if resuming else "Existing audio found"
            question = (
                "A previous migration was paused or interrupted. Resume from its "
                "saved checkpoint now?"
                if resuming
                else "Create the fast audio pack now?"
            )
            proceed = askUser(
                "Local Audio Server found your existing collection automatically.\n\n"
                f"Mappings: {existing['rows']:,}\n"
                f"Source folders found: {existing['source_folders']:,} of "
                f"{existing['database_sources']:,}\n\n"
                f"{question} No picker or file copy is needed. Nothing will be "
                "moved or deleted, and you can pause/resume the build.\n\n"
                "Choosing No stops this prompt for this database/source setup; start "
                "it later from Tools → Local Audio Server → Build/rebuild fast "
                "desktop audio pack.",
                title=title,
                defaultno=False,
            )
        except Exception as error:
            try:
                finish_automatic_pack_build(
                    pack_root, fingerprint, "failed", str(error)
                )
            finally:
                _finish_job()
            showWarning(f"Could not validate the detected audio collection:\n\n{error}")
            return
        if not proceed:
            try:
                finish_automatic_pack_build(pack_root, fingerprint, "declined")
            finally:
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


def _offer_managed_cleanup() -> None:
    info = inspect_managed_cleanup(
        get_db_path(),
        get_program_root_path() / "user_files" / "fast_audio",
        ALL_SOURCES,
        get_program_root_path(),
    )
    if info.get("eligible") and info.get("status") == "ready":
        remove_loose_audio_operation(info=info, quiet_unavailable=True)


def _cleanup_failure(error: Exception, progress, *, restoring: bool = False) -> None:
    pack_root = get_program_root_path() / "user_files" / "fast_audio"
    try:
        progress.close()
    finally:
        _finish_job()
    try:
        state = load_packed_only_state(pack_root)
    except PackedOnlyStateError:
        state = None
    if isinstance(error, PackBuildCancelled):
        if restoring:
            showInfo(
                f"{error.progress.message}\n\n"
                "Protected staging and the packed-only guard were kept. Resume "
                "from Tools → Local Audio Server → Restore/verify loose audio "
                "originals…"
            )
            return
        if state is None:
            showInfo(
                "Cleanup was cancelled before any originals were moved.\n\n"
                "The active pack and every loose source file remain usable."
            )
            return
        moved = int(state.get("moved_files", 0))
        total = int(state.get("total_files", 0))
        showInfo(
            f"Cleanup paused safely after securing {moved:,}/{total:,} files in "
            "protected local quarantine.\n\nResume from Tools → Local Audio "
            "Server → Move verified loose audio to Trash…"
        )
        return
    if restoring:
        showWarning(_restore_stop_message(error))
        return
    showWarning(_cleanup_stop_message(error))


# The exact Tools menu paths the user must follow to resume; kept verbatim so the
# copy matches the menu labels documented in the README and built in init_gui().
_CLEANUP_MENU_PATH = "Tools → Local Audio Server → Move verified loose audio to Trash…"
_RESTORE_MENU_PATH = (
    "Tools → Local Audio Server → Restore/verify loose audio originals…"
)


def _with_detail(guidance: str, error: Exception) -> str:
    """Append the raw exception as optional technical detail after the guidance.

    The friendly guidance always leads; the invariant text (including any filename)
    is preserved for diagnostics but never becomes the headline.
    """

    return f"{guidance}\n\nTechnical detail: {error}"


def _cleanup_stop_message(error: Exception) -> str:
    """Build calm, actionable copy for a cleanup safety stop.

    Every branch leads with what happened in plain language, states prominently that
    nothing was permanently deleted, and names the exact menu action to retry after
    the folder is stable. The raw exception is preserved as technical detail.
    """

    if isinstance(error, LooseAudioChangedError):
        return _with_detail(
            "Cleanup stopped because an audio file changed.\n\n"
            "Nothing was permanently deleted. The verified pack and your loose "
            "originals were all kept.\n\n"
            "Another program, sync service, or file copy may be changing the audio "
            "folder. Close or pause it so the files stay still, then run "
            f"{_CLEANUP_MENU_PATH} again.",
            error,
        )
    if isinstance(error, PackMismatchError):
        return _with_detail(
            "Cleanup stopped because the audio no longer matches the verified pack.\n\n"
            "Nothing was permanently deleted, and the loose original was kept.\n\n"
            "Rebuild the fast pack from the current files, test playback, then run "
            f"{_CLEANUP_MENU_PATH} again.",
            error,
        )
    return _with_detail(
        "Cleanup stopped to protect your audio.\n\n"
        "Nothing was permanently deleted. If loose-audio-originals-v1 is still in "
        "user_files/fast_audio, run "
        f"{_CLEANUP_MENU_PATH} again. If it already reached Trash, restore it and "
        f"use {_RESTORE_MENU_PATH}.",
        error,
    )


def _restore_stop_message(error: Exception) -> str:
    """Build calm, actionable copy for a restore/verify safety stop."""

    if isinstance(error, LooseAudioChangedError):
        return _with_detail(
            "Restore stopped because an audio file changed.\n\n"
            "No existing file was overwritten, and the packed-only safeguard is still "
            "active.\n\n"
            "Another program, sync service, or file copy may be changing the audio "
            "folder. Close or pause it, then run "
            f"{_RESTORE_MENU_PATH} again.",
            error,
        )
    return _with_detail(
        "Restore stopped to protect your audio.\n\n"
        "No existing file was overwritten, and the packed-only safeguard is still "
        "active.\n\n"
        "Fix or restore the protected cleanup folder, then run "
        f"{_RESTORE_MENU_PATH} again.",
        error,
    )


def remove_loose_audio_operation(
    _checked: bool = False,
    *,
    info: Optional[dict] = None,
    quiet_unavailable: bool = False,
) -> None:
    pack_root = get_program_root_path() / "user_files" / "fast_audio"
    if info is None:
        info = inspect_managed_cleanup(
            get_db_path(), pack_root, ALL_SOURCES, get_program_root_path()
        )
    if not info.get("eligible"):
        if not quiet_unavailable:
            showInfo(info.get("reason") or "No verified managed audio is ready to clean up.")
        return
    try:
        from send2trash import send2trash
    except ImportError:
        showWarning(
            "The operating-system Trash integration is unavailable. Nothing was "
            "removed; this add-on never falls back to permanent deletion."
        )
        return
    moved_files = int(info.get("moved_files", 0))
    moved_bytes = int(info.get("moved_bytes", 0))
    total_files = int(info.get("files", 0))
    total_bytes = int(info.get("bytes", 0))
    remaining_files = max(0, total_files - moved_files)
    remaining_bytes = max(0, total_bytes - moved_bytes)
    gib = remaining_bytes / (1024 ** 3)
    resume = info.get("status") != "ready"
    verb = "Resume moving" if resume else "Move"
    if not askUser(
        f"{verb} {remaining_files:,} database-referenced loose audio "
        f"files (about {gib:.2f} GiB) to the operating system Trash?\n\n"
        + (
            f"A previous run already moved {moved_files:,}/{total_files:,} files.\n\n"
            if resume
            else ""
        )
        +
        "Before moving anything, the add-on freshly verifies SHA-256 for the "
        "pack/index, checks every database row, and byte-compares each loose file "
        "with its packed copy. Only referenced audio inside this add-on's own "
        "user_files is eligible; external folders, metadata, config, entries.db, "
        "unreferenced files, and links are never removed.\n\n"
        "After cleanup, database regeneration and pack rebuilding stay disabled "
        "until you restore and verify originals. Keeping originals is the safest "
        "choice, and No is the default.",
        title="Move verified loose audio to Trash",
        defaultno=True,
    ):
        return
    if not _claim_job("verifying and moving loose audio to Trash"):
        return
    from .progress_ui import OperationProgress

    progress = OperationProgress(
        "Local Audio Server",
        "Freshly verifying the active pack before cleanup…",
    )

    def action() -> dict:
        return trash_verified_loose_audio(
            get_db_path(),
            pack_root,
            ALL_SOURCES,
            get_program_root_path(),
            trash=send2trash,
            progress_callback=progress.emit_progress,
            should_cancel=progress.cancelled,
        )

    def success(result: dict) -> None:
        try:
            progress.close()
        finally:
            _finish_job()
        showInfo(
            "Verified loose audio was moved to the operating system Trash.\n\n"
            f"Files: {result['files']:,}\n"
            f"Space represented: {result['bytes'] / (1024 ** 3):.2f} GiB\n"
            f"Active pack version: {result['version']}\n\n"
            "entries.db, config, source metadata, unreferenced files, and the fast "
            "pack were retained. Restore and verify originals before regenerating "
            "or rebuilding. Test playback first; space is reclaimed only after you "
            "empty the operating system Trash."
        )

    operation = QueryOp(parent=mw, op=lambda _: action(), success=success)
    operation = _with_failure(
        operation, lambda error: _cleanup_failure(error, progress)
    )
    operation.run_in_background()


def restore_loose_audio_operation(_checked: bool = False) -> None:
    pack_root = get_program_root_path() / "user_files" / "fast_audio"
    try:
        state = load_packed_only_state(pack_root)
    except PackedOnlyStateError as error:
        showWarning(str(error))
        return
    if state is None:
        showInfo("This collection is not in packed-only mode.")
        return
    if not _claim_job("restoring loose audio originals"):
        return
    from .progress_ui import OperationProgress

    progress = OperationProgress(
        "Local Audio Server",
        "Restoring quarantined loose audio to its source folders…",
    )

    def action() -> dict:
        result = restore_quarantined_audio(
            get_db_path(),
            pack_root,
            ALL_SOURCES,
            get_program_root_path(),
            progress_callback=progress.emit_progress,
            should_cancel=progress.cancelled,
        )
        runtime = get_runtime()
        if runtime is not None:
            try:
                result["runtime_pack_active"] = runtime.store.reload_pack()
            except Exception as error:
                result["runtime_reload_error"] = str(error)
        return result

    def success(result: dict) -> None:
        try:
            progress.close()
        finally:
            _finish_job()
        showInfo(
            "Loose audio originals are restored and packed-only safeguards are "
            "cleared.\n\n"
            f"Database rows checked: {result['rows']:,}\n"
            f"Files moved from restored staging: {result['files']:,}\n\n"
            "Database regeneration and pack rebuilding are available again."
            + (
                "\n\nThe previous database/pack binding failed verification, so "
                "the fast pack was disabled. Regenerate the database if needed, "
                "then rebuild the pack from the independently verified originals."
                if not result.get("pack_valid", True)
                else ""
            )
            + (
                "\n\nRestart Anki to refresh the server: "
                + result["runtime_reload_error"]
                if result.get("runtime_reload_error")
                else ""
            )
        )

    operation = QueryOp(parent=mw, op=lambda _: action(), success=success)
    operation = _with_failure(
        operation, lambda error: _cleanup_failure(error, progress, restoring=True)
    )
    operation.run_in_background()


def import_existing_audio_operation() -> None:
    if _packed_only_blocks_maintenance("import another audio collection"):
        return
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
    cleanup = QAction("Move verified loose audio to Trash…", mw)
    qconnect(cleanup.triggered, remove_loose_audio_operation)
    menu.addAction(cleanup)
    restore = QAction("Restore/verify loose audio originals…", mw)
    qconnect(restore.triggered, restore_loose_audio_operation)
    menu.addAction(restore)
    statistics = QAction("Show statistics", mw)
    qconnect(statistics.triggered, show_stats)
    menu.addAction(statistics)
    gui_hooks.main_window_did_init.append(attempt_init_db_gui)
    _gui_initialized = True
