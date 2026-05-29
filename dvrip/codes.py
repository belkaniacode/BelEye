"""DVRIP message IDs (subset used by BelEye)."""

from enum import IntEnum


class MsgId(IntEnum):
    LOGIN_REQ2 = 1000
    LOGIN_RSP = 1001
    LOGOUT_REQ = 1002
    LOGOUT_RSP = 1003
    KEEPALIVE_REQ = 1006
    KEEPALIVE_RSP = 1007

    SYSINFO_REQ = 1020
    SYSINFO_RSP = 1021

    ABILITY_GET_REQ = 1360
    ABILITY_GET_RSP = 1361

    # Per-channel digital status (lists camera names; empty ports read "D05"…)
    DIGITAL_CHANNEL_STATUS_REQ = 1048
    DIGITAL_CHANNEL_STATUS_RSP = 1049

    CONFIG_GET_REQ = 1042
    CONFIG_GET_RSP = 1043

    # Verified against real hardware (Xiongmai NBD80S16S-KL, V4.03):
    #   Claim REQ 1413 -> RSP 1414 (Ret=100), then Start REQ 1410,
    #   binary video then streams in as MONITOR_DATA = 1412 packets.
    # (The "obvious" 1410/1411/1412/1413 ordering is WRONG for this firmware
    #  and gets the claim rejected with Ret=103.)
    MONITOR_CLAIM_REQ = 1413
    MONITOR_CLAIM_RSP = 1414
    MONITOR_START_REQ = 1410
    MONITOR_DATA = 1412
    MONITOR_STOP_REQ = 1413  # Action "Stop" on the claim opcode

    PLAYBACK_CLAIM_REQ = 1420
    PLAYBACK_CLAIM_RSP = 1421
    PLAYBACK_REQ = 1422
    PLAYBACK_DATA = 1423
    PLAYBACK_CTRL_REQ = 1424

    FILE_QUERY_REQ = 1440
    FILE_QUERY_RSP = 1441
