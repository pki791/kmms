"""Unit tests for model.py (project files) and formats.py."""

from __future__ import annotations

import json
import math

import pytest

from kmms import formats
from kmms.model import ConnectionConfig
from kmms.model import ConnectionMode
from kmms.model import PollSet
from kmms.model import Project
from kmms.model import ProjectError
from kmms.model import WindowState


def _sample_project() -> Project:
    """Build a project that exercises every field."""
    return Project(
        connection=ConnectionConfig(
            mode=ConnectionMode.SERIAL_ASCII,
            serial_port="COM7",
            baudrate=115200,
            bytesize=7,
            parity="E",
            stopbits=1.5,
            flow_control="rtscts",
            host="10.0.0.5",
            tcp_port=1502,
        ),
        sets=[
            PollSet(name="Temps", slave=5, function=4, start=100, length=300,
                    interval_ms=250, timeout_ms=400, error_pause_ms=9000,
                    columns=["signed", "float32"], byte_swap=True, word_swap=True,
                    window=WindowState(x=1, y=2, width=300, height=200, visible=False,
                                       minimized=True)),
            PollSet(name="Coils", slave=1, function=1, start=0, length=16, enabled=False),
        ],
        log_visible=False,
        view_mode="tabs",
    )


def test_project_roundtrip(tmp_path):
    """Saving and loading preserves every setting."""
    project = _sample_project()
    path = tmp_path / "p.kmms"
    project.save(path)
    loaded = Project.load(path)
    assert loaded.to_dict() == project.to_dict()
    assert loaded.connection.mode is ConnectionMode.SERIAL_ASCII
    assert loaded.sets[0].window.minimized is True
    assert loaded.sets[1].enabled is False


def test_project_file_is_readable_json(tmp_path):
    """The file is human readable, versioned JSON."""
    path = tmp_path / "p.kmms"
    _sample_project().save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format"] == "kmms-project" and data["version"] == 1
    assert [s["name"] for s in data["sets"]] == ["Temps", "Coils"]
    assert not list(tmp_path.glob(".kmms-*"))  # temp file cleaned up


def test_load_rejects_garbage(tmp_path):
    """Invalid files give a ProjectError, never a crash."""
    path = tmp_path / "bad.kmms"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ProjectError):
        Project.load(path)
    path.write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(ProjectError):
        Project.load(path)
    with pytest.raises(ProjectError):
        Project.load(tmp_path / "missing.kmms")


@pytest.mark.parametrize(
    "patch",
    [
        {"slave": 300},
        {"function": 6},
        {"start": 65535, "length": 5},
        {"interval_ms": -1},
        {"name": 5},
        {"enabled": "yes"},
        {"timeout_ms": 1.5},
    ],
)
def test_invalid_set_values_rejected(patch):
    """Out-of-range or mistyped set fields are reported."""
    data = PollSet().to_dict()
    data.update(patch)
    with pytest.raises(ProjectError):
        PollSet.from_dict(data)


def test_newer_version_rejected():
    """Files from a future version are refused with a clear message."""
    data = Project().to_dict()
    data["version"] = 99
    with pytest.raises(ProjectError, match="newer"):
        Project.from_dict(data)


def test_duplicate_set_ids_are_repaired():
    """Duplicated ids (hand edited files) are made unique."""
    data = Project(sets=[PollSet(id="x"), PollSet(id="x")]).to_dict()
    loaded = Project.from_dict(data)
    assert loaded.sets[0].id != loaded.sets[1].id


# ---------------------------------------------------------------------------- formats


def test_basic_formats():
    """16 bit presentations."""
    assert formats.format_value("unsigned", [65535]) == "65535"
    assert formats.format_value("signed", [65535]) == "-1"
    assert formats.format_value("hex", [0x1A2B]) == "0x1A2B"
    assert formats.format_value("binary", [0x1A2B]) == "0001 1010 0010 1011"
    assert formats.format_value("ascii", [0x4F4B]) == "OK"


def test_32_bit_word_orders():
    """The four classic 32-bit orderings (ABCD, CDAB, BADC, DCBA)."""
    words = [0x1234, 0x5678]
    assert formats.format_value("uint32", words) == str(0x12345678)
    assert formats.format_value("uint32", words, word_swap=True) == str(0x56781234)
    assert formats.format_value("uint32", words, byte_swap=True) == str(0x34127856)
    assert formats.format_value("uint32", words, True, True) == str(0x78563412)


