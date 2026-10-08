"""Integration tests: the main window driving the real engine against a pymodbus slave."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from kmms import engine as eng
from kmms.gui import theme
from kmms.gui.app_settings import AppSettings
from kmms.gui.main_window import MainWindow
from kmms.model import ConnectionConfig
from kmms.model import ConnectionMode
from kmms.model import PollSet
from kmms.model import Project


def make_settings(tmp_path: Path) -> AppSettings:
    """Settings stored in a temp dir so tests never touch the real user settings."""
    store = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    return AppSettings(store, tmp_path / "data")


@pytest.fixture
def window(qtbot, tmp_path):
    """A shown, non-interactive main window."""
    theme.apply(QApplication.instance(), theme.LIGHT)
    win = MainWindow(make_settings(tmp_path), interactive=False)
    qtbot.addWidget(win)
    win.resize(1100, 720)
    win.show()
    yield win
    win.engine.stop()


def connect_to(win: MainWindow, slave) -> None:
    """Point the connection bar at ``slave`` and open the connection."""
    win.connection_bar.set_config(ConnectionConfig(
        mode=ConnectionMode.TCP, host="127.0.0.1", tcp_port=slave.port))
    win.open_connection()


def cell(panel, row: int, key: str) -> str:
    """Display text of a table cell."""
    model = panel.model
    return model.data(model.index(row, model.columns.index(key)), Qt.ItemDataRole.DisplayRole)


def test_add_sets_creates_windows(window):
    """Each set gets its own window; names stay unique."""
    first = window.insert_set(PollSet(name="A"))
    second = window.insert_set(PollSet(name="B", start=20))
    assert set(window.panels) == {first.id, second.id}
    assert all(not sub.isHidden() for sub in window.subwindows.values())
    assert window.hint.isHidden()  # the empty-workspace hint disappears
    assert window._unique_name("A") == "A 2"
    assert window.dirty


def test_polling_shows_values_and_counts(window, qtbot, tcp_slave):
    """Open the connection: values and counters appear, status bar totals follow."""
    poll = window.insert_set(PollSet(name="Regs", slave=1, function=3, start=10, length=5,
                                     interval_ms=50))
    connect_to(window, tcp_slave)
    panel = window.panels[poll.id]
    qtbot.waitUntil(lambda: panel.counters.ok >= 3, timeout=5000)
    assert [cell(panel, r, "unsigned") for r in range(5)] == ["10", "11", "12", "13", "14"]
    assert window.connection_bar.state == eng.STATE_OPEN
    assert "Connected" in window.status_state.text()
    qtbot.waitUntil(lambda: f"{window.totals.ok:,}" in window.status_ok.text())
    assert window.totals.ok >= 3 and window.totals.errors == 0


def test_value_change_is_detected_live(window, qtbot, tcp_slave):
    """A register changed on the slave shows the new value."""
    poll = window.insert_set(PollSet(slave=1, function=3, start=0, length=3, interval_ms=40))
    connect_to(window, tcp_slave)
    panel = window.panels[poll.id]
    qtbot.waitUntil(lambda: panel.counters.ok >= 2, timeout=5000)
    tcp_slave.set_holding(1, [4321])
    qtbot.waitUntil(lambda: cell(panel, 1, "unsigned") == "4321", timeout=5000)
    assert panel.model._changed_at[1] > 0  # amber highlight timestamp recorded
    assert panel.model._changed_at[0] == float("-inf")


def test_errors_are_counted_per_set_and_in_status_bar(window, qtbot, tcp_slave):
    """Failed reads increment the error counters and the red status."""
    bad = window.insert_set(PollSet(name="bad", slave=1, function=3, start=5000, length=2,
                                    interval_ms=50, error_pause_ms=100))
    good = window.insert_set(PollSet(name="good", slave=1, function=3, start=0, length=2,
                                     interval_ms=50))
    connect_to(window, tcp_slave)
    qtbot.waitUntil(lambda: window.panels[bad.id].counters.errors >= 2
                    and window.panels[good.id].counters.ok >= 2, timeout=5000)
    assert window.panels[bad.id].counters.ok == 0
    assert window.panels[good.id].counters.errors == 0
    assert "Illegal Data Address" in window.panels[bad.id].counters.last_error
    qtbot.waitUntil(lambda: "Errors:" in window.status_err.text()
                    and f"{window.totals.errors:,}" in window.status_err.text())
    assert window.totals.errors >= 2 and window.totals.ok >= 2
    assert window.panels[bad.id].model._stale


def test_close_connection_resets_state(window, qtbot, tcp_slave):
    """Closing stops polling, updates the bar and disables writing."""
    poll = window.insert_set(PollSet(slave=1, function=3, interval_ms=40))
    connect_to(window, tcp_slave)
    panel = window.panels[poll.id]
    qtbot.waitUntil(lambda: panel.counters.ok >= 2, timeout=5000)
    window.close_connection()
    assert window.connection_bar.state == eng.STATE_CLOSED
    assert not window.engine.is_running
    assert window.act_connect.isEnabled() and not window.act_disconnect.isEnabled()
    count = panel.counters.ok
    qtbot.wait(200)
    assert panel.counters.ok == count
    assert not panel.model.flags(panel.model.index(0, 1)) & Qt.ItemFlag.ItemIsEditable


def test_connection_failure_shows_error_and_allows_retry(window, qtbot, tcp_slave):
    """A refused connection shows the error state, then opening again works."""
    window.insert_set(PollSet(interval_ms=50))
    window.connection_bar.set_config(ConnectionConfig(
        mode=ConnectionMode.TCP, host="127.0.0.1", tcp_port=1))
    window.open_connection()
    qtbot.waitUntil(lambda: window.connection_bar.state == eng.STATE_ERROR, timeout=5000)
    assert "Cannot connect" in window.connection_bar.state_label.toolTip()
    assert window.connection_bar.button.text() == "Open"
    connect_to(window, tcp_slave)
    qtbot.waitUntil(lambda: window.connection_bar.state == eng.STATE_OPEN, timeout=5000)


def test_serial_requires_port(window):
    """Opening a serial connection without a port is refused before anything starts."""
    window.connection_bar.set_config(ConnectionConfig(mode=ConnectionMode.SERIAL_RTU))
    window.open_connection()
    assert not window.engine.is_running
    assert window.connection_bar.state == eng.STATE_CLOSED


def test_cell_edit_writes_to_slave(window, qtbot, tcp_slave):
    """Editing a value cell performs FC06; the re-poll then shows the written value."""
    poll = window.insert_set(PollSet(slave=1, function=3, start=0, length=4, interval_ms=2000,
                                     columns=["unsigned"]))
    connect_to(window, tcp_slave)
    panel = window.panels[poll.id]
    qtbot.waitUntil(lambda: panel.counters.ok >= 1, timeout=5000)
    qtbot.waitUntil(lambda: bool(panel.model.flags(panel.model.index(0, 1))
                                 & Qt.ItemFlag.ItemIsEditable), timeout=2000)
    panel.model.setData(panel.model.index(2, 1), "5150")
    qtbot.waitUntil(lambda: tcp_slave.get_holding(2) == [5150], timeout=5000)
    # the 2 s poll interval is bypassed: a successful write triggers an immediate re-poll
    qtbot.waitUntil(lambda: cell(panel, 2, "unsigned") == "5150", timeout=1500)
    assert "Write OK" in window.log.text()


def test_coil_toggle_by_double_click(window, qtbot, tcp_slave):
    """Double clicking a coil row toggles it with FC05."""
    poll = window.insert_set(PollSet(slave=1, function=1, start=0, length=4, interval_ms=2000))
    connect_to(window, tcp_slave)
    panel = window.panels[poll.id]
    qtbot.waitUntil(lambda: panel.counters.ok >= 1, timeout=5000)
    before = tcp_slave.get_coils(0)[0]
    panel._double_clicked(panel.model.index(0, 1))
    qtbot.waitUntil(lambda: tcp_slave.get_coils(0)[0] == 1 - before, timeout=5000)


def test_write_without_connection_is_refused(window):
    """Writes need an open connection; the user is told."""
    poll = window.insert_set(PollSet())
    window._write_from_panel(poll.id, 0, [1])
    assert "Not connected" in window.statusBar().currentMessage()
    assert window.engine.drain() == []


def test_write_dialog_roundtrip(window, qtbot, tcp_slave):
    """The write dialog sends FC16 and shows the result."""
    window.insert_set(PollSet(slave=1, function=3, interval_ms=500))
    connect_to(window, tcp_slave)
    qtbot.waitUntil(lambda: window.connection_bar.state == eng.STATE_OPEN, timeout=5000)
    window.show_write_dialog(None)
    dialog = window._write_dialog
    dialog.function.setCurrentIndex(dialog.function.findData("registers"))
    dialog.address.setValue(50)
    dialog.values.setText("7, 0x10 300")
    dialog._submit()
    qtbot.waitUntil(lambda: tcp_slave.get_holding(50, 3) == [7, 16, 300], timeout=5000)
    qtbot.waitUntil(lambda: dialog.result_label.text().startswith("OK"), timeout=3000)


def test_write_dialog_validation(window):
    """Bad input is rejected locally with a message."""
    window.show_write_dialog(None)
    dialog = window._write_dialog
    dialog.function.setCurrentIndex(dialog.function.findData("register"))
    dialog.values.setText("1 2")
    dialog._submit()
    assert dialog.result_label.text().startswith("Failed")
    dialog.values.setText("70000")
    dialog._submit()
    assert "0..65535" in dialog.result_label.text()


# ------------------------------------------------------------------------ editing sets


def test_edit_set_applies_while_running(window, qtbot, tcp_slave):
    """Changing a set on a live connection takes effect without reconnecting."""
    poll = window.insert_set(PollSet(name="X", slave=1, function=3, start=0, length=2,
                                     interval_ms=40))
    connect_to(window, tcp_slave)
    panel = window.panels[poll.id]
    qtbot.waitUntil(lambda: panel.counters.ok >= 2, timeout=5000)
    edited = poll.copy()
    edited.start, edited.length, edited.name = 20, 3, "Renamed"
    window.apply_set_edit(edited)
    qtbot.waitUntil(lambda: cell(panel, 0, "unsigned") == "20", timeout=5000)
    assert panel.model.rowCount() == 3
    assert window.subwindows[poll.id].windowTitle() == "Renamed"


def test_pause_and_resume_set(window, qtbot, tcp_slave):
    """The Pause button stops polling of one set only."""
    a = window.insert_set(PollSet(name="A", interval_ms=40))
    b = window.insert_set(PollSet(name="B", interval_ms=40))
    connect_to(window, tcp_slave)
    qtbot.waitUntil(lambda: window.panels[a.id].counters.ok >= 2
                    and window.panels[b.id].counters.ok >= 2, timeout=5000)
    window.toggle_set(a.id)
    qtbot.wait(150)
    frozen = window.panels[a.id].counters.ok
    qtbot.wait(250)
    assert window.panels[a.id].counters.ok == frozen
    assert window.panels[b.id].counters.ok > frozen
    assert window.panels[a.id].toggle_button.text() == "Resume"
    window.toggle_set(a.id)
    qtbot.waitUntil(lambda: window.panels[a.id].counters.ok > frozen, timeout=3000)


def test_duplicate_and_remove_set(window):
    """Duplicate creates an independent copy; remove deletes the window."""
    original = window.insert_set(PollSet(name="Orig", start=5, length=7))
    window.duplicate_set(original.id)
    assert len(window.project.sets) == 2
    clone = window.project.sets[1]
    assert clone.id != original.id and clone.name == "Orig 2" and clone.length == 7
    window.remove_set(clone.id)
    assert [s.id for s in window.project.sets] == [original.id]
    assert clone.id not in window.panels and clone.id not in window.subwindows


def test_reset_counters(window, qtbot, tcp_slave):
    """Reset zeroes per-set and total counters."""
    poll = window.insert_set(PollSet(interval_ms=30))
    connect_to(window, tcp_slave)
    qtbot.waitUntil(lambda: window.totals.ok >= 3, timeout=5000)
    window.reset_counters()
    assert window.totals.ok == 0 and window.panels[poll.id].counters.ok == 0


# ------------------------------------------------------------------------ window management


def _inside(window, sub) -> bool:
    """True when ``sub`` lies completely inside the workspace."""
    area = window.mdi.viewport().rect()
    return area.contains(sub.geometry())


def _overlap(a, b) -> bool:
    """True when two windows overlap."""
    return a.geometry().intersects(b.geometry())


def _make_windows(window, count: int) -> list:
    """Create ``count`` sets with deliberately messy geometry."""
    sets = []
    for index in range(count):
        poll = PollSet(name=f"S{index}")
        poll.window.x, poll.window.y = 400 * index, 300 * index  # partly off screen
        poll.window.width, poll.window.height = 500, 400
        sets.append(window.insert_set(poll))
    return [window.subwindows[s.id] for s in sets]


def test_tile_puts_all_windows_in_view_without_overlap(window):
    """Tile: grid, nothing overlaps, everything visible."""
    subs = _make_windows(window, 4)
    window.tile_grid()
    assert all(_inside(window, s) for s in subs)
    assert not any(_overlap(a, b) for i, a in enumerate(subs) for b in subs[i + 1:])


def test_tile_horizontal_and_vertical(window):
    """Horizontal: full-width rows; vertical: full-height columns."""
    subs = _make_windows(window, 3)
    area = window.mdi.viewport().rect()
    window.tile_horizontal()
    assert all(s.geometry().width() == area.width() for s in subs)
    assert [s.geometry().y() for s in subs] == sorted(s.geometry().y() for s in subs)
    assert all(_inside(window, s) for s in subs)
    window.tile_vertical()
    assert all(s.geometry().height() == area.height() for s in subs)
    assert [s.geometry().x() for s in subs] == sorted(s.geometry().x() for s in subs)
    assert all(_inside(window, s) for s in subs)


def test_fit_all_in_view_keeps_layout_but_makes_everything_visible(window):
    """Fit: windows that were scattered beyond the workspace end up fully inside it."""
    subs = _make_windows(window, 3)
    assert not all(_inside(window, s) for s in subs)
    window.fit_all_in_view()
    assert all(_inside(window, s) for s in subs)
    xs = [s.geometry().x() for s in subs]
    assert xs == sorted(xs)  # relative arrangement preserved


def test_arrange_restores_minimized_windows(window):
    """Tiling brings minimized windows back."""
    subs = _make_windows(window, 2)
    window.minimize_all()
    assert all(s.isMinimized() for s in subs)
    window.tile_grid()
    assert not any(s.isMinimized() for s in subs)
    assert all(_inside(window, s) for s in subs)
    window.minimize_all()
    window.restore_all()
    assert not any(s.isMinimized() for s in subs)


def test_closing_a_window_hides_it_and_menu_brings_it_back(window):
    """The close button hides the window (set stays); Window menu / Show All restore it."""
    poll = window.insert_set(PollSet(name="Keep me"))
    sub = window.subwindows[poll.id]
    sub.close()
    assert sub.isHidden() and poll.id in window.panels and len(window.project.sets) == 1
    assert not window.hint.isHidden()  # workspace is empty again
    window.show_set_window(poll.id)
    assert not sub.isHidden() and window.hint.isHidden()
    sub.close()
    window.show_all_sets()
    assert not sub.isHidden()


def test_hidden_windows_are_skipped_when_tiling(window):
    """Hidden windows take no space in a tile."""
    subs = _make_windows(window, 3)
    subs[1].close()
    window.tile_vertical()
    area = window.mdi.viewport().rect()
    assert subs[0].geometry().width() == area.width() // 2


def test_window_menu_lists_sets(window):
    """The Window menu has arrangement actions and one entry per set."""
    window.insert_set(PollSet(name="Alpha"))
    window.insert_set(PollSet(name="Beta"))
    window._rebuild_window_menu()
    titles = [a.text() for a in window.window_menu.actions()]
    assert "&Tile" in titles and "&Fit All in View" in titles
    assert "1  Alpha" in titles and "2  Beta" in titles


def test_tabbed_mode_toggle_and_arrangement_leaves_it(window):
    """Tabs can be switched on; arranging windows switches back to floating windows."""
    _make_windows(window, 2)
    window.act_tabbed.setChecked(True)
    assert window.mdi.viewMode().name == "TabbedView"
    window.tile_grid()
    assert window.mdi.viewMode().name == "SubWindowView"


# ------------------------------------------------------------------------ projects


def test_project_roundtrip_through_ui(window, tmp_path):
    """Save everything (connection + all windows) and load it into a fresh window."""
    window.connection_bar.set_config(ConnectionConfig(
        mode=ConnectionMode.SERIAL_ASCII, serial_port="/dev/ttyUSB7", baudrate=57600,
        bytesize=7, parity="E", stopbits=2, flow_control="xonxoff"))
    a = window.insert_set(PollSet(name="Temps", slave=3, function=4, start=100, length=8,
                                  interval_ms=250, timeout_ms=300, error_pause_ms=1500,
                                  columns=["signed", "float32"], word_swap=True))
    b = window.insert_set(PollSet(name="Coils", slave=3, function=1, length=16))
    sub = window.subwindows[a.id]
    sub.setGeometry(33, 44, 411, 222)
    window.subwindows[b.id].close()
    path = tmp_path / "plant.kmms"
    window.path = path
    assert window.save_project()
    assert not window.dirty and path.exists()

    data = json.loads(path.read_text())
    assert data["connection"]["serial_port"] == "/dev/ttyUSB7"
    assert [s["name"] for s in data["sets"]] == ["Temps", "Coils"]

    fresh = MainWindow(make_settings(tmp_path / "other"), interactive=False)
    fresh.show()
    assert fresh.open_project(path)
    try:
        config = fresh.connection_bar.config()
        assert config.mode is ConnectionMode.SERIAL_ASCII and config.baudrate == 57600
        assert config.parity == "E" and config.stopbits == 2 and config.bytesize == 7
        assert config.serial_port == "/dev/ttyUSB7" and config.flow_control == "xonxoff"
        loaded = fresh.project.sets
        assert [s.name for s in loaded] == ["Temps", "Coils"]
        assert loaded[0].columns == ["signed", "float32"] and loaded[0].word_swap
        assert (loaded[0].interval_ms, loaded[0].timeout_ms, loaded[0].error_pause_ms) == \
               (250, 300, 1500)
        geo = fresh.subwindows[loaded[0].id].geometry()
        assert (geo.x(), geo.y(), geo.width(), geo.height()) == (33, 44, 411, 222)
        assert fresh.subwindows[loaded[1].id].isHidden()
        assert not fresh.dirty and fresh.path == path
        assert "plant.kmms" in fresh.windowTitle()
    finally:
        fresh.engine.stop()
        fresh.close()


def test_dirty_flag_and_title(window, tmp_path):
    """Edits mark the project modified; saving clears it."""
    assert "Untitled" in window.windowTitle() and "*" not in window.windowTitle()
    window.insert_set(PollSet())
    assert window.dirty and "*" in window.windowTitle()
    window.path = tmp_path / "x.kmms"
    window.save_project()
    assert not window.dirty and "*" not in window.windowTitle()
    window.connection_bar.host.setText("10.1.1.1")
    assert window.dirty


def test_open_invalid_file_reports_error(window, tmp_path):
    """A broken project file leaves the workspace untouched."""
    window.insert_set(PollSet(name="Stay"))
    bad = tmp_path / "bad.kmms"
    bad.write_text("{not json")
    window.dirty = False
    assert not window.open_project(bad)
    assert [s.name for s in window.project.sets] == ["Stay"]
    assert "Cannot open project" in window.statusBar().currentMessage()


def test_new_project_clears_workspace(window):
    """New project removes all sets and windows."""
    window.insert_set(PollSet())
    window.dirty = False
    assert window.new_project()
    assert not window.panels and not window.project.sets and not window.dirty


def test_load_project_closes_connection(window, qtbot, tcp_slave):
    """Loading another project while connected closes the connection first."""
    window.insert_set(PollSet(interval_ms=40))
    connect_to(window, tcp_slave)
    qtbot.waitUntil(lambda: window.connection_bar.state == eng.STATE_OPEN, timeout=5000)
    window.load_project(Project(), None)
    assert not window.engine.is_running
    assert window.connection_bar.state == eng.STATE_CLOSED


def test_recent_files(window, tmp_path):
    """Saved/opened projects appear in the recent list, newest first."""
    window.insert_set(PollSet())
    first, second = tmp_path / "a.kmms", tmp_path / "b.kmms"
    window.path = first
    window.save_project()
    window.path = second
    window.save_project()
    assert window.settings.recent_files[:2] == [str(second), str(first)]
    window._rebuild_recent_menu()
    assert [a.text() for a in window.recent_menu.actions()][:2] == ["b.kmms", "a.kmms"]


def test_session_restore(qtbot, tmp_path):
    """Closing writes an autosave; a new window restores sets, connection and window layout."""
    settings = make_settings(tmp_path)
    first = MainWindow(settings, interactive=False)
    first.show()
    first.connection_bar.host.setText("192.168.5.5")
    poll = first.insert_set(PollSet(name="Persisted", start=9, length=3))
    first.subwindows[poll.id].setGeometry(10, 20, 330, 250)
    first.close()
    assert settings.autosave_path.exists()

    second = MainWindow(settings, interactive=False)
    qtbot.addWidget(second)
    second.show()
    assert second.restore_session()
    assert [s.name for s in second.project.sets] == ["Persisted"]
    assert second.connection_bar.config().host == "192.168.5.5"
    geo = second.subwindows[second.project.sets[0].id].geometry()
    assert (geo.x(), geo.y(), geo.width(), geo.height()) == (10, 20, 330, 250)
    second.engine.stop()


def test_session_restore_can_be_disabled(qtbot, tmp_path):
    """With the preference off, nothing is restored."""
    settings = make_settings(tmp_path)
    first = MainWindow(settings, interactive=False)
    first.show()
    first.insert_set(PollSet())
    first.close()
    settings.restore_session = False
    second = MainWindow(settings, interactive=False)
    qtbot.addWidget(second)
    assert not second.restore_session()
    assert not second.project.sets


def test_theme_switch_keeps_working(window):
    """Switching themes at runtime recolors everything without errors."""
    window.insert_set(PollSet())
    for mode in ("dark", "light", "system", "dark"):
        window.set_theme(mode)
    assert window.settings.theme == "dark" and theme.current().name == "dark"
    window.set_theme("light")


def test_traffic_log_collects_frames(window, qtbot, tcp_slave):
    """TX/RX frames appear in the log and it can be paused, cleared and saved."""
    window.insert_set(PollSet(interval_ms=50))
    connect_to(window, tcp_slave)
    qtbot.waitUntil(lambda: " TX " in window.log.text() and " RX " in window.log.text(),
                    timeout=5000)
    window.log.pause.setChecked(True)
    size = len(window.log.view.toPlainText())
    qtbot.wait(300)
    assert len(window.log.view.toPlainText()) == size  # display frozen while paused
    window.log.pause.setChecked(False)
    assert len(window.log.view.toPlainText()) >= size
    window.log.clear()
    assert window.log.text() == ""


def test_window_close_stops_engine(qtbot, tmp_path, tcp_slave):
    """Closing the main window while connected shuts the poller down."""
    win = MainWindow(make_settings(tmp_path), interactive=False)
    win.show()
    win.insert_set(PollSet(interval_ms=40))
    connect_to(win, tcp_slave)
    qtbot.waitUntil(lambda: win.connection_bar.state == eng.STATE_OPEN, timeout=5000)
    win.close()
    assert not win.engine.is_running
