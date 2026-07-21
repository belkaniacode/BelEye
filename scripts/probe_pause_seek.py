"""[FIX archive-bp] Probe firmware support for playback Pause/Resume and
StartTime-based seek.

Test (a) — Pause/Resume:
  start playback → collect 3s → send 1420 Action=Pause → collect 3s
  (expect ~0 bytes) → send 1420 Action=Start → collect 3s (expect flow).

Test (b) — StartTime seek:
  claim the same file but with StartTime = file_begin + 10 min.
  Decode the first I-frame; the OSD timestamp must be ~mid-file, not the
  file's beginning.

Usage: python scripts/probe_pause_seek.py [CHANNEL]
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


def pick_file(client, channel, app, min_len_s=1200):
    """Login + query, return first record at least min_len_s long."""
    holder = {}

    def on_login(_sid):
        end = datetime.now().replace(microsecond=0)
        client.query_files(channel, end - timedelta(days=7), end)

    def on_files(files):
        for f in files:
            if (f.end - f.begin).total_seconds() >= min_len_s:
                holder["f"] = f
                break
        app.quit()

    client.loginOk.connect(on_login)
    client.fileList.connect(on_files)
    QTimer.singleShot(40000, app.quit)
    app.exec()
    return holder.get("f")


def test_pause(channel: int) -> dict:
    nvr = load_nvrs()[0]
    pw = keyring.get_password("beleye", f"nvr:{nvr.id}")
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    client = DvripClient(app, auto_discover=False)

    phase = ["play"]
    counts = {"play": 0, "paused": 0, "resumed": 0}

    def on_pb(data):
        counts[phase[0]] += len(data)
    client.playbackChunk.connect(on_pb)

    client.connect_to(nvr.host, nvr.port, nvr.username, pw)
    f = pick_file(client, channel, app)
    if f is None:
        client.close()
        return {"error": "no long-enough file"}
    print(f"  [pause-test] file={f.file_name}")

    client.start_playback(f.file_name, f.begin, f.end, channel=channel)

    def do_pause():
        phase[0] = "paused"
        params = client._pending_playback.get("Parameter", {})
        client._send(MsgId.PLAYBACK_REQ_START, {
            "Name": "OPPlayBack", "SessionID": client._sid_str(),
            "OPPlayBack": {"Action": "Pause", "Parameter": params},
        })
        print("  [pause-test] sent Pause")

    def do_resume():
        phase[0] = "resumed"
        params = client._pending_playback.get("Parameter", {})
        client._send(MsgId.PLAYBACK_REQ_START, {
            "Name": "OPPlayBack", "SessionID": client._sid_str(),
            "OPPlayBack": {"Action": "Start", "Parameter": params},
        })
        print("  [pause-test] sent Resume (Start)")

    QTimer.singleShot(3000, do_pause)
    QTimer.singleShot(6000, do_resume)
    QTimer.singleShot(9000, app.quit)
    app.exec()
    try:
        client.stop_playback()
        client.close()
    except Exception:
        pass
    return counts


def test_seek(channel: int) -> dict:
    nvr = load_nvrs()[0]
    pw = keyring.get_password("beleye", f"nvr:{nvr.id}")
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    client = DvripClient(app, auto_discover=False)
    parser = SofiaFrameParser()
    parser._name = "seek-test"
    buf = bytearray()

    def on_pb(data):
        clean = parser.feed(data)
        if clean:
            buf.extend(clean)
            if len(buf) > 200_000:
                QTimer.singleShot(50, app.quit)
    client.playbackChunk.connect(on_pb)

    client.connect_to(nvr.host, nvr.port, nvr.username, pw)
    f = pick_file(client, channel, app)
    if f is None:
        client.close()
        return {"error": "no long-enough file"}
    seek_pos = f.begin + timedelta(minutes=10)
    print(f"  [seek-test] file={f.file_name} begin={f.begin} seek_to={seek_pos}")

    # Claim with StartTime = seek position.
    body = {
        "Action": "Claim",
        "Parameter": {"FileName": f.file_name, "TransMode": "TCP"},
        "StartTime": seek_pos.strftime("%Y-%m-%d %H:%M:%S"),
        "EndTime": f.end.strftime("%Y-%m-%d %H:%M:%S"),
    }
    client._pending_playback = body
    client._send(MsgId.PLAYBACK_CLAIM_REQ_NEW, {
        "Name": "OPPlayBack", "SessionID": client._sid_str(),
        "OPPlayBack": body,
    })

    def send_start():
        sb = dict(body)
        sb["Action"] = "Start"
        client._send(MsgId.PLAYBACK_REQ_START, {
            "Name": "OPPlayBack", "SessionID": client._sid_str(),
            "OPPlayBack": sb,
        })
    QTimer.singleShot(300, send_start)
    QTimer.singleShot(12000, app.quit)
    app.exec()
    try:
        client.stop_playback()
        client.close()
    except Exception:
        pass

    res = {"bytes": len(buf), "expected_osd": seek_pos.strftime("%H:%M")}
    if buf:
        es = EVIDENCE_DIR / f"seek_ch{channel}.es"
        es.write_bytes(bytes(buf))
        png = EVIDENCE_DIR / f"seek_ch{channel}_first.png"
        for codec in ("hevc", "h264"):
            png.unlink(missing_ok=True)
            rc = subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error", "-f", codec,
                "-i", str(es), "-frames:v", "1", "-vf", "scale=640:-1",
                str(png)], capture_output=True).returncode
            if rc == 0 and png.exists() and png.stat().st_size > 1000:
                res["png"] = str(png)
                break
    return res


if __name__ == "__main__":
    ch = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    print(f"=== (a) Pause/Resume ch={ch} ===")
    r1 = test_pause(ch)
    print(f"  bytes per phase: {r1}")
    time.sleep(5)
    print(f"=== (b) StartTime seek ch={ch} ===")
    r2 = test_seek(ch)
    print(f"  result: {r2}")
    print()
    if isinstance(r1, dict) and "paused" in r1:
        paused_ratio = r1["paused"] / max(1, r1["play"])
        print(f"PAUSE verdict: paused/play byte ratio = {paused_ratio:.2f} "
              f"({'SUPPORTED' if paused_ratio < 0.2 and r1['resumed'] > 0 else 'NOT SUPPORTED'})")
    if "png" in (r2 or {}):
        print(f"SEEK verdict: check OSD on {r2['png']} — expect ~{r2['expected_osd']}")
