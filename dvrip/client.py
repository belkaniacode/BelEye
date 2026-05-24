"""Asynchronous DVRIP client built on QTcpSocket.

The client owns one TCP connection to one NVR and multiplexes all
operations (login, keepalive, live monitor for N channels, playback,
file query) over it. Higher layers (UI, grid view) consume Qt
signals and never touch the socket directly.

This module is intentionally protocol-only. Decoding of the H.264
elementary stream wrapped inside MONITOR_DATA / PLAYBACK_DATA frames
happens downstream in ``video.ffmpeg_player``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket

from .auth import sofia_hash
from .codes import MsgId
from .packet import Packet, pack, unpack

log = logging.getLogger(__name__)

KEEPALIVE_INTERVAL_MS = 20_000
DEFAULT_PORT = 34567


@dataclass(slots=True)
class Channel:
    number: int           # 1-based channel index
    name: str             # human-readable label from the NVR


@dataclass(slots=True)
class _PendingMonitor:
    channel: int
    stream_type: str  # "Main" | "Sub"


class DvripClient(QObject):
    """One TCP session to one NVR. Emits Qt signals for every event."""

    # Connection lifecycle
    connected = Signal()
    disconnected = Signal()
    error = Signal(str)

    # Auth
    loginOk = Signal(int)         # session_id
    loginFailed = Signal(str)     # reason

    # Discovery
    channelsDiscovered = Signal(list)  # list[Channel]

    # Live & playback (raw NVR-framed payload; downstream unwraps to H.264)
    videoChunk = Signal(int, bytes)    # (channel, payload)
    playbackChunk = Signal(bytes)

    # File query
    fileList = Signal(list)            # list[FileRecord]

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._sock = QTcpSocket(self)
        self._sock.connected.connect(self._on_connected)
        self._sock.disconnected.connect(self._on_disconnected)
        self._sock.readyRead.connect(self._on_ready_read)
        self._sock.errorOccurred.connect(self._on_socket_error)

        self._rx_buf = bytearray()
        self._session_id: int = 0
        self._sequence: int = 0
        self._user: str = ""
        self._password: str = ""
        self._logged_in: bool = False
        self._pending_monitors: dict[int, _PendingMonitor] = {}

        self._keepalive = QTimer(self)
        self._keepalive.setInterval(KEEPALIVE_INTERVAL_MS)
        self._keepalive.timeout.connect(self._send_keepalive)

    # ----- public API ----------------------------------------------------

    def connect_to(self, host: str, port: int, user: str, password: str) -> None:
        """Open the TCP connection. Emits ``loginOk`` once the NVR accepts us."""
        self._user = user
        self._password = password
        log.info("[DVRIP] connect %s:%d as %s", host, port, user)
        self._sock.connectToHost(host, port)

    def close(self) -> None:
        log.info("[DVRIP] close session=0x%08x", self._session_id)
        if self._logged_in:
            try:
                self._send(MsgId.LOGOUT_REQ, {"Name": self._user, "SessionID": self._sid_str()})
            except Exception:
                log.exception("[DVRIP] logout failed")
        self._keepalive.stop()
        self._sock.disconnectFromHost()

    def start_monitor(self, channel: int, stream_type: str = "Main") -> None:
        """Request live frames for ``channel``. Frames arrive via ``videoChunk``."""
        if not self._logged_in:
            log.warning("[DVRIP] start_monitor before login (ch=%d)", channel)
            return
        self._pending_monitors[channel] = _PendingMonitor(channel, stream_type)
        log.info("[NVR] monitor claim ch=%d stream=%s", channel, stream_type)
        self._send(
            MsgId.MONITOR_CLAIM_REQ,
            {
                "Name": "OPMonitor",
                "SessionID": self._sid_str(),
                "OPMonitor": {
                    "Action": "Claim",
                    "Parameter": {
                        "Channel": channel - 1,   # NVR is 0-based on the wire
                        "CombinMode": "NONE",
                        "StreamType": stream_type,
                        "TransMode": "TCP",
                    },
                },
            },
        )

    def stop_monitor(self, channel: int) -> None:
        if not self._logged_in:
            return
        log.info("[NVR] monitor stop ch=%d", channel)
        self._send(
            MsgId.MONITOR_STOP_REQ,
            {
                "Name": "OPMonitor",
                "SessionID": self._sid_str(),
                "OPMonitor": {
                    "Action": "Stop",
                    "Parameter": {
                        "Channel": channel - 1,
                        "CombinMode": "NONE",
                        "StreamType": "Main",
                        "TransMode": "TCP",
                    },
                },
            },
        )
        self._pending_monitors.pop(channel, None)

    # ----- socket callbacks ---------------------------------------------

    def _on_connected(self) -> None:
        log.info("[DVRIP] tcp connected, sending LOGIN_REQ2")
        self.connected.emit()
        self._send_login()

    def _on_disconnected(self) -> None:
        log.info("[DVRIP] tcp disconnected")
        self._keepalive.stop()
        self._logged_in = False
        self._session_id = 0
        self.disconnected.emit()

    def _on_socket_error(self, _err: QAbstractSocket.SocketError) -> None:
        msg = self._sock.errorString()
        log.warning("[DVRIP] socket error: %s", msg)
        self.error.emit(msg)

    def _on_ready_read(self) -> None:
        chunk = bytes(self._sock.readAll().data())
        if not chunk:
            return
        self._rx_buf.extend(chunk)
        log.debug("[DVRIP] rx +%d bytes (buf=%d)", len(chunk), len(self._rx_buf))
        self._drain_buffer()

    def _drain_buffer(self) -> None:
        offset = 0
        while True:
            pkt, consumed = unpack(self._rx_buf, offset)
            if pkt is None and consumed == 0:
                break
            if pkt is None:
                # Resync skipped junk bytes; continue scanning.
                offset += consumed
                continue
            offset += consumed
            try:
                self._dispatch(pkt)
            except Exception:
                log.exception("[DVRIP] dispatch failed for msg=%d", pkt.msg_id)
        if offset:
            del self._rx_buf[:offset]

    # ----- protocol logic -----------------------------------------------

    def _send_login(self) -> None:
        body = {
            "EncryptType": "MD5",
            "LoginType": "DVRIP-Web",
            "PassWord": sofia_hash(self._password),
            "UserName": self._user,
        }
        self._send(MsgId.LOGIN_REQ2, body)

    def _send_keepalive(self) -> None:
        if not self._logged_in:
            return
        self._send(MsgId.KEEPALIVE_REQ, {"Name": "KeepAlive", "SessionID": self._sid_str()})

    def _send(self, msg_id: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\x00"
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        pkt = Packet(
            msg_id=int(msg_id),
            payload=payload,
            session_id=self._session_id,
            sequence=self._sequence,
        )
        wire = pack(pkt)
        n = self._sock.write(wire)
        log.debug("[DVRIP] send msg=%d bytes=%d written=%d", msg_id, len(wire), n)

    def _dispatch(self, pkt: Packet) -> None:
        log.debug("[DVRIP] recv msg=%d len=%d", pkt.msg_id, len(pkt.payload))

        if pkt.msg_id == MsgId.LOGIN_RSP:
            self._handle_login_rsp(pkt)
        elif pkt.msg_id == MsgId.MONITOR_CLAIM_RSP:
            self._handle_monitor_claim_rsp(pkt)
        elif pkt.msg_id == MsgId.MONITOR_DATA:
            self._handle_monitor_data(pkt)
        elif pkt.msg_id == MsgId.PLAYBACK_DATA:
            self.playbackChunk.emit(pkt.payload)
        elif pkt.msg_id == MsgId.SYSINFO_RSP:
            self._handle_sysinfo_rsp(pkt)
        elif pkt.msg_id == MsgId.KEEPALIVE_RSP:
            log.debug("[DVRIP] keepalive ack")
        elif pkt.msg_id == MsgId.FILE_QUERY_RSP:
            self._handle_file_query_rsp(pkt)
        else:
            log.debug("[DVRIP] unhandled msg=%d", pkt.msg_id)

    # ----- specific response handlers -----------------------------------

    def _handle_login_rsp(self, pkt: Packet) -> None:
        body = _parse_json(pkt.payload)
        ret = int(body.get("Ret", -1))
        sid_str = body.get("SessionID", "0x0")
        try:
            self._session_id = int(sid_str, 16) if isinstance(sid_str, str) else int(sid_str)
        except ValueError:
            self._session_id = 0
        if ret == 100:  # Sofia "OK"
            self._logged_in = True
            self._keepalive.start()
            log.info("[DVRIP] login ok session=0x%08x", self._session_id)
            self.loginOk.emit(self._session_id)
            # kick off channel discovery right away
            self._send(MsgId.SYSINFO_REQ, {"Name": "SystemInfo", "SessionID": self._sid_str()})
        else:
            reason = f"login failed (Ret={ret})"
            log.warning("[DVRIP] %s payload=%s", reason, body)
            self.loginFailed.emit(reason)

    def _handle_sysinfo_rsp(self, pkt: Packet) -> None:
        body = _parse_json(pkt.payload)
        info = body.get("SystemInfo", {})
        n_channels = int(info.get("VideoInChannel", 0) or 0)
        if n_channels <= 0:
            # Some firmwares report ChannelNum at the top level instead.
            n_channels = int(body.get("ChannelNum", 0) or 0)
        if n_channels <= 0:
            log.warning("[NVR] SystemInfo returned 0 channels: %s", body)
            return
        channels = [Channel(number=i + 1, name=f"CH{i + 1:02d}") for i in range(n_channels)]
        log.info("[NVR] discovered %d channels", n_channels)
        self.channelsDiscovered.emit(channels)

    def _handle_monitor_claim_rsp(self, pkt: Packet) -> None:
        body = _parse_json(pkt.payload)
        ret = int(body.get("Ret", -1))
        if ret != 100:
            log.warning("[NVR] monitor claim rejected Ret=%d", ret)
            return
        # Now send the actual Start request to begin the data stream.
        for pending in list(self._pending_monitors.values()):
            self._send(
                MsgId.MONITOR_REQ,
                {
                    "Name": "OPMonitor",
                    "SessionID": self._sid_str(),
                    "OPMonitor": {
                        "Action": "Start",
                        "Parameter": {
                            "Channel": pending.channel - 1,
                            "CombinMode": "NONE",
                            "StreamType": pending.stream_type,
                            "TransMode": "TCP",
                        },
                    },
                },
            )
            log.info("[NVR] monitor start ch=%d", pending.channel)

    def _handle_monitor_data(self, pkt: Packet) -> None:
        # The NVR does not tag MONITOR_DATA packets with a channel number; the
        # session itself is per-claim. With a single active claim we route to
        # that channel; multi-channel multiplexing is added in the OPMonitor task.
        if len(self._pending_monitors) == 1:
            ch = next(iter(self._pending_monitors))
        else:
            ch = 0
        self.videoChunk.emit(ch, pkt.payload)

    def _handle_file_query_rsp(self, pkt: Packet) -> None:
        body = _parse_json(pkt.payload)
        items = body.get("OPFileQuery") or []
        self.fileList.emit(items)

    # ----- helpers ------------------------------------------------------

    def _sid_str(self) -> str:
        return f"0x{self._session_id:08x}"


@dataclass(slots=True)
class FileRecord:
    channel: int
    begin_time: str
    end_time: str
    file_name: str
    size: int = 0
    disk: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _parse_json(payload: bytes) -> dict[str, Any]:
    """Tolerant JSON parser — strips trailing \\0 and ignores parse errors."""
    raw = payload.rstrip(b"\x00").decode("utf-8", errors="replace")
    if not raw.strip():
        return {}
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("[DVRIP] non-JSON payload: %r", raw[:200])
        return {}
    if not isinstance(out, dict):
        return {"_": out}
    return out
