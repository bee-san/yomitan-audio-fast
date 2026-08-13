import sqlite3
from typing import Callable, Optional

from .audio_source import AudioSource
from ..consts import *


class ForvoAudioSource(AudioSource):
    def add_entries(
        self,
        connection: sqlite3.Connection,
        should_cancel: Optional[Callable[[], bool]] = None,
    ):
        sql = "INSERT INTO entries (expression, source, speaker, display, file) VALUES (?,?,?,?,?)"
        cur = connection.cursor()
        batch = []

        for path in self.find_media_files():
            if should_cancel is not None and should_cancel():
                raise InterruptedError("database generation cancelled")
            relative_path = str(path.relative_to(self.get_media_dir_path()))
            speaker = path.parent.name
            display = speaker
            expr = path.stem
            batch.append((expr, self.data.id, speaker, display, relative_path))
            if len(batch) >= 8192:
                cur.executemany(sql, batch)
                batch.clear()

        if batch:
            cur.executemany(sql, batch)

        cur.close()
        connection.commit()
