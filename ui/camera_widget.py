from __future__ import annotations

import logging

from PySide6.QtCore import QMimeData, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDrag,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QApplication, QFrame, QMenu, QSizePolicy, QVBoxLayout, QWidget

from app.config import CameraConfig
from app.rtsp import build_rtsp_url
from app.secrets import get_password
from video.ffmpeg_player import FFmpegPlayer
from .theme import theme

log = logging.getLogger(__name__)


# Overlay colors come from the VIDEO_* token family, which is identical in
# both themes on purpose: these are painted on top of a decoded frame, and
# the tile background stays dark whatever the chrome does. See ui/theme.py.
_STATUS_TOKENS = {
    "live": "video_status_live",
    "connecting": "video_status_connecting",
    "down": "video_status_down",
    "unknown": "video_status_unknown",
}


def _status_color(status: str) -> QColor:
    return theme.color(_STATUS_TOKENS.get(status, "video_status_unknown"))


class _Overlay(QWidget):
    """
    Transparent overlay drawn ON TOP of the video. Renders the camera name
    at the bottom-left with a text shadow (no background pill), and a small
    status dot at the bottom-right. Passes all mouse events through so the
    underlying tile still handles clicks/menus.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._name = ""
        self._status = "connecting"
        self._hovered = False
        self._reorder = False
        self._drop_target = False
        self._recording = False
        # [FIX uxbug] Optional big "identity badge" — used by NVR tiles to make
        # the camera name + port number unmistakable from across the grid. The
        # bottom-left thin caption alone is too small to read on multi-tile
        # layouts, and after a drag-drop reorder the position in the grid
        # diverges from the NVR's port numbering — the user clicked tile #2
        # expecting CAM02 but got CAM04 because his persisted order is
        # [CAM01, CAM04, CAM03, CAM02].
        self._identity_badge: str = ""
        # [FIX seamless2] Circular busy loader (top-right) shown while a
        # background quality upgrade is warming up. Purely informational —
        # the video underneath keeps playing.
        self._busy = False
        self._busy_angle = 0
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(50)
        self._busy_timer.timeout.connect(self._spin)

    def set_busy(self, on: bool) -> None:
        if self._busy == on:
            return
        self._busy = on
        if on:
            self._busy_timer.start()
        else:
            self._busy_timer.stop()
        self.update()

    def _spin(self) -> None:
        self._busy_angle = (self._busy_angle + 24) % 360
        self.update()

    def set_recording(self, on: bool) -> None:
        if self._recording == on:
            return
        self._recording = on
        self.update()

    def set_identity_badge(self, text: str) -> None:
        if self._identity_badge == text:
            return
        self._identity_badge = text
        self.update()

    def set_name(self, name: str) -> None:
        self._name = name
        self.update()

    def set_status(self, status: str) -> None:
        self._status = status
        self.update()

    def set_hovered(self, on: bool) -> None:
        if self._hovered == on:
            return
        self._hovered = on
        self.update()

    def set_reorder(self, on: bool) -> None:
        if self._reorder == on:
            return
        self._reorder = on
        if not on:
            self._drop_target = False
        self.update()

    def set_drop_target(self, on: bool) -> None:
        if self._drop_target == on:
            return
        self._drop_target = on
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # Hover/selection border — drawn FIRST so subsequent text/dot sit on top.
        # Drawn HERE (in the topmost overlay) instead of in CameraTile so child
        # widgets (player) can't cover it.
        if self._drop_target:
            # Bright dashed accent — clear visual "drop here" affordance
            pen = QPen(theme.color("video_select"), 3, Qt.DashLine)
            p.setPen(pen)
            p.drawRect(2, 2, self.width() - 5, self.height() - 5)
            # tinted overlay
            p.fillRect(self.rect(), QColor(34, 197, 94, 40))
        elif self._reorder:
            pen = QPen(theme.color("video_accent"), 2, Qt.DashLine)
            p.setPen(pen)
            p.drawRect(2, 2, self.width() - 5, self.height() - 5)
        elif self._hovered:
            pen = QPen(theme.color("video_accent"), 2)
            p.setPen(pen)
            inset = 1
            p.drawRect(
                inset,
                inset,
                self.width() - 2 * inset - 1,
                self.height() - 2 * inset - 1,
            )

        # [FIX seamless2] Busy spinner — top-right, an arc rotating while a
        # quality upgrade warms up in the background.
        if self._busy:
            size = 22
            x = self.width() - size - 10
            y = 10
            pen = QPen(QColor(255, 255, 255, 230), 3)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            # drawArc angles are in 1/16 deg; span 100° arc.
            p.drawArc(x, y, size, size, -self._busy_angle * 16, 100 * 16)

        # Drag handle (grip) in reorder mode — top-right corner
        if self._reorder:
            handle_w, handle_h = 30, 22
            x = self.width() - handle_w - 6
            y = 6
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 170))
            p.drawRoundedRect(x, y, handle_w, handle_h, 4, 4)
            p.setPen(theme.color("video_overlay_fg"))
            font = QFont()
            font.setPointSize(11)
            font.setBold(True)
            p.setFont(font)
            p.drawText(QRect(x, y, handle_w, handle_h), Qt.AlignCenter, "⠿")

        # Recording badge (top-left): solid-red "● REC" pill when the channel
        # is recording. Sized to be clearly visible from across a 4-tile grid.
        if self._recording:
            font = QFont()
            font.setPointSize(10)
            font.setBold(True)
            p.setFont(font)
            label = "REC"
            fm = QFontMetrics(font)
            dot_d = 10
            pad_x = 9
            gap = 6
            text_w = fm.horizontalAdvance(label)
            badge_w = pad_x + dot_d + gap + text_w + pad_x
            badge_h = 24
            bx, by = 10, 10
            # Filled red pill — unmistakable
            p.setPen(Qt.NoPen)
            p.setBrush(theme.color("video_rec"))
            p.drawRoundedRect(bx, by, badge_w, badge_h, badge_h // 2, badge_h // 2)
            # White dot inside the pill
            p.setBrush(theme.color("video_overlay_fg"))
            p.drawEllipse(bx + pad_x, by + (badge_h - dot_d) // 2, dot_d, dot_d)
            p.setPen(theme.color("video_overlay_fg"))
            p.drawText(
                QRect(bx + pad_x + dot_d + gap, by, text_w + 4, badge_h),
                Qt.AlignVCenter | Qt.AlignLeft,
                label,
            )

        # [FIX uxbug] Big identity badge — top-right of the tile, mirror of
        # the REC badge on the left. Always-on text in a contrasting pill so
        # the user can tell at a glance "this tile is CAM04 / port 4" even
        # in a 4-up grid. Disambiguates the post-reorder scenario where the
        # grid position doesn't match the NVR's port numbering.
        if self._identity_badge:
            font = QFont()
            font.setPointSize(11)
            font.setBold(True)
            p.setFont(font)
            fm = QFontMetrics(font)
            text_w = fm.horizontalAdvance(self._identity_badge)
            pad_x = 10
            badge_w = pad_x * 2 + text_w
            badge_h = 24
            bx = self.width() - badge_w - 10
            by = 10
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(15, 23, 42, 220))  # near-black translucent pill
            p.drawRoundedRect(bx, by, badge_w, badge_h, badge_h // 2, badge_h // 2)
            p.setPen(theme.color("video_name_fg"))  # amber-50 — high contrast
            p.drawText(
                QRect(bx, by, badge_w, badge_h),
                Qt.AlignCenter,
                self._identity_badge,
            )

        if not self._name and not self._status:
            return

        # Status dot (bottom-right)
        dot_color = _status_color(self._status)
        dot_d = 10
        margin = 8
        dot_rect = QRect(
            self.width() - dot_d - margin,
            self.height() - dot_d - margin,
            dot_d,
            dot_d,
        )
        p.setPen(QPen(QColor(0, 0, 0, 180), 1))
        p.setBrush(dot_color)
        p.drawEllipse(dot_rect)

        # Name (bottom-left) with subtle shadow for legibility on any background
        if self._name:
            font = QFont()
            font.setPointSize(9)
            font.setWeight(QFont.Medium)
            font.setLetterSpacing(QFont.AbsoluteSpacing, 0.2)
            p.setFont(font)
            fm = QFontMetrics(font)
            x = margin
            y = self.height() - margin - fm.descent()
            # subtle shadow (offset 1px down + soft dark)
            p.setPen(QColor(0, 0, 0, 180))
            p.drawText(x + 1, y + 1, self._name)
            # body — slightly off-white for less contrast harshness
            p.setPen(theme.color("video_text"))
            p.drawText(x, y, self._name)


CAMERA_MIME = "application/x-beleye-camera-id"


class DraggableTileMixin:
    """Drag-and-drop reordering shared by RTSP and NVR tiles.

    Host widget must provide:
      - ``self._overlay`` (an _Overlay) for the reorder/drop visuals,
      - ``self.swapRequested`` signal (str src_id, str dst_id),
      - ``self.drag_id`` property returning this tile's grid key.
    The grid keys it against ``GridView._tiles`` so swap works uniformly.
    """

    def _init_drag(self) -> None:
        self._reorder_mode = False
        self._drag_start: QPoint | None = None

    def set_reorder_mode(self, on: bool) -> None:
        if self._reorder_mode == on:
            return
        self._reorder_mode = on
        self.setAcceptDrops(on)
        self._overlay.set_reorder(on)
        if on:
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.unsetCursor()
            self._drag_start = None

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._reorder_mode and event.button() == Qt.LeftButton:
            self._drag_start = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            self._reorder_mode
            and self._drag_start is not None
            and (event.buttons() & Qt.LeftButton)
        ):
            delta = (event.position().toPoint() - self._drag_start).manhattanLength()
            if delta >= QApplication.startDragDistance():
                self._start_drag()
                self._drag_start = None
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._reorder_mode:
            self.setCursor(Qt.OpenHandCursor)
        self._drag_start = None
        super().mouseReleaseEvent(event)

    def _start_drag(self) -> None:
        log.info("[FIX] Drag start: tile=%s", self.drag_id)
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(CAMERA_MIME, self.drag_id.encode("utf-8"))
        drag.setMimeData(mime)
        pix = self.grab()
        target_w = min(240, self.width())
        if pix.width() > target_w:
            pix = pix.scaledToWidth(target_w, Qt.SmoothTransformation)
        drag.setPixmap(pix)
        drag.setHotSpot(pix.rect().center())
        drag.exec(Qt.MoveAction)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if not self._reorder_mode:
            event.ignore()
            return
        if event.mimeData().hasFormat(CAMERA_MIME):
            src_id = bytes(event.mimeData().data(CAMERA_MIME)).decode("utf-8")
            if src_id != self.drag_id:
                event.acceptProposedAction()
                self._overlay.set_drop_target(True)
                return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._overlay.set_drop_target(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self._overlay.set_drop_target(False)
        if not (self._reorder_mode and event.mimeData().hasFormat(CAMERA_MIME)):
            event.ignore()
            return
        src_id = bytes(event.mimeData().data(CAMERA_MIME)).decode("utf-8")
        if src_id and src_id != self.drag_id:
            log.info("[FIX] Drop swap: %s -> %s", src_id, self.drag_id)
            self.swapRequested.emit(src_id, self.drag_id)
            event.acceptProposedAction()
        else:
            event.ignore()


class CameraTile(DraggableTileMixin, QFrame):
    expandRequested = Signal(str)
    editRequested = Signal(str)
    removeRequested = Signal(str)
    reconnectRequested = Signal(str)
    swapRequested = Signal(str, str)  # source_id, target_id

    @property
    def drag_id(self) -> str:
        return self.camera.id

    def __init__(self, camera: CameraConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.camera = camera
        self.setObjectName("CameraTile")
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        url = build_rtsp_url(camera, get_password(camera.id))
        self.player = FFmpegPlayer(url, self)
        self.player.streamUp.connect(lambda: self._overlay.set_status("live"))
        self.player.streamDown.connect(lambda _r: self._overlay.set_status("down"))

        self._overlay = _Overlay(self)
        self._overlay.set_name(camera.name)
        self._overlay.set_status("connecting")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.player)

        # Compatibility shim for grid_view._name_label.setText calls
        self._name_label = _NameProxy(self._overlay)

        # Hover/selection border is drawn by the topmost _Overlay so it
        # cannot be covered by the child player widget. CameraTile itself
        # carries only the dark background; no QSS border (would be hidden
        # by the child widgets anyway).
        self.setMouseTracking(True)
        # Background comes from the global QSS rule for #CameraTile
        # (video_bg — dark in both themes by design).

        self._init_drag()

    # Public ------------------------------------------------------------

    def start(self) -> None:
        self._overlay.set_status("connecting")
        self.player.start()

    def stop(self) -> None:
        self.player.stop()
        self._overlay.set_status("down")

    def reload_credentials(self) -> None:
        url = build_rtsp_url(self.camera, get_password(self.camera.id))
        was_running = self.player.is_running() or not self.player._stopped
        self.player.stop()
        self.player.set_url(url)
        self._overlay.set_status("connecting")
        if was_running:
            self.player.start()

    # Events ------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._overlay.setGeometry(0, 0, self.width(), self.height())

    def enterEvent(self, event) -> None:  # noqa: N802
        self._overlay.set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._overlay.set_hovered(False)
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.expandRequested.emit(self.camera.id)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        act_edit = QAction("Изменить...", menu)
        act_remove = QAction("Удалить", menu)
        act_reconnect = QAction("Переподключить", menu)
        menu.addAction(act_edit)
        menu.addAction(act_reconnect)
        menu.addSeparator()
        menu.addAction(act_remove)
        act_edit.triggered.connect(lambda: self.editRequested.emit(self.camera.id))
        act_remove.triggered.connect(lambda: self.removeRequested.emit(self.camera.id))
        act_reconnect.triggered.connect(lambda: self.reconnectRequested.emit(self.camera.id))
        menu.exec(event.globalPos())


class _NameProxy:
    """Tiny shim so existing `_name_label.setText(...)` callers keep working."""

    def __init__(self, overlay: _Overlay) -> None:
        self._overlay = overlay

    def setText(self, text: str) -> None:
        self._overlay.set_name(text)
