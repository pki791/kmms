"""Pure Modbus protocol helpers: PDU building/parsing, framing, CRC and LRC.

Nothing in this module performs I/O, which keeps it trivially testable.
"""

from __future__ import annotations

import struct
from enum import Enum
from enum import IntEnum
from typing import Sequence


class FunctionCode(IntEnum):
    """Modbus function codes supported by the application."""

    READ_COILS = 1
    READ_DISCRETE_INPUTS = 2
    READ_HOLDING_REGISTERS = 3
    READ_INPUT_REGISTERS = 4
    WRITE_SINGLE_COIL = 5
    WRITE_SINGLE_REGISTER = 6
    WRITE_MULTIPLE_COILS = 15
    WRITE_MULTIPLE_REGISTERS = 16


READ_FUNCTIONS = (
    FunctionCode.READ_COILS,
    FunctionCode.READ_DISCRETE_INPUTS,
    FunctionCode.READ_HOLDING_REGISTERS,
    FunctionCode.READ_INPUT_REGISTERS,
)
BIT_READ_FUNCTIONS = (FunctionCode.READ_COILS, FunctionCode.READ_DISCRETE_INPUTS)

FUNCTION_LABELS = {
    FunctionCode.READ_COILS: "01 Read Coils",
    FunctionCode.READ_DISCRETE_INPUTS: "02 Read Discrete Inputs",
    FunctionCode.READ_HOLDING_REGISTERS: "03 Read Holding Registers",
    FunctionCode.READ_INPUT_REGISTERS: "04 Read Input Registers",
    FunctionCode.WRITE_SINGLE_COIL: "05 Write Single Coil",
    FunctionCode.WRITE_SINGLE_REGISTER: "06 Write Single Register",
    FunctionCode.WRITE_MULTIPLE_COILS: "15 Write Multiple Coils",
    FunctionCode.WRITE_MULTIPLE_REGISTERS: "16 Write Multiple Registers",
}

EXCEPTION_NAMES = {
    1: "Illegal Function",
    2: "Illegal Data Address",
    3: "Illegal Data Value",
    4: "Slave Device Failure",
    5: "Acknowledge",
    6: "Slave Device Busy",
    7: "Negative Acknowledge",
    8: "Memory Parity Error",
    10: "Gateway Path Unavailable",
    11: "Gateway Target Device Failed to Respond",
}

MAX_READ_BITS = 2000
MAX_READ_REGISTERS = 125
MAX_WRITE_BITS = 1968
MAX_WRITE_REGISTERS = 123
MAX_PDU_SIZE = 253


class Framing(str, Enum):
    """Wire framing used to wrap a PDU."""

    RTU = "rtu"
    ASCII = "ascii"
    SOCKET = "socket"


class ModbusError(Exception):
    """Base class for all Modbus related failures."""

    kind = "protocol"


class ModbusTimeout(ModbusError):
    """No (complete) response arrived within the configured timeout."""

    kind = "timeout"


class ModbusConnectionError(ModbusError):
    """The underlying port or socket is unusable (closed, unplugged, refused)."""

    kind = "connection"


class ModbusFrameError(ModbusError):
    """A response was malformed, failed its CRC/LRC or did not match the request."""

    kind = "protocol"


class ModbusExceptionResponse(ModbusError):
    """The slave answered with a Modbus exception response."""

    kind = "exception"

    def __init__(self, function: int, code: int) -> None:
        """Store the failing function code and the exception code."""
        self.function = function
        self.code = code
        name = EXCEPTION_NAMES.get(code, "Unknown exception")
        super().__init__(f"Exception {code:02d}: {name} (FC{function:02d})")


