"""Export a Sofia-stripped H.26x stream to mp4 by remuxing (no re-encode).

The exporter wraps an ffmpeg subprocess running ``-c copy`` so CPU stays low
and the output is bit-for-bit the same video. Bytes are pushed in via
:meth:`feed_bytes`; once no new bytes arrive for a few seconds the writer
closes its stdin so ffmpeg finalises the mp4 moov atom.
"""

from __future__ import annotations

import logging
import shutil
from typing import Optional

from PySide6.QtCore import QByteArray, QObject, QProcess, QTimer, Signal

log = logging.getLogger(__name__)

IDLE_FINALIZE_MS = 4000


class MP4Exporter(QObject):
    progress = Signal(str)              # ffmpeg "frame= ... size= ..." line
    finished = Signal(bool, str)        # (ok, message)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc: Optional[QProcess] = None
        self._out_path: str = ""
        self._codec: str = "h264"
        self._stderr_tail: list[str] = []
        self._bytes_written: int = 0
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(IDLE_FINALIZE_MS)
        self._idle_timer.timeout.connect(self._finalize)

    def start(self, out_path: str, codec: str) -> bool:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.finished.emit(False, "ffmpeg не найден в PATH")
            return False
        self._out_path = out_path
        self._codec = codec
        self._stderr_tail.clear()
        self._bytes_written = 0
        args = [
            "-hide_banner", "-loglevel", "info", "-y",
            "-f", codec, "-i", "pipe:0",
            "-c", "copy", "-movflags", "+faststart",
            out_path,
        ]
        log.info("[export] start codec=%s out=%s", codec, out_path)
        proc = QProcess(self)
        proc.setProgram(ffmpeg)
        proc.setArguments(args)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(lambda e: log.warning("[export] QProcess error: %s", int(e)))
        proc.start()
        self._proc = proc
        return True

    def feed_bytes(self, data: bytes) -> None:
        if not data or self._proc is None:
            return
        if self._proc.state() != QProcess.Running:
            return
        try:
            n = self._proc.write(QByteArray(data))
            if n > 0:
                self._bytes_written += n
        except Exception:
            log.exception("[export] write failed")
            return
        self._idle_timer.start()  # restart idle-finalize watchdog

    def cancel(self) -> None:
        log.info("[export] cancel requested")
        self._idle_timer.stop()
        if self._proc is not None and self._proc.state() == QProcess.Running:
            try:
                self._proc.closeWriteChannel()
            except Exception:
                pass
            QTimer.singleShot(2500, lambda p=self._proc:
                              p.kill() if p and p.state() != QProcess.NotRunning else None)

    # ---- internals --------------------------------------------------

    def _finalize(self) -> None:
        if self._proc is None or self._proc.state() != QProcess.Running:
            return
        log.info("[export] idle %dms — closing stdin to finalize mp4", IDLE_FINALIZE_MS)
        try:
            self._proc.closeWriteChannel()
        except Exception:
            log.exception("[export] closeWriteChannel failed")

    def _on_stderr(self) -> None:
        if self._proc is None:
            return
        data = bytes(self._proc.readAllStandardError())
        for raw in data.decode("utf-8", errors="replace").splitlines():
            line = raw.rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            if len(self._stderr_tail) > 40:
                self._stderr_tail = self._stderr_tail[-40:]
            if line.startswith("frame=") or " bitrate=" in line:
                self.progress.emit(line)
            elif any(k in line.lower() for k in ("error", "invalid", "fail")):
                log.warning("ffmpeg-export: %s", line)
            else:
                log.debug("ffmpeg-export: %s", line)

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._idle_timer.stop()
        ok = code == 0 and status == QProcess.NormalExit
        if ok:
            msg = f"Сохранено: {self._out_path} ({self._bytes_written // 1024} KB записано в stdin)"
            log.info("[export] done OK %s", self._out_path)
        else:
            tail = " | ".join(self._stderr_tail[-3:]) if self._stderr_tail else "(no stderr)"
            msg = f"Ошибка ffmpeg (code={code}): {tail}"
            log.warning("[export] failed: %s", msg)
        self.finished.emit(ok, msg)
        if self._proc is not None:
            self._proc.deleteLater()
            self._proc = None
