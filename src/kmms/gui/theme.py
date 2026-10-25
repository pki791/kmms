"""Light and dark themes (palette + stylesheet) and the colors used for cell highlights."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtGui import QPainter
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QProxyStyle
from PySide6.QtWidgets import QStyle
from PySide6.QtWidgets import QStyleOptionComplex
from PySide6.QtWidgets import QStyleOptionTitleBar
from PySide6.QtWidgets import QWidget


@dataclass(frozen=True)
class Theme:
    """All colors of one theme as ``#rrggbb`` strings."""

    name: str
    window: str
    base: str
    alt_base: str
    text: str
    muted: str
    border: str
    accent: str
    accent_text: str
    button: str
    button_hover: str
    update_color: str
    change_color: str
    error_color: str
    ok_color: str


LIGHT = Theme(
    name="light", window="#f3f4f6", base="#ffffff", alt_base="#f8fafc", text="#1f2430",
    muted="#6b7280", border="#d5d9e0", accent="#2f6fed", accent_text="#ffffff",
    button="#ffffff", button_hover="#eef2fb", update_color="#22c55e", change_color="#f59e0b",
    error_color="#ef4444", ok_color="#16a34a",
)
DARK = Theme(
    name="dark", window="#1b1e24", base="#23272f", alt_base="#282c35", text="#e5e7eb",
    muted="#9097a5", border="#3a404c", accent="#5b8def", accent_text="#ffffff",
    button="#2d323c", button_hover="#363c48", update_color="#22c55e", change_color="#f59e0b",
    error_color="#ef4444", ok_color="#4ade80",
)

_current: Theme = LIGHT


class AppStyle(QProxyStyle):
    """Fusion with a crisp window title on MDI sub-windows.

    Fusion paints an active title twice - a dark copy shifted by one pixel under the white
    text - which looks blurred. Here the title is drawn once, bold while active.
    """

    def __init__(self) -> None:
        """Wrap the Fusion style."""
        super().__init__("Fusion")

    def drawComplexControl(self, control: QStyle.ComplexControl,  # noqa: N802 - Qt naming
                           option: QStyleOptionComplex, painter: QPainter,
                           widget: Optional[QWidget] = None) -> None:
        """Draw title bars without their text, then add the text once."""
        if (control != QStyle.ComplexControl.CC_TitleBar
                or not isinstance(option, QStyleOptionTitleBar) or not option.text):
            super().drawComplexControl(control, option, painter, widget)
            return
        bare = QStyleOptionTitleBar(option)
        bare.text = ""
        super().drawComplexControl(control, bare, painter, widget)
        active = bool(option.state & QStyle.StateFlag.State_Active)
        rect = self.subControlRect(control, option, QStyle.SubControl.SC_TitleBarLabel, widget)
        painter.save()
        font = painter.font()
        font.setBold(active)
        painter.setFont(font)
        role = QPalette.ColorRole.HighlightedText if active else QPalette.ColorRole.Text
        painter.setPen(option.palette.color(role))
        text = painter.fontMetrics().elidedText(option.text, Qt.TextElideMode.ElideRight,
                                                rect.width() - 14)
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.restore()


_style: Optional[AppStyle] = None


def current() -> Theme:
    """Return the theme that is currently applied."""
    return _current


def system_is_dark(app: QApplication) -> bool:
    """Return True when the operating system prefers a dark appearance."""
    scheme = app.styleHints().colorScheme()
    # Qt.ColorScheme.Dark == 2; compare by name to stay tolerant of enum differences.
    return getattr(scheme, "name", str(scheme)) == "Dark"


def resolve(mode: str, app: QApplication) -> Theme:
    """Map the preference ``system`` / ``light`` / ``dark`` to a theme."""
    if mode == "dark":
        return DARK
    if mode == "light":
        return LIGHT
    return DARK if system_is_dark(app) else LIGHT


def blend(base: QColor, top: QColor, amount: float) -> QColor:
    """Return ``base`` mixed with ``top`` (0 = base, 1 = top), always opaque."""
    amount = max(0.0, min(1.0, amount))
    return QColor(
        round(base.red() + (top.red() - base.red()) * amount),
        round(base.green() + (top.green() - base.green()) * amount),
        round(base.blue() + (top.blue() - base.blue()) * amount),
    )


def apply(app: QApplication, theme: Theme) -> None:
    """Install ``theme`` as palette and stylesheet of the application."""
    global _current, _style
    _current = theme
    if _style is None:
        _style = AppStyle()  # module reference: the application does not keep it alive
        app.setStyle(_style)
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: theme.window,
        QPalette.ColorRole.WindowText: theme.text,
        QPalette.ColorRole.Base: theme.base,
        QPalette.ColorRole.AlternateBase: theme.alt_base,
        QPalette.ColorRole.Text: theme.text,
        QPalette.ColorRole.Button: theme.button,
        QPalette.ColorRole.ButtonText: theme.text,
        QPalette.ColorRole.ToolTipBase: theme.base,
        QPalette.ColorRole.ToolTipText: theme.text,
        QPalette.ColorRole.Highlight: theme.accent,
        QPalette.ColorRole.HighlightedText: theme.accent_text,
        QPalette.ColorRole.PlaceholderText: theme.muted,
        QPalette.ColorRole.Link: theme.accent,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText,
                 QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(theme.muted))
    app.setPalette(palette)
    app.setStyleSheet(stylesheet(theme))


