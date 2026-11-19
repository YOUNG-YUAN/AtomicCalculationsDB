"""Application configuration.

Configuration lives in ``workspace/config.yaml`` so that the whole workspace
folder can be copied/migrated as one unit.  Defaults are merged on load, which
keeps old config files forward compatible when new options are introduced.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Mapping

import yaml

DEFAULTS: dict[str, Any] = {
    "app": {
        "name": "AtomicCalculationDB",
        "language": "zh-CN",
        "theme": "material3-light",
        "demo_seed": False,
    },
    "explore": {
        "page_size": 20,
    },
    "semi_auto": {
        "max_batch_tokens": 20000,
        "agent_model": "GPT-6.2 Sol Ultra",
    },
    "parser": {
        "mineru": {
            "enabled": False,
            "command": "mineru-kit",
            "python": "",
            "tier": "standard",
            "ocr_mode": "auto",
            "device": "auto",
            "timeout_sec": 1800,
            "fallback_to_pymupdf": True,
            "keep_artifacts": True,
            "max_output_mb": 2048,
            "min_markdown_chars": 200,
            "concurrent_jobs": 1,
            "language": "en",
            "model_source": "",
        }
    },
    "search": {
        "fts_limit": 50,
        "rerank_limit": 10,
        "snippet_tokens": 24,
    },
    "night_shift": {
        "enabled": True,
        "start": "18:00",
        "end": "08:00",
        "max_runtime_minutes": 840,
        "stop_new_fetch_minutes": 15,
        "poll_seconds": 20,
        "concurrency": 1,
    },
    "runtime_bridge": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8765,
    },
    "extraction": {
        "concurrency": 4,
        "skip_low_value_manual": True,
    },
    "endpoint_pool": {
        "nodes": [],
        "strategies": [],
        "role_bindings": {},
    },
    "crawler": {
        "playwright_headless": True,
        "request_timeout_sec": 60,
        "unpaywall_email": "",
        "arxiv_enabled": True,
        "playwright_enabled": True,
        "scihub_enabled": False,
        "fallback_max_year": 2021,
        "scihub_mirrors": [],
    },
    "vectors": {
        "enabled": True,
        "collection": "knowledge_cards",
        "subdir": "vector_db",
    },
    "sync": {
        "webdav": {
            "enabled": False,
            "base_url": "",
            "username": "",
            "password": "",
            "remote_dir": "/AtomicCalculationDB",
        },
        "s3": {
            "enabled": False,
            "endpoint_url": "",
            "access_key": "",
            "secret_key": "",
            "bucket": "",
            "prefix": "AtomicCalculationDB",
        },
    },
    "mcp": {
        "enabled": False,
        "transport": "stdio",
        "host": "127.0.0.1",
        "port": 8765,
        "allow_task_write": True,
        "allow_direct_db_write": False,
    },
}


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base`` without mutating either."""
    result = copy.deepcopy(dict(base))
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], Mapping)
            and isinstance(value, Mapping)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class AppConfig:
    """YAML backed configuration with dotted-path get/set helpers."""

    def __init__(self, data: Mapping[str, Any] | None = None, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self._data = _deep_merge(DEFAULTS, data or {})

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: str | Path, create: bool = True) -> "AppConfig":
        path = Path(path)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(raw, Mapping):
                    data = dict(raw)
            except (OSError, yaml.YAMLError) as exc:
                raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc
        config = cls(data, path)
        if create and not path.exists():
            config.save()
        return config

    # ------------------------------------------------------------- accessors
    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value

    def update(self, values: Mapping[str, Any]) -> None:
        self._data = _deep_merge(self._data, values)

    def resolve_secret(self, dotted: str, default: str = "") -> str:
        """Return a secret, allowing ``env:NAME`` indirection in YAML."""
        value = self.get(dotted, default)
        if isinstance(value, str) and value.startswith("env:"):
            return os.environ.get(value[4:], default)
        return value if isinstance(value, str) else default

    # ----------------------------------------------------------------- save
    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ConfigError("配置文件路径未设置")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            yaml.safe_dump(
                self._data,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, target)
        self.path = target
        return target


class ConfigError(RuntimeError):
    """Raised when the configuration file cannot be read or written."""
