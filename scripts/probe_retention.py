"""[A2] Probe NVR retention — issue OPFileQuery for several months back.

Tells us whether the NVR genuinely only keeps ~3 days, or whether our
current single-month load is missing data that exists earlier.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime

import keyring
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from app.nvr_config import load_nvrs
from dvrip.client import DvripClient

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("probe.retention")
log.setLevel(logging.INFO)


def main() -> int:
    app = QCoreApplication(sys.argv)
    nvr = load_nvrs()[0]
    pwd = keyring.get_password("beleye", f"nvr:{nvr.id}")

    months = [
        (2026, 6),
        (2026, 5),
        (2026, 4),
        (2026, 3),
        (2026, 2),
        (2026, 1),
    ]
    summary: dict[tuple[int, int], dict] = {}

    client = DvripClient(auto_discover=False)
    loop_login = QEventLoop()
    client.loginOk.connect(lambda _: loop_login.quit())
    client.loginFailed.connect(lambda r: (log.error("login %s", r), loop_login.quit()))
    client.connect_to(nvr.host, nvr.port, nvr.username, pwd)
    QTimer.singleShot(5000, loop_login.quit)
    loop_login.exec()
    if not client._logged_in:
        return 4

    for (year, month) in months:
        log.info("==== probing %04d-%02d ====", year, month)
        first = datetime(year, month, 1)
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        last = datetime(next_year, next_month, 1)
        from datetime import timedelta
        last = last - timedelta(seconds=1)

        local_loop = QEventLoop()
        out: dict = {"records": None}

        def on_files(records):
            out["records"] = records
            local_loop.quit()

        conn = client.fileList.connect(on_files)
        client.query_files(channel=1, begin=first, end=last)
        QTimer.singleShot(10_000, local_loop.quit)
        local_loop.exec()
        client.fileList.disconnect(conn)

        records = out["records"] or []
        days = sorted({r.begin.date() for r in records})
        summary[(year, month)] = {
            "total": len(records),
            "days_with_records": days,
        }
        log.info(
            "[probe retention] month=%04d-%02d total_files=%d earliest=%s latest=%s",
            year, month, len(records),
            min(days).isoformat() if days else "-",
            max(days).isoformat() if days else "-",
        )

    client.close()

    log.info("==== SUMMARY ====")
    for (y, m), info in summary.items():
        log.info(
            "%04d-%02d  files=%d  days=%d  first=%s  last=%s",
            y, m, info["total"], len(info["days_with_records"]),
            info["days_with_records"][0].isoformat() if info["days_with_records"] else "-",
            info["days_with_records"][-1].isoformat() if info["days_with_records"] else "-",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
