from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
PACKAGE = "_local_audio_fast_cleanup_test_addon"
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

cleanup = importlib.import_module(f"{PACKAGE}.cleanup")
db_utils = importlib.import_module(f"{PACKAGE}.db_utils")
fast_pack = importlib.import_module(f"{PACKAGE}.fast_pack")
fast_store = importlib.import_module(f"{PACKAGE}.fast_store")


@dataclass
class FakeData:
    id: str
    display: str


class FakeSource:
    def __init__(self, source_id: str, root: Path) -> None:
        self.data = FakeData(source_id, "Source")
        self.root = root

    def get_media_dir_path(self) -> Path:
        return self.root


class CleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.program_root = self.root / "1045800357"
        self.user_files = self.program_root / "user_files"
        self.source_root = self.user_files / "fixture_files"
        self.source_root.mkdir(parents=True)
        self.first = self.source_root / "first.opus"
        self.second = self.source_root / "second.mp3"
        self.first_bytes = b"OggS-cleanup-first"
        self.second_bytes = b"ID3-cleanup-second"
        self.first.write_bytes(self.first_bytes)
        self.second.write_bytes(self.second_bytes)
        self.unreferenced = self.source_root / "keep.opus"
        self.unreferenced.write_bytes(b"unreferenced")
        self.metadata = self.source_root / "source_meta.json"
        self.metadata.write_text('{"type":"fixture"}', encoding="utf-8")
        self.config = self.user_files / "config.json"
        self.config.write_text("{}", encoding="utf-8")
        self.db_path = self.user_files / "entries.db"
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "CREATE TABLE entries (id INTEGER PRIMARY KEY NOT NULL, "
                "expression TEXT NOT NULL, reading TEXT, source TEXT NOT NULL, "
                "speaker TEXT, display TEXT, file TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO entries VALUES (?,?,?,?,?,?,?)",
                (
                    (1, "one", None, "fixture", None, None, "first.opus"),
                    (2, "uno", None, "fixture", None, None, "first.opus"),
                    (3, "two", None, "fixture", None, None, "second.mp3"),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        self.pack_root = self.user_files / "fast_audio"
        self.sources = {"fixture": FakeSource("fixture", self.source_root)}
        self.build = fast_pack.build_audio_pack(
            self.db_path, self.pack_root, self.sources, workers=1
        )
        self.trash_root = self.root / "trash"
        self.trash_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def trash(self, paths: list[str]) -> None:
        for index, value in enumerate(paths):
            path = Path(value)
            path.rename(self.trash_root / f"{len(list(self.trash_root.iterdir()))}-{index}-{path.name}")

    def test_cleanup_moves_only_exact_referenced_audio_and_keeps_serving_pack(self) -> None:
        offer = cleanup.inspect_managed_cleanup(
            self.db_path,
            self.pack_root,
            self.sources,
            self.program_root,
        )
        self.assertTrue(offer["eligible"])
        self.assertEqual(offer["files"], 2)

        result = cleanup.trash_verified_loose_audio(
            self.db_path,
            self.pack_root,
            self.sources,
            self.program_root,
            self.trash,
            batch_size=1,
        )

        self.assertEqual(result["files"], 2)
        self.assertFalse(self.first.exists())
        self.assertFalse(self.second.exists())
        self.assertTrue(self.unreferenced.is_file())
        self.assertTrue(self.metadata.is_file())
        self.assertTrue(self.config.is_file())
        self.assertTrue(self.db_path.is_file())
        self.assertTrue((self.pack_root / "active.json").is_file())
        marker = cleanup.load_packed_only_state(self.pack_root)
        self.assertIsNotNone(marker)
        assert marker is not None
        self.assertEqual(marker["status"], "completed")
        with self.assertRaisesRegex(RuntimeError, "packed-only"):
            fast_pack.build_audio_pack(
                self.db_path, self.pack_root, self.sources, workers=1
            )
        with patch.object(db_utils, "get_db_path", return_value=self.db_path):
            with self.assertRaisesRegex(RuntimeError, "packed-only"):
                db_utils.init_db(sources={})

        store = fast_store.LookupStore(
            self.db_path,
            self.sources,
            "http://127.0.0.1:5050",
            self.pack_root,
        )
        replacement = self.user_files / "replacement.db"
        shutil.copy2(self.db_path, replacement)
        try:
            with self.assertRaisesRegex(RuntimeError, "packed-only"):
                store.publish_database(replacement)
            self.assertTrue(self.db_path.is_file())
            self.assertTrue(replacement.is_file())
        finally:
            store.close()

        pack = fast_pack.AudioPack.open_active(self.pack_root, self.db_path)
        self.assertIsNotNone(pack)
        assert pack is not None
        try:
            first = pack.get(1)
            second = pack.get(3)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            first_view = pack.view(first, 0, first.length)
            second_view = pack.view(second, 0, second.length)
            try:
                self.assertEqual(bytes(first_view), self.first_bytes)
                self.assertEqual(bytes(second_view), self.second_bytes)
            finally:
                first_view.release()
                second_view.release()
        finally:
            pack.close()

    def test_cancel_during_fresh_verification_moves_nothing_and_writes_no_marker(self) -> None:
        with self.assertRaises(fast_pack.PackBuildCancelled):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                self.trash,
                should_cancel=lambda: True,
            )
        self.assertTrue(self.first.is_file())
        self.assertTrue(self.second.is_file())
        self.assertIsNone(cleanup.load_packed_only_state(self.pack_root))

    def test_immediate_cancel_on_resume_never_loses_marker_or_staged_file(self) -> None:
        cancel = {"value": False}

        def progress(value) -> None:
            if value.stage == "cleanup-staging" and value.current >= 1:
                cancel["value"] = True

        with self.assertRaises(fast_pack.PackBuildCancelled):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                self.trash,
                progress_callback=progress,
                should_cancel=lambda: cancel["value"],
                batch_size=1,
            )
        first_marker = cleanup.load_packed_only_state(self.pack_root)
        assert first_marker is not None
        self.assertGreaterEqual(first_marker["moved_files"], 1)
        cancel["value"] = True
        with self.assertRaises(fast_pack.PackBuildCancelled):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                self.trash,
                should_cancel=lambda: cancel["value"],
            )
        second_marker = cleanup.load_packed_only_state(self.pack_root)
        self.assertIsNotNone(second_marker)
        self.assertTrue(any((self.pack_root / cleanup.STAGING_DIR_NAME).rglob("*.opus")))

    def test_cancel_after_failed_quarantine_removes_marker_only_with_empty_tree(self) -> None:
        original_replace = cleanup.os.replace

        def fail_first_audio_move(source, destination) -> None:
            if Path(source) == self.first and cleanup.STAGING_DIR_NAME in Path(
                destination
            ).parts:
                raise OSError("simulated quarantine rename failure")
            original_replace(source, destination)

        with patch.object(cleanup.os, "replace", side_effect=fail_first_audio_move):
            with self.assertRaisesRegex(cleanup.CleanupSafetyError, "quarantine"):
                cleanup.trash_verified_loose_audio(
                    self.db_path,
                    self.pack_root,
                    self.sources,
                    self.program_root,
                    self.trash,
                )
        self.assertIsNotNone(cleanup.load_packed_only_state(self.pack_root))
        with self.assertRaises(fast_pack.PackBuildCancelled):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                self.trash,
                should_cancel=lambda: True,
            )
        marker = cleanup.load_packed_only_state(self.pack_root)
        stage_exists = os.path.lexists(self.pack_root / cleanup.STAGING_DIR_NAME)
        self.assertEqual(marker is None, not stage_exists)
        self.assertTrue(self.first.is_file())
        self.assertTrue(self.second.is_file())

    def test_changed_loose_bytes_are_never_trashed(self) -> None:
        self.first.write_bytes(b"OggS-cleanup-WRONG")
        self.assertEqual(len(self.first.read_bytes()), len(self.first_bytes))
        with self.assertRaisesRegex(
            cleanup.CleanupSafetyError, "no longer match the verified pack"
        ):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                self.trash,
            )
        self.assertTrue(self.first.is_file())
        self.assertTrue(self.second.is_file())

    def test_normalized_database_path_aliases_block_cleanup(self) -> None:
        nested = self.source_root / "nested"
        nested.mkdir()
        aliased = nested / "alias.opus"
        aliased.write_bytes(b"OggS-alias")
        with sqlite3.connect(self.db_path) as connection:
            connection.executemany(
                "INSERT INTO entries VALUES (?,?,?,?,?,?,?)",
                (
                    (4, "alias", None, "fixture", None, None, "nested/alias.opus"),
                    (5, "alias", None, "fixture", None, None, "nested\\alias.opus"),
                ),
            )
            connection.commit()
        fast_pack.build_audio_pack(
            self.db_path, self.pack_root, self.sources, workers=1
        )
        calls = []
        with self.assertRaisesRegex(cleanup.CleanupSafetyError, "ambiguous mappings"):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                lambda paths: calls.append(paths),
            )
        self.assertEqual(calls, [])
        self.assertTrue(aliased.is_file())
        self.assertTrue(self.first.is_file())

    def test_corrupt_pack_is_rejected_before_any_trash_call(self) -> None:
        active = json.loads((self.pack_root / "active.json").read_text(encoding="utf-8"))
        pack_path = self.pack_root / active["pack"]
        payload = bytearray(pack_path.read_bytes())
        payload[-1] ^= 0xFF
        pack_path.write_bytes(payload)
        calls = []
        with self.assertRaisesRegex(cleanup.CleanupSafetyError, "SHA-256"):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                lambda paths: calls.append(paths),
            )
        self.assertEqual(calls, [])
        self.assertTrue(self.first.is_file())
        self.assertIsNone(cleanup.load_packed_only_state(self.pack_root))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_external_symlink_pack_is_never_accepted_as_the_sole_copy(self) -> None:
        active = json.loads((self.pack_root / "active.json").read_text(encoding="utf-8"))
        pack_path = self.pack_root / active["pack"]
        outside = self.root / "outside.pack"
        pack_path.rename(outside)
        try:
            pack_path.symlink_to(outside)
        except OSError as error:
            outside.rename(pack_path)
            self.skipTest(f"symlink creation unavailable: {error}")
        with self.assertRaisesRegex(cleanup.CleanupSafetyError, "symbolic|regular managed"):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                self.trash,
            )
        self.assertTrue(self.first.is_file())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_external_symlink_database_is_never_accepted_as_the_sole_copy(self) -> None:
        outside = self.root / "outside.db"
        self.db_path.rename(outside)
        try:
            self.db_path.symlink_to(outside)
        except OSError as error:
            outside.rename(self.db_path)
            self.skipTest(f"symlink creation unavailable: {error}")
        with self.assertRaisesRegex(cleanup.CleanupSafetyError, "symbolic|private regular"):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                self.trash,
            )
        self.assertTrue(self.first.is_file())

    def test_external_source_root_is_never_eligible(self) -> None:
        external = self.root / "external"
        shutil.copytree(self.source_root, external)
        external_sources = {"fixture": FakeSource("fixture", external)}
        offer = cleanup.inspect_managed_cleanup(
            self.db_path,
            self.pack_root,
            external_sources,
            self.program_root,
        )
        self.assertFalse(offer["eligible"])
        self.assertIn("outside", offer["reason"])
        with self.assertRaisesRegex(cleanup.CleanupSafetyError, "outside"):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                external_sources,
                self.program_root,
                self.trash,
            )

    def test_trash_failure_keeps_quarantine_and_resumes_idempotently(self) -> None:
        calls = 0

        def partial(paths: list[str]) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("simulated Trash interruption")

        with self.assertRaisesRegex(cleanup.CleanupSafetyError, "did not complete"):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                partial,
                batch_size=1,
            )
        self.assertEqual(calls, 1)
        marker = cleanup.load_packed_only_state(self.pack_root)
        self.assertIsNotNone(marker)
        assert marker is not None
        self.assertEqual(marker["status"], "trash-failed")
        self.assertEqual(marker["moved_files"], 2)
        self.assertTrue((self.pack_root / cleanup.STAGING_DIR_NAME).is_dir())

        result = cleanup.trash_verified_loose_audio(
            self.db_path,
            self.pack_root,
            self.sources,
            self.program_root,
            self.trash,
            batch_size=1,
        )
        self.assertEqual(result["status"], "completed")
        self.assertFalse(self.first.exists())
        self.assertFalse(self.second.exists())
        final = cleanup.load_packed_only_state(self.pack_root)
        assert final is not None
        self.assertEqual(final["status"], "completed")

    def test_unexpected_file_in_quarantine_is_never_sent_to_trash(self) -> None:
        calls = []
        injected = {"value": False}

        def inject_after_staging(value) -> None:
            if (
                value.stage == "cleanup-staging"
                and value.current == 2
                and not injected["value"]
            ):
                extra = self.pack_root / cleanup.STAGING_DIR_NAME / "not-referenced.txt"
                extra.write_text("keep me", encoding="utf-8")
                injected["value"] = True

        with self.assertRaisesRegex(cleanup.CleanupSafetyError, "unexpected"):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                lambda paths: calls.append(paths),
                progress_callback=inject_after_staging,
            )
        self.assertEqual(calls, [])
        self.assertTrue(
            (self.pack_root / cleanup.STAGING_DIR_NAME / "not-referenced.txt").is_file()
        )
        self.assertIsNotNone(cleanup.load_packed_only_state(self.pack_root))

    def test_preexisting_unjournaled_staging_is_never_adopted_or_trashed(self) -> None:
        staging = self.pack_root / cleanup.STAGING_DIR_NAME
        staging.mkdir()
        unrelated = staging / "unrelated.txt"
        unrelated.write_text("do not trash", encoding="utf-8")
        calls = []
        with self.assertRaisesRegex(cleanup.CleanupSafetyError, "without a journal"):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                lambda paths: calls.append(paths),
            )
        self.assertEqual(calls, [])
        self.assertTrue(unrelated.is_file())
        self.assertTrue(self.first.is_file())
        with self.assertRaisesRegex(RuntimeError, "packed-only"):
            fast_pack.build_audio_pack(
                self.db_path, self.pack_root, self.sources, workers=1
            )
        with patch.object(db_utils, "get_db_path", return_value=self.db_path):
            with self.assertRaisesRegex(RuntimeError, "packed-only"):
                db_utils.init_db(sources={})

    def test_post_trash_pack_corruption_requires_recovery_and_never_reports_complete(self) -> None:
        active = json.loads((self.pack_root / "active.json").read_text(encoding="utf-8"))
        pack_path = self.pack_root / active["pack"]

        def corrupt_after_trash(paths: list[str]) -> None:
            self.trash(paths)
            payload = bytearray(pack_path.read_bytes())
            payload[-1] ^= 0xFF
            pack_path.write_bytes(payload)

        with self.assertRaisesRegex(cleanup.CleanupSafetyError, "restore"):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                corrupt_after_trash,
            )
        marker = cleanup.load_packed_only_state(self.pack_root)
        assert marker is not None
        self.assertEqual(marker["status"], "recovery-required")

    def test_crash_after_trash_before_completion_requires_restore(self) -> None:
        original_atomic = cleanup._atomic_json

        def fail_completed(path: Path, value: dict) -> None:
            if value.get("status") == "completed":
                raise OSError("simulated crash before completed marker")
            original_atomic(path, value)

        with patch.object(cleanup, "_atomic_json", side_effect=fail_completed):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                cleanup.trash_verified_loose_audio(
                    self.db_path,
                    self.pack_root,
                    self.sources,
                    self.program_root,
                    self.trash,
                )
        self.assertFalse((self.pack_root / cleanup.STAGING_DIR_NAME).exists())
        with self.assertRaisesRegex(cleanup.CleanupSafetyError, "restore"):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                self.trash,
            )
        marker = cleanup.load_packed_only_state(self.pack_root)
        assert marker is not None
        self.assertEqual(marker["status"], "recovery-required")

    def test_trash_receives_only_quarantine_not_swappable_source_paths(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        victim = outside / "first.opus"
        victim.write_bytes(b"do-not-touch")
        backup = self.user_files / "fixture-backup"
        received = []

        def swap_source_then_trash(paths: list[str]) -> None:
            received.extend(paths)
            self.source_root.rename(backup)
            try:
                self.source_root.symlink_to(outside, target_is_directory=True)
            except OSError:
                backup.rename(self.source_root)
                raise
            self.trash(paths)

        try:
            result = cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                swap_source_then_trash,
            )
        except OSError as error:
            self.skipTest(f"directory symlinks unavailable: {error}")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(received, [str(self.pack_root / cleanup.STAGING_DIR_NAME)])
        self.assertEqual(victim.read_bytes(), b"do-not-touch")
        self.assertTrue((backup / "keep.opus").is_file())

    def test_restored_quarantine_returns_originals_and_clears_guard(self) -> None:
        cleanup.trash_verified_loose_audio(
            self.db_path,
            self.pack_root,
            self.sources,
            self.program_root,
            self.trash,
        )
        trashed_stage = next(
            path
            for path in self.trash_root.iterdir()
            if path.name.endswith(cleanup.STAGING_DIR_NAME)
        )
        trashed_stage.rename(self.pack_root / cleanup.STAGING_DIR_NAME)
        restored = cleanup.restore_quarantined_audio(
            self.db_path,
            self.pack_root,
            self.sources,
            self.program_root,
        )
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(self.first.read_bytes(), self.first_bytes)
        self.assertEqual(self.second.read_bytes(), self.second_bytes)
        self.assertIsNone(cleanup.load_packed_only_state(self.pack_root))
        rebuilt = fast_pack.build_audio_pack(
            self.db_path, self.pack_root, self.sources, workers=1
        )
        self.assertEqual(rebuilt["valid_rows"], 3)

    def test_modified_restored_quarantine_never_clears_guard(self) -> None:
        cleanup.trash_verified_loose_audio(
            self.db_path,
            self.pack_root,
            self.sources,
            self.program_root,
            self.trash,
        )
        trashed_stage = next(
            path
            for path in self.trash_root.iterdir()
            if path.name.endswith(cleanup.STAGING_DIR_NAME)
        )
        first_staged = next(trashed_stage.rglob("first.opus"))
        payload = bytearray(first_staged.read_bytes())
        payload[-1] ^= 0xFF
        first_staged.write_bytes(payload)
        trashed_stage.rename(self.pack_root / cleanup.STAGING_DIR_NAME)
        with self.assertRaisesRegex(cleanup.CleanupSafetyError, "SHA-256|bytes|match"):
            cleanup.restore_quarantined_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
            )
        self.assertIsNotNone(cleanup.load_packed_only_state(self.pack_root))
        self.assertFalse(self.first.exists())

    def test_recovery_inventory_restores_after_pack_and_database_damage(self) -> None:
        cleanup.trash_verified_loose_audio(
            self.db_path,
            self.pack_root,
            self.sources,
            self.program_root,
            self.trash,
        )
        active = json.loads((self.pack_root / "active.json").read_text(encoding="utf-8"))
        pack_path = self.pack_root / active["pack"]
        payload = bytearray(pack_path.read_bytes())
        payload[-1] ^= 0xFF
        pack_path.write_bytes(payload)
        self.db_path.write_bytes(b"not a sqlite database")
        trashed_stage = next(
            path
            for path in self.trash_root.iterdir()
            if path.name.endswith(cleanup.STAGING_DIR_NAME)
        )
        trashed_stage.rename(self.pack_root / cleanup.STAGING_DIR_NAME)

        restored = cleanup.restore_quarantined_audio(
            self.db_path,
            self.pack_root,
            self.sources,
            self.program_root,
        )

        self.assertEqual(restored["status"], "restored")
        self.assertFalse(restored["pack_valid"])
        self.assertEqual(self.first.read_bytes(), self.first_bytes)
        self.assertEqual(self.second.read_bytes(), self.second_bytes)
        self.assertIsNone(cleanup.load_packed_only_state(self.pack_root))
        self.assertFalse((self.pack_root / "active.json").exists())

    def test_recovery_disables_pack_for_same_stat_database_corruption(self) -> None:
        cleanup.trash_verified_loose_audio(
            self.db_path,
            self.pack_root,
            self.sources,
            self.program_root,
            self.trash,
        )
        before = self.db_path.stat()
        payload = bytearray(self.db_path.read_bytes())
        position = payload.find(b"first.opus")
        self.assertGreaterEqual(position, 0)
        payload[position] ^= 1
        self.db_path.write_bytes(payload)
        os.utime(
            self.db_path,
            ns=(before.st_atime_ns, before.st_mtime_ns),
        )
        self.assertEqual(self.db_path.stat().st_size, before.st_size)
        trashed_stage = next(
            path
            for path in self.trash_root.iterdir()
            if path.name.endswith(cleanup.STAGING_DIR_NAME)
        )
        trashed_stage.rename(self.pack_root / cleanup.STAGING_DIR_NAME)

        restored = cleanup.restore_quarantined_audio(
            self.db_path,
            self.pack_root,
            self.sources,
            self.program_root,
        )

        self.assertFalse(restored["pack_valid"])
        self.assertFalse((self.pack_root / "active.json").exists())
        self.assertEqual(self.first.read_bytes(), self.first_bytes)

    def test_corrupt_marker_fails_closed(self) -> None:
        marker = self.pack_root / cleanup.PACKED_ONLY_MARKER_NAME
        marker.write_text("{broken", encoding="utf-8")
        with self.assertRaises(cleanup.PackedOnlyStateError):
            cleanup.load_packed_only_state(self.pack_root)
        with self.assertRaisesRegex(RuntimeError, "packed-only"):
            fast_pack.build_audio_pack(
                self.db_path, self.pack_root, self.sources, workers=1
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_dangling_marker_link_fails_closed(self) -> None:
        marker = self.pack_root / cleanup.PACKED_ONLY_MARKER_NAME
        try:
            marker.symlink_to(self.root / "missing-marker.json")
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        with self.assertRaises(cleanup.PackedOnlyStateError):
            cleanup.load_packed_only_state(self.pack_root)
        with self.assertRaisesRegex(RuntimeError, "packed-only"):
            fast_pack.build_audio_pack(
                self.db_path, self.pack_root, self.sources, workers=1
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_symlink_candidate_is_rejected(self) -> None:
        target = self.root / "outside.opus"
        target.write_bytes(self.first_bytes)
        self.first.unlink()
        try:
            self.first.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        with self.assertRaisesRegex(cleanup.CleanupSafetyError, "symbolic links"):
            cleanup.trash_verified_loose_audio(
                self.db_path,
                self.pack_root,
                self.sources,
                self.program_root,
                self.trash,
            )
        self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
