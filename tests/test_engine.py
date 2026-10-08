"""Tests for the polling engine against a real (pymodbus) slave."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from kmms import engine as eng
from kmms.model import ConnectionConfig
from kmms.model import ConnectionMode
from kmms.model import PollSet
from slave_server import SlaveServer


def _config(port: int, mode: ConnectionMode = ConnectionMode.TCP) -> ConnectionConfig:
    """Config for the local test slave."""
    return ConnectionConfig(mode=mode, host="127.0.0.1", tcp_port=port)


class Collector:
    """Drains an engine in the background and lets tests wait for conditions."""

    def __init__(self, engine: eng.PollEngine) -> None:
        """Attach to ``engine``."""
        self.engine = engine
        self.events: list = []

    def pump(self) -> None:
        """Move new engine events into the local list."""
        self.events.extend(self.engine.drain())

    def wait_for(self, predicate, timeout: float = 5.0):
        """Wait until ``predicate(events)`` is truthy and return its value."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump()
            result = predicate(self.events)
            if result:
                return result
            time.sleep(0.01)
        self.pump()
        raise AssertionError(f"Condition not met; events: {self.events[-8:]}")

    def results(self, set_id: str | None = None) -> list:
        """All PollResults (optionally of one set)."""
        return [e for e in self.events if isinstance(e, eng.PollResult)
                and (set_id is None or e.set_id == set_id)]

    def states(self) -> list[str]:
        """The sequence of reported connection states."""
        return [e.state for e in self.events if isinstance(e, eng.StateEvent)]


@pytest.fixture
def engine():
    """A fresh engine that is always stopped afterwards."""
    instance = eng.PollEngine()
    yield instance
    instance.stop()


def test_polls_and_reports_values(engine, tcp_slave):
    """The engine opens the connection, polls and emits ordered state/result events."""
    poll = PollSet(name="A", slave=1, function=3, start=5, length=4, interval_ms=50)
    engine.start(_config(tcp_slave.port), [poll])
    col = Collector(engine)
    result = col.wait_for(lambda ev: next((e for e in ev if isinstance(e, eng.PollResult)), None))
    assert result.ok and result.values == [5, 6, 7, 8] and result.set_id == poll.id
    assert result.duration_ms > 0 and result.timestamp > 0
    assert col.states()[:2] == ["connecting", "open"]
    engine.stop()
    col.pump()
    assert col.states()[-1] == "closed"
    assert not engine.is_running


def test_value_changes_are_seen(engine, tcp_slave):
    """A change on the slave appears in a later poll."""
    poll = PollSet(slave=1, function=3, start=0, length=2, interval_ms=30)
    engine.start(_config(tcp_slave.port), [poll])
    col = Collector(engine)
    col.wait_for(lambda ev: col.results())
    tcp_slave.set_holding(0, [4242, 4343])
    col.wait_for(lambda ev: any(r.values == [4242, 4343] for r in col.results()))


def test_interval_is_respected(engine, tcp_slave):
    """Polls of one set are spaced by roughly the configured interval."""
    poll = PollSet(slave=1, function=3, start=0, length=1, interval_ms=200)
    engine.start(_config(tcp_slave.port), [poll])
    col = Collector(engine)
    col.wait_for(lambda ev: len(col.results()) >= 4, timeout=3)
    stamps = [r.timestamp for r in col.results()[:4]]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(0.17 <= g <= 0.4 for g in gaps), gaps


def test_multiple_sets_have_independent_timing(engine, tcp_slave):
    """A fast set polls more often than a slow one and both keep working."""
    fast = PollSet(name="fast", slave=1, function=3, start=0, length=1, interval_ms=50)
    slow = PollSet(name="slow", slave=7, function=4, start=0, length=2, interval_ms=400)
    engine.start(_config(tcp_slave.port), [fast, slow])
    col = Collector(engine)
    col.wait_for(lambda ev: len(col.results(slow.id)) >= 2, timeout=4)
    assert len(col.results(fast.id)) > 2 * len(col.results(slow.id))
    assert col.results(slow.id)[0].values == [1000, 1001]


def test_large_ranges_are_split(engine, tcp_slave):
    """300 registers (more than the 125 per request limit) are read via several requests."""
    poll = PollSet(slave=1, function=3, start=10, length=300, interval_ms=500)
    engine.start(_config(tcp_slave.port), [poll])
    col = Collector(engine)
    result = col.wait_for(lambda ev: next(iter(col.results()), None))
    assert result.ok and result.values == list(range(10, 310))


def test_coil_set(engine, tcp_slave):
    """Bit areas deliver 0/1 values."""
    poll = PollSet(slave=1, function=1, start=0, length=6, interval_ms=100)
    engine.start(_config(tcp_slave.port), [poll])
    col = Collector(engine)
    result = col.wait_for(lambda ev: next(iter(col.results()), None))
    assert result.values == [1, 0, 1, 0, 1, 0]


