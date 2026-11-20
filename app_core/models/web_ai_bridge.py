"""Web-AI Playwright bridge and semi-automatic clipboard collaboration.

Playwright is optional.  Login profiles persist under
``workspace/web_ai_profiles/<node_id>/``; when Playwright or a browser is not
available the bridge raises ``WebAIBridgeUnavailable`` and the UI can fall back
to the clipboard workflow.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from models.atomic_prompts import (
    CARD_SYSTEM_PROMPT,
    MANUAL_MODE_INSTRUCTION,
    ExtractionResult,
    parse_extraction_json,
    repair_json_text,
    sanitize_z_num,
)
from models.endpoint_pool import EndpointNode
from models.aistudio_autopilot import (
    AI_STUDIO_NEW_CHAT,
    AistudioPageDriver,
    AutopilotError,
    launch_or_connect_browser,
    close_browser,
)
from parser.batch_packer import DistilledItem, DynamicBatch, DynamicBatchPacker
from parser.context_bundler import ContextBundle, bundle_hash


class WebAIBridgeUnavailable(RuntimeError):
    pass


# Presets keep the URL, send shortcut and DOM selectors in one place so the
# dialog and the persistent browser session cannot drift apart.
WEB_AI_PRESETS: dict[str, dict[str, str]] = {
    "google_ai_studio": {
        "label": "Google AI Studio (aistudio.google.com)",
        "url": "https://aistudio.google.com/prompts/new_chat",
        "send_mode": "ctrl_enter",
        "input": 'textarea, ms-autosize-textarea, [contenteditable="true"]',
        "send": 'button:has-text("Run")',
        "stop": 'button:has-text("Stop")',
        "response": "ms-chat-turn, .model-response-text, [data-test-id='response']",
        "clear": 'button:has-text("Clear"), button:has-text("Delete turns")',
        "model": "Gemini 4 Argon",
        "thinking": "High",
        "profile_name": "Google AI Studio 网页版",
    },
    "deepseek": {
        "label": "DeepSeek 网页版",
        "url": "https://chat.deepseek.com/",
        "send_mode": "enter",
        "input": "textarea",
        "send": 'button[type="submit"]',
        "stop": 'button:has-text("Stop")',
        "response": ".markdown-body, .ds-markdown",
        "profile_name": "DeepSeek 网页版",
    },
    "kimi": {
        "label": "Kimi 网页版",
        "url": "https://kimi.moonshot.cn/",
        "send_mode": "enter",
        "input": "textarea",
        "send": 'button[type="submit"]',
        "stop": 'button:has-text("停止")',
        "response": ".markdown-body",
        "profile_name": "Kimi 网页版",
    },
    "doubao": {
        "label": "豆包网页版",
        "url": "https://www.doubao.com/chat/",
        "send_mode": "enter",
        "input": "textarea",
        "send": 'button[type="submit"]',
        "stop": 'button:has-text("停止")',
        "response": ".markdown-body, [data-testid='message-content']",
        "profile_name": "豆包网页版",
    },
    "qwen": {
        "label": "通义千问",
        "url": "https://tongyi.aliyun.com/qianwen/",
        "send_mode": "enter",
        "input": "textarea",
        "send": 'button[type="submit"]',
        "stop": 'button:has-text("停止")',
        "response": ".markdown-body, .qwen-markdown",
        "profile_name": "通义千问网页版",
    },
    "chatgpt": {
        "label": "ChatGPT",
        "url": "https://chatgpt.com/",
        "send_mode": "enter",
        "input": "#prompt-textarea, textarea",
        "send": 'button[data-testid="send-button"]',
        "stop": 'button[aria-label="Stop generating"]',
        "response": '[data-message-author-role="assistant"]',
        "profile_name": "ChatGPT 网页版",
    },
    "custom": {
        "label": "自定义网页",
        "url": "",
        "send_mode": "enter",
        "input": 'textarea, [contenteditable="true"]',
        "send": "button",
        "stop": "",
        "response": "body",
        "profile_name": "自定义网页 AI",
    },
}


def web_preset_choices() -> list[tuple[str, str]]:
    return [(key, value["label"]) for key, value in WEB_AI_PRESETS.items()]


def preset_key_for_url(url: str) -> str:
    value = str(url or "").strip().lower()
    for key, preset in WEB_AI_PRESETS.items():
        candidate = str(preset.get("url") or "").strip().lower()
        if candidate and (value == candidate or value.startswith(candidate.rstrip("/") + "/")):
            return key
    return "custom"


def _normalise_proxy(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "=" in text:
        # Registry ProxyServer may look like
        # "http=host:port;https=host:port".
        parts = {
            key.strip().lower(): item.strip()
            for key, item in (segment.split("=", 1) for segment in text.split(";") if "=" in segment)
        }
        text = parts.get("https") or parts.get("http") or text
    if "://" not in text:
        text = f"http://{text}"
    return text


def system_proxy_url() -> str:
    """Return an explicit proxy from the environment or Windows registry."""
    for name in (
        "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
        "HTTP_PROXY", "http_proxy",
    ):
        value = _normalise_proxy(os.environ.get(name, ""))
        if value:
            return value
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                if enabled:
                    server, _ = winreg.QueryValueEx(key, "ProxyServer")
                    return _normalise_proxy(str(server))
        except Exception:
            pass
    return ""


def resolved_proxy_url(node: EndpointNode) -> str:
    return _normalise_proxy(node.proxy_url) or system_proxy_url()


def merged_selectors(node: EndpointNode) -> dict[str, str]:
    preset = WEB_AI_PRESETS.get(str(node.web_preset or ""))
    selectors = dict(preset or {})
    # URL/label/profile_name are metadata; only DOM keys belong in selectors.
    selectors.pop("label", None)
    selectors.pop("url", None)
    selectors.pop("profile_name", None)
    selectors.pop("model", None)
    selectors.pop("thinking", None)
    selectors.update(dict(node.selectors or {}))
    if str(node.web_preset or "") == "custom":
        selectors["send_mode"] = node.send_mode or "enter"
    else:
        selectors.setdefault("send_mode", node.send_mode or "enter")
    return selectors


def is_aistudio_node(node: EndpointNode) -> bool:
    return (
        str(node.web_preset or "") == "google_ai_studio"
        or "aistudio.google.com" in str(node.web_url or "")
    )


def web_model_settings(node: EndpointNode) -> tuple[str, str]:
    preset = WEB_AI_PRESETS.get(str(node.web_preset or "")) or {}
    model = str(node.web_model or preset.get("model") or "").strip()
    thinking = str(node.thinking_level or preset.get("thinking") or "").strip()
    return model, thinking


def browser_context_options(
    node: EndpointNode,
    *,
    headless: bool = False,
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "headless": bool(headless),
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        "ignore_default_args": ["--enable-automation"],
    }
    proxy = resolved_proxy_url(node)
    if proxy:
        options["proxy"] = {"server": proxy}
    return options


def launch_persistent_browser(
    playwright: Any,
    node: EndpointNode,
    profile_dir: str | Path,
    *,
    headless: bool = False,
) -> Any:
    """Launch Chrome, then Edge, then bundled Chromium with a persistent profile."""
    browser_type = getattr(playwright, "chromium", None)
    if browser_type is None:
        raise WebAIBridgeUnavailable("Playwright 缺少 chromium 浏览器")
    last_error: Exception | None = None
    for channel in ("chrome", "msedge", None):
        options = browser_context_options(node, headless=headless)
        if channel:
            options["channel"] = channel
        try:
            return browser_type.launch_persistent_context(
                str(profile_dir),
                **options,
            )
        except Exception as exc:
            last_error = exc
    raise WebAIBridgeUnavailable(
        f"无法启动常驻浏览器（Chrome / Edge / Chromium 均失败）：{last_error}"
    )


_SESSIONS: dict[str, "_PersistentBrowserSession"] = {}
_SESSIONS_LOCK = threading.Lock()


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _page_closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return True


def _first_visible(page: Any, selector: str, timeout_ms: int = 500) -> Any:
    if not selector:
        return None
    try:
        locator = page.locator(selector)
        count = int(locator.count())
        for index in range(count - 1, -1, -1):
            item = locator.nth(index)
            try:
                if item.is_visible(timeout=timeout_ms):
                    return item
            except Exception:
                continue
        return locator.nth(count - 1) if count else None
    except Exception:
        return None


def _read_response(page: Any, selector: str) -> str:
    locator = _first_visible(page, selector, timeout_ms=1500)
    try:
        if locator is not None:
            return str(locator.inner_text(timeout=3000)).strip()
    except Exception:
        pass
    try:
        return str(page.inner_text("body", timeout=3000)).strip()
    except Exception:
        return ""


def _looks_generating(page: Any, selectors: Mapping[str, str], previous: str, current: str) -> bool:
    if _first_visible(page, selectors.get("stop", ""), timeout_ms=300) is not None:
        return True
    return bool(current) and len(current) > len(previous)


def _click_locator(page: Any, selector: str, timeout_ms: int = 5000) -> bool:
    locator = _first_visible(page, selector, timeout_ms=800)
    if locator is None:
        return False
    try:
        locator.click(timeout=timeout_ms)
        return True
    except Exception:
        return False


def _fill_prompt(page: Any, locator: Any, prompt: str, timeout_ms: int) -> None:
    try:
        locator.fill(str(prompt), timeout=timeout_ms)
        return
    except Exception:
        pass
    try:
        locator.click(timeout=timeout_ms)
    except Exception:
        pass
    try:
        locator.focus(timeout=timeout_ms)
    except Exception:
        pass
    try:
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(str(prompt))
    except Exception:
        try:
            page.keyboard.type(str(prompt))
        except Exception as exc:
            raise WebAIBridgeUnavailable(f"无法填入网页输入框：{exc}") from exc


class _PersistentBrowserSession:
    """A persistent Playwright page owned by a background daemon thread."""

    def __init__(
        self,
        key: str,
        profile: Path,
        node: EndpointNode,
        *,
        headless: bool,
        navigation_timeout_ms: int = 120_000,
    ):
        self.key = key
        self.profile = Path(profile)
        self.node = node
        self.headless = bool(headless)
        self.navigation_timeout_ms = max(10_000, int(navigation_timeout_ms or 120_000))
        self._commands: queue.Queue[dict[str, Any]] = queue.Queue()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._error: Exception | None = None
        self._page: Any = None
        self._context: Any = None
        self._handle: Any = None
        self._prepared_signature: tuple[str, str, str] | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"web-ai-{node.node_id}",
            daemon=True,
        )

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and not self._closed.is_set()

    def usable(self) -> bool:
        if not self.alive:
            return False
        try:
            return self._page is not None and not _page_closed(self._page)
        except Exception:
            return False

    def start(self, timeout: float = 45.0) -> "_PersistentBrowserSession":
        self._thread.start()
        if not self._ready.wait(timeout):
            raise WebAIBridgeUnavailable("浏览器启动超时，请检查 Chrome / Edge 是否可用")
        if self._error is not None:
            raise WebAIBridgeUnavailable(str(self._error))
        return self

    def _run(self) -> None:
        handle = None
        try:
            handle = launch_or_connect_browser(
                self.profile,
                cdp_port=self.node.web_cdp_port,
                start_url=self.node.web_url or AI_STUDIO_NEW_CHAT,
                timeout=min(60.0, self.navigation_timeout_ms / 1000.0),
                proxy_url=resolved_proxy_url(self.node),
            )
            self._handle = handle
            self._context = handle.context
            page = handle.page
            self._page = page
            try:
                page.set_default_timeout(15_000)
            except Exception:
                pass
            self._ready.set()
            while not self._closed.is_set():
                try:
                    command = self._commands.get(timeout=0.5)
                except queue.Empty:
                    continue
                kind = str(command.get("type") or "")
                if kind == "close":
                    break
                if kind == "open":
                    try:
                        self._page.bring_to_front()
                    except Exception:
                        pass
                    continue
                event: threading.Event = command.get("event")
                result: dict[str, Any] = command.get("result") or {}
                try:
                    if kind == "prepare_login":
                        self._prepare_page(
                            command["node"],
                            int(command.get("timeout_ms") or 300_000),
                        )
                        result["ok"] = True
                    elif kind == "ask":
                        result["answer"] = self._ask_page(
                            self._page,
                            command["node"],
                            str(command["prompt"]),
                            int(command["timeout_ms"]),
                        )
                except Exception as exc:
                    result["error"] = str(exc)
                finally:
                    if event is not None:
                        event.set()
        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            close_browser(handle)
            self._closed.set()
            if not self._ready.is_set():
                self._ready.set()
            while True:
                try:
                    command = self._commands.get_nowait()
                except queue.Empty:
                    break
                event = command.get("event")
                result = command.get("result")
                if result is not None:
                    result["error"] = str(self._error or "浏览器会话已关闭")
                if event is not None:
                    event.set()

    def _prepare_page(self, node: EndpointNode, timeout_ms: int) -> None:
        if self._page is None or _page_closed(self._page):
            raise WebAIBridgeUnavailable("网页标签页已关闭，请重新点击启动浏览器")
        if is_aistudio_node(node):
            model, thinking = web_model_settings(node)
            driver = AistudioPageDriver(
                self._page,
                model=model,
                thinking=thinking,
            )
            seconds = max(5.0, min(300.0, timeout_ms / 1000.0))
            driver.ensure_logged_in(timeout_seconds=seconds)
            driver.configure()
            self._prepared_signature = (str(node.web_url or ""), model, thinking)
            return
        selectors = merged_selectors(node)
        input_selector = selectors.get("input") or 'textarea, [contenteditable="true"]'
        if _first_visible(self._page, input_selector, timeout_ms=3000) is None:
            raise WebAIBridgeUnavailable(
                "网页尚未加载出输入框；请确认已登录并打开目标网页"
            )
        self._prepared_signature = (
            str(node.web_url or ""),
            "",
            "",
        )

    def _ask_page(
        self,
        page: Any,
        node: EndpointNode,
        prompt: str,
        timeout_ms: int,
    ) -> str:
        if page is None or _page_closed(page):
            raise WebAIBridgeUnavailable("网页标签页已关闭，请重新点击启动浏览器")
        if is_aistudio_node(node):
            model, thinking = web_model_settings(node)
            signature = (str(node.web_url or ""), model, thinking)
            if getattr(self, "_prepared_signature", None) != signature:
                self._prepare_page(node, min(30_000, timeout_ms))
            driver = AistudioPageDriver(
                page,
                model=model,
                thinking=thinking,
            )
            baseline = driver.reply_snapshot()
            driver.fill_prompt(prompt)
            driver.run()
            return driver.wait_for_reply(
                prompt_text=prompt,
                timeout_seconds=max(30.0, timeout_ms / 1000.0),
                poll_seconds=max(0.5, float(node.web_poll_interval_sec or 1.5)),
                baseline_texts=baseline,
            )

        selectors = merged_selectors(node)
        current_url = str(getattr(page, "url", "") or "")
        if not current_url or current_url == "about:blank":
            if node.web_url:
                page.goto(node.web_url, timeout=self.navigation_timeout_ms)
        if selectors.get("clear"):
            _click_locator(page, selectors["clear"])
        input_selector = selectors.get("input") or 'textarea, [contenteditable="true"]'
        input_locator = _first_visible(page, input_selector, timeout_ms=1500)
        if input_locator is None:
            raise WebAIBridgeUnavailable(
                "找不到网页输入框；请检查预设或节点 selectors.input"
            )
        _fill_prompt(page, input_locator, prompt, timeout_ms=15_000)
        try:
            input_locator.focus(timeout=3000)
        except Exception:
            pass
        send_mode = str(selectors.get("send_mode") or node.send_mode or "enter")
        if send_mode == "click":
            if not _click_locator(page, selectors.get("send", "")):
                raise WebAIBridgeUnavailable("找不到网页发送按钮；请检查 selectors.send")
        elif send_mode == "ctrl_enter":
            page.keyboard.press("Control+Enter")
        else:
            page.keyboard.press("Enter")
        return self._poll_response(page, node, selectors, timeout_ms)

    def _poll_response(
        self,
        page: Any,
        node: EndpointNode,
        selectors: Mapping[str, str],
        timeout_ms: int,
    ) -> str:
        poll_seconds = (
            max(0.5, float(node.web_poll_interval_sec or 1.5))
            if node.node_type == "WEB_AI_PLAYWRIGHT"
            else max(1, int(node.poll_interval_sec or 20))
        )
        deadline = time.monotonic() + max(5.0, timeout_ms / 1000.0)
        response_selector = selectors.get("response") or "body"
        done_selector = selectors.get("done") or ""
        last = ""
        stable = 0
        while time.monotonic() < deadline:
            time.sleep(poll_seconds)
            if _page_closed(page):
                raise WebAIBridgeUnavailable("网页在生成回复时被关闭")
            text = _read_response(page, response_selector)
            if done_selector and _first_visible(page, done_selector, timeout_ms=300) is not None:
                if text:
                    return text
            generating = _looks_generating(page, selectors, last, text)
            if text and text == last and not generating:
                stable += 1
                if stable >= 2:
                    return text
            else:
                stable = 0
            last = text or last
        if last:
            return last
        raise WebAIBridgeUnavailable("网页 AI 未返回可识别的内容（超时）")

    def prepare_login(self, node: EndpointNode, timeout_ms: int) -> None:
        if not self.alive:
            raise WebAIBridgeUnavailable("浏览器会话已关闭，请重新启动浏览器")
        event = threading.Event()
        result: dict[str, Any] = {}
        self._commands.put(
            {
                "type": "prepare_login",
                "node": node,
                "timeout_ms": timeout_ms,
                "event": event,
                "result": result,
            }
        )
        if not event.wait(max(10.0, timeout_ms / 1000.0 + 60.0)):
            raise WebAIBridgeUnavailable("等待登录/模型配置超时")
        if result.get("error"):
            raise WebAIBridgeUnavailable(str(result["error"]))

    def ask(self, node: EndpointNode, prompt: str, timeout_ms: int) -> str:
        if not self.alive:
            raise WebAIBridgeUnavailable("浏览器会话已关闭，请重新启动浏览器")
        event = threading.Event()
        result: dict[str, Any] = {}
        self._commands.put(
            {
                "type": "ask",
                "node": node,
                "prompt": prompt,
                "timeout_ms": timeout_ms,
                "event": event,
                "result": result,
            }
        )
        if not event.wait(max(10.0, timeout_ms / 1000.0 + 60.0)):
            raise WebAIBridgeUnavailable("等待网页 AI 回复超时")
        if result.get("error"):
            raise WebAIBridgeUnavailable(str(result["error"]))
        return str(result.get("answer") or "")

    def bring_to_front(self) -> None:
        self._commands.put({"type": "open"})

    def close(self) -> None:
        if not self.alive:
            return
        self._closed.set()
        self._commands.put({"type": "close"})
        self._thread.join(timeout=5.0)


def close_all_sessions() -> None:
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for session in sessions:
        try:
            session.close()
        except Exception:
            pass


atexit.register(close_all_sessions)


class WebAIBridge:
    def __init__(
        self,
        workspace: str | Path,
        *,
        playwright_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.workspace = Path(workspace)
        self.profile_root = self.workspace / "web_ai_profiles"
        self.profile_root.mkdir(parents=True, exist_ok=True)
        self.playwright_factory = playwright_factory
        self.sleep = sleep or time.sleep

    def profile_dir(self, node: EndpointNode) -> Path:
        target = Path(node.profile_dir) if node.profile_dir else self.profile_root / node.node_id
        if not target.is_absolute():
            target = self.workspace / target
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _playwright(self):
        if self.playwright_factory is not None:
            return self.playwright_factory()
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise WebAIBridgeUnavailable(
                "Playwright 未安装。请执行 `python -m playwright install chromium`，"
                "或使用半自动剪贴板模式。"
            ) from exc
        return sync_playwright()

    def _ensure_session(
        self,
        node: EndpointNode,
        *,
        headless: bool,
        force_headed: bool = False,
        navigation_timeout_ms: int = 120_000,
    ) -> _PersistentBrowserSession:
        profile = self.profile_dir(node)
        key = (
            f"{profile.resolve().as_posix().lower()}::{node.web_url}"
            f"::{int(node.web_cdp_port or 9333)}"
        )
        with _SESSIONS_LOCK:
            existing = _SESSIONS.get(key)
            if existing is not None and existing.usable():
                return existing
            if existing is not None:
                existing.close()
                _SESSIONS.pop(key, None)
            session = _PersistentBrowserSession(
                key,
                profile,
                node,
                headless=headless,
                navigation_timeout_ms=navigation_timeout_ms,
            )
            try:
                session.start()
            except Exception:
                _SESSIONS.pop(key, None)
                raise
            _SESSIONS[key] = session
            return session

    def open_login(self, node: EndpointNode, *, timeout_ms: int = 300_000) -> Path:
        """Launch a headed, persistent browser and keep it open for model setup."""
        if not node.web_url:
            raise WebAIBridgeUnavailable("该网页节点未配置 web_url")
        profile = self.profile_dir(node)
        session = self._ensure_session(
            node,
            headless=False,
            force_headed=True,
            navigation_timeout_ms=timeout_ms,
        )
        session.bring_to_front()
        session.prepare_login(node, timeout_ms)
        return profile

    def ask(
        self,
        node: EndpointNode,
        prompt: str,
        *,
        timeout_ms: int = 300_000,
    ) -> str:
        """Send through the resident page without reloading its model settings."""
        if not node.web_url:
            raise WebAIBridgeUnavailable("该网页节点未配置 web_url")
        selectors = merged_selectors(node)
        headless = _truthy(selectors.get("headless"), default=True)
        session = self._ensure_session(
            node,
            headless=headless,
            navigation_timeout_ms=120_000,
        )
        return session.ask(node, prompt, timeout_ms)

    def ask_json(
        self,
        node: EndpointNode,
        prompt: str,
        *,
        timeout_ms: int = 300_000,
    ) -> Any:
        return _extract_json_payload(self.ask(node, prompt, timeout_ms=timeout_ms))

    def close(self, node: EndpointNode) -> None:
        profile = self.profile_dir(node)
        key = f"{profile.resolve().as_posix().lower()}::{node.web_url}"
        with _SESSIONS_LOCK:
            session = _SESSIONS.pop(key, None)
        if session is not None:
            session.close()


@dataclass
class ClipboardPacket:
    batch: DynamicBatch
    prompt: str
    node_id: str
    item_bundles: dict[str, ContextBundle] = field(default_factory=dict)


def _extract_json_payload(text: str) -> Any:
    body = str(text or "").strip()
    try:
        value = json.loads(body, strict=False)
        if isinstance(value, (dict, list)):
            return value
    except Exception:
        pass

    repaired_body = repair_json_text(body)
    try:
        value = json.loads(repaired_body, strict=False)
        if isinstance(value, (dict, list)):
            return value
    except Exception:
        pass

    candidates: list[Any] = []
    for block in re.findall(r"```(?:json)?\s*(.+?)```", body, re.S | re.I):
        try:
            value = json.loads(block.strip(), strict=False)
            if isinstance(value, (dict, list)):
                candidates.append(value)
                continue
        except Exception:
            pass
        try:
            value = json.loads(repair_json_text(block.strip()), strict=False)
            if isinstance(value, (dict, list)):
                candidates.append(value)
        except Exception:
            pass

    decoder = json.JSONDecoder(strict=False)
    # Scan left-to-right on repaired_body so an outer {"items": [...]} wins over a nested card
    # object that happens to appear later in the text.
    for start, character in enumerate(repaired_body):
        if character not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(repaired_body[start:])
        except Exception:
            continue
        if isinstance(value, (dict, list)):
            candidates.append(value)
    for value in candidates:
        if isinstance(value, dict) and "items" in value:
            return value
    for value in candidates:
        if isinstance(value, dict) and "cards" in value:
            return value
    for value in candidates:
        if isinstance(value, list) and (not value or any(isinstance(x, (dict, list)) for x in value)):
            return value
    if candidates:
        return candidates[0]
    raise ValueError("网页回复中没有 JSON 对象或数组")


def _sanitize_card_dict(raw_card: Mapping[str, Any]) -> dict[str, Any]:
    card = dict(raw_card)
    # 1. title_zh mapping from title / title_en with smart fallback
    if not card.get("title_zh"):
        candidate = str(card.get("title") or card.get("title_en") or "").strip()
        if not candidate:
            summary = str(card.get("summary_zh") or card.get("summary") or "").strip()
            if summary:
                clean_summary = re.sub(r"\s+", " ", summary).strip()
                first_sent = re.split(r"[。！？]", clean_summary)[0].strip()
                if first_sent:
                    candidate = first_sent[:45] + ("..." if len(first_sent) > 45 else "")
            if not candidate:
                ion = str(card.get("ion_stage") or "").strip()
                proc = str(card.get("physical_process") or "").strip()
                if ion or proc:
                    candidate = f"{ion} {proc} 提炼卡片".strip()
            if not candidate:
                candidate = "原子物理计算特性提炼卡片"
        card["title_zh"] = candidate

    # 2. card_type normalization to valid CARD_TYPES (STRAT, MODEL, CODE, PITFALL, CONC)
    ctype = str(card.get("card_type") or "").upper().strip()
    if any(k in ctype for k in ("METHODOLOGY", "METHOD", "STRAT", "STRATEGY", "策略", "方法", "流程")):
        card["card_type"] = "STRAT"
    elif any(k in ctype for k in ("FORMULA", "MODEL", "METHOD_BOUNDARY", "BOUNDARY", "模型", "势函数", "算符")):
        card["card_type"] = "MODEL"
    elif any(k in ctype for k in ("CODE", "SNIPPET", "SCRIPT", "PROGRAM", "ALGORITHM", "代码", "程序", "算法", "公式")):
        card["card_type"] = "CODE"
    elif any(k in ctype for k in ("PITFALL", "WARNING", "CAUTION", "TRAP", "ERROR", "避坑", "陷阱", "失效")):
        card["card_type"] = "PITFALL"
    else:
        card["card_type"] = "CONC"

    # 3. z_num normalization
    card["z_num"] = sanitize_z_num(card.get("z_num"))

    # 4. formulas_latex: list of LaTeX strings -> newline separated string
    flatex = card.get("formulas_latex")
    if isinstance(flatex, list):
        card["formulas_latex"] = "\n".join(str(f) for f in flatex if str(f).strip())
    elif flatex is None:
        card["formulas_latex"] = ""
    else:
        card["formulas_latex"] = str(flatex)

    # 5. generalized_conjecture into structured_details and string coercion
    gen_conj = card.get("generalized_conjecture")
    if gen_conj:
        struct = dict(card.get("structured_details") or {})
        if isinstance(gen_conj, Mapping):
            struct["generalized_conjecture"] = json.dumps(gen_conj, ensure_ascii=False)
            parts = []
            stmt = gen_conj.get("statement_zh") or gen_conj.get("statement")
            if stmt:
                parts.append(str(stmt).strip())
            basis = gen_conj.get("physical_basis_zh") or gen_conj.get("physical_basis")
            if basis:
                parts.append(f"【物理基础】: {str(basis).strip()}")
            bnd = gen_conj.get("boundary_zh") or gen_conj.get("boundary")
            if bnd:
                parts.append(f"【适用边界】: {str(bnd).strip()}")
            card["generalized_conjecture"] = "\n".join(parts) if parts else json.dumps(gen_conj, ensure_ascii=False)
        else:
            struct["generalized_conjecture"] = str(gen_conj).strip()
            card["generalized_conjecture"] = str(gen_conj).strip()
        card["structured_details"] = struct

    # 6. keywords: convert dict list or objects to string list
    kws = card.get("keywords") or card.get("keywords_en")
    if isinstance(kws, list):
        clean_kws = []
        for kw in kws:
            if isinstance(kw, str) and kw.strip():
                clean_kws.append(kw.strip())
            elif isinstance(kw, Mapping):
                term = str(kw.get("term_en") or kw.get("term_zh") or kw.get("term") or "").strip()
                if term:
                    clean_kws.append(term)
        card["keywords"] = clean_kws

    # 7. conditions: coerce dict/list/int values to strings
    cond = card.get("conditions")
    if isinstance(cond, Mapping):
        clean_cond = dict(cond)
        for k in (
            "target_quantity", "unit", "value", "uncertainty",
            "state_initial", "state_final", "transition",
            "j_parity", "method_boundary", "convergence", "applicability"
        ):
            v = clean_cond.get(k)
            if v is not None and not isinstance(v, str):
                if isinstance(v, (dict, list)):
                    clean_cond[k] = json.dumps(v, ensure_ascii=False)
                else:
                    clean_cond[k] = str(v)
        card["conditions"] = clean_cond

    return card


def _sanitize_result_payload(raw_res: Mapping[str, Any]) -> dict[str, Any]:
    res = dict(raw_res)
    # 1. document_year
    y = res.get("document_year")
    if isinstance(y, str):
        m = re.search(r"\b(19\d\d|20\d\d)\b", y)
        res["document_year"] = int(m.group(1)) if m else None
    elif isinstance(y, int) and 1000 <= y <= 2999:
        res["document_year"] = y
    else:
        res["document_year"] = None

    # 2. keywords
    kw_list = res.get("keywords")
    if isinstance(kw_list, list):
        new_kws = []
        for kw in kw_list:
            if isinstance(kw, str) and kw.strip():
                new_kws.append({"term_en": kw.strip()})
            elif isinstance(kw, dict):
                new_kws.append(kw)
        res["keywords"] = new_kws

    # 3. cards wrap & sanitize
    cards = res.get("cards")
    if isinstance(cards, list):
        res["cards"] = [_sanitize_card_dict(c) for c in cards if isinstance(c, Mapping)]
    elif isinstance(cards, Mapping):
        res["cards"] = [_sanitize_card_dict(cards)]
    elif not res.get("skip"):
        card_keys = {
            "card_type",
            "title_zh",
            "title",
            "summary_zh",
            "premises_and_conditions",
        }
        if card_keys.intersection(res):
            card_obj = _sanitize_card_dict({k: v for k, v in res.items() if k != "skip"})
            res["cards"] = [card_obj]

    return res


class ClipboardBatchService:
    """Render a batch into a copyable prompt and apply pasted JSON replies."""

    def __init__(self, packer: DynamicBatchPacker | None = None):
        self.packer = packer or DynamicBatchPacker(system_tokens=0, global_premise_tokens=0)

    def build_packets(
        self,
        items: Iterable[DistilledItem],
        node: EndpointNode,
        *,
        item_bundles: Mapping[str, ContextBundle] | None = None,
        system_prompt: str = "",
        global_premise: str = "",
        shared_refs_map: Mapping[str, str] | None = None,
    ) -> list[ClipboardPacket]:
        """Render every batch that fits the node into its own copyable prompt."""
        queue = list(items)
        batches = self.packer.checkout_batches(
            queue,
            node,
            shared_refs_map=shared_refs_map,
        )
        if not batches:
            raise ValueError("没有可装入当前节点的待处理片段")
        bundles = dict(item_bundles or {})
        packets: list[ClipboardPacket] = []
        for batch in batches:
            blocks = []
            batch_tables: list[str] = []
            seen_tables: set[str] = set()

            for item in batch.items:
                metadata = item.metadata or {}
                doc_id = str(metadata.get("doc_id") or "")
                title = str(metadata.get("title") or "")
                header = ""
                if doc_id:
                    header = f"【文献】{doc_id}"
                    if title:
                        header += f" · {title}"
                    header += "\n"

                item_raw_text = item.text.strip()
                item_bundle = metadata.get("bundle") or bundles.get(item.item_id)
                t_md = ""
                if isinstance(item_bundle, dict):
                    t_md = str(item_bundle.get("table_markdown") or "").strip()
                elif hasattr(item_bundle, "table_markdown"):
                    t_md = str(getattr(item_bundle, "table_markdown") or "").strip()

                if t_md:
                    if t_md not in seen_tables:
                        seen_tables.add(t_md)
                        batch_tables.append(t_md)
                    table_idx = batch_tables.index(t_md) + 1
                    if "## 表格数据 (压缩脱水版)" in item_raw_text:
                        item_raw_text = re.sub(
                            r"## 表格数据 \(压缩脱水版\)\n.*",
                            f"## 表格调用说明\n（本条目调用数据表，详见文末【参考表格附件】[表格附件 {table_idx}]）",
                            item_raw_text,
                            flags=re.S,
                        )
                    else:
                        item_raw_text += f"\n\n## 表格调用说明\n（本条目调用数据表，详见文末【参考表格附件】[表格附件 {table_idx}]）"

                blocks.append(
                    f"### ITEM {item.item_id}\n{header}{item_raw_text}"
                )

            # 1. 任务与输出规范块（格式与输出声明放一块）
            spec_parts = []
            if system_prompt.strip():
                spec_parts.append(system_prompt.strip())
            if global_premise.strip():
                spec_parts.append(global_premise.strip())
            spec_parts.extend([
                "【任务与输出规范】",
                "请仔细阅读后续【具体提问条目】" + ("及文末【参考表格附件】" if batch_tables else "") + "，提炼原子物理计算数据与模型信息。",
                "【输出要求】",
                "请输出严格 JSON：{\"items\":[{\"item_id\":\"...\",\"result\":{...ExtractionResult...}}]}。",
                "特别提醒：JSON 内部的所有 LaTeX 反斜杠必须写为双反斜杠（如 \\\\frac, \\\\langle, \\\\beta 而非单个 \\），只输出纯 JSON，不要 Markdown 解释。",
            ])
            spec_section = "\n\n".join(spec_parts)

            # 2. 具体提问条目块（具体提问条目放另一块）
            items_section = "【具体提问条目】\n\n" + "\n\n".join(blocks)

            prompt_sections = [spec_section, items_section]

            # 3. 参考表格附件块（如果还有表格，表格再放另一块）
            if batch_tables:
                table_blocks = []
                for idx, table_content in enumerate(batch_tables, 1):
                    table_blocks.append(f"### [表格附件 {idx}] (已采用骨架压缩脱水)\n{table_content}")
                tables_section = "【参考表格附件 (按需调用)】\n\n" + "\n\n---\n\n".join(table_blocks)
                prompt_sections.append(tables_section)

            prompt = "\n\n".join(prompt_sections)
            packets.append(
                ClipboardPacket(
                    batch=batch,
                    prompt=prompt,
                    node_id=node.node_id,
                    item_bundles=bundles,
                )
            )
        return packets

    def build_packet(
        self,
        items: Iterable[DistilledItem],
        node: EndpointNode,
        *,
        item_bundles: Mapping[str, ContextBundle] | None = None,
        system_prompt: str = "",
        global_premise: str = "",
        shared_refs_map: Mapping[str, str] | None = None,
    ) -> ClipboardPacket:
        packets = self.build_packets(
            items,
            node,
            item_bundles=item_bundles,
            system_prompt=system_prompt,
            global_premise=global_premise,
            shared_refs_map=shared_refs_map,
        )
        return packets[0]

    def parse_reply(self, text: str) -> dict[str, ExtractionResult]:
        payload = _extract_json_payload(text)
        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict) and "items" in payload:
            entries = payload.get("items") or []
        elif isinstance(payload, dict) and "cards" in payload:
            entries = [{"item_id": "", "result": payload}]
        else:
            raise ValueError("回复 JSON 缺少 items 或 cards")
        results: dict[str, ExtractionResult] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            item_id = str(entry.get("item_id") or "")
            result_payload = entry.get("result", entry)
            if not isinstance(result_payload, Mapping):
                continue
            sanitized_payload = _sanitize_result_payload(result_payload)
            try:
                parsed = ExtractionResult.model_validate(sanitized_payload)
            except Exception:
                continue
            if not parsed.skip and not parsed.cards:
                continue
            results[item_id] = parsed
        if not results:
            raise ValueError("回复中没有可解析的 ExtractionResult")
        return results

    def apply_reply(
        self,
        pipeline: Any,
        document: Mapping[str, Any],
        packet: ClipboardPacket,
        text: str,
    ) -> dict[str, Any]:
        results = self.parse_reply(text)
        bundles = packet.item_bundles
        stored = 0
        for item in packet.batch.items:
            result = results.get(item.item_id)
            if result is None:
                continue
            bundle = bundles.get(item.item_id)
            if bundle is None:
                continue
            for draft in result.cards:
                if pipeline._store_draft(draft, document, bundle, result.keywords):
                    stored += 1
        cards = len(pipeline.db.cards_for_document(str(document["doc_id"])))
        if cards:
            pipeline.db.update_parse_status(str(document["doc_id"]), "LLM_EXTRACTED")
        return {"stored": stored, "items": len(results), "cards": cards}


__all__ = [
    "WebAIBridgeUnavailable",
    "WebAIBridge",
    "ClipboardPacket",
    "ClipboardBatchService",
    "build_document_packet",
]


def build_document_packet(
    db: Any,
    workspace: str | Path,
    document: Mapping[str, Any],
    pipeline: Any,
    *,
    node: EndpointNode | None = None,
    system_prompt: str = "",
) -> tuple[ClipboardBatchService, ClipboardPacket]:
    """Build one semi-auto packet for a document's un-extracted bundles."""
    workspace = Path(workspace)
    local = document.get("local_pdf_path")
    if not local:
        raise ValueError("这篇文献还没有本地 PDF")
    pdf = Path(str(local))
    if not pdf.is_absolute():
        pdf = workspace / pdf
    bundles_path = pdf.parent / "bundles.json"
    if not bundles_path.is_file():
        raise ValueError("尚未生成证据包，请先解析文献")
    raw = json.loads(bundles_path.read_text(encoding="utf-8"))
    doc_id = str(document["doc_id"])
    stored = db.bundle_hashes_for_document(doc_id)
    items: list[DistilledItem] = []
    bundles_by_item: dict[str, ContextBundle] = {}
    for index, item in enumerate(raw):
        try:
            bundle = ContextBundle.model_validate(item)
        except Exception:
            continue
        if bundle_hash(bundle) in stored:
            continue
        item_id = f"{doc_id}-{index}"
        items.append(DistilledItem(item_id=item_id, text=bundle.prompt_markdown()))
        bundles_by_item[item_id] = bundle
    if not items:
        raise ValueError("这篇文献没有尚未提炼的证据包")
    pool = getattr(getattr(pipeline, "gateway", None), "pool", None)
    node = node or (pool.node_for_role("fast_llm") if pool is not None else None)
    if node is None:
        node = EndpointNode(
            node_id="semi-auto-clipboard",
            name="半自动剪贴板",
            node_type="SEMI_AUTO_CLIPBOARD",
            capabilities=["TEXT_LLM"],
            max_input_tokens=5000,
            max_output_tokens=4000,
            max_items_per_batch=6,
        )
    system = system_prompt or CARD_SYSTEM_PROMPT.replace("{doc_id}", doc_id)
    if str(document.get("doc_type") or "").upper() in ("MAN", "BOK"):
        system += "\n\n" + MANUAL_MODE_INSTRUCTION
    packer = DynamicBatchPacker(system_prompt=system)
    service = ClipboardBatchService(packer)
    packet = service.build_packet(
        items,
        node,
        item_bundles=bundles_by_item,
        system_prompt=system,
    )
    return service, packet
