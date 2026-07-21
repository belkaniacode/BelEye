"""[FIX icons] Small painted icons that do not depend on system fonts.

Emoji glyphs (👁 etc.) render as tofu boxes when the Qt font stack lacks
an emoji font — common on minimal Linux setups. These helpers paint the
needed glyphs with QPainter so they look identical everywhere.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap


def eye_icon(slashed: bool = False, size: int = 20,
             color: str = "#cbd5e1") -> QIcon:
    """An eye (password visible) or slashed eye (hidden)."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color), max(1.5, size / 12))
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    w, h = size, size
    # Eye outline: two arcs forming a lens shape.
    rect = QRectF(w * 0.1, h * 0.25, w * 0.8, h * 0.5)
    p.drawArc(rect, 0, 180 * 16)
    p.drawArc(rect, 180 * 16, 180 * 16)
    # Pupil.
    r = w * 0.13
    p.setBrush(QColor(color))
    p.drawEllipse(QPointF(w / 2, h / 2), r, r)
    if slashed:
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(w * 0.15, h * 0.85), QPointF(w * 0.85, h * 0.15))
    p.end()
    return QIcon(pix)
