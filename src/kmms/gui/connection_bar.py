"""The connection settings bar with Open / Close control."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtCore import Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import QComboBox
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QLineEdit
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtWidgets import QSpinBox
from PySide6.QtWidgets import QStackedWidget
from PySide6.QtWidgets import QToolButton
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget

from kmms import engine
from kmms.gui import theme
from kmms.model import BAUD_RATES
from kmms.model import DATA_BITS
from kmms.model import FLOW_CONTROLS
from kmms.model import PARITIES
from kmms.model import STOP_BITS
from kmms.model import ConnectionConfig
from kmms.model import ConnectionMode
from kmms.transport import list_serial_ports


class PortComboBox(QComboBox):
    """Editable combo box listing serial ports; rescans whenever the list is opened."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """Create an editable port selector."""
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.lineEdit().setPlaceholderText("Select or type a port")
        self.setMinimumWidth(190)

    def refresh(self) -> None:
        """Reload the port list, keeping the current text."""
        current = self.currentText()
        self.blockSignals(True)
        self.clear()
        for port in list_serial_ports():
            self.addItem(port.device)
            if port.description:
                self.setItemData(self.count() - 1, port.description,
                                 Qt.ItemDataRole.ToolTipRole)
        self.setEditText(current)
        self.blockSignals(False)

    def showPopup(self) -> None:  # noqa: N802 - Qt naming
        """Rescan before showing the list."""
        self.refresh()
        super().showPopup()


def _caption(text: str, widget: QWidget) -> QWidget:
    """Return ``widget`` with a small muted caption above it."""
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    label = QLabel(text)
    label.setObjectName("muted")
    font = label.font()
    font.setPointSizeF(font.pointSizeF() * 0.85)
    label.setFont(font)
    layout.addWidget(label)
    layout.addWidget(widget)
    return holder


class ConnectionBar(QFrame):
    """Mode, serial or TCP parameters, and the Open / Close button."""

    open_requested = Signal()
    close_requested = Signal()
    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """Build the bar."""
        super().__init__(parent)
        self.setObjectName("connectionBar")
        self._state = engine.STATE_CLOSED

        self.mode = QComboBox()
        for mode in ConnectionMode:
            self.mode.addItem(mode.label, mode.value)
        self.mode.setCurrentIndex(self.mode.findData(ConnectionMode.TCP.value))

        self.port = PortComboBox()
        self.refresh_button = QToolButton()
        self.refresh_button.setText("↻")
        self.refresh_button.setToolTip("Rescan serial ports")
        self.baud = QComboBox()
        self.baud.setEditable(True)
        self.baud.lineEdit().setValidator(QIntValidator(50, 4_000_000))
        for rate in BAUD_RATES:
            self.baud.addItem(str(rate))
        self.baud.setCurrentText("9600")
        self.baud.setMinimumWidth(90)
        self.bits = QComboBox()
        for bits in DATA_BITS:
            self.bits.addItem(str(bits), bits)
        self.bits.setCurrentIndex(self.bits.findData(8))
        self.parity = QComboBox()
        for key, name in PARITIES.items():
            self.parity.addItem(name, key)
        self.stop = QComboBox()
        for stop in STOP_BITS:
            self.stop.addItem(f"{stop:g}", stop)
        self.flow = QComboBox()
        for key, name in FLOW_CONTROLS.items():
            self.flow.addItem(name, key)

        serial_page = QWidget()
        serial_layout = QHBoxLayout(serial_page)
        serial_layout.setContentsMargins(0, 0, 0, 0)
        port_box = QWidget()
        port_row = QHBoxLayout(port_box)
        port_row.setContentsMargins(0, 0, 0, 0)
        port_row.setSpacing(4)
        port_row.addWidget(self.port, 1)
        port_row.addWidget(self.refresh_button)
        serial_layout.addWidget(_caption("Port", port_box))
        serial_layout.addWidget(_caption("Baud rate", self.baud))
        serial_layout.addWidget(_caption("Data bits", self.bits))
        serial_layout.addWidget(_caption("Parity", self.parity))
        serial_layout.addWidget(_caption("Stop bits", self.stop))
        serial_layout.addWidget(_caption("Flow control", self.flow))
        serial_layout.addStretch(1)

        self.host = QLineEdit("127.0.0.1")
        self.host.setPlaceholderText("IP address or host name")
        self.host.setMinimumWidth(200)
        self.tcp_port = QSpinBox()
        self.tcp_port.setRange(1, 65535)
        self.tcp_port.setValue(502)
        tcp_page = QWidget()
        tcp_layout = QHBoxLayout(tcp_page)
        tcp_layout.setContentsMargins(0, 0, 0, 0)
        tcp_layout.addWidget(_caption("IP address / host", self.host))
        tcp_layout.addWidget(_caption("TCP port", self.tcp_port))
        tcp_layout.addStretch(1)

        self.pages = QStackedWidget()
        self.pages.addWidget(serial_page)
        self.pages.addWidget(tcp_page)
        self.pages.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.led = QLabel()
        self.led.setFixedSize(14, 14)
        self.state_label = QLabel("Closed")
        self.state_label.setMinimumWidth(110)
        self.state_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.button = QPushButton("Open")
        self.button.setObjectName("primary")
        self.button.setMinimumWidth(96)
        self.button.setMinimumHeight(32)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)
        layout.addWidget(_caption("Connection", self.mode))
        layout.addWidget(self.pages, 1)
        layout.addWidget(self.led)
        layout.addWidget(self.state_label)
        layout.addWidget(self.button)

        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.refresh_button.clicked.connect(self.port.refresh)
        self.button.clicked.connect(self._button_clicked)
        for signal in (self.mode.currentIndexChanged, self.port.editTextChanged,
                       self.baud.editTextChanged, self.bits.currentIndexChanged,
                       self.parity.currentIndexChanged, self.stop.currentIndexChanged,
                       self.flow.currentIndexChanged, self.host.textChanged,
                       self.tcp_port.valueChanged):
            signal.connect(lambda *_: self.changed.emit())
        self.port.refresh()
        self._mode_changed()
        self.set_state(engine.STATE_CLOSED)

    # ------------------------------------------------------------------ config access

    def config(self) -> ConnectionConfig:
        """Return the settings currently entered."""
        try:
            baud = int(self.baud.currentText())
        except ValueError:
            baud = 9600
        return ConnectionConfig(
            mode=ConnectionMode(self.mode.currentData()),
            serial_port=self.port.currentText().strip(),
            baudrate=baud,
            bytesize=int(self.bits.currentData()),
            parity=str(self.parity.currentData()),
            stopbits=self.stop.currentData(),
            flow_control=str(self.flow.currentData()),
            host=self.host.text().strip(),
            tcp_port=self.tcp_port.value(),
        )

    def set_config(self, config: ConnectionConfig) -> None:
        """Show ``config`` in the controls without emitting change signals."""
        widgets = (self.mode, self.port, self.baud, self.bits, self.parity, self.stop,
                   self.flow, self.host, self.tcp_port)
        for widget in widgets:
            widget.blockSignals(True)
        self.mode.setCurrentIndex(max(self.mode.findData(config.mode.value), 0))
        self.port.setEditText(config.serial_port)
        self.baud.setEditText(str(config.baudrate))
        self.bits.setCurrentIndex(max(self.bits.findData(config.bytesize), 0))
        self.parity.setCurrentIndex(max(self.parity.findData(config.parity), 0))
        self.stop.setCurrentIndex(max(self.stop.findData(config.stopbits), 0))
        self.flow.setCurrentIndex(max(self.flow.findData(config.flow_control), 0))
        self.host.setText(config.host)
        self.tcp_port.setValue(config.tcp_port)
        for widget in widgets:
            widget.blockSignals(False)
        self._mode_changed()

    # ------------------------------------------------------------------ state

    def _mode_changed(self) -> None:
        """Show the serial or the TCP parameter page."""
        is_serial = ConnectionMode(self.mode.currentData()).is_serial
        self.pages.setCurrentIndex(0 if is_serial else 1)

    def _button_clicked(self) -> None:
        """Open or close depending on the current state."""
        if self._state == engine.STATE_CLOSED or self._state == engine.STATE_ERROR:
            self.open_requested.emit()
        else:
            self.close_requested.emit()

    def set_state(self, state: str, message: str = "") -> None:
        """Reflect the connection ``state`` in LED, label, button and enabled controls."""
        self._state = state
        colors = theme.current()
        led_color, text, button = {
            engine.STATE_CLOSED: (colors.muted, "Closed", "Open"),
            engine.STATE_CONNECTING: (colors.change_color, "Connecting...", "Cancel"),
            engine.STATE_OPEN: (colors.ok_color, "Open", "Close"),
            engine.STATE_RECONNECTING: (colors.change_color, "Reconnecting...", "Close"),
            engine.STATE_ERROR: (colors.error_color, "Error", "Open"),
        }.get(state, (colors.muted, state, "Open"))
        self.led.setStyleSheet(f"background: {led_color}; border-radius: 7px;")
        self.state_label.setText(text)
        self.state_label.setToolTip(message)
        self.button.setText(button)
        self.button.setObjectName("danger" if button in ("Close", "Cancel") else "primary")
        self.button.style().unpolish(self.button)
        self.button.style().polish(self.button)
        editable = state in (engine.STATE_CLOSED, engine.STATE_ERROR)
        for widget in (self.mode, self.pages):
            widget.setEnabled(editable)

    @property
    def state(self) -> str:
        """Return the last state set."""
        return self._state
