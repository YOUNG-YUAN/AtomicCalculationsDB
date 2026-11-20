"""AtomicCalculationDB shell launcher.

Responsibilities, in order:
1. resolve the release root (``BASE_DIR``);
2. apply ``workspace/pending_update.zip`` to the hot-updatable ``app_core``;
3. put ``app_core`` on ``sys.path`` and start the Qt application.

This file is the only part that is bundled by PyInstaller; business code is
never frozen and can be replaced with a small zip package.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

APP_NAME = "AtomicCalculationDB"


def _ensure_mcp_stdio() -> None:
    """Restore stdin/stdout for a windowed PyInstaller build.

    MCP clients launch the exe with pipes, but PyInstaller's windowed
    bootloader normally replaces stdio with ``None``.  Re-open the inherited
    file descriptors; when launched from a console, attach to it as a fallback.
    """
    if not any(arg in ("--mcp", "--mcp-sse", "agent") for arg in sys.argv[1:]):
        return
    if sys.stdin is not None and sys.stdout is not None:
        return
    for fd, mode, name in ((0, "r", "stdin"), (1, "w", "stdout"), (2, "w", "stderr")):
        if getattr(sys, name) is not None:
            continue
        try:
            stream = os.fdopen(
                fd,
                mode,
                encoding="utf-8" if "b" not in mode else None,
                errors="replace",
                closefd=False,
                buffering=1 if mode == "w" else -1,
            )
            setattr(sys, name, stream)
        except OSError:
            pass
    if sys.stdin is not None and sys.stdout is not None:
        return
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.AttachConsole(-1)
            if sys.stdin is None:
                sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
            if sys.stdout is None:
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            if sys.stderr is None:
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        except Exception:
            pass


def get_base_dir() -> Path:
    # The tiny native shell lives at the release root and launches the frozen
    # runtime from ``runtime/``; ACDB_RELEASE_ROOT points back to the root so
    # app_core/workspace stay external.
    release_root = os.environ.get("ACDB_RELEASE_ROOT", "").strip()
    if release_root:
        return Path(release_root).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def ensure_workspace(base: Path) -> None:
    for relative in (
        "workspace",
        "workspace/logs",
        "workspace/storage",
        "workspace/inbox_manual",
        "workspace/vector_db",
        "workspace/exports",
    ):
        (base / relative).mkdir(parents=True, exist_ok=True)


def setup_launcher_logging(base: Path) -> None:
    log_file = base / "workspace" / "logs" / "launcher.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def show_fatal_error(message: str) -> None:
    logging.getLogger("launcher").error(message)
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, message, f"{APP_NAME} 启动失败", 0x10
            )
    except Exception:
        pass


def main() -> int:
    _ensure_mcp_stdio()
    base = get_base_dir()
    ensure_workspace(base)
    setup_launcher_logging(base)
    log = logging.getLogger("launcher")

    core = base / "app_core"
    if not core.exists():
        show_fatal_error(f"找不到 app_core 目录：{core}\n请完整解压发布包。")
        return 2

    sys.path.insert(0, str(core))

    # ------------------------------------------------------------------ update
    try:
        from updater.updater import UpdateError, apply_pending_update

        try:
            applied = apply_pending_update(base)
        except UpdateError as exc:
            # Move the bad package aside so every subsequent launch does not
            # retry the same failure.  The user can re-import it after fixing.
            pending = base / "workspace" / "pending_update.zip"
            failed_note = ""
            if pending.exists():
                failed = base / "workspace" / (
                    f"failed_update_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
                )
                try:
                    os.replace(pending, failed)
                    failed_note = f"\n更新包已移至：{failed.name}"
                except OSError:
                    failed_note = "\n无法移动失败的更新包，将在下次启动时重试。"
            error_file = base / "workspace" / "last_update_error.txt"
            error_file.write_text(str(exc) + failed_note, encoding="utf-8")
            log.error("Hot update failed, continuing with current core: %s", exc)
        else:
            if applied is not None:
                log.info(
                    "Applied hot update %s (build %s, %s files)",
                    applied.version,
                    applied.build,
                    applied.entries,
                )
                (base / "workspace" / "last_update_error.txt").unlink(missing_ok=True)
    except Exception:
        # Updater problems must never prevent the app from starting.
        log.exception("Updater module error; continuing with current core")

    # ------------------------------------------------------------------- start
    try:
        from main_entry import start_gui_app

        return int(start_gui_app(str(base)) or 0)
    except Exception:
        detail = traceback.format_exc()
        log.critical("Application crashed:\n%s", detail)
        show_fatal_error(
            "程序启动失败，详细信息已写入：\n"
            f"{base / 'workspace' / 'logs' / 'launcher.log'}\n\n"
            + detail[-1200:]
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
