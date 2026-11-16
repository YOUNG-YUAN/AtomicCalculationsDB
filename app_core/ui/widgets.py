"""Reusable Material 3 widgets for the V2 UI."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui.atomic_markdown import render_atomic_markdown
from ui.formula_renderer import FormulaWidget
from ui.icon_assets import workbench_icon
from ui.m3_theme import (
    card_type_label,
    code_html,
    doc_type_label,
    download_label,
    focus_html,
    highlight_snippet,
    parse_label,
    plain_snippet,
    status_tone,
)
from ui.flow_layout import FlowLayout, clear_layout


def add_shadow(widget: QWidget, blur: int = 26, dy: int = 6, alpha: int = 18) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    effect.setColor(QColor(23, 43, 77, alpha))
    widget.setGraphicsEffect(effect)


def repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def make_chip(text: str, object_name: str = "Chip") -> QLabel:
    label = QLabel(text)
    label.setObjectName(object_name)
    label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    return label


def make_type_chip(text: str, *, knw: str | None = None, kind: str | None = None) -> QLabel:
    label = make_chip(text, "TypeChip")
    if knw:
        label.setProperty("knw", knw)
    if kind:
        label.setProperty("kind", kind)
    return label


def make_status_pill(status: str, *, parse: bool = False) -> QLabel:
    tone = status_tone(status)
    object_name = {"ok": "StatusOk", "warn": "StatusWarn"}.get(tone, "StatusOff")
    text = parse_label(status) if parse else download_label(status)
    return make_chip(text, object_name)


def separator() -> QFrame:
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet("background-color: #EFF1F6; border: none;")
    return line


def make_formula_view(latex: str, max_width: int = 430) -> QWidget:
    return FormulaWidget(latex, max_width=max_width)


# ------------------------------------------------------------------ navigation
class NavButton(QFrame):
    clicked = Signal(int)

    def __init__(
        self,
        emoji: str,
        text: str,
        index: int,
        parent: QWidget | None = None,
        *,
        icon: QIcon | None = None,
    ):
        super().__init__(parent)
        self.index = index
        self.setObjectName("NavButton")
        self.setAttribute(Qt.WA_Hover, True)
        self.setFixedSize(72, 62)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 7, 0, 5)
        layout.setSpacing(1)
        layout.setAlignment(Qt.AlignCenter)
        self.emoji = QLabel(emoji)
        self.emoji.setObjectName("NavEmoji")
        self.emoji.setAlignment(Qt.AlignCenter)
        if icon is not None and not icon.isNull():
            self.emoji.setPixmap(icon.pixmap(QSize(24, 24)))
            self.emoji.setText("")
        self.label = QLabel(text)
        self.label.setObjectName("NavText")
        self.label.setAlignment(Qt.AlignCenter)
        self.emoji.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.emoji)
        layout.addWidget(self.label)
        self.set_selected(False)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        repolish(self)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.index)
        super().mouseReleaseEvent(event)


class NavRail(QFrame):
    nav_changed = Signal(int)
    ITEMS = [
        ("⌂", "探索中心"),
        ("", "工作台"),
        ("▤", "文献库"),
        ("☁", "同步/MCP"),
        ("⚙", "设置"),
    ]

    def __init__(self, version: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("NavRail")
        self.setFixedWidth(100)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(8)
        self.buttons: list[NavButton] = []
        for index, (emoji, text) in enumerate(self.ITEMS):
            icon = workbench_icon(26) if index == 1 else None
            button = NavButton(emoji, text, index, icon=icon)
            button.clicked.connect(self._on_clicked)
            layout.addWidget(button, 0, Qt.AlignHCenter)
            self.buttons.append(button)
        layout.addStretch(1)
        version_label = QLabel(version or "v2.3.10")
        version_label.setObjectName("NavVersion")
        version_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(version_label)
        self.set_current(0)

    def _on_clicked(self, index: int) -> None:
        self.set_current(index)
        self.nav_changed.emit(index)

    def set_current(self, index: int) -> None:
        for i, button in enumerate(self.buttons):
            button.set_selected(i == index)


# ------------------------------------------------------------------ containers
class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(2, 0, 2, 0)
        self.layout.setSpacing(10)
        text_box = QVBoxLayout()
        text_box.setSpacing(3)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("PageTitle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("PageSubtitle")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(subtitle))
        text_box.addWidget(self.title_label)
        text_box.addWidget(self.subtitle_label)
        self.layout.addLayout(text_box)
        self.layout.addStretch(1)
        self._actions = QHBoxLayout()
        self._actions.setSpacing(8)
        self.layout.addLayout(self._actions)

    def add_action(self, widget: QWidget) -> None:
        self._actions.addWidget(widget, 0, Qt.AlignVCenter)


class SectionCard(QFrame):
    def __init__(self, title: str, hint: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAttribute(Qt.WA_Hover, True)
        add_shadow(self, blur=24, dy=5, alpha=14)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(10)
        header = QHBoxLayout()
        header.setSpacing(8)
        title_label = QLabel(title)
        title_label.setObjectName("SectionTitle")
        header.addWidget(title_label)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("SectionHint")
            header.addWidget(hint_label)
        header.addStretch(1)
        outer.addLayout(header)
        self.body = QVBoxLayout()
        self.body.setSpacing(9)
        outer.addLayout(self.body)


class StatCard(QFrame):
    def __init__(self, value: str, label: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("StatCard")
        self.setMinimumWidth(130)
        self.setFixedHeight(92)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        add_shadow(self, blur=22, dy=4, alpha=12)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 13, 18, 11)
        layout.setSpacing(2)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("StatValue")
        self.label_label = QLabel(label)
        self.label_label.setObjectName("StatLabel")
        layout.addWidget(self.value_label)
        layout.addWidget(self.label_label)
        layout.addStretch(1)

    def set_value(self, value: object) -> None:
        self.value_label.setText(str(value))


class MasterBanner(QFrame):
    opened = Signal(str)

    def __init__(self, card: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.card = card
        self.knw_id = card.get("knw_id", "")
        self.setObjectName("MasterBanner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(3)
        title = QLabel(card.get("title_zh") or card.get("title_en") or "")
        title.setObjectName("MasterTitle")
        title.setWordWrap(True)
        body = QLabel(
            render_atomic_markdown(
                card.get("summary_zh") or card.get("premises_and_conditions") or "",
                inline=True,
            )
        )
        body.setObjectName("MasterBody")
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        meta = QLabel(
            f"{self.knw_id} · {card_type_label(card.get('card_type'))}"
            f" · 关联文献 {len(card.get('supporting_doc_ids_list') or [])} 篇"
        )
        meta.setObjectName("Tiny")
        text.addWidget(title)
        text.addWidget(body)
        text.addWidget(meta)
        layout.addLayout(text, 1)
        button = QPushButton("查看总纲")
        button.setObjectName("Pill")
        button.clicked.connect(lambda: self.opened.emit(self.knw_id))
        layout.addWidget(button, 0, Qt.AlignVCenter)


class CardSummaryBrowser(QTextBrowser):
    """Transparent, independently scrollable card summary."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CardSummary")
        self.setFrameShape(QFrame.NoFrame)
        self.setOpenExternalLinks(False)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setStyleSheet(
            "QTextBrowser#CardSummary { background: transparent; border: none;"
            " padding: 0px 14px 0px 0px; }"
        )

    def wheelEvent(self, event) -> None:  # noqa: N802
        bar = self.verticalScrollBar()
        if bar.maximum() <= bar.minimum():
            event.ignore()
            return
        delta = event.angleDelta().y()
        if delta > 0 and bar.value() <= bar.minimum():
            event.ignore()
            return
        if delta < 0 and bar.value() >= bar.maximum():
            event.ignore()
            return
        super().wheelEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class KnowledgeCard(QFrame):
    opened = Signal(str)

    def __init__(self, card: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.card = card
        self.knw_id = card.get("knw_id", "")
        self.setObjectName("Card")
        if int(card.get("audit_delete_suggested") or 0) or int(card.get("audit_hidden") or 0):
            self.setProperty("auditState", "suggested_delete")
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setMinimumWidth(300)
        self.setFixedHeight(286)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        add_shadow(self, blur=26, dy=6, alpha=18)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 15, 18, 13)
        layout.setSpacing(7)

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(make_chip(self.knw_id))
        if card.get("card_level") == "L1":
            top.addWidget(make_chip("L1", "StatusOk"))
        if int(card.get("has_conflict") or 0):
            top.addWidget(make_chip("⚡ 争议", "StatusWarn"))
        if int(card.get("audit_delete_suggested") or 0) or int(card.get("audit_hidden") or 0):
            top.addWidget(make_chip("建议删除", "StatusDanger"))
        elif str(card.get("audit_status") or "").upper() == "AI_ADDED":
            top.addWidget(make_chip("AI 审计新增", "StatusOk"))
        if int(card.get("audit_divergence") or 0):
            top.addWidget(make_chip("评价分歧", "StatusWarn"))
        elif int(card.get("audit_positive_count") or 0) >= 2:
            top.addWidget(make_chip("多模型肯定", "StatusOk"))
        top.addStretch(1)
        top.addWidget(
            make_type_chip(
                card_type_label(card.get("card_type")),
                knw=card.get("card_type"),
            )
        )
        layout.addLayout(top)

        title = QLabel(card.get("title_zh") or card.get("title_en") or "")
        title.setObjectName("CardTitle")
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        title.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(title)

        self.summary_browser = CardSummaryBrowser(self)
        self.summary_browser.setHtml(
            render_atomic_markdown(card.get("summary_zh") or "", inline=True)
        )
        self.summary_browser.setFixedHeight(86)
        self.summary_browser.clicked.connect(lambda: self.opened.emit(self.knw_id))
        layout.addWidget(self.summary_browser)

        keywords = [str(item.get("term_en")) for item in card.get("keywords", [])][:3]
        if keywords:
            tag_label = QLabel(" · ".join(keywords))
            tag_label.setObjectName("Tiny")
            tag_label.setWordWrap(True)
            layout.addWidget(tag_label)

        layout.addStretch(0)
        docs = card.get("supporting_doc_ids_list") or []
        page = card.get("page_num")
        footer_text = "来源: " + (docs[0] if docs else "—")
        if page is not None:
            footer_text += f" · 第 {page} 页"
        footer_text += f" · 关联文献 {len(docs)} 篇"
        footer = QLabel(footer_text)
        footer.setObjectName("CardFooter")
        footer.setWordWrap(True)
        layout.addWidget(footer)

        self.setToolTip(
            (card.get("title_zh") or "")
            + "\n"
            + plain_snippet(card.get("premises_and_conditions") or "")
        )
        for child in self.findChildren(QLabel):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.opened.emit(self.knw_id)
        super().mouseReleaseEvent(event)


