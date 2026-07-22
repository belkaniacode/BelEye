"""Grid tile for one NVR channel. Owns its own DVRIP socket + ffmpeg pipe."""

from __future__ import annotations

import logging

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFrame, QMenu, QSizePolicy, QVBoxLayout, QWidget

from app.nvr_config import NvrChannel, NvrConfig
from dvrip.client import DvripClient
from dvrip.sofia_frame import SofiaFrameParser, detect_codec
from video.ffmpeg_player import FFmpegPlayer

from .camera_widget import _Overlay, _NameProxy, DraggableTileMixin
from .prefs import prefs

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
    # [hq] Emitted exactly once per set_preferred_stream() call — on success,
    # on abort, and on the no-op paths. GridView serializes switching by
    # waiting for this, so a request that never replied would wedge the queue.
    streamSwitched = Signal(str, str)  # tile_id, resulting stream

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
        # Background: global QSS rule for #CameraTile (video_bg).
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

        # [FIX freeze] Recovery state. A live tile must NEVER stay frozen:
        # any session failure (TCP drop, login refused, rx deadline) schedules
        # an automatic reconnect with backoff; a stall watchdog additionally
        # detects "session alive but no video bytes" and forces the same
        # reconnect path. `_stopped` gates everything so an explicitly
        # stopped tile does not fight its own recovery.
        self._stopped = True
        # [FIX quality] Current live stream type ("Extra1" grid / "Main"
        # focused). Applied at OPMonitor claim time; switchable at runtime.
        # [hq] With "high quality everywhere" on, a tile is BORN on Main, so
        # neither a fresh start nor a reconnect costs an extra switch.
        self._current_stream = (
            "Main" if prefs.hq_all()
            else ("Extra1" if getattr(nvr, "prefer_substream", True) else "Main")
        )
        self._reconnect_idx = 0
        self._reconnect_backoff_ms = [3000, 5000, 10000, 30000]
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._do_reconnect)
        self._last_chunk_ms = 0
        self._stall_timer = QTimer(self)
        self._stall_timer.setInterval(5000)
        self._stall_timer.timeout.connect(self._check_video_stall)

        # [FIX seamless2] Make-before-break stream switch. The current
        # session + decoder keep RUNNING (video never freezes) while a
        # second warm-up pipeline connects with the new stream type. Only
        # when the warm decoder produces its first frame do we swap the
        # widgets and tear the old pipeline down. A spinner on the overlay
        # shows progress; failure/timeout simply keeps the old stream.
        self._switch_client: DvripClient | None = None
        self._switch_parser: SofiaFrameParser | None = None
        self._switch_player: FFmpegPlayer | None = None
        self._switch_codec_detected = False
        self._switch_target: str | None = None
        self._switch_timeout = QTimer(self)
        self._switch_timeout.setSingleShot(True)
        self._switch_timeout.setInterval(15000)
        self._switch_timeout.timeout.connect(self._abort_switch)

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
        self._stopped = False
        self._overlay.set_status("connecting")
        self._chunk_count = 0
        self._first_chunk_logged = False
        self._parser = SofiaFrameParser()
        self._parser._name = self._tile_id  # for log readability
        # [FIX perf] No more 2 MB accumulator. We start ffmpeg on the FIRST
        # non-empty Sofia output (an I-frame payload, which on Xiongmai
        # firmwares already carries SPS/VPS/PPS). If detect_codec can't tell
        # we default to h264 — main path on these devices.
        self._codec_detected = False
        self._spawn_client()

    def stop(self) -> None:
        # [FIX freeze] Explicit stop wins over recovery — cancel any pending
        # reconnect and the stall watchdog before tearing the client down.
        self._stopped = True
        self._reconnect_timer.stop()
        self._stall_timer.stop()
        # [FIX seamless2] Drop any in-flight warm-up pipeline too.
        self._abort_switch()
        self._teardown_client()
        self.player.stop()
        self._overlay.set_status("down")

    def _teardown_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                log.exception("[NVR] tile close failed")
            self._client.deleteLater()
            self._client = None

    def reload_credentials(self) -> None:
        """Restart the DVRIP session — picks up a possibly-changed password."""
        self.stop()
        self.start()

    def set_preferred_stream(self, stream: str) -> None:
        """[FIX quality] Switch this tile between "Extra1" (sub stream, grid)
        and "Main" (full quality, focused view) on the LIVE session.

        Restarts the OPMonitor claim with the new stream type and recycles
        the local decoder so codec detection re-runs against the new
        bitstream. The output scale cap follows the stream: 640 px for the
        sub stream, 1920 px for Main.
        """
        if stream == self._current_stream and self._switch_target is None:
            self.streamSwitched.emit(self._tile_id, self._current_stream)
            return
        if self._stopped or self._client is None:
            # Not streaming: just record the preference, it is applied at the
            # next claim.
            self._current_stream = stream
            self.streamSwitched.emit(self._tile_id, stream)
            return
        if self._switch_target == stream:
            return  # this exact switch already warming up; it will reply
        if self._switch_target is not None:
            self._abort_switch()  # supersede an in-flight switch
        if stream == self._current_stream:
            self.streamSwitched.emit(self._tile_id, self._current_stream)
            return

        # [FIX seamless2] Make-before-break: the CURRENT pipeline keeps
        # playing untouched (the user must never see the video stop). A
        # second DVRIP session + decoder warm up with the new stream type;
        # the swap happens in _complete_switch on the warm decoder's FIRST
        # frame. The firmware can't re-claim a channel inside one TCP
        # session (Ret=103), which is why a second session is required.
        self._switch_target = stream
        self._overlay.set_busy(True)

        wp = FFmpegPlayer(parent=self, input_mode="pipe", input_codec="h264")
        wp.set_output_width(640 if stream != "Main" else 1920)
        wp.hide()
        self.layout().addWidget(wp)
        wp.streamUp.connect(self._complete_switch)
        self._switch_player = wp
        self._switch_parser = SofiaFrameParser()
        self._switch_parser._name = f"{self._tile_id}@{stream}+warm"
        self._switch_codec_detected = False

        wc = DvripClient(self, auto_discover=False)
        wc.loginOk.connect(
            lambda _sid: wc.start_monitor(self.channel.number, stream_type=stream)
        )
        wc.loginFailed.connect(lambda _r: self._abort_switch())
        wc.error.connect(lambda _e: self._abort_switch())
        wc.disconnected.connect(self._on_switch_client_disconnected)
        wc.videoChunk.connect(self._on_switch_chunk)
        self._switch_client = wc
        wc.connect_to(self.nvr.host, self.nvr.port, self.nvr.username, self._password)
        self._switch_timeout.start()

    def _on_switch_client_disconnected(self) -> None:
        # Only abort if the warm client is still in the warm-up role — after
        # promotion its disconnects belong to the normal recovery path.
        if self._switch_client is not None:
            self._abort_switch()

    def _on_switch_chunk(self, _ch: int, data: bytes) -> None:
        if self._switch_parser is None or self._switch_player is None:
            return
        clean = self._switch_parser.feed(data)
        if not clean:
            return
        # [FIX stutter] Same keyframe gate as the primary pipeline.
        if self._switch_parser.iframes_seen == 0:
            return
        if self._switch_codec_detected:
            self._switch_player.feed_bytes(clean)
            return
        codec = detect_codec(clean) or "h264"
        self._switch_player.set_input_codec(codec)
        self._switch_player.start()
        self._switch_player.feed_bytes(clean)
        self._switch_codec_detected = True

    def _complete_switch(self) -> None:
        """Warm decoder produced its first frame — swap pipelines."""
        if self._switch_player is None or self._switch_client is None:
            return
        stream = self._switch_target or self._current_stream
        self._switch_timeout.stop()
        self._overlay.set_busy(False)

        old_player, old_client = self.player, self._client
        new_player, new_client = self._switch_player, self._switch_client

        # Promote warm pipeline to primary.
        new_client.videoChunk.disconnect(self._on_switch_chunk)
        new_client.videoChunk.connect(self._on_video_chunk)
        new_client.disconnected.disconnect(self._on_switch_client_disconnected)
        new_client.disconnected.connect(
            lambda: self._on_session_failed("disconnected")
        )
        new_client.error.disconnect()
        new_client.error.connect(lambda e: self._on_session_failed(f"net: {e}"))
        new_player.streamUp.disconnect(self._complete_switch)
        new_player.streamUp.connect(lambda: self._overlay.set_status("live"))
        new_player.streamDown.connect(lambda _r: self._overlay.set_status("down"))

        self.player = new_player
        self._client = new_client
        self._parser = self._switch_parser
        self._codec_detected = self._switch_codec_detected
        self._current_stream = stream
        self._last_chunk_ms = int(time.monotonic() * 1000)
        self._switch_player = None
        self._switch_client = None
        self._switch_parser = None
        self._switch_target = None

        new_player.show()
        log.info("[hq] tile %s switched to %s", self._tile_id, stream)
        self.streamSwitched.emit(self._tile_id, stream)
        # Tear the OLD pipeline down only after the new one is visible.
        if old_player is not None:
            self.layout().removeWidget(old_player)
            old_player.stop()
            old_player.deleteLater()
        if old_client is not None:
            try:
                old_client.close()
            except Exception:
                pass
            old_client.deleteLater()
        self._overlay.raise_()

    def _abort_switch(self) -> None:
        """Warm-up failed or timed out — keep the old stream, drop the warm
        pipeline, and revert the target so a later attempt can retry."""
        if self._switch_target is None:
            return
        log.warning("[FIX seamless2] tile %s stream switch to %s aborted",
                    self._tile_id, self._switch_target)
        self._switch_timeout.stop()
        self._overlay.set_busy(False)
        self._switch_target = None
        if self._switch_client is not None:
            wc, self._switch_client = self._switch_client, None
            try:
                wc.close()
            except Exception:
                pass
            wc.deleteLater()
        if self._switch_player is not None:
            wp, self._switch_player = self._switch_player, None
            self.layout().removeWidget(wp)
            wp.stop()
            wp.deleteLater()
        self._switch_parser = None
        self.streamSwitched.emit(self._tile_id, self._current_stream)

    # ----- DVRIP wiring ------------------------------------------------

    def _spawn_client(self) -> None:
        c = DvripClient(self, auto_discover=False)
        c.loginOk.connect(self._on_login_ok)
        c.loginFailed.connect(lambda r: self._on_session_failed(f"login: {r}"))
        c.error.connect(lambda e: self._on_session_failed(f"net: {e}"))
        # [FIX freeze] TCP drop / rx-deadline abort both surface here — this
        # was previously left unconnected, which is why a dropped session
        # parked the tile in "down" until an app restart.
        c.disconnected.connect(lambda: self._on_session_failed("disconnected"))
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
        # [FIX quality] Use the tile's current stream preference — Extra1 in
        # the grid, Main when focused (GridView drives this via
        # set_preferred_stream).
        stream = self._current_stream
        log.info(
            "[FIX perf] tile %s login ok; starting OPMonitor ch=%d stream=%s",
            self._tile_id, self.channel.number, stream,
        )
        self.player.set_output_width(640 if stream != "Main" else 1920)
        self._client.start_monitor(self.channel.number, stream_type=stream)
        # [FIX freeze] Arm the stall watchdog with a fresh baseline: if no
        # video chunk arrives within the stall window (even the FIRST one),
        # the session is recycled.
        self._last_chunk_ms = int(time.monotonic() * 1000)
        self._stall_timer.start()

    def _on_session_failed(self, reason: str) -> None:
        if self._stopped:
            return
        log.warning("[NVR] tile %s session failure: %s", self._tile_id, reason)
        self._overlay.set_status("down")
        # [FIX freeze] Auto-reconnect with backoff. Tear the dead client
        # down and schedule a fresh session; backoff index resets when video
        # actually flows again (first chunk of the new session).
        if self._reconnect_timer.isActive():
            return
        self._stall_timer.stop()
        self._teardown_client()
        delay = self._reconnect_backoff_ms[
            min(self._reconnect_idx, len(self._reconnect_backoff_ms) - 1)
        ]
        self._reconnect_idx += 1
        log.warning(
            "[FIX freeze] tile %s reconnect attempt %d in %d ms (%s)",
            self._tile_id, self._reconnect_idx, delay, reason,
        )
        self._overlay.set_status("connecting")
        self._reconnect_timer.start(delay)

    def _do_reconnect(self) -> None:
        if self._stopped:
            return
        self._overlay.set_status("connecting")
        self._chunk_count = 0
        self._first_chunk_logged = False
        self._parser = SofiaFrameParser()
        self._parser._name = self._tile_id
        self._codec_detected = False
        self.player.stop()
        self._spawn_client()

    def _check_video_stall(self) -> None:
        # [FIX freeze] Session up but no MONITOR_DATA for >10 s — the NVR
        # went silent (or the claim died server-side). Recycle the session
        # through the same reconnect path.
        if self._stopped or self._client is None:
            return
        silent_ms = int(time.monotonic() * 1000) - self._last_chunk_ms
        if silent_ms > 10_000:
            log.warning(
                "[FIX freeze] tile %s video stalled (%d ms without chunks)",
                self._tile_id, silent_ms,
            )
            self._on_session_failed("video stall")

    def _on_video_chunk(self, _channel_hint: int, data: bytes) -> None:
        self._chunk_count += 1
        self._last_chunk_ms = int(time.monotonic() * 1000)
        if not self._first_chunk_logged:
            self._first_chunk_logged = True
            # [FIX freeze] Video flows — the session recovered; reset backoff.
            self._reconnect_idx = 0
            log.info(
                "[NVR] tile %s first chunk: %d bytes head=%s",
                self._tile_id, len(data), data[:32].hex(),
            )
        # Strip the Sofia frame wrapper before feeding ffmpeg.
        clean = self._parser.feed(data) if self._parser else data
        if not clean:
            return
        # [FIX stutter] Never feed the decoder before the first keyframe —
        # mid-GOP P-frames only produce reference-error spam and garbage
        # frames. The firmware sends an I-frame within one GOP (~2 s).
        if self._parser is not None and self._parser.iframes_seen == 0:
            return

        if self._codec_detected:
            self.player.feed_bytes(clean)
            return

        # [FIX perf] Start ffmpeg on the FIRST non-empty Sofia output. The
        # Sofia parser only yields I-frame and P-frame payloads, and an
        # I-frame on Xiongmai already carries SPS/VPS/PPS — so detect_codec
        # almost always succeeds on the very first chunk. If not, default to
        # h264 instead of buffering megabytes.
        codec = detect_codec(clean) or "h264"
        log.info(
            "[FIX perf] tile %s codec=%s, starting decoder (chunk=%d B)",
            self._tile_id, codec, len(clean),
        )
        self.player.set_input_codec(codec)
        self.player.start()
        self.player.feed_bytes(clean)
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
