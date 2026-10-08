"""Byte channels (serial / TCP) and the Modbus master that runs transactions over them."""

from __future__ import annotations

import logging
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable
from typing import Optional
from typing import Protocol

import serial
from serial.tools import list_ports

from kmms.model import ConnectionConfig
from kmms.model import ConnectionMode
from kmms.protocol import Framing
from kmms.protocol import ModbusConnectionError
from kmms.protocol import ModbusError
from kmms.protocol import ModbusFrameError
from kmms.protocol import ModbusTimeout
from kmms.protocol import build_ascii_frame
from kmms.protocol import build_mbap_frame
from kmms.protocol import build_rtu_frame
from kmms.protocol import decode_ascii_frame
from kmms.protocol import decode_rtu_frame
from kmms.protocol import rtu_response_length

LOG = logging.getLogger(__name__)

TraceCallback = Callable[[str, bytes], None]

#: Longest time a blocking read waits before re-checking the abort flag.
ABORT_POLL_S = 0.05
CONNECT_TIMEOUT_S = 3.0

_PARITY = {
    "N": serial.PARITY_NONE,
    "E": serial.PARITY_EVEN,
    "O": serial.PARITY_ODD,
    "M": serial.PARITY_MARK,
    "S": serial.PARITY_SPACE,
}
_STOPBITS = {
    1: serial.STOPBITS_ONE,
    1.5: serial.STOPBITS_ONE_POINT_FIVE,
    2: serial.STOPBITS_TWO,
}
_BYTESIZE = {
    5: serial.FIVEBITS,
    6: serial.SIXBITS,
    7: serial.SEVENBITS,
    8: serial.EIGHTBITS,
}


@dataclass(frozen=True)
class PortInfo:
    """A serial port discovered on this machine."""

    device: str
    description: str


def list_serial_ports() -> list[PortInfo]:
    """Return the serial ports of this machine (real, USB and virtual ones)."""
    ports: list[PortInfo] = []
    try:
        found = list(list_ports.comports())
    except (OSError, serial.SerialException) as exc:
        LOG.warning("Could not enumerate serial ports: %s", exc)
        return ports
    for info in found:
        # On macOS every port shows up twice (/dev/cu.* and /dev/tty.*); cu.* is the right one
        # for initiating connections.
        if sys.platform == "darwin" and info.device.startswith("/dev/tty."):
            continue
        description = info.description if info.description and info.description != "n/a" else ""
        ports.append(PortInfo(info.device, description))
    ports.sort(key=lambda p: _natural_key(p.device))
    return ports


def _natural_key(text: str) -> list:
    """Sort key that orders COM2 before COM10."""
    parts: list = []
    number = ""
    for char in text:
        if char.isdigit():
            number += char
        else:
            if number:
                parts.append((1, int(number)))
                number = ""
            parts.append((0, char))
    if number:
        parts.append((1, int(number)))
    return parts


class Channel(Protocol):
    """Minimal byte-stream interface the master needs."""

    def write(self, data: bytes) -> None:
        """Send ``data`` completely."""

    def read_available(self, max_bytes: int, timeout: float) -> bytes:
        """Return 1..max_bytes bytes, or ``b""`` if nothing arrived within ``timeout``."""

    def discard_input(self) -> None:
        """Drop any stale bytes in the receive buffer."""

    def close(self) -> None:
        """Release the underlying port/socket."""


