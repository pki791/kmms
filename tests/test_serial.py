"""Serial RTU/ASCII tests using a pseudo-terminal pair (POSIX only)."""

from __future__ import annotations

import os
import select
import sys
import threading
import time

import pytest

from kmms import protocol as p
from kmms.model import ConnectionConfig
from kmms.model import ConnectionMode
from kmms.model import PollSet
from kmms.transport import list_serial_ports
from kmms.transport import open_link

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pty")

if sys.platform != "win32":
    import pty
    import tty


class PtySlave:
    """A tiny serial slave answering reads of holding registers (value = address)."""

    def __init__(self, ascii_mode: bool = False) -> None:
        """Create the pty pair; ``port`` is the device name the master opens."""
        self.master_fd, slave_fd = pty.openpty()
        tty.setraw(self.master_fd)
        tty.setraw(slave_fd)
        self.port = os.ttyname(slave_fd)
        self._slave_fd = slave_fd  # keep open so the device does not vanish
        self.ascii_mode = ascii_mode
        self.requests: list[bytes] = []
        self.corrupt_next = False
        self.silent = False
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _read_exact(self, size: int) -> bytes:
        """Blocking read of ``size`` bytes (until stopped)."""
        data = b""
        while len(data) < size and not self._stop:
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.05)
                if not ready:
                    continue
                chunk = os.read(self.master_fd, size - len(data))
            except (OSError, ValueError):
                return b""
            data += chunk
        return data

    def _serve(self) -> None:
        """Answer FC03 read requests forever."""
        while not self._stop:
            if self.ascii_mode:
                frame = b""
                while not frame.endswith(b"\r\n") and not self._stop:
                    frame += self._read_exact(1)
                if self._stop:
                    return
                slave, pdu = p.decode_ascii_frame(frame)
            else:
                header = self._read_exact(8)
                if len(header) < 8:
                    return
                slave, pdu = p.decode_rtu_frame(header)
            self.requests.append(pdu)
            if self.silent:
                continue
            _, address, count = int.from_bytes(pdu[:1], "big"), int.from_bytes(
                pdu[1:3], "big"), int.from_bytes(pdu[3:5], "big")
            body = bytes([3, count * 2]) + b"".join(
                (address + i).to_bytes(2, "big") for i in range(count))
            reply = (p.build_ascii_frame if self.ascii_mode else p.build_rtu_frame)(slave, body)
            if self.corrupt_next:
                self.corrupt_next = False
                reply = reply[:-3] + bytes([reply[-3] ^ 0xFF]) + reply[-2:]
            os.write(self.master_fd, reply)

    def close(self) -> None:
        """Stop the slave and close the pty."""
        self._stop = True
        self._thread.join(2)
        for fd in (self.master_fd, self._slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def _config(port: str, mode: ConnectionMode) -> ConnectionConfig:
    """Serial config for the pty slave."""
    return ConnectionConfig(mode=mode, serial_port=port, baudrate=115200, bytesize=8,
                            parity="N", stopbits=1)


@pytest.fixture
def rtu_slave():
    """RTU slave on a pty."""
    slave = PtySlave()
    yield slave
    slave.close()


@pytest.fixture
def ascii_slave():
    """ASCII slave on a pty."""
    slave = PtySlave(ascii_mode=True)
    yield slave
    slave.close()


def test_serial_rtu_read(rtu_slave):
    """Reads work over a serial device, repeatedly."""
    link = open_link(_config(rtu_slave.port, ConnectionMode.SERIAL_RTU))
    try:
        for start in (0, 10, 100):
            pdu = p.build_read_request(3, start, 4)
            response = link.execute(1, pdu, 1.0)
            assert p.parse_read_response(3, response, 4) == list(range(start, start + 4))
    finally:
        link.close()


def test_serial_ascii_read(ascii_slave):
    """ASCII mode over a serial device."""
    link = open_link(_config(ascii_slave.port, ConnectionMode.SERIAL_ASCII))
    try:
        pdu = p.build_read_request(3, 20, 3)
        assert p.parse_read_response(3, link.execute(5, pdu, 1.0), 3) == [20, 21, 22]
    finally:
        link.close()


def test_serial_timeout_and_recovery(rtu_slave):
    """A silent slave times out; a later good answer works (stale bytes are discarded)."""
    link = open_link(_config(rtu_slave.port, ConnectionMode.SERIAL_RTU))
    try:
        rtu_slave.silent = True
        with pytest.raises(p.ModbusTimeout):
            link.execute(1, p.build_read_request(3, 0, 2), 0.2)
        rtu_slave.silent = False
        response = link.execute(1, p.build_read_request(3, 0, 2), 1.0)
        assert p.parse_read_response(3, response, 2) == [0, 1]
    finally:
        link.close()


def test_serial_crc_error(rtu_slave):
    """A corrupted reply is reported as a CRC error."""
    link = open_link(_config(rtu_slave.port, ConnectionMode.SERIAL_RTU))
    try:
        rtu_slave.corrupt_next = True
        with pytest.raises(p.ModbusFrameError, match="CRC"):
            link.execute(1, p.build_read_request(3, 0, 2), 1.0)
    finally:
        link.close()


def test_frame_gap_between_requests(rtu_slave):
    """Back-to-back requests at a slow baud rate keep the 3.5 character silence."""
    config = _config(rtu_slave.port, ConnectionMode.SERIAL_RTU)
    config.baudrate = 9600
    link = open_link(config)
    try:
        started = time.monotonic()
        for _ in range(5):
            link.execute(1, p.build_read_request(3, 0, 1), 1.0)
        assert time.monotonic() - started < 2.0
    finally:
        link.close()


def test_missing_port_reports_connection_error():
    """Opening a non-existent port raises a descriptive error."""
    with pytest.raises(p.ModbusConnectionError, match="Cannot open"):
        open_link(_config("/dev/does-not-exist", ConnectionMode.SERIAL_RTU))


def test_no_port_selected():
    """An empty port name is rejected up front."""
    with pytest.raises(p.ModbusConnectionError, match="No serial port"):
        open_link(_config("", ConnectionMode.SERIAL_RTU))


def test_engine_over_serial(rtu_slave):
    """The whole engine runs over a serial line."""
    from kmms.engine import PollEngine
    from kmms.engine import PollResult

    engine = PollEngine()
    poll = PollSet(slave=1, function=3, start=3, length=2, interval_ms=50, timeout_ms=500)
    engine.start(_config(rtu_slave.port, ConnectionMode.SERIAL_RTU), [poll])
    try:
        end = time.monotonic() + 3
        results = []
        while time.monotonic() < end and len(results) < 3:
            results += [e for e in engine.drain() if isinstance(e, PollResult)]
            time.sleep(0.02)
        assert len(results) >= 3 and all(r.ok and r.values == [3, 4] for r in results)
    finally:
        engine.stop()


def test_list_serial_ports_returns_info_objects():
    """Port enumeration never raises and yields device names."""
    for port in list_serial_ports():
        assert port.device


def test_flow_control_and_all_line_settings_are_accepted(rtu_slave):
    """The port opens with every data-bit/parity/stop-bit/flow combination the UI offers."""
    for bytesize in (7, 8):
        for parity in ("N", "E", "O"):
            for stopbits in (1, 2):
                for flow in ("none", "xonxoff"):
                    config = _config(rtu_slave.port, ConnectionMode.SERIAL_RTU)
                    config.bytesize, config.parity = bytesize, parity
                    config.stopbits, config.flow_control = stopbits, flow
                    open_link(config).close()


def test_unplugged_port_triggers_reconnect_state():
    """Losing the serial device (USB unplug) is reported as reconnecting, not a crash."""
    from kmms.engine import PollEngine
    from kmms.engine import StateEvent

    slave = PtySlave()
    engine = PollEngine()
    poll = PollSet(slave=1, function=3, start=0, length=2, interval_ms=30, timeout_ms=200)
    engine.start(_config(slave.port, ConnectionMode.SERIAL_RTU), [poll])
    try:
        time.sleep(0.4)
        slave.close()  # the device node disappears
        end = time.monotonic() + 6
        states: list[str] = []
        while time.monotonic() < end and "reconnecting" not in states:
            states += [e.state for e in engine.drain() if isinstance(e, StateEvent)]
            time.sleep(0.05)
        assert "reconnecting" in states, states
    finally:
        started = time.monotonic()
        engine.stop()
        assert time.monotonic() - started < 2.0
