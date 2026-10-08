"""Dockable traffic log showing raw TX/RX frames and application messages."""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Iterable
from typing import Optional

from PySide6.QtGui import QColor
from PySide6.QtGui import QFont
from PySide6.QtGui import QFontDatabase
from PySide6.QtGui import QSyntaxHighlighter
from PySide6.QtGui import QTextCharFormat
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QCheckBox
from PySide6.QtWidgets import QFileDialog
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QMessageBox
from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget

from kmms.engine import TraceEvent
from kmms.gui import theme
from kmms.protocol import format_hex

LOG = logging.getLogger(__name__)

MAX_LINES = 20_000
KIND_TX = "TX"
KIND_RX = "RX"
KIND_INFO = "INFO"
KIND_ERROR = "ERR"


def format_entry(timestamp: float, kind: str, text: str) -> str:
    """Return one log line: ``HH:MM:SS.mmm  KIND  text``."""
    stamp = time.strftime("%H:%M:%S", time.localtime(timestamp))
    millis = int((timestamp % 1) * 1000)
    return f"{stamp}.{millis:03d}  {kind:<4} {text}"


class LogHighlighter(QSyntaxHighlighter):
    """Colors lines by their kind column."""

    def __init__(self, document: QTextDocument) -> None:
        """Attach to ``document``."""
        super().__init__(document)
        self.refresh_colors()

    def refresh_colors(self) -> None:
        """Pick colors that fit the current theme."""
        colors = theme.current()
        dark = colors.name == "dark"
        self._formats = {
            KIND_TX: self._fmt("#7fb2ff" if dark else "#1d4ed8"),
            KIND_RX: self._fmt("#4ade80" if dark else "#15803d"),
            KIND_ERROR: self._fmt(colors.error_color),
            KIND_INFO: self._fmt(colors.muted),
        }
        self.rehighlight()

    @staticmethod
    def _fmt(color: str) -> QTextCharFormat:
        """Create a char format with a foreground color."""
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        return fmt

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt naming
        """Color the whole line according to the kind column (characters 14-18)."""
        kind = text[14:19].strip()
        fmt = self._formats.get(kind)
        if fmt is not None:
            self.setFormat(0, len(text), fmt)


class TrafficLog(QWidget):
    """Keeps a bounded history of frames and shows it in a read-only text view."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """Build the log view and its toolbar."""
        super().__init__(parent)
        self._entries: deque[str] = deque(maxlen=MAX_LINES)
        self._paused = False

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setMaximumBlockCount(MAX_LINES)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSizeF(max(font.pointSizeF() - 1, 9.0))
        self.view.setFont(QFont(font))
        self.highlighter = LogHighlighter(self.view.document())

        self.pause = QCheckBox("Pause")
        self.autoscroll = QCheckBox("Auto-scroll")
        self.autoscroll.setChecked(True)
        clear = QPushButton("Clear")
        save = QPushButton("Save...")
        self.pause.toggled.connect(self._pause_toggled)
        clear.clicked.connect(self.clear)
        save.clicked.connect(self.save_to_file)

        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 0)
        bar.addWidget(self.pause)
        bar.addWidget(self.autoscroll)
        bar.addStretch(1)
        bar.addWidget(clear)
        bar.addWidget(save)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(bar)
        layout.addWidget(self.view, 1)

    # ------------------------------------------------------------------ input

    def add_trace(self, events: Iterable[TraceEvent]) -> None:
        """Append raw frames."""
        lines = [format_entry(e.timestamp, e.direction, format_hex(e.data)) for e in events]
        self._append(lines)

    def add_info(self, text: str, error: bool = False) -> None:
        """Append an application message."""
        kind = KIND_ERROR if error else KIND_INFO
        self._append([format_entry(time.time(), kind, text)])

    def _append(self, lines: list[str]) -> None:
        """Store lines and show them unless paused."""
        if not lines:
            return
        self._entries.extend(lines)
        if self._paused:
            return
        bar = self.view.verticalScrollBar()
        position = bar.value()
        self.view.appendPlainText("\n".join(lines))
        if self.autoscroll.isChecked():
            bar.setValue(bar.maximum())
        else:
            bar.setValue(position)

    # ------------------------------------------------------------------ actions

    def _pause_toggled(self, paused: bool) -> None:
        """When resuming, show everything that arrived meanwhile."""
        self._paused = paused
        if not paused:
            self.view.setPlainText("\n".join(self._entries))
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def clear(self) -> None:
        """Remove all entries."""
        self._entries.clear()
        self.view.clear()

    def text(self) -> str:
        """Return the complete stored log as text."""
        return "\n".join(self._entries)

    def save_to_file(self) -> None:
        """Ask for a file name and write the log."""
        path, _ = QFileDialog.getSaveFileName(self, "Save traffic log", "kmms-traffic.log",
                                              "Log files (*.log *.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.text() + "\n")
        except OSError as exc:
            LOG.error("Could not save log to %s: %s", path, exc)
            QMessageBox.warning(self, "Save failed", f"Could not write the log:\n{exc}")

    def refresh_theme(self) -> None:
        """Recolor after a theme change."""
        self.highlighter.refresh_colors()
