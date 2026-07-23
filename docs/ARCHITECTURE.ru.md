# Архитектура BelEye

[English version](ARCHITECTURE.md)

Слоистое приложение PySide6. Сейчас четыре слоя:

- `app/` — конфигурация (RTSP-камеры + NVR), keyring, пути, логирование.
  **Не импортирует Qt.**
- `dvrip/` — клиент протокола Sofia/DVRIP (порт 34567): login, OPMonitor,
  OPPlayBack, OPFileQuery, парсер Sofia-кадров, авто-детект кодека
- `video/` — обёртки над процессами FFmpeg (декод в pipe-режиме, экспорт).
  Листовой модуль: не должен импортировать из `ui/`.
- `ui/` — Qt-виджеты: сетка, плитки, диалоги, окно архива, темы

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
│   ├── theme.py                  # семантические цветовые токены, светлая/тёмная, автоопределение
│   ├── prefs.py                  # постоянные настройки интерфейса (QSettings)
│   ├── icon_util.py              # рисованные иконки — без шрифтов и файлов ресурсов
│   ├── camera_widget.py          # CameraTile (RTSP) + DraggableTileMixin + REC badge
│   ├── nvr_channel_widget.py     # NvrChannelTile = DvripClient + pipe FFmpegPlayer
│   ├── grid_view.py              # QStackedLayout: GRID / SINGLE / empty + diff-update
│   ├── camera_form.py            # CameraForm — RTSP camera CRUD
│   ├── nvr_form.py               # NvrForm — добавление NVR + Test connection
│   ├── settings_dialog.py        # SettingsDialog — список камер + NVR
│   ├── playback_view.py          # Окно архива: calendar + timeline + player + export
│   └── main_window.py            # MainWindow + persistent NVR control client + REC poll
└── resources/
    └── styles.qss.tmpl           # шаблон QSS, рендерится под тему в ui/theme.py
```

## DVRIP: как это работает на железе

Все опкоды и форматы кадров **проверены прямым опросом регистратора**
(Xiongmai NBD80S16S-KL, прошивка V4.03) и зафиксированы в `codes.py`:

| Поток | Claim REQ | Claim RSP | Start REQ | Data |
|-------|----------:|----------:|----------:|-----:|
| Живой просмотр (`OPMonitor`) | 1413 | 1414 (Ret=100) | 1410 | 1412 |
| Архив (`OPPlayBack`) | 1424 | 1425 (Ret=100) | 1420 | **1422** |

Две ловушки, о которых стоит знать до правок:

- Порядок claim/start для живого потока — **1413, затем 1410**, а не
  «очевидный» 1410 → 1413. При очевидном порядке claim отклоняется с `Ret=103`.
- Данные архива приходят на **1422**, хотя широко цитируемая эталонная
  реализация описывает 1426. На этой прошивке на 1426 не приходит ничего.

Внутри пакетов 1412 — поток **Sofia-кадров** с маркерами
`00 00 01 FC/FD/FA/FB` (I-frame / P-frame / audio / info). Парсер
снимает обёртку → чистый H.264/HEVC Annex-B, передаётся в `FFmpegPlayer`
pipe-режима (`-f hevc|h264 -i pipe:0`). Кодек автодетектится по первому
parameter-set или IDR NAL.

Заголовок аудиокадра — 8 байт, длина **u16** по смещению 6, а не 16-байтная
раскладка как у видео. Ошибка здесь заставляет парсер читать сэмплы G.711 как
длину в ~1,6 млрд байт и ресинхронизироваться на каждом аудиокадре; а когда
скан ресинхронизации натыкается внутри аудио на байты, похожие на маркер, он
съедает часть следующего *видео*кадра — отсюда постоянный поток «undecodable
NALU» и многосекундные зависания плиток.

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
                  OPPlayBack Claim 1424 ── Start 1420 ── DATA 1422
                                                            │
                       SofiaFrameParser ────► FFmpegPlayer (pipe)
                                          └──► MP4Exporter (-c copy)
```

`OPFileQuery` молча обрезает каждый ответ на **64 записях**, отсортированных
от старых к новым, поэтому `query_files` листает курсором по последней записи,
пока сервер не вернёт пустую страницу или не начнёт повторяться. Наивный
одиночный запрос выглядит как «регистратор хранит всего три дня».

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
2 (архив + экспорт) = 7 сессий пиково. `_open_archive` предупреждает, когда
общее число доходит до 6.

Смена качества потока идёт по схеме **make-before-break**: вторая сессия и
скрытый декодер прогреваются, пока текущая картинка продолжает идти, а замена
происходит на первом кадре прогретого декодера. Поскольку на время прогрева
число сессий плитки удваивается, `GridView` сериализует переключения — в любой
момент выполняется ровно одно.

## Узкое место — возврат кадров, а не декодирование

Декодирует ffmpeg, но декодированные кадры возвращаются в GUI-поток сырым BGR,
и этот поток обязан скопировать и перерисовать каждый. При пределе 1920 px это
6,2 МБ на кадр с плитки; четыре плитки насыщают главный поток, ffmpeg упирается
в запись stdout, перестаёт читать stdin, и защита по backpressure на 2 МБ
перезапускает декодер. Замерено на 12-ядерном десктопе: 1920 давал сбои и
перезапуски за 90 секунд, а 1440 и ниже работали чисто. Отсюда пределы ширины
вывода в `ui/nvr_channel_widget.py` — ограничивает *передача кадров*, а не
декодирование.

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
- **QSettings:** геометрия окна, тема, настройки интерфейса.
- **В памяти:** `GridView._tiles: dict[id, CameraTile | NvrChannelTile]`,
  `MainWindow._nvr_control: dict[nvr_id, DvripClient]`.

## FFmpeg integration

В RTSP-режиме `FFmpegPlayer` спавнит `ffmpeg -i <url>` с выбранным
`-rtsp_transport` и `-fflags nobuffer`, парсит размер кадра из stderr,
читает `rawvideo bgr24` со stdout и рисует через `QImage` + `QPainter`.

В **pipe-режиме** (NVR live и archive): `ffmpeg -f hevc|h264 -i pipe:0`,
данные пишутся в stdin через `QProcess.write()`. Декодирование
заворачивается в тот же `rawvideo` пайплайн — UI работает одинаково.

В обоих случаях процесс ffmpeg прикреплён к Qt event loop через
`QProcess` (никаких QThread / threading), что критично для отзывчивости
UI под нагрузкой множества каналов.

Учтите: stderr нужно собирать через границы чтений — чтение может оборваться
посреди строки, и обрывок скрывает от парсера маркер `Output #0` или строку с
разрешением.

## Темизация

В `ui/theme.py` три таблицы токенов: `DARK` и `LIGHT` для интерфейса и `VIDEO`
для поверхностей с видео. Видео-токены **намеренно одинаковы в обеих темах** —
светлый фон размывает OSD-надписи, нарисованные поверх кадра. `styles.qss.tmpl`
рендерится с активной таблицей и ставится на `QApplication` — именно поэтому
стили доходят до окна архива и всех диалогов.

Иконки рисуются в рантайме, а не лежат файлами, и должны пересобираться при
смене темы: `QIcon` запекает цвет в пиксмап и не может перекраситься сам.

## Расширение

- **ONVIF discovery:** библиотека `onvif-zeep`, скан локальной сети — самое
  большое приобретение для совместимости
- **Motion detection:** второй ffmpeg с `-vf select='gt(scene,0.1)'` или OpenCV
- **Multi-monitor:** `QScreen` API + отдельные `MainWindow` на каждый экран
- **Локальная запись c live:** параллельный `MP4Exporter` на live-плитку
