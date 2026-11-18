"""Material 3 theme, labels and small HTML renderers for the V2 UI."""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

COLORS = {
    "surface": "#F8FAFD",
    "container": "#EFF3F8",
    "primary": "#0B57D0",
    "primary_container": "#D3E3FD",
    "on_surface": "#1B1B1F",
    "muted": "#5F6368",
}

CARD_TYPE_LABELS = {
    "STRAT": "计算策略",
    "MODEL": "物理模型",
    "CODE": "程序代码",
    "PITFALL": "排错踩坑",
    "CONC": "关键结论",
    "MASTER": "L1 总纲",
}

DOC_TYPE_LABELS = {"PAP": "论文", "BOK": "专著", "REV": "综述", "MAN": "手册"}

DOWNLOAD_STATUS_LABELS = {
    "DISCOVERED": "已发现",
    "QUEUED": "已排队",
    "DOWNLOADING": "下载中",
    "DOWNLOADED": "已下载",
    "MANUAL_REQUIRED": "待手动导入",
    "REJECTED": "已拒绝",
    "FAILED": "下载失败",
}
PARSE_STATUS_LABELS = {
    "PENDING": "待解析",
    "LIGHT_PARSED": "待提炼",
    "LLM_EXTRACTED": "已解析",
    "FAILED": "解析失败",
}

_EXTRA_QSS = """
QFrame#MasterBanner {
    background: #E8F0FE;
    border: 1px solid #C9DCF8;
    border-radius: 20px;
}
QLabel#MasterTitle { font-size: 16px; font-weight: 700; color: #0B57D0; }
QLabel#MasterBody { font-size: 12px; color: #3B4A61; }
QPushButton#FilterChip {
    background: #FFFFFF; border: 1px solid #DDE3EC; border-radius: 15px;
    padding: 5px 12px; font-size: 11px; color: #3C4043;
}
QPushButton#FilterChip:checked {
    background: #D3E3FD; border-color: #D3E3FD; color: #0B57D0; font-weight: 600;
}
QPushButton#IntentChip {
    background: #EFF3F8; border: none; border-radius: 12px;
    padding: 4px 10px; font-size: 10px; color: #3B4657;
}
QPushButton#ModelBadge {
    background-color: #F1F5F9;
    color: #475569;
    border: 1px solid #CBD5E1;
    border-radius: 12px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 500;
}
QPushButton#ModelBadge:hover {
    background-color: #E2E8F0;
    color: #1E293B;
    border-color: #94A3B8;
}
QFrame#Drawer {
    background: #FFFFFF;
    border-left: 1px solid #E3E8F0;
    border-top-left-radius: 20px;
    border-bottom-left-radius: 20px;
}
QLabel#DrawerTitle { font-size: 18px; font-weight: 700; color: #1B1B1F; }
QFrame#FormulaBox {
    background: #F7F9FC; border: 1px solid #E6EBF3; border-radius: 12px;
}
QTextBrowser#FormulaView {
    background: transparent; border: none;
    padding: 4px; color: #263238;
    font-family: "Cambria Math", "Consolas", monospace; font-size: 14px;
}
QTextBrowser#CodeView {
    background: #F7F9FC; border: 1px solid #E6EBF3; border-radius: 12px;
    padding: 8px; font-size: 11px;
}
QFrame#ConflictCard { background: #FFF4E5; border: 1px solid #F3D5A7; border-radius: 16px; }
QLabel#ConflictTitle { font-size: 13px; font-weight: 700; color: #8A4B00; }
QLabel#ReportView { background: #FFFFFF; border: 1px solid #EEF1F6; border-radius: 16px; padding: 12px; }
"""


def load_stylesheet() -> str:
    path = Path(__file__).with_name("theme") / "m3_theme.qss"
    assets = (Path(__file__).with_name("assets")).as_posix()
    extra = f"""
QCheckBox::indicator:checked {{
    image: url("{assets}/checkmark.svg");
}}
QSpinBox, QDoubleSpinBox {{
    padding-right: 30px;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    width: 24px;
    border: none;
    background: transparent;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-position: top right;
    border-top-right-radius: 12px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-position: bottom right;
    border-bottom-right-radius: 12px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background: #EFF3F8;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url("{assets}/chevron-up.svg");
    width: 10px;
    height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url("{assets}/chevron-down.svg");
    width: 10px;
    height: 10px;
}}
"""
    return path.read_text(encoding="utf-8") + "\n" + _EXTRA_QSS + "\n" + extra


def card_type_label(kind: str | None) -> str:
    return CARD_TYPE_LABELS.get(str(kind or "").upper(), str(kind or "知识"))


def doc_type_label(doc_type: str | None) -> str:
    return DOC_TYPE_LABELS.get(str(doc_type or "").upper(), str(doc_type or "文献"))


def download_label(status: str | None) -> str:
    return DOWNLOAD_STATUS_LABELS.get(str(status or "").upper(), str(status or "未知"))


