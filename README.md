<div align="center">

<img src="resources/icons/beleye-256.png" alt="BelEye" width="128" height="128" />

# BelEye

**A modern desktop client for Chinese NVRs and IP cameras — without the cloud.**

Connect a Xiongmai/DVRIP recorder by its IP and see every channel.
Or add any RTSP camera. Nothing leaves your network.

Linux · Windows · macOS — free and open source (MIT)

[Compatibility](docs/COMPATIBILITY.md) · [Usage](docs/USAGE.md) · [Architecture](docs/ARCHITECTURE.md) · [Русская версия](README.ru.md)

</div>

---

## The problem this solves

You bought an 8-channel PoE NVR from AliExpress. It works — but the software does not.
What comes with it is a Windows-only desktop client that looks like it was written in 2013, and a
mobile app that routes your video through a cloud server in another country. There is usually no
Linux client at all, and P2P cloud features on budget recorders have a long public track record of
security problems.

**BelEye is the local alternative.** One IP address in, all your channels out. No account, no
cloud, no telemetry, no phone-home. Your video never leaves your LAN.

## Will it work with my recorder?

**If your recorder is one of these, it will very likely work.** Check in ten seconds:

```bash
nc -vz 192.168.1.108 34567     # your recorder's IP
```

If that port is open, your device speaks **DVRIP** (also called the *Sofia* protocol) — the
protocol the standard phone apps for these recorders use, and the one BelEye speaks.
Hangzhou Xiongmai is an OEM that supplies **over a hundred** downstream brands and
sells almost nothing under its own name, so this single protocol covers a large share of the
affordable market: most of the unbranded 4/8/16-channel PoE recorders sold on AliExpress, Amazon
and eBay are the same firmware behind different logos.

> **Don't judge by the chip.** Listings advertise *Mstar*, *HiSilicon*, *Novatek*. That tells you
> nothing — the **firmware** decides compatibility, not the silicon. The same chip with
> Hikvision-style firmware will not work; a different chip running Xiongmai firmware will.

| | Support |
|---|---|
| **Xiongmai / DVRIP recorders** — NVR, DVR, HVR, PoE, 4/8/16-channel | ✅ Live view, archive, export |
| **Any IP camera with RTSP** — Hikvision, Dahua, Reolink, Uniview, TP-Link, no-name | ✅ Live view |
| Hikvision / Dahua / Uniview **recorders** | ❌ Closed protocols, not supported |

Verified on hardware: **Xiongmai NBD80S16S-KL**, 8-channel PoE HVR, firmware `V4.03.R11`, H.265,
with face / human / vehicle detection — sold under dozens of names.
Full details and honest caveats: **[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)**.

## Screenshot

> _Add `docs/screenshot.png` here._

## Features

**Recorders (DVRIP / Sofia)**
- **One IP, every camera** — connect the recorder and the channel list is discovered for you.
  No per-camera RTSP paths, no per-camera passwords.
- **H.265 and H.264** live view, decoded through FFmpeg.
- **Archive playback** — a calendar with recorded days highlighted, a per-day file list, and a
  24-hour timeline you can click to seek anywhere in the day.
- **Playback speed ¼× to 8×**, pause and frame-accurate seek.
- **Export to mp4** — save any fragment by stream copy: no re-encoding, no quality loss, no CPU.
- **REC indicator** — a red badge on channels the recorder is actively recording.
- **Quality that follows your attention** — the grid uses sub streams to keep the recorder and
  the network calm; expanding a camera switches it to the full stream *without interrupting the
  picture*. One toolbar switch keeps every camera at full quality if you prefer.

**IP cameras (RTSP)**
- Works with any RTSP camera, H.264 or H.265, TCP or UDP.
- Per-camera error messages right on the tile — `401 Unauthorized`, `Connection refused`,
  `timed out` — instead of a black rectangle.

