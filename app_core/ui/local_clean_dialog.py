"""Local Clean & Maintenance Dialog for AtomicCalculationDB V2.9.0.

Provides fine-grained selection of offline maintenance operations:
- Repair document metadata from PDF/Zotero filenames
- Purge truncated/invalid cards
- Sanitize LaTeX formula artefacts
- Semantic deduplication / clustering
- NLI conflict detection

Features:
- Micro-transaction safety: Every item is committed atomically; cancellation
  guarantees zero data loss and safe rollback/checkpoints.
- Granular operation checkboxes with [Select All] and [Clear All].
- Target scope: Selected documents vs Filtered results vs Entire library.
- Background QThread execution with real-time ETA and safe abort.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from PySide6.QtCore import QPoint, QTimer, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)


def _format_authors_short(authors_val: Any) -> str:
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
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]}, {names[1]}"
        return f"{names[0]} 等"
    return str(authors_val).strip() or "—"


class DocSelectDialog(QDialog):
    """Document selection dialog supporting hierarchical collections tree, point-click, box-select, and range-select."""

    def __init__(
        self,
        db: Any,
        initial_selected_ids: Iterable[str] | None = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.current_collection_id = "__all__"
        self._selected_doc_ids: set[str] = set(str(d) for d in (initial_selected_ids or []) if d)
        self._all_docs: list[dict[str, Any]] = []
        self._filtered_docs: list[dict[str, Any]] = []
        self._updating_table: bool = False
        self.total_doc_count = self.db.count_documents() if self.db else 0

        self.setWindowTitle("📋 选择维护文献 - 我的文库")
        self.resize(1020, 620)
        self.setMinimumSize(800, 480)
        self._init_ui()
        self._reload_collections_tree()
        self._load_data()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Header description
        hint = QLabel(
            "<b>文库文献多选清单</b>：左侧可按分类目录筛选文献；右侧支持鼠标<b>【点选】</b>单篇、<b>【框选】</b>按住左键拖拽连续多选、"
            "按住 Shift<b>【连选】</b>连续多篇、按住 Ctrl 增减多选、Ctrl+A 全选。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Main splitter: Left is Collections Tree (Zotero-style), Right is Table + Search Toolbar
        self.splitter = QSplitter(Qt.Horizontal)

        # Left panel: Collections Widget
        self.collections_widget = QWidget()
        col_layout = QVBoxLayout(self.collections_widget)
        col_layout.setContentsMargins(0, 0, 6, 0)
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
        self.collections_tree.setMinimumWidth(190)
        col_layout.addWidget(self.collections_tree)
        self.splitter.addWidget(self.collections_widget)

        # Right panel: Search Bar + Table
        self.table_widget = QWidget()
        table_panel_layout = QVBoxLayout(self.table_widget)
        table_panel_layout.setContentsMargins(0, 0, 0, 0)
        table_panel_layout.setSpacing(8)

        tool_row = QHBoxLayout()
        tool_row.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 快速搜索文献标题、DOC-ID、作者、期刊、年份...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search_changed)
        tool_row.addWidget(self.search_edit, 1)

        self.btn_select_all = QPushButton("✓ 全选")
        self.btn_select_all.setCursor(Qt.PointingHandCursor)
        self.btn_select_all.clicked.connect(self._select_all_rows)

        self.btn_deselect_all = QPushButton("✗ 清空选择")
        self.btn_deselect_all.setCursor(Qt.PointingHandCursor)
        self.btn_deselect_all.clicked.connect(self._deselect_all_rows)

        self.btn_invert_selection = QPushButton("🔄 反选")
        self.btn_invert_selection.setCursor(Qt.PointingHandCursor)
        self.btn_invert_selection.clicked.connect(self._invert_selection)

        tool_row.addWidget(self.btn_select_all)
        tool_row.addWidget(self.btn_deselect_all)
        tool_row.addWidget(self.btn_invert_selection)
        table_panel_layout.addLayout(tool_row)

        # Table
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "DOC-ID",
            "标题",
            "类型",
            "年份",
            "期刊",
            "作者",
            "下载 / 解析状态",
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(QHeaderView.Interactive)
        header_view.setSectionsMovable(False)

        self.table.setColumnWidth(0, 185)  # DOC-ID
        self.table.setColumnWidth(1, 280)  # 标题
        self.table.setColumnWidth(2, 60)   # 类型
        self.table.setColumnWidth(3, 60)   # 年份
        self.table.setColumnWidth(4, 130)  # 期刊
        self.table.setColumnWidth(5, 130)  # 作者
        self.table.setColumnWidth(6, 120)  # 状态
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        table_panel_layout.addWidget(self.table, 1)

        self.splitter.addWidget(self.table_widget)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([210, 780])
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter, 1)

        # Bottom bar
        bottom_row = QHBoxLayout()
        self.count_label = QLabel("已选择 0 篇文献")
        self.count_label.setStyleSheet("font-weight: bold; color: #212529;")
        bottom_row.addWidget(self.count_label)
        bottom_row.addStretch()

        self.btn_confirm = QPushButton("确定选择")
        self.btn_confirm.setCursor(Qt.PointingHandCursor)
        self.btn_confirm.setStyleSheet(
            "QPushButton { background-color: #0d6efd; color: white; font-weight: bold; padding: 6px 18px; border-radius: 4px; } "
            "QPushButton:hover { background-color: #0b5ed7; }"
        )
        self.btn_confirm.clicked.connect(self._on_confirm)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.clicked.connect(self.reject)

        bottom_row.addWidget(self.btn_confirm)
        bottom_row.addWidget(self.btn_cancel)
        layout.addLayout(bottom_row)

    # ----------------------------------------------------------- collections tree
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

            total_docs = self.db.count_documents() if self.db else 0
            self.total_doc_count = total_docs
            unfiled_docs = self.db.count_collection_documents("__unfiled__") if self.db else 0
            recent_docs = self.db.count_collection_documents("__recent__") if self.db else 0

            all_item = QTreeWidgetItem([f"🏛️ 全部文献 ({total_docs})"])
            all_item.setData(0, Qt.UserRole, "__all__")
            self.collections_tree.addTopLevelItem(all_item)

            recent_item = QTreeWidgetItem([f"⏱️ 最近添加 ({recent_docs})"])
            recent_item.setData(0, Qt.UserRole, "__recent__")
            self.collections_tree.addTopLevelItem(recent_item)

            unfiled_item = QTreeWidgetItem([f"📥 未分类文献 ({unfiled_docs})"])
            unfiled_item.setData(0, Qt.UserRole, "__unfiled__")
            self.collections_tree.addTopLevelItem(unfiled_item)

            collections = self.db.list_collections() if self.db else []
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
            self._load_data()

    def _create_new_collection_prompt(self, parent_id: str | None = None, parent_name: str | None = None) -> None:
        if not isinstance(parent_id, str):
            parent_id = None
        if not isinstance(parent_name, str):
            parent_name = None

        parent_win = self.window() or self
        title = f"在 [{parent_name}] 下新建子分类" if parent_name else "新建顶层分类"
        try:
            name, ok = QInputDialog.getText(parent_win, title, "请输入分类目录名称：")
            if ok and name.strip() and self.db:
                new_cid = self.db.create_collection(name.strip(), parent_id=parent_id)
                self.current_collection_id = new_cid
                QTimer.singleShot(0, self._reload_collections_tree)
                QTimer.singleShot(0, self._load_data)
        except Exception as exc:
            log.exception("新建分类失败: %s", exc)
            QMessageBox.warning(parent_win, "创建失败", f"新建分类目录失败：{exc}")

    def _show_collection_context_menu(self, pos: QPoint) -> None:
        item = self.collections_tree.itemAt(pos)
        menu = QMenu(self)
        if item:
            cid = item.data(0, Qt.UserRole)
            cname = item.data(0, Qt.UserRole + 1)
            if cid and cid not in ("__all__", "__recent__", "__unfiled__", "__header__"):
                menu.addAction("📁 新建子分类...", lambda: self._create_new_collection_prompt(parent_id=cid, parent_name=cname))
        menu.addAction("📁 新建顶层分类...", lambda: self._create_new_collection_prompt())
        menu.exec(self.collections_tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------- table data
    def _load_data(self) -> None:
        if self.db:
            try:
                self._all_docs = self.db.list_documents_summary(
                    collection_id=self.current_collection_id,
                    limit=100000,
                    order_by="updated_at DESC",
                )
            except Exception as e:
                log.exception("Failed to load documents summary: %s", e)
                self._all_docs = []
        else:
            self._all_docs = []
        self._apply_filter()

    def _apply_filter(self) -> None:
        query = self.search_edit.text().strip().lower()
        if not query:
            self._filtered_docs = list(self._all_docs)
        else:
            filtered = []
            for d in self._all_docs:
                title = str(d.get("title") or "").lower()
                doc_id = str(d.get("doc_id") or "").lower()
                authors = str(d.get("authors") or "").lower()
                journal = str(d.get("journal") or "").lower()
                year = str(d.get("pub_year") or "").lower()
                if (query in title or query in doc_id or query in authors or query in journal or query in year):
                    filtered.append(d)
            self._filtered_docs = filtered

        self._populate_table()

    def _populate_table(self) -> None:
        self._updating_table = True
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        first_selected_item = None

        for row, doc in enumerate(self._filtered_docs):
            self.table.insertRow(row)
            doc_id = str(doc.get("doc_id") or "")
            title = str(doc.get("title") or "（无标题）")
            doc_type = str(doc.get("doc_type") or "PAP")
            pub_year = str(doc.get("pub_year") or "—")
            journal = str(doc.get("journal") or "—")
            authors = _format_authors_short(doc.get("authors"))
            dl_stat = str(doc.get("download_status") or "")
            parse_stat = str(doc.get("parse_status") or "")
            status_text = f"{dl_stat} / {parse_stat}" if parse_stat else dl_stat

            id_item = QTableWidgetItem(doc_id)
            id_item.setData(Qt.UserRole, doc_id)
            title_item = QTableWidgetItem(title)
            title_item.setToolTip(title)
            type_item = QTableWidgetItem(doc_type)
            year_item = QTableWidgetItem(pub_year)
            journal_item = QTableWidgetItem(journal)
            authors_item = QTableWidgetItem(authors)
            status_item = QTableWidgetItem(status_text)

            self.table.setItem(row, 0, id_item)
            self.table.setItem(row, 1, title_item)
            self.table.setItem(row, 2, type_item)
            self.table.setItem(row, 3, year_item)
            self.table.setItem(row, 4, journal_item)
            self.table.setItem(row, 5, authors_item)
            self.table.setItem(row, 6, status_item)

            if doc_id in self._selected_doc_ids:
                for col in range(7):
                    item = self.table.item(row, col)
                    if item:
                        item.setSelected(True)
                if first_selected_item is None:
                    first_selected_item = id_item

        self.table.blockSignals(False)
        self._updating_table = False
        self._update_count_label()
        if first_selected_item:
            self.table.scrollToItem(first_selected_item)

    def _on_search_changed(self, _text: str) -> None:
        self._apply_filter()

    def _on_selection_changed(self) -> None:
        if self._updating_table:
            return
        # Sync visible rows selection to persistent _selected_doc_ids
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                did = item.data(Qt.UserRole) or item.text().strip()
                if did:
                    if item.isSelected():
                        self._selected_doc_ids.add(did)
                    else:
                        self._selected_doc_ids.discard(did)
        self._update_count_label()

    def _update_count_label(self) -> None:
        n_selected = len(self._selected_doc_ids)
        n_visible = self.table.rowCount()
        n_total = self.total_doc_count
        self.count_label.setText(f"已选择 {n_selected} 篇文献 (当前目录共 {n_visible} 篇 / 全库共 {n_total} 篇)")

    def _select_all_rows(self) -> None:
        self.table.selectAll()

    def _deselect_all_rows(self) -> None:
        if self.table.selectedIndexes():
            self.table.clearSelection()
        else:
            self._selected_doc_ids.clear()
            self._update_count_label()

    def _invert_selection(self) -> None:
        self._updating_table = True
        self.table.blockSignals(True)
        selected_rows = {index.row() for index in self.table.selectedIndexes()}
        for row in range(self.table.rowCount()):
            is_selected = row in selected_rows
            item0 = self.table.item(row, 0)
            did = (item0.data(Qt.UserRole) or item0.text().strip()) if item0 else None
            new_selected = not is_selected
            for col in range(self.table.columnCount()):
                item = self.table.item(row, col)
                if item:
                    item.setSelected(new_selected)
            if did:
                if new_selected:
                    self._selected_doc_ids.add(did)
                else:
                    self._selected_doc_ids.discard(did)
        self.table.blockSignals(False)
        self._updating_table = False
        self._update_count_label()

    def _on_confirm(self) -> None:
        self.accept()

    def get_selected_doc_ids(self) -> list[str]:
        return sorted(self._selected_doc_ids)


class LocalCleanThread(QThread):
    progress_signal = Signal(str, float)
    finished_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(
        self,
        reprocess_manager: Any,
        operations: list[str],
        doc_ids: list[str] | None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.reprocess_manager = reprocess_manager
        self.operations = operations
        self.doc_ids = doc_ids
        self._should_stop = False

    def stop(self) -> None:
        self._should_stop = True

    def should_stop(self) -> bool:
        return self._should_stop

    def run(self) -> None:
        try:
            def _progress(label: str, fraction: float) -> None:
                self.progress_signal.emit(str(label), float(fraction))

            result = self.reprocess_manager.local_clean(
                operations=self.operations,
                doc_ids=self.doc_ids,
                progress=_progress,
                should_stop=self.should_stop,
            )
            self.finished_signal.emit(result or {})
        except Exception as e:
            log.exception("Local clean thread error: %s", e)
            self.error_signal.emit(str(e))


class LocalCleanDialog(QDialog):
    """Configurable local maintenance dialog with safe abort."""

    def __init__(
        self,
        reprocess_manager: Any,
        *,
        selected_doc_ids: Iterable[str] | None = None,
        filtered_doc_ids: Iterable[str] | None = None,
        total_doc_count: int = 0,
        db: Any = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.reprocess_manager = reprocess_manager
        self.db = db or getattr(reprocess_manager, "db", None)
        self.selected_doc_ids = list(selected_doc_ids or [])
        self.filtered_doc_ids = list(filtered_doc_ids or []) if filtered_doc_ids is not None else None
        self.total_doc_count = total_doc_count or (self.db.count_documents() if self.db else 0)
        self.thread: LocalCleanThread | None = None
        self.result: dict[str, Any] | None = None

        self.setWindowTitle("🧹 本地数据库维护与清洗 (0 Token 消耗)")
        self.resize(600, 530)
        self.setMinimumWidth(520)
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Header description
        info_label = QLabel(
            "<b>本地离线维护引擎</b>：基于本地规则与轻量模型执行整理，完全不消耗任何 API Token。<br>"
            "<font color='#555'>所有操作均采用单项原子微事务。点击【安全中止】可立即在当前项完成后安全停止，已处理数据完好保存，绝不损坏数据库。</font>"
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        # 1. Operation selection group
        ops_group = QGroupBox("1. 选择维护工序 (可自由勾选)")
        ops_layout = QVBoxLayout(ops_group)

        # Quick select buttons
        quick_layout = QHBoxLayout()
        self.btn_select_all_ops = QPushButton("✓ 全选工序")
        self.btn_select_all_ops.clicked.connect(self._select_all_ops)
        self.btn_deselect_all_ops = QPushButton("✗ 清空工序")
        self.btn_deselect_all_ops.clicked.connect(self._deselect_all_ops)
        quick_layout.addWidget(self.btn_select_all_ops)
        quick_layout.addWidget(self.btn_deselect_all_ops)
        quick_layout.addStretch()
        ops_layout.addLayout(quick_layout)

        self.chk_metadata = QCheckBox("修复文献元数据 (从本地 PDF / DOI / Zotero 文件名校验并补全)")
        self.chk_metadata.setChecked(True)
        ops_layout.addWidget(self.chk_metadata)

        self.chk_purge = QCheckBox("清理证据截断废卡 (清理内容不完整或孤立废弃的低质卡片)")
        self.chk_purge.setChecked(True)
        ops_layout.addWidget(self.chk_purge)

        self.chk_formulas = QCheckBox("净化公式伪影与说明 (清除 LaTeX 中的多余标点与说明文本)")
        self.chk_formulas.setChecked(True)
        ops_layout.addWidget(self.chk_formulas)

        self.chk_recluster = QCheckBox("语义去重聚类与合并 (基于 MiniLM 语义嵌入合并同义卡片)")
        self.chk_recluster.setChecked(True)
        ops_layout.addWidget(self.chk_recluster)

        self.chk_conflicts = QCheckBox("争议命题冲突校验 (基于本地 NLI 模型检测命题冲突与矛盾)")
        self.chk_conflicts.setChecked(True)
        ops_layout.addWidget(self.chk_conflicts)

        layout.addWidget(ops_group)

        # 2. Scope selection group
        scope_group = QGroupBox("2. 选择维护范围")
        scope_layout = QVBoxLayout(scope_group)

        # Quick scope buttons: Select all / Clear / Open document picker
        scope_quick_row = QHBoxLayout()
        self.btn_select_all_docs = QPushButton("✓ 选择所有文献")
        self.btn_select_all_docs.setCursor(Qt.PointingHandCursor)
        self.btn_select_all_docs.clicked.connect(self._select_all_docs)

        self.btn_clear_all_docs = QPushButton("✗ 清空所有选择")
        self.btn_clear_all_docs.setCursor(Qt.PointingHandCursor)
        self.btn_clear_all_docs.clicked.connect(self._clear_all_docs)

        self.btn_open_doc_select = QPushButton("🔍 选择文献 (文库列表)...")
        self.btn_open_doc_select.setCursor(Qt.PointingHandCursor)
        self.btn_open_doc_select.clicked.connect(self._open_doc_select_dialog)

        scope_quick_row.addWidget(self.btn_select_all_docs)
        scope_quick_row.addWidget(self.btn_clear_all_docs)
        scope_quick_row.addWidget(self.btn_open_doc_select)
        scope_quick_row.addStretch()
        scope_layout.addLayout(scope_quick_row)

        self.scope_btn_group = QButtonGroup(self)

        n_selected = len(self.selected_doc_ids)
        self.radio_selected = QRadioButton(f"仅处理选中文献 (已选 {n_selected} 篇)")
        self.radio_selected.setEnabled(n_selected > 0)
        self.scope_btn_group.addButton(self.radio_selected)
        scope_layout.addWidget(self.radio_selected)

        n_filtered = len(self.filtered_doc_ids) if self.filtered_doc_ids is not None else 0
        self.radio_filtered = QRadioButton(f"处理当前筛选结果 (共 {n_filtered} 篇)")
        self.radio_filtered.setEnabled(n_filtered > 0)
        self.scope_btn_group.addButton(self.radio_filtered)
        scope_layout.addWidget(self.radio_filtered)

        self.radio_all = QRadioButton(f"处理全库所有文献 (全库共 {self.total_doc_count} 篇)")
        self.scope_btn_group.addButton(self.radio_all)
        scope_layout.addWidget(self.radio_all)

        # Default scope selection
        if n_selected > 0:
            self.radio_selected.setChecked(True)
        elif n_filtered > 0:
            self.radio_filtered.setChecked(True)
        else:
            self.radio_all.setChecked(True)

        layout.addWidget(scope_group)

        # 3. Progress and Status
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("准备就绪。请选择工序和范围后点击【开始执行】。")
        self.status_label.setStyleSheet("color: #444; font-size: 12px;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # 4. Action buttons
        btn_layout = QHBoxLayout()
        self.start_button = QPushButton("▶ 开始执行")
        self.start_button.setStyleSheet(
            "QPushButton { background-color: #0d6efd; color: white; font-weight: bold; padding: 6px 14px; border-radius: 4px; } "
            "QPushButton:hover { background-color: #0b5ed7; } "
            "QPushButton:disabled { background-color: #b0c4de; }"
        )
        self.start_button.clicked.connect(self._start_execution)

        self.stop_button = QPushButton("⏹ 立即安全中止")
        self.stop_button.setEnabled(False)
        self.stop_button.setStyleSheet(
            "QPushButton { background-color: #dc3545; color: white; font-weight: bold; padding: 6px 14px; border-radius: 4px; } "
            "QPushButton:hover { background-color: #bb2d3b; } "
            "QPushButton:disabled { background-color: #f5c2c7; color: #888; }"
        )
        self.stop_button.clicked.connect(self._stop_execution)

        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)

        btn_layout.addWidget(self.start_button)
        btn_layout.addWidget(self.stop_button)
        btn_layout.addStretch()
        btn_layout.addWidget(self.close_button)
        layout.addLayout(btn_layout)

    def _select_all_ops(self) -> None:
        self.chk_metadata.setChecked(True)
        self.chk_purge.setChecked(True)
        self.chk_formulas.setChecked(True)
        self.chk_recluster.setChecked(True)
        self.chk_conflicts.setChecked(True)

    def _deselect_all_ops(self) -> None:
        self.chk_metadata.setChecked(False)
        self.chk_purge.setChecked(False)
        self.chk_formulas.setChecked(False)
        self.chk_recluster.setChecked(False)
        self.chk_conflicts.setChecked(False)

    def _get_selected_operations(self) -> list[str]:
        ops = []
        if self.chk_metadata.isChecked():
            ops.append("metadata")
        if self.chk_purge.isChecked():
            ops.append("purge")
        if self.chk_formulas.isChecked():
            ops.append("formulas")
        if self.chk_recluster.isChecked():
            ops.append("recluster")
        if self.chk_conflicts.isChecked():
            ops.append("conflicts")
        return ops

    def _select_all_docs(self) -> None:
        if self.db:
            try:
                all_docs = self.db.list_documents_summary(limit=100000)
                self.selected_doc_ids = [str(d["doc_id"]) for d in all_docs if d.get("doc_id")]
            except Exception as e:
                log.exception("Failed to query all doc ids: %s", e)
        self.radio_all.setChecked(True)
        n = len(self.selected_doc_ids) if self.selected_doc_ids else self.total_doc_count
        self.radio_selected.setText(f"仅处理选中文献 (已选 {n} 篇)")
        self.radio_selected.setEnabled(n > 0)
        self.status_label.setText(f"已选择处理全库所有文献（全库共 {self.total_doc_count} 篇）。")

    def _clear_all_docs(self) -> None:
        self.selected_doc_ids = []
        self.radio_selected.setText("仅处理选中文献 (已选 0 篇)")
        self.radio_selected.setEnabled(False)
        if self.radio_filtered.isEnabled():
            self.radio_filtered.setChecked(True)
        else:
            self.radio_all.setChecked(True)
        self.status_label.setText("已清空文献选择。可点击【选择文献】从文库点选/连选/框选，或点击【选择所有文献】。")

    def _open_doc_select_dialog(self) -> None:
        dlg = DocSelectDialog(
            db=self.db,
            initial_selected_ids=self.selected_doc_ids,
            parent=self.window() or self,
        )
        if dlg.exec() == QDialog.Accepted:
            self.selected_doc_ids = dlg.get_selected_doc_ids()
            n = len(self.selected_doc_ids)
            self.radio_selected.setText(f"仅处理选中文献 (已选 {n} 篇)")
            self.radio_selected.setEnabled(n > 0)
            if n > 0:
                self.radio_selected.setChecked(True)
                self.status_label.setText(f"已从文库选定 {n} 篇维护文献。")
            else:
                if self.radio_filtered.isEnabled():
                    self.radio_filtered.setChecked(True)
                else:
                    self.radio_all.setChecked(True)
                self.status_label.setText("未选择任何文献。请重新选择或选择全库。")

    def _get_target_doc_ids(self) -> list[str] | None:
        if self.radio_selected.isChecked():
            return list(self.selected_doc_ids)
        if self.radio_filtered.isChecked():
            return list(self.filtered_doc_ids) if self.filtered_doc_ids is not None else None
        return None

    def _start_execution(self) -> None:
        ops = self._get_selected_operations()
        if not ops:
            self.status_label.setText("⚠️ 请至少勾选一项维护工序！")
            return

        if self.radio_selected.isChecked() and not self.selected_doc_ids:
            self.status_label.setText("⚠️ 当前选中文献数量为 0！请点击【选择文献】或【选择所有文献】。")
            return

        doc_ids = self._get_target_doc_ids()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.close_button.setEnabled(False)
        self.btn_select_all_ops.setEnabled(False)
        self.btn_deselect_all_ops.setEnabled(False)
        self.btn_select_all_docs.setEnabled(False)
        self.btn_clear_all_docs.setEnabled(False)
        self.btn_open_doc_select.setEnabled(False)
        self.chk_metadata.setEnabled(False)
        self.chk_purge.setEnabled(False)
        self.chk_formulas.setEnabled(False)
        self.chk_recluster.setEnabled(False)
        self.chk_conflicts.setEnabled(False)
        self.radio_selected.setEnabled(False)
        self.radio_filtered.setEnabled(False)
        self.radio_all.setEnabled(False)

        self.progress_bar.setValue(0)
        self.status_label.setText("正在启动本地维护引擎...")

        self.thread = LocalCleanThread(
            self.reprocess_manager,
            operations=ops,
            doc_ids=doc_ids,
            parent=self,
        )
        self.thread.progress_signal.connect(self._on_progress)
        self.thread.finished_signal.connect(self._on_finished)
        self.thread.error_signal.connect(self._on_error)
        self.thread.start()

    def _stop_execution(self) -> None:
        if self.thread and self.thread.isRunning():
            self.stop_button.setEnabled(False)
            self.status_label.setText("⚠️ 正在安全中止：等待当前单项微事务提交落盘...")
            self.thread.stop()

    def _on_progress(self, label: str, fraction: float) -> None:
        self.progress_bar.setValue(int(fraction * 100))
        self.status_label.setText(label)

    def _on_finished(self, result: dict[str, Any]) -> None:
        self.result = result
        self._reset_ui_after_run()
        if result.get("stopped"):
            self.status_label.setText("⚠️ 已安全中止。已完成修复的数据已完整落盘，未处理文献保持原状。")
        else:
            meta_repaired = result.get("metadata", {}).get("repaired", 0)
            purged = result.get("purged", {}).get("deleted", 0)
            formulas = result.get("formulas", {}).get("updated", 0)
            merged = result.get("recluster", {}).get("merged", 0)
            self.status_label.setText(
                f"✅ 本地维护清洗完成！修复元数据 {meta_repaired} 篇，"
                f"清理废卡 {purged} 张，净化公式 {formulas} 张，聚类合并 {merged} 组。"
            )

    def _on_error(self, err_msg: str) -> None:
        self._reset_ui_after_run()
        self.status_label.setText(f"❌ 执行出错: {err_msg}")

    def _reset_ui_after_run(self) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.close_button.setEnabled(True)
        self.close_button.setText("完成并关闭")
        self.btn_select_all_ops.setEnabled(True)
        self.btn_deselect_all_ops.setEnabled(True)
        self.btn_select_all_docs.setEnabled(True)
        self.btn_clear_all_docs.setEnabled(True)
        self.btn_open_doc_select.setEnabled(True)
        self.chk_metadata.setEnabled(True)
        self.chk_purge.setEnabled(True)
        self.chk_formulas.setEnabled(True)
        self.chk_recluster.setEnabled(True)
        self.chk_conflicts.setEnabled(True)
        self.radio_selected.setEnabled(len(self.selected_doc_ids) > 0)
        self.radio_filtered.setEnabled(self.filtered_doc_ids is not None and len(self.filtered_doc_ids) > 0)
        self.radio_all.setEnabled(True)

    def closeEvent(self, event: Any) -> None:
        if self.thread and self.thread.isRunning():
            self._stop_execution()
            self.thread.wait(3000)
        event.accept()
