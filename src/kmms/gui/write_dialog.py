"""Dialog for manual writes (FC05 / FC06 / FC15 / FC16)."""

from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox
from PySide6.QtWidgets import QDialog
from PySide6.QtWidgets import QDialogButtonBox
from PySide6.QtWidgets import QFormLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QLineEdit
from PySide6.QtWidgets import QSpinBox
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget

from kmms.engine import WriteRequest
from kmms.protocol import FUNCTION_LABELS
from kmms.protocol import MAX_WRITE_BITS
from kmms.protocol import MAX_WRITE_REGISTERS
from kmms.protocol import FunctionCode

_KINDS = (
    (FunctionCode.WRITE_SINGLE_COIL, "coil"),
    (FunctionCode.WRITE_SINGLE_REGISTER, "register"),
    (FunctionCode.WRITE_MULTIPLE_COILS, "coils"),
    (FunctionCode.WRITE_MULTIPLE_REGISTERS, "registers"),
)


def parse_values(text: str, kind: str) -> list[int]:
    """Parse a list of numbers separated by commas/spaces. Raises ``ValueError``."""
    tokens = [t for t in re.split(r"[,\s;]+", text.strip()) if t]
    if not tokens:
        raise ValueError("Enter at least one value")
    values = [int(t, 0) for t in tokens]
    if kind in ("coil", "coils"):
        if any(v not in (0, 1) for v in values):
            raise ValueError("Coil values must be 0 or 1")
    elif any(not 0 <= v <= 0xFFFF for v in values):
        raise ValueError("Register values must be 0..65535 (or 0x0000..0xFFFF)")
    if kind in ("coil", "register") and len(values) != 1:
        raise ValueError("This function writes exactly one value")
    limit = MAX_WRITE_BITS if kind == "coils" else MAX_WRITE_REGISTERS
    if kind in ("coils", "registers") and len(values) > limit:
        raise ValueError(f"At most {limit} values per request")
    return values


class WriteDialog(QDialog):
    """Lets the user write to any slave/address; stays open for repeated writes."""

    submitted = Signal(object)

    def __init__(self, slave: int = 1, address: int = 0, kind: str = "register",
                 parent: Optional[QWidget] = None) -> None:
        """Create the dialog with sensible defaults."""
        super().__init__(parent)
        self.setWindowTitle("Write to slave")
        self.setMinimumWidth(420)
        self.function = QComboBox()
        for code, name in _KINDS:
            self.function.addItem(FUNCTION_LABELS[code], name)
        self.function.setCurrentIndex(max(self.function.findData(kind), 0))
        self.slave = QSpinBox()
        self.slave.setRange(0, 255)
        self.slave.setValue(slave)
        self.slave.setToolTip("Slave 0 is the broadcast address: no response is expected")
        self.address = QSpinBox()
        self.address.setRange(0, 65535)
        self.address.setValue(address)
        self.values = QLineEdit()
        self.values.setPlaceholderText("e.g. 1234   or   10, 0x20, 30")
        self.timeout = QSpinBox()
        self.timeout.setRange(10, 60_000)
        self.timeout.setSuffix(" ms")
        self.timeout.setValue(1000)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Function", self.function)
        form.addRow("Slave address", self.slave)
        form.addRow("Address", self.address)
        form.addRow("Value(s)", self.values)
        form.addRow("Timeout", self.timeout)

        buttons = QDialogButtonBox()
        self.write_button = buttons.addButton("Write", QDialogButtonBox.ButtonRole.AcceptRole)
        self.write_button.setObjectName("primary")
        close = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        close.clicked.connect(self.close)
        self.write_button.clicked.connect(self._submit)
        self.values.returnPressed.connect(self._submit)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.result_label)
        layout.addWidget(buttons)
        self.function.currentIndexChanged.connect(self._kind_changed)
        self._kind_changed()

    def _kind_changed(self) -> None:
        """Adjust the hint for the selected function."""
        kind = self.function.currentData()
        hints = {
            "coil": "0 or 1",
            "coils": "0/1 values, e.g. 1 0 1 1",
            "register": "one value, decimal or 0x hex",
            "registers": "values separated by comma or space",
        }
        self.values.setPlaceholderText(hints[kind])

    def _submit(self) -> None:
        """Validate the input and emit a write request."""
        kind = self.function.currentData()
        try:
            values = parse_values(self.values.text(), kind)
        except ValueError as exc:
            self.show_result(False, str(exc))
            return
        self.result_label.setText("Sending...")
        self.result_label.setStyleSheet("")
        self.submitted.emit(
            WriteRequest(self.slave.value(), kind, self.address.value(), values,
                         self.timeout.value()))

    def show_result(self, ok: bool, text: str) -> None:
        """Display the outcome of the latest write."""
        color = "#16a34a" if ok else "#ef4444"
        self.result_label.setStyleSheet(f"color: {color};")
        self.result_label.setText(("OK: " if ok else "Failed: ") + text)
