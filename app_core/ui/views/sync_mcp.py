"""Sync & MCP view: .acdb packs, WebDAV/S3, local MCP server."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ui.controls import WheelSafeComboBox as QComboBox

from db.sqlite_manager import SQLiteManager
from sync_mcp.acdb_packager import ACDBPackager
from sync_mcp.cloud_sync import CloudSyncManager
from sync_mcp.mcp_server import KnowledgeMCPServer, build_mcp_config
from ui.m3_theme import format_time
from ui.widgets import PageHeader, SectionCard, make_chip
from ui.workers import PackageWorker, SyncWorker
from updater.updater import clean_launch_env


def _free_port(host: str, start: int, attempts: int = 30) -> int:
    for port in range(int(start), int(start) + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    return int(start)


class SyncMCPView(QWidget):
    toast = Signal(str)
    data_changed = Signal()

    def __init__(
        self,
        db: SQLiteManager,
        packager: ACDBPackager,
        sync_manager: CloudSyncManager,
        mcp_server: KnowledgeMCPServer,
        qdrant: Any,
        base_dir: str,
        config: Any = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.packager = packager
        self.sync_manager = sync_manager
        self.mcp_server = mcp_server
        self.qdrant = qdrant
        self.base_dir = Path(base_dir)
        self.config = config
        self._workers: list[Any] = []
        self._mcp_process: subprocess.Popen | None = None

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
            "同步与 MCP",
            "导出知识库数据包以供备份或团队共享，或开启本地 MCP 接口供 Claude / Cursor 等外部 AI 助手安全连接检索。",
        )
        refresh_button = QPushButton("刷新")
        refresh_button.setObjectName("Ghost")
        refresh_button.clicked.connect(self.refresh)
        header.add_action(refresh_button)
        layout.addWidget(header)

        grid = QGridLayout()
        grid.setSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        # ---------------------------------------------------------- .acdb
        pack_card = SectionCard("数据包 · .acdb", "按 DOC-/KNW- ID 增量合并")
        pack_text = QLabel(
            "导出 manifest + meta_dump.jsonl + vectors.jsonl + 可选 PDF；"
            "导入时相同 ID 合并，新 ID 插入，不删除本地数据。"
        )
        pack_text.setObjectName("Muted")
        pack_text.setWordWrap(True)
        pack_card.body.addWidget(pack_text)
        pack_row = QHBoxLayout()
        export_button = QPushButton("导出全部")
        export_button.setObjectName("Pill")
        export_button.clicked.connect(self._export_pack)
        import_button = QPushButton("导入知识包")
        import_button.setObjectName("Pill")
        import_button.clicked.connect(self._import_pack)
        pack_row.addWidget(export_button)
        pack_row.addWidget(import_button)
        pack_row.addStretch(1)
        pack_card.body.addLayout(pack_row)
        self.pack_count = QLabel("")
        self.pack_count.setObjectName("Tiny")
        pack_card.body.addWidget(self.pack_count)
        pack_card.body.addStretch(1)
        grid.addWidget(pack_card, 0, 0)

        # ------------------------------------------------------------- MCP
        mcp_card = SectionCard("MCP Server", "stdio / SSE")
        self.mcp_status = make_chip("未启动", "StatusOff")
        mcp_card.body.addWidget(self.mcp_status)
        mcp_text = QLabel(
            "检索工具：query_calculation_strategy · explain_atomic_model · lookup_by_id · "
            "get_code_template · search_knowledge · get_raw_evidence\n"
            "Agent 工单工具：list_workbench_tasks · parse_document_for_agent · "
            "create_semi_auto_batch_run · list_semi_auto_runs · get_semi_auto_batch_prompt · "
            "submit_semi_auto_batch_reply · finalize_semi_auto_run · "
            "export_document_audit_ticket · get_audit_ticket_content · submit_audit_ticket_result"
        )
        mcp_text.setObjectName("Muted")
        mcp_text.setWordWrap(True)
        mcp_card.body.addWidget(mcp_text)
        self.mcp_task_write_box = QCheckBox(
            "启用 MCP 任务工单接口（允许 Agent 领包/解析/暂存回复与导出审计）"
        )
        self.mcp_direct_write_box = QCheckBox(
            "允许 Agent 通过 MCP 直接写入正式知识库（提取入库与审计应用）"
        )
        self.mcp_task_write_box.toggled.connect(self._save_mcp_permissions)
        self.mcp_direct_write_box.toggled.connect(self._save_mcp_permissions)
        mcp_card.body.addWidget(self.mcp_task_write_box)
        mcp_card.body.addWidget(self.mcp_direct_write_box)
        exe_path = self.base_dir / ("AtomicCalculationDB.exe" if getattr(sys, "frozen", False) else "launcher.py")
        self.mcp_json = QTextBrowser()
        self.mcp_json.setObjectName("JsonView")
        self.mcp_json.setMaximumHeight(130)
        self.mcp_json.setPlainText(
            json.dumps(build_mcp_config(exe_path), ensure_ascii=False, indent=2)
        )
        mcp_card.body.addWidget(self.mcp_json)
        mcp_row = QHBoxLayout()
        copy_button = QPushButton("复制配置")
        copy_button.setObjectName("Pill")
        copy_button.clicked.connect(self._copy_mcp)
        self.mcp_toggle = QPushButton("启动 SSE")
        self.mcp_toggle.setObjectName("Primary")
        self.mcp_toggle.clicked.connect(self._toggle_mcp)
        mcp_row.addWidget(copy_button)
        mcp_row.addWidget(self.mcp_toggle)
        mcp_row.addStretch(1)
        mcp_card.body.addLayout(mcp_row)
        mcp_card.body.addStretch(1)
        grid.addWidget(mcp_card, 0, 1)

        # ---------------------------------------------------------- WebDAV
        webdav_card = SectionCard("WebDAV", "坚果云 / Nextcloud / 群晖")
        self.webdav_status = make_chip("未配置", "StatusOff")
        webdav_card.body.addWidget(self.webdav_status)
        webdav_text = QLabel("工作台运行结束后只上传发生变化的 SQLite 快照、PDF 与向量文件。")
        webdav_text.setObjectName("Muted")
        webdav_text.setWordWrap(True)
        webdav_card.body.addWidget(webdav_text)
        webdav_button = QPushButton("立即同步")
        webdav_button.setObjectName("Pill")
        webdav_button.clicked.connect(lambda: self._sync("webdav"))
        webdav_card.body.addWidget(webdav_button, 0, Qt.AlignLeft)
        webdav_card.body.addStretch(1)
        grid.addWidget(webdav_card, 1, 0)

        s3_card = SectionCard("S3 协议", "AWS S3 / R2 / OSS / MinIO")
        self.s3_status = make_chip("未配置", "StatusOff")
        s3_card.body.addWidget(self.s3_status)
        s3_text = QLabel("使用 boto3 增量同步；可与 WebDAV 同时启用。")
        s3_text.setObjectName("Muted")
        s3_text.setWordWrap(True)
        s3_card.body.addWidget(s3_text)
        s3_button = QPushButton("立即同步")
        s3_button.setObjectName("Pill")
        s3_button.clicked.connect(lambda: self._sync("s3"))
        s3_card.body.addWidget(s3_button, 0, Qt.AlignLeft)
        s3_card.body.addStretch(1)
        grid.addWidget(s3_card, 1, 1)

        # ---------------------------------------------------------- Qdrant
        vector_card = SectionCard("Qdrant 本地向量库", "BGE-M3 · 1024 维")
        self.vector_info = QLabel("")
        self.vector_info.setObjectName("Muted")
        self.vector_info.setWordWrap(True)
        vector_card.body.addWidget(self.vector_info)
        self.vector_path = QLabel("")
        self.vector_path.setObjectName("Tiny")
        self.vector_path.setWordWrap(True)
        vector_card.body.addWidget(self.vector_path)
        vector_card.body.addStretch(1)
        grid.addWidget(vector_card, 2, 0, 1, 2)

        layout.addStretch(1)
        self.refresh()

    # ------------------------------------------------------------------ state
    def refresh(self) -> None:
        stats = self.db.stats()
        self.pack_count.setText(
            f"当前 {stats['documents']} 篇文献 · {stats['cards']} 张卡片 · "
            f"{stats['evidence']} 条证据"
        )
        webdav_enabled = bool(self.config.get("sync.webdav.enabled", False)) if self.config else False
        s3_enabled = bool(self.config.get("sync.s3.enabled", False)) if self.config else False
        self._set_chip(self.webdav_status, "已启用" if webdav_enabled else "未配置", webdav_enabled)
        self._set_chip(self.s3_status, "已启用" if s3_enabled else "未配置", s3_enabled)
        if self.qdrant is None:
            self.vector_info.setText("向量库未启用。")
            self.vector_path.setText("")
        else:
            status = self.qdrant.status()
            if status.ok:
                self.vector_info.setText(
                    f"正常 · 集合 {status.collection} · 维度 {status.dimension} · "
                    f"{status.count} 个向量"
                )
                self.vector_path.setText(f"持久化目录：{status.path}")
            else:
                self.vector_info.setText(f"不可用：{status.message}")
                self.vector_path.setText(f"路径：{status.path}")
        running = self._mcp_process is not None and self._mcp_process.poll() is None
        self._set_chip(self.mcp_status, "SSE 运行中" if running else "未启动", running)
        self.mcp_toggle.setText("停止 SSE" if running else "启动 SSE")
        if self.config is not None:
            task_write = bool(self.config.get("mcp.allow_task_write", True))
            direct_write = bool(self.config.get("mcp.allow_direct_db_write", False))
            self.mcp_task_write_box.blockSignals(True)
            self.mcp_direct_write_box.blockSignals(True)
            self.mcp_task_write_box.setChecked(task_write)
            self.mcp_direct_write_box.setChecked(direct_write)
            self.mcp_direct_write_box.setEnabled(task_write)
            self.mcp_task_write_box.blockSignals(False)
            self.mcp_direct_write_box.blockSignals(False)

    def _save_mcp_permissions(self) -> None:
        if self.config is None:
            return
        task_write = self.mcp_task_write_box.isChecked()
        direct_write = self.mcp_direct_write_box.isChecked()
        self.mcp_direct_write_box.setEnabled(task_write)
        self.config.set("mcp.allow_task_write", task_write)
        self.config.set("mcp.allow_direct_db_write", direct_write)
        self.config.save()
        self.toast.emit(
            "MCP 权限已保存："
            + ("允许工单" if task_write else "仅只读检索")
            + (" · 允许直写正式库" if (task_write and direct_write) else " · 仅暂存不直写")
        )

    @staticmethod
    def _set_chip(chip: QLabel, text: str, active: bool) -> None:
        chip.setText(text)
        chip.setObjectName("StatusOk" if active else "StatusOff")
        style = chip.style()
        style.unpolish(chip)
        style.polish(chip)

    # ------------------------------------------------------------------ pack
    def _export_pack(self) -> None:
        default = str(self.base_dir / "workspace" / "exports" / "knowledge.acdb")
        path, _ = QFileDialog.getSaveFileName(self, "导出 .acdb 知识包", default, "ACDB (*.acdb)")
        if not path:
            return
        worker = PackageWorker("export", self.packager, path)
        worker.finished_result.connect(lambda result: self._pack_done("导出", result))
        worker.failed.connect(lambda message: self.toast.emit(f"导出失败：{message}"))
        worker.finished.connect(lambda: self._cleanup(worker))
        self._workers.append(worker)
        worker.start()
        self.toast.emit("正在导出知识包…")

    def _import_pack(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入 .acdb 知识包", str(self.base_dir / "workspace"), "ACDB (*.acdb)"
        )
        if not path:
            return
        worker = PackageWorker("import", self.packager, path)
        worker.finished_result.connect(lambda result: self._pack_done("导入", result))
        worker.failed.connect(lambda message: self.toast.emit(f"导入失败：{message}"))
        worker.finished.connect(lambda: self._cleanup(worker))
        self._workers.append(worker)
        worker.start()
        self.toast.emit("正在导入知识包…")

    def _pack_done(self, action: str, result: Any) -> None:
        if action == "导出":
            self.toast.emit(f"导出完成：{result}")
        else:
            self.toast.emit(f"导入完成：{result}")
            self.data_changed.emit()
        self.refresh()

    # ------------------------------------------------------------------- sync
    def _sync(self, provider: str) -> None:
        enabled = bool(self.config.get(f"sync.{provider}.enabled", False)) if self.config else False
        if not enabled:
            self.toast.emit(f"{provider.upper()} 尚未在设置中启用")
            return
        worker = SyncWorker(self.sync_manager, provider)
        worker.finished_result.connect(self._sync_done)
        worker.failed.connect(lambda message: self.toast.emit(f"同步失败：{message}"))
        worker.finished.connect(lambda: self._cleanup(worker))
        self._workers.append(worker)
        worker.start()
        self.toast.emit(f"正在执行 {provider.upper()} 增量同步…")

    def _sync_done(self, result: Any) -> None:
        self.toast.emit(result.message or f"{result.provider} 同步完成")
        if getattr(result, "merged_documents", 0) or getattr(result, "merged_cards", 0) or getattr(result, "updated_cards", 0):
            self.data_changed.emit()
        self.refresh()

    # -------------------------------------------------------------------- MCP
    def _copy_mcp(self) -> None:
        QGuiApplication.clipboard().setText(self.mcp_json.toPlainText())
        self.toast.emit("MCP 配置已复制到剪贴板")

    def _toggle_mcp(self) -> None:
        if self._mcp_process is not None and self._mcp_process.poll() is None:
            self._mcp_process.terminate()
            self._mcp_process = None
            self.toast.emit("MCP SSE 服务已停止")
            self.refresh()
            return
        if not self.mcp_server.available:
            self.toast.emit("mcp 包未安装，无法启动本地服务")
            return
        configured_port = int(self.config.get("mcp.port", 8765)) if self.config else 8765
        host = str(self.config.get("mcp.host", "127.0.0.1")) if self.config else "127.0.0.1"
        # The in-process runtime bridge normally owns 8765; allocate the next
        # free port for the optional SSE listener.
        port = _free_port(host, configured_port)
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--mcp-sse", "--host", host, "--port", str(port)]
        else:
            command = [
                sys.executable,
                str(self.base_dir / "launcher.py"),
                "--mcp-sse",
                "--host",
                host,
                "--port",
                str(port),
            ]
        log_path = self.base_dir / "workspace" / "logs" / "mcp_sse.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = open(log_path, "a", encoding="utf-8")
            self._mcp_process = subprocess.Popen(
                command,
                cwd=str(self.base_dir),
                stdout=handle,
                stderr=handle,
                env=clean_launch_env(),
            )
            self.toast.emit(f"MCP SSE 已启动：http://{host}:{port}")
        except Exception as exc:
            self.toast.emit(f"MCP 启动失败：{exc}")
        self.refresh()

    def _cleanup(self, worker: Any) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        worker.deleteLater()

    def shutdown(self) -> None:
        if self._mcp_process is not None and self._mcp_process.poll() is None:
            self._mcp_process.terminate()
            self._mcp_process = None
