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


class AgentRejectionDialog(QDialog):
    """Modal dialog displaying rejection reasons when result.json validation fails."""

    def __init__(
        self,
        doc_id: str,
        task_dir: Path,
        verification_report: dict[str, Any],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.doc_id = doc_id
        self.task_dir = Path(task_dir)
        self.report = verification_report
        self.force_ingest_requested = False

        self.setWindowTitle(f"❌ 任务核验未通过（已打回重做）— {self.doc_id}")
        self.resize(720, 520)
        self.setMinimumSize(620, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # 1. Warning banner
        banner = QFrame()
        banner.setStyleSheet(
            "QFrame {"
            " background: #fef2f2;"
            " border: 1.5px solid #fecaca;"
            " border-radius: 8px;"
            " padding: 12px;"
            "}"
        )
        b_layout = QVBoxLayout(banner)
        b_layout.setContentsMargins(4, 4, 4, 4)
        b_layout.setSpacing(4)

        b_title = QLabel("⚠️ 结果核验未通过，已打回！数据未写入数据库。")
        b_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #dc2626;")
        b_layout.addWidget(b_title)

        reasons = self.report.get("rejection_reasons", [])
        result_file = self.task_dir / "result.json"
        b_desc = QLabel(f"在对 `{result_file.name}` 进行自动化严格核验时，共发现 {len(reasons)} 处问题：")
        b_desc.setStyleSheet("font-size: 12px; color: #991b1b;")
        b_layout.addWidget(b_desc)
        layout.addWidget(banner)

        # 2. Detailed reasons box
        lbl_reasons = QLabel("核验问题清单与详情：")
        lbl_reasons.setStyleSheet("font-weight: bold; color: #334155; font-size: 12px;")
        layout.addWidget(lbl_reasons)

        self.reasons_edit = QTextEdit()
        self.reasons_edit.setReadOnly(True)
        self.reasons_edit.setStyleSheet(
            "QTextEdit {"
            " border: 1px solid #cbd5e1;"
            " border-radius: 6px;"
            " background: #f8fafc;"
            " font-family: monospace;"
            " font-size: 12px;"
            " padding: 8px;"
            "}"
        )
        lines = []
        for idx, r in enumerate(reasons, 1):
            lines.append(f"{idx}. {r}")
        self.reasons_edit.setPlainText("\n\n".join(lines) if lines else "（未检测到具体错误信息）")
        layout.addWidget(self.reasons_edit, 1)

        # 3. Action bar
        btn_bar = QHBoxLayout()
        btn_bar.setSpacing(10)

        self.copy_fix_prompt_btn = QPushButton("📋 复制打回重做提示词 (发送给 Agent)")
        self.copy_fix_prompt_btn.setStyleSheet(
            "QPushButton {"
            " background-color: #dc2626; color: #ffffff;"
            " border: none; border-radius: 6px;"
            " font-weight: bold; font-size: 12px; padding: 6px 16px;"
            "}"
            "QPushButton:hover { background-color: #b91c1c; }"
        )
        self.copy_fix_prompt_btn.clicked.connect(self._copy_fix_prompt)
        btn_bar.addWidget(self.copy_fix_prompt_btn)

        open_folder_btn = QPushButton("📁 打开任务目录")
        open_folder_btn.clicked.connect(self._open_folder)
        btn_bar.addWidget(open_folder_btn)

        btn_bar.addStretch(1)

        force_btn = QPushButton("⚠️ 忽略缺失强制部分入库")
        force_btn.setObjectName("Ghost")
        force_btn.setStyleSheet("color: #b45309; border: 1px solid #fde68a; font-size: 11px;")
        force_btn.setToolTip("仅将校验通过的条目入库，文献将标记为 PARTIAL_EXTRACTED")
        force_btn.clicked.connect(self._force_partial_ingest)
        btn_bar.addWidget(force_btn)

        close_btn = QPushButton("关闭")
        close_btn.setFixedSize(80, 32)
        close_btn.clicked.connect(self.reject)
        btn_bar.addWidget(close_btn)

        layout.addLayout(btn_bar)

    def _copy_fix_prompt(self) -> None:
        prompt = self.report.get("fix_prompt", "")
        if not prompt:
            reasons = self.report.get("rejection_reasons", [])
            prompt = "\n".join(reasons)
        clipboard = QGuiApplication.clipboard()
        if clipboard:
            clipboard.setText(prompt)
        QMessageBox.information(self, "复制成功", "已复制打回重做提示词！直接粘贴发送给 Agent 即可。")

    def _open_folder(self) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(str(self.task_dir.resolve()))
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.task_dir.resolve())))
        except Exception as exc:
            QMessageBox.warning(self, "打开文件夹失败", str(exc))

    def _force_partial_ingest(self) -> None:
        reply = QMessageBox.question(
            self,
            "确认强制部分入库",
            "当前 result.json 存在条目缺失或校验错误。\n"
            "强制入库将仅提取校验通过的有效卡片，且文献状态将标记为「部分提炼 (PARTIAL_EXTRACTED)」。\n\n"
            "确定要强制部分入库吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.force_ingest_requested = True
            self.accept()


class AgentTaskDialog(QDialog):
    """Workbench dialog for managing Agent task packages, table summaries, and reply imports."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        db: Any,
        workspace: str | Path,
        pipeline: Any = None,
        doc_id: str | list[str],
    ):
        super().__init__(parent)
        self.db = db
        self.workspace = Path(workspace)
        self.pipeline = pipeline
        if isinstance(doc_id, (list, tuple)):
            self.doc_ids = [str(d).strip() for d in doc_id if str(d).strip()]
        else:
            self.doc_ids = [str(doc_id).strip()] if str(doc_id).strip() else []
        self.doc_id = self.doc_ids[0] if self.doc_ids else ""
        self.packager = AgentTaskPackager(self.db, self.workspace, pipeline=self.pipeline)
        self.current_agent_model = get_default_agent_model(getattr(self.pipeline, "config", None))

        title_suffix = f" (共 {len(self.doc_ids)} 篇)" if len(self.doc_ids) > 1 else ""
        self.setWindowTitle(f"💻 本地 Agent / CLI 任务工作台 — {self.doc_id}{title_suffix}")
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

        if len(self.doc_ids) > 1:
            selector_row = QHBoxLayout()
            lbl_sel = QLabel(f"切换目标文献 ({len(self.doc_ids)} 篇):")
            lbl_sel.setStyleSheet("font-weight: bold; font-size: 12px; color: #1e293b;")
            selector_row.addWidget(lbl_sel)

            self.doc_combo = QComboBox()
            self.doc_combo.setMinimumWidth(260)
            for did in self.doc_ids:
                d = self.db.get_document(did) if self.db else None
                short_title = str(d.get("title") or "") if d else ""
                if len(short_title) > 35:
                    short_title = short_title[:32] + "..."
                self.doc_combo.addItem(f"{did}  {short_title}", did)
            self.doc_combo.currentIndexChanged.connect(self._on_doc_combo_changed)
            selector_row.addWidget(self.doc_combo)

            batch_export_btn = QPushButton("📦 批量导出全部任务包")
            batch_export_btn.setToolTip("为选中的所有文献生成专属 Agent 任务目录")
            batch_export_btn.clicked.connect(self._batch_export_all)
            selector_row.addWidget(batch_export_btn)
            selector_row.addStretch(1)
            info_vbox.addLayout(selector_row)

        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #0f172a;")
        self.title_label.setWordWrap(True)
        info_vbox.addWidget(self.title_label)

        self.meta_label = QLabel()
        self.meta_label.setStyleSheet("font-size: 11px; color: #64748b;")
        info_vbox.addWidget(self.meta_label)
        h_layout.addLayout(info_vbox, 1)

        right_vbox = QVBoxLayout()
        right_vbox.setSpacing(6)
        right_vbox.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.status_badge = QLabel()
        self.status_badge.setStyleSheet(
            "padding: 4px 10px; border-radius: 6px; font-weight: bold; font-size: 12px; background: #e0f2fe; color: #0369a1;"
        )
        right_vbox.addWidget(self.status_badge, 0, Qt.AlignRight)

        model_row = QHBoxLayout()
        model_row.setSpacing(6)
        lbl_m = QLabel("🤖 处理模型:")
        lbl_m.setStyleSheet("font-size: 11px; font-weight: bold; color: #475569;")
        model_row.addWidget(lbl_m)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(180)
        items = list(PRESET_AGENT_MODELS)
        if self.current_agent_model and self.current_agent_model not in items:
            items.insert(0, self.current_agent_model)
        self.model_combo.addItems(items)
        self.model_combo.setCurrentText(self.current_agent_model)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        model_row.addWidget(self.model_combo)

        right_vbox.addLayout(model_row)
        h_layout.addLayout(right_vbox)
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
        cli_cmd = f"python -m app_core agent execute {self.doc_id} --model \"{self.current_agent_model}\""
        self.cli_edit = QLineEdit(cli_cmd)
        self.cli_edit.setReadOnly(True)
        self.cli_edit.setStyleSheet("background: #f1f5f9; color: #0284c7; font-family: monospace;")
        row2.addWidget(self.cli_edit, 1)

        copy_cli_btn = QPushButton("📋 复制命令")
        copy_cli_btn.clicked.connect(lambda: self._copy_to_clipboard(self.cli_edit.text(), "CLI 命令已复制"))
        row2.addWidget(copy_cli_btn)
        ctrl_vbox.addLayout(row2)

        # Row 3: Agent Prompt Instruction & Auto-Verification
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("任务操作与核验:"))
        copy_prompt_btn = QPushButton("📋 复制完整 Agent 提示词")
        copy_prompt_btn.setStyleSheet("font-weight: bold; color: #1d4ed8;")
        copy_prompt_btn.clicked.connect(self._copy_agent_instructions)
        row3.addWidget(copy_prompt_btn)

        self.verify_btn = QPushButton("⚡ 检测并一键验收 result.json")
        self.verify_btn.setObjectName("Pill")
        self.verify_btn.setToolTip("自动检测当前目录下是否存在 result.json，进行格式与完整性严格校验。通过则直接入库，存在问题则打回并生成重做提示词。")
        self.verify_btn.setStyleSheet(
            "QPushButton {"
            " background: #0284c7; color: #ffffff; border: 1px solid #0369a1;"
            " border-radius: 4px; padding: 5px 14px; font-weight: bold;"
            "}"
            "QPushButton:hover { background: #0369a1; }"
        )
        self.verify_btn.clicked.connect(self._verify_and_ingest_result_json)
        row3.addWidget(self.verify_btn)

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

        # Auto verify button
        self.auto_verify_tab_btn = QPushButton("⚡ 自动验收 result.json")
        self.auto_verify_tab_btn.setObjectName("Pill")
        self.auto_verify_tab_btn.setToolTip("自动识别目录下是否有 result.json，尝试解析校验，成功则通过入库，存在问题则打回并生成重做提示词。")
        self.auto_verify_tab_btn.setStyleSheet(
            "QPushButton {"
            " background: #0284c7; color: #ffffff; border: 1px solid #0369a1;"
            " border-radius: 4px; padding: 5px 12px; font-weight: bold;"
            "}"
            "QPushButton:hover { background: #0369a1; }"
        )
        self.auto_verify_tab_btn.clicked.connect(self._verify_and_ingest_result_json)
        bar.addWidget(self.auto_verify_tab_btn)

        # Two buttons matching Image 2
        self.import_items_file_btn = QPushButton("📥 导入回复文件")
        self.import_items_file_btn.setObjectName("Pill")
        self.import_items_file_btn.setStyleSheet(
            "QPushButton {"
            " background: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd;"
            " border-radius: 4px; padding: 5px 12px; font-weight: bold;"
            "}"
            "QPushButton:hover { background: #bae6fd; }"
        )
        self.import_items_file_btn.clicked.connect(self._import_items_file)
        bar.addWidget(self.import_items_file_btn)

        self.paste_items_reply_btn = QPushButton("📝 粘贴回复文本")
        self.paste_items_reply_btn.setObjectName("Ghost")
        self.paste_items_reply_btn.setStyleSheet(
            "QPushButton {"
            " background: #f8fafc; color: #2563eb; border: 1px solid #bfdbfe;"
            " border-radius: 4px; padding: 5px 12px; font-weight: bold;"
            "}"
            "QPushButton:hover { background: #eff6ff; }"
        )
        self.paste_items_reply_btn.clicked.connect(self._paste_items_reply)
        bar.addWidget(self.paste_items_reply_btn)

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

    def _on_model_changed(self, text: str) -> None:
        val = text.strip() or "GPT-6.2 Sol Ultra"
        self.current_agent_model = val
        save_default_agent_model(val, getattr(self.pipeline, "config", None))
        if hasattr(self, "cli_edit"):
            self.cli_edit.setText(f"python -m app_core agent execute {self.doc_id} --model \"{val}\"")

    def _on_doc_combo_changed(self, idx: int) -> None:
        if idx < 0 or not hasattr(self, "doc_combo"):
            return
        new_doc_id = str(self.doc_combo.currentData() or "").strip()
        if new_doc_id and new_doc_id != self.doc_id:
            self.doc_id = new_doc_id
            self.task_dir = self.workspace / "agent_tasks" / self.doc_id
            try:
                if not self.task_dir.is_dir() or not (self.task_dir / "items.json").is_file():
                    self.packager.export_task_bundle(self.doc_id)
            except Exception as exc:
                log.warning("Export task bundle warning: %s", exc)
            self.path_edit.setText(str(self.task_dir.resolve()))
            self.cli_edit.setText(f"python -m app_core agent execute {self.doc_id} --model \"{self.current_agent_model}\"")
            title_suffix = f" (共 {len(self.doc_ids)} 篇)" if len(self.doc_ids) > 1 else ""
            self.setWindowTitle(f"💻 本地 Agent / CLI 任务工作台 — {self.doc_id}{title_suffix}")
            self._refresh_all()

    def _batch_export_all(self) -> None:
        exported = 0
        errors = []
        for did in self.doc_ids:
            try:
                self.packager.export_task_bundle(did)
                exported += 1
            except Exception as exc:
                errors.append(f"{did}: {exc}")
        msg = f"已成功为 {exported}/{len(self.doc_ids)} 篇文献生成 Agent 任务包！"
        if errors:
            msg += "\n\n部分失败:\n" + "\n".join(errors[:5])
        QMessageBox.information(self, "批量导出完成", msg)
        self._refresh_all()

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
        self._update_verification_buttons_state(parse_status == "LLM_EXTRACTED")

        # Refresh items
        self._populate_items()
        # Refresh tables
        self._populate_tables()
        # Refresh raw files
        self._populate_files()

        self.bottom_status.setText(f"已就绪 | 任务包: {self.task_dir.name} | 已入库 {len(cards)} 张知识卡片")

    def _update_verification_buttons_state(self, is_extracted: bool) -> None:
        """Update buttons state according to whether the task result is verified & ingested."""
        if is_extracted:
            green_style = (
                "QPushButton {"
                " background-color: #16a34a; color: #ffffff; border: 1px solid #15803d;"
                " border-radius: 4px; padding: 5px 14px; font-weight: bold;"
                "}"
                "QPushButton:disabled {"
                " background-color: #16a34a; color: #ffffff; border: 1px solid #15803d;"
                "}"
            )
            if hasattr(self, "verify_btn"):
                self.verify_btn.setText("✅ 已验收通过")
                self.verify_btn.setStyleSheet(green_style)
                self.verify_btn.setEnabled(False)
                self.verify_btn.setToolTip("该文献任务已通过严格验收并成功入库。若需重新提取，可点击上方「🔄 重新生成」。")
            if hasattr(self, "auto_verify_tab_btn"):
                self.auto_verify_tab_btn.setText("✅ 已验收通过")
                self.auto_verify_tab_btn.setStyleSheet(green_style)
                self.auto_verify_tab_btn.setEnabled(False)
                self.auto_verify_tab_btn.setToolTip("该文献任务已通过严格验收并成功入库。若需重新提取，可点击上方「🔄 重新生成」。")
            if hasattr(self, "import_items_file_btn"):
                self.import_items_file_btn.setEnabled(False)
                self.import_items_file_btn.setToolTip("当前文献已验收通过。")
            if hasattr(self, "paste_items_reply_btn"):
                self.paste_items_reply_btn.setEnabled(False)
                self.paste_items_reply_btn.setToolTip("当前文献已验收通过。")
        else:
            blue_btn_style = (
                "QPushButton {"
                " background: #0284c7; color: #ffffff; border: 1px solid #0369a1;"
                " border-radius: 4px; padding: 5px 14px; font-weight: bold;"
                "}"
                "QPushButton:hover { background: #0369a1; }"
            )
            if hasattr(self, "verify_btn"):
                self.verify_btn.setText("⚡ 检测并一键验收 result.json")
                self.verify_btn.setStyleSheet(blue_btn_style)
                self.verify_btn.setEnabled(True)
                self.verify_btn.setToolTip("自动检测当前目录下是否存在 result.json，进行格式与完整性严格校验。通过则直接入库，存在问题则打回并生成重做提示词。")
            if hasattr(self, "auto_verify_tab_btn"):
                self.auto_verify_tab_btn.setText("⚡ 自动验收 result.json")
                self.auto_verify_tab_btn.setStyleSheet(blue_btn_style)
                self.auto_verify_tab_btn.setEnabled(True)
                self.auto_verify_tab_btn.setToolTip("自动识别目录下是否有 result.json，尝试解析校验，成功则通过入库，存在问题则打回并生成重做提示词。")
            if hasattr(self, "import_items_file_btn"):
                self.import_items_file_btn.setEnabled(True)
                self.import_items_file_btn.setToolTip("")
            if hasattr(self, "paste_items_reply_btn"):
                self.paste_items_reply_btn.setEnabled(True)
                self.paste_items_reply_btn.setToolTip("")

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
        raw_instructions = instr_file.read_text(encoding="utf-8") if instr_file.is_file() else ""
        doc = self.db.get_document(self.doc_id) if self.db else None
        title = str(doc.get("title") or "—") if doc else "—"

        header = (
            f"你好，请帮我处理位于以下本地目录的原子物理计算知识提取任务：\n\n"
            f"【工作目录】：`{self.task_dir.resolve()}`\n"
            f"【目标文献】：`{self.doc_id}` — 《{title}》\n\n"
            f"【执行要求】：\n"
            f"1. 请直接访问并读取上述工作目录下的 `task_instructions.md` 与 `items.json`；\n"
            f"2. 结合 `tables/` 目录下的表格文件与 `parsed.md` 原文；\n"
            f"3. 严格遵循 `schema.json` 定义的格式规范进行物理提炼；\n"
            f"4. 处理完成后，请直接在该工作目录下生成并保存 `result.json`。\n"
            f"5. 若遇到大模型速率限制 (429) 或网络超时，请暂停并每隔 5~10 分钟轮询一次等待解除，切勿中断终止任务。\n\n"
            f"---\n\n"
        )
        full_text = header + raw_instructions
        self._copy_to_clipboard(full_text, "已复制包含完整路径的 Agent 提示词！直接粘贴给 Codex 即可。")

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

    # ---------------------------------------------------------------- Auto Verification & Ingestion
    def _verify_and_ingest_result_json(self) -> None:
        """Automatically detect result.json in task_dir, validate schema and completeness.

        If valid, ingests into database and transitions status to LLM_EXTRACTED.
        If errors or missing items exist, rejects and opens AgentRejectionDialog with fix prompt.
        """
        result_file = self.task_dir / "result.json"
        if not result_file.is_file():
            QMessageBox.warning(
                self,
                "未检测到结果文件",
                f"在当前任务工作目录下尚未找到 `result.json`：\n\n"
                f"{result_file.resolve()}\n\n"
                f"请确认 Agent（如 Codex / Claude Code / Antigravity）是否已处理完毕并在该目录下生成了 `result.json` 文件。",
            )
            return

        report = self.packager.validate_task_result(self.doc_id, result_file)
        if not report.get("passed"):
            dlg = AgentRejectionDialog(self.doc_id, self.task_dir, report, parent=self)
            if dlg.exec() == QDialog.Accepted and getattr(dlg, "force_ingest_requested", False):
                try:
                    from ui.progress_dialog import run_task_with_progress

                    res = run_task_with_progress(
                        self,
                        self.packager.submit_task_result,
                        self.doc_id,
                        result_file,
                        agent_name="agent_workbench",
                        agent_model=self.current_agent_model,
                        complete=False,
                        title="💾 正在强制部分入库",
                        message="正在将部分有效条目写入知识库与向量索引...",
                    )
                    QMessageBox.information(
                        self,
                        "部分入库成功",
                        f"已强制入库有效卡片：{res['stored_cards']} 张卡片、{res['stored_keywords']} 个关键词。\n"
                        f"文献状态已更新为：【部分提炼 (PARTIAL_EXTRACTED)】",
                    )
                    self._refresh_all()
                    if hasattr(self.parent(), "_refresh_related_cards"):
                        self.parent()._refresh_related_cards()
                    if hasattr(self.parent(), "_refresh_stepper"):
                        self.parent()._refresh_stepper()
                except Exception as exc:
                    QMessageBox.critical(self, "强制入库失败", f"强制入库异常: {exc}")
            return

        # Verification passed! Ingest into database
        try:
            from ui.progress_dialog import run_task_with_progress

            res = run_task_with_progress(
                self,
                self.packager.submit_task_result,
                self.doc_id,
                result_file,
                agent_name="agent_workbench",
                agent_model=self.current_agent_model,
                complete=True,
                title="💾 正在验收并写入数据库",
                message="正在将 Agent 提炼结果写入知识库与向量索引...",
            )
            cards = res.get("stored_cards", 0)
            kws = res.get("stored_keywords", 0)
            items = res.get("processed_items", 0)
            total = res.get("total_items", items)

            QMessageBox.information(
                self,
                "验收通过并入库成功",
                f"🎉 恭喜！`result.json` 自动化严格核验 100% 通过！\n\n"
                f"• 验证条目: {items} / {total} 条（完整无缺失）\n"
                f"• 成功入库知识卡片: {cards} 张\n"
                f"• 成功入库关键词: {kws} 个\n"
                f"• 文献状态已更新为: 【AI 已提炼 (LLM_EXTRACTED)】",
            )
            self._refresh_all()
            if hasattr(self.parent(), "_refresh_related_cards"):
                self.parent()._refresh_related_cards()
            if hasattr(self.parent(), "_refresh_stepper"):
                self.parent()._refresh_stepper()
        except Exception as exc:
            QMessageBox.critical(self, "入库失败", f"入库过程遇到异常：{exc}")

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
            from ui.progress_dialog import run_task_with_progress

            res = run_task_with_progress(
                self,
                self.packager.submit_task_result,
                self.doc_id,
                source,
                agent_name="agent_workbench",
                agent_model=agent_model,
                title="💾 正在导入条目并写入数据库",
                message="正在解析条目回复并生成知识卡片与向量索引...",
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
            from ui.progress_dialog import run_task_with_progress

            res = run_task_with_progress(
                self,
                self.packager.submit_table_summary_result,
                self.doc_id,
                source,
                agent_name="agent_workbench",
                agent_model=agent_model,
                title="📊 正在导入表格总结并写入数据库",
                message="正在为数据表格生成数据类型知识卡 (DATA)...",
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
