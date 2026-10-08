"""Build the single-file executable for the current platform and smoke-test it.

Usage: python packaging/build.py
Result: dist/KMMS-<version>-<os>-<arch>(.exe | .zip containing KMMS.app | no extension)
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kmms import __version__  # noqa: E402


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, echoing it, and fail loudly."""
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=True, cwd=ROOT, **kwargs)


def platform_tag() -> str:
    """Return e.g. ``macos-arm64``, ``windows-x64``, ``linux-x64``."""
    system = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine, machine)
    return f"{system}-{arch}"


def frozen_executable() -> Path:
    """Return the path of the executable PyInstaller produced."""
    if sys.platform == "darwin":
        return ROOT / "dist" / "KMMS.app" / "Contents" / "MacOS" / "KMMS"
    if sys.platform == "win32":
        return ROOT / "dist" / "KMMS.exe"
    return ROOT / "dist" / "KMMS"


def main() -> int:
    """Render icons, run PyInstaller, smoke-test and name the artifact."""
    icon_dir = ROOT / "build" / "icons"
    run([sys.executable, "packaging/make_icons.py", str(icon_dir)])
    shutil.rmtree(ROOT / "dist", ignore_errors=True)
    env = {**os.environ, "KMMS_ICON_DIR": str(icon_dir)}
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--workpath", str(ROOT / "build" / "pyinstaller"), "--distpath", str(ROOT / "dist"),
         "packaging/kmms.spec"], env=env)

    exe = frozen_executable()
    if not exe.exists():
        print(f"ERROR: expected {exe} was not produced", file=sys.stderr)
        return 1

    name = f"KMMS-{__version__}-{platform_tag()}"
    if sys.platform == "darwin":
        artifact = ROOT / "dist" / f"{name}.zip"
        # ditto keeps the symlinks inside the bundle; Python's zipfile would break them.
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
             str(ROOT / "dist" / "KMMS.app"), str(artifact)])
        shutil.rmtree(ROOT / "dist" / "KMMS.app")
        shutil.rmtree(ROOT / "dist" / "KMMS", ignore_errors=True)  # PyInstaller's COLLECT dir
    else:
        artifact = ROOT / "dist" / (name + (".exe" if sys.platform == "win32" else ""))
        exe.rename(artifact)

    # Test what will actually be shipped: the extracted archive / the renamed binary.
    with tempfile.TemporaryDirectory() as tmp:
        if sys.platform == "darwin":
            run(["ditto", "-x", "-k", str(artifact), tmp])
            candidate = Path(tmp) / "KMMS.app" / "Contents" / "MacOS" / "KMMS"
        else:
            candidate = Path(tmp) / artifact.name
            shutil.copy2(artifact, candidate)
        if not smoke_test(candidate):
            print("ERROR: packaged binary failed its self-test", file=sys.stderr)
            return 1
    size_mb = artifact.stat().st_size / 1_048_576
    print(f"Built {artifact.relative_to(ROOT)}  ({size_mb:.1f} MB)")
    return 0


def smoke_test(executable: Path) -> bool:
    """Run ``--selftest``. By default on Qt's offscreen platform; set
    KMMS_SELFTEST_PLATFORM=native to use the real windowing system (needs a display)."""
    print(f"Smoke-testing {executable.name} ...", flush=True)
    env = dict(os.environ)
    if os.environ.get("KMMS_SELFTEST_PLATFORM", "offscreen") != "native":
        env["QT_QPA_PLATFORM"] = "offscreen"
    else:
        env.pop("QT_QPA_PLATFORM", None)
    started = time.monotonic()
    result = subprocess.run([str(executable), "--selftest"], env=env, capture_output=True,
                            text=True, timeout=240)
    print(result.stdout.strip() or "(no output; exit code decides)")
    print(f"  finished in {time.monotonic() - started:.1f} s with exit code {result.returncode}")
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
    return result.returncode == 0


if __name__ == "__main__":
    sys.exit(main())
