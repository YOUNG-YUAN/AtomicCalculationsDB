"""Build a hot-update package from ``app_core``.

Usage::

    python tools/build_update.py --version 0.1.1 --build 20261101
    python tools/build_update.py --output dist/my-update.zip

The resulting zip contains the *contents* of ``app_core`` at its root
(``version.json``, ``main_entry.py``, ``ui/``, ``db/`` ...), which is exactly
what ``launcher.py`` expects in ``workspace/pending_update.zip``.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app_core"))

from updater.updater import UpdateError, inspect_package  # noqa: E402

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp"}


def collect_files(core: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(core.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(core)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an AtomicCalculationDB update package")
    parser.add_argument("--version", help="Override version.json version")
    parser.add_argument("--build", help="Override version.json build")
    parser.add_argument("--channel", help="Override version.json channel")
    parser.add_argument(
        "--core",
        type=Path,
        default=ROOT / "app_core",
        help="app_core directory to package (default: project app_core)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output zip path (default: dist/update-<version>.zip)",
    )
    args = parser.parse_args()

    core = args.core.resolve()
    if not core.is_dir():
        print(f"error: app_core not found: {core}", file=sys.stderr)
        return 2

    version_path = core / "version.json"
    try:
        version = json.loads(version_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {version_path}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(version, dict):
        print("error: version.json must contain an object", file=sys.stderr)
        return 2
    if args.version:
        version["version"] = args.version
    if args.build:
        version["build"] = args.build
    if args.channel:
        version["channel"] = args.channel
    if not version.get("version"):
        print("error: version.json needs a version", file=sys.stderr)
        return 2

    default_name = f"update-{version['version']}.zip"
    output = (args.output or (ROOT / "dist" / default_name)).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    files = collect_files(core)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            relative = path.relative_to(core)
            if relative.as_posix() == "version.json":
                continue
            archive.write(path, relative.as_posix())
        archive.writestr(
            "version.json",
            json.dumps(version, ensure_ascii=False, indent=2) + "\n",
        )

    try:
        info = inspect_package(output)
    except UpdateError as exc:
        output.unlink(missing_ok=True)
        print(f"error: generated package failed validation: {exc}", file=sys.stderr)
        return 3

    print(f"Built {output}")
    print(
        f"  version={info.version} build={info.build} "
        f"files={info.entries} size={output.stat().st_size / 1024:.1f} KiB"
    )
    print("  Import it via Settings -> 导入更新包 (.zip)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
