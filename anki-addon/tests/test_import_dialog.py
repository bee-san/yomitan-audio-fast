from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
import types
import unittest

from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
PACKAGE = "_local_audio_fast_import_dialog_test_addon"
os.environ["LOCAL_AUDIO_FAST_STANDALONE"] = "1"
specification = importlib.util.spec_from_file_location(
    PACKAGE,
    ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
assert specification is not None and specification.loader is not None
package = importlib.util.module_from_spec(specification)
sys.modules[PACKAGE] = package
specification.loader.exec_module(package)


class FakeDialog:
    def __init__(self, *_args):
        self.accepted = False

    def setWindowTitle(self, *_args):
        pass

    def setAcceptDrops(self, *_args):
        pass

    def setMinimumWidth(self, *_args):
        pass

    def accept(self):
        self.accepted = True

    def reject(self):
        pass


class FakeLabel:
    def __init__(self, text):
        self.text = text

    def setWordWrap(self, *_args):
        pass

    def setMinimumHeight(self, *_args):
        pass

    def setStyleSheet(self, *_args):
        pass

    def setText(self, text):
        self.text = text


class FakeButton:
    def __init__(self, *_args):
        self.clicked = object()


class FakeLayout:
    def __init__(self, *_args):
        pass

    def addWidget(self, *_args):
        pass


class ImportDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fake_qt = types.ModuleType("aqt.qt")
        fake_qt.QDialog = FakeDialog
        fake_qt.QFileDialog = object
        fake_qt.QLabel = FakeLabel
        fake_qt.QPushButton = FakeButton
        fake_qt.QVBoxLayout = FakeLayout
        fake_qt.qconnect = lambda *_args: None
        fake_aqt = types.ModuleType("aqt")
        with patch.dict(sys.modules, {"aqt": fake_aqt, "aqt.qt": fake_qt}):
            cls.dialog_module = importlib.import_module(f"{PACKAGE}.import_dialog")

    def test_invalid_directory_stays_open_with_inline_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary)

            def reject(_path):
                raise ValueError("entries.db quick_check failed: malformed")

            dialog = self.dialog_module.ExistingAudioDropDialog(
                None, selected, reject
            )
            dialog._select(selected)
            self.assertFalse(dialog.accepted)
            self.assertIsNone(dialog.selected_path)
            text = dialog._label.text
            lowered = text.lower()
            # Leads with plain language, not the raw quick_check jargon.
            self.assertTrue(
                lowered.startswith("that folder")
                or lowered.startswith("this folder"),
                f"label should lead plainly, got: {text!r}",
            )
            self.assertNotIn("quick_check", text.split("Details")[0])
            # Tells the user what to do next.
            self.assertIn("another folder", lowered)
            # Preserves the exact reason for support, after the plain guidance.
            self.assertIn("entries.db quick_check failed: malformed", text)
            self.assertIn("Details", text)
            self.assertLess(
                text.index("Details"),
                text.index("quick_check"),
                "raw reason must come after the plain guidance",
            )

    def test_valid_directory_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary)
            dialog = self.dialog_module.ExistingAudioDropDialog(
                None, selected, lambda _path: None
            )
            dialog._select(selected)
            self.assertTrue(dialog.accepted)
            self.assertEqual(dialog.selected_path, selected)


if __name__ == "__main__":
    unittest.main()
