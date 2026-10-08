"""Application icon, drawn in code so no image files have to be shipped."""

from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtGui import QIcon
from PySide6.QtGui import QLinearGradient
from PySide6.QtGui import QPainter
from PySide6.QtGui import QPixmap


def make_pixmap(size: int = 256) -> QPixmap:
    """Render the icon: a register grid with one updated (green) and one changed (amber) cell."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    margin = size * 0.06
    body = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    gradient = QLinearGradient(body.topLeft(), body.bottomRight())
    gradient.setColorAt(0.0, QColor("#4f8cff"))
    gradient.setColorAt(1.0, QColor("#1e40af"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(body, size * 0.22, size * 0.22)

    cell = size * 0.19
    gap = size * 0.055
    total = 3 * cell + 2 * gap
    left = (size - total) / 2
    top = (size - total) / 2
    highlights = {(0, 1): "#22c55e", (1, 2): "#f59e0b"}
    for row in range(3):
        for column in range(3):
            color = QColor(highlights.get((row, column), "#ffffff"))
            if (row, column) not in highlights:
                color.setAlphaF(0.88)
            painter.setBrush(color)
            painter.drawRoundedRect(
                QRectF(left + column * (cell + gap), top + row * (cell + gap), cell, cell),
                cell * 0.22, cell * 0.22)
    painter.end()
    return pixmap


def make_icon() -> QIcon:
    """Return the application icon with several raster sizes."""
    icon = QIcon()
    for size in (16, 32, 48, 64, 128, 256, 512):
        icon.addPixmap(make_pixmap(size))
    return icon


_small: QIcon | None = None


def small_icon() -> QIcon:
    """Return a cached icon for window title bars."""
    global _small
    if _small is None:
        _small = QIcon()
        for size in (16, 32):
            _small.addPixmap(make_pixmap(size))
    return _small
