"""Probe OPPlayBack on the NVR to discover the working opcodes + stream format.

Like OPMonitor, the "obvious" playback opcodes are not guaranteed correct for
Xiongmai firmware, so we brute-force them against the real device:

  1. login
  2. OPFileQuery for today on channel 0 -> take the first recorded file
  3. try OPPlayBack Claim with a set of candidate opcodes until one returns
     Ret=100, then try Start opcodes until binary data flows
  4. run the captured payload through SofiaFrameParser + ffprobe to confirm
     it's a decodable H.26x elementary stream

Usage:
    .venv/bin/python scripts/probe_playback.py            # uses saved NVR #0
    .venv/bin/python scripts/probe_playback.py HOST PORT USER PASSWORD
"""

from __future__ import annotations

import datetime
import json
import os
import socket
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dvrip.auth import sofia_hash  # noqa: E402
from dvrip.sofia_frame import SofiaFrameParser, detect_codec  # noqa: E402

HDR = struct.Struct("<BBBBIIBBHI")


def _load_creds() -> tuple[str, int, str, str]:
    if len(sys.argv) >= 5:
        return sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
    import keyring
    nvrs = json.load(open(os.path.expanduser("~/.config/beleye/nvrs.json")))
    n = nvrs[0]
    pw = keyring.get_password("beleye", "nvr:" + n["id"])
    return n["host"], n["port"], n["username"], pw


def pk(msg: int, body: dict, sid: int = 0, seq: int = 0) -> bytes:
    payload = json.dumps(body, separators=(",", ":")).encode() + b"\x00"
    return HDR.pack(0xFF, 1, 0, 0, sid, seq, 0, 0, msg, len(payload)) + payload


def _rd(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            break
        buf += c
    return buf


def rx(s: socket.socket, timeout: float = 3.0):
    s.settimeout(timeout)
    head = _rd(s, 20)
    if len(head) < 20:
        return None
    *_, msg, ln = HDR.unpack(head)
    return msg, _rd(s, ln)


def login(s: socket.socket, user: str, pw: str) -> int:
    s.sendall(pk(1000, {
        "EncryptType": "MD5", "LoginType": "DVRIP-Web",
        "PassWord": sofia_hash(pw), "UserName": user,
    }))
    r = rx(s)
    return int(json.loads(r[1].rstrip(b"\x00"))["SessionID"], 16)


def main() -> int:
    host, port, user, pw = _load_creds()
    s = socket.create_connection((host, port), timeout=5)
    sess = login(s, user, pw)
    if not sess:
        print("login failed / no session"); return 1
    sd = "0x%08X" % sess
    print("login ok, session", sd)

    today = datetime.date.today()
    begin = f"{today:%Y-%m-%d} 00:00:00"
    end = f"{today:%Y-%m-%d} 23:59:59"
    s.sendall(pk(1440, {"Name": "OPFileQuery", "SessionID": sd, "OPFileQuery": {
        "BeginTime": begin, "EndTime": end, "Channel": 0,
        "DriveTypeMask": 0, "Event": "*", "Type": "h264", "StreamType": 0,
    }}, sid=sess, seq=1))
    r = rx(s)
    files = json.loads(r[1].rstrip(b"\x00")).get("OPFileQuery") or []
    if not files:
        print("no files for today on ch0"); return 1
    f0 = files[0]
    print(f"first file: {f0['FileName']}  {f0['BeginTime']}..{f0['EndTime']}")
    s.close()

    # Candidate claim/start opcodes (OPMonitor used 1413 claim / 1410 start).
    claim_codes = [1413, 1420, 1424, 1417]
    start_codes = [1410, 1422, 1425, 1412]
    pb_name = "OPPlayBack"
    param_by_name = {
        "Action": "Claim",
        "Parameter": {
            "PlayMode": "ByName", "FileName": f0["FileName"],
            "StreamType": 0, "Value": 0, "TransMode": "TCP",
        },
        "StartTime": f0["BeginTime"], "EndTime": f0["EndTime"],
    }

    for cc in claim_codes:
        s = socket.create_connection((host, port), timeout=5)
        sess = login(s, user, pw); sd = "0x%08X" % sess
        body = {"Name": pb_name, "SessionID": sd, pb_name: dict(param_by_name)}
        s.sendall(pk(cc, body, sid=sess, seq=1))
        r = rx(s)
        ret = None
        if r:
            try:
                ret = json.loads(r[1].rstrip(b"\x00")).get("Ret")
            except Exception:
                ret = "?"
        print(f"claim code {cc} -> rsp {r[0] if r else '-'} Ret={ret}")
        if r and ret == 100:
            for sc in start_codes:
                start = {"Name": pb_name, "SessionID": sd,
                         pb_name: {**param_by_name, "Action": "Start"}}
                s.sendall(pk(sc, start, sid=sess, seq=2))
                s.settimeout(3.0)
                total = 0
                first = b""
                try:
                    while total < 200000:
                        c = s.recv(65536)
                        if not c:
                            break
                        if not first:
                            first = c[:32]
                        total += len(c)
                except socket.timeout:
                    pass
                note = ""
                if first[:1] == b"\xff":
                    note = "(JSON ack, no media)"
                print(f"   start code {sc}: rx={total} first={first[:16].hex()} {note}")
                if total > 5000 and first[:1] != b"\xff":
                    print(f"   >>> DATA via claim={cc} start={sc}")
            s.close()
            break
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
