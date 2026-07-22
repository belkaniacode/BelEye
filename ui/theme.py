"""Theme engine — semantic color tokens + light/dark switching.

Single source of truth for every color in the UI. Widgets must never carry
hardcoded hex literals; they ask for a *semantic* token instead:

    from ui.theme import theme
    color = theme.color("text_muted")          # QColor, current theme
    css   = f"color: {theme.token('accent')};" # str "#rrggbb"

Three token tables live here:

* ``DARK`` / ``LIGHT`` — the chrome (toolbar, dialogs, lists, calendar…).
  ``DARK`` is a verbatim extraction of the palette the app shipped with, so
  switching to it is a no-op visually.
* ``VIDEO`` — surfaces that show camera footage. These are deliberately
  **theme-independent and always dark**, the way VLC and professional NVR
  clients behave: a light video backdrop would wash out the OSD overlays
  painted on top of the frame (camera name, REC dot, "нет сигнала").

Consumption rules for widgets:

* static chrome  → let the global QSS do it, no per-widget stylesheet;
* inline stylesheet that QSS can't express → put it in an ``_apply_theme()``
  method and connect ``theme.changed`` to it;
* ``paintEvent`` → call ``theme.color(...)`` *at paint time*, never cache the
  QColor in ``__init__``, and connect ``theme.changed`` to ``update``;
* baked pixmaps (icons) → rebuild them on ``theme.changed``; a pixmap cannot
  re-tint itself.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from string import Template

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)

LIGHT = "light"
DARK = "dark"

SETTINGS_KEY = "ui/theme"
_QSS_TEMPLATE = Path(__file__).resolve().parent.parent / "resources" / "styles.qss.tmpl"

# Loud magenta: an unknown token must be impossible to miss on screen.
_MISSING = "#ff00ff"


# --------------------------------------------------------------------------
# Token tables
# --------------------------------------------------------------------------

#: Chrome palette, dark. Extracted 1:1 from the original resources/styles.qss
#: so that "dark" after this refactor renders exactly as it did before.
DARK_TOKENS: dict[str, str] = {
    # surfaces
    "bg_base": "#0b0d10",
    "bg_surface": "#0f1115",
    "bg_elevated": "#13161c",
    "bg_hover": "#1a1d22",
    "bg_active": "#1f242b",
    "bg_pressed": "#15181d",
    "bg_input": "#13161c",
    "bg_input_focus": "#15181d",
    "bg_disabled": "#13161c",
    # borders
    "border": "#1a1d22",
    "border_strong": "#1f242b",
    "border_stronger": "#2a3038",
    "border_focus": "#3b82f6",
    # text
    "text_primary": "#e5e7eb",
    "text_secondary": "#cbd5e1",
    "text_muted": "#94a3b8",
    "text_disabled": "#475569",
    "text_hover": "#ffffff",
    # accent
    "accent": "#3b82f6",
    "accent_hover": "#2563eb",
    "accent_pressed": "#1d4ed8",
    "accent_fg": "#ffffff",
    "accent_soft": "#60a5fa",
    "accent_wash": "rgba(59, 130, 246, 0.12)",
    "selection_bg": "#1a3a6b",
    "selection_fg": "#ffffff",
    # semantic status (chrome only — tile overlays use the VIDEO_* set)
    "danger": "#f87171",
    "danger_strong": "#dc2626",
    "danger_bg": "#2a1518",
    "danger_border": "#2a1f24",
    "danger_border_hover": "#3a1f25",
    "danger_hover_fg": "#fca5a5",
    "warning": "#eab308",
    "warning_fg": "#0b0d10",
    "success": "#22c55e",
    # scrollbars
    "scroll_handle": "#2a3038",
    "scroll_handle_hover": "#3a4250",
    # tooltips
    "tooltip_bg": "#1a1d22",
    "tooltip_fg": "#e5e7eb",
    "tooltip_border": "#2a3038",
}

#: Chrome palette, light. Every key of DARK_TOKENS must be present — the
#: audit in scripts/probe_theme.py enforces that. Contrast of every
#: text-on-background pair was checked against WCAG AA (>= 4.5:1).
LIGHT_TOKENS: dict[str, str] = {
    # surfaces
    "bg_base": "#f8fafc",
    "bg_surface": "#ffffff",
    "bg_elevated": "#ffffff",
    "bg_hover": "#f1f5f9",
    "bg_active": "#e2e8f0",
    "bg_pressed": "#e2e8f0",
    "bg_input": "#ffffff",
    "bg_input_focus": "#ffffff",
    "bg_disabled": "#f1f5f9",
    # borders
    "border": "#e2e8f0",
    "border_strong": "#cbd5e1",
    "border_stronger": "#94a3b8",
    "border_focus": "#2563eb",
    # text
    "text_primary": "#0f172a",   # 17.2:1 on bg_base
    "text_secondary": "#334155",  # 9.9:1
    "text_muted": "#64748b",     # 4.6:1
    "text_disabled": "#94a3b8",
    "text_hover": "#0f172a",
    # accent
    "accent": "#2563eb",         # white on it = 5.2:1
    "accent_hover": "#1d4ed8",
    "accent_pressed": "#1e40af",
    "accent_fg": "#ffffff",
    "accent_soft": "#2563eb",
    "accent_wash": "rgba(37, 99, 235, 0.10)",
    "selection_bg": "#dbeafe",
    "selection_fg": "#1e3a8a",
    # semantic status
    "danger": "#dc2626",         # 4.6:1 on bg_base
    "danger_strong": "#b91c1c",
    "danger_bg": "#fee2e2",
    "danger_border": "#fecaca",
    "danger_border_hover": "#fca5a5",
    "danger_hover_fg": "#b91c1c",
    "warning": "#a16207",        # 4.7:1 as text; amber fills use warning_fg on top
    "warning_fg": "#0b0d10",
    "success": "#15803d",        # 4.8:1
    # scrollbars
    "scroll_handle": "#cbd5e1",
    "scroll_handle_hover": "#94a3b8",
    # tooltips — classic inverted tooltip reads best on light chrome
    "tooltip_bg": "#0f172a",
    "tooltip_fg": "#f8fafc",
    "tooltip_border": "#0f172a",
}

#: Video surfaces and the overlays painted on top of a decoded frame.
#: Identical in both themes **by design** — see the module docstring.
#: Do not "fix" these to follow the theme.
VIDEO_TOKENS: dict[str, str] = {
    "video_bg": "#0b0d10",
    "video_surface": "#0f1115",
    "video_text": "#f1f5f9",
    "video_text_muted": "#94a3b8",
    "video_overlay_fg": "#ffffff",
    "video_name_fg": "#fef3c7",
    "video_accent": "#3b82f6",
    "video_rec": "#dc2626",
    "video_select": "#22c55e",
    "video_status_live": "#22c55e",
    "video_status_connecting": "#eab308",
    "video_status_down": "#ef4444",
    "video_status_unknown": "#6b7280",
    "video_track": "#1b2129",
    "video_track_border": "#3a424d",
    "video_progress": "#2563eb",
    "video_marker": "#ef4444",
}


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------

class ThemeManager(QObject):
    """Owns the active mode, renders the QSS, and notifies listeners."""

    #: Emitted after a successful apply with the new mode ("light"|"dark").
    changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._mode: str = DARK
        self._template: str | None = None

    # -------- state --------

    def mode(self) -> str:
        return self._mode

    def is_dark(self) -> bool:
        return self._mode == DARK

    def token(self, name: str) -> str:
        """Resolve a semantic token to a CSS color string."""
        if name in VIDEO_TOKENS:
            return VIDEO_TOKENS[name]
        table = DARK_TOKENS if self._mode == DARK else LIGHT_TOKENS
        value = table.get(name)
        if value is None:
            log.warning("[theme] unknown token %r (mode=%s)", name, self._mode)
            return _MISSING
        return value

    def color(self, name: str) -> QColor:
        return QColor(self.token(name))

    # -------- system detection --------

    def resolve_system(self) -> str:
        """Best guess at the OS color scheme.

        Qt's own hint first; it is authoritative when the platform theme
        plugin is loaded. Under headless/offscreen (and on some bare X11
        setups) it reports Unknown, so fall back to the XDG desktop portal,
        then to dark.
        """
        app = QApplication.instance()
        if app is not None:
            try:
                scheme = app.styleHints().colorScheme()
                if scheme == Qt.ColorScheme.Dark:
                    return DARK
                if scheme == Qt.ColorScheme.Light:
                    return LIGHT
            except Exception:
                log.warning("[theme] styleHints().colorScheme() failed", exc_info=True)
        portal = self._portal_scheme()
        if portal is not None:
            return portal
        return DARK

    @staticmethod
    def _portal_scheme() -> str | None:
        """org.freedesktop.appearance color-scheme: 1=dark, 2=light, 0=none.

        Hard-timeboxed and fully guarded — a missing `gdbus` or a hung
        portal must never delay application startup.
        """
        try:
            out = subprocess.run(
                [
                    "gdbus", "call", "--session",
                    "--dest", "org.freedesktop.portal.Desktop",
                    "--object-path", "/org/freedesktop/portal/desktop",
                    "--method", "org.freedesktop.portal.Settings.ReadOne",
                    "org.freedesktop.appearance", "color-scheme",
                ],
                capture_output=True, text=True, timeout=2.0,
            )
        except Exception as exc:
            log.warning("[theme] portal probe unavailable: %s", exc)
            return None
        if out.returncode != 0:
            return None
        m = re.search(r"uint32\s+(\d+)", out.stdout)
        if m is None:
            return None
        value = int(m.group(1))
        if value == 1:
            return DARK
        if value == 2:
            return LIGHT
        return None

    def initial_mode(self) -> str:
        """Persisted choice, or the OS scheme on first run."""
        stored = QSettings("BelEye", "BelEye").value(SETTINGS_KEY)
        if stored in (LIGHT, DARK):
            return str(stored)
        return self.resolve_system()

    # -------- apply --------

    def apply(self, app: QApplication, mode: str | None = None,
              *, source: str = "explicit") -> None:
        """Render the stylesheet + palette for ``mode`` onto the application."""
        mode = mode if mode in (LIGHT, DARK) else self._mode
        self._mode = mode
        tokens = self._all_tokens(mode)

        app.setStyleSheet(self._render_qss(tokens))
        app.setPalette(self._build_palette(tokens))

        # Keeps platform-drawn bits (native dialogs, some tooltips) in step
        # with our stylesheet. Qt >= 6.8 only.
        hints = app.styleHints()
        if hasattr(hints, "setColorScheme"):
            try:
                hints.setColorScheme(
                    Qt.ColorScheme.Dark if mode == DARK else Qt.ColorScheme.Light
                )
            except Exception:
                log.warning("[theme] setColorScheme failed", exc_info=True)

        log.info("[theme] applied mode=%s source=%s", mode, source)
        self.changed.emit(mode)

    def set_mode(self, mode: str, *, persist: bool = True) -> None:
        if mode not in (LIGHT, DARK):
            log.warning("[theme] refusing unknown mode %r", mode)
            return
        if persist:
            QSettings("BelEye", "BelEye").setValue(SETTINGS_KEY, mode)
        app = QApplication.instance()
        if app is None:
            log.warning("[theme] set_mode called before QApplication exists")
            self._mode = mode
            return
        self.apply(app, mode, source="user")

    def toggle(self) -> None:
        self.set_mode(LIGHT if self._mode == DARK else DARK)

    # -------- rendering --------

    def _all_tokens(self, mode: str) -> dict[str, str]:
        tokens = dict(DARK_TOKENS if mode == DARK else LIGHT_TOKENS)
        tokens.update(VIDEO_TOKENS)
        return tokens

    def _render_qss(self, tokens: dict[str, str]) -> str:
        if self._template is None:
            try:
                self._template = _QSS_TEMPLATE.read_text(encoding="utf-8")
            except OSError as exc:
                log.error("[theme] cannot read %s: %s", _QSS_TEMPLATE, exc)
                self._template = ""
        try:
            return Template(self._template).substitute(tokens)
        except (KeyError, ValueError) as exc:
            # Undefined token, or a stray dollar sign in the template. Render
            # what we can rather than leaving the whole app unstyled.
            log.error("[theme] QSS template problem: %r", exc)
            return Template(self._template).safe_substitute(tokens)

    @staticmethod
    def _build_palette(tokens: dict[str, str]) -> QPalette:
        """QSS does not reach everything Qt paints — keep a palette in sync."""
        c = lambda name: QColor(tokens[name])  # noqa: E731
        pal = QPalette()
        pal.setColor(QPalette.Window, c("bg_base"))
        pal.setColor(QPalette.WindowText, c("text_primary"))
        pal.setColor(QPalette.Base, c("bg_input"))
        pal.setColor(QPalette.AlternateBase, c("bg_elevated"))
        pal.setColor(QPalette.Text, c("text_primary"))
        pal.setColor(QPalette.Button, c("bg_hover"))
        pal.setColor(QPalette.ButtonText, c("text_primary"))
        pal.setColor(QPalette.BrightText, c("danger_strong"))
        pal.setColor(QPalette.Highlight, c("accent"))
        pal.setColor(QPalette.HighlightedText, c("accent_fg"))
        pal.setColor(QPalette.ToolTipBase, c("tooltip_bg"))
        pal.setColor(QPalette.ToolTipText, c("tooltip_fg"))
        pal.setColor(QPalette.PlaceholderText, c("text_muted"))
        pal.setColor(QPalette.Link, c("accent"))
        pal.setColor(QPalette.LinkVisited, c("accent_hover"))
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
            pal.setColor(QPalette.Disabled, role, c("text_disabled"))
        pal.setColor(QPalette.Disabled, QPalette.Base, c("bg_disabled"))
        pal.setColor(QPalette.Disabled, QPalette.Button, c("bg_disabled"))
        return pal


#: Application-wide singleton. Safe to import before QApplication exists —
#: nothing here touches the GUI until apply() is called.
theme = ThemeManager()
