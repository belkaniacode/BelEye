"""Grid tile for one NVR channel. Owns its own DVRIP socket + ffmpeg pipe."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFrame, QMenu, QSizePolicy, QVBoxLayout, QWidget

from app.nvr_config import NvrChannel, NvrConfig
from dvrip.client import DvripClient
from dvrip.sofia_frame import SofiaFrameParser, detect_codec
from video.ffmpeg_player import FFmpegPlayer

from .camera_widget import _Overlay, _NameProxy, DraggableTileMixin

log = logging.getLogger(__name__)


def nvr_tile_id(nvr_id: str, channel: int) -> str:
    """Stable id used by GridView as the dict key for an NVR-channel tile."""
    return f"nvr:{nvr_id}:ch{channel}"


class NvrChannelTile(DraggableTileMixin, QFrame):
    """Visually identical to CameraTile, but fed via DVRIP instead of RTSP.

    Exposes the same signals as ``CameraTile`` so ``GridView`` can treat them
    uniformly (expand on double-click, context menu, drag-and-drop reorder).
    """

    expandRequested = Signal(str)
    editRequested = Signal(str)        # emitted with nvr_id (opens NVR settings)
    removeRequested = Signal(str)      # emitted with tile_id (disables this channel)
    reconnectRequested = Signal(str)   # emitted with tile_id
    swapRequested = Signal(str, str)   # source tile_id, target tile_id
    archiveRequested = Signal(str, int)  # nvr_id, channel_number

    @property
    def drag_id(self) -> str:
        return self._tile_id

    def __init__(
        self,
        nvr: NvrConfig,
        channel: NvrChannel,
        password: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.nvr = nvr
        self.channel = channel
        self._password = password
        self._tile_id = nvr_tile_id(nvr.id, channel.number)

        self.setObjectName("CameraTile")
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("#CameraTile { background: #0b0d10; }")
        self.setMouseTracking(True)

        # Pipe-mode player: ffmpeg reads H.264 elementary stream from stdin.
        self.player = FFmpegPlayer(parent=self, input_mode="pipe", input_codec="h264")
        self.player.streamUp.connect(lambda: self._overlay.set_status("live"))
        self.player.streamDown.connect(lambda _r: self._overlay.set_status("down"))

        self._overlay = _Overlay(self)
        self._overlay.set_name(f"{nvr.name} · {channel.name}")
        self._overlay.set_status("connecting")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.player)

        # Compatibility shim so any future ``_name_label.setText`` calls work.
        self._name_label = _NameProxy(self._overlay)

        # Dedicated DVRIP socket per channel — simplest multiplex.
        self._client: DvripClient | None = None
        self._chunk_count = 0
        self._first_chunk_logged = False
        self._parser: SofiaFrameParser | None = None
        self._init_drag()

    # ----- compatibility surface (so GridView can treat us like CameraTile) -----

    @property
    def camera(self) -> NvrConfig:
        """Some grid code reads `.camera`; expose the NVR object so .id works."""
        # Note: we return the NVR config, not a CameraConfig. This is only used
        # in places that read `.id` — the tile's logical id is the tile_id below.
        return self.nvr

    def set_recording(self, on: bool) -> None:
        self._overlay.set_recording(on)

    # ----- lifecycle ----------------------------------------------------

    def start(self) -> None:
        self._overlay.set_status("connecting")
        self._chunk_count = 0
        self._first_chunk_logged = False
        self._parser = SofiaFrameParser()
        self._parser._name = self._tile_id  # for log readability
        # Player start is deferred until we detect the codec (h264 vs hevc)
        # from the first parameter-set NAL in the elementary stream.
        self._codec_detected = False
        self._pending_es = bytearray()
        self._spawn_client()

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                log.exception("[NVR] tile close failed")
            self._client.deleteLater()
            self._client = None
        self.player.stop()
        self._overlay.set_status("down")

    def reload_credentials(self) -> None:
        """Restart the DVRIP session — picks up a possibly-changed password."""
        self.stop()
        self.start()

    # ----- DVRIP wiring ------------------------------------------------

    def _spawn_client(self) -> None:
        c = DvripClient(self, auto_discover=False)
        c.loginOk.connect(self._on_login_ok)
        c.loginFailed.connect(lambda r: self._on_session_failed(f"login: {r}"))
        c.error.connect(lambda e: self._on_session_failed(f"net: {e}"))
        c.videoChunk.connect(self._on_video_chunk)
        self._client = c
        log.info(
            "[NVR] tile %s connecting %s:%d ch=%d",
            self._tile_id, self.nvr.host, self.nvr.port, self.channel.number,
        )
        c.connect_to(self.nvr.host, self.nvr.port, self.nvr.username, self._password)

    def _on_login_ok(self, _sid: int) -> None:
        if self._client is None:
            return
        # Start live monitor for our specific channel.
        log.info("[NVR] tile %s login ok; starting OPMonitor ch=%d",
                 self._tile_id, self.channel.number)
        self._client.start_monitor(self.channel.number, stream_type="Main")

    def _on_session_failed(self, reason: str) -> None:
        log.warning("[NVR] tile %s session failure: %s", self._tile_id, reason)
        self._overlay.set_status("down")
        # The DvripClient already emits disconnected; we don't auto-reconnect
        # here for now — user can right-click → Переподключить.

    def _on_video_chunk(self, _channel_hint: int, data: bytes) -> None:
        self._chunk_count += 1
        if not self._first_chunk_logged:
            self._first_chunk_logged = True
            log.info(
                "[NVR] tile %s first chunk: %d bytes head=%s",
                self._tile_id, len(data), data[:32].hex(),
            )
        # Strip the Sofia frame wrapper before feeding ffmpeg.
        clean = self._parser.feed(data) if self._parser else data
        if not clean:
            return

        if self._codec_detected:
            self.player.feed_bytes(clean)
            return

        # Still detecting: accumulate until we see a parameter-set NAL.
        self._pending_es.extend(clean)
        codec = detect_codec(bytes(self._pending_es))
        if codec is None:
            if len(self._pending_es) > 2_000_000:
                # Give up detecting; assume h264 to avoid unbounded buffering.
                codec = "h264"
                log.warning("[NVR] tile %s codec undetected, assuming h264", self._tile_id)
            else:
                return
        log.info("[NVR] tile %s codec=%s, starting decoder", self._tile_id, codec)
        self.player.set_input_codec(codec)
        self.player.start()
        self.player.feed_bytes(bytes(self._pending_es))
        self._pending_es.clear()
        self._codec_detected = True

    # ----- events ------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._overlay.setGeometry(0, 0, self.width(), self.height())

    def enterEvent(self, event) -> None:  # noqa: N802
        self._overlay.set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._overlay.set_hovered(False)
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.expandRequested.emit(self._tile_id)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        act_archive = QAction("Архив…", menu)
        act_reconnect = QAction("Переподключить", menu)
        act_remove = QAction("Скрыть этот канал", menu)
        act_edit = QAction("Настройки регистратора...", menu)
        menu.addAction(act_archive)
        menu.addSeparator()
        menu.addAction(act_reconnect)
        menu.addAction(act_remove)
        menu.addAction(act_edit)
        act_archive.triggered.connect(
            lambda: self.archiveRequested.emit(self.nvr.id, self.channel.number))
        act_reconnect.triggered.connect(lambda: self.reconnectRequested.emit(self._tile_id))
        act_remove.triggered.connect(lambda: self.removeRequested.emit(self._tile_id))
        act_edit.triggered.connect(lambda: self.editRequested.emit(self.nvr.id))
        menu.exec(event.globalPos())
