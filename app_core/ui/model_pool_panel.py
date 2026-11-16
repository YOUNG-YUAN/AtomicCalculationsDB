"""Settings panel for the V2.3 endpoint pool / strategy / time routing."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.controls import WheelSafeComboBox as QComboBox

from models.endpoint_pool import (
    NODE_TYPE_LABELS,
    ROLE_KEYS,
    ROLE_LABELS,
    EndpointPool,
    EndpointNode,
    ProtectedEndpointError,
    RoleBinding,
)
from models.web_ai_bridge import WebAIBridge, web_model_settings
from ui.model_pool_dialogs import (
    EndpointNodeDialog,
    RoleRulesDialog,
    StrategyDialog,
)
from ui.widgets import SectionCard


class _LoginWorker(QThread):
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, workspace: str, node: EndpointNode, parent=None):
        super().__init__(parent)
        self.workspace = workspace
        self.node = node

    def run(self) -> None:
        try:
            profile = WebAIBridge(self.workspace).open_login(self.node)
            self.finished_ok.emit(str(profile))
        except Exception as exc:
            self.failed.emit(str(exc))


class ModelPoolPanel(QWidget):
    changed = Signal()
    toast = Signal(str)

    def __init__(
        self,
        config: Any,
        pool: EndpointPool,
        workspace: str,
        gateway: Any = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.config = config
        self.pool = pool
        self.workspace = workspace
        self.gateway = gateway
        self._login_worker: _LoginWorker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.role_card = SectionCard(
            "🕒 角色绑定与分时段策略路由表",
            "默认策略 + 时间段切换；到点自动生效，无需重启",
        )
        self.preview_label = QLabel("")
        self.preview_label.setObjectName("SectionHint")
        self.preview_label.setWordWrap(True)
        self.role_card.body.addWidget(self.preview_label)
        self.role_rows: dict[str, dict[str, Any]] = {}
        for role in ROLE_KEYS:
            row = QHBoxLayout()
            row.setSpacing(8)
            label = QLabel(ROLE_LABELS[role])
            label.setMinimumWidth(72)
            row.addWidget(label)
            combo = QComboBox()
            combo.setMinimumWidth(220)
            combo.currentIndexChanged.connect(
                lambda _index, key=role: self._save_role_default(key)
            )
            row.addWidget(combo, 1)
            preview = QLabel("")
            preview.setObjectName("Tiny")
            row.addWidget(preview, 1)
            rules = QPushButton("分时段规则")
            rules.setObjectName("Ghost")
            rules.clicked.connect(
                lambda _checked=False, key=role: self._edit_rules(key)
            )
            row.addWidget(rules)
            self.role_card.body.addLayout(row)
            self.role_rows[role] = {"combo": combo, "preview": preview}
        layout.addWidget(self.role_card)

        self.strategy_card = SectionCard(
            "🔀 模型调度策略编排器",
            "接力顺序、全链冷却与循环次数",
        )
        self.strategy_list = QListWidget()
        self.strategy_list.setMinimumHeight(130)
        self.strategy_card.body.addWidget(self.strategy_list)
        strategy_actions = QHBoxLayout()
        for label, callback in (
            ("➕ 新建策略", self._add_strategy),
            ("编辑", self._edit_strategy),
            ("删除", self._delete_strategy),
        ):
            button = QPushButton(label)
            button.setObjectName("Pill" if "新建" in label else "Ghost")
            button.clicked.connect(callback)
            strategy_actions.addWidget(button)
        strategy_actions.addStretch(1)
        self.strategy_card.body.addLayout(strategy_actions)
        layout.addWidget(self.strategy_card)

        self.node_card = SectionCard(
            "🏊 统一模型与算力池",
            "每个节点独立容量、可用时段与排队参数",
        )
        self.node_list = QListWidget()
        self.node_list.setMinimumHeight(160)
        self.node_list.currentItemChanged.connect(
            lambda *_args: self._sync_node_actions()
        )
        self.node_card.body.addWidget(self.node_list)
        node_actions = QHBoxLayout()
        for label, callback in (
            ("➕ 新增节点", self._add_node),
            ("编辑", self._edit_node),
            ("删除", self._delete_node),
            ("⚡ 测试连接", self._test_node),
            ("🔑 登录网页端", self._login_node),
        ):
            button = QPushButton(label)
            button.setObjectName("Pill" if "新增" in label else "Ghost")
            button.clicked.connect(callback)
            node_actions.addWidget(button)
            if label == "删除":
                self.node_delete_button = button
        node_actions.addStretch(1)
        self.node_card.body.addLayout(node_actions)
        layout.addWidget(self.node_card)
        self._timer = QTimer(self)
        self._timer.setInterval(30_000)
        self._timer.timeout.connect(self._refresh_preview)
        self._timer.start()
        self.refresh()

    # ------------------------------------------------------------------ state
    def refresh(self) -> None:
        self.pool.load()
        self._refresh_roles()
        self._refresh_strategies()
        self._refresh_nodes()
        self._refresh_preview()

    def _refresh_roles(self) -> None:
        strategies = list(self.pool.strategies.values())
        for role, widgets in self.role_rows.items():
            combo: QComboBox = widgets["combo"]
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("（未绑定）", "")
            for strategy in strategies:
                combo.addItem(strategy.strategy_name, strategy.strategy_id)
            binding = self.pool.bindings.get(role) or RoleBinding()
            index = combo.findData(binding.default_strategy_id)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)
            widgets["preview"].setText(self.pool.preview(role))
        self._refresh_preview()

    def _refresh_strategies(self) -> None:
        self.strategy_list.clear()
        for strategy in self.pool.strategies.values():
            chain = " ➔ ".join(
                self.pool.node_summary(node_id) for node_id in strategy.relay_chain
            ) or "（空链）"
            item = QListWidgetItem(
                f"{strategy.strategy_name}  ·  {chain}"
                + (
                    f"  ·  🔁 {strategy.max_loops} 轮"
                    if strategy.enable_loop
                    else "  ·  不循环"
                )
            )
            item.setData(Qt.UserRole, strategy.strategy_id)
            self.strategy_list.addItem(item)

    def _refresh_nodes(self) -> None:
        self.node_list.clear()
        for node in self.pool.nodes.values():
            if node.node_type == "WEB_AI_PLAYWRIGHT":
                display_model = web_model_settings(node)[0]
            elif node.node_type == "AGENT_CLI":
                display_model = node.model or node.cli_preset or node.cli_command
            else:
                display_model = node.model
            icon = "🤖" if node.node_type == "AGENT_CLI" else "🧠"
            model = f"  ·  {icon} {display_model}" if display_model else ""
            lock = "  ·  🔒 内置" if node.builtin else ""
            ready = self._node_files_ready(node)
            state = ""
            if ready is True:
                state = "  ·  ✅ 模型就绪"
            elif ready is False:
                state = "  ·  ⚠️ 模型文件缺失"
            item = QListWidgetItem(
                f"{node.name}  ·  {NODE_TYPE_LABELS.get(node.node_type, node.node_type)}"
                f"{model}{lock}{state}  ·  {node.capacity_label()}  ·  {node.window_label()}"
                f"  ·  {node.poll_label()}"
            )
            item.setData(Qt.UserRole, node.node_id)
            web_detail = (
                f"\n网页预设: {node.web_preset or '—'}"
                f"\nWeb URL: {node.web_url or '—'}"
                f"\n网页模型: {node.web_model or '—'}"
                f"\nThinking: {node.thinking_level or '—'}"
                f"\n发送方式: {node.send_mode}"
                f"\nCDP 端口: {node.web_cdp_port}"
                if node.node_type == "WEB_AI_PLAYWRIGHT"
                else ""
            )
            cli_detail = (
                f"\nCLI 预设: {node.cli_preset or '—'}"
                f"\n命令路径: {node.cli_command or '—'}"
                f"\n沙箱模式: {node.cli_sandbox or 'read-only'}"
                f"\n超时(秒): {node.cli_timeout_sec}"
                if node.node_type == "AGENT_CLI"
                else ""
            )
            item.setToolTip(
                f"节点 ID: {node.node_id}\n"
                f"模型: {display_model or '—'}\n"
                f"Base URL: {node.base_url or '—'}\n"
                f"模型文件: {node.model_path or '—'}\n"
                f"分词器: {node.tokenizer_path or '—'}\n"
                f"文件状态: {'就绪' if ready else '缺失'}"
                f"{web_detail}{cli_detail}"
            )
            self.node_list.addItem(item)
        self._sync_node_actions()

    def _node_files_ready(self, node: EndpointNode) -> bool | None:
        if node.node_type != "LOCAL_ONNX" or not node.model_path:
            return None
        base = Path(self.workspace)
        def resolve(value: str) -> Path:
            path = Path(value)
            if path.is_absolute():
                return path
            if path.parts and path.parts[0].lower() == "workspace":
                path = Path(*path.parts[1:])
            return base / path
        return resolve(node.model_path).is_file() and (
            not node.tokenizer_path or resolve(node.tokenizer_path).is_file()
        )

    def _sync_node_actions(self) -> None:
        item = self.node_list.currentItem()
        node = self.pool.nodes.get(str(item.data(Qt.UserRole))) if item else None
        protected = bool(node and (node.builtin or not node.deletable))
        if hasattr(self, "node_delete_button"):
            self.node_delete_button.setEnabled(not protected)
            self.node_delete_button.setToolTip(
                "程序自带模型节点不可删除" if protected else ""
            )

    def _refresh_preview(self) -> None:
        now = datetime.now()
        text = " · ".join(
            f"{ROLE_LABELS[role]}: {self.pool.preview(role)}"
            for role in ("fast_llm", "slow_llm", "audit_llm", "vlm")
        )
        self.preview_label.setText(
            f"🟢 当前时间 {now.strftime('%H:%M')} 生效策略预览：{text}"
        )
        for role, widgets in self.role_rows.items():
            widgets["preview"].setText(self.pool.preview(role))

    # ------------------------------------------------------------------ saves
    def _persist(self, message: str = "") -> None:
        try:
            self.pool.save()
            self.config.save()
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.refresh()
        self.changed.emit()
        if message:
            self.toast.emit(message)

    def _save_role_default(self, role: str) -> None:
        combo: QComboBox = self.role_rows[role]["combo"]
        binding = self.pool.bindings.get(role) or RoleBinding()
        binding.default_strategy_id = str(combo.currentData() or "")
        self.pool.bindings[role] = binding
        self._persist(f"{ROLE_LABELS[role]}默认策略已保存")

    def _edit_rules(self, role: str) -> None:
        strategies = list(self.pool.strategies.values())
        if not strategies:
            self.toast.emit("请先创建至少一套模型策略")
            return
        binding = self.pool.bindings.get(role) or RoleBinding()
        dialog = RoleRulesDialog(
            ROLE_LABELS[role], strategies, binding, parent=self
        )
        if dialog.exec() == QDialog.Accepted:
            self.pool.bindings[role] = dialog.result_binding()
            self._persist(f"{ROLE_LABELS[role]}分时段规则已保存")

    # --------------------------------------------------------------- strategies
    def _add_strategy(self) -> None:
        dialog = StrategyDialog(list(self.pool.nodes.values()), parent=self)
        if dialog.exec() == QDialog.Accepted:
            strategy = dialog.result_strategy()
            self.pool.upsert_strategy(strategy)
            self._persist(f"已创建策略：{strategy.strategy_name}")

    def _edit_strategy(self) -> None:
        item = self.strategy_list.currentItem()
        if item is None:
            return
        strategy = self.pool.strategies.get(str(item.data(Qt.UserRole)))
        if strategy is None:
            return
        dialog = StrategyDialog(
            list(self.pool.nodes.values()), strategy, parent=self
        )
        if dialog.exec() == QDialog.Accepted:
            updated = dialog.result_strategy()
            if updated.strategy_id != strategy.strategy_id:
                self.pool.remove_strategy(strategy.strategy_id)
            self.pool.upsert_strategy(updated)
            self._persist(f"策略已保存：{updated.strategy_name}")

    def _delete_strategy(self) -> None:
        item = self.strategy_list.currentItem()
        if item is None:
            return
        strategy_id = str(item.data(Qt.UserRole))
        if QMessageBox.question(
            self,
            "删除策略",
            "删除后绑定到它的角色会失去默认策略，确定吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            self.pool.remove_strategy(strategy_id)
        except ProtectedEndpointError as exc:
            self.toast.emit(str(exc))
            return
        self._persist("策略已删除")

    # ------------------------------------------------------------------- nodes
    def _add_node(self) -> None:
        dialog = EndpointNodeDialog(parent=self, workspace=self.workspace)
        if dialog.exec() == QDialog.Accepted:
            node = dialog.result_node()
            self.pool.upsert_node(node)
            self._persist(f"已新增节点：{node.name}")

    def _edit_node(self) -> None:
        item = self.node_list.currentItem()
        if item is None:
            return
        node = self.pool.nodes.get(str(item.data(Qt.UserRole)))
        if node is None:
            return
        dialog = EndpointNodeDialog(node, parent=self, workspace=self.workspace)
        if dialog.exec() == QDialog.Accepted:
            updated = dialog.result_node()
            if updated.node_id != node.node_id:
                self.pool.remove_node(node.node_id)
            self.pool.upsert_node(updated)
            self._persist(f"节点已保存：{updated.name}")

    def _delete_node(self) -> None:
        item = self.node_list.currentItem()
        if item is None:
            return
        node_id = str(item.data(Qt.UserRole))
        node = self.pool.nodes.get(node_id)
        if node is not None and (node.builtin or not node.deletable):
            self.toast.emit("程序自带模型节点不可删除；可在角色绑定中切换其他策略。")
            return
        if QMessageBox.question(
            self,
            "删除节点",
            "该节点会从所有策略接力链中移除，确定吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            self.pool.remove_node(node_id)
        except ProtectedEndpointError as exc:
            self.toast.emit(str(exc))
            return
        self._persist("节点已删除")

    def _test_node(self) -> None:
        item = self.node_list.currentItem()
        if item is None:
            return
        node = self.pool.nodes.get(str(item.data(Qt.UserRole)))
        if node is None:
            return
        if node.node_type == "AGENT_CLI":
            from models.agent_cli import test_agent_cli

            try:
                msg = test_agent_cli(node, workspace=self.workspace)
                self.toast.emit(f"{node.name}：{msg}")
            except Exception as exc:
                self.toast.emit(f"{node.name} 检测失败：{exc}")
            return
        if node.node_type not in ("OPENAI_COMPATIBLE_API", "QUEUE_POLLING_API"):
            self.toast.emit("该节点类型没有可直接测试的 HTTP/CLI 接口")
            return
        if not node.base_url:
            self.toast.emit("节点未配置 Base URL")
            return
        try:
            import httpx

            headers = {}
            if node.api_key and not node.api_key.startswith("env:"):
                headers["Authorization"] = f"Bearer {node.api_key}"
            with httpx.Client(timeout=8) as client:
                response = client.get(node.base_url + "/models", headers=headers)
            self.toast.emit(f"{node.name} 连接状态：HTTP {response.status_code}")
        except Exception as exc:
            self.toast.emit(f"{node.name} 连接失败：{exc}")

    def _login_node(self) -> None:
        item = self.node_list.currentItem()
        if item is None:
            return
        node = self.pool.nodes.get(str(item.data(Qt.UserRole)))
        if node is None:
            return
        if node.node_type != "WEB_AI_PLAYWRIGHT":
            self.toast.emit("只有网页版 AI 节点需要登录")
            return
        worker = self._login_worker
        if worker is not None:
            try:
                if worker.isRunning():
                    self.toast.emit("登录窗口已打开，请先完成登录")
                    return
            except RuntimeError:
                # The C++ QThread was already deleted by a previous
                # deleteLater(); drop the stale Python wrapper.
                self._login_worker = None
        self.toast.emit("正在打开常驻浏览器，请在窗口中完成登录并固定模型…")
        worker = _LoginWorker(self.workspace, node, parent=self)
        worker.finished_ok.connect(self._login_ready)
        worker.failed.connect(self._login_failed)
        worker.finished.connect(
            lambda finished_worker=worker: self._login_finished(finished_worker)
        )
        self._login_worker = worker
        worker.start()

    def _login_ready(self, profile: str) -> None:
        message = (
            f"Google/网页登录已完成，浏览器保持常驻：{profile}\n"
            "程序已自动配置节点指定的网页模型与 Thinking 深度。"
            "无需再次点击“启动浏览器”，也没有额外的“通过”步骤；"
            "不要关闭该窗口，后续自动发送会复用同一标签页和当前模型设置。"
        )
        self.toast.emit(message.replace("\n", " "))
        QMessageBox.information(self, "浏览器已启动", message)

    def _login_failed(self, message: str) -> None:
        self.toast.emit(f"浏览器启动失败：{message}")
        QMessageBox.warning(self, "浏览器启动失败", message)

    def _login_finished(self, worker: _LoginWorker) -> None:
        if self._login_worker is worker:
            self._login_worker = None
        worker.deleteLater()
