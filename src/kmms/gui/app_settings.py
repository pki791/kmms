"""Per-user application settings (QSettings) and the preferences dialog."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QByteArray
from PySide6.QtCore import QSettings
from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QCheckBox
from PySide6.QtWidgets import QComboBox
from PySide6.QtWidgets import QDialog
from PySide6.QtWidgets import QDialogButtonBox
from PySide6.QtWidgets import QFormLayout
from PySide6.QtWidgets import QSpinBox
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget

LOG = logging.getLogger(__name__)

MAX_RECENT = 10


class AppSettings:
    """Typed access to the values persisted between sessions."""

    def __init__(self, settings: Optional[QSettings] = None,
                 data_dir: Optional[Path] = None) -> None:
        """Wrap ``settings`` (default: the application's QSettings)."""
        self._s = settings if settings is not None else QSettings()
        if data_dir is None:
            location = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppDataLocation)
            data_dir = Path(location) if location else Path.home() / ".kmms"
        self.data_dir = data_dir

    # ------------------------------------------------------------------ preferences

    @property
    def theme(self) -> str:
        """Return ``system``, ``light`` or ``dark``."""
        value = str(self._s.value("ui/theme", "system"))
        return value if value in ("system", "light", "dark") else "system"

    @theme.setter
    def theme(self, value: str) -> None:
        self._s.setValue("ui/theme", value)

    @property
    def restore_session(self) -> bool:
        """Return True when the previous workspace is reopened on start."""
        return _as_bool(self._s.value("session/restore", True))

    @restore_session.setter
    def restore_session(self, value: bool) -> None:
        self._s.setValue("session/restore", bool(value))

    @property
    def flash_ms(self) -> int:
        """Duration of the green 'value updated' flash."""
        return _as_int(self._s.value("ui/flash_ms", 350), 350, 20, 5000)

    @flash_ms.setter
    def flash_ms(self, value: int) -> None:
        self._s.setValue("ui/flash_ms", int(value))

    @property
    def change_ms(self) -> int:
        """Duration of the amber 'value changed' highlight."""
        return _as_int(self._s.value("ui/change_ms", 1500), 1500, 100, 30000)

    @change_ms.setter
    def change_ms(self, value: int) -> None:
        self._s.setValue("ui/change_ms", int(value))

    @property
    def highlight_read(self) -> bool:
        """Flash every register that was read (green)."""
        return _as_bool(self._s.value("ui/highlight_read", True))

    @highlight_read.setter
    def highlight_read(self, value: bool) -> None:
        self._s.setValue("ui/highlight_read", bool(value))

    @property
    def highlight_change(self) -> bool:
        """Mark registers whose value changed (amber)."""
        return _as_bool(self._s.value("ui/highlight_change", True))

    @highlight_change.setter
    def highlight_change(self, value: bool) -> None:
        self._s.setValue("ui/highlight_change", bool(value))

    @property
    def highlight_error(self) -> bool:
        """Blink the whole set red on a read error."""
        return _as_bool(self._s.value("ui/highlight_error", True))

    @highlight_error.setter
    def highlight_error(self, value: bool) -> None:
        self._s.setValue("ui/highlight_error", bool(value))

    @property
    def autosave_seconds(self) -> int:
        """Interval of the automatic session save."""
        return _as_int(self._s.value("session/autosave_s", 30), 30, 5, 3600)

    @autosave_seconds.setter
    def autosave_seconds(self, value: int) -> None:
        self._s.setValue("session/autosave_s", int(value))

    # ------------------------------------------------------------------ recent files

    @property
    def recent_files(self) -> list[str]:
        """Return the recently used project paths, newest first."""
        value = self._s.value("files/recent", [])
        if isinstance(value, str):
            value = [value]
        return [str(v) for v in (value or []) if v][:MAX_RECENT]

    def add_recent(self, path: str) -> None:
        """Move ``path`` to the top of the recent list."""
        files = [f for f in self.recent_files if f != path]
        files.insert(0, path)
        self._s.setValue("files/recent", files[:MAX_RECENT])

    def remove_recent(self, path: str) -> None:
        """Forget ``path`` (for example because it no longer exists)."""
        self._s.setValue("files/recent", [f for f in self.recent_files if f != path])

    def clear_recent(self) -> None:
        """Empty the recent list."""
        self._s.setValue("files/recent", [])

    @property
    def last_dir(self) -> str:
        """Directory last used in a file dialog."""
        return str(self._s.value("files/last_dir", str(Path.home())))

    @last_dir.setter
    def last_dir(self, value: str) -> None:
        self._s.setValue("files/last_dir", value)

    # ------------------------------------------------------------------ session

    @property
    def autosave_path(self) -> Path:
        """Return the file the working state is written to."""
        return self.data_dir / "autosave.kmms"

    @property
    def session_path(self) -> str:
        """Path of the project the autosave belongs to (may be empty for untitled)."""
        return str(self._s.value("session/path", ""))

    @property
    def session_dirty(self) -> bool:
        """Whether the autosaved state had unsaved changes."""
        return _as_bool(self._s.value("session/dirty", False))

    def store_session(self, path: str, dirty: bool) -> None:
        """Remember which project the autosave belongs to."""
        self._s.setValue("session/path", path)
        self._s.setValue("session/dirty", bool(dirty))

    def clear_session(self) -> None:
        """Forget the autosave so the next start begins with an empty workspace."""
        try:
            self.autosave_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            LOG.warning("Could not remove autosave: %s", exc)
        self._s.setValue("session/path", "")
        self._s.setValue("session/dirty", False)

    # ------------------------------------------------------------------ window geometry

    def geometry(self, key: str) -> QByteArray:
        """Return stored window geometry/state bytes."""
        value = self._s.value(f"window/{key}")
        return value if isinstance(value, QByteArray) else QByteArray()

    def set_geometry(self, key: str, value: QByteArray) -> None:
        """Store window geometry/state bytes."""
        self._s.setValue(f"window/{key}", value)

    def sync(self) -> None:
        """Flush settings to disk."""
        self._s.sync()


