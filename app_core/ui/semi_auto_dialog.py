"""Batch web-AI extraction workspace for one or more documents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl, Signal
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
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from models.semi_auto import (
    DEFAULT_MAX_BATCH_TOKENS,
    BatchMisalignedError,
    SemiAutoError,
    create_run,
    delete_batch,
    delete_run,
    finalize_run,
    find_unfinished_run,
    import_reply,
    read_prompt,
    semi_auto_root,
)
from ui.controls import WheelSafeSpinBox
from ui.flow_layout import clear_layout
from ui.m3_theme import doc_type_label, format_authors_short
from ui.step_progress_widget import StepProgressWidget
from models.pipeline_stepper import compute_document_pipeline_status
from ui.workers import ParseWorker


class PasteReplyDialog(QDialog):
    """Modal dialog for pasting web AI reply JSON, matching Image 4 specification."""

    def __init__(
        self,
        slot: int,
        model: str = "GPT-6.1 Sol Ultra",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"粘贴提问包 {slot} 的网页 AI 回复")
        self.resize(540, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        lbl = QLabel("请粘贴完整 JSON：")
        lbl.setStyleSheet("font-weight: 500; font-size: 13px; color: #1f2937;")
        top_row.addWidget(lbl)
        top_row.addStretch(1)

        model_lbl = QLabel("处理模型:")
        model_lbl.setStyleSheet("font-size: 12px; color: #4b5563;")
        top_row.addWidget(model_lbl)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems([
            "GPT-6.1 Sol Ultra",
            "Claude 3.7 Sonnet",
            "Gemini 2.5 Pro",
            "DeepSeek R1",
            "o3-mini",
            "GPT-4o",
        ])
        if model:
            self.model_combo.setCurrentText(str(model).strip())
        self.model_combo.setMinimumWidth(160)
        top_row.addWidget(self.model_combo)
        layout.addLayout(top_row)

        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("在此粘贴网页 AI 模型的完整 JSON 回复...")
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
        self.ok_btn = QPushButton("OK")
        self.ok_btn.setFixedSize(80, 32)
        self.ok_btn.setStyleSheet(
            "QPushButton {"
            " background-color: #1a73e8; color: #ffffff;"
            " border: none; border-radius: 16px;"
            " font-weight: bold; font-size: 13px;"
            "}"
            "QPushButton:hover { background-color: #1557b0; }"
        )
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setFixedSize(80, 32)
        self.cancel_btn.setStyleSheet(
            "QPushButton {"
            " background-color: #1a73e8; color: #ffffff;"
            " border: none; border-radius: 16px;"
            " font-weight: bold; font-size: 13px;"
            "}"
            "QPushButton:hover { background-color: #1557b0; }"
        )
        self.ok_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.ok_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

    def get_text(self) -> str:
        return self.text_edit.toPlainText().strip()

    def get_model_name(self) -> str:
        return self.model_combo.currentText().strip() or "GPT-6.1 Sol Ultra"


class SemiAutoBatchDialog(QDialog):
    """Generate, import and finally write a set of web-AI task packages."""

    changed = Signal()

    def __init__(
        self,
        db: Any,
        workspace: str | Path,
        pipeline: Any,
        doc_ids: list[str],
        *,
        mode: str = "force",
        config: Any = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.workspace = Path(workspace)
        self.pipeline = pipeline
        self.doc_ids = [str(item) for item in doc_ids if item]
        self.mode = str(mode or "force").lower()
        self.config = config
        self.manifest = find_unfinished_run(self.workspace, self.doc_ids)
        if (
            self.manifest
            and str(self.manifest.get("mode") or "").lower() != self.mode
        ):
            self.manifest = None
        self._parse_worker: ParseWorker | None = None
        self.setWindowTitle("网页 AI 半自动提炼")
        self.resize(1200, 880)
        self.setMinimumSize(960, 640)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(9)

        title = QLabel("🧠 批量文献双模 AI 理解（无 API 网页版半自动）")
        title.setObjectName("DialogTitle")
        outer.addWidget(title)

        # 1. 平铺文献清单（高宽适中，约10行显示，带垂直滚动条）
        docs = [self.db.get_document(doc_id) for doc_id in self.doc_ids] if self.db else []
        docs = [item for item in docs if item]
        summary = "、".join(
            f"{item.get('doc_id')} · {str(item.get('title') or '')[:42]}"
            for item in docs
        )
        self.doc_label = QLabel(summary or "没有可用文献")
        self.doc_label.setVisible(False)

        self.doc_box = QGroupBox(f"文献清单 (共 {len(self.doc_ids)} 篇文献)")
        self.doc_box.setObjectName("Card")
        doc_box_layout = QVBoxLayout(self.doc_box)
        doc_box_layout.setContentsMargins(10, 10, 10, 10)
        doc_box_layout.setSpacing(6)

        self.doc_table = QTableWidget(0, 7)
        self.doc_table.setObjectName("doc_table")
        self.doc_table.setHorizontalHeaderLabels([
            "DOC-ID", "标题", "类型", "年份", "期刊", "作者", "Token 消耗"
        ])
        doc_header = self.doc_table.horizontalHeader()
        doc_header.setFixedHeight(30)
        doc_header.setSectionResizeMode(0, QHeaderView.Interactive)
        doc_header.setSectionResizeMode(1, QHeaderView.Stretch)
        doc_header.setSectionResizeMode(2, QHeaderView.Interactive)
        doc_header.setSectionResizeMode(3, QHeaderView.Interactive)
        doc_header.setSectionResizeMode(4, QHeaderView.Interactive)
        doc_header.setSectionResizeMode(5, QHeaderView.Interactive)
        doc_header.setSectionResizeMode(6, QHeaderView.Interactive)
        self.doc_table.setColumnWidth(0, 180)
        self.doc_table.setColumnWidth(2, 60)
        self.doc_table.setColumnWidth(3, 60)
        self.doc_table.setColumnWidth(4, 150)
        self.doc_table.setColumnWidth(5, 120)
        self.doc_table.setColumnWidth(6, 170)

        self.doc_table.verticalHeader().setVisible(True)
        self.doc_table.verticalHeader().setDefaultSectionSize(28)
        self.doc_table.setAlternatingRowColors(True)
        self.doc_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.doc_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.doc_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.doc_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.doc_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        doc_box_layout.addWidget(self.doc_table)
        outer.addWidget(self.doc_box)
        self._populate_doc_table()

        self.stepper = StepProgressWidget(self)
        outer.addWidget(self.stepper)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(QLabel("单次提问字数上限(Tokens):"))
        self.budget = WheelSafeSpinBox()
        self.budget.setRange(4_000, 200_000)
        self.budget.setSingleStep(5_000)
        self.budget.setMaximumWidth(150)
        self.budget.setValue(self._default_budget())
        controls.addWidget(self.budget)

        controls.addWidget(QLabel("处理模型:"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems([
            "GPT-6.1 Sol Ultra",
            "Claude 3.7 Sonnet",
            "Gemini 2.5 Pro",
            "DeepSeek R1",
            "o3-mini",
            "GPT-4o",
        ])
        initial_model = "GPT-6.1 Sol Ultra"
        if self.manifest and self.manifest.get("agent_model"):
            initial_model = str(self.manifest["agent_model"]).strip()
        self.current_agent_model = initial_model
        self.model_combo.setCurrentText(initial_model)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        self.model_combo.setMinimumWidth(160)
        self.model_combo.setMaximumWidth(200)
        controls.addWidget(self.model_combo)

        self.parse_button = QPushButton("🧠 解析 / OCR 当前文献")
        self.parse_button.setObjectName("Pill")
        self.parse_button.setToolTip(
            "先调用 MinerU/PyMuPDF 生成证据包；没有文本层的 PDF 需要 MinerU OCR。"
        )
        self.parse_button.clicked.connect(self._parse_documents)
        controls.addWidget(self.parse_button)
        self.generate_button = QPushButton("📦 第一步：提取数据并生成提问包")
        self.generate_button.setObjectName("Primary")
        self.generate_button.clicked.connect(self._generate)
        controls.addWidget(self.generate_button)
        self.open_dir_button = QPushButton("📂 打开任务包目录")
        self.open_dir_button.setObjectName("Ghost")
        self.open_dir_button.clicked.connect(self._open_dir)
        controls.addWidget(self.open_dir_button)
        self.clear_button = QPushButton("🗑 清空/删除已生成提问包")
        self.clear_button.setObjectName("Ghost")
        self.clear_button.clicked.connect(self._clear)
        controls.addWidget(self.clear_button)
        controls.addStretch(1)
        outer.addLayout(controls)

        self.status_label = QLabel(
            "尚未生成提问包。提问包是纯文本证据，不需要额外上传 PDF。"
        )
        self.status_label.setObjectName("Tiny")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)
        self.parse_progress = QProgressBar()
        self.parse_progress.setRange(0, 100)
        self.parse_progress.setValue(0)
        self.parse_progress.setVisible(False)
        outer.addWidget(self.parse_progress)

        # 3. 提问包列表（带行号、约10行显示、配独立垂直滚动条）
        self.batch_box = QGroupBox("提问包列表")
        self.batch_box.setObjectName("Card")
        batch_layout = QVBoxLayout(self.batch_box)
        batch_layout.setContentsMargins(10, 10, 10, 10)
        batch_layout.setSpacing(6)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setMinimumHeight(240)

        host = QWidget()
        self.rows_layout = QVBoxLayout(host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(6)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(host)
        batch_layout.addWidget(self.scroll)
        outer.addWidget(self.batch_box, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.finalize_button = QPushButton("🚀 第二步：写入知识库")
        self.finalize_button.setObjectName("Primary")
        self.finalize_button.clicked.connect(self._finalize)
        footer.addWidget(self.finalize_button)
        self.step5_button = QPushButton("✨ AI 深度归纳 (冒金光)")
        self.step5_button.setObjectName("Warning")
        self.step5_button.setCursor(Qt.PointingHandCursor)
        self.step5_button.setToolTip("所有卡片写入知识库后，点击执行跨卡深度归纳提炼 L1 Master 卡片。")
        self.step5_button.clicked.connect(self._open_step5_deep)
        footer.addWidget(self.step5_button)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("Muted")
        self.summary_label.setWordWrap(True)
        footer.addWidget(self.summary_label, 1)
        close = QPushButton("关闭")
        close.setObjectName("Pill")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        outer.addLayout(footer)

        if self.manifest:
            self.budget.setValue(
                int(
                    self.manifest.get("max_batch_tokens")
                    or self._default_budget()
                )
            )
            self.status_label.setText(
                f"已恢复未完成任务包 {self.manifest['run_id']}。"
            )
        self._render()

    # ------------------------------------------------------------- controls
    def _on_model_changed(self, text: str) -> None:
        self.current_agent_model = text.strip() or "GPT-6.1 Sol Ultra"
        if self.manifest:
            self.manifest["agent_model"] = self.current_agent_model

    def _default_budget(self) -> int:
        if self.config is not None:
            try:
                return int(
                    self.config.get(
                        "semi_auto.max_batch_tokens",
                        DEFAULT_MAX_BATCH_TOKENS,
                    )
                )
            except (TypeError, ValueError):
                pass
        return DEFAULT_MAX_BATCH_TOKENS

    def _documents(self) -> list[dict[str, Any]]:
        documents = []
        for doc_id in self.doc_ids:
            document = self.db.get_document(doc_id)
            if document:
                documents.append(document)
        return documents

    def _parse_documents(self) -> None:
        if self.pipeline is None or self.manifest:
            return
        if self._parse_worker is not None and self._parse_worker.isRunning():
            return
        self.parse_button.setEnabled(False)
        self.generate_button.setEnabled(False)
        self.parse_progress.setValue(0)
        self.parse_progress.setVisible(True)
        self.status_label.setText(
            "正在解析/OCR；无文本层 PDF 会由 MinerU 自动 OCR，请稍候…"
        )
        worker = ParseWorker(self.pipeline, self.doc_ids, parent=self)
        worker.progress_event.connect(self._on_parse_progress)
        worker.finished_result.connect(self._on_parse_done)
        worker.failed.connect(self._on_parse_failed)
        worker.finished.connect(lambda: self._cleanup_parse(worker))
        self._parse_worker = worker
        worker.start()

    def _on_parse_progress(self, label: str, fraction: float) -> None:
        self.parse_progress.setValue(
            max(0, min(100, int(round(float(fraction) * 100))))
        )
        self.status_label.setText(label)

    def _on_parse_done(self, result: dict) -> None:
        self.parse_progress.setVisible(False)
        self._parse_worker = None
        failed = result.get("failed") or []
        parsed = int(result.get("parsed") or 0)
        bundles = int(result.get("bundles") or 0)
        parsed_ids = [
            str(item) for item in (result.get("parsed_doc_ids") or []) if item
        ]
        if parsed_ids:
            self.doc_ids = parsed_ids
        if failed:
            names = "、".join(
                str(item.get("doc_id") or "") for item in failed[:4]
            )
            self.status_label.setText(
                f"解析完成 {parsed} 篇、{bundles} 个证据包；"
                f"失败 {len(failed)} 篇：{names}。请检查 MinerU OCR/日志。"
            )
            QMessageBox.warning(
                self,
                "部分文献解析失败",
                f"{len(failed)} 篇文献没有生成证据包，无法进入提问包。\n"
                "请确认 MinerU 已启用、OCR 模式不是“仅文本层”，再重试。",
            )
        else:
            self.status_label.setText(
                f"解析完成：{parsed} 篇文献，{bundles} 个证据包。"
                "现在可以点击“生成提问包”。"
            )
        self._render()
        self.generate_button.setEnabled(bool(bundles))
        self.changed.emit()

    def _on_parse_failed(self, message: str) -> None:
        self.parse_progress.setVisible(False)
        self._parse_worker = None
        self._render()
        self.status_label.setText(f"解析失败：{message}")
        QMessageBox.warning(self, "解析失败", message)

    def _cleanup_parse(self, worker: ParseWorker) -> None:
        worker.deleteLater()

    def _generate(self) -> None:
        if self.manifest and str(self.manifest.get("status")).upper() != "FINALIZED":
            answer = QMessageBox.question(
                self,
                "重新生成提问包",
                "重新生成会删除当前尚未写入知识库的任务包和已导入回复，"
                "但不会改动知识库中的卡片。是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            delete_run(self.workspace, str(self.manifest["run_id"]))
            self.manifest = None
        if self.mode == "force":
            answer = QMessageBox.question(
                self,
                "确认半自动强制重提",
                "将为本批文献生成全量证据包。最后点击「写入知识库」时，"
                "会删除这些文献中未人工确认的旧卡，并保留 is_user_verified=1 的卡片。\n\n"
                "是否继续生成？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return
        self.budget.setEnabled(False)
        self.parse_button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.manifest = create_run(
                self.db,
                self.workspace,
                self._documents(),
                self.pipeline,
                max_batch_tokens=int(self.budget.value()),
                mode=self.mode,
            )
        except Exception as exc:
            QMessageBox.warning(self, "生成提问包失败", str(exc))
            self.manifest = None
            self.budget.setEnabled(True)
            self._render()
            self.parse_button.setEnabled(self.pipeline is not None)
            return
        finally:
            QApplication.restoreOverrideCursor()
        if self.config is not None:
            try:
                self.config.set(
                    "semi_auto.max_batch_tokens", int(self.budget.value())
                )
                self.config.save()
            except Exception:
                pass
        warning_count = len(self.manifest.get("warnings") or [])
        self.status_label.setText(
            f"已生成 {len(self.manifest.get('batches') or [])} 个提问包，"
            "请逐个复制提示词交给网页 AI，并导入回复。"
            + (
                f" 另有 {warning_count} 篇文献没有证据包，已跳过。"
                if warning_count
                else ""
            )
        )
        self._render()
        self.changed.emit()

    def _copy_prompt(self, batch: dict[str, Any]) -> None:
        if not self.manifest:
            return
        try:
            text = read_prompt(self.workspace, self.manifest, batch)
        except Exception as exc:
            QMessageBox.warning(self, "读取失败", str(exc))
            return
        QGuiApplication.clipboard().setText(text)
        self.status_label.setText(
            f"{batch['batch_id']} 的提示词已复制到剪贴板。"
        )

    def _import_file(self, batch: dict[str, Any]) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择网页 AI 回复",
            "",
            "回复文件 (*.json *.txt);;所有文件 (*)",
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "读取失败", str(exc))
            return
        self._apply_reply(batch, text, agent_model=self.current_agent_model)

    def _paste_reply(self, batch: dict[str, Any]) -> None:
        slot = int(batch.get("slot") or 1)
        dialog = PasteReplyDialog(
            slot, model=self.current_agent_model, parent=self
        )
        if dialog.exec() == QDialog.Accepted:
            text = dialog.get_text()
            if not text:
                QMessageBox.information(
                    self, "内容为空", "粘贴的内容为空，未导入任何回复。"
                )
                return
            chosen_model = dialog.get_model_name()
            self._apply_reply(batch, text, agent_model=chosen_model)

    def _copy_album_pdf(self, batch: dict[str, Any]) -> None:
        album_path = batch.get("album_pdf_path") or ""
        p = Path(album_path) if album_path else None
        if p and not p.is_absolute():
            p = self.workspace / p
        if p and p.is_file():
            from PySide6.QtCore import QMimeData, QUrl
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(p))])
            mime.setText(str(p))
            QApplication.clipboard().setMimeData(mime)
            self.status_label.setText(f"已复制图册 PDF 文件路径到剪贴板：{p.name}")
        else:
            QMessageBox.information(
                self,
                "图册 PDF 接口预留",
                "当前提问包所属文献暂无独立配图图册 PDF。\n\n"
                "（此接口已按规划预留，后续与 MinerU 提取插图流水线贯通后，"
                "可直接在此一键复制图册并粘贴到网页端 AI 发送。）",
            )

    def _apply_reply(
        self,
        batch: dict[str, Any],
        text: str,
        *,
        agent_model: str = "",
    ) -> None:
        if not self.manifest:
            return
        model_name = agent_model or self.current_agent_model
        try:
            result = import_reply(
                self.workspace,
                self.manifest,
                batch,
                text,
                agent_model=model_name,
            )
        except BatchMisalignedError as exc:
            ans = QMessageBox.question(
                self,
                "提问包错位拦截与智能纠偏",
                exc.args[0],
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if ans == QMessageBox.Yes:
                self._apply_reply(exc.target_batch, text, agent_model=model_name)
            return
        except SemiAutoError as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        if result["status"] == "IMPORTED":
            message = (
                f"{batch['batch_id']} 已导入通过 "
                f"（{result['received']} 项，解析到 {result.get('cards', 0)} 张卡 · {model_name}）。"
            )
        else:
            message = (
                f"{batch['batch_id']} 只收到 {result['received']}/"
                f"{result['expected']} 项，尚缺 {len(result['missing'])} 项；"
                "请补齐后重新导入。"
            )
        self.status_label.setText(message)
        self._render()
        self.changed.emit()

    def _delete_batch(self, batch: dict[str, Any]) -> None:
        if not self.manifest:
            return
        answer = QMessageBox.question(
            self,
            "删除提问包",
            f"确定删除 {batch['batch_id']}？其提示词和已导入回复都会移除，"
            "该操作不可撤回。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        result = delete_batch(
            self.workspace, self.manifest, str(batch["batch_id"])
        )
        self.manifest = result
        if self.manifest is None:
            self.status_label.setText("任务包已全部删除。")
        else:
            self.status_label.setText(
                f"已删除 {batch['batch_id']}，剩余 "
                f"{len(self.manifest.get('batches') or [])} 个提问包。"
            )
        self._render()
        self.changed.emit()

    def _clear(self) -> None:
        if not self.manifest:
            return
        answer = QMessageBox.question(
            self,
            "清空任务包",
            "确定删除本次全部提问包、提示词和已导入回复？"
            "知识库中已经写入的卡片不会被删除。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        delete_run(self.workspace, str(self.manifest["run_id"]))
        self.manifest = None
        self.status_label.setText("任务包已清空。")
        self._render()
        self.changed.emit()

    def _finalize(self) -> None:
        if not self.manifest:
            QMessageBox.information(self, "尚未生成", "请先生成提问包。")
            return
        batches = self.manifest.get("batches") or []
        if not batches:
            QMessageBox.information(self, "没有提问包", "当前没有可写入的提问包。")
            return
        if any(str(item.get("status")).upper() != "IMPORTED" for item in batches):
            QMessageBox.information(
                self,
                "尚未就绪",
                "请先把所有提问包的网页 AI 回复导入并通过校验。",
            )
            return
        if self.mode == "force":
            answer = QMessageBox.question(
                self,
                "确认写入知识库",
                "将删除本批文献中未人工确认的旧卡，然后写入全部网页 AI 回复。"
                "人工确认卡片会保留。是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = finalize_run(
                self.pipeline,
                self.db,
                self.workspace,
                self.manifest,
                agent_model=self.current_agent_model,
            )
        except Exception as exc:
            QMessageBox.warning(self, "写入失败", str(exc))
            QApplication.restoreOverrideCursor()
            return
        QApplication.restoreOverrideCursor()
        self.changed.emit()
        self._render()
        if result.get("already_finalized"):
            message = "这批提问包已经写入过知识库。"
            self.status_label.setText(message)
            QMessageBox.information(self, "写入完成", message)
            return
        stored = int(result.get("stored", 0))
        skipped = int(result.get("skipped_items", 0))
        empty = int(result.get("empty_items", 0))
        message = (
            f"已写入 {stored} 张卡，覆盖 {result.get('documents', 0)} 篇文献。"
        )
        if stored == 0:
            message = (
                "没有写入任何卡片。\n\n"
                f"AI 将 {skipped} 个 ITEM 标记为 skip；"
                f"{empty} 个 ITEM 有回复但没有 cards。\n"
                "请检查回复 JSON 是否为每个 ITEM 提供了 result.cards，"
                "或重新导入修正后的回复。"
            )
            self.status_label.setText(message.replace("\n", " "))
            QMessageBox.warning(self, "写入完成但无卡片", message)
            return
        self.status_label.setText(message)
        QMessageBox.information(self, "写入完成", message)

    def _open_dir(self) -> None:
        if not self.manifest:
            QMessageBox.information(self, "尚未生成", "请先生成提问包。")
            return
        path = semi_auto_root(self.workspace) / str(self.manifest["run_id"])
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # --------------------------------------------------------------- rendering
    def _populate_doc_table(self) -> None:
        if not hasattr(self, "doc_table"):
            return
        docs = [self.db.get_document(doc_id) for doc_id in self.doc_ids] if self.db else []
        docs = [item for item in docs if item]
        total_tokens = sum(
            int((item.get("input_tokens") or 0) + (item.get("output_tokens") or 0))
            for item in docs
        )
        tok_info = f" · 累计历史 Token: {total_tokens:,}" if total_tokens else ""
        if hasattr(self, "doc_box"):
            self.doc_box.setTitle(f"文献清单 (共 {len(self.doc_ids)} 篇文献{tok_info})")

        self.doc_table.setRowCount(0)
        for row, doc_id in enumerate(self.doc_ids):
            self.doc_table.insertRow(row)
            item = self.db.get_document(doc_id) if self.db else None

            # col 0: DOC-ID
            id_item = QTableWidgetItem(doc_id)
            id_item.setData(Qt.UserRole, doc_id)
            self.doc_table.setItem(row, 0, id_item)

            # col 1: 标题
            title_text = str((item.get("title") if item else None) or "—")
            title_item = QTableWidgetItem(title_text)
            title_item.setToolTip(title_text)
            self.doc_table.setItem(row, 1, title_item)

            # col 2: 类型
            doc_type = item.get("doc_type") if item else None
            type_label = doc_type_label(doc_type) if doc_type else "论文"
            type_item = QTableWidgetItem(type_label)
            type_item.setTextAlignment(Qt.AlignCenter)
            self.doc_table.setItem(row, 2, type_item)

            # col 3: 年份
            year_text = str((item.get("pub_year") if item else None) or "—")
            year_item = QTableWidgetItem(year_text)
            year_item.setTextAlignment(Qt.AlignCenter)
            self.doc_table.setItem(row, 3, year_item)

            # col 4: 期刊
            journal_text = str((item.get("journal") if item else None) or "—")
            self.doc_table.setItem(row, 4, QTableWidgetItem(journal_text))

            # col 5: 作者
            authors_text = format_authors_short(item.get("authors") if item else None)
            authors_item = QTableWidgetItem(authors_text)
            authors_item.setToolTip(str((item.get("authors") if item else "") or ""))
            self.doc_table.setItem(row, 5, authors_item)

            # col 6: Token 消耗
            in_tok = int(((item.get("input_tokens") if item else 0) or 0))
            out_tok = int(((item.get("output_tokens") if item else 0) or 0))
            token_text = f"入: {in_tok:,} / 出: {out_tok:,}" if (in_tok or out_tok) else "—"
            token_item = QTableWidgetItem(token_text)
            token_item.setTextAlignment(Qt.AlignCenter)
            self.doc_table.setItem(row, 6, token_item)

        visible_rows = min(max(len(self.doc_ids), 2), 10)
        self.doc_table.setFixedHeight(visible_rows * 28 + 36)

    def _render(self) -> None:
        self._populate_doc_table()
        clear_layout(self.rows_layout)
        self._refresh_stepper()
        self.finalize_button.setEnabled(False)
        self.clear_button.setEnabled(bool(self.manifest))
        self.open_dir_button.setEnabled(bool(self.manifest))
        self.parse_button.setEnabled(
            not self.manifest
            and self.pipeline is not None
            and not (
                self._parse_worker is not None
                and self._parse_worker.isRunning()
            )
        )
        if not self.manifest:
            self.budget.setEnabled(True)
            self.generate_button.setText("📦 第一步：生成提问包")
            self.summary_label.setText("")
            if hasattr(self, "batch_box"):
                self.batch_box.setTitle("提问包列表 (尚未生成提问包)")
            hint = QLabel(
                "尚未生成提问包。点击上方按钮按 Token 上限生成；"
                "每个提问包是一份可直接粘贴的文本提示词。"
            )
            hint.setObjectName("Muted")
            hint.setWordWrap(True)
            self.rows_layout.addWidget(hint)
            self.rows_layout.addStretch(1)
            return
        batches = list(self.manifest.get("batches") or [])
        imported = sum(
            1
            for batch in batches
            if str(batch.get("status")).upper() == "IMPORTED"
        )
        finalized = str(self.manifest.get("status")).upper() == "FINALIZED"
        self.budget.setEnabled(finalized)
        self.generate_button.setText("📦 重新生成提问包")
        title = (
            f"任务包 {self.manifest.get('run_id')} · "
            f"{len(self.manifest.get('documents') or [])} 篇文献 · "
            f"{len(batches)} 份提问包 · 已导入 {imported}/{len(batches)}"
        )
        if finalized:
            title += " · ✅ 已写入知识库"
        self.summary_label.setText(title)
        if hasattr(self, "batch_box"):
            self.batch_box.setTitle(
                f"提问包列表 (共 {len(batches)} 份提问包 · 已导入 {imported}/{len(batches)})"
            )
        if finalized:
            self.finalize_button.setText("✅ 已写入知识库")
            self.finalize_button.setEnabled(False)
        elif bool(batches) and imported == len(batches):
            self.finalize_button.setText("🚀 第二步：写入知识库")
            self.finalize_button.setEnabled(True)
        else:
            self.finalize_button.setText(
                f"⏳ 第二步：等待导入提问包 ({imported}/{len(batches)})"
            )
            self.finalize_button.setEnabled(False)
        for batch in batches:
            self.rows_layout.addWidget(self._batch_row(batch, finalized))
        self.rows_layout.addStretch(1)

    def _batch_row(self, batch: dict[str, Any], finalized: bool) -> QWidget:
        row = QFrame()
        row.setObjectName("Card")
        row.setFixedHeight(40)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 3, 10, 3)
        layout.setSpacing(7)

        slot_idx = int(batch.get("slot") or 0)
        slot_badge = QLabel(f"行号 #{slot_idx:02d}")
        slot_badge.setStyleSheet(
            "background: #e8f0fe; color: #1967d2; font-weight: bold; "
            "padding: 2px 8px; border-radius: 4px; font-size: 12px;"
        )
        layout.addWidget(slot_badge)

        status = str(batch.get("status") or "PENDING").upper()
        mark = "✅" if status == "IMPORTED" else "⚠️" if status == "PARTIAL" else "⏳"
        label = {
            "PENDING": "待导入",
            "PARTIAL": "部分导入",
            "IMPORTED": "已导入通过",
        }.get(status, status)
        doc_info = f" · {batch.get('doc_ids')[0]}" if batch.get("doc_ids") else ""
        model_str = str(batch.get("agent_model") or "").strip()
        model_badge = f" · 🤖 {model_str}" if model_str else ""
        text = QLabel(
            f"{mark} 提问包 {slot_idx:02d}{doc_info} · "
            f"{batch.get('batch_id')} · {len(batch.get('item_ids') or [])} 项 · "
            f"约 {int(batch.get('estimated_tokens') or 0):,} Token · {label}{model_badge}"
        )
        text.setMinimumWidth(320)
        layout.addWidget(text, 1)

        if status == "PARTIAL":
            warn_lbl = QLabel("请补齐缺少的 item")
            warn_lbl.setStyleSheet("color: #b42318; font-size: 11px;")
            layout.addWidget(warn_lbl)

        copy = QPushButton("📋 复制提示词")
        copy.setObjectName("Pill")
        copy.clicked.connect(
            lambda _checked=False, item=batch: self._copy_prompt(item)
        )
        layout.addWidget(copy)

        album_btn = QPushButton("📎 复制图册PDF")
        album_btn.setObjectName("Pill")
        album_btn.setToolTip("左键点击：复制图册 PDF（接口已预留，供后续图表流水线使用）")
        album_btn.clicked.connect(
            lambda _checked=False, item=batch: self._copy_album_pdf(item)
        )
        layout.addWidget(album_btn)

        if not finalized and status != "IMPORTED":
            import_file = QPushButton("📥 导入回复文件")
            import_file.setObjectName("Pill")
            import_file.clicked.connect(
                lambda _checked=False, item=batch: self._import_file(item)
            )
            layout.addWidget(import_file)
            paste = QPushButton("📝 粘贴回复文本")
            paste.setObjectName("Ghost")
            paste.clicked.connect(
                lambda _checked=False, item=batch: self._paste_reply(item)
            )
            layout.addWidget(paste)
        if not finalized:
            remove = QPushButton("🗑 删除")
            remove.setObjectName("Ghost")
            remove.setStyleSheet(
                "QPushButton { color: #b42318; background: #fce8e6; border: 1px solid #f2b8b5; border-radius: 4px; padding: 4px 10px; font-weight: bold; } "
                "QPushButton:hover { background: #f8d7da; }"
            )
            remove.clicked.connect(
                lambda _checked=False, item=batch: self._delete_batch(item)
            )
            layout.addWidget(remove)
        return row

    def _refresh_stepper(self) -> None:
        if len(self.doc_ids) == 1:
            self.stepper.setVisible(True)
            self.step5_button.setVisible(True)
            doc = self.db.get_document(self.doc_ids[0])
            if doc:
                cards = self.db.cards_for_document(self.doc_ids[0])
                status = compute_document_pipeline_status(
                    doc, cards, semi_auto_manifest=self.manifest
                )
                self.stepper.set_pipeline_status(status)
                has_cards = len(cards) > 0
                self.step5_button.setEnabled(has_cards)
        else:
            self.stepper.setVisible(False)
            self.step5_button.setVisible(False)

    def _open_step5_deep(self) -> None:
        if len(self.doc_ids) != 1:
            return
        doc_id = self.doc_ids[0]
        from ui.deep_synthesis_dialog import DeepSynthesisDialog
        dialog = DeepSynthesisDialog(
            self.db,
            self.workspace,
            doc_id,
            parent=self,
            pipeline=self.pipeline,
        )
        dialog.synthesis_completed.connect(lambda d, k: self._refresh_stepper())
        dialog.exec()
        self._refresh_stepper()
        self.changed.emit()


__all__ = ["SemiAutoBatchDialog"]
