from __future__ import annotations

import json
import sqlite3

from typing import Callable, Final, Optional, TypedDict

from .audio_source import AudioSource
from ..jp_util import hiragana_to_katakana, split_into_mora


class OZK5Data(TypedDict):
    kanji: str
    kana: str
    audio_file: str


class OZK5Meta(TypedDict):
    media_dir: str


class OZK5Index(TypedDict):
    meta: OZK5Meta
    entries: list[OZK5Data]


SQL: Final[str] = (
    "INSERT INTO entries (expression, reading, source, display, file) VALUES (?,?,?,?,?)"
)


class OZK5AudioSource(AudioSource):
    def get_display_text(self, entry: OZK5Data) -> Optional[str]:
        reading = entry.get("kana")
        if reading is None:
            return None
        return "".join(split_into_mora(hiragana_to_katakana(reading)))

    def add_entries(
        self,
        connection: sqlite3.Connection,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> None:
        index_file = self.get_media_dir_path() / "index.json"
        if not index_file.is_file():
            print(f"({self.__class__.__name__}) Cannot find entries file: {index_file}")
            return
        data: OZK5Index = json.loads(index_file.read_text(encoding="utf-8"))
        media_dir = data["meta"].get("media_dir", "media")
        rows = []
        for entry in data["entries"]:
            if should_cancel is not None and should_cancel():
                raise InterruptedError("database generation cancelled")
            expression = entry["kanji"] or entry["kana"]
            reading = entry["kana"]
            relative = (self.get_media_dir_path() / media_dir / entry["audio_file"]).relative_to(
                self.get_media_dir_path()
            )
            if not (self.get_media_dir_path() / relative).is_file():
                continue
            display = self.get_display_text(entry)
            rows.append((expression, reading, self.data.id, display, str(relative)))
            if entry["kanji"] and entry["kanji"] != reading:
                rows.append((reading, reading, self.data.id, display, str(relative)))
        connection.executemany(SQL, rows)
        connection.commit()
