"""Modern asynchronous progress modal dialog for database ingestion and long-running tasks.

Ensures that writing to database, indexing vectors, and parsing never freezes the GUI.
Displays an animated, smooth progress bar with real-time status messages.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)


class TaskWorker(QThread):
    """Background worker thread to run a callable and emit progress signals."""

    progress = Signal(int, int, str)  # current, total, message
    finished_result = Signal(object)
    failed = Signal(str)

    def __init__(self, task_func: Callable[..., Any], *args: Any, **kwargs: Any):
        super().__init__()
        self.task_func = task_func
        self.args = args
        self.kwargs = kwargs

    def run(self) -> None:
        try:
            def cb(curr: int, tot: int, msg: str = "") -> None:
                self.progress.emit(curr, tot, msg)

            # Try passing progress_callback first
            try:
                res = self.task_func(*self.args, progress_callback=cb, **self.kwargs)
            except TypeError as te:
                if "unexpected keyword argument 'progress_callback'" in str(te):
                    res = self.task_func(*self.args, **self.kwargs)
                else:
                    raise
            self.finished_result.emit(res)
        except Exception as exc:
            log.exception("TaskWorker execution failed")
            self.failed.emit(str(exc))


class AsyncProgressDialog(QDialog):
    """Modern modal progress dialog with animated progress bar and live status."""

    def __init__(
        self,
        title: str = "💾 正在写入数据库",
        message: str = "正在写入知识库与向量索引，请稍候...",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        self.setWindowModality(Qt.WindowModal)
        self.resize(480, 160)
        self.setFixedSize(480, 160)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        # Header with icon and title
        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        icon_lbl = QLabel("💾")
        icon_lbl.setStyleSheet("font-size: 26px;")
        header_row.addWidget(icon_lbl)

        self.title_label = QLabel(message)
        self.title_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #0f172a;")
        self.title_label.setWordWrap(True)
        header_row.addWidget(self.title_label, 1)
        layout.addLayout(header_row)

        # Modern animated progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # Animated marquee by default
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setStyleSheet(
            "QProgressBar {"
            " background: #e2e8f0; border-radius: 4px; border: none;"
            "}"
            "QProgressBar::chunk {"
            " background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0284c7, stop:1 #38bdf8);"
            " border-radius: 4px;"
            "}"
        )
        layout.addWidget(self.progress_bar)

        # Detail text
        self.detail_label = QLabel("正在生成物理知识卡片并同步更新向量索引，请稍候...")
        self.detail_label.setStyleSheet("font-size: 11px; color: #64748b;")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

    def update_progress(self, current: int, total: int, msg: str) -> None:
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
            self.title_label.setText(f"正在写入数据库... ({current}/{total})")
        else:
            self.progress_bar.setRange(0, 0)
        if msg:
            self.detail_label.setText(msg)


def run_task_with_progress(
    parent: QWidget,
    task_func: Callable[..., Any],
    *args: Any,
    title: str = "💾 正在写入数据库",
    message: str = "正在写入知识库与向量索引，请稍候...",
    **kwargs: Any,
) -> Any:
    """Run a long task in a background QThread with an interactive modal progress dialog.

    Returns the task result, or raises the caught exception on error.
    """
    dialog = AsyncProgressDialog(title=title, message=message, parent=parent)
    worker = TaskWorker(task_func, *args, **kwargs)

    result_box: dict[str, Any] = {"result": None, "error": None}

    def on_progress(curr: int, tot: int, msg: str) -> None:
        dialog.update_progress(curr, tot, msg)

    def on_finished(res: Any) -> None:
        result_box["result"] = res
        dialog.accept()

    def on_failed(err_msg: str) -> None:
        result_box["error"] = err_msg
        dialog.reject()

    worker.progress.connect(on_progress)
    worker.finished_result.connect(on_finished)
    worker.failed.connect(on_failed)

    worker.start()
    dialog.exec()
    worker.wait()

    if result_box["error"]:
        raise RuntimeError(result_box["error"])
    return result_box["result"]