class SerialChannel:
    """Channel over a serial port with RTU inter-frame spacing."""

    def __init__(self, config: ConnectionConfig) -> None:
        """Open the serial port described by ``config``."""
        if not config.serial_port:
            raise ModbusConnectionError("No serial port selected")
        try:
            self._ser = serial.Serial(
                port=config.serial_port,
                baudrate=config.baudrate,
                bytesize=_BYTESIZE[config.bytesize],
                parity=_PARITY[config.parity],
                stopbits=_STOPBITS[config.stopbits],
                timeout=0,
                write_timeout=2.0,
                rtscts=config.flow_control == "rtscts",
                xonxoff=config.flow_control == "xonxoff",
                exclusive=True if sys.platform != "win32" else None,
            )
        except (serial.SerialException, OSError, ValueError) as exc:
            raise ModbusConnectionError(f"Cannot open {config.serial_port}: {exc}") from exc
        bits_per_char = 1 + config.bytesize + (0 if config.parity == "N" else 1)
        bits_per_char += config.stopbits
        char_time = bits_per_char / max(config.baudrate, 1)
        # Modbus RTU: 3.5 character times of silence, fixed at 1.75 ms above 19200 baud.
        self._frame_gap = 0.00175 if config.baudrate > 19200 else 3.5 * char_time
        self._last_activity = 0.0

    def write(self, data: bytes) -> None:
        """Wait for bus silence, send ``data`` and block until it has left the UART."""
        wait = self._last_activity + self._frame_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            self._ser.write(data)
            self._ser.flush()
        except (serial.SerialException, OSError) as exc:
            raise ModbusConnectionError(f"Serial write failed: {exc}") from exc
        self._last_activity = time.monotonic()

    def read_available(self, max_bytes: int, timeout: float) -> bytes:
        """Wait up to ``timeout`` for data and return what is buffered (at least one byte)."""
        try:
            self._ser.timeout = max(timeout, 0)
            first = self._ser.read(1)
            if not first:
                return b""
            waiting = self._ser.in_waiting
            extra = b""
            if waiting and max_bytes > 1:
                extra = self._ser.read(min(waiting, max_bytes - 1))
        except (serial.SerialException, OSError, TypeError) as exc:
            raise ModbusConnectionError(f"Serial read failed: {exc}") from exc
        self._last_activity = time.monotonic()
        return first + extra

    def discard_input(self) -> None:
        """Flush the receive buffer."""
        try:
            self._ser.reset_input_buffer()
        except (serial.SerialException, OSError) as exc:
            raise ModbusConnectionError(f"Serial error: {exc}") from exc

    def close(self) -> None:
        """Close the port, ignoring errors from already vanished devices."""
        try:
            self._ser.close()
        except (serial.SerialException, OSError) as exc:
            LOG.debug("Error while closing serial port: %s", exc)


class SocketChannel:
    """Channel over a TCP socket."""

    def __init__(self, config: ConnectionConfig, connect_timeout: float = CONNECT_TIMEOUT_S):
        """Connect to ``config.host:config.tcp_port``."""
        if not config.host:
            raise ModbusConnectionError("No host given")
        try:
            self._sock = socket.create_connection(
                (config.host, config.tcp_port), timeout=connect_timeout
            )
        except (OSError, ValueError, OverflowError) as exc:
            raise ModbusConnectionError(
                f"Cannot connect to {config.host}:{config.tcp_port}: {exc}"
            ) from exc
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    def write(self, data: bytes) -> None:
        """Send ``data`` completely."""
        try:
            self._sock.settimeout(2.0)
            self._sock.sendall(data)
        except (OSError, socket.timeout) as exc:
            raise ModbusConnectionError(f"Socket write failed: {exc}") from exc

    def read_available(self, max_bytes: int, timeout: float) -> bytes:
        """Wait up to ``timeout`` for data; raise if the peer closed the connection."""
        try:
            self._sock.settimeout(max(timeout, 0.0005))
            chunk = self._sock.recv(max_bytes)
        except socket.timeout:
            return b""
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise ModbusConnectionError(f"Socket read failed: {exc}") from exc
        if not chunk:
            raise ModbusConnectionError("Connection closed by the remote host")
        return chunk

    def discard_input(self) -> None:
        """Drop bytes that arrived since the last transaction (late responses)."""
        try:
            self._sock.settimeout(0)
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise ModbusConnectionError("Connection closed by the remote host")
        except (BlockingIOError, socket.timeout):
            return
        except OSError as exc:
            raise ModbusConnectionError(f"Socket error: {exc}") from exc

    def close(self) -> None:
        """Shut the socket down and close it."""
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError as exc:
            LOG.debug("Error while closing socket: %s", exc)


