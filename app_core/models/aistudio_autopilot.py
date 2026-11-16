"""Google AI Studio automation helpers for the web-AI bridge.

This module adapts the proven real-browser CDP approach used by the
AIPowerpointGenerator project: launch the user's installed Chrome/Edge with a
dedicated profile and remote-debugging port, attach with Playwright over CDP,
wait for the Angular editor and Google login, select the requested model /
Thinking level, submit a prompt, and wait until the Stop button disappears
before accepting a JSON reply.

It intentionally does not close the browser on disconnect.  The login profile
and the model selected in the right-hand Run settings panel survive between
calls.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

AI_STUDIO_NEW_CHAT = "https://aistudio.google.com/prompts/new_chat"
ProgressFn = Callable[[str], None]


class AutopilotError(RuntimeError):
    pass


@dataclass
class BrowserHandle:
    playwright: Any
    browser: Any
    context: Any
    page: Any
    launched_process: Optional[subprocess.Popen] = None


def find_browser_executable() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if Path(path).is_file():
            return path
    raise AutopilotError("未找到本机 Chrome 或 Edge，请先安装 Chrome/Edge")


def _cdp_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}/json/version", timeout=1.5
        ) as response:
            return response.status == 200
    except Exception:
        return False


def launch_or_connect_browser(
    profile_dir: str | Path,
    *,
    cdp_port: int = 9333,
    start_url: str = AI_STUDIO_NEW_CHAT,
    timeout: float = 25.0,
    executable: str | None = None,
    proxy_url: str = "",
) -> BrowserHandle:
    """Connect to a real Chrome/Edge over CDP, launching it if necessary.

    Google's login flow is much more reliable in a real installed browser than
    in Playwright's bundled Chromium.  The browser process is not terminated
    when Playwright disconnects, so the user can keep the page and model
    settings open.
    """
    from playwright.sync_api import sync_playwright

    profile = Path(profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    port = int(cdp_port or 9333)
    playwright = sync_playwright().start()
    endpoint = f"http://127.0.0.1:{port}"
    try:
        browser = playwright.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[-1] if context.pages else context.new_page()
        if start_url and "aistudio.google.com" not in str(page.url or ""):
            page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        return BrowserHandle(playwright, browser, context, page)
    except Exception:
        logger.info("CDP 端口 %s 未连接，尝试启动本机浏览器", port)

    exe = executable or find_browser_executable()
    command = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
    ]
    if proxy_url:
        command.append(f"--proxy-server={proxy_url}")
    command.append(start_url)
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + max(5.0, float(timeout))
    browser = None
    while time.monotonic() < deadline:
        if _cdp_ready(port):
            try:
                browser = playwright.chromium.connect_over_cdp(endpoint)
                break
            except Exception:
                pass
        time.sleep(0.4)
    if browser is None:
        playwright.stop()
        raise AutopilotError(
            f"浏览器 CDP 端口 {port} 未就绪。请关闭旧的调试浏览器后重试。"
        )
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.pages[-1] if context.pages else context.new_page()
    if start_url and "aistudio.google.com" not in str(page.url or ""):
        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
    return BrowserHandle(playwright, browser, context, page, process)


def close_browser(handle: Optional[BrowserHandle]) -> None:
    """Disconnect Playwright but leave the user's browser open."""
    if handle is None:
        return
    try:
        handle.playwright.stop()
    except Exception:
        pass


def _first_visible(page: Any, selectors: Sequence[str], timeout: float = 0.4) -> Any:
    timeout_ms = max(50.0, min(150.0, float(timeout) * 1000.0))
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count() and locator.first.is_visible(timeout=timeout_ms):
                return locator.first
        except Exception:
            continue
    return None


def _wait_for_first_visible(
    page: Any,
    selectors: Sequence[str],
    timeout: float = 30.0,
    poll: float = 0.5,
) -> Any:
    deadline = time.monotonic() + max(0.1, timeout)
    while True:
        locator = _first_visible(page, selectors, timeout=min(1.0, poll + 0.5))
        if locator is not None:
            return locator
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def _click_first(page: Any, selectors: Sequence[str], timeout: float = 3.0) -> bool:
    locator = _first_visible(page, selectors, timeout=timeout)
    if locator is None:
        return False
    try:
        locator.click(timeout=timeout * 1000)
        return True
    except Exception:
        return False


