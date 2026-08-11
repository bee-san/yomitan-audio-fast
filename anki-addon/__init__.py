"""Fast desktop drop-in for Local Audio Server for Yomichan/Yomitan."""

import os


if __name__ != "plugin" and os.environ.get("LOCAL_AUDIO_FAST_STANDALONE") != "1":
    from .gui import init_gui
    from .server import run_server

    run_server()
    init_gui()
