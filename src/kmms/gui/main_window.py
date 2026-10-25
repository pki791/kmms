"""The main window: workspace of set windows, connection control and project handling."""

from __future__ import annotations

import logging
import platform
import sys
from pathlib import Path
from typing import Optional

import PySide6
from PySide6.QtCore import QEvent
from PySide6.QtCore import QObject
from PySide6.QtCore import QRect
from PySide6.QtCore import Qt
from PySide6.QtCore import QTimer
from PySide6.QtCore import Signal
from PySide6.QtGui import QAction
from PySide6.QtGui import QActionGroup
from PySide6.QtGui import QCloseEvent
from PySide6.QtGui import QDragEnterEvent
from PySide6.QtGui import QDropEvent
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QDockWidget
from PySide6.QtWidgets import QFileDialog
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QMainWindow
from PySide6.QtWidgets import QMdiArea
from PySide6.QtWidgets import QMdiSubWindow
from PySide6.QtWidgets import QMessageBox
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QToolBar
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget

from kmms import APP_NAME
from kmms import LICENSE_NAME
from kmms import LICENSE_URL
from kmms import PROJECT_EXTENSION
from kmms import __version__
from kmms import engine as eng
from kmms.gui import theme
from kmms.gui.app_settings import AppSettings
from kmms.gui.app_settings import PreferencesDialog
from kmms.gui.connection_bar import ConnectionBar
from kmms.gui.icon import small_icon
from kmms.gui.set_dialog import PollSetDialog
from kmms.gui.set_panel import SetPanel
from kmms.gui.traffic_log import TrafficLog
from kmms.gui.write_dialog import WriteDialog
from kmms.model import PollSet
from kmms.model import Project
from kmms.model import ProjectError
from kmms.model import WindowState
from kmms.stats import Counters

LOG = logging.getLogger(__name__)

FILE_FILTER = f"KMMS projects (*{PROJECT_EXTENSION});;All files (*)"
EVENT_PUMP_MS = 30


class SetSubWindow(QMdiSubWindow):
    """MDI window hosting one :class:`SetPanel`. Closing only hides it."""

    closed_by_user = Signal(str)

    def __init__(self, panel: SetPanel) -> None:
        """Wrap ``panel``."""
        super().__init__()
        self.set_id = panel.poll_set.id
        self.setWidget(panel)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setWindowTitle(panel.poll_set.name)
        self.setWindowIcon(small_icon())
        self.resize(520, 360)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        """Hide instead of destroying: the set stays in the project."""
        event.ignore()
        self.hide()
        self.closed_by_user.emit(self.set_id)


class _HintOverlay(QLabel):
    """Centered hint shown while there is no visible set window."""

    def __init__(self, area: QMdiArea) -> None:
        """Attach to the viewport of ``area``."""
        super().__init__(area.viewport())
        self._area = area
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setObjectName("muted")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setText(
            "<div style='font-size:18px;font-weight:600'>No polling sets to show</div>"
            "<div style='margin-top:6px'>Use <b>Sets → Add Set...</b> (Ctrl+Shift+N) to "
            "define what to read.<br>Then open the connection to start polling.</div>")
        area.viewport().installEventFilter(self)
        self.reposition()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """Keep the overlay covering the viewport."""
        if watched is self._area.viewport() and event.type() == QEvent.Type.Resize:
            self.reposition()
        return False

    def reposition(self) -> None:
        """Resize to the viewport."""
        self.setGeometry(self._area.viewport().rect())


