"""Settings & hot update: unified model pool, builtin models, maintenance."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.controls import (
    WheelSafeComboBox as QComboBox,
    WheelSafeSpinBox as QSpinBox,
)

from crawler.mirror_probe import normalize_mirror
from db.demo_v2 import seed_demo_v2
from db.sqlite_manager import SQLiteManager
from models.endpoint_pool import EndpointPool, ROLE_LABELS
from parser.mineru_backend import MinerUConfig, MinerUConverter
from ui.m3_theme import format_time
from ui.model_pool_panel import ModelPoolPanel
from ui.widgets import PageHeader, SectionCard, make_chip
from ui.workers import MirrorProbeWorker, ModelDownloadWorker
from updater.patch_manager import PatchManager, UpdateError


class SettingsView(QWidget):
    toast = Signal(str)
    models_changed = Signal()
    data_changed = Signal()
    update_requested = Signal()

    def __init__(
        self,
        db: SQLiteManager,
        config: Any,
        gateway: Any,
        base_dir: str,
        workspace: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.config = config
        self.gateway = gateway
        self.base_dir = Path(base_dir)
        self.workspace = Path(workspace)
        self.patch = PatchManager(self.base_dir)
        self._model_worker: ModelDownloadWorker | None = None
        self._probe_worker: MirrorProbeWorker | None = None
        self._mirror_timer = QTimer(self)
        self._mirror_timer.setSingleShot(True)
        self._mirror_timer.setInterval(650)
        self._mirror_timer.timeout.connect(self._save_mirrors)

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
            "设置与热更新",
            "统一模型与算力池、ONNX 引擎、文献下载源、数据维护与运行中热更新。",
        )
        self.header_update_button = QPushButton("📦 导入更新包 (.zip) 立即更新")
        self.header_update_button.setObjectName("UpdateCapsule")
        self.header_update_button.setCursor(Qt.PointingHandCursor)
        self.header_update_button.clicked.connect(self.update_requested)
        header.add_action(self.header_update_button)
        layout.addWidget(header)

        # ------------------------------------- V2.3 endpoint pool / strategies
        self.endpoint_pool = getattr(gateway, "pool", None) or EndpointPool(config)
        self.model_pool_panel = ModelPoolPanel(
            config,
            self.endpoint_pool,
            str(self.workspace),
            gateway,
            parent=self,
        )
        self.model_pool_panel.changed.connect(self._on_pool_changed)
        self.model_pool_panel.toast.connect(self.toast)
        layout.addWidget(self.model_pool_panel)

        # ---------------------------------------------------------- builtin models
        self.builtin_card = SectionCard(
            "🧠 本地轻量神经网络引擎 (ONNX)", "CPU 加速 · 无需 PyTorch · 国内镜像优先"
        )
        self.builtin_grid = QGridLayout()
        self.builtin_grid.setHorizontalSpacing(12)
        self.builtin_grid.setVerticalSpacing(7)
        self.builtin_card.body.addLayout(self.builtin_grid)
        builtin_hint = QLabel(
            "MiniLM 负责同义聚类、DeBERTa-NLI 负责极性冲突、MiniLM-Reranker 负责精排。"
            "缺失时自动降级为 FTS / token 聚类 / 启发式冲突，不影响使用。"
        )
        builtin_hint.setObjectName("Tiny")
        builtin_hint.setWordWrap(True)
        self.builtin_card.body.addWidget(builtin_hint)
        self.model_progress = QProgressBar()
        self.model_progress.setRange(0, 100)
        self.model_progress.setTextVisible(True)
        self.model_progress.setVisible(False)
        self.builtin_card.body.addWidget(self.model_progress)
        self.model_status_line = QLabel("")
        self.model_status_line.setObjectName("Tiny")
        self.model_status_line.setWordWrap(True)
        self.builtin_card.body.addWidget(self.model_status_line)
        builtin_actions = QHBoxLayout()
        builtin_actions.setSpacing(8)
        self.download_models_button = QPushButton("⬇️ 一键下载并激活全部内置 ONNX 模型")
        self.download_models_button.setObjectName("Primary")
        self.download_models_button.clicked.connect(self._download_models)
        self.import_models_button = QPushButton("📂 从本地文件夹导入模型")
        self.import_models_button.setObjectName("Pill")
        self.import_models_button.clicked.connect(self._import_models)
        builtin_actions.addWidget(self.download_models_button)
        builtin_actions.addWidget(self.import_models_button)
        builtin_actions.addStretch(1)
        self.builtin_card.body.addLayout(builtin_actions)
        layout.addWidget(self.builtin_card)

        # --------------------------------------------------------------- MinerU 4
        self.mineru_card = SectionCard(
            "📄 MinerU 4 高质量 OCR / Markdown",
            "可选外部后端 · 扫描件/复杂版面优先 · 失败自动回退 PyMuPDF",
        )
        mineru_grid = QGridLayout()
        mineru_grid.setHorizontalSpacing(10)
        mineru_grid.setVerticalSpacing(6)
        self.mineru_enabled = QCheckBox("启用 MinerU 4 解析 PDF")
        mineru_grid.addWidget(self.mineru_enabled, 0, 0, 1, 3)
        mineru_grid.addWidget(QLabel("命令 / 可执行文件"), 1, 0)
        self.mineru_command = QLineEdit()
        self.mineru_command.setPlaceholderText("mineru-kit 或完整路径")
        mineru_grid.addWidget(self.mineru_command, 1, 1)
        mineru_browse = QPushButton("浏览…")
        mineru_browse.setObjectName("Pill")
        mineru_browse.clicked.connect(self._browse_mineru)
        mineru_grid.addWidget(mineru_browse, 1, 2)
        mineru_grid.addWidget(QLabel("解析档位"), 2, 0)
        self.mineru_tier = QComboBox()
        self.mineru_tier.setMaximumWidth(220)
        for label, value in (
            ("Flash · 快速预览", "flash"),
            ("Basic · OCR/小模型", "basic"),
            ("Standard · 高质量", "standard"),
            ("Advanced · 最高质量", "advanced"),
        ):
            self.mineru_tier.addItem(label, value)
        mineru_grid.addWidget(self.mineru_tier, 2, 1, Qt.AlignLeft)
        mineru_grid.addWidget(QLabel("OCR 模式"), 3, 0)
        self.mineru_ocr = QComboBox()
        self.mineru_ocr.setMaximumWidth(220)
        for label, value in (
            ("自动", "auto"),
            ("仅文本层", "txt"),
            ("强制 OCR", "ocr"),
        ):
            self.mineru_ocr.addItem(label, value)
        mineru_grid.addWidget(self.mineru_ocr, 3, 1, Qt.AlignLeft)
        mineru_grid.addWidget(QLabel("设备"), 4, 0)
        self.mineru_device = QComboBox()
        self.mineru_device.setMaximumWidth(180)
        for label, value in (
            ("自动", "auto"),
            ("CPU", "cpu"),
            ("GPU", "gpu"),
        ):
            self.mineru_device.addItem(label, value)
        mineru_grid.addWidget(self.mineru_device, 4, 1, Qt.AlignLeft)
        mineru_grid.addWidget(QLabel("超时（秒）"), 5, 0)
        self.mineru_timeout = QSpinBox()
        self.mineru_timeout.setRange(60, 7200)
        self.mineru_timeout.setSingleStep(60)
        self.mineru_timeout.setMaximumWidth(140)
        mineru_grid.addWidget(self.mineru_timeout, 5, 1, Qt.AlignLeft)
        self.mineru_fallback = QCheckBox("MinerU 失败时回退 PyMuPDF")
        mineru_grid.addWidget(self.mineru_fallback, 6, 0, 1, 3)
        self.mineru_keep = QCheckBox("保留 MinerU 中间产物（Markdown/JSON/图片）")
        mineru_grid.addWidget(self.mineru_keep, 7, 0, 1, 3)
        self.mineru_status = QLabel("")
        self.mineru_status.setObjectName("Tiny")
        self.mineru_status.setWordWrap(True)
        mineru_grid.addWidget(self.mineru_status, 8, 0, 1, 3)
        mineru_grid.setColumnStretch(1, 1)
        self.mineru_card.body.addLayout(mineru_grid)
        mineru_actions = QHBoxLayout()
        mineru_actions.setSpacing(8)
        self.mineru_detect_button = QPushButton("🔍 检测 MinerU")
        self.mineru_detect_button.setObjectName("Pill")
        self.mineru_detect_button.clicked.connect(self._detect_mineru)
        self.mineru_save_button = QPushButton("💾 保存 MinerU 设置")
        self.mineru_save_button.setObjectName("Primary")
        self.mineru_save_button.clicked.connect(self._save_mineru)
        open_mineru = QPushButton("📂 打开 MinerU 缓存")
        open_mineru.setObjectName("Pill")
        open_mineru.clicked.connect(self._open_mineru_cache)
        install_mineru = QPushButton("安装说明")
        install_mineru.setObjectName("Ghost")
        install_mineru.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl("https://opendatalab.github.io/MinerU/quick_start/")
            )
        )
        mineru_actions.addWidget(self.mineru_detect_button)
        mineru_actions.addWidget(self.mineru_save_button)
        mineru_actions.addWidget(open_mineru)
        mineru_actions.addWidget(install_mineru)
        mineru_actions.addStretch(1)
        self.mineru_card.body.addLayout(mineru_actions)
        layout.addWidget(self.mineru_card)
        self._load_mineru_settings()

        # ---------------------------------------------------------- storage/update
        storage_card = SectionCard("存储与热更新", "app_core 可被 zip 替换")
        storage_grid = QGridLayout()
        storage_grid.setHorizontalSpacing(14)
        storage_grid.setVerticalSpacing(5)
        storage_grid.addWidget(QLabel("数据目录"), 0, 0)
        workspace_label = QLabel(str(self.workspace))
        workspace_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        workspace_label.setWordWrap(True)
        storage_grid.addWidget(workspace_label, 0, 1)
        storage_grid.addWidget(QLabel("应用版本"), 1, 0)
        self.version_label = QLabel("")
        storage_grid.addWidget(self.version_label, 1, 1)
        storage_grid.addWidget(QLabel("更新方式"), 2, 0)
        update_hint = QLabel(
            "选择 .zip → 校验 version.json/main_entry.py → 暂存 → 重启时替换 app_core；失败自动回滚。"
        )
        update_hint.setObjectName("Muted")
        update_hint.setWordWrap(True)
        storage_grid.addWidget(update_hint, 2, 1)
        storage_grid.setColumnStretch(1, 1)
        storage_card.body.addLayout(storage_grid)
        self.update_error = QLabel("")
        self.update_error.setObjectName("StatusWarn")
        self.update_error.setWordWrap(True)
        self.update_error.setVisible(False)
        storage_card.body.addWidget(self.update_error)
        storage_actions = QHBoxLayout()
        open_folder = QPushButton("打开数据目录")
        open_folder.setObjectName("Pill")
        open_folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.workspace)))
        )
        update_button = QPushButton("导入更新包 (.zip)")
        update_button.setObjectName("Primary")
        update_button.clicked.connect(self._import_update)
        storage_actions.addWidget(open_folder)
        storage_actions.addWidget(update_button)
        storage_actions.addStretch(1)
        storage_card.body.addLayout(storage_actions)
        layout.addWidget(storage_card)

        # --------------------------------------------------------- download sources
        source_card = SectionCard(
            "🌐 文献自动下载与备用源配置", "arXiv → 出版商直连 → 自定义备用源 → 手动清单"
        )
        self.level2_enabled = QCheckBox("启用校园网 / 机构 IP 出版商直连（优先路径）")
        self.level2_enabled.setChecked(
            bool(self.config.get("crawler.playwright_enabled", True))
        )
        self.level3_enabled = QCheckBox("启用自定义备用下载源")
        self.level3_enabled.setChecked(bool(self.config.get("crawler.scihub_enabled", False)))
        self.level2_enabled.stateChanged.connect(self._schedule_mirror_save)
        self.level3_enabled.stateChanged.connect(self._schedule_mirror_save)
        source_card.body.addWidget(self.level2_enabled)
        source_card.body.addWidget(self.level3_enabled)
        mirror_hint = QLabel(
            "每行一个备用源域名（如 https://your-mirror.example）。仓库不附带、不硬编码任何地址；"
            "请只访问你有权访问的文献，并遵守出版商与机构条款。"
        )
        mirror_hint.setObjectName("Tiny")
        mirror_hint.setWordWrap(True)
        source_card.body.addWidget(mirror_hint)
        self.mirror_edit = QPlainTextEdit()
        self.mirror_edit.setPlainText(
            "\n".join(
                str(item)
                for item in (self.config.get("crawler.scihub_mirrors", []) or [])
            )
        )
        self.mirror_edit.setPlaceholderText("https://mirror-a.example\nhttps://mirror-b.example")
        self.mirror_edit.setMaximumHeight(92)
        self.mirror_edit.textChanged.connect(self._schedule_mirror_save)
        source_card.body.addWidget(self.mirror_edit)
        probe_row = QHBoxLayout()
        probe_row.setSpacing(8)
        self.probe_button = QPushButton("⚡ 测试镜像连通性")
        self.probe_button.setObjectName("Pill")
        self.probe_button.clicked.connect(self._probe_mirrors)
        self.probe_result = QLabel("")
        self.probe_result.setObjectName("Tiny")
        self.probe_result.setWordWrap(True)
        probe_row.addWidget(self.probe_button)
        probe_row.addWidget(self.probe_result, 1)
        source_card.body.addLayout(probe_row)
        layout.addWidget(source_card)

        # ---------------------------------------------------------- maintenance
        maintenance = SectionCard("数据维护", "示例数据可一键清除")
        maintenance_text = QLabel(
            "载入示例数据用于体验 L1/L2/L3、冲突雷达与证据抽屉；清除只删除带 demo 标记的文献。"
        )
        maintenance_text.setObjectName("Muted")
        maintenance_text.setWordWrap(True)
        maintenance.body.addWidget(maintenance_text)
        row = QHBoxLayout()
        row.setSpacing(8)
        seed_button = QPushButton("载入示例数据")
        seed_button.setObjectName("Pill")
        seed_button.clicked.connect(self._seed_demo)
        clear_button = QPushButton("清除示例数据")
        clear_button.setObjectName("Danger")
        clear_button.clicked.connect(self._clear_demo)
        reindex_button = QPushButton("重建 FTS 索引")
        reindex_button.setObjectName("Pill")
        reindex_button.clicked.connect(self._reindex)
        vacuum_button = QPushButton("压缩数据库")
        vacuum_button.setObjectName("Pill")
        vacuum_button.clicked.connect(self._vacuum)
        for button in (seed_button, clear_button, reindex_button, vacuum_button):
            row.addWidget(button)
        row.addStretch(1)
        maintenance.body.addLayout(row)
        layout.addWidget(maintenance)
        layout.addStretch(1)
        self.refresh()

    # ------------------------------------------------------------------ state
    def refresh(self) -> None:
        version = self.patch.current_version()
        self.version_label.setText(
            f"{version.get('version', '未知')} · build {version.get('build', '—')}"
            f" · schema {version.get('schema_ver', '2')}"
        )
        error_file = self.workspace / "last_update_error.txt"
        if error_file.exists():
            try:
                self.update_error.setText("上次更新失败：" + error_file.read_text(encoding="utf-8"))
                self.update_error.setVisible(True)
            except OSError:
                pass
        self._refresh_builtin()
        self._refresh_health()
        if hasattr(self, "model_pool_panel"):
            self.model_pool_panel.refresh()

    def _refresh_builtin(self) -> None:
        while self.builtin_grid.count():
            item = self.builtin_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        statuses = self.gateway.registry.status() if self.gateway else []
        for row, status in enumerate(statuses):
            name_text = {
                "spacy_en": "spaCy 句法模型",
                "mini_cluster_embed": "MiniLM 同义聚类",
                "conflict_nli": "DeBERTa NLI 冲突",
                "local_reranker": "MiniLM Reranker 精排",
            }.get(status["name"], status["name"])
            name = QLabel(name_text)
            name.setObjectName("Muted")
            detail = QLabel(status.get("description", ""))
            detail.setObjectName("Tiny")
            detail.setWordWrap(True)
            active = bool(status.get("active"))
            present = bool(status.get("present"))
            badge = status.get("badge") or ("🟢 ONNX 已激活" if active else "🟡 启发式降级中")
            chip = make_chip(badge, "StatusOk" if active else "StatusWarn")
            chip.setToolTip(status.get("path", ""))
            self.builtin_grid.addWidget(name, row, 0)
            self.builtin_grid.addWidget(detail, row, 1)
            self.builtin_grid.addWidget(chip, row, 2)
            self.builtin_grid.addWidget(
                make_chip("文件已就绪" if present else "未下载", "StatusOff"),
                row,
                3,
            )
            self.builtin_grid.setColumnStretch(1, 1)

    def _refresh_health(self) -> None:
        if not self.gateway:
            return
        health = self.gateway.health()
        messages = [
            f"{ROLE_LABELS.get(str(item.get('name')), str(item.get('name')))}: "
            f"{'可用' if item.get('available') else '未配置'}"
            for item in health
            if not item.get("available")
        ]
        if messages and hasattr(self, "model_status_line"):
            self.model_status_line.setText(" · ".join(messages[:4]))

    # ------------------------------------------------------------------ actions
    def _load_mineru_settings(self) -> None:
        cfg = MinerUConfig.from_config(self.config)
        self.mineru_enabled.setChecked(cfg.enabled)
        self.mineru_command.setText(cfg.command)
        for combo, value in (
            (self.mineru_tier, cfg.tier),
            (self.mineru_ocr, cfg.ocr_mode),
            (self.mineru_device, cfg.device),
        ):
            index = combo.findData(value)
            combo.setCurrentIndex(index if index >= 0 else 0)
        self.mineru_timeout.setValue(max(60, min(7200, cfg.timeout_sec)))
        self.mineru_fallback.setChecked(cfg.fallback_to_pymupdf)
        self.mineru_keep.setChecked(cfg.keep_artifacts)
        self.mineru_status.setText(
            "未检测。安装 MinerU 4 后点击“检测 MinerU”。"
            if not cfg.command
            else f"当前命令：{cfg.command}"
        )

    def _browse_mineru(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择 MinerU 命令",
            str(self.workspace),
            "可执行文件 (*.exe);;所有文件 (*.*)",
        )
        if path:
            self.mineru_command.setText(path)

    def _save_mineru(self) -> None:
        self.config.set("parser.mineru.enabled", self.mineru_enabled.isChecked())
        self.config.set(
            "parser.mineru.command", self.mineru_command.text().strip() or "mineru-kit"
        )
        self.config.set(
            "parser.mineru.tier",
            str(self.mineru_tier.currentData() or "standard"),
        )
        self.config.set(
            "parser.mineru.ocr_mode",
            str(self.mineru_ocr.currentData() or "auto"),
        )
        self.config.set(
            "parser.mineru.device",
            str(self.mineru_device.currentData() or "auto"),
        )
        self.config.set(
            "parser.mineru.timeout_sec", int(self.mineru_timeout.value())
        )
        self.config.set(
            "parser.mineru.fallback_to_pymupdf",
            self.mineru_fallback.isChecked(),
        )
        self.config.set(
            "parser.mineru.keep_artifacts", self.mineru_keep.isChecked()
        )
        try:
            self.config.save()
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.toast.emit("MinerU 设置已保存；新解析任务生效")
        self._detect_mineru()

    def _detect_mineru(self) -> None:
        cfg = MinerUConfig.from_config(self.config)
        cfg.command = self.mineru_command.text().strip() or cfg.command
        probe = MinerUConverter(self.workspace, cfg).probe()
        if probe.get("available"):
            self.mineru_status.setText(
                f"✅ MinerU {probe.get('version')} · {probe.get('command')}"
            )
        else:
            self.mineru_status.setText(
                f"❌ 不可用：{probe.get('error') or '未找到 mineru-kit'}"
            )

    def _open_mineru_cache(self) -> None:
        path = self.workspace / "mineru"
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _download_models(self) -> None:
        if self._model_worker is not None and self._model_worker.isRunning():
            self.toast.emit("ONNX 模型正在下载中…")
            return
        if not self.gateway:
            self.toast.emit("模型网关未初始化")
            return
        self.model_progress.setVisible(True)
        self.model_progress.setRange(0, 0)
        self.model_progress.setFormat("正在连接镜像…")
        self.download_models_button.setEnabled(False)
        self.import_models_button.setEnabled(False)
        self.model_status_line.setText("正在从 hf-mirror.com 下载，失败自动回退 huggingface.co…")
        worker = ModelDownloadWorker(self.gateway.registry, force=True, parent=self)
        worker.progress_event.connect(self._on_model_progress)
        worker.finished_download.connect(self._on_models_downloaded)
        worker.failed.connect(self._on_model_download_failed)
        worker.finished.connect(worker.deleteLater)
        self._model_worker = worker
        worker.start()

    def _on_model_progress(self, event: dict) -> None:
        kind = event.get("event")
        label = event.get("label") or event.get("model") or ""
        if kind == "progress":
            total = int(event.get("total") or 0)
            if total:
                self.model_progress.setRange(0, 100)
                self.model_progress.setValue(int(event.get("percent") or 0))
                self.model_progress.setFormat(
                    f"{label}  %p%  ({event.get('downloaded', 0) // 1048576} / "
                    f"{total // 1048576} MB)"
                )
            else:
                self.model_progress.setRange(0, 0)
        elif kind == "start":
            self.model_progress.setRange(0, 0)
            self.model_progress.setFormat(f"{label} 下载中…")
        elif kind == "done":
            self.model_progress.setRange(0, 100)
            self.model_progress.setValue(100)
        elif kind in ("model_start", "all_done"):
            if label:
                self.model_status_line.setText(f"处理 {label}…")

    def _on_models_downloaded(self, result: dict) -> None:
        self.download_models_button.setEnabled(True)
        self.import_models_button.setEnabled(True)
        self._model_worker = None
        ok = bool(result.get("ok"))
        self.model_progress.setRange(0, 100)
        self.model_progress.setValue(100 if ok else 0)
        self.model_progress.setVisible(not ok)
        errors = result.get("errors") or []
        self.model_status_line.setText(
            "全部 ONNX 模型已下载并通过 SHA256/完整性校验，本地检索已满血激活。"
            if ok
            else "部分模型下载失败：" + "；".join(errors[:3])
        )
        self.refresh()
        self.models_changed.emit()
        self.toast.emit("ONNX 模型已下载并激活" if ok else "部分 ONNX 模型不可用，请检查网络")

    def _on_model_download_failed(self, message: str) -> None:
        self.download_models_button.setEnabled(True)
        self.import_models_button.setEnabled(True)
        self._model_worker = None
        self.model_progress.setVisible(False)
        self.model_status_line.setText(f"下载失败：{message}")
        self.toast.emit(f"ONNX 下载失败：{message}")

    def _import_models(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "选择包含 ONNX / tokenizer.json 的模型目录", str(self.workspace / "models")
        )
        if not folder:
            return
        try:
            result = self.gateway.registry.import_from_folder(folder)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        imported = sum(1 for value in (result.get("imported") or {}).values() if value)
        missing = result.get("missing") or []
        self.refresh()
        self.models_changed.emit()
        self.toast.emit(
            f"已导入 {imported} 个模型" + (f"，缺少：{', '.join(missing)}" if missing else "")
        )

    def _schedule_mirror_save(self) -> None:
        self._mirror_timer.start()

    def _save_mirrors(self) -> None:
        values = []
        for line in self.mirror_edit.toPlainText().splitlines():
            normalized = normalize_mirror(line)
            if normalized and normalized not in values:
                values.append(normalized)
        self.config.set("crawler.playwright_enabled", self.level2_enabled.isChecked())
        self.config.set("crawler.scihub_enabled", self.level3_enabled.isChecked())
        self.config.set("crawler.scihub_mirrors", values)
        try:
            self.config.save()
        except Exception as exc:
            self.toast.emit(f"镜像配置保存失败：{exc}")

    def _probe_mirrors(self) -> None:
        self._save_mirrors()
        mirrors = [
            normalize_mirror(line)
            for line in self.mirror_edit.toPlainText().splitlines()
        ]
        mirrors = [item for item in mirrors if item]
        if not mirrors:
            self.toast.emit("请先填写至少一个镜像域名")
            return
        if self._probe_worker is not None and self._probe_worker.isRunning():
            return
        self.probe_button.setEnabled(False)
        self.probe_result.setText("正在测试连通性…")
        worker = MirrorProbeWorker(mirrors, parent=self)
        worker.finished_results.connect(self._on_probe_done)
        worker.failed.connect(lambda message: self._on_probe_done([], message))
        worker.finished.connect(worker.deleteLater)
        self._probe_worker = worker
        worker.start()

    def _on_probe_done(self, results: list[dict], error: str = "") -> None:
        self.probe_button.setEnabled(True)
        self._probe_worker = None
        if error:
            self.probe_result.setText(f"测试失败：{error}")
            return
        parts = []
        for item in results:
            icon = "🟢" if item.get("ok") else "🔴"
            parts.append(
                f"{icon} {item.get('mirror')} "
                f"HTTP {item.get('status')} · {item.get('latency_ms')} ms"
            )
        self.probe_result.setText("   ".join(parts))

    def _on_pool_changed(self) -> None:
        self.models_changed.emit()
        self._refresh_health()

    def _import_update(self) -> None:
        # MainWindow owns the safe shutdown + relaunch sequence.
        self.update_requested.emit()

    def _seed_demo(self) -> None:
        counts = seed_demo_v2(self.db)
        self.data_changed.emit()
        self.toast.emit(
            f"已载入示例：{counts['documents']} 篇文献、{counts['cards']} 张卡片、"
            f"{counts['keywords']} 个关键词"
        )
        self.refresh()

    def _clear_demo(self) -> None:
        answer = QMessageBox.question(
            self,
            "清除示例数据",
            "将删除所有带 demo 标记的文献、卡片与证据；真实数据不受影响。继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        count = self.db.clear_demo_documents()
        self.data_changed.emit()
        self.toast.emit(f"已清除 {count} 篇示例文献")
        self.refresh()

    def _reindex(self) -> None:
        self.db.rebuild_fts()
        self.toast.emit("FTS5 索引已重建")
        self.refresh()

    def _vacuum(self) -> None:
        self.db.vacuum()
        self.toast.emit("数据库已压缩")
