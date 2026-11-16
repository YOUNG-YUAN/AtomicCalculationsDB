"""Shell/core hot update mechanics.

The launcher (shell) owns a PyInstaller runtime; all business code lives in the
plain ``app_core`` folder.  A signed-off ``.zip`` dropped into
``workspace/pending_update.zip`` is validated, extracted to a staging folder and
swapped in before Qt is even imported.  A failed swap is rolled back to
``app_core_bak``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

log = logging.getLogger(__name__)

VERSION_FILE = "version.json"
PENDING_NAME = "pending_update.zip"
MAX_ENTRIES = 5000
MAX_UNCOMPRESSED_BYTES = 400 * 1024 * 1024

# PyInstaller onefile propagates these to child processes so they can reuse the
# parent's extraction directory.  A relaunched *application* must NOT reuse it:
# the old bootloader would delete the directory while the new GUI is importing
# numpy/Qt from it, producing "Failed to remove temporary directory" and
# partially-initialized module errors.  Strip them before every relaunch.
_PYI_ENV_BLACKLIST = (
    "_PYI_APPLICATION_HOME_DIR",
    "_PYI_PARENT_PROCESS_LEVEL",
    "_PYI_ARCHIVE_FILE",
    "_PYI_SPLASH_IPC",
    "_MEIPASS2",
    "ACDB_AUTO_HOT_RESTART",
)


def clean_launch_env(environ: Any = None) -> dict[str, str]:
    """Return a copy of the environment safe for a fresh frozen launch."""
    source = os.environ if environ is None else environ
    env = dict(source)
    for key in _PYI_ENV_BLACKLIST:
        env.pop(key, None)
    return env


class UpdateError(RuntimeError):
    pass


@dataclass
class UpdateInfo:
    version: str
    build: str = ""
    entries: int = 0
    uncompressed_bytes: int = 0
    package: str = ""
    prefix: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_version(root: str | Path) -> dict[str, Any]:
    path = Path(root) / VERSION_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def current_version(base_dir: str | Path) -> dict[str, Any]:
    return read_version(Path(base_dir) / "app_core")


def _iter_safe_entries(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = zf.infolist()
    if len(entries) > MAX_ENTRIES:
        raise UpdateError(f"更新包文件数过多（{len(entries)} > {MAX_ENTRIES}）")
    total = 0
    safe: list[zipfile.ZipInfo] = []
    seen: set[str] = set()
    for info in entries:
        name = info.filename.replace("\\", "/")
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts:
            raise UpdateError(f"更新包含非法路径: {info.filename}")
        if info.is_dir():
            continue
        # Normalise separators so "a/b" and "a\\b" cannot overwrite each other.
        normalized = pure.as_posix()
        if normalized in seen:
            raise UpdateError(f"更新包包含重复路径: {info.filename}")
        seen.add(normalized)
        # Reject symlinks (Unix mode stored in the high bits of external_attr).
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise UpdateError(f"更新包包含符号链接: {info.filename}")
        total += int(info.file_size)
        if total > MAX_UNCOMPRESSED_BYTES:
            raise UpdateError("更新包解压后体积超过 400 MB 限制")
        safe.append(info)
    if not safe:
        raise UpdateError("更新包为空")
    return safe


def inspect_package(package: str | Path) -> UpdateInfo:
    path = Path(package)
    if not path.is_file():
        raise UpdateError(f"更新包不存在: {path}")
    if not zipfile.is_zipfile(path):
        raise UpdateError("不是有效的 .zip 更新包")
    with zipfile.ZipFile(path) as zf:
        entries = _iter_safe_entries(zf)
        names = {info.filename.replace("\\", "/") for info in entries}
        prefix = ""
        version_name = VERSION_FILE
        if VERSION_FILE not in names:
            if f"app_core/{VERSION_FILE}" in names:
                prefix = "app_core/"
                version_name = f"app_core/{VERSION_FILE}"
            else:
                raise UpdateError(
                    f"更新包根目录缺少 {VERSION_FILE}（或 app_core/{VERSION_FILE}）"
                )
        try:
            version = json.loads(zf.read(version_name).decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            raise UpdateError(f"{version_name} 解析失败: {exc}") from exc
        if not isinstance(version, dict) or not str(version.get("version") or "").strip():
            raise UpdateError(f"{version_name} 必须包含 version 字段")
        total = sum(int(info.file_size) for info in entries)
        return UpdateInfo(
            version=str(version.get("version")),
            build=str(version.get("build") or ""),
            entries=len(entries),
            uncompressed_bytes=total,
            package=str(path),
            prefix=prefix,
        )


def pending_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "workspace" / PENDING_NAME


def stage_package(base_dir: str | Path, package: str | Path) -> Path:
    """Validate a user-selected package and copy it to the pending slot."""
    source = Path(package)
    inspect_package(source)
    target = pending_path(base_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".zip.tmp")
    shutil.copy2(source, tmp)
    os.replace(tmp, target)
    log.info("Staged update package: %s", target)
    return target


def apply_pending_update(base_dir: str | Path) -> UpdateInfo | None:
    """Apply ``workspace/pending_update.zip`` if present.  Returns update info.

    The function is intentionally defensive: the existing ``app_core`` is never
    removed until a complete staging tree has been validated.
    """
    base = Path(base_dir)
    package = pending_path(base)
    if not package.exists():
        return None

    staging = base / ".update_staging"
    core = base / "app_core"
    backup = base / "app_core_bak"

    try:
        info = inspect_package(package)
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        with zipfile.ZipFile(package) as zf:
            safe_names = {
                entry.filename.replace("\\", "/")
                for entry in _iter_safe_entries(zf)
            }
            for entry in zf.infolist():
                normalized = entry.filename.replace("\\", "/")
                if entry.is_dir() or normalized not in safe_names:
                    continue
                if info.prefix:
                    if not normalized.startswith(info.prefix):
                        continue
                    normalized = normalized[len(info.prefix) :]
                if not normalized:
                    continue
                target = staging / PurePosixPath(normalized)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

        # Validate the extracted tree, not just the zip listing.
        new_version = read_version(staging)
        if not new_version.get("version"):
            raise UpdateError("解压后的更新包缺少有效 version.json")
        if not (staging / "main_entry.py").exists():
            raise UpdateError("解压后的更新包缺少 main_entry.py")

        if core.exists():
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(core, backup)
        os.replace(staging, core)
    except Exception as exc:
        log.exception("Update failed: %s", exc)
        # Restore from backup only if the live core is missing.
        try:
            if not core.exists() and backup.exists():
                os.replace(backup, core)
        except Exception:
            log.exception("Rollback failed; app_core_bak left in place")
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(str(exc)) from exc

    try:
        package.unlink()
    except OSError:
        log.warning("Could not remove pending package %s", package)

    result = UpdateInfo(
        version=info.version,
        build=info.build,
        entries=info.entries,
        uncompressed_bytes=info.uncompressed_bytes,
        package=str(package),
        prefix=info.prefix,
    )
    log.info("Update applied: version %s (backup: %s)", result.version, backup)
    return result


def restart_command(base_dir: str | Path) -> list[str]:
    """Build the command that relaunches the shell in dev or frozen mode."""
    base = Path(base_dir)
    shell = os.environ.get("ACDB_SHELL_PATH", "").strip()
    if shell and Path(shell).is_file():
        return [shell]
    if getattr(sys, "frozen", False):
        return [sys.executable]
    launcher = Path(__file__).resolve().parents[2] / "launcher.py"
    return [sys.executable, str(launcher)]


def relaunch(base_dir: str | Path) -> None:
    """Spawn a fresh process and exit the current one."""
    command = restart_command(base_dir)
    log.info("Relaunching: %s", command)
    subprocess.Popen(
        command,
        cwd=str(base_dir),
        close_fds=True,
        env=clean_launch_env(),
    )
    os._exit(0)
