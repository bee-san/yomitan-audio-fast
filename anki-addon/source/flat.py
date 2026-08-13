import sqlite3

from typing import Callable, Final, Optional

from .audio_source import AudioSource


SQL: Final[str] = (
    "INSERT INTO entries (expression, reading, source, speaker, display, file) "
    "VALUES (?,?,?,?,?,?)"
)


class FlatDirAudioSource(AudioSource):
    """Index-less source: every audio file's stem is the expression, reading is unknown.

    Used by the Yomitan Ultimate Audio extra Forvo sets, which ship no metadata file.
    """

    def add_entries(
        self,
        connection: sqlite3.Connection,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> None:
        media_dir = self.get_media_dir_path()
        cur = connection.cursor()
        batch = []

        # a missing directory simply yields no files
        for path in self.find_media_files():
            if should_cancel is not None and should_cancel():
                raise InterruptedError("database generation cancelled")
            relative_path = str(path.relative_to(media_dir))
            batch.append((path.stem, None, self.data.id, None, None, relative_path))
            if len(batch) >= 8192:
                cur.executemany(SQL, batch)
                batch.clear()

        if batch:
            cur.executemany(SQL, batch)

        cur.close()
        connection.commit()