def find_run_button(page: Any, timeout: float = 30.0) -> Any:
    """Find the real bottom Run button, never "Rerun this turn" or settings."""
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        try:
            candidates = page.locator("button, [role='button']")
            count = int(candidates.count())
        except Exception:
            count = 0
        for index in range(count):
            item = candidates.nth(index)
            try:
                if not item.is_visible():
                    continue
                text = str(item.inner_text(timeout=500) or "").strip()
                aria = str(item.get_attribute("aria-label", timeout=500) or "").strip()
                label = f"{text} {aria}".strip().lower()
                if not label.startswith("run"):
                    continue
                if label.startswith("rerun") or "settings" in label or "running" in label:
                    continue
                if item.is_disabled():
                    continue
                return item
            except Exception:
                continue
        time.sleep(0.4)
    return None


def _emit(progress: Optional[ProgressFn], message: str) -> None:
    if progress is None:
        return
    try:
        progress(message)
    except Exception:
        logger.debug("autopilot progress callback failed", exc_info=True)


def _body_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=2000))
    except Exception:
        return ""


def ensure_aistudio_logged_in(
    page: Any,
    *,
    timeout_seconds: float = 300.0,
    progress: Optional[ProgressFn] = None,
) -> None:
    """Wait for Google login and for the Angular editor/model entry to render."""
    deadline = time.monotonic() + timeout_seconds
    told_login = False
    told_loading = False
    saw_login = False
    while time.monotonic() < deadline:
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        body = _body_text(page)
        need_login = (
            "accounts.google.com" in url
            or "Sign in to continue" in body
            or ("Sign in" in body and "Start typing" not in body)
        )
        if need_login:
            saw_login = True
            if not told_login:
                told_login = True
                _emit(
                    progress,
                    "🔐 请在浏览器中登录 Google 账号；登录完成后程序会自动继续…",
                )
            time.sleep(2.0)
            continue
        for selector in (
            "button:has-text('gemini-4-argon')",
            "button:has-text('gemini-')",
            "button:has-text('Gemini')",
            "textarea:visible",
            "button:has-text('Run settings')",
        ):
            try:
                locator = page.locator(selector)
                if locator.count() and locator.first.is_visible():
                    return
            except Exception:
                continue
        if not told_loading:
            told_loading = True
            _emit(progress, "⏳ AI Studio 页面仍在加载，等待模型面板出现…")
        time.sleep(1.0)
    if saw_login:
        raise AutopilotError(
            "等待 Google 登录超时。请先在浏览器中登录 AI Studio 后重试。"
        )
    raise AutopilotError("等待 AI Studio 页面加载超时，请检查网络后重试。")


def _open_model_panel(page: Any, timeout_seconds: float = 90.0) -> Any:
    carousel_selector = "ms-model-carousel[data-test-id='model-carousel-in-selector']"
    carousel = _wait_for_first_visible(page, [carousel_selector], timeout=2.0)
    if carousel is not None:
        return carousel
    entries = [
        "button:has-text('gemini-4-argon')",
        "button:has-text('Gemini 4 Argon')",
        "button:has-text('gemini-')",
        "button:has-text('Gemini')",
    ]
    entry = _wait_for_first_visible(page, entries, timeout=timeout_seconds)
    if entry is None:
        settings = _wait_for_first_visible(
            page,
            ["button:has-text('Run settings')", "[role='button']:has-text('Run settings')"],
            timeout=5.0,
        )
        if settings is not None:
            try:
                settings.click(timeout=5000)
            except Exception:
                pass
            entry = _wait_for_first_visible(page, entries, timeout=30.0)
    if entry is None:
        raise AutopilotError("找不到 AI Studio 模型选择入口，页面可能尚未加载完成")
    for _attempt in range(3):
        entry = _wait_for_first_visible(page, entries, timeout=15.0)
        if entry is None:
            break
        try:
            entry.click(timeout=8000)
        except Exception:
            time.sleep(1.0)
            continue
        carousel = _wait_for_first_visible(page, [carousel_selector], timeout=30.0)
        if carousel is not None:
            return carousel
        time.sleep(1.0)
    raise AutopilotError("找不到 AI Studio 模型选择面板")


