from pathlib import Path
from types import SimpleNamespace

from models.endpoint_pool import EndpointNode, NODE_TYPE_LABELS, SEND_MODES
from models.web_ai_bridge import (
    WEB_AI_PRESETS,
    _PersistentBrowserSession,
    _extract_json_payload,
    browser_context_options,
    launch_persistent_browser,
    merged_selectors,
    preset_key_for_url,
    resolved_proxy_url,
)
from ui.model_pool_dialogs import EndpointNodeDialog


def _node(**overrides):
    values = {
        "node_id": "web-google",
        "name": "Google AI Studio",
        "node_type": "WEB_AI_PLAYWRIGHT",
        "capabilities": ["TEXT_LLM"],
        "web_url": WEB_AI_PRESETS["google_ai_studio"]["url"],
        "web_preset": "google_ai_studio",
        "send_mode": "ctrl_enter",
        "web_model": "Gemini 4 Argon",
        "thinking_level": "High",
        "model": "deepseek-flash",
    }
    values.update(overrides)
    return EndpointNode(**values)


def test_node_dialog_hides_irrelevant_fields_for_each_type(qapp):
    dialog = EndpointNodeDialog(workspace=".")
    dialog.show()
    qapp.processEvents()

    def select(node_type):
        dialog.node_type.setCurrentIndex(dialog.node_type.findData(node_type))
        qapp.processEvents()

    select("OPENAI_COMPATIBLE_API")
    assert dialog.base_url.isVisible()
    assert dialog.model.isVisible()
    assert dialog.api_key.isVisible()
    assert dialog.model_dim.isVisible()
    assert not dialog.web_url.isVisible()
    assert not dialog.model_path.isVisible()
    assert not dialog.submit_url.isVisible()
    assert dialog.api_test_button.isVisible()
    assert not dialog.launch_browser_button.isVisible()

    select("QUEUE_POLLING_API")
    assert dialog.submit_url.isVisible()
    assert dialog.status_url.isVisible()
    assert dialog.poll_section.isVisible()

    select("WEB_AI_PLAYWRIGHT")
    assert dialog.web_preset.isVisible()
    assert dialog.web_url.isVisible()
    assert dialog.web_model.isVisible()
    assert dialog.thinking_level.isVisible()
    assert dialog.web_cdp_port.isVisible()
    assert dialog.web_poll_interval.isVisible()
    assert dialog.send_mode.isVisible()
    assert dialog.launch_browser_button.isVisible()
    assert not dialog.api_test_button.isVisible()
    assert not dialog.base_url.isVisible()
    assert not dialog.model_path.isVisible()
    assert dialog.capacity_section.isVisible()
    assert dialog.time_section.isVisible()
    assert not dialog.poll_section.isVisible()
    assert dialog.web_poll_interval.isVisible()
    assert dialog.web_url.text() == WEB_AI_PRESETS["google_ai_studio"]["url"]
    assert dialog.send_mode.currentData() == "ctrl_enter"
    assert dialog.name.text()

    select("LOCAL_ONNX")
    assert dialog.model_path.isVisible()
    assert dialog.tokenizer_path.isVisible()
    assert dialog.model_dim.isVisible()
    assert dialog.cap_section.isVisible()
    assert not dialog.capacity_section.isVisible()
    assert not dialog.time_section.isVisible()
    assert not dialog.poll_section.isVisible()

    # SEMI_AUTO_CLIPBOARD was retired in V3.0 from standalone node types, moved into operation UI
    assert "SEMI_AUTO_CLIPBOARD" not in NODE_TYPE_LABELS
    dialog.close()


def test_node_preset_and_serialization_roundtrip():
    node = _node(proxy_url="http://127.0.0.1:7890")
    restored = EndpointNode.from_dict(node.as_dict())
    assert restored.web_preset == "google_ai_studio"
    assert restored.send_mode == "ctrl_enter"
    assert restored.proxy_url == "http://127.0.0.1:7890"
    assert restored.model == ""
    assert restored.web_model == "Gemini 4 Argon"
    assert restored.thinking_level == "High"
    assert restored.web_cdp_port == 9333
    assert restored.web_poll_interval_sec == 1.5
    assert "ctrl_enter" in SEND_MODES
    assert preset_key_for_url(WEB_AI_PRESETS["google_ai_studio"]["url"]) == "google_ai_studio"
    assert preset_key_for_url("https://example.com/chat") == "custom"
    assert merged_selectors(node)["send_mode"] == "ctrl_enter"


