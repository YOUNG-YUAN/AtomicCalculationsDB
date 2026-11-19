"""Library view: discovery, document ledger, manual queue, drag-and-drop."""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Iterable

from PySide6.QtCore import QPoint, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from ui.controls import WheelSafeComboBox as QComboBox, WheelSafeSpinBox as QSpinBox

from crawler.manual_inbox_watcher import ManualInboxWatcher
from db.sqlite_manager import SQLiteManager
from models.pipeline_stepper import (
    DocumentPipelineStatus,
    compute_document_pipeline_status,
    rollback_pipeline_from_step,
)
from reprocess import ReprocessManager
from sync_mcp.exporter import export_pending_html
from ui.m3_theme import (
    doc_type_label,
    download_label,
    format_authors_short,
    parse_label,
)
from ui.progress_eta import EtaTracker
from ui.semi_auto_dialog import SemiAutoBatchDialog
from ui.step_progress_widget import MiniStepProgressWidget, StepProgressDelegate
from ui.widgets import DropZone, PageHeader, SectionCard
from ui.workers import (
    DiscoveryWorker,
    IngestWorker,
    MetadataRepairWorker,
    ReprocessWorker,
    ScanInboxWorker,
)

logger = logging.getLogger(__name__)

_FILTERS = [
    ("全部文献", None),
    ("已排队", "QUEUED"),
    ("已下载", "DOWNLOADED"),
    ("待手动导入", "MANUAL_REQUIRED"),
    ("已拒绝", "REJECTED"),
    ("下载失败", "FAILED"),
]
_STATUS_COLORS = {
    "DOWNLOADED": "#1E6B45",
    "LLM_EXTRACTED": "#1E6B45",
    "MANUAL_REQUIRED": "#8A4B00",
    "REJECTED": "#B3261E",
    "FAILED": "#B3261E",
    "NEEDS_OCR": "#8A4B00",
}

_COLUMN_SORT_FIELDS = {
    0: "doc_id",
    1: "title",
    2: "doc_type",
    3: "pub_year",
    4: "journal",
    5: "authors",
    6: "input_tokens",
    7: "download_status",
    8: "parse_status",
    9: "pipeline_step",
}

_BASE_HEADER_LABELS = [
    "DOC-ID", "标题", "类型", "年份", "期刊", "作者",
    "Token 消耗 (输入 / 输出)", "下载", "解析", "流程进度",
]



