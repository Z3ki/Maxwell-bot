"""Dispatcher name-collision fix + main-bot autofix (no sub-agents)."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from api.state import _sanitize_control
from autofix import (
    apply_patch,
    allowed_relpath,
    branch_name,
    fingerprint_error,
    is_autofixable,
    is_protected_branch,
    parse_llm_patch,
    sanitize_tool_args,
    schedule_tool_autofix,
    should_schedule,
    _git,
)
from bot import MaxwellBot, ToolCircuitBreaker
from control_defaults import DEFAULT_CONTROL


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


def test_fingerprint_stable_for_same_crash():
    err = TypeError("_invoke_request_tool() got multiple values for argument 'name'")
    tb = 'File "/root/maxwell/bot.py", line 1, in _execute_tool_by_name\nTypeError'
    a = fingerprint_error("create_site", err, tb)
    b = fingerprint_error("create_site", err, tb)
    assert a == b
    assert a != fingerprint_error("host_file", err, tb)


def test_is_autofixable_types():
    assert is_autofixable(TypeError("got multiple values for argument 'name'"))
    assert is_autofixable(AttributeError("x"))
    assert not is_autofixable(TimeoutError("slow"))
    assert not is_autofixable(ConnectionError("net"))
    assert not is_autofixable(OSError("disk"))


def test_protected_branch_names():
    assert is_protected_branch("main")
    assert is_protected_branch("refs/heads/master")
    assert is_protected_branch("origin/main")
    assert is_protected_branch("HEAD:main")
    assert not is_protected_branch("fix/autofix-TypeError-20260101T000000")
    assert not is_protected_branch("HEAD:fix/autofix-TypeError-1")


def test_branch_name_shape():
    name = branch_name("TypeError", when=1_700_000_000)
    assert name.startswith("fix/autofix-TypeError-")
    assert is_protected_branch(name) is False


def test_allowed_relpath_blocks_secrets(tmp_path):
    repo = tmp_path
    (repo / "bot.py").write_text("x", encoding="utf-8")
    assert allowed_relpath("bot.py", repo=repo) == (repo / "bot.py").resolve()
    assert allowed_relpath("tests/test_foo.py", repo=repo) is not None
    assert allowed_relpath("../bot.py", repo=repo) is None
    assert allowed_relpath(".env", repo=repo) is None
    assert allowed_relpath("data/bot_control.json", repo=repo) is None
    assert allowed_relpath("secrets.pem", repo=repo) is None
    assert allowed_relpath("/etc/passwd", repo=repo) is None


def test_parse_and_apply_patch(tmp_path):
    (tmp_path / "bot.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    blob = """
    here is the patch
    ```json
    {
      "summary": "bump alpha",
      "edits": [{"path": "bot.py", "old": "alpha = 1", "new": "alpha = 2"}],
      "new_files": [{"path": "tests/test_alpha.py", "content": "def test_ok():\\n    assert True\\n"}],
      "commit_message": "fix: alpha",
      "pr_title": "fix: alpha",
      "pr_body": "details"
    }
    ```
    """
    patch = parse_llm_patch(blob)
    changed = apply_patch(patch, root=tmp_path)
    assert "bot.py" in changed
    assert "tests/test_alpha.py" in changed
    assert (tmp_path / "bot.py").read_text(encoding="utf-8") == "alpha = 2\nbeta = 2\n"
    assert "assert True" in (tmp_path / "tests" / "test_alpha.py").read_text(
        encoding="utf-8"
    )


def test_apply_patch_refuses_overwrite_of_existing_non_test(tmp_path):
    (tmp_path / "bot.py").write_text("keep\n", encoding="utf-8")
    patch = {
        "edits": [],
        "new_files": [{"path": "bot.py", "content": "wiped\n"}],
    }
    with pytest.raises(ValueError, match="overwrite"):
        apply_patch(patch, root=tmp_path)


def test_git_refuses_push_to_main(tmp_path):
    with pytest.raises(RuntimeError, match="protected"):
        _git(["push", "origin", "main"], cwd=tmp_path)
    with pytest.raises(RuntimeError, match="protected"):
        _git(["push", "-u", "origin", "HEAD:main"], cwd=tmp_path)
    with pytest.raises(RuntimeError, match="force-push"):
        _git(["push", "--force", "origin", "fix/autofix-x"], cwd=tmp_path)
    with pytest.raises(RuntimeError, match="protected"):
        _git(["checkout", "main"], cwd=tmp_path)


def test_schedule_is_noop_under_pytest():
    bot = SimpleNamespace(
        _control={"autofix_enabled": True},
        ai_provider=object(),
        config=SimpleNamespace(DATA_DIR="data"),
    )
    assert (
        schedule_tool_autofix(
            bot,
            tool_name="create_site",
            tool_args={"name": "x"},
            exc=TypeError("got multiple values for argument 'name'"),
        )
        is False
    )


def test_should_schedule_honors_disable(monkeypatch, tmp_path):
    monkeypatch.setattr("autofix.is_under_pytest", lambda: False)
    monkeypatch.setattr("autofix.env_forced_off", lambda: False)
    bot = SimpleNamespace(
        _control={"autofix_enabled": False},
        ai_provider=object(),
        config=SimpleNamespace(DATA_DIR=str(tmp_path)),
    )
    err = TypeError("got multiple values for argument 'name'")
    assert should_schedule(bot, "create_site", err, "File x") is None
    bot._control["autofix_enabled"] = True
    fp = should_schedule(bot, "create_site", err, 'File "/root/maxwell/bot.py", line 1')
    assert fp


def test_autofix_control_defaults_and_clamps():
    assert DEFAULT_CONTROL["autofix_enabled"] is True
    assert DEFAULT_CONTROL["autofix_open_pr"] is True
    assert DEFAULT_CONTROL["autofix_max_per_hour"] == 3
    assert DEFAULT_CONTROL["autofix_cooldown_hours"] == 24
    out = _sanitize_control({"autofix_max_per_hour": 999, "autofix_cooldown_hours": 0})
    assert out["autofix_max_per_hour"] == 20
    assert out["autofix_cooldown_hours"] == 1
    assert _sanitize_control({"autofix_enabled": "false"})["autofix_enabled"] is False
