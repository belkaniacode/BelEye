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
from .icon_util import eye_icon
from .theme import theme

log = logging.getLogger(__name__)


class _ProbeWorker(QObject):
    """[FIX] Runs probe_rtsp off the GUI thread — UI no longer freezes."""

    done = Signal(bool, str)

    def __init__(self, url: str, transport: str = "tcp") -> None:
        super().__init__()
        self._url = url
        self._transport = transport

    def run(self) -> None:
        try:
            ok, info = probe_rtsp(self._url, timeout_s=8.0,
                                  transport=self._transport)
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
        # [FIX icons] qdarktheme renders the spin arrows as a broken dash on
        # this platform; ports are typed anyway, so drop the buttons.
        self.port_spin.setButtonSymbols(QSpinBox.NoButtons)
        self.user_edit = QLineEdit(self._camera.username)
        self.pass_edit = QLineEdit(password)
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.path_edit = QLineEdit(self._camera.path)
        self.transport_combo = QComboBox()
        self.transport_combo.addItems(["tcp", "udp"])
        self.transport_combo.setCurrentText(self._camera.transport)

        # [FIX icons] Painted eye icon — the previous "👁" emoji rendered as
        # a tofu box on systems without an emoji font.
        self._show_pass = QPushButton()
        self._show_pass.setCheckable(True)
        self._show_pass.setFixedWidth(34)
        self._show_pass.setCursor(Qt.PointingHandCursor)
        self._show_pass.toggled.connect(self._toggle_pass)
        show_pass = self._show_pass
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
        # Status color is semantic, so it has to be re-resolved on a theme
        # flip — remember which state we are in.
        self._status_token = "text_muted"

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

        self._apply_theme()
        theme.changed.connect(lambda _m: self._apply_theme())

    def _toggle_pass(self, on: bool) -> None:
        self.pass_edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        self._show_pass.setToolTip("Скрыть пароль" if on else "Показать пароль")
        self._apply_theme()

    def _apply_theme(self) -> None:
        # QIcon bakes its color into a pixmap, so it must be rebuilt.
        self._show_pass.setIcon(eye_icon(slashed=not self._show_pass.isChecked()))
        self.test_status.setStyleSheet(f"color: {theme.token(self._status_token)};")

    def _set_status(self, text: str, token: str) -> None:
        self._status_token = token
        self.test_status.setText(text)
        self.test_status.setStyleSheet(f"color: {theme.token(token)};")

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
            self._set_status("Укажите хост.", "danger")
            return
        url = build_rtsp_url(cam, pwd)
        # Probe over the transport the user picked — otherwise a green test
        # would say nothing about how playback will actually behave.
        self._set_status(f"Проверка ({cam.transport.upper()})...", "text_muted")
        self.test_btn.setEnabled(False)

        # [FIX] Run probe in a QThread so the GUI keeps responding
        self._probe_thread = QThread(self)
        self._probe_worker = _ProbeWorker(url, cam.transport)
        self._probe_worker.moveToThread(self._probe_thread)
        self._probe_thread.started.connect(self._probe_worker.run)
        self._probe_worker.done.connect(self._on_probe_done)
        self._probe_thread.start()

    def _on_probe_done(self, ok: bool, info: str) -> None:
        if ok:
            self._set_status(f"OK — {info}", "success")
        else:
            self._set_status(f"Ошибка: {info}", "danger")
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
