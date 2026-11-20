"""CLI entry point for python -m app_core."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure app_core directory is at the head of sys.path
app_core_dir = Path(__file__).resolve().parent
if str(app_core_dir) not in sys.path:
    sys.path.insert(0, str(app_core_dir))

from main_entry import main

if __name__ == "__main__":
    sys.exit(main())