def configure_aistudio(
    page: Any,
    target_model: str,
    thinking_level: str,
    *,
    progress: Optional[ProgressFn] = None,
) -> None:
    """Select the target model and Thinking level in Run settings."""
    target_model = str(target_model or "").strip()
    thinking_level = str(thinking_level or "").strip()
    if not target_model and not thinking_level:
        return
    _emit(
        progress,
        f"🤖 正在选择模型 {target_model or '当前模型'} / "
        f"Thinking {thinking_level or '当前档位'}…",
    )
    if target_model:
        carousel = _open_model_panel(page)
        slug = target_model.lower().replace(" ", "-")
        card = None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            for selector in (
                f"button:has-text('{target_model}')",
                f"button:has-text('{slug}')",
            ):
                try:
                    candidates = carousel.locator(selector)
                    for index in range(min(candidates.count(), 8)):
                        candidate = candidates.nth(index)
                        if candidate.is_visible():
                            card = candidate
                            break
                except Exception:
                    continue
                if card is not None:
                    break
            if card is not None:
                break
            time.sleep(0.3)
        if card is None:
            raise AutopilotError(f"模型选择面板中找不到目标模型：{target_model}")
        card.click(timeout=8000)
        try:
            page.locator(
                "ms-model-carousel[data-test-id='model-carousel-in-selector']"
            ).first.wait_for(state="hidden", timeout=8000)
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(500)

    if thinking_level:
        thinking = _wait_for_first_visible(
            page,
            ["mat-select[aria-label*='Thinking' i]"],
            timeout=30.0,
        )
        if thinking is not None:
            try:
                current = thinking.inner_text(timeout=1000).strip()
                if current.casefold() != thinking_level.casefold():
                    thinking.click(timeout=5000)
                    option = _wait_for_first_visible(
                        page,
                        [
                            f"[role='option']:has-text('{thinking_level}')",
                            f"mat-option:has-text('{thinking_level}')",
                        ],
                        timeout=10.0,
                    )
                    if option is None:
                        raise AutopilotError(
                            f"找不到 Thinking 选项：{thinking_level}"
                        )
                    option.click(timeout=5000)
                    page.wait_for_timeout(500)
            except AutopilotError:
                raise
            except Exception as exc:
                logger.warning("设置 Thinking 档位失败: %s", exc)


_GENERATION_STOP_SELECTORS = [
    "button:has-text('Stop')",
    "[role='button']:has-text('Stop')",
    "button[aria-label*='Stop' i]",
    "[role='button'][aria-label*='Stop' i]",
]

_MODEL_REPLY_SELECTORS = [
    "model-response",
    "[data-turn-role='model']",
    "[data-test-id*='model-response']",
    "ms-chat-turn[data-turn-role='model']",
    "ms-chat-turn[data-test-id*='model-response']",
]
_MODEL_CODE_SELECTORS = [
    "model-response pre code",
    "model-response code",
    "[data-turn-role='model'] pre code",
    "[data-turn-role='model'] code",
    "ms-chat-turn[data-turn-role='model'] pre code",
]


def generation_running(page: Any) -> bool:
    try:
        return _first_visible(page, _GENERATION_STOP_SELECTORS, timeout=0.3) is not None
    except Exception:
        return False


def _iter_locator_texts(page: Any, selectors: Sequence[str], tail: int = 4) -> list[str]:
    texts: list[str] = []
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = int(locator.count())
            for index in range(max(0, count - tail), count):
                try:
                    value = locator.nth(index).inner_text(timeout=1500)
                except Exception:
                    continue
                if value and value.strip():
                    texts.append(str(value))
        except Exception:
            continue
    return texts


