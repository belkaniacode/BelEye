from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from app.logging_setup import setup_logging
from video.ffmpeg_player import find_ffmpeg, find_ffprobe

APP_ICON_DIR = Path(__file__).resolve().parent / "resources" / "icons"
APP_ICON_PATH = APP_ICON_DIR / "beleye.svg"


def _load_app_icon() -> QIcon | None:
    """
    Build a QIcon containing the SVG source and every pre-rasterized PNG
    variant. Adding raster sizes alongside the SVG gives the best result on
    Linux WMs whose Wayland/X11 protocol exchanges only fixed-size pixmaps
    (KDE Plasma taskbars, GNOME shell, etc.).
    """
    if not APP_ICON_PATH.exists():
        return None
    icon = QIcon(str(APP_ICON_PATH))
    for size in (16, 24, 32, 48, 64, 96, 128, 256, 512):
        png = APP_ICON_DIR / f"beleye-{size}.png"
        if png.exists():
            icon.addFile(str(png))
    return icon

log = logging.getLogger(__name__)


def _apply_theme(app: QApplication) -> None:
    try:
        import qdarktheme  # type: ignore

        qdarktheme.setup_theme("dark")
    except Exception as exc:
        log.warning("qdarktheme unavailable (%s); falling back to Fusion+dark palette.", exc)
        from PySide6.QtGui import QColor, QPalette
        from PySide6.QtCore import Qt

        app.setStyle("Fusion")
        pal = QPalette()
        pal.setColor(QPalette.Window, QColor("#0f1115"))
        pal.setColor(QPalette.WindowText, Qt.white)
        pal.setColor(QPalette.Base, QColor("#15181d"))
        pal.setColor(QPalette.AlternateBase, QColor("#1c2026"))
        pal.setColor(QPalette.Text, Qt.white)
        pal.setColor(QPalette.Button, QColor("#1c2026"))
        pal.setColor(QPalette.ButtonText, Qt.white)
        pal.setColor(QPalette.Highlight, QColor("#3b82f6"))
        pal.setColor(QPalette.HighlightedText, Qt.white)
        app.setPalette(pal)


def main() -> int:
    setup_logging()
    log.info("Starting BelEye")

    app = QApplication(sys.argv)
    app.setApplicationName("BelEye")
    app.setApplicationDisplayName("BelEye")
    app.setOrganizationName("BelEye")
    app.setDesktopFileName("beleye")  # ties window to .desktop entry on Linux

    icon = _load_app_icon()
    if icon is not None:
        app.setWindowIcon(icon)
        log.info("App icon loaded from %s (+ PNG sizes)", APP_ICON_PATH)
    else:
        log.warning("App icon not found at %s", APP_ICON_PATH)

    _apply_theme(app)

    if not find_ffmpeg() or not find_ffprobe():
        QMessageBox.critical(
            None,
            "Зависимость отсутствует",
            "Не найдены `ffmpeg` и/или `ffprobe`.\n\n"
            "Установите FFmpeg:\n"
            "  • Arch:    sudo pacman -S ffmpeg\n"
            "  • Debian:  sudo apt install ffmpeg\n"
            "  • macOS:   brew install ffmpeg\n"
            "  • Windows: https://www.gyan.dev/ffmpeg/builds/\n",
        )
        return 1

    from ui.main_window import MainWindow

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
