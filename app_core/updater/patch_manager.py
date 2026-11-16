"""GUI-facing hot-update facade.

The launcher owns the actual file swap; this module validates a user-selected
zip, stages it in ``workspace/pending_update.zip`` and can relaunch the shell.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from updater.updater import (
    UpdateError,
    UpdateInfo,
    apply_pending_update,
    clean_launch_env,
    current_version,
    inspect_package,
    pending_path,
    read_version,
    relaunch,
    restart_command,
    stage_package,
)


class PatchManager:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)

    def current_version(self) -> dict[str, Any]:
        return current_version(self.base_dir)

    def validate(self, package: str | Path) -> UpdateInfo:
        return inspect_package(package)

    def stage(self, package: str | Path) -> Path:
        return stage_package(self.base_dir, package)

    def pending(self) -> Path:
        return pending_path(self.base_dir)

    def apply_pending(self) -> UpdateInfo | None:
        return apply_pending_update(self.base_dir)

    def restart_command(self) -> list[str]:
        return restart_command(self.base_dir)

    def relaunch(self) -> None:
        """Start the shell detached (QProcess when Qt is loaded) and exit."""
        command = restart_command(self.base_dir)
        try:
            from PySide6.QtCore import QProcess, QProcessEnvironment

            environment = QProcessEnvironment.systemEnvironment()
            clean = clean_launch_env()
            for key in list(environment.keys()):
                if key not in clean:
                    environment.remove(key)
            process = QProcess()
            process.setProgram(command[0])
            process.setArguments(command[1:])
            process.setWorkingDirectory(str(self.base_dir))
            process.setProcessEnvironment(environment)
            started = process.startDetached()
            if isinstance(started, tuple):
                started = bool(started[0])
            if started:
                os._exit(0)
        except Exception:
            pass
        relaunch(self.base_dir)

    def status(self) -> dict[str, Any]:
        pending = self.pending()
        return {
            "current": read_version(self.base_dir / "app_core"),
            "pending": str(pending) if pending.exists() else "",
            "launcher": restart_command(self.base_dir),
        }


__all__ = [
    "PatchManager",
    "UpdateError",
    "UpdateInfo",
    "apply_pending_update",
    "inspect_package",
    "pending_path",
    "stage_package",
]
