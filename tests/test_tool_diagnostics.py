"""Dispatcher argument validation and safe diagnostic traces."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace


from diagnostics_safety import redact_diagnostics, sanitize_tool_args

from bot import MaxwellBot, ToolCircuitBreaker


def test_configured_credentials_are_redacted_from_diagnostics(monkeypatch):
    secret = "audit-only-fake-credential"
    monkeypatch.setenv("DISCORD_TOKEN", secret)
    assert secret not in redact_diagnostics(f"TypeError: login failed with {secret}")
    assert sanitize_tool_args({"OPENAI_API_KEY": "not-env-value"}) == {
        "OPENAI_API_KEY": "[redacted]"
    }


class _FakeTool:
    def __init__(self):
        self.calls = []

    async def execute(self, message, **kwargs):
        self.calls.append((message, kwargs))
        return f"ok:{kwargs.get('name')}"


def _message(mid=7):
    return SimpleNamespace(id=mid, channel=SimpleNamespace(id=22), author=SimpleNamespace(id=1))


def test_invoke_request_tool_signature_is_positional_only():
    sig = inspect.signature(MaxwellBot._invoke_request_tool)
    assert "name" not in sig.parameters
    assert "tool_identifier" in sig.parameters
    for key in ("inbound_message", "tool_identifier", "handler"):
        assert sig.parameters[key].kind is inspect.Parameter.POSITIONAL_ONLY


def test_invoke_request_tool_accepts_name_kwarg():
    """create_site(name=...) used to TypeError on the dispatcher."""
    bot = object.__new__(MaxwellBot)
    tool = _FakeTool()
    message = _message()

    async def run():
        return await MaxwellBot._invoke_request_tool(
            bot, message, "create_site", tool, name="demo-site", title="Hi"
        )

    result = asyncio.run(run())
    assert result == "ok:demo-site"
    assert tool.calls[0][0] is message
    assert tool.calls[0][1]["name"] == "demo-site"
    assert tool.calls[0][1]["title"] == "Hi"


def test_invoke_request_tool_strips_message_kwarg():
    bot = object.__new__(MaxwellBot)
    tool = _FakeTool()
    message = _message()

    async def run():
        return await MaxwellBot._invoke_request_tool(
            bot,
            message,
            "host_file",
            tool,
            name="clip",
            message="should-not-shadow",
        )

    asyncio.run(run())
    assert "message" not in tool.calls[0][1]
    assert tool.calls[0][1]["name"] == "clip"


def test_execute_tool_by_name_forwards_name_argument():
    tool = _FakeTool()
    bot = SimpleNamespace(
        tools={"create_site": tool},
        _control={"tools_enabled": True, "disabled_tools": [], "autofix_enabled": False},
        _tool_breaker=ToolCircuitBreaker(failure_threshold=999, recovery_seconds=0),
        _tainted_messages=set(),
        plugin_manager=None,
        tool_concurrency=None,
        traces=[],
    )
    bot._message_tool_platform = lambda _m: "discord"
    bot._compatible_tool_names = lambda _p: {"create_site"}
    bot.is_message_tainted = lambda _m: False
    bot._consume_destructive_confirm = lambda _a: False
    bot._render_custom_emojis = lambda text, _g: text
    bot._progress_enabled = lambda _sid: False

    async def _record_llm_trace(message, payload):
        bot.traces.append(payload)

    bot._record_llm_trace = _record_llm_trace
    message = _message()

    async def run():
        return await MaxwellBot._execute_tool_by_name(
            bot,
            message,
            "create_site",
            {"name": "my-site", "title": "Hello", "body": "<h1>x</h1>"},
            disabled=set(),
            compatible={"create_site"},
        )

    result = asyncio.run(run())
    assert result.startswith("Tool create_site: ok:my-site")
    assert tool.calls[0][1]["name"] == "my-site"
    assert tool.calls[0][1]["title"] == "Hello"


def test_sanitize_tool_args_redacts_secrets_and_trims():
    out = sanitize_tool_args(
        {
            "name": "demo",
            "api_key": "sk-live-secret",
            "token": "abc",
            "body": "x" * 500,
            "nested": {"password": "hunter2", "ok": True},
        }
    )
    assert out["name"] == "demo"
    assert out["api_key"] == "[redacted]"
    assert out["token"] == "[redacted]"
    assert out["nested"]["password"] == "[redacted]"
    assert out["nested"]["ok"] is True
    assert out["body"].endswith("chars]")
