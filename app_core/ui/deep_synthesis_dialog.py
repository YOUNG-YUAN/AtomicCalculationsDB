"""Deep Synthesis Dialog — Step 5 (L1 Master Card Synthesis) across Web AI, Codex & Antigravity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from models.deep_synthesis import (
    apply_master_synthesis_payload,
    export_master_synthesis_prompt,
    synthesize_with_agent_cli,
)
from models.endpoint_pool import EndpointPool
from ui.step_progress_widget import MiniStepProgressWidget


class CodexWorker(QThread):
    finished_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(
        self,
        db: Any,
        qdrant: Any,
        workspace: Path,
        doc_id: str,
        node: Any,
    ):
        super().__init__()
        self.db = db
        self.qdrant = qdrant
        self.workspace = workspace
        self.doc_id = doc_id
        self.node = node

    def run(self) -> None:
        try:
            res = synthesize_with_agent_cli(
                self.db,
                self.qdrant,
                self.workspace,
                self.doc_id,
                self.node,
            )
            self.finished_signal.emit(res)
        except Exception as exc:
            self.error_signal.emit(str(exc))


class DeepSynthesisDialog(QDialog):
    """Step 5 Deep Synthesis Dialog supporting Web AI, Codex, and Antigravity."""

    synthesis_completed = Signal(str, str)  # doc_id, knw_id

    def __init__(
        self,
        db: Any,
        workspace: str | Path,
        doc_id: str | list[str],
        parent: QWidget | None = None,
        *,
        pipeline: Any = None,
    ):
        super().__init__(parent)
        self.db = db
        self.workspace = Path(workspace)
        if isinstance(doc_id, list):
            self.doc_ids = [str(d) for d in doc_id if d]
            self.doc_id = self.doc_ids[0] if self.doc_ids else ""
        else:
            self.doc_ids = [str(doc_id)] if doc_id else []
            self.doc_id = str(doc_id) if doc_id else ""
        self.pipeline = pipeline
        self.qdrant = getattr(pipeline, "qdrant", None)

        self._export_info: dict[str, Any] | None = None
        self._codex_worker: CodexWorker | None = None

        self.setWindowTitle(f"文献 [{self.doc_id}] 深度归纳工作台 · 进阶第 ⑤ 步 (冒金光)")
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowMinMaxButtonsHint
            | Qt.WindowCloseButtonHint
        )
        self.resize(760, 620)
        self.setMinimumSize(640, 500)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        # 1. Header with Document Title & Mini Stepper
        header_card = QFrame()
        header_card.setObjectName("SurfaceCard")
        h_layout = QVBoxLayout(header_card)
        h_layout.setContentsMargins(12, 10, 12, 10)
        h_layout.setSpacing(6)

        title_row = QHBoxLayout()
        self.doc_title_label = QLabel(f"文献：{self.doc_id}")
        self.doc_title_label.setObjectName("SectionTitle")
        self.doc_title_label.setWordWrap(True)

        if len(self.doc_ids) > 1:
            title_row.addWidget(QLabel("选择文献："), 0)
            self.doc_combo = QComboBox()
            self.doc_combo.setMinimumWidth(340)
            for did in self.doc_ids:
                d_obj = self.db.get_document(did) if self.db else None
                t_str = (d_obj.get("title") if d_obj else None) or did
                self.doc_combo.addItem(f"[{did}] {t_str[:40]}", did)
            self.doc_combo.currentIndexChanged.connect(self._on_doc_combo_changed)
            title_row.addWidget(self.doc_combo, 1)
        else:
            self.doc_combo = None
            title_row.addWidget(self.doc_title_label, 1)

        self.mini_stepper = MiniStepProgressWidget()
        title_row.addWidget(self.mini_stepper, 0)
        h_layout.addLayout(title_row)

        self.meta_summary_label = QLabel("正在加载卡片统计与方法特征...")
        self.meta_summary_label.setObjectName("Tiny")
        h_layout.addWidget(self.meta_summary_label)
        outer.addWidget(header_card)

        # 2. Three Tab Channels for Step 5
        self.tabs = QTabWidget()

        # Tab 1: Web AI Clipboard
        tab_web = QWidget()
        web_layout = QVBoxLayout(tab_web)
        web_layout.setContentsMargins(12, 12, 12, 12)
        web_layout.setSpacing(10)

        web_desc = QLabel(
            "💡 推荐方式（零 Token 成本，充分发挥网页旗舰模型超长思维链）：\n"
            "点击「复制深度归纳 Prompt」，粘贴至 Claude 3.7 (Thinking) / DeepSeek-R1 / Gemini 2.5 Pro 网页端；"
            "随后将 AI 输出的 Master JSON 贴回下方即可。"
        )
        web_desc.setObjectName("Tiny")
        web_desc.setWordWrap(True)
        web_layout.addWidget(web_desc)

        copy_row = QHBoxLayout()
        self.copy_prompt_button = QPushButton("📋 第一步：复制深度归纳 Prompt 到剪贴板")
        self.copy_prompt_button.setObjectName("Primary")
        self.copy_prompt_button.clicked.connect(self._copy_prompt)
        copy_row.addWidget(self.copy_prompt_button)
        self.prompt_status_label = QLabel("")
        self.prompt_status_label.setObjectName("Tiny")
        copy_row.addWidget(self.prompt_status_label, 1)
        web_layout.addLayout(copy_row)

        web_layout.addWidget(QLabel("第二步：将网页版 AI 回复的 JSON 结果粘贴在下方："))
        self.web_reply_edit = QPlainTextEdit()
        self.web_reply_edit.setPlaceholderText(
            "请将网页版 AI 回复的 JSON 结果粘贴在此处（包含 title_zh, summary_zh, key_findings, open_questions...）"
        )
        web_layout.addWidget(self.web_reply_edit, 1)

        submit_row = QHBoxLayout()
        self.web_submit_button = QPushButton("🚀 第三步：写入知识库 (点亮金光)")
        self.web_submit_button.setObjectName("Warning")
        self.web_submit_button.setCursor(Qt.PointingHandCursor)
        self.web_submit_button.clicked.connect(self._submit_web_reply)
        submit_row.addWidget(self.web_submit_button)
        submit_row.addStretch(1)
        web_layout.addLayout(submit_row)

        self.tabs.addTab(tab_web, "📋 网页版 AI (零 Token·推荐)")

        # Tab 2: Codex Agent CLI Sandbox
        tab_codex = QWidget()
        codex_layout = QVBoxLayout(tab_codex)
        codex_layout.setContentsMargins(12, 12, 12, 12)
        codex_layout.setSpacing(10)

        codex_desc = QLabel(
            "⚡ 无人值守批处理：通过本地 Codex Agent CLI (`codex exec`) 在安全隔离沙盒中自主运行推理，"
            "自动提取并写入 L1 Master 主卡片。"
        )
        codex_desc.setObjectName("Tiny")
        codex_desc.setWordWrap(True)
        codex_layout.addWidget(codex_desc)

        node_row = QHBoxLayout()
        node_row.addWidget(QLabel("选择 Agent 节点："))
        self.agent_node_combo = QComboBox()
        self.run_codex_button = QPushButton("⚡ 启动 Codex 沙盒归纳")
        self.run_codex_button.setObjectName("Primary")
        self.run_codex_button.clicked.connect(self._run_codex_agent)
        self._load_agent_nodes()
        node_row.addWidget(self.agent_node_combo, 1)
        node_row.addWidget(self.run_codex_button)
        codex_layout.addLayout(node_row)

        self.codex_progress = QProgressBar()
        self.codex_progress.setRange(0, 0)
        self.codex_progress.setVisible(False)
        codex_layout.addWidget(self.codex_progress)

        self.codex_log_view = QPlainTextEdit()
        self.codex_log_view.setReadOnly(True)
        self.codex_log_view.setPlaceholderText("Codex 执行日志将在此实时显示...")
        codex_layout.addWidget(self.codex_log_view, 1)

        self.tabs.addTab(tab_codex, "⚡ Codex 沙盒 Agent")

        # Tab 3: Antigravity Agent MCP
        tab_antigravity = QWidget()
        anti_layout = QVBoxLayout(tab_antigravity)
        anti_layout.setContentsMargins(12, 12, 12, 12)
        anti_layout.setSpacing(10)

        anti_desc = QLabel(
            "🎫 人机协作对话模式：\n"
            "系统已为该文献提供 MCP 深度归纳工单工具 `export_deep_synthesis_task` 与 `submit_deep_synthesis_result`。\n\n"
            f"您只需在 Antigravity 聊天窗口中对我发送：\n"
            f"『请为文献 [{doc_id}] 执行第 ⑤ 步深度归纳，提炼 L1 Master 主卡片』\n\n"
            "Antigravity 将直接通过 MCP 读取全部卡片并在后台写入正式知识库，"
            "软件检测到数据变更后将自动点亮金光！"
        )
        anti_desc.setWordWrap(True)
        anti_layout.addWidget(anti_desc)
        anti_layout.addStretch(1)

        self.tabs.addTab(tab_antigravity, "🎫 Antigravity 对话工单")

        outer.addWidget(self.tabs, 1)

        # Footer
        footer = QHBoxLayout()
        footer.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("Pill")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        outer.addLayout(footer)

        self._load_document_data()

    def _on_doc_combo_changed(self, index: int) -> None:
        if not getattr(self, "doc_combo", None):
            return
        new_doc_id = self.doc_combo.currentData()
        if new_doc_id and new_doc_id != self.doc_id:
            self.doc_id = str(new_doc_id)
            self.setWindowTitle(f"文献 [{self.doc_id}] 深度归纳工作台 · 进阶第 ⑤ 步 (冒金光)")
            self._load_document_data()

    def _load_document_data(self) -> None:
        if not self.doc_id:
            return
        doc = self.db.get_document(self.doc_id) if self.db else None
        if not doc:
            return
        cards = self.db.cards_for_document(self.doc_id) if self.db else []
        from models.pipeline_stepper import compute_document_pipeline_status
        status = compute_document_pipeline_status(doc, cards)
        self.mini_stepper.set_pipeline_status(status)

        self.doc_title_label.setText(f"文献：{doc.get('title') or self.doc_id}")
        if hasattr(self, "web_reply_edit"):
            self.web_reply_edit.clear()
        if hasattr(self, "prompt_status_label"):
            self.prompt_status_label.clear()

        try:
            self._export_info = export_master_synthesis_prompt(self.db, self.doc_id)
            c_cnt = self._export_info["card_count"]
            method = self._export_info["method"] or "多方法综合"
            iso = self._export_info["isoelectronic"] or "通用"
            self.meta_summary_label.setText(
                f"已就绪原子化细节卡片：{c_cnt} 张 | 主要方法：{method} | 等电子序列：{iso}"
            )
            self.copy_prompt_button.setEnabled(True)
        except Exception as exc:
            self.meta_summary_label.setText(f"⚠️ {exc}")
            self.copy_prompt_button.setEnabled(False)

    def _load_agent_nodes(self) -> None:
        self.agent_node_combo.clear()
        pool_file = self.workspace / "config" / "endpoint_pool.json"
        try:
            pool = EndpointPool(pool_file)
            agent_nodes = [
                n for n in pool.nodes.values()
                if str(getattr(n, "node_type", "")).upper() == "AGENT_CLI" and getattr(n, "enabled", True)
            ]
        except Exception:
            agent_nodes = []
        if not agent_nodes:
            self.agent_node_combo.addItem("未配置启用的 Agent CLI 节点（请在模型池添加）", None)
            if hasattr(self, "run_codex_button"):
                self.run_codex_button.setEnabled(False)
        else:
            for n in agent_nodes:
                self.agent_node_combo.addItem(f"{n.name} ({n.provider})", n)
            if hasattr(self, "run_codex_button"):
                self.run_codex_button.setEnabled(True)

    def _copy_prompt(self) -> None:
        if not self._export_info:
            return
        prompt_text = self._export_info.get("prompt", "")
        clipboard = QApplication.clipboard()
        clipboard.setText(prompt_text)
        self.prompt_status_label.setText("✅ 已复制完整归纳 Prompt！请前往网页版 AI 粘贴。")

    def _submit_web_reply(self) -> None:
        raw_text = self.web_reply_edit.toPlainText().strip()
        if not raw_text:
            QMessageBox.warning(self, "提示", "请先将网页版 AI 回复的 JSON 内容粘贴于文本框中")
            return
        try:
            res = apply_master_synthesis_payload(
                self.db,
                self.qdrant,
                self.doc_id,
                raw_text,
                source_kind="WEB_CLIPBOARD",
            )
            QMessageBox.information(
                self,
                "深度归纳完成",
                f"✨ 成功生成并入库 L1 Master 卡片 [{res['knw_id']}]！\n"
                f"标题：{res.get('title_zh')}\n"
                "文献流水线已彻底达成，点亮黄金光芒！",
            )
            self._load_document_data()
            self.synthesis_completed.emit(self.doc_id, res["knw_id"])
            if len(self.doc_ids) > 1 and getattr(self, "doc_combo", None):
                cur_idx = self.doc_combo.currentIndex()
                if cur_idx + 1 < len(self.doc_ids):
                    reply = QMessageBox.question(
                        self,
                        "继续下一篇？",
                        f"✨ 文献 [{self.doc_id}] 深度归纳已完成！\n是否继续对下一篇文献 [{self.doc_ids[cur_idx + 1]}] 进行深度归纳？",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.Yes,
                    )
                    if reply == QMessageBox.Yes:
                        self.doc_combo.setCurrentIndex(cur_idx + 1)
                        return
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "解析失败", f"Master 卡片校验或入库失败：\n{exc}")

    def _run_codex_agent(self) -> None:
        node = self.agent_node_combo.currentData()
        if not node:
            return
        self.run_codex_button.setEnabled(False)
        self.codex_progress.setVisible(True)
        self.codex_log_view.setPlainText(f"正在启动 Agent CLI 任务: {node.name}...\n")

        self._codex_worker = CodexWorker(
            self.db,
            self.qdrant,
            self.workspace,
            self.doc_id,
            node,
        )
        self._codex_worker.finished_signal.connect(self._on_codex_success)
        self._codex_worker.error_signal.connect(self._on_codex_failed)
        self._codex_worker.start()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            parent = self.parent()
            if parent is not None:
                top = parent.window() if hasattr(parent, "window") else parent
                try:
                    import sys
                    if sys.platform == "win32":
                        import ctypes
                        ctypes.windll.user32.EnableWindow(
                            int(top.winId()),
                            1 if self.isMinimized() else 0,
                        )
                except Exception:
                    pass

    def _on_codex_success(self, res: dict) -> None:
        self.codex_progress.setVisible(False)
        self.run_codex_button.setEnabled(True)
        self.codex_log_view.appendPlainText(f"\n✅ 归纳成功！生成主卡：{res.get('knw_id')}")
        QMessageBox.information(
            self,
            "Codex 归纳完成",
            f"✨ Codex Agent 沙盒已生成并入库 L1 Master 卡片 [{res['knw_id']}]！\n"
            "进度条已点亮黄金光芒！",
        )
        self._load_document_data()
        self.synthesis_completed.emit(self.doc_id, res.get("knw_id", ""))
        if len(self.doc_ids) > 1 and getattr(self, "doc_combo", None):
            cur_idx = self.doc_combo.currentIndex()
            if cur_idx + 1 < len(self.doc_ids):
                reply = QMessageBox.question(
                    self,
                    "继续下一篇？",
                    f"✨ 文献 [{self.doc_id}] 深度归纳已完成！\n是否继续对下一篇文献 [{self.doc_ids[cur_idx + 1]}] 进行深度归纳？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )
                if reply == QMessageBox.Yes:
                    self.doc_combo.setCurrentIndex(cur_idx + 1)
                    return
        self.accept()

    def _on_codex_failed(self, error: str) -> None:
        self.codex_progress.setVisible(False)
        self.run_codex_button.setEnabled(True)
        self.codex_log_view.appendPlainText(f"\n❌ 执行失败：{error}")
        QMessageBox.critical(self, "Codex 执行失败", f"Agent 沙盒执行发生异常：\n{error}")
