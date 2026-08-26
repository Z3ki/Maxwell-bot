"""Native sub-agent: the loop, the workdir sandbox, and the budgets."""

import asyncio

import pytest

import bot_tools
import json
import time
import types

from bot_tools import SubAgentTool, SubAgentMessageTool, _SubChan


def _call(name, args, call_id="1"):
    return {
        "id": call_id,
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class _ScriptedProvider:
    """Replays a fixed list of assistant messages, recording what it was fed."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    async def generate_chat_completion(self, messages=None, **kwargs):
        self.seen.append(list(messages or []))
        if not self.replies:
            return {"role": "assistant", "content": "out of script", "tool_calls": []}
        return self.replies.pop(0)


def _tool(provider, tmp_path, monkeypatch, sandbox="host"):
    monkeypatch.setenv("SUBAGENT_BASE_DIR", str(tmp_path))
    from config import Config

    monkeypatch.setattr(Config, "SUBAGENT_BASE_DIR", str(tmp_path))
    # The loop tests are about the loop, so they run commands directly rather
    # than in the Docker sandbox the tool defaults to — otherwise every one of
    # them needs a working daemon and pays a container start. The sandbox
    # itself is covered by the docker-gated tests at the bottom of this file.
    monkeypatch.setattr(Config, "SUBAGENT_SANDBOX", sandbox)
    return SubAgentTool(types.SimpleNamespace(provider=provider))


def test_writes_runs_and_reports(tmp_path, monkeypatch):
    provider = _ScriptedProvider(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    _call("write_file", {"path": "hi.py", "content": "print('hey')\n"})
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [_call("run_command", {"command": "python3 hi.py"}, "2")],
            },
            {
                "role": "assistant",
                "tool_calls": [_call("finish", {"report": "built and ran hi.py"}, "3")],
            },
        ]
    )
    tool = _tool(provider, tmp_path, monkeypatch)
    result = asyncio.run(tool.execute(None, task="write a hello script"))

    assert "built and ran hi.py" in result
    assert "files written: hi.py" in result
    # The command's real output was fed back to the model, not invented.
    command_result = provider.seen[-1][-1]
    assert command_result["role"] == "tool"
    tool_outputs = [m["content"] for m in provider.seen[-1] if m.get("role") == "tool"]
    assert any("hey" in out and "exit=0" in out for out in tool_outputs)


def test_uses_ai_provider_when_present(tmp_path, monkeypatch):
    # The real bot binds its LLM as ``bot.ai_provider``; there is no ``.provider``
    # attribute on it. The sub-agent must run on ``ai_provider``, not refuse with
    # "no LLM provider" (the bug that surfaced in production).
    provider = _ScriptedProvider(
        [
            {"role": "assistant", "tool_calls": [_call("finish", {"report": "worked"}, "1")]}
        ]
    )
    monkeypatch.setenv("SUBAGENT_BASE_DIR", str(tmp_path))
    from config import Config

    monkeypatch.setattr(Config, "SUBAGENT_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(Config, "SUBAGENT_SANDBOX", "host")
    # The bot object exposes ai_provider but no provider attribute.
    tool = SubAgentTool(types.SimpleNamespace(ai_provider=provider))
    result = asyncio.run(tool.execute(None, task="write a hello script"))
    assert "worked" in result
    assert result != "sub_agent is unavailable: no LLM provider on this bot."


def test_paths_cannot_escape_the_workdir(tmp_path, monkeypatch):
    provider = _ScriptedProvider(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    _call("write_file", {"path": "../escaped.txt", "content": "nope"})
                ],
            },
            {"role": "assistant", "tool_calls": [_call("finish", {"report": "done"}, "2")]},
        ]
    )
    tool = _tool(provider, tmp_path, monkeypatch)
    asyncio.run(tool.execute(None, task="try to escape"))

    assert not (tmp_path / "escaped.txt").exists()
    outputs = [m["content"] for m in provider.seen[-1] if m.get("role") == "tool"]
    assert any("escapes the workdir" in out for out in outputs)


def test_read_outside_workdir_is_refused(tmp_path, monkeypatch):
    provider = _ScriptedProvider(
        [
            {
                "role": "assistant",
                "tool_calls": [_call("read_file", {"path": "../../etc/passwd"})],
            },
            {"role": "assistant", "tool_calls": [_call("finish", {"report": "done"}, "2")]},
        ]
    )
    tool = _tool(provider, tmp_path, monkeypatch)
    asyncio.run(tool.execute(None, task="read something"))

    outputs = [m["content"] for m in provider.seen[-1] if m.get("role") == "tool"]
    assert any("escapes the workdir" in out for out in outputs)


def test_step_budget_ends_the_loop(tmp_path, monkeypatch):
    # A model that never calls finish must still terminate.
    provider = _ScriptedProvider(
        [
            {"role": "assistant", "tool_calls": [_call("list_files", {}, str(i))]}
            for i in range(10)
        ]
    )
    tool = _tool(provider, tmp_path, monkeypatch)
    result = asyncio.run(tool.execute(None, task="loop forever", max_steps=3))

    assert "used all 3 steps" in result
    assert len(provider.seen) == 3


def test_prose_without_a_tool_call_is_reported(tmp_path, monkeypatch):
    provider = _ScriptedProvider(
        [{"role": "assistant", "content": "I finished the thing.", "tool_calls": []}]
    )
    tool = _tool(provider, tmp_path, monkeypatch)
    result = asyncio.run(tool.execute(None, task="do a thing"))

    assert "I finished the thing." in result


def test_missing_task_is_rejected(tmp_path, monkeypatch):
    tool = _tool(_ScriptedProvider([]), tmp_path, monkeypatch)
    assert "needs a `task`" in asyncio.run(tool.execute(None))


def test_command_timeout_is_reported_not_raised(tmp_path, monkeypatch):
    from config import Config

    monkeypatch.setattr(Config, "SUBAGENT_COMMAND_TIMEOUT_SECONDS", 1)
    provider = _ScriptedProvider(
        [
            {"role": "assistant", "tool_calls": [_call("run_command", {"command": "sleep 30"})]},
            {"role": "assistant", "tool_calls": [_call("finish", {"report": "done"}, "2")]},
        ]
    )
    tool = _tool(provider, tmp_path, monkeypatch)
    asyncio.run(tool.execute(None, task="sleep too long"))

    outputs = [m["content"] for m in provider.seen[-1] if m.get("role") == "tool"]
    assert any("timed out" in out for out in outputs)


# ─── event streaming ─────────────────────────────────────────────────────


def test_a_run_publishes_its_progress(tmp_path, monkeypatch):
    """A run is minutes of silence otherwise — this is what fills it."""
    from agent_events import AgentEventBus

    bus = AgentEventBus()
    provider = _ScriptedProvider(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    _call("write_file", {"path": "a.py", "content": "print(1)\n"})
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [_call("run_command", {"command": "python3 a.py"}, "2")],
            },
            {"role": "assistant", "tool_calls": [_call("finish", {"report": "ok"}, "3")]},
        ]
    )
    tool = _tool(provider, tmp_path, monkeypatch)
    tool.bot.agent_events = bus
    message = types.SimpleNamespace(
        author=types.SimpleNamespace(display_name="alice"),
        channel=types.SimpleNamespace(id=7),
    )

    asyncio.run(tool.execute(message, task="write a script"))

    run = bus.snapshot()[0]
    assert run["status"] == "done"
    assert run["task"] == "write a script"
    assert run["requested_by"] == "alice"
    assert run["channel_id"] == "7"
    assert run["steps"] == 3
    assert run["commands_run"] == 1
    assert run["files_written"] == ["a.py"]

    events = bus.events(run["run_id"])
    assert [e["type"] for e in events][0] == "start"
    assert [e["type"] for e in events][-1] == "finish"
    labels = [e.get("label", "") for e in events]
    # The label is what a human reads, so it carries the command, not the
    # tool's name.
    assert any("running: python3 a.py" in label for label in labels)
    assert any("writing: a.py" in label for label in labels)
    assert any("step 1/" in label for label in labels)


def test_a_run_without_a_bus_still_works(tmp_path, monkeypatch):
    provider = _ScriptedProvider(
        [{"role": "assistant", "tool_calls": [_call("finish", {"report": "done"})]}]
    )
    tool = _tool(provider, tmp_path, monkeypatch)
    assert "done" in asyncio.run(tool.execute(None, task="no telemetry here"))


# ─── the docker sandbox ──────────────────────────────────────────────────


def _docker_available() -> bool:
    import subprocess

    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=20
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def test_sandbox_mode_defaults_to_docker(tmp_path, monkeypatch):
    from config import Config

    tool = _tool(_ScriptedProvider([]), tmp_path, monkeypatch, sandbox="docker")
    assert tool._sandbox_mode() == "docker"
    for opt_out in ("host", "off", "none", "0", "false", "HOST"):
        monkeypatch.setattr(Config, "SUBAGENT_SANDBOX", opt_out)
        assert tool._sandbox_mode() == "host"
    # Anything unrecognised stays sandboxed. Failing open on a typo is how
    # you end up thinking you are isolated when you are not.
    monkeypatch.setattr(Config, "SUBAGENT_SANDBOX", "sanbdox")
    assert tool._sandbox_mode() == "docker"


def test_missing_docker_refuses_rather_than_falling_back(tmp_path, monkeypatch):
    tool = _tool(_ScriptedProvider([]), tmp_path, monkeypatch, sandbox="docker")

    async def _no_docker(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(bot_tools, "_run_docker_cmd", _no_docker)
    out = asyncio.run(tool._run_command(tmp_path, "echo hi"))
    # A silent downgrade to the host would be the worst possible outcome.
    assert "error:" in out
    assert "SUBAGENT_SANDBOX=host" in out


@pytest.mark.skipif(not _docker_available(), reason="needs a docker daemon")
def test_the_sandbox_cannot_see_the_bot_source_tree(tmp_path, monkeypatch):
    """The reason this exists: the bot's .env was one `cat` away."""
    tool = _tool(_ScriptedProvider([]), tmp_path, monkeypatch, sandbox="docker")
    workspace = tmp_path / "work"
    workspace.mkdir()

    async def run():
        try:
            inside = await tool._run_command(workspace, "echo hello; ls /root/maxwell")
            escaped = await tool._run_command(
                workspace, "test -f /root/maxwell/.env && echo LEAKED || echo safe"
            )
            return inside, escaped
        finally:
            await tool._stop_sandbox(workspace)

    inside, escaped = asyncio.run(run())
    assert "hello" in inside
    assert "LEAKED" not in escaped
    assert "safe" in escaped


class _FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.last_message = None

    async def send(self, content, **kwargs):
        msg = _FakeMessage(content)
        self.last_message = msg
        return msg


class _FakeMessage:
    def __init__(self, content):
        self.content = content
        self.edits = []

    async def edit(self, content=None, **kwargs):
        self.edits.append(content)
        if content is not None:
            self.content = content


def test_sub_agent_can_message_main(tmp_path, monkeypatch):
    # sub -> main: message_main pushes to the run's channel and posts to the
    # delivery target so Maxwell and the channel see it, and keeps working.
    provider = _ScriptedProvider(
        [
            {"role": "assistant", "tool_calls": [_call("message_main", {"text": "need a decision"}, "1")]},
            {"role": "assistant", "tool_calls": [_call("finish", {"report": "done"}, "2")]},
        ]
    )
    tool = _tool(provider, tmp_path, monkeypatch)
    target = _FakeChannel(9)
    chan = _SubChan("r1")
    chan.target = target
    tool._chans["r1"] = chan

    result = asyncio.run(tool._message_main("r1", "need a decision"))
    assert "sent to Maxwell" in result
    assert chan.msgs[-1]["src"] == "sub"
    assert chan.msgs[-1]["text"] == "need a decision"
    # Quiet relay: the sub-agent's message is NOT posted to the channel.
    assert target.last_message is None


def test_sub_agent_message_main_tool(tmp_path, monkeypatch):
    # main -> sub: the main-facing tool pushes to a running sub-agent and
    # returns the conversation thread.
    tool = _tool(_ScriptedProvider([]), tmp_path, monkeypatch)
    chan = _SubChan("r2")
    tool._chans["r2"] = chan
    bot = types.SimpleNamespace(tools={"sub_agent": tool})
    main_tool = SubAgentMessageTool(bot)

    result = asyncio.run(main_tool.execute(None, run_id="r2", text="do X"))
    assert "Sent to the sub-agent" in result
    assert chan.msgs[-1]["src"] == "main"
    assert chan.msgs[-1]["text"] == "do X"
    assert "do X" in result

    # Unknown run id is a clear error, not a crash.
    bad = asyncio.run(main_tool.execute(None, run_id="nope", text="hi"))
    assert "no running sub-agent" in bad


def test_main_message_is_injected_into_the_sub_agent_loop(tmp_path, monkeypatch):
    # A main -> sub message must land inside the sub-agent's context on its
    # next step, so it can answer it.
    provider = _ScriptedProvider(
        [{"role": "assistant", "tool_calls": [_call("finish", {"report": "done"}, "1")]}]
    )
    tool = _tool(provider, tmp_path, monkeypatch)
    conv = _SubChan("r3")
    conv.push("main", "answer me")

    asyncio.run(
        tool._agent_loop(
            "task",
            tmp_path,
            max_steps=2,
            deadline=time.time() + 30,
            model=None,
            provider=provider,
            conv=conv,
        )
    )
    injected = [m for m in provider.seen[0] if "Message from Maxwell/main agent" in str(m.get("content") or "")]
    assert injected and "answer me" in injected[0]["content"]


def test_sub_agent_can_call_bot_tools(tmp_path, monkeypatch):
    # `bot_call` lets a sub-agent finish a job that needs a bot/host capability
    # (create_site etc.) on the host, running as the requesting user.
    called = {}

    class FakeCreateSite:
        async def execute(self, message, **kwargs):
            called["author"] = str(message.author.id)
            called["args"] = kwargs
            return f"published site {kwargs.get('name')}"

    class FakeBot:
        provider = _ScriptedProvider([])
        tools = {"create_site": FakeCreateSite()}

        def get_channel(self, cid):
            return None

        async def fetch_channel(self, cid):
            return None

        def get_user(self, uid):
            return None

        async def fetch_user(self, uid):
            return None

    from config import Config

    monkeypatch.setattr(Config, "SUBAGENT_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(Config, "SUBAGENT_SANDBOX", "host")
    tool = SubAgentTool(FakeBot())
    msg = types.SimpleNamespace(
        author=types.SimpleNamespace(id=77, display_name="alice"),
        channel=types.SimpleNamespace(id=5),
    )

    result = asyncio.run(
        tool._bot_call(msg, "create_site", {"name": "test", "title": "T", "body": "<html></html>"})
    )
    assert "published site test" in result
    assert called["author"] == "77"
    assert called["args"]["name"] == "test"

    # Non-whitelisted tools are refused, not run.
    blocked = asyncio.run(tool._bot_call(msg, "ban_member", {"user_id": "1"}))
    assert "not available to sub-agents" in blocked


@pytest.mark.skipif(not _docker_available(), reason="needs a docker daemon")
def test_the_sandbox_keeps_state_between_commands(tmp_path, monkeypatch):
    # One container per run, not one per command: an installed package or a
    # built binary has to survive to the next step.
    tool = _tool(_ScriptedProvider([]), tmp_path, monkeypatch, sandbox="docker")
    workspace = tmp_path / "work"
    workspace.mkdir()

    async def run():
        try:
            await tool._run_command(workspace, "echo persisted > marker.txt")
            return await tool._run_command(workspace, "cat marker.txt")
        finally:
            await tool._stop_sandbox(workspace)

    assert "persisted" in asyncio.run(run())
    # Written through the bind mount, so the host tools see it too.
    assert (workspace / "marker.txt").read_text().strip() == "persisted"
