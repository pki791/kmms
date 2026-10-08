"""Allow ``python -m kmms``."""

import sys

from kmms.app import main

if __name__ == "__main__":
    sys.exit(main())
