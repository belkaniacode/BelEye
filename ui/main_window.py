from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from app import config as cfg
from app import nvr_config as nvrcfg
from app import secrets as keystore
from app.nvr_config import nvr_keyring_user
from .grid_view import GridView
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)


# Lucide-style icons embedded as SVG strings. 24x24 viewBox, stroke-based,
# 2 px stroke, round line caps/joins. Rendered via QSvgRenderer into a
# QPixmap so they stay crisp at any size and look identical across platforms.
_ICON_SVGS: dict[str, str] = {
    "settings": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"/>'
        '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9 1.65 1.65 0 0 0 4.27 7.18l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/>'
        '</svg>'
    ),
    "refresh": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<polyline points="23 4 23 10 17 10"/>'
        '<path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>'
        '</svg>'
    ),
    "grid": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<rect x="3"  y="3"  width="7" height="7" rx="1"/>'
        '<rect x="14" y="3"  width="7" height="7" rx="1"/>'
        '<rect x="3"  y="14" width="7" height="7" rx="1"/>'
        '<rect x="14" y="14" width="7" height="7" rx="1"/>'
        '</svg>'
    ),
    "reorder": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M7 3v18"/>'
        '<polyline points="3 7 7 3 11 7"/>'
        '<path d="M17 21V3"/>'
        '<polyline points="21 17 17 21 13 17"/>'
        '</svg>'
    ),
    "fullscreen": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M8 3H5a2 2 0 0 0-2 2v3"/>'
        '<path d="M21 8V5a2 2 0 0 0-2-2h-3"/>'
        '<path d="M3 16v3a2 2 0 0 0 2 2h3"/>'
        '<path d="M16 21h3a2 2 0 0 0 2-2v-3"/>'
        '</svg>'
    ),
}


