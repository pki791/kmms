"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from pymodbus import FramerType

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from slave_server import SlaveServer  # noqa: E402


@pytest.fixture
def tcp_slave():
    """A Modbus TCP slave (unit ids 1 and 7)."""
    server = SlaveServer(FramerType.SOCKET).start()
    yield server
    server.stop()


@pytest.fixture
def rtu_tcp_slave():
    """A slave speaking RTU framing over a TCP socket."""
    server = SlaveServer(FramerType.RTU).start()
    yield server
    server.stop()


@pytest.fixture
def ascii_tcp_slave():
    """A slave speaking ASCII framing over a TCP socket."""
    server = SlaveServer(FramerType.ASCII).start()
    yield server
    server.stop()
