from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
PACKAGE = "_local_audio_fast_resume_addon"
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


class FakeSource:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get_media_dir_path(self) -> Path:
        return self.root


class PackResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        payloads = {
            "01.opus": b"OggS-one",
            "02.opus": b"OggS-two",
            "03.opus": b"OggS-one",
            "04.opus": b"OggS-four-old",
            "05.opus": b"OggS-two",
        }
        for filename, payload in payloads.items():
            (self.media / filename).write_bytes(payload)
        self.db_path = self.root / "entries.db"
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "CREATE TABLE entries (id INTEGER PRIMARY KEY NOT NULL, "
                "expression TEXT NOT NULL, reading TEXT, source TEXT NOT NULL, "
                "speaker TEXT, display TEXT, file TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO entries VALUES (?,?,NULL,'s1',NULL,NULL,?)",
                [(index, f"word-{index}", f"0{index}.opus") for index in range(1, 7)],
            )
            connection.commit()
        finally:
            connection.close()
        self.sources = {"s1": FakeSource(self.media)}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cancel_preserves_active_pack_and_resume_matches_clean_build(self) -> None:
        pack_root = self.root / "pack"
        original = fast_pack.build_audio_pack(
            self.db_path, pack_root, self.sources, workers=2
        )
        active_path = pack_root / "active.json"
        active_before = active_path.read_bytes()
        (self.media / "04.opus").write_bytes(b"OggS-four-new")

        cancel = {"requested": False}

        def on_progress(progress) -> None:
            if progress.stage == "packing" and progress.current >= 3:
                cancel["requested"] = True

        with patch.object(fast_pack, "PROGRESS_INTERVAL_SECONDS", 0):
            with self.assertRaises(fast_pack.PackBuildCancelled):
                fast_pack.build_audio_pack(
                    self.db_path,
                    pack_root,
                    self.sources,
                    workers=2,
                    progress_callback=on_progress,
                    should_cancel=lambda: cancel["requested"],
                )

        self.assertEqual(active_path.read_bytes(), active_before)
        stages = list((pack_root / "versions").glob(".building-*"))
        self.assertEqual(len(stages), 1)
        checkpoint = json.loads(
            (stages[0] / fast_pack.CHECKPOINT_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(checkpoint["processed_rows"], 3)
        self.assertEqual(checkpoint["phase"], "packing")

        # Simulate a hard exit after writes advanced beyond the durable checkpoint.
        # Resume must truncate the pack and clear a stale row when that source is missing.
        junk = b"OggS-uncommitted"
        with (stages[0] / "audio.pack").open("ab") as pack_file:
            pack_file.write(junk)
        with (stages[0] / "audio.idx").open("r+b") as index_file:
            index_file.seek(fast_pack.INDEX_HEADER.size + 6 * fast_pack.RECORD.size)
            index_file.write(
                fast_pack.RECORD.pack(
                    checkpoint["pack_bytes"],
                    len(junk),
                    4,
                    fast_pack.RECORD_VALID,
                )
            )

        checkpoint_before_resume_cancel = (
            stages[0] / fast_pack.CHECKPOINT_NAME
        ).read_bytes()
        with self.assertRaises(fast_pack.PackBuildCancelled):
            fast_pack.build_audio_pack(
                self.db_path,
                pack_root,
                self.sources,
                workers=2,
                should_cancel=lambda: True,
            )
        self.assertEqual(
            (stages[0] / fast_pack.CHECKPOINT_NAME).read_bytes(),
            checkpoint_before_resume_cancel,
        )

        read_filenames = []
        progress_updates = []
        original_read = fast_pack._read_group

        def recording_read(group, roots):
            read_filenames.append(group.filename)
            return original_read(group, roots)

        with patch.object(fast_pack, "_read_group", side_effect=recording_read), patch.object(
            fast_pack, "PROGRESS_INTERVAL_SECONDS", 0
        ):
            resumed = fast_pack.build_audio_pack(
                self.db_path,
                pack_root,
                self.sources,
                workers=2,
                progress_callback=progress_updates.append,
            )

        self.assertTrue(resumed["resumed"])
        self.assertNotEqual(resumed["version"], original["version"])
        self.assertEqual(read_filenames, ["04.opus", "05.opus", "06.opus"])
        packing_values = [
            item.current for item in progress_updates if item.stage == "packing"
        ]
        self.assertEqual(packing_values, sorted(packing_values))
        self.assertEqual(packing_values[-1], 6)

        clean_root = self.root / "clean-pack"
        clean = fast_pack.build_audio_pack(
            self.db_path, clean_root, self.sources, workers=2
        )
        self.assertEqual(resumed["version"], clean["version"])
        self.assertEqual(
            (pack_root / resumed["index"]).read_bytes(),
            (clean_root / clean["index"]).read_bytes(),
        )
        self.assertEqual(
            (pack_root / resumed["pack"]).read_bytes(),
            (clean_root / clean["pack"]).read_bytes(),
        )

    def test_processed_source_change_discards_checkpoint_and_rebuilds_cleanly(self) -> None:
        pack_root = self.root / "changed-source-pack"
        cancel = {"requested": False}

        def on_progress(progress) -> None:
            if progress.stage == "packing" and progress.current >= 3:
                cancel["requested"] = True

        with patch.object(fast_pack, "PROGRESS_INTERVAL_SECONDS", 0):
            with self.assertRaises(fast_pack.PackBuildCancelled):
                fast_pack.build_audio_pack(
                    self.db_path,
                    pack_root,
                    self.sources,
                    workers=2,
                    progress_callback=on_progress,
                    should_cancel=lambda: cancel["requested"],
                )

        (self.media / "01.opus").write_bytes(b"OggS-one-replaced")
        rebuilt = fast_pack.build_audio_pack(
            self.db_path, pack_root, self.sources, workers=2
        )
        self.assertFalse(rebuilt["resumed"])

        clean_root = self.root / "changed-source-clean"
        clean = fast_pack.build_audio_pack(
            self.db_path, clean_root, self.sources, workers=2
        )
        self.assertEqual(rebuilt["version"], clean["version"])
        self.assertEqual(
            (pack_root / rebuilt["index"]).read_bytes(),
            (clean_root / clean["index"]).read_bytes(),
        )
        self.assertEqual(
            (pack_root / rebuilt["pack"]).read_bytes(),
            (clean_root / clean["pack"]).read_bytes(),
        )

    def test_cross_process_lock_rejects_a_second_builder(self) -> None:
        pack_root = self.root / "locked-pack"
        with fast_pack._PackBuildLock(pack_root / fast_pack.BUILD_LOCK_NAME):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                fast_pack.build_audio_pack(
                    self.db_path, pack_root, self.sources, workers=2
                )


if __name__ == "__main__":
    unittest.main()
