"""
Event-driven RTSP player.

Implementation notes
--------------------
Previous iterations used QThread + subprocess + threading.Event. Under
real-world conditions (one broken camera + one working camera) the
amount of cross-thread signalling, the 12-second blocking wait for
dimensions parsing, and rapid worker recreation during reconnect loops
ended up freezing the GUI.

This version uses ``QProcess`` directly:

- ffmpeg is a child process attached to Qt's event loop.
- stderr is read via ``readyReadStandardError`` (signal), parsed for the
  output stream dimensions.
- stdout is read via ``readyReadStandardOutput`` (signal); raw BGR bytes
  accumulate in a ``QByteArray`` and we slice off full frames as soon
  as ``W*H*3`` bytes are available.
- Lifecycle is driven by ``finished`` / ``errorOccurred`` signals.
- No QThread, no Python threads, no blocking waits. Anywhere.

The decoding cost itself (H264 → bgr24) happens inside the ffmpeg
process; the GUI thread just memcpy's bytes into ``QImage`` and
triggers a repaint. A scale+fps filter caps the work at 960px / 20 fps
per tile, which is light enough to stay smooth under N cameras.
"""

from __future__ import annotations

import logging
import re
import shutil
from typing import Optional

from PySide6.QtCore import (
    QByteArray,
    QObject,
    QProcess,
    QRect,
    QSize,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QImage, QPainter, QPalette, QPen
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)


_RE_DIMS = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_RE_VIDEO_STREAM = re.compile(r"Stream #\d+:\d+.*?Video:", re.IGNORECASE)


def find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def find_ffprobe() -> Optional[str]:
    return shutil.which("ffprobe")


def find_ffplay() -> Optional[str]:
    return shutil.which("ffplay")


