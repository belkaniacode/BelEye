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

    # [FIX archive3] DVRIP archive playback opcodes — Sofia/Xiongmai-flavour.
    # Hardware-verified on Xiongmai NBD80S16S-KL: 1424 Claim → 1420 Start →
    # archive data streams on 1422. (alexshpilkin/dvrip reference says data
    # arrives on 1426, but this firmware emits on 1422 — we keep the canonical
    # constant names for clarity.)
    #
    # Sequence:
    #   1. Send PLAYBACK_CLAIM_REQ_NEW (1424) with Action="Claim",
    #      Parameter={FileName, TransMode:"TCP"}, StartTime, EndTime.
    #   2. Receive PLAYBACK_CLAIM_RSP_NEW (1425) with Ret=100.
    #   3. Send PLAYBACK_REQ_START (1420) with Action="Start", same body.
    #   4. Receive archive video on PLAYBACK_DATA_STREAM (1422).
    #   5. To stop, send PLAYBACK_REQ_START (1420) with Action="Stop".
    PLAYBACK_REQ_START = 1420       # opcode for Action=Start/Stop/Pause/Fast/Slow
    PLAYBACK_REQ_START_RSP = 1421   # reply for 1420
    PLAYBACK_DATA_STREAM = 1422     # archive video data stream (was misnamed PLAYBACK_REQ)
    PLAYBACK_DATA_LEGACY = 1423     # unused on this firmware
    PLAYBACK_CLAIM_REQ_NEW = 1424   # opcode for Action=Claim
    PLAYBACK_CLAIM_RSP_NEW = 1425   # reply for 1424
    PLAYBACK_STREAM_DATA_LEGACY = 1426  # unused on this firmware (per alexshpilkin)

    # Old aliases kept for any straggler call-sites; new code uses the names
    # above.
    PLAYBACK_CLAIM_REQ = 1420
    PLAYBACK_CLAIM_RSP = 1421
    PLAYBACK_REQ = 1422
    PLAYBACK_DATA = 1423
    PLAYBACK_CTRL_REQ = 1424
    PLAYBACK_CTRL_RSP = 1425
    PLAYBACK_STREAM_DATA = 1426

    FILE_QUERY_REQ = 1440
    FILE_QUERY_RSP = 1441
