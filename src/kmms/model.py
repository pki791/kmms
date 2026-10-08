"""Data model: connection settings, polling sets and the project file."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from pathlib import Path
from typing import Any

from kmms.protocol import BIT_READ_FUNCTIONS
from kmms.protocol import READ_FUNCTIONS
from kmms.protocol import FunctionCode

LOG = logging.getLogger(__name__)

PROJECT_FORMAT = "kmms-project"
PROJECT_VERSION = 1

BAUD_RATES = (300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600, 76800,
              115200, 230400, 460800, 921600)
PARITIES = {"N": "None", "E": "Even", "O": "Odd", "M": "Mark", "S": "Space"}
STOP_BITS = (1, 1.5, 2)
FLOW_CONTROLS = {"none": "None", "rtscts": "RTS/CTS (hardware)", "xonxoff": "XON/XOFF (software)"}
DATA_BITS = (5, 6, 7, 8)

DEFAULT_COLUMNS = ("unsigned", "hex")


class ProjectError(Exception):
    """Raised when a project file cannot be read, validated or written."""


class ConnectionMode(str, Enum):
    """Transport/framing combination used to talk to the slaves."""

    SERIAL_RTU = "serial_rtu"
    SERIAL_ASCII = "serial_ascii"
    TCP = "tcp"
    RTU_OVER_TCP = "rtu_over_tcp"

    @property
    def is_serial(self) -> bool:
        """Return True for the serial line modes."""
        return self in (ConnectionMode.SERIAL_RTU, ConnectionMode.SERIAL_ASCII)

    @property
    def label(self) -> str:
        """Return the human readable name of the mode."""
        return {
            ConnectionMode.SERIAL_RTU: "Serial RTU",
            ConnectionMode.SERIAL_ASCII: "Serial ASCII",
            ConnectionMode.TCP: "Modbus TCP",
            ConnectionMode.RTU_OVER_TCP: "RTU over TCP",
        }[self]


def _int(data: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    """Read an integer from ``data`` and validate its range."""
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectError(f"'{key}' must be an integer, got {value!r}")
    if isinstance(value, float) and not value.is_integer():
        raise ProjectError(f"'{key}' must be an integer, got {value!r}")
    value = int(value)
    if not low <= value <= high:
        raise ProjectError(f"'{key}' must be between {low} and {high}, got {value}")
    return value


def _str(data: dict[str, Any], key: str, default: str, max_len: int = 256) -> str:
    """Read a string from ``data``."""
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ProjectError(f"'{key}' must be a string, got {value!r}")
    return value[:max_len]


def _bool(data: dict[str, Any], key: str, default: bool) -> bool:
    """Read a boolean from ``data``."""
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ProjectError(f"'{key}' must be true or false, got {value!r}")
    return value


@dataclass
class ConnectionConfig:
    """Connection parameters for serial and TCP based transports."""

    mode: ConnectionMode = ConnectionMode.TCP
    serial_port: str = ""
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    flow_control: str = "none"
    host: str = "127.0.0.1"
    tcp_port: int = 502

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON compatible dictionary."""
        return {
            "mode": self.mode.value,
            "serial_port": self.serial_port,
            "baudrate": self.baudrate,
            "bytesize": self.bytesize,
            "parity": self.parity,
            "stopbits": self.stopbits,
            "flow_control": self.flow_control,
            "host": self.host,
            "tcp_port": self.tcp_port,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectionConfig:
        """Create a validated config from a dictionary."""
        try:
            mode = ConnectionMode(data.get("mode", ConnectionMode.TCP.value))
        except ValueError as exc:
            raise ProjectError(f"Unknown connection mode: {data.get('mode')!r}") from exc
        parity = _str(data, "parity", "N", 1).upper()
        if parity not in PARITIES:
            raise ProjectError(f"Unknown parity: {parity!r}")
        stopbits = data.get("stopbits", 1)
        if stopbits not in STOP_BITS:
            raise ProjectError(f"Unsupported stop bits: {stopbits!r}")
        flow_control = _str(data, "flow_control", "none", 16)
        if flow_control not in FLOW_CONTROLS:
            raise ProjectError(f"Unknown flow control: {flow_control!r}")
        return cls(
            mode=mode,
            serial_port=_str(data, "serial_port", ""),
            baudrate=_int(data, "baudrate", 9600, 50, 4_000_000),
            bytesize=_int(data, "bytesize", 8, 5, 8),
            parity=parity,
            stopbits=stopbits,
            flow_control=flow_control,
            host=_str(data, "host", "127.0.0.1"),
            tcp_port=_int(data, "tcp_port", 502, 1, 65535),
        )

    def describe(self) -> str:
        """Return a short human readable description of the endpoint."""
        if self.mode.is_serial:
            flow = {"none": "", "rtscts": " RTS/CTS", "xonxoff": " XON/XOFF"}[self.flow_control]
            return (
                f"{self.serial_port or '(no port)'} {self.baudrate} "
                f"{self.bytesize}{self.parity}{self.stopbits:g}{flow}"
            )
        return f"{self.host}:{self.tcp_port}"


@dataclass
class WindowState:
    """Position and visibility of a polling set window inside the workspace."""

    x: int = 20
    y: int = 20
    width: int = 520
    height: int = 360
    visible: bool = True
    minimized: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON compatible dictionary."""
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "visible": self.visible,
            "minimized": self.minimized,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WindowState:
        """Create a window state from a dictionary."""
        return cls(
            x=_int(data, "x", 20, -100_000, 100_000),
            y=_int(data, "y", 20, -100_000, 100_000),
            width=_int(data, "width", 520, 100, 100_000),
            height=_int(data, "height", 360, 80, 100_000),
            visible=_bool(data, "visible", True),
            minimized=_bool(data, "minimized", False),
        )


def _new_id() -> str:
    """Return a fresh unique identifier for a polling set."""
    return uuid.uuid4().hex


@dataclass
class PollSet:
    """One polling definition: what to read, from whom and how often."""

    id: str = field(default_factory=_new_id)
    name: str = "Set 1"
    slave: int = 1
    function: int = int(FunctionCode.READ_HOLDING_REGISTERS)
    start: int = 0
    length: int = 10
    interval_ms: int = 1000
    timeout_ms: int = 1000
    error_pause_ms: int = 3000
    enabled: bool = True
    columns: list[str] = field(default_factory=lambda: list(DEFAULT_COLUMNS))
    byte_swap: bool = False
    word_swap: bool = False
    window: WindowState = field(default_factory=WindowState)

    @property
    def is_bit_set(self) -> bool:
        """Return True when the set reads coils or discrete inputs."""
        return self.function in BIT_READ_FUNCTIONS

    @property
    def is_writable(self) -> bool:
        """Return True when the polled data area can be written (coils/holding registers)."""
        return self.function in (
            FunctionCode.READ_COILS,
            FunctionCode.READ_HOLDING_REGISTERS,
        )

    def copy(self) -> PollSet:
        """Return an independent deep copy (same id)."""
        return PollSet.from_dict(self.to_dict())

    def timing_signature(self) -> tuple:
        """Return the values that force an immediate re-poll when they change."""
        return (self.slave, self.function, self.start, self.length, self.enabled)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON compatible dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "slave": self.slave,
            "function": self.function,
            "start": self.start,
            "length": self.length,
            "interval_ms": self.interval_ms,
            "timeout_ms": self.timeout_ms,
            "error_pause_ms": self.error_pause_ms,
            "enabled": self.enabled,
            "columns": list(self.columns),
            "byte_swap": self.byte_swap,
            "word_swap": self.word_swap,
            "window": self.window.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PollSet:
        """Create a validated polling set from a dictionary."""
        function = _int(data, "function", 3, 1, 4)
        if function not in READ_FUNCTIONS:
            raise ProjectError(f"Unsupported read function: {function}")
        start = _int(data, "start", 0, 0, 65535)
        length = _int(data, "length", 10, 1, 65536)
        if start + length > 65536:
            raise ProjectError("Start address + length exceeds 65536")
        columns = data.get("columns", list(DEFAULT_COLUMNS))
        if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
            raise ProjectError("'columns' must be a list of strings")
        window = data.get("window", {})
        if not isinstance(window, dict):
            raise ProjectError("'window' must be an object")
        set_id = _str(data, "id", "") or _new_id()
        return cls(
            id=set_id,
            name=_str(data, "name", "Set"),
            slave=_int(data, "slave", 1, 0, 255),
            function=function,
            start=start,
            length=length,
            interval_ms=_int(data, "interval_ms", 1000, 0, 3_600_000),
            timeout_ms=_int(data, "timeout_ms", 1000, 10, 600_000),
            error_pause_ms=_int(data, "error_pause_ms", 3000, 0, 3_600_000),
            enabled=_bool(data, "enabled", True),
            columns=list(columns),
            byte_swap=_bool(data, "byte_swap", False),
            word_swap=_bool(data, "word_swap", False),
            window=WindowState.from_dict(window),
        )


@dataclass
class Project:
    """A complete workspace: connection settings, all polling sets and UI state."""

    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    sets: list[PollSet] = field(default_factory=list)
    log_visible: bool = True
    view_mode: str = "windows"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON compatible dictionary."""
        return {
            "format": PROJECT_FORMAT,
            "version": PROJECT_VERSION,
            "connection": self.connection.to_dict(),
            "sets": [s.to_dict() for s in self.sets],
            "ui": {"log_visible": self.log_visible, "view_mode": self.view_mode},
        }

    @classmethod
    def from_dict(cls, data: Any) -> Project:
        """Create a validated project from a parsed JSON document."""
        if not isinstance(data, dict) or data.get("format") != PROJECT_FORMAT:
            raise ProjectError("Not a KMMS project file")
        version = data.get("version")
        if not isinstance(version, int) or version < 1:
            raise ProjectError("Missing or invalid project version")
        if version > PROJECT_VERSION:
            raise ProjectError(
                f"Project was created by a newer version (file v{version}, "
                f"supported v{PROJECT_VERSION})"
            )
        connection = data.get("connection", {})
        sets = data.get("sets", [])
        ui = data.get("ui", {})
        if not isinstance(connection, dict) or not isinstance(ui, dict):
            raise ProjectError("Malformed project file")
        if not isinstance(sets, list) or not all(isinstance(s, dict) for s in sets):
            raise ProjectError("'sets' must be a list of objects")
        parsed_sets = [PollSet.from_dict(s) for s in sets]
        seen: set[str] = set()
        for poll_set in parsed_sets:
            if poll_set.id in seen:
                poll_set.id = _new_id()
            seen.add(poll_set.id)
        view_mode = _str(ui, "view_mode", "windows", 16)
        return cls(
            connection=ConnectionConfig.from_dict(connection),
            sets=parsed_sets,
            log_visible=_bool(ui, "log_visible", True),
            view_mode=view_mode if view_mode in ("windows", "tabs") else "windows",
        )

    def save(self, path: Path | str) -> None:
        """Write the project to ``path`` atomically as pretty printed JSON."""
        target = Path(path)
        text = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        tmp_name = ""
        try:
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".kmms-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.write("\n")
            os.replace(tmp_name, target)
        except OSError as exc:
            LOG.error("Could not save project to %s: %s", target, exc)
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            raise ProjectError(f"Could not save '{target}': {exc.strerror or exc}") from exc

    @classmethod
    def load(cls, path: Path | str) -> Project:
        """Read and validate a project file."""
        target = Path(path)
        try:
            with open(target, encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError as exc:
            raise ProjectError(f"Could not read '{target}': {exc.strerror or exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProjectError(f"'{target.name}' is not valid JSON: {exc}") from exc
        return cls.from_dict(data)
