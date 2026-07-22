from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
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
from .icon_util import svg_icon
from .prefs import prefs
from .settings_dialog import SettingsDialog
from .theme import theme

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("BelEye — Видеонаблюдение")
        # [FIX hygiene] Restore last window geometry; fall back to a sane
        # default on first run.
        from PySide6.QtCore import QSettings
        settings = QSettings("BelEye", "BelEye")
        geo = settings.value("main_window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1280, 760)

        # Window icon — many Wayland compositors honor the per-window icon
        # separately from the application-wide QApplication icon.
        from pathlib import Path
        icon_path = Path(__file__).resolve().parent.parent / "resources" / "icons" / "beleye.svg"
        if icon_path.exists():
            from PySide6.QtGui import QIcon
            self.setWindowIcon(QIcon(str(icon_path)))

        # One persistent control connection per NVR (id -> DvripClient).
        self._nvr_control: dict = {}

        # Periodic record-status poll — reuses the persistent control
        # connections (no per-tick reconnect, to respect the NVR session cap).
        self._record_timer = QTimer(self)
        self._record_timer.setInterval(30_000)
        self._record_timer.timeout.connect(self._poll_nvr_record_status)

        self._playback_windows: list = []

        self.grid = GridView(self)
        self.grid.archiveRequested.connect(self._open_archive)
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

        # Icons and the banner carry baked colors; refresh them on every flip.
        theme.changed.connect(self._on_theme_changed)

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
        self.act_settings = QAction(svg_icon("settings"), "Настройки", self)
        self.act_settings.triggered.connect(self._open_settings)
        tb.addAction(self.act_settings)

        self.act_refresh = QAction(svg_icon("refresh"), "Обновить", self)
        self.act_refresh.triggered.connect(self._reload_cameras)
        tb.addAction(self.act_refresh)

        tb.addSeparator()

        self.act_view = QAction(svg_icon("grid"), "Сетка / 1 камера", self)
        self.act_view.setShortcut(QKeySequence("Ctrl+G"))
        self.act_view.triggered.connect(self.grid.toggle_mode)
        tb.addAction(self.act_view)

        self.act_reorder = QAction(svg_icon("reorder"), "Перетащить", self)
        self.act_reorder.setToolTip("Включить режим перетаскивания камер")
        self.act_reorder.triggered.connect(self._enter_reorder_mode)
        tb.addAction(self.act_reorder)

        self.act_fullscreen = QAction(svg_icon("fullscreen"), "Полный экран", self)
        self.act_fullscreen.setShortcut(QKeySequence("F11"))
        self.act_fullscreen.triggered.connect(self._toggle_fullscreen)
        tb.addAction(self.act_fullscreen)

        # Expanding spacer pins everything after it to the right edge —
        # QToolBar has no "align right" of its own.
        spacer = QWidget(tb)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        spacer.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # Without this the universal "QWidget { background }" rule paints the
        # spacer in the window color, leaving a visible slab in the toolbar.
        spacer.setStyleSheet("background: transparent;")
        tb.addWidget(spacer)

        # Quality policy toggle, then the theme toggle — both far right.
        self.act_hq = QAction(self)
        self.act_hq.setCheckable(True)
        self.act_hq.setChecked(prefs.hq_all())
        self.act_hq.setToolTip(
            "Высокое качество на всех камерах.\n"
            "Раскрытие камеры перестаёт переподключаться — картинка уже в полном "
            "качестве. Нагружает регистратор и сеть: при 4+ каналах возможны подвисания."
        )
        self.act_hq.toggled.connect(prefs.set_hq_all)
        tb.addAction(self.act_hq)

        self.act_theme = QAction(self)
        self.act_theme.triggered.connect(theme.toggle)
        tb.addAction(self.act_theme)

        # Which toolbar actions carry a baked-pixmap icon, so _retint_toolbar
        # can rebuild them when the theme flips.
        self._toolbar_icons = [
            (self.act_settings, "settings"),
            (self.act_refresh, "refresh"),
            (self.act_view, "grid"),
            (self.act_reorder, "reorder"),
            (self.act_fullscreen, "fullscreen"),
        ]

        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        # Icon-only for the right-hand toggles: their labels would be the only
        # text on that side of the bar and read as clutter. Must come *after*
        # the toolbar-wide style, which otherwise overwrites per-button
        # settings.
        for action in (self.act_hq, self.act_theme):
            btn = tb.widgetForAction(action)
            if btn is not None:
                btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
                btn.setCursor(Qt.PointingHandCursor)

        self._retint_toolbar()

    def _retint_toolbar(self) -> None:
        """Rebuild every toolbar icon for the active theme.

        A QIcon holds a rasterized pixmap with the stroke color baked in; it
        cannot re-tint itself, so the icons must be recreated on each theme
        change or they stay light-grey on a white toolbar.
        """
        for action, name in getattr(self, "_toolbar_icons", ()):
            action.setIcon(svg_icon(name))
        # Checked state is drawn by the QToolButton:checked QSS rule; the
        # icon only needs re-tinting for the new theme.
        self.act_hq.setIcon(svg_icon("zap", right_pad=0))
        # The toggle advertises the theme you will GET, not the one you're in.
        going_light = theme.is_dark()
        self.act_theme.setIcon(svg_icon("sun" if going_light else "moon", right_pad=0))
        label = "Светлая тема" if going_light else "Тёмная тема"
        self.act_theme.setText(label)
        self.act_theme.setToolTip(f"Переключить на: {label}")

    def _build_reorder_banner(self) -> QFrame:
        banner = QFrame()
        banner.setObjectName("ReorderBanner")
        banner.setFixedHeight(56)

        # Left accent strip — subtle, single color, low chroma
        accent = QFrame()
        accent.setFixedWidth(3)

        # Icon glyph (Unicode arrows) in a muted bubble
        icon = QLabel("⇅")
        icon.setFixedSize(32, 32)
        icon.setAlignment(Qt.AlignCenter)

        title = QLabel("Режим перетаскивания")
        subtitle = QLabel("Перетащите одну камеру на другую, чтобы поменять их местами")

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        text_col.addWidget(title)
        text_col.addWidget(subtitle)

        # Buttons: primary (filled accent) + ghost. Both are plain QPushButtons
        # now — the global QSS styles them via the [primary] property, so they
        # follow the theme with no per-widget stylesheet.
        self.btn_apply = QPushButton("Применить")
        self.btn_apply.setProperty("primary", True)
        self.btn_apply.setCursor(Qt.PointingHandCursor)
        self.btn_apply.clicked.connect(self._apply_reorder)

        self.btn_cancel = QPushButton("Отменить")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
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

        self._banner_parts = (banner, accent, icon, title, subtitle)
        self._style_reorder_banner()
        return banner

    def _style_reorder_banner(self) -> None:
        """Colors for the banner, which QSS can't reach by type alone."""
        banner, accent, icon, title, subtitle = self._banner_parts
        banner.setStyleSheet(
            "#ReorderBanner {"
            f"  background: {theme.token('bg_elevated')};"
            f"  border-bottom: 1px solid {theme.token('border_strong')};"
            "}"
        )
        accent.setStyleSheet(f"background: {theme.token('accent')};")
        icon.setStyleSheet(
            "QLabel {"
            f"  background: {theme.token('accent_wash')};"
            f"  color: {theme.token('accent_soft')};"
            "  border-radius: 8px;"
            "  font-size: 16px;"
            "  font-weight: 600;"
            "}"
        )
        title.setStyleSheet(
            f"color: {theme.token('text_primary')};"
            " font-size: 13px; font-weight: 600; letter-spacing: 0.1px;"
        )
        subtitle.setStyleSheet(
            f"color: {theme.token('text_muted')}; font-size: 12px;"
        )

    def _on_theme_changed(self, _mode: str) -> None:
        self._retint_toolbar()
        self._style_reorder_banner()

    # Camera ops --------------------------------------------------------

    def _reload_cameras(self) -> None:
        cameras = cfg.load_cameras()
        self.grid.set_cameras(cameras)
        self._rebuild_nvr_tiles()
        # One persistent control connection per NVR handles discovery +
        # record status. We deliberately do NOT open a fresh socket on each
        # poll: the NVR has a low session cap, and per-tick reconnects
        # accumulated/leaked sessions and eventually starved the live tiles
        # ("remote host closed the connection").
        nvrs = nvrcfg.load_nvrs()
        for nvr in nvrs:
            self._ensure_control_client(nvr)
        if nvrs and not self._record_timer.isActive():
            self._record_timer.start()
        elif not nvrs:
            self._record_timer.stop()

    def _poll_nvr_record_status(self) -> None:
        for nvr in nvrcfg.load_nvrs():
            client = self._nvr_control.get(nvr.id)
            if client is None:
                self._ensure_control_client(nvr)  # reconnect if it dropped
            else:
                client.query_record_status()  # reuse the existing session

    def _open_archive(self, nvr_id: str, channel_number: int) -> None:
        from .playback_view import PlaybackView

        nvr = next((n for n in nvrcfg.load_nvrs() if n.id == nvr_id), None)
        if nvr is None:
            log.warning("[FIX archive] menu open ignored: nvr id %s not found", nvr_id)
            return
        ch = next((c for c in nvr.channels if c.number == channel_number), None)
        ch_name = ch.name if ch else f"CH{channel_number:02d}"
        password = keystore.get_password(nvr_keyring_user(nvr.id))
        log.info("[FIX archive] menu open nvr=%s ch=%d", nvr.name, channel_number)
        # [D1] Session budget — this Xiongmai HVR appears to revoke claims
        # silently after ~6 active sessions. Live tiles + control client +
        # any open PlaybackView all consume one. Warn the user before they
        # accumulate a backlog they can't recover from without restarting
        # the NVR.
        try:
            live_tiles = sum(
                1 for tid in self.grid._tiles.keys() if tid.startswith("nvr:")
            )
        except Exception:
            live_tiles = 0
        active = live_tiles + len(self._nvr_control) + len(self._playback_windows)
        if active >= 6:
            log.warning(
                "[D1] NVR session budget at %d (live tiles=%d, control=%d, archives=%d) — "
                "next playback claim may be rejected with Ret=103; "
                "close older archive windows first",
                active, live_tiles, len(self._nvr_control), len(self._playback_windows),
            )
        try:
            view = PlaybackView(nvr, channel_number, ch_name, password, parent=None)
        except Exception:
            # [FIX archive] Any exception during PlaybackView construction
            # used to be swallowed silently — the user just saw "nothing
            # opens" with no clue why. Log the full traceback so we can
            # see exactly where the constructor died.
            log.exception("[FIX archive] PlaybackView construction failed")
            return
        view.setAttribute(Qt.WA_DeleteOnClose, True)
        view.destroyed.connect(lambda *_: self._playback_windows.remove(view)
                               if view in self._playback_windows else None)
        self._playback_windows.append(view)
        view.show()
        # [FIX archive] Make sure the window actually surfaces above the
        # main window — on some WMs `show()` alone leaves it behind the
        # parent and the user thinks "nothing happened".
        view.raise_()
        view.activateWindow()
        log.info(
            "[FIX archive] PlaybackView shown visible=%s geometry=%s",
            view.isVisible(), view.geometry(),
        )

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

    def _ensure_control_client(self, nvr) -> None:
        """Create (once) a persistent control connection for this NVR.

        Discovers channels on login, then serves periodic record-status
        queries over the SAME socket. If the connection drops, it is removed
        so the next poll recreates it.
        """
        from dvrip.client import DvripClient

        if nvr.id in self._nvr_control:
            return
        nvr_id = nvr.id
        password = keystore.get_password(nvr_keyring_user(nvr_id))
        client = DvripClient(self, auto_discover=True)
        self._nvr_control[nvr_id] = client

        def on_discovered(channels: list) -> None:
            active_map = {int(c.number): str(c.name) for c in channels}
            if not active_map:
                return
            merged = [
                nvrcfg.NvrChannel(number=c.number, name=active_map[c.number], enabled=True)
                for c in nvr.channels
                if c.number in active_map
            ]
            existing = {c.number for c in merged}
            for num in sorted(active_map):
                if num not in existing:
                    merged.append(nvrcfg.NvrChannel(number=num, name=active_map[num], enabled=True))
            before = [(c.number, c.name) for c in nvr.channels]
            after = [(c.number, c.name) for c in merged]
            if after != before:
                log.info("[NVR] %s channels changed %s -> %s", nvr.name, before, after)
                nvr.channels = merged
                saved = nvrcfg.load_nvrs()
                for s in saved:
                    if s.id == nvr_id:
                        s.channels = merged
                        break
                nvrcfg.save_nvrs(saved)
                self._rebuild_nvr_tiles()
            client.query_record_status()

        def on_down() -> None:
            c = self._nvr_control.pop(nvr_id, None)
            if c is not None:
                c.deleteLater()

        client.channelsDiscovered.connect(on_discovered)
        client.recordStatus.connect(lambda st: self.grid.set_recording_status(nvr_id, st))
        client.disconnected.connect(on_down)
        client.connect_to(nvr.host, nvr.port, nvr.username, password)

    def _dispose_control_clients(self) -> None:
        for client in list(self._nvr_control.values()):
            try:
                client.close()
            except Exception:
                log.exception("[NVR] control close failed")
            client.deleteLater()
        self._nvr_control.clear()

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
        # [FIX hygiene] Persist geometry and close any archive windows —
        # they used to outlive the main window with their DVRIP sessions.
        from PySide6.QtCore import QSettings
        QSettings("BelEye", "BelEye").setValue(
            "main_window/geometry", self.saveGeometry()
        )
        for view in list(self._playback_windows):
            try:
                view.close()
            except Exception:
                log.warning("[FIX hygiene] closing playback window failed")
        self._record_timer.stop()
        self._dispose_control_clients()
        self.grid.stop_all()
        super().closeEvent(event)