def crc16(data: bytes) -> int:
    """Return the Modbus RTU CRC-16 of ``data`` (low byte is sent first)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def lrc(data: bytes) -> int:
    """Return the Modbus ASCII longitudinal redundancy check of ``data``."""
    return (-sum(data)) & 0xFF


# --------------------------------------------------------------------------- PDU builders


def build_read_request(function: int, address: int, count: int) -> bytes:
    """Build a read request PDU for function codes 1-4."""
    if function not in READ_FUNCTIONS:
        raise ValueError(f"Not a read function: {function}")
    limit = MAX_READ_BITS if function in BIT_READ_FUNCTIONS else MAX_READ_REGISTERS
    _check_range(address, count, limit)
    return struct.pack(">BHH", function, address, count)


def build_write_single_coil(address: int, value: bool) -> bytes:
    """Build a Write Single Coil (FC05) request PDU."""
    _check_range(address, 1, 1)
    return struct.pack(">BHH", FunctionCode.WRITE_SINGLE_COIL, address, 0xFF00 if value else 0)


def build_write_single_register(address: int, value: int) -> bytes:
    """Build a Write Single Register (FC06) request PDU."""
    _check_range(address, 1, 1)
    _check_word(value)
    return struct.pack(">BHH", FunctionCode.WRITE_SINGLE_REGISTER, address, value)


def build_write_multiple_coils(address: int, values: Sequence[bool]) -> bytes:
    """Build a Write Multiple Coils (FC15) request PDU."""
    _check_range(address, len(values), MAX_WRITE_BITS)
    packed = bytearray((len(values) + 7) // 8)
    for index, value in enumerate(values):
        if value:
            packed[index // 8] |= 1 << (index % 8)
    header = struct.pack(
        ">BHHB", FunctionCode.WRITE_MULTIPLE_COILS, address, len(values), len(packed)
    )
    return header + bytes(packed)


def build_write_multiple_registers(address: int, values: Sequence[int]) -> bytes:
    """Build a Write Multiple Registers (FC16) request PDU."""
    _check_range(address, len(values), MAX_WRITE_REGISTERS)
    for value in values:
        _check_word(value)
    header = struct.pack(
        ">BHHB", FunctionCode.WRITE_MULTIPLE_REGISTERS, address, len(values), len(values) * 2
    )
    return header + struct.pack(f">{len(values)}H", *values)


def _check_range(address: int, count: int, limit: int) -> None:
    """Validate an address/quantity pair against the Modbus limits."""
    if not 0 <= address <= 0xFFFF:
        raise ValueError(f"Address out of range: {address}")
    if not 1 <= count <= limit:
        raise ValueError(f"Quantity must be 1..{limit}, got {count}")
    if address + count > 0x10000:
        raise ValueError("Address range exceeds 65535")


def _check_word(value: int) -> None:
    """Validate that ``value`` fits into an unsigned 16-bit register."""
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"Register value out of range: {value}")


# --------------------------------------------------------------------------- PDU parsers


def check_exception(request_function: int, pdu: bytes) -> None:
    """Raise :class:`ModbusExceptionResponse` if ``pdu`` is an exception response."""
    if not pdu:
        raise ModbusFrameError("Empty response PDU")
    if pdu[0] == (request_function | 0x80):
        if len(pdu) < 2:
            raise ModbusFrameError("Truncated exception response")
        raise ModbusExceptionResponse(request_function, pdu[1])
    if pdu[0] != request_function:
        raise ModbusFrameError(
            f"Unexpected function code in response: {pdu[0]:#04x} "
            f"(expected {request_function:#04x})"
        )


def parse_read_response(function: int, pdu: bytes, count: int) -> list[int]:
    """Decode a read response PDU into ``count`` register values or 0/1 bit values."""
    check_exception(function, pdu)
    if len(pdu) < 2:
        raise ModbusFrameError("Truncated read response")
    byte_count = pdu[1]
    data = pdu[2:]
    if len(data) != byte_count:
        raise ModbusFrameError(f"Byte count {byte_count} does not match payload {len(data)}")
    if function in BIT_READ_FUNCTIONS:
        expected = (count + 7) // 8
        if byte_count != expected:
            raise ModbusFrameError(f"Expected {expected} data bytes, got {byte_count}")
        return [(data[i // 8] >> (i % 8)) & 1 for i in range(count)]
    if byte_count != count * 2:
        raise ModbusFrameError(f"Expected {count * 2} data bytes, got {byte_count}")
    return list(struct.unpack(f">{count}H", data))


def parse_write_response(request: bytes, pdu: bytes) -> None:
    """Validate a write response: it must echo function, address and value/quantity."""
    function = request[0]
    check_exception(function, pdu)
    if len(pdu) != 5 or pdu[:5] != request[:5]:
        raise ModbusFrameError("Write response does not echo the request")


# --------------------------------------------------------------------------- framing


def build_rtu_frame(slave: int, pdu: bytes) -> bytes:
    """Wrap ``pdu`` in an RTU frame (slave address + PDU + CRC low/high)."""
    body = bytes([slave]) + pdu
    return body + struct.pack("<H", crc16(body))


def build_ascii_frame(slave: int, pdu: bytes) -> bytes:
    """Wrap ``pdu`` in an ASCII frame (':' + hex(slave+pdu+LRC) + CRLF)."""
    body = bytes([slave]) + pdu
    return b":" + (body + bytes([lrc(body)])).hex().upper().encode("ascii") + b"\r\n"


def build_mbap_frame(transaction_id: int, unit: int, pdu: bytes) -> bytes:
    """Wrap ``pdu`` in a Modbus TCP MBAP frame."""
    return struct.pack(">HHHB", transaction_id & 0xFFFF, 0, len(pdu) + 1, unit) + pdu


def decode_rtu_frame(frame: bytes) -> tuple[int, bytes]:
    """Validate the CRC of an RTU ``frame`` and return ``(slave, pdu)``."""
    if len(frame) < 4:
        raise ModbusFrameError("RTU frame too short")
    received = struct.unpack("<H", frame[-2:])[0]
    expected = crc16(frame[:-2])
    if received != expected:
        raise ModbusFrameError(f"CRC error (received {received:04X}, expected {expected:04X})")
    return frame[0], frame[1:-2]


def decode_ascii_frame(frame: bytes) -> tuple[int, bytes]:
    """Validate the LRC of an ASCII ``frame`` and return ``(slave, pdu)``."""
    if not (frame.startswith(b":") and frame.endswith(b"\r\n")):
        raise ModbusFrameError("Malformed ASCII frame")
    try:
        raw = bytes.fromhex(frame[1:-2].decode("ascii"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ModbusFrameError("ASCII frame contains non-hex characters") from exc
    if len(raw) < 4:
        raise ModbusFrameError("ASCII frame too short")
    if lrc(raw[:-1]) != raw[-1]:
        raise ModbusFrameError(f"LRC error (received {raw[-1]:02X}, expected {lrc(raw[:-1]):02X})")
    return raw[0], raw[1:-1]


def rtu_response_length(prefix: bytes) -> int | None:
    """Return the total RTU response length once the first 2-3 bytes are known.

    ``prefix`` holds the bytes received so far. ``None`` means more bytes are needed
    to decide; a :class:`ModbusFrameError` means the function code is unknown.
    """
    if len(prefix) < 2:
        return None
    function = prefix[1]
    if function & 0x80:
        return 5
    if function in (1, 2, 3, 4):
        if len(prefix) < 3:
            return None
        return 3 + prefix[2] + 2
    if function in (5, 6, 15, 16):
        return 8
    raise ModbusFrameError(f"Unknown function code in response: {function:#04x}")


def format_hex(data: bytes) -> str:
    """Return ``data`` as space separated upper-case hex bytes."""
    return data.hex(" ").upper()
