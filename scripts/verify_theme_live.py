#!/usr/bin/env python3
"""Live hardware check: flipping the theme must not disturb streaming.

Re-applying an application stylesheet re-polishes every widget. If that
tore down or reset the FFmpegPlayer render widget, the picture would freeze
or the tile would reconnect — exactly the class of regression the
fix/live-stability-quality work just eliminated.

PASS when, across a theme flip: every tile's chunk counter keeps climbing,
no tile status drops out of "live", and no reconnect is scheduled.

Usage:
    .venv/bin/python scripts/verify_theme_live.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import nvr_config as nvrcfg  # noqa: E402
from app import secrets as keystore  # noqa: E402
from app.nvr_config import nvr_keyring_user  # noqa: E402
from ui.theme import theme  # noqa: E402

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s %(name)s %(message)s")


def wait(ms: int) -> None:
    loop = QEventLoop()
    t = QTimer()
    t.setSingleShot(True)
    t.timeout.connect(loop.quit)
    t.start(ms)
    loop.exec()
    t.stop()


def snapshot(tiles) -> dict[str, tuple[int, str, bool]]:
    return {
        t._tile_id: (
            t._chunk_count,
            t._overlay._status,
            t._reconnect_timer.isActive(),
        )
        for t in tiles
    }


def main() -> int:
    nvrs = nvrcfg.load_nvrs()
    if not nvrs:
        print("no NVR configured — nothing to verify")
        return 0
    nvr = nvrs[0]
    pwd = keystore.get_password(nvr_keyring_user(nvr.id))
    if not pwd:
        print("no stored password for the NVR — cannot verify")
        return 1

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    theme.apply(app, "dark", source="verify")

    from ui.main_window import MainWindow
    win = MainWindow()
    win.resize(1200, 700)
    win.show()

    print("warming up tiles (20 s)...")
    wait(20_000)

    tiles = [t for t in win.grid._tiles.values() if hasattr(t, "_chunk_count")]
    if not tiles:
        print("no NVR tiles built — nothing to verify")
        return 1

    before = snapshot(tiles)
    live_before = [k for k, v in before.items() if v[1] == "live"]
    print(f"before flip: {len(live_before)}/{len(tiles)} live, "
          f"chunks={{{', '.join(f'{k[-4:]}:{v[0]}' for k, v in before.items())}}}")
    if not live_before:
        print("FAIL — no tile reached 'live' during warm-up; cannot judge the flip")
        return 1

    print("--> flipping to LIGHT")
    theme.apply(app, "light", source="verify")
    wait(6_000)
    mid = snapshot(tiles)

    print("--> flipping back to DARK")
    theme.apply(app, "dark", source="verify")
    wait(6_000)
    after = snapshot(tiles)

    print(f"after flips: chunks="
          f"{{{', '.join(f'{k[-4:]}:{v[0]}' for k, v in after.items())}}}")

    ok = True
    for tid in live_before:
        c0, _s0, _r0 = before[tid]
        c1, s1, r1 = mid[tid]
        c2, s2, r2 = after[tid]
        if not (c0 < c1 < c2):
            print(f"  FAIL {tid}: chunk flow stalled ({c0} -> {c1} -> {c2})")
            ok = False
        if s1 != "live" or s2 != "live":
            print(f"  FAIL {tid}: status left 'live' ({s1} / {s2})")
            ok = False
        if r1 or r2:
            print(f"  FAIL {tid}: a reconnect was scheduled")
            ok = False
        if ok:
            print(f"  PASS {tid}: {c0} -> {c1} -> {c2} chunks, stayed live")

    win.close()
    print("\n" + ("THEME FLIP IS STREAM-SAFE" if ok else "REGRESSION DETECTED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
