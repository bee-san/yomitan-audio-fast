from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types
import unittest

from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
PACKAGE = "_local_audio_fast_gui_messages_test_addon"
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


def _load_gui(warnings: list, infos: list):
    """Import the add-on gui module with a stubbed aqt and message capture."""

    fake_aqt = types.ModuleType("aqt")
    fake_aqt.gui_hooks = types.SimpleNamespace(main_window_did_init=[])
    fake_aqt.mw = types.SimpleNamespace(
        form=types.SimpleNamespace(menuTools=None),
        taskman=types.SimpleNamespace(),
    )
    fake_operations = types.ModuleType("aqt.operations")
    fake_operations.QueryOp = object
    fake_qt = types.ModuleType("aqt.qt")
    fake_qt.QAction = object
    fake_qt.QFileDialog = object
    fake_qt.qconnect = lambda *args, **kwargs: None
    fake_utils = types.ModuleType("aqt.utils")
    fake_utils.askUser = lambda *args, **kwargs: False
    fake_utils.showInfo = lambda text, *args, **kwargs: infos.append(text)
    fake_utils.showWarning = lambda text, *args, **kwargs: warnings.append(text)
    modules = {
        "aqt": fake_aqt,
        "aqt.operations": fake_operations,
        "aqt.qt": fake_qt,
        "aqt.utils": fake_utils,
    }
    gui_name = f"{PACKAGE}.gui"
    sys.modules.pop(gui_name, None)
    with patch.dict(sys.modules, modules):
        gui = importlib.import_module(gui_name)
    return gui


class _FakeProgress:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class CleanupFailureMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.gui = _load_gui(self.warnings, self.infos)
        self.cleanup = importlib.import_module(f"{PACKAGE}.cleanup")

    def _run_failure(self, error: Exception, *, restoring: bool = False) -> str:
        progress = _FakeProgress()
        with patch.object(self.gui, "load_packed_only_state", return_value=None):
            self.gui._cleanup_failure(error, progress, restoring=restoring)
        self.assertTrue(progress.closed, "progress dialog must be closed")
        self.assertEqual(len(self.warnings), 1, "exactly one warning should show")
        self.assertEqual(self.infos, [], "a safety stop must not use an info dialog")
        return self.warnings[0]

    def test_loose_audio_changed_leads_with_calm_no_deletion_headline(self) -> None:
        path = "/collection/user_files/source/word.opus"
        error = self.cleanup.LooseAudioChangedError(
            f"loose audio changed during verification: {path}"
        )
        message = self._run_failure(error)
        lines = message.splitlines()
        headline = lines[0].strip()
        # Calm, plain-language headline: it names that a file changed, not an invariant.
        self.assertIn("audio file changed", headline.lower())
        self.assertNotIn("verification", headline.lower())
        # Prominently reassures that nothing was deleted, near the top.
        top = "\n".join(lines[:3]).lower()
        self.assertIn("nothing", top)
        self.assertIn("delet", top)
        # Explains another process may be changing the folder.
        lowered = message.lower()
        self.assertTrue(
            "another program" in lowered
            or "another process" in lowered
            or "sync" in lowered,
            "must explain another process may be changing the folder",
        )
        # Gives the exact retry menu path.
        self.assertIn(
            "Tools → Local Audio Server → Move verified loose audio to Trash",
            message,
        )
        # Preserves the technical detail (including the filename) after the guidance.
        self.assertIn(path, message)
        detail_index = message.index("Technical detail")
        self.assertLess(detail_index, message.index(path))

    def test_pack_mismatch_says_nothing_deleted_and_names_rebuild_and_retry(self) -> None:
        path = "/collection/user_files/source/word.opus"
        error = self.cleanup.PackMismatchError(
            f"loose audio bytes no longer match the verified pack: {path}"
        )
        message = self._run_failure(error)
        lowered = message.lower()
        self.assertIn("no longer matches", lowered)
        self.assertIn("nothing", lowered)
        self.assertIn("delet", lowered)
        # Guides the user to rebuild the pack, then retry cleanup.
        self.assertIn("rebuild", lowered)
        self.assertIn(
            "Tools → Local Audio Server → Move verified loose audio to Trash",
            message,
        )
        detail_index = message.index("Technical detail")
        self.assertLess(detail_index, message.index(path))

    def test_generic_cleanup_safety_stop_stays_calm_and_actionable(self) -> None:
        error = self.cleanup.CleanupSafetyError(
            "the active pack has no integrity record"
        )
        message = self._run_failure(error)
        lowered = message.lower()
        self.assertTrue(lowered.startswith("cleanup stopped"))
        self.assertIn("nothing", lowered)
        self.assertIn("delet", lowered)
        self.assertIn(
            "Tools → Local Audio Server → Move verified loose audio to Trash",
            message,
        )
        self.assertIn("the active pack has no integrity record", message)
        detail_index = message.index("Technical detail")
        self.assertLess(
            detail_index, message.index("the active pack has no integrity record")
        )

    def test_restore_stop_names_restore_menu_and_reassures_no_overwrite(self) -> None:
        error = self.cleanup.CleanupSafetyError(
            "restore refuses to overwrite an existing file: /x/y.opus"
        )
        message = self._run_failure(error, restoring=True)
        lowered = message.lower()
        self.assertTrue(lowered.startswith("restore stopped"))
        self.assertIn("overwrit", lowered)
        self.assertIn(
            "Tools → Local Audio Server → Restore/verify loose audio originals",
            message,
        )
        self.assertIn("Technical detail", message)


