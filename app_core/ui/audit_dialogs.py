"""Dialogs for importing a zero-token external AI audit result."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.controls import WheelSafeComboBox as QComboBox

from audit.importer import (
    AuditImportError,
    AuditPreview,
    apply_audit_payload,
    load_audit_payload,
    preview_audit_payload,
)


class AuditImportDialog(QDialog):
    """Preview first, then apply only selected audit changes."""

    def __init__(
        self,
        db: Any,
        doc_id: str,
        *,
        pipeline: Any = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.doc_id = str(doc_id)
        self.pipeline = pipeline
        self.preview: AuditPreview | None = None
        self.setWindowTitle(f"导入 AI 校对结果 · {doc_id}")
        self.resize(980, 680)
        self.setMinimumSize(760, 540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(9)

        heading = QLabel("外部 AI 校对结果")
        heading.setObjectName("DialogTitle")
        layout.addWidget(heading)
        hint = QLabel(
            "只做本地 JSON 校验和数据库回写，不调用大模型、不消耗 Token；"
            "“建议删除”只会把卡片标红并默认隐藏，不会物理删除。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("审计文件"))
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("选择外部 AI 返回的 JSON，或包含 JSON 代码块的 .md")
        file_row.addWidget(self.file_edit, 1)
        browse = QPushButton("浏览…")
        browse.setObjectName("Pill")
        browse.clicked.connect(self._browse)
        file_row.addWidget(browse)
        layout.addLayout(file_row)

        form = QFormLayout()
        self.auditor_combo = QComboBox()
        self.auditor_combo.addItem("手动填写", "")
        self._populate_auditors()
        self.auditor_combo.currentIndexChanged.connect(self._auditor_changed)
        form.addRow("审计 AI 来源", self.auditor_combo)
        self.auditor_name = QLineEdit("外部 AI")
        form.addRow("AI 名称", self.auditor_name)
        self.auditor_model = QLineEdit()
        form.addRow("模型名", self.auditor_model)
        self.auditor_provider = QLineEdit()
        form.addRow("Provider", self.auditor_provider)
        layout.addLayout(form)

        actions = QHBoxLayout()
        self.parse_button = QPushButton("解析并预览")
        self.parse_button.setObjectName("Primary")
        self.parse_button.clicked.connect(self._parse)
        actions.addWidget(self.parse_button)
        self.apply_button = QPushButton("应用选中的修改")
        self.apply_button.setObjectName("Pill")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        actions.addWidget(self.apply_button)
        self.apply_verified = QCheckBox("仍要应用“用户已确认”卡片的建议")
        actions.addWidget(self.apply_verified)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.stats_label = QLabel("尚未解析。")
        self.stats_label.setObjectName("SectionHint")
        self.stats_label.setWordWrap(True)
        layout.addWidget(self.stats_label)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["导入", "卡片 / 新增", "分数", "动作 / 评级", "状态 / 说明"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 54)
        self.table.setColumnWidth(1, 230)
        self.table.setColumnWidth(2, 70)
        self.table.setColumnWidth(3, 160)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table, 1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("Ghost")
        close.clicked.connect(self.reject)
        close_row.addWidget(close)
        layout.addLayout(close_row)

    def _populate_auditors(self) -> None:
        pool = None
        if self.pipeline is not None:
            pool = getattr(getattr(self.pipeline, "gateway", None), "pool", None)
        for node in getattr(pool, "nodes", {}).values() if pool is not None else []:
            model = getattr(node, "model", "") or getattr(node, "web_model", "")
            self.auditor_combo.addItem(
                f"{node.name} · {model or node.node_type}", node.node_id
            )

    def _auditor_changed(self, _index: int = -1) -> None:
        node_id = str(self.auditor_combo.currentData() or "")
        if not node_id:
            return
        pool = None
        if self.pipeline is not None:
            pool = getattr(getattr(self.pipeline, "gateway", None), "pool", None)
        node = getattr(pool, "nodes", {}).get(node_id) if pool is not None else None
        if node is None:
            return
        self.auditor_name.setText(node.name or "外部 AI")
        self.auditor_model.setText(
            str(getattr(node, "web_model", "") or getattr(node, "model", "") or "")
        )
        self.auditor_provider.setText(
            "web"
            if node.node_type == "WEB_AI_PLAYWRIGHT"
            else "local"
            if node.node_type == "LOCAL_ONNX"
            else "api"
        )

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择 AI 校对结果",
            str(Path.home()),
            "审计 JSON (*.json *.md);;所有文件 (*.*)",
        )
        if path:
            self.file_edit.setText(path)

    def _auditor_kwargs(self) -> dict[str, str]:
        node_id = str(self.auditor_combo.currentData() or "")
        return {
            "auditor_name": self.auditor_name.text().strip() or "外部 AI",
            "auditor_model": self.auditor_model.text().strip(),
            "auditor_provider": self.auditor_provider.text().strip(),
            "auditor_node_id": node_id,
            "audit_source": "pool" if node_id else "manual_import",
        }

    def _parse(self) -> None:
        path = self.file_edit.text().strip()
        if not path:
            QMessageBox.information(self, "请选择文件", "请先选择审计 JSON 文件。")
            return
        try:
            payload = load_audit_payload(path)
            auditor = payload.get("auditor") or {}
            if isinstance(auditor, dict):
                if self.auditor_name.text().strip() in ("", "外部 AI"):
                    self.auditor_name.setText(
                        str(auditor.get("name") or self.auditor_name.text())
                    )
                if not self.auditor_model.text().strip():
                    self.auditor_model.setText(str(auditor.get("model") or ""))
                if not self.auditor_provider.text().strip():
                    self.auditor_provider.setText(str(auditor.get("provider") or ""))
            self.preview = preview_audit_payload(
                self.db,
                payload,
                document_id=self.doc_id,
                **self._auditor_kwargs(),
            )
        except (AuditImportError, ValueError) as exc:
            QMessageBox.warning(self, "解析失败", str(exc))
            return
        self._fill_table(self.preview)
        stats = self.preview.stats
        self.stats_label.setText(
            "可应用 {ready} · 过期 {stale} · 缺失 {missing} · 无效 {invalid} · "
            "用户确认待复核 {verified_pending} · 修改 {modify} · 删除 {delete} · "
            "新增 {new} · 重复 {duplicate}".format(**stats)
        )
        self.apply_button.setEnabled(True)

    def _fill_table(self, preview: AuditPreview) -> None:
        self.table.setRowCount(0)
        for item in preview.reviews:
            payload = item.get("payload") or {}
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = QTableWidgetItem("")
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            check.setCheckState(
                Qt.Checked if item.get("status") == "ready" else Qt.Unchecked
            )
            check.setData(Qt.UserRole, ("card", item.get("knw_id")))
            self.table.setItem(row, 0, check)
            current = item.get("current") or {}
            self.table.setItem(
                row,
                1,
                QTableWidgetItem(
                    f"{item.get('knw_id')} · "
                    f"{str(current.get('title_zh') or '')[:80]}"
                ),
            )
            self.table.setItem(
                row, 2, QTableWidgetItem(str(payload.get("overall_score", "—")))
            )
            self.table.setItem(
                row,
                3,
                QTableWidgetItem(
                    f"{payload.get('suggested_action') or 'keep'} / "
                    f"{payload.get('verdict') or '—'}"
                ),
            )
            warning = "; ".join(
                part
                for part in (
                    item.get("status"),
                    item.get("warning"),
                )
                if part
            )
            self.table.setItem(row, 4, QTableWidgetItem(warning or "可应用"))
        for entry in preview.new_cards:
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = QTableWidgetItem("")
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Checked)
            check.setData(Qt.UserRole, ("new", entry.get("temp_id")))
            self.table.setItem(row, 0, check)
            card = entry.get("card") or {}
            self.table.setItem(
                row,
                1,
                QTableWidgetItem(
                    f"新增 {entry.get('temp_id')} · "
                    f"{str(card.get('title_zh') or '')[:80]}"
                ),
            )
            self.table.setItem(row, 2, QTableWidgetItem("—"))
            self.table.setItem(row, 3, QTableWidgetItem("add"))
            self.table.setItem(
                row, 4, QTableWidgetItem(str(entry.get("reason") or "AI 新增卡片"))
            )

    def _selected(self) -> tuple[list[str], list[str]]:
        cards: list[str] = []
        new_cards: list[str] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None or item.checkState() != Qt.Checked:
                continue
            data = item.data(Qt.UserRole) or ("", "")
            kind, identifier = data
            if kind == "card":
                cards.append(str(identifier))
            elif kind == "new":
                new_cards.append(str(identifier))
        return cards, new_cards

    def _apply(self) -> None:
        if self.preview is None:
            return
        cards, new_cards = self._selected()
        if not cards and not new_cards:
            QMessageBox.information(self, "没有选中项", "请至少勾选一条要应用的审计结果。")
            return
        try:
            result = apply_audit_payload(
                self.db,
                self.preview,
                selected_knw_ids=cards,
                selected_new_ids=new_cards,
                apply_verified=self.apply_verified.isChecked(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "应用失败", str(exc))
            return
        QMessageBox.information(
            self,
            "应用完成",
            f"修改 {result.modified} 张，建议删除 {result.deleted} 张，"
            f"新增 {result.added} 张，保留 {result.kept} 张，"
            f"待人工复核 {result.needs_review} 张，拒绝 {result.rejected} 张。",
        )
        self.accept()


class DocumentAuditDialog(QDialog):
    """Workbench for Step 4 Document AI Audit & Proofreading (zero-token export/import)."""

    def __init__(
        self,
        db: Any,
        workspace: str | Path,
        doc_ids: list[str] | str,
        parent: QWidget | None = None,
        pipeline: Any = None,
    ):
        super().__init__(parent)
        self.db = db
        self.workspace = Path(workspace)
        if isinstance(doc_ids, list):
            self.doc_ids = [str(d) for d in doc_ids if d]
            self.doc_id = self.doc_ids[0] if self.doc_ids else ""
        else:
            self.doc_ids = [str(doc_ids)] if doc_ids else []
            self.doc_id = str(doc_ids) if doc_ids else ""
        self.pipeline = pipeline
        self._export_result: Any = None

        self.setWindowTitle(f"文献 [{self.doc_id}] AI 校对工作台 · 第 ④ 步 (审稿与延拓审核)")
        self.resize(780, 560)
        self.setMinimumSize(660, 460)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        # 1. Header card
        header_card = QFrame()
        header_card.setObjectName("SurfaceCard")
        h_layout = QVBoxLayout(header_card)
        h_layout.setContentsMargins(12, 10, 12, 10)
        h_layout.setSpacing(6)

        title_row = QHBoxLayout()
        if len(self.doc_ids) > 1:
            title_row.addWidget(QLabel("选择文献："), 0)
            self.doc_combo = QComboBox()
            self.doc_combo.setMinimumWidth(380)
            for did in self.doc_ids:
                d_obj = self.db.get_document(did) if self.db else None
                t_str = (d_obj.get("title") if d_obj else None) or did
                self.doc_combo.addItem(f"[{did}] {t_str[:45]}", did)
            self.doc_combo.currentIndexChanged.connect(self._on_doc_combo_changed)
            title_row.addWidget(self.doc_combo, 1)
            self.doc_title_label = None
        else:
            self.doc_combo = None
            self.doc_title_label = QLabel(f"文献：{self.doc_id}")
            self.doc_title_label.setObjectName("SectionTitle")
            self.doc_title_label.setWordWrap(True)
            title_row.addWidget(self.doc_title_label, 1)

        h_layout.addLayout(title_row)

        self.meta_summary_label = QLabel("正在加载卡片统计与状态...")
        self.meta_summary_label.setObjectName("Tiny")
        h_layout.addWidget(self.meta_summary_label)
        outer.addWidget(header_card)

        # 2. Body card
        body_card = QFrame()
        body_card.setObjectName("Card")
        b_layout = QVBoxLayout(body_card)
        b_layout.setContentsMargins(14, 14, 14, 14)
        b_layout.setSpacing(12)

        desc_label = QLabel(
            "💡 <b>第 ④ 步推荐工作流（零 Token 成本 · 充分借助网页端 AI 超长推理链）：</b><br>"
            "1. 点击<b>「第一步：一键复制 AI 校对包」</b>，将审稿提示词与证据卡片粘贴至 Claude / DeepSeek / ChatGPT 网页端；<br>"
            "2. 审稿模型将核查结论真伪、<b>审核推广规律在原理上是否可行（不可行给出理由并纠偏）</b>，并顺路补齐缺失的物理延拓；<br>"
            "3. 将 AI 输出的 JSON 结果保存为文件，点击<b>「第二步：导入 AI 校对结果」</b>一键预览并入库应用。"
        )
        desc_label.setObjectName("Tiny")
        desc_label.setWordWrap(True)
        b_layout.addWidget(desc_label)

        # Options
        options_layout = QHBoxLayout()
        self.check_include_source = QCheckBox("附带 source.md 原文 Markdown")
        self.check_include_source.setChecked(True)
        self.check_include_source.setToolTip("将解析后的 Markdown 原文一并打包，方便审稿 AI 交叉核验。")
        self.check_coverage = QCheckBox("开启覆盖审计模式")
        self.check_coverage.setChecked(False)
        self.check_coverage.setToolTip("除核对现有卡片外，额外检查原文中是否遗漏了重要物理知识。")
        self.check_history = QCheckBox("包含已有历史审计记录")
        self.check_history.setChecked(False)
        self.check_history.setToolTip("默认关闭，避免外部 AI 被上一次的评分结果锚定。")
        options_layout.addWidget(self.check_include_source)
        options_layout.addWidget(self.check_coverage)
        options_layout.addWidget(self.check_history)
        options_layout.addStretch(1)
        b_layout.addLayout(options_layout)

        # Step 1 box
        step1_box = QGroupBox("第一步：导出与复制校对任务包")
        s1_layout = QHBoxLayout(step1_box)
        self.copy_audit_button = QPushButton("📋 一键复制 AI 校对包到剪贴板")
        self.copy_audit_button.setObjectName("Primary")
        self.copy_audit_button.setCursor(Qt.PointingHandCursor)
        self.copy_audit_button.clicked.connect(self._copy_audit_bundle)
        s1_layout.addWidget(self.copy_audit_button)

        self.open_dir_button = QPushButton("📂 打开校对包文件夹")
        self.open_dir_button.setObjectName("Pill")
        self.open_dir_button.setCursor(Qt.PointingHandCursor)
        self.open_dir_button.clicked.connect(self._open_export_dir)
        s1_layout.addWidget(self.open_dir_button)
        b_layout.addWidget(step1_box)

        self.status_hint = QLabel("")
        self.status_hint.setObjectName("SectionHint")
        self.status_hint.setWordWrap(True)
        b_layout.addWidget(self.status_hint)

        # Step 2 box
        step2_box = QGroupBox("第二步：导入审稿结果（自动纠偏、补齐延拓）")
        s2_layout = QHBoxLayout(step2_box)
        self.import_audit_button = QPushButton("📥 导入 AI 校对结果 (.json / .md)...")
        self.import_audit_button.setObjectName("Primary")
        self.import_audit_button.setCursor(Qt.PointingHandCursor)
        self.import_audit_button.clicked.connect(self._open_import_dialog)
        s2_layout.addWidget(self.import_audit_button)
        b_layout.addWidget(step2_box)

        outer.addWidget(body_card, 1)

        # Close row
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("Ghost")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        outer.addLayout(close_row)

        self._refresh_doc_view()

    def _on_doc_combo_changed(self, index: int) -> None:
        if index < 0 or not self.doc_combo:
            return
        self.doc_id = str(self.doc_combo.currentData() or "")
        self._export_result = None
        self.status_hint.setText("")
        self._refresh_doc_view()

    def _refresh_doc_view(self) -> None:
        if not self.db or not self.doc_id:
            return
        doc = self.db.get_document(self.doc_id)
        if not doc:
            return
        cards = self.db.cards_for_document(self.doc_id)
        if self.doc_combo is None and hasattr(self, "doc_title_label") and self.doc_title_label:
            self.doc_title_label.setText(f"文献：{doc.get('title') or self.doc_id}")
        conjecture_count = sum(
            1 for c in cards
            if c.get("generalized_conjecture") or (c.get("structured") or {}).get("generalized_conjecture")
        )
        self.meta_summary_label.setText(
            f"关联知识卡：{len(cards)} 张 | 已具广义延拓：{conjecture_count} 张 | "
            f"年份：{doc.get('pub_year') or '—'} | 期刊：{doc.get('journal') or 'N/A'}"
        )

    def _ensure_export(self) -> Any:
        from audit.report import export_audit_bundle

        return export_audit_bundle(
            self.db,
            self.workspace,
            self.doc_id,
            include_source=self.check_include_source.isChecked(),
            coverage_audit=self.check_coverage.isChecked(),
            include_history=self.check_history.isChecked(),
        )

    def _copy_audit_bundle(self) -> None:
        try:
            res = self._ensure_export()
            self._export_result = res
            prompt_file = res.export_dir / "audit_prompt.md"
            if not prompt_file.exists():
                candidates = list(res.export_dir.glob("audit_prompt*.md"))
                prompt_file = candidates[0] if candidates else None
            report_file = res.export_dir / "report.md"
            if not report_file.exists():
                candidates = list(res.export_dir.glob("report*.md"))
                report_file = candidates[0] if candidates else None

            parts = []
            if prompt_file and prompt_file.exists():
                parts.append(prompt_file.read_text(encoding="utf-8"))
            if report_file and report_file.exists():
                parts.append("\n\n---\n\n" + report_file.read_text(encoding="utf-8"))

            combined = "\n\n".join(parts)
            clipboard = QGuiApplication.clipboard()
            clipboard.setText(combined)
            self.status_hint.setText(
                f"✅ 已复制文献 [{self.doc_id}] 的校对提示词与卡片报告（共 {res.card_count} 张卡片）！请直接前往网页端 AI 粘贴。"
            )
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def _open_export_dir(self) -> None:
        try:
            res = self._ensure_export()
            self._export_result = res
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(res.export_dir)))
        except Exception as exc:
            QMessageBox.warning(self, "打开文件夹失败", str(exc))

    def _open_import_dialog(self) -> None:
        dialog = AuditImportDialog(
            self.db,
            self.doc_id,
            pipeline=self.pipeline,
            parent=self,
        )
        if dialog.exec():
            self._refresh_doc_view()
            self.status_hint.setText("🎉 校对结果已成功导入并持久化更新！")
