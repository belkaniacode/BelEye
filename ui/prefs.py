"""UI preferences — small, persistent, observable.

Settings that change how the interface behaves rather than how it looks.
Same shape as ``ui/theme.py``: a module-level singleton backed by QSettings
that emits a signal so open widgets can react without being polled.

Safe to import before ``QApplication`` exists — nothing here touches the GUI
until a getter is called.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QSettings, Signal

log = logging.getLogger(__name__)

#: All camera tiles stream at full quality, not just the focused one.
KEY_HQ_ALL = "ui/hq_all"


class UiPrefs(QObject):
    """Persistent UI preferences with change notification."""

    #: (key, value) after a preference actually changed.
    changed = Signal(str, object)

    @staticmethod
    def _store() -> QSettings:
        return QSettings("BelEye", "BelEye")

    # -------- hq_all --------

    def hq_all(self) -> bool:
        """Keep every tile on the Main stream instead of only the focused one.

        Defaults to False: the sub stream is what an unconfigured install
        gets, because Main on every channel is heavy enough to saturate a
        small NVR (see NvrConfig.prefer_substream).
        """
        raw = self._store().value(KEY_HQ_ALL, False)
        if isinstance(raw, bool):
            return raw
        # QSettings round-trips bools as the strings "true"/"false" on some
        # backends.
        return str(raw).strip().lower() in ("true", "1", "yes")

    def set_hq_all(self, value: bool) -> None:
        value = bool(value)
        if value == self.hq_all():
            return
        store = self._store()
        store.setValue(KEY_HQ_ALL, value)
        store.sync()
        log.info("[hq] hq_all -> %s", value)
        self.changed.emit(KEY_HQ_ALL, value)


#: Application-wide singleton.
prefs = UiPrefs()
