"""Add/Edit NVR dialog with a live DVRIP connection test."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
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

from app.nvr_config import NvrChannel, NvrConfig
from dvrip.client import DvripClient

log = logging.getLogger(__name__)

PROBE_TIMEOUT_MS = 12_000


class NvrForm(QDialog):
    """Form for one NVR. Probes connection via DVRIP and stores discovered channels."""

    def __init__(
        self,
        nvr: NvrConfig | None = None,
        password: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Видеорегистратор (NVR)")
        self.setMinimumWidth(440)
        self._nvr = nvr or NvrConfig()
        self._discovered: list[NvrChannel] | None = None

        self.name_edit = QLineEdit(self._nvr.name)
        self.host_edit = QLineEdit(self._nvr.host)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self._nvr.port)
        self.user_edit = QLineEdit(self._nvr.username)
        self.pass_edit = QLineEdit(password)
        self.pass_edit.setEchoMode(QLineEdit.Password)

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

        self.test_btn = QPushButton("Проверить соединение")
        self.test_btn.setCursor(Qt.PointingHandCursor)
        self.test_btn.clicked.connect(self._on_test)
        self.test_status = QLabel(
            f"Найдено каналов: {len(self._nvr.channels)}" if self._nvr.channels else
            "Введите данные и нажмите «Проверить соединение»."
        )
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
        layout.addStretch(1)
        layout.addWidget(buttons)

        self._client: DvripClient | None = None
        self._timeout_timer: QTimer | None = None

    # ---------------- probe ----------------

    def _on_test(self) -> None:
        if self._client is not None:
            log.info("[NVR] probe already running; ignoring re-click")
            return
        host = self.host_edit.text().strip()
        if not host:
            self._set_status("Укажите хост / IP.", error=True)
            return
        user = self.user_edit.text()
        password = self.pass_edit.text()
        port = int(self.port_spin.value())

        self._set_status("Подключение...", error=False)
        self.test_btn.setEnabled(False)

        c = DvripClient(self)
        c.loginOk.connect(self._on_login_ok)
        c.loginFailed.connect(lambda r: self._finish_probe(False, f"Логин отклонён: {r}"))
        c.error.connect(lambda e: self._finish_probe(False, f"Сеть: {e}"))
        c.channelsDiscovered.connect(self._on_channels)
        self._client = c

        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(
            lambda: self._finish_probe(False, "Таймаут (нет ответа за 12 с).")
        )
        self._timeout_timer.start(PROBE_TIMEOUT_MS)

        log.info("[NVR] probe %s:%d as %s", host, port, user)
        c.connect_to(host, port, user, password)

    def _on_login_ok(self, sid: int) -> None:
        self._set_status(f"Логин ok (session 0x{sid:08x}). Жду список каналов...", error=False)

    def _on_channels(self, channels: list) -> None:
        # channels: list[dvrip.client.Channel]
        if not channels:
            self._finish_probe(
                False,
                "Логин ok, но прошивка не вернула список каналов. "
                "Пришли лог `[DVRIP] recv …` — подкручу парсер.",
            )
            return
        self._discovered = [
            NvrChannel(number=int(c.number), name=str(c.name) or f"CH{c.number:02d}", enabled=True)
            for c in channels
        ]
        self._finish_probe(True, f"Найдено каналов: {len(self._discovered)}")

    def _finish_probe(self, ok: bool, message: str) -> None:
        self._set_status(message, error=not ok)
        self.test_btn.setEnabled(True)
        if self._timeout_timer is not None:
            self._timeout_timer.stop()
            self._timeout_timer = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                log.exception("[NVR] probe close failed")
            self._client.deleteLater()
            self._client = None

    def _set_status(self, text: str, *, error: bool) -> None:
        color = "#ef4444" if error else "#22c55e" if "Найдено" in text or "ok" in text.lower() else "#94a3b8"
        self.test_status.setStyleSheet(f"color: {color};")
        self.test_status.setText(text)

    # ---------------- accept ----------------

    def _on_accept(self) -> None:
        if not self.host_edit.text().strip():
            QMessageBox.warning(self, "Ошибка", "Укажите хост / IP регистратора.")
            return
        if self._discovered is None and not self._nvr.channels:
            res = QMessageBox.question(
                self,
                "Без проверки",
                "Соединение не проверено и каналов нет. Сохранить всё равно?",
            )
            if res != QMessageBox.Yes:
                return
        self.accept()

    def result_data(self) -> tuple[NvrConfig, str]:
        channels = self._discovered if self._discovered is not None else self._nvr.channels
        nvr = NvrConfig(
            id=self._nvr.id,
            name=self.name_edit.text().strip() or "NVR",
            host=self.host_edit.text().strip(),
            port=int(self.port_spin.value()),
            username=self.user_edit.text(),
            channels=channels,
        )
        return nvr, self.pass_edit.text()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        # Make sure we never leave a probe socket dangling.
        if self._client is not None:
            self._finish_probe(False, "Закрыто.")
        super().closeEvent(event)
