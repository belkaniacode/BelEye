<div align="center">

<img src="resources/icons/beleye-256.png" alt="BelEye" width="128" height="128" />

# BelEye

**A lightweight, modern desktop app for RTSP video surveillance.**
Built with Python, PySide6 and FFmpeg. Local-only, no cloud, no telemetry.

</div>

---

## Features

- **Multi-camera grid** — auto-arranging layout (1, 2×2, 3×3, 4×4 …)
- **Single-camera focus** — double-click a tile to zoom in, double-click again to return
- **Drag-and-drop reordering** — dedicated edit mode with Apply / Cancel
- **Low-latency playback** via FFmpeg (`-rtsp_transport tcp -fflags nobuffer -flags low_delay`)
- **Auto-reconnect** with exponential backoff for flaky streams
- **Per-camera error feedback** ("401 Unauthorized", "Connection refused", "timed out", …) right on the tile
- **One bad camera never freezes the UI** — fully event-driven QProcess pipeline, no blocking on the GUI thread
- **Modern dark UI** with Lucide-style icons, custom QSS theme, HiDPI-aware
- **OS keyring for credentials** (GNOME Keyring / KWallet / macOS Keychain / Windows Credential Manager) — passwords never sit in JSON
- **Cross-platform** — Linux (Wayland + X11), Windows, macOS

## Screenshot

> Add a screenshot here once you have one (e.g. `docs/screenshot.png`).

## Tech Stack

- Python 3.11+
- PySide6 (Qt 6, with QtSvg)
- FFmpeg / FFprobe (external runtime dependency)
- `platformdirs`, `keyring`

## System Requirements

- Python **3.11+**
- FFmpeg + FFprobe in `PATH`

| OS | Install FFmpeg |
|----|----------------|
| Arch / Manjaro | `sudo pacman -S ffmpeg` |
| Debian / Ubuntu | `sudo apt install ffmpeg` |
| Fedora | `sudo dnf install ffmpeg` |
| macOS | `brew install ffmpeg` |
| Windows | https://www.gyan.dev/ffmpeg/builds/ — add `bin/` to PATH |

## Installation

```bash
# 1. Clone
git clone git@github.com:belkaniacode/BelEye.git
cd BelEye

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate

# 3. Install Python dependencies
pip install -r requirements.txt
```

> HTTPS alternative: `git clone https://github.com/belkaniacode/BelEye.git`

## Running

```bash
python main.py
```

> **Wayland note:** if the video doesn't appear embedded properly, force the X11 backend:
> ```bash
> QT_QPA_PLATFORM=xcb python main.py
> ```

### Verbose logs

```bash
BELEYE_LOG_LEVEL=DEBUG python main.py
```

Log file location:

- Linux: `~/.local/state/beleye/beleye.log`
- macOS: `~/Library/Logs/beleye/beleye.log`
- Windows: `%LOCALAPPDATA%\beleye\Logs\beleye.log`

## Adding a Camera

1. Click **⚙ Настройки** → **Добавить камеру**
2. Fill in the form:
   - **Name** — any label (e.g. "Front gate")
   - **Host / IP** — e.g. `192.168.0.63`
   - **Port** — RTSP default is `554`
   - **User / Password** — camera credentials
   - **Path** — vendor-specific (see [docs/USAGE.md](docs/USAGE.md) for cheat sheet)
   - **Transport** — `tcp` recommended, `udp` for slightly lower latency
3. Click **Проверить соединение** to validate (uses `ffmpeg`, takes ~2 s)
4. **Save**

## Shortcuts

| Key | Action |
|-----|--------|
| `F11` | Fullscreen |
| `Esc` | Exit fullscreen / return to grid / exit reorder mode |
| `Ctrl+G` | Toggle grid ↔ single-camera mode |
| Double-click a tile | Expand camera to fill the window |
| Right-click a tile | Edit / Reconnect / Remove |

## Desktop Integration (Linux)

The project ships an installer that creates the `.desktop` entry and
copies icons into the hicolor theme:

```bash
./install-desktop.sh
```

After running it, BelEye appears in the system menu / dock with its
icon. The script is idempotent — safe to re-run after updates.

## Configuration & Data

- Camera list (no passwords): `~/.config/beleye/cameras.json`
- Passwords: stored in the system keyring under service name `beleye`
- Logs: see "Verbose logs" above

## Documentation

- [docs/USAGE.md](docs/USAGE.md) — full usage guide, RTSP path cheat sheet, troubleshooting
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — code layout and design notes

## Why

Most surveillance suites are heavy, paywalled, or cloud-bound.
BelEye is a small, hackable alternative for the case where you just
want to see your cameras on your own machine.

## License

MIT
