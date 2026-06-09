"""[A1] Probe LIVE OPMonitor first I-frame per channel.

Spawns a fresh DvripClient per channel (1..4), claims the Sub stream,
captures ~300 KB of clean Sofia output, decodes the first frame and
saves it to ``/tmp/probe_live_ch<N>.png``. The render plus a sha256
of the elementary stream lets us decide whether the cameras themselves
produce DISTINCT video — if all four look identical to the eye and
share the same OSD text, the playback-routes-to-CAM01 problem is a
hardware misconfiguration, not a protocol bug.

Run from project root with PYTHONPATH=. — keyring read is silent.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from datetime import datetime

import keyring
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from app.nvr_config import load_nvrs
from dvrip.client import DvripClient
from dvrip.sofia_frame import SofiaFrameParser, detect_codec

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("probe.live")


def main() -> int:
    app = QCoreApplication(sys.argv)
    nvrs = load_nvrs()
    if not nvrs:
        log.error("no NVRs configured")
        return 2
    nvr = nvrs[0]
    pwd = keyring.get_password("beleye", f"nvr:{nvr.id}")
    if not pwd:
        log.error("no password in keyring for nvr:%s", nvr.id)
        return 3

    results: dict[int, dict] = {}

    for ch in [1, 2, 3, 4]:
        log.info("===== ch=%d LIVE OPMonitor (Extra1) =====", ch)
        client = DvripClient(auto_discover=False)
        state = {
            "buf": bytearray(),
            "parser": SofiaFrameParser(),
            "done": False,
            "first_chunk_hex": None,
        }
        loop = QEventLoop()

        def on_login(_, c=ch):
            # Main is the only stream confirmed working in the user's live tiles
            # right now (Extra1 may be unconfigured on this NVR).
            client.start_monitor(c, stream_type="Main")

        def on_chunk(_hint, data, c=ch):
            if state["done"]:
                return
            if state["first_chunk_hex"] is None:
                state["first_chunk_hex"] = data[:16].hex()
            clean = state["parser"].feed(data)
            if not clean:
                return
            state["buf"].extend(clean)
            if len(state["buf"]) > 300_000:
                state["done"] = True
                codec = detect_codec(bytes(state["buf"])) or "h264"
                sha = hashlib.sha256(bytes(state["buf"])).hexdigest()[:16]
                results[c] = {
                    "codec": codec,
                    "sha": sha,
                    "size": len(state["buf"]),
                    "raw_first16": state["first_chunk_hex"],
                }
                # Persist .bin for re-render
                bin_path = f"/tmp/probe_live_ch{c}.bin"
                with open(bin_path, "wb") as f:
                    f.write(bytes(state["buf"]))
                results[c]["bin"] = bin_path
                log.info(
                    "ch=%d captured codec=%s sha=%s size=%d sofia_head=%s",
                    c, codec, sha, len(state["buf"]), state["first_chunk_hex"],
                )
                try:
                    client.stop_monitor(c)
                except Exception:
                    pass
                loop.quit()

        def on_failed(reason, c=ch):
            log.error("ch=%d login failed: %s", c, reason)
            loop.quit()

        client.loginOk.connect(on_login)
        client.videoChunk.connect(on_chunk)
        client.loginFailed.connect(on_failed)
        client.error.connect(lambda e, c=ch: log.warning("ch=%d net err: %s", c, e))
        client.connect_to(nvr.host, nvr.port, nvr.username, pwd)
        QTimer.singleShot(12000, lambda: (log.warning("ch=%d TIMEOUT", ch), loop.quit())[1])
        loop.exec()
        client.close()
        # Brief grace so server frees session before next iter
        grace = QTimer.singleShot(700, lambda: None)
        QEventLoop().processEvents()

    log.info("=========== summary ===========")
    for ch, r in results.items():
        log.info(
            "ch=%d sha=%s codec=%s size=%d head=%s bin=%s",
            ch, r["sha"], r["codec"], r["size"], r["raw_first16"], r["bin"],
        )

    distinct_hashes = {r["sha"] for r in results.values()}
    log.info(
        "distinct hashes: %d (need %d for cameras to be visibly different)",
        len(distinct_hashes), len(results),
    )

    # Render first frame to PNG for visual inspection
    for ch, r in results.items():
        png = f"/tmp/probe_live_ch{ch}.png"
        if os.path.exists(png):
            os.unlink(png)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", r["codec"], "-i", r["bin"],
                 "-frames:v", "1", "-vf", "scale=640:-2", png],
                capture_output=True, timeout=10,
            )
        except Exception:
            log.exception("ffmpeg render ch=%d failed", ch)
        size = os.path.getsize(png) if os.path.exists(png) else 0
        if size:
            r["png"] = png
            log.info("ch=%d rendered -> %s (%d B)", ch, png, size)
        else:
            log.warning("ch=%d render failed (no frame produced)", ch)

    log.info("[probe live] open /tmp/probe_live_ch{1..4}.png to compare OSD")
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
