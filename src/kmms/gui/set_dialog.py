"""Dialog to create or edit a polling set."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QCheckBox
from PySide6.QtWidgets import QComboBox
from PySide6.QtWidgets import QDialog
from PySide6.QtWidgets import QDialogButtonBox
from PySide6.QtWidgets import QFormLayout
from PySide6.QtWidgets import QGridLayout
from PySide6.QtWidgets import QGroupBox
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QLineEdit
from PySide6.QtWidgets import QSpinBox
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget

from kmms import formats
from kmms.model import PollSet
from kmms.protocol import FUNCTION_LABELS
from kmms.protocol import READ_FUNCTIONS


def _ms_spin(low: int, high: int, value: int, step: int = 100) -> QSpinBox:
    """Create a millisecond spin box."""
    spin = QSpinBox()
    spin.setRange(low, high)
    spin.setSingleStep(step)
    spin.setSuffix(" ms")
    spin.setValue(value)
    spin.setGroupSeparatorShown(True)
    return spin


class PollSetDialog(QDialog):
    """Edit all properties of a :class:`PollSet`."""

    def __init__(self, poll_set: PollSet, parent: Optional[QWidget] = None,
                 title: str = "Polling set") -> None:
        """Populate the dialog from ``poll_set`` (which is not modified until accepted)."""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self._original = poll_set

        self.name = QLineEdit(poll_set.name)
        self.name.setPlaceholderText("e.g. Boiler temperatures")
        self.slave = QSpinBox()
        self.slave.setRange(0, 255)
        self.slave.setValue(poll_set.slave)
        self.function = QComboBox()
        for code in READ_FUNCTIONS:
            self.function.addItem(FUNCTION_LABELS[code], int(code))
        self.function.setCurrentIndex(max(self.function.findData(poll_set.function), 0))
        self.start = QSpinBox()
        self.start.setRange(0, 65535)
        self.start.setValue(poll_set.start)
        self.length = QSpinBox()
        self.length.setRange(1, 65536)
        self.length.setValue(poll_set.length)
        self.start_hint = QLabel()
        self.start_hint.setObjectName("muted")

        what = QGroupBox("What to read")
        form = QFormLayout(what)
        form.addRow("Name", self.name)
        form.addRow("Slave address", self.slave)
        form.addRow("Function", self.function)
        form.addRow("Start address", self.start)
        form.addRow("", self.start_hint)
        form.addRow("Length", self.length)

        self.interval = _ms_spin(0, 3_600_000, poll_set.interval_ms)
        self.timeout = _ms_spin(10, 600_000, poll_set.timeout_ms)
        self.pause = _ms_spin(0, 3_600_000, poll_set.error_pause_ms)
        self.interval.setToolTip("Time between the start of two polls. 0 = as fast as the slave "
                                 "answers.")
        self.timeout.setToolTip("How long to wait for the slave's response before counting a "
                                "timeout error.")
        self.pause.setToolTip("After a read error or timeout this set is not polled again for "
                              "this long (other sets keep running).")
        self.enabled = QCheckBox("Poll this set while connected")
        self.enabled.setChecked(poll_set.enabled)
        timing = QGroupBox("Timing")
        tform = QFormLayout(timing)
        tform.addRow("Poll interval", self.interval)
        tform.addRow("Response timeout", self.timeout)
        tform.addRow("Pause after error / timeout", self.pause)
        tform.addRow("", self.enabled)

        self.column_boxes: dict[str, QCheckBox] = {}
        grid = QGridLayout()
        for index, column in enumerate(formats.COLUMN_FORMATS):
            box = QCheckBox(column.title)
            box.setChecked(column.key in poll_set.columns)
            if column.words > 1:
                box.setToolTip(f"Combines {column.words} consecutive registers")
            self.column_boxes[column.key] = box
            grid.addWidget(box, index // 4, index % 4)
        self.byte_swap = QCheckBox("Swap bytes in each register")
        self.byte_swap.setChecked(poll_set.byte_swap)
        self.word_swap = QCheckBox("Low register first (32/64-bit values)")
        self.word_swap.setChecked(poll_set.word_swap)
        self.display = QGroupBox("Value display")
        dlayout = QVBoxLayout(self.display)
        dlayout.addLayout(grid)
        dlayout.addWidget(self.byte_swap)
        dlayout.addWidget(self.word_swap)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #ef4444;")
        self.error_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(what)
        layout.addWidget(timing)
        layout.addWidget(self.display)
        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)

        self.start.valueChanged.connect(self._limit_length)
        self.length.valueChanged.connect(self._limit_start)
        self.start.valueChanged.connect(self._update_hint)
        self.function.currentIndexChanged.connect(self._function_changed)
        self._limit_length()
        self._limit_start()
        self._function_changed()

    def _limit_length(self) -> None:
        """Keep start + length within the 16 bit address space."""
        self.length.setMaximum(65536 - self.start.value())

    def _limit_start(self) -> None:
        """Keep start + length within the 16 bit address space."""
        self.start.setMaximum(65536 - self.length.value())

    def _function_changed(self) -> None:
        """Value formats only apply to register areas; refresh the address hint."""
        is_bits = self.function.currentData() in (1, 2)
        self.display.setEnabled(not is_bits)
        self._update_hint()

    def _update_hint(self) -> None:
        """Show the start address in hex and traditional notation."""
        function = self.function.currentData()
        start = self.start.value()
        self.start_hint.setText(
            f"0x{start:04X}  -  {formats.modicon_address(function, start)} (Modicon)")

    def _accept(self) -> None:
        """Validate and close."""
        if not self.name.text().strip():
            self.error_label.setText("Please enter a name for the set.")
            self.name.setFocus()
            return
        if self.start.value() + self.length.value() > 65536:
            self.error_label.setText("Start address + length must not exceed 65536.")
            return
        self.accept()

    def result_set(self) -> PollSet:
        """Return a copy of the original set with the edited values applied."""
        edited = self._original.copy()
        edited.name = self.name.text().strip()
        edited.slave = self.slave.value()
        edited.function = int(self.function.currentData())
        edited.start = self.start.value()
        edited.length = self.length.value()
        edited.interval_ms = self.interval.value()
        edited.timeout_ms = self.timeout.value()
        edited.error_pause_ms = self.pause.value()
        edited.enabled = self.enabled.isChecked()
        selected = [key for key, box in self.column_boxes.items() if box.isChecked()]
        edited.columns = selected or ["unsigned"]
        edited.byte_swap = self.byte_swap.isChecked()
        edited.word_swap = self.word_swap.isChecked()
        return edited
