"""The content of one polling set window: header, register table and counters."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QModelIndex
from PySide6.QtCore import Qt
from PySide6.QtCore import QTimer
from PySide6.QtCore import Signal
from PySide6.QtGui import QAction
from PySide6.QtGui import QGuiApplication
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QHeaderView
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QMenu
from PySide6.QtWidgets import QTableView
from PySide6.QtWidgets import QToolButton
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget

from kmms.engine import PollResult
from kmms.gui import theme
from kmms.gui.register_model import DEFAULT_CHANGE_S
from kmms.gui.register_model import DEFAULT_FLASH_S
from kmms.gui.register_model import RegisterTableModel
from kmms.model import PollSet
from kmms.stats import Counters

ANIMATION_INTERVAL_MS = 33


class RegisterTable(QTableView):
    """Table view with Ctrl+C support (tab separated text)."""

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Copy the selection on Ctrl+C / Cmd+C."""
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            return
        super().keyPressEvent(event)

    def copy_selection(self) -> None:
        """Put the selected cells on the clipboard as TSV."""
        indexes = sorted(self.selectedIndexes(), key=lambda i: (i.row(), i.column()))
        if not indexes:
            return
        lines: list[str] = []
        current_row = indexes[0].row()
        cells: list[str] = []
        for index in indexes:
            if index.row() != current_row:
                lines.append("\t".join(cells))
                cells = []
                current_row = index.row()
            cells.append(str(index.data(Qt.ItemDataRole.DisplayRole) or ""))
        lines.append("\t".join(cells))
        QGuiApplication.clipboard().setText("\n".join(lines))


