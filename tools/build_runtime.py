"""Build the portable Python runtime used by the tiny native launcher.

The runtime is a Python embeddable distribution plus the project's third-party
requirements installed into ``runtime/Lib/site-packages``.  It is intentionally
external to ``AtomicCalculationDB.exe`` so the launcher stays under ~100 KiB.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import sysconfig
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
PYTHON_VERSION = "3.11.9"
EMBED_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
    f"python-{PYTHON_VERSION}-embed-amd64.zip"
)
EMBED_NAME = f"python-{PYTHON_VERSION}-embed-amd64.zip"
MARKER = ".acdb_runtime.json"
PTH_NAME = "python311._pth"


def _requirements_hash() -> str:
    digest = hashlib.sha256()
    digest.update(REQUIREMENTS.read_bytes())
    digest.update(PYTHON_VERSION.encode("ascii"))
    return digest.hexdigest()


def _download_embed(cache: Path) -> Path:
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists() and zipfile.is_zipfile(cache):
        return cache
    temp = cache.with_suffix(".part")
    print(f"Downloading {EMBED_URL}")
    with urllib.request.urlopen(EMBED_URL, timeout=120) as response, open(temp, "wb") as handle:
        while True:
            block = response.read(1024 * 256)
            if not block:
                break
            handle.write(block)
    temp.replace(cache)
    return cache


def _extract_embed(archive: Path, runtime: Path) -> None:
    with zipfile.ZipFile(archive) as package:
        for info in package.infolist():
            target = (runtime / info.filename).resolve()
            if runtime.resolve() not in target.parents and target != runtime.resolve():
                raise RuntimeError(f"unsafe embed path: {info.filename}")
        package.extractall(runtime)


def _write_pth(runtime: Path) -> None:
    # Python embeddable disables site by default.  Add site-packages and let
    # site process package .pth files (PySide6/matplotlib rely on this).
    (runtime / PTH_NAME).write_text(
        "python311.zip\n.\nLib\nLib\\site-packages\nimport site\n",
        encoding="utf-8",
    )
    (runtime / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)


def _pip_install(runtime: Path) -> None:
    site_packages = runtime / "Lib" / "site-packages"
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--target",
        str(site_packages),
        "--ignore-installed",
        "--only-binary=:all:",
        "--no-warn-script-location",
        "--upgrade",
        "-r",
        str(REQUIREMENTS),
    ]
    print("Installing runtime dependencies into", site_packages)
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode != 0:
        raise RuntimeError("pip install for portable runtime failed")


def _copy_spacy_model(runtime: Path) -> None:
    source_site = Path(sysconfig.get_paths().get("purelib", ""))
    target_site = runtime / "Lib" / "site-packages"
    if not source_site.is_dir():
        return
    copied = False
    for pattern in ("en_core_web_sm", "en_core_web_sm-*.dist-info"):
        for source in source_site.glob(pattern):
            if not source.exists():
                continue
            destination = target_site / source.name
            if destination.exists():
                shutil.rmtree(destination)
            if source.is_dir():
                shutil.copytree(source, destination)
            copied = True
    if not copied:
        print("warning: en_core_web_sm not found in the build environment; spaCy will degrade")


def _copy_runtime_dlls(runtime: Path) -> None:
    python_root = Path(sys.executable).resolve().parent
    for name in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"):
        source = python_root / name
        if source.exists() and not (runtime / name).exists():
            shutil.copy2(source, runtime / name)


def _smoke_test(runtime: Path) -> None:
    python = runtime / "python.exe"
    if not python.exists():
        raise RuntimeError(f"portable Python missing: {python}")
    code = (
        "import PySide6, PySide6.QtSvg, qdrant_client, onnxruntime, tokenizers, "
        "spacy, matplotlib, fitz, mcp, openai, webdav4, boto3, numpy, httpx; "
        "import spacy; spacy.load('en_core_web_sm'); print('runtime-ok')"
    )
    completed = subprocess.run(
        [str(python), "-c", code],
        cwd=str(runtime),
        text=True,
        capture_output=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "portable runtime smoke test failed:\n"
            + (completed.stderr or completed.stdout)[-4000:]
        )
    print(completed.stdout.strip())


def build_runtime(dist: Path, *, force: bool = False) -> Path:
    runtime = dist / "runtime"
    marker = runtime / MARKER
    expected = _requirements_hash()
    if not force and marker.exists() and (runtime / "python.exe").exists():
        try:
            current = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:
            current = {}
        if current.get("requirements_hash") == expected:
            print("Portable runtime is up to date:", runtime)
            return runtime

    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True)
    archive = ROOT / "build" / "cache" / EMBED_NAME
    _extract_embed(_download_embed(archive), runtime)
    _write_pth(runtime)
    _pip_install(runtime)
    _copy_spacy_model(runtime)
    _copy_runtime_dlls(runtime)
    _smoke_test(runtime)

    marker.write_text(
        json.dumps(
            {
                "python": PYTHON_VERSION,
                "requirements_hash": expected,
                "built_from": str(REQUIREMENTS),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("Portable runtime ready:", runtime)
    return runtime


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build the AtomicCalculationDB portable runtime")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    build_runtime(args.dist.resolve(), force=args.force)
