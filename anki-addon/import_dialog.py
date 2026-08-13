from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from aqt.qt import (
    QDialog,
    QFileDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    qconnect,
)


class ExistingAudioDropDialog(QDialog):
    """One obvious drop target for replacing an existing Local Audio Server."""

    def __init__(
        self,
        parent,
        initial_directory: Path,
        validator: Callable[[Path], None],
    ) -> None:
        super().__init__(parent)
        self.selected_path: Optional[Path] = None
        self._initial_directory = initial_directory
        self._validator = validator
        self._prompt = (
            "Drop your old Local Audio Server folder or its user_files folder here.\n\n"
            "Your original database and audio stay where they are."
        )
        self.setWindowTitle("Import existing audio collection")
        self.setAcceptDrops(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        self._label = QLabel(self._prompt)
        self._label.setWordWrap(True)
        self._label.setMinimumHeight(130)
        self._label.setStyleSheet(
            "QLabel { border: 2px dashed palette(mid); border-radius: 8px; "
            "padding: 24px; font-size: 15px; }"
        )
        layout.addWidget(self._label)

        browse = QPushButton("Browse for the old add-on folder…")
        cancel = QPushButton("Cancel")
        qconnect(browse.clicked, self._browse)
        qconnect(cancel.clicked, self.reject)
        layout.addWidget(browse)
        layout.addWidget(cancel)

    @staticmethod
    def _directory_from_event(event) -> Optional[Path]:
        urls = event.mimeData().urls()
        if len(urls) != 1:
            return None
        local_path = urls[0].toLocalFile()
        if not local_path:
            return None
        path = Path(local_path)
        return path if path.is_dir() else None

    def dragEnterEvent(self, event) -> None:
        if self._directory_from_event(event) is None:
            event.ignore()
            return
        self._label.setText("Release to safely import this audio collection")
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self._label.setText(self._prompt)
        event.accept()

    def dropEvent(self, event) -> None:
        path = self._directory_from_event(event)
        if path is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self._select(path)

    def _select(self, path: Path) -> None:
        try:
            self._validator(path)
        except (OSError, ValueError) as error:
            self._label.setText(
                "That folder is not a recognized Local Audio Server collection.\n\n"
                f"{error}\n\nTry another folder."
            )
            return
        self.selected_path = path
        self.accept()

    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose old Local Audio Server folder or user_files",
            str(self._initial_directory),
        )
        if selected:
            self._select(Path(selected))


def choose_existing_audio_directory(
    parent,
    initial_directory: Path,
    validator: Callable[[Path], None],
) -> Optional[Path]:
    dialog = ExistingAudioDropDialog(parent, initial_directory, validator)
    return dialog.selected_path if dialog.exec() else None
