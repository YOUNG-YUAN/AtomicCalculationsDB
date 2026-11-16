"""Unified model gateway.

Adapters
--------
* FastLLM / SlowReasoningLLM / VLM: any OpenAI-compatible endpoint
  (DeepSeek, OpenAI, Claude via proxy, Gemini via proxy, Ollama, vLLM, LM Studio).
* Embedding: cloud OpenAI-compatible ``/embeddings``, Ollama, or a local
  BGE-M3 ONNX model (1024-d).  The signature is locked in SQLite/Qdrant.
* Reranker: local ONNX cross-encoder or a cloud ``/rerank`` endpoint.

No torch import anywhere.  Every provider degrades to ``ModelUnavailable`` so
the retriever can fall back to FTS-only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from builtin_models.model_registry import ModelRegistry, ModelUnavailable
from models.endpoint_pool import EndpointNode, EndpointPool
from models.relay_executor import QueuePoller
from models.atomic_prompts import (
    ConflictVerdict,
    ExtractionResult,
    KnowledgeCardDraft,
    MasterCardDraft,
    build_card_revision_messages,
    build_conflict_messages,
    build_extraction_messages,
    build_master_messages,
    parse_card_revision_json,
    parse_conflict_json,
    parse_extraction_json,
)
from parser.context_bundler import ContextBundle

log = logging.getLogger(__name__)


def estimate_text_tokens(text: Any) -> int:
    """Conservative local estimate when the API omits usage."""
    body = str(text or "")
    if not body:
        return 0
    # Mixed Chinese/English: count CJK as one token and latin words as ~1.3
    cjk = len(re.findall(r"[㐀-鿿]", body))
    latin = len(re.findall(r"[A-Za-z0-9_]+", body))
    return max(1, cjk + int(latin * 1.3))


class GatewayUnavailable(RuntimeError):
    pass


def _workspace_path(workspace: Path, value: Any, default: str) -> Path:
    path = Path(str(value)) if value else workspace / default
    if path.is_absolute():
        return path
    if path.parts and path.parts[0].lower() == "workspace":
        path = Path(*path.parts[1:])
    return workspace / path


def _extract_json(text: str) -> Any:
    body = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", body, re.S | re.I)
    if fenced:
        body = fenced.group(1).strip()
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型响应中没有 JSON 对象")
    return json.loads(body[start : end + 1])


@dataclass
class ProviderStatus:
    name: str
    provider: str
    model: str
    available: bool
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "available": self.available,
            "message": self.message,
        }


def _messages_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, Mapping) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


class OpenAICompatibleClient:
    """Chat-completions adapter with an httpx fallback."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 180.0,
        token_callback: Any = None,
    ):
        self.name = name
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.token_callback = token_callback
        self._client = None
        self._client_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model)

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            provider="openai-compatible",
            model=self.model,
            available=self.available,
            message="" if self.available else "base_url/model 未配置",
        )

    def _openai(self):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                from openai import OpenAI

                self._client = OpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key or "not-needed",
                    timeout=self.timeout,
                )
            except Exception as exc:
                log.debug("openai SDK unavailable, using httpx: %s", exc)
                self._client = None
            return self._client

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        n_images: int = 0,
    ) -> str:
        if not self.available:
            raise GatewayUnavailable(f"{self.name} 未配置 base_url/model")
        client = self._openai()
        if client is not None:
            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                }
                if max_tokens:
                    kwargs["max_tokens"] = max_tokens
                if response_format:
                    kwargs["response_format"] = response_format
                response = client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                if content is None:
                    raise GatewayUnavailable("模型返回空内容")
                self._record_usage(
                    getattr(response, "usage", None),
                    messages,
                    str(content),
                    n_images=n_images,
                )
                return str(content)
            except Exception as exc:
                if not self.base_url:
                    raise GatewayUnavailable(str(exc)) from exc
                log.debug("openai SDK call failed, falling back to httpx: %s", exc)
        return self._chat_httpx(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            n_images=n_images,
        )

    def _chat_httpx(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        max_tokens: int | None,
        response_format: dict[str, Any] | None,
        n_images: int = 0,
    ) -> str:
        import httpx

        url = self.base_url + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if response_format:
            payload["response_format"] = response_format
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GatewayUnavailable(f"无法解析模型响应: {data}") from exc
        self._record_usage(
            data.get("usage"), messages, str(content), n_images=n_images
        )
        return str(content)

    def _record_usage(
        self, usage: Any, messages: list[dict[str, Any]], content: str,
        *, n_images: int = 0,
    ) -> None:
        if self.token_callback is None:
            return
        prompt = 0
        completion = 0
        estimated = usage is None
        if usage is not None:
            def value(*names: str) -> int:
                for name in names:
                    raw = usage.get(name) if isinstance(usage, Mapping) else getattr(usage, name, None)
                    if raw is not None:
                        try:
                            return int(raw)
                        except (TypeError, ValueError):
                            return 0
                return 0

            prompt = value("prompt_tokens", "input_tokens")
            completion = value("completion_tokens", "output_tokens")
        if prompt <= 0:
            prompt = estimate_text_tokens(_messages_text(messages))
            estimated = True
        if completion <= 0:
            completion = estimate_text_tokens(content)
            estimated = True
        if n_images:
            prompt += n_images * 800
        try:
            self.token_callback(
                self.name, prompt, completion, self.model, estimated
            )
        except Exception:
            log.debug("Token usage callback failed", exc_info=True)

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> Any:
        try:
            content = self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        except Exception:
            content = self.chat(
                messages, temperature=temperature, max_tokens=max_tokens
            )
        return _extract_json(content)

    def vision(self, prompt: str, image_bytes: bytes, mime: str = "image/png") -> str:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{encoded}"},
                    },
                ],
            }
        ]
        return self.chat(messages, temperature=0.1, n_images=1)


