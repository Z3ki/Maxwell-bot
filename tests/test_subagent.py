"""Native sub-agent: the loop, the workdir sandbox, and the budgets."""

import asyncio
import json
import types

from bot_tools import SubAgentTool


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


def _tool(provider, tmp_path, monkeypatch):
    monkeypatch.setenv("SUBAGENT_BASE_DIR", str(tmp_path))
    from config import Config

    monkeypatch.setattr(Config, "SUBAGENT_BASE_DIR", str(tmp_path))
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
