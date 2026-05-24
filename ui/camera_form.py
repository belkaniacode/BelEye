from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import CameraConfig
from app.rtsp import build_rtsp_url
from video.stream_monitor import probe_rtsp

log = logging.getLogger(__name__)


class _ProbeWorker(QObject):
    """[FIX] Runs probe_rtsp off the GUI thread — UI no longer freezes."""

    done = Signal(bool, str)

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url

    def run(self) -> None:
        try:
            ok, info = probe_rtsp(self._url, timeout_s=8.0)
        except Exception as exc:
            ok, info = False, f"внутренняя ошибка: {exc}"
        self.done.emit(ok, info)


class CameraForm(QDialog):
    def __init__(
        self,
        camera: CameraConfig | None = None,
        password: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Камера")
        self.setMinimumWidth(420)
        self._camera = camera or CameraConfig()

        self.name_edit = QLineEdit(self._camera.name)
        self.host_edit = QLineEdit(self._camera.host)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self._camera.port)
        self.user_edit = QLineEdit(self._camera.username)
        self.pass_edit = QLineEdit(password)
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.path_edit = QLineEdit(self._camera.path)
        self.transport_combo = QComboBox()
        self.transport_combo.addItems(["tcp", "udp"])
        self.transport_combo.setCurrentText(self._camera.transport)

        show_pass = QPushButton("👁")
        show_pass.setCheckable(True)
        show_pass.setFixedWidth(34)
        show_pass.toggled.connect(
            lambda on: self.pass_edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        )
        pass_row = QHBoxLayout()
        pass_row.setContentsMargins(0, 0, 0, 0)
        pass_row.addWidget(self.pass_edit)
        pass_row.addWidget(show_pass)
        pass_container = QWidget()
        pass_container.setLayout(pass_row)

        form = QFormLayout()
        form.addRow("Название:", self.name_edit)
        form.addRow("Хост / IP:", self.host_edit)
        form.addRow("Порт:", self.port_spin)
        form.addRow("Логин:", self.user_edit)
        form.addRow("Пароль:", pass_container)
        form.addRow("Путь:", self.path_edit)
        form.addRow("Транспорт:", self.transport_combo)

        self.test_btn = QPushButton("Проверить соединение")
        self.test_btn.clicked.connect(self._on_test)
        self.test_status = QLabel("")
        self.test_status.setWordWrap(True)
        self.test_status.setStyleSheet("color: #94a3b8;")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("Сохранить")
        ok_btn.setProperty("primary", True)
        ok_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("Отмена")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.test_btn)
        layout.addWidget(self.test_status)

        self._probe_thread: QThread | None = None
        self._probe_worker: _ProbeWorker | None = None
        layout.addStretch(1)
        layout.addWidget(buttons)

    def _collect(self) -> tuple[CameraConfig, str]:
        cam = CameraConfig(
            id=self._camera.id,
            name=self.name_edit.text().strip() or "Camera",
            host=self.host_edit.text().strip(),
            port=int(self.port_spin.value()),
            username=self.user_edit.text(),
            path=self.path_edit.text().strip() or "/",
            transport=self.transport_combo.currentText(),
        )
        return cam, self.pass_edit.text()

    def _on_test(self) -> None:
        if self._probe_thread is not None:
            log.info("[FIX] Probe already running; ignoring re-click")
            return
        cam, pwd = self._collect()
        if not cam.host:
            self.test_status.setText("Укажите хост.")
            return
        url = build_rtsp_url(cam, pwd)
        self.test_status.setStyleSheet("color: #94a3b8;")
        self.test_status.setText("Проверка...")
        self.test_btn.setEnabled(False)

        # [FIX] Run probe in a QThread so the GUI keeps responding
        self._probe_thread = QThread(self)
        self._probe_worker = _ProbeWorker(url)
        self._probe_worker.moveToThread(self._probe_thread)
        self._probe_thread.started.connect(self._probe_worker.run)
        self._probe_worker.done.connect(self._on_probe_done)
        self._probe_thread.start()

    def _on_probe_done(self, ok: bool, info: str) -> None:
        if ok:
            self.test_status.setStyleSheet("color: #22c55e;")
            self.test_status.setText(f"OK — {info}")
        else:
            self.test_status.setStyleSheet("color: #ef4444;")
            self.test_status.setText(f"Ошибка: {info}")
        self.test_btn.setEnabled(True)
        if self._probe_thread is not None:
            self._probe_thread.quit()
            self._probe_thread.wait(500)
        self._probe_worker = None
        self._probe_thread = None

    def _on_accept(self) -> None:
        cam, _ = self._collect()
        if not cam.host:
            QMessageBox.warning(self, "Ошибка", "Укажите хост / IP камеры.")
            return
        self.accept()

    def result_data(self) -> tuple[CameraConfig, str]:
        return self._collect()
