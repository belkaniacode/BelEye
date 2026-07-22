"""Painted icons — no system fonts, no image assets, theme-aware.

Two families live here:

* ``svg_icon(name, …)`` — Lucide-style stroke icons rendered from inline SVG;
* ``eye_icon(slashed)`` — the password show/hide glyph, painted directly.

Emoji glyphs (👁, ⚙, …) render as tofu boxes when the Qt font stack lacks an
emoji font, which is common on minimal Linux setups — that is why nothing
here depends on a font.

**A QIcon bakes its color into a pixmap and cannot re-tint itself.** Every
caller must therefore rebuild its icons when ``theme.changed`` fires, or the
light theme ends up with light-grey icons on a white toolbar.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .theme import theme

# Lucide-style icons embedded as SVG strings. 24x24 viewBox, stroke-based,
# 2 px stroke, round line caps/joins. Rendered via QSvgRenderer into a
# QPixmap so they stay crisp at any size and look identical across platforms.
_ICON_SVGS: dict[str, str] = {
    "settings": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"/>'
        '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9 1.65 1.65 0 0 0 4.27 7.18l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/>'
        '</svg>'
    ),
    "refresh": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<polyline points="23 4 23 10 17 10"/>'
        '<path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>'
        '</svg>'
    ),
    "grid": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<rect x="3"  y="3"  width="7" height="7" rx="1"/>'
        '<rect x="14" y="3"  width="7" height="7" rx="1"/>'
        '<rect x="3"  y="14" width="7" height="7" rx="1"/>'
        '<rect x="14" y="14" width="7" height="7" rx="1"/>'
        '</svg>'
    ),
    "reorder": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M7 3v18"/>'
        '<polyline points="3 7 7 3 11 7"/>'
        '<path d="M17 21V3"/>'
        '<polyline points="21 17 17 21 13 17"/>'
        '</svg>'
    ),
    "fullscreen": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M8 3H5a2 2 0 0 0-2 2v3"/>'
        '<path d="M21 8V5a2 2 0 0 0-2-2h-3"/>'
        '<path d="M3 16v3a2 2 0 0 0 2 2h3"/>'
        '<path d="M16 21h3a2 2 0 0 0 2-2v-3"/>'
        '</svg>'
    ),
    # Theme toggle. The icon shows the theme you will GET, not the one
    # you are in: moon while light, sun while dark.
    "moon": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>'
        '</svg>'
    ),
    "sun": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
        ' stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="12" cy="12" r="4"/>'
        '<path d="M12 2v2"/><path d="M12 20v2"/>'
        '<path d="M2 12h2"/><path d="M20 12h2"/>'
        '<path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/>'
        '<path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>'
        '</svg>'
    ),
}


def svg_icon(name: str, size: int = 14, color: str | None = None,
             right_pad: int = 8) -> QIcon:
    """Lucide SVG → QIcon.

    ``right_pad`` adds transparent space to the right of the icon, baked into
    the pixmap. This is how we get a real gap between the icon and a toolbar
    label — Qt's ToolButtonTextBesideIcon exposes no icon-text spacing.

    ``color`` defaults to the current theme's secondary text color, resolved
    *per call* on purpose: a module-level default would freeze whichever
    theme happened to be active at import time.
    """
    svg_template = _ICON_SVGS.get(name)
    if svg_template is None:
        return QIcon()
    svg = svg_template.format(color=color or theme.token("text_secondary"))
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    scale = 2
    total_w = (size + right_pad) * scale
    total_h = size * scale
    pm = QPixmap(total_w, total_h)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    # Render the SVG into the left "size × size" square; the right side
    # stays transparent, becoming the icon-text gap.
    renderer.render(p, QRectF(0, 0, size * scale, size * scale))
    p.end()
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


def eye_icon(slashed: bool = False, size: int = 20,
             color: str | None = None) -> QIcon:
    """An eye (password visible) or slashed eye (hidden)."""
    stroke = QColor(color or theme.token("text_secondary"))
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(QPen(stroke, max(1.5, size / 12)))
    p.setBrush(Qt.NoBrush)

    w, h = size, size
    # Eye outline: two arcs forming a lens shape.
    rect = QRectF(w * 0.1, h * 0.25, w * 0.8, h * 0.5)
    p.drawArc(rect, 0, 180 * 16)
    p.drawArc(rect, 180 * 16, 180 * 16)
    # Pupil.
    r = w * 0.13
    p.setBrush(stroke)
    p.drawEllipse(QPointF(w / 2, h / 2), r, r)
    if slashed:
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(w * 0.15, h * 0.85), QPointF(w * 0.85, h * 0.15))
    p.end()
    return QIcon(pix)
