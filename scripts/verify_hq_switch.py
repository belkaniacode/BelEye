#!/usr/bin/env python3
"""Hardware verification for the "high quality everywhere" switch.

Phase 1 — instant expand. With HQ on, expanding and collapsing a tile must
change nothing: no stream switch, no ffmpeg restart, no pause in chunk flow.
That is the whole user-visible promise of the feature.

Phase 2 — serialized switching. Toggling HQ with every tile streaming must
switch them one at a time (never two warm sessions at once) and no tile may
leave "live".

Phase 3 — soak with Main on every channel. This is the risky configuration
NvrConfig.prefer_substream exists to avoid, so measure it rather than assume.

Each phase runs in its own QEventLoop with its timers explicitly stopped —
a leftover singleShot from an earlier phase would quit the next one's loop.

Usage:
    .venv/bin/python scripts/verify_hq_switch.py [soak_seconds]
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QSettings, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import nvr_config as nvrcfg  # noqa: E402
from ui.prefs import KEY_HQ_ALL, prefs  # noqa: E402
from ui.theme import theme  # noqa: E402

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s %(name)s %(message)s")

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def wait(ms: int) -> None:
    loop = QEventLoop()
    t = QTimer()
    t.setSingleShot(True)
    t.timeout.connect(loop.quit)
    t.start(ms)
    loop.exec()
    t.stop()


def nvr_tiles(win):
    return [t for t in win.grid._tiles.values() if hasattr(t, "_current_stream")]


def snapshot(tiles):
    return {
        t._tile_id: {
            "stream": t._current_stream,
            "chunks": t._chunk_count,
            "status": t._overlay._status,
            "starts": getattr(t.player, "process_starts", None),
            "reconnecting": t._reconnect_timer.isActive(),
        }
        for t in tiles
    }


def phase1_instant_expand(app, win) -> None:
    print("\n== phase 1: expand must be instant with HQ on ==")
    tiles = nvr_tiles(win)
    before = snapshot(tiles)
    live = [k for k, v in before.items() if v["status"] == "live"]
    check(len(live) == len(tiles), "all tiles live before the test",
          f"{len(live)}/{len(tiles)}")
    check(all(v["stream"] == "Main" for v in before.values()),
          "every tile already on Main",
          str(sorted({v["stream"] for v in before.values()})))

    target = tiles[0]._tile_id
    win.grid.show_single(target)
    check(len(win.grid._switch_queue) == 0 and win.grid._switching is None,
          "expand enqueued no stream switch",
          f"queue={len(win.grid._switch_queue)} switching={win.grid._switching}")
    wait(4000)
    mid = snapshot(tiles)

    win.grid.show_grid()
    check(len(win.grid._switch_queue) == 0 and win.grid._switching is None,
          "collapse enqueued no stream switch",
          f"queue={len(win.grid._switch_queue)} switching={win.grid._switching}")
    wait(4000)
    after = snapshot(tiles)

    for tid in live:
        b, m, a = before[tid], mid[tid], after[tid]
        check(b["chunks"] < m["chunks"] < a["chunks"],
              f"{tid[-4:]}: chunk flow never paused",
              f"{b['chunks']} -> {m['chunks']} -> {a['chunks']}")
        check(a["stream"] == "Main" and m["stream"] == "Main",
              f"{tid[-4:]}: stayed on Main")
        check(a["status"] == "live" and m["status"] == "live",
              f"{tid[-4:]}: stayed live", f"{m['status']} / {a['status']}")
        if b["starts"] is not None:
            check(b["starts"] == a["starts"],
                  f"{tid[-4:]}: decoder never restarted",
                  f"{b['starts']} -> {a['starts']}")


def phase2_serialized(app, win) -> None:
    print("\n== phase 2: toggling HQ switches one tile at a time ==")
    tiles = nvr_tiles(win)
    concurrent_max = {"n": 0}

    def sample():
        n = sum(1 for t in tiles if t._switch_client is not None)
        concurrent_max["n"] = max(concurrent_max["n"], n)

    sampler = QTimer()
    sampler.setInterval(200)
    sampler.timeout.connect(sample)
    sampler.start()

    print("  HQ off -> tiles drop to Extra1 one by one")
    prefs.set_hq_all(False)
    wait(45_000)
    down = snapshot(tiles)

    print("  HQ on  -> tiles climb back to Main one by one")
    prefs.set_hq_all(True)
    wait(45_000)
    up = snapshot(tiles)

    sampler.stop()

    check(concurrent_max["n"] <= 1, "never more than one warm session at a time",
          f"max concurrent = {concurrent_max['n']}")
    check(all(v["status"] == "live" for v in down.values()),
          "all tiles live after switching down",
          str({k[-4:]: v["status"] for k, v in down.items()}))
    check(all(v["status"] == "live" for v in up.values()),
          "all tiles live after switching up",
          str({k[-4:]: v["status"] for k, v in up.items()}))
    check(all(v["stream"] == "Main" for v in up.values()),
          "all tiles back on Main",
          str({k[-4:]: v["stream"] for k, v in up.items()}))


def phase3_soak(app, win, seconds: int) -> None:
    print(f"\n== phase 3: soak {seconds}s with Main on every channel ==")
    tiles = nvr_tiles(win)
    state = {"reconnects": 0, "drops": 0, "samples": 0, "stalled": set()}
    prev = {t._tile_id: t._chunk_count for t in tiles}

    def sample():
        state["samples"] += 1
        for t in tiles:
            if t._reconnect_timer.isActive():
                state["reconnects"] += 1
            if t._overlay._status != "live":
                state["drops"] += 1
            now = t._chunk_count
            if now == prev[t._tile_id]:
                state["stalled"].add(t._tile_id)
            prev[t._tile_id] = now

    sampler = QTimer()
    sampler.setInterval(5000)
    sampler.timeout.connect(sample)
    sampler.start()
    wait(seconds * 1000)
    sampler.stop()

    end = snapshot(tiles)
    print(f"  {state['samples']} samples over {seconds}s")
    tail = ", ".join(f"{k[-4:]}:{v['chunks']}" for k, v in end.items())
    print(f"  final chunks: {{{tail}}}")
    check(state["reconnects"] == 0, "no reconnect fired during the soak",
          f"{state['reconnects']} sample(s) saw a pending reconnect")
    check(state["drops"] == 0, "no tile left 'live'",
          f"{state['drops']} sample(s) saw a non-live tile")
    check(not state["stalled"], "no tile went a full 5s window without chunks",
          str(sorted(t[-4:] for t in state["stalled"])))


def main() -> int:
    soak_seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 180

    if not nvrcfg.load_nvrs():
        print("no NVR configured — nothing to verify")
        return 0

    store = QSettings("BelEye", "BelEye")
    original = store.value(KEY_HQ_ALL)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    theme.apply(app, "dark", source="verify")

    # Start with HQ already on so the tiles are born on Main — phase 1 is
    # about the steady state, not about the switch.
    store.setValue(KEY_HQ_ALL, True)
    store.sync()

    from ui.main_window import MainWindow
    win = MainWindow()
    win.resize(1280, 760)
    win.show()

    print("warming up tiles on Main (30 s)...")
    wait(30_000)

    try:
        phase1_instant_expand(app, win)
        phase2_serialized(app, win)
        phase3_soak(app, win, soak_seconds)
    finally:
        win.close()
        restore = QSettings("BelEye", "BelEye")
        if original is None:
            restore.remove(KEY_HQ_ALL)
        else:
            restore.setValue(KEY_HQ_ALL, original)
        restore.sync()

    print("\n" + ("ALL CHECKS PASSED" if not failures
                  else f"{len(failures)} FAILURE(S): {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
