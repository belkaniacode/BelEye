"""[FIX stutter] Capture a live substream and audit the Sofia parser output.

Grabs ~30 s of raw MONITOR_DATA from one channel, runs SofiaFrameParser,
then audits:
  1. RAW side: histogram of Sofia frame markers + header/length sanity —
     any unknown marker or length mismatch corrupts everything after it.
  2. CLEAN side: walk Annex-B NALs in the parser output; count NAL types
     and flag "junk runs" — byte spans between start codes that are not
     valid NAL payloads (forbidden bit set on the header byte).
  3. Feed the clean ES to ffmpeg and count decoded frames vs
     'undecodable NALU' stderr lines.

Usage: python scripts/probe_sofia_integrity.py [CHANNEL] [SECONDS]
"""
from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import keyring
from PySide6.QtCore import QCoreApplication, QTimer

from app.nvr_config import load_nvrs
from dvrip.client import DvripClient
from dvrip.sofia_frame import SofiaFrameParser, MARKER_IFRAME, MARKER_PFRAME, MARKER_AUDIO, MARKER_INFO

EVIDENCE = ROOT / ".ai-factory" / "evidence"
EVIDENCE.mkdir(parents=True, exist_ok=True)


def capture(channel: int, seconds: int) -> bytes:
    nvr = load_nvrs()[0]
    pw = keyring.get_password("beleye", f"nvr:{nvr.id}")
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    c = DvripClient(app, auto_discover=False)
    raw = bytearray()
    c.videoChunk.connect(lambda _ch, d: raw.extend(d))
    c.loginOk.connect(lambda _s: c.start_monitor(channel, stream_type="Extra1"))
    c.connect_to(nvr.host, nvr.port, nvr.username, pw)
    QTimer.singleShot(seconds * 1000, app.quit)
    app.exec()
    try:
        c.close()
    except Exception:
        pass
    return bytes(raw)


def audit_raw(raw: bytes) -> None:
    """Walk the raw Sofia container, validating frame structure."""
    known = {MARKER_IFRAME: "I", MARKER_PFRAME: "P", MARKER_AUDIO: "A", MARKER_INFO: "N"}
    hist: dict[str, int] = {}
    bad_lengths = 0
    resyncs = 0
    i = 0
    n = len(raw)
    while i < n - 16:
        if raw[i:i+3] == b"\x00\x00\x01" and raw[i+3] in known:
            m = raw[i+3]
            if m == MARKER_PFRAME:
                length = struct.unpack_from("<I", raw, i+4)[0]
                total = 8 + length
            else:
                length = struct.unpack_from("<I", raw, i+12)[0]
                total = 16 + length
            if length > 4*1024*1024 or i + total > n + 65536:
                bad_lengths += 1
                i += 4
                continue
            hist[known[m]] = hist.get(known[m], 0) + 1
            i += total
        else:
            resyncs += 1
            j = raw.find(b"\x00\x00\x01", i+1)
            i = j if j > 0 else n
    print(f"  RAW: frames={hist} bad_lengths={bad_lengths} resync_gaps={resyncs}")


def audit_clean(clean: bytes) -> None:
    """Walk Annex-B NALs; flag headers with the forbidden bit set."""
    nal_types: dict[int, int] = {}
    bad_headers = 0
    spans = []
    i = 0
    n = len(clean)
    last = -1
    while i < n - 4:
        if clean[i:i+3] == b"\x00\x00\x01":
            hb = clean[i+3]
            if hb & 0x80:
                bad_headers += 1
            else:
                t = (hb >> 1) & 0x3F
                nal_types[t] = nal_types.get(t, 0) + 1
            if last >= 0:
                spans.append(i - last)
            last = i
            i += 3
        else:
            i += 1
    top = sorted(nal_types.items(), key=lambda kv: -kv[1])[:8]
    print(f"  CLEAN: nal_type_hist(top)={top} forbidden_bit_headers={bad_headers}")


def audit_decode(clean: bytes, channel: int) -> None:
    es = EVIDENCE / f"sofia_integrity_ch{channel}.es"
    es.write_bytes(clean)
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "warning",
         "-err_detect", "ignore_err",
         "-fflags", "nobuffer+discardcorrupt+genpts+igndts",
         "-f", "hevc", "-i", str(es), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    stderr = proc.stderr
    undec = stderr.count("undecodable NALU")
    frames = 0
    for line in stderr.splitlines():
        if line.startswith("frame="):
            frames = line
    # ffmpeg prints frame count on stderr progress; simpler: run ffprobe count
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0",
         "-err_detect", "ignore_err", "-f", "hevc", str(es)],
        capture_output=True, text=True,
    )
    print(f"  DECODE: undecodable_NALU_lines={undec} "
          f"decoded_frames={probe.stdout.strip() or '?'} es={es}")


def main() -> int:
    channel = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    print(f"capturing ch={channel} for {seconds}s (Extra1)...")
    raw = capture(channel, seconds)
    print(f"  captured {len(raw)} raw bytes")
    (EVIDENCE / f"sofia_integrity_ch{channel}.raw").write_bytes(raw)

    audit_raw(raw)
    parser = SofiaFrameParser()
    parser._name = "integrity"
    clean = parser.feed(raw)
    print(f"  parser stats: {parser.stats()}")
    audit_clean(clean)
    audit_decode(clean, channel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
