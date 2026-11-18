"""V2 detail dialogs: knowledge cards, documents and keywords."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui.controls import (
    WheelSafeComboBox as QComboBox,
    WheelSafeSpinBox as QSpinBox,
)

from audit.report import export_audit_bundle
from db.id_generator import CARD_TYPES
from db.sqlite_manager import SQLiteManager
from models.atomic_prompts import CARD_SYSTEM_PROMPT, MANUAL_MODE_INSTRUCTION
from models.endpoint_pool import EndpointNode, EndpointPool
from models.web_ai_bridge import ClipboardBatchService, ClipboardPacket
from parser.batch_packer import DistilledItem, DynamicBatchPacker
from parser.context_bundler import ContextBundle, bundle_hash
from reprocess import ReprocessManager
from ui.atomic_markdown import render_atomic_markdown
from ui.audit_dialogs import AuditImportDialog
from ui.deep_synthesis_dialog import DeepSynthesisDialog
from ui.flow_layout import clear_layout
from ui.progress_eta import EtaTracker
from ui.pdf_locator import PdfLocatorDialog, locator_has_bbox
from ui.semi_auto_dialog import SemiAutoBatchDialog
from ui.step_progress_widget import StepActionBar, StepProgressWidget
from models.pipeline_stepper import (
    compute_document_pipeline_status,
    rollback_pipeline_from_step,
)
from ui.m3_theme import (
    CARD_TYPE_LABELS,
    DOC_TYPE_LABELS,
    card_type_label,
    code_html,
    doc_type_label,
    download_label,
    focus_html,
    format_authors,
    format_time,
    parse_label,
    status_tone,
)
from ui.widgets import make_chip, make_formula_view, make_status_pill, make_type_chip
from ui.workers import ReprocessWorker


def _copy(text: str) -> None:
    QGuiApplication.clipboard().setText(text)


def _open_url(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


def _open_file(path: Path) -> None:
    if not path.exists():
        QMessageBox.warning(None, "文件不存在", f"找不到文件：\n{path}")
        return
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def _scroll_page(parent: QWidget) -> tuple[QScrollArea, QVBoxLayout]:
    area = QScrollArea(parent)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    content = QWidget()
    content.setObjectName("PageRoot")
    layout = QVBoxLayout(content)
    layout.setContentsMargins(2, 2, 24, 10)
    layout.setSpacing(12)
    area.setWidget(content)
    return area, layout


def _meta_card(rows: list[tuple[str, str]]) -> QFrame:
    card = QFrame()
    card.setObjectName("Card")
    grid = QGridLayout(card)
    grid.setContentsMargins(16, 12, 16, 12)
    grid.setHorizontalSpacing(16)
    grid.setVerticalSpacing(8)
    for index, (key, value) in enumerate(rows):
        key_label = QLabel(key)
        key_label.setObjectName("Tiny")
        key_label.setMinimumWidth(64)
        value_label = QLabel(value or "—")
        value_label.setWordWrap(True)
        value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        grid.addWidget(key_label, index // 2, (index % 2) * 2)
        grid.addWidget(value_label, index // 2, (index % 2) * 2 + 1)
    grid.setColumnStretch(1, 1)
    grid.setColumnStretch(3, 1)
    return card


def _header_row(chips: list[QWidget], close_text: str = "关闭") -> tuple[QHBoxLayout, QPushButton]:
    row = QHBoxLayout()
    row.setSpacing(7)
    for chip in chips:
        row.addWidget(chip)
    row.addStretch(1)
    close_button = QPushButton(close_text)
    close_button.setObjectName("Pill")
    row.addWidget(close_button)
    return row, close_button


def get_card_model_name(
    card: dict[str, Any],
    card_provenance: dict[str, Any] | None = None,
    default_model: str = "",
) -> str:
    """Extract human-readable model name that produced this knowledge card."""
    # 1. From structured details first (e.g. extractor_model stamped by semi-auto / importer)
    struct = card.get("structured")
    if not isinstance(struct, dict) and card.get("structured_details_json"):
        try:
            struct = json.loads(card["structured_details_json"])
        except Exception:
            struct = {}
    if isinstance(struct, dict):
        for k in ("extractor_model", "model_name", "agent_model", "model"):
            v = str(struct.get(k) or "").strip()
            if v and v not in ("用户手动/外部网页 AI", "外部网页 AI", "未知"):
                return v

    # 2. From card_model_provenance records
    if card_provenance:
        is_l1 = (
            str(card.get("card_level") or "").upper() == "L1"
            or str(card.get("card_type") or "").upper() == "MASTER"
        )
        preferred_roles = (
            ("slow_llm", "fast_llm", "extraction")
            if is_l1
            else ("fast_llm", "slow_llm", "extraction")
        )
        for role in preferred_roles:
            info = card_provenance.get(role)
            if isinstance(info, dict):
                m = str(info.get("web_model") or info.get("model") or "").strip()
                if m and m not in ("用户手动/外部网页 AI", "外部网页 AI", "未知"):
                    return m
                node_n = str(info.get("node_name") or "").strip()
                if node_n and node_n not in (
                    "网页版 AI 手动",
                    "快速提取 API",
                    "深度归纳 API",
                    "AI 深度归纳",
                ):
                    return node_n
                if m:
                    return m

        # Fallback to any non-embedding role in provenance
        for role, info in card_provenance.items():
            if role in ("embedding", "reranker"):
                continue
            if isinstance(info, dict):
                m = str(info.get("web_model") or info.get("model") or "").strip()
                if m:
                    return m

    # 3. Fallback to default_model (e.g. from document's semi_auto manifest)
    if default_model and default_model.strip():
        return default_model.strip()

    # 4. From card top-level fields
    for k in ("extractor_model", "agent_model", "model", "model_name"):
        v = str(card.get(k) or "").strip()
        if v:
            return v

    return ""


class KnowledgeCardEditDialog(QDialog):
    """Manual editor for a knowledge card's user-visible fields."""

    def __init__(self, card: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.card = card
        self.setWindowTitle(f"编辑知识卡 · {card.get('knw_id', '')}")
        self.resize(760, 760)
        self.setMinimumSize(620, 560)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)
        heading = QLabel("手动编辑知识卡")
        heading.setObjectName("DrawerTitle")
        outer.addWidget(heading)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("PageRoot")
        form = QFormLayout(content)
        form.setContentsMargins(2, 2, 20, 10)
        form.setSpacing(9)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        self.title_edit = QLineEdit(str(card.get("title_zh") or ""))
        form.addRow("标题", self.title_edit)
        self.type_combo = QComboBox()
        types = ("MASTER",) if str(card.get("card_level") or "").upper() == "L1" else CARD_TYPES
        for value in types:
            self.type_combo.addItem(CARD_TYPE_LABELS.get(value, value), value)
        current_type = str(card.get("card_type") or "CONC").upper()
        index = self.type_combo.findData(current_type)
        self.type_combo.setCurrentIndex(index if index >= 0 else 0)
        form.addRow("类型", self.type_combo)
        self.element_edit = QLineEdit(str(card.get("element") or ""))
        form.addRow("元素", self.element_edit)
        self.z_spin = QSpinBox()
        self.z_spin.setRange(0, 118)
        self.z_spin.setSpecialValueText("—")
        self.z_spin.setValue(int(card.get("z_num") or 0))
        form.addRow("原子序数 Z", self.z_spin)
        self.ion_edit = QLineEdit(str(card.get("ion_stage") or ""))
        form.addRow("离子态", self.ion_edit)
        self.iso_edit = QLineEdit(str(card.get("isoelectronic") or ""))
        form.addRow("等电子序列", self.iso_edit)
        self.method_edit = QLineEdit(str(card.get("method") or ""))
        form.addRow("方法", self.method_edit)
        self.code_edit = QLineEdit(str(card.get("code_name") or ""))
        form.addRow("程序", self.code_edit)
        self.process_edit = QLineEdit(str(card.get("physical_process") or ""))
        form.addRow("物理过程", self.process_edit)

        self.summary_edit = self._text_edit(card.get("summary_zh"), 92)
        form.addRow("核心结论", self.summary_edit)
        self.premises_edit = self._text_edit(card.get("premises_and_conditions"), 92)
        form.addRow("前提与边界", self.premises_edit)
        self.formula_edit = self._text_edit(card.get("formulas_latex"), 110)
        form.addRow("核心公式 LaTeX", self.formula_edit)
        self.code_snippet_edit = self._text_edit(card.get("code_snippet"), 110)
        form.addRow("代码与参数", self.code_snippet_edit)
        self.pitfall_edit = self._text_edit(card.get("pitfall_warning"), 92)
        form.addRow("踩坑预警", self.pitfall_edit)
        conjecture_val = card.get("generalized_conjecture") or (card.get("structured") or {}).get("generalized_conjecture")
        self.conjecture_edit = self._text_edit(conjecture_val, 80)
        form.addRow("广义物理延拓/推论", self.conjecture_edit)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("Pill")
        cancel.clicked.connect(self.reject)
        save = QPushButton("💾 保存修改")
        save.setObjectName("Primary")
        save.clicked.connect(self._save)
        actions.addWidget(cancel)
        actions.addWidget(save)
        outer.addLayout(actions)

    @staticmethod
    def _text_edit(value: object, height: int) -> QTextEdit:
        editor = QTextEdit()
        editor.setPlainText(str(value or ""))
        editor.setMinimumHeight(height)
        return editor

    def _save(self) -> None:
        if len(self.title_edit.text().strip()) < 2:
            QMessageBox.warning(self, "无法保存", "标题不能为空。")
            return
        self.accept()

    def values(self) -> dict:
        z_num = self.z_spin.value()
        return {
            "title_zh": self.title_edit.text().strip(),
            "card_type": self.type_combo.currentData(),
            "element": self.element_edit.text().strip(),
            "z_num": None if z_num <= 0 else z_num,
            "ion_stage": self.ion_edit.text().strip(),
            "isoelectronic": self.iso_edit.text().strip(),
            "method": self.method_edit.text().strip(),
            "code_name": self.code_edit.text().strip(),
            "physical_process": self.process_edit.text().strip(),
            "summary_zh": self.summary_edit.toPlainText().strip(),
            "premises_and_conditions": self.premises_edit.toPlainText().strip(),
            "formulas_latex": self.formula_edit.toPlainText().strip(),
            "code_snippet": self.code_snippet_edit.toPlainText().strip(),
            "pitfall_warning": self.pitfall_edit.toPlainText().strip(),
            "generalized_conjecture": self.conjecture_edit.toPlainText().strip() or None,
        }


