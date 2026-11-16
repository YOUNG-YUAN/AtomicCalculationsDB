"""Dialogs for the V2.3 endpoint pool, strategy builder and time routing."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QThread, QTime, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ui.controls import (
    StrategyToggleSwitch,
    WheelSafeComboBox as QComboBox,
    WheelSafeSpinBox as QSpinBox,
    WheelSafeDoubleSpinBox as QDoubleSpinBox,
)

from models.endpoint_pool import (
    CAPABILITIES,
    NODE_TYPE_LABELS,
    SEND_MODE_LABELS,
    EndpointNode,
    ModelStrategy,
    RoleBinding,
    TimeSlotRule,
    default_capacity,
    format_clock,
    parse_clock,
)
from models.agent_cli import cli_preset_choices, test_agent_cli
from models.web_ai_bridge import (
    WEB_AI_PRESETS,
    WebAIBridge,
    preset_key_for_url,
    web_preset_choices,
)


class _BrowserLaunchThread(QThread):
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


class _ApiTestThread(QThread):
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, node: EndpointNode, parent=None):
        super().__init__(parent)
        self.node = node

    def run(self) -> None:
        try:
            import httpx

            headers = {}
            if self.node.api_key and not self.node.api_key.startswith("env:"):
                headers["Authorization"] = f"Bearer {self.node.api_key}"
            with httpx.Client(timeout=8) as client:
                response = client.get(
                    self.node.base_url.rstrip("/") + "/models",
                    headers=headers,
                )
            self.finished_ok.emit(f"HTTP {response.status_code}")
        except Exception as exc:
            self.failed.emit(str(exc))


class _CliTestThread(QThread):
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, workspace: str, node: EndpointNode, parent=None):
        super().__init__(parent)
        self.workspace = workspace
        self.node = node

    def run(self) -> None:
        try:
            message = test_agent_cli(self.node, workspace=self.workspace)
            self.finished_ok.emit(message)
        except Exception as exc:
            self.failed.emit(str(exc))


class EndpointNodeDialog(QDialog):
    def __init__(
        self,
        node: EndpointNode | None = None,
        parent: QWidget | None = None,
        *,
        workspace: str = ".",
    ):
        super().__init__(parent)
        self.node = node
        self.workspace = str(workspace or ".")
        self._browser_thread: _BrowserLaunchThread | None = None
        self._api_thread: _ApiTestThread | None = None
        self._cli_thread: _CliTestThread | None = None
        self.setWindowTitle("编辑算力节点" if node else "新增算力节点")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)

        self.form = QFormLayout()
        self.node_id = QLineEdit(node.node_id if node else "")
        self.node_id.setPlaceholderText("例如 web-google-ai-studio")
        self.name = QLineEdit(node.name if node else "")
        self.name.setPlaceholderText("例如 Google AI Studio - Gemini 4 Argon High")
        self.node_type = QComboBox()
        for value, label in NODE_TYPE_LABELS.items():
            self.node_type.addItem(label, value)
        if node:
            index = self.node_type.findData(node.node_type)
            self.node_type.setCurrentIndex(max(0, index))

        self.base_url = QLineEdit(node.base_url if node else "")
        self.base_url.setPlaceholderText("https://api.example.com/v1")
        self.model = QLineEdit(node.model if node else "")
        self.model.setPlaceholderText("例如 deepseek-chat / gemini-2.5-pro")
        self.model_path = QLineEdit(node.model_path if node else "")
        self.model_path.setPlaceholderText("models/xxx.onnx（本地模型）")
        self.tokenizer_path = QLineEdit(node.tokenizer_path if node else "")
        self.tokenizer_path.setPlaceholderText("models/xxx-tokenizer.json")
        self.model_dim = QSpinBox()
        self.model_dim.setRange(0, 8192)
        self.model_dim.setValue(node.model_dim if node else 0)
        self.api_key = QLineEdit(node.api_key if node else "")
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText("sk-... / env:VAR")

        self.web_preset = QComboBox()
        for key, label in web_preset_choices():
            self.web_preset.addItem(label, key)
        initial_preset = (
            node.web_preset if node and node.web_preset in WEB_AI_PRESETS
            else preset_key_for_url(node.web_url) if node
            else "google_ai_studio"
        )
        preset_index = self.web_preset.findData(initial_preset)
        self.web_preset.setCurrentIndex(max(0, preset_index))
        self.web_url = QLineEdit(node.web_url if node else "")
        self.web_url.setPlaceholderText("https://aistudio.google.com/prompts/new_chat")
        self.web_model = QComboBox()
        self.web_model.setEditable(True)
        for model_name in (
            "Gemini 4 Argon",
            "Gemini 3 Pro",
            "Gemini 2.5 Pro",
        ):
            self.web_model.addItem(model_name)
        if node and node.web_model:
            self.web_model.setCurrentText(node.web_model)
        elif not node:
            self.web_model.setCurrentText(
                str(WEB_AI_PRESETS["google_ai_studio"].get("model") or "")
            )
        self.thinking_level = QComboBox()
        for level in ("High", "Medium", "Low"):
            self.thinking_level.addItem(level)
        initial_thinking = (
            node.thinking_level if node and node.thinking_level
            else str(WEB_AI_PRESETS["google_ai_studio"].get("thinking") or "High")
        )
        thinking_index = self.thinking_level.findText(initial_thinking)
        self.thinking_level.setCurrentIndex(max(0, thinking_index))
        self.web_cdp_port = QSpinBox()
        self.web_cdp_port.setRange(1024, 65_535)
        self.web_cdp_port.setValue(node.web_cdp_port if node else 9333)
        self.web_cdp_port.setToolTip(
            "本机 Chrome/Edge 的 CDP 调试端口；同一时间一个端口只对应一个浏览器实例。"
        )
        self.web_poll_interval = QDoubleSpinBox()
        self.web_poll_interval.setRange(0.5, 30.0)
        self.web_poll_interval.setDecimals(1)
        self.web_poll_interval.setSingleStep(0.5)
        self.web_poll_interval.setSuffix(" 秒")
        self.web_poll_interval.setValue(
            node.web_poll_interval_sec if node else 1.5
        )
        self.send_mode = QComboBox()
        for value, label in SEND_MODE_LABELS.items():
            self.send_mode.addItem(label, value)
        send_index = self.send_mode.findData(node.send_mode if node else "ctrl_enter")
        self.send_mode.setCurrentIndex(max(0, send_index))
        self.proxy_url = QLineEdit(node.proxy_url if node else "")
        self.proxy_url.setPlaceholderText("可选，如 http://127.0.0.1:7890；留空自动读取系统代理")
        self.profile_dir = QLineEdit(node.profile_dir if node else "")
        self.submit_url = QLineEdit(node.submit_url if node else "")
        self.submit_url.setPlaceholderText("排队 API 提交地址（可选）")
        self.status_url = QLineEdit(node.status_url if node else "")
        self.status_url.setPlaceholderText("排队 API 状态地址（可选，支持 {job_id}）")
        self.cli_preset = QComboBox()
        for key, label in cli_preset_choices():
            self.cli_preset.addItem(label, key)
        cli_preset_idx = self.cli_preset.findData(
            (node.cli_preset if node and node.cli_preset else "codex")
        )
        self.cli_preset.setCurrentIndex(max(0, cli_preset_idx))
        self.cli_command = QLineEdit(node.cli_command if node else "codex")
        self.cli_command.setPlaceholderText("例如 codex 或完整可执行文件路径")
        self.cli_args = QLineEdit(node.cli_args if node else "")
        self.cli_args.setPlaceholderText("额外参数（custom 预设支持 {prompt_file} {output_file} {model}）")
        self.cli_workdir = QLineEdit(node.cli_workdir if node else "")
        self.cli_workdir.setPlaceholderText("可选；留空使用临时沙箱空目录")
        self.cli_sandbox = QComboBox()
        for mode in ("read-only", "workspace-write", "danger-full-access", "none"):
            self.cli_sandbox.addItem(mode, mode)
        sandbox_idx = self.cli_sandbox.findData(
            node.cli_sandbox if node and node.cli_sandbox else "read-only"
        )
        self.cli_sandbox.setCurrentIndex(max(0, sandbox_idx))
        self.cli_timeout_sec = QSpinBox()
        self.cli_timeout_sec.setRange(10, 3600)
        self.cli_timeout_sec.setSuffix(" 秒")
        self.cli_timeout_sec.setValue(node.cli_timeout_sec if node else 300)

        self._rows: dict[str, QWidget] = {}

        def add_row(key: str, label: str, widget: QWidget) -> None:
            self.form.addRow(label, widget)
            self._rows[key] = widget

        add_row("node_id", "节点 ID", self.node_id)
        add_row("name", "显示名称", self.name)
        add_row("node_type", "节点类型", self.node_type)
        add_row("base_url", "Base URL", self.base_url)
        add_row("model", "模型名称", self.model)
        add_row("model_path", "本地模型文件", self.model_path)
        add_row("tokenizer_path", "本地分词器", self.tokenizer_path)
        add_row("model_dim", "模型维度", self.model_dim)
        add_row("api_key", "API Key", self.api_key)
        add_row("web_preset", "网页平台预设", self.web_preset)
        add_row("web_url", "网页地址 (Web URL)", self.web_url)
        add_row("web_model", "网页模型（自动选择）", self.web_model)
        add_row("thinking_level", "Thinking 深度", self.thinking_level)
        add_row("send_mode", "发送触发方式", self.send_mode)
        add_row("proxy_url", "代理地址（可选）", self.proxy_url)
        add_row("web_cdp_port", "CDP 调试端口", self.web_cdp_port)
        add_row("web_poll_interval", "网页回复轮询", self.web_poll_interval)
        add_row("submit_url", "排队提交 URL", self.submit_url)
        add_row("status_url", "排队状态 URL", self.status_url)
        add_row("cli_preset", "CLI 预设", self.cli_preset)
        add_row("cli_command", "CLI 命令 / 路径", self.cli_command)
        add_row("cli_args", "CLI 额外参数", self.cli_args)
        add_row("cli_sandbox", "沙箱权限 (--sandbox)", self.cli_sandbox)
        add_row("cli_workdir", "工作目录（可选）", self.cli_workdir)
        add_row("cli_timeout_sec", "单次超时", self.cli_timeout_sec)
        layout.addLayout(self.form)

        self._action_widget = QWidget()
        action_row = QHBoxLayout(self._action_widget)
        action_row.setContentsMargins(0, 0, 0, 0)
        self.launch_browser_button = QPushButton(
            "🌐 启动浏览器：登录账号并固定网页右侧模型设置"
        )
        self.launch_browser_button.setObjectName("Primary")
        self.launch_browser_button.clicked.connect(self._launch_browser)
        self.api_test_button = QPushButton("⚡ 测试 API 连通性")
        self.api_test_button.setObjectName("Pill")
        self.api_test_button.clicked.connect(self._test_api)
        self.cli_test_button = QPushButton("🧪 测试 Agent CLI")
        self.cli_test_button.setObjectName("Pill")
        self.cli_test_button.clicked.connect(self._test_cli)
        action_row.addWidget(self.launch_browser_button)
        action_row.addWidget(self.api_test_button)
        action_row.addWidget(self.cli_test_button)
        action_row.addStretch(1)
        self.action_widget = self._action_widget
        layout.addWidget(self._action_widget)

        self.web_help = QLabel(
            "启动后 Chrome/Edge 会以独立 Profile 常驻。登录后程序会自动选择上面的"
            "网页模型与 Thinking 深度；后续发送复用同一个标签页，不会重新加载页面。"
            "也可以手动在右侧 Run settings 中调整，自动发送不会重置。"
        )
        self.web_help.setObjectName("Tiny")
        self.web_help.setWordWrap(True)
        layout.addWidget(self.web_help)

        self.cap_section = QWidget()
        cap_layout = QVBoxLayout(self.cap_section)
        cap_layout.setContentsMargins(0, 0, 0, 0)
        cap_layout.addWidget(QLabel("能力"))
        cap_row = QHBoxLayout()
        self.cap_boxes: dict[str, QCheckBox] = {}
        for capability in CAPABILITIES:
            box = QCheckBox(capability)
            box.setChecked(capability in (node.capabilities if node else ["TEXT_LLM"]))
            cap_row.addWidget(box)
            self.cap_boxes[capability] = box
        cap_row.addStretch(1)
        cap_layout.addLayout(cap_row)
        layout.addWidget(self.cap_section)

        self.capacity_section = QWidget()
        capacity = QFormLayout(self.capacity_section)
        capacity.setContentsMargins(0, 0, 0, 0)
        self.max_input = QSpinBox()
        self.max_input.setRange(256, 10_000_000)
        self.max_input.setSingleStep(1000)
        self.max_input.setValue(node.max_input_tokens if node else 32_000)
        self.max_output = QSpinBox()
        self.max_output.setRange(64, 1_000_000)
        self.max_output.setSingleStep(500)
        self.max_output.setValue(node.max_output_tokens if node else 8_000)
        self.max_items = QSpinBox()
        self.max_items.setRange(1, 500)
        self.max_items.setValue(node.max_items_per_batch if node else 20)
        capacity.addRow("单包最大输入 Token", self.max_input)
        capacity.addRow("单包最大输出 Token", self.max_output)
        capacity.addRow("单包最多片段数", self.max_items)
        layout.addWidget(self.capacity_section)

        self.time_section = QWidget()
        time_row = QHBoxLayout(self.time_section)
        time_row.setContentsMargins(0, 0, 0, 0)
        self.time_enabled = QCheckBox("限制可用时段")
        self.time_enabled.setChecked(
            node.time_restriction_enabled if node else False
        )
        self.start_time = QTimeEdit()
        self.start_time.setDisplayFormat("HH:mm")
        self.start_time.setTime(QTime.fromString(
            node.allowed_start_time if node else "00:00", "HH:mm"
        ))
        self.end_time = QTimeEdit()
        self.end_time.setDisplayFormat("HH:mm")
        self.end_time.setTime(QTime.fromString(
            node.allowed_end_time if node else "00:00", "HH:mm"
        ))
        time_row.addWidget(self.time_enabled)
        time_row.addWidget(QLabel("从"))
        time_row.addWidget(self.start_time)
        time_row.addWidget(QLabel("到"))
        time_row.addWidget(self.end_time)
        time_row.addStretch(1)
        layout.addWidget(self.time_section)

        self.poll_section = QWidget()
        poll = QFormLayout(self.poll_section)
        poll.setContentsMargins(0, 0, 0, 0)
        self.poll_interval = QSpinBox()
        self.poll_interval.setRange(1, 3600)
        self.poll_interval.setValue(node.poll_interval_sec if node else 20)
        self.poll_interval.setSuffix(" 秒")
        self.queue_wait = QSpinBox()
        self.queue_wait.setRange(1, 86_400)
        self.queue_wait.setValue(node.max_queue_wait_sec if node else 120)
        self.queue_wait.setSuffix(" 秒")
        poll.addRow("轮询间隔", self.poll_interval)
        poll.addRow("最长等待 / 转交", self.queue_wait)
        layout.addWidget(self.poll_section)

        self.action_status = QLabel("")
        self.action_status.setObjectName("Tiny")
        self.action_status.setWordWrap(True)
        layout.addWidget(self.action_status)

        hint = QLabel(
            "节点容量会直接用于动态分包；网页版 AI 建议 5,000 输入 / 6 片段，"
            "大模型 API 可用 32,000～100,000 输入。时段支持跨午夜。"
            + (
                "\n🔒 这是程序自带的本地模型节点：模型类型、能力、文件路径和节点 ID 只读，"
                "节点不可删除。"
                if node and node.builtin
                else ""
            )
        )
        hint.setObjectName("Tiny")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.node_type.currentIndexChanged.connect(self._on_type_changed)
        self.web_preset.currentIndexChanged.connect(self._on_preset_changed)
        self._apply_type_visibility()
        if (
            str(self.node_type.currentData()) == "WEB_AI_PLAYWRIGHT"
            and not self.web_url.text().strip()
        ):
            self._on_preset_changed()
        if node and node.builtin:
            for widget in (
                self.node_id, self.node_type, self.base_url, self.model,
                self.model_path, self.tokenizer_path, self.model_dim,
                self.api_key, self.web_preset, self.web_url, self.web_model,
                self.thinking_level, self.send_mode, self.proxy_url,
                self.web_cdp_port, self.web_poll_interval, self.profile_dir,
                self.submit_url, self.status_url,
            ):
                widget.setEnabled(False)
            for box in self.cap_boxes.values():
                box.setEnabled(False)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_type_visibility(self) -> None:
        node_type = str(self.node_type.currentData() or "OPENAI_COMPATIBLE_API")
        visible_fields = {"node_id", "name", "node_type"}
        if node_type in ("OPENAI_COMPATIBLE_API", "QUEUE_POLLING_API"):
            visible_fields.update(("base_url", "model", "api_key", "model_dim"))
        if node_type == "QUEUE_POLLING_API":
            visible_fields.update(("submit_url", "status_url"))
        if node_type == "WEB_AI_PLAYWRIGHT":
            visible_fields.update((
                "web_preset", "web_url", "web_model", "thinking_level",
                "send_mode", "proxy_url", "web_cdp_port",
                "web_poll_interval",
            ))
        if node_type == "AGENT_CLI":
            visible_fields.update((
                "cli_preset", "cli_command", "model", "cli_args",
                "cli_sandbox", "cli_workdir", "cli_timeout_sec", "proxy_url",
            ))
        if node_type == "LOCAL_ONNX":
            visible_fields.update(("model_path", "tokenizer_path", "model_dim"))
        for key, widget in self._rows.items():
            self.form.setRowVisible(widget, key in visible_fields)
        self.launch_browser_button.setVisible(node_type == "WEB_AI_PLAYWRIGHT")
        self.api_test_button.setVisible(
            node_type in ("OPENAI_COMPATIBLE_API", "QUEUE_POLLING_API")
        )
        self.cli_test_button.setVisible(node_type == "AGENT_CLI")
        self._action_widget.setVisible(
            node_type in ("WEB_AI_PLAYWRIGHT", "OPENAI_COMPATIBLE_API", "QUEUE_POLLING_API", "AGENT_CLI")
        )
        self.web_help.setVisible(node_type == "WEB_AI_PLAYWRIGHT")
        self.cap_section.setVisible(node_type != "SEMI_AUTO_CLIPBOARD")
        self.capacity_section.setVisible(node_type != "LOCAL_ONNX")
        self.time_section.setVisible(
            node_type in ("OPENAI_COMPATIBLE_API", "QUEUE_POLLING_API", "WEB_AI_PLAYWRIGHT", "AGENT_CLI")
        )
        self.poll_section.setVisible(node_type == "QUEUE_POLLING_API")
        self.adjustSize()

    def _on_type_changed(self, _index: int = -1) -> None:
        node_type = str(self.node_type.currentData() or "OPENAI_COMPATIBLE_API")
        if node_type in NODE_TYPE_LABELS:
            maximum = default_capacity(node_type)
            self.max_input.setValue(maximum[0])
            self.max_output.setValue(maximum[1])
            self.max_items.setValue(maximum[2])
        if node_type == "WEB_AI_PLAYWRIGHT" and not self.web_url.text().strip():
            if self.web_preset.currentData() == "custom":
                index = self.web_preset.findData("google_ai_studio")
                if index >= 0:
                    self.web_preset.setCurrentIndex(index)
            self._on_preset_changed()
        self._apply_type_visibility()

    def _on_preset_changed(self, _index: int = -1) -> None:
        key = str(self.web_preset.currentData() or "custom")
        preset = WEB_AI_PRESETS.get(key)
        if not preset:
            return
        if key != "custom":
            self.web_url.setText(str(preset.get("url") or ""))
        model_name = str(preset.get("model") or "")
        if model_name:
            self.web_model.setCurrentText(model_name)
        thinking = str(preset.get("thinking") or "")
        if thinking:
            thinking_index = self.thinking_level.findText(thinking)
            if thinking_index >= 0:
                self.thinking_level.setCurrentIndex(thinking_index)
        index = self.send_mode.findData(str(preset.get("send_mode") or "enter"))
        if index >= 0:
            self.send_mode.setCurrentIndex(index)
        if key != "custom" and not self.name.text().strip():
            self.name.setText(str(preset.get("profile_name") or preset.get("label") or ""))

    def _draft_node(self) -> EndpointNode:
        previous = self.node
        node_type = str(self.node_type.currentData())
        is_web = node_type == "WEB_AI_PLAYWRIGHT"
        preset = str(self.web_preset.currentData() or "") if is_web else ""
        preset_changed = bool(
            previous and previous.web_preset and previous.web_preset != preset
        )
        return EndpointNode(
            node_id=self.node_id.text().strip() or "web-node",
            name=self.name.text().strip() or "未命名节点",
            node_type=node_type,
            capabilities=[
                name for name, box in self.cap_boxes.items() if box.isChecked()
            ] or ["TEXT_LLM"],
            base_url=self.base_url.text().strip(),
            api_key=self.api_key.text().strip(),
            model="" if is_web else self.model.text().strip(),
            model_path=self.model_path.text().strip(),
            tokenizer_path=self.tokenizer_path.text().strip(),
            model_dim=self.model_dim.value(),
            builtin=previous.builtin if previous else False,
            deletable=previous.deletable if previous else True,
            role_hint=previous.role_hint if previous else "",
            integrity_sha256=previous.integrity_sha256 if previous else "",
            max_input_tokens=self.max_input.value(),
            max_output_tokens=self.max_output.value(),
            max_items_per_batch=self.max_items.value(),
            time_restriction_enabled=self.time_enabled.isChecked(),
            allowed_start_time=self.start_time.time().toString("HH:mm"),
            allowed_end_time=self.end_time.time().toString("HH:mm"),
            poll_interval_sec=self.poll_interval.value(),
            max_queue_wait_sec=self.queue_wait.value(),
            web_url=self.web_url.text().strip() if is_web else "",
            profile_dir=self.profile_dir.text().strip() if is_web else (
                previous.profile_dir if previous and previous.node_type == "WEB_AI_PLAYWRIGHT" else ""
            ),
            status_url=self.status_url.text().strip() if node_type == "QUEUE_POLLING_API" else "",
            submit_url=self.submit_url.text().strip() if node_type == "QUEUE_POLLING_API" else "",
            web_preset=preset,
            send_mode=str(self.send_mode.currentData() or "enter") if is_web else "enter",
            proxy_url=self.proxy_url.text().strip() if (is_web or node_type == "AGENT_CLI") else "",
            web_model=self.web_model.currentText().strip() if is_web else "",
            thinking_level=str(self.thinking_level.currentText() or "") if is_web else "",
            web_cdp_port=self.web_cdp_port.value() if is_web else 9333,
            web_poll_interval_sec=(
                self.web_poll_interval.value() if is_web else 1.5
            ),
            selectors={} if preset_changed else (
                dict(previous.selectors) if previous else {}
            ),
            cli_preset=str(self.cli_preset.currentData() or "codex") if node_type == "AGENT_CLI" else "",
            cli_command=self.cli_command.text().strip() if node_type == "AGENT_CLI" else "",
            cli_args=self.cli_args.text().strip() if node_type == "AGENT_CLI" else "",
            cli_workdir=self.cli_workdir.text().strip() if node_type == "AGENT_CLI" else "",
            cli_timeout_sec=self.cli_timeout_sec.value() if node_type == "AGENT_CLI" else 300,
            cli_sandbox=str(self.cli_sandbox.currentData() or "read-only") if node_type == "AGENT_CLI" else "read-only",
        )

    def _accept(self) -> None:
        if not self.node_id.text().strip():
            QMessageBox.warning(self, "缺少节点 ID", "请填写节点 ID。")
            return
        if not self.name.text().strip():
            QMessageBox.warning(self, "缺少名称", "请填写显示名称。")
            return
        if not any(box.isChecked() for box in self.cap_boxes.values()):
            QMessageBox.warning(self, "缺少能力", "至少选择一种能力。")
            return
        node_type = str(self.node_type.currentData())
        if node_type == "WEB_AI_PLAYWRIGHT" and not self.web_url.text().strip():
            QMessageBox.warning(self, "缺少网页地址", "网页版 AI 必须填写 Web URL。")
            return
        if node_type == "AGENT_CLI" and not (
            self.cli_command.text().strip()
            or str(self.cli_preset.currentData() or "") == "codex"
        ):
            QMessageBox.warning(self, "缺少 CLI 命令", "请填写 Agent CLI 可执行命令或路径。")
            return
        self.accept()

    def result_node(self) -> EndpointNode:
        return self._draft_node()

    def _launch_browser(self) -> None:
        node = self._draft_node()
        if not node.web_url:
            QMessageBox.warning(self, "缺少网页地址", "请先选择预设或填写 Web URL。")
            return
        if self._browser_thread is not None and self._browser_thread.isRunning():
            return
        self.launch_browser_button.setEnabled(False)
        self.action_status.setText("正在启动常驻浏览器，请稍候…")
        worker = _BrowserLaunchThread(self.workspace, node)
        worker.finished_ok.connect(self._browser_ready)
        worker.failed.connect(self._browser_failed)
        worker.finished.connect(worker.deleteLater)
        self._browser_thread = worker
        worker.start()

    def _browser_ready(self, profile: str) -> None:
        self.launch_browser_button.setEnabled(True)
        self.action_status.setText(f"浏览器已启动并保持常驻：{profile}")
        self._browser_thread = None
        QMessageBox.information(
            self,
            "浏览器已启动",
            "登录与模型配置已完成。\n"
            "程序已自动选择节点指定的网页模型与 Thinking 深度；"
            "请保持窗口开启，后续自动发送会复用该标签页，不会重置模型。",
        )

    def _browser_failed(self, message: str) -> None:
        self.launch_browser_button.setEnabled(True)
        self.action_status.setText(f"浏览器启动失败：{message}")
        self._browser_thread = None
        QMessageBox.warning(self, "浏览器启动失败", message)

    def _test_api(self) -> None:
        node = self._draft_node()
        if not node.base_url:
            QMessageBox.warning(self, "缺少 Base URL", "请先填写 Base URL。")
            return
        if self._api_thread is not None and self._api_thread.isRunning():
            return
        self.api_test_button.setEnabled(False)
        self.action_status.setText("正在测试 API 连通性…")
        worker = _ApiTestThread(node)
        worker.finished_ok.connect(self._api_ready)
        worker.failed.connect(self._api_failed)
        worker.finished.connect(worker.deleteLater)
        self._api_thread = worker
        worker.start()

    def _api_ready(self, result: str) -> None:
        self.api_test_button.setEnabled(True)
        self.action_status.setText(f"API 连通性：{result}")
        self._api_thread = None
        QMessageBox.information(self, "API 连通性", result)

    def _api_failed(self, message: str) -> None:
        self.api_test_button.setEnabled(True)
        self.action_status.setText(f"API 连通性失败：{message}")
        self._api_thread = None
        QMessageBox.warning(self, "API 连通性失败", message)

    def _test_cli(self) -> None:
        node = self._draft_node()
        if self._cli_thread is not None and self._cli_thread.isRunning():
            return
        self.cli_test_button.setEnabled(False)
        self.action_status.setText("正在检测 Agent CLI 可执行文件…")
        worker = _CliTestThread(self.workspace, node)
        worker.finished_ok.connect(self._cli_ready)
        worker.failed.connect(self._cli_failed)
        worker.finished.connect(worker.deleteLater)
        self._cli_thread = worker
        worker.start()

    def _cli_ready(self, result: str) -> None:
        self.cli_test_button.setEnabled(True)
        self.action_status.setText(result)
        self._cli_thread = None
        QMessageBox.information(self, "Agent CLI 检测成功", result)

    def _cli_failed(self, message: str) -> None:
        self.cli_test_button.setEnabled(True)
        self.action_status.setText(f"Agent CLI 检测失败：{message}")
        self._cli_thread = None
        QMessageBox.warning(self, "Agent CLI 检测失败", message)


class StrategyDialog(QDialog):
    def __init__(
        self,
        nodes: list[EndpointNode],
        strategy: ModelStrategy | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.nodes = {node.node_id: node for node in nodes}
        self.strategy = strategy
        self.is_vision_strategy = bool(
            strategy
            and (
                "图像理解" in strategy.strategy_name
                or "vlm" in strategy.strategy_id.lower()
                or "vision" in strategy.strategy_id.lower()
            )
        )
        self.protected_strategy = bool(
            strategy and strategy.strategy_id.startswith("builtin-local-")
        )
        self.setWindowTitle("编辑模型策略" if strategy else "新建模型策略")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        if self.is_vision_strategy:
            self.toggle_switch = StrategyToggleSwitch(
                checked=getattr(strategy, "enabled", True) if strategy else True
            )
            form.addRow("节点开关", self.toggle_switch)
        else:
            self.toggle_switch = None
        self.strategy_id = QLineEdit(strategy.strategy_id if strategy else "")
        self.strategy_name = QLineEdit(strategy.strategy_name if strategy else "")
        form.addRow("策略 ID", self.strategy_id)
        form.addRow("策略名称", self.strategy_name)
        if self.protected_strategy:
            self.strategy_id.setEnabled(False)
        layout.addLayout(form)
        chain_label = QLabel("接力链（从上到下依次尝试；失败后按节点容量重新分包）")
        chain_label.setObjectName("Tiny")
        layout.addWidget(chain_label)
        columns = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("算力池节点"))
        self.available = QListWidget()
        for node in nodes:
            item = QListWidgetItem(f"{node.name} · {node.capacity_label()}")
            item.setData(Qt.UserRole, node.node_id)
            self.available.addItem(item)
        left.addWidget(self.available)
        add_button = QPushButton("添加 →")
        add_button.setObjectName("Pill")
        add_button.clicked.connect(self._add_selected)
        left.addWidget(add_button)
        columns.addLayout(left, 1)
        right = QVBoxLayout()
        right.addWidget(QLabel("接力顺序"))
        self.chain = QListWidget()
        if strategy:
            for node_id in strategy.relay_chain:
                node = self.nodes.get(node_id)
                item = QListWidgetItem(
                    node.name if node else f"{node_id}（已删除）"
                )
                item.setData(Qt.UserRole, node_id)
                self.chain.addItem(item)
        right.addWidget(self.chain)
        controls = QHBoxLayout()
        for label, callback in (
            ("↑", self._up), ("↓", self._down), ("移除", self._remove)
        ):
            button = QPushButton(label)
            button.setObjectName("Ghost")
            button.clicked.connect(callback)
            controls.addWidget(button)
        controls.addStretch(1)
        right.addLayout(controls)
        columns.addLayout(right, 1)
        layout.addLayout(columns)
        self.loop = QCheckBox("全链失败后冷却并重回节点 1")
        self.loop.setChecked(strategy.enable_loop if strategy else True)
        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 86_400)
        self.cooldown.setSuffix(" 秒")
        self.cooldown.setValue(strategy.loop_cooldown_sec if strategy else 60)
        self.max_loops = QSpinBox()
        self.max_loops.setRange(0, 100)
        self.max_loops.setValue(strategy.max_loops if strategy else 2)
        loop_row = QHBoxLayout()
        loop_row.addWidget(self.loop)
        loop_row.addWidget(QLabel("冷却"))
        loop_row.addWidget(self.cooldown)
        loop_row.addWidget(QLabel("最多循环"))
        loop_row.addWidget(self.max_loops)
        loop_row.addStretch(1)
        layout.addLayout(loop_row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_selected(self) -> None:
        item = self.available.currentItem()
        if item is None:
            return
        node_id = str(item.data(Qt.UserRole))
        for index in range(self.chain.count()):
            if self.chain.item(index).data(Qt.UserRole) == node_id:
                return
        new = QListWidgetItem(item.text())
        new.setData(Qt.UserRole, node_id)
        self.chain.addItem(new)

    def _remove(self) -> None:
        row = self.chain.currentRow()
        if row < 0:
            return
        node_id = str(self.chain.item(row).data(Qt.UserRole))
        node = self.nodes.get(node_id)
        if self.protected_strategy and node is not None and node.builtin:
            QMessageBox.information(
                self,
                "内置节点受保护",
                "程序自带的本地模型节点不能从内置策略中移除。",
            )
            return
        self.chain.takeItem(row)

    def _up(self) -> None:
        row = self.chain.currentRow()
        if row > 0:
            item = self.chain.takeItem(row)
            self.chain.insertItem(row - 1, item)
            self.chain.setCurrentRow(row - 1)

    def _down(self) -> None:
        row = self.chain.currentRow()
        if 0 <= row < self.chain.count() - 1:
            item = self.chain.takeItem(row)
            self.chain.insertItem(row + 1, item)
            self.chain.setCurrentRow(row + 1)

    def _accept(self) -> None:
        if not self.strategy_id.text().strip():
            QMessageBox.warning(self, "缺少策略 ID", "请填写策略 ID。")
            return
        if not self.strategy_name.text().strip():
            QMessageBox.warning(self, "缺少策略名称", "请填写策略名称。")
            return
        self.accept()

    def result_strategy(self) -> ModelStrategy:
        enabled = (
            self.toggle_switch.isChecked()
            if self.toggle_switch is not None
            else (getattr(self.strategy, "enabled", True) if self.strategy else True)
        )
        return ModelStrategy(
            strategy_id=self.strategy_id.text().strip(),
            strategy_name=self.strategy_name.text().strip(),
            relay_chain=[
                str(self.chain.item(index).data(Qt.UserRole))
                for index in range(self.chain.count())
            ],
            enable_loop=self.loop.isChecked(),
            loop_cooldown_sec=self.cooldown.value(),
            max_loops=self.max_loops.value(),
            enabled=enabled,
        )


class TimeRuleDialog(QDialog):
    def __init__(
        self,
        strategies: list[ModelStrategy],
        rule: TimeSlotRule | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.rule = rule
        self.setWindowTitle("编辑分时段规则" if rule else "添加分时段规则")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(rule.rule_name if rule else "夜间策略")
        self.start = QTimeEdit()
        self.start.setDisplayFormat("HH:mm")
        self.start.setTime(QTime.fromString(rule.start_time if rule else "18:00", "HH:mm"))
        self.end = QTimeEdit()
        self.end.setDisplayFormat("HH:mm")
        self.end.setTime(QTime.fromString(rule.end_time if rule else "08:00", "HH:mm"))
        self.target = QComboBox()
        for strategy in strategies:
            self.target.addItem(strategy.strategy_name, strategy.strategy_id)
        if rule:
            index = self.target.findData(rule.target_strategy_id)
            if index >= 0:
                self.target.setCurrentIndex(index)
        form.addRow("规则名称", self.name)
        form.addRow("开始时间", self.start)
        form.addRow("结束时间", self.end)
        form.addRow("目标策略", self.target)
        layout.addLayout(form)
        hint = QLabel("时间可以跨午夜，例如 18:00 → 08:00。")
        hint.setObjectName("Tiny")
        layout.addWidget(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_rule(self) -> TimeSlotRule:
        return TimeSlotRule(
            rule_name=self.name.text().strip() or "分时段规则",
            start_time=self.start.time().toString("HH:mm"),
            end_time=self.end.time().toString("HH:mm"),
            target_strategy_id=str(self.target.currentData() or ""),
        )


class RoleRulesDialog(QDialog):
    def __init__(
        self,
        role_label: str,
        strategies: list[ModelStrategy],
        binding: RoleBinding,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.strategies = strategies
        self.binding = RoleBinding(
            default_strategy_id=binding.default_strategy_id,
            time_slot_rules=list(binding.time_slot_rules),
        )
        self.setWindowTitle(f"{role_label} · 分时段策略")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.default_strategy = QComboBox()
        for strategy in strategies:
            self.default_strategy.addItem(strategy.strategy_name, strategy.strategy_id)
        index = self.default_strategy.findData(self.binding.default_strategy_id)
        if index >= 0:
            self.default_strategy.setCurrentIndex(index)
        form.addRow("默认策略", self.default_strategy)
        layout.addLayout(form)
        layout.addWidget(QLabel("分时段规则（从上到下匹配，支持跨午夜）"))
        self.rules = QListWidget()
        layout.addWidget(self.rules)
        actions = QHBoxLayout()
        add = QPushButton("➕ 添加规则")
        add.setObjectName("Pill")
        add.clicked.connect(self._add)
        edit = QPushButton("编辑")
        edit.setObjectName("Ghost")
        edit.clicked.connect(self._edit)
        remove = QPushButton("删除")
        remove.setObjectName("Ghost")
        remove.clicked.connect(self._remove)
        for button in (add, edit, remove):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

    def _refresh(self) -> None:
        self.rules.clear()
        for rule in self.binding.time_slot_rules:
            target = next(
                (item.strategy_name for item in self.strategies
                 if item.strategy_id == rule.target_strategy_id),
                rule.target_strategy_id or "未指定",
            )
            item = QListWidgetItem(
                f"{rule.rule_name} · {format_clock(rule.start_time)}-"
                f"{format_clock(rule.end_time)} → {target}"
            )
            item.setData(Qt.UserRole, rule)
            self.rules.addItem(item)

    def _add(self) -> None:
        dialog = TimeRuleDialog(self.strategies, parent=self)
        if dialog.exec() == QDialog.Accepted:
            self.binding.time_slot_rules.append(dialog.result_rule())
            self._refresh()

    def _edit(self) -> None:
        item = self.rules.currentItem()
        if item is None:
            return
        rule = item.data(Qt.UserRole)
        dialog = TimeRuleDialog(self.strategies, rule, parent=self)
        if dialog.exec() == QDialog.Accepted:
            index = self.binding.time_slot_rules.index(rule)
            self.binding.time_slot_rules[index] = dialog.result_rule()
            self._refresh()

    def _remove(self) -> None:
        item = self.rules.currentItem()
        if item is None:
            return
        self.binding.time_slot_rules.remove(item.data(Qt.UserRole))
        self._refresh()

    def result_binding(self) -> RoleBinding:
        return RoleBinding(
            default_strategy_id=str(self.default_strategy.currentData() or ""),
            time_slot_rules=list(self.binding.time_slot_rules),
        )