class WebAIPlaywrightClient:
    """OpenAI-shaped facade over the resident Playwright web-AI page."""

    def __init__(
        self,
        node: EndpointNode,
        *,
        workspace: str | Path,
        token_callback: Any = None,
        timeout: float = 300.0,
    ):
        from models.web_ai_bridge import WebAIBridge

        self.node = node
        self.name = f"web:{node.node_id}"
        from models.web_ai_bridge import web_model_settings

        self.model = (
            web_model_settings(node)[0]
            or node.model
            or node.web_preset
            or node.name
        )
        self.timeout = timeout
        self.token_callback = token_callback
        self.bridge = WebAIBridge(workspace)

    @property
    def available(self) -> bool:
        return bool(self.node.web_url)

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            provider="web-ai-playwright",
            model=self.model,
            available=self.available,
            message="" if self.available else "web_url 未配置",
        )

    @staticmethod
    def _prompt(messages: list[dict[str, Any]]) -> str:
        blocks: list[str] = []
        for message in messages:
            role = str(message.get("role") or "user").upper()
            content = message.get("content")
            if isinstance(content, str):
                text = content
            else:
                text = _messages_text([message])
            if text.strip():
                blocks.append(f"[{role}]\n{text.strip()}")
        return "\n\n".join(blocks)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        n_images: int = 0,
    ) -> str:
        if not self.available:
            raise GatewayUnavailable(f"{self.name} 未配置 web_url")
        prompt = self._prompt(messages)
        try:
            content = self.bridge.ask(
                self.node,
                prompt,
                timeout_ms=int(self.timeout * 1000),
            )
        except Exception as exc:
            raise GatewayUnavailable(f"{self.name} 网页调用失败：{exc}") from exc
        if self.token_callback:
            try:
                self.token_callback(
                    self.name,
                    estimate_text_tokens(prompt),
                    estimate_text_tokens(content),
                    self.model,
                    True,
                )
            except Exception:
                log.debug("Web AI token usage callback failed", exc_info=True)
        return content

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> Any:
        from models.web_ai_bridge import _extract_json_payload

        return _extract_json_payload(
            self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        )

    def vision(self, prompt: str, image_bytes: bytes, mime: str = "image/png") -> str:
        # The persistent web page can accept images through the same prompt
        # box when the site supports them; the first version sends the text
        # instruction and leaves binary upload to the site-specific page.
        return self.chat([{"role": "user", "content": prompt}], n_images=1)