class KnowledgeDetailDialog(QDialog):
    def __init__(
        self,
        db: SQLiteManager,
        workspace: str | Path,
        knw_id: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.workspace = Path(workspace)
        self.node = db.get_knowledge_card(knw_id)
        self.setWindowTitle(f"{knw_id} · AtomicCalculationDB")
        self.resize(840, 740)
        self.setMinimumSize(700, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        if not self.node:
            layout.addWidget(QLabel(f"找不到知识卡：{knw_id}"))
            close = QPushButton("关闭")
            close.setObjectName("Pill")
            close.clicked.connect(self.accept)
            layout.addWidget(close, 0, Qt.AlignRight)
            return
        node = self.node
        header, close = _header_row(
            [
                make_chip(node["knw_id"]),
                make_type_chip(card_type_label(node.get("card_type")), knw=node.get("card_type")),
                make_chip("L1" if node.get("card_level") == "L1" else "L2"),
                make_chip("已向量化", "StatusOk")
                if node.get("is_vectorized")
                else make_chip("未向量化", "StatusOff"),
                *(
                    [make_chip("建议删除", "StatusDanger")]
                    if int(node.get("audit_delete_suggested") or 0) or int(node.get("audit_hidden") or 0)
                    else []
                ),
                *(
                    [make_chip("评价分歧", "StatusWarn")]
                    if int(node.get("audit_divergence") or 0)
                    else []
                ),
            ]
        )
        close.clicked.connect(self.accept)
        layout.addLayout(header)
        title = QLabel(node.get("title_zh") or node.get("title_en") or "")
        title.setObjectName("DialogTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        if int(node.get("audit_delete_suggested") or 0) or int(node.get("audit_hidden") or 0):
            restore_row = QHBoxLayout()
            banner = QLabel(
                "⛔ 建议删除 · 未物理删除："
                + (node.get("audit_delete_reason") or "外部 AI 建议删除这张卡片。")
            )
            banner.setObjectName("AuditDeleteBanner")
            banner.setWordWrap(True)
            restore_row.addWidget(banner, 1)
            restore = QPushButton("恢复为正常卡片")
            restore.setObjectName("Pill")
            restore.clicked.connect(self._restore_audit_card)
            restore_row.addWidget(restore)
            layout.addLayout(restore_row)
        scroll, body = _scroll_page(self)
        layout.addWidget(scroll, 1)

        conjecture = node.get("generalized_conjecture") or (node.get("structured") or {}).get("generalized_conjecture")
        for label, value in (
            ("核心结论", node.get("summary_zh")),
            ("前提与边界", node.get("premises_and_conditions")),
            ("广义物理延拓/推论", conjecture),
        ):
            if not value:
                continue
            heading = QLabel(label)
            heading.setObjectName("SectionTitle")
            text = QLabel()
            text.setObjectName("Muted")
            text.setWordWrap(True)
            text.setTextFormat(Qt.RichText)
            text.setText(
                render_atomic_markdown(
                    value or "—", premises=(label == "前提与边界")
                )
            )
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            body.addWidget(heading)
            body.addWidget(text)
        conditions = (node.get("structured") or {}).get("conditions") or {}
        if isinstance(conditions, dict):
            labels = (
                ("target_quantity", "目标量"),
                ("unit", "单位"),
                ("value", "数值"),
                ("uncertainty", "不确定度"),
                ("state_initial", "初态"),
                ("state_final", "末态"),
                ("transition", "跃迁类型"),
                ("j_parity", "J / 宇称"),
                ("method_boundary", "方法边界"),
                ("convergence", "收敛判据"),
                ("applicability", "适用范围"),
            )
            lines = [
                f"{label}：{conditions.get(key)}"
                for key, label in labels
                if conditions.get(key) not in (None, "")
            ]
            if conditions.get("unknown_fields"):
                lines.append(
                    "未知字段：" + "、".join(str(item) for item in conditions["unknown_fields"])
                )
            if lines:
                heading = QLabel("结构化适用条件")
                heading.setObjectName("SectionTitle")
                body.addWidget(heading)
                text = QLabel("\n".join(lines))
                text.setObjectName("Muted")
                text.setWordWrap(True)
                body.addWidget(text)
        if node.get("formulas_latex"):
            heading = QLabel("核心公式")
            heading.setObjectName("SectionTitle")
            body.addWidget(heading)
            body.addWidget(make_formula_view(node["formulas_latex"], max_width=740))
        if node.get("code_snippet"):
            heading = QLabel("程序工作流")
            heading.setObjectName("SectionTitle")
            view = QTextBrowser()
            view.setObjectName("CodeView")
            view.setHtml(code_html(node["code_snippet"]))
            view.setMinimumHeight(100)
            view.setMaximumHeight(220)
            body.addWidget(heading)
            body.addWidget(view)
        if node.get("pitfall_warning"):
            heading = QLabel("踩坑预警")
            heading.setObjectName("SectionTitle")
            text = QLabel(node["pitfall_warning"])
            text.setObjectName("Muted")
            text.setWordWrap(True)
            body.addWidget(heading)
            body.addWidget(text)
        if node.get("structured"):
            heading = QLabel("结构化参数")
            heading.setObjectName("SectionTitle")
            view = QTextBrowser()
            view.setObjectName("JsonView")
            view.setPlainText(json.dumps(node["structured"], ensure_ascii=False, indent=2))
            view.setMaximumHeight(140)
            body.addWidget(heading)
            body.addWidget(view)
        keywords = node.get("keywords") or []
        if keywords:
            row = QHBoxLayout()
            row.setSpacing(6)
            row.addWidget(QLabel("关键词"))
            for keyword in keywords[:8]:
                row.addWidget(make_chip(keyword.get("term_en", "")))
            row.addStretch(1)
            body.addLayout(row)
        provenance = node.get("provenance") or {}
        reviews = node.get("audit_reviews") or []
        if provenance or reviews:
            heading = QLabel("生成模型与 AI 审计")
            heading.setObjectName("SectionTitle")
            body.addWidget(heading)
            if provenance:
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
                        continue
                    model = info.get("web_model") or info.get("model") or ""
                    lines.append(
                        f"{label}：{info.get('node_name') or model or '未知'} "
                        f"{('· ' + model) if model and model != info.get('node_name') else ''}"
                    )
                text = QLabel("\n".join(lines) or "未记录生成模型")
                text.setObjectName("Muted")
                text.setWordWrap(True)
                body.addWidget(text)
            for review in reviews[:12]:
                text = QLabel(
                    f"{review.get('auditor_name') or '未命名 AI'}："
                    f"{review.get('overall_score') if review.get('overall_score') is not None else '—'} 分 · "
                    f"{review.get('verdict') or '—'} · {review.get('suggested_action') or 'keep'}"
                )
                text.setObjectName("Muted")
                text.setWordWrap(True)
                body.addWidget(text)
        relations = node.get("relations") or []
        if relations:
            heading = QLabel(f"关联卡片 · {len(relations)}")
            heading.setObjectName("SectionTitle")
            body.addWidget(heading)
            relation_labels = {
                "SAME_CLAIM": "同义结论",
                "SUPPORTS": "支持",
                "CONTRADICTS": "疑似矛盾",
                "RELATED": "相关",
            }
            for relation in relations[:12]:
                other_title = (
                    relation.get("other_title_zh")
                    or relation.get("other_title_en")
                    or relation.get("other_knw_id")
                    or "未知卡片"
                )
                text = QLabel(
                    f"{relation_labels.get(str(relation.get('relation_type') or '').upper(), relation.get('relation_type') or '关联')}："
                    f"{other_title} · {relation.get('other_knw_id') or ''}"
                    + (
                        f" · 相似度 {float(relation.get('confidence') or 0):.2f}"
                        if relation.get("confidence")
                        else ""
                    )
                )
                text.setObjectName("Muted")
                text.setWordWrap(True)
                body.addWidget(text)
        heading = QLabel(f"L3 原始证据 · {len(node.get('evidence') or [])}")
        heading.setObjectName("SectionTitle")
        body.addWidget(heading)
        for item in node.get("evidence") or []:
            card = QFrame()
            card.setObjectName("Card")
            box = QVBoxLayout(card)
            box.setContentsMargins(12, 10, 12, 10)
            meta = QLabel(
                f"第 {item.get('page_num') or '—'} 页 · {item.get('section_title') or '正文'}"
            )
            meta.setObjectName("Tiny")
            text = QLabel()
            text.setObjectName("Muted")
            text.setWordWrap(True)
            text.setTextFormat(Qt.RichText)
            text.setText(focus_html(item.get("context_bundle_md", "")))
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            box.addWidget(meta)
            box.addWidget(text)
            locator = item.get("locator") or {}
            if locator_has_bbox(locator):
                locate = QPushButton("定位原文")
                locate.setObjectName("Ghost")
                locate.clicked.connect(
                    lambda _checked=False, loc=dict(locator): self._open_locator(loc)
                )
                box.addWidget(locate, 0, Qt.AlignLeft)
            body.addWidget(card)
        docs = node.get("supporting_doc_ids_list") or []
        source = QFrame()
        source.setObjectName("Card")
        source_layout = QHBoxLayout(source)
        source_layout.setContentsMargins(14, 10, 14, 10)
        source_label = QLabel("来源文献：" + (", ".join(docs) if docs else "—"))
        source_label.setObjectName("Muted")
        source_layout.addWidget(source_label, 1)
        if docs:
            button = QPushButton("打开来源文献")
            button.setObjectName("Pill")
            button.clicked.connect(
                lambda: DocumentDetailDialog(
                    self.db, self.workspace, docs[0], parent=self
                ).exec()
            )
            source_layout.addWidget(button)
        body.addWidget(source)
        body.addStretch(1)

    def _open_locator(self, locator: dict) -> None:
        doc_id = str(locator.get("doc_id") or "").strip()
        if not doc_id:
            docs = self.node.get("supporting_doc_ids_list") or []
            doc_id = str(docs[0]) if docs else ""
        document = self.db.get_document(doc_id) if doc_id else None
        path_value = str((document or {}).get("local_pdf_path") or "")
        path = Path(path_value)
        if not path.is_absolute():
            path = self.workspace / path
        if not path_value or not path.is_file():
            QMessageBox.information(self, "无法定位", "找不到该文献的本地 PDF 文件。")
            return
        PdfLocatorDialog(path, locator, parent=self).exec()

    def _restore_audit_card(self) -> None:
        if not self.node:
            return
        if self.db.restore_card_from_audit_suggestion(self.node["knw_id"]):
            QMessageBox.information(self, "已恢复", "已恢复为正常卡片，审计历史仍保留。")
            self.accept()
        else:
            QMessageBox.information(self, "无需恢复", "这张卡没有待恢复的删除建议。")


class DocumentDetailDialog(QDialog):
    def __init__(
        self,
        db: SQLiteManager,
        workspace: str | Path,
        doc_id: str,
        parent: QWidget | None = None,
        *,
        pipeline: Any = None,
    ):
        super().__init__(parent)
        self.db = db
        self.workspace = Path(workspace)
        self.doc = db.get_document(doc_id)
        self.pipeline = pipeline
        self.reprocess_manager = (
            ReprocessManager(db, self.workspace, pipeline) if pipeline is not None else None
        )
        self._worker: ReprocessWorker | None = None
        self._reprocess_eta = EtaTracker()
        self._clipboard_packet: ClipboardPacket | None = None
        self._clipboard_service: ClipboardBatchService | None = None
        self.setWindowTitle(f"{doc_id} · AtomicCalculationDB")
        self.resize(820, 720)
        self.setMinimumSize(680, 560)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)
        if not self.doc:
            outer.addWidget(QLabel(f"找不到文献：{doc_id}"))
            close = QPushButton("关闭")
            close.setObjectName("Pill")
            close.clicked.connect(self.accept)
            outer.addWidget(close, 0, Qt.AlignRight)
            return
        doc = self.doc
        header, close = _header_row(
            [
                make_type_chip(doc_type_label(doc.get("doc_type")), kind="document"),
                make_status_pill(doc.get("download_status")),
                make_status_pill(doc.get("parse_status"), parse=True),
                make_chip(doc["doc_id"]),
            ]
        )
        close.clicked.connect(self.accept)
        outer.addLayout(header)
        title = QLabel(doc.get("title") or "")
        title.setObjectName("DialogTitle")
        title.setWordWrap(True)
        outer.addWidget(title)
        meta = QLabel(
            " · ".join(
                part
                for part in (
                    format_authors(doc.get("authors_list")),
                    doc.get("journal") or "",
                    str(doc.get("pub_year") or ""),
                )
                if part
            )
        )
        meta.setObjectName("Muted")
        meta.setWordWrap(True)
        outer.addWidget(meta)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        open_pdf = QPushButton("打开 PDF")
        open_pdf.setObjectName("Primary")
        open_pdf.setEnabled(bool(doc.get("local_pdf_path")))
        open_pdf.clicked.connect(self._open_pdf)
        actions.addWidget(open_pdf)
        if doc.get("doi"):
            doi_button = QPushButton("打开 DOI")
            doi_button.setObjectName("Pill")
            doi_button.clicked.connect(lambda: _open_url(f"https://doi.org/{doc['doi']}"))
            actions.addWidget(doi_button)
        folder = QPushButton("打开存储文件夹")
        folder.setObjectName("Pill")
        folder.clicked.connect(self._open_folder)
        actions.addWidget(folder)
        actions.addStretch(1)
        copy = QPushButton("复制 ID")
        copy.setObjectName("Ghost")
        copy.clicked.connect(lambda: _copy(doc["doc_id"]))
        actions.addWidget(copy)
        outer.addLayout(actions)

        audit_row = QHBoxLayout()
        audit_row.setSpacing(7)
        self.export_audit_button = QPushButton("📤 导出 AI 校对包")
        self.export_audit_button.setObjectName("Pill")
        self.export_audit_button.clicked.connect(self._export_audit_bundle)
        self.import_audit_button = QPushButton("📥 导入 AI 校对结果")
        self.import_audit_button.setObjectName("Pill")
        self.import_audit_button.clicked.connect(self._import_audit_result)
        audit_row.addWidget(self.export_audit_button)
        audit_row.addWidget(self.import_audit_button)
        audit_hint = QLabel("导出报告与提示词；导入外部 AI 的 JSON，不消耗 Token")
        audit_hint.setObjectName("Tiny")
        audit_row.addWidget(audit_hint)
        self.audit_history_check = QCheckBox("报告包含已有审计")
        self.audit_history_check.setToolTip(
            "默认关闭，避免外部 AI 被上一次评分锚定。"
        )
        audit_row.addWidget(self.audit_history_check)
        audit_row.addStretch(1)
        outer.addLayout(audit_row)

        audit_options = QHBoxLayout()
        audit_options.setSpacing(7)
        self.audit_source_check = QCheckBox("附带 OCR/Markdown 原文")
        self.audit_source_check.setToolTip(
            "把 parsed.md 复制为 source.md，随报告一起交给外部 AI。"
        )
        self.audit_source_check.setChecked(bool(doc.get("local_md_path")))
        self.audit_evidence_check = QCheckBox("报告包含 L3 证据")
        self.audit_evidence_check.setToolTip(
            "在报告和 cards.jsonl 中附带生成卡片时使用的上下文证据。"
        )
        self.audit_evidence_check.setChecked(True)
        self.audit_coverage_check = QCheckBox("覆盖审计（查漏卡）")
        self.audit_coverage_check.setToolTip(
            "要求外部 AI 除逐卡核对外，寻找原文中未被卡片覆盖的独立知识点。"
        )
        audit_options.addWidget(self.audit_source_check)
        audit_options.addWidget(self.audit_evidence_check)
        audit_options.addWidget(self.audit_coverage_check)
        audit_options.addStretch(1)
        outer.addLayout(audit_options)

        # Visual 5-Step Stepper & Action Controls
        self.stepper = StepProgressWidget(self)
        self.stepper.step_clicked.connect(self._handle_step_action)
        outer.addWidget(self.stepper)

        # Strategy / Execution Mode Row for Step 3 & Step 5
        self.strategy_frame = QFrame()
        self.strategy_frame.setObjectName("StrategyBar")
        self.strategy_frame.setStyleSheet("""
            QFrame#StrategyBar {
                background: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 6px;
                padding: 2px 8px;
            }
        """)
        strat_layout = QHBoxLayout(self.strategy_frame)
        strat_layout.setContentsMargins(6, 3, 6, 3)
        strat_layout.setSpacing(10)

        lbl_strat = QLabel("⚙️ 执行策略：")
        lbl_strat.setStyleSheet("font-weight: bold; color: #475569; font-size: 11px;")
        strat_layout.addWidget(lbl_strat)

        lbl_fast = QLabel("③ AI 快速提取:")
        lbl_fast.setStyleSheet("color: #334155; font-size: 11px;")
        self.fast_mode_combo = QComboBox()
        self.fast_mode_combo.setObjectName("Pill")
        self.fast_mode_combo.addItem("🤖 模型调度策略 (API 自动)", "auto")
        self.fast_mode_combo.addItem("📋 网页版 AI 手动 (零 Token)", "manual")
        self.fast_mode_combo.setToolTip("选择第 ③ 步「AI 快速提取」执行方式：走大模型接口并发调用，或导出任务包由网页版大模型处理")
        strat_layout.addWidget(lbl_fast)
        strat_layout.addWidget(self.fast_mode_combo)

        sep = QLabel("│")
        sep.setStyleSheet("color: #cbd5e1;")
        strat_layout.addWidget(sep)

        lbl_deep = QLabel("⑤ AI 深度归纳:")
        lbl_deep.setStyleSheet("color: #334155; font-size: 11px;")
        self.deep_mode_combo = QComboBox()
        self.deep_mode_combo.setObjectName("Pill")
        self.deep_mode_combo.addItem("🤖 模型调度策略 (API/Agent)", "auto")
        self.deep_mode_combo.addItem("📋 网页版 AI 手动 (零 Token)", "manual")
        self.deep_mode_combo.setToolTip("选择第 ⑤ 步「AI 深度归纳」执行方式：走后台 Agent/大模型推理，或复制深度归纳 Prompt 由网页版大模型生成 Master 知识卡")
        strat_layout.addWidget(lbl_deep)
        strat_layout.addWidget(self.deep_mode_combo)

        strat_layout.addStretch(1)
        outer.addWidget(self.strategy_frame)

        self.action_bar = StepActionBar(self)
        self.action_bar.action_triggered.connect(self._handle_step_action)
        outer.addWidget(self.action_bar)
        self.incremental_button = self.action_bar.btn_step3
        self.force_button = self.action_bar.btn_step1

        reprocess_row = QHBoxLayout()
        reprocess_row.setSpacing(7)
        reprocess_row.addWidget(QLabel("文献类型"))
        self.type_combo = QComboBox()
        for value in ("PAP", "MAN", "BOK", "REV"):
            self.type_combo.addItem(DOC_TYPE_LABELS.get(value, value), value)
        current_type = str(doc.get("doc_type") or "PAP").upper()
        type_index = self.type_combo.findData(current_type)
        self.type_combo.setCurrentIndex(type_index if type_index >= 0 else 0)
        self.type_combo.currentIndexChanged.connect(self._change_document_type)
        reprocess_row.addWidget(self.type_combo)

        self.stop_button = QPushButton("⏹ 停止当前任务")
        self.stop_button.setObjectName("Ghost")
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._stop_reprocess)
        reprocess_row.addWidget(self.stop_button)

        self.copy_packet_button = QPushButton("📋 生成任务包")
        self.copy_packet_button.setObjectName("Pill")
        self.copy_packet_button.setToolTip("生成剪贴板任务包供网页版 AI 使用")
        self.copy_packet_button.clicked.connect(self._copy_task_packet)
        self.import_clipboard_button = QPushButton("📥 导入回复")
        self.import_clipboard_button.setObjectName("Ghost")
        self.import_clipboard_button.clicked.connect(self._import_clipboard_reply)
        reprocess_row.addWidget(self.copy_packet_button)
        reprocess_row.addWidget(self.import_clipboard_button)

        reprocess_row.addStretch(1)
        if self.pipeline is None:
            self.action_bar.setEnabled(False)
            self.copy_packet_button.setEnabled(False)
            self.import_clipboard_button.setEnabled(False)
        outer.addLayout(reprocess_row)
        self.reprocess_progress = QProgressBar()
        self.reprocess_progress.setRange(0, 100)
        self.reprocess_progress.setValue(0)
        self.reprocess_progress.setFormat("整体进度 %p%")
        self.reprocess_progress.setVisible(False)
        outer.addWidget(self.reprocess_progress)
        self.reprocess_eta = QLabel("")
        self.reprocess_eta.setObjectName("Tiny")
        self.reprocess_eta.setVisible(False)
        outer.addWidget(self.reprocess_eta)
        self.reprocess_log = QPlainTextEdit()
        self.reprocess_log.setReadOnly(True)
        self.reprocess_log.setMaximumHeight(90)
        self.reprocess_log.setVisible(False)
        outer.addWidget(self.reprocess_log)

        scroll, body = _scroll_page(self)
        outer.addWidget(scroll, 1)
        backend_meta = doc.get("metadata") or {}
        backend = str(backend_meta.get("markdown_backend") or "—")
        if backend == "mineru":
            backend = (
                f"MinerU {backend_meta.get('mineru_version') or ''} "
                f"({backend_meta.get('mineru_tier') or '—'})"
            )
        body.addWidget(
            _meta_card(
                [
                    ("下载状态", download_label(doc.get("download_status"))),
                    ("解析状态", parse_label(doc.get("parse_status"))),
                    ("类型", doc_type_label(doc.get("doc_type"))),
                    ("年份", str(doc.get("pub_year") or "—")),
                    ("期刊", doc.get("journal") or "—"),
                    ("DOI", doc.get("doi") or "—"),
                    ("arXiv", doc.get("arxiv_id") or "—"),
                    ("来源", doc.get("source_url") or "—"),
                    ("文件", doc.get("local_pdf_path") or "尚未下载"),
                    ("Markdown", doc.get("local_md_path") or "尚未解析"),
                    ("Markdown 后端", backend),
                ]
            )
        )
        if doc.get("abstract"):
            abstract = QTextBrowser()
            abstract.setObjectName("MarkdownView")
            abstract.setOpenExternalLinks(True)
            abstract.setMarkdown(doc["abstract"])
            abstract.setMinimumHeight(100)
            abstract.setMaximumHeight(190)
            body.addWidget(abstract)
        self.related_frame = QFrame()
        self.related_frame.setObjectName("Card")
        self.related_layout = QVBoxLayout(self.related_frame)
        self.related_layout.setContentsMargins(16, 12, 16, 12)
        self.related_layout.setSpacing(7)
        body.addWidget(self.related_frame)
        self._refresh_related_cards()
        body.addStretch(1)

    def _refresh_related_cards(self) -> None:
        clear_layout(self.related_layout)
        related = self.db.cards_for_document(str(self.doc["doc_id"])) if self.doc else []
        suggested = sum(1 for card in related if int(card.get("audit_hidden") or 0))
        heading_text = f"关联知识卡 · {len(related)}"
        if suggested:
            heading_text += f"（其中建议删除 {suggested}）"
        heading = QLabel(heading_text)
        heading.setObjectName("SectionTitle")
        self.related_layout.addWidget(heading)
        if not related:
            status = str((self.doc or {}).get("parse_status") or "PENDING").upper()
            if status == "FAILED":
                reason = (self.doc or {}).get("error_log") or "解析失败，请查看错误日志"
                message = f"尚未生成知识卡：{reason}"
            elif not (self.doc or {}).get("local_md_path"):
                message = "尚未完成文本转换，请先解析 PDF。"
            elif status == "LIGHT_PARSED":
                message = "仅完成了文本转换，尚未执行知识卡提炼。"
            else:
                message = (
                    "尚未生成知识卡：可能是候选片段被质量规则过滤，"
                    "或模型接口/配置异常。"
                )
            empty = QLabel(message)
            empty.setObjectName("Muted")
            empty.setWordWrap(True)
            self.related_layout.addWidget(empty)
        card_ids = [str(c.get("knw_id") or "") for c in related if c.get("knw_id")]
        provenance_map = (
            self.db.provenance_for_cards(card_ids)
            if hasattr(self.db, "provenance_for_cards") and card_ids
            else {}
        )
        doc_semi_model = ""
        if self.doc and self.workspace:
            try:
                from models.semi_auto import find_unfinished_run
                manifest = find_unfinished_run(self.workspace, [str(self.doc["doc_id"])])
                if manifest and manifest.get("agent_model"):
                    doc_semi_model = str(manifest["agent_model"]).strip()
            except Exception:
                pass

        for card in related:
            kid = str(card.get("knw_id") or "")
            prov = provenance_map.get(kid, {})
            model_name = get_card_model_name(card, prov, default_model=doc_semi_model)

            card_row = QHBoxLayout()
            card_row.setContentsMargins(0, 0, 0, 0)
            card_row.setSpacing(8)

            button = QPushButton(f"{card['knw_id']}  ·  {card.get('title_zh', '')}")
            button.setObjectName("Ghost")
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet("text-align: left; padding: 7px 9px;")
            button.clicked.connect(
                lambda _checked=False, target_kid=kid: KnowledgeDetailDialog(
                    self.db, self.workspace, target_kid, parent=self
                ).exec()
            )
            card_row.addWidget(button, 1)

            if model_name:
                badge = QPushButton(f"🤖 {model_name}")
                badge.setObjectName("ModelBadge")
                badge.setCursor(Qt.PointingHandCursor)
                badge.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
                badge.setToolTip(f"该卡片由模型 [{model_name}] 提炼生成（点击查看卡片详情）")
                badge.clicked.connect(
                    lambda _checked=False, target_kid=kid: KnowledgeDetailDialog(
                        self.db, self.workspace, target_kid, parent=self
                    ).exec()
                )
                card_row.addWidget(badge, 0, Qt.AlignVCenter)

            self.related_layout.addLayout(card_row)
        self._refresh_stepper()

    def _refresh_stepper(self) -> None:
        if not self.doc or not hasattr(self, "stepper"):
            return
        cards = self.db.cards_for_document(str(self.doc["doc_id"]))
        status = compute_document_pipeline_status(self.doc, cards)
        self.stepper.set_pipeline_status(status)
        self.action_bar.highlight_next_step(status.current_step, status.glow)

    def _on_synthesis_completed(self, doc_id: str, knw_id: str) -> None:
        self.doc = self.db.get_document(doc_id) or self.doc
        self._refresh_related_cards()

    def _handle_step_action(self, step: int) -> None:
        if not self.doc:
            return
        doc_id = str(self.doc["doc_id"])
        qdrant = getattr(self.pipeline, "qdrant", None)

        if step == 1:
            reply = QMessageBox.question(
                self,
                "确认从第 ① 步重新提取文字",
                f"从第 ① 步重新提取文字将重构文本排版并重置后续步骤（清除文献 [{doc_id}] 未核准的旧卡片，绿光/金光将熄灭）。\n\n是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            rollback_pipeline_from_step(self.db, self.workspace, doc_id, 1, qdrant=qdrant)
            self.doc = self.db.get_document(doc_id) or self.doc
            self._refresh_related_cards()
            self._run_reprocess("force")

        elif step == 2:
            reply = QMessageBox.question(
                self,
                "确认从第 ② 步重新提取关键信息",
                f"从第 ② 步重新分析关键信息将保留文本层，但重新扫描雷达与证据包（重置后续步骤并清除旧卡片，绿光/金光将熄灭）。\n\n是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            rollback_pipeline_from_step(self.db, self.workspace, doc_id, 2, qdrant=qdrant)
            self.doc = self.db.get_document(doc_id) or self.doc
            self._refresh_related_cards()
            self._run_reprocess("force")

        elif step == 3:
            fast_mode = self.fast_mode_combo.currentData() if hasattr(self, "fast_mode_combo") else "auto"
            if fast_mode == "manual":
                self._open_semi_auto()
            else:
                # 检查是否已配置 fast_llm API
                config = getattr(self.pipeline, "config", None) or {}
                fast_cfg = config.get("models", {}).get("fast_llm", {}) if isinstance(config, dict) else {}
                if not (fast_cfg.get("base_url") and fast_cfg.get("model")):
                    choice = QMessageBox.warning(
                        self,
                        "大模型未配置",
                        f"当前工作区尚未配置「快速提取大模型 (fast_llm)」的 API 地址或模型名称。\n\n"
                        f"【确定】：立即切换至「📋 网页版 AI 手动 (零 Token)」半自动任务台\n"
                        f"【取消】：留在当前界面（前往「系统设置」填写 API 后再试）",
                        QMessageBox.Ok | QMessageBox.Cancel,
                        QMessageBox.Ok,
                    )
                    if choice == QMessageBox.Ok:
                        if hasattr(self, "fast_mode_combo"):
                            idx = self.fast_mode_combo.findData("manual")
                            if idx >= 0:
                                self.fast_mode_combo.setCurrentIndex(idx)
                        self._open_semi_auto()
                    return
                self._run_reprocess("incremental")

        elif step == 4:
            cards = self.db.cards_for_document(doc_id)
            if cards:
                QMessageBox.information(
                    self,
                    "第 ④ 步：写入数据库",
                    f"文献 [{doc_id}] 的 {len(cards)} 张知识卡已存在于数据库中（状态正常，冒绿光中）。\n"
                    f"如需查看细节，请参阅下方关联知识卡列表。",
                )
            else:
                self._open_semi_auto()

        elif step == 5:
            cards = self.db.cards_for_document(doc_id)
            l2_cards = [
                c for c in cards
                if str(c.get("card_level") or "L2").upper() == "L2"
                and str(c.get("card_type") or "").upper() != "MASTER"
            ]
            if not l2_cards:
                QMessageBox.warning(
                    self,
                    "提示",
                    f"文献 [{doc_id}] 目前尚无已入库的 L2 细节卡片。\n"
                    f"请先完成前 ④ 步（冒绿光）后再执行第 ⑤ 步 AI 深度归纳。",
                )
                return
            dialog = DeepSynthesisDialog(
                self.db,
                self.workspace,
                doc_id,
                parent=self,
                pipeline=self.pipeline,
            )
            deep_mode = self.deep_mode_combo.currentData() if hasattr(self, "deep_mode_combo") else "auto"
            if deep_mode == "manual":
                dialog.tabs.setCurrentIndex(0)  # Tab 0: 网页版 AI (零 Token·推荐)
            else:
                dialog.tabs.setCurrentIndex(1)  # Tab 1: Codex Agent CLI Sandbox
            dialog.synthesis_completed.connect(self._on_synthesis_completed)
            dialog.exec()

    def _change_document_type(self) -> None:
        if not self.doc:
            return
        value = str(self.type_combo.currentData() or "PAP")
        try:
            self.db.upsert_document({"doc_id": self.doc["doc_id"], "doc_type": value})
            self.doc["doc_type"] = value
            if hasattr(self, "reprocess_log"):
                self.reprocess_log.appendPlainText(f"文献类型已更新为 {doc_type_label(value)}")
        except Exception as exc:
            QMessageBox.warning(self, "更新失败", str(exc))

    def _open_semi_auto(self) -> None:
        if self.pipeline is None or not self.doc:
            return
        dialog = SemiAutoBatchDialog(
            self.db,
            self.workspace,
            self.pipeline,
            [str(self.doc["doc_id"])],
            mode="force",
            config=getattr(self.pipeline, "config", None),
            parent=self,
        )
        dialog.exec()
        self.doc = self.db.get_document(str(self.doc["doc_id"])) or self.doc
        self._refresh_related_cards()

    def _run_reprocess(self, mode: str) -> None:
        if self.reprocess_manager is None:
            return
        if self._worker is not None and self._worker.isRunning():
            return
        if mode == "force":
            tokens = self.reprocess_manager.estimate_force_tokens([self.doc["doc_id"]])
            answer = QMessageBox.question(
                self,
                "确认强制重新提炼",
                f"强制重新提炼这篇文献预计将消耗约 {tokens:,} 输入 Token"
                "（您手动编辑过的卡片将被安全保留），是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        elif mode == "incremental":
            tokens = self.reprocess_manager.estimate_incremental_tokens(
                [self.doc["doc_id"]]
            )
            answer = QMessageBox.question(
                self,
                "确认智能补提",
                f"这篇文献的未提炼证据包预计最多消耗约 {tokens:,} 输入 Token。\n"
                "已提炼片段会自动跳过，目录/安装/引用等低价值片段也会过滤；"
                "4 路并行运行，可随时停止。是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        if hasattr(self, "action_bar"):
            self.action_bar.setEnabled(False)
        if hasattr(self, "incremental_button"):
            self.incremental_button.setEnabled(False)
        if hasattr(self, "force_button"):
            self.force_button.setEnabled(False)
        self.stop_button.setVisible(True)
        self.stop_button.setEnabled(True)
        self.reprocess_progress.setVisible(True)
        self.reprocess_progress.setValue(0)
        self.reprocess_eta.setVisible(True)
        self._reprocess_eta.reset()
        self.reprocess_eta.setText("已用 不足 1 秒 · 预计剩余 计算中…")
        self.reprocess_log.setVisible(True)
        self.reprocess_log.clear()
        self.reprocess_log.appendPlainText(
            "开始智能补提…" if mode == "incremental" else "开始强制重新提炼…"
        )
        worker = ReprocessWorker(
            self.reprocess_manager, mode, [self.doc["doc_id"]]
        )
        worker.progress_event.connect(self._on_reprocess_progress)
        worker.finished_result.connect(self._on_reprocess_done)
        worker.failed.connect(self._on_reprocess_failed)
        worker.finished.connect(lambda: self._cleanup_reprocess(worker))
        self._worker = worker
        worker.start()

    def _stop_reprocess(self) -> None:
        worker = self._worker
        if worker is None or not worker.isRunning():
            return
        worker.cancel()
        self.stop_button.setEnabled(False)
        self.reprocess_log.appendPlainText(
            "正在停止，等待当前模型请求结束后保存进度…"
        )

    def _export_audit_bundle(self) -> None:
        if not self.doc:
            return
        try:
            result = export_audit_bundle(
                self.db,
                self.workspace,
                str(self.doc["doc_id"]),
                include_history=self.audit_history_check.isChecked(),
                include_source=self.audit_source_check.isChecked(),
                include_evidence=self.audit_evidence_check.isChecked(),
                coverage_audit=self.audit_coverage_check.isChecked(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        QMessageBox.information(
            self,
            "AI 校对包已导出",
            f"已导出 {result.card_count} 张卡片，共 {result.part_count} 个分片。\n\n"
            f"目录：\n{result.export_dir}\n\n"
            "请把 PDF、report.md（或全部分片）和 audit_prompt.md 一起交给外部 AI。",
        )
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.export_dir)))

    def _import_audit_result(self) -> None:
        if not self.doc:
            return
        dialog = AuditImportDialog(
            self.db,
            str(self.doc["doc_id"]),
            pipeline=self.pipeline,
            parent=self,
        )
        if dialog.exec():
            self.doc = self.db.get_document(str(self.doc["doc_id"])) or self.doc
            self._refresh_related_cards()

    def _clipboard_node(self) -> EndpointNode:
        pool = getattr(getattr(self.pipeline, "gateway", None), "pool", None)
        node = pool.node_for_role("fast_llm") if pool is not None else None
        if node is not None:
            return node
        return EndpointNode(
            node_id="semi-auto-clipboard",
            name="半自动剪贴板",
            node_type="SEMI_AUTO_CLIPBOARD",
            capabilities=["TEXT_LLM"],
            max_input_tokens=5000,
            max_output_tokens=4000,
            max_items_per_batch=6,
        )

    def _copy_task_packet(self) -> None:
        if self.pipeline is None or not self.doc:
            return
        local = self.doc.get("local_pdf_path")
        if not local:
            QMessageBox.information(self, "无法生成任务包", "这篇文献还没有本地 PDF。")
            return
        pdf = Path(str(local))
        if not pdf.is_absolute():
            pdf = self.workspace / pdf
        bundles_path = pdf.parent / "bundles.json"
        if not bundles_path.is_file():
            QMessageBox.information(self, "无法生成任务包", "尚未生成证据包，请先解析文献。")
            return
        try:
            raw = json.loads(bundles_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "读取失败", str(exc))
            return
        stored = self.db.bundle_hashes_for_document(str(self.doc["doc_id"]))
        items: list[DistilledItem] = []
        bundles_by_item: dict[str, ContextBundle] = {}
        for index, item in enumerate(raw):
            try:
                bundle = ContextBundle.model_validate(item)
            except Exception:
                continue
            if bundle_hash(bundle) in stored:
                continue
            item_id = f"{self.doc['doc_id']}-{index}"
            items.append(
                DistilledItem(
                    item_id=item_id,
                    text=bundle.prompt_markdown(),
                    metadata={"bundle": item},
                )
            )
            bundles_by_item[item_id] = bundle
        if not items:
            QMessageBox.information(self, "没有待处理片段", "这篇文献没有尚未提炼的证据包。")
            return
        system = CARD_SYSTEM_PROMPT.replace("{doc_id}", str(self.doc["doc_id"]))
        if str(self.doc.get("doc_type") or "").upper() in ("MAN", "BOK"):
            system += "\n\n" + MANUAL_MODE_INSTRUCTION
        node = self._clipboard_node()
        try:
            packer = DynamicBatchPacker(system_prompt=system)
            service = ClipboardBatchService(packer)
            packet = service.build_packet(
                items,
                node,
                item_bundles=bundles_by_item,
                system_prompt=system,
            )
        except Exception as exc:
            QMessageBox.warning(self, "任务包生成失败", str(exc))
            return
        self._clipboard_packet = packet
        self._clipboard_service = service
        _copy(packet.prompt)
        QMessageBox.information(
            self,
            "任务包已复制",
            f"已按节点「{node.name}」容量装入 {packet.batch.item_count} 个片段，"
            f"约 {packet.batch.input_tokens:,} 输入 Token。\n\n"
            "已复制到剪贴板，发给网页版 AI 后，将回复 JSON 复制回来，"
            "再点击「导入剪贴板回复」。",
        )

    def _import_clipboard_reply(self) -> None:
        if self.pipeline is None or not self.doc or self._clipboard_packet is None:
            QMessageBox.information(
                self, "没有待导入任务", "请先生成并复制一个任务包，再导入对应回复。"
            )
            return
        text = QGuiApplication.clipboard().text()
        if not text.strip():
            QMessageBox.information(self, "剪贴板为空", "请先复制 AI 返回的 JSON。")
            return
        try:
            service = self._clipboard_service or ClipboardBatchService()
            result = service.apply_reply(
                self.pipeline, self.doc, self._clipboard_packet, text
            )
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        self._clipboard_packet = None
        self._clipboard_service = None
        self.doc = self.db.get_document(str(self.doc["doc_id"])) or self.doc
        self._refresh_related_cards()
        QMessageBox.information(
            self,
            "导入完成",
            f"已保存 {result['stored']} 张卡，当前关联 {result['cards']} 张。\n"
            "可再次点击「生成并复制任务包」处理下一批。",
        )

    def _cancel_worker_on_close(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()

    def accept(self) -> None:
        self._cancel_worker_on_close()
        super().accept()

    def reject(self) -> None:
        self._cancel_worker_on_close()
        super().reject()

    def closeEvent(self, event) -> None:
        self._cancel_worker_on_close()
        super().closeEvent(event)

    def _on_reprocess_progress(self, label: str, fraction: float) -> None:
        self.reprocess_progress.setValue(
            max(0, min(100, int(round(float(fraction) * 100))))
        )
        self.reprocess_eta.setText(
            self._reprocess_eta.summary(float(fraction), label)
        )
        self.reprocess_log.appendPlainText(label)

    def _on_reprocess_done(self, result: dict) -> None:
        if hasattr(self, "action_bar"):
            self.action_bar.setEnabled(True)
        if hasattr(self, "incremental_button"):
            self.incremental_button.setEnabled(True)
        if hasattr(self, "force_button"):
            self.force_button.setEnabled(True)
        self.stop_button.setVisible(False)
        if result.get("cancelled"):
            self.reprocess_eta.setText(self._reprocess_eta.stopped_text())
        else:
            self.reprocess_progress.setValue(100)
            self.reprocess_eta.setText(self._reprocess_eta.finish_text())
        self._worker = None
        self.doc = self.db.get_document(str(self.doc["doc_id"])) or self.doc
        self._refresh_related_cards()
        if result.get("cancelled"):
            message = (
                f"已停止：新卡 {result.get('extracted', 0)} 张；"
                "已完成部分已保存，可再次增量补提继续。"
            )
        else:
            message = (
                f"完成：新卡 {result.get('extracted', 0)} 张，"
                f"跳过已提炼片段 {result.get('skipped', 0)} 个，"
                f"过滤低价值片段 {result.get('skipped_low_value', 0)} 个。"
            )
        self.reprocess_log.appendPlainText(message)

    def _on_reprocess_failed(self, message: str) -> None:
        if hasattr(self, "action_bar"):
            self.action_bar.setEnabled(True)
        if hasattr(self, "incremental_button"):
            self.incremental_button.setEnabled(True)
        if hasattr(self, "force_button"):
            self.force_button.setEnabled(True)
        self.stop_button.setVisible(False)
        self.reprocess_progress.setVisible(False)
        self.reprocess_eta.setVisible(False)
        self.reprocess_log.appendPlainText(f"失败：{message}")
        self._worker = None

    def _cleanup_reprocess(self, worker: ReprocessWorker) -> None:
        worker.deleteLater()

    def _open_pdf(self) -> None:
        if self.doc and self.doc.get("local_pdf_path"):
            path = Path(self.doc["local_pdf_path"])
            _open_file(path if path.is_absolute() else self.workspace / path)

    def _open_folder(self) -> None:
        if not self.doc:
            return
        value = self.doc.get("local_pdf_path")
        folder = (
            (Path(value) if Path(value).is_absolute() else self.workspace / value).parent
            if value
            else self.workspace / "storage" / self.doc["doc_id"]
        )
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


class KeywordDetailDialog(QDialog):
    def __init__(
        self,
        db: SQLiteManager,
        workspace: str | Path,
        kw_id: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.workspace = Path(workspace)
        keyword = db.get_keyword(kw_id)
        self.setWindowTitle(f"{kw_id} · AtomicCalculationDB")
        self.resize(720, 620)
        self.setMinimumSize(620, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        if not keyword:
            layout.addWidget(QLabel(f"找不到关键词：{kw_id}"))
            close = QPushButton("关闭")
            close.setObjectName("Pill")
            close.clicked.connect(self.accept)
            layout.addWidget(close, 0, Qt.AlignRight)
            return
        header, close = _header_row(
            [make_chip(keyword["kw_id"]), make_type_chip("受控关键词", kind="keyword")]
        )
        close.clicked.connect(self.accept)
        layout.addLayout(header)
        term = QLabel(keyword.get("term_en") or "")
        term.setObjectName("DialogTitle")
        layout.addWidget(term)
        if keyword.get("term_zh"):
            zh = QLabel(keyword["term_zh"])
            zh.setObjectName("Muted")
            layout.addWidget(zh)
        definition = QLabel(keyword.get("definition") or "（暂无释义）")
        definition.setWordWrap(True)
        definition.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(definition)
        synonyms = keyword.get("synonyms_list") or []
        if synonyms:
            row = QHBoxLayout()
            row.addWidget(QLabel("同义词"))
            for item in synonyms:
                row.addWidget(make_chip(str(item)))
            row.addStretch(1)
            layout.addLayout(row)
        cards = db.cards_for_keyword(keyword["kw_id"])
        heading = QLabel(f"关联知识卡 · {len(cards)}")
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        scroll, body = _scroll_page(self)
        layout.addWidget(scroll, 1)
        for card in cards:
            button = QPushButton(
                f"{card['knw_id']}  ·  {card.get('title_zh', '')}\n"
                f"{', '.join(card.get('supporting_doc_ids_list') or [])}"
            )
            button.setObjectName("Ghost")
            button.setStyleSheet("text-align: left; padding: 7px 9px;")
            button.clicked.connect(
                lambda _checked=False, kid=card["knw_id"]: KnowledgeDetailDialog(
                    self.db, self.workspace, kid, parent=self
                ).exec()
            )
            body.addWidget(button)
        body.addStretch(1)
