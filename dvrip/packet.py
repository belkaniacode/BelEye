"""Sofia/DVRIP wire packet codec.

Header layout (20 bytes, little-endian):

  offset  size  field
  ------  ----  -----------------
       0     1  head_flag  (0xFF)
       1     1  version    (0x01)
       2     1  reserved1  (0x00)
       3     1  reserved2  (0x00)
       4     4  session_id (u32)
       8     4  sequence   (u32)
      12     1  total_pkt
      13     1  cur_pkt
      14     2  msg_id     (u16)
      16     4  payload_len(u32)
      20     N  payload    (typically JSON bytes, often \\0-terminated)
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

log = logging.getLogger(__name__)

HEAD_FLAG = 0xFF
VERSION = 0x01
HEADER_SIZE = 20
HEADER_STRUCT = struct.Struct("<BBBBIIBBHI")


@dataclass(slots=True)
class Packet:
    msg_id: int
    payload: bytes = b""
    session_id: int = 0
    sequence: int = 0
    total: int = 0
    current: int = 0


def pack(p: Packet) -> bytes:
    """Serialize a Packet into wire bytes."""
    head = HEADER_STRUCT.pack(
        HEAD_FLAG,
        VERSION,
        0,
        0,
        p.session_id,
        p.sequence,
        p.total,
        p.current,
        p.msg_id,
        len(p.payload),
    )
    log.debug("[DVRIP] pack msg_id=%d len=%d session=0x%08x seq=%d",
              p.msg_id, len(p.payload), p.session_id, p.sequence)
    return head + p.payload


def unpack(buf: bytes, offset: int = 0) -> tuple[Packet | None, int]:
    """Parse one Packet from ``buf`` starting at ``offset``.

    Returns ``(packet, consumed_bytes)``. When the buffer does not yet hold a
    full packet, returns ``(None, 0)`` so the caller can wait for more bytes.
    """
    if len(buf) - offset < HEADER_SIZE:
        return None, 0

    head_flag, version, _r1, _r2, session_id, sequence, total, current, msg_id, payload_len = \
        HEADER_STRUCT.unpack_from(buf, offset)

    if head_flag != HEAD_FLAG:
        # Resync: scan forward for the next 0xFF and let the caller retry.
        idx = buf.find(bytes([HEAD_FLAG]), offset + 1)
        if idx < 0:
            log.warning("[DVRIP] unpack: no head flag found, dropping %d bytes",
                        len(buf) - offset)
            return None, len(buf) - offset
        log.warning("[DVRIP] unpack: bad head=0x%02x at %d, resync to %d",
                    head_flag, offset, idx)
        return None, idx - offset

    if version != VERSION:
        log.warning("[DVRIP] unpack: unexpected version=0x%02x msg_id=%d",
                    version, msg_id)

    end = offset + HEADER_SIZE + payload_len
    if end > len(buf):
        return None, 0

    payload = bytes(buf[offset + HEADER_SIZE:end])
    log.debug("[DVRIP] unpack msg_id=%d len=%d session=0x%08x seq=%d",
              msg_id, payload_len, session_id, sequence)
    return Packet(
        msg_id=msg_id,
        payload=payload,
        session_id=session_id,
        sequence=sequence,
        total=total,
        current=current,
    ), end - offset
