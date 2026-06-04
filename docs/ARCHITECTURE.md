# Архитектура BelEye

Слоистое приложение PySide6. Сейчас четыре слоя:

- `app/` — конфигурация (RTSP-камеры + NVR), keyring, пути, логирование
- `dvrip/` — клиент протокола Sofia/DVRIP (порт 34567): login, OPMonitor,
  OPPlayBack, OPFileQuery, парсер Sofia-кадров, авто-детект кодека
- `video/` — обёртки над FFmpeg subprocess (декод в pipe-режиме, экспорт)
- `ui/` — Qt-виджеты: сетка, плитки, диалоги, окно архива

`main.py` — точка входа, склеивает слои.

## Структура

```
beleye/
├── main.py                       # entry: logging → theme → ffmpeg-check → MainWindow
├── app/
│   ├── paths.py                  # platformdirs (cameras.json, nvrs.json, log)
│   ├── logging_setup.py          # консоль + RotatingFileHandler
│   ├── config.py                 # CameraConfig — одиночные RTSP-камеры
│   ├── nvr_config.py             # NvrConfig + NvrChannel — регистраторы
│   ├── secrets.py                # keyring (service="beleye", username=id или "nvr:<id>")
│   └── rtsp.py                   # build_rtsp_url() с URL-escape
├── dvrip/
│   ├── codes.py                  # MsgId enum (hardware-verified opcodes)
│   ├── packet.py                 # 20-byte Sofia header pack/unpack
│   ├── auth.py                   # sofia_hash() — Xiongmai password hash
│   ├── client.py                 # DvripClient(QObject) — QTcpSocket, сигналы Qt
│   └── sofia_frame.py            # Парсер Sofia-кадров (FC/FD/FA/FB) + detect_codec()
├── video/
│   ├── ffmpeg_player.py          # FFmpegPlayer(QWidget): RTSP режим или pipe (H.264/HEVC)
│   ├── export.py                 # MP4Exporter — remux Sofia-stripped stream в mp4
│   └── stream_monitor.py         # probe_rtsp() для "Test connection"
├── ui/
│   ├── camera_widget.py          # CameraTile (RTSP) + DraggableTileMixin + REC badge
│   ├── nvr_channel_widget.py     # NvrChannelTile = DvripClient + pipe FFmpegPlayer
│   ├── grid_view.py              # QStackedLayout: GRID / SINGLE / empty + diff-update
│   ├── camera_form.py            # CameraForm — RTSP camera CRUD
│   ├── nvr_form.py               # NvrForm — добавление NVR + Test connection
│   ├── settings_dialog.py        # SettingsDialog — список камер + NVR
│   ├── playback_view.py          # Окно архива: calendar + timeline + player + export
│   └── main_window.py            # MainWindow + persistent NVR control client + REC poll
└── resources/
    └── styles.qss
```

## DVRIP: как это работает на железе

Все опкоды и форматы кадров **проверены прямым опросом регистратора**
(Xiongmai NBD80S16S-KL, прошивка V4.03) и зафиксированы в `codes.py`:

| Поток | Claim REQ | Claim RSP | Start REQ | Data |
|-------|----------:|----------:|----------:|-----:|
| Live monitor | 1413 | 1414 (Ret=100) | 1410 | 1412 |
| Archive playback | 1413 | 1414 (Ret=100) | 1410 | 1412 |

Live и playback используют **одинаковый низкоуровневый транспорт** —
отличается только тело JSON (`OPMonitor` vs `OPPlayBack` с `FileName`).

Внутри пакетов 1412 — поток **Sofia-кадров** с маркерами
`00 00 01 FC/FD/FA/FB` (I-frame / P-frame / audio / info). Парсер
снимает обёртку → чистый H.264/HEVC Annex-B, передаётся в `FFmpegPlayer`
pipe-режима (`-f hevc|h264 -i pipe:0`). Кодек автодетектится по
первому parameter-set NAL (VPS=hevc, SPS=h264).