class FFmpegPlayer(QWidget):
    streamUp = Signal()
    streamDown = Signal(str)

    BACKOFF_STEPS_MS = [3000, 5000, 8000, 15000, 30000]
    READY_TIMEOUT_MS = 12000  # max wait for dimensions before giving up

    def __init__(self, url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._url = url

        self._stopped = True
        self._backoff_idx = 0
        self._frame: Optional[QImage] = None
        self._status_msg: str = "Подключение..."

        # Subprocess state
        self._proc: Optional[QProcess] = None
        self._stdout_buf: QByteArray = QByteArray()
        self._stderr_tail: list[str] = []
        self._in_output_section: bool = False
        self._width: int = 0
        self._height: int = 0
        self._frame_size: int = 0
        self._first_frame_seen: bool = False

        # Widget chrome
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor("#0b0d10"))
        self.setPalette(pal)
        self.setMinimumSize(160, 90)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

        # Timers
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._start_process)

        self._ready_timer = QTimer(self)
        self._ready_timer.setSingleShot(True)
        self._ready_timer.timeout.connect(self._on_ready_timeout)

    # Public API ---------------------------------------------------------

    def set_url(self, url: str) -> None:
        self._url = url

    def start(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        self._backoff_idx = 0
        self._first_frame_seen = False
        self._status_msg = "Подключение..."
        self.update()
        self._start_process()

    def stop(self) -> None:
        self._stopped = True
        self._reconnect_timer.stop()
        self._ready_timer.stop()
        self._kill_process(detach=True)
        self._frame = None
        self._status_msg = "Остановлено"
        self.update()

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    # Process management ------------------------------------------------

    def _start_process(self) -> None:
        if self._stopped:
            return
        self._kill_process()  # ensure no previous proc lingering

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            log.error("[FIX] ffmpeg not found in PATH")
            self._on_failure("ffmpeg not installed")
            return

        log.info("[FIX] Spawning ffmpeg for %s", self._safe_url())

        # Reset per-attempt state
        self._stdout_buf = QByteArray()
        self._stderr_tail = []
        self._in_output_section = False
        self._width = 0
        self._height = 0
        self._frame_size = 0
        self._first_frame_seen = False

        args = [
            "-hide_banner",
            "-loglevel", "info",
            "-rtsp_transport", "tcp",
            "-timeout", "5000000",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-an", "-sn",
            "-i", self._url,
            "-an", "-sn",
            "-vf", "scale='min(960,iw)':-2,fps=20",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-",
        ]

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_proc_finished)
        proc.errorOccurred.connect(self._on_proc_error)
        proc.setProgram(ffmpeg)
        proc.setArguments(args)
        self._proc = proc
        proc.start()
        self._ready_timer.start(self.READY_TIMEOUT_MS)

    def _kill_process(self, detach: bool = False) -> None:
        """Non-blocking process kill. NEVER calls waitForFinished on the GUI thread."""
        proc = self._proc
        if proc is None:
            return
        # Detach all signal handlers so a dying proc can't fight a new one.
        for sig, slot in (
            (proc.readyReadStandardOutput, self._on_stdout),
            (proc.readyReadStandardError, self._on_stderr),
            (proc.finished, self._on_proc_finished),
            (proc.errorOccurred, self._on_proc_error),
        ):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._proc = None
        if proc.state() != QProcess.NotRunning:
            proc.terminate()
            # Async escalation: 1 s later, force-kill; 2 s later, delete.
            QTimer.singleShot(1000, lambda p=proc: p.kill() if p.state() != QProcess.NotRunning else None)
        QTimer.singleShot(2000, proc.deleteLater)
        if detach:
            self._ready_timer.stop()

    # Signal handlers ---------------------------------------------------

    def _on_stderr(self) -> None:
        if self._proc is None:
            return
        data = bytes(self._proc.readAllStandardError())
        if not data:
            return
        text = data.decode("utf-8", errors="replace")
        for line in text.splitlines():
            line = line.rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            if len(self._stderr_tail) > 50:
                self._stderr_tail = self._stderr_tail[-50:]
            if line.startswith("Output #"):
                self._in_output_section = True
            if (
                self._frame_size == 0
                and self._in_output_section
                and _RE_VIDEO_STREAM.search(line)
            ):
                m = _RE_DIMS.search(line)
                if m:
                    self._width = int(m.group(1))
                    self._height = int(m.group(2))
                    self._frame_size = self._width * self._height * 3
                    self._ready_timer.stop()
                    log.info(
                        "[FIX] Stream dimensions: %dx%d (frame=%d B)",
                        self._width, self._height, self._frame_size,
                    )
            # Surface real errors at WARN level
            low = line.lower()
            if any(k in low for k in (
                "error", "fail", "could not", "invalid", "401", "403",
                "404", "timed out", "refused", "unauthorized",
            )):
                log.warning("ffmpeg: %s", line)
            else:
                log.debug("ffmpeg: %s", line)

    def _on_stdout(self) -> None:
        if self._proc is None:
            return
        chunk = self._proc.readAllStandardOutput()
        if chunk.isEmpty():
            return
        self._stdout_buf.append(chunk)
        if self._frame_size <= 0:
            return  # not yet known; keep buffering
        # Extract as many whole frames as we have
        while self._stdout_buf.size() >= self._frame_size:
            frame_bytes = bytes(self._stdout_buf.left(self._frame_size))
            self._stdout_buf.remove(0, self._frame_size)
            img = QImage(
                frame_bytes,
                self._width,
                self._height,
                self._width * 3,
                QImage.Format_BGR888,
            ).copy()
            if not self._first_frame_seen:
                self._first_frame_seen = True
                self._backoff_idx = 0
                self.streamUp.emit()
            self._frame = img
            self.update()

    def _on_proc_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        # exit_status: NormalExit / CrashExit
        if self._proc is None:
            return
        tail = " | ".join(self._stderr_tail[-3:]) if self._stderr_tail else "no stderr"
        log.info(
            "[FIX] ffmpeg finished code=%s status=%s tail=%s",
            exit_code, int(exit_status), tail,
        )
        self._kill_process()
        if self._stopped:
            return
        if not self._first_frame_seen:
            reason = self._best_error_reason() or f"exit {exit_code}"
            self._on_failure(reason)
        else:
            self._on_failure("поток оборвался")

    def _on_proc_error(self, err: QProcess.ProcessError) -> None:
        log.warning("[FIX] QProcess error: %s", int(err))
        # finished may still fire; if not, ensure we react
        if self._proc is not None and self._proc.state() == QProcess.NotRunning:
            self._kill_process()
            if not self._stopped:
                self._on_failure(self._best_error_reason() or f"process error {int(err)}")

    def _on_ready_timeout(self) -> None:
        if self._frame_size > 0 or self._stopped:
            return
        log.warning(
            "[FIX] No stream dimensions after %d ms. stderr tail: %s",
            self.READY_TIMEOUT_MS,
            " | ".join(self._stderr_tail[-5:]) or "<empty>",
        )
        self._kill_process()
        self._on_failure(self._best_error_reason() or "не получены размеры потока")

    # Failure / reconnect ----------------------------------------------

    def _on_failure(self, reason: str) -> None:
        if self._stopped:
            return
        self._status_msg = f"Ошибка: {reason} — переподключение..."
        self.update()
        self.streamDown.emit(reason)
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._stopped:
            return
        delay = self.BACKOFF_STEPS_MS[min(self._backoff_idx, len(self.BACKOFF_STEPS_MS) - 1)]
        self._backoff_idx += 1
        log.info("[FIX] Reconnect in %d ms", delay)
        self._reconnect_timer.start(delay)

    def _best_error_reason(self) -> str:
        """Pick a meaningful line from the stderr tail to show the user."""
        keywords = ("401", "403", "404", "unauthorized", "refused", "timed out",
                    "no route", "could not", "failed", "invalid")
        for line in reversed(self._stderr_tail):
            low = line.lower()
            if any(k in low for k in keywords):
                # Trim to something reasonable
                return line[:140]
        return ""

    # Painting ---------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0b0d10"))
        if self._frame is not None and not self._frame.isNull():
            # "cover" — fill the whole tile, crop overflow. No black bars.
            painter.setClipRect(self.rect())
            target = self._cover_rect(self._frame.size(), self.size())
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawImage(target, self._frame)
        else:
            painter.setPen(QPen(QColor("#94a3b8")))
            painter.drawText(self.rect(), Qt.AlignCenter, self._status_msg)

    @staticmethod
    def _cover_rect(src: QSize, dst: QSize) -> QRect:
        """Object-fit: cover — scale so dst is fully covered, center-crop excess."""
        if src.width() <= 0 or src.height() <= 0:
            return QRect(0, 0, dst.width(), dst.height())
        sr = src.width() / src.height()
        dr = dst.width() / dst.height()
        if dr > sr:
            # destination is wider than source aspect → match width, overflow vertically
            w = dst.width()
            h = int(w / sr)
            x = 0
            y = (dst.height() - h) // 2
        else:
            h = dst.height()
            w = int(h * sr)
            x = (dst.width() - w) // 2
            y = 0
        return QRect(x, y, w, h)

    def _safe_url(self) -> str:
        try:
            if "@" in self._url:
                scheme, rest = self._url.split("://", 1)
                _, hostpart = rest.split("@", 1)
                return f"{scheme}://***@{hostpart}"
        except ValueError:
            pass
        return self._url

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop()
        super().closeEvent(event)
