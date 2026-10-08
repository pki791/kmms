"""Entry point used for running from source and as the PyInstaller script."""

import sys
from pathlib import Path

try:
    from kmms.app import main
except ModuleNotFoundError:  # running from a source checkout without installing
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from kmms.app import main

if __name__ == "__main__":
    sys.exit(main())
