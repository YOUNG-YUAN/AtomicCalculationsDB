"""Small PDF page/bbox viewer used by L3 evidence locators."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
)

try:
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover
    try:
        import fitz  # type: ignore
    except ImportError:  # pragma: no cover
        fitz = None


def locator_has_bbox(locator: Mapping[str, Any] | None) -> bool:
    return bool(locator and locator.get("bbox"))


class PdfLocatorDialog(QDialog):
    """Render one PDF page and draw the evidence bbox."""

    def __init__(
        self,
        pdf_path: str | Path,
        locator: Mapping[str, Any],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("原文定位")
        self.resize(980, 820)
        self.setMinimumSize(620, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)

        page_num = locator.get("page_num")
        block_id = str(locator.get("block_id") or "")
        info = QLabel(
            f"第 {page_num if page_num is not None else '—'} 页"
            + (f" · {block_id}" if block_id else "")
        )
        info.setObjectName("Muted")
        layout.addWidget(info)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        image_label = QLabel()
        image_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll.setWidget(image_label)
        layout.addWidget(scroll, 1)

        close = QPushButton("关闭")
        close.setObjectName("Pill")
        close.clicked.connect(self.accept)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(close)
        layout.addLayout(footer)

        pixmap = self._render(Path(pdf_path), locator)
        if pixmap is None:
            image_label.setText("无法渲染该页面，或 PDF 不存在。")
        else:
            image_label.setPixmap(pixmap)

    @staticmethod
    def _render(path: Path, locator: Mapping[str, Any]) -> QPixmap | None:
        if fitz is None or not path.is_file():
            return None
        try:
            with fitz.open(path) as document:
                page_index = int(locator.get("page_num") or 1) - 1
                if page_index < 0 or page_index >= document.page_count:
                    return None
                page = document[page_index]
                scale = 2.0
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                image = QImage(
                    pix.samples,
                    pix.width,
                    pix.height,
                    pix.stride,
                    QImage.Format_RGB888,
                ).copy()
                bbox = locator.get("bbox")
                if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                    try:
                        x0, y0, x1, y1 = (float(value) for value in bbox)
                        painter = QPainter(image)
                        painter.setPen(QPen(QColor("#D32F2F"), 18))
                        painter.setBrush(QColor(211, 47, 47, 35))
                        painter.drawRect(
                            QRectF(
                                x0 * scale,
                                y0 * scale,
                                max(1.0, (x1 - x0) * scale),
                                max(1.0, (y1 - y0) * scale),
                            )
                        )
                        painter.end()
                    except (TypeError, ValueError):
                        pass
                return QPixmap.fromImage(image)
        except Exception:
            return None


__all__ = ["PdfLocatorDialog", "locator_has_bbox"]
