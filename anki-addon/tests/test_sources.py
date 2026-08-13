from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest

from contextlib import closing
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
PACKAGE = "_local_audio_fast_sources_test_addon"
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

audio_source = importlib.import_module(f"{PACKAGE}.source.audio_source")
ajt_jp = importlib.import_module(f"{PACKAGE}.source.ajt_jp")
flat = importlib.import_module(f"{PACKAGE}.source.flat")
config_module = importlib.import_module(f"{PACKAGE}.config")
db_utils = importlib.import_module(f"{PACKAGE}.db_utils")


def make_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    db_utils._initialize_schema(connection)
    return connection


def rows_of(connection: sqlite3.Connection) -> list[tuple]:
    return connection.execute(
        "SELECT expression, reading, source, speaker, display, file FROM entries "
        "ORDER BY expression, file"
    ).fetchall()


def flat_source(root: Path, source_id: str = "forvo_ext"):
    data = audio_source.AudioSourceData(source_id, str(root), "Forvo Ext")
    return flat.FlatDirAudioSource(data)


def ajt_source(root: Path, source_id: str = "taas"):
    data = audio_source.AudioSourceData(source_id, str(root), "TAAS")
    return ajt_jp.AJTJapaneseSource(data)


def write_index(root: Path, index: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8"
    )


class FlatSourceTests(unittest.TestCase):
    def test_nested_files_use_the_stem_and_a_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a" / "b").mkdir(parents=True)
            (root / "a" / "b" / "猫.mp3").write_bytes(b"ID3")
            (root / "犬.opus").write_bytes(b"OggS")
            (root / "notes.txt").write_text("ignored", encoding="utf-8")
            with closing(make_connection()) as connection:
                flat_source(root).add_entries(connection)
                rows = rows_of(connection)
        self.assertEqual(
            sorted(rows),
            sorted(
                [
                    ("猫", None, "forvo_ext", None, None, str(Path("a/b/猫.mp3"))),
                    ("犬", None, "forvo_ext", None, None, "犬.opus"),
                ]
            ),
        )

    def test_should_cancel_keyword_is_accepted_and_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "猫.mp3").write_bytes(b"ID3")
            with closing(make_connection()) as connection:
                with self.assertRaises(InterruptedError):
                    flat_source(root).add_entries(
                        connection, should_cancel=lambda: True
                    )
                self.assertEqual(rows_of(connection), [])

    def test_missing_directory_adds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "absent"
            with closing(make_connection()) as connection:
                flat_source(root).add_entries(connection, should_cancel=lambda: False)
                self.assertEqual(rows_of(connection), [])