class ResultCard(QFrame):
    opened = Signal(object)

    def __init__(self, hit, parent: QWidget | None = None):
        super().__init__(parent)
        self.hit = hit
        self.setObjectName("Card")
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        add_shadow(self, blur=22, dy=5, alpha=13)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(6)
        if hit.kind == "master":
            chip = make_type_chip("L1 总纲", knw="MASTER")
        elif hit.kind == "card":
            chip = make_type_chip(
                card_type_label((hit.card or {}).get("card_type")),
                knw=(hit.card or {}).get("card_type"),
            )
        elif hit.kind == "document":
            chip = make_type_chip("文献", kind="document")
        elif hit.kind == "keyword":
            chip = make_type_chip("关键词", kind="keyword")
        else:
            chip = make_type_chip("证据", kind="document")
        top.addWidget(chip)
        top.addWidget(make_chip(hit.id))
        top.addStretch(1)
        if hit.scores:
            score = QLabel(f"score {hit.score:.3f}")
            score.setObjectName("Tiny")
            top.addWidget(score)
        layout.addLayout(top)
        title = QLabel(hit.title or "")
        title.setObjectName("CardTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        snippet = QLabel()
        snippet.setObjectName("CardBody")
        snippet.setWordWrap(True)
        snippet.setTextFormat(Qt.RichText)
        snippet.setText(highlight_snippet(hit.snippet or hit.summary))
        snippet.setMaximumHeight(48)
        layout.addWidget(snippet)
        self.setToolTip(plain_snippet(hit.summary or hit.snippet))
        for child in self.findChildren(QLabel):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.opened.emit(self.hit)
        super().mouseReleaseEvent(event)


class KeywordChip(QPushButton):
    def __init__(self, keyword: dict, parent: QWidget | None = None):
        term = keyword.get("term_en") or ""
        zh = keyword.get("term_zh") or ""
        label = f"{term} · {zh}" if zh else term
        super().__init__(label, parent)
        self.term = term
        self.setObjectName("KeywordChip")
        self.setCursor(QCursor(Qt.PointingHandCursor))
        if keyword.get("definition"):
            self.setToolTip(keyword["definition"])


# ------------------------------------------------------------------- drop zone
class DropZone(QFrame):
    files_dropped = Signal(list)
    browse_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(128)
        self.setCursor(QCursor(Qt.PointingHandCursor))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(2)
        layout.setAlignment(Qt.AlignCenter)
        icon = QLabel("⇩")
        icon.setObjectName("DropIcon")
        icon.setAlignment(Qt.AlignCenter)
        title = QLabel("将 PDF 文献拖拽至此处导入并解析")
        title.setObjectName("DropTitle")
        title.setAlignment(Qt.AlignCenter)
        hint = QLabel("自动读取首页 DOI 与标题，匹配待手动导入清单")
        hint.setObjectName("DropHint")
        hint.setAlignment(Qt.AlignCenter)
        self.browse_button = QPushButton("选择本地 PDF 导入")
        self.browse_button.setObjectName("Pill")
        self.browse_button.clicked.connect(self.browse_requested)
        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self.browse_button, 0, Qt.AlignHCenter)
        for child in self.findChildren(QLabel):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def _is_pdf_url(self, event) -> bool:
        return event.mimeData().hasUrls() and any(
            url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf")
            for url in event.mimeData().urls()
        )

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._is_pdf_url(event):
            event.acceptProposedAction()
            self.setProperty("drag", "true")
            repolish(self)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.setProperty("drag", "false")
        repolish(self)

    def dropEvent(self, event) -> None:  # noqa: N802
        self.setProperty("drag", "false")
        repolish(self)
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf")
        ]
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)
        else:
            event.ignore()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.browse_requested.emit()
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------- toast
class Toast(QLabel):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setMaximumWidth(640)
        self.setStyleSheet(
            "background-color: #30343B; color: #FFFFFF; border-radius: 14px;"
            "padding: 9px 18px; font-size: 12px;"
        )
        self.setVisible(False)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(1.0)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(self._on_anim_finished)
        self._fading_out = False

    def show_message(self, text: str, duration_ms: int = 2800) -> None:
        self.setText(text)
        self.adjustSize()
        self.reposition()
        self._fading_out = False
        self._effect.setOpacity(0.0)
        self.setVisible(True)
        self.raise_()
        self._anim.stop()
        self._anim.setDuration(160)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._timer.start(duration_ms)

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        x = max(12, (parent.width() - self.width()) // 2)
        y = max(12, parent.height() - self.height() - 30)
        self.move(x, y)

    def _fade_out(self) -> None:
        self._fading_out = True
        self._anim.stop()
        self._anim.setDuration(220)
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _on_anim_finished(self) -> None:
        if self._fading_out:
            self.setVisible(False)
            self._fading_out = False


# --------------------------------------------------------------------- drawer
class CardDrawer(QFrame):
    """Right-side overlay drawer populated from a V2 knowledge card."""

    open_document = Signal(str)
    closed = Signal()
    edit_requested = Signal(str)
    ai_requested = Signal(str, str)
    delete_requested = Signal(str)
    restore_requested = Signal(str)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("Drawer")
        self.setFixedWidth(470)
        self.setVisible(False)
        self.card: dict = {}
        self.ai_panel: QFrame | None = None
        self.ai_instruction: QTextEdit | None = None
        self.ai_send_button: QPushButton | None = None
        self.ai_button: QPushButton | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(8)
        header = QHBoxLayout()
        title = QLabel("知识卡详情")
        title.setObjectName("DrawerTitle")
        header.addWidget(title)
        header.addStretch(1)
        close = QPushButton("✕")
        close.setObjectName("WindowButton")
        close.setFixedSize(30, 28)
        close.clicked.connect(self.close_drawer)
        header.addWidget(close)
        outer.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.content = QWidget()
        self.content.setObjectName("PageRoot")
        self.layout_body = QVBoxLayout(self.content)
        self.layout_body.setContentsMargins(2, 2, 24, 10)
        self.layout_body.setSpacing(9)
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll, 1)

    def _build_actions(self, card: dict) -> None:
        knw_id = str(card.get("knw_id") or "")
        row = QHBoxLayout()
        row.setSpacing(6)
        edit = QPushButton("✏️ 手动编辑")
        edit.setObjectName("Pill")
        edit.setCursor(Qt.PointingHandCursor)
        edit.clicked.connect(lambda: self.edit_requested.emit(knw_id))
        ai = QPushButton("✨ AI 修改")
        ai.setObjectName("Pill")
        ai.setCursor(Qt.PointingHandCursor)
        ai.clicked.connect(self._toggle_ai_panel)
        delete = QPushButton("🗑️ 删除卡片")
        delete.setObjectName("Danger")
        delete.setCursor(Qt.PointingHandCursor)
        delete.clicked.connect(lambda: self.delete_requested.emit(knw_id))
        for button in (edit, ai, delete):
            row.addWidget(button)
        row.addStretch(1)
        self.layout_body.addLayout(row)
        self.ai_button = ai

        panel = QFrame()
        panel.setObjectName("Card")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(12, 10, 12, 10)
        panel_layout.setSpacing(7)
        hint = QLabel("请输入修改建议，例如：补全公式定义、修正某参数或删除乱码描述…")
        hint.setObjectName("Tiny")
        hint.setWordWrap(True)
        instruction = QTextEdit()
        instruction.setPlaceholderText("描述你希望如何修改这张知识卡…")
        instruction.setFixedHeight(78)
        send = QPushButton("🚀 发送给 AI 重新提炼")
        send.setObjectName("Primary")
        send.clicked.connect(self._send_ai)
        panel_layout.addWidget(hint)
        panel_layout.addWidget(instruction)
        panel_layout.addWidget(send, 0, Qt.AlignRight)
        panel.setVisible(False)
        self.layout_body.addWidget(panel)
        self.ai_panel = panel
        self.ai_instruction = instruction
        self.ai_send_button = send

    def _toggle_ai_panel(self) -> None:
        if self.ai_panel is None:
            return
        visible = not self.ai_panel.isVisible()
        self.ai_panel.setVisible(visible)
        if visible and self.ai_instruction is not None:
            self.ai_instruction.setFocus()

    def _send_ai(self) -> None:
        if self.ai_instruction is None:
            return
        instruction = self.ai_instruction.toPlainText().strip()
        if not instruction:
            self.ai_instruction.setFocus()
            return
        self.set_ai_busy(True)
        self.ai_requested.emit(str(self.card.get("knw_id") or ""), instruction)

    def set_ai_busy(self, busy: bool) -> None:
        if self.ai_send_button is not None:
            self.ai_send_button.setEnabled(not busy)
            self.ai_send_button.setText(
                "正在重新提炼…" if busy else "🚀 发送给 AI 重新提炼"
            )
        if self.ai_button is not None:
            self.ai_button.setEnabled(not busy)

    def close_drawer(self) -> None:
        self.setVisible(False)
        self.closed.emit()

    def _build_audit_sections(self, card: dict) -> None:
        provenance = card.get("provenance") or {}
        if provenance:
            heading = QLabel("生成模型来源")
            heading.setObjectName("SectionTitle")
            self.layout_body.addWidget(heading)
            labels = {
                "fast_llm": "快速提取",
                "slow_llm": "深度归纳",
                "vlm": "图像理解",
                "embedding": "向量模型",
                "reranker": "重排模型",
            }
            lines = []
            for role, label in labels.items():
                info = provenance.get(role) or {}
                if not info:
                    lines.append(f"{label}：未记录")
                    continue
                name = info.get("node_name") or info.get("model") or "未知节点"
                model = info.get("web_model") or info.get("model") or ""
                detail = str(name)
                if model and model != name:
                    detail += f" / {model}"
                lines.append(f"{label}：{detail}")
            body = QLabel("\n".join(lines))
            body.setObjectName("Muted")
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.layout_body.addWidget(body)

        reviews = card.get("audit_reviews") or []
        heading = QLabel(f"AI 交叉审计 · {len(reviews)}")
        heading.setObjectName("SectionTitle")
        self.layout_body.addWidget(heading)
        if not reviews:
            empty = QLabel("暂无外部 AI 审计记录。")
            empty.setObjectName("Muted")
            self.layout_body.addWidget(empty)
            return
        for review in reviews[:12]:
            score = review.get("overall_score")
            verdict = review.get("verdict") or "—"
            action = review.get("suggested_action") or "keep"
            auditor = review.get("auditor_name") or "未命名 AI"
            line = f"{auditor}：{score if score is not None else '—'} 分 · {verdict} · {action}"
            if review.get("needs_manual_review"):
                line += " · 需人工复核"
            body = QLabel(line)
            body.setObjectName("Muted")
            body.setWordWrap(True)
            self.layout_body.addWidget(body)
            if review.get("delete_reason"):
                reason = QLabel(f"删除理由：{review['delete_reason']}")
                reason.setObjectName("Tiny")
                reason.setWordWrap(True)
                self.layout_body.addWidget(reason)

    def show_card(self, card: dict) -> None:
        self.card = card
        self.ai_panel = None
        self.ai_instruction = None
        self.ai_send_button = None
        clear_layout(self.layout_body)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(make_chip(card.get("knw_id", "")))
        top.addWidget(
            make_type_chip(
                card_type_label(card.get("card_type")),
                knw=card.get("card_type"),
            )
        )
        if card.get("has_conflict"):
            top.addWidget(make_chip("⚡ 争议", "StatusWarn"))
        if int(card.get("audit_delete_suggested") or 0) or int(card.get("audit_hidden") or 0):
            top.addWidget(make_chip("建议删除", "StatusDanger"))
        elif str(card.get("audit_status") or "").upper() == "AI_ADDED":
            top.addWidget(make_chip("AI 审计新增", "StatusOk"))
        if int(card.get("audit_divergence") or 0):
            top.addWidget(make_chip("评价分歧", "StatusWarn"))
        elif int(card.get("audit_positive_count") or 0) >= 2:
            top.addWidget(make_chip("多模型肯定", "StatusOk"))
        top.addStretch(1)
        self.layout_body.addLayout(top)

        if int(card.get("audit_delete_suggested") or 0) or int(card.get("audit_hidden") or 0):
            banner = QLabel(
                "⛔ 建议删除 · 未物理删除\n"
                f"{card.get('audit_delete_reason') or '外部 AI 建议删除这张卡片。'}\n"
                f"审计来源：{card.get('audit_delete_by') or '—'}"
            )
            banner.setObjectName("AuditDeleteBanner")
            banner.setWordWrap(True)
            self.layout_body.addWidget(banner)
            restore = QPushButton("♻️ 恢复为正常卡片")
            restore.setObjectName("Pill")
            restore.clicked.connect(
                lambda: self.restore_requested.emit(str(card.get("knw_id") or ""))
            )
            self.layout_body.addWidget(restore, 0, Qt.AlignLeft)

        title = QLabel(card.get("title_zh") or card.get("title_en") or "")
        title.setObjectName("DrawerTitle")
        title.setWordWrap(True)
        self.layout_body.addWidget(title)
        self._build_actions(card)

        conjecture = card.get("generalized_conjecture") or (card.get("structured") or {}).get("generalized_conjecture")
        for label, value in (
            ("核心结论", card.get("summary_zh")),
            ("前提与边界", card.get("premises_and_conditions")),
            ("广义物理延拓/推论", conjecture),
        ):
            if not value:
                continue
            heading = QLabel(label)
            heading.setObjectName("SectionTitle")
            body = QLabel()
            body.setObjectName("Muted")
            body.setWordWrap(True)
            body.setTextFormat(Qt.RichText)
            body.setText(
                render_atomic_markdown(
                    value, premises=(label == "前提与边界")
                )
            )
            body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.layout_body.addWidget(heading)
            self.layout_body.addWidget(body)

        formulas = card.get("formulas_latex")
        if formulas:
            heading = QLabel("核心公式")
            heading.setObjectName("SectionTitle")
            self.layout_body.addWidget(heading)
            self.layout_body.addWidget(make_formula_view(formulas, max_width=410))

        code = card.get("code_snippet")
        if code:
            heading = QLabel("程序工作流")
            heading.setObjectName("SectionTitle")
            view = QTextBrowser()
            view.setObjectName("CodeView")
            view.setHtml(code_html(code))
            view.setMaximumHeight(190)
            self.layout_body.addWidget(heading)
            self.layout_body.addWidget(view)

        if card.get("pitfall_warning"):
            heading = QLabel("踩坑预警")
            heading.setObjectName("SectionTitle")
            body = QLabel(card["pitfall_warning"])
            body.setObjectName("Muted")
            body.setWordWrap(True)
            self.layout_body.addWidget(heading)
            self.layout_body.addWidget(body)

        keywords = card.get("keywords") or []
        if keywords:
            label = QLabel("关键词")
            label.setObjectName("SectionTitle")
            self.layout_body.addWidget(label)
            keyword_box = QWidget()
            keyword_flow = FlowLayout(keyword_box, margin=0, h_spacing=6, v_spacing=6)
            keyword_box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
            for item in keywords[:8]:
                keyword_flow.addWidget(make_chip(str(item.get("term_en", ""))))
            keyword_box.setMinimumHeight(
                keyword_flow.heightForWidth(max(280, self.width() - 60))
            )
            self.layout_body.addWidget(keyword_box)

        self._build_audit_sections(card)

        evidence = card.get("evidence") or []
        heading = QLabel(f"L3 原始证据 · {len(evidence)}")
        heading.setObjectName("SectionTitle")
        self.layout_body.addWidget(heading)
        if not evidence:
            empty = QLabel("暂无原文证据包。")
            empty.setObjectName("Muted")
            self.layout_body.addWidget(empty)
        for item in evidence:
            card_frame = QFrame()
            card_frame.setObjectName("Card")
            box = QVBoxLayout(card_frame)
            box.setContentsMargins(12, 10, 12, 10)
            box.setSpacing(4)
            meta = QLabel(
                f"第 {item.get('page_num') or '—'} 页 · {item.get('section_title') or '正文'}"
            )
            meta.setObjectName("Tiny")
            body = QLabel()
            body.setObjectName("Muted")
            body.setWordWrap(True)
            body.setTextFormat(Qt.RichText)
            body.setText(focus_html(item.get("context_bundle_md", "")))
            body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            box.addWidget(meta)
            box.addWidget(body)
            self.layout_body.addWidget(card_frame)

        source_docs = card.get("supporting_doc_ids_list") or []
        source = QLabel(
            "来源: "
            + (", ".join(source_docs) if source_docs else "—")
            + (f" · 关联文献 {len(source_docs)} 篇" if source_docs else "")
        )
        source.setObjectName("Tiny")
        source.setWordWrap(True)
        self.layout_body.addWidget(source)
        footer = QHBoxLayout()
        copy = QPushButton("复制 ID")
        copy.setObjectName("Ghost")
        copy.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(card.get("knw_id", ""))
        )
        footer.addWidget(copy)
        if source_docs:
            open_doc = QPushButton("打开来源文献")
            open_doc.setObjectName("Pill")
            open_doc.clicked.connect(lambda: self.open_document.emit(source_docs[0]))
            footer.addWidget(open_doc)
        footer.addStretch(1)
        self.layout_body.addLayout(footer)
        self.layout_body.addStretch(1)
