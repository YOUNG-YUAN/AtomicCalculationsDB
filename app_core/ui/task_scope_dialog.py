"""Task Scope Selection Dialog for AtomicCalculationDB V2.9.4.

Provides unified scope selection (Select All, Clear, Document Picker) for:
- Step 3 API Fast Scan
- Step 3 Semi-Auto Web Batch Workbench
- Step 5 Deep Synthesis Workbench
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from ui.local_clean_dialog import DocSelectDialog

log = logging.getLogger(__name__)


class TaskScopeDialog(QDialog):
    """Scope selection dialog for AI Step 3 (API), Step 3 (Semi-Auto), and Step 5 (Deep Synthesis)."""

    def __init__(
        self,
        db: Any,
        task_type: str,  # "step3_api" | "step3_semi" | "step5_deep"
        *,
        selected_doc_ids: Iterable[str] | None = None,
        filtered_doc_ids: Iterable[str] | None = None,
        total_doc_count: int = 0,
        reprocess_manager: Any = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.task_type = task_type
        self.reprocess_manager = reprocess_manager
        self.selected_doc_ids = list(selected_doc_ids or [])
        self.filtered_doc_ids = list(filtered_doc_ids or []) if filtered_doc_ids is not None else None
        self.total_doc_count = total_doc_count or (self.db.count_documents() if self.db else 0)

        self._init_task_meta()
        self.resize(580, 360)
        self.setMinimumWidth(500)
        self._init_ui()
        self._update_estimates()

    def _init_task_meta(self) -> None:
        if self.task_type in ("step3_api", "step3_extract"):
            self.window_title = "⚡ AI 快速理解 · 范围与方式选择"
            self.header_title = "<b>第 ③ 步：AI 快速理解</b>"
            self.header_desc = "支持依照模型调度策略全自动提炼，或无API网页版AI半自动模式批量打包。请选择处理文献范围与执行方式："
            self.confirm_text = "▶ 开始执行 AI 快速理解"
            self.confirm_color = "#0d6efd"  # Primary blue
        elif self.task_type == "step3_semi":
            self.window_title = "📋 网页版半自动速览 · 范围选择 (零 Token 消耗)"
            self.header_title = "<b>第 ③ 步：网页版 AI 半自动速览</b>"
            self.header_desc = "按 Token 上限自动将文献按篇打包为 Prompt，复制到网页端（ChatGPT / Claude / DeepSeek / Gemini）提炼，完全零 Token 成本。请选择要打包的文献范围："
            self.confirm_text = "📋 打开半自动工作台"
            self.confirm_color = "#0b5ed7"
        elif self.task_type == "step4_audit":
            self.window_title = "🔍 AI 校对核查 · 范围与方式选择"
            self.header_title = "<b>第 ④ 步：AI 校对核查（审稿人核验与延拓审核）</b>"
            self.header_desc = "对选中文献进行知识卡交叉审稿，核验真伪、审查原理可行性并顺路补齐或修正广义物理延拓。支持自动调度或网页端半自动："
            self.confirm_text = "🔍 打开文献 AI 校对工作台"
            self.confirm_color = "#198754"
        elif self.task_type == "step5_deep":
            self.window_title = "✨ AI 深度归纳 · 范围与方式选择"
            self.header_title = "<b>第 ⑤ 步：AI 深度归纳（Master 宏观全景卡片）</b>"
            self.header_desc = "综合各证据包深度凝练宏观知识全景，完成后文献状态将点亮金光。支持自动归纳或网页版半自动："
            self.confirm_text = "✨ 打开深度归纳工作台"
            self.confirm_color = "#0d6efd"
        else:
            self.window_title = "选择任务处理范围与方式"
            self.header_title = "<b>选择任务处理范围与方式</b>"
            self.header_desc = "请选择要处理的文献范围与执行方式："
            self.confirm_text = "确定并继续"
            self.confirm_color = "#0d6efd"

    def _init_ui(self) -> None:
        self.setWindowTitle(self.window_title)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        info_label = QLabel(f"{self.header_title}<br><font color='#555'>{self.header_desc}</font>")
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        # Scope selection group
        scope_group = QGroupBox("选择处理文献范围")
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
        self.scope_btn_group.buttonToggled.connect(lambda *_: self._update_estimates())

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

        # Execution mode selection group
        mode_group = QGroupBox("选择执行方式")
        mode_layout = QVBoxLayout(mode_group)
        self.mode_btn_group = QButtonGroup(self)
        self.radio_mode_auto = QRadioButton("⚡ 依照模型策略自动处理 (按模型调度策略编排器设置自动运行)")
        self.radio_mode_semi = QRadioButton("📋 无API网页版AI半自动模式 (剪切板提问包，零 Token 消耗)")
        self.mode_btn_group.addButton(self.radio_mode_auto)
        self.mode_btn_group.addButton(self.radio_mode_semi)
        self.radio_mode_auto.setChecked(True)
        self.mode_btn_group.buttonToggled.connect(lambda *_: self._update_estimates())
        mode_layout.addWidget(self.radio_mode_auto)
        mode_layout.addWidget(self.radio_mode_semi)
        layout.addWidget(mode_group)

        # Status & Estimate label
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #444; font-size: 12px;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # Action buttons
        btn_layout = QHBoxLayout()
        self.confirm_button = QPushButton(self.confirm_text)
        self.confirm_button.setCursor(Qt.PointingHandCursor)
        self.confirm_button.setStyleSheet(
            f"QPushButton {{ background-color: {self.confirm_color}; color: white; font-weight: bold; padding: 6px 16px; border-radius: 4px; }} "
            f"QPushButton:hover {{ opacity: 0.9; }} "
            f"QPushButton:disabled {{ background-color: #b0c4de; }}"
        )
        self.confirm_button.clicked.connect(self._on_confirm)

        self.cancel_button = QPushButton("取消")
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.clicked.connect(self.reject)

        btn_layout.addWidget(self.confirm_button)
        btn_layout.addStretch()
        btn_layout.addWidget(self.cancel_button)
        layout.addLayout(btn_layout)

    def _select_all_docs(self) -> None:
        if self.db:
            try:
                all_docs = self.db.list_documents_summary(limit=100000)
                self.selected_doc_ids = [str(d["doc_id"]) for d in all_docs if d.get("doc_id")]
            except Exception as e:
                log.exception("Failed to query all doc ids: %s", e)
        n = len(self.selected_doc_ids) if self.selected_doc_ids else self.total_doc_count
        self.radio_selected.setText(f"仅处理选中文献 (已选 {n} 篇)")
        self.radio_selected.setEnabled(n > 0)
        self.radio_selected.setChecked(True)
        self._update_estimates()

    def _clear_all_docs(self) -> None:
        self.selected_doc_ids = []
        self.radio_selected.setText("仅处理选中文献 (已选 0 篇)")
        self.radio_selected.setEnabled(False)
        if self.radio_filtered.isEnabled():
            self.radio_filtered.setChecked(True)
        else:
            self.radio_all.setChecked(True)
        self._update_estimates()

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
            else:
                if self.radio_filtered.isEnabled():
                    self.radio_filtered.setChecked(True)
                else:
                    self.radio_all.setChecked(True)
            self._update_estimates()

    def get_execution_mode(self) -> str:
        """Return 'semi_auto' for clipboard mode, 'auto' for strategy/API mode."""
        if hasattr(self, "radio_mode_semi") and self.radio_mode_semi.isChecked():
            return "semi_auto"
        return "auto"

    def _update_estimates(self) -> None:
        if not hasattr(self, "status_label"):
            return
        target_ids = self.get_target_doc_ids()
        count = len(target_ids) if target_ids is not None else self.total_doc_count
        is_semi = hasattr(self, "radio_mode_semi") and self.radio_mode_semi.isChecked()

        if count == 0:
            self.status_label.setText("⚠️ 未选定任何文献。请选择文献范围或选择全库。")
            if hasattr(self, "confirm_button"):
                self.confirm_button.setEnabled(False)
            return

        if hasattr(self, "confirm_button"):
            self.confirm_button.setEnabled(True)

        if self.task_type in ("step3_api", "step3_extract"):
            if is_semi:
                if hasattr(self, "confirm_button"):
                    self.confirm_button.setText("📋 打开半自动工作台 (零Token)")
                self.status_label.setText(
                    f"已选定目标：{count} 篇文献（按 Token 上限自动按篇分包，单包绝不混淆多篇文献。完全零 API Token 成本）。"
                )
            else:
                token_est = ""
                if self.reprocess_manager:
                    try:
                        tokens = self.reprocess_manager.estimate_incremental_tokens(target_ids)
                        token_est = f" · 预计最多消耗约 {tokens:,} 输入 Token（4 路并发）"
                    except Exception:
                        pass
                if hasattr(self, "confirm_button"):
                    self.confirm_button.setText("▶ 依照策略自动速览")
                self.status_label.setText(f"已选定目标：{count} 篇文献{token_est}。")
        elif self.task_type == "step3_semi":
            if hasattr(self, "confirm_button"):
                self.confirm_button.setText("📋 打开半自动工作台")
            self.status_label.setText(f"已选定目标：{count} 篇文献（按 Token 上限自动按篇分包，零 API 成本）。")
        elif self.task_type == "step4_audit":
            if is_semi:
                if hasattr(self, "confirm_button"):
                    self.confirm_button.setText("📋 打开校对工作台 (剪切板/导出)")
                self.status_label.setText(f"已选定目标：{count} 篇文献（导出校对任务包并在网页端 AI 交叉审核，完全零 Token 成本）。")
            else:
                if hasattr(self, "confirm_button"):
                    self.confirm_button.setText("🔍 依照策略自动校对核查")
                self.status_label.setText(f"已选定目标：{count} 篇文献（依照 AI 校对核查策略全自动审稿并补齐延拓）。")
        elif self.task_type == "step5_deep":
            if is_semi:
                if hasattr(self, "confirm_button"):
                    self.confirm_button.setText("📋 打开深度归纳工作台 (网页版Prompt)")
                self.status_label.setText(f"已选定目标：{count} 篇文献（可按篇切换复制宏观全景 Prompt 到网页端旗舰 AI 提炼）。")
            else:
                if hasattr(self, "confirm_button"):
                    self.confirm_button.setText("✨ 依照策略自动深度归纳")
                self.status_label.setText(f"已选定目标：{count} 篇文献（依照 AI 深度归纳策略全自动凝练 Master 全景卡片）。")

    def _on_confirm(self) -> None:
        target_ids = self.get_target_doc_ids()
        if self.radio_selected.isChecked() and not self.selected_doc_ids:
            self.status_label.setText("⚠️ 当前选中文献数量为 0！请点击【选择文献】或【选择所有文献】。")
            return
        self.accept()

    def get_target_doc_ids(self) -> list[str] | None:
        if self.radio_selected.isChecked():
            return list(self.selected_doc_ids)
        if self.radio_filtered.isChecked():
            return list(self.filtered_doc_ids) if self.filtered_doc_ids is not None else None
        return None  # None indicates all documents
