from __future__ import annotations

import json
import unittest

from pathlib import Path


ROOT = Path(__file__).parents[1]


class PackageDefinitionTests(unittest.TestCase):
    def test_code_only_package_has_required_runtime_files_and_no_user_data(self) -> None:
        script = (ROOT / "build-code-only-package.ps1").read_text(encoding="utf-8")
        required = {
            "__init__.py",
            "cleanup.py",
            "fast_pack.py",
            "gui.py",
            "import_dialog.py",
            "manifest.json",
            "migration.py",
            "progress_ui.py",
        }
        for filename in required:
            self.assertIn(f"'{filename}'", script)
        self.assertNotIn("'user_files'", script)
        self.assertIn("foreach ($directory in @('source', 'tests'))", script)

        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["package"], "1045800357")
        self.assertEqual(manifest["human_version"], "2.0.0-fast")


if __name__ == "__main__":
    unittest.main()