class SetPanel(QWidget):
    """Shows the live values of one :class:`PollSet`."""

    edit_requested = Signal(str)
    toggle_requested = Signal(str)
    write_dialog_requested = Signal(str)
    duplicate_requested = Signal(str)
    remove_requested = Signal(str)
    #: (set id, first address, list of register words or bit values)
    write_requested = Signal(str, int, list)
    message = Signal(str, bool)
    counters_changed = Signal()

    def __init__(self, poll_set: PollSet, parent: Optional[QWidget] = None) -> None:
        """Create the panel for ``poll_set``."""
        super().__init__(parent)
        self.poll_set = poll_set
        self.counters = Counters()
        self._connected = False
        self._last_ok: Optional[bool] = None
        self._flash_pref = DEFAULT_FLASH_S
        self._change_pref = DEFAULT_CHANGE_S

        self.model = RegisterTableModel(poll_set, self)
        self.table = RegisterTable()
        self.table.setModel(self.model)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(72)
        self.table.setShowGrid(True)
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.doubleClicked.connect(self._double_clicked)
        self.model.write_requested.connect(self._model_write)
        self.model.edit_failed.connect(lambda text: self.message.emit(text, True))

        # header
        self.dot = QLabel()
        self.dot.setFixedSize(12, 12)
        self.title = QLabel()
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        self.subtitle = QLabel()
        self.subtitle.setObjectName("muted")
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        self.toggle_button = self._button("Pause", "Pause or resume polling of this set")
        self.edit_button = self._button("Edit...", "Change address, timing and display")
        self.write_button = self._button("Write...", "Write values to the slave")
        self.more_button = self._button("⋯", "More actions")
        menu = QMenu(self.more_button)
        duplicate = menu.addAction("Duplicate set")
        remove = menu.addAction("Remove set")
        copy = menu.addAction("Copy selection")
        self.more_button.setMenu(menu)
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        duplicate.triggered.connect(lambda: self.duplicate_requested.emit(self.poll_set.id))
        remove.triggered.connect(lambda: self.remove_requested.emit(self.poll_set.id))
        copy.triggered.connect(self.table.copy_selection)
        self.toggle_button.clicked.connect(lambda: self.toggle_requested.emit(self.poll_set.id))
        self.edit_button.clicked.connect(lambda: self.edit_requested.emit(self.poll_set.id))
        self.write_button.clicked.connect(
            lambda: self.write_dialog_requested.emit(self.poll_set.id))

        header = QFrame()
        header.setObjectName("setHeader")
        hlayout = QHBoxLayout(header)
        hlayout.setContentsMargins(10, 6, 8, 6)
        hlayout.addWidget(self.dot)
        hlayout.addLayout(titles, 1)
        for button in (self.toggle_button, self.edit_button, self.write_button,
                       self.more_button):
            hlayout.addWidget(button)

        # footer
        self.ok_label = QLabel()
        self.error_label = QLabel()
        self.detail_label = QLabel()
        self.detail_label.setObjectName("muted")
        footer = QFrame()
        footer.setObjectName("setFooter")
        flayout = QHBoxLayout(footer)
        flayout.setContentsMargins(10, 4, 10, 4)
        flayout.setSpacing(14)
        flayout.addWidget(self.ok_label)
        flayout.addWidget(self.error_label)
        flayout.addWidget(self.detail_label, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self.table, 1)
        layout.addWidget(footer)

        self._timer = QTimer(self)
        self._timer.setInterval(ANIMATION_INTERVAL_MS)
        self._timer.timeout.connect(self._animate)

        self.set_definition(poll_set)

    def _button(self, text: str, tip: str) -> QToolButton:
        """Create a flat header button."""
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tip)
        button.setAutoRaise(True)
        return button

    # ------------------------------------------------------------------ definition

    def set_definition(self, poll_set: PollSet) -> None:
        """Apply new set parameters (columns, range, timing) and refresh the header."""
        rebuild = (
            poll_set.start != self.poll_set.start
            or poll_set.length != self.poll_set.length
            or poll_set.function != self.poll_set.function
            or poll_set.slave != self.poll_set.slave
            or poll_set.columns != self.poll_set.columns
            or poll_set.byte_swap != self.poll_set.byte_swap
            or poll_set.word_swap != self.poll_set.word_swap
        )
        self.poll_set = poll_set
        self._apply_timing()
        if rebuild:
            self.model.configure(poll_set)
            self._last_ok = None
        else:
            self.model.relabel(poll_set)  # same shape: keep values, labels may change
        self.title.setText(poll_set.name)
        self.subtitle.setText(self.model.describe())
        self.toggle_button.setText("Pause" if poll_set.enabled else "Resume")
        self.write_button.setVisible(poll_set.is_writable)
        self._refresh_status()
        self._refresh_counters()

    def set_highlight_times(self, flash_s: float, change_s: float) -> None:
        """Set the user preferred durations of the green flash and the amber change mark."""
        self._flash_pref = flash_s
        self._change_pref = change_s
        self._apply_timing()

    def _apply_timing(self) -> None:
        """Flash no longer than 60 % of the poll interval so fast polls still visibly pulse."""
        flash = min(self._flash_pref, max(0.08, self.poll_set.interval_ms / 1000.0 * 0.6))
        self.model.set_timing(flash, self._change_pref)

    def set_connection_state(self, connected: bool) -> None:
        """Enable cell editing only on an open connection."""
        self._connected = connected
        self.model.set_write_enabled(connected)
        if not connected:
            self.model.mark_idle()
            self._last_ok = None
        self._refresh_status()

    # ------------------------------------------------------------------ results

    def apply_result(self, result: PollResult) -> None:
        """Show the outcome of a poll and update the counters."""
        if result.ok and len(result.values) != self.poll_set.length:
            return  # belongs to an earlier definition of this set
        self.counters.record(result.ok, result.duration_ms, result.error, result.error_kind)
        self._last_ok = result.ok
        if result.ok:
            animate = self.model.apply_values(result.values)
        else:
            animate = self.model.apply_error()
        if animate and not self._timer.isActive():
            self._timer.start()
        self._refresh_status()
        self._refresh_counters()
        self.counters_changed.emit()

    def apply_connection_error(self) -> None:
        """The link was lost: blink red like a failed read, without counting it."""
        self._last_ok = False
        if self.model.apply_error() and not self._timer.isActive():
            self._timer.start()
        self._refresh_status()

    def reset_counters(self) -> None:
        """Zero the counters."""
        self.counters.reset()
        self._refresh_counters()

    def _animate(self) -> None:
        """Timer slot: advance highlight animation, stop when finished."""
        if not self.model.tick():
            self._timer.stop()

    # ------------------------------------------------------------------ status display

    def _refresh_status(self) -> None:
        """Update the status dot."""
        colors = theme.current()
        if not self.poll_set.enabled:
            color, tip = colors.muted, "Polling paused"
        elif not self._connected:
            color, tip = colors.muted, "Not connected"
        elif self._last_ok is None:
            color, tip = colors.change_color, "Waiting for first response"
        elif self._last_ok:
            color, tip = colors.ok_color, "Last read succeeded"
        else:
            color, tip = colors.error_color, self.counters.last_error or "Last read failed"
        self.dot.setStyleSheet(f"background: {color}; border-radius: 6px;")
        self.dot.setToolTip(tip)

    def _refresh_counters(self) -> None:
        """Update the footer."""
        colors = theme.current()
        c = self.counters
        self.ok_label.setText(f"<span style='color:{colors.ok_color}'>✔</span> {c.ok:,}")
        error_color = colors.error_color if c.errors else colors.muted
        self.error_label.setText(
            f"<span style='color:{error_color}'>✖</span> {c.errors:,}")
        self.ok_label.setToolTip("Successful reads")
        self.error_label.setToolTip(f"Failed reads ({c.timeouts:,} timeouts)")
        if c.last_error:
            text = c.last_error
        elif c.ok:
            text = f"{c.last_ms:.0f} ms"
        else:
            text = ""
        self.detail_label.setText(text)
        self.detail_label.setToolTip(text)

    def refresh_theme(self) -> None:
        """Re-apply colors after a theme change."""
        self._refresh_status()
        self._refresh_counters()
        self.model.tick()

    # ------------------------------------------------------------------ editing / writing

    def _model_write(self, address: int, words: list) -> None:
        """Forward a cell edit to the main window."""
        self.write_requested.emit(self.poll_set.id, address, words)

    def _double_clicked(self, index: QModelIndex) -> None:
        """Toggle a coil on double click (registers use the in-place editor)."""
        if not (self.poll_set.is_bit_set and self.poll_set.is_writable):
            return
        if not self._connected:
            self.message.emit("Open the connection to write", True)
            return
        row = index.row()
        current = self.model._values[row]
        if current is None:
            return
        self.write_requested.emit(self.poll_set.id, self.poll_set.start + row,
                                  [0 if current else 1])

    def current_address(self) -> int:
        """Return the address of the current row (or the set's start address)."""
        index = self.table.currentIndex()
        row = index.row() if index.isValid() else 0
        return self.poll_set.start + row

    def _table_menu(self, position) -> None:
        """Context menu of the table."""
        menu = QMenu(self)
        copy = QAction("Copy", menu)
        copy.triggered.connect(self.table.copy_selection)
        menu.addAction(copy)
        index = self.table.indexAt(position)
        if index.isValid() and self.poll_set.is_writable and self._connected:
            write = QAction("Write here...", menu)
            write.triggered.connect(lambda: self.write_dialog_requested.emit(self.poll_set.id))
            menu.addAction(write)
        menu.exec(self.table.viewport().mapToGlobal(position))
