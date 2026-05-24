"""Sofia/DVRIP protocol client for Xiongmai-based NVRs (XMEye)."""

from .codes import MsgId
from .packet import Packet, pack, unpack, HEAD_FLAG, VERSION

__all__ = ["Packet", "pack", "unpack", "MsgId", "HEAD_FLAG", "VERSION"]
