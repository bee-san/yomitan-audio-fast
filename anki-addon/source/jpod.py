import sqlite3
from typing import Callable, Optional

from .audio_source import AudioSource
from ..jp_util import is_kana


class JPodAudioSource(AudioSource):
    def add_entries(
        self,
        connection: sqlite3.Connection,
        should_cancel: Optional[Callable[[], bool]] = None,
    ):
        cur = connection.cursor()
        batch = []
        sql = f"""
        INSERT INTO entries
          (expression, reading, source, file)
        VALUES
          (?,?,?,?)
            """

        for path in self.find_media_files():
            if should_cancel is not None and should_cancel():
                raise InterruptedError("database generation cancelled")
            relative_path = str(path.relative_to(self.get_media_dir_path()))
            basename_noext = path.stem
            parts = basename_noext.split(" - ")

            # Cannot parse required fields from a filename missing a " - " separator.
            if len(parts) != 2:
                print(
                    f"({self.__class__.__name__}) skipping file without ' - ' sep: {relative_path}"
                )
                continue

            # usually, jpod file names are formatted as:
            # "reading - term.ext"
            # however, sometimes, the reading section is just the term (even if the term is kanji)
            reading, expr = parts

            if reading == expr:
                if is_kana(reading):
                    # it's likely safe to store kana only words like this
                    batch.append((reading, reading, self.data.id, relative_path))
                else:
                    batch.append((reading, None, self.data.id, relative_path))
            else:
                batch.append((expr, reading, self.data.id, relative_path))

            if len(batch) >= 8192:
                cur.executemany(sql, batch)
                batch.clear()

        if batch:
            cur.executemany(sql, batch)

        cur.close()
        connection.commit()
