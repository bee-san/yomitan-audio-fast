from __future__ import annotations

import http.client
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest

from pathlib import Path
from urllib.parse import quote, urlsplit


SOURCE = Path(__file__).parents[1]
ADDON_ID = "1045800357"


class DropInOverlayTests(unittest.TestCase):
    def test_same_id_overlay_preserves_collection_and_serves_existing_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            addon = Path(temporary) / "Anki2" / "addons21" / ADDON_ID
            user_files = addon / "user_files"
            media = user_files / "jpod_files"
            media.mkdir(parents=True)
            audio = b"ID3-existing-user-audio"
            (media / "neko.mp3").write_bytes(audio)
            sentinel = user_files / "must-survive.txt"
            sentinel.write_text("legacy user data", encoding="utf-8")
            (user_files / "config.json").write_text(
                json.dumps(
                    {
                        "server": {"port": 5050},
                        "sources": [
                            {
                                "type": "jpod",
                                "id": "jpod",
                                "path": "user_files/jpod_files",
                                "display": "Jpod101",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            database = sqlite3.connect(user_files / "entries.db")
            try:
                database.execute(
                    "CREATE TABLE entries (id INTEGER PRIMARY KEY NOT NULL, "
                    "expression TEXT NOT NULL, reading TEXT, source TEXT NOT NULL, "
                    "speaker TEXT, display TEXT, file TEXT NOT NULL)"
                )
                database.execute(
                    "INSERT INTO entries VALUES (1,?,?,?,?,?,?)",
                    ("猫", "ねこ", "jpod", None, None, "neko.mp3"),
                )
                database.execute(
                    "CREATE INDEX idx_expr_reading ON entries(expression, reading)"
                )
                database.commit()
            finally:
                database.close()

            # This is the documented upgrade operation: copy code over the same
            # numeric add-on directory without deleting user_files first.
            shutil.copytree(SOURCE, addon, dirs_exist_ok=True)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "legacy user data")
            self.assertEqual((media / "neko.mp3").read_bytes(), audio)

            import importlib.util
            import os
            import sys

            package_name = "_drop_in_overlay_test_addon"
            os.environ["LOCAL_AUDIO_FAST_STANDALONE"] = "1"
            specification = importlib.util.spec_from_file_location(
                package_name,
                addon / "__init__.py",
                submodule_search_locations=[str(addon)],
            )
            assert specification is not None and specification.loader is not None
            package = importlib.util.module_from_spec(specification)
            sys.modules[package_name] = package
            specification.loader.exec_module(package)
            server_module = __import__(f"{package_name}.server", fromlist=["ServerRuntime"])
            runtime = server_module.ServerRuntime(port=0)
            thread = threading.Thread(target=runtime.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(*runtime.address, timeout=5)
                lookup = f"/?term={quote('猫')}&reading={quote('ねこ')}"
                connection.request("GET", lookup)
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["type"], "audioSourceList")
                self.assertEqual(len(payload["audioSources"]), 1)

                audio_url = urlsplit(payload["audioSources"][0]["url"])
                connection.request("GET", audio_url.path)
                audio_response = connection.getresponse()
                self.assertEqual(audio_response.status, 200)
                self.assertEqual(audio_response.read(), audio)
                connection.close()
            finally:
                runtime.stop()
                thread.join(5)
                sys.modules.pop(package_name, None)


if __name__ == "__main__":
    unittest.main()
