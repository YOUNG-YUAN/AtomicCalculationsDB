"""Agent Task Workbench Dialog for AtomicCalculationsDB.

Provides an interactive GUI for managing local autonomous AI agent tasks (Codex,
Claude Code, Antigravity, etc.), manual prompt copying, table summary extraction,
and two-button reply ingestion (file import & text paste).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from sync_mcp.agent_task_packager import AgentTaskPackager
from ui.semi_auto_dialog import PRESET_AGENT_MODELS, get_default_agent_model, save_default_agent_model

log = logging.getLogger(__name__)


class AgentPasteReplyDialog(QDialog):
    """Modal dialog for pasting agent reply JSON."""

    def __init__(
        self,
        title: str = "粘贴 Agent 回复 JSON",
        placeholder: str = "在此粘贴 AI Agent 或大模型的完整 JSON 回复...",
        model: str = "",
        config: Any = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.config = config
        current_model = (model or "").strip() or get_default_agent_model(self.config)

        self.setWindowTitle(title)
        self.resize(600, 460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        lbl = QLabel("请粘贴完整 JSON 回复：")
        lbl.setStyleSheet("font-weight: 500; font-size: 13px; color: #1f2937;")
        top_row.addWidget(lbl)
        top_row.addStretch(1)

        model_lbl = QLabel("处理模型/Agent:")
        model_lbl.setStyleSheet("font-size: 12px; color: #4b5563;")
        top_row.addWidget(model_lbl)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        items = list(PRESET_AGENT_MODELS)
        if current_model and current_model not in items:
            items.insert(0, current_model)
        self.model_combo.addItems(items)
        self.model_combo.setCurrentText(current_model)
        self.model_combo.setMinimumWidth(160)
        top_row.addWidget(self.model_combo)
        layout.addLayout(top_row)

        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText(placeholder)
        self.text_edit.setStyleSheet(
            "QTextEdit {"
            " border: 1.5px solid #1a73e8;"
            " border-radius: 8px;"
            " padding: 8px;"
            " font-family: monospace;"
            " font-size: 12px;"
            " background: #ffffff;"
            "}"
        )
        layout.addWidget(self.text_edit, 1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch(1)
        self.ok_btn = QPushButton("确定导入")
        self.ok_btn.setFixedSize(96, 32)
        self.ok_btn.setStyleSheet(
            "QPushButton {"
            " background-color: #1a73e8; color: #ffffff;"
            " border: none; border-radius: 16px;"
            " font-weight: bold; font-size: 13px;"
            "}"
            "QPushButton:hover { background-color: #1557b0; }"
        )
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setFixedSize(80, 32)
        self.cancel_btn.setStyleSheet(
            "QPushButton {"
            " background-color: #f1f3f4; color: #3c4043;"
            " border: 1px solid #dadce0; border-radius: 16px;"
            " font-size: 13px;"
            "}"
            "QPushButton:hover { background-color: #e8eaed; }"
        )
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.ok_btn)
        layout.addLayout(btn_layout)

        self.ok_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)

    def get_text(self) -> str:
        return self.text_edit.toPlainText().strip()

    def get_model_name(self) -> str:
        return self.model_combo.currentText().strip()


class AgentTaskDialog(QDialog):
    """Workbench dialog for managing Agent task packages, table summaries, and reply imports."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        db: Any,
        workspace: str | Path,
        pipeline: Any = None,
        doc_id: str,
    ):
        super().__init__(parent)
        self.db = db
        self.workspace = Path(workspace)
        self.pipeline = pipeline
        self.doc_id = str(doc_id or "").strip()
        self.packager = AgentTaskPackager(self.db, self.workspace, pipeline=self.pipeline)
        self.current_agent_model = get_default_agent_model(getattr(self.pipeline, "config", None))

        self.setWindowTitle(f"💻 本地 Agent / CLI 任务工作台 — {self.doc_id}")
        self.resize(1000, 720)
        self.setMinimumSize(850, 600)

        self.task_dir = self.workspace / "agent_tasks" / self.doc_id

        # Ensure task bundle exists
        try:
            if not self.task_dir.is_dir() or not (self.task_dir / "items.json").is_file():
                self.packager.export_task_bundle(self.doc_id)
        except Exception as exc:
            log.warning("Export task bundle initial warning: %s", exc)

        self._build_ui()
        self._refresh_all()

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 14, 16, 14)
        main_layout.setSpacing(10)

        # 1. Header banner
        header_frame = QFrame()
        header_frame.setStyleSheet(
            "QFrame {"
            " background: #f8fafc;"
            " border: 1px solid #e2e8f0;"
            " border-radius: 8px;"
            " padding: 10px;"
            "}"
        )
        h_layout = QHBoxLayout(header_frame)
        h_layout.setContentsMargins(8, 6, 8, 6)

        info_vbox = QVBoxLayout()
        info_vbox.setSpacing(4)
        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #0f172a;")
        self.title_label.setWordWrap(True)
        info_vbox.addWidget(self.title_label)

        self.meta_label = QLabel()
        self.meta_label.setStyleSheet("font-size: 11px; color: #64748b;")
        info_vbox.addWidget(self.meta_label)
        h_layout.addLayout(info_vbox, 1)

        self.status_badge = QLabel()
        self.status_badge.setStyleSheet(
            "padding: 4px 10px; border-radius: 6px; font-weight: bold; font-size: 12px; background: #e0f2fe; color: #0369a1;"
        )
        h_layout.addWidget(self.status_badge)
        main_layout.addWidget(header_frame)

        # 2. Control Card: Agent Folder & CLI commands
        ctrl_group = QGroupBox("📂 Agent 任务包与工作路径")
        ctrl_group.setStyleSheet("QGroupBox { font-weight: bold; color: #1e293b; }")
        ctrl_vbox = QVBoxLayout(ctrl_group)
        ctrl_vbox.setSpacing(8)

        # Row 1: Folder Path
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("任务包目录:"))
        self.path_edit = QLineEdit(str(self.task_dir.resolve()))
        self.path_edit.setReadOnly(True)
        self.path_edit.setStyleSheet("background: #f1f5f9; color: #334155;")
        row1.addWidget(self.path_edit, 1)

        copy_path_btn = QPushButton("📋 复制路径")
        copy_path_btn.clicked.connect(self._copy_task_dir)
        row1.addWidget(copy_path_btn)

        open_folder_btn = QPushButton("📁 打开文件夹")
        open_folder_btn.clicked.connect(self._open_task_dir)
        row1.addWidget(open_folder_btn)

        regen_btn = QPushButton("🔄 重新生成")
        regen_btn.setToolTip("强制重新从文献 Evidence 证据包导出 Agent 任务目录")
        regen_btn.clicked.connect(self._reexport_bundle)
        row1.addWidget(regen_btn)
        ctrl_vbox.addLayout(row1)

        # Row 2: CLI Command
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("CLI 命令行:"))
        cli_cmd = f"python -m app_core agent execute {self.doc_id}"
        self.cli_edit = QLineEdit(cli_cmd)
        self.cli_edit.setReadOnly(True)
        self.cli_edit.setStyleSheet("background: #f1f5f9; color: #0284c7; font-family: monospace;")
        row2.addWidget(self.cli_edit, 1)

        copy_cli_btn = QPushButton("📋 复制命令")
        copy_cli_btn.clicked.connect(lambda: self._copy_to_clipboard(self.cli_edit.text(), "CLI 命令已复制"))
        row2.addWidget(copy_cli_btn)
        ctrl_vbox.addLayout(row2)

        # Row 3: Agent Prompt Instruction
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("任务说明与提示词:"))
        copy_prompt_btn = QPushButton("📋 复制完整 Agent 提示词")
        copy_prompt_btn.setStyleSheet("font-weight: bold; color: #1d4ed8;")
        copy_prompt_btn.clicked.connect(self._copy_agent_instructions)
        row3.addWidget(copy_prompt_btn)
        row3.addStretch(1)
        ctrl_vbox.addLayout(row3)

        main_layout.addWidget(ctrl_group)

        # 3. Main Tabs
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #cbd5e1; border-radius: 6px; background: #ffffff; }"
            "QTabBar::tab { padding: 6px 16px; font-weight: 500; font-size: 12px; }"
            "QTabBar::tab:selected { background: #ffffff; border-bottom: 2px solid #2563eb; color: #2563eb; }"
        )

        # Tab 1: Question Items (items.json)
        self.items_tab = QWidget()
        self._build_items_tab()
        self.tabs.addTab(self.items_tab, "📝 物理问题条目 (items.json)")

        # Tab 2: Tables & Table Summaries (tables/)
        self.tables_tab = QWidget()
        self._build_tables_tab()
        self.tabs.addTab(self.tables_tab, "📊 数据表格与总结 (tables/)")

        # Tab 3: Raw Files Explorer
        self.files_tab = QWidget()
        self._build_files_tab()
        self.tabs.addTab(self.files_tab, "📄 任务包原始文件")

        main_layout.addWidget(self.tabs, 1)

        # 4. Bottom status & close
        bottom_row = QHBoxLayout()
        self.bottom_status = QLabel("就绪")
        self.bottom_status.setStyleSheet("color: #475569; font-size: 12px;")
        bottom_row.addWidget(self.bottom_status, 1)

        close_btn = QPushButton("关闭")
        close_btn.setFixedSize(90, 32)
        close_btn.clicked.connect(self.accept)
        bottom_row.addWidget(close_btn)
        main_layout.addLayout(bottom_row)

    # ---------------------------------------------------------------- Tab 1: Items
    def _build_items_tab(self) -> None:
        layout = QVBoxLayout(self.items_tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        desc = QLabel(
            "说明：以下为从论文中提取的物理问题与上下文条目。条目可单独或整体发给本地 Agent / 网页大模型处理。"
            "提取完成后，使用右侧按键将 Agent 生成的 JSON 回复导入系统生成知识卡。"
        )
        desc.setStyleSheet("color: #475569; font-size: 11px;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        self.items_table = QTableWidget()
        self.items_table.setColumnCount(5)
        self.items_table.setHorizontalHeaderLabels(["条目编号", "章节位置", "页码", "焦点陈述 (Focal Sentence)", "关联表格"])
        self.items_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.items_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.items_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.items_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.items_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.items_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.items_table.setAlternatingRowColors(True)
        layout.addWidget(self.items_table, 1)

        # Items action bar with Image 2 buttons!
        bar = QHBoxLayout()
        copy_all_btn = QPushButton("📋 复制全部条目 JSON")
        copy_all_btn.clicked.connect(self._copy_all_items_json)
        bar.addWidget(copy_all_btn)

        copy_sel_btn = QPushButton("📋 复制选中条目 JSON")
        copy_sel_btn.clicked.connect(self._copy_selected_item_json)
        bar.addWidget(copy_sel_btn)

        bar.addStretch(1)

        # Two buttons matching Image 2
        import_items_file_btn = QPushButton("📥 导入回复文件")
        import_items_file_btn.setObjectName("Pill")
        import_items_file_btn.setStyleSheet(
            "QPushButton {"
            " background: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd;"
            " border-radius: 4px; padding: 5px 12px; font-weight: bold;"
            "}"
            "QPushButton:hover { background: #bae6fd; }"
        )
        import_items_file_btn.clicked.connect(self._import_items_file)
        bar.addWidget(import_items_file_btn)

        paste_items_reply_btn = QPushButton("📝 粘贴回复文本")
        paste_items_reply_btn.setObjectName("Ghost")
        paste_items_reply_btn.setStyleSheet(
            "QPushButton {"
            " background: #f8fafc; color: #2563eb; border: 1px solid #bfdbfe;"
            " border-radius: 4px; padding: 5px 12px; font-weight: bold;"
            "}"
            "QPushButton:hover { background: #eff6ff; }"
        )
        paste_items_reply_btn.clicked.connect(self._paste_items_reply)
        bar.addWidget(paste_items_reply_btn)

        layout.addLayout(bar)

    # ---------------------------------------------------------------- Tab 2: Tables
    def _build_tables_tab(self) -> None:
        layout = QVBoxLayout(self.tables_tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        desc = QLabel(
            "说明：表格是论文计算或实验数据的核心载体。AI 可根据表格大概信息（表头、列标签及大致内容）"
            "写出总结，入库为【数据类型知识卡 (DATA Cards)】。请使用「复制表格总结提示词」发给 AI，并使用导入按键导入回复。"
        )
        desc.setStyleSheet("color: #475569; font-size: 11px;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        splitter = QSplitter(Qt.Horizontal)

        # Left list: Tables
        left_widget = QWidget()
        left_vbox = QVBoxLayout(left_widget)
        left_vbox.setContentsMargins(0, 0, 0, 0)
        left_vbox.addWidget(QLabel("数据表格清单:"))
        self.tables_list = QListWidget()
        self.tables_list.currentRowChanged.connect(self._on_table_selected)
        left_vbox.addWidget(self.tables_list, 1)
        splitter.addWidget(left_widget)

        # Right preview: Table markdown skeleton
        right_widget = QWidget()
        right_vbox = QVBoxLayout(right_widget)
        right_vbox.setContentsMargins(0, 0, 0, 0)
        right_vbox.addWidget(QLabel("表格骨架预览 (Skeletonized Preview):"))
        self.table_preview = QTextEdit()
        self.table_preview.setReadOnly(True)
        self.table_preview.setStyleSheet("font-family: monospace; font-size: 11px; background: #fafafa;")
        right_vbox.addWidget(self.table_preview, 1)
        splitter.addWidget(right_widget)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        # Tables Action Bar with Image 2 buttons!
        bar = QHBoxLayout()
        copy_tbl_prompt_btn = QPushButton("📋 复制表格总结提示词")
        copy_tbl_prompt_btn.setStyleSheet("font-weight: bold; color: #15803d;")
        copy_tbl_prompt_btn.clicked.connect(self._copy_table_summary_prompt)
        bar.addWidget(copy_tbl_prompt_btn)

        copy_tbl_md_btn = QPushButton("📋 复制当前表格 Markdown")
        copy_tbl_md_btn.clicked.connect(self._copy_current_table_md)
        bar.addWidget(copy_tbl_md_btn)

        bar.addStretch(1)

        # Two buttons matching Image 2
        import_tbl_file_btn = QPushButton("📥 导入回复文件")
        import_tbl_file_btn.setObjectName("Pill")
        import_tbl_file_btn.setStyleSheet(
            "QPushButton {"
            " background: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd;"
            " border-radius: 4px; padding: 5px 12px; font-weight: bold;"
            "}"
            "QPushButton:hover { background: #bae6fd; }"
        )
        import_tbl_file_btn.clicked.connect(self._import_tables_file)
        bar.addWidget(import_tbl_file_btn)

        paste_tbl_reply_btn = QPushButton("📝 粘贴回复文本")
        paste_tbl_reply_btn.setObjectName("Ghost")
        paste_tbl_reply_btn.setStyleSheet(
            "QPushButton {"
            " background: #f8fafc; color: #2563eb; border: 1px solid #bfdbfe;"
            " border-radius: 4px; padding: 5px 12px; font-weight: bold;"
            "}"
            "QPushButton:hover { background: #eff6ff; }"
        )
        paste_tbl_reply_btn.clicked.connect(self._paste_tables_reply)
        bar.addWidget(paste_tbl_reply_btn)

        layout.addLayout(bar)

    # ---------------------------------------------------------------- Tab 3: Raw Files
    def _build_files_tab(self) -> None:
        layout = QVBoxLayout(self.files_tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        splitter = QSplitter(Qt.Horizontal)

        left_widget = QWidget()
        left_vbox = QVBoxLayout(left_widget)
        left_vbox.setContentsMargins(0, 0, 0, 0)
        left_vbox.addWidget(QLabel("任务包文件列表:"))
        self.files_list = QListWidget()
        self.files_list.currentRowChanged.connect(self._on_file_selected)
        left_vbox.addWidget(self.files_list, 1)
        splitter.addWidget(left_widget)

        right_widget = QWidget()
        right_vbox = QVBoxLayout(right_widget)
        right_vbox.setContentsMargins(0, 0, 0, 0)
        right_vbox.addWidget(QLabel("文件内容预览:"))
        self.file_preview = QTextEdit()
        self.file_preview.setReadOnly(True)
        self.file_preview.setStyleSheet("font-family: monospace; font-size: 11px; background: #fafafa;")
        right_vbox.addWidget(self.file_preview, 1)
        splitter.addWidget(right_widget)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        bar = QHBoxLayout()
        copy_file_content_btn = QPushButton("📋 复制选中文件内容")
        copy_file_content_btn.clicked.connect(self._copy_selected_file_content)
        bar.addWidget(copy_file_content_btn)

        copy_file_path_btn = QPushButton("📋 复制选中文件路径")
        copy_file_path_btn.clicked.connect(self._copy_selected_file_path)
        bar.addWidget(copy_file_path_btn)

        bar.addStretch(1)
        layout.addLayout(bar)

    # ---------------------------------------------------------------- Data Loading
    def _refresh_all(self) -> None:
        doc = self.db.get_document(self.doc_id) if self.db else None
        if not doc:
            self.title_label.setText(f"文献 {self.doc_id}（未在数据库中找到）")
            return

        title = str(doc.get("title") or "—")
        self.title_label.setText(f"[{self.doc_id}] {title}")

        year = str(doc.get("pub_year") or "—")
        journal = str(doc.get("journal") or "—")
        authors = str(doc.get("authors") or "—")
        if len(authors) > 60:
            authors = authors[:57] + "..."
        parse_status = str(doc.get("parse_status") or "PENDING")

        cards = self.db.cards_for_document(self.doc_id) if hasattr(self.db, "cards_for_document") else []
        data_cards = [c for c in cards if "DATA" in str(c.get("structured_details_json") or "").upper()]

        self.meta_label.setText(f"年份: {year} | 期刊: {journal} | 作者: {authors} | 知识卡: {len(cards)} 张 (数据表卡: {len(data_cards)} 张)")
        self.status_badge.setText(parse_status)

        # Refresh items
        self._populate_items()
        # Refresh tables
        self._populate_tables()
        # Refresh raw files
        self._populate_files()

        self.bottom_status.setText(f"已就绪 | 任务包: {self.task_dir.name} | 已入库 {len(cards)} 张知识卡片")

    def _populate_items(self) -> None:
        items_file = self.task_dir / "items.json"
        items = []
        if items_file.is_file():
            try:
                items = json.loads(items_file.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Failed to parse items.json: %s", exc)

        self.items_table.setRowCount(0)
        for row, item in enumerate(items):
            self.items_table.insertRow(row)
            iid = str(item.get("item_id") or f"item-{row}")
            sec = str(item.get("section_path") or "—")
            page = str(item.get("page_num") or "—")
            focal = str(item.get("focal_sentence") or "—")
            tbls = ", ".join(item.get("table_refs") or []) or "—"

            item_id_w = QTableWidgetItem(iid)
            item_id_w.setData(Qt.UserRole, item)
            self.items_table.setItem(row, 0, item_id_w)
            self.items_table.setItem(row, 1, QTableWidgetItem(sec))
            self.items_table.setItem(row, 2, QTableWidgetItem(page))
            focal_w = QTableWidgetItem(focal)
            focal_w.setToolTip(focal)
            self.items_table.setItem(row, 3, focal_w)
            self.items_table.setItem(row, 4, QTableWidgetItem(tbls))

    def _populate_tables(self) -> None:
        tables_dir = self.task_dir / "tables"
        self.tables_list.clear()
        self.table_preview.clear()

        if not tables_dir.is_dir():
            return

        table_files = sorted(tables_dir.glob("*.md"))
        for tf in table_files:
            item = QListWidgetItem(f"📊 {tf.name}")
            item.setData(Qt.UserRole, str(tf))
            self.tables_list.addItem(item)

        if self.tables_list.count() > 0:
            self.tables_list.setCurrentRow(0)

    def _on_table_selected(self, row: int) -> None:
        item = self.tables_list.item(row)
        if not item:
            self.table_preview.clear()
            return
        path_str = item.data(Qt.UserRole)
        p = Path(path_str)
        if p.is_file():
            try:
                self.table_preview.setPlainText(p.read_text(encoding="utf-8"))
            except Exception as exc:
                self.table_preview.setPlainText(f"读取失败: {exc}")

    def _populate_files(self) -> None:
        self.files_list.clear()
        self.file_preview.clear()

        if not self.task_dir.is_dir():
            return

        # List files in task_dir
        for f in sorted(self.task_dir.glob("*.*")):
            item = QListWidgetItem(f"📄 {f.name}")
            item.setData(Qt.UserRole, str(f))
            self.files_list.addItem(item)

        tables_dir = self.task_dir / "tables"
        if tables_dir.is_dir():
            for f in sorted(tables_dir.glob("*.md")):
                item = QListWidgetItem(f"📊 tables/{f.name}")
                item.setData(Qt.UserRole, str(f))
                self.files_list.addItem(item)

        images_dir = self.task_dir / "images"
        if images_dir.is_dir():
            for f in sorted(images_dir.glob("*.*")):
                item = QListWidgetItem(f"🖼 images/{f.name}")
                item.setData(Qt.UserRole, str(f))
                self.files_list.addItem(item)

        if self.files_list.count() > 0:
            self.files_list.setCurrentRow(0)

    def _on_file_selected(self, row: int) -> None:
        item = self.files_list.item(row)
        if not item:
            self.file_preview.clear()
            return
        p = Path(item.data(Qt.UserRole))
        if p.is_file():
            if p.suffix.lower() in (".png", ".jpg", ".jpeg"):
                self.file_preview.setPlainText(f"[图片文件: {p.name}] 路径: {p.resolve()}")
            else:
                try:
                    self.file_preview.setPlainText(p.read_text(encoding="utf-8"))
                except Exception as exc:
                    self.file_preview.setPlainText(f"无法读取文件: {exc}")

    # ---------------------------------------------------------------- Clipboard & Actions
    def _copy_to_clipboard(self, text: str, toast_msg: str = "已复制到剪贴板") -> None:
        QGuiApplication.clipboard().setText(text)
        self.bottom_status.setText(toast_msg)

    def _copy_task_dir(self) -> None:
        self._copy_to_clipboard(str(self.task_dir.resolve()), "任务包路径已复制到剪贴板")

    def _open_task_dir(self) -> None:
        if not self.task_dir.is_dir():
            self.task_dir.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(self.task_dir.resolve()))
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.task_dir.resolve())))
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", f"无法打开任务文件夹: {exc}")

    def _reexport_bundle(self) -> None:
        reply = QMessageBox.question(
            self,
            "重新生成任务包",
            f"确定要重新导出文献 [{self.doc_id}] 的 Agent 任务包吗？\n这将覆盖现有的 items.json 和 tables/ 文件。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            try:
                res = self.packager.export_task_bundle(self.doc_id, force=True)
                QMessageBox.information(
                    self,
                    "导出成功",
                    f"Agent 任务包导出成功！\n条目数: {res['item_count']} | 表格数: {res['table_count']}",
                )
                self._refresh_all()
            except Exception as exc:
                QMessageBox.critical(self, "导出失败", f"重新导出失败: {exc}")

    def _copy_agent_instructions(self) -> None:
        instr_file = self.task_dir / "task_instructions.md"
        if instr_file.is_file():
            text = instr_file.read_text(encoding="utf-8")
        else:
            text = (
                f"请处理位于 `{self.task_dir.resolve()}` 的原子物理提取任务包。\n"
                f"包含 items.json, tables/, schema.json, parsed.md。\n"
                f"请根据 schema.json 输出合法的提炼结果并保存为 result.json。"
            )
        self._copy_to_clipboard(text, "已复制完整 Agent 提示词")

    def _copy_all_items_json(self) -> None:
        items_file = self.task_dir / "items.json"
        if items_file.is_file():
            self._copy_to_clipboard(items_file.read_text(encoding="utf-8"), "已复制全部条目 JSON")
        else:
            QMessageBox.warning(self, "文件缺失", "未找到 items.json 文件")

    def _copy_selected_item_json(self) -> None:
        row = self.items_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择条目", "请先在列表中选中一个物理问题条目。")
            return
        item_w = self.items_table.item(row, 0)
        data = item_w.data(Qt.UserRole) if item_w else None
        if data:
            self._copy_to_clipboard(json.dumps(data, ensure_ascii=False, indent=2), f"已复制条目 {data.get('item_id')} JSON")

    def _copy_table_summary_prompt(self) -> None:
        try:
            prompt = self.packager.build_table_summary_prompt(self.doc_id)
            self._copy_to_clipboard(prompt, "已复制数据表格总结提示词，可直接发送给 AI")
        except Exception as exc:
            QMessageBox.warning(self, "生成失败", f"无法生成表格总结提示词: {exc}")

    def _copy_current_table_md(self) -> None:
        row = self.tables_list.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择表格", "请先在列表中选择一个表格。")
            return
        item = self.tables_list.item(row)
        p = Path(item.data(Qt.UserRole))
        if p.is_file():
            self._copy_to_clipboard(p.read_text(encoding="utf-8"), f"已复制 {p.name} Markdown 内容")

    def _copy_selected_file_content(self) -> None:
        row = self.files_list.currentRow()
        if row < 0:
            return
        item = self.files_list.item(row)
        p = Path(item.data(Qt.UserRole))
        if p.is_file():
            try:
                self._copy_to_clipboard(p.read_text(encoding="utf-8"), f"已复制 {p.name} 文件内容")
            except Exception as exc:
                QMessageBox.warning(self, "复制失败", str(exc))

    def _copy_selected_file_path(self) -> None:
        row = self.files_list.currentRow()
        if row < 0:
            return
        item = self.files_list.item(row)
        p = Path(item.data(Qt.UserRole))
        self._copy_to_clipboard(str(p.resolve()), f"已复制 {p.name} 路径")

    # ---------------------------------------------------------------- Import Actions (Image 2)
    def _import_items_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择 Agent / 大模型问题条目回复文件",
            str(self.task_dir),
            "JSON/文本回复 (*.json *.txt *.md);;所有文件 (*)",
        )
        if not path:
            return
        self._submit_items(path, agent_model=self.current_agent_model)

    def _paste_items_reply(self) -> None:
        dlg = AgentPasteReplyDialog(
            title=f"粘贴问题条目回复 - {self.doc_id}",
            placeholder="在此粘贴 Agent 针对 items.json 输出的完整 JSON 回复（包含 items 数组）...",
            model=self.current_agent_model,
            config=getattr(self.pipeline, "config", None),
            parent=self,
        )
        if dlg.exec() == QDialog.Accepted:
            text = dlg.get_text()
            if not text:
                QMessageBox.information(self, "内容为空", "未粘贴任何内容。")
                return
            chosen_model = dlg.get_model_name()
            self.current_agent_model = chosen_model
            save_default_agent_model(chosen_model, getattr(self.pipeline, "config", None))
            self._submit_items(text, agent_model=chosen_model)

    def _submit_items(self, source: str | Path, agent_model: str) -> None:
        try:
            res = self.packager.submit_task_result(
                self.doc_id,
                source,
                agent_name="agent_workbench",
                agent_model=agent_model,
            )
            QMessageBox.information(
                self,
                "条目回复导入成功",
                f"成功导入 Agent 提炼结果！\n\n"
                f"• 入库知识卡片: {res['stored_cards']} 张\n"
                f"• 入库关键词: {res['stored_keywords']} 个\n"
                f"• 处理进度: {res['processed_items']}/{res['total_items']} ({res['status']})",
            )
            self._refresh_all()
        except Exception as exc:
            log.exception("Submit items failed")
            QMessageBox.critical(self, "导入失败", f"条目回复解析或入库失败:\n{exc}")

    def _import_tables_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择数据表格总结回复文件",
            str(self.task_dir),
            "JSON/文本回复 (*.json *.txt *.md);;所有文件 (*)",
        )
        if not path:
            return
        self._submit_tables(path, agent_model=self.current_agent_model)

    def _paste_tables_reply(self) -> None:
        dlg = AgentPasteReplyDialog(
            title=f"粘贴数据表格总结回复 - {self.doc_id}",
            placeholder="在此粘贴 AI 根据数据表格大概信息总结的 JSON 回复（包含 tables 数组）...",
            model=self.current_agent_model,
            config=getattr(self.pipeline, "config", None),
            parent=self,
        )
        if dlg.exec() == QDialog.Accepted:
            text = dlg.get_text()
            if not text:
                QMessageBox.information(self, "内容为空", "未粘贴任何内容。")
                return
            chosen_model = dlg.get_model_name()
            self.current_agent_model = chosen_model
            save_default_agent_model(chosen_model, getattr(self.pipeline, "config", None))
            self._submit_tables(text, agent_model=chosen_model)

    def _submit_tables(self, source: str | Path, agent_model: str) -> None:
        try:
            res = self.packager.submit_table_summary_result(
                self.doc_id,
                source,
                agent_name="agent_workbench",
                agent_model=agent_model,
            )
            QMessageBox.information(
                self,
                "表格总结导入成功",
                f"成功导入数据表格总结！\n\n"
                f"• 处理数据表格: {res['tables_processed']} 个\n"
                f"• 生成数据类型知识卡 (DATA): {res['stored_cards']} 张",
            )
            self._refresh_all()
        except Exception as exc:
            log.exception("Submit table summary failed")
            QMessageBox.critical(self, "导入失败", f"表格总结解析或入库失败:\n{exc}")
