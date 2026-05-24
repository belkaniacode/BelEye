"""
Stream probe helper for the "Test connection" button in the settings dialog.

We use `ffmpeg -t 2 -i URL -f null -` rather than `ffprobe`, because:
- ffmpeg honors -stimeout/-rw_timeout for RTSP reliably
- It actually tries to read 2 seconds of stream, so a passing probe
  means we can really get frames (not just open a socket)
- Same binary that the player uses — fewer moving parts
"""

from __future__ import annotations

import logging
import re
import subprocess

from .ffmpeg_player import find_ffmpeg

log = logging.getLogger(__name__)

_RE_VIDEO = re.compile(r"Stream #\d+:\d+.*?Video:\s*([^,]+).*?(\d{2,5}x\d{2,5})", re.IGNORECASE)


def probe_rtsp(url: str, timeout_s: float = 8.0) -> tuple[bool, str]:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, "ffmpeg not installed"
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "info",
        "-rtsp_transport", "tcp",
        "-timeout", "5000000",
        "-t", "2",
        "-i", url,
        "-an",
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout (камера не отвечает)"
    except OSError as exc:
        return False, str(exc)

    stderr = result.stderr or ""
    m = _RE_VIDEO.search(stderr)
    if m:
        codec = m.group(1).strip()
        dims = m.group(2)
        return True, f"OK — {codec} {dims}"

    # Failed: extract a meaningful tail
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    informative = [
        ln for ln in lines
        if any(k in ln.lower() for k in ("error", "fail", "could not", "401", "403", "404", "refused", "unauthorized", "timed out", "no route"))
    ]
    msg = informative[-1] if informative else (lines[-1] if lines else f"exit {result.returncode}")
    return False, msg
