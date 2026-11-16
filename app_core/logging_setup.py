"""Shared logging configuration for the launcher and the application core."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_configured = False


def setup_logging(base_dir: str | Path, level: int = logging.INFO) -> Path:
    """Configure console + rotating file logging once.

    Returns the log file path (``workspace/logs/app.log``).
    """
    global _configured
    base = Path(base_dir)
    log_dir = base / "workspace" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    if not _configured:
        root = logging.getLogger()
        root.setLevel(level)
        formatter = logging.Formatter(_LOG_FORMAT)

        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

        if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
            console = logging.StreamHandler()
            console.setFormatter(formatter)
            root.addHandler(console)

        _configured = True

    logging.getLogger(__name__).debug("Logging ready: %s", log_file)
    return log_file