class AJTMediaLayoutTests(unittest.TestCase):
    def _index(self) -> dict:
        return {
            "meta": {"version": 1},
            "headwords": {"読む": ["yomu.ogg"]},
            "files": {"yomu.ogg": {"kana_reading": "よむ", "pitch_number": "1"}},
        }

    def test_audio_directory_is_used_when_media_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_index(root, self._index())
            (root / "audio").mkdir()
            (root / "audio" / "yomu.ogg").write_bytes(b"OggS")
            with closing(make_connection()) as connection:
                ajt_source(root).add_entries(connection)
                rows = rows_of(connection)
        self.assertEqual(
            rows,
            [("読む", "よむ", "taas", None, "ヨ＼ム [1]", str(Path("audio/yomu.ogg")))],
        )

    def test_explicit_meta_media_dir_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index = self._index()
            index["meta"]["media_dir"] = "sounds"
            write_index(root, index)
            for name in ("media", "audio", "sounds"):
                (root / name).mkdir()
            (root / "media" / "yomu.ogg").write_bytes(b"OggS-media")
            (root / "sounds" / "yomu.ogg").write_bytes(b"OggS-sounds")
            with closing(make_connection()) as connection:
                ajt_source(root).add_entries(connection)
                rows = rows_of(connection)
        self.assertEqual(rows[0][5], str(Path("sounds/yomu.ogg")))

    def test_media_wins_over_audio_when_both_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_index(root, self._index())
            (root / "media").mkdir()
            (root / "audio").mkdir()
            (root / "media" / "yomu.ogg").write_bytes(b"OggS-media")
            (root / "audio" / "yomu.ogg").write_bytes(b"OggS-audio")
            with closing(make_connection()) as connection:
                ajt_source(root).add_entries(connection)
                rows = rows_of(connection)
        self.assertEqual(rows[0][5], str(Path("media/yomu.ogg")))

    def test_extension_substitution_resolves_a_different_container(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_index(root, self._index())
            (root / "media").mkdir()
            (root / "media" / "yomu.mp3").write_bytes(b"ID3")
            with closing(make_connection()) as connection:
                ajt_source(root).add_entries(connection)
                rows = rows_of(connection)
        self.assertEqual(rows[0][5], str(Path("media/yomu.mp3")))

    def test_exact_name_beats_extension_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_index(root, self._index())
            (root / "media").mkdir()
            (root / "media" / "yomu.mp3").write_bytes(b"ID3")
            (root / "media" / "yomu.ogg").write_bytes(b"OggS")
            with closing(make_connection()) as connection:
                ajt_source(root).add_entries(connection)
                rows = rows_of(connection)
        self.assertEqual(rows[0][5], str(Path("media/yomu.ogg")))

    def test_meta_media_dir_outside_the_source_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary) / "shared_audio"
            outside.mkdir()
            (outside / "yomu.ogg").write_bytes(b"OggS-outside")
            root = Path(temporary) / "taas_files"
            index = self._index()
            index["meta"]["media_dir"] = "../shared_audio"
            write_index(root, index)
            (root / "media").mkdir()
            (root / "media" / "yomu.ogg").write_bytes(b"OggS-media")
            with closing(make_connection()) as connection:
                ajt_source(root).add_entries(connection)
                rows = rows_of(connection)
        self.assertEqual(rows[0][5], str(Path("media/yomu.ogg")))

    def test_case_differing_index_key_still_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_index(
                root,
                {
                    "meta": {"version": 1},
                    "headwords": {"読む": ["Yomu.OGG"]},
                    "files": {"Yomu.OGG": {"kana_reading": "よむ", "pitch_number": "1"}},
                },
            )
            (root / "media").mkdir()
            (root / "media" / "yomu.ogg").write_bytes(b"OggS")
            with closing(make_connection()) as connection:
                ajt_source(root).add_entries(connection)
                rows = rows_of(connection)
        self.assertEqual(rows[0][5], str(Path("media/yomu.ogg")))

    def test_nested_index_keys_and_missing_files_are_handled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_index(
                root,
                {
                    "meta": {"version": 1},
                    "headwords": {"読む": ["a/yomu.ogg"], "犬": ["inu.ogg"]},
                    "files": {
                        "a/yomu.ogg": {"kana_reading": "よむ", "pitch_number": "1"},
                        "inu.ogg": {"kana_reading": "いぬ", "pitch_number": "2"},
                    },
                },
            )
            (root / "audio" / "a").mkdir(parents=True)
            (root / "audio" / "a" / "yomu.opus").write_bytes(b"OggS")
            with closing(make_connection()) as connection:
                ajt_source(root).add_entries(connection)
                rows = rows_of(connection)
        self.assertEqual([row[0] for row in rows], ["読む"])
        self.assertEqual(rows[0][5], str(Path("audio/a/yomu.opus")))

    def test_missing_index_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with closing(make_connection()) as connection:
                ajt_source(Path(temporary) / "absent").add_entries(connection)
                self.assertEqual(rows_of(connection), [])


class ConfigSourceTypeTests(unittest.TestCase):
    def test_flat_type_is_registered(self) -> None:
        self.assertIs(config_module.SOURCE_TYPES["flat"], flat.FlatDirAudioSource)

    def test_unknown_config_type_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default_path = root / "default.json"
            default_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "type": "not_a_type",
                                "id": "bogus",
                                "path": "user_files/bogus_files",
                                "display": "Bogus",
                            },
                            {
                                "type": "flat",
                                "id": "forvo_ext",
                                "path": "user_files/forvo_ext_files",
                                "display": "Forvo Ext",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                config_module, "get_default_config_path", return_value=default_path
            ), patch.object(
                config_module, "get_config_path", return_value=root / "missing.json"
            ), patch.object(
                config_module, "get_program_root_path", return_value=root
            ):
                sources = config_module.get_all_sources()
        self.assertEqual(set(sources), {"forvo_ext"})

    def test_unknown_source_meta_override_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default_path = root / "default.json"
            default_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "type": "flat",
                                "id": "forvo_ext",
                                "path": "user_files/forvo_ext_files",
                                "display": "Forvo Ext",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            media = root / "user_files" / "forvo_ext_files"
            media.mkdir(parents=True)
            (media / "source_meta.json").write_text(
                json.dumps({"type": "from_the_future"}), encoding="utf-8"
            )
            with patch.object(
                config_module, "get_default_config_path", return_value=default_path
            ), patch.object(
                config_module, "get_config_path", return_value=root / "missing.json"
            ), patch.object(
                config_module, "get_program_root_path", return_value=root
            ):
                sources = config_module.get_all_sources()
        self.assertEqual(sources, {})


class InitDbWithAbsentSourcesTests(unittest.TestCase):
    def test_default_sources_without_folders_build_an_empty_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            version = root / "version.txt"
            version.write_text("2.0.0\n", encoding="utf-8")
            database = root / "user_files" / "entries.db"
            with patch.object(
                db_utils, "get_program_root_path", return_value=root
            ), patch.object(
                db_utils, "get_db_path", return_value=database
            ), patch.object(
                db_utils, "get_version_file_path", return_value=version
            ), patch.object(
                audio_source, "get_program_root_path", return_value=root / "absent"
            ):
                db_utils.init_db(sources=config_module.get_all_sources())
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(rows_of(connection), [])


if __name__ == "__main__":
    unittest.main()
