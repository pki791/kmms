"""Display formats: turn raw 16-bit register words into text and back."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ColumnFormat:
    """A way of presenting registers. ``words`` is how many registers one value spans."""

    key: str
    title: str
    words: int
    struct_code: str = ""


COLUMN_FORMATS: tuple[ColumnFormat, ...] = (
    ColumnFormat("unsigned", "UInt16", 1, ">H"),
    ColumnFormat("signed", "Int16", 1, ">h"),
    ColumnFormat("hex", "Hex", 1),
    ColumnFormat("binary", "Binary", 1),
    ColumnFormat("ascii", "ASCII", 1),
    ColumnFormat("uint32", "UInt32", 2, ">I"),
    ColumnFormat("int32", "Int32", 2, ">i"),
    ColumnFormat("float32", "Float32", 2, ">f"),
    ColumnFormat("hex32", "Hex32", 2),
    ColumnFormat("uint64", "UInt64", 4, ">Q"),
    ColumnFormat("int64", "Int64", 4, ">q"),
    ColumnFormat("float64", "Float64", 4, ">d"),
)
FORMATS_BY_KEY = {f.key: f for f in COLUMN_FORMATS}

#: Columns shown for coil / discrete input sets.
BIT_COLUMNS = ("Value", "State")


def known_columns(keys: Sequence[str]) -> list[str]:
    """Filter ``keys`` down to known column keys, preserving the canonical order."""
    wanted = set(keys)
    return [f.key for f in COLUMN_FORMATS if f.key in wanted]


def words_to_bytes(words: Sequence[int], byte_swap: bool, word_swap: bool) -> bytes:
    """Convert register words to the big-endian byte string of the combined value."""
    ordered = list(reversed(words)) if word_swap else list(words)
    out = bytearray()
    for word in ordered:
        pair = (word & 0xFFFF).to_bytes(2, "big")
        out += pair[::-1] if byte_swap else pair
    return bytes(out)


def bytes_to_words(data: bytes, byte_swap: bool, word_swap: bool) -> list[int]:
    """Inverse of :func:`words_to_bytes`."""
    words = []
    for index in range(0, len(data), 2):
        pair = data[index : index + 2]
        words.append(int.from_bytes(pair[::-1] if byte_swap else pair, "big"))
    return list(reversed(words)) if word_swap else words


def format_value(key: str, words: Sequence[int], byte_swap: bool = False,
                 word_swap: bool = False) -> str:
    """Format ``words`` (exactly ``words`` registers) according to column ``key``."""
    column = FORMATS_BY_KEY[key]
    if len(words) < column.words:
        return ""
    words = words[: column.words]
    data = words_to_bytes(words, byte_swap, word_swap)
    if key == "hex":
        return f"0x{int.from_bytes(data, 'big'):04X}"
    if key == "hex32":
        return f"0x{int.from_bytes(data, 'big'):08X}"
    if key == "binary":
        bits = f"{int.from_bytes(data, 'big'):016b}"
        return " ".join(bits[i : i + 4] for i in range(0, 16, 4))
    if key == "ascii":
        return "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    value = struct.unpack(column.struct_code, data)[0]
    if key == "float32":
        return _format_float(value, 7)
    if key == "float64":
        return _format_float(value, 15)
    return str(value)


def _format_float(value: float, digits: int) -> str:
    """Format a float compactly; NaN/Inf are spelled out."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return f"{value:.{digits}g}"


def parse_value(key: str, text: str, byte_swap: bool = False,
                word_swap: bool = False) -> list[int]:
    """Parse user input for column ``key`` into register words. Raises ``ValueError``."""
    column = FORMATS_BY_KEY[key]
    text = text.strip()
    if not text:
        raise ValueError("Empty value")
    if key in ("hex", "hex32"):
        number = int(text, 16)
        bits = 16 * column.words
        if not 0 <= number < (1 << bits):
            raise ValueError(f"Value must fit in {bits} bits")
        data = number.to_bytes(2 * column.words, "big")
    elif key == "binary":
        number = int(text.replace(" ", "").replace("_", ""), 2)
        if not 0 <= number <= 0xFFFF:
            raise ValueError("Value must fit in 16 bits")
        data = number.to_bytes(2, "big")
    elif key == "ascii":
        raw = text.encode("latin-1", errors="strict")
        if not 1 <= len(raw) <= 2:
            raise ValueError("Enter one or two characters")
        data = raw.ljust(2, b"\x00")
    elif key in ("float32", "float64"):
        data = struct.pack(column.struct_code, float(text.replace(",", ".")))
    else:
        number = int(text, 0)
        try:
            data = struct.pack(column.struct_code, number)
        except struct.error as exc:
            raise ValueError(f"Value out of range for {column.title}") from exc
    return bytes_to_words(data, byte_swap, word_swap)


def modicon_address(function: int, address: int) -> str:
    """Return the traditional 5/6 digit Modicon notation, e.g. 40001 for holding register 0."""
    prefix = {1: 0, 2: 1, 3: 4, 4: 3}.get(function)
    if prefix is None:
        return str(address)
    if address < 9999:
        return f"{prefix}{address + 1:04d}"
    return f"{prefix}{address + 1:05d}"
