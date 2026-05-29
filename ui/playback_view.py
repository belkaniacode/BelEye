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

from PySide6.QtCore import QDate, Qt, Signal
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
from dvrip.client import DvripClient, FileRecord
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

        self.setWindowTitle(
            f"Архив — {nvr.name} / {channel_name} / "
            f"{QDate.currentDate().toString('yyyy-MM-dd')}"
        )
        self.resize(1100, 680)

        # Left column: calendar + file list
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        self.calendar.selectionChanged.connect(self._on_day_selected)
        self.calendar.currentPageChanged.connect(lambda y, m: self._load_month(y, m))

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
        self._status = QLabel("Выберите запись в списке или кликните по таймлайну.")
        self._status.setStyleSheet("color: #94a3b8;")

        transport = QHBoxLayout()
        transport.addWidget(self.btn_play)
        transport.addWidget(self.btn_stop)
        transport.addWidget(self.btn_speed)
        transport.addStretch(1)
        transport.addWidget(self.btn_export)

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

        # DVRIP session dedicated to this archive window.
        self._client = DvripClient(self, auto_discover=False)
        self._client.loginOk.connect(self._on_login)
        self._client.loginFailed.connect(lambda r: self._set_status(f"Логин отклонён: {r}"))
        self._client.error.connect(lambda e: self._set_status(f"Сеть: {e}"))
        self._client.fileList.connect(self._on_file_list)
        self._client.connect_to(nvr.host, nvr.port, nvr.username, password)

    # ----- session / queries -------------------------------------------

    def _on_login(self, _sid: int) -> None:
        d = self.calendar.selectedDate()
        self._load_month(d.year(), d.month())

    def _load_month(self, year: int, month: int) -> None:
        if not self._client:
            return
        first = datetime(year, month, 1, 0, 0, 0)
        nxt = datetime(year + (month == 12), (month % 12) + 1, 1)
        last = nxt - timedelta(seconds=1)
        self._loading_scope = "month"
        self._set_status(f"Загрузка записей за {year}-{month:02d}…")
        self._client.query_files(self.channel_number, first, last)

    def _on_file_list(self, records: list) -> None:
        if self._loading_scope == "month":
            self._month_records = records
            self._highlight_days()
            self._refresh_day()
        else:
            self._day_records = records
            self._populate_day(records)

    def _highlight_days(self) -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#3b82f6"))
        fmt.setFontWeight(75)
        days_with = {r.begin.date() for r in self._month_records}
        for d in days_with:
            self.calendar.setDateTextFormat(QDate(d.year, d.month, d.day), fmt)
        log.info("[PB] month days_with_records=%d", len(days_with))

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

    def _populate_day(self, records: list[FileRecord]) -> None:
        self.file_list.clear()
        for rec in records:
            label = f"{rec.begin:%H:%M:%S}–{rec.end:%H:%M:%S}   ({rec.size // 1024} KB)"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, rec)
            self.file_list.addItem(item)
        self.timeline.set_day(self.calendar.selectedDate(), records)
        self.setWindowTitle(
            f"Архив — {self.nvr.name} / {self.channel_name} / "
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
        # NOTE: OPPlayBack streaming is wired once device opcodes are verified.
        # For now we surface the selection so the UI flow is testable.
        log.info("[PB] start playback file=%s seek=%s", rec.file_name, seek_to)
        self._set_status(
            f"Воспроизведение: {rec.begin:%H:%M:%S}–{rec.end:%H:%M:%S} "
            "(стриминг подключается следующей задачей)"
        )
        self.timeline.set_cursor(seek_to or rec.begin)

    def _on_play_pause(self) -> None:
        self._start_playback(self._selected_record())

    def _on_stop(self) -> None:
        self.player.stop()
        self.timeline.set_cursor(None)
        self._set_status("Остановлено.")

    def _cycle_speed(self) -> None:
        self._speed_idx = (self._speed_idx + 1) % len(self._speeds)
        self.btn_speed.setText(f"{self._speeds[self._speed_idx]}×")

    def _on_export(self) -> None:
        self._set_status("Экспорт будет доступен после подключения стриминга.")

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
        try:
            self.player.stop()
            if self._client:
                self._client.close()
                self._client.deleteLater()
                self._client = None
        except Exception:
            log.exception("[PB] close failed")
        super().closeEvent(event)
