"""[FIX speed2] Probe DVRIP Fast/Slow on this firmware AND verify ffmpeg
can decode the resulting (often I-only) trick-play stream with the new
flags.

Sequence:
  login → file query → start playback (1424 Claim + 1420 Start)
  collect 1422 bytes for 3s as "normal"
  send 1420 Action=Fast → 3s more as "fast"
  Strip Sofia, feed each phase's bytes to ffmpeg with the SAME flags
  the player uses now, and check whether any frames were decoded.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import keyring
from PySide6.QtCore import QCoreApplication, QTimer

import subprocess
import tempfile
from app.nvr_config import load_nvrs
from dvrip.client import DvripClient
from dvrip.codes import MsgId
from dvrip.sofia_frame import SofiaFrameParser


def main() -> int:
    channel = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    nvr = load_nvrs()[0]
    pw = keyring.get_password("beleye", f"nvr:{nvr.id}")
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    client = DvripClient(app, auto_discover=False)

    phase = ["normal"]
    phase_bytes = {"normal": 0, "fast": 0, "slow": 0}
    phase_raw = {"normal": bytearray(), "fast": bytearray(), "slow": bytearray()}

    orig_dispatch = client._dispatch
    def patched(pkt):
        mid = int(pkt.msg_id)
        if mid not in (1412, 1422, 1007, 1441):
            try:
                txt = pkt.payload.decode("utf-8", "replace").rstrip("\x00")[:150]
                print(f"  recv mid={mid} body={txt}")
            except Exception:
                pass
        orig_dispatch(pkt)
    client._dispatch = patched

    def on_pb(data):
        phase_bytes[phase[0]] += len(data)
        phase_raw[phase[0]].extend(data)
    client.playbackChunk.connect(on_pb)

    def on_login(_sid):
        end = datetime.now().replace(microsecond=0)
        begin = end - timedelta(days=7)
        client.query_files(channel, begin, end)

    def on_files(files):
        if not files:
            app.quit()
            return
        f = files[0]
        print(f"  playing {f.file_name}")
        client.start_playback(f.file_name, f.begin, f.end, channel=channel)
        QTimer.singleShot(3000, switch_fast)
        QTimer.singleShot(6000, switch_slow)
        QTimer.singleShot(9000, app.quit)

    def switch_fast():
        phase[0] = "fast"
        print("  >>> sending Fast")
        client.playback_fast()

    def switch_slow():
        phase[0] = "slow"
        print("  >>> sending Slow")
        client.playback_slow()

    client.loginOk.connect(on_login)
    client.fileList.connect(on_files)
    client.connect_to(nvr.host, nvr.port, nvr.username, pw)
    QTimer.singleShot(20000, app.quit)
    app.exec()
    try:
        client.stop_playback()
        client.close()
    except Exception:
        pass

    print("\n=== BYTES PER PHASE ===")
    for k, v in phase_bytes.items():
        print(f"  {k}: {v} bytes  (~{v / 3:.0f} B/s)")

    print("\n=== FFMPEG TRICK-PLAY DECODE PER PHASE ===")
    for phase_name in ("normal", "fast", "slow"):
        raw = bytes(phase_raw[phase_name])
        if not raw:
            print(f"  {phase_name}: no data")
            continue
        parser = SofiaFrameParser()
        parser._name = phase_name
        clean = parser.feed(raw)
        if not clean:
            print(f"  {phase_name}: Sofia returned 0 clean bytes")
            continue
        for codec in ("hevc", "h264"):
            with tempfile.NamedTemporaryFile(suffix=".es", delete=False) as f:
                f.write(clean)
                es = f.name
            rc = subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-fflags", "nobuffer+discardcorrupt+genpts+igndts",
                "-flags", "low_delay",
                "-err_detect", "ignore_err",
                "-f", codec, "-i", es,
                "-vframes", "30",
                "-f", "null", "-",
            ], capture_output=True)
            stderr = rc.stderr.decode("utf-8", "replace")[:200]
            print(f"  {phase_name} codec={codec}: rc={rc.returncode} "
                  f"clean_bytes={len(clean)} stderr_head={stderr.strip()[:120]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