def stylesheet(t: Theme) -> str:
    """Return the application stylesheet for theme ``t``."""
    return f"""
    QMainWindow, QDialog {{ background: {t.window}; }}
    QToolTip {{ background: {t.base}; color: {t.text}; border: 1px solid {t.border};
               padding: 4px; }}
    QMenuBar {{ background: {t.window}; }}
    QMenuBar::item {{ padding: 4px 10px; border-radius: 4px; background: transparent; }}
    QMenuBar::item:selected {{ background: {t.button_hover}; }}
    QMenu {{ background: {t.base}; border: 1px solid {t.border}; padding: 4px; }}
    QMenu::item {{ padding: 5px 24px 5px 18px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {t.accent}; color: {t.accent_text}; }}
    QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 8px; }}
    QToolBar {{ background: {t.window}; border: none; spacing: 4px; padding: 3px; }}
    QToolBar::separator {{ width: 1px; background: {t.border}; margin: 4px 6px; }}
    QToolButton {{ padding: 5px 9px; border-radius: 6px; border: 1px solid transparent; }}
    QToolButton:hover {{ background: {t.button_hover}; border-color: {t.border}; }}
    QToolButton:pressed, QToolButton:checked {{ background: {t.border}; }}
    QPushButton {{ background: {t.button}; border: 1px solid {t.border}; border-radius: 6px;
                  padding: 5px 14px; }}
    QPushButton:hover {{ background: {t.button_hover}; }}
    QPushButton:disabled {{ color: {t.muted}; }}
    QPushButton#primary {{ background: {t.accent}; color: {t.accent_text}; border-color: {t.accent};
                          font-weight: 600; }}
    QPushButton#primary:hover {{ background: {t.accent}; }}
    QPushButton#danger {{ background: {t.error_color}; color: white; border-color: {t.error_color};
                         font-weight: 600; }}
    QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
        background: {t.base}; border: 1px solid {t.border}; border-radius: 5px; padding: 3px 6px;
        selection-background-color: {t.accent}; selection-color: {t.accent_text}; }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {t.accent}; }}
    QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {t.muted};
        background: {t.window}; }}
    QComboBox QAbstractItemView {{ background: {t.base}; border: 1px solid {t.border};
        selection-background-color: {t.accent}; selection-color: {t.accent_text}; }}
    QTableView {{ background: {t.base}; border: 1px solid {t.border}; border-radius: 4px;
                 gridline-color: {t.border}; selection-background-color: {t.accent};
                 selection-color: {t.accent_text}; }}
    QHeaderView::section {{ background: {t.window}; color: {t.muted}; padding: 4px 8px;
        border: none; border-bottom: 1px solid {t.border}; border-right: 1px solid {t.border};
        font-weight: 600; }}
    QTableCornerButton::section {{ background: {t.window}; border: none; }}
    QStatusBar {{ background: {t.window}; border-top: 1px solid {t.border}; }}
    QStatusBar::item {{ border: none; }}
    QDockWidget {{ font-weight: 600; }}
    QDockWidget::title {{ background: {t.window}; padding: 6px; border-bottom: 1px solid
        {t.border}; }}
    QMdiArea {{ background: {t.window}; }}
    QMdiSubWindow {{ background: {t.base}; }}
    QTabBar::tab {{ padding: 6px 14px; border: 1px solid {t.border}; border-bottom: none;
                   background: {t.window}; border-top-left-radius: 6px;
                   border-top-right-radius: 6px; }}
    QTabBar::tab:selected {{ background: {t.base}; }}
    QScrollBar:vertical {{ width: 12px; background: transparent; margin: 0; }}
    QScrollBar:horizontal {{ height: 12px; background: transparent; margin: 0; }}
    QScrollBar::handle {{ background: {t.border}; border-radius: 5px; min-height: 24px;
                         min-width: 24px; }}
    QScrollBar::handle:hover {{ background: {t.muted}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QGroupBox {{ border: 1px solid {t.border}; border-radius: 6px; margin-top: 10px;
                padding: 10px 8px 6px 8px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {t.muted}; }}
    QLabel#muted {{ color: {t.muted}; }}
    QFrame#setHeader {{ background: {t.window}; border-bottom: 1px solid {t.border}; }}
    QFrame#setFooter {{ background: {t.window}; border-top: 1px solid {t.border}; }}
    QFrame#connectionBar {{ background: {t.base}; border: 1px solid {t.border};
                           border-radius: 8px; }}
    """