## Поток данных: NVR live

```
NvrChannelTile ──┐
  DvripClient ◄──┤  (one TCP session per (NVR, channel))
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

## Поток данных: NVR архив

```
PlaybackView
  DvripClient ─── login ── OPFileQuery 1440 ───────► fileList(records)
                                                            │
                                       calendar + timeline ─┘
                                                            │
                                            user picks  ────┤
                                                            ▼
                  OPPlayBack Claim 1413 ── Start 1410 ── MONITOR_DATA 1412
                                                            │
                       SofiaFrameParser ────► FFmpegPlayer (pipe)
                                          └──► MP4Exporter (-c copy)
```

`MP4Exporter` запускает второй ffmpeg с `-c copy -movflags +faststart`:
поток из NVR remux-ится в mp4 без перекодирования. По окончании
playback'а (тайм-аут 4 c без новых байт) stdin закрывается → ffmpeg
финализирует `moov` атом → файл готов.

## Сессии NVR

Xiongmai-прошивки имеют **низкий лимит одновременных сессий** и медленно
освобождают подвисшие. Архитектура минимизирует session pressure:

- **Одна постоянная control-сессия на NVR** (`MainWindow._nvr_control`)
  — делает discovery один раз на логине и далее переиспользуется для
  периодического опроса статуса записи. **Никакого open/close на каждый
  тик** — иначе live-сессии задушатся.
- **Одна сессия на live-канал.** Live-данные не помечены каналом, так
  что одна сессия = один канал.
- **Архив — своя отдельная сессия** в `PlaybackView`, живёт пока окно
  открыто.

Установившееся состояние для 4-камерного NVR: 1 control + 4 live + до
2 (архив + export) = 7 сессий пиково. С churn ≤ 1 за минуту это
безопасно даже на дешёвых прошивках.

## REC-индикатор

`DvripClient.query_record_status()` → `CONFIG_GET 1042 Name="Record"` →
`recordStatus(dict[ch -> bool])`. Канал считается «записывает», если
первое слово маски расписания записи ненулевое. Сигнал поднимается до
`_Overlay.set_recording(True)` → красная заполненная «таблетка»
**● REC** в левом верхнем углу плитки.

## Где жить состоянию

- **На диске:** `cameras.json` (RTSP), `nvrs.json` (NVR + channels), оба
  без паролей; пути через `platformdirs`.
- **Keyring:** пароли (`service="beleye"`, `username=id` для RTSP, `"nvr:<id>"`
  для NVR).
- **В памяти:** `GridView._tiles: dict[id, CameraTile | NvrChannelTile]`,
  `MainWindow._nvr_control: dict[nvr_id, DvripClient]`.

## FFmpeg integration

В RTSP-режиме `FFmpegPlayer` спавнит `ffmpeg -i <url>` с
`-rtsp_transport tcp -fflags nobuffer`, парсит размер кадра из stderr,
читает `rawvideo bgr24` со stdout и рисует через `QImage` + `QPainter`.

В **pipe-режиме** (NVR live и archive): `ffmpeg -f hevc|h264 -i pipe:0`,
данные пишутся в stdin через `QProcess.write()`. Декодирование
заворачивается в тот же `rawvideo` пайплайн — UI работает одинаково.

В обоих случаях процесс ffmpeg прикреплён к Qt event loop через
`QProcess` (никаких QThread / threading), что критично для отзывчивости
UI под нагрузкой множества каналов.

## Расширение

- **Motion detection:** второй ffmpeg с `-vf select='gt(scene,0.1)'` или OpenCV
- **ONVIF discovery:** библиотека `onvif-zeep`, scan локальной сети
- **Multi-monitor:** `QScreen` API + отдельные `MainWindow` на каждый экран
- **Локальная запись c live:** параллельный `MP4Exporter` на live-плитку
