"""Model endpoint pool, relay strategies and time-aware role routing.

The pool is deliberately UI-agnostic and has no neural-network dependency.  It
stores all nodes, strategies and role bindings in ``workspace/config.yaml`` so a
workspace remains portable across machines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

from models.security import load_encrypted_json, save_encrypted_json

NODE_TYPES = (
    "OPENAI_COMPATIBLE_API",
    "QUEUE_POLLING_API",
    "WEB_AI_PLAYWRIGHT",
    "AGENT_CLI",
    "LOCAL_ONNX",
)
CAPABILITIES = ("TEXT_LLM", "VISION_VLM", "EMBEDDING", "RERANK")
SEND_MODES = ("ctrl_enter", "enter", "click")
SEND_MODE_LABELS = {
    "ctrl_enter": "Ctrl + Enter (Google AI Studio 模式)",
    "enter": "Enter 回车发送",
    "click": "点击发送按钮",
}
ROLE_KEYS = ("fast_llm", "slow_llm", "audit_llm", "vlm", "embedding", "reranker")
ROLE_LABELS = {
    "fast_llm": "AI 快速提取",
    "slow_llm": "AI 深度归纳",
    "audit_llm": "AI 校对核查",
    "vlm": "AI 图像理解",
    "embedding": "向量模型",
    "reranker": "重排模型",
}
ROLE_CAPABILITY = {
    "fast_llm": "TEXT_LLM",
    "slow_llm": "TEXT_LLM",
    "audit_llm": "TEXT_LLM",
    "vlm": "VISION_VLM",
    "embedding": "EMBEDDING",
    "reranker": "RERANK",
}
class ProtectedEndpointError(RuntimeError):
    """Raised when trying to delete a program-provided endpoint/strategy."""


NODE_TYPE_LABELS = {
    "OPENAI_COMPATIBLE_API": "OpenAI 兼容 API",
    "QUEUE_POLLING_API": "排队轮询 API",
    "WEB_AI_PLAYWRIGHT": "网页版 AI（Playwright）",
    "AGENT_CLI": "本地 Agent CLI (Codex / 自定义)",
    "LOCAL_ONNX": "本地 ONNX",
}


def parse_clock(value: Any, default: dt_time | None = None) -> dt_time:
    """Parse ``HH:MM`` or ``H:MM``; invalid values use the supplied default."""
    text = str(value or "").strip()
    try:
        hour, minute = text.split(":", 1)
        result = dt_time(int(hour), int(minute))
        return result
    except (TypeError, ValueError):
        return default or dt_time(0, 0)


def format_clock(value: Any) -> str:
    clock = parse_clock(value)
    return f"{clock.hour:02d}:{clock.minute:02d}"


def window_active(
    enabled: bool,
    start: Any,
    end: Any,
    now: dt_time | None = None,
) -> bool:
    if not enabled:
        return True
    current = now or datetime.now().time()
    left = parse_clock(start)
    right = parse_clock(end)
    if left == right:
        return True  # 00:00-00:00 means the whole day.
    if left < right:
        return left <= current < right
    return current >= left or current < right


def _int(value: Any, default: int, minimum: int = 0, maximum: int = 10_000_000) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def default_capacity(node_type: str) -> tuple[int, int, int]:
    if node_type == "WEB_AI_PLAYWRIGHT":
        return 5_000, 4_000, 6
    if node_type == "AGENT_CLI":
        return 20_000, 6_000, 8
    if node_type == "SEMI_AUTO_CLIPBOARD":
        return 5_000, 4_000, 6
    if node_type == "LOCAL_ONNX":
        return 8_192, 2_000, 10
    return 32_000, 8_000, 20


@dataclass
class EndpointNode:
    node_id: str
    name: str
    node_type: str = "OPENAI_COMPATIBLE_API"
    capabilities: list[str] = field(default_factory=list)
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    max_input_tokens: int = 32_000
    max_output_tokens: int = 8_000
    max_items_per_batch: int = 20
    time_restriction_enabled: bool = False
    allowed_start_time: str = "00:00"
    allowed_end_time: str = "00:00"
    poll_interval_sec: int = 20
    max_queue_wait_sec: int = 120
    web_url: str = ""
    profile_dir: str = ""
    status_url: str = ""
    submit_url: str = ""
    web_preset: str = ""
    send_mode: str = "enter"
    proxy_url: str = ""
    web_model: str = ""
    thinking_level: str = ""
    web_cdp_port: int = 9333
    web_poll_interval_sec: float = 1.5
    selectors: dict[str, str] = field(default_factory=dict)
    builtin: bool = False
    deletable: bool = True
    role_hint: str = ""
    model_path: str = ""
    tokenizer_path: str = ""
    model_dim: int = 0
    integrity_sha256: str = ""
    cli_preset: str = ""
    cli_command: str = ""
    cli_args: str = ""
    cli_workdir: str = ""
    cli_timeout_sec: int = 300
    cli_sandbox: str = "read-only"

    def __post_init__(self) -> None:
        self.node_id = str(self.node_id or "node").strip()
        self.node_type = str(self.node_type or "OPENAI_COMPATIBLE_API").upper()
        if self.node_type not in NODE_TYPES:
            self.node_type = "OPENAI_COMPATIBLE_API"
        if not self.capabilities:
            self.capabilities = ["TEXT_LLM"]
        self.capabilities = [
            str(item).upper() for item in self.capabilities if str(item).upper() in CAPABILITIES
        ] or ["TEXT_LLM"]
        defaults = default_capacity(self.node_type)
        self.max_input_tokens = _int(self.max_input_tokens, defaults[0], 256)
        self.max_output_tokens = _int(self.max_output_tokens, defaults[1], 64)
        self.max_items_per_batch = _int(self.max_items_per_batch, defaults[2], 1, 500)
        self.poll_interval_sec = _int(self.poll_interval_sec, 20, 1, 3600)
        self.max_queue_wait_sec = _int(self.max_queue_wait_sec, 120, 1, 86_400)
        self.web_preset = str(self.web_preset or "").strip()
        self.send_mode = str(self.send_mode or "enter").strip().lower()
        if self.send_mode not in SEND_MODES:
            self.send_mode = "enter"
        self.proxy_url = str(self.proxy_url or "").strip()
        self.web_model = str(self.web_model or "").strip()
        # A web-AI node has its own model field; never let an old API model
        # name (e.g. deepseek-flash) leak into the web node display/routing.
        if self.node_type == "WEB_AI_PLAYWRIGHT":
            self.model = ""
        self.thinking_level = str(self.thinking_level or "").strip()
        self.web_cdp_port = _int(self.web_cdp_port, 9333, 1024, 65_535)
        try:
            self.web_poll_interval_sec = max(
                0.5, min(30.0, float(self.web_poll_interval_sec))
            )
        except (TypeError, ValueError):
            self.web_poll_interval_sec = 1.5
        self.allowed_start_time = format_clock(self.allowed_start_time)
        self.allowed_end_time = format_clock(self.allowed_end_time)
        self.selectors = {
            str(key): str(value)
            for key, value in dict(self.selectors or {}).items()
            if str(key).strip()
        }
        self.builtin = bool(self.builtin)
        self.deletable = bool(self.deletable) and not self.builtin
        self.role_hint = str(self.role_hint or "")
        self.model_path = str(self.model_path or "")
        self.tokenizer_path = str(self.tokenizer_path or "")
        self.model_dim = _int(self.model_dim, 0, 0, 1_000_000)
        self.integrity_sha256 = str(self.integrity_sha256 or "")
        self.cli_preset = str(self.cli_preset or "").strip().lower()
        self.cli_command = str(self.cli_command or "").strip()
        self.cli_args = str(self.cli_args or "").strip()
        self.cli_workdir = str(self.cli_workdir or "").strip()
        self.cli_timeout_sec = _int(self.cli_timeout_sec, 300, 10, 3600)
        sandbox = str(self.cli_sandbox or "read-only").strip().lower()
        self.cli_sandbox = (
            sandbox
            if sandbox in ("read-only", "workspace-write", "danger-full-access", "none")
            else "read-only"
        )

    def supports(self, role: str) -> bool:
        return ROLE_CAPABILITY.get(role, str(role).upper()) in self.capabilities

    def active_at(self, now: dt_time | None = None) -> bool:
        return window_active(
            self.time_restriction_enabled,
            self.allowed_start_time,
            self.allowed_end_time,
            now,
        )

    def capacity_label(self) -> str:
        return (
            f"📦 {self.max_input_tokens // 1000}k Tokens / "
            f"{self.max_items_per_batch}片段"
        )

    def window_label(self) -> str:
        if not self.time_restriction_enabled:
            return "🕒 全天"
        return f"🌙 {self.allowed_start_time}-{self.allowed_end_time}"

    def poll_label(self) -> str:
        return f"⏱️ {self.poll_interval_sec}s 轮询 / {self.max_queue_wait_sec}s 转交"

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "node_type": self.node_type,
            "capabilities": list(self.capabilities),
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_items_per_batch": self.max_items_per_batch,
            "time_restriction_enabled": self.time_restriction_enabled,
            "allowed_start_time": self.allowed_start_time,
            "allowed_end_time": self.allowed_end_time,
            "poll_interval_sec": self.poll_interval_sec,
            "max_queue_wait_sec": self.max_queue_wait_sec,
            "web_url": self.web_url,
            "profile_dir": self.profile_dir,
            "status_url": self.status_url,
            "submit_url": self.submit_url,
            "web_preset": self.web_preset,
            "send_mode": self.send_mode,
            "proxy_url": self.proxy_url,
            "web_model": self.web_model,
            "thinking_level": self.thinking_level,
            "web_cdp_port": self.web_cdp_port,
            "web_poll_interval_sec": self.web_poll_interval_sec,
            "selectors": dict(self.selectors),
            "builtin": self.builtin,
            "deletable": self.deletable,
            "role_hint": self.role_hint,
            "model_path": self.model_path,
            "tokenizer_path": self.tokenizer_path,
            "model_dim": self.model_dim,
            "integrity_sha256": self.integrity_sha256,
            "cli_preset": self.cli_preset,
            "cli_command": self.cli_command,
            "cli_args": self.cli_args,
            "cli_workdir": self.cli_workdir,
            "cli_timeout_sec": self.cli_timeout_sec,
            "cli_sandbox": self.cli_sandbox,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EndpointNode":
        return cls(
            node_id=str(value.get("node_id") or "node"),
            name=str(value.get("name") or value.get("node_id") or "未命名节点"),
            node_type=str(value.get("node_type") or "OPENAI_COMPATIBLE_API"),
            capabilities=list(value.get("capabilities") or []),
            base_url=str(value.get("base_url") or ""),
            api_key=str(value.get("api_key") or ""),
            model=str(value.get("model") or ""),
            max_input_tokens=value.get("max_input_tokens", 32_000),
            max_output_tokens=value.get("max_output_tokens", 8_000),
            max_items_per_batch=value.get("max_items_per_batch", 20),
            time_restriction_enabled=bool(value.get("time_restriction_enabled", False)),
            allowed_start_time=str(value.get("allowed_start_time") or "00:00"),
            allowed_end_time=str(value.get("allowed_end_time") or "00:00"),
            poll_interval_sec=value.get("poll_interval_sec", 20),
            max_queue_wait_sec=value.get("max_queue_wait_sec", 120),
            web_url=str(value.get("web_url") or ""),
            profile_dir=str(value.get("profile_dir") or ""),
            status_url=str(value.get("status_url") or ""),
            submit_url=str(value.get("submit_url") or ""),
            web_preset=str(value.get("web_preset") or ""),
            send_mode=str(value.get("send_mode") or "enter"),
            proxy_url=str(value.get("proxy_url") or ""),
            web_model=str(value.get("web_model") or ""),
            thinking_level=str(value.get("thinking_level") or ""),
            web_cdp_port=value.get("web_cdp_port", 9333),
            web_poll_interval_sec=value.get("web_poll_interval_sec", 1.5),
            selectors=dict(value.get("selectors") or {}),
            builtin=bool(value.get("builtin", False)),
            deletable=bool(value.get("deletable", True)),
            role_hint=str(value.get("role_hint") or ""),
            model_path=str(value.get("model_path") or ""),
            tokenizer_path=str(value.get("tokenizer_path") or ""),
            model_dim=value.get("model_dim", 0),
            integrity_sha256=str(value.get("integrity_sha256") or ""),
            cli_preset=str(value.get("cli_preset") or ""),
            cli_command=str(value.get("cli_command") or ""),
            cli_args=str(value.get("cli_args") or ""),
            cli_workdir=str(value.get("cli_workdir") or ""),
            cli_timeout_sec=value.get("cli_timeout_sec", 300),
            cli_sandbox=str(value.get("cli_sandbox") or "read-only"),
        )


@dataclass
class TimeSlotRule:
    rule_name: str
    start_time: str
    end_time: str
    target_strategy_id: str

    def active_at(self, now: dt_time | None = None) -> bool:
        return window_active(True, self.start_time, self.end_time, now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "start_time": format_clock(self.start_time),
            "end_time": format_clock(self.end_time),
            "target_strategy_id": self.target_strategy_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TimeSlotRule":
        return cls(
            rule_name=str(value.get("rule_name") or "分时段规则"),
            start_time=str(value.get("start_time") or "00:00"),
            end_time=str(value.get("end_time") or "00:00"),
            target_strategy_id=str(value.get("target_strategy_id") or ""),
        )


@dataclass
class ModelStrategy:
    strategy_id: str
    strategy_name: str
    relay_chain: list[str] = field(default_factory=list)
    enable_loop: bool = True
    loop_cooldown_sec: int = 60
    max_loops: int = 2
    enabled: bool = True

    def __post_init__(self) -> None:
        self.strategy_id = str(self.strategy_id or "strategy").strip()
        self.strategy_name = str(self.strategy_name or self.strategy_id)
        self.relay_chain = [str(item) for item in self.relay_chain if str(item).strip()]
        self.loop_cooldown_sec = _int(self.loop_cooldown_sec, 60, 0, 86_400)
        self.max_loops = _int(self.max_loops, 2, 0, 100)
        self.enabled = bool(self.enabled)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "relay_chain": list(self.relay_chain),
            "enable_loop": self.enable_loop,
            "loop_cooldown_sec": self.loop_cooldown_sec,
            "max_loops": self.max_loops,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ModelStrategy":
        return cls(
            strategy_id=str(value.get("strategy_id") or "strategy"),
            strategy_name=str(value.get("strategy_name") or value.get("strategy_id") or "策略"),
            relay_chain=list(value.get("relay_chain") or []),
            enable_loop=bool(value.get("enable_loop", True)),
            loop_cooldown_sec=value.get("loop_cooldown_sec", 60),
            max_loops=value.get("max_loops", 2),
            enabled=bool(value.get("enabled", True)),
        )


@dataclass
class RoleBinding:
    default_strategy_id: str = ""
    time_slot_rules: list[TimeSlotRule] = field(default_factory=list)

    def resolve_strategy_id(self, now: dt_time | None = None) -> str:
        for rule in self.time_slot_rules:
            if rule.active_at(now) and rule.target_strategy_id:
                return rule.target_strategy_id
        return self.default_strategy_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "default_strategy_id": self.default_strategy_id,
            "time_slot_rules": [rule.as_dict() for rule in self.time_slot_rules],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RoleBinding":
        return cls(
            default_strategy_id=str(value.get("default_strategy_id") or ""),
            time_slot_rules=[
                TimeSlotRule.from_dict(item)
                for item in value.get("time_slot_rules") or []
                if isinstance(item, Mapping)
            ],
        )


class EndpointPool:
    """Persistent endpoint/strategy/role configuration."""

    CONFIG_KEY = "endpoint_pool"

    def __init__(self, config: Any, workspace: Path | None = None):
        self.config = config
        self.workspace = self._resolve_workspace(workspace)
        self.nodes: dict[str, EndpointNode] = {}
        self.strategies: dict[str, ModelStrategy] = {}
        self.bindings: dict[str, RoleBinding] = {}
        self.load()

    def _resolve_workspace(self, workspace: Path | None = None) -> Path | None:
        if workspace is not None:
            return Path(workspace).resolve()
        if hasattr(self.config, "path") and self.config.path:
            return Path(self.config.path).resolve().parent
        if hasattr(self.config, "workspace") and self.config.workspace:
            return Path(self.config.workspace).resolve()
        return None

    @property
    def enc_path(self) -> Path | None:
        if self.workspace:
            return self.workspace / "model_pool.enc"
        return None

    # ------------------------------------------------------------- persistence
    def load(self) -> None:
        raw: dict[str, Any] = {}
        if self.enc_path and self.enc_path.is_file():
            try:
                raw = load_encrypted_json(self.enc_path)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Failed to decrypt %s: %s; falling back to config",
                    self.enc_path, exc,
                )
                raw = {}
        if not raw:
            raw = self._raw()
            if raw and self.enc_path and not self.enc_path.is_file():
                try:
                    save_encrypted_json(self.enc_path, raw)
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "Failed to save initial encrypted %s: %s", self.enc_path, exc,
                    )
        for item in raw.get("nodes") or []:
            if isinstance(item, Mapping):
                node = EndpointNode.from_dict(item)
                self.nodes[node.node_id] = node
        for item in raw.get("strategies") or []:
            if isinstance(item, Mapping):
                strategy = ModelStrategy.from_dict(item)
                self.strategies[strategy.strategy_id] = strategy
        for role in ROLE_KEYS:
            value = (raw.get("role_bindings") or {}).get(role)
            if isinstance(value, Mapping):
                self.bindings[role] = RoleBinding.from_dict(value)
        if not self.nodes:
            self._seed_from_legacy()

        # Migrate/standardize naming to V3.0 specs
        for strategy in self.strategies.values():
            if strategy.strategy_name == "快速提取 API策略":
                strategy.strategy_name = "AI 快速提取策略"
            elif strategy.strategy_name == "深度归纳 API策略":
                strategy.strategy_name = "AI 深度归纳策略"
            elif strategy.strategy_name == "图像理解 API策略":
                strategy.strategy_name = "AI 图像理解策略"

        for node in self.nodes.values():
            if node.name == "快速提取 API":
                node.name = "AI 快速提取"
            elif node.name == "深度归纳 API":
                node.name = "AI 深度归纳"
            elif node.name == "图像理解 API":
                node.name = "AI 图像理解"

        # Ensure AI 校对核查策略 exists
        has_audit_strat = any("校对核查" in s.strategy_name for s in self.strategies.values())
        if not has_audit_strat:
            fallback_chain = []
            fast_binding = self.bindings.get("fast_llm")
            if fast_binding and fast_binding.default_strategy_id in self.strategies:
                fallback_chain = list(self.strategies[fast_binding.default_strategy_id].relay_chain)
            elif self.nodes:
                fallback_chain = [next(iter(self.nodes.keys()))]
            audit_strat = ModelStrategy(
                strategy_id="legacy-audit-strategy",
                strategy_name="AI 校对核查策略",
                relay_chain=fallback_chain,
                enable_loop=False,
                max_loops=0,
            )
            self.strategies["legacy-audit-strategy"] = audit_strat
            if "audit_llm" in self.bindings and not self.bindings["audit_llm"].default_strategy_id:
                self.bindings["audit_llm"].default_strategy_id = "legacy-audit-strategy"

        for role in ROLE_KEYS:
            self.bindings.setdefault(role, RoleBinding())
        if "audit_llm" in self.bindings and not self.bindings["audit_llm"].default_strategy_id:
            for sid, s in self.strategies.items():
                if "校对核查" in s.strategy_name:
                    self.bindings["audit_llm"].default_strategy_id = sid
                    break
        self._ensure_builtin_nodes()
        self._ensure_bindings()

    def _raw(self) -> dict[str, Any]:
        if self.config is None:
            return {}
        getter = getattr(self.config, "get", None)
        if callable(getter):
            value = getter(self.CONFIG_KEY, {})
            return dict(value) if isinstance(value, Mapping) else {}
        if isinstance(self.config, Mapping):
            value = self.config.get(self.CONFIG_KEY, {})
            return dict(value) if isinstance(value, Mapping) else {}
        return {}

    def _seed_from_legacy(self) -> None:
        """One-time migration of the removed single-node gateway config.

        Only roles with real legacy connection values become pool nodes.  We do
        not create empty placeholder strategies for unconfigured roles.
        """
        specs = (
            ("legacy-fast", "AI 快速提取", "fast_llm", "TEXT_LLM"),
            ("legacy-slow", "AI 深度归纳", "slow_llm", "TEXT_LLM"),
            ("legacy-audit", "AI 校对核查", "audit_llm", "TEXT_LLM"),
            ("legacy-vlm", "AI 图像理解", "vlm", "VISION_VLM"),
        )
        for node_id, default_name, prefix, capability in specs:
            base = self._get(f"models.{prefix}.base_url", "")
            model = self._get(f"models.{prefix}.model", "")
            key = self._get(f"models.{prefix}.api_key", "")
            if not (base or model):
                continue
            self.nodes[node_id] = EndpointNode(
                node_id=node_id,
                name=default_name,
                node_type="OPENAI_COMPATIBLE_API",
                capabilities=[capability],
                base_url=str(base or ""),
                api_key=str(key or ""),
                model=str(model or ""),
            )
            strategy = ModelStrategy(
                strategy_id=f"legacy-{prefix}-strategy",
                strategy_name=f"{default_name}策略",
                relay_chain=[node_id],
                enable_loop=False,
                max_loops=0,
            )
            self.strategies[strategy.strategy_id] = strategy
            self.bindings[prefix] = RoleBinding(
                default_strategy_id=strategy.strategy_id
            )

    def _ensure_builtin_nodes(self) -> None:
        """Idempotently register program-provided local ONNX engines."""
        specs = (
            {
                "node_id": "builtin-local-bge-m3",
                "name": "本地 BGE-M3 向量模型",
                "capabilities": ["EMBEDDING"],
                "role_hint": "embedding",
                "model": "BAAI/bge-m3",
                "model_path": "models/bge-m3/model.onnx",
                "tokenizer_path": "models/bge-m3/tokenizer.json",
                "model_dim": 1024,
            },
            {
                "node_id": "builtin-local-reranker",
                "name": "本地语义精排模型",
                "capabilities": ["RERANK"],
                "role_hint": "reranker",
                "model": "ms-marco-MiniLM-L-6-v2",
                "model_path": "models/ms-marco-MiniLM-L-6-v2-int8.onnx",
                "tokenizer_path": "models/ms-marco-MiniLM-L-6-v2-tokenizer.json",
                "model_dim": 0,
            },
            {
                "node_id": "builtin-local-minilm",
                "name": "本地 MiniLM 语义引擎",
                "capabilities": ["EMBEDDING"],
                "role_hint": "embedding_aux",
                "model": "sentence-transformers/all-MiniLM-L6-v2",
                "model_path": "models/all-MiniLM-L6-v2-int8.onnx",
                "tokenizer_path": "models/all-MiniLM-L6-v2-tokenizer.json",
                "model_dim": 384,
            },
            {
                "node_id": "builtin-local-nli",
                "name": "本地 DeBERTa NLI 冲突引擎",
                "capabilities": ["RERANK"],
                "role_hint": "nli_aux",
                "model": "cross-encoder/nli-deberta-v3-xsmall",
                "model_path": "models/nli-deberta-v3-xsmall-int8.onnx",
                "tokenizer_path": "models/nli-deberta-v3-xsmall-tokenizer.json",
                "model_dim": 0,
            },
        )
        for spec in specs:
            node_id = str(spec["node_id"])
            existing = self.nodes.get(node_id)
            if existing is None:
                self.nodes[node_id] = EndpointNode(
                    node_id=node_id,
                    name=str(spec["name"]),
                    node_type="LOCAL_ONNX",
                    capabilities=list(spec["capabilities"]),
                    model=str(spec["model"]),
                    model_path=str(spec["model_path"]),
                    tokenizer_path=str(spec["tokenizer_path"]),
                    model_dim=int(spec["model_dim"]),
                    builtin=True,
                    deletable=False,
                    role_hint=str(spec["role_hint"]),
                )
            else:
                existing.builtin = True
                existing.deletable = False
                existing.node_type = "LOCAL_ONNX"
                existing.capabilities = list(spec["capabilities"])
                existing.model = str(spec["model"])
                existing.model_path = str(spec["model_path"])
                existing.tokenizer_path = str(spec["tokenizer_path"])
                existing.model_dim = int(spec["model_dim"])
                existing.role_hint = str(spec["role_hint"])
        strategies = (
            (
                "builtin-local-embedding-strategy",
                "向量模型策略（本地 BGE-M3）",
                "builtin-local-bge-m3",
            ),
            (
                "builtin-local-reranker-strategy",
                "重排模型策略（本地语义精排）",
                "builtin-local-reranker",
            ),
            (
                "builtin-local-minilm-strategy",
                "本地 MiniLM 语义策略",
                "builtin-local-minilm",
            ),
            (
                "builtin-local-nli-strategy",
                "本地 NLI 冲突策略",
                "builtin-local-nli",
            ),
        )
        for strategy_id, strategy_name, node_id in strategies:
            strategy = self.strategies.get(strategy_id)
            if strategy is None:
                self.strategies[strategy_id] = ModelStrategy(
                    strategy_id=strategy_id,
                    strategy_name=strategy_name,
                    relay_chain=[node_id],
                    enable_loop=False,
                    max_loops=0,
                )
            else:
                strategy.relay_chain = [node_id]
                if not strategy.strategy_name:
                    strategy.strategy_name = strategy_name
        for role, builtin_id, node_id in (
            ("embedding", "builtin-local-embedding-strategy", "builtin-local-bge-m3"),
            ("reranker", "builtin-local-reranker-strategy", "builtin-local-reranker"),
        ):
            binding = self.bindings.setdefault(role, RoleBinding())
            current = self.strategies.get(binding.default_strategy_id)
            if current is not None and current.relay_chain and any(
                item in self.nodes for item in current.relay_chain
            ):
                continue
            if current is not None:
                current.relay_chain = [node_id]
                current.strategy_name = (
                    "向量模型策略（本地 BGE-M3）"
                    if role == "embedding"
                    else "重排模型策略（本地语义精排）"
                )
                binding.default_strategy_id = current.strategy_id
            else:
                binding.default_strategy_id = builtin_id
        referenced = {binding.default_strategy_id for binding in self.bindings.values()}
        for binding in self.bindings.values():
            referenced.update(
                rule.target_strategy_id for rule in binding.time_slot_rules
            )
        for strategy_id in (
            "builtin-local-embedding-strategy",
            "builtin-local-reranker-strategy",
        ):
            if strategy_id not in referenced:
                self.strategies.pop(strategy_id, None)

    def _get(self, dotted: str, default: Any = None) -> Any:
        if self.config is None:
            return default
        getter = getattr(self.config, "get", None)
        if callable(getter):
            return getter(dotted, default)
        if isinstance(self.config, Mapping):
            node: Any = self.config
            for part in dotted.split("."):
                if not isinstance(node, Mapping) or part not in node:
                    return default
                node = node[part]
            return node
        return default

    def _ensure_bindings(self) -> None:
        for role in ROLE_KEYS:
            self.bindings.setdefault(role, RoleBinding())

    def as_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.as_dict() for node in self.nodes.values()],
            "strategies": [strategy.as_dict() for strategy in self.strategies.values()],
            "role_bindings": {
                role: binding.as_dict() for role, binding in self.bindings.items()
            },
        }

    def save(self) -> None:
        data = self.as_dict()
        if self.enc_path:
            try:
                save_encrypted_json(self.enc_path, data)
            except Exception as exc:
                logging.getLogger(__name__).error(
                    "Failed to save encrypted %s: %s", self.enc_path, exc,
                )
        if self.config is not None:
            setter = getattr(self.config, "set", None)
            if callable(setter):
                if self.enc_path:
                    setter(self.CONFIG_KEY, {"storage": "model_pool.enc", "encrypted": True})
                else:
                    setter(self.CONFIG_KEY, data)
            elif isinstance(self.config, dict):
                if self.enc_path:
                    self.config[self.CONFIG_KEY] = {"storage": "model_pool.enc", "encrypted": True}
                else:
                    self.config[self.CONFIG_KEY] = data

    # --------------------------------------------------------------- mutations
    def upsert_node(self, node: EndpointNode) -> EndpointNode:
        self.nodes[node.node_id] = node
        return node

    def remove_node(self, node_id: str) -> bool:
        node = self.nodes.get(node_id)
        if node is not None and (node.builtin or not node.deletable):
            raise ProtectedEndpointError(
                f"内置节点 {node.name} 由程序自带，不能删除"
            )
        self.nodes.pop(node_id, None)
        for strategy in self.strategies.values():
            strategy.relay_chain = [
                item for item in strategy.relay_chain if item != node_id
            ]
        return True

    def upsert_strategy(self, strategy: ModelStrategy) -> ModelStrategy:
        self.strategies[strategy.strategy_id] = strategy
        return strategy

    def remove_strategy(self, strategy_id: str) -> bool:
        if strategy_id.startswith("builtin-local-"):
            raise ProtectedEndpointError("程序自带本地模型策略不能删除")
        self.strategies.pop(strategy_id, None)
        for binding in self.bindings.values():
            if binding.default_strategy_id == strategy_id:
                binding.default_strategy_id = ""
            binding.time_slot_rules = [
                rule for rule in binding.time_slot_rules
                if rule.target_strategy_id != strategy_id
            ]
        return True

    # --------------------------------------------------------------- routing
    def strategy_for_role(self, role: str, now: dt_time | None = None) -> ModelStrategy | None:
        binding = self.bindings.get(role)
        if binding is None:
            return None
        strategy_id = binding.resolve_strategy_id(now)
        if strategy_id and strategy_id in self.strategies:
            return self.strategies[strategy_id]
        return None

    def node_for_role(self, role: str, now: dt_time | None = None) -> EndpointNode | None:
        strategy = self.strategy_for_role(role, now)
        if strategy is None:
            return None
        for node_id in strategy.relay_chain:
            node = self.nodes.get(node_id)
            if node is not None and node.supports(role) and node.active_at(now):
                return node
        return None

    def chain_for_role(self, role: str, now: dt_time | None = None) -> list[EndpointNode]:
        strategy = self.strategy_for_role(role, now)
        if strategy is None:
            return []
        return [
            node
            for node_id in strategy.relay_chain
            if (node := self.nodes.get(node_id)) is not None
            and node.supports(role)
            and node.active_at(now)
        ]

    def preview(self, role: str, now: dt_time | None = None) -> str:
        strategy = self.strategy_for_role(role, now)
        node = self.node_for_role(role, now)
        if strategy is None:
            return "未绑定策略"
        if node is None:
            return f"策略「{strategy.strategy_name}」当前无可用节点"
        return f"策略「{strategy.strategy_name}」→ {node.name}"

    def node_summary(self, node_id: str) -> str:
        node = self.nodes.get(node_id)
        return node.name if node else f"{node_id}（已删除）"


__all__ = [
    "NODE_TYPES",
    "CAPABILITIES",
    "ROLE_KEYS",
    "ROLE_LABELS",
    "ROLE_CAPABILITY",
    "NODE_TYPE_LABELS",
    "EndpointNode",
    "ModelStrategy",
    "TimeSlotRule",
    "RoleBinding",
    "EndpointPool",
    "ProtectedEndpointError",
    "parse_clock",
    "format_clock",
    "window_active",
]
