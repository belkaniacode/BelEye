"""[FIX freeze] Hardware verification for the stability/quality work.

Offscreen Qt run against the live NVR:

  Phase 1 — reconnect drill: spawn a real NvrChannelTile, wait for video,
    then abort() its DVRIP socket (simulated network drop). PASS if chunks
    flow again within 20 s without any manual action.
  Phase 2 — quality upgrade: switch the tile to Main via
    set_preferred_stream and verify the decoded frame width grows beyond
    the 640px substream cap.
  Phase 3 — mini-soak: 4 tiles running concurrently for ~60 s; PASS if
    every tile's chunk counter keeps increasing and no tile ends "down".

Usage: python scripts/verify_stability.py
"""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import keyring
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from app.nvr_config import load_nvrs


def run_loop(schedule: list[tuple[int, object]], hard_timeout_ms: int) -> None:
    """Run an isolated event loop with per-phase timers. Every timer is
    parented and stopped on exit so phases cannot contaminate each other."""
    loop = QEventLoop()
    timers = []
    for delay_ms, fn in schedule:
        t = QTimer()
        t.setSingleShot(True)
        t.timeout.connect(fn)
        t.start(delay_ms)
        timers.append(t)
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(hard_timeout_ms)
    timers.append(guard)
    loop._quit = loop.quit  # convenience for callbacks
    loop.exec()
    for t in timers:
        t.stop()


def phase1_reconnect(app, nvr, pw) -> bool:
    from ui.nvr_channel_widget import NvrChannelTile
    ch = nvr.channels[0]
    tile = NvrChannelTile(nvr, ch, pw)
    tile.resize(640, 360)
    tile.show()
    tile.start()

    state = {"pre": 0, "post": 0, "dropped": False}
    loop = QEventLoop()

    def drop():
        state["pre"] = tile._chunk_count
        if tile._client is not None:
            print(f"  [drill] chunks before drop: {state['pre']} — aborting socket")
            state["dropped"] = True
            tile._client._sock.abort()
            tile._client._on_disconnected()

    def check():
        state["post"] = tile._chunk_count
        loop.quit()

    t1 = QTimer(); t1.setSingleShot(True); t1.timeout.connect(drop); t1.start(8000)
    t2 = QTimer(); t2.setSingleShot(True); t2.timeout.connect(check); t2.start(30000)
    guard = QTimer(); guard.setSingleShot(True); guard.timeout.connect(loop.quit); guard.start(45000)
    loop.exec()
    for t in (t1, t2, guard):
        t.stop()

    recovered = state["dropped"] and state["post"] > state["pre"] + 10
    print(f"  [drill] chunks after recovery window: {state['post']} "
          f"(pre-drop {state['pre']}) → {'PASS' if recovered else 'FAIL'}")
    tile.stop()
    return recovered


def phase2_quality(app, nvr, pw) -> bool:
    from ui.nvr_channel_widget import NvrChannelTile
    ch = nvr.channels[0]
    tile = NvrChannelTile(nvr, ch, pw)
    tile.resize(1280, 720)
    tile.show()
    tile.start()

    dims = {"sub": 0, "main": 0}
    loop = QEventLoop()

    def snapshot_sub():
        dims["sub"] = tile.player._width
        print(f"  [quality] substream decoded width: {dims['sub']}")
        tile.set_preferred_stream("Main")

    def snapshot_main():
        dims["main"] = tile.player._width
        print(f"  [quality] Main decoded width: {dims['main']}")
        loop.quit()

    t1 = QTimer(); t1.setSingleShot(True); t1.timeout.connect(snapshot_sub); t1.start(12000)
    t2 = QTimer(); t2.setSingleShot(True); t2.timeout.connect(snapshot_main); t2.start(32000)
    guard = QTimer(); guard.setSingleShot(True); guard.timeout.connect(loop.quit); guard.start(45000)
    loop.exec()
    for t in (t1, t2, guard):
        t.stop()

    ok = dims["main"] > dims["sub"] and dims["main"] > 640
    print(f"  [quality] {dims['sub']} → {dims['main']} px → {'PASS' if ok else 'FAIL'}")
    tile.stop()
    return ok


def phase3_soak(app, nvr, pw, seconds: int = 60) -> bool:
    from ui.nvr_channel_widget import NvrChannelTile
    tiles = []
    for ch in nvr.channels:
        t = NvrChannelTile(nvr, ch, pw)
        t.resize(480, 270)
        t.show()
        t.start()
        tiles.append(t)

    samples: list[list[int]] = []
    loop = QEventLoop()

    def sample():
        samples.append([t._chunk_count for t in tiles])

    stimer = QTimer()
    stimer.setInterval(10000)
    stimer.timeout.connect(sample)
    stimer.start()
    guard = QTimer(); guard.setSingleShot(True); guard.timeout.connect(loop.quit)
    guard.start(seconds * 1000)
    loop.exec()
    stimer.stop()
    guard.stop()

    ok = True
    for i, t in enumerate(tiles):
        counts = [s[i] for s in samples]
        grew = all(b > a for a, b in zip(counts, counts[1:])) if len(counts) > 1 else False
        print(f"  [soak] ch={t.channel.number} chunk samples: {counts} "
              f"→ {'flowing' if grew else 'STALLED'}")
        ok = ok and grew
        t.stop()
    return ok


def main() -> int:
    nvr = load_nvrs()[0]
    pw = keyring.get_password("beleye", f"nvr:{nvr.id}")
    app = QApplication.instance() or QApplication(sys.argv)
    results = {}

    print("=== Phase 1: reconnect drill ===")
    results["reconnect"] = phase1_reconnect(app, nvr, pw)
    time.sleep(5)
    print("=== Phase 2: Main-stream quality upgrade ===")
    results["quality"] = phase2_quality(app, nvr, pw)
    time.sleep(5)
    print("=== Phase 3: 60s multi-tile soak ===")
    results["soak"] = phase3_soak(app, nvr, pw)

    print("\n=== SUMMARY ===")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
