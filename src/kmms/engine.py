"""Polling engine: one worker thread that owns the connection and schedules all sets.

The engine is deliberately free of any GUI dependency. It publishes everything that happens
(state changes, poll results, write results, raw traffic) as small event objects which a
consumer drains with :meth:`PollEngine.drain`.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Callable
from typing import Optional
from typing import Union

from kmms.model import ConnectionConfig
from kmms.model import PollSet
from kmms.protocol import BIT_READ_FUNCTIONS
from kmms.protocol import MAX_READ_BITS
from kmms.protocol import MAX_READ_REGISTERS
from kmms.protocol import FunctionCode
from kmms.protocol import ModbusConnectionError
from kmms.protocol import ModbusError
from kmms.protocol import build_read_request
from kmms.protocol import build_write_multiple_coils
from kmms.protocol import build_write_multiple_registers
from kmms.protocol import build_write_single_coil
from kmms.protocol import build_write_single_register
from kmms.protocol import parse_read_response
from kmms.protocol import parse_write_response
from kmms.transport import ModbusLink
from kmms.transport import open_link

LOG = logging.getLogger(__name__)

RECONNECT_INTERVAL_S = 1.5
MAX_PENDING_TRACE = 50_000

# Connection states reported by StateEvent.
STATE_CONNECTING = "connecting"
STATE_OPEN = "open"
STATE_RECONNECTING = "reconnecting"
STATE_CLOSED = "closed"
STATE_ERROR = "error"


@dataclass
class StateEvent:
    """The connection state changed."""

    state: str
    message: str = ""


@dataclass
class PollResult:
    """Outcome of one poll of a set."""

    set_id: str
    ok: bool
    values: list = field(default_factory=list)
    error: str = ""
    error_kind: str = ""
    duration_ms: float = 0.0
    timestamp: float = 0.0


@dataclass
class WriteRequest:
    """A manual write to a slave. ``kind`` is coil, coils, register or registers."""

    slave: int
    kind: str
    address: int
    values: list
    timeout_ms: int = 1000

    def describe(self) -> str:
        """Return a short summary used in messages and the log."""
        shown = ", ".join(str(int(v)) for v in self.values[:8])
        if len(self.values) > 8:
            shown += ", ..."
        fc = _WRITE_CODES[self.kind]
        return f"FC{fc:02d} slave {self.slave} @{self.address} = [{shown}]"


@dataclass
class WriteResult:
    """Outcome of a :class:`WriteRequest`."""

    request_id: int
    ok: bool
    summary: str
    error: str = ""


@dataclass
class TraceEvent:
    """Raw bytes sent (TX) or received (RX)."""

    timestamp: float
    direction: str
    data: bytes


Event = Union[StateEvent, PollResult, WriteResult, TraceEvent]
LinkFactory = Callable[..., ModbusLink]

_WRITE_CODES = {
    "coil": int(FunctionCode.WRITE_SINGLE_COIL),
    "coils": int(FunctionCode.WRITE_MULTIPLE_COILS),
    "register": int(FunctionCode.WRITE_SINGLE_REGISTER),
    "registers": int(FunctionCode.WRITE_MULTIPLE_REGISTERS),
}


class _Schedule:
    """Runtime scheduling state of one set."""

    def __init__(self, poll_set: PollSet) -> None:
        """Start due immediately."""
        self.signature = poll_set.timing_signature()
        self.next_due = 0.0


class PollEngine:
    """Owns the connection and polls all enabled sets from a background thread."""

    def __init__(self, link_factory: Optional[LinkFactory] = None) -> None:
        """Create an idle engine. ``link_factory`` allows injecting fake links in tests."""
        self._link_factory = link_factory or open_link
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sets: list[PollSet] = []
        self._poll_now: set[str] = set()
        self._writes: list[tuple[int, WriteRequest]] = []
        self._write_ids = itertools.count(1)
        self._config = ConnectionConfig()

    # ------------------------------------------------------------------ public API

    @property
    def is_running(self) -> bool:
        """Return True while the worker thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, config: ConnectionConfig, sets: list[PollSet]) -> None:
        """Open the connection and start polling ``sets`` in a background thread."""
        if self.is_running:
            raise RuntimeError("Engine already running")
        self._config = ConnectionConfig.from_dict(config.to_dict())
        with self._lock:
            self._sets = [s.copy() for s in sets]
            self._poll_now.clear()
            self._writes.clear()
        self._stop = threading.Event()
        self._wake.clear()
        self._thread = threading.Thread(target=self._run, name="kmms-poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop polling, close the connection and wait for the worker thread to end."""
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        self._wake.set()
        if thread is not threading.current_thread():
            thread.join(timeout)
        if thread.is_alive():
            LOG.warning("Poller thread did not stop within %.1f s", timeout)
        else:
            self._thread = None

    def update_sets(self, sets: list[PollSet]) -> None:
        """Replace the set definitions while running (add/remove/edit take effect at once)."""
        with self._lock:
            self._sets = [s.copy() for s in sets]
        self._wake.set()

    def poll_now(self, set_id: str) -> None:
        """Request an immediate poll of ``set_id``."""
        with self._lock:
            self._poll_now.add(set_id)
        self._wake.set()

    def write(self, request: WriteRequest) -> int:
        """Queue a write; its :class:`WriteResult` carries the returned id."""
        request_id = next(self._write_ids)
        with self._lock:
            self._writes.append((request_id, request))
        self._wake.set()
        return request_id

    def drain(self) -> list[Event]:
        """Return and remove all events produced since the last call."""
        events: list[Event] = []
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            events.append(event)
        return events

    # ------------------------------------------------------------------ worker thread

    def _emit(self, event: Event) -> None:
        """Queue an event for the consumer."""
        if isinstance(event, TraceEvent) and self._events.qsize() >= MAX_PENDING_TRACE:
            return
        self._events.put(event)

    def _trace(self, direction: str, data: bytes) -> None:
        """Trace callback handed to the link."""
        self._emit(TraceEvent(time.time(), direction, data))

    def _run(self) -> None:
        """Thread body: connect, poll until stopped, always report the closed state."""
        stop = self._stop
        link: Optional[ModbusLink] = None
        try:
            self._emit(StateEvent(STATE_CONNECTING, self._config.describe()))
            try:
                link = self._link_factory(self._config, self._trace, stop)
            except ModbusConnectionError as exc:
                self._emit(StateEvent(STATE_ERROR, str(exc)))
                return
            self._emit(StateEvent(STATE_OPEN, self._config.describe()))
            self._poll_loop(link, stop)
        except Exception as exc:  # noqa: BLE001 - the thread must report, not die silently
            LOG.exception("Poller crashed")
            self._emit(StateEvent(STATE_ERROR, f"Internal error: {exc}"))
        finally:
            self._emit(StateEvent(STATE_CLOSED))

    def _poll_loop(self, link: ModbusLink, stop: threading.Event) -> None:
        """Schedule writes and polls until ``stop`` is set; always closes the active link."""
        schedules: dict[str, _Schedule] = {}
        current: Optional[ModbusLink] = link
        try:
            while not stop.is_set():
                if current is None:
                    current = self._reconnect(stop)
                    if current is None:
                        return
                    schedules.clear()
                sets = self._snapshot_sets(schedules)
                try:
                    self._run_writes(current, sets, schedules)
                    did_work = self._run_next_poll(current, sets, schedules)
                except ModbusConnectionError as exc:
                    if stop.is_set():
                        return
                    LOG.warning("Connection lost: %s", exc)
                    current.close()
                    current = None
                    self._emit(StateEvent(STATE_RECONNECTING, str(exc)))
                    continue
                if not did_work:
                    self._sleep_until_next(sets, schedules)
        finally:
            if current is not None:
                current.close()

    def _reconnect(self, stop: threading.Event) -> Optional[ModbusLink]:
        """Try to reopen the connection until it works or the engine is stopped."""
        while not stop.is_set():
            if stop.wait(RECONNECT_INTERVAL_S):
                return None
            try:
                new_link = self._link_factory(self._config, self._trace, stop)
            except ModbusConnectionError as exc:
                self._emit(StateEvent(STATE_RECONNECTING, str(exc)))
                continue
            self._emit(StateEvent(STATE_OPEN, self._config.describe()))
            return new_link
        return None

    def _snapshot_sets(self, schedules: dict[str, _Schedule]) -> list[PollSet]:
        """Take the current set list and sync the schedule table with it."""
        with self._lock:
            sets = list(self._sets)
            forced = set(self._poll_now)
            self._poll_now.clear()
        known = {s.id for s in sets}
        for set_id in list(schedules):
            if set_id not in known:
                del schedules[set_id]
        for poll_set in sets:
            schedule = schedules.get(poll_set.id)
            signature = poll_set.timing_signature()
            if schedule is None:
                schedules[poll_set.id] = _Schedule(poll_set)
            elif schedule.signature != signature:
                schedule.signature = signature
                schedule.next_due = 0.0
            if poll_set.id in forced:
                schedules[poll_set.id].next_due = 0.0
        return sets

    def _run_next_poll(
        self, link: ModbusLink, sets: list[PollSet], schedules: dict[str, _Schedule]
    ) -> bool:
        """Poll the set that has been due the longest. Return False if none is due."""
        now = time.monotonic()
        best: Optional[PollSet] = None
        for poll_set in sets:
            if not poll_set.enabled:
                continue
            due = schedules[poll_set.id].next_due
            if due <= now and (best is None or due < schedules[best.id].next_due):
                best = poll_set
        if best is None:
            return False
        started = time.monotonic()
        timer_start = time.perf_counter()  # monotonic() only ticks every ~15 ms on Windows
        result = self._poll_one(link, best)
        finished = time.monotonic()
        result.duration_ms = (time.perf_counter() - timer_start) * 1000.0
        result.timestamp = time.time()
        self._emit(result)
        schedule = schedules.get(best.id)
        if schedule is not None:
            if result.ok:
                schedule.next_due = max(started + best.interval_ms / 1000.0, finished)
            else:
                schedule.next_due = finished + best.error_pause_ms / 1000.0
        return True

    def _poll_one(self, link: ModbusLink, poll_set: PollSet) -> PollResult:
        """Read the whole range of ``poll_set`` (split into protocol sized requests)."""
        is_bits = poll_set.function in BIT_READ_FUNCTIONS
        chunk_size = MAX_READ_BITS if is_bits else MAX_READ_REGISTERS
        values: list[int] = []
        address = poll_set.start
        remaining = poll_set.length
        try:
            while remaining > 0:
                count = min(remaining, chunk_size)
                pdu = build_read_request(poll_set.function, address, count)
                response = link.execute(poll_set.slave, pdu, poll_set.timeout_ms / 1000.0)
                values.extend(parse_read_response(poll_set.function, response, count))
                address += count
                remaining -= count
        except ModbusConnectionError:
            raise
        except ModbusError as exc:
            return PollResult(poll_set.id, False, error=str(exc), error_kind=exc.kind)
        except ValueError as exc:
            return PollResult(poll_set.id, False, error=str(exc), error_kind="protocol")
        return PollResult(poll_set.id, True, values=values)

    def _run_writes(
        self, link: ModbusLink, sets: list[PollSet], schedules: dict[str, _Schedule]
    ) -> None:
        """Execute queued manual writes and re-poll the affected sets."""
        with self._lock:
            pending = list(self._writes)
            self._writes.clear()
        for index, (request_id, request) in enumerate(pending):
            try:
                self._execute_write(link, request)
            except ModbusConnectionError as exc:
                self._emit(WriteResult(request_id, False, request.describe(), str(exc)))
                # Requests that were not attempted are re-queued for after the reconnect.
                with self._lock:
                    self._writes[:0] = pending[index + 1 :]
                raise
            except (ModbusError, ValueError) as exc:
                self._emit(WriteResult(request_id, False, request.describe(), str(exc)))
                continue
            self._emit(WriteResult(request_id, True, request.describe()))
            count = len(request.values)
            for poll_set in sets:
                overlaps = (
                    poll_set.slave == request.slave
                    and request.address < poll_set.start + poll_set.length
                    and poll_set.start < request.address + count
                )
                if overlaps and poll_set.id in schedules:
                    schedules[poll_set.id].next_due = 0.0

    def _execute_write(self, link: ModbusLink, request: WriteRequest) -> None:
        """Send one write request and validate the echo."""
        if request.kind == "coil":
            pdu = build_write_single_coil(request.address, bool(request.values[0]))
        elif request.kind == "coils":
            pdu = build_write_multiple_coils(request.address, [bool(v) for v in request.values])
        elif request.kind == "register":
            pdu = build_write_single_register(request.address, int(request.values[0]))
        elif request.kind == "registers":
            pdu = build_write_multiple_registers(
                request.address, [int(v) for v in request.values]
            )
        else:
            raise ValueError(f"Unknown write kind: {request.kind}")
        timeout = request.timeout_ms / 1000.0
        if request.slave == 0:  # broadcast: nobody answers
            link.execute(0, pdu, timeout, expect_response=False)
            time.sleep(min(timeout, 0.2))
            return
        response = link.execute(request.slave, pdu, timeout)
        parse_write_response(pdu, response)

    def _sleep_until_next(self, sets: list[PollSet], schedules: dict[str, _Schedule]) -> None:
        """Block until the next set is due or something wakes the thread."""
        now = time.monotonic()
        due_times = [
            schedules[s.id].next_due for s in sets if s.enabled and s.id in schedules
        ]
        delay = max(min(due_times) - now, 0.0) if due_times else 3600.0
        self._wake.wait(min(delay, 3600.0))
        self._wake.clear()