def test_google_preset_and_browser_options():
    node = _node(proxy_url="http://127.0.0.1:7890")
    options = browser_context_options(node, headless=False)
    assert options["headless"] is False
    assert "--disable-blink-features=AutomationControlled" in options["args"]
    assert "--start-maximized" in options["args"]
    assert options["ignore_default_args"] == ["--enable-automation"]
    assert options["proxy"] == {"server": "http://127.0.0.1:7890"}
    assert resolved_proxy_url(node) == "http://127.0.0.1:7890"


def test_browser_launch_falls_back_from_chrome_to_edge():
    calls = []

    class FakeBrowserType:
        def launch_persistent_context(self, profile, **kwargs):
            calls.append((profile, kwargs))
            if kwargs.get("channel") == "chrome":
                raise RuntimeError("chrome unavailable")
            return SimpleNamespace(profile=profile)

    fake = SimpleNamespace(chromium=FakeBrowserType())
    context = launch_persistent_browser(fake, _node(), "C:/tmp/profile")
    assert context.profile == "C:/tmp/profile"
    assert calls[0][1]["channel"] == "chrome"
    assert calls[1][1]["channel"] == "msedge"
    assert calls[1][1]["args"] == calls[0][1]["args"]


def test_login_worker_reference_is_cleared_before_delete():
    from ui.model_pool_panel import ModelPoolPanel

    deleted = []
    worker = SimpleNamespace(deleteLater=lambda: deleted.append(True))
    host = SimpleNamespace(_login_worker=worker)
    ModelPoolPanel._login_finished(host, worker)
    assert host._login_worker is None
    assert deleted == [True]


def test_gateway_routes_web_node_to_persistent_client(tmp_path):
    from config import AppConfig
    from models.gateway import ModelGateway, WebAIPlaywrightClient

    gateway = ModelGateway(AppConfig({}), tmp_path)
    node = _node(web_url="https://example.com/web")
    client = gateway._client_from_node("fast_llm", node)
    assert isinstance(client, WebAIPlaywrightClient)
    assert client.available
    assert client.model == "Gemini 4 Argon"
    client.bridge = SimpleNamespace(
        ask=lambda _node, prompt, timeout_ms=0: (
            prompt if "first" in prompt else '{"items":[]}'
        )
    )
    usage = []
    client.token_callback = lambda *args: usage.append(args)
    assert "first" in client.chat([{"role": "user", "content": "first"}])
    assert client.chat_json([{"role": "user", "content": "schema"}]) == {"items": []}
    assert usage


