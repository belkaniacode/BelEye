from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import config as cfg
from app import nvr_config as nvrcfg
from app import secrets as keystore
from app.config import CameraConfig
from app.nvr_config import NvrConfig, nvr_keyring_user
from .camera_form import CameraForm
from .nvr_form import NvrForm

log = logging.getLogger(__name__)

ROLE_KIND = Qt.UserRole       # "camera" | "nvr"
ROLE_ID = Qt.UserRole + 1     # source id


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки источников")
        self.resize(560, 460)

        self._cameras: list[CameraConfig] = cfg.load_cameras()
        self._nvrs: list[NvrConfig] = nvrcfg.load_nvrs()
        self._changed = False

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._edit())

        title = QLabel("Источники")
        title.setStyleSheet("font-size: 16px; font-weight: 600; color: #f1f5f9;")
        self._subtitle = QLabel("")
        self._subtitle.setStyleSheet("font-size: 12px; color: #94a3b8;")
        self._refresh_list()

        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(2)
        header.addWidget(title)
        header.addWidget(self._subtitle)

        add_cam_btn = QPushButton("Добавить камеру")
        add_cam_btn.setProperty("primary", True)
        add_cam_btn.setCursor(Qt.PointingHandCursor)
        add_cam_btn.clicked.connect(self._add_camera)

        add_nvr_btn = QPushButton("Добавить NVR")
        add_nvr_btn.setProperty("primary", True)
        add_nvr_btn.setCursor(Qt.PointingHandCursor)
        add_nvr_btn.clicked.connect(self._add_nvr)

        edit_btn = QPushButton("Изменить")
        edit_btn.setCursor(Qt.PointingHandCursor)
        edit_btn.clicked.connect(self._edit)

        del_btn = QPushButton("Удалить")
        del_btn.setProperty("danger", True)
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.clicked.connect(self._delete)

        side = QVBoxLayout()
        side.setSpacing(6)
        side.addWidget(add_cam_btn)
        side.addWidget(add_nvr_btn)
        side.addSpacing(8)
        side.addWidget(edit_btn)
        side.addWidget(del_btn)
        side.addStretch(1)

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self.list, 1)
        body.addLayout(side)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn = buttons.button(QDialogButtonBox.Close)
        close_btn.setText("Готово")
        close_btn.setCursor(Qt.PointingHandCursor)
        buttons.rejected.connect(self.accept)
        close_btn.clicked.connect(self.accept)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)
        root.addLayout(header)
        root.addLayout(body, 1)
        root.addWidget(buttons)

    # ---------------- list ----------------

    def _refresh_list(self) -> None:
        self.list.clear()
        for cam in self._cameras:
            path = "" if (cam.path or "").strip() in ("", "/") else cam.path
            # [FIX icons] Text markers instead of emoji — emoji glyphs render
            # as tofu boxes without an emoji font installed.
            item = QListWidgetItem(f"[CAM]  {cam.name}   ·   {cam.host}:{cam.port}{path}")
            item.setData(ROLE_KIND, "camera")
            item.setData(ROLE_ID, cam.id)
            self.list.addItem(item)
        for nvr in self._nvrs:
            ch_n = len(nvr.channels)
            item = QListWidgetItem(
                f"[NVR]  {nvr.name}   ·   {nvr.host}:{nvr.port}   ·   каналов: {ch_n}"
            )
            item.setData(ROLE_KIND, "nvr")
            item.setData(ROLE_ID, nvr.id)
            self.list.addItem(item)
        self._update_subtitle()

    def _update_subtitle(self) -> None:
        parts = []
        if self._cameras:
            parts.append(f"камер: {len(self._cameras)}")
        if self._nvrs:
            parts.append(f"NVR: {len(self._nvrs)}")
        self._subtitle.setText(", ".join(parts) if parts else "пусто")

    def _selected(self) -> tuple[str, str] | None:
        item = self.list.currentItem()
        if not item:
            return None
        return str(item.data(ROLE_KIND)), str(item.data(ROLE_ID))

    # ---------------- camera ops ----------------

    def _add_camera(self) -> None:
        form = CameraForm(parent=self)
        if form.exec() == QDialog.Accepted:
            cam, pwd = form.result_data()
            self._cameras = cfg.add_camera(self._cameras, cam)
            try:
                keystore.set_password(cam.id, pwd)
            except Exception as exc:
                QMessageBox.warning(self, "Keyring", f"Не удалось сохранить пароль: {exc}")
            self._changed = True
            self._refresh_list()

    def _edit_camera(self, cam_id: str) -> None:
        cam = next((c for c in self._cameras if c.id == cam_id), None)
        if not cam:
            return
        pwd = keystore.get_password(cam.id)
        form = CameraForm(camera=cam, password=pwd, parent=self)
        if form.exec() == QDialog.Accepted:
            updated, new_pwd = form.result_data()
            self._cameras = cfg.update_camera(self._cameras, updated)
            try:
                keystore.set_password(updated.id, new_pwd)
            except Exception as exc:
                QMessageBox.warning(self, "Keyring", f"Не удалось сохранить пароль: {exc}")
            self._changed = True
            self._refresh_list()

    def _delete_camera(self, cam_id: str) -> None:
        cam = next((c for c in self._cameras if c.id == cam_id), None)
        if not cam:
            return
        if QMessageBox.question(self, "Удалить", f"Удалить камеру «{cam.name}»?") != QMessageBox.Yes:
            return
        self._cameras = cfg.delete_camera(self._cameras, cam.id)
        keystore.delete_password(cam.id)
        self._changed = True
        self._refresh_list()

    # ---------------- nvr ops ----------------

    def _add_nvr(self) -> None:
        form = NvrForm(parent=self)
        if form.exec() == QDialog.Accepted:
            nvr, pwd = form.result_data()
            self._nvrs = nvrcfg.add_nvr(self._nvrs, nvr)
            try:
                keystore.set_password(nvr_keyring_user(nvr.id), pwd)
            except Exception as exc:
                QMessageBox.warning(self, "Keyring", f"Не удалось сохранить пароль: {exc}")
            self._changed = True
            self._refresh_list()

    def _edit_nvr(self, nvr_id: str) -> None:
        nvr = next((n for n in self._nvrs if n.id == nvr_id), None)
        if not nvr:
            return
        pwd = keystore.get_password(nvr_keyring_user(nvr.id))
        form = NvrForm(nvr=nvr, password=pwd, parent=self)
        if form.exec() == QDialog.Accepted:
            updated, new_pwd = form.result_data()
            self._nvrs = nvrcfg.update_nvr(self._nvrs, updated)
            try:
                keystore.set_password(nvr_keyring_user(updated.id), new_pwd)
            except Exception as exc:
                QMessageBox.warning(self, "Keyring", f"Не удалось сохранить пароль: {exc}")
            self._changed = True
            self._refresh_list()

    def _delete_nvr(self, nvr_id: str) -> None:
        nvr = next((n for n in self._nvrs if n.id == nvr_id), None)
        if not nvr:
            return
        if QMessageBox.question(self, "Удалить", f"Удалить регистратор «{nvr.name}»?") != QMessageBox.Yes:
            return
        self._nvrs = nvrcfg.delete_nvr(self._nvrs, nvr.id)
        keystore.delete_password(nvr_keyring_user(nvr.id))
        self._changed = True
        self._refresh_list()

    # ---------------- dispatch ----------------

    def _edit(self) -> None:
        sel = self._selected()
        if not sel:
            return
        kind, sid = sel
        if kind == "camera":
            self._edit_camera(sid)
        elif kind == "nvr":
            self._edit_nvr(sid)

    def _delete(self) -> None:
        sel = self._selected()
        if not sel:
            return
        kind, sid = sel
        if kind == "camera":
            self._delete_camera(sid)
        elif kind == "nvr":
            self._delete_nvr(sid)

    # ---------------- external ----------------

    def changed(self) -> bool:
        return self._changed

    def cameras(self) -> list[CameraConfig]:
        return self._cameras

    def nvrs(self) -> list[NvrConfig]:
        return self._nvrs
