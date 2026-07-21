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
import time
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


def _shiboken_valid(obj) -> bool:
    """[FIX shiboken] True if the underlying C++ object is still alive."""
    try:
        import shiboken6
        return shiboken6.isValid(obj)
    except ImportError:
        return True


def _safe_proc_kill(p: QProcess) -> None:
    """[FIX shiboken] Deferred force-kill that tolerates the process object
    having been destroyed in the meantime."""
    if not _shiboken_valid(p):
        return
    try:
        if p.state() != QProcess.NotRunning:
            p.kill()
    except RuntimeError:
        pass


def _safe_proc_delete(p: QProcess) -> None:
    if not _shiboken_valid(p):
        return
    try:
        p.deleteLater()
    except RuntimeError:
        pass


def find_ffprobe() -> Optional[str]:
    return shutil.which("ffprobe")


def find_ffplay() -> Optional[str]:
    return shutil.which("ffplay")


class FFmpegPlayer(QWidget):
    streamUp = Signal()
    streamDown = Signal(str)

    BACKOFF_STEPS_MS = [3000, 5000, 8000, 15000, 30000]
    READY_TIMEOUT_MS = 12000  # max wait for dimensions before giving up

    def __init__(
        self,
        url: str = "",
        parent: QWidget | None = None,
        *,
        input_mode: str = "rtsp",
        input_codec: str = "h264",
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._input_mode = input_mode  # "rtsp" or "pipe"
        self._input_codec = input_codec  # for pipe mode: "h264" | "hevc"

        self._stopped = True
        self._backoff_idx = 0
        self._frame: Optional[QImage] = None
        self._status_msg: str = "Подключение..."

        # Subprocess state
        self._proc: Optional[QProcess] = None
        # [FIX live-perf] bytearray + offset instead of QByteArray.remove() to
        # avoid O(N) memmove per frame extraction. Under load 4 tiles each
        # emit ~20fps × ~700 KB raw frames; the previous remove(0, frame_size)
        # shifted the rest of the buffer per frame and caused visible UI lag
        # and freezes when the OS pipe delivered multiple frames per stdout
        # event. Compact only when the offset exceeds a threshold.
        self._stdout_buf: bytearray = bytearray()
        self._stdout_off: int = 0
        self._stderr_tail: list[str] = []
        self._in_output_section: bool = False
        self._width: int = 0
        self._height: int = 0
        self._frame_size: int = 0
        self._first_frame_seen: bool = False
        self._fed_dropped_warned: bool = False
        # [FIX quality] Output width cap for the scale filter. 640 suits the
        # multi-tile grid; a focused/maximized tile raises it (with the Main
        # stream) for full quality. Changing it at runtime restarts ffmpeg.
        self._output_width: int = 640

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

        # [FIX freeze] Post-startup watchdog. The exit-only failure detection
        # missed two real-world freeze modes: (a) ffmpeg alive but no frames
        # decoded (wedged decoder), (b) ffmpeg alive but not reading stdin —
        # the backpressure guard then drops every chunk forever. Both are
        # detected here and recycled through the existing _on_failure →
        # backoff-reconnect path.
        self._last_frame_ms: int = 0
        self._backpressure_since_ms: int = 0
        self._stall_suspended: bool = False
        self._stall_watchdog = QTimer(self)
        self._stall_watchdog.setInterval(4000)
        self._stall_watchdog.timeout.connect(self._check_decoder_stall)

    # Public API ---------------------------------------------------------

    def set_url(self, url: str) -> None:
        self._url = url

    def set_input_codec(self, codec: str) -> None:
        """Set 'h264' or 'hevc' for pipe mode. Must be called before start()."""
        self._input_codec = codec

    def set_output_width(self, width: int) -> None:
        """[FIX quality] Change the scale-filter width cap. If the decoder is
        already running with a different cap, restart it so the new value
        takes effect immediately."""
        width = max(160, int(width))
        if width == self._output_width:
            return
        self._output_width = width
        if self.is_running():
            self._kill_process()
            self._start_process()

    def start(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        self._backoff_idx = 0
        self._first_frame_seen = False
        self._status_msg = "Подключение..."
        self.update()
        self._start_process()

    def stop(self, preserve_frame: bool = False) -> None:
        """Stop the decoder. With ``preserve_frame=True`` the last decoded
        image stays on screen — used for seamless stream switches where a
        black 'stopped' flash would look like a reconnect to the user."""
        self._stopped = True
        self._reconnect_timer.stop()
        self._ready_timer.stop()
        self._stall_watchdog.stop()
        self._last_frame_ms = 0
        self._backpressure_since_ms = 0
        self._kill_process(detach=True)
        if not preserve_frame:
            self._frame = None
            self._status_msg = "Остановлено"
            self.update()

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    def feed_bytes(self, data: bytes) -> None:
        """Feed elementary-stream bytes to ffmpeg's stdin. Pipe mode only."""
        if self._input_mode != "pipe":
            return
        if not data:
            return
        if self._proc is None or self._proc.state() != QProcess.Running:
            return
        # [FIX live-perf] Backpressure: if ffmpeg is decode-bound, QProcess
        # keeps queuing writes in an unbounded internal buffer and the
        # process backs up arbitrarily — eventually starving the GUI thread
        # via memory pressure. When the pending stdin queue exceeds 2 MB
        # (≈1 s of NVR sub-stream), drop the incoming chunk and warn. The
        # NVR will resend a keyframe soon enough to recover; better to skip
        # frames than freeze the UI.
        try:
            pending = self._proc.bytesToWrite()
            if pending > 2 * 1024 * 1024:
                if not self._fed_dropped_warned:
                    log.warning(
                        "[FIX live-perf] %s stdin backpressure %d B — "
                        "dropping chunks until decoder catches up",
                        self._safe_url(), pending,
                    )
                    self._fed_dropped_warned = True
                return
            if self._fed_dropped_warned and pending < 512 * 1024:
                self._fed_dropped_warned = False
                log.info("[FIX live-perf] %s stdin backpressure cleared",
                         self._safe_url())
            self._proc.write(QByteArray(data))
        except Exception:
            log.exception("[player] feed_bytes failed")

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
        self._stdout_buf = bytearray()
        self._stdout_off = 0
        self._stderr_tail = []
        self._in_output_section = False
        self._width = 0
        self._height = 0
        self._frame_size = 0
        self._first_frame_seen = False
        self._fed_dropped_warned = False
        self._last_frame_ms = 0
        self._backpressure_since_ms = 0

        if self._input_mode == "pipe":
            # [FIX live-perf] Tile-sized output (640 wide) and a hard
            # 2-thread cap. With 4 live tiles the previous default (full
            # ffmpeg auto-threads × 960-wide scale) thrashed the CPU and
            # caused visible freezes. The Extra1 sub-stream the tiles use
            # is already ≤640 wide on this NVR, so 'min(640,iw)' is a no-op
            # on the common path but caps oversize main-stream tiles.
            # Drop fps=20: the fps filter buffers frames to a fixed cadence
            # which on a constrained pipe ADDS latency; let ffmpeg emit at
            # the source rate and rely on `_on_stdout` collapsing bursts to
            # the latest frame for paint.
            args = [
                "-hide_banner",
                "-loglevel", "info",
                "-threads", "2",
                # [FIX perf] Cut ffmpeg's default 5 s / 5 MB probe to a
                # constant — pipe input gives us the codec up-front, so
                # extra probing only adds startup latency. The combined
                # "nobuffer+discardcorrupt" flag drops partial frames on
                # the rare resync glitch instead of stalling the decoder.
                "-probesize", "32",
                "-analyzeduration", "0",
                # [FIX trick-play] Tolerate the I-frame-only stream the NVR
                # emits while a Fast/Slow playback is active: regenerate
                # PTS from input rate, ignore stale DTS, and never bail out
                # on a missing reference frame. These flags are safe at 1×
                # too — a healthy continuous GOP still decodes cleanly
                # under them.
                "-fflags", "nobuffer+discardcorrupt+genpts+igndts",
                "-flags", "low_delay",
                "-err_detect", "ignore_err",
                "-f", self._input_codec,
                "-i", "pipe:0",
                "-an", "-sn",
                "-vf", f"scale='min({self._output_width},iw)':-2",
                "-f", "rawvideo",
                "-pix_fmt", "bgr24",
                "pipe:1",
            ]
        else:
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
                "-vf", f"scale='min({max(self._output_width, 960)},iw)':-2,fps=20",
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
        # [FIX shiboken] Detach the QProcess from this widget BEFORE the
        # deferred kill/delete timers: when the widget itself is deleted
        # (make-before-break player swap), Qt's parent-child teardown would
        # destroy the C++ QProcess early and the 1s/2s lambdas would raise
        # "libshiboken: Internal C++ object already deleted". Unparented,
        # the process outlives the widget and the deferred cleanup owns it.
        proc.setParent(None)
        if proc.state() != QProcess.NotRunning:
            proc.terminate()
            # Async escalation: 1 s later, force-kill; 2 s later, delete.
            QTimer.singleShot(1000, lambda p=proc: _safe_proc_kill(p))
        QTimer.singleShot(2000, lambda p=proc: _safe_proc_delete(p))
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
        # [FIX live-perf] Append raw bytes to bytearray (amortised O(1));
        # use an offset cursor for frame extraction so we never memmove the
        # remaining buffer per frame. Compact only when the offset crosses
        # a generous threshold (16 MB) — under steady-state the buffer
        # holds at most ~1 frame so compaction is rare.
        self._stdout_buf.extend(bytes(chunk))
        if self._frame_size <= 0:
            return  # not yet known; keep buffering
        buf = self._stdout_buf
        fs = self._frame_size
        off = self._stdout_off
        latest_frame: Optional[QImage] = None
        while len(buf) - off >= fs:
            # Single copy: memoryview slice → bytes for QImage backing storage.
            frame_bytes = bytes(memoryview(buf)[off:off + fs])
            off += fs
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
                # [FIX freeze] Frames flow — arm the post-startup watchdog.
                self._stall_watchdog.start()
                self.streamUp.emit()
            self._last_frame_ms = int(time.monotonic() * 1000)
            # Only keep the latest frame from this batch — repainting every
            # frame in a multi-frame stdout burst is wasted work; only the
            # most recent one matters for live display.
            latest_frame = img
        self._stdout_off = off
        # Compact when the head waste grows large to bound memory.
        if off > 16 * 1024 * 1024:
            del buf[:off]
            self._stdout_off = 0
        if latest_frame is not None:
            self._frame = latest_frame
            self.update()

    def _check_decoder_stall(self) -> None:
        """[FIX freeze] Detect a wedged-but-alive decoder and recycle it."""
        if self._stopped or self._proc is None or not self._first_frame_seen:
            return
        if self._stall_suspended:
            return
        now = int(time.monotonic() * 1000)
        # (a) No decoded frames for >8 s while the process claims to run.
        if self._last_frame_ms and now - self._last_frame_ms > 8000:
            log.warning(
                "[FIX freeze] decoder stalled (%d ms without frames) — restarting",
                now - self._last_frame_ms,
            )
            self._stall_watchdog.stop()
            self._kill_process()
            self._on_failure("decoder stalled")
            return
        # (b) stdin backpressure that never drains — ffmpeg stopped reading.
        try:
            pending = self._proc.bytesToWrite()
        except Exception:
            return
        if pending > 2 * 1024 * 1024:
            if self._backpressure_since_ms == 0:
                self._backpressure_since_ms = now
            elif now - self._backpressure_since_ms > 6000:
                log.warning(
                    "[FIX freeze] stdin backpressure stuck %d ms (%d B) — restarting",
                    now - self._backpressure_since_ms, pending,
                )
                self._backpressure_since_ms = 0
                self._stall_watchdog.stop()
                self._kill_process()
                self._on_failure("stdin backpressure stuck")
        else:
            self._backpressure_since_ms = 0

    def suspend_stall_watchdog(self, suspended: bool) -> None:
        """[FIX freeze] Playback pause legitimately stops the frame flow —
        the owner suspends the watchdog for its duration."""
        self._stall_suspended = suspended
        if not suspended:
            self._last_frame_ms = int(time.monotonic() * 1000)

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
