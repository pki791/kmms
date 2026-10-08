"""Tests for the register table model: formatting, highlight logic and editing."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from kmms.gui import theme
from kmms.gui.register_model import ERROR_BLINK_S
from kmms.gui.register_model import RegisterTableModel
from kmms.model import PollSet


@pytest.fixture(autouse=True)
def _theme(qapp):
    """Use the light theme for predictable colors."""
    theme.apply(qapp, theme.LIGHT)


def _model(**kwargs) -> RegisterTableModel:
    """Model for a small holding register set."""
    defaults = dict(start=100, length=4, function=3, columns=["unsigned", "hex", "float32"])
    defaults.update(kwargs)
    model = RegisterTableModel(PollSet(**defaults))
    model.set_timing(0.35, 1.5)
    return model


def _text(model: RegisterTableModel, row: int, key: str) -> str:
    """Display text of a cell addressed by column key."""
    return model.data(model.index(row, model.columns.index(key)), Qt.ItemDataRole.DisplayRole)


def _bg(model: RegisterTableModel, row: int, key: str):
    """Background color of a cell."""
    return model.data(model.index(row, model.columns.index(key)), Qt.ItemDataRole.BackgroundRole)


def test_columns_and_addresses():
    """Rows carry real addresses; columns follow the set definition in canonical order."""
    model = _model(columns=["float32", "unsigned"])
    assert model.columns == ["addr", "unsigned", "float32"]
    assert model.rowCount() == 4
    assert [_text(model, r, "addr") for r in range(4)] == ["100", "101", "102", "103"]
    assert model.headerData(1, Qt.Orientation.Horizontal) == "UInt16"


def test_values_render_in_all_formats():
    """Text appears after the first poll; 32 bit columns span two registers."""
    model = _model()
    assert _text(model, 0, "unsigned") == ""  # nothing polled yet
    model.apply_values([0x4120, 0x0000, 1, 2], now=1.0)
    assert _text(model, 0, "unsigned") == "16672"
    assert _text(model, 0, "hex") == "0x4120"
    assert _text(model, 0, "float32") == "10"
    assert _text(model, 3, "float32") == ""  # no partner register for the last row


def test_bit_set_columns():
    """Coil sets show value and ON/OFF state."""
    model = _model(function=1, length=3)
    assert model.columns == ["addr", "value", "state"]
    model.apply_values([1, 0, 1], now=1.0)
    assert [_text(model, r, "state") for r in range(3)] == ["ON", "OFF", "ON"]


def test_first_poll_flashes_green_but_is_not_a_change():
    """Initial load: green 'updated' flash, no amber 'changed' mark."""
    model = _model(columns=["unsigned"])
    model.apply_values([1, 2, 3, 4], now=10.0)
    model.tick(10.0)
    green = _bg(model, 0, "unsigned")
    assert green is not None
    assert green.green() > green.red()  # greenish, not amber
    model.tick(10.0 + 0.5)  # flash over
    assert _bg(model, 0, "unsigned") is None


def test_update_flash_fades_out():
    """The green flash strength decreases with time and ends after the flash duration."""
    model = _model(columns=["unsigned"])
    model.apply_values([1, 2, 3, 4], now=10.0)
    model.tick(10.01)
    strong = _bg(model, 0, "unsigned")
    model.tick(10.30)
    weak = _bg(model, 0, "unsigned")
    base = theme.current().base
    assert abs(strong.green() - int(base[3:5], 16)) != 0
    assert (abs(strong.red() - 255) + abs(strong.blue() - 255)) > \
           (abs(weak.red() - 255) + abs(weak.blue() - 255))
    model.tick(10.36)
    assert _bg(model, 0, "unsigned") is None


def test_changed_value_gets_amber_and_only_that_row():
    """A changed register is marked differently (amber) from merely updated ones (green)."""
    model = _model(columns=["unsigned"])
    model.apply_values([1, 2, 3, 4], now=10.0)
    model.apply_values([1, 2, 99, 4], now=11.0)
    model.tick(11.0)
    changed = _bg(model, 2, "unsigned")
    updated = _bg(model, 1, "unsigned")
    assert changed.red() > changed.blue() + 60  # amber: red high, blue low
    assert updated.green() > updated.red()  # green
    assert changed != updated
    model.tick(11.0 + 1.0)
    assert _bg(model, 2, "unsigned") is not None  # amber outlives the green flash
    assert _bg(model, 1, "unsigned") is None
    assert model.tick(11.0 + 1.6) is False
    assert _bg(model, 2, "unsigned") is None


def test_unchanged_repoll_is_only_an_update():
    """Polling the same values again flashes green but never amber."""
    model = _model(columns=["unsigned"])
    model.apply_values([1, 2, 3, 4], now=10.0)
    model.apply_values([1, 2, 3, 4], now=12.0)
    model.tick(12.0)
    color = _bg(model, 0, "unsigned")
    assert color.green() > color.red()


def test_multi_register_cell_highlights_when_either_register_changes():
    """A float32 cell spans two rows and flags a change in the second one as well."""
    model = _model(columns=["float32"], length=4)
    model.apply_values([0, 0, 0, 0], now=10.0)
    model.apply_values([0, 0, 0, 5], now=11.0)
    model.tick(11.9)  # flash has ended, only 'changed' remains
    assert _bg(model, 2, "float32") is not None  # registers 2+3
    assert _bg(model, 1, "float32") is None  # registers 1+2: untouched


def test_error_blinks_red_then_stays_tinted_until_success():
    """Errors flash red, fade to a faint permanent tint and clear on the next success."""
    model = _model(columns=["unsigned"])
    model.apply_values([1, 2, 3, 4], now=10.0)
    model.apply_error(now=20.0)
    model.tick(20.0)
    peak = _bg(model, 0, "unsigned")
    assert peak.red() > peak.green() + 60
    model.tick(20.0 + ERROR_BLINK_S + 1)
    tint = _bg(model, 0, "unsigned")
    assert tint is not None and tint.red() > tint.green()  # persistent faint red
    assert (255 - tint.green()) < (255 - peak.green())  # fainter than the blink
    assert _text(model, 0, "unsigned") == "1"  # last known value stays visible
    model.apply_values([1, 2, 3, 4], now=30.0)
    model.tick(30.6)
    assert _bg(model, 0, "unsigned") is None


def test_animation_stops_when_nothing_is_active():
    """tick() reports whether more frames are needed."""
    model = _model()
    model.apply_values([1, 2, 3, 4], now=10.0)
    assert model.tick(10.1) is True
    assert model.tick(20.0) is False


def test_length_mismatch_is_ignored():
    """A result for an older definition (other length) must not corrupt the table."""
    model = _model()
    assert model.apply_values([1, 2], now=1.0) is False
    assert not model.has_values()


def test_configure_resets_values():
    """Changing the definition clears old values."""
    model = _model()
    model.apply_values([1, 2, 3, 4], now=1.0)
    model.configure(PollSet(start=0, length=2, columns=["hex"]))
    assert model.rowCount() == 2 and not model.has_values()


def test_editing_requires_connection_and_writable_area():
    """Only holding register value cells are editable, and only while connected."""
    model = _model(columns=["unsigned"])
    index = model.index(0, 1)
    assert not (model.flags(index) & Qt.ItemFlag.ItemIsEditable)
    model.set_write_enabled(True)
    assert model.flags(index) & Qt.ItemFlag.ItemIsEditable
    assert not (model.flags(model.index(0, 0)) & Qt.ItemFlag.ItemIsEditable)  # address
    readonly = _model(function=4, columns=["unsigned"])
    readonly.set_write_enabled(True)
    assert not (readonly.flags(readonly.index(0, 1)) & Qt.ItemFlag.ItemIsEditable)
    wide = _model(columns=["float32"])
    wide.set_write_enabled(True)
    assert wide.flags(wide.index(2, 1)) & Qt.ItemFlag.ItemIsEditable
    assert not (wide.flags(wide.index(3, 1)) & Qt.ItemFlag.ItemIsEditable)  # needs 2 registers


def test_edit_emits_write_request():
    """Entering a value requests a write at the row's address."""
    model = _model(columns=["unsigned", "hex"])
    model.set_write_enabled(True)
    seen = []
    model.write_requested.connect(lambda address, words: seen.append((address, words)))
    assert model.setData(model.index(2, 1), "1234") is False  # table updates via re-poll
    assert model.setData(model.index(3, 2), "0xBEEF") is False
    assert seen == [(102, [1234]), (103, [0xBEEF])]


def test_edit_32bit_honours_word_order():
    """A float32 edit produces two words in the configured order."""
    model = _model(columns=["float32"], word_swap=True)
    model.set_write_enabled(True)
    seen = []
    model.write_requested.connect(lambda address, words: seen.append((address, words)))
    model.setData(model.index(0, 1), "10.0")
    assert seen == [(100, [0x0000, 0x4120])]


def test_invalid_edit_reports_error():
    """Garbage input does not write; the failure is reported."""
    model = _model(columns=["unsigned"])
    model.set_write_enabled(True)
    errors, writes = [], []
    model.edit_failed.connect(errors.append)
    model.write_requested.connect(lambda *a: writes.append(a))
    model.setData(model.index(0, 1), "70000")
    model.setData(model.index(0, 1), "abc")
    assert len(errors) == 2 and not writes


def test_tooltip_shows_notation_and_raw_value():
    """Hovering explains the address and raw register content."""
    model = _model(columns=["unsigned"])
    model.apply_values([0x1234, 0, 0, 0], now=1.0)
    tip = model.data(model.index(0, 1), Qt.ItemDataRole.ToolTipRole)
    assert "Address 100" in tip and "40101" in tip and "1234" in tip
