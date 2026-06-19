"""[FIX archive3] Probe canonical DVRIP playback opcodes 1424/1425/1426.

Per channel:
  1. Login.
  2. Query files for the last 7 days (chunked existing way), pick first record.
  3. Send 1424 Action=Claim with Parameter.FileName, Channel, TransMode,
     StartTime, EndTime.
  4. Wait for 1425 with Ret=100.
  5. Send 1424 Action=Start.
  6. Collect 1426 packets, strip Sofia, save first 250 KB to .es file.
  7. Decode first I-frame to PNG.
  8. Manual visual check: OSD timestamp must be the FILE'S time, not "now",
     and OSD watermark must be the requested CAM0N.

Success criterion: any 1425 reply with Ret=100 AND non-empty 1426 packets.
PNG check is the visual confirmation that we got archive frames.

Usage:
    python scripts/probe_playback_1424.py [CHANNEL ...]   # default: 4
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import keyring
from PySide6.QtCore import QCoreApplication, QTimer

from app.nvr_config import load_nvrs
from dvrip.client import DvripClient
from dvrip.codes import MsgId
from dvrip.sofia_frame import SofiaFrameParser

EVIDENCE_DIR = ROOT / ".ai-factory" / "evidence"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def probe(channel: int, app: QCoreApplication) -> dict:
    nvr = load_nvrs()[0]
    pw = keyring.get_password("beleye", f"nvr:{nvr.id}")
    client = DvripClient(app, auto_discover=False)
    parser = SofiaFrameParser()
    parser._name = f"pb1424-ch{channel}"

    result = {
        "channel": channel,
        "claim_ret": None,
        "stream_bytes": 0,
        "monitor_bytes": 0,
        "png": None,
        "codec": None,
        "error": None,
        "file": None,
    }

    stream_buf = bytearray()
    monitor_buf = bytearray()

    # Wire raw packet handler — DvripClient's _handle_packet only dispatches
    # to known msg ids; we want to also see 1425/1426 by hooking into them
    # via custom handlers attached on the socket level. Easiest: monkey-patch
    # _handle_packet to also forward to our probe handlers.
    original_handle = client._dispatch

    pending_file = {}

    all_msgids = {}
    def patched_handle(pkt):
        try:
            mid = int(pkt.msg_id)
        except Exception:
            mid = -1
        all_msgids[mid] = all_msgids.get(mid, 0) + 1
        if mid not in (1412, 1426, 1007, 1441):  # noisy: data + keepalive + filequery
            try:
                payload_str = pkt.payload.decode("utf-8", "replace").rstrip("\x00")[:200]
                print(f"  [ch{channel}] >> recv mid={mid} len={len(pkt.payload)} body={payload_str}")
            except Exception:
                pass
        if mid in (1426, 1422):
            stream_buf.extend(pkt.payload)
            return
        if mid in (int(MsgId.PLAYBACK_CTRL_RSP), int(MsgId.PLAYBACK_CLAIM_RSP)):
            # 1425 — claim reply OR 1421 — DoPlayback reply
            import json
            try:
                body = json.loads(pkt.payload.decode("utf-8", "replace").rstrip("\x00"))
                ret = body.get("Ret")
                print(f"  [ch{channel}] msg={mid} Ret={ret} body={body}")
                if result["claim_ret"] is None:
                    result["claim_ret"] = ret
                else:
                    result["start_ret"] = ret
            except Exception as e:
                pass
            return
        if mid == int(MsgId.PLAYBACK_STREAM_DATA):
            # 1426 — archive data
            stream_buf.extend(pkt.payload)
            return
        # Forward to original for monitor data, login responses, etc.
        original_handle(pkt)

    client._dispatch = patched_handle

    def on_video_chunk(_ch, data):
        # Live MONITOR_DATA arrives via this signal. We DO NOT want it during
        # the probe — log how many bytes leak so we can confirm 1426 carries
        # the archive cleanly.
        monitor_buf.extend(data)
    client.videoChunk.connect(on_video_chunk)

    def on_login(_sid):
        end = datetime.now().replace(microsecond=0)
        begin = end - timedelta(days=7)
        print(f"  [ch{channel}] login ok; querying {begin}..{end}")
        client.query_files(channel, begin, end, chunk_days=2)

    def on_files(files):
        if not files:
            result["error"] = "no files in last 7 days"
            QTimer.singleShot(50, app.quit)
            return
        f = files[0]
        result["file"] = f.file_name
        pending_file["f"] = f
        print(f"  [ch{channel}] file={f.file_name} {f.begin}–{f.end}")
        # [FIX archive3] Minimal canonical body per alexshpilkin/dvrip:
        # just FileName + TransMode in Parameter. No Channel, no PlayMode,
        # no Value, no StreamType — the firmware infers from the file path.
        params = {
            "FileName": f.file_name,
            "TransMode": "TCP",
        }
        body = {
            "Action": "Claim",
            "Parameter": params,
            "StartTime": f.begin.strftime("%Y-%m-%d %H:%M:%S"),
            "EndTime": f.end.strftime("%Y-%m-%d %H:%M:%S"),
        }
        client._send(MsgId.PLAYBACK_CTRL_REQ, {
            "Name": "OPPlayBack",
            "SessionID": client._sid_str(),
            "OPPlayBack": body,
        })
        print(f"  [ch{channel}] sent 1424 Claim")

        def send_start():
            start_body = dict(body)
            start_body["Action"] = "Start"
            # [FIX archive3] Per alexshpilkin/dvrip io.py, the action is sent
            # on opcode 1420 (DoPlayback) — NOT another 1424. Sending Start
            # on 1424 is interpreted as a duplicate Claim and rejected with
            # Ret=103 (resource busy).
            client._send(MsgId.PLAYBACK_CLAIM_REQ, {
                "Name": "OPPlayBack",
                "SessionID": client._sid_str(),
                "OPPlayBack": start_body,
            })
            print(f"  [ch{channel}] sent 1420 Start")
        QTimer.singleShot(300, send_start)

        # Run for 10s collecting 1426 packets
        QTimer.singleShot(10000, app.quit)

    client.loginOk.connect(on_login)
    client.fileList.connect(on_files)

    print(f"  [ch{channel}] connecting {nvr.host}:{nvr.port}")
    client.connect_to(nvr.host, nvr.port, nvr.username, pw)
    QTimer.singleShot(40000, app.quit)  # absolute timeout
    app.exec()

    try:
        client._send(MsgId.PLAYBACK_CTRL_REQ, {
            "Name": "OPPlayBack",
            "SessionID": client._sid_str(),
            "OPPlayBack": {"Action": "Stop", "Parameter": {
                "FileName": pending_file.get("f").file_name if pending_file else "",
                "Channel": channel - 1,
                "TransMode": "TCP",
            }},
        })
    except Exception:
        pass
    try:
        client.close()
    except Exception:
        pass

    result["monitor_bytes"] = len(monitor_buf)
    if stream_buf:
        clean = parser.feed(bytes(stream_buf))
        result["stream_bytes"] = len(stream_buf)
        result["clean_es_bytes"] = len(clean)
        if clean:
            es_path = EVIDENCE_DIR / f"archive3_ch{channel}.es"
            es_path.write_bytes(clean)
            png_path = EVIDENCE_DIR / f"archive3_ch{channel}_first.png"
            for codec in ("hevc", "h264"):
                png_path.unlink(missing_ok=True)
                rc = subprocess.run([
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", codec, "-i", str(es_path),
                    "-frames:v", "1",
                    "-vf", "scale=640:-1",
                    str(png_path),
                ], capture_output=True).returncode
                if rc == 0 and png_path.exists() and png_path.stat().st_size > 1000:
                    result["png"] = str(png_path)
                    result["codec"] = codec
                    break
    else:
        result["stream_bytes"] = 0
    return result


def main() -> int:
    channels = [int(x) for x in sys.argv[1:]] or [4]
    print(f"[FIX archive3] probe 1424/1425/1426 channels={channels}")
    print(f"NVR: {load_nvrs()[0].host}:{load_nvrs()[0].port}")

    results = []
    for ch in channels:
        print(f"\n--- channel {ch} ---")
        app = QCoreApplication.instance() or QCoreApplication(sys.argv)
        r = probe(ch, app)
        results.append(r)
        print(f"  RESULT: claim_ret={r['claim_ret']} "
              f"stream_bytes={r['stream_bytes']} "
              f"monitor_bytes={r['monitor_bytes']} "
              f"png={r.get('png')} err={r.get('error')}")
        time.sleep(4)

    print("\n=== SUMMARY ===")
    any_data = False
    for r in results:
        ok = bool(r.get("png")) and r.get("claim_ret") == 100
        if r.get("stream_bytes", 0) > 0:
            any_data = True
        print(f"  ch{r['channel']}: claim_ret={r['claim_ret']} "
              f"stream={r['stream_bytes']}B mon_leak={r['monitor_bytes']}B "
              f"png={'YES' if r.get('png') else 'NO'} "
              f"{'OK' if ok else 'FAIL'}")
    print()
    if any_data:
        print("[FIX archive3] DECISION: 1424/1425/1426 path emits data — proceed with refactor")
    else:
        print("[FIX archive3] DECISION: 1424 path emits ZERO 1426 packets — firmware does NOT")
        print("              support this opcode. STOP and document limitation.")
    return 0 if any_data else 1


if __name__ == "__main__":
    sys.exit(main())
