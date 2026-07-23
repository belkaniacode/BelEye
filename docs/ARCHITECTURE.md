# BelEye architecture

[Русская версия](ARCHITECTURE.ru.md)

A layered PySide6 application. Four layers:

- `app/` — configuration (RTSP cameras + recorders), keyring, paths, logging. **No Qt imports.**
- `dvrip/` — the Sofia/DVRIP protocol client (port 34567): login, OPMonitor, OPPlayBack,
  OPFileQuery, the Sofia frame parser, codec auto-detection
- `video/` — FFmpeg process wrappers (pipe-mode decoding, export). A leaf module: it must not
  import from `ui/`.
- `ui/` — Qt widgets: grid, tiles, dialogs, the archive window, theming

`main.py` is the entry point that wires the layers together.

## Layout

```
beleye/
├── main.py                       # entry: logging → theme → ffmpeg check → MainWindow
├── app/
│   ├── paths.py                  # platformdirs (cameras.json, nvrs.json, log)
│   ├── logging_setup.py          # console + RotatingFileHandler
│   ├── config.py                 # CameraConfig — standalone RTSP cameras
│   ├── nvr_config.py             # NvrConfig + NvrChannel — recorders
│   ├── secrets.py                # keyring (service="beleye", username=id or "nvr:<id>")
│   └── rtsp.py                   # build_rtsp_url() with URL escaping
├── dvrip/
│   ├── codes.py                  # MsgId enum (hardware-verified opcodes)
│   ├── packet.py                 # 20-byte Sofia header pack/unpack
│   ├── auth.py                   # sofia_hash() — Xiongmai password hash
│   ├── client.py                 # DvripClient(QObject) — QTcpSocket, Qt signals
│   └── sofia_frame.py            # Sofia frame parser (FC/FD/FA/FB) + detect_codec()
├── video/
│   ├── ffmpeg_player.py          # FFmpegPlayer(QWidget): RTSP mode or pipe (H.264/HEVC)
│   ├── export.py                 # MP4Exporter — remux a Sofia-stripped stream to mp4
│   └── stream_monitor.py         # probe_rtsp() for the connection test
├── ui/
│   ├── theme.py                  # semantic color tokens, light/dark, system detection
│   ├── prefs.py                  # persistent UI preferences (QSettings)
│   ├── icon_util.py              # painted icons — no font or asset dependencies
│   ├── camera_widget.py          # CameraTile (RTSP) + DraggableTileMixin + overlay
│   ├── nvr_channel_widget.py     # NvrChannelTile = DvripClient + pipe FFmpegPlayer
│   ├── grid_view.py              # QStackedLayout: GRID / SINGLE / empty + quality policy
│   ├── camera_form.py            # CameraForm — RTSP camera CRUD
│   ├── nvr_form.py               # NvrForm — add a recorder + connection test
│   ├── settings_dialog.py        # SettingsDialog — camera and recorder list
│   ├── playback_view.py          # archive window: calendar + timeline + player + export
│   └── main_window.py            # MainWindow + persistent control client + REC poll
└── resources/
    └── styles.qss.tmpl           # QSS template, rendered per theme by ui/theme.py
```

## DVRIP, as it actually behaves on hardware

Every opcode and frame format here was **verified by talking to a real recorder** (Xiongmai
NBD80S16S-KL, firmware V4.03) and is recorded in `codes.py`. Several of them contradict what the
obvious ordering — or the reference implementation — would suggest:

| Stream | Claim REQ | Claim RSP | Start REQ | Data |
|--------|----------:|----------:|----------:|-----:|
| Live monitor (`OPMonitor`) | 1413 | 1414 (Ret=100) | 1410 | 1412 |
| Archive playback (`OPPlayBack`) | 1424 | 1425 (Ret=100) | 1420 | **1422** |

Two traps worth knowing before touching this code:

- The live claim/start order is **1413 then 1410**, not the numerically obvious 1410 then 1413.
  The obvious order gets the claim rejected with `Ret=103`.
- Archive data arrives on **1422**, although the widely cited reference implementation documents
  1426. On this firmware nothing ever appears on 1426.

Inside the data packets is a stream of **Sofia frames** marked `00 00 01 FC/FD/FA/FB`
(I-frame / P-frame / audio / info). The parser strips the wrappers and yields a clean H.264/HEVC
Annex-B stream, which goes to a pipe-mode `FFmpegPlayer` (`-f hevc|h264 -i pipe:0`). The codec is
auto-detected from the first parameter-set or IDR NAL.

The audio frame header is 8 bytes with a **u16** length at offset 6 — not the 16-byte layout the
video frames use. Getting this wrong makes the parser read G.711 samples as a ~1.6-billion-byte
length, forcing a resync on every audio frame; when a resync scan hits a byte pattern inside the
audio that looks like a frame marker it swallows part of the following *video* frame, which
produces constant "undecodable NALU" spam and multi-second tile freezes.

## Data flow: live from a recorder