class PackedOnlyBlockMessageTests(unittest.TestCase):
    """Plain-language copy for the packed-only maintenance guards.

    These dialogs previously led with implementation terms (``packed-only-v1.json``,
    ``loose-audio-originals-v1``, "cleanup journal", "invalidate"). An ordinary user
    should first read what state their audio is in and exactly what to do, with the
    filenames preserved afterwards as technical detail for support.
    """

    def setUp(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.gui = _load_gui(self.warnings, self.infos)

    def _assert_plain_first(self, message: str, jargon: str) -> None:
        lowered = message.lower()
        # If a jargon token appears at all, it must live in the technical detail,
        # never in the leading guidance an ordinary user reads first.
        if jargon.lower() in lowered:
            self.assertIn("Technical detail", message)
            self.assertLess(
                message.index("Technical detail"),
                lowered.index(jargon.lower()) + 1,
                f"{jargon!r} must appear only after the technical-detail marker",
            )

    def test_packed_only_active_block_leads_with_plain_language(self) -> None:
        message = self.gui._packed_only_block_message("rebuild the audio pack")
        lines = message.splitlines()
        headline = lines[0].lower()
        # Plain headline naming the blocked action, no raw filename.
        self.assertIn("rebuild the audio pack", headline)
        self.assertNotIn("packed-only", headline)
        self.assertNotIn(".json", headline)
        lowered = message.lower()
        # Reassures the originals are safe in the Trash, not lost.
        self.assertIn("trash", lowered)
        self.assertTrue(
            "original" in lowered and "safe" in lowered,
            "must reassure the original audio is safe",
        )
        # Names the exact restore menu path to get maintenance back.
        self.assertIn(
            "Tools → Local Audio Server → Restore/verify loose audio originals",
            message,
        )
        # The internal filename is preserved for support, after the guidance.
        self._assert_plain_first(message, "packed-only-v1.json")

    def test_missing_journal_block_avoids_jargon_headline(self) -> None:
        message = self.gui._quarantine_without_journal_message("rebuild the audio pack")
        lowered = message.lower()
        self.assertTrue(lowered.startswith("can't rebuild the audio pack") or
                        lowered.startswith("cannot rebuild the audio pack"))
        # No "journal" in the plain guidance; reassure nothing was rebuilt/removed.
        self.assertNotIn("journal", message.split("Technical detail")[0].lower())
        self.assertTrue("nothing" in lowered)
        self.assertIn(
            "Tools → Local Audio Server → Restore/verify loose audio originals",
            message,
        )

    def test_packed_only_state_unreadable_leads_plain(self) -> None:
        message = self.gui._packed_only_unreadable_message(
            "rebuild the audio pack", "packed-only-v1.json is not valid JSON"
        )
        lowered = message.lower()
        self.assertTrue(lowered.startswith("can't") or lowered.startswith("cannot"))
        # Original detail preserved after guidance.
        self.assertIn("packed-only-v1.json is not valid JSON", message)
        self._assert_plain_first(message, "packed-only-v1.json is not valid json")
        self.assertIn("Restore/verify loose audio originals", message)


class SuccessProgressCopyTests(unittest.TestCase):
    """Success and coverage copy should be understandable to ordinary users."""

    def setUp(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.gui = _load_gui(self.warnings, self.infos)

    def test_full_coverage_line_is_plain(self) -> None:
        line = self.gui._pack_coverage_line(missing_files=0)
        lowered = line.lower()
        self.assertIn("every", lowered)
        self.assertNotIn("mapping", lowered)  # avoid the "mapping" jargon
        self.assertIn("original", lowered)

    def test_partial_coverage_line_explains_plainly(self) -> None:
        line = self.gui._pack_coverage_line(missing_files=1234)
        lowered = line.lower()
        self.assertIn("1,234", line)
        # Explains what "missing" means without the loose-file cleanup jargon headline.
        self.assertTrue("could not be found" in lowered or "were missing" in lowered)
        self.assertIn("trash", lowered)  # references that Trash cleanup is unavailable


class OperationFailedMessageTests(unittest.TestCase):
    """The catch-all failure dialog should reassure and keep jargon out of the lead."""

    def setUp(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.gui = _load_gui(self.warnings, self.infos)

    def test_generic_failure_leads_plain_and_reassures(self) -> None:
        error = RuntimeError(
            "database regeneration is blocked while packed-only-v1.json exists; "
            "restore the complete original collection first"
        )
        message = self.gui._operation_failed_message(error)
        lowered = message.lower()
        # Calm, plain lead — a problem happened and it stopped safely.
        self.assertTrue(
            "ran into a problem" in lowered or "couldn't finish" in lowered,
            f"lead should be calm/plain, got: {message!r}",
        )
        # Reassures nothing was deleted, up top.
        top = "\n".join(message.splitlines()[:3]).lower()
        self.assertIn("delet", top)
        self.assertIn("nothing", top)
        # Raw internal string preserved only as technical detail, after the lead.
        self.assertIn("Technical detail", message)
        self.assertIn("packed-only-v1.json", message)
        self.assertLess(
            message.index("Technical detail"),
            message.index("packed-only-v1.json"),
        )
        self.assertNotIn("packed-only-v1.json", message.split("Technical detail")[0])

    def test_operation_failure_handler_uses_the_helper(self) -> None:
        with patch.object(self.gui, "_finish_job", lambda: None):
            self.gui._operation_failure(RuntimeError("quick_check failed: malformed"))
        self.assertEqual(len(self.warnings), 1)
        message = self.warnings[0]
        self.assertNotIn("quick_check", message.split("Technical detail")[0])
        self.assertIn("quick_check failed: malformed", message)


class DestructiveConfirmCopyTests(unittest.TestCase):
    """The Trash-move confirmation is high-risk; its copy must stay plain and safe."""

    def setUp(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.gui = _load_gui(self.warnings, self.infos)

    def _capture_prompt(self, info: dict) -> str:
        captured: dict = {}

        def fake_ask(text, *args, **kwargs):
            captured["text"] = text
            return False  # decline so no work runs

        fake_send2trash = types.ModuleType("send2trash")
        fake_send2trash.send2trash = lambda *a, **k: None
        with patch.dict(sys.modules, {"send2trash": fake_send2trash}), \
                patch.object(self.gui, "askUser", fake_ask), \
                patch.object(self.gui, "inspect_managed_cleanup", return_value=info), \
                patch.object(self.gui, "get_db_path", lambda: __import__("pathlib").Path(".")), \
                patch.object(self.gui, "get_program_root_path", lambda: __import__("pathlib").Path(".")):
            self.gui.remove_loose_audio_operation()
        return captured.get("text", "")

    def test_move_prompt_is_plain_and_preserves_safety_facts(self) -> None:
        info = {
            "eligible": True,
            "status": "ready",
            "moved_files": 0,
            "moved_bytes": 0,
            "files": 500,
            "bytes": 1024 ** 3,
        }
        prompt = self._capture_prompt(info)
        lowered = prompt.lower()
        # No raw jargon in the confirmation an ordinary user reads before deleting.
        self.assertNotIn("sha-256", lowered)
        self.assertNotIn("byte-compare", lowered)
        self.assertNotIn("entries.db", lowered)
        self.assertNotIn("unreferenced", lowered)
        # Plain reassurance the app double-checks and only touches its own audio.
        self.assertIn("trash", lowered)
        self.assertTrue("double-check" in lowered or "double check" in lowered)
        self.assertTrue("left alone" in lowered or "leaves" in lowered or "left" in lowered)
        # Still tells the user the count/size and that No is the safe default.
        self.assertIn("500", prompt)
        self.assertIn("safest", lowered)


if __name__ == "__main__":
    unittest.main()
