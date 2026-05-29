"""Probe an NVR over DVRIP: connect, login, discover channels, exit.

Usage:
    .venv/bin/python scripts/probe_nvr.py HOST[:PORT] USER PASSWORD

Examples:
    .venv/bin/python scripts/probe_nvr.py 192.168.0.60 admin secret
    .venv/bin/python scripts/probe_nvr.py 192.168.0.60:34567 admin ""

Logs everything at DEBUG so the DVRIP packet exchange is visible.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication, QTimer

from dvrip.client import DEFAULT_PORT, DvripClient


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__)
        return 2

    host_port = argv[1]
    if ":" in host_port:
        host, port_str = host_port.rsplit(":", 1)
        port = int(port_str)
    else:
        host = host_port
        port = DEFAULT_PORT
    user = argv[2]
    password = argv[3]

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    app = QCoreApplication(argv)
    client = DvripClient()

    state = {"done": False, "exit_code": 1}

    def finish(code: int) -> None:
        if state["done"]:
            return
        state["done"] = True
        state["exit_code"] = code
        client.close()
        QTimer.singleShot(200, app.quit)

    def on_login_ok(sid: int) -> None:
        print(f"\n>>> LOGIN OK  session_id=0x{sid:08x}\n")

    def on_login_failed(reason: str) -> None:
        print(f"\n>>> LOGIN FAILED: {reason}\n")
        finish(1)

    def on_channels(channels: list) -> None:
        print(f"\n>>> CHANNELS ({len(channels)}):")
        for ch in channels:
            print(f"     ch {ch.number:>2}  {ch.name}")
        print()
        finish(0)

    def on_error(msg: str) -> None:
        print(f"\n>>> SOCKET ERROR: {msg}\n")
        finish(1)

    client.loginOk.connect(on_login_ok)
    client.loginFailed.connect(on_login_failed)
    client.channelsDiscovered.connect(on_channels)
    client.error.connect(on_error)

    client.connect_to(host, port, user, password)

    # Hard timeout so the script never hangs.
    QTimer.singleShot(15_000, lambda: finish(1) if not state["done"] else None)

    app.exec()
    return state["exit_code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
