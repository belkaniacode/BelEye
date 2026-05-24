from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import config as cfg
from app import secrets as keystore
from app.config import CameraConfig
from .camera_form import CameraForm

log = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки камер")
        self.resize(520, 420)

        self._cameras: list[CameraConfig] = cfg.load_cameras()
        self._changed = False

        self.list = QListWidget()
        self._refresh_list()
        self.list.itemDoubleClicked.connect(lambda _i: self._edit())

        from PySide6.QtWidgets import QLabel

        title = QLabel("Камеры")
        title.setStyleSheet("font-size: 16px; font-weight: 600; color: #f1f5f9;")
        subtitle = QLabel(f"{len(self._cameras)} в списке")
        subtitle.setStyleSheet("font-size: 12px; color: #94a3b8;")
        self._subtitle = subtitle

        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(2)
        header.addWidget(title)
        header.addWidget(subtitle)

        add_btn = QPushButton("Добавить камеру")
        add_btn.setProperty("primary", True)
        add_btn.setCursor(Qt.PointingHandCursor)
        edit_btn = QPushButton("Изменить")
        edit_btn.setCursor(Qt.PointingHandCursor)
        del_btn = QPushButton("Удалить")
        del_btn.setProperty("danger", True)
        del_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(self._add)
        edit_btn.clicked.connect(self._edit)
        del_btn.clicked.connect(self._delete)

        side = QVBoxLayout()
        side.setSpacing(6)
        side.addWidget(add_btn)
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

    def _refresh_list(self) -> None:
        self.list.clear()
        for cam in self._cameras:
            path = "" if (cam.path or "").strip() in ("", "/") else cam.path
            item = QListWidgetItem(f"{cam.name}   ·   {cam.host}:{cam.port}{path}")
            item.setData(Qt.UserRole, cam.id)
            self.list.addItem(item)
        if hasattr(self, "_subtitle"):
            self._subtitle.setText(f"{len(self._cameras)} в списке")

    def _selected(self) -> CameraConfig | None:
        item = self.list.currentItem()
        if not item:
            return None
        cam_id = item.data(Qt.UserRole)
        return next((c for c in self._cameras if c.id == cam_id), None)

    def _add(self) -> None:
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

    def _edit(self) -> None:
        cam = self._selected()
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

    def _delete(self) -> None:
        cam = self._selected()
        if not cam:
            return
        if QMessageBox.question(
            self, "Удалить", f"Удалить камеру «{cam.name}»?"
        ) != QMessageBox.Yes:
            return
        self._cameras = cfg.delete_camera(self._cameras, cam.id)
        keystore.delete_password(cam.id)
        self._changed = True
        self._refresh_list()

    def changed(self) -> bool:
        return self._changed

    def cameras(self) -> list[CameraConfig]:
        return self._cameras
