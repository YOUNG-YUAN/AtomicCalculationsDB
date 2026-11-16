"""Build the tiny native launcher + external PyInstaller runtime.

Result::

    dist/AtomicCalculationDB.exe          <- native shell, ~100 KiB
    dist/runtime/                         <- Python/Qt/ONNX/matplotlib runtime
        AtomicCalculationDB_Runtime.exe
        _internal/...
    dist/app_core/                        <- hot-updatable business code
    dist/workspace/                       <- user data (preserved on rebuild)

The .exe does not contain Python or Qt.  It starts the onedir runtime and
waits; all arguments (including ``--mcp``) are forwarded unchanged, so stdio
MCP works.  ``ACDB_RELEASE_ROOT`` tells the frozen shell where app_core and
workspace live.

Release archives are written to the top-level ``release/`` directory so full
packages and hot-update patches share one predictable location.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME_SPEC = ROOT / "AtomicCalculationDB_Runtime.spec"
NATIVE_SOURCE = ROOT / "native_launcher" / "launcher.c"
NATIVE_RC = ROOT / "native_launcher" / "launcher.rc"
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", ".mypy_cache"}


def _copy_core(target: Path) -> None:
    source = ROOT / "app_core"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.pyc", "*.pyo"),
    )


def _prepare_workspace(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for relative in (
        "logs",
        "storage",
        "inbox_manual",
        "morning_reports",
        "exports",
        "models",
        "vector_db",
        "sync_snapshots",
        "sync_incoming",
        "browser_profile",
    ):
        (target / relative).mkdir(parents=True, exist_ok=True)


def _build_runtime(dist: Path, *, clean: bool) -> Path:
    runtime = dist / "runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(RUNTIME_SPEC),
        "--noconfirm",
        "--distpath",
        str(dist),
    ]
    if clean:
        command.append("--clean")
    print("Running:", " ".join(command))
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    runtime_exe = runtime / "AtomicCalculationDB_Runtime.exe"
    if not runtime_exe.exists():
        raise SystemExit("PyInstaller did not produce dist/runtime/AtomicCalculationDB_Runtime.exe")
    return runtime_exe


def _copy_spacy_model(runtime: Path) -> None:
    import sysconfig

    source = Path(sysconfig.get_paths().get("purelib", ""))
    target = runtime / "_internal"
    for pattern in ("en_core_web_sm", "en_core_web_sm-*.dist-info"):
        for item in source.glob(pattern):
            destination = target / item.name
            if destination.exists():
                shutil.rmtree(destination) if destination.is_dir() else destination.unlink()
            if item.is_dir():
                shutil.copytree(item, destination)


def _build_native(dist: Path) -> Path:
    gcc = shutil.which("gcc")
    windres = shutil.which("windres")
    if not gcc or not windres:
        raise SystemExit(
            "gcc/windres not found. Install MinGW-w64 (Strawberry Perl's C toolchain works)."
        )
    build_dir = ROOT / "build" / "native"
    build_dir.mkdir(parents=True, exist_ok=True)
    resource = build_dir / "launcher_res.o"
    subprocess.run(
        [windres, str(NATIVE_RC), "-O", "coff", "-o", str(resource)],
        cwd=ROOT,
        check=True,
    )
    output = dist / "AtomicCalculationDB.exe"
    subprocess.run(
        [
            gcc,
            "-O2",
            "-s",
            "-municode",
            "-mwindows",
            str(NATIVE_SOURCE),
            str(resource),
            "-o",
            str(output),
            "-lshell32",
        ],
        cwd=ROOT,
        check=True,
    )
    if not output.exists():
        raise SystemExit("native launcher compilation failed")
    return output


def _size_mb(path: Path) -> float:
    total = 0
    if path.is_file():
        return path.stat().st_size / 1024 / 1024
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total / 1024 / 1024


def main() -> int:
    parser = argparse.ArgumentParser(description="Build AtomicCalculationDB tiny launcher release")
    parser.add_argument("--clean", action="store_true", default=True)
    parser.add_argument("--no-clean", dest="clean", action="store_false")
    parser.add_argument(
        "--dist",
        type=Path,
        default=ROOT / "dist",
        help="Output directory (default: dist/)",
    )
    parser.add_argument(
        "--release-dir",
        type=Path,
        default=ROOT / "release",
        help="Directory for full/patch zip archives (default: release/)",
    )
    parser.add_argument(
        "--no-full-zip",
        dest="full_zip",
        action="store_false",
        default=True,
        help="Skip the complete release zip",
    )
    parser.add_argument(
        "--no-patch",
        dest="patch",
        action="store_false",
        default=True,
        help="Skip the app_core hot-update zip",
    )
    args = parser.parse_args()
    dist = args.dist.resolve()
    dist.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(ROOT / "app_core"))
    from ui.icon_assets import ensure_assets

    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        ensure_assets(force=True)
        del app
    except Exception as exc:
        raise SystemExit(f"无法生成品牌图标资源: {exc}")

    runtime_exe = _build_runtime(dist, clean=args.clean)
    _copy_spacy_model(runtime_exe.parent)
    exe = _build_native(dist)
    _copy_core(dist / "app_core")
    _prepare_workspace(dist / "workspace")
    for name in ("README.md", "requirements.txt"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, dist / name)

    version_data = json.loads(
        (dist / "app_core" / "version.json").read_text(encoding="utf-8")
    )
    version = str(version_data.get("version") or "unknown")
    release_dir = args.release_dir.resolve()
    if args.patch or args.full_zip:
        release_dir.mkdir(parents=True, exist_ok=True)
    if args.patch:
        patch = release_dir / f"AtomicCalculationDB_Patch_v{version}.zip"
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "build_update.py"),
                "--core",
                str(dist / "app_core"),
                "--output",
                str(patch),
            ],
            cwd=ROOT,
            check=True,
        )
        print(f"Patch  : {patch}")
    if args.full_zip:
        from scripts.make_full_release import build_full_release

        full_zip = build_full_release(
            dist,
            output=release_dir / f"AtomicCalculationDB_Full_v{version}.zip",
        )
        print(f"Full   : {full_zip}")

    print(f"\nLauncher: {exe} ({_size_mb(exe):.2f} MB)")
    print(f"Runtime : {runtime_exe.parent} ({_size_mb(runtime_exe.parent):.0f} MB)")
    print(f"Release : {dist} ({_size_mb(dist):.0f} MB)")
    print(f"Packages: {release_dir}")
    print("The launcher forwards --mcp/--mcp-sse unchanged; app_core stays external.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
