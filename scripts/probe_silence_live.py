"""[FIX archive2] Headless probe: verify playback streams ONLY archive frames
for the requested channel — no live leak, correct OSD watermark.

Per channel: login → query files for the last 24h → pick first record →
start_playback() → capture first 250 KB of MONITOR_DATA after Sofia-strip →
decode first I-frame to PNG via ffmpeg pipe. Saves to
.ai-factory/evidence/archive2_chN_first.png. Compare PNGs to confirm OSD
matches expected CAM0N. Reports per-channel pass/fail.

Usage:
    python scripts/probe_silence_live.py [CHANNEL ...]   # default: 1 2 3 4
"""
from __future__ import annotations

import os
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
from dvrip.sofia_frame import SofiaFrameParser

EVIDENCE_DIR = ROOT / ".ai-factory" / "evidence"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def probe_channel(nvr, password: str, channel: int, app: QCoreApplication) -> dict:
    result = {"channel": channel, "logged_in": False, "files": 0,
              "bytes": 0, "png": None, "error": None}

    client = DvripClient(app, auto_discover=False)
    parser = SofiaFrameParser()
    clean_buf = bytearray()
    target_bytes = 250_000

    def on_login_ok():
        result["logged_in"] = True
        end = datetime.now().replace(microsecond=0)
        begin = end - timedelta(hours=24)
        client.query_files(channel, begin, end, chunk_days=1)

    def on_file_list(files):
        result["files"] = len(files)
        if not files:
            QTimer.singleShot(100, app.quit)
            return
        f = files[0]
        print(f"  [ch{channel}] file={f.file_name} {f.begin}–{f.end}")
        client.start_playback(f.file_name, f.begin, f.end, channel=channel)
        QTimer.singleShot(12000, app.quit)

    def on_chunk(_ch, data):
        clean = parser.feed(data)
        if clean:
            clean_buf.extend(clean)
            result["bytes"] = len(clean_buf)
            if len(clean_buf) >= target_bytes:
                QTimer.singleShot(50, app.quit)

    def on_login_failed(reason):
        result["error"] = f"login failed: {reason}"
        QTimer.singleShot(50, app.quit)

    client.loginOk.connect(on_login_ok)
    client.loginFailed.connect(on_login_failed)
    client.fileList.connect(on_file_list)
    # [FIX archive3] Archive flows on playbackChunk (opcode 1422), not
    # videoChunk. Keep videoChunk wired to detect live-leak regression.
    client.videoChunk.connect(on_chunk)
    pb_bytes = bytearray()
    def on_pb_chunk(data):
        pb_bytes.extend(data)
        # Mirror into the same buf the script already processes downstream.
        clean = parser.feed(data)
        if clean:
            clean_buf.extend(clean)
            result["bytes"] = len(clean_buf)
            if len(clean_buf) >= target_bytes:
                QTimer.singleShot(50, app.quit)
    client.playbackChunk.connect(on_pb_chunk)

    print(f"  [ch{channel}] connecting {nvr.host}:{nvr.port} as {nvr.username}")
    client.connect_to(nvr.host, nvr.port, nvr.username, password)
    app.exec()

    try:
        client.stop_playback()
    except Exception:
        pass
    try:
        client.close()
    except Exception:
        pass

    result["pb_data_bytes"] = len(pb_bytes)
    if not clean_buf:
        result["error"] = result["error"] or "no clean ES bytes captured"
        return result

    es_path = EVIDENCE_DIR / f"archive2_ch{channel}.es"
    es_path.write_bytes(bytes(clean_buf))
    png_path = EVIDENCE_DIR / f"archive2_ch{channel}_first.png"

    for codec in ("hevc", "h264"):
        png_path.unlink(missing_ok=True)
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", codec,
               "-i", str(es_path), "-frames:v", "1",
               "-vf", "scale=640:-1", str(png_path)]
        rc = subprocess.run(cmd, capture_output=True).returncode
        if rc == 0 and png_path.exists() and png_path.stat().st_size > 1000:
            result["png"] = str(png_path)
            result["codec"] = codec
            return result
    result["error"] = "ffmpeg failed for both hevc/h264"
    return result


def main() -> int:
    channels = [int(c) for c in sys.argv[1:]] if len(sys.argv) > 1 else [1, 2, 3, 4]

    nvrs = load_nvrs()
    if not nvrs:
        print("no NVR configured", file=sys.stderr)
        return 2
    nvr = nvrs[0]
    password = keyring.get_password("beleye", f"nvr:{nvr.id}")
    if not password:
        print(f"no password in keyring for nvr:{nvr.id}", file=sys.stderr)
        return 2

    print(f"NVR {nvr.host}:{nvr.port} user={nvr.username}")
    print(f"probing channels: {channels}")

    results = []
    for ch in channels:
        print(f"\n--- channel {ch} ---")
        app = QCoreApplication.instance() or QCoreApplication(sys.argv)
        r = probe_channel(nvr, password, ch, app)
        results.append(r)
        print(f"  result: logged_in={r['logged_in']} files={r['files']} "
              f"bytes={r['bytes']} pb_data_bytes={r.get('pb_data_bytes', 0)} "
              f"png={r.get('png')} codec={r.get('codec')} "
              f"error={r.get('error')}")
        # Let NVR breathe between probes (avoid session-budget Ret=103 storms)
        time.sleep(3)

    print("\n=== SUMMARY ===")
    for r in results:
        status = "OK" if r.get("png") else "FAIL"
        print(f"  ch{r['channel']}: {status} bytes={r['bytes']} "
              f"png={r.get('png') or '-'} err={r.get('error') or '-'}")
    return 0 if all(r.get("png") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