**Everything**
- **Auto-arranging grid** — 1, 2×2, 3×3, 4×4 …; double-click to focus one camera.
- **Drag-and-drop reordering** of tiles, for recorder channels and cameras alike.
- **Automatic reconnection** with backoff, plus watchdogs that catch a stream that has gone quiet
  without dropping its connection.
- **One bad camera never freezes the app** — fully event-driven, nothing blocks the UI thread.
- **Light and dark themes**, following your system by default.
- **Passwords in the OS keyring** (GNOME Keyring, KWallet, macOS Keychain, Windows Credential
  Manager) — never in a config file.

## Install

Requires **Python 3.11+** and **FFmpeg** in `PATH`.

| OS | FFmpeg |
|----|--------|
| Arch / Manjaro | `sudo pacman -S ffmpeg` |
| Debian / Ubuntu | `sudo apt install ffmpeg` |
| Fedora | `sudo dnf install ffmpeg` |
| macOS | `brew install ffmpeg` |
| Windows | [gyan.dev builds](https://www.gyan.dev/ffmpeg/builds/) — add `bin/` to PATH |

```bash
git clone https://github.com/belkaniacode/BelEye.git
cd BelEye
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

On Linux, `./install-desktop.sh` adds BelEye to your application menu with its icon.

> **Wayland:** if video does not appear embedded correctly, force X11:
> `QT_QPA_PLATFORM=xcb python main.py`

## Adding a recorder

**⚙ Настройки → Добавить NVR** → host, port `34567`, user, password → **Проверить соединение**.
The channels appear by themselves. On an 8-channel unit with 4 cameras connected you will see 4
tiles — empty ports are hidden deliberately.

## Adding a single IP camera

**⚙ Настройки → Добавить камеру** → host, port `554`, credentials, and the **stream path**, which
is vendor-specific (`/Streaming/Channels/101` for Hikvision, `/cam/realmonitor?channel=1&subtype=0`
for Dahua, and so on — see the [cheat sheet](docs/COMPATIBILITY.md#ip-cameras-without-a-recorder)).
Leave **Transport** on `tcp` unless you have a reason not to.

There is no ONVIF auto-discovery yet, so the path has to be typed by hand.

## Shortcuts

| Key | Action |
|-----|--------|
| `F11` | Fullscreen |
| `Esc` | Exit fullscreen / return to grid / cancel reordering |
| `Ctrl+G` | Toggle grid ↔ single camera |
| Double-click | Expand a camera |
| Right-click | Edit / Reconnect / Archive / Remove |

## Troubleshooting

**The recorder does not connect.** Check `nc -vz <ip> 34567`. If the port is closed, the device
is not DVRIP-based, or the port was changed in its settings.

**Channels are not discovered.** Run with `BELEYE_LOG_LEVEL=DEBUG` and look for `[NVR] discovery`.
BelEye tries six firmware dialects; if yours needs a seventh, open an issue with that log line —
it is usually a small fix.

**An RTSP camera does not play.** The path is almost always the culprit. Verify it against your
camera's manual, then confirm with FFmpeg directly:
`ffmpeg -rtsp_transport tcp -i "rtsp://user:pass@ip:554/path" -t 3 -f null -`

**Choppy video over Wi-Fi.** Keep Transport on `tcp`. Some recorders' RTSP servers only answer
over TCP — the verified Xiongmai unit is one of them.

**Logs:** `~/.local/state/beleye/beleye.log` (Linux), `~/Library/Logs/beleye/beleye.log` (macOS),
`%LOCALAPPDATA%\beleye\Logs\beleye.log` (Windows).

## Built with

Python 3.11+ · PySide6 (Qt 6) · FFmpeg · a hand-written DVRIP/Sofia protocol implementation.
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Configuration

- Cameras and recorders: `~/.config/beleye/cameras.json`, `nvrs.json` — **no passwords**
- Passwords: OS keyring, service name `beleye`

## Contributing

The most valuable contribution is a **compatibility report**: your recorder's model, the firmware
string, and whether live view and archive worked. That is how the device list grows.

## License

MIT
