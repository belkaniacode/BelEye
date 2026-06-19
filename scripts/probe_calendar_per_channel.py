"""[FIX archive2] Probe calendar correctness per channel.

For each enabled channel, query files for the last 7 days and check:
- Number of records returned
- All file paths contain the expected /00N/ segment for that channel
- Date range covered (min, max)
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

from app.nvr_config import load_nvrs
from dvrip.client import DvripClient


def probe(channel: int, app: QCoreApplication) -> dict:
    nvr = load_nvrs()[0]
    pw = keyring.get_password("beleye", f"nvr:{nvr.id}")
    client = DvripClient(app, auto_discover=False)
    res = {"channel": channel, "files": [], "wrong_channel": []}

    def on_login():
        end = datetime.now().replace(microsecond=0)
        begin = end - timedelta(days=7)
        client.query_files(channel, begin, end, chunk_days=2)

    def on_files(files):
        res["files"] = files
        QTimer.singleShot(50, app.quit)

    client.loginOk.connect(lambda *_: on_login())
    client.fileList.connect(on_files)
    client.connect_to(nvr.host, nvr.port, nvr.username, pw)
    QTimer.singleShot(45000, app.quit)
    app.exec()
    try:
        client.close()
    except Exception:
        pass

    expected_seg = f"/{channel:03d}/"
    dates = set()
    for f in res["files"]:
        if expected_seg not in f.file_name:
            res["wrong_channel"].append(f.file_name)
        dates.add(f.begin.strftime("%Y-%m-%d"))
    res["count"] = len(res["files"])
    res["dates"] = sorted(dates)
    res.pop("files", None)
    return res


def main() -> int:
    chans = [int(x) for x in sys.argv[1:]] or [1, 2, 3, 4]
    print(f"probing calendar per-channel: {chans}")
    for ch in chans:
        print(f"\n--- ch={ch} ---")
        app = QCoreApplication.instance() or QCoreApplication(sys.argv)
        r = probe(ch, app)
        status = "OK" if not r["wrong_channel"] else "MISMATCH"
        print(f"  {status} count={r['count']} dates={r['dates']} "
              f"wrong_channel={len(r['wrong_channel'])}")
        if r["wrong_channel"]:
            for w in r["wrong_channel"][:3]:
                print(f"    BAD: {w}")
        time.sleep(3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
