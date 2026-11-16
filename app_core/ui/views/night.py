"""Workbench: all-hours scheduler, pipeline, conflicts and run report."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QTime, QTimer, Signal, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ui.controls import WheelSafeComboBox as QComboBox

from db.sqlite_manager import SQLiteManager
from models.web_ai_bridge import ClipboardBatchService, ClipboardPacket, build_document_packet
from ui.flow_layout import clear_layout
from ui.icon_assets import workbench_icon
from ui.m3_theme import download_label, parse_label
from ui.semi_auto_dialog import SemiAutoBatchDialog
from ui.widgets import PageHeader, SectionCard, StatCard, make_chip


class NightView(QWidget):
    open_card = Signal(str)
    toast = Signal(str)

    def __init__(
        self,
        db: SQLiteManager,
        worker: Any,
        workspace: str,
        config: Any = None,
        pipeline: Any = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.worker = worker
        self.workspace = workspace
        self.config = config
        self.pipeline = pipeline
        self._clip_service: ClipboardBatchService | None = None
        self._clip_packet: ClipboardPacket | None = None

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
            "工作台",
            "全自动文献检索与知识提取工作台；任意时间段定时启停，停止前 15 分钟自动进入跨文献深度归纳。",
        )
        refresh_button = QPushButton("刷新")
        refresh_button.setObjectName("Ghost")
        refresh_button.clicked.connect(self.refresh)
        header.add_action(refresh_button)
        layout.addWidget(header)

        hero = QFrame()
        hero.setObjectName("Card")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(22, 18, 22, 18)
        hero_layout.setSpacing(16)
        logo = QLabel()
        logo.setPixmap(workbench_icon(44).pixmap(44, 44))
        logo.setFixedSize(48, 48)
        logo.setStyleSheet("background: transparent;")
        hero_layout.addWidget(logo)
        text = QVBoxLayout()
        text.setSpacing(5)
        row = QHBoxLayout()
        row.setSpacing(8)
        title = QLabel("全自动文献知识提取引擎")
        title.setObjectName("SectionTitle")
        self.status_pill = make_chip("空闲", "StatusOff")
        row.addWidget(title)
        row.addWidget(self.status_pill)
        row.addStretch(1)
        text.addLayout(row)
        self.schedule_label = QLabel("")
        self.schedule_label.setObjectName("Muted")
        text.addWidget(self.schedule_label)
        self.queue_label = QLabel("")
        self.queue_label.setObjectName("Tiny")
        text.addWidget(self.queue_label)
        hero_layout.addLayout(text, 1)
        layout.addWidget(hero)

        # ------------------------------------------------------------- schedule
        schedule = SectionCard("定时运行时间段", "同日 / 跨夜 / 全天候均支持")
        self.schedule_enabled = QCheckBox("启用定时自动运行")
        self.schedule_enabled.setChecked(bool(self.config.get("night_shift.enabled", True)))
        schedule.body.addWidget(self.schedule_enabled)
        time_row = QHBoxLayout()
        time_row.setSpacing(8)
        time_row.addWidget(QLabel("开始"))
        self.start_time = QTimeEdit()
        self.start_time.setDisplayFormat("HH:mm")
        self.start_time.setTime(
            QTime.fromString(
                str(self.config.get("night_shift.start", "18:00")), "HH:mm"
            )
        )
        time_row.addWidget(self.start_time)
        time_row.addWidget(QLabel("结束"))
        self.end_time = QTimeEdit()
        self.end_time.setDisplayFormat("HH:mm")
        self.end_time.setTime(
            QTime.fromString(str(self.config.get("night_shift.end", "08:00")), "HH:mm")
        )
        time_row.addWidget(self.end_time)
        time_row.addStretch(1)
        save_button = QPushButton("保存并应用")
        save_button.setObjectName("Pill")
        save_button.clicked.connect(self._save_schedule)
        time_row.addWidget(save_button)
        schedule.body.addLayout(time_row)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        preset_row.addWidget(QLabel("快捷预设"))
        for label, start, end in (
            ("午休时段 12:00-14:00", "12:00", "14:00"),
            ("下午时段 14:00-18:00", "14:00", "18:00"),
            ("跨夜时段 18:00-08:00", "18:00", "08:00"),
            ("全天候持续运行", "00:00", "00:00"),
        ):
            button = QPushButton(label)
            button.setObjectName("FilterChip")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(
                lambda _checked=False, s=start, e=end: self._apply_preset(s, e)
            )
            preset_row.addWidget(button)
        preset_row.addStretch(1)
        schedule.body.addLayout(preset_row)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.start_button = QPushButton("▶ 立即启动运行")
        self.start_button.setObjectName("Primary")
        self.start_button.clicked.connect(self._start)
        self.stop_button = QPushButton("⏹ 优雅停止并生成简报")
        self.stop_button.setObjectName("Pill")
        self.stop_button.clicked.connect(self._stop)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addStretch(1)
        schedule.body.addLayout(controls)
        layout.addWidget(schedule)

        stats = QHBoxLayout()
        stats.setSpacing(12)
        self.stat_pending = StatCard("0", "待处理任务")
        self.stat_manual = StatCard("0", "待手动导入")
        self.stat_conflicts = StatCard("0", "⚡ 学术争议")
        self.stat_extracted = StatCard("0", "已解析文献")
        self.stat_tokens = StatCard("0", "Token 合计")
        for card in (
            self.stat_pending,
            self.stat_manual,
            self.stat_conflicts,
            self.stat_extracted,
            self.stat_tokens,
        ):
            stats.addWidget(card, 1)
        layout.addLayout(stats)
        self.token_detail_label = QLabel("")
        self.token_detail_label.setObjectName("SectionHint")
        layout.addWidget(self.token_detail_label)

        pipeline = SectionCard("流水线状态", "任务队列实时计数")
        self.pipeline_label = QLabel("")
        self.pipeline_label.setObjectName("Muted")
        self.pipeline_label.setWordWrap(True)
        pipeline.body.addWidget(self.pipeline_label)
        self.relay_label = QLabel("")
        self.relay_label.setObjectName("SectionHint")
        self.relay_label.setWordWrap(True)
        pipeline.body.addWidget(self.relay_label)
        layout.addWidget(pipeline)

        semi = SectionCard(
            "📋 半自动剪贴板协作台",
            "按节点容量生成任务包 → 网页 AI 回复 JSON → 导入拆包入库",
        )
        semi_row = QHBoxLayout()
        semi_row.setSpacing(8)
        semi_row.addWidget(QLabel("文献"))
        self.semi_doc_combo = QComboBox()
        self.semi_doc_combo.setMinimumWidth(360)
        semi_row.addWidget(self.semi_doc_combo, 1)
        self.semi_copy_button = QPushButton("📋 生成并复制任务包")
        self.semi_copy_button.setObjectName("Pill")
        self.semi_copy_button.clicked.connect(self._semi_copy_packet)
        self.semi_import_button = QPushButton("📥 导入剪贴板回复")
        self.semi_import_button.setObjectName("Ghost")
        self.semi_import_button.clicked.connect(self._semi_import_reply)
        self.semi_batch_button = QPushButton("📚 批量任务台")
        self.semi_batch_button.setObjectName("Primary")
        self.semi_batch_button.clicked.connect(self._open_semi_auto)
        semi_row.addWidget(self.semi_copy_button)
        semi_row.addWidget(self.semi_import_button)
        semi_row.addWidget(self.semi_batch_button)
        semi.body.addLayout(semi_row)
        self.semi_status = QLabel(
            "任务包按当前快速提取节点容量自动分包；导入后已完成片段会写入缓存。"
        )
        self.semi_status.setObjectName("Tiny")
        self.semi_status.setWordWrap(True)
        semi.body.addWidget(self.semi_status)
        layout.addWidget(semi)
        if self.pipeline is None:
            self.semi_copy_button.setEnabled(False)
            self.semi_import_button.setEnabled(False)
            self.semi_batch_button.setEnabled(False)

        self.conflicts_card = SectionCard("⚡ 学术争议与策略冲突", "跨文献深度归纳专栏")
        layout.addWidget(self.conflicts_card)

        report_card = SectionCard("工作台运行简报", "最新一份 workspace/morning_reports")
        self.report_view = QTextBrowser()
        self.report_view.setObjectName("ReportView")
        self.report_view.setMarkdown("暂无简报。工作台运行结束时会自动生成。")
        self.report_view.setMinimumHeight(260)
        report_card.body.addWidget(self.report_view)
        layout.addWidget(report_card)

        self.events_card = SectionCard("最近事件", "SQLite 运行日志")
        layout.addWidget(self.events_card)
        layout.addStretch(1)

        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.refresh()

    # ---------------------------------------------------------------- actions
    def _apply_preset(self, start: str, end: str) -> None:
        self.start_time.setTime(QTime.fromString(start, "HH:mm"))
        self.end_time.setTime(QTime.fromString(end, "HH:mm"))
        self.schedule_enabled.setChecked(True)
        self._save_schedule()

    def _save_schedule(self) -> None:
        self.config.set("night_shift.enabled", self.schedule_enabled.isChecked())
        self.config.set("night_shift.start", self.start_time.time().toString("HH:mm"))
        self.config.set("night_shift.end", self.end_time.time().toString("HH:mm"))
        try:
            self.config.save()
        except Exception as exc:
            self.toast.emit(f"保存失败：{exc}")
            return
        if self.worker.status().state in ("IDLE", "STOPPED") and self.schedule_enabled.isChecked():
            try:
                self.worker.start(force=False)
            except Exception:
                pass
        self.toast.emit("定时运行时间段已保存")
        self.refresh()

    def _start(self) -> None:
        started = self.worker.start(force=True)
        self.toast.emit("工作台已启动" if started else "工作台已经在运行")
        self.refresh()

    def _stop(self) -> None:
        self.worker.stop(graceful=True, generate_report=True)
        self.toast.emit("工作台已停止，运行简报已生成")
        self.refresh()

    def _tick(self) -> None:
        self._refresh_status_only()

    def refresh(self) -> None:
        self._refresh_status_only()
        self._refresh_semi_docs()
        self._refresh_conflicts()
        self._refresh_report()
        self._refresh_events()

    def _refresh_semi_docs(self) -> None:
        current = str(self.semi_doc_combo.currentData() or "")
        documents = self.db.list_documents(limit=500)
        self.semi_doc_combo.blockSignals(True)
        self.semi_doc_combo.clear()
        for document in documents:
            title = str(document.get("title") or "")[:70]
            self.semi_doc_combo.addItem(
                f"{document['doc_id']} · {title}", str(document["doc_id"])
            )
        if current:
            index = self.semi_doc_combo.findData(current)
            if index >= 0:
                self.semi_doc_combo.setCurrentIndex(index)
        self.semi_doc_combo.blockSignals(False)
        enabled = bool(documents) and self.pipeline is not None
        self.semi_copy_button.setEnabled(enabled)
        self.semi_import_button.setEnabled(enabled)

    def _open_semi_auto(self) -> None:
        if self.pipeline is None:
            return
        doc_id = str(self.semi_doc_combo.currentData() or "")
        if not doc_id:
            self.semi_status.setText("请先选择一篇文献。")
            return
        dialog = SemiAutoBatchDialog(
            self.db,
            self.workspace,
            self.pipeline,
            [doc_id],
            mode="force",
            config=self.config,
            parent=self,
        )
        dialog.exec()
        self.refresh()

    def _semi_copy_packet(self) -> None:
        if self.pipeline is None:
            return
        doc_id = str(self.semi_doc_combo.currentData() or "")
        document = self.db.get_document(doc_id) if doc_id else None
        if not document:
            self.semi_status.setText("请先选择一篇文献。")
            return
        try:
            service, packet = build_document_packet(
                self.db, self.workspace, document, self.pipeline
            )
        except Exception as exc:
            self.semi_status.setText(f"任务包生成失败：{exc}")
            return
        self._clip_service = service
        self._clip_packet = packet
        QGuiApplication.clipboard().setText(packet.prompt)
        self.semi_status.setText(
            f"已复制 {packet.batch.item_count} 个片段（约 "
            f"{packet.batch.input_tokens:,} 输入 Token）；导入回复后会继续下一批。"
        )

    def _semi_import_reply(self) -> None:
        if self.pipeline is None or self._clip_packet is None or self._clip_service is None:
            self.semi_status.setText("请先生成并复制任务包。")
            return
        doc_id = str(self.semi_doc_combo.currentData() or "")
        document = self.db.get_document(doc_id) if doc_id else None
        if not document:
            self.semi_status.setText("请先选择一篇文献。")
            return
        text = QGuiApplication.clipboard().text()
        if not text.strip():
            self.semi_status.setText("剪贴板为空，请先复制 AI 回复 JSON。")
            return
        try:
            result = self._clip_service.apply_reply(
                self.pipeline, document, self._clip_packet, text
            )
        except Exception as exc:
            self.semi_status.setText(f"导入失败：{exc}")
            return
        self._clip_packet = None
        self._clip_service = None
        self.refresh()
        self.semi_status.setText(
            f"导入完成：保存 {result['stored']} 张卡，当前关联 {result['cards']} 张。"
        )

    def _refresh_status_only(self) -> None:
        stats = self.db.stats()
        status = self.worker.status()
        self.stat_pending.set_value(stats.get("pending_tasks", 0))
        self.stat_manual.set_value(stats.get("manual_required", 0))
        self.stat_conflicts.set_value(stats.get("conflicts", 0))
        self.stat_extracted.set_value(stats.get("llm_extracted", 0))
        self.stat_tokens.set_value(f"{int(stats.get('total_tokens') or 0):,}")
        self.token_detail_label.setText(
            f"全部文献累计消耗 Token：总输入: {int(stats.get('input_tokens') or 0):,} | "
            f"总输出: {int(stats.get('output_tokens') or 0):,} | "
            f"合计: {int(stats.get('total_tokens') or 0):,}"
        )
        state = status.state
        active = state in ("RUNNING", "SLOW_BRAIN")
        self.status_pill.setText(
            {
                "RUNNING": "运行中",
                "SLOW_BRAIN": "深度归纳",
                "STOPPING": "停止中",
                "STOPPED": "已停止",
            }.get(state, "空闲")
        )
        self.status_pill.setObjectName("StatusOk" if active else "StatusOff")
        style = self.status_pill.style()
        style.unpolish(self.status_pill)
        style.polish(self.status_pill)
        if status.always_on:
            window_text = "全天候持续运行"
        else:
            window_text = f"每日 {status.window_start}–{status.window_end}"
        self.schedule_label.setText(
            f"{window_text} · 当前{'在' if status.in_window else '不在'}时间窗内"
            + (" · 手动强制运行" if status.force else "")
            + ("" if status.enabled else " · 定时已关闭")
        )
        self.queue_label.setText(
            f"待处理 {stats.get('pending_tasks', 0)} · 已下载 {stats.get('downloaded', 0)} · "
            f"待提炼 {stats.get('parse_status', {}).get('LIGHT_PARSED', 0)} · "
            f"已解析 {stats.get('llm_extracted', 0)} · 证据 {stats.get('evidence', 0)}"
        )
        downloads = " · ".join(
            f"{download_label(key)} {value}"
            for key, value in stats.get("download_status", {}).items()
        ) or "—"
        parses = " · ".join(
            f"{parse_label(key)} {value}"
            for key, value in stats.get("parse_status", {}).items()
        ) or "—"
        task_labels = {
            "PENDING": "待处理",
            "RUNNING": "运行中",
            "DONE": "已完成",
            "FAILED": "失败",
            "TOTAL": "总计",
            "DISCOVER": "文献寻源",
            "FETCH": "文献下载",
            "PARSE": "文献解析",
            "EXTRACT": "知识提取",
            "VECTORIZE": "向量更新",
            "CONSOLIDATE": "冲突仲裁",
            "REPORT": "运行简报",
            "SYNC": "云同步",
        }
        tasks = " · ".join(
            f"{task_labels.get(str(key).upper(), str(key))} {value}"
            for key, value in stats.get("tasks", {}).items()
        ) or "—"
        self.pipeline_label.setText(
            f"下载：{downloads}\n解析：{parses}\n任务：{tasks}"
        )
        pool = getattr(getattr(self.pipeline, "gateway", None), "pool", None)
        if pool is not None:
            self.relay_label.setText(
                "🔀 当前路由预览 · " + " · ".join(
                    f"{label}: {pool.preview(role)}"
                    for role, label in (
                        ("fast_llm", "快速提取"),
                        ("slow_llm", "深度归纳"),
                        ("vlm", "图像理解"),
                    )
                )
            )
        else:
            self.relay_label.setText("🔀 当前未启用算力池路由")
        self.start_button.setEnabled(not active)
        self.stop_button.setEnabled(active or status.force)

    def _refresh_conflicts(self) -> None:
        clear_layout(self.conflicts_card.body)
        conflicts = self.db.conflict_cards(limit=20)
        if not conflicts:
            empty = QLabel("未发现极性冲突。")
            empty.setObjectName("Muted")
            self.conflicts_card.body.addWidget(empty)
            return
        for card in conflicts:
            frame = QFrame()
            frame.setObjectName("ConflictCard")
            frame.setMinimumHeight(82)
            box = QHBoxLayout(frame)
            box.setContentsMargins(14, 10, 14, 10)
            box.setAlignment(Qt.AlignVCenter)
            text = QVBoxLayout()
            title = QLabel(f"{card['knw_id']} · {card.get('title_zh', '')}")
            title.setObjectName("ConflictTitle")
            title.setWordWrap(True)
            body = QLabel(
                f"冲突对象：{', '.join(card.get('conflict_with_knw_ids_list') or []) or '待关联'}"
                f"\n{card.get('premises_and_conditions', '')[:240]}"
            )
            body.setObjectName("Muted")
            body.setWordWrap(True)
            text.addWidget(title)
            text.addWidget(body)
            box.addLayout(text, 1)
            button = QPushButton("查看")
            button.setObjectName("Pill")
            button.clicked.connect(
                lambda _checked=False, kid=card["knw_id"]: self.open_card.emit(kid)
            )
            box.addWidget(button)
            self.conflicts_card.body.addWidget(frame)

    def _refresh_report(self) -> None:
        text = ""
        try:
            text = self.worker.reporter.read()
        except Exception:
            text = ""
        if text:
            self.report_view.setMarkdown(text)
        else:
            self.report_view.setMarkdown("暂无简报。工作台运行结束时会自动生成。")

    def _refresh_events(self) -> None:
        clear_layout(self.events_card.body)
        events = self.db.recent_events(10)
        if not events:
            empty = QLabel("还没有事件记录。")
            empty.setObjectName("Muted")
            self.events_card.body.addWidget(empty)
            return
        for event in events:
            row = QHBoxLayout()
            row.setSpacing(8)
            time_label = QLabel(str(event.get("ts", ""))[5:16].replace("T", " "))
            time_label.setObjectName("Tiny")
            time_label.setFixedWidth(90)
            row.addWidget(time_label)
            level = str(event.get("level", "INFO")).upper()
            row.addWidget(
                make_chip(level, "StatusWarn" if level in ("ERROR", "CRITICAL") else "StatusOff")
            )
            message = QLabel(event.get("message", ""))
            message.setObjectName("Muted")
            message.setWordWrap(True)
            row.addWidget(message, 1)
            self.events_card.body.addLayout(row)