```
NvrChannelTile ──┐
  DvripClient ◄──┤  (one TCP session per (recorder, channel))
    │
    ├─ login (sofia_hash)
    ├─ OPMonitor Claim 1413
    ├─ OPMonitor Start 1410
    └─ MONITOR_DATA 1412 ───► SofiaFrameParser ───► detect_codec()
                                  │                     │
                                  │                     ▼
                                  └────────────────► FFmpegPlayer (pipe)
                                                        │
                                                        ▼ rawvideo bgr24
                                                     QPainter → tile
```

## Data flow: archive

```
PlaybackView
  DvripClient ─── login ── OPFileQuery 1440 ───────► fileList(records)
                                                            │
                                       calendar + timeline ─┘
                                                            │
                                            user picks  ────┤
                                                            ▼
                  OPPlayBack Claim 1424 ── Start 1420 ── DATA 1422
                                                            │
                       SofiaFrameParser ────► FFmpegPlayer (pipe)
                                          └──► MP4Exporter (-c copy)
```

`OPFileQuery` silently caps every reply at **64 records**, sorted oldest first, so `query_files`
pages with a last-record cursor until the server returns an empty page or repeats itself.
A naive single query looks like "the recorder only kept three days".

`MP4Exporter` runs a second ffmpeg with `-c copy -movflags +faststart`: the stream is remuxed
without re-encoding. When playback ends (4 s with no new bytes) stdin closes, ffmpeg finalises the
`moov` atom, and the file is ready.

## Recorder sessions

Xiongmai firmware has a **low concurrent-session limit** and releases stale sessions slowly. The
architecture minimises session pressure:

- **One persistent control session per recorder** (`MainWindow._nvr_control`) — does discovery
  once at login and is then reused for the periodic record-status poll. **No open/close per
  tick**, which would starve the live sessions.
- **One session per live channel.** Live data is not tagged with a channel, so one session equals
  one channel.
- **The archive gets its own session** in `PlaybackView`, alive while the window is open.

Steady state for a 4-camera recorder: 1 control + 4 live + up to 2 (archive + export) = 7 sessions
at peak. `_open_archive` warns once the total reaches 6.

Stream quality switching is **make-before-break**: a second session and a hidden decoder warm up
while the current picture keeps playing, and the swap happens on the warm decoder's first frame.
Because that doubles a tile's sessions during warm-up, `GridView` serialises switching — exactly
one switch is in flight at a time.

## The frame return path is the binding constraint

Decoding happens inside ffmpeg, but decoded frames travel back into the GUI thread as raw BGR,
and that thread has to memcpy and repaint each one. At a 1920 px cap that is 6.2 MB per frame per
tile; four tiles saturate the main thread, ffmpeg blocks writing stdout, stops draining its stdin,
and the 2 MB backpressure guard restarts the decoder. Measured on a 12-core desktop: 1920 caused
backpressure drops and decoder restarts within 90 seconds, while 1440 and below ran clean. Hence
the output-width caps in `ui/nvr_channel_widget.py` — the limit is *frame transport*, not decode.

## Where state lives

- **On disk:** `cameras.json` (RTSP), `nvrs.json` (recorders + channels), both **without
  passwords**; paths via `platformdirs`.
- **Keyring:** passwords (`service="beleye"`, `username=id` for RTSP, `"nvr:<id>"` for recorders).
- **QSettings:** window geometry, theme, UI preferences.
- **In memory:** `GridView._tiles: dict[id, CameraTile | NvrChannelTile]`,
  `MainWindow._nvr_control: dict[nvr_id, DvripClient]`.

## FFmpeg integration

In **RTSP mode** `FFmpegPlayer` spawns `ffmpeg -i <url>` with the configured `-rtsp_transport`
plus `-fflags nobuffer`, parses the frame size out of stderr, reads `rawvideo bgr24` from stdout
and paints it with `QImage` + `QPainter`.

In **pipe mode** (recorder live and archive) it runs `ffmpeg -f hevc|h264 -i pipe:0` and data is
written to stdin via `QProcess.write()`. Decoding lands in the same rawvideo pipeline, so the UI
side is identical.

In both cases the ffmpeg process is attached to the Qt event loop through `QProcess` — no QThread,
no Python threads, nothing that blocks. This is what keeps the UI responsive when one camera is
broken and the others are not.

Note that stderr must be reassembled across reads: a read can end mid-line, and handing the
parser a fragment hides the `Output #0` marker or the dimensions line.

## Theming

`ui/theme.py` holds three token tables: `DARK` and `LIGHT` for the chrome, and `VIDEO` for
surfaces that show camera footage. The video tokens are **identical in both themes on purpose** —
a light backdrop washes out the OSD overlays painted on top of the frame. `styles.qss.tmpl` is
rendered with the active table and installed on the `QApplication`, which is what makes it reach
the archive window and all dialogs.

Icons are painted at runtime rather than shipped as assets, and must be rebuilt on a theme change:
a `QIcon` bakes its colour into a pixmap and cannot re-tint itself.

## Extension ideas

- **ONVIF discovery** — `onvif-zeep`, scan the local network; the single largest win for
  compatibility
- **Motion detection** — a second ffmpeg with `-vf select='gt(scene,0.1)'`, or OpenCV
- **Multi-monitor** — `QScreen` API with one `MainWindow` per screen
- **Local recording from live** — a parallel `MP4Exporter` attached to a live tile
