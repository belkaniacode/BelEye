"""[FIX archive3] Hardware verification — call the REAL DvripClient.start_playback
for each channel, capture playbackChunk bytes, decode first I-frame to PNG.

Success: for each channel ch=1..4 the PNG OSD shows the correct camera
(CAM0N) AND timestamp inside the requested file's window.
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
from dvrip.sofia_frame import SofiaFrameParser

EVIDENCE_DIR = ROOT / ".ai-factory" / "evidence"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def verify(channel: int, app: QCoreApplication) -> dict:
    nvr = load_nvrs()[0]
    pw = keyring.get_password("beleye", f"nvr:{nvr.id}")
    client = DvripClient(app, auto_discover=False)
    parser = SofiaFrameParser()
    parser._name = f"v3-ch{channel}"

    res = {"ch": channel, "file": None, "pb_bytes": 0,
           "mon_leak": 0, "png": None, "codec": None}
    clean_buf = bytearray()
    mon_buf = bytearray()
    target = 200_000

    def on_login(_sid):
        end = datetime.now().replace(microsecond=0)
        begin = end - timedelta(days=7)
        client.query_files(channel, begin, end, chunk_days=2)

    def on_files(files):
        if not files:
            res["err"] = "no files"
            QTimer.singleShot(50, app.quit)
            return
        f = files[0]
        res["file"] = f.file_name
        res["file_begin"] = f.begin.isoformat()
        print(f"  [ch{channel}] playing {f.file_name}")
        client.start_playback(f.file_name, f.begin, f.end, channel=channel)
        QTimer.singleShot(12000, app.quit)

    def on_pb(data):
        clean = parser.feed(data)
        if clean:
            clean_buf.extend(clean)
            res["pb_bytes"] = len(clean_buf)
            if len(clean_buf) >= target:
                QTimer.singleShot(50, app.quit)

    def on_live(_ch, data):
        mon_buf.extend(data)

    client.loginOk.connect(on_login)
    client.fileList.connect(on_files)
    client.playbackChunk.connect(on_pb)
    client.videoChunk.connect(on_live)
    print(f"  [ch{channel}] connecting")
    client.connect_to(nvr.host, nvr.port, nvr.username, pw)
    QTimer.singleShot(40000, app.quit)
    app.exec()

    try:
        client.stop_playback()
    except Exception:
        pass
    try:
        client.close()
    except Exception:
        pass

    res["mon_leak"] = len(mon_buf)
    if clean_buf:
        es_path = EVIDENCE_DIR / f"archive3v_ch{channel}.es"
        es_path.write_bytes(bytes(clean_buf))
        png_path = EVIDENCE_DIR / f"archive3v_ch{channel}_first.png"
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
                res["png"] = str(png_path)
                res["codec"] = codec
                break
    return res


def main() -> int:
    chs = [int(x) for x in sys.argv[1:]] or [1, 2, 3, 4]
    print(f"[FIX archive3] verify channels={chs}")
    results = []
    for ch in chs:
        print(f"\n--- ch={ch} ---")
        app = QCoreApplication.instance() or QCoreApplication(sys.argv)
        r = verify(ch, app)
        results.append(r)
        print(f"  result: pb_bytes={r['pb_bytes']} mon_leak={r['mon_leak']} "
              f"png={'YES' if r.get('png') else 'NO'} file={r.get('file')}")
        time.sleep(5)
    print("\n=== SUMMARY ===")
    ok = 0
    for r in results:
        good = bool(r.get("png")) and r["pb_bytes"] > 50_000 and r["mon_leak"] == 0
        if good:
            ok += 1
        print(f"  ch{r['ch']}: pb={r['pb_bytes']:>7}B leak={r['mon_leak']}B "
              f"png={'OK' if r.get('png') else '--'} "
              f"file_begin={r.get('file_begin')} → {'PASS' if good else 'FAIL'}")
    print(f"\n{ok}/{len(results)} channels passing")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
