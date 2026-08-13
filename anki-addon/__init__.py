"""Fast desktop drop-in for Local Audio Server for Yomichan/Yomitan."""

import os


if __name__ != "plugin" and os.environ.get("LOCAL_AUDIO_FAST_STANDALONE") != "1":
    from aqt import mw

    from .gui import init_gui
    from .server import run_server

    # A local same-ID .ankiaddon install preserves the old AnkiWeb metadata.
    # Disable automatic updates so the original add-on cannot overwrite this
    # replacement on the next update check.
    try:
        addon_id = __name__.split(".", 1)[0]
        metadata = mw.addonManager.addonMeta(addon_id)
        if metadata.get("update_enabled", True):
            metadata["update_enabled"] = False
            mw.addonManager.writeAddonMeta(addon_id, metadata)
    except (AttributeError, OSError, TypeError, ValueError):
        pass

    run_server()
    init_gui()
