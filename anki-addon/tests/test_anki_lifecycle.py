from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest

from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
PACKAGE = "_local_audio_fast_lifecycle_test_addon"
os.environ["LOCAL_AUDIO_FAST_STANDALONE"] = "1"
specification = importlib.util.spec_from_file_location(
    PACKAGE,
    ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
assert specification is not None and specification.loader is not None
module = importlib.util.module_from_spec(specification)
sys.modules[PACKAGE] = module
specification.loader.exec_module(module)

config_module = importlib.import_module(f"{PACKAGE}.config")
server_module = importlib.import_module(f"{PACKAGE}.server")


class AnkiLifecycleTests(unittest.TestCase):
    def test_installing_this_addon_stops_the_server_before_files_move(self) -> None:
        package = "_local_audio_fast_installed_lifecycle_test_addon"
        hooks = []
        starts = []
        stops = []
        gui_starts = []
        metadata = {"update_enabled": False}
        fake_aqt = types.ModuleType("aqt")
        fake_aqt.gui_hooks = types.SimpleNamespace(
            addon_manager_will_install_addon=hooks
        )
        fake_aqt.mw = types.SimpleNamespace(
            addonManager=types.SimpleNamespace(
                addonMeta=lambda _addon_id: metadata,
                writeAddonMeta=lambda _addon_id, _metadata: None,
            )
        )
        fake_gui = types.ModuleType(f"{package}.gui")
        fake_gui.init_gui = lambda: gui_starts.append(True)
        fake_server = types.ModuleType(f"{package}.server")

        class FakeServerStartupError(RuntimeError):
            pass

        fake_server.ServerStartupError = FakeServerStartupError
        fake_server.run_server = lambda: starts.append(True)
        fake_server.stop_server = lambda: stops.append(True)
        modules = {
            "aqt": fake_aqt,
            f"{package}.gui": fake_gui,
            f"{package}.server": fake_server,
        }
        specification = importlib.util.spec_from_file_location(
            package,
            ROOT / "__init__.py",
            submodule_search_locations=[str(ROOT)],
        )
        assert specification is not None and specification.loader is not None
        installed_module = importlib.util.module_from_spec(specification)
        modules[package] = installed_module

        try:
            with patch.dict(sys.modules, modules), patch.dict(
                os.environ, {"LOCAL_AUDIO_FAST_STANDALONE": "0"}
            ):
                specification.loader.exec_module(installed_module)
                self.assertEqual(len(hooks), 1)
                hooks[0](object(), "another-addon")
                self.assertEqual(stops, [])
                hooks[0](object(), package)
        finally:
            for name in modules:
                sys.modules.pop(name, None)

        self.assertEqual(starts, [True])
        self.assertEqual(stops, [True])
        self.assertEqual(gui_starts, [True])

    def test_server_global_start_and_stop_are_idempotent(self) -> None:
        created = []

        class FakeRuntime:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs
                self.start_count = 0
                self.stop_count = 0
                created.append(self)

            def start_background(self) -> None:
                self.start_count += 1

            def stop(self) -> None:
                self.stop_count += 1

        original_runtime = server_module._runtime
        server_module._runtime = None
        try:
            with patch.object(server_module, "ServerRuntime", FakeRuntime), patch.object(
                server_module,
                "get_server_config",
                return_value={
                    "port": 5050,
                    "lookup_mode": "sqlite",
                    "response_cache_entries": 10,
                    "row_cache_entries": 20,
                },
            ):
                first = server_module.run_server()
                second = server_module.run_server()
                self.assertIs(first, second)
                self.assertEqual(len(created), 1)
                self.assertEqual(first.start_count, 1)
                server_module.stop_server()
                server_module.stop_server()
                self.assertEqual(first.stop_count, 1)
                self.assertIsNone(server_module.get_runtime())
        finally:
            server_module._runtime = original_runtime

    def test_gui_initialization_creates_one_menu_hook_and_action_set(self) -> None:
        hooks = []
        menus = []
        connections = []

        class FakeMenu:
            def __init__(self, title: str) -> None:
                self.title = title
                self.actions = []

            def addAction(self, action) -> None:
                self.actions.append(action)

        class FakeMenuTools:
            def addMenu(self, title: str):
                menu = FakeMenu(title)
                menus.append(menu)
                return menu

        class FakeAction:
            def __init__(self, title: str, parent) -> None:
                self.title = title
                self.parent = parent
                self.triggered = object()

        fake_mw = types.SimpleNamespace(
            form=types.SimpleNamespace(menuTools=FakeMenuTools()),
            taskman=types.SimpleNamespace(),
        )
        fake_aqt = types.ModuleType("aqt")
        fake_aqt.gui_hooks = types.SimpleNamespace(main_window_did_init=hooks)
        fake_aqt.mw = fake_mw
        fake_operations = types.ModuleType("aqt.operations")
        fake_operations.QueryOp = object
        fake_qt = types.ModuleType("aqt.qt")
        fake_qt.QAction = FakeAction
        fake_qt.QFileDialog = object
        fake_qt.qconnect = lambda signal, callback: connections.append(
            (signal, callback)
        )
        fake_utils = types.ModuleType("aqt.utils")
        fake_utils.askUser = lambda *args, **kwargs: False
        fake_utils.showInfo = lambda *args, **kwargs: None
        fake_utils.showWarning = lambda *args, **kwargs: None
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
            gui.init_gui()
            gui.init_gui()
        self.assertEqual(len(menus), 1)
        self.assertEqual(menus[0].title, "Local Audio Server")
        self.assertEqual(
            [action.title for action in menus[0].actions],
            [
                "Regenerate desktop database",
                "Import existing audio collection…",
                "Build/rebuild fast desktop audio pack",
                "Move verified loose audio to Trash…",
                "Restore/verify loose audio originals…",
                "Show statistics",
            ],
        )
        self.assertEqual(len(connections), 6)
        self.assertEqual(hooks, [gui.attempt_init_db_gui])
        gui._active_job = "another operation"
        gui.regenerate_database_operation()
        self.assertEqual(gui._active_job, "another operation")
        gui._finish_job()

        finished = []
        runtime = types.SimpleNamespace(
            store=types.SimpleNamespace(info=lambda: {"audioPack": None})
        )
        with patch.object(
            gui, "load_packed_only_state", return_value=None
        ), patch.object(gui, "get_runtime", return_value=runtime), patch.object(
            gui, "claim_automatic_pack_build", return_value="fingerprint"
        ), patch.object(
            gui,
            "inspect_installed_collection",
            return_value={"rows": 10, "source_folders": 2, "database_sources": 2},
        ), patch.object(
            gui, "askUser", return_value=False
        ), patch.object(
            gui,
            "finish_automatic_pack_build",
            side_effect=lambda *args: finished.append(args),
        ):
            gui.maybe_automatic_pack_build(confirm_existing=True)
        self.assertEqual(finished[0][1:], ("fingerprint", "declined"))
        self.assertIsNone(gui._active_job)

        with patch.object(
            gui,
            "load_packed_only_state",
            return_value={"status": "completed", "version": "0123456789abcdef"},
        ), patch.object(gui, "_claim_job") as claim:
            gui.regenerate_database_operation()
            gui.build_fast_pack_operation()
        claim.assert_not_called()

    def test_user_server_overlay_preserves_sources_and_validates_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default_path = root / "default.json"
            user_path = root / "user.json"
            sources = [
                {
                    "type": "jpod",
                    "id": "fixture",
                    "path": "user_files/fixture",
                    "display": "Fixture",
                }
            ]
            default_path.write_text(
                json.dumps(
                    {
                        "server": {
                            "port": 5050,
                            "lookup_mode": "sqlite",
                            "response_cache_entries": 100,
                            "row_cache_entries": 200,
                        },
                        "sources": sources,
                    }
                ),
                encoding="utf-8",
            )
            user_path.write_text(
                json.dumps(
                    {
                        "server": {
                            "port": 5059,
                            "response_cache_entries": 999,
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                config_module, "get_default_config_path", return_value=default_path
            ), patch.object(config_module, "get_config_path", return_value=user_path):
                merged = config_module.read_config()
                server = config_module.get_server_config()
            # a user config without a "sources" key keeps the defaults verbatim
            self.assertEqual(merged["sources"], sources)
            self.assertEqual(
                server,
                {
                    "port": 5059,
                    "lookup_mode": "sqlite",
                    "response_cache_entries": 999,
                    "row_cache_entries": 200,
                },
            )

            user_path.write_text(
                json.dumps(
                    {
                        "server": {
                            "port": "invalid",
                            "lookup_mode": "unsupported",
                            "response_cache_entries": -5,
                            "row_cache_entries": 99999999,
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                config_module, "get_default_config_path", return_value=default_path
            ), patch.object(config_module, "get_config_path", return_value=user_path):
                validated = config_module.get_server_config()
            self.assertEqual(
                validated,
                {
                    "port": 5050,
                    "lookup_mode": "sqlite",
                    "response_cache_entries": 0,
                    "row_cache_entries": 262144,
                },
            )


    def test_pinned_user_sources_keep_their_paths_and_gain_new_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default_path = root / "default.json"
            user_path = root / "user.json"
            default_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "type": "jpod",
                                "id": "jpod",
                                "path": "user_files/jpod_files",
                                "display": "Jpod101",
                            },
                            {
                                "type": "flat",
                                "id": "forvo_ext",
                                "path": "user_files/forvo_ext_files",
                                "display": "Forvo Ext",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            user_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "type": "jpod",
                                "id": "jpod",
                                "path": "D:/audio/jpod_files",
                                "display": "Jpod101",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                config_module, "get_default_config_path", return_value=default_path
            ), patch.object(config_module, "get_config_path", return_value=user_path):
                merged = config_module.read_config()
        self.assertEqual(
            [(item["id"], item["path"]) for item in merged["sources"]],
            [
                ("jpod", "D:/audio/jpod_files"),
                ("forvo_ext", "user_files/forvo_ext_files"),
            ],
        )

    def test_invalid_user_sources_fall_back_to_the_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default_path = root / "default.json"
            user_path = root / "user.json"
            sources = [
                {
                    "type": "jpod",
                    "id": "jpod",
                    "path": "user_files/jpod_files",
                    "display": "Jpod101",
                }
            ]
            default_path.write_text(
                json.dumps({"sources": sources}), encoding="utf-8"
            )
            user_path.write_text(
                json.dumps({"sources": "not a list"}), encoding="utf-8"
            )
            with patch.object(
                config_module, "get_default_config_path", return_value=default_path
            ), patch.object(config_module, "get_config_path", return_value=user_path):
                merged = config_module.read_config()
        self.assertEqual(merged["sources"], sources)


if __name__ == "__main__":
    unittest.main()
