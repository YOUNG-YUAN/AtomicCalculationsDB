"""High-DPI transparent formula rendering for atomic-physics LaTeX.

Matplotlib mathtext understands a useful subset of LaTeX but not the physics
macros (``\\bra``/``\\ket``), ``aligned`` environments or ``\\text``.  This
module pre-expands those constructs, renders each aligned row separately and
stacks them into one transparent QPixmap.  A copy button keeps the original
LaTeX source one click away.
"""

from __future__ import annotations

import io
import logging
import re
from functools import lru_cache

import matplotlib

# Formula rendering is strictly off-screen.  Never import pyplot's GUI
# backends in the Qt application process.
matplotlib.use("Agg", force=True)
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

log = logging.getLogger(__name__)

_BRACED = r"\{([^{}]*)\}"
_MACRO_PATTERNS = (
    (re.compile(r"\\braket" + _BRACED + r"\s*" + _BRACED), r"\\langle \1 | \2 \\rangle"),
    (re.compile(r"\\ketbra\{([^{}]*)\}\{([^{}]*)\}"), r"|\1\\rangle\\langle \2|"),
    (re.compile(r"\\bra" + _BRACED), r"\\langle \1 |"),
    (re.compile(r"\\ket" + _BRACED), r"| \1 \\rangle"),
    (re.compile(r"\\text" + _BRACED), r"\\mathrm{\1}"),
    (re.compile(r"\\operatorname" + _BRACED), r"\\mathrm{\1}"),
    (re.compile(r"\\bm" + _BRACED), r"\\mathbf{\1}"),
    (re.compile(r"\\boldsymbol" + _BRACED), r"\\mathbf{\1}"),
    (re.compile(r"\\label\{[^{}]*\}"), ""),
    (re.compile(r"\\tag\{[^{}]*\}"), ""),
)


def strip_math_delimiters(latex: str) -> str:
    body = str(latex or "").strip()
    body = re.sub(r"^\s*\$\$(.*?)\$\$\s*$", r"\1", body, flags=re.S)
    body = re.sub(r"^\s*\$(.*?)\$\s*$", r"\1", body, flags=re.S)
    for left, right in ((r"\[", r"\]"), (r"\(", r"\)")):
        if body.startswith(left) and body.endswith(right):
            body = body[len(left) : -len(right)].strip()
    return body


def expand_physics_macros(latex: str) -> str:
    """Expand common physics macros into mathtext-compatible LaTeX."""
    body = strip_math_delimiters(latex)
    for pattern, replacement in _MACRO_PATTERNS:
        body = pattern.sub(replacement, body)
    # mathtext rejects \text-style spaces inside \mathrm; escape them.
    def _mathrm(match: re.Match[str]) -> str:
        content = match.group(1)
        return r"\mathrm{" + content.replace(" ", r"\ ") + "}"

    body = re.sub(r"\\mathrm\{([^{}]*)\}", _mathrm, body)
    return body.strip()


def split_aligned(latex: str) -> list[str]:
    """Return display rows for aligned/array/split/cases environments."""
    body = strip_math_delimiters(latex)
    env = re.search(
        r"\\begin\{(aligned|align|array|split|cases)\}(.*?)\\end\{\1\}",
        body,
        flags=re.S,
    )
    if env:
        inner = env.group(2)
        # array has a column spec as its first argument.
        if env.group(1) == "array":
            inner = re.sub(r"^\s*\{[^{}]*\}", "", inner, count=1)
        rows = [row.strip() for row in inner.split("\\\\") if row.strip()]
        if rows:
            return [expand_physics_macros(row) for row in rows]
    text = expand_physics_macros(body).replace("\\\\", "\n")
    lines = [line.strip() for line in text.split("\n")]
    return [line for line in lines if line]


