"""Table model for the registers of one polling set, including the highlight animation."""

from __future__ import annotations

import time
from typing import Any
from typing import Optional

from PySide6.QtCore import QAbstractTableModel
from PySide6.QtCore import QModelIndex
from PySide6.QtCore import QPersistentModelIndex
from PySide6.QtCore import Qt
from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtGui import QFont
from PySide6.QtGui import QFontDatabase

from kmms import formats
from kmms.gui import theme
from kmms.model import PollSet

ADDRESS_COLUMN = "addr"
VALUE_COLUMN = "value"
STATE_COLUMN = "state"

#: Defaults for the highlight timing (seconds); the UI can override them.
DEFAULT_FLASH_S = 0.35
DEFAULT_CHANGE_S = 1.5
ERROR_BLINK_S = 0.8
#: Strength of the permanent tint while a set is in error (0..1).
STALE_TINT = 0.12
#: Peak strength of the green flash / amber change / red error highlight.
UPDATE_ALPHA = 0.55
CHANGE_ALPHA = 0.85
ERROR_ALPHA = 0.7

_NEVER = float("-inf")


class RegisterTableModel(QAbstractTableModel):
    """Rows are registers (or bits); columns are address plus the chosen value formats."""

    #: Emitted when the user edited a cell: (first address, list of register words).
    write_requested = Signal(int, list)
    #: Emitted when an edit could not be parsed.
    edit_failed = Signal(str)

    def __init__(self, poll_set: PollSet, parent: Any = None) -> None:
        """Create the model for ``poll_set``."""
        super().__init__(parent)
        self._mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self._flash_s = DEFAULT_FLASH_S
        self._change_s = DEFAULT_CHANGE_S
        self._error_blink = True
        self._write_enabled = False
        self._columns: list[str] = []
        self._values: list[Optional[int]] = []
        self._updated_at: list[float] = []
        self._changed_at: list[float] = []
        self._error_at = _NEVER
        self._stale = False
        self._anim_until = 0.0
        self._now = time.monotonic()
        self._set = poll_set
        self.configure(poll_set)

    # ------------------------------------------------------------------ configuration

    def configure(self, poll_set: PollSet) -> None:
        """(Re)build rows and columns from ``poll_set``; values are cleared."""
        self.beginResetModel()
        self._set = poll_set
        if poll_set.is_bit_set:
            self._columns = [ADDRESS_COLUMN, VALUE_COLUMN, STATE_COLUMN]
        else:
            keys = formats.known_columns(poll_set.columns) or ["unsigned"]
            self._columns = [ADDRESS_COLUMN] + keys
        count = poll_set.length
        self._values = [None] * count
        self._updated_at = [_NEVER] * count
        self._changed_at = [_NEVER] * count
        self._error_at = _NEVER
        self._stale = False
        self._anim_until = 0.0
        self.endResetModel()

    def relabel(self, poll_set: PollSet) -> None:
        """Adopt a definition with the same shape (name/timing changed) and keep the values."""
        self._set = poll_set

    def set_timing(self, flash_s: float, change_s: float, error_blink: bool = True) -> None:
        """Change the highlight durations; 0 disables a highlight.

        Without ``error_blink`` a failed read only applies the faint red tint.
        """
        self._flash_s = flash_s
        self._change_s = change_s
        self._error_blink = error_blink

    def set_write_enabled(self, enabled: bool) -> None:
        """Allow or forbid editing cells (writes need an open connection)."""
        self._write_enabled = enabled

    @property
    def columns(self) -> list[str]:
        """Return the column keys in display order."""
        return list(self._columns)

    def column_key(self, column: int) -> str:
        """Return the key of ``column``."""
        return self._columns[column]

    # ------------------------------------------------------------------ data input

    def apply_values(self, values: list[int], now: Optional[float] = None) -> bool:
        """Store a successful poll. Returns True when animations should run."""
        now = time.monotonic() if now is None else now
        if len(values) != len(self._values):
            return False
        first = all(v is None for v in self._values)
        for row, new in enumerate(values):
            old = self._values[row]
            if not first and old != new:
                self._changed_at[row] = now
            self._updated_at[row] = now
            self._values[row] = new
        self._stale = False
        self._anim_until = max(self._anim_until, now + max(self._flash_s, self._change_s))
        self._now = now
        self._emit_all()
        return self._anim_until > now

    def apply_error(self, now: Optional[float] = None) -> bool:
        """Mark the set as failing (red blink, then a permanent faint red tint)."""
        now = time.monotonic() if now is None else now
        self._error_at = now
        self._stale = True
        if self._error_blink:
            self._anim_until = max(self._anim_until, now + ERROR_BLINK_S)
        self._now = now
        self._emit_all()
        return self._anim_until > now

    def mark_idle(self) -> None:
        """Clear the error tint (connection closed or set paused)."""
        if self._stale:
            self._stale = False
            self._emit_all()

    def tick(self, now: Optional[float] = None) -> bool:
        """Advance the animation. Returns True while another tick is needed."""
        self._now = time.monotonic() if now is None else now
        self._emit_all()
        return self._now < self._anim_until

    def has_values(self) -> bool:
        """Return True once at least one poll succeeded."""
        return any(v is not None for v in self._values)

    def _emit_all(self) -> None:
        """Ask views to repaint every value cell."""
        if not self._values:
            return
        top_left = self.index(0, 0)
        bottom_right = self.index(len(self._values) - 1, len(self._columns) - 1)
        self.dataChanged.emit(top_left, bottom_right)

    # ------------------------------------------------------------------ model interface

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """Return the number of registers."""
        return 0 if parent.isValid() else len(self._values)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        """Return the number of columns."""
        return 0 if parent.isValid() else len(self._columns)

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        """Return column titles; rows are numbered by the address column instead."""
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            key = self._columns[section]
            if key == ADDRESS_COLUMN:
                return "Address"
            if key == VALUE_COLUMN:
                return "Value"
            if key == STATE_COLUMN:
                return "State"
            return formats.FORMATS_BY_KEY[key].title
        if role == Qt.ItemDataRole.ToolTipRole and orientation == Qt.Orientation.Horizontal:
            key = self._columns[section]
            if key in formats.FORMATS_BY_KEY and formats.FORMATS_BY_KEY[key].words > 1:
                words = formats.FORMATS_BY_KEY[key].words
                return f"Combines {words} consecutive registers starting at each row"
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        """Cells are selectable; value cells are editable when writing is possible."""
        base = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        key = self._columns[index.column()]
        if (self._write_enabled and self._set.is_writable and not self._set.is_bit_set
                and key in formats.FORMATS_BY_KEY):
            words = formats.FORMATS_BY_KEY[key].words
            if index.row() + words <= len(self._values):
                return base | Qt.ItemFlag.ItemIsEditable
        return base

    def _words_at(self, row: int, count: int) -> Optional[list[int]]:
        """Return ``count`` register values starting at ``row`` or None if unavailable."""
        chunk = self._values[row : row + count]
        if len(chunk) < count or any(v is None for v in chunk):
            return None
        return chunk  # type: ignore[return-value]

    def data(self, index: QModelIndex | QPersistentModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        """Return display text, colors, fonts and tooltips for a cell."""
        if not index.isValid():
            return None
        row, key = index.row(), self._columns[index.column()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self._text(row, key)
        if role == Qt.ItemDataRole.BackgroundRole:
            return self._background(row, key)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._foreground(row, key)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if key in (ADDRESS_COLUMN, STATE_COLUMN, VALUE_COLUMN):
                return int(Qt.AlignmentFlag.AlignCenter)
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.FontRole:
            return self._font(key)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(row, key)
        return None

    def _text(self, row: int, key: str) -> str:
        """Return the text of one cell."""
        if key == ADDRESS_COLUMN:
            return str(self._set.start + row)
        value = self._values[row]
        if key == VALUE_COLUMN:
            return "" if value is None else str(value)
        if key == STATE_COLUMN:
            return "" if value is None else ("ON" if value else "OFF")
        width = formats.FORMATS_BY_KEY[key].words
        words = self._words_at(row, width)
        if words is None:
            return ""
        return formats.format_value(key, words, self._set.byte_swap, self._set.word_swap)

    def _font(self, key: str) -> QFont:
        """Monospace for values, normal font for the address column."""
        font = QFont(self._mono)
        font.setPointSizeF(max(font.pointSizeF(), 11.0))
        if key == STATE_COLUMN:
            font.setBold(True)
        return font

    def _tooltip(self, row: int, key: str) -> str:
        """Hover help: addresses in several notations, raw words for values."""
        address = self._set.start + row
        notation = formats.modicon_address(self._set.function, address)
        head = f"Address {address} (0x{address:04X}) - {notation}"
        if key == ADDRESS_COLUMN or self._values[row] is None:
            return head
        width = 1 if key in (VALUE_COLUMN, STATE_COLUMN) else formats.FORMATS_BY_KEY[key].words
        words = self._words_at(row, width)
        if words is None:
            return head
        raw = " ".join(f"{w:04X}" for w in words) if not self._set.is_bit_set else str(words[0])
        return f"{head}\nRaw: {raw}"

    # ------------------------------------------------------------------ colors

    def _foreground(self, row: int, key: str) -> Optional[QColor]:
        """Dim values while the set is failing; make ON states green."""
        colors = theme.current()
        if key == ADDRESS_COLUMN:
            return QColor(colors.muted)
        if self._stale:
            return QColor(colors.muted)
        if key == STATE_COLUMN and self._values[row]:
            return QColor(colors.ok_color)
        return None

    def _background(self, row: int, key: str) -> Optional[QColor]:
        """Compute the highlight color for the current animation time."""
        colors = theme.current()
        if key == ADDRESS_COLUMN:
            return None
        span = 1
        if key in formats.FORMATS_BY_KEY:
            span = formats.FORMATS_BY_KEY[key].words
        last = min(row + span, len(self._values))
        now = self._now
        base = QColor(colors.base)
        result = base
        if self._stale:
            blink = 0.0
            if self._error_blink:
                blink = max(0.0, 1.0 - (now - self._error_at) / ERROR_BLINK_S)
            amount = STALE_TINT + (ERROR_ALPHA - STALE_TINT) * blink
            return theme.blend(base, QColor(colors.error_color), amount)
        updated = max(self._updated_at[row:last], default=_NEVER)
        changed = max(self._changed_at[row:last], default=_NEVER)
        flash = max(0.0, 1.0 - (now - updated) / self._flash_s) if self._flash_s > 0 else 0.0
        change = max(0.0, 1.0 - (now - changed) / self._change_s) if self._change_s > 0 else 0.0
        if flash > 0:
            result = theme.blend(result, QColor(colors.update_color), flash * UPDATE_ALPHA)
        if change > 0:
            result = theme.blend(result, QColor(colors.change_color), change * CHANGE_ALPHA)
        return result if result != base else None

    # ------------------------------------------------------------------ editing

    def setData(self, index: QModelIndex | QPersistentModelIndex, value: Any,
                role: int = Qt.ItemDataRole.EditRole) -> bool:
        """Parse the edited text and request a write; the table itself updates on re-poll."""
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        key = self._columns[index.column()]
        if key not in formats.FORMATS_BY_KEY:
            return False
        try:
            words = formats.parse_value(key, str(value), self._set.byte_swap,
                                        self._set.word_swap)
        except (ValueError, UnicodeEncodeError) as exc:
            self.edit_failed.emit(f"Invalid value: {exc}")
            return False
        self.write_requested.emit(self._set.start + index.row(), words)
        return False

    def describe(self) -> str:
        """Return a compact title such as ``Slave 1 · FC03 · 0–9``."""
        s = self._set
        last = s.start + s.length - 1
        return f"Slave {s.slave} \u00b7 FC{s.function:02d} \u00b7 {s.start}\u2013{last}"