class AgentCLIClient:
    """OpenAI-shaped facade over a headless local Agent CLI (e.g. Codex)."""

    def __init__(
        self,
        node: EndpointNode,
        *,
        workspace: str | Path,
        token_callback: Any = None,
    ):
        self.node = node
        self.workspace = Path(workspace)
        self.token_callback = token_callback
        self.name = f"agent_cli:{node.node_id}"
        self.model = str(node.model or node.cli_preset or node.cli_command or "agent-cli")

    @property
    def available(self) -> bool:
        return bool(
            str(self.node.cli_command or "").strip()
            or (self.node.cli_preset or "").lower() == "codex"
        )

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            provider="agent-cli",
            model=self.model,
            available=self.available,
            message="" if self.available else "cli_command 未配置",
        )

    @staticmethod
    def _prompt(messages: list[dict[str, Any]]) -> str:
        blocks: list[str] = []
        for message in messages:
            role = str(message.get("role") or "user").upper()
            content = message.get("content")
            text = content if isinstance(content, str) else _messages_text([message])
            if str(text or "").strip():
                blocks.append(f"[{role}]\n{str(text).strip()}")
        return "\n\n".join(blocks)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        n_images: int = 0,
    ) -> str:
        from models.agent_cli import AgentCLIError, run_agent_cli

        if not self.available:
            raise GatewayUnavailable(f"{self.name} 未配置 Agent CLI 命令")
        prompt = self._prompt(messages)
        try:
            content = run_agent_cli(
                self.node,
                prompt,
                workspace=self.workspace,
            )
        except AgentCLIError as exc:
            raise GatewayUnavailable(str(exc)) from exc
        if self.token_callback:
            try:
                self.token_callback(
                    self.name,
                    estimate_text_tokens(prompt),
                    estimate_text_tokens(content),
                    self.model,
                    True,
                )
            except Exception:
                log.debug("Agent CLI token usage callback failed", exc_info=True)
        return content

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> Any:
        from models.web_ai_bridge import _extract_json_payload

        return _extract_json_payload(
            self.chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        )

    def vision(self, prompt: str, image_bytes: bytes, mime: str = "image/png") -> str:
        return self.chat([{"role": "user", "content": prompt}], n_images=1)


