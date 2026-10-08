"""Transport tests against an independent pymodbus slave plus fault injection."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from kmms import protocol as p
from kmms.model import ConnectionConfig
from kmms.model import ConnectionMode
from kmms.transport import Channel
from kmms.transport import ModbusMaster
from kmms.transport import SocketChannel
from kmms.transport import open_link


def _config(mode: ConnectionMode, port: int) -> ConnectionConfig:
    """Config pointing at the local test slave."""
    return ConnectionConfig(mode=mode, host="127.0.0.1", tcp_port=port)


def _read(link, slave, function, address, count, timeout=1.0):
    """Run a read transaction and decode it."""
    pdu = p.build_read_request(function, address, count)
    return p.parse_read_response(function, link.execute(slave, pdu, timeout), count)


@pytest.mark.parametrize("slave_fixture,mode", [
    ("tcp_slave", ConnectionMode.TCP),
    ("rtu_tcp_slave", ConnectionMode.RTU_OVER_TCP),
])
def test_all_read_functions(request, slave_fixture, mode):
    """FC01-04 work against an independent implementation for TCP and RTU framing."""
    slave = request.getfixturevalue(slave_fixture)
    link = open_link(_config(mode, slave.port))
    try:
        assert _read(link, 1, 3, 0, 5) == [0, 1, 2, 3, 4]
        assert _read(link, 1, 4, 10, 3) == [1010, 1011, 1012]
        assert _read(link, 1, 1, 0, 4) == [1, 0, 1, 0]
        assert _read(link, 1, 2, 0, 4) == [0, 1, 0, 1]
        assert _read(link, 7, 3, 2, 2) == [2, 3]  # second unit id
        assert _read(link, 1, 3, 0, 125) == list(range(125))  # protocol maximum
    finally:
        link.close()


@pytest.mark.parametrize("slave_fixture,mode", [
    ("tcp_slave", ConnectionMode.TCP),
    ("rtu_tcp_slave", ConnectionMode.RTU_OVER_TCP),
])
def test_all_write_functions(request, slave_fixture, mode):
    """FC05/06/15/16 change the slave's data and echo correctly."""
    slave = request.getfixturevalue(slave_fixture)
    link = open_link(_config(mode, slave.port))
    try:
        for pdu in (
            p.build_write_single_register(20, 4321),
            p.build_write_single_coil(30, True),
            p.build_write_multiple_registers(40, [1, 2, 3]),
            p.build_write_multiple_coils(50, [True, True, False, True]),
        ):
            p.parse_write_response(pdu, link.execute(1, pdu, 1.0))
        assert slave.get_holding(20) == [4321]
        assert slave.get_holding(40, 3) == [1, 2, 3]
        assert slave.get_coils(30) == [1]
        assert slave.get_coils(50, 4) == [1, 1, 0, 1]
    finally:
        link.close()


def test_ascii_framing_over_socket(ascii_tcp_slave):
    """ASCII framing is interoperable with pymodbus."""
    channel = SocketChannel(_config(ConnectionMode.TCP, ascii_tcp_slave.port))
    master = ModbusMaster(channel, p.Framing.ASCII)
    try:
        pdu = p.build_read_request(3, 4, 3)
        assert p.parse_read_response(3, master.execute(1, pdu, 1.0), 3) == [4, 5, 6]
        pdu = p.build_write_single_register(9, 99)
        p.parse_write_response(pdu, master.execute(1, pdu, 1.0))
        assert ascii_tcp_slave.get_holding(9) == [99]
    finally:
        master.close()


def test_exception_from_slave(tcp_slave):
    """Reading outside the slave's data space yields an exception response."""
    link = open_link(_config(ConnectionMode.TCP, tcp_slave.port))
    try:
        with pytest.raises(p.ModbusExceptionResponse) as info:
            _read(link, 1, 3, 5000, 2)
        assert info.value.code == 2
    finally:
        link.close()


def test_trace_reports_raw_frames(tcp_slave):
    """Every transaction produces a TX and an RX trace entry."""
    seen = []
    link = open_link(_config(ConnectionMode.TCP, tcp_slave.port),
                     trace=lambda direction, data: seen.append((direction, data)))
    try:
        _read(link, 1, 3, 0, 2)
    finally:
        link.close()
    assert [d for d, _ in seen] == ["TX", "RX"]
    assert seen[0][1][-5:] == bytes.fromhex("0300000002")
    assert len(seen[1][1]) == 7 + 2 + 4  # MBAP + fc + bytecount + 2 registers


