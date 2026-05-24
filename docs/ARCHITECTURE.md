# Архитектура BelEye

Простое слоистое приложение PySide6. Три слоя: `app/` (логика и хранилище), `video/` (внешний процесс ffplay), `ui/` (Qt-виджеты). `main.py` — точка входа, склеивает слои.

## Структура

```
beleye/
├── main.py                       # entry: setup logging → theme → ffplay-check → MainWindow
├── app/
│   ├── paths.py                  # platformdirs-обёртка (config/log пути)
│   ├── logging_setup.py          # консоль + RotatingFileHandler
│   ├── config.py                 # CameraConfig dataclass, JSON I/O (атомарная запись)
│   ├── secrets.py                # keyring (service="beleye", username=camera.id)
│   └── rtsp.py                   # build_rtsp_url() — URL-encoding credentials
├── video/
│   ├── ffmpeg_player.py          # QWidget + subprocess ffplay через SDL_WINDOWID
│   └── stream_monitor.py         # probe_rtsp() через ffprobe (для "Test connection")
├── ui/
│   ├── camera_widget.py          # CameraTile = FFmpegPlayer + overlay (имя, status dot)
│   ├── grid_view.py              # QStackedLayout: GRID (auto-cols) / SINGLE / empty
│   ├── camera_form.py            # CameraForm — добавление/редактирование камеры
│   ├── settings_dialog.py        # SettingsDialog — список камер + CRUD
│   └── main_window.py            # MainWindow — toolbar, центральный GridView, статус-бар
└── resources/
    └── styles.qss                # QSS поверх qdarktheme
```

## Поток данных

1. `MainWindow._reload_cameras()` → `config.load_cameras()` (читает JSON)
2. `GridView.set_cameras(cameras)` создаёт `CameraTile` на каждую камеру
3. `CameraTile.__init__` → `secrets.get_password(id)` → `rtsp.build_rtsp_url()` → `FFmpegPlayer(url)`
4. `FFmpegPlayer.start()` спавнит `ffplay` с `SDL_WINDOWID=winId()` — видео встраивается в виджет
5. `QTimer` watchdog опрашивает процесс; при падении — exponential backoff reconnect

## Где жить состоянию

- **На диске:** `cameras.json` (без паролей)
- **Keyring:** пароли (service="beleye", username=camera.id)
- **В памяти:** активные `CameraTile` в `GridView._tiles: dict[id, CameraTile]`

`SettingsDialog` редактирует свою копию списка, после `OK` `MainWindow._reload_cameras()` пересоздаёт сетку — простой ре-render вместо diff-логики.

## FFmpeg embed: как это работает

`ffplay` собран с SDL2. SDL умеет рисовать в существующее окно через `SDL_WINDOWID` (env var). Мы передаём `int(self.winId())` Qt-виджета — ffplay рисует прямо туда.

- **Linux (X11):** работает из коробки
- **Linux (Wayland):** требует XWayland — запуск с `QT_QPA_PLATFORM=xcb`
- **Windows:** работает (SDL поддерживает HWND через тот же env var)
- **macOS:** SDL_WINDOWID не поддерживается — нужен fallback (decode-via-ffmpeg + QOpenGLWidget). Не реализован в v1.

## Расширение

- **Запись:** добавить параллельный `ffmpeg -c copy -f segment` процесс на камеру
- **ONVIF discovery:** библиотека `onvif-zeep`, scan локальной сети, авто-заполнение формы
- **Motion detection:** второй ffmpeg-процесс с `-vf select='gt(scene,0.1)'` или OpenCV
- **Multi-monitor:** `QScreen` API + отдельные `MainWindow` на каждый экран