class LibraryView(QWidget):
    open_document = Signal(str)
    toast = Signal(str)
    data_changed = Signal()

    def __init__(
        self,
        db: SQLiteManager,
        fetcher: Any,
        workspace: str,
        config: Any = None,
        watcher: Any = None,
        pipeline: Any = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.fetcher = fetcher
        self.workspace = workspace
        self.config = config
        self.watcher = watcher or ManualInboxWatcher(db, workspace)
        self.pipeline = pipeline
        self.reprocess_manager = (
            ReprocessManager(db, workspace, pipeline) if pipeline is not None else None
        )
        self._workers: list[Any] = []
        self._reprocess_worker: ReprocessWorker | None = None
        self._reprocess_mode = ""
        self._reprocess_eta = EtaTracker()
        self._loading_table = False
        self._auto_repair_started = False
        self.current_page = 1
        self.page_size = 50
        self.total_count = 0
        self.total_pages = 1
        self.current_collection_id = "__all__"
        self.sort_column_index = 0
        self.sort_order = "DESC"

        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setInterval(2500)
        self._auto_refresh_timer.timeout.connect(self._on_auto_refresh_tick)
        self._auto_refresh_timer.start()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("PageRoot")
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 4, 10, 18)
        layout.setSpacing(12)

        header = PageHeader(
            "文献库与文献清单",
            "文献自动下载 → 手动导入 → 解析 → 知识提取，全程由 DOC-ID 追踪。",
        )
        scan_button = QPushButton("扫描收件箱")
        scan_button.setObjectName("Pill")
        scan_button.clicked.connect(self._scan_inbox)
        export_button = QPushButton("导出待下载清单")
        export_button.setObjectName("Pill")
        export_button.clicked.connect(self._export_pending)
        refresh_button = QPushButton("刷新")
        refresh_button.setObjectName("Ghost")
        refresh_button.clicked.connect(self.refresh)
        header.add_action(scan_button)
        header.add_action(export_button)
        header.add_action(refresh_button)
        layout.addWidget(header)

        reprocess_row = QHBoxLayout()
        reprocess_row.setSpacing(7)
        self.local_clean_button = QPushButton("🧹 全库本地修复与清洗 (0 Token)")
        self.local_clean_button.setObjectName("Pill")
        self.local_clean_button.setCursor(Qt.PointingHandCursor)
        self.local_clean_button.clicked.connect(self._open_local_clean_dialog)
        self.step3_api_button = QPushButton("⚡ ③ AI 快速理解")
        self.step3_api_button.setObjectName("Primary")
        self.step3_api_button.setCursor(Qt.PointingHandCursor)
        self.step3_api_button.setToolTip("打开第 ③ 步 AI 快速理解：支持依照模型策略自动处理，或无API网页版AI半自动模式批量打包。")
        self.step3_api_button.clicked.connect(self._run_selected_step3_api)

        self.step4_audit_button = QPushButton("🔍 ④ AI 校对核查")
        self.step4_audit_button.setObjectName("Pill")
        self.step4_audit_button.setCursor(Qt.PointingHandCursor)
        self.step4_audit_button.setToolTip(
            "打开第 ④ 步 AI 校对核查：支持依照模型策略自动处理，或无API网页版AI半自动模式审稿核验与补齐广义延拓。"
        )
        self.step4_audit_button.clicked.connect(self._open_selected_step4_audit)

        self.step5_deep_button = QPushButton("✨ ⑤ AI 深度归纳")
        self.step5_deep_button.setObjectName("Primary")
        self.step5_deep_button.setCursor(Qt.PointingHandCursor)
        self.step5_deep_button.setToolTip("打开第 ⑤ 步 AI 深度归纳：支持依照模型策略自动处理，或无API网页版AI半自动模式跨卡凝练宏观全景。")
        self.step5_deep_button.clicked.connect(self._open_selected_step5_deep)

        self.incremental_button = self.step3_api_button
        self.force_button = self.step3_api_button

        self.stop_reprocess_button = QPushButton("⏹ 停止当前任务")
        self.stop_reprocess_button.setObjectName("Ghost")
        self.stop_reprocess_button.setCursor(Qt.PointingHandCursor)
        self.stop_reprocess_button.setVisible(False)
        self.stop_reprocess_button.clicked.connect(self._stop_reprocess)
        for button in (
            self.local_clean_button,
            self.step3_api_button,
            self.step4_audit_button,
            self.step5_deep_button,
            self.stop_reprocess_button,
        ):
            reprocess_row.addWidget(button)
        reprocess_row.addStretch(1)
        layout.addLayout(reprocess_row)
        self.reprocess_status = QLabel(
            "五步流式进度：①提取文字 ── ②提取关键信息 ── ③AI 快速提取 ── ④写入数据库 (冒绿光) ── ⑤AI 深度归纳 (冒金光)。右键文献可从指定步骤开始执行。"
        )
        self.reprocess_status.setObjectName("Tiny")
        self.reprocess_status.setWordWrap(True)
        layout.addWidget(self.reprocess_status)
        self.reprocess_progress = QProgressBar()
        self.reprocess_progress.setRange(0, 100)
        self.reprocess_progress.setValue(0)
        self.reprocess_progress.setFormat("整体进度 %p%")
        self.reprocess_progress.setTextVisible(True)
        self.reprocess_progress.setVisible(False)
        self.reprocess_eta = QLabel("")
        self.reprocess_eta.setObjectName("Tiny")
        self.reprocess_eta.setMinimumWidth(260)
        self.reprocess_eta.setVisible(False)
        reprocess_progress_row = QHBoxLayout()
        reprocess_progress_row.setSpacing(10)
        reprocess_progress_row.addWidget(self.reprocess_progress, 1)
        reprocess_progress_row.addWidget(self.reprocess_eta, 0)
        layout.addLayout(reprocess_progress_row)
        if self.reprocess_manager is None:
            for button in (
                self.local_clean_button,
                self.step3_api_button,
            ):
                button.setEnabled(False)
        if self.pipeline is None:
            self.step5_deep_button.setEnabled(False)

        discovery = SectionCard("文献寻源", "OpenAlex / Crossref / arXiv / Semantic Scholar")
        row = QHBoxLayout()
        row.setSpacing(8)
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("如：Fe XVII MCDHF active space convergence")
        self.query_edit.returnPressed.connect(self._discover)
        row.addWidget(self.query_edit, 1)
        self.sources: dict[str, QCheckBox] = {}
        for key, label, checked in (
            ("openalex", "OpenAlex", True),
            ("crossref", "Crossref", True),
            ("arxiv", "arXiv", True),
            ("semantic_scholar", "S2", False),
        ):
            box = QCheckBox(label)
            box.setChecked(checked)
            row.addWidget(box)
            self.sources[key] = box
        discover_button = QPushButton("寻源并排队")
        discover_button.setObjectName("Primary")
        discover_button.clicked.connect(self._discover)
        row.addWidget(discover_button)
        discovery.body.addLayout(row)
        hint = QLabel(
            "下载前会执行文献相关性初筛；命中纯实验/临床/纯观测词且无原子结构理论词的条目会被拒绝。"
        )
        hint.setObjectName("Tiny")
        hint.setWordWrap(True)
        discovery.body.addWidget(hint)
        layout.addWidget(discovery)

        self.drop_zone = DropZone()
        self.drop_zone.files_dropped.connect(self._ingest_paths)
        self.drop_zone.browse_requested.connect(self._browse)
        layout.addWidget(self.drop_zone)

        # Lower splitter: Left is Collections Tree (Zotero-style), Right is Table + Filters + Pagination
        self.splitter = QSplitter(Qt.Horizontal)

        # Left panel: Collections Widget
        self.collections_widget = QWidget()
        col_layout = QVBoxLayout(self.collections_widget)
        col_layout.setContentsMargins(0, 0, 8, 0)
        col_layout.setSpacing(6)

        col_header_row = QHBoxLayout()
        col_title = QLabel("我的文库")
        col_title.setStyleSheet("font-weight: bold; font-size: 13px; color: #212529;")
        col_header_row.addWidget(col_title)
        col_header_row.addStretch()

        self.btn_new_collection = QPushButton("+ 新建分类")
        self.btn_new_collection.setObjectName("Ghost")
        self.btn_new_collection.setCursor(Qt.PointingHandCursor)
        self.btn_new_collection.clicked.connect(lambda: self._create_new_collection_prompt())
        col_header_row.addWidget(self.btn_new_collection)
        col_layout.addLayout(col_header_row)

        self.collections_tree = QTreeWidget()
        self.collections_tree.setHeaderHidden(True)
        self.collections_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.collections_tree.customContextMenuRequested.connect(self._show_collection_context_menu)
        self.collections_tree.itemClicked.connect(self._on_collection_item_clicked)
        self.collections_tree.setMinimumWidth(210)
        self.collections_tree.setMinimumHeight(380)
        col_layout.addWidget(self.collections_tree)
        self.splitter.addWidget(self.collections_widget)

        # Right panel: Table + Filters + Pagination
        self.table_panel = QWidget()
        table_panel_layout = QVBoxLayout(self.table_panel)
        table_panel_layout.setContentsMargins(0, 0, 0, 0)
        table_panel_layout.setSpacing(6)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        title = QLabel("文献清单")
        title.setObjectName("SectionTitle")
        filter_row.addWidget(title)
        self.count_label = QLabel("")
        self.count_label.setObjectName("SectionHint")
        filter_row.addWidget(self.count_label)
        filter_row.addStretch(1)
        self.filter_combo = QComboBox()
        for label, value in _FILTERS:
            self.filter_combo.addItem(label, value)
        self.filter_combo.setFixedWidth(150)
        self.filter_combo.currentIndexChanged.connect(lambda _index: self.refresh())
        filter_row.addWidget(self.filter_combo)
        table_panel_layout.addLayout(filter_row)

        self.token_label = QLabel("")
        self.token_label.setObjectName("SectionHint")
        table_panel_layout.addWidget(self.token_label)

        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(list(_BASE_HEADER_LABELS))
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        header_view = self.table.horizontalHeader()
        header_view.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header_view.sectionClicked.connect(self._on_header_section_clicked)
        header_view.setSectionsClickable(True)

        # Allow interactive column resizing by dragging column divider lines in the header
        header_view.setSectionResizeMode(QHeaderView.Interactive)
        header_view.setSectionsMovable(False)

        # Enable horizontal and vertical scrollbars for table
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        # Set comfortable initial column widths
        self.table.setColumnWidth(0, 190)  # DOC-ID
        self.table.setColumnWidth(1, 280)  # 标题
        self.table.setColumnWidth(2, 60)   # 类型
        self.table.setColumnWidth(3, 65)   # 年份
        self.table.setColumnWidth(4, 150)  # 期刊
        self.table.setColumnWidth(5, 140)  # 作者
        self.table.setColumnWidth(6, 170)  # Token 消耗 (输入 / 输出)
        self.table.setColumnWidth(7, 95)   # 下载
        self.table.setColumnWidth(8, 95)   # 解析
        self.table.setColumnWidth(9, 140)  # 流程进度
        self.table.setItemDelegateForColumn(9, StepProgressDelegate(self.table))
        self.table.setMinimumHeight(380)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.cellDoubleClicked.connect(self._cell_double_clicked)
        self.table.itemChanged.connect(self._on_item_changed)
        table_panel_layout.addWidget(self.table)

        # Pagination toolbar
        page_bar = QHBoxLayout()
        page_bar.setContentsMargins(0, 4, 0, 4)
        page_bar.setSpacing(6)

        self.page_summary_label = QLabel("共 0 篇文献")
        self.page_summary_label.setObjectName("SectionHint")
        page_bar.addWidget(self.page_summary_label)
        page_bar.addStretch(1)

        self.first_page_button = QPushButton("首页")
        self.first_page_button.setFixedWidth(50)
        self.first_page_button.clicked.connect(self._go_first_page)
        page_bar.addWidget(self.first_page_button)

        self.prev_page_button = QPushButton("◀ 上一页")
        self.prev_page_button.setFixedWidth(75)
        self.prev_page_button.clicked.connect(self._go_prev_page)
        page_bar.addWidget(self.prev_page_button)

        self.page_indicator_label = QLabel("第 1 / 1 页")
        self.page_indicator_label.setAlignment(Qt.AlignCenter)
        self.page_indicator_label.setMinimumWidth(90)
        page_bar.addWidget(self.page_indicator_label)

        self.next_page_button = QPushButton("下一页 ▶")
        self.next_page_button.setFixedWidth(75)
        self.next_page_button.clicked.connect(self._go_next_page)
        page_bar.addWidget(self.next_page_button)

        self.last_page_button = QPushButton("末页")
        self.last_page_button.setFixedWidth(50)
        self.last_page_button.clicked.connect(self._go_last_page)
        page_bar.addWidget(self.last_page_button)

        page_bar.addSpacing(12)
        page_size_label = QLabel("每页")
        page_size_label.setObjectName("SectionHint")
        page_bar.addWidget(page_size_label)

        self.page_size_combo = QComboBox()
        for sz in (25, 50, 100, 200):
            self.page_size_combo.addItem(f"{sz} 条", sz)
        self.page_size_combo.setCurrentIndex(1)  # default 50
        self.page_size_combo.currentIndexChanged.connect(self._on_page_size_changed)
        page_bar.addWidget(self.page_size_combo)

        page_bar.addSpacing(8)
        jump_label = QLabel("跳至")
        jump_label.setObjectName("SectionHint")
        page_bar.addWidget(jump_label)

        self.jump_spin = QSpinBox()
        self.jump_spin.setRange(1, 1)
        self.jump_spin.setFixedWidth(60)
        page_bar.addWidget(self.jump_spin)

        jump_btn = QPushButton("跳转")
        jump_btn.setFixedWidth(50)
        jump_btn.clicked.connect(self._jump_to_page)
        page_bar.addWidget(jump_btn)

        table_panel_layout.addLayout(page_bar)

        self.splitter.addWidget(self.table_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([220, 880])
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter)

        hint2 = QLabel(
            "提示：点击表头任意列可进行正序（▲）/倒序（▼）排序；双击标题、年份、期刊或作者可直接内联编辑保存。"
            "左侧目录树可右键新建或管理分类，右键文献可将其快速归档至指定分类。"
        )
        hint2.setObjectName("Tiny")
        hint2.setWordWrap(True)
        layout.addWidget(hint2)
        layout.addStretch(1)

        self._reload_collections_tree()
        self._update_header_indicators()
        self.refresh()
        QTimer.singleShot(1200, self._auto_repair_metadata)

    @staticmethod
    def _stepper_tooltip(status: DocumentPipelineStatus) -> str:
        lines = []
        for s in status.steps:
            status_text = "已完成" if str(getattr(s.status, "value", s.status)).upper() == "COMPLETED" else (
                "失败" if str(getattr(s.status, "value", s.status)).upper() == "FAILED" else "未完成"
            )
            lines.append(f"{s.index}. {s.name}: {status_text}")
        glow_val = str(getattr(status.glow, "value", status.glow)).upper()
        if glow_val == "GOLD":
            lines.append("★ 状态: 深度归纳完成 (冒金光)")
        elif glow_val == "GREEN":
            lines.append("● 状态: 基础入库完成 (冒绿光)")
        return "\n".join(lines)

    # ------------------------------------------------------------------ data
    def _go_first_page(self) -> None:
        if self.current_page != 1:
            self.current_page = 1
            self.refresh(keep_page=True)

    def _go_prev_page(self) -> None:
        if self.current_page > 1:
            self.current_page -= 1
            self.refresh(keep_page=True)

    def _go_next_page(self) -> None:
        if self.current_page < self.total_pages:
            self.current_page += 1
            self.refresh(keep_page=True)

    def _go_last_page(self) -> None:
        if self.current_page != self.total_pages:
            self.current_page = self.total_pages
            self.refresh(keep_page=True)

    def _on_page_size_changed(self) -> None:
        self.page_size = int(self.page_size_combo.currentData() or 50)
        self.current_page = 1
        self.refresh()

    def _jump_to_page(self) -> None:
        target = self.jump_spin.value()
        if 1 <= target <= self.total_pages and target != self.current_page:
            self.current_page = target
            self.refresh(keep_page=True)

    # --------------------------------------------------------------- sorting
    def _on_header_section_clicked(self, logical_index: int) -> None:
        field = _COLUMN_SORT_FIELDS.get(logical_index)
        if not field:
            return
        if self.sort_column_index == logical_index:
            self.sort_order = "ASC" if self.sort_order == "DESC" else "DESC"
        else:
            self.sort_column_index = logical_index
            if field in ("pub_year", "input_tokens", "pipeline_step"):
                self.sort_order = "DESC"
            else:
                self.sort_order = "ASC"
        self._update_header_indicators()
        self.current_page = 1
        self.refresh()

    def _update_header_indicators(self) -> None:
        arrow = " ▲" if self.sort_order == "ASC" else " ▼"
        for idx, base_label in enumerate(_BASE_HEADER_LABELS):
            item = self.table.horizontalHeaderItem(idx)
            if item is None:
                item = QTableWidgetItem()
                self.table.setHorizontalHeaderItem(idx, item)
            if idx == self.sort_column_index:
                item.setText(f"{base_label}{arrow}")
            else:
                item.setText(base_label)

    def _get_current_order_by(self) -> str:
        field = _COLUMN_SORT_FIELDS.get(self.sort_column_index, "updated_at")
        direction = self.sort_order
        if field == "pipeline_step":
            return f"pipeline_step {direction}, pipeline_glow {direction}"
        return f"{field} {direction}"

    # --------------------------------------------------------------- collections
    def _find_tree_item(self, target_id: str) -> QTreeWidgetItem | None:
        def _search(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            if item.data(0, Qt.UserRole) == target_id:
                return item
            for i in range(item.childCount()):
                child = item.child(i)
                if child:
                    found = _search(child)
                    if found:
                        return found
            return None

        for i in range(self.collections_tree.topLevelItemCount()):
            top_item = self.collections_tree.topLevelItem(i)
            if top_item:
                found = _search(top_item)
                if found:
                    return found
        return None

    def _reload_collections_tree(self) -> None:
        self.collections_tree.blockSignals(True)
        try:
            self.collections_tree.clear()

            total_docs = self.db.count_documents()
            unfiled_docs = self.db.count_collection_documents("__unfiled__")
            recent_docs = self.db.count_collection_documents("__recent__")

            all_item = QTreeWidgetItem([f"🏛️ 全部文献 ({total_docs})"])
            all_item.setData(0, Qt.UserRole, "__all__")
            self.collections_tree.addTopLevelItem(all_item)

            recent_item = QTreeWidgetItem([f"⏱️ 最近添加 ({recent_docs})"])
            recent_item.setData(0, Qt.UserRole, "__recent__")
            self.collections_tree.addTopLevelItem(recent_item)

            unfiled_item = QTreeWidgetItem([f"📥 未分类文献 ({unfiled_docs})"])
            unfiled_item.setData(0, Qt.UserRole, "__unfiled__")
            self.collections_tree.addTopLevelItem(unfiled_item)

            collections = self.db.list_collections()
            if collections:
                header_item = QTreeWidgetItem(["🏷️ 我的目录"])
                header_item.setFlags(header_item.flags() & ~Qt.ItemIsSelectable)
                header_item.setData(0, Qt.UserRole, "__header__")
                self.collections_tree.addTopLevelItem(header_item)

                item_map: dict[str, QTreeWidgetItem] = {}
                for col in collections:
                    cid = col["collection_id"]
                    name = col["name"]
                    count = col.get("doc_count", 0)
                    item = QTreeWidgetItem([f"📁 {name} ({count})"])
                    item.setData(0, Qt.UserRole, cid)
                    item.setData(0, Qt.UserRole + 1, name)
                    item_map[cid] = item

                for col in collections:
                    cid = col["collection_id"]
                    pid = col.get("parent_id")
                    item = item_map[cid]
                    if pid and pid in item_map:
                        item_map[pid].addChild(item)
                    else:
                        self.collections_tree.addTopLevelItem(item)

            self.collections_tree.expandAll()

            # Reselect current collection item safely
            selected_item = self._find_tree_item(str(self.current_collection_id))
            if selected_item:
                self.collections_tree.setCurrentItem(selected_item)
            else:
                self.current_collection_id = "__all__"
                self.collections_tree.setCurrentItem(all_item)
        finally:
            self.collections_tree.blockSignals(False)

    def _on_collection_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        cid = item.data(0, Qt.UserRole)
        if not cid or cid == "__header__":
            return
        if self.current_collection_id != cid:
            self.current_collection_id = str(cid)
            self.current_page = 1
            self.refresh()

    def _show_collection_context_menu(self, pos: QPoint) -> None:
        item = self.collections_tree.itemAt(pos)
        menu = QMenu(self)
        if item:
            cid = item.data(0, Qt.UserRole)
            cname = item.data(0, Qt.UserRole + 1)
            if cid and cid not in ("__all__", "__recent__", "__unfiled__", "__header__"):
                menu.addAction("📁 新建子分类...", lambda: self._create_new_collection_prompt(parent_id=cid, parent_name=cname))
                menu.addAction("✏️ 重命名分类...", lambda: self._rename_collection_prompt(cid, cname))
                menu.addSeparator()
                menu.addAction("🗑️ 删除分类", lambda: self._delete_collection_prompt(cid, cname))
                menu.addSeparator()
        menu.addAction("📁 新建顶层分类...", lambda: self._create_new_collection_prompt())
        menu.exec(self.collections_tree.viewport().mapToGlobal(pos))

    def _create_new_collection_prompt(self, parent_id: str | None = None, parent_name: str | None = None) -> None:
        if not isinstance(parent_id, str):
            parent_id = None
        if not isinstance(parent_name, str):
            parent_name = None

        parent_win = self.window() or self
        title = f"在 [{parent_name}] 下新建子分类" if parent_name else "新建顶层分类"
        try:
            name, ok = QInputDialog.getText(parent_win, title, "请输入分类目录名称：")
            if ok and name.strip():
                new_cid = self.db.create_collection(name.strip(), parent_id=parent_id)
                self.current_collection_id = new_cid
                QTimer.singleShot(0, self._reload_collections_tree)
                QTimer.singleShot(0, lambda: self.refresh(keep_page=False))
        except Exception as exc:
            logger.exception("新建分类失败: %s", exc)
            QMessageBox.warning(parent_win, "创建失败", f"新建分类目录失败：{exc}")

    def _rename_collection_prompt(self, collection_id: str, old_name: str) -> None:
        parent_win = self.window() or self
        try:
            new_name, ok = QInputDialog.getText(parent_win, "重命名分类", "请输入新的分类名称：", text=old_name)
            if ok and new_name.strip() and new_name.strip() != old_name:
                self.db.rename_collection(collection_id, new_name.strip())
                QTimer.singleShot(0, self._reload_collections_tree)
        except Exception as exc:
            logger.exception("重命名分类失败: %s", exc)
            QMessageBox.warning(parent_win, "重命名失败", f"重命名分类失败：{exc}")

    def _delete_collection_prompt(self, collection_id: str, name: str) -> None:
        parent_win = self.window() or self
        try:
            reply = QMessageBox.question(
                parent_win,
                "确认删除分类",
                f"确定要删除分类目录 [{name}] 吗？\n\n注意：分类内的文献不会被删除，仅解除归档关系。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self.db.delete_collection(collection_id)
                if self.current_collection_id == collection_id:
                    self.current_collection_id = "__all__"
                QTimer.singleShot(0, self._reload_collections_tree)
                QTimer.singleShot(0, lambda: self.refresh(keep_page=False))
        except Exception as exc:
            logger.exception("删除分类失败: %s", exc)
            QMessageBox.warning(parent_win, "删除失败", f"删除分类失败：{exc}")

    def _add_docs_to_collection(self, collection_id: str, doc_ids: list[str]) -> None:
        try:
            count = self.db.add_documents_to_collection(collection_id, doc_ids)
            self.toast.emit(f"已将 {count} 篇文献归档到分类")
            QTimer.singleShot(0, self._reload_collections_tree)
            self.refresh(keep_page=True)
        except Exception as exc:
            logger.exception("归档文献到分类失败: %s", exc)
            QMessageBox.warning(self.window() or self, "归档失败", f"归档文献失败：{exc}")

    def _remove_docs_from_current_collection(self, doc_ids: list[str]) -> None:
        try:
            count = self.db.remove_documents_from_collection(self.current_collection_id, doc_ids)
            self.toast.emit(f"已从当前分类移除 {count} 篇文献")
            QTimer.singleShot(0, self._reload_collections_tree)
            self.refresh(keep_page=True)
        except Exception as exc:
            logger.exception("移除文献分类失败: %s", exc)
            QMessageBox.warning(self.window() or self, "移除失败", f"移除文献失败：{exc}")

    def _current_filtered_doc_ids(self) -> list[str] | None:
        status = self.filter_combo.currentData()
        collection_id = self.current_collection_id
        if collection_id == "__all__" and status is None:
            return None
        try:
            docs = self.db.list_documents_summary(
                collection_id=collection_id,
                download_status=status,
                limit=100000,
            )
            return [str(d["doc_id"]) for d in docs if d.get("doc_id")]
        except Exception:
            return None

    def _open_local_clean_dialog(self) -> None:
        if self.reprocess_manager is None:
            QMessageBox.warning(self, "未就绪", "离线维护管理器尚未完成初始化。")
            return
        from ui.local_clean_dialog import LocalCleanDialog

        selected = self._selected_doc_ids()
        filtered = self._current_filtered_doc_ids()
        dialog = LocalCleanDialog(
            self.reprocess_manager,
            selected_doc_ids=selected,
            filtered_doc_ids=filtered,
            total_doc_count=self.db.count_documents(),
            db=self.db,
            parent=self.window() or self,
        )
        dialog.exec()
        self.refresh(keep_page=True)
        QTimer.singleShot(0, self._reload_collections_tree)

    def refresh(self, keep_page: bool = False) -> None:
        status = self.filter_combo.currentData()
        collection_id = self.current_collection_id
        self.total_count = self.db.count_documents(
            download_status=status,
            collection_id=collection_id,
        )
        self.total_pages = max(1, math.ceil(self.total_count / self.page_size))
        if keep_page:
            self.current_page = max(1, min(self.current_page, self.total_pages))
        else:
            self.current_page = 1

        offset = (self.current_page - 1) * self.page_size
        order_by = self._get_current_order_by()
        documents = self.db.list_documents_summary(
            collection_id=collection_id,
            download_status=status,
            limit=self.page_size,
            offset=offset,
            order_by=order_by,
        )

        self._loading_table = True
        self.table.clearSpans()
        self.table.setRowCount(0)
        for row, doc in enumerate(documents):
            self.table.insertRow(row)
            doc_id = str(doc.get("doc_id") or "")
            id_item = QTableWidgetItem(doc_id)
            id_item.setData(Qt.UserRole, doc_id)
            id_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 0, id_item)

            title_item = QTableWidgetItem(doc.get("title", ""))
            title_item.setToolTip(doc.get("title", ""))
            title_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 1, title_item)

            type_item = QTableWidgetItem(doc_type_label(doc.get("doc_type")))
            type_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 2, type_item)

            year_item = QTableWidgetItem(str(doc.get("pub_year") or "—"))
            year_item.setTextAlignment(Qt.AlignCenter)
            year_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 3, year_item)

            journal_item = QTableWidgetItem(doc.get("journal") or "—")
            journal_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 4, journal_item)

            authors_text = format_authors_short(doc.get("authors"))
            authors_item = QTableWidgetItem(authors_text)
            authors_item.setToolTip(str(doc.get("authors") or ""))
            authors_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 5, authors_item)

            token_item = QTableWidgetItem(
                f"入: {int(doc.get('input_tokens') or 0):,} / "
                f"出: {int(doc.get('output_tokens') or 0):,}"
            )
            token_item.setTextAlignment(Qt.AlignCenter)
            token_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 6, token_item)

            download_item = QTableWidgetItem(download_label(doc.get("download_status")))
            color = _STATUS_COLORS.get(str(doc.get("download_status", "")).upper())
            if color:
                download_item.setForeground(QColor(color))
            download_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 7, download_item)

            parse_status = str(doc.get("parse_status", "")).upper()
            error_code = str(doc.get("parse_error_code", "")).upper()
            parse_text = "需要 OCR" if error_code == "NEEDS_OCR" else parse_label(parse_status)
            parse_item = QTableWidgetItem(parse_text)
            color = _STATUS_COLORS.get(
                "NEEDS_OCR" if error_code == "NEEDS_OCR" else parse_status
            )
            if color:
                parse_item.setForeground(QColor(color))
            parse_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 8, parse_item)

            pipe_status = compute_document_pipeline_status(
                doc,
                card_count=int(doc.get("card_count") or 0),
                has_master=bool(doc.get("has_master") or 0),
                workspace=self.workspace,
            )
            step_item = QTableWidgetItem()
            step_item.setData(Qt.UserRole, pipe_status.to_dict())
            step_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            step_item.setToolTip(self._stepper_tooltip(pipe_status))
            self.table.setItem(row, 9, step_item)

        total_all = self.db.count_documents()
        start_idx = offset + 1 if self.total_count > 0 else 0
        end_idx = min(offset + len(documents), self.total_count)
        self.count_label.setText(f"第 {self.current_page}/{self.total_pages} 页 · 当前 {len(documents)} 条 / 共 {self.total_count} 条")
        self.page_summary_label.setText(f"显示 {start_idx}-{end_idx} 条 / 共 {self.total_count} 条 (全库共 {total_all} 篇)")
        self.page_indicator_label.setText(f"第 {self.current_page} / {self.total_pages} 页")
        self.jump_spin.setRange(1, self.total_pages)
        self.jump_spin.setValue(self.current_page)
        self.first_page_button.setEnabled(self.current_page > 1)
        self.prev_page_button.setEnabled(self.current_page > 1)
        self.next_page_button.setEnabled(self.current_page < self.total_pages)
        self.last_page_button.setEnabled(self.current_page < self.total_pages)

        totals = self.db.token_totals()
        self.token_label.setText(
            f"全部文献累计消耗 Token：总输入: {totals['input_tokens']:,} | "
            f"总输出: {totals['output_tokens']:,} | 合计: {totals['total_tokens']:,}"
        )
        self._loading_table = False
        if not documents:
            self.table.setRowCount(1)
            placeholder = QTableWidgetItem("当前筛选条件下没有文献。")
            placeholder.setTextAlignment(Qt.AlignCenter)
            placeholder.setFlags(Qt.NoItemFlags)
            self.table.setItem(0, 0, placeholder)
            self.table.setSpan(0, 0, 1, 10)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.refresh_visible_pipeline_statuses()

    def _on_auto_refresh_tick(self) -> None:
        if not self.isVisible() or self._loading_table:
            return
        self.refresh_visible_pipeline_statuses()

    def refresh_visible_pipeline_statuses(self, doc_ids: Iterable[str] | None = None) -> None:
        """In-place refresh for pipeline progress, tokens, and statuses without altering document sort order."""
        if not self.db or self._loading_table:
            return
        target_ids = {str(d).strip().upper() for d in doc_ids} if doc_ids is not None else None

        row_count = self.table.rowCount()
        updated_any = False
        for row in range(row_count):
            id_item = self.table.item(row, 0)
            if not id_item:
                continue
            doc_id = str(id_item.data(Qt.UserRole) or id_item.text()).strip()
            if not doc_id:
                continue
            if target_ids is not None and doc_id.upper() not in target_ids:
                continue

            try:
                doc = self.db.get_document(doc_id)
            except Exception as exc:
                logger.debug("Failed to get document %s for status refresh: %s", doc_id, exc)
                continue
            if not doc:
                continue

            # Column 6: tokens
            token_item = self.table.item(row, 6)
            new_tokens_text = (
                f"入: {int(doc.get('input_tokens') or 0):,} / "
                f"出: {int(doc.get('output_tokens') or 0):,}"
            )
            if token_item and token_item.text() != new_tokens_text:
                token_item.setText(new_tokens_text)
                updated_any = True

            # Column 7: download status
            download_item = self.table.item(row, 7)
            new_dl_text = download_label(doc.get("download_status"))
            if download_item and download_item.text() != new_dl_text:
                download_item.setText(new_dl_text)
                color = _STATUS_COLORS.get(str(doc.get("download_status", "")).upper())
                if color:
                    download_item.setForeground(QColor(color))
                updated_any = True

            # Column 8: parse status
            parse_item = self.table.item(row, 8)
            parse_status = str(doc.get("parse_status", "")).upper()
            error_code = str(doc.get("parse_error_code", "")).upper()
            parse_text = "需要 OCR" if error_code == "NEEDS_OCR" else parse_label(parse_status)
            if parse_item and parse_item.text() != parse_text:
                parse_item.setText(parse_text)
                color = _STATUS_COLORS.get("NEEDS_OCR" if error_code == "NEEDS_OCR" else parse_status)
                if color:
                    parse_item.setForeground(QColor(color))
                updated_any = True

            # Column 9: pipeline progress (流程进度)
            pipe_status = compute_document_pipeline_status(
                doc,
                card_count=int(doc.get("card_count") or 0),
                has_master=bool(doc.get("has_master") or 0),
                workspace=self.workspace,
            )
            new_data = pipe_status.to_dict()
            step_item = self.table.item(row, 9)
            if step_item:
                old_data = step_item.data(Qt.UserRole)
                if old_data != new_data:
                    step_item.setData(Qt.UserRole, new_data)
                    step_item.setToolTip(self._stepper_tooltip(pipe_status))
                    updated_any = True
            else:
                new_step_item = QTableWidgetItem()
                new_step_item.setData(Qt.UserRole, new_data)
                new_step_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                new_step_item.setToolTip(self._stepper_tooltip(pipe_status))
                self.table.setItem(row, 9, new_step_item)
                updated_any = True

        if updated_any:
            self.table.viewport().update()

    # --------------------------------------------------------------- actions
    def _cell_double_clicked(self, row: int, column: int) -> None:
        self._open_row(row, column)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_table:
            return
        column = item.column()
        if column not in (1, 3, 4, 5):
            return
        id_item = self.table.item(item.row(), 0)
        if id_item is None:
            return
        doc_id = str(id_item.data(Qt.UserRole) or "")
        if not doc_id:
            return
        document = self.db.get_document(doc_id)
        if document is None:
            return
        updates: dict[str, Any] = {"doc_id": doc_id}
        if column == 1:
            title = item.text().strip()
            if len(title) < 8:
                self.toast.emit("标题过短，已恢复原值")
                QTimer.singleShot(0, self.refresh)
                return
            updates["title"] = title
        elif column == 3:
            raw = item.text().strip()
            if raw in ("", "—"):
                updates["pub_year"] = None
            else:
                try:
                    year = int(raw)
                except ValueError:
                    self.toast.emit("年份必须是 4 位数字")
                    QTimer.singleShot(0, self.refresh)
                    return
                if not (1800 <= year <= 2200):
                    self.toast.emit("年份超出合理范围")
                    QTimer.singleShot(0, self.refresh)
                    return
                updates["pub_year"] = year
        elif column == 4:
            value = item.text().strip()
            updates["journal"] = "" if value == "—" else value
        elif column == 5:
            value = item.text().strip()
            updates["authors"] = [a.strip() for a in value.split(",") if a.strip()] if value not in ("", "—") else []
        try:
            self.db.upsert_document(updates)
            self.toast.emit("文献信息已保存")
            self.data_changed.emit()
            QTimer.singleShot(0, lambda: self.refresh(keep_page=True))
        except Exception as exc:
            self.toast.emit(f"保存失败：{exc}")
            QTimer.singleShot(0, lambda: self.refresh(keep_page=True))

    def _selected_doc_ids(self) -> list[str]:
        ids: list[str] = []
        for row in self.table.selectionModel().selectedRows():
            item = self.table.item(row.row(), 0)
            if item is not None:
                value = str(item.data(Qt.UserRole) or item.text())
                if value:
                    ids.append(value)
        return ids

    def _set_reprocess_busy(self, busy: bool) -> None:
        buttons = [
            getattr(self, "local_clean_button", None),
            getattr(self, "step3_api_button", None),
            getattr(self, "step4_audit_button", None),
            getattr(self, "step5_deep_button", None),
        ]
        for button in buttons:
            if button is not None:
                button.setEnabled(not busy)
        self.stop_reprocess_button.setVisible(
            busy and self._reprocess_mode in ("incremental", "force")
        )
        self.stop_reprocess_button.setEnabled(busy)

    def _run_selected_step3_api(self) -> None:
        if self.reprocess_manager is None:
            self.toast.emit("AI 快速理解需要本地模型管线和文献源支持")
            return
        from ui.task_scope_dialog import TaskScopeDialog

        selected = self._selected_doc_ids()
        filtered = self._current_filtered_doc_ids()
        dialog = TaskScopeDialog(
            db=self.db,
            task_type="step3_api",
            selected_doc_ids=selected,
            filtered_doc_ids=filtered,
            total_doc_count=self.db.count_documents() if self.db else 0,
            reprocess_manager=self.reprocess_manager,
            parent=self.window() or self,
        )
        if dialog.exec() == QDialog.Accepted:
            target_ids = dialog.get_target_doc_ids()
            if target_ids is None:
                all_docs = self.db.list_documents_summary(limit=100000)
                target_ids = [str(d["doc_id"]) for d in all_docs if d.get("doc_id")]
            if not target_ids:
                self.toast.emit("未选择任何文献")
                return

            mode = dialog.get_execution_mode()
            if mode == "semi_auto":
                from ui.semi_auto_dialog import SemiAutoBatchDialog

                semi_dlg = SemiAutoBatchDialog(
                    self.db,
                    self.workspace,
                    self.pipeline,
                    target_ids,
                    config=getattr(self.pipeline, "config", None),
                    parent=self.window() or self,
                )
                semi_dlg.changed.connect(lambda: self.refresh_visible_pipeline_statuses(target_ids))
                semi_dlg.exec()
                self.refresh_visible_pipeline_statuses(target_ids)
                self.data_changed.emit()
            else:
                self._run_reprocess("incremental", target_doc_ids=target_ids, skip_confirmation=True)

    def _open_selected_step5_deep(self) -> None:
        from ui.task_scope_dialog import TaskScopeDialog

        selected = self._selected_doc_ids()
        filtered = self._current_filtered_doc_ids()
        dialog = TaskScopeDialog(
            db=self.db,
            task_type="step5_deep",
            selected_doc_ids=selected,
            filtered_doc_ids=filtered,
            total_doc_count=self.db.count_documents() if self.db else 0,
            reprocess_manager=self.reprocess_manager,
            parent=self.window() or self,
        )
        if dialog.exec() == QDialog.Accepted:
            doc_ids = dialog.get_target_doc_ids()
            if doc_ids is None:
                all_docs = self.db.list_documents_summary(limit=100000)
                doc_ids = [str(d["doc_id"]) for d in all_docs if d.get("doc_id")]
            if not doc_ids:
                self.toast.emit("未选择任何文献")
                return
            mode = dialog.get_execution_mode()
            from ui.deep_synthesis_dialog import DeepSynthesisDialog

            deep_dlg = DeepSynthesisDialog(
                self.db,
                self.workspace,
                doc_ids,
                parent=self.window() or self,
                pipeline=self.pipeline,
            )
            if mode == "auto" and hasattr(deep_dlg, "tabs"):
                deep_dlg.tabs.setCurrentIndex(1)
            deep_dlg.synthesis_completed.connect(lambda d, k: self.refresh_visible_pipeline_statuses([d] if d else doc_ids))
            deep_dlg.exec()
            self.refresh_visible_pipeline_statuses(doc_ids)
            self.data_changed.emit()

    def _open_selected_step4_audit(self) -> None:
        from ui.task_scope_dialog import TaskScopeDialog

        selected = self._selected_doc_ids()
        filtered = self._current_filtered_doc_ids()
        dialog = TaskScopeDialog(
            db=self.db,
            task_type="step4_audit",
            selected_doc_ids=selected,
            filtered_doc_ids=filtered,
            total_doc_count=self.db.count_documents() if self.db else 0,
            reprocess_manager=self.reprocess_manager,
            parent=self.window() or self,
        )
        if dialog.exec() == QDialog.Accepted:
            doc_ids = dialog.get_target_doc_ids()
            if doc_ids is None:
                all_docs = self.db.list_documents_summary(limit=100000)
                doc_ids = [str(d["doc_id"]) for d in all_docs if d.get("doc_id")]
            if not doc_ids:
                self.toast.emit("未选择任何文献")
                return
            from ui.audit_dialogs import DocumentAuditDialog

            audit_dlg = DocumentAuditDialog(
                self.db,
                self.workspace,
                doc_ids,
                parent=self.window() or self,
                pipeline=self.pipeline,
            )
            audit_dlg.exec()
            self.refresh_visible_pipeline_statuses(doc_ids)
            self.data_changed.emit()

    def _open_audit_for_doc(self, doc_id: str) -> None:
        from ui.audit_dialogs import DocumentAuditDialog

        audit_dlg = DocumentAuditDialog(
            self.db,
            self.workspace,
            [doc_id],
            parent=self.window() or self,
            pipeline=self.pipeline,
        )
        audit_dlg.exec()
        self.refresh_visible_pipeline_statuses([doc_id])
        self.data_changed.emit()

    def _open_semi_auto_force(self) -> None:
        if self.pipeline is None:
            self.toast.emit("半自动提炼需要 GUI 模型管线和本地文献源")
            return
        from ui.task_scope_dialog import TaskScopeDialog

        selected = self._selected_doc_ids()
        filtered = self._current_filtered_doc_ids()
        dialog = TaskScopeDialog(
            db=self.db,
            task_type="step3_semi",
            selected_doc_ids=selected,
            filtered_doc_ids=filtered,
            total_doc_count=self.db.count_documents() if self.db else 0,
            reprocess_manager=self.reprocess_manager,
            parent=self.window() or self,
        )
        if dialog.exec() == QDialog.Accepted:
            doc_ids = dialog.get_target_doc_ids()
            if doc_ids is None:
                all_docs = self.db.list_documents_summary(limit=100000)
                doc_ids = [str(d["doc_id"]) for d in all_docs if d.get("doc_id")]
            if not doc_ids:
                self.toast.emit("未选择任何文献")
                return
            dialog_batch = SemiAutoBatchDialog(
                self.db,
                self.workspace,
                self.pipeline,
                doc_ids,
                mode="force",
                config=self.config,
                parent=self.window() or self,
            )
            dialog_batch.changed.connect(self._on_semi_auto_changed)
            dialog_batch.exec()
            self.refresh_visible_pipeline_statuses(doc_ids)
            self.data_changed.emit()

    def _show_context_menu(self, pos: QPoint) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        item = self.table.item(row, 0)
        if not item:
            return
        doc_id = str(item.data(Qt.UserRole) or item.text())
        if not doc_id:
            return

        menu = QMenu(self)
        menu.addAction("▶ 从第 ① 步开始：重新提取文本", lambda: self._run_step_from_menu(doc_id, 1))
        menu.addAction("▶ 从第 ② 步开始：重新提取关键信息", lambda: self._run_step_from_menu(doc_id, 2))
        menu.addAction("▶ 从第 ③ 步开始：重新 AI 快速理解", lambda: self._run_step_from_menu(doc_id, 3))
        menu.addAction("▶ 从第 ④ 步开始：写入数据库", lambda: self._run_step_from_menu(doc_id, 4))
        menu.addAction("🔍 第 ④ 步：文献 AI 校对核查", lambda: self._open_audit_for_doc(doc_id))
        menu.addAction("✨ 第 ⑤ 步：启动 AI 深度归纳", lambda: self._run_step_from_menu(doc_id, 5))
        menu.addSeparator()
        menu.addAction("📄 打开文献详情工作台", lambda: self.open_document.emit(doc_id))
        menu.addAction("📖 导入BibTex自动更新文献信息", lambda: self._import_bibtex_for_doc(doc_id))
        menu.addAction("✏️ 编辑文献信息", lambda: self._edit_metadata_for_doc(doc_id))

        user_collections = self.db.list_collections()
        if user_collections:
            menu.addSeparator()
            archive_menu = menu.addMenu("📁 归档到分类")
            selected_ids = self._selected_doc_ids() or [doc_id]
            for col in user_collections:
                cid = col["collection_id"]
                cname = col["name"]
                archive_menu.addAction(
                    f"📁 {cname}",
                    lambda target_cid=cid, docs=selected_ids: self._add_docs_to_collection(target_cid, docs),
                )
        if self.current_collection_id and self.current_collection_id not in ("__all__", "__recent__", "__unfiled__"):
            selected_ids = self._selected_doc_ids() or [doc_id]
            menu.addAction(
                "❌ 从当前分类移除",
                lambda: self._remove_docs_from_current_collection(selected_ids),
            )

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _run_step_from_menu(self, doc_id: str, step: int) -> None:
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
            self.refresh_visible_pipeline_statuses([doc_id])
            self._run_reprocess("force", target_doc_ids=[doc_id], skip_confirmation=True)

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
            self.refresh_visible_pipeline_statuses([doc_id])
            self._run_reprocess("force", target_doc_ids=[doc_id], skip_confirmation=True)

        elif step == 3:
            choice = QMessageBox.question(
                self,
                "第 ③ 步：AI 快速提取",
                f"请选择 AI 快速提取的执行方式：\n\n"
                f"【Yes】：打开网页版 AI 半自动任务台（零 Token 消耗）\n"
                f"【No】：直接调用后台 API 全自动提取（消耗 Token）",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if choice == QMessageBox.Cancel:
                return
            if choice == QMessageBox.Yes:
                dialog = SemiAutoBatchDialog(
                    self.db,
                    self.workspace,
                    self.pipeline,
                    [doc_id],
                    mode="force",
                    config=self.config,
                    parent=self,
                )
                dialog.changed.connect(lambda: self.refresh_visible_pipeline_statuses([doc_id]))
                dialog.exec()
                self.refresh_visible_pipeline_statuses([doc_id])
                self.data_changed.emit()
            else:
                self._run_reprocess("incremental")

        elif step == 4:
            cards = self.db.cards_for_document(doc_id)
            if cards:
                QMessageBox.information(
                    self,
                    "第 ④ 步：写入数据库",
                    f"文献 [{doc_id}] 的 {len(cards)} 张知识卡已存在于数据库中（状态正常，冒绿光中）。",
                )
            else:
                dialog = SemiAutoBatchDialog(
                    self.db,
                    self.workspace,
                    self.pipeline,
                    [doc_id],
                    mode="force",
                    config=self.config,
                    parent=self,
                )
                dialog.changed.connect(lambda: self.refresh_visible_pipeline_statuses([doc_id]))
                dialog.exec()
                self.refresh_visible_pipeline_statuses([doc_id])
                self.data_changed.emit()

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
                    f"请先完成前 ④ 步（冒绿光）后再执行第 ⑤ 步深度归纳。",
                )
                return
            from ui.deep_synthesis_dialog import DeepSynthesisDialog
            dialog = DeepSynthesisDialog(
                self.db,
                self.workspace,
                doc_id,
                parent=self,
                pipeline=self.pipeline,
            )
            dialog.synthesis_completed.connect(lambda d, k: self.refresh_visible_pipeline_statuses([doc_id]))
            dialog.exec()
            self.refresh_visible_pipeline_statuses([doc_id])
            self.data_changed.emit()

    def _on_semi_auto_changed(self) -> None:
        self.refresh_visible_pipeline_statuses()
        self.data_changed.emit()

    def _run_reprocess(
        self,
        mode: str,
        target_doc_ids: list[str] | None = None,
        skip_confirmation: bool = False,
    ) -> None:
        if self.reprocess_manager is None:
            self.toast.emit("重处理需要 GUI 模型管线和本地文献源")
            return
        if self._reprocess_worker is not None and self._reprocess_worker.isRunning():
            self.toast.emit("已有重处理任务正在运行")
            return
        doc_ids: list[str] | None = target_doc_ids
        if mode == "force":
            if doc_ids is None:
                doc_ids = self._selected_doc_ids()
            if not doc_ids:
                self.toast.emit("请先在文献清单中选中要强制重提的文献")
                return
            if not skip_confirmation:
                tokens = self.reprocess_manager.estimate_force_tokens(doc_ids)
                answer = QMessageBox.question(
                    self,
                    "确认强制重新提炼",
                    f"强制重新提炼选中的 {len(doc_ids)} 篇文献预计将消耗约 "
                    f"{tokens:,} 输入 Token（您手动编辑过的卡片将被安全保留），是否继续？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return
        elif mode == "incremental":
            if not skip_confirmation:
                if doc_ids is None:
                    selected = self._selected_doc_ids()
                    doc_ids = selected or None
                documents = len(doc_ids) if doc_ids else self.db.count_documents()
                if documents == 0:
                    self.toast.emit("文献库为空，没有可补提的文献")
                    return
                tokens = self.reprocess_manager.estimate_incremental_tokens(doc_ids)
                scope = f"选中的 {documents} 篇文献" if doc_ids else f"全库 {documents} 篇文献"
                answer = QMessageBox.question(
                    self,
                    "确认智能增量补提",
                    f"将对{scope}执行增量补提，仅处理尚未提炼的证据包。\n"
                    "目录、安装/编译、引用等低价值片段会自动跳过。\n\n"
                    f"预计最多消耗约 {tokens:,} 输入 Token（4 路并行）。\n"
                    "扫描件或没有有效正文的 PDF 会直接标记失败，不调用模型。\n\n"
                    "任务运行中可点击「停止当前任务」，已完成部分会保留。是否继续？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return
        self._reprocess_mode = mode
        label = {
            "local": "正在执行全库本地修复与清洗（0 Token）…",
            "incremental": (
                f"正在执行智能增量补提（{len(doc_ids) if doc_ids else self.db.count_documents()} 篇），"
                "跳过已提炼片段…"
            ),
            "force": f"正在强制重新提炼 {len(doc_ids or [])} 篇文献…",
        }[mode]
        self._set_reprocess_busy(True)
        self.reprocess_status.setText(label)
        self.reprocess_progress.setValue(0)
        self.reprocess_progress.setVisible(True)
        self.reprocess_eta.setVisible(True)
        self._reprocess_eta.reset()
        self.reprocess_eta.setText("已用 不足 1 秒 · 预计剩余 计算中…")
        worker = ReprocessWorker(self.reprocess_manager, mode, doc_ids)
        worker.progress_event.connect(self._reprocess_progress)
        worker.finished_result.connect(self._reprocess_done)
        worker.failed.connect(self._reprocess_failed)
        worker.finished.connect(lambda: self._cleanup_reprocess(worker))
        self._reprocess_worker = worker
        worker.start()

    def _stop_reprocess(self) -> None:
        worker = self._reprocess_worker
        if worker is None or not worker.isRunning():
            return
        worker.cancel()
        self.stop_reprocess_button.setEnabled(False)
        self.reprocess_status.setText("正在停止，等待当前模型请求结束后保存进度…")

    def _reprocess_progress(self, label: str, fraction: float) -> None:
        self.reprocess_status.setText(label)
        self.reprocess_progress.setValue(
            max(0, min(100, int(round(float(fraction) * 100))))
        )
        self.reprocess_eta.setText(
            self._reprocess_eta.summary(float(fraction), label)
        )
        self.refresh_visible_pipeline_statuses()

    def _reprocess_done(self, result: dict) -> None:
        self._set_reprocess_busy(False)
        self.refresh_visible_pipeline_statuses()
        self.data_changed.emit()
        metadata = result.get("metadata", {}) if isinstance(result, dict) else {}
        purged = result.get("purged", {}) if isinstance(result, dict) else {}
        formulas = result.get("formulas", {}) if isinstance(result, dict) else {}
        recluster = result.get("recluster", {}) if isinstance(result, dict) else {}
        mode = self._reprocess_mode
        if mode == "local":
            message = (
                f"本地清洗完成：元数据修复 {metadata.get('repaired', 0)} 篇，"
                f"删除废卡 {purged.get('deleted', 0)} 张，"
                f"公式净化 {formulas.get('updated', 0)} 张，"
                f"聚类合并 {recluster.get('merged', 0)} 张"
            )
        elif result.get("cancelled"):
            message = (
                f"已停止：文献 {result.get('documents', 0)} 篇，"
                f"新卡 {result.get('extracted', 0)} 张；"
                "已完成部分已保存，可再次增量补提继续。"
            )
        else:
            message = (
                f"重处理完成：文献 {result.get('documents', 0)} 篇，"
                f"新卡 {result.get('extracted', 0)} 张，"
                f"跳过已提炼片段 {result.get('skipped', 0)} 个，"
                f"过滤低价值片段 {result.get('skipped_low_value', 0)} 个"
            )
        if result.get("cancelled"):
            self.reprocess_eta.setText(self._reprocess_eta.stopped_text())
        else:
            self.reprocess_progress.setValue(100)
            self.reprocess_eta.setText(self._reprocess_eta.finish_text())
        self.reprocess_status.setText(message)
        self.toast.emit(message)

    def _reprocess_failed(self, message: str) -> None:
        self._set_reprocess_busy(False)
        self.reprocess_progress.setVisible(False)
        self.reprocess_eta.setVisible(False)
        self.reprocess_status.setText(f"重处理失败：{message}")
        self.toast.emit(f"重处理失败：{message}")

    def _cleanup_reprocess(self, worker: ReprocessWorker) -> None:
        if self._reprocess_worker is worker:
            self._reprocess_worker = None
        worker.deleteLater()

    def _auto_repair_metadata(self) -> None:
        if self._auto_repair_started:
            return
        self._auto_repair_started = True
        self._repair_metadata(silent=True)

    def _repair_metadata(self, *, silent: bool = False) -> None:
        if any(isinstance(worker, MetadataRepairWorker) for worker in self._workers):
            return
        worker = MetadataRepairWorker(self.db, self.workspace)
        worker.progress_event.connect(
            lambda index, total, doc_id: self.count_label.setText(
                f"正在重新识别文献信息 {index}/{total} …"
            )
        )
        worker.finished_result.connect(
            lambda result: self._on_repair_done(result, silent=silent)
        )
        worker.failed.connect(lambda message: self.toast.emit(f"识别失败：{message}"))
        worker.finished.connect(lambda: self._cleanup(worker))
        self._workers.append(worker)
        worker.start()
        if not silent:
            self.toast.emit("正在重新识别异常文献标题与年份…")

    def _on_repair_done(self, result: dict, *, silent: bool = False) -> None:
        repaired = int(result.get("repaired") or 0)
        checked = int(result.get("checked") or 0)
        self.refresh()
        if repaired:
            self.data_changed.emit()
            self.toast.emit(f"文献信息修复完成：检查 {checked} 篇，更新 {repaired} 篇")
        elif not silent:
            self.toast.emit(f"文献信息检查完成：{checked} 篇无需修复")
        self._auto_repair_started = True

    def _open_row(self, row: int, _column: int) -> None:
        item = self.table.item(row, 0)
        if item is None:
            return
        doc_id = item.data(Qt.UserRole)
        if doc_id:
            self.open_document.emit(str(doc_id))

    def _edit_metadata_for_doc(self, doc_id: str) -> None:
        document = self.db.get_document(doc_id)
        if not document:
            self.toast.emit(f"找不到文献：{doc_id}")
            return
        from ui.dialogs import DocumentEditDialog

        dlg = DocumentEditDialog(self.db, document, parent=self.window() or self)
        if dlg.exec() == QDialog.Accepted:
            self.toast.emit(f"文献 [{doc_id}] 信息已更新")
            self.refresh()
            self.data_changed.emit()

    def _import_bibtex_for_doc(self, doc_id: str) -> None:
        document = self.db.get_document(doc_id)
        if not document:
            self.toast.emit(f"找不到文献：{doc_id}")
            return
        from ui.dialogs import BibtexImportDialog

        dlg = BibtexImportDialog(self.db, document, parent=self.window() or self)
        if dlg.exec() == QDialog.Accepted:
            self.toast.emit(f"文献 [{doc_id}] 已成功从 BibTeX 更新信息")
            self.refresh()
            self.data_changed.emit()

    def _discover(self) -> None:
        query = self.query_edit.text().strip()
        if not query:
            self.toast.emit("请先输入寻源关键词")
            return
        sources = [key for key, box in self.sources.items() if box.isChecked()]
        if not sources:
            self.toast.emit("至少选择一个寻源数据源")
            return
        worker = DiscoveryWorker(self.fetcher, query, sources)
        worker.finished_results.connect(self._on_discovered)
        worker.failed.connect(lambda message: self.toast.emit(f"寻源失败：{message}"))
        worker.finished.connect(lambda: self._cleanup(worker))
        self._workers.append(worker)
        worker.start()
        self.toast.emit("正在寻源，结果会自动进入下载队列…")

    def _on_discovered(self, doc_ids: list) -> None:
        self.toast.emit(f"寻源完成：{len(doc_ids)} 条进入下载队列")
        self.filter_combo.setCurrentIndex(1)  # 已排队
        self.refresh()
        self.data_changed.emit()

    def _browse(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要入库的 PDF", self.workspace + "/inbox_manual", "PDF (*.pdf)"
        )
        if paths:
            self._ingest_paths(paths)

    def _ingest_paths(self, paths: list[str]) -> None:
        worker = IngestWorker(self.watcher, paths)
        worker.finished_results.connect(self._on_ingested)
        worker.failed.connect(lambda message: self.toast.emit(f"入库失败：{message}"))
        worker.finished.connect(lambda: self._cleanup(worker))
        self._workers.append(worker)
        worker.start()
        self.toast.emit("正在读取 PDF 首页并匹配待下载清单…")

    def _on_ingested(self, results: list) -> None:
        created = sum(1 for item in results if item.created)
        matched = sum(1 for item in results if item.matched)
        errors = [item for item in results if item.error]
        message = f"导入完成：新建 {created} 篇，匹配 {matched} 篇"
        if errors:
            message += f"，失败 {len(errors)} 篇"
        self.toast.emit(message)
        self.refresh()
        self.data_changed.emit()

    def _scan_inbox(self) -> None:
        worker = ScanInboxWorker(self.watcher)
        worker.finished_results.connect(self._on_ingested)
        worker.failed.connect(lambda message: self.toast.emit(f"扫描失败：{message}"))
        worker.finished.connect(lambda: self._cleanup(worker))
        self._workers.append(worker)
        worker.start()

    def _export_pending(self) -> None:
        target = export_pending_html(self.db, self.workspace)
        self.toast.emit(f"已导出：{target.name}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _cleanup(self, worker: Any) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        worker.deleteLater()
