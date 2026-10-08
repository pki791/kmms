"""Test helper: an independent Modbus slave (pymodbus) running in a background thread."""

from __future__ import annotations

import asyncio
import socket
import threading

from pymodbus import FramerType
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType
from pymodbus.simulator import SimData
from pymodbus.simulator import SimDevice


def free_port() -> int:
    """Return a TCP port that is currently unused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SlaveServer:
    """A pymodbus slave (unit ids 1 and 7) with 1000 items per data area."""

    def __init__(self, framer: FramerType = FramerType.SOCKET, size: int = 1000,
                 port: int = 0) -> None:
        """Prepare the server; call :meth:`start` to run it."""
        self.port = port or free_port()
        self.framer = framer
        self._size = size
        self._loop = asyncio.new_event_loop()
        self._server: ModbusTcpServer | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self._error: BaseException | None = None

    def _make_device(self, device_id: int) -> SimDevice:
        """Create a device whose coils/inputs/registers hold recognisable patterns."""
        size = self._size
        coils = [SimData(0, values=[(i + 1) % 2 == 1 for i in range(size)],
                         datatype=DataType.BITS)]
        discrete = [SimData(0, values=[i % 2 == 1 for i in range(size)],
                            datatype=DataType.BITS)]
        holding = [SimData(0, values=list(range(size)), datatype=DataType.REGISTERS)]
        inputs = [SimData(0, values=[1000 + i for i in range(size)],
                          datatype=DataType.REGISTERS)]
        return SimDevice(device_id, simdata=(coils, discrete, holding, inputs))

    def _run(self) -> None:
        """Thread body: serve until shut down."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except BaseException as exc:  # noqa: BLE001 - surfaced through start()
            self._error = exc
            self._ready.set()

    async def _serve(self) -> None:
        """Create the server inside the running loop, start it and signal readiness."""
        self._server = ModbusTcpServer(
            [self._make_device(1), self._make_device(7)],
            framer=self.framer,
            address=("127.0.0.1", self.port),
        )
        await self._server.serve_forever(background=True)
        self._ready.set()
        await self._server.serving

    def start(self) -> SlaveServer:
        """Start the server and wait until it accepts connections."""
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("Slave server did not start")
        if self._error is not None:
            raise RuntimeError(f"Slave server failed: {self._error!r}")
        return self

    def _call(self, coroutine):
        """Run a server coroutine on the server's loop and return its result."""
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(5)

    def set_holding(self, address: int, values: list[int], device_id: int = 1) -> None:
        """Change holding register values from the test thread."""
        self._call(self._server.async_setValues(device_id, 16, address, values))

    def get_holding(self, address: int, count: int = 1, device_id: int = 1) -> list[int]:
        """Read holding register values."""
        return list(self._call(self._server.async_getValues(device_id, 3, address, count)))

    def get_coils(self, address: int, count: int = 1, device_id: int = 1) -> list[int]:
        """Read coil values."""
        values = self._call(self._server.async_getValues(device_id, 1, address, count))
        return [int(v) for v in values]

    def stop(self) -> None:
        """Shut the server down and stop its loop."""
        if self._server is not None:
            try:
                self._call(self._server.shutdown())
            except Exception:  # noqa: BLE001 - best effort cleanup in tests
                pass
        self._thread.join(5)
