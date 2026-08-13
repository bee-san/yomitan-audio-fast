from __future__ import annotations  # for Python 3.7-3.9

import json
import sqlite3
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Final, TypedDict
# THIS REQUIRES A PIP INSTALL, making it impossible to use in Anki...
#from typing_extensions import NotRequired

from .audio_source import AudioSource
from ..jp_util import split_into_mora, hiragana_to_katakana


"""
This is based off of the schema found under `AudioSource`
(https://github.com/Ajatt-Tools/Japanese/blob/f5bbbad901a9ffc4dbf00f244de19f3a9a8120bd/helpers/audio_manager.py#L156):

{
  "meta": {
    // ... (not important for this add-on)
  },
  "headwords": {
    // maps words to file
  },
  "files": {
    // maps file to {"kana_reading": ..., "pitch_number": ...}
    // WARNING: "kana_reading" may be katakana!
    // WARNING: "pitch_number" maps to a string for whatever reason
  },
}
"""


class AJTFile(TypedDict):
    #kana_reading: NotRequired[str]
    kana_reading: str
    pitch_number: str
    #pitch_pattern: NotRequired[str]
    pitch_pattern: str


class AJTMeta(TypedDict):
    version: int
    media_dir: str
    # other fields are currently ignored for the purposes of this add-on


class AJTIndex(TypedDict):
    meta: AJTMeta
    headwords: dict[str, list[str]]
    files: dict[str, AJTFile]


SQL: Final[
    str
] = "INSERT INTO entries (expression, reading, source, display, file) VALUES (?,?,?,?,?)"

# preference order for resolving an index entry against a different container on disk;
# also breaks stem collisions deterministically
EXTENSION_PREFERENCE: Final[tuple] = (
    ".mp3",
    ".ogg",
    ".opus",
    ".m4a",
    ".aac",
    ".flac",
    ".wav",
    ".oga",
)


def walk_media_files(media_dir: Path) -> dict:
    """posix-keyed relative name -> path, walked once instead of probing per headword"""
    files = {}
    for path in media_dir.rglob("*"):
        if path.is_file():
            files[path.relative_to(media_dir).as_posix()] = path
    return files


def resolve_media_file(files: dict, word_file: str) -> Optional[Path]:
    key = PurePosixPath(word_file.replace("\\", "/")).as_posix()
    path = files.get(key)
    if path is not None:
        return path
    # some datasets ship a different container than the index names
    stem = key[: len(key) - len(PurePosixPath(key).suffix)]
    for extension in EXTENSION_PREFERENCE:
        path = files.get(stem + extension)
        if path is not None:
            return path
    return None


class AJTJapaneseSource(AudioSource):
    def get_media_dir_name(self, index: AJTIndex) -> str:
        """AJT ships `media/`, the Yomitan Ultimate Audio sets ship `audio/`"""
        root = self.get_media_dir_path()
        meta = index.get("meta") or {}
        name = meta.get("media_dir") if isinstance(meta, dict) else None
        if isinstance(name, str) and name and root.joinpath(name).is_dir():
            return name
        for candidate in ("media", "audio"):
            if root.joinpath(candidate).is_dir():
                return candidate
        return "media"

    def get_display_text(self, ajt_file: AJTFile) -> Optional[str]:
        """
        displays as katakana with number and downstep, i.e. "ヨ＼ム [1]"
        """
        reading = ajt_file.get("kana_reading", None)
        if reading is None:
            return None
        mora_list = split_into_mora(hiragana_to_katakana(reading))
        try:
            if ajt_file["pitch_number"] == "?":
                return None
            pitch_accent = int(ajt_file["pitch_number"])
        except Exception:
            # apparently, pitch_number can be something like "0+2", in which case we look for pitch_pattern
            pitch_pattern = ajt_file.get("pitch_pattern", None)
            if pitch_pattern is not None:
                return pitch_pattern
            print(f"({self.data.id}) pitch_number is not an integer: {ajt_file}")
            return None
        if pitch_accent > 0:
            mora_list.insert(pitch_accent, "＼")
        return "".join(mora_list) + f" [{pitch_accent}]"

    def add_entries(
        self,
        connection: sqlite3.Connection,
        should_cancel: Optional[Callable[[], bool]] = None,
    ):
        cur = connection.cursor()
        batch = []
        index_file = self.get_media_dir_path().joinpath("index.json")

        if not index_file.is_file(): # don't error if it simply doesn't exist
            print(f"({self.__class__.__name__}) Cannot find entries file: {index_file}")
            cur.close()
            return

        with open(index_file, encoding="utf-8") as f:
            entries: AJTIndex = json.load(f)
            files = entries["files"]
            media_dir = self.get_media_dir_path().joinpath(self.get_media_dir_name(entries))
            on_disk = walk_media_files(media_dir)

            for expression, word_files in entries["headwords"].items():
                if should_cancel is not None and should_cancel():
                    raise InterruptedError("database generation cancelled")
                for word_file in word_files:
                    fullpath = resolve_media_file(on_disk, word_file)
                    if fullpath is None:
                        continue
                    relpath = fullpath.relative_to(self.get_media_dir_path())
                    ajt_file = files.get(word_file, None)
                    if ajt_file is not None:
                        reading = ajt_file.get("kana_reading", None)
                        display = self.get_display_text(ajt_file)
                        batch.append((expression, reading, self.data.id, display, str(relpath)))
                        if len(batch) >= 8192:
                            cur.executemany(SQL, batch)
                            batch.clear()

            if batch:
                cur.executemany(SQL, batch)

        cur.close()
        connection.commit()
