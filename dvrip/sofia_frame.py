"""Strip Sofia/Xiongmai frame wrappers from MONITOR_DATA payloads.

Xiongmai NVRs do not emit raw H.264 over OPMonitor. They send a stream of
sub-frames, each prefixed with a small header:

  0x00 0x00 0x01 <marker> <header bytes...> <H.264 payload>

Common markers seen in the wild (python-dvr, sofiactl, dvrip-py):

  0xFC  I-frame (key)         16-byte header, length at bytes 12..15 (u32 LE)
  0xFD  P-frame               8-byte header,  length at bytes 4..7   (u32 LE)
  0xFA  audio                 8-byte header,  length at bytes 6..7   (u16 LE)
  0xFB  info / sub stream     16-byte header, length at bytes 12..15

[FIX stutter] The audio layout was hardware-verified on the Xiongmai
NBD80S16S-KL: header is ``00 00 01 FA <media u8> <rate u8> <len u16 LE>``
followed by ``len`` bytes of G.711 (738/738 frames in a 30 s capture
match; the previously assumed 16-byte header read PCM samples as a
u32 length, forcing a parser resync on EVERY audio frame — and when
the resync scan hit a byte pattern inside the audio that looked like a
frame marker, it swallowed part of the following VIDEO frame, which is
what produced the constant "undecodable NALU" decoder spam and the
random multi-second tile freezes until the next IDR).

Some firmwares interleave audio + video on the same monitor session; we drop
audio + info and concatenate I- and P-frame payloads into a clean H.264
elementary stream that ffmpeg can decode with ``-f h264 -i pipe:0``.

The parser is intentionally tolerant: if it can't find a known marker it
buffers up to a reasonable window and resynchronises forward.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Maximum bytes we buffer waiting for a complete sub-frame. If the stream
# ever exceeds this without yielding a recognisable frame we drop the buffer
# rather than grow it unbounded.
MAX_BUFFER = 4 * 1024 * 1024  # 4 MiB

MARKER_IFRAME = 0xFC
MARKER_PFRAME = 0xFD
MARKER_AUDIO = 0xFA
MARKER_INFO = 0xFB
VIDEO_MARKERS = (MARKER_IFRAME, MARKER_PFRAME)
_ALL_MARKERS = frozenset((MARKER_IFRAME, MARKER_PFRAME, MARKER_AUDIO, MARKER_INFO))


@dataclass
class SofiaFrameParser:
    """Stateful parser. Feed bytes, get back clean H.264 chunks."""

    _buf: bytearray = field(default_factory=bytearray)
    _passthrough: bool | None = None
    _frames_seen: int = 0
    # [FIX stutter] Count of I-frames extracted so far. Consumers use this
    # to hold off feeding a decoder until the stream has a keyframe —
    # decoding P-frames from mid-GOP only yields reference-error spam.
    iframes_seen: int = 0
    _bytes_yielded: int = 0
    _resync_count: int = 0
    _name: str = "?"

    def feed(self, chunk: bytes) -> bytes:
        """Append ``chunk``, parse what we can, return concatenated H.264 bytes."""
        if not chunk:
            return b""

        # Decide passthrough mode on the very first chunk: if it looks like a
        # raw H.264 elementary stream (starts with an Annex-B NAL start code
        # 0x00 0x00 0x00 0x01 or 0x00 0x00 0x01 + 0x67/0x68/0x65/0x61), we
        # bypass parsing entirely. Saves CPU on firmwares that already send
        # raw H.264.
        if self._passthrough is None:
            self._passthrough = _looks_like_raw_h264(chunk)
            log.info(
                "[sofia] parser[%s] passthrough=%s first16=%s",
                self._name, self._passthrough, chunk[:16].hex(),
            )

        if self._passthrough:
            self._bytes_yielded += len(chunk)
            return chunk

        self._buf.extend(chunk)
        out = bytearray()
        while True:
            consumed, payload = self._try_parse_one()
            if consumed == 0:
                break
            del self._buf[:consumed]
            if payload:
                out.extend(payload)
                self._frames_seen += 1
                self._bytes_yielded += len(payload)

        if len(self._buf) > MAX_BUFFER:
            log.warning(
                "[sofia] parser[%s] buffer overflow (%d bytes), dropping",
                self._name, len(self._buf),
            )
            self._buf.clear()
        return bytes(out)

    def stats(self) -> dict:
        return {
            "passthrough": self._passthrough,
            "frames": self._frames_seen,
            "bytes": self._bytes_yielded,
            "resyncs": self._resync_count,
            "buffered": len(self._buf),
        }

    # -------- internals --------

    def _try_parse_one(self) -> tuple[int, bytes]:
        """Try to parse one sub-frame from the head of ``_buf``.

        Returns (consumed_bytes, payload_bytes). consumed=0 means "need more
        data". payload may be empty (frame was audio/info and skipped).

        Only the 4 known Sofia frame markers (FC/FD/FA/FB) are treated as
        frame boundaries. A bare ``00 00 01`` followed by any other byte is an
        *internal* H.264/HEVC NAL start code that lives inside a frame payload
        — we must NOT treat it as a frame boundary. Resync therefore scans for
        ``00 00 01 {FC|FD|FA|FB}`` specifically.
        """
        buf = self._buf
        if len(buf) < 8:
            return 0, b""

        if not (buf[0:3] == b"\x00\x00\x01" and buf[3] in _ALL_MARKERS):
            consumed = self._resync_to_marker()
            return consumed, b""

        marker = buf[3]

        if marker == MARKER_PFRAME:
            # 8-byte header, length at bytes 4..7 (u32 LE)
            length = struct.unpack_from("<I", buf, 4)[0]
            total = 8 + length
            if length > MAX_BUFFER:
                return self._resync_to_marker(), b""
            if len(buf) < total:
                return 0, b""
            return total, bytes(buf[8:total])

        if marker == MARKER_AUDIO:
            # [FIX stutter] 8-byte header, length at bytes 6..7 (u16 LE).
            # Payload is audio — dropped.
            length = struct.unpack_from("<H", buf, 6)[0]
            total = 8 + length
            if len(buf) < total:
                return 0, b""
            return total, b""

        # I-frame / info: 16-byte header, length at bytes 12..15
        if len(buf) < 16:
            return 0, b""
        length = struct.unpack_from("<I", buf, 12)[0]
        total = 16 + length
        if length > MAX_BUFFER:
            return self._resync_to_marker(), b""
        if len(buf) < total:
            return 0, b""
        if marker == MARKER_IFRAME:
            self.iframes_seen += 1
            return total, bytes(buf[16:total])
        return total, b""  # info — drop

    def _resync_to_marker(self) -> int:
        """Return how many bytes to drop to reach the next valid frame marker."""
        buf = self._buf
        self._resync_count += 1
        search_from = 1
        while True:
            idx = buf.find(b"\x00\x00\x01", search_from)
            if idx < 0:
                return max(0, len(buf) - 3)  # keep tail in case marker straddles
            if idx + 3 < len(buf) and buf[idx + 3] in _ALL_MARKERS:
                return idx
            search_from = idx + 1


def detect_codec(data: bytes) -> str | None:
    """Inspect an Annex-B elementary stream and return 'h264', 'hevc', or None.

    [FIX codec] Recognise BOTH parameter sets AND IDR slices — Xiongmai
    OPPlayBack often emits an I-frame whose first NAL is an IDR slice
    rather than a VPS/SPS/PPS, so a parameter-set-only check returns None
    and the caller falls back to the wrong codec.

      H.264 (nal_type = byte & 0x1F):
        5  = IDR slice
        7  = SPS
        8  = PPS
      HEVC  (nal_type = (byte >> 1) & 0x3F, forbidden_zero bit must be 0):
        19, 20 = IDR_W_RADL / IDR_N_LP
        32, 33, 34 = VPS / SPS / PPS

    HEVC NAL header has the forbidden_zero bit in bit 7 (must be 0); a
    well-formed HEVC byte therefore has high bit 0. We use that to break
    ties when both classifications would otherwise match (e.g. some byte
    values give a legal H.264 type AND a legal HEVC type — the HEVC
    forbidden-zero check resolves the ambiguity).
    """
    HEVC_IRAP = (19, 20)
    HEVC_PARAM = (32, 33, 34)
    H264_IDR_OR_PARAM = (5, 7, 8)

    # NOTE: order matters. Bytes like 0x28 mean "HEVC IDR_N_LP" but also
    # parse as "H.264 PPS with ref_idc=1". Real-world H.264 SPS/PPS use
    # ref_idc=3 (bytes 0x67/0x68 for SPS/PPS), which are NEVER valid HEVC
    # IRAP/param types — so we check HEVC ranges first.
    i = 0
    n = len(data)
    while i < n - 4:
        if data[i:i + 3] == b"\x00\x00\x01":
            nb = data[i + 3]
            h264_type = nb & 0x1F
            hevc_type = (nb >> 1) & 0x3F
            hevc_ok = (nb & 0x80) == 0  # forbidden_zero must be 0
            if hevc_ok and hevc_type in HEVC_PARAM:
                return "hevc"
            if hevc_ok and hevc_type in HEVC_IRAP:
                return "hevc"
            if h264_type in (7, 8):
                return "h264"
            if h264_type == 5:
                return "h264"
            i += 3
        else:
            i += 1
    return None


def _looks_like_raw_h264(chunk: bytes) -> bool:
    """Heuristic: does ``chunk`` start with an Annex-B H.264 NAL start code?"""
    if len(chunk) < 5:
        return False
    # Annex B: 00 00 00 01 or 00 00 01, followed by a valid NAL header byte.
    if chunk[:4] == b"\x00\x00\x00\x01":
        nal_byte = chunk[4]
    elif chunk[:3] == b"\x00\x00\x01":
        nal_byte = chunk[3]
    else:
        return False
    # NAL header: forbidden_zero=0, nal_ref_idc 0-3, nal_unit_type 1-23
    if nal_byte & 0x80:  # forbidden_zero bit must be 0
        return False
    nal_type = nal_byte & 0x1F
    return 1 <= nal_type <= 23