def test_connection_refused():
    """Connecting to a closed port raises a ModbusConnectionError."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(p.ModbusConnectionError):
        open_link(_config(ConnectionMode.TCP, port))


class _SilentServer:
    """TCP server that accepts connections but never answers (for timeout tests)."""

    def __init__(self) -> None:
        """Listen on an ephemeral port."""
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.clients: list = []
        self.script = None
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        """Accept clients and optionally run a scripted reply."""
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            self.clients.append(client)
            if self.script:
                threading.Thread(target=self.script, args=(client,), daemon=True).start()

    def close(self) -> None:
        """Close everything."""
        for client in self.clients:
            client.close()
        self.sock.close()


def test_timeout_is_honoured():
    """A silent slave produces ModbusTimeout after roughly the configured time."""
    server = _SilentServer()
    link = open_link(_config(ConnectionMode.TCP, server.port))
    try:
        started = time.monotonic()
        with pytest.raises(p.ModbusTimeout):
            _read(link, 1, 3, 0, 1, timeout=0.3)
        assert 0.25 <= time.monotonic() - started < 0.6
    finally:
        link.close()
        server.close()


def test_partial_response_times_out():
    """A response that stops half way is reported as a timeout, not a hang."""
    server = _SilentServer()

    def script(client):
        """Send only the MBAP header, then go quiet."""
        client.recv(64)
        client.sendall(bytes.fromhex("0001 0000 0007 01".replace(" ", "")))

    server.script = script
    link = open_link(_config(ConnectionMode.TCP, server.port))
    try:
        with pytest.raises(p.ModbusTimeout, match="Incomplete"):
            _read(link, 1, 3, 0, 2, timeout=0.3)
    finally:
        link.close()
        server.close()


def test_stale_response_is_skipped():
    """A late response of an earlier transaction (wrong transaction id) is ignored."""
    server = _SilentServer()

    def script(client):
        """Answer with a stale frame first, then the real one."""
        request = client.recv(64)
        transaction = request[:2]
        stale = bytes.fromhex("FFFF 0000 0005 01 03 02 0063".replace(" ", ""))
        good = transaction + bytes.fromhex("0000 0005 01 03 02 0007".replace(" ", ""))
        client.sendall(stale + good)

    server.script = script
    link = open_link(_config(ConnectionMode.TCP, server.port))
    try:
        assert _read(link, 1, 3, 0, 1) == [7]
    finally:
        link.close()
        server.close()


def test_connection_closed_by_peer():
    """A peer that hangs up is reported as a connection error (so the engine reconnects)."""
    server = _SilentServer()

    def script(client):
        """Close immediately after the request arrives."""
        client.recv(64)
        client.close()

    server.script = script
    link = open_link(_config(ConnectionMode.TCP, server.port))
    try:
        with pytest.raises(p.ModbusConnectionError):
            _read(link, 1, 3, 0, 1)
    finally:
        link.close()
        server.close()


class _ScriptedChannel:
    """In-memory channel replaying prepared bytes, used for RTU/ASCII fault injection."""

    def __init__(self, reply: bytes) -> None:
        """Store the bytes that will be 'received'."""
        self.reply = bytearray(reply)
        self.sent = bytearray()

    def write(self, data: bytes) -> None:
        """Record sent bytes."""
        self.sent += data

    def read_available(self, max_bytes: int, timeout: float) -> bytes:
        """Return up to ``max_bytes`` of the prepared reply."""
        if not self.reply:
            time.sleep(min(timeout, 0.01))
            return b""
        chunk = bytes(self.reply[:max_bytes])
        del self.reply[:max_bytes]
        return chunk

    def discard_input(self) -> None:
        """Nothing buffered besides the scripted reply."""

    def close(self) -> None:
        """Nothing to close."""


def _rtu_master(reply: bytes) -> ModbusMaster:
    """Master over a scripted channel with RTU framing."""
    channel: Channel = _ScriptedChannel(reply)
    return ModbusMaster(channel, p.Framing.RTU)


def test_rtu_good_response():
    """A well formed RTU reply is decoded."""
    reply = p.build_rtu_frame(1, bytes.fromhex("030200AB"))
    pdu = _rtu_master(reply).execute(1, p.build_read_request(3, 0, 1), 0.2)
    assert p.parse_read_response(3, pdu, 1) == [0xAB]


def test_rtu_crc_error_detected():
    """A corrupted RTU reply raises a frame error mentioning the CRC."""
    reply = bytearray(p.build_rtu_frame(1, bytes.fromhex("030200AB")))
    reply[3] ^= 0xFF
    with pytest.raises(p.ModbusFrameError, match="CRC"):
        _rtu_master(bytes(reply)).execute(1, p.build_read_request(3, 0, 1), 0.2)


def test_rtu_wrong_slave_detected():
    """A reply from another slave address is not accepted."""
    reply = p.build_rtu_frame(2, bytes.fromhex("030200AB"))
    with pytest.raises(p.ModbusFrameError, match="slave 2"):
        _rtu_master(reply).execute(1, p.build_read_request(3, 0, 1), 0.2)


def test_rtu_exception_frame():
    """RTU exception responses are 5 bytes long and decoded."""
    reply = p.build_rtu_frame(1, bytes([0x83, 0x02]))
    pdu = _rtu_master(reply).execute(1, p.build_read_request(3, 0, 1), 0.2)
    with pytest.raises(p.ModbusExceptionResponse):
        p.parse_read_response(3, pdu, 1)


def test_rtu_truncated_reply_times_out():
    """Half a frame is a timeout."""
    reply = p.build_rtu_frame(1, bytes.fromhex("030200AB"))[:-3]
    with pytest.raises(p.ModbusTimeout):
        _rtu_master(reply).execute(1, p.build_read_request(3, 0, 1), 0.15)


def test_ascii_garbage_before_start_is_skipped():
    """Line noise before the ':' of an ASCII frame is ignored."""
    reply = b"\x00\xffzz" + p.build_ascii_frame(1, bytes.fromhex("030200AB"))
    master = ModbusMaster(_ScriptedChannel(reply), p.Framing.ASCII)
    pdu = master.execute(1, p.build_read_request(3, 0, 1), 0.3)
    assert p.parse_read_response(3, pdu, 1) == [0xAB]


def test_abort_event_interrupts_wait():
    """Setting the abort flag ends a blocking wait promptly."""
    abort = threading.Event()
    master = ModbusMaster(_ScriptedChannel(b""), p.Framing.RTU, abort=abort)
    threading.Timer(0.1, abort.set).start()
    started = time.monotonic()
    with pytest.raises(p.ModbusConnectionError):
        master.execute(1, p.build_read_request(3, 0, 1), 10.0)
    assert time.monotonic() - started < 1.0
