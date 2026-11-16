"""Create a distributable AtomicCalculationDB patch zip from the current core.

Example::

    python scripts/make_update_patch.py --version 2.2.3 --build 20261101

Output defaults to ``release/AtomicCalculationDB_Patch_v<version>.zip`` and can
be imported from the running GUI's ``📦 导入更新包`` button.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    version_file = ROOT / "app_core" / "version.json"
    current = json.loads(version_file.read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description="Build an AtomicCalculationDB update patch")
    parser.add_argument("--version", default=str(current.get("version") or "2.1.0"))
    parser.add_argument("--build", default=str(current.get("build") or "20261031"))
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output zip (default: release/AtomicCalculationDB_Patch_v<version>.zip)",
    )
    args = parser.parse_args()
    output = args.output or (
        ROOT / "release" / f"AtomicCalculationDB_Patch_v{args.version}.zip"
    )
    command = [
        sys.executable,
        str(ROOT / "tools" / "build_update.py"),
        "--version",
        str(args.version),
        "--build",
        str(args.build),
        "--output",
        str(output),
    ]
    completed = subprocess.run(command, cwd=ROOT)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
