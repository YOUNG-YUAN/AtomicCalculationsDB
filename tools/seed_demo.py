"""Seed or clear the V2 demo dataset from the command line.

    python tools/seed_demo.py
    python tools/seed_demo.py --clear
    python tools/seed_demo.py --workspace path/to/workspace
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app_core"))

from db.demo_v2 import seed_demo_v2  # noqa: E402
from db.sqlite_manager import SQLiteManager  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed / clear AtomicCalculationDB V2 demo data")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=ROOT / "workspace",
        help="Workspace directory (default: project workspace)",
    )
    parser.add_argument("--clear", action="store_true", help="Delete demo-flagged documents")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    db = SQLiteManager(workspace)
    try:
        if args.clear:
            count = db.clear_demo_documents()
            print(f"Cleared {count} demo documents from {workspace}")
        else:
            counts = seed_demo_v2(db)
            print(
                f"Seeded {counts['documents']} documents, {counts['cards']} cards, "
                f"{counts['keywords']} keywords into {workspace}"
            )
        print("Stats:", db.stats())
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