class QueuePollingClient:
    """OpenAI-shaped facade over a submit/status queue endpoint."""

    def __init__(
        self,
        node: EndpointNode,
        *,
        token_callback: Any = None,
    ):
        self.node = node
        self.token_callback = token_callback
        self.name = f"queue:{node.node_id}"

    @property
    def available(self) -> bool:
        return bool(self.node.submit_url and self.node.status_url)

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            provider="queue-polling",
            model=self.node.model,
            available=self.available,
            message="" if self.available else "未配置 submit_url/status_url",
        )

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> Any:
        if not self.available:
            raise GatewayUnavailable(
                f"队列节点 {self.node.name} 未配置 submit_url/status_url"
            )
        import httpx

        payload = {
            "model": self.node.model,
            "messages": messages,
            "prompt": _messages_text(messages),
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        headers = {"Content-Type": "application/json"}
        if self.node.api_key and not self.node.api_key.startswith("env:"):
            headers["Authorization"] = f"Bearer {self.node.api_key}"
        with httpx.Client(timeout=max(30, self.node.max_queue_wait_sec + 120)) as client:
            response = client.post(
                self.node.submit_url, json=payload, headers=headers
            )
            response.raise_for_status()
            submitted = response.json()
            job_id = str(
                submitted.get("job_id")
                or submitted.get("task_id")
                or submitted.get("id")
                or ""
            )
            if not job_id:
                raise GatewayUnavailable("队列响应缺少 job_id/task_id")
            poller = QueuePoller(
                emit=lambda message: log.info("%s", message)
            )

            def status() -> dict[str, Any]:
                url = self.node.status_url
                params = {"job_id": job_id}
                if "{job_id}" in url:
                    url = url.format(job_id=job_id)
                    params = {}
                result = client.get(url, headers=headers, params=params)
                result.raise_for_status()
                return dict(result.json())

            final = poller.wait(self.node, status)
        content = (
            final.get("content")
            or final.get("result")
            or final.get("output")
            or final.get("answer")
        )
        if isinstance(content, Mapping):
            return dict(content)
        if isinstance(content, str) and content.strip():
            if self.token_callback is not None:
                usage = final.get("usage") or {}
                prompt = int(
                    usage.get("prompt_tokens")
                    or usage.get("input_tokens")
                    or estimate_text_tokens(_messages_text(messages))
                )
                completion = int(
                    usage.get("completion_tokens")
                    or usage.get("output_tokens")
                    or estimate_text_tokens(content)
                )
                try:
                    self.token_callback(
                        self.name, prompt, completion, self.node.model, False
                    )
                except Exception:
                    pass
            return _extract_json(content)
        raise GatewayUnavailable("队列任务完成但没有可解析的内容")


class FastLLM:
    def __init__(self, client: OpenAICompatibleClient):
        self.client = client

    def extract_cards(
        self,
        bundle: ContextBundle,
        *,
        doc_metadata: dict[str, Any] | None = None,
        manual_mode: bool = False,
    ) -> ExtractionResult:
        messages = build_extraction_messages(
            bundle, doc_metadata=doc_metadata, manual_mode=manual_mode
        )
        payload = self.client.chat_json(messages)
        try:
            return ExtractionResult.model_validate(payload)
        except Exception:
            # The model may have wrapped the object; reuse the strict parser.
            return parse_extraction_json(json.dumps(payload, ensure_ascii=False))

    def revise_card(
        self,
        card: dict[str, Any],
        evidence_text: str,
        instruction: str,
    ) -> KnowledgeCardDraft:
        messages = build_card_revision_messages(card, evidence_text, instruction)
        payload = self.client.chat_json(messages, temperature=0.1)
        return parse_card_revision_json(json.dumps(payload, ensure_ascii=False))


class SlowReasoningLLM:
    def __init__(self, client: OpenAICompatibleClient):
        self.client = client

    def adjudicate_conflict(
        self, card_a: Mapping[str, Any], card_b: Mapping[str, Any]
    ) -> ConflictVerdict:
        messages = build_conflict_messages(dict(card_a), dict(card_b))
        payload = self.client.chat_json(messages, temperature=0.0)
        return ConflictVerdict.model_validate(payload)

    def generate_master(
        self,
        cards: Sequence[Mapping[str, Any]],
        *,
        isoelectronic: str = "",
        method: str = "",
    ) -> MasterCardDraft:
        messages = build_master_messages(
            [dict(card) for card in cards],
            isoelectronic=isoelectronic,
            method=method,
        )
        payload = self.client.chat_json(messages, temperature=0.1)
        return MasterCardDraft.model_validate(payload)


class VLMClient:
    def __init__(self, client: OpenAICompatibleClient):
        self.client = client

    def describe(self, image_bytes: bytes, prompt: str, mime: str = "image/png") -> str:
        return self.client.vision(prompt, image_bytes, mime=mime)


class EmbeddingProvider:
    def __init__(
        self,
        config: Any,
        workspace_dir: str | Path,
        registry: ModelRegistry | None = None,
        usage_callback: Any = None,
        node: EndpointNode | None = None,
    ):
        self.config = config
        self.workspace = Path(workspace_dir)
        self.registry = registry
        self.usage_callback = usage_callback
        self.node = node
        # V2.3.10: embedding configuration lives exclusively in the endpoint
        # pool.  There is no hidden legacy embedding.* fallback.
        self.provider = "none"
        self.model = ""
        self.dimension = 1024
        self.base_url = ""
        self.api_key = ""
        self.local_model = self.workspace / "models/bge-m3/model.onnx"
        self.local_tokenizer = self.workspace / "models/bge-m3/tokenizer.json"
        if node is not None:
            self.model = node.model or ""
            if node.node_type == "LOCAL_ONNX":
                self.provider = "local"
                self.dimension = int(node.model_dim or 1024)
                self.base_url = ""
                self.api_key = ""
                self.local_model = _workspace_path(
                    self.workspace, node.model_path, "models/bge-m3/model.onnx"
                )
                self.local_tokenizer = _workspace_path(
                    self.workspace,
                    node.tokenizer_path,
                    "models/bge-m3/tokenizer.json",
                )
            elif node.node_type in ("OPENAI_COMPATIBLE_API", "QUEUE_POLLING_API"):
                self.provider = "cloud"
                self.base_url = node.base_url.rstrip("/")
                self.api_key = node.api_key
                if self.api_key.startswith("env:"):
                    self.api_key = os.environ.get(self.api_key[4:], "")
                self.dimension = int(node.model_dim or 1024)
        self._session = None
        self._tokenizer = None

    @property
    def signature(self) -> str:
        return f"{self.model}::dim={self.dimension}"

    @property
    def available(self) -> bool:
        if self.provider in ("cloud", "openai", "siliconflow", "api"):
            return bool(self.base_url and self.model)
        if self.provider == "ollama":
            return bool(self.base_url or self.model)
        if self.provider in ("local", "onnx", "fastembed"):
            return self.local_model.is_file() and self.local_tokenizer.is_file()
        return False

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name="embedding",
            provider=self.provider,
            model=self.model,
            available=self.available,
            message="" if self.available else "未配置或本地 ONNX 文件缺失",
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        texts = [str(text) for text in texts]
        if not texts:
            return []
        if self.provider in ("cloud", "openai", "siliconflow", "api"):
            vectors = self._embed_openai(texts)
        elif self.provider == "ollama":
            vectors = self._embed_ollama(texts)
        elif self.provider in ("local", "onnx", "fastembed"):
            vectors = self._embed_local(texts)
        else:
            raise ModelUnavailable(f"Embedding provider 未启用: {self.provider}")
        for vector in vectors:
            if len(vector) != self.dimension:
                raise ModelUnavailable(
                    f"Embedding 维度 {len(vector)} != 配置 {self.dimension}"
                )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def _embed_openai(self, texts: Sequence[str]) -> list[list[float]]:
        import httpx

        if not self.base_url:
            raise ModelUnavailable("Embedding base_url 未配置")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self.base_url + "/embeddings"
        with httpx.Client(timeout=120) as client:
            response = client.post(
                url,
                headers=headers,
                json={"model": self.model, "input": list(texts)},
            )
            response.raise_for_status()
            data = response.json()
        if self.usage_callback is not None:
            usage = data.get("usage") or {}
            prompt = (
                usage.get("prompt_tokens")
                or usage.get("input_tokens")
                or estimate_text_tokens("\n".join(texts))
            )
            try:
                self.usage_callback("embedding", int(prompt), 0, self.model)
            except Exception:
                log.debug("Embedding token callback failed", exc_info=True)
        items = sorted(data.get("data", []), key=lambda item: item.get("index", 0))
        return [list(map(float, item["embedding"])) for item in items]

    def _embed_ollama(self, texts: Sequence[str]) -> list[list[float]]:
        import httpx

        base = self.base_url or "http://localhost:11434"
        with httpx.Client(timeout=180) as client:
            try:
                response = client.post(
                    base.rstrip("/") + "/api/embed",
                    json={"model": self.model, "input": list(texts)},
                )
                response.raise_for_status()
                data = response.json()
                embeddings = data.get("embeddings")
                if embeddings:
                    return [list(map(float, vector)) for vector in embeddings]
            except Exception:
                pass
            vectors: list[list[float]] = []
            for text in texts:
                response = client.post(
                    base.rstrip("/") + "/api/embeddings",
                    json={"model": self.model, "prompt": text},
                )
                response.raise_for_status()
                vectors.append(list(map(float, response.json()["embedding"])))
            return vectors

    def release_local_session(self) -> None:
        """Free MiniLM/BGE session memory before another local model runs."""
        self._session = None
        self._tokenizer = None
        import gc

        gc.collect()

    def _embed_local(
        self, texts: Sequence[str], *, batch_size: int = 16
    ) -> list[list[float]]:
        import numpy as np

        if self._session is None:
            if not self.available:
                raise ModelUnavailable("本地 BGE-M3 ONNX 文件缺失")
            try:
                import onnxruntime as ort
                from tokenizers import Tokenizer

                options = ort.SessionOptions()
                options.enable_cpu_mem_arena = False
                options.enable_mem_pattern = False
                options.intra_op_num_threads = max(
                    1, min(4, os.cpu_count() or 1)
                )
                self._session = ort.InferenceSession(
                    str(self.local_model),
                    sess_options=options,
                    providers=["CPUExecutionProvider"],
                )
                self._tokenizer = Tokenizer.from_file(str(self.local_tokenizer))
                self._tokenizer.enable_truncation(max_length=512)
                self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            except Exception as exc:
                raise ModelUnavailable(f"本地 Embedding 加载失败: {exc}") from exc
        values = [str(text or "") for text in texts]
        if not values:
            return []
        batch_size = max(1, min(64, int(batch_size or 16)))
        names = {item.name for item in self._session.get_inputs()}
        output_names = [item.name for item in self._session.get_outputs()]
        vectors: list[list[float]] = []
        for start in range(0, len(values), batch_size):
            batch = values[start:start + batch_size]
            encodings = self._tokenizer.encode_batch(batch)
            input_ids = np.array([enc.ids for enc in encodings], dtype=np.int64)
            attention = np.array(
                [[1 if token != 0 else 0 for token in enc.ids] for enc in encodings],
                dtype=np.int64,
            )
            inputs: dict[str, Any] = {self._session.get_inputs()[0].name: input_ids}
            if "attention_mask" in names:
                inputs["attention_mask"] = attention
            outputs = self._session.run(None, inputs)
            if "sentence_embedding" in output_names:
                pooled = np.asarray(
                    outputs[output_names.index("sentence_embedding")],
                    dtype=np.float32,
                )
            else:
                hidden = np.asarray(outputs[0], dtype=np.float32)
                if hidden.ndim == 3:
                    mask = attention[..., None].astype(np.float32)
                    pooled = np.sum(hidden * mask, axis=1) / np.clip(
                        np.sum(mask, axis=1), 1e-9, None
                    )
                else:
                    pooled = hidden
            norm = np.linalg.norm(pooled, axis=1, keepdims=True)
            pooled = pooled / np.clip(norm, 1e-9, None)
            vectors.extend(pooled.tolist())
        return vectors


class RerankerProvider:
    def __init__(
        self,
        config: Any,
        registry: ModelRegistry,
        node: EndpointNode | None = None,
    ):
        self.config = config
        self.registry = registry
        self.node = node
        # V2.3.10: reranker configuration lives exclusively in the pool.
        self.provider = "none"
        self.model = ""
        self.base_url = ""
        self.api_key = ""
        self.model_path = ""
        if node is not None:
            self.model = node.model or ""
            self.model_path = str(node.model_path or "")
            if node.node_type == "LOCAL_ONNX":
                self.provider = "local"
                self.base_url = ""
                self.api_key = ""
            elif node.node_type in ("OPENAI_COMPATIBLE_API", "QUEUE_POLLING_API"):
                self.provider = "cloud"
                self.base_url = node.base_url.rstrip("/")
                self.api_key = node.api_key
                if self.api_key.startswith("env:"):
                    self.api_key = os.environ.get(self.api_key[4:], "")

    @property
    def available(self) -> bool:
        if self.provider in ("cloud", "api", "siliconflow", "jina", "cohere"):
            return bool(self.base_url)
        if self.provider in ("local", "onnx"):
            try:
                return self.registry.is_present("local_reranker")
            except Exception:
                return False
        return False

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name="reranker",
            provider=self.provider,
            model=self.model,
            available=self.available,
            message="" if self.available else "未配置或本地 ONNX 文件缺失",
        )

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        documents = [str(document) for document in documents]
        if not documents:
            return []
        if self.provider in ("local", "onnx"):
            try:
                return self.registry.rerank_local(query, documents)
            except Exception as exc:
                raise ModelUnavailable(str(exc)) from exc
        if self.provider in ("cloud", "api", "siliconflow", "jina", "cohere"):
            import httpx

            if not self.base_url:
                raise ModelUnavailable("Reranker base_url 未配置")
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            url = self.base_url + "/rerank"
            with httpx.Client(timeout=120) as client:
                response = client.post(
                    url,
                    headers=headers,
                    json={
                        "model": self.model,
                        "query": query,
                        "documents": documents,
                        "top_n": len(documents),
                    },
                )
                response.raise_for_status()
                data = response.json()
            scores = [0.0] * len(documents)
            for item in data.get("results", data.get("data", [])):
                index = int(item.get("index", 0))
                score = item.get("relevance_score", item.get("score", 0.0))
                if 0 <= index < len(scores):
                    scores[index] = float(score)
            return scores
        raise ModelUnavailable(f"Reranker provider 未启用: {self.provider}")


