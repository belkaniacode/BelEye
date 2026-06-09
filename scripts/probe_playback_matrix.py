"""[A3] OPPlayBack variant matrix — find the body that actually routes per channel.

For each row in MATRIX:
    1. open a fresh DvripClient (no leftover session state),
    2. query the first 'normal' file for the row's channel,
    3. send the row's claim body, then START (auto-handled by client),
    4. collect ~250 KB of Sofia-clean payload,
    5. render first I-frame to PNG and ALSO sha-256 the elementary stream.

The row PASSES if two channels (ch=1 and ch=4) yield DIFFERENT
content. Visual diff via PNG saved next to it so the operator
double-checks the OSD watermark.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from datetime import datetime

import keyring
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from app.nvr_config import load_nvrs
from dvrip.client import DvripClient
from dvrip.codes import MsgId
from dvrip.sofia_frame import SofiaFrameParser, detect_codec

logging.basicConfig(level=logging.WARN, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("p.matrix")
log.setLevel(logging.INFO)
logging.getLogger("dvrip.client").setLevel(logging.WARNING)


# Each entry describes how to build the body and optional pre-steps
MATRIX = [
    # (id, opcode, has_param_channel, has_param_value_channel,
    #  has_top_channel, pre_opmonitor)
    ("01_current",                      1413, False, False, False, None),
    ("02_param_channel",                1413, True,  False, False, None),
    ("03_param_value=channel",          1413, False, True,  False, None),
    ("04_pre_opmonitor_start",          1413, False, False, False, "start"),
    ("05_pre_opmonitor_claim_only",     1413, False, False, False, "claim"),
    ("06_all_hints_plus_opmonitor",     1413, True,  True,  False, "start"),
    ("07_op1420",                       1420, False, False, False, None),
    ("08_op1420_all_hints",             1420, True,  True,  False, None),
    ("09_top_channel_only",             1413, False, False, True,  None),
]


def build_body(file_name: str, begin: datetime, end: datetime, channel: int,
               has_param_channel: bool, has_param_value_channel: bool,
               has_top_channel: bool) -> dict:
    params = {
        "PlayMode": "ByName", "FileName": file_name,
        "StreamType": 0,
        "Value": (channel - 1) if has_param_value_channel else 0,
        "TransMode": "TCP",
    }
    if has_param_channel:
        params["Channel"] = channel - 1
    body = {
        "Action": "Claim",
        "Parameter": params,
        "StartTime": begin.strftime("%Y-%m-%d %H:%M:%S"),
        "EndTime": end.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if has_top_channel:
        body["Channel"] = channel - 1
    return body


def capture_one(nvr, pwd, channel: int, row) -> dict:
    (rid, opcode, has_pc, has_pv, has_tc, pre_op) = row
    client = DvripClient(auto_discover=False)
    state = {
        "rec": None, "buf": bytearray(),
        "parser": SofiaFrameParser(), "done": False,
        "claim_seen": False, "started": False, "data_count": 0,
    }
    state["parser"]._name = f"{rid}:ch{channel}"
    loop = QEventLoop()

    orig = client._dispatch
    def my_dispatch(pkt):
        if pkt.msg_id in (1414, 1421) and not state["claim_seen"]:
            state["claim_seen"] = True
            try:
                body = json.loads(pkt.payload.rstrip(b"\x00").decode())
            except Exception:
                body = {"_raw": pkt.payload[:80].hex()}
            log.info("  %s ch=%d claim rsp opcode=%d Ret=%s",
                     rid, channel, pkt.msg_id, body.get("Ret"))
            if body.get("Ret") == 100:
                # send START
                start_body = dict(client._pending_playback)
                start_body["Action"] = "Start"
                # mirror Stop opcode mapping: for 1420 path use 1422 for start
                start_op = MsgId.PLAYBACK_REQ if pkt.msg_id == 1421 else MsgId.MONITOR_START_REQ
                client._send(start_op, {
                    "Name": "OPPlayBack",
                    "SessionID": client._sid_str(),
                    "OPPlayBack": start_body,
                })
                state["started"] = True
            else:
                state["done"] = True
                loop.quit()
            return
        if pkt.msg_id in (MsgId.MONITOR_DATA, MsgId.PLAYBACK_DATA) and state["started"]:
            state["data_count"] += 1
            clean = state["parser"].feed(pkt.payload)
            if clean:
                state["buf"].extend(clean)
            if len(state["buf"]) > 250_000 and not state["done"]:
                state["done"] = True
                loop.quit()
            return
        orig(pkt)
    client._dispatch = my_dispatch

    def on_login(_):
        client.query_files(channel=channel,
                           begin=datetime(2026, 6, 1),
                           end=datetime(2026, 6, 9, 23, 59, 59))
    def on_files(records):
        cont = [r for r in records if r.event_type == "normal"]
        if not cont:
            log.warning("  %s ch=%d no normal records", rid, channel)
            loop.quit(); return
        state["rec"] = cont[0]

        # Optional pre-OPMonitor step
        if pre_op:
            # Send OPMonitor claim for the target channel
            client._send(MsgId.MONITOR_CLAIM_REQ, {
                "Name": "OPMonitor",
                "SessionID": client._sid_str(),
                "OPMonitor": {
                    "Action": "Claim",
                    "Parameter": {
                        "Channel": channel - 1, "CombinMode": "NONE",
                        "StreamType": "Main", "TransMode": "TCP",
                    },
                },
            })
            if pre_op == "start":
                # Also send OPMonitor START (we don't wait on its Ret here)
                client._send(MsgId.MONITOR_START_REQ, {
                    "Name": "OPMonitor",
                    "SessionID": client._sid_str(),
                    "OPMonitor": {
                        "Action": "Start",
                        "Parameter": {
                            "Channel": channel - 1, "CombinMode": "NONE",
                            "StreamType": "Main", "TransMode": "TCP",
                        },
                    },
                })

        body = build_body(cont[0].file_name, cont[0].begin, cont[0].end, channel,
                          has_pc, has_pv, has_tc)
        client._pending_playback = body
        log.info("  %s ch=%d sending opcode=%d body=%s",
                 rid, channel, opcode, json.dumps(body))
        client._send(opcode, {
            "Name": "OPPlayBack",
            "SessionID": client._sid_str(),
            "OPPlayBack": body,
        })

    client.loginOk.connect(on_login)
    client.fileList.connect(on_files)
    client.loginFailed.connect(lambda r: (log.error("login %s", r), loop.quit()))
    client.connect_to(nvr.host, nvr.port, nvr.username, pwd)
    QTimer.singleShot(12000, loop.quit)
    loop.exec()
    client.close()
    # grace
    QTimer.singleShot(700, lambda: None); QEventLoop().processEvents()

    if not state["buf"]:
        return {"sha": None, "size": 0, "data_count": state["data_count"]}
    sha = hashlib.sha256(bytes(state["buf"])).hexdigest()[:16]
    codec = detect_codec(bytes(state["buf"])) or "hevc"
    bin_path = f"/tmp/matrix_{rid}_ch{channel}.bin"
    with open(bin_path, "wb") as f:
        f.write(bytes(state["buf"]))
    png_path = f"/tmp/matrix_{rid}_ch{channel}.png"
    if os.path.exists(png_path):
        os.unlink(png_path)
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", codec, "-i", bin_path,
         "-frames:v", "1", "-vf", "scale=480:-2", png_path],
        capture_output=True, timeout=8,
    )
    return {
        "sha": sha, "size": len(state["buf"]),
        "codec": codec, "bin": bin_path, "png": png_path,
        "data_count": state["data_count"],
        "file_name": state["rec"].file_name if state["rec"] else "",
    }


def main() -> int:
    app = QCoreApplication(sys.argv)
    nvr = load_nvrs()[0]
    pwd = keyring.get_password("beleye", f"nvr:{nvr.id}")

    verdicts = []
    for row in MATRIX:
        rid = row[0]
        log.info("============ %s ============", rid)
        r1 = capture_one(nvr, pwd, 1, row)
        r4 = capture_one(nvr, pwd, 4, row)
        s1, s4 = r1.get("sha"), r4.get("sha")
        distinct = bool(s1 and s4 and s1 != s4)
        verdict = "PASS distinct=yes" if distinct else "FAIL"
        if not s1:
            verdict = "FAIL no-data-ch1"
        if not s4:
            verdict = "FAIL no-data-ch4"
        verdicts.append({"id": rid, "verdict": verdict,
                         "ch1": r1, "ch4": r4})
        log.info("  %s ch1.sha=%s ch4.sha=%s -> %s",
                 rid, s1, s4, verdict)

    log.info("==================== MATRIX RESULTS ====================")
    for v in verdicts:
        ch1_size = v["ch1"]["size"]; ch4_size = v["ch4"]["size"]
        log.info("%s  ch1=%dB sha=%s  ch4=%dB sha=%s  -> %s",
                 v["id"], ch1_size, v["ch1"].get("sha"),
                 ch4_size, v["ch4"].get("sha"), v["verdict"])

    passing = [v for v in verdicts if v["verdict"].startswith("PASS")]
    log.info("==================== WINNERS ====================")
    if not passing:
        log.warning("NO variant produced distinct content. Need PCAP from XMEye.")
        return 5
    for v in passing:
        log.info("WINNER %s (cmp PNGs %s vs %s)",
                 v["id"], v["ch1"]["png"], v["ch4"]["png"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
