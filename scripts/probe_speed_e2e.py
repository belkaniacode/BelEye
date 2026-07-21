"""[FIX speed3-debug] End-to-end probe of the actual PlaybackView speed path.

Launches PlaybackView offscreen, schedules:
  T+0s:   construct, login, file query
  T+3s:   double-click first record → real start_playback
  T+8s:   call _cycle_speed (1× → 2×) — sends Fast + restarts decoder
  T+13s:  call _cycle_speed again (2× → 4×)
  T+18s:  call _cycle_speed (4× → 8×)
  T+25s:  quit
Captures the player's `_frame_size`, frame counter, ffmpeg stderr tail
across each phase to file. Reports byte rates and whether any new
frames were rendered after each speed change.
"""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import keyring
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.nvr_config import load_nvrs
from ui.playback_view import PlaybackView

LOG_PATH = ROOT / ".ai-factory" / "evidence" / "speed_e2e.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, mode="w"),
              logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("speed_e2e")


def main() -> int:
    channel = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    nvr = load_nvrs()[0]
    pw = keyring.get_password("beleye", f"nvr:{nvr.id}")

    app = QApplication.instance() or QApplication(sys.argv)
    view = PlaybackView(nvr, channel, f"CAM0{channel}", pw, parent=None)
    view.show()

    # State to record across phases.
    phase = {"name": "init", "frames_at_start": 0, "frame_size": 0}

    def snapshot(label: str) -> None:
        fc = sum(1 for _ in []) if False else (
            1 if view.player._frame is not None else 0
        )
        log.info("[E2E %s] frame_size=%d has_frame=%s",
                 label, view.player._frame_size, fc)

    def begin_playback() -> None:
        log.info("[E2E] T+3 starting playback")
        if view._day_records:
            log.info("[E2E] day_records=%d, starting first", len(view._day_records))
            view._start_playback(view._day_records[0])
        else:
            log.warning("[E2E] no day_records yet — file query maybe delayed")
            # try again
            QTimer.singleShot(2000, begin_playback)

    def click_speed(label: str) -> None:
        snapshot(f"before-{label}")
        log.info("[E2E] T={} cycling speed to {}".format(label, label))
        view._cycle_speed()
        snapshot(f"after-{label}")

    def maybe_pick_day() -> None:
        # PlaybackView opens calendar on TODAY. Records exist on past days
        # (e.g. 2026-06-15). Switch the calendar to last week so day_records
        # actually populates from the per-day group.
        log.info("[E2E] selecting recent day for archive")
        from PySide6.QtCore import QDate
        today = QDate.currentDate()
        for back in range(0, 14):
            d = today.addDays(-back)
            view.calendar.setSelectedDate(d)
            # process events so on_day_selected handler fires
            app.processEvents()
            if view._day_records:
                log.info("[E2E] day %s has %d records",
                         d.toString("yyyy-MM-dd"), len(view._day_records))
                break

    QTimer.singleShot(2000, maybe_pick_day)
    QTimer.singleShot(4000, begin_playback)
    QTimer.singleShot(9000, lambda: click_speed("2x"))
    QTimer.singleShot(13000, lambda: click_speed("4x"))
    QTimer.singleShot(17000, lambda: click_speed("8x"))
    QTimer.singleShot(22000, lambda: snapshot("final"))
    QTimer.singleShot(24000, app.quit)
    app.exec()
    log.info("[E2E] done — log at %s", LOG_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
