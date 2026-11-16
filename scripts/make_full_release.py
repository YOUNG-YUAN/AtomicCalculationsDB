"""Package a complete, runnable AtomicCalculationDB release into one zip.

The full package contains the native launcher, the PyInstaller runtime,
``app_core``, and a clean ``workspace`` skeleton.  User databases, PDFs,
exports and logs are deliberately excluded; the optional ONNX models are
included when available so the unzipped program works offline.

Usage::

    python scripts/make_full_release.py
    python scripts/make_full_release.py --root dist
    python scripts/make_full_release.py --root build/release_v2.2 \
        --runtime dist/runtime --models dist/workspace/models \
        --output release/AtomicCalculationDB_Full_v2.2.0.zip

By default the archive is written to the top-level ``release/`` directory.
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "AtomicCalculationDB"
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", ".mypy_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp"}
WORKSPACE_DIRS = (
    "storage",
    "inbox_manual",
    "morning_reports",
    "exports",
    "logs",
    "models",
    "vector_db",
    "sync_snapshots",
    "sync_incoming",
    "browser_profile",
)


def _version(root: Path) -> str:
    try:
        data = json.loads((root / "app_core" / "version.json").read_text(encoding="utf-8"))
        return str(data.get("version") or "unknown")
    except (OSError, ValueError):
        return "unknown"


def _iter_files(base: Path, skip_dirs: set[str] | None = None,
                skip_suffixes: set[str] | None = None) -> Iterable[Path]:
    skip_dirs = skip_dirs or SKIP_DIRS
    skip_suffixes = skip_suffixes or SKIP_SUFFIXES
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if any(part in skip_dirs for part in relative.parts):
            continue
        if path.suffix.lower() in skip_suffixes:
            continue
        yield path


def _add_tree(archive: zipfile.ZipFile, source: Path, prefix: str,
              *, skip_dirs: set[str] | None = None,
              skip_suffixes: set[str] | None = None) -> int:
    count = 0
    if not source.is_dir():
        return 0
    for path in _iter_files(source, skip_dirs, skip_suffixes):
        relative = path.relative_to(source).as_posix()
        archive.write(path, f"{prefix}/{relative}")
        count += 1
    return count


def _add_workspace(archive: zipfile.ZipFile, models: Path | None,
                   include_models: bool) -> int:
    prefix = f"{PACKAGE_NAME}/workspace"
    archive.writestr(
        f"{prefix}/README.txt",
        "此目录保存本机文献、SQLite 数据库、向量库与日志。\n"
        "完整发布包只提供空目录结构，不包含任何用户数据。\n"
        "首次启动后程序会自动创建 config.yaml；迁移时请整体备份此目录。\n",
    )
    for name in WORKSPACE_DIRS:
        archive.writestr(f"{prefix}/{name}/", b"")
    count = 1 + len(WORKSPACE_DIRS)
    if include_models and models is not None and models.is_dir():
        count += _add_tree(
            archive,
            models.resolve(),
            f"{prefix}/models",
            skip_dirs=set(),
            skip_suffixes=set(),
        )
        if not any(models.iterdir()):
            archive.writestr(f"{prefix}/models/README.txt",
                             "可将 ONNX 模型放入此目录，或在设置页一键下载。\n")
    return count


def build_full_release(
    source_root: str | Path,
    *,
    output: str | Path | None = None,
    runtime: str | Path | None = None,
    models: str | Path | None = None,
    include_models: bool = True,
    compresslevel: int = 6,
) -> Path:
    """Build a zip from an already assembled release root."""
    root = Path(source_root).resolve()
    version = _version(root)
    output_path = Path(output) if output else (
        ROOT / "release" / f"AtomicCalculationDB_Full_v{version}.zip"
    )
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    runtime_path = Path(runtime).resolve() if runtime else (root / "runtime")
    if not runtime_path.is_dir():
        raise FileNotFoundError(f"runtime 不存在: {runtime_path}")
    if not (root / "app_core" / "version.json").is_file():
        raise FileNotFoundError(f"找不到 app_core/version.json: {root}")
    launcher = root / "AtomicCalculationDB.exe"
    if not launcher.is_file():
        raise FileNotFoundError(f"找不到启动器: {launcher}")

    models_path = Path(models) if models else (root / "workspace" / "models")
    if include_models and not models_path.is_dir():
        include_models = False

    print(f"Packing AtomicCalculationDB {version} -> {output_path}")
    print(f"  launcher : {launcher}")
    print(f"  runtime  : {runtime_path}")
    print(f"  app_core : {root / 'app_core'}")
    print(f"  models   : {models_path if include_models else 'not included'}")

    added = 0
    with zipfile.ZipFile(
        output_path, "w", compression=zipfile.ZIP_DEFLATED,
        compresslevel=max(0, min(9, int(compresslevel))),
    ) as archive:
        archive.write(launcher, f"{PACKAGE_NAME}/AtomicCalculationDB.exe")
        added += 1
        for name in ("README.md", "requirements.txt"):
            source = root / name
            if source.is_file():
                archive.write(source, f"{PACKAGE_NAME}/{name}")
                added += 1
        added += _add_tree(
            archive,
            root / "app_core",
            f"{PACKAGE_NAME}/app_core",
        )
        added += _add_tree(
            archive,
            runtime_path,
            f"{PACKAGE_NAME}/runtime",
            skip_dirs=set(),
            skip_suffixes={".log", ".tmp"},
        )
        added += _add_workspace(archive, models_path, include_models and models_path.is_dir())

    size_mb = output_path.stat().st_size / 1024 / 1024
    print(f"  files    : {added}")
    print(f"  zip size : {size_mb:.1f} MB")
    print(f"Done: {output_path}")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a complete AtomicCalculationDB release zip")
    parser.add_argument("--root", type=Path, default=ROOT / "dist",
                        help="release root containing AtomicCalculationDB.exe/app_core/runtime")
    parser.add_argument("--runtime", type=Path, default=None,
                        help="runtime directory (default: <root>/runtime)")
    parser.add_argument("--models", type=Path, default=None,
                        help="optional ONNX model directory (default: <root>/workspace/models)")
    parser.add_argument("--output", type=Path, default=None,
                        help="output zip (default: release/AtomicCalculationDB_Full_v<version>.zip)")
    parser.add_argument("--no-models", dest="include_models", action="store_false",
                        default=True,
                        help="do not include ONNX models")
    parser.add_argument("--compresslevel", type=int, default=6,
                        help="zip compression level 0-9 (default: 6)")
    args = parser.parse_args()
    try:
        build_full_release(
            args.root,
            output=args.output,
            runtime=args.runtime,
            models=args.models,
            include_models=args.include_models,
            compresslevel=args.compresslevel,
        )
    except Exception as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
