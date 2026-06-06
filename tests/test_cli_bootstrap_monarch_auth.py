from __future__ import annotations

import builtins
import sys
import types
from pathlib import Path

from studentaid_monarch_sync.cli import _bootstrap_monarch_auth


class _FakePage:
    def __init__(self) -> None:
        self.goto_calls: list[tuple[str, str]] = []

    async def goto(self, url: str, wait_until: str) -> None:
        self.goto_calls.append((url, wait_until))


class _FakeContext:
    def __init__(self) -> None:
        self.page = _FakePage()
        self.closed = False

    async def new_page(self) -> _FakePage:
        return self.page

    async def cookies(self) -> list[dict[str, str]]:
        return [
            {"name": "session_id", "value": "abc"},
            {"name": "csrftoken", "value": "def"},
            {"name": "extra_cookie", "value": "ignored"},
        ]

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.context = _FakeContext()
        self.closed = False
        self.launch_calls: list[dict[str, object]] = []

    async def new_context(self) -> _FakeContext:
        return self.context

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self) -> None:
        self.browser = _FakeBrowser()
        self.calls: list[dict[str, object]] = []

    async def launch(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.browser


class _FakeAsyncPlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium

    async def __aenter__(self):
        return types.SimpleNamespace(chromium=self.chromium)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeMonarchMoney:
    instances: list["_FakeMonarchMoney"] = []

    def __init__(self, session_file: str) -> None:
        self.session_file = session_file
        self.login_calls: list[dict[str, object]] = []
        self.__class__.instances.append(self)

    async def login_with_cookies(self, cookie_string: str, **kwargs) -> None:
        self.login_calls.append({"cookie_string": cookie_string, **kwargs})
        Path(self.session_file).write_text("saved-session", encoding="utf-8")


def test_bootstrap_monarch_auth_saves_session(monkeypatch, tmp_path, capsys) -> None:
    chromium = _FakeChromium()
    _FakeMonarchMoney.instances = []

    fake_playwright_mod = types.ModuleType("playwright.async_api")

    def async_playwright():
        return _FakeAsyncPlaywright(chromium)

    fake_playwright_mod.async_playwright = async_playwright
    fake_playwright_pkg = types.ModuleType("playwright")
    fake_playwright_pkg.__path__ = []  # mark as package
    fake_monarch_mod = types.ModuleType("monarchmoney")
    fake_monarch_mod.MonarchMoney = _FakeMonarchMoney

    monkeypatch.setitem(sys.modules, "playwright", fake_playwright_pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_playwright_mod)
    monkeypatch.setitem(sys.modules, "monarchmoney", fake_monarch_mod)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")

    out_path = tmp_path / "monarch_session.pickle"

    # `input()` is stubbed, so the flow runs end-to-end without manual interaction.
    import asyncio

    asyncio.run(_bootstrap_monarch_auth(login_url="https://app.monarchmoney.com", out_path=str(out_path)))

    assert chromium.calls == [{"headless": False}]
    assert chromium.browser.context.page.goto_calls == [("https://app.monarchmoney.com", "domcontentloaded")]
    assert _FakeMonarchMoney.instances[0].login_calls == [
        {
            "cookie_string": "session_id=abc; csrftoken=def",
            "save_session": True,
            "verify": True,
        }
    ]
    assert out_path.read_text(encoding="utf-8") == "saved-session"

    stdout = capsys.readouterr().out
    assert "Saved Monarch session" in stdout