class MainWindow(QMainWindow):
    """Top level window of the application."""

    def __init__(self, settings: AppSettings, engine: Optional[eng.PollEngine] = None,
                 interactive: bool = True) -> None:
        """Build the UI. ``interactive=False`` suppresses blocking message boxes (tests)."""
        super().__init__()
        self.settings = settings
        self.engine = engine or eng.PollEngine()
        self.interactive = interactive
        self.project = Project()
        self.path: Optional[Path] = None
        self.dirty = False
        self.panels: dict[str, SetPanel] = {}
        self.subwindows: dict[str, SetSubWindow] = {}
        self.totals = Counters()
        self._pending_writes: dict[int, Optional[WriteDialog]] = {}
        self._write_dialog: Optional[WriteDialog] = None
        self._totals_dirty = False
        self._closing = False
        self._last_state = eng.STATE_CLOSED

        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        self.resize(1180, 760)

        self._build_central()
        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_statusbar()
        self._build_log_dock()
        self._update_title()
        self._update_actions()

        self._pump_timer = QTimer(self)
        self._pump_timer.setInterval(EVENT_PUMP_MS)
        self._pump_timer.timeout.connect(self.pump_events)
        self._pump_timer.start()
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start(self.settings.autosave_seconds * 1000)

        geometry = self.settings.geometry("main")
        if not geometry.isEmpty():
            self.restoreGeometry(geometry)

    # ================================================================== construction

    def _build_central(self) -> None:
        """Connection bar above the MDI workspace."""
        self.connection_bar = ConnectionBar()
        self.connection_bar.open_requested.connect(self.open_connection)
        self.connection_bar.close_requested.connect(self.close_connection)
        self.connection_bar.changed.connect(self._connection_edited)

        self.mdi = QMdiArea()
        self.mdi.setViewMode(QMdiArea.ViewMode.SubWindowView)
        self.mdi.setTabsClosable(False)
        self.mdi.setTabsMovable(True)
        self.mdi.setOption(QMdiArea.AreaOption.DontMaximizeSubWindowOnActivation, True)
        self.hint = _HintOverlay(self.mdi)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 6)
        layout.setSpacing(8)
        layout.addWidget(self.connection_bar)
        layout.addWidget(self.mdi, 1)
        self.setCentralWidget(central)

    def _action(self, text: str, slot, shortcut=None, tip: str = "",
                checkable: bool = False) -> QAction:
        """Create an action owned by the window."""
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut) if isinstance(shortcut, str) else shortcut)
        if tip:
            action.setStatusTip(tip)
            action.setToolTip(tip)
        action.setCheckable(checkable)
        if checkable:
            action.toggled.connect(slot)
        else:
            action.triggered.connect(lambda _checked=False: slot())
        return action

    def _build_actions(self) -> None:
        """Create all actions."""
        a = self._action
        self.act_new = a("&New Project", self.new_project, QKeySequence.StandardKey.New)
        self.act_open = a("&Open Project...", self.open_project_dialog,
                          QKeySequence.StandardKey.Open)
        self.act_save = a("&Save Project", self.save_project, QKeySequence.StandardKey.Save)
        self.act_save_as = a("Save Project &As...", self.save_project_as,
                             QKeySequence.StandardKey.SaveAs)
        self.act_quit = a("&Quit", self.close, QKeySequence.StandardKey.Quit)
        self.act_prefs = a("&Preferences...", self.show_preferences, "Ctrl+,")

        self.act_connect = a("&Open Connection", self.open_connection, "F5",
                             "Open the port / connect and start polling")
        self.act_disconnect = a("&Close Connection", self.close_connection, "Shift+F5",
                                "Stop polling and close the port / connection")

        self.act_add = a("&Add Set...", self.add_set, "Ctrl+Shift+N",
                         "Define a new polling set")
        self.act_write = a("&Write to Slave...", lambda: self.show_write_dialog(None),
                           "Ctrl+Shift+W", "Write coils or registers")
        self.act_reset = a("&Reset Counters", self.reset_counters, None,
                           "Zero the success / error counters")

        self.act_tile = a("&Tile", self.tile_grid, "Ctrl+Shift+T",
                          "Arrange all windows in a grid")
        self.act_tile_h = a("Tile &Horizontally", self.tile_horizontal, None,
                            "Stack windows top to bottom")
        self.act_tile_v = a("Tile &Vertically", self.tile_vertical, None,
                            "Place windows side by side")
        self.act_cascade = a("&Cascade", self.cascade, None)
        self.act_fit = a("&Fit All in View", self.fit_all_in_view, "Ctrl+Shift+F",
                         "Move and shrink windows so every one is visible, keeping the layout")
        self.act_show_all = a("&Show All Sets", self.show_all_sets, None,
                              "Unhide every set window")
        self.act_minimize_all = a("&Minimize All", self.minimize_all)
        self.act_restore_all = a("&Restore All", self.restore_all)
        self.act_tabbed = a("Tabbed &Mode", self.set_tabbed, None,
                            "Show the set windows as tabs", checkable=True)

        self.act_log = a("&Traffic Log", self._toggle_log, "Ctrl+L", checkable=True)
        self.act_about = a("&About", self.show_about)

        self.theme_group = QActionGroup(self)
        self.theme_actions: dict[str, QAction] = {}
        for key, label in (("system", "System"), ("light", "Light"), ("dark", "Dark")):
            action = QAction(label, self, checkable=True)
            action.triggered.connect(lambda _c=False, k=key: self.set_theme(k))
            self.theme_group.addAction(action)
            self.theme_actions[key] = action
        self.theme_actions[self.settings.theme].setChecked(True)

    def _build_menus(self) -> None:
        """Create the menu bar."""
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        file_menu.addActions([self.act_new, self.act_open])
        self.recent_menu = file_menu.addMenu("Open &Recent")
        self.recent_menu.aboutToShow.connect(self._rebuild_recent_menu)
        file_menu.addSeparator()
        file_menu.addActions([self.act_save, self.act_save_as])
        file_menu.addSeparator()
        file_menu.addAction(self.act_prefs)
        file_menu.addAction(self.act_quit)

        conn_menu = bar.addMenu("&Connection")
        conn_menu.addActions([self.act_connect, self.act_disconnect])

        sets_menu = bar.addMenu("&Sets")
        sets_menu.addAction(self.act_add)
        sets_menu.addAction(self.act_write)
        sets_menu.addSeparator()
        sets_menu.addAction(self.act_reset)

        view_menu = bar.addMenu("&View")
        view_menu.addAction(self.act_log)
        theme_menu = view_menu.addMenu("&Theme")
        theme_menu.addActions(list(self.theme_actions.values()))

        self.window_menu = bar.addMenu("&Window")
        self.window_menu.aboutToShow.connect(self._rebuild_window_menu)
        self._rebuild_window_menu()

        help_menu = bar.addMenu("&Help")
        help_menu.addAction(self.act_about)

    def _build_toolbar(self) -> None:
        """Create the toolbar with the most used actions."""
        bar = QToolBar("Main")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(bar)
        self.toolbar = bar
        bar.addAction(self.act_add)
        bar.addAction(self.act_write)
        bar.addSeparator()
        for action in (self.act_tile, self.act_tile_h, self.act_tile_v, self.act_cascade,
                       self.act_fit):
            bar.addAction(action)
        bar.addSeparator()
        bar.addAction(self.act_log)
        bar.addSeparator()
        bar.addAction(self.act_save)

    def _build_statusbar(self) -> None:
        """Create the status bar with connection info and read counters."""
        status = self.statusBar()
        status.setSizeGripEnabled(False)
        self.status_state = QLabel("Closed")
        self.status_ok = QLabel()
        self.status_err = QLabel()
        reset = QPushButton("Reset")
        reset.setFlat(True)
        reset.setToolTip("Reset the success / error counters")
        reset.clicked.connect(self.reset_counters)
        reset.setFixedHeight(20)
        status.addWidget(self.status_state)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        status.addPermanentWidget(self.status_ok)
        status.addPermanentWidget(self.status_err)
        status.addPermanentWidget(reset)
        self._refresh_totals()

    def _build_log_dock(self) -> None:
        """Create the dockable traffic log."""
        self.log = TrafficLog()
        self.log_dock = QDockWidget("Traffic Log", self)
        self.log_dock.setObjectName("trafficLogDock")
        self.log_dock.setWidget(self.log)
        self.log_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.log_dock.visibilityChanged.connect(self._log_visibility_changed)
        self.act_log.setChecked(True)
        self.log_dock.resize(self.log_dock.width(), 180)

    # ================================================================== project handling

    def collect_project(self) -> Project:
        """Return the project including connection settings and current window layout."""
        self.project.connection = self.connection_bar.config()
        self._sync_window_states()
        self.project.log_visible = not self.log_dock.isHidden()
        self.project.view_mode = "tabs" if self.mdi.viewMode() == QMdiArea.ViewMode.TabbedView \
            else "windows"
        return self.project

    def _sync_window_states(self) -> None:
        """Copy the live geometry of the windows into the project sets."""
        tabbed = self.mdi.viewMode() == QMdiArea.ViewMode.TabbedView
        for poll_set in self.project.sets:
            sub = self.subwindows.get(poll_set.id)
            if sub is None:
                continue
            state = poll_set.window
            state.visible = not sub.isHidden()
            state.minimized = sub.isMinimized()
            if tabbed or sub.isMinimized() or sub.isMaximized():
                continue
            geo = sub.geometry()
            state.x, state.y, state.width, state.height = geo.x(), geo.y(), geo.width(), \
                geo.height()

    def load_project(self, project: Project, path: Optional[Path]) -> None:
        """Replace the workspace by ``project`` (the connection is closed first)."""
        self.close_connection()
        for set_id in list(self.panels):
            self._destroy_window(set_id)
        self.project = project
        self.path = path
        self.connection_bar.set_config(project.connection)
        for poll_set in project.sets:
            self._create_window(poll_set)
        self.mdi.setViewMode(QMdiArea.ViewMode.TabbedView if project.view_mode == "tabs"
                             else QMdiArea.ViewMode.SubWindowView)
        self.act_tabbed.blockSignals(True)
        self.act_tabbed.setChecked(project.view_mode == "tabs")
        self.act_tabbed.blockSignals(False)
        self.log_dock.setVisible(project.log_visible)
        self.totals.reset()
        self._refresh_totals()
        self._set_dirty(False)
        self._update_hint()
        self._update_actions()

    def new_project(self) -> bool:
        """Start an empty project."""
        if not self._maybe_save():
            return False
        project = Project()
        project.connection = self.connection_bar.config()
        self.load_project(project, None)
        return True

    def open_project_dialog(self) -> None:
        """Ask for a project file and open it."""
        path, _ = QFileDialog.getOpenFileName(self, "Open project", self.settings.last_dir,
                                              FILE_FILTER)
        if path:
            self.open_project(Path(path))

    def open_project(self, path: Path) -> bool:
        """Load the project at ``path``. Returns False on failure."""
        if not self._maybe_save():
            return False
        try:
            project = Project.load(path)
        except ProjectError as exc:
            self.settings.remove_recent(str(path))
            self._error("Cannot open project", str(exc))
            return False
        self.load_project(project, path)
        self.settings.add_recent(str(path))
        self.settings.last_dir = str(path.parent)
        self._update_title()
        self._notify(f"Opened {path.name}")
        return True

    def save_project(self) -> bool:
        """Save to the current path (asks for one if the project is untitled)."""
        if self.path is None:
            return self.save_project_as()
        return self._write_project(self.path)

    def save_project_as(self) -> bool:
        """Ask for a path and save."""
        suggestion = str(self.path) if self.path else str(
            Path(self.settings.last_dir) / f"project{PROJECT_EXTENSION}")
        path, _ = QFileDialog.getSaveFileName(self, "Save project", suggestion, FILE_FILTER)
        if not path:
            return False
        target = Path(path)
        if target.suffix == "":
            target = target.with_suffix(PROJECT_EXTENSION)
        return self._write_project(target)

    def _write_project(self, path: Path) -> bool:
        """Write the project file and update bookkeeping."""
        try:
            self.collect_project().save(path)
        except ProjectError as exc:
            self._error("Cannot save project", str(exc))
            return False
        self.path = path
        self.settings.add_recent(str(path))
        self.settings.last_dir = str(path.parent)
        self._set_dirty(False)
        self._autosave()
        self._notify(f"Saved {path.name}")
        return True

    def _maybe_save(self) -> bool:
        """Offer to save unsaved changes. Returns False if the user cancels."""
        if not self.dirty or not self.interactive:
            return True
        name = self.path.name if self.path else "Untitled"
        box = QMessageBox(QMessageBox.Icon.Question, APP_NAME,
                          f"Save changes to “{name}”?", parent=self)
        box.setInformativeText("Your changes will be lost if you don't save them.")
        box.setStandardButtons(QMessageBox.StandardButton.Save
                               | QMessageBox.StandardButton.Discard
                               | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        answer = box.exec()
        if answer == QMessageBox.StandardButton.Save:
            return self.save_project()
        return answer == QMessageBox.StandardButton.Discard

    def _set_dirty(self, dirty: bool) -> None:
        """Track unsaved changes and refresh the title."""
        self.dirty = dirty
        self._update_title()

    def _mark_dirty(self) -> None:
        """Flag the project as modified."""
        if not self.dirty:
            self._set_dirty(True)

    def _update_title(self) -> None:
        """Window title: ``name* - KMMS``."""
        name = self.path.name if self.path else "Untitled"
        self.setWindowTitle(f"{name}{' *' if self.dirty else ''} — {APP_NAME}")

    def _rebuild_recent_menu(self) -> None:
        """Fill the Open Recent submenu."""
        self.recent_menu.clear()
        files = self.settings.recent_files
        for file in files:
            action = self.recent_menu.addAction(Path(file).name)
            action.setToolTip(file)
            action.triggered.connect(lambda _c=False, f=file: self.open_project(Path(f)))
        if not files:
            self.recent_menu.addAction("(none)").setEnabled(False)
        else:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("Clear list").triggered.connect(
                self.settings.clear_recent)

    # ------------------------------------------------------------------ session

    def _autosave(self) -> None:
        """Write the working state so the next start can restore it."""
        try:
            path = self.settings.autosave_path
            path.parent.mkdir(parents=True, exist_ok=True)
            self.collect_project().save(path)
            self.settings.store_session(str(self.path) if self.path else "", self.dirty)
        except (ProjectError, OSError) as exc:
            LOG.warning("Autosave failed: %s", exc)

    def restore_session(self) -> bool:
        """Reopen the workspace of the previous run. Returns True if something was restored."""
        if not self.settings.restore_session:
            return False
        autosave = self.settings.autosave_path
        stored = self.settings.session_path
        path = Path(stored) if stored else None
        if autosave.exists():
            try:
                project = Project.load(autosave)
            except ProjectError as exc:
                LOG.warning("Ignoring unreadable autosave: %s", exc)
            else:
                self.load_project(project, path)
                self._set_dirty(self.settings.session_dirty)
                return True
        if path is not None and path.exists():
            try:
                self.load_project(Project.load(path), path)
                return True
            except ProjectError as exc:
                LOG.warning("Could not reopen %s: %s", path, exc)
        return False

    # ================================================================== sets and windows

    def _create_window(self, poll_set: PollSet) -> None:
        """Create the MDI window for ``poll_set`` according to its saved state."""
        panel = SetPanel(poll_set)
        self._apply_highlights(panel)
        panel.edit_requested.connect(self.edit_set)
        panel.toggle_requested.connect(self.toggle_set)
        panel.duplicate_requested.connect(self.duplicate_set)
        panel.remove_requested.connect(self.remove_set)
        panel.write_dialog_requested.connect(self.show_write_dialog)
        panel.write_requested.connect(self._write_from_panel)
        panel.message.connect(self._notify)
        panel.counters_changed.connect(self._totals_changed)
        panel.set_connection_state(self._last_state == eng.STATE_OPEN)
        sub = SetSubWindow(panel)
        sub.closed_by_user.connect(self._window_hidden)
        self.mdi.addSubWindow(sub)
        state = poll_set.window
        sub.setGeometry(state.x, state.y, state.width, state.height)
        self.panels[poll_set.id] = panel
        self.subwindows[poll_set.id] = sub
        if state.visible:
            sub.show()
            if state.minimized:
                sub.showMinimized()
        else:
            sub.hide()
        self._update_hint()

    def _destroy_window(self, set_id: str) -> None:
        """Remove a window and its panel for good."""
        sub = self.subwindows.pop(set_id, None)
        self.panels.pop(set_id, None)
        if sub is not None:
            self.mdi.removeSubWindow(sub)
            sub.deleteLater()
        self._update_hint()

    def _next_window_state(self) -> WindowState:
        """Staggered default position for a new window."""
        count = len(self.subwindows)
        offset = 28 * (count % 10)
        return WindowState(x=16 + offset, y=16 + offset, width=520, height=380)

    def _unique_name(self, base: str) -> str:
        """Return ``base`` or ``base 2``, ``base 3`` ... not yet used."""
        names = {s.name for s in self.project.sets}
        if base not in names:
            return base
        index = 2
        while f"{base} {index}" in names:
            index += 1
        return f"{base} {index}"

    def add_set(self) -> Optional[PollSet]:
        """Let the user define a new set and open its window."""
        draft = PollSet(name=self._unique_name(f"Set {len(self.project.sets) + 1}"),
                        window=self._next_window_state())
        if self.project.sets:
            last = self.project.sets[-1]
            draft.slave, draft.function = last.slave, last.function
            draft.start = min(last.start + last.length, 65535)
            draft.length = min(draft.length, 65536 - draft.start)
        dialog = PollSetDialog(draft, self, "Add polling set")
        if dialog.exec() != PollSetDialog.DialogCode.Accepted:
            return None
        return self.insert_set(dialog.result_set())

    def insert_set(self, poll_set: PollSet) -> PollSet:
        """Add an already defined set to the project and show it."""
        self.project.sets.append(poll_set)
        self._create_window(poll_set)
        self._sets_changed()
        return poll_set

    def edit_set(self, set_id: str) -> None:
        """Open the edit dialog for a set."""
        poll_set = self._find_set(set_id)
        if poll_set is None:
            return
        dialog = PollSetDialog(poll_set, self, f"Edit “{poll_set.name}”")
        if dialog.exec() == PollSetDialog.DialogCode.Accepted:
            self.apply_set_edit(dialog.result_set())

    def apply_set_edit(self, edited: PollSet) -> None:
        """Replace the stored set with ``edited`` (same id) and refresh its window."""
        index = next((i for i, s in enumerate(self.project.sets) if s.id == edited.id), None)
        if index is None:
            return
        edited.window = self.project.sets[index].window
        self.project.sets[index] = edited
        panel = self.panels[edited.id]
        panel.set_definition(edited)
        self.subwindows[edited.id].setWindowTitle(edited.name)
        self._sets_changed()

    def toggle_set(self, set_id: str) -> None:
        """Pause or resume polling of one set."""
        poll_set = self._find_set(set_id)
        if poll_set is None:
            return
        edited = poll_set.copy()
        edited.enabled = not poll_set.enabled
        self.apply_set_edit(edited)

    def duplicate_set(self, set_id: str) -> None:
        """Create a copy of a set."""
        original = self._find_set(set_id)
        if original is None:
            return
        self._sync_window_states()
        clone = PollSet.from_dict({**original.to_dict(), "id": ""})
        clone.name = self._unique_name(original.name)
        clone.window = self._next_window_state()
        self.insert_set(clone)

    def remove_set(self, set_id: str) -> None:
        """Delete a set and its window."""
        poll_set = self._find_set(set_id)
        if poll_set is None:
            return
        if self.interactive:
            answer = QMessageBox.question(
                self, "Remove set", f"Remove the polling set “{poll_set.name}”?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.project.sets = [s for s in self.project.sets if s.id != set_id]
        self._destroy_window(set_id)
        self._sets_changed()

    def _find_set(self, set_id: str) -> Optional[PollSet]:
        """Return the set with ``set_id``."""
        return next((s for s in self.project.sets if s.id == set_id), None)

    def _sets_changed(self) -> None:
        """Propagate set changes to the engine and bookkeeping."""
        if self.engine.is_running:
            self.engine.update_sets(self.project.sets)
        self._mark_dirty()
        self._update_hint()
        self._update_actions()

    def _window_hidden(self, _set_id: str) -> None:
        """A window was closed with its title bar button."""
        self._mark_dirty()
        self._update_hint()

    def _update_hint(self) -> None:
        """Show the empty-workspace hint when no set window is visible."""
        visible = any(not sub.isHidden() for sub in self.subwindows.values())
        self.hint.setVisible(not visible)
        self.hint.reposition()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Keep the hint overlay in place."""
        super().resizeEvent(event)
        self.hint.reposition()

    # ------------------------------------------------------------------ window arrangement

    def _arrangeable(self) -> list[SetSubWindow]:
        """Visible set windows in creation order (restored from minimized state)."""
        if self.mdi.viewMode() == QMdiArea.ViewMode.TabbedView:
            self.act_tabbed.setChecked(False)
        windows = []
        for poll_set in self.project.sets:
            sub = self.subwindows.get(poll_set.id)
            if sub is not None and not sub.isHidden():
                if sub.isMinimized() or sub.isMaximized():
                    sub.showNormal()
                windows.append(sub)
        return windows

    def tile_grid(self) -> None:
        """Arrange windows in a grid filling the workspace."""
        windows = self._arrangeable()
        if not windows:
            return
        area = self.mdi.viewport().rect()
        count = len(windows)
        columns = max(1, round((count * area.width() / max(area.height(), 1)) ** 0.5))
        columns = min(columns, count)
        rows = -(-count // columns)
        width, height = area.width() // columns, area.height() // rows
        for index, sub in enumerate(windows):
            row, column = divmod(index, columns)
            sub.setGeometry(column * width, row * height, width, height)

    def tile_horizontal(self) -> None:
        """Stack windows on top of each other."""
        windows = self._arrangeable()
        if not windows:
            return
        area = self.mdi.viewport().rect()
        height = area.height() // len(windows)
        for index, sub in enumerate(windows):
            sub.setGeometry(0, index * height, area.width(), height)

    def tile_vertical(self) -> None:
        """Place windows next to each other."""
        windows = self._arrangeable()
        if not windows:
            return
        area = self.mdi.viewport().rect()
        width = area.width() // len(windows)
        for index, sub in enumerate(windows):
            sub.setGeometry(index * width, 0, width, area.height())

    def cascade(self) -> None:
        """Cascade windows."""
        if self._arrangeable():
            self.mdi.cascadeSubWindows()

    def fit_all_in_view(self) -> None:
        """Scale and shift the current layout so that all windows are inside the workspace."""
        windows = self._arrangeable()
        if not windows:
            return
        area = self.mdi.viewport().rect()
        union = QRect(windows[0].geometry())
        for sub in windows[1:]:
            union = union.united(sub.geometry())
        scale = min(area.width() / max(union.width(), 1),
                    area.height() / max(union.height(), 1), 1.0)
        for sub in windows:
            geo = sub.geometry()
            x = round((geo.x() - union.x()) * scale)
            y = round((geo.y() - union.y()) * scale)
            width = max(round(geo.width() * scale), sub.minimumSizeHint().width())
            height = max(round(geo.height() * scale), sub.minimumSizeHint().height())
            x = max(0, min(x, area.width() - width))
            y = max(0, min(y, area.height() - height))
            sub.setGeometry(x, y, width, height)

    def show_all_sets(self) -> None:
        """Unhide every set window."""
        for sub in self.subwindows.values():
            if sub.isHidden():
                sub.show()
        self._mark_dirty()
        self._update_hint()

    def minimize_all(self) -> None:
        """Minimize every visible window."""
        for sub in self.subwindows.values():
            if not sub.isHidden():
                sub.showMinimized()

    def restore_all(self) -> None:
        """Restore every minimized or maximized window."""
        for sub in self.subwindows.values():
            if not sub.isHidden() and (sub.isMinimized() or sub.isMaximized()):
                sub.showNormal()

    def set_tabbed(self, tabbed: bool) -> None:
        """Switch between floating windows and tabs."""
        self.mdi.setViewMode(QMdiArea.ViewMode.TabbedView if tabbed
                             else QMdiArea.ViewMode.SubWindowView)
        self._mark_dirty()

    def _rebuild_window_menu(self) -> None:
        """Arrangement actions followed by one checkable entry per set."""
        menu = self.window_menu
        menu.clear()
        menu.addActions([self.act_tile, self.act_tile_h, self.act_tile_v, self.act_cascade])
        menu.addSeparator()
        menu.addActions([self.act_fit, self.act_show_all, self.act_minimize_all,
                         self.act_restore_all])
        menu.addSeparator()
        menu.addAction(self.act_tabbed)
        if self.project.sets:
            menu.addSeparator()
        for index, poll_set in enumerate(self.project.sets):
            sub = self.subwindows.get(poll_set.id)
            action = QAction(f"{index + 1}  {poll_set.name}", menu)
            action.setCheckable(True)
            action.setChecked(sub is not None and not sub.isHidden())
            action.triggered.connect(lambda _c=False, sid=poll_set.id: self.show_set_window(sid))
            menu.addAction(action)

    def show_set_window(self, set_id: str) -> None:
        """Bring a set's window to the front, unhiding and restoring it if needed."""
        sub = self.subwindows.get(set_id)
        if sub is None:
            return
        if sub.isHidden():
            sub.show()
            self._mark_dirty()
        if sub.isMinimized():
            sub.showNormal()
        self.mdi.setActiveSubWindow(sub)
        self._update_hint()

    # ================================================================== connection

    def _connection_edited(self) -> None:
        """The user changed connection parameters."""
        self._mark_dirty()

    def open_connection(self) -> None:
        """Open the port / socket and start polling."""
        if self.engine.is_running and self._last_state not in (eng.STATE_CLOSED,
                                                               eng.STATE_ERROR):
            return
        self.engine.stop()  # join a worker that is still winding down after a failed connect
        config = self.connection_bar.config()
        if config.mode.is_serial and not config.serial_port:
            self._error("Cannot open connection", "Select or enter a serial port first.")
            return
        if not config.mode.is_serial and not config.host:
            self._error("Cannot open connection", "Enter the IP address or host name first.")
            return
        self.project.connection = config
        self.log.add_info(f"Opening {config.mode.label}: {config.describe()}")
        self.engine.start(config, self.project.sets)
        self.connection_bar.set_state(eng.STATE_CONNECTING)
        self._set_status_state(eng.STATE_CONNECTING)
        self._update_actions()

    def close_connection(self) -> None:
        """Stop polling and close the connection."""
        if self.engine.is_running:
            self.engine.stop()
        self.pump_events()
        if self._last_state not in (eng.STATE_CLOSED, eng.STATE_ERROR):
            self._apply_state(eng.StateEvent(eng.STATE_CLOSED))

    def pump_events(self) -> None:
        """Move engine events into the UI (called by a timer; public for tests)."""
        events = self.engine.drain()
        if not events:
            return
        traces: list[eng.TraceEvent] = []
        for event in events:
            if isinstance(event, eng.TraceEvent):
                traces.append(event)
            elif isinstance(event, eng.PollResult):
                self._apply_poll_result(event)
            elif isinstance(event, eng.StateEvent):
                self._apply_state(event)
            elif isinstance(event, eng.WriteResult):
                self._apply_write_result(event)
        if traces:
            self.log.add_trace(traces)
        if self._totals_dirty:
            self._refresh_totals()

    def _apply_poll_result(self, result: eng.PollResult) -> None:
        """Route a poll result to its panel and the totals."""
        panel = self.panels.get(result.set_id)
        if panel is None:
            return
        panel.apply_result(result)
        self.totals.record(result.ok, result.duration_ms, result.error, result.error_kind)
        self._totals_dirty = True
        if not result.ok:
            self.log.add_info(f"{panel.poll_set.name}: {result.error}", error=True)

    def _apply_state(self, event: eng.StateEvent) -> None:
        """Reflect a connection state change."""
        previous = self._last_state
        state = event.state
        if state == eng.STATE_CLOSED and previous == eng.STATE_ERROR:
            self._last_state = eng.STATE_ERROR  # keep showing the error, not "closed"
            self._update_actions()
            return
        self._last_state = state
        self.connection_bar.set_state(state, event.message)
        self._set_status_state(state, event.message)
        connected = state == eng.STATE_OPEN
        for panel in self.panels.values():
            panel.set_connection_state(connected)
            if state == eng.STATE_RECONNECTING:
                panel.apply_connection_error()
        text = {
            eng.STATE_CONNECTING: "Connecting...",
            eng.STATE_OPEN: "Connection open",
            eng.STATE_RECONNECTING: "Connection lost, retrying",
            eng.STATE_CLOSED: "Connection closed",
            eng.STATE_ERROR: "Connection failed",
        }.get(state, state)
        if event.message and state not in (eng.STATE_CLOSED,):
            text = f"{text}: {event.message}"
        self.log.add_info(text, error=state in (eng.STATE_ERROR, eng.STATE_RECONNECTING))
        if state == eng.STATE_ERROR and previous == eng.STATE_CONNECTING:
            self._error("Cannot open connection", event.message)
        self._update_actions()

    def _set_status_state(self, state: str, message: str = "") -> None:
        """Update the left part of the status bar."""
        colors = theme.current()
        color = {
            eng.STATE_OPEN: colors.ok_color,
            eng.STATE_CONNECTING: colors.change_color,
            eng.STATE_RECONNECTING: colors.change_color,
            eng.STATE_ERROR: colors.error_color,
        }.get(state, colors.muted)
        label = {
            eng.STATE_OPEN: "Connected",
            eng.STATE_CONNECTING: "Connecting...",
            eng.STATE_RECONNECTING: "Reconnecting...",
            eng.STATE_ERROR: "Connection error",
            eng.STATE_CLOSED: "Not connected",
        }.get(state, state)
        detail = ""
        if state == eng.STATE_OPEN:
            detail = f" — {self.connection_bar.config().describe()}"
        elif message and state in (eng.STATE_ERROR, eng.STATE_RECONNECTING):
            detail = f" — {message}"
        self.status_state.setText(f"<span style='color:{color}'>●</span> {label}{detail}")

    # ================================================================== writing

    def _write_from_panel(self, set_id: str, address: int, values: list) -> None:
        """A cell edit or coil toggle in a set window."""
        poll_set = self._find_set(set_id)
        if poll_set is None:
            return
        if poll_set.is_bit_set:
            kind = "coil" if len(values) == 1 else "coils"
        else:
            kind = "register" if len(values) == 1 else "registers"
        self.send_write(eng.WriteRequest(poll_set.slave, kind, address, values,
                                         poll_set.timeout_ms))

    def send_write(self, request: eng.WriteRequest,
                   dialog: Optional[WriteDialog] = None) -> Optional[int]:
        """Queue ``request`` if the connection is open. Returns the request id."""
        if self._last_state != eng.STATE_OPEN or not self.engine.is_running:
            message = "Not connected — open the connection to write"
            if dialog is not None:
                dialog.show_result(False, message)
            self._notify(message, error=True)
            return None
        request_id = self.engine.write(request)
        self._pending_writes[request_id] = dialog
        return request_id

    def show_write_dialog(self, set_id: Optional[str] = None) -> None:
        """Open the (non-modal) write dialog, prefilled from a set when given."""
        slave, address, kind = 1, 0, "register"
        if set_id is not None:
            poll_set = self._find_set(set_id)
            panel = self.panels.get(set_id)
            if poll_set is not None and panel is not None:
                slave = poll_set.slave
                address = panel.current_address()
                kind = "coil" if poll_set.is_bit_set else "register"
        elif self.project.sets:
            slave = self.project.sets[0].slave
        if self._write_dialog is None:
            self._write_dialog = WriteDialog(slave, address, kind, self)
            self._write_dialog.submitted.connect(
                lambda request: self.send_write(request, self._write_dialog))
        else:
            self._write_dialog.slave.setValue(slave)
            self._write_dialog.address.setValue(address)
            self._write_dialog.function.setCurrentIndex(
                max(self._write_dialog.function.findData(kind), 0))
        self._write_dialog.show()
        self._write_dialog.raise_()
        self._write_dialog.activateWindow()

    def _apply_write_result(self, result: eng.WriteResult) -> None:
        """Report the outcome of a write."""
        dialog = self._pending_writes.pop(result.request_id, None)
        if result.ok:
            text = f"Write OK: {result.summary}"
            self._notify(text)
        else:
            text = f"Write failed: {result.summary} — {result.error}"
            self._notify(text, error=True)
        if dialog is not None:
            dialog.show_result(result.ok, result.summary if result.ok else result.error)

    # ================================================================== status & misc

    def _totals_changed(self) -> None:
        """Panels report new counters; the status bar refreshes with the next event batch."""
        self._totals_dirty = True

    def _refresh_totals(self) -> None:
        """Update the success / error counters in the status bar."""
        colors = theme.current()
        self._totals_dirty = False
        self.status_ok.setText(
            f"<span style='color:{colors.ok_color}'>✔</span> Reads OK: "
            f"<b>{self.totals.ok:,}</b>")
        error_color = colors.error_color if self.totals.errors else colors.muted
        self.status_err.setText(
            f"<span style='color:{error_color}'>✖</span> Errors: "
            f"<b>{self.totals.errors:,}</b>")
        self.status_err.setToolTip(f"{self.totals.timeouts:,} of them timeouts")

    def reset_counters(self) -> None:
        """Zero all success / error counters."""
        self.totals.reset()
        for panel in self.panels.values():
            panel.reset_counters()
        self._refresh_totals()

    def _notify(self, text: str, error: bool = False) -> None:
        """Show a message in the status bar and the log."""
        self.statusBar().showMessage(text, 6000)
        self.log.add_info(text, error=error)

    def _error(self, title: str, text: str) -> None:
        """Report an error to the user."""
        LOG.warning("%s: %s", title, text)
        self.statusBar().showMessage(f"{title}: {text}", 8000)
        if self.interactive:
            QMessageBox.warning(self, title, text)

    def _update_actions(self) -> None:
        """Enable actions according to the connection state."""
        busy = self._last_state in (eng.STATE_CONNECTING, eng.STATE_OPEN,
                                    eng.STATE_RECONNECTING)
        self.act_connect.setEnabled(not busy)
        self.act_disconnect.setEnabled(busy)

    def _log_visibility_changed(self, visible: bool) -> None:
        """Keep the menu check mark in sync with the dock."""
        self.act_log.blockSignals(True)
        self.act_log.setChecked(visible)
        self.act_log.blockSignals(False)

    def _toggle_log(self, visible: bool) -> None:
        """Show or hide the log dock."""
        self.log_dock.setVisible(visible)

    def set_theme(self, mode: str) -> None:
        """Apply and remember a theme."""
        self.settings.theme = mode
        app = QApplication.instance()
        if app is not None:
            theme.apply(app, theme.resolve(mode, app))
        self.theme_actions[mode].setChecked(True)
        self.log.refresh_theme()
        for panel in self.panels.values():
            panel.refresh_theme()
        self.connection_bar.set_state(self.connection_bar.state)
        self._set_status_state(self._last_state)
        self._refresh_totals()

    def show_preferences(self) -> None:
        """Open the preferences dialog."""
        dialog = PreferencesDialog(self.settings, self)
        if dialog.exec() != PreferencesDialog.DialogCode.Accepted:
            return
        dialog.apply_to(self.settings)
        self.set_theme(self.settings.theme)
        for panel in self.panels.values():
            self._apply_highlights(panel)
        self._autosave_timer.start(self.settings.autosave_seconds * 1000)

    def _apply_highlights(self, panel: SetPanel) -> None:
        """Pass the highlight preferences to ``panel``."""
        s = self.settings
        panel.set_highlights(s.flash_ms / 1000.0 if s.highlight_read else 0.0,
                             s.change_ms / 1000.0 if s.highlight_change else 0.0,
                             s.highlight_error)

    def show_about(self) -> None:
        """Show the about box."""
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<h3>{APP_NAME}</h3><p>Version {__version__}</p>"
            "<p>A Modbus master for serial RTU / ASCII and Modbus TCP, with live "
            "register monitoring.</p>"
            f"<p>Free for non-commercial use under the "
            f"<a href='{LICENSE_URL}'>{LICENSE_NAME}</a>.</p>"
            f"<p style='color:gray'>Python {platform.python_version()} · "
            f"PySide6 {PySide6.__version__} · {sys.platform}</p>")

    # ================================================================== window events

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt naming
        """Accept project files dragged onto the window."""
        urls = event.mimeData().urls()
        if urls and urls[0].toLocalFile().endswith(PROJECT_EXTENSION):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt naming
        """Open a dropped project file."""
        urls = event.mimeData().urls()
        if urls:
            self.open_project(Path(urls[0].toLocalFile()))

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        """Ask about unsaved changes, stop polling, remember the session."""
        if not self._maybe_save():
            event.ignore()
            return
        self._closing = True
        self._pump_timer.stop()
        self._autosave_timer.stop()
        self.engine.stop()
        if self.dirty and self.interactive:
            # The user chose "Discard": forget the unsaved state but keep the last saved file.
            self.settings.clear_session()
            self.settings.store_session(str(self.path) if self.path else "", False)
        else:
            self._autosave()
        self.settings.set_geometry("main", self.saveGeometry())
        self.settings.sync()
        super().closeEvent(event)