class ModbusMaster:
    """Runs request/response transactions over a :class:`Channel`.

    The master does no retries: every transaction either returns the response PDU or raises
    a :class:`ModbusError` subclass, so the caller fully controls timing and error handling.
    """

    def __init__(
        self,
        channel: Channel,
        framing: Framing,
        trace: Optional[TraceCallback] = None,
        abort: Optional[threading.Event] = None,
    ) -> None:
        """Create a master for ``channel`` using the given ``framing``."""
        self._channel = channel
        self._framing = framing
        self._trace = trace
        self._abort = abort or threading.Event()
        self._transaction_id = 0

    @property
    def framing(self) -> Framing:
        """Return the wire framing in use."""
        return self._framing

    def close(self) -> None:
        """Close the underlying channel."""
        self._channel.close()

    def execute(
        self, slave: int, pdu: bytes, timeout: float, expect_response: bool = True
    ) -> bytes:
        """Send ``pdu`` to ``slave`` and return the response PDU.

        Raises :class:`ModbusTimeout` if no complete response arrives within ``timeout``
        seconds, :class:`ModbusFrameError` for corrupt or mismatched frames and
        :class:`ModbusConnectionError` if the link is lost.
        """
        self._channel.discard_input()
        if self._framing is Framing.RTU:
            frame = build_rtu_frame(slave, pdu)
        elif self._framing is Framing.ASCII:
            frame = build_ascii_frame(slave, pdu)
        else:
            self._transaction_id = (self._transaction_id + 1) & 0xFFFF
            frame = build_mbap_frame(self._transaction_id, slave, pdu)
        self._emit("TX", frame)
        self._channel.write(frame)
        if not expect_response:
            return b""
        deadline = time.monotonic() + timeout
        if self._framing is Framing.RTU:
            return self._receive_rtu(slave, deadline)
        if self._framing is Framing.ASCII:
            return self._receive_ascii(slave, deadline)
        return self._receive_socket(slave, deadline)

    # ----------------------------------------------------------------------- receiving

    def _emit(self, direction: str, data: bytes) -> None:
        """Report raw traffic to the trace callback."""
        if self._trace is not None and data:
            try:
                self._trace(direction, data)
            except Exception:  # noqa: BLE001 - a broken trace sink must never break polling
                LOG.exception("Trace callback failed")

    def _read_some(self, max_bytes: int, deadline: float) -> bytes:
        """Read at least one byte before ``deadline`` or raise :class:`ModbusTimeout`."""
        while True:
            if self._abort.is_set():
                raise ModbusConnectionError("Connection closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModbusTimeout("No response (timeout)")
            chunk = self._channel.read_available(max_bytes, min(remaining, ABORT_POLL_S))
            if chunk:
                return chunk

    def _read_exact(self, size: int, deadline: float, partial: bytearray) -> bytes:
        """Read exactly ``size`` bytes; received bytes are appended to ``partial``."""
        buffer = bytearray()
        while len(buffer) < size:
            chunk = self._read_some(size - len(buffer), deadline)
            buffer += chunk
            partial += chunk
        return bytes(buffer)

    def _receive_rtu(self, slave: int, deadline: float) -> bytes:
        """Receive one RTU frame whose length is derived from its function code."""
        frame = bytearray()
        try:
            self._read_exact(2, deadline, frame)
            total = rtu_response_length(bytes(frame))
            if total is None:
                self._read_exact(1, deadline, frame)
                total = rtu_response_length(bytes(frame))
            self._read_exact(total - len(frame), deadline, frame)
        except ModbusTimeout:
            self._emit("RX", bytes(frame))
            if frame:
                raise ModbusTimeout("Incomplete response (timeout)") from None
            raise
        except ModbusFrameError:
            self._emit("RX", bytes(frame))
            self._channel.discard_input()
            raise
        self._emit("RX", bytes(frame))
        response_slave, pdu = decode_rtu_frame(bytes(frame))
        if response_slave != slave:
            raise ModbusFrameError(f"Response from slave {response_slave}, expected {slave}")
        return pdu

    def _receive_ascii(self, slave: int, deadline: float) -> bytes:
        """Receive one ASCII frame (``:`` ... CR LF)."""
        frame = bytearray()
        try:
            while not frame.endswith(b"\r\n"):
                frame += self._read_some(1, deadline)
                if frame[:1] != b":":
                    start = frame.find(b":")
                    del frame[: start if start >= 0 else len(frame)]
                if len(frame) > 520:
                    raise ModbusFrameError("ASCII frame too long")
        except ModbusTimeout:
            self._emit("RX", bytes(frame))
            if frame:
                raise ModbusTimeout("Incomplete response (timeout)") from None
            raise
        except ModbusFrameError:
            self._emit("RX", bytes(frame))
            raise
        self._emit("RX", bytes(frame))
        response_slave, pdu = decode_ascii_frame(bytes(frame))
        if response_slave != slave:
            raise ModbusFrameError(f"Response from slave {response_slave}, expected {slave}")
        return pdu

    def _receive_socket(self, slave: int, deadline: float) -> bytes:
        """Receive one MBAP frame, skipping late responses of earlier transactions."""
        while True:
            raw = bytearray()
            try:
                header = self._read_exact(7, deadline, raw)
                transaction, protocol, length = struct.unpack(">HHH", header[:6])
                if protocol != 0 or not 2 <= length <= 254:
                    self._emit("RX", bytes(raw))
                    raise ModbusFrameError("Invalid MBAP header")
                body = self._read_exact(length - 1, deadline, raw)
            except ModbusTimeout:
                self._emit("RX", bytes(raw))
                if raw:
                    raise ModbusTimeout("Incomplete response (timeout)") from None
                raise
            self._emit("RX", bytes(raw))
            if transaction != self._transaction_id:
                LOG.debug("Dropping stale response with transaction id %d", transaction)
                continue
            # The unit identifier is not compared: many gateways and PLCs answer with 0 or 255.
            return body


class ModbusLink:
    """An open connection: a configured master plus the means to close it."""

    def __init__(self, master: ModbusMaster, config: ConnectionConfig) -> None:
        """Wrap ``master`` which was opened for ``config``."""
        self.master = master
        self.config = config

    def execute(
        self, slave: int, pdu: bytes, timeout: float, expect_response: bool = True
    ) -> bytes:
        """Run a transaction (see :meth:`ModbusMaster.execute`)."""
        return self.master.execute(slave, pdu, timeout, expect_response)

    def close(self) -> None:
        """Close the connection."""
        self.master.close()


def open_link(
    config: ConnectionConfig,
    trace: Optional[TraceCallback] = None,
    abort: Optional[threading.Event] = None,
) -> ModbusLink:
    """Open the port/socket described by ``config`` and return a ready :class:`ModbusLink`."""
    channel: Channel
    if config.mode.is_serial:
        channel = SerialChannel(config)
        framing = Framing.RTU if config.mode is ConnectionMode.SERIAL_RTU else Framing.ASCII
    else:
        channel = SocketChannel(config)
        framing = Framing.SOCKET if config.mode is ConnectionMode.TCP else Framing.RTU
    return ModbusLink(ModbusMaster(channel, framing, trace, abort), config)


__all__ = [
    "Channel",
    "ModbusError",
    "ModbusLink",
    "ModbusMaster",
    "PortInfo",
    "SerialChannel",
    "SocketChannel",
    "list_serial_ports",
    "open_link",
]
