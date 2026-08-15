from __future__ import annotations

import socket
import subprocess
import sys
import unittest

from pathlib import Path


ROOT = Path(__file__).parents[1]


class StandaloneStartupTests(unittest.TestCase):
    """The standalone CLI must fail a blocked start with friendly guidance.

    A bind failure (most often the port already in use) should not dump a raw
    Python traceback. The CLI leads with what happened and what to do, keeps the
    raw OS detail as technical detail, and exits with a non-zero status.
    """

    def test_occupied_port_exits_nonzero_with_actionable_guidance(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            port = holder.getsockname()[1]
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "standalone.py"),
                    "--root",
                    str(ROOT),
                    "--port",
                    str(port),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            holder.close()
        # Non-zero exit: the start genuinely failed.
        self.assertNotEqual(completed.returncode, 0)
        combined = completed.stdout + completed.stderr
        lowered = combined.lower()
        # Friendly, actionable lead — not a bare traceback.
        self.assertIn("local audio server could not start", lowered)
        self.assertIn("port", lowered)
        self.assertNotIn("traceback (most recent call last)", lowered)
        # Raw OS detail retained for support.
        self.assertIn("Technical detail", combined)
        self.assertIn("address already in use", lowered)


if __name__ == "__main__":
    unittest.main()