def test_exception_response_is_a_failed_poll(engine, tcp_slave):
    """Illegal address -> failed result with kind 'exception' and the error pause applies."""
    poll = PollSet(slave=1, function=3, start=5000, length=2, interval_ms=50, error_pause_ms=300)
    engine.start(_config(tcp_slave.port), [poll])
    col = Collector(engine)
    col.wait_for(lambda ev: len(col.results()) >= 2, timeout=3)
    first, second = col.results()[:2]
    assert not first.ok and first.error_kind == "exception"
    assert "Illegal Data Address" in first.error
    assert second.timestamp - first.timestamp >= 0.28  # pause after error, not the 50 ms interval


class _MuteServer:
    """Accepts TCP connections and never answers."""

    def __init__(self) -> None:
        """Listen on an ephemeral port."""
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.clients: list = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        """Keep the clients open."""
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            self.clients.append(client)

    def close(self) -> None:
        """Close all sockets."""
        for client in self.clients:
            client.close()
        self.sock.close()


def test_timeout_then_pause(engine):
    """Timeouts are reported and followed by the configured pause."""
    server = _MuteServer()
    try:
        poll = PollSet(slave=1, function=3, start=0, length=1, interval_ms=10,
                       timeout_ms=150, error_pause_ms=400)
        engine.start(_config(server.port), [poll])
        col = Collector(engine)
        col.wait_for(lambda ev: len(col.results()) >= 2, timeout=4)
        first, second = col.results()[:2]
        assert not first.ok and first.error_kind == "timeout"
        assert 140 <= first.duration_ms < 400
        gap = second.timestamp - first.timestamp
        assert 0.35 <= gap <= 0.9, gap  # timeout (150) + pause (400) = ~550 ms
    finally:
        engine.stop()
        server.close()


def test_failing_set_does_not_block_a_healthy_one(engine, tcp_slave):
    """While a set sits in its error pause the other set keeps being polled."""
    bad = PollSet(name="bad", slave=1, function=3, start=5000, length=1, interval_ms=50,
                  error_pause_ms=2000)
    good = PollSet(name="good", slave=1, function=3, start=0, length=1, interval_ms=50)
    engine.start(_config(tcp_slave.port), [bad, good])
    col = Collector(engine)
    col.wait_for(lambda ev: len(col.results(good.id)) >= 5, timeout=3)
    assert len(col.results(bad.id)) == 1


def test_connect_failure_reports_error_and_stops(engine):
    """A refused connection ends in an error state followed by closed."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    engine.start(_config(port), [PollSet()])
    col = Collector(engine)
    col.wait_for(lambda ev: "closed" in col.states())
    assert col.states() == ["connecting", "error", "closed"]
    errors = [e for e in col.events if isinstance(e, eng.StateEvent) and e.state == "error"]
    assert "Cannot connect" in errors[0].message


def test_writes(engine, tcp_slave):
    """Manual writes execute, report success and trigger an immediate re-poll."""
    poll = PollSet(slave=1, function=3, start=0, length=3, interval_ms=3000)
    engine.start(_config(tcp_slave.port), [poll])
    col = Collector(engine)
    col.wait_for(lambda ev: col.results())
    ids = [
        engine.write(eng.WriteRequest(1, "register", 1, [777])),
        engine.write(eng.WriteRequest(1, "registers", 10, [1, 2, 3])),
        engine.write(eng.WriteRequest(1, "coil", 4, [1])),
        engine.write(eng.WriteRequest(1, "coils", 20, [1, 0, 1])),
    ]
    col.wait_for(lambda ev: len([e for e in ev if isinstance(e, eng.WriteResult)]) == 4)
    outcomes = {e.request_id: e for e in col.events if isinstance(e, eng.WriteResult)}
    assert all(outcomes[i].ok for i in ids), outcomes
    assert tcp_slave.get_holding(1) == [777] and tcp_slave.get_holding(10, 3) == [1, 2, 3]
    assert tcp_slave.get_coils(4) == [1] and tcp_slave.get_coils(20, 3) == [1, 0, 1]
    # interval is 3 s, so seeing the new value quickly proves the re-poll after the write
    col.wait_for(lambda ev: any(r.values[1] == 777 for r in col.results()), timeout=1.5)


def test_failed_write_is_reported(engine, tcp_slave):
    """A write to a missing address is reported as failed with the slave's exception."""
    engine.start(_config(tcp_slave.port), [PollSet(interval_ms=1000)])
    col = Collector(engine)
    col.wait_for(lambda ev: col.results())
    request_id = engine.write(eng.WriteRequest(1, "register", 5000, [1]))
    result = col.wait_for(
        lambda ev: next((e for e in ev if isinstance(e, eng.WriteResult)), None))
    assert result.request_id == request_id and not result.ok
    assert "Illegal Data Address" in result.error