def _dedupe(texts: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for text in texts:
        key = re.sub(r"\s+", "", str(text))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(str(text))
    return result


def model_reply_candidates(page: Any) -> list[str]:
    code = _iter_locator_texts(page, _MODEL_CODE_SELECTORS)
    full = _iter_locator_texts(page, _MODEL_REPLY_SELECTORS)
    return _dedupe(code + full)


def is_prompt_echo(text: str, prompt: str) -> bool:
    if not text or not prompt:
        return False
    compact_text = re.sub(r"\s+", "", text)
    compact_prompt = re.sub(r"\s+", "", prompt)
    if len(compact_text) >= 64 and compact_text in compact_prompt:
        return True
    # The extraction prompt repeats JSON schema/instruction text.  A long
    # common prefix is a strong sign that the user turn was captured.
    return bool(
        len(compact_text) >= 64
        and compact_text[:64] in compact_prompt
    )


def configure_and_wait(
    page: Any,
    *,
    model: str,
    thinking: str,
    timeout_seconds: float = 300.0,
    progress: Optional[ProgressFn] = None,
) -> None:
    ensure_aistudio_logged_in(
        page, timeout_seconds=timeout_seconds, progress=progress
    )
    configure_aistudio(
        page, model, thinking, progress=progress
    )


class AistudioPageDriver:
    """Small page driver used by :class:`WebAIBridge` and its tests."""

    def __init__(
        self,
        page: Any,
        *,
        model: str = "Gemini 4 Argon",
        thinking: str = "High",
        progress: Optional[ProgressFn] = None,
    ):
        self.page = page
        self.model = model
        self.thinking = thinking
        self.progress = progress
        self._prepared = False
        self._input_locator: Any = None

    def prepare_new_chat(self) -> None:
        _emit(self.progress, "🤖 正在打开 Google AI Studio 新对话…")
        self.page.goto(
            AI_STUDIO_NEW_CHAT,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

    def ensure_logged_in(self, timeout_seconds: float = 300.0) -> None:
        ensure_aistudio_logged_in(
            self.page,
            timeout_seconds=timeout_seconds,
            progress=self.progress,
        )

    def reply_snapshot(self) -> list[str]:
        """Capture existing model replies before submitting a new prompt."""
        return model_reply_candidates(self.page)

    def configure(self) -> None:
        configure_aistudio(
            self.page,
            self.model,
            self.thinking,
            progress=self.progress,
        )
        self._prepared = True

    def fill_prompt(self, prompt: str) -> None:
        _emit(self.progress, "✍️ 正在填入提示词…")
        locator = _wait_for_first_visible(
            self.page,
            ["textarea:visible", "[contenteditable='true']:visible", "[role='textbox']:visible"],
            timeout=60.0,
        )
        if locator is None:
            raise AutopilotError("找不到 AI Studio 提示词输入框")
        self._input_locator = locator
        try:
            locator.click()
            locator.fill(prompt)
            return
        except Exception:
            pass
        try:
            locator.evaluate(
                """(el, text) => {
                    el.focus();
                    if (el.tagName === 'TEXTAREA') {
                        el.value = text;
                    } else {
                        el.textContent = text;
                    }
                    el.dispatchEvent(new InputEvent('input', {
                        bubbles: true, inputType: 'insertText', data: text
                    }));
                }""",
                prompt,
            )
        except Exception as exc:
            raise AutopilotError(f"填入提示词失败：{exc}") from exc

    def run(self) -> None:
        _emit(self.progress, "🚀 正在点击 Run，启动生成…")
        run_button = find_run_button(self.page, timeout=30.0)
        if run_button is not None:
            try:
                run_button.click(timeout=8000)
                return
            except Exception:
                logger.warning("点击 Run 失败，回退 Control+Enter", exc_info=True)
        # Do not use a broad button[aria-label*='Run'] selector here: AI Studio
        # has per-turn "Rerun this turn" buttons that would match it and rerun
        # an old answer instead of sending the newly filled prompt.
        try:
            if self._input_locator is not None:
                try:
                    self._input_locator.focus(timeout=3000)
                except Exception:
                    pass
            self.page.keyboard.press("Control+Enter")
        except Exception as exc:
            raise AutopilotError(f"无法触发 Run：{exc}") from exc

    def wait_for_reply(
        self,
        *,
        prompt_text: str,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 1.5,
        baseline_texts: Sequence[str] = (),
    ) -> str:
        started = time.monotonic()
        echo_seen = False
        baseline = {
            re.sub(r"\s+", "", str(text))
            for text in baseline_texts
            if str(text or "").strip()
        }
        while time.monotonic() - started < timeout_seconds:
            running = generation_running(self.page)
            candidates = model_reply_candidates(self.page)
            if not running:
                for text in candidates:
                    if re.sub(r"\s+", "", str(text)) in baseline:
                        continue
                    if is_prompt_echo(text, prompt_text):
                        if not echo_seen:
                            echo_seen = True
                            _emit(
                                self.progress,
                                "⏳ 已检测到提问词回显，继续等待模型真正生成回复…",
                            )
                        continue
                    if "{" not in text and "[" not in text:
                        continue
                    try:
                        # A complete JSON object/array is the only thing this
                        # application needs; partial streaming text is ignored.
                        from models.web_ai_bridge import _extract_json_payload

                        _extract_json_payload(text)
                    except Exception:
                        continue
                    _emit(self.progress, "✅ 模型回复已生成，正在提取 JSON…")
                    return text
            elapsed = int(time.monotonic() - started)
            _emit(
                self.progress,
                f"🧠 {'仍在生成' if running else '等待完整 JSON'}"
                f"（已用时 {elapsed}s）…",
            )
            time.sleep(max(1.0, float(poll_seconds)))
        raise AutopilotError(
            f"等待 Google AI Studio 回复超时（{timeout_seconds:.0f}s）"
        )


__all__ = [
    "AI_STUDIO_NEW_CHAT",
    "AutopilotError",
    "AistudioPageDriver",
    "BrowserHandle",
    "configure_aistudio",
    "configure_and_wait",
    "ensure_aistudio_logged_in",
    "find_browser_executable",
    "generation_running",
    "launch_or_connect_browser",
    "model_reply_candidates",
    "is_prompt_echo",
    "close_browser",
]