def _render_line_png(line: str, *, fontsize: float = 15.0, dpi: int = 300) -> bytes | None:
    try:
        figure = Figure(figsize=(0.01, 0.01), dpi=dpi)
        figure.patch.set_alpha(0.0)
        text = figure.text(
            0,
            0,
            "$" + line + "$",
            fontsize=fontsize,
            color="#263238",
            math_fontfamily="dejavusans",
        )
        canvas = FigureCanvasAgg(figure)
        canvas.draw()
        bbox = text.get_window_extent(renderer=canvas.get_renderer())
        # Recreate with a bounding box just large enough for the formula.
        width = max(0.2, bbox.width / dpi)
        height = max(0.2, bbox.height / dpi)
        figure = Figure(figsize=(width, height), dpi=dpi)
        figure.patch.set_alpha(0.0)
        figure.text(
            0.01,
            0.1,
            "$" + line + "$",
            fontsize=fontsize,
            color="#263238",
            math_fontfamily="dejavusans",
        )
        canvas = FigureCanvasAgg(figure)
        buffer = io.BytesIO()
        figure.savefig(
            buffer,
            format="png",
            dpi=dpi,
            transparent=True,
            bbox_inches="tight",
            pad_inches=0.03,
        )
        return buffer.getvalue()
    except Exception:
        log.debug("Formula line render failed: %s", line, exc_info=True)
        return None


@lru_cache(maxsize=128)
def render_formula_png(latex: str, *, fontsize: int = 15) -> bytes | None:
    """Render (possibly aligned) LaTeX to transparent PNG bytes."""
    lines = split_aligned(latex)
    if not lines:
        return None
    images: list[bytes] = []
    for line in lines:
        image = _render_line_png(line, fontsize=float(fontsize))
        if image is None:
            return None
        images.append(image)
    if len(images) == 1:
        return images[0]
    # Stack rows with QPainter so the result stays transparent.
    from PySide6.QtGui import QImage, QPainter

    pixmaps = []
    width = 0
    height = 0
    gap = 10
    for data in images:
        item = QPixmap()
        if not item.loadFromData(data, "PNG"):
            return None
        pixmaps.append(item)
        width = max(width, item.width())
        height += item.height() + gap
    height = max(1, height - gap)
    canvas = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    y = 0
    for item in pixmaps:
        painter.drawPixmap(0, y, item)
        y += item.height() + gap
    painter.end()
    buffer = io.BytesIO()
    from PySide6.QtCore import QBuffer, QIODevice

    data = QBuffer()
    data.open(QIODevice.WriteOnly)
    canvas.save(data, "PNG")
    data.close()
    return bytes(data.data())


def render_formula_pixmap(latex: str, max_width: int = 430, *, fontsize: int = 15):
    """Return a QPixmap scaled to ``max_width``, or ``None`` when unrenderable."""
    data = render_formula_png(str(latex or "").strip(), fontsize=fontsize)
    if not data:
        return None
    pixmap = QPixmap()
    if not pixmap.loadFromData(data, "PNG"):
        return None
    if pixmap.width() > max_width:
        pixmap = pixmap.scaledToWidth(max_width, Qt.SmoothTransformation)
    return pixmap


class FormulaWidget(QFrame):
    """Rendered formula plus a one-click LaTeX source copy interaction."""

    def __init__(
        self,
        latex: str,
        max_width: int = 430,
        *,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.latex = str(latex or "").strip()
        self.setObjectName("FormulaBox")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 8)
        outer.setSpacing(6)
        pixmap = render_formula_pixmap(self.latex, max_width=max_width)
        if pixmap is not None:
            image = QLabel()
            image.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            image.setPixmap(pixmap)
            image.setStyleSheet("background: transparent;")
            outer.addWidget(image)
        else:
            source = QTextBrowser()
            source.setObjectName("FormulaView")
            source.setPlainText(self.latex)
            source.setMaximumHeight(170)
            outer.addWidget(source)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("mathtext · 透明底")
        hint.setObjectName("Tiny")
        footer.addWidget(hint)
        footer.addStretch(1)
        copy = QPushButton("复制 LaTeX")
        copy.setObjectName("Ghost")
        copy.setCursor(Qt.PointingHandCursor)
        copy.clicked.connect(self._copy)
        footer.addWidget(copy)
        outer.addLayout(footer)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.latex)


__all__ = [
    "FormulaWidget",
    "expand_physics_macros",
    "render_formula_pixmap",
    "render_formula_png",
    "split_aligned",
    "strip_math_delimiters",
]