def test_web_session_never_reloads_page_and_uses_ctrl_enter(monkeypatch):
    import models.web_ai_bridge as bridge_module

    sleeps = []
    monkeypatch.setattr(bridge_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    class FakeLocator:
        def __init__(self, page, kind, text=""):
            self.page = page
            self.kind = kind
            self.text = text

        def count(self):
            return 1 if self.kind != "missing" else 0

        def nth(self, index):
            return self

        def is_visible(self, timeout=0):
            return self.kind != "missing"

        def inner_text(self, timeout=0):
            return self.text

        def fill(self, value, timeout=0):
            if self.kind == "input":
                self.page.fills.append(value)

        def focus(self, timeout=0):
            self.page.focused += 1

        def click(self, timeout=0):
            self.page.clicks.append(self.kind)

    class FakeKeyboard:
        def __init__(self, page):
            self.page = page

        def press(self, key):
            self.page.keys.append(key)

        def insert_text(self, text):
            self.page.fills.append(text)

        def type(self, text):
            self.page.fills.append(text)

    class FakePage:
        def __init__(self):
            self.url = WEB_AI_PRESETS["google_ai_studio"]["url"]
            self.goto_calls = []
            self.fills = []
            self.keys = []
            self.clicks = []
            self.focused = 0
            self.keyboard = FakeKeyboard(self)

        def is_closed(self):
            return False

        def goto(self, url, timeout=0):
            self.goto_calls.append(url)

        def set_default_timeout(self, timeout):
            pass

        def locator(self, selector):
            if "ms-chat-turn" in selector:
                return FakeLocator(self, "response", '{"items":[]}')
            if selector in ('button:has-text("Clear"), button:has-text("Delete turns")',):
                return FakeLocator(self, "missing")
            if "textarea" in selector or "contenteditable" in selector:
                return FakeLocator(self, "input")
            if "Stop" in selector or "Run" in selector:
                return FakeLocator(self, "missing")
            return FakeLocator(self, "missing")

        def inner_text(self, selector, timeout=0):
            return '{"items":[]}'

    page = FakePage()
    session = _PersistentBrowserSession.__new__(_PersistentBrowserSession)
    session.navigation_timeout_ms = 120_000
    custom_node = _node(
        web_preset="custom",
        web_url="https://example.com/chat",
        send_mode="ctrl_enter",
    )
    answer = session._ask_page(page, custom_node, "first packet", 5000)
    answer2 = session._ask_page(page, custom_node, "second packet", 5000)
    assert answer == answer2 == '{"items":[]}'
    assert page.goto_calls == []
    assert page.fills == ["first packet", "second packet"]
    assert page.keys == ["Control+Enter", "Control+Enter"]
    assert sleeps and sleeps[0] == 1.5

    assert _extract_json_payload('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert _extract_json_payload('prefix {"cards": []} suffix') == {"cards": []}


def test_aistudio_driver_waits_for_stop_and_extracts_model_json(monkeypatch):
    import models.aistudio_autopilot as autopilot

    monkeypatch.setattr(autopilot.time, "sleep", lambda _seconds: None)

    class FakeLocator:
        def __init__(self, text="", visible=True):
            self.text = text
            self.visible = visible
            self.first = self

        def count(self):
            return 1 if self.visible else 0

        def nth(self, _index):
            return self

        def is_visible(self, timeout=0):
            return self.visible

        def inner_text(self, timeout=0):
            return self.text

    class FakePage:
        def __init__(self):
            self.stop_checks = 0
            self.url = autopilot.AI_STUDIO_NEW_CHAT

        def locator(self, selector):
            if "Stop" in selector:
                self.stop_checks += 1
                return FakeLocator(visible=self.stop_checks == 1)
            if "model-response" in selector or "data-turn-role='model'" in selector:
                return FakeLocator('{"items": [{"asset_id": "a1"}]}')
            return FakeLocator(visible=False)

        def is_closed(self):
            return False

    driver = autopilot.AistudioPageDriver(FakePage())
    text = driver.wait_for_reply(
        prompt_text="Please return JSON.",
        timeout_seconds=3,
        poll_seconds=1.5,
    )
    assert '"items"' in text
    echo = '{"items": [{"a": "x"}]}' * 5
    assert autopilot.is_prompt_echo(echo, "Please return " + echo)
    assert not autopilot.is_prompt_echo('{"items": [{"asset_id": "a1"}]}', "Please return JSON.")


def test_run_button_finder_skips_rerun_and_run_settings():
    from models.aistudio_autopilot import find_run_button

    class FakeLocator:
        def __init__(self, text, aria="", disabled=False):
            self.text = text
            self.aria = aria
            self.disabled = disabled

        def is_visible(self):
            return True

        def inner_text(self, timeout=0):
            return self.text

        def get_attribute(self, name, timeout=0):
            return self.aria if name == "aria-label" else None

        def is_disabled(self):
            return self.disabled

    class FakeCollection:
        def __init__(self, items):
            self.items = items

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class FakePage:
        def locator(self, _selector):
            return FakeCollection([
                FakeLocator("Rerun this turn", "Rerun this turn"),
                FakeLocator("Run settings", "Run settings"),
                FakeLocator("Run | Ctrl | keyboard_return"),
                FakeLocator("Run", disabled=True),
            ])

    found = find_run_button(FakePage(), timeout=1.0)
    assert found is not None
    assert found.text.startswith("Run | Ctrl")