def test_update_sets_while_running(engine, tcp_slave):
    """Sets can be added, changed and removed on a live connection."""
    first = PollSet(name="one", slave=1, function=3, start=0, length=1, interval_ms=40)
    engine.start(_config(tcp_slave.port), [first])
    col = Collector(engine)
    col.wait_for(lambda ev: col.results(first.id))
    second = PollSet(name="two", slave=1, function=4, start=0, length=1, interval_ms=40)
    engine.update_sets([first, second])
    col.wait_for(lambda ev: col.results(second.id))
    first_disabled = first.copy()
    first_disabled.enabled = False
    engine.update_sets([first_disabled, second])
    time.sleep(0.2)
    col.pump()
    count = len(col.results(first.id))
    time.sleep(0.3)
    col.pump()
    assert len(col.results(first.id)) == count  # no longer polled
    engine.update_sets([second])
    time.sleep(0.2)
    col.pump()
    assert len(col.results(second.id)) > 3


def test_poll_now_overrides_interval(engine, tcp_slave):
    """poll_now triggers an immediate poll of a slow set."""
    poll = PollSet(slave=1, function=3, start=0, length=1, interval_ms=60_000)
    engine.start(_config(tcp_slave.port), [poll])
    col = Collector(engine)
    col.wait_for(lambda ev: col.results())
    engine.poll_now(poll.id)
    col.wait_for(lambda ev: len(col.results()) >= 2, timeout=1)


def test_reconnect_after_connection_loss(engine):
    """When the slave goes away and returns, polling resumes without user action."""
    server = SlaveServer().start()
    port = server.port
    poll = PollSet(slave=1, function=3, start=0, length=2, interval_ms=40, timeout_ms=200)
    engine.start(_config(port), [poll])
    col = Collector(engine)
    col.wait_for(lambda ev: col.results())
    server.stop()
    col.wait_for(lambda ev: "reconnecting" in col.states(), timeout=6)
    before = len(col.results())
    revived = SlaveServer(port=port).start()
    try:
        col.wait_for(lambda ev: col.states().count("open") >= 2, timeout=8)
        col.wait_for(lambda ev: len(col.results()) > before and col.results()[-1].ok, timeout=4)
    finally:
        engine.stop()
        revived.stop()


def test_stop_is_prompt_even_during_long_timeout(engine):
    """Closing while waiting on a 30 s timeout returns quickly."""
    server = _MuteServer()
    try:
        poll = PollSet(slave=1, function=3, start=0, length=1, timeout_ms=30_000)
        engine.start(_config(server.port), [poll])
        time.sleep(0.3)
        started = time.monotonic()
        engine.stop()
        assert time.monotonic() - started < 1.0
        assert not engine.is_running
    finally:
        server.close()


def test_engine_can_be_restarted(engine, tcp_slave):
    """start / stop / start works."""
    poll = PollSet(interval_ms=50)
    for _ in range(2):
        engine.start(_config(tcp_slave.port), [poll])
        col = Collector(engine)
        col.wait_for(lambda ev: any(isinstance(e, eng.PollResult) for e in ev))
        engine.stop()
        assert not engine.is_running


def test_trace_events_are_emitted(engine, tcp_slave):
    """Raw frames are available as TraceEvents."""
    engine.start(_config(tcp_slave.port), [PollSet(interval_ms=100)])
    col = Collector(engine)
    col.wait_for(lambda ev: col.results())
    traces = [e for e in col.events if isinstance(e, eng.TraceEvent)]
    assert {t.direction for t in traces} == {"TX", "RX"}


def test_rtu_over_tcp_mode(engine, rtu_tcp_slave):
    """The RTU-over-TCP mode polls an RTU framed TCP slave."""
    poll = PollSet(slave=1, function=3, start=2, length=3, interval_ms=50)
    engine.start(_config(rtu_tcp_slave.port, ConnectionMode.RTU_OVER_TCP), [poll])
    col = Collector(engine)
    result = col.wait_for(lambda ev: next(iter(col.results()), None))
    assert result.ok and result.values == [2, 3, 4]


def test_very_large_set_is_read_completely(engine, tcp_slave):
    """1000 registers (8 chunks of 125) arrive complete and in order."""
    poll = PollSet(slave=1, function=3, start=0, length=1000, interval_ms=500, timeout_ms=3000)
    engine.start(_config(tcp_slave.port), [poll])
    col = Collector(engine)
    result = col.wait_for(lambda ev: next(iter(col.results()), None), timeout=8)
    assert result.ok and result.values == list(range(1000))
    assert result.duration_ms < 2000