def _make_icon(name: str, size: int = 14, color: str = "#cbd5e1", right_pad: int = 8) -> QIcon:
    """
    Lucide SVG → QIcon. ``right_pad`` adds transparent space to the right
    of the icon, baked into the pixmap. This is how we get a real gap
    between the icon and the toolbar label — Qt's ToolButtonTextBesideIcon
    does not expose a configurable icon-text spacing.
    """
    svg_template = _ICON_SVGS.get(name)
    if svg_template is None:
        return QIcon()
    svg = svg_template.format(color=color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    scale = 2
    total_w = (size + right_pad) * scale
    total_h = size * scale
    pm = QPixmap(total_w, total_h)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    # Render the SVG into the left "size × size" square; the right side
    # stays transparent, becoming the icon-text gap.
    from PySide6.QtCore import QRectF
    renderer.render(p, QRectF(0, 0, size * scale, size * scale))
    p.end()
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("BelEye — Видеонаблюдение")
        self.resize(1280, 760)

        # Window icon — many Wayland compositors honor the per-window icon
        # separately from the application-wide QApplication icon.
        from pathlib import Path
        icon_path = Path(__file__).resolve().parent.parent / "resources" / "icons" / "beleye.svg"
        if icon_path.exists():
            from PySide6.QtGui import QIcon
            self.setWindowIcon(QIcon(str(icon_path)))

        self._nvr_refreshers: dict = {}

        self.grid = GridView(self)
        self.grid.editRequested.connect(self._on_edit_camera)
        self.grid.removeRequested.connect(self._on_remove_camera)
        self.grid.orderChanged.connect(self._on_order_changed)

        # Central widget hosts an optional reorder banner above the grid
        central = QWidget(self)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)

        self.reorder_banner = self._build_reorder_banner()
        self.reorder_banner.hide()

        central_layout.addWidget(self.reorder_banner)
        central_layout.addWidget(self.grid, 1)
        self.setCentralWidget(central)

        self._build_toolbar()
        self.setStatusBar(QStatusBar())

        self._load_qss()
        self._reload_cameras()

    # Toolbar / banner --------------------------------------------------

    def _build_toolbar(self) -> None:
        tb = QToolBar("Главная панель", self)
        tb.setMovable(False)
        # iconSize matches the padded pixmap (14 icon + 8 right transparent padding).
        tb.setIconSize(QSize(14 + 8, 14))
        self.addToolBar(tb)

        # Toolbar: text-only labels with consistent icons drawn as QIcon
        # from a tiny pixmap. Mixing Unicode emoji (⚙, ⟳, …) with text caused
        # vertical-alignment drift because some glyphs rendered as colored
        # emoji and others as plain font glyphs, each with its own baseline.
        # Using QIcon ensures Qt centers the icon and text uniformly.

        # Icon-text gap is baked into the icon pixmap (transparent right pad)
        # so the labels can use plain text without leading spaces.
        self.act_settings = QAction(_make_icon("settings"), "Настройки", self)
        self.act_settings.triggered.connect(self._open_settings)
        tb.addAction(self.act_settings)

        self.act_refresh = QAction(_make_icon("refresh"), "Обновить", self)
        self.act_refresh.triggered.connect(self._reload_cameras)
        tb.addAction(self.act_refresh)

        tb.addSeparator()

        self.act_view = QAction(_make_icon("grid"), "Сетка / 1 камера", self)
        self.act_view.setShortcut(QKeySequence("Ctrl+G"))
        self.act_view.triggered.connect(self.grid.toggle_mode)
        tb.addAction(self.act_view)

        self.act_reorder = QAction(_make_icon("reorder"), "Перетащить", self)
        self.act_reorder.setToolTip("Включить режим перетаскивания камер")
        self.act_reorder.triggered.connect(self._enter_reorder_mode)
        tb.addAction(self.act_reorder)

        self.act_fullscreen = QAction(_make_icon("fullscreen"), "Полный экран", self)
        self.act_fullscreen.setShortcut(QKeySequence("F11"))
        self.act_fullscreen.triggered.connect(self._toggle_fullscreen)
        tb.addAction(self.act_fullscreen)

        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)

    def _build_reorder_banner(self) -> QFrame:
        banner = QFrame()
        banner.setObjectName("ReorderBanner")
        banner.setFixedHeight(56)
        banner.setStyleSheet(
            "#ReorderBanner {"
            "  background: #13161c;"
            "  border-bottom: 1px solid #1f242b;"
            "}"
        )

        # Left accent strip — subtle, single color, low chroma
        accent = QFrame()
        accent.setFixedWidth(3)
        accent.setStyleSheet("background: #3b82f6;")

        # Icon glyph (Unicode arrows) in a muted bubble
        icon = QLabel("⇅")
        icon.setFixedSize(32, 32)
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(
            "QLabel {"
            "  background: rgba(59, 130, 246, 0.12);"
            "  color: #60a5fa;"
            "  border-radius: 8px;"
            "  font-size: 16px;"
            "  font-weight: 600;"
            "}"
        )

        title = QLabel("Режим перетаскивания")
        title.setStyleSheet(
            "color: #e5e7eb; font-size: 13px; font-weight: 600; letter-spacing: 0.1px;"
        )
        subtitle = QLabel("Перетащите одну камеру на другую, чтобы поменять их местами")
        subtitle.setStyleSheet("color: #94a3b8; font-size: 12px;")

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        text_col.addWidget(title)
        text_col.addWidget(subtitle)

        # Buttons: primary (filled accent) + ghost (transparent with border)
        self.btn_apply = QPushButton("Применить")
        self.btn_apply.setCursor(Qt.PointingHandCursor)
        self.btn_apply.setStyleSheet(
            "QPushButton {"
            "  background: #3b82f6; color: #ffffff; border: 1px solid #3b82f6;"
            "  padding: 7px 16px; border-radius: 7px;"
            "  font-size: 12px; font-weight: 600;"
            "}"
            "QPushButton:hover { background: #2563eb; border-color: #2563eb; }"
            "QPushButton:pressed { background: #1d4ed8; border-color: #1d4ed8; }"
        )
        self.btn_apply.clicked.connect(self._apply_reorder)

        self.btn_cancel = QPushButton("Отменить")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.setStyleSheet(
            "QPushButton {"
            "  background: transparent; color: #cbd5e1; border: 1px solid #2a3038;"
            "  padding: 7px 16px; border-radius: 7px;"
            "  font-size: 12px; font-weight: 500;"
            "}"
            "QPushButton:hover { background: #1f242b; color: #ffffff; border-color: #2f3640; }"
            "QPushButton:pressed { background: #15181d; }"
        )
        self.btn_cancel.clicked.connect(self._cancel_reorder)

        layout = QHBoxLayout(banner)
        layout.setContentsMargins(0, 0, 20, 0)
        layout.setSpacing(0)
        layout.addWidget(accent)
        # Padding after accent
        inner = QHBoxLayout()
        inner.setContentsMargins(20, 10, 0, 10)
        inner.setSpacing(14)
        inner.addWidget(icon)
        inner.addLayout(text_col, 1)
        inner.addSpacing(12)
        inner.addWidget(self.btn_cancel)
        inner.addSpacing(8)
        inner.addWidget(self.btn_apply)
        layout.addLayout(inner, 1)
        return banner

    def _load_qss(self) -> None:
        qss_path = Path(__file__).resolve().parent.parent / "resources" / "styles.qss"
        if qss_path.exists():
            try:
                self.setStyleSheet(qss_path.read_text(encoding="utf-8"))
            except OSError as exc:
                log.warning("Failed to load QSS: %s", exc)

    # Camera ops --------------------------------------------------------

    def _reload_cameras(self) -> None:
        cameras = cfg.load_cameras()
        self.grid.set_cameras(cameras)
        self._rebuild_nvr_tiles()
        # Reconcile saved channels with the device's actual active channels
        # in the background, so the grid self-heals when cameras are added or
        # removed on the NVR without the user re-probing.
        for nvr in nvrcfg.load_nvrs():
            self._refresh_nvr_channels(nvr)

    def _rebuild_nvr_tiles(self) -> None:
        cameras = cfg.load_cameras()
        nvrs = nvrcfg.load_nvrs()
        items = []
        total = 0
        for nvr in nvrs:
            password = keystore.get_password(nvr_keyring_user(nvr.id))
            for ch in nvr.channels:
                if ch.enabled:
                    items.append((nvr, ch, password))
                    total += 1
        self.grid.set_nvr_channels(items)
        if nvrs:
            self.statusBar().showMessage(
                f"Загружено: камер {len(cameras)}, NVR {len(nvrs)} ({total} каналов)", 3000)
        else:
            self.statusBar().showMessage(f"Загружено камер: {len(cameras)}", 3000)

    def _refresh_nvr_channels(self, nvr) -> None:
        from dvrip.client import DvripClient

        password = keystore.get_password(nvr_keyring_user(nvr.id))
        client = DvripClient(self, auto_discover=True)
        self._nvr_refreshers[nvr.id] = client

        def on_discovered(channels: list) -> None:
            active_map = {int(c.number): str(c.name) for c in channels}
            if not active_map:
                self._dispose_refresher(nvr.id)
                return
            # Preserve the user's existing channel order; update names, append
            # newly-discovered channels, drop channels no longer present.
            merged = [
                nvrcfg.NvrChannel(number=c.number, name=active_map[c.number], enabled=True)
                for c in nvr.channels
                if c.number in active_map
            ]
            existing_nums = {c.number for c in merged}
            for num in sorted(active_map):
                if num not in existing_nums:
                    merged.append(nvrcfg.NvrChannel(number=num, name=active_map[num], enabled=True))

            before = [(c.number, c.name) for c in nvr.channels]
            after = [(c.number, c.name) for c in merged]
            if after != before:
                log.info("[NVR] %s channels changed %s -> %s", nvr.name, before, after)
                nvr.channels = merged
                nvrs = nvrcfg.load_nvrs()
                # update_nvr matches by id; mutate the loaded copy's channels
                for saved_nvr in nvrs:
                    if saved_nvr.id == nvr.id:
                        saved_nvr.channels = merged
                        break
                nvrcfg.save_nvrs(nvrs)
                self._rebuild_nvr_tiles()
            self._dispose_refresher(nvr.id)

        client.channelsDiscovered.connect(on_discovered)
        client.loginFailed.connect(lambda _r: self._dispose_refresher(nvr.id))
        client.error.connect(lambda _e: self._dispose_refresher(nvr.id))
        client.connect_to(nvr.host, nvr.port, nvr.username, password)

    def _dispose_refresher(self, nvr_id: str) -> None:
        client = self._nvr_refreshers.pop(nvr_id, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                log.exception("[NVR] refresher close failed")
            client.deleteLater()

    def _open_settings(self) -> None:
        if self.grid.is_reorder_mode():
            return  # don't open settings while reordering
        dlg = SettingsDialog(self)
        dlg.exec()
        if dlg.changed():
            self._reload_cameras()

    def _on_edit_camera(self, camera_id: str) -> None:
        from .camera_form import CameraForm
        from app import secrets as keystore

        cameras = cfg.load_cameras()
        cam = next((c for c in cameras if c.id == camera_id), None)
        if not cam:
            return
        pwd = keystore.get_password(cam.id)
        form = CameraForm(camera=cam, password=pwd, parent=self)
        if form.exec() == QDialog.Accepted:
            updated, new_pwd = form.result_data()
            cfg.update_camera(cameras, updated)
            keystore.set_password(updated.id, new_pwd)
            self._reload_cameras()

    def _on_remove_camera(self, camera_id: str) -> None:
        from app import secrets as keystore

        cameras = cfg.load_cameras()
        cfg.delete_camera(cameras, camera_id)
        keystore.delete_password(camera_id)
        self._reload_cameras()

    # Reorder mode ------------------------------------------------------

    def _enter_reorder_mode(self) -> None:
        if self.grid.is_reorder_mode():
            return
        log.info("[FIX] Entering reorder mode")
        self.grid.enter_reorder_mode()
        self.reorder_banner.show()
        self.act_reorder.setEnabled(False)
        self.act_settings.setEnabled(False)
        self.act_refresh.setEnabled(False)
        self.statusBar().showMessage(
            "Режим перетаскивания: перетащите камеру на другую, чтобы поменять местами"
        )

    def _apply_reorder(self) -> None:
        new_order = self.grid.apply_reorder()
        self.reorder_banner.hide()
        self.act_reorder.setEnabled(True)
        self.act_settings.setEnabled(True)
        self.act_refresh.setEnabled(True)
        log.info("[FIX] Apply reorder: %d cameras", len(new_order))
        self.statusBar().showMessage("Порядок камер сохранён", 3000)

    def _cancel_reorder(self) -> None:
        log.info("[FIX] Cancel reorder")
        self.grid.cancel_reorder()
        self.reorder_banner.hide()
        self.act_reorder.setEnabled(True)
        self.act_settings.setEnabled(True)
        self.act_refresh.setEnabled(True)
        self.statusBar().showMessage("Изменения порядка отменены", 3000)

    def _on_order_changed(self, new_order: list[str]) -> None:
        """Persist the new tile order. The list mixes RTSP camera ids and NVR
        tile ids ("nvr:<id>:ch<n>"); split and persist each to its own file."""
        # --- RTSP cameras ---
        cameras = cfg.load_cameras()
        by_id = {c.id: c for c in cameras}
        cam_order = [cid for cid in new_order if cid in by_id]
        if cam_order:
            reordered = [by_id[cid] for cid in cam_order]
            reordered += [c for c in cameras if c.id not in by_id or c.id not in cam_order]
            # de-dup while preserving order
            seen = set()
            uniq = []
            for c in reordered:
                if c.id not in seen:
                    seen.add(c.id)
                    uniq.append(c)
            cfg.save_cameras(uniq)

        # --- NVR channels: reorder each NVR's channel list ---
        nvrs = nvrcfg.load_nvrs()
        changed = False
        for nvr in nvrs:
            prefix = f"nvr:{nvr.id}:ch"
            wanted = [int(tid[len(prefix):]) for tid in new_order if tid.startswith(prefix)]
            if not wanted:
                continue
            by_num = {c.number: c for c in nvr.channels}
            new_channels = [by_num[num] for num in wanted if num in by_num]
            new_channels += [c for c in nvr.channels if c.number not in wanted]
            if [c.number for c in new_channels] != [c.number for c in nvr.channels]:
                nvr.channels = new_channels
                changed = True
        if changed:
            nvrcfg.save_nvrs(nvrs)

    # Misc --------------------------------------------------------------

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            if self.grid.is_reorder_mode():
                self._cancel_reorder()
                return
            if self.isFullScreen():
                self.showNormal()
                return
            self.grid.show_grid()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.grid.stop_all()
        super().closeEvent(event)