def parse_label(status: str | None) -> str:
    return PARSE_STATUS_LABELS.get(str(status or "").upper(), str(status or "未知"))


def status_tone(status: str | None) -> str:
    value = str(status or "").upper()
    if value in ("DOWNLOADED", "LLM_EXTRACTED"):
        return "ok"
    if value in ("MANUAL_REQUIRED", "FAILED", "REJECTED"):
        return "warn"
    return "off"


def format_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value)).strftime("%m-%d %H:%M")
    except ValueError:
        return str(value)[:16]


def format_authors(value: object, limit: int = 3) -> str:
    if isinstance(value, list):
        authors = [str(item) for item in value if str(item).strip()]
    elif value:
        authors = [str(value)]
    else:
        authors = []
    if not authors:
        return ""
    if len(authors) > limit:
        return ", ".join(authors[:limit]) + f" 等 {len(authors)} 人"
    return ", ".join(authors)


def format_authors_short(authors_val: Any) -> str:
    import json
    if not authors_val:
        return "—"
    if isinstance(authors_val, str):
        try:
            parsed = json.loads(authors_val)
            if isinstance(parsed, list):
                authors_val = parsed
            else:
                return str(authors_val).strip() or "—"
        except Exception:
            return str(authors_val).strip() or "—"
    if isinstance(authors_val, list):
        names = [str(a).strip() for a in authors_val if str(a).strip()]
        if not names:
            return "—"
        if len(names) <= 2:
            return ", ".join(names)
        return f"{names[0]} 等"
    return str(authors_val).strip() or "—"


_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MD_LIST_RE = re.compile(r"^\s{0,3}[-*+]\s+", re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def _strip_markdown(text: str) -> str:
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_LIST_RE.sub("", text)
    text = _MD_BOLD_RE.sub(lambda match: match.group(1) or match.group(2), text)
    text = _MD_LINK_RE.sub(r"\1", text)
    return text.replace("```", " ").replace("`", "")


def highlight_snippet(text: str | None) -> str:
    if not text:
        return ""
    cleaned = _strip_markdown(str(text))
    escaped = html.escape(cleaned, quote=False)
    escaped = escaped.replace(
        "\x01", '<span style="background-color:#FFF1B8;color:#5C4700;">'
    ).replace("\x02", "</span>")
    return escaped.replace("\r", " ").replace("\n", " ").strip()


def plain_snippet(text: str | None) -> str:
    if not text:
        return ""
    cleaned = _strip_markdown(str(text))
    return cleaned.replace("\x01", "").replace("\x02", "").replace("\n", " ").strip()


def focus_html(text: str | None) -> str:
    """Escape evidence text and turn ⟪FOCUS⟫ markers into a highlight."""
    body = html.escape(str(text or ""), quote=False)
    body = body.replace(
        "⟪FOCUS⟫",
        '<span style="background-color:#FFF1B8;color:#5C4700;font-weight:600;">',
    ).replace("⟪/FOCUS⟫", "</span>")
    return body.replace("\n", "<br>")


def code_html(code: str | None) -> str:
    body = str(code or "")
    if not body.strip():
        return ""
    try:
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import BashLexer, PythonLexer, TextLexer

        lexer = BashLexer() if re.search(r"\b(rmcdhf|rci|rncsl|fac)\b", body) else TextLexer()
        return highlight(body, lexer, HtmlFormatter(noclasses=True, nowrap=False))
    except Exception:
        return f"<pre>{html.escape(body)}</pre>"


def formula_html(latex: str | None) -> str:
    body = str(latex or "").strip()
    if not body:
        return ""
    escaped = html.escape(body)
    return (
        '<div style="padding:10px;color:#263238;font-family:Cambria Math,Consolas,monospace;'
        'font-size:15px;white-space:pre-wrap;">'
        + escaped
        + "</div>"
    )


def render_formula_pixmap(latex: str | None, max_width: int = 430):
    """Optional typeset formula via matplotlib mathtext.

    Returns ``None`` when matplotlib is unavailable or the formula contains
    constructs mathtext cannot parse; callers then show the LaTeX source.
    """
    body = str(latex or "").strip()
    if not body:
        return None
    try:
        import io

        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import mathtext

        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPixmap

        cleaned = (
            body.replace("$$", "")
            .replace("\\boldsymbol", "\\mathbf")
            .replace("\\text{", "\\mathrm{")
            .strip()
        )
        if "\n\n" in cleaned:
            cleaned = cleaned.split("\n\n")[0]
        buffer = io.BytesIO()
        mathtext.math_to_image(
            "$" + cleaned + "$",
            buffer,
            dpi=220,
            format="png",
            color="#263238",
        )
        pixmap = QPixmap()
        if not pixmap.loadFromData(buffer.getvalue(), "PNG"):
            return None
        if pixmap.width() > max_width:
            pixmap = pixmap.scaledToWidth(max_width, Qt.SmoothTransformation)
        return pixmap
    except Exception:
        return None
