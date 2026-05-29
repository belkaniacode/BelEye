from __future__ import annotations

import logging
import math
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGridLayout, QLabel, QStackedLayout, QWidget

from app.config import CameraConfig
from app.nvr_config import NvrConfig
from .camera_widget import CameraTile
from .nvr_channel_widget import NvrChannelTile, nvr_tile_id

log = logging.getLogger(__name__)


class GridView(QWidget):
    """Auto-layout grid of camera tiles with single-camera focus mode."""

    editRequested = Signal(str)
    removeRequested = Signal(str)
    orderChanged = Signal(list)  # emitted on apply_reorder with new list[id]

    MODE_GRID = "grid"
    MODE_SINGLE = "single"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Heterogeneous: CameraTile for RTSP, NvrChannelTile for NVR channels.
        # Both expose the same minimal interface used here (start/stop, signals,
        # set_reorder_mode). NVR tile ids are namespaced ("nvr:<id>:ch<n>").
        self._tiles: dict[str, "CameraTile | NvrChannelTile"] = {}
        self._mode = self.MODE_GRID
        self._focused: Optional[str] = None
        self._reorder_mode = False
        self._order_snapshot: list[str] = []

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self._grid_container = QWidget()
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setSpacing(0)
        self._grid_layout.setContentsMargins(0, 0, 0, 0)

        self._single_container = QWidget()
        self._single_layout = QGridLayout(self._single_container)
        self._single_layout.setSpacing(0)
        self._single_layout.setContentsMargins(0, 0, 0, 0)

        self._empty = QLabel("Камеры не настроены.\nОткройте «Настройки» и добавьте RTSP-камеру.")
        self._empty.setStyleSheet("color: #94a3b8; font-size: 16px;")
        self._empty.setAlignment(Qt.AlignCenter)

        self._stack.addWidget(self._grid_container)  # index 0
        self._stack.addWidget(self._single_container)  # index 1
        self._stack.addWidget(self._empty)  # index 2

    # Public -------------------------------------------------------------

    def set_cameras(self, cameras: list[CameraConfig]) -> None:
        """
        [FIX] Diff-update: keep tiles whose URL is unchanged, only stop/recreate
        modified ones, and remove deleted ones. Avoids the "freeze everything,
        rebuild everything" behavior that hung the UI on every settings save.

        Touches only RTSP CameraTile entries; NVR tiles are managed separately
        by ``set_nvr_channels`` and survive this call untouched.
        """
        new_ids = {c.id for c in cameras}

        # Remove tiles for deleted RTSP cameras (skip NVR tiles).
        for cam_id in list(self._tiles.keys()):
            if cam_id.startswith("nvr:"):
                continue
            if cam_id not in new_ids:
                tile = self._tiles.pop(cam_id)
                tile.stop()
                tile.setParent(None)
                tile.deleteLater()

        # Add or update tiles (one keyring lookup + URL build per camera)
        from app.rtsp import build_rtsp_url
        from app.secrets import get_password

        for cam in cameras:
            new_url = build_rtsp_url(cam, get_password(cam.id))
            existing = self._tiles.get(cam.id)
            if existing is None:
                tile = CameraTile(cam, self)
                tile.expandRequested.connect(self._on_expand)
                tile.editRequested.connect(self.editRequested)
                tile.removeRequested.connect(self.removeRequested)
                tile.reconnectRequested.connect(self._on_reconnect)
                tile.swapRequested.connect(self._on_swap)
                tile._current_url = new_url
                if self._reorder_mode:
                    tile.set_reorder_mode(True)
                self._tiles[cam.id] = tile
                tile.start()
            else:
                existing.camera = cam
                existing._name_label.setText(cam.name)
                if getattr(existing, "_current_url", None) != new_url:
                    existing._current_url = new_url
                    existing.reload_credentials()

        self._relayout()

    def set_recording_status(self, nvr_id: str, status: dict) -> None:
        """Apply per-channel recording flags to this NVR's tiles.
        ``status`` maps 1-based channel number -> bool recording."""
        for ch_no, recording in status.items():
            tid = nvr_tile_id(nvr_id, int(ch_no))
            tile = self._tiles.get(tid)
            if tile is not None and hasattr(tile, "set_recording"):
                tile.set_recording(bool(recording))

    def set_nvr_channels(
        self,
        items: list[tuple["NvrConfig", "object", str]],
    ) -> None:
        """Sync NVR-channel tiles. ``items`` is a list of (nvr, channel, password).

        Diff-based: only restarts a tile if its (nvr_id, channel_no) is new or
        its connection params changed. NVR tiles are keyed under ``nvr:...``;
        ``set_cameras`` ignores those keys.
        """
        desired_ids = {nvr_tile_id(nvr.id, ch.number) for nvr, ch, _ in items}

        # Remove NVR tiles that are no longer wanted (channel disabled / NVR
        # deleted / replaced by a new id).
        for tile_id in list(self._tiles.keys()):
            if not tile_id.startswith("nvr:"):
                continue
            if tile_id not in desired_ids:
                tile = self._tiles.pop(tile_id)
                tile.stop()
                tile.setParent(None)
                tile.deleteLater()

        for nvr, channel, password in items:
            tid = nvr_tile_id(nvr.id, channel.number)
            existing = self._tiles.get(tid)
            if existing is None:
                tile = NvrChannelTile(nvr, channel, password, self)
                tile.expandRequested.connect(self._on_expand)
                tile.editRequested.connect(self.editRequested)
                tile.removeRequested.connect(self.removeRequested)
                tile.reconnectRequested.connect(self._on_reconnect)
                tile.swapRequested.connect(self._on_swap)
                if self._reorder_mode:
                    tile.set_reorder_mode(True)
                self._tiles[tid] = tile
                tile.start()
            else:
                # If host/port/user/channel name changed, restart.
                changed = (
                    existing.nvr.host != nvr.host
                    or existing.nvr.port != nvr.port
                    or existing.nvr.username != nvr.username
                    or existing._password != password
                )
                existing.nvr = nvr
                existing.channel = channel
                existing._password = password
                existing._overlay.set_name(f"{nvr.name} · {channel.name}")
                if changed:
                    existing.reload_credentials()
        self._relayout()

    def show_grid(self) -> None:
        self._mode = self.MODE_GRID
        self._focused = None
        self._relayout()

    def show_single(self, camera_id: str) -> None:
        if camera_id not in self._tiles:
            return
        self._mode = self.MODE_SINGLE
        self._focused = camera_id
        self._relayout()

    def toggle_mode(self) -> None:
        if self._mode == self.MODE_SINGLE:
            self.show_grid()
        elif self._tiles:
            self.show_single(next(iter(self._tiles)))

    # Internal -----------------------------------------------------------

    def _on_expand(self, camera_id: str) -> None:
        if self._mode == self.MODE_SINGLE and self._focused == camera_id:
            self.show_grid()
        else:
            self.show_single(camera_id)

    def _on_reconnect(self, camera_id: str) -> None:
        tile = self._tiles.get(camera_id)
        if tile:
            tile.stop()
            tile.start()

    def _clear_layout(self, layout: QGridLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

    def _relayout(self) -> None:
        self._clear_layout(self._grid_layout)
        self._clear_layout(self._single_layout)

        if not self._tiles:
            self._stack.setCurrentWidget(self._empty)
            return

        if self._mode == self.MODE_SINGLE and self._focused in self._tiles:
            tile = self._tiles[self._focused]
            self._single_layout.addWidget(tile, 0, 0)
            self._stack.setCurrentWidget(self._single_container)
            return

        # Grid mode
        n = len(self._tiles)
        cols = max(1, math.ceil(math.sqrt(n)))
        rows = math.ceil(n / cols)
        for i, tile in enumerate(self._tiles.values()):
            r, c = divmod(i, cols)
            self._grid_layout.addWidget(tile, r, c)
        for r in range(rows):
            self._grid_layout.setRowStretch(r, 1)
        for c in range(cols):
            self._grid_layout.setColumnStretch(c, 1)

        self._stack.setCurrentWidget(self._grid_container)

    def stop_all(self) -> None:
        for tile in self._tiles.values():
            tile.stop()

    # Reorder mode -----------------------------------------------------

    def is_reorder_mode(self) -> bool:
        return self._reorder_mode

    def enter_reorder_mode(self) -> None:
        if self._reorder_mode:
            return
        self._reorder_mode = True
        self._order_snapshot = list(self._tiles.keys())
        for tile in self._tiles.values():
            tile.set_reorder_mode(True)
        # Force grid mode (can't reorder a single-cam view)
        if self._mode != self.MODE_GRID:
            self.show_grid()

    def apply_reorder(self) -> list[str]:
        if not self._reorder_mode:
            return list(self._tiles.keys())
        self._reorder_mode = False
        for tile in self._tiles.values():
            tile.set_reorder_mode(False)
        new_order = list(self._tiles.keys())
        self.orderChanged.emit(new_order)
        return new_order

    def cancel_reorder(self) -> None:
        if not self._reorder_mode:
            return
        # Restore order from snapshot
        snapshot = self._order_snapshot
        restored: dict[str, CameraTile] = {}
        for cam_id in snapshot:
            if cam_id in self._tiles:
                restored[cam_id] = self._tiles[cam_id]
        # Append any tiles that weren't in the snapshot (shouldn't happen, defensive)
        for cam_id, tile in self._tiles.items():
            if cam_id not in restored:
                restored[cam_id] = tile
        self._tiles = restored
        self._reorder_mode = False
        for tile in self._tiles.values():
            tile.set_reorder_mode(False)
        self._relayout()

    def _on_swap(self, src_id: str, dst_id: str) -> None:
        if src_id not in self._tiles or dst_id not in self._tiles:
            return
        items = list(self._tiles.items())
        src_idx = next(i for i, (k, _) in enumerate(items) if k == src_id)
        dst_idx = next(i for i, (k, _) in enumerate(items) if k == dst_id)
        items[src_idx], items[dst_idx] = items[dst_idx], items[src_idx]
        self._tiles = dict(items)
        self._relayout()