class ModelGateway:
    def __init__(
        self,
        config: Any,
        workspace_dir: str | Path,
        *,
        registry: ModelRegistry | None = None,
        db: Any = None,
    ):
        self.config = config
        self.workspace = Path(workspace_dir)
        self.registry = registry or ModelRegistry(self.workspace)
        self.db = db
        self._active_doc_id: str | None = None
        self.pool = EndpointPool(config)
        self._role_client_signatures: dict[str, Any] = {}
        self._route_lock = threading.RLock()
        self.fast_client = self._client_for_role("fast_llm")
        self.slow_client = self._client_for_role("slow_llm")
        self.audit_client = self._client_for_role("audit_llm")
        self.vlm_client = self._client_for_role("vlm")
        self.fast = FastLLM(self.fast_client)
        self.slow = SlowReasoningLLM(self.slow_client)
        self.audit = FastLLM(self.audit_client)
        self.vlm = VLMClient(self.vlm_client)
        self.embedding = EmbeddingProvider(
            config, self.workspace, self.registry,
            usage_callback=self._record_embedding_usage,
            node=self.pool.node_for_role("embedding"),
        )
        self.reranker = RerankerProvider(
            config, self.registry, node=self.pool.node_for_role("reranker")
        )

    def set_active_document(self, doc_id: str | None) -> None:
        self._active_doc_id = str(doc_id).upper() if doc_id else None

    def _record_token_usage(
        self, role: str, prompt: int, completion: int,
        model: str, estimated: bool,
    ) -> None:
        if self.db is None:
            return
        self.db.add_token_usage(self._active_doc_id, prompt, completion)

    def _record_embedding_usage(
        self, role: str, prompt: int, completion: int, model: str
    ) -> None:
        if self.db is None:
            return
        self.db.add_token_usage(self._active_doc_id, prompt, 0)

    def _unconfigured_client(self, role: str) -> OpenAICompatibleClient:
        """An explicit unavailable client; never reads the deleted legacy config."""
        return OpenAICompatibleClient(
            name=role,
            base_url="",
            api_key="",
            model="",
            token_callback=self._record_token_usage,
        )

    def _client_from_node(self, role: str, node: EndpointNode) -> Any:
        if node.node_type == "AGENT_CLI":
            return AgentCLIClient(
                node,
                workspace=self.workspace,
                token_callback=self._record_token_usage,
            )
        if node.node_type == "WEB_AI_PLAYWRIGHT":
            if not node.web_url:
                raise GatewayUnavailable(
                    f"{node.name} 未配置网页地址（web_url）"
                )
            return WebAIPlaywrightClient(
                node,
                workspace=self.workspace,
                token_callback=self._record_token_usage,
            )
        if (
            node.node_type == "QUEUE_POLLING_API"
            and node.submit_url
            and node.status_url
        ):
            return QueuePollingClient(
                node, token_callback=self._record_token_usage
            )
        if node.node_type in (
            "OPENAI_COMPATIBLE_API",
            "QUEUE_POLLING_API",
        ) and node.base_url and node.model:
            api_key = node.api_key
            if api_key.startswith("env:"):
                api_key = os.environ.get(api_key[4:], "")
            return OpenAICompatibleClient(
                name=f"{role}:{node.node_id}",
                base_url=node.base_url,
                api_key=api_key,
                model=node.model,
                token_callback=self._record_token_usage,
            )
        return self._unconfigured_client(role)

    def _client_for_role(self, role: str) -> Any:
        node = self.pool.node_for_role(role)
        if node is not None:
            return self._client_from_node(role, node)
        return self._unconfigured_client(role)

    def refresh_route(self) -> None:
        """Re-resolve time-of-day role bindings before a new LLM operation."""
        with self._route_lock:
            for role, attr in (
                ("fast_llm", "fast_client"),
                ("slow_llm", "slow_client"),
                ("audit_llm", "audit_client"),
                ("vlm", "vlm_client"),
            ):
                node = self.pool.node_for_role(role)
                strategy = self.pool.strategy_for_role(role)
                strategy_enabled = getattr(strategy, "enabled", True) if strategy else True
                signature = (
                    node.node_id,
                    node.base_url,
                    node.model,
                    node.api_key,
                    node.node_type,
                    node.submit_url,
                    node.status_url,
                    node.web_url,
                    node.web_preset,
                    node.send_mode,
                    node.proxy_url,
                    node.web_model,
                    node.thinking_level,
                    node.web_cdp_port,
                    node.cli_preset,
                    node.cli_command,
                    node.cli_args,
                    node.cli_workdir,
                    node.cli_timeout_sec,
                    node.cli_sandbox,
                    strategy_enabled,
                ) if node is not None else (None, strategy_enabled)
                if self._role_client_signatures.get(role) == signature:
                    continue
                client = self._client_for_role(role)
                if role == "vlm" and not strategy_enabled:
                    # Mark VLM as disabled by strategy switch
                    if hasattr(client, "available"):
                        client.available = False
                setattr(self, attr, client)
                if role == "fast_llm":
                    self.fast = FastLLM(client)
                elif role == "slow_llm":
                    self.slow = SlowReasoningLLM(client)
                elif role == "audit_llm":
                    self.audit = FastLLM(client)
                else:
                    self.vlm = VLMClient(client)
                self._role_client_signatures[role] = signature

    def reload_pool(self) -> None:
        self.pool = EndpointPool(self.config)
        self._role_client_signatures.clear()
        self.refresh_route()
        self.fast_client = self.fast.client
        self.slow_client = self.slow.client
        self.audit_client = self.audit.client
        self.vlm_client = self.vlm.client

    @property
    def embedding_signature(self) -> str:
        return self.embedding.signature

    def health(self) -> list[dict[str, Any]]:
        return [
            self.fast_client.status().as_dict(),
            self.slow_client.status().as_dict(),
            self.audit_client.status().as_dict(),
            self.vlm_client.status().as_dict(),
            self.embedding.status().as_dict(),
            self.reranker.status().as_dict(),
        ]

    def extract_cards(
        self,
        bundle: ContextBundle,
        *,
        doc_metadata: dict[str, Any] | None = None,
        manual_mode: bool = False,
    ) -> ExtractionResult:
        self.refresh_route()
        chain = self.pool.chain_for_role("fast_llm")
        if chain and (
            len(chain) > 1 or chain[0].node_type == "QUEUE_POLLING_API"
        ):
            errors: list[str] = []
            for node in chain:
                try:
                    client = self._client_from_node("fast_llm", node)
                    return FastLLM(client).extract_cards(
                        bundle,
                        doc_metadata=doc_metadata,
                        manual_mode=manual_mode,
                    )
                except Exception as exc:
                    errors.append(f"{node.name}: {exc}")
                    log.warning("Extraction relay node %s failed: %s", node.name, exc)
            raise GatewayUnavailable("；".join(errors) or "所有接力节点均失败")
        return self.fast.extract_cards(
            bundle, doc_metadata=doc_metadata, manual_mode=manual_mode
        )

    def revise_card(
        self,
        card: dict[str, Any],
        evidence_text: str,
        instruction: str,
    ) -> KnowledgeCardDraft:
        self.refresh_route()
        return self.fast.revise_card(card, evidence_text, instruction)

    def adjudicate(self, card_a: Mapping[str, Any], card_b: Mapping[str, Any]) -> ConflictVerdict:
        self.refresh_route()
        return self.slow.adjudicate_conflict(card_a, card_b)

    def generate_master(
        self,
        cards: Sequence[Mapping[str, Any]],
        *,
        isoelectronic: str = "",
        method: str = "",
    ) -> MasterCardDraft:
        self.refresh_route()
        return self.slow.generate_master(
            cards, isoelectronic=isoelectronic, method=method
        )
