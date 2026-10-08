"""Unit tests for protocol.py."""

from __future__ import annotations

import pytest

from kmms import protocol as p


def test_crc16_known_vectors():
    """CRC reference values: 01 03 00 00 00 01 -> 84 0A and 11 03 00 6B 00 03 -> 76 87."""
    assert p.crc16(bytes.fromhex("010300000001")) == 0x0A84
    assert p.build_rtu_frame(1, bytes.fromhex("0300000001")) == bytes.fromhex("010300000001840A")
    assert p.build_rtu_frame(0x11, bytes.fromhex("03006B0003")) == bytes.fromhex(
        "1103006B00037687"
    )


def test_lrc_known_vector():
    """ASCII reference frame from the Modbus guide: :1103006B00037E."""
    assert p.lrc(bytes.fromhex("1103006B0003")) == 0x7E
    assert p.build_ascii_frame(0x11, bytes.fromhex("03006B0003")) == b":1103006B00037E\r\n"


def test_ascii_frame_roundtrip():
    """ASCII frames encode and decode."""
    pdu = p.build_read_request(3, 0, 10)
    frame = p.build_ascii_frame(5, pdu)
    assert frame.startswith(b":05") and frame.endswith(b"\r\n")
    assert p.decode_ascii_frame(frame) == (5, pdu)


def test_ascii_bad_lrc():
    """A corrupted ASCII frame is rejected."""
    frame = bytearray(p.build_ascii_frame(5, p.build_read_request(3, 0, 10)))
    frame[5] = ord("F") if frame[5] != ord("F") else ord("E")
    with pytest.raises(p.ModbusFrameError):
        p.decode_ascii_frame(bytes(frame))


def test_rtu_roundtrip_and_bad_crc():
    """RTU frames decode, and a flipped bit fails the CRC."""
    pdu = p.build_write_single_register(10, 1234)
    frame = p.build_rtu_frame(3, pdu)
    assert p.decode_rtu_frame(frame) == (3, pdu)
    broken = bytearray(frame)
    broken[3] ^= 0x01
    with pytest.raises(p.ModbusFrameError, match="CRC"):
        p.decode_rtu_frame(bytes(broken))


def test_mbap_frame_layout():
    """MBAP header: transaction, protocol 0, length, unit."""
    frame = p.build_mbap_frame(0x1234, 7, bytes.fromhex("0300000002"))
    assert frame == bytes.fromhex("1234 0000 0006 07 0300000002".replace(" ", ""))


def test_read_registers_response():
    """Register responses decode to integers."""
    pdu = bytes.fromhex("03 04 0001 FFFF".replace(" ", ""))
    assert p.parse_read_response(3, pdu, 2) == [1, 65535]


def test_read_bits_response_lsb_first():
    """Bit responses are packed LSB first and trimmed to the requested count."""
    pdu = bytes.fromhex("01 02 CD 01".replace(" ", ""))
    assert p.parse_read_response(1, pdu, 9) == [1, 0, 1, 1, 0, 0, 1, 1, 1]


def test_exception_response():
    """Exception responses raise with the code and a readable name."""
    with pytest.raises(p.ModbusExceptionResponse) as info:
        p.parse_read_response(3, bytes([0x83, 0x02]), 1)
    assert info.value.code == 2
    assert "Illegal Data Address" in str(info.value)


def test_wrong_byte_count_is_frame_error():
    """A response whose byte count disagrees with the request is rejected."""
    with pytest.raises(p.ModbusFrameError):
        p.parse_read_response(3, bytes.fromhex("0302 0001".replace(" ", "")), 2)


def test_write_multiple_coils_packing():
    """Coils are packed LSB first."""
    pdu = p.build_write_multiple_coils(0, [True, False, True, True, False, False, True, True, True])
    assert pdu == bytes.fromhex("0F 0000 0009 02 CD 01".replace(" ", ""))


def test_write_multiple_registers_pdu():
    """FC16 request layout."""
    pdu = p.build_write_multiple_registers(1, [10, 258])
    assert pdu == bytes.fromhex("10 0001 0002 04 000A 0102".replace(" ", ""))


def test_write_response_must_echo():
    """Write responses must echo address and value."""
    request = p.build_write_single_register(5, 77)
    p.parse_write_response(request, request)
    with pytest.raises(p.ModbusFrameError):
        p.parse_write_response(request, p.build_write_single_register(5, 78))


@pytest.mark.parametrize(
    "function,address,count",
    [(3, 0, 0), (3, 0, 126), (1, 0, 2001), (3, 65535, 2), (3, -1, 1), (3, 70000, 1)],
)
def test_request_limits(function, address, count):
    """Out of range requests are refused before anything is sent."""
    with pytest.raises(ValueError):
        p.build_read_request(function, address, count)


def test_rtu_response_length():
    """Frame length is derived from the function code."""
    assert p.rtu_response_length(b"\x01") is None
    assert p.rtu_response_length(bytes([1, 0x83])) == 5
    assert p.rtu_response_length(bytes([1, 3])) is None
    assert p.rtu_response_length(bytes([1, 3, 4])) == 9
    assert p.rtu_response_length(bytes([1, 6])) == 8
    with pytest.raises(p.ModbusFrameError):
        p.rtu_response_length(bytes([1, 0x2B]))
