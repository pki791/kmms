"""Render the application icon to PNG / ICO / ICNS files for packaging.

Usage: python packaging/make_icons.py <output-dir>
The ICNS file is only produced on macOS (it needs the ``iconutil`` tool).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QGuiApplication  # noqa: E402

from kmms.gui.icon import make_pixmap  # noqa: E402


def main(out_dir: Path) -> None:
    """Write kmms.png, kmms.ico and (on macOS) kmms.icns into ``out_dir``."""
    app = QGuiApplication([])  # noqa: F841 - needed for pixmap rendering
    out_dir.mkdir(parents=True, exist_ok=True)
    make_pixmap(512).save(str(out_dir / "kmms.png"), "PNG")

    # Windows icon: a multi-size ICO written by Qt's ICO plugin (largest image is 256 px).
    ico_images = [make_pixmap(size).toImage() for size in (16, 24, 32, 48, 64, 128, 256)]
    if not ico_images[-1].save(str(out_dir / "kmms.ico"), "ICO"):
        print("warning: Qt could not write an ICO file", file=sys.stderr)

    if sys.platform == "darwin" and shutil.which("iconutil"):
        iconset = out_dir / "kmms.iconset"
        iconset.mkdir(exist_ok=True)
        for size in (16, 32, 128, 256, 512):
            make_pixmap(size).save(str(iconset / f"icon_{size}x{size}.png"), "PNG")
            make_pixmap(size * 2).save(str(iconset / f"icon_{size}x{size}@2x.png"), "PNG")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o",
                        str(out_dir / "kmms.icns")], check=True)
        shutil.rmtree(iconset)
    print(f"icons written to {out_dir}: {sorted(p.name for p in out_dir.iterdir())}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "build/icons"))
