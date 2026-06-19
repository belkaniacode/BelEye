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
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket

from .auth import sofia_hash
from .codes import MsgId
from .packet import Packet, pack, unpack

log = logging.getLogger(__name__)

KEEPALIVE_INTERVAL_MS = 10_000  # [FIX perf] was 20s — consumer NVRs idle-close ~15s
DEFAULT_PORT = 34567

# [FIX arch] Wildcard "*" returns ONLY continuous segments on many Xiongmai
# firmwares; motion/alarm/human events are stored as separate records and
# require a per-event query. We send the full set and merge.
FILE_QUERY_EVENTS: tuple[str, ...] = ("*", "A", "M", "H")


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

    # Recording status: {channel_no(1-based) -> bool recording}
    recordStatus = Signal(dict)

    def __init__(self, parent: QObject | None = None, *, auto_discover: bool = True) -> None:
        super().__init__(parent)
        # auto_discover: probe channel list right after login. The "Add NVR"
        # form wants this; live tiles do NOT (they already know their channel,
        # and running discovery on the same socket competes with OPMonitor and
        # makes the firmware reject the monitor claim with Ret=103).
        self._auto_discover = auto_discover
        self._sock = QTcpSocket(self)
        # [FIX perf] Disable Nagle — DVRIP is request/response with small bursts
        # plus tightly-packed video frames; Nagle adds 40 ms ACK lag per write.
        self._sock.setSocketOption(QAbstractSocket.LowDelayOption, 1)
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
        self._pending_playback: dict | None = None
        # [FIX arch] Multi-event file-query state. We send one request per event
        # filter and aggregate. Native XMEye does the same — wildcard alone
        # misses motion/alarm segments on most Xiongmai firmwares.
        self._file_query_channel: int = 1
        self._file_query_begin: datetime | None = None
        self._file_query_end: datetime | None = None
        # [FIX cap] Queue is now list of (window_begin, window_end, event)
        # so each request stays under the firmware's ~64-record response cap.
        # [FIX archive3] Paginated file-query state — see query_files.
        self._file_query_event_queue: list[str] = []
        self._file_query_cursor: datetime | None = None
        self._file_query_last_key: tuple | None = None
        self._file_query_page_count: int = 0
        self._file_query_page_cap: int = 200
        self._file_query_current_event: str | None = None
        self._file_query_accumulator: dict[tuple[str, str], FileRecord] = {}

        self._keepalive = QTimer(self)
        self._keepalive.setInterval(KEEPALIVE_INTERVAL_MS)
        self._keepalive.timeout.connect(self._send_keepalive)

        # Channel discovery is best-effort: firmwares differ a lot, so we try
        # several requests in sequence and accept whichever one yields a count.
        self._discovery_pending: bool = False
        self._discovery_queue: list[tuple[int, dict[str, Any]]] = []
        self._discovery_timer = QTimer(self)
        self._discovery_timer.setSingleShot(True)
        self._discovery_timer.timeout.connect(self._next_discovery_request)

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

    def start_playback(
        self,
        file_name: str,
        begin: datetime,
        end: datetime,
        channel: int = 1,
    ) -> None:
        """Play back an archived file for ``channel`` (1-based).

        [FIX archive3] CANONICAL DVRIP PLAYBACK PATH — hardware-verified.

        Per the alexshpilkin/dvrip reference implementation, and confirmed by
        ``scripts/probe_playback_1424.py`` against the Xiongmai NBD80S16S-KL:

          1. Send 1424 (PLAYBACK_CLAIM_REQ_NEW) Action="Claim" with the
             minimal Parameter body {FileName, TransMode:"TCP"} plus
             StartTime/EndTime. The firmware identifies the channel from
             the file path itself — no Channel/Value/StreamType field is
             needed, and including them can confuse the parser.
          2. Receive 1425 (PLAYBACK_CLAIM_RSP_NEW) with Ret=100.
          3. Send 1420 (PLAYBACK_REQ_START) Action="Start" with the same
             body. (NB: sending the Start on 1424 again is interpreted as a
             duplicate Claim and rejected with Ret=103 — the action is
             always sent on 1420.)
          4. Archive video flows on 1422 (PLAYBACK_DATA_STREAM). Decoded as
             a Sofia-wrapped H.264/HEVC elementary stream just like live.
          5. To stop, send 1420 with Action="Stop".

        Channel is correct (CAM04 OSD), timestamps match the requested
        file's recording window, and ZERO live MONITOR_DATA (1412) leaks
        — the firmware multiplexes them on different opcodes, so the
        archive stream is naturally isolated from any concurrent live
        traffic. The pre-OPMonitor channel-hint hack that was used in the
        archive/archive2 fixes is no longer needed and has been removed.
        """
        if not self._logged_in:
            log.warning("[PB] start_playback before login")
            return

        # Cancel any prior playback session on this client before starting
        # a new one, so the firmware doesn't hold the previous claim.
        if self._pending_playback is not None:
            self.stop_playback()

        params = {
            "FileName": file_name,
            "TransMode": "TCP",
        }
        body = {
            "Action": "Claim",
            "Parameter": params,
            "StartTime": begin.strftime("%Y-%m-%d %H:%M:%S"),
            "EndTime": end.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._pending_playback = body

        log.info("[FIX archive3] PB 1424 Claim ch=%d file=%s", channel, file_name)
        self._send(MsgId.PLAYBACK_CLAIM_REQ_NEW, {
            "Name": "OPPlayBack",
            "SessionID": self._sid_str(),
            "OPPlayBack": body,
        })

        def _send_start():
            if self._pending_playback is None or not self._logged_in:
                log.info("[FIX archive3] PB delayed Start aborted (no pending)")
                return
            start_body = dict(body)
            start_body["Action"] = "Start"
            log.info("[FIX archive3] PB 1420 Start ch=%d", channel)
            self._send(MsgId.PLAYBACK_REQ_START, {
                "Name": "OPPlayBack",
                "SessionID": self._sid_str(),
                "OPPlayBack": start_body,
            })

        # Give the firmware ~300 ms to process the Claim before issuing
        # Start — probe showed Ret=103 on back-to-back sends.
        QTimer.singleShot(300, _send_start)

    def stop_playback(self) -> None:
        if not self._logged_in or self._pending_playback is None:
            return
        params = self._pending_playback.get("Parameter", {})
        log.info("[FIX archive3] PB 1420 Stop")
        self._send(MsgId.PLAYBACK_REQ_START, {
            "Name": "OPPlayBack",
            "SessionID": self._sid_str(),
            "OPPlayBack": {"Action": "Stop", "Parameter": params},
        })
        self._pending_playback = None

    def query_files(self, channel: int, begin: datetime, end: datetime,
                    chunk_days: int = 2) -> None:
        """Query recorded files for ``channel`` in [begin, end]. Emits ``fileList``
        with a list[FileRecord]. Channel is 1-based here, 0-based on the wire.

        [FIX archive3] Pagination by last-record cursor — matches the
        alexshpilkin/dvrip ``files()`` pattern. The firmware caps any single
        OPFileQuery (opcode 1440) reply at ~64 records, sorted oldest-first;
        the old "chunk_days × event" cartesian schedule missed days when
        a single chunk-window held >64 records (which happens whenever the
        NVR records continuously). The fix: for each Event filter, send
        one request, take the LAST record's begin as the next BeginTime,
        and keep paging until the server returns an empty page or repeats
        the last record. This walks the WHOLE time range no matter how
        dense the recordings are.

        The ``chunk_days`` parameter is ignored — kept for call-site
        compatibility.
        """
        del chunk_days  # unused — pagination replaces chunking
        if not self._logged_in:
            log.warning("[PB] query_files before login (ch=%d)", channel)
            return
        self._file_query_channel = channel
        self._file_query_begin = begin
        self._file_query_end = end
        self._file_query_accumulator = {}
        # One paginated walk per event filter. Cursor is the BeginTime sent in
        # the next 1440 request; advanced to last_record.begin after every
        # non-empty reply. Page-safety cap: 200 pages × 64 records = 12 800
        # records per event — far more than any sane NVR holds for one month.
        self._file_query_event_queue = list(FILE_QUERY_EVENTS)
        self._file_query_current_event = None
        self._file_query_cursor: datetime | None = None
        self._file_query_last_key: tuple | None = None
        self._file_query_page_count: int = 0
        self._file_query_page_cap: int = 200
        log.info(
            "[FIX archive3] query ch=%d %s..%s events=%d (paginated)",
            channel,
            begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
            len(self._file_query_event_queue),
        )
        self._start_next_event_query()

    def _start_next_event_query(self) -> None:
        """[FIX archive3] Begin paginated walk for the next event filter."""
        if not self._file_query_event_queue:
            self._emit_aggregated_file_list()
            return
        self._file_query_current_event = self._file_query_event_queue.pop(0)
        self._file_query_cursor = self._file_query_begin
        self._file_query_last_key = None
        self._file_query_page_count = 0
        self._send_file_query_page()

    def _send_file_query_page(self) -> None:
        """[FIX archive3] Send one OPFileQuery for the current event+cursor."""
        if self._file_query_current_event is None:
            return
        b = self._file_query_cursor.strftime("%Y-%m-%d %H:%M:%S")
        e = self._file_query_end.strftime("%Y-%m-%d %H:%M:%S")
        self._file_query_page_count += 1
        self._send(MsgId.FILE_QUERY_REQ, {
            "Name": "OPFileQuery",
            "SessionID": self._sid_str(),
            "OPFileQuery": {
                "BeginTime": b, "EndTime": e,
                "Channel": self._file_query_channel - 1,
                "DriveTypeMask": 0,
                "Event": self._file_query_current_event,
                "Type": "h264", "StreamType": 0,
            },
        })

    def _emit_aggregated_file_list(self) -> None:
        merged = sorted(
            self._file_query_accumulator.values(), key=lambda r: r.begin
        )
        log.info(
            "[FIX archive3] file-query ch=%d aggregated %d unique records",
            self._file_query_channel, len(merged),
        )
        self._file_query_current_event = None
        self._file_query_accumulator = {}
        self.fileList.emit(merged)

    # Back-compat alias: some older call paths still invoke this name.
    def _send_next_file_query(self) -> None:
        self._send_file_query_page()

    def query_record_status(self) -> None:
        """Request the per-channel record schedule. Emits ``recordStatus``.

        Uses CONFIG_GET (1042) Name="Record". A channel is considered
        recording when the first word of its schedule Mask is non-zero.
        """
        if not self._logged_in:
            log.warning("[REC] query_record_status before login")
            return
        log.info("[REC] query record status")
        self._send(MsgId.CONFIG_GET_REQ, {"Name": "Record", "SessionID": self._sid_str()})

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
        # [FIX archive3] Don't leave a half-finished paginated file query in
        # mid-flight — next session would resume from a stale cursor.
        self._file_query_event_queue = []
        self._file_query_current_event = None
        self._file_query_accumulator = {}
        self._file_query_cursor = None
        self._file_query_last_key = None
        self._file_query_page_count = 0
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
        # Streaming + keepalive are noisy; log them short. Everything else
        # (control responses) is logged in FULL so firmware quirks are
        # diagnosable from the log without re-running.
        if pkt.msg_id in (MsgId.MONITOR_DATA, MsgId.PLAYBACK_DATA,
                           MsgId.PLAYBACK_DATA_STREAM, MsgId.KEEPALIVE_RSP):
            log.debug("[DVRIP] recv msg=%d len=%d", pkt.msg_id, len(pkt.payload))
        else:
            full = pkt.payload.rstrip(b"\x00").decode("utf-8", errors="replace")
            log.info("[DVRIP] recv msg=%d len=%d body=%s", pkt.msg_id, len(pkt.payload), full)

        # Record-status response is a CONFIG_GET_RSP carrying a "Record" key.
        # Route it by content before discovery so the two don't collide.
        if pkt.msg_id == MsgId.CONFIG_GET_RSP:
            body = _parse_json(pkt.payload)
            if "Record" in body:
                self._handle_record_status(body)
                return

        # Generic: any JSON body with a channel-count-ish field counts as discovery.
        if self._discovery_pending and pkt.msg_id != MsgId.LOGIN_RSP:
            if self._try_discover_from_payload(pkt):
                return

        if pkt.msg_id == MsgId.LOGIN_RSP:
            self._handle_login_rsp(pkt)
        elif pkt.msg_id == MsgId.MONITOR_CLAIM_RSP:  # 1414
            self._handle_monitor_claim_rsp(pkt)
        elif pkt.msg_id == MsgId.MONITOR_DATA:       # 1412 — live tiles
            self._handle_monitor_data(pkt)
        elif pkt.msg_id == MsgId.PLAYBACK_DATA_STREAM:  # 1422 — [FIX archive3] archive
            self.playbackChunk.emit(pkt.payload)
        elif pkt.msg_id == MsgId.PLAYBACK_CLAIM_RSP_NEW:  # 1425
            body = _parse_json(pkt.payload)
            ret = int(body.get("Ret", -1))
            if ret != 100:
                log.warning("[FIX archive3] PB 1424 Claim rejected Ret=%d body=%s",
                            ret, body)
            else:
                log.info("[FIX archive3] PB 1424 Claim ACK Ret=100")
        elif pkt.msg_id == MsgId.PLAYBACK_REQ_START_RSP:  # 1421
            body = _parse_json(pkt.payload)
            ret = int(body.get("Ret", -1))
            if ret != 100:
                log.warning("[FIX archive3] PB 1420 reply Ret=%d body=%s", ret, body)
            else:
                log.debug("[FIX archive3] PB 1420 ACK Ret=100")
        elif pkt.msg_id in (
            MsgId.SYSINFO_RSP, MsgId.ABILITY_GET_RSP, MsgId.CONFIG_GET_RSP,
            MsgId.DIGITAL_CHANNEL_STATUS_RSP,
        ):
            self._handle_sysinfo_rsp(pkt)
        elif pkt.msg_id == MsgId.KEEPALIVE_RSP:
            log.debug("[DVRIP] keepalive ack")
        elif pkt.msg_id == MsgId.FILE_QUERY_RSP:
            self._handle_file_query_rsp(pkt)
        else:
            log.info("[DVRIP] unhandled msg=%d (logged above)", pkt.msg_id)

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
            if self._auto_discover:
                self._start_channel_discovery()
        else:
            reason = f"login failed (Ret={ret})"
            log.warning("[DVRIP] %s payload=%s", reason, body)
            self.loginFailed.emit(reason)

    def _handle_sysinfo_rsp(self, pkt: Packet) -> None:
        self._try_discover_from_payload(pkt)

    def _handle_record_status(self, body: dict[str, Any]) -> None:
        """Parse Record config into {channel_no -> recording bool}.

        Each channel entry has a ``Mask`` (list of rows of hex words). The
        channel is recording-enabled when any word in the mask is non-zero.
        """
        records = body.get("Record") or []
        status: dict[int, bool] = {}
        for idx, entry in enumerate(records):
            mask = entry.get("Mask") if isinstance(entry, dict) else None
            recording = False
            if isinstance(mask, list):
                for row in mask:
                    cells = row if isinstance(row, list) else [row]
                    for cell in cells:
                        try:
                            if int(str(cell), 16) != 0:
                                recording = True
                                break
                        except (TypeError, ValueError):
                            pass
                    if recording:
                        break
            status[idx + 1] = recording
        n_rec = sum(1 for v in status.values() if v)
        log.info("[REC] record status: %d/%d channels recording", n_rec, len(status))
        self.recordStatus.emit(status)

    # ----- channel discovery (firmware-tolerant) ------------------------

    _CHANNEL_FIELDS = (
        "VideoInChannel", "DigChannel", "ChannelNum", "ChannelCount",
        "VideoInputChannels", "ChannelTitle",
    )

    def _start_channel_discovery(self) -> None:
        sid = self._sid_str()
        # Ordered list of (msg_id, body) to try until one yields a channel count.
        self._discovery_queue = [
            # Preferred: lists per-channel status; empty digital ports read
            # as the default "D05".."D08" placeholder, so we can show only
            # the channels that actually have a camera.
            (MsgId.DIGITAL_CHANNEL_STATUS_REQ,
                {"Name": "OPMonitor.DigitalChannelStatus", "SessionID": sid}),
            (MsgId.SYSINFO_REQ,    {"Name": "SystemInfo", "SessionID": sid}),
            (MsgId.CONFIG_GET_REQ, {"Name": "SystemInfo", "SessionID": sid}),
            (MsgId.CONFIG_GET_REQ, {"Name": "OPMachine",  "SessionID": sid}),
            (MsgId.ABILITY_GET_REQ,{"Name": "SystemFunction", "SessionID": sid}),
            (MsgId.CONFIG_GET_REQ, {"Name": "ChannelTitle", "SessionID": sid}),
        ]
        self._discovery_pending = True
        self._next_discovery_request()

    def _next_discovery_request(self) -> None:
        if not self._discovery_pending:
            return
        if not self._discovery_queue:
            log.warning("[NVR] discovery exhausted: no firmware-specific request returned channels")
            self._discovery_pending = False
            # As a last resort, ask the user-facing layer to retry by emitting
            # an empty list — the form treats empty as failure.
            self.channelsDiscovered.emit([])
            return
        msg_id, body = self._discovery_queue.pop(0)
        log.info("[NVR] discovery try msg=%d body=%s", msg_id, body)
        self._send(msg_id, body)
        # Give the NVR ~2.5 s, then move on to the next strategy.
        self._discovery_timer.start(2500)

    def _try_discover_from_payload(self, pkt: Packet) -> bool:
        body = _parse_json(pkt.payload)
        if not body:
            return False
        active = _extract_active_channels(body)
        if not active:
            return False
        channels = [Channel(number=num, name=name) for num, name in active]
        log.info(
            "[NVR] discovered %d active channels via msg=%d: %s",
            len(channels), pkt.msg_id,
            ", ".join(f"ch{num}={name!r}" for num, name in active),
        )
        self._discovery_pending = False
        self._discovery_timer.stop()
        self._discovery_queue.clear()
        self.channelsDiscovered.emit(channels)
        return True

    def _handle_monitor_claim_rsp(self, pkt: Packet) -> None:
        body = _parse_json(pkt.payload)
        ret = int(body.get("Ret", -1))
        # [FIX archive3] OPPlayBack no longer uses opcode 1413 — only OPMonitor
        # (live tiles) does. The response Name is always "OPMonitor" here.
        name = str(body.get("Name", ""))
        if ret != 100:
            log.warning("[NVR] claim rejected Name=%s Ret=%d full=%s", name, ret, body)
            return
        # OPMonitor ACK path (live tiles AND the pre-claim from start_playback).
        # Only auto-Start the live-tile claims we tracked in _pending_monitors;
        # the pre-claim OPMonitor inside start_playback already sent its own
        # Start, so there is nothing to do for it here.
        for pending in list(self._pending_monitors.values()):
            self._send(
                MsgId.MONITOR_START_REQ,
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
        # [FIX archive3] LIVE monitor data only. Archive playback flows on a
        # different opcode (1422 / PLAYBACK_DATA_STREAM) so there is no
        # cross-stream mixing to worry about here.
        # The NVR does not tag MONITOR_DATA packets with a channel number;
        # the session itself is per-claim. With a single active claim we
        # route to that channel; multi-channel multiplexing is added in the
        # OPMonitor task.
        if len(self._pending_monitors) == 1:
            ch = next(iter(self._pending_monitors))
        else:
            ch = 0
        self.videoChunk.emit(ch, pkt.payload)

    def _handle_file_query_rsp(self, pkt: Packet) -> None:
        body = _parse_json(pkt.payload)
        ret = body.get("Ret")
        items = body.get("OPFileQuery") or []
        channel = getattr(self, "_file_query_channel", 1)
        event = self._file_query_current_event or "*"
        event_type = _FILE_EVENT_TYPE.get(event, "normal")

        added = 0
        last_record_begin: datetime | None = None
        last_key: tuple | None = None
        for it in items:
            if not isinstance(it, dict):
                continue
            rec = FileRecord.from_dict(channel, it, event_type=event_type)
            if rec is None:
                continue
            key = (rec.file_name, rec.begin.strftime("%Y-%m-%d %H:%M:%S"))
            prev = self._file_query_accumulator.get(key)
            # If the same file came under both "*" and a specific event
            # filter, prefer the more specific event tag so the UI colors it.
            if prev is None or (prev.event_type == "normal" and event_type != "normal"):
                self._file_query_accumulator[key] = rec
                added += 1
            last_record_begin = rec.begin
            last_key = key

        log.debug(
            "[FIX archive3] file-query ch=%d event=%s page=%d items=%d added=%d Ret=%s",
            channel, event, self._file_query_page_count, len(items), added, ret,
        )

        # [FIX archive3] Pagination termination conditions:
        #   1. Empty page (no items at all).
        #   2. Page cap exceeded — safety net for buggy firmwares.
        #   3. Last record key is the same as the previous page's last key
        #      → we are not advancing, server returned the same window twice.
        page_repeats = (
            last_key is not None and last_key == self._file_query_last_key
        )
        if (
            not items
            or self._file_query_page_count >= self._file_query_page_cap
            or page_repeats
            or last_record_begin is None
        ):
            # Done with this event filter — try the next one.
            self._start_next_event_query()
            return

        # Advance cursor past the last record's start, then ask for more.
        # Using `last.begin + 1 second` avoids re-receiving the exact same
        # boundary record on the next page.
        from datetime import timedelta
        self._file_query_last_key = last_key
        self._file_query_cursor = last_record_begin + timedelta(seconds=1)
        if self._file_query_cursor > self._file_query_end:
            self._start_next_event_query()
            return
        self._send_file_query_page()

    # ----- helpers ------------------------------------------------------

    def _sid_str(self) -> str:
        return f"0x{self._session_id:08x}"


# [FIX arch] Map Sofia Event-filter value -> high-level record category used
# by the UI to colour the entry in the archive list.
_FILE_EVENT_TYPE: dict[str, str] = {
    "*": "normal",
    "A": "alarm",
    "M": "motion",
    "H": "human",
}


@dataclass(slots=True)
class FileRecord:
    channel: int
    begin: datetime
    end: datetime
    file_name: str
    size: int = 0
    disk: int = 0
    # [FIX arch] One of: "normal" / "motion" / "alarm" / "human". Set from the
    # Event filter that produced the record so the UI can color-code it.
    event_type: str = "normal"

    @classmethod
    def from_dict(
        cls,
        channel: int,
        d: dict,
        event_type: str = "normal",
    ) -> "FileRecord | None":
        try:
            begin = datetime.strptime(d["BeginTime"], "%Y-%m-%d %H:%M:%S")
            end = datetime.strptime(d["EndTime"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            return None
        size_raw = d.get("FileLength", 0)
        try:
            size = int(str(size_raw), 16) if isinstance(size_raw, str) else int(size_raw)
        except (TypeError, ValueError):
            size = 0
        return cls(
            channel=channel,
            begin=begin,
            end=end,
            file_name=str(d.get("FileName", "")),
            size=size,
            disk=int(d.get("DiskNo", 0) or 0),
            event_type=event_type,
        )


def _extract_channel_count(body: dict[str, Any]) -> int:
    """Walk the response dict looking for the real number of configured channels.

    Order of preference:
      1. ChannelTitle with non-empty names — that's how many channels the
         user actually configured (an NVR that supports 8 but has 4 cameras
         plugged in still lists 8 slots in VideoInChannel, but only 4 in
         ChannelTitle with non-empty names).
      2. ChannelTitle length (any names, including empty) — fallback.
      3. Numeric fields VideoInChannel / DigChannel / ChannelNum / ChannelCount
         / VideoInputChannels — last resort.
    """
    numeric_fields = (
        "VideoInChannel", "DigChannel", "ChannelNum", "ChannelCount",
        "VideoInputChannels",
    )

    # First pass: look for a ChannelTitle array and count non-empty entries.
    titles = _find_channel_titles(body)
    if titles is not None:
        non_empty = sum(1 for t in titles if str(t).strip())
        if non_empty > 0:
            return non_empty
        if titles:  # all empty but the array is there — fall back to length
            return len(titles)

    # Second pass: numeric field hints.
    def walk(obj: Any) -> int:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in numeric_fields:
                    try:
                        n = int(value)
                        if 0 < n <= 64:
                            return n
                    except (TypeError, ValueError):
                        pass
                got = walk(value)
                if got:
                    return got
        elif isinstance(obj, list):
            for item in obj:
                got = walk(item)
                if got:
                    return got
        return 0

    return walk(body)


def _find_channel_titles(body: Any) -> list[str] | None:
    if isinstance(body, dict):
        for key, value in body.items():
            if key == "ChannelTitle" and isinstance(value, list):
                return [str(x) for x in value]
            got = _find_channel_titles(value)
            if got is not None:
                return got
    elif isinstance(body, list):
        for item in body:
            got = _find_channel_titles(item)
            if got is not None:
                return got
    return None


_DEFAULT_DIGITAL_NAME = re.compile(r"^D0*\d+$")  # "D05", "D5", "D08"… = empty port


def _extract_active_channels(body: dict[str, Any]) -> list[tuple[int, str]]:
    """Return [(channel_number, name), ...] for channels that actually have a
    camera.

    Preferred source: ``OPMonitor.DigitalChannelStatus`` — an array of
    per-channel names where an *unconnected* digital port reads as the default
    placeholder ``D05`` / ``D08``. Channels with any other name are live.

    Fallbacks: non-empty ``ChannelTitle[]`` entries, then a numeric channel
    count with synthesized names.
    """
    status = body.get("OPMonitor.DigitalChannelStatus")
    if isinstance(status, list) and status:
        active = [
            (i + 1, str(name))
            for i, name in enumerate(status)
            if str(name).strip() and not _DEFAULT_DIGITAL_NAME.match(str(name).strip())
        ]
        if active:
            return active
        # Everything looked like a placeholder — fall through to other hints.

    titles = _find_channel_titles(body)
    if titles is not None:
        active = [(i + 1, t.strip()) for i, t in enumerate(titles) if str(t).strip()]
        if active:
            return active
        # All empty but the array exists — synthesize for its length
        if titles:
            return [(i + 1, f"CH{i + 1:02d}") for i in range(len(titles))]
    n = _extract_channel_count(body)
    return [(i + 1, f"CH{i + 1:02d}") for i in range(n)]


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
