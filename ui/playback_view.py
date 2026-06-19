"""Archive playback window: calendar + per-day timeline + file list + player.

Layout (industry-standard, à la SmartPSS / iVMS / XMEye PC):

  +---------------------+-----------------------------+
  | QCalendarWidget     |   FFmpegPlayer (pipe)       |
  | (days w/ records    |                             |
  |  highlighted)       |                             |
  +---------------------+-----------------------------+
  | file list (day)     |   transport: play/pause...  |
  +---------------------+-----------------------------+
  |          _DayTimeline (00:00 .. 24:00)            |
  +---------------------------------------------------+

Live archive streaming (OPPlayBack) is wired in a later task once the
device opcodes are verified; this view already drives file discovery via
the hardware-verified OPFileQuery, the calendar highlight, and the
timeline.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from PySide6.QtCore import QDate, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.nvr_config import NvrConfig
from PySide6.QtWidgets import QFileDialog, QMessageBox

from dvrip.client import DvripClient, FileRecord
from dvrip.sofia_frame import SofiaFrameParser, detect_codec
from video.export import MP4Exporter
from video.ffmpeg_player import FFmpegPlayer

log = logging.getLogger(__name__)


class _DayTimeline(QWidget):
    """Horizontal 00:00–24:00 bar with filled blocks where records exist."""

    seekRequested = Signal(datetime)  # absolute time the user clicked

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(46)
        self.setMouseTracking(True)
        self._day: QDate = QDate.currentDate()
        self._records: list[FileRecord] = []
        self._cursor: datetime | None = None

    def set_day(self, day: QDate, records: list[FileRecord]) -> None:
        self._day = day
        self._records = records
        self.update()

    def set_cursor(self, when: datetime | None) -> None:
        self._cursor = when
        self.update()

    def _x_for(self, frac: float) -> int:
        return int(frac * (self.width() - 1))

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0f1115"))
        track_top, track_h = 14, 18
        w = self.width()
        # base track
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#1b2129"))
        p.drawRect(0, track_top, w, track_h)
        # hour ticks + labels (every 3h)
        p.setPen(QColor("#3a424d"))
        for h in range(0, 25, 3):
            x = self._x_for(h / 24.0)
            p.drawLine(x, track_top, x, track_top + track_h)
            p.drawText(min(max(x - 12, 0), w - 24), track_top + track_h + 12, f"{h:02d}:00")
        # record blocks
        day_start = datetime.combine(self._day.toPython(), time.min)
        p.setBrush(QColor("#2563eb"))
        p.setPen(Qt.NoPen)
        for rec in self._records:
            f0 = max(0.0, (rec.begin - day_start).total_seconds() / 86400.0)
            f1 = min(1.0, (rec.end - day_start).total_seconds() / 86400.0)
            if f1 <= f0:
                continue
            x0, x1 = self._x_for(f0), self._x_for(f1)
            p.drawRect(x0, track_top, max(2, x1 - x0), track_h)
        # playback cursor
        if self._cursor is not None:
            frac = (self._cursor - day_start).total_seconds() / 86400.0
            if 0 <= frac <= 1:
                x = self._x_for(frac)
                p.setPen(QColor("#ef4444"))
                p.drawLine(x, track_top - 4, x, track_top + track_h + 4)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self.width() <= 1:
            return
        frac = min(1.0, max(0.0, event.position().x() / (self.width() - 1)))
        day_start = datetime.combine(self._day.toPython(), time.min)
        when = day_start + timedelta(seconds=frac * 86400.0)
        log.info("[PB] timeline seek -> %s", when)
        self.seekRequested.emit(when)


class PlaybackView(QWidget):
    """Top-level archive window for one (NVR, channel)."""

    def __init__(
        self,
        nvr: NvrConfig,
        channel_number: int,
        channel_name: str,
        password: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.Window)
        self.nvr = nvr
        self.channel_number = channel_number
        self.channel_name = channel_name
        self._password = password
        self._month_records: list[FileRecord] = []
        self._day_records: list[FileRecord] = []
        self._loading_scope = "month"  # "month" | "day"

        # [FIX uxbug] Always include the NVR port number — after drag-drop
        # reorder the camera at "grid position 2" may map to port 4. The
        # user repeatedly hit "I clicked the 2nd camera but got CAM04 / port
        # 4 records" — having the port in the title settles it.
        self.setWindowTitle(
            f"Архив — {nvr.name} / {channel_name} (порт #{channel_number}) / "
            f"{QDate.currentDate().toString('yyyy-MM-dd')}"
        )
        # [FIX channel] Some Xiongmai/HVR firmware always replies with the
        # CAM01 stream regardless of which channel's file was requested.
        # We tested four different protocol variants — all returned the
        # same CAM01 watermark. The file LIST is correct per channel; only
        # the PLAYBACK content may be misrouted on affected devices.
        self._firmware_warning_shown = False
        self.resize(1100, 680)

        # Left column: calendar + file list
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        self.calendar.selectionChanged.connect(self._on_day_selected)
        # [C3] currentPageChanged fires when the user clicks the < / > arrows
        # AND when we programmatically jump (we block signals around those).
        self.calendar.currentPageChanged.connect(self._on_page_changed)
        # [C2/C3] Calendar nav fallback state.
        self._max_fallback_months = 6
        self._fallback_remaining = 0
        # Track the most-recent year/month known to contain data so we can
        # clamp forward navigation past it (calendars beyond hold no recs).
        self._latest_data_month: tuple[int, int] | None = None

        self.file_list = QListWidget()
        self.file_list.itemDoubleClicked.connect(self._on_file_activated)

        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(self.calendar)
        left.addWidget(QLabel("Записи за день:"))
        left.addWidget(self.file_list, 1)
        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setFixedWidth(340)

        # Right column: player + transport
        self.player = FFmpegPlayer(parent=self, input_mode="pipe", input_codec="h264")
        self.player.setMinimumSize(480, 320)

        self.btn_play = QPushButton("▶ Воспроизвести")
        self.btn_play.setCursor(Qt.PointingHandCursor)
        self.btn_play.clicked.connect(self._on_play_pause)
        self.btn_stop = QPushButton("■ Стоп")
        self.btn_stop.setCursor(Qt.PointingHandCursor)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_speed = QPushButton("1×")
        self.btn_speed.setCursor(Qt.PointingHandCursor)
        self.btn_speed.clicked.connect(self._cycle_speed)
        self.btn_export = QPushButton("Экспорт…")
        self.btn_export.setCursor(Qt.PointingHandCursor)
        self.btn_export.clicked.connect(self._on_export)
        # [D2] Always-visible Close button. The native window decoration is
        # easy to miss on some WMs, and during debugging "the window is
        # stuck" makes Stop+close-by-X feel painful.
        self.btn_close = QPushButton("× Закрыть")
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.setToolTip("Закрыть окно архива (Esc)")
        self.btn_close.clicked.connect(self.close)
        self._status = QLabel("Выберите запись в списке или кликните по таймлайну.")
        self._status.setStyleSheet("color: #94a3b8;")

        transport = QHBoxLayout()
        transport.addWidget(self.btn_play)
        transport.addWidget(self.btn_stop)
        transport.addWidget(self.btn_speed)
        transport.addStretch(1)
        transport.addWidget(self.btn_export)
        transport.addWidget(self.btn_close)

        self.timeline = _DayTimeline()
        self.timeline.seekRequested.connect(self._on_timeline_seek)

        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(self.player, 1)
        right.addLayout(transport)
        right.addWidget(self._status)
        right.addWidget(self.timeline)
        right_w = QWidget()
        right_w.setLayout(right)

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        root.addWidget(left_w)
        root.addWidget(right_w, 1)

        self._speed_idx = 0
        self._speeds = [1, 2, 4]

        # [FIX archive3] Streaming state — archive data flows on opcode 1422,
        # exposed via the ``playbackChunk`` signal (NOT ``videoChunk``, which
        # carries live MONITOR_DATA). Subscribing to the right signal is
        # what cleanly separates archive from live.
        self._parser: SofiaFrameParser | None = None
        self._codec_detected = False
        # [FIX codec] Small accumulator (capped at 256 KB ≈ 0.1 s of HEVC main
        # stream) so detect_codec gets at least one full I-frame before we
        # commit ffmpeg to a wrong codec. Replaces the prior "start on the
        # first chunk" path that crashed playback when the first NAL was an
        # IDR slice (no SPS/VPS) — see [FIX codec] in dvrip/sofia_frame.py.
        self._pending_es: bytearray = bytearray()
        self._codec_detect_cap: int = 256 * 1024
        # NVR-side "Stop then immediately Claim again" sometimes makes the
        # firmware drop the new claim. We delay the new playback by a few
        # hundred ms after a stop so the device is ready.
        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.timeout.connect(self._do_start_playback)
        self._restart_pending: tuple[FileRecord, datetime | None] | None = None
        self._playing_rec: FileRecord | None = None
        self._exporter: MP4Exporter | None = None

        # DVRIP session dedicated to this archive window.
        self._client = DvripClient(self, auto_discover=False)
        self._client.loginOk.connect(self._on_login)
        self._client.loginFailed.connect(lambda r: self._set_status(f"Логин отклонён: {r}"))
        self._client.error.connect(lambda e: self._set_status(f"Сеть: {e}"))
        self._client.fileList.connect(self._on_file_list)
        self._client.playbackChunk.connect(self._on_playback_chunk)
        self._client.connect_to(nvr.host, nvr.port, nvr.username, password)

    # ----- session / queries -------------------------------------------

    def _on_login(self, _sid: int) -> None:
        # [C2] Initial month load with a budget of fallbacks: try the
        # currently-shown month first, and if it comes back empty roll back
        # one month at a time up to ``_max_fallback_months`` to find data.
        d = self.calendar.selectedDate()
        self._fallback_remaining = self._max_fallback_months
        self._load_month(d.year(), d.month())

    def _load_month(self, year: int, month: int) -> None:
        if not self._client:
            return
        first = datetime(year, month, 1, 0, 0, 0)
        nxt = datetime(year + (month == 12), (month % 12) + 1, 1)
        last = nxt - timedelta(seconds=1)
        self._loading_scope = "month"
        self._set_status(f"Загрузка записей за {year}-{month:02d}…")
        # [FIX cap] DvripClient.query_files chunks under the 64-record cap.
        self._client.query_files(self.channel_number, first, last)

    def _on_page_changed(self, year: int, month: int) -> None:
        """User flipped the calendar page (< / >) OR we did programmatically.

        [C3] Clamp navigation past the latest-known data month. The user can
        still navigate freely backwards (history exploration), but jumping
        into the future just lands on empty calendars — confusing, so we
        snap back to the latest known month.
        """
        latest = self._latest_data_month
        if latest is not None and (year, month) > latest:
            log.info(
                "[C3] clamping calendar nav (%04d-%02d) -> latest data month %04d-%02d",
                year, month, latest[0], latest[1],
            )
            self.calendar.blockSignals(True)
            try:
                self.calendar.setCurrentPage(latest[0], latest[1])
            finally:
                self.calendar.blockSignals(False)
            return
        # Manual user nav resets the fallback budget — they want to see THIS
        # month, not be silently redirected.
        self._fallback_remaining = 0
        self._load_month(year, month)

    def _update_latest_data_month(self) -> None:
        if not self._month_records:
            return
        latest_day = max(r.begin.date() for r in self._month_records)
        candidate = (latest_day.year, latest_day.month)
        if self._latest_data_month is None or candidate > self._latest_data_month:
            self._latest_data_month = candidate
            log.info("[C3] latest data month is now %04d-%02d", *candidate)

    def _on_file_list(self, records: list) -> None:
        if self._loading_scope == "month":
            self._month_records = records
            # [C2] Auto-fallback to the previous month when the current
            # month has no data — typical for NVRs early in the month
            # while last month still has the bulk of recordings.
            if not records and self._fallback_remaining > 0:
                self._fallback_remaining -= 1
                cur = self.calendar.selectedDate()
                year = cur.year()
                month = cur.month() - 1
                if month < 1:
                    month = 12
                    year -= 1
                target = QDate(year, month, 1)
                log.info("[C2] empty month, falling back to %04d-%02d", year, month)
                self.calendar.blockSignals(True)
                try:
                    self.calendar.setCurrentPage(year, month)
                    self.calendar.setSelectedDate(target)
                finally:
                    self.calendar.blockSignals(False)
                self._load_month(year, month)
                return
            self._highlight_days()
            self._auto_select_latest_day_with_records()
            # [C3] Now that we know which months have data, clamp forward nav
            # so the user can't accidentally jump into the future and see
            # an empty calendar.
            self._update_latest_data_month()
            self._refresh_day()
        else:
            self._day_records = records
            self._populate_day(records)

    def _highlight_days(self) -> None:
        # [FIX uxbug] The old highlight used a blue foreground only, which on
        # dark themes was visually indistinguishable from the calendar's
        # "muted other-month" cells. We now paint a coloured background pill
        # so days with recordings are *unmistakable*.
        normal_fmt = QTextCharFormat()
        normal_fmt.setForeground(QColor("#ffffff"))
        normal_fmt.setBackground(QColor("#1e40af"))   # bold blue background
        normal_fmt.setFontWeight(75)
        # Days that have any event-triggered (motion/alarm/human) records get
        # an accent so the user can spot incident days at a glance.
        event_fmt = QTextCharFormat()
        event_fmt.setForeground(QColor("#0b0d10"))
        event_fmt.setBackground(QColor("#eab308"))    # amber for incident day
        event_fmt.setFontWeight(75)

        days_any = {r.begin.date() for r in self._month_records}
        days_event = {
            r.begin.date()
            for r in self._month_records
            if getattr(r, "event_type", "normal") != "normal"
        }
        # Apply normal-day format first, then overwrite with event format so
        # incident days win.
        for d in days_any:
            qd = QDate(d.year, d.month, d.day)
            self.calendar.setDateTextFormat(qd, normal_fmt)
        for d in days_event:
            qd = QDate(d.year, d.month, d.day)
            self.calendar.setDateTextFormat(qd, event_fmt)
        log.info(
            "[FIX uxbug] highlight days_with_records=%d (incident_days=%d)",
            len(days_any), len(days_event),
        )

    def _auto_select_latest_day_with_records(self) -> None:
        if not self._month_records:
            self._set_status("За этот месяц записей нет.")
            return
        latest = max(r.begin.date() for r in self._month_records)
        target = QDate(latest.year, latest.month, latest.day)
        if self.calendar.selectedDate() == target:
            return
        log.info("[FIX uxbug] auto-select latest day with records: %s", latest)
        # Block the selectionChanged loop so _refresh_day runs only once below.
        self.calendar.blockSignals(True)
        try:
            self.calendar.setSelectedDate(target)
        finally:
            self.calendar.blockSignals(False)

    def _on_day_selected(self) -> None:
        self._refresh_day()

    def _refresh_day(self) -> None:
        day = self.calendar.selectedDate().toPython()
        records = sorted(
            (r for r in self._month_records if r.begin.date() == day),
            key=lambda r: r.begin,
        )
        self._day_records = records
        self._populate_day(records)

    # [FIX events-preview] Color-coded markers retained only as accent dots
    # in the row label — the textual "Запись"/"Движение"/etc. labels were
    # misleading because on this NVR every recording is motion-triggered,
    # so the distinction was bogus. We keep the dot color as a subtle event-
    # type accent (yellow=motion, red=alarm, green=human, blue=continuous)
    # in case the firmware ever does report a more diverse mix.
    _EVENT_DOTS: dict[str, tuple[str, str]] = {
        "normal": ("●", "#3b82f6"),
        "motion": ("●", "#eab308"),
        "alarm":  ("●", "#dc2626"),
        "human":  ("●", "#22c55e"),
    }

    def _populate_day(self, records: list[FileRecord]) -> None:
        self.file_list.clear()
        for rec in records:
            etype = getattr(rec, "event_type", "normal")
            dot, color = self._EVENT_DOTS.get(etype, self._EVENT_DOTS["normal"])
            label = (
                f"{dot}  {rec.begin:%H:%M:%S}–{rec.end:%H:%M:%S}    "
                f"{rec.size // 1024} KB"
            )
            item = QListWidgetItem(label)
            item.setForeground(QColor(color))
            item.setData(Qt.UserRole, rec)
            self.file_list.addItem(item)
        self.timeline.set_day(self.calendar.selectedDate(), records)
        self.setWindowTitle(
            f"Архив — {self.nvr.name} / {self.channel_name} "
            f"(порт #{self.channel_number}) / "
            f"{self.calendar.selectedDate().toString('yyyy-MM-dd')}"
        )
        self._set_status(
            f"Записей за день: {len(records)}" if records else "Записей за этот день нет."
        )

    # ----- transport (streaming wired in a later task) ------------------

    def _selected_record(self) -> FileRecord | None:
        item = self.file_list.currentItem()
        if item is not None:
            return item.data(Qt.UserRole)
        return self._day_records[0] if self._day_records else None

    def _on_file_activated(self, _item: QListWidgetItem) -> None:
        self._start_playback(self._selected_record())

    def _on_timeline_seek(self, when: datetime) -> None:
        rec = next(
            (r for r in self._day_records if r.begin <= when <= r.end),
            None,
        )
        if rec is not None:
            self.timeline.set_cursor(when)
            self._start_playback(rec, seek_to=when)
        else:
            self._set_status("В этот момент записи нет.")

    def _start_playback(self, rec: FileRecord | None, seek_to: datetime | None = None) -> None:
        if rec is None:
            self._set_status("Нет записи для воспроизведения.")
            return
        if not self._client:
            self._set_status("Нет соединения с регистратором.")
            return
        # [FIX codec] Tear EVERYTHING down before the next claim — ffmpeg
        # subprocess, parser, accumulator, and tell the NVR to stop. Any
        # MONITOR_DATA chunks still in flight from the previous playback
        # would otherwise contaminate the new parser and corrupt detect_codec.
        try:
            self._client.stop_playback()
        except Exception:
            log.exception("[PB] stop_playback failed")
        self.player.stop()
        self._parser = None
        self._codec_detected = False
        self._pending_es.clear()
        self._playing_rec = None
        # Give the NVR a moment to actually free the playback session before
        # the new Claim arrives — sequential stop+claim is the usual cause of
        # the second playback never starting.
        self._restart_pending = (rec, seek_to)
        log.info("[FIX codec] PB scheduling start in 200 ms file=%s seek=%s",
                 rec.file_name, seek_to)
        self._restart_timer.start(200)

    def _do_start_playback(self) -> None:
        if self._restart_pending is None or not self._client:
            return
        rec, seek_to = self._restart_pending
        self._restart_pending = None
        self._parser = SofiaFrameParser()
        self._parser._name = f"pb:{rec.file_name.rsplit('/', 1)[-1]}"
        self._codec_detected = False
        self._pending_es.clear()
        self._playing_rec = rec
        log.info("[FIX codec] PB start playback file=%s seek=%s", rec.file_name, seek_to)
        # [FIX channel] Pass the channel — without it the NVR streams CAM01
        # for every playback request regardless of which file was named.
        self._client.start_playback(
            rec.file_name, rec.begin, rec.end, channel=self.channel_number
        )
        self.timeline.set_cursor(seek_to or rec.begin)
        self.btn_play.setText("⏸ Пауза")
        self._set_status(
            f"Воспроизведение: {rec.begin.strftime('%H:%M:%S')}–"
            f"{rec.end.strftime('%H:%M:%S')}"
        )

    def _on_playback_chunk(self, data: bytes) -> None:
        if not self._parser:
            # Chunks arriving between stop and (delayed) start — drop them so
            # they don't contaminate the next detection pass.
            return
        clean = self._parser.feed(data)
        if not clean:
            return
        if self._codec_detected:
            self.player.feed_bytes(clean)
            if self._exporter is not None:
                self._exporter.feed_bytes(clean)
            return

        # [FIX codec] Accumulate up to 256 KB and detect on the whole window,
        # then start ffmpeg. detect_codec now recognises both parameter sets
        # and IDR slices, so this normally succeeds inside the first chunk.
        # If even after the cap we cannot tell, default to HEVC — Xiongmai
        # main stream (which OPPlayBack always uses) is H.265 by default.
        self._pending_es.extend(clean)
        codec = detect_codec(bytes(self._pending_es))
        if codec is None:
            if len(self._pending_es) < self._codec_detect_cap:
                return
            codec = "hevc"
            log.warning(
                "[FIX codec] PB codec undetected after %d B buffered — defaulting hevc",
                len(self._pending_es),
            )
        buffered = bytes(self._pending_es)
        self._pending_es.clear()
        log.info(
            "[FIX codec] PB codec=%s, starting decoder (buffered %d B)",
            codec, len(buffered),
        )
        # [B2 audit] Dump the buffered first-frame elementary stream to disk
        # AND render a thumbnail (best-effort). The OSD watermark on the
        # thumbnail proves the playback came from the channel the user
        # actually clicked, not from CAM01. Path is greppable in the log
        # for field diagnostics.
        self._audit_first_frame(buffered, codec)
        self.player.set_input_codec(codec)
        self.player.start()
        self.player.feed_bytes(buffered)
        if self._exporter is not None:
            if self._exporter.start(self._exporter_out_path, codec):
                self._exporter.feed_bytes(buffered)
        self._codec_detected = True

    def _audit_first_frame(self, buffered: bytes, codec: str) -> None:
        """Persist first-frame ES + thumbnail under /tmp so the operator can
        confirm visually that the playback actually serves the requested
        channel. Best-effort: logs the path and any ffmpeg failure but never
        raises into the streaming path."""
        import os
        import shutil
        import subprocess
        try:
            stem = f"/tmp/beleye_audit_pb_ch{self.channel_number}_first"
            bin_path = f"{stem}.bin"
            png_path = f"{stem}.png"
            with open(bin_path, "wb") as f:
                f.write(buffered)
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                subprocess.run(
                    [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                     "-f", codec, "-i", bin_path, "-frames:v", "1",
                     "-vf", "scale=480:-2", png_path],
                    capture_output=True, timeout=6,
                )
            ok = os.path.exists(png_path) and os.path.getsize(png_path) > 1024
            log.info(
                "[FIX channel] PB first-frame audit ch=%d bin=%s png=%s ok=%s",
                self.channel_number, bin_path, png_path, ok,
            )
        except Exception:
            log.exception("[FIX channel] first-frame audit failed (non-fatal)")

    def _on_play_pause(self) -> None:
        self._start_playback(self._selected_record())

    def _on_stop(self) -> None:
        # [FIX codec] Cancel any pending delayed-start, otherwise it would
        # fire after the user pressed Stop and re-open a playback we just
        # tore down.
        self._restart_timer.stop()
        self._restart_pending = None
        if self._client:
            try:
                self._client.stop_playback()
            except Exception:
                log.exception("[PB] stop_playback failed")
        self.player.stop()
        self._parser = None
        self._pending_es.clear()
        self._codec_detected = False
        self._playing_rec = None
        self.timeline.set_cursor(None)
        self.btn_play.setText("▶ Воспроизвести")
        self._set_status("Остановлено.")

    def _cycle_speed(self) -> None:
        self._speed_idx = (self._speed_idx + 1) % len(self._speeds)
        self.btn_speed.setText(f"{self._speeds[self._speed_idx]}×")

    def _on_export(self) -> None:
        rec = self._selected_record()
        if rec is None:
            self._set_status("Выберите запись для экспорта.")
            return
        if self._exporter is not None:
            self._set_status("Экспорт уже идёт. Дождитесь завершения.")
            return
        suggested = (
            f"{self.nvr.name}_{self.channel_name}_"
            f"{rec.begin.strftime('%Y%m%d_%H%M%S')}.mp4"
        ).replace(" ", "_")
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт фрагмента в mp4", suggested, "MP4 video (*.mp4)"
        )
        if not path:
            return
        if not path.lower().endswith(".mp4"):
            path += ".mp4"
        # Restart playback from the start of the record so the exported file
        # is complete (the same chunks feed the player AND the exporter).
        self._exporter = MP4Exporter(self)
        self._exporter_out_path = path
        self._exporter.progress.connect(lambda line: self._set_status(f"Экспорт: {line}"))
        self._exporter.finished.connect(self._on_export_finished)
        self.btn_export.setEnabled(False)
        log.info("[export] queued out=%s file=%s", path, rec.file_name)
        self._start_playback(rec)
        self._set_status(f"Запись стартует для экспорта в {path}…")

    def _on_export_finished(self, ok: bool, msg: str) -> None:
        self.btn_export.setEnabled(True)
        if self._exporter is not None:
            self._exporter.deleteLater()
            self._exporter = None
        if ok:
            QMessageBox.information(self, "Экспорт", msg)
        else:
            QMessageBox.warning(self, "Экспорт", msg)
        self._set_status(msg)

    # ----- misc ---------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        if event.key() == Qt.Key_Space:
            self._on_play_pause()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        # [B3] Send an explicit OPPlayBack Stop BEFORE closing the socket so
        # the firmware frees the playback slot — re-opening the window
        # immediately after a close used to race a stale claim and get
        # Ret=103 back. Also cancel any pending restart timer.
        try:
            self._restart_timer.stop()
            self._restart_pending = None
            if self._client is not None:
                try:
                    self._client.stop_playback()
                except Exception:
                    log.exception("[B3] stop_playback in closeEvent failed")
            self.player.stop()
            if self._client:
                self._client.close()
                self._client.deleteLater()
                self._client = None
        except Exception:
            log.exception("[PB] close failed")
        super().closeEvent(event)