def _as_bool(value: object) -> bool:
    """QSettings may hand back strings on some platforms."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_int(value: object, default: int, low: int, high: int) -> int:
    """Convert a stored value to a bounded integer."""
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


class PreferencesDialog(QDialog):
    """Edit the :class:`AppSettings` preferences."""

    def __init__(self, settings: AppSettings, parent: Optional[QWidget] = None) -> None:
        """Populate the dialog from ``settings``."""
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(380)
        self.theme = QComboBox()
        for key, label in (("system", "Follow system"), ("light", "Light"), ("dark", "Dark")):
            self.theme.addItem(label, key)
        self.theme.setCurrentIndex(max(self.theme.findData(settings.theme), 0))
        self.flash = QSpinBox()
        self.flash.setRange(20, 5000)
        self.flash.setSuffix(" ms")
        self.flash.setValue(settings.flash_ms)
        self.change = QSpinBox()
        self.change.setRange(100, 30000)
        self.change.setSuffix(" ms")
        self.change.setSingleStep(100)
        self.change.setValue(settings.change_ms)
        self.flash_on = QCheckBox("Flash on read (green)")
        self.flash_on.setChecked(settings.highlight_read)
        self.flash_on.toggled.connect(self.flash.setEnabled)
        self.flash.setEnabled(settings.highlight_read)
        self.change_on = QCheckBox("Highlight on change (amber)")
        self.change_on.setChecked(settings.highlight_change)
        self.change_on.toggled.connect(self.change.setEnabled)
        self.change.setEnabled(settings.highlight_change)
        self.error_on = QCheckBox("Blink on error (red)")
        self.error_on.setToolTip("When off, a failing set only gets a faint red tint")
        self.error_on.setChecked(settings.highlight_error)
        self.restore = QCheckBox("Reopen the last workspace on start")
        self.restore.setChecked(settings.restore_session)
        self.autosave = QSpinBox()
        self.autosave.setRange(5, 3600)
        self.autosave.setSuffix(" s")
        self.autosave.setValue(settings.autosave_seconds)

        form = QFormLayout()
        form.addRow("Theme", self.theme)
        form.addRow(self.flash_on, self.flash)
        form.addRow(self.change_on, self.change)
        form.addRow(self.error_on)
        form.addRow("", self.restore)
        form.addRow("Autosave interval", self.autosave)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def apply_to(self, settings: AppSettings) -> None:
        """Store the dialog's values."""
        settings.theme = str(self.theme.currentData())
        settings.flash_ms = self.flash.value()
        settings.change_ms = self.change.value()
        settings.highlight_read = self.flash_on.isChecked()
        settings.highlight_change = self.change_on.isChecked()
        settings.highlight_error = self.error_on.isChecked()
        settings.restore_session = self.restore.isChecked()
        settings.autosave_seconds = self.autosave.value()
