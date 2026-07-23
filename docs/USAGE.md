# Using BelEye

[Русская версия](USAGE.ru.md)

## Connecting a recorder (NVR / DVRIP / Sofia, port 34567)

If your cameras are wired into a Chinese recorder running Xiongmai firmware — which is most of the
budget NVRs configured over port 34567 — you only need **one** address. BelEye finds every
connected camera by itself and picks up the recording status.
To identify your model, see [compatibility](COMPATIBILITY.md).

1. ⚙ **Настройки** → **Добавить NVR**
2. Fill in the form:
   - **Name** — anything you like ("Home", "Warehouse")
   - **Host / IP** — e.g. `192.168.0.60`
   - **Port** — `34567` (the DVRIP default)
   - **User / Password** — the recorder's credentials
3. **Проверить соединение** — logs in and reads the digital channel status. After a second or two
   you get "Найдено каналов: N" with the real camera names.
4. **Save.** The grid shows tiles only for ports that **actually have a camera** — empty ports
   (`D05`, `D06`, …) are hidden.

On the next launch BelEye polls the recorder again and refreshes the list if you added or removed
a camera. Nothing to do by hand.

### Recording indicator

A channel that is being recorded right now shows a red **● REC** badge in the top-left corner of
its tile. The signal comes from the recorder's own record schedule (`Record` config) and refreshes
every 30 seconds.

### Archive playback

1. **Right-click** an NVR channel tile → **Архив…**
2. The window has four parts:
   - **Calendar, top left.** Days that contain recordings are highlighted; days with
     event-triggered recordings get a different colour.
   - **File list, bottom left** — the fragments for the selected day (start–end time and size).
   - **Player, right.** Double-click a fragment, or click the timeline, and playback starts
     straight from the recorder (HEVC or H.264, decoded through FFmpeg).
   - **Timeline, bottom** — 00:00 to 24:00. Filled blocks are recordings, the red vertical line is
     the playback position. Click anywhere to seek to that second.
3. **Transport controls:** ▶ play / ⏸ pause / ■ stop, and a speed button cycling
   ¼× → ½× → 1× → 2× → 4× → 8×.
4. **Shortcuts:** `Space` play/pause, `Esc` close.

Navigating to a month with no recordings makes BelEye walk back automatically until it finds one
(up to six months), so an empty calendar usually means the recordings really have rotated out.

### Exporting a fragment to mp4

In the archive window select a fragment → **Экспорт…** → choose a path. The video is saved
**without re-encoding** (`-c copy`): no CPU load, and the quality is bit-identical to the original.

## Adding a single IP camera (RTSP)

1. Click **⚙ Настройки** in the toolbar
2. **Добавить камеру** → fill in:
   - **Name** — anything ("Front door")
   - **Host / IP** — e.g. `192.168.0.63`
   - **Port** — usually `554`
   - **User / Password** — the camera's credentials
   - **Path** — vendor-specific, see the table below
   - **Transport** — `tcp` is recommended; `udp` only if your camera has trouble with TCP
3. **Проверить соединение** — FFmpeg tries to pull two seconds of real video, over the same
   transport playback will use
4. **Save** — the camera appears in the grid immediately

## Shortcuts

| Key | Action |
|-----|--------|
| `F11` | Fullscreen |
| `Esc` | Exit fullscreen / back to the grid |
| `Ctrl+G` | Toggle grid ↔ single camera |
| Double-click a tile | Expand that camera |
| Right-click a tile | Edit / Reconnect / Archive / Remove |

## RTSP paths by vendor

| Vendor | Example path |
|--------|--------------|
| Xiongmai / DVRIP | `/user=admin&password=PASS&channel=1&stream=0.sdp` (`stream=1` = sub) |
| Hikvision | `/Streaming/Channels/101` (main), `/102` (sub) |
| Dahua | `/cam/realmonitor?channel=1&subtype=0` |
| Reolink | `/h264Preview_01_main`, `/h264Preview_01_sub` |
| TP-Link Tapo | `/stream1` (HD), `/stream2` (SD) |
| Uniview | `/media/video1` |
| ONVIF generic | `/onvif1`, `/live/main`, `/live.sdp` |

If you do not know the path, check the camera's web interface or its manual.

## Testing from the terminal

```bash
ffmpeg -rtsp_transport tcp -i "rtsp://admin:password@192.168.0.63:554/path" -t 3 -f null -
```

If that works in a terminal it will work in BelEye — put the same values into the camera form.

## Troubleshooting

### Black tile, or an error message on it

- Confirm FFmpeg is installed: `which ffmpeg`
- Try the URL by hand with the command above — the error it prints is usually the whole story
- Check the network path to the camera: `ping <host>`, `nc -zv <host> 554`
- Try switching transport `tcp` ↔ `udp`

### The recorder connects but no channels are found

Run with `BELEYE_LOG_LEVEL=DEBUG` and look for `[NVR] discovery`. BelEye tries six firmware
dialects; if yours needs a seventh, that log line is exactly what an issue report needs.

### High latency

- Keep the transport on `tcp` — it is more stable over Wi-Fi
- Use the camera's sub stream (lower resolution → lower latency)
- For recorders, leave the quality switch off so the grid uses sub streams

### Passwords are not saved

On Linux install a keyring service:

```bash
sudo pacman -S gnome-keyring    # Arch
sudo apt install gnome-keyring  # Debian/Ubuntu
```

Diagnose with `python -c "import keyring; print(keyring.get_keyring())"`.

### Video does not render correctly under Wayland

```bash
QT_QPA_PLATFORM=xcb python main.py
```

## Files

- `~/.config/beleye/cameras.json` — RTSP cameras (no passwords)
- `~/.config/beleye/nvrs.json` — recorders and their channels (no passwords)
- `~/.local/state/beleye/beleye.log` — log
- System keyring — passwords
