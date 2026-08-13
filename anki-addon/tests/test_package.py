from __future__ import annotations

import json
import re
import unittest

from pathlib import Path


ROOT = Path(__file__).parents[1]


class PackageDefinitionTests(unittest.TestCase):
    def test_code_only_package_contains_only_runtime_files(self) -> None:
        script = (ROOT / "build-code-only-package.ps1").read_text(encoding="utf-8")
        root_files_block = re.search(
            r"\$rootFiles = @\(\s*(.*?)\s*\)", script, re.DOTALL
        )
        self.assertIsNotNone(root_files_block)
        packaged_root_files = set(re.findall(r"'([^']+)'", root_files_block.group(1)))
        self.assertEqual(
            packaged_root_files,
            {
                "__init__.py",
                "config.py",
                "consts.py",
                "cleanup.py",
                "db_utils.py",
                "default_config.json",
                "fast_pack.py",
                "fast_store.py",
                "gui.py",
                "import_dialog.py",
                "jp_util.py",
                "manifest.json",
                "migration.py",
                "progress_ui.py",
                "server.py",
                "util.py",
                "version.txt",
            },
        )
        self.assertNotIn("'user_files'", script)
        self.assertIn("foreach ($directory in @('source'))", script)
        self.assertNotIn("Join-Path $PSScriptRoot 'benchmarks'", script)

        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["package"], "1045800357")
        self.assertEqual(manifest["human_version"], "2.0.1-fast")


if __name__ == "__main__":
    unittest.main()