def test_float32_and_float64():
    """IEEE floats decode correctly: 0x41200000 is 10.0."""
    assert formats.format_value("float32", [0x4120, 0x0000]) == "10"
    assert formats.format_value("float32", [0x0000, 0x4120], word_swap=True) == "10"
    assert formats.format_value("float64", [0x4024, 0, 0, 0]) == "10"
    assert formats.format_value("float32", [0x7FC0, 0]) == "NaN"
    assert formats.format_value("float32", [0x7F80, 0]) == "+Inf"


def test_short_input_gives_empty_text():
    """Not enough registers for a wide format (end of a table) renders blank."""
    assert formats.format_value("float32", [1]) == ""


@pytest.mark.parametrize(
    "key,text",
    [
        ("unsigned", "1234"),
        ("signed", "-1234"),
        ("hex", "0xBEEF"),
        ("binary", "1010 0000 1111 0101"),
        ("uint32", "4000000000"),
        ("int32", "-2000000000"),
        ("float32", "3.5"),
        ("float64", "-123.456"),
        ("uint64", "18446744073709551615"),
        ("hex32", "DEADBEEF"),
    ],
)
@pytest.mark.parametrize("byte_swap,word_swap", [(False, False), (True, False), (False, True),
                                                  (True, True)])
def test_parse_format_roundtrip(key, text, byte_swap, word_swap):
    """Whatever the user types, formatting the parsed words gives it back."""
    words = formats.parse_value(key, text, byte_swap, word_swap)
    assert len(words) == formats.FORMATS_BY_KEY[key].words
    shown = formats.format_value(key, words, byte_swap, word_swap)
    if key in ("float32", "float64"):
        assert math.isclose(float(shown), float(text), rel_tol=1e-6)
    elif key in ("hex", "hex32"):
        assert int(shown, 16) == int(text.replace("0x", ""), 16)
    elif key == "binary":
        assert shown.replace(" ", "") == text.replace(" ", "")
    else:
        assert shown == text


@pytest.mark.parametrize(
    "key,text",
    [("unsigned", "70000"), ("unsigned", "-1"), ("signed", "40000"), ("hex", "10000"),
     ("unsigned", "abc"), ("float32", "x"), ("binary", "102"), ("ascii", "abc"),
     ("unsigned", "")],
)
def test_parse_rejects_bad_input(key, text):
    """Invalid user input raises ValueError."""
    with pytest.raises(ValueError):
        formats.parse_value(key, text)


def test_modicon_addresses():
    """Traditional addressing notation."""
    assert formats.modicon_address(3, 0) == "40001"
    assert formats.modicon_address(4, 9) == "30010"
    assert formats.modicon_address(1, 0) == "00001"
    assert formats.modicon_address(2, 0) == "10001"
    assert formats.modicon_address(3, 65535) == "465536"


def test_known_columns_keeps_canonical_order():
    """Unknown keys are dropped and the order is stable."""
    assert formats.known_columns(["float32", "bogus", "unsigned"]) == ["unsigned", "float32"]


def test_invalid_connection_values_rejected():
    """Unknown flow control, parity or stop bits in a file are reported."""
    base = ConnectionConfig().to_dict()
    for patch in ({"flow_control": "dtr"}, {"parity": "X"}, {"stopbits": 3}, {"baudrate": 1},
                  {"tcp_port": 70000}, {"mode": "carrier-pigeon"}):
        with pytest.raises(ProjectError):
            ConnectionConfig.from_dict({**base, **patch})


def test_old_files_without_flow_control_still_load():
    """Files written before flow control existed default to none."""
    data = ConnectionConfig().to_dict()
    del data["flow_control"]
    assert ConnectionConfig.from_dict(data).flow_control == "none"


def test_connection_description():
    """The status bar text shows the serial settings compactly."""
    config = ConnectionConfig(mode=ConnectionMode.SERIAL_RTU, serial_port="COM3", baudrate=19200,
                              parity="E", stopbits=1, flow_control="rtscts")
    assert config.describe() == "COM3 19200 8E1 RTS/CTS"
    assert ConnectionConfig(host="10.0.0.1", tcp_port=1502).describe() == "10.0.0.1:1502"
