from __future__ import annotations

import threading

from aqt import mw
from aqt.qt import QProgressDialog, qconnect

from .fast_pack import PackProgress


class OperationProgress:
    """Cancelable determinate progress without relying on Anki backend jobs."""

    def __init__(self, title: str, label: str) -> None:
        self._cancelled = threading.Event()
        self._closed = False
        self._dialog = QProgressDialog(label, "Cancel", 0, 0, mw)
        self._dialog.setWindowTitle(title)
        self._dialog.setMinimumDuration(0)
        self._dialog.setAutoClose(False)
        self._dialog.setAutoReset(False)
        self._dialog.setMinimumWidth(440)
        qconnect(self._dialog.canceled, self._cancelled.set)
        self._dialog.show()

    @staticmethod
    def _scaled(progress: PackProgress) -> tuple[int, int]:
        maximum = 10_000
        if progress.total <= maximum:
            return min(progress.current, progress.total), progress.total
        current = int(min(progress.current, progress.total) * maximum / progress.total)
        return current, maximum

    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def emit_message(self, message: str) -> None:
        mw.taskman.run_on_main(lambda: self._set_message(message))

    def emit_progress(self, progress: PackProgress) -> None:
        mw.taskman.run_on_main(lambda: self._set_progress(progress))

    def _set_message(self, message: str) -> None:
        if self._closed:
            return
        self._dialog.setRange(0, 0)
        self._dialog.setLabelText(message)

    def _set_progress(self, progress: PackProgress) -> None:
        if self._closed:
            return
        self._dialog.setLabelText(progress.message)
        if progress.total > 0:
            current, maximum = self._scaled(progress)
            self._dialog.setRange(0, maximum)
            self._dialog.setValue(current)
        else:
            self._dialog.setRange(0, 0)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._dialog.hide()
        self._dialog.reset()
        self._dialog.deleteLater()
