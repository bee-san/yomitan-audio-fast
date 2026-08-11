from __future__ import annotations

import hashlib
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
PACKAGE = "_local_audio_fast_migration_addon"
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
migration = importlib.import_module(f"{PACKAGE}.migration")
audio_source = importlib.import_module(f"{PACKAGE}.source.audio_source")


class FakeSource:
    def __init__(self, data) -> None:
        self.data = data

    def get_media_dir_path(self) -> Path:
        return Path(self.data.media_dir)


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.installed = self.root / "installed"
        self.installed_user = self.installed / "user_files"
        self.installed_user.mkdir(parents=True)
        self.external = self.root / "old-addon"
        self.external_user = self.external / "user_files"
        self.external_source = self.external_user / "s1_files"
        self.external_source.mkdir(parents=True)
        self.audio_path = self.external_source / "reading - cat.opus"
        self.audio_path.write_bytes(b"OggS-original-must-remain")
        self.external_db = self.external_user / "entries.db"
        self._database(self.external_db, "s1")
        self.sources = {
            "s1": FakeSource(
                audio_source.AudioSourceData(
                    "s1", str(self.installed_user / "s1_files"), "Source"
                )
            )
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _database(path: Path, source_id: str) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE entries (id INTEGER PRIMARY KEY NOT NULL, "
                "expression TEXT NOT NULL, reading TEXT, source TEXT NOT NULL, "
                "speaker TEXT, display TEXT, file TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO entries VALUES (1,'cat','reading',?,NULL,NULL,'reading - cat.opus')",
                (source_id,),
            )
            connection.commit()
        finally:
            connection.close()

    def test_discovers_addon_user_files_and_single_source_folder(self) -> None:
        addon = migration.discover_existing_collection(self.external, self.sources)
        self.assertEqual(addon.database, self.external_db.resolve())
        self.assertEqual(addon.source_paths, {"s1": self.external_source.resolve()})
        source = migration.discover_existing_collection(self.external_source, self.sources)
        self.assertEqual(source.database, self.external_db.resolve())
        self.assertEqual(source.source_paths["s1"], self.external_source.resolve())

    def test_process_copies_valid_db_persists_absolute_source_and_never_moves_audio(self) -> None:
        destination_db = self.installed_user / "entries.db"
        config_path = self.installed_user / "config.json"
        config_path.write_text(
            json.dumps({"server": {"port": 5050}}), encoding="utf-8"
        )
        before_audio = hashlib.sha256(self.audio_path.read_bytes()).hexdigest()
        before_db = hashlib.sha256(self.external_db.read_bytes()).hexdigest()
        replaced_sources = []
        reloads = []

        def publisher(temporary: Path) -> None:
            os.replace(temporary, destination_db)

        def fake_pack(db_path, pack_root, sources, callback=None):
            self.assertEqual(db_path, destination_db)
            self.assertEqual(
                sources["s1"].get_media_dir_path(), self.external_source.resolve()
            )
            return {
                "version": "0123456789abcdef",
                "valid_rows": 1,
                "pack_bytes": len(self.audio_path.read_bytes()),
            }

        merged_config = {
            "server": {"port": 5050},
            "sources": [
                {"type": "jpod", "id": "s1", "path": "user_files/s1_files", "display": "Source"}
            ],
        }
        with patch.object(migration, "get_config_path", return_value=config_path), patch.object(
            migration, "read_config", return_value=merged_config
        ), patch.object(migration, "update_db_version"), patch.object(
            migration, "build_audio_pack", side_effect=fake_pack
        ):
            result = migration.process_existing_collection(
                self.external,
                self.sources,
                destination_db,
                self.installed_user / "fast_audio",
                publisher=publisher,
                replace_sources=lambda value: replaced_sources.append(value),
                reload_pack=lambda: reloads.append(True) or True,
            )

        self.assertEqual(result["database"]["mode"], "copied")
        self.assertEqual(result["database"]["rows"], 1)
        self.assertEqual(reloads, [True])
        self.assertEqual(len(replaced_sources), 1)
        self.assertEqual(hashlib.sha256(self.audio_path.read_bytes()).hexdigest(), before_audio)
        self.assertEqual(hashlib.sha256(self.external_db.read_bytes()).hexdigest(), before_db)
        self.assertTrue(self.audio_path.exists())
        self.assertTrue(self.external_db.exists())
        self.assertTrue(destination_db.exists())
        persisted = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["server"], {"port": 5050})
        self.assertEqual(
            persisted["sources"][0]["path"], str(self.external_source.resolve())
        )

    def test_invalid_unknown_database_source_is_rejected_before_config_write(self) -> None:
        unknown_db = self.external_user / "unknown.db"
        self._database(unknown_db, "unknown")
        with self.assertRaisesRegex(ValueError, "unconfigured sources"):
            migration.validate_entries_database(unknown_db, {"s1"})

    def test_automatic_build_marker_claims_once_per_unchanged_collection(self) -> None:
        pack_root = self.installed_user / "fast_audio"
        auto_sources = migration.remap_sources(
            self.sources, {"s1": self.external_source}
        )
        first = migration.claim_automatic_pack_build(
            self.external_db, pack_root, auto_sources, pack_is_active=False
        )
        self.assertIsNotNone(first)
        second = migration.claim_automatic_pack_build(
            self.external_db, pack_root, auto_sources, pack_is_active=False
        )
        self.assertIsNone(second)
        assert first is not None
        migration.finish_automatic_pack_build(pack_root, first, "completed")
        marker = json.loads(
            (pack_root / migration.AUTO_MARKER_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(marker, {"fingerprint": first, "status": "completed"})
        # A completed marker does not hide a subsequently deleted/invalid pack.
        retry = migration.claim_automatic_pack_build(
            self.external_db, pack_root, auto_sources, pack_is_active=False
        )
        self.assertEqual(retry, first)
        self.assertIsNone(
            migration.claim_automatic_pack_build(
                self.external_db, pack_root, auto_sources, pack_is_active=True
            )
        )

    def test_automatic_build_requires_a_supported_audio_file(self) -> None:
        config_only = self.root / "config-only"
        config_only.mkdir()
        (config_only / "index.json").write_text("{}", encoding="utf-8")
        sources = migration.remap_sources(self.sources, {"s1": config_only})
        pack_root = self.installed_user / "config-only-pack"
        self.assertIsNone(
            migration.claim_automatic_pack_build(
                self.external_db, pack_root, sources, pack_is_active=False
            )
        )
        self.assertFalse((pack_root / migration.AUTO_MARKER_NAME).exists())

    def test_pack_failure_leaves_database_config_runtime_and_globals_consistent(self) -> None:
        destination_db = self.installed_user / "entries.db"
        config_path = self.installed_user / "config.json"
        replaced_sources = []

        def publisher(temporary: Path) -> None:
            os.replace(temporary, destination_db)

        merged_config = {
            "sources": [
                {"type": "jpod", "id": "s1", "path": "user_files/s1_files", "display": "Source"}
            ]
        }
        with patch.object(migration, "get_config_path", return_value=config_path), patch.object(
            migration, "read_config", return_value=merged_config
        ), patch.object(migration, "update_db_version"), patch.object(
            migration, "build_audio_pack", side_effect=RuntimeError("pack failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "pack failed"):
                migration.process_existing_collection(
                    self.external,
                    self.sources,
                    destination_db,
                    self.installed_user / "fast_audio",
                    publisher=publisher,
                    replace_sources=lambda value: replaced_sources.append(value),
                )
        self.assertTrue(destination_db.is_file())
        self.assertEqual(len(replaced_sources), 1)
        expected = self.external_source.resolve()
        self.assertEqual(self.sources["s1"].get_media_dir_path(), expected)
        self.assertEqual(replaced_sources[0]["s1"].get_media_dir_path(), expected)
        persisted = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["sources"][0]["path"], str(expected))


if __name__ == "__main__":
    unittest.main()
