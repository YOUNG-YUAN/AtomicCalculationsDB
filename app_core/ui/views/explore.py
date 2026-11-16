"""Explore center: live search, physics-intent chips, L1 banner, L2 flow."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui.controls import WheelSafeComboBox as QComboBox

from db.hybrid_retriever import parse_intent
from db.sqlite_manager import SQLiteManager
from parser.conclusion_tagger import (
    FLAG_BOUNDARY,
    FLAG_EXISTENCE,
    FLAG_MECHANISM,
    FLAG_QUANTITATIVE,
    FLAG_RECOMMENDATION,
    FLAG_TREND,
)
from ui.flow_layout import FlowLayout, clear_layout
from ui.icon_assets import logo_pixmap
from ui.m3_theme import card_type_label, plain_snippet
from ui.widgets import (
    KnowledgeCard,
    KeywordChip,
    MasterBanner,
    PageHeader,
    ResultCard,
    SectionCard,
    make_chip,
)
from ui.workers import SearchWorker

_FLAG_CHIPS = [
    ("📊 量化", FLAG_QUANTITATIVE),
    ("📈 趋势", FLAG_TREND),
    ("⚙️ 机理", FLAG_MECHANISM),
    ("✅ 存在", FLAG_EXISTENCE),
    ("⚠️ 局限", FLAG_BOUNDARY),
    ("💡 建议", FLAG_RECOMMENDATION),
]


class ExploreView(QWidget):
    open_card = Signal(str)
    open_document = Signal(str)
    open_keyword = Signal(str)
    open_hit = Signal(object)
    seed_requested = Signal()
    toast = Signal(str)
    query_changed = Signal(str)

    def __init__(
        self,
        db: SQLiteManager,
        retriever: Any,
        workspace: str,
        config: Any = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.retriever = retriever
        self.workspace = workspace
        self.config = config
        self.query = ""
        self.flag_mask = 0
        self.status_filter = "normal"
        self.current_page = 1
        configured_page_size = 20
        if config is not None:
            try:
                configured_page_size = int(config.get("explore.page_size", 20))
            except (TypeError, ValueError):
                configured_page_size = 20
        self.page_size = configured_page_size
        self.total_results = 0
        self._generation = 0
        self._workers: list[SearchWorker] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.scroll = scroll
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("PageRoot")
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 4, 10, 18)
        layout.setSpacing(12)

        header = PageHeader(
            "探索中心",
            "L1 总纲 + L2 知识卡 + L3 原文证据；输入离子、方法、过程或任意 ID 实时检索。",
        )
        refresh_button = QPushButton("刷新")
        refresh_button.setObjectName("Ghost")
        refresh_button.clicked.connect(self.refresh)
        header.add_action(refresh_button)
        layout.addWidget(header)

        self.intent_container = QWidget()
        self.intent_container.setObjectName("FlowBox")
        self.intent_container.setAttribute(Qt.WA_TranslucentBackground, True)
        self.intent_container.setAutoFillBackground(False)
        self.intent_flow = FlowLayout(self.intent_container, margin=0, h_spacing=6, v_spacing=6)
        self.intent_container.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self.intent_container)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        filter_label = QLabel("结论类型")
        filter_label.setObjectName("Tiny")
        filter_row.addWidget(filter_label)
        self.flag_buttons: dict[int, QPushButton] = {}
        for text, flag in _FLAG_CHIPS:
            button = QPushButton(text)
            button.setObjectName("FilterChip")
            button.setCheckable(True)
            button.toggled.connect(lambda checked, value=flag: self._toggle_flag(value, checked))
            filter_row.addWidget(button)
            self.flag_buttons[flag] = button
        filter_row.addSpacing(8)
        filter_row.addWidget(QLabel("卡片状态"))
        self.status_combo = QComboBox()
        for label, value in (
            ("正常卡片", "normal"),
            ("建议删除的知识卡片", "suggested_delete"),
            ("AI 审计新增", "ai_added"),
            ("审计分歧", "divergence"),
            ("全部", "all"),
        ):
            self.status_combo.addItem(label, value)
        self.status_combo.currentIndexChanged.connect(self._on_status_filter_changed)
        filter_row.addWidget(self.status_combo)
        filter_row.addStretch(1)
        clear_button = QPushButton("清除筛选")
        clear_button.setObjectName("Ghost")
        clear_button.clicked.connect(self.clear_filters)
        filter_row.addWidget(clear_button)
        layout.addLayout(filter_row)

        self.master_container = QWidget()
        self.master_layout = QVBoxLayout(self.master_container)
        self.master_layout.setContentsMargins(0, 0, 0, 0)
        self.master_layout.setSpacing(8)
        layout.addWidget(self.master_container)

        results_header = QHBoxLayout()
        results_header.setSpacing(8)
        self.results_title = QLabel("知识卡片")
        self.results_title.setObjectName("SectionTitle")
        results_header.addWidget(self.results_title)
        self.count_label = QLabel("")
        self.count_label.setObjectName("SectionHint")
        results_header.addWidget(self.count_label)
        results_header.addStretch(1)
        hint = QLabel("点击卡片展开右侧抽屉：公式 / 代码 / L3 证据")
        hint.setObjectName("Tiny")
        results_header.addWidget(hint)
        layout.addLayout(results_header)

        self.card_container = QWidget()
        self.card_grid = QGridLayout(self.card_container)
        self.card_grid.setContentsMargins(0, 0, 0, 0)
        self.card_grid.setHorizontalSpacing(12)
        self.card_grid.setVerticalSpacing(12)
        self.card_grid.setAlignment(Qt.AlignTop)
        self.card_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._card_widgets: list[QWidget] = []
        layout.addWidget(self.card_container)

        self.page_bar = QWidget()
        page_layout = QVBoxLayout(self.page_bar)
        page_layout.setContentsMargins(0, 2, 0, 2)
        page_layout.setSpacing(6)
        page_top = QHBoxLayout()
        page_top.setSpacing(6)
        page_top.addWidget(QLabel("显示"))
        self.page_size_combo = QComboBox()
        for size in (10, 20, 30, 50, 100):
            self.page_size_combo.addItem(str(size), size)
        configured_index = self.page_size_combo.findData(self.page_size)
        self.page_size_combo.setCurrentIndex(
            configured_index if configured_index >= 0 else 1
        )
        self.page_size = int(self.page_size_combo.currentData() or 20)
        self.page_size_combo.setFixedWidth(74)
        self.page_size_combo.currentIndexChanged.connect(
            self._on_page_size_changed
        )
        page_top.addWidget(self.page_size_combo)
        self.page_range_label = QLabel("")
        self.page_range_label.setObjectName("SectionHint")
        page_top.addWidget(self.page_range_label)
        page_top.addStretch(1)
        self.page_status_label = QLabel("")
        self.page_status_label.setObjectName("Tiny")
        page_top.addWidget(self.page_status_label)
        page_layout.addLayout(page_top)
        page_bottom = QHBoxLayout()
        page_bottom.setSpacing(5)
        self.page_home_button = QPushButton("首页")
        self.page_home_button.setObjectName("PageButton")
        self.page_home_button.clicked.connect(lambda: self._go_page(1))
        self.page_prev_button = QPushButton("上一页")
        self.page_prev_button.setObjectName("PageButton")
        self.page_prev_button.clicked.connect(
            lambda: self._go_page(self.current_page - 1)
        )
        self.page_next_button = QPushButton("下一页")
        self.page_next_button.setObjectName("PageButton")
        self.page_next_button.clicked.connect(
            lambda: self._go_page(self.current_page + 1)
        )
        self.page_numbers_container = QWidget()
        self.page_numbers_layout = QHBoxLayout(self.page_numbers_container)
        self.page_numbers_layout.setContentsMargins(0, 0, 0, 0)
        self.page_numbers_layout.setSpacing(5)
        page_bottom.addWidget(self.page_home_button)
        page_bottom.addWidget(self.page_prev_button)
        page_bottom.addWidget(self.page_numbers_container)
        page_bottom.addWidget(self.page_next_button)
        page_bottom.addStretch(1)
        page_bottom.addWidget(QLabel("跳至"))
        self.page_input = QLineEdit()
        self.page_input.setFixedWidth(52)
        self.page_input.setAlignment(Qt.AlignCenter)
        self.page_input.setPlaceholderText("页码")
        self.page_validator = QIntValidator(1, 1, self.page_input)
        self.page_input.setValidator(self.page_validator)
        self.page_input.returnPressed.connect(self._jump_to_page_input)
        page_bottom.addWidget(self.page_input)
        page_bottom.addWidget(QLabel("页"))
        self.page_jump_button = QPushButton("跳转")
        self.page_jump_button.setObjectName("PageButton")
        self.page_jump_button.clicked.connect(self._jump_to_page_input)
        page_bottom.addWidget(self.page_jump_button)
        page_layout.addLayout(page_bottom)
        self.page_bar.setVisible(False)
        layout.addWidget(self.page_bar)

        self.empty_card = QFrame()
        self.empty_card.setObjectName("Card")
        empty_layout = QVBoxLayout(self.empty_card)
        empty_layout.setContentsMargins(28, 24, 28, 24)
        empty_layout.setSpacing(8)
        self.empty_logo = QLabel()
        self.empty_logo.setPixmap(logo_pixmap(96))
        self.empty_logo.setAlignment(Qt.AlignCenter)
        self.empty_logo.setStyleSheet("background: transparent;")
        self.empty_title = QLabel("数据库还是空的")
        self.empty_title.setObjectName("SectionTitle")
        self.empty_title.setAlignment(Qt.AlignCenter)
        self.empty_text = QLabel(
            "载入示例数据体验 L1/L2 卡片、冲突雷达与证据抽屉，或从文献库拖入 PDF。"
        )
        self.empty_text.setObjectName("Muted")
        self.empty_text.setAlignment(Qt.AlignCenter)
        self.empty_text.setWordWrap(True)
        self.seed_button = QPushButton("载入示例数据")
        self.seed_button.setObjectName("Primary")
        self.seed_button.clicked.connect(self.seed_requested)
        empty_layout.addWidget(self.empty_logo)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_text)
        empty_layout.addWidget(self.seed_button, 0, Qt.AlignHCenter)
        self.empty_card.setVisible(False)
        layout.addWidget(self.empty_card)

        self.keyword_card = SectionCard("受控关键词", "点击关键词即可快速筛选相关知识卡片与文献")
        self.keyword_container = QWidget()
        self.keyword_container.setObjectName("FlowBox")
        self.keyword_container.setAttribute(Qt.WA_TranslucentBackground, True)
        self.keyword_container.setAutoFillBackground(False)
        self.keyword_flow = FlowLayout(self.keyword_container, margin=0, h_spacing=8, v_spacing=8)
        self.keyword_container.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.keyword_card.body.addWidget(self.keyword_container)
        self.keyword_container.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(self.keyword_card)
        layout.addStretch(1)

        # Requested order: keywords -> pagination -> result cards.  Both
        # controls are above the card grid so the reading flow stays stable
        # while paging through a large library.
        layout.removeWidget(self.keyword_card)
        layout.removeWidget(self.page_bar)
        header_index = layout.indexOf(results_header)
        layout.insertWidget(header_index, self.keyword_card)
        layout.insertWidget(header_index + 1, self.page_bar)

        self.refresh_keywords()
        QTimer.singleShot(0, self.refresh)

    # ---------------------------------------------------------------- search
    def submit_query(self, query: str) -> None:
        self.query = query.strip()
        self._run_search()

    def refresh(self) -> None:
        self.refresh_keywords()
        if hasattr(self.retriever, "clear_search_cache"):
            self.retriever.clear_search_cache()
        self._run_search()

    def _run_search(self, *, reset_page: bool = True) -> None:
        if reset_page:
            self.current_page = 1
        self._generation += 1
        generation = self._generation
        filters: dict[str, Any] = {}
        if self.flag_mask:
            filters["conclusion_flags"] = self.flag_mask
        if self.status_filter == "suggested_delete":
            filters["audit_delete_suggested"] = 1
            filters["include_hidden"] = True
        elif self.status_filter == "ai_added":
            filters["audit_status"] = "AI_ADDED"
            filters["include_hidden"] = True
        elif self.status_filter == "divergence":
            filters["audit_divergence"] = 1
            filters["audit_hidden"] = 0
        elif self.status_filter == "all":
            filters["include_hidden"] = True
        else:
            filters["audit_hidden"] = 0
        if not self.query and not self.db.count_knowledge_cards():
            self._show_empty_database()
            return
        self.count_label.setText("检索中…")
        self.page_bar.setEnabled(False)
        worker = SearchWorker(
            self.retriever,
            self.query,
            extra_filters=filters,
            page=self.current_page,
            page_size=self.page_size,
        )
        worker.finished_page.connect(
            lambda hits, total, gen=generation: self._apply_page(
                gen, hits, total
            )
        )
        worker.failed.connect(
            lambda message, gen=generation: self._apply_error(gen, message)
        )
        worker.finished.connect(lambda: self._cleanup_worker(worker))
        self._workers.append(worker)
        worker.start()

    def _cleanup_worker(self, worker: SearchWorker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        worker.deleteLater()

    def _apply_page(self, generation: int, hits: list, total: int) -> None:
        if generation != self._generation:
            return
        self.total_results = max(0, int(total))
        pages = self._page_count()
        if self.total_results and self.current_page > pages:
            self.current_page = pages
            self._run_search(reset_page=False)
            return
        self._render_hits(hits)
        self._update_pagination(hits)
        self.page_bar.setEnabled(True)
        QTimer.singleShot(0, self._scroll_to_top)

    def _render_hits(self, hits: list) -> None:
        clear_layout(self.master_layout)
        clear_layout(self.card_grid)
        self._card_widgets = []
        masters = [
            hit
            for hit in hits
            if hit.kind == "master"
            or (hit.card or {}).get("card_level") == "L1"
        ]
        # Keep the two most prominent master banners, but never silently drop
        # additional L1 hits: render the rest as ordinary cards in page order.
        banner_hits = masters[:2]
        banner_ids = {hit.id for hit in banner_hits}
        others = [hit for hit in hits if hit.id not in banner_ids]
        for hit in banner_hits:
            card = hit.card or {}
            banner = MasterBanner(card)
            banner.opened.connect(self.open_card)
            self.master_layout.addWidget(banner)
        self.master_container.setVisible(bool(masters))
        for hit in others:
            if hit.card:
                card = KnowledgeCard(hit.card)
                card.opened.connect(self.open_card)
                self._card_widgets.append(card)
            else:
                result = ResultCard(hit)
                result.opened.connect(self.open_hit)
                self._card_widgets.append(result)
        self._relayout_cards()
        self.card_container.setVisible(bool(others))
        self.empty_card.setVisible(not hits and not self.total_results)
        if not hits and not self.total_results:
            self.empty_title.setText("没有匹配结果")
            self.empty_text.setText(
                "试试更短的英文术语、物理坐标（Fe XVII MCDHF），或完整的 DOC-/KNW-/MASTER- ID。"
            )
            self.seed_button.setVisible(False)
        self._update_intent_chips()
        self._sync_flow_heights()
        QTimer.singleShot(0, self._sync_flow_heights)

    def _page_count(self) -> int:
        return max(
            1,
            (max(0, self.total_results) + self.page_size - 1) // self.page_size,
        )

    @staticmethod
    def _page_window(current: int, pages: int, span: int = 9) -> list[int]:
        """Return up to ``span`` page numbers centered on the current page."""
        pages = max(1, int(pages))
        current = max(1, min(int(current), pages))
        span = max(1, min(int(span), pages))
        if pages <= span:
            return list(range(1, pages + 1))
        half = span // 2
        start = max(1, min(current - half, pages - span + 1))
        return list(range(start, start + span))

    def _update_pagination(self, hits: list) -> None:
        total = self.total_results
        if total <= 0:
            self.page_bar.setVisible(False)
            self.count_label.setText("0 条结果")
            return
        pages = self._page_count()
        self.current_page = max(1, min(self.current_page, pages))
        offset = (self.current_page - 1) * self.page_size
        start = min(total, offset + 1)
        end = min(total, offset + len(hits))
        masters = sum(
            1
            for hit in hits
            if hit.kind == "master"
            or (hit.card or {}).get("card_level") == "L1"
        )
        self.count_label.setText(
            f"{start}–{end} / 共 {total:,} 条 · "
            f"本页 L1 {masters} · L2 {max(0, len(hits) - masters)}"
        )
        self.page_range_label.setText(f"{start}–{end} / 共 {total:,} 条")
        self.page_status_label.setText(
            f"第 {self.current_page} / {pages} 页"
        )
        self.page_bar.setVisible(True)
        self.page_home_button.setEnabled(self.current_page > 1)
        self.page_prev_button.setEnabled(self.current_page > 1)
        self.page_next_button.setEnabled(self.current_page < pages)
        self.page_validator.setRange(1, pages)
        if not self.page_input.hasFocus():
            self.page_input.setText(str(self.current_page))
        clear_layout(self.page_numbers_layout)
        for number in self._page_window(self.current_page, pages):
            button = QPushButton(str(number))
            button.setObjectName(
                "PageActive" if number == self.current_page else "PageButton"
            )
            button.setCursor(Qt.PointingHandCursor)
            if number != self.current_page:
                button.clicked.connect(
                    lambda _checked=False, target=number: self._go_page(target)
                )
            self.page_numbers_layout.addWidget(button)

    def _on_page_size_changed(self, _index: int = -1) -> None:
        size = int(self.page_size_combo.currentData() or 20)
        if size == self.page_size:
            return
        self.page_size = size
        if self.config is not None:
            try:
                self.config.set("explore.page_size", size)
                self.config.save()
            except Exception:
                pass
        self.current_page = 1
        self._run_search(reset_page=True)

    def _go_page(self, page: int) -> None:
        pages = self._page_count()
        target = max(1, min(int(page), pages))
        if target == self.current_page and self.total_results:
            return
        self.current_page = target
        self._run_search(reset_page=False)

    def _jump_to_page_input(self) -> None:
        text = self.page_input.text().strip()
        if not text:
            return
        try:
            page = int(text)
        except ValueError:
            self.toast.emit("请输入有效页码")
            return
        self._go_page(page)

    def _scroll_to_top(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.minimum())

    def _apply_error(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        self.total_results = 0
        self.page_bar.setVisible(False)
        self.count_label.setText("检索失败")
        self.toast.emit(f"检索失败：{message}")

    def _show_empty_database(self) -> None:
        clear_layout(self.master_layout)
        clear_layout(self.card_grid)
        self._card_widgets = []
        self.total_results = 0
        self.master_container.setVisible(False)
        self.card_container.setVisible(False)
        self.page_bar.setVisible(False)
        self.empty_card.setVisible(True)
        self.empty_title.setText("数据库还是空的")
        self.empty_text.setText(
            "载入示例数据体验 L1/L2 卡片、冲突雷达与证据抽屉，或从文献库拖入 PDF。"
        )
        self.seed_button.setVisible(True)
        self.count_label.setText("0 条结果")
        self._update_intent_chips()

    # ----------------------------------------------------------------- chips
    def _toggle_flag(self, flag: int, checked: bool) -> None:
        if checked:
            self.flag_mask |= flag
        else:
            self.flag_mask &= ~flag
        self._run_search()

    def _on_status_filter_changed(self, _index: int = -1) -> None:
        self.status_filter = str(self.status_combo.currentData() or "normal")
        self.current_page = 1
        self.refresh_keywords()
        self._run_search(reset_page=True)

    def clear_filters(self) -> None:
        for flag, button in self.flag_buttons.items():
            button.blockSignals(True)
            button.setChecked(False)
            button.blockSignals(False)
        self.flag_mask = 0
        self.status_combo.blockSignals(True)
        self.status_combo.setCurrentIndex(0)
        self.status_combo.blockSignals(False)
        self.status_filter = "normal"
        self._run_search()

    def _update_intent_chips(self) -> None:
        clear_layout(self.intent_flow)
        intent = parse_intent(self.query)
        labels = intent.chip_labels()
        if self.flag_mask:
            labels.append(f"结论类型 mask={self.flag_mask}")
        for label in labels:
            chip = make_chip(label, "IntentChip")
            self.intent_flow.addWidget(chip)
        self.intent_container.setVisible(bool(labels))
        self._sync_flow_heights()

    # -------------------------------------------------------------- keywords
    def refresh_keywords(self) -> None:
        clear_layout(self.keyword_flow)
        keywords = self.db.list_keywords(limit=18)
        for keyword in keywords:
            chip = KeywordChip(keyword)
            chip.clicked.connect(
                lambda _checked=False, term=keyword.get("term_en", ""): self._keyword_click(term)
            )
            self.keyword_flow.addWidget(chip)
        if not keywords:
            empty = QLabel("还没有关键词；提取文献后会自动积累。")
            empty.setObjectName("Muted")
            self.keyword_flow.addWidget(empty)
        self.keyword_card.setVisible(bool(keywords))
        QTimer.singleShot(0, self._sync_flow_heights)

    def _keyword_click(self, term: str) -> None:
        self.query = term
        self.query_changed.emit(term)
        self._run_search()

    # --------------------------------------------------------------- layout
    def _grid_columns(self) -> int:
        width = self.card_container.width() or max(360, self.width() - 150)
        return max(1, min(4, (max(300, width) + 12) // (312 + 12)))

    def _relayout_cards(self) -> None:
        """Reflow fixed-height cards in a strict QGridLayout.

        Every card occupies exactly one row/column cell, so a long title can
        never make the first card span two visual rows.
        """
        while self.card_grid.count():
            self.card_grid.takeAt(0)
        for card in self._card_widgets:
            card.hide()
        for column in range(4):
            self.card_grid.setColumnStretch(column, 0)
        for row in range(self.card_grid.rowCount()):
            self.card_grid.setRowStretch(row, 0)
        if not self._card_widgets:
            return
        columns = self._grid_columns()
        for column in range(columns):
            self.card_grid.setColumnStretch(column, 1)
        for index, card in enumerate(self._card_widgets):
            row = index // columns
            column = index % columns
            self.card_grid.addWidget(
                card, row, column, 1, 1, Qt.AlignTop
            )
            card.show()
        self.card_grid.setRowStretch(
            (len(self._card_widgets) - 1) // columns + 1, 1
        )

    def _sync_flow_heights(self) -> None:
        fallback = max(360, self.width() - 150)
        for container, flow in (
            (self.keyword_container, self.keyword_flow),
            (self.intent_container, self.intent_flow),
        ):
            width = container.width() or fallback
            container.setMinimumHeight(max(0, flow.heightForWidth(width)))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        QTimer.singleShot(0, self._sync_flow_heights)
        QTimer.singleShot(0, self._relayout_cards)
