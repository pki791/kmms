"""Command line handling and the self-test used to verify packaged binaries."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from kmms.app import parse_args

ROOT = Path(__file__).resolve().parent.parent


def test_parse_args_passes_qt_options_through():
    """Our options are parsed; unknown (Qt) options are left alone."""
    options, rest = parse_args(["kmms", "plant.kmms", "-platform", "offscreen"])
    assert options.project == "plant.kmms" and not options.selftest
    assert rest == ["-platform", "offscreen"]
    options, rest = parse_args(["kmms"])
    assert options.project is None and rest == []


def _run(*args: str) -> subprocess.CompletedProcess:
    """Run the entry script in a subprocess without a display."""
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    return subprocess.run([sys.executable, str(ROOT / "run_kmms.py"), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def test_version_flag():
    """--version prints the name and version."""
    result = _run("--version")
    assert result.returncode == 0 and "Kuna's Modbus Master Simulator" in result.stdout


def test_selftest_starts_the_whole_ui():
    """--selftest builds the window, renders it and exits 0."""
    result = _run("--selftest")
    assert result.returncode == 0, result.stderr
    assert "selftest OK" in result.stdout
