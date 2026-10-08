"""Application bootstrap and command line handling."""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path
from typing import Optional
from typing import Sequence

from PySide6.QtCore import QCoreApplication
from PySide6.QtCore import QSettings
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from kmms import APP_NAME
from kmms import __version__
from kmms.gui import theme
from kmms.gui.app_settings import AppSettings
from kmms.gui.icon import make_icon
from kmms.gui.main_window import MainWindow

LOG = logging.getLogger(__name__)


def parse_args(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    """Parse our options; everything unknown (Qt options) is passed through."""
    parser = argparse.ArgumentParser(prog="kmms", description=f"{APP_NAME} {__version__}")
    parser.add_argument("project", nargs="?", help="project file (.kmms) to open")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_known_args(list(argv)[1:])


def selftest(app: QApplication) -> int:
    """Create the full UI with a sample set, render it and exit.

    Used to verify that a packaged binary can start (Qt plugins, imports, fonts). It
    uses a throw-away settings location so nothing of the user's data is touched.
    """
    from kmms.model import PollSet
    from kmms.protocol import build_rtu_frame
    from kmms.transport import list_serial_ports

    with tempfile.TemporaryDirectory() as tmp:
        store = QSettings(str(Path(tmp) / "selftest.ini"), QSettings.Format.IniFormat)
        window = MainWindow(AppSettings(store, Path(tmp)), interactive=False)
        window.show()
        window.insert_set(PollSet(name="Selftest", length=4))
        window.tile_grid()
        QTimer.singleShot(150, app.quit)
        app.exec()
        image = window.grab()
        window.engine.stop()
        ports = list_serial_ports()
        frame = build_rtu_frame(1, bytes.fromhex("0300000001"))
        ok = (not image.isNull() and image.width() > 100 and frame.hex() == "010300000001840a")
        print(f"selftest {'OK' if ok else 'FAILED'}: window {image.width()}x{image.height()}, "
              f"{len(ports)} serial port(s) found, Qt platform '{app.platformName()}'")
        return 0 if ok else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Start the GUI. An optional first argument is a project file to open."""
    args = list(sys.argv if argv is None else argv)
    options, qt_args = parse_args(args)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    QCoreApplication.setOrganizationName("Kuna")
    QCoreApplication.setApplicationName("KMMS")
    app = QApplication([args[0], *qt_args])
    app.setApplicationDisplayName(APP_NAME)
    app.setWindowIcon(make_icon())

    if options.selftest:
        theme.apply(app, theme.LIGHT)
        return selftest(app)

    settings = AppSettings()
    theme.apply(app, theme.resolve(settings.theme, app))
    window = MainWindow(settings)

    def system_scheme_changed(*_args: object) -> None:
        """Follow the OS appearance while the theme is set to 'system'."""
        if settings.theme == "system":
            window.set_theme("system")

    app.styleHints().colorSchemeChanged.connect(system_scheme_changed)
    window.show()
    if options.project and Path(options.project).is_file():
        window.open_project(Path(options.project))
    else:
        window.restore_session()
    return app.exec()
