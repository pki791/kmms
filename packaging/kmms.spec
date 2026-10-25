# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one self-contained executable per platform.

Build with ``python packaging/build.py`` (which also renders the icons first).
Environment: KMMS_ICON_DIR points at the rendered icons (default: build/icons).
"""
import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
ICON_DIR = Path(os.environ.get("KMMS_ICON_DIR", ROOT / "build" / "icons"))

sys.path.insert(0, str(ROOT / "src"))
from kmms import __version__  # noqa: E402

if sys.platform == "win32":
    icon = ICON_DIR / "kmms.ico"
elif sys.platform == "darwin":
    icon = ICON_DIR / "kmms.icns"
else:
    icon = None
icon_arg = str(icon) if icon is not None and icon.exists() else None

# Qt modules the application does not use; excluding them keeps the binary small.
UNUSED_QT = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebView", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQuick3D", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtWebSockets",
    "PySide6.QtWebChannel", "PySide6.QtRemoteObjects", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtNetwork", "PySide6.QtXml", "PySide6.QtConcurrent", "PySide6.QtStateMachine",
    "PySide6.QtScxml", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
    "PySide6.QtHttpServer", "PySide6.QtGraphs", "PySide6.QtGraphsWidgets",
]
UNUSED_PY = ["tkinter", "pytest", "pymodbus", "unittest", "pydoc", "test", "distutils",
             "setuptools", "pip", "numpy"]

a = Analysis(
    [str(ROOT / "run_kmms.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=UNUSED_QT + UNUSED_PY,
    noarchive=False,
)

# PyInstaller's Qt hooks also bundle plugins that pull in whole Qt modules (QML, Quick, PDF,
# SVG, virtual keyboard, software OpenGL). The application uses none of them.
import re  # noqa: E402

_DROP = re.compile(
    r"(Qt6?Qml|Qt6?Quick|Qt6?VirtualKeyboard|Qt6?Pdf|Qt6?Svg|opengl32sw|qsvg|qpdf|"
    r"virtualkeyboard|qtuiotouchplugin|qtvirtualkeyboardplugin)", re.IGNORECASE)
a.binaries = [entry for entry in a.binaries if not _DROP.search(entry[0])]

pyz = PYZ(a.pure)

# Windows and Linux: one self-contained executable (onefile).
# macOS: a .app bundle (one zip to distribute). A onefile executable inside a bundle works but
# unpacks ~100 MB on every launch (about 7 s) and PyInstaller deprecates it, so the bundle
# keeps its libraries inside instead.
ONEFILE = sys.platform != "darwin"

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="KMMS",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        icon=icon_arg,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="KMMS",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon=icon_arg,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="KMMS")
    app = BUNDLE(
        coll,
        name="KMMS.app",
        icon=icon_arg,
        bundle_identifier="com.kuna.kmms",
        info_plist={
            "CFBundleName": "KMMS",
            "CFBundleDisplayName": "Kuna's Modbus Master Simulator",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "KMMS Project",
                "CFBundleTypeExtensions": ["kmms"],
                "CFBundleTypeRole": "Editor",
            }],
        },
    )
